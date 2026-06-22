"""
app/core/crypto.py
------------------
Symmetric encryption for secrets stored at rest (LLM provider API keys).

Uses Fernet (AES-128-CBC + HMAC). The key is derived from ``settings.llm_secret_key``
when set, else from the database URL as a zero-config fallback. Encrypted keys live in
the DB; plaintext keys never do.

IMPORTANT — set ``LLM_SECRET_KEY`` in any real deployment. The database-URL fallback is
only stable WITHIN one connection string: the same Postgres reached via different URLs
(e.g. the API container uses ``@db:5432`` while a host tool uses ``@localhost:5545``)
derives DIFFERENT keys, so secrets written in one environment can't be decrypted in the
other. A shared ``LLM_SECRET_KEY`` (in the ``.env`` that both load) makes encryption
portable across host and container.
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings


def _fernet() -> Fernet:
    secret = (settings.llm_secret_key or settings.database_url or "objective-content").encode("utf-8")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret).digest())   # 32-byte -> valid Fernet key
    return Fernet(key)


def encrypt(plaintext: str) -> str:
    """Encrypt a secret for storage. Empty input -> empty string."""
    if not plaintext:
        return ""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    """Decrypt a stored secret. Returns "" on empty/invalid token (e.g. key rotated)."""
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError):
        return ""


def mask(plaintext: str) -> str:
    """A safe display form of a key — last 4 chars only, never the full secret."""
    if not plaintext:
        return ""
    return ("•" * 6) + plaintext[-4:] if len(plaintext) >= 4 else "•" * 6
