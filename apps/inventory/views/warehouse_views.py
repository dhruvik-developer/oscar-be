"""Inventory views submodule."""
import json
import logging
import uuid
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Count, DecimalField, F, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import User
from apps.core.exceptions import (
    BusinessRuleViolation,
    Codes,
    Conflict,
    NotFound,
    PermissionDenied,
    ValidationFailed,
)
from apps.core.money import ZERO, D, round2, round4
from apps.core.numbering import allocate_number
from apps.core.pagination import envelope
from apps.core.permissions import HasModulePermission, require_permission
from apps.core.viewsets import ReadOnlyTenantViewSet, TenantModelViewSet
from apps.jobwork.models import JWOInward, JWOOutward, JWOReprocess, JobWorkOrder
from apps.masters import uom
from apps.masters.models import (
    Design,
    Fabric,
    FabricQuality,
    Item,
    ItemCategory,
    ItemPart,
    Location,
    Party,
    Shade,
)
from apps.purchase.models import (
    GoodsReceipt,
    GoodsReceiptLine,
    PurchaseBill,
    PurchaseBillLine,
    PurchaseOrder,
)
from apps.sales.models import SalesReturn, SalesReturnLine

from .. import costing_services, logistics_services, sampling_services
from .. import services as stock
from ..models import (
    FaultyPart,
    LotCostEntry,
    LotTaka,
    QCInspection,
    QCInspectionLine,
    QCInspectionLineDefect,
    QualityStandard,
    Sample,
    ServiceUsage,
    StockAudit,
    StockAuditLine,
    StockBalance,
    StockMovement,
    StockTransfer,
    StockTransferLine,
    TakaTagPrintLog,
    Trip,
    TripMaterialLine,
    ZoneRequest,
    ZoneRequestLine,
)
from ..serializers import (
    FaultyPartSerializer,
    ItemPartSerializer,
    LotCostEntrySerializer,
    QCInspectionLineDefectSerializer,
    QCInspectionLineSerializer,
    QCInspectionSerializer,
    QualityStandardSerializer,
    SampleSerializer,
    ServiceUsageSerializer,
    StockAdjustmentSerializer,
    StockAuditSerializer,
    StockMovementSerializer,
    StockTransferSerializer,
    TripMaterialLineSerializer,
    TripSerializer,
    ZoneRequestSerializer,
)

logger = logging.getLogger(__name__)

QTY = DecimalField(max_digits=18, decimal_places=4)
MONEY = DecimalField(max_digits=18, decimal_places=2)

TRIP_SIGNATORIES = ("consignor", "driver", "consignee")

class FaultyPartViewSet(TenantModelViewSet):
    queryset = FaultyPart.objects.select_related("item", "vendor", "serial")
    serializer_class = FaultyPartSerializer
    audit_entity_type = "FaultyPart"
    audit_label_field = "rma_number"
    required_permissions = ["view_inventory"]
    status_field = "status"
    default_date_field = "reported_date"
    search_fields = ["rma_number", "item__sku", "item__name", "notes"]
    ordering = ["-reported_date"]

    @transaction.atomic
    def perform_create(self, serializer):
        serializer.validated_data["rma_number"] = allocate_number(
            self.request.user.client, "RMA"
        )
        serializer.validated_data.setdefault("reported_by", self.request.user)
        part = super().perform_create(serializer)

        # api.md §7.2 -- reporting a faulty part posts a FAULTY movement,
        # which is what makes `damaged` a derived figure rather than a guess.
        location = part.location_id or part.item.default_location_id
        if location and part.item.holds_stock:
            stock.post_movement(
                client_id=self.get_client_id(),
                item=part.item_id,
                location=location,
                type="FAULTY",
                quantity=-D(part.quantity),
                unit_cost=part.item.cost_price,
                reference_type="FaultyPart",
                reference_id=part.id,
                reference_number=part.rma_number,
                movement_date=part.reported_date,
                user=self.request.user,
            )
        if part.serial_id:
            stock.set_serial_status([part.serial_id], "faulty")
        return part

    @action(detail=True, methods=["patch"])
    def notes(self, request, pk=None):
        part = self.get_object()
        part.notes = request.data.get("notes")
        part.save(update_fields=["notes", "updated_at"])
        return Response(self.get_serializer(part).data)


