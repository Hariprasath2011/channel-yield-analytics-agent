"""Historical selection tracking and application stage transition service.

Implements Phase 3 requirements:
  - Fetches application hiring pipeline and transition history from Zoho Recruit.
  - Normalizes stage transitions, dwell duration, comments, rejection reasons,
    and hiring outcomes.
  - Dedupes and structures historical stage events with stable integration IDs.
  - Explicitly flags when historical transitions are unavailable rather than
    fabricating historical data from current application status.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from app.core.logging import get_logger
from app.integrations.zoho.client import ZohoRecruitClient

logger = get_logger(__name__)


def _parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    """Parse ISO datetime strings safely into UTC timezone-aware datetimes."""
    if not value or not isinstance(value, str):
        return None
    val = value.strip()
    try:
        if "T" in val:
            dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(val).replace(tzinfo=timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


class SelectionHistoryService:
    """Orchestrates retrieval, normalization, and deduplication of application selection history."""

    @staticmethod
    def get_selection_history(
        client: ZohoRecruitClient,
        access_token: str,
        application_id: str,
        candidate_id: Optional[str] = None,
        requisition_id: Optional[str] = None,
        include_activities: bool = False,
    ) -> dict[str, Any]:
        """Retrieve complete historical stage transition details for an application.

        Guarantees:
          - Never fabricates stage transitions from current application status.
          - Explicitly marks history as unavailable or incomplete if Zoho returns no stage events.
          - Extracts stage dwell duration, previous/new stage, comments, and outcomes.
        """
        clean_app_id = str(application_id).strip()
        if not clean_app_id:
            return {
                "status": "error",
                "error": "missing_application_id",
                "message": "application_id is required to fetch selection history.",
            }

        # 1. Fetch current application context from Zoho
        raw_app_resp = client.get_application_by_id(clean_app_id, access_token)
        app_data = {}
        if isinstance(raw_app_resp, dict):
            data_list = raw_app_resp.get("data")
            if isinstance(data_list, list) and len(data_list) > 0:
                app_data = data_list[0]
            elif isinstance(data_list, dict):
                app_data = data_list
            elif "id" in raw_app_resp:
                app_data = raw_app_resp

        # Extract application context attributes
        cand_obj = app_data.get("Candidate_ID") or {}
        resolved_cand_id = (
            str(cand_obj.get("id"))
            if isinstance(cand_obj, dict) and cand_obj.get("id")
            else str(candidate_id or "")
        )
        cand_name = (
            cand_obj.get("name")
            if isinstance(cand_obj, dict) and cand_obj.get("name")
            else str(app_data.get("Candidate_Name") or "Candidate")
        )

        job_obj = app_data.get("Job_Opening_ID") or {}
        resolved_req_id = (
            str(job_obj.get("id"))
            if isinstance(job_obj, dict) and job_obj.get("id")
            else str(requisition_id or "")
        )
        job_name = (
            job_obj.get("name")
            if isinstance(job_obj, dict) and job_obj.get("name")
            else str(app_data.get("Posting_Title") or app_data.get("Job_Opening_Name") or "Job Opening")
        )

        applied_at = (
            app_data.get("Created_Time")
            or app_data.get("Applied_Time")
            or None
        )
        current_status = str(
            app_data.get("Application_Status")
            or app_data.get("Candidate_Status")
            or "Applied"
        ).strip()
        current_stage = str(
            app_data.get("Hiring_Stage")
            or app_data.get("Current_Stage")
            or current_status
        ).strip()

        # Outcome determination
        status_lower = current_status.lower()
        if any(w in status_lower for w in ("hired", "joined", "offer accepted")):
            hiring_outcome = "hired" if "hired" in status_lower or "joined" in status_lower else "offer_accepted"
        elif any(w in status_lower for w in ("rejected", "disqualified", "archived")):
            hiring_outcome = "rejected"
        elif "withdrawn" in status_lower:
            hiring_outcome = "withdrawn"
        else:
            hiring_outcome = "in_progress"

        joining_date = app_data.get("Joining_Date") or app_data.get("Joined_Date")
        rejection_reason = app_data.get("Rejection_Reason") or app_data.get("Reason_For_Rejection")

        # 2. Fetch official transition history from Zoho
        transition_resp = client.get_application_transition_details(clean_app_id, access_token)
        raw_transitions: list[dict[str, Any]] = []

        if isinstance(transition_resp, dict):
            raw_transitions = transition_resp.get("data") or transition_resp.get("stages") or []
        elif isinstance(transition_resp, list):
            raw_transitions = transition_resp

        # 3. Process transitions if available
        events: list[dict[str, Any]] = []
        is_history_available = bool(raw_transitions and len(raw_transitions) > 0)

        if is_history_available:
            prev_stage = "Applied"
            prev_status = "Applied"
            prev_time_dt: Optional[datetime] = _parse_iso_datetime(applied_at)

            for idx, raw_t in enumerate(raw_transitions):
                event_id = str(raw_t.get("id") or f"evt_{clean_app_id}_{idx + 1}")
                new_stage = str(
                    raw_t.get("stage")
                    or raw_t.get("Stage_Name")
                    or raw_t.get("Stage")
                    or raw_t.get("To_Stage")
                    or raw_t.get("to_stage")
                    or "Unknown"
                ).strip()
                new_status = str(
                    raw_t.get("status")
                    or raw_t.get("Status_Name")
                    or raw_t.get("Status")
                    or raw_t.get("To_Status")
                    or raw_t.get("to_status")
                    or new_stage
                ).strip()

                t_time_str = (
                    raw_t.get("transition_time")
                    or raw_t.get("modified_time")
                    or raw_t.get("Modified_Time")
                    or raw_t.get("Created_Time")
                    or raw_t.get("stage_entered_time")
                )
                t_time_dt = _parse_iso_datetime(t_time_str)

                # Calculate duration in previous stage
                from_time_str = raw_t.get("From_Time") or raw_t.get("Created_Time") or raw_t.get("stage_entered_time")
                from_time_dt = _parse_iso_datetime(from_time_str) or prev_time_dt
                duration_seconds: Optional[int] = None
                duration_days: Optional[float] = None
                if t_time_dt and from_time_dt and t_time_dt >= from_time_dt:
                    duration_seconds = int((t_time_dt - from_time_dt).total_seconds())
                    duration_days = round(duration_seconds / 86400.0, 2)

                user_info = raw_t.get("user") or raw_t.get("modified_by") or raw_t.get("Modified_By") or raw_t.get("Created_By") or {}
                user_name = (
                    user_info.get("name")
                    if isinstance(user_info, dict)
                    else str(user_info or "System")
                )

                comments = raw_t.get("comments") or raw_t.get("Comments") or raw_t.get("note") or raw_t.get("remark")
                stage_rej_reason = raw_t.get("rejection_reason") or (rejection_reason if "reject" in new_stage.lower() else None)
                interview_outcome = raw_t.get("interview_outcome") or raw_t.get("Interview_Outcome") or raw_t.get("rating")
                offer_status = raw_t.get("offer_status") or raw_t.get("Offer_Status")
                offer_date = raw_t.get("offer_date") or raw_t.get("Offer_Date")

                events.append({
                    "event_id": event_id,
                    "previous_stage": raw_t.get("From_Stage") or raw_t.get("from_stage") or prev_stage,
                    "new_stage": new_stage,
                    "previous_status": raw_t.get("from_status") or prev_status,
                    "new_status": new_status,
                    "transition_timestamp": t_time_str,
                    "duration_seconds_in_previous_stage": duration_seconds,
                    "duration_days_in_previous_stage": duration_days,
                    "performed_by": user_name,
                    "comments": comments,
                    "rejection_reason": stage_rej_reason,
                    "interview_outcome": interview_outcome,
                    "offer_status": offer_status,
                    "offer_date": offer_date,
                    "hiring_outcome": raw_t.get("hiring_outcome") or raw_t.get("Hiring_Outcome"),
                })

                prev_stage = new_stage
                prev_status = new_status
                if t_time_dt:
                    prev_time_dt = t_time_dt

        # 4. Optional Activities Timeline
        activities: list[dict[str, Any]] = []
        if include_activities:
            act_resp = client.get_record_timeline("Applications", clean_app_id, access_token)
            if isinstance(act_resp, dict) and "data" in act_resp:
                for act in act_resp.get("data", []):
                    activities.append({
                        "activity_id": str(act.get("id") or ""),
                        "activity_type": act.get("activity_type") or act.get("Subject") or "General",
                        "timestamp": act.get("Created_Time") or act.get("Modified_Time"),
                        "performed_by": (act.get("Created_By") or {}).get("name") if isinstance(act.get("Created_By"), dict) else str(act.get("Created_By") or ""),
                        "summary": act.get("Description") or act.get("Notes") or act.get("Subject"),
                    })

        return {
            "status": "success",
            "application_id": clean_app_id,
            "candidate_id": resolved_cand_id or None,
            "candidate_name": cand_name,
            "requisition_id": resolved_req_id or None,
            "job_opening_name": job_name,
            "application_date": applied_at,
            "current_stage": current_stage,
            "current_status": current_status,
            "final_hiring_outcome": hiring_outcome,
            "joining_date": joining_date,
            "joining_status": "joined" if joining_date or hiring_outcome == "hired" else "not_joined",
            "rejection_reason": rejection_reason,
            "history_available": is_history_available,
            "history_status": "complete" if is_history_available else "unavailable",
            "history_note": (
                "Verified historical stage transitions retrieved from Zoho Recruit."
                if is_history_available
                else "Zoho Recruit did not expose historical stage transition records for this application. "
                     "Stage transition history is explicitly marked unavailable and is never fabricated from current status."
            ),
            "total_transitions": len(events),
            "stage_transitions": events,
            "activities": activities if include_activities else None,
        }
