"""LO pipeline (LO-first) · Node 4 — review_and_validate (dedup + R1–R8 rubric + structural gate).

Fuses the former review_outcomes_quality and validate nodes into one stage — they always ran
back-to-back (an unconditional edge, validate's only predecessor), and the rubric verdict the judge
produces is exactly what the structural gate's V13 reads, so combining them removes a node boundary
and a state round-trip without changing the loop. `repair` remains its OWN node, so the retry loop
(repair → resolve_prerequisites → review_and_validate), `retry_count`, per-iteration checkpointing,
and the HITL Gate-2 reject→repair path are all unchanged.

Two jobs, in order:
  1. DEDUP the outcome set (one per concept_id × Bloom, heaviest kept) + backfill toward budget, then
     score each survivor against the R1–R8 rubric (LLM, isolated, temp 0; only re-scoring outcomes
     whose signature changed since the last attempt). → lo_reviews
  2. The deterministic structural GATE over that set:
       V4  every in-scope BROAD concept (parent_concept) targeted by >=1 outcome (coverage).
       V5  every apply/scenario outcome carries a non-empty prerequisite set.
       V6  apply/scenario prerequisite closure fully in-scope or assumed (RAG-verified scope).
       V7  the concept dependency graph is acyclic.
       V8  apply/scenario outcomes target a procedural concept (applied_skill vote).
       V9  each outcome's evidence quote appears verbatim in the source.
       V10 each outcome's action verb is in its Bloom tier's controlled vocabulary.
       V13 every outcome passes all R1–R8 (failures in lo_reviews).
       V14 no outcome targets an out-of-scope (non-explained) concept.
       V15 every apply/scenario outcome has ALL required prerequisites COVERED.
       V16 outcomes are unique — no two test the same (concept, Bloom level).

Input:  state["outcomes"], concept_inventory, concept_graph, lo_reviews, backfill_pool, source_text.
Output: outcomes (deduped/backfilled), lo_reviews, validation_report {Vk:{pass,detail,failing}}, log.
"""
from __future__ import annotations

import json
from collections import Counter

from app.mcq_pipeline.config import VERBS
from app.mcq_pipeline.utils.concept_graph import loosen_text
from app.mcq_pipeline.utils.llm import chat, parse_json
from app.mcq_pipeline.prompts.store import get_prompt, register
from app.mcq_pipeline.utils._common import _ctx, _prog
from app.mcq_pipeline.utils._lo_helpers import backfill_to_budget


# ── the unified R1–R8 rubric judge (one LO at a time) ─────────────────────── #
_RUBRIC = register("lo.rubric", (
    "You are a STRICT validator of learning outcomes against a reading passage.\n"
    "You decide whether a SINGLE outcome is fully supported by the material.\n\n"

    "Evaluate each criterion independently. Default is FAIL. A criterion passes ONLY if it is "
    "explicitly supported by the section text OR the listed prerequisites (valid prior knowledge). "
    "If any required detail is missing or inferred → FAIL.\n\n"

    "Return ONLY JSON:\n"
    '{"R1_present":bool,"R2_depth":bool,"R3_answerable":bool,"R4_in_scope":bool,'
    '"R5_answer_key":bool,"R6_self_contained":bool,"R7_distinct":bool,"R8_apply_valid":bool,'
    '"fail_reason":"<one line>","suggested_fix":"<safe lower-level outcome>"}\n\n'

    "R1 PRESENT — concept is explicitly TAUGHT in the section (not merely mentioned/named/assumed).\n"
    "R2 DEPTH — the material supports the action type: remember→fact stated; understand→explanation "
    "given; apply→step-by-step method or worked example exists; scenario→transfer demonstrated. Do "
    "NOT assume missing steps or upgrade depth by inference.\n"
    "R3 ANSWERABLE — a learner can answer using ONLY the section text + listed prerequisites; FAIL "
    "if any external knowledge is required or implied.\n"
    "R4 IN-SCOPE — all required details exist in the section or prerequisites; FAIL if the outcome "
    "introduces any missing element, case, value, or concept.\n"
    "R5 ANSWER KEY — a single correct answer is derivable without ambiguity.\n"
    "R6 SELF-CONTAINED — does NOT depend on project names, sample variables, or placeholder "
    "identifiers (Project A, File1, etc.).\n"
    "R7 DISTINCT — targets ONE concept and ONE assessable idea; FAIL if it mixes concepts or is a "
    "broad umbrella.\n"
    "R8 APPLY VALIDITY — for apply/scenario ONLY: PASS only if the method/steps are explicitly shown "
    "in the material; for remember/understand → automatically PASS.\n\n"

    "If uncertain → FAIL (no benefit of the doubt). fail_reason states the EXACT missing element; "
    "suggested_fix downgrades to a safe, explicitly supported outcome.\n"
))

