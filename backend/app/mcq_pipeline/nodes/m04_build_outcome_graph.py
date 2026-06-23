"""LO pipeline (LO-first) · Node 4 — build_outcome_graph.

Step 3 of the LO-first flow: "Build a graph of the learning outcomes and add a WEIGHT for each
outcome." Built in two layers:

  1. A concept prerequisite DAG (same K-sample edge voting as the old pipeline), which also yields
     each concept's `applied_skill` (procedural) flag and the session's assumed-prior knowledge.
     Downstream prerequisite resolution and the artifact knowledge-map key off this concept graph.

  2. From that DAG we derive, per concept, a WEIGHT (= number of transitive dependents — how
     foundational it is) and a `dag_depth` (= longest prerequisite chain into it — how advanced it
     is). Every outcome inherits its concept's weight + dag_depth, and we emit an `outcome_graph`
     (edges lifted from the concept edges) so planning, sequencing, and the portal can reason at the
     outcome level. Heavier (more-foundational) outcomes are the ones planning prefers when the
     budget is scarce; lower dag_depth sorts earlier in the deep-dive order.

Input:  state["concept_inventory"], state["outcomes"]  (candidates from map_concepts).
Output: concept_graph, concept_inventory (procedural set), outcomes (weight + dag_depth stamped),
        outcome_graph = {nodes, edges, weights}  +  logs.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict

from app.mcq_pipeline.utils.concurrency import pmap
from app.mcq_pipeline.config import K_SAMPLES, MAJORITY, TEMP_GRAPH
from app.mcq_pipeline.utils.concept_graph import reachable
from app.mcq_pipeline.utils.llm import chat, parse_json
from app.mcq_pipeline.prompts.store import get_prompt, register
from app.mcq_pipeline.nodes._common import _bind_rag, _prog


# Reuse the proven concept-dependency voting prompt (same key, unchanged contract).
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

    "applied_skill = true ONLY if the target is something the learner EXECUTES (performs steps, "
    "solves a problem, applies a method/algorithm, constructs/produces output). false for "
    "definitions, recognition/identification, or conceptual understanding without execution. If both "
    "conceptual and applied, classify by the PRIMARY assessment behavior.\n\n"

    "assumed_prior: include ONLY knowledge that is NOT in the given concept list and is commonly "
    "assumed before this session (e.g. 'basic algebra', 'file system basics'). Never include "
    "anything that exists as a concept_id in the session.\n\n"

    "Return ONLY valid JSON. No explanation, no markdown.\n"
))


def _concept_metrics(graph: dict) -> dict:
    """Per-concept (weight, dag_depth) from the concept DAG. adj[X] = concepts that DEPEND ON X.
    weight = |transitive dependents of X| (foundational-ness); dag_depth = longest prerequisite
    chain INTO X (0 = foundational). Both drive planning + sequencing."""
    adj = {k: list(v) for k, v in (graph.get("_adj") or {}).items()}
    nodes = list(graph.get("nodes") or [])
    rev: dict = defaultdict(list)                       # rev[c] = prerequisites of c
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
        if x in stack:                                 # cycle guard (graph is acyclic, but be safe)
            return 0
        stack.add(x)
        d = 0 if not rev.get(x) else 1 + max(_depth(p, memo, stack) for p in rev[x])
        stack.discard(x)
        memo[x] = d
        return d

    memo: dict = {}
    return {n: {"weight": len(_descendants(n, set())), "dag_depth": _depth(n, memo, set())}
            for n in nodes}


def build_outcome_graph(state, config) -> dict:
    """Build the concept DAG (K-sample voting) + procedural flags, then derive per-outcome weights
    and the outcome-level graph. ONE concept at a time (isolated), K-sample self-consistency."""
    _bind_rag(config)
    prog = _prog(config)
    inv = state["concept_inventory"]
    ids = [c["concept_id"] for c in inv]
    idset = set(ids)

    def _line(c):
        return f'{c["concept_id"]}: {c["canonical_name"]} (evidence: "{(c.get("evidence") or {}).get("quote", "")[:100]}")'

    on_done = prog.counter("build_outcome_graph", len(inv))
    prereq_votes: dict = {}
    skill_votes, prior = Counter(), Counter()
    for c in inv:
        cid = c["concept_id"]
        others = "\n".join(_line(o) for o in inv if o["concept_id"] != cid) or "(none)"
        usr = f"TARGET CONCEPT:\n{_line(c)}\n\nOTHER CONCEPTS IN THIS SESSION:\n{others}"

        def _vote(_i, _usr=usr):
            return parse_json(chat([{"role": "system", "content": get_prompt("lo.graph_sys", _GRAPH_SYS)},
                                    {"role": "user", "content": _usr}], temperature=TEMP_GRAPH)) or {}

        pv = Counter()
        for d in pmap(_vote, list(range(K_SAMPLES))):
            if not isinstance(d, dict):
                continue
            for p in d.get("prerequisites", []):
                if p in idset and p != cid:
                    pv[p] += 1
            if d.get("applied_skill") is True:
                skill_votes[cid] += 1
            for ap in d.get("assumed_prior", []):
                if isinstance(ap, str) and ap.strip():
                    prior[re.sub(r"\s+", " ", ap.strip().lower())] += 1
        prereq_votes[cid] = pv
        on_done()

    # majority-voted prerequisite edges P->C, stronger-edge-first, cycle-guarded.
    adj, edges, logs = defaultdict(set), [], []
    candidates = sorted(((p, cid, v) for cid, pv in prereq_votes.items()
                         for p, v in pv.items() if v >= MAJORITY),
                        key=lambda e: (-e[2], e[0], e[1]))
    for (p, cid, v) in candidates:
        if not reachable(adj, cid, p):
            adj[p].add(cid)
            edges.append({"from": p, "to": cid, "relation": "depends_on"})
        else:
            logs.append({"node": "build_outcome_graph", "dropped_edge": [p, cid], "reason": "would_create_cycle"})
    assumed = [p for p, v in prior.items() if v >= MAJORITY]

    # topic-level DAG (lifted from concept edges), cycle-guarded independently.
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

    # procedurality = the LLM applied_skill majority vote (no regex floor).
    procedural_ids = {cid for cid, v in skill_votes.items() if v >= MAJORITY}
    new_inv = [{**c, "procedural": c["concept_id"] in procedural_ids} for c in inv]

    # Per-concept weight + dag_depth, then stamp every outcome with its concept's metrics.
    metrics = _concept_metrics(graph)
    outcomes = []
    for o in state["outcomes"]:
        m = metrics.get(o.get("concept_id"), {"weight": 0, "dag_depth": 0})
        outcomes.append({**o, "weight": m["weight"], "dag_depth": m["dag_depth"]})

    # outcome-level graph: lift each concept edge to the outcomes that sit on those concepts.
    by_concept: dict = defaultdict(list)
    for o in outcomes:
        by_concept[o["concept_id"]].append(o["id"])
    o_edges = []
    for e in edges:
        for src in by_concept.get(e["from"], []):
            for dst in by_concept.get(e["to"], []):
                o_edges.append({"from": src, "to": dst, "relation": "depends_on"})
    outcome_graph = {"nodes": [o["id"] for o in outcomes], "edges": o_edges,
                     "weights": {o["id"]: o["weight"] for o in outcomes}}

    logs.append({"node": "build_outcome_graph", "k": K_SAMPLES, "edges": len(edges),
                 "assumed_prior": assumed, "procedural": sorted(procedural_ids)})
    name_of = {c["concept_id"]: c["canonical_name"] for c in new_inv}
    snapshot = {"concept_count": len(ids), "edge_count": len(edges),
                "outcome_edge_count": len(o_edges),
                "procedural": [name_of.get(p, p) for p in sorted(procedural_ids)],
                "assumed_prior": assumed,
                "edges": [{"from": name_of.get(e["from"], e["from"]),
                           "to": name_of.get(e["to"], e["to"])} for e in edges[:40]],
                "weights": sorted(({"concept": name_of.get(c["concept_id"], c["concept_id"]),
                                    "weight": m["weight"], "dag_depth": m["dag_depth"]}
                                   for c, m in ((c, metrics.get(c["concept_id"], {"weight": 0, "dag_depth": 0}))
                                                for c in new_inv)),
                                   key=lambda x: -x["weight"])[:25]}
    prog.done("build_outcome_graph",
              detail=f"{len(ids)} concepts, {len(edges)} edges, {len(o_edges)} outcome edges",
              snapshot=snapshot)
    return {"concept_graph": graph, "concept_inventory": new_inv, "outcomes": outcomes,
            "outcome_graph": outcome_graph, "log": logs}
