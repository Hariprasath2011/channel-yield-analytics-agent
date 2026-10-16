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

- **Cryptographic Tenant Resolution**: The authorized organization is derived from a customer-specific, cryptographically signed connection credential (`cyct_...`) issued by `POST /v2/integrations/zoho/mcp-token`. The server validates HMAC-SHA256 signatures and claims (`tid`, `sub`, `exp`, `iss`, `aud`) on every connection and message. **Caller-supplied tenant identifiers cannot override verified credentials.**
- **Legacy Dev-Mode Fallback**: `CY_MCP_TENANT_ID` is disabled in production (`CY_ENV=production`) and is only permitted for single-tenant local debugging when `CY_MCP_DEV_MODE=true`.
- **Authentication Mechanisms**:
  1. **Customer Connection Token (`/sse/{connection_token}`)**:
     Standard customer flow for clients like iGentic that connect using an SSE URL.
     - Endpoints: `GET /sse/{connection_token}` and `POST /messages/{connection_token}?session_id=<uuid>`
     - Bound to the customer's verified tenant ID and authenticated user.
     - Cross-session tenant mismatch is rejected with HTTP 403.
     - Unauthenticated tool executions are rejected with HTTP 401.
  2. **Operational Capability-Path Key (`CY_MCP_SECRET_PATH_KEY`)**:
     Optional static path key reserved for operational health checks or administrative access.
  3. **Bearer Token Guard (`CY_MCP_AUTH_TOKEN`)**:
     For clients supporting standard headers (`Authorization: Bearer <token>` or `X-MCP-Token: <token>`).
- **Production Fail-Closed Guarantee**:
  In production (`CY_ENV=production`), application startup fails immediately (`ValueError`) if no signing secret (`CY_MCP_SIGNING_SECRET` / `CY_SECRET_KEY`) or authentication mechanism is configured. Unauthenticated public exposure is strictly prevented.

---

## 5. iGentic Connection & Render Deployment

### 5.1 iGentic Customer Connection via Signed Connection Token
In iGentic, each customer connects to their own isolated Zoho Recruit data using a signed connection token:

1. Customer signs into the Channel Yield web application.
2. Customer connects their Zoho Recruit account via OAuth 2.0.
3. Customer navigates to Integrations -> Zoho Recruit -> Get MCP Connection Token (`POST /v2/integrations/zoho/mcp-token`).
4. In iGentic MCP configuration:
   - **Server URL**: `https://<render-service-name>.onrender.com/sse/<connection-token>`
   - **Transport**: `sse`
   - **Headers**: None required.

### 5.2 Infrastructure Security & URL Path Protection
> [!WARNING]
> **URL Path Credentials Notice**:
> Connection tokens in URL paths should be treated as sensitive credentials:
> - Configure reverse-proxy / CDN access logging to redact the path token for `/sse/*` and `/messages/*`.
> - Never print or log the connection token in application logging.
> - Default token lifetime is 24 hours (configurable up to 7 days).

### 5.3 Service Deployment on Render Free Tier
To run the real multi-tenant MCP server on Render's free tier without creating a second paid service:
1. The existing service `zoho-recruit-mcp-demo` is updated via `render-mcp-demo.yaml` with:
   - Build Command: `pip install -r requirements-mcp.txt`
   - Start Command: `python -m mcp_server.server --host 0.0.0.0 --port $PORT`
   - Health Check: `/health`
   - Plan: `free`
2. Rollback to the fictional dummy demo is preserved in `mcp_demo/render-mcp-demo.original.yaml`.
