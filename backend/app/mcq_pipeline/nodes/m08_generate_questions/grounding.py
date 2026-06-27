"""Question pipeline · Node 8 — generate_questions · grounding.

Course-domain detection, the grounding-context builder, RAG code-coverage probe, the
creation-layer grounding gate (stem/option named-term check with an in-place fix loop),
and the MCQ fallback for code questions that lean on untaught constructs.

`fix_lean` (node.py) and the per-type agents (agents.py) are imported LAZILY inside the
functions that need them, to avoid an import cycle (node/agents → grounding → …).
"""
from __future__ import annotations

import re

from app.mcq_pipeline.utils import rag_api, scope
from app.mcq_pipeline.nodes.m08_generate_questions.prompts import CODE_PATH_TYPES


def _course_is_sql() -> bool:
    """True when the CURRENT run's course is configured as a SQL course.

    Deterministic and course-level: the value comes from Course.question_domain
    (set per course), carried on the run-scoped RagAdapter as `domain`. There is no
    per-outcome guessing — a course is SQL or it is not. Returns False when no adapter
    is bound (e.g. unit tests) so callers can use it unconditionally."""
    try:
        return (getattr(scope.get_adapter(), "domain", "") or "").upper() == "SQL"
    except Exception:  # noqa: BLE001 — no adapter bound -> not a SQL run
        return False


def _ground(lo: dict, max_seq: int | None) -> str:
    """Grounding context for generation/review.

    Leads with the PRIMARY span the outcome was grounded in (its evidence quote + the
    section it was drawn from) — the question must be answerable from THAT. Then the
    FULL current-session reading material as supporting BACKGROUND (so the model never
    declares a concept 'not in the material' just because a retrieved slice missed it),
    plus RAG-retrieved related/prior sections for cross-session context.
    """
    query = " ".join(p for p in (lo.get("concept"), lo.get("sub_concept"), lo.get("syntax")) if p)
    secs = rag_api.search_reading_material(query or lo.get("outcome", ""), top_k=6)
    if max_seq is not None:
        secs = [s for s in secs if s.get("seq", 0) <= max_seq]
    related = "\n\n".join(f"[{s.get('seq')} {s.get('unit_name')} > {s.get('section')}]\n{s.get('snippet')}"
                          for s in secs)
    try:
        session_text = (scope.get_adapter().reading_material or "").strip()
    except Exception:  # noqa: BLE001 — no adapter bound (e.g. unit test); fall back to RAG only
        session_text = ""
    # PRIMARY anchor — the exact span this outcome was grounded in. source_evidence is a
    # quote string on the legacy LO (a {quote,section} dict on the new outcome); the
    # section text rides along via source_section_text (added at the bridge).
    se = lo.get("source_evidence")
    ev = se.strip() if isinstance(se, str) else ((se or {}).get("quote", "").strip() if isinstance(se, dict) else "")
    sec = (lo.get("source_section_text") or "").strip()
    parts = []
    primary = []
    if ev:
        primary.append('EVIDENCE — the exact text this outcome is grounded in; the question '
                       f'MUST be answerable from this:\n"{ev}"')
    if sec:
        primary.append("SECTION the outcome was drawn from:\n" + sec)
    if primary:
        parts.append("PRIMARY GROUNDING (anchor the question here):\n" + "\n\n".join(primary))
    if session_text:
        parts.append("BACKGROUND — full current-session reading material (supporting context only):\n"
                     + session_text)
    if related:
        parts.append("RELATED / PRIOR COURSE MATERIAL (retrieved, background):\n" + related)
    return "\n\n---\n\n".join(parts) or "(no matching course material found)"


def _lo_block(lo: dict) -> str:
    keys = ("outcome", "concept", "sub_concept", "description", "learner_action", "syntax", "source_evidence")
    return "\n".join(f"{k}: {lo.get(k)}" for k in keys if lo.get(k))


