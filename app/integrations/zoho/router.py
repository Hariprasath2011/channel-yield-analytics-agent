"""Zoho Recruit integration router (/v2/integrations/zoho)."""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.dependencies import AuthenticatedContext, require_role
from app.core.config import get_settings
from app.core.exceptions import NotFoundError, OAuthError
from app.core.logging import get_logger
from app.integrations.zoho.client import DATACENTER_DOMAINS, ZohoRecruitClient
from app.integrations.zoho.service import ZohoService
from app.persistence.database import get_db

logger = get_logger(__name__)

router = APIRouter(prefix="/v2/integrations/zoho", tags=["Zoho Recruit Integration"])


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
