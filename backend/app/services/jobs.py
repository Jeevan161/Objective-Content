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
from app.services import progress_broker
from app.services.extraction import run_extraction_job
from app.services.portal_sync import run_sync_job
from app.services.rag_build import run_build_rag_job


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _bind_user_for_job(job_id: uuid.UUID):
    """Bind the triggering user's API key FOR THE ACTIVE CONNECTOR on this job thread
    (so every pipeline LLM call uses it) and return the user id for attribution/logging.
    Falls back to the global connector key when there's no user or no key."""
    from app.mcq_pipeline.utils import scope
    from app.models import SyncJob
    from app.services.user_keys import active_provider, get_user_key
    user_id = None
    try:
        with SessionLocal() as s:
            job = s.get(SyncJob, job_id)
            user_id = getattr(job, "created_by", None) if job is not None else None
            prov = active_provider(s)
            if user_id and prov is not None:
                key = get_user_key(s, user_id, prov.id)
                if key:
                    scope.set_user_api_key(key)
    except Exception:  # noqa: BLE001 — never block a job on key binding
        pass
    return user_id


# Bound simultaneous MCQ pipeline runs: each fans out per-LO worker threads, K-sample
# LLM calls, and DB connections, so unbounded concurrent jobs would exhaust threads /
# the DB pool / OpenRouter rate limits. Queued jobs wait here (their SyncJob row stays
# pending) until a slot frees.
_MCQ_SEMAPHORE = threading.Semaphore(max(1, settings.mcq_max_concurrent_jobs))


# --- cooperative job cancellation (in-process; single uvicorn process) -------------- #
# A running/queued MCQ job registers a threading.Event here keyed by str(job_id). The
# cancel endpoint sets it; the pipeline's ProgressReporter consults `is_cancelled` on
# every stage transition and raises JobCancelled, which the runner unwinds.
_cancel_events: dict[str, threading.Event] = {}
_cancel_lock = threading.Lock()


def _register_cancel(job_id: uuid.UUID) -> threading.Event:
    ev = threading.Event()
    with _cancel_lock:
        _cancel_events[str(job_id)] = ev
    return ev


def _clear_cancel(job_id: uuid.UUID) -> None:
    with _cancel_lock:
        _cancel_events.pop(str(job_id), None)


def is_cancelled(job_id: uuid.UUID) -> bool:
    with _cancel_lock:
        ev = _cancel_events.get(str(job_id))
    return bool(ev is not None and ev.is_set())


def request_cancel(job_id: uuid.UUID) -> bool:
    """Signal a running/queued MCQ job to stop at its next cooperative checkpoint.
    Returns True if a live worker is registered to observe the signal."""
    with _cancel_lock:
        ev = _cancel_events.get(str(job_id))
    if ev is not None:
        ev.set()
        return True
    return False


def _cancel_job(job_id: uuid.UUID) -> None:
    """Finalize a job as CANCELLED (clearing any parked review payload) and notify sockets."""
    from app.models import SyncJob
    with SessionLocal() as session:
        job = session.get(SyncJob, job_id)
        if job is not None and job.status not in (SyncJob.SUCCESS, SyncJob.FAILURE):
            job.status = SyncJob.CANCELLED
            job.message = "Cancelled by user."
            prog = dict(job.progress or {})
            prog.pop("awaiting_review", None)
            prog.pop("review", None)
            job.progress = prog
            job.updated_at = _now()
            session.commit()
    progress_broker.publish(str(job_id))


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
        progress_broker.publish(str(job_id))   # nudge live WebSocket subscribers (no-op if none)
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
                job.message = f"Awaiting review: {gate}."
                job.updated_at = _now()
            session.commit()
            progress_broker.publish(str(job_id))   # push the paused/awaiting state to the socket
            return
        # Generation version for this session = (# prior runs of the same course/session) + 1.
        from sqlalchemy import func, select
        prior = session.scalar(
            select(func.count()).select_from(McqRun)
            .where(McqRun.course_id == course_id, McqRun.unit_id == unit_id)
        ) or 0
        session.add(McqRun(
            job_id=job_id, course_id=course_id, topic_id=topic_id, unit_id=unit_id,
            langsmith_run_url=result.get("langsmith_run_url", ""),
            lo_count=result.get("lo_count", 0),
            question_count=result.get("question_count", 0),
            needs_human_count=result.get("needs_human_count", 0),
            version=prior + 1,
            result=result,
            created_by=getattr(job, "created_by", None),   # attribute the run to its user
        ))
        if job is not None:
            job.status = SyncJob.SUCCESS
            job.message = (
                f"{result.get('question_count', 0)} questions, "
                f"{result.get('lo_count', 0)} LOs, "
                f"{result.get('needs_human_count', 0)} to review."
            )
            prog = dict(job.progress or {})
            prog.pop("awaiting_review", None)
            prog.pop("review", None)
            job.progress = prog
            job.updated_at = _now()
        session.commit()
        progress_broker.publish(str(job_id))       # push the terminal (SUCCESS/FAILURE) state


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
    import traceback

    from app.mcq_pipeline.runner import run_mcq_pipeline
    from app.mcq_pipeline.utils.progress import JobCancelled
    from app.services.task_log import ERROR, log_task

    user_id = _bind_user_for_job(job_id)
    cancel_event = _register_cancel(job_id)
    log_task(task_type="MCQ", event="start", job_id=job_id, user_id=user_id,
             message=f"course={course_id} unit={unit_id}")
    try:
        with _MCQ_SEMAPHORE:
            if cancel_event.is_set():            # cancelled while queued on the semaphore
                raise JobCancelled()
            result = run_mcq_pipeline(
                course_id=course_id, topic_id=topic_id, unit_id=unit_id,
                review=review, prereq_unit_ids=prereq_unit_ids,
                question_budget=question_budget, hitl_enabled=hitl_enabled,
                progress_sink=_mcq_sink(job_id), thread_id=str(job_id),
                cancel_check=cancel_event.is_set,
            )
        _persist_mcq_result(job_id, result, course_id, topic_id, unit_id)
        log_task(task_type="MCQ", event="complete", job_id=job_id, user_id=user_id,
                 message=str(result.get("status", "completed")))
    except JobCancelled:
        log_task(task_type="MCQ", event="cancelled", job_id=job_id, user_id=user_id,
                 message="cancelled by user")
        _cancel_job(job_id)
    except Exception as err:  # noqa: BLE001 — surface failure on the job row
        log_task(task_type="MCQ", event="error", level=ERROR, job_id=job_id, user_id=user_id,
                 message=str(err), detail={"trace": traceback.format_exc()[:8000]})
        _fail_job(job_id, err)
    finally:
        _clear_cancel(job_id)


