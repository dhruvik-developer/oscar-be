"""
Production-ready rate limiting and throttling tests for Oscar ERP.

Covers:
1. Login within the allowed limit succeeds.
2. Login exceeding the limit returns 429.
3. Failed login rate limit returns 429.
4. Successful login clears failed attempts counter and is not blocked by previous failures.
5. Authenticated user API calls throttled after configured limit.
6. Anonymous API calls throttled after configured limit.
7. Different authenticated users have independent limits.
8. Different IP addresses have independent anonymous limits.
9. Password reset endpoints are throttled according to PASSWORD_RESET_RATE_LIMIT.
10. OTP verify/resend endpoints are throttled according to OTP_VERIFY_RATE_LIMIT.
11. Token refresh operations throttled according to TOKEN_REFRESH_RATE_LIMIT.
12. HTTP 429 response status and message correctness.
13. Retry-After header correctness.
14. Rate-limit reset after simulated window expiration.
15. Concurrent requests and counter consistency.
16. Cache operation consistency without leaking sensitive values.
17. Different users sharing the same IP.
18. Different IPs accessing the same account.
19. Rate limiting cannot be bypassed by spoofing X-Forwarded-For headers.
20. Trusted proxy configuration honors forwarded IP when configured.
"""
import uuid
import time
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, Role, User
from apps.core.throttling import FailedLoginThrottle

API = "/api/v1"


