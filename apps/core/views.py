"""
Platform endpoints: files, notifications, audit, settings, search, support,
exchange rates and the SSE event stream.
"""
import hashlib
import json
import time
from datetime import timedelta

from django.conf import settings as django_settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import Count, Q
from django.http import FileResponse, HttpResponse, StreamingHttpResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.parsers import FileUploadParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from . import files as file_service
from .audit import mark_read, record_audit, request_ip
from .exceptions import NotFound, ValidationFailed
from .models import (
    AuditLog,
    CompanyProfile,
    DocumentSignature,
    File,
    Notification,
    Setting,
    SupportTicket,
)
from .pagination import envelope
from .permissions import AllowPublic, HasModulePermission, IsAuthenticatedInTenant
from .serializers_platform import (
    AuditLogSerializer,
    FileSerializer,
    NotificationSerializer,
    SupportTicketSerializer,
    UploadUrlRequestSerializer,
)
from .viewsets import BaseViewSet, ReadOnlyTenantViewSet, TenantModelViewSet


# ---------------------------------------------------------------------------
# api.md §1.12 -- Files
# ---------------------------------------------------------------------------
class UploadUrlView(APIView):
    """``POST /files/upload-url/`` -> ``{ uploadUrl, fileId, expiresAt }``."""

    def post(self, request):
        serializer = UploadUrlRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        content_type, size = file_service.validate_upload_request(
            file_name=data["fileName"],
            content_type=data.get("contentType"),
            size=data["size"],
            scope=data["scope"],
        )

        row = File.objects.create(
            client_id=request.client_id,
            scope=data["scope"],
            storage_key=file_service.build_storage_key(
                request.client_id, data["scope"], data["fileName"]
            ),
            file_name=data["fileName"],
            content_type=content_type,
            file_size=size,
            status="pending",
            uploaded_by=request.user,
        )

        token = file_service.sign_upload(row.id)
        expires_at = timezone.now() + timedelta(
            seconds=django_settings.UPLOAD_URL_TTL_SECONDS
        )
        return Response(
            {
                "fileId": str(row.id),
                "uploadUrl": request.build_absolute_uri(
                    f"{django_settings.API_BASE_PATH}/files/{row.id}/upload/?token={token}"
                ),
                "expiresAt": expires_at,
            },
            status=status.HTTP_201_CREATED,
        )


class FileUploadView(APIView):
    """The ``PUT <uploadUrl>`` step. Authenticated by the signed token alone,
    so the browser can upload without re-sending the bearer token."""

    authentication_classes = []
    permission_classes = [AllowPublic]
    parser_classes = [MultiPartParser, FileUploadParser]

    def put(self, request, pk):
        token = request.query_params.get("token")
        if not token:
            raise ValidationFailed("Missing upload token.", code="UPLOAD_URL_INVALID")
        if file_service.verify_upload(token) != str(pk):
            raise ValidationFailed("Upload token does not match.", code="UPLOAD_URL_INVALID")

        row = File.objects.filter(pk=pk, status="pending").first()
        if row is None:
            raise NotFound("That upload is no longer pending.")

        payload = request.data.get("file") if hasattr(request.data, "get") else None
        data = payload.read() if payload is not None else request.body

        limit = file_service.max_bytes_for(row.scope)
        if len(data) > limit:
            raise ValidationFailed(
                "This file is larger than the declared size.",
                field_errors={"file": [f"Exceeds {limit // (1024 * 1024)} MB."]},
            )

        file_service.write_bytes(row.storage_key, data)
        row.file_size = len(data)
        row.checksum_sha256 = hashlib.sha256(data).hexdigest()
        row.save(update_fields=["file_size", "checksum_sha256"])
        return Response(status=status.HTTP_204_NO_CONTENT)


