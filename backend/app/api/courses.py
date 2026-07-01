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
import uuid

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db.session import SessionLocal, get_session
from app.api.deps import get_current_user, require_active
from app.models import (
    BetaLoad,
    ClassroomQuizDeck,
    ClassroomQuizScope,
    Course,
    CourseCollaborator,
    McqRun,
    McqTrace,
    RagChunk,
    SyncJob,
    Topic,
    Unit,
    UnitPart,
    User,
)
from app.schemas import (
    ALLOWED_QUESTION_DOMAINS,
    ApproveRunRequest,
    BuildRagRequest,
    ClassroomQuizIngestRequest,
    CollaboratorRequest,
    CourseSettingsRequest,
    ExecuteCodeRequest,
    ExtractRequest,
    McqGenerateRequest,
    McqReviewRequest,
    serialize_cq_deck,
    serialize_cq_scope,
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
    serialize_user,
)
from app.services import progress_broker
from app.services.extraction import (
    READING_MATERIAL_LABEL,
    collect_courses_recursive,
    environments_needing_token,
    required_environments,
)
from app.services.beta_load import (
    export_filename as _export_filename,
    require_reviewed as _require_reviewed,
    result_for_load as _result_for_load,
)
from app.services.jobs import (
    request_cancel,
    start_build_rag_job,
    start_export_job,
    start_extraction_job,
    start_load_job,
    start_cq_scope_job,
    start_cq_resume_job,
    start_cq_variants_job,
    start_mcq_job,
    start_mcq_regen_job,
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


# --------------------------------------------------------------------------- #
# Course access control — who may work on (generate content for) a course.
# Access = the owner (Course.created_by) OR a collaborator (course_collaborators)
# OR an admin. Owners and admins manage collaborators, and grants take effect
# immediately (no approval step). Unowned/legacy courses (created_by is None)
# stay open to everyone.
# --------------------------------------------------------------------------- #
def _user_can_access_course(session: Session, course_id: str, user: User) -> bool:
    if user.role == User.ROLE_ADMIN:
        return True
    course = session.get(Course, course_id)
    if course is None:
        return True  # nothing to gate (e.g. a run whose course row no longer exists)
    owner = getattr(course, "created_by", None)
    if owner is None or owner == user.id:
        return True
    return bool(session.scalar(
        select(func.count()).select_from(CourseCollaborator).where(
            CourseCollaborator.course_id == course_id,
            CourseCollaborator.user_id == user.id,
        )
    ))


def _require_course_access(session: Session, course_id: str, user: User) -> None:
    """403 unless the user owns the course, is a collaborator on it, or is an admin."""
    if not _user_can_access_course(session, course_id, user):
        raise HTTPException(
            status_code=403,
            detail="You don't have access to this course. Ask its owner or an admin to add you.",
        )


def _require_course_manage(course: Course, user: User) -> None:
    """Only the course owner or an admin may add/remove collaborators."""
    owner = getattr(course, "created_by", None)
    if user.role != User.ROLE_ADMIN and (owner is None or owner != user.id):
        raise HTTPException(
            status_code=403,
            detail="Only the course owner or an admin can manage access to it.",
        )


@router.post("/courses/extract/", status_code=status.HTTP_202_ACCEPTED)
def extract_content(body: ExtractRequest, session: Session = Depends(get_session),
                    user: User = Depends(require_active)) -> dict:
    """Start a background reading-material extraction for a course + prerequisites.

    Tokens are OPTIONAL (and never required): by default each course's individual
    learning_resource ids are discovered token-free from the content-loading admin
    (GET_UNIT_RESOURCE_DETAILS → CSV) and the content is scraped via the admin
    panel. A supplied Bearer token still takes precedence for its environment
    (learning API, tutorial-aware). Tokens are used for this run only, never stored."""
    course_id = (body.course_id or "").strip()
    if not course_id:
        raise HTTPException(status_code=400, detail="course_id is required.")
    course = session.get(Course, course_id)
    if not course:
        raise HTTPException(status_code=404, detail="Course not found.")
    _require_course_access(session, course_id, user)

    raw_tokens = body.tokens or {}
    if not isinstance(raw_tokens, dict):
        raise HTTPException(status_code=400, detail="tokens must be an object {ENV: token}.")
    tokens = {
        k.upper(): v.strip()
        for k, v in raw_tokens.items()
        if isinstance(v, str) and v.strip()
    }

    unit_ids = [u.strip() for u in (body.unit_ids or []) if isinstance(u, str) and u.strip()]

    # Tokens are no longer required: reading materials without stored resource ids
    # are discovered token-free from the content-loading admin's CSV
    # (GET_UNIT_RESOURCE_DETAILS) at extraction time. A supplied token still takes
    # precedence (learning API, tutorial-aware) for its environment.

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
    _require_course_access(session, course_id, user)
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
# Course collaborators — the owner or an admin grants other users access to work
# on a course. Grants are immediate (no approval). Two static segments after the
# course id, so they don't collide with the `/courses/{course_id}/` catch-all.
# --------------------------------------------------------------------------- #
@router.get("/courses/{course_id}/collaborators/")
def list_course_collaborators(course_id: str, session: Session = Depends(get_session),
                              user: User = Depends(require_active)) -> dict:
    """Who can work on this course: its owner plus any granted collaborators. Visible to
    the owner, a collaborator, or an admin."""
    course = session.get(Course, course_id)
    if course is None:
        raise HTTPException(status_code=404, detail="Course not found.")
    _require_course_access(session, course_id, user)
    owner = session.get(User, course.created_by) if course.created_by else None
    rows = session.scalars(
        select(CourseCollaborator).where(CourseCollaborator.course_id == course_id)
    ).all()
    collaborators = []
    for r in rows:
        u = session.get(User, r.user_id)
        if u is not None:
            collaborators.append({**serialize_user(u), "granted_at": r.created_at})
    can_manage = (user.role == User.ROLE_ADMIN
                  or (course.created_by is not None and course.created_by == user.id))
    return {
        "course_id": course_id,
        "owner": serialize_user(owner) if owner else None,
        "collaborators": collaborators,
        "can_manage": can_manage,
    }


@router.post("/courses/{course_id}/collaborators/", status_code=status.HTTP_201_CREATED)
def add_course_collaborator(course_id: str, body: CollaboratorRequest,
                            session: Session = Depends(get_session),
                            user: User = Depends(require_active)) -> dict:
    """Grant a user (by email) access to work on this course. Allowed for the course owner
    or an admin; the grant is immediate, with no approval step. Admins can grant on any course."""
    course = session.get(Course, course_id)
    if course is None:
        raise HTTPException(status_code=404, detail="Course not found.")
    _require_course_manage(course, user)
    email = (body.email or "").strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="A user email is required.")
    target = session.scalar(select(User).where(func.lower(User.email) == email))
    if target is None:
        raise HTTPException(status_code=404, detail=f"No user found with email “{email}”.")
    if course.created_by is not None and course.created_by == target.id:
        raise HTTPException(status_code=400, detail="That user already owns this course.")
    existing = session.scalar(
        select(CourseCollaborator).where(
            CourseCollaborator.course_id == course_id,
            CourseCollaborator.user_id == target.id,
        )
    )
    if existing is None:
        session.add(CourseCollaborator(
            course_id=course_id, user_id=target.id, granted_by=user.id))
        session.commit()
    return serialize_user(target)


