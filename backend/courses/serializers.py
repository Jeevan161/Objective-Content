from rest_framework import serializers

from .models import Course, SyncJob, Topic, Unit, UnitPart


class UnitPartSerializer(serializers.ModelSerializer):
    has_content = serializers.SerializerMethodField()
    content_chars = serializers.SerializerMethodField()

    class Meta:
        model = UnitPart
        fields = [
            "label", "unit_id", "unit_type", "name", "link", "error", "order",
            "content_status", "has_content", "content_chars",
        ]

    def get_has_content(self, obj):
        return bool(obj.content)

    def get_content_chars(self, obj):
        return len(obj.content or "")


class UnitSerializer(serializers.ModelSerializer):
    parts = UnitPartSerializer(many=True, read_only=True)

    class Meta:
        model = Unit
        fields = ["kind", "label", "order", "parts"]


class TopicSerializer(serializers.ModelSerializer):
    units = UnitSerializer(many=True, read_only=True)

    class Meta:
        model = Topic
        fields = ["topic_id", "topic_name", "topic_link", "order", "units"]


class CourseListSerializer(serializers.ModelSerializer):
    """Summary fields needed to render a (top-level or nested) course card."""
    topic_count = serializers.IntegerField(source="topics.count", read_only=True)
    prerequisite_count = serializers.IntegerField(source="prerequisites.count", read_only=True)

    class Meta:
        model = Course
        fields = [
            "course_id", "environment", "course_name", "course_category", "course_link",
            "selected_version_id", "is_latest_version", "last_synced_at",
            "content_extracted_at", "topic_count", "prerequisite_count",
        ]


class CourseDetailSerializer(serializers.ModelSerializer):
    topics = TopicSerializer(many=True, read_only=True)
    # Nested prerequisites use the list (summary) shape; the frontend renders each
    # as its own card and lazy-loads its full detail on expand.
    prerequisites = CourseListSerializer(many=True, read_only=True)

    class Meta:
        model = Course
        fields = [
            "course_id", "environment", "course_name", "description", "duration",
            "multimedia_url", "course_category", "course_link", "selected_courseversion_id",
            "selected_version_id", "is_latest_version", "last_synced_at",
            "prerequisites", "topics",
        ]


class SyncJobSerializer(serializers.ModelSerializer):
    class Meta:
        model = SyncJob
        fields = [
            "id", "job_type", "course_id", "environment", "version_id", "is_latest_version",
            "status", "message", "error", "created_at", "updated_at",
        ]