class FileCommitView(APIView):
    """``POST /files/{fileId}/commit/`` -- flips ``pending`` to ``committed``."""

    def post(self, request, pk):
        row = File.objects.filter(
            pk=pk, client_id=request.client_id
        ).first()
        if row is None:
            raise NotFound("That file no longer exists.")
        if row.status == "committed":
            return Response(FileSerializer(row, context={"request": request}).data)

        if not file_service.local_path(row.storage_key).exists():
            raise ValidationFailed(
                "The file was never uploaded.", code="UPLOAD_INCOMPLETE"
            )

        row.status = "committed"
        row.committed_at = timezone.now()
        row.save(update_fields=["status", "committed_at"])
        return Response(FileSerializer(row, context={"request": request}).data)


class FileDownloadView(APIView):
    """Signed download. Public by token so a client proof link works without
    an account (api.md §10.6)."""

    authentication_classes = []
    permission_classes = [AllowPublic]

    def get(self, request, pk):
        token = request.query_params.get("token")
        if not token or file_service.verify_download(token) != str(pk):
            raise NotFound("Invalid link.")

        row = File.objects.filter(pk=pk, status="committed").first()
        if row is None:
            raise NotFound("That file no longer exists.")

        path = file_service.local_path(row.storage_key)
        if not path.exists():
            raise NotFound("That file is no longer available.")

        response = FileResponse(open(path, "rb"), content_type=row.content_type)
        response["Content-Disposition"] = f'inline; filename="{row.file_name}"'
        # The PMS proof viewer renders PDFs in an <iframe> on the same origin
        # (vite dev proxy / same deployment), which the global DENY policy
        # would block. Relax clickjacking protection for this response only.
        response["X-Frame-Options"] = "SAMEORIGIN"
        return response


class FileViewSet(ReadOnlyTenantViewSet):
    queryset = File.objects.all()
    serializer_class = FileSerializer
    filter_soft_deleted = True
    filter_map = {"scope": "scope", "status": "status"}
    status_field = "status"
    search_fields = ["file_name"]
    ordering = ["-created_at"]


