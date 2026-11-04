# iGentic AI Platform Integration Guide
**Custom Zoho Recruit MCP Server Connection**

This guide provides instructions for connecting the **Channel Yield Analytics Custom MCP Server** to the **iGentic AI platform**.

---

## 1. Integration Architecture & Protocol

The Channel Yield platform integrates with iGentic through the official [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) specification (`2024-11-05`).

```
┌─────────────────────────────────┐
│       iGentic AI Platform       │
│  (AI Agents & Workflow Runners) │
└───────────────┬─────────────────┘
                │
                │ 1. MCP Remote Transport (SSE or Streamable HTTP)
                │    URL: https://<mcp-host>/sse/{connection_token}
                │    OR:  https://<mcp-host>/mcp/{connection_token}
                ▼
┌────────────────────────────────────────────────────────┐
│             Render Custom MCP Server                   │
│               (`zoho-recruit-mcp`)                     │
│  - Cryptographic token verification (HMAC-SHA256)      │
│  - Strict Tenant Scoping & Read-Only Enforcement       │
└───────┬────────────────────────────────────────┬───────┘
        │                                        │
        │ Database Lookup                        │ Read-Only REST API v2
        ▼                                        ▼
┌───────────────────────┐            ┌───────────────────────┐
│ PostgreSQL Analytics  │            │     Zoho Recruit      │
│       Database        │            │   (API Data Center)   │
└───────────────────────┘            └───────────────────────┘
```

---

## 2. Supported Transports & Endpoint Formats

The MCP server supports two remote network transports on Render:

| Transport Mode | Recommended For | iGentic Configuration Endpoint | HTTP Protocol |
| :--- | :--- | :--- | :--- |
| **Server-Sent Events (SSE)** | iGentic standard MCP connector | `https://<mcp-host>/sse/<connection_token>` | SSE stream (`GET`) + JSON-RPC messages (`POST`) |
| **Streamable HTTP** | Modern HTTP MCP clients | `https://<mcp-host>/mcp/<connection_token>` | Direct JSON-RPC 2.0 streaming (`POST`) |

### Capability-Path Authentication
Because iGentic's connector UI may not permit setting arbitrary HTTP request headers (`Authorization: Bearer <token>`), the server implements **capability-path routing**. The HMAC-SHA256 connection token (`cyct_...`) is embedded directly in the URL path. 

The server cryptographically extracts the tenant ID and user ID from the signed token, ensuring that **no caller can spoof or access another tenant's recruitment records**.

---

## 3. Step-by-Step Configuration in iGentic

### Step 1: Obtain Your Organization's Connection Token
1. Log into your Channel Yield web portal or call the authenticated endpoint:
   ```http
   GET /v2/integrations/zoho/mcp-token?expire_hours=168
   Authorization: Bearer <your-user-jwt>
   ```
2. The response returns your organization's signed connection token:
   ```json
   {
     "connection_token": "cyct_eyJ0aWQiOiJ...b64.sig",
     "token_type": "Bearer",
     "expires_in_hours": 168,
     "expires_at": "2026-11-11T12:00:00Z",
     "tenant_id": "tenant-uuid-123",
     "zoho_connected": true,
     "mcp_endpoint_suggestion": "/sse/cyct_eyJ0aWQiOiJ...b64.sig"
   }
   ```

### Step 2: Configure the MCP Server in iGentic
1. Open your **iGentic Workspace**.
2. Navigate to **Integrations** $\to$ **Model Context Protocol (MCP)** $\to$ **Add Custom MCP Server**.
3. Fill in the connection settings:
   - **Server Name**: `Channel Yield Recruitment Analytics`
   - **Transport Type**: Select **SSE (Server-Sent Events)** or **Streamable HTTP / Remote MCP**.
   - **Server URL**:
     ```
     https://<mcp-service-name>.onrender.com/sse/<connection_token>
     ```
     *(Replace `<connection_token>` with your actual token from Step 1).*
   - **Environment Variables**: Leave blank (server runs entirely hosted on Render; no `npx` or local node packages required).
4. Click **Connect & Save**.

---

## 4. Discovering Available Tools

Upon successful connection, iGentic executes the standard MCP `tools/list` handshake. The following **16 read-only tools** are discovered automatically:

### 4.1 Connection & Diagnostic Tools
- `test_zoho_connection`: Verifies active connection to Zoho Recruit and token validity.
- `get_recruitment_modules`: Discovers available modules (`JobOpenings`, `Candidates`, `Applications`).
- `get_recruitment_fields`: Retrieves field metadata and types for a specific module.
- `get_sync_status`: Inspects administrative synchronization job history and status.

