"""Centralised exception hierarchy and HTTP error helpers.

All application-specific exceptions descend from ChannelYieldError so
that callers can catch the base type or a specific subtype.

HTTP exception factories produce consistent RFC 7807-style error bodies
that never expose stack traces, SQL, internal paths, or tenant information
belonging to other tenants.
"""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status


# ─── Base exception ───────────────────────────────────────────────────────────


class ChannelYieldError(Exception):
    """Base class for all application-level errors."""

    def __init__(self, message: str, *, code: str = "internal_error") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


# ─── Auth / Tenant ────────────────────────────────────────────────────────────


class AuthenticationError(ChannelYieldError):
    """Caller could not be authenticated."""

    def __init__(self, message: str = "Authentication required.") -> None:
        super().__init__(message, code="authentication_required")


class AuthorizationError(ChannelYieldError):
    """Caller is authenticated but not authorised for this operation."""

    def __init__(self, message: str = "Access denied.") -> None:
        super().__init__(message, code="access_denied")


class TenantNotFoundError(ChannelYieldError):
    """Requested tenant does not exist or is not accessible."""

    def __init__(self, tenant_id: str | None = None) -> None:
        super().__init__("Tenant not found.", code="tenant_not_found")


class CrossTenantAccessError(ChannelYieldError):
    """Operation attempted to access another tenant's resources."""

    def __init__(self) -> None:
        super().__init__(
            "Cross-tenant access denied.", code="cross_tenant_access_denied"
        )


# ─── Resource ─────────────────────────────────────────────────────────────────


class NotFoundError(ChannelYieldError):
    """Requested resource does not exist within the caller's tenant."""

    def __init__(self, resource: str = "Resource") -> None:
        super().__init__(f"{resource} not found.", code="not_found")


class ConflictError(ChannelYieldError):
    """Operation would create a duplicate or conflicting resource."""

    def __init__(self, message: str = "Conflict.") -> None:
        super().__init__(message, code="conflict")


class ValidationError(ChannelYieldError):
    """Input data failed validation."""

    def __init__(self, message: str, *, details: list[Any] | None = None) -> None:
        super().__init__(message, code="validation_error")
        self.details = details or []


# ─── Integration ──────────────────────────────────────────────────────────────


class IntegrationError(ChannelYieldError):
    """External integration (e.g. Zoho Recruit) returned an error."""

    def __init__(self, provider: str, message: str) -> None:
        super().__init__(f"{provider} integration error: {message}", code="integration_error")
        self.provider = provider


class OAuthError(ChannelYieldError):
    """OAuth flow or token error."""

    def __init__(self, message: str, *, provider: str = "unknown") -> None:
        super().__init__(message, code="oauth_error")
        self.provider = provider


class RateLimitError(ChannelYieldError):
    """Rate limit encountered."""

    def __init__(
        self,
        provider: str = "service",
        retry_after: int | None = None,
        message: str | None = None,
    ) -> None:
        msg = message or f"{provider} rate limit exceeded."
        super().__init__(msg, code="rate_limited")
        self.provider = provider
        self.retry_after = retry_after


# ─── Analytics ────────────────────────────────────────────────────────────────


class AnalyticsError(ChannelYieldError):
    """Analytics calculation or configuration error."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="analytics_error")


class InsufficientDataError(ChannelYieldError):
    """Not enough data to compute the requested metric."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="insufficient_data")


# ─── HTTP exception factories ─────────────────────────────────────────────────
# These produce consistent, safe error bodies for FastAPI exception handlers.


def http_401(detail: str = "A valid bearer token is required.") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"error": "unauthorized", "message": detail},
        headers={"WWW-Authenticate": "Bearer"},
    )


def http_403(detail: str = "Access denied.") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"error": "access_denied", "message": detail},
    )


def http_404(resource: str = "Resource") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"error": "not_found", "message": f"{resource} not found."},
    )


def http_409(detail: str = "Conflict.") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"error": "conflict", "message": detail},
    )


def http_422(detail: str, *, errors: list[Any] | None = None) -> HTTPException:
    body: dict[str, Any] = {"error": "validation_error", "message": detail}
    if errors:
        body["details"] = errors
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail=body,
    )


def http_429(detail: str = "Too many requests.", retry_after: int | None = None) -> HTTPException:
    headers = {}
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail={"error": "rate_limited", "message": detail},
        headers=headers or None,
    )


def http_503(detail: str = "Service temporarily unavailable.") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"error": "service_unavailable", "message": detail},
    )
