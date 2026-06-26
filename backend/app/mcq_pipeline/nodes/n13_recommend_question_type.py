
"""
question_type_agent.py (vendored, adapted)
-------------------------------------------
Given a Learning Outcome, pick the IDEAL platform question type to test it.
Adapted: relative imports + app config shim; system prompt via `prompt_store`
(DB-overridable); optional `on_progress` callback for live per-LO progress.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.mcq_pipeline.utils import llm as config, rag_api, scope
from app.mcq_pipeline.utils.concurrency import pmap
from app.mcq_pipeline.utils.concept_graph import is_setup_or_cli
from app.mcq_pipeline.config import EXCLUDED_QUESTION_TYPES
from app.mcq_pipeline.prompts.store import get_prompt, register
from app.mcq_pipeline.nodes.n14_generate_questions import _course_is_sql

CODE_PATH_TYPES = {
    "FIB_CODING", "CODE_ANALYSIS_MULTIPLE_CHOICE",
    "CODE_ANALYSIS_MORE_THAN_ONE_MULTIPLE_CHOICE", "CODE_ANALYSIS_TEXTUAL",
}

QUESTION_TYPES = (
    "MULTIPLE_CHOICE", "TRUE_OR_FALSE", "MORE_THAN_ONE_MULTIPLE_CHOICE", "TEXTUAL",
    "CODE_ANALYSIS_MULTIPLE_CHOICE", "CODE_ANALYSIS_MORE_THAN_ONE_MULTIPLE_CHOICE",
    "CODE_ANALYSIS_TEXTUAL", "FIB_CODING", "REARRANGE",
)


class QuestionTypeChoice(BaseModel):
    """The recommendation for ONE Learning Outcome."""
    question_type: Literal[
        "MULTIPLE_CHOICE", "TRUE_OR_FALSE", "MORE_THAN_ONE_MULTIPLE_CHOICE", "TEXTUAL",
        "CODE_ANALYSIS_MULTIPLE_CHOICE", "CODE_ANALYSIS_MORE_THAN_ONE_MULTIPLE_CHOICE",
        "CODE_ANALYSIS_TEXTUAL", "FIB_CODING", "REARRANGE",
    ]
    rationale: str = Field(description="why this type fits + what exactly to assess (how to test)")


_SYS = register("qtype.sys", """You select the single IDEAL question type to assess a Learning Outcome.

You do NOT write the question. You ONLY select the most appropriate evaluation format and explain how it tests the outcome.

Return exactly ONE type from the given list and a short rationale (1–2 sentences).

────────────────────────────────────────
PRIMARY OBJECTIVE
────────────────────────────────────────
Choose the question type that BEST measures whether the learner has achieved the outcome with minimal ambiguity and reliable grading.

The choice must prioritize:
1. WHAT is being assessed (concept / code behavior / procedure / sequence)
2. HOW reliably it can be graded
3. WHETHER multiple answers exist or a single answer is expected

────────────────────────────────────────
QUESTION TYPE DEFINITIONS (STRICT)
────────────────────────────────────────

- MULTIPLE_CHOICE:
  One best answer for conceptual understanding or factual recall.

- TRUE_OR_FALSE:
  Single binary judgment of a clearly stated claim.

- MORE_THAN_ONE_MULTIPLE_CHOICE:
  Multiple correct answers based on conceptual understanding.

- TEXTUAL:
  Short exact answer of AT MOST ONE OR TWO WORDS (a single term, value, keyword, or command name). The answer is graded by EXACT STRING MATCH — there is NO AI/grader judging equivalence — so the learner must be able to reproduce the EXACT string. Use ONLY when the one correct answer is a single unambiguous token/value the learner cannot phrase more than one way. If the expected answer would be a phrase, a sentence, a definition, an explanation, or more than two words, DO NOT use TEXTUAL — use MULTIPLE_CHOICE instead.

- CODE_ANALYSIS_MULTIPLE_CHOICE:
  Analyze code and choose correct behavior/output/interpretation.

- CODE_ANALYSIS_MORE_THAN_ONE_MULTIPLE_CHOICE:
  Multiple correct interpretations of code behavior.

- CODE_ANALYSIS_TEXTUAL:
  Exact output or exact result of code execution (strict match required).

- FIB_CODING:
  Fill a missing part of runnable code that produces output (input → output execution required).

- REARRANGE:
  Ordering task where items must be placed in a SINGLE correct sequence.
  This applies to:
  - workflows
  - pipelines
  - lifecycles
  - processes
  - step-by-step procedures
  ONLY when the order is canonical and clearly defined in the outcome.

