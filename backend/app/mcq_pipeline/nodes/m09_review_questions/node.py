"""Question pipeline · Node 9 — review_questions · node entrypoints.

Per-type REVIEW + targeted-fix pass over the lean questions. `_review_sys` re-applies the
EXACT generation rule blocks (from Node 8) plus the review-specific audits, so the reviewer
independently verifies compliance; `_review_lean` layers the deterministic guards on top.
The fix loop reviews → fixes (via Node 8's `fix_lean`) → re-reviews up to `max_retries`.
"""
from __future__ import annotations

from app.mcq_pipeline.utils import scope
from app.mcq_pipeline.utils.concurrency import pmap
from app.mcq_pipeline.prompts.store import get_prompt
from app.mcq_pipeline.nodes.m08_generate_questions import (
    CODE_PATH_TYPES, OPTION_TYPES, _CODE_RULES, _EXACT_ANSWER_RULES, _EXPLANATION_RULES,
    _FIB_RULES, _GROUNDING_RULES, _MORE_THAN_ONE_RULES, _OPTION_RULES, _QUESTION_TEXT_RULES,
    _REARRANGE_RULES, _SQL_RULES, _TRUE_FALSE_RULES, _TYPED_ANSWER_TYPES,
    _course_is_sql, _ground, _lo_block, fix_lean,
)
from app.mcq_pipeline.nodes.m09_review_questions.prompts import (
    _REVIEW_PERSONA, _REVIEW_CONSTRAINTS, _VALIDATION_CHECKLIST, _GROUNDING_AUDIT,
    _SELF_CONTAINMENT_AUDIT, _DISTRACTOR_AUDIT, _TYPE_CHECKLIST,
)
from app.mcq_pipeline.nodes.m09_review_questions.guards import (
    QuestionReview, _review_model, _named_only, _uncovered, _external_ref,
    _code_external_dep, _verbatim_code, _PHANTOM_CODE_RE, _audit_distractor_depth,
)


