"""Tests for multi-tenant analytics router (/v2/analytics) and controlled agent interface (/v2/agent/query)."""
import json
from datetime import date
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth.tokens import create_access_token
from app.persistence.models import CanonicalRecord
from app.tenancy.service import create_tenant, create_user


@pytest.fixture
def analytics_seed_data(db_session: Session):
    """Seed two distinct tenants with isolated recruitment records and users."""
    # Tenant Alpha
    t_alpha = create_tenant(db_session, name="Alpha Corp", slug="alpha")
    u_alpha_analyst, _ = create_user(
        db_session,
        tenant_id=t_alpha.id,
        email="analyst@alpha.com",
        password="AlphaAnalystPass123!",
        role="analyst",
    )
    u_alpha_viewer, _ = create_user(
        db_session,
        tenant_id=t_alpha.id,
        email="viewer@alpha.com",
        password="AlphaViewerPass123!",
        role="viewer",
    )

    # Tenant Beta
    t_beta = create_tenant(db_session, name="Beta Ltd", slug="beta")
    u_beta_analyst, _ = create_user(
        db_session,
        tenant_id=t_beta.id,
        email="analyst@beta.com",
        password="BetaAnalystPass123!",
        role="analyst",
    )

    # Insert Canonical Records for Alpha
    # 1. Requisition
    req_alpha = {
        "requisition_id": "REQ-ALPHA-01",
        "job_title": "Backend Engineer",
        "open_date": "2026-01-01",
        "status": "open",
    }
    db_session.add(
        CanonicalRecord(
            tenant_id=t_alpha.id,
            entity_type="requisitions",
            entity_id="REQ-ALPHA-01",
            source_system_id="ats",
            source_record_id="R-A1",
            fingerprint="fp1",
            payload=json.dumps(req_alpha),
        )
    )

    # 2. Applications for Alpha
    # App 1: Agency -> Joined
    app_a1 = {
        "application_id": "APP-A1",
        "requisition_id": "REQ-ALPHA-01",
        "candidate_id": "C-1",
        "applied_at": "2026-02-01T10:00:00Z",
        "source_channel": "Agency",
        "current_stage": "hired",
        "application_outcome": "joined",
    }
    # App 2: LinkedIn -> Joined
    app_a2 = {
        "application_id": "APP-A2",
        "requisition_id": "REQ-ALPHA-01",
        "candidate_id": "C-2",
        "applied_at": "2026-02-15T10:00:00Z",
        "source_channel": "LinkedIn",
        "current_stage": "hired",
        "application_outcome": "joined",
    }
    # App 3: Referral -> Rejected
    app_a3 = {
        "application_id": "APP-A3",
        "requisition_id": "REQ-ALPHA-01",
        "candidate_id": "C-3",
        "applied_at": "2026-02-20T10:00:00Z",
        "source_channel": "Referral",
        "current_stage": "rejected",
        "application_outcome": "rejected",
    }
    for app in [app_a1, app_a2, app_a3]:
        db_session.add(
            CanonicalRecord(
                tenant_id=t_alpha.id,
                entity_type="applications",
                entity_id=app["application_id"],
                source_system_id="ats",
                source_record_id=app["application_id"],
                fingerprint=f"fp_{app['application_id']}",
                payload=json.dumps(app),
            )
        )

    # 3. Stage events for Alpha
    event_a1 = {
        "stage_event_id": "EVT-A1",
        "application_id": "APP-A1",
        "stage_name": "joined",
        "event_date": "2026-03-15",
        "outcome": "joined",
    }
    event_a2 = {
        "stage_event_id": "EVT-A2",
        "application_id": "APP-A2",
        "stage_name": "joined",
        "event_date": "2026-04-01",
        "outcome": "joined",
    }
    for ev in [event_a1, event_a2]:
        db_session.add(
            CanonicalRecord(
                tenant_id=t_alpha.id,
                entity_type="stage_events",
                entity_id=ev["stage_event_id"],
                source_system_id="ats",
                source_record_id=ev["stage_event_id"],
                fingerprint=f"fp_{ev['stage_event_id']}",
                payload=json.dumps(ev),
            )
        )

    # 4. Direct Costs for Alpha
    cost_a1 = {
        "cost_id": "COST-A1",
        "requisition_id": "REQ-ALPHA-01",
        "channel": "Agency",
        "amount": 5000.0,
        "currency": "USD",
        "cost_date": "2026-03-20",
    }
    db_session.add(
        CanonicalRecord(
            tenant_id=t_alpha.id,
            entity_type="costs",
            entity_id="COST-A1",
            source_system_id="ats",
            source_record_id="C-A1",
            fingerprint="fp_ca1",
            payload=json.dumps(cost_a1),
        )
    )

    # Insert Canonical Records for Beta (Deliberately completely different)
    req_beta = {
        "requisition_id": "REQ-BETA-01",
        "job_title": "Product Designer",
        "open_date": "2026-01-10",
        "status": "open",
    }
    db_session.add(
        CanonicalRecord(
            tenant_id=t_beta.id,
            entity_type="requisitions",
            entity_id="REQ-BETA-01",
            source_system_id="ats",
            source_record_id="R-B1",
            fingerprint="fp_rb1",
            payload=json.dumps(req_beta),
        )
    )

    app_b1 = {
        "application_id": "APP-B1",
        "requisition_id": "REQ-BETA-01",
        "candidate_id": "C-B1",
        "applied_at": "2026-02-10T10:00:00Z",
        "source_channel": "Career Fair",
        "current_stage": "hired",
        "application_outcome": "joined",
    }
    db_session.add(
        CanonicalRecord(
            tenant_id=t_beta.id,
            entity_type="applications",
            entity_id="APP-B1",
            source_system_id="ats",
            source_record_id="APP-B1",
            fingerprint="fp_app_b1",
            payload=json.dumps(app_b1),
        )
    )

    event_b1 = {
        "stage_event_id": "EVT-B1",
        "application_id": "APP-B1",
        "stage_name": "joined",
        "event_date": "2026-04-10",
        "outcome": "joined",
    }
    db_session.add(
        CanonicalRecord(
            tenant_id=t_beta.id,
            entity_type="stage_events",
            entity_id="EVT-B1",
            source_system_id="ats",
            source_record_id="EVT-B1",
            fingerprint="fp_eb1",
            payload=json.dumps(event_b1),
        )
    )

    db_session.commit()

    # Generate tokens
    alpha_analyst_token = create_access_token(
        user_id=u_alpha_analyst.id, tenant_id=t_alpha.id, role="analyst"
    )
    alpha_viewer_token = create_access_token(
        user_id=u_alpha_viewer.id, tenant_id=t_alpha.id, role="viewer"
    )
    beta_analyst_token = create_access_token(
        user_id=u_beta_analyst.id, tenant_id=t_beta.id, role="analyst"
    )

    return {
        "t_alpha": t_alpha,
        "t_beta": t_beta,
        "alpha_analyst_token": alpha_analyst_token,
        "alpha_viewer_token": alpha_viewer_token,
        "beta_analyst_token": beta_analyst_token,
    }


