"""Zoho Recruit integration router (/v2/integrations/zoho)."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.dependencies import AuthenticatedContext, require_role
from app.core.config import get_settings
from app.core.exceptions import NotFoundError, OAuthError, RateLimitError
from app.core.logging import get_logger
from app.integrations.zoho.client import DATACENTER_DOMAINS, ZohoRecruitClient
from app.integrations.zoho.service import ZohoService
from app.persistence.database import get_db
from mcp_server.server import create_user_connection_token

logger = get_logger(__name__)

router = APIRouter(prefix="/v2/integrations/zoho", tags=["Zoho Recruit Integration"])

_SAFE_CODE_REGEX = re.compile(r"^[a-zA-Z0-9_\-\.]{5,512}$")
_SAFE_STATE_REGEX = re.compile(r"^[a-zA-Z0-9_\-]{8,128}$")


def _render_callback_html(
    *,
    title: str,
    heading: str,
    message: str,
    is_success: bool,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    badge_bg = "rgba(16, 185, 129, 0.15)" if is_success else "rgba(244, 63, 94, 0.15)"
    badge_text = "#34d399" if is_success else "#fb7185"
    badge_label = "Success" if is_success else "Notice"
    icon_svg = (
        '<svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="#10b981" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"></path><polyline points="22 4 12 14.01 9 11.01"></polyline></svg>'
        if is_success
        else '<svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="#f43f5e" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"></circle><line x1="12" y1="8" x2="12" y2="12"></line><line x1="12" y1="16" x2="12.01" y2="16"></line></svg>'
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title} — Channel Yield Analytics</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background-color: #0b0f19;
      color: #f1f5f9;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 1.5rem;
    }}
    .card {{
      background: #1e293b;
      border: 1px solid #334155;
      border-radius: 1rem;
      padding: 2.5rem 2rem;
      max-width: 480px;
      width: 100%;
      text-align: center;
      box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.5), 0 8px 10px -6px rgba(0, 0, 0, 0.4);
    }}
    .icon-wrapper {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 80px;
      height: 80px;
      border-radius: 50%;
      background: {badge_bg};
      margin-bottom: 1.5rem;
    }}
    .badge {{
      display: inline-block;
      font-size: 0.75rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      padding: 0.25rem 0.75rem;
      border-radius: 9999px;
      background: {badge_bg};
      color: {badge_text};
      margin-bottom: 0.75rem;
    }}
    h1 {{
      font-size: 1.5rem;
      font-weight: 700;
      letter-spacing: -0.025em;
      margin-bottom: 0.75rem;
      color: #ffffff;
    }}
    p {{
      font-size: 0.95rem;
      line-height: 1.6;
      color: #94a3b8;
      margin-bottom: 1.75rem;
    }}
    .footer-note {{
      font-size: 0.8rem;
      color: #64748b;
      border-top: 1px solid #334155;
      padding-top: 1.25rem;
    }}
  </style>
</head>
<body>
  <main class="card">
    <div class="icon-wrapper">
      {icon_svg}
    </div>
    <div>
      <span class="badge">{badge_label}</span>
      <h1>{heading}</h1>
      <p>{message}</p>
    </div>
    <div class="footer-note">
      Channel Yield Analytics Agent &bull; Zoho Recruit Integration
    </div>
  </main>
</body>
</html>"""

    response = HTMLResponse(content=html, status_code=status_code)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'"
    return response


class AuthorizeResponse(BaseModel):
    authorization_url: str
    state: str


class CallbackRequest(BaseModel):
    state: str = Field(..., description="CSRF state token")
    code: str = Field(..., description="Zoho authorization code")


class IntegrationStatusResponse(BaseModel):
    status: str
    data_center: str
    zoho_org_id: Optional[str] = None
    last_sync_at: Optional[str] = None
    last_sync_status: Optional[str] = None
    is_connected: bool


