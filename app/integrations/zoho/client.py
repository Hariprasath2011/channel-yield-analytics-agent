"""Zoho Recruit API Client.

Implements official Zoho Recruit API v2 and OAuth 2.0 flows:
  - Multi-datacenter support (.com, .eu, .in, .au, .jp, .ca).
  - Authorization code exchange and offline refresh token generation.
  - Access token refresh.
  - Paginated module records retrieval (JobOpenings, Candidates, Applications).
  - Explicit rate-limit handling (HTTP 429) and safe error mapping.
"""
from __future__ import annotations

import math
from typing import Any, Optional
import httpx

from app.core.exceptions import (
    IntegrationError,
    OAuthError,
    OAuthScopeError,
    RateLimitError,
)
from app.core.logging import get_logger

logger = get_logger(__name__)

# Data center domain mappings
DATACENTER_DOMAINS = {
    "com": {"accounts": "https://accounts.zoho.com", "api": "https://recruit.zoho.com/recruit/v2"},
    "eu": {"accounts": "https://accounts.zoho.eu", "api": "https://recruit.zoho.eu/recruit/v2"},
    "in": {"accounts": "https://accounts.zoho.in", "api": "https://recruit.zoho.in/recruit/v2"},
    "au": {"accounts": "https://accounts.zoho.com.au", "api": "https://recruit.zoho.com.au/recruit/v2"},
    "jp": {"accounts": "https://accounts.zoho.jp", "api": "https://recruit.zoho.jp/recruit/v2"},
    "ca": {"accounts": "https://accounts.zohocloud.ca", "api": "https://recruit.zoho.zohocloud.ca/recruit/v2"},
}

DEFAULT_SCOPES = [
    "ZohoRecruit.modules.jobopening.READ",
    "ZohoRecruit.modules.candidate.READ",
    "ZohoRecruit.modules.application.READ",
    "ZohoRecruit.settings.modules.read",
    "ZohoRecruit.settings.fields.read",
]

MAX_RETRY_AFTER_SECONDS: int = 300
DEFAULT_RETRY_AFTER_SECONDS: int = 60


def parse_retry_after(
    raw_val: Any,
    max_delay: int = MAX_RETRY_AFTER_SECONDS,
    default_delay: int = DEFAULT_RETRY_AFTER_SECONDS,
) -> int:
    """Defensively parse the Retry-After header.

    HTTP RFC 9110 specifies delay-seconds as a non-negative decimal integer.
    Rejects:
    - Missing (None, empty, whitespace)
    - Decimal / non-integer values (e.g. 45.5, "45.5")
    - Non-finite / NaN / Infinity
    - Negative and zero values (<= 0)
    - Malformed strings

    Bounds:
    - Default to default_delay (60s) on invalid/rejected values
    - Cap at max_delay (300s) on excessively large values
    """
    if raw_val is None or isinstance(raw_val, bool):
        return default_delay

    val: Optional[int] = None

    if isinstance(raw_val, int):
        val = raw_val
    elif isinstance(raw_val, str):
        s = raw_val.strip()
        if s.startswith("-"):
            if s[1:].isdigit():
                val = -int(s[1:])
            else:
                return default_delay
        elif s.isdigit():
            val = int(s)
        else:
            return default_delay
    elif isinstance(raw_val, float):
        if not math.isfinite(raw_val) or not raw_val.is_integer():
            return default_delay
        val = int(raw_val)
    else:
        return default_delay

    if val is None or val <= 0:
        return default_delay
    if val > max_delay:
        return max_delay
    return val


def _sanitize_error_code(raw_code: Any) -> str:
    """Sanitize error code for safe logging (alphanumeric, underscore, hyphen only, max 32 chars)."""
    if not raw_code:
        return "UNKNOWN"
    clean = "".join(c for c in str(raw_code) if c.isalnum() or c in ("_", "-"))[:32]
    return clean or "UNKNOWN"


def _extract_correlation_id(resp: httpx.Response) -> Optional[str]:
    """Extract and sanitize correlation/request identifier from response headers."""
    raw_id = (
        resp.headers.get("x-request-id")
        or resp.headers.get("X-Request-Id")
        or resp.headers.get("x-correlation-id")
        or resp.headers.get("X-Correlation-Id")
        or resp.headers.get("z-request-id")
    )
    if not raw_id:
        return None
    clean_id = "".join(c for c in str(raw_id) if c.isalnum() or c in ("-", "_"))[:64]
    return clean_id or None


