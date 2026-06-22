from django.contrib import admin

from .models import Course, SyncJob, Topic, Unit, UnitPart


class UnitPartInline(admin.TabularInline):
    model = UnitPart
    extra = 0


class UnitInline(admin.TabularInline):
    model = Unit
    extra = 0


class TopicInline(admin.TabularInline):
    model = Topic
    extra = 0


@admin.register(Course)
class CourseAdmin(admin.ModelAdmin):
    list_display = ("course_id", "environment", "course_name", "selected_version_id", "last_synced_at")
    search_fields = ("course_id", "course_name")
    inlines = [TopicInline]


@admin.register(Topic)
class TopicAdmin(admin.ModelAdmin):
    list_display = ("topic_id", "topic_name", "course", "order")
    search_fields = ("topic_id", "topic_name")
    inlines = [UnitInline]


@admin.register(Unit)
class UnitAdmin(admin.ModelAdmin):
    list_display = ("label", "kind", "topic", "order")
    list_filter = ("kind",)
    inlines = [UnitPartInline]


@admin.register(SyncJob)
class SyncJobAdmin(admin.ModelAdmin):
    list_display = ("id", "course_id", "status", "message", "created_at")
    list_filter = ("status",)
