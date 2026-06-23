"""
app/mcq_pipeline/runner.py
--------------------------
Entry point: run the MCQ pipeline for one selected course/topic/session and return
a structured result (LOs + questions + reviews + trace + prompt versions).

Resolves the session's reading material + the course-scope (course + prerequisite
courses) from the DB, binds the RAG adapter, and invokes the LangGraph under a
LangSmith trace, streaming live progress via `progress_sink`.
"""

from __future__ import annotations

from typing import Callable

from sqlalchemy import func, select

from app.db.session import SessionLocal
from app.models import Course, RagChunk, Topic, Unit, UnitPart
from app.services.extraction import READING_MATERIAL_LABEL, collect_courses_recursive

import threading
import uuid

from app.mcq_pipeline.prompts import store as prompt_store
from app.mcq_pipeline.utils.llm import set_call_context
from app.mcq_pipeline.graph import get_lo_graph
from app.mcq_pipeline.state import REGISTRY, RunContext, new_state
from app.mcq_pipeline.utils.progress import ProgressReporter
from app.mcq_pipeline.utils.rag_adapter import RagAdapter
from app.mcq_pipeline.utils.tracing import disable_langsmith


def _session_reading_material(session, course_id: str, unit_id: str) -> tuple[str, str]:
    """Assemble the reading material for the session that owns the reading-material
    part `unit_id`. Returns (reading_material, session_label)."""
    part = session.scalar(
        select(UnitPart)
        .join(Unit, UnitPart.container_id == Unit.id)
        .join(Topic, Unit.topic_id == Topic.id)
        .where(Topic.course_id == course_id, UnitPart.unit_id == unit_id)
    )
    if part is None:
        return "", ""
    container = part.container
    pieces = [
        p.content for p in container.parts
        if p.label == READING_MATERIAL_LABEL and (p.content or "").strip()
    ]
    return "\n\n---\n\n".join(pieces), (container.label or "")


def _has_chunks(session, course_ids: list[str]) -> bool:
    return bool(session.scalar(
        select(func.count()).select_from(RagChunk).where(RagChunk.course_id.in_(course_ids))
    ))


def _reading_unit_ids(session, course_id: str) -> list[str]:
    """Reading-material portal unit_ids for a course's SESSION units."""
    rows = session.execute(
        select(UnitPart.unit_id)
        .join(Unit, UnitPart.container_id == Unit.id)
        .join(Topic, Unit.topic_id == Topic.id)
        .where(
            Topic.course_id == course_id,
            Unit.kind == Unit.SESSION,
            UnitPart.label == READING_MATERIAL_LABEL,
        )
    ).all()
    return [r[0] for r in rows]


def _session_labels(session, course_id: str, unit_ids: list[str]) -> list[dict]:
    """Label + order for the CURRENT course's reading-material sessions identified by
    `unit_ids`. Used to attach earlier same-course sessions as prerequisites."""
    if not unit_ids:
        return []
    rows = session.execute(
        select(Unit.label, Topic.order, Unit.order, UnitPart.unit_id)
        .join(Unit, UnitPart.container_id == Unit.id)
        .join(Topic, Unit.topic_id == Topic.id)
        .where(
            Topic.course_id == course_id,
            UnitPart.label == READING_MATERIAL_LABEL,
            UnitPart.unit_id.in_(unit_ids),
        )
    ).all()
    return [
        {"unit_id": uid, "label": label or uid, "seq": (t_order or 0) * 1000 + (u_order or 0)}
        for (label, t_order, u_order, uid) in rows
    ]


def _unit_owner_courses(session, course_ids: list[str], unit_ids: list[str]) -> set[str]:
    """course_ids that own any of the given reading-material unit_ids."""
    if not unit_ids:
        return set()
    rows = session.execute(
        select(Topic.course_id)
        .join(Unit, Unit.topic_id == Topic.id)
        .join(UnitPart, UnitPart.container_id == Unit.id)
        .where(Topic.course_id.in_(course_ids), UnitPart.unit_id.in_(unit_ids))
        .distinct()
    ).all()
    return {r[0] for r in rows}


