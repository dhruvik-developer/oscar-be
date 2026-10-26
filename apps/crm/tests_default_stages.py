"""A workspace with no CRM stages can still save leads: the shipped defaults
are seeded on first need, and a lead without a stage enters the first one."""
import uuid

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, User
from apps.crm.models import DealStage, Lead, Source, Stage
from apps.crm.services import DEFAULT_STAGES


class WorkspaceTestCase(TestCase):
    """A brand-new workspace with an admin signed in."""

    def setUp(self):
        self.tenant = Client.objects.create(slug=f"crm-{uuid.uuid4().hex[:8]}", name="Fresh Workspace")
        admin = User.objects.create_user(
            email=f"admin-{uuid.uuid4().hex[:6]}@example.com",
            password="Pass1234!",
            client=self.tenant,
            name="Admin",
            is_superuser=True,
        )
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(admin)['access']}")


class DefaultStageTests(WorkspaceTestCase):
    def test_lead_saves_in_a_workspace_with_no_stages(self):
        self.assertFalse(Stage.objects.filter(client=self.tenant).exists())
        resp = self.api.post("/api/v1/crm/leads/", {"name": "radha", "company": "Om Textiles"}, format="json")
        self.assertEqual(resp.status_code, 201, resp.content[:400])
        self.assertEqual(resp.data["status"], "New Lead")
        self.assertTrue(Lead.objects.filter(client=self.tenant, name="radha").exists())
        self.assertEqual(Stage.objects.filter(client=self.tenant).count(), len(DEFAULT_STAGES))

    def test_listing_stages_seeds_the_defaults_once(self):
        first = self.api.get("/api/v1/crm/stages/")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.data["count"], len(DEFAULT_STAGES))
        self.api.get("/api/v1/crm/stages/")
        self.assertEqual(Stage.objects.filter(client=self.tenant).count(), len(DEFAULT_STAGES))

    def test_a_workspace_that_removed_its_stages_is_not_reseeded(self):
        Stage.objects.create(client=self.tenant, name="Old", sequence=1, deleted_at=timezone.now())
        resp = self.api.get("/api/v1/crm/stages/")
        self.assertEqual(resp.data["count"], 0)
        self.assertEqual(Stage._base_manager.filter(client=self.tenant).count(), 1)


class DefaultLeadSourceTests(WorkspaceTestCase):
    def test_sources_list_seeds_broker_and_sales_person(self):
        resp = self.api.get("/api/v1/crm/sources/")
        self.assertEqual(resp.status_code, 200)
        names = sorted(row["name"] for row in resp.data["results"])
        self.assertEqual(names, ["Broker", "Sales Person"])
        self.api.get("/api/v1/crm/sources/")
        self.assertEqual(Source.objects.filter(client=self.tenant).count(), 2)

    def test_lead_keeps_the_chosen_source(self):
        broker = next(r for r in self.api.get("/api/v1/crm/sources/").data["results"] if r["name"] == "Broker")
        resp = self.api.post(
            "/api/v1/crm/leads/", {"name": "radha", "sourceId": broker["id"]}, format="json"
        )
        self.assertEqual(resp.status_code, 201, resp.content[:400])
        self.assertEqual(resp.data["source"], "Broker")

    def test_existing_sources_are_left_alone(self):
        Source.objects.create(client=self.tenant, name="Exhibition")
        names = [row["name"] for row in self.api.get("/api/v1/crm/sources/").data["results"]]
        self.assertEqual(names, ["Exhibition"])


class DealStageCatalogueTests(WorkspaceTestCase):
    def test_board_gets_the_pipeline_stages_and_not_stray_ones(self):
        DealStage.objects.create(client=self.tenant, name="frieght", sequence=0)
        resp = self.api.get("/api/v1/crm/deal-stages/")
        self.assertEqual(resp.status_code, 200)
        names = [row["name"] for row in resp.data["results"]]
        self.assertEqual(names, ["Draft", "Sent", "Open", "Won", "Lost"])
        self.api.get("/api/v1/crm/deal-stages/")
        self.assertEqual(DealStage.objects.filter(client=self.tenant, name="Draft").count(), 1)
