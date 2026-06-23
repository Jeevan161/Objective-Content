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

    # --- structural invariants (domain-agnostic; each detail states what the rule ASSERTS) ----- #
    rule("V1", len(O) == budget,
         f"outcome count must equal the planned budget — got {len(O)}, want {budget}")
    want = {k: plan.get("tier_counts", {}).get(k, 0) for k in TIER_ORDER}
    got = {k: sum(o["bloom_level"] == k for o in O) for k in TIER_ORDER}
    rule("V2", got == want,
         f"realized Bloom-tier split must match the plan — got {got}, want {want}")
    bad = []
    for tid, p in plan["by_topic"].items():
        cnt = sum(o["topic_id"] == tid for o in O)
        if cnt != p["slots"]:
            bad.append({"topic": tid, "got": cnt, "want": p["slots"]})
    rule("V3", not bad, "each topic's outcome count must equal its planned slot count", bad)
    covered = {o["concept_id"] for o in O}
    rule("V4", not (inv_ids - covered),
         "every in-scope concept must be targeted by at least one outcome (uncovered listed)",
         sorted(inv_ids - covered))
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
    # V8 (de-coupled): an apply/scenario outcome must target a concept the LLM flagged procedural
    # (procedurality = the applied_skill vote, no regex). The verb itself is checked by V10.
    proc = {c["concept_id"]: bool(c.get("procedural")) for c in inv}
    fake = [o["id"] for o in O if o["bloom_level"] in apply_like and not proc.get(o["concept_id"], False)]
    rule("V8", not fake,
         "apply/scenario outcomes must target a procedural concept (applied_skill vote)", fake)
    ungrounded = [o["id"] for o in O
                  if not (o.get("source_evidence") or {}).get("quote", "").strip()
                  or _loosen((o.get("source_evidence") or {}).get("quote", ""))[:60] not in src]
    rule("V9", not ungrounded,
         "each outcome's evidence quote must appear verbatim in the source text", ungrounded)
    badverb = [o["id"] for o in O if o["learner_action"] not in VERBS.get(o["bloom_level"], set())]
    rule("V10", not badverb,
         "each outcome's action verb must be in its Bloom tier's controlled vocabulary", badverb)

    # --- composite rubric gate (V13): every LO must pass every R1–R8 criterion --------- #
    # The unified Judge owns ALL quality judgments (depth, answerability, beyond-scope,
    # self-containment, distinctness, apply-validity). Absent verdicts (judge disabled / LLM
    # down) never flag. V11 (code-syntax grounding) and V12 (depth heuristic) were REMOVED —
    # both were domain-coupled and are now subsumed by R8 / R2 of the unified rubric.
    reviews = state.get("lo_reviews") or {}
    not_covered = [o["id"] for o in O if (rv := reviews.get(o["id"])) and not rv.get("covered", True)]
    rule("V13", not not_covered,
         "every outcome must pass all unified-Judge rubric criteria R1-R8 (failures in lo_reviews)",
         not_covered)

    failed = [k for k, v in rep.items() if not v["pass"]]
    # Advisory (non-blocking): apply/scenario prerequisites that are PRESENT but taught only
    # shallowly (Node 7 depth grading). Surfaced for Gate-1 visibility; deliberately NOT a rule, so
    # it never enters `failed` and never routes to repair — prior-course depth is not author-repairable.
    shallow_prereqs = sorted({nm for o in O if o["bloom_level"] in apply_like
                              for nm in (o.get("prerequisite_coverage") or {}).get("shallow", [])})
    log = {"node": "validate", "attempt": state.get("retry_count", 0), "failed": failed,
           "shallow_prereqs": shallow_prereqs}
    prog.done("validate", detail="pass" if not failed else f"failing: {failed}")
    return {"validation_report": rep, "log": [log]}


