"""
app/schemas.py
--------------
Request validation models (Pydantic) + output serializers that reproduce the
former DRF serializer shapes EXACTLY, so the existing frontend is unaffected.

Outputs are built as plain dicts (FastAPI's jsonable_encoder renders datetimes /
UUIDs), which keeps full control over the nested + computed fields the DRF
ModelSerializers produced (has_content, content_chars, topic_count, …).
"""

from __future__ import annotations

from pydantic import BaseModel, field_validator

from app.models import Course, SyncJob, Topic, Unit, UnitPart

# Allowed per-course MCQ generation domains. "" = generic (default); "SQL" activates
# the SQL generation/review rule blocks for the whole run. Extend as new domains land.
ALLOWED_QUESTION_DOMAINS = {"", "SQL"}


# --------------------------------------------------------------------------- #
# Request bodies
# --------------------------------------------------------------------------- #
class VersionsRequest(BaseModel):
    course_id: str = ""
    environment: str | None = None


class SyncRequest(BaseModel):
    course_id: str = ""
    courseversion_id: str | None = None
    version_id: str | None = None
    is_latest_version: bool = False
    environment: str | None = None
    prerequisite_for: str | None = None
    question_domain: str = ""          # per-course MCQ domain (e.g. "SQL"), chosen at add time


class ExtractRequest(BaseModel):
    course_id: str = ""
    tokens: dict | None = None
    # When given, limit extraction to these reading-material/learning-set portal
    # unit_ids (per-unit "Sync content") instead of the whole course.
    unit_ids: list[str] | None = None


class BuildRagRequest(BaseModel):
    course_id: str = ""
    unit_ids: list[str] | None = None


class McqGenerateRequest(BaseModel):
    course_id: str = ""
    topic_id: str = ""
    unit_id: str = ""  # a reading-material part's portal unit_id within the session
    review: bool = True
    # User-chosen LO/question budget (default ceiling = 20; the Planner steps it down by 5s if thin).
    question_budget: int | None = None
    # Pause at the human-in-the-loop gates (division + LO↔concept mapping) instead of running through.
    hitl: bool = False
    # Reading-material portal unit_ids of the PREREQUISITE units to include in RAG
    # grounding. None = include all prerequisites (default); [] = none.
    prerequisite_unit_ids: list[str] | None = None


class McqReviewRequest(BaseModel):
    """A human decision at a HITL gate, plus the run context needed to resume the paused
    pipeline (rebuilds the run-scoped RAG adapter; the job_id is the checkpoint thread_id)."""
    action: str = "approve"                       # "approve" | "reject"
    # Per-LO reject + reason: [{"id", "feedback"}]. Each reason drives that LO's regeneration.
    rejected: list[dict] | None = None
    rejected_ids: list[str] | None = None         # legacy: ids only (reason falls back to `note`)
    note: str = ""
    # Per-LO review captured at the gate — ONE entry per reviewed outcome, stored regardless of
    # approve/reject (for the LO-feedback dataset): [{"id", "verdict": good|needs_work|regenerate,
    # "comment"}]. Regenerate entries also drive that LO's regeneration (mirrored into `rejected`).
    lo_feedback: list[dict] | None = None
    course_id: str = ""
    topic_id: str = ""
    unit_id: str = ""
    prerequisite_unit_ids: list[str] | None = None
    question_budget: int | None = None
    review: bool = True


class RagSearchRequest(BaseModel):
    course_ids: list[str]
    query: str
    topic_ids: list[str] | None = None
    unit_ids: list[str] | None = None
    top_k: int = 10


# --- human-in-the-loop review (Gate B) ---
class RegenerateQuestionRequest(BaseModel):
    feedback: str
    tags: list[str] = []
    reviewer: str = ""


class QuestionFeedbackRequest(BaseModel):
    action: str = "accept"          # accept | reject_regenerate (regenerate has its own route)
    tags: list[str] = []
    comment: str = ""
    reviewer: str = ""


class ApproveRunRequest(BaseModel):
    reviewer: str = ""


class CourseSettingsRequest(BaseModel):
    """Per-course MCQ-generation settings. `question_domain` activates a domain's
    rule blocks for every run of this course (e.g. "SQL"); "" = generic."""
    question_domain: str = ""

    @field_validator("question_domain")
    @classmethod
    def _norm_domain(cls, v: str) -> str:
        v = (v or "").strip().upper()
        if v not in ALLOWED_QUESTION_DOMAINS:
            raise ValueError(
                f"question_domain must be one of {sorted(ALLOWED_QUESTION_DOMAINS)}")
        return v


class QuestionApprovalRequest(BaseModel):
    approval: str = "approved"      # approved | rejected | pending (cleared)
    reviewer: str = ""


