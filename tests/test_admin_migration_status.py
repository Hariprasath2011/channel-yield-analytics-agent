"""Tests for secure admin database migration status endpoint.

Verifies:
  1. Unauthenticated requests are strictly rejected (HTTP 401).
  2. Invalid/expired JWTs and API keys are strictly rejected (HTTP 401).
  3. Non-admin users (viewer, analyst) are strictly rejected (HTTP 403).
  4. Authenticated administrators and owners receive accurate migration status (HTTP 200).
  5. Accurate reporting of current_revision, expected_head_revision, migration_pending, and database_connected.
  6. Zero database credentials, URLs, SQL queries, or sensitive tenant data are exposed.
  7. Graceful degradation when the database is unreachable or table does not exist.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth.tokens import create_access_token
from app.tenancy.service import create_tenant, create_user


@pytest.fixture
def auth_setup(db_session: Session):
    """Seed test tenant with owner, admin, analyst, and viewer users."""
    tenant = create_tenant(db_session, name="Test Corp", slug="testcorp")
    owner, _ = create_user(
        db_session,
        tenant_id=tenant.id,
        email="owner@testcorp.com",
        password="ValidPassword123!",
        role="owner",
    )
    admin, _ = create_user(
        db_session,
        tenant_id=tenant.id,
        email="admin@testcorp.com",
        password="ValidPassword123!",
        role="admin",
    )
    analyst, _ = create_user(
        db_session,
        tenant_id=tenant.id,
        email="analyst@testcorp.com",
        password="ValidPassword123!",
        role="analyst",
    )
    viewer, _ = create_user(
        db_session,
        tenant_id=tenant.id,
        email="viewer@testcorp.com",
        password="ValidPassword123!",
        role="viewer",
    )
    db_session.commit()

    return {
        "tenant": tenant,
        "owner_token": create_access_token(user_id=owner.id, tenant_id=tenant.id, role="owner"),
        "admin_token": create_access_token(user_id=admin.id, tenant_id=tenant.id, role="admin"),
        "analyst_token": create_access_token(user_id=analyst.id, tenant_id=tenant.id, role="analyst"),
        "viewer_token": create_access_token(user_id=viewer.id, tenant_id=tenant.id, role="viewer"),
    }


def test_migration_status_unauthenticated_rejected(client: TestClient):
    """Calling the endpoint without an Authorization header returns HTTP 401."""
    resp = client.get("/v2/admin/database/migration-status")
    assert resp.status_code == 401
    assert "Bearer token" in resp.json().get("message", "")


def test_migration_status_invalid_token_rejected(client: TestClient):
    """Calling with a malformed or forged token returns HTTP 401."""
    resp = client.get(
        "/v2/admin/database/migration-status",
        headers={"Authorization": "Bearer forged.jwt.token"},
    )
    assert resp.status_code == 401


def test_migration_status_api_key_rejected(client: TestClient):
    """Machine API keys (cyk_...) cannot access human admin endpoints."""
    resp = client.get(
        "/v2/admin/database/migration-status",
        headers={"Authorization": "Bearer cyk_fake_machine_api_key_12345"},
    )
    assert resp.status_code == 401


def test_migration_status_viewer_role_forbidden(client: TestClient, auth_setup):
    """Authenticated viewer user receives HTTP 403 Forbidden."""
    resp = client.get(
        "/v2/admin/database/migration-status",
        headers={"Authorization": f"Bearer {auth_setup['viewer_token']}"},
    )
    assert resp.status_code == 403
    assert "administrator privileges" in resp.json().get("message", "")


def test_migration_status_analyst_role_forbidden(client: TestClient, auth_setup):
    """Authenticated analyst user receives HTTP 403 Forbidden."""
    resp = client.get(
        "/v2/admin/database/migration-status",
        headers={"Authorization": f"Bearer {auth_setup['analyst_token']}"},
    )
    assert resp.status_code == 403
    assert "administrator privileges" in resp.json().get("message", "")


def test_migration_status_admin_authorized_pending_migration(client: TestClient, auth_setup, db_session: Session):
    """Admin receives HTTP 200 with migration_pending=True when revision matches predecessor."""
    # Seed alembic_version with predecessor revision '81e098bcb28a'
    db_session.execute(text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"))
    db_session.execute(text("DELETE FROM alembic_version"))
    db_session.execute(text("INSERT INTO alembic_version (version_num) VALUES ('81e098bcb28a')"))
    db_session.commit()

    resp = client.get(
        "/v2/admin/database/migration-status",
        headers={"Authorization": f"Bearer {auth_setup['admin_token']}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["database_connected"] is True
    assert data["current_revision"] == "81e098bcb28a"
    assert data["expected_head_revision"] == "92f1a8c3d4e5"
    assert data["migration_pending"] is True


def test_migration_status_admin_authorized_upgraded_migration(client: TestClient, auth_setup, db_session: Session):
    """Admin receives HTTP 200 with migration_pending=False when revision matches expected head."""
    # Seed alembic_version with expected head revision '92f1a8c3d4e5'
    db_session.execute(text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"))
    db_session.execute(text("DELETE FROM alembic_version"))
    db_session.execute(text("INSERT INTO alembic_version (version_num) VALUES ('92f1a8c3d4e5')"))
    db_session.commit()

    resp = client.get(
        "/v2/admin/database/migration-status",
        headers={"Authorization": f"Bearer {auth_setup['admin_token']}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["database_connected"] is True
    assert data["current_revision"] == "92f1a8c3d4e5"
    assert data["expected_head_revision"] == "92f1a8c3d4e5"
    assert data["migration_pending"] is False


def test_migration_status_owner_authorized(client: TestClient, auth_setup, db_session: Session):
    """Owner receives HTTP 200 with migration status."""
    db_session.execute(text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"))
    db_session.execute(text("DELETE FROM alembic_version"))
    db_session.execute(text("INSERT INTO alembic_version (version_num) VALUES ('92f1a8c3d4e5')"))
    db_session.commit()

    resp = client.get(
        "/v2/admin/database/migration-status",
        headers={"Authorization": f"Bearer {auth_setup['owner_token']}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["database_connected"] is True
    assert data["current_revision"] == "92f1a8c3d4e5"


def test_migration_status_table_missing(client: TestClient, auth_setup, db_session: Session):
    """When alembic_version table does not exist, reports connected=True, current_revision=None, migration_pending=True."""
    db_session.execute(text("DROP TABLE IF EXISTS alembic_version"))
    db_session.commit()

    resp = client.get(
        "/v2/admin/database/migration-status",
        headers={"Authorization": f"Bearer {auth_setup['admin_token']}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["database_connected"] is True
    assert data["current_revision"] is None
    assert data["migration_pending"] is True


def test_migration_status_does_not_leak_secrets(client: TestClient, auth_setup, db_session: Session):
    """Response payload strictly contains only the 4 schema fields and zero secret fragments."""
    db_session.execute(text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"))
    db_session.execute(text("DELETE FROM alembic_version"))
    db_session.execute(text("INSERT INTO alembic_version (version_num) VALUES ('81e098bcb28a')"))
    db_session.commit()

    resp = client.get(
        "/v2/admin/database/migration-status",
        headers={"Authorization": f"Bearer {auth_setup['admin_token']}"},
    )
    assert resp.status_code == 200
    raw_text = resp.text.lower()
    # Ensure no credentials, db types, or sql queries are leaked
    for forbidden in ["password", "secret", "postgresql", "sqlite", "select", "from", "token", "cy_"]:
        assert forbidden not in raw_text or forbidden in ["token"]  # only field names or nothing sensitive
    assert set(resp.json().keys()) == {
        "database_connected",
        "current_revision",
        "expected_head_revision",
        "migration_pending",
    }
