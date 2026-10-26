"""Sign-in hardening: immediate revocation, forced password change on the
server, password-change throttle, two-step sign-in, sessions, login history,
helpdesk info."""
import re
import uuid
from datetime import timedelta

from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Client, LoginChallenge, Role, User

LOCMEM = "django.core.mail.backends.locmem.EmailBackend"
PASSWORD = "Right1234!"


def uid(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@override_settings(EMAIL_BACKEND=LOCMEM)
class LoginSecurityTests(TestCase):
    def setUp(self):
        cache.clear()
        self.tenant = Client.objects.create(slug=uid("sec"), name="Oscar Textiles")
        self.email = f"{uid('u')}@example.com"
        self.user = User.objects.create_user(
            email=self.email, password=PASSWORD, client=self.tenant, name="Sec User"
        )

    # -- helpers --------------------------------------------------------------

    def login(self, email=None, password=PASSWORD):
        return APIClient().post(
            "/api/v1/auth/login/", {"email": email or self.email, "password": password}, format="json"
        )

    def signed_in(self, access):
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        return api

    def last_code(self):
        return re.search(r"code is: (\d{6})", mail.outbox[-1].body).group(1)

    # -- revocation takes effect at once --------------------------------------

    def test_logout_kills_the_access_token_immediately(self):
        api = self.signed_in(self.login().data["access"])
        self.assertEqual(api.get("/api/v1/auth/me/").status_code, 200)
        self.assertEqual(api.post("/api/v1/auth/logout/", {}, format="json").status_code, 204)
        resp = api.get("/api/v1/auth/me/")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.data["code"], "TOKEN_REVOKED")

    def test_sign_out_other_devices(self):
        laptop = self.signed_in(self.login().data["access"])
        phone = self.signed_in(self.login().data["access"])
        resp = laptop.post("/api/v1/auth/sessions/revoke-others/", {}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["revoked"], 1)
        self.assertEqual(phone.get("/api/v1/auth/me/").status_code, 401)
        self.assertEqual(laptop.get("/api/v1/auth/me/").status_code, 200)

    def test_password_change_signs_out_other_devices_now(self):
        laptop = self.signed_in(self.login().data["access"])
        phone = self.signed_in(self.login().data["access"])
        changed = laptop.post(
            "/api/v1/auth/change-password/",
            {"currentPassword": PASSWORD, "newPassword": "Brand9876!x"},
            format="json",
        )
        self.assertEqual(changed.status_code, 204)
        self.assertEqual(phone.get("/api/v1/auth/me/").status_code, 401)
        self.assertEqual(laptop.get("/api/v1/auth/me/").status_code, 200)

    # -- forced password change is enforced by the server ----------------------

    def test_admin_set_password_blocks_the_api_until_changed(self):
        self.user.must_change_password = True
        self.user.save(update_fields=["must_change_password"])
        api = self.signed_in(self.login().data["access"])

        blocked = api.get("/api/v1/auth/sessions/")
        self.assertEqual(blocked.status_code, 403)
        self.assertEqual(blocked.data["code"], "PASSWORD_CHANGE_REQUIRED")
        self.assertEqual(api.get("/api/v1/auth/me/").status_code, 200)

        api.post(
            "/api/v1/auth/change-password/",
            {"currentPassword": PASSWORD, "newPassword": "Brand9876!x"},
            format="json",
        )
        self.assertEqual(api.get("/api/v1/auth/sessions/").status_code, 200)

    # -- change-password is throttled per user ----------------------------------

    def test_change_password_guessing_is_throttled(self):
        api = self.signed_in(self.login().data["access"])
        codes = [
            api.post(
                "/api/v1/auth/change-password/",
                {"currentPassword": f"Guess{i}000!", "newPassword": "Brand9876!x"},
                format="json",
            ).status_code
            for i in range(7)
        ]
        self.assertEqual(codes[:5], [400] * 5)
        self.assertEqual(codes[5:], [429, 429])

    # -- two-step sign-in -----------------------------------------------------

    def require_two_factor_by_role(self):
        role = Role.objects.create(client=self.tenant, code="OWN", name="Owner", requires_two_factor=True)
        self.user.role = role
        self.user.save(update_fields=["role"])

    def test_role_with_two_factor_gets_a_code_not_tokens(self):
        self.require_two_factor_by_role()
        resp = self.login()
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data["twoFactorRequired"])
        self.assertNotIn("access", resp.data)
        self.assertIn("•", resp.data["email"])
        self.assertEqual(mail.outbox[-1].to, [self.email])
        self.assertIn("sign-in code", mail.outbox[-1].subject)

        verify = lambda code: APIClient().post(  # noqa: E731
            "/api/v1/auth/login/verify-otp/",
            {"challengeId": resp.data["challengeId"], "otp": code},
            format="json",
        )
        code = self.last_code()
        wrong = verify(f"{(int(code) + 1) % 10**6:06d}")
        self.assertEqual(wrong.status_code, 400)
        self.assertEqual(wrong.data["code"], "OTP_INVALID")
        ok = verify(code)
        self.assertEqual(ok.status_code, 200, ok.content[:300])
        self.assertIn("access", ok.data)
        self.assertEqual(self.signed_in(ok.data["access"]).get("/api/v1/auth/me/").status_code, 200)
        self.assertEqual(verify(code).status_code, 400, "single use")

    def test_wrong_password_never_sends_a_code(self):
        self.require_two_factor_by_role()
        self.assertEqual(self.login(password="Nope0000!").status_code, 401)
        self.assertEqual(len(mail.outbox), 0)

    def test_five_wrong_codes_burn_the_challenge(self):
        self.require_two_factor_by_role()
        challenge = self.login().data["challengeId"]
        code = self.last_code()
        for _ in range(LoginChallenge.MAX_ATTEMPTS):
            APIClient().post(
                "/api/v1/auth/login/verify-otp/",
                {"challengeId": challenge, "otp": f"{(int(code) + 1) % 10**6:06d}"},
                format="json",
            )
        right = APIClient().post(
            "/api/v1/auth/login/verify-otp/", {"challengeId": challenge, "otp": code}, format="json"
        )
        self.assertEqual(right.status_code, 400)

    def test_resend_has_a_cooldown_and_replaces_the_code(self):
        self.require_two_factor_by_role()
        first = self.login().data["challengeId"]
        old_code = self.last_code()
        resend = lambda cid: APIClient().post(  # noqa: E731
            "/api/v1/auth/login/resend-otp/", {"challengeId": cid}, format="json"
        )
        self.assertEqual(resend(first).data["code"], "OTP_RESEND_REFUSED")
        LoginChallenge.objects.filter(pk=first).update(created_at=timezone.now() - timedelta(seconds=31))
        again = resend(first)
        self.assertEqual(again.status_code, 200, again.content[:300])
        self.assertNotEqual(again.data["challengeId"], first)
        old = APIClient().post(
            "/api/v1/auth/login/verify-otp/", {"challengeId": first, "otp": old_code}, format="json"
        )
        self.assertEqual(old.status_code, 400, "the earlier code stops working")
        self.assertEqual(resend("not-a-uuid").status_code, 400)

    def test_user_can_opt_in_to_two_factor(self):
        api = self.signed_in(self.login().data["access"])
        resp = api.patch("/api/v1/auth/me/", {"twoFactorEnabled": True}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data["user"]["twoFactorRequired"])
        self.assertTrue(self.login().data["twoFactorRequired"])

    # -- login history ----------------------------------------------------------

    def test_login_history_lists_own_events_only(self):
        self.login(password="Nope0000!")
        api = self.signed_in(self.login().data["access"])
        other = User.objects.create_user(
            email=f"{uid('o')}@example.com", password=PASSWORD, client=self.tenant, name="Other"
        )
        self.login(email=other.email)

        rows = api.get("/api/v1/auth/login-history/").data["results"]
        actions = [row["action"] for row in rows]
        self.assertEqual(actions[:2], ["login", "login_failed"])
        self.assertEqual(len(actions), 2, "another user's sign-in is not listed")

    # -- helpdesk ---------------------------------------------------------------

    @override_settings(SUPPORT_EMAIL="help@oscar.test", SUPPORT_PHONE="+91 90000 00000", SUPPORT_HOURS="")
    def test_support_info_is_public_and_from_settings(self):
        resp = APIClient().get("/api/v1/auth/support/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, {"email": "help@oscar.test", "phone": "+91 90000 00000", "hours": ""})
