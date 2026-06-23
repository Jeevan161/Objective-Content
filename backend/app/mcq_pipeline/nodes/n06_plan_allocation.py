"""LO pipeline · Node 5 — plan_allocation / Planner (feasibility-driven 4-tier division)."""
from __future__ import annotations

from collections import defaultdict

from app.mcq_pipeline.config import BUDGET_STEP, MAX_LOS_PER_CONCEPT, MIN_BUDGET, QUESTION_BUDGET, SCENARIO_TARGET, TIER_ORDER, feasible_tiers
from app.mcq_pipeline.nodes._common import _ctx, _prog


# ── Node 5 · plan_allocation / Planner (D · feasibility-driven 4-tier division) ── #
def _feasible_concept(c: dict) -> tuple:
    return feasible_tiers(c.get("depth_category", "moderate"), bool(c.get("procedural")))


def _quantize_budget(requested: int, capacity: int, n_concepts: int) -> tuple:
    """Budget is a CEILING: step DOWN to the nearest multiple of BUDGET_STEP, capped by what
    the material supports (capacity). Coverage (V4) is a hard floor — never fewer LOs than
    in-scope concepts. Returns (final_budget, flags)."""
    flags = []
    target = max(0, min(requested, capacity))
    final = max(MIN_BUDGET, (target // BUDGET_STEP) * BUDGET_STEP)
    if final < requested:
        flags.append({"flag": "budget_reduced", "requested": requested, "final": final,
                      "reason": f"material supports ~{capacity} grounded LOs"})
    if final < n_concepts:                       # coverage floor: every in-scope concept (V4)
        final = min(capacity, ((n_concepts + BUDGET_STEP - 1) // BUDGET_STEP) * BUDGET_STEP)
        flags.append({"flag": "budget_raised_for_coverage", "final": final,
                      "reason": f"{n_concepts} in-scope concepts must each be covered (V4)"})
    if final <= 10:
        flags.append({"flag": "low_budget_review", "final": final,
                      "reason": "thin material — recommend human review at Gate 1"})
    return final, flags


def _allocate_tiers(inv_scope: list, final_budget: int) -> list:
    """Deterministic feasibility-driven allocator. Produces exactly `final_budget`
    (concept_id, tier) assignments: (a) one LO per in-scope concept (coverage / V4) at its top
    non-scenario feasible tier; (b) up to SCENARIO_TARGET scenario LOs from deep+procedural
    concepts; (c) fill to budget by deepening concepts that still have capacity
    (<= MAX_LOS_PER_CONCEPT), foundational tier first. Feasibility is never exceeded."""
    assignments, used = [], defaultdict(set)

    def add(cid, tier):
        assignments.append({"concept_id": cid, "tier": tier})
        used[cid].add(tier)

    for c in inv_scope:                          # (a) coverage
        tiers = [t for t in _feasible_concept(c) if t != "scenario"]
        add(c["concept_id"], tiers[-1] if tiers else "remember")

    for c in inv_scope:                          # (b) scenario quota
        if sum(1 for a in assignments if a["tier"] == "scenario") >= SCENARIO_TARGET:
            break
        if len(assignments) >= final_budget:
            break
        if "scenario" in _feasible_concept(c) and len(used[c["concept_id"]]) < MAX_LOS_PER_CONCEPT:
            add(c["concept_id"], "scenario")

    progressed = True                            # (c) fill to budget (deepen with spare tiers)
    while len(assignments) < final_budget and progressed:
        progressed = False
        for c in inv_scope:
            if len(assignments) >= final_budget:
                break
            cid = c["concept_id"]
            if len(used[cid]) >= MAX_LOS_PER_CONCEPT:
                continue
            spare = [t for t in _feasible_concept(c) if t not in used[cid]]
            if not spare:
                continue
            add(cid, spare[0])
            progressed = True
    return assignments[:final_budget]


def plan_allocation(state, config) -> dict:
    """Planner: derive a FEASIBILITY-driven 4-tier Bloom division (no fixed split) + a
    user-supplied, capacity-bounded, multiples-of-5 budget. Emits per-topic (concept_id, tier)
    assignments and a `division_proposal` payload for the Gate-1 human review."""
    prog = _prog(config)
    prog.start("plan_allocation")
    ctx = _ctx(config)
    requested = int(getattr(ctx, "question_budget", None) or QUESTION_BUDGET)

    inv = [dict(c) for c in state["concept_inventory"]]
    inv_scope = [c for c in inv if c.get("in_scope")]
    n = len(inv_scope)
    capacity = max(MIN_BUDGET, n * MAX_LOS_PER_CONCEPT)
    final_budget, flags = _quantize_budget(requested, capacity, n)

    # apply-suitability is now PURELY feasibility (LLM-derived): apply feasible iff the concept
    # is procedural AND taught beyond a bare mention. No code-fence / setup-CLI gating.
    for c in inv:
        c["apply_suitable"] = "apply" in feasible_tiers(
            c.get("depth_category", "moderate"), bool(c.get("procedural")))

    assignments = _allocate_tiers(inv_scope, final_budget)

    topic_of = {c["concept_id"]: c["topic_id"] for c in inv}
    plan = {t["topic_id"]: {"topic_id": t["topic_id"], "title": t["title"], "order": t["order"],
                            "slots": 0, "bloom": {k: 0 for k in TIER_ORDER}, "assignments": []}
            for t in state["sections"]}
    tier_counts = {k: 0 for k in TIER_ORDER}
    for a in assignments:
        tid = topic_of.get(a["concept_id"])
        if tid not in plan:
            continue
        plan[tid]["assignments"].append(a)
        plan[tid]["bloom"][a["tier"]] += 1
        plan[tid]["slots"] += 1
        tier_counts[a["tier"]] += 1

    if tier_counts["scenario"] == 0:        # surfaced at Gate 1 (O1: proceed with 0 scenario + flag)
        flags.append({"flag": "no_scenario_feasible",
                      "reason": "no deep+procedural concept supports a scenario item"})

    order_of = {t["topic_id"]: t["order"] for t in state["sections"]}
    division_proposal = {
        "requested_budget": requested, "final_budget": final_budget, "capacity": capacity,
        "budget_reduced": final_budget < requested, "tier_counts": tier_counts,
        "per_topic": [{"topic_id": tid, "title": plan[tid]["title"],
                       "slots": plan[tid]["slots"], "tiers": plan[tid]["bloom"]}
                      for tid in sorted(plan, key=lambda x: order_of.get(x, 99))],
        "in_scope": [{"concept_id": c["concept_id"], "name": c["canonical_name"],
                      "depth": c.get("depth_category", "moderate"),
                      "procedural": bool(c.get("procedural")),
                      "ceiling": list(_feasible_concept(c))} for c in inv_scope],
        "dropped": [{"concept_id": c["concept_id"], "name": c["canonical_name"],
                     "reason": c.get("out_of_scope_reason", "out of scope")}
                    for c in inv if not c.get("in_scope")],
        "flags": [f["flag"] for f in flags],
    }
    allocation = {"by_topic": plan, "tier_counts": tier_counts,
                  "question_budget": final_budget, "capacity": capacity}
    overrides = [{"rule": "feasibility_division", **f} for f in flags]
    logs = [{"node": "plan_allocation", "budget": final_budget, "requested": requested,
             "tier_counts": tier_counts, "flags": [f["flag"] for f in flags]}]
    prog.done("plan_allocation",
              detail=f"budget {final_budget} · "
                     + "/".join(f"{k[:3]}:{tier_counts[k]}" for k in TIER_ORDER))
    return {"concept_inventory": inv, "allocation_plan": allocation,
            "division_proposal": division_proposal, "overrides": overrides, "log": logs}


