"""Configurable CSV -> canonical records -> validation -> SQLite idempotent upsert.

Standard-library prototype. Synthetic-data demo only; not production-certified.
Run: python csv_ingestion_pipeline.py
Or: python csv_ingestion_pipeline.py --config ingestion_config_demo.json --db demo_ingestion.sqlite
"""
from __future__ import annotations
import argparse, csv, hashlib, json, sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from semantic_validator import ENTITIES, parse_dt, validate_bundle

ENTITY_ID = {"requisitions":"requisition_id", "applications":"application_id", "stage_events":"stage_event_id", "costs":"cost_id"}
REQUIRED = {
    "requisitions": ["client_id", "requisition_id", "job_title", "status"],
    "applications": ["client_id", "application_id", "requisition_id", "source_channel", "applied_at", "application_outcome"],
    "stage_events": ["client_id", "stage_event_id", "application_id", "stage", "entered_at"],
    "costs": ["client_id", "cost_id", "source_channel", "amount", "currency"]
}
LINEAGE = ["source_system_id", "source_record_id", "mapping_version", "ingest_run_id"]


def clean_value(value: Any) -> Any:
    if value is None: return None
    if isinstance(value, str):
        value = value.strip()
        return value if value else None
    return value


def load_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        return [{k: clean_value(v) for k, v in row.items()} for row in csv.DictReader(f)]


def normalize_row(raw: dict, entity: str, config: dict, run_id: str) -> dict:
    mapping = config.get("field_mappings", {}).get(entity, {})
    # mapping format is {canonical_field: source_column}; empty mapping means identity mapping.
    out = {}
    for canonical_field, source_column in mapping.items():
        out[canonical_field] = clean_value(raw.get(source_column))
    if not mapping:
        out = {k: clean_value(v) for k, v in raw.items()}
    out["client_id"] = config["client_id"]
    out["source_system_id"] = config.get("source_system_id", "unknown_source")
    out["source_record_id"] = out.get("source_record_id") or clean_value(raw.get(config.get("source_record_id_column", "source_record_id"))) or clean_value(raw.get(ENTITY_ID[entity]))
    out["mapping_version"] = config.get("mapping_version", "1.0")
    out["ingest_run_id"] = run_id
    return out


def row_errors(row: dict, entity: str) -> list[str]:
    errs=[]
    for field in REQUIRED[entity]:
        if row.get(field) is None: errs.append(f"missing_required_field:{field}")
    for field in ("source_system_id", "source_record_id", "mapping_version", "ingest_run_id"):
        if not row.get(field): errs.append(f"missing_lineage_field:{field}")
    for field in ("applied_at", "joined_at", "source_evidence_at", "entered_at", "exited_at"):
        if row.get(field) is not None and field in row:
            try: parse_dt(row[field])
            except Exception: errs.append(f"invalid_timestamp:{field}")
    if entity == "applications":
        if row.get("application_outcome") == "joined" and not row.get("joined_at"):
            errs.append("joined_outcome_missing_join_evidence")
        if row.get("joined_at") and row.get("applied_at"):
            try:
                if parse_dt(row["joined_at"]) < parse_dt(row["applied_at"]): errs.append("joined_before_application")
            except Exception: pass
    if entity == "stage_events" and row.get("entered_at") and row.get("exited_at"):
        try:
            if parse_dt(row["exited_at"]) < parse_dt(row["entered_at"]): errs.append("stage_exit_before_entry")
        except Exception: pass
    if entity == "costs":
        try:
            if float(row.get("amount")) < 0: errs.append("negative_cost")
        except (TypeError, ValueError): errs.append("invalid_cost_amount")
        currency=row.get("currency")
        if not (isinstance(currency,str) and len(currency)==3 and currency.isalpha() and currency.isupper()): errs.append("invalid_currency")
    return errs


def fingerprint(row: dict) -> str:
    # Exclude run id so an exact replay on a later run remains unchanged.
    content={k:v for k,v in row.items() if k != "ingest_run_id"}
    return hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def init_db(conn: sqlite3.Connection):
    conn.execute("CREATE TABLE IF NOT EXISTS canonical_records (entity TEXT NOT NULL, client_id TEXT NOT NULL, entity_id TEXT NOT NULL, source_system_id TEXT NOT NULL, source_record_id TEXT NOT NULL, fingerprint TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(entity, client_id, entity_id), UNIQUE(entity, client_id, source_system_id, source_record_id))")
    conn.commit()


