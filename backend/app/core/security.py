"""
app/core/security.py
---------------------
Password hashing (bcrypt) + JWT access tokens (PyJWT). Stateless auth: login
returns a signed JWT the SPA sends as `Authorization: Bearer <token>`.

bcrypt only considers the first 72 bytes of input, so we SHA-256 + base64 the
password first — a fixed 44-byte digest that preserves full entropy and never
trips bcrypt's length limit. (passlib is avoided: it's unmaintained and breaks
with bcrypt 4.x.)
"""

from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from app.core.config import settings


def _prehash(plain: str) -> bytes:
    return base64.b64encode(hashlib.sha256(plain.encode("utf-8")).digest())


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(_prehash(plain), bcrypt.gensalt()).decode("ascii")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(_prehash(plain), hashed.encode("ascii"))
    except (ValueError, TypeError):
        return False


def create_access_token(*, sub: str, role: str) -> str:
    """Sign a JWT carrying the user id (`sub`) and role, with an expiry."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": sub,
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.jwt_expire_minutes)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict | None:
    """Decode/verify a JWT. Returns the payload, or None if invalid/expired."""
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError:
        return None