@router.delete("/courses/{course_id}/collaborators/{user_id}/",
               status_code=status.HTTP_204_NO_CONTENT)
def remove_course_collaborator(course_id: str, user_id: uuid.UUID,
                               session: Session = Depends(get_session),
                               user: User = Depends(require_active)) -> None:
    """Revoke a collaborator's access. Allowed for the course owner or an admin."""
    course = session.get(Course, course_id)
    if course is None:
        raise HTTPException(status_code=404, detail="Course not found.")
    _require_course_manage(course, user)
    row = session.scalar(
        select(CourseCollaborator).where(
            CourseCollaborator.course_id == course_id,
            CourseCollaborator.user_id == user_id,
        )
    )
    if row is not None:
        session.delete(row)
        session.commit()


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


@router.post("/courses/mcq/execute/")
def execute_code(body: ExecuteCodeRequest,
                 user: User = Depends(require_active)) -> dict:
    """Run a candidate program (and optionally check stdout against an expected output)
    using the SAME sandboxed runner that grades FIBs. Powers the reviewer's FIB
    'Run & Check' and code-analysis 'Run code' buttons. Authenticated; runs grounded
    LLM-authored code with CPU/file-size rlimits and a wall-clock timeout."""
    from app.mcq_pipeline.utils import code_exec

    if not (body.code or "").strip():
        raise HTTPException(status_code=400, detail="code is required.")
    if not code_exec.language_supported(body.language):
        return {"supported": False, "ran": False,
                "stderr": f"language {body.language!r} is not executable here", "language": body.language}
    if body.expected_output is not None:
        res = code_exec.verify_output(body.language, body.code, body.stdin or "", body.expected_output)
        res["expected"] = body.expected_output
    else:
        res = code_exec.run_code(body.language, body.code, body.stdin or "")
    res["language"] = body.language
    return res


