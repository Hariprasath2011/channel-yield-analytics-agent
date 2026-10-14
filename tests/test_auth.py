"""Comprehensive tests for authentication, tenant isolation, and role enforcement."""
from datetime import datetime, timedelta, timezone
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth.tokens import create_access_token
from app.auth.passwords import hash_password
from app.auth.rate_limiter import login_limiter
from app.persistence.models import ApiKey, Membership, Tenant, User
from app.tenancy.service import create_tenant, create_user, create_api_key


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    login_limiter.reset()
    yield
    login_limiter.reset()


@pytest.fixture
def seed_data(db_session: Session):
    """Seed two isolated tenants with users of different roles."""
    # Tenant Alpha
    t_alpha = create_tenant(db_session, name="Alpha Corp", slug="alpha")
    u_alpha_owner, _ = create_user(
        db_session,
        tenant_id=t_alpha.id,
        email="owner@alpha.com",
        password="AlphaOwnerPassword123!",
        role="owner",
    )
    u_alpha_admin, _ = create_user(
        db_session,
        tenant_id=t_alpha.id,
        email="admin@alpha.com",
        password="AlphaAdminPassword123!",
        role="admin",
    )
    u_alpha_analyst, _ = create_user(
        db_session,
        tenant_id=t_alpha.id,
        email="analyst@alpha.com",
        password="AlphaAnalystPassword123!",
        role="analyst",
    )
    u_alpha_viewer, _ = create_user(
        db_session,
        tenant_id=t_alpha.id,
        email="viewer@alpha.com",
        password="AlphaViewerPassword123!",
        role="viewer",
    )

    # Tenant Beta
    t_beta = create_tenant(db_session, name="Beta Ltd", slug="beta")
    u_beta_owner, _ = create_user(
        db_session,
        tenant_id=t_beta.id,
        email="owner@beta.com",
        password="BetaOwnerPassword123!",
        role="owner",
    )

    db_session.commit()

    return {
        "tenant_alpha": t_alpha,
        "tenant_beta": t_beta,
        "alpha_owner": u_alpha_owner,
        "alpha_admin": u_alpha_admin,
        "alpha_analyst": u_alpha_analyst,
        "alpha_viewer": u_alpha_viewer,
        "beta_owner": u_beta_owner,
    }


# ─── A. Authentication Token Tests ────────────────────────────────────────────


