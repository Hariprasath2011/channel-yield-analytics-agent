"""Tests for Alembic database migration reproducibility."""
import os
import tempfile
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


def test_alembic_migration_lifecycle():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        db_path = Path(td) / "migration_test.sqlite"
        db_url = f"sqlite+pysqlite:///{db_path}"

        alembic_cfg = Config("alembic.ini")
        alembic_cfg.set_main_option("sqlalchemy.url", db_url)

        # Upgrade to head
        command.upgrade(alembic_cfg, "head")

        # Verify all tables exist
        engine = create_engine(db_url)
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())

        expected_tables = {
            "tenants",
            "users",
            "memberships",
            "api_keys",
            "zoho_integrations",
            "oauth_states",
            "import_jobs",
            "sync_jobs",
            "canonical_records",
            "audit_events",
            "alembic_version",
        }
        assert expected_tables.issubset(tables), f"Missing tables: {expected_tables - tables}"
        engine.dispose()

        # Downgrade back to base
        command.downgrade(alembic_cfg, "base")

        # Verify tables dropped
        engine2 = create_engine(db_url)
        inspector_after = inspect(engine2)
        tables_after = set(inspector_after.get_table_names())
        assert "tenants" not in tables_after
        assert "canonical_records" not in tables_after
        engine2.dispose()
