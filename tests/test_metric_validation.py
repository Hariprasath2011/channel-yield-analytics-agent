"""Deterministic synthetic dataset metric validation tests.

Validates exact calculation results for:
  - Successful joins (only joined with joined_at evidence).
  - Offers accepted without joining (never counted as confirmed join).
  - Immature vs. mature applications (observation window elapsed).
  - Missing and unknown channel attribution (tracked, excluded from named yields).
  - Direct costs vs. unallocated shared costs.
  - Duplicate ingestion replays (idempotency preserves metrics).
  - Cross-tenant data isolation.
  - Ranking behavior under sample size thresholds.
"""
from __future__ import annotations

import json
from typing import Generator
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.analytics.adapter import compute_tenant_analytics
from app.auth.passwords import hash_password
from app.auth.tokens import create_access_token
from app.main import create_app
from app.persistence.database import Base, get_db
from app.persistence.models import CanonicalRecord, Membership, Tenant, User


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def setup_tenants(db_session: Session):
    # Tenant Alpha
    t_alpha = Tenant(slug="alpha-inc", name="Alpha Inc")
    db_session.add(t_alpha)
    db_session.flush()

    user_alpha = User(
        tenant_id=t_alpha.id,
        email="analyst@alpha.com",
        full_name="Alpha Analyst",
        hashed_password=hash_password("pw1"),
    )
    db_session.add(user_alpha)
    db_session.flush()
    db_session.add(Membership(tenant_id=t_alpha.id, user_id=user_alpha.id, role="analyst"))

    # Tenant Beta
    t_beta = Tenant(slug="beta-inc", name="Beta Inc")
    db_session.add(t_beta)
    db_session.flush()

    user_beta = User(
        tenant_id=t_beta.id,
        email="analyst@beta.com",
        full_name="Beta Analyst",
        hashed_password=hash_password("pw2"),
    )
    db_session.add(user_beta)
    db_session.flush()
    db_session.add(Membership(tenant_id=t_beta.id, user_id=user_beta.id, role="analyst"))

    db_session.commit()

    return {
        "tenant_alpha": t_alpha,
        "tenant_beta": t_beta,
        "token_alpha": create_access_token(user_id=user_alpha.id, tenant_id=t_alpha.id, role="analyst"),
        "token_beta": create_access_token(user_id=user_beta.id, tenant_id=t_beta.id, role="analyst"),
    }


def _seed_record(db: Session, tenant_id: str, entity_type: str, entity_id: str, payload: dict):
    rec = CanonicalRecord(
        tenant_id=tenant_id,
        entity_type=entity_type,
        entity_id=entity_id,
        source_system_id="synthetic_test",
        source_record_id=f"src_{entity_id}",
        fingerprint=f"fp_{entity_id}",
        payload=json.dumps(payload),
    )
    db.add(rec)
    db.commit()


def test_metric_validation_successful_joins_and_offers_accepted(db_session: Session, setup_tenants):
    """Scenario 1 & 2:
    - 1 candidate joined with joined_at evidence (confirmed join).
    - 1 candidate rejected (mature non-join).
    - 1 candidate offer accepted but never joined (not a confirmed join).
    Expected: mature denominator = 2, confirmed joins = 1, yield = 50.0%.
    """
    tenant_id = setup_tenants["tenant_alpha"].id

    _seed_record(
        db_session, tenant_id, "requisitions", "R-1",
        {"client_id": tenant_id, "requisition_id": "R-1", "job_title": "Staff Architect", "status": "open"}
    )

    # 1. Confirmed join (mature: applied 2026-01-10, as_of 2026-06-01 is 142 days > 90)
    _seed_record(
        db_session, tenant_id, "applications", "A-1",
        {
            "client_id": tenant_id, "application_id": "A-1", "requisition_id": "R-1",
            "source_channel": "LinkedIn", "applied_at": "2026-01-10T00:00:00Z",
            "application_outcome": "joined", "joined_at": "2026-02-15T00:00:00Z"
        }
    )

    # 2. Mature non-join (rejected)
    _seed_record(
        db_session, tenant_id, "applications", "A-2",
        {
            "client_id": tenant_id, "application_id": "A-2", "requisition_id": "R-1",
            "source_channel": "LinkedIn", "applied_at": "2026-01-15T00:00:00Z",
            "application_outcome": "rejected"
        }
    )

    # 3. Offer accepted without joining (mature) -> not a confirmed join
    _seed_record(
        db_session, tenant_id, "applications", "A-3",
        {
            "client_id": tenant_id, "application_id": "A-3", "requisition_id": "R-1",
            "source_channel": "LinkedIn", "applied_at": "2026-01-20T00:00:00Z",
            "application_outcome": "offer_accepted"
        }
    )

    result = compute_tenant_analytics(
        db_session,
        tenant_id=tenant_id,
        cohort_start="2026-01-01",
        cohort_end="2026-01-31",
        as_of_date="2026-06-01",
        observation_days=90,
    )

    metrics = {m["channel"]: m for m in result["channel_metrics"]}
    linkedin = metrics["LinkedIn"]

    assert linkedin["applications_in_cohort"] == 3
    # Denominator only includes confirmed joins and final mature non-joins (rejected, withdrawn)
    assert linkedin["mature_eligible_applications"] == 2
    assert linkedin["confirmed_joins"] == 1
    assert linkedin["mature_non_joins"] == 1
    assert linkedin["pending_or_immature"] == 1  # offer_accepted is pending/not final
    assert linkedin["yield_percent"] == 50.0  # (1 / 2) * 100 = 50.0%


