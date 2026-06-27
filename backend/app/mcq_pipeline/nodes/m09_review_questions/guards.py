"""Question pipeline · Node 9 — review_questions · models + deterministic guards.

The review LLM's structured-output models, plus the high-precision deterministic detectors
that run alongside it (RAG term-coverage, self-containment, external-resource-in-code,
verbatim-source, phantom-code) and the batched distractor-depth audit. These catch what
the LLM reviewer can overlook and force a fix (passed=False) when they fire.
"""
from __future__ import annotations

import re
from typing import List, Literal

from pydantic import BaseModel, Field

from app.mcq_pipeline.utils import rag_api
from app.mcq_pipeline.prompts.store import get_prompt
from app.mcq_pipeline.nodes.m08_generate_questions import OPTION_TYPES
from app.mcq_pipeline.nodes.m09_review_questions.prompts import _DISTRACTOR_DEPTH_AUDIT


def _review_model(temp: float = 0):
    # Question REVIEW agent. Built on the active connector (OpenRouter) but with the
    # review model id (settings.mcq_review_model — GPT-4o); empty -> the connector's own
    # model. Distinct from generation (m08._model), which runs on Sonnet 4.6.
    from app.core.config import settings
    from app.mcq_pipeline.utils.llm import make_chat_model
    return make_chat_model(temperature=temp, model=settings.mcq_review_model or None)


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


# A "named term" carries a technical signal — an uppercase letter (proper noun /
# CamelCase), a digit (version), or code punctuation (dotted call, path, parens,
# underscore, backtick). Generic all-lowercase prose phrases ("shared space",
# "project dependencies") carry none and are NOT RAG-checked: whether their CLAIM is
# grounded is the reviewer LLM's job, not a literal keyword lookup. This is what
# prevents the option-term check from false-flagging paraphrased distractors.
_NAMED_SIGNAL = re.compile(r"[A-Z0-9._/()`+=*%<>&|]")


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
    r"|\baccording to (?:the |this |our )?(?:above |below |given )?(?:course |reading |study )?"
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


# Deterministic EXTERNAL-DEPENDENCY guard for code questions. A self-contained snippet
# runs on built-ins + taught constructs alone; the moment it reaches for a file, the
# network, a database, an env var, the shell, or a third-party data library, it depends
# on a resource the learner cannot see. Patterns are deliberately specific (an accessor
# call / import), not bare words, so ordinary identifiers don't false-flag.
_CODE_EXTERNAL_RE = re.compile(
    # network
    r"\bimport\s+requests\b|\bfrom\s+requests\b|\brequests\.(?:get|post|put|delete|patch|session)\b"
    r"|\burllib\b|\bhttp\.client\b|\burlopen\b|\bsocket\.\w|\bfetch\s*\(|\baxios\b|\bXMLHttpRequest\b|https?://"
    # filesystem
    r"|\bopen\s*\(|\bwith\s+open\b|\bpathlib\b|\bos\.path\b|\breadFileSync\b|\bwriteFileSync\b|\bfs\.(?:read|write|append)"
    r"|\bread_csv\b|\bto_csv\b|\bread_excel\b|\bnp\.loadtxt\b|\bnp\.load\b"
    # database (match the library/engine — NOT bare .connect()/.cursor(), which also hit
    # ordinary user-defined objects and would false-flag non-DB code)
    r"|\bsqlite3\b|\bpsycopg2?\b|\bpymysql\b|\bsqlalchemy\b|\bMongoClient\b|\bcreate_engine\b|\bmysql\.connector\b"
    # env / shell
    r"|\bos\.environ\b|\bos\.getenv\b|\bgetenv\s*\(|\bos\.system\b|\bsubprocess\b|\bsys\.argv\b",
    re.I,
)


def _code_text(lean: dict) -> str:
    """All code the learner would see/run: the `code` field, FIB `code_lines`, and the fill."""
    parts = [lean.get("code") or ""]
    parts += [str(l) for l in (lean.get("code_lines") or [])]
    parts.append(lean.get("blank_answer") or "")
    return "\n".join(p for p in parts if p)


def _code_external_dep(lean: dict) -> str | None:
    """The first external-resource accessor in the question's code, else None."""
    m = _CODE_EXTERNAL_RE.search(_code_text(lean))
    return m.group(0) if m else None


def _norm_code_line(s: str) -> str:
    return " ".join((s or "").split())


# A handful of structural lines are common to almost any program and copying them is not
# "lifting the example" — don't count them toward a verbatim run.
_TRIVIAL_CODE_LINES = {"pass", "return", "break", "continue", "else:", "try:", "{", "}",
                       "});", "()", "end", "begin", "main()", "return 0;"}


