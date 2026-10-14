"""Analytics adapter — tenant-aware wrapper around the existing analytics engine.

This module bridges the new multi-tenant architecture with the existing
deterministic analytics calculation engine (analytics_service.py).

The adapter:
  1. Fetches canonical records from the new SQLAlchemy CanonicalRecord table.
  2. Reconstructs the payload format expected by compute_analytics().
  3. Delegates all calculations to the existing engine.
  4. Returns the result with tenant context metadata added.

This design preserves the validated, tested analytics logic while adding
proper tenant isolation at the data access layer.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from app.core.exceptions import AnalyticsError, NotFoundError
from app.core.logging import get_logger
from app.persistence.models import CanonicalRecord

logger = get_logger(__name__)

# Import the existing analytics engine (preserved from prototype).
# This import works because the repo root is on the Python path.
try:
    from analytics_service import compute_analytics as _compute_analytics
    _ANALYTICS_AVAILABLE = True
except ImportError:
    _ANALYTICS_AVAILABLE = False
    logger.error("analytics.engine_import_failed")


def _fetch_tenant_records(
    db: Session, tenant_id: str, entity_type: str
) -> list[dict]:
    """Fetch canonical records for a tenant, returning parsed payloads.

    Every query is scoped to tenant_id — cross-tenant access is impossible
    because the WHERE clause always includes tenant_id = :tenant_id.
    """
    rows = (
        db.query(CanonicalRecord)
        .filter(
            CanonicalRecord.tenant_id == tenant_id,
            CanonicalRecord.entity_type == entity_type,
        )
        .order_by(CanonicalRecord.entity_id)
        .all()
    )
    return [json.loads(r.payload) for r in rows]


def compute_tenant_analytics(
    db: Session,
    *,
    tenant_id: str,
    cohort_start: str,
    cohort_end: str,
    as_of_date: str,
    observation_days: int,
    requisition_id: Optional[str] = None,
    min_sample_size: Optional[int] = None,
) -> dict:
    """Compute channel yield analytics for a specific tenant.

    Data is read from the PostgreSQL/SQLite CanonicalRecord table and
    delegated to the existing, tested analytics engine.

    The tenant_id is provided by the server-side auth context and never
    comes from the request body or query parameters.
    """
    if not _ANALYTICS_AVAILABLE:
        raise AnalyticsError("Analytics engine is not available.")

    # Validate dates (the engine also validates, but fail fast here).
    try:
        start_d = date.fromisoformat(cohort_start)
        end_d = date.fromisoformat(cohort_end)
        as_of_d = date.fromisoformat(as_of_date)
    except ValueError as exc:
        raise AnalyticsError(f"Invalid date: {exc}") from exc

    if end_d < start_d:
        raise AnalyticsError("cohort_end must be on or after cohort_start.")

    # Fetch tenant-scoped records.
    reqs = _fetch_tenant_records(db, tenant_id, "requisitions")
    apps = _fetch_tenant_records(db, tenant_id, "applications")
    events = _fetch_tenant_records(db, tenant_id, "stage_events")
    costs = _fetch_tenant_records(db, tenant_id, "costs")

    logger.info(
        "analytics.records_fetched",
        tenant_id=tenant_id,
        requisitions=len(reqs),
        applications=len(apps),
        stage_events=len(events),
        costs=len(costs),
    )

    # Write to a temp SQLite DB for the existing engine.
    # TODO (Phase E): Refactor analytics_service to accept data directly
    # and eliminate this SQLite intermediary step.
    import sqlite3
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tmp:
        tmp_path = Path(tmp.name)

    try:
        conn = sqlite3.connect(tmp_path)
        conn.execute(
            "CREATE TABLE canonical_records "
            "(entity TEXT, client_id TEXT, entity_id TEXT, "
            "source_system_id TEXT, source_record_id TEXT, "
            "fingerprint TEXT, payload TEXT, "
            "PRIMARY KEY(entity, client_id, entity_id))"
        )
        for entity, records in [
            ("requisitions", reqs),
            ("applications", apps),
            ("stage_events", events),
            ("costs", costs),
        ]:
            for rec in records:
                entity_id_field = {
                    "requisitions": "requisition_id",
                    "applications": "application_id",
                    "stage_events": "stage_event_id",
                    "costs": "cost_id",
                }.get(entity, "id")
                conn.execute(
                    "INSERT OR IGNORE INTO canonical_records VALUES (?,?,?,?,?,?,?)",
                    (
                        entity,
                        tenant_id,
                        rec.get(entity_id_field, ""),
                        rec.get("source_system_id", ""),
                        rec.get("source_record_id", ""),
                        "",
                        json.dumps(rec),
                    ),
                )
        conn.commit()
        conn.close()

        result = _compute_analytics(
            tmp_path,
            client_id=tenant_id,
            cohort_start=cohort_start,
            cohort_end=cohort_end,
            as_of_date=as_of_date,
            observation_days=observation_days,
            requisition_id=requisition_id,
            min_sample_size=min_sample_size,
        )
    finally:
        try:
            tmp_path.unlink()
        except Exception:
            pass

    if result.get("status") == "not_found":
        raise NotFoundError("Requisition")

    return result
