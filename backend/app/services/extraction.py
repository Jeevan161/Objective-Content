"""
app/services/extraction.py
--------------------------
Reading-material extraction (ported from the Django ``courses/extraction.py``).

Walks a course and all its prerequisites (recursively); for every Session's
"Reading Material" part it fetches the Markdown from the CCBP learning API (using a
caller-supplied Bearer token) and stores the cleaned content on the UnitPart.
Auth tokens are passed in memory only — never written to the DB.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import requests
from requests.adapters import HTTPAdapter
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Course, SyncJob, Topic, Unit, UnitPart
from portal.client import PortalClient
from portal.learning_resource import (
    fetch_admin_content,
    fetch_reading_material,
    fetch_resource_ids,
)

READING_MATERIAL_LABEL = "Reading Material"
LEARNING_SET_TYPE = "LEARNING_SET"
_POOL_SIZE = 16


def _now() -> datetime:
    return datetime.now(timezone.utc)


def collect_courses_recursive(course: Course) -> list[Course]:
    """Return [course, …all prerequisites recursively…], de-duplicated, cycle-safe."""
    seen: set[str] = set()
    ordered: list[Course] = []
    stack = [course]
    while stack:
        c = stack.pop()
        if c.course_id in seen:
            continue
        seen.add(c.course_id)
        ordered.append(c)
        stack.extend(c.prerequisites)
    return ordered


def required_environments(course: Course) -> list[str]:
    """Distinct environments across the course + all its prerequisites."""
    envs = {(c.environment or "PROD").upper() for c in collect_courses_recursive(course)}
    return sorted(envs)


def environments_needing_token(
    session: Session, course: Course, unit_ids: list[str] | None = None
) -> list[str]:
    """Environments (course + prerequisites) that still NEED a Bearer token to
    extract — i.e. they have reading-material parts with no stored resource ids,
    so the token-free admin path can't be used for them yet. ``unit_ids`` limits
    the check to those reading-material parts (for a scoped, per-unit sync)."""
    envs = set()
    for c in collect_courses_recursive(course):
        env = (c.environment or "PROD").upper()
        if env in envs:
            continue
        stmt = (
            select(func.count())
            .select_from(UnitPart)
            .join(Unit, UnitPart.container_id == Unit.id)
            .join(Topic, Unit.topic_id == Topic.id)
            .where(
                Topic.course_id == c.course_id,
                Unit.kind == Unit.SESSION,
                UnitPart.label == READING_MATERIAL_LABEL,
                func.cardinality(UnitPart.resource_ids) == 0,
            )
        )
        if unit_ids:
            stmt = stmt.where(UnitPart.unit_id.in_(unit_ids))
        if session.scalar(stmt):
            envs.add(env)
    return sorted(envs)


def learning_set_parts(session: Session, course: Course) -> list[UnitPart]:
    """All learning-set parts across the course's Session containers — both
    "Reading Material" and "Learning Resource" labels (unit_type LEARNING_SET).
    We capture the learning_resource ids for every one; content is only fetched
    for the reading-material parts."""
    stmt = (
        select(UnitPart)
        .join(Unit, UnitPart.container_id == Unit.id)
        .join(Topic, Unit.topic_id == Topic.id)
        .where(
            Topic.course_id == course.course_id,
            Unit.kind == Unit.SESSION,
            UnitPart.unit_type == LEARNING_SET_TYPE,
        )
        .order_by(UnitPart.order)
    )
    return list(session.scalars(stmt))


def run_extraction_job(
    session: Session, job_id: uuid.UUID, tokens: dict, unit_ids: list[str] | None = None
) -> None:
    """Execute an extraction job end to end (intended to run in a worker thread).

    ``tokens`` is a {ENVIRONMENT: bearer_token} map; each course's reading material
    is fetched with the token for that course's environment. ``unit_ids`` (when
    given) limits the run to those learning-set parts — used by the per-unit sync.
    """
    tokens = {(k or "").upper(): v for k, v in (tokens or {}).items()}
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
            job.status = SyncJob.FAILURE
            job.error = "Course not found."
            job.updated_at = _now()
            session.commit()
            return

        report("Collecting course + prerequisites…")
        courses = collect_courses_recursive(root)
        # (part, environment) pairs so each fetch uses the right base + token.
        # Covers every learning set; reading materials also get their content.
        items = [
            (p, (c.environment or "PROD").upper())
            for c in courses
            for p in learning_set_parts(session, c)
            if not unit_ids or p.unit_id in unit_ids
        ]
        total = len(items)
        report(f"Found {len(courses)} course(s), {total} learning set(s). Extracting…")

        http = requests.Session()
        adapter = HTTPAdapter(pool_connections=_POOL_SIZE, pool_maxsize=_POOL_SIZE)
        http.mount("https://", adapter)
        http.mount("http://", adapter)

        # Logged-in admin clients are created lazily, per environment, only when
        # the token-free admin fallback is actually needed.
        admin_clients: dict[str, PortalClient] = {}

        def admin_client(env: str) -> PortalClient:
            if env not in admin_clients:
                client = PortalClient(environment=env)
                client.login()
                admin_clients[env] = client
            return admin_clients[env]

        def set_content(part, content, *, via):
            part.content = content or ""
            part.content_status = "EXTRACTED" if content else "EMPTY"
            part.content_error = ""
            part.content_extracted_at = _now()

        extracted = empty = failed = via_admin = 0
        for idx, (part, env) in enumerate(items, start=1):
            token = tokens.get(env)
            is_reading = part.label == READING_MATERIAL_LABEL
            try:
                if token:
                    # Token present → learning API: full content (tutorial-aware)
                    # for reading materials, and capture resource ids for every set.
                    if is_reading:
                        result = fetch_reading_material(http, part.unit_id, token, env)
                        part.resource_ids = result.resource_ids
                        set_content(part, result.content, via="token")
                        extracted += bool(result.content)
                        empty += not result.content
                    else:
                        part.resource_ids = fetch_resource_ids(http, part.unit_id, token, env)
                elif is_reading and part.resource_ids:
                    # No token but we have ids → admin panel (cheat-sheet content).
                    content = fetch_admin_content(admin_client(env), part.resource_ids)
                    set_content(part, content, via="admin")
                    via_admin += 1
                    extracted += bool(content)
                    empty += not content
                elif is_reading:
                    # No token and no stored ids → cannot extract this one.
                    raise RuntimeError(
                        f"No Bearer token for {env} and no stored learning resource id."
                    )
                # else: non-reading learning set without a token — ids only come
                # from the token API, so there's nothing to do this run.
            except Exception as err:  # noqa: BLE001 — record per-part failure, continue
                if is_reading:
                    part.content_status = "ERROR"
                    part.content_error = str(err)
                    part.content_extracted_at = _now()
                failed += 1
            if idx % 3 == 0 or idx == total:
                report(f"Processed {idx}/{total} learning set(s)…")

        # Mark the triggering course as having a completed extraction.
        root.content_extracted_at = _now()

        job.status = SyncJob.SUCCESS
        job.message = (
            f"{total} sets: {extracted} extracted, {empty} empty, {failed} failed."
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
