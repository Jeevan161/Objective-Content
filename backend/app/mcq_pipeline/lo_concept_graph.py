"""
app/mcq_pipeline/lo_concept_graph.py
-------------------------------------
The deterministic core of the LO pipeline — pure functions, NO LLM, NO DB, NO
module-level mutable state. These are the parts that make the pipeline
reproducible: text grounding (`ground_quote`/`syntax_grounded`), the slot
allocator (`largest_remainder`), the apply-suitability signal (`_is_procedural`),
and the graph-backed prerequisite tools (`graph_check_concept` /
`graph_find_prerequisites`) that read the run's OWN frozen concept DAG.

Ported verbatim (logic-for-logic) from the POC `_build_lo.py`.
"""

from __future__ import annotations

import re
from collections import defaultdict

from .lo_config import ALIAS_MAP


# --- text utilities -------------------------------------------------------- #
def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")
    return re.sub(r"_+", "_", s) or "x"


def ground_quote(term: str, text: str, width: int = 180) -> str:
    """Cut a VERBATIM (whitespace-collapsed) snippet from `text` near `term`, so every
    evidence quote provably resolves to the source (V9 becomes deterministic)."""
    t = text or ""
    low = t.lower()
    for w in sorted(re.findall(r"[A-Za-z_]{3,}", term or ""), key=len, reverse=True):
        i = low.find(w.lower())
        if i != -1:
            start = t.rfind("\n", 0, i) + 1
            return re.sub(r"\s+", " ", t[start:start + width]).strip()
    return re.sub(r"\s+", " ", t[:width]).strip()


def loosen_text(t: str) -> str:
    """Markdown/whitespace-insensitive flattening (shared by syntax + quote grounding)."""
    return re.sub(r"\s+", " ", re.sub(r"[*_`#>]+", " ", (t or "").lower())).strip()


_DESC_STOP = {"this", "that", "with", "from", "into", "your", "they", "them", "then",
              "than", "such", "when", "what", "which", "while", "where", "have", "will",
              "would", "could", "should", "about", "these", "those", "their", "there",
              "also", "used", "uses", "using", "between", "different", "example"}


def description_grounded(description: str, evidence_quotes, section_text: str,
                         threshold: float = 0.55) -> bool:
    """True if a concept's prose DESCRIPTION traces to the material (its evidence quotes
    or section text) rather than outside knowledge — the evidence-binding gate of the LO
    quality model. Lenient content-word overlap: most non-trivial words in the
    description must appear in the source. An empty description is NOT grounded."""
    src = loosen_text(" \n ".join(evidence_quotes or []) + " \n " + (section_text or ""))
    if not src:
        return False
    words = {w for w in re.findall(r"[a-z][a-z0-9]{3,}", (description or "").lower())
             if w not in _DESC_STOP}
    if not words:
        return False
    hits = sum(1 for w in words if w in src)
    return hits / len(words) >= threshold


def syntax_grounded(syntax: str | None, text: str) -> bool:
    """True if an Apply outcome's syntax/command resolves to the reading material.
    Holds `syntax` to the same grounding bar as the evidence quote (V9). A null
    syntax (non-code outcome) is always grounded."""
    if not syntax:
        return True
    src_t, snip = loosen_text(text), loosen_text(syntax)
    if not snip or snip in src_t:
        return True
    toks = re.findall(r"[a-z_][a-z0-9_]{2,}", snip)
    return True if not toks else all(t in src_t for t in toks)


_FENCE = re.compile(r"```[a-zA-Z0-9]*\n(.*?)```", re.S)


def recover_syntax(text: str) -> str | None:
    """Pull the first line of the first fenced code block from a section as grounded
    syntax (repairs an Apply outcome whose authored syntax failed grounding). None if
    the section has no code block."""
    m = _FENCE.search(text or "")
    if not m:
        return None
    lines = [ln.strip() for ln in m.group(1).strip().splitlines() if ln.strip()]
    return lines[0] if lines else None


