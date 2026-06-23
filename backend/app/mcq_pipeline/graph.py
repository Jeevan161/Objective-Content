"""
app/mcq_pipeline/lo_graph.py
----------------------------
The LangGraph pipeline. The LO-creation stage is the LO-first flow (`nodes` +
`artifact.finalize`); a `lo_to_legacy` bridge maps its frozen outcomes onto the
legacy LearningOutcome shape, and the (unchanged) question stage follows:

    START → parse_structure → generate_outcomes → map_concepts → build_outcome_graph
          → profile_depth → plan_outcomes → review_division (Gate 1)
          → resolve_prerequisites → review_outcomes_quality (dedup + R1–R8) → validate ─cond─┐
                pass / retries-exhausted → review_outcomes (Gate 2) │ still-fixable → repair ↺
          → finalize → lo_to_legacy → sequence_outcomes (deep-dive order)
          → recommend_question_types → generate_questions → review_questions → END

Run-scoped objects (RagAdapter, ProgressReporter) ride in a RunContext keyed by
thread_id (see `lo_state`), NOT in checkpointed state — so the graph is compiled
once with a durable Postgres checkpointer and reused across concurrent runs.
"""

from __future__ import annotations

import threading

from langgraph.graph import END, START, StateGraph

from app.mcq_pipeline.prompts import rules as lo_rules  # noqa: F401 — registers the lo.rules.* reference docs
from app.mcq_pipeline.utils import scope
from app.mcq_pipeline.artifact import build_final_los, finalize as _finalize_artifact
from app.mcq_pipeline.config import MAX_RETRIES
from app.mcq_pipeline.nodes import build_outcome_graph, generate_outcomes, map_concepts, parse_structure, plan_outcomes, profile_depth, repair, resolve_prerequisites, review_outcomes_quality, sequence_outcomes, validate
from app.mcq_pipeline.state import LOState, run_ctx
from app.mcq_pipeline.nodes._common import _prog
from app.mcq_pipeline.nodes.n14_generate_questions import generate_for_los
from app.mcq_pipeline.nodes.n15_review_questions import review_and_fix_for_los
from app.mcq_pipeline.nodes.n13_recommend_question_type import recommend_for_los


# --- terminal LO node + legacy bridge -------------------------------------- #
def finalize(state, config) -> dict:
    ctx = run_ctx(config)
    ctx.progress.start("finalize")
    out = _finalize_artifact(state)
    art = out["artifact"]
    snapshot = {"status": art.get("status"), "lo_count": len(art.get("outcomes", [])),
                "bloom_split": art.get("effective_bloom_split"), "spec_hash": art.get("spec_hash"),
                "validation_failed": [k for k, v in (art.get("validation_report") or {}).items()
                                      if not v.get("pass")],
                "escalation": (art.get("escalation") or {}).get("reason")}
    ctx.progress.done("finalize", detail=f"{art['status']} · {len(art['outcomes'])} LOs",
                      snapshot=snapshot)
    return out


def lo_to_legacy(state, config) -> dict:
    ctx = run_ctx(config)
    ctx.progress.start("lo_to_legacy")
    final_los = build_final_los(state, ctx.db_prereq_units)
    snapshot = {"final_los": len(final_los),
                "outcomes": [{"outcome": lo.get("outcome"), "bloom": lo.get("bloom_category"),
                              "concept": lo.get("concept")} for lo in final_los]}
    ctx.progress.done("lo_to_legacy", detail=f"{len(final_los)} outcomes bridged", snapshot=snapshot)
    return {"final_los": final_los}


