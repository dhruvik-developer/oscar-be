"""
Production-ready rate limiting and throttling.

Implements:
- Safe client IP extraction respecting trusted proxy settings (NUM_PROXIES, TRUSTED_PROXIES)
- Flexible rate parsing supporting multi-unit intervals (e.g., '10/10minute', '3/15minute')
- Configurable settings/environment variables with test override support
- Sliding window / token bucket per user (authenticated) and per IP (anonymous)
- Login brute-force protection (attempts limit + failed attempts limit with clear on success)
- Sensitive endpoint rate limiting (password reset, OTP verify, token refresh)
"""
import math
import re

from django.conf import settings
from rest_framework.settings import api_settings
from rest_framework.throttling import SimpleRateThrottle


def get_client_ip(request):
    """
    Extract the client IP safely.
    
    Does not trust client-supplied X-Forwarded-For headers unless trusted proxies
    are explicitly configured via settings.NUM_PROXIES or settings.TRUSTED_PROXIES.
    """
    if request is None:
        return None

    remote_addr = request.META.get("REMOTE_ADDR") or "127.0.0.1"

    num_proxies = getattr(api_settings, "NUM_PROXIES", None)
    if num_proxies is None or num_proxies == 0:
        num_proxies = getattr(settings, "NUM_PROXIES", None)
    if num_proxies is None or num_proxies == 0:
        rf_settings = getattr(settings, "REST_FRAMEWORK", {}) or {}
        num_proxies = rf_settings.get("NUM_PROXIES", num_proxies)
    trusted_proxies = getattr(settings, "TRUSTED_PROXIES", None)

    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    if xff and (num_proxies or trusted_proxies):
        addrs = [ip.strip() for ip in xff.split(",") if ip.strip()]
        if num_proxies is not None and num_proxies > 0:
            if len(addrs) >= num_proxies:
                return addrs[-num_proxies]
            return remote_addr
        if trusted_proxies and remote_addr in trusted_proxies:
            for ip in reversed(addrs):
                if ip not in trusted_proxies:
                    return ip
            return addrs[0]

    return remote_addr


class ProductionRateThrottle(SimpleRateThrottle):
    """
    Enhanced throttle base supporting:
    - Dynamic rate lookups from Django settings (allowing override in tests)
    - Custom intervals such as '3/15minute', '10/10minute', '120/minute'
    - Secure IP resolution
    - Accurate wait calculation for Retry-After
    """
    setting_name = None
    setting_names = None
    default_rate = None

    def get_ident(self, request):
        return get_client_ip(request) or "127.0.0.1"

    def get_rate(self):
        """
        Dynamically resolve the rate string, honoring runtime test overrides.
        """
        candidate_names = []
        if self.setting_name:
            candidate_names.append(self.setting_name)
        if self.setting_names:
            for name in self.setting_names:
                if name not in candidate_names:
                    candidate_names.append(name)

        for name in candidate_names:
            if hasattr(settings, name):
                val = getattr(settings, name)
                if val:
                    return val

        drf_rates = getattr(settings, "REST_FRAMEWORK", {}).get("DEFAULT_THROTTLE_RATES", {})
        if self.scope and self.scope in drf_rates:
            return drf_rates[self.scope]

        if self.scope and hasattr(api_settings, "DEFAULT_THROTTLE_RATES") and self.scope in api_settings.DEFAULT_THROTTLE_RATES:
            return api_settings.DEFAULT_THROTTLE_RATES[self.scope]

        return self.default_rate

    def parse_rate(self, rate):
        """
        Given the request rate string, return a two-tuple of:
        <number of requests>, <duration in seconds>
        Supports standard DRF rates ('5/m', '10/h') and multi-unit periods
        ('3/15minute', '10/10min', '120/minute').
        """
        if rate is None:
            return (None, None)
        if isinstance(rate, (tuple, list)) and len(rate) == 2:
            return rate

        parts = str(rate).split("/")
        if len(parts) != 2:
            raise ValueError(f"Invalid rate format: '{rate}'. Expected '<count>/<period>'.")

        num_requests = int(parts[0].strip())
        period_str = parts[1].strip().lower()

        match = re.match(r"^(\d+)?\s*([a-z]+)$", period_str)
        if not match:
            raise ValueError(f"Invalid throttle period: '{period_str}'")

        multiplier = int(match.group(1)) if match.group(1) else 1
        unit = match.group(2)

        if unit.startswith("s"):
            unit_secs = 1
        elif unit.startswith("m"):
            unit_secs = 60
        elif unit.startswith("h"):
            unit_secs = 3600
        elif unit.startswith("d"):
            unit_secs = 86400
        else:
            raise ValueError(f"Unknown time unit in rate period: '{unit}'")

        duration = multiplier * unit_secs
        return (num_requests, duration)

    def allow_request(self, request, view):
        self.rate = self.get_rate()
        self.num_requests, self.duration = self.parse_rate(self.rate)
        if self.rate is None:
            return True
        return super().allow_request(request, view)

    def wait(self):
        """
        Returns the duration in seconds until the throttle window opens.
        """
        if self.history:
            remaining_duration = self.duration - (self.now - self.history[-1])
            return max(1, int(math.ceil(remaining_duration)))
        return max(1, int(self.duration))


