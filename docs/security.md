# Security Architecture and Policy

## 1. Overview & Trust Boundaries

The Channel Yield Analytics Agent is designed to operate in multi-tenant environments where recruitment data, analytics calculations, and third-party integrations (e.g., Zoho Recruit) are strictly isolated between client organizations.

### Trust Boundaries
1. **Public Internet / Egress Services**: Untrusted network zone. Any incoming HTTP request is considered hostile until cryptographically authenticated and mapped to an active tenant.
2. **iGentic Integration Boundary**: Untrusted ingress. While the iGentic workspace itself is restricted to authorized team members, HTTP calls originating from the iGentic Direct HTTP API traverse the public internet without proven request authentication or caller identity.
3. **Application Layer (FastAPI)**: Enforces authentication, correlation IDs, tenant scoping, role-based authorization, rate limiting, and parameter validation.
4. **Persistence Layer (PostgreSQL / SQLAlchemy)**: All tenant-owned records require explicit `tenant_id` foreign keys, compound unique constraints, and UTC timestamps. Direct database access is restricted to application services.

---

## 2. Authentication & Credential Lifecycle

### 2.1 Identity Principles
- **No Caller-Supplied Tenant Scope**: Tenant identity is NEVER accepted from request bodies, query strings, or headers (`?tenant_id=...` or `X-Tenant-ID`). Tenant identity is exclusively resolved from server-verified cryptographic tokens or database-hashed API keys.
- **Clear Identity Separation**:
  - **Human Identity**: Represented by a `User` record, authenticated via JWT access tokens.
  - **Machine / Integration Identity**: Represented by an `ApiKey` record, authenticated via bearer `cyk_...` tokens.
  - **Tenant Identity**: Represented by a `Tenant` record, linked via active `Membership` or `ApiKey` associations.
  - **Provider Connection Identity**: Represented by encrypted third-party credentials (`ZohoIntegration`), never shared between tenants.

### 2.2 Token Lifetime and Algorithms
- **JWT Algorithm**: `HS256` (HMAC SHA-256) in prototype/staging, configurable to `RS256` in enterprise SSO deployments.
- **Access Token Expiry**: Default 30 minutes (`CY_ACCESS_TOKEN_EXPIRE_MINUTES`). Tokens are short-lived.
- **Signing Key (`CY_SECRET_KEY`)**: Must be at least 32 characters (256 bits of entropy). In production mode, the application immediately aborts startup if the secret key is shorter than 32 characters or set to a placeholder.

### 2.3 API Keys (Machine Credentials)
- Format: `cyk_<32-byte-base64-random>`
- Storage: Plaintext is shown **once** upon creation and discarded. The server stores only `key_prefix` (first 8 characters for identification) and `key_hash` (SHA-256 hex digest).
- Lookup: Constant-time comparison using `hmac.compare_digest` against the database hash.
- Revocation: Immediate status update (`is_active = False`). Expired or revoked keys return `HTTP 401 Unauthorized`.

### 2.4 User Provisioning, Membership, and Administrative Bootstrap
- **No Insecure Public Self-Registration**: There is no unauthenticated endpoint that permits creating arbitrary tenants or administrator accounts.
- **Administrative Bootstrap**: Initial tenant and owner accounts are created exclusively via administrative CLI commands or controlled deployment scripts requiring direct database/host access (`CY_ADMIN_BOOTSTRAP_TOKEN` or local CLI invocation).
- **Tenant Membership**: Users belong to tenants through `Membership` records with explicit roles (`owner`, `admin`, `analyst`, `viewer`).

### 2.5 Role-Permission Matrix

