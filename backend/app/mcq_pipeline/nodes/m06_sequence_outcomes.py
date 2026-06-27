"""LO pipeline · Node 6 — sequence_outcomes (deep-dive ordering)."""
from __future__ import annotations

import json

from app.mcq_pipeline.config import TIER_ORDER
from app.mcq_pipeline.utils.llm import chat, parse_json
from app.mcq_pipeline.prompts.store import get_prompt, register
from app.mcq_pipeline.utils._common import _prog

_BLOOM_RANK = {t: i for i, t in enumerate(TIER_ORDER)}   # remember<understand<apply<scenario


def _bloom_rank(level: str) -> int:
    """Rank a Bloom tier basic→advanced (remember 0 … scenario 3). Tolerant of legacy labels."""
    b = (level or "").lower()
    for t, i in _BLOOM_RANK.items():
        if b.startswith(t[:4]):
            return i
    return 1   # default ~understand when unknown


# ── Node 6 · sequence_outcomes (A · deep-dive ordering) ─────────────────── #
# Orders the final questions basic -> advanced as a coherent DEEP DIVE: driven by the
# prerequisite concept DAG + concept weights + topic order, AND — within each concept —
# a BASIC→ADVANCED Bloom progression (remember→understand→apply→scenario), so a concept is
# tested at a basic level before any advanced question on it. LLM-primary (prompt
# lo.sequence_sys); deterministic graph+weight+Bloom sort as the fallback.
_SEQUENCE_SYS = register("lo.sequence_sys", (
    "You order a set of learning outcomes into a STRICT pedagogical sequence that behaves like a "
    "DEPENDENCY-SAFE deep dive.\n\n"

    "PRIMARY GOAL:\n"
    "- Produce a single valid ordering where learning progresses from foundational → advanced.\n"
    "- Ensure each outcome appears exactly once.\n\n"

    "HARD CONSTRAINT (NON-NEGOTIABLE):\n"
    "- If outcome A depends on outcome B (via prerequisite concept edges), then B MUST appear before A.\n"
    "- This is a strict ordering constraint, not a suggestion.\n\n"

    "ORDERING PRINCIPLES (in priority order):\n"
    "1. PREREQUISITE VALIDITY (hard constraint)\n"
    "   - Never violate dependency edges between concepts.\n\n"

    "2. FOUNDATIONALITY (primary sorting signal)\n"
    "   - Lower dag_depth comes BEFORE higher dag_depth.\n"
    "   - Higher weight (more downstream dependents) comes earlier.\n\n"

    "3. BLOOM PROGRESSION — BASIC → ADVANCED WITHIN EACH CONCEPT (always)\n"
    "   - For a given concept, order its outcomes from LOWER to HIGHER Bloom level using `level`:\n"
    "     remember → understand → apply → scenario.\n"
    "   - A concept must be introduced/tested at a BASIC level (remember/understand) BEFORE any\n"
    "     ADVANCED question on it (apply / implement / scenario). 'understand' ALWAYS before 'apply'.\n"
    "   - Keep a concept's outcomes together and run them basic→advanced, then move to the next\n"
    "     concept. Never place an advanced outcome of a concept before its basic outcome.\n"
    "   - This applies WITHIN a concept; it must not override the prerequisite (DAG) constraint\n"
    "     across DIFFERENT concepts (a prerequisite concept still comes first as a whole).\n\n"

    "4. TOPIC FLOW COHERENCE (secondary constraint)\n"
    "   - Prefer keeping outcomes within the same topic_order together.\n"
    "   - Only move across topics when required by dependencies or stronger foundationality.\n\n"

    "5. CONCEPT COMPLEXITY PROGRESSION\n"
    "   - Simpler conceptual depth comes before complex depth.\n"
    "   - Use depth only as a tie-breaker after structural constraints.\n\n"

    "TIE-BREAK RULE (STRICT AND STABLE):\n"
    "- If two outcomes are otherwise equivalent, preserve original relative order by id.\n\n"

    "CRITICAL RULES:\n"
    "- Do NOT randomize ordering.\n"
    "- Do NOT reorder within the same concept unless required by dependency constraints.\n"
    "- Do NOT violate DAG structure even for better narrative flow.\n\n"

    "OUTPUT REQUIREMENT:\n"
    "- Return ONLY JSON.\n"
    "- Must include EVERY outcome id exactly once.\n"
    "- Must be a full permutation, not partial ordering.\n\n"

    'Return ONLY JSON: {"order": ["<outcome_id>", ...]}'
))