────────────────────────────────────────
SELECTION RULES (HIERARCHY)
────────────────────────────────────────

STEP 1 — CODE DETECTION (HIGHEST PRIORITY)
- If outcome involves code behavior, output, debugging, or execution:
  → use CODE_ANALYSIS_* types only.

STEP 2 — EXECUTABLE PROGRAM LOGIC
- If learner must write code that runs (input → output):
  → use FIB_CODING

STEP 3 — ORDERED STRUCTURE DETECTION
- If outcome describes a fixed sequence of steps, stages, or flow:
  → use REARRANGE
- Do NOT convert ordered processes into MCQ.

STEP 4 — EXACT SHORT ANSWER CHECK
- If the one correct answer is a single token, command name, value, or keyword that fits in AT MOST
  TWO WORDS and can only be written ONE way:
  → TEXTUAL
- If the answer is longer than two words, or a phrase/definition/explanation, or could be worded in
  more than one acceptable way: do NOT use TEXTUAL (exact string match would fail a correct learner)
  → MULTIPLE_CHOICE

STEP 5 — DEFAULT CONCEPTUAL ASSESSMENT
- Otherwise:
  → MULTIPLE_CHOICE (preferred default)
  → TRUE_OR_FALSE only if statement is simple binary claim

────────────────────────────────────────
CRITICAL CONSTRAINTS
────────────────────────────────────────

- Choose EXACTLY ONE type per outcome.
- Never mix reasoning types.
- Never use TEXTUAL / CODE_ANALYSIS_TEXTUAL / FIB_CODING if multiple valid answers exist.
- TEXTUAL answers are graded by EXACT STRING MATCH (no AI grader). Use TEXTUAL ONLY when the expected
  answer is AT MOST TWO WORDS and has exactly one spelling/wording. For anything longer or phrasable
  more than one way → MULTIPLE_CHOICE. (CODE_ANALYSIS_TEXTUAL likewise only for a short, exact,
  deterministic output the learner reproduces character-for-character.)
- Never use FIB_CODING or TEXTUAL for installation / CLI / setup commands (pip, npm, cd, activate, export, etc.) → use MULTIPLE_CHOICE instead.
- For SQL outcomes (writing / completing / reading a query), do NOT use FIB_CODING — SQL is not execution-graded here. Use CODE_ANALYSIS_MULTIPLE_CHOICE (analyse a query / its result) or MULTIPLE_CHOICE instead.
- REARRANGE must only be used when a SINGLE canonical order exists.
- If uncertain, choose MULTIPLE_CHOICE (safe fallback).

────────────────────────────────────────
RATIONALE RULE
────────────────────────────────────────
- Explain WHY this type is optimal for testing the outcome.
- Mention what is being tested (concept, reasoning, code behavior, or sequence).
- Keep it to 1–2 sentences only.

