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
from .batch_views import _can_see_batch_cost
from .valuation_views import InventoryDashboardView
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


class StockPositionView(APIView):
    """``GET /inventory/stock/`` -- every figure derived from the ledger."""

    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]

    def get(self, request):
        queryset = Item.objects.filter(
            client_id=request.client_id, deleted_at__isnull=True
        ).exclude(item_kind="Service").select_related("category")

        # If detail/type query param requested, return full position data
        if request.query_params.get("detail") == "true" or request.query_params.get("type"):
            return InventoryDashboardView().get(request)

        category_id = request.query_params.get("categoryId")
        if category_id:
            queryset = queryset.filter(category_id=category_id)

        location_id = request.query_params.get("locationId")
        items = list(queryset)
        stock.annotate_items_with_stock(request.client_id, items)

        if location_id:
            balances = stock.balances_for(
                request.client_id, [item.id for item in items], location_id
            )
            for item in items:
                row = balances.get(item.id, {})
                item.on_hand_qty = row.get("on_hand", ZERO) or ZERO
                item.damaged_qty = row.get("damaged", ZERO) or ZERO
                item.available_qty = item.on_hand_qty - (item.reserved_qty or ZERO)
                item.stock_status = stock.stock_status(item.available_qty, item.reorder_level)

        if request.query_params.get("belowReorder") == "true":
            items = [item for item in items if item.stock_status in ("Low Stock", "Critical")]
        status_filter = request.query_params.getlist("status")
        if status_filter:
            items = [item for item in items if item.stock_status in status_filter]

        rows = []
        total_value = ZERO
        for item in items:
            unit_cost = round4(item.cost_price)
            value = round2(D(item.on_hand_qty) * unit_cost)
            total_value += value
            rows.append(
                {
                    "itemId": str(item.id),
                    "sku": item.sku,
                    "name": item.name,
                    "category": item.category.name if item.category_id else None,
                    "uom": item.uom,
                    "onHand": item.on_hand_qty,
                    "reserved": item.reserved_qty,
                    "available": item.available_qty,
                    "damaged": item.damaged_qty,
                    "reorderLevel": round4(item.reorder_level),
                    "status": item.stock_status,
                    "unitCost": unit_cost,
                    "value": value,
                }
            )

        return Response(
            envelope(
                rows,
                aggregates={
                    "totalItems": len(rows),
                    "totalValue": round2(total_value),
                    "lowStock": sum(1 for row in rows if row["status"] == "Low Stock"),
                    "critical": sum(1 for row in rows if row["status"] == "Critical"),
                    "outOfStock": sum(1 for row in rows if D(row["available"]) <= ZERO),
                },
            )
        )

    def post(self, request):
        return InventoryDashboardView().post(request)


class StockSummaryView(APIView):
    """``GET /inventory/stock/summary/`` -- the KPI tiles only."""

    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]

    def get(self, request):
        items = list(
            Item.objects.filter(client_id=request.client_id, deleted_at__isnull=True)
            .exclude(item_kind="Service")
            .only("id", "reorder_level", "cost_price")
        )
        stock.annotate_items_with_stock(request.client_id, items)

        total_value = sum(
            (round2(D(item.on_hand_qty) * D(item.cost_price)) for item in items), ZERO
        )
        return Response(
            {
                "totalItems": len(items),
                "totalValue": round2(total_value),
                "lowStock": sum(1 for item in items if item.stock_status == "Low Stock"),
                "critical": sum(1 for item in items if item.stock_status == "Critical"),
                "outOfStock": sum(1 for item in items if D(item.available_qty) <= ZERO),
            }
        )


