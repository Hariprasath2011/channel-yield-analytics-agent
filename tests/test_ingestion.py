"""Tests for tenant-scoped canonical data ingestion, on-demand refresh, and rollback safety."""
from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone
from typing import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth.passwords import hash_password
from app.auth.tokens import create_access_token
from app.main import create_app
from app.persistence.database import Base, get_db
from app.persistence.models import AuditEvent, CanonicalRecord, ImportJob, Membership, Tenant, User

SAMPLE_REQS_CSV = """requisition_id,job_title,status,source_record_id
REQ-101,Backend Engineer,open,SRC-REQ-101
REQ-102,Frontend Engineer,open,SRC-REQ-102
"""

SAMPLE_APPS_CSV = """application_id,requisition_id,source_channel,applied_at,application_outcome,joined_at,source_record_id
APP-201,REQ-101,LinkedIn,2026-02-01T10:00:00Z,joined,2026-03-01T10:00:00Z,SRC-APP-201
APP-202,REQ-101,Agency,2026-02-05T10:00:00Z,rejected,,SRC-APP-202
APP-203,REQ-102,unknown,2026-02-10T10:00:00Z,joined,2026-03-10T10:00:00Z,SRC-APP-203
"""


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def client(db_session: Session) -> Generator[TestClient, None, None]:
    app = create_app()

    def _override_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = _override_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def setup_tenants(db_session: Session):
    # Tenant A
    tenant_a = Tenant(slug="tenant-a", name="Tenant A Corp")
    db_session.add(tenant_a)
    db_session.flush()

    user_admin_a = User(
        tenant_id=tenant_a.id,
        email="admin@tenant-a.com",
        full_name="Admin A",
        hashed_password=hash_password("admin_pass_123"),
    )
    user_viewer_a = User(
        tenant_id=tenant_a.id,
        email="viewer@tenant-a.com",
        full_name="Viewer A",
        hashed_password=hash_password("viewer_pass_123"),
    )
    db_session.add_all([user_admin_a, user_viewer_a])
    db_session.flush()

    mem_admin_a = Membership(tenant_id=tenant_a.id, user_id=user_admin_a.id, role="admin")
    mem_viewer_a = Membership(tenant_id=tenant_a.id, user_id=user_viewer_a.id, role="viewer")
    db_session.add_all([mem_admin_a, mem_viewer_a])

    # Tenant B
    tenant_b = Tenant(slug="tenant-b", name="Tenant B Corp")
    db_session.add(tenant_b)
    db_session.flush()

    user_admin_b = User(
        tenant_id=tenant_b.id,
        email="admin@tenant-b.com",
        full_name="Admin B",
        hashed_password=hash_password("admin_pass_456"),
    )
    db_session.add(user_admin_b)
    db_session.flush()
    mem_admin_b = Membership(tenant_id=tenant_b.id, user_id=user_admin_b.id, role="admin")
    db_session.add(mem_admin_b)

    db_session.commit()

    token_admin_a = create_access_token(user_id=user_admin_a.id, tenant_id=tenant_a.id, role="admin")
    token_viewer_a = create_access_token(user_id=user_viewer_a.id, tenant_id=tenant_a.id, role="viewer")
    token_admin_b = create_access_token(user_id=user_admin_b.id, tenant_id=tenant_b.id, role="admin")

    return {
        "tenant_a": tenant_a,
        "tenant_b": tenant_b,
        "token_admin_a": token_admin_a,
        "token_viewer_a": token_viewer_a,
        "token_admin_b": token_admin_b,
    }


def test_refresh_authorized_succeeds(client: TestClient, setup_tenants):
    token = setup_tenants["token_admin_a"]
    resp = client.post(
        "/v2/ingestion/refresh",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "source_system_id": "test_ats",
            "mapping_version": "1.0",
            "csv_payloads": {
                "requisitions": SAMPLE_REQS_CSV,
                "applications": SAMPLE_APPS_CSV,
            },
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] in ("completed", "completed_with_warnings")
    assert data["summary"]["inserted"] == 5
    assert data["summary"]["committed"] is True


