"""LO pipeline · Node 8 — validate (structural invariants + composite rubric gate)."""
from __future__ import annotations

from app.mcq_pipeline.config import QUESTION_BUDGET, TIER_ORDER, VERBS
from app.mcq_pipeline.utils.concept_graph import loosen_text
from app.mcq_pipeline.nodes._common import _prog


# ── Node 8 · validate (D · structural invariants V1–V10/V14 + composite rubric gate V13) ─ #
def _loosen(t: str) -> str:
    return loosen_text(t)


def validate(state, config) -> dict:
    prog = _prog(config)
    prog.start("validate")
    O = state["outcomes"]
    plan = state["allocation_plan"]
    rep: dict = {}
    src = _loosen(state["source_text"])
    inv = state["concept_inventory"]
    inv_ids = {c["concept_id"] for c in inv if c["in_scope"]}
    budget = plan.get("question_budget", QUESTION_BUDGET)
    apply_like = ("apply", "scenario")

    def rule(rid, ok, detail="", items=None):
        rep[rid] = {"pass": bool(ok), "detail": detail, "failing": items or []}

    # --- structural invariants (domain-agnostic) --------------------------------- #
    rule("V1", len(O) == budget, f"count={len(O)} (want {budget})")
    want = {k: plan.get("tier_counts", {}).get(k, 0) for k in TIER_ORDER}
    got = {k: sum(o["bloom_level"] == k for o in O) for k in TIER_ORDER}
    rule("V2", got == want, f"got={got} want={want}")
    bad = []
    for tid, p in plan["by_topic"].items():
        cnt = sum(o["topic_id"] == tid for o in O)
        if cnt != p["slots"]:
            bad.append({"topic": tid, "got": cnt, "want": p["slots"]})
    rule("V3", not bad, "per-topic slot mismatch", bad)
    covered = {o["concept_id"] for o in O}
    rule("V4", not (inv_ids - covered), "uncovered in-scope concepts", sorted(inv_ids - covered))
    off_scope = [o["id"] for o in O if o["concept_id"] not in inv_ids]
    rule("V14", not off_scope, "outcome targets a non-explained (out-of-scope) concept", off_scope)
    no_pre = [o["id"] for o in O if o["bloom_level"] in apply_like and not o["prerequisites"]]
    rule("V5", not no_pre, "apply/scenario outcome with empty prerequisite set", no_pre)
    oos = [o["id"] for o in O if o["bloom_level"] in apply_like
           and o["prerequisite_scope"] == "has_out_of_scope"]
    rule("V6", not oos, "apply/scenario prerequisite closure out of scope", oos)
    rule("V7", state["concept_graph"]["acyclic"], "DAG acyclicity")
    # V8 (de-coupled): an apply/scenario outcome must target a concept the LLM flagged procedural
    # (procedurality = the applied_skill vote, no regex). The verb itself is checked by V10.
    proc = {c["concept_id"]: bool(c.get("procedural")) for c in inv}
    fake = [o["id"] for o in O if o["bloom_level"] in apply_like and not proc.get(o["concept_id"], False)]
    rule("V8", not fake, "apply/scenario outcome on a non-procedural concept", fake)
    ungrounded = [o["id"] for o in O
                  if not o["source_evidence"]["quote"].strip()
                  or _loosen(o["source_evidence"]["quote"])[:60] not in src]
    rule("V9", not ungrounded, "source_evidence not found verbatim in source", ungrounded)
    badverb = [o["id"] for o in O if o["learner_action"] not in VERBS.get(o["bloom_level"], set())]
    rule("V10", not badverb, "action verb outside the tier's controlled vocabulary", badverb)

    # --- composite rubric gate (V13): every LO must pass every R1–R8 criterion --------- #
    # The unified Judge owns ALL quality judgments (depth, answerability, beyond-scope,
    # self-containment, distinctness, apply-validity). Absent verdicts (judge disabled / LLM
    # down) never flag. V11 (code-syntax grounding) and V12 (depth heuristic) were REMOVED —
    # both were domain-coupled and are now subsumed by R8 / R2 of the unified rubric.
    reviews = state.get("lo_reviews") or {}
    not_covered = [o["id"] for o in O if (rv := reviews.get(o["id"])) and not rv.get("covered", True)]
    rule("V13", not not_covered,
         "rubric gate: an outcome fails one or more of R1–R8 (see lo_reviews)", not_covered)

    failed = [k for k, v in rep.items() if not v["pass"]]
    log = {"node": "validate", "attempt": state.get("retry_count", 0), "failed": failed}
    prog.done("validate", detail="pass" if not failed else f"failing: {failed}")
    return {"validation_report": rep, "log": [log]}


