"""plan_los · the specialized LLM sub-agents (phase logic).

Each phase is its own LLM role with its own prompt; the graph nodes in :mod:`node` wire them in
deterministic order (option B — phases as first-class nodes). Two phases carry a GATED critic/reviser
sub-agent (m01-style reflexion): a second LLM pass that only fires when the first output looks off,
reviews it against the phase goal, and returns a corrected version validated by the same guards.

  author_section   → candidate outcomes for one section          [lo.generate_sys]
  consolidate      → semantic merge groups + taught depth        [lo.consolidate_sys]
                       └ gated critic                            [lo.consolidate_critic_sys]
  plan             → preferred budget-aware outcome selection     [lo.plan_sys]
                       └ gated critic                            [lo.plan_critic_sys]
"""
from __future__ import annotations

import json

from app.mcq_pipeline.config import EXCLUDED_QUESTION_TYPES, TEMP_AUTHOR
from app.mcq_pipeline.prompts.store import get_prompt
from app.mcq_pipeline.utils.llm import chat, parse_json
from app.mcq_pipeline.nodes.m02_plan_los import gate
from app.mcq_pipeline.nodes.m07_recommend_question_type import recommend_one
from app.mcq_pipeline.nodes.m08_generate_questions import _course_is_sql
from app.mcq_pipeline.nodes.m02_plan_los.prompts import (CONSOLIDATE_CRITIC_SYS, CONSOLIDATE_SYS,
                                                         PLAN_CRITIC_SYS, PLAN_SYS,
                                                         generate_sys_verb_subbed)


# ── PHASE 1 · per-section author ──────────────────────────────────────────── #
def author_section(topic: dict, sys: str | None = None) -> list:
    sys = sys or generate_sys_verb_subbed()
    data = parse_json(chat([{"role": "system", "content": sys},
                            {"role": "user", "content":
                             f"SECTION: {topic['title']} ({topic['topic_id']})\n\n{topic['text'][:6000]}"}],
                           temperature=TEMP_AUTHOR)) or []
    items = data if isinstance(data, list) else []
    return [p for it in items if isinstance(it, dict) and (p := gate.proto_outcome(it, topic))]


# ── PHASE 1b · consolidation (+ gated critic) ─────────────────────────────── #
def _sub_concept_payload(protos: list) -> list:
    idx: dict = {}
    for o in protos:
        key = o["_sub_concept_name"]
        if key not in idx:
            idx[key] = {"name": key, "parent": o["_concept_name"],
                        "evidence": (o.get("source_evidence") or {}).get("quote", "")[:300]}
    return list(idx.values())


def _consolidate_call(subs: list, source_text: str) -> list:
    payload = {"reading": (source_text or "")[:12000], "sub_concepts": subs}
    try:
        data = parse_json(chat(
            [{"role": "system", "content": get_prompt("lo.consolidate_sys", CONSOLIDATE_SYS)},
             {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}], temperature=0))
    except Exception:  # noqa: BLE001
        return []
    return data if isinstance(data, list) else []


def _consolidation_suspicious(groups: list, n_subs: int) -> bool:
    """Gate the critic: fire only when the merge looks off — collapsed too hard (far fewer groups
    than sub-concepts), a group fusing many members, or a missing/invalid depth."""
    if not groups:
        return False
    if n_subs >= 6 and len(groups) < max(2, n_subs // 3):
        return True
    for g in groups:
        if not isinstance(g, dict):
            return True
        if len(g.get("members", []) or []) > 5:
            return True
        if str(g.get("depth", "")).lower() not in ("named", "mention", "moderate", "deep"):
            return True
    return False


def _consolidate_critic(subs: list, groups: list) -> list | None:
    """Reviewer pass over the proposed grouping. Returns a corrected group list, or None to keep."""
    payload = {"sub_concepts": subs, "proposed_groups": groups}
    try:
        data = parse_json(chat(
            [{"role": "system", "content": get_prompt("lo.consolidate_critic_sys", CONSOLIDATE_CRITIC_SYS)},
             {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}], temperature=0))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict) or data.get("ok") is True:
        return None
    revised = data.get("groups")
    return revised if isinstance(revised, list) and revised else None


