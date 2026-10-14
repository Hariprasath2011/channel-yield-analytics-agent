import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.core.config import get_settings
from app.persistence.database import Base
import app.persistence.models  # Ensure all models are registered on Base.metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _is_valid_url(url: str | None) -> bool:
    """Check if the provided database URL is non-empty and not a placeholder."""
    if not url:
        return False
    url_clean = url.strip()
    if not url_clean:
        return False
    if url_clean.startswith("driver://") or "user:pass@localhost/dbname" in url_clean:
        return False
    return True


def get_url() -> str:
    """Resolve database URL in priority order:
    1. Explicitly configured, valid 'sqlalchemy.url' from Alembic Config.
    2. Alembic CLI -x arguments (e.g. -x url=...).
    3. CY_DATABASE_URL process environment variable.
    4. Application settings (e.g. .env or runtime environment settings).
    Raises ValueError if no valid database URL is configured.
    """
    # 1. Explicitly configured, valid sqlalchemy.url from Alembic Config
    cfg_url = config.get_main_option("sqlalchemy.url")
    if _is_valid_url(cfg_url):
        return cfg_url

    # 2. Alembic CLI -x arguments
    try:
        x_args = context.get_x_argument(as_dictionary=True)
        x_url = x_args.get("url") or x_args.get("db_url")
        if _is_valid_url(x_url):
            return x_url
    except Exception:
        pass

    # 3. CY_DATABASE_URL environment variable
    env_url = os.environ.get("CY_DATABASE_URL")
    if _is_valid_url(env_url):
        return env_url

    # 4. Application settings
    try:
        settings_url = get_settings().database_url
        if _is_valid_url(settings_url):
            return settings_url
    except Exception:
        pass

    raise ValueError(
        "No valid database URL configured for Alembic migrations. "
        "Please provide an explicit 'sqlalchemy.url' in Alembic Config or set 'CY_DATABASE_URL'."
    )


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,  # Crucial for SQLite compatibility in tests/dev
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    configuration = config.get_section(config.config_ini_section, {}) or {}
    configuration["sqlalchemy.url"] = get_url()

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,  # Crucial for SQLite batch alter support
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
