import uuid

from django.db import models


class Course(models.Model):
    """A course fetched from the portal, keyed by its portal course UUID."""
    course_id = models.CharField(max_length=64, primary_key=True)
    environment = models.CharField(max_length=16, default="PROD")
    course_name = models.CharField(max_length=500, blank=True)
    description = models.TextField(blank=True)
    duration = models.CharField(max_length=64, blank=True)
    multimedia_url = models.URLField(max_length=1000, blank=True)
    course_category = models.CharField(max_length=255, blank=True)
    course_link = models.URLField(max_length=1000, blank=True)

    # The course version this course's hierarchy was fetched from. Reused by Sync.
    selected_courseversion_id = models.CharField(max_length=64, blank=True)
    selected_version_id = models.CharField(max_length=64, blank=True)
    is_latest_version = models.BooleanField(default=False)

    last_synced_at = models.DateTimeField(null=True, blank=True)
    # Set when a reading-material extraction (this course + its prerequisites)
    # last completed — gates the "Build RAG" action.
    content_extracted_at = models.DateTimeField(null=True, blank=True)

    # Prerequisite courses for this course (directional: X requires Y).
    prerequisites = models.ManyToManyField(
        "self", symmetrical=False, related_name="required_by", blank=True
    )

    def __str__(self):
        return self.course_name or self.course_id


class Topic(models.Model):
    course = models.ForeignKey(Course, related_name="topics", on_delete=models.CASCADE)
    topic_id = models.CharField(max_length=64)
    topic_name = models.CharField(max_length=500, blank=True)
    topic_link = models.URLField(max_length=1000, blank=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order"]

    def __str__(self):
        return self.topic_name or self.topic_id


class Unit(models.Model):
    """A container grouping one or more related portal units (its parts).

    ``kind`` drives the display, grouped by unit type:
      • SESSION  — LEARNING_SET + QUIZ (learning resource, reading material, quizzes)
      • PRACTICE — PRACTICE + QUESTION_SET (MCQ, Coding)
      • SINGLE   — anything else (one part)
    """
    SESSION = "SESSION"
    PRACTICE = "PRACTICE"
    SINGLE = "SINGLE"
    KIND_CHOICES = [
        (SESSION, "Session"),
        (PRACTICE, "Practice"),
        (SINGLE, "Single"),
    ]

    topic = models.ForeignKey(Topic, related_name="units", on_delete=models.CASCADE)
    kind = models.CharField(max_length=16, choices=KIND_CHOICES, default=SINGLE)
    label = models.CharField(max_length=500, blank=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order"]

    def __str__(self):
        return f"{self.label} [{self.kind}]"


class UnitPart(models.Model):
    """One portal unit inside a Unit container (e.g. a quiz 'A', a reading material)."""
    container = models.ForeignKey(Unit, related_name="parts", on_delete=models.CASCADE)
    label = models.CharField(max_length=64, blank=True)
    unit_id = models.CharField(max_length=64)
    unit_type = models.CharField(max_length=64, blank=True)
    name = models.CharField(max_length=500, blank=True)
    link = models.URLField(max_length=1000, blank=True)
    error = models.TextField(blank=True)
    order = models.PositiveIntegerField(default=0)

    # Extracted reading-material content (for parts labelled "Reading Material").
    content = models.TextField(blank=True)
    content_status = models.CharField(max_length=16, blank=True)  # EXTRACTED/EMPTY/ERROR
    content_error = models.TextField(blank=True)
    content_extracted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["order"]

    def __str__(self):
        return f"{self.label}: {self.name or self.unit_id}"


class SyncJob(models.Model):
    """Tracks a background fetch so the frontend can poll for progress."""
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    STATUS_CHOICES = [
        (PENDING, "Pending"),
        (RUNNING, "Running"),
        (SUCCESS, "Success"),
        (FAILURE, "Failure"),
    ]

    SYNC = "SYNC"
    EXTRACT = "EXTRACT"
    JOB_TYPE_CHOICES = [(SYNC, "Sync"), (EXTRACT, "Extract")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job_type = models.CharField(max_length=16, choices=JOB_TYPE_CHOICES, default=SYNC)
    course_id = models.CharField(max_length=64)
    environment = models.CharField(max_length=16, default="PROD")
    # When set, the synced course is linked as a prerequisite of this course_id.
    prerequisite_for = models.CharField(max_length=64, blank=True)
    courseversion_id = models.CharField(max_length=64, blank=True)
    version_id = models.CharField(max_length=64, blank=True)
    is_latest_version = models.BooleanField(default=False)

    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=PENDING)
    message = models.TextField(blank=True)
    error = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.course_id} [{self.status}]"
