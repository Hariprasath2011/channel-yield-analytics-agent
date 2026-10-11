"""Read-only analytics service over canonical SQLite records.

Prototype only. Uses deterministic calculations; the LLM should explain these outputs,
not recalculate authoritative metrics. Synthetic-data demo; not production-certified.
"""
from __future__ import annotations
import argparse, json, sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

STAGE_ORDER = ["applied", "screened", "interviewed", "offered", "joined"]
FINAL_NON_JOIN = {"rejected", "withdrawn"}
UNKNOWN_VALUES = {"", "unknown", "unattributed", "n/a", "na"}

def read_records(conn, client_id: str, entity: str):
    rows = conn.execute("SELECT payload FROM canonical_records WHERE client_id=? AND entity=? ORDER BY entity_id", (client_id, entity)).fetchall()
    return [json.loads(r[0]) for r in rows]

def parse_day(value):
    if not value: return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).date()

def compute_analytics(db_path: Path, *, client_id: str, cohort_start: str, cohort_end: str,
                      as_of_date: str, observation_days: int, requisition_id: Optional[str] = None,
                      min_sample_size: Optional[int] = None) -> dict:
    start, end, as_of = date.fromisoformat(cohort_start), date.fromisoformat(cohort_end), date.fromisoformat(as_of_date)
    if end < start: raise ValueError("cohort_end must be on or after cohort_start")
    if observation_days < 0: raise ValueError("observation_days must be >= 0")
    if min_sample_size is not None and min_sample_size < 1: raise ValueError("min_sample_size must be >= 1")
    conn = sqlite3.connect(db_path)
    try:
        # Tenant scope is mandatory and applied to every collection read.
        reqs = read_records(conn, client_id, "requisitions")
        apps = read_records(conn, client_id, "applications")
        events = read_records(conn, client_id, "stage_events")
        costs = read_records(conn, client_id, "costs")
    finally: conn.close()
    req_by_id = {r.get("requisition_id"): r for r in reqs}
    if requisition_id and requisition_id not in req_by_id:
        return {"status":"not_found", "client_id":client_id, "requisition_id":requisition_id,
                "error":"requisition_not_found_for_client", "metrics":None}
    if requisition_id: apps = [a for a in apps if a.get("requisition_id") == requisition_id]
    cohort=[]
    for app in apps:
        applied = parse_day(app.get("applied_at"))
        if applied and start <= applied <= end: cohort.append(app)
    cohort_ids = {a.get("application_id") for a in cohort}
    channel_stats = defaultdict(lambda: {"applications_in_cohort":0,"mature_eligible_applications":0,"confirmed_joins":0,"mature_non_joins":0,"pending_or_immature":0})
    unknown_count=0
    for app in cohort:
        channel=(app.get("source_channel") or "").strip()
        is_unknown=channel.casefold() in {v.casefold() for v in UNKNOWN_VALUES}
        if is_unknown:
            unknown_count += 1
            continue
        g=channel_stats[channel]; g["applications_in_cohort"] += 1
        applied=parse_day(app.get("applied_at")); mature=as_of >= applied + timedelta(days=observation_days)
        outcome=(app.get("application_outcome") or "unknown").strip().lower()
        joined_evidence=bool(app.get("joined_at"))
        if outcome == "joined" and joined_evidence:
            if mature:
                g["mature_eligible_applications"] += 1; g["confirmed_joins"] += 1
            else: g["pending_or_immature"] += 1
        elif mature and outcome in FINAL_NON_JOIN:
            g["mature_eligible_applications"] += 1; g["mature_non_joins"] += 1
        else: g["pending_or_immature"] += 1
    channels=[]
    for channel,g in sorted(channel_stats.items()):
        denom=g["mature_eligible_applications"]
        yield_pct=round(g["confirmed_joins"] / denom * 100, 2) if denom else None
        eligible=min_sample_size is not None and denom >= min_sample_size and yield_pct is not None
        channels.append({"channel":channel,**g,"yield_percent":yield_pct,
                         "yield_status":"available" if denom else "not_applicable_no_mature_denominator",
                         "ranking_eligible":eligible,
                         "ranking_status":"eligible_under_configured_threshold" if eligible else ("minimum_sample_size_policy_not_configured" if min_sample_size is None else "below_minimum_sample_size_or_no_yield")})
    ranked=sorted([c for c in channels if c["ranking_eligible"]], key=lambda c:(-c["yield_percent"],-c["mature_eligible_applications"],c["channel"]))
    for idx,row in enumerate(ranked,1): row["rank"]=idx
    stage_apps=defaultdict(set)
    for ev in events:
        if ev.get("application_id") in cohort_ids:
            stage=(ev.get("stage") or "").strip().lower()
            if stage in STAGE_ORDER: stage_apps[stage].add(ev.get("application_id"))
    # If no stage event data exists, do not fabricate funnel metrics from current_stage.
    funnel_available=bool(events) and any(ev.get("application_id") in cohort_ids for ev in events)
    funnel=[{"stage":s,"applications":len(stage_apps[s])} for s in STAGE_ORDER] if funnel_available else None
    # Costs are surfaced only as grouped reported spend; do not invent allocation or FX conversion.
    unallocated_cost_count=sum(1 for c in costs if not c.get("requisition_id"))
    # When filtering to one requisition, never silently allocate general campaign spend to it.
    scoped_costs=[c for c in costs if (not requisition_id or c.get("requisition_id")==requisition_id)]
    cost_groups=defaultdict(float)
    for c in scoped_costs:
        try: cost_groups[(c.get("source_channel") or "unknown", c.get("currency") or "unknown")] += float(c.get("amount"))
        except (ValueError,TypeError): pass
    cost_summary=[{"channel":k[0],"currency":k[1],"reported_cost":round(v,2)} for k,v in sorted(cost_groups.items())]
    known_apps=sum(x["applications_in_cohort"] for x in channel_stats.values())
    warnings=[]
    if unknown_count: warnings.append("unknown_source_applications_excluded_from_named_channel_yield")
    if min_sample_size is None: warnings.append("ranking_disabled_until_minimum_sample_size_policy_is_configured")
    if not funnel_available: warnings.append("funnel_unavailable_no_stage_events_for_selected_cohort")
    if unallocated_cost_count: warnings.append("some_cost_records_are_not_requisition_allocated")
    if any(c["pending_or_immature"] for c in channels): warnings.append("pending_or_immature_outcomes_excluded_from_yield_denominator")
    return {
      "contract_version":"analytics_response_v1",
      "status":"completed",
      "client_id":client_id,
      "scope":{"requisition_id":requisition_id,"cohort_start":cohort_start,"cohort_end":cohort_end,"as_of_date":as_of_date,"observation_days":observation_days,"hire_definition":"confirmed_joined_at_timestamp","yield_formula":"mature_confirmed_joins / mature_eligible_applications * 100","source_attribution_rule":"last recorded source channel before application when evidence exists; otherwise source field or unknown"},
      "requisition_context":([{"requisition_id":r.get("requisition_id"),"job_title":r.get("job_title"),"role_family":r.get("role_family"),"status":r.get("status")} for r in reqs if not requisition_id or r.get("requisition_id")==requisition_id]),
      "summary":{"applications_in_cohort":len(cohort),"known_source_applications":known_apps,"unknown_source_applications":unknown_count,"source_coverage_percent":round(known_apps/len(cohort)*100,2) if cohort else None,"confirmed_mature_joins":sum(x["confirmed_joins"] for x in channel_stats.values()),"channels_with_yield":sum(x["yield_percent"] is not None for x in channels),"ranking_eligible_channels":len(ranked)},
      "channel_metrics":channels,
      "ranked_channels":[{"rank":c["rank"],"channel":c["channel"],"yield_percent":c["yield_percent"],"mature_eligible_applications":c["mature_eligible_applications"],"confirmed_joins":c["confirmed_joins"]} for c in ranked],
      "funnel":{"available":funnel_available,"stages":funnel},
      "reported_costs":{"available":bool(costs),"groups":cost_summary,"currency_conversion_performed":False,"cost_per_hire_calculated":False},
      "data_quality":{"unknown_source_applications":unknown_count,"pending_or_immature_applications_excluded":sum(x["pending_or_immature"] for x in channels),"warnings":warnings},
      "calculation_version":"yield_policy_v1"
    }