class CombinedGreyStockView(APIView):
    """
    GET /inventory/combined-grey-stock/
    Authoritative calculation of combined grey fabric stock across all fabric qualities,
    configured minimum threshold, shortage status, and external WIP.
    """
    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]

    def get(self, request):
        client_id = request.client_id
        location_id = request.query_params.get("locationId") or request.query_params.get("location_id")
        grey_calc = stock.calculate_combined_grey_stock(client_id, location_id=location_id)
        grey_setting = stock.get_combined_grey_stock_setting(client_id)
        threshold = grey_setting["threshold"]
        avail_grey = grey_calc["available_grey_stock"]

        shortage = ZERO
        if not grey_setting["is_configured"]:
            status_val = "Unconfigured"
        elif not grey_setting["is_enabled"]:
            status_val = "Disabled"
        elif avail_grey < threshold:
            shortage = round4(threshold - avail_grey)
            status_val = "Critical" if avail_grey <= ZERO else "Low Stock"
        else:
            status_val = "Sufficient"

        return Response({
            "physicalGrey": float(grey_calc["physical_grey_stock"]),
            "reservedGrey": float(grey_calc["reserved_grey_stock"]),
            "availableGrey": float(grey_calc["available_grey_stock"]),
            "externalWip": float(grey_calc["external_wip"]),
            "wipByParty": grey_calc["wip_by_party"],
            "uom": grey_calc["uom"],
            "qualitiesCount": grey_calc["qualities_count"],
            "takasCount": grey_calc["takas_count"],
            "threshold": float(threshold),
            "shortage": float(shortage),
            "status": status_val,
            "isConfigured": grey_setting["is_configured"],
            "isEnabled": grey_setting["is_enabled"],
            "channels": grey_setting["channels"],
            "incompatibleItems": grey_calc["incompatible_items"],
        })


class CombinedGreyStockSettingView(APIView):
    """
    GET /inventory/combined-grey-stock/setting/
    POST /inventory/combined-grey-stock/setting/
    View and update the minimum total grey stock threshold in AlertSetting.
    """
    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]

    def get(self, request):
        setting = stock.get_combined_grey_stock_setting(request.client_id)
        return Response({
            "threshold": float(setting["threshold"]),
            "isEnabled": setting["is_enabled"],
            "isConfigured": setting["is_configured"],
            "uom": setting["uom"],
            "channels": setting["channels"],
            "settingId": setting["setting_id"],
        })

    def post(self, request):
        client_id = request.client_id
        data = request.data or {}

        raw_threshold = data.get("threshold")
        if raw_threshold is None or str(raw_threshold).strip() == "":
            raise ValidationFailed(
                "Threshold amount is required.",
                field_errors={"threshold": ["Please specify a minimum grey stock threshold in meters."]},
            )
        try:
            threshold = round4(D(raw_threshold))
            if threshold < ZERO:
                raise ValueError()
        except (ValueError, TypeError, ArithmeticError):
            raise ValidationFailed(
                "Invalid threshold amount.",
                field_errors={"threshold": ["Threshold must be a non-negative number."]},
            )

        uom_str = data.get("uom", "Mtr")
        if uom_str and uom_str.strip().lower() not in ("mtr", "meter", "m", "meters"):
            raise ValidationFailed(
                "Combined grey stock threshold must be configured in linear meters.",
                field_errors={"uom": ["Only meter units are supported for combined grey stock aggregation."]},
            )

        is_enabled = data.get("isEnabled", True)
        if isinstance(is_enabled, str):
            is_enabled = is_enabled.lower() in ("true", "1", "yes")

        channels = data.get("channels") or ["in_app", "email"]
        if not isinstance(channels, list):
            channels = [channels]

        updated_setting = stock.set_combined_grey_stock_setting(
            client_id=client_id,
            threshold=threshold,
            is_enabled=is_enabled,
            channels=channels,
            user=request.user if getattr(request, "user", None) and request.user.is_authenticated else None,
        )

        return Response({
            "threshold": float(updated_setting["threshold"]),
            "isEnabled": updated_setting["is_enabled"],
            "isConfigured": updated_setting["is_configured"],
            "uom": updated_setting["uom"],
            "channels": updated_setting["channels"],
            "settingId": updated_setting["setting_id"],
            "message": "Combined grey stock threshold updated successfully.",
        })