@router.get("/courses/jobs/")
def list_jobs(active: bool = False, limit: int = 50,
              session: Session = Depends(get_session),
              user: User = Depends(require_active)) -> list[dict]:
    """List the caller's recent jobs (admins see everyone's), newest first. With
    `active=true`, only unsettled jobs (PENDING/RUNNING/AWAITING_REVIEW) — used so every
    browser tab can show the same in-flight Activity, not just the tab that started a job."""
    q = select(SyncJob).order_by(SyncJob.updated_at.desc())
    if user.role != User.ROLE_ADMIN:
        q = q.where(SyncJob.created_by == user.id)
    if active:
        q = q.where(SyncJob.status.in_(
            [SyncJob.PENDING, SyncJob.RUNNING, SyncJob.AWAITING_REVIEW]))
    rows = session.scalars(q.limit(max(1, min(limit, 200)))).all()
    return [serialize_job(r) for r in rows]


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
    # Only the course owner (first syncer), a collaborator they (or an admin) added,
    # or an admin may generate for it. Unowned (legacy) courses stay open.
    _require_course_access(session, course_id, user)

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

    # Regenerating an existing session (a prior run exists for this course/session) requires
    # a reason — captured for the analytics' session-level regeneration view. First-time
    # generation needs none.
    prior_runs = session.scalar(
        select(func.count()).select_from(McqRun)
        .where(McqRun.course_id == course_id, McqRun.unit_id == unit_id)
    ) or 0
    reason = (body.reason or "").strip()
    if prior_runs > 0 and not reason:
        raise HTTPException(
            status_code=400,
            detail="A reason is required to regenerate a session's MCQs.")

    # Stash the run's selection context on the job so the Activity drawer can reopen it
    # to the exact page/stage later (the job row alone doesn't carry topic/unit).
    job = SyncJob(
        course_id=course_id, job_type=SyncJob.MCQ, created_by=user.id,
        progress={"ctx": {"topic_id": (body.topic_id or "").strip(), "unit_id": unit_id,
                          "regen_reason": reason}},
    )
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
                "lo_feedback": body.lo_feedback or [],
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


@router.post("/courses/mcq/jobs/{job_id}/cancel/", status_code=status.HTTP_202_ACCEPTED)
def cancel_mcq_job(job_id: uuid.UUID, session: Session = Depends(get_session),
                   user: User = Depends(require_active)) -> dict:
    """Cancel a running or HITL-paused MCQ/regeneration job. A RUNNING job is signalled to
    stop at its next cooperative checkpoint (the worker then marks it CANCELLED); a paused
    (AWAITING_REVIEW) or queued job is cancelled immediately. Only the job's creator (or an
    admin) may cancel it. Already-settled jobs are returned unchanged."""
    job = session.get(SyncJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    if (job.created_by is not None and job.created_by != user.id
            and user.role != User.ROLE_ADMIN):
        raise HTTPException(status_code=403, detail="You can only cancel your own jobs.")
    if job.status in (SyncJob.SUCCESS, SyncJob.FAILURE, SyncJob.CANCELLED):
        return serialize_job(job)            # already settled — nothing to do
    if job.status == SyncJob.AWAITING_REVIEW:
        # No live worker (it exited when it paused) — finalize directly.
        job.status = SyncJob.CANCELLED
        job.message = "Cancelled by user."
        prog = dict(job.progress or {})
        prog.pop("awaiting_review", None)
        prog.pop("review", None)
        job.progress = prog
        session.commit()
        progress_broker.publish(str(job_id))
        return serialize_job(job)
    # PENDING / RUNNING: signal the worker; it observes this and finalizes to CANCELLED.
    signalled = request_cancel(job_id)
    job.message = "Cancelling…" if signalled else "Cancel requested…"
    session.commit()
    progress_broker.publish(str(job_id))
    return serialize_job(job)


# --------------------------------------------------------------------------- #
# Classroom Quiz — a published Slides deck → per-quiz scopes → reading material →
# LOs (4–6) → base questions → objective-bound variants (per scope, no RAG).
# --------------------------------------------------------------------------- #
def _require_active_key(session: Session, user: User) -> None:
    from app.services.user_keys import active_provider, user_has_active_key
    if not user_has_active_key(session, user.id):
        prov = active_provider(session)
        raise HTTPException(
            status_code=400,
            detail=f"Add your API key for the active connector "
                   f"'{prov.name if prov else ''}' (Account) before generating.")


def _cq_deck_or_404(session: Session, deck_id: uuid.UUID, user: User) -> ClassroomQuizDeck:
    deck = session.get(ClassroomQuizDeck, deck_id)
    if deck is None:
        raise HTTPException(status_code=404, detail="Deck not found.")
    if (deck.created_by is not None and deck.created_by != user.id
            and user.role != User.ROLE_ADMIN):
        raise HTTPException(status_code=403, detail="You can only access your own decks.")
    return deck


def _cq_scopes(session: Session, deck_id) -> list[ClassroomQuizScope]:
    return list(session.scalars(
        select(ClassroomQuizScope)
        .where(ClassroomQuizScope.deck_id == deck_id)
        .order_by(ClassroomQuizScope.scope_no)
    ).all())


def _make_cq_scope_job(session: Session, deck: ClassroomQuizDeck,
                       scope: ClassroomQuizScope, user: User) -> SyncJob:
    """Create (add + flush) one background generation job for a scope, with the scope context the
    runner/resume endpoints read back. Caller commits and then calls start_cq_scope_job."""
    job = SyncJob(
        course_id=str(deck.id), job_type=SyncJob.CLASSROOM_QUIZ, created_by=user.id,
        progress={"ctx": {"deck_id": str(deck.id), "scope_id": str(scope.id),
                          "scope_no": scope.scope_no}},
    )
    session.add(job)
    session.flush()
    return job


@router.post("/classroom-quiz/ingest/", status_code=status.HTTP_201_CREATED)
def cq_ingest(body: ClassroomQuizIngestRequest, session: Session = Depends(get_session),
              user: User = Depends(require_active)) -> dict:
    """Ingest a published Google Slides deck: fetch it, segment into per-quiz scopes, and
    persist the deck + scopes. Generation is kicked off separately (per scope)."""
    from app.services.quiz_scopes import scope_slides

    url = (body.slides_url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="slides_url is required.")
    try:
        scopes = scope_slides(url)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))
    except Exception as err:  # noqa: BLE001 — fetch/parse failure → actionable 400
        raise HTTPException(status_code=400, detail=f"Could not read the slides deck: {err}")
    if not scopes:
        raise HTTPException(status_code=400,
                            detail="No quiz scopes found (need an 'Agenda for Today's Session' slide).")

    deck = ClassroomQuizDeck(
        slides_url=url, title=(body.title or "").strip(), status=ClassroomQuizDeck.SCOPED,
        scope_count=len(scopes), question_domain=(body.question_domain or "").strip().upper(),
        created_by=user.id,
    )
    session.add(deck)
    session.flush()
    for sc in scopes:
        session.add(ClassroomQuizScope(
            deck_id=deck.id, scope_no=sc.scope_no, kind=sc.kind,
            slide_start=sc.slide_start, slide_end=sc.slide_end, slide_text=sc.slide_text,
        ))
    session.commit()
    return serialize_cq_deck(deck, _cq_scopes(session, deck.id))


