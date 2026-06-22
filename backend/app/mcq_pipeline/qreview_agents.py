"""
qreview_agents.py (vendored, adapted)
-------------------------------------
Per-type REVIEW + targeted-fix pass over the lean questions. Adapted: relative
imports; reuses the SAME guideline blocks the generator used (so editing a
`gen.*` prompt updates both generation AND review); review-specific prompts
registered with `prompt_store`; optional `on_progress` callback.
"""

from __future__ import annotations

import re
from typing import List, Literal

from pydantic import BaseModel, Field

from . import rag_api, scope
from .concurrency import pmap
from .prompt_store import get_prompt, register
from .qgen_agents import (
    CODE_PATH_TYPES, OPTION_TYPES, _CODE_RULES, _EXACT_ANSWER_RULES,
    _EXPLANATION_RULES, _FIB_RULES, _GROUNDING_RULES, _MORE_THAN_ONE_RULES,
    _OPTION_RULES, _QUESTION_TEXT_RULES, _REARRANGE_RULES, _TRUE_FALSE_RULES,
    _TYPED_ANSWER_TYPES, _ground, _lo_block, _model, fix_lean,
)


class ReviewIssue(BaseModel):
    rule: str = Field(description="ONE primary guideline area, e.g. OPTION RULES / GROUNDING / "
                                  "TRUE-FALSE. Severitize by THIS rule — never inherit GROUNDING "
                                  "severity for an option-quality concern.")
    problem: str = Field(description="what specifically violates the rule in this question")
    severity: Literal["high", "medium", "low"]
    suggested_fix: str = Field(description="the minimal change that would resolve it")


class QuestionReview(BaseModel):
    passed: bool = Field(description="true if there are NO high-severity issues "
                                     "(medium/low are advisory and do not block)")
    issues: List[ReviewIssue] = Field(description="every genuine guideline violation found")
    used_syntax: List[str] = Field(
        default_factory=list,
        description="for code questions, every DISTINCT language construct, function, "
                    "operator, or API the snippet uses (e.g. 'f-string', 'list "
                    "comprehension', 'dict.get()', 'enumerate()'); empty list for "
                    "non-code questions")
    option_terms: List[str] = Field(
        default_factory=list,
        description="for questions WITH OPTIONS, every DISTINCT NAMED technical entity "
                    "an option refers to — a library, framework, tool, command, "
                    "function/method, API, language keyword, version number, or proper "
                    "noun (e.g. 'Django', 'venv', 'dict.get()', 'Django 4'). These are "
                    "things a learner must have been taught BY NAME. Do NOT list generic "
                    "descriptive phrases or paraphrases of the concept (e.g. 'shared "
                    "space', 'project dependencies', 'standard practice') — those are "
                    "prose, not named terms. Empty list when the options name nothing specific.")
    summary: str = Field(description="one-line verdict for the human reviewer")


_REVIEW_PERSONA = register("review.persona", """\
You are a Senior Assessment Quality Reviewer with 10+ years of experience reviewing programming assessments.

Your role is to determine whether a generated question is safe to release to learners.

You must perform a strict validation against:
- Learning Outcome alignment
- Bloom level alignment
- Grounding rules
- Question-writing rules
- Question-type rules
- Distractor quality rules
- Exact-answer rules
- Code rules
- Explanation rules

Assume the question is incorrect until it proves otherwise — for the answer key, LO/Bloom alignment, and grading reliability.

For GROUNDING specifically, judge at the CONCEPT level: paraphrases, synonyms, and natural-language restatements of taught concepts PASS. Flag a grounding issue only for a genuinely new, untaught NAMED term (technology/tool/library/command/version/proper-noun) that you can name concretely as absent from the material.

Your responsibility is to identify every genuine issue that could reduce assessment validity, fairness, clarity, grounding, or grading reliability — without inventing defects to justify failing a sound question.

Do NOT rewrite the question. Only report issues and the minimum change required to fix them.""")