class ServiceUsageViewSet(TenantModelViewSet):
    queryset = ServiceUsage.objects.select_related("item", "party")
    serializer_class = ServiceUsageSerializer
    audit_entity_type = "ServiceUsage"
    audit_label_field = "ticket_number"
    required_permissions = ["view_inventory"]
    status_field = None
    default_date_field = "used_on"
    search_fields = ["ticket_number", "technician", "item__sku"]
    ordering = ["-used_on"]

    @transaction.atomic
    def perform_create(self, serializer):
        serializer.validated_data["ticket_number"] = allocate_number(
            self.request.user.client, "TKT"
        )
        serializer.validated_data.setdefault("used_by", self.request.user)
        usage = super().perform_create(serializer)

        location = usage.item.default_location_id
        if location and usage.item.holds_stock:
            stock.assert_sufficient_stock(
                self.get_client_id(), usage.item, usage.quantity, location, usage.item.name
            )
            stock.post_movement(
                client_id=self.get_client_id(),
                item=usage.item_id,
                location=location,
                type="SERVICE_USAGE",
                quantity=-D(usage.quantity),
                unit_cost=usage.item.cost_price,
                reference_type="ServiceUsage",
                reference_id=usage.id,
                reference_number=usage.ticket_number,
                movement_date=usage.used_on,
                user=self.request.user,
            )
        return usage


class ZoneRequestViewSet(TenantModelViewSet):
    queryset = ZoneRequest.objects.select_related("zone_location").prefetch_related("lines__item")
    serializer_class = ZoneRequestSerializer
    audit_entity_type = "ZoneRequest"
    audit_label_field = "request_number"
    required_permissions = ["view_inventory"]
    status_field = "status"
    default_date_field = "request_date"
    search_fields = ["request_number", "requested_by_name", "notes"]
    ordering = ["-requested_at"]

    @transaction.atomic
    def perform_create(self, serializer):
        lines = serializer.validated_data.pop("lines", [])
        serializer.validated_data["request_number"] = allocate_number(
            self.request.user.client, "REQ"
        )
        serializer.validated_data.setdefault("requested_by", self.request.user)
        serializer.validated_data.setdefault("requested_by_name", self.request.user.name)
        request_row = super().perform_create(serializer)

        ZoneRequestLine.objects.bulk_create(
            [
                ZoneRequestLine(client_id=self.get_client_id(), zone_request=request_row, **line)
                for line in lines
            ]
        )
        return request_row

    @transaction.atomic
    def perform_update(self, serializer):
        """api.md §7 -- only the ``Fulfilled`` transition issues stock."""
        previous = serializer.instance.status
        request_row = super().perform_update(serializer)

        if previous != "Fulfilled" and request_row.status == "Fulfilled":
            from apps.core.permissions import require_permission

            require_permission(self.request.user, "approve_zone_request")
            for line in request_row.lines.filter(deleted_at__isnull=True).select_related("item"):
                quantity = D(line.requested_qty) - D(line.issued_qty)
                if quantity <= ZERO or not line.item.holds_stock:
                    continue
                stock.assert_sufficient_stock(
                    self.get_client_id(), line.item, quantity,
                    request_row.zone_location_id, line.item.name,
                )
                stock.post_movement(
                    client_id=self.get_client_id(),
                    item=line.item_id,
                    location=request_row.zone_location_id,
                    type="ZONE_ISSUE",
                    quantity=-quantity,
                    unit_cost=line.item.cost_price,
                    reference_type="ZoneRequest",
                    reference_id=request_row.id,
                    reference_number=request_row.request_number,
                    user=self.request.user,
                )
                line.issued_qty = line.requested_qty
                line.save(update_fields=["issued_qty", "updated_at"])
            request_row.issued_by = self.request.user
            request_row.save(update_fields=["issued_by", "updated_at"])
        return request_row