class MCPConnectionTokenResponse(BaseModel):
    connection_token: str = Field(..., description="Cryptographically signed HMAC-SHA256 connection token")
    token_type: str = Field("Bearer", description="Token authorization type")
    expires_in_hours: int = Field(24, description="Token validity window in hours")
    expires_in_days: int = Field(1, description="Token validity window in days")
    expires_at: str = Field(..., description="ISO 8601 UTC timestamp when token expires")
    tenant_id: str = Field(..., description="Authorized tenant identifier")
    zoho_connected: bool = Field(True, description="Whether Zoho integration is active")
    zoho_data_center: Optional[str] = Field(None, description="Connected Zoho data center")
    mcp_endpoint_suggestion: str = Field(..., description="Suggested MCP SSE capability path for iGentic")


VALID_DATA_CENTERS = set(DATACENTER_DOMAINS.keys())


def _get_zoho_client(data_center: Optional[str] = None) -> ZohoRecruitClient:
    settings = get_settings()
    dc = (data_center or settings.zoho_data_center or "com").lower().strip()
    if dc not in VALID_DATA_CENTERS:
        raise OAuthError("Zoho OAuth configuration has an invalid data center.")

    client_id = (settings.zoho_client_id or "").strip()
    client_secret = (settings.zoho_client_secret or "").strip()
    redirect_uri = (settings.zoho_redirect_uri or "").strip()

    if settings.is_production:
        missing = []
        if not client_id:
            missing.append("client ID")
        if not client_secret:
            missing.append("client secret")
        if not redirect_uri:
            missing.append("redirect URI")
        if missing:
            raise OAuthError(
                "Zoho OAuth is not configured for production. "
                f"Missing required setting(s): {', '.join(missing)}."
            )
        return ZohoRecruitClient(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            data_center=dc,
        )

    return ZohoRecruitClient(
        client_id=client_id or "dummy_client_id",
        client_secret=client_secret or "dummy_client_secret",
        redirect_uri=redirect_uri or "https://api.example.com/v2/integrations/zoho/callback",
        data_center=dc,
    )


@router.get(
    "/status",
    summary="Get Zoho Recruit connection status for caller's organization",
    response_model=IntegrationStatusResponse,
)
def get_status(
    ctx: AuthenticatedContext = Depends(require_role("viewer")),
    db: Session = Depends(get_db),
) -> IntegrationStatusResponse:
    """Retrieve connection status without exposing tokens or credentials."""
    integration = ZohoService.get_integration(db, ctx.tenant_id)
    if not integration:
        return IntegrationStatusResponse(
            status="pending_auth",
            data_center="com",
            is_connected=False,
        )
    return IntegrationStatusResponse(
        status=integration.status,
        data_center=integration.data_center,
        zoho_org_id=integration.zoho_org_id,
        last_sync_at=integration.last_sync_at.isoformat() if integration.last_sync_at else None,
        last_sync_status=integration.last_sync_status,
        is_connected=integration.status == "active",
    )


