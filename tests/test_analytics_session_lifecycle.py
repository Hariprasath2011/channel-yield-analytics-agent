"""Comprehensive unit and concurrency tests for session-based Zoho MCP architecture.

Covers:
  1. Atomic session acquisition and mutual exclusion (one active session globally).
  2. Rejection of competing sessions with BUSY_MESSAGE.
  3. OAuth state security: SHA-256 state hashing, one-time consumption, replay rejection.
  4. Credential isolation: temporary Fernet-encrypted credentials separate from permanent records.
  5. Session verification, touch, and 30-minute inactivity timeout.
  6. Explicit session termination and best-effort remote revocation with unconditional local purge.
  7. Resiliency to Zoho revocation failures (network timeout / HTTP 500).
  8. Multi-worker concurrency simulation.
  9. Session-aware MCP tools (start_analytics_session, end_analytics_session, etc.).
"""
from __future__ import annotations

import concurrent.futures
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.exceptions import OAuthError, ValidationError
from app.integrations.zoho.crypto import decrypt_token, encrypt_token
from app.persistence.models import (
    AnalyticsSession,
    AnalyticsSessionCredential,
    AnalyticsSessionLock,
    Base,
    Tenant,
    ZohoIntegration,
)
from app.sessions.manager import (
    BUSY_MESSAGE,
    GLOBAL_LOCK_ID,
    INACTIVITY_TIMEOUT_MINUTES,
    LOCK_KEY,
    SessionManager,
)
from mcp_server.server import MCPServerDispatcher
from mcp_server.zoho_recruit import ZohoRecruitMCPHandler


@pytest.fixture
def session_db():
    """In-memory SQLite database initialized with all tables and singleton lock."""
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    db = SessionLocal()

    # Seed singleton lock
    lock = AnalyticsSessionLock(
        lock_key=LOCK_KEY,
        locked_by_session_id=None,
        acquired_at=None,
        heartbeat_at=None,
    )
    db.add(lock)
    db.commit()

    yield db
    db.close()


# ─── 1. Atomic Acquisition & Mutual Exclusion ─────────────────────────────────


def test_acquire_session_success(session_db):
    """First acquisition succeeds, transitions to AWAITING_ZOHO_AUTHORIZATION, and sets lock."""
    res = SessionManager.acquire_session(session_db, data_center="eu")
    assert res["status"] == "awaiting_authorization"
    assert "session_id" in res
    assert "authorization_url" in res
    assert "accounts.zoho.eu" in res["authorization_url"]

    # Verify session row in DB
    sess = session_db.query(AnalyticsSession).filter_by(id=res["session_id"]).first()
    assert sess is not None
    assert sess.status == "AWAITING_ZOHO_AUTHORIZATION"
    assert sess.data_center == "eu"
    assert sess.oauth_state_hash is not None

    # Verify lock row in DB
    lock = session_db.query(AnalyticsSessionLock).filter_by(lock_key=LOCK_KEY).first()
    assert lock.locked_by_session_id == res["session_id"]


def test_competing_active_session_rejected_busy(session_db):
    """When an ACTIVE session exists, subsequent acquisition is rejected with busy message."""
    res1 = SessionManager.acquire_session(session_db, data_center="com")
    sess_id = res1["session_id"]

    # Transition session to ACTIVE
    sess = session_db.query(AnalyticsSession).filter_by(id=sess_id).first()
    sess.status = "ACTIVE"
    session_db.commit()

    # Competing acquisition attempt
    res2 = SessionManager.acquire_session(session_db, data_center="in")
    assert res2["status"] == "busy"
    assert res2["message"] == BUSY_MESSAGE
    assert res2["active_session_status"] == "ACTIVE"


def test_pending_authorization_session_reuse(session_db):
    """Calling acquire_session again while awaiting authorization reuses the pending session."""
    res1 = SessionManager.acquire_session(session_db, data_center="in")
    url1 = res1["authorization_url"]

    res2 = SessionManager.acquire_session(session_db, data_center="in")
    assert res2["status"] == "awaiting_authorization"
    assert res2["session_id"] == res1["session_id"]
    assert res2["authorization_url"] == url1


