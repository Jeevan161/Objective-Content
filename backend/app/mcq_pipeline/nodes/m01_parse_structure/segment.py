"""parse_structure · initial segmentation (LLM segmenter + guards).

Numbers every source line, asks the segmenter LLM (``lo.segment_sys``, temp 0) for each topic's
(title, start_line), and validates the reply into strictly-increasing, in-range breaks. Returns
``None`` on any failure so the orchestrator can fall back to the deterministic heading split.

``number_lines`` and ``validate_breaks`` are shared with :mod:`critique` so the segmenter and the
reviewer speak the same line-numbered language and are held to the same invariants.
"""
from __future__ import annotations

from app.mcq_pipeline.prompts.store import get_prompt
from app.mcq_pipeline.utils.llm import chat, parse_json

from app.mcq_pipeline.nodes.m01_parse_structure.prompts import SEGMENT_SYS


def number_lines(lines: list[str]) -> str:
    return "\n".join(f"{i + 1}: {ln}" for i, ln in enumerate(lines))


def validate_breaks(data, n: int) -> list[dict] | None:
    """Validate a raw LLM topic list into strictly-increasing, in-range breaks. Returns the
    normalized ``[{start_line, title}]`` list, or ``None`` if the shape or any guard fails. Shared
    by the initial segmenter and the reviewer so a revised list is held to the SAME invariants."""
    if not isinstance(data, list) or not data:
        return None
    breaks, last = [], 0
    for item in data:
        if not isinstance(item, dict):
            return None
        try:
            sl = int(item.get("start_line"))
        except (TypeError, ValueError):
            return None
        if sl < 1 or sl > n or sl <= last:          # in-range AND strictly increasing (monotonic)
            return None
        title = (item.get("title") or "").strip()
        breaks.append({"start_line": sl, "title": title or f"Topic {len(breaks) + 1}"})
        last = sl
    return breaks


def llm_line_breaks(lines: list[str]) -> list[dict] | None:
    """Ask the segmenter for topic boundaries as (title, start_line). Returns a validated,
    strictly-increasing list of breaks, or ``None`` if the LLM is unavailable / the response fails
    any guard — in which case the caller falls back to the deterministic heading split."""
    n = len(lines)
    if n < 2:
        return None
    try:
        data = parse_json(chat(
            [{"role": "system", "content": get_prompt("lo.segment_sys", SEGMENT_SYS)},
             {"role": "user", "content": number_lines(lines)}],
            temperature=0))
    except Exception:  # noqa: BLE001 — LLM down: signal fallback, never fail the node
        return None
    return validate_breaks(data, n)
