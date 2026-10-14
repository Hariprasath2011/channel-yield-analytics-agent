"""SQLAlchemy database engine and session factory.

Production use:
    - PostgreSQL via psycopg2.
    - Connection pooling via SQLAlchemy's built-in pool.
    - All sessions are scoped to a request via FastAPI dependency injection.

Testing:
    - SQLite in-memory per test, created by overriding get_settings().
    - Tests MUST use a separate engine/session factory; they must not share
      the global engine with application code.

Never call engine.dispose() inside a request handler.
"""
from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from typing import AsyncGenerator

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, MappedColumn, Session, sessionmaker

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""
    pass


def _build_engine(database_url: str | None = None):
    """Create a SQLAlchemy engine with appropriate settings.

    For PostgreSQL:
        - Pool size 10, overflow 20, recycle 1800s.
        - Connection health is verified on checkout (pool_pre_ping).

    For SQLite (testing / demo only):
        - WAL mode is enabled for concurrent readers.
        - Foreign-key enforcement is explicitly turned on.
    """
    settings = get_settings()
    url = database_url or settings.database_url

    if url.startswith("postgresql"):
        engine = create_engine(
            url,
            pool_size=10,
            max_overflow=20,
            pool_recycle=1800,
            pool_pre_ping=True,      # Verify connections on checkout
            pool_timeout=30,
            connect_args={"connect_timeout": 10},
            echo=settings.log_level == "DEBUG",
        )
    else:
        # SQLite — for local dev and testing only.
        engine = create_engine(
            url,
            connect_args={"check_same_thread": False},
            echo=settings.log_level == "DEBUG",
        )

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(dbapi_conn, _record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    logger.info(
        "database.engine_created",
        backend="postgresql" if url.startswith("postgresql") else "sqlite",
    )
    return engine


# Module-level engine and session factory (created lazily).
_engine = None
_SessionLocal = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = _build_engine()
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(
            bind=get_engine(),
            autocommit=False,
            autoflush=False,
            expire_on_commit=False,
        )
    return _SessionLocal


def get_db() -> Session:
    """FastAPI dependency: yield a database session for a single request.

    Automatically commits on success and rolls back on exception.
    Always closes the session when the request is done.
    """
    SessionLocal = get_session_factory()
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def check_db_connectivity() -> bool:
    """Return True if the database is reachable; used by the readiness check."""
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        logger.error("database.connectivity_check_failed", error=str(exc))
        return False


@contextmanager
def get_db_context():
    """Synchronous context manager for CLI scripts, operational tasks, and migrations."""
    SessionLocal = get_session_factory()
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