| Operation | Minimum Role | Viewer | Analyst | Admin | Owner | Machine API Key (`cyk_...`) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| **Read Aggregate Analytics** (`/v2/analytics`) | `viewer` | Yes | Yes | Yes | Yes | Yes |
| **Execute Agent Queries** (`/v2/agent/query`) | `analyst` | No | Yes | Yes | Yes | Yes (default role) |
| **List API Keys** (`GET /v2/auth/api-keys`) | `admin` | No | No | Yes | Yes | No |
| **Issue API Keys** (`POST /v2/auth/api-keys`) | `admin` | No | No | Yes | Yes | No |
| **Revoke API Keys** (`DELETE /v2/auth/api-keys/{id}`) | `admin` | No | No | Yes | Yes | No |
| **Run Data Ingestion Jobs** (`/v2/ingestion/*`) | `admin` | No | No | Yes | Yes | No |
| **Manage Zoho Integration** (`/v2/integrations/*`) | `admin` | No | No | Yes | Yes | No |
| **List Organization Members** (`GET /v2/auth/members`) | `admin` | No | No | Yes | Yes | No |
| **Provision / Invite Members** (`POST /v2/auth/members`) | `owner` | No | No | No | Yes | No |

### 2.6 Authentication Endpoint Contracts & Behavior

* **`POST /v2/auth/token`**:
  * Body: `{"email": "...", "password": "...", "tenant_slug": "..."}` (`tenant_slug` required only if email exists in multiple tenants).
  * Success (`200 OK`): `{"access_token": "...", "token_type": "bearer", "expires_in": 1800, "tenant_id": "...", "role": "..."}`.
  * Invalid credentials: Returns `401 Unauthorized` without revealing whether the email exists.
* **`GET /v2/auth/me`**:
  * Header: `Authorization: Bearer <token_or_cyk_key>`.
  * Success (`200 OK`): Returns caller's user ID, email, tenant ID, tenant name, slug, and role.
* **`POST /v2/auth/api-keys`**:
  * Header: `Authorization: Bearer <jwt_admin_or_owner>`.
  * Body: `{"name": "Runner Name"}`.
  * Success (`201 Created`): Returns key metadata and plaintext `api_key` (displayed only once).
* **`GET /v2/auth/api-keys`**:
  * Header: `Authorization: Bearer <jwt_admin_or_owner>`.
  * Success (`200 OK`): List of tenant keys (prefix, name, active status, creation/last-used dates; never secret).
* **`DELETE /v2/auth/api-keys/{id}`**:
  * Header: `Authorization: Bearer <jwt_admin_or_owner>`.
  * Scoped strictly to caller's `tenant_id`. If key belongs to another tenant, returns `404 Not Found` (never leaks foreign tenant key existence).
* **`POST /v2/auth/members`**:
  * Header: `Authorization: Bearer <jwt_owner>`.
  * Body: `{"email": "...", "full_name": "...", "password": "...", "role": "viewer|analyst|admin|owner"}`.
  * Success (`201 Created`): Creates tenant member with hashed password.

### 2.7 Tenant Isolation & Anti-Enumeration Strategy
1. **Server-Derived Context**: All database operations query by `Model.tenant_id == ctx.tenant_id`.
2. **Defensive Non-Enumeration**: If an actor queries, revokes, or deletes an ID belonging to a different tenant, the backend returns a generic `404 Not Found` rather than `403 Forbidden`, preventing attackers from enumerating resources across organizations.
3. **Token Tampering Rejection**: If a JWT is constructed with a valid user ID but a foreign `tid`, the membership lookup `(Membership.tenant_id == tid, Membership.user_id == sub)` returns empty, failing closed with `403 Access Denied`.

### 2.8 Revocation Lifecycle & Brute-Force Rate Limiting
1. **Immediate Revocation via Database-Backed Verification**:
   - Although JWTs are cryptographically signed, the backend dependency (`get_authenticated_context`) re-verifies `User.is_active`, `Tenant.is_active`, and active `Membership` in the database on **every single request**.
   - If an employee is offboarded (`User.is_active = False`), their membership removed, or the tenant suspended (`Tenant.is_active = False`), any unexpired JWT held by that actor is **immediately rejected with `401 Unauthorized` on the next HTTP request**.
   - No token blacklist or distributed cache expiration lag is required for immediate offboarding.
