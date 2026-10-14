"""Structured logging configuration.

Emits JSON-structured log records so that log aggregation tools can parse
fields reliably. Sanitises records to prevent accidental credential leakage.

Usage:
    from app.core.logging import get_logger
    logger = get_logger(__name__)
    logger.info("analytics.query", tenant_id=tenant_id, duration_ms=42)
"""
from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from typing import Any

from contextvars import ContextVar

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

_SCRUB_KEYS = frozenset({
    "password", "secret", "token", "api_key", "access_token", "refresh_token",
    "authorization", "client_secret", "encryption_key", "secret_key",
})


def _scrub(value: Any, key: str = "") -> Any:
    """Recursively replace sensitive values with a redacted marker."""
    if isinstance(key, str) and key.lower() in _SCRUB_KEYS:
        return "[REDACTED]"
    if isinstance(value, dict):
        return {k: _scrub(v, k) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(v) for v in value]
    return value


class _JsonFormatter(logging.Formatter):
    """Emit structured JSON log lines."""

    def format(self, record: logging.LogRecord) -> str:
        extras: dict[str, Any] = {}
        for key, val in record.__dict__.items():
            if key not in (
                "name", "msg", "args", "levelname", "levelno", "pathname",
                "filename", "module", "exc_info", "exc_text", "stack_info",
                "lineno", "funcName", "created", "msecs", "relativeCreated",
                "thread", "threadName", "processName", "process", "message",
                "taskName", "asctime",
            ) and not key.startswith("_"):
                output_key = key[6:] if key.startswith("field_") else key
                extras[output_key] = val
        entry: dict[str, Any] = {
            "ts": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        rid = request_id_var.get()
        if rid:
            entry["request_id"] = rid
        if extras:
            entry.update(_scrub(extras))
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Configure root logger to emit structured JSON to stdout."""
    numeric = getattr(logging, level.upper(), logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(numeric)
    # Suppress noisy third-party loggers in non-debug mode.
    if numeric > logging.DEBUG:
        for noisy in ("uvicorn.access", "sqlalchemy.engine"):
            logging.getLogger(noisy).setLevel(logging.WARNING)


_RESERVED_LOGRECORD_ATTRS = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "asctime", "taskName",
})


class StructuredLogger(logging.LoggerAdapter):
    """Logger adapter that moves arbitrary kwargs into the extra dict for JSON formatting."""

    def process(self, msg: Any, kwargs: Any) -> tuple[Any, Any]:
        extra = kwargs.setdefault("extra", {})
        for k in list(kwargs.keys()):
            if k not in ("exc_info", "stack_info", "stacklevel", "extra"):
                val = kwargs.pop(k)
                target_k = f"field_{k}" if k in _RESERVED_LOGRECORD_ATTRS else k
                extra[target_k] = val
        return msg, kwargs


def get_logger(name: str) -> StructuredLogger:
    return StructuredLogger(logging.getLogger(name), {})
