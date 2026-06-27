"""plan_los · the four LangGraph nodes (option B — phases as first-class nodes).

The LO-planning work runs as four observable nodes wired in fixed order in the main graph:

    author_outcomes → consolidate_concepts → graph_outcomes → select_outcomes

Each is its own progress stage with independent retry + checkpointing; the agent sub-agents
(:mod:`agent`) supply the LLM judgment, the gate + tools (:mod:`gate`, :mod:`tools`) enforce the
invariants and assemble the state contract. Intermediate results flow through the `outcomes` /
`concept_inventory` channels exactly as the old staged pipeline did (complete-return REPLACE).

There is no legacy fallback (replace-outright); an unrecoverable phase raises ESCALATE.
"""
from __future__ import annotations

from collections import Counter

from app.mcq_pipeline.config import QUESTION_BUDGET
from app.mcq_pipeline.utils.concurrency import pmap
from app.mcq_pipeline.utils._common import _bind_rag, _ctx, _prog
from app.mcq_pipeline.nodes.m02_plan_los import agent, gate, tools
from app.mcq_pipeline.nodes.m02_plan_los.prompts import generate_sys_verb_subbed


def author_outcomes(state, config) -> dict:
    """PHASE 1 · author candidate outcomes per section (parallel)."""
    _bind_rag(config)
    prog = _prog(config)
    sections = state["sections"]
    sys = generate_sys_verb_subbed()
    on_done = prog.counter("author_outcomes", len(sections))

    def _one(topic):
        protos = agent.author_section(topic, sys)
        on_done()
        return protos

    protos = [p for batch in pmap(_one, sections) for p in batch]
    if not protos:
        raise RuntimeError("ESCALATE: no candidate outcomes could be authored from the source.")
    prog.done("author_outcomes", detail=f"{len(protos)} candidate outcomes",
              snapshot={"candidates": len(protos)})
    return {"outcomes": protos,
            "log": [{"node": "author_outcomes", "candidates": len(protos)}]}


def consolidate_concepts(state, config) -> dict:
    """PHASE 1b · semantic merge + taught depth (+ gated critic) → concept_inventory."""
    _bind_rag(config)
    prog = _prog(config)
    prog.start("consolidate_concepts")
    protos = [dict(o) for o in state["outcomes"]]
    sec_text = {s["topic_id"]: s["text"] for s in state["sections"]}
    groups, cmeta = agent.consolidate(protos, state.get("source_text", ""))
    inv, outcomes = gate.build_inventory(protos, groups, sec_text)
    in_scope = sum(1 for c in inv if c.get("in_scope"))
    critic = " · critic revised" if cmeta.get("critic_fired") else (
        " · critic checked" if cmeta.get("critic_gated") else "")
    prog.done("consolidate_concepts", detail=f"{len(inv)} concepts ({in_scope} in-scope){critic}",
              snapshot={"concept_count": len(inv), "in_scope": in_scope, "merged_from": len(protos),
                        "critic_gated": cmeta.get("critic_gated", False),
                        "critic_fired": cmeta.get("critic_fired", False),
                        "concepts": [{"concept_id": c["concept_id"], "name": c["canonical_name"],
                                      "depth": c["depth_category"], "in_scope": c["in_scope"]}
                                     for c in inv]})
    return {"outcomes": outcomes, "concept_inventory": inv,
            "log": [{"node": "consolidate_concepts", "concepts": len(inv), "in_scope": in_scope}]}


def graph_outcomes(state, config) -> dict:
    """PHASE 2 · concept dependency DAG (K-sample voting) → weights, dag_depth, procedural."""
    _bind_rag(config)
    prog = _prog(config)
    inv = state["concept_inventory"]
    on_done = prog.counter("graph_outcomes", len(inv))
    concept_graph, inv, outcomes, outcome_graph = tools.build_graph(inv, state["outcomes"], on_done)
    procedural = sum(1 for c in inv if c.get("procedural"))
    prog.done("graph_outcomes",
              detail=f"{len(inv)} concepts · {len(concept_graph['edges'])} edges · {procedural} procedural",
              snapshot={"concept_count": len(inv), "edges": len(concept_graph["edges"]),
                        "procedural": procedural, "assumed_prior": concept_graph.get("assumed_prior", [])})
    return {"concept_inventory": inv, "outcomes": outcomes,
            "concept_graph": concept_graph, "outcome_graph": outcome_graph,
            "log": [{"node": "graph_outcomes", "edges": len(concept_graph["edges"])}]}


def select_outcomes(state, config) -> dict:
    """PHASE 3 · budget-aware selection (agent proposes + gated critic; gate enforces) + assembly."""
    _bind_rag(config)
    prog = _prog(config)
    prog.start("select_outcomes")
    ctx = _ctx(config)
    requested = int(getattr(ctx, "question_budget", None) or QUESTION_BUDGET)
    inv = state["concept_inventory"]
    outcomes = state["outcomes"]
    prefer, pmeta = agent.plan(inv, outcomes, requested)
    res = gate.enforce(state, inv, outcomes, state["concept_graph"], state["outcome_graph"],
                       requested, prefer)
    # PHASE 3b — plan the question TYPE alongside each selected LO (parallel), so each outcome leaves
    # planning as a complete unit (concept + tier + question type) and the review gate shows it.
    res["outcomes"] = pmap(agent.recommend_type, res["outcomes"])
    # ENFORCE the target count: if fewer distinct outcomes than the budget, fill toward it with
    # same-outcome variants in different question formats (best-effort; thin sessions stay below).
    base_n = len(res["outcomes"])
    res["outcomes"] = agent.expand_to_target(res["outcomes"], requested)
    n_variants = len(res["outcomes"]) - base_n
    if isinstance(res.get("allocation_plan"), dict):      # keep budget target consistent post-expand
        res["allocation_plan"]["question_budget"] = len(res["outcomes"])
    qtypes = Counter(o.get("question_type") for o in res["outcomes"])
    snapshot = res.pop("_snapshot", None)
    detail = res.pop("_detail", "")
    if isinstance(snapshot, dict):
        snapshot["plan_critic_gated"] = pmeta.get("critic_gated", False)
        snapshot["plan_critic_fired"] = pmeta.get("critic_fired", False)
        snapshot["by_question_type"] = dict(qtypes)
        snapshot["type_variants_added"] = n_variants
    detail += f" · {len(res['outcomes'])} LOs"
    if n_variants:
        detail += f" (+{n_variants} type-variants)"
    detail += " · types: " + "/".join(f"{(t or '?').split('_')[0].lower()}:{n}" for t, n in qtypes.items())
    if pmeta.get("critic_fired"):
        detail += " · plan critic revised"
    prog.done("select_outcomes", detail=detail, snapshot=snapshot)
    return res
