"""
app/services/rag_build.py
-------------------------
Build the RAG vector store for a course + its prerequisites.

For every extracted "Reading Material" UnitPart (optionally limited to a selection
of unit_ids), the part's Markdown is chunked, embedded via OpenRouter, and stored
as RagChunk rows tagged with course/topic/unit for scoped retrieval. Runs in a
background thread; progress is written onto the SyncJob row (polled by the UI).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import Course, RagChunk, SyncJob, Topic, Unit, UnitPart
from app.services.chunking import chunk_markdown
from app.services.extraction import (
    READING_MATERIAL_LABEL,
    collect_courses_recursive,
)
from app.services.openrouter import embed_texts

_EMBED_BATCH = 64


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _extracted_parts(session: Session, course_id: str, unit_ids: list[str] | None):
    """Reading-material parts for a course that have extracted content, with their
    owning topic/unit ids (for denormalized tagging). Optionally limited to a set
    of portal unit_ids selected in the UI."""
    stmt = (
        select(UnitPart, Topic.id, Unit.id)
        .join(Unit, UnitPart.container_id == Unit.id)
        .join(Topic, Unit.topic_id == Topic.id)
        .where(
            Topic.course_id == course_id,
            Unit.kind == Unit.SESSION,
            UnitPart.label == READING_MATERIAL_LABEL,
            UnitPart.content_status == "EXTRACTED",
            UnitPart.content != "",
        )
        .order_by(UnitPart.order)
    )
    if unit_ids:
        stmt = stmt.where(UnitPart.unit_id.in_(unit_ids))
    return list(session.execute(stmt))


def run_build_rag_job(
    session: Session, job_id: uuid.UUID, unit_ids: list[str] | None = None
) -> None:
    job = session.get(SyncJob, job_id)
    if job is None:
        return

    def report(message: str, status: str = SyncJob.RUNNING) -> None:
        job.status = status
        job.message = message
        job.updated_at = _now()
        session.commit()

    try:
        root = session.get(Course, job.course_id)
        if not root:
            report("Course not found.", SyncJob.FAILURE)
            return

        report("Collecting course + prerequisites…")
        courses = collect_courses_recursive(root)
        course_ids = [c.course_id for c in courses]

        # Re-build is idempotent: clear existing chunks for these courses first.
        session.execute(delete(RagChunk).where(RagChunk.course_id.in_(course_ids)))
        session.commit()

        # Gather all (part, topic_id, unit_id) rows across the courses.
        rows: list[tuple[UnitPart, uuid.UUID, uuid.UUID, str]] = []
        for c in courses:
            for part, topic_id, unit_id in _extracted_parts(session, c.course_id, unit_ids):
                rows.append((part, topic_id, unit_id, c.course_id))

        if not rows:
            report(
                "No extracted reading material to index"
                + (" for the selected units." if unit_ids else "."),
                SyncJob.SUCCESS,
            )
            return

        # Chunk every part up front so we can embed in batches across parts.
        pending: list[dict] = []
        for part, topic_id, unit_id, course_id in rows:
            for ch in chunk_markdown(part.content):
                pending.append(
                    {
                        "unit_part_id": part.id,
                        "course_id": course_id,
                        "topic_id": topic_id,
                        "unit_id": unit_id,
                        **ch,
                    }
                )

        total = len(pending)
        report(f"Embedding {total} chunk(s) from {len(rows)} reading material(s)…")

        created = 0
        for start in range(0, total, _EMBED_BATCH):
            batch = pending[start : start + _EMBED_BATCH]
            vectors = embed_texts([c["text"] for c in batch])
            session.add_all([
                RagChunk(
                    unit_part_id=c["unit_part_id"],
                    course_id=c["course_id"],
                    topic_id=c["topic_id"],
                    unit_id=c["unit_id"],
                    section=c["section"],
                    position=c["position"],
                    has_code=c["has_code"],
                    char_len=c["char_len"],
                    text=c["text"],
                    embedding=vec,
                )
                for c, vec in zip(batch, vectors)
            ])
            session.commit()
            created += len(batch)
            report(f"Embedded {created}/{total} chunk(s)…")

        job.status = SyncJob.SUCCESS
        job.message = (
            f"Indexed {created} chunks from {len(rows)} materials."
        )
        job.updated_at = _now()
        session.commit()
    except Exception as err:  # noqa: BLE001
        session.rollback()
        job = session.get(SyncJob, job_id)
        if job is not None:
            job.status = SyncJob.FAILURE
            job.error = str(err)
            job.updated_at = _now()
            session.commit()
