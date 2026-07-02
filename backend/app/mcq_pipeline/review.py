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

import json
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


# --- REASONING-FIRST edit planner ------------------------------------------- #
# The coarse intent buckets above apply a GENERIC instruction that can fight the reviewer (e.g.
# "wrong_answer" -> "mark the correct option per the COURSE MATERIAL" reverts an explicit human
# answer-key). Instead, first REASON about the exact intent with the question laid out (options
# A/B/C/D, code, stem, key), the READING MATERIAL (authoritative for what was taught) + RAG context,
# and the sibling questions (to de-duplicate), then emit a precise, surgical fix directive.
_EDIT_PLAN_SYS = register("review.edit_plan_sys", (
    "You are a senior assessment editor fixing ONE question from a reviewer's feedback. Do NOT rewrite "
    "blindly — first REASON about what the reviewer actually wants, then give a precise, surgical plan.\n\n"
    "You are given: the reviewer FEEDBACK; the CURRENT question (stem, any CODE, options labelled "
    "A/B/C/D with the CORRECT one marked, explanation); the READING MATERIAL / course context that is "
    "AUTHORITATIVE for what was actually taught; and the OTHER questions in this set (to avoid overlap).\n\n"
    "Reason in steps:\n"
    "1. INTENT — state precisely what to change: a specific option (by letter), the correct answer/key, "
    "the stem wording, the CODE, the number of options, self-containment, or 'make it different from "
    "question N'. Quote the reviewer's concrete ask.\n"
    "2. VERIFY against the READING MATERIAL — when the reviewer asserts a correct answer or syntax "
    "('we taught X', 'the answer should be X'), check the material. The reviewer + the material are "
    "AUTHORITATIVE over any earlier grounding guess: if they say X is the taught answer and the material "
    "is consistent, X BECOMES the correct key — do NOT keep the old key. Use the material to get exact "
    "syntax right (e.g. request.POST.get('title') vs request.POST['title']).\n"
    "3. PLAN — one specific fix_directive naming exactly which option letter / field / code / stem to "
    "change and the new value, preserving everything the reviewer did NOT complain about.\n\n"
    "HARD RULES for the fix:\n"
    "- SELF-CONTAINED STEM: the reviewer's mention of 'the session'/'what we taught' tells YOU which "
    "answer is correct — it must NEVER appear in the question. Do NOT write stems like 'according to the "
    "session context...', 'as taught in the session...', 'based on the reading...'. The stem must read as "
    "a standalone question answerable from itself.\n"
    "- VALID DISTRACTORS: every wrong option must be a real, plausible, SYNTACTICALLY-VALID alternative in "
    "the same domain that a learner could genuinely confuse with the key (verify option syntax against the "
    "material) — never malformed, nonsense, or a trivially-wrong string.\n"
    "- EXACTLY ONE correct option for single-select types; the correct option must be genuinely correct.\n\n"
    "SCOPE — this is critical: change ONLY the field(s) the reviewer actually asked about; everything "
    "else must stay byte-for-byte. List those fields in change_fields, drawn from: 'stem' (the question "
    "text/description), 'code', 'options' (add/replace/remove a distractor's wording), 'correct_key' "
    "(which option is correct), 'explanation'. E.g. 'change option B' -> ['options'] (NOT 'stem'); "
    "'reword the question' -> ['stem']; 'the answer should be X' -> ['correct_key'] (+ 'options' only if "
    "an option's text must change). Never include a field the reviewer did not raise.\n"
    "- CODE-ANALYSIS questions (the answer is the OUTPUT of the shown code): a request to change 'the "
    "correct answer', a syntax, or a model/variable/field name means editing the CODE itself — include "
    "'code' in change_fields and put the exact literal change in fix_directive (e.g. rename Product->Book, "
    "or request.POST['title'] -> request.POST.get('title')).\n\n"
    'Return ONLY JSON: {"reasoning": "<intent + verification, 1-3 sentences>", '
    '"change_fields": ["<subset of stem|code|options|correct_key|explanation>"], '
    '"authoritative_correct_answer": "<verbatim text the correct option MUST have, or null>", '
    '"fix_directive": "<the precise surgical instruction to apply>"}.'
))

