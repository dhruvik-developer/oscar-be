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


def _can_see_batch_cost(user):
    """Cost and profit on a batch are financial data: ``view_ledger`` only."""
    if getattr(user, "is_superuser", False):
        return True
    granted = getattr(user, "permission_ids", None)
    if granted is None:
        granted = user.effective_permissions()
    return "*" in granted or "view_ledger" in granted


def _batch_permissions(user):
    """Which Batch 360 operations this user may start (the server checks each
    again; this only decides which buttons the screen shows)."""
    from apps.core.permissions import has_permission

    return {
        "adjustStock": has_permission(user, "adjust_stock"),
        "qc": has_permission(user, "approve_qc"),
        "production": has_permission(user, "enter_production"),
        "verifyProduction": has_permission(user, "verify_production"),
        "jobWork": has_permission(user, "manage_jobwork"),
        "sell": has_permission(user, "create_invoice") or has_permission(user, "create_sales_order"),
        "purchaseReturn": has_permission(user, "approve_qc"),
        "cost": _can_see_batch_cost(user),
    }


def _batch_body(request, batch, status_code=status.HTTP_200_OK, **extra):
    """The Batch 360 detail as every batch endpoint returns it."""
    from ..batch_services import serialize_batch

    include_cost = _can_see_batch_cost(request.user)
    body = serialize_batch(batch, include_cost=include_cost, detail=True)
    body["canSeeCost"] = include_cost
    body["permissions"] = _batch_permissions(request.user)
    body.update(extra)
    return Response(body, status=status_code)


