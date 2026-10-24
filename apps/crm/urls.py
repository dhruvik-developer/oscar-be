"""CRM routes (api.md §9). Paths for leads, tasks and forms match ``crmService``."""
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter(trailing_slash=True)
router.register("leads", views.LeadViewSet, basename="crm-leads")
router.register("stages", views.StageViewSet, basename="crm-stages")
router.register("deal-stages", views.DealStageViewSet, basename="crm-deal-stages")
router.register("stage-tasks", views.StageTaskViewSet, basename="crm-stage-tasks")
router.register("master-tasks", views.MasterTaskViewSet, basename="crm-master-tasks")
router.register("tasks", views.TaskViewSet, basename="crm-tasks")
router.register(
    "task-allocations", views.TaskAllocationViewSet, basename="crm-task-allocations"
)
router.register("deals", views.DealViewSet, basename="crm-deals")
router.register("contracts", views.ContractViewSet, basename="crm-contracts")
router.register("projects", views.CrmProjectViewSet, basename="crm-projects")
router.register("sources", views.SourceViewSet, basename="crm-sources")
router.register("industries", views.IndustryViewSet, basename="crm-industries")
router.register("lost-reasons", views.LostReasonViewSet, basename="crm-lost-reasons")
router.register(
    "user-allocations", views.UserAllocationViewSet, basename="crm-user-allocations"
)
router.register("user-locations", views.UserLocationViewSet, basename="crm-user-locations")
router.register("forms", views.FormViewSet, basename="crm-forms")

urlpatterns = [
    path("team-roster/", views.TeamRosterView.as_view(), name="crm-team-roster"),
    path("setup/", views.CrmSetupView.as_view(), name="crm-setup"),
    path("dashboard/", views.CrmDashboardView.as_view(), name="crm-dashboard"),
    path("reminders/", views.ReminderView.as_view(), name="crm-reminders"),
    path("reports/<str:report_key>/", views.CrmReportView.as_view(), name="crm-reports"),
    path("", include(router.urls)),
]
