# Analytics Service Test Report

**Result: 8/8 tests passed.**

All tests use synthetic fixture data. This is a prototype, not production certification.

## Tests

- **PASS** — analytics fixture ingestion completes: `completed_with_warnings`
- **PASS** — unknown source is reported and excluded from named channels: `{'applications_in_cohort': 5, 'known_source_applications': 4, 'unknown_source_applications': 1, 'source_coverage_percent': 80.0, 'confirmed_mature_joins': 1, 'channels_with_yield': 2, 'ranking_eligible_channels': 0}`
- **PASS** — active and immature outcomes do not count as mature failures: `[{'Agency': 1}, {'Employee Referral': 1}, {'LinkedIn': 0}]`
- **PASS** — ranking disabled without approved sample threshold: `['unknown_source_applications_excluded_from_named_channel_yield', 'ranking_disabled_until_minimum_sample_size_policy_is_configured', 'some_cost_records_are_not_requisition_allocated', 'pending_or_immature_outcomes_excluded_from_yield_denominator']`
- **PASS** — ranking only includes channels meeting configured threshold: `[{'rank': 1, 'channel': 'Employee Referral', 'yield_percent': 100.0, 'mature_eligible_applications': 1, 'confirmed_joins': 1}, {'rank': 2, 'channel': 'LinkedIn', 'yield_percent': 0.0, 'mature_eligible_applications': 1, 'confirmed_joins': 0}]`
- **PASS** — tenant scope prevents reading another client's records: `{'applications_in_cohort': 0, 'known_source_applications': 0, 'unknown_source_applications': 0, 'source_coverage_percent': None, 'confirmed_mature_joins': 0, 'channels_with_yield': 0, 'ranking_eligible_channels': 0}`
- **PASS** — unknown requisition returns explicit not-found result: `requisition_not_found_for_client`
- **PASS** — invalid observation policy is rejected: `negative observation_days raises ValueError`

## Important interpretation notes

- The demo uses a 90-day observation window only as an example; the client must approve the maturity policy.
- Ranking is disabled unless a minimum sample-size policy is explicitly supplied. The threshold of 1 used in one test is only a test input, not a recommendation.
- Unknown source applications are counted for coverage but excluded from named-channel yield.
- Funnel metrics require stage-event records. Current stage is not used to invent historical funnel transitions.
- Costs are shown by channel and currency; no currency conversion or cost-per-hire calculation is performed. Unallocated campaign costs are not attributed to a specific requisition.
- Client scoping is applied in database queries, but production authorization must also enforce tenant access outside this library.
- The current yield policy treats mature confirmed joins and mature rejected/withdrawn outcomes as denominator-eligible. Client outcome/maturity policy must be approved and versioned.
