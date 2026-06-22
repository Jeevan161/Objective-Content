from django.urls import path

from . import views

urlpatterns = [
    path("courses/versions/", views.fetch_versions, name="fetch-versions"),
    path("courses/sync/", views.start_sync, name="start-sync"),
    path("courses/<str:course_id>/extract-info/", views.extract_info, name="extract-info"),
    path("courses/extract/", views.extract_content, name="extract-content"),
    path("courses/build-rag/", views.build_rag, name="build-rag"),
    path("courses/jobs/<uuid:job_id>/", views.job_status, name="job-status"),
    path("courses/", views.CourseListView.as_view(), name="course-list"),
    path("courses/<str:course_id>/", views.CourseDetailView.as_view(), name="course-detail"),
]