def build_adapter(course_id: str, unit_id: str, prereq_unit_ids: list[str] | None = None):
    """Build a run-scoped RagAdapter for a course/session (reused by single-question
    regeneration so it grounds on the same course scope). Returns (adapter,
    prereq_units, session_label)."""
    with SessionLocal() as session:
        course = session.get(Course, course_id)
        if course is None:
            raise ValueError("Course not found.")
        reading_material, session_label = _session_reading_material(session, course_id, unit_id)
        if not reading_material.strip():
            raise ValueError("This session has no extracted reading-material content.")
        courses = collect_courses_recursive(course)
        prereq_courses = [c for c in courses if c.course_id != course_id]

        unit_filter: list[str] | None = None
        current_prereq_units: list[dict] = []
        if prereq_unit_ids is None:
            scope_courses = prereq_courses
        else:
            selected = set(prereq_unit_ids)
            current_reading = set(_reading_unit_ids(session, course_id))
            selected_current = [u for u in selected if u in current_reading]
            selected_prereq = [u for u in selected if u not in current_reading]
            owners = _unit_owner_courses(session, [c.course_id for c in prereq_courses], selected_prereq)
            scope_courses = [c for c in prereq_courses if c.course_id in owners]
            unit_filter = list(dict.fromkeys([unit_id] + list(current_reading) + list(selected))) or None
            current_prereq_units = [
                {"course_name": course.course_name, "unit_name": s["label"], "seq": s["seq"]}
                for s in sorted(_session_labels(session, course_id, selected_current), key=lambda s: s["seq"])
            ]

        course_ids = [course_id] + [c.course_id for c in scope_courses]
        prereq_units = [
            {"course_name": c.course_name, "unit_name": c.course_name, "seq": 0}
            for c in scope_courses
        ] + current_prereq_units
        ingested = _has_chunks(session, course_ids)

    adapter = RagAdapter(
        course_ids=course_ids, prereq_units=prereq_units,
        reading_material=reading_material, ingested=ingested, unit_ids=unit_filter,
    )
    return adapter, prereq_units, session_label


def _attach_dependencies(questions: list, los: list, state: dict) -> None:
    """Attach per-question dependency context — the concept it tests, the concepts it
    DEPENDS ON (prerequisites, must-know-first), and the concepts that BUILD on it — derived
    from the prerequisite DAG. Lets the reviewer see each question's place in the deep dive."""
    adj = (state.get("concept_graph") or {}).get("_adj", {})           # A -> [B,...] : A prereq of B
    name = {c["concept_id"]: c.get("canonical_name", c["concept_id"])
            for c in (state.get("concept_inventory") or [])}
    cid_of = {lo.get("outcome"): lo.get("concept_id") for lo in (los or [])}
    parents: dict = {}
    for a, kids in adj.items():
        for b in kids:
            parents.setdefault(b, []).append(a)
    for q in (questions or []):
        cid = cid_of.get(q.get("outcome"))
        if not cid:
            continue
        q["dependencies"] = {
            "concept": name.get(cid, cid),
            "prerequisites": [name.get(p, p) for p in parents.get(cid, [])],
            "builds_toward": [name.get(d, d) for d in adj.get(cid, [])],
        }


def _prompt_versions() -> list[dict]:
    """The active prompt versions in effect, for run reproducibility."""
    from app.models import McqPrompt

    with SessionLocal() as session:
        rows = session.scalars(select(McqPrompt).where(McqPrompt.active.is_(True))).all()
        return [{"key": r.key, "version": r.version} for r in rows]


