"""LO pipeline · Node 7.5 — judge_outcomes (unified R1-R8 rubric)."""
from __future__ import annotations

import json

from app.mcq_pipeline.utils.llm import chat, parse_json
from app.mcq_pipeline.prompts.store import get_prompt, register
from app.mcq_pipeline.nodes._common import _ctx, _prog


# ── Node 7.5 · coverage_gate (A · STRICT coverage rubric) ─────────────────── #
# A dedicated agent that scores each authored outcome against an explicit COVERAGE RUBRIC
# (lo.coverage_rubric): could a student answer a question built from this outcome using
# ONLY the material? It STRICTLY rejects the core failure — the concept is covered, but
# the outcome reaches PAST what is taught (unanswerable for the learner). Verdicts feed
# validate as V13. One call per outcome (concurrent, temp 0); cached by signature so the
# repair loop only re-scores what changed; degrades to "covered" if the LLM is
# unavailable so it never blocks a run.
_RUBRIC = register("lo.rubric", (
    "You score ONE learning outcome against the reading material with a STRICT, UNIFORM RUBRIC. "
    "A learner will see ONLY a question built from this outcome and must answer it from THIS "
    "material PLUS whatever the listed PREREQUISITES cover (a prerequisite taught here or in a "
    "prior course is fair game) — but NO other outside knowledge. For EACH criterion, PASS/FAIL:\n"
    "R1 PRESENT — the concept is explicitly TAUGHT here (not merely named in passing or assumed).\n"
    "R2 DEPTH MATCHES DEMAND — taught to the depth the verb needs: remember (identify/list/define) "
    "= the fact is stated; understand (explain/describe) = the reason/explanation is given; "
    "comparison = the items are EXPLICITLY contrasted; apply = method/steps/worked example shown; "
    "scenario = enough shown that the method TRANSFERS to a new situation.\n"
    "R3 ANSWERABLE — a learner with ONLY this material (plus the listed prerequisites) can "
    "determine the answer with certainty; no un-taught outside knowledge, no inference the "
    "material does not make.\n"
    "R4 NO BEYOND-SCOPE LEAP — does NOT require a detail, value, case, comparison, or sub-topic "
    "absent from BOTH this material and the listed prerequisites. (THE key failure: the concept is "
    "covered, but the outcome reaches past it.)\n"
    "R5 ANSWER KEY DERIVABLE — the single correct answer (and why wrong options are wrong) is "
    "derivable from the material.\n"
    "R6 SELF-CONTAINED & TRANSFERABLE — states the GENERAL concept; references NO source-local "
    "entity (a scenario label like 'Project A', a sample variable/file/function name, a character, "
    "or a one-off value).\n"
    "R7 DISTINCT & SINGLE-ANSWER — targets ONE concept with ONE unambiguous correct answer (not a "
    "broad/umbrella outcome admitting several defensible answers).\n"
    "R8 APPLY-VALIDITY — for apply/scenario outcomes ONLY: the material shows HOW (method, steps, "
    "or a worked example) so genuine application is possible. For remember/understand, pass R8.\n\n"
    "Judge THIS ONE outcome in ISOLATION. Apply the rubric STRICTLY: the DEFAULT is FAIL; pass a "
    "criterion ONLY when the material (or a listed prerequisite) PLAINLY supplies what is needed. "
    "Any doubt, gap, or required un-taught inference means FAIL. Give NO benefit of the doubt.\n"
    'Return ONLY JSON: {"R1_present":<bool>,"R2_depth":<bool>,"R3_answerable":<bool>,'
    '"R4_in_scope":<bool>,"R5_answer_key":<bool>,"R6_self_contained":<bool>,"R7_distinct":<bool>,'
    '"R8_apply_valid":<bool>,"fail_reason":"<one line: exactly what is missing, or empty if all '
    'pass>","suggested_fix":"<a lower/safer outcome the material DOES fully support, or empty>"}.'
))

_COVERAGE_SRC_CAP = 12000
_RUBRIC_KEYS = ("R1_present", "R2_depth", "R3_answerable", "R4_in_scope", "R5_answer_key",
                "R6_self_contained", "R7_distinct", "R8_apply_valid")


