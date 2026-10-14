"""Authentication and identity router (/v2/auth).

Endpoints:
  - POST   /v2/auth/token       - Authenticate human user with email/password -> JWT
  - GET    /v2/auth/me          - Retrieve authenticated identity, tenant, and role
  - POST   /v2/auth/api-keys    - Issue a new machine API key (admin or owner only)
  - GET    /v2/auth/api-keys    - List active/revoked API keys (admin or owner only)
  - DELETE /v2/auth/api-keys/{id} - Revoke an API key (admin or owner only)
  - GET    /v2/auth/members     - List tenant members (admin or owner only)
  - POST   /v2/auth/members     - Provision a new member (owner only)
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from app.auth.dependencies import (
    AuthenticatedContext,
    get_authenticated_context,
    require_role,
)
from app.auth.passwords import verify_password
from app.auth.rate_limiter import login_limiter
from app.auth.tokens import create_access_token
from app.core.config import get_settings
from app.core.exceptions import http_401, http_403, http_404, http_429
from app.core.logging import get_logger
from app.persistence.database import get_db
from app.persistence.models import Membership, Tenant, User
from app.tenancy.service import (
    create_api_key,
    create_user,
    list_api_keys,
    list_members,
    revoke_api_key,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/v2/auth", tags=["Authentication & Identity"])


# ─── Request / Response Schemas ───────────────────────────────────────────────


class LoginRequest(BaseModel):
    email: str = Field(..., max_length=320, description="User email address")
    password: str = Field(..., max_length=128, description="User password")
    tenant_slug: Optional[str] = Field(
        None, max_length=64, description="Optional tenant slug for organizations with shared email"
    )


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    tenant_id: str
    tenant_slug: str
    role: str


class CurrentUserResponse(BaseModel):
    user_id: str
    email: str
    tenant_id: str
    tenant_name: str
    tenant_slug: str
    role: str


class CreateApiKeyRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128, description="Description/name for the key")


class ApiKeyCreatedResponse(BaseModel):
    id: str
    name: str
    key_prefix: str
    api_key: str
    created_at: datetime


class ApiKeyItemResponse(BaseModel):
    id: str
    name: str
    key_prefix: str
    is_active: bool
    created_at: datetime
    last_used_at: Optional[datetime] = None


class CreateMemberRequest(BaseModel):
    email: str
    full_name: Optional[str] = None
    password: Optional[str] = None
    role: str = Field("viewer", pattern="^(viewer|analyst|admin|owner)$")


class MemberItemResponse(BaseModel):
    user_id: str
    email: str
    full_name: Optional[str] = None
    role: str
    is_active: bool
    created_at: datetime


# ─── Endpoints ────────────────────────────────────────────────────────────────


@router.post(
    "/token",
    response_model=TokenResponse,
    summary="Authenticate user and issue JWT access token",
)
def login(
    req: LoginRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> TokenResponse:
    settings = get_settings()
    email = req.email.lower().strip()
    client_ip = request.client.host if request.client else "unknown"
    rate_key = f"{client_ip}:{email}"

    # Brute-force rate check
    if login_limiter.is_rate_limited(rate_key) or login_limiter.is_rate_limited(client_ip):
        raise http_429("Too many failed login attempts. Please try again later.", retry_after=300)

    # Locate tenant
    tenant: Optional[Tenant] = None
    if req.tenant_slug:
        tenant = (
            db.query(Tenant)
            .filter(Tenant.slug == req.tenant_slug.lower().strip(), Tenant.is_active == True)
            .first()
        )
        if tenant is None:
            login_limiter.record_failure(rate_key)
            login_limiter.record_failure(client_ip)
            raise http_401("Invalid credentials or inactive organization.")
    else:
        # If no tenant slug specified, find tenants where this user exists
        users = (
            db.query(User, Tenant)
            .join(Tenant, User.tenant_id == Tenant.id)
            .filter(User.email == email, User.is_active == True, Tenant.is_active == True)
            .all()
        )
        if len(users) == 1:
            tenant = users[0][1]
        elif len(users) > 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": "tenant_required",
                    "message": "User belongs to multiple organizations; specify 'tenant_slug'.",
                },
            )
        else:
            login_limiter.record_failure(rate_key)
            login_limiter.record_failure(client_ip)
            raise http_401("Invalid credentials or inactive organization.")

    # Locate user in tenant
    user = (
        db.query(User)
        .filter(User.tenant_id == tenant.id, User.email == email, User.is_active == True)
        .first()
    )
    if user is None or not user.hashed_password:
        login_limiter.record_failure(rate_key)
        login_limiter.record_failure(client_ip)
        raise http_401("Invalid credentials or inactive organization.")

    if not verify_password(req.password, user.hashed_password):
        login_limiter.record_failure(rate_key)
        login_limiter.record_failure(client_ip)
        raise http_401("Invalid credentials or inactive organization.")

    # Locate active membership
    membership = (
        db.query(Membership)
        .filter(Membership.tenant_id == tenant.id, Membership.user_id == user.id)
        .first()
    )
    if membership is None:
        login_limiter.record_failure(rate_key)
        login_limiter.record_failure(client_ip)
        raise http_401("Account has no active membership in this organization.")

    # Authentication succeeded: reset rate limit counters
    login_limiter.record_success(rate_key)
    login_limiter.record_success(client_ip)

    token = create_access_token(
        user_id=user.id,
        tenant_id=tenant.id,
        role=membership.role,
        expire_minutes=settings.access_token_expire_minutes,
    )
    logger.info("auth.login_success", user_id=user.id, tenant_id=tenant.id, role=membership.role)

    return TokenResponse(
        access_token=token,
        token_type="bearer",
        expires_in=settings.access_token_expire_minutes * 60,
        tenant_id=tenant.id,
        tenant_slug=tenant.slug,
        role=membership.role,
    )


@router.get(
    "/me",
    response_model=CurrentUserResponse,
    summary="Get current authenticated identity and organization details",
)
def get_me(
    ctx: AuthenticatedContext = Depends(get_authenticated_context),
) -> CurrentUserResponse:
    return CurrentUserResponse(
        user_id=ctx.user.id,
        email=ctx.user.email,
        tenant_id=ctx.tenant.id,
        tenant_name=ctx.tenant.name,
        tenant_slug=ctx.tenant.slug,
        role=ctx.role,
    )


@router.post(
    "/api-keys",
    response_model=ApiKeyCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Issue a new machine API key (admin or owner only)",
)
def issue_api_key(
    req: CreateApiKeyRequest,
    ctx: AuthenticatedContext = Depends(require_role("admin")),
    db: Session = Depends(get_db),
) -> ApiKeyCreatedResponse:
    key_obj, plaintext_key = create_api_key(
        db,
        tenant_id=ctx.tenant_id,
        name=req.name,
    )
    return ApiKeyCreatedResponse(
        id=key_obj.id,
        name=key_obj.name,
        key_prefix=key_obj.key_prefix,
        api_key=plaintext_key,
        created_at=key_obj.created_at,
    )


@router.get(
    "/api-keys",
    response_model=list[ApiKeyItemResponse],
    summary="List API keys for current organization (admin or owner only)",
)
def list_tenant_api_keys(
    ctx: AuthenticatedContext = Depends(require_role("admin")),
    db: Session = Depends(get_db),
) -> list[ApiKeyItemResponse]:
    keys = list_api_keys(db, tenant_id=ctx.tenant_id)
    return [
        ApiKeyItemResponse(
            id=k.id,
            name=k.name,
            key_prefix=k.key_prefix,
            is_active=k.is_active,
            created_at=k.created_at,
            last_used_at=k.last_used_at,
        )
        for k in keys
    ]


@router.delete(
    "/api-keys/{api_key_id}",
    summary="Revoke an API key (admin or owner only)",
)
def delete_api_key(
    api_key_id: str,
    ctx: AuthenticatedContext = Depends(require_role("admin")),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    revoke_api_key(db, tenant_id=ctx.tenant_id, api_key_id=api_key_id)
    return {"status": "revoked", "id": api_key_id}


@router.get(
    "/members",
    response_model=list[MemberItemResponse],
    summary="List organization members (admin or owner only)",
)
def get_tenant_members(
    ctx: AuthenticatedContext = Depends(require_role("admin")),
    db: Session = Depends(get_db),
) -> list[MemberItemResponse]:
    members = list_members(db, tenant_id=ctx.tenant_id)
    return [MemberItemResponse(**m) for m in members]


@router.post(
    "/members",
    response_model=MemberItemResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Provision a new organization member (owner only)",
)
def add_tenant_member(
    req: CreateMemberRequest,
    ctx: AuthenticatedContext = Depends(require_role("owner")),
    db: Session = Depends(get_db),
) -> MemberItemResponse:
    user, membership = create_user(
        db,
        tenant_id=ctx.tenant_id,
        email=req.email,
        full_name=req.full_name,
        password=req.password,
        role=req.role,
    )
    return MemberItemResponse(
        user_id=user.id,
        email=user.email,
        full_name=user.full_name,
        role=membership.role,
        is_active=user.is_active,
        created_at=membership.created_at,
    )
