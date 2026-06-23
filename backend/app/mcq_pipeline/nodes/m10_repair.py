"""LO pipeline · Node 9 — repair (regenerate-with-feedback loop)."""
from __future__ import annotations

import json
from collections import Counter

from app.mcq_pipeline.config import (MAX_RETRIES, REMEMBER_VERBS, TEMP_AUTHOR, TIER_ORDER, VERBS,
                                     feasible_tiers)
from app.mcq_pipeline.utils.concept_graph import ground_quote
from app.mcq_pipeline.utils.llm import chat, parse_json
from app.mcq_pipeline.prompts.store import get_prompt, register
from app.mcq_pipeline.nodes._common import _bind_rag, _prog
from app.mcq_pipeline.nodes.m02_generate_outcomes import _coerce_outcome


# ── Node 9 · repair (A · loop) ────────────────────────────────────────────── #
# DB-overridable (sentinel placeholders substituted in code).
_REPAIR_SYS = register("lo.repair_sys", (
    "Rewrite ONE learning outcome so it PASSES every rubric criterion it currently fails. Make the "
    "MINIMAL change needed: fix only what the failed criteria require and preserve the outcome's "
    "original intent everywhere else.\n\n"

    "HARD CONSTRAINTS (never violate):\n"
    '- Keep bloom_level = "<TIER>" and concept_id = "<CONCEPT_ID>" EXACTLY (the tier label and '
    "concept are fixed by the plan; do not change them).\n"
    "- learner_action MUST be one of <VERBS>. Within that set you MAY switch to a LOWER-demand verb "
    "when the current one over-reaches what the material supports — this is the INTENDED way to fix "
    "a depth (R2) or apply-validity (R8) failure WITHOUT changing the tier.\n"
    "- Return ONLY one JSON object with the SAME keys as the CURRENT OUTCOME shown below.\n\n"

    "GROUNDING (use ONLY the provided SECTION TEXT):\n"
    "- Introduce NO fact, term, value, comparison, or sub-topic that is not explicitly present in "
    "the SECTION TEXT. Make no inference beyond the evidence.\n"
    "- The evidence 'quote' must be copied verbatim from the SECTION TEXT.\n"
    "- If the material cannot support the outcome even at the lowest verb in <VERBS>, write the "
    "SIMPLEST fully-supported outcome you can — never invent content to satisfy a criterion.\n\n"

    "SELF-CONTAINED & TRANSFERABLE:\n"
    "- title and description must state the GENERAL concept/skill and stand on their own.\n"
    "- NEVER reference a source-local entity: a scenario label ('Project A'/'Project B'), a sample "
    "variable/file/function name, a character, a dataset name, or a one-off numeric value — "
    "generalise it. Technologies/tools genuinely taught by name MAY be named.\n\n"

    "HOW TO FIX (address each FAILED RUBRIC CRITERION):\n"
    "- R1_present / R3_answerable: re-anchor the outcome on what the SECTION TEXT actually states.\n"
    "- R2_depth: lower the verb within <VERBS> and/or narrow the claim to the taught depth.\n"
    "- R4_in_scope: remove the beyond-scope leap; keep only what the section covers.\n"
    "- R6_self_contained: replace any source-local entity with the general concept.\n"
    "- R7_distinct: sharpen to the ONE idea this outcome should test.\n"
    "- R8_apply_valid: make the action a concrete procedure the section demonstrates, or lower the verb.\n\n"

    "Directly address the FAILED RUBRIC CRITERIA, the JUDGE FEEDBACK, and the SUGGESTED FIX below. "
    "Do not over-edit: leave untouched anything the failed criteria do not require you to change."
))


def _topic_of(state, tid):
    return next(t for t in state["sections"] if t["topic_id"] == tid)


# Safe default verb per tier for the deterministic grounded fallback (each ∈ VERBS[tier]).
_DEFAULT_VERB = {"remember": "identify", "understand": "describe", "apply": "apply", "scenario": "apply"}


def _feasible_target(concept: dict | None, current_tier: str) -> str:
    """Tier the deterministic terminal fallback grounds to: the HIGHEST tier the material supports
    that does NOT exceed the current tier AND is at most 'understand'. Capped at understand because a
    bare grounded remember/understand LO always satisfies R1–R8 + V5/V6, whereas apply/scenario would
    owe authored prerequisites + a demonstrated method we won't fabricate here. Graduated
    (apply→understand when feasible) instead of a blunt drop to 'remember'."""
    rank = {t: i for i, t in enumerate(TIER_ORDER)}
    c = concept or {}
    depth = c.get("depth_category") or c.get("taught_depth") or "moderate"
    feas = feasible_tiers(depth, bool(c.get("procedural")))
    cap = min(rank.get(current_tier, 0), rank["understand"])
    below = [t for t in feas if rank[t] <= cap]
    return max(below, key=lambda t: rank[t]) if below else "remember"


