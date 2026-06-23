"""LO pipeline · Node 4 — build_dependency_graph (K-sample edge voting)."""
from __future__ import annotations

from collections import Counter, defaultdict

from app.mcq_pipeline.utils.concurrency import pmap
from app.mcq_pipeline.config import K_SAMPLES, MAJORITY, TEMP_GRAPH
from app.mcq_pipeline.utils.concept_graph import reachable
from app.mcq_pipeline.utils.llm import chat, parse_json
from app.mcq_pipeline.prompts.store import get_prompt, register
from app.mcq_pipeline.nodes._common import _bind_rag, _prog


# ── Node 4 · build_dependency_graph (A · K-sample edge voting) ────────────── #
_GRAPH_SYS = register("lo.graph_sys", (
    "You analyze ONE target concept from a reading (ANY subject) against the OTHER concepts "
    "taught in the same session, and output JSON about the TARGET only.\n"
    'Output ONLY JSON: {"prerequisites": ["<concept_id>", ...], "applied_skill": <bool>, '
    '"assumed_prior": ["<short prior-knowledge name>", ...]}.\n'
    "- prerequisites: which of the OTHER given concept_ids must be understood BEFORE the "
    "target (are its direct prerequisites). Use ONLY ids from the given list; [] if none.\n"
    "- applied_skill: true ONLY if the target is a PERFORMABLE SKILL the learner actively "
    "carries out or APPLIES (solve a problem, compute a value, apply a method/framework to a "
    "case, construct an argument, produce an artifact, execute steps) — NOT a fact, "
    "definition, or idea merely recognized or explained. DOMAIN-GENERAL: include "
    "non-programming skills, not just code.\n"
    "- assumed_prior: foundational knowledge the target ASSUMES but that is NOT taught in "
    "this session (short generic names); [] if none. Anything the target needs that is "
    "OUTSIDE the given concept list belongs here, NOT in prerequisites.\n"
    "Judge ONLY from what the material teaches; do not invent."
))


def build_dependency_graph(state, config) -> dict:
    """Build the prerequisite DAG + applied-skill + assumed-prior signals ONE concept at a
    time (sequential, isolated), with K-sample self-consistency per concept. assumed_prior is
    LLM-derived per concept (no hardcoded fallback)."""
    _bind_rag(config)
    prog = _prog(config)
    inv = state["concept_inventory"]
    ids = [c["concept_id"] for c in inv]
    idset = set(ids)

    def _line(c):
        return f'{c["concept_id"]}: {c["canonical_name"]} (evidence: "{(c.get("evidence") or {}).get("quote", "")[:100]}")'

    on_done = prog.counter("build_dependency_graph", len(inv))
    prereq_votes: dict = {}                 # cid -> Counter(prereq_id -> votes)
    skill_votes, prior = Counter(), Counter()
    # ONE concept at a time (sequential, isolated); K-sample self-consistency per concept.
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
                    prior[ap.strip()] += 1
        prereq_votes[cid] = pv
        on_done()

    # majority-voted prerequisites -> edges P->C (P is a prerequisite of C), with cycle guard
    adj, edges, logs = defaultdict(set), [], []
    candidates = sorted((p, cid) for cid, pv in prereq_votes.items()
                        for p, v in pv.items() if v >= MAJORITY)
    for (p, cid) in candidates:
        if not reachable(adj, cid, p):          # adding p->cid is safe if cid can't already reach p
            adj[p].add(cid)
            edges.append({"from": p, "to": cid, "relation": "depends_on"})
        else:
            logs.append({"node": "build_graph", "dropped_edge": [p, cid], "reason": "would_create_cycle"})
    assumed = [p for p, v in prior.items() if v >= MAJORITY]   # LLM-derived; NO hardcoded default

    # Two-level graph (P2): lift concept edges to a TOPIC dependency DAG — topic B precedes
    # topic A when some concept of A depends on a concept of B. Deterministic (derived, no
    # extra LLM), cycle-guarded independently of the concept graph. Drives sequencing and
    # gives the portal a coarse structural map.
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

    # procedurality is decided SOLELY by the LLM applied_skill majority vote (no regex floor).
    procedural_ids = {cid for cid, v in skill_votes.items() if v >= MAJORITY}
    new_inv, added = [], []
    for c in inv:
        c2 = dict(c)
        c2["procedural"] = c2["concept_id"] in procedural_ids
        if c2["procedural"]:
            added.append(c2["concept_id"])
        new_inv.append(c2)

    logs.append({"node": "build_graph", "k": K_SAMPLES, "per_concept": True, "edges": len(edges),
                 "assumed_prior": assumed, "llm_procedural_promoted": added})
    prog.done("build_dependency_graph",
              detail=f"{len(ids)} nodes, {len(edges)} edges, +{len(added)} apply-skill (per-concept)")
    return {"concept_graph": graph, "concept_inventory": new_inv, "log": logs}


