"""Zoho Recruit MCP tools implementation.

Provides read-only MCP tools for querying Zoho Recruit data directly through
the existing ZohoRecruitClient while enforcing:
  1. Strict module allowlisting (JobOpenings, Candidates, Applications only).
  2. Bounded pagination (default 20, max 100).
  3. Candidate privacy protection (stripping PII such as phone, address, and salary).
  4. Auto-refresh and secure database persistence of expired OAuth access tokens.
  5. Server-side tenant resolution (caller-supplied tenant IDs are strictly rejected).
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.analytics.adapter import compute_live_channel_yield
from app.core.config import get_settings
from app.core.exceptions import (
    ChannelYieldError,
    IntegrationError,
    NotFoundError,
    OAuthError,
    RateLimitError,
    ValidationError,
)
from app.core.logging import get_logger
from app.integrations.zoho.client import ZohoRecruitClient
from app.integrations.zoho.crypto import decrypt_token, encrypt_token
from app.integrations.zoho.mapper import map_zoho_application, map_zoho_job_opening
from app.integrations.zoho.service import ZohoService
from app.persistence.database import get_db_context
from app.persistence.models import ZohoIntegration

logger = get_logger(__name__)

# Strict allowlist of Zoho Recruit API modules permitted for read operations
ALLOWED_MODULES = frozenset({"JobOpenings", "Candidates", "Applications"})

# Bounded pagination defaults and limits
DEFAULT_PER_PAGE = 20
MAX_PER_PAGE = 100


class ZohoRecruitMCPHandler:
    """Handles MCP tool invocations against Zoho Recruit API with tenant isolation."""

    def __init__(self, tenant_id: Optional[str] = None) -> None:
        """Initialize handler with trusted server-side tenant configuration.

        If tenant_id is not passed explicitly, it is read from the CY_MCP_TENANT_ID
        environment variable. It is NEVER taken from caller/agent inputs.
        """
        self._tenant_id = (tenant_id or os.getenv("CY_MCP_TENANT_ID", "")).strip()

    @property
    def tenant_id(self) -> str:
        return self._tenant_id

    def _get_authenticated_client(
        self, db: Session
    ) -> tuple[ZohoRecruitClient, str]:
        """Resolve tenant integration, check token expiry, refresh if needed, and return client."""
        if not self._tenant_id:
            raise ValidationError(
                "MCP server has no configured tenant ID. "
                "Set the CY_MCP_TENANT_ID environment variable on the server."
            )

        integration = ZohoService.get_integration(db, self._tenant_id)
        if not integration or integration.status != "active":
            raise NotFoundError(
                f"No active Zoho Recruit integration found for tenant '{self._tenant_id}'."
            )

        settings = get_settings()
        now_utc = datetime.now(timezone.utc)
        token_exp = integration.token_expires_at
        if token_exp and token_exp.tzinfo is None:
            token_exp = token_exp.replace(tzinfo=timezone.utc)

        access_token = decrypt_token(integration.encrypted_access_token)

        # Refresh token if missing or expiring within 60 seconds
        if not access_token or (token_exp and token_exp <= now_utc + timedelta(seconds=60)):
            refresh_token = decrypt_token(integration.encrypted_refresh_token)
            if not refresh_token:
                integration.status = "token_expired"
                db.commit()
                raise OAuthError("Zoho refresh token is missing; re-authorization is required.")

            client = ZohoRecruitClient(
                client_id=settings.zoho_client_id,
                client_secret=settings.zoho_client_secret,
                redirect_uri=settings.zoho_redirect_uri,
                data_center=integration.data_center or "com",
            )
            logger.info("mcp.zoho_refreshing_access_token", tenant_id=self._tenant_id)
            refreshed = client.refresh_access_token(refresh_token)
            access_token = refreshed["access_token"]
            expires_in = int(refreshed.get("expires_in", 3600))

            integration.encrypted_access_token = encrypt_token(access_token)
            integration.token_expires_at = now_utc + timedelta(seconds=expires_in)
            db.commit()
        else:
            client = ZohoRecruitClient(
                client_id=settings.zoho_client_id,
                client_secret=settings.zoho_client_secret,
                redirect_uri=settings.zoho_redirect_uri,
                data_center=integration.data_center or "com",
            )

        return client, access_token

    def _fetch_records(
        self, module: str, page: int, per_page: int
    ) -> dict[str, Any]:
        """Fetch raw records from Zoho Recruit enforcing the module allowlist."""
        if module not in ALLOWED_MODULES:
            raise ValidationError(
                f"Module '{module}' is not permitted. Allowed modules: {sorted(ALLOWED_MODULES)}"
            )

        page_clean = max(1, int(page))
        per_page_clean = max(1, min(int(per_page), MAX_PER_PAGE))

        with get_db_context() as db:
            client, access_token = self._get_authenticated_client(db)
            raw_response = client.get_module_records(
                module=module,
                access_token=access_token,
                page=page_clean,
                per_page=per_page_clean,
            )

        return raw_response

    # ─── Tool 1: list_job_openings ────────────────────────────────────────────

    def list_job_openings(
        self,
        page: int = 1,
        per_page: int = DEFAULT_PER_PAGE,
        status: Optional[str] = None,
    ) -> dict[str, Any]:
        """Retrieve paginated job openings from Zoho Recruit.

        Args:
            page: 1-indexed page number (default 1).
            per_page: Number of records per page (default 20, max 100).
            status: Optional status filter (e.g., 'In-progress', 'Filled', 'Cancelled').
        """
        try:
            raw = self._fetch_records("JobOpenings", page, per_page)
            data = raw.get("data", []) or []
            info = raw.get("info", {}) or {}

            items = []
            for rec in data:
                rec_status = str(
                    rec.get("Job_Opening_Status") or rec.get("status") or "open"
                ).strip()
                if status and rec_status.lower() != status.lower().strip():
                    continue

                items.append({
                    "id": str(rec.get("id") or ""),
                    "job_opening_id": str(rec.get("Job_Opening_ID") or rec.get("id") or ""),
                    "posting_title": str(rec.get("Posting_Title") or rec.get("Job_Opening_Name") or "Untitled"),
                    "status": rec_status,
                    "department": rec.get("Department"),
                    "number_of_positions": rec.get("Number_of_Positions"),
                    "city": rec.get("City"),
                    "state": rec.get("State"),
                    "country": rec.get("Country"),
                    "opened_date": rec.get("Date_Opened"),
                    "target_date": rec.get("Target_Date"),
                })

            return {
                "module": "JobOpenings",
                "page": max(1, int(page)),
                "per_page": max(1, min(int(per_page), MAX_PER_PAGE)),
                "count": len(items),
                "has_more": bool(info.get("more_records", False)),
                "job_openings": items,
            }
        except RateLimitError as exc:
            return {"error": "rate_limited", "message": str(exc.message)}
        except (OAuthError, NotFoundError, ValidationError, IntegrationError) as exc:
            return {"error": exc.code if hasattr(exc, "code") else "error", "message": str(exc.message if hasattr(exc, "message") else exc)}
        except Exception as exc:
            logger.error("mcp.list_job_openings_unexpected_error", error=type(exc).__name__)
            return {"error": "integration_error", "message": "Failed to retrieve job openings from Zoho Recruit."}

    # ─── Tool 2: list_candidates ──────────────────────────────────────────────

    def list_candidates(
        self,
        page: int = 1,
        per_page: int = DEFAULT_PER_PAGE,
        source: Optional[str] = None,
    ) -> dict[str, Any]:
        """Retrieve paginated candidates from Zoho Recruit with privacy minimization.

        Data Minimization:
            Excludes home addresses, phone numbers, mobile numbers, salary numbers,
            and private notes. Returns only safe recruitment attributes necessary
            for channel yield and pipeline analysis.

        Args:
            page: 1-indexed page number (default 1).
            per_page: Number of records per page (default 20, max 100).
            source: Optional source filter (e.g., 'LinkedIn', 'Referral', 'Career Site').
        """
        try:
            raw = self._fetch_records("Candidates", page, per_page)
            data = raw.get("data", []) or []
            info = raw.get("info", {}) or {}

            items = []
            for rec in data:
                lead_source = str(rec.get("Source") or rec.get("Lead_Source") or "Unknown").strip()
                if source and lead_source.lower() != source.lower().strip():
                    continue

                # Field minimization: safe metadata only; personal contact & financial data excluded
                items.append({
                    "id": str(rec.get("id") or ""),
                    "candidate_status": rec.get("Candidate_Status") or "Active",
                    "source": lead_source,
                    "highest_qualification": rec.get("Highest_Qualification_Held"),
                    "current_job_title": rec.get("Current_Job_Title"),
                    "experience_in_years": rec.get("Experience_in_Years"),
                    "city": rec.get("City"),
                    "created_time": rec.get("Created_Time"),
                })

            return {
                "module": "Candidates",
                "page": max(1, int(page)),
                "per_page": max(1, min(int(per_page), MAX_PER_PAGE)),
                "count": len(items),
                "has_more": bool(info.get("more_records", False)),
                "candidates": items,
            }
        except RateLimitError as exc:
            return {"error": "rate_limited", "message": str(exc.message)}
        except (OAuthError, NotFoundError, ValidationError, IntegrationError) as exc:
            return {"error": exc.code if hasattr(exc, "code") else "error", "message": str(exc.message if hasattr(exc, "message") else exc)}
        except Exception as exc:
            logger.error("mcp.list_candidates_unexpected_error", error=type(exc).__name__)
            return {"error": "integration_error", "message": "Failed to retrieve candidates from Zoho Recruit."}

    # ─── Tool 3: list_applications ────────────────────────────────────────────

    def list_applications(
        self,
        page: int = 1,
        per_page: int = DEFAULT_PER_PAGE,
        job_opening_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> dict[str, Any]:
        """Retrieve paginated job applications from Zoho Recruit.

        Args:
            page: 1-indexed page number (default 1).
            per_page: Number of records per page (default 20, max 100).
            job_opening_id: Optional job opening identifier filter.
            status: Optional application stage or status filter.
        """
        try:
            raw = self._fetch_records("Applications", page, per_page)
            data = raw.get("data", []) or []
            info = raw.get("info", {}) or {}

            items = []
            for rec in data:
                # Extract related job opening ID
                job_obj = rec.get("Job_Opening_ID") or {}
                rec_job_id = str(job_obj.get("id") if isinstance(job_obj, dict) else job_obj or "")
                if job_opening_id and rec_job_id != str(job_opening_id).strip():
                    continue

                app_status = str(
                    rec.get("Application_Status")
                    or rec.get("Stage")
                    or rec.get("status")
                    or "Applied"
                ).strip()
                if status and app_status.lower() != status.lower().strip():
                    continue

                cand_obj = rec.get("Candidate_ID") or {}
                cand_id = str(cand_obj.get("id") if isinstance(cand_obj, dict) else cand_obj or "")

                items.append({
                    "id": str(rec.get("id") or ""),
                    "job_opening_id": rec_job_id,
                    "job_title": rec.get("Posting_Title") or (job_obj.get("Posting_Title") if isinstance(job_obj, dict) else None),
                    "candidate_id": cand_id,
                    "status": app_status,
                    "rating": rec.get("Rating"),
                    "source": rec.get("Source") or rec.get("Lead_Source"),
                    "created_time": rec.get("Created_Time"),
                })

            return {
                "module": "Applications",
                "page": max(1, int(page)),
                "per_page": max(1, min(int(per_page), MAX_PER_PAGE)),
                "count": len(items),
                "has_more": bool(info.get("more_records", False)),
                "applications": items,
            }
        except RateLimitError as exc:
            return {"error": "rate_limited", "message": str(exc.message)}
        except (OAuthError, NotFoundError, ValidationError, IntegrationError) as exc:
            return {"error": exc.code if hasattr(exc, "code") else "error", "message": str(exc.message if hasattr(exc, "message") else exc)}
        except Exception as exc:
            logger.error("mcp.list_applications_unexpected_error", error=type(exc).__name__)
            return {"error": "integration_error", "message": "Failed to retrieve applications from Zoho Recruit."}

    # ─── Tool 4: check_zoho_connection ───────────────────────────────────────

    def check_zoho_connection(self) -> dict[str, Any]:
        """Verify whether this user's organization has an active Zoho Recruit connection."""
        if not self._tenant_id:
            return {
                "connected": False,
                "status": "unconfigured",
                "message": "No tenant ID is associated with this session. Please connect your Zoho Recruit account.",
                "onboarding_action": "Authenticate or provide a valid user capability token.",
            }

        try:
            with get_db_context() as db:
                integration = ZohoService.get_integration(db, self._tenant_id)
                if not integration or integration.status != "active":
                    return {
                        "connected": False,
                        "status": integration.status if integration else "pending_auth",
                        "tenant_id": self._tenant_id,
                        "message": f"No active Zoho Recruit integration found for tenant '{self._tenant_id}'.",
                        "onboarding_url": "/v2/integrations/zoho/authorize",
                    }
                return {
                    "connected": True,
                    "status": "active",
                    "tenant_id": self._tenant_id,
                    "data_center": integration.data_center,
                    "zoho_org_id": integration.zoho_org_id,
                    "last_sync_at": integration.last_sync_at.isoformat() if integration.last_sync_at else None,
                }
        except Exception as exc:
            logger.error("mcp.check_connection_error", error=type(exc).__name__)
            return {"error": "database_error", "message": "Failed to check integration status."}

    # ─── Tool 5: compute_channel_yield (Live In-Memory Analytics) ─────────────

    def compute_channel_yield(
        self,
        cohort_start: str,
        cohort_end: str,
        as_of_date: Optional[str] = None,
        observation_days: int = 90,
        requisition_id: Optional[str] = None,
        min_sample_size: int = 5,
    ) -> dict[str, Any]:
        """Retrieve live Zoho Recruit records, map in memory, and calculate deterministic channel yield.

        Zero-Data-Persistence Guarantee:
            Raw job openings, candidate profiles, and applications are never saved to database tables,
            CSV files, temporary SQLite files, or persistent caches.
        """
        if not self._tenant_id:
            return {
                "status": "error",
                "error": "unconfigured_tenant",
                "message": "Session has no configured tenant ID. Please connect your Zoho Recruit account first.",
            }

        try:
            # 1. Obtain authenticated Zoho client and access token
            with get_db_context() as db:
                client, access_token = self._get_authenticated_client(db)

            # 2. Fetch live records across required modules (bounded to reasonable pagination limits)
            raw_jobs = client.fetch_all_module_records("JobOpenings", access_token, max_pages=10)
            raw_candidates = client.fetch_all_module_records("Candidates", access_token, max_pages=20)
            raw_applications = client.fetch_all_module_records("Applications", access_token, max_pages=20)

            # 3. Construct candidate source attribution map in memory
            candidate_source_map: dict[str, str] = {}
            for cand in raw_candidates:
                cand_id = str(cand.get("id") or "")
                if cand_id:
                    src = cand.get("Source") or cand.get("Lead_Source") or "unknown"
                    candidate_source_map[cand_id] = str(src).strip()

            # 4. Map records in memory using canonical schema mappers (transient dictionaries)
            canonical_jobs = [
                map_zoho_job_opening(rec, self._tenant_id, "live_mcp_run")
                for rec in raw_jobs
            ]
            canonical_apps = [
                map_zoho_application(rec, candidate_source_map, self._tenant_id, "live_mcp_run")
                for rec in raw_applications
            ]

            # 5. Execute in-memory yield analytics engine
            calculation_as_of = as_of_date or datetime.now(timezone.utc).date().isoformat()
            metrics = compute_live_channel_yield(
                tenant_id=self._tenant_id,
                requisitions=canonical_jobs,
                applications=canonical_apps,
                stage_events=[],  # Explicitly empty: no fabricated events
                costs=[],         # Explicitly empty: no fabricated costs
                cohort_start=cohort_start,
                cohort_end=cohort_end,
                as_of_date=calculation_as_of,
                observation_days=max(0, int(observation_days)),
                requisition_id=requisition_id,
                min_sample_size=max(1, int(min_sample_size)) if min_sample_size is not None else None,
            )

            # 6. Generate deterministic, evidence-based recommendations
            recommendations: list[str] = []
            ranked = metrics.get("ranked_channels", [])
            summary = metrics.get("summary", {})
            total_apps = summary.get("applications_in_cohort", 0)
            confirmed_hires = summary.get("confirmed_mature_joins", 0)

            if total_apps == 0:
                recommendations.append(
                    f"No applications were submitted during the selected cohort window ({cohort_start} to {cohort_end}). "
                    "Verify your date filter or confirm recent candidate activity in Zoho Recruit."
                )
            elif confirmed_hires == 0:
                recommendations.append(
                    f"Across {total_apps} application(s) in this cohort, 0 confirmed hires have matured within the "
                    f"{observation_days}-day observation window. Check whether candidates are still active in pipeline or increase the cohort range."
                )
            elif ranked:
                top_channel = ranked[0]
                recommendations.append(
                    f"Top Performing Channel: '{top_channel['channel']}' achieved the highest mature yield ({top_channel['yield_percent']}%) "
                    f"with {top_channel['confirmed_joins']} confirmed hire(s) out of {top_channel['mature_eligible_applications']} mature candidate(s)."
                )
                if len(ranked) > 1:
                    lowest_channel = ranked[-1]
                    if lowest_channel["yield_percent"] < top_channel["yield_percent"]:
                        recommendations.append(
                            f"Channel Review: '{lowest_channel['channel']}' demonstrated a mature yield of {lowest_channel['yield_percent']}% "
                            f"({lowest_channel['confirmed_joins']} hire(s) / {lowest_channel['mature_eligible_applications']} mature apps). "
                            "Consider evaluating channel screening criteria or reallocating recruitment focus."
                        )
            else:
                recommendations.append(
                    f"Hires exist ({confirmed_hires} confirmed), but individual channel volume did not meet the "
                    f"configured ranking threshold (min_sample_size = {min_sample_size}). "
                    "Lower min_sample_size or expand cohort range to generate authoritative channel rankings."
                )

            data_qual = metrics.get("data_quality", {})
            unknown_apps = data_qual.get("unknown_source_applications", 0)
            if unknown_apps > 0:
                recommendations.append(
                    f"Data Quality Notice: {unknown_apps} application(s) lacked channel attribution and were excluded "
                    "from named channel yield calculations. Ensure recruiters capture Lead Source in Zoho Recruit."
                )

            limitations = [
                "Recruitment costs are not tracked in Zoho Recruit; cost-per-hire and ROI metrics require external cost inputs.",
                "Granular stage-to-stage transition timelines are not provided by Zoho module records; funnel conversion requires persistent stage events.",
            ]

            return {
                "status": "completed",
                "analysis_period": {
                    "cohort_start": cohort_start,
                    "cohort_end": cohort_end,
                    "as_of_date": calculation_as_of,
                    "observation_days": observation_days,
                },
                "summary": summary,
                "ranked_channels": ranked,
                "channel_metrics": metrics.get("channel_metrics", []),
                "historical_funnel": {
                    "available": False,
                    "stages": None,
                    "reason": "Zoho Recruit module APIs return current status only; historical stage transitions require timeline logging.",
                },
                "recommendations": recommendations,
                "data_quality": data_qual,
                "limitations": limitations,
            }

        except RateLimitError as exc:
            return {"status": "error", "error": "rate_limited", "message": str(exc.message)}
        except (OAuthError, NotFoundError, ValidationError, IntegrationError) as exc:
            return {
                "status": "error",
                "error": exc.code if hasattr(exc, "code") else "error",
                "message": str(exc.message if hasattr(exc, "message") else exc),
            }
        except Exception as exc:
            logger.error("mcp.compute_channel_yield_unexpected_error", error=type(exc).__name__)
            return {"status": "error", "error": "calculation_error", "message": "Failed to calculate channel yield from Zoho Recruit data."}
