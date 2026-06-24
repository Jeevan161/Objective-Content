"""LO pipeline (LO-first) · Node 9 — validate (structural invariants + rubric gate).

The deterministic gate over the FROZEN-candidate outcome set. In the LO-first flow the budget is a
selection ceiling, not an allocation contract, so the old count/tier/slot checks (V1–V3) are gone;
what remains are the quality, grounding, coverage, prerequisite, and uniqueness invariants:

  V4  every in-scope BROAD concept (parent_concept) is targeted by >=1 outcome (coverage).
  V5  every apply/scenario outcome carries a non-empty prerequisite set.
  V6  apply/scenario prerequisite closure is fully in-scope or assumed (RAG-verified scope).
  V7  the concept dependency graph is acyclic.
  V8  apply/scenario outcomes target a procedural concept (applied_skill vote).
  V9  each outcome's evidence quote appears verbatim in the source.
  V10 each outcome's action verb is in its Bloom tier's controlled vocabulary.
  V13 every outcome passes all unified-Judge rubric criteria R1–R8.
  V14 no outcome targets an out-of-scope (non-explained) concept.
  V15 every apply/scenario outcome has ALL required prerequisites COVERED (none uncovered).  [new]
  V16 outcomes are unique — no two test the same (concept, Bloom level).                     [new]

Input:  state["outcomes"], concept_inventory, concept_graph, lo_reviews, source_text.
Output: validation_report {Vk: {pass, detail, failing}}  +  a log row.
"""
from __future__ import annotations

from collections import Counter

from app.mcq_pipeline.config import VERBS
from app.mcq_pipeline.utils.concept_graph import loosen_text
from app.mcq_pipeline.nodes._common import _prog


def validate(state, config) -> dict:
    prog = _prog(config)
    prog.start("validate")
    O = state["outcomes"]
    rep: dict = {}
    src = loosen_text(state["source_text"])
    inv = state["concept_inventory"]
    inv_ids = {c["concept_id"] for c in inv if c["in_scope"]}
    apply_like = ("apply", "scenario")

    def rule(rid, ok, detail="", items=None):
        rep[rid] = {"pass": bool(ok), "detail": detail, "failing": items or []}

    # V4 — coverage is keyed on the BROAD concept (parent_concept), not the fine sub-concept: every
    # in-scope broad concept must be targeted by >=1 outcome (a topic may decompose into many fine
    # sub-concepts; we don't require each one covered — that would blow past the budget). `failing`
    # still lists concept_ids (one in-scope representative per uncovered broad concept) so repair's
    # donor-retarget can act on it.
    parent_of = {c["concept_id"]: (c.get("parent_concept") or c["concept_id"]) for c in inv}
    covered_parents = {parent_of.get(o["concept_id"], o["concept_id"]) for o in O}
    uncovered_reps, seen_parents = [], set()
    for c in inv:
        if not c["in_scope"]:
            continue
        p = parent_of[c["concept_id"]]
        if p not in covered_parents and p not in seen_parents:
            uncovered_reps.append(c["concept_id"])
            seen_parents.add(p)
    rule("V4", not uncovered_reps,
         "every in-scope broad concept must be targeted by at least one outcome "
         "(one representative sub-concept id per uncovered broad concept listed)",
         sorted(uncovered_reps))
    off_scope = [o["id"] for o in O if o["concept_id"] not in inv_ids]
    rule("V14", not off_scope,
         "no outcome may target an out-of-scope (non-explained) concept", off_scope)
    no_pre = [o["id"] for o in O if o["bloom_level"] in apply_like and not o.get("prerequisites")]
    rule("V5", not no_pre,
         "every apply/scenario outcome must carry a non-empty prerequisite set", no_pre)
    oos = [o["id"] for o in O if o["bloom_level"] in apply_like
           and o.get("prerequisite_scope") == "has_out_of_scope"]
    rule("V6", not oos,
         "apply/scenario prerequisite closure must be fully in-scope or assumed (RAG-verified)", oos)
    rule("V7", state["concept_graph"]["acyclic"], "the concept dependency graph must be acyclic")
    proc = {c["concept_id"]: bool(c.get("procedural")) for c in inv}
    fake = [o["id"] for o in O if o["bloom_level"] in apply_like and not proc.get(o["concept_id"], False)]
    rule("V8", not fake,
         "apply/scenario outcomes must target a procedural concept (applied_skill vote)", fake)
    ungrounded = [o["id"] for o in O
                  if not (o.get("source_evidence") or {}).get("quote", "").strip()
                  or loosen_text((o.get("source_evidence") or {}).get("quote", ""))[:60] not in src]
    rule("V9", not ungrounded,
         "each outcome's evidence quote must appear verbatim in the source text", ungrounded)
    badverb = [o["id"] for o in O if o["learner_action"] not in VERBS.get(o["bloom_level"], set())]
    rule("V10", not badverb,
         "each outcome's action verb must be in its Bloom tier's controlled vocabulary", badverb)

    # composite rubric gate (V13): every LO must pass every R1–R8 criterion.
    reviews = state.get("lo_reviews") or {}
    not_covered = [o["id"] for o in O if (rv := reviews.get(o["id"])) and not rv.get("covered", True)]
    rule("V13", not not_covered,
         "every outcome must pass all unified-Judge rubric criteria R1-R8 (failures in lo_reviews)",
         not_covered)

    # V15 — apply/scenario outcomes must have EVERY required prerequisite covered (the LO-first
    # flow's explicit "all prerequisites covered for all apply" gate). uncovered = present-but-
    # missing prior knowledge; repair downgrades the tier when a prereq cannot be covered.
    uncovered = [o["id"] for o in O if o["bloom_level"] in apply_like
                 and (o.get("prerequisite_coverage") or {}).get("uncovered")]
    rule("V15", not uncovered,
         "every apply/scenario outcome must have all required prerequisites covered", uncovered)

    # V16 — uniqueness: no two outcomes test the same (concept_id, Bloom level). review_outcomes_
    # quality dedups before this, so it should always pass; kept as a guard.
    seen = Counter((o["concept_id"], o["bloom_level"]) for o in O)
    dups = [o["id"] for o in O if seen[(o["concept_id"], o["bloom_level"])] > 1]
    rule("V16", not dups,
         "no two outcomes may test the same concept at the same Bloom level", sorted(set(dups)))

    failed = [k for k, v in rep.items() if not v["pass"]]
    shallow_prereqs = sorted({nm for o in O if o["bloom_level"] in apply_like
                              for nm in (o.get("prerequisite_coverage") or {}).get("shallow", [])})
    log = {"node": "validate", "attempt": state.get("retry_count", 0), "failed": failed,
           "shallow_prereqs": shallow_prereqs}
    snapshot = {"attempt": state.get("retry_count", 0), "failed": failed,
                "shallow_prereqs": shallow_prereqs,
                "rules": [{"code": k, "pass": v["pass"], "detail": v["detail"],
                           "failing": v["failing"][:12]} for k, v in rep.items()]}
    prog.done("validate", detail="pass" if not failed else f"failing: {failed}", snapshot=snapshot)
    return {"validation_report": rep, "log": [log]}