def _coverage(lo: dict, qtype: str, max_seq: int | None):
    if qtype not in CODE_PATH_TYPES:
        return True, None
    # FIB needs an explicit, taught blank syntax. With no LO syntax the concept-only
    # probe rubber-stamps any taught concept, so FIB invents shell syntax. Force the
    # existing MCQ fallback instead.
    if qtype == "FIB_CODING" and not (lo.get("syntax") or "").strip():
        return False, {"covered": False,
                       "reason": "FIB requires explicit blank syntax taught in the session"}
    cov = lo.get("code_coverage") or rag_api.code_coverage(
        lo.get("concept") or lo.get("outcome", ""), syntax=lo.get("syntax") or None, max_seq=max_seq)
    return bool(cov.get("covered")), cov


# --- creation-layer grounding gate ----------------------------------------- #
# Enforce, AT CREATION, that the stem and EVERY option use only concepts/keywords
# taught in this session or an earlier one (RAG check_concept is scoped to the
# course + prerequisite courses). Ungrounded named terms trigger an in-place
# regeneration here, so the question reaches review already grounded.
MAX_GROUNDING_FIXES = 2
_GROUNDING_TERM_CAP = 14

_GT_BACKTICK = re.compile(r"`([^`]+)`")
_GT_DOTTED = re.compile(r"\b[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+\b")     # obj.method / mod.attr
_GT_CALL = re.compile(r"\b[A-Za-z_]\w*\(\)")                        # func()
_GT_VERSIONED = re.compile(r"\b[A-Za-z][\w-]*==?\d[\w.]*\b")        # django==3.0
_GT_PROPER = re.compile(r"[A-Z][A-Za-z]{2,}")                       # Django, Flask
# Capitalized words that are ordinary prose / boolean / IO — never "named entities".
_GT_PROPER_STOP = {
    "The", "This", "That", "These", "Those", "There", "Their", "They", "Then", "Than",
    "True", "False", "None", "Null", "Yes", "Not", "Output", "Outputs", "Input", "Inputs",
    "Error", "Errors", "Value", "Values", "Result", "Results", "Code", "Program",
    "Function", "Functions", "Variable", "Variables", "List", "Lists", "String", "Strings",
    "Number", "Numbers", "Object", "Objects", "Class", "Classes", "Method", "Methods",
    "Loop", "Loops", "When", "Where", "Which", "What", "Why", "How", "Who", "Whose",
    "Its", "Else", "For", "While", "And", "But", "Both", "Each", "Every", "All", "Any",
    "Some", "Use", "Used", "Using", "Return", "Returns", "Print", "Prints", "Allow",
    "Allows", "Ensure", "Ensures", "Avoid", "Avoids", "Reduce", "Reduces", "Create",
    "Creates", "Add", "Adds", "Remove", "Removes", "First", "Second", "Third", "Last",
    "Next", "Previous", "Same", "Different", "Correct", "Incorrect", "Only", "Also",
}


def _named_candidates(text: str) -> set[str]:
    """Named entities a learner would need taught BY NAME — code identifiers, dotted
    calls, versioned tokens, and non-prose proper nouns. Deliberately conservative
    (matches the review-layer policy) so paraphrased prose isn't false-flagged."""
    cands: set[str] = set()
    for span in _GT_BACKTICK.findall(text):
        cands.update(re.findall(r"[A-Za-z_]\w*(?:\.\w+)?(?:\(\))?", span))
    cands.update(_GT_DOTTED.findall(text))
    cands.update(_GT_CALL.findall(text))
    cands.update(_GT_VERSIONED.findall(text))
    for seg in re.split(r"[.\n;:!?]", text):                       # skip sentence-initial caps
        for w in re.findall(r"[A-Za-z][A-Za-z0-9+#-]*", seg)[1:]:
            if _GT_PROPER.fullmatch(w) and w not in _GT_PROPER_STOP:
                cands.add(w)
    return {c.strip() for c in cands if len(c.strip()) >= 2}