# --- procedurality / canonicalization -------------------------------------- #
# Apply-suitability signal — STRICT, evidence-based (BR2 / V8). A concept is
# "procedural" only when its section shows something to *do*: a fenced code block,
# OR a STRONG operational cue. Loose prose verbs are deliberately excluded.
_STRONG_PROC = re.compile(
    r"\b(syntax|command|compute|calculate|execute|implement|invoke|debug|trace|"
    r"evaluate|construct|instantiate|initiali[sz]e)\b|run\s+the|step\s*\d", re.I)


def is_procedural(quote: str, canon: str, section_text: str) -> bool:
    """CONCEPT-scoped procedurality. A concept is procedural only when it is itself
    tied to something to *do*: its name tokens appear INSIDE a fenced code block of
    the section, OR a strong operational cue occurs in the concept's own quote/name.
    (Previously this scanned the whole section, so one unrelated `pip install` fence
    or one strong-cue word marked EVERY concept in the section procedural.)"""
    fences = "\n".join(_FENCE.findall(section_text or "")).lower()
    name_toks = set(re.findall(r"[a-z_][a-z0-9_]{2,}", (canon or "").lower()))
    in_fence = bool(name_toks) and any(t in fences for t in name_toks)
    strong = bool(_STRONG_PROC.search(quote or "") or _STRONG_PROC.search(canon or ""))
    return in_fence or strong


# Installation / shell / environment-setup activities read as "procedural" (they have
# commands and operational cues) but are NOT a valid apply/code target: they are not
# runnable input->output programs (a venv/env name is arbitrary, so nothing
# deterministic to grade), so they must be assessed CONCEPTUALLY (MCQ), never as
# apply / FIB / code-analysis. Reports #3/#4: fake-apply FIBs for "create a venv".
_SETUP_CLI_RE = re.compile(
    r"\b(?:pip3?|conda|apt|apt-get|brew|npm|npx|yarn|pnpm|gem|cargo|gradle|mvn|go)\s+"
    r"(?:install|add|i|get|remove|uninstall|init)\b"
    r"|\bpython3?\s+-m\s+(?:venv|pip)\b"
    r"|\b(?:virtualenv|venv|mkvirtualenv|pipenv|poetry)\b"
    r"|\bsource\s+\S+/bin/activate\b|\b(?:de)?activate\b"
    r"|\b(?:mkdir|rmdir|chmod|chown|sudo|export|cd)\b"
    r"|\b(?:virtual|global|shared|isolated)\s+environments?\b"
    r"|\b(?:install(?:ing|ation|ed|s)?|set(?:ting)?[ -]up|setup|environment setup|"
    r"command[ -]line|terminal|shell prompt)\b",
    re.I,
)


def is_setup_or_cli(quote: str, canon: str, section_text: str = "") -> bool:
    """True when a concept is an installation / shell / environment-setup activity
    rather than an executable coding skill. Such concepts are conceptual-for-assessment
    (test via MCQ), never apply / FIB / code-analysis. Judged on the concept's OWN
    name + evidence quote (not the whole section), so a code section that merely
    mentions `pip install` in passing does not taint unrelated concepts."""
    return bool(_SETUP_CLI_RE.search(quote or "") or _SETUP_CLI_RE.search(canon or ""))


# --- LO grounding-DEPTH signals (V12: over-reach beyond what's taught) ------- #
# An outcome's cognitive demand must match what the material TEACHES, not merely
# mention. These measure taught depth + whether a comparison is explicitly taught, so
# the validator can flag over-reach (e.g. "differentiate Flask and FastAPI" minted from
# a one-line overview that never contrasts them).
_SENT_RE = re.compile(r"[.!?\n]+")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9+#.]{2,}")
_CONTRAST_CUE = re.compile(
    r"\b(?:but|whereas|while|unlike|versus|vs|compared|contrast|contrasts|differ|"
    r"differs|different|differences?|rather than|instead of|than|more|less|faster|"
    r"slower|lighter|heavier|simpler|smaller|larger|better|worse|prefer|over)\b", re.I)
