"""Recruitment sourcing channel attribution and normalization engine.

Implements Phase 5 requirements:
  - Application-level sourcing attribution prioritized over candidate-level source.
  - Candidate-level source utilized strictly as a documented fallback.
  - Attribution method ('application_field', 'candidate_fallback', 'unattributed')
    and attribution confidence scoring (0.0 to 1.0).
  - Configurable channel normalization mapping preserving raw source and canonical channel.
  - No silent invention of sources; explicit unknown classification.
"""
from __future__ import annotations

from typing import Any, Optional, Tuple

CANONICAL_CHANNELS = [
    "LinkedIn",
    "Employee Referral",
    "Job Portals",
    "Recruitment Agencies",
    "Company Careers Website",
    "Direct Applications",
    "Recruitment Marketing Campaigns",
    "Vendor Sources",
    "Other",
]

# Configurable normalization rules mapping raw source substrings/tokens to canonical channels
DEFAULT_CHANNEL_RULES: list[tuple[list[str], str]] = [
    (["linkedin"], "LinkedIn"),
    (["referral", "employee referral", "internal referral", "referee"], "Employee Referral"),
    (["indeed", "glassdoor", "monster", "naukri", "dice", "ziprecruiter", "job portal", "portal"], "Job Portals"),
    (["agency", "staffing", "headhunter", "recruitment agency", "external agency"], "Recruitment Agencies"),
    (["careers", "career site", "company site", "careers page", "website", "careers website"], "Company Careers Website"),
    (["direct", "walk-in", "email application", "direct application", "inbound"], "Direct Applications"),
    (["campaign", "marketing", "google ads", "facebook", "adwords", "social media"], "Recruitment Marketing Campaigns"),
    (["vendor", "contractor supplier", "vendor source"], "Vendor Sources"),
]

UNKNOWN_TOKENS = frozenset({"", "unknown", "unattributed", "n/a", "na", "null", "none"})


def normalize_channel(raw_source: Optional[str]) -> Tuple[str, str, float]:
    """Normalize a raw recruitment source string into a canonical channel.

    Returns:
      (canonical_channel, raw_source_string, confidence_score)
    """
    if not raw_source or not isinstance(raw_source, str):
        return "unknown", "unknown", 0.0

    clean_raw = raw_source.strip()
    clean_lower = clean_raw.lower()

    if clean_lower in UNKNOWN_TOKENS:
        return "unknown", clean_raw, 0.0

    for keywords, canonical in DEFAULT_CHANNEL_RULES:
        if any(k in clean_lower for k in keywords):
            return canonical, clean_raw, 1.0

    # If non-empty but doesn't match standard patterns, preserve as Other
    return "Other", clean_raw, 0.70


def resolve_application_attribution(
    app_record: dict[str, Any],
    candidate_source_map: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """Resolve sourcing channel attribution for an application with priority and confidence.

    Attribution Rules:
      1. Inspect application-level source fields first ('Lead_Source', 'Source', 'Application_Source').
      2. If application-level source is missing or unknown, fall back to candidate-level source.
      3. If both are missing or unknown, mark as 'unattributed' with confidence 0.0.
      4. Preserves both raw_source and normalized source_channel.
    """
    cand_map = candidate_source_map or {}

    cand_obj = app_record.get("Candidate_ID") or {}
    cand_id = str(cand_obj.get("id") if isinstance(cand_obj, dict) else (cand_obj or "")).strip()

    # 1. Primary: Application-level source inspection
    app_raw_source = (
        app_record.get("Lead_Source")
        or app_record.get("Source")
        or app_record.get("Application_Source")
        or app_record.get("Channel")
    )

    if app_raw_source and str(app_raw_source).strip().lower() not in UNKNOWN_TOKENS:
        canonical, raw, conf = normalize_channel(str(app_raw_source))
        return {
            "source_channel": canonical,
            "raw_source": raw,
            "attribution_method": "application_field",
            "attribution_confidence": conf,
        }

    # 2. Fallback: Candidate-level source
    cand_raw_source = cand_map.get(cand_id)
    if cand_raw_source and str(cand_raw_source).strip().lower() not in UNKNOWN_TOKENS:
        canonical, raw, conf = normalize_channel(str(cand_raw_source))
        # Fallback has a slightly discounted confidence (e.g., 0.85 * base confidence)
        return {
            "source_channel": canonical,
            "raw_source": raw,
            "attribution_method": "candidate_fallback",
            "attribution_confidence": round(conf * 0.85, 2),
        }

    # 3. Unattributed
    return {
        "source_channel": "unknown",
        "raw_source": "unknown",
        "attribution_method": "unattributed",
        "attribution_confidence": 0.0,
    }
