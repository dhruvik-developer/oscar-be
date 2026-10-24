"""
Per-request tenant scoping (db.md §1.3, api.md §1.11).

Every business table carries `client_id`; every query is scoped to the caller's
tenant, and cross-tenant access returns 404, never 403.

Two layers of defence, exactly as db.md §1.3 describes:

  1. The ORM filter -- ``TenantModelViewSet.get_queryset()`` narrows every
     queryset through :func:`require_client_id`. This is the first defence.
  2. Row-level security -- ``manage.py enable_rls`` installs policies keyed on
     ``current_setting('app.client_id')``, which this module sets on the
     connection for the lifetime of the request. This is the one that holds
     when someone forgets layer 1.
"""
from contextlib import contextmanager
from threading import local

from django.db import connection

_state = local()


def get_current_client_id():
    """The tenant id bound to this request/thread, or None outside a request."""
    return getattr(_state, "client_id", None)


def set_current_client_id(client_id, *, push_to_db=True):
    """Bind a tenant to this thread and, optionally, to the DB session."""
    _state.client_id = client_id
    if push_to_db:
        _set_db_client_id(client_id)


def clear_current_client_id(*, push_to_db=True):
    _state.client_id = None
    if push_to_db:
        _set_db_client_id(None)


def _set_db_client_id(client_id):
    """Publish the tenant to Postgres so RLS policies can read it.

    Uses ``set_config(..., is_local => false)`` because the value must outlive
    any individual transaction within the request. Swallowing errors here is
    deliberate: a connection that is not yet usable must not break the request
    before the view has had a chance to report a real error.
    """
    if connection.vendor != "postgresql":
        return
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "select set_config('app.client_id', %s, false)",
                [str(client_id) if client_id else ""],
            )
    except Exception:  # pragma: no cover - connection not ready / read-only replica
        pass


@contextmanager
def tenant_context(client_id, *, push_to_db=True):
    """Run a block scoped to one tenant. Used by seeders, jobs and tests."""
    previous = get_current_client_id()
    set_current_client_id(client_id, push_to_db=push_to_db)
    try:
        yield client_id
    finally:
        if previous is None:
            clear_current_client_id(push_to_db=push_to_db)
        else:
            set_current_client_id(previous, push_to_db=push_to_db)


class TenantMiddleware:
    """Clears the thread-local tenant once the response is on its way out.

    The tenant is *set* by the authentication class (which is the only place
    that has read the JWT), not here -- middleware runs before DRF
    authentication. This middleware exists so a pooled worker thread never
    leaks one request's tenant into the next.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        clear_current_client_id(push_to_db=False)
        try:
            return self.get_response(request)
        finally:
            clear_current_client_id()


def require_client_id(request=None):
    """The tenant for the current request.

    Prefers the value stamped on the request by authentication, falling back to
    the thread-local. Returns None when unauthenticated -- callers decide
    whether that is an error (authenticated endpoints) or expected (public
    token endpoints, which resolve their own tenant from the share record).
    """
    if request is not None:
        client_id = getattr(request, "client_id", None)
        if client_id:
            return client_id
    return get_current_client_id()
