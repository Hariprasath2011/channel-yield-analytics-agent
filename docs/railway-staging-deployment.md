# Railway Staging Deployment & Operational Guide
**Channel Yield Analytics Agent — Isolated Staging Environment**

This guide documents the configuration, deployment, migration, and verification procedures for migrating the **Channel Yield Analytics Agent Staging Environment** from Render to **Railway**.

---

## 1. Staging Architecture & Isolation Boundary

`[ARCHITECTURE SPECIFICATION: CODE AUDIT & PLANNED RAILWAY TOPOLOGY]`

The staging environment on Railway is planned to be strictly decoupled from production:

| Component | Staging Resource Name | Production Counterpart | Isolation Mechanism | Verification Status |
| :--- | :--- | :--- | :--- | :--- |
| **Git Branch** | `staging` | `main` | Separate Git branch; merges to `main` occur only after staging verification. | **Verified Fact** (Commit `81e31500`) |
| **PostgreSQL Database** | `Postgres` (Railway plugin) | Production PostgreSQL | Isolated database instance; production data is never touched or cloned. | **Planned — pending Railway provisioning and live isolation verification.** |
| **Backend REST API** | `backend-staging` | `channel-yield-backend` | Dedicated public HTTPS domain: `https://<backend-staging>.up.railway.app` | **Planned — pending Railway service creation and domain generation** |
| **Custom MCP Server** | `mcp-staging` | `zoho-recruit-mcp` | Dedicated public HTTPS domain: `https://<mcp-staging>.up.railway.app` | **Planned — pending Railway service creation and domain generation** |
| **Sync Cron Worker** | `worker-staging` | `channel-yield-sync-worker` | **Intentionally not provisioned during initial staging rollout.** Dormancy verified in code (`CY_SYNC_WORKER_ENABLED="false"`). | **Deferred from Initial Provisioning** |
| **Zoho OAuth App** | Dedicated Staging Client ID | Production Client ID | Dedicated staging redirect URI registered in Zoho Developer Console. | **Planned — pending live config & Zoho registration** |
| **Cryptographic Keys** | Staging Fernet & JWT Keys | Production Keys | Independently generated 256-bit entropy secrets; zero production-secret reuse. | **Locally Tested** (Independent key generation & crypto verified) |

```mermaid
flowchart TD
    subgraph Railway Staging Environment: channel-yield-staging [Railway Staging Environment: channel-yield-staging (Planned)]
        subgraph Postgres Plugin [Postgres (Planned Plugin)]
            DB[(PostgreSQL 16\nDatabase: railway\nPrivate Network)]
        end

        subgraph Service 1 [backend-staging (Planned Initial Service)]
            Backend["FastAPI App\nBuild: pip install -r requirements.txt\nStart: uvicorn app.main:app --host 0.0.0.0 --port $PORT\nHealthcheck: /health"]
            SweeperB["Session Sweeper\n(Reads CY_MCP_ENABLE_BACKGROUND_SWEEPER)"]
        end

        subgraph Service 2 [mcp-staging (Planned Initial Service)]
            MCP["Starlette MCP\nBuild: pip install -r requirements.txt\nStart: python -m mcp_server.server --host 0.0.0.0 --port $PORT\nHealthcheck: /health"]
            SweeperM["Session Sweeper\n(Starts Unconditionally on Lifespan)"]
        end

        subgraph Service 3 [worker-staging (Deferred Service)]
            Worker["app.worker --once\n(INTENTIONALLY NOT PROVISIONED IN INITIAL ROLLOUT)"]
        end

        Backend -->|Planned Private IPv6 Mesh\n${{Postgres.DATABASE_PRIVATE_URL}}| DB
        MCP -->|Planned Private IPv6 Mesh\n${{Postgres.DATABASE_PRIVATE_URL}}| DB
        Worker -.->|Not Provisioned| DB
    end

    Client([iGentic Staging Agent]) -->|Proposed Initial: /sse/{key} (Pending Live Verification)\nOptional Stateless: /mcp/{key}| MCP
    User([User / Browser]) -->|OAuth Authorize / Callback| Backend
    Zoho([Zoho Accounts]) -->|OAuth Redirect /v2/...| Backend
```

### Security Warning: Human-Identity & Single-Session Constraints

> [!WARNING]
> **Human-Identity & Single-Session Constraints:**
> 1. **Singleton Session Lock:** The database-backed session lock (`analytics_session_locks` and advisory locks in `SessionManager`) strictly enforces **one active analytics session globally across the entire cluster**.
> 2. **Shared Capability Key:** The MCP capability URL/key (`CY_MCP_SECRET_PATH_KEY`, configured exclusively on `mcp-staging`) protects endpoint access but does **not** independently authenticate individual iGentic users or distinguish between distinct human operators (the backend neither consumes nor requires this key).
> 3. **Trusted Operator Restriction:** The staging pilot must be strictly restricted to **trusted operators**.
> 4. **No Per-User Identity:** This implementation **must not be represented as providing per-user iGentic identity or production-grade multi-user isolation**.

