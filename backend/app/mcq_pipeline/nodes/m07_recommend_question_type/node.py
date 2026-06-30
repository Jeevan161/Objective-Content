"""Question pipeline · Node 7 — recommend_question_type · node.

Given a Learning Outcome, pick the IDEAL platform question type to test it. One LLM
call per LO (DB-overridable prompt) with a deterministic fallback + guards: excluded
(exact-string-match) types are remapped, setup/CLI concepts stay conceptual, scenario
LOs stay MCQ-family, and a SQL "write a query" outcome routes to SQL_FIB_CODING.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.mcq_pipeline.utils import llm as config, rag_api, scope
from app.mcq_pipeline.utils.concurrency import pmap
from app.mcq_pipeline.utils.concept_graph import is_setup_or_cli
from app.mcq_pipeline.config import EXCLUDED_QUESTION_TYPES
from app.mcq_pipeline.prompts.store import get_prompt
from app.mcq_pipeline.nodes.m08_generate_questions import _course_is_sql
from app.mcq_pipeline.nodes.m07_recommend_question_type.prompts import _SYS

CODE_PATH_TYPES = {
    "FIB_CODING", "SQL_FIB_CODING", "CODE_ANALYSIS_MULTIPLE_CHOICE",
    "CODE_ANALYSIS_MORE_THAN_ONE_MULTIPLE_CHOICE", "CODE_ANALYSIS_TEXTUAL",
}

QUESTION_TYPES = (
    "MULTIPLE_CHOICE", "TRUE_OR_FALSE", "MORE_THAN_ONE_MULTIPLE_CHOICE", "TEXTUAL",
    "CODE_ANALYSIS_MULTIPLE_CHOICE", "CODE_ANALYSIS_MORE_THAN_ONE_MULTIPLE_CHOICE",
    "CODE_ANALYSIS_TEXTUAL", "FIB_CODING", "SQL_FIB_CODING", "REARRANGE",
)


class QuestionTypeChoice(BaseModel):
    """The recommendation for ONE Learning Outcome."""
    question_type: Literal[
        "MULTIPLE_CHOICE", "TRUE_OR_FALSE", "MORE_THAN_ONE_MULTIPLE_CHOICE", "TEXTUAL",
        "CODE_ANALYSIS_MULTIPLE_CHOICE", "CODE_ANALYSIS_MORE_THAN_ONE_MULTIPLE_CHOICE",
        "CODE_ANALYSIS_TEXTUAL", "FIB_CODING", "SQL_FIB_CODING", "REARRANGE",
    ]
    rationale: str = Field(description="why this type fits + what exactly to assess (how to test)")


def _model():
    # Build from the active LlmProvider (legacy OpenRouter fallback when none configured).
    from app.mcq_pipeline.utils.llm import make_chat_model
    return make_chat_model(temperature=0)


def _lo_text(lo: dict) -> str:
    return " ".join(str(lo.get(k) or "") for k in
                    ("syntax", "concept", "sub_concept", "description", "outcome"))


def _excluded_remap(qt: str) -> str:
    """Safe replacement for a disabled (exact-string-match) type. A code-output type
    stays in the CODE family (so the snippet is preserved); a plain typed answer
    becomes a conceptual MCQ."""
    return "CODE_ANALYSIS_MULTIPLE_CHOICE" if qt == "CODE_ANALYSIS_TEXTUAL" else "MULTIPLE_CHOICE"


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
    return t if t not in EXCLUDED_QUESTION_TYPES else _excluded_remap(t)


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

    # Excluded types (config) are remapped to a safe OPTION type regardless of what the LLM picked.
    # TEXTUAL and CODE_ANALYSIS_TEXTUAL are OFF (exact string-match grading, no AI grader — a space
    # or typo fails a correct answer). Code-output stays in the CODE family so the snippet survives.
    if out["question_type"] in EXCLUDED_QUESTION_TYPES:
        repl = _excluded_remap(out["question_type"])
        out["question_type_rationale"] = (
            f"[excluded:{out['question_type']}] exact-match type disabled -> {repl}. "
            + out.get("question_type_rationale", ""))
        out["question_type"] = repl

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
            and out["question_type"] in {"FIB_CODING", "SQL_FIB_CODING", "TEXTUAL",
                                         "CODE_ANALYSIS_TEXTUAL", "REARRANGE"}:
        out["question_type"] = "MULTIPLE_CHOICE"
        out["question_type_rationale"] = (
            "[scenario] situation-based reasoning -> MULTIPLE_CHOICE. "
            + out.get("question_type_rationale", ""))

    # SQL has its OWN fill-in-code type. FIB_CODING is for programming languages (Python / Java /
    # JavaScript); a SQL "write / complete a query" outcome uses SQL_FIB_CODING instead.
    if out["question_type"] == "FIB_CODING" and _course_is_sql():
        out["question_type"] = "SQL_FIB_CODING"
        out["question_type_rationale"] = (
            "[sql] SQL write/complete-query -> SQL_FIB_CODING (the SQL fill-in-code type; "
            "FIB_CODING is for Python/Java/JS). "
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
