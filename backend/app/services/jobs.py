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


def _run_mcq_job(job_id: uuid.UUID, course_id: str, topic_id: str, unit_id: str,
                 review: bool, prereq_unit_ids: list[str] | None = None) -> None:
    """Run the MCQ pipeline end to end, streaming structured progress onto the
    SyncJob row and persisting the result as an McqRun. Heavy imports (langgraph)
    are deferred to here so the API doesn't depend on them at import time."""
    from app.models import McqRun, SyncJob
    from app.mcq_pipeline.runner import run_mcq_pipeline

    def sink(snapshot: dict) -> None:
        # Each progress flush opens its own short-lived session (called from the
        # pipeline's worker threads), mirroring the other job writers.
        with SessionLocal() as session:
            job = session.get(SyncJob, job_id)
            if job is not None:
                job.progress = snapshot
                if job.status not in (SyncJob.SUCCESS, SyncJob.FAILURE):
                    job.status = SyncJob.RUNNING
                job.updated_at = _now()
                session.commit()

    try:
        with _MCQ_SEMAPHORE:
            result = run_mcq_pipeline(
                course_id=course_id, topic_id=topic_id, unit_id=unit_id,
                review=review, prereq_unit_ids=prereq_unit_ids, progress_sink=sink,
                thread_id=str(job_id),
            )
        with SessionLocal() as session:
            session.add(McqRun(
                job_id=job_id, course_id=course_id, topic_id=topic_id, unit_id=unit_id,
                langsmith_run_url=result.get("langsmith_run_url", ""),
                lo_count=result.get("lo_count", 0),
                question_count=result.get("question_count", 0),
                needs_human_count=result.get("needs_human_count", 0),
                result=result,
            ))
            job = session.get(SyncJob, job_id)
            if job is not None:
                job.status = SyncJob.SUCCESS
                job.message = (
                    f"{result.get('question_count', 0)} question(s) from "
                    f"{result.get('lo_count', 0)} LO(s); "
                    f"{result.get('needs_human_count', 0)} need human review."
                )
                job.updated_at = _now()
            session.commit()
    except Exception as err:  # noqa: BLE001 — surface failure on the job row
        with SessionLocal() as session:
            job = session.get(SyncJob, job_id)
            if job is not None:
                job.status = SyncJob.FAILURE
                job.error = str(err)
                job.updated_at = _now()
                session.commit()


def start_mcq_job(job_id: uuid.UUID, course_id: str, topic_id: str, unit_id: str,
                  review: bool = True, prereq_unit_ids: list[str] | None = None) -> threading.Thread:
    thread = threading.Thread(
        target=_run_mcq_job,
        args=(job_id, course_id, topic_id, unit_id, review, prereq_unit_ids),
        daemon=True,
    )
    thread.start()
    return thread
