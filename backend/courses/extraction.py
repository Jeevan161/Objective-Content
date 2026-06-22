"""Reading-material extraction: fetch + clean + store session content.

Walks a course and all its prerequisites (recursively), and for every Session's
"Reading Material" part fetches the Markdown content from the CCBP learning API
(using a caller-supplied Bearer token) and stores the cleaned content on the
UnitPart. Progress is reported on the SyncJob row so the frontend can poll it.

The auth token is passed in memory only — it is never written to the DB.
"""
import requests
from django.utils import timezone
from requests.adapters import HTTPAdapter

from portal.learning_resource import fetch_reading_material

from .models import Course, SyncJob, Unit, UnitPart

READING_MATERIAL_LABEL = "Reading Material"
_POOL_SIZE = 16


def collect_courses_recursive(course: Course) -> list:
    """Return [course, …all prerequisites recursively…], de-duplicated, cycle-safe."""
    seen, ordered, stack = set(), [], [course]
    while stack:
        c = stack.pop()
        if c.course_id in seen:
            continue
        seen.add(c.course_id)
        ordered.append(c)
        stack.extend(c.prerequisites.all())
    return ordered


def required_environments(course: Course) -> list:
    """Distinct environments across the course + all its prerequisites."""
    envs = {(c.environment or "PROD").upper() for c in collect_courses_recursive(course)}
    return sorted(envs)


def _reading_material_parts(course: Course):
    """All "Reading Material" parts across the course's Session containers."""
    return list(
        UnitPart.objects.filter(
            container__topic__course=course,
            container__kind=Unit.SESSION,
            label=READING_MATERIAL_LABEL,
        )
    )


def run_extraction_job(job_id, tokens):
    """Execute an extraction job end to end (intended to run in a worker thread).

    ``tokens`` is a {ENVIRONMENT: bearer_token} map; each course's reading
    material is fetched with the token for that course's environment.
    """
    tokens = {(k or "").upper(): v for k, v in (tokens or {}).items()}
    try:
        job = SyncJob.objects.get(id=job_id)
    except SyncJob.DoesNotExist:
        return

    def report(message, status=SyncJob.RUNNING):
        SyncJob.objects.filter(id=job_id).update(
            status=status, message=message, updated_at=timezone.now()
        )

    try:
        root = Course.objects.filter(course_id=job.course_id).first()
        if not root:
            SyncJob.objects.filter(id=job_id).update(
                status=SyncJob.FAILURE, error="Course not found.", updated_at=timezone.now()
            )
            return

        report("Collecting course + prerequisites…")
        courses = collect_courses_recursive(root)
        # (part, environment) pairs so each fetch uses the right base + token.
        items = [
            (p, (c.environment or "PROD").upper())
            for c in courses
            for p in _reading_material_parts(c)
        ]
        total = len(items)
        report(f"Found {len(courses)} course(s), {total} reading material(s). Extracting…")

        session = requests.Session()
        adapter = HTTPAdapter(pool_connections=_POOL_SIZE, pool_maxsize=_POOL_SIZE)
        session.mount("https://", adapter)
        session.mount("http://", adapter)

        extracted = empty = failed = 0
        for idx, (part, env) in enumerate(items, start=1):
            token = tokens.get(env)
            try:
                if not token:
                    raise RuntimeError(f"No authorization token provided for {env}.")
                content = fetch_reading_material(session, part.unit_id, token, env)
                if content:
                    status = "EXTRACTED"
                    extracted += 1
                else:
                    status = "EMPTY"
                    empty += 1
                UnitPart.objects.filter(pk=part.pk).update(
                    content=content,
                    content_status=status,
                    content_error="",
                    content_extracted_at=timezone.now(),
                )
            except Exception as err:
                failed += 1
                UnitPart.objects.filter(pk=part.pk).update(
                    content_status="ERROR", content_error=str(err),
                    content_extracted_at=timezone.now(),
                )
            if idx % 3 == 0 or idx == total:
                report(f"Extracted {idx}/{total} reading material(s)…")

        # Mark the triggering course as having a completed extraction.
        Course.objects.filter(course_id=root.course_id).update(
            content_extracted_at=timezone.now()
        )

        SyncJob.objects.filter(id=job_id).update(
            status=SyncJob.SUCCESS,
            message=f"Extracted {extracted}, empty {empty}, failed {failed} "
                    f"across {len(courses)} course(s).",
            updated_at=timezone.now(),
        )
    except Exception as err:
        SyncJob.objects.filter(id=job_id).update(
            status=SyncJob.FAILURE, error=str(err), updated_at=timezone.now()
        )