_REVIEW_CONSTRAINTS = register("review.constraints", """\
REVIEW CONSTRAINTS

General Rules
- Do not change the question type.
- Do not change the intended Learning Outcome.
- Do not flip correctness labels.
- Do not redesign the question.
- Suggest the smallest possible fix.

Pass Criteria
passed = false if ANY HIGH-severity issue exists. MEDIUM and LOW issues are ADVISORY — report them, but they do NOT block passing.
A question that satisfies all the rules passes; do not invent defects to justify failing it.

Validation Scope
Re-apply ALL generation rule blocks and INDEPENDENTLY verify compliance — do not assume the generator followed them:
- gen.grounding_rules
- gen.question_text_rules
- gen.option_rules
- gen.true_false_rules
- gen.more_than_one_rules
- gen.fib_rules
- gen.explanation_rules
- gen.exact_answer_rules
- gen.code_rules
- gen.extra.{question_type}

Issue Severity
HIGH (these BLOCK passing — reserve for genuine validity/grading defects):
- Incorrect answer key
- Grounding violation — a genuinely NEW, untaught NAMED technology/tool/command/version/proper-noun you can quote as absent (NOT a paraphrase/synonym of a taught concept)
- LO mismatch
- Bloom mismatch
- More than one correct answer in a single-answer question (or a second defensible correct answer)
- Missing correct answer
- Invalid exact-answer grading (the typed answer has multiple valid forms)
- Answer giveaway (the stem or options reveal the answer)
- Not self-contained — the stem or an option defers to the source ('according to the material', 'the reading', 'the lesson') or names an example entity the learner cannot see (a scenario label like 'Project A', a sample variable/file name from the reading)
MEDIUM (advisory — report, do not block):
- Ambiguous wording
- Excessive complexity
LOW (advisory — report, do not block):
- Poor option balance / length imbalance
- Weak distractors
- Ungrounded distractor that uses taught vocabulary (debatable)
- Explanation quality issue
- Style / minor wording / formatting
Only HIGH issues prevent passing; MEDIUM and LOW are advisory.

Output Fields (REQUIRED — the automated grounding checks depend on these)
- For code questions, populate `used_syntax` with every distinct language construct, function, operator, or API the snippet uses (empty list for non-code questions).
- For questions WITH OPTIONS, populate `option_terms` with the distinct NAMED technical entities the options refer to (libraries, tools, commands, functions/methods, version numbers, proper nouns) — ESPECIALLY in distractors. List ONLY named terms a learner must be taught BY NAME; do NOT list generic descriptive phrases or paraphrases.""")

_VALIDATION_CHECKLIST = register("review.checklist", """\
VALIDATION CHECKLIST

Before returning passed=true, verify ALL of the following:

LO Alignment
- Tests the intended skill
- Tests the intended concept
- Matches Bloom level
- Does not assess unrelated knowledge

Grounding (concept-level — do not demand exact wording)
- Every technical CONCEPT is taught (paraphrases/synonyms/restatements are fine)
- Every distractor builds on taught concepts
- No genuinely NEW, untaught technology/tool/library/command/version/proper-noun/syntax is introduced

Question Quality
- Single clear problem
- No ambiguity
- No hidden assumptions
- No accidental clues
- No answer giveaway

Answer Quality
- Correct answer is uniquely correct
- Incorrect answers are genuinely incorrect
- Distractors are plausible
- Distractors are conceptually related

Explanation Quality
- Correct reasoning is explained
- Incorrect reasoning is addressed
- Uses only taught concepts (paraphrase OK)

Grading Reliability
- Can be graded consistently
- No multiple interpretations
- No alternate valid answers

Type-Specific Rules
- All rules for the question type are satisfied""")

_GROUNDING_AUDIT = register("review.grounding_audit", """\
GROUNDING AUDIT

Judge grounding at the CONCEPT level, against the FULL current-session reading material provided below (not just a remembered slice).

A paraphrase, synonym, or natural-language restatement of a concept the material teaches is GROUNDED — do NOT flag it, and do NOT demand the question reuse the material's exact wording. (e.g. if the material describes Project A needing Django 3 while Project B needs Django 5, then "version conflict" is grounded; if it says "global environment", then "shared environment" is grounded.) A distractor that expresses a plausible misconception using taught vocabulary is grounded.

Raise a GROUNDING issue ONLY for a genuinely NEW, untaught technology, tool, library, command, version, or proper noun that does not appear in the material.

Audit every named keyword, operator, function, command, API, library, framework, and proper noun in the stem, options, code, and explanation. For each:
1. Is the underlying concept taught in the material? (paraphrase is fine)
2. Is it used correctly?

If you are UNSURE whether a named item is external, DOWNGRADE it to a LOW note — never HIGH. Reserve HIGH for a specific named item you can quote as provably absent from the material. If only the wording differs from the material, do not raise an issue at all.""")

