"""
app/api/courses.py
------------------
FastAPI routes — a drop-in replacement for the Django/DRF ``courses`` API. Paths,
trailing slashes, status codes, and JSON shapes match the originals so the React
frontend works unchanged.

Route order matters: the static ``/courses/<x>/`` segments (versions, sync,
extract, build-rag) are declared BEFORE the ``/courses/{course_id}/`` catch-all so
they aren't captured as a course id.
"""

from __future__ import annotations

import asyncio
import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.session import SessionLocal, get_session
from app.api.deps import get_current_user, require_active
from app.models import BetaLoad, Course, McqRun, McqTrace, RagChunk, SyncJob, Topic, Unit, UnitPart, User
from app.services.task_log import ERROR, log_task
from app.schemas import (
    ALLOWED_QUESTION_DOMAINS,
    ApproveRunRequest,
    BuildRagRequest,
    CourseSettingsRequest,
    ExtractRequest,
    McqGenerateRequest,
    McqReviewRequest,
    PrepareSheetRequest,
    QuestionApprovalRequest,
    QuestionExcludeRequest,
    QuestionFeedbackRequest,
    RagAnswerRequest,
    RagCheckRequest,
    RagSearchRequest,
    RegenerateQuestionRequest,
    SyncRequest,
    VersionsRequest,
    serialize_course_detail,
    serialize_course_list,
    serialize_job,
    serialize_mcq_run,
    serialize_mcq_trace,
)
from app.services import progress_broker
from app.services.extraction import (
    READING_MATERIAL_LABEL,
    collect_courses_recursive,
    environments_needing_token,
    required_environments,
)
from app.services.jobs import (
    start_build_rag_job,
    start_extraction_job,
    start_mcq_job,
    start_mcq_resume_job,
    start_sync_job,
)
from app.services.portal_sync import get_course_versions, lookup_course_environments
from portal.constants import ENVIRONMENTS

router = APIRouter(prefix="/api")

# Reading-material content statuses that mean extraction ran but produced nothing
# usable — these are the units worth flagging for attention.
CONTENT_ISSUE_STATUSES = ("EMPTY", "ERROR")


def _content_issue_counts(session: Session, course_ids: list[str] | None = None) -> dict:
    """Per-course count of Reading Material parts (in SESSION units) whose
    extraction came back EMPTY or ERROR. Keyed by course_id; absent = 0."""
    stmt = (
        select(Topic.course_id, func.count())
        .select_from(UnitPart)
        .join(Unit, UnitPart.container_id == Unit.id)
        .join(Topic, Unit.topic_id == Topic.id)
        .where(
            Unit.kind == Unit.SESSION,
            UnitPart.label == READING_MATERIAL_LABEL,
            UnitPart.content_status.in_(CONTENT_ISSUE_STATUSES),
        )
        .group_by(Topic.course_id)
    )
    if course_ids is not None:
        stmt = stmt.where(Topic.course_id.in_(course_ids))
    return dict(session.execute(stmt).all())


def _clean_environment(value: str | None) -> str:
    """Normalize/validate an environment name; raise 400 if invalid."""
    env = (value or "PROD").strip().upper()
    if env not in ENVIRONMENTS:
        valid = ", ".join(ENVIRONMENTS)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid environment '{env}'. Valid: {valid}.",
        )
    return env


# --------------------------------------------------------------------------- #
# Static /courses/<x>/ routes — declared before /courses/{course_id}/
# --------------------------------------------------------------------------- #
@router.post("/courses/versions/")
def fetch_versions(body: VersionsRequest) -> dict:
    """Step 1: given a course_id, return its available versions for the popup."""
    course_id = (body.course_id or "").strip()
    if not course_id:
        raise HTTPException(status_code=400, detail="course_id is required.")
    environment = _clean_environment(body.environment)
    try:
        versions = get_course_versions(course_id, environment=environment)
    except Exception as err:  # noqa: BLE001 — portal failure surfaces as 502
        raise HTTPException(status_code=502, detail=f"Failed to fetch versions: {err}")
    return {"course_id": course_id, "environment": environment, "versions": versions}


@router.post("/courses/lookup/")
def lookup_course(body: VersionsRequest) -> dict:
    """Given a course_id, probe BOTH environments (PROD + BETA) in parallel and report what's
    present where — PROD versions, BETA presence (usually unversioned), and a 'not found' marker
    per environment. Lets the UI show cross-environment availability before the user picks one to
    sync. No comparison — the environments are reported independently."""
    course_id = (body.course_id or "").strip()
    if not course_id:
        raise HTTPException(status_code=400, detail="course_id is required.")
    try:
        environments = lookup_course_environments(course_id)
    except Exception as err:  # noqa: BLE001 — portal failure surfaces as 502
        raise HTTPException(status_code=502, detail=f"Lookup failed: {err}")
    return {"course_id": course_id, "environments": environments}


