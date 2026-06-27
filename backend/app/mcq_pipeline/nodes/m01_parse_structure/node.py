"""parse_structure · the LangGraph node (orchestrator).

Wires the submodules into the flow documented in the package ``__init__``: segment → (gated)
recheck loop → deterministic line-split → token-conservation guard → heading fallback. This module
owns only the orchestration and the conservation check; the segmentation, recheck, and splitting
logic each live in their own module.
"""
from __future__ import annotations

import re
from collections import Counter

from app.mcq_pipeline.utils._common import _prog
from app.mcq_pipeline.nodes.m01_parse_structure.critique import (
    MAX_REVISIONS,
    critique_breaks,
    looks_suspicious,
)
from app.mcq_pipeline.nodes.m01_parse_structure.sections import regex_sections, sections_from_breaks  # noqa: E501
from app.mcq_pipeline.nodes.m01_parse_structure.segment import llm_line_breaks


def _tokens(s: str) -> list[str]:
    return re.findall(r"\S+", s)


def parse_structure(state, config) -> dict:
    prog = _prog(config)
    prog.start("parse_structure")
    text = state["source_text"]
    lines = text.split("\n")

    method = "llm"
    breaks = llm_line_breaks(lines)

    # Goal-driven recheck: when the first split looks suspicious, let the reviewer re-point the
    # boundaries (bounded loop). Each revision must pass the same guards or it's discarded; the
    # reviewer can only move cuts to real line numbers, so the result stays grounded & lossless.
    revisions = 0
    if breaks and looks_suspicious(breaks, len(lines)):
        for _ in range(MAX_REVISIONS):
            revised = critique_breaks(lines, breaks)
            if not revised:                         # reviewer satisfied / down / bad revision
                break
            breaks, revisions = revised, revisions + 1
            method = "llm+critique"
            if not looks_suspicious(breaks, len(lines)):
                break

    sections = sections_from_breaks(lines, breaks) if breaks else []

    # Conservation: the union of section text must preserve every non-whitespace token of the
    # source. A line-partition is provably lossless; this guards against a bug or a bad split,
    # and falls back to the (also-lossless) heading split if anything is off.
    conserved = bool(sections) and \
        Counter(_tokens("\n".join(s["text"] for s in sections))) == Counter(_tokens(text))
    if not conserved:
        sections = regex_sections(text)
        method = "regex-fallback"

    if not sections:
        raise RuntimeError("ESCALATE: structure could not be recovered from source.")
    logs = [{"node": "parse_structure", "method": method, "topics": len(sections),
             "revisions": revisions}]
    snapshot = {"method": method, "topic_count": len(sections), "revisions": revisions,
                "topics": [{"topic_id": s["topic_id"], "title": s["title"],
                            "chars": len(s["text"]), "has_code": s["has_code"]} for s in sections]}
    prog.done("parse_structure", detail=f"{len(sections)} topics ({method})", snapshot=snapshot)
    return {"sections": sections, "log": logs}
