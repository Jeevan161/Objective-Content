"""
app/mcq_pipeline/lo_graph.py
----------------------------
The LangGraph pipeline. The LO-creation stage is the LO-first flow (`nodes` +
`artifact.finalize`); a `lo_to_legacy` bridge maps its frozen outcomes onto the
legacy LearningOutcome shape, and the (unchanged) question stage follows:

    START → parse_structure → author_outcomes → consolidate_concepts → graph_outcomes
          → select_outcomes  (the plan_los sub-graph: author → merge+depth → DAG → budget-select)
          → resolve_prerequisites → review_and_validate (dedup + R1–R8 rubric + structural gate) ─cond─┐
                pass / retries-exhausted → finalize │ still-fixable → repair ↺
          → finalize → lo_to_legacy → sequence_outcomes (basic→advanced)
          → review_outcomes (HITL gate — reviewer sees the sequenced order)
                the gate's per-LO reject → repair (regenerate w/ feedback) ↺ → ... → sequence → gate
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
from app.mcq_pipeline.nodes import author_outcomes, consolidate_concepts, graph_outcomes, parse_structure, repair, resolve_prerequisites, review_and_validate, select_outcomes, sequence_outcomes
from app.mcq_pipeline.state import LOState, run_ctx
from app.mcq_pipeline.utils._common import _prog
from app.mcq_pipeline.nodes.m08_generate_questions import generate_for_los
from app.mcq_pipeline.nodes.m09_review_questions import review_and_fix_for_los
from app.mcq_pipeline.nodes.m07_recommend_question_type import recommend_for_los


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
    # (Re)recommend a question type only for LOs that arrive WITHOUT one; the types planned with the
    # LO in select_outcomes (plan_los), incl. same-content type-variants, are preserved and were
    # shown at the review gate.
    ctx = run_ctx(config)
    scope.set_adapter(ctx.rag)
    los = state["final_los"]
    # PRESERVE the types planned in plan_los (incl. same-content type-variants) — only (re)recommend
    # for LOs that arrive WITHOUT a type, so re-typing can't collapse the variants.
    untyped = [lo for lo in los if not lo.get("question_type")]
    on_progress = ctx.progress.counter("recommend_question_types", len(untyped) or 1)
    if untyped:
        recommend_for_los(untyped, max_seq=None, on_progress=on_progress)   # mutates in place
    else:
        on_progress()
    # Mirror the recommended type onto the LO outcomes (the gate renders state["outcomes"]);
    # final_los[*].outcome == outcomes[*].id.
    type_by_id = {lo.get("outcome"): lo.get("question_type") for lo in los}
    rat_by_id = {lo.get("outcome"): lo.get("question_type_rationale", "") for lo in los}
    outcomes = [{**o, "question_type": type_by_id.get(o["id"], o.get("question_type")),
                 "question_type_rationale": rat_by_id.get(o["id"], o.get("question_type_rationale", ""))}
                for o in state.get("outcomes", [])]
    from collections import Counter as _Counter
    types = _Counter(lo.get("question_type", "?") for lo in los)
    ctx.progress.done("recommend_question_types",
                      detail=" · ".join(f"{t}:{n}" for t, n in types.items()),
                      snapshot={"count": len(los), "by_type": dict(types),
                                "outcomes": [{"outcome": lo.get("outcome"),
                                              "question_type": lo.get("question_type")} for lo in los]})
    return {"final_los": los, "outcomes": outcomes}


def generate_questions_node(state, config) -> dict:
    ctx = run_ctx(config)
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
    if not ctx.review_questions:
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


# --- human-in-the-loop gate (inert pass-through unless ctx.hitl_enabled) --- #
def review_outcomes(state, config) -> dict:
    """HITL gate — pause for a human to review the final LOs. The reviewer unchecks any
    outcome and gives a per-LO reason ('why'); each reason is written to that LO's
    `lo_reviews.fail_reason`, so the existing repair path regenerates exactly those LOs
    USING that feedback, then re-judges + re-reviews and pauses here again (the loop). The
    interrupt payload carries `regenerated_ids` — the LOs regenerated in the round that just
    completed — so the UI can show 'regenerated' vs 'previously approved'. Inert pass-through
    unless HITL is on. (Gate 1 / division review was removed.)"""
    ctx = run_ctx(config)
    if not getattr(ctx, "hitl_enabled", False):
        return {}
    prog = _prog(config)
    prog.start("review_outcomes", detail="awaiting human review")
    # The gate now runs AFTER sequence_outcomes, so present the outcomes in the
    # basic→advanced order the questions will follow (clearer to review). final_los[*].outcome
    # == outcomes[*].id; any id missing from the sequence falls to the end, order preserved.
    outcomes = state.get("outcomes", [])
    seq_ids = [lo.get("outcome") for lo in (state.get("final_los") or [])]
    if seq_ids:
        rank = {oid: i for i, oid in enumerate(seq_ids)}
        outcomes = sorted(outcomes, key=lambda o: rank.get(o.get("id"), len(rank)))
    from langgraph.types import interrupt
    decision = interrupt({"gate": "outcomes", "outcomes": outcomes,
                          "reviews": state.get("lo_reviews", {}),
                          "regenerated_ids": list(state.get("last_regenerated_ids") or [])})
    decision = decision if isinstance(decision, dict) else {"action": "approve"}
    action = decision.get("action", "approve")

    # Per-LO feedback: rejected = [{"id", "feedback"}]. Accept the legacy {rejected_ids, note} too.
    rejected_items = list(decision.get("rejected") or [])
    if not rejected_items and decision.get("rejected_ids"):
        note = decision.get("note", "")
        rejected_items = [{"id": rid, "feedback": note} for rid in decision["rejected_ids"]]
    rejected_ids = [r.get("id") for r in rejected_items if r.get("id")]

    prog.done("review_outcomes",
              detail=f"human {action}" + (f", {len(rejected_ids)} to regenerate" if rejected_ids else ""))
    out = {"gate_decision": {"gate": "outcomes", "action": action, "rejected_ids": rejected_ids},
           "notes": [f"Gate {action}" + (f" ({len(rejected_ids)} rejected)" if rejected_ids else "")]}
    if action == "reject" and rejected_ids:
        vr = dict(state.get("validation_report") or {})
        prev = (vr.get("V13") or {}).get("failing", [])
        vr["V13"] = {"pass": False, "detail": "human rejected at the outcomes gate",
                     "failing": sorted(set(prev) | set(rejected_ids))}
        reviews = dict(state.get("lo_reviews") or {})
        for item in rejected_items:
            rid = item.get("id")
            if not rid:
                continue
            fb = (item.get("feedback") or "").strip()
            rv = dict(reviews.get(rid) or {})
            rv.update({"covered": False, "_sig": None,
                       "fail_reason": f"human review: {fb}" if fb else "human rejected this outcome"})
            reviews[rid] = rv
        out["validation_report"] = vr
        out["lo_reviews"] = reviews
        # Marked 'regenerated' when the gate re-pauses after repair, so the UI can split the
        # just-regenerated LOs (left) from the previously-approved ones (right).
        out["last_regenerated_ids"] = rejected_ids
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


def route_after_outcomes(state) -> str:
    """approve / inert → continue to questions ; per-LO reject → repair (regenerate the
    rejected LOs, which re-runs finalize → sequence → gate)."""
    d = state.get("gate_decision") or {}
    if d.get("gate") == "outcomes" and d.get("action") == "reject" and d.get("rejected_ids"):
        return "repair"
    return "continue"


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
    g.add_node("author_outcomes", author_outcomes)
    g.add_node("consolidate_concepts", consolidate_concepts)
    g.add_node("graph_outcomes", graph_outcomes)
    g.add_node("select_outcomes", select_outcomes)
    g.add_node("resolve_prerequisites", resolve_prerequisites)
    g.add_node("review_and_validate", review_and_validate)
    g.add_node("repair", repair)
    g.add_node("review_outcomes", review_outcomes)        # HITL Gate 2 (inert unless hitl_enabled)
    g.add_node("finalize", finalize)
    g.add_node("lo_to_legacy", lo_to_legacy)
    g.add_node("sequence_outcomes", sequence_outcomes)
    g.add_node("recommend_question_types", recommend_question_types)
    g.add_node("generate_questions", generate_questions_node)
    g.add_node("review_questions", review_questions_node)

    g.add_edge(START, "parse_structure")
    g.add_edge("parse_structure", "author_outcomes")       # plan_los sub-graph (4 nodes)
    g.add_edge("author_outcomes", "consolidate_concepts")
    g.add_edge("consolidate_concepts", "graph_outcomes")
    g.add_edge("graph_outcomes", "select_outcomes")
    g.add_edge("select_outcomes", "resolve_prerequisites")  # Gate 1 (division review) removed
    g.add_edge("resolve_prerequisites", "review_and_validate")
    g.add_conditional_edges("review_and_validate", route_after_validate,
                            {"repair": "repair", "finalize": "finalize"})
    g.add_edge("repair", "resolve_prerequisites")
    g.add_edge("finalize", "lo_to_legacy")
    g.add_edge("lo_to_legacy", "sequence_outcomes")
    # Recommend question types BEFORE the gate so each LO shows its type while being reviewed;
    # the HITL gate then sits after sequencing + typing (reviewer sees basic→advanced order + type).
    g.add_edge("sequence_outcomes", "recommend_question_types")
    g.add_edge("recommend_question_types", "review_outcomes")
    g.add_conditional_edges("review_outcomes", route_after_outcomes,
                            {"repair": "repair", "continue": "generate_questions"})
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
