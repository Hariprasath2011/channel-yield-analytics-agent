# Local-first and Render demo guide

## Safety boundary
- Use synthetic fixture data only on Render.
- Do not commit real API tokens, `.env` files, credentials, or recruitment data.
- The API is read-only and requires `Authorization: Bearer <CY_API_TOKEN>` for `/v1/analytics`. The included Render start command rebuilds the SQLite database from synthetic CSV fixtures on each instance start; this is deliberately disposable demo behavior, not a persistence strategy.
- `/health` is intentionally public and returns no data.
- Render Free web services can sleep and their filesystem is ephemeral. This demo database is created during build and is only for a disposable synthetic-data test. Do not use this pattern for real client data or production.
- iGentic currently shows Client Credential and Azure DevOps authentication only. Its compatibility with this API's bearer-token header has not been established, so successful deployment does not yet mean iGentic can authenticate to it.

## Local setup
Use Python 3.11+ if available. From this folder:

```bash
python csv_ingestion_pipeline.py --self-test
python analytics_service.py --self-test
```

Create the demo SQLite database from synthetic CSV fixtures:

```bash
python csv_ingestion_pipeline.py --config ingestion_config_demo.json --db demo_ingestion.sqlite
```

Set environment variables in your terminal (use a strong random token, never a checked-in literal):

PowerShell:
```powershell
$env:CY_API_TOKEN = "<strong-random-secret>"
$env:CY_CLIENT_ID = "demo_client"
$env:CY_DB_PATH = "demo_ingestion.sqlite"
python analytics_api.py --host 127.0.0.1 --port 8080
```

macOS/Linux:
```bash
export CY_API_TOKEN='<strong-random-secret>'
export CY_CLIENT_ID='demo_client'
export CY_DB_PATH='demo_ingestion.sqlite'
python analytics_api.py --host 127.0.0.1 --port 8080
```

In another terminal, check health:
```bash
curl http://127.0.0.1:8080/health
```

Test analytics (replace token and dates as appropriate):
```bash
curl -H "Authorization: Bearer <strong-random-secret>" \
  "http://127.0.0.1:8080/v1/analytics?cohort_start=2026-01-01&cohort_end=2026-06-30&as_of_date=2026-10-11&observation_days=90"
```

## Render test deployment
1. Put this project in a Git repository you are allowed to use. Review the files first; the CSV fixtures are synthetic.
2. Create a Render Web Service from that repository and use the included `render.yaml` Blueprint configuration, or set the build/start/health-check values from it manually. The start command imports the synthetic fixtures into a fresh local SQLite database before launching the API, so the demo does not depend on a database file surviving between deployments.
3. Add `CY_API_TOKEN` as a secret environment variable in Render. Generate a unique random token. Do not put the token in the repository or share it in screenshots.
4. Deploy, then test `https://<your-service>.onrender.com/health`.
5. Test `/v1/analytics` with a bearer token from a secure API client.

Do not connect iGentic yet: first confirm whether Direct HTTP API can send the required Authorization header or whether a compatible OAuth token provider is available. Never disable API authentication to make the test work.
