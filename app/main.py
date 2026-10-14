"""Channel Yield Analytics Agent — FastAPI Application.

Production-oriented backend providing:
  - Validated configuration with production environment guards.
  - Correlation ID tracking (X-Request-ID propagation & sanitized logging).
  - Request body size limits (HTTP 413).
  - Uniform safe error responses without leaking tracebacks, SQL, or secrets.
  - Distinct /health (liveness) and /ready (readiness) probes.
  - Legacy backward-compatible /v1/analytics endpoint when configured.
"""
from __future__ import annotations

import re
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator

from fastapi import FastAPI, Header, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import Settings, get_settings
from app.core.exceptions import (
    AnalyticsError,
    AuthenticationError,
    AuthorizationError,
    ChannelYieldError,
    ConflictError,
    CrossTenantAccessError,
    InsufficientDataError,
    IntegrationError,
    NotFoundError,
    OAuthError,
    RateLimitError,
    TenantNotFoundError,
    ValidationError,
)
from app.core.logging import configure_logging, get_logger, request_id_var
from app.persistence.database import check_db_connectivity

logger = get_logger(__name__)

_SAFE_REQUEST_ID_REGEX = re.compile(r"^[a-zA-Z0-9_\-]{8,64}$")
_DEFAULT_BODY_LIMIT_BYTES = 65_536  # 64 KB for standard endpoints


# ─── Middleware ───────────────────────────────────────────────────────────────


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Ensure every request has a validated or newly generated X-Request-ID.

    Untrusted incoming IDs that do not match safe patterns are discarded
    and replaced with a secure random UUID4 to prevent log injection.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        incoming_id = request.headers.get("X-Request-ID", "").strip()
        if incoming_id and _SAFE_REQUEST_ID_REGEX.match(incoming_id):
            request_id = incoming_id
        else:
            request_id = f"req_{uuid.uuid4().hex}"

        request.state.request_id = request_id
        token = request_id_var.set(request_id)
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            request_id_var.reset(token)


class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    """Enforce request body size limits before body reading completes.

    Upload endpoints allow up to CY_MAX_UPLOAD_BYTES (50 MB default).
    All standard endpoints reject payloads larger than 64 KB with HTTP 413.
    """

    def __init__(self, app: Any, max_upload_bytes: int = 52_428_800) -> None:
        super().__init__(app)
        self.max_upload_bytes = max_upload_bytes

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        is_upload = any(p in path for p in ("/upload", "/import", "/ingestion", "/sync"))
        limit = self.max_upload_bytes if is_upload else _DEFAULT_BODY_LIMIT_BYTES

        content_length = request.headers.get("content-length")
        if content_length:
            try:
                length = int(content_length)
                if length > limit:
                    return JSONResponse(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        content={
                            "error": "payload_too_large",
                            "message": f"Request body exceeds the limit of {limit} bytes.",
                        },
                    )
            except ValueError:
                return JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content={
                        "error": "invalid_content_length",
                        "message": "Invalid Content-Length header.",
                    },
                )

        return await call_next(request)


