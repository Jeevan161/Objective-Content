"""parse_structure · goal-driven recheck (bounded reflexion).

``looks_suspicious`` is a cheap gate: only an off-looking split — a single giant topic (likely
under-segmented) or roughly a topic every few lines (likely over-segmented) — earns a reviewer
round; a reasonable handful-of-topics split skips the recheck entirely.

``critique_breaks`` is the reviewer pass: it re-reads the source against the GOAL in
``lo.segment_critique_sys`` and returns a corrected, RE-VALIDATED break list, or ``None`` when no
change is warranted/possible (reviewer says ok, LLM down, malformed reply, or the revision fails a
guard) — in which case the caller keeps the current split. The reviewer can only re-point cuts at
real line numbers, so the result stays grounded and lossless.

The loop itself (bounded by :data:`MAX_REVISIONS`) lives in :mod:`node`.
"""
from __future__ import annotations

import json

from app.mcq_pipeline.prompts.store import get_prompt
from app.mcq_pipeline.utils.llm import chat, parse_json

from app.mcq_pipeline.nodes.m01_parse_structure.prompts import CRITIQUE_SYS, MAX_REVISIONS
from app.mcq_pipeline.nodes.m01_parse_structure.segment import number_lines, validate_breaks

__all__ = ["MAX_REVISIONS", "looks_suspicious", "critique_breaks"]


def looks_suspicious(breaks: list[dict], n_lines: int) -> bool:
    """Cheap gate: only spend a reviewer round when the segmentation looks off — a single giant
    topic (likely under-segmented) or a topic roughly every few lines (likely over-segmented).
    A reasonable handful-of-topics split skips the recheck entirely."""
    t = len(breaks)
    if t <= 1:
        return True
    return n_lines >= 12 and t > max(2, n_lines // 6)


def critique_breaks(lines: list[str], breaks: list[dict]) -> list[dict] | None:
    """Reviewer pass: re-read the source against the GOAL and return a corrected, re-validated
    break list, or ``None`` when no change is warranted/possible (reviewer says ok, LLM is down,
    the response is malformed, or the revision fails a guard — caller then keeps the current
    split)."""
    payload = {
        "proposed_topics": [{"start_line": b["start_line"], "title": b["title"]} for b in breaks],
        "numbered_source": number_lines(lines),
    }
    try:
        data = parse_json(chat(
            [{"role": "system", "content": get_prompt("lo.segment_critique_sys", CRITIQUE_SYS)},
             {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            temperature=0))
    except Exception:  # noqa: BLE001 — LLM down: keep current split, never fail the node
        return None
    if not isinstance(data, dict) or data.get("ok") is True:
        return None
    return validate_breaks(data.get("topics"), len(lines))
