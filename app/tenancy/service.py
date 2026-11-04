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

import re
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.exc import IntegrityError
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

_EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")


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

    if password is not None:
        pw_bytes = password.encode("utf-8")
        if len(pw_bytes) > 72:
            raise ValidationError(
                f"Password exceeds bcrypt limit of 72 UTF-8 bytes ({len(pw_bytes)} bytes provided)."
            )

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


def validate_bootstrap_password(password: str) -> None:
    """Validate password according to security policy.

    Rejects passwords that are empty, shorter than 8 characters,
    or longer than bcrypt's 72-byte limit to avoid silent truncation.
    """
    if not password or not password.strip():
        raise ValidationError("Password must not be blank.")
    if len(password) < 8:
        raise ValidationError("Password must be at least 8 characters long.")
    pw_bytes = password.encode("utf-8")
    if len(pw_bytes) > 72:
        raise ValidationError(
            f"Password exceeds bcrypt limit of 72 UTF-8 bytes ({len(pw_bytes)} bytes provided). "
            "Please choose a password of 72 bytes or fewer."
        )


def bootstrap_tenant_and_owner(
    db: Session,
    *,
    name: str,
    slug: str,
    email: str,
    password: str,
    full_name: Optional[str] = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Bootstrap an initial tenant organisation and its first owner account.

    Enforces:
      - Validates non-empty tenant name (<= 256 chars).
      - Validates slug format (2-64 chars, lowercase alphanumeric and hyphens, no leading/trailing hyphens).
      - Validates email address format and length.
      - Enforces password security policy (non-blank, >= 8 chars, <= 72 UTF-8 bytes).
      - Checks for duplicate tenant slug or account collisions before mutating.
      - If dry_run is True, verifies preconditions without creating or modifying database records.
      - Atomically creates Tenant, User, and Membership with role='owner'.
      - Rolls back all changes on any failure or constraint race.
    """
    if not name or not name.strip():
        raise ValidationError("Tenant name must not be blank.")
    clean_name = name.strip()
    if len(clean_name) > 256:
        raise ValidationError("Tenant name must not exceed 256 characters.")

    if not slug or not slug.strip():
        raise ValidationError("Tenant slug must not be blank.")
    clean_slug = slug.strip()
    if len(clean_slug) < 2 or len(clean_slug) > 64:
        raise ValidationError("Tenant slug must be between 2 and 64 characters.")
    if not all((c.islower() or c.isdigit() or c == "-") for c in clean_slug):
        raise ValidationError("Tenant slug must be lowercase alphanumeric with hyphens only.")
    if clean_slug.startswith("-") or clean_slug.endswith("-"):
        raise ValidationError("Tenant slug must not start or end with a hyphen.")

    if not email or not email.strip():
        raise ValidationError("Owner email must not be blank.")
    clean_email = email.strip().lower()
    if len(clean_email) > 320 or not _EMAIL_REGEX.match(clean_email):
        raise ValidationError("A valid email address is required.")

    validate_bootstrap_password(password)

    clean_full_name = full_name.strip() if full_name and full_name.strip() else None
    if clean_full_name and len(clean_full_name) > 256:
        raise ValidationError("Full name must not exceed 256 characters.")

    # Check for existing tenant slug
    existing_tenant = db.query(Tenant).filter(Tenant.slug == clean_slug).first()
    if existing_tenant is not None:
        raise ConflictError(f"A tenant with slug '{clean_slug}' already exists.")

    if dry_run:
        # Preflight mode: all validations passed, verify DB connection is live, do not mutate.
        db.rollback()
        return {
            "status": "dry_run",
            "tenant_name": clean_name,
            "tenant_slug": clean_slug,
            "owner_email": clean_email,
            "owner_full_name": clean_full_name,
            "role": "owner",
            "message": "Preflight validation succeeded. Tenant and owner can be safely created.",
        }

    # Atomic creation within active session
    try:
        tenant = create_tenant(db, name=clean_name, slug=clean_slug)
        user, membership = create_user(
            db,
            tenant_id=tenant.id,
            email=clean_email,
            full_name=clean_full_name,
            password=password,
            role="owner",
        )
        db.flush()
        logger.info(
            "bootstrap.completed",
            tenant_id=tenant.id,
            tenant_slug=tenant.slug,
            user_id=user.id,
            role="owner",
        )
        return {
            "status": "created",
            "tenant_id": tenant.id,
            "tenant_name": tenant.name,
            "tenant_slug": tenant.slug,
            "user_id": user.id,
            "owner_email": user.email,
            "owner_full_name": user.full_name,
            "role": membership.role,
        }
    except IntegrityError as exc:
        db.rollback()
        logger.warning("bootstrap.integrity_violation", slug=clean_slug)
        raise ConflictError(f"A tenant with slug '{clean_slug}' or user with this email already exists.") from exc
    except Exception:
        db.rollback()
        raise


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