class QuestionExcludeRequest(BaseModel):
    excluded: bool = True           # True = exclude from load; False = include again


class PrepareSheetRequest(BaseModel):
    """User-facing fields for the exam-config sheet (Form tab). The rest is derived:
    number of questions = generated count, name = 'MCQ Practice'."""
    # Parent resource (Form!B14). Defaults to the run's topic_id; the reviewer may override
    # it at load time (e.g. to attach the exam under a different topic).
    topic_id: str = ""
    child_order: int
    duration_min: int = 30
    pass_percentage: float = 80.0          # percent (80 → stored as 0.8 in Form!B40)
    show_answer_scoring_mode: str = "INCORRECT"
    should_send_solutions: str = "yes"
    reviewer_email: str = ""               # also shared on the prepared sheet, if given
    approved_only: bool = False            # load only approved questions (else all must be approved)


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
class RegisterRequest(BaseModel):
    email: str
    password: str
    name: str = ""


class LoginRequest(BaseModel):
    email: str
    password: str


class ApiKeyRequest(BaseModel):
    api_key: str


class RoleRequest(BaseModel):
    role: str   # "user" | "admin"


class AppFeedbackRequest(BaseModel):
    """Application-level feedback: an emoji rating (1–5), a category, an optional
    'was this helpful?' vote, and free text."""
    rating: int = 0                 # 1–5 (emoji); 0 = unrated
    category: str = ""              # Generation Issue | Review | UI Related | Enhancement
    helpful: bool | None = None     # yes / no / not answered
    message: str = ""


def serialize_user(user) -> dict:
    """Public shape of a User. API keys are per-connector (see /auth/me/keys) and
    never returned here."""
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "role": user.role,
        "is_active": user.is_active,
        "created_at": user.created_at,
    }


class RagCheckRequest(BaseModel):
    course_ids: list[str]
    topic: str
    syntax: str | None = None


class RagAnswerRequest(BaseModel):
    course_ids: list[str]
    query: str = ""
    # Generous default: "list all X" questions need high recall (items scatter
    # across many sections). Capped to keep the chat context bounded.
    top_k: int = 15
    # When true, the scope is expanded to include each course's prerequisites.
    include_prerequisites: bool = True


class ExecuteCodeRequest(BaseModel):
    """Run a candidate program and (optionally) check its stdout — the same
    execution that grades a FIB. Used by the reviewer's 'Run & Check' button."""
    language: str = "PYTHON"
    code: str
    stdin: str = ""
    # When provided, the response reports whether stdout matches it
    # (trailing-whitespace-insensitive) — i.e. the FIB pass/fail.
    expected_output: str | None = None


# --------------------------------------------------------------------------- #
# Output serializers (parity with DRF)
# --------------------------------------------------------------------------- #
# `chunk_count`/`is_ingested` are derived from the RAG store (presence of RagChunk
# rows) rather than a stored flag, so they stay correct across re-ingests (which
# clear and rebuild chunks). Counts are passed in by the routes — keyed by
# UnitPart.id for parts and by Course.course_id for courses — to avoid loading the
# (large) embedding vectors just to display a status.
def serialize_unit_part(part: UnitPart, *, chunk_count: int = 0,
                        ingest_stale: bool = False) -> dict:
    return {
        "label": part.label,
        "unit_id": part.unit_id,
        "unit_type": part.unit_type,
        "name": part.name,
        "link": part.link,
        "error": part.error,
        "order": part.order,
        "content_status": part.content_status,
        "content_error": part.content_error,
        "resource_ids": part.resource_ids,
        "has_content": bool(part.content),
        "content_chars": len(part.content or ""),
        "chunk_count": chunk_count,
        "is_ingested": chunk_count > 0,
        # True when the content was modified after it was last ingested — the part
        # should be offered for re-ingestion even though it has chunks.
        "ingest_stale": bool(ingest_stale),
    }


def serialize_unit(unit: Unit, *, part_counts: dict | None = None,
                   stale_part_ids: set | None = None) -> dict:
    part_counts = part_counts or {}
    stale_part_ids = stale_part_ids or set()
    return {
        "kind": unit.kind,
        "label": unit.label,
        "order": unit.order,
        "parts": [
            serialize_unit_part(p, chunk_count=part_counts.get(p.id, 0),
                                ingest_stale=p.id in stale_part_ids)
            for p in unit.parts
        ],
    }


def serialize_topic(topic: Topic, *, part_counts: dict | None = None,
                    stale_part_ids: set | None = None) -> dict:
    return {
        "topic_id": topic.topic_id,
        "topic_name": topic.topic_name,
        "topic_link": topic.topic_link,
        "order": topic.order,
        "units": [serialize_unit(u, part_counts=part_counts, stale_part_ids=stale_part_ids)
                  for u in topic.units],
    }