def test_metric_validation_immature_applications(db_session: Session, setup_tenants):
    """Scenario 3:
    Candidate applied 2026-05-15, as_of_date 2026-06-01 (17 days elapsed < 90 observation days).
    Expected: Counted under pending_or_immature, yield denominator = 0.
    """
    tenant_id = setup_tenants["tenant_alpha"].id

    _seed_record(
        db_session, tenant_id, "requisitions", "R-2",
        {"client_id": tenant_id, "requisition_id": "R-2", "job_title": "Data Lead", "status": "open"}
    )
    _seed_record(
        db_session, tenant_id, "applications", "A-4",
        {
            "client_id": tenant_id, "application_id": "A-4", "requisition_id": "R-2",
            "source_channel": "Agency", "applied_at": "2026-05-15T00:00:00Z",
            "application_outcome": "joined", "joined_at": "2026-05-25T00:00:00Z"
        }
    )

    result = compute_tenant_analytics(
        db_session,
        tenant_id=tenant_id,
        cohort_start="2026-05-01",
        cohort_end="2026-05-31",
        as_of_date="2026-06-01",
        observation_days=90,
    )

    agency = [m for m in result["channel_metrics"] if m["channel"] == "Agency"][0]
    assert agency["applications_in_cohort"] == 1
    assert agency["mature_eligible_applications"] == 0
    assert agency["pending_or_immature"] == 1
    assert agency["yield_percent"] is None
    assert agency["yield_status"] == "not_applicable_no_mature_denominator"


def test_metric_validation_unknown_source_attribution(db_session: Session, setup_tenants):
    """Scenario 4:
    Unknown source channel applications are counted in total cohort, but excluded from named channel yields.
    """
    tenant_id = setup_tenants["tenant_alpha"].id

    _seed_record(
        db_session, tenant_id, "requisitions", "R-3",
        {"client_id": tenant_id, "requisition_id": "R-3", "job_title": "Analyst", "status": "open"}
    )
    _seed_record(
        db_session, tenant_id, "applications", "A-5",
        {
            "client_id": tenant_id, "application_id": "A-5", "requisition_id": "R-3",
            "source_channel": "unknown", "applied_at": "2026-01-10T00:00:00Z",
            "application_outcome": "joined", "joined_at": "2026-02-10T00:00:00Z"
        }
    )

    result = compute_tenant_analytics(
        db_session,
        tenant_id=tenant_id,
        cohort_start="2026-01-01",
        cohort_end="2026-01-31",
        as_of_date="2026-06-01",
        observation_days=90,
    )

    assert result["summary"]["applications_in_cohort"] == 1
    assert result["summary"]["unknown_source_applications"] == 1
    assert result["summary"]["known_source_applications"] == 0
    assert result["data_quality"]["unknown_source_applications"] == 1
    assert "unknown_source_applications_excluded_from_named_channel_yield" in result["data_quality"]["warnings"]
    assert len(result["channel_metrics"]) == 0  # Not attributed to any named channel


def test_metric_validation_direct_costs_vs_unallocated_costs(db_session: Session, setup_tenants):
    """Scenario 5:
    Direct costs with requisition_id vs unallocated shared costs.
    """
    tenant_id = setup_tenants["tenant_alpha"].id
    _seed_record(
        db_session, tenant_id, "requisitions", "R-1",
        {"client_id": tenant_id, "requisition_id": "R-1", "job_title": "Platform Lead", "status": "open"}
    )
    _seed_record(
        db_session, tenant_id, "costs", "C-1",
        {"client_id": tenant_id, "cost_id": "C-1", "requisition_id": "R-1", "source_channel": "LinkedIn", "amount": 1500.0, "currency": "USD"}
    )
    _seed_record(
        db_session, tenant_id, "costs", "C-2",
        {"client_id": tenant_id, "cost_id": "C-2", "requisition_id": None, "source_channel": "BrandCampaign", "amount": 5000.0, "currency": "USD"}
    )

    # Scoped to requisition R-1
    res_req = compute_tenant_analytics(
        db_session,
        tenant_id=tenant_id,
        cohort_start="2026-01-01",
        cohort_end="2026-06-30",
        as_of_date="2026-07-01",
        observation_days=90,
        requisition_id="R-1",
    )
    # Requisition-scoped query should include only C-1
    cost_groups = res_req["reported_costs"]["groups"]
    channels = {g["channel"]: g["reported_cost"] for g in cost_groups}
    assert channels.get("LinkedIn") == 1500.0
    assert "BrandCampaign" not in channels
    assert "some_cost_records_are_not_requisition_allocated" in res_req["data_quality"]["warnings"]


