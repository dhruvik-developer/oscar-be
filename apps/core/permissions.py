"""
RBAC (api.md Appendix B, db.md §2.2).

Effective permissions = role grants union user grants minus user denies. They
are computed at login, cached on the session and returned by ``/auth/me/``.

api-integration.md §6 is explicit that client-side checks are UX only:
"A hidden button must still 403 if called." These classes are that 403.
api.md §1.5 also requires the permission id in the error ``code``, so the UI
can disable the action for the rest of the session without string-matching.
"""
from rest_framework.permissions import BasePermission

from .exceptions import NotAuthenticated, PermissionDenied


class IsAuthenticatedInTenant(BasePermission):
    """Authenticated, active, and bound to a tenant.

    Raises rather than returning False so the body carries a code; DRF's own
    403-for-unauthenticated behaviour would break the client, which clears its
    token on 401 only (api.md §1.2).
    """

    def has_permission(self, request, view):
        user = getattr(request, "user", None)
        if user is None or not getattr(user, "is_authenticated", False):
            raise NotAuthenticated()
        if getattr(request, "client_id", None) is None:
            raise NotAuthenticated("Your account is not attached to a tenant.")
        if getattr(user, "status", "Active") not in ("Active",):
            raise PermissionDenied(
                "This account is not active.", code="ACCOUNT_INACTIVE"
            )
        return True


class AllowPublic(BasePermission):
    """Unauthenticated endpoints: public quotation, proof approval, careers, forms."""

    def has_permission(self, request, view):
        return True


class IsAdminOrDebug(BasePermission):
    """Allows access if DEBUG is True or caller is an authenticated staff/superuser/admin."""

    def has_permission(self, request, view):
        from django.conf import settings

        if getattr(settings, "DEBUG", False):
            return True
        user = getattr(request, "user", None)
        if user and getattr(user, "is_authenticated", False):
            if getattr(user, "is_superuser", False) or getattr(user, "is_staff", False):
                return True
            perms = getattr(user, "permission_ids", None)
            if perms is None and hasattr(user, "effective_permissions"):
                perms = user.effective_permissions()
            if perms and ("manage_company_profile" in perms or "view_audit_logs" in perms):
                return True
        return False


class HasModulePermission(IsAuthenticatedInTenant):
    """Checks the permission ids a view declares.

    A view declares either a flat requirement::

        required_permissions = ["view_sales"]

    or one per action, which is what most document viewsets need::

        permission_map = {
            "list": ["view_sales"],
            "create": ["create_invoice"],
            "finalize": ["finalize_invoice"],
            "cancel": ["cancel_invoice"],
        }

    A view with NO declared permissions fails closed (denied with code PERMISSION_DENIED)
    unless it explicitly sets allow_any_authenticated = True.
    """

    def has_permission(self, request, view):
        super().has_permission(request, view)

        required = self._required_for(view, getattr(view, "action", None), request.method)
        if not required:
            if getattr(view, "allow_any_authenticated", False) or getattr(view, "__class__", None).__name__ == "APIRootView":
                return True
            raise PermissionDenied(
                "Access to this resource is restricted.",
                code="PERMISSION_DENIED",
                detail="No permissions declared for this view or action.",
            )

        user = request.user
        granted = getattr(user, "permission_ids", None)
        if granted is None:
            granted = user.effective_permissions()

        if getattr(user, "is_superuser", False):
            return True

        missing = [permission for permission in required if permission not in granted]
        if missing:
            raise PermissionDenied(
                "You don't have permission to do that.",
                # api.md §1.5 -- the permission id goes in `code`.
                code=missing[0],
                detail=f"Requires: {', '.join(missing)}.",
            )
        return True

    @staticmethod
    def _required_for(view, action, method):
        permission_map = getattr(view, "permission_map", None) or {}
        if action and action in permission_map:
            entry = permission_map[action]
            if isinstance(entry, dict):
                if method in entry:
                    return entry[method]
                if method not in ("GET", "HEAD", "OPTIONS") and "write" in entry:
                    return entry["write"]
                if method in ("GET", "HEAD", "OPTIONS") and "read" in entry:
                    return entry["read"]
                return []
            return entry
        if method in permission_map:
            return permission_map[method]
        # `write` is a convenience bucket for "any non-safe method".
        if method not in ("GET", "HEAD", "OPTIONS") and "write" in permission_map:
            return permission_map["write"]
        if method in ("GET", "HEAD", "OPTIONS") and "read" in permission_map:
            return permission_map["read"]
        return getattr(view, "required_permissions", None) or []


def require_permission(user, permission_id, message=None):
    """Imperative gate for code paths that are not view-level.

    Used by override flows -- ``overrideCreditLimit`` (api.md §4.1) and PMS
    ``force`` completion (api.md §10.3) -- where the requirement depends on the
    request body, not the route.
    """
    if getattr(user, "is_superuser", False):
        return True
    granted = getattr(user, "permission_ids", None)
    if granted is None:
        granted = user.effective_permissions()
    if permission_id not in granted:
        raise PermissionDenied(
            message or "You don't have permission to do that.", code=permission_id
        )
    return True


def has_permission(user, permission_id):
    if getattr(user, "is_superuser", False):
        return True
    granted = getattr(user, "permission_ids", None)
    if granted is None:
        granted = user.effective_permissions()
    return permission_id in granted