class BatchListView(APIView):
    """
    GET /inventory/batches/?q=&origin=&ownership=&supplierId=&from=&to=&status=&page=&page_size=
        &itemId=&designId=&shadeId=&locationId=&holding=available|reserved|qcHold|rework|
         secondQuality|rejected|withJobWorker
    The batch registry: one row per QC-passed batch posted to inventory stock.
    Design / shade / godown / holding look at the rolls in hand, so "navy in
    Surat" finds every batch with such a roll. ``summary`` totals the stock of
    all matching batches (not just the page).
    """
    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]

    def get(self, request):
        from django.db.models import Prefetch

        from ..batch_services import serialize_batch
        from ..batch_services import registry_eligible_takas
        from ..models import Batch, LotTaka

        params = request.query_params
        eligible_takas = registry_eligible_takas(request.client_id).select_related(
            "item", "item__category", "location", "fabric_quality", "design", "shade", "supplier"
        ).order_by("created_at")
        # Grey fabric at a dyeing mill belongs on the Dyeing side until it is
        # back: the part still at OSCAR stays Grey, so one batch can be in both.
        fabric_type = params.get("fabricType")
        sent_for_dyeing = LotTaka.objects.filter(
            client_id=request.client_id, deleted_at__isnull=True,
            jwo_outward_lines__outward__job_work_order__purchase_order__isnull=False,
        )
        at_mill = sent_for_dyeing.filter(available_quantity__gt=ZERO, location__code__startswith="SYS-WIP-")
        batch_scope = Q(pk__in=eligible_takas.values("batch_id"))
        if fabric_type == "DYED":
            batch_scope |= Q(pk__in=at_mill.values("batch_id"))
            eligible_takas = LotTaka.objects.filter(
                Q(batch__fabric_type="DYED", pk__in=eligible_takas.values("pk")) | Q(pk__in=at_mill.values("pk"))
            ).select_related(
                "item", "item__category", "location", "fabric_quality", "design", "shade", "supplier"
            ).order_by("created_at")
        qs = Batch.objects.filter(
            batch_scope, client_id=request.client_id, deleted_at__isnull=True,
        ).select_related(
            "item", "supplier", "owner_party"
        ).prefetch_related(
            Prefetch("takas", queryset=eligible_takas, to_attr="registry_eligible_takas")
        )
        if fabric_type == "DYED":
            qs = qs.filter(Q(fabric_type="DYED") | Q(pk__in=at_mill.values("batch_id")))
        elif fabric_type:
            in_house = LotTaka.objects.filter(
                client_id=request.client_id, deleted_at__isnull=True, available_quantity__gt=ZERO,
            ).exclude(location__code__startswith="SYS-WIP-")
            qs = qs.filter(fabric_type=fabric_type).exclude(
                Q(pk__in=sent_for_dyeing.values("batch_id")) & ~Q(pk__in=in_house.values("batch_id"))
            )
        q = (params.get("q") or "").strip()
        if q:
            matching_po_ids = PurchaseOrder.objects.filter(
                client_id=request.client_id,
                deleted_at__isnull=True,
                po_number__icontains=q,
            ).values_list("id", flat=True)
            matching_receipt_ids = GoodsReceipt.objects.filter(
                client_id=request.client_id,
                deleted_at__isnull=True,
            ).filter(
                Q(purchase_order_id__in=matching_po_ids)
                | Q(purchase_bill__purchase_order_id__in=matching_po_ids)
            ).values_list("id", flat=True)
            matching_bill_ids = PurchaseBill.objects.filter(
                client_id=request.client_id,
                deleted_at__isnull=True,
                purchase_order_id__in=matching_po_ids,
            ).values_list("id", flat=True)
            qs = qs.filter(
                Q(batch_code__icontains=q)
                | Q(source_document_number__icontains=q)
                | Q(item__name__icontains=q)
                | Q(supplier__name__icontains=q)
                | Q(purchase_order__po_number__icontains=q)
                | Q(purchase_order_line__purchase_order__po_number__icontains=q)
                | Q(goods_receipt__grn_number__icontains=q)
                | Q(takas__lot_number__icontains=q)
                | Q(takas__taka_id__icontains=q)
                | Q(takas__source_document_number__icontains=q)
                | Q(source_document_type__iexact="PurchaseOrder", source_document_id__in=matching_po_ids)
                | Q(purchase_order_line__purchase_order_id__in=matching_po_ids)
                | Q(goods_receipt_id__in=matching_receipt_ids)
                | Q(source_document_type__iexact="GRN", source_document_id__in=matching_receipt_ids)
                | Q(source_document_type__iexact="GoodsReceipt", source_document_id__in=matching_receipt_ids)
                | Q(source_document_type__iexact="PurchaseBill", source_document_id__in=matching_bill_ids)
            ).distinct()
        for param, field in (("origin", "origin"), ("ownership", "ownership"),
                             ("supplierId", "supplier_id"), ("itemId", "item_id")):
            if params.get(param):
                qs = qs.filter(**{field: params[param]})
        po_filter = params.get("poId") or params.get("purchaseOrderId")
        if po_filter:
            po_qs = PurchaseOrder.objects.filter(client_id=request.client_id, deleted_at__isnull=True)
            po = po_qs.filter(po_number__iexact=po_filter).first()
            if po is None:
                try:
                    po = po_qs.filter(pk=uuid.UUID(str(po_filter))).first()
                except (TypeError, ValueError, AttributeError):
                    po = None
            if po is None:
                qs = qs.none()
            else:
                receipt_ids = GoodsReceipt.objects.filter(
                    client_id=request.client_id,
                    deleted_at__isnull=True,
                ).filter(
                    Q(purchase_order_id=po.id) | Q(purchase_bill__purchase_order_id=po.id)
                ).values_list("id", flat=True)
                bill_ids = PurchaseBill.objects.filter(
                    client_id=request.client_id,
                    deleted_at__isnull=True,
                    purchase_order_id=po.id,
                ).values_list("id", flat=True)
                qs = qs.filter(
                    Q(purchase_order_id=po.id)
                    | Q(purchase_order_line__purchase_order_id=po.id)
                    | Q(source_document_type__iexact="PurchaseOrder", source_document_id=po.id)
                    | Q(goods_receipt__purchase_order_id=po.id)
                    | Q(goods_receipt_id__in=receipt_ids)
                    | Q(source_document_type__iexact="GRN", source_document_id__in=receipt_ids)
                    | Q(source_document_type__iexact="GoodsReceipt", source_document_id__in=receipt_ids)
                    | Q(source_document_type__iexact="PurchaseBill", source_document_id__in=bill_ids)
                ).distinct()
        if params.get("qcStatus"):
            qs = qs.filter(
                pk__in=eligible_takas.filter(qc_status=params["qcStatus"]).values("batch_id")
            )
        if params.get("from"):
            qs = qs.filter(created_on__gte=params["from"])
        if params.get("to"):
            qs = qs.filter(created_on__lte=params["to"])
        # Roll-level filters, in one condition so the same roll must match them all.
        from ..batch_services import roll_filter, stock_totals

        roll = roll_filter(params)
        if roll is not None:
            qs = qs.filter(pk__in=eligible_takas.filter(roll).values("batch_id"))
        qs = qs.order_by("-created_on", "-created_at")

        try:
            page = max(1, int(params.get("page") or 1))
            page_size = min(200, max(1, int(params.get("page_size") or 25)))
        except ValueError:
            page, page_size = 1, 25
        # ?withCost=1 adds cost / revenue / profit per batch (the batch
        # profitability report) for users who may see the ledger.
        with_cost = params.get("withCost") in ("1", "true") and _can_see_batch_cost(request.user)
        wanted_status = params.get("status")
        if wanted_status:
            rows = [
                r for r in (
                    serialize_batch(b, takas=b.registry_eligible_takas, include_cost=with_cost)
                    for b in qs
                )
                if r["status"] == wanted_status
            ]
            total = len(rows)
            rows = rows[(page - 1) * page_size: page * page_size]
        else:
            total = qs.count()
            rows = [
                serialize_batch(b, takas=b.registry_eligible_takas, include_cost=with_cost)
                for b in qs[(page - 1) * page_size: page * page_size]
            ]
        body = envelope(rows, count=total, page=page, page_size=page_size)
        body["canSeeCost"] = _can_see_batch_cost(request.user)
        body["awaitingQcCount"] = 0
        body["summary"] = stock_totals(
            request.client_id, qs.values("id"), roll_filter(params), eligible_only=True
        )
        return Response(body)

    def post(self, request):
        """POST /inventory/batches/ -- add OSCAR's own fabric by hand.
        { itemId, locationId, rolls: [{meters}], rate, reason, fabricType?: GREY|DYED, supplierId?,
          fabricQualityId?, designId?, shadeId?, width?, gsm?, date?, reference?, sendToQc? }
        The batch code is generated, never sent. QC is optional (sendToQc)."""
        from apps.core.permissions import require_permission

        from ..batch_services import create_manual_batch

        require_permission(request.user, "adjust_stock", "Adding stock by hand needs stock adjustment rights.")
        data = request.data
        cid = request.client_id

        def pick(model, key):
            return model.objects.filter(client_id=cid, pk=data.get(key)).first() if data.get(key) else None

        item, location = pick(Item, "itemId"), pick(Location, "locationId")
        if item is None or location is None:
            raise ValidationFailed("Choose the fabric and the godown.",
                                   field_errors={"itemId": ["Required."], "locationId": ["Required."]})
        batch = create_manual_batch(
            client=request.user.client, item=item, location=location, rolls=data.get("rolls"),
            total_meters=data.get("totalMeters"),
            rate=data.get("rate"), reason=data.get("reason"), supplier=pick(Party, "supplierId"),
            fabric_quality=pick(FabricQuality, "fabricQualityId"), shade=pick(Shade, "shadeId"),
            design=pick(Design, "designId"), fabric_type=data.get("fabricType") or "GREY",
            reference=(data.get("reference") or "").strip() or None, hsn_code=data.get("hsnCode"),
            width=data.get("width") or None, gsm=data.get("gsm") or None,
            received_on=data.get("date") or None, send_to_qc=bool(data.get("sendToQc")), user=request.user,
        )
        return _batch_body(request, batch, status.HTTP_201_CREATED)