_SELF_CONTAINMENT_AUDIT = register("review.self_containment_audit", """\
SELF-CONTAINMENT AUDIT

A question is an INDEPENDENT resource: the learner sees ONLY the stem and options, never the reading material. Raise a HIGH 'SELF-CONTAINMENT' issue when the question fails to stand alone:

1. It defers to the source — phrases like 'according to the material', 'based on the lesson', 'in the reading/passage', 'as discussed', 'from this session'.
2. It references an example entity defined only in the source and NOT in the question itself — a scenario label ('Project A'/'Project B'), a sample file/variable/function name, a character, or a one-off value the learner would only know from the reading.
3. Answering requires having read a specific passage rather than understanding the concept.

The fix is to embed the needed context generically in the stem, or to test the transferable concept instead of the source-specific detail. NOTE: naming a genuinely taught technology/tool/command (venv, pip, Django) is NOT a violation — only source-local references and undefined example entities are.""")

_DISTRACTOR_AUDIT = register("review.distractor_audit", """\
DISTRACTOR AUDIT

For every incorrect choice, verify it is:
- grounded
- plausible
- related to the LO
- technically incorrect

Reject (flag) distractors that are:
- absurd
- unrelated
- untaught
- obviously wrong
- accidentally correct""")

_TYPE_CHECKLIST = {
    "MULTIPLE_CHOICE": (
        "Confirm:\n"
        "- Exactly one answer is correct.\n"
        "- No distractor is accidentally correct.\n"
        "- No distractor is obviously absurd.\n"
        "- All options are grounded.\n"
        "- Options are balanced in length.\n"
        "- The correct answer does not stand out."),
    "TRUE_OR_FALSE": (
        "Confirm the statement is declarative (not a question), not prefixed with "
        "'True or False:', tests exactly one idea, and that a false statement is "
        "plausible and grounded."),
    "MORE_THAN_ONE_MULTIPLE_CHOICE": (
        "Confirm:\n"
        "- 4–6 options exist.\n"
        "- At least 2 answers are correct.\n"
        "- At least 1 answer is incorrect.\n"
        "- Each option can be evaluated independently.\n"
        "- No option implies another option.\n"
        "- No overlapping correctness exists."),
    "FIB_CODING": (
        "Confirm:\n"
        "- The blank targets the LO concept.\n"
        "- The blank is not boilerplate.\n"
        "- The answer is not revealed elsewhere.\n"
        "- Exactly one valid completion exists.\n"
        "Fail if multiple completions could work, equivalent alternatives exist, or "
        "the blank can be solved by guesswork rather than understanding the LO."),
    "CODE_ANALYSIS_MULTIPLE_CHOICE": (
        "Confirm the code is essential, not in backticks, does not disclose the "
        "correct output, exactly one option is correct, and all options are grounded."),
    "CODE_ANALYSIS_TEXTUAL": (
        "Confirm:\n"
        "- Code is essential.\n"
        "- Code is not wrapped in backticks.\n"
        "- Output is a single short value or line.\n"
        "- Exactly one output representation exists.\n"
        "- Output is not formatting-sensitive.\n"
        "- No whitespace-sensitive grading issue exists.\n"
        "Fail if the output could reasonably be written in multiple valid ways."),
    "CODE_ANALYSIS_MORE_THAN_ONE_MULTIPLE_CHOICE": (
        "Confirm the code is essential, not in backticks, does not disclose the "
        "answers, at least 2 statements are true and at least 1 is false, and each "
        "statement is independently evaluable."),
    "TEXTUAL": (
        "Confirm:\n"
        "- Answer is one word OR two words maximum OR a numeric value.\n"
        "- Answer is not a command.\n"
        "- Answer is not code.\n"
        "- Answer is not a sentence.\n"
        "- Exactly one acceptable form exists.\n"
        "- No reasonable synonym exists.\n"
        "- No reasonable spelling variation exists.\n"
        "- No case-sensitivity trap exists.\n"
        "If two knowledgeable learners could provide different correct answers, fail "
        "the question."),
    "REARRANGE": (
        "Confirm:\n"
        "- Every item is a CONCRETE action or a real code line (NOT a concept, goal, or "
        "mental state such as 'understand X').\n"
        "- The items form a genuine ordered procedure with ONE canonical sequence.\n"
        "- There are 3-6 distinct, non-overlapping steps.\n"
        "- The order is unambiguous and grounded in the material.\n"
        "Fail if any item is conceptual, if the order is not unique, or if the material "
        "defines no strict sequence (then REARRANGE is the wrong type)."),
}
for _k, _v in _TYPE_CHECKLIST.items():
    register(f"review.type_checklist.{_k}", _v)


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
    # Final gate — applied last.
    parts.append(get_prompt("review.checklist", _VALIDATION_CHECKLIST))
    return "\n\n".join(parts)


