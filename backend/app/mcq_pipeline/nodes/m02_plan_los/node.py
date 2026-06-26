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
    groups = agent.consolidate(protos, state.get("source_text", ""))
    inv, outcomes = gate.build_inventory(protos, groups, sec_text)
    in_scope = sum(1 for c in inv if c.get("in_scope"))
    prog.done("consolidate_concepts", detail=f"{len(inv)} concepts ({in_scope} in-scope)",
              snapshot={"concept_count": len(inv), "in_scope": in_scope, "merged_from": len(protos),
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
    prefer = agent.plan(inv, outcomes, requested)
    res = gate.enforce(state, inv, outcomes, state["concept_graph"], state["outcome_graph"],
                       requested, prefer)
    snapshot = res.pop("_snapshot", None)
    detail = res.pop("_detail", "")
    prog.done("select_outcomes", detail=detail, snapshot=snapshot)
    return res