# ─── Application Factory ──────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup and shutdown lifecycle."""
    settings = get_settings()
    configure_logging(settings.log_level)
    logger.info(
        "app.startup",
        env=settings.env,
        host=settings.host,
        port=settings.port,
        legacy_enabled=settings.legacy_enabled,
    )
    yield
    logger.info("app.shutdown")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create and configure the FastAPI application."""
    active_settings = settings or get_settings()

    app = FastAPI(
        title="Channel Yield Analytics Agent",
        description="Authoritative, deterministic channel yield analytics backend.",
        version="1.0.0",
        docs_url="/docs" if not active_settings.is_production else None,
        redoc_url="/redoc" if not active_settings.is_production else None,
        openapi_url="/openapi.json" if not active_settings.is_production else None,
        lifespan=lifespan,
    )

    # ── Middleware Registration ──
    app.add_middleware(CorrelationIdMiddleware)
    app.add_middleware(
        RequestSizeLimitMiddleware,
        max_upload_bytes=active_settings.max_upload_bytes,
    )
    cors_origins = active_settings.cors_origins
    allow_credentials = "*" not in cors_origins
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=allow_credentials,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

    # ── Global Exception Handlers ──

    @app.exception_handler(ChannelYieldError)
    async def channel_yield_error_handler(
        request: Request, exc: ChannelYieldError
    ) -> JSONResponse:
        status_map: dict[type[ChannelYieldError], int] = {
            AuthenticationError: status.HTTP_401_UNAUTHORIZED,
            AuthorizationError: status.HTTP_403_FORBIDDEN,
            CrossTenantAccessError: status.HTTP_403_FORBIDDEN,
            TenantNotFoundError: status.HTTP_404_NOT_FOUND,
            NotFoundError: status.HTTP_404_NOT_FOUND,
            ConflictError: status.HTTP_409_CONFLICT,
            ValidationError: status.HTTP_422_UNPROCESSABLE_ENTITY,
            RateLimitError: status.HTTP_429_TOO_MANY_REQUESTS,
            AnalyticsError: status.HTTP_400_BAD_REQUEST,
            InsufficientDataError: status.HTTP_400_BAD_REQUEST,
            IntegrationError: status.HTTP_502_BAD_GATEWAY,
            OAuthError: status.HTTP_502_BAD_GATEWAY,
        }
        http_status = status_map.get(type(exc), status.HTTP_500_INTERNAL_SERVER_ERROR)
        payload = {"error": exc.code, "message": exc.message}
        if isinstance(exc, ValidationError) and exc.details:
            payload["details"] = exc.details
        return JSONResponse(status_code=http_status, content=payload)

    @app.exception_handler(HTTPException)
    async def http_exception_handler(
        request: Request, exc: HTTPException
    ) -> JSONResponse:
        detail = exc.detail
        if isinstance(detail, dict):
            content = detail
        else:
            content = {"error": "http_error", "message": str(detail)}
        return JSONResponse(
            status_code=exc.status_code,
            content=content,
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        sanitized_errors = []
        for err in exc.errors():
            sanitized_errors.append(
                {
                    "loc": [str(loc) for loc in err.get("loc", [])],
                    "msg": err.get("msg", "Invalid value"),
                    "type": err.get("type", "value_error"),
                }
            )
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "error": "validation_error",
                "message": "Invalid request parameters.",
                "details": sanitized_errors,
            },
        )

    @app.exception_handler(Exception)
    async def generic_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        rid = getattr(request.state, "request_id", None) or request_id_var.get() or f"req_{uuid.uuid4().hex}"
        logger.error(
            "unhandled_server_exception",
            error_type=type(exc).__name__,
            error=str(exc),
            path=request.url.path,
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": "internal_error",
                "message": "An unexpected error occurred. Please contact support with the request ID.",
                "request_id": rid,
            },
        )

    # ── Health & Readiness Probes ──

    @app.get(
        "/health",
        summary="Liveness probe",
        tags=["System"],
    )
    async def health() -> dict[str, str]:
        """Liveness probe.

        Distinct purpose: checks whether the application process is running
        and capable of receiving HTTP connections. It does NOT depend on
        external databases or third-party providers.
        """
        return {
            "status": "ok",
            "service": "channel-yield-analytics-agent",
            "version": "1.0.0",
            "environment": active_settings.env,
        }

    @app.get(
        "/ready",
        summary="Readiness probe",
        tags=["System"],
    )
    async def readiness() -> JSONResponse:
        """Readiness probe.

        Distinct purpose: verifies that the database and internal dependencies
        are reachable and the instance is ready to accept production traffic.
        Returns HTTP 503 if database connectivity fails.
        """
        db_ok = check_db_connectivity()
        if not db_ok:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={
                    "status": "not_ready",
                    "service": "channel-yield-analytics-agent",
                    "database": "disconnected",
                },
            )
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "status": "ready",
                "service": "channel-yield-analytics-agent",
                "database": "connected",
            },
        )

    # ── Auth & Identity Router ──
    from app.auth.router import router as auth_router
    from app.analytics.router import router as analytics_router
    from app.agent.router import router as agent_router

    app.include_router(auth_router)
    app.include_router(analytics_router)
    app.include_router(agent_router)

    # ── Ingestion & Integration Routers ──
    from app.ingestion.router import router as ingestion_router
    from app.integrations.zoho.router import router as zoho_router

    app.include_router(ingestion_router)
    app.include_router(zoho_router)

    # ── Legacy Single-Tenant Endpoint Backward Compatibility ──

    if active_settings.legacy_enabled:
        from app.auth.dependencies import verify_legacy_token
        from pathlib import Path
        from analytics_service import compute_analytics

        @app.get(
            "/v1/analytics",
            summary="Legacy prototype analytics endpoint",
            tags=["Legacy"],
            deprecated=True,
        )
        async def legacy_analytics(
            authorization: str = Header(default="", alias="Authorization"),
            cohort_start: str = "2026-01-01",
            cohort_end: str = "2026-06-30",
            as_of_date: str | None = None,
            observation_days: int = 90,
            requisition_id: str | None = None,
            min_sample_size: int | None = None,
        ) -> Any:
            if not verify_legacy_token(authorization):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail={"error": "unauthorized", "message": "A valid bearer token is required"},
                    headers={"WWW-Authenticate": "Bearer"},
                )

            if observation_days < 0:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"error": "invalid_request", "message": "observation_days must be >= 0"},
                )

            from datetime import date
            as_of = as_of_date or date.today().isoformat()
            try:
                date.fromisoformat(cohort_start)
                date.fromisoformat(cohort_end)
                date.fromisoformat(as_of)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"error": "invalid_request", "message": str(exc)},
                )

            result = compute_analytics(
                Path(active_settings.legacy_db_path),
                client_id=active_settings.legacy_client_id,
                cohort_start=cohort_start,
                cohort_end=cohort_end,
                as_of_date=as_of,
                observation_days=observation_days,
                requisition_id=requisition_id,
                min_sample_size=min_sample_size,
            )
            if result.get("status") == "not_found":
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=result,
                )
            return result

    return app


app = create_app()
