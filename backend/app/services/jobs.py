"""
app/services/jobs.py
--------------------
Lightweight thread-based background job runner (ported from the Django
``courses/tasks.py``). Avoids an external broker; each job runs in a daemon thread
with its OWN SQLAlchemy session, closed when the thread exits. Progress is
persisted on the SyncJob row, which the frontend polls.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone

from app.core.config import settings
from app.db.session import SessionLocal
from app.services.extraction import run_extraction_job
from app.services.portal_sync import run_sync_job
from app.services.rag_build import run_build_rag_job


def _now() -> datetime:
    return datetime.now(timezone.utc)


# Bound simultaneous MCQ pipeline runs: each fans out per-LO worker threads, K-sample
# LLM calls, and DB connections, so unbounded concurrent jobs would exhaust threads /
# the DB pool / OpenRouter rate limits. Queued jobs wait here (their SyncJob row stays
# pending) until a slot frees.
_MCQ_SEMAPHORE = threading.Semaphore(max(1, settings.mcq_max_concurrent_jobs))


def _run(target, *args) -> None:
    session = SessionLocal()
    try:
        target(session, *args)
    finally:
        session.close()


def start_sync_job(job_id: uuid.UUID) -> threading.Thread:
    thread = threading.Thread(target=_run, args=(run_sync_job, job_id), daemon=True)
    thread.start()
    return thread


def start_extraction_job(
    job_id: uuid.UUID, tokens: dict, unit_ids: list[str] | None = None
) -> threading.Thread:
    # tokens ({ENV: bearer}) are passed in memory only — never persisted.
    # unit_ids (when given) limit extraction to those reading-material parts.
    thread = threading.Thread(
        target=_run, args=(run_extraction_job, job_id, tokens, unit_ids), daemon=True
    )
    thread.start()
    return thread


def start_build_rag_job(job_id: uuid.UUID, unit_ids: list[str] | None = None) -> threading.Thread:
    thread = threading.Thread(
        target=_run, args=(run_build_rag_job, job_id, unit_ids), daemon=True
    )
    thread.start()
    return thread


def _mcq_sink(job_id: uuid.UUID):
    """Progress sink: each flush opens its own short-lived session (called from the pipeline's
    worker threads). MERGES the stage snapshot into job.progress so any non-stage keys already
    stored (e.g. a parked review payload) are preserved."""
    def sink(snapshot: dict) -> None:
        from app.models import SyncJob
        with SessionLocal() as session:
            job = session.get(SyncJob, job_id)
            if job is not None:
                prog = dict(job.progress or {})
                prog.update(snapshot)
                job.progress = prog
                if job.status not in (SyncJob.SUCCESS, SyncJob.FAILURE):
                    job.status = SyncJob.RUNNING
                job.updated_at = _now()
                session.commit()
    return sink


def _persist_mcq_result(job_id: uuid.UUID, result: dict, course_id: str,
                        topic_id: str, unit_id: str) -> None:
    """Persist the outcome of a (possibly paused) MCQ run. status 'awaiting_review' -> park the
    job at AWAITING_REVIEW with the review payload on `progress`; 'completed' -> store the McqRun
    and mark SUCCESS."""
    from app.models import McqRun, SyncJob

    with SessionLocal() as session:
        job = session.get(SyncJob, job_id)
        if result.get("status") == "awaiting_review":
            if job is not None:
                prog = dict(job.progress or {})
                prog["review"] = result.get("review")
                prog["awaiting_review"] = True
                prog["durable_checkpoint"] = result.get("durable_checkpoint", True)
                job.progress = prog
                job.status = SyncJob.AWAITING_REVIEW
                gate = (result.get("review") or {}).get("gate", "review")
                job.message = f"Awaiting human review (gate: {gate})."
                job.updated_at = _now()
            session.commit()
            return
        session.add(McqRun(
            job_id=job_id, course_id=course_id, topic_id=topic_id, unit_id=unit_id,
            langsmith_run_url=result.get("langsmith_run_url", ""),
            lo_count=result.get("lo_count", 0),
            question_count=result.get("question_count", 0),
            needs_human_count=result.get("needs_human_count", 0),
            result=result,
        ))
        if job is not None:
            job.status = SyncJob.SUCCESS
            job.message = (
                f"{result.get('question_count', 0)} question(s) from "
                f"{result.get('lo_count', 0)} LO(s); "
                f"{result.get('needs_human_count', 0)} need human review."
            )
            prog = dict(job.progress or {})
            prog.pop("awaiting_review", None)
            prog.pop("review", None)
            job.progress = prog
            job.updated_at = _now()
        session.commit()


def _fail_job(job_id: uuid.UUID, err: Exception) -> None:
    from app.models import SyncJob
    with SessionLocal() as session:
        job = session.get(SyncJob, job_id)
        if job is not None:
            job.status = SyncJob.FAILURE
            job.error = str(err)
            job.updated_at = _now()
            session.commit()


def _run_mcq_job(job_id: uuid.UUID, course_id: str, topic_id: str, unit_id: str,
                 review: bool, prereq_unit_ids: list[str] | None = None,
                 question_budget: int | None = None, hitl_enabled: bool = False) -> None:
    """Run the MCQ pipeline, streaming progress onto the SyncJob row. May PAUSE at a HITL gate
    (status -> AWAITING_REVIEW) instead of completing. Heavy imports are deferred to here."""
    from app.mcq_pipeline.runner import run_mcq_pipeline

    try:
        with _MCQ_SEMAPHORE:
            result = run_mcq_pipeline(
                course_id=course_id, topic_id=topic_id, unit_id=unit_id,
                review=review, prereq_unit_ids=prereq_unit_ids,
                question_budget=question_budget, hitl_enabled=hitl_enabled,
                progress_sink=_mcq_sink(job_id), thread_id=str(job_id),
            )
        _persist_mcq_result(job_id, result, course_id, topic_id, unit_id)
    except Exception as err:  # noqa: BLE001 — surface failure on the job row
        _fail_job(job_id, err)


def _resume_mcq_job(job_id: uuid.UUID, course_id: str, topic_id: str, unit_id: str,
                    decision: dict, prereq_unit_ids: list[str] | None = None,
                    question_budget: int | None = None, review: bool = True) -> None:
    """Resume a HITL-paused MCQ run after a human decision; may pause AGAIN at the next gate."""
    from app.mcq_pipeline.runner import resume_run

    try:
        with _MCQ_SEMAPHORE:
            result = resume_run(
                course_id=course_id, unit_id=unit_id, thread_id=str(job_id), decision=decision,
                prereq_unit_ids=prereq_unit_ids, question_budget=question_budget, review=review,
                progress_sink=_mcq_sink(job_id),
            )
        _persist_mcq_result(job_id, result, course_id, topic_id, unit_id)
    except Exception as err:  # noqa: BLE001
        _fail_job(job_id, err)


def start_mcq_job(job_id: uuid.UUID, course_id: str, topic_id: str, unit_id: str,
                  review: bool = True, prereq_unit_ids: list[str] | None = None,
                  question_budget: int | None = None, hitl_enabled: bool = False) -> threading.Thread:
    thread = threading.Thread(
        target=_run_mcq_job,
        args=(job_id, course_id, topic_id, unit_id, review, prereq_unit_ids,
              question_budget, hitl_enabled),
        daemon=True,
    )
    thread.start()
    return thread


def start_mcq_resume_job(job_id: uuid.UUID, course_id: str, topic_id: str, unit_id: str,
                         decision: dict, prereq_unit_ids: list[str] | None = None,
                         question_budget: int | None = None, review: bool = True) -> threading.Thread:
    thread = threading.Thread(
        target=_resume_mcq_job,
        args=(job_id, course_id, topic_id, unit_id, decision, prereq_unit_ids,
              question_budget, review),
        daemon=True,
    )
    thread.start()
    return thread
