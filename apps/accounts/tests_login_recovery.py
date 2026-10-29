"""Login-page flows: workspace choice, forgot password, reset by emailed link."""
import re
import uuid
from datetime import timedelta

from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Client, PasswordResetToken, Role, User
from apps.accounts.serializers import UserSerializer
from apps.accounts.views import MAX_FAILED_LOGINS


def uid(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


LOCMEM = "django.core.mail.backends.locmem.EmailBackend"


def staff_role(tenant):
    """Every sign-in names the user's role, so test users hold one."""
    return Role.objects.get_or_create(client=tenant, code="STAFF", defaults={"name": "Staff"})[0]


@override_settings(EMAIL_BACKEND=LOCMEM, FRONTEND_URL="https://erp.example.com")
class LoginRecoveryTests(TestCase):
    def setUp(self):
        cache.clear()  # the login throttle counts per IP across tests
        self.api = APIClient()
        self.email = f"{uid('u')}@example.com"
        self.a = Client.objects.create(slug=uid("alpha"), name="Alpha Textiles")
        self.b = Client.objects.create(slug=uid("beta"), name="Beta Fabrics")
        self.user_a = User.objects.create_user(
            email=self.email, password="Alpha1234!", client=self.a, name="Asha", role=staff_role(self.a)
        )

    def login(self, password, tenant=None):
        body = {"email": self.email, "password": password, "role": "STAFF"}
        if tenant:
            body["tenant"] = tenant
        return self.api.post("/api/v1/auth/login/", body, format="json")

    def add_second_account(self, password):
        return User.objects.create_user(
            email=self.email, password=password, client=self.b, name="Asha B", role=staff_role(self.b)
        )

    # -- workspace choice ---------------------------------------------------

    def test_shared_email_with_one_matching_password_signs_in(self):
        self.add_second_account("Other9876!")
        resp = self.login("Alpha1234!")
        self.assertEqual(resp.status_code, 200, resp.content[:300])

    def test_shared_email_and_password_asks_for_workspace(self):
        self.add_second_account("Alpha1234!")
        resp = self.login("Alpha1234!")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.data["code"], "TENANT_REQUIRED")
        slugs = {w["slug"] for w in resp.data["payload"]["workspaces"]}
        self.assertEqual(slugs, {self.a.slug, self.b.slug})

        picked = self.login("Alpha1234!", tenant=self.b.slug)
        self.assertEqual(picked.status_code, 200, picked.content[:300])

    def test_shared_email_wrong_password_reveals_nothing(self):
        self.add_second_account("Other9876!")
        resp = self.login("Wrong0000!")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.data["code"], "INVALID_CREDENTIALS")
        self.assertNotIn("payload", resp.data)

    # -- forgot / reset ------------------------------------------------------

    def forgot(self, email=None):
        return self.api.post(
            "/api/v1/auth/forgot-password/", {"email": email or self.email}, format="json"
        )

    def token_from_mail(self, message):
        match = re.search(r"/reset-password\?token=([\w\-]+)", message.body)
        self.assertIsNotNone(match, message.body)
        return match.group(1)

    def test_forgot_password_emails_a_working_link(self):
        self.assertEqual(self.forgot().status_code, 204)
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, [self.email])
        self.assertIn("https://erp.example.com/reset-password?token=", message.body)

        token = self.token_from_mail(message)
        code = re.search(r"verification code is: (\d{6})", message.body).group(1)
        link_only = self.api.post(
            "/api/v1/auth/reset-password/",
            {"token": token, "newPassword": "Fresh5678!"},
            format="json",
        )
        self.assertEqual(link_only.status_code, 400, "the link alone must not reset")
        self.assertEqual(link_only.data["code"], "OTP_INVALID")
        resp = self.api.post(
            "/api/v1/auth/reset-password/",
            {"token": token, "otp": code, "newPassword": "Fresh5678!"},
            format="json",
        )
        self.assertEqual(resp.status_code, 204, resp.content[:300])
        self.assertEqual(self.login("Fresh5678!").status_code, 200)

        reused = self.api.post(
            "/api/v1/auth/reset-password/",
            {"token": token, "newPassword": "Again5678!"},
            format="json",
        )
        self.assertEqual(reused.status_code, 400)

    def test_forgot_password_unknown_email_is_204_and_sends_nothing(self):
        self.assertEqual(self.forgot("nobody@example.com").status_code, 204)
        self.assertEqual(len(mail.outbox), 0)

    def test_forgot_password_covers_every_workspace(self):
        self.add_second_account("Other9876!")
        self.assertEqual(self.forgot().status_code, 204)
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(PasswordResetToken.objects.filter(user__email=self.email).count(), 2)

    def test_reset_token_is_not_logged(self):
        with self.assertLogs("apps.accounts", level="DEBUG") as logs:
            import logging

            logging.getLogger("apps.accounts").debug("sentinel")
            self.forgot()
        token = self.token_from_mail(mail.outbox[0])
        self.assertFalse(any(token in line for line in logs.output))

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend", EMAIL_HOST="")
    def test_forgot_password_without_email_server_still_204(self):
        self.assertEqual(self.forgot().status_code, 204)
        self.assertEqual(len(mail.outbox), 0)


