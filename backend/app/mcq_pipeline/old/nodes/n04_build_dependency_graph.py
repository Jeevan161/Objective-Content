"""LO pipeline · Node 4 — build_dependency_graph (K-sample edge voting)."""
from __future__ import annotations

import re
from collections import Counter, defaultdict

from app.mcq_pipeline.utils.concurrency import pmap
from app.mcq_pipeline.config import K_SAMPLES, MAJORITY, TEMP_GRAPH
from app.mcq_pipeline.utils.concept_graph import reachable
from app.mcq_pipeline.utils.llm import chat, parse_json
from app.mcq_pipeline.prompts.store import get_prompt, register
from app.mcq_pipeline.nodes._common import _bind_rag, _prog


# ── Node 4 · build_dependency_graph (A · K-sample edge voting) ────────────── #
_GRAPH_SYS = register("lo.graph_sys", (
    "You analyze ONE target concept from a learning session against OTHER concepts "
    "in the same session and output ONLY dependency metadata for the TARGET concept.\n\n"

    "Return ONLY JSON in this format:\n"
    '{"prerequisites": ["<concept_id>", ...], '
    '"applied_skill": <bool>, '
    '"assumed_prior": ["<short prior-knowledge name>", ...]}\n\n'

    "----------------------------\n"
    "CORE TASK\n"
    "----------------------------\n"
    "Determine ONLY DIRECT LEARNING DEPENDENCIES for the target concept.\n"
    "Do NOT build indirect chains or extended prerequisite trees.\n\n"

    "----------------------------\n"
    "1. PREREQUISITES (STRICT RULE)\n"
    "----------------------------\n"
    "A concept X is a prerequisite of target T ONLY IF ALL are true:\n"
    "1. T explicitly or implicitly REQUIRES understanding or ability to use X\n"
    "2. Without X, T cannot be correctly understood or performed\n"
    "3. X is directly used inside T's explanation, steps, or reasoning\n\n"

    "IMPORTANT FILTERS:\n"
    "- Do NOT include 'supporting', 'related', or 'helpful' concepts\n"
    "- Do NOT include general background unless it is structurally required\n"
    "- Do NOT include parent concepts if the child already captures dependency\n"
    "- Do NOT infer multi-hop dependencies (A → B → C, only A → C is invalid)\n\n"

    "If uncertain → DO NOT include the edge.\n\n"

    "----------------------------\n"
    "2. APPLIED_SKILL CLASSIFICATION\n"
    "----------------------------\n"
    "Set applied_skill = true ONLY if the target concept is something the learner EXECUTES.\n\n"

    "TRUE if it involves:\n"
    "- performing steps\n"
    "- solving a problem\n"
    "- applying a method or algorithm\n"
    "- constructing or producing an output\n\n"

    "FALSE if it involves:\n"
    "- definitions\n"
    "- recognition or identification\n"
    "- conceptual understanding without execution\n\n"

    "Edge case rule:\n"
    "- If it is both conceptual + applied, classify by PRIMARY assessment behavior.\n\n"

    "----------------------------\n"
    "3. ASSUMED_PRIOR (EXTERNAL KNOWLEDGE ONLY)\n"
    "----------------------------\n"
    "Include ONLY knowledge that:\n"
    "- is NOT present in the given concept list\n"
    "- is commonly assumed before learning this session\n\n"

    "Examples:\n"
    "- 'basic algebra'\n"
    "- 'file system basics'\n"
    "- 'programming fundamentals'\n\n"

    "Do NOT include anything that exists as a concept_id in the session.\n\n"

    "----------------------------\n"
    "NEGATIVE CONSTRAINTS (VERY IMPORTANT)\n"
    "----------------------------\n"
    "- Do NOT create extra concepts\n"
    "- Do NOT paraphrase concept IDs\n"
    "- Do NOT infer missing session concepts\n"
    "- Do NOT over-connect based on similarity of words\n"
    "- Only connect based on functional dependency in learning\n\n"

    "----------------------------\n"
    "DECISION HEURISTIC\n"
    "----------------------------\n"
    "Ask internally:\n"
    "- Does learning T FAIL without X? → prerequisite\n"
    "- Is X just context or explanation support? → ignore\n"
    "- Is X part of the same step-level mechanism? → prerequisite\n"
    "- Is X only loosely related? → ignore\n\n"

    "----------------------------\n"
    "OUTPUT CONSTRAINT\n"
    "----------------------------\n"
    "Return ONLY valid JSON. No explanation, no markdown.\n"
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
                # Normalize (case + whitespace) before tallying so phrasing variants
                # ("Basic Algebra" / "basic  algebra") don't split the vote.
                if isinstance(ap, str) and ap.strip():
                    prior[re.sub(r"\s+", " ", ap.strip().lower())] += 1
        prereq_votes[cid] = pv
        on_done()

    # majority-voted prerequisites -> edges P->C (P is a prerequisite of C), with cycle guard
    adj, edges, logs = defaultdict(set), [], []
    # Resolve 2-cycles in favor of the STRONGER edge: order by vote count desc (then id) so the
    # higher-confidence edge is added first and its weaker reverse is the one the guard drops.
    candidates = sorted(((p, cid, v) for cid, pv in prereq_votes.items()
                         for p, v in pv.items() if v >= MAJORITY),
                        key=lambda e: (-e[2], e[0], e[1]))
    for (p, cid, v) in candidates:
        if not reachable(adj, cid, p):          # adding p->cid is safe if cid can't already reach p
            adj[p].add(cid)
            edges.append({"from": p, "to": cid, "relation": "depends_on"})
        else:
            logs.append({"node": "build_graph", "dropped_edge": [p, cid], "reason": "would_create_cycle"})
    # NOTE: `prior` accumulates across ALL concepts x K samples (unlike the per-concept prereq
    # votes), so MAJORITY here is a weaker, session-wide bar than the per-concept threshold.
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