def _ground_recall(o: dict, name: str, state: dict, tier: str, verb: str, title: str, why: str) -> None:
    """Rewrite an outcome in place as a grounded LO at `tier` (used by deterministic fixes)."""
    o.update({"learner_action": verb, "bloom_level": tier, "scenario": tier == "scenario",
              "skill_type": "practical_application" if tier in ("apply", "scenario") else "conceptual",
              "title": title, "description": title if title.endswith(".") else title + ".",
              "source_evidence": {"quote": ground_quote(name, _topic_of(state, o["topic_id"])["text"]),
                                  "section": o["topic_id"]},
              "justification": why, "prerequisites": [], "prerequisite_scope": None})


def repair(state, config) -> dict:
    """Regenerate-with-feedback. Structural failures (V4 coverage gap, V14 out-of-scope target) get
    a cheap deterministic retarget. Rubric + item failures (V13/V5/V6/V8/V9/V10) are REGENERATED by
    the LLM with the Judge's per-criterion reasons + suggested fix, KEEPING the assigned tier and
    concept. On the FINAL attempt, any still-failing rubric LO is GROUNDED to the highest tier the
    material supports but no higher than 'understand' — a graduated downgrade (not a blunt drop to
    recall) so the loop always converges; a real tier downgrade is flagged NEEDS_REVIEW."""
    _bind_rag(config)
    prog = _prog(config)
    attempt = state.get("retry_count", 0) + 1
    prog.start("repair", detail=f"attempt {attempt}")
    rep = state["validation_report"]
    reviews = state.get("lo_reviews") or {}
    outcomes = [dict(o) for o in state["outcomes"]]
    by_id = {o["id"]: o for o in outcomes}
    inv_all = state["concept_inventory"]
    name_of = {c["concept_id"]: c["canonical_name"] for c in inv_all}
    concept_by_id = {c["concept_id"]: c for c in inv_all}
    last_attempt = attempt >= MAX_RETRIES
    logs = []

    # (1) coverage gap (V4): retarget a donor LO (a concept covered >1x) onto the missing concept.
    cover = Counter(o["concept_id"] for o in outcomes)
    for mid in rep.get("V4", {}).get("failing", []):
        donor = next((o for o in outcomes if cover[o["concept_id"]] > 1), None)
        if not donor:
            break
        cover[donor["concept_id"]] -= 1
        cover[mid] += 1
        c = next(c for c in inv_all if c["concept_id"] == mid)
        donor["concept_id"] = mid
        donor["topic_id"] = c["topic_id"]
        _ground_recall(donor, c["canonical_name"], state, "remember", "identify",
                       f"Identify {c['canonical_name']}", "Repaired to close a coverage gap.")
        logs.append({"node": "repair", "fix": "coverage_v4", "concept": mid})

    # (1a) out-of-scope target (V14): retarget onto an in-scope concept (prefer an uncovered one).
    in_scope = [c for c in inv_all if c.get("in_scope")]
    in_scope_ids = {c["concept_id"] for c in in_scope}
    for oid in rep.get("V14", {}).get("failing", []):
        o = by_id.get(oid)
        if not o or o["concept_id"] in in_scope_ids or not in_scope:
            continue
        tgt = next((c for c in in_scope if cover[c["concept_id"]] == 0), None) \
            or next((c for c in in_scope if c["concept_id"] != o["concept_id"]), in_scope[0])
        cover[o["concept_id"]] -= 1
        cover[tgt["concept_id"]] += 1
        o["concept_id"] = tgt["concept_id"]
        o["topic_id"] = tgt["topic_id"]
        _ground_recall(o, tgt["canonical_name"], state, "remember", "identify",
                       f"Identify {tgt['canonical_name']}", "Repaired: retargeted off a non-explained concept.")
        logs.append({"node": "repair", "fix": "explained_only_v14", "id": oid, "to": tgt["concept_id"]})

    # (2) rubric + item failures: REGENERATE keeping the assigned (tier, concept) + judge feedback.
    # V15 (apply prereq not fully covered) is repairable the same way as V6 — regeneration can lower
    # the verb / narrow the claim, and the terminal pass downgrades the tier when a prereq is missing.
    failing = set()
    for rid in ("V13", "V8", "V9", "V10", "V5", "V6", "V15"):
        failing.update(rep.get(rid, {}).get("failing", []))
    for oid in failing:
        o = by_id.get(oid)
        if not o:
            continue
        tier, cid = o["bloom_level"], o["concept_id"]
        name = name_of.get(cid, o.get("title", "this concept"))
        rv = reviews.get(oid) or {}
        rubric_fail = oid in rep.get("V13", {}).get("failing", [])
        # An apply outcome with an uncovered prerequisite (V15/V6) that survives to the final attempt
        # is grounded down to a tier the material fully supports — same terminal path as a rubric fail.
        prereq_fail = oid in rep.get("V15", {}).get("failing", []) or oid in rep.get("V6", {}).get("failing", [])
        if last_attempt and (rubric_fail or prereq_fail):
            # terminal fallback: ground to the HIGHEST tier the material supports (capped at
            # understand) so it always satisfies R1–R8 and the loop converges — a graduated
            # downgrade, not a blunt drop to recall. A real tier drop is surfaced via overrides below.
            target = _feasible_target(concept_by_id.get(cid), tier)
            allowed = VERBS.get(target, REMEMBER_VERBS)
            sugg = (rv.get("suggested_fix") or "").strip()
            first = sugg.split()[0].lower() if sugg else ""
            verb = first if first in allowed else _DEFAULT_VERB.get(target, "identify")
            title = sugg if (first in allowed and sugg) else f"{verb.capitalize()} {name}"
            _ground_recall(o, name, state, target, verb, title,
                           f"Repaired (final): grounded to the highest tier the material supports ({target}).")
            logs.append({"node": "repair", "fix": "terminal_grounded", "id": oid, "tier": target})
            continue
        topic = _topic_of(state, o["topic_id"])
        inv = [c for c in inv_all if c["topic_id"] == o["topic_id"]] or inv_all
        reasons = [rid for rid in ("V5", "V6", "V8", "V9", "V10", "V13")
                   if oid in rep.get(rid, {}).get("failing", [])]
        sys = (get_prompt("lo.repair_sys", _REPAIR_SYS)
               .replace("<TIER>", tier).replace("<CONCEPT_ID>", cid)
               .replace("<VERBS>", str(sorted(VERBS.get(tier, set())))))
        # Pass the SPECIFIC failing rubric criteria (R1–R8), not just the V-code — the Judge stored
        # them per-criterion in rv["rubric"], so the model knows exactly what to fix.
        failed_criteria = [k for k, ok in (rv.get("rubric") or {}).items() if not ok]
        usr = (f"FAILED RUBRIC CRITERIA: {failed_criteria or reasons}\n"
               f"JUDGE FEEDBACK: {rv.get('fail_reason', '')}\n"
               f"SUGGESTED FIX: {rv.get('suggested_fix', '')}\n"
               f"CURRENT OUTCOME: {json.dumps(o)}\n"
               f"SECTION TEXT:\n{topic['text'][:2500]}\n"
               f"This outcome MUST stay concept_id={cid}, bloom_level={tier}.")
        fixed = parse_json(chat([{"role": "system", "content": sys},
                                 {"role": "user", "content": usr}], temperature=TEMP_AUTHOR))
        if isinstance(fixed, dict):
            merged = _coerce_outcome(fixed, topic, {"concept_id": cid, "tier": tier}, inv)
            merged["id"] = o["id"]
            o.update(merged)
            logs.append({"node": "repair", "fix": "regenerate", "id": oid, "rules": reasons})

    # Reconcile allocation_plan with the repaired outcome set. Structural retargets (V4/V14) and
    # the terminal recall can move an LO's tier/topic; recomputing tier_counts + per-topic slots
    # here keeps V2/V3 validating INTERNAL consistency (they already checked the author's first
    # pass against the Gate-1 division) instead of spuriously failing after a legitimate fix.
    plan = dict(state["allocation_plan"])
    rebuilt = {tid: {**p, "slots": 0, "bloom": {k: 0 for k in TIER_ORDER}, "assignments": []}
               for tid, p in plan.get("by_topic", {}).items()}
    tier_counts = {k: 0 for k in TIER_ORDER}
    for o in outcomes:
        tier = o["bloom_level"]
        tier_counts[tier] = tier_counts.get(tier, 0) + 1
        row = rebuilt.get(o["topic_id"])
        if row is not None:
            row["slots"] += 1
            row["bloom"][tier] = row["bloom"].get(tier, 0) + 1
    plan["by_topic"] = rebuilt
    plan["tier_counts"] = tier_counts

    # A genuine tier DOWNGRADE (material couldn't support the planned tier) is surfaced EXPLICITLY
    # for human review, instead of leaking out as a confusing V2 tier-count mismatch.
    rank = {t: i for i, t in enumerate(TIER_ORDER)}
    orig = {o["id"]: o["bloom_level"] for o in state["outcomes"]}
    downgraded = sorted(o["id"] for o in outcomes
                        if rank.get(o["bloom_level"], 0) < rank.get(orig.get(o["id"], o["bloom_level"]), 0))
    overrides = ([{"rule": "tier_downgraded", "ids": downgraded,
                   "reason": "repair lowered these outcomes below their planned Bloom tier — the "
                             "material did not support it; recommend human review"}]
                 if downgraded else [])
    snapshot = {"attempt": attempt, "fixes": logs, "downgraded": downgraded}
    prog.done("repair", detail=(f"{len(downgraded)} downgraded" if downgraded else "reconciled"),
              snapshot=snapshot)
    return {"outcomes": outcomes, "allocation_plan": plan, "retry_count": attempt,
            "overrides": overrides, "log": logs}