---

## 2. Railway Dashboard Service Configuration (Initial Rollout)

`[PLANNED RAILWAY DASHBOARD CONFIGURATION (PENDING PROVISIONING)]`

Configure each service independently in the Railway Dashboard under project `channel-yield-staging`.

> [!IMPORTANT]
> **No root `railway.json` is used.** Because this repository hosts multiple distinct entry points (FastAPI REST API, Starlette MCP server), defining settings per service in the Railway Dashboard prevents configuration conflicts and eliminates accidental build-time migration execution.

### Service 1: `Postgres` (PostgreSQL Database Plugin)
1. In the Railway project, click **+ New** $\to$ **Database** $\to$ **Add PostgreSQL**.
2. Service name: `Postgres`.
3. Note the planned reference variable: `${{Postgres.DATABASE_PRIVATE_URL}}` (unverified until provisioned).

---

### Service 2: `backend-staging` (FastAPI REST API)
1. Click **+ New** $\to$ **GitHub Repo** $\to$ select `Hariprasath2011/channel-yield-analytics-agent`.
2. Under **Settings**:
   - **Service Name:** `backend-staging`
   - **Source Repo Branch:** `staging`
   - **Root Directory:** `/`
   - **Build Command:** `pip install -r requirements.txt` *(dependencies only; no Alembic)*
   - **Start Command:** `uvicorn app.main:app --host 0.0.0.0 --port $PORT` `[LOCALLY TESTED COMMAND]`
   - **Healthcheck Path:** `/health`
   - **Healthcheck Timeout:** `15` seconds
   - **Restart Policy:** `Always`
3. Under **Networking**:
   - Click **Generate Domain** (e.g. `backend-staging-production-xxxx.up.railway.app`). `[PLANNED STEP — DOMAIN UNVERIFIED UNTIL GENERATED]`

---

### Service 3: `mcp-staging` (Zoho Recruit MCP Server)
1. Click **+ New** $\to$ **GitHub Repo** $\to$ select `Hariprasath2011/channel-yield-analytics-agent`.
2. Under **Settings**:
   - **Service Name:** `mcp-staging`
   - **Source Repo Branch:** `staging`
   - **Root Directory:** `/`
   - **Build Command:** `pip install -r requirements.txt` *(dependencies only; no Alembic)*
   - **Start Command:** `python -m mcp_server.server --host 0.0.0.0 --port $PORT` `[LOCALLY TESTED COMMAND]`
   - **Healthcheck Path:** `/health`
   - **Healthcheck Timeout:** `15` seconds
   - **Restart Policy:** `Always`
3. Under **Networking**:
   - Click **Generate Domain** (e.g. `mcp-staging-production-xxxx.up.railway.app`). `[PLANNED STEP — DOMAIN UNVERIFIED UNTIL GENERATED]`

---

### Worker Service Status: Intentionally Not Provisioned
> [!NOTE]
> The scheduled sync worker (`worker-staging`) is **intentionally omitted from the initial staging rollout**. It must not be created or deployed until periodic background synchronization is explicitly authorized. See Section 8 for verified dormancy semantics and future provisioning requirements.

---

## 3. Environment Variables & Cross-Service References

`[CODE AUDITED SPECIFICATION & PLANNED RAILWAY CONFIGURATION]`

### Key Sharing Between Staging Services vs Zero Production-Secret Reuse
* **Cross-Service Sharing (Staging Only):** `backend-staging` and `mcp-staging` must share the **exact same staging cryptographic keys** (`CY_SECRET_KEY` and `CY_ENCRYPTION_KEY`) so that the MCP server can decrypt session credentials created during backend OAuth flows and verify HMAC tokens.
* **Planned Railway Variable Reference Syntax:** Use `${{backend-staging.CY_SECRET_KEY}}` and `${{backend-staging.CY_ENCRYPTION_KEY}}` in `mcp-staging` to prevent key divergence (unverified on Railway until deployment).
* **Strict Production Boundary (Zero Production-Secret Reuse):** These keys must be **newly generated 32-byte staging keys**. Never copy, import, or reuse keys from production Render services.

