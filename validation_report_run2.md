# Validation Run 2 — Maturity-Aware Calculation and Negative-Test Plan

**Status:** The reference calculation and expected negative-test specification have been updated. The negative scenarios are defined but the full ingestion validator has not yet been implemented, so they are not all execution-tested.

## Policy for this demonstration
- Cohort: applications applied between inclusive `cohort_start` and `cohort_end`.
- Cutoff: explicit `as_of_date`.
- Observation window: example 90 calendar days after application.
- Confirmed joined outcomes count only if the application is mature under this strict cohort policy.
- Mature rejected/withdrawn outcomes count in denominator but not numerator.
- Active/unknown outcomes and immature outcomes are reported separately.
- Unknown source is reported separately and excluded from named-channel yield.

**The 90-day window is not a universal recommendation.** It must be configurable and approved by each client based on hiring cycle and business definitions.

## Calculation output using the synthetic fixture
Using cohort 2026-01-01 through 2026-06-30, as-of date 2026-10-11, and a 90-day illustrative window:

| Channel | Mature eligible applications | Joined | Pending/immature | Yield |
|---|---:|---:|---:|---:|
| Employee Referral | 1 | 1 | 1 | 100.00% |
| LinkedIn | 1 | 0 | 0 | 0.00% |
| Agency | 0 | 0 | 1 | Not applicable |
| unknown | 0 | 0 | 0 | Not applicable |

These values are still **not suitable for channel ranking**: the fixture is tiny, and one observation per named channel provides no meaningful comparative evidence. The report is testing treatment of pending outcomes and explicit cutoff logic, not demonstrating predictive validity.

## Negative tests specified
Duplicate IDs, missing IDs, invalid timestamps, cross-tenant references, invalid stage timeline, joined outcome without join evidence, invalid last-touch ordering, duplicate ingestion/idempotency, zero denominator, immature active outcomes, and mature final non-join outcomes.

## Next engineering action
Implement and execute a semantic validation harness that injects invalid fixtures and verifies expected reject/warn/exclude behavior. Then add ranking eligibility thresholds and tests before exposing analytics to iGentic.
