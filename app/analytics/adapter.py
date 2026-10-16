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
    from analytics_service import (
        compute_analytics as _compute_analytics,
        compute_analytics_from_records as _compute_analytics_from_records,
    )
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

    # Delegate directly to in-memory engine (eliminating SQLite intermediary)
    result = _compute_analytics_from_records(
        client_id=tenant_id,
        requisitions=reqs,
        applications=apps,
        stage_events=events,
        costs=costs,
        cohort_start=cohort_start,
        cohort_end=cohort_end,
        as_of_date=as_of_date,
        observation_days=observation_days,
        requisition_id=requisition_id,
        min_sample_size=min_sample_size,
    )

    if result.get("status") == "not_found":
        raise NotFoundError("Requisition")

    return result


def compute_live_channel_yield(
    *,
    tenant_id: str,
    requisitions: list[dict],
    applications: list[dict],
    stage_events: Optional[list[dict]] = None,
    costs: Optional[list[dict]] = None,
    cohort_start: str,
    cohort_end: str,
    as_of_date: str,
    observation_days: int,
    requisition_id: Optional[str] = None,
    min_sample_size: Optional[int] = None,
) -> dict:
    """Compute channel yield directly on in-memory mapped Zoho collections.

    Zero-data-persistence: Collections are never written to database tables or files.
    """
    if not _ANALYTICS_AVAILABLE:
        raise AnalyticsError("Analytics engine is not available.")

    # Validate dates
    try:
        start_d = date.fromisoformat(cohort_start)
        end_d = date.fromisoformat(cohort_end)
        as_of_d = date.fromisoformat(as_of_date)
    except ValueError as exc:
        raise AnalyticsError(f"Invalid date: {exc}") from exc

    if end_d < start_d:
        raise AnalyticsError("cohort_end must be on or after cohort_start.")

    return _compute_analytics_from_records(
        client_id=tenant_id,
        requisitions=requisitions,
        applications=applications,
        stage_events=stage_events or [],
        costs=costs or [],
        cohort_start=cohort_start,
        cohort_end=cohort_end,
        as_of_date=as_of_date,
        observation_days=observation_days,
        requisition_id=requisition_id,
        min_sample_size=min_sample_size,
    )
