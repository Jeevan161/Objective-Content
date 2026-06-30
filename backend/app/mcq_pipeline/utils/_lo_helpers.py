"""Shared LO helpers — outcome coercion + budget backfill.

These were originally defined inside the (now removed) staged nodes m02_generate_outcomes and
m06_plan_outcomes. They are consumed by nodes that OUTLIVE the m02-m06 collapse — the repair loop
(`m05_repair`) re-coerces a regenerated outcome onto its fixed (concept, tier), and both the repair
loop and the quality review (`m04_review_and_validate`) top the outcome set back up toward the
budget after a drop. Hosting them here gives those nodes a stable import that does not depend on the
LO-generation node's internal layout.

* `_tier_of` / `_DEFAULT_VERB` / `_coerce_outcome` — clamp one LLM item onto a FIXED
  (concept_id, Bloom tier): verb clamped into that tier's controlled vocabulary, evidence re-grounded.
* `backfill_to_budget` — refill the outcome set toward `budget` from the highest-weighted unselected
  candidates, never reintroducing a duplicate `(concept, Bloom)` pair or an excluded concept.
"""
from __future__ import annotations

from app.mcq_pipeline.config import SKILL_TYPES, TIER_ORDER, VERBS
from app.mcq_pipeline.utils.concept_graph import ground_quote, slugify

_RANK = {t: i for i, t in enumerate(TIER_ORDER)}

# Safe default verb per tier (each ∈ VERBS[tier]) — used when an item's verb is missing/out-of-vocab.
_DEFAULT_VERB = {"remember": "identify", "understand": "explain",
                 "apply": "apply", "scenario": "apply"}


def _tier_of(item: dict) -> str:
    """Map an LLM item's declared bloom_level to one of the 4 canonical tiers ('' if unknown)."""
    b = str(item.get("bloom_level") or item.get("tier") or "").lower()
    if b.startswith("scen"):
        return "scenario"
    if b.startswith("appl"):
        return "apply"
    if b.startswith("under"):
        return "understand"
    if b.startswith("rem"):
        return "remember"
    return ""


def _coerce_outcome(item: dict, topic: dict, assignment: dict, inv: list) -> dict:
    """Coerce one LLM item onto a FIXED (concept_id, Bloom tier) assignment. Used by the repair
    node to keep a regenerated outcome on its planned concept + tier (verb clamped into that tier's
    controlled vocabulary; evidence re-grounded)."""
    tier = assignment["tier"]
    cid = assignment["concept_id"]
    cur = next((c for c in inv if c["concept_id"] == cid), None)
    cname = cur["canonical_name"] if cur else cid[2:].replace("_", " ")
    verb = str(item.get("learner_action", "")).lower().strip()
    if verb not in VERBS[tier]:
        verb = _DEFAULT_VERB[tier]
    title = (item.get("title") or f"{verb.title()} {cname}").strip()
    skill = item.get("skill_type")
    if skill not in SKILL_TYPES:
        skill = "practical_application" if tier in ("apply", "scenario") else "conceptual"
    quote = ground_quote(cname, topic["text"])
    return {"id": slugify(f"{verb}_{cid[2:]}"), "title": title, "topic_id": topic["topic_id"],
            "concept_id": cid, "bloom_level": tier, "scenario": tier == "scenario",
            "skill_type": skill, "learner_action": verb,
            "description": (item.get("description") or title).strip(),
            "syntax": (item.get("syntax") or None),
            "prerequisites": [], "prerequisite_scope": None, "target_questions": 1,
            "source_evidence": {"quote": quote, "section": topic["topic_id"]},
            "justification": (item.get("justification") or "Grounded in section evidence.").strip()}


def backfill_to_budget(outcomes: list, pool: list, budget: int,
                       exclude_concepts: frozenset = frozenset()) -> tuple[list, list]:
    """Top the outcome set back up toward `budget` using the highest-weighted UNSELECTED candidates
    from `pool` — keeping budget a TARGET, not just a ceiling. Used after dedup drops a duplicate and
    after repair drops a not-taught (R1) outcome. Skips: ids already present, a `(concept, Bloom)`
    pair already present (so it can't reintroduce a just-deduped twin), and `exclude_concepts` (e.g.
    concepts the judge said aren't taught). No per-concept cap — the `(concept, Bloom)` pair guard is
    the bound. Best-effort — returns (outcomes, added_ids); FEWER than budget if the pool is exhausted
    (quality over count)."""
    if len(outcomes) >= budget or not pool:
        return list(outcomes), []
    present_ids = {o["id"] for o in outcomes}
    present_pairs = {(o["concept_id"], o["bloom_level"]) for o in outcomes}
    ranked = sorted(pool, key=lambda o: (-o.get("weight", 0), -_RANK.get(o.get("bloom_level"), 0),
                                         o.get("dag_depth", 0), o.get("id", "")))
    out, added = list(outcomes), []
    for cand in ranked:
        if len(out) >= budget:
            break
        cid, pair = cand.get("concept_id"), (cand.get("concept_id"), cand.get("bloom_level"))
        if cand.get("id") in present_ids or pair in present_pairs:
            continue
        if cid in exclude_concepts:
            continue
        out.append(dict(cand))
        added.append(cand["id"])
        present_ids.add(cand["id"])
        present_pairs.add(pair)
    return out, added
