# Semantic validation rules — v1.1

## Record and relationship validation
- Required identifiers and timestamps must be present and valid.
- IDs must be unique within the configured client/source identity rules.
- Application -> requisition and stage event -> application references must resolve within the same client.
- A stage event's exited_at must not precede entered_at.
- A `joined` outcome without confirmed join evidence is inconsistent and must be flagged, not silently counted.
- If source attribution method claims `last_touch_before_application`, source evidence must be strictly earlier than applied_at.
- A source event tie requires configured deterministic tie-break or ambiguous attribution status.

## Outcome maturity (initial configurable policy)
- Every analytics request must define cohort start/end and an `as_of_date`.
- Observation window is client-configurable; 90 calendar days is only a demonstration default.
- Mature confirmed joins count as joined applications and denominator applications.
- Mature final rejected/withdrawn outcomes count as denominator applications but not joins, subject to client policy.
- Active, unknown, or unresolved outcomes are pending and excluded from mature-cohort denominator.
- An immature application is not a failure; report it separately.
- The policy must be reviewed for roles with unusually long or short hiring cycles.
- If a client defines maturity differently, e.g. requisition closure or a fixed follow-up date, implement that policy as a versioned strategy rather than changing the formula silently.

## Yield result
- Yield = mature confirmed joined applications / mature eligible applications * 100.
- If the denominator is zero, yield is null/not applicable.
- Unknown-source records must be included in source coverage reporting but not assigned to named-channel yield.
- Do not rank channels until minimum evidence/sample-size policy has been approved and met.