# ---------------------------------------------------------------------------
# api.md §1.13 -- Notifications
# ---------------------------------------------------------------------------
class NotificationViewSet(ReadOnlyTenantViewSet):
    queryset = Notification.objects.all()
    serializer_class = NotificationSerializer
    filter_soft_deleted = False
    status_field = None
    filter_map = {"category": "category", "type": "type"}
    ordering = ["-created_at"]

    search_fields = ["title", "body"]

    def get_queryset(self):
        """The recipient's own notifications. ``?view=`` picks the box:
        ``inbox`` (default: not dismissed, not snoozed), ``snoozed``,
        ``archived`` (dismissed) or ``all``."""
        queryset = super().get_queryset().filter(recipient=self.request.user).select_related("actor")
        params = self.request.query_params
        view = params.get("view") or "inbox"
        now = timezone.now()
        awake = Q(snoozed_until__isnull=True) | Q(snoozed_until__lte=now)
        if view == "inbox":
            queryset = queryset.filter(dismissed_at__isnull=True).filter(awake)
        elif view == "snoozed":
            queryset = queryset.filter(dismissed_at__isnull=True, snoozed_until__gt=now)
        elif view == "archived":
            queryset = queryset.filter(dismissed_at__isnull=False)
        if params.get("unread") == "true":
            queryset = queryset.filter(read_at__isnull=True)
        return queryset

    def get_aggregates(self, queryset):
        return {
            "unread": queryset.filter(read_at__isnull=True).count(),
            "total": queryset.count(),
        }

    def _mine(self):
        return Notification.objects.filter(client_id=self.request.client_id, recipient=self.request.user)

    @action(detail=True, methods=["post"])
    def dismiss(self, request, pk=None):
        """Clear it from the bell (kept under Archived)."""
        notification = self._mine().filter(pk=pk).first()
        if notification is None:
            raise NotFound()
        notification.dismissed_at = timezone.now()
        notification.read_at = notification.read_at or notification.dismissed_at
        notification.save(update_fields=["dismissed_at", "read_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["post"])
    def restore(self, request, pk=None):
        notification = self._mine().filter(pk=pk).first()
        if notification is None:
            raise NotFound()
        notification.dismissed_at = None
        notification.snoozed_until = None
        notification.save(update_fields=["dismissed_at", "snoozed_until"])
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["post"])
    def snooze(self, request, pk=None):
        """``{until}`` (ISO datetime) or ``{minutes}``: hide it until then; it
        comes back unread."""
        from datetime import timedelta

        from django.utils.dateparse import parse_datetime

        notification = self._mine().filter(pk=pk).first()
        if notification is None:
            raise NotFound()
        until = parse_datetime(str(request.data.get("until") or "")) if request.data.get("until") else None
        if until is None:
            try:
                minutes = int(request.data.get("minutes") or 0)
            except (TypeError, ValueError):
                minutes = 0
            if minutes <= 0:
                raise ValidationFailed("Pick when to be reminded.", field_errors={"until": ["Required."]})
            until = timezone.now() + timedelta(minutes=min(minutes, 60 * 24 * 30))
        if timezone.is_naive(until):
            until = timezone.make_aware(until)
        if until <= timezone.now():
            raise ValidationFailed("The reminder time must be in the future.", field_errors={"until": ["Future time."]})
        notification.snoozed_until = until
        notification.read_at = None
        notification.save(update_fields=["snoozed_until", "read_at"])
        return Response({"snoozedUntil": until.isoformat()})

    @action(detail=False, methods=["post"], url_path="clear-read")
    def clear_read(self, request):
        """Dismiss every read notification still in the inbox."""
        now = timezone.now()
        updated = self._mine().filter(read_at__isnull=False, dismissed_at__isnull=True).update(dismissed_at=now)
        return Response({"updated": updated})

    @action(detail=False, methods=["post"])
    def bulk(self, request):
        """``{ids: [...], action: read|unread|dismiss|restore}`` for the history page."""
        ids = [i for i in (request.data.get("ids") or []) if i][:500]
        verb = request.data.get("action")
        if not ids or verb not in ("read", "unread", "dismiss", "restore"):
            raise ValidationFailed("Pick notifications and an action.", field_errors={"action": ["Invalid."]})
        rows = self._mine().filter(pk__in=ids)
        now = timezone.now()
        if verb == "read":
            updated = rows.filter(read_at__isnull=True).update(read_at=now)
        elif verb == "unread":
            updated = rows.update(read_at=None)
        elif verb == "dismiss":
            updated = rows.filter(dismissed_at__isnull=True).update(dismissed_at=now)
            rows.filter(read_at__isnull=True).update(read_at=now)
        else:
            updated = rows.update(dismissed_at=None, snoozed_until=None)
        return Response({"updated": updated})

    @action(detail=True, methods=["post"])
    def read(self, request, pk=None):
        mark_read(self.get_object())
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["post"])
    def unread(self, request, pk=None):
        """Put a notification back in the unread list (the bell's "Mark as unread")."""
        notification = self.get_object()
        notification.read_at = None
        notification.save(update_fields=["read_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=False, methods=["post"], url_path="read-all")
    def read_all(self, request):
        queryset = self.get_queryset().filter(read_at__isnull=True)
        # ``get_queryset`` is the inbox; a snoozed notification stays snoozed.
        category = request.data.get("category")
        if category:
            queryset = queryset.filter(category=category)
        updated = queryset.update(read_at=timezone.now())
        return Response({"updated": updated})

    @action(detail=False, methods=["get"])
    def digest(self, request):
        """``GET /notifications/digest/`` -- counts per category for the topbar
        and sidebar badges. A grouped count over the partial index, never a
        table scan (db.md §2.6)."""
        rows = (
            Notification.objects.filter(
                client_id=request.client_id, recipient=request.user, read_at__isnull=True,
                dismissed_at__isnull=True,
            )
            .filter(Q(snoozed_until__isnull=True) | Q(snoozed_until__lte=timezone.now()))
            .values("category")
            .annotate(count=Count("id"))
        )
        by_category = {row["category"]: row["count"] for row in rows}
        return Response(
            {
                "total": sum(by_category.values()),
                "crm": by_category.get("crm", 0),
                "hrms": by_category.get("hrms", 0),
                "pms": by_category.get("pms", 0),
                "erp": by_category.get("erp", 0),
                "system": by_category.get("system", 0),
            }
        )


class EventStreamView(APIView):
    """``GET /events/stream/`` -- SSE for live badges and toasts (api.md §1.13).

    Replaces the frontend's ``storage``-event cross-tab hack, which could only
    ever see changes made in the same browser (api-integration.md §9.2). This
    also picks up changes made by other users.

    api.md accepts 60s polling as a v1 fallback, so the stream sends a heartbeat
    and closes after a bounded window rather than holding a worker forever --
    the client's EventSource reconnects.
    """

    def get(self, request):
        client_id = request.client_id
        user_id = request.user.id
        deadline = time.monotonic() + 55

        def events():
            last_seen = timezone.now()
            yield "retry: 5000\n\n"
            while time.monotonic() < deadline:
                fresh = list(
                    Notification.objects.filter(
                        client_id=client_id,
                        recipient_id=user_id,
                        created_at__gt=last_seen,
                    ).order_by("created_at")[:20]
                )
                for notification in fresh:
                    last_seen = max(last_seen, notification.created_at)
                    payload = json.dumps(
                        {
                            "id": str(notification.id),
                            "type": notification.type,
                            "category": notification.category,
                            "title": notification.title,
                            "body": notification.body,
                            "entityType": notification.entity_type,
                            "entityId": str(notification.entity_id)
                            if notification.entity_id
                            else None,
                        }
                    )
                    yield f"event: notification\ndata: {payload}\n\n"
                yield ": keep-alive\n\n"
                time.sleep(3)

        response = StreamingHttpResponse(events(), content_type="text/event-stream")
        response["Cache-Control"] = "no-cache"
        response["X-Accel-Buffering"] = "no"
        return response


# ---------------------------------------------------------------------------
# api.md §1.10 -- Audit
# ---------------------------------------------------------------------------
class AuditLogViewSet(ReadOnlyTenantViewSet):
    queryset = AuditLog.objects.all()
    serializer_class = AuditLogSerializer
    filter_soft_deleted = False
    status_field = None
    permission_map = {"read": ["view_audit_logs"]}
    filter_map = {
        "entityType": "entity_type",
        "entity_type": "entity_type",
        "entityId": "entity_id",
        "entity_id": "entity_id",
        "actorId": "actor_id",
        "actor_id": "actor_id",
        "action": "action",
    }
    default_date_field = "created_at"
    allowed_date_fields = ("created_at",)
    search_fields = ["description", "entity_label", "actor_name"]
    ordering = ["-created_at"]


# ---------------------------------------------------------------------------
# api.md §3.4 -- Settings blobs
# ---------------------------------------------------------------------------
SETTING_KEYS = {
    "preferences",
    "tax",
    "numbering",
    "print_templates",
    "print-templates",
    "notifications",
    "attendance_flexibility",
}

#: api.md §4.2 -- the steel-family HSN map, moved out of
#: ``ERPContext.mapCategoryToHSN`` so it is editable instead of hardcoded.
DEFAULT_TAX_SETTINGS = {
    "gstSlabs": [0, 5, 12, 18, 28],
    "defaultTaxPct": 18,
    "hsnDefaults": [
        {"match": ["angle", "channel", "section", "beam"], "hsnCode": "7216.32"},
        {"match": ["pipe", "tube", "hollow"], "hsnCode": "7306.30"},
        {"match": ["sheet", "coil", "plate", "flat"], "hsnCode": "7208.10"},
        {"match": ["bar", "rod", "round"], "hsnCode": "7214.10"},
        {"match": ["wire"], "hsnCode": "7217.10"},
        {"match": ["table", "furniture"], "hsnCode": "9403.20"},
        {"match": ["fastener", "bolt", "nut", "screw"], "hsnCode": "7318.15"},
        {"match": ["fabrication"], "hsnCode": "7308.90"},
    ],
    "fallbackHsnCode": "7216.99",
    "tdsSections": [
        {"section": "194C", "label": "Contractors", "rate": 1},
        {"section": "194J", "label": "Professional fees", "rate": 10},
        {"section": "194Q", "label": "Purchase of goods", "rate": 0.1},
    ],
    "tcsRate": 0.1,
}

DEFAULT_PREFERENCES = {
    "currency": "INR",
    "dateFormat": "dd/MM/yyyy",
    "financialYearStartMonth": 4,
    "timezone": "Asia/Kolkata",
}


def default_setting(key):
    return {
        "tax": DEFAULT_TAX_SETTINGS,
        "preferences": DEFAULT_PREFERENCES,
    }.get(key, {})


class SettingBlobView(APIView):
    """``GET/PUT /settings/{key}/`` -- one narrow key-value table (db.md §2.8)."""

    permission_classes = [HasModulePermission]
    permission_map = {"read": [], "write": ["manage_company_profile"]}

    def get(self, request, key):
        key = key.replace("-", "_")
        row = Setting.objects.filter(client_id=request.client_id, key=key).first()
        return Response(row.value if row else default_setting(key))

    def put(self, request, key):
        key = key.replace("-", "_")
        if not isinstance(request.data, dict):
            raise ValidationFailed(
                "Settings must be an object.", field_errors={"value": ["Expected an object."]}
            )
        row, _ = Setting.objects.update_or_create(
            client_id=request.client_id,
            key=key,
            defaults={"value": request.data, "updated_by": request.user},
        )
        return Response(row.value)


def hsn_for_category(client_id, category_name):
    """api.md §4.2 -- resolve the HSN default for a category from settings."""
    row = Setting.objects.filter(client_id=client_id, key="tax").first()
    config = row.value if row and isinstance(row.value, dict) else DEFAULT_TAX_SETTINGS
    name = (category_name or "").lower()
    for rule in config.get("hsnDefaults", []):
        if any(token in name for token in rule.get("match", [])):
            return rule.get("hsnCode")
    return config.get("fallbackHsnCode", DEFAULT_TAX_SETTINGS["fallbackHsnCode"])


# ---------------------------------------------------------------------------
# api.md §12.3 -- Support tickets
# ---------------------------------------------------------------------------
class SupportTicketViewSet(TenantModelViewSet):
    queryset = SupportTicket.objects.all()
    serializer_class = SupportTicketSerializer
    audit_entity_type = "SupportTicket"
    audit_label_field = "subject"
    status_field = "status"
    ordering = ["-created_at"]

    def get_queryset(self):
        """api.md §12.3 -- ``GET /support/tickets/`` is "the caller's own tickets"."""
        return super().get_queryset().filter(raised_by=self.request.user)

    def create_defaults(self):
        return {**super().create_defaults(), "raised_by": self.request.user}


# ---------------------------------------------------------------------------
# api.md §12.4 -- Exchange rates
# ---------------------------------------------------------------------------
class ExchangeRatesView(APIView):
    """Moved server-side from ``utils/currencyUtils.js``.

    The browser calling ``open.er-api.com`` directly means CORS exposure,
    third-party rate limits, and two users seeing different rates for the same
    document. Cached for a day (api.md §12.4).
    """

    def get(self, request):
        cached = cache.get("exchange_rates")
        if cached:
            return Response(cached)

        payload = {
            "base": "USD",
            "rates": {},
            "fetchedAt": None,
            "stale": True,
        }
        try:
            import urllib.request

            with urllib.request.urlopen(
                django_settings.EXCHANGE_RATE_URL, timeout=8
            ) as handle:
                data = json.loads(handle.read().decode())
            payload = {
                "base": data.get("base_code", "USD"),
                "rates": data.get("rates", {}),
                "fetchedAt": timezone.now(),
                "stale": False,
            }
            cache.set(
                "exchange_rates", payload, django_settings.EXCHANGE_RATE_CACHE_SECONDS
            )
        except Exception:
            # A rate provider being down must not break a page that only wants
            # to render a currency symbol.
            payload["fetchedAt"] = timezone.now()

        return Response(payload)


# ---------------------------------------------------------------------------
# api.md §12 -- Global search (the command palette)
# ---------------------------------------------------------------------------
SEARCH_TYPES = [
    "item", "customer", "vendor", "party", "salesOrder", "purchaseOrder",
    "invoice", "challan", "proforma", "lead", "deal", "project", "employee",
    "task", "page",
]


class GlobalSearchView(APIView):
    """``GET /search/?q=`` -- a flat ranked list ``{ type, id, label, sublabel, url }``.

    Replaces ``CommandPalette``'s scan of eight context arrays, which
    under-reports the moment those lists are server-paginated
    (api-integration.md §9.1.3).
    """

    def get(self, request):
        query = (request.query_params.get("q") or "").strip()
        if len(query) < 2:
            return Response(envelope([]))

        wanted = set(
            filter(None, (request.query_params.get("types") or "").split(","))
        ) or set(SEARCH_TYPES)
        limit = min(int(request.query_params.get("limit") or 8), 25)
        client_id = request.client_id
        results = []

        if "item" in wanted:
            from apps.masters.models import Item

            for row in Item.objects.filter(
                Q(name__icontains=query) | Q(sku__icontains=query),
                client_id=client_id,
                deleted_at__isnull=True,
            )[:limit]:
                results.append(
                    {
                        "type": "item",
                        "id": str(row.id),
                        "label": row.name,
                        "sublabel": row.sku,
                        "url": f"/inventory/items/{row.id}",
                    }
                )

        if wanted & {"customer", "vendor", "party"}:
            from apps.masters.models import Party

            for row in Party.objects.filter(
                Q(name__icontains=query) | Q(code__icontains=query) | Q(phone__icontains=query),
                client_id=client_id,
                deleted_at__isnull=True,
            )[:limit]:
                kind = "vendor" if row.type == "Vendor" else "customer"
                results.append(
                    {
                        "type": kind,
                        "id": str(row.id),
                        "label": row.name,
                        "sublabel": row.code,
                        "url": f"/parties/{row.id}",
                    }
                )

        document_sources = [
            ("salesOrder", "apps.sales.models", "SalesOrder", "order_number", "/sales/orders/"),
            ("invoice", "apps.sales.models", "SalesInvoice", "invoice_number", "/sales/invoices/"),
            ("challan", "apps.sales.models", "DeliveryChallan", "challan_number", "/sales/challans/"),
            # [Quotation & Proforma Invoice modules are commented out]
            # ("proforma", "apps.sales.models", "ProformaInvoice", "proforma_number",
             # "/sales/proforma-invoices/"),
            ("purchaseOrder", "apps.purchase.models", "PurchaseOrder", "po_number",
             "/purchase/orders/"),
        ]
        for kind, module_path, model_name, number_field, url_prefix in document_sources:
            if kind not in wanted:
                continue
            import importlib

            model = getattr(importlib.import_module(module_path), model_name)
            rows = model.objects.filter(
                Q(**{f"{number_field}__icontains": query}) | Q(party_name__icontains=query),
                client_id=client_id,
                deleted_at__isnull=True,
            )[:limit]
            for row in rows:
                results.append(
                    {
                        "type": kind,
                        "id": str(row.id),
                        "label": getattr(row, number_field) or "(draft)",
                        "sublabel": row.party_name,
                        "url": f"{url_prefix}{row.id}",
                    }
                )

        if "lead" in wanted:
            from apps.crm.models import Lead

            for row in Lead.objects.filter(
                Q(name__icontains=query) | Q(company__icontains=query)
                | Q(lead_number__icontains=query),
                client_id=client_id,
                deleted_at__isnull=True,
            )[:limit]:
                results.append(
                    {
                        "type": "lead",
                        "id": str(row.id),
                        "label": row.name,
                        "sublabel": row.company or row.lead_number,
                        "url": f"/crm/leads/{row.id}",
                    }
                )

        if "project" in wanted:
            from apps.pms.models import Project

            for row in Project.objects.filter(
                Q(code__icontains=query) | Q(customer_name__icontains=query)
                | Q(product_name__icontains=query),
                client_id=client_id,
                deleted_at__isnull=True,
            )[:limit]:
                results.append(
                    {
                        "type": "project",
                        "id": str(row.id),
                        "label": row.code,
                        "sublabel": row.customer_name,
                        "url": f"/pms/projects/{row.code}",
                    }
                )

        if "employee" in wanted:
            from apps.hrms.models import Employee

            for row in Employee.objects.filter(
                Q(name__icontains=query) | Q(employee_code__icontains=query),
                client_id=client_id,
                deleted_at__isnull=True,
            )[:limit]:
                results.append(
                    {
                        "type": "employee",
                        "id": str(row.id),
                        "label": row.name,
                        "sublabel": row.employee_code,
                        "url": f"/hrms/employees/{row.id}",
                    }
                )

        lowered = query.lower()
        results.sort(
            key=lambda row: (
                0 if (row["label"] or "").lower().startswith(lowered) else 1,
                len(row["label"] or ""),
            )
        )
        return Response(envelope(results))


# ---------------------------------------------------------------------------
# Digital signatures on printed documents
# ---------------------------------------------------------------------------
class DocumentSignatureView(APIView):
    """``GET/POST /signatures/`` -- e-signatures for any printable document.

    GET  ``?documentType=&documentId=`` -> ``{signatures: {signatory: {...}}}``
    POST ``{documentType, documentId, signatoryType, signature}`` records one
    signature line; ``signature: null`` removes it. Returns the same shape.
    """

    permission_classes = [IsAuthenticatedInTenant]

    @staticmethod
    def _slot_args(source):
        doc_type = str(source.get("documentType") or "").strip()[:64]
        doc_id = str(source.get("documentId") or "").strip()[:128]
        if not doc_type or not doc_id:
            raise ValidationFailed(
                "documentType and documentId are required.",
                field_errors={"documentType": ["Required."], "documentId": ["Required."]},
            )
        return doc_type, doc_id

    @staticmethod
    def _payload(client_id, doc_type, doc_id):
        rows = DocumentSignature.objects.filter(
            client_id=client_id, document_type=doc_type, document_id=doc_id
        )
        return {"signatures": {r.signatory: r.signature for r in rows}}

    def get(self, request):
        doc_type, doc_id = self._slot_args(request.query_params)
        return Response(self._payload(request.client_id, doc_type, doc_id))

    @transaction.atomic
    def post(self, request):
        data = request.data if isinstance(request.data, dict) else {}
        doc_type, doc_id = self._slot_args(data)
        signatory = str(data.get("signatoryType") or "").strip()[:64]
        if not signatory:
            raise ValidationFailed(
                "Invalid signatory.", field_errors={"signatoryType": ["Required."]}
            )
        signature = data.get("signature")
        if signature is not None and not isinstance(signature, dict):
            raise ValidationFailed(
                "Signature must be an object.", field_errors={"signature": ["Expected an object."]}
            )
        slot = dict(
            client_id=request.client_id,
            document_type=doc_type,
            document_id=doc_id,
            signatory=signatory,
        )
        if signature is None:
            DocumentSignature.objects.filter(**slot).delete()
        else:
            DocumentSignature.objects.update_or_create(
                **slot,
                defaults={
                    "signature": {**signature, "recordedBy": getattr(request.user, "email", None)},
                    "updated_by": request.user,
                },
            )
        record_audit(
            client=request.client_id,
            actor=request.user,
            action="sign",
            entity_type=doc_type,
            entity_id=doc_id,
            entity_label=doc_id,
            description=f"{signatory} signature {'removed' if signature is None else 'recorded'}",
            ip=request_ip(request),
        )
        return Response(self._payload(request.client_id, doc_type, doc_id))