class ItemPartViewSet(TenantModelViewSet):
    queryset = ItemPart.objects.select_related("parent_item", "part_item")
    serializer_class = ItemPartSerializer
    audit_entity_type = "ItemPart"
    status_field = None
    filter_map = {"parentItemId": "parent_item_id"}
    search_fields = ["part_item__name", "part_item__sku"]
    ordering = ["part_item__name"]
    required_permissions = ["view_inventory"]


class StockAuditViewSet(TenantModelViewSet):
    queryset = StockAudit.objects.select_related("location", "conducted_by", "created_by").prefetch_related("lines__item")
    serializer_class = StockAuditSerializer
    audit_entity_type = "StockAudit"
    audit_label_field = "audit_number"
    required_permissions = ["view_inventory"]
    permission_map = {
        "read": ["view_inventory"],
        "write": ["perform_audit"],
        "create": ["perform_audit"],
        "update": ["perform_audit"],
        "partial_update": ["perform_audit"],
        "destroy": ["perform_audit"],
        "post_variances": ["perform_audit"],
        "save_counts": ["perform_audit"],
        "cancel": ["perform_audit"],
    }
    status_field = "status"
    default_date_field = "period_month"
    ordering = ["-period_month", "-created_at"]
    filter_map = {
        "status": "status",
        "location": "location_id",
        "locationId": "location_id",
        "period_month": "period_month",
        "periodMonth": "period_month",
    }
    draft_only_writes = True
    draft_values = ("Draft", "Counting")

    def create(self, request, *args, **kwargs):
        # 1. Require authorized user to perform audit
        require_permission(request.user, "perform_audit", "You do not have permission to create stock audit cycles.")

        # 2. Extract and normalize parameters
        raw_period = request.data.get("period_month") or request.data.get("periodMonth")
        if raw_period:
            try:
                parts = str(raw_period).split("-")
                period_month = date(int(parts[0]), int(parts[1]), 1)
            except Exception:
                raise ValidationFailed("Invalid period_month. Expected format YYYY-MM or YYYY-MM-01.")
        else:
            today = timezone.localdate()
            period_month = date(today.year, today.month, 1)

        location_id = request.data.get("location") or request.data.get("location_id") or request.data.get("locationId") or None
        if location_id in ("", "null", "None"):
            location_id = None
        resume_if_exists = bool(request.data.get("resume_if_exists") or request.query_params.get("resume") == "true")

        # 3. Transaction-level advisory lock & duplicate validation
        with transaction.atomic():
            from django.db import connection
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    [f"stock_audit_{self.get_client_id()}_{location_id or 'all'}_{period_month.isoformat()}"],
                )

            # Check if an audit for this warehouse and period is already Posted
            posted_qs = StockAudit.objects.filter(
                client_id=self.get_client_id(),
                period_month=period_month,
                status="Posted",
                deleted_at__isnull=True,
            )
            if location_id:
                posted_qs = posted_qs.filter(location_id=location_id)
            else:
                posted_qs = posted_qs.filter(location__isnull=True)

            posted = posted_qs.first()
            if posted:
                loc_label = posted.location.name if posted.location else "All Godowns"
                raise Conflict(
                    f"Audit {posted.audit_number} for {loc_label} ({period_month.strftime('%B %Y')}) has already been posted and closed. Posted audits are locked and cannot be recreated.",
                    code="AUDIT_ALREADY_POSTED",
                )

            # Check if an active audit (Draft or Counting) already exists
            active_qs = StockAudit.objects.filter(
                client_id=self.get_client_id(),
                period_month=period_month,
                status__in=["Draft", "Counting"],
                deleted_at__isnull=True,
            ).select_related("location", "conducted_by", "created_by").order_by("-created_at")
            if location_id:
                active_qs = active_qs.filter(location_id=location_id)
            else:
                active_qs = active_qs.filter(location__isnull=True)

            existing = active_qs.first()
            if existing:
                if resume_if_exists:
                    serializer = self.get_serializer(existing)
                    return Response(serializer.data, status=status.HTTP_200_OK)

                loc_label = existing.location.name if existing.location else "All Godowns"
                period_label = period_month.strftime("%B %Y")
                return Response(
                    {
                        "error": f"An active {existing.status.lower()} audit ({existing.audit_number}) already exists for {loc_label} for {period_label}.",
                        "code": "ACTIVE_AUDIT_EXISTS",
                        "existingAuditId": str(existing.id),
                        "auditNumber": existing.audit_number,
                        "status": existing.status,
                        "periodMonth": str(existing.period_month),
                        "locationId": str(existing.location_id) if existing.location_id else None,
                        "locationName": loc_label,
                        "createdAt": existing.created_at.isoformat() if existing.created_at else None,
                    },
                    status=status.HTTP_409_CONFLICT,
                )

            # Safe to create
            data = dict(request.data)
            data["period_month"] = period_month.isoformat()
            data["location"] = location_id

            serializer = self.get_serializer(data=data)
            serializer.is_valid(raise_exception=True)
            audit = self.perform_create(serializer)
            out_serializer = self.get_serializer(audit)
            headers = self.get_success_headers(out_serializer.data)
            return Response(out_serializer.data, status=status.HTTP_201_CREATED, headers=headers)

    @transaction.atomic
    def perform_create(self, serializer):
        lines = serializer.validated_data.pop("lines", [])
        serializer.validated_data["audit_number"] = allocate_number(
            self.request.user.client, "AUD"
        )
        serializer.validated_data.setdefault("conducted_by", self.request.user)
        audit = super().perform_create(serializer)

        if not lines:
            # Snapshot items and their book stock for the chosen warehouse (or company-wide)
            items = list(
                Item.objects.filter(
                    client_id=self.get_client_id(), deleted_at__isnull=True
                ).exclude(item_kind="Service")
            )
            item_ids = [item.id for item in items]

            if audit.location_id:
                balances = stock.balances_for(self.get_client_id(), item_ids=item_ids, location_id=audit.location_id)
                lines = []
                for item in items:
                    on_hand = balances.get(item.id, {}).get("on_hand", ZERO) or ZERO
                    if D(on_hand) != ZERO:
                        lines.append({"item": item, "system_qty": on_hand})
                if not lines:
                    for item in items[:25]:
                        lines.append({"item": item, "system_qty": ZERO})
            else:
                stock.annotate_items_with_stock(self.get_client_id(), items)
                lines = [
                    {"item": item, "system_qty": item.on_hand_qty}
                    for item in items
                    if D(item.on_hand_qty) != ZERO
                ]
                if not lines:
                    for item in items[:25]:
                        lines.append({"item": item, "system_qty": ZERO})

        StockAuditLine.objects.bulk_create(
            [
                StockAuditLine(client_id=self.get_client_id(), stock_audit=audit, **line)
                for line in lines
            ]
        )
        return audit

    @action(detail=True, methods=["post", "patch"])
    def save_counts(self, request, pk=None):
        """Allows saving physical count progress for a Draft or Counting audit."""
        require_permission(request.user, "perform_audit")
        audit = self.get_object()
        if audit.status == "Posted":
            raise Conflict("Posted audits are locked and cannot be modified.", code="DRAFT_ONLY")

        lines_data = request.data.get("lines", [])
        updated_count = 0
        with transaction.atomic():
            for row in lines_data:
                line_id = row.get("id")
                counted_qty = row.get("physicalCount") if "physicalCount" in row else row.get("counted_qty")
                reason = row.get("reason")
                if line_id and counted_qty is not None:
                    try:
                        val = Decimal(str(counted_qty))
                    except (InvalidOperation, TypeError):
                        continue
                    fields_to_update = {"counted_qty": val, "updated_at": timezone.now()}
                    if reason is not None:
                        fields_to_update["reason"] = reason
                    StockAuditLine.objects.filter(
                        id=line_id,
                        stock_audit=audit,
                        client_id=self.get_client_id(),
                    ).update(**fields_to_update)
                    updated_count += 1

            if audit.status == "Draft":
                audit.status = "Counting"
                audit.save(update_fields=["status", "updated_at"])

        return Response({"savedLines": updated_count, "status": audit.status})

    @action(detail=True, methods=["post"])
    def post_variances(self, request, pk=None):
        """Turns every non-zero variance into an ``ADJUSTMENT`` movement and
        stamps the back-link (db.md §7.4)."""
        require_permission(request.user, "perform_audit")
        audit = self.get_object()
        if audit.status == "Posted":
            raise Conflict("This audit is already posted and locked.", code=Codes.ALREADY_DONE)

        # Save any counts supplied along with posting
        lines_data = request.data.get("lines", [])
        if lines_data:
            for row in lines_data:
                line_id = row.get("id")
                counted_qty = row.get("physicalCount") if "physicalCount" in row else row.get("counted_qty")
                reason = row.get("reason")
                if line_id and counted_qty is not None:
                    try:
                        val = Decimal(str(counted_qty))
                    except (InvalidOperation, TypeError):
                        continue
                    fields_to_update = {"counted_qty": val, "updated_at": timezone.now()}
                    if reason is not None:
                        fields_to_update["reason"] = reason
                    StockAuditLine.objects.filter(
                        id=line_id,
                        stock_audit=audit,
                        client_id=self.get_client_id(),
                    ).update(**fields_to_update)

        posted = 0
        with transaction.atomic():
            for line in audit.lines.filter(deleted_at__isnull=True).select_related("item"):
                if line.counted_qty is None:
                    continue
                variance = round4(D(line.counted_qty) - D(line.system_qty))
                if variance == ZERO:
                    continue

                location = audit.location_id or line.item.default_location_id
                if location is None:
                    continue

                movement = stock.post_movement(
                    client_id=request.client_id,
                    item=line.item_id,
                    location=location,
                    type="ADJUSTMENT",
                    quantity=variance,
                    unit_cost=line.item.cost_price,
                    notes=line.reason or f"Stock audit {audit.audit_number}",
                    user=request.user,
                )
                line.adjustment_movement = movement
                line.save(update_fields=["adjustment_movement", "updated_at"])
                posted += 1

            audit.status = "Posted"
            audit.posted_at = timezone.now()
            audit.save(update_fields=["status", "posted_at", "updated_at"])
            self.write_audit(
                "post", audit, description=f"{posted} variance adjustment(s) posted"
            )

        return Response({"postedAdjustments": posted, "status": audit.status})

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        """Allows cancelling an active Draft or Counting audit cycle."""
        require_permission(request.user, "perform_audit")
        audit = self.get_object()
        if audit.status == "Posted":
            raise Conflict("Posted audits are certified and cannot be cancelled.", code="POSTED_CANNOT_CANCEL")
        audit.status = "Cancelled"
        audit.save(update_fields=["status", "updated_at"])
        self.write_audit("cancel", audit, description="Audit cycle cancelled")
        return Response({"status": audit.status})


