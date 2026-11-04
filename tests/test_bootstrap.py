"""Comprehensive tests for administrative tenant and owner bootstrap."""
from __future__ import annotations

import io
import sys
import uuid
from typing import Generator
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

import app.persistence.database as db_mod
from app.auth.passwords import verify_password
from app.cli.bootstrap import execute_bootstrap
from app.core.config import Settings, get_settings
from app.core.exceptions import ConflictError, ValidationError
from app.persistence.database import Base
from app.persistence.models import Membership, Tenant, User
from app.tenancy.service import bootstrap_tenant_and_owner, create_tenant, create_user


# ─── 1. Successful Tenant, Owner User, and Owner Membership Creation ──────────


def test_bootstrap_successful(db_session: Session):
    result = bootstrap_tenant_and_owner(
        db_session,
        name="Acme Corporation",
        slug="acme-corp",
        email="owner@acme-corp.com",
        password="ValidOwnerPassword123!",
        full_name="Alice Owner",
    )
    db_session.commit()

    assert result["status"] == "created"
    assert result["tenant_slug"] == "acme-corp"
    assert result["tenant_name"] == "Acme Corporation"
    assert result["owner_email"] == "owner@acme-corp.com"
    assert result["owner_full_name"] == "Alice Owner"
    assert result["role"] == "owner"

    tenant = db_session.query(Tenant).filter(Tenant.slug == "acme-corp").first()
    assert tenant is not None
    assert tenant.name == "Acme Corporation"
    assert tenant.is_active is True

    user = db_session.query(User).filter(User.tenant_id == tenant.id, User.email == "owner@acme-corp.com").first()
    assert user is not None
    assert user.full_name == "Alice Owner"
    assert user.is_active is True

    membership = (
        db_session.query(Membership)
        .filter(Membership.tenant_id == tenant.id, Membership.user_id == user.id)
        .first()
    )
    assert membership is not None
    assert membership.role == "owner"


# ─── 2. Password Hashing, Verification, and Token Login ───────────────────────


