
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

from . import config, rag_api, scope
from .concurrency import pmap
from .lo_concept_graph import is_setup_or_cli
from .prompt_store import get_prompt, register

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


_SYS = register("qtype.sys", """You select the single IDEAL question type to test each Learning Outcome.
You do NOT write the question — only choose the type and explain how to test it.

Pick from these 9 platform types:
- MULTIPLE_CHOICE: one best answer about a concept. (remember/understand, conceptual)
- TRUE_OR_FALSE: a single statement judged true/false. (simple remember)
- MORE_THAN_ONE_MULTIPLE_CHOICE: select ALL correct options. (understand; several correct facts)
- TEXTUAL: learner types a short exact answer — a term, value, or a command. (remember; apply for commands)
- CODE_ANALYSIS_MULTIPLE_CHOICE: show code, learner predicts the output/behavior (one correct).
- CODE_ANALYSIS_MORE_THAN_ONE_MULTIPLE_CHOICE: show code, multiple correct statements/outputs.
- CODE_ANALYSIS_TEXTUAL: show code, learner types the exact output.
- FIB_CODING: learner fills a blank in code (writes code). Best for APPLY/IMPLEMENT with syntax.
- REARRANGE: learner puts items into the correct ORDER. Best for ANY ordered sequence — a flow, pipeline, lifecycle, process, or multi-step procedure that has one canonical order.

How to choose, using the LO fields:
- bloom_category remember/understand + conceptual  -> MULTIPLE_CHOICE / TRUE_OR_FALSE / MORE_THAN_ONE_MULTIPLE_CHOICE (BUT if the outcome is about an ordered sequence / flow / order / lifecycle / stages, choose REARRANGE — see below — even at remember/understand level)
- SHOW a code snippet to reason about (predict output / find the bug / judge behavior) -> CODE_ANALYSIS_* (TEXTUAL if a typed output, MCQ if pick one). Never paste a snippet to analyze into a non-CODE_ANALYSIS stem.
- apply/implement by WRITING runnable code that reads input and produces output -> FIB_CODING (a blank in the LOGIC of a runnable program)
- a genuine ORDERED SEQUENCE the learner must arrange — a flow, pipeline, lifecycle, process, or multi-step procedure with ONE canonical order and CONCRETE items (steps, stages, code lines) -> REARRANGE, at ANY Bloom level. "Describe / explain the flow / order / sequence / lifecycle / stages of X" IS a REARRANGE (e.g. the request flow "URL -> View -> Model -> Template") — do NOT turn it into MULTIPLE_CHOICE. Prefer MULTIPLE_CHOICE over REARRANGE ONLY when there is no single canonical order, or the items would be abstract concepts rather than concrete ordered steps.

FIB_CODING is graded by EXECUTION (fill the blank -> run on input -> match output), so use it ONLY for a runnable input->output program. NEVER use FIB_CODING (or TEXTUAL) for an installation / shell / environment-setup command (pip install, npm install, mkdir, cd, source .../bin/activate, virtualenv, python -m venv, export VAR=) — route those to MULTIPLE_CHOICE.

EXACT-ANSWER CONSTRAINT (important):
TEXTUAL, CODE_ANALYSIS_TEXTUAL, and FIB_CODING are graded by exact string match,
so the learner's typed answer is sensitive to spelling, whitespace, and case.
- Pick one of these types ONLY when the expected answer is SHORT and EXACT — a
  single term, value, command, or one short line with exactly one acceptable form.
- If the natural answer would be long, a full sentence, or have multiple valid
  forms (synonyms/spellings), DO NOT use these types — prefer an MCQ-style type
  (MULTIPLE_CHOICE / MORE_THAN_ONE_MULTIPLE_CHOICE / CODE_ANALYSIS_MULTIPLE_CHOICE)
  so grading stays robust.

Choose exactly ONE type per outcome. Keep the rationale to 1-2 sentences and say what to assess.""")


def _model():
    # Build from the active LlmProvider (legacy OpenRouter fallback when none configured).
    from .llm_factory import make_chat_model
    return make_chat_model(temperature=0)


def _lo_text(lo: dict) -> str:
    return " ".join(str(lo.get(k) or "") for k in
                    ("syntax", "concept", "sub_concept", "description", "outcome"))


def _fallback_type(lo: dict) -> str:
    """Deterministic backup if the LLM omits a recommendation for some LO."""
    bloom = (lo.get("bloom_category") or "").lower()
    has_syntax = bool((lo.get("syntax") or "").strip())
    # Setup/CLI concepts are conceptual-for-assessment — never a code/FIB target. Gated to
    # LOs that carry code/syntax, so it stays a programming-domain heuristic (won't mis-fire
    # on e.g. 'shared environment' in genetics, which has no syntax).
    if has_syntax and is_setup_or_cli(_lo_text(lo), ""):
        return "MULTIPLE_CHOICE"
    if bloom in ("apply", "implement"):
        return "FIB_CODING" if has_syntax else "TEXTUAL"
    if has_syntax:
        return "CODE_ANALYSIS_MULTIPLE_CHOICE"
    return "MULTIPLE_CHOICE"


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
