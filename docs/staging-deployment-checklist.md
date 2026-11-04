# Staging Deployment & Operational Checklist
**Channel Yield Analytics Agent — Isolated Staging Environment**

This checklist guides the operator through provisioning, configuring, verifying, and testing the isolated **Render Staging Environment** defined in [render.staging.yaml](file:///render.staging.yaml).

---

## 1. Staging Architecture & Isolation Boundary

The staging environment is 100% decoupled from production:

| Component | Staging Resource Name | Production Counterpart | Isolation Mechanism |
| :--- | :--- | :--- | :--- |
| **Git Branch** | `staging` | `main` | Independent codebase branch; changes merged only after staging sign-off. |
| **PostgreSQL Database** | `channel-yield-db-staging` | Production PostgreSQL | Separate database instance; production data is never touched or cloned. |
| **Backend REST API** | `channel-yield-backend-staging` | `channel-yield-backend` | Distinct hostname: `https://channel-yield-backend-staging.onrender.com` |
| **Custom MCP Server** | `zoho-recruit-mcp-staging` | `zoho-recruit-mcp` | Distinct hostname: `https://zoho-recruit-mcp-staging.onrender.com` |
| **Sync Cron Worker** | `channel-yield-sync-worker-staging` | `channel-yield-sync-worker` | **Initially disabled** via `CY_SYNC_WORKER_ENABLED="false"`. |
| **Zoho OAuth App** | Dedicated Staging Client ID | Production Client ID | Distinct redirect URI registered in Zoho Developer Console. |
| **Cryptographic Keys** | Staging Fernet & JWT Keys | Production Keys | Separate 256-bit entropy secrets; zero shared secrets. |

---

## 2. Pre-Deployment Setup Checklist

### Step 2.1: Prepare the Git Staging Branch
Ensure a clean `staging` branch exists in GitHub:
```bash
# From your current repository root:
git checkout -b staging
git add render.staging.yaml docs/staging-deployment-checklist.md
git commit -m "chore: add isolated Render staging blueprint and deployment checklist"
# Note: Do not push to remote until approved.
```

### Step 2.2: Generate Dedicated Staging Cryptographic Secrets
Run these commands locally in a secure terminal to generate staging keys (do not commit them):
```bash
# 1. Staging JWT Secret Key (for CY_SECRET_KEY):
python -c "import secrets; print('Staging JWT Secret:', secrets.token_hex(32))"

# 2. Staging Token Encryption Key (for CY_ENCRYPTION_KEY):
python -c "from cryptography.fernet import Fernet; print('Staging Fernet Key:', Fernet.generate_key().decode())"

# 3. Staging MCP Capability-Path Key (for CY_MCP_SECRET_PATH_KEY):
python -c "import secrets; print('Staging MCP Secret Path Key:', secrets.token_hex(32))"
```

### Step 2.3: Register Dedicated Staging Zoho OAuth Application
1. Sign in to the [Zoho Developer Console](https://api-console.zoho.com/).
2. Click **Add Client** $\to$ **Server-based Applications**.
3. Configure the staging application:
   - **Client Name**: `Channel Yield Analytics (Staging)`
   - **Homepage URL**: `https://channel-yield-backend-staging.onrender.com`
   - **Authorized Redirect URIs**:
     ```
     https://channel-yield-backend-staging.onrender.com/v2/integrations/zoho/callback
     ```
4. Copy the generated **Staging Client ID** and **Staging Client Secret**.

---

## 3. Render Blueprint Deployment Steps

### Step 3.1: Create Staging Blueprint on Render
1. Open the [Render Dashboard](https://dashboard.render.com/).
2. Click **Blueprints** $\to$ **New Blueprint Instance**.
3. Select your repository: `Hariprasath2011/channel-yield-analytics-agent`.
4. Set the **Branch** to `staging`.
5. In **Blueprint File**, specify:
   ```
   render.staging.yaml
   ```
6. Render will parse the blueprint and detect:
   - Database: `channel-yield-db-staging`
   - Services: `channel-yield-backend-staging`, `zoho-recruit-mcp-staging`, `channel-yield-sync-worker-staging`

### Step 3.2: Populate Staging Environment Secrets in Render Dashboard
Render will prompt for all `sync: false` variables. Enter the staging secrets generated in Step 2:

| Service | Environment Variable | Value Source |
| :--- | :--- | :--- |
| **All Services** | `CY_DATABASE_URL` | Auto-populated by Render from `channel-yield-db-staging` |
| **Backend & Worker** | `CY_SECRET_KEY` | Generated Staging JWT Secret (Step 2.2) |
| **All Services** | `CY_ENCRYPTION_KEY` | Generated Staging Fernet Key (Step 2.2) |
| **All Services** | `CY_ZOHO_CLIENT_ID` | Staging Zoho Client ID (Step 2.3) |
| **All Services** | `CY_ZOHO_CLIENT_SECRET`| Staging Zoho Client Secret (Step 2.3) |
| **MCP Server** | `CY_MCP_SECRET_PATH_KEY`| Generated Staging MCP Secret Path Key (Step 2.2) |

7. Click **Apply**. Render provisions the PostgreSQL database first, then builds and launches the staging services.

---

## 4. Post-Deployment Verification & Testing

### Step 4.1: Verify Health & Readiness Probes
Run curl commands against the public staging endpoints:

```bash
# 1. Staging Backend Liveness Probe:
curl -s https://channel-yield-backend-staging.onrender.com/health
# Expected: {"status":"ok","service":"channel-yield-analytics-agent","version":"1.0.0","environment":"staging"}

# 2. Staging Backend Readiness Probe (confirms staging PostgreSQL connection):
curl -s https://channel-yield-backend-staging.onrender.com/ready
# Expected: {"status":"ready","service":"channel-yield-analytics-agent","database":"connected"}

# 3. Staging Backend Version Probe:
curl -s https://channel-yield-backend-staging.onrender.com/version
# Expected: {"service":"channel-yield-analytics-agent","version":"1.0.0","mcp_version":"2024-11-05","environment":"staging"}

# 4. Staging MCP Server Liveness & Version Probe:
curl -s https://zoho-recruit-mcp-staging.onrender.com/health
# Expected: {"status":"ok","service":"zoho-recruit-mcp","version":"1.0.0","mcp_version":"2024-11-05"}
```

### Step 4.2: Perform Staging Zoho OAuth Authorization
1. Open a browser and navigate to the staging authorization endpoint:
   ```
   https://channel-yield-backend-staging.onrender.com/v2/integrations/zoho/authorize?data_center=in
   ```
   *(Replace `in` with your Zoho data center: `com`, `eu`, `in`, `au`, `jp`, `ca`).*
2. Authenticate and approve permissions in Zoho.
3. You will be redirected to the success screen:
   `https://channel-yield-backend-staging.onrender.com/v2/integrations/zoho/callback`
   *(Displays: "Authorization Successful — Zoho Recruit Connected").*

### Step 4.3: Test Manual Sync on Staging
Before enabling automated cron execution, trigger a manual on-demand sync:
```bash
# Check integration status:
curl -s -H "Authorization: Bearer <staging-admin-jwt>" \
  https://channel-yield-backend-staging.onrender.com/v2/integrations/zoho/status

# Trigger on-demand sync:
curl -s -X POST -H "Authorization: Bearer <staging-admin-jwt>" \
  https://channel-yield-backend-staging.onrender.com/v2/integrations/zoho/sync
```
Verify that job records appear and complete with status `completed`.

### Step 4.4: Verify MCP Tools on Staging
1. Issue a staging MCP connection token:
   ```bash
   curl -s -H "Authorization: Bearer <staging-admin-jwt>" \
     "https://channel-yield-backend-staging.onrender.com/v2/integrations/zoho/mcp-token?expire_hours=24"
   ```
2. Test tool execution via Streamable HTTP:
   ```bash
   curl -s -X POST \
     -H "Content-Type: application/json" \
     -d '{"jsonrpc": "2.0", "method": "tools/call", "id": 1, "params": {"name": "test_zoho_connection", "arguments": {}}}' \
     https://zoho-recruit-mcp-staging.onrender.com/mcp/<connection_token>
   ```
   Confirm response returns `{"status": "connected", "token_valid": true}`.

---

## 5. Enabling the Staging Hourly Sync Cron Job

Once manual sync and OAuth tests succeed:
1. In the Render Dashboard, navigate to **`channel-yield-sync-worker-staging`**.
2. Click **Environment**.
3. Change the variable:
   - `CY_SYNC_WORKER_ENABLED` $\to$ **`true`**
4. Save changes. The cron worker will now execute hourly incremental syncs automatically.

---

## 6. Teardown / Deletion (If No Longer Needed)

Because all staging resources are grouped in the Blueprint instance:
1. In the Render Dashboard, navigate to **Blueprints**.
2. Select the staging blueprint instance.
3. Click **Delete Blueprint Instance** and select **Delete all associated resources**.
4. This drops `channel-yield-db-staging`, `channel-yield-backend-staging`, `zoho-recruit-mcp-staging`, and `channel-yield-sync-worker-staging` without impacting any production services.
