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
    "You are a STRICT validator of learning outcomes against a reading passage.\n"
    "You decide whether a SINGLE outcome is fully supported by the material.\n\n"

    "You must evaluate each criterion independently. Default is FAIL.\n\n"

    "Return ONLY JSON:\n"
    '{"R1_present":bool,"R2_depth":bool,"R3_answerable":bool,"R4_in_scope":bool,'
    '"R5_answer_key":bool,"R6_self_contained":bool,"R7_distinct":bool,"R8_apply_valid":bool,'
    '"fail_reason":"<one line>","suggested_fix":"<safe lower-level outcome>"}\n\n'

    "----------------------------\n"
    "CRITICAL REASONING RULE\n"
    "----------------------------\n"
    "A criterion passes ONLY if it is explicitly supported by:\n"
    "- the section text OR\n"
    "- the listed prerequisites (as valid prior knowledge)\n\n"

    "If any required detail is missing or inferred → FAIL.\n\n"

    "----------------------------\n"
    "R1 PRESENT (TEACHING CHECK)\n"
    "----------------------------\n"
    "PASS only if the concept is explicitly taught in the section.\n"
    "FAIL if it is only mentioned, named, or assumed.\n\n"

    "----------------------------\n"
    "R2 DEPTH MATCH (VERB ALIGNMENT)\n"
    "----------------------------\n"
    "PASS only if the material supports the ACTION TYPE:\n"
    "- remember → fact explicitly stated\n"
    "- understand → explanation or reasoning is explicitly given\n"
    "- apply → step-by-step method OR worked example exists\n"
    "- scenario → transfer is explicitly demonstrated\n\n"

    "IMPORTANT:\n"
    "- Do NOT assume missing steps\n"
    "- Do NOT upgrade depth from inference\n\n"

    "----------------------------\n"
    "R3 ANSWERABLE (NO OUTSIDE KNOWLEDGE)\n"
    "----------------------------\n"
    "PASS only if a learner can answer using ONLY:\n"
    "- section text\n"
    "- listed prerequisites\n\n"

    "FAIL if ANY external knowledge is required or implied.\n\n"

    "----------------------------\n"
    "R4 IN-SCOPE (NO BEYOND MATERIAL)\n"
    "----------------------------\n"
    "PASS only if ALL required details exist in:\n"
    "- section OR prerequisites\n\n"

    "FAIL if outcome introduces ANY missing element, case, value, or concept.\n\n"

    "IMPORTANT DISTINCTION:\n"
    "- R3 = can the learner answer?\n"
    "- R4 = does the outcome stay within taught boundaries?\n\n"

    "----------------------------\n"
    "R5 ANSWER KEY DERIVABLE\n"
    "----------------------------\n"
    "PASS only if a single correct answer can be derived without ambiguity.\n"
    "FAIL if multiple valid interpretations exist.\n\n"

    "----------------------------\n"
    "R6 SELF-CONTAINED\n"
    "----------------------------\n"
    "PASS only if outcome does NOT depend on:\n"
    "- project names\n"
    "- sample variables\n"
    "- placeholder identifiers (Project A, File1, etc.)\n\n"

    "----------------------------\n"
    "R7 DISTINCT\n"
    "----------------------------\n"
    "PASS only if outcome targets ONE concept and ONE assessable idea.\n"
    "FAIL if it mixes multiple concepts or broad umbrellas.\n\n"

    "----------------------------\n"
    "R8 APPLY VALIDITY\n"
    "----------------------------\n"
    "For apply/scenario outcomes ONLY:\n"
    "- PASS only if method or steps are explicitly shown in material\n"
    "For remember/understand → automatically PASS.\n\n"

    "----------------------------\n"
    "STRICT DEFAULT RULE\n"
    "----------------------------\n"
    "If uncertain → FAIL.\n"
    "Do NOT give benefit of doubt.\n\n"

    "----------------------------\n"
    "FAILURE OUTPUT RULE\n"
    "----------------------------\n"
    "- fail_reason must state EXACT missing element\n"
    "- suggested_fix must downgrade to a safe, explicitly supported outcome\n\n"
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