def test_bootstrap_password_hashing_and_login(client: TestClient, db_session: Session):
    plain_password = "SecureOwnerPassword2026!"
    result = bootstrap_tenant_and_owner(
        db_session,
        name="Security Org",
        slug="security-org",
        email="owner@security.org",
        password=plain_password,
    )
    db_session.commit()

    user = db_session.query(User).filter(User.id == result["user_id"]).one()
    assert user.hashed_password is not None
    assert user.hashed_password != plain_password
    assert user.hashed_password.startswith("$2b$") or user.hashed_password.startswith("$2a$")

    # Native password verification
    assert verify_password(plain_password, user.hashed_password) is True
    assert verify_password("IncorrectPassword123!", user.hashed_password) is False

    # Login through auth token endpoint
    resp = client.post(
        "/v2/auth/token",
        json={"email": "owner@security.org", "password": plain_password},
    )
    assert resp.status_code == 200
    token_data = resp.json()
    assert token_data["role"] == "owner"
    assert token_data["tenant_slug"] == "security-org"

    # Access /v2/auth/me
    token = token_data["access_token"]
    me_resp = client.get("/v2/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me_resp.status_code == 200
    assert me_resp.json()["role"] == "owner"


# ─── 3. Rejection of Passwords Longer than 72 UTF-8 Bytes ─────────────────────


def test_bootstrap_rejects_password_longer_than_72_bytes(db_session: Session):
    # 73 ASCII bytes
    long_ascii_pw = "P" * 73
    with pytest.raises(ValidationError) as exc_info:
        bootstrap_tenant_and_owner(
            db_session,
            name="Too Long Corp",
            slug="too-long-corp",
            email="owner@toolong.com",
            password=long_ascii_pw,
        )
    assert "72" in exc_info.value.message

    # Multi-byte UTF-8 password: 19 4-byte characters = 76 bytes
    multibyte_pw = "🔑" * 19
    assert len(multibyte_pw.encode("utf-8")) == 76
    with pytest.raises(ValidationError) as exc_info_mb:
        bootstrap_tenant_and_owner(
            db_session,
            name="Multibyte Corp",
            slug="multibyte-corp",
            email="owner@multibyte.com",
            password=multibyte_pw,
        )
    assert "72" in exc_info_mb.value.message

    # Verify no tenant was created
    assert db_session.query(Tenant).filter(Tenant.slug == "too-long-corp").first() is None
    assert db_session.query(Tenant).filter(Tenant.slug == "multibyte-corp").first() is None


def test_create_user_directly_rejects_password_longer_than_72_bytes(db_session: Session):
    tenant = create_tenant(db_session, name="Direct User Org", slug="direct-user-org")
    with pytest.raises(ValidationError) as exc_info:
        create_user(
            db_session,
            tenant_id=tenant.id,
            email="user@direct.com",
            password="X" * 73,
            role="owner",
        )
    assert "72" in exc_info.value.message


# ─── 4. Rejection of Duplicate Tenant Slug ────────────────────────────────────


def test_bootstrap_rejects_duplicate_tenant_slug(db_session: Session):
    bootstrap_tenant_and_owner(
        db_session,
        name="First Corp",
        slug="duplicate-slug",
        email="owner1@first.com",
        password="ValidPassword123!",
    )
    db_session.commit()

    with pytest.raises(ConflictError) as exc_info:
        bootstrap_tenant_and_owner(
            db_session,
            name="Second Corp",
            slug="duplicate-slug",
            email="owner2@second.com",
            password="ValidPassword123!",
        )
    assert "duplicate-slug" in exc_info.value.message

    # Ensure original tenant remains unchanged
    tenants = db_session.query(Tenant).filter(Tenant.slug == "duplicate-slug").all()
    assert len(tenants) == 1
    assert tenants[0].name == "First Corp"


# ─── 5. Rejection of Account Conflict Without Adoption ────────────────────────


def test_bootstrap_rejects_existing_account_conflict(db_session: Session):
    # Setup an existing tenant and owner
    t = create_tenant(db_session, name="Existing Org", slug="existing-org")
    u, m = create_user(
        db_session,
        tenant_id=t.id,
        email="existing_owner@existing.com",
        password="OriginalPassword123!",
        role="owner",
    )
    db_session.commit()
    orig_hash = u.hashed_password

    # Attempt to bootstrap again with same slug and same email
    with pytest.raises(ConflictError):
        bootstrap_tenant_and_owner(
            db_session,
            name="Existing Org",
            slug="existing-org",
            email="existing_owner@existing.com",
            password="NewPassword123!",
        )

    # Re-verify original user and password hash were not modified
    db_session.expire_all()
    user_after = db_session.query(User).filter(User.id == u.id).one()
    assert user_after.hashed_password == orig_hash
    assert user_after.email == "existing_owner@existing.com"


# ─── 6. Rejection of Invalid Slugs and Emails ─────────────────────────────────


@pytest.mark.parametrize(
    "invalid_slug",
    [
        "",
        "   ",
        "has spaces",
        "UPPERCASE",  # slug normalization handles lower, but let's test invalid chars
        "invalid!chars@",
        "-starts-with-hyphen",
        "ends-with-hyphen-",
        "x",  # too short
        "a" * 65,  # too long (> 64)
    ],
)
def test_bootstrap_rejects_invalid_slugs(db_session: Session, invalid_slug: str):
    with pytest.raises(ValidationError):
        bootstrap_tenant_and_owner(
            db_session,
            name="Valid Name",
            slug=invalid_slug,
            email="owner@example.com",
            password="ValidPassword123!",
        )


@pytest.mark.parametrize(
    "invalid_email",
    [
        "",
        "   ",
        "no-at-sign",
        "missing@domain",
        "@nodomain.com",
        "spaces in@email.com",
        ("a" * 315) + "@example.com",  # > 320 chars
    ],
)
def test_bootstrap_rejects_invalid_emails(db_session: Session, invalid_email: str):
    with pytest.raises(ValidationError):
        bootstrap_tenant_and_owner(
            db_session,
            name="Valid Name",
            slug="valid-slug",
            email=invalid_email,
            password="ValidPassword123!",
        )


# ─── 7. Transaction Rollback on Failure (Atomicity) ───────────────────────────


def test_bootstrap_rolls_back_on_user_creation_failure(db_session: Session):
    with patch("app.tenancy.service.create_user", side_effect=RuntimeError("Database failure")):
        with pytest.raises(RuntimeError):
            bootstrap_tenant_and_owner(
                db_session,
                name="Rollback Org",
                slug="rollback-org",
                email="owner@rollback.com",
                password="ValidPassword123!",
            )

    # Verify tenant creation was rolled back
    assert db_session.query(Tenant).filter(Tenant.slug == "rollback-org").first() is None


# ─── 8. CLI Execution, Stdin Password, and Secret Sanitization ────────────────


def test_cli_bootstrap_success_with_password_stdin(
    db_engine, session_factory, monkeypatch, capsys
):
    monkeypatch.setattr(db_mod, "get_engine", lambda: db_engine)
    monkeypatch.setattr(db_mod, "get_session_factory", lambda: session_factory)

    test_password = "SecretPassword123!Safe"
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"{test_password}\n"))

    exit_code = execute_bootstrap(
        [
            "--name", "CLI Corporation",
            "--slug", "cli-corp",
            "--email", "cli_owner@cli-corp.com",
            "--full-name", "CLI Admin",
            "--password-stdin",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "[SUCCESS] Organization and initial owner account successfully created." in captured.out
    assert "Tenant Slug:  cli-corp" in captured.out
    assert "Owner Email:  cli_owner@cli-corp.com" in captured.out

    # Secret sanitization assertions: password and hashes must NEVER appear in output
    assert test_password not in captured.out
    assert test_password not in captured.err
    assert "$2b$" not in captured.out
    assert "$2b$" not in captured.err


def test_cli_bootstrap_dry_run_does_not_persist(
    db_engine, session_factory, monkeypatch, capsys
):
    monkeypatch.setattr(db_mod, "get_engine", lambda: db_engine)
    monkeypatch.setattr(db_mod, "get_session_factory", lambda: session_factory)

    exit_code = execute_bootstrap(
        [
            "--name", "DryRun Org",
            "--slug", "dry-run-org",
            "--email", "dryrun@org.com",
            "--dry-run",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "[DRY-RUN] Preflight validation passed. No database changes were made." in captured.out

    # Verify no records were persisted in the database
    with db_mod.get_db_context() as db:
        assert db.query(Tenant).filter(Tenant.slug == "dry-run-org").first() is None


def test_cli_bootstrap_rejects_missing_password_in_non_interactive_mode(
    db_engine, session_factory, monkeypatch, capsys
):
    monkeypatch.setattr(db_mod, "get_engine", lambda: db_engine)
    monkeypatch.setattr(db_mod, "get_session_factory", lambda: session_factory)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    exit_code = execute_bootstrap(
        [
            "--name", "NonInteractive Org",
            "--slug", "noninteractive-org",
            "--email", "owner@noninteractive.com",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Error: Non-interactive execution requires --password-stdin" in captured.err


# ─── 9. Database Unavailable Handling ─────────────────────────────────────────


def test_cli_bootstrap_database_unavailable_handling(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("ValidPassword123!\n"))
    monkeypatch.setattr(
        db_mod,
        "get_session_factory",
        lambda: (_ for _ in ()).throw(OperationalError("FATAL: connection to server on socket failed", None, None)),
    )

    exit_code = execute_bootstrap(
        [
            "--name", "Offline Org",
            "--slug", "offline-org",
            "--email", "offline@org.com",
            "--password-stdin",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Error: Database connection failed." in captured.err
    # Verify raw database URLs or internal credentials are NOT leaked
    assert "postgresql://" not in captured.err
    assert "FATAL" not in captured.err


# ─── 10. PostgreSQL Concurrency and Database Constraint Test ──────────────────


def _is_pg_reachable() -> bool:
    try:
        settings = get_settings()
        if not settings.database_url.startswith("postgresql"):
            return False
        engine = create_engine(settings.database_url, connect_args={"connect_timeout": 3})
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        return True
    except Exception:
        return False


pg_available = _is_pg_reachable()


@pytest.mark.skipif(
    not pg_available,
    reason="PostgreSQL database unavailable or not configured. Skipping real PostgreSQL bootstrap constraint test.",
)
def test_postgres_concurrent_bootstrap_unique_slug_race():
    """Verify that concurrent bootstrap invocations against PostgreSQL respect unique constraints."""
    import concurrent.futures

    settings = get_settings()
    schema_name = f"test_boot_{uuid.uuid4().hex[:8]}"

    admin_engine = create_engine(settings.database_url, pool_pre_ping=True)
    with admin_engine.connect() as conn:
        conn.execute(text(f"CREATE SCHEMA {schema_name}"))
        conn.commit()

    pg_engine = create_engine(
        settings.database_url,
        connect_args={"options": f"-csearch_path={schema_name}"},
        pool_pre_ping=True,
    )
    Base.metadata.create_all(bind=pg_engine)
    SessionFactory = sessionmaker(bind=pg_engine, autocommit=False, autoflush=False)

    shared_slug = f"shared-{uuid.uuid4().hex[:6]}"

    def _worker(worker_id: int):
        db = SessionFactory()
        try:
            res = bootstrap_tenant_and_owner(
                db,
                name=f"Worker {worker_id} Org",
                slug=shared_slug,
                email=f"owner_{worker_id}@shared.com",
                password="ValidPassword123!",
            )
            db.commit()
            return "created"
        except ConflictError:
            db.rollback()
            return "conflict"
        finally:
            db.close()

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(_worker, [1, 2]))

        assert sorted(results) == ["conflict", "created"]

        with SessionFactory() as verify_session:
            tenants = verify_session.query(Tenant).filter(Tenant.slug == shared_slug).all()
            assert len(tenants) == 1
    finally:
        pg_engine.dispose()
        with admin_engine.connect() as conn:
            conn.execute(text(f"DROP SCHEMA IF EXISTS {schema_name} CASCADE"))
            conn.commit()
        admin_engine.dispose()