_DEPTH_STOP = {
    "the", "a", "an", "of", "for", "and", "or", "to", "in", "on", "with", "as", "is",
    "are", "be", "its", "their", "this", "that", "these", "those", "by", "from", "into",
    "each", "both", "all", "any", "use", "used", "using", "via", "per", "between",
    "about", "such", "etc", "framework", "frameworks", "capability", "capabilities",
    "characteristic", "characteristics", "feature", "features", "concept", "concepts",
    "type", "types", "kind", "kinds", "way", "ways", "terms",
}


def _depth_tokens(text: str) -> set[str]:
    return {w.lower() for w in _WORD_RE.findall(text or "")
            if w.lower() not in _DEPTH_STOP and len(w) >= 3}


def concept_depth(name: str, source_text: str) -> int:
    """Number of source sentences that TEACH this concept (mention a content token of
    its name). A concept taught in depth recurs; a one-line mention scores <= 1."""
    toks = _depth_tokens(name)
    if not toks:
        return 0
    return sum(1 for s in _SENT_RE.split((source_text or "").lower())
               if any(t in s for t in toks))


def has_explicit_contrast(text: str, source_text: str) -> bool:
    """True when the source EXPLICITLY contrasts the outcome's entities — a contrast
    cue in a sentence that mentions one of them. Distinguishes a genuinely taught
    comparison from items the material only mentions separately (one line each)."""
    toks = _depth_tokens(text)
    if not toks:
        return False
    rel = [s for s in _SENT_RE.split((source_text or "").lower()) if any(t in s for t in toks)]
    return any(_CONTRAST_CUE.search(s) for s in rel)


def canonical_name(name: str) -> str:
    n = re.sub(r"\s+", " ", (name or "").strip().lower())
    return ALIAS_MAP.get(n, n)


# --- allocation ------------------------------------------------------------ #
def largest_remainder(weights: list[int], total: int) -> list[int]:
    """Distribute `total` slots across topics by weight (largest-remainder method)."""
    s = sum(weights)
    if s <= 0 or total <= 0:
        return [0] * len(weights)
    quotas = [w / s * total for w in weights]
    alloc = [int(q // 1) for q in quotas]
    rem = total - sum(alloc)
    for i in sorted(range(len(weights)), key=lambda i: (-(quotas[i] - alloc[i]), i))[:rem]:
        alloc[i] += 1
    return alloc


# --- graph ops ------------------------------------------------------------- #
def reachable(adj: dict, src, dst) -> bool:
    seen, stack = set(), [src]
    while stack:
        n = stack.pop()
        if n == dst:
            return True
        if n in seen:
            continue
        seen.add(n)
        stack.extend(adj.get(n, ()))
    return False


def graph_check_concept(state: dict, concept_id: str) -> dict:
    """Node lookup + in-scope flag over the frozen inventory/DAG (PRD check_concept).
    Deterministic — not an LLM/RAG call."""
    c = next((c for c in state["concept_inventory"] if c["concept_id"] == concept_id), None)
    if c:
        return {"concept_id": concept_id, "exists": True, "in_scope": c["in_scope"],
                "canonical_name": c["canonical_name"], "topic_id": c["topic_id"],
                "procedural": c["procedural"]}
    assumed = concept_id in {"C_" + slugify(p) for p in state["concept_graph"]["assumed_prior"]}
    return {"concept_id": concept_id, "exists": assumed, "in_scope": assumed,
            "assumed_prior": assumed}


def graph_find_prerequisites(state: dict, concept_id: str) -> list[str]:
    """Ancestor (prerequisite) closure of a concept in the DAG (PRD find_prerequisites).
    Deterministic — not an LLM/RAG call."""
    adj = state["concept_graph"].get("_adj", {})
    parents = defaultdict(set)
    for src, dsts in adj.items():
        for d in dsts:
            parents[d].add(src)
    closure, stack = set(), list(parents.get(concept_id, ()))
    while stack:
        n = stack.pop()
        if n in closure:
            continue
        closure.add(n)
        stack.extend(parents.get(n, ()))
    return sorted(closure)
