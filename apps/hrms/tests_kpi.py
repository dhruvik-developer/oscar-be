"""KPI Data create endpoint (HRMS -> Performance -> KPI Data).

Regression cover for the ``source='designation'`` binding assertion that made
``POST /hrms/performance/kpis/`` return 500 for every create.
"""
import uuid

from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, Role, RolePermission, User
from apps.accounts.permission_catalogue import seed_roles, sync_permissions
from apps.hrms import services
from apps.hrms.models import Kpi


def uid(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


class KpiCreateTests(TestCase):
    def setUp(self):
        self.tenant = Client.objects.create(slug=uid("hr"), name="HR Tenant")
        sync_permissions()
        seed_roles(self.tenant)
        role = Role.objects.create(client=self.tenant, code=uid("ROLE"), name=uid("Role"))
        for permission_id in ("edit_staff", "view_staff"):
            RolePermission.objects.create(role=role, permission_id=permission_id)
        self.user = User.objects.create_user(
            email=f"{uid('u')}@example.com", password="pass", client=self.tenant,
            name="HR Manager", role=role,
        )
        services.sync_employee_from_user(self.user)
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(self.user)['access']}")

    def payload(self, **overrides):
        body = {
            "name": "On-time delivery",
            "department": "Engineering",
            "designation": "Senior Engineer",
            "assignedEmployee": "All in Department",
            "target": "\u2265 90%",
            "measurementType": "Percentage",
            "weight": 15,
            "status": "Active",
        }
        body.update(overrides)
        return body

    def test_create_returns_201_and_persists_one_row(self):
        response = self.api.post(
            "/api/v1/hrms/performance/kpis/", self.payload(), format="json"
        )
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual(body["name"], "On-time delivery")
        self.assertEqual(body["department"], "Engineering")
        self.assertEqual(body["designation"], "Senior Engineer")
        self.assertEqual(body["assignedEmployee"], "All in Department")
        self.assertEqual(body["measurementType"], "Percentage")
        self.assertEqual(body["status"], "Active")
        self.assertEqual(Kpi.objects.filter(client=self.tenant).count(), 1)
        stored = Kpi.objects.get(client=self.tenant)
        self.assertEqual(str(stored.target), "90.00")
        self.assertEqual(str(stored.weight), "15.00")

    def test_created_kpi_is_listed(self):
        created = self.api.post(
            "/api/v1/hrms/performance/kpis/", self.payload(), format="json"
        )
        listing = self.api.get("/api/v1/hrms/performance/kpis/")
        self.assertEqual(listing.status_code, 200)
        ids = [row["id"] for row in listing.json()["results"]]
        self.assertIn(created.json()["id"], ids)

    def test_weight_out_of_range_is_rejected(self):
        response = self.api.post(
            "/api/v1/hrms/performance/kpis/", self.payload(weight=150), format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("weight", response.json()["field_errors"])
        self.assertEqual(Kpi.objects.count(), 0)

    def test_target_without_number_is_rejected(self):
        response = self.api.post(
            "/api/v1/hrms/performance/kpis/", self.payload(target="excellent"), format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("target", response.json()["field_errors"])
        self.assertEqual(Kpi.objects.count(), 0)