def _review_sys(qtype: str) -> str:
    # Re-apply the EXACT generation rule blocks the question was written from, so the
    # reviewer independently verifies compliance rather than proofreading.
    parts = [
        get_prompt("review.persona", _REVIEW_PERSONA),
        get_prompt("review.constraints", _REVIEW_CONSTRAINTS),
        get_prompt("gen.grounding_rules", _GROUNDING_RULES),
        get_prompt("gen.question_text_rules", _QUESTION_TEXT_RULES),
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
    # The exact per-type generation instruction the question was authored against.
    extra = get_prompt(f"gen.extra.{qtype}")
    if extra:
        parts.append(f"TYPE INSTRUCTION THE QUESTION WAS WRITTEN FROM:\n{extra}")
    # Dedicated audits.
    parts.append(get_prompt("review.grounding_audit", _GROUNDING_AUDIT))
    parts.append(get_prompt("review.self_containment_audit", _SELF_CONTAINMENT_AUDIT))
    if qtype in OPTION_TYPES:
        parts.append(get_prompt("review.distractor_audit", _DISTRACTOR_AUDIT))
    if qtype in _TYPE_CHECKLIST:
        parts.append("FOCUS FOR THIS TYPE:\n" + get_prompt(
            f"review.type_checklist.{qtype}", _TYPE_CHECKLIST[qtype]))
    # SQL domain rules last among the domain blocks, so for SQL they AUTHORITATIVELY
    # override the generic "say 'prints'/'output'" wording in the code type-checklists
    # (a SQL query RETURNS a result set — it does not print).
    if _course_is_sql():
        parts.append("SQL OVERRIDE — for this SQL question the rules below take precedence "
                     "over any generic 'prints'/'output' wording above:\n"
                     + get_prompt("gen.sql_rules", _SQL_RULES))
    # Final gate — applied last.
    parts.append(get_prompt("review.checklist", _VALIDATION_CHECKLIST))
    return "\n\n".join(parts)


def _review_lean(lo: dict, qtype: str, lean: dict, ctx: str) -> dict:
    user = (
        f"LEARNING OUTCOME (intent):\n{_lo_block(lo)}\n\n"
        f"COURSE MATERIAL (ground truth):\n{ctx}\n\n"
        f"GENERATED {qtype} QUESTION (review this):\n{lean}"
    )
    review: QuestionReview = _review_model(0).with_structured_output(QuestionReview).invoke(
        [{"role": "system", "content": _review_sys(qtype)},
         {"role": "user", "content": user}]
    )
    out = review.model_dump()

    # Beyond the LLM's guideline review, verify against the RAG that everything the
    # question leans on is actually taught. Untaught material becomes a high-severity
    # grounding issue, which the fix loop regenerates away (re-checked each re-review).
    # One shared `seen` budget bounds the combined RAG fan-out for this question.
    seen: set[str] = set()
    issues = list(out.get("issues") or [])

    # (a) code snippet constructs
    if qtype in CODE_PATH_TYPES:
        uncovered = _uncovered(out.get("used_syntax") or [], seen)
        out["uncovered_syntax"] = uncovered
        if uncovered:
            joined = ", ".join(uncovered)
            issues.append({
                "rule": "GROUNDING",
                "problem": f"the snippet uses syntax not explained in the course material: {joined}",
                "severity": "high",
                "suggested_fix": f"rewrite the snippet to use ONLY constructs taught in the "
                                 f"material (replace or remove: {joined})",
            })
            out["passed"] = False

    # (b) option/distractor terms — a wrong option built from an untaught term is
    # invalid: the learner was never taught it, so can't evaluate the option.
    if qtype in OPTION_TYPES:
        named = _named_only(out.get("option_terms") or [])
        uncovered_opts = _uncovered(named, seen)
        out["uncovered_option_terms"] = uncovered_opts
        if uncovered_opts:
            joined = ", ".join(uncovered_opts)
            issues.append({
                "rule": "GROUNDING",
                "problem": f"option(s) reference terms not taught in the course material: "
                           f"{joined} — a learner never taught these cannot evaluate the distractor",
                "severity": "high",
                "suggested_fix": f"rebuild the affected option(s) using ONLY concepts/terms taught "
                                 f"in the material; make distractors wrong by misapplying TAUGHT "
                                 f"concepts (replace or remove: {joined})",
            })
            out["passed"] = False

    # (c) self-containment — a question that defers to the external source is invalid;
    # the learner never sees the reading. Deterministic so the LLM can't overlook it.
    ref = _external_ref(lean)
    if ref:
        issues.append({
            "rule": "SELF-CONTAINMENT",
            "problem": f"the question refers to the external source instead of standing alone "
                       f"(found: {ref!r}) — a learner sees only the question, not the reading",
            "severity": "high",
            "suggested_fix": "remove the reference and embed any needed context directly in the "
                             "stem; the question must be answerable without the reading material",
        })
        out["passed"] = False

    # (c2) external dependency in code — the snippet must run on built-ins + taught
    # constructs alone. A file read, network/db call, env var, or shell command pulls in
    # a resource the learner can't see, so the question isn't self-contained.
    if qtype in CODE_PATH_TYPES:
        dep = _code_external_dep(lean)
        if dep:
            issues.append({
                "rule": "SELF-CONTAINMENT",
                "problem": f"the code depends on an external resource the learner cannot see "
                           f"(found: {dep!r}) — files, network, database, env vars, and shell "
                           f"commands are not available when the question is shown in isolation",
                "severity": "high",
                "suggested_fix": "remove the external access and define every value the program "
                                 "needs INLINE as a literal (or via the FIB test stdin); the snippet "
                                 "must run with only built-ins and taught constructs",
            })
            out["passed"] = False

    # (c3) verbatim source code — a snippet copied wholesale from the material tests memory
    # of the example, not understanding. Flag so the fix loop re-expresses it (rename
    # variables/identifiers, change literal values) while keeping the taught pattern.
    if qtype in CODE_PATH_TYPES:
        copied = _verbatim_code(lean, ctx)
        if copied:
            issues.append({
                "rule": "VERBATIM SOURCE",
                "problem": f"the code is copied verbatim from the course material (e.g. {copied!r}) — "
                           f"this tests recall of the example rather than understanding the concept",
                "severity": "high",
                "suggested_fix": "rewrite the snippet so it is NOT a copy: rename every variable, "
                                 "function, and identifier to fresh generic names and change the literal "
                                 "values, while keeping the taught construct/pattern the question assesses",
            })
            out["passed"] = False

    # (c4) phantom code reference — an OPTION type with NO code field (MULTIPLE_CHOICE /
    # MORE_THAN_ONE_MULTIPLE_CHOICE) whose stem points at a snippet the learner cannot see.
    # The right home for a code-behaviour question is CODE_ANALYSIS_MULTIPLE_CHOICE (it has a
    # code field). Deterministic so a 'the following code' stem on a plain MCQ never passes.
    if qtype in ("MULTIPLE_CHOICE", "MORE_THAN_ONE_MULTIPLE_CHOICE"):
        stem = lean.get("question") or ""
        has_code = bool((lean.get("code") or "").strip()) or "```" in stem
        if not has_code and _PHANTOM_CODE_RE.search(stem):
            issues.append({
                "rule": "PHANTOM CODE",
                "problem": "the stem refers to a code snippet/program/output, but this question type "
                           "cannot display code — the learner sees a dangling reference to code that is "
                           "not shown",
                "severity": "high",
                "suggested_fix": "regenerate as CODE_ANALYSIS_MULTIPLE_CHOICE with the snippet in the "
                                 "code field, or rewrite the stem so it tests the concept without "
                                 "referring to a shown snippet",
            })
            out["passed"] = False

    # (d) distractor depth — for understand+/apply LOs, every WRONG option must be evaluable
    # from taught material (the concept that makes it wrong is taught to the required depth).
    dd_issues = _audit_distractor_depth(lo, qtype, lean, ctx)
    if dd_issues:
        issues.extend(dd_issues)
        out["passed"] = False

    out["issues"] = issues
    return out


def review_and_fix_one(lo: dict, gen: dict, *, max_seq: int | None = None,
                       max_retries: int = 3) -> dict:
    """Review a generated question and, while it fails, apply a TARGETED fix and
    re-review — up to `max_retries` times. Keeps every verdict in `review_history`.
    Records the RAG calls made during review/fix under `review_rag_calls` (the
    generation-time `rag_calls` carried in `gen` are preserved alongside)."""
    with scope.recording() as rag_calls:
        res = _review_and_fix_one(lo, gen, max_seq=max_seq, max_retries=max_retries)
    res["review_rag_calls"] = rag_calls
    return res


def _review_and_fix_one(lo: dict, gen: dict, *, max_seq: int | None = None,
                        max_retries: int = 3) -> dict:
    if gen.get("status") != "generated" or not gen.get("lean"):
        return {**gen, "review": None, "review_history": [], "attempts": 0, "needs_human": False}

    qtype = gen["question_type"]
    ctx = _ground(lo, max_seq)
    fix_lo = {**lo, "question_type": qtype}

    lean = gen["lean"]
    history: list[dict] = []
    review = _review_lean(lo, qtype, lean, ctx)
    history.append(review)

    attempts = 0
    while not review.get("passed") and attempts < max_retries:
        lean = fix_lean(fix_lo, ctx, lean, review.get("issues", []))
        attempts += 1
        review = _review_lean(lo, qtype, lean, ctx)
        history.append(review)

    return {**gen, "lean": lean, "review": review, "review_history": history,
            "attempts": attempts, "needs_human": not review.get("passed")}


def _pair(los: list[dict], questions: list[dict]):
    by_outcome = {lo.get("outcome"): lo for lo in los}
    for i, gen in enumerate(questions):
        yield (by_outcome.get(gen.get("outcome")) or (los[i] if i < len(los) else {})), gen


def review_and_fix_for_los(los: list[dict], questions: list[dict], *,
                           max_seq: int | None = None, max_retries: int = 3,
                           workers: int = 8, on_progress=None) -> list[dict]:
    """Review-and-fix each generated question against its LO, concurrently."""
    def _one(pair):
        res = review_and_fix_one(pair[0], pair[1], max_seq=max_seq, max_retries=max_retries)
        if on_progress:
            on_progress(needs_human=bool(res.get("needs_human")))
        return res
    return pmap(_one, list(_pair(los, questions)), workers=workers)
