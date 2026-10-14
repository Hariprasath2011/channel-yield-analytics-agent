"""Cryptographic utilities for OAuth token encryption and decryption.

Uses Fernet symmetric authenticated encryption to ensure tokens are never
stored in plaintext in the database or leaked in application logs.
"""
from __future__ import annotations

import base64
import hashlib
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import get_settings
from app.core.exceptions import OAuthError


def _get_fernet() -> Fernet:
    settings = get_settings()
    key_str = settings.encryption_key.strip()
    if not key_str:
        # Development fallback: derive deterministic key from secret_key
        derived = hashlib.sha256(settings.secret_key.encode("utf-8")).digest()
        key_str = base64.urlsafe_b64encode(derived).decode("utf-8")
    try:
        return Fernet(key_str.encode("utf-8"))
    except Exception as exc:
        raise OAuthError("Failed to initialize cryptographic provider") from exc


def encrypt_token(plain_token: Optional[str]) -> Optional[str]:
    """Encrypt a sensitive OAuth token."""
    if not plain_token:
        return None
    fernet = _get_fernet()
    return fernet.encrypt(plain_token.encode("utf-8")).decode("utf-8")


def decrypt_token(encrypted_token: Optional[str]) -> Optional[str]:
    """Decrypt a stored sensitive OAuth token."""
    if not encrypted_token:
        return None
    fernet = _get_fernet()
    try:
        return fernet.decrypt(encrypted_token.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise OAuthError("Token decryption failed: invalid or corrupt token") from exc