@router.post("/courses/sync/", status_code=status.HTTP_202_ACCEPTED)
def start_sync(body: SyncRequest, session: Session = Depends(get_session),
               user: User = Depends(require_active)) -> dict:
    """Step 2 / Sync: start a background fetch for a course + chosen version."""
    course_id = (body.course_id or "").strip()
    if not course_id:
        raise HTTPException(status_code=400, detail="course_id is required.")

    courseversion_id = (body.courseversion_id or "").strip()
    version_id = (body.version_id or "").strip()
    is_latest = bool(body.is_latest_version)

    environment = _clean_environment(body.environment)
    env_provided = bool(body.environment)
    prerequisite_for = (body.prerequisite_for or "").strip()

    existing = session.get(Course, course_id)

    # A course lives in exactly one environment — always sync from it unless the
    # caller explicitly overrides. This guards the re-sync path (Sync button),
    # which sends only course_id, from silently defaulting to PROD.
    if not env_provided and existing and existing.environment:
        environment = existing.environment

    # Reuse the course's stored version when the caller didn't choose one.
    if not courseversion_id and existing and existing.selected_courseversion_id:
        courseversion_id = existing.selected_courseversion_id
        version_id = existing.selected_version_id
        is_latest = existing.is_latest_version

    # Per-course MCQ domain, chosen at add time (e.g. "SQL"); applied to the Course when
    # this sync persists it. Empty = generic.
    domain = (body.question_domain or "").strip().upper()
    if domain not in ALLOWED_QUESTION_DOMAINS:
        raise HTTPException(status_code=400,
                            detail=f"question_domain must be one of {sorted(ALLOWED_QUESTION_DOMAINS)}")

    job = SyncJob(
        course_id=course_id,
        environment=environment,
        prerequisite_for=prerequisite_for,
        courseversion_id=courseversion_id,
        version_id=version_id,
        is_latest_version=is_latest,
        question_domain=domain,
        created_by=user.id,
    )
    session.add(job)
    session.commit()
    start_sync_job(job.id)
    return serialize_job(job)


@router.post("/courses/extract/", status_code=status.HTTP_202_ACCEPTED)
def extract_content(body: ExtractRequest, session: Session = Depends(get_session),
                    user: User = Depends(require_active)) -> dict:
    """Start a background reading-material extraction for a course + prerequisites.

    Tokens are OPTIONAL: with a Bearer token an environment is extracted via the
    learning API (full content incl. tutorials) and its resource ids are stored;
    without one, reading materials that already have stored resource ids are
    extracted token-free via the admin panel (cheat-sheet content). A token is
    only *required* for an environment that still has reading materials with no
    stored resource id. Tokens are used for this run only and never stored."""
    course_id = (body.course_id or "").strip()
    if not course_id:
        raise HTTPException(status_code=400, detail="course_id is required.")
    course = session.get(Course, course_id)
    if not course:
        raise HTTPException(status_code=404, detail="Course not found.")

    raw_tokens = body.tokens or {}
    if not isinstance(raw_tokens, dict):
        raise HTTPException(status_code=400, detail="tokens must be an object {ENV: token}.")
    tokens = {
        k.upper(): v.strip()
        for k, v in raw_tokens.items()
        if isinstance(v, str) and v.strip()
    }

    unit_ids = [u.strip() for u in (body.unit_ids or []) if isinstance(u, str) and u.strip()]

    needed = environments_needing_token(session, course, unit_ids or None)
    missing = [env for env in needed if env not in tokens]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=(
                f"A Bearer token is required for: {', '.join(missing)} — "
                "these have reading materials with no stored learning resource id yet."
            ),
        )

    job = SyncJob(course_id=course_id, job_type=SyncJob.EXTRACT, created_by=user.id)
    session.add(job)
    session.commit()
    start_extraction_job(job.id, tokens, unit_ids or None)
    return serialize_job(job)


@router.post("/courses/build-rag/", status_code=status.HTTP_202_ACCEPTED)
def build_rag(body: BuildRagRequest, session: Session = Depends(get_session),
              user: User = Depends(require_active)) -> dict:
    """Ingest extracted reading material (course + prerequisites, optionally limited
    to selected reading-material unit_ids) into the RAG vector store."""
    course_id = (body.course_id or "").strip()
    course = session.get(Course, course_id)
    if not course:
        raise HTTPException(status_code=404, detail="Course not found.")
    if not course.content_extracted_at:
        raise HTTPException(
            status_code=409, detail="Extract the learning resource content first."
        )

    job = SyncJob(course_id=course_id, job_type=SyncJob.RAG, created_by=user.id)
    session.add(job)
    session.commit()
    start_build_rag_job(job.id, body.unit_ids or None)
    return serialize_job(job)


