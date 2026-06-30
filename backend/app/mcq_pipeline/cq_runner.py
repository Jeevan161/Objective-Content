"""
app/mcq_pipeline/cq_runner.py
-----------------------------
Entry point for the CLASSROOM QUIZ pipeline — one quiz scope at a time.

Unlike `runner.run_mcq_pipeline` (which resolves reading material + a course scope from
the portal-synced Course/Unit tables and retrieves over pgvector), a classroom quiz is
grounded ONLY in what was taught in the session. So this runner:

  1. m00 — generates the session reading material from the scope's slide copy
            (BEFORE the graph, because the RagAdapter grounds on it).
  2. builds a NO-RAG RagAdapter (ingested=False) over that reading material — every
     downstream node degrades to in-memory search over the handout, never the corpus.
  3. runs the SAME compiled LO+question LangGraph, unchanged.
  4. m10 — expands each base question into objective-bound variants (AFTER the graph).

m00 and m10 are runner-level stages (not graph nodes) sharing the run's ProgressReporter,
so they appear on the live board + trace exactly like the graph nodes between them.
"""

from __future__ import annotations

import uuid
from typing import Callable

from app.db.session import SessionLocal
from app.models import ClassroomQuizDeck, ClassroomQuizScope

from app.mcq_pipeline.graph import get_lo_graph
from app.mcq_pipeline.nodes.m00_generate_reading_material import generate_reading_material
from app.mcq_pipeline.runner import (
    _attach_dependencies,
    _checkpoint_durable,
    _interrupt_payload,
    _make_trace_sink,
    _prompt_versions,
)
from app.mcq_pipeline.state import REGISTRY, RunContext, new_state
from app.mcq_pipeline.utils.llm import set_call_context
from app.mcq_pipeline.utils.progress import CQ_BASE_STAGE_DEFS, CQ_VARIANT_STAGE_DEFS, ProgressReporter
from app.mcq_pipeline.utils.rag_adapter import RagAdapter
from app.mcq_pipeline.utils.tracing import disable_langsmith


def coverage_flag(lo_count: int) -> str:
    """Map the realized LO count to a scope coverage flag (clamp: ceiling 6 / floor 4 /
    hard-floor 3). >=4 OK · 3 THIN · <3 INSUFFICIENT."""
    if lo_count >= 4:
        return ClassroomQuizScope.OK
    if lo_count == 3:
        return ClassroomQuizScope.THIN
    return ClassroomQuizScope.INSUFFICIENT


def _load_scope(scope_id) -> tuple[str, str, str, str, int]:
    """Returns (slide_text, deck_id, domain, title, scope_no) for a scope row."""
    with SessionLocal() as session:
        scope_row = session.get(ClassroomQuizScope, scope_id)
        if scope_row is None:
            raise ValueError(f"Classroom quiz scope {scope_id} not found.")
        deck = session.get(ClassroomQuizDeck, scope_row.deck_id)
        if deck is None:
            raise ValueError(f"Deck {scope_row.deck_id} not found.")
        return (scope_row.slide_text or "", str(deck.id),
                deck.question_domain or "", deck.title or "", scope_row.scope_no)