# --- question stage (adapted to RunContext; logic unchanged) --------------- #
def recommend_question_types(state, config) -> dict:
    ctx = run_ctx(config)
    scope.set_adapter(ctx.rag)
    los = state["final_los"]
    on_progress = ctx.progress.counter("recommend_question_types", len(los))
    los = recommend_for_los(los, max_seq=None, on_progress=on_progress)
    from collections import Counter as _Counter
    types = _Counter(lo.get("question_type", "?") for lo in los)
    ctx.progress.done("recommend_question_types",
                      snapshot={"count": len(los), "by_type": dict(types),
                                "outcomes": [{"outcome": lo.get("outcome"),
                                              "question_type": lo.get("question_type")} for lo in los]})
    return {"final_los": los}


def generate_questions_node(state, config) -> dict:
    ctx = run_ctx(config)
    if not ctx.generate_questions:
        ctx.progress.done("generate_questions", detail="skipped")
        return {}
    scope.set_adapter(ctx.rag)
    los = state["final_los"]
    on_progress = ctx.progress.counter("generate_questions", len(los))
    questions = generate_for_los(los, max_seq=None, on_progress=on_progress)
    gen = [q for q in questions if q.get("status") == "generated"]
    ctx.progress.done("generate_questions",
                      snapshot={"total": len(questions), "generated": len(gen),
                                "skipped": len(questions) - len(gen)})
    return {"questions": questions}


def review_questions_node(state, config) -> dict:
    ctx = run_ctx(config)
    if not (ctx.generate_questions and ctx.review_questions):
        ctx.progress.done("review_questions", detail="skipped")
        return {}
    scope.set_adapter(ctx.rag)
    los, qs = state["final_los"], state.get("questions", [])
    total = len(qs)
    ctx.progress.tick("review_questions", 0, total, needs_human=0)
    lock = threading.Lock()
    counters = {"done": 0, "nh": 0}

    def on_progress(needs_human: bool = False):
        with lock:
            counters["done"] += 1
            if needs_human:
                counters["nh"] += 1
            d, nh = counters["done"], counters["nh"]
        ctx.progress.tick("review_questions", d, total, needs_human=nh)

    reviewed = review_and_fix_for_los(los, qs, max_seq=None, on_progress=on_progress)
    summaries = [{"outcome": r.get("outcome"), "question_type": r.get("question_type"),
                  "attempts": r.get("attempts", 0), "needs_human": r.get("needs_human", False),
                  "review": r.get("review")} for r in reviewed]
    notes = [f"reviewed {len(reviewed)} questions; "
             f"{sum(1 for s in summaries if s['needs_human'])} still need human review"]
    ctx.progress.done("review_questions", needs_human=counters["nh"],
                      snapshot={"reviewed": len(reviewed), "needs_human": counters["nh"],
                                "summaries": summaries})
    return {"questions": reviewed, "question_reviews": summaries, "notes": notes}


# --- human-in-the-loop gates (inert pass-through unless ctx.hitl_enabled) --- #
def review_division(state, config) -> dict:
    """HITL Gate 1 — pause for a human to approve/reject the Planner's LO division. Returns a
    pass-through {} (no interrupt) unless the run enabled HITL. Emits a progress/trace span so the
    gate is VISIBLE: it lights up 'running' (awaiting human) while paused, then 'done' with the
    decision on resume — instead of the board/trace appearing frozen at plan_allocation and then
    jumping to the next node."""
    ctx = run_ctx(config)
    if not getattr(ctx, "hitl_enabled", False):
        return {}
    prog = _prog(config)
    prog.start("review_division", detail="awaiting human review")
    from langgraph.types import interrupt
    decision = interrupt({"gate": "division", "proposal": state.get("division_proposal", {})})
    decision = decision if isinstance(decision, dict) else {"action": "approve"}
    action, note = decision.get("action", "approve"), decision.get("note", "")
    prog.done("review_division", detail=f"human {action}" + (f": {note[:60]}" if note else ""))
    return {"gate_decision": {"gate": "division", "action": action, "note": note},
            "notes": [f"Gate-1 {action}" + (f": {note}" if note else "")]}


