"""Question pipeline · Node 7 — recommend_question_type (package).

Given a Learning Outcome, pick the IDEAL platform question type to test it. One LLM
call per LO selects the format; deterministic guards remap disabled (exact-string-match)
types, keep setup/CLI and scenario outcomes in the MCQ family, and route SQL
write-a-query outcomes to SQL_FIB_CODING. Runs BEFORE the gate so plan-time types are
preserved (only untyped LOs are typed here).

Submodules:
    prompts.py — the DB-overridable `qtype.sys` selection prompt.
    node.py    — recommend_one / recommend_for_los + the deterministic fallback & guards.
"""
from __future__ import annotations

from app.mcq_pipeline.nodes.m07_recommend_question_type.node import (
    CODE_PATH_TYPES, QUESTION_TYPES, recommend_for_los, recommend_one,
)

__all__ = ["recommend_one", "recommend_for_los", "QUESTION_TYPES", "CODE_PATH_TYPES"]
