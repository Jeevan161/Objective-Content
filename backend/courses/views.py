from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.generics import ListAPIView, RetrieveAPIView
from rest_framework.response import Response

from .models import Course, SyncJob
from .serializers import (
    CourseDetailSerializer,
    CourseListSerializer,
    SyncJobSerializer,
)
from portal.constants import ENVIRONMENTS

from .extraction import required_environments
from .services import get_course_versions
from .tasks import start_extraction_job, start_sync_job


def _clean_environment(value):
    """Normalize/validate an environment name; returns (env, error_response)."""
    env = (value or "PROD").strip().upper()
    if env not in ENVIRONMENTS:
        valid = ", ".join(ENVIRONMENTS)
        return None, Response(
            {"detail": f"Invalid environment '{env}'. Valid: {valid}."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    return env, None


@api_view(["POST"])
def fetch_versions(request):
    """Step 1: given a course_id, return its available versions for the popup."""
    course_id = (request.data.get("course_id") or "").strip()
    if not course_id:
        return Response({"detail": "course_id is required."}, status=status.HTTP_400_BAD_REQUEST)

    environment, err = _clean_environment(request.data.get("environment"))
    if err:
        return err

    try:
        versions = get_course_versions(course_id, environment=environment)
    except Exception as err:
        return Response({"detail": f"Failed to fetch versions: {err}"},
                        status=status.HTTP_502_BAD_GATEWAY)

    return Response({"course_id": course_id, "environment": environment, "versions": versions})


@api_view(["POST"])
def start_sync(request):
    """Step 2 / Sync: start a background fetch for a course + chosen version.

    If no version is supplied (e.g. the Sync button on an existing course),
    the course's previously selected version is reused.
    """
    course_id = (request.data.get("course_id") or "").strip()
    if not course_id:
        return Response({"detail": "course_id is required."}, status=status.HTTP_400_BAD_REQUEST)

    courseversion_id = (request.data.get("courseversion_id") or "").strip()
    version_id = (request.data.get("version_id") or "").strip()
    is_latest = bool(request.data.get("is_latest_version", False))

    environment, err = _clean_environment(request.data.get("environment"))
    if err:
        return err
    env_provided = bool(request.data.get("environment"))
    prerequisite_for = (request.data.get("prerequisite_for") or "").strip()

    if not courseversion_id:
        existing = Course.objects.filter(course_id=course_id).first()
        if existing and existing.selected_courseversion_id:
            courseversion_id = existing.selected_courseversion_id
            version_id = existing.selected_version_id
            is_latest = existing.is_latest_version
            # Reuse the stored environment unless the caller overrode it.
            if not env_provided and existing.environment:
                environment = existing.environment

    job = SyncJob.objects.create(
        course_id=course_id,
        environment=environment,
        prerequisite_for=prerequisite_for,
        courseversion_id=courseversion_id,
        version_id=version_id,
        is_latest_version=is_latest,
    )
    start_sync_job(job.id)
    return Response(SyncJobSerializer(job).data, status=status.HTTP_202_ACCEPTED)


@api_view(["GET"])
def extract_info(request, course_id):
    """Environments needed to extract a course + its prerequisites (so the UI can
    prompt for one Bearer token per environment)."""
    course = Course.objects.filter(course_id=course_id).first()
    if not course:
        return Response({"detail": "Course not found."}, status=status.HTTP_404_NOT_FOUND)
    return Response({
        "course_id": course_id,
        "environments": required_environments(course),
    })


@api_view(["POST"])
def extract_content(request):
    """Start a background reading-material extraction for a course + its
    prerequisites (recursive). Requires a learning-API Bearer token PER
    environment, used for this run only and never stored."""
    course_id = (request.data.get("course_id") or "").strip()
    if not course_id:
        return Response({"detail": "course_id is required."}, status=status.HTTP_400_BAD_REQUEST)
    course = Course.objects.filter(course_id=course_id).first()
    if not course:
        return Response({"detail": "Course not found."}, status=status.HTTP_404_NOT_FOUND)

    # tokens: {ENVIRONMENT: bearer_token}. Must cover every environment involved.
    raw_tokens = request.data.get("tokens") or {}
    if not isinstance(raw_tokens, dict):
        return Response({"detail": "tokens must be an object {ENV: token}."},
                        status=status.HTTP_400_BAD_REQUEST)
    tokens = {k.upper(): v.strip() for k, v in raw_tokens.items() if isinstance(v, str) and v.strip()}

    needed = required_environments(course)
    missing = [env for env in needed if env not in tokens]
    if missing:
        return Response(
            {"detail": f"Authorization token required for: {', '.join(missing)}."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    job = SyncJob.objects.create(course_id=course_id, job_type=SyncJob.EXTRACT)
    start_extraction_job(job.id, tokens)
    return Response(SyncJobSerializer(job).data, status=status.HTTP_202_ACCEPTED)


@api_view(["POST"])
def build_rag(request):
    """Placeholder — RAG building is not implemented yet (extraction-first)."""
    course_id = (request.data.get("course_id") or "").strip()
    course = Course.objects.filter(course_id=course_id).first()
    if not course:
        return Response({"detail": "Course not found."}, status=status.HTTP_404_NOT_FOUND)
    if not course.content_extracted_at:
        return Response(
            {"detail": "Extract the learning resource content first."},
            status=status.HTTP_409_CONFLICT,
        )
    return Response(
        {"detail": "RAG building is not implemented yet — content extraction is the current focus."},
        status=status.HTTP_501_NOT_IMPLEMENTED,
    )


@api_view(["GET"])
def job_status(request, job_id):
    """Poll a sync job's progress."""
    try:
        job = SyncJob.objects.get(id=job_id)
    except SyncJob.DoesNotExist:
        return Response({"detail": "Job not found."}, status=status.HTTP_404_NOT_FOUND)
    return Response(SyncJobSerializer(job).data)


class CourseListView(ListAPIView):
    # Only top-level courses — those that are not a prerequisite of another course
    # (prerequisites are shown nested inside their parent instead).
    queryset = (
        Course.objects.filter(required_by__isnull=True).order_by("course_name").distinct()
    )
    serializer_class = CourseListSerializer


class CourseDetailView(RetrieveAPIView):
    queryset = Course.objects.all()
    serializer_class = CourseDetailSerializer
    lookup_field = "course_id"