@router.post(
    "/mcp-token",
    summary="Issue a signed per-user MCP connection token for iGentic",
    response_model=MCPConnectionTokenResponse,
)
@router.get(
    "/mcp-token",
    summary="Retrieve a signed per-user MCP connection token for iGentic",
    response_model=MCPConnectionTokenResponse,
)
def issue_mcp_connection_token(
    expire_hours: int = Query(24, ge=1, le=168, description="Token validity in hours (default 24h, max 168h / 7 days)"),
    ctx: AuthenticatedContext = Depends(require_role("analyst")),
    db: Session = Depends(get_db),
) -> MCPConnectionTokenResponse:
    """Issue a cryptographically signed MCP connection token for the authenticated user and tenant.

    Security & Privacy Guarantees:
      - Derives tenant and user strictly from authenticated session (JWT or API key).
      - Caller-supplied user_id or tenant_id parameters are rejected (not accepted).
      - Verifies active Zoho integration before issuing.
      - Signs payload with server secret using HMAC-SHA256 (configurable short-lived expiry, default 24h).
      - Never returns OAuth tokens, client secrets, or database credentials.
    """
    integration = ZohoService.get_integration(db, ctx.tenant_id)
    if not integration or integration.status != "active":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "integration_not_active",
                "message": (
                    f"No active Zoho Recruit integration found for tenant '{ctx.tenant_id}'. "
                    "Please complete Zoho authorization before connecting iGentic."
                ),
            },
        )

    token = create_user_connection_token(
        tenant_id=ctx.tenant_id,
        user_id=ctx.user_id,
        expire_hours=expire_hours,
    )
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=expire_hours)).isoformat()

    return MCPConnectionTokenResponse(
        connection_token=token,
        token_type="Bearer",
        expires_in_hours=expire_hours,
        expires_in_days=max(1, expire_hours // 24),
        expires_at=expires_at,
        tenant_id=ctx.tenant_id,
        zoho_connected=True,
        zoho_data_center=integration.data_center,
        mcp_endpoint_suggestion=f"/sse/{token}",
    )


@router.post(
    "/authorize",
    summary="Initiate Zoho Recruit OAuth authorization flow",
    response_model=AuthorizeResponse,
)
def initiate_authorization(
    data_center: str = Query("com", description="Zoho data center: com, eu, in, au, jp, ca"),
    ctx: AuthenticatedContext = Depends(require_role("admin")),
    db: Session = Depends(get_db),
) -> AuthorizeResponse:
    """Generate CSRF state and OAuth authorization URL."""
    dc = (data_center or "com").lower().strip()
    if dc not in VALID_DATA_CENTERS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_data_center",
                "message": f"Unsupported Zoho data center: '{data_center}'. Allowed data centers: com, eu, in, au, jp, ca.",
            },
        )
    state = ZohoService.create_oauth_state(db, ctx.tenant_id, data_center=dc)
    client = _get_zoho_client(data_center=dc)
    auth_url = client.get_authorization_url(state=state)
    return AuthorizeResponse(authorization_url=auth_url, state=state)


