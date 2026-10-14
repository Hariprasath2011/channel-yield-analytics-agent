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
from app.persistence.models import AuditEvent, CanonicalRecord, Membership, SyncJob, Tenant, User, ZohoIntegration


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
        assert "accounts.zoho.eu" in resp_auth_eu.json()["authorization_url"]

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