# --------------------------------------------------------------------------- #
# RAG retrieval (scoped) — two static segments, no collision with {course_id}
# --------------------------------------------------------------------------- #
@router.post("/courses/rag/search/")
def rag_search(body: RagSearchRequest, session: Session = Depends(get_session)) -> dict:
    from app.services.rag_search import search

    if not body.course_ids:
        raise HTTPException(status_code=400, detail="course_ids is required.")
    hits = search(
        session,
        course_ids=body.course_ids,
        query=body.query,
        topic_ids=body.topic_ids,
        unit_ids=body.unit_ids,
        top_k=body.top_k,
    )
    return {"course_ids": body.course_ids, "query": body.query, "results": hits}


@router.post("/courses/rag/check-concept/")
def rag_check_concept(body: RagCheckRequest, session: Session = Depends(get_session)) -> dict:
    from app.services.rag_search import check_concept

    if not body.course_ids:
        raise HTTPException(status_code=400, detail="course_ids is required.")
    return check_concept(
        session, course_ids=body.course_ids, topic=body.topic, syntax=body.syntax
    )


@router.post("/courses/rag/answer/")
def rag_answer(body: RagAnswerRequest, session: Session = Depends(get_session)) -> dict:
    """Conversational RAG answer scoped to a course (+ its prerequisites). Retrieves
    the relevant sections and synthesizes a cited answer from them."""
    from app.services.rag_search import answer

    if not body.course_ids:
        raise HTTPException(status_code=400, detail="course_ids is required.")
    if not (body.query or "").strip():
        raise HTTPException(status_code=400, detail="query is required.")

    # Expand the scope to include each selected course's prerequisites.
    course_ids = list(body.course_ids)
    if body.include_prerequisites:
        seen = set(course_ids)
        for cid in body.course_ids:
            course = session.get(Course, cid)
            if course is None:
                continue
            for c in collect_courses_recursive(course):
                if c.course_id not in seen:
                    seen.add(c.course_id)
                    course_ids.append(c.course_id)

    top_k = max(1, min(body.top_k, 40))  # keep the chat context bounded
    return answer(session, course_ids=course_ids, query=body.query, top_k=top_k)