class StockMovementViewSet(ReadOnlyTenantViewSet):
    """``GET /inventory/movements/`` -- the movement ledger.

    Read-only by design: movements are append-only and corrections are new
    reversing movements (api.md §7.2).
    """

    queryset = StockMovement.objects.select_related("item", "location")
    serializer_class = StockMovementSerializer
    required_permissions = ["view_inventory"]
    status_field = None
    filter_map = {
        "itemId": "item_id",
        "item_id": "item_id",
        "type": "type",
        "referenceType": "reference_type",
        "reference_type": "reference_type",
        "referenceId": "reference_id",
        "locationId": "location_id",
    }
    default_date_field = "movement_date"
    allowed_date_fields = ("movement_date", "created_at")
    search_fields = ["reference_number", "item__sku", "item__name", "notes"]
    ordering = ["-movement_date", "-created_at"]

    def get_aggregates(self, queryset):
        rows = queryset.aggregate(
            inward=Coalesce(
                Sum("quantity", filter=Q(quantity__gt=0)), Value(Decimal("0.0000")),
                output_field=QTY,
            ),
            outward=Coalesce(
                Sum("quantity", filter=Q(quantity__lt=0)), Value(Decimal("0.0000")),
                output_field=QTY,
            ),
            count=Count("id"),
        )
        rows["outward"] = abs(rows["outward"])
        # Inward/outward only add up within one unit; break them out per unit
        # and drop the combined figures when the filter spans several.
        by_uom = list(
            queryset.values("uom").annotate(
                inward=Coalesce(
                    Sum("quantity", filter=Q(quantity__gt=0)), Value(Decimal("0.0000")),
                    output_field=QTY,
                ),
                outward=Coalesce(
                    Sum("quantity", filter=Q(quantity__lt=0)), Value(Decimal("0.0000")),
                    output_field=QTY,
                ),
            ).order_by("uom")
        )
        for row in by_uom:
            row["outward"] = abs(row["outward"])
        rows["byUom"] = by_uom
        if len(by_uom) > 1:
            rows["inward"] = None
            rows["outward"] = None
        return rows


