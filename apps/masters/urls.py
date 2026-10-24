"""Party routes (api.md §4.1). Items live under /inventory/ (see apps.inventory.urls)."""
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import PartyViewSet

router = DefaultRouter(trailing_slash=True)
router.register("", PartyViewSet, basename="parties")

urlpatterns = [path("", include(router.urls))]
