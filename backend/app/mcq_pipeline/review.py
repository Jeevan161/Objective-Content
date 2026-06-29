"""
app/mcq_pipeline/review.py
--------------------------
Human-in-the-loop review (Gate B): record reviewer feedback, regenerate a single
question with that feedback injected, and approve a run. Decoupled from the main
LangGraph run — a regeneration is a targeted call over the question's stored LO, so
no graph run is held open across human latency.

Heavy imports (langgraph / agents) are deferred into the functions so importing
this module from the API stays cheap.
"""

from __future__ import annotations

import re

from app.db.session import SessionLocal
from app.models import McqQuestionFeedback, McqRun

from app.mcq_pipeline.prompts.store import get_prompt, register

# Reviewer phrasings -> canonical question type, MOST-SPECIFIC first so
# 'more than one multiple choice' / 'code analysis ...' win over bare 'multiple choice'.
_QTYPE_ALIASES = [
    ("CODE_ANALYSIS_MORE_THAN_ONE_MULTIPLE_CHOICE",
        ("code analysis multiple correct", "code multiple correct", "code analysis more than one")),
    ("CODE_ANALYSIS_TEXTUAL",
        ("code analysis textual", "type the output", "output textual", "code textual")),
    ("CODE_ANALYSIS_MULTIPLE_CHOICE",
        ("code analysis", "predict the output", "output prediction", "analyse the code",
         "analyze the code", "what is the output", "code mcq", "code-based mcq")),
    ("MORE_THAN_ONE_MULTIPLE_CHOICE",
        ("more than one multiple choice", "more than one correct", "multiple correct",
         "select all", "multi-select", "multi select", "multiple answers", "msq", "multi-correct")),
    ("FIB_CODING",
        ("fill in the blank", "fill-in-the-blank", "fill the blank", "fib", "complete the code",
         "code completion", "blank in the code")),
    ("TRUE_OR_FALSE",
        ("true or false", "true/false", "true false", "t/f", "boolean")),
    ("REARRANGE",
        ("rearrange", "re-arrange", "reorder", "re-order", "order the steps", "arrange the steps",
         "ordering", "sequence the steps")),
    ("TEXTUAL",
        ("textual", "short answer", "short-answer", "one word answer", "one-word answer",
         "type the answer", "free text", "free-text")),
    ("MULTIPLE_CHOICE",
        ("multiple choice", "mcq", "single best answer", "best answer", "single correct",
         "one correct option", "single-select")),
]
_CHANGE_CUE = re.compile(
    r"\b(change|convert|turn|switch|make|recast|reframe|rephrase|instead|rather than|"
    r"should be|wrong type|different type|better as|present (?:it|this) as|ask (?:it|this) as)\b",
    re.I)


