"""Multi-tenant ingestion and refresh router (/v2/ingestion).

Requirements:
  - Role-based authorization: requires 'admin' or 'owner' for imports/refresh.
  - Server-verified tenant scoping: derived strictly from `ctx.tenant_id`.
  - Durable job tracking and safe error reporting.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.dependencies import AuthenticatedContext, require_role
from app.core.exceptions import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.ingestion.service import IngestionService
from app.persistence.database import get_db

logger = get_logger(__name__)

router = APIRouter(prefix="/v2/ingestion", tags=["Data Ingestion & Refresh"])


class RefreshRequest(BaseModel):
    source_system_id: str = Field(default="on_demand_refresh", description="Source system tag")
    mapping_version: str = Field(default="1.0", description="Schema mapping version")
    optional_entities: list[str] = Field(
        default=["stage_events", "costs"],
        description="Entities allowed to be omitted without failing the import",
    )
    # Inline CSV text payload support for API-driven ingestion
    csv_payloads: Optional[dict[str, str]] = Field(
        default=None,
        description="Optional dict of entity -> raw CSV string content",
    )


class JobSummaryResponse(BaseModel):
    job_id: str
    status: str
    source_system_id: str
    mapping_version: str
    rows_read: int
    rows_inserted: int
    rows_updated: int
    rows_quarantined: int
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    created_at: str


@router.post(
    "/refresh",
    summary="Trigger an on-demand canonical ingestion refresh",
    response_description="Ingestion job execution report",
)
def trigger_refresh(
    req: RefreshRequest,
    ctx: AuthenticatedContext = Depends(require_role("admin")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Execute on-demand ingestion for the authenticated tenant.

    Guarantees:
      - Admin or Owner privilege enforced.
      - Tenant scope strictly bound to `ctx.tenant_id`.
      - Prevents concurrent overlapping imports via 409 Conflict.
      - Preserves last successful dataset if validation fails.
    """
    files: dict[str, Any] = req.csv_payloads or {}

    report = IngestionService.ingest_bundle(
        db,
        tenant_id=ctx.tenant_id,
        files=files,
        user_id=ctx.user_id,
        source_system_id=req.source_system_id,
        mapping_version=req.mapping_version,
        optional_entities=req.optional_entities,
    )
    return report


@router.post(
    "/csv",
    summary="Upload CSV files for canonical ingestion",
    response_description="Ingestion job execution report",
)
async def upload_csv_bundle(
    requisitions: Optional[UploadFile] = File(None, description="Requisitions CSV file"),
    applications: Optional[UploadFile] = File(None, description="Applications CSV file"),
    stage_events: Optional[UploadFile] = File(None, description="Stage Events CSV file (optional)"),
    costs: Optional[UploadFile] = File(None, description="Costs CSV file (optional)"),
    source_system_id: str = Form("csv_upload"),
    mapping_version: str = Form("1.0"),
    ctx: AuthenticatedContext = Depends(require_role("admin")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Upload multipart CSV files and ingest into tenant canonical store."""
    files: dict[str, bytes] = {}
    for name, upload in [
        ("requisitions", requisitions),
        ("applications", applications),
        ("stage_events", stage_events),
        ("costs", costs),
    ]:
        if upload and upload.filename:
            content = await upload.read()
            if content:
                files[name] = content

    report = IngestionService.ingest_bundle(
        db,
        tenant_id=ctx.tenant_id,
        files=files,
        user_id=ctx.user_id,
        source_system_id=source_system_id,
        mapping_version=mapping_version,
        optional_entities=["stage_events", "costs"],
    )
    return report


@router.get(
    "/jobs",
    summary="List recent ingestion jobs for caller's tenant",
    response_model=list[JobSummaryResponse],
)
def list_jobs(
    limit: int = 50,
    ctx: AuthenticatedContext = Depends(require_role("viewer")),
    db: Session = Depends(get_db),
) -> list[JobSummaryResponse]:
    """List recent import jobs scoped to the authenticated tenant."""
    jobs = IngestionService.list_jobs(db, tenant_id=ctx.tenant_id, limit=limit)
    return [
        JobSummaryResponse(
            job_id=j.id,
            status=j.status,
            source_system_id=j.source_system_id,
            mapping_version=j.mapping_version,
            rows_read=j.rows_read,
            rows_inserted=j.rows_inserted,
            rows_updated=j.rows_updated,
            rows_quarantined=j.rows_quarantined,
            started_at=j.started_at.isoformat() if j.started_at else None,
            finished_at=j.finished_at.isoformat() if j.finished_at else None,
            created_at=j.created_at.isoformat(),
        )
        for j in jobs
    ]


@router.get(
    "/jobs/{job_id}",
    summary="Get status and summary of a specific ingestion job",
)
def get_job_status(
    job_id: str,
    ctx: AuthenticatedContext = Depends(require_role("viewer")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Retrieve details of an import job within caller's tenant."""
    job = IngestionService.get_job(db, tenant_id=ctx.tenant_id, job_id=job_id)
    return {
        "job_id": job.id,
        "tenant_id": job.tenant_id,
        "status": job.status,
        "source_system_id": job.source_system_id,
        "mapping_version": job.mapping_version,
        "rows_read": job.rows_read,
        "rows_inserted": job.rows_inserted,
        "rows_updated": job.rows_updated,
        "rows_quarantined": job.rows_quarantined,
        "error_summary": job.error_summary,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "created_at": job.created_at.isoformat(),
    }
