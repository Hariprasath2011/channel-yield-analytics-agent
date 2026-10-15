"""PostgreSQL row-level locking concurrency tests for ingestion service.

Verifies:
  - Genuine concurrency across separate threads and separate database sessions.
  - Serialization of concurrent ingestion requests for the same tenant via row locking.
  - Stale job recovery atomicity: concurrent requests cannot both pass the active-job guard.
  - Tenant isolation: concurrent requests for distinct tenants execute independently.

Requirements:
  - Uses an isolated disposable PostgreSQL test schema to never mutate existing data.
  - Cleans up schemas automatically in fixture teardown.
  - Explicitly skips with a clear reason if PostgreSQL is unreachable.
"""
from __future__ import annotations

import concurrent.futures
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Generator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.core.exceptions import ConflictError, OAuthError
from app.ingestion.service import IngestionService
import app.ingestion.service as ing_mod
from app.integrations.zoho.client import ZohoRecruitClient
from app.integrations.zoho.service import ZohoService
from app.persistence.database import Base
from app.persistence.models import AuditEvent, ImportJob, OAuthState, Tenant, ZohoIntegration


def _is_postgres_available() -> bool:
    """Check whether a real PostgreSQL database is reachable."""
    try:
        settings = get_settings()
        if not settings.database_url.startswith("postgresql"):
            return False
        engine = create_engine(settings.database_url, connect_args={"connect_timeout": 3})
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        return True
    except Exception:
        return False


postgres_available = _is_postgres_available()

pytestmark = pytest.mark.skipif(
    not postgres_available,
    reason="PostgreSQL database unavailable or not configured. Skipping PostgreSQL row-locking concurrency tests.",
)


@pytest.fixture(scope="module")
def pg_isolated_engine():
    """Create a disposable PostgreSQL schema and return an engine bound exclusively to it."""
    settings = get_settings()
    schema_name = f"test_concurrency_{uuid.uuid4().hex[:8]}"

    # Root engine to manage schema lifecycle
    admin_engine = create_engine(settings.database_url, pool_pre_ping=True)
    with admin_engine.connect() as conn:
        conn.execute(text(f"CREATE SCHEMA {schema_name}"))
        conn.commit()

    # Schema-scoped engine for test execution
    schema_engine = create_engine(
        settings.database_url,
        connect_args={"options": f"-csearch_path={schema_name}"},
        pool_size=10,
        max_overflow=10,
        pool_pre_ping=True,
    )
    Base.metadata.create_all(bind=schema_engine)

    try:
        yield schema_engine, schema_name
    finally:
        schema_engine.dispose()
        with admin_engine.connect() as conn:
            conn.execute(text(f"DROP SCHEMA IF EXISTS {schema_name} CASCADE"))
            conn.commit()
        admin_engine.dispose()


@pytest.fixture
def pg_session_factory(pg_isolated_engine) -> sessionmaker[Session]:
    schema_engine, _ = pg_isolated_engine
    return sessionmaker(bind=schema_engine, autocommit=False, autoflush=False, expire_on_commit=False)


@pytest.fixture
def pg_db(pg_session_factory) -> Generator[Session, None, None]:
    session = pg_session_factory()
    try:
        yield session
    finally:
        session.close()


