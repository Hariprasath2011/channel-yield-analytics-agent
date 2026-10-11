# Validation Run 3 — Executable Semantic Validator

**Status:** Executed against the synthetic fixture. This is an initial test harness, not production certification.

## Results
- Base fixture: **PASS**
- Base fixture errors: **0**
- Base fixture warnings: **1**
- Negative tests: **8/8 passed**

The base fixture's one warning is expected: `APP-003` has an unknown source channel.

## Negative test results
- **PASS** — duplicate application ID (expected signal: `application_duplicate_application_id`)\n- **PASS** — missing application ID (expected signal: `application_missing_application_id`)\n- **PASS** — invalid application timestamp (expected signal: `application_invalid_applied_at`)\n- **PASS** — cross-tenant requisition reference (expected signal: `application_requisition_not_found_same_tenant`)\n- **PASS** — missing join evidence (expected signal: `joined_outcome_missing_join_evidence`)\n- **PASS** — invalid last-touch ordering (expected signal: `source_evidence_not_before_application`)\n- **PASS** — bad stage timeline (expected signal: `stage_event_exited_before_entered`)\n- **PASS** — unknown source is warned (expected signal: `unknown_source_channel`)\n
## Important limitations
1. This harness checks a starter subset of semantic rules. It does not yet validate every JSON Schema constraint or enforce all production data-quality policies.
2. Duplicate IDs are currently flagged. Production ingestion must distinguish an identical replay of the same source record (idempotent upsert) from conflicting duplicate records, using source-system identity and lineage.
3. The harness flags cross-tenant and cross-reference problems, but authentication/authorization enforcement must also exist in the real storage and API layers.
4. The observation-window calculator is separate from this validator. The client-specific maturity policy and minimum sample-size ranking rule still require approval.
5. These tests use synthetic records only; no real client system or iGentic integration has been tested.

## Decision
The starter semantic validator passes its current fixture and all 8 negative tests. Next, extend the tests to cover JSON Schema validation, idempotent re-import with source lineage, cost period consistency, channel/stage mapping errors, and cohort maturity/ranking eligibility.
