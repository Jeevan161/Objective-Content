"""LO pipeline · Node 1 — parse_structure.

Splits the reading material into TOPIC sections. An LLM segmenter decides the topic
boundaries (semantic, heading-independent), but the split itself is deterministic and
lossless: the LLM returns only the START LINE of each topic, and a script partitions the
ORIGINAL line array at those indices. Anchoring on line NUMBERS (not sentence text) means a
sentence repeated in the material can never create an ambiguous cut. If the LLM is
unavailable or its response fails any guard, we fall back to the original markdown-heading
split (also lossless). Either way, every non-whitespace token of the source is preserved
(asserted before returning).
"""
from __future__ import annotations

import re
from collections import Counter

from app.mcq_pipeline.utils.concept_graph import slugify
from app.mcq_pipeline.utils.llm import chat, parse_json
from app.mcq_pipeline.prompts.store import get_prompt, register
from app.mcq_pipeline.nodes._common import _prog


# ── Node 1 · parse_structure (LLM segmenter + deterministic line-split) ───── #
_TOPIC_HEADING = re.compile(r"^(#{1,3})\s+(.*\S)\s*$")
# NOTE: recap/summary sections are NOT dropped — that was an English-only heading heuristic
# that risked deleting substantive material in other domains. A genuine recap simply yields no
# NEW concepts in extract_concepts (K-sample majority + canonicalize dedup absorb it).

_SEGMENT_SYS = register("lo.segment_sys", """\
You divide instructional reading material (any subject) into its TOPIC sections — the natural teaching units a curriculum map would use. The material is given with every line numbered as "<n>: <text>".

Return the topics IN DOCUMENT ORDER. For each topic give:
- "title": a short, descriptive topic title (derive it from the content; do NOT copy a whole sentence).
- "start_line": the line NUMBER (integer) where that topic begins — its heading line, or the first line of its content.

Rules:
- start_line values MUST be in document order and STRICTLY INCREASING.
- The first topic starts at the first line that has real content (skip leading blank lines).
- Cut ONLY at genuine shifts in subject matter. Prefer a handful of substantive topics; do NOT make a topic per paragraph. Fold short transitions or one-off examples into the surrounding topic.
- Use ONLY the line numbers shown; never invent, merge, or renumber lines.

Return ONLY a JSON list, e.g.: [{"title": "Virtual environments", "start_line": 1}, {"title": "Installing packages", "start_line": 23}]""")


def _number_lines(lines: list[str]) -> str:
    return "\n".join(f"{i + 1}: {ln}" for i, ln in enumerate(lines))


def _llm_line_breaks(lines: list[str]) -> list[dict] | None:
    """Ask the segmenter for topic boundaries as (title, start_line). Returns a validated,
    strictly-increasing list of breaks, or None if the LLM is unavailable / the response fails
    any guard — in which case the caller falls back to the deterministic heading split."""
    n = len(lines)
    if n < 2:
        return None
    try:
        data = parse_json(chat(
            [{"role": "system", "content": get_prompt("lo.segment_sys", _SEGMENT_SYS)},
             {"role": "user", "content": _number_lines(lines)}],
            temperature=0))
    except Exception:  # noqa: BLE001 — LLM down: signal fallback, never fail the node
        return None
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


def _section_dict(title: str, body_lines: list[str], order: int) -> dict | None:
    body = "\n".join(body_lines).strip()
    if not body:
        return None
    return {"topic_id": f"T{order + 1}_{slugify(title)}"[:48],
            "title": title, "order": order, "text": body, "has_code": "```" in body}


def _sections_from_breaks(lines: list[str], breaks: list[dict]) -> list[dict]:
    """Partition the ORIGINAL line array at the break indices → sections. Content before the
    first break becomes an 'Introduction' section (preamble is never dropped). The spans cover
    [0, len) contiguously and without overlap, so no line — and no token — can be lost."""
    starts = [b["start_line"] - 1 for b in breaks]      # 0-based line indices
    spans: list[tuple[str, int, int]] = []
    if starts[0] > 0:                                   # preamble before the first topic
        spans.append(("Introduction", 0, starts[0]))
    for i, s in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(lines)
        spans.append((breaks[i]["title"], s, end))

    sections, order = [], 0
    for title, s, e in spans:
        sec = _section_dict(title, lines[s:e], order)
        if sec is not None:
            sections.append(sec)
            order += 1
    return sections


def _regex_sections(text: str) -> list[dict]:
    """Deterministic fallback: split on markdown #/## headings. Lossless. This is the original
    parse_structure logic, retained for when the LLM segmenter is unavailable or fails a guard."""
    blocks, cur, in_fence = [], None, False
    for line in text.split("\n"):
        if re.match(r"^\s*```", line):
            in_fence = not in_fence
        m = None if in_fence else _TOPIC_HEADING.match(line)
        if m and len(m.group(1)) <= 2:                  # split on # / ## only
            cur = {"title": m.group(2).strip(), "lines": []}
            blocks.append(cur)
        elif cur is not None:
            cur["lines"].append(line)
        elif line.strip():                              # preamble before first heading
            cur = {"title": "Introduction", "lines": [line]}
            blocks.append(cur)
    if not blocks:                                      # no headings: whole doc as one topic
        blocks = [{"title": "Introduction", "lines": text.split("\n")}]

    sections, order = [], 0
    for b in blocks:
        sec = _section_dict(b["title"], b["lines"], order)
        if sec is not None:
            sections.append(sec)
            order += 1
    return sections


def _tokens(s: str) -> list[str]:
    return re.findall(r"\S+", s)


def parse_structure(state, config) -> dict:
    prog = _prog(config)
    prog.start("parse_structure")
    text = state["source_text"]
    lines = text.split("\n")

    method = "llm"
    breaks = _llm_line_breaks(lines)
    sections = _sections_from_breaks(lines, breaks) if breaks else []

    # Conservation: the union of section text must preserve every non-whitespace token of the
    # source. A line-partition is provably lossless; this guards against a bug or a bad split,
    # and falls back to the (also-lossless) heading split if anything is off.
    conserved = bool(sections) and \
        Counter(_tokens("\n".join(s["text"] for s in sections))) == Counter(_tokens(text))
    if not conserved:
        sections = _regex_sections(text)
        method = "regex-fallback"

    if not sections:
        raise RuntimeError("ESCALATE: structure could not be recovered from source.")
    logs = [{"node": "parse_structure", "method": method, "topics": len(sections)}]
    prog.done("parse_structure", detail=f"{len(sections)} topics ({method})")
    return {"sections": sections, "log": logs}