@router.get(
    "/callback",
    summary="Complete Zoho Recruit OAuth flow via browser redirect",
    response_class=HTMLResponse,
)
def handle_browser_callback(
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
    error_description: Optional[str] = Query(None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Receive browser redirection from Zoho Accounts and exchange auth code.

    This endpoint does not require an Authorization header because top-level
    browser navigations do not attach custom headers. Tenant context is
    securely recovered from the atomically claimed one-time OAuth state.
    """
    logger.info("zoho.browser_callback_received")

    # 1. Handle user cancellation or Zoho consent denial
    if error:
        logger.warning("zoho.oauth_callback_denied")
        if state and _SAFE_STATE_REGEX.match(state):
            ZohoService.invalidate_oauth_state(db, state)
        return _render_callback_html(
            title="Authorization Denied",
            heading="Authorization Cancelled",
            message="Authorization was not completed in Zoho. You may close this window and restart the connection from Channel Yield Analytics when ready.",
            is_success=False,
            status_code=status.HTTP_200_OK,
        )

    # 2. Check for missing parameters
    if not code or not state:
        logger.warning("zoho.oauth_callback_missing_params")
        return _render_callback_html(
            title="Invalid Request",
            heading="Missing Parameters",
            message="The authorization callback was missing required parameters. Please return to Channel Yield Analytics and start a new connection request.",
            is_success=False,
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # 3. Validate parameter format
    if not _SAFE_CODE_REGEX.match(code) or not _SAFE_STATE_REGEX.match(state):
        logger.warning("zoho.oauth_callback_malformed_params")
        return _render_callback_html(
            title="Invalid Request",
            heading="Malformed Parameters",
            message="The authorization callback contained invalid or malformed parameters. Please return to Channel Yield Analytics and start a new connection request.",
            is_success=False,
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # 4. Exchange code for tokens and persist credentials
    try:
        client = _get_zoho_client()
        integration = ZohoService.exchange_and_save_tokens(
            db,
            state_token=state,
            code=code,
            client=client,
        )
        logger.info(
            "zoho.oauth_connected",
            data_center=integration.data_center,
        )
        return _render_callback_html(
            title="Authorization Successful",
            heading="Zoho Recruit Connected",
            message="Your Zoho Recruit account has been successfully connected to Channel Yield Analytics. You may now close this window and return to iGentic or your dashboard.",
            is_success=True,
            status_code=status.HTTP_200_OK,
        )
    except OAuthError:
        db.rollback()
        logger.warning("zoho.oauth_callback_state_error")
        return _render_callback_html(
            title="Session Expired",
            heading="Session Expired or Invalid",
            message="This authorization request has expired or has already been used. Please return to Channel Yield Analytics and initiate a new connection.",
            is_success=False,
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    except RateLimitError:
        db.rollback()
        logger.warning("zoho.oauth_callback_rate_limited")
        return _render_callback_html(
            title="Rate Limit Exceeded",
            heading="Rate Limit Exceeded",
            message="Zoho API rate limit was reached during token exchange. Please wait a few moments and try again.",
            is_success=False,
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        )
    except Exception as exc:
        db.rollback()
        logger.error("zoho.oauth_callback_failed", error_type=type(exc).__name__)
        return _render_callback_html(
            title="Connection Failed",
            heading="Connection Failed",
            message="An unexpected error occurred while completing authorization with Zoho. Please check your credentials or try again later.",
            is_success=False,
            status_code=status.HTTP_502_BAD_GATEWAY,
        )


@router.post(
    "/callback",
    summary="Complete Zoho Recruit OAuth flow by exchanging authorization code",
    response_model=IntegrationStatusResponse,
)
def complete_callback(
    req: CallbackRequest,
    ctx: AuthenticatedContext = Depends(require_role("admin")),
    db: Session = Depends(get_db),
) -> IntegrationStatusResponse:
    """Validate state and exchange auth code for tokens.

    The data center is not taken from a callback query parameter. After the
    one-time state row is claimed, the service rebinds the client to the
    data center encoded in that stored state token.
    """
    client = _get_zoho_client()
    integration = ZohoService.exchange_and_save_tokens(
        db,
        tenant_id=ctx.tenant_id,
        state_token=req.state,
        code=req.code,
        client=client,
    )
    return IntegrationStatusResponse(
        status=integration.status,
        data_center=integration.data_center,
        zoho_org_id=integration.zoho_org_id,
        last_sync_at=integration.last_sync_at.isoformat() if integration.last_sync_at else None,
        last_sync_status=integration.last_sync_status,
        is_connected=integration.status == "active",
    )


@router.post(
    "/sync",
    summary="Trigger an on-demand sync from Zoho Recruit into canonical records",
)
def trigger_sync(
    ctx: AuthenticatedContext = Depends(require_role("admin")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Execute on-demand synchronization for authenticated tenant."""
    integration = ZohoService.get_integration(db, ctx.tenant_id)
    if not integration or integration.status != "active":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "integration_not_active", "message": "Zoho Recruit integration is not active."},
        )

    client = _get_zoho_client(data_center=integration.data_center)
    sync_job = ZohoService.execute_sync(
        db,
        tenant_id=ctx.tenant_id,
        client=client,
        user_id=ctx.user_id,
    )
    return {
        "sync_job_id": sync_job.id,
        "status": sync_job.status,
        "records_fetched": sync_job.records_fetched,
        "records_upserted": sync_job.records_upserted,
        "started_at": sync_job.started_at.isoformat() if sync_job.started_at else None,
        "finished_at": sync_job.finished_at.isoformat() if sync_job.finished_at else None,
    }
