# Zoho Recruit Model Context Protocol (MCP) Integration Guide

## 1. Overview & Architecture

The **Zoho Recruit MCP Server** is an isolated, standalone service that exposes read-only recruitment tools to AI agents using the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/).

It integrates with the existing Channel Yield multi-tenant architecture while remaining fully decoupled from the core analytics API:
- **Reuse of Existing Zoho Client**: Reuses `ZohoRecruitClient` (`app/integrations/zoho/client.py`) for API communication, rate-limit backoff, and pagination.
- **Credential Protection**: Uses existing Fernet authenticated encryption (`app/integrations/zoho/crypto.py`) to decrypt access and refresh tokens at rest.
- **Automatic Token Lifecycle**: Detects expired access tokens, automatically negotiates refreshed tokens with Zoho Accounts, re-encrypts, and commits them to the PostgreSQL database.
- **Strict Read-Only Guarantee**: Implements only querying tools. No create, update, delete, or sync write operations are permitted.

---

## 2. Implemented MCP Tools

All tools enforce bounded pagination (`page >= 1`, `per_page` between 1 and 100, default 20) and strict module allowlisting (`JobOpenings`, `Candidates`, `Applications`).

### `list_job_openings`
Retrieves active or archived requisitions/job openings.
- **Input Parameters**:
  - `page` *(integer, default: 1)*: 1-indexed page number.
  - `per_page` *(integer, default: 20, max: 100)*: Records per page.
  - `status` *(string, optional)*: Filter by job opening status (e.g., `'In-progress'`, `'Filled'`, `'Cancelled'`).
- **Returned Fields**:
  `id`, `job_opening_id`, `posting_title`, `status`, `department`, `number_of_positions`, `city`, `state`, `country`, `opened_date`, `target_date`.

### `list_candidates`
Retrieves candidate profiles with **candidate privacy minimization**.
- **Privacy Minimization Policy**:
  Candidate personal identifying information (PII) including street addresses, phone numbers, mobile numbers, personal email addresses, current/expected salary numbers, and internal notes are **strictly excluded**.
- **Input Parameters**:
  - `page` *(integer, default: 1)*: 1-indexed page number.
  - `per_page` *(integer, default: 20, max: 100)*: Records per page.
  - `source` *(string, optional)*: Filter by acquisition channel (e.g., `'LinkedIn'`, `'Referral'`, `'Career Site'`).
- **Returned Fields**:
  `id`, `candidate_status`, `source`, `highest_qualification`, `current_job_title`, `experience_in_years`, `city`, `created_time`.

### `list_applications`
Retrieves applicant submissions mapped to specific requisitions.
- **Input Parameters**:
  - `page` *(integer, default: 1)*: 1-indexed page number.
  - `per_page` *(integer, default: 20, max: 100)*: Records per page.
  - `job_opening_id` *(string, optional)*: Filter applications by job opening ID.
  - `status` *(string, optional)*: Filter by application stage/status (e.g., `'Applied'`, `'Interview Scheduled'`).
- **Returned Fields**:
  `id`, `job_opening_id`, `job_title`, `candidate_id`, `status`, `rating`, `source`, `created_time`.

---

## 3. Transports Supported

### 3.1 Standard Input/Output (`stdio`)
For secure local agent invocation, CLI subprocess execution, or test harnesses:
```bash
python -m mcp_server.server --stdio
```
The server reads JSON-RPC 2.0 messages from `stdin` and writes JSON-RPC 2.0 responses to `stdout`.

### 3.2 Server-Sent Events (`SSE`)
For network-based HTTP agents:
```bash
python -m mcp_server.server --host 0.0.0.0 --port 8001
```
- **Endpoints**:
  - `GET /health` and `GET /ready`: Returns service status and tenant configuration check.
  - `GET /sse`: Establishes the Server-Sent Events stream and returns session endpoint.
  - `POST /messages?session_id=<uuid>`: Receives JSON-RPC requests and dispatches responses over the active SSE session.

---

## 4. Tenant Isolation & Access Control

- **Server-Side Tenant Resolution**: The authorized organization is defined exclusively by the `CY_MCP_TENANT_ID` server environment variable. The server queries the database for that tenant's active integration. **Caller-supplied tenant identifiers are never accepted as proof of authorization.**
- **Dual Authentication Mechanisms**:
  1. **Capability-Path Key (`CY_MCP_SECRET_PATH_KEY`)**:
     Designed for clients such as iGentic that cannot inject custom HTTP headers.
     - Endpoints: `GET /sse/{secret_key}` and `POST /messages/{secret_key}?session_id=<uuid>`
     - Constant-time secret comparison using `hmac.compare_digest`.
     - Standard unauthenticated routes (`/sse`, `/messages`) reject unauthorized callers with `HTTP 401 Unauthorized`.
  2. **Bearer Token Guard (`CY_MCP_AUTH_TOKEN`)**:
     For clients supporting standard headers (`Authorization: Bearer <token>` or `X-MCP-Token: <token>`).
- **Production Fail-Closed Guarantee**:
  In production (`CY_ENV=production`), application startup fails immediately (`ValueError`) if neither `CY_MCP_SECRET_PATH_KEY` nor `CY_MCP_AUTH_TOKEN` is configured. Unauthenticated public exposure is strictly prevented.

---

## 5. iGentic Connection & Render Deployment

### 5.1 iGentic Connection via Capability URL
Because the iGentic tool configuration UI accepts a Server URL but does not provide custom HTTP authorization headers, iGentic connects using the capability URL path.

1. **Generate a High-Entropy Secret Key**:
   Generate a cryptographic secret (at least 32 bytes / 43 base64 characters):
   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```
2. **Configure Render Environment**:
   Set `CY_MCP_SECRET_PATH_KEY` to this generated secret value in the Render service settings.
3. **Configure iGentic**:
   In the iGentic MCP configuration screen:
   - **Server URL**: `https://<render-service-name>.onrender.com/sse/<secret-path-key>`
   - **Transport**: `sse`
   - **Headers**: None required.

### 5.2 Infrastructure Security & URL Path Protection
> [!WARNING]
> **URL Path Credentials Notice**:
> Capability keys embedded in URL paths can be captured in intermediary web proxies, CDN access logs, and browser histories.
> - Configure reverse-proxy / CDN access logging to redact the path key or truncate URI paths for `/sse/*` and `/messages/*`.
> - Never print or log the capability URL or path key in application logging.
> - Rotate `CY_MCP_SECRET_PATH_KEY` periodically or immediately if an unauthorized party obtains the URL.

### 5.3 Service Separation Architecture
To prevent credential mixing and accidental disruption, three isolated Render Blueprints are maintained:
1. `render.yaml`: Main Channel Yield multi-tenant analytics service.
2. `render-mcp-demo.yaml`: Standalone dummy-data MCP demo service (`zoho-recruit-mcp-demo`) requiring zero secrets.
3. `render-mcp.yaml`: Real Zoho Recruit MCP server (`zoho-recruit-mcp`) requiring database connection and capability key.

When approved, deploy the real MCP service independently:
```bash
render blueprint launch render-mcp.yaml
```
