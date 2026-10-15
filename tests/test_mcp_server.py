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
from app.persistence.models import Tenant, ZohoIntegration
from mcp_server.server import MCPServerDispatcher, create_mcp_app
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
    assert len(tools) == 3


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
        _ACTIVE_SESSIONS[session_id] = session_queue

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
