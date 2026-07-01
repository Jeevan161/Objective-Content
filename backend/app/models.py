"""
app/models.py
-------------
SQLAlchemy 2.0 mirror of the former Django schema (courses app) + the new RAG
corpus table. Field names and semantics match the Django models so the API can
reproduce the exact JSON the frontend expects.

Hierarchy:  Course → Topic → Unit (kind SESSION/PRACTICE/SINGLE) → UnitPart
            Course ←→ Course  (self-referential prerequisites M2M)
            UnitPart → RagChunk (embedded section chunks, for retrieval)
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.config import settings
from app.db.base import Base, created_at_col, updated_at_col, uuid_pk

# --------------------------------------------------------------------------- #
# Course prerequisites — self-referential M2M (directional: course requires prereq)
# --------------------------------------------------------------------------- #
course_prerequisites = Table(
    "course_prerequisites",
    Base.metadata,
    Column(
        "course_id",
        String(64),
        ForeignKey("courses.course_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "prerequisite_id",
        String(64),
        ForeignKey("courses.course_id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


class Course(Base):
    """A course fetched from the portal, keyed by its portal course UUID."""

    __tablename__ = "courses"

    course_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    environment: Mapped[str] = mapped_column(String(16), default="PROD")
    course_name: Mapped[str] = mapped_column(String(500), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    duration: Mapped[str] = mapped_column(String(64), default="")
    multimedia_url: Mapped[str] = mapped_column(String(1000), default="")
    course_category: Mapped[str] = mapped_column(String(255), default="")
    course_link: Mapped[str] = mapped_column(String(1000), default="")
    # Question DOMAIN for MCQ generation, set per course (e.g. "SQL"). Empty = generic.
    # Deterministically activates domain-specific generation/review rules for the WHOLE
    # run (read into RagAdapter.domain), instead of guessing the domain per outcome.
    question_domain: Mapped[str] = mapped_column(String(16), default="")

    selected_courseversion_id: Mapped[str] = mapped_column(String(64), default="")
    selected_version_id: Mapped[str] = mapped_column(String(64), default="")
    is_latest_version: Mapped[bool] = mapped_column(Boolean, default=False)

    last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Set when a reading-material extraction (this course + its prerequisites)
    # last completed — gates the "Build RAG" action.
    content_extracted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # The user who first added (synced) this course. Gates who may generate MCQs for
    # it (admins bypass; legacy/unclaimed rows are open and get claimed on next sync).
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    topics: Mapped[list["Topic"]] = relationship(
        back_populates="course",
        cascade="all, delete-orphan",
        order_by="Topic.order",
    )
    # X.prerequisites = courses X depends on; X.required_by = courses that need X.
    prerequisites: Mapped[list["Course"]] = relationship(
        "Course",
        secondary=course_prerequisites,
        primaryjoin=course_id == course_prerequisites.c.course_id,
        secondaryjoin=course_id == course_prerequisites.c.prerequisite_id,
        back_populates="required_by",
    )
    required_by: Mapped[list["Course"]] = relationship(
        "Course",
        secondary=course_prerequisites,
        primaryjoin=course_id == course_prerequisites.c.prerequisite_id,
        secondaryjoin=course_id == course_prerequisites.c.course_id,
        back_populates="prerequisites",
    )


class Topic(Base):
    __tablename__ = "topics"

    id: Mapped[uuid.UUID] = uuid_pk()
    course_id: Mapped[str] = mapped_column(
        ForeignKey("courses.course_id", ondelete="CASCADE")
    )
    topic_id: Mapped[str] = mapped_column(String(64), default="")
    topic_name: Mapped[str] = mapped_column(String(500), default="")
    topic_link: Mapped[str] = mapped_column(String(1000), default="")
    order: Mapped[int] = mapped_column(Integer, default=0)
    # Authoritative child order from the details fetch (GET_UNIT_RESOURCE_DETAILS `topic_order`).
    # Nullable until extraction populates it; the view falls back to `order` when absent.
    child_order: Mapped[int | None] = mapped_column(Integer, nullable=True)

    course: Mapped["Course"] = relationship(back_populates="topics")
    units: Mapped[list["Unit"]] = relationship(
        back_populates="topic",
        cascade="all, delete-orphan",
        order_by="Unit.order",
    )


class Unit(Base):
    """A container grouping related portal units (its parts), by unit type:
    SESSION (LEARNING_SET + QUIZ), PRACTICE (PRACTICE + QUESTION_SET), SINGLE."""

    __tablename__ = "units"

    SESSION = "SESSION"
    PRACTICE = "PRACTICE"
    SINGLE = "SINGLE"

    id: Mapped[uuid.UUID] = uuid_pk()
    topic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("topics.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16), default=SINGLE)
    label: Mapped[str] = mapped_column(String(500), default="")
    order: Mapped[int] = mapped_column(Integer, default=0)
    # Container child order = the smallest `unit_order` among its learning-set parts (details fetch).
    child_order: Mapped[int | None] = mapped_column(Integer, nullable=True)

    topic: Mapped["Topic"] = relationship(back_populates="units")
    parts: Mapped[list["UnitPart"]] = relationship(
        back_populates="container",
        cascade="all, delete-orphan",
        order_by="UnitPart.order",
    )


class UnitPart(Base):
    """One portal unit inside a Unit container (e.g. a quiz 'A', a reading material)."""

    __tablename__ = "unit_parts"

    id: Mapped[uuid.UUID] = uuid_pk()
    container_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("units.id", ondelete="CASCADE"))
    label: Mapped[str] = mapped_column(String(64), default="")
    unit_id: Mapped[str] = mapped_column(String(64), default="")
    unit_type: Mapped[str] = mapped_column(String(64), default="")
    name: Mapped[str] = mapped_column(String(500), default="")
    link: Mapped[str] = mapped_column(String(1000), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    order: Mapped[int] = mapped_column(Integer, default=0)
    # Authoritative child order from the details fetch (GET_UNIT_RESOURCE_DETAILS `unit_order`).
    child_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Published slide-deck URLs for this learning set (from the details CSV `slide_urls`), shown as
    # an embedded iframe in the course view. Only LEARNING_SET units carry these.
    slide_urls: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)

    # Extracted reading-material content (parts labelled "Reading Material").
    content: Mapped[str] = mapped_column(Text, default="")
    content_status: Mapped[str] = mapped_column(String(16), default="")  # EXTRACTED/EMPTY/ERROR
    content_error: Mapped[str] = mapped_column(Text, default="")
    content_extracted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Portal learning_resource ids of every resource inside this set, captured
    # during extraction (the unit_id is the *set* id; these are the resources
    # within it — each also doubles as a tutorial_entity_id).
    resource_ids: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, default=list
    )

    container: Mapped["Unit"] = relationship(back_populates="parts")
    rag_chunks: Mapped[list["RagChunk"]] = relationship(
        back_populates="unit_part", cascade="all, delete-orphan"
    )


class SyncJob(Base):
    """Tracks a background job (sync / extract / rag) so the frontend can poll it."""

    __tablename__ = "sync_jobs"

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    AWAITING_REVIEW = "AWAITING_REVIEW"   # paused at a HITL gate (division / LO-mapping)
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    CANCELLED = "CANCELLED"               # stopped by the user mid-run / while paused

    SYNC = "SYNC"
    EXTRACT = "EXTRACT"
    RAG = "RAG"
    MCQ = "MCQ"
    REGEN = "REGEN"                       # one-question regeneration (Review Queue)
    LOAD = "LOAD"                         # portal "Prepare & Load" (background)
    EXPORT = "EXPORT"                     # build + upload the questions ZIP (background)
    CLASSROOM_QUIZ = "CLASSROOM_QUIZ"     # generate one classroom-quiz scope (slides -> RM -> LOs -> Qs -> variants)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    job_type: Mapped[str] = mapped_column(String(16), default=SYNC)
    course_id: Mapped[str] = mapped_column(String(64))
    environment: Mapped[str] = mapped_column(String(16), default="PROD")
    # Structured stage board for live multi-step progress (MCQ pipeline). Shape:
    # {"stages": [{key,label,state,parallel_group?,detail?,done?,total?,needs_human?}], "updated_at"}.
    progress: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # When set, the synced course is linked as a prerequisite of this course_id.
    prerequisite_for: Mapped[str] = mapped_column(String(64), default="")
    courseversion_id: Mapped[str] = mapped_column(String(64), default="")
    version_id: Mapped[str] = mapped_column(String(64), default="")
    is_latest_version: Mapped[bool] = mapped_column(Boolean, default=False)
    # Chosen at course-add time; applied to the Course when this sync persists it.
    question_domain: Mapped[str] = mapped_column(String(16), default="")

    status: Mapped[str] = mapped_column(String(16), default=PENDING)
    message: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")

    # The user who triggered this job (nullable: pre-auth rows + system jobs).
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class RagChunk(Base):
    """One embedded section chunk. course_id/topic_id/unit_id are denormalized so
    scoped retrieval filters never need a join."""

    __tablename__ = "rag_chunks"
    __table_args__ = (
        Index("ix_rag_chunks_course", "course_id"),
        Index("ix_rag_chunks_course_topic", "course_id", "topic_id"),
        Index("ix_rag_chunks_course_unit", "course_id", "unit_id"),
        # HNSW cosine index on `embedding` is created in the migration via raw SQL.
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    unit_part_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("unit_parts.id", ondelete="CASCADE")
    )
    course_id: Mapped[str] = mapped_column(ForeignKey("courses.course_id", ondelete="CASCADE"))
    topic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("topics.id", ondelete="CASCADE"))
    unit_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("units.id", ondelete="CASCADE"))

    section: Mapped[str | None] = mapped_column(String(512), nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    has_code: Mapped[bool] = mapped_column(Boolean, default=False)
    char_len: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(settings.embed_dimensions))
    created_at: Mapped[datetime] = created_at_col()

    unit_part: Mapped["UnitPart"] = relationship(back_populates="rag_chunks")


class McqRun(Base):
    """One run of the MCQ-generation pipeline for a selected course/topic/session.
    The full pipeline output (LOs + questions + reviews + trace) is stored as JSONB;
    summary columns make listing/filtering cheap."""

    __tablename__ = "mcq_runs"

    id: Mapped[uuid.UUID] = uuid_pk()
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sync_jobs.id", ondelete="SET NULL"), nullable=True
    )
    course_id: Mapped[str] = mapped_column(String(64), index=True)
    topic_id: Mapped[str] = mapped_column(String(64), default="")
    unit_id: Mapped[str] = mapped_column(String(64), default="", index=True)  # portal session id
    langsmith_run_url: Mapped[str] = mapped_column(Text, default="")

    lo_count: Mapped[int] = mapped_column(Integer, default=0)
    question_count: Mapped[int] = mapped_column(Integer, default=0)
    needs_human_count: Mapped[int] = mapped_column(Integer, default=0)
    # Token usage + ESTIMATED cost (USD, list-price estimate) for this run. The full
    # per-model / per-step breakdown lives in `result["cost"]`; these are summary columns
    # for cheap listing/aggregation. total_tokens = input + output.
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    # How many generated questions a human has explicitly approved — drives the load gate.
    approved_count: Mapped[int] = mapped_column(Integer, default=0)
    # 1-based generation version within a (course_id, unit_id) session (v1 = oldest); a
    # session is generated multiple times and this distinguishes the runs.
    version: Mapped[int] = mapped_column(Integer, default=1)

    # Human-in-the-loop review lifecycle: draft → lo_review → question_review → approved.
    review_status: Mapped[str] = mapped_column(String(20), default="draft")

    # final_los + questions + question_reviews + notes + apply_tool_trace + prompt versions.
    # For Classroom Quiz runs, `questions` also holds the per-base variants (linked by
    # `base_question_key`).
    result: Mapped[dict] = mapped_column(JSONB, default=dict)
    # Classroom Quiz: the session reading material generated for this scope (m00 output).
    # Empty for portal-sourced MCQ runs, which read reading material from UnitParts.
    reading_material: Mapped[str] = mapped_column(Text, default="")
    # The user who generated this run (nullable for pre-auth rows). Carried from the job.
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = created_at_col()


class McqTrace(Base):
    """One node-execution SPAN of an MCQ pipeline run — our own LangGraph-tailored trace
    (replaces LangSmith). One row per node ENTRY, so a node re-run by the repair loop (or across
    a HITL pause/resume) yields multiple spans. `job_id` is the run's checkpoint thread_id.
    Read ordered by `started_at` then `seq`."""

    __tablename__ = "mcq_traces"
    __table_args__ = (Index("ix_mcq_traces_job", "job_id"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    job_id: Mapped[uuid.UUID] = mapped_column(index=True)   # = thread_id (the run's checkpoint key)
    seq: Mapped[int] = mapped_column(Integer, default=0)
    node: Mapped[str] = mapped_column(String(64))
    label: Mapped[str] = mapped_column(String(160), default="")
    status: Mapped[str] = mapped_column(String(16), default="ok")   # ok | error
    detail: Mapped[str] = mapped_column(Text, default="")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    # Compact, JSON-safe snapshot of what this node produced (counts + small samples), surfaced in
    # the UI when a node is expanded. Deliberately small — NOT the full checkpoint state.
    snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = created_at_col()


class McqPrompt(Base):
    """A versioned, editable prompt used by the MCQ pipeline. The active version per
    key is what the pipeline reads; code constants are the seed/fallback."""

    __tablename__ = "mcq_prompts"
    __table_args__ = (UniqueConstraint("key", "version", name="uq_mcq_prompt_key_version"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    key: Mapped[str] = mapped_column(String(128), index=True)
    content: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    description: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = updated_at_col()


class McqQuestionFeedback(Base):
    """One human review action on a generated LO or question — the durable signal
    that powers regeneration history and (later) few-shot / prompt-tuning. Keyed to
    the run + the LO `outcome` slug. No auth model yet, so `reviewer` is a free string."""

    __tablename__ = "mcq_question_feedback"

    id: Mapped[uuid.UUID] = uuid_pk()
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("mcq_runs.id", ondelete="CASCADE"), index=True, nullable=True
    )
    stage: Mapped[str] = mapped_column(String(16), default="question")   # 'lo' | 'question'
    outcome: Mapped[str] = mapped_column(String(160), default="", index=True)
    question_type: Mapped[str] = mapped_column(String(48), default="")
    action: Mapped[str] = mapped_column(String(24), default="")          # 'accept' | 'reject_regenerate'
    tags: Mapped[list] = mapped_column(JSONB, default=list)
    comment: Mapped[str] = mapped_column(Text, default="")
    before_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)
    after_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    reviewer: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = created_at_col()


class AppFeedback(Base):
    """Application-level feedback from any signed-in user — an emoji rating (1–5), a
    category, an optional 'was this helpful?' vote, and free text. Surfaced in the
    admin dashboard. Distinct from `McqQuestionFeedback`, which records per-question
    review actions on a run."""

    __tablename__ = "app_feedback"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
    )
    category: Mapped[str] = mapped_column(String(40), default="", index=True)
    rating: Mapped[int] = mapped_column(Integer, default=0)          # 1–5 (emoji); 0 = unrated
    helpful: Mapped[bool | None] = mapped_column(Boolean, nullable=True)  # True/False/None
    message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = created_at_col()


class LlmProvider(Base):
    """A user-configurable LLM connector (OpenAI / OpenRouter / Anthropic / internal
    proxy). The API key is stored ENCRYPTED (Fernet) in `api_key_enc`; exactly one row is
    `active` and drives every pipeline LLM call. `extra_body` holds the proxy metadata
    block (required by the internal proxy)."""

    __tablename__ = "llm_providers"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    adapter: Mapped[str] = mapped_column(String(32), default="openai_compatible")  # openai_compatible | anthropic
    model: Mapped[str] = mapped_column(String(128), default="")
    base_url: Mapped[str] = mapped_column(String(500), default="")
    api_key_enc: Mapped[str] = mapped_column(Text, default="")          # Fernet-encrypted; never plaintext
    default_headers: Mapped[dict] = mapped_column(JSONB, default=dict)
    extra_body: Mapped[dict] = mapped_column(JSONB, default=dict)        # e.g. {"metadata": {...}} for the proxy
    active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class User(Base):
    """An application user. Self-registration creates an INACTIVE account; an admin must
    approve it before it can generate/load. Each user supplies their own LLM API key
    (Fernet-encrypted in `api_key_enc`) — all other provider settings stay global."""

    __tablename__ = "users"

    ROLE_USER = "user"
    ROLE_LEAD = "lead"
    ROLE_MANAGER = "manager"
    ROLE_ADMIN = "admin"

    # All assignable roles, and the elevated set that may view oversight surfaces
    # (analytics dashboard, task logs, reviewer feedback). User management stays admin-only.
    ROLES = {ROLE_USER, ROLE_LEAD, ROLE_MANAGER, ROLE_ADMIN}
    ELEVATED_ROLES = {ROLE_LEAD, ROLE_MANAGER, ROLE_ADMIN}

    id: Mapped[uuid.UUID] = uuid_pk()
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(16), default=ROLE_USER)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class CourseCollaborator(Base):
    """Grants a user access to work on (generate content for) a course they don't own.
    Added by the course owner or an admin; the grant is immediate — there is no approval
    step. Effective access to a course = its owner (``Course.created_by``) OR a row here
    OR any admin. Unowned/legacy courses (created_by is None) stay open to everyone."""

    __tablename__ = "course_collaborators"
    __table_args__ = (
        UniqueConstraint("course_id", "user_id", name="uq_course_collaborator"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    course_id: Mapped[str] = mapped_column(
        ForeignKey("courses.course_id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    # Who granted the access (owner or admin). Kept for audit; SET NULL if they're removed.
    granted_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = created_at_col()


class UserLlmKey(Base):
    """A user's personal API key for ONE LLM connector. A user supplies a key per
    connector they use (up to one per `llm_providers` row); only the key is per-user —
    the connector's model / base_url / proxy `extra_body` / headers stay global."""

    __tablename__ = "user_llm_keys"
    __table_args__ = (UniqueConstraint("user_id", "provider_id", name="uq_user_llm_key"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    provider_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("llm_providers.id", ondelete="CASCADE"), index=True
    )
    api_key_enc: Mapped[str] = mapped_column(Text, default="")          # Fernet-encrypted; never plaintext
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class TaskLog(Base):
    """One backend log line for a task (sync/extract/rag/mcq/export/load), persisted so
    crashes are diagnosable from the admin dashboard. Also mirrored to stdout."""

    __tablename__ = "task_logs"
    __table_args__ = (Index("ix_task_logs_job", "job_id"),
                      Index("ix_task_logs_created", "created_at"))

    id: Mapped[uuid.UUID] = uuid_pk()
    job_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)   # indexed via __table_args__
    run_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    task_type: Mapped[str] = mapped_column(String(16), default="")      # SYNC|EXTRACT|RAG|MCQ|EXPORT|LOAD
    level: Mapped[str] = mapped_column(String(8), default="INFO")       # INFO|WARNING|ERROR
    event: Mapped[str] = mapped_column(String(64), default="")
    message: Mapped[str] = mapped_column(Text, default="")
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)           # stack trace / context
    created_at: Mapped[datetime] = created_at_col()