2. **Machine API Key Revocation**:
   - Calling `DELETE /v2/auth/api-keys/{id}` immediately sets `ApiKey.is_active = False`.
   - The key is blocked instantaneously across all running backend instances.
3. **Login Brute-Force Rate Limiting**:
   - `POST /v2/auth/token` implements thread-safe sliding-window rate limiting.
   - 5 failed login attempts within 300 seconds (5 minutes) from the same IP or targeting the same email triggers an immediate `HTTP 429 Too Many Requests` with a `Retry-After: 300` header.
   - Successful authentication immediately clears the failure counter for that IP and account.
4. **CORS Security**:
   - Wildcard origin (`*`) is strictly rejected by `Settings.production_guards` in production when credentials (`allow_credentials=True`) are enabled, preventing cross-origin token harvesting.

---

## 3. iGentic Integration Status: BLOCKED

### 3.1 Status
**Production Integration Status**: **`BLOCKED`**

### 3.2 Constraint and Threat Model
The iGentic Direct HTTP API tool currently cannot validate authentication or inject custom authentication mechanisms (`Authorization: Bearer`, custom headers, API keys, OAuth client credentials, or request signing).

- **Why it is BLOCKED**: If the backend opened an unauthenticated or secret-URL endpoint to receive iGentic requests, any party on the public internet discovering or guessing the URL would gain unauthorized access to private recruitment metrics and data.
- **Workspace Access is Not Request Proof**: The fact that the iGentic workspace requires authorized logins does not cryptographically authenticate HTTP requests emitted by its runners over the public internet.
- **Unacceptable Workarounds**:
  - Exposing an unauthenticated public route: **REJECTED**.
  - Accepting caller-supplied tenant IDs without credentials: **REJECTED**.
  - Trusting unverified source IP addresses on shared cloud infrastructure: **REJECTED**.
  - Putting API secrets in query parameters (which leak in logs and proxies): **REJECTED**.

### 3.3 Minimum Unblocking Requirements
To unblock production integration, one of the following must be provided and verified:
1. **Custom Headers Support**: Ability to inject `Authorization: Bearer <cyk_token>` in iGentic Direct HTTP API requests.
2. **Standard OAuth 2.0 Client Credentials**: Ability for iGentic to negotiate a short-lived bearer token from our token endpoint.
3. **Private Network Interconnect**: Deployment inside a private VPC with PrivateLink, Cloudflare Tunnel, or mutual TLS (mTLS) with client certificates.

---

## 4. Data Protection & Secrets Management

### 4.1 Encryption at Rest
- Third-party OAuth tokens (Zoho Recruit access and refresh tokens) are encrypted prior to database insertion using Fernet symmetric encryption (`CY_ENCRYPTION_KEY`).
- In production, missing `CY_ENCRYPTION_KEY` halts application startup.

### 4.2 Logging & Secret Scrubbing
- Structured JSON logging emits machine-readable events to `stdout`.
- The logging formatter scrubs all keys matching `_SCRUB_KEYS`:
  `password`, `secret`, `token`, `api_key`, `access_token`, `refresh_token`, `authorization`, `client_secret`, `encryption_key`, `secret_key`.
- Request URLs in access logs are stripped of query parameters to prevent leakage of potential filters or sensitive identifiers.

### 4.3 Safe Error Responses
- The application implements global exception handlers.
- Unhandled internal exceptions return a standard generic error payload:
  ```json
  {
    "error": "internal_error",
    "message": "An unexpected error occurred. Please contact support with the request ID.",
    "request_id": "req-..."
  }
  ```
- Stack traces, database schema definitions, SQL queries, and internal file paths are strictly prevented from appearing in API responses.

### 4.4 Request Controls
- **Request Size Limiting**: Request bodies exceeding `CY_MAX_UPLOAD_BYTES` (default 50 MB) or non-upload bodies exceeding 64 KB are rejected with `HTTP 413 Payload Too Large`.
- **Correlation IDs**: All requests are assigned a validated `X-Request-ID` (sanitized or generated via UUID4), returned in response headers and injected into log context.