### 4.2 Recruitment Records Tools
- `list_job_openings`: Lists job openings/requisitions with filtering by status.
- `list_candidates`: Lists candidates with strict PII minimization (names, source, qualification, experience; sensitive contact info stripped).
- `list_applications`: Lists applications with requisition and candidate linkages.
- `get_candidate_details`: Detailed candidate profile (anonymized PII).
- `get_application_details`: Single application details with current stage and rating.

### 4.3 Historical Selection & Funnel Tools
- `get_selection_history`: Retrieves full stage transition history, time in stage, user, and rejection reasons.
- `get_candidate_history`: Complete chronological activity and application history for a candidate.
- `get_recruitment_funnel`: Step-by-step conversion funnel (`Applied` $\to$ `Screened` $\to$ `Interviewed` $\to$ `Offered` $\to$ `Hired`).

### 4.4 Analytics & Recommendations Tools
- `compute_channel_yield`: Sourcing channel yield rates (% hires / applicants).
- `get_channel_conversion`: Stage-by-stage conversion percentages across channels.
- `get_channel_costs`: Cost per hire and channel recruitment expense metrics.
- `get_channel_recommendations`: Multi-factor channel ranking and strategic sourcing recommendations.

---

## 5. Testing Tool Execution & Verifying Real Data

In your iGentic chat or workflow builder, run these test prompts:

### Prompt 1: Connection Verification
> *"Check the connection status of our Zoho Recruit account."*
- **iGentic Tool Call**: `test_zoho_connection()`
- **Expected Result**: Returns `{"status": "connected", "data_center": "...", "tenant_id": "...", "token_valid": true}`.

### Prompt 2: Job Openings Retrieval
> *"What are the current active job openings in Zoho Recruit?"*
- **iGentic Tool Call**: `list_job_openings({"page": 1, "per_page": 10, "status": "In-progress"})`
- **Expected Result**: Returns list of opening titles, requisition IDs, departments, and open dates.

### Prompt 3: Selection History Inspection
> *"Retrieve the selection stage history for application APP-101."*
- **iGentic Tool Call**: `get_selection_history({"application_id": "APP-101"})`
- **Expected Result**: Chronological sequence of stage movements, durations, comments, and outcomes without fabricated data.

### Prompt 4: Sourcing Channel Recommendations
> *"Which sourcing channels have the highest yield rate and lowest time-to-hire?"*
- **iGentic Tool Call**: `get_channel_recommendations({"min_sample_size": 5})`
- **Expected Result**: Ranked channel table with yields, conversions, time-to-hire, and recommendations.

---

## 6. Troubleshooting & Common Issues

| Error Message in iGentic | Root Cause | Resolution |
| :--- | :--- | :--- |
| `HTTP 401 Unauthorized: Invalid or missing authentication` | Connection token missing or malformed | Re-generate token via `/v2/integrations/zoho/mcp-token` and ensure the full token is in the URL path. |
| `HTTP 401: Token expired` | 7-day token expired | Generate a fresh connection token and update the iGentic server URL. |
| `status: "not_connected"` | Zoho OAuth has not been completed | Visit `https://<backend-host>/v2/integrations/zoho/authorize` to authorize your Zoho Recruit organization. |
| `HTTP 429 Too Many Requests` | Zoho API rate limits exceeded | Wait 60 seconds; the client automatically backs off with exponential backoff and jitter. |
| `Tools list is empty` | MCP handshake timeout | Ensure the Render service plan has not spun down (use Starter plan or above). Verify `GET https://<mcp-host>/health` returns `200 OK`. |

---

## 7. Direct HTTP API vs. Native MCP Support

If your iGentic plan does **not** have the native MCP connector enabled and only provides the legacy **Direct HTTP API** tool:
1. Inquire with iGentic support whether **Custom Request Headers** (`Authorization: Bearer <key>`) can be enabled.
2. In the interim, use the **capability-path Streamable HTTP endpoint**:
   ```
   POST https://<mcp-host>/mcp/<connection_token>
   Content-Type: application/json

   {
     "jsonrpc": "2.0",
     "method": "tools/call",
     "id": 1,
     "params": {
       "name": "compute_channel_yield",
       "arguments": {"cohort_start": "2026-01-01", "cohort_end": "2026-06-30"}
     }
   }
   ```
3. This allows the Direct HTTP API tool in iGentic to invoke any of the 16 MCP analytics tools directly via standard POST requests without custom headers.