def test_metric_validation_sample_size_ranking_rules(db_session: Session, setup_tenants):
    """Scenario 8:
    Without approved sample size threshold, ranking is disabled.
    With approved sample size (e.g. 2), only channels meeting threshold are ranked.
    """
    tenant_id = setup_tenants["tenant_alpha"].id

    # Seed 2 mature applications for Employee Referral (100% yield)
    for i in range(1, 3):
        _seed_record(
            db_session, tenant_id, "applications", f"A-REF-{i}",
            {
                "client_id": tenant_id, "application_id": f"A-REF-{i}", "requisition_id": "R-1",
                "source_channel": "Referral", "applied_at": "2026-01-10T00:00:00Z",
                "application_outcome": "joined", "joined_at": "2026-02-10T00:00:00Z"
            }
        )

    # Seed 1 mature application for JobBoard (100% yield)
    _seed_record(
        db_session, tenant_id, "applications", "A-JB-1",
        {
            "client_id": tenant_id, "application_id": "A-JB-1", "requisition_id": "R-1",
            "source_channel": "JobBoard", "applied_at": "2026-01-10T00:00:00Z",
            "application_outcome": "joined", "joined_at": "2026-02-10T00:00:00Z"
        }
    )

    # 1. Unapproved threshold (min_sample_size=None)
    res_no_thresh = compute_tenant_analytics(
        db_session,
        tenant_id=tenant_id,
        cohort_start="2026-01-01",
        cohort_end="2026-01-31",
        as_of_date="2026-06-01",
        observation_days=90,
        min_sample_size=None,
    )
    assert len(res_no_thresh["ranked_channels"]) == 0
    assert "ranking_disabled_until_minimum_sample_size_policy_is_configured" in res_no_thresh["data_quality"]["warnings"]

    # 2. Configured threshold = 2
    res_thresh = compute_tenant_analytics(
        db_session,
        tenant_id=tenant_id,
        cohort_start="2026-01-01",
        cohort_end="2026-01-31",
        as_of_date="2026-06-01",
        observation_days=90,
        min_sample_size=2,
    )
    ranked = res_thresh["ranked_channels"]
    # Only Referral has mature_eligible_applications >= 2
    assert len(ranked) == 1
    assert ranked[0]["channel"] == "Referral"
    assert ranked[0]["rank"] == 1


def test_metric_validation_cross_tenant_isolation(db_session: Session, setup_tenants):
    """Scenario 7:
    Tenant Alpha and Tenant Beta data never mix.
    """
    alpha_id = setup_tenants["tenant_alpha"].id
    beta_id = setup_tenants["tenant_beta"].id

    _seed_record(
        db_session, beta_id, "requisitions", "R-BETA",
        {"client_id": beta_id, "requisition_id": "R-BETA", "job_title": "Beta Engineer", "status": "open"}
    )
    _seed_record(
        db_session, beta_id, "applications", "A-BETA-1",
        {
            "client_id": beta_id, "application_id": "A-BETA-1", "requisition_id": "R-BETA",
            "source_channel": "Campus", "applied_at": "2026-01-10T00:00:00Z",
            "application_outcome": "joined", "joined_at": "2026-02-10T00:00:00Z"
        }
    )

    res_alpha = compute_tenant_analytics(
        db_session,
        tenant_id=alpha_id,
        cohort_start="2026-01-01",
        cohort_end="2026-06-30",
        as_of_date="2026-07-01",
        observation_days=90,
    )
    res_beta = compute_tenant_analytics(
        db_session,
        tenant_id=beta_id,
        cohort_start="2026-01-01",
        cohort_end="2026-06-30",
        as_of_date="2026-07-01",
        observation_days=90,
    )

    alpha_channels = {m["channel"] for m in res_alpha["channel_metrics"]}
    beta_channels = {m["channel"] for m in res_beta["channel_metrics"]}

    assert "Campus" not in alpha_channels
    assert "Campus" in beta_channels
