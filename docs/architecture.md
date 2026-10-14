# System Architecture: Channel Yield Analytics Agent

## 1. System Overview

The Channel Yield Analytics Agent provides deterministic, mathematically authoritative recruitment channel performance analytics for multi-tenant enterprise environments.

### Core Architecture Components
1. **Core API Service (`FastAPI`)**: High-performance asynchronous API providing authentication, tenant isolation, health/readiness endpoints, correlation tracking, and safe error serialization.
2. **Deterministic Analytics Engine (`analytics_service.py` & `yield_calculation.py`)**: Computes cohort-based channel yields, hire rates, mature candidate masks, sample size thresholds, and data quality warnings.
3. **Multi-Tenant Persistence Layer (`SQLAlchemy` / `PostgreSQL` / `Alembic`)**: Strong schema enforcement with compound tenant foreign keys, unique source-system constraints, and encrypted credential storage.
4. **Data Ingestion Service**: Validates CSV imports, enforces schema compliance, executes duplicate detection and conflict logging, and maintains durable `ImportJob` lifecycle states.
5. **Integration Service (Zoho Recruit)**: Manages OAuth 2.0 handshakes, CSRF state verification, Fernet-encrypted token storage, and candidate synchronization.
6. **Controlled Agent Interface**: Constrained tool dispatcher executing only allowlisted, server-scoped analytics functions with validated input schemas.

---

## 2. Component Architecture Diagram