class SafeUserRateThrottle(ProductionRateThrottle):
    """
    Default authenticated user throttle: 120 requests per minute per user.
    """
    scope = "user"
    setting_name = "AUTHENTICATED_RATE_LIMIT"
    setting_names = ["AUTHENTICATED_RATE_LIMIT", "AUTHENTICATED_API_RATE_LIMIT"]
    default_rate = "120/minute"

    def allow_request(self, request, view):
        is_debug = getattr(settings, "DEBUG", False)
        is_testing = getattr(settings, "IS_TESTING", False)
        has_override = any(hasattr(settings, n) and getattr(settings, n) for n in (self.setting_names or [self.setting_name]))
        if is_debug and not is_testing and not has_override:
            return True
        return super().allow_request(request, view)

    def get_cache_key(self, request, view):
        if request.user and request.user.is_authenticated:
            return self.cache_format % {"scope": self.scope, "ident": str(request.user.pk)}
        return None


class SafeAnonRateThrottle(ProductionRateThrottle):
    """
    Default unauthenticated throttle: 30 requests per minute per IP.
    Only applies to unauthenticated callers.
    """
    scope = "anon"
    setting_name = "ANONYMOUS_RATE_LIMIT"
    setting_names = ["ANONYMOUS_RATE_LIMIT", "ANONYMOUS_API_RATE_LIMIT"]
    default_rate = "30/minute"

    def allow_request(self, request, view):
        is_debug = getattr(settings, "DEBUG", False)
        is_testing = getattr(settings, "IS_TESTING", False)
        has_override = any(hasattr(settings, n) and getattr(settings, n) for n in (self.setting_names or [self.setting_name]))
        if is_debug and not is_testing and not has_override:
            return True
        return super().allow_request(request, view)

    def get_cache_key(self, request, view):
        if request.user and request.user.is_authenticated:
            return None
        ident = self.get_ident(request)
        return self.cache_format % {"scope": self.scope, "ident": ident}


class LoginThrottle(ProductionRateThrottle):
    """
    Limits total login attempts per IP: default 5 attempts per minute per IP.
    """
    scope = "login"
    setting_name = "LOGIN_RATE_LIMIT"
    default_rate = "5/minute"

    def get_cache_key(self, request, view):
        ident = self.get_ident(request)
        return self.cache_format % {"scope": self.scope, "ident": ident}


