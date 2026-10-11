# Channel Yield Analytics Agent — Canonical Data Contract v1.0

**Status:** Proposed design with synthetic fixtures; not implemented against real client data.

## Contents
- `canonical_schema_v1.json`: JSON Schema Draft 2020-12 for the canonical bundle.
- `requisitions.csv`: synthetic requisitions.
- `applications.csv`: synthetic application-grain records, including unknown source and pending outcomes.
- `stage_events.csv`: synthetic stage history.
- `costs_optional.csv`: optional synthetic cost records.
- `validation_rules.md`: semantic checks that JSON Schema alone cannot enforce.
- `test_cases.md`: initial expected behaviors.
- `yield_calculation.py`: small illustrative calculation reference.

CSV blank cells must be normalized to null by ingestion. Cross-record uniqueness, foreign keys, tenant boundaries, attribution ordering, and cohort maturity require semantic validation. Replace the example.invalid schema ID before publishing.

## Validation Run 4 additions
- `semantic_validator.py`: executable semantic checks and in-memory idempotent upsert simulation.
- `validation_run4_output.json`: machine-readable test results.
- `validation_report_run4.md`: summary and limitations.
- Canonical CSV rows now include source lineage and ingestion metadata.


## CSV ingestion pipeline prototype
- `ingestion_config_demo.json`: source file list, client/source metadata, and configurable canonical-field-to-source-column mappings. Empty mappings use identity column names.
- `csv_ingestion_pipeline.py`: CSV loading, normalization, starter row/reference checks, quarantine, warning generation, SQLite idempotent upsert, and structured run summary.
- `ingestion_pipeline_test_output.json`: machine-readable automated test results.
- `ingestion_pipeline_test_report.md`: human-readable test report and limitations.

Run the tests with `python csv_ingestion_pipeline.py --self-test`. Run an ingestion with `python csv_ingestion_pipeline.py --config ingestion_config_demo.json --db demo_ingestion.sqlite`. Do not point this demo at real personal data until security, retention, access controls, and the validation contract have been reviewed.

## Read-only REST API prototype for iGentic
- `analytics_api.py`: standard-library HTTP API exposing `/health` and authenticated `GET /v1/analytics`. The client scope is configured server-side via environment variables; no client ID is accepted from the caller.
- `analytics_api_openapi.yaml`: OpenAPI description for importing into an API tool configuration, subject to the iGentic UI's supported schema format.
- `igentic_api_tool_setup.md`: setup, request examples, agent tool description, error handling, and production caveats.
- `analytics_api_test.py`, `analytics_api_test_output.json`, `analytics_api_test_report.md`: local HTTP smoke tests using synthetic fixtures.

Set `CY_API_TOKEN`, `CY_CLIENT_ID`, and `CY_DB_PATH`, then run `python analytics_api.py`. For example, prepare a demo DB first with `python csv_ingestion_pipeline.py --config ingestion_config_demo.json --db demo_ingestion.sqlite`. Do not expose the local development server directly to the public internet. Deploy behind HTTPS and review security before connecting a hosted agent.