# A "named term" carries a technical signal — an uppercase letter (proper noun /
# CamelCase), a digit (version), or code punctuation (dotted call, path, parens,
# underscore, backtick). Generic all-lowercase prose phrases ("shared space",
# "project dependencies") carry none and are NOT RAG-checked: whether their CLAIM is
# grounded is the reviewer LLM's job, not a literal keyword lookup. This is what
# prevents the option-term check from false-flagging paraphrased distractors.
_NAMED_SIGNAL = re.compile(r"[A-Z0-9._/()`]")


def _named_only(terms: list[str]) -> list[str]:
    return [t for t in (terms or []) if t and _NAMED_SIGNAL.search(t)]


def _uncovered(terms: list[str], seen: set[str], *, cap: int = 14) -> list[str]:
    """RAG-check each term and return those the course material does NOT explain.
    `seen` is shared across the syntax + option checks for one question so the
    combined RAG fan-out stays bounded (deduped, capped)."""
    uncovered: list[str] = []
    for raw in terms or []:
        term = (raw or "").strip()
        key = term.lower()
        if not term or key in seen:
            continue
        seen.add(key)
        if len(seen) > cap:  # safety cap on RAG fan-out per question
            break
        verdict = (rag_api.check_concept(term).get("verdict") or "").split("\n", 1)[0].upper()
        if "NOT EXPLAINED" in verdict:
            uncovered.append(term)
    return uncovered


# Deterministic self-containment guard. A self-contained question embeds everything it
# needs; deferring to the external source ("according to the material", "the reading",
# "the lesson"…) is never valid and the LLM reviewer can miss it (reports #2/#7). High
# precision BY DESIGN: it does NOT match "which of the following", "the following code",
# "based on the input", or "in the session" (Django session), to avoid false positives.
_EXTERNAL_REF_RE = re.compile(
    r"\b(?:reading|course|learning|study)\s+material\b"
    r"|\baccording to (?:the |this |our )?(?:above |below |given )?(?:course |reading )?"
    r"(?:material|reading|lesson|passage|tutorial|curriculum|notes|article|chapter|"
    r"document|content|text|slides?|video|transcript)\b"
    r"|\bbased on (?:the |this )?(?:reading|lesson|material|passage|tutorial|curriculum|"
    r"article|chapter|notes|video|transcript)\b"
    r"|\bas (?:mentioned|described|discussed|taught|explained|stated|noted|covered|"
    r"presented|shown|seen|introduced) in (?:the |this )?(?:reading|lesson|material|"
    r"passage|session|tutorial|curriculum|article|chapter|notes|video|above|below)\b"
    r"|\b(?:in|from) (?:the |this )(?:reading|lesson|passage|tutorial|curriculum)\b",
    re.I,
)


def _external_ref(lean: dict) -> str | None:
    """The first source-deferring phrase in the stem/options/items, else None."""
    texts = [lean.get("question", ""), lean.get("statement", "")]
    texts += [o.get("content", "") for o in (lean.get("options") or [])]
    texts += list(lean.get("ordered_items") or [])
    for t in texts:
        m = _EXTERNAL_REF_RE.search(t or "")
        if m:
            return m.group(0)
    return None


class _DistractorVerdict(BaseModel):
    evaluable: bool = Field(description="true if a learner could tell WHY this distractor is "
                                        "wrong using ONLY the material, at the required depth")
    reason: str = Field(description="one line: what understanding is needed, and whether the material teaches it")


_DISTRACTOR_DEPTH_AUDIT = register("review.distractor_depth_audit", (
    "You check whether a learner could tell WHY a wrong answer (distractor) is wrong using "
    "ONLY the material. You are given the question's target concept, the DEPTH the learner is "
    "expected to have, the distractor, and the material. Decide whether the concept or "
    "relationship that makes this distractor WRONG is taught to AT LEAST the required depth "
    "(recall = the relevant fact is stated; understand = it is explained with reasoning). A "
    "distractor whose wrongness depends on a concept only mentioned in passing, or on outside "
    "knowledge, is NOT evaluable. Be strict; when unsure, say NOT evaluable."
))


