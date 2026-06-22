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

import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.session import get_session
from app.models import Course, McqRun, RagChunk, SyncJob, Topic, Unit, UnitPart
from app.schemas import (
    ApproveRunRequest,
    BuildRagRequest,
    ExtractRequest,
    McqGenerateRequest,
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
)
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
    start_sync_job,
)
from app.services.portal_sync import get_course_versions
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


@router.post("/courses/sync/", status_code=status.HTTP_202_ACCEPTED)
def start_sync(body: SyncRequest, session: Session = Depends(get_session)) -> dict:
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

    job = SyncJob(
        course_id=course_id,
        environment=environment,
        prerequisite_for=prerequisite_for,
        courseversion_id=courseversion_id,
        version_id=version_id,
        is_latest_version=is_latest,
    )
    session.add(job)
    session.commit()
    start_sync_job(job.id)
    return serialize_job(job)


@router.post("/courses/extract/", status_code=status.HTTP_202_ACCEPTED)
def extract_content(body: ExtractRequest, session: Session = Depends(get_session)) -> dict:
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

    job = SyncJob(course_id=course_id, job_type=SyncJob.EXTRACT)
    session.add(job)
    session.commit()
    start_extraction_job(job.id, tokens, unit_ids or None)
    return serialize_job(job)


@router.post("/courses/build-rag/", status_code=status.HTTP_202_ACCEPTED)
def build_rag(body: BuildRagRequest, session: Session = Depends(get_session)) -> dict:
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

    job = SyncJob(course_id=course_id, job_type=SyncJob.RAG)
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


# --------------------------------------------------------------------------- #
# MCQ generation (LangGraph pipeline) — static /courses/mcq/* before the catch-all
# --------------------------------------------------------------------------- #
@router.post("/courses/mcq/generate/", status_code=status.HTTP_202_ACCEPTED)
def generate_mcq(body: McqGenerateRequest, session: Session = Depends(get_session)) -> dict:
    """Start a background MCQ-generation run for a selected course/topic/session.
    `unit_id` is a reading-material part's portal unit_id within the session; the
    session must have extracted reading-material content (ingestion not required)."""
    course_id = (body.course_id or "").strip()
    unit_id = (body.unit_id or "").strip()
    if not course_id or not unit_id:
        raise HTTPException(status_code=400, detail="course_id and unit_id are required.")
    if session.get(Course, course_id) is None:
        raise HTTPException(status_code=404, detail="Course not found.")

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

    job = SyncJob(course_id=course_id, job_type=SyncJob.MCQ)
    session.add(job)
    session.commit()
    start_mcq_job(
        job.id, course_id, (body.topic_id or "").strip(), unit_id,
        bool(body.review), prereq_unit_ids,
    )
    return serialize_job(job)


@router.get("/courses/mcq/runs/")
def list_mcq_runs(
    course_id: str | None = None, unit_id: str | None = None, limit: int = 10,
    session: Session = Depends(get_session),
) -> list[dict]:
    """Recent MCQ runs (summaries, no full result), newest first; optionally scoped
    to a course/session."""
    stmt = select(McqRun).order_by(McqRun.created_at.desc()).limit(max(1, min(limit, 50)))
    if course_id:
        stmt = stmt.where(McqRun.course_id == course_id)
    if unit_id:
        stmt = stmt.where(McqRun.unit_id == unit_id)
    runs = session.scalars(stmt).all()
    return [serialize_mcq_run(r, include_result=False) for r in runs]


@router.get("/courses/mcq/runs/{run_id}/")
def get_mcq_run(run_id: uuid.UUID, session: Session = Depends(get_session)) -> dict:
    run = session.get(McqRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="MCQ run not found.")
    return serialize_mcq_run(run)


def _export_filename(run) -> str:
    label = (run.result or {}).get("session_label") or "questions"
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", label).strip("_") or "questions"
    return f"{safe}_MCQ_export.zip"


@router.post("/courses/mcq/runs/{run_id}/export-beta/")
def export_mcq_run_to_beta(run_id: uuid.UUID, session: Session = Depends(get_session)) -> dict:
    """Build the portal-format export ZIP for a run (in memory) and upload it to the
    BETA content-loading S3 bucket; return the public URL. Nothing is stored on the
    server — the ZIP is derived on demand from the run's stored questions."""
    from app.mcq_pipeline.portal_export import ExportValidationError, build_zip_bytes
    from app.services.beta_s3 import upload_bytes

    run = session.get(McqRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="MCQ run not found.")
    try:
        data, info = build_zip_bytes(run.result or {})
    except ExportValidationError as err:
        raise HTTPException(status_code=400, detail={"message": "Export validation failed",
                                                     "errors": err.errors}) from err
    if info["total_questions"] == 0:
        raise HTTPException(status_code=400, detail="This run has no generated questions to export.")
    filename = _export_filename(run)
    try:
        url = upload_bytes(data, filename)
    except Exception as err:  # noqa: BLE001 — surface the failing step to the caller
        raise HTTPException(status_code=502, detail=f"Beta S3 upload failed: {err}") from err
    return {"url": url, "filename": filename, "counts": info["counts"],
            "total": info["total_questions"], "batch_id": info["batch_id"]}


# --- Human-in-the-loop review (Gate B): feedback + regenerate + approve --------- #
@router.post("/courses/mcq/runs/{run_id}/questions/{outcome}/regenerate/")
def regenerate_mcq_question(run_id: uuid.UUID, outcome: str,
                            body: RegenerateQuestionRequest,
                            session: Session = Depends(get_session)) -> dict:
    """Regenerate one question for its LO with the reviewer's feedback injected,
    re-review it, persist (with revision history), and log the feedback. Synchronous
    (a few LLM calls) — the reviewer is actively waiting on the screen."""
    if session.get(McqRun, run_id) is None:
        raise HTTPException(status_code=404, detail="MCQ run not found.")
    if not (body.feedback or "").strip():
        raise HTTPException(status_code=400, detail="Feedback is required to regenerate.")
    from app.mcq_pipeline.review import regenerate_question
    try:
        question = regenerate_question(run_id, outcome, body.feedback,
                                       reviewer=body.reviewer, tags=body.tags)
    except ValueError as err:
        raise HTTPException(status_code=404, detail=str(err)) from err
    except Exception as err:  # noqa: BLE001 — surface generation failure
        raise HTTPException(status_code=502, detail=f"Regeneration failed: {err}") from err
    return {"question": question}


@router.post("/courses/mcq/runs/{run_id}/questions/{outcome}/feedback/")
def submit_mcq_feedback(run_id: uuid.UUID, outcome: str,
                        body: QuestionFeedbackRequest,
                        session: Session = Depends(get_session)) -> dict:
    if session.get(McqRun, run_id) is None:
        raise HTTPException(status_code=404, detail="MCQ run not found.")
    from app.mcq_pipeline.review import record_feedback
    return record_feedback(run_id, outcome, action=body.action, tags=body.tags,
                           comment=body.comment, reviewer=body.reviewer)


@router.post("/courses/mcq/runs/{run_id}/approve/")
def approve_mcq_run(run_id: uuid.UUID, body: ApproveRunRequest,
                    session: Session = Depends(get_session)) -> dict:
    if session.get(McqRun, run_id) is None:
        raise HTTPException(status_code=404, detail="MCQ run not found.")
    from app.mcq_pipeline.review import approve_run
    return approve_run(run_id, reviewer=body.reviewer)


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
    return serialize_course_detail(
        course,
        part_counts=part_counts,
        course_counts=course_counts,
        issue_counts=issue_counts,
    )
