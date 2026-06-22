"""
app/services/chunking.py
------------------------
Section-based Markdown chunking (ported from the Workflow POC ``ingest.py``).

  • Split on level-2/3 headings (## / ###) — each chunk is one concept-section.
  • Level-1 (#) is the document title — not a split point.
  • Level-4+ (#### Code / Input / Output) stay inside their parent section, so a
    worked example (code + input + output) is never split apart.
  • Headings inside fenced ``` code blocks are ignored.
  • Tiny sections are merged forward to MIN_CHARS.

The course/unit identity comes from the DB rows, so the POC's frontmatter /
filename-seq / CSV concerns are dropped. Public entrypoint: ``chunk_markdown``.
"""

from __future__ import annotations

import re

_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_BLANKS_RE = re.compile(r"\n{3,}")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_FENCE_RE = re.compile(r"^\s*```")

MIN_CHARS = 200


def _clean(markdown: str) -> str:
    """Drop image embeds and collapse runs of blank lines."""
    return _BLANKS_RE.sub("\n\n", _IMAGE_RE.sub("", markdown)).strip()


def _split_sections(text: str) -> list[tuple[str | None, str]]:
    """Return [(section_heading | None, section_text), …]."""
    doc_title_seen = False
    sections: list[tuple[str | None, str]] = []
    cur_heading: str | None = None
    cur_body: list[str] = []
    in_fence = False

    def flush() -> None:
        body = "\n".join(cur_body).strip()
        if cur_heading is not None or body:
            sections.append((cur_heading, body))
        cur_body.clear()

    for line in text.split("\n"):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            cur_body.append(line)
            continue
        if not in_fence:
            m = _HEADING_RE.match(line)
            if m:
                level, htext = len(m.group(1)), m.group(2).strip()
                if level == 1:
                    if not doc_title_seen:
                        doc_title_seen = True  # document title; not a split point
                    else:
                        flush()
                        cur_heading = htext  # rare 2nd H1 -> new section
                    continue
                if level in (2, 3):
                    flush()
                    cur_heading = htext
                    continue
                # level >= 4 falls through and stays inside the current section
        cur_body.append(line)

    flush()
    return sections


def _merge_small(sections: list[tuple[str | None, str]]) -> list[tuple[str, str]]:
    """Merge consecutive sections until each chunk reaches MIN_CHARS."""
    chunks: list[tuple[str, str]] = []
    buf_label: str | None = None
    buf_parts: list[str] = []
    buf_len = 0

    def piece(heading: str | None, body: str) -> str:
        head = f"### {heading}\n" if heading else ""
        return f"{head}{body}".strip()

    def flush() -> None:
        nonlocal buf_label, buf_parts, buf_len
        if buf_parts:
            chunks.append((buf_label or "intro", "\n\n".join(buf_parts).strip()))
        buf_label, buf_parts, buf_len = None, [], 0

    for heading, body in sections:
        p = piece(heading, body)
        if not p:
            continue
        if buf_label is None:
            buf_label = heading or "intro"
        buf_parts.append(p)
        buf_len += len(p)
        if buf_len >= MIN_CHARS:
            flush()
    flush()
    return chunks


def chunk_markdown(text: str) -> list[dict]:
    """Split reading-material Markdown into section chunks.

    Returns dicts: {section, position, text, has_code, char_len}.
    """
    cleaned = _clean(text or "")
    if not cleaned:
        return []
    merged = _merge_small(_split_sections(cleaned))
    return [
        {
            "section": section,
            "position": idx,
            "text": body,
            "has_code": "```" in body,
            "char_len": len(body),
        }
        for idx, (section, body) in enumerate(merged)
    ]
