"""plan_los · the deterministic gate — "code enforces".

This is where the agent's JUDGMENT is turned into the pipeline's invariants and the full downstream
state contract. It relocates the proven logic of the old map_concepts (id keying), profile_depth
(taught-depth application + scope drop) and plan_outcomes (feasibility clamp, budget quantize,
coverage-first selection, plan assembly) nodes — unchanged in behaviour, only re-homed.

Three entry points, called in order by :mod:`agent`:
  * ``proto_outcome``    — normalize one phase-1 LLM item into a candidate outcome.
  * ``build_inventory``  — apply the phase-1b merge groups + taught depth → concept_inventory,
                           remap every outcome onto its canonical concept_id.
  * ``enforce``          — clamp tiers to feasibility, select toward budget honouring the agent's
                           phase-3 preference order BUT guaranteeing coverage + the budget ceiling,
                           and assemble allocation_plan / backfill_pool / division_proposal /
                           selection_summary / coverage_profile.
"""
from __future__ import annotations

from collections import Counter, defaultdict

from app.mcq_pipeline.config import (BUDGET_STEP, DEPTH_CATEGORIES, DROP_NAMED_ONLY, MIN_BUDGET,
                                     QUESTION_BUDGET, SCENARIO_TARGET, SKILL_TYPES, TIER_ORDER,
                                     VERBS, feasible_tiers)
from app.mcq_pipeline.utils.concept_graph import (canonical_name, description_grounded, display_name,
                                                  graph_find_prerequisites, ground_quote, slugify)
from app.mcq_pipeline.utils._lo_helpers import _DEFAULT_VERB, _tier_of

_RANK = {t: i for i, t in enumerate(TIER_ORDER)}


def _norm(s: str) -> str:
    return " ".join((s or "").lower().split())


# ── phase-1 item → candidate outcome ──────────────────────────────────────── #
def proto_outcome(item: dict, topic: dict) -> dict | None:
    """Build a candidate (proto) outcome from one phase-1 LLM item. Tier + verb clamped to the
    controlled vocabulary; evidence grounded against the section. concept_id + final id are assigned
    in `build_inventory`. Returns None if the item names no concept."""
    cname = (item.get("concept") or "").strip()
    if not cname:
        return None
    sub_name = (item.get("sub_concept") or "").strip() or cname
    tier = _tier_of(item) or "understand"
    verb = str(item.get("learner_action", "")).lower().strip()
    if verb not in VERBS[tier]:
        verb = _DEFAULT_VERB[tier]
    title = (item.get("title") or f"{verb.title()} {cname}").strip()
    skill = item.get("skill_type")
    if skill not in SKILL_TYPES:
        skill = "practical_application" if tier in ("apply", "scenario") else "conceptual"
    llm_quote = (item.get("quote") or "").strip()
    quote = llm_quote if (llm_quote and llm_quote in topic["text"]) else ground_quote(cname, topic["text"])
    return {"_concept_name": cname, "_sub_concept_name": sub_name,
            "title": title, "topic_id": topic["topic_id"],
            "bloom_level": tier, "scenario": tier == "scenario",
            "skill_type": skill, "learner_action": verb,
            "description": (item.get("description") or title).strip(),
            "syntax": (item.get("syntax") or None),
            "prerequisites": [], "prerequisite_scope": None, "target_questions": 1,
            "source_evidence": {"quote": quote, "section": topic["topic_id"]},
            "justification": (item.get("justification") or "Grounded in section evidence.").strip()}


