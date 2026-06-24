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
    from app.mcq_pipeline.nodes.n13_recommend_question_type import QUESTION_TYPES
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
    from app.mcq_pipeline.nodes.n14_generate_questions import _ground, fix_lean, generate_lean
    from app.mcq_pipeline.nodes.n15_review_questions import review_and_fix_one
    from app.mcq_pipeline.nodes.n13_recommend_question_type import recommend_one
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

    # Honor a reviewer request to CHANGE the question type: regenerate AS the requested
    # type rather than fixing within the old one (fix_lean can't change type). Detection
    # is LLM-PRIMARY (robust to phrasing) with a deterministic keyword fallback; if a
    # change is wanted but no type is named, the type-agent re-picks the ideal for the LO.
    _lean = old_q.get("lean") or {}
    question_text = (_lean.get("question") or _lean.get("statement") or lo.get("description") or "")[:1200]
    target = _detect_type_change(feedback, current_qtype, question_text)
    if target == "REPICK":
        target = (recommend_one(lo) or {}).get("question_type")
    new_qtype = target or current_qtype

    gen_lo = {**lo, "question_type": new_qtype}
    ctx = _ground(gen_lo, None)
    gen = generate_lean(gen_lo)
    if gen.get("status") == "generated" and gen.get("lean"):
        gen["lean"] = fix_lean(gen_lo, ctx, gen["lean"], [{
            "severity": "high", "rule": "HUMAN FEEDBACK",
            "problem": feedback, "suggested_fix": feedback,
        }])
    gen["outcome"] = outcome
    new_q = review_and_fix_one(lo, gen)
    # generate_lean may itself fall back (e.g. an ungroundable code type -> MCQ), so the
    # actual type is whatever was produced.
    actual_qtype = new_q.get("question_type") or new_qtype
    if actual_qtype != current_qtype:
        new_q["type_change"] = {"from": current_qtype, "to": actual_qtype, "requested": new_qtype}
        lo["question_type"] = actual_qtype   # lo is the run-result's object; keep it in sync

    # 3) carry forward revision + feedback history on the question
    prev = {k: v for k, v in old_q.items() if k != "revisions"}
    new_q["revisions"] = (old_q.get("revisions") or []) + [prev]
    new_q["human_feedback"] = (old_q.get("human_feedback") or []) + [
        {"feedback": feedback, "tags": tags or [], "reviewer": reviewer}]
    result["questions"][idx] = new_q

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
        if actual_qtype != current_qtype:        # keep the run's LO type in sync, on the fresh copy
            for flo in (fresh.get("final_los") or []):
                if flo.get("outcome") == outcome:
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
                            "question_type": actual_qtype, "type_change": new_q.get("type_change")},
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