def review_outcomes(state, config) -> dict:
    """HITL Gate 2 — pause for a human to approve/reject the final LOs by concept mapping. A per-LO
    reject marks those ids as rubric failures (with the human note) so the existing repair path
    regenerates exactly those, then re-judges + re-reviews. Inert pass-through unless HITL on.
    Emits a progress/trace span so the gate and the human decision are VISIBLE (the board lights up
    'running' while awaiting review, then 'done' on resume) rather than the run appearing frozen."""
    ctx = run_ctx(config)
    if not getattr(ctx, "hitl_enabled", False):
        return {}
    prog = _prog(config)
    prog.start("review_outcomes", detail="awaiting human review")
    from langgraph.types import interrupt
    decision = interrupt({"gate": "outcomes", "outcomes": state.get("outcomes", []),
                          "reviews": state.get("lo_reviews", {})})
    decision = decision if isinstance(decision, dict) else {"action": "approve"}
    action = decision.get("action", "approve")
    rejected = list(decision.get("rejected_ids", []))
    note = decision.get("note", "")
    prog.done("review_outcomes",
              detail=f"human {action}" + (f", {len(rejected)} to regenerate" if rejected else ""))
    out = {"gate_decision": {"gate": "outcomes", "action": action,
                             "rejected_ids": rejected, "note": note},
           "notes": [f"Gate-2 {action}" + (f" ({len(rejected)} rejected)" if rejected else "")]}
    if action == "reject" and rejected:
        vr = dict(state.get("validation_report") or {})
        prev = (vr.get("V13") or {}).get("failing", [])
        vr["V13"] = {"pass": False, "detail": "human rejected at Gate 2",
                     "failing": sorted(set(prev) | set(rejected))}
        reviews = dict(state.get("lo_reviews") or {})
        for rid in rejected:
            rv = dict(reviews.get(rid) or {})
            rv.update({"covered": False, "_sig": None,
                       "fail_reason": f"human review: {note}" if note else "human rejected this outcome"})
            reviews[rid] = rv
        out["validation_report"] = vr
        out["lo_reviews"] = reviews
    return out


# --- conditional routing --------------------------------------------------- #
def route_after_validate(state) -> str:
    """pass OR retries-exhausted → Gate 2 ; still-fixable failures → repair."""
    failed = [k for k, v in state["validation_report"].items() if not v["pass"]]
    if not failed:
        return "finalize"
    if state.get("retry_count", 0) >= MAX_RETRIES:
        return "finalize"
    return "repair"


def route_after_division(state) -> str:
    """approve / inert → resolve prerequisites ; reject → re-plan (re-selects; the note is recorded)."""
    d = state.get("gate_decision") or {}
    if d.get("gate") == "division" and d.get("action") == "reject":
        return "plan_outcomes"
    return "resolve_prerequisites"


def route_after_outcomes(state) -> str:
    """approve / inert → finalize ; per-LO reject → repair (regenerate the rejected LOs)."""
    d = state.get("gate_decision") or {}
    if d.get("gate") == "outcomes" and d.get("action") == "reject" and d.get("rejected_ids"):
        return "repair"
    return "finalize"


# --- checkpointer (durable, shared) ---------------------------------------- #
_CHECKPOINTER = None
_CP_LOCK = threading.Lock()


