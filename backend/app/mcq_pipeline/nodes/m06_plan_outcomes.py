"""LO pipeline (LO-first) · Node 6 — plan_outcomes (budget-bounded selection).

Step 4 of the LO-first flow: "Plan the outcomes considering the default budget (20) and identify
the apply-level outcomes." Node 2 over-generated; this node SELECTS the set we keep:

  * Drop candidates whose concept profiled out of scope (named-in-passing / external).
  * Clamp every candidate's Bloom tier to its concept's FEASIBILITY ceiling (taught depth +
    procedurality) — an apply/scenario candidate on a non-procedural or shallow concept is
    downgraded to the highest tier the material supports. This is where apply-level outcomes are
    legitimately IDENTIFIED (apply/scenario survive only on deep + procedural concepts).
  * Select toward the budget (a CEILING; default 20, quantized): coverage first — one outcome per
    in-scope concept at its top non-scenario feasible tier — then fill remaining slots by WEIGHT
    (foundational concepts first), respecting the per-concept cap. The budget floor rises if there
    are more in-scope concepts than the requested budget (every concept must be covered).

Emits the `division_proposal` payload reviewed at HITL Gate 1, an `allocation_plan` (kept
plan-shaped so the repair loop's reconciliation still works), and a `selection_summary`.

Input:  state["outcomes"] (candidates w/ weight), state["concept_inventory"] (depth + procedural).
Output: outcomes (the SELECTED set), allocation_plan, division_proposal, selection_summary,
        overrides, logs.
"""
from __future__ import annotations

from collections import Counter, defaultdict

from app.mcq_pipeline.config import (BUDGET_STEP, MAX_LOS_PER_CONCEPT, MIN_BUDGET, QUESTION_BUDGET,
                                     SKILL_TYPES, TIER_ORDER, VERBS, feasible_tiers)
from app.mcq_pipeline.nodes._common import _ctx, _prog
from app.mcq_pipeline.nodes.m02_generate_outcomes import _DEFAULT_VERB

_RANK = {t: i for i, t in enumerate(TIER_ORDER)}


def _feasible(c: dict) -> tuple:
    return feasible_tiers(c.get("depth_category", "moderate"), bool(c.get("procedural")))


def _clamp_to_feasible(o: dict, concept: dict) -> dict:
    """Return a copy of outcome `o` with its tier clamped to the concept's feasibility ceiling.
    If the candidate's tier is feasible, it's returned unchanged; otherwise it is downgraded to the
    highest feasible tier not above its current one, and the verb/skill_type are re-clamped."""
    feas = _feasible(concept)
    tier = o["bloom_level"]
    if tier in feas:
        return dict(o)
    cur = _RANK.get(tier, 0)
    below = [t for t in feas if _RANK[t] <= cur] or list(feas) or ["remember"]
    target = max(below, key=lambda t: _RANK[t])
    verb = o.get("learner_action", "")
    if verb not in VERBS[target]:
        verb = _DEFAULT_VERB[target]
    skill = o.get("skill_type")
    if skill not in SKILL_TYPES:
        skill = "practical_application" if target in ("apply", "scenario") else "conceptual"
    return {**o, "bloom_level": target, "scenario": target == "scenario",
            "learner_action": verb, "skill_type": skill}