def test_refresh_unauthorized_rejected(client: TestClient, setup_tenants):
    # No auth
    resp = client.post(
        "/v2/ingestion/refresh",
        json={"csv_payloads": {"requisitions": SAMPLE_REQS_CSV}},
    )
    assert resp.status_code == 401

    # Viewer role rejected (needs admin or owner)
    viewer_token = setup_tenants["token_viewer_a"]
    resp = client.post(
        "/v2/ingestion/refresh",
        headers={"Authorization": f"Bearer {viewer_token}"},
        json={"csv_payloads": {"requisitions": SAMPLE_REQS_CSV}},
    )
    assert resp.status_code == 403


def test_ingestion_idempotency(client: TestClient, setup_tenants, db_session: Session):
    token = setup_tenants["token_admin_a"]
    payload = {
        "source_system_id": "test_ats",
        "csv_payloads": {
            "requisitions": SAMPLE_REQS_CSV,
            "applications": SAMPLE_APPS_CSV,
        },
    }

    # First run
    resp1 = client.post(
        "/v2/ingestion/refresh",
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
    )
    assert resp1.status_code == 200
    assert resp1.json()["summary"]["inserted"] == 5

    # Second run (exact replay)
    resp2 = client.post(
        "/v2/ingestion/refresh",
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
    )
    assert resp2.status_code == 200
    summary2 = resp2.json()["summary"]
    assert summary2["inserted"] == 0
    assert summary2["unchanged_replays"] == 5

    # Total canonical records in DB remains 5
    tenant_id = setup_tenants["tenant_a"].id
    count = db_session.query(CanonicalRecord).filter(CanonicalRecord.tenant_id == tenant_id).count()
    assert count == 5


def test_cross_tenant_job_isolation(client: TestClient, setup_tenants):
    token_a = setup_tenants["token_admin_a"]
    token_b = setup_tenants["token_admin_b"]

    # Tenant A creates an import job
    resp_a = client.post(
        "/v2/ingestion/refresh",
        headers={"Authorization": f"Bearer {token_a}"},
        json={
            "csv_payloads": {
                "requisitions": SAMPLE_REQS_CSV,
                "applications": SAMPLE_APPS_CSV,
            }
        },
    )
    job_id_a = resp_a.json()["job_id"]

    # Tenant B lists jobs -> should see 0
    resp_b_list = client.get(
        "/v2/ingestion/jobs",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert resp_b_list.status_code == 200
    assert len(resp_b_list.json()) == 0

    # Tenant B tries to get Tenant A's job -> 404
    resp_b_get = client.get(
        f"/v2/ingestion/jobs/{job_id_a}",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert resp_b_get.status_code == 404


def test_semantic_validation_failure_rollback_and_preservation(
    client: TestClient, setup_tenants, db_session: Session, monkeypatch
):
    token = setup_tenants["token_admin_a"]
    tenant_id = setup_tenants["tenant_a"].id

    # 1. Seed successful baseline
    resp1 = client.post(
        "/v2/ingestion/refresh",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "csv_payloads": {
                "requisitions": SAMPLE_REQS_CSV,
                "applications": SAMPLE_APPS_CSV,
            }
        },
    )
    assert resp1.status_code == 200
    assert db_session.query(CanonicalRecord).filter(CanonicalRecord.tenant_id == tenant_id).count() == 5

    # 2. Submit bad batch with an application referencing an unresolvable requisition
    bad_apps_csv = """application_id,requisition_id,source_channel,applied_at,application_outcome,joined_at,source_record_id
APP-999,REQ-NONEXISTENT,LinkedIn,2026-02-01T10:00:00Z,joined,2026-03-01T10:00:00Z,SRC-APP-999
"""
    resp2 = client.post(
        "/v2/ingestion/refresh",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "csv_payloads": {
                "requisitions": SAMPLE_REQS_CSV,
                "applications": bad_apps_csv,
            }
        },
    )
    # Quarantine catches the unresolvable reference
    data2 = resp2.json()
    assert len(data2["quarantine"]) > 0

    # 3. Test that semantic validation failure triggers full transaction rollback
    monkeypatch.setattr(
        "app.ingestion.service.validate_bundle",
        lambda bundle: {"passed": False, "errors": ["forced_test_validation_failure"], "warnings": []},
    )
    resp3 = client.post(
        "/v2/ingestion/refresh",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "csv_payloads": {
                "requisitions": SAMPLE_REQS_CSV,
                "applications": SAMPLE_APPS_CSV,
            }
        },
    )
    data3 = resp3.json()
    assert data3["status"] == "failed_validation"
    assert data3["summary"]["committed"] is False
    assert "forced_test_validation_failure" in data3["semantic_validation"]["errors"]

    # Verify baseline canonical records are untouched (still exactly 5)
    remaining_count = db_session.query(CanonicalRecord).filter(CanonicalRecord.tenant_id == tenant_id).count()
    assert remaining_count == 5