# Fields whose keys we can deterministically preserve when they are OUT of the reviewer's scope.
_STEM_KEYS = ("question", "statement")
_CODE_KEYS = ("code", "code_snippet", "code_stub")
_ANSWER_KEYS = ("options", "is_true", "correct_output", "correct_outputs", "blank_answer", "ordered_items")


def _apply_field_scope(old_lean: dict, new_lean: dict, change_fields) -> dict:
    """Hard-preserve the fields the reviewer did NOT ask to change, so 'change option B' can't
    silently rewrite the stem (the #1 complaint). The reasoned plan declares change_fields; anything
    not listed is restored verbatim from the original question. Answer fields (options + key) are only
    restored wholesale when NEITHER 'options' nor 'correct_key' is in scope — when an option IS being
    edited we keep the model's new options (can't safely restore a single one by identity)."""
    cf = {str(f).strip().lower() for f in (change_fields or [])}
    if not cf:                       # planner didn't scope it — don't over-constrain
        return new_lean
    merged = dict(new_lean or {})
    if "stem" not in cf:
        for k in _STEM_KEYS:
            if k in (old_lean or {}):
                merged[k] = old_lean[k]
    if "code" not in cf:
        for k in _CODE_KEYS:
            if k in (old_lean or {}):
                merged[k] = old_lean[k]
    if "options" not in cf and "correct_key" not in cf:   # answer untouched -> restore it wholesale
        for k in _ANSWER_KEYS:
            if k in (old_lean or {}):
                merged[k] = old_lean[k]
    return merged


def _labeled_question(lean: dict, qtype: str) -> str:
    """Render the current question with A/B/C/D-labelled options + the marked key, code and stem —
    so the planner can target a SPECIFIC option/field the way the reviewer refers to them."""
    lean = lean or {}
    parts = []
    stem = lean.get("question") or lean.get("statement") or ""
    if stem:
        parts.append(f"STEM: {stem}")
    code = lean.get("code") or lean.get("code_snippet") or lean.get("code_stub")
    if code:
        parts.append(f"CODE:\n{code}")
    for i, o in enumerate(lean.get("options") or []):
        mark = "  <-- CORRECT" if o.get("is_correct") else ""
        parts.append(f"{chr(65 + i)}) {o.get('content', '')}{mark}")
    if qtype == "TRUE_OR_FALSE":
        parts.append(f"KEY: {'True' if lean.get('is_true') else 'False'}")
    if lean.get("blank_answer"):
        parts.append(f"BLANK ANSWER: {lean.get('blank_answer')}")
    if lean.get("explanation"):
        parts.append(f"EXPLANATION: {lean.get('explanation')}")
    return "\n".join(parts)


def _sibling_stems(result: dict, self_key: str) -> list:
    """Stems of the OTHER questions in the run — context for 'this overlaps / make it different'."""
    out = []
    for q in (result.get("questions") or []):
        if (q.get("question_key") or q.get("outcome")) == self_key or q.get("excluded"):
            continue
        t = _q_text(q.get("lean") or {})
        if t:
            out.append(t[:150])
    return out