def test_postgres_concurrent_ingest_same_tenant_rejects_duplicate_active_job(
    pg_session_factory: sessionmaker[Session],
    pg_db: Session,
    monkeypatch: pytest.MonkeyPatch,
):
    """Verify two concurrent ingestion requests for the same tenant cannot both create active running jobs.

    One request acquires the tenant row lock, commits a 'running' job, and proceeds with processing.
    The concurrent request must be serialized behind the lock and receive 409 Conflict once unblocked.
    """
    tenant = Tenant(
        id=f"tenant-lock-{uuid.uuid4().hex[:6]}",
        slug=f"slug-{uuid.uuid4().hex[:6]}",
        name="Lock Test Tenant",
    )
    pg_db.add(tenant)
    pg_db.commit()

    # Introduce a brief delay during processing so the job remains in 'running' status while
    # the second worker attempts ingestion.
    orig_validate = ing_mod.validate_bundle

    def _slow_validate(bundle):
        time.sleep(0.15)
        return orig_validate(bundle)

    monkeypatch.setattr(ing_mod, "validate_bundle", _slow_validate)

    barrier = threading.Barrier(2)

    def _worker(worker_id: int) -> dict[str, Any]:
        with pg_session_factory() as session:
            barrier.wait()
            try:
                res = IngestionService.ingest_bundle(
                    session,
                    tenant_id=tenant.id,
                    files={},
                    optional_entities=["requisitions", "applications", "stage_events", "costs"],
                )
                return {"outcome": "success", "worker_id": worker_id, "job_id": res.get("job_id")}
            except ConflictError as exc:
                return {"outcome": "conflict", "worker_id": worker_id, "error": str(exc)}
            except Exception as exc:
                return {"outcome": "error", "worker_id": worker_id, "error": f"{type(exc).__name__}: {exc}"}

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(_worker, 1)
        f2 = executor.submit(_worker, 2)
        r1 = f1.result()
        r2 = f2.result()

    outcomes = [r1["outcome"], r2["outcome"]]
    assert "success" in outcomes, f"One worker must succeed, got {outcomes}"
    assert "conflict" in outcomes, f"The concurrent worker must receive 409 Conflict, got {outcomes}"

    # Verify only 1 job was created for this tenant
    jobs = pg_db.query(ImportJob).filter(ImportJob.tenant_id == tenant.id).all()
    assert len(jobs) == 1, f"Expected exactly 1 job created, found {len(jobs)}"


def test_postgres_concurrent_stale_job_recovery_creates_only_one_job(
    pg_session_factory: sessionmaker[Session],
    pg_db: Session,
):
    """Verify that during stale-job recovery, concurrent requests cannot both pass the active-job guard.

    Prior flaw: stale recovery committed before creating the replacement job, releasing
    the tenant row lock and allowing both concurrent workers to create new running jobs.
    With the fix, the tenant lock is held continuously until the replacement running job is committed.
    """
    tenant = Tenant(
        id=f"tenant-stale-{uuid.uuid4().hex[:6]}",
        slug=f"slug-stale-{uuid.uuid4().hex[:6]}",
        name="Stale Lock Tenant",
    )
    pg_db.add(tenant)

    # Seed a stale running job (45 minutes old)
    stale_time = datetime.now(timezone.utc) - timedelta(minutes=45)
    stale_job = ImportJob(
        tenant_id=tenant.id,
        status="running",
        source_system_id="crashed_worker",
        mapping_version="1.0",
        started_at=stale_time,
        created_at=stale_time,
    )
    pg_db.add(stale_job)
    pg_db.commit()
    stale_job_id = stale_job.id

    barrier = threading.Barrier(2)

    def _worker(worker_id: int) -> dict[str, Any]:
        with pg_session_factory() as session:
            barrier.wait()
            try:
                res = IngestionService.ingest_bundle(
                    session,
                    tenant_id=tenant.id,
                    files={},
                    optional_entities=["requisitions", "applications", "stage_events", "costs"],
                )
                return {"outcome": "success", "worker_id": worker_id, "job_id": res.get("job_id")}
            except ConflictError as exc:
                return {"outcome": "conflict", "worker_id": worker_id, "error": str(exc)}
            except Exception as exc:
                return {"outcome": "error", "worker_id": worker_id, "error": f"{type(exc).__name__}: {exc}"}

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(_worker, 1)
        f2 = executor.submit(_worker, 2)
        r1 = f1.result()
        r2 = f2.result()

    outcomes = [r1["outcome"], r2["outcome"]]
    assert "success" in outcomes, f"One worker must recover stale job and succeed: {outcomes}"
    assert "conflict" in outcomes, f"Concurrent worker must be blocked with ConflictError: {outcomes}"

    # Verify database state
    pg_db.expire_all()
    recovered_job = pg_db.query(ImportJob).filter(ImportJob.id == stale_job_id).first()
    assert recovered_job is not None
    assert recovered_job.status == "failed"
    assert "stale_job_timeout" in (recovered_job.error_summary or "")

    # Exactly 1 new replacement job created (total 2 jobs for tenant: 1 failed stale + 1 replacement)
    all_jobs = pg_db.query(ImportJob).filter(ImportJob.tenant_id == tenant.id).all()
    assert len(all_jobs) == 2, f"Expected 2 total jobs (1 failed stale + 1 replacement), got {len(all_jobs)}"

    # Audit event recorded for stale recovery
    audit = (
        pg_db.query(AuditEvent)
        .filter(AuditEvent.tenant_id == tenant.id, AuditEvent.event_type == "ingestion.stale_job_recovered")
        .first()
    )
    assert audit is not None
    assert audit.outcome == "failure"