def _quantize_budget(requested: int, capacity: int, n_concepts: int) -> tuple:
    """Budget is a CEILING (step DOWN to a multiple of BUDGET_STEP, capped by capacity), with a hard
    coverage floor: never fewer outcomes than in-scope concepts. Returns (final_budget, flags)."""
    flags = []
    target = max(0, min(requested, capacity))
    final = max(MIN_BUDGET, (target // BUDGET_STEP) * BUDGET_STEP)
    if final < requested:
        flags.append({"flag": "budget_reduced", "requested": requested, "final": final,
                      "reason": f"material supports ~{capacity} grounded outcomes"})
    if final < n_concepts:
        final = min(capacity, ((n_concepts + BUDGET_STEP - 1) // BUDGET_STEP) * BUDGET_STEP)
        flags.append({"flag": "budget_raised_for_coverage", "final": final,
                      "reason": f"{n_concepts} in-scope concepts must each be covered"})
    if final <= 10:
        flags.append({"flag": "low_budget_review", "final": final,
                      "reason": "thin material — recommend human review at Gate 1"})
    return final, flags


def _select(candidates: list, budget: int) -> list:
    """Pick up to `budget` outcomes: (a) coverage — each concept's top non-scenario candidate;
    (b) fill remaining slots by weight (then tier, then shallower dag_depth), capped per concept."""
    by_concept: dict = defaultdict(list)
    for o in candidates:
        by_concept[o["concept_id"]].append(o)

    selected, used, picked_ids = [], defaultdict(int), set()

    def take(o):
        selected.append(o)
        used[o["concept_id"]] += 1
        picked_ids.add(o["id"])

    # (a) coverage: one outcome per concept at its highest non-scenario tier (fallback: any).
    for cid, group in by_concept.items():
        non_scenario = [o for o in group if o["bloom_level"] != "scenario"] or group
        best = max(non_scenario, key=lambda o: (_RANK[o["bloom_level"]], o.get("weight", 0)))
        take(best)

    # (b) fill to budget by foundational-ness, respecting the per-concept cap.
    rest = sorted((o for o in candidates if o["id"] not in picked_ids),
                  key=lambda o: (-o.get("weight", 0), -_RANK[o["bloom_level"]],
                                 o.get("dag_depth", 0), o["id"]))
    for o in rest:
        if len(selected) >= budget:
            break
        if used[o["concept_id"]] >= MAX_LOS_PER_CONCEPT:
            continue
        take(o)

    # If coverage alone already exceeded the budget, keep the heaviest ones (every concept still
    # appears at least once because coverage picks are added first and sorted stably).
    if len(selected) > budget:
        selected = sorted(selected, key=lambda o: (-o.get("weight", 0), -_RANK[o["bloom_level"]],
                                                   o.get("dag_depth", 0), o["id"]))[:budget]
    return selected


def backfill_to_budget(outcomes: list, pool: list, budget: int,
                       exclude_concepts: frozenset = frozenset()) -> tuple[list, list]:
    """Top the outcome set back up toward `budget` using the highest-weighted UNSELECTED candidates
    from `pool` — keeping budget a TARGET, not just a ceiling. Used after dedup drops a duplicate and
    after repair drops a not-taught (R1) outcome. Skips: ids already present, a `(concept, Bloom)`
    pair already present (so it can't reintroduce a just-deduped twin), concepts over the per-concept
    cap, and `exclude_concepts` (e.g. concepts the judge said aren't taught). Best-effort — returns
    (outcomes, added_ids); FEWER than budget if the pool is exhausted (quality over count)."""
    if len(outcomes) >= budget or not pool:
        return list(outcomes), []
    present_ids = {o["id"] for o in outcomes}
    present_pairs = {(o["concept_id"], o["bloom_level"]) for o in outcomes}
    used = Counter(o["concept_id"] for o in outcomes)
    ranked = sorted(pool, key=lambda o: (-o.get("weight", 0), -_RANK.get(o.get("bloom_level"), 0),
                                         o.get("dag_depth", 0), o.get("id", "")))
    out, added = list(outcomes), []
    for cand in ranked:
        if len(out) >= budget:
            break
        cid, pair = cand.get("concept_id"), (cand.get("concept_id"), cand.get("bloom_level"))
        if cand.get("id") in present_ids or pair in present_pairs:
            continue
        if cid in exclude_concepts or used[cid] >= MAX_LOS_PER_CONCEPT:
            continue
        out.append(dict(cand))
        added.append(cand["id"])
        present_ids.add(cand["id"])
        present_pairs.add(pair)
        used[cid] += 1
    return out, added


def plan_outcomes(state, config) -> dict:
    prog = _prog(config)
    prog.start("plan_outcomes")
    ctx = _ctx(config)
    requested = int(getattr(ctx, "question_budget", None) or QUESTION_BUDGET)

    inv = state["concept_inventory"]
    inv_by_id = {c["concept_id"]: c for c in inv}
    in_scope_ids = {c["concept_id"] for c in inv if c.get("in_scope")}
    n = len(in_scope_ids)

    all_cands = state["outcomes"]
    # keep candidates whose concept is in-scope, then clamp each to its feasibility ceiling.
    candidates = [_clamp_to_feasible(o, inv_by_id[o["concept_id"]])
                  for o in all_cands if o.get("concept_id") in in_scope_ids]
    dropped_oos = [o["id"] for o in all_cands if o.get("concept_id") not in in_scope_ids]

    capacity = max(MIN_BUDGET, min(n * MAX_LOS_PER_CONCEPT, len(candidates) or MIN_BUDGET))
    final_budget, flags = _quantize_budget(requested, capacity, n)

    selected = _select(candidates, final_budget)
    selected_ids = {o["id"] for o in selected}
    dropped_unselected = [o["id"] for o in candidates if o["id"] not in selected_ids]
    apply_ids = [o["id"] for o in selected if o["bloom_level"] in ("apply", "scenario")]

    # Derive a plan-shaped structure from the SELECTED set (so the repair loop's reconciliation +
    # the artifact bridge stay happy). assignments mirror each kept outcome's (concept_id, tier).
    topic_of = {c["concept_id"]: c["topic_id"] for c in inv}
    plan = {t["topic_id"]: {"topic_id": t["topic_id"], "title": t["title"], "order": t["order"],
                            "slots": 0, "bloom": {k: 0 for k in TIER_ORDER}, "assignments": []}
            for t in state["sections"]}
    tier_counts = {k: 0 for k in TIER_ORDER}
    for o in selected:
        tid = o["topic_id"] if o["topic_id"] in plan else topic_of.get(o["concept_id"])
        if tid in plan:
            plan[tid]["slots"] += 1
            plan[tid]["bloom"][o["bloom_level"]] += 1
            plan[tid]["assignments"].append({"concept_id": o["concept_id"], "tier": o["bloom_level"]})
        tier_counts[o["bloom_level"]] += 1

    if tier_counts["scenario"] == 0:
        flags.append({"flag": "no_scenario_feasible",
                      "reason": "no deep+procedural concept supports a scenario outcome"})

    order_of = {t["topic_id"]: t["order"] for t in state["sections"]}
    division_proposal = {
        "requested_budget": requested, "final_budget": len(selected), "capacity": capacity,
        "budget_reduced": len(selected) < requested, "tier_counts": tier_counts,
        "candidates_generated": len(all_cands),
        "per_topic": [{"topic_id": tid, "title": plan[tid]["title"],
                       "slots": plan[tid]["slots"], "tiers": plan[tid]["bloom"]}
                      for tid in sorted(plan, key=lambda x: order_of.get(x, 99))],
        "in_scope": [{"concept_id": c["concept_id"], "name": c["canonical_name"],
                      "depth": c.get("depth_category", "moderate"),
                      "procedural": bool(c.get("procedural")),
                      "ceiling": list(_feasible(c))}
                     for c in inv if c.get("in_scope")],
        "dropped": [{"concept_id": c["concept_id"], "name": c["canonical_name"],
                     "reason": c.get("out_of_scope_reason", "out of scope")}
                    for c in inv if not c.get("in_scope")],
        "flags": [f["flag"] for f in flags], "importance_ranked": False,
        "apply_outcomes": apply_ids,
    }
    allocation = {"by_topic": plan, "tier_counts": tier_counts,
                  "question_budget": len(selected), "capacity": capacity}
    selection_summary = {"candidates": len(all_cands), "selected": len(selected),
                         "dropped_out_of_scope": len(dropped_oos),
                         "dropped_unselected": len(dropped_unselected),
                         "apply_outcomes": len(apply_ids)}
    overrides = [{"rule": "feasibility_division", **f} for f in flags]
    logs = [{"node": "plan_outcomes", "candidates": len(all_cands), "selected": len(selected),
             "budget": final_budget, "apply": len(apply_ids), "tier_counts": tier_counts,
             "flags": [f["flag"] for f in flags]}]
    name_of = {c["concept_id"]: c["canonical_name"] for c in inv}
    snapshot = {**selection_summary, "budget": final_budget, "tier_counts": tier_counts,
                "flags": [f["flag"] for f in flags],
                "selected": [{"id": o["id"], "title": o["title"], "bloom": o["bloom_level"],
                              "concept": name_of.get(o["concept_id"], o["concept_id"]),
                              "weight": o.get("weight", 0)} for o in selected],
                "apply_outcomes": [name_of.get(
                    next((o["concept_id"] for o in selected if o["id"] == oid), oid), oid)
                    for oid in apply_ids]}
    prog.done("plan_outcomes",
              detail=f"{len(selected)}/{len(all_cands)} kept · {len(apply_ids)} apply · "
                     + "/".join(f"{k[:3]}:{tier_counts[k]}" for k in TIER_ORDER),
              snapshot=snapshot)
    # the unselected (but in-scope, feasibility-clamped) candidates, kept as the backfill pool so
    # dedup / R1-drop can top the set back up toward budget with the next-weighted taught concept.
    backfill_pool = [o for o in candidates if o["id"] not in selected_ids]
    return {"outcomes": selected, "allocation_plan": allocation, "backfill_pool": backfill_pool,
            "division_proposal": division_proposal, "selection_summary": selection_summary,
            "overrides": overrides, "log": logs}
