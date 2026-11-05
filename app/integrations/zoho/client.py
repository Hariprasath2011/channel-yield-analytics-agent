"""Zoho Recruit API Client.

Implements official Zoho Recruit API v2 and OAuth 2.0 flows:
  - Multi-datacenter support (.com, .eu, .in, .au, .jp, .ca).
  - Authorization code exchange and offline refresh token generation.
  - Access token refresh.
  - Paginated module records retrieval (JobOpenings, Candidates, Applications).
  - Explicit rate-limit handling (HTTP 429) and safe error mapping.
"""
from __future__ import annotations

from typing import Any, Optional
import httpx

from app.core.exceptions import IntegrationError, OAuthError, RateLimitError
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
]


class ZohoRecruitClient:
    """Client for Zoho Recruit REST API v2."""

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
            logger.error("zoho.oauth_token_exchange_timeout", error=str(exc))
            raise IntegrationError("zoho_recruit", "Connection timed out exchanging authorization code.")
        except httpx.RequestError as exc:
            logger.error("zoho.oauth_token_exchange_network_error", error=str(exc))
            raise IntegrationError("zoho_recruit", f"Network error during token exchange: {exc}")

        if resp.status_code == 429:
            raise RateLimitError("zoho_recruit", message="Zoho OAuth rate limit reached.")
        try:
            payload = resp.json()
        except ValueError as exc:
            logger.error("zoho.oauth_token_exchange_invalid_json")
            raise OAuthError("Zoho token exchange returned an invalid response.") from exc
        if not isinstance(payload, dict):
            logger.error("zoho.oauth_token_exchange_invalid_payload")
            raise OAuthError("Zoho token exchange returned an invalid response.")
        if "error" in payload or resp.status_code >= 400:
            err = payload.get("error", "token_exchange_failed")
            logger.error("zoho.oauth_token_exchange_failed", error=err)
            raise OAuthError(f"Zoho token exchange failed: {err}")
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token.strip():
            logger.error("zoho.oauth_token_exchange_missing_access_token")
            raise OAuthError("Zoho token response is missing a valid access_token.")
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
            logger.error("zoho.oauth_token_refresh_timeout", error=str(exc))
            raise IntegrationError("zoho_recruit", "Connection timed out refreshing access token.")
        except httpx.RequestError as exc:
            logger.error("zoho.oauth_token_refresh_network_error", error=str(exc))
            raise IntegrationError("zoho_recruit", f"Network error during token refresh: {exc}")

        if resp.status_code == 429:
            raise RateLimitError("zoho_recruit", message="Zoho OAuth rate limit reached.")
        payload = resp.json()
        if "error" in payload or resp.status_code >= 400:
            err = payload.get("error", "token_refresh_failed")
            logger.error("zoho.oauth_token_refresh_failed", error=err)
            raise OAuthError(f"Zoho token refresh failed: {err}")
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

        if resp.status_code == 429:
            retry_header = resp.headers.get("Retry-After", "60")
            try:
                retry_after = int(retry_header)
            except ValueError:
                retry_after = 60
            raise RateLimitError("zoho_recruit", retry_after=retry_after, message=f"Zoho API rate limit reached. Retry after {retry_after}s.")

        if resp.status_code == 401:
            raise OAuthError("Zoho access token is invalid or expired.")

        # Zoho returns HTTP 204 No Content when a module contains 0 records
        if resp.status_code == 204:
            return {"data": [], "info": {"page": page, "per_page": min(per_page, 200), "more_records": False}}

        if resp.status_code >= 400:
            logger.error("zoho.fetch_module_failed", module=module, status_code=resp.status_code)
            raise IntegrationError("zoho_recruit", f"Failed to fetch {module}: HTTP {resp.status_code}")

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

        if resp.status_code == 429:
            retry_header = resp.headers.get("Retry-After", "60")
            try:
                retry_after = int(retry_header)
            except ValueError:
                retry_after = 60
            raise RateLimitError("zoho_recruit", retry_after=retry_after, message=f"Zoho API rate limit reached.")

        if resp.status_code == 401:
            raise OAuthError("Zoho access token is invalid or expired.")

        if resp.status_code == 404:
            return {"data": [], "status": "not_found"}

        if resp.status_code >= 400:
            logger.error("zoho.get_record_failed", module=module, record_id=clean_id, status_code=resp.status_code)
            raise IntegrationError("zoho_recruit", f"Failed to fetch {module} {clean_id}: HTTP {resp.status_code}")

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

        if resp.status_code == 429:
            retry_header = resp.headers.get("Retry-After", "60")
            try:
                retry_after = int(retry_header)
            except ValueError:
                retry_after = 60
            raise RateLimitError("zoho_recruit", retry_after=retry_after, message=f"Zoho API rate limit reached.")

        if resp.status_code == 401:
            raise OAuthError("Zoho access token is invalid or expired.")

        # Zoho search returns HTTP 204 No Content when no records match criteria
        if resp.status_code == 204:
            return {"data": [], "info": {"page": page, "more_records": False}}

        if resp.status_code >= 400:
            logger.error("zoho.search_failed", module=module, status_code=resp.status_code)
            raise IntegrationError("zoho_recruit", f"Failed to search {module}: HTTP {resp.status_code}")

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

        if resp.status_code == 429:
            raise RateLimitError("zoho_recruit", message="Zoho API rate limit reached.")
        if resp.status_code == 401:
            raise OAuthError("Zoho access token is invalid or expired.")
        if resp.status_code >= 400:
            logger.error("zoho.get_modules_failed", status_code=resp.status_code)
            raise IntegrationError("zoho_recruit", f"Failed to fetch module metadata: HTTP {resp.status_code}")

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

        if resp.status_code == 429:
            raise RateLimitError("zoho_recruit", message="Zoho API rate limit reached.")
        if resp.status_code == 401:
            raise OAuthError("Zoho access token is invalid or expired.")
        if resp.status_code >= 400:
            logger.error("zoho.get_fields_failed", module=clean_module, status_code=resp.status_code)
            raise IntegrationError("zoho_recruit", f"Failed to fetch fields for {clean_module}: HTTP {resp.status_code}")

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

        if resp.status_code == 429:
            raise RateLimitError("zoho_recruit", message="Zoho API rate limit reached.")
        if resp.status_code == 401:
            raise OAuthError("Zoho access token is invalid or expired.")
        if resp.status_code == 404:
            return {"data": [], "status": "not_found"}
        if resp.status_code >= 400:
            logger.error("zoho.get_pipeline_failed", status_code=resp.status_code)
            raise IntegrationError("zoho_recruit", f"Failed to fetch hiring pipeline: HTTP {resp.status_code}")

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

        if resp.status_code == 429:
            raise RateLimitError("zoho_recruit", message="Zoho API rate limit reached.")
        if resp.status_code == 401:
            raise OAuthError("Zoho access token is invalid or expired.")
        if resp.status_code == 404:
            return {"data": [], "status": "not_found"}
        if resp.status_code >= 400:
            logger.error("zoho.get_app_pipeline_failed", application_id=clean_app_id, status_code=resp.status_code)
            raise IntegrationError("zoho_recruit", f"Failed to fetch pipeline for application {clean_app_id}: HTTP {resp.status_code}")

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

        if resp.status_code == 429:
            raise RateLimitError("zoho_recruit", message="Zoho API rate limit reached.")
        if resp.status_code == 401:
            raise OAuthError("Zoho access token is invalid or expired.")
        if resp.status_code == 404:
            return {"data": [], "status": "history_unavailable"}
        if resp.status_code >= 400:
            logger.error("zoho.get_transitions_failed", application_id=clean_app_id, status_code=resp.status_code)
            raise IntegrationError("zoho_recruit", f"Failed to fetch stage transitions for application {clean_app_id}: HTTP {resp.status_code}")

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

        if resp.status_code == 429:
            raise RateLimitError("zoho_recruit", message="Zoho API rate limit reached.")
        if resp.status_code == 401:
            raise OAuthError("Zoho access token is invalid or expired.")
        if resp.status_code == 404:
            return {"data": [], "status": "timeline_unavailable"}
        if resp.status_code >= 400:
            logger.error("zoho.get_timeline_failed", module=clean_mod, record_id=clean_id, status_code=resp.status_code)
            raise IntegrationError("zoho_recruit", f"Failed to fetch timeline for {clean_mod} {clean_id}: HTTP {resp.status_code}")

        return resp.json()
