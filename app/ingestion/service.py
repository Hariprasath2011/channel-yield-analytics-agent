"""Tenant-scoped canonical data ingestion service.

Enforces:
  - Strict tenant isolation: client_id is hard-bound to the authenticated tenant_id.
  - Idempotent upserts with SHA-256 fingerprint replay detection.
  - Transactional boundary with rollback on semantic validation failure,
    preserving the last successful dataset intact.
  - Durable job tracking via ImportJob and audit logging via AuditEvent.
  - Safe error reporting and quarantine tracking without logging secrets or credentials.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.persistence.models import AuditEvent, CanonicalRecord, ImportJob, Tenant
from semantic_validator import ENTITIES, parse_dt, validate_bundle

logger = get_logger(__name__)

ENTITY_ID = {
    "requisitions": "requisition_id",
    "applications": "application_id",
    "stage_events": "stage_event_id",
    "costs": "cost_id",
}

REQUIRED = {
    "requisitions": ["client_id", "requisition_id", "job_title", "status"],
    "applications": [
        "client_id",
        "application_id",
        "requisition_id",
        "source_channel",
        "applied_at",
        "application_outcome",
    ],
    "stage_events": ["client_id", "stage_event_id", "application_id", "stage", "entered_at"],
    "costs": ["client_id", "cost_id", "source_channel", "amount", "currency"],
}


def _clean_val(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, str):
        v = v.strip()
        return v if v else None
    return v


def parse_csv_content(content: str | bytes | Path) -> list[dict[str, Any]]:
    """Parse raw CSV input after validating header shape (no duplicates, no blank headers)."""
    if isinstance(content, Path):
        text = content.read_text(encoding="utf-8-sig")
    elif isinstance(content, bytes):
        text = content.decode("utf-8-sig")
    elif isinstance(content, str):
        text = content
    else:
        raise ValueError("Invalid CSV input type")

    stream = io.StringIO(text)
    reader = csv.DictReader(stream)
    headers = reader.fieldnames
    if not headers:
        raise ValueError("csv_header_missing")

    cleaned_headers = [h.strip() if isinstance(h, str) else "" for h in headers]
    if any(not h for h in cleaned_headers):
        raise ValueError("csv_header_blank")
    if len(set(cleaned_headers)) != len(cleaned_headers):
        raise ValueError("csv_header_duplicate")

    reader.fieldnames = cleaned_headers
    rows: list[dict[str, Any]] = []
    for line_no, row in enumerate(reader, start=2):
        if None in row:
            raise ValueError(f"csv_row_has_extra_columns:line_{line_no}")
        rows.append({k: _clean_val(v) for k, v in row.items()})
    return rows


def normalize_row(
    raw: dict[str, Any],
    entity: str,
    tenant_id: str,
    run_id: str,
    source_system_id: str = "csv_upload",
    mapping_version: str = "1.0",
    field_mappings: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """Normalize raw row into canonical record format with tenant-bound client_id."""
    mapping = field_mappings or {}
    out: dict[str, Any] = {}
    if mapping:
        for canonical_field, source_col in mapping.items():
            out[canonical_field] = _clean_val(raw.get(source_col))
    else:
        out = {k: _clean_val(v) for k, v in raw.items()}

    out["client_id"] = tenant_id  # Strictly bound to tenant
    out["source_system_id"] = source_system_id
    out["source_record_id"] = (
        out.get("source_record_id")
        or _clean_val(raw.get("source_record_id"))
        or _clean_val(raw.get(ENTITY_ID[entity]))
    )
    out["mapping_version"] = mapping_version
    out["ingest_run_id"] = run_id
    return out


def evaluate_row_errors(row: dict[str, Any], entity: str) -> list[str]:
    """Validate required fields, timestamp formats, and business invariants."""
    errs: list[str] = []
    for field in REQUIRED[entity]:
        if row.get(field) is None:
            errs.append(f"missing_required_field:{field}")

    for field in ("source_system_id", "source_record_id", "mapping_version", "ingest_run_id"):
        if not row.get(field):
            errs.append(f"missing_lineage_field:{field}")

    for field in ("applied_at", "joined_at", "source_evidence_at", "entered_at", "exited_at"):
        if row.get(field) is not None and field in row:
            try:
                parse_dt(row[field])
            except Exception:
                errs.append(f"invalid_timestamp:{field}")

    if entity == "applications":
        if row.get("application_outcome") == "joined" and not row.get("joined_at"):
            errs.append("joined_outcome_missing_join_evidence")
        if row.get("joined_at") and row.get("applied_at"):
            try:
                if parse_dt(row["joined_at"]) < parse_dt(row["applied_at"]):
                    errs.append("joined_before_application")
            except Exception:
                pass

    if entity == "stage_events" and row.get("entered_at") and row.get("exited_at"):
        try:
            if parse_dt(row["exited_at"]) < parse_dt(row["entered_at"]):
                errs.append("stage_exit_before_entry")
        except Exception:
            pass

    if entity == "costs":
        try:
            if float(row.get("amount", 0)) < 0:
                errs.append("negative_cost")
        except (TypeError, ValueError):
            errs.append("invalid_cost_amount")
        currency = row.get("currency")
        if not (isinstance(currency, str) and len(currency) == 3 and currency.isalpha() and currency.isupper()):
            errs.append("invalid_currency")

    return errs


def compute_fingerprint(row: dict[str, Any]) -> str:
    """Compute deterministic SHA-256 digest of record contents excluding ingest_run_id."""
    content = {k: v for k, v in row.items() if k != "ingest_run_id"}
    return hashlib.sha256(
        json.dumps(content, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class IngestionService:
    """Orchestrates tenant-scoped CSV imports, idempotent upserts, and rollback validation."""

    @staticmethod
    def get_job(db: Session, tenant_id: str, job_id: str) -> ImportJob:
        """Fetch import job scoped to tenant."""
        job = (
            db.query(ImportJob)
            .filter(ImportJob.tenant_id == tenant_id, ImportJob.id == job_id)
            .first()
        )
        if not job:
            raise NotFoundError("Import job")
        return job

    @staticmethod
    def list_jobs(db: Session, tenant_id: str, limit: int = 50) -> list[ImportJob]:
        """List recent import jobs for tenant."""
        return (
            db.query(ImportJob)
            .filter(ImportJob.tenant_id == tenant_id)
            .order_by(ImportJob.created_at.desc())
            .limit(limit)
            .all()
        )

    @classmethod
    def ingest_bundle(
        cls,
        db: Session,
        *,
        tenant_id: str,
        files: dict[str, str | bytes | Path],
        user_id: Optional[str] = None,
        source_system_id: str = "csv_upload",
        mapping_version: str = "1.0",
        required_entities: Optional[list[str]] = None,
        optional_entities: Optional[list[str]] = None,
        field_mappings: Optional[dict[str, dict[str, str]]] = None,
    ) -> dict[str, Any]:
        """Ingest a CSV bundle for a tenant with transactional safety and rollback on failure.

        Lifecycle:
          1. Concurrency Check: Rejects if an import is already running for this tenant.
          2. Durable Job Created: Recorded in `import_jobs` as 'running'.
          3. Row Normalization & Quarantine: Field extraction, schema validation, quarantine tracking.
          4. Cross-Reference Validation: Staged relational consistency.
          5. Idempotent Upsert: Replays detected via fingerprint; conflict detection on source/canonical IDs.
          6. Transactional Semantic Validation: `validate_bundle()` executed against the entire candidate dataset.
             - If validation fails: COMPLETE ROLLBACK of canonical mutations; job marked `failed_validation`.
             - If validation passes: COMMIT transaction; job marked `completed` / `completed_with_warnings`.
          7. Audit Event logged.
        """
        # 1. Concurrency Guard & Stale Job Recovery
        # Acquire advisory / row lock on the tenant row to serialize concurrent imports across workers.
        # This lock MUST be held continuously across stale-job recovery, active-job inspection,
        # and creation of the replacement running job. Never commit or roll back before creating
        # the replacement job, as that would release the row lock and create a race window.
        tenant = (
            db.query(Tenant)
            .filter(Tenant.id == tenant_id)
            .with_for_update()
            .first()
        )
        if not tenant:
            raise NotFoundError("Tenant")

        active_job = (
            db.query(ImportJob)
            .filter(ImportJob.tenant_id == tenant_id, ImportJob.status == "running")
            .first()
        )
        now_utc = datetime.now(timezone.utc)

        if active_job:
            started = active_job.started_at or active_job.created_at
            if started and started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            elapsed_seconds = (now_utc - started).total_seconds() if started else 999999
            if elapsed_seconds > 1800:  # 30-minute threshold for crash/interrupted recovery
                logger.warning(
                    "ingestion.stale_job_recovered",
                    tenant_id=tenant_id,
                    job_id=active_job.id,
                    elapsed_seconds=elapsed_seconds,
                )
                active_job.status = "failed"
                active_job.finished_at = now_utc
                active_job.error_summary = json.dumps({
                    "error": "stale_job_timeout",
                    "message": f"Job timed out after exceeding 30-minute execution window ({int(elapsed_seconds)}s).",
                })
                audit = AuditEvent(
                    tenant_id=tenant_id,
                    actor_id=user_id or "system",
                    actor_type="system",
                    event_type="ingestion.stale_job_recovered",
                    resource_type="import_job",
                    resource_id=active_job.id,
                    outcome="failure",
                )
                db.add(audit)
                # Note: Do NOT commit here! Committing would release the tenant row lock,
                # allowing a concurrent worker to slip past the active_job guard before the
                # replacement running job is created. Both the stale job recovery and
                # the new running job creation are committed together below.
            else:
                logger.warning(
                    "ingestion.concurrency_conflict",
                    tenant_id=tenant_id,
                    running_job_id=active_job.id,
                )
                raise ConflictError("An import job is already in progress for this tenant.")

        run_id = f"ingest-{now_utc.strftime('%Y%m%dT%H%M%S%f')}"

        # 2. Durable Job Entry
        job = ImportJob(
            tenant_id=tenant_id,
            initiated_by_user_id=user_id,
            status="running",
            source_system_id=source_system_id,
            mapping_version=mapping_version,
            started_at=now_utc,
        )
        db.add(job)
        db.commit()
        db.refresh(job)

        optional_set = set(optional_entities or ["stage_events", "costs"])
        required_set = set(required_entities or ["requisitions", "applications"])
        mappings = field_mappings or {}

        report: dict[str, Any] = {
            "job_id": job.id,
            "run_id": run_id,
            "tenant_id": tenant_id,
            "status": "running",
            "entities": {},
            "quarantine": [],
            "warnings": [],
            "started_at": now_utc.isoformat(),
        }

        accepted_by_entity: dict[str, list[dict[str, Any]]] = {}
        total_rows_read = 0

        # 3. Parse and Validate Each Entity
        expected_entities = list(dict.fromkeys([*files.keys(), *sorted(required_set), *sorted(optional_set)]))

        for entity in expected_entities:
            if entity not in ENTITY_ID:
                report["warnings"].append(f"unsupported_entity_skipped:{entity}")
                continue

            content = files.get(entity)
            stats = {
                "read": 0,
                "valid": 0,
                "inserted": 0,
                "updated": 0,
                "unchanged_replays": 0,
                "conflicts": 0,
                "quarantined": 0,
                "warnings": 0,
                "status": "pending",
            }

            if not content:
                if entity in optional_set:
                    stats["status"] = "optional_file_not_configured"
                    report["entities"][entity] = stats
                    report["warnings"].append({"entity": entity, "warning": "optional_file_not_configured"})
                    accepted_by_entity[entity] = []
                else:
                    stats["status"] = "failed_missing_required_entity"
                    report["entities"][entity] = stats
                    report["status"] = "failed"
                continue

            try:
                raw_rows = parse_csv_content(content)
            except Exception as exc:
                stats["status"] = "failed_read"
                stats["error"] = str(exc)
                report["entities"][entity] = stats
                report["status"] = "failed"
                continue

            stats["read"] = len(raw_rows)
            total_rows_read += len(raw_rows)
            stats["status"] = "loaded_empty" if not raw_rows else "loaded"

            entity_mapping = mappings.get(entity, {})
            good_rows: list[dict[str, Any]] = []

            for line_no, raw in enumerate(raw_rows, start=2):
                row = normalize_row(
                    raw=raw,
                    entity=entity,
                    tenant_id=tenant_id,
                    run_id=run_id,
                    source_system_id=source_system_id,
                    mapping_version=mapping_version,
                    field_mappings=entity_mapping,
                )
                errs = evaluate_row_errors(row, entity)
                if errs:
                    report["quarantine"].append(
                        {
                            "entity": entity,
                            "line": line_no,
                            "source_record_id": row.get("source_record_id"),
                            "errors": errs,
                            "record": raw,
                        }
                    )
                    stats["quarantined"] += 1
                else:
                    if entity == "applications":
                        src = str(row.get("source_channel") or "").strip().lower()
                        if not src or src in ("unknown", "unattributed", "n/a", "na"):
                            report["warnings"].append(
                                {
                                    "entity": entity,
                                    "source_record_id": row.get("source_record_id"),
                                    "warning": "unknown_source_channel",
                                }
                            )
                            stats["warnings"] += 1
                    good_rows.append(row)

            accepted_by_entity[entity] = good_rows
            stats["valid"] = len(good_rows)
            report["entities"][entity] = stats

        # Abort if any required entity failed reading
        if report["status"] == "failed":
            finished_now = datetime.now(timezone.utc)
            job.status = "failed"
            job.rows_read = total_rows_read
            job.rows_quarantined = len(report["quarantine"])
            job.error_summary = json.dumps({"error": "required_entity_failed_or_missing"})
            job.finished_at = finished_now
            db.commit()

            report["finished_at"] = finished_now.isoformat()
            report["summary"] = {
                "rows_read": total_rows_read,
                "inserted": 0,
                "updated": 0,
                "unchanged_replays": 0,
                "conflicts": 0,
                "quarantined": len(report["quarantine"]),
                "committed": False,
            }
            return report

        # 4. Cross-Reference Validation Against Staged + Stored Records
        existing_records = (
            db.query(CanonicalRecord)
            .filter(CanonicalRecord.tenant_id == tenant_id)
            .all()
        )
        existing_by_entity: dict[str, list[dict[str, Any]]] = {e: [] for e in ENTITIES}
        for r in existing_records:
            if r.entity_type in existing_by_entity:
                existing_by_entity[r.entity_type].append(json.loads(r.payload))

        staged_req_ids = {
            r.get("requisition_id")
            for r in (existing_by_entity["requisitions"] + accepted_by_entity.get("requisitions", []))
        }
        staged_app_ids = {
            r.get("application_id")
            for r in (existing_by_entity["applications"] + accepted_by_entity.get("applications", []))
        }

        # Filter out rows with unresolved references
        for entity, fk, valid_ids in [
            ("applications", "requisition_id", staged_req_ids),
            ("stage_events", "application_id", staged_app_ids),
            ("costs", "requisition_id", staged_req_ids),
        ]:
            kept: list[dict[str, Any]] = []
            for row in accepted_by_entity.get(entity, []):
                val = row.get(fk)
                if val and val not in valid_ids:
                    report["quarantine"].append(
                        {
                            "entity": entity,
                            "source_record_id": row.get("source_record_id"),
                            "errors": [f"unresolved_reference:{fk}:{val}"],
                            "record": row,
                        }
                    )
                    report["entities"][entity]["quarantined"] += 1
                    report["entities"][entity]["valid"] -= 1
                else:
                    kept.append(row)
            accepted_by_entity[entity] = kept

        # 5. Transactional Upsert and Staging
        # We start a savepoint/transaction to isolate our canonical mutations
        try:
            total_inserted = 0
            total_updated = 0
            total_replays = 0
            total_conflicts = 0

            # Map existing records for fast identity comparison
            # by source: (entity_type, source_system_id, source_record_id) -> record
            # by canonical id: (entity_type, entity_id) -> record
            source_map: dict[tuple[str, str, str], CanonicalRecord] = {}
            id_map: dict[tuple[str, str], CanonicalRecord] = {}

            for r in existing_records:
                source_map[(r.entity_type, r.source_system_id, r.source_record_id)] = r
                id_map[(r.entity_type, r.entity_id)] = r

            for entity in ENTITIES:
                stats = report["entities"].get(entity)
                if not stats or "failed" in stats.get("status", ""):
                    continue

                for row in accepted_by_entity.get(entity, []):
                    eid = row.get(ENTITY_ID[entity])
                    ss = row.get("source_system_id")
                    sr = row.get("source_record_id")

                    if not all([eid, ss, sr]):
                        report["quarantine"].append(
                            {
                                "entity": entity,
                                "source_record_id": sr,
                                "errors": ["missing_identity_fields"],
                                "record": row,
                            }
                        )
                        stats["quarantined"] += 1
                        stats["conflicts"] += 1
                        total_conflicts += 1
                        continue

                    fp = compute_fingerprint(row)
                    payload_str = json.dumps(row, sort_keys=True)

                    existing_by_src = source_map.get((entity, ss, sr))
                    if existing_by_src:
                        if existing_by_src.entity_id != eid:
                            report["quarantine"].append(
                                {
                                    "entity": entity,
                                    "source_record_id": sr,
                                    "errors": ["source_key_conflict_different_canonical_id"],
                                    "record": row,
                                }
                            )
                            stats["quarantined"] += 1
                            stats["conflicts"] += 1
                            total_conflicts += 1
                            continue
                        if existing_by_src.fingerprint == fp:
                            stats["unchanged_replays"] += 1
                            total_replays += 1
                            continue
                        # Updated record
                        existing_by_src.fingerprint = fp
                        existing_by_src.payload = payload_str
                        existing_by_src.import_job_id = job.id
                        stats["updated"] += 1
                        total_updated += 1
                        continue

                    existing_by_id = id_map.get((entity, eid))
                    if existing_by_id:
                        report["quarantine"].append(
                            {
                                "entity": entity,
                                "source_record_id": sr,
                                "errors": ["canonical_id_conflict_different_source_key"],
                                "record": row,
                            }
                        )
                        stats["quarantined"] += 1
                        stats["conflicts"] += 1
                        total_conflicts += 1
                        continue

                    # Insert new CanonicalRecord
                    new_rec = CanonicalRecord(
                        tenant_id=tenant_id,
                        entity_type=entity,
                        entity_id=eid,
                        source_system_id=ss,
                        source_record_id=sr,
                        fingerprint=fp,
                        payload=payload_str,
                        import_job_id=job.id,
                    )
                    db.add(new_rec)
                    source_map[(entity, ss, sr)] = new_rec
                    id_map[(entity, eid)] = new_rec
                    stats["inserted"] += 1
                    total_inserted += 1

            # 6. Semantic Validation of Full Resulting Bundle
            final_bundle: dict[str, Any] = {"client_id": tenant_id}
            for entity in ENTITIES:
                # Combine unchanged, updated, and newly inserted rows
                final_bundle[entity] = [
                    json.loads(r.payload)
                    for r in id_map.values()
                    if r.entity_type == entity
                ]

            validation = validate_bundle(final_bundle)
            report["semantic_validation"] = {
                "passed": validation["passed"],
                "errors": validation["errors"],
                "warnings": validation["warnings"],
            }

            if not validation["passed"]:
                logger.error(
                    "ingestion.semantic_validation_failed",
                    tenant_id=tenant_id,
                    errors=validation["errors"],
                )
                db.rollback()  # Rollback all CanonicalRecord changes!

                # Re-fetch or re-bind job to record failure status
                failed_job = db.query(ImportJob).filter(ImportJob.id == job.id).first()
                if failed_job:
                    failed_job.status = "failed_validation"
                    failed_job.rows_read = total_rows_read
                    failed_job.rows_quarantined = len(report["quarantine"])
                    failed_job.error_summary = json.dumps(
                        {"validation_errors": validation["errors"]}
                    )
                    failed_job.finished_at = datetime.now(timezone.utc)
                    db.commit()

                report["status"] = "failed_validation"
                report["finished_at"] = datetime.now(timezone.utc).isoformat()
                report["summary"] = {
                    "rows_read": total_rows_read,
                    "inserted": 0,
                    "updated": 0,
                    "unchanged_replays": 0,
                    "conflicts": total_conflicts,
                    "quarantined": len(report["quarantine"]),
                    "committed": False,
                }
                return report

            # Validation passed: finalize job & audit
            finished_now = datetime.now(timezone.utc)
            final_status = (
                "completed_with_quarantine"
                if report["quarantine"]
                else (
                    "completed_with_warnings"
                    if (report["warnings"] or validation["warnings"])
                    else "completed"
                )
            )

            job.status = final_status
            job.rows_read = total_rows_read
            job.rows_inserted = total_inserted
            job.rows_updated = total_updated
            job.rows_quarantined = len(report["quarantine"])
            job.finished_at = finished_now

            # Audit event
            audit = AuditEvent(
                tenant_id=tenant_id,
                actor_id=user_id,
                actor_type="user" if user_id else "system",
                event_type="ingestion.csv_import",
                resource_type="import_job",
                resource_id=job.id,
                outcome="success",
            )
            db.add(audit)
            db.commit()

            report["status"] = final_status
            report["finished_at"] = finished_now.isoformat()
            report["summary"] = {
                "rows_read": total_rows_read,
                "inserted": total_inserted,
                "updated": total_updated,
                "unchanged_replays": total_replays,
                "conflicts": total_conflicts,
                "quarantined": len(report["quarantine"]),
                "committed": True,
            }
            return report

        except Exception as exc:
            db.rollback()
            logger.error("ingestion.exception", error=str(exc))
            err_job = db.query(ImportJob).filter(ImportJob.id == job.id).first()
            if err_job:
                err_job.status = "failed"
                err_job.error_summary = json.dumps({"error": str(exc)})
                err_job.finished_at = datetime.now(timezone.utc)
                db.commit()
            raise