def _verbatim_code(lean: dict, ctx: str) -> str | None:
    """Flag when the question's code reproduces the source WHOLESALE. Returns a short
    sample of the copied lines when the snippet is substantially lifted from the
    material — at least 4 non-trivial lines copied AND >= 70% of its non-trivial lines
    present (whitespace-normalized) in the material — else None. The ratio gate (not a
    bare consecutive-line count) keeps formulaic lines a real snippet must share with
    any example (a `for x in items:` header, a `def f(...):` signature) from
    false-flagging a question that only borrows the taught pattern."""
    code = _code_text(lean)
    if not code.strip() or not ctx:
        return None
    src = {_norm_code_line(l) for l in ctx.splitlines() if len(_norm_code_line(l)) >= 8}
    if not src:
        return None
    nontrivial = [_norm_code_line(l) for l in code.splitlines()
                  if len(_norm_code_line(l)) >= 8 and _norm_code_line(l).lower() not in _TRIVIAL_CODE_LINES]
    if len(nontrivial) < 4:
        return None  # too short to judge deterministically — leave it to the LLM reviewer
    matched = [l for l in nontrivial if l in src]
    if len(matched) >= 4 and len(matched) / len(nontrivial) >= 0.7:
        return " / ".join(matched[:3])
    return None


# A stem that points at a snippet the learner can SEE ("the following code", "output of the
# given program", "what will the following snippet print"). On an option type with no code
# field this is a dangling reference to invisible code.
_PHANTOM_CODE_RE = re.compile(
    r"\b(?:the |this )?(?:following|given|above|below)\s+(?:code|program|snippet|script|function|query)\b"
    r"|\boutput of (?:the )?(?:following|given|this|above|code|program|snippet)\b"
    r"|\bwhat (?:will|does) the (?:following|given|above|code|program|snippet)\b",
    re.I,
)


class _DistractorVerdict(BaseModel):
    index: int = Field(description="the 1-based index of the distractor being judged "
                                   "(matches the numbered DISTRACTORS list in the prompt)")
    evaluable: bool = Field(description="true if a learner could tell WHY this distractor is "
                                        "wrong using ONLY the material, at the required depth")
    reason: str = Field(description="one line: what understanding is needed, and whether the material teaches it")


class _DistractorBatchVerdict(BaseModel):
    verdicts: List[_DistractorVerdict] = Field(
        description="exactly one verdict per numbered distractor, carrying that distractor's index")


def _audit_distractor_depth(lo: dict, qtype: str, lean: dict, ctx: str) -> list[dict]:
    """For understand+/apply LOs, verify each WRONG option is evaluable from taught material at
    the expected depth (one Bloom level below the LO). ONE batched LLM call per question (all
    distractors judged together) instead of one call per distractor; never blocks review on its
    own failure. Recall-level LOs skip (distractors are recognition)."""
    if qtype not in OPTION_TYPES:
        return []
    bloom = (lo.get("bloom_category") or "").lower()
    if bloom not in ("understand", "apply", "implement"):
        return []
    depth = lo.get("expected_distractor_depth") or ("understand" if bloom != "understand" else "remember")
    concept = lo.get("concept") or lo.get("description") or ""

    # Number the distractors so the batched verdict can map back by index.
    distractors = [(o.get("content") or "").strip()
                   for o in (lean.get("options") or []) if not o.get("is_correct")]
    distractors = [d for d in distractors if d]
    if not distractors:
        return []

    numbered = "\n".join(f"{i}. {d}" for i, d in enumerate(distractors, start=1))
    usr = (f"TARGET CONCEPT: {concept}\nREQUIRED DEPTH to judge a distractor: {depth}\n\n"
           f"DISTRACTORS (each a wrong option):\n{numbered}\n\n"
           f"MATERIAL (ground truth):\n{(ctx or '')[:8000]}")
    try:
        batch = _review_model(0).with_structured_output(_DistractorBatchVerdict).invoke(
            [{"role": "system", "content": get_prompt("review.distractor_depth_audit", _DISTRACTOR_DEPTH_AUDIT)},
             {"role": "user", "content": usr}])
    except Exception:  # noqa: BLE001 — never block review on the audit
        return []

    issues: list[dict] = []
    for v in (batch.verdicts or []):
        if v.evaluable:
            continue
        if not (1 <= v.index <= len(distractors)):   # guard against an out-of-range index
            continue
        content = distractors[v.index - 1]
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