@router.get("/classroom-quiz/decks/")
def cq_list_decks(session: Session = Depends(get_session),
                  user: User = Depends(require_active)) -> list[dict]:
    stmt = select(ClassroomQuizDeck).order_by(ClassroomQuizDeck.created_at.desc())
    if user.role != User.ROLE_ADMIN:
        stmt = stmt.where(ClassroomQuizDeck.created_by == user.id)
    return [serialize_cq_deck(d) for d in session.scalars(stmt).all()]


@router.get("/classroom-quiz/decks/{deck_id}/")
def cq_get_deck(deck_id: uuid.UUID, session: Session = Depends(get_session),
                user: User = Depends(require_active)) -> dict:
    deck = _cq_deck_or_404(session, deck_id, user)
    return serialize_cq_deck(deck, _cq_scopes(session, deck.id))


@router.post("/classroom-quiz/decks/{deck_id}/generate/", status_code=status.HTTP_202_ACCEPTED)
def cq_generate(deck_id: uuid.UUID, session: Session = Depends(get_session),
                user: User = Depends(require_active)) -> dict:
    """Fan out one background generation job PER SCOPE. Each job runs reading material → LOs and
    then PAUSES at GATE 1 (LO finalization) — resume it via `/classroom-quiz/jobs/{job_id}/resume/`
    to produce base questions, which are then reviewed and expanded into variants (Phase 2).
    Progress streams over the existing `/courses/mcq/jobs/{job_id}/ws` socket."""
    _require_active_key(session, user)
    deck = _cq_deck_or_404(session, deck_id, user)
    scopes = _cq_scopes(session, deck.id)
    if not scopes:
        raise HTTPException(status_code=400, detail="This deck has no scopes to generate.")

    started = [(_make_cq_scope_job(session, deck, sc, user), sc.id) for sc in scopes]
    deck.status = ClassroomQuizDeck.GENERATING
    session.commit()
    payload = {"deck_id": str(deck.id), "jobs": [serialize_job(j) for j, _ in started]}
    for job, scope_id in started:          # start AFTER commit so the worker can read the row
        start_cq_scope_job(job.id, scope_id)
    return payload


@router.post("/classroom-quiz/scopes/{scope_id}/generate/", status_code=status.HTTP_202_ACCEPTED)
def cq_generate_scope(scope_id: uuid.UUID, session: Session = Depends(get_session),
                      user: User = Depends(require_active)) -> dict:
    """Generate (or regenerate) a SINGLE quiz scope — the same per-scope pipeline as 'generate
    all', but for one scope, so a user can run quizzes one at a time. Pauses at GATE 1 (LO
    finalization). Returns the started job."""
    _require_active_key(session, user)
    scope = session.get(ClassroomQuizScope, scope_id)
    if scope is None:
        raise HTTPException(status_code=404, detail="Quiz scope not found.")
    deck = _cq_deck_or_404(session, scope.deck_id, user)   # ownership/role check
    if not (scope.slide_text or "").strip():
        raise HTTPException(status_code=400,
                            detail="This quiz scope has no slide content to generate from.")
    job = _make_cq_scope_job(session, deck, scope, user)
    deck.status = ClassroomQuizDeck.GENERATING
    session.commit()
    payload = serialize_job(job)
    start_cq_scope_job(job.id, scope.id)   # start AFTER commit so the worker can read the row
    return payload