def _audit_distractor_depth(lo: dict, qtype: str, lean: dict, ctx: str) -> list[dict]:
    """For understand+/apply LOs, verify each WRONG option is evaluable from taught material
    at the expected depth (one Bloom level below the LO). One LLM call per distractor; never
    blocks review on its own failure. Recall-level LOs skip (distractors are recognition)."""
    if qtype not in OPTION_TYPES:
        return []
    bloom = (lo.get("bloom_category") or "").lower()
    if bloom not in ("understand", "apply", "implement"):
        return []
    depth = lo.get("expected_distractor_depth") or ("understand" if bloom != "understand" else "remember")
    concept = lo.get("concept") or lo.get("description") or ""
    issues: list[dict] = []
    for o in (lean.get("options") or []):
        if o.get("is_correct"):
            continue
        content = (o.get("content") or "").strip()
        if not content:
            continue
        usr = (f"TARGET CONCEPT: {concept}\nREQUIRED DEPTH to judge a distractor: {depth}\n\n"
               f"DISTRACTOR (a wrong option): {content}\n\nMATERIAL (ground truth):\n{(ctx or '')[:8000]}")
        try:
            v = _model(0).with_structured_output(_DistractorVerdict).invoke(
                [{"role": "system", "content": get_prompt("review.distractor_depth_audit", _DISTRACTOR_DEPTH_AUDIT)},
                 {"role": "user", "content": usr}])
        except Exception:  # noqa: BLE001 — never block review on the audit
            continue
        if not v.evaluable:
            issues.append({
                "rule": "DISTRACTOR DEPTH",
                "problem": f"a learner cannot tell why the option {content!r} is wrong from the material "
                           f"(needs {depth}-depth understanding the material doesn't give): {v.reason}",
                "severity": "high",
                "suggested_fix": "rebuild this distractor so its wrongness rests on a concept the material "
                                 "teaches to the required depth, or make it a recognition-level wrong option "
                                 "that needs no untaught reasoning",
            })
    return issues


def _review_lean(lo: dict, qtype: str, lean: dict, ctx: str) -> dict:
    user = (
        f"LEARNING OUTCOME (intent):\n{_lo_block(lo)}\n\n"
        f"COURSE MATERIAL (ground truth):\n{ctx}\n\n"
        f"GENERATED {qtype} QUESTION (review this):\n{lean}"
    )
    review: QuestionReview = _model(0).with_structured_output(QuestionReview).invoke(
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

    # (d) distractor depth — for understand+/apply LOs, every WRONG option must be evaluable
    # from taught material (the concept that makes it wrong is taught to the required depth).
    dd_issues = _audit_distractor_depth(lo, qtype, lean, ctx)
    if dd_issues:
        issues.extend(dd_issues)
        out["passed"] = False

    out["issues"] = issues
    return out


def review_and_fix_one(lo: dict, gen: dict, *, max_seq: int | None = None,
                       max_retries: int = 2) -> dict:
    """Review a generated question and, while it fails, apply a TARGETED fix and
    re-review — up to `max_retries` times. Keeps every verdict in `review_history`.
    Records the RAG calls made during review/fix under `review_rag_calls` (the
    generation-time `rag_calls` carried in `gen` are preserved alongside)."""
    with scope.recording() as rag_calls:
        res = _review_and_fix_one(lo, gen, max_seq=max_seq, max_retries=max_retries)
    res["review_rag_calls"] = rag_calls
    return res


def _review_and_fix_one(lo: dict, gen: dict, *, max_seq: int | None = None,
                        max_retries: int = 2) -> dict:
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
                           max_seq: int | None = None, max_retries: int = 2,
                           workers: int = 8, on_progress=None) -> list[dict]:
    """Review-and-fix each generated question against its LO, concurrently."""
    def _one(pair):
        res = review_and_fix_one(pair[0], pair[1], max_seq=max_seq, max_retries=max_retries)
        if on_progress:
            on_progress(needs_human=bool(res.get("needs_human")))
        return res
    return pmap(_one, list(_pair(los, questions)), workers=workers)
