"""LO pipeline · Node 2 — plan_los (package).

A goal-driven group of specialized LLM sub-agents that produce the final, budget-bounded,
evidence-grounded learning outcome set. It replaces the former five staged nodes —
generate_outcomes, map_concepts, build_outcome_graph, profile_depth, plan_outcomes — with FOUR
observable nodes (option B: phases as first-class graph nodes, each its own progress stage with
independent retry + checkpointing), on the principle **the agent proposes, the code enforces**: the
LLM owns the judgment (which concepts exist, are two the same, how deeply each is taught, which
outcomes to keep), while deterministic code owns the invariants (id keying, quote grounding,
feasibility clamp, budget ceiling, coverage floor, and the allocation_plan shape the repair loop
reconciles against). Two phases carry a GATED critic/reviser sub-agent (m01-style reflexion).

Why collapse: the old flow OVER-generated candidate outcomes, then fired a per-concept LLM call in
build_outcome_graph (×K votes) and profile_depth (depth + scope) for every candidate concept — most
of which plan_outcomes then dropped or tier-downgraded. Planning the concept set up front means the
graph/depth work runs on the SELECTED-scale concept count, not the over-generated one.

FLOW (four nodes in the main graph)
    state["sections"] + ctx.question_budget
        │
   ▸ author_outcomes      per-section author (parallel)      [LLM · lo.generate_sys]
        │                   → candidate outcomes (broad concept ⊃ fine sub-concept)
   ▸ consolidate_concepts semantic merge + taught depth      [LLM · lo.consolidate_sys]
        │                   + gated critic                   [LLM · lo.consolidate_critic_sys]
        │                   gate.build_inventory → concept_inventory + remapped outcomes
   ▸ graph_outcomes       concept DAG (K-sample voting)       [LLM · lo.graph_sys]
        │                   tools.build_graph → edges, weights, dag_depth, procedural
   ▸ select_outcomes      budget-aware selection proposal     [LLM · lo.plan_sys]
        │                   + gated critic                   [LLM · lo.plan_critic_sys]
        │                   gate.enforce → clamp tiers · budget ceiling · coverage floor ·
        │                   allocation_plan / backfill_pool / division_proposal / coverage_profile
        ▼  emits every state key the old five nodes did
   resolve_prerequisites → review_and_validate → repair → … → questions

No legacy fallback (replace-outright); an unrecoverable phase raises ESCALATE.

Submodules:
    prompts.py — the DB-backed judgment + critic prompts (+ verb substitution).
    agent.py   — the specialized LLM sub-agents (phase logic + gated critics).
    tools.py   — deterministic concept-DAG voting + metrics (drift-prone work kept behind K votes).
    gate.py    — invariant enforcement + full downstream state-contract assembly.
    node.py    — the four LangGraph node entries.
"""
from __future__ import annotations

from app.mcq_pipeline.nodes.m02_plan_los.node import (author_outcomes, consolidate_concepts,
                                                      derive_session_focus, graph_outcomes,
                                                      select_outcomes)

__all__ = ["derive_session_focus", "author_outcomes", "consolidate_concepts",
           "graph_outcomes", "select_outcomes"]