@router.post("/classroom-quiz/jobs/{job_id}/resume/", status_code=status.HTTP_202_ACCEPTED)
def cq_resume(job_id: uuid.UUID, body: McqReviewRequest,
              session: Session = Depends(get_session),
              user: User = Depends(require_active)) -> dict:
    """GATE 1 (LO finalization): resume a classroom-quiz scope paused at the LO-review gate after
    a per-LO decision. `action='approve'` accepts the LOs as-is; `action='reject'` regenerates the
    rejected LOs (via `rejected`/`rejected_ids` + per-LO feedback) and re-pauses. Once the LOs are
    accepted, the run produces base questions and the existing base-question review + variants
    finalization (Phase 2) takes over."""
    job = session.get(SyncJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.status != SyncJob.AWAITING_REVIEW:
        raise HTTPException(status_code=409, detail="Job is not awaiting review.")
    sid = ((job.progress or {}).get("ctx") or {}).get("scope_id")
    if not sid:
        raise HTTPException(status_code=400, detail="Job is missing its scope context.")
    try:
        scope_id = uuid.UUID(str(sid))
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Job has an invalid scope id.")
    action = (body.action or "approve").strip().lower()
    if action not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail="action must be 'approve' or 'reject'.")
    decision = {"action": action, "rejected": body.rejected or [],
                "rejected_ids": body.rejected_ids or [], "note": body.note or "",
                "lo_feedback": body.lo_feedback or [], "reviewer": _reviewer_name(user)}
    job.status = SyncJob.RUNNING
    job.message = f"Resuming after {action}…"
    session.commit()
    start_cq_resume_job(job.id, scope_id, decision)
    return serialize_job(job)