class BatchGroupsView(APIView):
    """GET /inventory/batches/groups/?groupBy=po|fabric."""

    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]

    def get(self, request):
        from ..batch_registry import grouped_batch_registry

        with_cost = request.query_params.get("withCost") in ("1", "true") and _can_see_batch_cost(request.user)
        try:
            result = grouped_batch_registry(request.client_id, request.query_params, with_cost=with_cost)
            page = max(1, int(request.query_params.get("page") or 1))
            page_size = min(200, max(1, int(request.query_params.get("page_size") or 25)))
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        groups = result["groups"]
        group_count = len(groups)
        page_groups = groups[(page - 1) * page_size: page * page_size]
        body = envelope(page_groups, count=group_count, page=page, page_size=page_size)
        body["groups"] = page_groups
        body["groupCount"] = group_count
        body["batchCount"] = result["batchCount"]
        body["purchaseOrders"] = result["purchaseOrders"]
        body["summary"] = result["summary"]
        body["canSeeCost"] = _can_see_batch_cost(request.user)
        return Response(body)


class BatchEligibleStockView(APIView):
    """
    GET /inventory/batches/eligible/?itemId=&locationId=&batch=&orderId=
    Sellable stock of an item grouped by batch, for the sales batch picker
    (manual pick or QR scan of a batch tag).
    """
    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]

    def get(self, request):
        from ..batch_services import eligible_sale_stock

        item_id = request.query_params.get("itemId")
        if not item_id:
            raise ValidationFailed("itemId is required.", field_errors={"itemId": ["Required."]})
        rows = eligible_sale_stock(
            request.client_id,
            item_id,
            location_id=request.query_params.get("locationId") or None,
            batch_code=request.query_params.get("batch") or None,
            order_id=request.query_params.get("orderId") or None,
        )
        return Response({"results": rows})


