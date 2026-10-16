"""Model Context Protocol (MCP) Server for Zoho Recruit.

Supports both:
  1. Standard I/O (stdio) transport: For secure local CLI agents and subprocesses.
  2. Server-Sent Events (SSE) transport: For remote HTTP agents conforming to MCP specification.

Tenant Scoping:
  The authorized organization is resolved strictly from cryptographically signed,
  customer-specific connection tokens (`cyct_...`) bound to the authenticated MCP session.
  Caller-supplied tenant IDs cannot override verified credentials.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import os
import sys
import time
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


def _is_production() -> bool:
    env = os.getenv("CY_ENV", "").strip().lower()
    return env in ("production", "prod")


def _get_signing_secret() -> str:
    """Resolve trusted server-side secret key for cryptographic token signing.

    In production, CY_MCP_SIGNING_SECRET or CY_SECRET_KEY is mandatory.
    Hard-coded fallback secrets are strictly prohibited in production.
    """
    secret = (
        os.getenv("CY_MCP_SIGNING_SECRET", "").strip()
        or os.getenv("CY_SECRET_KEY", "").strip()
    )
    if secret:
        return secret

    if _is_production():
        raise ValueError(
            "Production security failure: CY_MCP_SIGNING_SECRET or CY_SECRET_KEY is required. "
            "The production MCP server refuses to sign or verify tokens with unconfigured keys."
        )

    # Non-production / test fallback ONLY
    dev_secret = os.getenv("CY_MCP_SECRET_PATH_KEY", "").strip() or os.getenv("CY_ENCRYPTION_KEY", "").strip()
    return dev_secret or "dev-internal-signing-secret-not-for-production"


MCP_TOKEN_ISSUER = "channel-yield-system"
MCP_TOKEN_AUDIENCE = "zoho-recruit-mcp"
DEFAULT_EXPIRY_HOURS = 24


def create_user_connection_token(
    tenant_id: str,
    user_id: str,
    secret: Optional[str] = None,
    expire_hours: int = DEFAULT_EXPIRY_HOURS,
    expire_days: Optional[int] = None,
) -> str:
    """Generate a tamper-proof HMAC-SHA256 connection token binding an MCP session to a user's tenant."""
    sec = secret or _get_signing_secret()
    now = int(time.time())
    duration_seconds = (expire_days * 86400) if expire_days is not None else (expire_hours * 3600)
    payload = {
        "tid": str(tenant_id).strip(),
        "sub": str(user_id).strip(),
        "iss": MCP_TOKEN_ISSUER,
        "aud": MCP_TOKEN_AUDIENCE,
        "iat": now,
        "exp": now + duration_seconds,
    }
    payload_b64 = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("utf-8").rstrip("=")
    sig = hmac.new(sec.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"cyct_{payload_b64}.{sig}"


def verify_user_connection_token(
    token: str,
    secret: Optional[str] = None,
    expected_audience: str = MCP_TOKEN_AUDIENCE,
    expected_issuer: str = MCP_TOKEN_ISSUER,
) -> Optional[dict[str, Any]]:
    """Verify cryptographic signature and expiry of a user connection token.

    Returns the decoded payload dict if valid, or None if invalid, expired, or tampered.
    """
    if not token or not isinstance(token, str) or not token.startswith("cyct_") or "." not in token:
        return None
    try:
        sec = secret or _get_signing_secret()
    except ValueError:
        return None
    raw = token[5:]
    try:
        payload_b64, sig = raw.rsplit(".", 1)
        expected_sig = hmac.new(sec.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            return None
        padding = "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode((payload_b64 + padding).encode("utf-8")))
        if payload.get("exp", 0) < time.time():
            return None
        if not payload.get("tid"):
            return None
        if "aud" in payload and payload["aud"] != expected_audience:
            return None
        if "iss" in payload and payload["iss"] != expected_issuer:
            return None
        return payload
    except Exception:
        return None


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
    {
        "name": "check_zoho_connection",
        "description": (
            "Verify whether this user's organization has an active Zoho Recruit connection. "
            "Returns connection status and onboarding URL if not connected."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "connection_token": {
                    "type": "string",
                    "description": "Optional cryptographically signed user connection token for per-user tenant scoping.",
                },
            },
        },
    },
    {
        "name": "compute_channel_yield",
        "description": (
            "Compute recruitment channel yield, mature hire conversion rates, and evidence-based recommendations "
            "directly from live Zoho Recruit data. Operates entirely in memory without persisting recruitment records."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "cohort_start": {
                    "type": "string",
                    "description": "ISO start date of application cohort (YYYY-MM-DD), e.g., '2026-01-01'.",
                },
                "cohort_end": {
                    "type": "string",
                    "description": "ISO end date of application cohort (YYYY-MM-DD), e.g., '2026-06-30'.",
                },
                "as_of_date": {
                    "type": "string",
                    "description": "Optional calculation as-of date (default: today).",
                },
                "observation_days": {
                    "type": "integer",
                    "description": "Observation days for outcome maturity (default 90).",
                    "default": 90,
                    "minimum": 0,
                },
                "requisition_id": {
                    "type": "string",
                    "description": "Optional requisition identifier filter.",
                },
                "min_sample_size": {
                    "type": "integer",
                    "description": "Minimum sample size for ranking eligibility (default 5).",
                    "default": 5,
                    "minimum": 1,
                },
                "connection_token": {
                    "type": "string",
                    "description": "Optional cryptographically signed user connection token for per-user tenant scoping.",
                },
            },
            "required": ["cohort_start", "cohort_end"],
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

            # Cryptographically verify per-user connection token if provided in tool arguments
            target_handler = self.handler
            conn_token = tool_args.get("connection_token")
            if conn_token:
                verified = verify_user_connection_token(str(conn_token).strip())
                if not verified:
                    return {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": json.dumps({
                                        "status": "error",
                                        "error": "unauthorized",
                                        "message": "Invalid, expired, or tampered connection token."
                                    }, indent=2)
                                }
                            ],
                            "isError": True,
                        }
                    }
                # Check for mismatch against handler's pre-bound tenant if already bound
                if self.handler.tenant_id and verified["tid"] != self.handler.tenant_id:
                    return {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": json.dumps({
                                        "status": "error",
                                        "error": "tenant_mismatch",
                                        "message": "Provided connection token does not match the bound session tenant."
                                    }, indent=2)
                                }
                            ],
                            "isError": True,
                        }
                    }
                target_handler = ZohoRecruitMCPHandler(tenant_id=verified["tid"])
            elif not target_handler.tenant_id:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps({
                                    "status": "error",
                                    "error": "unauthorized",
                                    "message": "No tenant is bound to this request or session. Please authenticate with a valid customer connection token."
                                }, indent=2)
                            }
                        ],
                        "isError": True,
                    }
                }

            if tool_name == "check_zoho_connection":
                result_data = target_handler.check_zoho_connection()
            elif tool_name == "compute_channel_yield":
                result_data = target_handler.compute_channel_yield(
                    cohort_start=str(tool_args.get("cohort_start", "2026-01-01")),
                    cohort_end=str(tool_args.get("cohort_end", "2026-06-30")),
                    as_of_date=tool_args.get("as_of_date"),
                    observation_days=int(tool_args.get("observation_days", 90)),
                    requisition_id=tool_args.get("requisition_id"),
                    min_sample_size=int(tool_args.get("min_sample_size", 5)),
                )
            elif tool_name == "list_job_openings":
                result_data = target_handler.list_job_openings(
                    page=tool_args.get("page", 1),
                    per_page=tool_args.get("per_page", 20),
                    status=tool_args.get("status"),
                )
            elif tool_name == "list_candidates":
                result_data = target_handler.list_candidates(
                    page=tool_args.get("page", 1),
                    per_page=tool_args.get("per_page", 20),
                    source=tool_args.get("source"),
                )
            elif tool_name == "list_applications":
                result_data = target_handler.list_applications(
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

            is_error = "error" in result_data or result_data.get("status") == "error"
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
    """Liveness probe returning service status without exposing credentials or tenant details."""
    return JSONResponse({
        "status": "ok",
        "service": SERVER_NAME,
        "version": SERVER_VERSION,
    })


async def ready_check(_request: Request) -> JSONResponse:
    """Readiness probe checking production configuration and cryptographic signing readiness."""
    is_prod = _is_production()
    has_signing_secret = bool(os.getenv("CY_MCP_SIGNING_SECRET", "").strip() or os.getenv("CY_SECRET_KEY", "").strip())
    has_path_key = bool(os.getenv("CY_MCP_SECRET_PATH_KEY", "").strip())
    has_auth_token = bool(os.getenv("CY_MCP_AUTH_TOKEN", "").strip())

    is_ready = True
    if is_prod and not (has_signing_secret or has_path_key or has_auth_token):
        is_ready = False

    status_code = 200 if is_ready else 503
    return JSONResponse({
        "status": "ready" if is_ready else "not_ready",
        "service": SERVER_NAME,
        "version": SERVER_VERSION,
        "multi_tenant_ready": has_signing_secret,
        "auth_configured": has_signing_secret or has_path_key or has_auth_token,
    }, status_code=status_code)


def _get_session_queue(session_id: str) -> Optional[asyncio.Queue]:
    entry = _ACTIVE_SESSIONS.get(session_id)
    if isinstance(entry, asyncio.Queue):
        return entry
    if isinstance(entry, dict):
        return entry.get("queue")
    return None


def _get_session_tenant_id(session_id: str) -> Optional[str]:
    entry = _ACTIVE_SESSIONS.get(session_id)
    if isinstance(entry, dict):
        return entry.get("tenant_id")
    return None


def _verify_path_key(secret_key: str) -> bool:
    """Verify secret path key: either customer connection token or static path secret."""
    if not secret_key:
        return False
    # 1. Customer-specific connection token
    if verify_user_connection_token(secret_key) is not None:
        return True
    # 2. Static path secret (for operational health/admin if configured)
    expected_key = os.getenv("CY_MCP_SECRET_PATH_KEY", "").strip()
    if expected_key and hmac.compare_digest(secret_key, expected_key):
        return True
    return False


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


def _create_sse_response(endpoint_data: str, tenant_id: Optional[str] = None, user_id: Optional[str] = None) -> StreamingResponse:
    """Establish SSE session and stream MCP events to client with bound tenant context."""
    session_id = str(uuid.uuid4())
    queue: asyncio.Queue = asyncio.Queue()
    _ACTIVE_SESSIONS[session_id] = {
        "queue": queue,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "created_at": time.time(),
    }

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
    verified = verify_user_connection_token(secret_key)
    tenant_id = verified["tid"] if verified else None
    user_id = verified.get("sub") if verified else None
    return _create_sse_response(endpoint_data=f"/messages/{secret_key}", tenant_id=tenant_id, user_id=user_id)


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

    is_tool_call = isinstance(body, dict) and body.get("method") == "tools/call"

    # Validate session_id if provided
    if session_id:
        session_entry = _ACTIVE_SESSIONS.get(session_id)
        if not session_entry or not isinstance(session_entry, dict):
            return JSONResponse(
                {"jsonrpc": "2.0", "id": body.get("id") if isinstance(body, dict) else None,
                 "error": {"code": -32002, "message": "Session not found or expired."}},
                status_code=404,
            )
        session_tenant_id = session_entry.get("tenant_id")
    else:
        session_entry = None
        session_tenant_id = None

    # Check capability path parameter token if present
    url_secret_key = request.path_params.get("secret_key", "").strip()
    if url_secret_key and url_secret_key.startswith("cyct_"):
        verified_token = verify_user_connection_token(url_secret_key)
        if not verified_token:
            return JSONResponse(
                {"jsonrpc": "2.0", "id": body.get("id") if isinstance(body, dict) else None,
                 "error": {"code": -32004, "message": "Invalid or expired connection token in message path."}},
                status_code=401,
            )
        token_tenant_id = verified_token["tid"]
        if session_tenant_id and session_tenant_id != token_tenant_id:
            return JSONResponse(
                {"jsonrpc": "2.0", "id": body.get("id") if isinstance(body, dict) else None,
                 "error": {"code": -32005, "message": "Session tenant does not match credential tenant."}},
                status_code=403,
            )
        if not session_tenant_id:
            session_tenant_id = token_tenant_id

    # For tool executions, require an authenticated tenant bound to session, token, or argument
    if is_tool_call:
        tool_args = body.get("params", {}).get("arguments", {}) if isinstance(body, dict) else {}
        has_arg_token = bool(tool_args.get("connection_token"))
        if not session_tenant_id and not has_arg_token:
            return JSONResponse(
                {"jsonrpc": "2.0", "id": body.get("id"),
                 "error": {"code": -32003, "message": "Authentication required: no authorized tenant bound to this session."}},
                status_code=401,
            )

    if session_tenant_id:
        dispatcher = MCPServerDispatcher(handler=ZohoRecruitMCPHandler(tenant_id=session_tenant_id))
    else:
        dispatcher = MCPServerDispatcher(handler=ZohoRecruitMCPHandler(tenant_id=None))

    response_payload = dispatcher.dispatch(body)

    sess_queue = session_entry.get("queue") if session_entry else None
    if sess_queue is not None:
        if response_payload is not None:
            await sess_queue.put(response_payload)
        return JSONResponse({"status": "accepted"}, status_code=202)

    # Direct JSON response fallback if no active SSE session queue
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

    Fails closed in production if neither signing secret, capability path key,
    nor bearer auth token is configured.
    """
    is_prod = _is_production()
    has_signing_secret = bool(os.getenv("CY_MCP_SIGNING_SECRET", "").strip() or os.getenv("CY_SECRET_KEY", "").strip())
    has_path_key = bool(os.getenv("CY_MCP_SECRET_PATH_KEY", "").strip())
    has_auth_token = bool(os.getenv("CY_MCP_AUTH_TOKEN", "").strip())

    if is_prod and not (has_signing_secret or has_path_key or has_auth_token):
        raise ValueError(
            "Production MCP server refuses to start without an authentication mechanism configured. "
            "Set CY_MCP_SIGNING_SECRET, CY_SECRET_KEY, CY_MCP_SECRET_PATH_KEY, or CY_MCP_AUTH_TOKEN."
        )

    routes = [
        Route("/health", health_check, methods=["GET"]),
        Route("/ready", ready_check, methods=["GET"]),
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