@router.post("/classroom-quiz/runs/{run_id}/variants/", status_code=status.HTTP_202_ACCEPTED)
def cq_generate_variants(run_id: uuid.UUID, session: Session = Depends(get_session),
                         user: User = Depends(require_active)) -> dict:
    """Phase 2 — generate variants for a scope's APPROVED base questions. Gated on the base
    questions having been reviewed & finalized (at least one approved). Re-runnable: it drops
    any prior variants and regenerates for the current approved set."""
    _require_active_key(session, user)
    run = session.get(McqRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    deck = session.get(ClassroomQuizDeck, run.course_id) if run.course_id else None
    if deck is None:
        raise HTTPException(status_code=400, detail="This is not a Classroom Quiz run.")
    if (deck.created_by is not None and deck.created_by != user.id
            and user.role != User.ROLE_ADMIN):
        raise HTTPException(status_code=403, detail="You can only generate for your own decks.")
    if (run.approved_count or 0) < 1:
        raise HTTPException(
            status_code=400,
            detail="Review and approve at least one base question (Review Queue) before generating variants.")

    job = SyncJob(
        course_id=str(deck.id), job_type=SyncJob.CLASSROOM_QUIZ, created_by=user.id,
        progress={"ctx": {"deck_id": str(deck.id), "run_id": str(run_id), "phase": "variants"}},
    )
    session.add(job)
    session.commit()
    start_cq_variants_job(job.id, run_id)
    return serialize_job(job)


# --- live job progress over WebSocket (replaces the frontend poll) -------------- #
_SETTLED_STATUSES = {SyncJob.SUCCESS, SyncJob.FAILURE, SyncJob.CANCELLED,
                     SyncJob.AWAITING_REVIEW}


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


def _unit_names(session: Session, unit_ids: list) -> dict:
    """Map portal unit_id → its human session/unit name (UnitPart.name). One query."""
    ids = [u for u in unit_ids if u]
    if not ids:
        return {}
    rows = session.execute(
        select(UnitPart.unit_id, UnitPart.name).where(UnitPart.unit_id.in_(ids))
    ).all()
    return {u: n for u, n in rows if n}


def _loaded_run_ids(session: Session, run_ids: list) -> set:
    """Subset of run_ids that have a SUCCESSFUL portal load (drives the 'Loaded' badge +
    hiding the load option). One query, empty set when there are no ids."""
    if not run_ids:
        return set()
    rows = session.execute(
        select(BetaLoad.run_id).where(
            BetaLoad.run_id.in_(run_ids),
            BetaLoad.action == "load",
            BetaLoad.status == "SUCCESS",
        )
    ).all()
    return {r[0] for r in rows}


def _creator_names(session: Session, user_ids: list) -> dict:
    """Map each creator user_id -> display name (name or email) for the 'created by' tag.
    One query; ignores None ids and missing users."""
    ids = {u for u in user_ids if u}
    if not ids:
        return {}
    return {u.id: ((u.name or "").strip() or u.email)
            for u in session.scalars(select(User).where(User.id.in_(ids))).all()}


@router.get("/courses/mcq/runs/")
def list_mcq_runs(
    course_id: str | None = None, unit_id: str | None = None, limit: int = 10,
    session: Session = Depends(get_session),
    user: User = Depends(require_active),
) -> list[dict]:
    """Recent MCQ runs (summaries, no full result), newest first; optionally scoped
    to a course/session. Scoped to the courses the user can ACCESS (owner, collaborator,
    or admin) — NOT to who created the run, so every collaborator sees the course's practices."""
    stmt = select(McqRun).order_by(McqRun.created_at.desc()).limit(max(1, min(limit, 50)))
    if user.role != User.ROLE_ADMIN:
        all_c = set(session.scalars(select(Course.course_id)).all())
        if all_c:   # gate by course access; runs whose course row is gone are treated as open
            owned = set(session.scalars(
                select(Course.course_id).where(Course.created_by == user.id)).all())
            collab = set(session.scalars(
                select(CourseCollaborator.course_id).where(CourseCollaborator.user_id == user.id)).all())
            open_c = set(session.scalars(
                select(Course.course_id).where(Course.created_by.is_(None))).all())
            accessible = owned | collab | open_c
            stmt = stmt.where(or_(
                McqRun.course_id.in_(accessible or {"__none__"}),
                McqRun.course_id.not_in(all_c),
            ))
    if course_id:
        _require_course_access(session, course_id, user)
        stmt = stmt.where(McqRun.course_id == course_id)
    else:
        # The unscoped listing (Review Queue) shows portal MCQ runs, PLUS Classroom-Quiz runs
        # that have reached variant review (phase == "variants"). CQ BASE-question review happens
        # on the generation page, so CQ runs without variants yet stay out of the queue.
        deck_ids = [str(d) for d in session.scalars(select(ClassroomQuizDeck.id)).all()]
        if deck_ids:
            stmt = stmt.where(or_(
                McqRun.course_id.not_in(deck_ids),
                McqRun.result["phase"].astext == "variants",
            ))
    if unit_id:
        stmt = stmt.where(McqRun.unit_id == unit_id)
    runs = session.scalars(stmt).all()
    # Resolve each run's topic NAME (one query) so the run list can show it next to the id.
    tids = {r.topic_id for r in runs if r.topic_id}
    nmap: dict = {}
    if tids:
        for c, t, n in session.execute(
            select(Topic.course_id, Topic.topic_id, Topic.topic_name)
            .where(Topic.course_id.in_({r.course_id for r in runs}), Topic.topic_id.in_(tids))
        ):
            nmap[(c, t)] = n
    loaded_ids = _loaded_run_ids(session, [r.id for r in runs])
    unames = _unit_names(session, [r.unit_id for r in runs])
    cnames = _creator_names(session, [r.created_by for r in runs])
    return [serialize_mcq_run(r, include_result=False,
                              topic_name=nmap.get((r.course_id, r.topic_id), ""),
                              unit_name=unames.get(r.unit_id, ""),
                              loaded=r.id in loaded_ids,
                              created_by_name=cnames.get(r.created_by, ""))
            for r in runs]


@router.get("/courses/mcq/runs/{run_id}/")
def get_mcq_run(run_id: uuid.UUID, session: Session = Depends(get_session),
                user: User = Depends(require_active)) -> dict:
    run = session.get(McqRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="MCQ run not found.")
    # A run belongs to the COURSE, not its creator: anyone with access to the course (owner,
    # collaborator, or admin) may open it — regardless of who prepared the practice. 404 (not
    # 403) so a run's existence isn't leaked to users without access to its course.
    if not _user_can_access_course(session, run.course_id, user):
        raise HTTPException(status_code=404, detail="MCQ run not found.")
    tname = (session.scalar(select(Topic.topic_name).where(
                 Topic.course_id == run.course_id, Topic.topic_id == run.topic_id))
             if run.topic_id else "") or ""
    loaded = bool(_loaded_run_ids(session, [run.id]))
    uname = _unit_names(session, [run.unit_id]).get(run.unit_id, "")
    return serialize_mcq_run(run, topic_name=tname, unit_name=uname, loaded=loaded,
                             created_by_name=_creator_names(session, [run.created_by]).get(run.created_by, ""))


@router.post("/courses/mcq/runs/{run_id}/export-beta/", status_code=status.HTTP_202_ACCEPTED)
def export_mcq_run_to_beta(run_id: uuid.UUID, approved_only: bool = False,
                           session: Session = Depends(get_session),
                           user: User = Depends(require_active)) -> dict:
    """Build the portal-format export ZIP for a run and upload it to the BETA content-loading
    S3 bucket, as a BACKGROUND job tracked in the Activity drawer. The run is validated up front
    (so approval gates fail fast); the ZIP build + upload then run async and the public URL is
    surfaced on the resulting Loads row when the job completes."""
    run = session.get(McqRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="MCQ run not found.")
    _require_course_access(session, run.course_id, user)
    _require_reviewed(run)          # fast 409 before spawning a job
    _result_for_load(run, approved_only)  # fast 409/400 if approvals aren't met
    job = SyncJob(course_id=run.course_id, job_type=SyncJob.EXPORT, created_by=user.id,
                  message="Export queued…",
                  progress={"ctx": {"run_id": str(run_id), "kind": "export"}})
    session.add(job)
    session.commit()
    start_export_job(job.id, run_id, approved_only)
    return serialize_job(job)


@router.post("/courses/mcq/runs/{run_id}/prepare-and-load/", status_code=status.HTTP_202_ACCEPTED)
def prepare_and_load_mcq_run(run_id: uuid.UUID, body: PrepareSheetRequest,
                             session: Session = Depends(get_session),
                             user: User = Depends(require_active)) -> dict:
    """Full beta-load pipeline for a run (build+upload ZIP, copy/fill the exam-config sheet,
    submit the load task, poll it, unlock), run as a BACKGROUND job tracked in the Activity
    drawer. The run + approval gates are validated up front so failures are instant; the slow
    pipeline then runs async and its outcome appears on the Loads page.

    Content is ALWAYS loaded to BETA — there is no environment parameter and no PROD path
    anywhere in this pipeline (beta S3 bucket + beta content-loading admin only)."""
    run = session.get(McqRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="MCQ run not found.")
    _require_course_access(session, run.course_id, user)
    _require_reviewed(run)
    parent_topic_id = (body.topic_id or "").strip() or run.topic_id
    if not parent_topic_id:
        raise HTTPException(status_code=400,
                            detail="No topic_id for the exam's parent resource. Enter a topic "
                                   "id at load, or re-run with a topic selected.")
    _result_for_load(run, body.approved_only)  # fast 409/400 if approvals aren't met

    # Carry the reviewer-supplied fields + the loader's email to the background runner.
    body_dict = body.model_dump()
    body_dict["loader_email"] = user.email
    job = SyncJob(course_id=run.course_id, job_type=SyncJob.LOAD, created_by=user.id,
                  message="Load queued…",
                  progress={"ctx": {"run_id": str(run_id), "kind": "load"}})
    session.add(job)
    session.commit()
    start_load_job(job.id, run_id, body_dict)
    return serialize_job(job)


def _serialize_beta_load(row, *, include_content: bool = False) -> dict:
    """One Loads row. `include_content` adds the full loaded-questions snapshot (detail view)."""
    from app.services.beta_s3 import task_url
    content = row.content or {}
    questions = content.get("questions") or []
    out = {
        "id": row.id, "action": row.action, "status": row.status,
        "unit_name": content.get("session_label") or "",
        "run_id": row.run_id, "job_id": row.job_id, "course_id": getattr(row, "course_id", None),
        "resource_id": row.resource_id, "sheet_url": row.sheet_url, "s3_url": row.s3_url,
        "request_id": row.request_id, "unlock_id": getattr(row, "unlock_id", "") or "",
        # Beta-admin task pages for the content-loading + unlock-resource tasks.
        "loading_task_url": task_url(row.request_id),
        "unlock_task_url": task_url(getattr(row, "unlock_id", "")),
        "message": row.message,
        "count": len(questions), "has_content": bool(content), "created_at": row.created_at,
    }
    if include_content:
        out["content"] = content
    return out


@router.get("/courses/mcq/loads/")
def list_beta_loads(limit: int = 100, session: Session = Depends(get_session),
                    user: User = Depends(require_active)) -> list[dict]:
    """List portal loads / ZIP exports, newest first. Own rows by default; elevated roles
    (lead/manager/admin) see everyone's."""
    stmt = select(BetaLoad).order_by(BetaLoad.created_at.desc()).limit(max(1, min(limit, 500)))
    if user.role not in User.ELEVATED_ROLES:
        stmt = stmt.where(BetaLoad.user_id == user.id)
    rows = session.scalars(stmt).all()
    # Attach course_id + a user label via the linked run / user (best-effort, cheap maps).
    run_ids = {r.run_id for r in rows if r.run_id}
    runs = {r.id: r for r in session.scalars(select(McqRun).where(McqRun.id.in_(run_ids))).all()} if run_ids else {}
    user_ids = {r.user_id for r in rows if r.user_id}
    users = {u.id: u for u in session.scalars(select(User).where(User.id.in_(user_ids))).all()} if user_ids else {}
    out = []
    for r in rows:
        d = _serialize_beta_load(r)
        run = runs.get(r.run_id)
        d["course_id"] = run.course_id if run else None
        u = users.get(r.user_id)
        d["user_name"] = (u.name or u.email) if u else "—"
        out.append(d)
    return out


@router.get("/courses/mcq/loads/{load_id}/")
def get_beta_load(load_id: uuid.UUID, session: Session = Depends(get_session),
                  user: User = Depends(require_active)) -> dict:
    """One load/export with its full loaded-questions snapshot (for the loaded-content view)."""
    row = session.get(BetaLoad, load_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Load not found.")
    if user.role not in User.ELEVATED_ROLES and row.user_id != user.id:
        raise HTTPException(status_code=403, detail="Not your load.")
    d = _serialize_beta_load(row, include_content=True)
    run = session.get(McqRun, row.run_id) if row.run_id else None
    d["course_id"] = run.course_id if run else None
    return d


# --- Human-in-the-loop review (Gate B): feedback + regenerate + approve --------- #
def _reviewer_name(user: User) -> str:
    """Attribution for a review action — taken from the authenticated user (we no
    longer ask the reviewer to type their name). Prefer the display name, fall back
    to the email so it's never blank."""
    return (user.name or "").strip() or user.email


@router.post("/courses/mcq/runs/{run_id}/questions/{outcome}/regenerate/",
             status_code=status.HTTP_202_ACCEPTED)
def regenerate_mcq_question(run_id: uuid.UUID, outcome: str,
                            body: RegenerateQuestionRequest,
                            session: Session = Depends(get_session),
                            user: User = Depends(require_active)) -> dict:
    """Regenerate one question for its LO with the reviewer's feedback injected, re-review it,
    persist (with revision history) and log the feedback. Runs in the background as a tracked
    job (REGEN) so it shows up in Activity and the reviewer can keep working; the frontend
    re-fetches the run when the job completes. The reviewer is the authenticated user."""
    run = session.get(McqRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="MCQ run not found.")
    _require_course_access(session, run.course_id, user)
    if not (body.feedback or "").strip():
        raise HTTPException(status_code=400, detail="Feedback is required to regenerate.")
    job = SyncJob(course_id=run.course_id, job_type=SyncJob.REGEN, created_by=user.id,
                  message=f"Regenerating “{outcome}”…")
    session.add(job)
    session.commit()
    start_mcq_regen_job(job.id, run_id, outcome, body.feedback, body.tags, _reviewer_name(user))
    return serialize_job(job)


@router.post("/courses/mcq/runs/{run_id}/questions/{outcome}/feedback/")
def submit_mcq_feedback(run_id: uuid.UUID, outcome: str,
                        body: QuestionFeedbackRequest,
                        session: Session = Depends(get_session),
                        user: User = Depends(require_active)) -> dict:
    run = session.get(McqRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="MCQ run not found.")
    _require_course_access(session, run.course_id, user)
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
    run = session.get(McqRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="MCQ run not found.")
    _require_course_access(session, run.course_id, user)
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
    run = session.get(McqRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="MCQ run not found.")
    _require_course_access(session, run.course_id, user)
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
    _require_course_access(session, run.course_id, user)
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
    """Environments a course + its prerequisites span. Tokens are now OPTIONAL
    everywhere — reading materials without stored resource ids are discovered
    token-free from the content-loading admin's CSV (GET_UNIT_RESOURCE_DETAILS) —
    so ``token_required`` is always empty. ``token_optional`` lists the
    environments where supplying a token would still enrich extraction (the
    learning API is tutorial-aware, unlike the admin cheat-sheet path)."""
    course = session.get(Course, course_id)
    if not course:
        raise HTTPException(status_code=404, detail="Course not found.")
    return {
        "course_id": course_id,
        "environments": required_environments(course),
        "token_required": [],
        "token_optional": environments_needing_token(session, course),
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
    # Include prerequisite courses too — they must be selectable in the MCQ generation picker
    # (you can generate learning outcomes for a prerequisite course as well), not only the
    # top-level courses. (Prerequisite courses still also appear nested under their parents.)
    top_level = session.scalars(select(Course).order_by(Course.course_name)).all()
    # One grouped query gives the ingested chunk count per course (0 = not ingested).
    counts = dict(
        session.execute(
            select(RagChunk.course_id, func.count()).group_by(RagChunk.course_id)
        ).all()
    )
    issue_counts = _content_issue_counts(session)
    # One grouped query maps each course to the users granted collaborator access.
    collabs: dict[str, list] = {}
    for cid, uid in session.execute(
        select(CourseCollaborator.course_id, CourseCollaborator.user_id)
    ).all():
        collabs.setdefault(cid, []).append(uid)
    return [
        serialize_course_list(
            c,
            ingested_chunk_count=counts.get(c.course_id, 0),
            content_issue_count=issue_counts.get(c.course_id, 0),
            collaborator_ids=collabs.get(c.course_id, []),
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
    collaborator_ids = list(session.scalars(
        select(CourseCollaborator.user_id).where(
            CourseCollaborator.course_id == course.course_id)
    ))
    return serialize_course_detail(
        course,
        part_counts=part_counts,
        course_counts=course_counts,
        issue_counts=issue_counts,
        stale_part_ids=stale_part_ids,
        collaborator_ids=collaborator_ids,
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
