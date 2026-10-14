"""SQLAlchemy ORM models for the Channel Yield Analytics Agent.

Design principles:
  - Every tenant-owned table has an explicit tenant_id foreign key.
  - Uniqueness constraints include tenant_id to enforce per-tenant isolation.
  - All timestamps are stored in UTC (DateTime(timezone=True)).
  - Canonical records are stored as typed columns with a JSON payload
    supplement — not as opaque blobs — so that indexes can be applied.
  - Credential fields (api_key_hash, encrypted_access_token, etc.) are
    never returned by default serialisation helpers.
  - Audit events carry who, what, when, and result; they do NOT store
    token values, passwords, or sensitive payloads.

Schema versioning is managed by Alembic migrations — not by CREATE TABLE
IF NOT EXISTS at startup.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.persistence.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ─── Tenant / Identity ────────────────────────────────────────────────────────


class Tenant(Base):
    """An independent client organisation.

    Each tenant's data is logically isolated. Tenant IDs are never trusted
    from callers; they are always resolved from a verified identity.
    """
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )

    # Relationships
    users: Mapped[list["User"]] = relationship("User", back_populates="tenant")
    memberships: Mapped[list["Membership"]] = relationship("Membership", back_populates="tenant")
    api_keys: Mapped[list["ApiKey"]] = relationship("ApiKey", back_populates="tenant")
    integrations: Mapped[list["ZohoIntegration"]] = relationship("ZohoIntegration", back_populates="tenant")
    import_jobs: Mapped[list["ImportJob"]] = relationship("ImportJob", back_populates="tenant")
    canonical_records: Mapped[list["CanonicalRecord"]] = relationship("CanonicalRecord", back_populates="tenant")
    audit_events: Mapped[list["AuditEvent"]] = relationship("AuditEvent", back_populates="tenant")


class User(Base):
    """A human user. Users belong to exactly one tenant in this design.

    For SSO/multi-org, extend with a UserTenantMembership join table.
    Password hashes use bcrypt via passlib. Passwords are never stored in plain text.
    """
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("tenant_id", "email", name="uq_users_tenant_email"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    full_name: Mapped[Optional[str]] = mapped_column(String(256))
    hashed_password: Mapped[Optional[str]] = mapped_column(String(256))  # NULL for SSO-only
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )

    tenant: Mapped["Tenant"] = relationship("Tenant", back_populates="users")
    memberships: Mapped[list["Membership"]] = relationship("Membership", back_populates="user")


class Membership(Base):
    """Associates a User with a Tenant and assigns a role.

    Roles:
      owner       — full control, can add/remove members and integrations.
      admin       — manage integrations, imports, and read all analytics.
      analyst     — read analytics, run agent queries.
      viewer      — read-only analytics access.
    """
    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", name="uq_memberships_tenant_user"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(
        Enum("owner", "admin", "analyst", "viewer", name="membership_role"),
        nullable=False,
        default="viewer",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )

    tenant: Mapped["Tenant"] = relationship("Tenant", back_populates="memberships")
    user: Mapped["User"] = relationship("User", back_populates="memberships")


class ApiKey(Base):
    """Machine-to-machine service credential for a tenant.

    The plaintext key is shown ONCE at creation time. Only the SHA-256 hash
    is stored. The hash is used for constant-time comparison on every request.
    """
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    key_prefix: Mapped[str] = mapped_column(String(8), nullable=False)  # First 8 chars for identification
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    tenant: Mapped["Tenant"] = relationship("Tenant", back_populates="api_keys")


# ─── Integrations ─────────────────────────────────────────────────────────────


class ZohoIntegration(Base):
    """A Zoho Recruit OAuth connection for one tenant.

    Tokens are stored encrypted using the CY_ENCRYPTION_KEY Fernet key.
    Token values are NEVER logged or returned in API responses.

    data_center: com | eu | in | au | jp | ca
    """
    __tablename__ = "zoho_integrations"
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_zoho_one_per_tenant"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    zoho_org_id: Mapped[Optional[str]] = mapped_column(String(64))
    data_center: Mapped[str] = mapped_column(String(8), nullable=False, default="com")
    status: Mapped[str] = mapped_column(
        Enum("pending_auth", "active", "token_expired", "revoked", "error",
             name="zoho_integration_status"),
        nullable=False,
        default="pending_auth",
    )
    # Encrypted with Fernet; never stored plaintext.
    encrypted_access_token: Mapped[Optional[str]] = mapped_column(Text)
    encrypted_refresh_token: Mapped[Optional[str]] = mapped_column(Text)
    token_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    scopes: Mapped[Optional[str]] = mapped_column(Text)  # Space-separated scopes
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )
    last_sync_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_sync_status: Mapped[Optional[str]] = mapped_column(String(32))

    tenant: Mapped["Tenant"] = relationship("Tenant", back_populates="integrations")
    sync_jobs: Mapped[list["SyncJob"]] = relationship("SyncJob", back_populates="integration")


class OAuthState(Base):
    """One-time-use OAuth state token, scoped to a tenant and expiring quickly.

    Prevents CSRF in the OAuth authorization flow.
    State values are unpredictable random tokens, never derived from tenant data.
    """
    __tablename__ = "oauth_states"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    state_token: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# ─── Jobs ─────────────────────────────────────────────────────────────────────


class ImportJob(Base):
    """Tracks a CSV import operation for a tenant.

    Every import is associated with a tenant and a user who initiated it.
    Results are stored here, not in the canonical records table, so that
    the job history remains auditable even after canonical records change.
    """
    __tablename__ = "import_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    initiated_by_user_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(
        Enum("pending", "running", "completed", "completed_with_warnings",
             "completed_with_quarantine", "failed", "failed_validation",
             name="import_job_status"),
        nullable=False,
        default="pending",
    )
    source_system_id: Mapped[str] = mapped_column(String(64), nullable=False)
    mapping_version: Mapped[str] = mapped_column(String(16), nullable=False)
    rows_read: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rows_inserted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rows_updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rows_quarantined: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_summary: Mapped[Optional[str]] = mapped_column(Text)  # Sanitised, no PII
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )

    tenant: Mapped["Tenant"] = relationship("Tenant", back_populates="import_jobs")

    __table_args__ = (
        Index("ix_import_jobs_tenant_created", "tenant_id", "created_at"),
    )


class SyncJob(Base):
    """Tracks a Zoho Recruit synchronisation operation."""
    __tablename__ = "sync_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    integration_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("zoho_integrations.id", ondelete="CASCADE"), nullable=False
    )
    sync_type: Mapped[str] = mapped_column(
        Enum("full", "incremental", name="sync_type"), nullable=False, default="full"
    )
    status: Mapped[str] = mapped_column(
        Enum("pending", "running", "completed", "failed", "cancelled",
             name="sync_job_status"),
        nullable=False,
        default="pending",
    )
    records_fetched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    records_upserted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    records_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    watermark: Mapped[Optional[str]] = mapped_column(String(64))  # Last sync cursor / timestamp
    error_summary: Mapped[Optional[str]] = mapped_column(Text)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )

    tenant: Mapped["Tenant"] = relationship("Tenant")
    integration: Mapped["ZohoIntegration"] = relationship("ZohoIntegration", back_populates="sync_jobs")

    __table_args__ = (
        Index("ix_sync_jobs_tenant_integration", "tenant_id", "integration_id"),
    )


# ─── Canonical Records ────────────────────────────────────────────────────────


class CanonicalRecord(Base):
    """Tenant-scoped canonical recruitment record.

    This replaces the prototype's single-table BLOB store.
    The entity_type column allows querying by record type (e.g. 'applications')
    while the payload column retains full canonical field flexibility.

    Uniqueness is enforced at two levels:
      1. (tenant_id, entity_type, entity_id) — canonical identity.
      2. (tenant_id, entity_type, source_system_id, source_record_id) — source identity.

    The fingerprint enables idempotent replay detection without recomputing payloads.
    """
    __tablename__ = "canonical_records"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "entity_type", "entity_id",
            name="uq_canonical_records_tenant_entity",
        ),
        UniqueConstraint(
            "tenant_id", "entity_type", "source_system_id", "source_record_id",
            name="uq_canonical_records_source_key",
        ),
        Index("ix_canonical_records_tenant_type", "tenant_id", "entity_type"),
        Index("ix_canonical_records_tenant_entity", "tenant_id", "entity_type", "entity_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_system_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_record_id: Mapped[str] = mapped_column(String(128), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)  # JSON
    import_job_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("import_jobs.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )

    tenant: Mapped["Tenant"] = relationship("Tenant", back_populates="canonical_records")


# ─── Audit Events ─────────────────────────────────────────────────────────────


class AuditEvent(Base):
    """Immutable log of security-sensitive operations.

    Events capture who, what, when, and the outcome. They do NOT capture
    credential values, token contents, or candidate personal data.
    """
    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_tenant_created", "tenant_id", "created_at"),
        Index("ix_audit_events_actor", "actor_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="SET NULL")
    )
    actor_id: Mapped[Optional[str]] = mapped_column(String(128))  # user_id or api_key_id
    actor_type: Mapped[Optional[str]] = mapped_column(String(32))  # "user" | "api_key" | "system"
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    # Sanitised context — never include tokens, passwords, or raw payloads.
    resource_type: Mapped[Optional[str]] = mapped_column(String(64))
    resource_id: Mapped[Optional[str]] = mapped_column(String(128))
    outcome: Mapped[str] = mapped_column(
        Enum("success", "failure", "blocked", name="audit_outcome"),
        nullable=False,
    )
    ip_address: Mapped[Optional[str]] = mapped_column(String(45))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )

    tenant: Mapped[Optional["Tenant"]] = relationship("Tenant", back_populates="audit_events")
