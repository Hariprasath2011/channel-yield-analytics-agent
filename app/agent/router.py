"""Controlled agent query interface (/v2/agent/query).

Enforces:
  - Explicit allowlist of analytical operations (no raw SQL, no URL fetching, no shell execution).
  - Server-side tenant scoping: the agent tool execution is strictly bound to `ctx.tenant_id`.
  - Role-based authorization: requires 'analyst' or higher.
  - Parameter validation and safe deterministic routing.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.analytics.adapter import compute_tenant_analytics
from app.auth.dependencies import AuthenticatedContext, require_role
from app.core.exceptions import NotFoundError
from app.core.logging import get_logger, request_id_var
from app.persistence.database import get_db

logger = get_logger(__name__)

router = APIRouter(prefix="/v2/agent", tags=["Controlled Agent Tools"])

_PROHIBITED_PATTERNS = [
    re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE|DROP|UNION|ALTER|EXEC|EVAL)\b", re.IGNORECASE),
    re.compile(r"(--|/\*|\*/|;|\||`|\${)", re.IGNORECASE),
    re.compile(r"\b(https?|ftp|file)://", re.IGNORECASE),
    re.compile(r"\b(cmd\.exe|powershell|bash|sh|bin)\b", re.IGNORECASE),
]

_ALLOWLISTED_OPERATIONS = {
    "query_channel_yield",
    "query_requisition_summary",
    "rank_recruitment_channels",
    "get_data_quality_report",
}


# ─── Request / Response Models ────────────────────────────────────────────────


class AgentQueryRequest(BaseModel):
    operation: str = Field(
        ...,
        description="Allowlisted operation: query_channel_yield | query_requisition_summary | rank_recruitment_channels | get_data_quality_report",
    )
    parameters: dict[str, Any] = Field(
        default_factory=dict,
        description="Typed parameters for the requested operation",
    )


class AgentQueryResponse(BaseModel):
    status: str = "success"
    operation: str
    execution_mode: str = "deterministic_rule_engine"
    request_id: str
    tenant_id: str
    data: dict[str, Any]


# ─── Safety Inspection ────────────────────────────────────────────────────────


def _assert_safe_parameters(params: dict[str, Any]) -> None:
    """Scan all string parameters recursively for injection or malicious vectors."""
    for key, value in params.items():
        if isinstance(value, str):
            for pattern in _PROHIBITED_PATTERNS:
                if pattern.search(value):
                    logger.warning(
                        "security.agent_prohibited_parameter_rejected",
                        param_key=key,
                        matched_pattern=pattern.pattern,
                    )
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail={
                            "error": "prohibited_parameter",
                            "message": f"Parameter '{key}' contains disallowed characters or patterns.",
                        },
                    )
        elif isinstance(value, dict):
            _assert_safe_parameters(value)


# ─── Endpoint ─────────────────────────────────────────────────────────────────


@router.post(
    "/query",
    response_model=AgentQueryResponse,
    summary="Execute a typed, allowlisted analytical operation through the agent interface",
)
def execute_agent_query(
    req: AgentQueryRequest,
    ctx: AuthenticatedContext = Depends(require_role("analyst")),
    db: Session = Depends(get_db),
) -> AgentQueryResponse:
    """Controlled analytical tool execution for automated agents and analysts.

    Execution Model:
      - Deterministic rule engine routing (not autonomous, unconstrained code execution).
      - Strict allowlist: only approved operations are dispatched.
      - Tenant scoping is enforced server-side; caller cannot override `tenant_id`.
    """
    op = req.operation.strip()
    if op not in _ALLOWLISTED_OPERATIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "unsupported_operation",
                "message": f"Operation '{op}' is not in the approved agent tools allowlist.",
                "allowed_operations": sorted(_ALLOWLISTED_OPERATIONS),
            },
        )

    # Validate against injection or external URL fetching
    _assert_safe_parameters(req.parameters)

    params = req.parameters
    cohort_start = params.get("cohort_start", "2026-01-01")
    cohort_end = params.get("cohort_end", "2026-06-30")
    as_of = params.get("as_of_date") or date.today().isoformat()
    observation_days = int(params.get("observation_days", 90))
    requisition_id = params.get("requisition_id")
    min_sample_size = params.get("min_sample_size")

    # Date sanity checks
    try:
        start_d = date.fromisoformat(cohort_start)
        end_d = date.fromisoformat(cohort_end)
        as_of_d = date.fromisoformat(as_of)
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_date_format", "message": str(exc)},
        )

    if end_d < start_d:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_date_range", "message": "cohort_end must be on or after cohort_start."},
        )

    # Bounded query window: max 730 days (2 years)
    if (end_d - start_d).days > 730:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "query_window_too_large", "message": "Cohort window cannot exceed 730 days."},
        )

    if observation_days < 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_parameter", "message": "observation_days must be >= 0."},
        )

    try:
        if op == "query_channel_yield":
            raw_result = compute_tenant_analytics(
                db,
                tenant_id=ctx.tenant_id,
                cohort_start=cohort_start,
                cohort_end=cohort_end,
                as_of_date=as_of,
                observation_days=observation_days,
                requisition_id=requisition_id,
                min_sample_size=min_sample_size,
            )
            data_output = raw_result

        elif op == "query_requisition_summary":
            if not requisition_id or not str(requisition_id).strip():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"error": "missing_parameter", "message": "requisition_id is required for this operation."},
                )
            raw_result = compute_tenant_analytics(
                db,
                tenant_id=ctx.tenant_id,
                cohort_start=cohort_start,
                cohort_end=cohort_end,
                as_of_date=as_of,
                observation_days=observation_days,
                requisition_id=str(requisition_id).strip(),
                min_sample_size=min_sample_size,
            )
            data_output = {
                "requisition_id": requisition_id,
                "summary": raw_result.get("summary", {}),
                "channel_metrics": raw_result.get("channel_metrics", []),
                "data_quality": raw_result.get("data_quality", {}),
            }

        elif op == "rank_recruitment_channels":
            if min_sample_size is None or int(min_sample_size) < 1:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={
                        "error": "ranking_threshold_required",
                        "message": "min_sample_size >= 1 is required to produce authoritative rankings.",
                    },
                )
            raw_result = compute_tenant_analytics(
                db,
                tenant_id=ctx.tenant_id,
                cohort_start=cohort_start,
                cohort_end=cohort_end,
                as_of_date=as_of,
                observation_days=observation_days,
                requisition_id=requisition_id,
                min_sample_size=int(min_sample_size),
            )
            data_output = {
                "ranking_status": raw_result.get("ranking_status"),
                "ranked_channels": raw_result.get("ranked_channels", []),
                "data_quality": raw_result.get("data_quality", {}),
            }

        elif op == "get_data_quality_report":
            raw_result = compute_tenant_analytics(
                db,
                tenant_id=ctx.tenant_id,
                cohort_start=cohort_start,
                cohort_end=cohort_end,
                as_of_date=as_of,
                observation_days=observation_days,
                requisition_id=requisition_id,
                min_sample_size=min_sample_size,
            )
            data_output = {
                "data_quality": raw_result.get("data_quality", {}),
                "summary": raw_result.get("summary", {}),
                "contract_version": raw_result.get("contract_version"),
            }

    except NotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "requisition_not_found",
                "message": f"Requisition '{requisition_id}' not found in your organization.",
            },
        )

    rid = request_id_var.get() or "unknown"
    return AgentQueryResponse(
        status="success",
        operation=op,
        execution_mode="deterministic_rule_engine",
        request_id=rid,
        tenant_id=ctx.tenant_id,
        data=data_output,
    )
