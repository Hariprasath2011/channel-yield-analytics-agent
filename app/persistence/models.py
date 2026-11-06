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


# ─── Canonical Recruitment Relational Entities (Phase 4) ──────────────────────


class SourceChannel(Base):
    """Normalized sourcing channel definition and mapping table."""
    __tablename__ = "source_channels"
    __table_args__ = (
        UniqueConstraint("tenant_id", "raw_source_value", name="uq_source_channels_tenant_raw"),
        Index("ix_source_channels_tenant_channel", "tenant_id", "channel_name"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    channel_name: Mapped[str] = mapped_column(String(128), nullable=False)
    raw_source_value: Mapped[str] = mapped_column(String(128), nullable=False)
    category: Mapped[Optional[str]] = mapped_column(String(64), default="direct")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )


class Candidate(Base):
    """Canonical candidate record with PII minimization."""
    __tablename__ = "candidates"
    __table_args__ = (
        UniqueConstraint("tenant_id", "zoho_candidate_id", name="uq_candidates_tenant_zoho_id"),
        Index("ix_candidates_tenant_status", "tenant_id", "candidate_status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    zoho_candidate_id: Mapped[str] = mapped_column(String(128), nullable=False)
    full_name: Mapped[Optional[str]] = mapped_column(String(256))
    masked_email: Mapped[Optional[str]] = mapped_column(String(320))
    candidate_status: Mapped[Optional[str]] = mapped_column(String(64))
    lead_source: Mapped[Optional[str]] = mapped_column(String(128))
    channel_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("source_channels.id", ondelete="SET NULL")
    )
    created_time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    updated_time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )


class JobOpening(Base):
    """Canonical job opening / requisition entity."""
    __tablename__ = "job_openings"
    __table_args__ = (
        UniqueConstraint("tenant_id", "zoho_job_opening_id", name="uq_job_openings_tenant_zoho_id"),
        Index("ix_job_openings_tenant_status", "tenant_id", "job_opening_status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    zoho_job_opening_id: Mapped[str] = mapped_column(String(128), nullable=False)
    posting_title: Mapped[str] = mapped_column(String(256), nullable=False)
    job_opening_status: Mapped[str] = mapped_column(String(64), nullable=False, default="open")
    department: Mapped[Optional[str]] = mapped_column(String(128))
    number_of_positions: Mapped[Optional[int]] = mapped_column(Integer, default=1)
    opened_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    target_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )


class Application(Base):
    """Canonical job application entity connecting candidate and requisition."""
    __tablename__ = "applications"
    __table_args__ = (
        UniqueConstraint("tenant_id", "zoho_application_id", name="uq_applications_tenant_zoho_id"),
        Index("ix_applications_tenant_req", "tenant_id", "job_opening_id"),
        Index("ix_applications_tenant_cand", "tenant_id", "candidate_id"),
        Index("ix_applications_tenant_channel", "tenant_id", "source_channel"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    zoho_application_id: Mapped[str] = mapped_column(String(128), nullable=False)
    candidate_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("candidates.id", ondelete="SET NULL")
    )
    job_opening_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("job_openings.id", ondelete="SET NULL")
    )
    channel_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("source_channels.id", ondelete="SET NULL")
    )
    application_status: Mapped[str] = mapped_column(String(64), nullable=False)
    current_stage: Mapped[str] = mapped_column(String(64), nullable=False, default="applied")
    source_channel: Mapped[str] = mapped_column(String(128), nullable=False, default="unknown")
    attribution_method: Mapped[str] = mapped_column(
        String(32), nullable=False, default="application_field"
    )
    attribution_confidence: Mapped[Optional[float]] = mapped_column(Numeric(3, 2), default=1.0)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    joined_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    application_outcome: Mapped[str] = mapped_column(
        String(32), nullable=False, default="in_progress"
    )
    rejection_reason: Mapped[Optional[str]] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )


class ApplicationStageEvent(Base):
    """Historical selection stage transition event for an application."""
    __tablename__ = "application_stage_events"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "application_id", "stage", "entered_at",
            name="uq_stage_events_tenant_app_stage_entered",
        ),
        Index("ix_stage_events_tenant_app", "tenant_id", "application_id"),
        Index("ix_stage_events_tenant_stage", "tenant_id", "stage"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    application_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("applications.id", ondelete="CASCADE"), nullable=False
    )
    zoho_event_id: Mapped[Optional[str]] = mapped_column(String(128))
    stage: Mapped[str] = mapped_column(String(64), nullable=False)
    previous_stage: Mapped[Optional[str]] = mapped_column(String(64))
    status: Mapped[Optional[str]] = mapped_column(String(64))
    entered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    exited_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[Optional[int]] = mapped_column(Integer)
    duration_days: Mapped[Optional[float]] = mapped_column(Numeric(8, 2))
    performed_by: Mapped[Optional[str]] = mapped_column(String(128))
    comments: Mapped[Optional[str]] = mapped_column(Text)
    rejection_reason: Mapped[Optional[str]] = mapped_column(String(256))
    interview_outcome: Mapped[Optional[str]] = mapped_column(String(64))
    offer_status: Mapped[Optional[str]] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )


