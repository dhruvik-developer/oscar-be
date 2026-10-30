"""Alert Center screen ↔ API: defaults appear on first read, edits from the
screen save, and checks can be run on demand."""
import uuid

from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, User
from apps.core.alert_services import DEFAULT_ALERT_CONFIGS
from apps.core.models import AlertSetting


class AlertCenterApiTests(TestCase):
    def setUp(self):
        self.tenant = Client.objects.create(slug=f"al-{uuid.uuid4().hex[:8]}", name="Alerts")
        admin = User.objects.create_user(
            email=f"a-{uuid.uuid4().hex[:6]}@example.com", password="Pass1234!",
            client=self.tenant, name="Admin", is_superuser=True,
        )
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(admin)['access']}")

    def settings_rows(self):
        body = self.api.get("/api/v1/alerts/settings/").data
        return body["results"] if isinstance(body, dict) else body

    def test_thresholds_exist_before_any_alert_run(self):
        self.assertEqual(len(self.settings_rows()), len(DEFAULT_ALERT_CONFIGS))
        self.settings_rows()
        self.assertEqual(AlertSetting.objects.filter(client=self.tenant).count(), len(DEFAULT_ALERT_CONFIGS))

    def test_screen_edit_saves_pct_switch_and_channels(self):
        row = next(r for r in self.settings_rows() if r["alert_key"] == "loss_tolerance_exceeded")
        resp = self.api.patch(
            f"/api/v1/alerts/settings/{row['id']}/",
            {"threshold_days": 0, "threshold_pct": 7.5, "is_enabled": False, "channels": ["email"]},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        saved = AlertSetting.objects.get(pk=row["id"])
        self.assertEqual(float(saved.threshold_pct), 7.5)
        self.assertFalse(saved.is_enabled)
        self.assertEqual(saved.channels, ["email"])

    def test_run_checks_on_demand(self):
        resp = self.api.post("/api/v1/alerts/incidents/run/", {}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        self.assertIn("incidentCount", resp.data)
        self.assertEqual(resp.data["errors"], [])
