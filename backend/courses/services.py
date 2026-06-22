"""Glue between the portal fetch layer and the Django models."""
from django.db import transaction
from django.utils import timezone

from portal.client import PortalClient
from portal.fetch import build_course_data, fetch_course_versions

from .models import Course, SyncJob, Topic, Unit, UnitPart


def get_course_versions(course_id, environment="PROD"):
    """Quick, synchronous fetch of the available versions for a course id."""
    client = PortalClient(environment=environment)
    client.login()
    return fetch_course_versions(client, course_id)


@transaction.atomic
def _persist_course_data(course_id, data, version_row, environment="PROD"):
    """Replace any stored topics/units for the course with freshly fetched data."""
    details = data.get("course_details", {})

    course, _ = Course.objects.update_or_create(
        course_id=course_id,
        defaults={
            "environment": environment,
            "course_name": details.get("course_name", ""),
            "description": details.get("description", ""),
            "duration": str(details.get("duration", "")),
            "multimedia_url": details.get("multimedia_url", ""),
            "course_category": details.get("course_category", ""),
            "course_link": details.get("course_link", ""),
            "selected_courseversion_id": (version_row or {}).get("row_id", ""),
            "selected_version_id": (version_row or {}).get("version_id", ""),
            "is_latest_version": bool((version_row or {}).get("is_latest_version", False)),
            "last_synced_at": timezone.now(),
        },
    )

    # Rebuild the hierarchy from scratch to avoid stale rows.
    course.topics.all().delete()
    for t_order, topic in enumerate(data.get("topics", [])):
        topic_obj = Topic.objects.create(
            course=course,
            topic_id=topic.get("topic_id", ""),
            topic_name=topic.get("topic_name", ""),
            topic_link=topic.get("topic_link", ""),
            order=t_order,
        )
        for u_order, container in enumerate(topic.get("units", [])):
            unit_obj = Unit.objects.create(
                topic=topic_obj,
                kind=container.get("kind", Unit.SINGLE),
                label=container.get("label", ""),
                order=u_order,
            )
            UnitPart.objects.bulk_create([
                UnitPart(
                    container=unit_obj,
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

    return course


def run_sync_job(job_id):
    """Execute a sync job end to end, updating its status as it goes.

    Intended to run in a background thread. Reports progress onto the SyncJob row
    so the frontend can poll it.
    """
    try:
        job = SyncJob.objects.get(id=job_id)
    except SyncJob.DoesNotExist:
        return

    def report(message):
        SyncJob.objects.filter(id=job_id).update(
            status=SyncJob.RUNNING, message=message, updated_at=timezone.now()
        )

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
            client,
            job.course_id,
            selected_version_row=version_row,
            progress=report,
        )

        report("Saving to database…")
        _persist_course_data(job.course_id, data, version_row, environment=environment)

        # If this sync was for a prerequisite, link it to its parent course.
        if job.prerequisite_for:
            parent = Course.objects.filter(course_id=job.prerequisite_for).first()
            prereq = Course.objects.filter(course_id=job.course_id).first()
            if parent and prereq and parent.pk != prereq.pk:
                parent.prerequisites.add(prereq)

        topic_count = len(data.get("topics", []))
        unit_count = sum(len(t.get("units", [])) for t in data.get("topics", []))
        SyncJob.objects.filter(id=job_id).update(
            status=SyncJob.SUCCESS,
            message=f"Saved {topic_count} topic(s) and {unit_count} unit(s).",
            updated_at=timezone.now(),
        )
    except Exception as err:
        SyncJob.objects.filter(id=job_id).update(
            status=SyncJob.FAILURE, error=str(err), updated_at=timezone.now()
        )
