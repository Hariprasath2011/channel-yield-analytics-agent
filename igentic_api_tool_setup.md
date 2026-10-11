# Configure iGentic to call Channel Yield Analytics API

## Current status

This is an integration guide for the read-only prototype. It has not been connected to an iGentic workspace. The API is scoped to one client per running instance using server environment variables; do not accept a caller-provided client/tenant ID as authorization.

## 1. Start a local demo API

From this directory, first load the synthetic fixtures into a local database:

```bash
python csv_ingestion_pipeline.py --config ingestion_config_demo.json --db demo_ingestion.sqlite
```

Set environment variables in your shell (use a unique random secret; never put it in the agent prompt or source control):

```bash
export CY_API_TOKEN='replace-with-a-long-random-secret'
export CY_CLIENT_ID='demo_client'
export CY_DB_PATH='./demo_ingestion.sqlite'
python analytics_api.py --host 127.0.0.1 --port 8080
```

Local smoke tests in another terminal:

```bash
curl http://127.0.0.1:8080/health
curl -H "Authorization: Bearer $CY_API_TOKEN" \
  'http://127.0.0.1:8080/v1/analytics?cohort_start=2026-01-01&cohort_end=2026-06-30&as_of_date=2026-10-11&observation_days=90'
```

`127.0.0.1` is only reachable from the same machine. iGentic's hosted service cannot call your laptop's loopback address. For a real integration, deploy behind HTTPS in an approved environment or use an approved secure network path. Do not expose the development server directly to the public internet.

## 2. Configure the iGentic API tool

In iGentic's API/tool configuration UI, create a GET request using the deployed HTTPS base URL:

- **Method:** `GET`
- **Path:** `/v1/analytics`
- **Authentication:** Bearer token; store the token in a secret/credential field if iGentic provides one.
- **Query parameters:** `cohort_start`, `cohort_end`, `as_of_date`, `observation_days`; optional `requisition_id` and `min_sample_size`.
- **Response:** JSON object returned by the API.
- **Do not configure POST/write operations.**

If iGentic asks for an OpenAPI document, import `analytics_api_openapi.yaml` and replace its server URL with the deployed HTTPS endpoint. If it asks for a JSON schema instead, use the example response from `analytics_service_demo_output.json` as a starting point, then formalize the schema before production.

## 3. Suggested tool description for the agent

> Retrieves authoritative recruitment channel yield analytics for the authenticated client and requested application cohort. Always pass the requested date range and requisition ID when known. Explain the returned metrics and warnings; never recalculate or invent metrics. If a metric is unavailable or data-quality warnings exist, disclose that. Do not set `min_sample_size` unless an approved client ranking policy is provided. This tool is read-only.

## 4. Prompting and error handling

- Ask a clarifying question if the requisition or reporting period is ambiguous.
- Use `status`, `error`, `data_quality.warnings`, `yield_status`, and `ranking_status` from the response.
- Treat `404` as a requisition not found for this API's configured client; do not retry against another tenant.
- Treat `400` as a request/policy issue; correct the input rather than retrying unchanged.
- Treat `401` as a credential/configuration issue; do not reveal the token.
- Treat `500`/`503` as service errors; do not fabricate fallback numbers.
- Keep personally identifying candidate data out of the agent request; this API returns aggregate analytics only.

## 5. Before production

This standard-library server is a prototype, not a production deployment. Add HTTPS termination, managed secrets and rotation, rate limiting, request IDs, structured audit logs, monitoring, bounded query windows, database transaction/concurrency strategy, hardened input schemas, deployment health checks, and security review. For multiple clients, use authenticated identity-to-tenant authorization in the server; do not trust a query parameter for tenant scope. Verify that iGentic supports secure bearer credentials and can reach the deployed endpoint before committing to the final adapter.