def _resume_mcq_job(job_id: uuid.UUID, course_id: str, topic_id: str, unit_id: str,
                    decision: dict, prereq_unit_ids: list[str] | None = None,
                    question_budget: int | None = None, review: bool = True) -> None:
    """Resume a HITL-paused MCQ run after a human decision; may pause AGAIN at the next gate."""
    import traceback

    from app.mcq_pipeline.runner import resume_run
    from app.mcq_pipeline.utils.progress import JobCancelled
    from app.services.task_log import ERROR, log_task

    user_id = _bind_user_for_job(job_id)
    cancel_event = _register_cancel(job_id)
    log_task(task_type="MCQ", event="resume", job_id=job_id, user_id=user_id,
             message=f"course={course_id} unit={unit_id}")
    try:
        with _MCQ_SEMAPHORE:
            if cancel_event.is_set():
                raise JobCancelled()
            result = resume_run(
                course_id=course_id, unit_id=unit_id, thread_id=str(job_id), decision=decision,
                prereq_unit_ids=prereq_unit_ids, question_budget=question_budget, review=review,
                progress_sink=_mcq_sink(job_id), cancel_check=cancel_event.is_set,
            )
        _persist_mcq_result(job_id, result, course_id, topic_id, unit_id)
        log_task(task_type="MCQ", event="complete", job_id=job_id, user_id=user_id,
                 message=str(result.get("status", "completed")))
    except JobCancelled:
        log_task(task_type="MCQ", event="cancelled", job_id=job_id, user_id=user_id,
                 message="cancelled by user")
        _cancel_job(job_id)
    except Exception as err:  # noqa: BLE001
        log_task(task_type="MCQ", event="error", level=ERROR, job_id=job_id, user_id=user_id,
                 message=str(err), detail={"trace": traceback.format_exc()[:8000]})
        _fail_job(job_id, err)
    finally:
        _clear_cancel(job_id)


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


def _run_mcq_regen_job(job_id: uuid.UUID, run_id: uuid.UUID, outcome: str,
                       feedback: str, tags: list[str] | None, reviewer: str) -> None:
    """Regenerate ONE question for its LO in the background so the action is tracked as an
    Activity (and the reviewer can keep working). `regenerate_question` re-reviews, persists
    the new question onto the run, and logs the feedback; the frontend re-fetches the run on
    success. Shares the MCQ semaphore so a regen never crowds out a full generation."""
    import traceback

    from app.mcq_pipeline.review import regenerate_question
    from app.models import SyncJob
    from app.services.task_log import ERROR, log_task

    user_id = _bind_user_for_job(job_id)
    log_task(task_type="REGEN", event="start", job_id=job_id, user_id=user_id,
             message=f"run={run_id} outcome={outcome}")
    try:
        with _MCQ_SEMAPHORE:
            regenerate_question(run_id, outcome, feedback, reviewer=reviewer, tags=tags or [])
        with SessionLocal() as session:
            job = session.get(SyncJob, job_id)
            if job is not None:
                job.status = SyncJob.SUCCESS
                job.message = f"Regenerated “{outcome}”."
                prog = dict(job.progress or {})
                prog["regen"] = {"run_id": str(run_id), "outcome": outcome}
                job.progress = prog
                job.updated_at = _now()
                session.commit()
        log_task(task_type="REGEN", event="complete", job_id=job_id, user_id=user_id, message=outcome)
    except Exception as err:  # noqa: BLE001 — surface failure on the job row
        log_task(task_type="REGEN", event="error", level=ERROR, job_id=job_id, user_id=user_id,
                 message=str(err), detail={"trace": traceback.format_exc()[:8000]})
        _fail_job(job_id, err)
    finally:
        progress_broker.publish(str(job_id))


def start_mcq_regen_job(job_id: uuid.UUID, run_id: uuid.UUID, outcome: str,
                        feedback: str, tags: list[str] | None, reviewer: str) -> threading.Thread:
    thread = threading.Thread(
        target=_run_mcq_regen_job,
        args=(job_id, run_id, outcome, feedback, tags, reviewer),
        daemon=True,
    )
    thread.start()
    return thread
