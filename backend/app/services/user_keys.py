"""
app/services/user_keys.py
-------------------------
Helpers for per-connector, per-user LLM API keys. A user supplies one key per LLM
connector (`llm_providers` row); generation uses the ACTIVE connector with that
user's key for it. Everything else about the connector stays global.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.crypto import decrypt
from app.models import LlmProvider, UserLlmKey


def active_provider(session: Session) -> LlmProvider | None:
    """The single active LLM connector (or None → legacy env fallback)."""
    return session.scalars(select(LlmProvider).where(LlmProvider.active.is_(True))).first()


def get_user_key(session: Session, user_id: uuid.UUID, provider_id: uuid.UUID) -> str:
    """The user's DECRYPTED key for a connector, or "" if none stored."""
    row = session.scalar(
        select(UserLlmKey).where(UserLlmKey.user_id == user_id,
                                 UserLlmKey.provider_id == provider_id))
    return decrypt(row.api_key_enc) if (row and row.api_key_enc) else ""


def user_has_active_key(session: Session, user_id: uuid.UUID) -> bool:
    """Whether the user has a key for the currently active connector. True when no
    connector is active (legacy env fallback path needs no per-user key)."""
    prov = active_provider(session)
    if prov is None:
        return True
    return bool(get_user_key(session, user_id, prov.id))
