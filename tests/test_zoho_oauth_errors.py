"""Tests for Zoho Recruit OAuth scopes and defensive error classification.

Covers:
1. Exact lowercase settings scope strings (ZohoRecruit.settings.modules.read, ZohoRecruit.settings.fields.read).
2. Ambiguous HTTP 401 (empty body, generic unauthorized, unknown code).
3. Explicit expired-token HTTP 401 (INVALID_TOKEN / 'invalid oauth token', EXPIRED_TOKEN).
4. Generic ACCESS_DENIED without scope evidence (must NOT raise OAuthScopeError).
5. Scope denial with explicit evidence (raises OAuthScopeError with code oauth_scope_denied).
6. Sensitive content in Zoho error messages (verifying credentials/tokens never leak into exception messages).
7. Existing HTTP 204 handling (returns empty data structure without error).
8. Token refresh behavior and caller compatibility.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
import httpx
import pytest

import app.persistence.database as db_mod
from app.core.exceptions import (
    IntegrationError,
    OAuthError,
    OAuthScopeError,
    RateLimitError,
)
from app.integrations.zoho.client import (
    DEFAULT_SCOPES,
    MAX_RETRY_AFTER_SECONDS,
    ZohoRecruitClient,
    parse_retry_after,
)
from app.integrations.zoho.crypto import decrypt_token, encrypt_token
from app.persistence.models import Tenant, ZohoIntegration
from mcp_server.zoho_recruit import ZohoRecruitMCPHandler


@pytest.fixture
def zoho_client():
    return ZohoRecruitClient(
        client_id="test_client_id_xyz",
        client_secret="test_client_secret_xyz",
        redirect_uri="https://app.example.com/v2/integrations/zoho/callback",
        data_center="in",
    )


# ─── 1. Exact Lowercase Settings Scopes ──────────────────────────────────────


def test_default_scopes_contain_exact_lowercase_settings_scopes():
    """Verify that DEFAULT_SCOPES contains exact official lowercase settings scopes."""
    assert "ZohoRecruit.settings.modules.read" in DEFAULT_SCOPES
    assert "ZohoRecruit.settings.fields.read" in DEFAULT_SCOPES

    # Ensure uppercase versions are strictly absent
    assert "ZohoRecruit.settings.modules.READ" not in DEFAULT_SCOPES
    assert "ZohoRecruit.settings.fields.READ" not in DEFAULT_SCOPES

    # Ensure existing recruitment module scopes are preserved
    assert "ZohoRecruit.modules.jobopening.READ" in DEFAULT_SCOPES
    assert "ZohoRecruit.modules.candidate.READ" in DEFAULT_SCOPES
    assert "ZohoRecruit.modules.application.READ" in DEFAULT_SCOPES

    assert DEFAULT_SCOPES == [
        "ZohoRecruit.modules.jobopening.READ",
        "ZohoRecruit.modules.candidate.READ",
        "ZohoRecruit.modules.application.READ",
        "ZohoRecruit.settings.modules.read",
        "ZohoRecruit.settings.fields.read",
    ]


def test_authorization_url_contains_exact_lowercase_scopes(zoho_client):
    """Verify authorization URL includes exact lowercase settings scopes in order."""
    auth_url = zoho_client.get_authorization_url(state="csrf_state_456")
    parsed = urlparse(auth_url)
    params = parse_qs(parsed.query)

    assert "scope" in params
    requested_scopes = params["scope"][0].split(",")
    assert requested_scopes == [
        "ZohoRecruit.modules.jobopening.READ",
        "ZohoRecruit.modules.candidate.READ",
        "ZohoRecruit.modules.application.READ",
        "ZohoRecruit.settings.modules.read",
        "ZohoRecruit.settings.fields.read",
    ]


# ─── 2. Ambiguous HTTP 401 Classification ────────────────────────────────────


@pytest.mark.parametrize(
    "ambiguous_body",
    [
        b"",                                      # Empty body
        b"Unauthorized",                          # Plain text
        b"<html><body>401 Unauthorized</body></html>", # HTML body
        b"{malformed json",                       # Malformed JSON
        b'{"code": "UNKNOWN_ERROR"}',             # Unknown code without token info
        b'{"error": "unauthorized"}',             # Generic error string
    ],
)
def test_http_401_ambiguous_returns_generic_auth_error(ambiguous_body):
    """Verify ambiguous 401 responses raise generic OAuthError, NOT expired token or scope denial."""
    mock_transport = httpx.MockTransport(
        lambda req: httpx.Response(401, content=ambiguous_body)
    )
    client = ZohoRecruitClient(
        "client_id", "secret", "https://cb.test", "in",
        http_client=httpx.Client(transport=mock_transport),
    )

    with pytest.raises(OAuthError) as exc_info:
        client.get_module_metadata("mock_token")

    # Must NOT be classified as scope denial
    assert not isinstance(exc_info.value, OAuthScopeError)
    # Must NOT claim token is expired when ambiguous
    assert "Zoho authentication failed (HTTP 401)." in str(exc_info.value)
    assert exc_info.value.code == "oauth_error"


# ─── 3. Explicit Expired/Invalid Token HTTP 401 ──────────────────────────────


@pytest.mark.parametrize(
    "explicit_payload",
    [
        {"code": "INVALID_TOKEN", "message": "invalid oauth token"},
        {"code": "INVALID_TOKEN", "message": "token has expired"},
        {"code": "EXPIRED_TOKEN", "message": "Token expired"},
        {"code": "TOKEN_EXPIRED", "message": "Access token expired"},
        {"code": "AUTHENTICATION_FAILURE", "message": "Invalid token"},
    ],
)
def test_http_401_explicit_expired_token_classified_correctly(explicit_payload):
    """Verify explicit token invalidation/expiration raises 'Zoho access token is invalid or expired.'."""
    mock_transport = httpx.MockTransport(
        lambda req: httpx.Response(401, json=explicit_payload)
    )
    client = ZohoRecruitClient(
        "client_id", "secret", "https://cb.test", "in",
        http_client=httpx.Client(transport=mock_transport),
    )

    with pytest.raises(OAuthError) as exc_info:
        client.get_module_records("JobOpenings", "mock_token")

    assert not isinstance(exc_info.value, OAuthScopeError)
    assert "Zoho access token is invalid or expired." in str(exc_info.value)


# ─── 4. Generic ACCESS_DENIED Without Scope Evidence ──────────────────────────


def test_generic_access_denied_401_does_not_infer_scope_denial():
    """Verify generic ACCESS_DENIED on 401 without scope evidence does NOT raise OAuthScopeError."""
    generic_access_denied = {
        "code": "ACCESS_DENIED",
        "message": "Access denied",
        "status": "error",
    }
    mock_transport = httpx.MockTransport(
        lambda req: httpx.Response(401, json=generic_access_denied)
    )
    client = ZohoRecruitClient(
        "client_id", "secret", "https://cb.test", "in",
        http_client=httpx.Client(transport=mock_transport),
    )

    with pytest.raises(OAuthError) as exc_info:
        client.get_module_metadata("mock_token")

    # MUST NOT be OAuthScopeError because no scope evidence exists
    assert not isinstance(exc_info.value, OAuthScopeError)
    assert "Zoho authentication failed (HTTP 401)." in str(exc_info.value)


def test_generic_access_denied_403_raises_integration_error():
    """Verify generic ACCESS_DENIED on 403 raises IntegrationError, NOT OAuthScopeError."""
    generic_access_denied = {
        "code": "ACCESS_DENIED",
        "message": "Access denied",
        "status": "error",
    }
    mock_transport = httpx.MockTransport(
        lambda req: httpx.Response(403, json=generic_access_denied)
    )
    client = ZohoRecruitClient(
        "client_id", "secret", "https://cb.test", "in",
        http_client=httpx.Client(transport=mock_transport),
    )

    with pytest.raises(IntegrationError) as exc_info:
        client.get_module_metadata("mock_token")

    assert not isinstance(exc_info.value, OAuthScopeError)
    assert "HTTP 403 Forbidden" in str(exc_info.value)


# ─── 5. Scope Denial With Explicit Evidence ───────────────────────────────────


@pytest.mark.parametrize(
    "status_code,scope_payload",
    [
        (401, {"code": "INVALID_TOKEN", "message": "Access denied for this scope"}),
        (401, {"code": "OAUTH_SCOPE_MISMATCH", "message": "Scope mismatch"}),
        (401, {"code": "INSUFFICIENT_SCOPE", "message": "Missing required scope"}),
        (403, {"code": "OAUTH_SCOPE_MISMATCH", "message": "Access denied for this scope"}),
        (403, {"code": "INVALID_OAUTH", "message": "Scope permission denied"}),
    ],
)
def test_explicit_scope_denial_raises_oauth_scope_error(status_code, scope_payload):
    """Verify explicit scope mismatch evidence raises OAuthScopeError with fixed safe message."""
    mock_transport = httpx.MockTransport(
        lambda req: httpx.Response(status_code, json=scope_payload)
    )
    client = ZohoRecruitClient(
        "client_id", "secret", "https://cb.test", "in",
        http_client=httpx.Client(transport=mock_transport),
    )

    with pytest.raises(OAuthScopeError) as exc_info:
        client.get_module_metadata("mock_token")

    assert isinstance(exc_info.value, OAuthError)
    assert exc_info.value.code == "oauth_scope_denied"
    assert str(exc_info.value) == (
        "Zoho API access denied: missing or insufficient OAuth scope. Re-authorization is required."
    )


# ─── 6. Sensitive Content In Zoho Messages Is Never Leaked ───────────────────


def test_sensitive_content_in_zoho_error_never_leaks():
    """Verify arbitrary Zoho error messages, tokens, passwords, or PII never appear in exceptions."""
    sensitive_leak = {
        "code": "INVALID_TOKEN",
        "message": "Token secret_token_12345 invalid for user admin@corp.com password=mySecretPassword123",
        "error_description": "Raw trace /var/secrets/zoho_api_key.json",
    }
    mock_transport = httpx.MockTransport(
        lambda req: httpx.Response(401, json=sensitive_leak)
    )
    client = ZohoRecruitClient(
        "client_id", "secret", "https://cb.test", "in",
        http_client=httpx.Client(transport=mock_transport),
    )

    with pytest.raises(OAuthError) as exc_info:
        client.get_module_records("Candidates", "caller_token")

    err_text = str(exc_info.value)
    # The message MUST be fixed and sanitized
    assert err_text == "Zoho access token is invalid or expired."
    assert "secret_token_12345" not in err_text
    assert "admin@corp.com" not in err_text
    assert "mySecretPassword123" not in err_text
    assert "/var/secrets" not in err_text


# ─── 7. HTTP 204 Empty Handling & Auto-Refresh Behavior ───────────────────────


def test_http_204_empty_dataset_handling():
    """Verify HTTP 204 returns expected empty dataset structure."""
    mock_transport = httpx.MockTransport(
        lambda req: httpx.Response(204)
    )
    client = ZohoRecruitClient(
        "client_id", "secret", "https://cb.test", "in",
        http_client=httpx.Client(transport=mock_transport),
    )

    records = client.get_module_records("Applications", "mock_token", page=1, per_page=50)
    assert records["data"] == []
    assert records["info"]["page"] == 1
    assert records["info"]["more_records"] is False


@pytest.fixture(autouse=True)
def bind_test_session_factory(session_factory, monkeypatch):
    """Ensure get_db_context() in MCP handler uses test in-memory SQLite database."""
    monkeypatch.setattr(db_mod, "get_session_factory", lambda: session_factory)


def test_token_auto_refresh_persists_in_mcp(db_session):
    """Verify expired token in MCP handler auto-refreshes and persists without regression."""
    tenant = Tenant(
        id="mcp-refresh-test-01",
        slug="mcp-refresh-slug",
        name="MCP Refresh Organization",
        is_active=True,
    )
    db_session.add(tenant)
    db_session.flush()

    integration = ZohoIntegration(
        id="zoho-refresh-int-01",
        tenant_id=tenant.id,
        data_center="in",
        status="active",
        encrypted_access_token=encrypt_token("old_expired_access_token"),
        encrypted_refresh_token=encrypt_token("valid_refresh_token_xyz"),
        token_expires_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    db_session.add(integration)
    db_session.commit()

    handler = ZohoRecruitMCPHandler(tenant_id=tenant.id)

    mock_refresh_response = {
        "access_token": "newly_refreshed_access_token_123",
        "expires_in": 3600,
        "api_domain": "https://recruit.zoho.in",
    }

    with patch("app.integrations.zoho.client.ZohoRecruitClient.refresh_access_token", return_value=mock_refresh_response) as mock_refresh:
        with patch("app.integrations.zoho.client.ZohoRecruitClient.get_module_records", return_value={"data": [], "info": {}}):
            res = handler.list_job_openings()
            assert "job_openings" in res
            assert mock_refresh.called

    # Verify encrypted new token was persisted
    db_session.expire_all()
    updated = db_session.query(ZohoIntegration).filter(ZohoIntegration.tenant_id == tenant.id).one()
    decrypted = decrypt_token(updated.encrypted_access_token)
    assert decrypted == "newly_refreshed_access_token_123"
    exp = updated.token_expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    assert exp > datetime.now(timezone.utc)


# ─── 8. Defensive Bounded Retry-After Parsing ─────────────────────────────────


@pytest.mark.parametrize(
    "raw_input,expected_delay",
    [
        # Missing values -> default 60s
        (None, 60),
        ("", 60),
        ("   ", 60),
        # Negative and zero values -> default 60s
        (-10, 60),
        ("-10", 60),
        (-1, 60),
        (0, 60),
        ("0", 60),
        # Decimals (rejected by strict integer delta-seconds format) -> default 60s
        ("45.5", 60),
        ("45.0", 60),
        (45.5, 60),
        (0.5, 60),
        ("-45.5", 60),
        # NaN and non-finite values -> default 60s
        ("NaN", 60),
        ("nan", 60),
        (float("nan"), 60),
        # Infinity values -> default 60s
        ("Infinity", 60),
        ("-Infinity", 60),
        ("inf", 60),
        (float("inf"), 60),
        (float("-inf"), 60),
        # Malformed / non-numeric values -> default 60s
        ("malformed", 60),
        ("not_a_number", 60),
        ([120], 60),
        ({"delay": 30}, 60),
        (True, 60),
        (False, 60),
        # Excessively large values -> capped at MAX_RETRY_AFTER_SECONDS (300s)
        (301, 300),
        ("301", 300),
        (500, 300),
        ("999999", 300),
        ("999999999999999999", 300),
        (1000000, 300),
        # Valid strict integer values within range [1, 300]
        (1, 1),
        ("1", 1),
        (45, 45),
        ("45", 45),
        (120, 120),
        ("120", 120),
        (300, 300),
        ("300", 300),
    ],
)
def test_parse_retry_after_defensive_bounds(raw_input, expected_delay):
    """Verify parse_retry_after defensively handles missing, negative, malformed, and large values."""
    assert parse_retry_after(raw_input) == expected_delay
    assert ZohoRecruitClient.parse_retry_after(raw_input) == expected_delay


@pytest.mark.parametrize(
    "header_val,expected_retry",
    [
        ("999999", 300),     # Excessively large -> capped at 300s
        ("-30", 60),         # Negative -> fallback 60s
        ("invalid", 60),     # Malformed -> fallback 60s
        (None, 60),          # Missing -> fallback 60s
        ("45", 45),          # Valid -> exact 45s
    ],
)
def test_http_429_rate_limiting_retry_after_bounded(header_val, expected_retry):
    """Verify HTTP 429 response parses and bounds Retry-After across all boundary cases."""
    headers = {"Retry-After": header_val} if header_val is not None else {}
    mock_transport = httpx.MockTransport(
        lambda req: httpx.Response(429, headers=headers, json={"message": "Too many requests"})
    )
    client = ZohoRecruitClient(
        "client_id", "secret", "https://cb.test", "in",
        http_client=httpx.Client(transport=mock_transport),
    )

    with pytest.raises(RateLimitError) as exc_info:
        client.get_module_records("JobOpenings", "test_token")

    assert exc_info.value.retry_after == expected_retry
    assert f"Retry after {expected_retry}s." in str(exc_info.value)


# ─── 9. Token Exchange Error Sanitization & Non-Leakage ──────────────────────


@pytest.mark.parametrize(
    "status_code,response_payload",
    [
        (400, {"error": "invalid_client_password_leak", "error_description": "<script>alert(1)</script> password=supersecret"}),
        (401, {"error": "token_expired_corp_user_credential_xyz", "message": "Failed auth for user=admin@corp.com"}),
        (500, {"error": "internal_db_error", "trace": "SELECT * FROM secrets WHERE key='API_KEY_123'"}),
        (200, {"error": "unsupported_grant_type", "details": "secret_token_abc"}),
    ],
)
def test_token_exchange_fixed_message_and_sanitization(caplog, status_code, response_payload):
    """Verify token exchange errors return fixed message and never leak raw error strings or secrets."""
    mock_transport = httpx.MockTransport(
        lambda req: httpx.Response(status_code, json=response_payload)
    )
    client = ZohoRecruitClient(
        "client_id", "secret", "https://cb.test", "in",
        http_client=httpx.Client(transport=mock_transport),
    )

    with pytest.raises(OAuthError) as exc_info:
        client.exchange_code_for_tokens("test_code_123")

    err_msg = str(exc_info.value)
    # The message MUST be strictly the fixed string
    assert err_msg == "Zoho token exchange failed."

    # Proving sensitive or malicious strings cannot appear in exceptions
    for sensitive_str in ["supersecret", "<script>", "admin@corp.com", "API_KEY_123", "secret_token_abc"]:
        assert sensitive_str not in err_msg
        assert sensitive_str not in caplog.text


def test_token_exchange_invalid_json_and_missing_access_token_uses_fixed_message():
    """Verify invalid JSON or missing access token returns fixed token exchange message."""
    # 1. Non-JSON response
    mock_html = httpx.MockTransport(
        lambda req: httpx.Response(502, text="<html><body>502 Bad Gateway</body></html>")
    )
    client = ZohoRecruitClient(
        "client_id", "secret", "https://cb.test", "in",
        http_client=httpx.Client(transport=mock_html),
    )
    with pytest.raises(OAuthError) as exc_info:
        client.exchange_code_for_tokens("code_123")
    assert str(exc_info.value) == "Zoho token exchange failed."

    # 2. Missing access_token
    mock_missing = httpx.MockTransport(
        lambda req: httpx.Response(200, json={"token_type": "Bearer", "expires_in": 3600})
    )
    client_missing = ZohoRecruitClient(
        "client_id", "secret", "https://cb.test", "in",
        http_client=httpx.Client(transport=mock_missing),
    )
    with pytest.raises(OAuthError) as exc_info:
        client_missing.exchange_code_for_tokens("code_123")
    assert str(exc_info.value) == "Zoho token exchange failed."


# ─── 10. Token Refresh Error Sanitization & Non-Leakage ───────────────────────


@pytest.mark.parametrize(
    "status_code,response_payload",
    [
        (400, {"error": "refresh_token_leak", "error_description": "private_key=XYZ123456 token=secret_ref_99"}),
        (401, {"error": "invalid_refresh_token", "message": "Failed credentials for user=db_admin; DROP TABLE users;"}),
        (500, {"error": "upstream_timeout", "debug": "Internal token service at 10.0.0.5:8443 failed"}),
        (200, {"error": "general_failure", "sensitive": "aws_secret_access_key=ABCDEF"}),
    ],
)
def test_token_refresh_fixed_message_and_sanitization(caplog, status_code, response_payload):
    """Verify token refresh errors return fixed message and never leak raw error strings or secrets."""
    mock_transport = httpx.MockTransport(
        lambda req: httpx.Response(status_code, json=response_payload)
    )
    client = ZohoRecruitClient(
        "client_id", "secret", "https://cb.test", "in",
        http_client=httpx.Client(transport=mock_transport),
    )

    with pytest.raises(OAuthError) as exc_info:
        client.refresh_access_token("test_refresh_token")

    err_msg = str(exc_info.value)
    # The message MUST be strictly the fixed string
    assert err_msg == "Zoho token refresh failed."

    # Proving sensitive or malicious strings cannot appear in exceptions or logs
    for sensitive_str in ["XYZ123456", "secret_ref_99", "DROP TABLE", "10.0.0.5", "ABCDEF"]:
        assert sensitive_str not in err_msg
        assert sensitive_str not in caplog.text


def test_token_refresh_invalid_json_and_non_dict_uses_fixed_message():
    """Verify invalid JSON or non-dict refresh response returns fixed token refresh message."""
    # 1. Non-JSON 502 Bad Gateway
    mock_html = httpx.MockTransport(
        lambda req: httpx.Response(502, text="<html><body>502 Bad Gateway</body></html>")
    )
    client = ZohoRecruitClient(
        "client_id", "secret", "https://cb.test", "in",
        http_client=httpx.Client(transport=mock_html),
    )
    with pytest.raises(OAuthError) as exc_info:
        client.refresh_access_token("ref_token_123")
    assert str(exc_info.value) == "Zoho token refresh failed."

    # 2. Non-dict payload (e.g. list)
    mock_list = httpx.MockTransport(
        lambda req: httpx.Response(200, json=["unexpected_item"])
    )
    client_list = ZohoRecruitClient(
        "client_id", "secret", "https://cb.test", "in",
        http_client=httpx.Client(transport=mock_list),
    )
    with pytest.raises(OAuthError) as exc_info:
        client_list.refresh_access_token("ref_token_123")
    assert str(exc_info.value) == "Zoho token refresh failed."


# ─── 11. Network Exception Sanitization & Non-Leakage ─────────────────────────


def test_token_exchange_network_error_uses_fixed_message_and_sanitizes_logs(caplog):
    """Verify network errors in token exchange return fixed message without leaking URLs or secrets in logs."""
    def _raise_network_error(req):
        raise httpx.ConnectError(
            "Connection refused: https://accounts.zoho.in/oauth/v2/token?client_secret=SENSITIVE_SECRET&code=LEAKED_AUTH_CODE",
            request=req,
        )

    client = ZohoRecruitClient(
        "client_id", "secret", "https://cb.test", "in",
        http_client=httpx.Client(transport=httpx.MockTransport(_raise_network_error)),
    )

    with pytest.raises(IntegrationError) as exc_info:
        client.exchange_code_for_tokens("code_xyz")

    err_msg = str(exc_info.value)
    assert "Zoho token exchange failed due to a network error." in err_msg

    # Exception and logs MUST NOT leak sensitive strings or URLs
    for sensitive_str in ["SENSITIVE_SECRET", "LEAKED_AUTH_CODE", "Connection refused"]:
        assert sensitive_str not in err_msg
        assert sensitive_str not in caplog.text


def test_token_exchange_timeout_uses_fixed_message_and_sanitizes_logs(caplog):
    """Verify timeout in token exchange returns fixed message without leaking URLs or secrets in logs."""
    def _raise_timeout(req):
        raise httpx.TimeoutException("Timeout after 30.0s connecting to user:pass@10.0.0.1", request=req)

    client = ZohoRecruitClient(
        "client_id", "secret", "https://cb.test", "in",
        http_client=httpx.Client(transport=httpx.MockTransport(_raise_timeout)),
    )

    with pytest.raises(IntegrationError) as exc_info:
        client.exchange_code_for_tokens("code_xyz")

    err_msg = str(exc_info.value)
    assert "Zoho token exchange failed due to a network error." in err_msg
    for sensitive_str in ["user:pass", "10.0.0.1", "Timeout after 30.0s"]:
        assert sensitive_str not in err_msg
        assert sensitive_str not in caplog.text


def test_token_refresh_network_error_uses_fixed_message_and_sanitizes_logs(caplog):
    """Verify network errors in token refresh return fixed message without leaking URLs or secrets in logs."""
    def _raise_network_error(req):
        raise httpx.ConnectError(
            "Network unreachable: https://accounts.zoho.in/oauth/v2/token?refresh_token=LEAKED_REFRESH_TOKEN",
            request=req,
        )

    client = ZohoRecruitClient(
        "client_id", "secret", "https://cb.test", "in",
        http_client=httpx.Client(transport=httpx.MockTransport(_raise_network_error)),
    )

    with pytest.raises(IntegrationError) as exc_info:
        client.refresh_access_token("refresh_tok_xyz")

    err_msg = str(exc_info.value)
    assert "Zoho token refresh failed due to a network error." in err_msg
    for sensitive_str in ["LEAKED_REFRESH_TOKEN", "Network unreachable"]:
        assert sensitive_str not in err_msg
        assert sensitive_str not in caplog.text


def test_token_refresh_timeout_uses_fixed_message_and_sanitizes_logs(caplog):
    """Verify timeout in token refresh returns fixed message without leaking URLs or secrets in logs."""
    def _raise_timeout(req):
        raise httpx.TimeoutException("Timeout connecting to secret_upstream_host", request=req)

    client = ZohoRecruitClient(
        "client_id", "secret", "https://cb.test", "in",
        http_client=httpx.Client(transport=httpx.MockTransport(_raise_timeout)),
    )

    with pytest.raises(IntegrationError) as exc_info:
        client.refresh_access_token("refresh_tok_xyz")

    err_msg = str(exc_info.value)
    assert "Zoho token refresh failed due to a network error." in err_msg
    for sensitive_str in ["secret_upstream_host", "Timeout connecting"]:
        assert sensitive_str not in err_msg
        assert sensitive_str not in caplog.text