@override_settings(EMAIL_BACKEND=LOCMEM)
class LockoutAndStatusTests(TestCase):
    def setUp(self):
        cache.clear()
        self.api = APIClient()
        self.tenant = Client.objects.create(slug=uid("lock"), name="Lock Tenant")
        self.email = f"{uid('u')}@example.com"
        self.user = User.objects.create_user(
            email=self.email, password="Right1234!", client=self.tenant, name="Lock User",
            role=staff_role(self.tenant),
        )

    def login(self, password):
        return self.api.post(
            "/api/v1/auth/login/", {"email": self.email, "password": password, "role": "STAFF"}, format="json"
        )

    def test_lock_is_announced_with_wait_time(self):
        for _ in range(MAX_FAILED_LOGINS - 1):
            self.assertEqual(self.login("Wrong0000!").data["code"], "INVALID_CREDENTIALS")
        resp = self.login("Wrong0000!")
        self.assertEqual(resp.data["code"], "ACCOUNT_LOCKED")
        self.assertGreater(resp.data["payload"]["retryAfterSeconds"], 0)
        self.assertIn("15 minutes", resp.data["message"])

    def test_locked_account_rejects_even_the_right_password(self):
        self.user.locked_until = timezone.now() + timedelta(minutes=5)
        self.user.save(update_fields=["locked_until"])
        self.assertEqual(self.login("Right1234!").data["code"], "ACCOUNT_LOCKED")

    def test_expired_lock_starts_a_fresh_count(self):
        self.user.failed_login_count = MAX_FAILED_LOGINS
        self.user.locked_until = timezone.now() - timedelta(minutes=1)
        self.user.save(update_fields=["failed_login_count", "locked_until"])
        self.assertEqual(self.login("Wrong0000!").data["code"], "INVALID_CREDENTIALS")
        self.user.refresh_from_db()
        self.assertEqual(self.user.failed_login_count, 1)

    def test_inactive_status_needs_the_right_password(self):
        self.user.status = "Inactive"
        self.user.save(update_fields=["status"])
        self.assertEqual(self.login("Wrong0000!").data["code"], "INVALID_CREDENTIALS")
        self.assertEqual(self.login("Right1234!").data["code"], "ACCOUNT_INACTIVE")