def upsert(conn: sqlite3.Connection, entity: str, row: dict) -> str:
    eid=row.get(ENTITY_ID[entity]); cid=row.get("client_id"); ss=row.get("source_system_id"); sr=row.get("source_record_id")
    if not all([eid,cid,ss,sr]): return "conflict"
    fp=fingerprint(row); payload=json.dumps(row, sort_keys=True)
    by_source=conn.execute("SELECT entity_id, fingerprint FROM canonical_records WHERE entity=? AND client_id=? AND source_system_id=? AND source_record_id=?", (entity,cid,ss,sr)).fetchone()
    if by_source:
        if by_source[0] != eid: return "conflict"
        if by_source[1] == fp: return "unchanged_replay"
        conn.execute("UPDATE canonical_records SET fingerprint=?, payload=? WHERE entity=? AND client_id=? AND entity_id=?", (fp,payload,entity,cid,eid)); return "updated"
    by_id=conn.execute("SELECT source_system_id, source_record_id FROM canonical_records WHERE entity=? AND client_id=? AND entity_id=?", (entity,cid,eid)).fetchone()
    if by_id: return "conflict"
    conn.execute("INSERT INTO canonical_records VALUES (?,?,?,?,?,?,?)", (entity,cid,eid,ss,sr,fp,payload)); return "inserted"


def existing_bundle(conn: sqlite3.Connection, client_id: str) -> dict:
    bundle={"client_id":client_id}
    for entity in ENTITIES:
        rows=conn.execute("SELECT payload FROM canonical_records WHERE entity=? AND client_id=? ORDER BY entity_id", (entity,client_id)).fetchall()
        bundle[entity]=[json.loads(r[0]) for r in rows]
    return bundle


def run_pipeline(config_path: Path, db_path: Path, base_dir: Path | None=None) -> dict:
    config=json.loads(config_path.read_text(encoding="utf-8")); base_dir=base_dir or config_path.parent
    run_id=f"{config.get('run_id_prefix','ingest')}-{datetime.now().strftime('%Y%m%dT%H%M%S%f')}"
    report={"run_id":run_id,"client_id":config.get("client_id"),"status":"running","entities":{},"quarantine":[],"warnings":[],"started_at":datetime.now().astimezone().isoformat()}
    conn=sqlite3.connect(db_path); init_db(conn)
    accepted_by_entity={}
    try:
        for entity, filename in config.get("files",{}).items():
            if entity not in ENTITY_ID: report["warnings"].append(f"unsupported_entity_skipped:{entity}"); continue
            path=base_dir/filename
            stats={"file":filename,"read":0,"valid":0,"inserted":0,"updated":0,"unchanged_replays":0,"conflicts":0,"quarantined":0,"warnings":0}
            if not path.exists():
                if entity in config.get("optional_entities",[]):
                    stats["status"]="optional_file_missing"; report["entities"][entity]=stats; accepted_by_entity[entity]=[]; continue
                report["entities"][entity]={**stats,"status":"failed_missing_file"}; report["status"]="failed"; continue
            try: raw_rows=load_csv(path)
            except Exception as exc:
                report["entities"][entity]={**stats,"status":"failed_read","error":str(exc)}; report["status"]="failed"; continue
            stats["read"]=len(raw_rows); good=[]
            for line_no, raw in enumerate(raw_rows, start=2):
                row=normalize_row(raw,entity,config,run_id)
                errs=row_errors(row,entity)
                if errs:
                    report["quarantine"].append({"entity":entity,"file":filename,"line":line_no,"source_record_id":row.get("source_record_id"),"errors":errs,"record":raw}); stats["quarantined"]+=1
                else:
                    if entity=="applications" and (not row.get("source_channel") or str(row.get("source_channel")).lower()=="unknown"):
                        report["warnings"].append({"entity":entity,"source_record_id":row.get("source_record_id"),"warning":"unknown_source_channel"}); stats["warnings"]+=1
                    good.append(row)
            accepted_by_entity[entity]=good; stats["valid"]=len(good)
            report["entities"][entity]=stats
        # Validate references against this batch plus already stored rows, before writing.
        staged={}
        for entity in ENTITIES:
            stored=existing_bundle(conn, config["client_id"])[entity]
            staged[entity]=stored + accepted_by_entity.get(entity,[])
        req_ids={r.get("requisition_id") for r in staged["requisitions"]}
        app_ids={r.get("application_id") for r in staged["applications"]}
        for entity, fk, valid_ids in [("applications","requisition_id",req_ids),("stage_events","application_id",app_ids),("costs","requisition_id",req_ids)]:
            kept=[]
            for row in accepted_by_entity.get(entity,[]):
                val=row.get(fk)
                if val and val not in valid_ids:
                    report["quarantine"].append({"entity":entity,"source_record_id":row.get("source_record_id"),"errors":[f"unresolved_reference:{fk}:{val}"],"record":row}); report["entities"][entity]["quarantined"]+=1; report["entities"][entity]["valid"]-=1
                else: kept.append(row)
            accepted_by_entity[entity]=kept
        conn.execute("BEGIN")
        for entity in ENTITIES:
            stats=report["entities"].get(entity)
            if not stats or "failed" in stats.get("status",""): continue
            for row in accepted_by_entity.get(entity,[]):
                outcome=upsert(conn,entity,row)
                if outcome=="conflict":
                    report["quarantine"].append({"entity":entity,"source_record_id":row.get("source_record_id"),"errors":["canonical_or_source_identity_conflict"],"record":row}); stats["quarantined"]+=1; stats["conflicts"]+=1
                else: stats[{"unchanged_replay":"unchanged_replays"}.get(outcome,outcome)]+=1
        conn.commit()
        final_bundle=existing_bundle(conn,config["client_id"]); validation=validate_bundle(final_bundle)
        report["semantic_validation"]={"passed":validation["passed"],"errors":validation["errors"],"warnings":validation["warnings"]}
        report["finished_at"]=datetime.now().astimezone().isoformat()
        report["status"]="completed_with_quarantine" if report["quarantine"] else ("completed_with_warnings" if report["warnings"] or validation["warnings"] else "completed")
        report["summary"]={"files_processed":len(report["entities"]),"rows_read":sum(x["read"] for x in report["entities"].values()),"inserted":sum(x["inserted"] for x in report["entities"].values()),"updated":sum(x["updated"] for x in report["entities"].values()),"unchanged_replays":sum(x["unchanged_replays"] for x in report["entities"].values()),"conflicts":sum(x["conflicts"] for x in report["entities"].values()),"quarantined":len(report["quarantine"])}
        return report
    except Exception:
        conn.rollback(); raise
    finally: conn.close()