class BetaLoad(Base):
    """One export/load action against an MCQ run — the per-user audit trail for the beta
    pipeline (powers the admin dashboard's load counts)."""

    __tablename__ = "beta_loads"

    id: Mapped[uuid.UUID] = uuid_pk()
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("mcq_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    action: Mapped[str] = mapped_column(String(8), default="load")      # export | load
    status: Mapped[str] = mapped_column(String(16), default="")         # SUCCESS|FAILURE|INCOMPLETE
    resource_id: Mapped[str] = mapped_column(String(64), default="")
    sheet_url: Mapped[str] = mapped_column(Text, default="")
    s3_url: Mapped[str] = mapped_column(Text, default="")
    request_id: Mapped[str] = mapped_column(String(64), default="")     # content-loading task id
    unlock_id: Mapped[str] = mapped_column(String(64), default="")      # unlock-resources task id
    message: Mapped[str] = mapped_column(Text, default="")
    # Soft link to the background SyncJob that performed this load/export (nullable).
    job_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True, index=True)
    # Snapshot of exactly what was loaded/exported: the {**result, "questions": [...]} payload,
    # so the loaded content can be viewed later even if the run is regenerated. Nullable for
    # legacy rows predating this column.
    content: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = created_at_col()


# --------------------------------------------------------------------------- #
# Classroom Quiz — a published Slides deck segmented into per-quiz "scopes".
# A deck is the ingest unit; each scope is generated independently (one McqRun
# per scope, reusing the MCQ LangGraph with no-RAG, session-only grounding).
# --------------------------------------------------------------------------- #
class ClassroomQuizDeck(Base):
    """One published Google Slides deck submitted for Classroom Quiz generation.
    `scope_slides()` segments it into `ClassroomQuizScope` rows at ingest time."""

    __tablename__ = "classroom_quiz_decks"

    SCOPED = "SCOPED"                     # ingested + segmented into scopes
    GENERATING = "GENERATING"             # at least one scope job is running
    READY_FOR_REVIEW = "READY_FOR_REVIEW"  # all scope runs settled, awaiting human review
    APPROVED = "APPROVED"                 # all scopes approved
    FAILED = "FAILED"                     # ingest failed (no scopes produced)

    id: Mapped[uuid.UUID] = uuid_pk()
    slides_url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(20), default=SCOPED, index=True)
    scope_count: Mapped[int] = mapped_column(Integer, default=0)
    # Optional course/domain attribution (drives code-path gating + provenance); the deck
    # itself is NOT tied to a portal Course row.
    question_domain: Mapped[str] = mapped_column(String(16), default="")
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()

    scopes: Mapped[list["ClassroomQuizScope"]] = relationship(
        back_populates="deck", cascade="all, delete-orphan", order_by="ClassroomQuizScope.scope_no"
    )


