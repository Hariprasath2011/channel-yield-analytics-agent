"""Zoho Recruit integration orchestrator and sync service."""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, NotFoundError, OAuthError
from app.core.logging import get_logger
from app.ingestion.service import IngestionService
from app.integrations.zoho.client import ZohoRecruitClient
from app.integrations.zoho.crypto import decrypt_token, encrypt_token
from app.integrations.zoho.mapper import map_zoho_application, map_zoho_job_opening
from app.persistence.models import AuditEvent, OAuthState, SyncJob, Tenant, ZohoIntegration

logger = get_logger(__name__)


class ZohoService:
    """Manages Zoho OAuth credentials, CSRF state verification, and synchronized ingestion."""

    @staticmethod
    def get_integration(db: Session, tenant_id: str) -> Optional[ZohoIntegration]:
        """Fetch tenant's Zoho integration record."""
        return (
            db.query(ZohoIntegration)
            .filter(ZohoIntegration.tenant_id == tenant_id)
            .first()
        )

    @classmethod
    def create_oauth_state(cls, db: Session, tenant_id: str) -> str:
        """Generate a cryptographically secure one-time CSRF token for OAuth handshake."""
        state_token = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=15)
        state_obj = OAuthState(
            tenant_id=tenant_id,
            state_token=state_token,
            provider="zoho_recruit",
            used=False,
            expires_at=expires_at,
        )
        db.add(state_obj)
        db.commit()
        return state_token

    @classmethod
    def exchange_and_save_tokens(
        cls,
        db: Session,
        *,
        tenant_id: str,
        state_token: str,
        code: str,
        client: ZohoRecruitClient,
    ) -> ZohoIntegration:
        """Validate CSRF state, exchange auth code for tokens, and store Fernet-encrypted credentials."""
        now_utc = datetime.now(timezone.utc)
        state_obj = (
            db.query(OAuthState)
            .filter(
                OAuthState.tenant_id == tenant_id,
                OAuthState.state_token == state_token,
                OAuthState.provider == "zoho_recruit",
                OAuthState.used == False,  # noqa: E712
            )
            .first()
        )
        if not state_obj:
            logger.warning("zoho.oauth_state_not_found", tenant_id=tenant_id)
            raise OAuthError("Invalid or expired OAuth state parameter.")

        state_exp = state_obj.expires_at
        if state_exp.tzinfo is None:
            state_exp = state_exp.replace(tzinfo=timezone.utc)
        if state_exp < now_utc:
            logger.warning("zoho.oauth_state_expired", tenant_id=tenant_id)
            raise OAuthError("Invalid or expired OAuth state parameter.")

        state_obj.used = True

        token_data = client.exchange_code_for_tokens(code)
        access_token = token_data.get("access_token")
        refresh_token = token_data.get("refresh_token")
        expires_in = int(token_data.get("expires_in", 3600))
        token_expires_at = now_utc + timedelta(seconds=expires_in)

        integration = cls.get_integration(db, tenant_id)
        if not integration:
            integration = ZohoIntegration(
                tenant_id=tenant_id,
                data_center=client.data_center,
            )
            db.add(integration)

        integration.status = "active"
        integration.encrypted_access_token = encrypt_token(access_token)
        if refresh_token:
            integration.encrypted_refresh_token = encrypt_token(refresh_token)
        integration.token_expires_at = token_expires_at
        integration.scopes = token_data.get("scope")
        integration.zoho_org_id = token_data.get("api_domain")

        audit = AuditEvent(
            tenant_id=tenant_id,
            actor_type="user",
            event_type="zoho.oauth_connected",
            resource_type="zoho_integration",
            resource_id=integration.id,
            outcome="success",
        )
        db.add(audit)
        db.commit()
        db.refresh(integration)
        return integration

    @classmethod
    def execute_sync(
        cls,
        db: Session,
        *,
        tenant_id: str,
        client: ZohoRecruitClient,
        user_id: Optional[str] = None,
    ) -> SyncJob:
        """Run an on-demand sync from Zoho Recruit into tenant's canonical records.

        Steps:
          1. Verify active connection.
          2. Refresh token if expired.
          3. Fetch JobOpenings, Candidates, Applications.
          4. Map records to canonical schema.
          5. Ingest mapped records through IngestionService ensuring idempotency & validation.
          6. Update SyncJob and integration last_sync_at.
        """
        integration = cls.get_integration(db, tenant_id)
        if not integration or integration.status != "active":
            raise NotFoundError("Active Zoho Recruit integration")

        # Concurrency Guard & Stale Sync Recovery
        # Acquire advisory / row lock on the tenant row to serialize concurrent syncs
        db.query(Tenant).filter(Tenant.id == tenant_id).with_for_update().first()

        active_sync = (
            db.query(SyncJob)
            .filter(SyncJob.tenant_id == tenant_id, SyncJob.status == "running")
            .first()
        )
        if active_sync:
            started = active_sync.started_at or active_sync.created_at
            if started and started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            elapsed_seconds = (datetime.now(timezone.utc) - started).total_seconds() if started else 999999
            if elapsed_seconds > 1800:
                logger.warning(
                    "zoho.stale_sync_recovered",
                    tenant_id=tenant_id,
                    sync_job_id=active_sync.id,
                    elapsed_seconds=elapsed_seconds,
                )
                active_sync.status = "failed"
                active_sync.finished_at = datetime.now(timezone.utc)
                active_sync.error_summary = "Sync timed out exceeding 30-minute threshold; recovered."
                audit = AuditEvent(
                    tenant_id=tenant_id,
                    actor_id=user_id or "system",
                    actor_type="system",
                    event_type="zoho.stale_sync_recovered",
                    resource_type="sync_job",
                    resource_id=active_sync.id,
                    outcome="failure",
                )
                db.add(audit)
                db.commit()
            else:
                logger.warning(
                    "zoho.concurrency_conflict",
                    tenant_id=tenant_id,
                    running_sync_id=active_sync.id,
                )
                raise ConflictError("A synchronization job is already in progress for this tenant.")

        now_utc = datetime.now(timezone.utc)
        sync_job = SyncJob(
            tenant_id=tenant_id,
            integration_id=integration.id,
            sync_type="full",
            status="running",
            started_at=now_utc,
        )
        db.add(sync_job)
        db.commit()
        db.refresh(sync_job)

        try:
            # Check access token validity and refresh if expired
            access_token = decrypt_token(integration.encrypted_access_token)
            token_exp = integration.token_expires_at
            if token_exp and token_exp.tzinfo is None:
                token_exp = token_exp.replace(tzinfo=timezone.utc)
            if not access_token or (token_exp and token_exp <= now_utc):
                refresh_token = decrypt_token(integration.encrypted_refresh_token)
                if not refresh_token:
                    integration.status = "token_expired"
                    db.commit()
                    raise OAuthError("Zoho refresh token is missing; re-authorization required.")

                refreshed = client.refresh_access_token(refresh_token)
                access_token = refreshed["access_token"]
                integration.encrypted_access_token = encrypt_token(access_token)
                integration.token_expires_at = now_utc + timedelta(
                    seconds=int(refreshed.get("expires_in", 3600))
                )
                db.commit()

            # Fetch modules
            run_id = f"zoho-{now_utc.strftime('%Y%m%dT%H%M%S')}"
            raw_openings = client.fetch_all_module_records("JobOpenings", access_token)
            raw_candidates = client.fetch_all_module_records("Candidates", access_token)
            raw_applications = client.fetch_all_module_records("Applications", access_token)

            # Build candidate source lookup
            cand_source_map: dict[str, str] = {}
            for c in raw_candidates:
                cid = str(c.get("id") or "")
                src = c.get("Lead_Source") or c.get("Source") or "unknown"
                if cid:
                    cand_source_map[cid] = str(src)

            # Map to canonical dicts
            mapped_reqs = [
                map_zoho_job_opening(o, tenant_id, run_id) for o in raw_openings
            ]
            mapped_apps = [
                map_zoho_application(a, cand_source_map, tenant_id, run_id)
                for a in raw_applications
            ]

            # Ingest through canonical pipeline
            import io, csv

            def dicts_to_csv(rows: list[dict[str, Any]], fieldnames: list[str]) -> str:
                if not rows:
                    return ""
                buf = io.StringIO()
                writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(rows)
                return buf.getvalue()

            req_csv = dicts_to_csv(
                mapped_reqs,
                ["client_id", "requisition_id", "job_title", "status", "role_family", "source_system_id", "source_record_id", "mapping_version", "ingest_run_id"]
            )
            app_csv = dicts_to_csv(
                mapped_apps,
                ["client_id", "application_id", "requisition_id", "source_channel", "applied_at", "application_outcome", "joined_at", "source_system_id", "source_record_id", "mapping_version", "ingest_run_id"]
            )

            files: dict[str, Any] = {}
            if req_csv:
                files["requisitions"] = req_csv
            if app_csv:
                files["applications"] = app_csv

            report = IngestionService.ingest_bundle(
                db,
                tenant_id=tenant_id,
                files=files,
                user_id=user_id,
                source_system_id="zoho_recruit",
                mapping_version="zoho_v2",
                required_entities=["requisitions", "applications"],
                optional_entities=["stage_events", "costs"],
            )

            finished_now = datetime.now(timezone.utc)
            if report.get("status") in ("failed", "failed_validation"):
                sync_job.status = "failed"
                sync_job.error_summary = str(report.get("semantic_validation", {}).get("errors"))
                sync_job.finished_at = finished_now
                db.commit()
            else:
                summary = report.get("summary", {})
                sync_job.status = "completed"
                sync_job.records_fetched = len(raw_openings) + len(raw_candidates) + len(raw_applications)
                sync_job.records_upserted = summary.get("inserted", 0) + summary.get("updated", 0)
                sync_job.finished_at = finished_now
                integration.last_sync_at = finished_now
                integration.last_sync_status = "success"
                db.commit()

            return sync_job

        except Exception as exc:
            logger.error("zoho.sync_failed", tenant_id=tenant_id, error=str(exc))
            sync_job.status = "failed"
            sync_job.error_summary = str(exc)
            sync_job.finished_at = datetime.now(timezone.utc)
            db.commit()
            raise