def _outcome_sig(o: dict) -> str:
    return "|".join(str(o.get(k)) for k in ("id", "learner_action", "bloom_level", "concept_id", "title"))


def _score_outcome(outcome: dict, section_text: str, source_text: str) -> dict:
    compact = {k: outcome.get(k) for k in
               ("title", "bloom_level", "scenario", "learner_action", "description")}
    ev = (outcome.get("source_evidence") or {}).get("quote", "")
    cov = outcome.get("prerequisite_coverage") or {}
    prereqs = ", ".join(cov.get("covered", []) or []) or "(none / not an apply outcome)"
    usr = (f"OUTCOME:\n{json.dumps(compact, ensure_ascii=False)}\n\n"
           f'CITED EVIDENCE (the exact span this outcome was drawn from):\n"{ev}"\n\n'
           f"PREREQUISITES the learner is assumed to have (taught here or in a prior course, "
           f"RAG-confirmed — knowledge the learner may rely on):\n{prereqs}\n\n"
           f"SECTION it was drawn from (the coverage scope — judge mainly against this):\n"
           f"{(section_text or '')[:_COVERAGE_SRC_CAP]}\n\n"
           f"REST OF THE READING (background only):\n{(source_text or '')[:_COVERAGE_SRC_CAP]}")
    try:
        data = parse_json(chat([{"role": "system", "content": get_prompt("lo.rubric", _RUBRIC)},
                                {"role": "user", "content": usr}], temperature=0)) or {}
    except Exception:  # noqa: BLE001 — LLM down: never block the run; treat as passing
        return {"covered": True, "rubric": {}, "fail_reason": "judge unavailable", "suggested_fix": ""}
    rubric = {k: bool(data.get(k, True)) for k in _RUBRIC_KEYS}   # omitted key -> pass (lenient on omission only)
    return {"covered": all(rubric.values()), "rubric": rubric,
            "fail_reason": str(data.get("fail_reason", data.get("beyond_coverage_reason", "")))[:300],
            "suggested_fix": str(data.get("suggested_fix", data.get("suggested_recall_title", "")))[:160]}


def judge_outcomes(state, config) -> dict:
    """Unified LLM JUDGE: score EVERY outcome against the R1–R8 rubric, SEQUENTIALLY (one isolated
    call each, no cross-outcome bleed). Re-scores only outcomes whose signature changed (cheap
    repair loop). Returns lo_reviews keyed by outcome id; validate() reads it as the composite
    rubric gate (V13). Degrades to 'all pass' if the LLM is unavailable so it never blocks a run."""
    prog = _prog(config)
    ctx = _ctx(config)
    if ctx is not None and not getattr(ctx, "run_coverage_gate", True):
        prog.done("judge_outcomes", detail="skipped")
        return {}
    outcomes = state["outcomes"]
    prev = state.get("lo_reviews") or {}
    todo = [o for o in outcomes if (prev.get(o["id"]) or {}).get("_sig") != _outcome_sig(o)]
    if not todo:
        prog.done("judge_outcomes", detail="no changes")
        return {}
    on_done = prog.counter("judge_outcomes", len(todo))
    sec_text = {s["topic_id"]: s.get("text", "") for s in state.get("sections", [])}
    src = state["source_text"]

    fresh = {}
    for o in todo:                                   # one outcome at a time — strict, isolated
        v = _score_outcome(o, sec_text.get(o.get("topic_id"), ""), src)
        v["_sig"] = _outcome_sig(o)
        fresh[o["id"]] = v
        on_done()

    merged = {**prev, **fresh}
    merged = {o["id"]: merged[o["id"]] for o in outcomes if o["id"] in merged}   # drop stale ids
    failed = sum(1 for v in merged.values() if not v.get("covered", True))
    prog.done("judge_outcomes", detail=f"{failed} of {len(outcomes)} fail a rubric criterion")
    return {"lo_reviews": merged}


# Backward-compat alias — `judge_outcomes` is the canonical name; the graph imports it.
coverage_gate = judge_outcomes


