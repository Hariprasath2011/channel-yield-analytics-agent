"""Multi-tenant recruitment channel yield analytics router (/v2/analytics).

Tenant identity is exclusively derived from the server-verified AuthenticatedContext.
Clients cannot supply, override, or spoof the tenant ID.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.analytics.adapter import compute_tenant_analytics
from app.auth.dependencies import AuthenticatedContext, require_role
from app.core.exceptions import NotFoundError
from app.core.logging import get_logger
from app.persistence.database import get_db

logger = get_logger(__name__)

router = APIRouter(prefix="/v2/analytics", tags=["Channel Yield Analytics"])


@router.get(
    "",
    summary="Compute recruitment channel yield analytics for caller's organization",
    response_description="Authoritative recruitment channel yield analytics",
)
def get_analytics(
    cohort_start: str = Query("2026-01-01", description="Cohort start date YYYY-MM-DD"),
    cohort_end: str = Query("2026-06-30", description="Cohort end date YYYY-MM-DD"),
    as_of_date: Optional[str] = Query(None, description="Maturity evaluation date YYYY-MM-DD"),
    observation_days: int = Query(90, ge=0, description="Cohort maturity window in days"),
    requisition_id: Optional[str] = Query(None, description="Optional requisition filter within tenant"),
    min_sample_size: Optional[int] = Query(None, ge=1, description="Approved ranking eligibility threshold"),
    ctx: AuthenticatedContext = Depends(require_role("viewer")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Retrieve authoritative recruitment channel yield analytics for the authenticated organization.

    Security & Isolation Guarantees:
      - Tenant scope is strictly bound to `ctx.tenant_id` from the verified credential.
      - Any caller-supplied tenant identifier is ignored.
      - Requisitions are filtered by tenant_id; querying an unknown or foreign requisition
        returns a 404 without disclosing foreign tenant data.
    """
    # Validate date formatting
    as_of = as_of_date or date.today().isoformat()
    try:
        start_d = date.fromisoformat(cohort_start)
        end_d = date.fromisoformat(cohort_end)
        as_of_d = date.fromisoformat(as_of)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_date_format", "message": f"Dates must use YYYY-MM-DD: {exc}"},
        )

    if end_d < start_d:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_date_range", "message": "cohort_end must be on or after cohort_start."},
        )

    if requisition_id is not None and not requisition_id.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_parameter", "message": "requisition_id cannot be blank."},
        )

    try:
        result = compute_tenant_analytics(
            db,
            tenant_id=ctx.tenant_id,
            cohort_start=cohort_start,
            cohort_end=cohort_end,
            as_of_date=as_of,
            observation_days=observation_days,
            requisition_id=requisition_id.strip() if requisition_id else None,
            min_sample_size=min_sample_size,
        )
        return result
    except NotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "requisition_not_found",
                "message": f"Requisition '{requisition_id}' not found in your organization.",
            },
        )