class FailedLoginThrottle(ProductionRateThrottle):
    """
    Limits failed login attempts per IP: default 10 failed attempts per 10 minutes per IP.
    Successful logins clear previous failed login records for the IP.
    """
    scope = "login_failed"
    setting_name = "LOGIN_FAILED_RATE_LIMIT"
    setting_names = ["LOGIN_FAILED_RATE_LIMIT", "LOGIN_FAILED_LIMIT"]
    default_rate = "10/10minute"

    def get_cache_key(self, request, view=None):
        ident = self.get_ident(request)
        return self.cache_format % {"scope": self.scope, "ident": ident}

    def allow_request(self, request, view):
        self.rate = self.get_rate()
        self.num_requests, self.duration = self.parse_rate(self.rate)
        if self.rate is None:
            return True

        self.key = self.get_cache_key(request, view)
        if self.key is None:
            return True

        self.now = self.timer()
        self.history = self.cache.get(self.key, [])
        self.history = [t for t in self.history if t > self.now - self.duration]

        if len(self.history) >= self.num_requests:
            return self.throttle_failure()
        return True

    @classmethod
    def record_failure(cls, request):
        instance = cls()
        instance.rate = instance.get_rate()
        instance.num_requests, instance.duration = instance.parse_rate(instance.rate)
        if instance.rate is None:
            return
        key = instance.get_cache_key(request, None)
        if not key:
            return
        now = instance.timer()
        history = instance.cache.get(key, [])
        history = [t for t in history if t > now - instance.duration]
        history.insert(0, now)
        instance.cache.set(key, history, instance.duration)

    @classmethod
    def clear_failures(cls, request):
        instance = cls()
        key = instance.get_cache_key(request, None)
        if key:
            instance.cache.delete(key)


class PasswordResetThrottle(ProductionRateThrottle):
    """
    Stricter limit for password reset requests: default 3 requests / 15 minutes / IP.
    """
    scope = "password_reset"
    setting_name = "PASSWORD_RESET_RATE_LIMIT"
    default_rate = "3/15minute"

    def get_cache_key(self, request, view):
        ident = self.get_ident(request)
        view_name = view.__class__.__name__.lower() if view else "default"
        return self.cache_format % {"scope": f"{self.scope}_{view_name}", "ident": ident}


class OTPVerifyThrottle(ProductionRateThrottle):
    """
    Stricter limit for OTP verification & generation: default 5 requests / 10 minutes / IP.
    """
    scope = "otp_verify"
    setting_name = "OTP_VERIFY_RATE_LIMIT"
    setting_names = ["OTP_VERIFY_RATE_LIMIT", "OTP_RATE_LIMIT"]
    default_rate = "5/10minute"

    def get_cache_key(self, request, view):
        ident = self.get_ident(request)
        return self.cache_format % {"scope": self.scope, "ident": ident}


class TokenRefreshThrottle(ProductionRateThrottle):
    """
    Stricter limit for token refresh operations: default 10 requests / 10 minutes / IP.
    """
    scope = "token_refresh"
    setting_name = "TOKEN_REFRESH_RATE_LIMIT"
    setting_names = ["TOKEN_REFRESH_RATE_LIMIT", "TOKEN_RATE_LIMIT"]
    default_rate = "10/10minute"

    def get_cache_key(self, request, view):
        ident = self.get_ident(request)
        return self.cache_format % {"scope": self.scope, "ident": ident}


class PasswordChangeThrottle(ProductionRateThrottle):
    """Per signed-in user: guards change-password."""
    scope = "password_change"
    setting_name = "PASSWORD_CHANGE_RATE_LIMIT"
    default_rate = "5/min"

    def get_cache_key(self, request, view):
        if request.user and request.user.is_authenticated:
            return self.cache_format % {"scope": self.scope, "ident": str(request.user.pk)}
        return None


class SensitiveActionThrottle(ProductionRateThrottle):
    """
    Throttle for authenticated sensitive operations.
    """
    scope = "sensitive_action"
    setting_name = "SENSITIVE_ACTION_RATE_LIMIT"
    default_rate = "10/10minute"

    def get_cache_key(self, request, view):
        if request.user and request.user.is_authenticated:
            ident = str(request.user.pk)
        else:
            ident = self.get_ident(request)
        return self.cache_format % {"scope": self.scope, "ident": ident}


class PublicEndpointThrottle(ProductionRateThrottle):
    """Reads of a public token page (quotation, proof, careers, lead forms)."""
    scope = "public"
    default_rate = "60/min"

    def get_cache_key(self, request, view):
        ident = self.get_ident(request)
        return self.cache_format % {"scope": self.scope, "ident": ident}


class PublicWriteThrottle(ProductionRateThrottle):
    """Writes from a public page: accept, reject, comment, submit, apply."""
    scope = "public_write"
    default_rate = "10/min"

    def get_cache_key(self, request, view):
        ident = self.get_ident(request)
        return self.cache_format % {"scope": self.scope, "ident": ident}