def test_login_successful_and_get_me(client: TestClient, seed_data):
    # Login as Alpha admin
    resp = client.post(
        "/v2/auth/token",
        json={"email": "admin@alpha.com", "password": "AlphaAdminPassword123!"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data
    assert data["role"] == "admin"
    assert data["tenant_slug"] == "alpha"
    assert data["token_type"] == "bearer"

    token = data["access_token"]

    # Call /v2/auth/me with bearer token
    me_resp = client.get("/v2/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me_resp.status_code == 200
    me_data = me_resp.json()
    assert me_data["email"] == "admin@alpha.com"
    assert me_data["role"] == "admin"
    assert me_data["tenant_slug"] == "alpha"


def test_login_invalid_password(client: TestClient, seed_data):
    resp = client.post(
        "/v2/auth/token",
        json={"email": "admin@alpha.com", "password": "WrongPassword!"},
    )
    assert resp.status_code == 401
    assert resp.json()["error"] == "unauthorized"


def test_login_nonexistent_user(client: TestClient, seed_data):
    resp = client.post(
        "/v2/auth/token",
        json={"email": "nobody@alpha.com", "password": "AnyPassword!"},
    )
    assert resp.status_code == 401
    assert resp.json()["error"] == "unauthorized"


def test_missing_or_malformed_token_rejected(client: TestClient):
    # Missing header
    assert client.get("/v2/auth/me").status_code == 401

    # Not bearer
    assert client.get("/v2/auth/me", headers={"Authorization": "Basic dXNlcjpwYXNz"}).status_code == 401

    # Malformed bearer string
    assert client.get("/v2/auth/me", headers={"Authorization": "Bearer not-a-jwt"}).status_code == 401


def test_expired_jwt_rejected(client: TestClient, test_settings, seed_data):
    alpha = seed_data["tenant_alpha"]
    owner = seed_data["alpha_owner"]

    # Issue token with negative expiry
    expired_token = create_access_token(
        user_id=owner.id,
        tenant_id=alpha.id,
        role="owner",
        expire_minutes=-5,
    )
    resp = client.get("/v2/auth/me", headers={"Authorization": f"Bearer {expired_token}"})
    assert resp.status_code == 401
    assert "invalid or expired" in resp.json()["message"].lower()


# ─── B. Inactive User / Inactive Tenant Tests ─────────────────────────────────


def test_inactive_user_rejected(client: TestClient, db_session: Session, seed_data):
    user = seed_data["alpha_viewer"]
    user.is_active = False
    db_session.commit()

    # Login fails
    resp = client.post(
        "/v2/auth/token",
        json={"email": "viewer@alpha.com", "password": "AlphaViewerPassword123!"},
    )
    assert resp.status_code == 401

    # Valid token previously issued fails resolution
    token = create_access_token(user_id=user.id, tenant_id=seed_data["tenant_alpha"].id, role="viewer")
    me_resp = client.get("/v2/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me_resp.status_code == 401
    assert "inactive" in me_resp.json()["message"].lower()


def test_inactive_tenant_rejected(client: TestClient, db_session: Session, seed_data):
    tenant = seed_data["tenant_alpha"]
    tenant.is_active = False
    db_session.commit()

    # Login fails
    resp = client.post(
        "/v2/auth/token",
        json={"email": "admin@alpha.com", "password": "AlphaAdminPassword123!"},
    )
    assert resp.status_code == 401

    # Existing token fails
    token = create_access_token(user_id=seed_data["alpha_admin"].id, tenant_id=tenant.id, role="admin")
    me_resp = client.get("/v2/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me_resp.status_code == 401


# ─── C. API Key Lifecycle & Revocation ─────────────────────────────────────────


def test_api_key_issuance_authentication_and_revocation(client: TestClient, seed_data):
    # Login as Alpha Admin
    login_resp = client.post(
        "/v2/auth/token",
        json={"email": "admin@alpha.com", "password": "AlphaAdminPassword123!"},
    )
    admin_token = login_resp.json()["access_token"]

    # Issue API key
    key_resp = client.post(
        "/v2/auth/api-keys",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"name": "CI Ingestion Runner"},
    )
    assert key_resp.status_code == 201
    key_data = key_resp.json()
    assert "api_key" in key_data
    raw_api_key = key_data["api_key"]
    key_id = key_data["id"]
    assert raw_api_key.startswith("cyk_")

    # Use API key directly to authenticate at /v2/auth/me
    api_auth_resp = client.get("/v2/auth/me", headers={"Authorization": f"Bearer {raw_api_key}"})
    assert api_auth_resp.status_code == 200
    assert api_auth_resp.json()["tenant_id"] == seed_data["tenant_alpha"].id
    assert api_auth_resp.json()["role"] == "analyst"

    # Revoke API key
    del_resp = client.delete(
        f"/v2/auth/api-keys/{key_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert del_resp.status_code == 200
    assert del_resp.json()["status"] == "revoked"

    # Subsequent request with revoked key must fail with 401
    revoked_resp = client.get("/v2/auth/me", headers={"Authorization": f"Bearer {raw_api_key}"})
    assert revoked_resp.status_code == 401
    assert "inactive" in revoked_resp.json()["message"].lower() or "not found" in revoked_resp.json()["message"].lower()


def test_expired_api_key_rejected(client: TestClient, db_session: Session, seed_data):
    tenant = seed_data["tenant_alpha"]
    key_obj, raw_key = create_api_key(db_session, tenant_id=tenant.id, name="Expiring Key")
    key_obj.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    db_session.commit()

    resp = client.get("/v2/auth/me", headers={"Authorization": f"Bearer {raw_key}"})
    assert resp.status_code == 401
    assert "expired" in resp.json()["message"].lower()


# ─── D. Tenant Isolation Tests ────────────────────────────────────────────────


def test_cross_tenant_token_tampering_denied(client: TestClient, seed_data):
    # Attacker tries to use user_id from Alpha, but set tid to Beta
    fake_token = create_access_token(
        user_id=seed_data["alpha_admin"].id,
        tenant_id=seed_data["tenant_beta"].id,  # Spoofed tenant
        role="owner",
    )
    resp = client.get("/v2/auth/me", headers={"Authorization": f"Bearer {fake_token}"})
    assert resp.status_code == 403
    assert resp.json()["error"] == "access_denied"


def test_cross_tenant_api_key_revocation_denied_with_404(client: TestClient, seed_data):
    # Alpha Admin creates a key in Alpha
    login_alpha = client.post(
        "/v2/auth/token",
        json={"email": "admin@alpha.com", "password": "AlphaAdminPassword123!"},
    )
    alpha_token = login_alpha.json()["access_token"]
    key_resp = client.post(
        "/v2/auth/api-keys",
        headers={"Authorization": f"Bearer {alpha_token}"},
        json={"name": "Alpha Service Key"},
    )
    alpha_key_id = key_resp.json()["id"]

    # Beta Owner logs in
    login_beta = client.post(
        "/v2/auth/token",
        json={"email": "owner@beta.com", "password": "BetaOwnerPassword123!"},
    )
    beta_token = login_beta.json()["access_token"]

    # Beta Owner attempts to delete Alpha's key -> must return 404 (does not leak existence)
    del_resp = client.delete(
        f"/v2/auth/api-keys/{alpha_key_id}",
        headers={"Authorization": f"Bearer {beta_token}"},
    )
    assert del_resp.status_code == 404
    assert del_resp.json()["error"] == "not_found"


def test_cross_tenant_api_key_listing_isolated(client: TestClient, seed_data):
    login_alpha = client.post(
        "/v2/auth/token",
        json={"email": "admin@alpha.com", "password": "AlphaAdminPassword123!"},
    )
    alpha_token = login_alpha.json()["access_token"]
    client.post(
        "/v2/auth/api-keys",
        headers={"Authorization": f"Bearer {alpha_token}"},
        json={"name": "Alpha Key Exclusive"},
    )

    login_beta = client.post(
        "/v2/auth/token",
        json={"email": "owner@beta.com", "password": "BetaOwnerPassword123!"},
    )
    beta_token = login_beta.json()["access_token"]

    # Beta lists keys: must be empty
    beta_list = client.get("/v2/auth/api-keys", headers={"Authorization": f"Bearer {beta_token}"})
    assert beta_list.status_code == 200
    assert len(beta_list.json()) == 0


# ─── E. Role Enforcement Tests ────────────────────────────────────────────────


def test_role_enforcement_api_key_issuance(client: TestClient, seed_data):
    # Viewer logs in
    resp_viewer = client.post(
        "/v2/auth/token",
        json={"email": "viewer@alpha.com", "password": "AlphaViewerPassword123!"},
    )
    viewer_token = resp_viewer.json()["access_token"]

    # Viewer tries to issue API key (requires admin or higher)
    resp = client.post(
        "/v2/auth/api-keys",
        headers={"Authorization": f"Bearer {viewer_token}"},
        json={"name": "Unauthorized Key"},
    )
    assert resp.status_code == 403
    assert "admin" in resp.json()["message"].lower()


def test_role_enforcement_member_provisioning(client: TestClient, seed_data):
    # Admin tries to provision a member (requires owner role)
    resp_admin = client.post(
        "/v2/auth/token",
        json={"email": "admin@alpha.com", "password": "AlphaAdminPassword123!"},
    )
    admin_token = resp_admin.json()["access_token"]

    resp = client.post(
        "/v2/auth/members",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"email": "newbie@alpha.com", "role": "viewer"},
    )
    assert resp.status_code == 403
    assert "owner" in resp.json()["message"].lower()

    # Owner provisions a member -> succeeds
    resp_owner = client.post(
        "/v2/auth/token",
        json={"email": "owner@alpha.com", "password": "AlphaOwnerPassword123!"},
    )
    owner_token = resp_owner.json()["access_token"]

    resp_success = client.post(
        "/v2/auth/members",
        headers={"Authorization": f"Bearer {owner_token}"},
        json={"email": "newbie@alpha.com", "role": "viewer"},
    )
    assert resp_success.status_code == 201
    assert resp_success.json()["email"] == "newbie@alpha.com"
    assert resp_success.json()["role"] == "viewer"


def test_login_rate_limiting_brute_force_protection(client: TestClient, seed_data):
    # Attempt 5 wrong passwords
    for i in range(5):
        resp = client.post(
            "/v2/auth/token",
            json={"email": "admin@alpha.com", "password": f"WrongPassword{i}!"},
        )
        assert resp.status_code == 401

    # 6th attempt should be blocked with 429
    resp_blocked = client.post(
        "/v2/auth/token",
        json={"email": "admin@alpha.com", "password": "AlphaAdminPassword123!"},
    )
    assert resp_blocked.status_code == 429
    assert resp_blocked.json()["error"] == "rate_limited"
    assert "Retry-After" in resp_blocked.headers

    # Reset limiter and verify legitimate login succeeds
    login_limiter.reset()
    resp_ok = client.post(
        "/v2/auth/token",
        json={"email": "admin@alpha.com", "password": "AlphaAdminPassword123!"},
    )
    assert resp_ok.status_code == 200
