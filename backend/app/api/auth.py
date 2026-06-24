"""
app/api/auth.py
---------------
Auth + admin endpoints.

- /auth/*   : register (creates an INACTIVE account), login (JWT), me, personal API key.
- /admin/*  : (admin-only) approve/deactivate users, change role, per-user stats, task logs.

Self-registration creates an inactive user; an admin must approve before the account
can generate/load. Each user stores ONE Fernet-encrypted LLM API key; all other provider
settings stay global.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_admin
from app.core.crypto import encrypt
from app.core.security import create_access_token, hash_password, verify_password
from app.db.session import get_session
from app.models import BetaLoad, LlmProvider, McqRun, TaskLog, User, UserLlmKey
from app.schemas import (
    ApiKeyRequest,
    LoginRequest,
    RegisterRequest,
    RoleRequest,
    serialize_user,
)
from app.services.user_keys import active_provider, user_has_active_key

router = APIRouter(prefix="/api", tags=["auth"])


# --- public auth ----------------------------------------------------------- #
@router.post("/auth/register", status_code=status.HTTP_201_CREATED)
def register(body: RegisterRequest, session: Session = Depends(get_session)) -> dict:
    email = body.email.strip().lower()
    if not email or not body.password:
        raise HTTPException(status_code=400, detail="Email and password are required.")
    exists = session.scalar(select(User).where(User.email == email))
    if exists is not None:
        raise HTTPException(status_code=409, detail="An account with this email already exists.")
    user = User(email=email, name=body.name.strip(), password_hash=hash_password(body.password),
                role=User.ROLE_USER, is_active=False)
    session.add(user)
    session.commit()
    session.refresh(user)
    return {"user": serialize_user(user),
            "message": "Account created. An admin must approve it before you can generate."}


@router.post("/auth/login")
def login(body: LoginRequest, session: Session = Depends(get_session)) -> dict:
    email = body.email.strip().lower()
    user = session.scalar(select(User).where(User.email == email))
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    token = create_access_token(sub=str(user.id), role=user.role)
    return {"access_token": token, "token_type": "bearer", "user": serialize_user(user)}


@router.get("/auth/me")
def me(user: User = Depends(get_current_user)) -> dict:
    return serialize_user(user)


@router.get("/auth/me/keys")
def my_keys(user: User = Depends(get_current_user),
            session: Session = Depends(get_session)) -> list[dict]:
    """Every LLM connector + whether the current user has supplied a key for it."""
    providers = session.scalars(select(LlmProvider).order_by(LlmProvider.name)).all()
    have = {k.provider_id for k in session.scalars(
        select(UserLlmKey).where(UserLlmKey.user_id == user.id)).all()
        if k.api_key_enc}
    return [{
        "provider_id": p.id, "name": p.name, "adapter": p.adapter, "model": p.model,
        "active": p.active, "has_key": p.id in have,
    } for p in providers]


@router.put("/auth/me/keys/{provider_id}")
def set_key(provider_id: uuid.UUID, body: ApiKeyRequest,
            user: User = Depends(get_current_user),
            session: Session = Depends(get_session)) -> dict:
    key = body.api_key.strip()
    if not key:
        raise HTTPException(status_code=400, detail="API key cannot be empty.")
    if session.get(LlmProvider, provider_id) is None:
        raise HTTPException(status_code=404, detail="Connector not found.")
    row = session.scalar(select(UserLlmKey).where(
        UserLlmKey.user_id == user.id, UserLlmKey.provider_id == provider_id))
    if row is None:
        row = UserLlmKey(user_id=user.id, provider_id=provider_id)
        session.add(row)
    row.api_key_enc = encrypt(key)
    session.commit()
    return {"provider_id": provider_id, "has_key": True}


@router.delete("/auth/me/keys/{provider_id}")
def clear_key(provider_id: uuid.UUID, user: User = Depends(get_current_user),
              session: Session = Depends(get_session)) -> dict:
    row = session.scalar(select(UserLlmKey).where(
        UserLlmKey.user_id == user.id, UserLlmKey.provider_id == provider_id))
    if row is not None:
        session.delete(row)
        session.commit()
    return {"provider_id": provider_id, "has_key": False}


# --- admin ------------------------------------------------------------------ #
def _get_user_or_404(session: Session, user_id: uuid.UUID) -> User:
    target = session.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found.")
    return target


@router.get("/admin/users")
def list_users(_: User = Depends(require_admin),
               session: Session = Depends(get_session)) -> list[dict]:
    users = session.scalars(select(User).order_by(User.created_at.desc())).all()
    return [serialize_user(u) for u in users]


@router.post("/admin/users/{user_id}/approve")
def approve_user(user_id: uuid.UUID, _: User = Depends(require_admin),
                 session: Session = Depends(get_session)) -> dict:
    target = _get_user_or_404(session, user_id)
    target.is_active = True
    session.add(target)
    session.commit()
    return serialize_user(target)


@router.post("/admin/users/{user_id}/deactivate")
def deactivate_user(user_id: uuid.UUID, admin: User = Depends(require_admin),
                    session: Session = Depends(get_session)) -> dict:
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="You cannot deactivate your own account.")
    target = _get_user_or_404(session, user_id)
    target.is_active = False
    session.add(target)
    session.commit()
    return serialize_user(target)


@router.post("/admin/users/{user_id}/role")
def set_role(user_id: uuid.UUID, body: RoleRequest, admin: User = Depends(require_admin),
             session: Session = Depends(get_session)) -> dict:
    if body.role not in (User.ROLE_USER, User.ROLE_ADMIN):
        raise HTTPException(status_code=400, detail="role must be 'user' or 'admin'.")
    if user_id == admin.id and body.role != User.ROLE_ADMIN:
        raise HTTPException(status_code=400, detail="You cannot demote your own admin account.")
    target = _get_user_or_404(session, user_id)
    target.role = body.role
    session.add(target)
    session.commit()
    return serialize_user(target)


@router.get("/admin/stats")
def admin_stats(_: User = Depends(require_admin),
                session: Session = Depends(get_session)) -> dict:
    # Per-user generation + load counts (grouped), merged onto the user list.
    gen_counts = dict(session.execute(
        select(McqRun.created_by, func.count()).group_by(McqRun.created_by)).all())
    load_counts = dict(session.execute(
        select(BetaLoad.user_id, func.count()).group_by(BetaLoad.user_id)).all())
    # Who has a key for the ACTIVE connector (that's what "needs a key" means).
    prov = active_provider(session)
    have_active = set()
    if prov is not None:
        have_active = {k.user_id for k in session.scalars(
            select(UserLlmKey).where(UserLlmKey.provider_id == prov.id)).all()
            if k.api_key_enc}
    users = session.scalars(select(User).order_by(User.created_at.desc())).all()
    rows = []
    for u in users:
        row = serialize_user(u)
        row["generations"] = int(gen_counts.get(u.id, 0))
        row["loads"] = int(load_counts.get(u.id, 0))
        row["has_active_key"] = (prov is None) or (u.id in have_active)
        rows.append(row)
    return {
        "users": rows,
        "active_connector": prov.name if prov is not None else None,
        "pending_approval": sum(1 for r in rows if not r["is_active"]),
        "needs_api_key": sum(1 for r in rows if r["is_active"] and not r["has_active_key"]),
        "total_generations": int(sum(gen_counts.values())),
        "total_loads": int(sum(load_counts.values())),
    }


@router.get("/admin/logs")
def admin_logs(level: str | None = None, limit: int = 200,
               _: User = Depends(require_admin),
               session: Session = Depends(get_session)) -> list[dict]:
    stmt = select(TaskLog).order_by(TaskLog.created_at.desc()).limit(min(limit, 1000))
    if level:
        stmt = stmt.where(TaskLog.level == level.upper())
    logs = session.scalars(stmt).all()
    return [{
        "id": t.id, "task_type": t.task_type, "level": t.level, "event": t.event,
        "message": t.message, "job_id": t.job_id, "run_id": t.run_id,
        "user_id": t.user_id, "detail": t.detail, "created_at": t.created_at,
    } for t in logs]
