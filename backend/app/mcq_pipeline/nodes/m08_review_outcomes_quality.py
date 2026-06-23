"""LO pipeline (LO-first) · Node 8 — review_outcomes_quality (dedup + R1–R8 rubric).

Step 6 of the LO-first flow: "Review and freeze the outcomes." Two jobs:

  1. UNIQUENESS — no two kept outcomes may test the SAME thing. We treat a (concept_id, Bloom
     level) pair as "the same thing": within a pair we keep the most foundational outcome and drop
     the rest. Outcomes that test a genuine sub-step (a different concept_id) or a different Bloom
     level survive — those are distinct, not duplicates. (Conservative: drop only true duplicates.)

  2. RUBRIC — score every surviving outcome against the unified R1–R8 rubric (`lo.rubric`): is it
     fully supported by the section + its listed prerequisites? The verdicts feed `validate` as the
     composite rubric gate (V13). Cached by signature so the repair loop only re-scores what
     changed; degrades to "covered" if the LLM is unavailable so it never blocks a run.

The explicit apply-prerequisite-coverage gate lives in `validate` (V6 + V15), which can route an
under-covered apply outcome to repair; here we also surface an apply-coverage summary for the gate.

Input:  state["outcomes"] (with prerequisites resolved), state["lo_reviews"] (cache).
Output: outcomes (deduped), lo_reviews keyed by outcome id, logs.
"""
from __future__ import annotations

import json

from app.mcq_pipeline.config import TIER_ORDER
from app.mcq_pipeline.utils.llm import chat, parse_json
from app.mcq_pipeline.prompts.store import get_prompt, register
from app.mcq_pipeline.nodes._common import _ctx, _prog

_RANK = {t: i for i, t in enumerate(TIER_ORDER)}

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


def review_outcomes_quality(state, config) -> dict:
    """Dedup the outcome set, then score each survivor against R1–R8 (sequential, isolated; only
    re-scoring outcomes whose signature changed). Returns the deduped outcomes + lo_reviews."""
    prog = _prog(config)
    ctx = _ctx(config)
    prog.start("review_outcomes_quality")
    outcomes, dropped = _dedupe(state["outcomes"])
    if dropped:
        prog.detail("review_outcomes_quality", f"dropped {len(dropped)} duplicate outcome(s)")

    if ctx is not None and not getattr(ctx, "run_coverage_gate", True):
        prog.done("review_outcomes_quality", detail=f"dedup only ({len(dropped)} dropped)",
                  snapshot={"deduped": dropped, "judge": "skipped", "kept": len(outcomes)})
        return {"outcomes": outcomes, "log": [{"node": "review_outcomes_quality",
                                               "deduped": dropped, "judge": "skipped"}]}

    prev = state.get("lo_reviews") or {}
    todo = [o for o in outcomes if (prev.get(o["id"]) or {}).get("_sig") != _outcome_sig(o)]
    sec_text = {s["topic_id"]: s.get("text", "") for s in state.get("sections", [])}
    src = state["source_text"]
    on_done = prog.counter("review_outcomes_quality", len(todo)) if todo else (lambda **k: None)

    fresh = {}
    for o in todo:
        v = _score_outcome(o, sec_text.get(o.get("topic_id"), ""), src)
        v["_sig"] = _outcome_sig(o)
        fresh[o["id"]] = v
        on_done()

    merged = {**prev, **fresh}
    merged = {o["id"]: merged[o["id"]] for o in outcomes if o["id"] in merged}   # drop stale ids
    failed = sum(1 for v in merged.values() if not v.get("covered", True))
    # apply-prereq-coverage summary (the hard gate is V6 + V15 in validate).
    apply_uncovered = sorted({nm for o in outcomes if o["bloom_level"] in ("apply", "scenario")
                              for nm in (o.get("prerequisite_coverage") or {}).get("uncovered", [])})
    title_of = {o["id"]: o["title"] for o in outcomes}
    snapshot = {"deduped": dropped, "kept": len(outcomes), "rubric_failures": failed,
                "apply_uncovered_prereqs": apply_uncovered,
                "reviews": [{"id": oid, "title": title_of.get(oid, oid),
                             "covered": v.get("covered"),
                             "failed": [k for k, ok in (v.get("rubric") or {}).items() if not ok],
                             "fail_reason": v.get("fail_reason", "")}
                            for oid, v in merged.items()]}
    prog.done("review_outcomes_quality",
              detail=f"{len(dropped)} dup dropped · {failed}/{len(outcomes)} fail a criterion",
              snapshot=snapshot)
    return {"outcomes": outcomes, "lo_reviews": merged,
            "log": [{"node": "review_outcomes_quality", "deduped": dropped,
                     "rubric_failures": failed, "apply_uncovered_prereqs": apply_uncovered}]}