def _plan_edit(feedback: str, lean: dict, qtype: str, lo: dict, grounding: str, siblings: list) -> dict:
    """Reason about the reviewer's feedback WITH the reading material + RAG + labelled options in view,
    and return {reasoning, authoritative_answer, fix_directive}. Best-effort: {} on failure (caller
    falls back to the coarse intent instruction)."""
    if not (feedback or "").strip():
        return {}
    from app.mcq_pipeline.utils.llm import chat, parse_json
    rm = (lo.get("source_section_text") or lo.get("source_evidence") or "")
    rm = rm if isinstance(rm, str) else ""
    ground = (grounding or "")
    sib = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(siblings[:12])) or "(none)"
    usr = (f"REVIEWER FEEDBACK:\n{feedback}\n\n"
           f"CURRENT QUESTION (type {qtype}):\n{_labeled_question(lean, qtype)}\n\n"
           f"READING MATERIAL (authoritative — what the session taught):\n{(rm or '(none stored)')[:3500]}\n\n"
           f"ADDITIONAL COURSE CONTEXT (RAG):\n{ground[:2500] or '(none)'}\n\n"
           f"OTHER QUESTIONS IN THIS SET (do NOT overlap with these):\n{sib}")
    try:
        data = parse_json(chat(
            [{"role": "system", "content": get_prompt("review.edit_plan_sys", _EDIT_PLAN_SYS)},
             {"role": "user", "content": usr}], temperature=0)) or {}
    except Exception:  # noqa: BLE001 — never block regeneration on the planner
        return {}
    auth = data.get("authoritative_correct_answer")
    auth = "" if auth in (None, "null", "None") else str(auth).strip()
    cf = data.get("change_fields") or []
    cf = [str(f).strip().lower() for f in cf if str(f).strip()] if isinstance(cf, list) else []
    return {"reasoning": str(data.get("reasoning") or "").strip(),
            "authoritative_answer": auth,
            "change_fields": cf,
            "fix_directive": str(data.get("fix_directive") or "").strip()}


def _reasoned_issue(intent: str, feedback: str, plan: dict) -> dict:
    """Turn the reasoned plan into the high-severity fix issue for fix_lean. Falls back to the coarse
    intent instruction when the planner produced nothing usable."""
    directive = (plan or {}).get("fix_directive") or ""
    if not directive:
        return _intent_issue(intent or "other", feedback)
    auth = (plan or {}).get("authoritative_answer") or ""
    cf = set((plan or {}).get("change_fields") or [])
    fix = directive
    if auth:
        fix += (f"\nAUTHORITATIVE ANSWER KEY: the correct option MUST be exactly \"{auth}\" — the reviewer "
                f"confirms this is what the session teaches. Mark that option correct, make every other "
                f"option genuinely wrong, and do NOT revert the key to a different option on grounding grounds.")
    if "code" in cf:
        fix += ("\nCODE EDIT IS AUTHORITATIVE: apply the reviewer's code change LITERALLY — rename the "
                "model/variable/field or change the syntax EXACTLY as asked (e.g. Product -> Book, or "
                "request.POST['title'] -> request.POST.get('title')) even if the grounding material still "
                "uses the old form; the reviewer's instruction overrides the material's naming/syntax here.")
    fix += ("\nSELF-CONTAINED: keep the stem standalone — never phrase it as 'according to the session/"
            "reading/context' or 'as taught'; that reference is about correctness, not text for the stem. "
            "Every distractor must be a plausible, syntactically-valid alternative (not malformed/nonsense).")
    return {"severity": "high", "rule": "REVIEWER FEEDBACK (reasoned)",
            "problem": (f"Reviewer feedback: {feedback}\n"
                        f"Understood intent: {(plan or {}).get('reasoning', '')[:500]}"),
            "suggested_fix": fix}


# --- regeneration MEMORY: feed prior rejected attempts back in so a regen fixes ALL --- #
# past complaints and never reproduces a version already rejected (the #1 driver of the
# observed ~2.4-regens-to-converge tail). The data is already on the question object
# (revisions + human_feedback); it was just never consumed.
def _q_text(lean: dict) -> str:
    return ((lean or {}).get("question") or (lean or {}).get("statement") or "").strip()