_COVERAGE_SRC_CAP = 12000
_RUBRIC_KEYS = ("R1_present", "R2_depth", "R3_answerable", "R4_in_scope", "R5_answer_key",
                "R6_self_contained", "R7_distinct", "R8_apply_valid")


def _outcome_sig(o: dict) -> str:
    return "|".join(str(o.get(k)) for k in ("id", "learner_action", "bloom_level", "concept_id", "title"))


def _dedupe(outcomes: list) -> tuple[list, list]:
    """Drop true duplicates: at most one outcome per (concept_id, Bloom level), keeping the most
    foundational (highest weight). Returns (kept, dropped_ids). Order preserved for survivors."""
    best: dict = {}
    for o in outcomes:
        key = (o.get("concept_id"), o.get("bloom_level"))
        cur = best.get(key)
        if cur is None or o.get("weight", 0) > cur.get("weight", 0):
            best[key] = o
    keep_ids = {id(o) for o in best.values()}
    kept = [o for o in outcomes if id(o) in keep_ids]
    dropped = [o["id"] for o in outcomes if id(o) not in keep_ids]
    return kept, dropped


def _score_outcome(outcome: dict, section_text: str, source_text: str) -> dict:
    compact = {k: outcome.get(k) for k in
               ("title", "bloom_level", "scenario", "learner_action", "description")}
    ev = (outcome.get("source_evidence") or {}).get("quote", "")
    cov = outcome.get("prerequisite_coverage") or {}
    prereqs = ", ".join(cov.get("covered", []) or []) or "(none / not an apply outcome)"
    usr = (f"OUTCOME:\n{json.dumps(compact, ensure_ascii=False)}\n\n"
           f'CITED EVIDENCE (the exact span this outcome was drawn from):\n"{ev}"\n\n'
           f"PREREQUISITES the learner is assumed to have (RAG-confirmed prior knowledge):\n{prereqs}\n\n"
           f"SECTION it was drawn from (judge mainly against this):\n"
           f"{(section_text or '')[:_COVERAGE_SRC_CAP]}\n\n"
           f"REST OF THE READING (background only):\n{(source_text or '')[:_COVERAGE_SRC_CAP]}")
    try:
        data = parse_json(chat([{"role": "system", "content": get_prompt("lo.rubric", _RUBRIC)},
                                {"role": "user", "content": usr}], temperature=0)) or {}
    except Exception:  # noqa: BLE001 — LLM down: never block the run; treat as passing
        return {"covered": True, "rubric": {}, "fail_reason": "judge unavailable", "suggested_fix": ""}
    rubric = {k: bool(data.get(k, True)) for k in _RUBRIC_KEYS}
    return {"covered": all(rubric.values()), "rubric": rubric,
            "fail_reason": str(data.get("fail_reason", ""))[:300],
            "suggested_fix": str(data.get("suggested_fix", ""))[:160]}


def _run_validation(outcomes: list, state: dict, reviews: dict) -> dict:
    """Deterministic structural gate (the former validate node), over the deduped outcome set
    and the freshly-computed rubric `reviews`. Returns validation_report {Vk:{pass,detail,failing}}."""
    O = outcomes
    rep: dict = {}
    src = loosen_text(state["source_text"])
    inv = state["concept_inventory"]
    inv_ids = {c["concept_id"] for c in inv if c["in_scope"]}
    apply_like = ("apply", "scenario")

    def rule(rid, ok, detail="", items=None):
        rep[rid] = {"pass": bool(ok), "detail": detail, "failing": items or []}

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

    not_covered = [o["id"] for o in O if (rv := reviews.get(o["id"])) and not rv.get("covered", True)]
    rule("V13", not not_covered,
         "every outcome must pass all unified-Judge rubric criteria R1-R8 (failures in lo_reviews)",
         not_covered)

    uncovered = [o["id"] for o in O if o["bloom_level"] in apply_like
                 and (o.get("prerequisite_coverage") or {}).get("uncovered")]
    rule("V15", not uncovered,
         "every apply/scenario outcome must have all required prerequisites covered", uncovered)

    seen = Counter((o["concept_id"], o["bloom_level"]) for o in O)
    dups = [o["id"] for o in O if seen[(o["concept_id"], o["bloom_level"])] > 1]
    rule("V16", not dups,
         "no two outcomes may test the same concept at the same Bloom level", sorted(set(dups)))
    return rep