### Sweeper Process Differentiation (`CY_MCP_ENABLE_BACKGROUND_SWEEPER`)
* **`backend-staging` (FastAPI):** Reads `CY_MCP_ENABLE_BACKGROUND_SWEEPER` in `app/main.py: L135`. When set to `"true"` (and `CY_ENV != "testing"`), it invokes `SessionManager.start_background_cleanup_task()`. Setting this variable to `"false"` disables the sweeper in the backend process.
* **`mcp-staging` (Starlette MCP):** Starts its background sweeper **unconditionally** during ASGI startup lifespan in `mcp_server/server.py: L1254`. It does **not** read `CY_MCP_ENABLE_BACKGROUND_SWEEPER`.
* **Cluster Concurrency Behavior:** Both processes call `SessionManager.start_background_cleanup_task()`. Concurrency is managed at two levels:
  1. *In-Process:* A thread check (`_cleanup_thread.is_alive()`) ensures only one thread per process.
  2. *Cross-Process / Cross-Container:* The cleanup transaction acquires a PostgreSQL transaction-level advisory lock (`pg_try_advisory_xact_lock(8492049182938491)`). The cleanup transaction holds this lock for the entire cleanup operation, and the lock is released when the transaction ends (upon commit or rollback). The lock prevents concurrent cleanup execution while held; it does not prevent separate cleanup cycles from running sequentially across services.

### Environment Variable Matrix

| Variable Name | `backend-staging` | `mcp-staging` | `worker-staging` *(Deferred)* | Source / Type | Status & Description |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `PORT` | Auto | Auto | N/A | Railway Injected | **Planned Platform Injected Variable:** Bound dynamically by Uvicorn & MCP CLI (CLI port binding locally tested). |
| `CY_ENV` | `staging` | `staging` | *(staging)* | Manual Config | **Code Audited:** Bypasses production-only startup guards when set to staging. |
| `CY_DATABASE_URL` | `${{Postgres.DATABASE_PRIVATE_URL}}` | `${{Postgres.DATABASE_PRIVATE_URL}}` | *(${{Postgres.DATABASE_PRIVATE_URL}})* | Railway Reference | **Planned Railway Reference:** Syntax pending live resolution in Railway. Code audited: application and migrations normalize to `postgresql+psycopg2://`. |
| `CY_SECRET_KEY` | *(Generate 32B hex)* | `${{backend-staging.CY_SECRET_KEY}}` | — | Manual / Reference | **Code Audited & Locally Tested:** 32-byte hex key for JWT & HMAC token signing. Cross-service reference unverified on Railway. |
| `CY_ENCRYPTION_KEY` | *(Generate Fernet key)* | `${{backend-staging.CY_ENCRYPTION_KEY}}` | *(Re-audit required)* | Manual / Reference | **Code Audited & Locally Tested:** 32-byte Fernet key for encrypting temporary credentials. Shared between backend and MCP. |
| `CY_MCP_ENABLE_BACKGROUND_SWEEPER` | `true` | *(Ignored)* | — | Manual Config | **Code Audited:** Read **only by backend-staging** (`app/main.py`). The `mcp-staging` server starts its sweeper **unconditionally** via its lifespan (`mcp_server/server.py`). |
| `CY_MCP_HOST` | — | `0.0.0.0` | — | Manual Config | **Code Audited & Locally Tested:** Binds MCP server across all network interfaces. |
| `CY_MCP_SECRET_PATH_KEY` | — | *(Generate 32B hex)* | — | Manual Config | **Code Audited & Locally Tested:** Capability key protecting `/sse/<key>` and `/mcp/<key>`. **Consumed exclusively by `mcp-staging`**; the backend does not consume, require, or validate this variable. |
| `CY_ALLOWED_ORIGINS` | `[UNVERIFIED PLACEHOLDER]` | — | — | Manual Config | **Unverified Placeholder:** Replace with confirmed staging agent origin(s). Do not deploy with unconfirmed domains. |
| `CY_ZOHO_CLIENT_ID` | Pending | Pending | — | Manual (Pending) | **Pending Live Config:** Dedicated Staging Zoho OAuth Client ID. |
| `CY_ZOHO_CLIENT_SECRET` | Pending | Pending | — | Manual (Pending) | **Pending Live Config:** Dedicated Staging Zoho OAuth Client Secret. |
| `CY_ZOHO_REDIRECT_URI` | `https://<backend-domain>/v2/integrations/zoho/callback` | Same | — | Manual (Pending) | **Pending Live Config:** Full staging OAuth callback URL (domain unverified until generated). |
| `CY_ZOHO_DATA_CENTER` | `in` (or `com`) | `in` (or `com`) | — | Manual Config | **Target Account Specific:** Set `in` if staging account is hosted in India (`zoho.in`). Set `com` if US (`zoho.com`). Do not assume `com`. |
| `CY_SYNC_WORKER_ENABLED` | — | — | `false` | **Safety Guard** | **Locally Tested:** Must remain `"false"` if worker service is ever configured. |
| `CY_LOG_LEVEL` | `INFO` | `INFO` | *(INFO)* | Manual Config | **Code Audited:** Controls structured log verbosity. |
| `NIXPACKS_PYTHON_VERSION` | `3.11` | `3.11` | *(3.11)* | Build Hint | **Planned Build Hint:** Instructs Nixpacks to pin Python runtime to 3.11. |

