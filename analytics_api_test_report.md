# Analytics API Smoke Test Report

**Result: 8/8 passed.** These tests use synthetic data and a local ephemeral HTTP server.

| Test | Result |
|---|---|
| Health endpoint responds | Pass |
| Missing bearer token rejected | Pass |
| Invalid bearer token rejected | Pass |
| Authorized analytics request returns structured JSON | Pass |
| Invalid observation policy rejected | Pass |
| Unknown query parameter rejected | Pass |
| Unknown requisition returns 404 | Pass |
| POST/write method rejected | Pass |

## Limitations

This verifies the local prototype's basic HTTP contract only. It does not establish production security, TLS, rate limiting, multi-tenant identity mapping, availability, performance, or actual iGentic connectivity. The API is configured for one client per server instance and must be deployed behind approved HTTPS infrastructure before a hosted agent can call it.
