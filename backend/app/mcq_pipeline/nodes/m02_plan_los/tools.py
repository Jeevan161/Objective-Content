"""plan_los · deterministic tools — the concept dependency graph (relocated from the old
build_outcome_graph node).

These are the "code" side of "agent proposes, code enforces": the LLM-driven parts that are
DRIFT-PRONE (a prerequisite DAG) stay behind K-sample majority voting rather than a single
free-form agent answer, exactly as the staged pipeline did. The agent decides WHICH concepts
exist; this module derives the edges, weights, procedurality, and assumed-prior between them.

`build_graph(inventory, outcomes)` returns (concept_graph, inventory+procedural, outcomes+metrics,
outcome_graph) — the same four artifacts the old m04 node produced, so everything downstream
(prerequisite resolution, sequencing, the portal map) is unchanged.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict

from app.mcq_pipeline.config import GRAPH_K_SAMPLES, GRAPH_MAJORITY, TEMP_GRAPH
from app.mcq_pipeline.prompts.store import get_prompt, register
from app.mcq_pipeline.utils.concept_graph import reachable
from app.mcq_pipeline.utils.concurrency import pmap
from app.mcq_pipeline.utils.llm import chat, parse_json

# Same prompt key + contract as the old build_outcome_graph node (DB-overridable, reused unchanged).
_GRAPH_SYS = register("lo.graph_sys", (
    "You analyze ONE target concept from a learning session against OTHER concepts "
    "in the same session and output ONLY dependency metadata for the TARGET concept.\n\n"

    "Return ONLY JSON in this format:\n"
    '{"prerequisites": ["<concept_id>", ...], '
    '"applied_skill": <bool>, '
    '"assumed_prior": ["<short prior-knowledge name>", ...]}\n\n'

    "A concept X is a prerequisite of target T ONLY IF: T requires understanding/using X, T cannot "
    "be correctly understood or performed without X, and X is directly used inside T's explanation, "
    "steps, or reasoning. Do NOT include 'supporting'/'related'/'helpful' concepts, general "
    "background, parent concepts already captured by the child, or multi-hop (A→B→C) edges. If "
    "uncertain, DO NOT include the edge.\n\n"

    "applied_skill = true if the target is something the learner DOES/USES — performs steps, solves "
    "a problem, applies a method/algorithm, constructs output, OR evaluates/computes an expression, "
    "applies an operator/rule/condition, decides based on a condition, or transforms data. Operators, "
    "logical/conditional operations, computations, and procedures are applied skills. false ONLY for "
    "purely definitional/recognition/conceptual targets with no use. If both conceptual and applied, "
    "classify by the PRIMARY assessment behavior.\n\n"

    "assumed_prior: include ONLY knowledge that is NOT in the given concept list and is commonly "
    "assumed before this session (e.g. 'basic algebra', 'file system basics'). Never include "
    "anything that exists as a concept_id in the session.\n\n"

    "Return ONLY valid JSON. No explanation, no markdown.\n"
))


def _line(c: dict) -> str:
    return f'{c["concept_id"]}: {c["canonical_name"]} (evidence: "{(c.get("evidence") or {}).get("quote", "")[:100]}")'


def _concept_votes(c: dict, inv: list, idset: set) -> tuple:
    """K-sample prerequisite / procedural / assumed-prior vote for ONE concept (pure + isolated, so
    concepts vote IN PARALLEL). Returns (concept_id, prereq_vote_counter, applied_skill_count,
    assumed_prior_terms)."""
    cid = c["concept_id"]
    others = "\n".join(_line(o) for o in inv if o["concept_id"] != cid) or "(none)"
    usr = f"TARGET CONCEPT:\n{_line(c)}\n\nOTHER CONCEPTS IN THIS SESSION:\n{others}"
    pv, skill, prior_terms = Counter(), 0, []
    for _ in range(GRAPH_K_SAMPLES):
        d = parse_json(chat([{"role": "system", "content": get_prompt("lo.graph_sys", _GRAPH_SYS)},
                             {"role": "user", "content": usr}], temperature=TEMP_GRAPH)) or {}
        if not isinstance(d, dict):
            continue
        for p in d.get("prerequisites", []):
            if p in idset and p != cid:
                pv[p] += 1
        if d.get("applied_skill") is True:
            skill += 1
        for ap in d.get("assumed_prior", []):
            if isinstance(ap, str) and ap.strip():
                prior_terms.append(re.sub(r"\s+", " ", ap.strip().lower()))
    return cid, pv, skill, prior_terms


def _concept_metrics(graph: dict) -> dict:
    """Per-concept (weight, dag_depth). weight = |transitive dependents| (foundational-ness);
    dag_depth = longest prerequisite chain INTO the concept (0 = foundational)."""
    adj = {k: list(v) for k, v in (graph.get("_adj") or {}).items()}
    nodes = list(graph.get("nodes") or [])
    rev: dict = defaultdict(list)
    for e in graph.get("edges", []):
        rev[e["to"]].append(e["from"])

    def _descendants(x, seen):
        for y in adj.get(x, []):
            if y not in seen:
                seen.add(y)
                _descendants(y, seen)
        return seen

    def _depth(x, memo, stack):
        if x in memo:
            return memo[x]
        if x in stack:
            return 0
        stack.add(x)
        d = 0 if not rev.get(x) else 1 + max(_depth(p, memo, stack) for p in rev[x])
        stack.discard(x)
        memo[x] = d
        return d

    memo: dict = {}
    return {n: {"weight": len(_descendants(n, set())), "dag_depth": _depth(n, memo, set())}
            for n in nodes}


def build_graph(inv: list, outcomes: list, on_done=None) -> tuple[dict, list, list, dict]:
    """Build the concept DAG (K-sample voting, concepts probed in parallel), derive per-concept
    weight + dag_depth, set `procedural` from the applied_skill majority, and stamp every outcome
    with its concept's metrics. Returns (concept_graph, inventory, outcomes, outcome_graph)."""
    ids = [c["concept_id"] for c in inv]
    idset = set(ids)

    def _work(c):
        r = _concept_votes(c, inv, idset)
        if on_done:
            on_done()
        return r

    prereq_votes: dict = {}
    skill_votes, prior = Counter(), Counter()
    for cid, pv, skill, prior_terms in pmap(_work, inv):
        prereq_votes[cid] = pv
        if skill:
            skill_votes[cid] = skill
        for t in prior_terms:
            prior[t] += 1

    adj, edges = defaultdict(set), []
    candidates = sorted(((p, cid, v) for cid, pv in prereq_votes.items()
                         for p, v in pv.items() if v >= GRAPH_MAJORITY),
                        key=lambda e: (-e[2], e[0], e[1]))
    for (p, cid, v) in candidates:
        if not reachable(adj, cid, p):
            adj[p].add(cid)
            edges.append({"from": p, "to": cid, "relation": "depends_on"})
    assumed = [p for p, v in prior.items() if v >= GRAPH_MAJORITY]

    topic_of = {c["concept_id"]: c["topic_id"] for c in inv}
    tadj, topic_edges = defaultdict(set), []
    for (tp, tc) in sorted({(topic_of[e["from"]], topic_of[e["to"]]) for e in edges
                            if topic_of.get(e["from"]) and topic_of.get(e["to"])
                            and topic_of[e["from"]] != topic_of[e["to"]]}):
        if not reachable(tadj, tc, tp):
            tadj[tp].add(tc)
            topic_edges.append({"from": tp, "to": tc, "relation": "depends_on"})

    graph = {"nodes": ids, "edges": edges, "_adj": {k: sorted(v) for k, v in adj.items()},
             "topic_nodes": sorted({c["topic_id"] for c in inv}), "topic_edges": topic_edges,
             "_topic_adj": {k: sorted(v) for k, v in tadj.items()},
             "assumed_prior": assumed, "acyclic": True}

    procedural_ids = {cid for cid, v in skill_votes.items() if v >= GRAPH_MAJORITY}
    new_inv = [{**c, "procedural": c["concept_id"] in procedural_ids} for c in inv]

    metrics = _concept_metrics(graph)
    out = []
    for o in outcomes:
        m = metrics.get(o.get("concept_id"), {"weight": 0, "dag_depth": 0})
        out.append({**o, "weight": m["weight"], "dag_depth": m["dag_depth"]})

    by_concept: dict = defaultdict(list)
    for o in out:
        by_concept[o["concept_id"]].append(o["id"])
    o_edges = []
    for e in edges:
        for src in by_concept.get(e["from"], []):
            for dst in by_concept.get(e["to"], []):
                o_edges.append({"from": src, "to": dst, "relation": "depends_on"})
    outcome_graph = {"nodes": [o["id"] for o in out], "edges": o_edges,
                     "weights": {o["id"]: o["weight"] for o in out}}
    return graph, new_inv, out, outcome_graph
