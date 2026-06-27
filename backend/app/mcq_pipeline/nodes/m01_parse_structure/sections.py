"""parse_structure · deterministic line-split + lossless heading fallback.

``sections_from_breaks`` partitions the ORIGINAL line array at the break indices into sections —
content before the first break becomes an 'Introduction' (preamble is never dropped); the spans
cover ``[0, len)`` contiguously and without overlap, so no line (and no token) can be lost.

``regex_sections`` is the markdown-heading fallback used when the LLM segmenter is unavailable or
a guard fails — the original parse_structure logic, itself lossless.

Both anchor on line positions, never on sentence text, so a sentence repeated in the material can
never create an ambiguous cut.
"""
from __future__ import annotations

import re

from app.mcq_pipeline.utils.concept_graph import slugify

# recap/summary sections are NOT dropped — that was an English-only heading heuristic that risked
# deleting substantive material in other domains. A genuine recap simply yields no NEW concepts in
# extract_concepts (K-sample majority + canonicalize dedup absorb it).
_TOPIC_HEADING = re.compile(r"^(#{1,3})\s+(.*\S)\s*$")


def _section_dict(title: str, body_lines: list[str], order: int) -> dict | None:
    body = "\n".join(body_lines).strip()
    if not body:
        return None
    return {"topic_id": f"T{order + 1}_{slugify(title)}"[:48],
            "title": title, "order": order, "text": body, "has_code": "```" in body}


def sections_from_breaks(lines: list[str], breaks: list[dict]) -> list[dict]:
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


def regex_sections(text: str) -> list[dict]:
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
