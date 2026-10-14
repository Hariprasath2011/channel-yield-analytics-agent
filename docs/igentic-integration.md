# iGentic Direct HTTP API Integration Contract & Status

## Executive Status: BLOCKED FOR PRODUCTION

| Integration Path | Status | Verification Detail |
| :--- | :--- | :--- |
| **Direct HTTP API (Production)** | **BLOCKED** | Direct HTTP API cannot send `Authorization: Bearer` headers, custom headers, API keys, or OAuth credentials. Exposing private analytics without request authentication violates security requirements. |
| **Direct HTTP API (Synthetic Demo)** | **ISOLATED / CONDITIONAL** | Dedicated, ephemeral deployment with synthetic-only fixture data, no customer credentials, read-only rate limits, and disposable database. Never connected to real client records. |
| **Backend Authenticated Routes** | **ACTIVE** | Available for authenticated web frontends, authorized machine clients, CLI tools, and integration tests using Bearer JWT or `cyk_...` API keys. |

---

## 1. Verified Facts vs. Unknown Capabilities

### 1.1 Verified Facts
1. The iGentic interface includes an action/tool designated as **Direct HTTP API**.
2. Direct HTTP API can initiate HTTP GET and POST requests to internet-accessible endpoints.
3. Access to create and configure tools in iGentic is restricted to authorized team members.
4. Requests from iGentic to external endpoints traverse public networks.

### 1.2 User-Reported Limitations
1. The Direct HTTP API tool cannot validate authentication or provide standard security mechanisms required for authenticated API calls.
2. The tool cannot be assumed to support `Authorization: Bearer <token>`, custom headers, request signing, or OAuth handshakes.

### 1.3 Unknown Capabilities (Marked UNKNOWN)
* **Static Egress IP Range**: UNKNOWN. No documented or guaranteed CIDR block for iGentic runner egress.
* **Mutual TLS (mTLS)**: UNKNOWN. No verified client certificate configuration UI.
* **Alternative Connectors**: UNKNOWN. Whether iGentic provides another tool type (e.g. "OAuth REST Connector") compatible with RFC 6749 Client Credentials has not been verified.

---

## 2. Integration Contract & Trust Boundary

### 2.1 The Ingress Problem
```
[Authorized Users]
        │ (Login session)
        ▼
[iGentic Workspace]
        │
        │ Direct HTTP API (No custom headers / No Bearer tokens / Public Internet)
        ▼
[Public Internet] ──────────► [Channel Yield Analytics Backend]
                                          ▲
                                          │ ??? How does backend distinguish
                                          │ legitimate iGentic request from an
                                          │ attacker on the public internet?
```

Because requests from iGentic traverse the public internet without an authentication credential, the backend has no cryptographic mechanism to verify:
1. That the request originated from iGentic.
2. That the caller is authorized.
3. Which tenant’s recruitment analytics should be returned.

### 2.2 Rejection of Insecure Workarounds
- **Removing Authentication**: An unauthenticated endpoint on Render would allow anyone on the internet to read recruitment pipeline analytics, salary/cost structures, and requisition details.
- **Passing Tenant ID in Query/Body**: Accepting `?tenant_id=xyz` without credentials allows trivial cross-tenant data harvesting.
- **Secret URL Path (`/v1/analytics/secret-path-xyz`)**: URLs are recorded in proxy logs, referrer headers, browser caches, and cloud provider access logs. This violates standard security policies.

---

## 3. Minimum Requirements to Unblock Production Integration

Production integration will be unblocked when one of the following platform capabilities is verified:

### Option A: Header Injection (Recommended)
iGentic Direct HTTP API must support configuring custom HTTP headers:
```http
GET /v2/analytics?cohort_start=2026-01-01&cohort_end=2026-06-30 HTTP/1.1
Host: analytics.yourdomain.com
Authorization: Bearer cyk_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```
The token is a tenant-scoped API key created via the administrative bootstrap or settings UI, hashed with SHA-256 in the backend database.

### Option B: OAuth 2.0 Client Credentials Grant
If iGentic supports a two-legged OAuth flow:
1. iGentic calls `/v2/auth/oauth/token` with `client_id` and `client_secret`.
2. Backend returns short-lived JWT (30-minute validity) scoped to that tenant.
3. iGentic caches and passes the JWT in subsequent tool calls.

### Option C: Private Network / VPN / Gateway
Deploy the Channel Yield backend inside a private VPC, reachable only through a dedicated private interconnect or tunnel (e.g., Cloudflare Tunnel with Access Service Tokens or AWS PrivateLink).

---

## 4. Synthetic-Only Demonstration Policy

If a demonstration or workshop is conducted using iGentic prior to connector unblocking:
1. **Isolated Infrastructure**: Run on an ephemeral instance (e.g., disposable Render demo service) completely separate from production databases and networks.
2. **Synthetic Data Only**: Use exclusively the synthetic fixtures (`applications.csv`, `requisitions.csv`, etc.). Zero real employee, candidate, or client data.
3. **No Privileged Operations**: The endpoint must be strictly read-only aggregate analytics; no write endpoints, user administration, or Zoho credentials exist in the demo environment.
4. **Explicit Disclaimer**: The demo endpoint must never be portrayed as a production-ready solution.