def serialize_course_list(
    course: Course, *, ingested_chunk_count: int = 0, content_issue_count: int = 0
) -> dict:
    """Summary fields needed to render a (top-level or nested) course card."""
    return {
        "course_id": course.course_id,
        "environment": course.environment,
        "course_name": course.course_name,
        "course_category": course.course_category,
        "course_link": course.course_link,
        "question_domain": getattr(course, "question_domain", "") or "",
        "selected_version_id": course.selected_version_id,
        "is_latest_version": course.is_latest_version,
        "last_synced_at": course.last_synced_at,
        "content_extracted_at": course.content_extracted_at,
        "topic_count": len(course.topics),
        "prerequisite_count": len(course.prerequisites),
        "ingested_chunk_count": ingested_chunk_count,
        "is_ingested": ingested_chunk_count > 0,
        # Reading materials that ran extraction but came back EMPTY/ERROR.
        "content_issue_count": content_issue_count,
        "created_by": getattr(course, "created_by", None),
    }


def serialize_course_detail(
    course: Course,
    *,
    part_counts: dict | None = None,
    course_counts: dict | None = None,
    issue_counts: dict | None = None,
    stale_part_ids: set | None = None,
) -> dict:
    course_counts = course_counts or {}
    issue_counts = issue_counts or {}
    return {
        "course_id": course.course_id,
        "environment": course.environment,
        "course_name": course.course_name,
        "description": course.description,
        "duration": course.duration,
        "multimedia_url": course.multimedia_url,
        "course_category": course.course_category,
        "course_link": course.course_link,
        "question_domain": getattr(course, "question_domain", "") or "",
        "selected_courseversion_id": course.selected_courseversion_id,
        "selected_version_id": course.selected_version_id,
        "is_latest_version": course.is_latest_version,
        "last_synced_at": course.last_synced_at,
        "ingested_chunk_count": course_counts.get(course.course_id, 0),
        "is_ingested": course_counts.get(course.course_id, 0) > 0,
        "content_issue_count": issue_counts.get(course.course_id, 0),
        "created_by": getattr(course, "created_by", None),
        # Nested prerequisites use the list (summary) shape.
        "prerequisites": [
            serialize_course_list(
                p,
                ingested_chunk_count=course_counts.get(p.course_id, 0),
                content_issue_count=issue_counts.get(p.course_id, 0),
            )
            for p in course.prerequisites
        ],
        "topics": [serialize_topic(t, part_counts=part_counts, stale_part_ids=stale_part_ids)
                   for t in course.topics],
    }


def serialize_job(job: SyncJob) -> dict:
    return {
        "id": job.id,
        "job_type": job.job_type,
        "course_id": job.course_id,
        "environment": job.environment,
        "version_id": job.version_id,
        "is_latest_version": job.is_latest_version,
        "status": job.status,
        "message": job.message,
        "error": job.error,
        "progress": job.progress,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


def serialize_mcq_run(run, *, include_result: bool = True, topic_name: str = "") -> dict:
    # Eligible = generated questions a reviewer hasn't excluded; the load gate compares
    # approved_count against this.
    qs = (run.result or {}).get("questions") or []
    eligible = sum(1 for q in qs if q.get("status") == "generated" and not q.get("excluded"))
    excluded = sum(1 for q in qs if q.get("status") == "generated" and q.get("excluded"))
    out = {
        "id": run.id,
        "job_id": run.job_id,
        "course_id": run.course_id,
        "topic_id": run.topic_id,
        "topic_name": topic_name,
        "unit_id": run.unit_id,
        "version": getattr(run, "version", 1),
        "langsmith_run_url": run.langsmith_run_url,
        "lo_count": run.lo_count,
        "question_count": run.question_count,
        "needs_human_count": run.needs_human_count,
        "approved_count": getattr(run, "approved_count", 0),
        "eligible_count": eligible,
        "excluded_count": excluded,
        "review_status": getattr(run, "review_status", "draft"),
        "created_by": getattr(run, "created_by", None),
        "created_at": run.created_at,
    }
    if include_result:
        out["result"] = run.result
    return out


def serialize_mcq_trace(t) -> dict:
    """One node-execution span of an MCQ run (our own LangGraph-tailored trace)."""
    return {
        "seq": t.seq,
        "node": t.node,
        "label": t.label,
        "status": t.status,
        "detail": t.detail,
        "duration_ms": t.duration_ms,
        "snapshot": getattr(t, "snapshot", None) or {},
        "started_at": t.started_at,
        "ended_at": t.ended_at,
    }