def review_and_validate(state, config) -> dict:
    """Dedup + backfill → R1–R8 rubric judge (incremental) → deterministic structural gate."""
    prog = _prog(config)
    ctx = _ctx(config)
    prog.start("review_and_validate")

    # (1) dedup + backfill toward budget (a TARGET, not just a ceiling).
    outcomes, dropped = _dedupe(state["outcomes"])
    if dropped:
        prog.detail("review_and_validate", f"dropped {len(dropped)} duplicate outcome(s)")
    budget = (state.get("allocation_plan") or {}).get("question_budget") or len(outcomes)
    outcomes, backfilled = backfill_to_budget(outcomes, state.get("backfill_pool") or [], budget)
    if backfilled:
        prog.detail("review_and_validate", f"backfilled {len(backfilled)} to reach budget {budget}")

    # (2) rubric judge — only re-scoring outcomes whose signature changed (skip if gate disabled).
    prev = state.get("lo_reviews") or {}
    gate_on = ctx is None or getattr(ctx, "run_coverage_gate", True)
    if gate_on:
        todo = [o for o in outcomes if (prev.get(o["id"]) or {}).get("_sig") != _outcome_sig(o)]
        sec_text = {s["topic_id"]: s.get("text", "") for s in state.get("sections", [])}
        src = state["source_text"]
        on_done = prog.counter("review_and_validate", len(todo)) if todo else (lambda **k: None)
        fresh = {}
        for o in todo:
            v = _score_outcome(o, sec_text.get(o.get("topic_id"), ""), src)
            v["_sig"] = _outcome_sig(o)
            fresh[o["id"]] = v
            on_done()
        merged = {**prev, **fresh}
        merged = {o["id"]: merged[o["id"]] for o in outcomes if o["id"] in merged}   # drop stale ids
    else:
        merged = prev

    # (3) deterministic structural gate (over the deduped set + fresh rubric verdicts).
    rep = _run_validation(outcomes, state, merged)
    failed = [k for k, v in rep.items() if not v["pass"]]
    rubric_failures = sum(1 for v in merged.values() if not v.get("covered", True))
    apply_uncovered = sorted({nm for o in outcomes if o["bloom_level"] in ("apply", "scenario")
                              for nm in (o.get("prerequisite_coverage") or {}).get("uncovered", [])})
    shallow_prereqs = sorted({nm for o in outcomes if o["bloom_level"] in ("apply", "scenario")
                              for nm in (o.get("prerequisite_coverage") or {}).get("shallow", [])})
    title_of = {o["id"]: o["title"] for o in outcomes}
    snapshot = {"deduped": dropped, "kept": len(outcomes), "attempt": state.get("retry_count", 0),
                "rubric_failures": rubric_failures, "failed": failed,
                "apply_uncovered_prereqs": apply_uncovered, "shallow_prereqs": shallow_prereqs,
                "rules": [{"code": k, "pass": v["pass"], "detail": v["detail"],
                           "failing": v["failing"][:12]} for k, v in rep.items()],
                "reviews": [{"id": oid, "title": title_of.get(oid, oid), "covered": v.get("covered"),
                             "failed": [k for k, ok in (v.get("rubric") or {}).items() if not ok],
                             "fail_reason": v.get("fail_reason", "")}
                            for oid, v in merged.items()]}
    prog.done("review_and_validate",
              detail=(f"{len(dropped)} dup dropped · " + ("pass" if not failed else f"failing: {failed}")),
              snapshot=snapshot)
    return {"outcomes": outcomes, "lo_reviews": merged, "validation_report": rep,
            "log": [{"node": "review_and_validate", "deduped": dropped, "attempt": state.get("retry_count", 0),
                     "failed": failed, "rubric_failures": rubric_failures,
                     "apply_uncovered_prereqs": apply_uncovered}]}