def consolidate(protos: list, source_text: str) -> tuple[list, dict]:
    """Semantic merge + taught depth, with a gated critic. Returns (groups, meta) — meta records
    whether the critic fired, for the trace. [] groups on failure → the gate treats each
    sub-concept standalone at moderate depth (degraded but never blocking)."""
    subs = _sub_concept_payload(protos)
    if not subs:
        return [], {"critic_gated": False, "critic_fired": False}
    groups = _consolidate_call(subs, source_text)
    gated = bool(groups) and _consolidation_suspicious(groups, len(subs))
    fired = False
    if gated:
        revised = _consolidate_critic(subs, groups)
        if revised:
            groups, fired = revised, True
    return groups, {"critic_gated": gated, "critic_fired": fired}


# ── PHASE 3 · selection proposal (+ gated critic) ─────────────────────────── #
def _plan_inputs(inv: list, outcomes: list) -> tuple[list, list]:
    weight_of: dict = {}
    for o in outcomes:
        weight_of[o["concept_id"]] = max(weight_of.get(o["concept_id"], 0), o.get("weight", 0))
    in_scope = {c["concept_id"] for c in inv if c.get("in_scope")}
    by_id = {c["concept_id"]: c for c in inv}
    concepts = [{"concept_id": c["concept_id"], "name": c["canonical_name"],
                 "parent": c["parent_concept"], "depth": c.get("depth_category", "moderate"),
                 "procedural": bool(c.get("procedural")), "weight": weight_of.get(c["concept_id"], 0)}
                for c in inv if c.get("in_scope")]
    cands = [{"id": o["id"], "concept_id": o["concept_id"],
              "parent": by_id.get(o["concept_id"], {}).get("parent_concept", ""),
              "bloom_level": o["bloom_level"], "title": o["title"]}
             for o in outcomes if o["concept_id"] in in_scope]
    return concepts, cands


def _plan_call(concepts: list, cands: list, budget: int) -> list:
    payload = {"budget": budget, "concepts": concepts, "candidates": cands}
    try:
        data = parse_json(chat(
            [{"role": "system", "content": get_prompt("lo.plan_sys", PLAN_SYS)},
             {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}], temperature=0))
    except Exception:  # noqa: BLE001
        return []
    if isinstance(data, dict) and isinstance(data.get("selected_ids"), list):
        return [i for i in data["selected_ids"] if isinstance(i, str)]
    return []


def _plan_suspicious(prefer: list, concepts: list, cands: list, budget: int) -> bool:
    """Gate the critic: the gate ALWAYS enforces coverage deterministically, so the critic only
    earns a call on a DEGENERATE proposal — empty, or covering well under half the broad concepts."""
    if not prefer:
        return bool(cands)
    parents = {c["parent"] for c in concepts if c.get("parent")}
    chosen = {next((c["parent"] for c in cands if c["id"] == i), None) for i in prefer}
    chosen.discard(None)
    return bool(parents) and len(chosen) < max(1, len(parents) // 2)


def _plan_critic(concepts: list, cands: list, budget: int, prefer: list) -> list | None:
    payload = {"budget": budget, "concepts": concepts, "candidates": cands,
               "proposed_selected_ids": prefer}
    try:
        data = parse_json(chat(
            [{"role": "system", "content": get_prompt("lo.plan_critic_sys", PLAN_CRITIC_SYS)},
             {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}], temperature=0))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict) or data.get("ok") is True:
        return None
    revised = data.get("selected_ids")
    return [i for i in revised if isinstance(i, str)] if isinstance(revised, list) and revised else None


