"""Sourcing attribution interface for Zoho Recruit integration.

Re-exports canonical attribution logic from app.analytics.attribution for modular access.
"""
from __future__ import annotations

from app.analytics.attribution import (
    CANONICAL_CHANNELS,
    DEFAULT_CHANNEL_RULES,
    UNKNOWN_TOKENS,
    normalize_channel,
    resolve_application_attribution,
)

__all__ = [
    "CANONICAL_CHANNELS",
    "DEFAULT_CHANNEL_RULES",
    "UNKNOWN_TOKENS",
    "normalize_channel",
    "resolve_application_attribution",
]
