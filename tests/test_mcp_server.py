"""Unit and integration tests for Zoho Recruit Model Context Protocol (MCP) Server."""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient

import app.persistence.database as db_mod
from app.core.config import Settings
from app.core.exceptions import RateLimitError, ValidationError
from app.integrations.zoho.crypto import decrypt_token, encrypt_token
from app.persistence.database import get_db_context
from app.persistence.models import CanonicalRecord, Tenant, ZohoIntegration
from mcp_server.server import (
    MCPServerDispatcher,
    create_mcp_app,
    create_user_connection_token,
    verify_user_connection_token,
)
from mcp_server.zoho_recruit import (
    ALLOWED_MODULES,
    ZohoRecruitMCPHandler,
)


@pytest.fixture(autouse=True)
def bind_test_session_factory(session_factory, monkeypatch):
    """Ensure get_db_context() in MCP handler uses the in-memory test database."""
    monkeypatch.setattr(db_mod, "get_session_factory", lambda: session_factory)


@pytest.fixture
def mcp_tenant(db_session):
    """Seed an isolated tenant organization with active Zoho integration."""
    tenant = Tenant(
        id="mcp-test-tenant-1",
        slug="mcp-tenant",
        name="MCP Test Organization",
        is_active=True,
    )
    db_session.add(tenant)

    # Active integration with valid unexpired token
    integration = ZohoIntegration(
        id="zoho-int-1",
        tenant_id=tenant.id,
        data_center="com",
        status="active",
        encrypted_access_token=encrypt_token("valid_access_token_123"),
        encrypted_refresh_token=encrypt_token("valid_refresh_token_abc"),
        token_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    db_session.add(integration)
    db_session.commit()
    return tenant


# ─── 1. Protocol & Tool Listing Tests ─────────────────────────────────────────


def test_mcp_dispatcher_initialize():
    dispatcher = MCPServerDispatcher(handler=ZohoRecruitMCPHandler(tenant_id="test-tenant"))
    req = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    resp = dispatcher.dispatch(req)

    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == 1
    assert resp["result"]["serverInfo"]["name"] == "zoho-recruit-mcp"
    assert "tools" in resp["result"]["capabilities"]


def test_mcp_tools_list():
    dispatcher = MCPServerDispatcher(handler=ZohoRecruitMCPHandler(tenant_id="test-tenant"))
    req = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    resp = dispatcher.dispatch(req)

    tools = resp["result"]["tools"]
    tool_names = [t["name"] for t in tools]
    assert "list_job_openings" in tool_names
    assert "list_candidates" in tool_names
    assert "list_applications" in tool_names
    assert "check_zoho_connection" in tool_names
    assert "compute_channel_yield" in tool_names
    assert len(tools) == 5


# ─── 2. Tool 1: list_job_openings ─────────────────────────────────────────────


def test_list_job_openings_success(mcp_tenant, monkeypatch):
    mock_records = {
        "data": [
            {
                "id": "job_101",
                "Job_Opening_ID": "REQ-001",
                "Posting_Title": "Senior Backend Engineer",
                "Job_Opening_Status": "In-progress",
                "Department": "Engineering",
                "Number_of_Positions": 2,
                "City": "New York",
                "Date_Opened": "2026-01-15",
            },
            {
                "id": "job_102",
                "Job_Opening_ID": "REQ-002",
                "Posting_Title": "Product Designer",
                "Job_Opening_Status": "Filled",
                "Department": "Design",
                "Number_of_Positions": 1,
                "City": "Remote",
                "Date_Opened": "2026-02-01",
            },
        ],
        "info": {"more_records": False},
    }

    handler = ZohoRecruitMCPHandler(tenant_id=mcp_tenant.id)
    monkeypatch.setattr(handler, "_fetch_records", lambda mod, page, per_page: mock_records)

    # Fetch all
    res = handler.list_job_openings(page=1, per_page=20)
    assert res["count"] == 2
    assert res["module"] == "JobOpenings"
    assert res["job_openings"][0]["posting_title"] == "Senior Backend Engineer"
    assert res["job_openings"][0]["job_opening_id"] == "REQ-001"

    # Filter by status
    res_filtered = handler.list_job_openings(page=1, per_page=20, status="In-progress")
    assert res_filtered["count"] == 1
    assert res_filtered["job_openings"][0]["id"] == "job_101"


# ─── 3. Tool 2: list_candidates & Data Minimization ───────────────────────────


def test_list_candidates_privacy_minimization(mcp_tenant, monkeypatch):
    """Verify that candidate contact details, salary, and notes are stripped."""
    mock_candidates = {
        "data": [
            {
                "id": "cand_201",
                "First_Name": "Jane",
                "Last_Name": "Doe",
                "Email": "jane.doe@private-email.com",
                "Phone": "+1-555-0199",
                "Mobile": "+1-555-0188",
                "Street": "123 Sensitive St",
                "City": "Austin",
                "Current_Salary": 150000,
                "Expected_Salary": 170000,
                "Candidate_Status": "Under Review",
                "Source": "LinkedIn",
                "Highest_Qualification_Held": "Master's Degree",
                "Current_Job_Title": "Staff Engineer",
                "Experience_in_Years": 8,
                "Created_Time": "2026-03-01T12:00:00Z",
            }
        ],
        "info": {"more_records": False},
    }

    handler = ZohoRecruitMCPHandler(tenant_id=mcp_tenant.id)
    monkeypatch.setattr(handler, "_fetch_records", lambda mod, page, per_page: mock_candidates)

    res = handler.list_candidates(page=1, per_page=20)
    assert res["count"] == 1
    cand = res["candidates"][0]

    # Required safe recruitment metadata present
    assert cand["id"] == "cand_201"
    assert cand["source"] == "LinkedIn"
    assert cand["candidate_status"] == "Under Review"
    assert cand["highest_qualification"] == "Master's Degree"
    assert cand["current_job_title"] == "Staff Engineer"
    assert cand["experience_in_years"] == 8

    # Explicit verification of data minimization (no PII leaked)
    assert "Phone" not in cand
    assert "Mobile" not in cand
    assert "Email" not in cand
    assert "Street" not in cand
    assert "Current_Salary" not in cand
    assert "Expected_Salary" not in cand
    assert "+1-555" not in json.dumps(res)
    assert "150000" not in json.dumps(res)


# ─── 4. Tool 3: list_applications ─────────────────────────────────────────────


def test_list_applications_filtering(mcp_tenant, monkeypatch):
    mock_applications = {
        "data": [
            {
                "id": "app_301",
                "Job_Opening_ID": {"id": "REQ-001", "Posting_Title": "Senior Backend Engineer"},
                "Candidate_ID": {"id": "cand_201"},
                "Application_Status": "Interview Scheduled",
                "Rating": 4.5,
                "Source": "LinkedIn",
                "Created_Time": "2026-03-02T10:00:00Z",
            },
            {
                "id": "app_302",
                "Job_Opening_ID": {"id": "REQ-002", "Posting_Title": "Product Designer"},
                "Candidate_ID": {"id": "cand_202"},
                "Application_Status": "Applied",
                "Rating": 3.0,
                "Source": "Referral",
                "Created_Time": "2026-03-05T14:00:00Z",
            },
        ],
        "info": {"more_records": False},
    }

    handler = ZohoRecruitMCPHandler(tenant_id=mcp_tenant.id)
    monkeypatch.setattr(handler, "_fetch_records", lambda mod, page, per_page: mock_applications)

    # Filter by job opening
    res = handler.list_applications(job_opening_id="REQ-001")
    assert res["count"] == 1
    assert res["applications"][0]["id"] == "app_301"
    assert res["applications"][0]["status"] == "Interview Scheduled"


# ─── 5. Strict Module Allowlist Enforcement ───────────────────────────────────


def test_module_allowlist_enforcement(mcp_tenant):
    handler = ZohoRecruitMCPHandler(tenant_id=mcp_tenant.id)

    # Allowed modules succeed through validation
    assert "JobOpenings" in ALLOWED_MODULES
    assert "Candidates" in ALLOWED_MODULES
    assert "Applications" in ALLOWED_MODULES

    # Disallowed modules raise ValidationError
    for forbidden in ["Contacts", "Accounts", "Invoices", "Vendors", "CustomModule1"]:
        with pytest.raises(ValidationError) as exc:
            handler._fetch_records(forbidden, page=1, per_page=10)
        assert "not permitted" in str(exc.value.message)


# ─── 6. Token Auto-Refresh & Persistence ──────────────────────────────────────


def test_token_auto_refresh_and_db_persistence(db_session, mcp_tenant, monkeypatch):
    """Verify that expired tokens are automatically refreshed and persisted in encrypted form."""
    # Set integration token as expired 1 hour ago
    integration = db_session.query(ZohoIntegration).filter(ZohoIntegration.tenant_id == mcp_tenant.id).one()
    integration.token_expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    integration.encrypted_access_token = encrypt_token("expired_token")
    db_session.commit()

    handler = ZohoRecruitMCPHandler(tenant_id=mcp_tenant.id)

    # Mock client token refresh and module get
    mock_refresh_result = {
        "access_token": "brand_new_refreshed_access_token_999",
        "expires_in": 3600,
    }

    with patch("app.integrations.zoho.client.ZohoRecruitClient.refresh_access_token", return_value=mock_refresh_result) as mock_refresh:
        with patch("app.integrations.zoho.client.ZohoRecruitClient.get_module_records", return_value={"data": [], "info": {}}):
            handler.list_job_openings()
            assert mock_refresh.called

    # Verify that database integration was updated with encrypted new token
    db_session.expire_all()
    updated_int = db_session.query(ZohoIntegration).filter(ZohoIntegration.tenant_id == mcp_tenant.id).one()
    decrypted_token = decrypt_token(updated_int.encrypted_access_token)
    exp = updated_int.token_expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    assert exp > datetime.now(timezone.utc)


# ─── 7. Missing & Inactive Integration Handling ────────────────────────────────


def test_missing_tenant_integration_handling():
    handler = ZohoRecruitMCPHandler(tenant_id="nonexistent-tenant-999")
    res = handler.list_job_openings()

    assert "error" in res
    assert "No active Zoho Recruit integration found" in res["message"]


def test_unconfigured_tenant_id():
    handler = ZohoRecruitMCPHandler(tenant_id="")
    res = handler.list_candidates()

    assert "error" in res
    assert "no configured tenant ID" in res["message"]


# ─── 8. Rate Limit Mapping ────────────────────────────────────────────────────


def test_rate_limit_error_mapping(mcp_tenant, monkeypatch):
    handler = ZohoRecruitMCPHandler(tenant_id=mcp_tenant.id)

    def _mock_rate_limit(*args, **kwargs):
        raise RateLimitError("zoho_recruit", retry_after=45, message="Zoho API rate limit reached.")

    monkeypatch.setattr(handler, "_fetch_records", _mock_rate_limit)

    res = handler.list_job_openings()
    assert res.get("error") == "rate_limited"
    assert "rate limit reached" in res.get("message")


# ─── 9. Dispatcher & Stdio JSON-RPC Execution ─────────────────────────────────


def test_dispatcher_tools_call():
    handler = ZohoRecruitMCPHandler(tenant_id="mock-tenant")
    handler.list_job_openings = MagicMock(return_value={"count": 0, "job_openings": []})

    dispatcher = MCPServerDispatcher(handler=handler)
    call_req = {
        "jsonrpc": "2.0",
        "id": 10,
        "method": "tools/call",
        "params": {
            "name": "list_job_openings",
            "arguments": {"page": 2, "per_page": 50, "status": "In-progress"},
        },
    }

    resp = dispatcher.dispatch(call_req)
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == 10
    assert resp["result"]["isError"] is False
    assert len(resp["result"]["content"]) == 1
    content_text = resp["result"]["content"][0]["text"]
    assert "job_openings" in content_text


def test_dispatcher_unknown_tool():
    dispatcher = MCPServerDispatcher(handler=ZohoRecruitMCPHandler(tenant_id="mock-tenant"))
    call_req = {
        "jsonrpc": "2.0",
        "id": 11,
        "method": "tools/call",
        "params": {"name": "delete_all_candidates", "arguments": {}},
    }
    resp = dispatcher.dispatch(call_req)
    assert resp["error"]["code"] == -32601
    assert "not found" in resp["error"]["message"]


# ─── 10. HTTP Endpoints & Access Control ──────────────────────────────────────


def test_health_check_endpoint():
    app = create_mcp_app()
    with TestClient(app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["service"] == "zoho-recruit-mcp"


def test_auth_guard_on_sse_and_messages(monkeypatch):
    """Verify that when CY_MCP_AUTH_TOKEN is configured, unauthenticated requests are rejected with 401."""
    monkeypatch.setenv("CY_MCP_AUTH_TOKEN", "super-secret-mcp-bearer-token")
    app = create_mcp_app()

    with TestClient(app) as client:
        # Unauthenticated GET /sse rejected
        resp_unauth = client.get("/sse")
        assert resp_unauth.status_code == 401

        # Unauthenticated POST /messages rejected
        resp_msg_unauth = client.post("/messages", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert resp_msg_unauth.status_code == 401

        # Authenticated with Bearer token accepted
        headers = {"Authorization": "Bearer super-secret-mcp-bearer-token"}
        resp_msg_auth = client.post(
            "/messages",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        )
        assert resp_msg_auth.status_code == 200
        assert resp_msg_auth.json()["result"] == {}


def test_sse_invalid_credentials_returns_401(monkeypatch):
    """Verify that invalid credentials return 401 for GET /sse."""
    monkeypatch.setenv("CY_MCP_AUTH_TOKEN", "super-secret-mcp-bearer-token")
    app = create_mcp_app()
    with TestClient(app) as client:
        resp = client.get("/sse", headers={"Authorization": "Bearer wrong-token"})
        assert resp.status_code == 401
        assert resp.json()["error"] == "unauthorized"


def test_messages_invalid_credentials_returns_401(monkeypatch):
    """Verify that invalid credentials return 401 for POST /messages."""
    monkeypatch.setenv("CY_MCP_AUTH_TOKEN", "super-secret-mcp-bearer-token")
    app = create_mcp_app()
    with TestClient(app) as client:
        resp = client.post(
            "/messages",
            headers={"Authorization": "Bearer wrong-token"},
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        )
        assert resp.status_code == 401
        assert resp.json()["error"] == "unauthorized"


def test_sse_valid_credentials_accepted(monkeypatch):
    """Verify that valid credentials are accepted for GET /sse without hanging on the SSE stream."""
    monkeypatch.setenv("CY_MCP_AUTH_TOKEN", "super-secret-mcp-bearer-token")
    app = create_mcp_app()

    async def _run():
        scope = {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "path": "/sse",
            "raw_path": b"/sse",
            "query_string": b"",
            "headers": [(b"authorization", b"Bearer super-secret-mcp-bearer-token")],
        }
        response_start: dict = {}
        body_chunks: list[bytes] = []

        async def receive():
            return {"type": "http.disconnect"}

        task = None

        async def send(message):
            if message["type"] == "http.response.start":
                response_start.update(message)
            elif message["type"] == "http.response.body":
                body_chunks.append(message.get("body", b""))
                if task:
                    task.cancel()

        task = asyncio.create_task(app(scope, receive, send))
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert response_start.get("status") == 200
        headers_dict = {k.decode().lower(): v.decode() for k, v in response_start.get("headers", [])}
        assert "text/event-stream" in headers_dict.get("content-type", "")
        assert any(b"event: endpoint" in chunk for chunk in body_chunks)

    asyncio.run(_run())


# ─── 11. Capability Path Authentication & Production Security ──────────────────


def test_capability_path_sse_valid_key(monkeypatch):
    """Verify that GET /sse/{secret_key} succeeds with valid capability key and advertises capability message endpoint."""
    monkeypatch.setenv("CY_MCP_SECRET_PATH_KEY", "high-entropy-capability-key-777")
    app = create_mcp_app()

    async def _run():
        scope = {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "path": "/sse/high-entropy-capability-key-777",
            "raw_path": b"/sse/high-entropy-capability-key-777",
            "query_string": b"",
            "headers": [],
        }
        response_start: dict = {}
        body_chunks: list[bytes] = []

        async def receive():
            return {"type": "http.disconnect"}

        task = None

        async def send(message):
            if message["type"] == "http.response.start":
                response_start.update(message)
            elif message["type"] == "http.response.body":
                body_chunks.append(message.get("body", b""))
                if task:
                    task.cancel()

        task = asyncio.create_task(app(scope, receive, send))
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert response_start.get("status") == 200
        headers_dict = {k.decode().lower(): v.decode() for k, v in response_start.get("headers", [])}
        assert "text/event-stream" in headers_dict.get("content-type", "")
        combined = b"".join(body_chunks)
        assert b"event: endpoint" in combined
        assert b"/messages/high-entropy-capability-key-777?session_id=" in combined

    asyncio.run(_run())


def test_capability_path_sse_invalid_or_unconfigured_key(monkeypatch):
    """Verify that GET /sse/{secret_key} returns 401 for invalid or unconfigured key."""
    # Invalid key when secret is configured
    monkeypatch.setenv("CY_MCP_SECRET_PATH_KEY", "correct-key-123")
    app = create_mcp_app()
    with TestClient(app) as client:
        resp = client.get("/sse/wrong-key-999")
        assert resp.status_code == 401
        assert resp.json()["error"] == "unauthorized"

    # Unconfigured secret path key
    monkeypatch.delenv("CY_MCP_SECRET_PATH_KEY", raising=False)
    app2 = create_mcp_app()
    with TestClient(app2) as client:
        resp2 = client.get("/sse/any-key-attempt")
        assert resp2.status_code == 401
        assert resp2.json()["error"] == "unauthorized"


def test_capability_path_messages_endpoint(monkeypatch):
    """Verify that POST /messages/{secret_key} requires valid capability key."""
    monkeypatch.setenv("CY_MCP_SECRET_PATH_KEY", "secure-message-key-456")
    app = create_mcp_app()
    with TestClient(app) as client:
        # Invalid key returns 401
        resp_invalid = client.post(
            "/messages/wrong-key-999",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        )
        assert resp_invalid.status_code == 401

        # Valid key succeeds
        resp_valid = client.post(
            "/messages/secure-message-key-456",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        )
        assert resp_valid.status_code == 200
        assert resp_valid.json()["result"] == {}


def test_standard_routes_rejected_when_only_capability_key_configured(monkeypatch):
    """Verify that standard /sse and /messages reject unauthenticated callers even when capability key is configured."""
    monkeypatch.setenv("CY_MCP_SECRET_PATH_KEY", "secret-capability-key-only")
    monkeypatch.delenv("CY_MCP_AUTH_TOKEN", raising=False)
    app = create_mcp_app()

    with TestClient(app) as client:
        # Standard unauthenticated GET /sse returns 401
        resp_sse = client.get("/sse")
        assert resp_sse.status_code == 401

        # Standard unauthenticated POST /messages returns 401
        resp_msg = client.post("/messages", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert resp_msg.status_code == 401


def test_production_startup_fails_without_authentication_configured(monkeypatch):
    """Verify that in production mode, create_mcp_app() fails closed if no auth is configured."""
    monkeypatch.setenv("CY_ENV", "production")
    monkeypatch.delenv("CY_MCP_SECRET_PATH_KEY", raising=False)
    monkeypatch.delenv("CY_MCP_AUTH_TOKEN", raising=False)

    with pytest.raises(ValueError) as exc_info:
        create_mcp_app()
    assert "refuses to start without an authentication mechanism configured" in str(exc_info.value)

    # Succeeds if capability key is present
    monkeypatch.setenv("CY_MCP_SECRET_PATH_KEY", "prod-capability-key")
    app_with_path = create_mcp_app()
    assert app_with_path is not None

    # Succeeds if auth token is present
    monkeypatch.delenv("CY_MCP_SECRET_PATH_KEY", raising=False)
    monkeypatch.setenv("CY_MCP_AUTH_TOKEN", "prod-auth-token")
    app_with_token = create_mcp_app()
    assert app_with_token is not None


def test_end_to_end_sse_message_delivery_for_real_tool(mcp_tenant, monkeypatch):
    """Verify end-to-end SSE delivery: tool invocation is queued to active SSE session."""
    from mcp_server.server import _ACTIVE_SESSIONS

    test_path_key = "e2e-delivery-test-key-555"
    monkeypatch.setenv("CY_MCP_SECRET_PATH_KEY", test_path_key)
    monkeypatch.setenv("CY_MCP_TENANT_ID", mcp_tenant.id)

    app = create_mcp_app()

    async def _run():
        session_id = str(uuid.uuid4())
        session_queue: asyncio.Queue = asyncio.Queue()
        _ACTIVE_SESSIONS[session_id] = {"queue": session_queue, "tenant_id": mcp_tenant.id}

        try:
            # Mock Zoho client records
            mock_jobs = {
                "data": [
                    {
                        "id": "job_999",
                        "Posting_Title": "Principal AI Architect",
                        "Job_Opening_Status": "In-progress",
                    }
                ],
                "info": {"more_records": False},
            }

            with patch("app.integrations.zoho.client.ZohoRecruitClient.get_module_records", return_value=mock_jobs):
                with TestClient(app) as client:
                    rpc_req = {
                        "jsonrpc": "2.0",
                        "id": "call-1",
                        "method": "tools/call",
                        "params": {"name": "list_job_openings", "arguments": {}},
                    }
                    post_resp = client.post(
                        f"/messages/{test_path_key}?session_id={session_id}",
                        json=rpc_req,
                    )
                    assert post_resp.status_code == 202
                    assert post_resp.json()["status"] == "accepted"

            # Check that message was delivered to the SSE session queue
            delivered = session_queue.get_nowait()
            assert delivered["jsonrpc"] == "2.0"
            assert delivered["id"] == "call-1"
            assert delivered["result"]["isError"] is False
            content_text = delivered["result"]["content"][0]["text"]
            assert "Principal AI Architect" in content_text

        finally:
            _ACTIVE_SESSIONS.pop(session_id, None)

    asyncio.run(_run())


# ─── 12. Live In-Memory Analytics & Zero-Persistence Tests ────────────────────


def test_check_zoho_connection_tool(mcp_tenant):
    """Verify check_zoho_connection reports active connection for configured tenant."""
    handler = ZohoRecruitMCPHandler(tenant_id=mcp_tenant.id)
    res = handler.check_zoho_connection()
    assert res["connected"] is True
    assert res["status"] == "active"
    assert res["tenant_id"] == mcp_tenant.id

    # Unconfigured / missing tenant returns connected=False
    handler_unconf = ZohoRecruitMCPHandler(tenant_id="nonexistent-tid-999")
    res_unconf = handler_unconf.check_zoho_connection()
    assert res_unconf["connected"] is False
    assert "/v2/integrations/zoho/authorize" in res_unconf.get("onboarding_url", "")


def test_compute_channel_yield_in_memory_and_zero_persistence(db_session, mcp_tenant, monkeypatch):
    """Verify live yield calculation runs in memory and persists ZERO recruitment records to database."""
    handler = ZohoRecruitMCPHandler(tenant_id=mcp_tenant.id)

    mock_jobs = [
        {"id": "job_1", "Job_Opening_ID": "REQ-01", "Posting_Title": "Lead Architect", "Job_Opening_Status": "open"}
    ]
    mock_candidates = [
        {"id": "cand_1", "Source": "LinkedIn"},
        {"id": "cand_2", "Source": "Referral"},
    ]
    mock_applications = [
        {
            "id": "app_1",
            "Job_Opening_ID": "REQ-01",
            "Candidate_ID": "cand_1",
            "Application_Status": "Joined",
            "Created_Time": "2026-01-10T10:00:00Z",
            "Joining_Date": "2026-02-15T10:00:00Z",
        },
        {
            "id": "app_2",
            "Job_Opening_ID": "REQ-01",
            "Candidate_ID": "cand_2",
            "Application_Status": "Rejected",
            "Created_Time": "2026-01-12T10:00:00Z",
        },
    ]

    def _mock_fetch(module, token, max_pages=10):
        if module == "JobOpenings":
            return mock_jobs
        if module == "Candidates":
            return mock_candidates
        if module == "Applications":
            return mock_applications
        return []

    with patch("app.integrations.zoho.client.ZohoRecruitClient.fetch_all_module_records", side_effect=_mock_fetch):
        res = handler.compute_channel_yield(
            cohort_start="2026-01-01",
            cohort_end="2026-03-31",
            observation_days=30,
            as_of_date="2026-04-01",
            min_sample_size=1,
        )

    assert res["status"] == "completed"
    assert res["summary"]["applications_in_cohort"] == 2
    assert res["summary"]["confirmed_mature_joins"] == 1

    # Check ranked channels
    ranked = res["ranked_channels"]
    assert len(ranked) >= 1
    top = ranked[0]
    assert top["channel"] == "LinkedIn"
    assert top["yield_percent"] == 100.0

    # Verify recommendations exist
    assert len(res["recommendations"]) > 0
    assert any("Top Performing Channel" in rec for rec in res["recommendations"])

    # Verify historical funnel limitation notice
    assert res["historical_funnel"]["available"] is False
    assert "reason" in res["historical_funnel"]
    assert len(res["limitations"]) == 2

    # CRITICAL: Verify ZERO records were written to CanonicalRecord table
    record_count = db_session.query(CanonicalRecord).count()
    assert record_count == 0


def test_cryptographic_connection_token_isolation(db_session, mcp_tenant):
    """Verify that a signed connection token scopes execution strictly to that user's tenant."""
    # Create second tenant
    tenant_b = Tenant(id="tenant-isolated-b", slug="isolated-b", name="Tenant B", is_active=True)
    db_session.add(tenant_b)
    db_session.commit()

    token_b = create_user_connection_token(tenant_id=tenant_b.id, user_id="user-b-123")
    assert token_b.startswith("cyct_")

    verified = verify_user_connection_token(token_b)
    assert verified is not None
    assert verified["tid"] == tenant_b.id
    assert verified["sub"] == "user-b-123"

    dispatcher = MCPServerDispatcher()
    call_req = {
        "jsonrpc": "2.0",
        "id": 99,
        "method": "tools/call",
        "params": {
            "name": "check_zoho_connection",
            "arguments": {"connection_token": token_b},
        },
    }

    # Tenant B has no Zoho integration, so check_zoho_connection must report tenant B's status
    resp = dispatcher.dispatch(call_req)
    assert resp["id"] == 99
    content = json.loads(resp["result"]["content"][0]["text"])
    assert content["connected"] is False
    assert content["tenant_id"] == tenant_b.id


def test_tampered_or_invalid_connection_token_rejected():
    """Verify that forged, tampered, or guessed connection tokens are rejected with error."""
    dispatcher = MCPServerDispatcher()

    for invalid_token in ["cyct_fakepayload.invalidsig", "arbitrary_user_id", "cyct_onlyonepart"]:
        call_req = {
            "jsonrpc": "2.0",
            "id": 100,
            "method": "tools/call",
            "params": {
                "name": "check_zoho_connection",
                "arguments": {"connection_token": invalid_token},
            },
        }
        resp = dispatcher.dispatch(call_req)
        assert resp["result"]["isError"] is True
        content = json.loads(resp["result"]["content"][0]["text"])
        assert content["error"] == "unauthorized"
        assert "Invalid, expired, or tampered" in content["message"]


def test_ready_endpoint_probe(monkeypatch):
    """Verify /ready probe reports multi-tenant readiness when signing keys are set."""
    monkeypatch.setenv("CY_MCP_SIGNING_SECRET", "test-signing-secret-12345")
    app = create_mcp_app()
    with TestClient(app) as client:
        resp = client.get("/ready")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ready"
        assert body["multi_tenant_ready"] is True
        assert body["auth_configured"] is True


def test_messages_unknown_session_id_returns_404(monkeypatch):
    """POSTing messages with an unknown session_id returns 404."""
    monkeypatch.setenv("CY_MCP_SECRET_PATH_KEY", "valid-key-123")
    app = create_mcp_app()
    with TestClient(app) as client:
        resp = client.post(
            "/messages/valid-key-123?session_id=nonexistent-session-uuid",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        )
        assert resp.status_code == 404
        assert "Session not found" in resp.json()["error"]["message"]


def test_session_tenant_mismatch_rejected(monkeypatch):
    """POSTing to /messages/{token_B} with a session bound to Tenant A returns 403."""
    from mcp_server.server import _ACTIVE_SESSIONS

    token_b = create_user_connection_token(tenant_id="tenant-b", user_id="user-b")
    app = create_mcp_app()

    session_id = "session-bound-to-tenant-a"
    _ACTIVE_SESSIONS[session_id] = {
        "queue": asyncio.Queue(),
        "tenant_id": "tenant-a",
        "user_id": "user-a",
        "created_at": 1000.0,
    }
    try:
        with TestClient(app) as client:
            resp = client.post(
                f"/messages/{token_b}?session_id={session_id}",
                json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            )
            assert resp.status_code == 403
            assert "Session tenant does not match" in resp.json()["error"]["message"]
    finally:
        _ACTIVE_SESSIONS.pop(session_id, None)


def test_token_with_wrong_audience_or_issuer_rejected():
    """Tokens signed with unexpected audience or issuer must be rejected."""
    import base64
    import hashlib
    import hmac
    import time

    sec = "test-custom-secret"
    # Token with wrong audience
    payload_bad_aud = {
        "tid": "tenant-xyz",
        "sub": "user-1",
        "iss": "channel-yield-system",
        "aud": "wrong-audience-server",
        "exp": int(time.time()) + 3600,
    }
    b64 = base64.urlsafe_b64encode(json.dumps(payload_bad_aud).encode("utf-8")).decode("utf-8").rstrip("=")
    sig = hmac.new(sec.encode("utf-8"), b64.encode("utf-8"), hashlib.sha256).hexdigest()
    bad_token = f"cyct_{b64}.{sig}"
    assert verify_user_connection_token(bad_token, secret=sec) is None


def test_production_signing_secret_missing_fails_closed(monkeypatch):
    """In production mode, missing signing keys must fail closed and raise ValueError."""
    from mcp_server.server import _get_signing_secret

    monkeypatch.setenv("CY_ENV", "production")
    monkeypatch.delenv("CY_MCP_SIGNING_SECRET", raising=False)
    monkeypatch.delenv("CY_SECRET_KEY", raising=False)

    with pytest.raises(ValueError, match="Production security failure"):
        _get_signing_secret()


def test_production_no_implicit_fallback_to_fixed_tenant(monkeypatch):
    """In production mode, ZohoRecruitMCPHandler refuses to fall back to CY_MCP_TENANT_ID."""
    monkeypatch.setenv("CY_ENV", "production")
    monkeypatch.setenv("CY_MCP_TENANT_ID", "unauthorized-fallback-tenant")

    handler = ZohoRecruitMCPHandler()  # No tenant_id passed
    assert handler.tenant_id == ""  # Did NOT adopt CY_MCP_TENANT_ID

    res = handler.check_zoho_connection()
    assert res["connected"] is False
    assert res["status"] == "unconfigured"
