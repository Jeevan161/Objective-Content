"""LO pipeline · Node 1 — parse_structure (package).

Splits the raw reading material into ordered TOPIC sections — the teaching units the rest of the
pipeline is organized around (per-topic concept extraction, budget allocation, authoring). The
topic boundaries are decided SEMANTICALLY by an LLM, then enforced LOSSLESSLY by deterministic
code, with a goal-driven recheck in between and a heading-split fallback underneath.

FLOW
    state["source_text"]
        │  split into lines
        ▼
    segment.llm_line_breaks ──► [(title, start_line), …] breaks      [LLM · lo.segment_sys]
        │                                                                │ none (LLM down /
        │                                                                │ reply fails a guard)
        ▼                                                                │
    critique.looks_suspicious?  ── no ──┐                                │
        │ yes (1 giant topic, or                                        │
        │      ~a topic every few lines)                                │
        ▼                                                                │
    critique.critique_breaks  (× MAX_REVISIONS)        [LLM · lo.segment_critique_sys]
        │  corrected breaks, re-held to the SAME guards (else kept as-is)│
        ├──────────────────────────────┘                                │
        ▼                                                                │
    sections.sections_from_breaks ──► sections      deterministic, lossless line-split
        │                                                                │
        ▼                                                                │
    token-conservation guard ── fails ──► sections.regex_sections ◄──────┘   heading fallback
        │                                                                     (also lossless)
        ▼
    {"sections": […], "log": […]}     method ∈ {llm, llm+critique, regex-fallback}

Anchoring cuts on line NUMBERS (not sentence text) means a sentence repeated in the material can
never create an ambiguous cut; the conservation guard then asserts no token was dropped/invented.
A run never blocks: if the LLM is unavailable or any guard fails, the heading split takes over.

Input:  state["source_text"]  — the raw reading material.
Output: sections = [{topic_id, title, order, text, has_code}]  +  a log noting the method used
        and the topic count (and how many reviewer revisions were applied).
Downstream: plan_los runs per section; topic_id threads through allocation, authoring,
        the dependency DAG, sequencing, and repair.

Submodules:
    prompts.py   — the two DB-backed LLM prompts (segmenter + reviewer) and the loop bound.
    segment.py   — initial LLM segmentation → validated (title, start_line) breaks.
    critique.py  — goal-driven recheck: cheap gate + reviewer (bounded reflexion loop).
    sections.py  — deterministic line-split + lossless markdown-heading fallback.
    node.py      — the LangGraph node wiring the above together.
"""
from __future__ import annotations

from app.mcq_pipeline.nodes.m01_parse_structure.node import parse_structure

__all__ = ["parse_structure"]