def _alias_in(alias: str, text: str) -> bool:
    # non-word boundaries so 'fib' doesn't match inside 'fibre' but 't/f' / 'mcq' still match.
    return re.search(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", text) is not None


def _requested_question_type(feedback: str, current: str) -> str | None:
    """If the reviewer explicitly asked to CHANGE the question type, return the target
    canonical type (or 'REPICK' if they asked for a change but named none); else None.
    A passing mention ('the MCQ options are off') or a same-type reference never
    triggers a switch."""
    t = (feedback or "").strip().lower()
    if not t:
        return None
    has_cue = bool(_CHANGE_CUE.search(t))
    # (a) explicit named-target switch: a change cue + a named type that differs.
    if has_cue:
        for canonical, aliases in _QTYPE_ALIASES:
            if any(_alias_in(a, t) for a in aliases):
                return canonical if canonical != current else None
    # (b) change wanted but no type named -> re-pick. Require an explicit 'question type'
    # phrase (NOT bare 'type', which collides with data type / type annotation / type
    # error) plus a change cue OR a direct criticism of the type.
    qtype_phrase = re.search(
        r"\b(?:question\s+type|type\s+of\s+(?:question|item)|kind\s+of\s+(?:question|item)|"
        r"qtype|question\s+format)\b", t)
    crit = re.search(
        r"\b(wrong|incorrect|not\s+right|unsuitable|inappropriate|doesn'?t\s+fit|"
        r"not\s+suitable|not\s+appropriate|better|different|pick|choose)\b", t)
    if qtype_phrase and (has_cue or crit):
        return "REPICK"
    return None


# LLM intent classifier — catches type-change requests phrased WITHOUT explicit
# keywords/aliases (e.g. "asking them to write code is overkill, just have them
# recognize the answer"). DB-editable prompt.
_TYPE_CHANGE_SYS = register("review.type_change_sys", (
    "You read a reviewer's feedback on an assessment question and decide ONE thing: is "
    "the reviewer asking to change the question to a DIFFERENT question TYPE/format?\n"
    "The formats are: MULTIPLE_CHOICE, TRUE_OR_FALSE, MORE_THAN_ONE_MULTIPLE_CHOICE, "
    "TEXTUAL, FIB_CODING (fill-in-the-blank code), CODE_ANALYSIS_MULTIPLE_CHOICE, "
    "CODE_ANALYSIS_TEXTUAL, CODE_ANALYSIS_MORE_THAN_ONE_MULTIPLE_CHOICE, REARRANGE.\n"
    "Say YES only when the reviewer wants a different FORMAT — e.g. 'make this multiple "
    "choice', 'this should be true/false', 'asking them to write code is overkill, just "
    "have them recognize the answer', 'this is really an ordering task'. Say NO for "
    "requests to fix wording, options, distractors, difficulty, grounding, or any "
    "content WITHIN the current type. Be conservative: if it is not clearly a format "
    "change, say NO.\n"
    'Return ONLY JSON: {"wants_type_change": <bool>, "target_type": "<one of the 9 type '
    'names if a specific one is requested, else empty string>"}.'
))


def _llm_type_change(feedback: str, current: str, question_text: str) -> str | None:
    """Returns a canonical type, 'REPICK' (change wanted, type unspecified), None (no
    change), or '__ERROR__' (LLM unavailable -> caller falls back to keywords)."""
    from app.mcq_pipeline.utils.llm import chat, parse_json
    from app.mcq_pipeline.nodes.m07_recommend_question_type import QUESTION_TYPES
    try:
        usr = (f"CURRENT QUESTION TYPE: {current}\n\nQUESTION:\n{question_text}\n\n"
               f"REVIEWER FEEDBACK:\n{feedback}")
        data = parse_json(chat(
            [{"role": "system", "content": get_prompt("review.type_change_sys", _TYPE_CHANGE_SYS)},
             {"role": "user", "content": usr}], temperature=0)) or {}
    except Exception:  # noqa: BLE001 — never block regeneration on the classifier
        return "__ERROR__"
    if not data.get("wants_type_change"):
        return None
    tt = str(data.get("target_type") or "").strip().upper()
    if tt in set(QUESTION_TYPES) and tt != current:
        return tt
    return "REPICK"


def _detect_type_change(feedback: str, current: str, question_text: str = "") -> str | None:
    """Decide the reviewer's requested question type. LLM-PRIMARY (robust to phrasing),
    with the deterministic keyword detector as a fallback when the LLM is unavailable and
    as a tie-breaker for the specific type when the LLM is vague. Returns a canonical
    type, 'REPICK', or None."""
    kw = _requested_question_type(feedback, current)
    llm = _llm_type_change(feedback, current, question_text)
    if llm == "__ERROR__":
        return kw                                    # graceful degradation -> keywords
    specific = next((x for x in (llm, kw) if x and x != "REPICK"), None)
    if specific:
        return specific
    return "REPICK" if (llm == "REPICK" or kw == "REPICK") else None


def _alternative_type(current: str, lo: dict) -> str:
    """A sensible DIFFERENT type for a 'change the type' regen when the recommender would
    otherwise re-derive the same one. Keeps code outcomes in the code family; never returns
    the current type or a disabled (exact-string-match) type."""
    from app.mcq_pipeline.config import EXCLUDED_QUESTION_TYPES
    has_code = bool((lo.get("syntax") or "").strip()) or (current or "").startswith("CODE_ANALYSIS")
    order = (["CODE_ANALYSIS_MULTIPLE_CHOICE", "CODE_ANALYSIS_MORE_THAN_ONE_MULTIPLE_CHOICE", "MULTIPLE_CHOICE"]
             if has_code else
             ["MULTIPLE_CHOICE", "MORE_THAN_ONE_MULTIPLE_CHOICE", "TRUE_OR_FALSE"])
    for t in order:
        if t != current and t not in EXCLUDED_QUESTION_TYPES:
            return t
    return "MULTIPLE_CHOICE"


# --- feedback INTENT classification (routes regeneration to a targeted fix) --- #
# Derived from real reviewer comments in RDS (mcq_question_feedback): "Wrong Options",
# "Options not aligned", "refers to external resources", "Use Markdown", "AI Generated
# strokes", "Option B is similar to the correct one", "not mandatory to know" (scope).
_FEEDBACK_INTENTS = {
    "wrong_options", "wrong_answer", "multi_valid", "not_self_contained",
    "formatting", "lo_misaligned", "new_question", "other",
}

_FEEDBACK_INTENT_SYS = register("review.feedback_intent_sys", (
    "You triage a reviewer's feedback on a generated assessment question into ONE intent, so the "
    "system can apply a TARGETED fix instead of blindly regenerating. Choose the single best intent:\n"
    "- wrong_options: distractors/options are wrong, weak, implausible, give the answer away, or are "
    "negations/opposites of the key.\n"
    "- wrong_answer: the marked correct answer (the key) is itself incorrect.\n"
    "- multi_valid: more than one option is defensibly correct, options are too similar, or the "
    "'correct' choice is subjective.\n"
    "- not_self_contained: the question or its answer depends on the source/reading or on context not "
    "stated IN the question.\n"
    "- formatting: presentation only — use Markdown, code/backtick formatting, AI-looking dashes or "
    "typography, wording style.\n"
    "- lo_misaligned: the question tests the wrong thing / is out of scope / 'should not be asked' / the "
    "learning outcome itself is unsuitable.\n"
    "- new_question: the reviewer wants a completely different/new question on the same outcome.\n"
    "- other: anything else, or unclear.\n"
    'Return ONLY JSON: {"intent": "<one of the names above>"}.'
))


def _keyword_intent(feedback: str) -> str:
    """Deterministic fallback intent when the LLM classifier is unavailable."""
    t = (feedback or "").lower()
    if re.search(r"\b(self[- ]?contain|external|refers? to|reading|session summary|not specified|context)\b", t):
        return "not_self_contained"
    if re.search(r"\b(markdown|format|backtick|strokes?|dashes?|em[- ]?dash|typo|styl|ai[\s-]?generat)\b", t):
        return "formatting"
    if re.search(r"\b(subjective|also (correct|valid)|too similar|similar to the correct|ambiguous|more than one|both correct)\b", t):
        return "multi_valid"
    if re.search(r"\b(wrong answers?|correct answer|answer is (wrong|incorrect)|answer key|key is wrong)\b", t):
        return "wrong_answer"
    if re.search(r"\b(wrong option|wrong options|distractor|options are|not aligned)\b", t):
        return "wrong_options"
    if re.search(r"\b(out of scope|not mandatory|cannot be asked|can'?t be asked|should not be asked|not relevant|trivia)\b", t):
        return "lo_misaligned"
    if re.search(r"\b(new question|different question|regenerate|start over|completely)\b", t):
        return "new_question"
    return "other"


def _classify_feedback(feedback: str, current: str, question_text: str) -> str:
    """LLM-primary intent classification with a keyword fallback. Returns one of
    _FEEDBACK_INTENTS."""
    from app.mcq_pipeline.utils.llm import chat, parse_json
    try:
        usr = (f"CURRENT QUESTION TYPE: {current}\n\nQUESTION:\n{question_text}\n\n"
               f"REVIEWER FEEDBACK:\n{feedback}")
        data = parse_json(chat(
            [{"role": "system", "content": get_prompt("review.feedback_intent_sys", _FEEDBACK_INTENT_SYS)},
             {"role": "user", "content": usr}], temperature=0)) or {}
        it = str(data.get("intent") or "").strip().lower()
        return it if it in _FEEDBACK_INTENTS else _keyword_intent(feedback)
    except Exception:  # noqa: BLE001 — never block regeneration on the classifier
        return _keyword_intent(feedback)


# Per-intent sharpened fix instruction fed to fix_lean (alongside the raw reviewer text).
_INTENT_FIX = {
    "wrong_options": ("OPTION RULES",
        "Rebuild the distractors: each must be a specific, plausible, TAUGHT misconception of the "
        "SAME polarity and scope as the correct answer — no negations/opposites/blanket denials, no "
        "giveaway absolutes, and every option must directly answer the exact question asked."),
    "wrong_answer": ("ANSWER KEY",
        "The marked correct answer is wrong. Mark the genuinely correct option per the COURSE MATERIAL "
        "and keep EXACTLY one correct; do not change the question's meaning."),
    "multi_valid": ("OPTION RULES",
        "More than one option is defensibly correct because the outcome is SET-VALUED (several items "
        "are genuinely true). Resolve it by ANSWER DETERMINACY, qualifier-first: (1) add a "
        "DISCRIMINATING QUALIFIER to the stem (a specific facet/scenario/sub-aspect) so EXACTLY ONE "
        "option is correct and the rest become genuinely wrong for that stem; (2) only if no honest "
        "qualifier can isolate a single answer, make this a MORE_THAN_ONE_MULTIPLE_CHOICE — keep 4-6 "
        "options, mark EVERY genuinely-true option correct and at least one genuinely-FALSE option. "
        "Do NOT down-rank a true item to a distractor just to keep one correct."),
    "not_self_contained": ("SELF-CONTAINMENT",
        "Make the question stand alone: remove any reference to the source/reading/session, and move "
        "EVERY detail the answer depends on INTO the stem. The answer must be fully determined by the "
        "stem the learner sees."),
    "formatting": ("MARKDOWN FORMATTING",
        "Fix presentation only (do NOT change meaning, options, or the key): clean Markdown with "
        "`backticks` for code/terms in the stem AND options (content_type MARKDOWN where used), and "
        "plain hyphens/straight quotes — remove any en/em dashes or AI-looking typography."),
}


def _intent_issue(intent: str, feedback: str) -> dict:
    """Build the high-severity fix issue for fix_lean from the classified intent."""
    rule, sharp = _INTENT_FIX.get(intent, ("HUMAN FEEDBACK", None))
    return {
        "severity": "high", "rule": rule,
        "problem": f"Reviewer feedback: {feedback}",
        "suggested_fix": (f"{sharp} (reviewer said: {feedback})" if sharp else feedback),
    }


def _pick_reserve_lo(result: dict, current_lo: dict) -> dict | None:
    """For a 'change the LO' request with no suggestion: pick a previously-DROPPED
    (reserve) outcome that is NOT already used in this run and is not a near-duplicate
    of an existing one. Returns the reserve LO dict, or None if the run has no usable
    reserve pool (older runs predate reserve persistence -> caller falls back)."""
    reserve = result.get("reserve_los") or []
    if not reserve:
        return None
    used_outcomes = {(lo.get("outcome") or "").strip().lower()
                     for lo in (result.get("final_los") or [])}
    used_concepts = {(lo.get("concept") or "").strip().lower()
                     for lo in (result.get("final_los") or [])}
    cur_concept = (current_lo.get("concept") or "").strip().lower()
    # Prefer a reserve on a DIFFERENT concept than the rejected one; never reuse an
    # outcome already in the run, and avoid an outcome whose concept is already covered.
    def _ok(r, *, allow_same_concept):
        o = (r.get("outcome") or "").strip().lower()
        c = (r.get("concept") or "").strip().lower()
        if not o or o in used_outcomes:
            return False
        if not allow_same_concept and (c in used_concepts or c == cur_concept):
            return False
        return True
    for allow in (False, True):   # first try a fresh concept, then any unused outcome
        for r in reserve:
            if _ok(r, allow_same_concept=allow):
                return r
    return None


def _apply_lo_swap(res: dict, from_outcome: str, reserve_lo: dict) -> None:
    """Swap the rejected outcome out of the run's `final_los` and the chosen reserve LO
    in (and drop it from `reserve_los` so it can't be picked twice). Mutates `res`."""
    to_outcome = reserve_lo.get("outcome")
    new_fl, replaced = [], False
    for lo in (res.get("final_los") or []):
        if lo.get("outcome") == from_outcome:
            new_fl.append(reserve_lo); replaced = True
        elif lo.get("outcome") == to_outcome:
            continue                         # drop any pre-existing copy of the reserve
        else:
            new_fl.append(lo)
    if not replaced and all(l.get("outcome") != to_outcome for l in new_fl):
        new_fl.append(reserve_lo)
    res["final_los"] = new_fl
    res["reserve_los"] = [r for r in (res.get("reserve_los") or [])
                          if r.get("outcome") != to_outcome]


def _find_lo(result: dict, outcome: str) -> dict | None:
    return next((lo for lo in (result.get("final_los") or []) if lo.get("outcome") == outcome), None)


def _find_q_index(result: dict, outcome: str) -> int:
    for i, q in enumerate(result.get("questions") or []):
        if q.get("outcome") == outcome:
            return i
    return -1


def _qtype_for(result: dict, outcome: str) -> str:
    i = _find_q_index(result, outcome)
    return (result.get("questions") or [{}])[i].get("question_type", "") if i >= 0 else ""


def _eligible(qs: list) -> list:
    """Questions that count toward approval — the ones actually generated, minus any a
    reviewer has excluded (excluded questions stay in the list but are not loaded)."""
    return [q for q in qs if q.get("status") == "generated" and not q.get("excluded")]


def _approved_count(qs: list) -> int:
    return sum(1 for q in _eligible(qs) if q.get("approval") == "approved")


def regenerate_question(run_id, outcome: str, feedback: str, *,
                        reviewer: str = "", tags: list | None = None) -> dict:
    """Regenerate the question for `outcome`, injecting the human feedback as a
    top-priority instruction; re-review; persist (with a revision + feedback row).
    Returns the new question dict."""
    from app.mcq_pipeline.utils import scope
    from app.mcq_pipeline.nodes.m08_generate_questions import _ground, difficulty_of, fix_lean, generate_lean
    from app.mcq_pipeline.nodes.m08_generate_questions.enforce import _is_multi_correct_shape
    from app.mcq_pipeline.nodes.m09_review_questions import review_and_fix_one
    from app.mcq_pipeline.nodes.m07_recommend_question_type import recommend_one
    from app.mcq_pipeline.runner import build_adapter

    # 1) load what we need, then release the session before the LLM work
    with SessionLocal() as s:
        run = s.get(McqRun, run_id)
        if run is None:
            raise ValueError("MCQ run not found.")
        course_id, unit_id = run.course_id, run.unit_id
        result = dict(run.result or {})

    lo = _find_lo(result, outcome)
    idx = _find_q_index(result, outcome)
    if lo is None or idx < 0:
        raise ValueError(f"No question found for outcome {outcome!r}.")
    old_q = result["questions"][idx]
    current_qtype = old_q.get("question_type") or lo.get("question_type")

    # 2) regenerate (grounded on the run's course scope) with feedback injected
    adapter, _pu, _label = build_adapter(course_id, unit_id, None)
    scope.set_adapter(adapter)
    # Populate the proxy metadata's required `unit` field for this single-question run.
    from app.mcq_pipeline.utils.llm import set_call_context
    set_call_context(unit=(_label or unit_id or str(run_id)), step="regenerate")

    # Route by reviewer INTENT (taxonomy derived from real RDS feedback). A TYPE CHANGE
    # regenerates AS the new type. LO-MISALIGNED regenerates + flags for a human (and will
    # swap in a reserve outcome once the run carries one). EVERY OTHER intent gets a
    # TARGETED fix of the EXISTING question — preserve what works, fix only the flagged
    # aspect — instead of a blind from-scratch regen that reintroduces the same flaw
    # ("regenerated but nothing really changed"). Type detection stays LLM-primary.
    _lean = old_q.get("lean") or {}
    question_text = (_lean.get("question") or _lean.get("statement") or lo.get("description") or "")[:1200]

    target = _detect_type_change(feedback, current_qtype, question_text)
    if target == "REPICK":
        # The reviewer asked to CHANGE the type but named none. recommend_one() re-derives
        # the SAME type deterministically for this LO, so a bare re-pick would no-op (the
        # reviewer's "change the type" would silently do nothing). Force a DIFFERENT,
        # non-excluded type when the re-pick lands back on the rejected current type.
        repick = (recommend_one(lo) or {}).get("question_type")
        target = repick if (repick and repick != current_qtype) else _alternative_type(current_qtype, lo)
    intent = None if target else _classify_feedback(feedback, current_qtype, question_text)

    new_qtype = target or current_qtype
    alignment_note = None
    # When the OUTCOME itself is rejected and a reserve (dropped) outcome is available,
    # swap that reserve LO into this slot instead of re-asking the misaligned outcome.
    swap_lo = _pick_reserve_lo(result, lo) if intent == "lo_misaligned" else None

    if swap_lo is not None:
        gen_lo = {**swap_lo, "question_type": new_qtype}
        ctx = _ground(gen_lo, None)
        gen = generate_lean(gen_lo)
        if gen.get("status") == "generated" and gen.get("lean"):
            gen["lean"] = fix_lean(gen_lo, ctx, gen["lean"], [_intent_issue("other", feedback)])
        alignment_note = (f"Outcome swapped (the original was flagged as misaligned/out-of-scope): "
                          f"'{outcome}' -> '{swap_lo.get('outcome')}'. Please review the new question.")
    elif (not target) and _lean and intent not in ("new_question", "lo_misaligned"):
        # surgical fix of the existing question (content / format / alignment intents)
        gen_lo = {**lo, "question_type": new_qtype}
        ctx = _ground(gen_lo, None)
        new_lean = fix_lean(gen_lo, ctx, _lean, [_intent_issue(intent or "other", feedback)])
        gen = {"status": "generated", "question_type": current_qtype,
               "difficulty": difficulty_of(lo), "lean": new_lean}
    else:
        # type change, "make a new one", no prior lean, or LO-misaligned with no reserve -> fresh gen
        gen_lo = {**lo, "question_type": new_qtype}
        ctx = _ground(gen_lo, None)
        gen = generate_lean(gen_lo)
        if gen.get("status") == "generated" and gen.get("lean"):
            gen["lean"] = fix_lean(gen_lo, ctx, gen["lean"], [_intent_issue(intent or "other", feedback)])
        if intent == "lo_misaligned":
            alignment_note = ("Reviewer flagged the learning outcome as misaligned/out-of-scope, but no "
                              "reserve (dropped) outcome is stored for this run; regenerated against the "
                              "same outcome and flagged for your decision.")

    # Honor a set-valued escalation: when qualifier-first still yields a valid multi-correct set
    # (e.g. the "this is also correct" intent on an irreducibly set-valued outcome), accept it as
    # MORE_THAN_ONE_MULTIPLE_CHOICE rather than leaving a forced single "correct" among co-true items.
    if gen.get("question_type") == "MULTIPLE_CHOICE" and _is_multi_correct_shape(gen.get("lean") or {}):
        gen["question_type"] = "MORE_THAN_ONE_MULTIPLE_CHOICE"

    # the LO this slot now belongs to (the reserve when swapped) and its outcome key.
    effective_lo = swap_lo or lo
    gen["outcome"] = effective_lo.get("outcome") if swap_lo else outcome
    new_q = review_and_fix_one(effective_lo, gen)
    if swap_lo is not None:
        new_q["lo_swap"] = {"from_outcome": outcome, "to_outcome": swap_lo.get("outcome"),
                            "from_concept": lo.get("concept"), "to_concept": swap_lo.get("concept")}
    if alignment_note:
        new_q["lo_alignment_note"] = alignment_note
        new_q["needs_human"] = True          # a swapped/flagged outcome needs a fresh human look
    # generate_lean may itself fall back (e.g. an ungroundable code type -> MCQ), so the
    # actual type is whatever was produced.
    actual_qtype = new_q.get("question_type") or new_qtype
    if actual_qtype != current_qtype:
        new_q["type_change"] = {"from": current_qtype, "to": actual_qtype, "requested": new_qtype}
        effective_lo["question_type"] = actual_qtype   # keep the run-result LO object in sync

    # 3) carry forward revision + feedback history on the question
    prev = {k: v for k, v in old_q.items() if k != "revisions"}
    new_q["revisions"] = (old_q.get("revisions") or []) + [prev]
    new_q["human_feedback"] = (old_q.get("human_feedback") or []) + [
        {"feedback": feedback, "tags": tags or [], "reviewer": reviewer}]
    result["questions"][idx] = new_q
    if swap_lo is not None:
        _apply_lo_swap(result, outcome, swap_lo)

    # 4) persist + record the feedback row. Merge THIS question into the FRESHLY-LOCKED row
    #    (not a blind overwrite of the whole result snapshot loaded ~10s ago) so a concurrent
    #    regeneration of ANOTHER question in the same run isn't lost. SELECT ... FOR UPDATE
    #    serializes the read-modify-write on result JSONB.
    with SessionLocal() as s:
        run = s.get(McqRun, run_id, with_for_update=True)
        if run is None:
            raise ValueError("MCQ run not found.")
        fresh = dict(run.result or {})
        qs = list(fresh.get("questions") or [])
        fidx = next((i for i, q in enumerate(qs) if q.get("outcome") == outcome), -1)
        if fidx >= 0:
            qs[fidx] = new_q
        else:
            qs.append(new_q)
        fresh["questions"] = qs
        if swap_lo is not None:                  # swap the reserve LO into the freshly-locked copy
            _apply_lo_swap(fresh, outcome, swap_lo)
        if actual_qtype != current_qtype:        # keep the run's LO type in sync, on the fresh copy
            sync_outcome = (swap_lo.get("outcome") if swap_lo else outcome)
            for flo in (fresh.get("final_los") or []):
                if flo.get("outcome") == sync_outcome:
                    flo["question_type"] = actual_qtype
        run.result = fresh
        run.review_status = "question_review"
        run.needs_human_count = sum(1 for q in qs if q.get("needs_human"))
        run.approved_count = _approved_count(qs)   # a regenerated question is no longer approved
        s.add(McqQuestionFeedback(
            run_id=run_id, stage="question", outcome=outcome, question_type=actual_qtype,
            action="reject_regenerate", tags=tags or [], comment=feedback or "",
            before_snapshot={"lean": old_q.get("lean"), "review": old_q.get("review"),
                             "question_type": current_qtype},
            after_snapshot={"lean": new_q.get("lean"), "review": new_q.get("review"),
                            "question_type": actual_qtype, "type_change": new_q.get("type_change"),
                            "lo_swap": new_q.get("lo_swap")},
            reviewer=reviewer or "",
        ))
        s.commit()
    return new_q


def record_feedback(run_id, outcome: str, *, action: str, tags: list | None = None,
                    comment: str = "", reviewer: str = "") -> dict:
    """Record a non-regenerating review action (e.g. accept) on a question."""
    with SessionLocal() as s:
        run = s.get(McqRun, run_id)
        if run is None:
            raise ValueError("MCQ run not found.")
        qtype = _qtype_for(run.result or {}, outcome)
        if run.review_status == "draft":
            run.review_status = "question_review"
        s.add(McqQuestionFeedback(
            run_id=run_id, stage="question", outcome=outcome, question_type=qtype,
            action=action, tags=tags or [], comment=comment or "", before_snapshot={},
            reviewer=reviewer or "",
        ))
        s.commit()
    return {"ok": True, "outcome": outcome, "action": action}


def set_question_approval(run_id, outcome: str, approval: str, *, reviewer: str = "") -> dict:
    """Set a human approval decision on one question and recompute the run's approved_count.
    `approval` is 'approved', 'rejected', or 'pending' (cleared). Serialized via
    SELECT … FOR UPDATE so a concurrent decision on another question isn't lost."""
    approval = (approval or "").strip().lower()
    if approval not in ("approved", "rejected", "pending"):
        raise ValueError("approval must be 'approved', 'rejected' or 'pending'.")
    with SessionLocal() as s:
        run = s.get(McqRun, run_id, with_for_update=True)
        if run is None:
            raise ValueError("MCQ run not found.")
        fresh = dict(run.result or {})
        qs = list(fresh.get("questions") or [])
        idx = next((i for i, q in enumerate(qs) if q.get("outcome") == outcome), -1)
        if idx < 0:
            raise ValueError(f"No question found for outcome {outcome!r}.")
        q = dict(qs[idx])
        q["approval"] = None if approval == "pending" else approval
        qs[idx] = q
        fresh["questions"] = qs
        run.result = fresh
        run.approved_count = _approved_count(qs)
        if run.review_status == "draft":
            run.review_status = "question_review"
        s.add(McqQuestionFeedback(
            run_id=run_id, stage="question", outcome=outcome,
            question_type=q.get("question_type", ""),
            action={"approved": "approve", "rejected": "reject"}.get(approval, "unapprove"),
            tags=[], comment="", before_snapshot={}, reviewer=reviewer or "",
        ))
        s.commit()
        approved_count = run.approved_count
    return {"ok": True, "outcome": outcome, "approval": q["approval"],
            "approved_count": approved_count, "eligible_count": len(_eligible(qs))}


def set_question_exclusion(run_id, outcome: str, excluded: bool, *, reviewer: str = "") -> dict:
    """Mark one question excluded (or include it again). Excluded questions remain in
    the list (shaded out) but drop out of the approval tally and are skipped on
    export/load. Recomputes approved_count and records the action."""
    with SessionLocal() as s:
        run = s.get(McqRun, run_id, with_for_update=True)
        if run is None:
            raise ValueError("MCQ run not found.")
        fresh = dict(run.result or {})
        qs = list(fresh.get("questions") or [])
        idx = next((i for i, q in enumerate(qs) if q.get("outcome") == outcome), -1)
        if idx < 0:
            raise ValueError(f"No question found for outcome {outcome!r}.")
        q = dict(qs[idx])
        q["excluded"] = bool(excluded)
        qs[idx] = q
        fresh["questions"] = qs
        run.result = fresh
        run.approved_count = _approved_count(qs)
        if run.review_status == "draft":
            run.review_status = "question_review"
        s.add(McqQuestionFeedback(
            run_id=run_id, stage="question", outcome=outcome,
            question_type=q.get("question_type", ""),
            action="exclude" if excluded else "include",
            tags=[], comment="", before_snapshot={}, reviewer=reviewer or "",
        ))
        s.commit()
        approved_count = run.approved_count
    return {"ok": True, "outcome": outcome, "excluded": q["excluded"],
            "approved_count": approved_count, "eligible_count": len(_eligible(qs))}


def approve_run(run_id, *, reviewer: str = "") -> dict:
    with SessionLocal() as s:
        run = s.get(McqRun, run_id)
        if run is None:
            raise ValueError("MCQ run not found.")
        run.review_status = "approved"
        # Durable audit of the run-level sign-off (per-question actions are already
        # recorded; this captures "who marked the whole run reviewed").
        s.add(McqQuestionFeedback(
            run_id=run_id, stage="run", outcome="", question_type="",
            action="approve_run", tags=[], comment="", before_snapshot={},
            reviewer=reviewer or "",
        ))
        s.commit()
        status = run.review_status
    return {"ok": True, "review_status": status, "reviewer": reviewer}


def feedback_insights() -> dict:
    """Aggregate feedback for the loop's L1 'store + insights' phase."""
    from collections import Counter

    from sqlalchemy import select
    with SessionLocal() as s:
        rows = s.scalars(select(McqQuestionFeedback)).all()
        by_action, by_type, by_tag = Counter(), Counter(), Counter()
        for r in rows:
            by_action[r.action] += 1
            if r.question_type:
                by_type[r.question_type] += 1
            for t in (r.tags or []):
                by_tag[t] += 1
    return {
        "total": len(rows),
        "by_action": dict(by_action),
        "by_question_type": dict(by_type),
        "by_tag": dict(by_tag.most_common()),
    }