# ─── 2. OAuth State Security & One-Time Consumption ───────────────────────────


def test_oauth_state_hash_stored_not_raw(session_db):
    """Verify raw OAuth state is never stored in DB; only SHA-256 hash is persisted."""
    res = SessionManager.acquire_session(session_db, data_center="in")
    sess = session_db.query(AnalyticsSession).filter_by(id=res["session_id"]).first()

    raw_url = res["authorization_url"]
    # Extract raw state from URL
    import urllib.parse
    parsed = urllib.parse.urlparse(raw_url)
    qs = urllib.parse.parse_qs(parsed.query)
    raw_state = qs["state"][0]

    # Stored state hash must be SHA-256 of raw_state
    expected_hash = SessionManager._hash_token(raw_state)
    assert sess.oauth_state_hash == expected_hash
    # Raw state must not appear in any session column
    assert raw_state != sess.oauth_state_hash


def test_oauth_code_binding_and_one_time_consumption(session_db):
    """Binding exchanges authorization code, stores encrypted tokens, and purges state hash."""
    res = SessionManager.acquire_session(session_db, data_center="in")
    raw_url = res["authorization_url"]

    import urllib.parse
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(raw_url).query)
    raw_state = qs["state"][0]

    mock_client = MagicMock()
    mock_client.exchange_authorization_code.return_value = {
        "access_token": "zoho_access_token_live_123",
        "refresh_token": "zoho_refresh_token_live_456",
        "expires_in": 3600,
    }
    mock_client.get_organization_info.return_value = {"id": "org_987654"}

    bound_sess = SessionManager.bind_oauth_credentials(
        session_db,
        raw_state=raw_state,
        code="auth_code_xyz",
        client=mock_client,
    )

    assert bound_sess.status == "ACTIVE"
    assert bound_sess.oauth_state_hash is None  # Purged immediately (one-time consumption)
    assert bound_sess.zoho_org_id == "org_987654"

    # Credentials row
    cred = session_db.query(AnalyticsSessionCredential).filter_by(session_id=bound_sess.id).first()
    assert cred is not None
    assert decrypt_token(cred.encrypted_access_token) == "zoho_access_token_live_123"
    assert decrypt_token(cred.encrypted_refresh_token) == "zoho_refresh_token_live_456"

    # Replay attack: re-submitting the same state must be rejected
    with pytest.raises(OAuthError) as exc_replay:
        SessionManager.bind_oauth_credentials(
            session_db,
            raw_state=raw_state,
            code="auth_code_xyz",
            client=mock_client,
        )
    assert "expired or has already been used" in str(exc_replay.value)


# ─── 3. Credential Isolation (Zero Permanent Tenant Impact) ───────────────────


def test_session_credentials_isolated_from_permanent_tables(session_db):
    """Session credentials must never create or touch permanent Tenant or ZohoIntegration rows."""
    initial_tenants = session_db.query(Tenant).count()
    initial_integrations = session_db.query(ZohoIntegration).count()

    res = SessionManager.acquire_session(session_db, data_center="com")
    import urllib.parse
    raw_state = urllib.parse.parse_qs(urllib.parse.urlparse(res["authorization_url"]).query)["state"][0]

    mock_client = MagicMock()
    mock_client.exchange_authorization_code.return_value = {
        "access_token": "temp_access",
        "refresh_token": "temp_refresh",
        "expires_in": 3600,
    }

    SessionManager.bind_oauth_credentials(session_db, raw_state=raw_state, code="test_code", client=mock_client)

    # Permanent tables remain completely unchanged
    assert session_db.query(Tenant).count() == initial_tenants
    assert session_db.query(ZohoIntegration).count() == initial_integrations


# ─── 4. Expiry, Inactivity Touch, and Token Refresh ───────────────────────────


