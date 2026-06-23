"""
app/mcq_pipeline/lo_graph.py
----------------------------
The LangGraph pipeline. The LO-creation stage is the deterministic 10-node flow
(`lo_nodes` + `lo_artifact.finalize`); a `lo_to_legacy` bridge maps its frozen
outcomes onto the legacy LearningOutcome shape, and the (unchanged) question
stage follows:

    START → parse_structure → extract_concepts → canonicalize_concepts
          → build_dependency_graph → profile_coverage → plan_allocation → author_outcomes
          → resolve_prerequisites → coverage_gate (strict coverage rubric) → validate ─cond─┐
                pass / retries-exhausted → finalize │  still-fixable → repair → resolve_prerequisites
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
from app.mcq_pipeline.nodes import author_outcomes, build_dependency_graph, canonicalize_concepts, extract_concepts, judge_outcomes, parse_structure, plan_allocation, profile_coverage, repair, resolve_prerequisites, sequence_outcomes, validate
from app.mcq_pipeline.state import LOState, run_ctx
from app.mcq_pipeline.nodes.n14_generate_questions import generate_for_los
from app.mcq_pipeline.nodes.n15_review_questions import review_and_fix_for_los
from app.mcq_pipeline.nodes.n13_recommend_question_type import recommend_for_los


# --- terminal LO node + legacy bridge -------------------------------------- #
def finalize(state, config) -> dict:
    ctx = run_ctx(config)
    ctx.progress.start("finalize")
    out = _finalize_artifact(state)
    art = out["artifact"]
    ctx.progress.done("finalize", detail=f"{art['status']} · {len(art['outcomes'])} LOs")
    return out


def lo_to_legacy(state, config) -> dict:
    ctx = run_ctx(config)
    ctx.progress.start("lo_to_legacy")
    final_los = build_final_los(state, ctx.db_prereq_units)
    ctx.progress.done("lo_to_legacy", detail=f"{len(final_los)} outcomes bridged")
    return {"final_los": final_los}


# --- question stage (adapted to RunContext; logic unchanged) --------------- #
def recommend_question_types(state, config) -> dict:
    ctx = run_ctx(config)
    scope.set_adapter(ctx.rag)
    los = state["final_los"]
    on_progress = ctx.progress.counter("recommend_question_types", len(los))
    los = recommend_for_los(los, max_seq=None, on_progress=on_progress)
    ctx.progress.done("recommend_question_types")
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
    ctx.progress.done("generate_questions")
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
    ctx.progress.done("review_questions", needs_human=counters["nh"])
    return {"questions": reviewed, "question_reviews": summaries, "notes": notes}


# --- human-in-the-loop gates (inert pass-through unless ctx.hitl_enabled) --- #
def review_division(state, config) -> dict:
    """HITL Gate 1 — pause for a human to approve/reject the Planner's LO division. Returns a
    pass-through {} (no interrupt) unless the run enabled HITL."""
    ctx = run_ctx(config)
    if not getattr(ctx, "hitl_enabled", False):
        return {}
    from langgraph.types import interrupt
    decision = interrupt({"gate": "division", "proposal": state.get("division_proposal", {})})
    decision = decision if isinstance(decision, dict) else {"action": "approve"}
    action, note = decision.get("action", "approve"), decision.get("note", "")
    return {"gate_decision": {"gate": "division", "action": action, "note": note},
            "notes": [f"Gate-1 {action}" + (f": {note}" if note else "")]}


def review_outcomes(state, config) -> dict:
    """HITL Gate 2 — pause for a human to approve/reject the final LOs by concept mapping. A per-LO
    reject marks those ids as rubric failures (with the human note) so the existing repair path
    regenerates exactly those, then re-judges + re-reviews. Inert pass-through unless HITL on."""
    ctx = run_ctx(config)
    if not getattr(ctx, "hitl_enabled", False):
        return {}
    from langgraph.types import interrupt
    decision = interrupt({"gate": "outcomes", "outcomes": state.get("outcomes", []),
                          "reviews": state.get("lo_reviews", {})})
    decision = decision if isinstance(decision, dict) else {"action": "approve"}
    action = decision.get("action", "approve")
    rejected = list(decision.get("rejected_ids", []))
    note = decision.get("note", "")
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
    """approve / inert → author ; reject → re-plan (Planner re-proposes; the note is recorded)."""
    d = state.get("gate_decision") or {}
    if d.get("gate") == "division" and d.get("action") == "reject":
        return "plan_allocation"
    return "author_outcomes"


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
    g.add_node("extract_concepts", extract_concepts)
    g.add_node("canonicalize_concepts", canonicalize_concepts)
    g.add_node("build_dependency_graph", build_dependency_graph)
    g.add_node("profile_coverage", profile_coverage)
    g.add_node("plan_allocation", plan_allocation)
    g.add_node("review_division", review_division)        # HITL Gate 1 (inert unless hitl_enabled)
    g.add_node("author_outcomes", author_outcomes)
    g.add_node("resolve_prerequisites", resolve_prerequisites)
    g.add_node("judge_outcomes", judge_outcomes)
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
    g.add_edge("parse_structure", "extract_concepts")
    g.add_edge("extract_concepts", "canonicalize_concepts")
    g.add_edge("canonicalize_concepts", "build_dependency_graph")
    g.add_edge("build_dependency_graph", "profile_coverage")
    g.add_edge("profile_coverage", "plan_allocation")
    g.add_edge("plan_allocation", "review_division")
    g.add_conditional_edges("review_division", route_after_division,
                            {"plan_allocation": "plan_allocation", "author_outcomes": "author_outcomes"})
    g.add_edge("author_outcomes", "resolve_prerequisites")
    g.add_edge("resolve_prerequisites", "judge_outcomes")
    g.add_edge("judge_outcomes", "validate")
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