def _build_checkpointer():
    """Singleton checkpointer. Postgres (durable/resumable) when configured and
    reachable; otherwise an in-memory saver. Pooled connections (NOT a single shared
    connection) so concurrent runs are safe."""
    from app.core.config import settings

    backend = (settings.mcq_checkpointer or "memory").lower()
    if backend == "postgres":
        try:
            from langgraph.checkpoint.postgres import PostgresSaver
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool

            conninfo = settings.database_url.replace("+psycopg2", "").replace("+psycopg", "")
            pool = ConnectionPool(
                conninfo, min_size=0, max_size=max(4, settings.db_pool_size // 2),
                open=True, timeout=5,   # lazy + fail-fast so a down DB doesn't hang startup
                kwargs={"autocommit": True, "prepare_threshold": 0,
                        "connect_timeout": 5, "row_factory": dict_row},
            )
            saver = PostgresSaver(pool)
            saver.setup()   # idempotent: creates checkpoint tables if absent
            return saver
        except Exception as err:  # noqa: BLE001 — fall back rather than break the run
            import logging
            logging.getLogger(__name__).warning(
                "Postgres checkpointer unavailable (%s); using in-memory saver.", err)
    from langgraph.checkpoint.memory import InMemorySaver
    return InMemorySaver()


def get_checkpointer():
    global _CHECKPOINTER
    if _CHECKPOINTER is None:
        with _CP_LOCK:
            if _CHECKPOINTER is None:
                _CHECKPOINTER = _build_checkpointer()
    return _CHECKPOINTER


# --- graph builder + compiled singleton ------------------------------------ #
def build_lo_graph(*, checkpointer=None):
    g = StateGraph(LOState)
    g.add_node("parse_structure", parse_structure)
    g.add_node("generate_outcomes", generate_outcomes)
    g.add_node("map_concepts", map_concepts)
    g.add_node("build_outcome_graph", build_outcome_graph)
    g.add_node("profile_depth", profile_depth)
    g.add_node("plan_outcomes", plan_outcomes)
    g.add_node("review_division", review_division)        # HITL Gate 1 (inert unless hitl_enabled)
    g.add_node("resolve_prerequisites", resolve_prerequisites)
    g.add_node("review_outcomes_quality", review_outcomes_quality)
    g.add_node("validate", validate)
    g.add_node("repair", repair)
    g.add_node("review_outcomes", review_outcomes)        # HITL Gate 2 (inert unless hitl_enabled)
    g.add_node("finalize", finalize)
    g.add_node("lo_to_legacy", lo_to_legacy)
    g.add_node("sequence_outcomes", sequence_outcomes)
    g.add_node("recommend_question_types", recommend_question_types)
    g.add_node("generate_questions", generate_questions_node)
    g.add_node("review_questions", review_questions_node)

    g.add_edge(START, "parse_structure")
    g.add_edge("parse_structure", "generate_outcomes")
    g.add_edge("generate_outcomes", "map_concepts")
    g.add_edge("map_concepts", "build_outcome_graph")
    g.add_edge("build_outcome_graph", "profile_depth")
    g.add_edge("profile_depth", "plan_outcomes")
    g.add_edge("plan_outcomes", "review_division")
    g.add_conditional_edges("review_division", route_after_division,
                            {"plan_outcomes": "plan_outcomes", "resolve_prerequisites": "resolve_prerequisites"})
    g.add_edge("resolve_prerequisites", "review_outcomes_quality")
    g.add_edge("review_outcomes_quality", "validate")
    g.add_conditional_edges("validate", route_after_validate,
                            {"repair": "repair", "finalize": "review_outcomes"})
    g.add_conditional_edges("review_outcomes", route_after_outcomes,
                            {"repair": "repair", "finalize": "finalize"})
    g.add_edge("repair", "resolve_prerequisites")
    g.add_edge("finalize", "lo_to_legacy")
    g.add_edge("lo_to_legacy", "sequence_outcomes")
    g.add_edge("sequence_outcomes", "recommend_question_types")
    g.add_edge("recommend_question_types", "generate_questions")
    g.add_edge("generate_questions", "review_questions")
    g.add_edge("review_questions", END)
    return g.compile(checkpointer=checkpointer)


_GRAPH = None
_GRAPH_LOCK = threading.Lock()


def get_lo_graph():
    """Compiled-once, reused graph with the durable checkpointer."""
    global _GRAPH
    if _GRAPH is None:
        with _GRAPH_LOCK:
            if _GRAPH is None:
                _GRAPH = build_lo_graph(checkpointer=get_checkpointer())
    return _GRAPH