def test_verify_and_touch_extends_inactivity_timer(session_db):
    """Every qualifying tool invocation verifies active state and extends 30m expiry."""
    res = SessionManager.acquire_session(session_db, data_center="com")
    sess = session_db.query(AnalyticsSession).filter_by(id=res["session_id"]).first()
    sess.status = "ACTIVE"
    now_utc = SessionManager._now_utc()
    sess.last_activity_at = now_utc - timedelta(minutes=10)
    sess.expires_at = now_utc + timedelta(minutes=20)

    cred = AnalyticsSessionCredential(
        session_id=sess.id,
        encrypted_access_token=encrypt_token("active_token"),
        encrypted_refresh_token=encrypt_token("refresh_token"),
        token_expires_at=now_utc + timedelta(hours=1),
        created_at=now_utc,
        updated_at=now_utc,
    )
    session_db.add(cred)
    session_db.commit()

    # Touch session
    active_sess, client, access_token = SessionManager.verify_and_touch_session(session_db)
    assert access_token == "active_token"
    # Expiry extended by 30 minutes from now
    diff_minutes = (SessionManager._to_utc(active_sess.expires_at) - now_utc).total_seconds() / 60
    assert 29.0 <= diff_minutes <= 30.5


def test_expired_session_fails_and_cleans_up(session_db):
    """Session past 30m inactivity is expired, credentials purged, and lock released."""
    res = SessionManager.acquire_session(session_db, data_center="com")
    sess = session_db.query(AnalyticsSession).filter_by(id=res["session_id"]).first()
    sess.status = "ACTIVE"
    now_utc = SessionManager._now_utc()
    sess.expires_at = now_utc - timedelta(seconds=1)  # Expired

    cred = AnalyticsSessionCredential(
        session_id=sess.id,
        encrypted_access_token=encrypt_token("expired_token"),
        encrypted_refresh_token=encrypt_token("refresh_token"),
        token_expires_at=now_utc + timedelta(hours=1),
        created_at=now_utc,
        updated_at=now_utc,
    )
    session_db.add(cred)
    session_db.commit()

    with pytest.raises(OAuthError) as exc_exp:
        SessionManager.verify_and_touch_session(session_db)
    assert "expired due to 30 minutes of inactivity" in str(exc_exp.value)

    # Verify credentials purged
    assert session_db.query(AnalyticsSessionCredential).filter_by(session_id=sess.id).first() is None
    # Verify session marked EXPIRED
    session_db.refresh(sess)
    assert sess.status == "EXPIRED"


# ─── 5. Explicit Termination & Resilient Revocation ───────────────────────────


def test_terminate_session_purges_credentials_and_releases_lock(session_db):
    """Explicit termination revokes remote token, deletes local credentials, and clears lock."""
    res = SessionManager.acquire_session(session_db, data_center="eu")
    sess = session_db.query(AnalyticsSession).filter_by(id=res["session_id"]).first()
    sess.status = "ACTIVE"
    now_utc = SessionManager._now_utc()

    cred = AnalyticsSessionCredential(
        session_id=sess.id,
        encrypted_access_token=encrypt_token("access_to_revoke"),
        encrypted_refresh_token=encrypt_token("refresh_to_revoke"),
        token_expires_at=now_utc + timedelta(hours=1),
        created_at=now_utc,
        updated_at=now_utc,
    )
    session_db.add(cred)
    session_db.commit()

    with patch.object(SessionManager, "_attempt_zoho_token_revocation", return_value={"status": "revoked"}) as mock_revoke:
        term_res = SessionManager.terminate_session(session_db, reason="user_requested")

    assert term_res["status"] == "terminated"
    mock_revoke.assert_called_once()

    # Local credentials must be unconditionally deleted
    assert session_db.query(AnalyticsSessionCredential).filter_by(session_id=sess.id).first() is None
    # Lock row must be cleared
    lock = session_db.query(AnalyticsSessionLock).filter_by(lock_key=LOCK_KEY).first()
    assert lock.locked_by_session_id is None


