"""Question pipeline · Node 8 — generate_questions · deterministic enforcement.

Creation-layer guards applied after generation so review rarely intervenes: the code
snippet must live in the `code` field (not a fenced block in the stem); option MCQs must
have the right count / exactly-one-or-2-3 correct / no near-duplicates / no shared
redundant lead-in; and a trailing full stop is stripped from stems and options.
`fix_lean` (node.py) is imported lazily to avoid a cycle.
"""
from __future__ import annotations

import difflib
import re

from app.mcq_pipeline.nodes.m08_generate_questions.grounding import _course_is_sql, _ground


_FENCE_RE = re.compile(r"```[A-Za-z0-9_+\-]*\n?(.*?)```", re.S)


def _enforce_code_visibility(lo: dict, res: dict) -> dict:
    """CODE_ANALYSIS_*/code types must carry the snippet in the `code` FIELD, never as a
    fenced ``` block inside the stem (the stem renders as prose; a learner would see the
    code duplicated or, for a plain MCQ, not at all). If the model inlined code in the
    stem, move it into `code` and leave the stem REFERRING to it; strip a duplicate fence
    when `code` is already populated."""
    lean = res.get("lean") or {}
    if "code" not in lean:                      # this lean shape has no code field
        return res
    stem = lean.get("question") or ""
    m = _FENCE_RE.search(stem)
    if not m:
        return res
    extracted = (m.group(1) or "").strip()
    if extracted and not (lean.get("code") or "").strip():
        lean["code"] = extracted
        if not (lean.get("code_language") or "").strip():
            lean["code_language"] = "SQL" if _course_is_sql() else "PYTHON"
    cleaned = _FENCE_RE.sub("", stem).strip()
    if len(cleaned) < 12:                        # stem was basically just the code
        cleaned = "What is the output of the given code snippet?"
    elif not re.search(r"\b(code|snippet|program|script|query)\b", cleaned, re.I):
        cleaned += " (refer to the given code snippet)"
    lean["question"] = cleaned
    return res


def _opt_issue(rule: str, problem: str) -> dict:
    return {"severity": "high", "rule": rule, "problem": problem,
            "suggested_fix": "fix only this; keep the question grounded and the type unchanged"}


def _common_option_prefix(opts: list[dict]) -> str:
    """The shared leading word-run across ALL options (>= 2 words), else ''. A redundant
    lead-in repeated in every option ('The sequence of operations: ...') belongs in the
    stem — it wastes scan time and is never the discriminator."""
    texts = [(o.get("content") or "").strip() for o in opts]
    texts = [t for t in texts if t]
    if len(texts) < 2:
        return ""
    words = [t.split() for t in texts]
    pref: list[str] = []
    for tup in zip(*words):
        if all(w.lower() == tup[0].lower() for w in tup):
            pref.append(tup[0])
        else:
            break
    # require >= 2 shared words AND that the prefix isn't the WHOLE of the shortest option
    if len(pref) >= 2 and len(pref) < min(len(w) for w in words):
        return " ".join(pref)
    return ""


def _enforce_options(lo: dict, res: dict, max_seq: int | None) -> dict:
    """Deterministic option guards for option MCQs: enforce count + exactly-one/2-3
    correct and de-duplicate near-identical options, feeding precise issues to the
    fix loop so these never reach review."""
    from app.mcq_pipeline.nodes.m08_generate_questions.node import fix_lean
    qtype = res["question_type"]
    if qtype not in ("MULTIPLE_CHOICE", "MORE_THAN_ONE_MULTIPLE_CHOICE"):
        return res
    fix_lo = {**lo, "question_type": qtype}
    ctx = _ground(fix_lo, max_seq)
    for _ in range(2):                      # separate small option-fix budget
        lean = res["lean"]
        opts = lean.get("options") or []
        n = len(opts)
        ncorrect = sum(1 for o in opts if o.get("is_correct"))
        issues: list[dict] = []
        if qtype == "MULTIPLE_CHOICE":
            if n != 4:
                issues.append(_opt_issue("OPTION RULES", f"must have exactly 4 options (has {n})"))
            if ncorrect != 1:
                issues.append(_opt_issue("OPTION RULES", f"exactly ONE option must be correct (has {ncorrect})"))
        else:
            if not (4 <= n <= 6):
                issues.append(_opt_issue("OPTION RULES", f"must have 4-6 options (has {n})"))
            if ncorrect < 2:
                issues.append(_opt_issue("MORE-THAN-ONE", f"at least 2 options must be correct (has {ncorrect})"))
            if n and ncorrect >= n:
                issues.append(_opt_issue("MORE-THAN-ONE", "at least 1 option must be incorrect"))
        for i in range(n):
            for j in range(i + 1, n):
                a = (opts[i].get("content") or "").strip().lower()
                b = (opts[j].get("content") or "").strip().lower()
                if a and b and difflib.SequenceMatcher(None, a, b).ratio() >= 0.82:
                    issues.append(_opt_issue(
                        "DISTRACTOR RULES",
                        f"two options express the same idea ('{opts[i].get('content')}' / "
                        f"'{opts[j].get('content')}') — make each option distinct and genuinely incorrect"))
        prefix = _common_option_prefix(opts)
        if prefix:
            issues.append(_opt_issue(
                "OPTION RULES",
                f"all options share a redundant lead-in ('{prefix}') — move that shared text into the "
                f"stem and keep ONLY the distinguishing part in each option"))
        if not issues:
            break
        res["lean"] = fix_lean(fix_lo, ctx, lean, issues)
    return res


def _strip_trailing_period(text: str) -> str:
    """Remove a single trailing full stop from learner-facing text. Preserves an
    ellipsis ('...') and leaves all other punctuation untouched — only a bare
    trailing '.' is dropped (per the no-trailing-period rule for stems/options)."""
    if not isinstance(text, str):
        return text
    t = text.rstrip()
    if t.endswith(".") and not t.endswith(".."):
        t = t[:-1].rstrip()
    return t


def _normalize_lean_text(lean: dict) -> dict:
    """In-place: strip a trailing period from the question stem / statement and from
    each option's content. Graded values (TEXTUAL/FIB answers, expected output) and
    `code` are intentionally left literal."""
    if not isinstance(lean, dict):
        return lean
    for fld in ("question", "statement"):
        if isinstance(lean.get(fld), str):
            lean[fld] = _strip_trailing_period(lean[fld])
    for o in (lean.get("options") or []):
        if isinstance(o, dict) and isinstance(o.get("content"), str):
            o["content"] = _strip_trailing_period(o["content"])
    return lean