class BatchChargeView(APIView):
    """
    POST /inventory/batches/allocate-charge/
    { batches: [code], amount, costType, basis: METER|TAKA|MANUAL,
      manual?: {code: amount}, reference?, remarks? }
    Split one freight / processing / other charge over batches the way the
    user chooses. Posting cost needs the same access as seeing it.
    """
    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]

    def post(self, request):
        from apps.core.permissions import require_permission

        from ..batch_services import allocate_charge

        require_permission(request.user, "view_ledger", "Posting batch costs needs ledger access.")
        data = request.data
        rows = allocate_charge(
            client_id=request.client_id,
            batch_codes=data.get("batches") or [],
            amount=data.get("amount"),
            cost_type=data.get("costType"),
            basis=data.get("basis"),
            manual=data.get("manual") or {},
            reference=(data.get("reference") or "").strip() or None,
            remarks=data.get("remarks"),
            user=request.user,
        )
        return Response({"allocations": rows}, status=status.HTTP_201_CREATED)


class BatchLossView(APIView):
    """
    POST  /inventory/batches/<code>/losses/  classify rejected metres
          { quantity, cause, responsiblePartyId?, process?, outcome?, reference?, notes?, date? }
    PATCH /inventory/batch-losses/<id>/      move a claim along
          { outcome?, claimStatus?, recoveredAmount?, reference?, notes? }
    Classifying needs QC approval rights; claim money needs ledger access.
    """
    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]

    def post(self, request, code):
        from apps.core.permissions import require_permission

        from ..batch_services import find_batch, record_loss, serialize_batch

        require_permission(request.user, "approve_qc", "Classifying rejections needs QC approval rights.")
        batch = find_batch(request.client_id, code)
        if batch is None:
            return Response({"message": f"No batch found for '{code}'.", "code": "NOT_FOUND"},
                            status=status.HTTP_404_NOT_FOUND)
        data = request.data
        record_loss(
            batch, quantity=data.get("quantity"), cause=data.get("cause") or "NORMAL_LOSS",
            responsible_party_id=data.get("responsiblePartyId"), process=data.get("process"),
            outcome=data.get("outcome") or "PENDING", reference=data.get("reference"),
            notes=data.get("notes"), loss_date=data.get("date") or None, user=request.user,
        )
        return _batch_body(request, batch, status.HTTP_201_CREATED)

    def patch(self, request, loss_id):
        from apps.core.permissions import require_permission

        from ..batch_services import serialize_batch, update_loss
        from ..models import BatchLoss

        loss = BatchLoss.objects.filter(client_id=request.client_id, pk=loss_id, deleted_at__isnull=True).first()
        if loss is None:
            return Response({"message": "Rejection record not found.", "code": "NOT_FOUND"},
                            status=status.HTTP_404_NOT_FOUND)
        data = request.data
        money = any(k in data for k in ("claimStatus", "recoveredAmount"))
        require_permission(request.user, "view_ledger" if money else "approve_qc")
        update_loss(
            loss, user=request.user, outcome=data.get("outcome"), claim_status=data.get("claimStatus"),
            recovered_amount=data.get("recoveredAmount"), reference=data.get("reference"),
            notes=data.get("notes"),
        )
        return _batch_body(request, loss.batch)