def _lean_texts(lean: dict) -> str:
    """All learner-facing text whose terms must be taught — stem + options + answers
    + explanation (NOT the code body; its syntax is gated separately via
    code_coverage / used_syntax / _enforce_code_grounding)."""
    parts = [lean.get("question", ""), lean.get("statement", ""), lean.get("explanation", "")]
    parts += [o.get("content", "") for o in (lean.get("options") or [])]
    parts += list(lean.get("wrong_answers") or [])
    parts += list(lean.get("correct_outputs") or [])
    parts += [lean.get(k, "") for k in ("correct_output", "answer", "expected_output", "blank_answer")]
    parts += list(lean.get("ordered_items") or [])
    return "\n".join(p for p in parts if p)


def _ungrounded_named(text: str) -> list[str]:
    seen: set[str] = set()
    uncovered: list[str] = []
    for term in _named_candidates(text):
        key = term.lower()
        if key in seen:
            continue
        seen.add(key)
        if len(seen) > _GROUNDING_TERM_CAP:
            break
        verdict = (rag_api.check_concept(term).get("verdict") or "").split("\n", 1)[0].upper()
        if "NOT EXPLAINED" in verdict:
            uncovered.append(term)
    return uncovered


def _enforce_grounding(lo: dict, res: dict, max_seq: int | None) -> dict:
    from app.mcq_pipeline.nodes.m08_generate_questions.node import fix_lean
    qtype = res["question_type"]
    fix_lo = {**lo, "question_type": qtype}
    ctx = _ground(fix_lo, max_seq)
    lean = res["lean"]
    uncovered = _ungrounded_named(_lean_texts(lean))
    attempts = 0
    while uncovered and attempts < MAX_GROUNDING_FIXES:
        issues = [{
            "severity": "high", "rule": "GROUNDING",
            "problem": (f"the stem/options use terms not taught in this or any earlier "
                        f"session: {', '.join(uncovered)} — a learner can only judge an "
                        f"option using concepts they were actually taught"),
            "suggested_fix": (f"reframe the question and ALL options using ONLY concepts "
                              f"taught in the material; remove {', '.join(uncovered)} and "
                              f"introduce no other untaught named term"),
        }]
        lean = fix_lean(fix_lo, ctx, lean, issues)
        attempts += 1
        uncovered = _ungrounded_named(_lean_texts(lean))
    res["lean"] = lean
    res["grounding"] = {**(res.get("grounding") or {}),
                        "creation_fix_attempts": attempts,
                        "remaining_uncovered_terms": uncovered}
    return res


def _fallback_to_mcq(lo: dict, res: dict, max_seq: int | None, reason: str) -> dict:
    """Convert a code question into a grounded MCQ (the proven-clean path)."""
    from app.mcq_pipeline.nodes.m08_generate_questions.agents import _AGENTS, _FALLBACK_TYPE
    fb = {"from": res["question_type"], "reason": reason}
    fb_lo = {**lo, "question_type": _FALLBACK_TYPE}
    lean2 = _AGENTS[_FALLBACK_TYPE](fb_lo, _ground(fb_lo, max_seq)).model_dump()
    res = {**res, "question_type": _FALLBACK_TYPE, "lean": lean2, "fallback": fb}
    return _enforce_grounding(lo, res, max_seq)   # re-ground the new MCQ


def _enforce_code_grounding(lo: dict, res: dict, max_seq: int | None) -> dict:
    """Verify the EMITTED code body uses only taught NAMED constructs; if not, fall
    back to a grounded MCQ instead of shipping invented/untaught syntax. (FIB blanks
    are NOT format-checked here — FIB is graded by execution, so any completion that
    produces the expected output is valid; see _verify_fib.)"""
    lean = res["lean"]
    code = lean.get("code") or "\n".join(lean.get("code_lines") or [])
    uncovered = _ungrounded_named(code) if code.strip() else []
    if not uncovered:
        return res
    return _fallback_to_mcq(lo, res, max_seq, f"code uses untaught constructs: {', '.join(uncovered)}")
