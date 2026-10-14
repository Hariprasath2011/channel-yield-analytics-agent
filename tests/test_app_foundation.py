"""Tests for FastAPI foundation: health/readiness, correlation IDs, error safety, and limits."""
import pytest
from fastapi.testclient import TestClient

from app.core.exceptions import NotFoundError
from app.main import create_app


def test_health_liveness_probe(client: TestClient):
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["service"] == "channel-yield-analytics-agent"
    assert "version" in data
    assert "environment" in data


def test_readiness_probe_success(client: TestClient):
    response = client.get("/ready")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ready"
    assert data["database"] == "connected"


def test_readiness_probe_database_failure(test_settings, monkeypatch):
    import app.main as main_mod

    # Simulate database connectivity failure
    monkeypatch.setattr(main_mod, "check_db_connectivity", lambda: False)

    app = create_app(test_settings)
    with TestClient(app) as test_c:
        response = test_c.get("/ready")
        assert response.status_code == 503
        data = response.json()
        assert data["status"] == "not_ready"
        assert data["database"] == "disconnected"


def test_correlation_id_generation_when_missing(client: TestClient):
    response = client.get("/health")
    assert response.status_code == 200
    req_id = response.headers.get("X-Request-ID")
    assert req_id is not None
    assert req_id.startswith("req_")


def test_correlation_id_preserved_when_valid(client: TestClient):
    custom_id = "trace-client-abc-123456"
    response = client.get("/health", headers={"X-Request-ID": custom_id})
    assert response.status_code == 200
    assert response.headers.get("X-Request-ID") == custom_id


def test_correlation_id_sanitized_when_malicious_or_invalid(client: TestClient):
    invalid_ids = [
        "<script>alert(1)</script>",
        "has spaces in header",
        "too_long_" + ("a" * 70),
        "short",  # < 8 chars
        "invalid@char!",
    ]
    for bad_id in invalid_ids:
        response = client.get("/health", headers={"X-Request-ID": bad_id})
        assert response.status_code == 200
        assigned_id = response.headers.get("X-Request-ID")
        assert assigned_id != bad_id
        assert assigned_id.startswith("req_")


def test_request_size_limit_rejection(client: TestClient):
    # Standard endpoint limit is 64 KB (65,536 bytes)
    oversized_length = 70_000
    response = client.post(
        "/health",
        headers={"Content-Length": str(oversized_length)},
        content="x" * 100,
    )
    assert response.status_code == 413
    data = response.json()
    assert data["error"] == "payload_too_large"


def test_safe_error_handling_unhandled_exception(test_settings):
    app = create_app(test_settings)

    @app.get("/test-internal-error")
    async def buggy_endpoint():
        # Raise unexpected error with sensitive details
        raise RuntimeError("FATAL: Database connection string postgresql://admin:SECRET_PASSWORD@db:5432 failed!")

    with TestClient(app, raise_server_exceptions=False) as test_c:
        response = test_c.get("/test-internal-error")
        assert response.status_code == 500
        data = response.json()
        assert data["error"] == "internal_error"
        # Secret password and stack trace must NOT be leaked
        assert "SECRET_PASSWORD" not in str(data)
        assert "Traceback" not in str(data)
        assert "request_id" in data
        assert data["request_id"].startswith("req_")


def test_safe_error_handling_domain_exception(test_settings):
    app = create_app(test_settings)

    @app.get("/test-domain-error")
    async def not_found_endpoint():
        raise NotFoundError("Requisition")

    with TestClient(app, raise_server_exceptions=False) as test_c:
        response = test_c.get("/test-domain-error")
        assert response.status_code == 404
        data = response.json()
        assert data["error"] == "not_found"
        assert data["message"] == "Requisition not found."