class ZohoRecruitClient:
    """Client for Zoho Recruit REST API v2."""

    parse_retry_after = staticmethod(parse_retry_after)

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        data_center: str = "com",
        http_client: Optional[httpx.Client] = None,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.data_center = data_center.lower()
        if self.data_center not in DATACENTER_DOMAINS:
            raise ValueError(f"Unsupported Zoho data center: {data_center}")

        self.accounts_url = DATACENTER_DOMAINS[self.data_center]["accounts"]
        self.api_url = DATACENTER_DOMAINS[self.data_center]["api"]
        self._http = http_client or httpx.Client(timeout=30.0)

    def set_data_center(self, data_center: str) -> None:
        """Update data center and corresponding account/API endpoints."""
        dc = data_center.lower().strip()
        if dc not in DATACENTER_DOMAINS:
            raise ValueError(f"Unsupported Zoho data center: {data_center}")
        self.data_center = dc
        self.accounts_url = DATACENTER_DOMAINS[dc]["accounts"]
        self.api_url = DATACENTER_DOMAINS[dc]["api"]

    def _classify_error_response(
        self,
        resp: httpx.Response,
        action_description: str,
        log_tag: Optional[str] = None,
    ) -> None:
        """Defensively parse and classify Zoho API HTTP errors without leaking secrets."""
        if resp.status_code < 400:
            return

        tag = log_tag or "api"
        corr_id = _extract_correlation_id(resp)

        # 1. Rate Limiting (HTTP 429)
        if resp.status_code == 429:
            retry_after = parse_retry_after(resp.headers.get("Retry-After"))
            logger.warning(
                f"zoho.{tag}_rate_limited",
                status_code=429,
                retry_after=retry_after,
                correlation_id=corr_id,
            )
            raise RateLimitError(
                "zoho_recruit",
                retry_after=retry_after,
                message=f"Zoho API rate limit reached. Retry after {retry_after}s.",
            )

        # 2. Defensive JSON parsing
        payload: dict[str, Any] = {}
        try:
            parsed = resp.json()
            if isinstance(parsed, dict):
                payload = parsed
            elif isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
                payload = parsed[0]
        except Exception:
            payload = {}

        raw_code = payload.get("code") or payload.get("error")
        raw_msg = payload.get("message") or payload.get("error_description")

        error_code = str(raw_code).strip().upper() if raw_code else ""
        error_msg = str(raw_msg).strip() if raw_msg else ""

        # Safe error code for logging: strictly alphanumeric + underscores, capped at 32 chars
        safe_code = _sanitize_error_code(error_code)

        code_lower = error_code.lower()
        msg_lower = error_msg.lower()

        # Explicit scope denial evidence: "scope" explicitly present in code or message
        has_explicit_scope_evidence = (
            "scope" in code_lower
            or "scope" in msg_lower
            or error_code in ("OAUTH_SCOPE_MISMATCH", "INSUFFICIENT_SCOPE")
        )

        if has_explicit_scope_evidence:
            logger.warning(
                f"zoho.{tag}_scope_denied",
                status_code=resp.status_code,
                error_code=safe_code,
                correlation_id=corr_id,
            )
            raise OAuthScopeError(
                "Zoho API access denied: missing or insufficient OAuth scope. Re-authorization is required.",
                provider="zoho_recruit",
            )

        # 3. HTTP 401 Unauthorized handling
        if resp.status_code == 401:
            # Check for explicit token invalidation or expiration evidence
            is_explicit_token_error = (
                error_code in ("EXPIRED_TOKEN", "TOKEN_EXPIRED")
                or any(phrase in msg_lower for phrase in ("invalid oauth token", "invalid token", "token has expired", "token expired", "expired token"))
                or (error_code in ("INVALID_TOKEN", "AUTHENTICATION_FAILURE") and any(term in msg_lower for term in ("invalid", "expired", "token")))
            )

            if is_explicit_token_error:
                logger.warning(
                    f"zoho.{tag}_token_invalid_or_expired",
                    status_code=401,
                    error_code=safe_code,
                    correlation_id=corr_id,
                )
                raise OAuthError("Zoho access token is invalid or expired.", provider="zoho_recruit")

            # Ambiguous 401 response (generic, non-JSON, or unspecified error)
            logger.warning(
                f"zoho.{tag}_unauthorized_ambiguous",
                status_code=401,
                error_code=safe_code,
                correlation_id=corr_id,
            )
            raise OAuthError("Zoho authentication failed (HTTP 401).", provider="zoho_recruit")

        # 4. HTTP 403 Forbidden (non-scope)
        if resp.status_code == 403:
            logger.error(
                f"zoho.{tag}_forbidden",
                status_code=403,
                error_code=safe_code,
                correlation_id=corr_id,
            )
            raise IntegrationError("zoho_recruit", f"Failed to {action_description}: HTTP 403 Forbidden.")

        # 5. Other HTTP >= 400 errors
        logger.error(
            f"zoho.{tag}_failed",
            status_code=resp.status_code,
            error_code=safe_code,
            correlation_id=corr_id,
        )
        raise IntegrationError("zoho_recruit", f"Failed to {action_description}: HTTP {resp.status_code}.")

    def get_authorization_url(self, state: str, scopes: Optional[list[str]] = None) -> str:
        """Construct the OAuth authorization redirect URL."""
        scope_str = ",".join(scopes or DEFAULT_SCOPES)
        params = {
            "scope": scope_str,
            "client_id": self.client_id,
            "response_type": "code",
            "access_type": "offline",
            "redirect_uri": self.redirect_uri,
            "state": state,
            "prompt": "consent",
        }
        req = httpx.Request("GET", f"{self.accounts_url}/oauth/v2/auth", params=params)
        return str(req.url)

    def exchange_code_for_tokens(self, code: str) -> dict[str, Any]:
        """Exchange authorization code for access and refresh tokens."""
        url = f"{self.accounts_url}/oauth/v2/token"
        data = {
            "grant_type": "authorization_code",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": self.redirect_uri,
            "code": code,
        }
        try:
            resp = self._http.post(url, data=data)
        except httpx.TimeoutException as exc:
            logger.error("zoho.oauth_token_exchange_timeout", error_type=exc.__class__.__name__)
            raise IntegrationError("zoho_recruit", "Zoho token exchange failed due to a network error.") from exc
        except httpx.RequestError as exc:
            logger.error("zoho.oauth_token_exchange_network_error", error_type=exc.__class__.__name__)
            raise IntegrationError("zoho_recruit", "Zoho token exchange failed due to a network error.") from exc

        corr_id = _extract_correlation_id(resp)

        if resp.status_code == 429:
            retry_after = parse_retry_after(resp.headers.get("Retry-After"))
            logger.warning(
                "zoho.oauth_token_exchange_rate_limited",
                status_code=429,
                retry_after=retry_after,
                correlation_id=corr_id,
            )
            raise RateLimitError(
                "zoho_recruit",
                retry_after=retry_after,
                message=f"Zoho OAuth rate limit reached. Retry after {retry_after}s.",
            )
        try:
            payload = resp.json()
        except ValueError as exc:
            logger.error(
                "zoho.oauth_token_exchange_invalid_json",
                status_code=resp.status_code,
                correlation_id=corr_id,
            )
            raise OAuthError("Zoho token exchange failed.", provider="zoho_recruit") from exc
        if not isinstance(payload, dict):
            logger.error(
                "zoho.oauth_token_exchange_invalid_payload",
                status_code=resp.status_code,
                correlation_id=corr_id,
            )
            raise OAuthError("Zoho token exchange failed.", provider="zoho_recruit")
        if "error" in payload or resp.status_code >= 400:
            safe_err = _sanitize_error_code(payload.get("error"))
            logger.error(
                "zoho.oauth_token_exchange_failed",
                status_code=resp.status_code,
                error_code=safe_err,
                correlation_id=corr_id,
            )
            raise OAuthError("Zoho token exchange failed.", provider="zoho_recruit")
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token.strip():
            logger.error(
                "zoho.oauth_token_exchange_missing_access_token",
                status_code=resp.status_code,
                correlation_id=corr_id,
            )
            raise OAuthError("Zoho token exchange failed.", provider="zoho_recruit")
        return payload

    def refresh_access_token(self, refresh_token: str) -> dict[str, Any]:
        """Refresh an expired access token using the offline refresh token."""
        url = f"{self.accounts_url}/oauth/v2/token"
        data = {
            "grant_type": "refresh_token",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": refresh_token,
        }
        try:
            resp = self._http.post(url, data=data)
        except httpx.TimeoutException as exc:
            logger.error("zoho.oauth_token_refresh_timeout", error_type=exc.__class__.__name__)
            raise IntegrationError("zoho_recruit", "Zoho token refresh failed due to a network error.") from exc
        except httpx.RequestError as exc:
            logger.error("zoho.oauth_token_refresh_network_error", error_type=exc.__class__.__name__)
            raise IntegrationError("zoho_recruit", "Zoho token refresh failed due to a network error.") from exc

        corr_id = _extract_correlation_id(resp)

        if resp.status_code == 429:
            retry_after = parse_retry_after(resp.headers.get("Retry-After"))
            logger.warning(
                "zoho.oauth_token_refresh_rate_limited",
                status_code=429,
                retry_after=retry_after,
                correlation_id=corr_id,
            )
            raise RateLimitError(
                "zoho_recruit",
                retry_after=retry_after,
                message=f"Zoho OAuth rate limit reached. Retry after {retry_after}s.",
            )
        try:
            payload = resp.json()
        except ValueError as exc:
            logger.error(
                "zoho.oauth_token_refresh_invalid_json",
                status_code=resp.status_code,
                correlation_id=corr_id,
            )
            raise OAuthError("Zoho token refresh failed.", provider="zoho_recruit") from exc
        if not isinstance(payload, dict):
            logger.error(
                "zoho.oauth_token_refresh_invalid_payload",
                status_code=resp.status_code,
                correlation_id=corr_id,
            )
            raise OAuthError("Zoho token refresh failed.", provider="zoho_recruit")
        if "error" in payload or resp.status_code >= 400:
            safe_err = _sanitize_error_code(payload.get("error"))
            logger.error(
                "zoho.oauth_token_refresh_failed",
                status_code=resp.status_code,
                error_code=safe_err,
                correlation_id=corr_id,
            )
            raise OAuthError("Zoho token refresh failed.", provider="zoho_recruit")
        return payload

    def get_module_records(
        self,
        module: str,
        access_token: str,
        page: int = 1,
        per_page: int = 200,
    ) -> dict[str, Any]:
        """Fetch a page of records from a Zoho Recruit module."""
        url = f"{self.api_url}/{module}"
        headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}
        params = {"page": page, "per_page": min(per_page, 200)}

        try:
            resp = self._http.get(url, headers=headers, params=params)
        except httpx.TimeoutException as exc:
            logger.error("zoho.fetch_module_timeout", module=module, error=str(exc))
            raise IntegrationError("zoho_recruit", f"Connection timed out fetching {module}.")
        except httpx.RequestError as exc:
            logger.error("zoho.fetch_module_network_error", module=module, error=str(exc))
            raise IntegrationError("zoho_recruit", f"Network error fetching {module}: {exc}")

        # Zoho returns HTTP 204 No Content when a module contains 0 records
        if resp.status_code == 204:
            return {"data": [], "info": {"page": page, "per_page": min(per_page, 200), "more_records": False}}

        self._classify_error_response(resp, f"fetch {module}", log_tag="fetch_module")
        return resp.json()

    def fetch_all_module_records(
        self,
        module: str,
        access_token: str,
        max_pages: int = 50,
        per_page: int = 200,
    ) -> list[dict[str, Any]]:
        """Paginate through all records for a module up to max_pages."""
        all_records: list[dict[str, Any]] = []
        page = 1
        while page <= max_pages:
            res = self.get_module_records(module, access_token, page=page, per_page=per_page)
            data = res.get("data", [])
            all_records.extend(data)
            info = res.get("info", {})
            more_records = info.get("more_records", False)
            if not more_records or not data:
                break
            page += 1
        return all_records

    # ── Single Record Retrieval ───────────────────────────────────────────────

    def get_record_by_id(
        self,
        module: str,
        record_id: str,
        access_token: str,
    ) -> dict[str, Any]:
        """Fetch a single record by external Zoho ID from a module."""
        clean_id = str(record_id).strip()
        url = f"{self.api_url}/{module}/{clean_id}"
        headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}

        try:
            resp = self._http.get(url, headers=headers, params={})
        except httpx.TimeoutException as exc:
            logger.error("zoho.get_record_timeout", module=module, record_id=clean_id, error=str(exc))
            raise IntegrationError("zoho_recruit", f"Connection timed out fetching {module} {clean_id}.")
        except httpx.RequestError as exc:
            logger.error("zoho.get_record_network_error", module=module, record_id=clean_id, error=str(exc))
            raise IntegrationError("zoho_recruit", f"Network error fetching {module} {clean_id}: {exc}")

        if resp.status_code == 404:
            return {"data": [], "status": "not_found"}

        self._classify_error_response(resp, f"fetch {module} {clean_id}", log_tag="get_record")
        return resp.json()

    # ── Search Capability ─────────────────────────────────────────────────────

    def search_module_records(
        self,
        module: str,
        access_token: str,
        criteria: Optional[str] = None,
        email: Optional[str] = None,
        phone: Optional[str] = None,
        word: Optional[str] = None,
        page: int = 1,
        per_page: int = 200,
    ) -> dict[str, Any]:
        """Search records within a Zoho Recruit module using criteria, email, phone, or word."""
        url = f"{self.api_url}/{module}/search"
        headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}
        params: dict[str, Any] = {"page": page, "per_page": min(per_page, 200)}

        if criteria:
            params["criteria"] = criteria
        elif email:
            params["email"] = email
        elif phone:
            params["phone"] = phone
        elif word:
            params["word"] = word

        try:
            resp = self._http.get(url, headers=headers, params=params)
        except httpx.TimeoutException as exc:
            logger.error("zoho.search_timeout", module=module, error=str(exc))
            raise IntegrationError("zoho_recruit", f"Connection timed out searching {module}.")
        except httpx.RequestError as exc:
            logger.error("zoho.search_network_error", module=module, error=str(exc))
            raise IntegrationError("zoho_recruit", f"Network error searching {module}: {exc}")

        # Zoho search returns HTTP 204 No Content when no records match criteria
        if resp.status_code == 204:
            return {"data": [], "info": {"page": page, "more_records": False}}

        self._classify_error_response(resp, f"search {module}", log_tag="search")
        return resp.json()

    # ── Convenience Typed Domain Methods ──────────────────────────────────────

    def get_candidates(
        self,
        access_token: str,
        page: int = 1,
        per_page: int = 200,
    ) -> dict[str, Any]:
        """Retrieve paginated candidate records."""
        return self.get_module_records("Candidates", access_token, page=page, per_page=per_page)

    def search_candidates(
        self,
        access_token: str,
        criteria: Optional[str] = None,
        email: Optional[str] = None,
        phone: Optional[str] = None,
        page: int = 1,
        per_page: int = 200,
    ) -> dict[str, Any]:
        """Search candidate records by criteria, email, or phone."""
        return self.search_module_records(
            "Candidates",
            access_token,
            criteria=criteria,
            email=email,
            phone=phone,
            page=page,
            per_page=per_page,
        )

    def get_candidate_by_id(
        self,
        candidate_id: str,
        access_token: str,
    ) -> dict[str, Any]:
        """Retrieve candidate by external ID."""
        return self.get_record_by_id("Candidates", candidate_id, access_token)

    def get_applications(
        self,
        access_token: str,
        page: int = 1,
        per_page: int = 200,
    ) -> dict[str, Any]:
        """Retrieve paginated job application records."""
        return self.get_module_records("Applications", access_token, page=page, per_page=per_page)

    def search_applications(
        self,
        access_token: str,
        criteria: Optional[str] = None,
        page: int = 1,
        per_page: int = 200,
    ) -> dict[str, Any]:
        """Search application records by criteria."""
        return self.search_module_records(
            "Applications",
            access_token,
            criteria=criteria,
            page=page,
            per_page=per_page,
        )

    def get_application_by_id(
        self,
        application_id: str,
        access_token: str,
    ) -> dict[str, Any]:
        """Retrieve application by external ID."""
        return self.get_record_by_id("Applications", application_id, access_token)

    def get_job_openings(
        self,
        access_token: str,
        page: int = 1,
        per_page: int = 200,
    ) -> dict[str, Any]:
        """Retrieve paginated job opening records."""
        return self.get_module_records("JobOpenings", access_token, page=page, per_page=per_page)

    def search_job_openings(
        self,
        access_token: str,
        criteria: Optional[str] = None,
        page: int = 1,
        per_page: int = 200,
    ) -> dict[str, Any]:
        """Search job opening records by criteria."""
        return self.search_module_records(
            "JobOpenings",
            access_token,
            criteria=criteria,
            page=page,
            per_page=per_page,
        )

    def get_job_opening_by_id(
        self,
        job_opening_id: str,
        access_token: str,
    ) -> dict[str, Any]:
        """Retrieve job opening by external ID."""
        return self.get_record_by_id("JobOpenings", job_opening_id, access_token)

    # ── Metadata & Field Discovery ────────────────────────────────────────────

    def get_module_metadata(
        self,
        access_token: str,
        module: Optional[str] = None,
    ) -> dict[str, Any]:
        """Get official module metadata (list of modules or details for a module)."""
        clean_module = str(module).strip() if module else ""
        url = f"{self.api_url}/settings/modules/{clean_module}" if clean_module else f"{self.api_url}/settings/modules"
        headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}

        try:
            resp = self._http.get(url, headers=headers, params={})
        except httpx.TimeoutException as exc:
            logger.error("zoho.get_modules_timeout", error=str(exc))
            raise IntegrationError("zoho_recruit", "Connection timed out fetching module metadata.")
        except httpx.RequestError as exc:
            logger.error("zoho.get_modules_network_error", error=str(exc))
            raise IntegrationError("zoho_recruit", f"Network error fetching module metadata: {exc}")

        self._classify_error_response(resp, f"fetch module metadata for '{clean_module or 'all'}'", log_tag="get_modules")
        return resp.json()

    def get_module_fields(
        self,
        module: str,
        access_token: str,
    ) -> dict[str, Any]:
        """Get official fields specification and metadata for a specific module."""
        clean_module = str(module).strip()
        url = f"{self.api_url}/settings/fields"
        headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}
        params = {"module": clean_module}

        try:
            resp = self._http.get(url, headers=headers, params=params)
        except httpx.TimeoutException as exc:
            logger.error("zoho.get_fields_timeout", module=clean_module, error=str(exc))
            raise IntegrationError("zoho_recruit", f"Connection timed out fetching fields for {clean_module}.")
        except httpx.RequestError as exc:
            logger.error("zoho.get_fields_network_error", module=clean_module, error=str(exc))
            raise IntegrationError("zoho_recruit", f"Network error fetching fields for {clean_module}: {exc}")

        self._classify_error_response(resp, f"fetch fields for {clean_module}", log_tag="get_fields")
        return resp.json()

    # ── Hiring Pipeline & Transition History (Phase 3 Core) ───────────────────

    def get_hiring_pipeline(
        self,
        access_token: str,
        job_opening_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Retrieve hiring pipeline stages configured for an opening or organization."""
        clean_job_id = str(job_opening_id).strip() if job_opening_id else ""
        if clean_job_id:
            url = f"{self.api_url}/JobOpenings/{clean_job_id}/pipelines"
        else:
            url = f"{self.api_url}/settings/pipelines"

        headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}
        try:
            resp = self._http.get(url, headers=headers, params={})
        except httpx.TimeoutException as exc:
            logger.error("zoho.get_pipeline_timeout", error=str(exc))
            raise IntegrationError("zoho_recruit", "Connection timed out fetching hiring pipeline.")
        except httpx.RequestError as exc:
            logger.error("zoho.get_pipeline_network_error", error=str(exc))
            raise IntegrationError("zoho_recruit", f"Network error fetching hiring pipeline: {exc}")

        if resp.status_code == 404:
            return {"data": [], "status": "not_found"}
        self._classify_error_response(resp, "fetch hiring pipeline", log_tag="get_pipeline")
        return resp.json()

    def get_application_hiring_pipeline(
        self,
        application_id: str,
        access_token: str,
    ) -> dict[str, Any]:
        """Retrieve pipeline configuration and current stage for a specific application."""
        clean_app_id = str(application_id).strip()
        url = f"{self.api_url}/Applications/{clean_app_id}/hiring_pipeline"
        headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}

        try:
            resp = self._http.get(url, headers=headers, params={})
        except httpx.TimeoutException as exc:
            logger.error("zoho.get_app_pipeline_timeout", application_id=clean_app_id, error=str(exc))
            raise IntegrationError("zoho_recruit", f"Connection timed out fetching pipeline for application {clean_app_id}.")
        except httpx.RequestError as exc:
            logger.error("zoho.get_app_pipeline_network_error", application_id=clean_app_id, error=str(exc))
            raise IntegrationError("zoho_recruit", f"Network error fetching pipeline for application {clean_app_id}: {exc}")

        if resp.status_code == 404:
            return {"data": [], "status": "not_found"}
        self._classify_error_response(resp, f"fetch pipeline for application {clean_app_id}", log_tag="get_app_pipeline")
        return resp.json()

    def get_application_transition_details(
        self,
        application_id: str,
        access_token: str,
    ) -> dict[str, Any]:
        """Retrieve complete historical stage transition details for an application.

        Fetches transition events, timestamps, duration, and comments from the official
        Zoho Recruit application hiring pipeline transition history.
        """
        clean_app_id = str(application_id).strip()
        # Zoho Recruit exposes transition history at /Applications/{id}/stage_history or /stages
        url = f"{self.api_url}/Applications/{clean_app_id}/stage_history"
        headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}

        try:
            resp = self._http.get(url, headers=headers, params={})
            if resp.status_code == 404:
                # Fallback to alternate stages endpoint
                url_alt = f"{self.api_url}/Applications/{clean_app_id}/stages"
                resp = self._http.get(url_alt, headers=headers, params={})
        except httpx.TimeoutException as exc:
            logger.error("zoho.get_transitions_timeout", application_id=clean_app_id, error=str(exc))
            raise IntegrationError("zoho_recruit", f"Connection timed out fetching stage history for application {clean_app_id}.")
        except httpx.RequestError as exc:
            logger.error("zoho.get_transitions_network_error", application_id=clean_app_id, error=str(exc))
            raise IntegrationError("zoho_recruit", f"Network error fetching stage history for application {clean_app_id}: {exc}")

        if resp.status_code == 404:
            return {"data": [], "status": "history_unavailable"}
        self._classify_error_response(resp, f"fetch stage transitions for application {clean_app_id}", log_tag="get_transitions")
        return resp.json()

    def get_record_timeline(
        self,
        module: str,
        record_id: str,
        access_token: str,
    ) -> dict[str, Any]:
        """Retrieve audit timeline or activity history for a specific record."""
        clean_mod = str(module).strip()
        clean_id = str(record_id).strip()
        url = f"{self.api_url}/{clean_mod}/{clean_id}/__timeline"
        headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}

        try:
            resp = self._http.get(url, headers=headers, params={})
            if resp.status_code == 404:
                # Fallback to Activities sub-resource
                url_alt = f"{self.api_url}/{clean_mod}/{clean_id}/Activities"
                resp = self._http.get(url_alt, headers=headers, params={})
        except httpx.TimeoutException as exc:
            logger.error("zoho.get_timeline_timeout", module=clean_mod, record_id=clean_id, error=str(exc))
            raise IntegrationError("zoho_recruit", f"Connection timed out fetching timeline for {clean_mod} {clean_id}.")
        except httpx.RequestError as exc:
            logger.error("zoho.get_timeline_network_error", module=clean_mod, record_id=clean_id, error=str(exc))
            raise IntegrationError("zoho_recruit", f"Network error fetching timeline for {clean_mod} {clean_id}: {exc}")

        if resp.status_code == 404:
            return {"data": [], "status": "timeline_unavailable"}
        self._classify_error_response(resp, f"fetch timeline for {clean_mod} {clean_id}", log_tag="get_timeline")
        return resp.json()