# ── phase-1b merge groups + depth → concept_inventory ──────────────────────── #
def build_inventory(outcomes: list, groups: list, sec_text: dict) -> tuple[list, list]:
    """Apply the semantic merge groups (canonical_name + members + depth + in_scope) to stamp every
    outcome with a canonical concept_id and accrue the concept_inventory. Replaces the old node's
    token-set Jaccard union-find (merge is now the LLM's judgment) and folds in profile_depth's
    taught-depth application + the DROP_NAMED_ONLY scope rule."""
    member_to_group: dict = {}
    for g in groups if isinstance(groups, list) else []:
        if not isinstance(g, dict):
            continue
        for m in g.get("members", []) or []:
            member_to_group[_norm(str(m))] = g
        member_to_group.setdefault(_norm(str(g.get("canonical_name", ""))), g)

    inv: dict = {}
    for o in outcomes:
        sub_name = (o.get("_sub_concept_name") or o.get("_concept_name") or o.get("title") or "").strip()
        broad_name = (o.get("_concept_name") or sub_name).strip()
        g = member_to_group.get(_norm(sub_name))
        if g:
            canon = canonical_name(g.get("canonical_name") or sub_name)
            parent_src = g.get("parent_concept") or broad_name
            depth = str(g.get("depth", "")).strip().lower()
            in_scope = g.get("in_scope", True) is not False
            why = str(g.get("why", ""))[:200]
        else:                                   # LLM dropped it from every group: treat standalone
            canon = canonical_name(sub_name)
            parent_src = broad_name
            depth, in_scope, why = "moderate", True, ""
        cid = "C_" + slugify(canon)
        o["concept_id"] = cid
        topic_id = o.get("topic_id", "")
        section_text = sec_text.get((o.get("source_evidence") or {}).get("section", topic_id), "")
        ev_quote = (o.get("source_evidence") or {}).get("quote", "")
        quote = ev_quote if (ev_quote and ev_quote in section_text) else ground_quote(display_name(canon), section_text)
        desc = (o.get("description") or "").strip()
        if not description_grounded(desc, [ev_quote, quote], section_text):
            desc = ev_quote or quote

        if cid not in inv:
            dc = depth if depth in DEPTH_CATEGORIES else "moderate"
            entry = {"concept_id": cid, "canonical_name": display_name(canon),
                     "parent_concept": display_name(canonical_name(parent_src)),
                     "topic_id": topic_id, "in_scope": True, "procedural": False,
                     "description": desc,
                     "evidence_quotes": [q for q in {ev_quote, quote} if q],
                     "evidence": {"quote": (quote or ev_quote), "section": topic_id},
                     "depth_category": dc, "depth_why": why,
                     "taught_depth": dc, "explained": dc in ("moderate", "deep")}
            if not in_scope:
                entry["in_scope"] = False
                entry["out_of_scope_reason"] = "named in passing; not taught in course scope (external)"
            elif DROP_NAMED_ONLY and dc == "named":
                entry["in_scope"] = False
                entry["out_of_scope_reason"] = "named in passing; no definition or explanation in the reading"
            inv[cid] = entry
        else:
            for q in (ev_quote, quote):
                if q and q not in inv[cid]["evidence_quotes"]:
                    inv[cid]["evidence_quotes"].append(q)
            if len(desc) > len(inv[cid]["description"]):
                inv[cid]["description"] = desc

    seen = Counter()
    for o in outcomes:
        o.pop("_concept_name", None)
        o.pop("_sub_concept_name", None)
        base = slugify(f'{o["learner_action"]}_{o["concept_id"][2:]}') or "lo"
        seen[base] += 1
        o["id"] = base if seen[base] == 1 else f"{base}_{seen[base]}"
    return list(inv.values()), outcomes


# ── phase-3 enforcement + state assembly (relocated plan_outcomes) ─────────── #
def _feasible(c: dict) -> tuple:
    return feasible_tiers(c.get("depth_category", "moderate"), bool(c.get("procedural")))


