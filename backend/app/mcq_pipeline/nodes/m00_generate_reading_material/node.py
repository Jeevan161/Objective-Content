"""Classroom Quiz · Node 0 — generate_reading_material.

Turns a quiz scope's raw slide copy into a clean Markdown student handout (the
"reading material") that the rest of the pipeline generates LOs + questions from.

This runs at the RUNNER level (not as a graph node): the run-scoped RagAdapter is
built FROM this output and grounds every downstream node on it, so the handout must
exist before the graph starts. One LLM call; DB-overridable prompt (`cq.reading_material`).
"""
from __future__ import annotations

from app.core.config import settings
from app.mcq_pipeline.prompts.store import get_prompt
from app.mcq_pipeline.nodes.m00_generate_reading_material.prompts import _SYS


def _model():
    # Reading-material authoring is a generation task — use the generation model
    # (settings.mcq_generation_model when on OpenRouter; otherwise the connector's own).
    from app.mcq_pipeline.utils.llm import make_chat_model
    return make_chat_model(temperature=0.2, model=settings.mcq_generation_model or None)


def generate_reading_material(slide_text: str, *, title: str = "") -> str:
    """Author the session handout for ONE quiz scope from its slide copy. Returns the
    Markdown handout (empty string if there is no slide content to work from)."""
    slide_text = (slide_text or "").strip()
    if not slide_text:
        return ""
    header = f"Session title: {title}\n\n" if title.strip() else ""
    user = (header
            + "Raw on-slide copy for this segment (in teaching order, slide-delimited):\n\n"
            + slide_text)
    resp = _model().invoke([
        {"role": "system", "content": get_prompt("cq.reading_material", _SYS)},
        {"role": "user", "content": user},
    ])
    return (getattr(resp, "content", "") or "").strip()