class RecruitmentActivity(Base):
    """Recruitment activity associated with candidate or application."""
    __tablename__ = "recruitment_activities"
    __table_args__ = (
        Index("ix_activities_tenant_app", "tenant_id", "application_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    application_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("applications.id", ondelete="CASCADE")
    )
    candidate_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("candidates.id", ondelete="CASCADE")
    )
    zoho_activity_id: Mapped[Optional[str]] = mapped_column(String(128))
    activity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    summary: Mapped[Optional[str]] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    performed_by: Mapped[Optional[str]] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )


class RecruitmentCost(Base):
    """Recruitment spend associated with channels, requisitions, or periods."""
    __tablename__ = "recruitment_costs"
    __table_args__ = (
        Index("ix_costs_tenant_channel", "tenant_id", "source_channel"),
        Index("ix_costs_tenant_req", "tenant_id", "job_opening_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    channel_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("source_channels.id", ondelete="SET NULL")
    )
    job_opening_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("job_openings.id", ondelete="SET NULL")
    )
    source_channel: Mapped[str] = mapped_column(String(128), nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")
    period_start: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    description: Mapped[Optional[str]] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )


class SyncLog(Base):
    """Granular log record for scheduled synchronization runs."""
    __tablename__ = "sync_logs"
    __table_args__ = (
        Index("ix_sync_logs_tenant_job", "tenant_id", "sync_job_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    sync_job_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("sync_jobs.id", ondelete="SET NULL")
    )
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    records_read: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    records_upserted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    records_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    message: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )


class AnalyticsSnapshot(Base):
    """Persisted point-in-time calculation snapshots for fast retrieval."""
    __tablename__ = "analytics_snapshots"
    __table_args__ = (
        Index("ix_snapshots_tenant_scope", "tenant_id", "cohort_start", "cohort_end"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    cohort_start: Mapped[str] = mapped_column(String(10), nullable=False)
    cohort_end: Mapped[str] = mapped_column(String(10), nullable=False)
    requisition_id: Mapped[Optional[str]] = mapped_column(String(128))
    as_of_date: Mapped[str] = mapped_column(String(10), nullable=False)
    metrics_payload: Mapped[str] = mapped_column(Text, nullable=False)  # JSON
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )


# ─── Session-Based Analytics ──────────────────────────────────────────────────


class AnalyticsSessionLock(Base):
    """Global coordination lock for session-based analytics.

    Enforces exactly one active analytics session across the entire deployment.
    """
    __tablename__ = "analytics_session_locks"

    lock_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    locked_by_session_id: Mapped[Optional[str]] = mapped_column(String(36))
    acquired_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class AnalyticsSession(Base):
    """Lifecycle and audit record for a temporary analytics session."""
    __tablename__ = "analytics_sessions"
    __table_args__ = (
        Index("ix_analytics_sessions_status", "status"),
        Index("ix_analytics_sessions_expires_at", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    session_token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="SESSION_INITIALIZING",
    )
    data_center: Mapped[str] = mapped_column(String(8), nullable=False, default="com")
    zoho_org_id: Mapped[Optional[str]] = mapped_column(String(64))
    oauth_state_hash: Mapped[Optional[str]] = mapped_column(String(64), unique=True)
    encrypted_auth_url: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    processing_started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    termination_reason: Mapped[Optional[str]] = mapped_column(String(64))

    credentials: Mapped[Optional["AnalyticsSessionCredential"]] = relationship(
        "AnalyticsSessionCredential", back_populates="session", uselist=False, cascade="all, delete-orphan"
    )


class AnalyticsSessionCredential(Base):
    """Temporary encrypted OAuth tokens bound to an active analytics session.

    Separated strictly from permanent ZohoIntegration records.
    Permanently purged on session termination, expiry, or authorization failure.
    """
    __tablename__ = "analytics_session_credentials"

    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("analytics_sessions.id", ondelete="CASCADE"), primary_key=True
    )
    encrypted_access_token: Mapped[str] = mapped_column(Text, nullable=False)
    encrypted_refresh_token: Mapped[Optional[str]] = mapped_column(Text)
    token_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )

    session: Mapped["AnalyticsSession"] = relationship(
        "AnalyticsSession", back_populates="credentials"
    )


class AnalyticsSessionAuditLog(Base):
    """Privacy-safe audit log for analytics session lifecycle events.

    Strictly avoids storing candidate PII, recruitment records,
    tokens, or raw OAuth secrets. Only sanitized operational metadata is recorded.
    """
    __tablename__ = "analytics_session_audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[Optional[str]] = mapped_column(String(36), index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    event_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    details_json: Mapped[Optional[str]] = mapped_column(Text)


