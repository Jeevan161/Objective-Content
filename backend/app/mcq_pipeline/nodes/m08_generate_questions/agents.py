"""Question pipeline · Node 8 — generate_questions · agents.

One LLM agent per question type. Each composes the reusable guideline blocks into a
system prompt (`_sys_for`), invokes the generation model with the type's lean schema,
and returns a pydantic model. `_AGENTS` / `_SCHEMA_BY_TYPE` are the dispatch tables.
"""
from __future__ import annotations

from app.mcq_pipeline.prompts.store import get_prompt, register
from app.mcq_pipeline.schemas.questions import (CodeMCQLean, CodeMoreThanOneLean, CodeTextualLean,
                                                FibCodingLean, MCQLean, RearrangeLean, TextualLean,
                                                TrueFalseLean)
from app.mcq_pipeline.nodes.m08_generate_questions.prompts import (
    CODE_PATH_TYPES, _TYPED_ANSWER_TYPES, difficulty_of,
    _PERSONA, _HEADER, _GROUNDING_RULES, _QUESTION_TEXT_RULES, _MARKDOWN_RULES, _OPTION_RULES,
    _EXPLANATION_RULES, _TRUE_FALSE_RULES, _MORE_THAN_ONE_RULES, _FIB_RULES, _REARRANGE_RULES,
    _CODE_RULES, _EXACT_ANSWER_RULES, _SQL_RULES, _SCENARIO_RULES, _FINAL_VALIDATION,
)
from app.mcq_pipeline.nodes.m08_generate_questions.grounding import _course_is_sql, _lo_block


def _model(temp: float = 0.3):
    # Question GENERATION agent. Built on the active connector (OpenRouter) but with the
    # generation model id (settings.mcq_generation_model — Sonnet 4.6); empty -> the
    # connector's own model. Review uses its own model (see m09._review_model).
    from app.core.config import settings
    from app.mcq_pipeline.utils.llm import make_chat_model
    return make_chat_model(temperature=temp, model=settings.mcq_generation_model or None)


def _sys_for(qtype: str, lo: dict) -> str:
    """Assemble the system prompt for a type, composing the reusable blocks
    (each fetched live so DB edits take effect)."""
    parts = [
        f"{get_prompt('gen.persona', _PERSONA)} "
        f"{get_prompt('gen.header', _HEADER).format(qtype=qtype)}",
        get_prompt("gen.grounding_rules", _GROUNDING_RULES),
        get_prompt("gen.question_text_rules", _QUESTION_TEXT_RULES),
        get_prompt("gen.markdown_rules", _MARKDOWN_RULES),
        get_prompt("gen.option_rules", _OPTION_RULES),
        get_prompt("gen.explanation_rules", _EXPLANATION_RULES),
    ]
    if qtype == "TRUE_OR_FALSE":
        parts.append(get_prompt("gen.true_false_rules", _TRUE_FALSE_RULES))
    if qtype == "MORE_THAN_ONE_MULTIPLE_CHOICE":
        parts.append(get_prompt("gen.more_than_one_rules", _MORE_THAN_ONE_RULES))
    if qtype == "FIB_CODING":
        parts.append(get_prompt("gen.fib_rules", _FIB_RULES))
    if qtype == "REARRANGE":
        parts.append(get_prompt("gen.rearrange_rules", _REARRANGE_RULES))
    if qtype in CODE_PATH_TYPES:
        parts.append(get_prompt("gen.code_rules", _CODE_RULES))
    if qtype in _TYPED_ANSWER_TYPES:
        parts.append(get_prompt("gen.exact_answer_rules", _EXACT_ANSWER_RULES))
    if _course_is_sql():
        parts.append(get_prompt("gen.sql_rules", _SQL_RULES))
    parts.append(f"Difficulty: {difficulty_of(lo)}.")
    if lo.get("is_scenario") or (lo.get("bloom_level_raw") or "").lower() == "scenario":
        parts.append(get_prompt("gen.scenario_rules", _SCENARIO_RULES))
    parts.append(get_prompt("gen.final_validation", _FINAL_VALIDATION))
    return "\n\n".join(parts)