### Local Staging Key Generation Commands
Generate fresh staging secrets in your local terminal (do not commit them to Git):
```bash
# 1. Staging JWT Secret Key (CY_SECRET_KEY):
python -c "import secrets; print(secrets.token_hex(32))"

# 2. Staging Fernet Key (CY_ENCRYPTION_KEY):
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# 3. Staging MCP Capability Path Key (CY_MCP_SECRET_PATH_KEY - MCP Only):
python -c "import secrets; print(secrets.token_hex(32))"
```

---

## 4. Railway Private Networking Configuration

`[PLANNED ARCHITECTURE / CODE AUDIT & PENDING LIVE VERIFICATION]`

Railway documentation specifies a private IPv6 overlay mesh network across all services within a project.

> [!NOTE]
> While internal database URL normalization has been verified in code and tests, Railway internal DNS resolution (`postgres.railway.internal`), mesh connectivity, and variable resolution are **planned platform capabilities that remain unverified until live deployment**.

### How It Operates
1. **Planned Private Hostname:** When the PostgreSQL database plugin is provisioned, Railway assigns it internal DNS hostname `postgres.railway.internal`.
2. **Planned Private URL Variable:** Railway exposes this via `${{Postgres.DATABASE_PRIVATE_URL}}`:
   ```text
   postgresql://postgres:<password>@postgres.railway.internal:5432/railway
   ```
3. **Verified URL Normalization (Code Audited & Locally Tested):** Both `app/persistence/database.py` and `migrations/env.py` contain `_normalize_url()`, which automatically converts `postgresql://` and `postgres://` to `postgresql+psycopg2://` at runtime.
4. **Planned Security & Zero Ingress (Pending Live Verification):**
   - Database port `5432` is planned to remain unexposed to the public internet.
   - Traffic between `backend-staging`, `mcp-staging`, and `Postgres` travels within Railway's encrypted private mesh.
   - Live isolation must be verified after provisioning.

---

## 5. Railway-Side Database Migration Procedure

`[CODE AUDITED & PLANNED RAILWAY PROCEDURE]`

> [!CAUTION]
> **Prohibition of Production Credentials:**  
> Never connect, input, or reference production Render database credentials in this staging environment. All migrations must target the isolated Railway staging database `${{Postgres.DATABASE_PRIVATE_URL}}`.

### Migration Lineage & Target Table Inventory
Inspected from [migrations/versions/a3c4e5f6a7b8_session_based_analytics.py](migrations/versions/a3c4e5f6a7b8_session_based_analytics.py):
- **Base Schema:** `81e098bcb28a` (`initial_schema`)
- **Parent Revision:** `92f1a8c3d4e5` (`recruitment_relational_models`)
- **Target Head Revision:** `a3c4e5f6a7b8` (`session_based_analytics`)
- **Complete Session Table Inventory (All 4 Tables):**
  1. `analytics_session_locks`: Singleton coordinator and cluster advisory lock row (`global_analytics_session`).
  2. `analytics_sessions`: Core session lifecycle, status tracking, timestamps, and partial unique constraint `uq_analytics_sessions_single_active`.
  3. `analytics_session_credentials`: Isolated, encrypted storage for temporary Zoho OAuth tokens (cascading delete on session removal).
  4. `analytics_session_audit_logs`: Privacy-safe audit trail recording lifecycle events without PII or raw secrets.