def _concept_metrics(graph: dict) -> dict:
    """Per-concept (weight, dag_depth) from the prerequisite DAG. _adj[A]=[B,...] means A is
    a prerequisite of B. weight = count of concepts that (transitively) depend on A
    (foundational-ness — higher sorts earlier); dag_depth = longest prerequisite chain
    leading INTO the concept (0 = foundational, higher = more advanced). Acyclic by
    construction; `seen` guards anyway."""
    adj = {k: list(v) for k, v in (graph.get("_adj") or {}).items()}
    nodes = list(dict.fromkeys(list(graph.get("nodes") or [])
                               + list(adj) + [b for vs in adj.values() for b in vs]))
    parents = {n: set() for n in nodes}
    for a, vs in adj.items():
        for b in vs:
            parents.setdefault(b, set()).add(a)

    desc_cache: dict = {}

    def descendants(n, seen=frozenset()):
        if n in desc_cache:
            return desc_cache[n]
        out = set()
        for m in adj.get(n, []):
            if m not in seen:
                out.add(m)
                out |= descendants(m, seen | {n})
        desc_cache[n] = out
        return out

    depth_cache: dict = {}

    def depth(n, seen=frozenset()):
        if n in depth_cache:
            return depth_cache[n]
        ps = [p for p in parents.get(n, ()) if p not in seen]
        d = 0 if not ps else 1 + max(depth(p, seen | {n}) for p in ps)
        depth_cache[n] = d
        return d

    return {n: {"weight": len(descendants(n)), "dag_depth": depth(n)} for n in nodes}


def sequence_outcomes(state, config) -> dict:
    """Order final_los into a basic->advanced deep dive. LLM-primary; on failure falls back
    to a deterministic graph+weight+topic sort. Domain-general (no verb/Bloom hardcoding)."""
    prog = _prog(config)
    los = state.get("final_los") or []
    if len(los) <= 1:
        return {}
    prog.start("sequence_outcomes")
    graph = state.get("concept_graph") or {}
    metrics = _concept_metrics(graph)
    topic_order = {s["topic_id"]: s.get("order", 99) for s in state.get("sections", [])}
    depth_by_c = {c["concept_id"]: c.get("depth_category", "moderate")
                  for c in (state.get("concept_inventory") or [])}

    def row(lo):
        cm = metrics.get(lo.get("concept_id"), {"weight": 0, "dag_depth": 0})
        return {"id": lo.get("outcome"), "concept": lo.get("concept"),
                "level": lo.get("bloom_level_raw") or lo.get("bloom_category"),
                "depth": depth_by_c.get(lo.get("concept_id"), "moderate"),
                "topic_order": topic_order.get(lo.get("source_section"), 99),
                "weight": cm["weight"], "dag_depth": cm["dag_depth"]}

    rows = [row(lo) for lo in los]
    by_id = {lo.get("outcome"): lo for lo in los}
    edges = [{"from": a, "to": b} for a, bs in (graph.get("_adj") or {}).items() for b in bs]

    order, source = None, "fallback"
    try:
        data = parse_json(chat(
            [{"role": "system", "content": get_prompt("lo.sequence_sys", _SEQUENCE_SYS)},
             {"role": "user", "content": f"OUTCOMES:\n{json.dumps(rows, ensure_ascii=False)}\n\n"
                                         f"PREREQUISITE EDGES (concept A must come before B):\n{json.dumps(edges)}"}],
            temperature=0)) or {}
        cand = [i for i in (data.get("order") or []) if i in by_id]
        if set(cand) == set(by_id):          # a valid permutation of EXACTLY the given ids
            order, source = cand, "LLM"
    except Exception:  # noqa: BLE001 — fall back to the deterministic graph+weight order
        order = None
    if order is None:
        # foundational concept first (topic → dag_depth → weight), keep each concept's outcomes
        # together, and run them BASIC → ADVANCED by Bloom rank within the concept.
        order = [r["id"] for r in sorted(
            rows, key=lambda r: (r["topic_order"], r["dag_depth"], -r["weight"],
                                 r["concept"] or "", _bloom_rank(r["level"]), r["id"]))]
    seq = [by_id[i] for i in order]
    snapshot = {"source": source, "count": len(seq),
                "order": [{"outcome": lo.get("outcome"), "concept": lo.get("concept"),
                           "level": lo.get("bloom_category")} for lo in seq]}
    prog.done("sequence_outcomes", detail=f"{source}: {len(seq)} outcomes, basic→advanced",
              snapshot=snapshot)
    return {"final_los": seq}
