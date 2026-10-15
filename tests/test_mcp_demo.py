"""Unit and integration tests for the temporary Zoho Recruit Demo MCP Server.

Validates:
- Health check probe
- MCP JSON-RPC protocol compliance (initialize, ping, tools/list, tools/call)
- Fictional data minimization and filtering
- SSE connection establishment and endpoint advertisement
- Message posting to active sessions and SSE queue delivery
- Session validation and error handling (missing session, expired/nonexistent session, parse errors)
"""
from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from starlette.testclient import TestClient

from mcp_demo.server import (
    DEMO_TOOLS,
    FICTIONAL_APPLICATIONS,
    FICTIONAL_CANDIDATES,
    FICTIONAL_JOB_OPENINGS,
    DemoMCPDispatcher,
    create_demo_app,
)


# ─── 1. Health Probe ──────────────────────────────────────────────────────────


def test_demo_health_check():
    """Verify that /health returns operational status and demo mode."""
    app = create_demo_app()
    with TestClient(app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["service"] == "zoho-recruit-mcp-demo"
        assert data["mode"] == "fictional-demo-data"
        assert "active_sessions" in data


# ─── 2. MCP JSON-RPC Protocol Dispatching ──────────────────────────────────────


def test_demo_dispatcher_initialize():
    dispatcher = DemoMCPDispatcher()
    req = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "1.0.0"},
        },
    }
    resp = dispatcher.dispatch(req)
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == 1
    assert resp["result"]["protocolVersion"] == "2024-11-05"
    assert resp["result"]["serverInfo"]["name"] == "zoho-recruit-mcp-demo"


def test_demo_dispatcher_notifications_initialized():
    dispatcher = DemoMCPDispatcher()
    req = {
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
        "params": {},
    }
    resp = dispatcher.dispatch(req)
    assert resp is None


def test_demo_dispatcher_ping():
    dispatcher = DemoMCPDispatcher()
    req = {"jsonrpc": "2.0", "id": "ping-42", "method": "ping"}
    resp = dispatcher.dispatch(req)
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == "ping-42"
    assert resp["result"] == {}


def test_demo_dispatcher_tools_list():
    dispatcher = DemoMCPDispatcher()
    req = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    resp = dispatcher.dispatch(req)
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == 2
    tools = resp["result"]["tools"]
    tool_names = [t["name"] for t in tools]
    assert "list_job_openings" in tool_names
    assert "list_candidates" in tool_names
    assert "list_applications" in tool_names


# ─── 3. Fictional Data Tool Calls ─────────────────────────────────────────────


def test_demo_tools_call_job_openings():
    dispatcher = DemoMCPDispatcher()

    # Call without filter
    call_req = {
        "jsonrpc": "2.0",
        "id": 10,
        "method": "tools/call",
        "params": {"name": "list_job_openings", "arguments": {"limit": 2}},
    }
    resp = dispatcher.dispatch(call_req)
    assert resp["result"]["isError"] is False
    content = json.loads(resp["result"]["content"][0]["text"])
    assert content["mode"] == "fictional-demo-data"
    assert content["count"] == 2
    assert len(content["job_openings"]) == 2

    # Call with status filter
    call_filtered = {
        "jsonrpc": "2.0",
        "id": 11,
        "method": "tools/call",
        "params": {"name": "list_job_openings", "arguments": {"status": "Draft"}},
    }
    resp_filtered = dispatcher.dispatch(call_filtered)
    content_filtered = json.loads(resp_filtered["result"]["content"][0]["text"])
    assert content_filtered["count"] == 1
    assert content_filtered["job_openings"][0]["status"] == "Draft"


def test_demo_tools_call_candidates():
    dispatcher = DemoMCPDispatcher()
    call_req = {
        "jsonrpc": "2.0",
        "id": 20,
        "method": "tools/call",
        "params": {"name": "list_candidates", "arguments": {}},
    }
    resp = dispatcher.dispatch(call_req)
    assert resp["result"]["isError"] is False
    content = json.loads(resp["result"]["content"][0]["text"])
    assert content["mode"] == "fictional-demo-data"
    assert content["count"] >= 1
    # Check that records are fictional summaries with no real email/phone
    for cand in content["candidates"]:
        assert "email" not in cand
        assert "phone" not in cand
        assert "Demo Candidate" in cand["candidate_reference"]


