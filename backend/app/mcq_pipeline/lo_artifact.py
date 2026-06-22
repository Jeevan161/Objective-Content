"""
app/mcq_pipeline/lo_artifact.py
-------------------------------
Terminal-stage helpers for the LO pipeline:

* `finalize(state)` — freeze the outcomes into a versioned, hashed artifact carried
  in state (NOT written to disk — the backend persists it on `McqRun.result`).
* `lo_to_legacy(...)` / `build_final_los(...)` — bridge the new AuthoredOutcome
  schema to the legacy `LearningOutcome` dict the question pipeline consumes,
  merging the in-session prerequisite closure with the DB cross-course prereq units.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date

from .lo_concept_graph import slugify, syntax_grounded
from .lo_config import QUESTION_BUDGET, SPEC_VERSION


# --- finalize (Node 10) ---------------------------------------------------- #
def finalize(state: dict) -> dict:
    """Assemble the ordered, frozen artifact. Status is FROZEN when validation
    passed; NEEDS_REVIEW when the run escalated (carries the escalation payload).
    Never raises. Returns the partial-state update {"artifact": ...}."""
    report = state.get("validation_report", {})
    failed = [k for k, v in report.items() if not v.get("pass")]
    escalation = None
    if failed:
        # We only reach finalize with failures when retries are exhausted (the
        # router sends still-fixable runs to repair) — flag, never raise (§16).
        escalation = {
            "reason": "validation failed after max retries",
            "session_id": state["session_id"],
            "failed_rules": failed,
            "report": {k: v for k, v in report.items() if not v.get("pass")},
            "retry_count": state.get("retry_count", 0),
        }
    status = "NEEDS_REVIEW" if escalation else "FROZEN"
    budget = state.get("allocation_plan", {}).get("question_budget", QUESTION_BUDGET)
    order = {t["topic_id"]: t["order"] for t in state["sections"]}
    outcomes = sorted(state["outcomes"], key=lambda o: (order.get(o["topic_id"], 99), o["id"]))

    # safety net: never ship an Apply outcome carrying syntax that doesn't ground in
    # the source — null it (repair-then-null policy, §16).
    for o in outcomes:
        if o.get("bloom_level") == "apply" and o.get("syntax"):
            if not syntax_grounded(o["syntax"], state["source_text"]):
                o["syntax"] = None

    canonical = json.dumps(outcomes, sort_keys=True, ensure_ascii=False).encode("utf-8")
    spec_hash = "sha256:" + hashlib.sha256(canonical).hexdigest()
    src_fp = "sha256:" + hashlib.sha256(state["source_text"].encode("utf-8")).hexdigest()
    got_ap = sum(o["bloom_level"] == "apply" for o in outcomes)

    artifact = {
        "session_id": state["session_id"],
        "spec_version": SPEC_VERSION,
        "status": status,
        "frozen_at": date.today().isoformat() if status == "FROZEN" else None,
        "spec_hash": spec_hash,
        "source_fingerprint": {"reading": src_fp},
        "question_budget": budget,
        "effective_bloom_split": {"remember_understand": len(outcomes) - got_ap, "apply": got_ap},
        "overrides": state.get("overrides", []),
        "validation_report": state.get("validation_report", {}),
        "outcomes": outcomes,
    }
    # Evidence-bound knowledge map (P1+P2): the topic/concept model the outcomes were
    # authored from — descriptions, taught depth, explained flag, and the two-level
    # (topic + concept) dependency graph. Lets the portal show WHY each outcome exists.
    graph = state.get("concept_graph") or {}
    artifact["knowledge_map"] = {
        "topics": [{"topic_id": s["topic_id"], "title": s.get("title", ""),
                    "description": s.get("description", "")} for s in state.get("sections", [])],
        "topic_edges": graph.get("topic_edges", []),
        "concepts": [{"concept_id": c["concept_id"], "name": c["canonical_name"],
                      "topic_id": c["topic_id"], "description": c.get("description", ""),
                      "taught_depth": c.get("taught_depth", c.get("depth_category")),
                      "explained": c.get("explained", c.get("in_scope", True)),
                      "in_scope": c.get("in_scope", True), "procedural": c.get("procedural", False)}
                     for c in state.get("concept_inventory", [])],
        "concept_edges": graph.get("edges", []),
        "assumed_prior": graph.get("assumed_prior", []),
        "coverage_profile": state.get("coverage_profile", {}),
    }
    # Return ONLY the artifact (which carries the sorted, frozen outcomes). Do NOT also
    # return a top-level "outcomes" — that REPLACE channel holds the working/repaired
    # outcomes, and overwriting it with the sorted snapshot conflates archival vs working
    # order (downstream reads artifact["outcomes"]).
    out = {"artifact": artifact}
    if escalation:
        artifact["escalation"] = escalation
        out["escalation"] = escalation
    return out


# --- legacy bridge --------------------------------------------------------- #
# Lossy Bloom map: the question pipeline keys off the old 4-level vocabulary.
_BLOOM_TO_LEGACY = {"apply": "apply", "remember_understand": "understand"}


def lo_to_legacy(outcome: dict, inv_by_id: dict, db_prereq_units: list,
                 sec_text: dict | None = None) -> dict:
    """Map one new AuthoredOutcome -> the legacy LearningOutcome dict shape that
    `recommend_for_los` / `generate_for_los` / `review_and_fix_for_los` expect."""
    cid = outcome.get("concept_id", "")
    inv = inv_by_id.get(cid, {})
    concept = inv.get("canonical_name") or outcome.get("title") or cid
    bloom = _BLOOM_TO_LEGACY.get(outcome.get("bloom_level", ""), "understand")
    # in-session prerequisite closure -> canonical names (audit-only; not consumed
    # by question generation, which keys off concept/sub_concept/syntax/evidence).
    in_session = [inv_by_id.get(p, {}).get("canonical_name", p) for p in outcome.get("prerequisites", [])]
    # The evidence's own section — the authoritative local span the outcome was drawn
    # from. Carried through so generation/review can ANCHOR on it (previously dropped).
    section_id = (outcome.get("source_evidence") or {}).get("section") or outcome.get("topic_id", "")
    section_text = ((sec_text or {}).get(section_id, "") or "")[:6000]
    return {
        "outcome": outcome.get("id"),
        "bloom_category": bloom,
        "bloom_level": bloom,
        "skill_type": outcome.get("skill_type") or ("practical_application" if bloom == "apply" else "conceptual"),
        "concept": concept,
        "sub_concept": cid[2:] if cid.startswith("C_") else cid,
        "description": outcome.get("description") or outcome.get("title") or "",
        "learner_action": outcome.get("learner_action") or "",
        "syntax": outcome.get("syntax") or "",
        "justification": outcome.get("justification") or "",
        "source_evidence": (outcome.get("source_evidence") or {}).get("quote", ""),
        # the evidence's section — the PRIMARY grounding span generation/review anchor on:
        "source_section": section_id,
        "source_section_text": section_text,
        # one Bloom level below the LO: a learner need only UNDERSTAND why a distractor is
        # wrong to answer an APPLY item. Drives the review distractor-depth audit.
        "expected_distractor_depth": {"apply": "understand"}.get(bloom, "remember"),
        # carried through for audit / UI (not read by generation):
        "prerequisites": db_prereq_units,
        "in_session_prerequisites": in_session,
        "prerequisite_scope": outcome.get("prerequisite_scope"),
        "bloom_level_raw": outcome.get("bloom_level"),
        "concept_id": cid,
    }


def build_final_los(state: dict, db_prereq_units: list) -> list[dict]:
    inv_by_id = {c["concept_id"]: c for c in state.get("concept_inventory", [])}
    sec_text = {s["topic_id"]: s.get("text", "") for s in state.get("sections", [])}
    src = state.get("artifact", {}).get("outcomes") or state.get("outcomes", [])
    return [lo_to_legacy(o, inv_by_id, db_prereq_units, sec_text) for o in src]