class CustomerMaterialView(APIView):
    """
    POST /inventory/batches/customer-receipt/
         {customerId, itemId, locationId, rolls: [{meters}], reference?, date?}
    POST /inventory/batches/<code>/deliver/  {rolls: [{lotTakaId, qty}], reference?}
    GET  /inventory/batches/physical-stock/   OSCAR vs customer material, by state
    """
    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]

    def get(self, request):
        from ..batch_services import physical_stock_summary

        return Response({"results": physical_stock_summary(request.client_id)})

    def post(self, request, code=None):
        from apps.core.permissions import require_permission

        from ..batch_services import deliver_to_owner, find_batch, receive_customer_material, serialize_batch

        require_permission(request.user, "adjust_stock", "Receiving or delivering customer material needs stock rights.")
        data = request.data
        if code is None:
            customer = Party.objects.filter(client_id=request.client_id, pk=data.get("customerId")).first() \
                if data.get("customerId") else None
            item = Item.objects.filter(client_id=request.client_id, pk=data.get("itemId")).first() \
                if data.get("itemId") else None
            location = Location.objects.filter(client_id=request.client_id, pk=data.get("locationId")).first() \
                if data.get("locationId") else None
            if customer is None or item is None or location is None:
                raise ValidationFailed("Choose the customer, fabric and godown.",
                                       field_errors={"customerId": ["Required."], "itemId": ["Required."],
                                                     "locationId": ["Required."]})
            batch, number = receive_customer_material(
                client=request.user.client, customer=customer, item=item, location=location,
                rolls=data.get("rolls"), total_meters=data.get("totalMeters"),
                reference=data.get("reference"), user=request.user,
                received_on=data.get("date") or None, fabric_type=data.get("fabricType") or "GREY",
            )
            body = serialize_batch(batch, detail=True)
            body["receiptNumber"] = number
            return Response(body, status=status.HTTP_201_CREATED)
        batch = find_batch(request.client_id, code)
        if batch is None:
            return Response({"message": f"No batch found for '{code}'.", "code": "NOT_FOUND"},
                            status=status.HTTP_404_NOT_FOUND)
        number = deliver_to_owner(batch, rolls=data.get("rolls"), reference=data.get("reference"), user=request.user)
        return _batch_body(request, batch, deliveryNumber=number)


class BatchTakaRegistrationView(APIView):
    """
    POST /inventory/batches/<code>/register-takas/  { takas: [{meters, weightKg?, width?, gsm?}] }
    Cut individual takas out of a batch received as bulk metres.
    """
    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]

    def post(self, request, code):
        from apps.core.permissions import require_permission

        from ..batch_services import find_batch, register_takas, serialize_batch

        require_permission(request.user, "adjust_stock", "Registering takas needs stock rights.")
        batch = find_batch(request.client_id, code)
        if batch is None:
            return Response({"message": f"No batch found for '{code}'.", "code": "NOT_FOUND"},
                            status=status.HTTP_404_NOT_FOUND)
        register_takas(batch, takas=request.data.get("takas"), user=request.user)
        return _batch_body(request, batch, status.HTTP_201_CREATED)


class BatchDetailView(APIView):
    """
    GET /inventory/batches/<code>/
    The batch traceability workspace (what a batch QR tag opens): overview,
    quantities, rolls and the full timeline; cost and profit only for users
    who may see the ledger. ``code`` may also be a roll's taka ID or lot number.
    """
    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]

    def get(self, request, code):
        from ..batch_services import find_batch, serialize_batch

        batch = find_batch(request.client_id, code)
        if batch is None:
            return Response(
                {"message": f"No batch found for '{code}'.", "code": "NOT_FOUND"},
                status=status.HTTP_404_NOT_FOUND,
            )
        return _batch_body(request, batch)

    def patch(self, request, code):
        """Set ownership: { ownership: OSCAR|CUSTOMER|THIRD_PARTY, ownerPartyId?, reason? }."""
        from apps.core.permissions import require_permission

        from ..batch_services import find_batch, serialize_batch, set_ownership

        require_permission(request.user, "adjust_stock", "Changing material ownership needs stock adjustment rights.")
        batch = find_batch(request.client_id, code)
        if batch is None:
            return Response({"message": f"No batch found for '{code}'.", "code": "NOT_FOUND"},
                            status=status.HTTP_404_NOT_FOUND)
        set_ownership(
            batch,
            ownership=request.data.get("ownership"),
            owner_party_id=request.data.get("ownerPartyId"),
            user=request.user,
            reason=request.data.get("reason"),
        )
        return _batch_body(request, batch)


