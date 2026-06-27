"""Question pipeline · Node 8 — generate_questions (package).

One LLM agent per question type writes LEAN, question-specific content grounded in LO
intent + RAG truth, then deterministic creation-layer guards make review rarely
intervene — on the principle **the agent proposes, the code enforces**. Every guideline
block is a DB-overridable `gen.*` prompt fetched at call time, so editing one updates
BOTH generation here and the review pass (Node 9), which re-applies the same blocks.

FLOW (per LO)
    recommend_question_type → generate_lean
        │  _generate_lean → route to the type-agent (SQL_FIB → CODE_ANALYSIS fallback;
        │                   uncovered syntax/concept → MCQ fallback)
        │  _enforce_grounding         stem/option named terms are taught (in-place fix loop)
        │  _enforce_code_grounding    emitted code uses only taught constructs (else MCQ)
        │  _enforce_code_visibility   snippet lives in the `code` field, not a fenced stem
        │  _verify_fib / _verify_code_output   execution-grade FIB / correct the output key
        │  _enforce_options           count · exactly-one/2-3 correct · dedupe · no shared lead-in
        ▼  lean question → review_and_fix (Node 9)

Submodules:
    prompts.py   — the DB-overridable gen.* guideline blocks + type-set constants + difficulty_of.
    agents.py    — one LLM agent per type, _sys_for composition, _AGENTS / _SCHEMA_BY_TYPE dispatch.
    grounding.py — course-domain detection, grounding context, RAG coverage, the grounding gate + MCQ fallback.
    verify.py    — execution verification (FIB run-grading, code-output correction).
    enforce.py   — deterministic option / code-visibility / typography guards.
    node.py      — generate_lean / _generate_lean / fix_lean / generate_for_los entrypoints.
"""
from __future__ import annotations

from app.mcq_pipeline.nodes.m08_generate_questions.node import (
    fix_lean, generate_for_los, generate_lean,
)
from app.mcq_pipeline.nodes.m08_generate_questions.prompts import (
    CODE_PATH_TYPES, OPTION_TYPES, difficulty_of, _TYPED_ANSWER_TYPES,
    _CODE_RULES, _EXACT_ANSWER_RULES, _EXPLANATION_RULES, _FIB_RULES, _GROUNDING_RULES,
    _MORE_THAN_ONE_RULES, _OPTION_RULES, _QUESTION_TEXT_RULES, _REARRANGE_RULES,
    _SQL_RULES, _TRUE_FALSE_RULES,
)
from app.mcq_pipeline.nodes.m08_generate_questions.grounding import (
    _course_is_sql, _ground, _lo_block,
)

__all__ = [
    "generate_for_los", "generate_lean", "fix_lean", "difficulty_of",
    "_ground", "_course_is_sql", "_lo_block", "CODE_PATH_TYPES", "OPTION_TYPES",
    "_TYPED_ANSWER_TYPES", "_CODE_RULES", "_EXACT_ANSWER_RULES", "_EXPLANATION_RULES",
    "_FIB_RULES", "_GROUNDING_RULES", "_MORE_THAN_ONE_RULES", "_OPTION_RULES",
    "_QUESTION_TEXT_RULES", "_REARRANGE_RULES", "_SQL_RULES", "_TRUE_FALSE_RULES",
]
