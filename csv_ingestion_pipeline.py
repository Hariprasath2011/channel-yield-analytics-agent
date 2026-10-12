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
    """Read a CSV after validating its header and row shape."""
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames
        if not headers:
            raise ValueError("csv_header_missing")
        cleaned_headers = [h.strip() if isinstance(h, str) else "" for h in headers]
        if any(not h for h in cleaned_headers):
            raise ValueError("csv_header_blank")
        if len(set(cleaned_headers)) != len(cleaned_headers):
            raise ValueError("csv_header_duplicate")
        reader.fieldnames = cleaned_headers
        rows = []
        for line_no, row in enumerate(reader, start=2):
            if None in row:
                raise ValueError(f"csv_row_has_extra_columns:line_{line_no}")
            rows.append({k: clean_value(v) for k, v in row.items()})
        return rows


def resolve_input_path(base_dir: Path, filename: str) -> Path:
    """Resolve an input path and prevent reads outside the configured input directory."""
    if not isinstance(filename, str) or not filename.strip():
        raise ValueError("input_filename_must_be_nonempty_string")
    root = base_dir.resolve()
    candidate = Path(filename)
    resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("input_path_outside_base_dir") from exc
    return resolved


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
        files = config.get("files", {})
        optional_entities = set(config.get("optional_entities", []))
        # Backward-compatible default: configured files are required unless listed optional.
        # Clients may now explicitly declare required_entities; any known entity not
        # listed as required or optional is not expected for this client.
        required_entities = set(config.get("required_entities", [e for e in files if e not in optional_entities]))
        unknown_policy_entities = (required_entities | optional_entities) - set(ENTITY_ID)
        if unknown_policy_entities:
            raise ValueError(f"Unknown entities in required_entities/optional_entities: {sorted(unknown_policy_entities)}")
        if required_entities & optional_entities:
            raise ValueError("An entity cannot be both required and optional")

        # Include explicitly optional/required entities even if no filename was supplied,
        # so the report makes absent inputs visible instead of silently ignoring them.
        expected_entities = list(dict.fromkeys([*files.keys(), *sorted(required_entities), *sorted(optional_entities)]))
        for entity in expected_entities:
            if entity not in ENTITY_ID:
                report["warnings"].append(f"unsupported_entity_skipped:{entity}")
                continue
            filename = files.get(entity)
            if not filename:
                stats={"file":None,"read":0,"valid":0,"inserted":0,"updated":0,"unchanged_replays":0,"conflicts":0,"quarantined":0,"warnings":0}
                if entity in optional_entities:
                    stats["status"]="optional_file_not_configured"
                    report["entities"][entity]=stats
                    report["warnings"].append({"entity":entity,"warning":"optional_file_not_configured"})
                    accepted_by_entity[entity]=[]
                else:
                    report["entities"][entity]={**stats,"status":"failed_file_not_configured"}
                    report["status"]="failed"
                continue
            stats={"file":filename,"read":0,"valid":0,"inserted":0,"updated":0,"unchanged_replays":0,"conflicts":0,"quarantined":0,"warnings":0}
            try:
                path=resolve_input_path(base_dir, filename)
            except (TypeError, ValueError, OSError) as exc:
                report["entities"][entity]={**stats,"status":"failed_path","error":str(exc)}
                report["status"]="failed"
                continue
            if not path.exists():
                if entity in optional_entities:
                    stats["status"]="optional_file_missing"
                    report["entities"][entity]=stats
                    report["warnings"].append({"entity":entity,"file":filename,"warning":"optional_file_missing"})
                    accepted_by_entity[entity]=[]
                    continue
                report["entities"][entity]={**stats,"status":"failed_missing_file"}
                report["status"]="failed"
                continue
            try: raw_rows=load_csv(path)
            except Exception as exc:
                report["entities"][entity]={**stats,"status":"failed_read","error":str(exc)}; report["status"]="failed"; continue
            stats["read"]=len(raw_rows)
            stats["status"]="loaded_empty" if not raw_rows else "loaded"
            good=[]
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
        # All-or-nothing policy: any required-file read/missing failure aborts the
        # entire run before canonical records are written.
        if report["status"] == "failed":
            report["finished_at"] = datetime.now().astimezone().isoformat()
            report["summary"] = {
                "files_processed": len(report["entities"]),
                "rows_read": sum(x["read"] for x in report["entities"].values()),
                "inserted": 0, "updated": 0, "unchanged_replays": 0,
                "conflicts": 0, "quarantined": len(report["quarantine"]),
                "committed": False,
            }
            return report

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
        # Validate inside the transaction. If canonical semantic validation fails,
        # roll back every write from this run rather than publishing invalid state.
        final_bundle=existing_bundle(conn,config["client_id"]); validation=validate_bundle(final_bundle)
        report["semantic_validation"]={"passed":validation["passed"],"errors":validation["errors"],"warnings":validation["warnings"]}
        if not validation["passed"]:
            conn.rollback()
            report["status"] = "failed_validation"
            report["finished_at"] = datetime.now().astimezone().isoformat()
            report["summary"] = {
                "files_processed": len(report["entities"]),
                "rows_read": sum(x["read"] for x in report["entities"].values()),
                "inserted": 0, "updated": 0, "unchanged_replays": 0,
                "conflicts": sum(x["conflicts"] for x in report["entities"].values()),
                "quarantined": len(report["quarantine"]), "committed": False,
            }
            return report
        conn.commit()
        report["finished_at"]=datetime.now().astimezone().isoformat()
        report["status"]="completed_with_quarantine" if report["quarantine"] else ("completed_with_warnings" if report["warnings"] or validation["warnings"] else "completed")
        report["summary"]={"files_processed":len(report["entities"]),"rows_read":sum(x["read"] for x in report["entities"].values()),"inserted":sum(x["inserted"] for x in report["entities"].values()),"updated":sum(x["updated"] for x in report["entities"].values()),"unchanged_replays":sum(x["unchanged_replays"] for x in report["entities"].values()),"conflicts":sum(x["conflicts"] for x in report["entities"].values()),"quarantined":len(report["quarantine"]),"committed":True}
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

        # Optional historical data may be absent; the rest of the run should commit.
        optional_cfg={"client_id":"optional_client","source_system_id":"ats_x","mapping_version":"1.0",
          "files":{"requisitions":"requisitions.csv","applications":"applications.csv","stage_events":"missing_history.csv"},
          "optional_entities":["stage_events"]}
        optional_cfg_path=Path(td)/"optional_config.json"; optional_cfg_path.write_text(json.dumps(optional_cfg),encoding="utf-8")
        optional_run=run_pipeline(optional_cfg_path,Path(td)/"optional.sqlite",folder)
        optional_status=optional_run["entities"].get("stage_events",{}).get("status")
        results.append({"test":"missing optional historical file does not block import",
          "passed":optional_run["summary"]["committed"] is True and optional_status=="optional_file_missing",
          "actual":{"status":optional_run["status"],"stage_events_status":optional_status,"summary":optional_run["summary"]}})

        # Missing required data must fail before any canonical rows are written.
        required_cfg={"client_id":"required_client","source_system_id":"ats_x","mapping_version":"1.0",
          "files":{"requisitions":"requisitions.csv","applications":"missing_applications.csv"},
          "required_entities":["requisitions","applications"],"optional_entities":["stage_events","costs"]}
        required_cfg_path=Path(td)/"required_config.json"; required_cfg_path.write_text(json.dumps(required_cfg),encoding="utf-8")
        required_db=Path(td)/"required.sqlite"
        required_run=run_pipeline(required_cfg_path,required_db,folder)
        required_conn=sqlite3.connect(required_db)
        required_rows=required_conn.execute("SELECT COUNT(*) FROM canonical_records WHERE client_id='required_client'").fetchone()[0]
        required_conn.close()
        results.append({"test":"missing required file aborts without canonical writes",
          "passed":required_run["status"]=="failed" and required_run["summary"]["committed"] is False and required_rows==0,
          "actual":{"status":required_run["status"],"summary":required_run["summary"],"canonical_rows":required_rows}})

        # A required path that exists but cannot be opened must fail before writes.
        unreadable_dir=Path(td)/"unreadable_requisitions.csv"
        unreadable_dir.mkdir()  # Opening a directory as a CSV reliably raises an I/O error, including on elevated Windows accounts.
        unreadable_cfg={"client_id":"unreadable_client","source_system_id":"ats_x","mapping_version":"1.0",
          "files":{"requisitions":"unreadable_requisitions.csv","applications":"applications.csv"},
          "required_entities":["requisitions","applications"],"optional_entities":["stage_events","costs"]}
        unreadable_cfg_path=Path(td)/"unreadable_config.json"; unreadable_cfg_path.write_text(json.dumps(unreadable_cfg),encoding="utf-8")
        unreadable_db=Path(td)/"unreadable.sqlite"
        unreadable_run=run_pipeline(unreadable_cfg_path,unreadable_db,Path(td))
        unreadable_conn=sqlite3.connect(unreadable_db)
        unreadable_rows=unreadable_conn.execute("SELECT COUNT(*) FROM canonical_records WHERE client_id='unreadable_client'").fetchone()[0]
        unreadable_conn.close()
        unreadable_status=unreadable_run["entities"].get("requisitions",{}).get("status")
        results.append({"test":"unreadable required file aborts without canonical writes",
          "passed":unreadable_run["status"]=="failed" and unreadable_status=="failed_read" and unreadable_run["summary"]["committed"] is False and unreadable_rows==0,
          "actual":{"status":unreadable_run["status"],"entity_status":unreadable_status,"summary":unreadable_run["summary"],"canonical_rows":unreadable_rows}})

        # A header-only optional CSV is present-but-empty, not missing, and should not block a commit.
        empty_history=Path(td)/"empty_history.csv"
        empty_history.write_text("stage_event_id,application_id,stage,entered_at\\n",encoding="utf-8")
        for fixture_name in ("requisitions.csv", "applications.csv"):
            (Path(td)/fixture_name).write_bytes((folder/fixture_name).read_bytes())
        empty_cfg={"client_id":"empty_history_client","source_system_id":"ats_x","mapping_version":"1.0",
          "files":{"requisitions":"requisitions.csv","applications":"applications.csv","stage_events":"empty_history.csv"},
          "required_entities":["requisitions","applications"],"optional_entities":["stage_events","costs"]}
        empty_cfg_path=Path(td)/"empty_config.json"; empty_cfg_path.write_text(json.dumps(empty_cfg),encoding="utf-8")
        empty_db=Path(td)/"empty.sqlite"
        empty_run=run_pipeline(empty_cfg_path,empty_db,Path(td))
        empty_status=empty_run["entities"].get("stage_events",{}).get("status")
        results.append({"test":"empty optional historical CSV is distinct from missing and commits",
          "passed":empty_run["summary"]["committed"] is True and empty_status=="loaded_empty" and empty_run["entities"]["stage_events"]["read"]==0,
          "actual":{"status":empty_run["status"],"stage_events_status":empty_status,"rows_read":empty_run["entities"]["stage_events"]["read"],"summary":empty_run["summary"]}})

        # Seed a database, then force semantic validation to fail after an update/insert is staged.
        # This verifies transaction rollback preserves previously committed canonical records.
        rollback_db=Path(td)/"rollback.sqlite"
        seed_run=run_pipeline(cfg,rollback_db,folder)
        def canonical_snapshot(db_path, client_id):
            c=sqlite3.connect(db_path)
            try:
                return c.execute("SELECT entity, entity_id, fingerprint, payload FROM canonical_records WHERE client_id=? ORDER BY entity, entity_id",(client_id,)).fetchall()
            finally:
                c.close()
        before_snapshot=canonical_snapshot(rollback_db, "demo_client")
        changed_req=Path(td)/"rollback_requisitions.csv"
        source_req=load_csv(folder/"requisitions.csv")
        if source_req:
            source_req[0]["job_title"]=(source_req[0].get("job_title") or "Role")+" - changed"
        with changed_req.open("w",newline="",encoding="utf-8") as f:
            writer=csv.DictWriter(f,fieldnames=list(source_req[0].keys()))
            writer.writeheader(); writer.writerows(source_req)
        for fixture_name in ("applications.csv", "stage_events.csv", "costs_optional.csv"):
            (Path(td)/fixture_name).write_bytes((folder/fixture_name).read_bytes())
        rollback_cfg={"client_id":"demo_client","source_system_id":"demo_csv","mapping_version":"1.0",
          "files":{"requisitions":"rollback_requisitions.csv","applications":"applications.csv","stage_events":"stage_events.csv","costs":"costs_optional.csv"}}
        rollback_cfg_path=Path(td)/"rollback_config.json"; rollback_cfg_path.write_text(json.dumps(rollback_cfg),encoding="utf-8")
        original_validator=validate_bundle
        try:
            globals()["validate_bundle"] = lambda bundle: {"passed":False,"errors":["forced_test_validation_failure"],"warnings":[]}
            rollback_run=run_pipeline(rollback_cfg_path,rollback_db,Path(td))
        finally:
            globals()["validate_bundle"] = original_validator
        after_snapshot=canonical_snapshot(rollback_db, "demo_client")
        results.append({"test":"semantic-validation failure rolls back and preserves existing records",
          "passed":seed_run["summary"]["committed"] is True and rollback_run["status"]=="failed_validation" and rollback_run["summary"]["committed"] is False and before_snapshot==after_snapshot,
          "actual":{"seed_committed":seed_run["summary"]["committed"],"status":rollback_run["status"],"summary":rollback_run["summary"],"records_preserved":before_snapshot==after_snapshot,"validation":rollback_run.get("semantic_validation")}})
        # Input path traversal must fail before any canonical writes.
        traversal_cfg={"client_id":"traversal_client","source_system_id":"demo_csv","mapping_version":"1.0",
          "files":{"requisitions":"../requisitions.csv","applications":"applications.csv"},
          "required_entities":["requisitions","applications"]}
        (Path(td)/"applications.csv").write_bytes((folder/"applications.csv").read_bytes())
        traversal_cfg_path=Path(td)/"traversal_config.json"; traversal_cfg_path.write_text(json.dumps(traversal_cfg),encoding="utf-8")
        traversal_db=Path(td)/"traversal.sqlite"
        traversal_run=run_pipeline(traversal_cfg_path,traversal_db,Path(td))
        traversal_conn=sqlite3.connect(traversal_db)
        traversal_rows=traversal_conn.execute("SELECT COUNT(*) FROM canonical_records WHERE client_id='traversal_client'").fetchone()[0]
        traversal_conn.close()
        results.append({"test":"path traversal is rejected before canonical writes",
          "passed":traversal_run["status"]=="failed" and traversal_run["entities"]["requisitions"]["status"]=="failed_path" and traversal_rows==0,
          "actual":{"status":traversal_run["status"],"entity_status":traversal_run["entities"]["requisitions"]["status"],"canonical_rows":traversal_rows}})

        # Duplicate CSV headers must fail as an input-read error.
        bad_header=Path(td)/"duplicate_header.csv"
        bad_header.write_text("requisition_id,requisition_id,job_title,status\\nR-1,R-2,Engineer,open\\n",encoding="utf-8")
        bad_header_cfg={"client_id":"header_client","source_system_id":"demo_csv","mapping_version":"1.0",
          "files":{"requisitions":"duplicate_header.csv"},"required_entities":["requisitions"]}
        bad_header_cfg_path=Path(td)/"bad_header_config.json"; bad_header_cfg_path.write_text(json.dumps(bad_header_cfg),encoding="utf-8")
        bad_header_run=run_pipeline(bad_header_cfg_path,Path(td)/"bad_header.sqlite",Path(td))
        results.append({"test":"duplicate CSV headers are rejected",
          "passed":bad_header_run["status"]=="failed" and bad_header_run["entities"]["requisitions"]["status"]=="failed_read" and "csv_header_duplicate" in bad_header_run["entities"]["requisitions"].get("error",""),
          "actual":{"status":bad_header_run["status"],"entity":bad_header_run["entities"]["requisitions"]}})

    return {"passed":sum(x["passed"] for x in results),"total":len(results),"tests":results}

if __name__=="__main__":
    parser=argparse.ArgumentParser(); parser.add_argument("--config",default="ingestion_config_demo.json"); parser.add_argument("--db",default="demo_ingestion.sqlite"); parser.add_argument("--self-test",action="store_true"); args=parser.parse_args()
    folder=Path(__file__).parent
    if args.self_test:
        result=self_test(folder); print(json.dumps(result,indent=2)); raise SystemExit(0 if result["passed"]==result["total"] else 1)
    result=run_pipeline(Path(args.config),Path(args.db),Path(args.config).resolve().parent)
    out=folder/"ingestion_run_output.json"; out.write_text(json.dumps(result,indent=2),encoding="utf-8")
    print(json.dumps(result,indent=2)); print(f"\nReport written to {out}")