def _memory_issue(old_q: dict) -> dict | None:
    """Build a high-severity 'do not repeat' issue from this question's prior rejected attempts."""
    revs = old_q.get("revisions") or []
    fbs = old_q.get("human_feedback") or []
    past_fb = [str(f.get("feedback", "")).strip() for f in fbs if str(f.get("feedback", "")).strip()]
    past_q = [t[:200] for r in revs if (t := _q_text(r.get("lean") or {}))]
    if not past_fb and not past_q:
        return None
    parts = [f"This question has already been regenerated {len(revs)} time(s); earlier attempts were REJECTED."]
    if past_fb:
        parts.append("Prior reviewer complaints — satisfy ALL of them, not just the latest: "
                     + " | ".join(past_fb[-5:]))
    if past_q:
        parts.append("Earlier REJECTED versions — do NOT reproduce or lightly reword any of these: "
                     + " || ".join(past_q[-3:]))
    return {"severity": "high", "rule": "REGENERATION MEMORY", "problem": "\n".join(parts),
            "suggested_fix": ("Produce a question that fixes EVERY prior complaint and is materially "
                              "different from every earlier rejected version above.")}


# --- self-check: did the regen actually RESOLVE the reviewer's complaint? One bounded repair --- #
_FEEDBACK_SATISFIED_SYS = register("review.feedback_satisfied_sys", (
    "You verify whether a regenerated multiple-choice question now RESOLVES a reviewer's complaint. "
    "You are given the reviewer feedback and the NEW question (as JSON: stem, options, correct flag). "
    "Decide ONLY whether the SPECIFIC issue the reviewer raised is now fixed — not general quality. "
    "If the feedback was vague/general with nothing concrete to verify, treat it as resolved. "
    'Return ONLY JSON: {"resolved": true|false, "reason": "<if false, the single thing still wrong>"}.'
))


def _satisfies_feedback(feedback: str, lean: dict) -> dict:
    """Best-effort check that the new question addresses the complaint. Never blocks on failure."""
    if not (feedback or "").strip() or not lean:
        return {"resolved": True}
    from app.mcq_pipeline.utils.llm import chat, parse_json
    try:
        view = json.dumps(lean, ensure_ascii=False)[:1800]
        data = parse_json(chat(
            [{"role": "system", "content": get_prompt("review.feedback_satisfied_sys", _FEEDBACK_SATISFIED_SYS)},
             {"role": "user", "content": f"REVIEWER FEEDBACK:\n{feedback}\n\nNEW QUESTION:\n{view}"}],
            temperature=0)) or {}
        return {"resolved": bool(data.get("resolved", True)), "reason": str(data.get("reason") or "")}
    except Exception:  # noqa: BLE001 — never block regeneration on the self-check
        return {"resolved": True}


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


_VARIANT_SUFFIX = re.compile(r"::v\d+$")


def _base_outcome(ident: str) -> str:
    """Strip a Classroom-Quiz variant suffix ('base::v1' -> 'base'). Bases are unaffected."""
    return _VARIANT_SUFFIX.sub("", ident or "")


def _find_lo(result: dict, outcome: str) -> dict | None:
    # CQ variants share their base's LO (keyed by the BASE outcome), so strip any ::vN suffix.
    base = _base_outcome(outcome)
    return next((lo for lo in (result.get("final_los") or []) if lo.get("outcome") == base), None)


def _find_q_index(result: dict, outcome: str) -> int:
    # Variants share their base's `outcome`, so match the UNIQUE `question_key` first (bases have
    # question_key == outcome), then fall back to `outcome` for older runs that predate question_key.
    qs = result.get("questions") or []
    i = next((i for i, q in enumerate(qs) if (q.get("question_key") or q.get("outcome")) == outcome), -1)
    if i >= 0:
        return i
    return next((i for i, q in enumerate(qs) if q.get("outcome") == outcome), -1)


def _variant_directive(old_q: dict) -> str:
    """Rebuild the m10 objective-binding directive for a variant being regenerated, so the reused
    m08 generator stays on the SAME assertion / Bloom tier / angle. Empty when not a variant or no
    objective is stored (older runs) — then it regenerates as a plain question on the base LO."""
    obj = old_q.get("objective") or {}
    if not (obj.get("assertion") or "").strip():
        return ""
    try:
        from app.mcq_pipeline.nodes.m10_generate_variants.prompts import _DIRECTIVE
        return get_prompt("cq.variants.directive", _DIRECTIVE).format(
            assertion=obj.get("assertion", ""), bloom=obj.get("bloom_tier", ""),
            axis=old_q.get("variant_axis", ""), angle_instruction=old_q.get("variant_angle", ""))
    except Exception:  # noqa: BLE001 — never block regeneration on directive rebuild
        return ""


