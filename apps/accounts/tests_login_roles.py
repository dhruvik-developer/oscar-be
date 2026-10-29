"""The sign-in page's "Select Your Role" list and the role check on sign-in.

    python manage.py test apps.accounts.tests_login_roles
"""
import uuid

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.login_roles import ADMIN_CODE
from apps.accounts.models import Client, Role, User

PASSWORD = "Right1234!"


def uid(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


class LoginRoleTests(TestCase):
    def setUp(self):
        cache.clear()  # login and public throttles count per IP across tests
        self.tenant = Client.objects.create(slug=uid("roles"), name="Roles Tenant")
        self.owner = Role.objects.create(client=self.tenant, code="OWNER", name="Owner")
        self.qc = Role.objects.create(client=self.tenant, code="QC_INSPECTOR", name="QC")
        self.godown = Role.objects.create(client=self.tenant, code="STORE_INCHARGE", name="Godown")
        self.owner_user = self.user("owner", self.owner)
        self.qc_user = self.user("qc", self.qc)

    def user(self, prefix, role=None, **extra):
        return User.objects.create_user(
            email=f"{uid(prefix)}@example.com", password=PASSWORD, client=self.tenant,
            name=prefix.title(), role=role, **extra,
        )

    def options(self, tenant=None):
        query = {"tenant": tenant} if tenant else {}
        resp = APIClient().get("/api/v1/auth/login-roles/", query)
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        return resp.data["roles"]

    def codes(self, tenant=None):
        return [r["code"] for r in self.options(tenant or self.tenant.slug)]

    def login(self, user, role, password=PASSWORD):
        body = {"email": user.email, "password": password}
        if role is not None:
            body["role"] = role
        return APIClient().post("/api/v1/auth/login/", body, format="json")

    # -- the dropdown -----------------------------------------------------------

    def test_only_roles_held_by_active_users_are_offered(self):
        self.assertEqual(self.codes(), ["OWNER", "QC_INSPECTOR"])   # Godown has no user

    def test_role_appears_and_disappears_with_assignments(self):
        clerk = self.user("store", self.godown)
        self.assertIn("STORE_INCHARGE", self.codes())

        clerk.status = "Inactive"
        clerk.save(update_fields=["status"])
        self.assertNotIn("STORE_INCHARGE", self.codes())

        clerk.status = "Active"
        clerk.save(update_fields=["status"])
        self.assertIn("STORE_INCHARGE", self.codes())

        clerk.deleted_at = timezone.now()
        clerk.save(update_fields=["deleted_at"])
        self.assertNotIn("STORE_INCHARGE", self.codes())

        self.qc_user.role = self.godown       # reassigned: QC loses its only holder
        self.qc_user.save(update_fields=["role"])
        self.assertEqual(self.codes(), ["STORE_INCHARGE", "OWNER"])

    def test_inactive_workspace_and_deleted_role_are_not_offered(self):
        self.qc.deleted_at = timezone.now()
        self.qc.save(update_fields=["deleted_at"])
        self.assertEqual(self.codes(), ["OWNER"])
        self.tenant.status = "Inactive"
        self.tenant.save(update_fields=["status"])
        self.assertEqual(self.codes(), [])

    def test_payload_names_roles_only(self):
        roles = self.options(self.tenant.slug)
        self.assertEqual(roles, [{"code": "OWNER", "name": "Owner"}, {"code": "QC_INSPECTOR", "name": "QC"}])
        body = APIClient().get("/api/v1/auth/login-roles/").content.decode()
        self.assertNotIn(self.owner_user.email, body)
        self.assertNotIn("@", body)

    def test_workspace_filter(self):
        other = Client.objects.create(slug=uid("other"), name="Other")
        sales = Role.objects.create(client=other, code="SALES_EXEC", name="Sales")
        User.objects.create_user(email=f"{uid('s')}@example.com", password=PASSWORD, client=other, name="S", role=sales)
        self.assertNotIn("SALES_EXEC", self.codes())
        self.assertEqual(self.codes(other.slug), ["SALES_EXEC"])
        self.assertIn("SALES_EXEC", [r["code"] for r in self.options()])   # no workspace: all

    def test_administrator_is_offered_only_while_an_active_superuser_exists(self):
        self.assertNotIn(ADMIN_CODE, self.codes())
        admin = self.user("admin", is_superuser=True, is_staff=True)
        self.assertEqual(self.options(self.tenant.slug)[-1], {"code": ADMIN_CODE, "name": "Administrator"})
        admin.is_active = False
        admin.save(update_fields=["is_active"])
        self.assertNotIn(ADMIN_CODE, self.codes())

    # -- sign-in ----------------------------------------------------------------

    def test_own_role_signs_in(self):
        resp = self.login(self.qc_user, "QC_INSPECTOR")
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        self.assertEqual(resp.data["user"]["roleCode"], "QC_INSPECTOR")

    def test_another_role_is_refused_without_counting_as_a_wrong_password(self):
        resp = self.login(self.qc_user, "OWNER")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.data["code"], "ROLE_MISMATCH")
        self.assertNotIn("access", resp.data)
        self.qc_user.refresh_from_db()
        self.assertEqual(self.qc_user.failed_login_count, 0)

    def test_role_is_not_revealed_before_the_password(self):
        resp = self.login(self.qc_user, "OWNER", password="Wrong0000!")
        self.assertEqual(resp.data["code"], "INVALID_CREDENTIALS")

    def test_inactive_account_is_refused_even_with_its_role(self):
        self.qc_user.status = "Inactive"
        self.qc_user.save(update_fields=["status"])
        self.assertEqual(self.login(self.qc_user, "QC_INSPECTOR").data["code"], "ACCOUNT_INACTIVE")

    def test_role_is_required(self):
        self.assertIn(self.login(self.qc_user, None).status_code, (400, 422))
        self.assertIn(self.login(self.qc_user, "").status_code, (400, 422))

    def test_ordinary_user_cannot_pick_administrator(self):
        resp = self.login(self.owner_user, ADMIN_CODE)
        self.assertEqual(resp.data["code"], "ROLE_MISMATCH")

    def test_role_less_user_cannot_sign_in(self):
        loose = self.user("loose")
        self.assertEqual(self.login(loose, "OWNER").data["code"], "ROLE_MISMATCH")

    def test_superuser_signs_in_as_administrator_with_full_access(self):
        admin = self.user("admin", is_superuser=True, is_staff=True)
        resp = self.login(admin, ADMIN_CODE)
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        self.assertTrue(len(resp.data["permissions"]) > 0)
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {resp.data['access']}")
        self.assertEqual(api.get("/api/v1/auth/me/").status_code, 200)

    def test_superuser_with_a_role_may_use_it_or_administrator_but_not_another(self):
        admin = self.user("boss", self.owner, is_superuser=True, is_staff=True)
        self.assertEqual(self.login(admin, "OWNER").status_code, 200)
        self.assertEqual(self.login(admin, ADMIN_CODE).status_code, 200)
        self.assertEqual(self.login(admin, "QC_INSPECTOR").data["code"], "ROLE_MISMATCH")

    def test_picked_entry_never_changes_permissions(self):
        """Permissions are the account's own, whichever valid entry was picked."""
        admin = self.user("boss", self.owner, is_superuser=True, is_staff=True)
        as_owner = self.login(admin, "OWNER").data["permissions"]
        as_admin = self.login(admin, ADMIN_CODE).data["permissions"]
        self.assertEqual(sorted(as_owner), sorted(as_admin))
