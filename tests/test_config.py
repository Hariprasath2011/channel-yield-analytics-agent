"""Tests for application configuration and production safety guards."""
import pytest
from app.core.config import Settings


def test_development_config_defaults():
    settings = Settings(
        env="development",
        database_url="sqlite+pysqlite:///./dev.sqlite",
    )
    assert settings.env == "development"
    assert not settings.is_production
    assert "sqlite" in settings.database_url


def test_production_guards_missing_secret_key():
    with pytest.raises(ValueError, match="CY_SECRET_KEY must be at least 32 characters in production"):
        Settings(
            env="production",
            database_url="postgresql+psycopg2://user:pass@localhost:5432/dbname",
            secret_key="short",
            encryption_key="valid-key",
        )


def test_production_guards_sqlite_rejected():
    with pytest.raises(ValueError, match="CY_DATABASE_URL must use PostgreSQL in production, not SQLite"):
        Settings(
            env="production",
            database_url="sqlite+pysqlite:///./prod.sqlite",
            secret_key="a" * 32,
            encryption_key="valid-key",
        )


def test_production_guards_missing_encryption_key():
    with pytest.raises(ValueError, match="CY_ENCRYPTION_KEY must be set in production"):
        Settings(
            env="production",
            database_url="postgresql+psycopg2://user:pass@localhost:5432/dbname",
            secret_key="a" * 32,
            encryption_key="",
        )


def test_production_valid_configuration():
    settings = Settings(
        env="production",
        database_url="postgresql+psycopg2://user:pass@dbhost:5432/proddb",
        secret_key="a" * 32,
        encryption_key="fernet-secret-encryption-key-xyz",
    )
    assert settings.is_production
    assert settings.is_postgres


def test_production_guards_wildcard_cors_rejected():
    with pytest.raises(ValueError, match="CY_ALLOWED_ORIGINS cannot contain wildcard"):
        Settings(
            env="production",
            database_url="postgresql+psycopg2://user:pass@localhost:5432/dbname",
            secret_key="a" * 32,
            encryption_key="valid-key",
            allowed_origins="*",
        )
