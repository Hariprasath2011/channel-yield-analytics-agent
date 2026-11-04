"""Administrator router for system diagnostics and migration status (/v2/admin).

Endpoints:
  - GET /v2/admin/database/migration-status:
      Inspect current Alembic revision in the production database vs. the application's
      expected head revision. Requires administrator JWT authentication.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.auth.dependencies import AuthenticatedContext, require_admin_jwt
from app.core.logging import get_logger
from app.persistence.database import get_db

logger = get_logger(__name__)

router = APIRouter(prefix="/v2/admin", tags=["Admin Operations"])


class MigrationStatusResponse(BaseModel):
    """Safe database migration status payload with zero credentials or SQL exposed."""

    database_connected: bool = Field(
        ...,
        description="Whether the database connection is active and responding to queries.",
    )
    current_revision: Optional[str] = Field(
        None,
        description="Current revision identifier stored in the database alembic_version table, if present.",
    )
    expected_head_revision: Optional[str] = Field(
        None,
        description="Latest head migration revision identifier defined in Alembic migrations.",
    )
    migration_pending: bool = Field(
        ...,
        description="True if the database has not yet been upgraded to the expected head revision.",
    )


def resolve_expected_head_revision() -> Optional[str]:
    """Inspect local Alembic script directory to resolve expected head revision.

    Returns the target revision string (e.g., '92f1a8c3d4e5') or None if unavailable.
    """
    candidates = [
        Path("alembic.ini"),
        Path(__file__).resolve().parent.parent.parent / "alembic.ini",
    ]
    for candidate in candidates:
        if candidate.is_file():
            try:
                cfg = Config(str(candidate))
                script = ScriptDirectory.from_config(cfg)
                return script.get_current_head()
            except Exception as exc:
                logger.warning(
                    "admin.resolve_expected_head_failed",
                    error=type(exc).__name__,
                )
    return None


@router.get(
    "/database/migration-status",
    response_model=MigrationStatusResponse,
    status_code=status.HTTP_200_OK,
    summary="Inspect database Alembic migration revision",
    description=(
        "Returns the current database Alembic revision and expected head revision. "
        "Strictly requires a verified administrator JWT. Never executes migrations or exposes credentials."
    ),
)
def get_migration_status(
    admin_ctx: AuthenticatedContext = Depends(require_admin_jwt),
    db: Session = Depends(get_db),
) -> MigrationStatusResponse:
    """Check Alembic version table without shell access or destructive actions."""
    expected_head = resolve_expected_head_revision()
    current_rev: Optional[str] = None
    connected = False

    try:
        inspector = inspect(db.bind)
        table_names = inspector.get_table_names()
        connected = True

        if "alembic_version" in table_names:
            row = db.execute(text("SELECT version_num FROM alembic_version LIMIT 1")).first()
            if row and row[0]:
                current_rev = str(row[0]).strip()
    except Exception as exc:
        logger.warning(
            "admin.query_alembic_version_error",
            error=type(exc).__name__,
        )
        connected = False

    # Pending if disconnected, or if revision differs from expected head revision
    if not connected or not expected_head:
        pending = True
    else:
        pending = current_rev != expected_head

    return MigrationStatusResponse(
        database_connected=connected,
        current_revision=current_rev,
        expected_head_revision=expected_head,
        migration_pending=pending,
    )