@router.get("/courses/jobs/{job_id}/")
def job_status(job_id: uuid.UUID, session: Session = Depends(get_session)) -> dict:
    job = session.get(SyncJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return serialize_job(job)


@router.get("/courses/mcq/jobs/{job_id}/trace/")
def mcq_trace(job_id: uuid.UUID, session: Session = Depends(get_session)) -> list[dict]:
    """Node-by-node execution trace for an MCQ run — our own tracing (replaces LangSmith).
    One row per node entry, ordered chronologically. job_id is the run's job id (= the LangGraph
    checkpoint thread_id); a run re-entered by the repair loop / HITL resume shows repeated nodes."""
    rows = session.scalars(
        select(McqTrace)
        .where(McqTrace.job_id == job_id)
        .order_by(McqTrace.started_at.asc(), McqTrace.seq.asc())
    ).all()
    return [serialize_mcq_trace(r) for r in rows]


# --------------------------------------------------------------------------- #
# MCQ generation (LangGraph pipeline) — static /courses/mcq/* before the catch-all
# --------------------------------------------------------------------------- #
@router.post("/courses/mcq/generate/", status_code=status.HTTP_202_ACCEPTED)
def generate_mcq(body: McqGenerateRequest, session: Session = Depends(get_session),
                 user: User = Depends(require_active)) -> dict:
    """Start a background MCQ-generation run for a selected course/topic/session.
    `unit_id` is a reading-material part's portal unit_id within the session; the
    session must have extracted reading-material content (ingestion not required)."""
    from app.services.user_keys import active_provider, user_has_active_key
    if not user_has_active_key(session, user.id):
        prov = active_provider(session)
        raise HTTPException(
            status_code=400,
            detail=f"Add your API key for the active connector "
                   f"'{prov.name if prov else ''}' (Account) before generating.")
    course_id = (body.course_id or "").strip()
    unit_id = (body.unit_id or "").strip()
    if not course_id or not unit_id:
        raise HTTPException(status_code=400, detail="course_id and unit_id are required.")
    course = session.get(Course, course_id)
    if course is None:
        raise HTTPException(status_code=404, detail="Course not found.")
    # Only the user who added (first synced) the course may generate for it. Admins
    # bypass; unowned (legacy) courses stay open.
    owner = getattr(course, "created_by", None)
    if owner is not None and owner != user.id and user.role != User.ROLE_ADMIN:
        raise HTTPException(
            status_code=403,
            detail="Only the user who added this course can generate MCQs for it.")

    # The selected session must have extracted reading-material content somewhere
    # in the unit that owns this part (mirrors the MCQ-page gate).
    part = session.scalar(
        select(UnitPart)
        .join(Unit, UnitPart.container_id == Unit.id)
        .join(Topic, Unit.topic_id == Topic.id)
        .where(Topic.course_id == course_id, UnitPart.unit_id == unit_id)
    )
    if part is None:
        raise HTTPException(status_code=404, detail="Session reading material not found.")
    has_content = any(
        p.label == READING_MATERIAL_LABEL and (p.content or "").strip()
        for p in part.container.parts
    )
    if not has_content:
        raise HTTPException(
            status_code=400,
            detail="This session has no extracted reading-material content — run Extract first.",
        )

    prereq_unit_ids = body.prerequisite_unit_ids
    if prereq_unit_ids is not None:
        prereq_unit_ids = [u.strip() for u in prereq_unit_ids if isinstance(u, str) and u.strip()]

    job = SyncJob(course_id=course_id, job_type=SyncJob.MCQ, created_by=user.id)
    session.add(job)
    session.commit()
    start_mcq_job(
        job.id, course_id, (body.topic_id or "").strip(), unit_id,
        bool(body.review), prereq_unit_ids,
        question_budget=body.question_budget, hitl_enabled=bool(body.hitl),
    )
    return serialize_job(job)


@router.post("/courses/mcq/jobs/{job_id}/resume/", status_code=status.HTTP_202_ACCEPTED)
def resume_mcq(job_id: uuid.UUID, body: McqReviewRequest,
               session: Session = Depends(get_session),
               user: User = Depends(require_active)) -> dict:
    """Resume a HITL-paused MCQ run after a human decision at a gate (approve / reject). The job
    must be AWAITING_REVIEW. Re-runs the pipeline from its checkpoint in the background; it may
    pause again at the next gate or complete. The run context (course/unit/etc.) is needed to
    rebuild the run-scoped RAG adapter — the job_id is the checkpoint thread_id."""
    job = session.get(SyncJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.status != SyncJob.AWAITING_REVIEW:
        raise HTTPException(status_code=409, detail="Job is not awaiting review.")
    course_id = (body.course_id or job.course_id or "").strip()
    unit_id = (body.unit_id or "").strip()
    if not course_id or not unit_id:
        raise HTTPException(status_code=400, detail="course_id and unit_id are required to resume.")
    action = (body.action or "approve").strip().lower()
    if action not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail="action must be 'approve' or 'reject'.")
    decision = {"action": action, "rejected": body.rejected or [],
                "rejected_ids": body.rejected_ids or [], "note": body.note or "",
                "reviewer": _reviewer_name(user)}
    prereq_unit_ids = body.prerequisite_unit_ids
    if prereq_unit_ids is not None:
        prereq_unit_ids = [u.strip() for u in prereq_unit_ids if isinstance(u, str) and u.strip()]
    job.status = SyncJob.RUNNING
    job.message = f"Resuming after {action}…"
    session.commit()
    start_mcq_resume_job(
        job.id, course_id, (body.topic_id or "").strip(), unit_id,
        decision, prereq_unit_ids, body.question_budget, bool(body.review),
    )
    return serialize_job(job)


# --- live job progress over WebSocket (replaces the frontend poll) -------------- #
_SETTLED_STATUSES = {SyncJob.SUCCESS, SyncJob.FAILURE, SyncJob.AWAITING_REVIEW}


def _read_serialized_job(job_id: uuid.UUID) -> dict | None:
    with SessionLocal() as session:
        job = session.get(SyncJob, job_id)
        return serialize_job(job) if job is not None else None


@router.websocket("/courses/mcq/jobs/{job_id}/ws")
async def mcq_job_ws(websocket: WebSocket, job_id: uuid.UUID) -> None:
    """Stream a job's serialized state live (replaces the ~1s poll). Pushes on every change and
    closes when the job SETTLES (SUCCESS / FAILURE / AWAITING_REVIEW); the client re-opens after a
    HITL resume. Backed by the in-process progress_broker (single uvicorn process). Messages:
    {"type":"job","data":<serialized job>} · {"type":"ping"} · {"type":"error","detail":...}."""
    await websocket.accept()
    jid = str(job_id)
    q = progress_broker.subscribe(jid)            # subscribe BEFORE the first read (no missed change)
    try:
        job = await asyncio.to_thread(_read_serialized_job, job_id)
        if job is None:
            await websocket.send_json({"type": "error", "detail": "job not found"})
            return
        await websocket.send_json({"type": "job", "data": jsonable_encoder(job)})
        if job["status"] in _SETTLED_STATUSES:
            return
        while True:
            try:
                await asyncio.wait_for(q.get(), timeout=20)
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "ping"})   # keepalive + dead-connection probe
                continue
            job = await asyncio.to_thread(_read_serialized_job, job_id)
            if job is None:
                break
            await websocket.send_json({"type": "job", "data": jsonable_encoder(job)})
            if job["status"] in _SETTLED_STATUSES:
                break
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001 — a socket error must never crash the worker
        pass
    finally:
        progress_broker.unsubscribe(jid, q)
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass


@router.get("/courses/mcq/runs/")
def list_mcq_runs(
    course_id: str | None = None, unit_id: str | None = None, limit: int = 10,
    session: Session = Depends(get_session),
    user: User = Depends(require_active),
) -> list[dict]:
    """Recent MCQ runs (summaries, no full result), newest first; optionally scoped
    to a course/session. Scoped to the current user's own runs (admins see all)."""
    stmt = select(McqRun).order_by(McqRun.created_at.desc()).limit(max(1, min(limit, 50)))
    if user.role != User.ROLE_ADMIN:
        stmt = stmt.where(McqRun.created_by == user.id)
    if course_id:
        stmt = stmt.where(McqRun.course_id == course_id)
    if unit_id:
        stmt = stmt.where(McqRun.unit_id == unit_id)
    runs = session.scalars(stmt).all()
    return [serialize_mcq_run(r, include_result=False) for r in runs]


@router.get("/courses/mcq/runs/{run_id}/")
def get_mcq_run(run_id: uuid.UUID, session: Session = Depends(get_session),
                user: User = Depends(require_active)) -> dict:
    run = session.get(McqRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="MCQ run not found.")
    # A user may only open their own runs (admins see all). 404 (not 403) so a run's
    # existence isn't leaked across users.
    if (run.created_by is not None and run.created_by != user.id
            and user.role != User.ROLE_ADMIN):
        raise HTTPException(status_code=404, detail="MCQ run not found.")
    return serialize_mcq_run(run)


def _export_filename(run) -> str:
    label = (run.result or {}).get("session_label") or "questions"
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", label).strip("_") or "questions"
    return f"{safe}_MCQ_export.zip"


def _require_reviewed(run) -> None:
    """Generate ZIP / Prepare & Load are FROZEN until the run is marked reviewed
    (which itself requires every question approved or excluded). 409 otherwise."""
    if (run.review_status or "") != "approved":
        raise HTTPException(
            status_code=409,
            detail="Mark the run reviewed before exporting or loading "
                   "(approve or exclude every question, then 'Mark run reviewed').")


def _result_for_load(run, approved_only: bool) -> dict:
    """Gate loading on human approval and return the result payload to export.

    `approved_only=False` (the default "Load all") requires EVERY generated question to be
    approved. `approved_only=True` ("Load approved only") loads just the approved subset and
    only requires at least one. Raises 409 when the gate isn't met."""
    result = run.result or {}
    # Excluded questions stay in the run but are never loaded.
    eligible = [q for q in (result.get("questions") or [])
                if q.get("status") == "generated" and not q.get("excluded")]
    approved = [q for q in eligible if q.get("approval") == "approved"]
    if approved_only:
        if not approved:
            raise HTTPException(status_code=409,
                detail="No questions are approved yet — approve at least one to load.")
        return {**result, "questions": approved}
    if not eligible or len(approved) != len(eligible):
        raise HTTPException(status_code=409,
            detail=f"Approve all {len(eligible)} questions before loading, "
                   f"or use 'Load approved only' ({len(approved)} approved).")
    return result


@router.post("/courses/mcq/runs/{run_id}/export-beta/")
def export_mcq_run_to_beta(run_id: uuid.UUID, approved_only: bool = False,
                           session: Session = Depends(get_session),
                           user: User = Depends(require_active)) -> dict:
    """Build the portal-format export ZIP for a run (in memory) and upload it to the
    BETA content-loading S3 bucket; return the public URL. Nothing is stored on the
    server — the ZIP is derived on demand from the run's stored questions.

    Gated on human approval: by default every question must be approved; pass
    `approved_only=true` to export just the approved subset."""
    from app.mcq_pipeline.portal_export import ExportValidationError, build_zip_bytes
    from app.services.beta_s3 import upload_bytes

    run = session.get(McqRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="MCQ run not found.")
    _require_reviewed(run)
    payload = _result_for_load(run, approved_only)
    try:
        data, info = build_zip_bytes(payload)
    except ExportValidationError as err:
        raise HTTPException(status_code=400, detail={"message": "Export validation failed",
                                                     "errors": err.errors}) from err
    if info["total_questions"] == 0:
        raise HTTPException(status_code=400, detail="This run has no generated questions to export.")
    filename = _export_filename(run)
    try:
        url = upload_bytes(data, filename)
    except Exception as err:  # noqa: BLE001 — surface the failing step to the caller
        log_task(task_type="EXPORT", event="error", level=ERROR, run_id=run_id, user_id=user.id,
                 message=f"Beta S3 upload failed: {err}")
        raise HTTPException(status_code=502, detail=f"Beta S3 upload failed: {err}") from err
    session.add(BetaLoad(run_id=run_id, user_id=user.id, action="export", status="SUCCESS",
                         s3_url=url, message=filename))
    session.commit()
    log_task(task_type="EXPORT", event="complete", run_id=run_id, user_id=user.id,
             message=f"{info['total_questions']} question(s) → {filename}")
    return {"url": url, "filename": filename, "counts": info["counts"],
            "total": info["total_questions"], "batch_id": info["batch_id"]}


