"""
app/mcq_pipeline/lo_rules.py
----------------------------
Read-only REFERENCE documentation for the LO pipeline's DETERMINISTIC stages
(parse_structure, canonicalize_concepts, plan_allocation, resolve_prerequisites,
validate, finalize, lo_to_legacy/bridge). These stages have no LLM prompt — they
are pure code — but the admin portal should still surface the exact rules they
enforce. The text lives in `lo_rules.json` (faithfully derived from the code and
adversarially fidelity-checked) and is registered here under `lo.rules.*` keys so
the prompt store / pipeline catalog can show it.

These keys are flagged `informational` by `prompt_store` and are read-only: editing
them cannot change behavior (the code is authoritative), so the API/UI block edits.
"""

from __future__ import annotations

import json
from pathlib import Path

from .prompt_store import register

_RULES_PATH = Path(__file__).with_name("lo_rules.json")

_DESCRIPTION = ("Read-only reference — the hard-coded rules of this deterministic "
                "stage (no LLM prompt drives it; editing has no effect).")


def _load() -> None:
    try:
        data = json.loads(_RULES_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — missing/invalid file must not break imports
        return
    for key, entry in data.items():
        body = (entry or {}).get("body") or ""
        if body.strip():
            register(key, body, description=_DESCRIPTION)


_load()
