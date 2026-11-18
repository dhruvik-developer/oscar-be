"""P2: an expired access token is renewed with the refresh token, so a signed-in
user is not logged out every ACCESS_TOKEN_LIFETIME (60 min).

The frontend (services/api.js) does this on the first 401: it posts the stored
refresh token to /auth/refresh/ once and replays the request with the new
access token. These tests pin the server half of that contract.
"""
import uuid
from datetime import timedelta

from django.test import TestCase, override_settings
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from apps.accounts.models import Client, Role, User


def uid(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


class TokenRefreshTests(TestCase):
    def setUp(self):
        self.tenant = Client.objects.create(slug=uid("refresh"), name="Refresh Tenant")
        self.email = f"{uid('u')}@example.com"
        role = Role.objects.create(client=self.tenant, code="STAFF", name="Staff")
        User.objects.create_user(email=self.email, password="Pass1234!", client=self.tenant, name="Refresh User", role=role)
        self.api = APIClient()

    def login(self):
        resp = self.api.post("/api/v1/auth/login/", {"email": self.email, "password": "Pass1234!", "role": "STAFF"}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        return resp.data["access"], resp.data["refresh"]

    def me(self, access):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        return client.get("/api/v1/auth/me/")

    def expired(self, access):
        token = AccessToken(access)
        token.set_exp(lifetime=-timedelta(minutes=1))
        return str(token)

    def test_expired_access_is_401_and_refresh_rotates_tokens(self):
        access, refresh = self.login()
        self.assertEqual(self.me(access).status_code, 200)
        self.assertEqual(self.me(self.expired(access)).status_code, 401)

        resp = self.api.post("/api/v1/auth/refresh/", {"refresh": refresh}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        self.assertEqual(self.me(resp.data["access"]).status_code, 200)

        # Token rotation: new refresh token is issued
        new_refresh = resp.data.get("refresh")
        self.assertIsNotNone(new_refresh)
        self.assertNotEqual(refresh, new_refresh)

        # New refresh token works
        again = self.api.post("/api/v1/auth/refresh/", {"refresh": new_refresh}, format="json")
        self.assertEqual(again.status_code, 200, again.content[:300])
        self.assertIn("access", again.data)
        self.assertIn("refresh", again.data)

    def test_reusing_old_refresh_token_triggers_reuse_detection_and_revokes_session(self):
        access, refresh = self.login()
        resp = self.api.post("/api/v1/auth/refresh/", {"refresh": refresh}, format="json")
        self.assertEqual(resp.status_code, 200)
        new_refresh = resp.data["refresh"]

        # Presenting the old rotated refresh token triggers reuse detection
        reuse_attempt = self.api.post("/api/v1/auth/refresh/", {"refresh": refresh}, format="json")
        self.assertEqual(reuse_attempt.status_code, 401)
        self.assertEqual(reuse_attempt.data["code"], "TOKEN_REVOKED")

        # The entire session was revoked, so the new refresh token is also revoked
        second_attempt = self.api.post("/api/v1/auth/refresh/", {"refresh": new_refresh}, format="json")
        self.assertEqual(second_attempt.status_code, 401)
        self.assertEqual(second_attempt.data["code"], "TOKEN_REVOKED")

    def test_inactive_workspace_fails_refresh(self):
        access, refresh = self.login()
        self.tenant.status = "Suspended"
        self.tenant.save(update_fields=["status"])

        resp = self.api.post("/api/v1/auth/refresh/", {"refresh": refresh}, format="json")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.data["code"], "TENANT_INACTIVE")

    def test_role_change_revokes_active_session(self):
        access, refresh = self.login()
        user = User.objects.get(email=self.email)
        new_role = Role.objects.create(client=self.tenant, code="MANAGER", name="Manager")

        # Updating user role
        from apps.accounts.serializers import UserSerializer
        serializer = UserSerializer(user, data={"roleId": new_role.id}, partial=True)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()

        # Session should be revoked immediately
        resp = self.api.post("/api/v1/auth/refresh/", {"refresh": refresh}, format="json")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.data["code"], "TOKEN_REVOKED")

    def test_permission_override_revokes_active_session(self):
        access, refresh = self.login()
        user = User.objects.get(email=self.email)
        admin = User.objects.create_user(
            email=f"admin_{uid('u')}@example.com",
            password="Pass1234!",
            client=self.tenant,
            name="Admin",
            is_superuser=True,
        )
        from apps.accounts.authentication import build_tokens
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(admin)['access']}")
        resp = client.post(
            f"/api/v1/admin/users/{user.id}/permissions/",
            {"grant": ["view_sales"], "deny": []},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content[:300])

        # User's session should be revoked immediately
        refresh_resp = self.api.post("/api/v1/auth/refresh/", {"refresh": refresh}, format="json")
        self.assertEqual(refresh_resp.status_code, 401)
        self.assertEqual(refresh_resp.data["code"], "TOKEN_REVOKED")

    def test_bad_or_signed_out_refresh_token_is_401(self):
        resp = self.api.post("/api/v1/auth/refresh/", {"refresh": "not-a-token"}, format="json")
        self.assertEqual(resp.status_code, 401)

        access, refresh = self.login()
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        client.post("/api/v1/auth/logout/", {"refresh": refresh}, format="json")
        resp = self.api.post("/api/v1/auth/refresh/", {"refresh": refresh}, format="json")
        self.assertEqual(resp.status_code, 401, resp.content[:300])