def _clamp_to_feasible(o: dict, concept: dict) -> dict:
    """Clamp an outcome's tier to its concept's feasibility ceiling (verb/skill re-clamped)."""
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
    """Budget is a CEILING (stepped down to a multiple of BUDGET_STEP, capped by capacity) with a
    hard coverage floor: never fewer than the in-scope broad-concept count."""
    flags = []
    target = max(0, min(requested, capacity))
    final = max(MIN_BUDGET, (target // BUDGET_STEP) * BUDGET_STEP)
    if final < requested:
        flags.append({"flag": "budget_reduced", "requested": requested, "final": final,
                      "reason": f"material supports ~{capacity} grounded outcomes"})
    if final < n_concepts:
        final = min(capacity, ((n_concepts + BUDGET_STEP - 1) // BUDGET_STEP) * BUDGET_STEP)
        flags.append({"flag": "budget_raised_for_coverage", "final": final,
                      "reason": f"{n_concepts} in-scope broad concepts must each be covered"})
    if final <= 10:
        flags.append({"flag": "low_budget_review", "final": final,
                      "reason": "thin material — recommend human review at Gate 1"})
    return final, flags


def _select(candidates: list, budget: int, parent_of: dict, prefer_ids: list | None = None,
            risky_ids: frozenset = frozenset()) -> list:
    """Coverage-first selection toward `budget`. (a) one outcome per BROAD concept at its top
    non-scenario tier (heaviest concept first; prereq-safe broken ties); (a2) reserve up to
    SCENARIO_TARGET scenario outcomes (prereq-safe first); (b) fill remaining slots PREREQ-SAFE
    first, then the agent's PREFERRED order (phase-3 proposal), then by weight — deduping
    `(concept_id, Bloom)` pairs. `risky_ids` = apply/scenario candidates whose DAG prereq closure
    has an out-of-scope (non-assumed) concept — they MAY fail coverage in resolve_prerequisites, so
    we prefer safe ones for the extra slots to avoid the select→downgrade churn (coverage stays
    tier-optimal; this only steers the extras)."""
    by_parent: dict = defaultdict(list)
    for o in candidates:
        by_parent[parent_of.get(o["concept_id"], o["concept_id"])].append(o)
    selected, picked_ids, picked_pairs = [], set(), set()

    def take(o):
        selected.append(o)
        picked_ids.add(o["id"])
        picked_pairs.add((o["concept_id"], o["bloom_level"]))

    def _group_weight(item):
        return max((o.get("weight", 0) for o in item[1]), default=0)
    for parent, group in sorted(by_parent.items(), key=_group_weight, reverse=True):
        non_scenario = [o for o in group if o["bloom_level"] != "scenario"] or group
        # tier-optimal for the guaranteed slot; among the same tier, prefer a prereq-safe outcome.
        take(max(non_scenario, key=lambda o: (_RANK[o["bloom_level"]],
                                              o["id"] not in risky_ids, o.get("weight", 0))))

    # (a2) scenario targeting: a clamped candidate at "scenario" tier IS feasible (deep+procedural),
    # else _clamp_to_feasible would have downgraded it. Reserve up to SCENARIO_TARGET (prereq-safe
    # first, then heaviest) before the weight fill, so transfer-level questions are produced when
    # the material supports them instead of being crowded out. dedup + budget still apply.
    scen = sorted((o for o in candidates if o["bloom_level"] == "scenario"
                   and o["id"] not in picked_ids
                   and (o["concept_id"], o["bloom_level"]) not in picked_pairs),
                  key=lambda o: (o["id"] in risky_ids, -o.get("weight", 0), o.get("dag_depth", 0), o["id"]))
    for o in scen[:max(0, SCENARIO_TARGET)]:
        if len(selected) >= budget:
            break
        take(o)

    # (b) fill: PREREQ-SAFE first (apply/scenario whose prereqs are all in-scope/assumed don't risk a
    # downgrade), then the agent's preferred order, then weight.
    pref = {oid: i for i, oid in enumerate(prefer_ids or [])}
    rest = sorted((o for o in candidates if o["id"] not in picked_ids),
                  key=lambda o: (o["id"] in risky_ids, pref.get(o["id"], 10**6), -o.get("weight", 0),
                                 -_RANK[o["bloom_level"]], o.get("dag_depth", 0), o["id"]))
    for o in rest:
        if len(selected) >= budget:
            break
        if (o["concept_id"], o["bloom_level"]) in picked_pairs:        # dedup distinct sub-concept/tier
            continue
        take(o)

    if len(selected) > budget:
        selected = sorted(selected, key=lambda o: (-o.get("weight", 0), -_RANK[o["bloom_level"]],
                                                   o.get("dag_depth", 0), o["id"]))[:budget]
    return selected


def enforce(state, inv: list, outcomes: list, concept_graph: dict, outcome_graph: dict,
            requested: int, prefer_ids: list | None) -> dict:
    """Clamp to feasibility, select toward budget (agent-preferred order, code-guaranteed coverage),
    and assemble every state key the old m04-m06 nodes emitted."""
    inv_by_id = {c["concept_id"]: c for c in inv}
    parent_of = {c["concept_id"]: (c.get("parent_concept") or c["concept_id"]) for c in inv}
    in_scope_ids = {c["concept_id"] for c in inv if c.get("in_scope")}
    n = len({parent_of[cid] for cid in in_scope_ids})

    candidates = [_clamp_to_feasible(o, inv_by_id[o["concept_id"]])
                  for o in outcomes if o.get("concept_id") in in_scope_ids]
    dropped_oos = [o["id"] for o in outcomes if o.get("concept_id") not in in_scope_ids]

    capacity = max(MIN_BUDGET, len(candidates) or MIN_BUDGET)
    final_budget, flags = _quantize_budget(requested, capacity, n)

    # Prereq-safe flag (RAG-free): an apply/scenario candidate is "risky" if its DAG prerequisite
    # closure contains a concept that is neither in-scope (taught here) nor a declared assumed-prior
    # — such a prereq MAY come back uncovered from resolve_prerequisites and force a downgrade. We
    # prefer safe candidates for the extra slots so we don't select doomed apply LOs in the first
    # place. in-scope is only a PROXY (an out-of-scope prereq might still be taught_earlier via RAG),
    # so this just steers preference; resolve_prerequisites remains the authority.
    assumed_ids = {"C_" + slugify(p) for p in (concept_graph.get("assumed_prior") or [])}
    safe = in_scope_ids | assumed_ids
    cg_state = {"concept_graph": concept_graph}
    risky_ids = frozenset(
        o["id"] for o in candidates if o["bloom_level"] in ("apply", "scenario")
        and not all(p in safe for p in graph_find_prerequisites(cg_state, o["concept_id"])))

    prefer = [i for i in (prefer_ids or []) if isinstance(i, str)]
    selected = _select(candidates, final_budget, parent_of, prefer_ids=prefer, risky_ids=risky_ids)
    selected_ids = {o["id"] for o in selected}
    dropped_unselected = [o["id"] for o in candidates if o["id"] not in selected_ids]
    apply_ids = [o["id"] for o in selected if o["bloom_level"] in ("apply", "scenario")]

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
        "candidates_generated": len(outcomes),
        "per_topic": [{"topic_id": tid, "title": plan[tid]["title"], "slots": plan[tid]["slots"],
                       "tiers": plan[tid]["bloom"]}
                      for tid in sorted(plan, key=lambda x: order_of.get(x, 99))],
        "in_scope": [{"concept_id": c["concept_id"], "name": c["canonical_name"],
                      "depth": c.get("depth_category", "moderate"),
                      "procedural": bool(c.get("procedural")), "ceiling": list(_feasible(c))}
                     for c in inv if c.get("in_scope")],
        "dropped": [{"concept_id": c["concept_id"], "name": c["canonical_name"],
                     "reason": c.get("out_of_scope_reason", "out of scope")}
                    for c in inv if not c.get("in_scope")],
        "flags": [f["flag"] for f in flags], "importance_ranked": False, "apply_outcomes": apply_ids,
    }
    allocation = {"by_topic": plan, "tier_counts": tier_counts,
                  "question_budget": len(selected), "capacity": capacity}
    selection_summary = {"candidates": len(outcomes), "selected": len(selected),
                         "dropped_out_of_scope": len(dropped_oos),
                         "dropped_unselected": len(dropped_unselected),
                         "apply_outcomes": len(apply_ids)}
    overrides = [{"rule": "feasibility_division", **f} for f in flags]
    backfill_pool = [o for o in candidates if o["id"] not in selected_ids]

    cats = Counter(c["depth_category"] for c in inv if c["in_scope"])
    coverage_profile = {"by_depth": dict(cats), "in_scope": sum(1 for c in inv if c["in_scope"]),
                        "dropped_external": [c["concept_id"] for c in inv if not c["in_scope"]
                                             and "external" in c.get("out_of_scope_reason", "")],
                        "dropped_named_only": [c["concept_id"] for c in inv if not c["in_scope"]
                                               and "definition" in c.get("out_of_scope_reason", "")]}

    name_of = {c["concept_id"]: c["canonical_name"] for c in inv}
    logs = [{"node": "plan_los", "candidates": len(outcomes), "selected": len(selected),
             "budget": final_budget, "apply": len(apply_ids), "tier_counts": tier_counts,
             "flags": [f["flag"] for f in flags]}]
    snapshot = {**selection_summary, "budget": final_budget, "tier_counts": tier_counts,
                "flags": [f["flag"] for f in flags],
                "selected": [{"id": o["id"], "title": o["title"], "bloom": o["bloom_level"],
                              "concept": name_of.get(o["concept_id"], o["concept_id"]),
                              "weight": o.get("weight", 0)} for o in selected]}
    return {"outcomes": selected, "concept_inventory": inv, "concept_graph": concept_graph,
            "outcome_graph": outcome_graph, "allocation_plan": allocation,
            "backfill_pool": backfill_pool, "division_proposal": division_proposal,
            "selection_summary": selection_summary, "coverage_profile": coverage_profile,
            "overrides": overrides, "log": logs, "_snapshot": snapshot,
            "_detail": f"{len(selected)}/{len(outcomes)} kept · {len(apply_ids)} apply · "
                       + "/".join(f"{k[:3]}:{tier_counts[k]}" for k in TIER_ORDER)}
