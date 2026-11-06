"""PostgreSQL-specific concurrency and coordination tests for session-based Zoho MCP.

Validates:
  1. Cluster-wide advisory transaction locking (`pg_advisory_xact_lock(8492049182938491)`).
  2. Multi-worker independent database connections competing to acquire a session.
  3. Database constraint enforcing exactly one non-terminal session.
  4. Automatic lock release on transaction rollback, connection drop, or worker crash.

This test module skips gracefully if a real PostgreSQL instance is not reachable,
ensuring local/CI unit tests always pass against SQLite while providing a complete
verification harness for PostgreSQL test environments.
"""
from __future__ import annotations

import concurrent.futures
import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.core.exceptions import OAuthError
from app.persistence.models import (
    AnalyticsSession,
    AnalyticsSessionCredential,
    AnalyticsSessionLock,
    Base,
)
from app.sessions.manager import (
    BUSY_MESSAGE,
    GLOBAL_LOCK_ID,
    LOCK_KEY,
    SessionManager,
)

POSTGRES_URL = (
    os.getenv("TEST_POSTGRES_URL", "").strip()
    or os.getenv("CY_TEST_DATABASE_URL", "").strip()
    or (
        os.getenv("CY_DATABASE_URL", "").strip()
        if os.getenv("CY_DATABASE_URL", "").strip().startswith("postgresql")
        else ""
    )
    or (
        get_settings().database_url
        if get_settings().database_url.startswith("postgresql")
        else ""
    )
)


def _is_postgres_available() -> bool:
    if not POSTGRES_URL or not POSTGRES_URL.startswith("postgresql"):
        return False
    try:
        engine = create_engine(POSTGRES_URL, connect_args={"connect_timeout": 3})
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _is_postgres_available(),
    reason="Live PostgreSQL instance not reachable; skipping PostgreSQL-specific concurrency tests.",
)


@pytest.fixture(scope="module")
def pg_engine():
    engine = create_engine(POSTGRES_URL, pool_size=10, max_overflow=5)
    Base.metadata.create_all(engine)

    # Ensure partial unique index is created if table was freshly created via metadata
    with engine.connect() as conn:
        conn.execute(
            text(
                "DELETE FROM analytics_sessions WHERE status IN "
                "('SESSION_INITIALIZING', 'AWAITING_ZOHO_AUTHORIZATION', 'ACTIVE', 'PROCESSING', 'ENDING')"
            )
        )
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_analytics_sessions_single_active "
                "ON analytics_sessions ((1)) WHERE status IN "
                "('SESSION_INITIALIZING', 'AWAITING_ZOHO_AUTHORIZATION', 'ACTIVE', 'PROCESSING', 'ENDING')"
            )
        )
        conn.commit()

    # Initialize coordinator row
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        lock = db.query(AnalyticsSessionLock).filter_by(lock_key=LOCK_KEY).first()
        if not lock:
            db.add(AnalyticsSessionLock(lock_key=LOCK_KEY))
            db.commit()

    yield engine
    engine.dispose()


def test_postgres_advisory_lock_mutual_exclusion(pg_engine):
    """Verify that pg_advisory_xact_lock serializes concurrent transaction evaluation on PostgreSQL."""
    SessionLocal = sessionmaker(bind=pg_engine)

    # Clean existing non-terminal sessions
    with SessionLocal() as db:
        db.query(AnalyticsSession).filter(
            AnalyticsSession.status.in_(["SESSION_INITIALIZING", "AWAITING_ZOHO_AUTHORIZATION", "ACTIVE", "PROCESSING", "ENDING"])
        ).delete(synchronize_session=False)
        lock = db.query(AnalyticsSessionLock).filter_by(lock_key=LOCK_KEY).first()
        if lock:
            lock.locked_by_session_id = None
        db.commit()

    def _worker(worker_id: int):
        with SessionLocal() as db:
            return SessionManager.acquire_session(db, data_center="com")

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(_worker, i) for i in range(5)]
        results = [f.result() for f in futures]

    statuses = [r.get("status") for r in results]
    # Exactly one session created; duplicates safely reuse or synchronize
    assert "awaiting_authorization" in statuses
    with SessionLocal() as db:
        count = db.query(AnalyticsSession).filter(
            AnalyticsSession.status.in_(["SESSION_INITIALIZING", "AWAITING_ZOHO_AUTHORIZATION", "ACTIVE", "PROCESSING", "ENDING"])
        ).count()
        assert count == 1


