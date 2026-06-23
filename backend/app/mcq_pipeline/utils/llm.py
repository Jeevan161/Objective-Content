"""
app/mcq_pipeline/utils/llm.py
-----------------------------
Central LLM utilities — a merge of the former `config` + `llm_factory` + `lo_llm`:

* OpenRouter settings shim (OPENROUTER_* / *_MODEL) sourced from app settings — the legacy
  fallback config.
* `make_chat_model()` — EVERY pipeline LLM call goes through here; builds a LangChain chat model
  from the ACTIVE LlmProvider row (openai_compatible / anthropic), falling back to the legacy
  OpenRouter settings when none is configured. Active config is cached; `refresh()` reloads it.
* `chat()` / `parse_json()` — the plain-text completion + tolerant JSON extractor the agent nodes use.
"""

from __future__ import annotations

import contextvars
import copy
import json
import re
import threading

from app.core.config import settings

# --- OpenRouter settings shim (legacy fallback config) ---------------------- #
OPENROUTER_API_KEY = settings.openrouter_api_key
OPENROUTER_BASE_URL = settings.openrouter_base_url
AGENT_MODEL = settings.mcq_agent_model          # strong model: LO agents + question gen/review
CHAT_MODEL = settings.rag_chat_model
EMBED_MODEL = settings.embed_model

_UNSET = object()
_active = _UNSET                # cached active-provider config dict, or None, or _UNSET (unloaded)
_lock = threading.Lock()
_meta_ctx: contextvars.ContextVar[dict] = contextvars.ContextVar("llm_meta_ctx", default={})


def set_call_context(**kw) -> None:
    """Merge per-run context (e.g. unit=<session label>, step=<stage>) into the proxy
    metadata. Context-local, so concurrent runs don't clash."""
    cur = dict(_meta_ctx.get())
    cur.update({k: v for k, v in kw.items() if v})
    _meta_ctx.set(cur)


def refresh() -> None:
    """Drop the active-provider cache so the next call reloads it from the DB."""
    global _active
    with _lock:
        _active = _UNSET


def _active_config():
    global _active
    if _active is _UNSET:
        with _lock:
            if _active is _UNSET:
                _active = _query_active()
    return _active


def _row_to_cfg(row) -> dict:
    """Convert a LlmProvider row into a use-time config dict (decrypting the key)."""
    from app.core.crypto import decrypt
    return {
        "name": row.name, "adapter": row.adapter, "model": row.model,
        "base_url": row.base_url or None, "api_key": decrypt(row.api_key_enc),
        "default_headers": row.default_headers or {}, "extra_body": row.extra_body or {},
    }


def _query_active():
    try:
        from sqlalchemy import select

        from app.db.session import SessionLocal
        from app.models import LlmProvider
        with SessionLocal() as s:
            row = s.scalars(select(LlmProvider).where(LlmProvider.active.is_(True))).first()
        return _row_to_cfg(row) if row is not None else None
    except Exception:  # noqa: BLE001 — DB down / table missing -> legacy fallback
        return None


def _resolve_extra_body(extra_body: dict) -> dict:
    """Fill the proxy metadata's `unit`/`step` from the run context (if set)."""
    if not extra_body:
        return {}
    eb = copy.deepcopy(extra_body)
    md = eb.get("metadata")
    if isinstance(md, dict):
        ctx = _meta_ctx.get()
        if ctx.get("unit"):
            md["unit"] = ctx["unit"]
        if ctx.get("step"):
            md["step"] = ctx["step"]
    return eb


def _legacy(temperature: float):
    """The original hardcoded OpenRouter client — used when no provider is configured."""
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(model=AGENT_MODEL, api_key=OPENROUTER_API_KEY,
                      base_url=OPENROUTER_BASE_URL, temperature=temperature)