def _qtype_for(result: dict, outcome: str) -> str:
    i = _find_q_index(result, outcome)
    return (result.get("questions") or [{}])[i].get("question_type", "") if i >= 0 else ""


# --- "what changed" summary + no-op detection (a regen that silently changes nothing is a top --- #
# reviewer pain point; surface a note and flag it instead of persisting an identical question). --- #
def _stem(lean: dict) -> str:
    return ((lean or {}).get("question") or (lean or {}).get("statement") or "").strip()


def _opt_texts(lean: dict) -> list:
    return [((o.get("text") or o.get("content") or "") if isinstance(o, dict) else str(o))
            for o in ((lean or {}).get("options") or [])]


def _correct_texts(lean: dict) -> list:
    return sorted((o.get("text") or o.get("content") or "") for o in ((lean or {}).get("options") or [])
                  if isinstance(o, dict) and o.get("is_correct"))


_LEAN_FIELDS = ("code", "blank_answer", "test_input", "test_output", "answer", "explanation")


def _lean_changed(a: dict, b: dict) -> bool:
    if _stem(a) != _stem(b) or _opt_texts(a) != _opt_texts(b) or _correct_texts(a) != _correct_texts(b):
        return True
    return any((a.get(k) or "") != (b.get(k) or "") for k in _LEAN_FIELDS)


def _summarize_change(a: dict, b: dict, type_change: dict | None) -> str:
    """A concise human-readable note of what the regeneration changed, for the review UI."""
    parts = []
    if type_change:
        parts.append(f"changed type {type_change.get('from')} → {type_change.get('to')}")
    if _stem(a) != _stem(b):
        parts.append("reworded the question")
    oa, ob = _opt_texts(a), _opt_texts(b)
    if oa != ob:
        n = max(len([o for o in ob if o not in oa]), len([o for o in oa if o not in ob]))
        parts.append(f"changed {n} option(s)" if n else "reordered the options")
    if _correct_texts(a) != _correct_texts(b):
        parts.append("changed the correct answer")
    for k, label in (("code", "code"), ("blank_answer", "blank"), ("explanation", "explanation")):
        if (a.get(k) or "") != (b.get(k) or ""):
            parts.append(f"updated the {label}")
    return "; ".join(parts)


def _eligible(qs: list) -> list:
    """Questions that count toward approval — the ones actually generated, minus any a
    reviewer has excluded (excluded questions stay in the list but are not loaded)."""
    return [q for q in qs if q.get("status") == "generated" and not q.get("excluded")]


def _approved_count(qs: list) -> int:
    return sum(1 for q in _eligible(qs) if q.get("approval") == "approved")


def _find_idx(qs: list, ident: str) -> int:
    """Index of the question identified by `ident`. Variants share their base's `outcome`, so
    we match the UNIQUE `question_key` first (bases have question_key == outcome), then fall back
    to `outcome` for older runs that predate question_key."""
    i = next((i for i, q in enumerate(qs)
              if (q.get("question_key") or q.get("outcome")) == ident), -1)
    if i >= 0:
        return i
    return next((i for i, q in enumerate(qs) if q.get("outcome") == ident), -1)