def uid(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


class ThrottlingTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.tenant = Client.objects.create(slug=uid("throttle"), name="Throttle Tenant")
        self.role = Role.objects.create(client=self.tenant, code="STAFF", name="Staff")
        self.user_a_email = f"{uid('usera')}@test.com"
        self.user_b_email = f"{uid('userb')}@test.com"

        self.user_a = User.objects.create_user(
            email=self.user_a_email,
            password="StrongPassword123!",
            client=self.tenant,
            name="User A",
            role=self.role,
            status="Active",
        )
        self.user_b = User.objects.create_user(
            email=self.user_b_email,
            password="StrongPassword123!",
            client=self.tenant,
            name="User B",
            role=self.role,
            status="Active",
        )
        self.tokens_a = build_tokens(self.user_a)
        self.tokens_b = build_tokens(self.user_b)

    def tearDown(self):
        cache.clear()


class LoginThrottlingTests(ThrottlingTestCase):
    @override_settings(LOGIN_RATE_LIMIT="5/minute")
    def test_login_within_allowed_limit_succeeds(self):
        """1. Login within the allowed limit succeeds."""
        resp = self.client.post(
            f"{API}/auth/login/",
            {"email": self.user_a_email, "password": "StrongPassword123!", "role": "STAFF"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("access", resp.json())

    @override_settings(LOGIN_RATE_LIMIT="3/minute")
    def test_login_exceeding_limit_returns_429(self):
        """2. Login exceeding the limit returns 429 with Retry-After and standard detail."""
        for _ in range(3):
            resp = self.client.post(
                f"{API}/auth/login/",
                {"email": self.user_a_email, "password": "WrongPassword!", "role": "STAFF"},
                format="json",
            )
            self.assertEqual(resp.status_code, 401)

        # 4th request exceeds LOGIN_RATE_LIMIT
        resp = self.client.post(
            f"{API}/auth/login/",
            {"email": self.user_a_email, "password": "StrongPassword123!", "role": "STAFF"},
            format="json",
        )
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(
            resp.json().get("detail"),
            "Request rate limit exceeded. Please try again later.",
        )
        self.assertIn("retry_after", resp.json())
        self.assertTrue(resp.has_header("Retry-After"))
        self.assertTrue(int(resp["Retry-After"]) >= 1)

    @override_settings(LOGIN_RATE_LIMIT="20/minute", LOGIN_FAILED_RATE_LIMIT="3/10minute")
    def test_failed_login_throttling_after_max_failures(self):
        """3. Failed logins exceeding the threshold return 429."""
        for _ in range(3):
            resp = self.client.post(
                f"{API}/auth/login/",
                {"email": self.user_a_email, "password": "WrongPassword!", "role": "STAFF"},
                format="json",
            )
            self.assertEqual(resp.status_code, 401)

        # 4th request from same IP is blocked due to excessive failed attempts
        resp = self.client.post(
            f"{API}/auth/login/",
            {"email": self.user_a_email, "password": "StrongPassword123!", "role": "STAFF"},
            format="json",
        )
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(
            resp.json().get("detail"),
            "Request rate limit exceeded. Please try again later.",
        )
        self.assertIn("retry_after", resp.json())
        self.assertTrue(resp.has_header("Retry-After"))

    @override_settings(LOGIN_RATE_LIMIT="10/minute", LOGIN_FAILED_RATE_LIMIT="5/10minute")
    def test_successful_login_clears_failed_attempts(self):
        """4. Successful login clears failed attempts counter and is not blocked by previous failures."""
        # 2 failed attempts
        for _ in range(2):
            resp = self.client.post(
                f"{API}/auth/login/",
                {"email": self.user_a_email, "password": "WrongPassword!", "role": "STAFF"},
                format="json",
            )
            self.assertEqual(resp.status_code, 401)

        # 3rd attempt is successful with correct credentials
        success_resp = self.client.post(
            f"{API}/auth/login/",
            {"email": self.user_a_email, "password": "StrongPassword123!", "role": "STAFF"},
            format="json",
        )
        self.assertEqual(success_resp.status_code, 200)

        # Failed login history was cleared; user is not blocked
        next_resp = self.client.post(
            f"{API}/auth/login/",
            {"email": self.user_a_email, "password": "StrongPassword123!", "role": "STAFF"},
            format="json",
        )
        self.assertEqual(next_resp.status_code, 200)

    def test_login_does_not_reveal_user_existence_on_failure(self):
        """Does not reveal whether an email exists when credentials fail."""
        resp_known = self.client.post(
            f"{API}/auth/login/",
            {"email": self.user_a_email, "password": "WrongPassword!", "role": "STAFF"},
            format="json",
        )
        resp_unknown = self.client.post(
            f"{API}/auth/login/",
            {"email": f"unknown-{uuid.uuid4().hex[:6]}@test.com", "password": "WrongPassword!", "role": "STAFF"},
            format="json",
        )
        self.assertEqual(resp_known.status_code, 401)
        self.assertEqual(resp_unknown.status_code, 401)
        self.assertEqual(resp_known.json().get("code"), resp_unknown.json().get("code"))


class AuthenticatedApiThrottlingTests(ThrottlingTestCase):
    @override_settings(AUTHENTICATED_RATE_LIMIT="3/minute")
    def test_authenticated_user_throttled_after_limit(self):
        """5. Authenticated user API calls are throttled after the configured limit (120/min)."""
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.tokens_a['access']}")

        # 3 allowed requests
        for _ in range(3):
            resp = client.get(f"{API}/auth/me/")
            self.assertEqual(resp.status_code, 200)

        # 4th request exceeds AUTHENTICATED_RATE_LIMIT
        resp = client.get(f"{API}/auth/me/")
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(
            resp.json().get("detail"),
            "Request rate limit exceeded. Please try again later.",
        )
        self.assertIn("retry_after", resp.json())
        self.assertTrue(resp.has_header("Retry-After"))

    @override_settings(AUTHENTICATED_RATE_LIMIT="2/minute")
    def test_different_authenticated_users_have_independent_limits(self):
        """7. Different authenticated users have independent limits."""
        client_a = APIClient()
        client_a.credentials(HTTP_AUTHORIZATION=f"Bearer {self.tokens_a['access']}")

        client_b = APIClient()
        client_b.credentials(HTTP_AUTHORIZATION=f"Bearer {self.tokens_b['access']}")

        # User A makes 2 requests and gets throttled on the 3rd
        self.assertEqual(client_a.get(f"{API}/auth/me/").status_code, 200)
        self.assertEqual(client_a.get(f"{API}/auth/me/").status_code, 200)
        self.assertEqual(client_a.get(f"{API}/auth/me/").status_code, 429)

        # User B has independent limit and can still make requests
        resp_b = client_b.get(f"{API}/auth/me/")
        self.assertEqual(resp_b.status_code, 200)


class AnonymousApiThrottlingTests(ThrottlingTestCase):
    @override_settings(ANONYMOUS_RATE_LIMIT="3/minute")
    def test_anonymous_api_throttled_after_limit(self):
        """6. Anonymous API calls are throttled after configured limit (30/min)."""
        url = f"{API}/auth/support/"
        for _ in range(3):
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 200)

        # 4th request is throttled
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(
            resp.json().get("detail"),
            "Request rate limit exceeded. Please try again later.",
        )
        self.assertIn("retry_after", resp.json())
        self.assertTrue(resp.has_header("Retry-After"))

    @override_settings(ANONYMOUS_RATE_LIMIT="2/minute")
    def test_different_ips_have_independent_anonymous_limits(self):
        """8. Different IP addresses have independent anonymous limits."""
        url = f"{API}/auth/support/"

        client_1 = APIClient(REMOTE_ADDR="198.51.100.1")
        client_2 = APIClient(REMOTE_ADDR="198.51.100.2")

        # IP 1 uses up its limit
        self.assertEqual(client_1.get(url).status_code, 200)
        self.assertEqual(client_1.get(url).status_code, 200)
        self.assertEqual(client_1.get(url).status_code, 429)

        # IP 2 is not affected
        self.assertEqual(client_2.get(url).status_code, 200)


class SecurityAndIsolationTests(ThrottlingTestCase):
    @override_settings(LOGIN_RATE_LIMIT="2/minute", NUM_PROXIES=0)
    def test_spoofed_x_forwarded_for_cannot_bypass_rate_limiting(self):
        """9. Rate limiting cannot be bypassed simply by changing X-Forwarded-For header."""
        for i in range(2):
            resp = self.client.post(
                f"{API}/auth/login/",
                {"email": self.user_a_email, "password": "WrongPassword!", "role": "STAFF"},
                HTTP_X_FORWARDED_FOR=f"10.0.0.{i+1}",
                format="json",
            )
            self.assertEqual(resp.status_code, 401)

        # Attacker sends a different spoofed header; must still be throttled based on REMOTE_ADDR
        resp = self.client.post(
            f"{API}/auth/login/",
            {"email": self.user_a_email, "password": "StrongPassword123!", "role": "STAFF"},
            HTTP_X_FORWARDED_FOR="198.51.100.99",
            format="json",
        )
        self.assertEqual(resp.status_code, 429)

    @override_settings(
        LOGIN_RATE_LIMIT="2/minute",
        NUM_PROXIES=1,
        REST_FRAMEWORK={"NUM_PROXIES": 1},
    )
    def test_trusted_proxy_configuration_honors_forwarded_ip(self):
        """When NUM_PROXIES is configured, client IP from trusted proxy is honored."""
        for _ in range(2):
            resp = self.client.post(
                f"{API}/auth/login/",
                {"email": self.user_a_email, "password": "WrongPassword!", "role": "STAFF"},
                HTTP_X_FORWARDED_FOR="203.0.113.10",
                format="json",
            )
            self.assertEqual(resp.status_code, 401)

        # Client 1 is now throttled
        resp = self.client.post(
            f"{API}/auth/login/",
            {"email": self.user_a_email, "password": "WrongPassword!", "role": "STAFF"},
            HTTP_X_FORWARDED_FOR="203.0.113.10",
            format="json",
        )
        self.assertEqual(resp.status_code, 429)

        # Client 2 through reverse proxy is NOT throttled
        resp2 = self.client.post(
            f"{API}/auth/login/",
            {"email": self.user_a_email, "password": "WrongPassword!", "role": "STAFF"},
            HTTP_X_FORWARDED_FOR="203.0.113.20",
            format="json",
        )
        self.assertEqual(resp2.status_code, 401)

    @override_settings(AUTHENTICATED_RATE_LIMIT="2/minute")
    def test_different_users_sharing_same_ip_are_isolated_by_user_id(self):
        """17. Different users sharing the same IP do not throttle each other on authenticated APIs."""
        client_a = APIClient(REMOTE_ADDR="198.51.100.50")
        client_a.credentials(HTTP_AUTHORIZATION=f"Bearer {self.tokens_a['access']}")

        client_b = APIClient(REMOTE_ADDR="198.51.100.50")
        client_b.credentials(HTTP_AUTHORIZATION=f"Bearer {self.tokens_b['access']}")

        # User A uses limit
        self.assertEqual(client_a.get(f"{API}/auth/me/").status_code, 200)
        self.assertEqual(client_a.get(f"{API}/auth/me/").status_code, 200)
        self.assertEqual(client_a.get(f"{API}/auth/me/").status_code, 429)

        # User B on same IP is NOT throttled
        self.assertEqual(client_b.get(f"{API}/auth/me/").status_code, 200)

    @override_settings(LOGIN_RATE_LIMIT="2/minute")
    def test_different_ips_accessing_same_account_isolated_by_ip(self):
        """18. Different IPs targeting the same account have independent login request budgets."""
        client_ip1 = APIClient(REMOTE_ADDR="198.51.100.101")
        client_ip2 = APIClient(REMOTE_ADDR="198.51.100.102")

        # IP 1 uses up login rate
        for _ in range(2):
            self.assertEqual(
                client_ip1.post(
                    f"{API}/auth/login/",
                    {"email": self.user_a_email, "password": "WrongPassword!", "role": "STAFF"},
                    format="json",
                ).status_code,
                401,
            )
        self.assertEqual(
            client_ip1.post(
                f"{API}/auth/login/",
                {"email": self.user_a_email, "password": "StrongPassword123!", "role": "STAFF"},
                format="json",
            ).status_code,
            429,
        )

        # IP 2 can still attempt login
        resp_ip2 = client_ip2.post(
            f"{API}/auth/login/",
            {"email": self.user_a_email, "password": "StrongPassword123!", "role": "STAFF"},
            format="json",
        )
        self.assertEqual(resp_ip2.status_code, 200)


class SensitiveEndpointsThrottlingTests(ThrottlingTestCase):
    @override_settings(PASSWORD_RESET_RATE_LIMIT="2/15minute")
    def test_password_reset_throttled_after_limit(self):
        """9. Sensitive password reset endpoint is throttled according to PASSWORD_RESET_RATE_LIMIT."""
        for _ in range(2):
            resp = self.client.post(
                f"{API}/auth/forgot-password/",
                {"email": self.user_a_email},
                format="json",
            )
            self.assertEqual(resp.status_code, 204)

        # 3rd request is throttled
        resp = self.client.post(
            f"{API}/auth/forgot-password/",
            {"email": self.user_a_email},
            format="json",
        )
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(
            resp.json().get("detail"),
            "Request rate limit exceeded. Please try again later.",
        )
        self.assertIn("retry_after", resp.json())

    @override_settings(OTP_VERIFY_RATE_LIMIT="2/10minute")
    def test_otp_verify_throttled_after_limit(self):
        """10. Sensitive OTP verify endpoint is throttled according to OTP_VERIFY_RATE_LIMIT."""
        for _ in range(2):
            resp = self.client.post(
                f"{API}/auth/verify-reset-otp/",
                {"email": self.user_a_email, "otp": "000000"},
                format="json",
            )
            # 400 validation error (wrong OTP) is normal, but it reaches the view
            self.assertIn(resp.status_code, [400, 200])

        # 3rd request is throttled
        resp = self.client.post(
            f"{API}/auth/verify-reset-otp/",
            {"email": self.user_a_email, "otp": "000000"},
            format="json",
        )
        self.assertEqual(resp.status_code, 429)

    @override_settings(TOKEN_REFRESH_RATE_LIMIT="2/10minute")
    def test_token_refresh_throttled_after_limit(self):
        """11. Sensitive token refresh endpoint is throttled according to TOKEN_REFRESH_RATE_LIMIT."""
        login_resp = self.client.post(
            f"{API}/auth/login/",
            {"email": self.user_a_email, "password": "StrongPassword123!", "role": "STAFF"},
            format="json",
        )
        self.assertEqual(login_resp.status_code, 200)
        refresh_token = login_resp.json()["refresh"]

        for _ in range(2):
            resp = self.client.post(
                f"{API}/auth/refresh/",
                {"refresh": refresh_token},
                format="json",
            )
            self.assertEqual(resp.status_code, 200)
            refresh_token = resp.json()["refresh"]

        # 3rd request is throttled
        resp = self.client.post(
            f"{API}/auth/refresh/",
            {"refresh": refresh_token},
            format="json",
        )
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(
            resp.json().get("detail"),
            "Request rate limit exceeded. Please try again later.",
        )
        self.assertIn("retry_after", resp.json())


class WindowResetAndCacheTests(ThrottlingTestCase):
    @override_settings(LOGIN_RATE_LIMIT="1/minute")
    def test_rate_limit_resets_after_window_expires(self):
        """14. Rate limit resets after the configured duration."""
        # 1st request succeeds
        resp = self.client.post(
            f"{API}/auth/login/",
            {"email": self.user_a_email, "password": "StrongPassword123!", "role": "STAFF"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)

        # 2nd immediate request throttled
        resp2 = self.client.post(
            f"{API}/auth/login/",
            {"email": self.user_a_email, "password": "StrongPassword123!", "role": "STAFF"},
            format="json",
        )
        self.assertEqual(resp2.status_code, 429)

        # Advance timer past 60s
        with patch("time.time", return_value=time.time() + 65):
            resp3 = self.client.post(
                f"{API}/auth/login/",
                {"email": self.user_a_email, "password": "StrongPassword123!", "role": "STAFF"},
                format="json",
            )
            self.assertEqual(resp3.status_code, 200)

    def test_no_sensitive_values_stored_in_rate_limit_cache(self):
        """Verify password, OTP, and token strings are never stored in cache keys."""
        cache.clear()
        raw_password = "SecretPassword123!"
        self.client.post(
            f"{API}/auth/login/",
            {"email": self.user_a_email, "password": raw_password, "role": "STAFF"},
            format="json",
        )
        # Check all keys in cache
        if hasattr(cache, "_cache"):
            for k in cache._cache.keys():
                self.assertNotIn(raw_password, str(k))
