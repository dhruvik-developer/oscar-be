"""
JWT bearer authentication with tenant binding (api.md §1.2, §1.11).

The client reads the token from ``localStorage`` and sends
``Authorization: Bearer <token>``. On 401 it clears the stored token, so an
expired or invalid token must return **401, not 403** -- returning 403 would
leave the client retrying forever with a dead token.

Authentication is also where the tenant is resolved, because it is the only
place that has read the JWT. It stamps ``request.client_id`` and publishes the
tenant to the database session for RLS (db.md §1.3).
"""
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import (
    AuthenticationFailed as JWTAuthenticationFailed,
)
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError

from apps.core.exceptions import NotAuthenticated
from apps.core.tenancy import set_current_client_id

CLIENT_CLAIM = "client_id"
PERMISSIONS_CLAIM = "perms"


class TenantJWTAuthentication(JWTAuthentication):
    def authenticate(self, request):
        try:
            result = super().authenticate(request)
        except (InvalidToken, TokenError, JWTAuthenticationFailed) as exc:
            raise NotAuthenticated(
                "Your session has expired. Please sign in again.",
                code="TOKEN_INVALID",
                detail=str(exc),
            ) from exc

        if result is None:
            request.client_id = None
            return None

        user, token = result

        # The tenant comes from the token, not from a header or a body field --
        # a client must not be able to ask for another tenant's data.
        client_id = token.get(CLIENT_CLAIM) or str(user.client_id)
        if str(user.client_id) != str(client_id):
            # The user was moved between tenants after the token was issued.
            raise NotAuthenticated(
                "Your session is no longer valid. Please sign in again.",
                code="TOKEN_INVALID",
            )

        request.client_id = user.client_id
        set_current_client_id(user.client_id)

        # Effective permissions are computed at login and cached on the token
        # (db.md §2.2). Falling back to a live lookup keeps a token minted
        # before a role change honest rather than stale.
        cached = token.get(PERMISSIONS_CLAIM)
        user.permission_ids = set(cached) if cached is not None else user.effective_permissions()
        user.session_id = token.get("sid")
        return user, token

    def get_user(self, validated_token):
        try:
            user = super().get_user(validated_token)
        except (InvalidToken, JWTAuthenticationFailed) as exc:
            raise NotAuthenticated(
                "Your session has expired. Please sign in again.", code="TOKEN_INVALID"
            ) from exc

        if user.deleted_at is not None or user.status == "Deleted":
            raise NotAuthenticated("This account no longer exists.", code="TOKEN_INVALID")
        return user


def build_tokens(user, *, session=None):
    """Mint the access/refresh pair returned by ``/auth/login/``."""
    from rest_framework_simplejwt.tokens import RefreshToken

    refresh = RefreshToken.for_user(user)
    refresh[CLIENT_CLAIM] = str(user.client_id)
    refresh[PERMISSIONS_CLAIM] = sorted(user.effective_permissions())
    if session is not None:
        refresh["sid"] = str(session.id)

    access = refresh.access_token
    access[CLIENT_CLAIM] = str(user.client_id)
    access[PERMISSIONS_CLAIM] = refresh[PERMISSIONS_CLAIM]
    if session is not None:
        access["sid"] = str(session.id)

    return {"access": str(access), "refresh": str(refresh)}