class ClassroomQuizScope(Base):
    """One quiz-worth of slides from a deck. Holds the raw slide copy and the generated
    reading material; `run_id` links to the McqRun that produced its LOs/questions/variants."""

    __tablename__ = "classroom_quiz_scopes"
    __table_args__ = (UniqueConstraint("deck_id", "scope_no", name="uq_cq_scope_deck_no"),)

    # Coverage flags set by the LO stage (see lo_config clamp: ceiling 6 / floor 4 / hard 3).
    OK = "OK"
    THIN = "THIN"                         # only 3 assessable LOs were feasible
    INSUFFICIENT = "INSUFFICIENT"         # fewer than 3 — needs a human / not enough taught
    FAILED = "FAILED"                     # the scope's generation job errored

    id: Mapped[uuid.UUID] = uuid_pk()
    deck_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("classroom_quiz_decks.id", ondelete="CASCADE"), index=True
    )
    scope_no: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(20), default="")       # "Quiz Time!" | "Key Takeaways"
    slide_start: Mapped[int] = mapped_column(Integer, default=0)
    slide_end: Mapped[int] = mapped_column(Integer, default=0)
    slide_text: Mapped[str] = mapped_column(Text, default="")        # raw readable slide copy
    reading_material: Mapped[str] = mapped_column(Text, default="")  # m00 output (the handout)
    coverage: Mapped[str] = mapped_column(String(16), default=OK)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("mcq_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()

    deck: Mapped["ClassroomQuizDeck"] = relationship(back_populates="scopes")