# ─── 1. /v2/analytics Tests ───────────────────────────────────────────────────


def test_analytics_authorized_request_succeeds(client: TestClient, analytics_seed_data):
    token = analytics_seed_data["alpha_viewer_token"]
    resp = client.get(
        "/v2/analytics?cohort_start=2026-01-01&cohort_end=2026-06-30&as_of_date=2026-10-11&observation_days=90",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["contract_version"] == "analytics_response_v1"
    assert "channel_metrics" in data

    # Verify metrics for Alpha channels
    channels = {c["channel"]: c for c in data["channel_metrics"]}
    assert "Agency" in channels
    assert "LinkedIn" in channels
    assert "Referral" in channels
    # Career Fair was for Beta, must NOT appear in Alpha!
    assert "Career Fair" not in channels


def test_analytics_missing_or_invalid_auth_rejected(client: TestClient):
    assert client.get("/v2/analytics").status_code == 401
    assert client.get("/v2/analytics", headers={"Authorization": "Bearer bad-token"}).status_code == 401


def test_analytics_cross_tenant_isolation(client: TestClient, analytics_seed_data):
    token_beta = analytics_seed_data["beta_analyst_token"]

    # Beta queries analytics: must see ONLY Career Fair
    resp_beta = client.get(
        "/v2/analytics?cohort_start=2026-01-01&cohort_end=2026-06-30&as_of_date=2026-10-11&observation_days=90",
        headers={"Authorization": f"Bearer {token_beta}"},
    )
    assert resp_beta.status_code == 200
    beta_data = resp_beta.json()
    beta_channels = [c["channel"] for c in beta_data.get("channel_metrics", [])]
    assert "Career Fair" in beta_channels
    assert "Agency" not in beta_channels
    assert "LinkedIn" not in beta_channels

    # Beta attempts to filter by Alpha's requisition: must return 404
    resp_cross = client.get(
        "/v2/analytics?requisition_id=REQ-ALPHA-01",
        headers={"Authorization": f"Bearer {token_beta}"},
    )
    assert resp_cross.status_code == 404
    assert resp_cross.json()["error"] == "requisition_not_found"


def test_analytics_date_validation(client: TestClient, analytics_seed_data):
    token = analytics_seed_data["alpha_viewer_token"]

    # Malformed date
    resp1 = client.get(
        "/v2/analytics?cohort_start=invalid-date",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp1.status_code == 400
    assert resp1.json()["error"] == "invalid_date_format"

    # End before start
    resp2 = client.get(
        "/v2/analytics?cohort_start=2026-06-01&cohort_end=2026-01-01",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp2.status_code == 400
    assert resp2.json()["error"] == "invalid_date_range"


# ─── 2. /v2/agent/query Tests ─────────────────────────────────────────────────


def test_agent_query_channel_yield(client: TestClient, analytics_seed_data):
    token = analytics_seed_data["alpha_analyst_token"]
    payload = {
        "operation": "query_channel_yield",
        "parameters": {
            "cohort_start": "2026-01-01",
            "cohort_end": "2026-06-30",
            "as_of_date": "2026-10-11",
            "observation_days": 90,
        },
    }
    resp = client.post(
        "/v2/agent/query",
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert data["operation"] == "query_channel_yield"
    assert data["execution_mode"] == "deterministic_rule_engine"
    assert data["tenant_id"] == analytics_seed_data["t_alpha"].id
    assert "channel_metrics" in data["data"]


def test_agent_query_requisition_summary(client: TestClient, analytics_seed_data):
    token = analytics_seed_data["alpha_analyst_token"]
    payload = {
        "operation": "query_requisition_summary",
        "parameters": {
            "requisition_id": "REQ-ALPHA-01",
            "as_of_date": "2026-10-11",
        },
    }
    resp = client.post(
        "/v2/agent/query",
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["requisition_id"] == "REQ-ALPHA-01"
    assert "summary" in data


def test_agent_query_role_enforcement(client: TestClient, analytics_seed_data):
    # Viewer cannot run agent queries (requires analyst role)
    viewer_token = analytics_seed_data["alpha_viewer_token"]
    payload = {
        "operation": "query_channel_yield",
        "parameters": {},
    }
    resp = client.post(
        "/v2/agent/query",
        headers={"Authorization": f"Bearer {viewer_token}"},
        json=payload,
    )
    assert resp.status_code == 403
    assert "analyst" in resp.json()["message"].lower()


def test_agent_query_unsupported_operation_rejected(client: TestClient, analytics_seed_data):
    token = analytics_seed_data["alpha_analyst_token"]
    payload = {
        "operation": "execute_arbitrary_shell_command",
        "parameters": {},
    }
    resp = client.post(
        "/v2/agent/query",
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "unsupported_operation"


def test_agent_query_sql_injection_rejected(client: TestClient, analytics_seed_data):
    token = analytics_seed_data["alpha_analyst_token"]
    payload = {
        "operation": "query_channel_yield",
        "parameters": {
            "requisition_id": "REQ-1; DROP TABLE users; --",
        },
    }
    resp = client.post(
        "/v2/agent/query",
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "prohibited_parameter"


def test_agent_query_url_fetching_rejected(client: TestClient, analytics_seed_data):
    token = analytics_seed_data["alpha_analyst_token"]
    payload = {
        "operation": "query_channel_yield",
        "parameters": {
            "requisition_id": "https://malicious-site.com/exploit",
        },
    }
    resp = client.post(
        "/v2/agent/query",
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "prohibited_parameter"


def test_agent_query_shell_command_rejected(client: TestClient, analytics_seed_data):
    token = analytics_seed_data["alpha_analyst_token"]
    payload = {
        "operation": "query_channel_yield",
        "parameters": {
            "requisition_id": "REQ-1 | powershell.exe -c whoami",
        },
    }
    resp = client.post(
        "/v2/agent/query",
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "prohibited_parameter"


def test_agent_query_cross_tenant_requisition_denied(client: TestClient, analytics_seed_data):
    # Beta analyst asks for Alpha's requisition via agent interface
    token_beta = analytics_seed_data["beta_analyst_token"]
    payload = {
        "operation": "query_requisition_summary",
        "parameters": {
            "requisition_id": "REQ-ALPHA-01",
        },
    }
    resp = client.post(
        "/v2/agent/query",
        headers={"Authorization": f"Bearer {token_beta}"},
        json=payload,
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "requisition_not_found"


def test_agent_ranking_requires_sample_size_threshold(client: TestClient, analytics_seed_data):
    token = analytics_seed_data["alpha_analyst_token"]
    # Calling rank_recruitment_channels without min_sample_size
    payload = {
        "operation": "rank_recruitment_channels",
        "parameters": {},
    }
    resp = client.post(
        "/v2/agent/query",
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "ranking_threshold_required"