class MustChangePasswordTests(TestCase):
    def setUp(self):
        cache.clear()
        self.api = APIClient()
        self.tenant = Client.objects.create(slug=uid("mcp"), name="MCP Tenant")

    def test_admin_chosen_password_must_be_changed_then_flag_clears(self):
        email = f"{uid('u')}@example.com"
        serializer = UserSerializer(
            data={"name": "New Joiner", "email": email, "password": "Temp1234!"},
            context={"client_id": self.tenant.id},
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save(client=self.tenant)
        User.objects.filter(email=email).update(role=staff_role(self.tenant))

        resp = self.api.post(
            "/api/v1/auth/login/", {"email": email, "password": "Temp1234!", "role": "STAFF"}, format="json"
        )
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        self.assertTrue(resp.data["user"]["mustChangePassword"])

        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {resp.data['access']}")
        changed = self.api.post(
            "/api/v1/auth/change-password/",
            {"currentPassword": "Temp1234!", "newPassword": "Mine5678!x"},
            format="json",
        )
        self.assertEqual(changed.status_code, 204, changed.content[:300])
        me = self.api.get("/api/v1/auth/me/")
        self.assertFalse(me.data["user"]["mustChangePassword"])

    def test_self_service_users_are_not_flagged(self):
        email = f"{uid('u')}@example.com"
        User.objects.create_user(email=email, password="Own1234!", client=self.tenant, name="Own", role=staff_role(self.tenant))
        resp = self.api.post(
            "/api/v1/auth/login/", {"email": email, "password": "Own1234!", "role": "STAFF"}, format="json"
        )
        self.assertFalse(resp.data["user"]["mustChangePassword"])


@override_settings(EMAIL_BACKEND=LOCMEM, FRONTEND_URL="https://erp.example.com")
class ResetOtpTests(TestCase):
    def setUp(self):
        cache.clear()
        self.api = APIClient()
        self.tenant = Client.objects.create(slug=uid("otp"), name="Oscar Textiles")
        self.email = f"{uid('u')}@example.com"
        self.user = User.objects.create_user(
            email=self.email, password="Old12345!", client=self.tenant, name="Otp User",
            role=staff_role(self.tenant),
        )

    def forgot(self):
        resp = self.api.post("/api/v1/auth/forgot-password/", {"email": self.email}, format="json")
        self.assertEqual(resp.status_code, 204)
        message = mail.outbox[-1]
        match = re.search(r"verification code is: (\d{6})", message.body)
        self.assertIsNotNone(match, message.body)
        return match.group(1), message

    def verify(self, code):
        return self.api.post(
            "/api/v1/auth/verify-reset-otp/", {"email": self.email, "otp": code}, format="json"
        )

    def reset(self, token, password="Fresh5678!", otp=None):
        body = {"token": token, "newPassword": password}
        if otp is not None:
            body["otp"] = otp
        return self.api.post("/api/v1/auth/reset-password/", body, format="json")

    def wrong(self, code):
        return f"{(int(code) + 1) % 10**6:06d}"

    def test_email_carries_code_in_subject_and_body_with_the_link(self):
        code, message = self.forgot()
        self.assertTrue(message.subject.startswith(f"{code} is your Oscar Textiles password reset"))
        self.assertIn("/reset-password?token=", message.body)

    def test_code_exchanges_for_a_token_that_resets_the_password(self):
        code, _ = self.forgot()
        resp = self.verify(f" {code} ")
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        self.assertEqual(self.reset(resp.data["token"]).status_code, 204)
        login = self.api.post(
            "/api/v1/auth/login/", {"email": self.email, "password": "Fresh5678!", "role": "STAFF"}, format="json"
        )
        self.assertEqual(login.status_code, 200)
        # The code is single-use.
        self.assertEqual(self.verify(code).status_code, 400)

    def test_wrong_code_is_rejected_and_five_misses_burn_the_code(self):
        code, _ = self.forgot()
        bad = self.verify(self.wrong(code))
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(bad.data["code"], "OTP_INVALID")
        for _ in range(4):
            self.verify(self.wrong(code))
        self.assertEqual(self.verify(code).status_code, 400, "burnt after 5 misses")

    def test_expired_code_is_rejected(self):
        code, _ = self.forgot()
        PasswordResetToken.objects.filter(user=self.user).update(
            otp_expires_at=timezone.now() - timedelta(seconds=1)
        )
        self.assertEqual(self.verify(code).status_code, 400)

    def test_new_request_replaces_the_old_code_and_link(self):
        old_code, old_message = self.forgot()
        old_token = re.search(r"token=([\w\-]+)", old_message.body).group(1)
        new_code, _ = self.forgot()
        if old_code != new_code:
            self.assertEqual(self.verify(old_code).status_code, 400)
        self.assertEqual(self.reset(old_token).status_code, 400)
        self.assertEqual(self.verify(new_code).status_code, 200)

    def test_link_needs_the_code_and_misses_count(self):
        code, message = self.forgot()
        token = re.search(r"token=([\w\-]+)", message.body).group(1)
        self.assertEqual(self.reset(token).data["code"], "OTP_INVALID")
        self.assertEqual(self.reset(token, otp=self.wrong(code)).data["code"], "OTP_INVALID")
        record = PasswordResetToken.objects.get(user=self.user, used_at__isnull=True)
        self.assertEqual(record.otp_attempts, 2, "misses are committed, not rolled back")
        self.assertEqual(self.reset(token, otp=code).status_code, 204)

    def test_link_dies_when_its_code_is_burnt(self):
        code, message = self.forgot()
        token = re.search(r"token=([\w\-]+)", message.body).group(1)
        for _ in range(PasswordResetToken.OTP_MAX_ATTEMPTS):
            self.reset(token, otp=self.wrong(code))
        self.assertEqual(self.reset(token, otp=code).status_code, 400)

    def test_code_is_not_stored_in_plain_text(self):
        code, _ = self.forgot()
        record = PasswordResetToken.objects.get(user=self.user, used_at__isnull=True)
        self.assertNotIn(code, record.otp_hash)
        self.assertEqual(len(record.otp_hash), 64)