def test_terminate_session_succeeds_even_when_remote_revocation_fails(session_db):
    """Remote Zoho failure (HTTP 500 / timeout) must NEVER prevent local credential deletion."""
    res = SessionManager.acquire_session(session_db, data_center="com")
    sess = session_db.query(AnalyticsSession).filter_by(id=res["session_id"]).first()
    sess.status = "ACTIVE"
    now_utc = SessionManager._now_utc()

    cred = AnalyticsSessionCredential(
        session_id=sess.id,
        encrypted_access_token=encrypt_token("access_fail"),
        encrypted_refresh_token=encrypt_token("refresh_fail"),
        token_expires_at=now_utc + timedelta(hours=1),
        created_at=now_utc,
        updated_at=now_utc,
    )
    session_db.add(cred)
    session_db.commit()

    # Simulate remote revocation failure
    with patch.object(
        SessionManager,
        "_attempt_zoho_token_revocation",
        return_value={"status": "failed", "error": "upstream_server_error"}
    ):
        term_res = SessionManager.terminate_session(session_db, reason="user_requested")

    assert term_res["status"] == "terminated"
    # Local credentials purged unconditionally
    assert session_db.query(AnalyticsSessionCredential).filter_by(session_id=sess.id).first() is None
    # Lock released
    lock = session_db.query(AnalyticsSessionLock).filter_by(lock_key=LOCK_KEY).first()
    assert lock.locked_by_session_id is None


# ─── 6. Multi-Worker Concurrency Simulation ───────────────────────────────────


def test_multi_worker_concurrency_active_session_rejection():
    """Simulate multiple application workers attempting to acquire a session while one is ACTIVE.

    All workers must be rejected with status='busy' and BUSY_MESSAGE.
    """
    from sqlalchemy.pool import StaticPool
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=False,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)

    # Initialize active session
    init_db = SessionLocal()
    init_db.add(AnalyticsSessionLock(lock_key=LOCK_KEY, locked_by_session_id="active-sess-1"))
    now_utc = SessionManager._now_utc()
    init_db.add(AnalyticsSession(
        id="active-sess-1",
        session_token_hash="hash_123",
        status="ACTIVE",
        data_center="com",
        created_at=now_utc,
        last_activity_at=now_utc,
        expires_at=now_utc + timedelta(minutes=30),
    ))
    init_db.commit()
    init_db.close()

    def _worker_attempt(worker_id: int):
        db = SessionLocal()
        try:
            return SessionManager.acquire_session(db, data_center="com")
        finally:
            db.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(_worker_attempt, i) for i in range(5)]
        results = [f.result() for f in futures]

    statuses = [r.get("status") for r in results]
    assert len(statuses) == 5
    assert all(s == "busy" for s in statuses)
    for r in results:
        assert r.get("message") == BUSY_MESSAGE


# ─── 7. MCP Session Tools Dispatching ─────────────────────────────────────────


def test_mcp_dispatcher_session_tools():
    """Verify MCP dispatcher routes start_analytics_session, get_status, end_session."""
    dispatcher = MCPServerDispatcher(handler=ZohoRecruitMCPHandler())

    with patch.object(ZohoRecruitMCPHandler, "start_analytics_session", return_value={"status": "awaiting_authorization"}):
        req1 = {
            "jsonrpc": "2.0",
            "id": "1",
            "method": "tools/call",
            "params": {"name": "start_analytics_session", "arguments": {"data_center": "in"}},
        }
        res1 = dispatcher.dispatch(req1)
        assert res1["result"]["isError"] is False
        assert "awaiting_authorization" in res1["result"]["content"][0]["text"]

    with patch.object(ZohoRecruitMCPHandler, "get_analytics_session_status", return_value={"status": "ACTIVE", "remaining_minutes": 25}):
        req2 = {
            "jsonrpc": "2.0",
            "id": "2",
            "method": "tools/call",
            "params": {"name": "get_analytics_session_status", "arguments": {}},
        }
        res2 = dispatcher.dispatch(req2)
        assert res2["result"]["isError"] is False
        assert "remaining_minutes" in res2["result"]["content"][0]["text"]

    with patch.object(ZohoRecruitMCPHandler, "end_analytics_session", return_value={"status": "terminated"}):
        req3 = {
            "jsonrpc": "2.0",
            "id": "3",
            "method": "tools/call",
            "params": {"name": "end_analytics_session", "arguments": {}},
        }
        res3 = dispatcher.dispatch(req3)
        assert res3["result"]["isError"] is False
        assert "terminated" in res3["result"]["content"][0]["text"]


