"""Model Context Protocol (MCP) Server for Zoho Recruit.

Supports both:
  1. Standard I/O (stdio) transport: For secure local CLI agents and subprocesses.
  2. Server-Sent Events (SSE) transport: For remote HTTP agents conforming to MCP specification.

Tenant Scoping:
  The authorized organization is resolved strictly via the server-side
  `CY_MCP_TENANT_ID` environment variable. Caller-supplied tenant IDs are rejected.
"""
from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import os
import sys
import uuid
from typing import Any, AsyncGenerator, Dict, Optional

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from app.core.logging import get_logger
from mcp_server.zoho_recruit import ZohoRecruitMCPHandler

logger = get_logger(__name__)

# Protocol version
MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "zoho-recruit-mcp"
SERVER_VERSION = "1.0.0"

# Tool specifications conform to Model Context Protocol JSON Schema
MCP_TOOLS = [
    {
        "name": "list_job_openings",
        "description": (
            "Retrieve paginated job openings from Zoho Recruit for the configured organization. "
            "Supports optional status filtering. Read-only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "page": {
                    "type": "integer",
                    "description": "1-indexed page number (default 1).",
                    "minimum": 1,
                    "default": 1,
                },
                "per_page": {
                    "type": "integer",
                    "description": "Number of records per page (default 20, max 100).",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 20,
                },
                "status": {
                    "type": "string",
                    "description": "Optional status filter (e.g., 'In-progress', 'Filled', 'Cancelled').",
                },
            },
        },
    },
    {
        "name": "list_candidates",
        "description": (
            "Retrieve paginated candidates from Zoho Recruit with privacy minimization. "
            "Excludes contact details and salary numbers, returning safe recruitment attributes "
            "for channel yield analysis. Read-only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "page": {
                    "type": "integer",
                    "description": "1-indexed page number (default 1).",
                    "minimum": 1,
                    "default": 1,
                },
                "per_page": {
                    "type": "integer",
                    "description": "Number of records per page (default 20, max 100).",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 20,
                },
                "source": {
                    "type": "string",
                    "description": "Optional acquisition source channel filter (e.g., 'LinkedIn', 'Referral').",
                },
            },
        },
    },
    {
        "name": "list_applications",
        "description": (
            "Retrieve paginated job applications from Zoho Recruit for pipeline and yield analysis. Read-only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "page": {
                    "type": "integer",
                    "description": "1-indexed page number (default 1).",
                    "minimum": 1,
                    "default": 1,
                },
                "per_page": {
                    "type": "integer",
                    "description": "Number of records per page (default 20, max 100).",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 20,
                },
                "job_opening_id": {
                    "type": "string",
                    "description": "Optional job opening ID filter.",
                },
                "status": {
                    "type": "string",
                    "description": "Optional application status or stage filter.",
                },
            },
        },
    },
]


class MCPServerDispatcher:
    """Dispatches incoming JSON-RPC 2.0 messages to read-only Zoho MCP tools."""

    def __init__(self, handler: Optional[ZohoRecruitMCPHandler] = None) -> None:
        self.handler = handler or ZohoRecruitMCPHandler()

    def dispatch(self, request: dict[str, Any]) -> Optional[dict[str, Any]]:
        """Process a JSON-RPC 2.0 request and return the JSON-RPC response."""
        req_id = request.get("id")
        method = request.get("method")
        params = request.get("params") or {}

        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {
                        "name": SERVER_NAME,
                        "version": SERVER_VERSION,
                    },
                },
            }

        if method == "notifications/initialized":
            # Notifications require no response
            return None

        if method == "ping":
            return {"jsonrpc": "2.0", "id": req_id, "result": {}}

        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"tools": MCP_TOOLS},
            }

        if method == "tools/call":
            tool_name = params.get("name")
            tool_args = params.get("arguments") or {}

            if tool_name == "list_job_openings":
                result_data = self.handler.list_job_openings(
                    page=tool_args.get("page", 1),
                    per_page=tool_args.get("per_page", 20),
                    status=tool_args.get("status"),
                )
            elif tool_name == "list_candidates":
                result_data = self.handler.list_candidates(
                    page=tool_args.get("page", 1),
                    per_page=tool_args.get("per_page", 20),
                    source=tool_args.get("source"),
                )
            elif tool_name == "list_applications":
                result_data = self.handler.list_applications(
                    page=tool_args.get("page", 1),
                    per_page=tool_args.get("per_page", 20),
                    job_opening_id=tool_args.get("job_opening_id"),
                    status=tool_args.get("status"),
                )
            else:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {
                        "code": -32601,
                        "message": f"Tool '{tool_name}' not found.",
                    },
                }

            is_error = "error" in result_data
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(result_data, indent=2),
                        }
                    ],
                    "isError": is_error,
                },
            }

        # Unknown method
        if req_id is not None:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": -32601,
                    "message": f"Method '{method}' not found.",
                },
            }
        return None