### Verified Environment Variables for Migration Runner
Inspected from [migrations/env.py#L41-L79](migrations/env.py#L41-L79):
- **Single Required Variable:** `CY_DATABASE_URL` (Priority 3 in `get_url()`). When set, `_is_valid_url` validates the string and `_normalize_url` ensures the `postgresql+psycopg2://` dialect prefix.
- **Optional Variable:** `CY_ENV` (defaults to development if omitted; setting `CY_ENV=staging` is recommended for log consistency).
- **No other variables are required or should be invented.** Alembic executes head-to-tail migrations using only the normalized database connection string.

---

### Procedure A: Dedicated Railway One-Off Migration Runner (Recommended — Zero Local CLI Dependency)

This method runs entirely inside Railway's private network using the Railway web dashboard with safeguards against unintentional reruns:

1. **Verify Isolation Pre-Check:** Confirm that the Railway project is `channel-yield-staging` and that `Postgres` is the staging database.
2. Click **+ New** $\to$ **GitHub Repo** $\to$ select `Hariprasath2011/channel-yield-analytics-agent`.
3. Under **Settings**:
   - **Service Name:** `staging-migration-runner`
   - **Branch:** `staging`
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `alembic upgrade head`
   - **Restart Policy:** Set strictly to **Never** (or max retries: `0`). This ensures the container will **not** restart or loop once Alembic finishes.
   - **Auto-Deploy on Push:** Ensure automatic deployment is **toggled OFF / disabled** so future Git pushes do not re-execute migrations.
4. Under **Variables**:
   - Add `CY_DATABASE_URL`: `${{Postgres.DATABASE_PRIVATE_URL}}`
   - Add `CY_ENV`: `staging`
5. Click **Deploy**.
6. **Verify Migration Completion in Logs:**
   Observe the deployment log according to your target database scenario:

   **Scenario 1: Fresh Empty Staging Database (Default):**
   The runner applies all three migrations from scratch:
   ```text
   INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.
   INFO  [alembic.runtime.migration] Will assume transactional DDL.
   INFO  [alembic.runtime.migration] Running upgrade  -> 81e098bcb28a, initial_schema
   INFO  [alembic.runtime.migration] Running upgrade 81e098bcb28a -> 92f1a8c3d4e5, recruitment_relational_models
   INFO  [alembic.runtime.migration] Running upgrade 92f1a8c3d4e5 -> a3c4e5f6a7b8, session_based_analytics
   ```

   **Scenario 2: Database Already at Previous Production Schema (`92f1a8c3d4e5`):**
   The runner applies only the session delta:
   ```text
   INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.
   INFO  [alembic.runtime.migration] Will assume transactional DDL.
   INFO  [alembic.runtime.migration] Running upgrade 92f1a8c3d4e5 -> a3c4e5f6a7b8, session_based_analytics
   ```

   In both scenarios, confirm the process completes and exits cleanly with **status 0 (Success / Completed)**.
7. **Immediate Teardown & Removal (Mandatory):**
   To guarantee the runner cannot unintentionally rerun after restarts, project restarts, or environment changes:
   - Go to `staging-migration-runner` $\to$ **Settings** $\to$ **Danger Zone** $\to$ **Delete Service**.
   - If keeping the service configuration as an inert template, immediately **Clear the Start Command** or toggle **Disable Service**.

---

### Procedure B: Railway CLI SSH (Vendor-Documented CLI v3+ Command — Unverified Locally)

> [!NOTE]
> The Railway CLI is not installed locally and its commands cannot be verified in this workspace. In Railway CLI v3+, `railway ssh` replaces the legacy `railway shell`. This procedure is documented for operators using the CLI, but remains **unverified until tested against live Railway services**. Procedure A is recommended.

1. Link to the project:
   ```bash
   railway link
   ```
2. Open an interactive SSH shell inside the running `backend-staging` container:
   ```bash
   railway ssh -s backend-staging
   ```
3. Inside the container shell, verify pre-migration revision:
   ```bash
   alembic current
   ```
   **Expected output:**
   - On a **completely fresh/empty database** (where the `alembic_version` table does not yet exist): Output is **empty / blank** (no revision printed).
   - On a database already migrated to the pre-session state: `92f1a8c3d4e5`.
4. Execute the migration to head **once**:
   ```bash
   alembic upgrade head
   ```
5. Confirm head revision:
   ```bash
   alembic current
   # Expected output: a3c4e5f6a7b8 (head)
   ```
6. Exit the shell:
   ```bash
   exit
   ```

---

## 6. Two-Stage Zoho OAuth Callback & Ephemeral Session Handoff

`[CODE AUDITED & INTEGRATION SPECIFICATION]`

Inspected from [app/integrations/zoho/router.py#L327-L491](app/integrations/zoho/router.py#L327-L491) and [app/sessions/manager.py#L260-L350](app/sessions/manager.py#L260-L350):

### Callback Architecture & Responsibilities

The backend provides two distinct callback endpoints under `/v2/integrations/zoho/callback`:

1. **Browser GET Callback (`GET /v2/integrations/zoho/callback`, [app/integrations/zoho/router.py#L327-L456](app/integrations/zoho/router.py#L327-L456)):
   - **What it receives:** Standard query parameters from Zoho Accounts via top-level browser redirect: `code`, `state`, `error`, `error_description`.
   - **Token Exchange Behavior:** The GET handler does **not** merely render a completion page. It performs the full server-to-server token exchange during the request:
     1. Validates `code` and `state` regex formats.
     2. Computes `state_hash = hashlib.sha256(state.encode("utf-8")).hexdigest()`.
     3. Matches `state_hash` against `AnalyticsSession.oauth_state_hash` in PostgreSQL.
     4. Resolves the Zoho Accounts endpoint for the session's datacenter (`accounts.zoho.{dc}`).
     5. Calls `SessionManager.bind_oauth_credentials()`, which contacts Zoho Accounts and exchanges the authorization code for refresh and access tokens.
     6. Encrypts tokens using `CY_ENCRYPTION_KEY` and persists them into `analytics_session_credentials`.
     7. Nullifies `oauth_state_hash` and `encrypted_auth_url` on `analytics_sessions` to prevent replay, and transitions session status to `ACTIVE`.
     8. Finally, renders an HTML status page instructing the user: *"Your Zoho Recruit account has been successfully connected to your Channel Yield Analytics session. You may now close this window, return to iGentic, and run your recruitment analysis."*
2. **Programmatic POST Callback (`POST /v2/integrations/zoho/callback`, [app/integrations/zoho/router.py#L459-L491](app/integrations/zoho/router.py#L459-L491)):
   - Requires JWT administrative authentication (`require_role("admin")`).
   - Intended exclusively for automated API clients in permanent tenant mode exchanging credentials programmatically.
   - It is **not** used during temporary analytics sessions.

### Cross-Service Credential Availability to `mcp-staging`
- Token exchange is performed exclusively by **`backend-staging`**.
- Once persisted in `analytics_session_credentials`, credentials become immediately available to **`mcp-staging`** because both services connect to the same PostgreSQL database and share the identical `CY_ENCRYPTION_KEY`.
- When an iGentic agent calls any recruitment tool, `mcp-staging` executes `SessionManager.verify_and_touch_session(db)`, which reads the active session, decrypts the tokens in memory, and queries Zoho Recruit.
- No direct inter-service HTTP requests occur between backend and MCP services.

### Browser-to-Agent Handoff
> [!NOTE]
> **Pending Live Integration Verification:**
> The browser flow completes by displaying the success HTML message to the user. Whether iGentic detects the newly active session automatically (via periodic polling of `get_analytics_session_status`) or relies on the user prompting the agent ("I have authorized access, please run the report") is an agent-level workflow that has not been live tested and remains **pending live integration verification** on Railway.

---

## 7. Supported MCP Transports & Verified Smoke-Test Procedures

`[LOCALLY TESTED BEHAVIOR / PENDING LIVE RAILWAY VERIFICATION]`

### Transport Protocols Supported by MCP Server
The server implements two distinct remote network transports in [mcp_server/server.py](mcp_server/server.py):

| Transport | Protocol | Endpoint URL Format | Implementation Reality & Compatibility Status |
| :--- | :--- | :--- | :--- |
| **Server-Sent Events (SSE)** | SSE stream (`GET`) + JSON-RPC messages (`POST`) | `https://<mcp-host>/sse/<key>` | **Proposed Initial iGentic Integration (Standard MCP Spec 2024-11-05).** Implements persistent event stream with session-bound message routing. **Pending Live Railway Verification** with iGentic. |
| **Streamable HTTP** | Direct JSON-RPC (`POST`) | `https://<mcp-host>/mcp/<key>` | **Stateless Direct HTTP Endpoint.** Dispatches individual JSON-RPC calls without session state or event queuing. Supports stateless discovery (`initialize`, `tools/list`). **Session-dependent analytics workflows require active Zoho session state and must be validated against the actual session implementation.** |

> [!WARNING]
> **iGentic Integration & Transport Status:**  
> - **Proposed Initial Transport:** Server-Sent Events (SSE) via `/sse/<key>` is the proposed initial transport for iGentic integration, matching the standard MCP protocol.
> - **Status:** Both SSE and Streamable HTTP transports are **pending live integration verification** on Railway. Historical Render documentation describes architectural intent, but does **not** prove a live, working connection from iGentic to Railway services.
> - **Stateless vs Session-Dependent Workflows:** `/mcp/<key>` is a stateless direct HTTP endpoint. While stateless introspection (`initialize`, `tools/list`) has been validated locally, any session-dependent analytics workflows (e.g. tools accessing Zoho or session state) require an active session and must be validated against the actual session implementation on live staging.

---

### Smoke-Test Procedure 1: Direct HTTP Stateless Transport (`/mcp/<key>`)
`[LOCALLY TESTED BEHAVIOR / LIVE RAILWAY VERIFICATION PENDING]`

Direct HTTP transport handles stateless JSON-RPC requests directly. Each request requires the header `Content-Type: application/json`.

*(Note: While standalone discovery POST calls succeed statelessly, any session-dependent tool calls require active session handling and must be validated against the live session coordinator).*

#### Step 1: Initialize Request
```bash
curl -fsS -X POST \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
      "protocolVersion": "2024-11-05",
      "capabilities": {},
      "clientInfo": {"name": "smoke-test", "version": "1.0.0"}
    }
  }' \
  https://<mcp-staging-domain>/mcp/<CY_MCP_SECRET_PATH_KEY>
```
**Expected Response (HTTP 200):**
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "protocolVersion": "2024-11-05",
    "capabilities": {"tools": {}},
    "serverInfo": {"name": "zoho-recruit-mcp", "version": "1.0.0"}
  }
}
```

#### Step 2: Discover Read-Only Tools (Stateless)
```bash
curl -fsS -X POST \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/list",
    "params": {}
  }' \
  https://<mcp-staging-domain>/mcp/<CY_MCP_SECRET_PATH_KEY>
```
**Expected Response (HTTP 200):** JSON-RPC object returning the 22 registered tools.

---

### Smoke-Test Procedure 2: SSE Transport Complete Protocol Handshake
`[LOCALLY TESTED BEHAVIOR / LIVE RAILWAY VERIFICATION PENDING]`

The standard MCP SSE protocol requires a coordinated, multi-step handshake across the same session:

#### Step 1: Establish Persistent SSE Stream & Capture Session Endpoint
In Terminal 1, initiate the streaming connection:
```bash
curl -N -H "Accept: text/event-stream" \
  https://<mcp-staging-domain>/sse/<CY_MCP_SECRET_PATH_KEY>
```
**Expected First Event Received in Terminal 1:**
```text
event: endpoint
data: /messages/<CY_MCP_SECRET_PATH_KEY>?session_id=3fa85f64-5717-4562-b3fc-2c963f66afa6
```
*(Leave Terminal 1 running. The server emits `: ping` comments every 15 seconds to keep the connection alive).*

#### Step 2: Send `initialize` Request (Using Same Session)
In Terminal 2, send the initialization handshake to the exact path captured in Step 1:
```bash
curl -fsS -X POST \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
      "protocolVersion": "2024-11-05",
      "capabilities": {},
      "clientInfo": {"name": "smoke-test-client", "version": "1.0.0"}
    }
  }' \
  "https://<mcp-staging-domain>/messages/<CY_MCP_SECRET_PATH_KEY>?session_id=<session_id_from_step_1>"
```
**Expected Responses:**
- **Terminal 2 (POST response):** `HTTP 202 Accepted` (`{"status": "accepted"}`).
- **Terminal 1 (Over the SSE stream):**
  ```text
  event: message
  data: {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "zoho-recruit-mcp", "version": "1.0.0"}}}
  ```

#### Step 3: Send `notifications/initialized` Notification (Standard MCP Handshake)
In Terminal 2, dispatch the initialized notification to the same session:
```bash
curl -fsS -X POST \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "method": "notifications/initialized",
    "params": {}
  }' \
  "https://<mcp-staging-domain>/messages/<CY_MCP_SECRET_PATH_KEY>?session_id=<session_id_from_step_1>"
