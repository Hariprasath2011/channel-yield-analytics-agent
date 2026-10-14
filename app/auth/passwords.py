"""Password hashing using native bcrypt.

Never store plaintext passwords. Never log password values.
Use `verify_password` for authentication; constant-time comparison is enforced
by bcrypt.checkpw under the hood.
"""
from __future__ import annotations

import bcrypt


def hash_password(plain: str) -> str:
    """Return a bcrypt hash of `plain`. Safe to store in the database."""
    # Bcrypt operates on max 72 bytes. Encode to utf-8.
    pw_bytes = plain.encode("utf-8")[:72]
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(pw_bytes, salt).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """Return True if `plain` matches `hashed`. Constant-time comparison."""
    try:
        pw_bytes = plain.encode("utf-8")[:72]
        hash_bytes = hashed.encode("utf-8")
        return bcrypt.checkpw(pw_bytes, hash_bytes)
    except Exception:
        return False
