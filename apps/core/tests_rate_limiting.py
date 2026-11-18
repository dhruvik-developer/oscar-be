"""Rate limiting and proxy IP resolution tests."""
from django.test import RequestFactory, TestCase, override_settings
from rest_framework import status
from rest_framework.response import Response
from rest_framework.test import APIClient
from rest_framework.views import APIView

from apps.core.audit import request_ip
from apps.core.permissions import AllowPublic
from apps.core.throttling import PublicEndpointThrottle


class DummyThrottledView(APIView):
    authentication_classes = []
    permission_classes = [AllowPublic]
    throttle_classes = [PublicEndpointThrottle]

    def get(self, request):
        return Response({"status": "ok"})


class RateLimitingProxyTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def test_request_ip_resolution_without_proxies(self):
        """When NUM_PROXIES is 0 or unset, request_ip ignores X-Forwarded-For and returns REMOTE_ADDR."""
        req = self.factory.get("/", HTTP_X_FORWARDED_FOR="1.1.1.1, 2.2.2.2", REMOTE_ADDR="198.51.100.5")
        with override_settings(REST_FRAMEWORK={"NUM_PROXIES": 0}):
            self.assertEqual(request_ip(req), "198.51.100.5")

        with override_settings(REST_FRAMEWORK={"NUM_PROXIES": None}):
            self.assertEqual(request_ip(req), "198.51.100.5")

    def test_request_ip_resolution_with_num_proxies(self):
        """When NUM_PROXIES=1, request_ip takes the 1st address from the right."""
        req = self.factory.get(
            "/",
            HTTP_X_FORWARDED_FOR="spoofed.attacker.ip, 203.0.113.195",
            REMOTE_ADDR="127.0.0.1",
        )
        with override_settings(REST_FRAMEWORK={"NUM_PROXIES": 1}):
            self.assertEqual(request_ip(req), "203.0.113.195")

    def test_request_ip_resolution_with_two_proxies(self):
        """When NUM_PROXIES=2 (e.g. Cloudflare -> Nginx -> Django), takes the 2nd address from the right."""
        req = self.factory.get(
            "/",
            HTTP_X_FORWARDED_FOR="spoofed.ip, 198.51.100.42, 172.16.0.2",
            REMOTE_ADDR="127.0.0.1",
        )
        with override_settings(REST_FRAMEWORK={"NUM_PROXIES": 2}):
            self.assertEqual(request_ip(req), "198.51.100.42")

    def test_request_ip_falls_back_when_insufficient_headers(self):
        """If X-Forwarded-For has fewer hops than NUM_PROXIES, fallback to REMOTE_ADDR."""
        req = self.factory.get(
            "/",
            HTTP_X_FORWARDED_FOR="single.ip",
            REMOTE_ADDR="198.51.100.99",
        )
        with override_settings(REST_FRAMEWORK={"NUM_PROXIES": 2}):
            self.assertEqual(request_ip(req), "198.51.100.99")

    def test_spoofed_forwarded_for_cannot_bypass_throttle(self):
        """When NUM_PROXIES=1, changing the leftmost IP in X-Forwarded-For does NOT change
        the rate limiter ident because DRF resolves the rightmost trusted proxy hop."""
        from django.core.cache import cache
        from rest_framework.settings import api_settings

        cache.clear()
        orig_num = api_settings.NUM_PROXIES
        api_settings.NUM_PROXIES = 1
        try:
            class TestThrottle(PublicEndpointThrottle):
                rate = "2/min"

                def get_rate(self):
                    return "2/min"

            throttle = TestThrottle()
            from django.contrib.auth.models import AnonymousUser
            req1 = self.factory.get("/", HTTP_X_FORWARDED_FOR="spoofed-1, 203.0.113.50", REMOTE_ADDR="127.0.0.1")
            req2 = self.factory.get("/", HTTP_X_FORWARDED_FOR="spoofed-2, 203.0.113.50", REMOTE_ADDR="127.0.0.1")
            req3 = self.factory.get("/", HTTP_X_FORWARDED_FOR="spoofed-3, 203.0.113.50", REMOTE_ADDR="127.0.0.1")
            req1.user = req2.user = req3.user = AnonymousUser()

            self.assertEqual(throttle.get_ident(req1), "203.0.113.50")
            self.assertEqual(throttle.get_ident(req2), "203.0.113.50")
            self.assertEqual(throttle.get_ident(req3), "203.0.113.50")

            self.assertTrue(throttle.allow_request(req1, None))
            self.assertTrue(throttle.allow_request(req2, None))
            # 3rd request from same client with different spoofed IP is throttled
            self.assertFalse(throttle.allow_request(req3, None))
        finally:
            api_settings.NUM_PROXIES = orig_num