def self_test(folder: Path) -> dict:
    import tempfile
    results=[]
    with tempfile.TemporaryDirectory() as td:
        db=Path(td)/"test.sqlite"
        cfg=folder/"ingestion_config_demo.json"
        first=run_pipeline(cfg,db,folder); second=run_pipeline(cfg,db,folder)
        results.append({"test":"first import inserts fixture rows","passed":first["summary"]["inserted"]==sum(len(load_csv(folder / f)) for f in ["requisitions.csv","applications.csv","stage_events.csv","costs_optional.csv"]),"actual":first["summary"]})
        results.append({"test":"second import is idempotent","passed":second["summary"]["inserted"]==0 and second["summary"]["unchanged_replays"]==sum(len(load_csv(folder / f)) for f in ["requisitions.csv","applications.csv","stage_events.csv","costs_optional.csv"]),"actual":second["summary"]})
        results.append({"test":"unknown source is warned, not quarantined","passed":any(w.get("warning")=="unknown_source_channel" for w in first["warnings"]) if first["warnings"] else False,"actual":first["warnings"]})
        results.append({"test":"base semantic validation has no errors","passed":first["semantic_validation"]["passed"],"actual":first["semantic_validation"]})
        # Exercise a client-specific source header mapping using an isolated temporary file.
        mapped_csv=Path(td)/"mapped_requisitions.csv"
        mapped_csv.write_text("Tenant,ReqNo,Title,ReqStatus,ExternalKey\nclient_x,R-1,QA Engineer,open,SRC-1\n",encoding="utf-8")
        mapped_cfg={"client_id":"client_x","source_system_id":"ats_x","mapping_version":"2.1","files":{"requisitions":"mapped_requisitions.csv"},"field_mappings":{"requisitions":{"client_id":"Tenant","requisition_id":"ReqNo","job_title":"Title","status":"ReqStatus","source_record_id":"ExternalKey"}},"optional_entities":["applications","stage_events","costs"]}
        mapped_cfg_path=Path(td)/"mapped_config.json"; mapped_cfg_path.write_text(json.dumps(mapped_cfg),encoding="utf-8")
        mapped=run_pipeline(mapped_cfg_path,Path(td)/"mapped.sqlite",Path(td))
        mapped_conn=sqlite3.connect(Path(td)/"mapped.sqlite")
        saved=json.loads(mapped_conn.execute("SELECT payload FROM canonical_records WHERE entity='requisitions'").fetchone()[0]); mapped_conn.close()
        results.append({"test":"client-specific source columns map to canonical fields","passed":saved.get("requisition_id")=="R-1" and saved.get("job_title")=="QA Engineer" and saved.get("source_record_id")=="SRC-1","actual":saved})
    return {"passed":sum(x["passed"] for x in results),"total":len(results),"tests":results}

if __name__=="__main__":
    parser=argparse.ArgumentParser(); parser.add_argument("--config",default="ingestion_config_demo.json"); parser.add_argument("--db",default="demo_ingestion.sqlite"); parser.add_argument("--self-test",action="store_true"); args=parser.parse_args()
    folder=Path(__file__).parent
    if args.self_test:
        result=self_test(folder); print(json.dumps(result,indent=2)); raise SystemExit(0 if result["passed"]==result["total"] else 1)
    result=run_pipeline(Path(args.config),Path(args.db),Path(args.config).resolve().parent)
    out=folder/"ingestion_run_output.json"; out.write_text(json.dumps(result,indent=2),encoding="utf-8")
    print(json.dumps(result,indent=2)); print(f"\nReport written to {out}")
