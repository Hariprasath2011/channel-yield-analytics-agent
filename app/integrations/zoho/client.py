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
    "ZohoRecruit.modules.jobopenings.READ",
    "ZohoRecruit.modules.candidates.READ",
    "ZohoRecruit.modules.applications.READ",
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
        payload = resp.json()
        if "error" in payload or resp.status_code >= 400:
            err = payload.get("error", "token_exchange_failed")
            logger.error("zoho.oauth_token_exchange_failed", error=err)
            raise OAuthError(f"Zoho token exchange failed: {err}")
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