def _invoke(qtype: str, lo: dict, ctx: str, schema, extra: str = ""):
    sys = _sys_for(qtype, lo)
    user = f"{extra}\nLEARNING OUTCOME (intent):\n{_lo_block(lo)}\n\nCOURSE MATERIAL (ground truth):\n{ctx}"
    return _model().with_structured_output(schema).invoke(
        [{"role": "system", "content": sys}, {"role": "user", "content": user}]
    )


# --- one agent per type (each returns a lean pydantic model) ---------------- #
# Shared code-analysis strategy: a single priority-ordered angle policy for all
# three CODE_ANALYSIS_* types (replaces the old per-type angle text).
_CODE_STRATEGY = """\
Choose the analysis angle that most directly measures the Learning Outcome.

Priority:
1. Output Prediction
2. Error Identification
3. Identify-and-Fix
4. Functionality
5. Logic Analysis
6. Equivalent Code

Use the highest-priority angle that naturally fits the LO; do not choose an angle merely for variety. Use the error/fix angles ONLY when the LO is actually about correctness, syntax, or structure — never manufacture an error for a purely conceptual LO."""

# Per-type instruction blocks (DB-overridable).
register("gen.extra.MULTIPLE_CHOICE", """\
Choose the simplest MCQ structure that directly measures the Learning Outcome.

Preference order:
1. Direct best-answer question
2. Choose the correct statement
3. Choose the incorrect statement
4. Assertion–Reason

Use Assertion–Reason only when the LO explicitly involves causal reasoning.

If the concept is narrow, build distractors from sibling or contrasting taught concepts rather than rewordings of the key; otherwise use a choose-the-incorrect framing.

Generate one stem and four options with EXACTLY one is_correct=true.""")
register("gen.extra.TRUE_OR_FALSE",
         "Write one statement and whether it is true. If the LO is about code, its output, "
         "or its behavior, include a short snippet in `code` and judge a claim about it; "
         "otherwise leave `code` empty for a purely conceptual statement.")
register("gen.extra.MORE_THAN_ONE_MULTIPLE_CHOICE",
         "Provide a question stem and 4-6 options with 2-3 is_correct=true and at least one "
         "is_correct=false. Every false option must be genuinely incorrect per the material "
         "(not a true taught fact relabeled false); if you can't, use fewer options.")
register("gen.extra.TEXTUAL", """\
Provide a question stem and one exact expected answer.

The answer must be:
- one word
- OR two words maximum
- OR a numeric value

Do NOT use commands, code statements, expressions, or sentences.

If the natural answer is longer than two words, choose a different question type.""")
register("gen.extra.CODE_ANALYSIS_MULTIPLE_CHOICE",
         f"{_CODE_STRATEGY} Write a short snippet from taught syntax; give its real "
         "output as correct_output and 3 plausible wrong outputs.")
register("gen.extra.CODE_ANALYSIS_TEXTUAL",
         f"{_CODE_STRATEGY} Write a short snippet whose exact output is brief and "
         "unambiguous (a single value/line), and give that exact real output.")
register("gen.extra.CODE_ANALYSIS_MORE_THAN_ONE_MULTIPLE_CHOICE",
         f"{_CODE_STRATEGY} Write a snippet; list all TRUE statements about it and the false ones.")
register("gen.extra.FIB_CODING",
         "Write a stem stating what to accomplish, then a COMPLETE runnable program "
         "(taught syntax only) with the sentinel {{BLANK}} on the ONE line to complete. "
         "Provide blank_answer (a correct completion), test_input (stdin, or \"\" if none), "
         "and test_output = the EXACT stdout the correctly-filled program prints. It is "
         "graded by RUNNING the code and comparing output, so ensure it actually runs and "
         "prints test_output.")