```
**Expected Response:**
- **Terminal 2 (POST response):** `HTTP 202 Accepted` (`{"status": "accepted"}`).
- **Terminal 1:** Notifications require no response; stream continues listening.

#### Step 4: Request Tools List (Using Same Session)
In Terminal 2, query the tool registry:
```bash
curl -fsS -X POST \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/list",
    "params": {}
  }' \
  "https://<mcp-staging-domain>/messages/<CY_MCP_SECRET_PATH_KEY>?session_id=<session_id_from_step_1>"
```
**Expected Responses:**
- **Terminal 2 (POST response):** `HTTP 202 Accepted` (`{"status": "accepted"}`).
- **Terminal 1 (Over the SSE stream):**
  ```text
  event: message
  data: {"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "start_analytics_session", ...}, ...]}}
  ```

---

## 8. Sync Worker Status: Deferred from Initial Rollout (Verified Dormancy Semantics)

`[LOCALLY TESTED BEHAVIOR & CODE AUDITED]`

### Intentional Omission from Initial Rollout
The scheduled synchronization worker (`worker-staging`) is **intentionally not provisioned** during the initial Railway staging rollout.

### Verified Dormancy Behavior
If the worker container is executed with `python -m app.worker --once`, its behavior is governed by [app/worker.py#L73-L77](app/worker.py#L73-L77):
```python
def run_sync_cycle() -> list[dict[str, Any]]:
    if os.environ.get("CY_SYNC_WORKER_ENABLED", "true").lower() in ("false", "0", "disabled"):
        logger.info("worker.sync_disabled_by_environment", reason="CY_SYNC_WORKER_ENABLED is disabled")
        return []
```
* **Locally Tested:** When `CY_SYNC_WORKER_ENABLED="false"`, `app.worker.run_sync_cycle()` short-circuits immediately. It performs **zero database queries**, makes **zero Zoho API calls**, returns `[]`, and exits cleanly with exit code `0`.
* **CLI Guard:** The `--once` flag cannot bypass this guard because `run_sync_cycle()` evaluates the environment variable on its very first line.

### Future Provisioning Re-Audit Requirement
> [!IMPORTANT]
> If `worker-staging` is provisioned and enabled in a future deployment, its environment-variable requirements **must be re-audited before enablement**:
> - An active worker executes `sync_tenant()` via `ZohoService.execute_sync()`, which calls `decrypt_token()` on stored tenant credentials.
> - Therefore, an active worker **requires `CY_ENCRYPTION_KEY`** (in addition to `CY_DATABASE_URL`, `CY_ZOHO_CLIENT_ID`, `CY_ZOHO_CLIENT_SECRET`, and `CY_ZOHO_REDIRECT_URI`).
> - Do not deploy an active worker without auditing and supplying these required credentials.

---

## 9. Production Isolation Verification Checklist

`[STAGING GATEWAY CHECKLIST]`

Verify every item on this checklist before declaring staging ready:

- [ ] **Codebase Isolation:** Staging runs exclusively from branch `staging` (Commit `81e315002e80c2232b115cd581557a6f17b353a4`). Main branch (`1dffa8202f925a0713ec174c2775d5d08ea027ca`) is untouched.
- [ ] **Database Isolation:** Railway `CY_DATABASE_URL` references `${{Postgres.DATABASE_PRIVATE_URL}}` in project `channel-yield-staging`. No production database host or credentials are used. Status: Planned — pending Railway provisioning and live isolation verification.
- [ ] **Zero Production-Secret Reuse:** Staging `CY_SECRET_KEY` and `CY_ENCRYPTION_KEY` are newly generated 32-byte secrets. Zero reuse of production keys (sharing permitted strictly between staging services).
- [ ] **Sweeper Process Configuration:** `backend-staging` reads `CY_MCP_ENABLE_BACKGROUND_SWEEPER`; `mcp-staging` sweeper starts unconditionally on ASGI lifespan. In-process check and PostgreSQL transaction-level advisory lock prevent concurrent cleanup execution while held.
- [ ] **Migration Runner Teardown Guard:** `staging-migration-runner` is configured with Restart Policy: Never and Auto-Deploy: Disabled, and is deleted or disabled immediately after observing exit code 0 to prevent unintentional reruns.
- [ ] **Dual Migration Scenario Awareness:** Fresh empty database applies `81e098bcb28a` $\to$ `92f1a8c3d4e5` $\to$ `a3c4e5f6a7b8` (head), whereas existing database applies `92f1a8c3d4e5` $\to$ `a3c4e5f6a7b8`.
- [ ] **Worker Intentional Omission:** `worker-staging` is confirmed omitted from initial rollout. If configured later, full environment variable re-audit is completed.
- [ ] **Two-Stage OAuth Clarity:** Browser GET callback exchanges code, encrypts credentials, and activates session; MCP decrypts credentials from shared database.
- [ ] **Human-Identity Limitation Acknowledged:** Global session lock enforces one active session at a time; shared MCP key (`CY_MCP_SECRET_PATH_KEY`, MCP-only) does not authenticate individual users; staging pilot restricted to trusted operators.
- [ ] **MCP Transport Distinction:** `/mcp/<key>` verified as stateless direct HTTP endpoint; session-dependent analytics workflows must be validated against actual session state.
- [ ] **iGentic Integration Pending:** Server-Sent Events (SSE) via `/sse/<key>` is the proposed initial iGentic integration, but is explicitly marked pending live Railway verification.
- [ ] **Zoho OAuth Isolation:**
  - `CY_ZOHO_CLIENT_ID` and `CY_ZOHO_CLIENT_SECRET` are issued from a dedicated staging application in the Zoho Developer Console.
  - Authorized Redirect URI in Zoho Developer Console strictly matches `https://<backend-staging-domain>/v2/integrations/zoho/callback`.
  - Production OAuth credentials are never entered into staging services.
- [ ] **Zoho Data Centre Alignment:** `CY_ZOHO_DATA_CENTER` is set to match the staging account location (`in` for India accounts, `com` for US accounts).
- [ ] **Decoupled Migrations:** `alembic upgrade head` is confirmed absent from all build commands.
- [ ] **Render Blueprint Integrity:** `render.yaml`, `render.staging.yaml`, `render-mcp.yaml`, and `render-mcp-demo.yaml` are preserved unmodified.
