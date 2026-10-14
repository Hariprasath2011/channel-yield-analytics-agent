"""Field mapper translating Zoho Recruit API payloads into canonical schema."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional


def _to_iso(date_str: Optional[str]) -> Optional[str]:
    """Normalize Zoho timestamps to ISO 8601 with UTC timezone."""
    if not date_str:
        return None
    date_str = date_str.strip()
    try:
        # Zoho timestamps usually: 2026-03-15T10:00:00+00:00 or 2026-03-15
        if "T" in date_str:
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except Exception:
        return None


def map_zoho_job_opening(record: dict[str, Any], tenant_id: str, run_id: str) -> dict[str, Any]:
    """Map Zoho JobOpenings module record to canonical requisition."""
    rec_id = str(record.get("id") or record.get("Job_Opening_ID") or "")
    req_no = str(record.get("Job_Opening_ID") or rec_id)
    title = str(record.get("Posting_Title") or record.get("Job_Opening_Name") or "Untitled Opening").strip()
    status_raw = str(record.get("Job_Opening_Status") or "open").strip().lower()

    return {
        "client_id": tenant_id,
        "requisition_id": req_no,
        "job_title": title,
        "status": status_raw,
        "role_family": record.get("Department") or "General",
        "source_system_id": "zoho_recruit",
        "source_record_id": rec_id,
        "mapping_version": "zoho_v2",
        "ingest_run_id": run_id,
    }


def map_zoho_application(
    app_record: dict[str, Any],
    candidate_source_map: dict[str, str],
    tenant_id: str,
    run_id: str,
) -> dict[str, Any]:
    """Map Zoho Application / Associated Candidate record to canonical application."""
    app_id = str(app_record.get("id") or "")
    cand_obj = app_record.get("Candidate_ID") or {}
    cand_id = cand_obj.get("id") if isinstance(cand_obj, dict) else str(cand_obj)

    job_obj = app_record.get("Job_Opening_ID") or {}
    req_id = str(job_obj.get("id") or job_obj) if isinstance(job_obj, dict) else str(job_obj)

    # Resolve source channel: prefer candidate source, then application lead source
    source = (
        candidate_source_map.get(cand_id)
        or app_record.get("Lead_Source")
        or app_record.get("Source")
        or "unknown"
    )

    # Normalize outcome
    status_raw = str(
        app_record.get("Application_Status")
        or app_record.get("Candidate_Status")
        or "Applied"
    ).strip().lower()

    if any(k in status_raw for k in ["hired", "joined"]):
        outcome = "joined"
    elif any(k in status_raw for k in ["reject", "archive", "disqualified"]):
        outcome = "rejected"
    elif "withdrawn" in status_raw:
        outcome = "withdrawn"
    elif "offer accepted" in status_raw:
        outcome = "offer_accepted"
    else:
        outcome = "in_progress"

    applied_at = _to_iso(app_record.get("Created_Time")) or datetime.now(timezone.utc).isoformat()
    joined_at = None
    if outcome == "joined":
        joined_at = _to_iso(app_record.get("Joining_Date") or app_record.get("Joined_Date") or app_record.get("Modified_Time") or applied_at)

    return {
        "client_id": tenant_id,
        "application_id": app_id,
        "requisition_id": req_id,
        "source_channel": source,
        "applied_at": applied_at,
        "application_outcome": outcome,
        "joined_at": joined_at,
        "source_system_id": "zoho_recruit",
        "source_record_id": app_id,
        "mapping_version": "zoho_v2",
        "ingest_run_id": run_id,
    }