register("gen.extra.REARRANGE",
         "Provide a question stem and 3-6 ordered steps/lines in the CORRECT sequence "
         "(first to last). Each item must be a CONCRETE action or a real code line — never "
         "a concept, goal, or 'understand X'. Use only steps grounded in the material, with "
         "ONE unambiguous canonical order.")


def _extra(qtype: str) -> str:
    return get_prompt(f"gen.extra.{qtype}")


def agent_multiple_choice(lo, ctx):
    return _invoke("MULTIPLE_CHOICE", lo, ctx, MCQLean, _extra("MULTIPLE_CHOICE"))

def agent_true_or_false(lo, ctx):
    return _invoke("TRUE_OR_FALSE", lo, ctx, TrueFalseLean, _extra("TRUE_OR_FALSE"))

def agent_more_than_one(lo, ctx):
    return _invoke("MORE_THAN_ONE_MULTIPLE_CHOICE", lo, ctx, MCQLean, _extra("MORE_THAN_ONE_MULTIPLE_CHOICE"))

def agent_textual(lo, ctx):
    return _invoke("TEXTUAL", lo, ctx, TextualLean, _extra("TEXTUAL"))

def agent_code_mcq(lo, ctx):
    return _invoke("CODE_ANALYSIS_MULTIPLE_CHOICE", lo, ctx, CodeMCQLean, _extra("CODE_ANALYSIS_MULTIPLE_CHOICE"))

def agent_code_textual(lo, ctx):
    return _invoke("CODE_ANALYSIS_TEXTUAL", lo, ctx, CodeTextualLean, _extra("CODE_ANALYSIS_TEXTUAL"))

def agent_code_more_than_one(lo, ctx):
    return _invoke("CODE_ANALYSIS_MORE_THAN_ONE_MULTIPLE_CHOICE", lo, ctx, CodeMoreThanOneLean,
                   _extra("CODE_ANALYSIS_MORE_THAN_ONE_MULTIPLE_CHOICE"))

def agent_fib_coding(lo, ctx):
    return _invoke("FIB_CODING", lo, ctx, FibCodingLean, _extra("FIB_CODING"))

def agent_rearrange(lo, ctx):
    return _invoke("REARRANGE", lo, ctx, RearrangeLean, _extra("REARRANGE"))


_AGENTS = {
    "MULTIPLE_CHOICE": agent_multiple_choice,
    "TRUE_OR_FALSE": agent_true_or_false,
    "MORE_THAN_ONE_MULTIPLE_CHOICE": agent_more_than_one,
    "TEXTUAL": agent_textual,
    "CODE_ANALYSIS_MULTIPLE_CHOICE": agent_code_mcq,
    "CODE_ANALYSIS_TEXTUAL": agent_code_textual,
    "CODE_ANALYSIS_MORE_THAN_ONE_MULTIPLE_CHOICE": agent_code_more_than_one,
    "FIB_CODING": agent_fib_coding,
    "REARRANGE": agent_rearrange,
}

_SCHEMA_BY_TYPE = {
    "MULTIPLE_CHOICE": MCQLean,
    "TRUE_OR_FALSE": TrueFalseLean,
    "MORE_THAN_ONE_MULTIPLE_CHOICE": MCQLean,
    "TEXTUAL": TextualLean,
    "CODE_ANALYSIS_MULTIPLE_CHOICE": CodeMCQLean,
    "CODE_ANALYSIS_TEXTUAL": CodeTextualLean,
    "CODE_ANALYSIS_MORE_THAN_ONE_MULTIPLE_CHOICE": CodeMoreThanOneLean,
    "FIB_CODING": FibCodingLean,
    "REARRANGE": RearrangeLean,
}

_FALLBACK_TYPE = "MULTIPLE_CHOICE"