def test_concurrency_lock_rejects_duplicate_active_job(
    client: TestClient, setup_tenants, db_session: Session
):
    token = setup_tenants["token_admin_a"]
    tenant_id = setup_tenants["tenant_a"].id

    # Seed an active running job
    running_job = ImportJob(
        tenant_id=tenant_id,
        status="running",
        source_system_id="manual_lock",
        mapping_version="1.0",
        started_at=datetime.now(timezone.utc),
    )
    db_session.add(running_job)
    db_session.commit()

    # Calling refresh must return 409 Conflict
    resp = client.post(
        "/v2/ingestion/refresh",
        headers={"Authorization": f"Bearer {token}"},
        json={"csv_payloads": {"requisitions": SAMPLE_REQS_CSV}},
    )
    assert resp.status_code == 409
    assert "already in progress" in resp.json()["message"]


def test_unknown_source_generates_warning_not_quarantine(client: TestClient, setup_tenants):
    token = setup_tenants["token_admin_a"]
    resp = client.post(
        "/v2/ingestion/refresh",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "csv_payloads": {
                "requisitions": SAMPLE_REQS_CSV,
                "applications": SAMPLE_APPS_CSV,
            }
        },
    )
    data = resp.json()
    assert any(
        w.get("warning") == "unknown_source_channel" for w in data.get("warnings", [])
    )
    assert data["summary"]["inserted"] == 5
    assert data["summary"]["quarantined"] == 0


def test_csv_multipart_upload_succeeds(client: TestClient, setup_tenants):
    token = setup_tenants["token_admin_a"]
    files = {
        "requisitions": ("requisitions.csv", io.BytesIO(SAMPLE_REQS_CSV.encode("utf-8")), "text/csv"),
        "applications": ("applications.csv", io.BytesIO(SAMPLE_APPS_CSV.encode("utf-8")), "text/csv"),
    }
    resp = client.post(
        "/v2/ingestion/csv",
        headers={"Authorization": f"Bearer {token}"},
        files=files,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["summary"]["inserted"] == 5


def test_stale_running_job_recovery(client: TestClient, setup_tenants, db_session: Session):
    token = setup_tenants["token_admin_a"]
    tenant_id = setup_tenants["tenant_a"].id

    # Create a stale running job from 45 minutes ago
    stale_time = datetime.now(timezone.utc) - timedelta(minutes=45)
    stale_job = ImportJob(
        tenant_id=tenant_id,
        status="running",
        source_system_id="crashed_process",
        mapping_version="1.0",
        started_at=stale_time,
        created_at=stale_time,
    )
    db_session.add(stale_job)
    db_session.commit()
    stale_job_id = stale_job.id

    # Initiating a new import must NOT be permanently blocked; it recovers the stale job
    resp = client.post(
        "/v2/ingestion/refresh",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "csv_payloads": {
                "requisitions": SAMPLE_REQS_CSV,
                "applications": SAMPLE_APPS_CSV,
            }
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] in ("completed", "completed_with_warnings")

    # Stale job should now be marked as failed
    db_session.refresh(stale_job)
    assert stale_job.status == "failed"
    assert "stale_job_timeout" in stale_job.error_summary

    # AuditEvent must be recorded
    audit = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.tenant_id == tenant_id, AuditEvent.event_type == "ingestion.stale_job_recovered")
        .first()
    )
    assert audit is not None
    assert audit.outcome == "failure"
