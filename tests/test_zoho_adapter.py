"""Tests for Zoho Recruit OAuth adapter, encryption, mapper, pagination, and sync lifecycle."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Generator
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth.passwords import hash_password
from app.auth.tokens import create_access_token
from app.core.exceptions import ConflictError, OAuthError, RateLimitError
from app.integrations.zoho.client import DATACENTER_DOMAINS, ZohoRecruitClient
from app.integrations.zoho.crypto import decrypt_token, encrypt_token
from app.integrations.zoho.mapper import map_zoho_application, map_zoho_job_opening
from app.integrations.zoho.service import ZohoService
from app.main import create_app
from app.persistence.database import Base, get_db
from app.persistence.models import (
    AuditEvent,
    CanonicalRecord,
    Membership,
    OAuthState,
    SyncJob,
    Tenant,
    User,
    ZohoIntegration,
)


class MockZohoHttpClient:
    """Mock HTTP client simulating official Zoho Recruit REST API responses."""

    def __init__(self):
        self.call_history = []

    def post(self, url: str, data: dict):
        self.call_history.append(("POST", url, data))
        if "oauth/v2/token" in url:
            if data.get("code") == "invalid_code":
                return MockResponse(400, {"error": "invalid_code"})
            if data.get("grant_type") == "authorization_code":
                return MockResponse(
                    200,
                    {
                        "access_token": "zoho_access_token_123",
                        "refresh_token": "zoho_refresh_token_456",
                        "expires_in": 3600,
                        "api_domain": "https://recruit.zoho.com",
                        "scope": "ZohoRecruit.modules.ALL",
                    },
                )
            if data.get("grant_type") == "refresh_token":
                return MockResponse(
                    200,
                    {
                        "access_token": "new_refreshed_access_token_789",
                        "expires_in": 3600,
                    },
                )
        return MockResponse(404, {"error": "not_found"})

    def get(self, url: str, headers: dict, params: dict):
        self.call_history.append(("GET", url, params))
        if "429_trigger" in url:
            return MockResponse(429, {"error": "rate_limit_exceeded"}, headers={"Retry-After": "30"})

        if "JobOpenings" in url:
            return MockResponse(
                200,
                {
                    "data": [
                        {
                            "id": "Z-REQ-1",
                            "Job_Opening_ID": "REQ-101",
                            "Posting_Title": "Staff Platform Engineer",
                            "Job_Opening_Status": "In-progress",
                            "Department": "Engineering",
                        }
                    ],
                    "info": {"page": params.get("page", 1), "more_records": False},
                },
            )
        if "Candidates" in url:
            return MockResponse(
                200,
                {
                    "data": [
                        {"id": "Z-CAND-1", "Lead_Source": "Employee Referral"},
                        {"id": "Z-CAND-2", "Source": "LinkedIn"},
                    ],
                    "info": {"page": params.get("page", 1), "more_records": False},
                },
            )
        if "Applications" in url:
            page = params.get("page", 1)
            if page == 1:
                return MockResponse(
                    200,
                    {
                        "data": [
                            {
                                "id": "Z-APP-1",
                                "Candidate_ID": {"id": "Z-CAND-1"},
                                "Job_Opening_ID": {"id": "REQ-101"},
                                "Application_Status": "Hired",
                                "Created_Time": "2026-01-15T09:00:00+00:00",
                                "Joining_Date": "2026-02-15T09:00:00+00:00",
                            }
                        ],
                        "info": {"page": 1, "more_records": True},
                    },
                )
            else:
                return MockResponse(
                    200,
                    {
                        "data": [
                            {
                                "id": "Z-APP-2",
                                "Candidate_ID": {"id": "Z-CAND-2"},
                                "Job_Opening_ID": {"id": "REQ-101"},
                                "Application_Status": "Rejected",
                                "Created_Time": "2026-01-20T11:00:00+00:00",
                            }
                        ],
                        "info": {"page": 2, "more_records": False},
                    },
                )
        return MockResponse(404, {"error": "module_not_found"})


class MockResponse:
    def __init__(self, status_code: int, json_data: dict, headers: dict | None = None):
        self.status_code = status_code
        self._json_data = json_data
        self.headers = headers or {}

    def json(self):
        return self._json_data


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
def setup_data(db_session: Session):
    tenant = Tenant(slug="zoho-tenant", name="Zoho Tenant Ltd")
    db_session.add(tenant)
    db_session.flush()

    admin = User(
        tenant_id=tenant.id,
        email="admin@zoho.com",
        full_name="Zoho Admin",
        hashed_password=hash_password("admin_pass"),
    )
    viewer = User(
        tenant_id=tenant.id,
        email="viewer@zoho.com",
        full_name="Zoho Viewer",
        hashed_password=hash_password("viewer_pass"),
    )
    db_session.add_all([admin, viewer])
    db_session.flush()

    db_session.add_all([
        Membership(tenant_id=tenant.id, user_id=admin.id, role="admin"),
        Membership(tenant_id=tenant.id, user_id=viewer.id, role="viewer"),
    ])
    db_session.commit()

    return {
        "tenant": tenant,
        "admin_token": create_access_token(user_id=admin.id, tenant_id=tenant.id, role="admin"),
        "viewer_token": create_access_token(user_id=viewer.id, tenant_id=tenant.id, role="viewer"),
    }


def test_datacenter_domains_and_authorization_url():
    for dc, domains in DATACENTER_DOMAINS.items():
        client = ZohoRecruitClient(
            client_id="test_client_id",
            client_secret="test_client_secret",
            redirect_uri="https://app.test/callback",
            data_center=dc,
        )
        assert client.accounts_url == domains["accounts"]
        assert client.api_url == domains["api"]

        auth_url = client.get_authorization_url(state="csrf_state_123")
        assert "response_type=code" in auth_url
        assert "state=csrf_state_123" in auth_url
        assert domains["accounts"] in auth_url


def test_fernet_token_encryption_and_decryption():
    plain = "sensitive_zoho_oauth_access_token_xyz"
    encrypted = encrypt_token(plain)
    assert encrypted != plain
    assert "sensitive" not in encrypted

    decrypted = decrypt_token(encrypted)
    assert decrypted == plain

    with pytest.raises(OAuthError):
        decrypt_token("invalid_corrupt_cipher_text")

def test_production_missing_encryption_key_fails_closed(monkeypatch):
    from types import SimpleNamespace
    from app.integrations.zoho import crypto

    production_settings = SimpleNamespace(
        is_production=True,
        encryption_key="",
        secret_key="a" * 64,
    )
    monkeypatch.setattr(
        crypto,
        "get_settings",
        lambda: production_settings,
    )

    with pytest.raises(OAuthError, match="Encryption key is required"):
        crypto._get_fernet()


def test_mocked_token_exchange_and_sync(setup_data, db_session: Session):
    tenant = setup_data["tenant"]
    mock_http = MockZohoHttpClient()
    client = ZohoRecruitClient(
        client_id="cid",
        client_secret="sec",
        redirect_uri="https://cb.test",
        data_center="com",
        http_client=mock_http,
    )

    # 1. State creation
    state = ZohoService.create_oauth_state(db_session, tenant.id)
    assert len(state) >= 32

    # 2. Token exchange and saving
    integration = ZohoService.exchange_and_save_tokens(
        db_session,
        tenant_id=tenant.id,
        state_token=state,
        code="valid_auth_code",
        client=client,
    )
    assert integration.status == "active"
    assert integration.encrypted_access_token is not None
    assert integration.encrypted_access_token != "zoho_access_token_123"
    assert decrypt_token(integration.encrypted_access_token) == "zoho_access_token_123"

    # 3. Synchronize data (multi-page applications + requisitions + candidates)
    sync_job = ZohoService.execute_sync(
        db_session,
        tenant_id=tenant.id,
        client=client,
    )
    assert sync_job.status == "completed"
    assert sync_job.records_fetched == 5  # 1 req + 2 cand + 2 app = 5
    assert sync_job.records_upserted >= 2

    # Verify canonical records were written and properly scoped
    records = db_session.query(CanonicalRecord).filter(CanonicalRecord.tenant_id == tenant.id).all()
    assert len(records) >= 2
    types = {r.entity_type for r in records}
    assert "requisitions" in types
    assert "applications" in types


def test_zoho_field_mapper():
    run_id = "test-run-1"
    req_mapped = map_zoho_job_opening(
        {
            "id": "123",
            "Job_Opening_ID": "REQ-77",
            "Posting_Title": "Senior Site Reliability Engineer",
            "Job_Opening_Status": "Open",
            "Department": "Infrastructure",
        },
        tenant_id="tenant-1",
        run_id=run_id,
    )
    assert req_mapped["requisition_id"] == "REQ-77"
    assert req_mapped["job_title"] == "Senior Site Reliability Engineer"
    assert req_mapped["status"] == "open"
    assert req_mapped["client_id"] == "tenant-1"

    app_mapped = map_zoho_application(
        {
            "id": "APP-55",
            "Candidate_ID": {"id": "CAND-99"},
            "Job_Opening_ID": {"id": "REQ-77"},
            "Application_Status": "Joined",
            "Created_Time": "2026-02-01T10:00:00Z",
            "Joining_Date": "2026-03-01T10:00:00Z",
        },
        candidate_source_map={"CAND-99": "LinkedIn"},
        tenant_id="tenant-1",
        run_id=run_id,
    )
    assert app_mapped["application_id"] == "APP-55"
    assert app_mapped["source_channel"] == "LinkedIn"
    assert app_mapped["application_outcome"] == "joined"
    assert app_mapped["joined_at"] is not None


def test_zoho_rate_limiting_error():
    mock_http = MockZohoHttpClient()
    client = ZohoRecruitClient(
        client_id="cid",
        client_secret="sec",
        redirect_uri="https://cb.test",
        data_center="com",
        http_client=mock_http,
    )
    with pytest.raises(RateLimitError, match="rate limit"):
        client.get_module_records("429_trigger", "token")


def test_zoho_endpoints_rbac_and_status(setup_data, db_session: Session):
    app = create_app()

    def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    with TestClient(app) as test_client:
        viewer_token = setup_data["viewer_token"]
        admin_token = setup_data["admin_token"]

        # Viewer can read status
        resp_status = test_client.get(
            "/v2/integrations/zoho/status",
            headers={"Authorization": f"Bearer {viewer_token}"},
        )
        assert resp_status.status_code == 200
        assert resp_status.json()["status"] == "pending_auth"

        # Viewer cannot initiate authorization (needs admin)
        resp_auth = test_client.post(
            "/v2/integrations/zoho/authorize",
            headers={"Authorization": f"Bearer {viewer_token}"},
        )
        assert resp_auth.status_code == 403

        # Admin can initiate authorization with default data center (com)
        resp_auth_admin = test_client.post(
            "/v2/integrations/zoho/authorize",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp_auth_admin.status_code == 200
        assert "authorization_url" in resp_auth_admin.json()

        # Admin can initiate authorization with valid regional data center (eu)
        resp_auth_eu = test_client.post(
            "/v2/integrations/zoho/authorize?data_center=eu",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp_auth_eu.status_code == 200
        eu_body = resp_auth_eu.json()
        assert "accounts.zoho.eu" in eu_body["authorization_url"]
        assert eu_body["state"].startswith("eu_")

        # Admin rejected with 400 when invalid data center is provided
        resp_auth_bad = test_client.post(
            "/v2/integrations/zoho/authorize?data_center=invalid_xyz",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp_auth_bad.status_code == 400
        assert resp_auth_bad.json()["error"] == "invalid_data_center"


def test_zoho_sync_concurrency_guard(setup_data, db_session: Session):
    tenant = setup_data["tenant"]
    mock_http = MockZohoHttpClient()
    client = ZohoRecruitClient(
        client_id="cid", client_secret="sec", redirect_uri="https://cb.test", http_client=mock_http
    )
    # Ensure integration is active
    state = ZohoService.create_oauth_state(db_session, tenant.id)
    ZohoService.exchange_and_save_tokens(
        db_session, tenant_id=tenant.id, state_token=state, code="valid_code", client=client
    )

    integration = db_session.query(ZohoIntegration).filter(ZohoIntegration.tenant_id == tenant.id).first()

    # Active sync job running
    active_sync = SyncJob(
        tenant_id=tenant.id,
        integration_id=integration.id,
        sync_type="full",
        status="running",
        started_at=datetime.now(timezone.utc),
    )
    db_session.add(active_sync)
    db_session.commit()

    # Triggering another sync raises ConflictError
    with pytest.raises(ConflictError, match="already in progress"):
        ZohoService.execute_sync(db_session, tenant_id=tenant.id, client=client)


def test_zoho_stale_sync_recovery(setup_data, db_session: Session):
    tenant = setup_data["tenant"]
    mock_http = MockZohoHttpClient()
    client = ZohoRecruitClient(
        client_id="cid", client_secret="sec", redirect_uri="https://cb.test", http_client=mock_http
    )
    state = ZohoService.create_oauth_state(db_session, tenant.id)
    integration = ZohoService.exchange_and_save_tokens(
        db_session, tenant_id=tenant.id, state_token=state, code="valid_code", client=client
    )

    # Stale sync job running from 45 mins ago
    stale_time = datetime.now(timezone.utc) - timedelta(minutes=45)
    stale_sync = SyncJob(
        tenant_id=tenant.id,
        integration_id=integration.id,
        sync_type="full",
        status="running",
        started_at=stale_time,
        created_at=stale_time,
    )
    db_session.add(stale_sync)
    db_session.commit()

    # Triggering sync should recover stale job (mark failed) and proceed
    new_sync = ZohoService.execute_sync(db_session, tenant_id=tenant.id, client=client)
    assert new_sync.status == "completed"

    db_session.refresh(stale_sync)
    assert stale_sync.status == "failed"

    # AuditEvent must be recorded
    audit = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.tenant_id == tenant.id, AuditEvent.event_type == "zoho.stale_sync_recovered")
        .first()
    )
    assert audit is not None
    assert audit.outcome == "failure"


def _zoho_client(mock_http: MockZohoHttpClient | None = None, data_center: str = "com") -> ZohoRecruitClient:
    return ZohoRecruitClient(
        client_id="cid",
        client_secret="sec",
        redirect_uri="https://cb.test",
        data_center=data_center,
        http_client=mock_http or MockZohoHttpClient(),
    )


def _oauth_state_row(db_session: Session, state_token: str) -> OAuthState | None:
    return db_session.query(OAuthState).filter(OAuthState.state_token == state_token).first()


def test_oauth_state_accepted_once(setup_data, db_session: Session):
    tenant = setup_data["tenant"]
    client = _zoho_client()
    state = ZohoService.create_oauth_state(db_session, tenant.id, data_center="com")
    assert state.startswith("com_")

    integration = ZohoService.exchange_and_save_tokens(
        db_session,
        tenant_id=tenant.id,
        state_token=state,
        code="valid_auth_code",
        client=client,
    )
    assert integration.status == "active"
    db_session.expire_all()
    row = _oauth_state_row(db_session, state)
    assert row is not None
    assert row.used is True


def test_oauth_state_cannot_be_reused_after_success(setup_data, db_session: Session):
    tenant = setup_data["tenant"]
    client = _zoho_client()
    state = ZohoService.create_oauth_state(db_session, tenant.id)
    ZohoService.exchange_and_save_tokens(
        db_session, tenant_id=tenant.id, state_token=state, code="valid_auth_code", client=client
    )
    with pytest.raises(OAuthError, match="Invalid or expired OAuth state"):
        ZohoService.exchange_and_save_tokens(
            db_session, tenant_id=tenant.id, state_token=state, code="valid_auth_code", client=client
        )


def test_oauth_state_consumed_when_token_exchange_fails(setup_data, db_session: Session):
    tenant = setup_data["tenant"]
    client = _zoho_client()
    state = ZohoService.create_oauth_state(db_session, tenant.id)
    with pytest.raises(OAuthError):
        ZohoService.exchange_and_save_tokens(
            db_session, tenant_id=tenant.id, state_token=state, code="invalid_code", client=client
        )
    db_session.expire_all()
    row = _oauth_state_row(db_session, state)
    assert row is not None
    assert row.used is True
    assert ZohoService.get_integration(db_session, tenant.id) is None
    with pytest.raises(OAuthError, match="Invalid or expired OAuth state"):
        ZohoService.exchange_and_save_tokens(
            db_session, tenant_id=tenant.id, state_token=state, code="valid_auth_code", client=client
        )


def test_expired_oauth_state_is_rejected(setup_data, db_session: Session):
    tenant = setup_data["tenant"]
    client = _zoho_client()
    state = ZohoService.create_oauth_state(db_session, tenant.id)
    row = _oauth_state_row(db_session, state)
    assert row is not None
    row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db_session.commit()

    with pytest.raises(OAuthError, match="Invalid or expired OAuth state"):
        ZohoService.exchange_and_save_tokens(
            db_session, tenant_id=tenant.id, state_token=state, code="valid_auth_code", client=client
        )
    db_session.expire_all()
    row = _oauth_state_row(db_session, state)
    assert row is not None
    assert row.used is False
    assert ZohoService.get_integration(db_session, tenant.id) is None


def test_oauth_state_rejected_for_different_tenant(setup_data, db_session: Session):
    tenant = setup_data["tenant"]
    other = Tenant(slug="other-zoho-tenant", name="Other Tenant")
    db_session.add(other)
    db_session.commit()
    client = _zoho_client()
    state = ZohoService.create_oauth_state(db_session, tenant.id)

    with pytest.raises(OAuthError, match="Invalid or expired OAuth state"):
        ZohoService.exchange_and_save_tokens(
            db_session, tenant_id=other.id, state_token=state, code="valid_auth_code", client=client
        )
    db_session.expire_all()
    row = _oauth_state_row(db_session, state)
    assert row is not None
    assert row.used is False
    assert row.tenant_id == tenant.id

    integration = ZohoService.exchange_and_save_tokens(
        db_session, tenant_id=tenant.id, state_token=state, code="valid_auth_code", client=client
    )
    assert integration.status == "active"


def test_invalid_oauth_state_token_is_rejected(setup_data, db_session: Session):
    tenant = setup_data["tenant"]
    client = _zoho_client()
    with pytest.raises(OAuthError, match="Invalid or expired OAuth state"):
        ZohoService.exchange_and_save_tokens(
            db_session,
            tenant_id=tenant.id,
            state_token="com_this_token_does_not_exist",
            code="valid_auth_code",
            client=client,
        )


def test_oauth_state_rejected_for_different_provider(setup_data, db_session: Session):
    tenant = setup_data["tenant"]
    client = _zoho_client()
    state = ZohoService.create_oauth_state(db_session, tenant.id)
    row = _oauth_state_row(db_session, state)
    assert row is not None
    row.provider = "other_provider"
    db_session.commit()

    with pytest.raises(OAuthError, match="Invalid or expired OAuth state"):
        ZohoService.exchange_and_save_tokens(
            db_session, tenant_id=tenant.id, state_token=state, code="valid_auth_code", client=client
        )
    db_session.expire_all()
    row = _oauth_state_row(db_session, state)
    assert row is not None
    assert row.used is False
    assert row.provider == "other_provider"


def test_concurrent_oauth_state_claim_not_proven_on_sqlite():
    """SQLite cannot reliably prove PostgreSQL UPDATE atomicity under concurrent callbacks.

    The claim uses a conditional UPDATE ... WHERE used=False AND expires_at > cutoff,
    then commits before the Zoho HTTP call. Concurrent safety should be verified with
    a PostgreSQL integration test that opens two sessions, starts both claims against
    the same unused state row, and asserts exactly one success. See
    tests/test_postgres_concurrency.py::test_postgres_concurrent_oauth_state_claim.
    """
    pytest.skip(
        "Concurrent OAuth state claim races are not proven on SQLite; "
        "run the PostgreSQL test when a disposable Postgres schema is available."
    )


def test_callback_uses_bound_non_default_data_center(setup_data, db_session: Session):
    tenant = setup_data["tenant"]
    mock_http = MockZohoHttpClient()
    client = _zoho_client(mock_http, data_center="com")
    state = ZohoService.create_oauth_state(db_session, tenant.id, data_center="eu")
    assert state.startswith("eu_")

    integration = ZohoService.exchange_and_save_tokens(
        db_session, tenant_id=tenant.id, state_token=state, code="valid_auth_code", client=client
    )
    assert integration.data_center == "eu"
    assert client.data_center == "eu"
    assert client.accounts_url == DATACENTER_DOMAINS["eu"]["accounts"]
    token_posts = [call for call in mock_http.call_history if call[0] == "POST"]
    assert token_posts
    assert token_posts[0][1].startswith("https://accounts.zoho.eu/")


def test_data_center_prefix_tampering_is_rejected(setup_data, db_session: Session):
    tenant = setup_data["tenant"]
    mock_http = MockZohoHttpClient()
    client = _zoho_client(mock_http, data_center="com")
    state = ZohoService.create_oauth_state(db_session, tenant.id, data_center="eu")
    _dc, raw = state.split("_", 1)
    tampered = f"in_{raw}"

    with pytest.raises(OAuthError, match="Invalid or expired OAuth state"):
        ZohoService.exchange_and_save_tokens(
            db_session, tenant_id=tenant.id, state_token=tampered, code="valid_auth_code", client=client
        )
    assert not any(call[0] == "POST" for call in mock_http.call_history)
    db_session.expire_all()
    original = _oauth_state_row(db_session, state)
    assert original is not None
    assert original.used is False
    assert ZohoService.get_integration(db_session, tenant.id) is None


def test_token_response_missing_access_token_is_rejected(setup_data, db_session: Session):
    tenant = setup_data["tenant"]
    client = _zoho_client()
    state = ZohoService.create_oauth_state(db_session, tenant.id)

    def _missing_access(_code: str):
        return {"refresh_token": "r", "expires_in": 3600}

    client.exchange_code_for_tokens = _missing_access  # type: ignore[method-assign]
    with pytest.raises(OAuthError, match="access_token"):
        ZohoService.exchange_and_save_tokens(
            db_session, tenant_id=tenant.id, state_token=state, code="valid_auth_code", client=client
        )
    db_session.expire_all()
    assert _oauth_state_row(db_session, state).used is True
    assert ZohoService.get_integration(db_session, tenant.id) is None


def test_token_response_malformed_expires_in_is_rejected(setup_data, db_session: Session):
    tenant = setup_data["tenant"]
    client = _zoho_client()
    state = ZohoService.create_oauth_state(db_session, tenant.id)

    def _bad_expires(_code: str):
        return {"access_token": "zoho_access_token_123", "expires_in": "not-a-number"}

    client.exchange_code_for_tokens = _bad_expires  # type: ignore[method-assign]
    with pytest.raises(OAuthError, match="expires_in"):
        ZohoService.exchange_and_save_tokens(
            db_session, tenant_id=tenant.id, state_token=state, code="valid_auth_code", client=client
        )
    db_session.expire_all()
    assert ZohoService.get_integration(db_session, tenant.id) is None


def test_empty_refresh_token_does_not_overwrite_existing(setup_data, db_session: Session):
    tenant = setup_data["tenant"]
    client = _zoho_client()
    state = ZohoService.create_oauth_state(db_session, tenant.id)
    integration = ZohoService.exchange_and_save_tokens(
        db_session, tenant_id=tenant.id, state_token=state, code="valid_auth_code", client=client
    )
    original_refresh = integration.encrypted_refresh_token
    assert original_refresh

    state2 = ZohoService.create_oauth_state(db_session, tenant.id)

    def _no_refresh(_code: str):
        return {"access_token": "rotated_access_token", "refresh_token": "  ", "expires_in": 1800}

    client.exchange_code_for_tokens = _no_refresh  # type: ignore[method-assign]
    updated = ZohoService.exchange_and_save_tokens(
        db_session, tenant_id=tenant.id, state_token=state2, code="valid_auth_code", client=client
    )
    assert decrypt_token(updated.encrypted_access_token) == "rotated_access_token"
    assert updated.encrypted_refresh_token == original_refresh


def test_production_zoho_client_fails_closed_when_config_missing(monkeypatch):
    from types import SimpleNamespace
    from app.integrations.zoho import router as zoho_router

    def _settings(**overrides):
        values = dict(
            is_production=True,
            zoho_client_id="client-id",
            zoho_client_secret="client-secret",
            zoho_redirect_uri="https://app.example/v2/integrations/zoho/callback",
            zoho_data_center="com",
        )
        values.update(overrides)
        return SimpleNamespace(**values)

    monkeypatch.setattr(zoho_router, "get_settings", lambda: _settings(zoho_client_id=""))
    with pytest.raises(OAuthError, match="client ID"):
        zoho_router._get_zoho_client()

    monkeypatch.setattr(zoho_router, "get_settings", lambda: _settings(zoho_client_secret=""))
    with pytest.raises(OAuthError, match="client secret"):
        zoho_router._get_zoho_client()

    monkeypatch.setattr(zoho_router, "get_settings", lambda: _settings(zoho_redirect_uri=""))
    with pytest.raises(OAuthError, match="redirect URI"):
        zoho_router._get_zoho_client()

    monkeypatch.setattr(zoho_router, "get_settings", lambda: _settings(zoho_data_center="mars"))
    with pytest.raises(OAuthError, match="data center"):
        zoho_router._get_zoho_client()

    monkeypatch.setattr(zoho_router, "get_settings", lambda: _settings())
    client = zoho_router._get_zoho_client()
    assert client.client_id == "client-id"
    assert "dummy" not in client.client_id
    assert "example.com" not in client.redirect_uri


def test_non_production_allows_placeholder_zoho_client(monkeypatch):
    from types import SimpleNamespace
    from app.integrations.zoho import router as zoho_router

    monkeypatch.setattr(
        zoho_router,
        "get_settings",
        lambda: SimpleNamespace(
            is_production=False,
            zoho_client_id="",
            zoho_client_secret="",
            zoho_redirect_uri="",
            zoho_data_center="com",
        ),
    )
    client = zoho_router._get_zoho_client()
    assert client.client_id == "dummy_client_id"