# ─── Stdio Transport ──────────────────────────────────────────────────────────


def run_stdio(dispatcher: Optional[MCPServerDispatcher] = None) -> None:
    """Run standard I/O (stdio) JSON-RPC loop for local agents and CLI tools."""
    disp = dispatcher or MCPServerDispatcher()
    logger.info("mcp.stdio_server_started", tenant_id=disp.handler.tenant_id)

    for line in sys.stdin:
        line_clean = line.strip()
        if not line_clean:
            continue
        try:
            req = json.loads(line_clean)
            resp = disp.dispatch(req)
            if resp is not None:
                sys.stdout.write(json.dumps(resp) + "\n")
                sys.stdout.flush()
        except json.JSONDecodeError:
            err_resp = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Parse error: invalid JSON."},
            }
            sys.stdout.write(json.dumps(err_resp) + "\n")
            sys.stdout.flush()


# ─── HTTP / SSE Transport ─────────────────────────────────────────────────────

# Active SSE sessions tracking
_ACTIVE_SESSIONS: Dict[str, asyncio.Queue] = {}


async def health_check(_request: Request) -> JSONResponse:
    """Readiness probe returning service status without exposing credentials or tenant details."""
    tenant_configured = bool(os.getenv("CY_MCP_TENANT_ID"))
    auth_configured = bool(os.getenv("CY_MCP_SECRET_PATH_KEY", "").strip()) or bool(os.getenv("CY_MCP_AUTH_TOKEN", "").strip())
    return JSONResponse({
        "status": "ok",
        "service": SERVER_NAME,
        "version": SERVER_VERSION,
        "tenant_configured": tenant_configured,
        "auth_configured": auth_configured,
    })


def _verify_path_key(secret_key: str) -> bool:
    """Verify high-entropy secret path key using constant-time comparison."""
    expected_key = os.getenv("CY_MCP_SECRET_PATH_KEY", "").strip()
    if not expected_key or not secret_key:
        return False
    return hmac.compare_digest(secret_key, expected_key)


def _verify_bearer_token(request: Request) -> bool:
    """Verify pre-shared Bearer or X-MCP-Token authentication header."""
    expected_token = os.getenv("CY_MCP_AUTH_TOKEN", "").strip()
    if not expected_token:
        return False

    auth_header = request.headers.get("Authorization", "").strip()
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
        return hmac.compare_digest(token, expected_token)

    custom_header = request.headers.get("X-MCP-Token", "").strip()
    if custom_header:
        return hmac.compare_digest(custom_header, expected_token)

    return False


def _create_sse_response(endpoint_data: str) -> StreamingResponse:
    """Establish SSE session and stream MCP events to client."""
    session_id = str(uuid.uuid4())
    queue: asyncio.Queue = asyncio.Queue()
    _ACTIVE_SESSIONS[session_id] = queue

    async def event_generator() -> AsyncGenerator[bytes, None]:
        # Initial MCP endpoint event
        yield f"event: endpoint\r\ndata: {endpoint_data}?session_id={session_id}\r\n\r\n".encode("utf-8")
        try:
            while True:
                # Wait for messages or ping keepalive
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


async def sse_endpoint(request: Request) -> Response:
    """Standard Server-Sent Events endpoint establishing an MCP session with Bearer token."""
    if not _verify_bearer_token(request):
        return JSONResponse(
            {"error": "unauthorized", "message": "Authentication required for MCP SSE endpoint."},
            status_code=401,
        )
    return _create_sse_response(endpoint_data="/messages")