@router.post("/courses/mcq/runs/{run_id}/prepare-and-load/")
def prepare_and_load_mcq_run(run_id: uuid.UUID, body: PrepareSheetRequest,
                             session: Session = Depends(get_session),
                             user: User = Depends(require_active)) -> dict:
    """Full beta-load pipeline for a run: build + upload the questions ZIP, copy the
    exam-config sheet template and fill its Form tab, submit the SHEET_LOADING task,
    poll it to completion, and (on success) unlock the resource.

    The parent (Form!B14) is the run's topic_id; the question count and name are
    derived; the five fields in `body` are the only reviewer-supplied values."""
    from app.mcq_pipeline.portal_export import ExportValidationError, build_zip_bytes
    from app.services import beta_s3, beta_sheet

    run = session.get(McqRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="MCQ run not found.")
    _require_reviewed(run)
    if not run.topic_id:
        raise HTTPException(status_code=400,
                            detail="This run has no topic_id, so the exam's parent "
                                   "resource cannot be set. Re-run with a topic selected.")

    # One id per unit, shared by the exam (Form!B5) AND the questions JSON filename in
    # the ZIP, so the loader can match the questions file to the exam unit.
    resource_id = str(uuid.uuid4())

    # Gate on human approval (all approved, or just the approved subset when approved_only).
    payload = _result_for_load(run, body.approved_only)

    # 1. Build the portal-format questions ZIP (in memory), named <resource_id>.json.
    try:
        data, info = build_zip_bytes(payload, batch_id=resource_id)
    except ExportValidationError as err:
        raise HTTPException(status_code=400, detail={"message": "Export validation failed",
                                                     "errors": err.errors}) from err
    if info["total_questions"] == 0:
        raise HTTPException(status_code=400, detail="This run has no generated questions to load.")

    # 2. Upload the ZIP to the beta S3 bucket.
    filename = _export_filename(run)
    try:
        s3_url = beta_s3.upload_bytes(data, filename)
    except Exception as err:  # noqa: BLE001
        log_task(task_type="LOAD", event="error", level=ERROR, run_id=run_id, user_id=user.id,
                 message=f"Beta S3 upload failed: {err}")
        raise HTTPException(status_code=502, detail=f"Beta S3 upload failed: {err}") from err

    # 3. Copy the template + fill the Form tab.
    try:
        sheet = beta_sheet.prepare_sheet(
            resource_id=resource_id,
            topic_id=run.topic_id,
            num_questions=info["total_questions"],
            child_order=body.child_order,
            duration_min=body.duration_min,
            pass_percentage=body.pass_percentage / 100.0,
            show_answer_scoring_mode=body.show_answer_scoring_mode,
            should_send_solutions=body.should_send_solutions,
            share_emails=[(body.reviewer_email or "").strip() or user.email],
        )
    except Exception as err:  # noqa: BLE001
        log_task(task_type="LOAD", event="error", level=ERROR, run_id=run_id, user_id=user.id,
                 message=f"Sheet preparation failed: {err}")
        raise HTTPException(status_code=502, detail=f"Sheet preparation failed: {err}") from err

    # 4. Submit the sheet-loading task.
    try:
        request_id = beta_s3.submit_sheet_loading(
            spreadsheet_id=sheet["spreadsheet_id"],
            spread_sheet_name=sheet["title"], s3_url=s3_url)
    except Exception as err:  # noqa: BLE001
        log_task(task_type="LOAD", event="error", level=ERROR, run_id=run_id, user_id=user.id,
                 message=f"Sheet-loading submit failed: {err}", detail={"sheet_url": sheet["url"]})
        raise HTTPException(status_code=502,
                            detail={"message": f"Sheet-loading submit failed: {err}",
                                    "sheet_url": sheet["url"]}) from err

    # 5. Poll to completion, then 6. unlock on success.
    status, message = beta_s3.poll_task(request_id)
    unlock_id = None
    if status == "SUCCESS":
        try:
            unlock_id = beta_s3.submit_unlock(sheet["resource_id"])
        except Exception as err:  # noqa: BLE001
            message = f"Loaded, but unlock failed: {err}"

    # Audit the load action (per-user) + log the outcome.
    session.add(BetaLoad(run_id=run_id, user_id=user.id, action="load", status=status,
                         resource_id=sheet["resource_id"], sheet_url=sheet["url"],
                         s3_url=s3_url, request_id=request_id, message=message))
    session.commit()
    log_task(task_type="LOAD", event="complete", run_id=run_id, user_id=user.id,
             level=(ERROR if status == "FAILURE" else "INFO"),
             message=f"status={status} resource={sheet['resource_id']} {message}".strip())

    return {
        "status": status, "message": message,
        "sheet_url": sheet["url"], "spreadsheet_id": sheet["spreadsheet_id"],
        "resource_id": sheet["resource_id"], "s3_url": s3_url,
        "request_id": request_id, "unlock_id": unlock_id,
        "total": info["total_questions"], "filename": filename,
    }


