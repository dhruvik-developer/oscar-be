"""PMS routes (api.md §10)."""
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter(trailing_slash=True)
router.register("departments", views.DepartmentViewSet, basename="pms-departments")
router.register("stage-configs", views.StageConfigViewSet, basename="pms-stage-configs")
router.register("projects", views.ProjectViewSet, basename="pms-projects")
router.register("delays", views.DelayViewSet, basename="pms-delays")

urlpatterns = [
    path("settings/", views.PmsSettingsView.as_view(), name="pms-settings"),
    path("my-tasks/", views.MyTasksView.as_view(), name="pms-my-tasks"),
    path("my-projects/", views.MyProjectsView.as_view(), name="pms-my-projects"),
    path("tasks/", views.AllTasksView.as_view(), name="pms-tasks"),
    path("nav-badges/", views.NavBadgesView.as_view(), name="pms-nav-badges"),
    path("activity/", views.PmsActivityView.as_view(), name="pms-activity"),
    path("timeline/", views.PmsTimelineView.as_view(), name="pms-timeline"),
    path("dashboard/", views.PmsDashboardView.as_view(), name="pms-dashboard"),
    path(
        "dashboard/<str:section>/",
        views.PmsDashboardView.as_view(),
        name="pms-dashboard-section",
    ),
    path("reports/<str:report_key>/", views.PmsReportView.as_view(), name="pms-reports"),
    path(
        "documents/<uuid:doc_id>/shares/",
        views.DocumentSharesView.as_view(),
        name="pms-document-shares",
    ),
    path(
        "shares/<str:token>/revoke/",
        views.RevokeShareView.as_view(),
        name="pms-share-revoke",
    ),
    path("", include(router.urls)),
]
