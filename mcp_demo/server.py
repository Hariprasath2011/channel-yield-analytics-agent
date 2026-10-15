"""Temporary Demo Model Context Protocol (MCP) Server for Zoho Recruit.

Pure standalone demo implementation serving fictional mock data over SSE transport.
Does NOT access real Zoho APIs, database, encryption keys, or tenant credentials.
Intended exclusively for testing iGentic MCP SSE connectivity.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from typing import Any, AsyncGenerator, Dict, List, Optional

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

SERVER_NAME = "zoho-recruit-mcp-demo"
SERVER_VERSION = "1.0.0"
PROTOCOL_VERSION = "2024-11-05"

# ─── Fictional Demo Datasets ──────────────────────────────────────────────────

FICTIONAL_JOB_OPENINGS: List[Dict[str, Any]] = [
    {
        "id": "JOB-DEMO-001",
        "title": "Senior Cloud Platform Engineer (Demo)",
        "department": "Infrastructure & Cloud",
        "status": "In-Progress",
        "openings_count": 2,
        "city": "Austin, TX",
        "source": "Fictional Demo Data",
    },
    {
        "id": "JOB-DEMO-002",
        "title": "Lead Talent Intelligence Analyst (Demo)",
        "department": "People Analytics",
        "status": "In-Progress",
        "openings_count": 1,
        "city": "Remote",
        "source": "Fictional Demo Data",
    },
    {
        "id": "JOB-DEMO-003",
        "title": "Frontend Systems Architect (Demo)",
        "department": "Product Engineering",
        "status": "Draft",
        "openings_count": 1,
        "city": "New York, NY",
        "source": "Fictional Demo Data",
    },
]

FICTIONAL_CANDIDATES: List[Dict[str, Any]] = [
    {
        "id": "CAND-DEMO-101",
        "candidate_reference": "Demo Candidate Alpha",
        "source": "Tech Career Fair (Demo)",
        "rating": 4.5,
        "status": "Interview Scheduled",
        "created_time": "2026-03-01T09:00:00Z",
    },
    {
        "id": "CAND-DEMO-102",
        "candidate_reference": "Demo Candidate Beta",
        "source": "Employee Referral (Demo)",
        "rating": 4.8,
        "status": "Offer Extended",
        "created_time": "2026-03-02T11:30:00Z",
    },
    {
        "id": "CAND-DEMO-103",
        "candidate_reference": "Demo Candidate Gamma",
        "source": "Direct Career Portal (Demo)",
        "rating": 3.5,
        "status": "Application Under Review",
        "created_time": "2026-03-03T15:00:00Z",
    },
]

FICTIONAL_APPLICATIONS: List[Dict[str, Any]] = [
    {
        "id": "APP-DEMO-501",
        "job_opening_id": "JOB-DEMO-001",
        "job_title": "Senior Cloud Platform Engineer (Demo)",
        "candidate_id": "CAND-DEMO-101",
        "candidate_reference": "Demo Candidate Alpha",
        "status": "Technical Interview",
        "rating": 4.5,
        "source": "Tech Career Fair (Demo)",
        "applied_time": "2026-03-01T10:15:00Z",
    },
    {
        "id": "APP-DEMO-502",
        "job_opening_id": "JOB-DEMO-002",
        "job_title": "Lead Talent Intelligence Analyst (Demo)",
        "candidate_id": "CAND-DEMO-102",
        "candidate_reference": "Demo Candidate Beta",
        "status": "Offer Extended",
        "rating": 4.8,
        "source": "Employee Referral (Demo)",
        "applied_time": "2026-03-02T14:00:00Z",
    },
    {
        "id": "APP-DEMO-503",
        "job_opening_id": "JOB-DEMO-001",
        "job_title": "Senior Cloud Platform Engineer (Demo)",
        "candidate_id": "CAND-DEMO-103",
        "candidate_reference": "Demo Candidate Gamma",
        "status": "Under Review",
        "rating": 3.5,
        "source": "Direct Career Portal (Demo)",
        "applied_time": "2026-03-03T16:20:00Z",
    },
]

# ─── Tool Definitions ─────────────────────────────────────────────────────────

DEMO_TOOLS = [
    {
        "name": "list_job_openings",
        "description": "Retrieve fictional demo job openings for connectivity verification.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": "Optional filter by status (e.g. In-Progress, Draft).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum records to return (1-50, default 10).",
                    "default": 10,
                },
            },
        },
    },
    {
        "name": "list_candidates",
        "description": "Retrieve fictional demo candidate summaries (PII-minimized, no real contact info).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Maximum records to return (1-50, default 10).",
                    "default": 10,
                },
            },
        },
    },
    {
        "name": "list_applications",
        "description": "Retrieve fictional demo job applications linking candidates to openings.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_opening_id": {
                    "type": "string",
                    "description": "Optional filter by job opening identifier (e.g. JOB-DEMO-001).",
                },
                "status": {
                    "type": "string",
                    "description": "Optional filter by stage/status (e.g. Technical Interview).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum records to return (1-50, default 10).",
                    "default": 10,
                },
            },
        },
    },
]

# ─── JSON-RPC Dispatcher ──────────────────────────────────────────────────────


class DemoMCPDispatcher:
    """Handles JSON-RPC 2.0 requests for fictional demo data."""

    def dispatch(self, request_payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        rpc_id = request_payload.get("id")
        method = request_payload.get("method")
        params = request_payload.get("params", {}) or {}

        # Handle notifications (no response required)
        if method == "notifications/initialized":
            return None

        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "result": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {
                        "name": SERVER_NAME,
                        "version": SERVER_VERSION,
                    },
                },
            }

        if method == "ping":
            return {"jsonrpc": "2.0", "id": rpc_id, "result": {}}

        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "result": {"tools": DEMO_TOOLS},
            }

        if method == "tools/call":
            tool_name = params.get("name")
            tool_args = params.get("arguments", {}) or {}
            return self._handle_tool_call(rpc_id, tool_name, tool_args)

        # Unknown method
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "error": {
                "code": -32601,
                "message": f"Method '{method}' not found.",
            },
        }

    def _handle_tool_call(
        self, rpc_id: Any, tool_name: Optional[str], arguments: Dict[str, Any]
    ) -> Dict[str, Any]:
        limit = min(max(int(arguments.get("limit", 10)), 1), 50)

        if tool_name == "list_job_openings":
            status_filter = arguments.get("status")
            records = [
                rec for rec in FICTIONAL_JOB_OPENINGS
                if not status_filter or rec.get("status", "").lower() == str(status_filter).lower()
            ][:limit]
            payload = {
                "mode": "fictional-demo-data",
                "count": len(records),
                "job_openings": records,
            }
            return {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(payload, indent=2)}],
                    "isError": False,
                },
            }

        if tool_name == "list_candidates":
            records = FICTIONAL_CANDIDATES[:limit]
            payload = {
                "mode": "fictional-demo-data",
                "count": len(records),
                "candidates": records,
            }
            return {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(payload, indent=2)}],
                    "isError": False,
                },
            }

        if tool_name == "list_applications":
            job_id_filter = arguments.get("job_opening_id")
            status_filter = arguments.get("status")
            records = [
                rec for rec in FICTIONAL_APPLICATIONS
                if (not job_id_filter or rec.get("job_opening_id") == job_id_filter)
                and (not status_filter or rec.get("status", "").lower() == str(status_filter).lower())
            ][:limit]
            payload = {
                "mode": "fictional-demo-data",
                "count": len(records),
                "applications": records,
            }
            return {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(payload, indent=2)}],
                    "isError": False,
                },
            }

        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "error": {
                "code": -32601,
                "message": f"Tool '{tool_name}' not found.",
            },
        }


# ─── HTTP / SSE Transport ─────────────────────────────────────────────────────

_ACTIVE_SESSIONS: Dict[str, asyncio.Queue] = {}
dispatcher = DemoMCPDispatcher()


async def health_check(_request: Request) -> JSONResponse:
    """Readiness probe returning demo status."""
    return JSONResponse({
        "status": "ok",
        "service": SERVER_NAME,
        "version": SERVER_VERSION,
        "mode": "fictional-demo-data",
        "active_sessions": len(_ACTIVE_SESSIONS),
    })


async def sse_endpoint(_request: Request) -> Response:
    """Server-Sent Events endpoint initiating MCP session."""
    session_id = str(uuid.uuid4())
    queue: asyncio.Queue = asyncio.Queue()
    _ACTIVE_SESSIONS[session_id] = queue

    async def event_generator() -> AsyncGenerator[bytes, None]:
        # Initial MCP endpoint event
        yield f"event: endpoint\r\ndata: /messages?session_id={session_id}\r\n\r\n".encode("utf-8")
        try:
            while True:
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"event: message\r\ndata: {json.dumps(data)}\r\n\r\n".encode("utf-8")
                except asyncio.TimeoutError:
                    # Keep-alive comment
                    yield b": ping\r\n\r\n"
        finally:
            _ACTIVE_SESSIONS.pop(session_id, None)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def messages_endpoint(request: Request) -> JSONResponse:
    """Receive JSON-RPC 2.0 messages from client and forward to SSE queue or return direct."""
    session_id = request.query_params.get("session_id")
    if not session_id:
        return JSONResponse(
            {"error": "bad_request", "message": "Missing 'session_id' query parameter."},
            status_code=400,
        )

    if session_id not in _ACTIVE_SESSIONS:
        return JSONResponse(
            {"error": "session_not_found", "message": f"Session '{session_id}' not found or expired."},
            status_code=404,
        )

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error: Invalid JSON."}},
            status_code=400,
        )

    response = dispatcher.dispatch(payload)
    if response is not None:
        await _ACTIVE_SESSIONS[session_id].put(response)

    return JSONResponse({"status": "accepted"}, status_code=202)


def create_demo_app() -> Starlette:
    """Create Starlette ASGI application for the demo server."""
    return Starlette(
        debug=False,
        routes=[
            Route("/health", health_check, methods=["GET"]),
            Route("/sse", sse_endpoint, methods=["GET"]),
            Route("/messages", messages_endpoint, methods=["POST"]),
        ],
    )


# ─── Local / CLI Entrypoint ───────────────────────────────────────────────────


async def run_stdio():
    """Run JSON-RPC stdio loop for local terminal debugging."""
    loop = asyncio.get_event_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)

    while True:
        line = await reader.readline()
        if not line:
            break
        text = line.decode("utf-8").strip()
        if not text:
            continue
        try:
            req = json.loads(text)
            resp = dispatcher.dispatch(req)
            if resp is not None:
                sys.stdout.write(json.dumps(resp) + "\n")
                sys.stdout.flush()
        except Exception as e:
            err = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(e)}}
            sys.stdout.write(json.dumps(err) + "\n")
            sys.stdout.flush()


def main():
    parser = argparse.ArgumentParser(description="Zoho Recruit Demo MCP Server (Fictional Data)")
    parser.add_argument("--stdio", action="store_true", help="Run via stdio transport instead of HTTP/SSE")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "10000")), help="HTTP port (default 10000)")
    parser.add_argument("--host", default="0.0.0.0", help="HTTP bind host (default 0.0.0.0)")
    args = parser.parse_args()

    if args.stdio:
        asyncio.run(run_stdio())
    else:
        import uvicorn
        app = create_demo_app()
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
