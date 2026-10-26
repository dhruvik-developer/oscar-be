"""A restore file is untrusted input: it must not grant privilege or reach
another tenant's rows."""
import uuid

from django.contrib.auth.hashers import make_password
from django.test import TestCase

from apps.accounts.models import Client, Role, User
from apps.core.snapshot import import_snapshot


def uid(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


class SnapshotRestoreSecurityTests(TestCase):
    def setUp(self):
        self.tenant = Client.objects.create(slug=uid("mine"), name="Mine")
        self.other = Client.objects.create(slug=uid("other"), name="Other")
        self.other_role = Role.objects.create(client=self.other, code="OWN", name="Other owner")

    def user_row(self, **extra):
        return {
            "id": str(uuid.uuid4()),
            "email": f"{uid('x')}@example.com",
            "name": "Restored",
            "status": "Active",
            **extra,
        }

    def test_restored_user_cannot_be_superuser_or_keep_a_password(self):
        row = self.user_row(
            is_superuser=True,
            is_staff=True,
            password=make_password("Known1234!"),
        )
        result = import_snapshot(self.tenant.id, {"tables": {"accounts.User": [row]}})
        self.assertEqual(result["created"], {"accounts.User": 1})

        user = User.objects.get(pk=row["id"])
        self.assertEqual(user.client_id, self.tenant.id)
        self.assertFalse(user.is_superuser)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.has_usable_password())
        self.assertFalse(user.check_password("Known1234!"))

    def test_table_order_cannot_reach_models_outside_the_list(self):
        rogue = {"id": str(uuid.uuid4()), "slug": uid("rogue"), "name": "Rogue tenant"}
        result = import_snapshot(
            self.tenant.id,
            {"tableOrder": ["accounts.Client"], "tables": {"accounts.Client": [rogue]}},
        )
        self.assertEqual(result["created"], {})
        self.assertFalse(Client.objects.filter(pk=rogue["id"]).exists())

    def test_foreign_key_into_another_tenant_is_rejected(self):
        row = self.user_row(role_id=str(self.other_role.id))
        result = import_snapshot(self.tenant.id, {"tables": {"accounts.User": [row]}})
        self.assertEqual(result["rejected"], {"accounts.User": 1})
        self.assertFalse(User.objects.filter(pk=row["id"]).exists())

    def test_foreign_key_to_a_row_in_the_same_snapshot_is_kept(self):
        role_id = str(uuid.uuid4())
        role = {"id": role_id, "code": "ACC", "name": "Accounts"}
        row = self.user_row(role_id=role_id)
        result = import_snapshot(
            self.tenant.id,
            {"tables": {"accounts.Role": [role], "accounts.User": [row]}},
        )
        self.assertEqual(result["created"], {"accounts.Role": 1, "accounts.User": 1})
        self.assertEqual(str(User.objects.get(pk=row["id"]).role_id), role_id)

    def test_reusing_another_tenants_primary_key_is_skipped(self):
        role = {"id": str(self.other_role.id), "code": "OWN", "name": "Hijack"}
        result = import_snapshot(self.tenant.id, {"tables": {"accounts.Role": [role]}})
        self.assertEqual(result["skipped"], {"accounts.Role": 1})
        self.other_role.refresh_from_db()
        self.assertEqual(self.other_role.client_id, self.other.id)
        self.assertEqual(self.other_role.name, "Other owner")