def test_postgres_concurrent_different_tenants_independent(
    pg_session_factory: sessionmaker[Session],
    pg_db: Session,
):
    """Verify that concurrent ingestion requests for different tenants run independently without blocking."""
    t1 = Tenant(
        id=f"tenant-indep-1-{uuid.uuid4().hex[:6]}",
        slug=f"slug-indep-1-{uuid.uuid4().hex[:6]}",
        name="Independent Tenant 1",
    )
    t2 = Tenant(
        id=f"tenant-indep-2-{uuid.uuid4().hex[:6]}",
        slug=f"slug-indep-2-{uuid.uuid4().hex[:6]}",
        name="Independent Tenant 2",
    )
    pg_db.add_all([t1, t2])
    pg_db.commit()

    barrier = threading.Barrier(2)

    def _worker(tenant_id: str) -> dict[str, Any]:
        with pg_session_factory() as session:
            barrier.wait()
            try:
                res = IngestionService.ingest_bundle(
                    session,
                    tenant_id=tenant_id,
                    files={},
                    optional_entities=["requisitions", "applications", "stage_events", "costs"],
                )
                return {"outcome": "success", "tenant_id": tenant_id, "job_id": res.get("job_id")}
            except Exception as exc:
                return {"outcome": "error", "tenant_id": tenant_id, "error": f"{type(exc).__name__}: {exc}"}

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(_worker, t1.id)
        f2 = executor.submit(_worker, t2.id)
        r1 = f1.result()
        r2 = f2.result()

    assert r1["outcome"] == "success", f"Tenant 1 failed: {r1}"
    assert r2["outcome"] == "success", f"Tenant 2 failed: {r2}"
    assert r1["job_id"] != r2["job_id"]

    # Both tenants have their job recorded
    j1 = pg_db.query(ImportJob).filter(ImportJob.tenant_id == t1.id).all()
    j2 = pg_db.query(ImportJob).filter(ImportJob.tenant_id == t2.id).all()
    assert len(j1) == 1
    assert len(j2) == 1


class _ConcurrentTokenHttp:
    """Minimal Zoho token endpoint mock for concurrent claim tests."""

    def post(self, url: str, data: dict):
        class _Resp:
            status_code = 200
            headers = {}

            def json(self):
                return {
                    "access_token": "concurrent_access",
                    "refresh_token": "concurrent_refresh",
                    "expires_in": 3600,
                    "api_domain": "https://recruit.zoho.com",
                    "scope": "ZohoRecruit.modules.ALL",
                }

        return _Resp()


def test_postgres_concurrent_oauth_state_claim(
    pg_session_factory: sessionmaker[Session],
    pg_db: Session,
):
    """Exactly one of two concurrent callbacks may claim the same unused OAuth state."""
    tenant = Tenant(
        id=f"tenant-oauth-{uuid.uuid4().hex[:6]}",
        slug=f"slug-oauth-{uuid.uuid4().hex[:6]}",
        name="OAuth Claim Tenant",
    )
    pg_db.add(tenant)
    pg_db.commit()

    state = ZohoService.create_oauth_state(pg_db, tenant.id, data_center="com")
    barrier = threading.Barrier(2)

    def _worker() -> str:
        client = ZohoRecruitClient(
            client_id="cid",
            client_secret="sec",
            redirect_uri="https://cb.test",
            data_center="com",
            http_client=_ConcurrentTokenHttp(),
        )
        with pg_session_factory() as session:
            barrier.wait()
            try:
                ZohoService.exchange_and_save_tokens(
                    session,
                    tenant_id=tenant.id,
                    state_token=state,
                    code="valid_auth_code",
                    client=client,
                )
                return "success"
            except OAuthError:
                return "rejected"
            except Exception as exc:
                return f"error:{type(exc).__name__}"

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = [f.result() for f in (executor.submit(_worker), executor.submit(_worker))]

    assert sorted(outcomes) == ["rejected", "success"]
    pg_db.expire_all()
    row = pg_db.query(OAuthState).filter(OAuthState.state_token == state).one()
    assert row.used is True
    integrations = (
        pg_db.query(ZohoIntegration).filter(ZohoIntegration.tenant_id == tenant.id).all()
    )
    assert len(integrations) == 1
    assert integrations[0].status == "active"
