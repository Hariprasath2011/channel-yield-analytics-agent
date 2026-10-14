"""Tenant and membership CRUD service.

All operations scope their database queries to the caller's tenant_id.
The tenant_id is always obtained from the server-resolved AuthenticatedContext,
never from a client-supplied parameter.

Cross-tenant access is prevented by design:
  - Every query includes tenant_id in its WHERE clause.
  - Helper functions raise CrossTenantAccessError if a resource belongs
    to a different tenant, as a defence-in-depth measure.
"""
from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.auth.passwords import hash_password
from app.core.exceptions import (
    ConflictError,
    CrossTenantAccessError,
    NotFoundError,
    ValidationError,
)
from app.core.logging import get_logger
from app.persistence.models import ApiKey, Membership, Tenant, User

logger = get_logger(__name__)


# ─── Tenant ───────────────────────────────────────────────────────────────────


def create_tenant(db: Session, *, name: str, slug: str) -> Tenant:
    """Create a new tenant organisation.

    Slug must be unique across all tenants. Use lowercase alphanumeric and hyphens.
    """
    if not name.strip():
        raise ValidationError("Tenant name must not be blank.")
    if not slug.strip() or not all(c.isalnum() or c == "-" for c in slug):
        raise ValidationError("Tenant slug must be alphanumeric with hyphens only.")

    existing = db.query(Tenant).filter(Tenant.slug == slug).first()
    if existing:
        raise ConflictError(f"A tenant with slug '{slug}' already exists.")

    tenant = Tenant(
        id=str(uuid.uuid4()),
        slug=slug.lower().strip(),
        name=name.strip(),
    )
    db.add(tenant)
    db.flush()
    logger.info("tenant.created", tenant_id=tenant.id, slug=tenant.slug)
    return tenant


def get_tenant_by_id(db: Session, tenant_id: str) -> Tenant:
    """Return an active Tenant by ID or raise NotFoundError."""
    tenant = db.query(Tenant).filter(Tenant.id == tenant_id, Tenant.is_active == True).first()
    if tenant is None:
        raise NotFoundError("Tenant")
    return tenant


def assert_tenant_access(resource_tenant_id: str, context_tenant_id: str) -> None:
    """Raise CrossTenantAccessError if resource belongs to a different tenant.

    Call this before returning any resource that includes a tenant_id field,
    as a defence-in-depth measure in addition to query-level scoping.
    """
    if resource_tenant_id != context_tenant_id:
        logger.error(
            "security.cross_tenant_access_attempt",
            resource_tenant=resource_tenant_id,
            context_tenant=context_tenant_id,
        )
        raise CrossTenantAccessError()


# ─── User ─────────────────────────────────────────────────────────────────────


def create_user(
    db: Session,
    *,
    tenant_id: str,
    email: str,
    full_name: Optional[str] = None,
    password: Optional[str] = None,
    role: str = "viewer",
) -> tuple[User, Membership]:
    """Create a user within a tenant and assign an initial role.

    Returns (user, membership). The password is hashed before storage;
    the plaintext is discarded immediately after hashing.
    """
    email = email.lower().strip()
    if not email or "@" not in email:
        raise ValidationError("A valid email address is required.")

    existing = db.query(User).filter(User.tenant_id == tenant_id, User.email == email).first()
    if existing:
        raise ConflictError(f"A user with email '{email}' already exists in this organisation.")

    hashed = hash_password(password) if password else None
    user = User(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        email=email,
        full_name=full_name,
        hashed_password=hashed,
    )
    db.add(user)
    db.flush()

    membership = Membership(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        user_id=user.id,
        role=role,
    )
    db.add(membership)
    db.flush()
    logger.info("user.created", tenant_id=tenant_id, user_id=user.id, role=role)
    return user, membership


def get_user_by_email(db: Session, *, tenant_id: str, email: str) -> Optional[User]:
    """Return an active User by email within a tenant."""
    return (
        db.query(User)
        .filter(User.tenant_id == tenant_id, User.email == email.lower().strip(), User.is_active == True)
        .first()
    )


# ─── API Keys ─────────────────────────────────────────────────────────────────


import hashlib


def create_api_key(
    db: Session,
    *,
    tenant_id: str,
    name: str,
) -> tuple[ApiKey, str]:
    """Generate a new API key for a tenant.

    Returns (ApiKey ORM object, plaintext_key).
    The plaintext_key is shown ONCE and is not stored.
    Only the SHA-256 hash is persisted.
    """
    if not name.strip():
        raise ValidationError("API key name must not be blank.")

    # Prefix + random component; prefix allows quick identification in logs.
    random_part = secrets.token_urlsafe(32)
    plaintext_key = f"cyk_{random_part}"
    key_hash = hashlib.sha256(plaintext_key.encode()).hexdigest()
    key_prefix = plaintext_key[:8]

    api_key = ApiKey(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        name=name.strip(),
        key_prefix=key_prefix,
        key_hash=key_hash,
    )
    db.add(api_key)
    db.flush()
    logger.info("api_key.created", tenant_id=tenant_id, api_key_id=api_key.id, key_name=name)
    return api_key, plaintext_key


def revoke_api_key(db: Session, *, tenant_id: str, api_key_id: str) -> ApiKey:
    """Deactivate an API key. Raises NotFoundError if not found within the tenant."""
    api_key = (
        db.query(ApiKey)
        .filter(ApiKey.id == api_key_id, ApiKey.tenant_id == tenant_id)
        .first()
    )
    if api_key is None:
        raise NotFoundError("API key")
    api_key.is_active = False
    db.flush()
    logger.info("api_key.revoked", tenant_id=tenant_id, api_key_id=api_key_id)
    return api_key


def list_api_keys(db: Session, *, tenant_id: str) -> list[ApiKey]:
    """Return all API keys for a tenant, ordered by creation date descending."""
    return (
        db.query(ApiKey)
        .filter(ApiKey.tenant_id == tenant_id)
        .order_by(ApiKey.created_at.desc())
        .all()
    )


def list_members(db: Session, *, tenant_id: str) -> list[dict]:
    """Return members of a tenant with user details and roles."""
    memberships = (
        db.query(Membership, User)
        .join(User, Membership.user_id == User.id)
        .filter(Membership.tenant_id == tenant_id)
        .order_by(Membership.created_at.asc())
        .all()
    )
    return [
        {
            "user_id": user.id,
            "email": user.email,
            "full_name": user.full_name,
            "role": membership.role,
            "is_active": user.is_active,
            "created_at": membership.created_at,
        }
        for membership, user in memberships
    ]
