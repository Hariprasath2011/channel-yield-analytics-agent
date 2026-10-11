"""Semantic validator and idempotent ingestion demo for canonical records v1.

Standard-library only. Demonstration, not production-certified.
Run: python semantic_validator.py
"""
from datetime import datetime
from copy import deepcopy
import csv

ENTITIES = {
    "requisitions": ("requisition_id",),
    "applications": ("application_id",),
    "stage_events": ("stage_event_id",),
    "costs": ("cost_id",)
}
def parse_dt(value):
    if value in (None, ""):
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("timezone required")
    return dt

def source_key(row):
    return (row.get("client_id"), row.get("source_system_id"), row.get("source_record_id"))

def content_fingerprint(row):
    # Deterministic representation for this demo; production should canonicalize
    # types/field ordering and explicitly exclude ingestion-only metadata as needed.
    stable = {k: row[k] for k in sorted(row) if k not in ("ingest_run_id",)}
    import hashlib, json
    return hashlib.sha256(json.dumps(stable, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

def idempotent_upsert(existing_rows, incoming_rows, entity_name):
    """Upsert by (client_id, source_system_id, source_record_id).

    Exact replay: skip without creating a duplicate.
    Changed source record: update the canonical row and record an update.
    Same canonical entity ID from a different source key: flag conflict for reconciliation.
    """
    key_field = ENTITIES[entity_name][0]
    index = {source_key(r): i for i, r in enumerate(existing_rows)}
    canonical_ids = {(r.get("client_id"), r.get(key_field)): source_key(r) for r in existing_rows}
    counts = {"inserted": 0, "unchanged_replays": 0, "updated": 0, "conflicts": 0}
    out = deepcopy(existing_rows)
    for incoming in incoming_rows:
        sk = source_key(incoming)
        if not all(sk):
            counts["conflicts"] += 1
            continue
        if sk in index:
            i = index[sk]
            if content_fingerprint(out[i]) == content_fingerprint(incoming):
                counts["unchanged_replays"] += 1
            else:
                # Stable source record has changed: update canonical row.
                out[i] = deepcopy(incoming)
                counts["updated"] += 1
            continue
        canonical_key = (incoming.get("client_id"), incoming.get(key_field))
        if canonical_key in canonical_ids and canonical_ids[canonical_key] != sk:
            counts["conflicts"] += 1
            continue
        index[sk] = len(out)
        canonical_ids[canonical_key] = sk
        out.append(deepcopy(incoming))
        counts["inserted"] += 1
    return out, counts

def validate_bundle(bundle):
    errors, warnings = [], []
    client_id = bundle.get("client_id")
    if not client_id:
        errors.append("bundle_missing_client_id")
    collections = {k: bundle.get(k, []) for k in ENTITIES}
    for kind, rows in collections.items():
        id_field = ENTITIES[kind][0]
        seen_ids, seen_sources = set(), set()
        for i, row in enumerate(rows):
            if row.get("client_id") != client_id:
                errors.append(f"{kind}_cross_tenant_record:{i}")
            for field in ("source_system_id", "source_record_id", "mapping_version", "ingest_run_id"):
                if not row.get(field):
                    errors.append(f"{kind}_missing_{field}:{i}")
            entity_id = row.get(id_field)
            if not entity_id:
                errors.append(f"{kind}_missing_{id_field}:{i}")
            elif (row.get("client_id"), entity_id) in seen_ids:
                errors.append(f"{kind}_duplicate_{id_field}:{entity_id}")
            else:
                seen_ids.add((row.get("client_id"), entity_id))
            sk = source_key(row)
            if all(sk) and sk in seen_sources:
                errors.append(f"{kind}_duplicate_source_key:{sk}")
            seen_sources.add(sk)
    req_keys = {(r.get("client_id"), r.get("requisition_id")) for r in collections["requisitions"]}
    app_keys = {(r.get("client_id"), r.get("application_id")) for r in collections["applications"]}
    for app in collections["applications"]:
        aid = app.get("application_id")
        if (app.get("client_id"), app.get("requisition_id")) not in req_keys:
            errors.append(f"application_requisition_not_found_same_tenant:{aid}")
        try: applied = parse_dt(app.get("applied_at"))
        except Exception: applied = None; errors.append(f"application_invalid_applied_at:{aid}")
        if not app.get("applied_at"):
            errors.append(f"application_missing_applied_at:{aid}")
        try: joined = parse_dt(app.get("joined_at"))
        except Exception: joined = None; errors.append(f"application_invalid_joined_at:{aid}")
        if app.get("application_outcome") == "joined" and joined is None:
            warnings.append(f"joined_outcome_missing_join_evidence:{aid}")
        method = app.get("source_attribution_method")
        if method == "last_touch_before_application":
            try: evidence = parse_dt(app.get("source_evidence_at"))
            except Exception: evidence = None
            if evidence is None:
                warnings.append(f"last_touch_claim_without_evidence:{aid}")
            elif applied and evidence >= applied:
                warnings.append(f"source_evidence_not_before_application:{aid}")
        if not app.get("source_channel") or app.get("source_channel","").strip().lower() == "unknown":
            warnings.append(f"unknown_source_channel:{aid}")
    for event in collections["stage_events"]:
        eid = event.get("stage_event_id")
        if (event.get("client_id"), event.get("application_id")) not in app_keys:
            errors.append(f"stage_event_application_not_found_same_tenant:{eid}")
        try: entered = parse_dt(event.get("entered_at"))
        except Exception: entered = None; errors.append(f"stage_event_invalid_entered_at:{eid}")
        try: exited = parse_dt(event.get("exited_at"))
        except Exception: exited = None; errors.append(f"stage_event_invalid_exited_at:{eid}")
        if entered and exited and exited < entered:
            errors.append(f"stage_event_exited_before_entered:{eid}")
    for cost in collections["costs"]:
        try:
            if float(cost.get("amount")) < 0: errors.append(f"cost_negative_amount:{cost.get('cost_id')}")
        except Exception: errors.append(f"cost_invalid_amount:{cost.get('cost_id')}")
        cur = cost.get("currency","")
        if not (isinstance(cur,str) and len(cur)==3 and cur.isalpha() and cur.isupper()):
            errors.append(f"cost_invalid_currency:{cost.get('cost_id')}")
    return {"errors":errors,"warnings":warnings,"passed":not errors}

def load_bundle(folder):
    def read(name):
        with open(folder/name, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    return {"client_id":"demo_client","requisitions":read("requisitions.csv"),
            "applications":read("applications.csv"),"stage_events":read("stage_events.csv"),
            "costs":read("costs_optional.csv")}

def run_tests(folder):
    base = load_bundle(folder)
    results = []
    def record(name, passed, detail):
        results.append({"test":name,"result":"PASS" if passed else "FAIL","detail":detail})
    for entity in ENTITIES:
        rows = base[entity]
        once, c1 = idempotent_upsert([], rows, entity)
        twice, c2 = idempotent_upsert(once, rows, entity)
        record(f"{entity}: initial import inserts all", c1["inserted"] == len(rows), str(c1))
        record(f"{entity}: identical re-import is idempotent", c2["unchanged_replays"] == len(rows) and len(twice)==len(once), str(c2))
        changed = deepcopy(rows[0])
        changed["ingest_run_id"] = "demo-run-002"
        changed[next(k for k in changed if k not in ("ingest_run_id",))] = changed[next(k for k in changed if k not in ("ingest_run_id",))]
        # Mutate a business field specific to each entity to prove update path.
        field = {"requisitions":"job_title","applications":"current_stage","stage_events":"stage","costs":"amount"}[entity]
        changed[field] = str(changed[field]) + "_updated"
        updated, cu = idempotent_upsert(once, [changed], entity)
        record(f"{entity}: changed source record updates existing", cu["updated"] == 1 and len(updated)==len(once), str(cu))
    # Conflict: same canonical application ID, different source record key.
    incoming = deepcopy(base["applications"][0])
    incoming["source_record_id"] = "different-source-record"
    _, c = idempotent_upsert(base["applications"], [incoming], "applications")
    record("conflicting canonical ID from different source key is flagged", c["conflicts"] == 1, str(c))
    validation = validate_bundle(base)
    record("base fixture passes semantic validation", validation["passed"] and not validation["errors"], f"errors={validation['errors']}; warnings={validation['warnings']}")
    return results, validation

if __name__ == "__main__":
    from pathlib import Path
    import json
    folder = Path(__file__).parent
    tests, validation = run_tests(folder)
    result = {"base_fixture":{"status":"PASS" if validation["passed"] else "FAIL","errors":validation["errors"],"warnings":validation["warnings"]},
              "tests":tests,"passed":sum(t["result"]=="PASS" for t in tests),"total":len(tests)}
    print(json.dumps(result, indent=2))
    if result["passed"] != result["total"] or result["base_fixture"]["status"] != "PASS":
        raise SystemExit(1)
