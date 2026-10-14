"""JWT token creation and verification.

Tokens encode:
  - sub:  subject (user ID or API key ID)
  - tid:  tenant_id (for fast tenant resolution without a DB lookup)
  - role: membership role
  - exp:  expiry
  - jti:  unique token ID (for future revocation support)

Verification checks:
  - Signature (using the application's secret key)
  - Algorithm (must be HS256; RS256 if key is RSA — not supported in this build)
  - Expiry
  - Required claims

Tenant ID from the token is used ONLY to avoid an extra DB hop.
Every sensitive operation MUST re-verify the membership exists and is active.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from jose import JWTError, jwt

from app.core.config import get_settings
from app.core.exceptions import AuthenticationError
from app.core.logging import get_logger

logger = get_logger(__name__)

_REQUIRED_CLAIMS = {"sub", "tid", "exp"}


def create_access_token(
    *,
    user_id: str,
    tenant_id: str,
    role: str,
    expire_minutes: Optional[int] = None,
) -> str:
    """Create a signed JWT access token."""
    settings = get_settings()
    expire = expire_minutes or settings.access_token_expire_minutes
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "tid": tenant_id,
        "role": role,
        "jti": str(uuid.uuid4()),
        "iat": now,
        "exp": now + timedelta(minutes=expire),
        "iss": "channel-yield-api",
        "aud": "channel-yield-client",
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict:
    """Decode and validate a JWT access token.

    Raises AuthenticationError for any invalid, expired, or malformed token.
    Never logs the raw token value.
    """
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.secret_key,
            algorithms=[settings.jwt_algorithm],
            audience="channel-yield-client",
            issuer="channel-yield-api",
        )
    except JWTError as exc:
        # Log that a failure occurred — not the token content.
        logger.warning("auth.token_decode_failed", reason=str(exc))
        raise AuthenticationError("Token is invalid or expired.") from exc

    missing = _REQUIRED_CLAIMS - set(payload)
    if missing:
        logger.warning("auth.token_missing_claims", missing=sorted(missing))
        raise AuthenticationError("Token is missing required claims.")

    return payload