class BatchActionView(APIView):
    """
    POST /inventory/batches/<code>/actions/<action>/  -- operations started from Batch 360:
         send-to-qc   {lotTakaIds?, quantity?, remarks?}           QC is optional; this starts it
         qc           {goodQty, secondQualityQty, reprocessQty, rejectedQty, holdQty, locationId,
                       remarks?, loss?: {cause, responsiblePartyId?, outcome?, process?}}
         rework-done  {lotTakaIds?, quantity?, cost?, remarks?}     reworked rolls back to QC
         yarn         {yarnItemId | yarnName, yarnLot?, yarnCount?, supplierId?, uom?, issuedQty,
                       consumedQty?, returnedQty?, wasteQty?, rate?, date?, remarks?}
         yarn-delete  {yarnId}
         fabric-type  {fabricType: GREY|DYED, reason?}
    Returns the refreshed batch. Sale, dyeing, purchase return and scrap use
    their own documents / endpoints.
    """
    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]
    ACTION_PERMISSION = {
        "send-to-qc": "approve_qc",
        "qc": "approve_qc",
        "rework-done": "approve_qc",
        "yarn": "enter_production",
        "yarn-delete": "enter_production",
        "fabric-type": "adjust_stock",
    }

    def post(self, request, code, action_name):
        from apps.core.permissions import require_permission

        from .. import batch360_services as b360
        from .. import qc as qc_service
        from ..batch_services import find_batch

        if action_name not in self.ACTION_PERMISSION:
            return Response({"message": "Unknown action.", "code": "NOT_FOUND"}, status=status.HTTP_404_NOT_FOUND)
        require_permission(request.user, self.ACTION_PERMISSION[action_name])
        batch = find_batch(request.client_id, code)
        if batch is None:
            return Response({"message": f"No batch found for '{code}'.", "code": "NOT_FOUND"},
                            status=status.HTTP_404_NOT_FOUND)
        data = request.data
        extra = {}
        if action_name == "send-to-qc":
            extra["reference"] = b360.send_to_qc(batch, lot_taka_ids=data.get("lotTakaIds") or None,
                                                 quantity=data.get("quantity"), remarks=data.get("remarks"),
                                                 user=request.user)
        elif action_name == "qc":
            extra["reference"], extra["movements"] = b360.record_qc(
                batch, quantities=qc_service.parse_quantities(data), good_location_id=data.get("locationId"),
                remarks=data.get("remarks"), loss=data.get("loss") or None, user=request.user,
            )
        elif action_name == "rework-done":
            cost = data.get("cost")
            if cost not in (None, "", 0, "0"):
                require_permission(request.user, "view_ledger", "Adding a rework cost needs ledger access.")
            extra["reference"] = b360.rework_done(batch, lot_taka_ids=data.get("lotTakaIds") or None,
                                                  quantity=data.get("quantity"), cost=cost,
                                                  remarks=data.get("remarks"), user=request.user)
        elif action_name == "yarn":
            b360.add_yarn(batch, data, user=request.user)
        elif action_name == "yarn-delete":
            b360.delete_yarn(batch, data.get("yarnId"), user=request.user)
        else:
            b360.set_fabric_type(batch, data.get("fabricType"), reason=data.get("reason"), user=request.user)
        batch.refresh_from_db()
        return _batch_body(request, batch, **extra)