# --- Human-in-the-loop review (Gate B): feedback + regenerate + approve --------- #
def _reviewer_name(user: User) -> str:
    """Attribution for a review action — taken from the authenticated user (we no
    longer ask the reviewer to type their name). Prefer the display name, fall back
    to the email so it's never blank."""
    return (user.name or "").strip() or user.email


@router.post("/courses/mcq/runs/{run_id}/questions/{outcome}/regenerate/")
def regenerate_mcq_question(run_id: uuid.UUID, outcome: str,
                            body: RegenerateQuestionRequest,
                            session: Session = Depends(get_session),
                            user: User = Depends(require_active)) -> dict:
    """Regenerate one question for its LO with the reviewer's feedback injected,
    re-review it, persist (with revision history), and log the feedback. Synchronous
    (a few LLM calls) — the reviewer is actively waiting on the screen. The reviewer
    is taken from the authenticated user, not the request body."""
    if session.get(McqRun, run_id) is None:
        raise HTTPException(status_code=404, detail="MCQ run not found.")
    if not (body.feedback or "").strip():
        raise HTTPException(status_code=400, detail="Feedback is required to regenerate.")
    from app.mcq_pipeline.review import regenerate_question
    try:
        question = regenerate_question(run_id, outcome, body.feedback,
                                       reviewer=_reviewer_name(user), tags=body.tags)
    except ValueError as err:
        raise HTTPException(status_code=404, detail=str(err)) from err
    except Exception as err:  # noqa: BLE001 — surface generation failure
        raise HTTPException(status_code=502, detail=f"Regeneration failed: {err}") from err
    return {"question": question}


@router.post("/courses/mcq/runs/{run_id}/questions/{outcome}/feedback/")
def submit_mcq_feedback(run_id: uuid.UUID, outcome: str,
                        body: QuestionFeedbackRequest,
                        session: Session = Depends(get_session),
                        user: User = Depends(require_active)) -> dict:
    if session.get(McqRun, run_id) is None:
        raise HTTPException(status_code=404, detail="MCQ run not found.")
    from app.mcq_pipeline.review import record_feedback
    return record_feedback(run_id, outcome, action=body.action, tags=body.tags,
                           comment=body.comment, reviewer=_reviewer_name(user))


@router.post("/courses/mcq/runs/{run_id}/questions/{outcome}/approval/")
def set_mcq_question_approval(run_id: uuid.UUID, outcome: str,
                             body: QuestionApprovalRequest,
                             session: Session = Depends(get_session),
                             user: User = Depends(require_active)) -> dict:
    """Set a human approval decision (approved / rejected / pending) on one question.
    Drives the per-question count that gates loading."""
    if session.get(McqRun, run_id) is None:
        raise HTTPException(status_code=404, detail="MCQ run not found.")
    from app.mcq_pipeline.review import set_question_approval
    try:
        return set_question_approval(run_id, outcome, body.approval, reviewer=_reviewer_name(user))
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err


@router.post("/courses/mcq/runs/{run_id}/questions/{outcome}/exclude/")
def set_mcq_question_exclusion(run_id: uuid.UUID, outcome: str,
                              body: QuestionExcludeRequest,
                              session: Session = Depends(get_session),
                              user: User = Depends(require_active)) -> dict:
    """Exclude a question from export/load (or include it again). It stays in the run,
    shaded out, but drops from the approval tally and is never loaded."""
    if session.get(McqRun, run_id) is None:
        raise HTTPException(status_code=404, detail="MCQ run not found.")
    from app.mcq_pipeline.review import set_question_exclusion
    try:
        return set_question_exclusion(run_id, outcome, body.excluded,
                                      reviewer=_reviewer_name(user))
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err


@router.post("/courses/mcq/runs/{run_id}/approve/")
def approve_mcq_run(run_id: uuid.UUID, body: ApproveRunRequest,
                    session: Session = Depends(get_session),
                    user: User = Depends(require_active)) -> dict:
    run = session.get(McqRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="MCQ run not found.")
    # A run may be marked reviewed only when EVERY generated question is resolved —
    # approved or excluded (no pending). This gates Generate ZIP / Prepare & Load.
    generated = [q for q in (run.result or {}).get("questions") or [] if q.get("status") == "generated"]
    pending = [q for q in generated if not q.get("excluded") and q.get("approval") != "approved"]
    if not generated:
        raise HTTPException(status_code=409, detail="This run has no generated questions to review.")
    if pending:
        raise HTTPException(
            status_code=409,
            detail=f"{len(pending)} of {len(generated)} questions are still pending — approve or "
                   f"exclude every question before marking the run reviewed.")
    from app.mcq_pipeline.review import approve_run
    return approve_run(run_id, reviewer=_reviewer_name(user))


@router.get("/courses/mcq/feedback/insights/")
def mcq_feedback_insights() -> dict:
    from app.mcq_pipeline.review import feedback_insights
    return feedback_insights()


