"""Pytest fixtures for the Channel Yield Analytics Agent test suite."""
from __future__ import annotations

from typing import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import app.persistence.database as db_mod
from app.core.config import Settings
from app.main import create_app
from app.persistence.database import Base


@pytest.fixture
def test_settings() -> Settings:
    return Settings(
        env="testing",
        database_url="sqlite+pysqlite:///:memory:",
        secret_key="a" * 32,
        encryption_key="fake-fernet-key-for-testing-only-12345",
        allowed_origins="http://localhost:3000",
        legacy_api_token="",
        legacy_client_id="",
        legacy_db_path="",
    )


@pytest.fixture
def db_engine(test_settings: Settings):
    engine = create_engine(
        test_settings.database_url,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(db_engine):
    return sessionmaker(bind=db_engine, autocommit=False, autoflush=False, expire_on_commit=False)


@pytest.fixture
def db_session(session_factory) -> Generator[Session, None, None]:
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@pytest.fixture
def client(test_settings: Settings, db_engine, session_factory, monkeypatch) -> Generator[TestClient, None, None]:
    monkeypatch.setattr(db_mod, "get_engine", lambda: db_engine)
    monkeypatch.setattr(db_mod, "get_session_factory", lambda: session_factory)

    app = create_app(test_settings)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