class StockAdjustmentView(APIView):
    """``POST /inventory/adjustments/`` -- replaces ``adjustItemStock``."""

    permission_classes = [HasModulePermission]
    required_permissions = ["adjust_stock"]

    @transaction.atomic
    def post(self, request):
        serializer = StockAdjustmentSerializer(
            data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        item = data["itemId"]
        location = data.get("locationId") or item.default_location
        if location is None:
            raise ValidationFailed(
                "Choose the location being adjusted.",
                field_errors={"locationId": ["Required."]},
            )

        lot = None
        lot_id = request.data.get("lotTakaId")
        if lot_id:
            lot = LotTaka.objects.filter(
                client_id=request.client_id, id=lot_id, item=item, deleted_at__isnull=True
            ).first()
            if lot is None:
                raise ValidationFailed("That taka does not exist for this item.", field_errors={"lotTakaId": ["Not found."]})
            location = lot.location

        current = stock.calculate_item_stock(request.client_id, item, location.id)
        current_on_hand = D(current["onHand"])
        # The base for an absolute count is the taka itself when one is named.
        base = D(lot.available_quantity) if lot is not None else current_on_hand
        if data.get("isAbsolute"):
            target_qty = round4(D(data["quantity"]))
            if target_qty < ZERO:
                raise ValidationFailed(
                    "Stock quantity cannot be adjusted to a negative value.",
                    field_errors={"quantity": ["Stock cannot be negative."]},
                )
            delta = round4(target_qty - base)
        else:
            delta = round4(data["quantity"])
        if base + delta < ZERO:
            raise ValidationFailed(
                f"Adjustment of {delta} would result in negative stock ({base + delta}). Current stock: {base}.",
                field_errors={"quantity": ["Adjustment results in negative stock."]},
            )
        if delta < ZERO:
            # Stock promised to open sales orders cannot be adjusted away.
            reserved = D(current["reserved"])
            free = D(current["onHand"]) - reserved
            if (
                location.code not in stock.BLOCKED_LOCATION_CODES
                and reserved > ZERO
                and abs(delta) > free
            ):
                raise BusinessRuleViolation(
                    f"Only {round4(max(free, ZERO))} of {item.name} is free; {round4(reserved)} is "
                    "reserved for open sales orders.",
                    code=Codes.INSUFFICIENT_STOCK,
                    payload={"reserved": str(round4(reserved)), "free": str(round4(max(free, ZERO)))},
                )

        if delta == ZERO:
            raise BusinessRuleViolation(
                "That adjustment would not change the stock figure.",
                code="NO_CHANGE",
            )

        # An adjustment worth more than STOCK_ADJUSTMENT_APPROVAL_LIMIT is not
        # posted: it waits in the approvals inbox and is posted by
        # inventory.approval_handlers once approved (PDF §16).
        from django.conf import settings as django_settings

        from .. import adjustments

        approval_limit = getattr(django_settings, "STOCK_ADJUSTMENT_APPROVAL_LIMIT", None)
        unit_cost = data.get("unitCost") or item.cost_price
        adjustment_value = round2(abs(delta) * D(unit_cost or 0))
        exceeds = approval_limit is not None and adjustment_value > D(str(approval_limit))

        if exceeds:
            req = adjustments.request_adjustment_approval(
                client_id=request.client_id,
                item=item,
                location=location,
                lot=lot,
                delta=delta,
                unit_cost=unit_cost,
                reason=data["reason"],
                value=adjustment_value,
                limit=D(str(approval_limit)),
                user=request.user,
            )
            return Response(
                {
                    "pendingApproval": True,
                    "approvalRequestId": str(req.id),
                    "approverRoleCodes": req.new_values.get("approverRoleCodes"),
                    "message": (
                        f"Adjustment worth {adjustment_value} is above the approval limit of "
                        f"{round2(D(str(approval_limit)))}. It will be posted once approved."
                    ),
                    "stock": stock.calculate_item_stock(request.client_id, item),
                    "adjustmentValue": adjustment_value,
                    "exceedsApprovalLimit": True,
                },
                status=status.HTTP_202_ACCEPTED,
            )

        movement = adjustments.post_adjustment(
            client_id=request.client_id,
            item=item,
            location=location,
            lot=lot,
            delta=delta,
            unit_cost=unit_cost,
            reason=data["reason"],
            user=request.user,
        )

        return Response(
            {
                "movement": StockMovementSerializer(movement).data,
                "stock": stock.calculate_item_stock(request.client_id, item),
                "adjustmentValue": adjustment_value,
                "exceedsApprovalLimit": False,
            },
            status=status.HTTP_201_CREATED,
        )


class StockTransferViewSet(TenantModelViewSet):
    """Inter-warehouse transfers; the workflow lives in ``warehouse_services``.

    Draft -> Requested (reserve) -> Approved -> In Transit (dispatch, partial
    allowed) -> Partially Received / Received -> Completed. Cancel is allowed
    until anything is received.
    """

    queryset = StockTransfer.objects.select_related(
        "from_location", "to_location", "requested_by", "approved_by", "received_by"
    ).prefetch_related("items__item")
    serializer_class = StockTransferSerializer
    audit_entity_type = "StockTransfer"
    audit_label_field = "transfer_number"
    required_permissions = ["view_inventory"]
    permission_map = {"write": ["create_transfer"]}
    status_field = "status"
    default_date_field = "transfer_date"
    search_fields = ["transfer_number", "notes", "reference_number"]
    filter_map = {"sourceLocationId": "from_location_id", "destLocationId": "to_location_id"}
    ordering = ["-transfer_date", "-created_at"]

    def _validate_takas(self, line, from_location_id):
        """Normalise a line's ``takas`` and check them against the source."""
        rows = []
        total = ZERO
        for raw in line.get("takas") or []:
            lot_id = raw.get("lotTakaId") or raw.get("id")
            lot = LotTaka.objects.filter(
                client_id=self.get_client_id(), id=lot_id, deleted_at__isnull=True
            ).first() if lot_id else None
            if lot is None:
                raise ValidationFailed("A selected taka does not exist.", field_errors={"takas": [f"Unknown taka {lot_id}."]})
            if lot.item_id != line["item"].id:
                raise ValidationFailed(
                    f"Taka {lot.taka_id or lot.taka_number} is not {line['item'].name}.",
                    field_errors={"takas": ["Item mismatch."]},
                )
            if str(lot.location_id) != str(from_location_id):
                raise ValidationFailed(
                    f"Taka {lot.taka_id or lot.taka_number} is not at the source location.",
                    field_errors={"takas": ["Wrong location."]},
                )
            qty = round4(D(raw.get("qty") if raw.get("qty") not in (None, "") else lot.available_quantity))
            if qty <= ZERO or qty > D(lot.available_quantity):
                raise ValidationFailed(
                    f"Taka {lot.taka_id or lot.taka_number}: quantity must be between 0 and "
                    f"{round4(lot.available_quantity)}.",
                    field_errors={"takas": ["Invalid taka quantity."]},
                )
            rows.append({"lotTakaId": str(lot.id), "qty": str(qty)})
            total += qty
        if rows and abs(total - round4(D(line["qty"]))) > Decimal("0.0001"):
            raise ValidationFailed(
                f"Selected takas ({total}) must add up to the line quantity ({round4(D(line['qty']))}).",
                field_errors={"takas": ["Taka total differs from the line quantity."]},
            )
        if not rows and getattr(line["item"], "tracking_type", "NONE") in ("LOT", "TAKA"):
            raise ValidationFailed(
                f"{line['item'].name} is taka-tracked: select the takas to transfer.",
                field_errors={"takas": ["Required for this item."]},
            )
        return rows

    def create(self, request, *args, **kwargs):
        from .. import warehouse_services

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        lines = data.get("items") or []
        if not lines:
            raise ValidationFailed(
                "A transfer needs at least one item.",
                field_errors={"items": ["Add at least one line."]},
            )
        for line in lines:
            line["takas"] = self._validate_takas(line, data["from_location"].id)
        draft = str(request.data.get("asDraft", "")).lower() in ("1", "true", "yes")
        with transaction.atomic():
            transfer = warehouse_services.create_transfer(
                client=request.user.client, user=request.user,
                from_location=data["from_location"], to_location=data["to_location"],
                transfer_date=data.get("transfer_date") or timezone.localdate(),
                lines=lines, notes=data.get("notes"), shipped_by=data.get("shipped_by"),
                draft=draft,
            )
            self.write_audit("create", transfer, description="Transfer drafted" if draft else "Transfer requested")
        return Response(self.get_serializer(transfer).data, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        transfer = self.get_object()
        if transfer.status != "Draft":
            raise Conflict(
                f"A {transfer.status} transfer cannot be edited; cancel it and raise a new one.",
                code=Codes.BAD_TARGET,
            )
        changes = {}
        if "notes" in request.data:
            changes["notes"] = request.data.get("notes") or None
        if "shipped_by" in request.data:
            changes["shipped_by"] = request.data.get("shipped_by") or None
        for key, value in changes.items():
            setattr(transfer, key, value)
        if changes:
            transfer.save(update_fields=[*changes, "updated_at"])
        return Response(self.get_serializer(transfer).data)

    def perform_destroy(self, instance):
        if instance.status != "Draft":
            raise Conflict("Only a draft transfer can be deleted; cancel it instead.", code=Codes.BAD_TARGET)
        super().perform_destroy(instance)

    def _run(self, request, fn, audit, description, permission=None, **kwargs):
        if permission:
            require_permission(request.user, permission)
        transfer = self.get_object()
        with transaction.atomic():
            transfer = fn(transfer, request.user, **kwargs)
            self.write_audit(audit, transfer, description=description)
        transfer = self.get_queryset().get(pk=transfer.pk)
        return Response(self.get_serializer(transfer).data)

    @action(detail=True, methods=["post"], url_path="request")
    def submit(self, request, pk=None):
        """Draft -> Requested: reserves the stock at the source."""
        from .. import warehouse_services

        return self._run(
            request, lambda t, _u: warehouse_services.reserve(t), "request", "Transfer requested"
        )

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        from .. import warehouse_services

        return self._run(
            request, warehouse_services.approve, "approve", "Transfer approved",
            permission="approve_transfer",
        )

    # ``dispatch`` is APIView's own entry point: a method of that name here
    # shadows it and breaks every request to this viewset. Keep the URL,
    # rename the method.
    @action(detail=True, methods=["post"], url_path="dispatch", url_name="dispatch")
    def dispatch_action(self, request, pk=None):
        """Source -> in-transit store. ``lines: [{lineId, qty}]`` for a
        partial dispatch; nothing dispatches all that is waiting."""
        from .. import warehouse_services

        return self._run(
            request, warehouse_services.dispatch, "dispatch", "Transfer dispatched",
            lines=request.data.get("lines") or None,
            shipped_by=request.data.get("shippedBy") or request.data.get("shipped_by"),
        )

    @action(detail=True, methods=["post"])
    def receive(self, request, pk=None):
        """In-transit store -> destination; ``lines`` for a partial receipt."""
        from .. import warehouse_services

        return self._run(
            request, warehouse_services.receive, "receive", "Transfer received",
            lines=request.data.get("lines") or None,
        )

    @action(detail=True, methods=["post"])
    def complete(self, request, pk=None):
        """Close it; settle anything still in transit (return or write off)."""
        from .. import warehouse_services

        return self._run(
            request, warehouse_services.complete, "complete", "Transfer completed",
            settlements=request.data.get("settlements") or None,
        )

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        """Reverses exactly this transfer's movements and releases every
        reservation; refused once anything has been received."""
        from .. import warehouse_services

        reason = request.data.get("reason")
        return self._run(
            request, lambda t, u: warehouse_services.cancel(t, u, reason=reason),
            "cancel", reason or "Transfer cancelled",
        )


class StockPositionScreenView(APIView):
    """
    GET  /inventory/stock-position/<screen>/  screen in overview | samples | scrap |
         usable | reprocessing | customer-returns.  Filters: q, itemId, designId,
         shadeId, locationId, batch, ownership, status, from, to, page, page_size.
         ``?export=csv`` returns every filtered row as CSV.
    POST /inventory/stock-position/scrap/<action>/  action in route-rework | dispose
         { lotTakaId, quantity, reason }
    Cost / value columns only for users with ledger access.
    """
    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]

    def get(self, request, screen):
        import csv

        from ..stock_position_services import SCREENS

        builder = SCREENS.get(screen)
        if builder is None:
            return Response({"message": "Unknown stock position screen.", "code": "NOT_FOUND"},
                            status=status.HTTP_404_NOT_FOUND)
        include_cost = _can_see_batch_cost(request.user)
        params = request.query_params.dict() if hasattr(request.query_params, "dict") else dict(request.query_params)
        data = builder(request.client_id, params, include_cost=include_cost)
        if request.query_params.get("export") == "csv":
            rows = data["results"]
            columns = []
            for row in rows:
                for key, value in row.items():
                    if key not in columns and not isinstance(value, (list, dict)):
                        columns.append(key)
            response = HttpResponse(content_type="text/csv")
            response["Content-Disposition"] = f'attachment; filename="stock-{screen}.csv"'
            writer = csv.writer(response)
            writer.writerow(columns)
            for row in rows:
                writer.writerow([row.get(c, "") if row.get(c) is not None else "" for c in columns])
            return response
        data["canSeeCost"] = include_cost
        return Response(data)

    def post(self, request, screen, action_name=None):
        from apps.core.permissions import require_permission

        from .. import stock_position_services as sp

        if screen != "scrap" or action_name not in ("route-rework", "dispose"):
            return Response({"message": "Unknown action.", "code": "NOT_FOUND"}, status=status.HTTP_404_NOT_FOUND)
        data = request.data
        if action_name == "route-rework":
            require_permission(request.user, "approve_qc", "Routing rejected stock to rework needs QC approval rights.")
            sp.route_to_rework(request.client_id, lot_taka_id=data.get("lotTakaId"), quantity=data.get("quantity"),
                               reason=data.get("reason"), user=request.user)
        else:
            require_permission(request.user, "adjust_stock", "Approving a scrap disposal needs stock adjustment rights.")
            sp.dispose_scrap(request.client_id, lot_taka_id=data.get("lotTakaId"), quantity=data.get("quantity"),
                             reason=data.get("reason"), user=request.user)
        return Response({"ok": True})