async def sse_capability_endpoint(request: Request) -> Response:
    """Capability Server-Sent Events endpoint establishing an MCP session with secret path key."""
    secret_key = request.path_params.get("secret_key", "").strip()
    if not _verify_path_key(secret_key):
        return JSONResponse(
            {"error": "unauthorized", "message": "Invalid or missing capability key."},
            status_code=401,
        )
    return _create_sse_response(endpoint_data=f"/messages/{secret_key}")


async def _dispatch_incoming_message(request: Request) -> JSONResponse:
    """Parse JSON-RPC payload and route to active SSE session or return direct response."""
    session_id = request.query_params.get("session_id")
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid JSON payload"}},
            status_code=400,
        )

    dispatcher = getattr(request.app.state, "dispatcher", None) or MCPServerDispatcher()
    response_payload = dispatcher.dispatch(body)

    if session_id and session_id in _ACTIVE_SESSIONS:
        if response_payload is not None:
            await _ACTIVE_SESSIONS[session_id].put(response_payload)
        return JSONResponse({"status": "accepted"}, status_code=202)

    # Direct JSON response fallback if no active SSE session
    if response_payload is not None:
        return JSONResponse(response_payload)
    return JSONResponse({"status": "accepted"}, status_code=202)


async def messages_endpoint(request: Request) -> JSONResponse:
    """Standard message endpoint requiring Bearer token."""
    if not _verify_bearer_token(request):
        return JSONResponse(
            {"error": "unauthorized", "message": "Authentication required for MCP message endpoint."},
            status_code=401,
        )
    return await _dispatch_incoming_message(request)


async def messages_capability_endpoint(request: Request) -> JSONResponse:
    """Capability message endpoint requiring secret path key."""
    secret_key = request.path_params.get("secret_key", "").strip()
    if not _verify_path_key(secret_key):
        return JSONResponse(
            {"error": "unauthorized", "message": "Invalid or missing capability key."},
            status_code=401,
        )
    return await _dispatch_incoming_message(request)


def create_mcp_app(dispatcher: Optional[MCPServerDispatcher] = None) -> Starlette:
    """Factory creating the ASGI application for the MCP service.

    Fails closed in production if neither CY_MCP_SECRET_PATH_KEY nor CY_MCP_AUTH_TOKEN
    is configured.
    """
    is_prod = os.getenv("CY_ENV", "").strip().lower() == "production"
    has_path_key = bool(os.getenv("CY_MCP_SECRET_PATH_KEY", "").strip())
    has_auth_token = bool(os.getenv("CY_MCP_AUTH_TOKEN", "").strip())

    if is_prod and not (has_path_key or has_auth_token):
        raise ValueError(
            "Production MCP server refuses to start without an authentication mechanism configured. "
            "Set CY_MCP_SECRET_PATH_KEY or CY_MCP_AUTH_TOKEN."
        )

    routes = [
        Route("/health", health_check, methods=["GET"]),
        Route("/ready", health_check, methods=["GET"]),
        Route("/sse", sse_endpoint, methods=["GET"]),
        Route("/sse/{secret_key}", sse_capability_endpoint, methods=["GET"]),
        Route("/messages", messages_endpoint, methods=["POST"]),
        Route("/messages/{secret_key}", messages_capability_endpoint, methods=["POST"]),
    ]
    app = Starlette(routes=routes)
    app.state.dispatcher = dispatcher or MCPServerDispatcher()
    return app


# ─── CLI Entrypoint ───────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Zoho Recruit Model Context Protocol (MCP) Server"
    )
    parser.add_argument(
        "--stdio",
        action="store_true",
        help="Run using standard I/O (stdio) transport for local agents and subprocesses.",
    )
    parser.add_argument(
        "--host",
        default=os.getenv("CY_MCP_HOST", "0.0.0.0"),
        help="Bind host for SSE HTTP transport (default 0.0.0.0).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("PORT", os.getenv("CY_MCP_PORT", "8001"))),
        help="Bind port for SSE HTTP transport (default 8001 or $PORT on Render).",
    )
    args = parser.parse_args()

    if args.stdio:
        run_stdio()
    else:
        import uvicorn
        app = create_mcp_app()
        uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