```
                              ┌────────────────────────────────────────┐
                              │            Clients / UIs               │
                              │  • Web App (Browser with JWT)          │
                              │  • CI / Machine Integrations (API Key) │
                              │  • [iGentic BLOCKED for Production]    │
                              └───────────────────┬────────────────────┘
                                                  │ HTTPS
                                                  ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                                FastAPI Backend Service                                 │
│                                                                                        │
│  ┌──────────────────────────────────────────────────────────────────────────────────┐  │
│  │ Middleware Layer:                                                                │  │
│  │   • CorrelationIdMiddleware (X-Request-ID propagation & sanitized logging)       │  │
│  │   • RequestSizeLimitMiddleware (Body size boundary checking, 413 on breach)      │  │
│  │   • SafeExceptionMiddleware (Uniform error envelopes, scrubbed tracebacks)        │  │
│  └──────────────────────────────────────────────────────────────────────────────────┘  │
│                                                                                        │
│  ┌──────────────────────────────────────────────────────────────────────────────────┐  │
│  │ Router Surface:                                                                  │  │
│  │   • /health               (Liveness probe: process responsiveness)               │  │
│  │   • /ready                (Readiness probe: DB connectivity & schema status)     │  │
│  │   • /v2/auth/*            (Login, token exchange, API key management)           │  │
│  │   • /v2/analytics/*       (Multi-tenant cohort yield queries)                    │  │
│  │   • /v2/ingestion/*       (CSV upload & durable ImportJob tracking)              │  │
│  │   • /v2/integrations/*    (Zoho Recruit OAuth & sync status)                     │  │
│  │   • /v2/agent/*           (Controlled, allowlisted agent tool execution)         │  │
│  │   • /v1/analytics         (Legacy backward-compat single-tenant route)           │  │
│  └────────────────────────────────────────┬─────────────────────────────────────────┘  │
│                                           │                                            │
│                     ┌─────────────────────┴─────────────────────┐                      │
│                     ▼                                           ▼                      │
│         ┌───────────────────────┐                   ┌───────────────────────┐          │
│         │   Analytics Adapter   │                   │    Tenancy Service    │          │
│         │ (app/analytics/       │                   │ (app/tenancy/         │          │
│         │  adapter.py)          │                   │  service.py)          │          │
│         └───────────┬───────────┘                   └───────────┬───────────┘          │
│                     │                                           │                      │
└─────────────────────┼───────────────────────────────────────────┼──────────────────────┘
                      │                                           │
                      ▼                                           ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                              Persistence Layer                                         │
│                                                                                        │
│   • Tenants (organizations, active status, creation dates)                             │
│   • Users & Memberships (bcrypt hashed passwords, role levels: owner/admin/analyst)    │
│   • ApiKeys (SHA-256 hashed machine tokens with prefix identification)                 │
│   • CanonicalRecords (tenant_id, entity_type, entity_id, fingerprint, payload)         │
│   • ImportJobs & SyncJobs (durable job state machines, counts, error summaries)        │
│   • ZohoIntegrations (Fernet-encrypted access/refresh tokens, status, datacenter)      │
│   • AuditEvents (immutable logs of actors, operations, outcomes, IP addresses)         │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Database Schema & Migration Strategy

- **Tooling**: Alembic manages all database migrations (`migrations/` directory).
- **Naming Conventions**:
  - Foreign keys: `fk_<table>_<column>_<target>`
  - Unique constraints: `uq_<table>_<columns>`
  - Indexes: `ix_<table>_<columns>`
- **Isolation Enforcement**: Every table storing client data has an explicit `tenant_id` foreign key with `ondelete="CASCADE"`. Queries at the repository layer MUST enforce `where(Model.tenant_id == ctx.tenant_id)`.
- **Credential Storage**: Passwords use bcrypt with adaptive salt cost; API keys store only SHA-256 hashes; OAuth tokens are encrypted symmetrically with Fernet.

---

## 4. Controlled Agent Tool Architecture

Rather than granting an LLM or autonomous agent direct database access or generic REST execution capabilities, the agent interface exposes strictly typed, server-authorized tool wrappers:

1. **Explicit Whitelist**: The agent can only call registered tools (`query_channel_yield`, `query_requisition_summary`).
2. **Server-Enforced Scope**: The agent prompt or caller cannot override the `tenant_id`. The server injects the authenticated caller's verified `tenant_id` into the tool parameters.
3. **No Arbitrary Capabilities**: Zero SQL execution, zero shell commands, zero dynamic code evaluation, zero unauthenticated external URL fetching.
4. **Structured Results**: Tool execution returns strictly validated Pydantic models matching canonical contracts.

---

## 5. Ingestion Pipeline & Durable Job Model

The ingestion architecture provides reliable, repeatable data ingestion from CSV files and external integrations (Zoho Recruit):

### Execution Pipeline & Invariants
1. **Input Normalization**:
   - Header validation: non-empty, non-blank, no duplicate headers.
   - Field mappings: translates source column names to canonical schema attributes.
   - String stripping and empty string normalization to `None`.
2. **Row Validation & Quarantining**:
   - Checks required fields per entity type.
   - Normalizes timestamps to UTC ISO-8601.
   - Quarantines records with missing join evidence (`outcome == 'joined'` without `joined_at`) or invalid ordering (`joined_at < applied_at`).
   - Flags unknown/unattributed sourcing channels as warnings without quarantining valid applications.
3. **Idempotency & Replay Detection**:
   - Computes deterministic SHA-256 fingerprint over canonical fields (excluding run-specific metadata).
   - Replays matching existing fingerprints are recorded as `unchanged_replays` without mutating the database.
   - Changed source records update canonical payloads and timestamps (`updated`).
   - Conflicting canonical IDs from different source keys are quarantined (`conflicts`).
4. **Transactional Rollback on Semantic Validation Failure**:
   - The entire candidate dataset (existing + newly staged records) is evaluated using `validate_bundle()` within the active transaction.
   - If semantic validation fails, the entire transaction rolls back; the last successful canonical dataset remains completely intact.
   - The `ImportJob` is marked `failed_validation` with sanitized error summaries.

### Why a Database-Backed Durable Job Model
Rather than introducing external message queues (Celery, RabbitMQ, Redis) that increase infrastructure surface area and operational complexity:
- `ImportJob` and `SyncJob` tables maintain durable lifecycle state machines (`pending`, `running`, `completed`, `completed_with_warnings`, `completed_with_quarantine`, `failed`, `failed_validation`).
- The database enforces active job concurrency locks: an active job (`status == 'running'`) rejects duplicate concurrent import requests with `HTTP 409 Conflict`.
- Import histories, row-level metrics, and sanitized error summaries remain permanently auditable across system restarts.

---

## 6. Zoho Recruit Integration Architecture

The Zoho Recruit adapter enables automated on-demand synchronization while preserving complete tenant data isolation:

1. **OAuth 2.0 with CSRF Protection**:
   - Initiates authorization with unpredictable 32-byte cryptographic state tokens (`OAuthState`) expiring in 15 minutes.
   - Exchanges authorization codes for offline refresh tokens and short-lived access tokens.
2. **Fernet Credential Encryption**:
   - Access and refresh tokens are symmetrically encrypted via Fernet before database insertion.
   - Raw tokens are never logged, never returned in API responses, and never printed in error tracebacks.
3. **Multi-Datacenter Routing**:
   - Configurable per-tenant routing across official Zoho data centers: `.com` (US), `.eu` (EU), `.in` (India), `.com.au` (Australia), `.jp` (Japan), and `.ca` (Canada).
4. **Resilient Pagination & Rate Limiting**:
   - Paginated record retrieval using `page` and `per_page` query parameters up to safety boundaries, checking `info.more_records`.
   - Rate limit responses (HTTP 429) are trapped and mapped to typed `RateLimitError` honoring `Retry-After` headers.
5. **Sandbox Isolation Policy**:
   - Live integration remains disabled until an authorized sandbox connection is explicitly verified. Tests use deterministic synthetic mocks (`MockZohoHttpClient`).
