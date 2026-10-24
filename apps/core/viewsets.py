"""
Base viewsets.

Carries the cross-cutting behaviour every endpoint owes the contract, so no
individual viewset has to remember it:

  - tenant scoping, with cross-tenant reads returning 404 (api.md §1.11)
  - the list envelope with ``aggregates`` (api.md §1.4)
  - optimistic concurrency via ``If-Unmodified-Since`` / ``expectedVersion``,
    409 with the current server copy in ``payload`` (api.md §1.8)
  - ``Idempotency-Key`` replay on create (api.md §1.8)
  - an audit row written inside the mutation's transaction (api.md §1.10)
  - soft delete, never a hard delete of anything posted (api.md §1.9)
"""
from django.db import transaction
from django.utils import timezone
from django.utils.http import parse_http_date_safe
from rest_framework import mixins, status, viewsets
from rest_framework.response import Response

from . import idempotency
from .audit import record_audit, request_ip, snapshot
from .exceptions import Codes, Conflict, NotFound, ValidationFailed
from .permissions import HasModulePermission


class TenantScopedMixin:
    """Narrows every queryset to the caller's tenant."""

    #: Set False for models that have no ``deleted_at`` (audit log, movements).
    filter_soft_deleted = True

    def get_client_id(self):
        return getattr(self.request, "client_id", None)

    def get_queryset(self):
        queryset = super().get_queryset()
        client_id = self.get_client_id()
        if client_id is None:
            return queryset.none()
        if hasattr(queryset.model, "client_id"):
            queryset = queryset.filter(client_id=client_id)
        if self.filter_soft_deleted and hasattr(queryset.model, "deleted_at"):
            queryset = queryset.filter(deleted_at__isnull=True)
        return queryset

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["client_id"] = self.get_client_id()
        return context


class ConcurrencyMixin:
    """api.md §1.8 -- ``If-Unmodified-Since`` or ``expectedVersion``.

    A conflict returns 409 with the current server copy in ``payload``, which is
    what api-integration.md §5.5 renders as the "This record changed since you
    opened it" dialog with Reload / Overwrite.
    """

    def check_concurrency(self, instance):
        updated_at = getattr(instance, "updated_at", None)
        if updated_at is None:
            return

        expected = None
        header = self.request.META.get("HTTP_IF_UNMODIFIED_SINCE")
        if header:
            parsed = parse_http_date_safe(header)
            if parsed is not None:
                expected = timezone.datetime.fromtimestamp(parsed, tz=timezone.utc)

        body_version = None
        if isinstance(self.request.data, dict):
            body_version = self.request.data.get("expectedVersion") or self.request.data.get(
                "expected_version"
            )
        if body_version:
            from rest_framework.fields import DateTimeField

            try:
                expected = DateTimeField().to_internal_value(body_version)
            except Exception:
                raise ValidationFailed(
                    "expectedVersion must be an ISO timestamp.",
                    field_errors={"expectedVersion": ["Expected an ISO-8601 timestamp."]},
                )

        if expected is None:
            return

        # HTTP-date headers have one-second resolution, so compare at that grain
        # or every request with a header would 409 on the microseconds.
        if int(updated_at.timestamp()) > int(expected.timestamp()):
            serializer = self.get_serializer(instance)
            raise Conflict(
                "This record changed since you opened it.",
                code=Codes.VERSION_CONFLICT,
                detail=f"Server copy was last updated at {updated_at.isoformat()}.",
                payload=serializer.data,
            )

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        instance = getattr(self, "_concurrency_instance", None)
        updated_at = getattr(instance, "updated_at", None)
        if updated_at is not None and "Last-Modified" not in response:
            response["Last-Modified"] = updated_at.strftime("%a, %d %b %Y %H:%M:%S GMT")
        return response


class AuditMixin:
    """Writes the api.md §1.10 audit row for every write this viewset performs."""

    #: Defaults to the model name; override where the UI uses another label
    #: (``PmsProject``, ``SalesInvoice``).
    audit_entity_type = None
    #: The field that produces the human-readable ``entity_label``.
    audit_label_field = None

    def get_audit_entity_type(self):
        if self.audit_entity_type:
            return self.audit_entity_type
        return self.queryset.model.__name__

    def get_audit_label(self, instance):
        if self.audit_label_field:
            return str(getattr(instance, self.audit_label_field, "") or "") or None
        for candidate in ("number", "code", "name", "title", "card_number", "lead_number"):
            value = getattr(instance, candidate, None)
            if value:
                return str(value)
        return None

    def write_audit(self, action, instance, before=None, after=None, description=None, **extra):
        return record_audit(
            client=self.get_client_id(),
            actor=getattr(self.request, "user", None),
            action=action,
            entity_type=self.get_audit_entity_type(),
            entity_id=getattr(instance, "id", None),
            entity_label=self.get_audit_label(instance),
            description=description,
            before=before,
            after=after,
            ip=request_ip(self.request),
            **extra,
        )


class AggregatesMixin:
    """Supplies ``aggregates`` for the list envelope (api.md §1.4).

    Override :meth:`get_aggregates` and compute with a database aggregate --
    db.md §15 is explicit that a list must never fetch all rows to sum in
    Python.
    """

    def get_aggregates(self, queryset):
        return {}


