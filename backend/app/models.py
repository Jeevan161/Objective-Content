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

    SYNC = "SYNC"
    EXTRACT = "EXTRACT"
    RAG = "RAG"
    MCQ = "MCQ"

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

    status: Mapped[str] = mapped_column(String(16), default=PENDING)
    message: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")

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

    # Human-in-the-loop review lifecycle: draft → lo_review → question_review → approved.
    review_status: Mapped[str] = mapped_column(String(20), default="draft")

    # final_los + questions + question_reviews + notes + apply_tool_trace + prompt versions.
    result: Mapped[dict] = mapped_column(JSONB, default=dict)
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
