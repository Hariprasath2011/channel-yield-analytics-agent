# Validation Run 4 — Idempotency and Source Lineage

**Status:** Executed against synthetic fixtures. Not production certification.

## Results
- Base fixture semantic validation: **PASS**
- Automated idempotency/lineage tests: **14/14 PASS**
- Expected warning: APP-003 has unknown source channel.

## Behaviors demonstrated
- First import inserts new source records.
- Re-importing the same source records does not add duplicates.
- A changed record from the same stable source key updates the existing record.
- A conflicting canonical ID from a different source key is flagged for reconciliation.
- Canonical rows now carry `source_system_id`, `source_record_id`, `mapping_version`, and `ingest_run_id`.

## Important implementation notes
- The idempotency key is `(client_id, source_system_id, source_record_id)`.
- A canonical entity ID conflict from a different source key is not automatically merged; it is flagged.
- This is an in-memory demonstration. Production needs persistent storage, transaction-safe upserts, unique constraints, retry handling, and an audit/event history.
- Fingerprint normalization must be specified for types, null/blank equivalence, timestamps, and source fields before production use.
- Re-ingestion semantics can differ by connector; some systems provide stable record IDs, while file feeds may require a configured composite key.
- Tenant authorization must be enforced by the backend and database, not only by this validator.
