"""
app/mcq_pipeline/lo_llm.py
--------------------------
LLM helpers for the LO pipeline's agent nodes. `chat()` mirrors the POC's
`chat(messages, temperature)` contract (a plain text completion over role/content
messages) on top of the same `ChatOpenAI` the question agents use, and
`parse_json()` is the POC's tolerant JSON extractor for agent replies.

Kept separate from the deterministic core so the pure nodes never import an LLM
client (and stay trivially unit-testable).
"""

from __future__ import annotations

import json
import re

from . import config


def _model(temperature: float = 0.2):
    # Build from the active LlmProvider (legacy OpenRouter fallback when none configured).
    from .llm_factory import make_chat_model
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
