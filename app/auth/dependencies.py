"""FastAPI dependency: resolve the current authenticated identity and tenant.

Supports:
  - Human users via signed JWT access tokens (`sub`, `tid`, `role`).
  - Machine integrations via SHA-256 hashed API keys (`cyk_...`).
  - Strict tenant isolation: tenant_id is always re-verified in the DB and never
    accepted from caller-supplied parameters.
  - Role-based authorization: viewer, analyst, admin, owner.
  - Inactive user and inactive tenant rejection.
"""
from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, Header, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.auth.tokens import decode_access_token
from app.core.config import get_settings
from app.core.exceptions import AuthenticationError, AuthorizationError, http_401, http_403
from app.core.logging import get_logger
from app.persistence.database import get_db
from app.persistence.models import ApiKey, Membership, Tenant, User

logger = get_logger(__name__)
_bearer = HTTPBearer(auto_error=False)

_ROLE_ORDER = {"viewer": 0, "analyst": 1, "admin": 2, "owner": 3}


class AuthenticatedContext:
    """Resolved authentication context for a request.

    user:       The authenticated User ORM object (or synthetic user for API keys).
    tenant:     The Tenant ORM object the identity belongs to.
    membership: The active Membership (contains role).
    """

    def __init__(self, user: User, tenant: Tenant, membership: Membership) -> None:
        self.user = user
        self.tenant = tenant
        self.membership = membership

    @property
    def tenant_id(self) -> str:
        return self.tenant.id

    @property
    def user_id(self) -> str:
        return self.user.id

    @property
    def role(self) -> str:
        return self.membership.role


def _hash_api_key(raw_key: str) -> str:
    """SHA-256 hash of a raw API key for database comparison."""
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def _resolve_user_from_payload(payload: dict, db: Session) -> AuthenticatedContext:
    user_id: str = payload.get("sub", "")
    tenant_id: str = payload.get("tid", "")

    if not user_id or not tenant_id:
        raise http_401("Token payload is incomplete.")

    user = db.query(User).filter(User.id == user_id, User.is_active == True).first()
    if user is None:
        raise http_401("User account not found or inactive.")

    tenant = db.query(Tenant).filter(Tenant.id == tenant_id, Tenant.is_active == True).first()
    if tenant is None:
        raise http_401("Tenant not found or inactive.")

    membership = (
        db.query(Membership)
        .filter(Membership.tenant_id == tenant_id, Membership.user_id == user_id)
        .first()
    )
    if membership is None:
        logger.warning(
            "auth.cross_tenant_attempt",
            user_id=user_id,
            token_tenant_id=tenant_id,
        )
        raise http_403("You are not a member of this organisation.")

    return AuthenticatedContext(user=user, tenant=tenant, membership=membership)


def _resolve_api_key_from_token(raw_key: str, db: Session) -> AuthenticatedContext:
    key_hash = _hash_api_key(raw_key)
    api_key = (
        db.query(ApiKey)
        .filter(ApiKey.key_hash == key_hash, ApiKey.is_active == True)
        .first()
    )
    if api_key is None:
        raise http_401("API key not found or inactive.")

    now = datetime.now(timezone.utc)
    if api_key.expires_at:
        exp = api_key.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp < now:
            raise http_401("API key has expired.")

    try:
        api_key.last_used_at = now
        db.commit()
    except Exception:
        db.rollback()

    tenant = db.query(Tenant).filter(Tenant.id == api_key.tenant_id, Tenant.is_active == True).first()
    if tenant is None:
        raise http_401("Tenant for this API key is inactive.")

    synthetic_membership = Membership(
        id="apikey",
        tenant_id=tenant.id,
        user_id=api_key.id,
        role="analyst",
    )
    synthetic_user = User(
        id=api_key.id,
        tenant_id=tenant.id,
        email=f"apikey:{api_key.key_prefix}",
        is_active=True,
    )
    return AuthenticatedContext(
        user=synthetic_user,
        tenant=tenant,
        membership=synthetic_membership,
    )


def get_current_token_payload(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(_bearer),
) -> dict:
    """Extract and verify the JWT bearer token from the Authorization header."""
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise http_401("A Bearer token is required.")
    try:
        return decode_access_token(credentials.credentials)
    except AuthenticationError as exc:
        raise http_401(str(exc)) from exc


def get_current_user(
    payload: dict = Depends(get_current_token_payload),
    db: Session = Depends(get_db),
) -> AuthenticatedContext:
    """Resolve User, Tenant, and active Membership from a verified JWT payload."""
    return _resolve_user_from_payload(payload, db)


def get_api_key_context(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(_bearer),
    db: Session = Depends(get_db),
) -> AuthenticatedContext:
    """Authenticate using an API key (service/machine credential)."""
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise http_401("A Bearer API key is required.")
    return _resolve_api_key_from_token(credentials.credentials.strip(), db)


def get_authenticated_context(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(_bearer),
    db: Session = Depends(get_db),
) -> AuthenticatedContext:
    """Unified authentication dependency: supports JWT access tokens or 'cyk_' API keys."""
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise http_401("A Bearer token or API key is required.")

    token = credentials.credentials.strip()
    if token.startswith("cyk_"):
        return _resolve_api_key_from_token(token, db)

    try:
        payload = decode_access_token(token)
    except AuthenticationError as exc:
        raise http_401(str(exc)) from exc

    return _resolve_user_from_payload(payload, db)


def require_role(minimum_role: str):
    """Enforce a minimum membership role for the authenticated context."""
    def _check(ctx: AuthenticatedContext = Depends(get_authenticated_context)) -> AuthenticatedContext:
        user_level = _ROLE_ORDER.get(ctx.role, -1)
        required_level = _ROLE_ORDER.get(minimum_role, 99)
        if user_level < required_level:
            raise http_403(f"This action requires the '{minimum_role}' role or higher.")
        return ctx
    return _check


def verify_legacy_token(authorization: str) -> bool:
    """Constant-time comparison of the legacy bearer token for prototype compat."""
    settings = get_settings()
    if not settings.legacy_enabled:
        return False
    if not authorization.startswith("Bearer "):
        return False
    supplied = authorization[7:].strip()
    return hmac.compare_digest(supplied, settings.legacy_api_token)
