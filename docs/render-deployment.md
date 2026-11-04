# Render Production Deployment Guide
**Channel Yield Analytics Agent & Custom MCP Server**

This guide provides instructions for deploying the Channel Yield Analytics platform on [Render](https://render.com), encompassing the FastAPI backend, the custom Model Context Protocol (MCP) server, and the scheduled synchronization worker.

---

## 1. Architecture Overview on Render

The production deployment consists of three decoupled components configured in [render.yaml](file:///render.yaml):

| Service Name | Type | Plan | Command / Entry Point | Health Check | Purpose |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `channel-yield-backend` | Web Service | Starter | `uvicorn app.main:app --host 0.0.0.0 --port $PORT` | `GET /health` | Core REST API, OAuth flow, CSV ingestion, analytics engine |
| `zoho-recruit-mcp` | Web Service | Starter | `python -m mcp_server.server --host 0.0.0.0 --port $PORT` | `GET /health` | Custom MCP server for iGentic AI platform (SSE & Streamable HTTP) |
| `channel-yield-sync-worker`| Cron Job | Starter | `python -m app.worker --once` (Hourly: `0 * * * *`) | Process exit code | Periodic data ingestion from Zoho Recruit across active tenants |

---

## 2. Health, Readiness, and Version Probes

The application provides distinct probes to comply with enterprise container lifecycle management:

### 2.1 Backend Probes (`channel-yield-backend`)
- **Liveness Probe**: `GET /health`
  - Returns `{"status": "ok", "service": "channel-yield-analytics-agent", "version": "1.0.0", "environment": "production"}`
  - Purpose: Validates that the process is alive and receiving traffic without depending on external databases.
- **Readiness Probe**: `GET /ready`
  - Returns `200 OK` with `{"status": "ready", "database": "connected"}` or `503 Service Unavailable` if PostgreSQL connectivity fails.
- **Version Probe**: `GET /version`
  - Returns `{"service": "channel-yield-analytics-agent", "version": "1.0.0", "mcp_version": "2024-11-05", "environment": "production"}`

### 2.2 MCP Server Probes (`zoho-recruit-mcp`)
- **Liveness & Protocol Probe**: `GET /health`
  - Returns `{"status": "ok", "service": "zoho-recruit-mcp", "version": "1.0.0", "mcp_version": "2024-11-05"}`
- **Version & Build Probe**: `GET /version`
  - Returns MCP server protocol specifications, active transports (`stdio`, `sse`, `streamable_http`), and tool counts.
- **Protocol Initialization Check**:
  - SSE handshake: `GET /sse/{token}` or `GET /sse` with `Authorization: Bearer <token>` initiates SSE stream.
  - Streamable HTTP: `POST /mcp` or `POST /mcp/{token}` with JSON-RPC payload `{"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test", "version": "1.0"}}}`.

---

## 3. Environment Variables Specification

Set the following variables in the Render Dashboard under **Environment**:

### 3.1 Backend Service (`channel-yield-backend`)

| Variable | Required | Type / Example | Description |
| :--- | :---: | :--- | :--- |
| `CY_ENV` | **Yes** | `production` | Enables production safeguards (blocks SQLite, requires strong secrets). |
| `CY_DATABASE_URL` | **Yes** | `postgresql+psycopg2://user:pass@host/dbname?sslmode=require` | Managed PostgreSQL connection string. |
| `CY_SECRET_KEY` | **Yes** | 64-char hex string | JWT signing key. Generate via `python -c "import secrets; print(secrets.token_hex(32))"`. |
| `CY_ENCRYPTION_KEY` | **Yes** | Fernet key string | Encryption key for Zoho OAuth tokens at rest. Generate via `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. |
| `CY_ZOHO_CLIENT_ID` | **Yes** | Zoho Client ID | OAuth Client ID from Zoho Developer Console. |
| `CY_ZOHO_CLIENT_SECRET`| **Yes** | Zoho Client Secret | OAuth Client Secret from Zoho Developer Console. |
| `CY_ZOHO_REDIRECT_URI` | **Yes** | `https://<backend-host>/v2/integrations/zoho/callback` | Authorized redirect URI configured in Zoho. |
| `CY_ZOHO_DATA_CENTER` | No | `com`, `eu`, `in`, `au`, `jp`, `ca` | Default Zoho data center (defaults to `com`). |
| `CY_CORS_ORIGINS` | No | `["https://igentic.ai", "https://app.igentic.ai"]` | JSON list of allowed origins. Wildcard `*` is strictly disallowed in production. |

### 3.2 MCP Service (`zoho-recruit-mcp`)

| Variable | Required | Type / Example | Description |
| :--- | :---: | :--- | :--- |
| `CY_ENV` | **Yes** | `production` | Production mode. |
| `CY_MCP_HOST` | No | `0.0.0.0` | Listen address. |
| `CY_DATABASE_URL` | **Yes** | Same as Backend | Accesses encrypted tokens to call Zoho Recruit APIs. |
| `CY_ENCRYPTION_KEY` | **Yes** | Same as Backend | Used to decrypt tokens for authorized tool execution. |
| `CY_MCP_SECRET_PATH_KEY`| **Yes** | 64-char hex string | Shared secret for capability-path tokens (`/sse/{key}` and `/mcp/{key}`) for iGentic. |
| `CY_MCP_TENANT_ID` | Optional | Tenant UUID | Default fallback tenant UUID if unauthenticated callers connect. |
| `CY_ZOHO_CLIENT_ID` | **Yes** | Same as Backend | Used for automated access token refresh. |
| `CY_ZOHO_CLIENT_SECRET`| **Yes** | Same as Backend | Used for automated access token refresh. |
| `CY_ZOHO_REDIRECT_URI` | **Yes** | Same as Backend | Used for automated access token refresh. |
| `CY_ZOHO_DATA_CENTER` | No | `com` | Default data center fallback. |

### 3.3 Hourly Sync Cron Worker (`channel-yield-sync-worker`)

| Variable | Required | Type / Example | Description |
| :--- | :---: | :--- | :--- |
| `CY_ENV` | **Yes** | `production` | Production mode. |
| `CY_DATABASE_URL` | **Yes** | Same as Backend | Connects to PostgreSQL. |
| `CY_ENCRYPTION_KEY` | **Yes** | Same as Backend | Decrypts tokens to execute scheduled sync. |
| `CY_ZOHO_CLIENT_ID` | **Yes** | Same as Backend | Token refresh during sync. |
| `CY_ZOHO_CLIENT_SECRET`| **Yes** | Same as Backend | Token refresh during sync. |
| `CY_ZOHO_REDIRECT_URI` | **Yes** | Same as Backend | Token refresh during sync. |

---

## 4. Step-by-Step Deployment Procedure

### Step 1: Provision Managed PostgreSQL
1. In Render Dashboard, click **New +** $\to$ **PostgreSQL**.
2. Select name (e.g. `channel-yield-db`), region (e.g. `Oregon`), plan (`Starter` or above).
3. Copy the **Internal Database URL** (for services inside the same Render region) and **External Database URL** (for administrative migrations/diagnostics).
4. Ensure `?sslmode=require` is appended to the connection string.

### Step 2: Run Database Migrations
Before starting web instances, apply the database migrations:
```bash
# Set external database URL locally in your secure terminal:
$env:CY_DATABASE_URL="postgresql+psycopg2://<user>:<password>@<host>/<dbname>?sslmode=require"

# Apply all Alembic migrations:
alembic upgrade head
```

### Step 3: Deploy via Render Blueprint
1. In Render Dashboard, navigate to **Blueprints** $\to$ **New Blueprint Instance**.
2. Select repository: `Hariprasath2011/channel-yield-analytics-agent`.
3. Render reads [render.yaml](file:///render.yaml) and provisions:
   - `channel-yield-backend`
   - `zoho-recruit-mcp`
   - `channel-yield-sync-worker`
4. Enter the sensitive environment variables (`CY_DATABASE_URL`, `CY_SECRET_KEY`, `CY_ENCRYPTION_KEY`, `CY_ZOHO_CLIENT_ID`, `CY_ZOHO_CLIENT_SECRET`, `CY_ZOHO_REDIRECT_URI`, `CY_MCP_SECRET_PATH_KEY`) in the Render Dashboard.
5. Click **Apply**.

### Step 4: Verify Deployment Health
Run curl commands against the public Render URLs:

```bash
# 1. Backend liveness & readiness:
curl -s https://<backend-host>/health
# Expected: {"status":"ok","service":"channel-yield-analytics-agent","version":"1.0.0","environment":"production"}

curl -s https://<backend-host>/ready
# Expected: {"status":"ready","service":"channel-yield-analytics-agent","database":"connected"}

# 2. MCP Server liveness & protocol check:
curl -s https://<mcp-host>/health
# Expected: {"status":"ok","service":"zoho-recruit-mcp","version":"1.0.0","mcp_version":"2024-11-05"}

curl -s https://<mcp-host>/version
# Expected: {"service":"zoho-recruit-mcp","version":"1.0.0","mcp_version":"2024-11-05",...}
```

---

## 5. Concurrency & High Availability Safeguards

1. **Duplicate Scheduler Protection**:
   - Instead of running an internal scheduler inside every web container (which would multiply sync jobs on scale-out), scheduled synchronization runs as a dedicated Render **Cron Job** (`channel-yield-sync-worker`) triggering `python -m app.worker --once` hourly.
   - At the database level, `ZohoService.execute_sync` utilizes `SELECT ... FOR UPDATE` row locks on the `tenants` table. Even if two cron executions overlap, the second execution is cleanly rejected or queued, preventing race conditions or double-ingestion.

2. **Connection Pooling**:
   - SQLAlchemy engines are configured with `pool_size=10`, `max_overflow=20`, `pool_recycle=1800`, and `pool_pre_ping=True` to prevent idle connection termination by cloud routers.

3. **Stateless Resiliency**:
   - Web processes maintain no in-memory session state; any instance restart or horizontal scaling preserves user sessions and OAuth flows via PostgreSQL tables.