def test_demo_tools_call_applications():
    dispatcher = DemoMCPDispatcher()

    # Filter by job opening ID
    call_req = {
        "jsonrpc": "2.0",
        "id": 30,
        "method": "tools/call",
        "params": {"name": "list_applications", "arguments": {"job_opening_id": "JOB-DEMO-001"}},
    }
    resp = dispatcher.dispatch(call_req)
    assert resp["result"]["isError"] is False
    content = json.loads(resp["result"]["content"][0]["text"])
    assert content["mode"] == "fictional-demo-data"
    for app in content["applications"]:
        assert app["job_opening_id"] == "JOB-DEMO-001"


def test_demo_tools_call_unknown_tool():
    dispatcher = DemoMCPDispatcher()
    call_req = {
        "jsonrpc": "2.0",
        "id": 99,
        "method": "tools/call",
        "params": {"name": "nonexistent_tool", "arguments": {}},
    }
    resp = dispatcher.dispatch(call_req)
    assert resp["error"]["code"] == -32601
    assert "not found" in resp["error"]["message"]


def test_demo_dispatcher_unknown_method():
    dispatcher = DemoMCPDispatcher()
    call_req = {
        "jsonrpc": "2.0",
        "id": 100,
        "method": "custom/nonexistent",
        "params": {},
    }
    resp = dispatcher.dispatch(call_req)
    assert resp["error"]["code"] == -32601
    assert "not found" in resp["error"]["message"]


# ─── 4. SSE Transport & Session Handling ───────────────────────────────────────


def test_demo_sse_connection_and_endpoint_event():
    """Verify that GET /sse returns text/event-stream and advertises the endpoint URL."""
    app = create_demo_app()

    async def _run():
        scope = {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "path": "/sse",
            "raw_path": b"/sse",
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

        combined_body = b"".join(body_chunks).decode("utf-8")
        assert "event: endpoint" in combined_body
        assert "/messages?session_id=" in combined_body

    asyncio.run(_run())


def test_demo_messages_post_and_sse_delivery():
    """Verify that sending a message via POST /messages delivers the response to the active SSE session."""
    app = create_demo_app()

    async def _run():
        from mcp_demo.server import _ACTIVE_SESSIONS

        # Setup an active session queue
        test_session_id = str(uuid.uuid4())
        session_queue: asyncio.Queue = asyncio.Queue()
        _ACTIVE_SESSIONS[test_session_id] = session_queue

        try:
            # Post a JSON-RPC request to /messages?session_id=...
            with TestClient(app) as client:
                rpc_request = {
                    "jsonrpc": "2.0",
                    "id": "req-123",
                    "method": "tools/list",
                }
                post_resp = client.post(
                    f"/messages?session_id={test_session_id}",
                    json=rpc_request,
                )
                assert post_resp.status_code == 202
                assert post_resp.json()["status"] == "accepted"

            # Check that the response was queued to the SSE session
            queued_response = session_queue.get_nowait()
            assert queued_response["jsonrpc"] == "2.0"
            assert queued_response["id"] == "req-123"
            assert "tools" in queued_response["result"]
        finally:
            _ACTIVE_SESSIONS.pop(test_session_id, None)

    asyncio.run(_run())


# ─── 5. Error Handling & Edge Cases ───────────────────────────────────────────


def test_demo_messages_missing_session_id():
    """Verify that POST /messages without session_id parameter returns 400 Bad Request."""
    app = create_demo_app()
    with TestClient(app) as client:
        resp = client.post("/messages", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert resp.status_code == 400
        assert resp.json()["error"] == "bad_request"


def test_demo_messages_invalid_session_id():
    """Verify that POST /messages with unknown session_id returns 404 Session Not Found."""
    app = create_demo_app()
    with TestClient(app) as client:
        resp = client.post(
            "/messages?session_id=nonexistent-session-uuid",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        )
        assert resp.status_code == 404
        assert resp.json()["error"] == "session_not_found"


def test_demo_messages_malformed_json():
    """Verify that POST /messages with malformed JSON body returns 400 Parse Error."""
    app = create_demo_app()

    async def _run():
        from mcp_demo.server import _ACTIVE_SESSIONS

        test_session_id = str(uuid.uuid4())
        _ACTIVE_SESSIONS[test_session_id] = asyncio.Queue()

        try:
            with TestClient(app) as client:
                resp = client.post(
                    f"/messages?session_id={test_session_id}",
                    content=b"{not valid json",
                    headers={"Content-Type": "application/json"},
                )
                assert resp.status_code == 400
                assert resp.json()["error"]["code"] == -32700
        finally:
            _ACTIVE_SESSIONS.pop(test_session_id, None)

    asyncio.run(_run())
