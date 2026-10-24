"""
Rate limits for the unauthenticated surfaces.

api.md rate-limits public quotation views by IP (§5.3), public lead form
submissions (§9.7) and career-portal applications (§11.5). Public proof
approval (§10.6) gets the same treatment -- an opaque token is not a
rate limit.
"""
from rest_framework.throttling import AnonRateThrottle


class PublicEndpointThrottle(AnonRateThrottle):
    """Reads of a public token page."""

    scope = "public"


class PublicWriteThrottle(AnonRateThrottle):
    """Writes from a public page: accept, reject, comment, submit, apply."""

    scope = "public_write"


class LoginThrottle(AnonRateThrottle):
    """Guards ``/auth/login/`` and the password-reset endpoints."""

    scope = "login"