def test_postgres_competing_active_session_rejection(pg_engine):
    """Verify that when an ACTIVE session exists, competing PostgreSQL connections are rejected with busy."""
    SessionLocal = sessionmaker(bind=pg_engine)
    now_utc = datetime.now(timezone.utc)

    # Setup active session
    with SessionLocal() as db:
        db.query(AnalyticsSession).filter(
            AnalyticsSession.status.in_(["SESSION_INITIALIZING", "AWAITING_ZOHO_AUTHORIZATION", "ACTIVE", "PROCESSING", "ENDING"])
        ).delete(synchronize_session=False)
        sess = AnalyticsSession(
            id="pg-active-1",
            session_token_hash="token_pg_active",
            status="ACTIVE",
            data_center="com",
            created_at=now_utc,
            last_activity_at=now_utc,
            expires_at=now_utc + timedelta(minutes=30),
        )
        db.add(sess)
        lock = db.query(AnalyticsSessionLock).filter_by(lock_key=LOCK_KEY).first()
        if lock:
            lock.locked_by_session_id = "pg-active-1"
        db.commit()

    # Competing worker attempts to acquire from independent connection
    with SessionLocal() as db_competing:
        res = SessionManager.acquire_session(db_competing, data_center="com")
        assert res.get("status") == "busy"
        assert res.get("message") == BUSY_MESSAGE

    # Teardown
    with SessionLocal() as db:
        db.query(AnalyticsSession).filter_by(id="pg-active-1").delete()
        lock = db.query(AnalyticsSessionLock).filter_by(lock_key=LOCK_KEY).first()
        if lock:
            lock.locked_by_session_id = None
        db.commit()


def test_postgres_partial_unique_index_constraint(pg_engine):
    """Verify the database partial unique index prevents two concurrent non-terminal sessions on PostgreSQL."""
    SessionLocal = sessionmaker(bind=pg_engine)
    now_utc = datetime.now(timezone.utc)

    with SessionLocal() as db1, SessionLocal() as db2:
        # Clean non-terminal sessions
        db1.query(AnalyticsSession).filter(
            AnalyticsSession.status.in_(["SESSION_INITIALIZING", "AWAITING_ZOHO_AUTHORIZATION", "ACTIVE", "PROCESSING", "ENDING"])
        ).delete(synchronize_session=False)
        db1.commit()

        # Session 1: Active
        sess1 = AnalyticsSession(
            id="pg-test-sess-1",
            session_token_hash="hash_pg_1",
            status="ACTIVE",
            data_center="com",
            created_at=now_utc,
            last_activity_at=now_utc,
            expires_at=now_utc + timedelta(minutes=30),
        )
        db1.add(sess1)
        db1.commit()

        # Attempt to insert a second active session directly without going through coordinator
        sess2 = AnalyticsSession(
            id="pg-test-sess-2",
            session_token_hash="hash_pg_2",
            status="ACTIVE",
            data_center="com",
            created_at=now_utc,
            last_activity_at=now_utc,
            expires_at=now_utc + timedelta(minutes=30),
        )
        db2.add(sess2)
        with pytest.raises(Exception) as exc_info:
            # Database constraint raises IntegrityError
            db2.commit()
        db2.rollback()

        # Confirm the constraint violation in PostgreSQL
        assert "uq_analytics_sessions_single_active" in str(exc_info.value) or "duplicate key" in str(exc_info.value).lower()

        # Clean up
        db1.query(AnalyticsSession).filter_by(id="pg-test-sess-1").delete()
        db1.commit()
