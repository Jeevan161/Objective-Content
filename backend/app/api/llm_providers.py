"""
app/api/llm_providers.py
------------------------
Manage the user-configurable LLM connectors (OpenAI / OpenRouter / Anthropic / the
internal proxy). Keys are stored ENCRYPTED at rest (Fernet); they are NEVER returned in
full — list responses show a masked tail only. Exactly one connector is `active` and
drives every pipeline LLM call (see `app/mcq_pipeline/llm_factory.py`).

  GET    /api/llm/providers/                 list connectors (masked keys)
  POST   /api/llm/providers/                 create or update a connector (by name)
  POST   /api/llm/providers/{name}/activate/ make this the active connector
  POST   /api/llm/providers/{name}/test/     live connectivity probe (tiny call)
  DELETE /api/llm/providers/{name}/          remove a connector
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.crypto import decrypt, encrypt, mask
from app.db.session import get_session
from app.mcq_pipeline import llm_factory
from app.models import LlmProvider

router = APIRouter(prefix="/api/llm/providers")

_ADAPTERS = {"openai_compatible", "anthropic"}


class ProviderUpsert(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    adapter: str = "openai_compatible"     # openai_compatible | anthropic
    model: str = ""
    base_url: str = ""
    # Plaintext key from the UI. Omit (or send empty) to KEEP the existing key on update.
    api_key: str | None = None
    default_headers: dict = Field(default_factory=dict)
    extra_body: dict = Field(default_factory=dict)   # e.g. {"metadata": {...}} for the proxy


def _view(row: LlmProvider) -> dict:
    """Public, safe representation of a connector — the key is masked, never returned."""
    return {
        "name": row.name,
        "adapter": row.adapter,
        "model": row.model,
        "base_url": row.base_url,
        "has_key": bool(row.api_key_enc),
        "key_masked": mask(decrypt(row.api_key_enc)),
        "default_headers": row.default_headers or {},
        "extra_body": row.extra_body or {},
        "active": row.active,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


@router.get("/")
def list_providers(session: Session = Depends(get_session)) -> list[dict]:
    rows = session.scalars(select(LlmProvider).order_by(LlmProvider.name)).all()
    return [_view(r) for r in rows]


@router.post("/")
def upsert_provider(body: ProviderUpsert, session: Session = Depends(get_session)) -> dict:
    if body.adapter not in _ADAPTERS:
        raise HTTPException(status_code=400, detail=f"adapter must be one of {sorted(_ADAPTERS)}")

    row = session.scalars(select(LlmProvider).where(LlmProvider.name == body.name)).first()
    if row is None:
        row = LlmProvider(name=body.name, active=False)
        session.add(row)

    row.adapter = body.adapter
    row.model = body.model
    row.base_url = body.base_url
    row.default_headers = body.default_headers or {}
    row.extra_body = body.extra_body or {}
    # Only overwrite the stored key when a new plaintext key is supplied — an empty/omitted
    # api_key on update preserves the existing encrypted secret (UI never sees the real key).
    if body.api_key is not None and body.api_key != "":
        row.api_key_enc = encrypt(body.api_key)

    session.commit()
    session.refresh(row)
    llm_factory.refresh()          # active config may have changed
    return _view(row)


@router.post("/{name}/activate/")
def activate_provider(name: str, session: Session = Depends(get_session)) -> dict:
    target = session.scalars(select(LlmProvider).where(LlmProvider.name == name)).first()
    if target is None:
        raise HTTPException(status_code=404, detail="Unknown connector.")
    # Single-active invariant: clear every other connector, set this one.
    for row in session.scalars(select(LlmProvider)).all():
        row.active = (row.name == name)
    session.commit()
    session.refresh(target)
    llm_factory.refresh()
    return _view(target)


@router.post("/{name}/test/")
def test_provider(name: str, session: Session = Depends(get_session)) -> dict:
    """Live connectivity probe: build a model from THIS connector and make a tiny call."""
    row = session.scalars(select(LlmProvider).where(LlmProvider.name == name)).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown connector.")
    # The proxy requires a non-empty metadata `unit`/`step`; supply probe values.
    llm_factory.set_call_context(unit="connectivity-test", step="test")
    try:
        model = llm_factory.chat_model_from_row(row, temperature=0, max_tokens=16)
        resp = model.invoke([{"role": "user", "content": "Reply with the single word: ok"}])
        text = (getattr(resp, "content", None) or str(resp)).strip()
        return {"ok": True, "name": row.name, "model": row.model, "reply": text[:200]}
    except Exception as exc:  # noqa: BLE001 — surface the failure reason to the UI
        return {"ok": False, "name": row.name, "model": row.model, "error": str(exc)[:500]}


@router.delete("/{name}/")
def delete_provider(name: str, session: Session = Depends(get_session)) -> dict:
    row = session.scalars(select(LlmProvider).where(LlmProvider.name == name)).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown connector.")
    was_active = row.active
    session.delete(row)
    session.commit()
    llm_factory.refresh()          # falls back to legacy/another connector if active was removed
    return {"deleted": name, "was_active": was_active}