class ProductionRunListView(APIView):
    """
    GET  /inventory/production-runs/?status=&q=&page=&page_size=
    POST /inventory/production-runs/  {outputItemId, plannedQty, runDate?, customerId?,
         fabricQualityId?, shadeId?, width?, gsm?, machine?, notes?}
    In-house production (yarn -> grey fabric); agency production is PMS.
    """
    permission_classes = [HasModulePermission]
    required_permissions = ["view_production"]

    def get(self, request):
        from ..models import ProductionRun
        from ..production_services import serialize_run

        qs = ProductionRun.objects.filter(client_id=request.client_id, deleted_at__isnull=True).select_related(
            "output_item", "customer", "batch"
        )
        if request.query_params.get("status"):
            qs = qs.filter(status=request.query_params["status"])
        q = (request.query_params.get("q") or "").strip()
        if q:
            qs = qs.filter(Q(run_number__icontains=q) | Q(output_item__name__icontains=q)
                           | Q(batch__batch_code__icontains=q) | Q(customer__name__icontains=q))
        try:
            page = max(1, int(request.query_params.get("page") or 1))
            page_size = min(200, max(1, int(request.query_params.get("page_size") or 25)))
        except ValueError:
            page, page_size = 1, 25
        total = qs.count()
        rows = [serialize_run(r) for r in qs[(page - 1) * page_size: page * page_size]]
        return Response(envelope(rows, count=total, page=page, page_size=page_size))

    def post(self, request):
        from apps.core.permissions import require_permission

        from ..production_services import create_run, serialize_run

        require_permission(request.user, "enter_production")
        data = request.data
        item = Item.objects.filter(client_id=request.client_id, pk=data.get("outputItemId"), deleted_at__isnull=True).first() \
            if data.get("outputItemId") else None
        if item is None:
            raise ValidationFailed("Choose the fabric to produce.", field_errors={"outputItemId": ["Required."]})
        customer = None
        if data.get("customerId"):
            customer = Party.objects.filter(client_id=request.client_id, pk=data["customerId"]).first()
            if customer is None:
                raise ValidationFailed("Customer not found.", field_errors={"customerId": ["Not found."]})
        fields = {
            "customer": customer,
            "fabric_quality": FabricQuality.objects.filter(client_id=request.client_id, pk=data.get("fabricQualityId")).first()
            if data.get("fabricQualityId") else None,
            "shade": Shade.objects.filter(client_id=request.client_id, pk=data.get("shadeId")).first()
            if data.get("shadeId") else None,
            "width": data.get("width") or None,
            "gsm": data.get("gsm") or None,
            "machine": data.get("machine") or None,
            "notes": data.get("notes") or None,
            "responsible": request.user,
        }
        if data.get("runDate"):
            from datetime import date as _d

            try:
                fields["run_date"] = _d.fromisoformat(str(data["runDate"])[:10])
            except ValueError:
                raise ValidationFailed("Run date must be YYYY-MM-DD.", field_errors={"runDate": ["Invalid date."]})
        run = create_run(client=request.user.client, output_item=item, planned_qty=data.get("plannedQty"),
                         user=request.user, open_batch=bool(data.get("openBatch")), **fields)
        return Response(serialize_run(run), status=status.HTTP_201_CREATED)