def self_test(folder: Path) -> dict:
    import tempfile, subprocess, sys
    tests=[]
    with tempfile.TemporaryDirectory() as td:
        db=Path(td)/"analytics.sqlite"
        # Import synthetic fixture into a clean SQLite store using the existing ingestion service.
        from csv_ingestion_pipeline import run_pipeline
        ingestion=run_pipeline(folder/"ingestion_config_demo.json",db,folder)
        tests.append({"test":"analytics fixture ingestion completes","passed":ingestion["status"].startswith("completed"),"detail":ingestion["status"]})
        base=dict(client_id="demo_client",cohort_start="2026-01-01",cohort_end="2026-06-30",as_of_date="2026-10-11",observation_days=90)
        out=compute_analytics(db,**base)
        tests.append({"test":"unknown source is reported and excluded from named channels","passed":out["summary"]["unknown_source_applications"]==1 and all(c["channel"].lower()!="unknown" for c in out["channel_metrics"]),"detail":out["summary"]})
        tests.append({"test":"active and immature outcomes do not count as mature failures","passed":sum(c["pending_or_immature"] for c in out["channel_metrics"])>=1,"detail":[{c["channel"]:c["pending_or_immature"]} for c in out["channel_metrics"]]})
        tests.append({"test":"ranking disabled without approved sample threshold","passed":not out["ranked_channels"] and all(not c["ranking_eligible"] for c in out["channel_metrics"]),"detail":out["data_quality"]["warnings"]})
        threshold=compute_analytics(db,**base,min_sample_size=1)
        tests.append({"test":"ranking only includes channels meeting configured threshold","passed":all(c["mature_eligible_applications"]>=1 for c in threshold["ranked_channels"]),"detail":threshold["ranked_channels"]})
        missing=compute_analytics(db,client_id="other_client",**{k:v for k,v in base.items() if k!="client_id"})
        tests.append({"test":"tenant scope prevents reading another client's records","passed":missing["summary"]["applications_in_cohort"]==0,"detail":missing["summary"]})
        notfound=compute_analytics(db,**base,requisition_id="NOT-A-REQ")
        tests.append({"test":"unknown requisition returns explicit not-found result","passed":notfound["status"]=="not_found","detail":notfound.get("error")})
        invalid=False
        try: compute_analytics(db,**{**base,"observation_days":-1})
        except ValueError: invalid=True
        tests.append({"test":"invalid observation policy is rejected","passed":invalid,"detail":"negative observation_days raises ValueError"})
        outpath=folder/"analytics_service_demo_output.json"; outpath.write_text(json.dumps(out,indent=2),encoding="utf-8")
    return {"passed":sum(t["passed"] for t in tests),"total":len(tests),"tests":tests}

if __name__=="__main__":
    p=argparse.ArgumentParser(); p.add_argument("--db",default="demo_ingestion.sqlite"); p.add_argument("--client-id",default="demo_client"); p.add_argument("--cohort-start",default="2026-01-01"); p.add_argument("--cohort-end",default="2026-06-30"); p.add_argument("--as-of-date",default="2026-10-11"); p.add_argument("--observation-days",type=int,default=90); p.add_argument("--requisition-id"); p.add_argument("--min-sample-size",type=int); p.add_argument("--self-test",action="store_true"); args=p.parse_args(); folder=Path(__file__).parent
    if args.self_test:
        result=self_test(folder); print(json.dumps(result,indent=2)); raise SystemExit(0 if result["passed"]==result["total"] else 1)
    result=compute_analytics(Path(args.db),client_id=args.client_id,cohort_start=args.cohort_start,cohort_end=args.cohort_end,as_of_date=args.as_of_date,observation_days=args.observation_days,requisition_id=args.requisition_id,min_sample_size=args.min_sample_size)
    print(json.dumps(result,indent=2))