class SampleViewSet(TenantModelViewSet):
    queryset = Sample.objects.select_related(
        "party", "salesperson", "broker", "source_lot_taka", "fabric_quality", "design", "shade"
    )
    serializer_class = SampleSerializer
    required_permissions = ["view_inventory"]
    audit_entity_type = "Sample"
    audit_label_field = "sample_number"
    filter_map = {
        "partyId": "party_id",
        "salespersonId": "salesperson_id",
        "brokerId": "broker_id",
        "status": "status",
        "isReturnable": "is_returnable",
    }

    def create(self, request, *args, **kwargs):
        data = request.data
        source_lot_taka_id = data.get("sourceLotTakaId") or data.get("source_lot_taka_id") or data.get("lotTakaId")
        if not source_lot_taka_id:
            batch_code = data.get("batchCode") or data.get("batch_code")
            batch_id = data.get("batchId") or data.get("batch_id")
            lookup_q = Q(batch__batch_code=batch_code) if batch_code else (Q(batch_id=batch_id) if batch_id else None)
            if lookup_q:
                # Prefer takas already in Sample Stock
                t = LotTaka.objects.filter(
                    lookup_q,
                    client_id=request.client_id,
                    available_quantity__gt=0,
                    deleted_at__isnull=True
                ).order_by(
                    models.Case(
                        models.When(location__code="SYS-SAMPLE", then=0),
                        models.When(status="Sample", then=1),
                        default=2,
                    ),
                    "-available_quantity",
                ).first()
                if t:
                    source_lot_taka_id = t.id

        sample = sampling_services.issue_sample(
            client_id=request.client_id,
            party_id=data.get("partyId") or data.get("party_id"),
            salesperson_id=data.get("salespersonId") or data.get("salesperson_id"),
            broker_id=data.get("brokerId") or data.get("broker_id"),
            source_lot_taka_id=source_lot_taka_id,
            fabric_quality_id=data.get("fabricQualityId") or data.get("fabric_quality_id"),
            design_id=data.get("designId") or data.get("design_id"),
            shade_id=data.get("shadeId") or data.get("shade_id"),
            sample_type=data.get("sampleType") or data.get("sample_type", "CUT_PIECE"),
            quantity=data.get("quantity", 1),
            uom=data.get("uom", "MTR"),
            unit_cost=data.get("unitCost") or data.get("unit_cost") or data.get("rate"),
            is_returnable=data.get("isReturnable", True) if "isReturnable" in data else data.get("is_returnable", True),
            due_date=data.get("dueDate") or data.get("due_date"),
            remarks=data.get("remarks"),
            user=request.user,
        )
        return Response(self.get_serializer(sample).data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["get"], url_path="available-batches")
    def available_batches(self, request):
        client_id = request.client_id
        # ONLY return takas that are physically held in Sample Stock
        sample_qs = list(
            LotTaka.objects.filter(
                client_id=client_id,
                available_quantity__gt=0,
                deleted_at__isnull=True,
            ).filter(
                Q(location__code="SYS-SAMPLE") | Q(status=LotTaka.STATUS_SAMPLE)
            ).select_related("batch", "item", "location", "fabric_quality", "design", "shade").order_by("-created_at")
        )

        results = []
        for t in sample_qs:
            batch_code = t.batch.batch_code if t.batch else (t.lot_number or f"BATCH-{str(t.id)[:8]}")
            fabric_name = t.fabric_quality.name if t.fabric_quality else (t.item.name if t.item else "Fabric")
            unit_rate = float(t.rate or getattr(t.batch, "landed_cost_per_meter", None) or 0.0)
            results.append({
                "lotTakaId": str(t.id),
                "batchId": str(t.batch_id) if t.batch_id else None,
                "batchCode": batch_code,
                "fabricName": fabric_name,
                "design": t.design.name if t.design else "",
                "shade": t.shade.name if t.shade else "",
                "locationId": str(t.location_id) if t.location_id else None,
                "locationName": t.location.name if t.location else "Sample Stock",
                "availableQuantity": float(t.available_quantity),
                "uom": t.uom or "MTR",
                "rate": unit_rate,
                "unitCost": unit_rate,
                "isSampleStock": True,
                "stockType": "Sample Stock",
                "qcReference": t.qc_notes or "",
            })
        return Response(results)

    @action(detail=False, methods=["post"], url_path="cut-from-batch")
    @transaction.atomic
    def cut_from_batch(self, request):
        """Batch QC -> Sample Stock cut workflow."""
        client_id = request.client_id
        data = request.data
        lot_taka_id = data.get("lotTakaId") or data.get("sourceLotTakaId")
        batch_code = data.get("batchCode")
        batch_id = data.get("batchId")
        quantity = D(str(data.get("quantity") or 0))
        qc_ref = data.get("qcReference") or data.get("qcRef") or allocate_number(client_id, "QC")
        remarks = data.get("remarks") or ""

        if quantity <= ZERO:
            raise ValidationFailed("Sample cut quantity must be greater than zero.", field_errors={"quantity": ["Required."]})

        source_taka = None
        if lot_taka_id:
            source_taka = LotTaka.objects.select_for_update().filter(client_id=client_id, pk=lot_taka_id, deleted_at__isnull=True).first()
        elif batch_code:
            source_taka = LotTaka.objects.select_for_update().filter(client_id=client_id, batch__batch_code=batch_code, available_quantity__gte=quantity, deleted_at__isnull=True).first()
        elif batch_id:
            source_taka = LotTaka.objects.select_for_update().filter(client_id=client_id, batch_id=batch_id, available_quantity__gte=quantity, deleted_at__isnull=True).first()

        if not source_taka:
            raise NotFound("Source batch / taka not found with sufficient available quantity.")

        if source_taka.available_quantity < quantity:
            raise ValidationFailed(f"Available stock is only {source_taka.available_quantity}; cannot cut {quantity}.", field_errors={"quantity": ["Exceeds available stock."]})

        sample_loc = stock.system_location(client_id, "SAMPLE")
        target, _ = stock.relocate_lot(
            client_id=client_id,
            lot=source_taka,
            quantity=quantity,
            to_location_id=sample_loc.id,
            out_type="QC_PASS",
            in_type="QC_PASS",
            reference_type="BatchQC",
            reference_id=source_taka.batch_id or source_taka.id,
            reference_number=qc_ref,
            movement_date=timezone.localdate(),
            notes=f"Batch QC sample cut: {qc_ref}. {remarks}".strip(),
            user=request.user,
            qc_status="Passed",
            split_suffix="SMP",
        )
        target.status = LotTaka.STATUS_SAMPLE
        target.qc_status = "Passed"
        target.qc_notes = f"QC Ref: {qc_ref} | {remarks}".strip()
        target.save(update_fields=["status", "qc_status", "qc_notes", "updated_at"])

        landed_rate = D(target.rate or getattr(target.batch, "landed_cost_per_meter", None) or ZERO)
        sample_cost = round2(quantity * landed_rate)

        return Response({
            "success": True,
            "lotTakaId": str(target.id),
            "batchCode": target.batch.batch_code if target.batch else target.lot_number,
            "fabricName": target.fabric_quality.name if target.fabric_quality else (target.item.name if target.item else "Fabric"),
            "shade": target.shade.name if target.shade else "",
            "design": target.design.name if target.design else "",
            "quantity": float(target.received_quantity or target.available_quantity),
            "availableQuantity": float(target.available_quantity),
            "uom": target.uom or "MTR",
            "landedRate": float(landed_rate),
            "sampleCost": float(sample_cost),
            "qcReference": qc_ref,
            "message": f"Sample cut of {quantity} {target.uom} recorded from batch into Sample Stock.",
        }, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="return-sample")
    def return_sample_action(self, request, pk=None):
        sample = self.get_object()
        return_qty = request.data.get("returnQuantity") or request.data.get("quantity") or request.data.get("return_quantity")
        condition = request.data.get("condition", "Good")
        remarks = request.data.get("remarks")
        sample = sampling_services.return_sample(
            sample, return_qty, condition=condition, user=request.user, remarks=remarks
        )
        return Response(self.get_serializer(sample).data)

    @action(detail=True, methods=["post"], url_path="consume-sample")
    def consume_sample_action(self, request, pk=None):
        sample = self.get_object()
        consume_qty = request.data.get("consumeQuantity") or request.data.get("consume_quantity")
        remarks = request.data.get("remarks")
        sample = sampling_services.consume_sample(
            sample, consume_qty, user=request.user, remarks=remarks
        )
        return Response(self.get_serializer(sample).data)

    @action(detail=False, methods=["get"], url_path="dashboard")
    def dashboard(self, request):
        return Response(sampling_services.get_sample_expense_dashboard(request.client_id))


