"""
OpenAPI description of the auth scheme (api.md §13).

Without this, drf-spectacular cannot see that ``TenantJWTAuthentication`` is
bearer auth, and the generated document would describe every endpoint as
unauthenticated -- which is exactly the sort of drift db.md §14.2 warns about.

Imported for its side effect by ``apps.accounts.apps.AccountsConfig.ready``.
"""
from drf_spectacular.extensions import OpenApiAuthenticationExtension


class TenantJWTScheme(OpenApiAuthenticationExtension):
    target_class = "apps.accounts.authentication.TenantJWTAuthentication"
    name = "bearerAuth"

    def get_security_definition(self, auto_schema):
        return {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
            "description": (
                "JWT from `POST /auth/login/`, sent as `Authorization: Bearer "
                "<token>`. The tenant is read from the token, never from a "
                "header. An expired or invalid token returns 401 (never 403), "
                "so the client clears it and redirects to sign-in."
            ),
        }
