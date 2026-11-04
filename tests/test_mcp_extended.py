"""Comprehensive unit and integration tests for extended MCP tools, selection history,
source attribution, analytics engine, and system endpoints.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.analytics.attribution import normalize_channel, resolve_application_attribution
from app.analytics.engine import RecruitmentAnalyticsEngine
from app.integrations.zoho.selection_history import SelectionHistoryService
from app.main import app
from app.persistence.database import get_db_context
from app.persistence.models import (
    Application,
    ApplicationStageEvent,
    Candidate,
    JobOpening,
    RecruitmentActivity,
    RecruitmentCost,
    Tenant,
    ZohoIntegration,
)
from mcp_server.server import (
    MCP_TOOLS,
    create_user_connection_token,
    verify_user_connection_token,
)
from mcp_server.zoho_recruit import ZohoRecruitMCPHandler


# ─── System Probes Tests ──────────────────────────────────────────────────────


def test_system_health_ready_and_version_probes():
    """Verify /health, /ready, and /version endpoints conform to operational contracts."""
    client = TestClient(app)

    # 1. /health
    res_health = client.get("/health")
    assert res_health.status_code == 200
    data_health = res_health.json()
    assert data_health["status"] == "ok"
    assert "version" in data_health
    assert "service" in data_health

    # 2. /ready
    res_ready = client.get("/ready")
    assert res_ready.status_code == 200
    data_ready = res_ready.json()
    assert data_ready["status"] == "ready"
    assert data_ready["database"] == "connected"

    # 3. /version
    res_ver = client.get("/version")
    assert res_ver.status_code == 200
    data_ver = res_ver.json()
    assert data_ver["version"] == "1.0.0"
    assert data_ver["mcp_version"] == "2024-11-05"


# ─── Selection History Service Tests ──────────────────────────────────────────


def test_selection_history_service_explicit_unavailable():
    """Verify selection history returns unavailable status when Zoho yields no records."""
    mock_client = MagicMock()
    mock_client.get_application_transition_details.return_value = []
    mock_client.get_application_hiring_pipeline.return_value = []
    mock_client.get_record_timeline.return_value = []
    mock_client.fetch_all_module_records.return_value = []

    res = SelectionHistoryService.get_selection_history(
        mock_client,
        access_token="test_token",
        application_id="APP-999",
    )

    assert res["status"] == "success"
    assert res["history_available"] is False
    assert res["history_status"] == "unavailable"
    assert res["stage_transitions"] == []
    assert "Zoho Recruit did not expose historical stage transition records" in res["history_note"]


def test_selection_history_service_with_transitions():
    """Verify selection history accurately parses transitions, durations, and outcomes."""
    mock_client = MagicMock()
    mock_client.get_application_transition_details.return_value = [
        {
            "id": "trans-1",
            "From_Stage": "Applied",
            "To_Stage": "Screening",
            "From_Status": "Submitted",
            "To_Status": "In Review",
            "Modified_Time": "2026-01-10T10:00:00Z",
            "Created_Time": "2026-01-05T10:00:00Z",
            "Comments": "Candidate resume passed initial filter",
            "Modified_By": {"name": "HR Reviewer", "id": "user-10"},
        },
        {
            "id": "trans-2",
            "From_Stage": "Screening",
            "To_Stage": "Interview",
            "From_Status": "In Review",
            "To_Status": "Scheduled",
            "Modified_Time": "2026-01-15T14:00:00Z",
            "Interview_Outcome": "Passed technical test",
            "Modified_By": {"name": "Engineering Lead"},
        },
        {
            "id": "trans-3",
            "From_Stage": "Interview",
            "To_Stage": "Hired",
            "From_Status": "Scheduled",
            "To_Status": "Joined",
            "Modified_Time": "2026-01-25T09:00:00Z",
            "Offer_Status": "Accepted",
            "Offer_Date": "2026-01-20",
            "Hiring_Outcome": "Hired",
        },
    ]
    mock_client.get_application_hiring_pipeline.return_value = []
    mock_client.get_record_timeline.return_value = []

    res = SelectionHistoryService.get_selection_history(
        mock_client,
        access_token="test_token",
        application_id="APP-100",
        candidate_id="CAND-50",
        requisition_id="REQ-10",
    )

    assert res["status"] == "success"
    assert res["history_available"] is True
    assert res["history_status"] == "complete"
    assert res["total_transitions"] == 3
    assert len(res["stage_transitions"]) == 3

    t1 = res["stage_transitions"][0]
    assert t1["previous_stage"] == "Applied"
    assert t1["new_stage"] == "Screening"
    assert t1["duration_days_in_previous_stage"] == 5.0
    assert t1["performed_by"] == "HR Reviewer"

    t3 = res["stage_transitions"][2]
    assert t3["new_stage"] == "Hired"
    assert t3["offer_status"] == "Accepted"
    assert t3["hiring_outcome"] == "Hired"


# ─── Source Attribution Engine Tests ──────────────────────────────────────────


def test_source_attribution_engine():
    """Verify application-first attribution, candidate fallback, and normalization."""
    # 1. Direct application source
    attr1 = resolve_application_attribution(
        {"Lead_Source": "linkedin job post", "Candidate_ID": {"id": "C-1"}},
        candidate_source_map={"C-1": "Agency"},
    )
    assert attr1["source_channel"] == "LinkedIn"
    assert attr1["attribution_method"] == "application_field"
    assert attr1["attribution_confidence"] == 1.0

    # 2. Candidate fallback when app source is missing
    attr2 = resolve_application_attribution(
        {"Lead_Source": "", "Candidate_ID": {"id": "C-2"}},
        candidate_source_map={"C-2": "Employee Referral"},
    )
    assert attr2["source_channel"] == "Employee Referral"
    assert attr2["attribution_method"] == "candidate_fallback"
    assert attr2["attribution_confidence"] > 0.0

    # 3. Unknown source
    attr3 = resolve_application_attribution(
        {"Lead_Source": None, "Candidate_ID": {"id": "C-3"}},
        candidate_source_map={"C-3": ""},
    )
    assert attr3["source_channel"] == "unknown"
    assert attr3["attribution_method"] == "unattributed"
    assert attr3["attribution_confidence"] == 0.0

    # 4. Canonical normalization variations
    chan1, _, _ = normalize_channel("naukri.com")
    assert chan1 == "Job Portals"
    chan2, _, _ = normalize_channel("Company Careers Page")
    assert chan2 == "Company Careers Website"
    chan3, _, _ = normalize_channel("Staffing Partner")
    assert chan3 == "Recruitment Agencies"


# ─── Recruitment Analytics Engine Tests ───────────────────────────────────────


def test_recruitment_analytics_engine():
    """Verify analytics engine calculates mature yield, conversion, time-to-hire, and recommendations."""
    applications = [
        {
            "application_id": "APP-1",
            "source_channel": "LinkedIn",
            "applied_at": "2026-01-01T00:00:00Z",
            "application_outcome": "joined",
            "joined_at": "2026-01-31T00:00:00Z",
        },
        {
            "application_id": "APP-2",
            "source_channel": "LinkedIn",
            "applied_at": "2026-01-05T00:00:00Z",
            "application_outcome": "rejected",
            "rejection_stage": "Screening",
            "rejection_reason": "Skill mismatch",
        },
        {
            "application_id": "APP-3",
            "source_channel": "Employee Referral",
            "applied_at": "2026-01-10T00:00:00Z",
            "application_outcome": "joined",
            "joined_at": "2026-01-25T00:00:00Z",
        },
    ]

    costs = [
        {"source_channel": "LinkedIn", "cost_amount": 2000.0},
        {"source_channel": "Employee Referral", "cost_amount": 1000.0},
    ]

    report = RecruitmentAnalyticsEngine.analyze(
        requisitions=[{"requisition_id": "REQ-1", "job_title": "Engineer"}],
        applications=applications,
        stage_events=[],
        costs=costs,
        cohort_start="2026-01-01",
        cohort_end="2026-06-30",
        as_of_date="2026-06-30",
        min_sample_size=1,
    )

    assert report["summary"]["total_applications"] == 3
    assert report["summary"]["overall_yield_percent"] == 66.67

    # Check channels
    channels = {c["channel"]: c for c in report["channel_yields"]}
    assert "LinkedIn" in channels
    assert "Employee Referral" in channels

    li = channels["LinkedIn"]
    assert li["applications"] == 2
    assert li["confirmed_joins"] == 1
    assert li["yield_percent"] == 50.0

    ref = channels["Employee Referral"]
    assert ref["applications"] == 1
    assert ref["confirmed_joins"] == 1
    assert ref["yield_percent"] == 100.0

    # Check ranked channels
    ranked = {r["channel"]: r for r in report["ranked_channels"]}
    assert "LinkedIn" in ranked
    assert ranked["LinkedIn"]["cost_per_hire"] == 2000.0
    assert ranked["LinkedIn"]["average_time_to_hire_days"] == 30.0
    assert ranked["Employee Referral"]["cost_per_hire"] == 1000.0
    assert ranked["Employee Referral"]["average_time_to_hire_days"] == 15.0

    # Recommendations must contain evidence-based items
    assert len(report["recommendations"]["actionable_recommendations"]) > 0


# ─── MCP Tools Catalog Verification ───────────────────────────────────────────


def test_mcp_tools_catalog_completeness():
    """Verify that all 16 required read-only tools are defined in the MCP catalog."""
    expected_tools = {
        "test_zoho_connection",
        "check_zoho_connection",
        "connect_zoho_account",
        "get_recruitment_modules",
        "get_recruitment_fields",
        "get_sync_status",
        "list_job_openings",
        "list_candidates",
        "list_applications",
        "get_candidate_details",
        "get_application_details",
        "get_selection_history",
        "get_candidate_history",
        "get_recruitment_funnel",
        "compute_channel_yield",
        "get_channel_conversion",
        "get_channel_costs",
        "get_channel_recommendations",
    }
    tool_names = {t["name"] for t in MCP_TOOLS}
    assert expected_tools.issubset(tool_names)


# ─── MCP Server Token & Streamable HTTP Tests ─────────────────────────────────


def test_mcp_connection_token_cryptography():
    """Verify HMAC-SHA256 connection token generation and tampering rejection."""
    secret = "a" * 64
    token = create_user_connection_token(
        tenant_id="tenant-xyz",
        user_id="user-123",
        secret=secret,
        expire_hours=24,
    )
    assert token.startswith("cyct_")

    payload = verify_user_connection_token(token, secret=secret)
    assert payload is not None
    assert payload["tid"] == "tenant-xyz"
    assert payload["sub"] == "user-123"

    # Tampered token signature must be rejected
    tampered_token = token[:-4] + "ffff"
    assert verify_user_connection_token(tampered_token, secret=secret) is None

    # Unknown format must be rejected
    assert verify_user_connection_token("invalid_token_string", secret=secret) is None


# ─── Worker Tests ─────────────────────────────────────────────────────────────


def test_worker_sync_cycle():
    """Verify background worker scans active integrations and handles sync cycle."""
    from app.worker import run_sync_cycle

    with patch("app.worker.sync_tenant") as mock_sync:
        mock_sync.return_value = {
            "tenant_id": "test-tenant-1",
            "status": "completed",
            "records_fetched": 10,
            "records_upserted": 10,
        }
        results = run_sync_cycle()
        assert isinstance(results, list)
