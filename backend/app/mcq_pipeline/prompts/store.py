"""
app/mcq_pipeline/prompt_store.py
--------------------------------
DB-backed, editable prompts. Vendored modules register their literal as the
DEFAULT at import time and read the live value via :func:`get_prompt` at call
time, so editing the active `mcq_prompts` row changes behaviour without a
redeploy. The defaults double as the seed inserted by migration `0003`.

    _GROUNDING = register("gen.grounding_rules", "GROUNDING RULES:\n...")
    ...
    parts.append(get_prompt("gen.grounding_rules"))   # DB-active or default
"""

from __future__ import annotations

import threading

from sqlalchemy import select

# Code defaults, populated as modules import (register). Used as fallback AND as
# the migration seed (see all_defaults()).
_defaults: dict[str, str] = {}
_descriptions: dict[str, str] = {}

# Active-version cache {key: content}; loaded lazily, refreshable.
_cache: dict[str, str] | None = None
_lock = threading.Lock()


def register(key: str, text: str, *, description: str = "") -> str:
    """Register a prompt's code default and return the text unchanged (so the
    module's constant still holds the literal)."""
    _defaults[key] = text
    if description:
        _descriptions[key] = description
    return text


def _load_cache() -> None:
    global _cache
    # Imported lazily to avoid a circular import at module load.
    from app.db.session import SessionLocal
    from app.models import McqPrompt

    try:
        with SessionLocal() as session:
            rows = session.scalars(select(McqPrompt).where(McqPrompt.active.is_(True))).all()
            _cache = {r.key: r.content for r in rows}
    except Exception:  # noqa: BLE001 — DB unavailable → fall back to code defaults
        _cache = {}


def get_prompt(key: str, default: str | None = None) -> str:
    """Return the active DB content for `key`, else the registered/code default."""
    if default is not None:
        _defaults.setdefault(key, default)
    global _cache
    if _cache is None:
        with _lock:
            if _cache is None:
                _load_cache()
    if _cache and key in _cache:
        return _cache[key]
    return _defaults.get(key, default if default is not None else "")


def refresh() -> None:
    """Drop the cache so the next get_prompt reloads active versions from the DB."""
    global _cache
    _cache = None


def all_defaults() -> list[dict]:
    """Every registered default. Importing the vendored modules first populates this."""
    return [
        {"key": k, "content": v, "description": _descriptions.get(k, "")}
        for k, v in _defaults.items()
    ]


def is_registered(key: str) -> bool:
    """True if `key` has a registered code default (i.e. is a known prompt)."""
    return key in _defaults


def default_for(key: str) -> str | None:
    return _defaults.get(key)


# --------------------------------------------------------------------------- #
# Editable-prompt CRUD (powers the admin UI). Each edit creates a NEW active
# version and deactivates the previous ones, so history is preserved and the
# pipeline always reads the single active row (falling back to the code default).
# --------------------------------------------------------------------------- #
# Keys under this prefix are read-only REFERENCE documentation for deterministic
# pipeline stages (no LLM prompt drives them) — shown in the admin UI but not editable.
INFORMATIONAL_PREFIX = "lo.rules."


def is_informational(key: str) -> bool:
    return (key or "").startswith(INFORMATIONAL_PREFIX)


def _row_dict(row, *, default: str) -> dict:
    return {
        "key": row.key,
        "content": row.content,
        "default": default,
        "description": row.description or _descriptions.get(row.key, ""),
        "version": row.version,
        "active": bool(row.active),
        "overridden": (row.content or "") != (default or ""),
        "informational": is_informational(row.key),
        "updated_at": row.updated_at,
    }


def list_prompts() -> list[dict]:
    """Every prompt's current state: active DB content (or code default if none),
    the code default for diffing, version, and whether it's been overridden.
    Ordered by registration order, with any DB-only keys appended."""
    from app.db.session import SessionLocal
    from app.models import McqPrompt

    rows: dict = {}
    try:
        with SessionLocal() as session:
            for r in session.scalars(select(McqPrompt).where(McqPrompt.active.is_(True))).all():
                rows[r.key] = r
    except Exception:  # noqa: BLE001 — DB unavailable → show code defaults only
        rows = {}

    keys = list(_defaults.keys()) + [k for k in rows if k not in _defaults]
    out: list[dict] = []
    for key in keys:
        default = _defaults.get(key, "")
        row = rows.get(key)
        if row is not None:
            out.append(_row_dict(row, default=default))
        else:
            out.append({
                "key": key, "content": default, "default": default,
                "description": _descriptions.get(key, ""), "version": 0,
                "active": False, "overridden": False,
                "informational": is_informational(key), "updated_at": None,
            })
    return out


def set_prompt(key: str, content: str, *, description: str | None = None) -> dict:
    """Save `content` as a new active version of `key` (deactivating prior ones),
    then refresh the cache so the pipeline picks it up immediately."""
    from app.db.session import SessionLocal
    from app.models import McqPrompt

    with SessionLocal() as session:
        existing = session.scalars(select(McqPrompt).where(McqPrompt.key == key)).all()
        next_version = max((r.version for r in existing), default=0) + 1
        for r in existing:
            r.active = False
        row = McqPrompt(
            key=key, content=content, version=next_version, active=True,
            description=(description if description is not None
                         else (next((r.description for r in existing if r.description), None)
                               or _descriptions.get(key, ""))),
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        result = _row_dict(row, default=_defaults.get(key, ""))
    refresh()
    return result


def reset_prompt(key: str) -> dict:
    """Reset a prompt back to its code default (as a new active version)."""
    default = _defaults.get(key)
    if default is None:
        raise KeyError(key)
    return set_prompt(key, default)


def seed_prompts() -> int:
    """Idempotently insert any registered default that has no DB row yet
    (version=1, active=true). Importing the pipeline package first triggers every
    `register()` so all keys are known. Safe to call at startup; returns the count
    inserted. Best-effort — never raises (missing deps/DB → 0)."""
    try:
        import app.mcq_pipeline.graph  # noqa: F401 — import triggers all register() calls
        from sqlalchemy import select as _select

        from app.db.session import SessionLocal
        from app.models import McqPrompt

        with SessionLocal() as session:
            existing = set(session.scalars(_select(McqPrompt.key)).all())
            added = 0
            for d in all_defaults():
                if d["key"] in existing:
                    continue
                session.add(McqPrompt(key=d["key"], content=d["content"], version=1,
                                      active=True, description=d.get("description", "")))
                added += 1
            if added:
                session.commit()
            refresh()
            return added
    except Exception:  # noqa: BLE001 — seeding is best-effort
        return 0
