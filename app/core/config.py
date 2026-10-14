"""Application configuration with Pydantic Settings.

All configuration is read from environment variables and/or a .env file.
The application raises a clear error at startup if mandatory production
settings are missing or insecure.

Never import this module inside a test fixture without explicitly overriding
settings — use the test_settings fixture from conftest.py instead.
"""
from __future__ import annotations

import secrets
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Centralised, validated configuration for every runtime environment."""

    model_config = SettingsConfigDict(
        env_prefix="CY_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # Ignore unknown env vars silently
    )

    # ── Runtime environment ────────────────────────────────────────────────
    env: Literal["development", "testing", "staging", "production"] = Field(
        default="development",
        description="Runtime environment name.",
    )

    # ── Server ────────────────────────────────────────────────────────────
    host: str = Field(default="127.0.0.1")
    port: int = Field(default=8080, ge=1, le=65535)

    # ── Database ──────────────────────────────────────────────────────────
    database_url: str = Field(
        default="sqlite+pysqlite:///./dev.sqlite",
        description=(
            "SQLAlchemy database URL. "
            "Production must use postgresql+psycopg2://..."
        ),
    )

    # ── Authentication ────────────────────────────────────────────────────
    secret_key: str = Field(
        default="",
        description="JWT signing key. Must be at least 32 random bytes (hex).",
    )
    access_token_expire_minutes: int = Field(default=30, ge=1, le=1440)
    jwt_algorithm: str = Field(default="HS256")

    # ── Encryption ────────────────────────────────────────────────────────
    encryption_key: str = Field(
        default="",
        description="Fernet encryption key for stored OAuth tokens and API keys.",
    )

    # ── Legacy single-tenant backward-compat ──────────────────────────────
    legacy_api_token: str = Field(default="")
    legacy_client_id: str = Field(default="")
    legacy_db_path: str = Field(default="")

    # ── CORS ──────────────────────────────────────────────────────────────
    allowed_origins: str = Field(
        default="http://localhost:3000",
        description="Comma-separated list of allowed CORS origins.",
    )

    # ── Zoho Recruit ──────────────────────────────────────────────────────
    zoho_client_id: str = Field(default="")
    zoho_client_secret: str = Field(default="")
    zoho_redirect_uri: str = Field(default="")
    zoho_data_center: str = Field(default="com")

    # ── LLM provider ──────────────────────────────────────────────────────
    llm_provider: Literal["openai", "anthropic", "none"] = Field(default="none")
    llm_api_key: str = Field(default="")
    llm_model: str = Field(default="gpt-4o-mini")

    # ── Upload limits ─────────────────────────────────────────────────────
    max_upload_bytes: int = Field(default=52_428_800, ge=1)   # 50 MB
    max_csv_rows: int = Field(default=100_000, ge=1)

    # ── Rate limits ───────────────────────────────────────────────────────
    rate_limit_anon: int = Field(default=60, ge=1)
    rate_limit_authed: int = Field(default=300, ge=1)

    # ── Logging ───────────────────────────────────────────────────────────
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(default="INFO")

    # ── Derived helpers ───────────────────────────────────────────────────
    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def is_testing(self) -> bool:
        return self.env == "testing"

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith("postgresql")

    @property
    def legacy_enabled(self) -> bool:
        return bool(self.legacy_api_token and self.legacy_client_id and self.legacy_db_path)

    # ── Validation ────────────────────────────────────────────────────────
    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, v: str) -> str:
        if not v:
            raise ValueError("CY_DATABASE_URL must be set.")
        return v

    @model_validator(mode="after")
    def production_guards(self) -> "Settings":
        """Refuse to start in production with insecure defaults."""
        if self.env != "production":
            return self

        errors: list[str] = []

        if not self.secret_key or len(self.secret_key) < 32:
            errors.append(
                "CY_SECRET_KEY must be at least 32 characters in production. "
                "Generate with: python -c \"import secrets; print(secrets.token_hex(32))\""
            )

        if "sqlite" in self.database_url:
            errors.append(
                "CY_DATABASE_URL must use PostgreSQL in production, not SQLite."
            )

        if not self.encryption_key:
            errors.append(
                "CY_ENCRYPTION_KEY must be set in production. "
                "Generate with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
            )

        if any(origin.strip() == "*" for origin in self.cors_origins):
            errors.append(
                "CY_ALLOWED_ORIGINS cannot contain wildcard '*' in production when credentials are supported."
            )

        if errors:
            raise ValueError(
                "Production configuration is invalid:\n" + "\n".join(f"  • {e}" for e in errors)
            )

        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached application settings singleton.

    In tests, clear the cache and supply a test-specific Settings instance
    via dependency injection rather than calling this function directly.
    """
    return Settings()