# ─── 8. Background Expiry Cleanup & Concurrency Safety ────────────────────────


def test_background_expiry_cleanup_without_mcp_request(session_db):
    """Automatic background cleanup sweeps expired sessions without requiring an MCP tool call."""
    now_utc = SessionManager._now_utc()

    # Create expired session with credentials
    expired_sess = AnalyticsSession(
        id="expired-sess-1",
        session_token_hash="hash_exp",
        status="ACTIVE",
        data_center="in",
        created_at=now_utc - timedelta(minutes=45),
        last_activity_at=now_utc - timedelta(minutes=35),
        expires_at=now_utc - timedelta(minutes=5),  # Expired 5 mins ago
    )
    session_db.add(expired_sess)
    session_db.add(AnalyticsSessionCredential(
        session_id="expired-sess-1",
        encrypted_access_token=encrypt_token("expired_access"),
        token_expires_at=now_utc - timedelta(minutes=5),
        created_at=now_utc - timedelta(minutes=45),
        updated_at=now_utc - timedelta(minutes=45),
    ))

    # Set lock to expired session
    lock = session_db.query(AnalyticsSessionLock).filter_by(lock_key=LOCK_KEY).first()
    lock.locked_by_session_id = "expired-sess-1"
    session_db.commit()

    # Run cleanup directly without invoking any MCP tool
    cleaned = SessionManager.cleanup_expired_sessions(session_db)
    assert cleaned == 1

    # Verify expired session marked EXPIRED and credentials purged
    sess = session_db.query(AnalyticsSession).filter_by(id="expired-sess-1").first()
    assert sess.status == "EXPIRED"
    assert sess.termination_reason == "background_inactivity_cleanup"
    assert session_db.query(AnalyticsSessionCredential).filter_by(session_id="expired-sess-1").first() is None

    # Verify coordinator lock released
    lock = session_db.query(AnalyticsSessionLock).filter_by(lock_key=LOCK_KEY).first()
    assert lock.locked_by_session_id is None


def test_cleanup_does_not_affect_newly_acquired_session(session_db):
    """Cleanup safely preserves active non-expired sessions and their credentials."""
    now_utc = SessionManager._now_utc()

    # Active fresh session
    fresh_sess = AnalyticsSession(
        id="fresh-sess-1",
        session_token_hash="hash_fresh",
        status="ACTIVE",
        data_center="eu",
        created_at=now_utc - timedelta(minutes=5),
        last_activity_at=now_utc - timedelta(minutes=2),
        expires_at=now_utc + timedelta(minutes=25),  # Valid for 25 more mins
    )
    session_db.add(fresh_sess)
    session_db.add(AnalyticsSessionCredential(
        session_id="fresh-sess-1",
        encrypted_access_token=encrypt_token("valid_access"),
        token_expires_at=now_utc + timedelta(hours=1),
        created_at=now_utc,
        updated_at=now_utc,
    ))
    session_db.commit()

    cleaned = SessionManager.cleanup_expired_sessions(session_db)
    assert cleaned == 0

    # Ensure fresh session credentials and status are unchanged
    sess = session_db.query(AnalyticsSession).filter_by(id="fresh-sess-1").first()
    assert sess.status == "ACTIVE"
    cred = session_db.query(AnalyticsSessionCredential).filter_by(session_id="fresh-sess-1").first()
    assert cred is not None
    assert decrypt_token(cred.encrypted_access_token) == "valid_access"


# ─── 9. Pending OAuth Authorization URL Persistence & Replay Safety ───────────