def run_mcq_pipeline(
    *, course_id: str, topic_id: str, unit_id: str, review: bool = True,
    prereq_unit_ids: list[str] | None = None,
    question_budget: int | None = None,
    hitl_enabled: bool = False,
    progress_sink: Callable[[dict], None] | None = None,
    thread_id: str | None = None,
) -> dict:
    disable_langsmith()
    # The checkpointer keys every run by thread_id; default to a fresh uuid when the
    # caller (e.g. the job runner) doesn't supply the job id.
    thread_id = thread_id or str(uuid.uuid4())

    with SessionLocal() as session:
        course = session.get(Course, course_id)
        if course is None:
            raise ValueError("Course not found.")
        reading_material, session_label = _session_reading_material(session, course_id, unit_id)
        if not reading_material.strip():
            raise ValueError("This session has no extracted reading-material content to generate from.")

        courses = collect_courses_recursive(course)
        prereq_courses = [c for c in courses if c.course_id != course_id]

        # Scope the prerequisites to the user's selection. None = include all.
        # The selection may include EARLIER SESSIONS OF THE CURRENT COURSE (offered in
        # the picker) as well as prerequisite-course units — they're handled separately.
        unit_filter: list[str] | None = None
        current_prereq_units: list[dict] = []
        if prereq_unit_ids is None:
            scope_courses = prereq_courses
        else:
            selected = set(prereq_unit_ids)
            current_reading = set(_reading_unit_ids(session, course_id))
            selected_current = [u for u in selected if u in current_reading]   # same course, earlier sessions
            selected_prereq = [u for u in selected if u not in current_reading]
            owners = _unit_owner_courses(
                session, [c.course_id for c in prereq_courses], selected_prereq
            )
            scope_courses = [c for c in prereq_courses if c.course_id in owners]
            # Restrict RAG search to the main course's units + the chosen prereq
            # units, always including the CURRENT session's unit so the session
            # being generated for stays in scope regardless of the prereq selection.
            unit_filter = list(dict.fromkeys(
                [unit_id] + list(current_reading) + list(selected)
            )) or None
            # Selected earlier same-course sessions become explicit prerequisites,
            # attached to every outcome (alongside any prerequisite-course units).
            current_prereq_units = [
                {"course_name": course.course_name, "unit_name": s["label"], "seq": s["seq"]}
                for s in sorted(_session_labels(session, course_id, selected_current),
                                key=lambda s: s["seq"])
            ]

        course_ids = [course_id] + [c.course_id for c in scope_courses]
        prereq_units = [
            {"course_name": c.course_name, "unit_name": c.course_name, "seq": 0}
            for c in scope_courses
        ] + current_prereq_units
        ingested = _has_chunks(session, course_ids)

    adapter = RagAdapter(
        course_ids=course_ids, prereq_units=prereq_units,
        reading_material=reading_material, ingested=ingested, unit_ids=unit_filter,
    )
    progress = ProgressReporter(sink=progress_sink, trace_sink=_make_trace_sink(thread_id))

    # Live, non-serializable objects ride in the RunContext (keyed by thread_id),
    # never in checkpointed state. Always cleared so the registry can't leak.
    ctx = RunContext(
        rag=adapter, progress=progress, db_prereq_units=prereq_units,
        generate_questions=True, review_questions=review,
        question_budget=question_budget, hitl_enabled=hitl_enabled,
    )
    REGISTRY.register(thread_id, ctx)
    state0 = new_state(
        session_id=(unit_id or session_label or thread_id),
        title=session_label, source_text=reading_material,
    )
    # Populate the proxy metadata's `unit` field (required by the internal proxy) with
    # this session's label. No-op for providers that don't use extra_body metadata.
    set_call_context(unit=(session_label or unit_id or thread_id))
    graph = get_lo_graph()
    cfg = {"configurable": {"thread_id": thread_id}, "recursion_limit": 80}
    try:
        state = graph.invoke(state0, config=cfg)
    finally:
        REGISTRY.pop(thread_id)

    if hitl_enabled:                         # paused at a HITL gate? return a review payload.
        paused = _interrupt_payload(graph, cfg)
        if paused is not None:
            return {"status": "awaiting_review", "thread_id": thread_id, "review": paused,
                    "durable_checkpoint": _checkpoint_durable(),
                    "session_label": session_label, "prereq_units": prereq_units,
                    "trace_job_id": thread_id}

    artifact = state.get("artifact", {})
    los = state.get("final_los", [])
    questions = state.get("questions", [])
    _attach_dependencies(questions, los, state)   # concept + prerequisites + builds_toward, per question
    generated = [q for q in questions if q.get("status") == "generated"]
    needs_human = sum(1 for q in questions if q.get("needs_human"))

    return {
        "status": "completed",
        "session_label": session_label,
        "ingested": ingested,
        "artifact": artifact,
        "lo_status": artifact.get("status", ""),
        "spec_hash": artifact.get("spec_hash", ""),
        "validation_report": artifact.get("validation_report", {}),
        "overrides": artifact.get("overrides", []),
        "escalation": artifact.get("escalation"),
        "final_los": los,
        "questions": questions,
        "question_reviews": state.get("question_reviews", []),
        "notes": state.get("notes", []),
        "log": state.get("log", []),
        "prereq_units": prereq_units,
        "prompt_versions": _prompt_versions(),
        "trace_job_id": thread_id,
        "lo_count": len(artifact.get("outcomes", los)),
        "question_count": len(generated),
        "needs_human_count": needs_human,
    }


def _make_trace_sink(thread_id: str):
    """Build a trace sink that writes one `McqTrace` row per node span — our own LangGraph-tailored
    trace (job_id = the run's thread_id). Own short-lived session per write, like the progress sink.
    Best-effort: never raises, so tracing can't break a run; returns None for a non-uuid thread_id."""
    try:
        jid = uuid.UUID(str(thread_id))
    except Exception:  # noqa: BLE001
        return None
    counter = {"n": 0}
    lock = threading.Lock()

    def sink(span: dict) -> None:
        from app.models import McqTrace
        with lock:
            counter["n"] += 1
            n = counter["n"]
        try:
            with SessionLocal() as session:
                session.add(McqTrace(
                    job_id=jid, seq=n, node=span.get("node", ""),
                    label=(span.get("label") or "")[:160], status=span.get("status", "ok"),
                    detail=(span.get("detail") or "")[:2000],
                    duration_ms=int(span.get("duration_ms", 0)),
                    snapshot=(span.get("snapshot") or {}),
                    started_at=span.get("started_at"), ended_at=span.get("ended_at"),
                ))
                session.commit()
        except Exception:  # noqa: BLE001 — tracing is best-effort
            pass

    return sink


