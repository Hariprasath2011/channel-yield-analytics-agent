# Validation Run 2 — negative and maturity tests

## Maturity policy used for this test run
- Cohort membership: `applied_at` date within inclusive start/end dates.
- `as_of_date`: explicit cutoff date.
- Example observation window: 90 calendar days after application.
- Mature confirmed joins count in numerator and denominator.
- Mature rejected/withdrawn applications count in denominator, not numerator.
- Active/unknown outcomes are pending and excluded from the mature denominator.
- Unknown source is reported separately and excluded from named-channel yield.
- The 90-day window is illustrative only and must be configurable and approved per client.

## Expected negative tests
1. Duplicate application ID in same tenant with conflicting values -> reject/quarantine conflict.
2. Missing `application_id` -> reject/quarantine.
3. Invalid `applied_at` -> reject/quarantine.
4. Application references requisition from another client -> reject/quarantine.
5. Stage event references nonexistent application -> reject/quarantine.
6. `exited_at` before `entered_at` -> reject/quarantine.
7. `joined` outcome without `joined_at` -> flag as inconsistent; exclude from confirmed joins pending reconciliation.
8. Source evidence timestamp equal to or later than `applied_at` while claiming last-touch-before-application -> reject attribution claim; use approved fallback or unknown.
9. Duplicate import of same stable source record -> idempotent upsert; no additional application.
10. Zero mature denominator -> yield null/not applicable, not 0%.
11. Immature active application -> pending/immature, not counted as a non-join.
12. Mature rejected application -> denominator yes, numerator no, assuming outcome status is final and policy-approved.

## Important caveat
The small reference function demonstrates cohort maturity behavior. It does not yet implement the complete ingestion validator, duplicate reconciliation, ranking minimum sample sizes, or all production-grade semantic checks.