class TripViewSet(TenantModelViewSet):
    queryset = Trip.objects.select_related("transporter", "transporter_party").prefetch_related("materials")
    serializer_class = TripSerializer
    required_permissions = ["view_inventory"]
    audit_entity_type = "Trip"
    audit_label_field = "trip_number"
    filter_map = {
        "status": "status",
        "tripType": "trip_type",
        "transporterId": "transporter_id",
        "vehicleNumber": "vehicle_number",
    }

    def create(self, request, *args, **kwargs):
        data = request.data
        trip = logistics_services.create_trip(
            client_id=request.client_id,
            from_location=data.get("fromLocation") or data.get("from_location"),
            to_location=data.get("toLocation") or data.get("to_location"),
            from_warehouse_id=data.get("fromWarehouseId") or data.get("from_warehouse_id"),
            to_warehouse_id=data.get("toWarehouseId") or data.get("to_warehouse_id"),
            vehicle_number=data.get("vehicleNumber") or data.get("vehicle_number"),
            vehicle_id=data.get("vehicleId") or data.get("vehicle_id"),
            driver_id=data.get("driverId") or data.get("driver_id"),
            trip_date=data.get("tripDate") or data.get("trip_date"),
            transporter_id=data.get("transporterId") or data.get("transporter_id"),
            transporter_party_id=data.get("transporterPartyId") or data.get("transporter_party_id"),
            driver_name=data.get("driverName") or data.get("driver_name"),
            driver_mobile=data.get("driverMobile") or data.get("driver_mobile"),
            lr_number=data.get("lrNumber") or data.get("lr_number"),
            lr_date=data.get("lrDate") or data.get("lr_date"),
            trip_type=data.get("tripType") or data.get("trip_type", "OUTWARD"),
            freight_amount=data.get("freightAmount") or data.get("freight_amount", 0),
            loading_charges=data.get("loadingCharges") or data.get("loading_charges", 0),
            unloading_charges=data.get("unloadingCharges") or data.get("unloading_charges", 0),
            other_charges=data.get("otherCharges") or data.get("other_charges", 0),
            paid_by=data.get("paidBy") or data.get("paid_by", "Company"),
            cost_allocation_target=data.get("costAllocationTarget") or data.get("cost_allocation_target", "LOT"),
            material_lines=data.get("materials", []),
            remarks=data.get("remarks"),
            user=request.user,
        )
        return Response(self.get_serializer(trip).data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["get"], url_path="available-inventory")
    def available_inventory(self, request):
        location_ref = (
            request.query_params.get("location_id")
            or request.query_params.get("warehouse_id")
            or request.query_params.get("from_location")
            or request.query_params.get("fromLocation")
        )
        data = logistics_services.get_available_godown_inventory(request.client_id, location_ref)
        return Response(data)

    @action(detail=True, methods=["post"], url_path="allocate-freight")
    def allocate_freight(self, request, pk=None):
        trip = self.get_object()
        trip = logistics_services.allocate_trip_freight(trip, user=request.user)
        return Response(self.get_serializer(trip).data)

    @action(detail=True, methods=["post"], url_path="confirm-delivery")
    def confirm_delivery(self, request, pk=None):
        trip = self.get_object()
        data = request.data
        trip = logistics_services.confirm_trip_delivery(
            trip,
            delivery_time=data.get("deliveryTime") or data.get("delivery_time"),
            proof_file_id=data.get("proofFileId") or data.get("proof_file") or data.get("proof_file_id"),
            remarks=data.get("remarks"),
            user=request.user,
        )
        return Response(self.get_serializer(trip).data)

    @action(detail=True, methods=["post"], url_path="cancel")
    def cancel(self, request, pk=None):
        trip = self.get_object()
        reason = request.data.get("reason")
        trip = logistics_services.cancel_trip(trip, user=request.user, reason=reason)
        return Response(self.get_serializer(trip).data)