Return ONLY valid JSON:
{"question_type": "...", "rationale": "..."}""")


def _model():
    # Build from the active LlmProvider (legacy OpenRouter fallback when none configured).
    from app.mcq_pipeline.utils.llm import make_chat_model
    return make_chat_model(temperature=0)


def _lo_text(lo: dict) -> str:
    return " ".join(str(lo.get(k) or "") for k in
                    ("syntax", "concept", "sub_concept", "description", "outcome"))


def _fallback_type(lo: dict) -> str:
    """Deterministic backup if the LLM omits a recommendation for some LO."""
    bloom = (lo.get("bloom_category") or "").lower()
    has_syntax = bool((lo.get("syntax") or "").strip())
    # Scenario LOs are situation-based reasoning — keep them in the MCQ family.
    if lo.get("is_scenario") or (lo.get("bloom_level_raw") or "").lower() == "scenario":
        return "MULTIPLE_CHOICE"
    # Setup/CLI concepts are conceptual-for-assessment — never a code/FIB target. Gated to
    # LOs that carry code/syntax, so it stays a programming-domain heuristic (won't mis-fire
    # on e.g. 'shared environment' in genetics, which has no syntax).
    if has_syntax and is_setup_or_cli(_lo_text(lo), ""):
        return "MULTIPLE_CHOICE"
    if bloom in ("apply", "implement"):
        t = "FIB_CODING" if has_syntax else "TEXTUAL"
    elif has_syntax:
        t = "CODE_ANALYSIS_MULTIPLE_CHOICE"
    else:
        t = "MULTIPLE_CHOICE"
    return t if t not in EXCLUDED_QUESTION_TYPES else "MULTIPLE_CHOICE"


def recommend_one(lo: dict, *, max_seq: int | None = None) -> dict:
    """Recommend the ideal question type for a SINGLE Learning Outcome (one LLM call)."""
    compact = {k: lo.get(k) for k in
               ("outcome", "bloom_category", "skill_type", "learner_action", "syntax", "concept", "description")}
    try:
        choice: QuestionTypeChoice = _model().with_structured_output(QuestionTypeChoice).invoke([
            {"role": "system", "content": get_prompt("qtype.sys", _SYS)},
            {"role": "user", "content": f"Recommend ONE question type for this outcome:\n\n{compact}"},
        ])
        out = {"question_type": choice.question_type, "question_type_rationale": choice.rationale}
    except Exception:  # noqa: BLE001 — fall back deterministically if the call fails
        out = {"question_type": _fallback_type(lo),
               "question_type_rationale": "fallback: inferred from bloom level and presence of syntax"}

    # Excluded types (config) are remapped to the safe default regardless of what the LLM picked.
    # TEXTUAL is OFF for now (exact string-match grading, no AI grader) — never emit it.
    if out["question_type"] in EXCLUDED_QUESTION_TYPES:
        out["question_type_rationale"] = (
            f"[excluded:{out['question_type']}] type disabled -> MULTIPLE_CHOICE. "
            + out.get("question_type_rationale", ""))
        out["question_type"] = "MULTIPLE_CHOICE"

    # Deterministic backstop — the LLM type rule can drift (reports #3/#4): a setup/CLI
    # concept is never a runnable code/FIB target. (No bloom-based REARRANGE downgrade: an
    # ordered sequence/flow is a valid REARRANGE at ANY Bloom level; bad rearranges — e.g.
    # conceptual 'steps' — are caught by the REARRANGE step-quality rules at gen + review,
    # not by blocking the type here.)
    qt = out["question_type"]
    if qt in CODE_PATH_TYPES and is_setup_or_cli(_lo_text(lo), ""):
        out["question_type"] = "MULTIPLE_CHOICE"
        out["question_type_rationale"] = (
            "[guard] setup/CLI concept is not a runnable code/FIB target -> MULTIPLE_CHOICE. "
            + out.get("question_type_rationale", ""))

    # Scenario LOs are situation-based reasoning — keep them MCQ-family (never FIB / typed / rearrange).
    if (lo.get("is_scenario") or (lo.get("bloom_level_raw") or "").lower() == "scenario") \
            and out["question_type"] in {"FIB_CODING", "TEXTUAL", "CODE_ANALYSIS_TEXTUAL", "REARRANGE"}:
        out["question_type"] = "MULTIPLE_CHOICE"
        out["question_type_rationale"] = (
            "[scenario] situation-based reasoning -> MULTIPLE_CHOICE. "
            + out.get("question_type_rationale", ""))

    # SQL outcomes must not be routed to FIB_CODING: SQL is not executable in this
    # pipeline (code_exec supports Python/Node/Java only), so a SQL FIB would always
    # fail execution-verification and fall back to MCQ anyway. Assess SQL "write /
    # complete a query" outcomes with CODE_ANALYSIS_MULTIPLE_CHOICE instead.
    if out["question_type"] == "FIB_CODING" and _course_is_sql():
        out["question_type"] = "CODE_ANALYSIS_MULTIPLE_CHOICE"
        out["question_type_rationale"] = (
            "[sql] SQL is not execution-graded here; FIB_CODING -> CODE_ANALYSIS_MULTIPLE_CHOICE. "
            + out.get("question_type_rationale", ""))

    with scope.recording() as rag_calls:
        if out["question_type"] in CODE_PATH_TYPES:
            out["code_coverage"] = rag_api.code_coverage(
                lo.get("concept") or lo.get("outcome", ""),
                syntax=lo.get("syntax") or None,
                max_seq=max_seq,
            )
    out["qtype_rag_calls"] = rag_calls
    return out


def recommend_for_los(los: list[dict], *, max_seq: int | None = None,
                      workers: int = 8, on_progress=None) -> list[dict]:
    """Attach `question_type` (+ `code_coverage` for code types) to each LO — one
    LLM call per outcome, run concurrently. `on_progress()` fires per finished LO."""
    def _one(lo: dict) -> dict:
        lo.update(recommend_one(lo, max_seq=max_seq))
        if on_progress:
            on_progress()
        return lo
    return pmap(_one, los, workers=workers)