def _checkpoint_durable() -> bool:
    """True if the active LangGraph checkpointer is the durable Postgres saver. HITL pause/resume
    needs a durable checkpoint to survive across workers/restarts; an in-memory fallback (Postgres
    unavailable when the singleton was built) cannot be resumed from another process — callers
    should treat durable_checkpoint=False as 'do not rely on resume'."""
    try:
        from app.mcq_pipeline.graph import get_checkpointer
        return type(get_checkpointer()).__name__ == "PostgresSaver"
    except Exception:  # noqa: BLE001
        return False


def _interrupt_payload(graph, cfg) -> dict | None:
    """If the graph is paused at a HITL interrupt, return that interrupt's payload; else None.
    Best-effort across LangGraph versions; when no interrupt is pending the graph reached END, so
    None is returned and the normal (completed) path runs unchanged."""
    try:
        snap = graph.get_state(cfg)
    except Exception:  # noqa: BLE001
        return None
    if not getattr(snap, "next", None):
        return None                          # graph reached END — not paused
    try:
        for task in (getattr(snap, "tasks", None) or []):
            for it in (getattr(task, "interrupts", None) or []):
                val = getattr(it, "value", None)
                if val is not None:
                    return val
    except Exception:  # noqa: BLE001
        pass
    return {"paused": True}


def resume_run(*, course_id: str, unit_id: str, thread_id: str, decision,
               prereq_unit_ids: list[str] | None = None, question_budget: int | None = None,
               review: bool = True, progress_sink: Callable[[dict], None] | None = None) -> dict:
    """Resume a HITL-paused run after a human decision (Gate 1 / Gate 2). Rebuilds the run-scoped
    RagAdapter/ProgressReporter (they are NOT checkpointed) and re-registers the RunContext under
    the SAME thread_id, then resumes the graph from its checkpoint with the decision. If the run
    pauses again (the other gate), returns another awaiting_review payload; otherwise the completed
    result. `decision` is e.g. {"action":"approve"} or {"action":"reject","rejected_ids":[...],
    "note":"..."}."""
    from langgraph.types import Command

    from app.mcq_pipeline.utils.progress import STAGE_DEFS
    disable_langsmith()
    adapter, prereq_units, session_label = build_adapter(course_id, unit_id, prereq_unit_ids)
    graph = get_lo_graph()
    cfg = {"configurable": {"thread_id": thread_id}, "recursion_limit": 80}
    # Which gate are we resuming from? Seed the fresh reporter so the stages already completed on the
    # original run stay 'done' on the board (otherwise the resume would reset them all to 'pending').
    gate = (_interrupt_payload(graph, cfg) or {}).get("gate")
    gate_key = {"division": "review_division", "outcomes": "review_outcomes"}.get(gate)
    keys = [d["key"] for d in STAGE_DEFS]
    seed_done = ([k for k in keys[:keys.index(gate_key)] if k != "repair"]   # repair is conditional
                 if gate_key in keys else [])
    progress = ProgressReporter(sink=progress_sink, trace_sink=_make_trace_sink(thread_id),
                                seed_done=seed_done)
    ctx = RunContext(rag=adapter, progress=progress, db_prereq_units=prereq_units,
                     generate_questions=True, review_questions=review,
                     question_budget=question_budget, hitl_enabled=True)
    REGISTRY.register(thread_id, ctx)
    set_call_context(unit=(session_label or unit_id or thread_id))
    try:
        state = graph.invoke(Command(resume=decision), config=cfg)
        paused = _interrupt_payload(graph, cfg)
    finally:
        REGISTRY.pop(thread_id)
    if paused is not None:
        return {"status": "awaiting_review", "thread_id": thread_id, "review": paused,
                "durable_checkpoint": _checkpoint_durable(),
                "session_label": session_label, "prereq_units": prereq_units,
                "trace_job_id": thread_id}
    artifact = state.get("artifact", {})
    los = state.get("final_los", [])
    questions = state.get("questions", [])
    _attach_dependencies(questions, los, state)
    generated = [q for q in questions if q.get("status") == "generated"]
    return {
        "status": "completed", "session_label": session_label,
        "ingested": getattr(adapter, "ingested", False),
        "artifact": artifact, "lo_status": artifact.get("status", ""),
        "spec_hash": artifact.get("spec_hash", ""),
        "validation_report": artifact.get("validation_report", {}),
        "overrides": artifact.get("overrides", []), "escalation": artifact.get("escalation"),
        "final_los": los, "questions": questions,
        "question_reviews": state.get("question_reviews", []),
        "notes": state.get("notes", []), "log": state.get("log", []),
        "prereq_units": prereq_units, "prompt_versions": _prompt_versions(),
        "trace_job_id": thread_id,
        "lo_count": len(artifact.get("outcomes", los)),
        "question_count": len(generated),
        "needs_human_count": sum(1 for q in questions if q.get("needs_human")),
    }