def _build_model(cfg: dict, temperature: float, max_tokens: int | None = None):
    """Build a LangChain chat model from a use-time config dict (see `_row_to_cfg`)."""
    if cfg["adapter"] == "anthropic":
        from langchain_anthropic import ChatAnthropic
        kw: dict = {"model": cfg["model"], "temperature": temperature,
                    "max_tokens": max_tokens or 4096}
        if cfg["api_key"]:
            kw["api_key"] = cfg["api_key"]
        if cfg["base_url"]:
            kw["base_url"] = cfg["base_url"]
        if cfg["default_headers"]:
            kw["default_headers"] = cfg["default_headers"]
        return ChatAnthropic(**kw)

    # openai_compatible: OpenAI / OpenRouter / internal proxy
    from langchain_openai import ChatOpenAI
    kw = {"model": cfg["model"], "temperature": temperature}
    if cfg["api_key"]:
        kw["api_key"] = cfg["api_key"]
    if cfg["base_url"]:
        kw["base_url"] = cfg["base_url"]
    if cfg["default_headers"]:
        kw["default_headers"] = cfg["default_headers"]
    if max_tokens:
        kw["max_tokens"] = max_tokens
    eb = _resolve_extra_body(cfg["extra_body"])
    if eb:
        kw["extra_body"] = eb       # the proxy reads required metadata from here
    return ChatOpenAI(**kw)


def make_chat_model(temperature: float = 0.2, *, max_tokens: int | None = None):
    """Build a LangChain chat model from the active provider (or legacy fallback)."""
    cfg = _active_config()
    if cfg is None:
        return _legacy(temperature)
    return _build_model(cfg, temperature, max_tokens)


def chat_model_from_row(row, *, temperature: float = 0, max_tokens: int | None = None):
    """Build a chat model from a SPECIFIC LlmProvider row (used by the 'test' endpoint to
    probe a connector regardless of which one is active)."""
    return _build_model(_row_to_cfg(row), temperature, max_tokens)


def seed_providers() -> int:
    """Idempotently seed connectors when none exist: an ACTIVE 'openrouter' mirroring the
    current settings (zero behavior change) + an inactive 'proxy' preset (gpt-4o + the
    required metadata block). Best-effort; returns the count inserted."""
    try:
        from sqlalchemy import func, select

        from app.core.config import settings
        from app.core.crypto import encrypt
        from app.db.session import SessionLocal
        from app.models import LlmProvider
        with SessionLocal() as s:
            if s.scalar(select(func.count()).select_from(LlmProvider)) > 0:
                return 0
            s.add_all([
                LlmProvider(
                    name="openrouter", adapter="openai_compatible",
                    model=settings.mcq_agent_model, base_url=settings.openrouter_base_url,
                    api_key_enc=encrypt(settings.openrouter_api_key or ""),
                    default_headers={"X-Title": settings.openrouter_site_name},
                    extra_body={}, active=True),
                LlmProvider(
                    name="proxy", adapter="openai_compatible",
                    model="gpt-4o", base_url=settings.proxy_base_url or "",
                    api_key_enc=encrypt(settings.proxy_api_key or settings.openai_api_key or ""),
                    default_headers={},
                    extra_body={"metadata": {
                        "project_name": "OBJECTIVE_CONTENT_MCQ", "feature": "MCQ_GENERATION",
                        "step": "generate", "team": "CONTENT",
                        "meta": {"course": "OBJECTIVE_CONTENT"}, "unit": ""}},
                    active=False),
            ])
            s.commit()
        refresh()
        return 2
    except Exception:  # noqa: BLE001 — seeding is best-effort
        return 0


# --- plain-text completion + tolerant JSON parsing (the agent nodes use these) - #
def _model(temperature: float = 0.2):
    return make_chat_model(temperature=temperature)


def chat(messages: list[dict], *, temperature: float = 0.2) -> str:
    """Run a chat completion and return the assistant text. `messages` is a list of
    ``{"role": ..., "content": ...}`` dicts (LangChain accepts this form directly)."""
    resp = _model(temperature).invoke(messages)
    return getattr(resp, "content", None) or str(resp)


def parse_json(text):
    """Best-effort JSON extraction from a chat reply (strips code fences / prose)."""
    t = (text or "").strip()
    t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except Exception:
        pass
    for open_c, close_c in (("[", "]"), ("{", "}")):
        i, j = t.find(open_c), t.rfind(close_c)
        if i != -1 and j != -1 and j > i:
            try:
                return json.loads(t[i:j + 1])
            except Exception:
                continue
    return None