def run_classroom_quiz_pipeline(
    *, scope_id, review: bool = True, hitl_enabled: bool = False,
    lo_budget: int | None = 6,
    progress_sink: Callable[[dict], None] | None = None,
    thread_id: str | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> dict:
    disable_langsmith()
    thread_id = thread_id or str(uuid.uuid4())

    slide_text, deck_id, domain, title, scope_no = _load_scope(scope_id)
    if not slide_text.strip():
        raise ValueError("This quiz scope has no slide content to generate from.")
    scope_label = f"Quiz {scope_no}" + (f" — {title}" if title else "")

    progress = ProgressReporter(sink=progress_sink, trace_sink=_make_trace_sink(thread_id),
                                cancel_check=cancel_check, stage_defs=CQ_BASE_STAGE_DEFS)
    # Proxy metadata `unit` field (no-op for providers that don't use extra_body metadata).
    set_call_context(unit=scope_label)

    # --- m00: reading material (runner-level, before the adapter) --------------- #
    progress.start("reading_material")
    reading_material = generate_reading_material(slide_text, title=title)
    if not reading_material.strip():
        progress.error("reading_material", "no reading material could be generated")
        raise ValueError("Could not generate reading material from the slide content.")
    progress.done("reading_material", detail=f"{len(reading_material)} chars",
                  snapshot={"chars": len(reading_material)})

    # --- no-RAG adapter: grounds every node on the handout ONLY ----------------- #
    adapter = RagAdapter(course_ids=[deck_id], prereq_units=[],
                         reading_material=reading_material, ingested=False, domain=domain)
    ctx = RunContext(rag=adapter, progress=progress, db_prereq_units=[],
                     review_questions=review, question_budget=None,
                     hitl_enabled=hitl_enabled, lo_budget=lo_budget)
    REGISTRY.register(thread_id, ctx)
    state0 = new_state(session_id=str(scope_id), title=(title or scope_label),
                       source_text=reading_material)
    graph = get_lo_graph()
    cfg = {"configurable": {"thread_id": thread_id}, "recursion_limit": 80}
    try:
        state = graph.invoke(state0, config=cfg)
    finally:
        REGISTRY.pop(thread_id)

    if hitl_enabled:                         # paused at the LO review gate?
        paused = _interrupt_payload(graph, cfg)
        if paused is not None:
            return {"status": "awaiting_review", "thread_id": thread_id, "review": paused,
                    "reading_material": reading_material, "session_label": scope_label,
                    "durable_checkpoint": _checkpoint_durable(), "trace_job_id": thread_id}

    return _assemble_result(state, reading_material=reading_material,
                            session_label=scope_label, thread_id=thread_id, progress=progress)


def _load_scope_reading_material(scope_id) -> str:
    """The reading material stashed on the scope row when a HITL run first parked at the LO gate
    (used to rebuild the no-RAG adapter on resume — the adapter is not checkpointed)."""
    with SessionLocal() as session:
        scope_row = session.get(ClassroomQuizScope, scope_id)
        return (getattr(scope_row, "reading_material", "") or "") if scope_row else ""


def resume_classroom_quiz_pipeline(
    *, scope_id, decision, thread_id: str, review: bool = True, lo_budget: int | None = 6,
    progress_sink: Callable[[dict], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> dict:
    """GATE 1 (LO finalization): resume a classroom-quiz scope paused at the LO-review gate after
    a human decision. Rebuilds the no-RAG adapter from the reading material stashed on the scope
    at the park (the adapter/reporter are NOT checkpointed), re-registers the RunContext under the
    same thread_id, and resumes the LangGraph from its checkpoint with `decision`. If the LO gate
    re-pauses (rejected LOs went through the repair loop), returns another awaiting_review payload;
    otherwise the graph produces the base questions and we return the Phase-1 (base) result — which
    then flows into the existing base-question review + variants finalization (Phase 2)."""
    from langgraph.types import Command

    from app.mcq_pipeline.runner import _persist_lo_feedback

    disable_langsmith()
    _slide, deck_id, domain, title, scope_no = _load_scope(scope_id)
    reading_material = _load_scope_reading_material(scope_id)
    if not reading_material.strip():
        raise ValueError("Cannot resume: this scope has no stored reading material.")
    scope_label = f"Quiz {scope_no}" + (f" — {title}" if title else "")

    adapter = RagAdapter(course_ids=[deck_id], prereq_units=[],
                         reading_material=reading_material, ingested=False, domain=domain)
    graph = get_lo_graph()
    cfg = {"configurable": {"thread_id": thread_id}, "recursion_limit": 80}

    # Seed the fresh reporter so stages completed before the LO gate stay 'done' on the board.
    payload = _interrupt_payload(graph, cfg) or {}
    _persist_lo_feedback(thread_id, decision, payload, adapter)
    keys = [d["key"] for d in CQ_BASE_STAGE_DEFS]
    seed_done = ([k for k in keys[:keys.index("review_outcomes")] if k != "repair"]
                 if "review_outcomes" in keys else [])
    progress = ProgressReporter(sink=progress_sink, trace_sink=_make_trace_sink(thread_id),
                                seed_done=seed_done, cancel_check=cancel_check,
                                stage_defs=CQ_BASE_STAGE_DEFS)
    ctx = RunContext(rag=adapter, progress=progress, db_prereq_units=[],
                     review_questions=review, question_budget=None,
                     hitl_enabled=True, lo_budget=lo_budget)
    REGISTRY.register(thread_id, ctx)
    set_call_context(unit=scope_label)
    try:
        state = graph.invoke(Command(resume=decision), config=cfg)
        paused = _interrupt_payload(graph, cfg)
    finally:
        REGISTRY.pop(thread_id)

    if paused is not None:                   # LO gate re-paused (LOs regenerated)
        return {"status": "awaiting_review", "thread_id": thread_id, "review": paused,
                "reading_material": reading_material, "session_label": scope_label,
                "durable_checkpoint": _checkpoint_durable(), "trace_job_id": thread_id}
    return _assemble_result(state, reading_material=reading_material,
                            session_label=scope_label, thread_id=thread_id, progress=progress)


def _assemble_result(state: dict, *, reading_material: str, session_label: str,
                     thread_id: str, progress=None) -> dict:
    """Phase-1 completion path: BASE questions only. Variants are NOT generated here — they
    are created in a separate, review-gated phase (`generate_variants_for_run`) once a human
    has reviewed and finalized the base questions in the Review Queue."""
    los = state.get("final_los", [])
    questions = state.get("questions", [])
    _attach_dependencies(questions, los, state)
    # Tag every base so the UI + the variant phase can identify them unambiguously.
    for q in questions:
        q.setdefault("question_key", q.get("outcome"))
        q.setdefault("is_variant", False)

    artifact = state.get("artifact", {})
    generated = [q for q in questions if q.get("status") == "generated"]
    lo_count = len(artifact.get("outcomes", los))
    return {
        "status": "completed",
        "phase": "base",
        "session_label": session_label,
        "reading_material": reading_material,
        "ingested": False,
        "artifact": artifact,
        "lo_status": artifact.get("status", ""),
        "spec_hash": artifact.get("spec_hash", ""),
        "validation_report": artifact.get("validation_report", {}),
        "escalation": artifact.get("escalation"),
        "final_los": los,
        "questions": questions,
        "question_reviews": state.get("question_reviews", []),
        "notes": state.get("notes", []),
        "log": state.get("log", []),
        "coverage": coverage_flag(lo_count),
        "prompt_versions": _prompt_versions(),
        "trace_job_id": thread_id,
        "lo_count": lo_count,
        "question_count": len(generated),
        "base_count": len(generated),
        "variant_count": 0,
        "needs_human_count": sum(1 for q in questions if q.get("needs_human")),
        "cost": progress.usage_summary() if progress is not None else {},
    }


def generate_variants_for_run(*, run_id, progress_sink: Callable[[dict], None] | None = None,
                              thread_id: str | None = None,
                              cancel_check: Callable[[], bool] | None = None) -> dict:
    """Phase 2: expand each APPROVED, non-excluded base question of an existing Classroom-Quiz
    run into objective-bound variants (m10). Re-runnable: it drops any prior variants and
    regenerates for the current approved set. Returns the FULL updated result for persistence."""
    disable_langsmith()
    thread_id = thread_id or str(uuid.uuid4())

    from app.models import ClassroomQuizDeck, McqRun
    with SessionLocal() as session:
        run = session.get(McqRun, run_id)
        if run is None:
            raise ValueError(f"Run {run_id} not found.")
        result = dict(run.result or {})
        reading_material = run.reading_material or result.get("reading_material", "")
        deck = session.get(ClassroomQuizDeck, run.course_id) if run.course_id else None
        domain = (deck.question_domain if deck is not None else "") or ""
        deck_id = run.course_id or "cq"
        label = result.get("session_label") or "variants"

    los = result.get("final_los", [])
    questions = result.get("questions", [])
    non_variants = [q for q in questions if not q.get("is_variant")]
    # Only APPROVED (and not excluded) base questions earn variants — the human's finalization.
    bases = [q for q in non_variants
             if q.get("status") == "generated" and q.get("approval") == "approved"
             and not q.get("excluded")]

    progress = ProgressReporter(sink=progress_sink, trace_sink=_make_trace_sink(thread_id),
                                cancel_check=cancel_check, stage_defs=CQ_VARIANT_STAGE_DEFS)
    set_call_context(unit=label)
    adapter = RagAdapter(course_ids=[deck_id], prereq_units=[],
                         reading_material=reading_material, ingested=False, domain=domain)

    from app.mcq_pipeline.nodes.m10_generate_variants import (
        CQ_VARIANT_MIN, generate_variants_for_questions,
    )
    on_var = progress.counter("generate_variants", len(bases) or 1)
    variants, shortfalls = generate_variants_for_questions(los, bases, adapter=adapter,
                                                           on_progress=on_var)
    progress.done("generate_variants",
                  detail=f"{len(variants)} variants for {len(bases)} approved base(s)"
                         + (f" · {len(shortfalls)} below min {CQ_VARIANT_MIN}" if shortfalls else ""),
                  snapshot={"bases": len(bases), "variants": len(variants), "shortfalls": shortfalls})

    new_questions = non_variants + variants            # prior variants dropped, fresh set appended
    result["questions"] = new_questions
    result["variant_count"] = len(variants)
    result["variant_shortfalls"] = shortfalls
    result["phase"] = "variants"
    # Fold this variant phase's token cost into the run's total (base run cost + variant cost).
    from app.mcq_pipeline.utils import pricing
    variant_cost = progress.usage_summary()
    result["variant_cost"] = variant_cost
    result["cost"] = pricing.merge_summaries([result.get("cost") or {}, variant_cost])
    generated = [q for q in new_questions if q.get("status") == "generated"]
    return {
        "status": "completed",
        "run_id": str(run_id),
        "result": result,
        "question_count": len(generated),
        "base_count": len(bases),
        "variant_count": len(variants),
        "variant_shortfalls": shortfalls,
        "needs_human_count": sum(1 for q in new_questions if q.get("needs_human")),
        "trace_job_id": thread_id,
    }
