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
from app.mcq_pipeline.utils.progress import CQ_STAGE_DEFS, ProgressReporter
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
                                cancel_check=cancel_check, stage_defs=CQ_STAGE_DEFS)
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

    return _assemble_result(state, reading_material=reading_material, adapter=adapter,
                            session_label=scope_label, thread_id=thread_id, progress=progress)


def _assemble_result(state: dict, *, reading_material: str, adapter, session_label: str,
                     thread_id: str, progress) -> dict:
    """Shared completion path (also reused by the CQ HITL resume in P5). m10 expands each base
    question into objective-bound variants; variants live alongside the bases in `questions`,
    linked by `base_question_key`."""
    los = state.get("final_los", [])
    questions = state.get("questions", [])
    _attach_dependencies(questions, los, state)

    # --- m10: variants (per base) — runner-level stage, sharing the run's adapter -------- #
    from app.mcq_pipeline.nodes.m10_generate_variants import (
        CQ_VARIANT_MIN, generate_variants_for_questions,
    )
    bases = [q for q in questions if q.get("status") == "generated"]
    on_var = progress.counter("generate_variants", len(bases) or 1)
    variants, shortfalls = generate_variants_for_questions(los, questions, adapter=adapter,
                                                           on_progress=on_var)
    questions = questions + variants
    progress.done("generate_variants",
                  detail=f"{len(variants)} variants for {len(bases)} bases"
                         + (f" · {len(shortfalls)} below min {CQ_VARIANT_MIN}" if shortfalls else ""),
                  snapshot={"bases": len(bases), "variants": len(variants),
                            "shortfalls": shortfalls})

    artifact = state.get("artifact", {})
    generated = [q for q in questions if q.get("status") == "generated"]
    lo_count = len(artifact.get("outcomes", los))
    return {
        "status": "completed",
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
        "base_count": len(bases),
        "variant_count": len(variants),
        "variant_shortfalls": shortfalls,
        "needs_human_count": sum(1 for q in questions if q.get("needs_human")),
    }