class BaseViewSet(
    TenantScopedMixin,
    ConcurrencyMixin,
    AuditMixin,
    AggregatesMixin,
    viewsets.GenericViewSet,
):
    permission_classes = [HasModulePermission]
    lookup_value_regex = "[^/]+"


class TenantModelViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    BaseViewSet,
):
    """Full CRUD with every cross-cutting rule applied."""

    #: When True, PATCH/PUT and DELETE are refused unless the row is a Draft
    #: (api.md §1.9 -- posted financial documents are cancelled, not edited).
    draft_only_writes = False
    draft_status_field = "status"
    draft_values = ("Draft",)

    #: Creates that allocate a number or post to stock/ledger accept an
    #: Idempotency-Key (api.md §1.8).
    idempotent_create = False

    def get_object(self):
        try:
            return super().get_object()
        except Exception as exc:
            from django.http import Http404

            if isinstance(exc, Http404):
                # api.md §1.11 -- cross-tenant access is 404, never 403.
                raise NotFound()
            raise

    # -- create -------------------------------------------------------------
    def create(self, request, *args, **kwargs):
        record = None
        if self.idempotent_create:
            try:
                record = idempotency.begin(
                    request, self.get_client_id(), request.path, request.data
                )
            except idempotency.Replay as replay:
                response = Response(replay.body, status=replay.status)
                response["Idempotency-Replayed"] = "true"
                return response

        try:
            response = super().create(request, *args, **kwargs)
        except Exception:
            idempotency.fail(record)
            raise

        return idempotency.finish(record, response, getattr(self, "_created_instance", None))

    @transaction.atomic
    def perform_create(self, serializer):
        instance = serializer.save(**self.create_defaults())
        self._created_instance = instance
        self._concurrency_instance = instance
        self.after_create(instance)
        self.write_audit("create", instance, after=snapshot(instance))
        return instance

    def create_defaults(self):
        defaults = {}
        model = self.queryset.model
        if hasattr(model, "client_id"):
            defaults["client_id"] = self.get_client_id()
        user = getattr(self.request, "user", None)
        if hasattr(model, "created_by_id") and getattr(user, "is_authenticated", False):
            defaults["created_by"] = user
            defaults["updated_by"] = user
        return defaults

    def after_create(self, instance):
        """Hook for side effects that belong in the creating transaction."""

    # -- update -------------------------------------------------------------
    @transaction.atomic
    def perform_update(self, serializer):
        instance = serializer.instance
        self.check_draft_only(instance, "edit")
        self.check_concurrency(instance)
        before = snapshot(instance)
        user = getattr(self.request, "user", None)
        extra = {}
        if hasattr(instance, "updated_by_id") and getattr(user, "is_authenticated", False):
            extra["updated_by"] = user
        updated = serializer.save(**extra)
        self._concurrency_instance = updated
        self.after_update(updated, before)
        self.write_audit("update", updated, before=before, after=snapshot(updated))
        return updated

    def after_update(self, instance, before):
        """Hook for side effects that belong in the updating transaction."""

    # -- delete -------------------------------------------------------------
    @transaction.atomic
    def perform_destroy(self, instance):
        self.check_draft_only(instance, "delete")
        self.check_delete_allowed(instance)
        before = snapshot(instance)
        user = getattr(self.request, "user", None)
        if hasattr(instance, "deleted_at"):
            # api.md §1.9 -- nothing financial is ever hard-deleted.
            instance.soft_delete(user if getattr(user, "is_authenticated", False) else None)
        else:
            instance.delete()
        self.write_audit("delete", instance, before=before)

    def check_delete_allowed(self, instance):
        """Hook for dependency checks that must block a delete (409)."""

    def check_draft_only(self, instance, verb):
        if not self.draft_only_writes:
            return
        current = getattr(instance, self.draft_status_field, None)
        if current not in self.draft_values:
            raise Conflict(
                f"Only a draft can be {verb}ed. This record is {current}.",
                code=Codes.DRAFT_ONLY,
                detail=(
                    "Posted documents are cancelled, which reverses their side "
                    "effects, rather than edited or deleted."
                ),
            )

    # -- retrieve -----------------------------------------------------------
    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        self._concurrency_instance = instance
        serializer = self.get_serializer(instance)
        return Response(serializer.data)


class ReadOnlyTenantViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, BaseViewSet
):
    """Lists and details only -- movements, audit, ledgers, derived worklists."""


class BulkDeleteMixin:
    """``POST /{collection}/bulk-delete/`` with ``{ ids: [] }`` (api.md §9.1)."""

    def bulk_delete(self, request):
        ids = request.data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            raise ValidationFailed(
                "Select at least one record to delete.",
                field_errors={"ids": ["Provide a non-empty list of ids."]},
            )
        queryset = self.get_queryset().filter(pk__in=ids)
        deleted = 0
        with transaction.atomic():
            for instance in queryset:
                self.perform_destroy(instance)
                deleted += 1
        return Response({"deleted": deleted}, status=status.HTTP_200_OK)
