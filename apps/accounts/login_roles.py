"""
The sign-in page's "Select Your Role" list, and the check that the role a
user picks is really theirs.

Source of truth is ``User.role`` (-> ``Role``): a role is offered only while
at least one active user in an active workspace holds it. Superusers keep
their access without a role, so the list carries one extra entry for them
whenever an active superuser exists. Picking a role never grants anything:
permissions still come from the account itself (``effective_permissions``).
"""
from .models import Role, User

#: The dropdown value for a superuser. Not a Role row, so it cannot clash
#: with a role code an administrator creates.
ADMIN_CODE = "__superuser__"
ADMIN_NAME = "Administrator"


def _eligible_users(tenant_slug=None):
    users = User.objects.filter(
        deleted_at__isnull=True,
        is_active=True,
        status="Active",
        client__status="Active",
    )
    if tenant_slug:
        users = users.filter(client__slug=tenant_slug)
    return users


def login_role_options(tenant_slug=None):
    """``[{code, name}]`` for the dropdown -- role labels only, no user data.

    Without a workspace, roles of every workspace are merged by code.
    """
    users = _eligible_users(tenant_slug)
    roles = (
        Role.objects.filter(deleted_at__isnull=True, users__in=users)
        .values_list("code", "name")
        .distinct()
        .order_by("name", "code")
    )
    options = {}
    for code, name in roles:
        options.setdefault(code, {"code": code, "name": name})
    result = list(options.values())
    if users.filter(is_superuser=True).exists():
        result.append({"code": ADMIN_CODE, "name": ADMIN_NAME})
    return result


def role_matches(user, code):
    """True when ``code`` (from the dropdown) is this user's own role."""
    code = (code or "").strip()
    if not code:
        return False
    if code == ADMIN_CODE:
        return bool(user.is_superuser)
    role = user.role if user.role_id else None
    return role is not None and role.deleted_at is None and role.code == code
