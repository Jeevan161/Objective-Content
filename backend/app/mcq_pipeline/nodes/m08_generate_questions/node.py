"""Question pipeline · Node 8 — generate_questions · node entrypoints.

Routes each LO to its type-agent, applies the creation-layer guards (grounding,
code-grounding + visibility, FIB execution / code-output verification, option enforcement),
and exposes the public API: generate_lean / generate_for_los / fix_lean.
"""
from __future__ import annotations

from app.mcq_pipeline.utils import scope
from app.mcq_pipeline.utils.concurrency import pmap
from app.mcq_pipeline.prompts.store import get_prompt
from app.mcq_pipeline.nodes.m08_generate_questions.prompts import CODE_PATH_TYPES, difficulty_of, _FIX_PREFIX
from app.mcq_pipeline.nodes.m08_generate_questions.agents import (
    _AGENTS, _FALLBACK_TYPE, _SCHEMA_BY_TYPE, _invoke,
)
from app.mcq_pipeline.nodes.m08_generate_questions.grounding import (
    _ground, _coverage, _enforce_grounding, _enforce_code_grounding,
)
from app.mcq_pipeline.nodes.m08_generate_questions.verify import _verify_fib, _verify_code_output
from app.mcq_pipeline.nodes.m08_generate_questions.enforce import (
    _enforce_code_visibility, _enforce_options, _normalize_lean_text,
)


def generate_lean(lo: dict, *, max_seq: int | None = None, ctx: str | None = None,
                  fallback_uncovered: bool = True) -> dict:
    """Route the LO to its type-agent and return the lean content (or skip).
    Enforces, AT CREATION (so review rarely intervenes): stem/option grounding,
    code-body grounding (with MCQ fallback), and option count/dedupe. Records every
    RAG call made under `rag_calls`."""
    with scope.recording() as rag_calls:
        res = _generate_lean(lo, max_seq=max_seq, ctx=ctx, fallback_uncovered=fallback_uncovered)
        if res.get("status") == "generated" and res.get("lean"):
            res = _enforce_grounding(lo, res, max_seq)
            if res["question_type"] in CODE_PATH_TYPES:
                res = _enforce_code_grounding(lo, res, max_seq)
                res = _enforce_code_visibility(lo, res)   # snippet lives in the code field, not the stem
            if res["question_type"] == "FIB_CODING":
                res = _verify_fib(lo, res, max_seq)   # run it; repair once; else MCQ
            elif res["question_type"] == "CODE_ANALYSIS_TEXTUAL":
                res = _verify_code_output(lo, res, max_seq)   # set the key to the real stdout
            res = _enforce_options(lo, res, max_seq)
        if res.get("lean"):
            _normalize_lean_text(res["lean"])
    res["rag_calls"] = rag_calls
    return res


def _generate_lean(lo: dict, *, max_seq: int | None = None, ctx: str | None = None,
                   fallback_uncovered: bool = True) -> dict:
    qtype = lo.get("question_type")
    # SQL_FIB_CODING has no generator yet (it needs the external DB_URL/TEST_URL sqlite + test
    # assets). Until that lands, generate the closest gradable SQL item — analyse-a-query — so SQL
    # apply outcomes still produce a usable question. The LO keeps SQL_FIB_CODING at planning/gate.
    sql_fib_fallback = None
    if qtype == "SQL_FIB_CODING":
        sql_fib_fallback = {"from": "SQL_FIB_CODING",
                            "reason": "SQL FIB generator + DB/test assets not yet wired"}
        qtype = "CODE_ANALYSIS_MULTIPLE_CHOICE"
        lo = {**lo, "question_type": qtype}
    if qtype not in _AGENTS:
        return {"status": "skipped", "question_type": qtype, "reason": f"unknown type {qtype!r}"}

    fallback = sql_fib_fallback
    covered, cov = _coverage(lo, qtype, max_seq)
    if not covered:
        if not fallback_uncovered:
            return {"status": "skipped", "question_type": qtype,
                    "reason": "syntax/concept not yet covered in the RAG", "grounding": cov}
        fallback = {"from": qtype, "reason": "syntax/concept not yet covered", "grounding": cov}
        qtype = _FALLBACK_TYPE
        lo = {**lo, "question_type": qtype}
        ctx = None

    if ctx is None:
        ctx = _ground(lo, max_seq)
    lean = _AGENTS[qtype](lo, ctx)
    res = {"status": "generated", "question_type": qtype,
           "difficulty": difficulty_of(lo), "lean": lean.model_dump()}
    if fallback:
        res["fallback"] = fallback
    return res


def fix_lean(lo: dict, ctx: str, prev_lean: dict, issues: list[dict]) -> dict:
    """Targeted regeneration: re-run the SAME type-agent with the previous question
    + the reviewer's issues, asking it to fix only the flagged problems."""
    qtype = lo.get("question_type")
    schema = _SCHEMA_BY_TYPE[qtype]
    issues_txt = "\n".join(
        f"- [{i.get('severity')}] {i.get('rule')}: {i.get('problem')} "
        f"(suggested fix: {i.get('suggested_fix')})" for i in issues) or "(none)"
    extra = get_prompt("gen.fix_prefix", _FIX_PREFIX).format(qtype=qtype, prev=prev_lean, issues=issues_txt)
    lean = _invoke(qtype, lo, ctx, schema, extra)
    return _normalize_lean_text(lean.model_dump())


def generate_for_los(los: list[dict], *, max_seq: int | None = None,
                     fallback_uncovered: bool = True, workers: int = 8,
                     on_progress=None) -> list[dict]:
    """Generate the LEAN question content per LO, concurrently. `on_progress()`
    fires per finished LO."""
    def _one(lo: dict) -> dict:
        res = generate_lean(lo, max_seq=max_seq, fallback_uncovered=fallback_uncovered)
        res["outcome"] = lo.get("outcome")
        if on_progress:
            on_progress()
        return res
    return pmap(_one, los, workers=workers)
