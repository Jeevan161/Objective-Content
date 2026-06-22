"""
app/services/portal_sync.py
---------------------------
Glue between the portal fetch layer and the SQLAlchemy models (ported from the
Django ``courses/services.py``, same logic). Runs in a background thread.

  get_course_versions   quick synchronous version list for a course id
  persist_course_data   replace stored topics/units for a course with fresh data
  run_sync_job          end-to-end sync, writing progress onto the SyncJob row
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import Course, SyncJob, Topic, Unit, UnitPart
from portal.client import PortalClient
from portal.fetch import build_course_data, fetch_course_versions


def _now() -> datetime:
    return datetime.now(timezone.utc)


def get_course_versions(course_id: str, environment: str = "PROD") -> list:
    """Quick, synchronous fetch of the available versions for a course id."""
    client = PortalClient(environment=environment)
    client.login()
    return fetch_course_versions(client, course_id)


def persist_course_data(
    session: Session, course_id: str, data: dict, version_row: dict | None,
    environment: str = "PROD",
) -> Course:
    """Replace any stored topics/units for the course with freshly fetched data."""
    details = data.get("course_details", {})
    version_row = version_row or {}

    course = session.get(Course, course_id)
    if course is None:
        course = Course(course_id=course_id)
        session.add(course)

    course.environment = environment
    course.course_name = details.get("course_name", "")
    course.description = details.get("description", "")
    course.duration = str(details.get("duration", ""))
    course.multimedia_url = details.get("multimedia_url", "")
    course.course_category = details.get("course_category", "")
    course.course_link = details.get("course_link", "")
    course.selected_courseversion_id = version_row.get("row_id", "")
    course.selected_version_id = version_row.get("version_id", "")
    course.is_latest_version = bool(version_row.get("is_latest_version", False))
    course.last_synced_at = _now()

    # Rebuild the hierarchy from scratch to avoid stale rows. The DB-level
    # ON DELETE CASCADE removes child units/parts (and any rag_chunks).
    session.execute(delete(Topic).where(Topic.course_id == course_id))
    session.flush()

    for t_order, topic in enumerate(data.get("topics", [])):
        topic_obj = Topic(
            course_id=course_id,
            topic_id=topic.get("topic_id", ""),
            topic_name=topic.get("topic_name", ""),
            topic_link=topic.get("topic_link", ""),
            order=t_order,
        )
        session.add(topic_obj)
        session.flush()  # assign topic_obj.id

        for u_order, container in enumerate(topic.get("units", [])):
            unit_obj = Unit(
                topic_id=topic_obj.id,
                kind=container.get("kind", Unit.SINGLE),
                label=container.get("label", ""),
                order=u_order,
            )
            session.add(unit_obj)
            session.flush()  # assign unit_obj.id

            session.add_all([
                UnitPart(
                    container_id=unit_obj.id,
                    label=part.get("label", ""),
                    unit_id=part.get("unit_id", ""),
                    unit_type=part.get("unit_type", ""),
                    name=part.get("name", ""),
                    link=part.get("link", ""),
                    error=part.get("error", ""),
                    order=p_order,
                )
                for p_order, part in enumerate(container.get("parts", []))
            ])

    session.commit()
    return course


def run_sync_job(session: Session, job_id: uuid.UUID) -> None:
    """Execute a sync job end to end, updating its status as it goes."""
    job = session.get(SyncJob, job_id)
    if job is None:
        return

    def report(message: str) -> None:
        job.status = SyncJob.RUNNING
        job.message = message
        job.updated_at = _now()
        session.commit()

    try:
        environment = job.environment or "PROD"
        report(f"Logging in to {environment} portal…")
        client = PortalClient(environment=environment)
        client.login()

        version_row = None
        if job.courseversion_id:
            version_row = {
                "row_id": job.courseversion_id,
                "version_id": job.version_id,
                "is_latest_version": job.is_latest_version,
            }

        data = build_course_data(
            client, job.course_id, selected_version_row=version_row, progress=report
        )

        report("Saving to database…")
        persist_course_data(session, job.course_id, data, version_row, environment=environment)

        # If this sync was for a prerequisite, link it to its parent course.
        if job.prerequisite_for:
            parent = session.get(Course, job.prerequisite_for)
            prereq = session.get(Course, job.course_id)
            if parent and prereq and parent.course_id != prereq.course_id:
                if prereq not in parent.prerequisites:
                    parent.prerequisites.append(prereq)
                    session.commit()

        topic_count = len(data.get("topics", []))
        unit_count = sum(len(t.get("units", [])) for t in data.get("topics", []))
        job.status = SyncJob.SUCCESS
        job.message = f"Saved {topic_count} topic(s) and {unit_count} unit(s)."
        job.updated_at = _now()
        session.commit()
    except Exception as err:  # noqa: BLE001 — report any failure onto the job row
        session.rollback()
        job = session.get(SyncJob, job_id)
        if job is not None:
            job.status = SyncJob.FAILURE
            job.error = str(err)
            job.updated_at = _now()
            session.commit()
