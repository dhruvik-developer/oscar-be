"""Administration → Roles: create from the screen's payload (name only), and
permission lists sent as `permissions` are saved."""
import uuid

from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, Role, RolePermission, User
from apps.accounts.permission_catalogue import sync_permissions


class RoleAdminTests(TestCase):
    def setUp(self):
        sync_permissions()
        self.tenant = Client.objects.create(slug=f"roles-{uuid.uuid4().hex[:8]}", name="Roles Tenant")
        admin = User.objects.create_user(
            email=f"admin-{uuid.uuid4().hex[:6]}@example.com",
            password="Pass1234!",
            client=self.tenant,
            name="Admin",
            is_superuser=True,
        )
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(admin)['access']}")

    def create(self, **body):
        return self.api.post("/api/v1/admin/roles/", body, format="json")

    def test_role_without_code_gets_one_from_its_name(self):
        resp = self.create(name="employee", description="cvc", permissions=[])
        self.assertEqual(resp.status_code, 201, resp.content[:300])
        self.assertEqual(resp.data["code"], "EMPLOYEE")

        second = self.create(name="Employee")
        self.assertEqual(second.status_code, 201, second.content[:300])
        self.assertEqual(second.data["code"], "EMPLOYEE_2")

        spaced = self.create(name="Sales Executive (North)")
        self.assertEqual(spaced.data["code"], "SALES_EXECUTIVE_NORTH")

    def test_explicit_code_is_still_unique(self):
        self.assertEqual(self.create(name="A", code="ACC").status_code, 201)
        self.assertEqual(self.create(name="B", code="ACC").status_code, 400)

    def test_permissions_sent_as_permissions_are_saved(self):
        resp = self.create(name="Viewer", permissions=["view_alerts"])
        self.assertEqual(resp.status_code, 201, resp.content[:300])
        role = Role.objects.get(pk=resp.data["id"])
        self.assertEqual(
            set(RolePermission.objects.filter(role=role).values_list("permission_id", flat=True)),
            {"view_alerts"},
        )

        patched = self.api.patch(
            f"/api/v1/admin/roles/{role.id}/",
            {"name": "Viewer", "permissions": ["view_alerts", "manage_alerts"]},
            format="json",
        )
        self.assertEqual(patched.status_code, 200, patched.content[:300])
        self.assertEqual(
            set(RolePermission.objects.filter(role=role).values_list("permission_id", flat=True)),
            {"view_alerts", "manage_alerts"},
        )
        role.refresh_from_db()
        self.assertEqual(role.code, "VIEWER", "an update without a code keeps the code")