def regenerate_question(run_id, outcome: str, feedback: str, *,
                        reviewer: str = "", tags: list | None = None,
                        dry_run: bool = False) -> dict:
    """Regenerate the question for `outcome`, injecting the human feedback as a
    top-priority instruction; re-review; persist (with a revision + feedback row).
    Returns the new question dict. `dry_run=True` runs the full reason→fix→review
    pipeline but writes NOTHING to the DB (used for smoke tests)."""
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
    # MEMORY: prior rejected attempts on THIS question, fed into every fix so the regen fixes all
    # past complaints and never reproduces a rejected version (empty list when this is the 1st regen).
    mem_issues = [m] if (m := _memory_issue(old_q)) else []
    # CQ VARIANT support: a variant is identified by its unique `question_key` (base::vN), shares the
    # base LO + outcome, and must stay bound to its objective/axis. Preserve all of that on regen.
    is_variant = bool(old_q.get("is_variant"))
    base_outcome = old_q.get("outcome") or _base_outcome(outcome)
    variant_key = old_q.get("question_key") or outcome
    variant_directive = _variant_directive(old_q) if is_variant else ""

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
    plan = {}   # reasoned edit plan (set in the content-fix branches); surfaced on the new question
    # When the OUTCOME itself is rejected and a reserve (dropped) outcome is available,
    # swap that reserve LO into this slot instead of re-asking the misaligned outcome.
    # A variant is bound to its base question's objective — never swap its LO (that would break the
    # variant set); a misaligned-outcome complaint on a variant is handled as a normal regen.
    swap_lo = _pick_reserve_lo(result, lo) if (intent == "lo_misaligned" and not is_variant) else None

    if swap_lo is not None:
        gen_lo = {**swap_lo, "question_type": new_qtype}
        ctx = _ground(gen_lo, None)
        gen = generate_lean(gen_lo)
        if gen.get("status") == "generated" and gen.get("lean"):
            gen["lean"] = fix_lean(gen_lo, ctx, gen["lean"], [_intent_issue("other", feedback)] + mem_issues)
        alignment_note = (f"Outcome swapped (the original was flagged as misaligned/out-of-scope): "
                          f"'{outcome}' -> '{swap_lo.get('outcome')}'. Please review the new question.")
    elif (not target) and _lean and intent not in ("new_question", "lo_misaligned"):
        # surgical fix of the existing question (content / format / alignment intents)
        gen_lo = {**lo, "question_type": new_qtype,
                  "description": (lo.get("description") or "") + variant_directive}
        ctx = _ground(gen_lo, None)
        # REASON about the exact intent with the reading material + RAG + labelled options + sibling
        # questions in view, then apply that precise, authoritative directive (not the coarse bucket).
        plan = _plan_edit(feedback, _lean, new_qtype, lo, ctx, _sibling_stems(result, variant_key))
        new_lean = fix_lean(gen_lo, ctx, _lean, [_reasoned_issue(intent, feedback, plan)] + mem_issues)
        gen = {"status": "generated", "question_type": current_qtype,
               "difficulty": difficulty_of(lo), "lean": new_lean}
    else:
        # type change, "make a new one", no prior lean, or LO-misaligned with no reserve -> fresh gen
        gen_lo = {**lo, "question_type": new_qtype,
                  "description": (lo.get("description") or "") + variant_directive}
        ctx = _ground(gen_lo, None)
        gen = generate_lean(gen_lo)
        if gen.get("status") == "generated" and gen.get("lean"):
            plan = _plan_edit(feedback, _lean or gen["lean"], new_qtype, lo, ctx,
                              _sibling_stems(result, variant_key))
            gen["lean"] = fix_lean(gen_lo, ctx, gen["lean"],
                                   [_reasoned_issue(intent or "other", feedback, plan)] + mem_issues)
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
    gen["outcome"] = effective_lo.get("outcome") if swap_lo else (base_outcome if is_variant else outcome)
    new_q = review_and_fix_one(effective_lo, gen)
    # SELF-CHECK: verify the regen actually RESOLVED the reviewer's complaint; one bounded repair if
    # not (carrying memory), so the human is no longer the convergence loop. Skipped for a request to
    # author a brand-new question (nothing specific to "resolve").
    if intent != "new_question":
        chk = _satisfies_feedback(feedback, new_q.get("lean") or {})
        if not chk.get("resolved", True):
            cur_type = new_q.get("question_type") or new_qtype
            retry_lo = {**effective_lo, "question_type": cur_type}
            relean = fix_lean(retry_lo, ctx, new_q.get("lean") or {},
                              [{"severity": "high", "rule": "REVIEWER FEEDBACK (STILL UNRESOLVED)",
                                "problem": ("The regenerated question still does not resolve the reviewer "
                                            f"feedback: {feedback}"),
                                "suggested_fix": chk.get("reason") or feedback}] + mem_issues)
            retry_gen = {"status": "generated", "question_type": cur_type,
                         "difficulty": difficulty_of(effective_lo), "lean": relean,
                         "outcome": gen.get("outcome")}
            new_q = review_and_fix_one(effective_lo, retry_gen)
            new_q["self_check_retried"] = True
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
        if not is_variant:                              # a variant must not rewrite the shared base LO
            effective_lo["question_type"] = actual_qtype   # keep the run-result LO object in sync
    # restore the variant's identity: outcome stays the BASE; the unique question_key and the
    # objective/axis binding are preserved so it remains a coherent member of the variant set.
    if is_variant:
        new_q.update({"is_variant": True, "question_key": variant_key, "outcome": base_outcome,
                      "base_question_key": old_q.get("base_question_key"),
                      "variant_axis": old_q.get("variant_axis"),
                      "variant_angle": old_q.get("variant_angle"),
                      "objective": old_q.get("objective")})

    # SCOPE the edit to the field(s) the reviewer actually raised: restore everything else verbatim so
    # 'change option B' can't silently rewrite the stem (the reviewer's complaint). Only applies on the
    # surgical path where the reasoned plan declared a scope; type-change / new-question rewrite freely.
    if plan.get("change_fields") and new_q.get("lean") is not None:
        new_q["lean"] = _apply_field_scope(_lean, new_q.get("lean") or {}, plan["change_fields"])

    # "WHAT CHANGED" note + no-op guard: tell the reviewer exactly what the regen altered, and if it
    # changed NOTHING (e.g. vague feedback the model couldn't act on), flag it instead of silently
    # persisting an identical question — the #1/#2 reviewer complaints ("not changing anything").
    old_lean = old_q.get("lean") or {}
    new_lean = new_q.get("lean") or {}
    if _lean_changed(old_lean, new_lean):
        new_q["change_summary"] = _summarize_change(old_lean, new_lean, new_q.get("type_change"))
        new_q["unchanged"] = False
    else:
        new_q["unchanged"] = True
        new_q["needs_human"] = True
        new_q["change_summary"] = ("No change was produced — the feedback may be too vague to act on. "
                                   "Tell me exactly what to change (which option, the stem, or the answer).")
    # Surface what the agent understood the reviewer to want (builds trust; shown alongside the change).
    if plan.get("reasoning"):
        new_q["regen_reasoning"] = plan["reasoning"]

    # 3) carry forward revision + feedback history on the question
    prev = {k: v for k, v in old_q.items() if k != "revisions"}
    new_q["revisions"] = (old_q.get("revisions") or []) + [prev]
    new_q["human_feedback"] = (old_q.get("human_feedback") or []) + [
        {"feedback": feedback, "tags": tags or [], "reviewer": reviewer}]
    result["questions"][idx] = new_q
    if swap_lo is not None:
        _apply_lo_swap(result, outcome, swap_lo)

    if dry_run:                    # smoke test: skip all persistence, just return the new question
        return new_q

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
        fidx = _find_idx(qs, outcome)
        if fidx >= 0:
            qs[fidx] = new_q
        else:
            qs.append(new_q)
        fresh["questions"] = qs
        if swap_lo is not None:                  # swap the reserve LO into the freshly-locked copy
            _apply_lo_swap(fresh, outcome, swap_lo)
        if actual_qtype != current_qtype and not is_variant:   # never resync the base LO from a variant
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
        idx = _find_idx(qs, outcome)
        if idx < 0:
            raise ValueError(f"No question found for {outcome!r}.")
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
        idx = _find_idx(qs, outcome)
        if idx < 0:
            raise ValueError(f"No question found for {outcome!r}.")
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