# ── PHASE 3b · plan the question TYPE alongside each LO ───────────────────── #
def recommend_type(o: dict) -> dict:
    """Recommend the question TYPE for ONE selected LO (one LLM call, deterministic fallback).
    Adapts the LO-stage outcome to the recommender's expected shape, so the type is planned WITH the
    outcome — making each LO a complete planned unit (concept + tier + type). Returns a copy of the
    outcome with `question_type` + `question_type_rationale`."""
    adapted = {"outcome": o.get("title") or o.get("description"),
               "bloom_category": o.get("bloom_level"), "bloom_level_raw": o.get("bloom_level"),
               "is_scenario": bool(o.get("scenario")), "skill_type": o.get("skill_type"),
               "learner_action": o.get("learner_action"), "syntax": o.get("syntax"),
               "concept": (o.get("concept_id") or "").replace("C_", ""),
               "description": o.get("description")}
    try:
        rec = recommend_one(adapted)
    except Exception:  # noqa: BLE001 — never block planning on the type call
        rec = {"question_type": "MULTIPLE_CHOICE", "question_type_rationale": "fallback"}
    return {**o, "question_type": rec.get("question_type", "MULTIPLE_CHOICE"),
            "question_type_rationale": rec.get("question_type_rationale", "")}


def feasible_question_types(o: dict) -> list[str]:
    """Plausible question FORMATS for an LO (primary first) — used to create same-outcome variants
    in a DIFFERENT question type when filling toward the target count. apply/scenario outcomes that
    carry code also get a fill-in-code variant: SQL_FIB_CODING for SQL courses, FIB_CODING for
    programming languages (Python/Java/JS). Excluded types are filtered out."""
    has_syntax = bool((o.get("syntax") or "").strip())
    is_apply = (o.get("bloom_level") or "").lower() in ("apply", "scenario")
    if has_syntax:
        types = ["CODE_ANALYSIS_MULTIPLE_CHOICE", "MULTIPLE_CHOICE",
                 "CODE_ANALYSIS_MORE_THAN_ONE_MULTIPLE_CHOICE", "TRUE_OR_FALSE"]
        if is_apply:                                    # write/complete-code variant for apply-code LOs
            types = [("SQL_FIB_CODING" if _course_is_sql() else "FIB_CODING")] + types
    else:
        types = ["MULTIPLE_CHOICE", "TRUE_OR_FALSE", "MORE_THAN_ONE_MULTIPLE_CHOICE"]
    return [t for t in types if t not in EXCLUDED_QUESTION_TYPES]


def expand_to_target(outcomes: list, target: int) -> list:
    """ENFORCE the target outcome count: when there are fewer distinct outcomes than `target`, fill
    toward it with SAME-outcome variants in a DIFFERENT question type (one concept assessed via
    several formats), spread round-robin across the base outcomes so no single concept stacks all
    variants. Best-effort — a thin session that can't justify the target stays below it."""
    out = list(outcomes)
    if len(out) >= target or not outcomes:
        return out
    used = {(o["concept_id"], o["bloom_level"], o.get("question_type")) for o in out}
    alts = [[t for t in feasible_question_types(o) if t != o.get("question_type")] for o in outcomes]
    depth = 0
    while len(out) < target:
        added = False
        for o, types in zip(outcomes, alts):
            if depth >= len(types):
                continue
            t = types[depth]
            key = (o["concept_id"], o["bloom_level"], t)
            if key in used:
                continue
            used.add(key)
            out.append({**o, "id": f"{o['id']}__{t.lower()}", "question_type": t,
                        "question_type_rationale": "same outcome assessed in a different question "
                        "format (added to reach the target count)", "_variant_of": o["id"]})
            added = True
            if len(out) >= target:
                break
        depth += 1
        if not added:                       # no more feasible variants — accept fewer
            break
    return out


def plan(inv: list, outcomes: list, budget: int) -> tuple[list, dict]:
    """Budget-aware selection PROPOSAL (preferred id order), with a gated critic. Returns
    (prefer_ids, meta). The gate guarantees coverage + budget regardless; this only steers fill
    order. [] → pure deterministic selection."""
    concepts, cands = _plan_inputs(inv, outcomes)
    if not cands:
        return [], {"critic_gated": False, "critic_fired": False}
    prefer = _plan_call(concepts, cands, budget)
    gated = _plan_suspicious(prefer, concepts, cands, budget)
    fired = False
    if gated:
        revised = _plan_critic(concepts, cands, budget, prefer)
        if revised is not None:
            prefer, fired = revised, True
    return prefer, {"critic_gated": gated, "critic_fired": fired}
