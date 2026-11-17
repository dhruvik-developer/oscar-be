"""Loom & Weaving Manufacturing URL routes."""
from django.urls import include, path
from rest_framework.routers import DefaultRouter
from . import loom_views as views

router = DefaultRouter(trailing_slash=True)
router.register("coordinators", views.LoomCoordinatorViewSet, basename="loom-coordinators")
router.register("factories", views.LoomFactoryViewSet, basename="loom-factories")
router.register("machines", views.LoomMachineViewSet, basename="loom-machines")
router.register("yarns", views.YarnMasterViewSet, basename="loom-yarns")
router.register("qualities", views.LoomQualityViewSet, basename="loom-qualities")
router.register("beams", views.LoomBeamViewSet, basename="loom-beams")
router.register("daily-production", views.DailyLoomProductionViewSet, basename="loom-daily-production")
router.register("yarn-bookings", views.YarnBookingViewSet, basename="loom-yarn-bookings")
router.register("yarn-deliveries", views.YarnDeliveryViewSet, basename="loom-yarn-deliveries")
router.register("programs", views.ProductionProgramViewSet, basename="loom-programs")
router.register("fabric-dispatches", views.FabricDispatchViewSet, basename="loom-fabric-dispatches")

urlpatterns = [
    path("dashboard-overview/", views.LoomDashboardOverviewView.as_view(), name="loom-dashboard-overview"),
    path("demo-data/", views.LoomDemoDataView.as_view(), name="loom-demo-data"),
    path("", include(router.urls)),
]