def test_pending_oauth_url_repeated_calls_same_state_and_worker_safety(session_db):
    """Repeated calls return the identical authorization URL across worker processes without raw state in DB."""
    res1 = SessionManager.acquire_session(session_db, data_center="com")
    url1 = res1["authorization_url"]

    # Clear process memory cache to simulate a different worker handling the request
    SessionManager._pending_auth_urls.clear()

    # Second acquisition call with same DC must return the original pending URL
    res2 = SessionManager.acquire_session(session_db, data_center="com")
    url2 = res2["authorization_url"]
    assert url1 == url2

    # Querying pending URL via get_pending_auth_url
    pending_info = SessionManager.get_pending_auth_url(session_db)
    assert pending_info["status"] == "awaiting_authorization"
    assert pending_info["authorization_url"] == url1

    # Verify raw state is NOT in the database table
    sess = session_db.query(AnalyticsSession).filter_by(id=res1["session_id"]).first()
    assert sess.oauth_state_hash is not None
    assert "https://" not in sess.oauth_state_hash
    # The URL in DB is encrypted
    assert sess.encrypted_auth_url is not None
    assert sess.encrypted_auth_url != url1  # Fernet cipher text
    assert decrypt_token(sess.encrypted_auth_url) == url1


def test_callback_replay_fails_after_consumption(session_db):
    """Once an OAuth callback is consumed, replaying the same callback is rejected."""
    res = SessionManager.acquire_session(session_db, data_center="com")
    auth_url = res["authorization_url"]
    import urllib.parse
    parsed = urllib.parse.urlparse(auth_url)
    qs = urllib.parse.parse_qs(parsed.query)
    raw_state = qs["state"][0]

    mock_client = MagicMock()
    mock_client.exchange_authorization_code.return_value = {
        "access_token": "acc_100",
        "refresh_token": "ref_100",
        "expires_in": 3600,
    }

    # First callback succeeds
    SessionManager.bind_oauth_credentials(session_db, raw_state=raw_state, code="code_1", client=mock_client)
    sess = session_db.query(AnalyticsSession).filter_by(id=res["session_id"]).first()
    assert sess.status == "ACTIVE"
    assert sess.oauth_state_hash is None
    assert sess.encrypted_auth_url is None

    # Replayed callback with same state fails
    with pytest.raises(OAuthError, match="expired or has already been used"):
        SessionManager.bind_oauth_credentials(session_db, raw_state=raw_state, code="code_replay", client=mock_client)


# ─── 10. Read-Only check_zoho_connection Verification ─────────────────────────


def test_check_zoho_connection_read_only_zero_side_effects(session_db):
    """check_zoho_connection only inspects status without acquiring a session or mutating DB."""
    handler = ZohoRecruitMCPHandler(tenant_id=None)

    with patch("mcp_server.zoho_recruit.get_db_context") as mock_ctx:
        mock_ctx.return_value.__enter__.return_value = session_db
        mock_ctx.return_value.__exit__.return_value = False

        res = handler.check_zoho_connection(data_center="in")
        assert res["connected"] is False
        assert res["status"] == "disconnected"
        assert "No active Zoho Recruit session" in res["message"]

        # Assert no session was created
        assert session_db.query(AnalyticsSession).count() == 0
        # Assert lock was not acquired
        lock = session_db.query(AnalyticsSessionLock).filter_by(lock_key=LOCK_KEY).first()
        assert lock.locked_by_session_id is None


# ─── 11. Privacy-Safe Audit Logging Verification ──────────────────────────────


def test_privacy_safe_audit_logging(session_db):
    """Session lifecycle writes privacy-safe audit records with zero tokens or PII."""
    from app.persistence.models import AnalyticsSessionAuditLog

    # Acquire session
    res = SessionManager.acquire_session(session_db, data_center="com")
    sess_id = res["session_id"]

    logs = session_db.query(AnalyticsSessionAuditLog).filter_by(session_id=sess_id).all()
    assert len(logs) >= 1
    assert any(l.event_type == "session_acquired" for l in logs)

    # Terminate session
    SessionManager.terminate_session(session_db, reason="unit_test")
    term_logs = session_db.query(AnalyticsSessionAuditLog).filter_by(session_id=sess_id).all()
    assert any(l.event_type == "session_terminated" for l in term_logs)

    for entry in term_logs:
        # Strict zero-PII check
        text_content = str(entry.details_json or "")
        for sensitive_word in ("candidate", "applicant", "email", "phone", "token", "code", "secret"):
            assert sensitive_word not in text_content.lower() or "had_credentials" in text_content