@router.get("/courses/{course_id}/extract-info/")
def extract_info(course_id: str, session: Session = Depends(get_session)) -> dict:
    """Environments a course + its prerequisites span, and which of them still
    REQUIRE a Bearer token (the rest can extract token-free via the admin panel
    using stored resource ids)."""
    course = session.get(Course, course_id)
    if not course:
        raise HTTPException(status_code=404, detail="Course not found.")
    return {
        "course_id": course_id,
        "environments": required_environments(course),
        "token_required": environments_needing_token(session, course),
    }



@router.get("/courses/{course_id}/units/{unit_id}/content/")
def get_unit_content(course_id: str, unit_id: str, session: Session = Depends(get_session)) -> dict:
    """Get the reading material content for a unit (session).
    Returns { title, content, content_chars }."""
    stmt = select(UnitPart).where(
        UnitPart.unit_id == unit_id,
        UnitPart.label == READING_MATERIAL_LABEL,
    )
    part = session.scalars(stmt).first()
    if not part or not part.content:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Reading material not found or not extracted.",
        )
    return {
        "title": part.name or "Reading Material",
        "content": part.content,
        "content_chars": len(part.content or ""),
    }

# --------------------------------------------------------------------------- #
# Listing / detail — declared last
# --------------------------------------------------------------------------- #
@router.get("/courses/")
def list_courses(session: Session = Depends(get_session)) -> list[dict]:
    # Only top-level courses — those that are not a prerequisite of another course.
    courses = session.scalars(select(Course).order_by(Course.course_name)).all()
    top_level = [c for c in courses if not c.required_by]
    # One grouped query gives the ingested chunk count per course (0 = not ingested).
    counts = dict(
        session.execute(
            select(RagChunk.course_id, func.count()).group_by(RagChunk.course_id)
        ).all()
    )
    issue_counts = _content_issue_counts(session)
    return [
        serialize_course_list(
            c,
            ingested_chunk_count=counts.get(c.course_id, 0),
            content_issue_count=issue_counts.get(c.course_id, 0),
        )
        for c in top_level
    ]


@router.get("/courses/{course_id}/")
def course_detail(course_id: str, session: Session = Depends(get_session)) -> dict:
    course = session.get(Course, course_id)
    if course is None:
        raise HTTPException(status_code=404, detail="Course not found.")

    # Per-course chunk counts for this course + its (nested) prerequisites, and
    # per-part counts for this course's reading materials — both derived from the
    # RAG store so the UI can mark exactly what's ingested.
    scope_ids = [course.course_id] + [p.course_id for p in course.prerequisites]
    course_counts = dict(
        session.execute(
            select(RagChunk.course_id, func.count())
            .where(RagChunk.course_id.in_(scope_ids))
            .group_by(RagChunk.course_id)
        ).all()
    )
    part_counts = dict(
        session.execute(
            select(RagChunk.unit_part_id, func.count())
            .where(RagChunk.course_id == course.course_id)
            .group_by(RagChunk.unit_part_id)
        ).all()
    )
    issue_counts = _content_issue_counts(session, scope_ids)
    # "Stale ingest" = a part whose content was extracted AFTER its chunks were built
    # (i.e. modified since it was last ingested) — those should still be offered for
    # re-ingestion, while up-to-date ingested parts are hidden.
    min_chunk_at = dict(
        session.execute(
            select(RagChunk.unit_part_id, func.min(RagChunk.created_at))
            .where(RagChunk.course_id == course.course_id)
            .group_by(RagChunk.unit_part_id)
        ).all()
    )
    part_extracted = dict(
        session.execute(
            select(UnitPart.id, UnitPart.content_extracted_at)
            .join(Unit, UnitPart.container_id == Unit.id)
            .join(Topic, Unit.topic_id == Topic.id)
            .where(Topic.course_id == course.course_id)
        ).all()
    )
    stale_part_ids = {
        pid for pid, ext in part_extracted.items()
        if ext is not None and min_chunk_at.get(pid) is not None and ext > min_chunk_at[pid]
    }
    return serialize_course_detail(
        course,
        part_counts=part_counts,
        course_counts=course_counts,
        issue_counts=issue_counts,
        stale_part_ids=stale_part_ids,
    )


@router.patch("/courses/{course_id}/settings/")
def update_course_settings(course_id: str, body: CourseSettingsRequest,
                           session: Session = Depends(get_session),
                           user: User = Depends(require_active)) -> dict:
    """Set per-course MCQ-generation settings. Currently `question_domain` (e.g. "SQL"),
    which deterministically activates that domain's generation/review rules for every
    run of this course — no per-outcome guessing. Empty string resets to generic."""
    course = session.get(Course, course_id)
    if course is None:
        raise HTTPException(status_code=404, detail="Course not found.")
    course.question_domain = body.question_domain   # already normalized/validated
    session.commit()
    session.refresh(course)
    return {"course_id": course.course_id, "question_domain": course.question_domain}
