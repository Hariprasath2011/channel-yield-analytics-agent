# Validation Run 1 — Canonical Data Contract v1.0

**Status:** Initial synthetic-fixture checks completed. This is not production certification and does not validate any real client data.

## Summary

- JSON Schema validation: **PASS** — 0 schema errors across the synthetic canonical bundle.
- Semantic checks: **PASS** — 9 checks passed.
- Calculation reference: **Executed**, but not ready for production use because the outcome observation window/cohort maturity policy is not implemented.

## Checks performed

| Check | Result | Observation |
|---|---|---|
| Schema structure and field types | PASS | Bundle conforms to JSON Schema Draft 2020-12, including date/time format checks. |
| Unique application IDs | PASS | 5 application records; IDs unique within the demo client. |
| Application → requisition references | PASS | Every application references a requisition in the same client. |
| Stage event → application references | PASS | Every stage event references an application in the same client. |
| Stage timeline ordering | PASS | No event has `exited_at` earlier than `entered_at`. |
| Joined outcome has join timestamp | PASS | The confirmed joined record has `joined_at`. |
| Last-touch source evidence ordering | PASS | All rows claiming last-touch attribution have evidence before application time. |
| Tenant scope in fixture | PASS | All sample rows belong to `demo_client`. |
| Unknown source retained explicitly | PASS | One application uses `unknown`; it is not assigned to a named channel. |
| Application grain | PASS | The same candidate has two distinct applications for two requisitions, and both are retained. |
| Cost amount/currency shape | PASS | Sample costs are nonnegative and use uppercase three-letter currency codes. |

## Calculation output from the illustrative reference function

| Channel | Eligible applications counted by demo function | Joined applications | Yield returned | Pending outcomes | Unknown source applications |
|---|---:|---:|---:|---:|---:|
| Employee Referral | 1 | 1 | 100.00% | 1 | 0 |
| LinkedIn | 1 | 0 | 0.00% | 0 | 0 |
| Agency | 0 | 0 | Not applicable | 1 | 0 |
| unknown | 0 | 0 | Not applicable | 0 | 1 |

**Important interpretation warning:** These figures are a test of code behavior, not meaningful channel performance. The sample is tiny, outcomes are at different stages, and the current illustrative function does not enforce a configured outcome-maturity window. In particular, the 100% Employee Referral result must not be used as a recommendation or ranking.

## Gaps discovered / next validation work

1. **Implement outcome maturity explicitly.** Define an `as_of_date`, an approved observation window or cohort policy, and treatment for pending/censored outcomes. Don't treat immature applications as failures.
2. **Add negative tests.** Inject duplicate IDs, cross-tenant references, missing required fields, invalid timestamps, joined outcome without join evidence, and source evidence at/after application time; verify they are rejected or flagged as designed.
3. **Fix the yield calculation interface.** It should accept an explicit reporting cohort and `as_of_date`, return coverage/completeness and maturity counts, and refuse to rank channels when evidence is insufficient.
4. **Validate client-configurable mappings.** Test unknown channel/stage values and the approved fallback behavior.
5. **Add idempotency/lineage tests.** Re-ingesting the same source record should not duplicate it, and the source key/configuration version should be traceable.
6. **Keep schema and semantic validation distinct.** JSON Schema validates record shape; cross-record rules and tenant isolation need separate validation code.

## Decision

**Proceed to Validation Run 2: negative tests + cohort maturity policy.** Do not connect the reference calculation to the iGentic agent as an authoritative analytics tool until those items are implemented and tested.
