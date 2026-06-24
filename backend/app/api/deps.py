"""
app/api/deps.py
---------------
Auth dependencies shared across routers: resolve the current user from the bearer
token, and gate routes by active-status / admin-role.
"""

from __future__ import annotations

import uuid

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.security import decode_token
from app.db.session import get_session
from app.models import User

# auto_error=False so we can return a clean 401 (not the default 403) when missing.
_bearer = HTTPBearer(auto_error=False)


def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: Session = Depends(get_session),
) -> User:
    if creds is None or not creds.credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Not authenticated.")
    payload = decode_token(creds.credentials)
    sub = (payload or {}).get("sub")
    if not sub:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid or expired token.")
    try:
        user = session.get(User, uuid.UUID(str(sub)))
    except (ValueError, TypeError):
        user = None
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="User no longer exists.")
    return user


def require_active(user: User = Depends(get_current_user)) -> User:
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Your account is pending admin approval.")
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != User.ROLE_ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Admin access required.")
    return user
