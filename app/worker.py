"""Background worker for scheduled Zoho Recruit synchronization.

Supports:
  - Periodic execution loop (default every 3600s / 1 hour).
  - Single-pass execution (--once) for serverless cron jobs (e.g. Render Cron Jobs).
  - Concurrency safety with tenant-level locking and stale-job recovery.
  - Safe credential handling and structured logging.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.integrations.zoho.client import ZohoRecruitClient
from app.integrations.zoho.service import ZohoService
from app.persistence.database import get_db_context
from app.persistence.models import Tenant, ZohoIntegration

logger = get_logger("app.worker")


def sync_tenant(
    db: Session,
    integration: ZohoIntegration,
    settings: Any,
) -> dict[str, Any]:
    """Execute scheduled synchronization for a single active tenant integration."""
    tenant_id = integration.tenant_id
    dc = integration.data_center or settings.zoho_data_center or "com"

    client = ZohoRecruitClient(
        client_id=settings.zoho_client_id or "dummy_client_id",
        client_secret=settings.zoho_client_secret or "dummy_client_secret",
        redirect_uri=settings.zoho_redirect_uri or "https://api.example.com/v2/integrations/zoho/callback",
        data_center=dc,
    )

    try:
        sync_job = ZohoService.execute_sync(
            db,
            tenant_id=tenant_id,
            client=client,
            user_id=None,
        )
        return {
            "tenant_id": tenant_id,
            "status": sync_job.status,
            "records_fetched": sync_job.records_fetched,
            "records_upserted": sync_job.records_upserted,
        }
    except Exception as exc:
        logger.error(
            "worker.tenant_sync_failed",
            tenant_id=tenant_id,
            error_type=type(exc).__name__,
        )
        return {
            "tenant_id": tenant_id,
            "status": "failed",
            "error": str(exc),
        }


def run_sync_cycle() -> list[dict[str, Any]]:
    """Scan all active Zoho integrations and run synchronization for each."""
    import os
    if os.environ.get("CY_SYNC_WORKER_ENABLED", "true").lower() in ("false", "0", "disabled"):
        logger.info("worker.sync_disabled_by_environment", reason="CY_SYNC_WORKER_ENABLED is disabled")
        return []

    settings = get_settings()
    results: list[dict[str, Any]] = []

    with get_db_context() as db:
        integrations = (
            db.query(ZohoIntegration)
            .filter(ZohoIntegration.status == "active")
            .all()
        )
        active_count = len(integrations)
        logger.info("worker.sync_cycle_started", active_integrations=active_count)

        for integration in integrations:
            res = sync_tenant(db, integration, settings)
            results.append(res)

    logger.info("worker.sync_cycle_completed", processed_count=len(results))
    return results


def main() -> None:
    """CLI entry point for the background synchronization worker."""
    configure_logging()
    parser = argparse.ArgumentParser(description="Channel Yield Analytics Background Sync Worker")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run synchronization once across all active tenants and exit (for cron jobs)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=3600,
        help="Polling interval in seconds between cycles (default 3600s / 1 hour)",
    )
    args = parser.parse_args()

    logger.info(
        "worker.started",
        mode="once" if args.once else "daemon",
        interval_seconds=args.interval if not args.once else None,
    )

    if args.once:
        run_sync_cycle()
        sys.exit(0)

    while True:
        try:
            run_sync_cycle()
        except KeyboardInterrupt:
            logger.info("worker.shutdown_requested")
            break
        except Exception as exc:
            logger.error("worker.cycle_exception", error=str(exc))

        time.sleep(args.interval)


if __name__ == "__main__":
    main()