class ProductionRunDetailView(APIView):
    """
    GET  /inventory/production-runs/<id>/
    POST /inventory/production-runs/<id>/<action>/  action in
         issue    {itemId, locationId, quantity, customerSupplied?, yarnLot?}
         return   {issueId, quantity}            unused material back to its store
         output   {rolls: [{meters, weightKg?, width?}], date?}   date = production day
         expense  {costType, amount, remarks?}
         qc       {goodQty, secondQualityQty, reprocessQty, rejectedQty, holdQty, locationId, remarks?}
         complete {wastageQty?, wastageUom?}
    """
    permission_classes = [HasModulePermission]
    required_permissions = ["view_production"]
    ACTION_PERMISSION = {
        "issue": "enter_production",
        "return": "enter_production",
        "output": "enter_production",
        "expense": "enter_production",
        "qc": "approve_qc",
        "complete": "verify_production",
        "cancel": "verify_production",
    }

    def _run(self, request, pk):
        from ..models import ProductionRun

        return ProductionRun.objects.filter(client_id=request.client_id, pk=pk, deleted_at__isnull=True).first()

    def get(self, request, pk):
        from ..production_services import serialize_run

        run = self._run(request, pk)
        if run is None:
            return Response({"message": "Production run not found.", "code": "NOT_FOUND"}, status=status.HTTP_404_NOT_FOUND)
        return Response(serialize_run(run))

    def post(self, request, pk, action_name=None):
        from apps.core.permissions import require_permission

        from .. import production_services as prod
        from .. import qc as qc_service

        if action_name not in self.ACTION_PERMISSION:
            return Response({"message": "Unknown action.", "code": "NOT_FOUND"}, status=status.HTTP_404_NOT_FOUND)
        require_permission(request.user, self.ACTION_PERMISSION[action_name])
        run = self._run(request, pk)
        if run is None:
            return Response({"message": "Production run not found.", "code": "NOT_FOUND"}, status=status.HTTP_404_NOT_FOUND)
        data = request.data
        result = {}
        if action_name == "issue":
            item = Item.objects.filter(client_id=request.client_id, pk=data.get("itemId")).first() if data.get("itemId") else None
            location = Location.objects.filter(client_id=request.client_id, pk=data.get("locationId")).first() \
                if data.get("locationId") else None
            if item is None or location is None:
                raise ValidationFailed("Choose the material and the location it is issued from.",
                                       field_errors={"itemId": ["Required."], "locationId": ["Required."]})
            prod.issue_material(run, item=item, location=location, quantity=data.get("quantity"),
                                customer_supplied=bool(data.get("customerSupplied")),
                                yarn_lot=data.get("yarnLot"), user=request.user)
        elif action_name == "return":
            prod.return_material(run, issue=data.get("issueId"), quantity=data.get("quantity"), user=request.user)
        elif action_name == "output":
            prod.record_output(run, rolls=data.get("rolls"), on_date=data.get("date") or None, user=request.user)
        elif action_name == "expense":
            prod.add_expense(run, cost_type=data.get("costType"), amount=data.get("amount"),
                             remarks=data.get("remarks"), user=request.user)
        elif action_name == "qc":
            result["movements"] = prod.dispose_qc(
                run, quantities=qc_service.parse_quantities(data), good_location_id=data.get("locationId"),
                user=request.user, remarks=data.get("remarks"),
            )
        elif action_name == "cancel":
            prod.cancel_run(run, reason=data.get("reason"), user=request.user)
        else:
            prod.complete_run(run, wastage_qty=data.get("wastageQty"), wastage_uom=data.get("wastageUom"),
                              user=request.user)
        run.refresh_from_db()
        return Response({**prod.serialize_run(run), **result})


class BatchProfitabilityView(APIView):
    """
    GET /inventory/batches/profitability/?search=&fabricType=&status=&sortBy=&from=&to=
    Returns batch-wise profitability records and aggregate financial summary.
    """
    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]

    def get(self, request):
        from ..batch_profitability import get_batch_profitability_list
        search = request.query_params.get("search") or request.query_params.get("q")
        fabric_type = request.query_params.get("fabricType") or request.query_params.get("stage")
        status_filter = request.query_params.get("status")
        sort_by = request.query_params.get("sortBy") or request.query_params.get("sort")
        date_from = request.query_params.get("from") or request.query_params.get("startDate")
        date_to = request.query_params.get("to") or request.query_params.get("endDate")

        refresh = request.query_params.get("refresh") in ("1", "true", "True")
        data = get_batch_profitability_list(
            request.client_id,
            search=search,
            fabric_type=fabric_type,
            status=status_filter,
            sort_by=sort_by,
            date_from=date_from,
            date_to=date_to,
            refresh=refresh,
        )
        return Response(data)


class BatchProfitabilityDetailView(APIView):
    """
    GET /inventory/batches/<str:code>/profitability/
    Returns full audit breakdown for a single batch modal/slideout.
    """
    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]

    def get(self, request, code):
        from ..batch_profitability import get_batch_profitability_detail
        data = get_batch_profitability_detail(request.client_id, code)
        if not data:
            return Response({"message": f"No batch found for '{code}'.", "code": "NOT_FOUND"}, status=status.HTTP_404_NOT_FOUND)
        return Response(data)

