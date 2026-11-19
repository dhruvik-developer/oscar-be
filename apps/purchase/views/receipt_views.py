"""Purchase views submodule."""
import logging
from decimal import Decimal

from django.db import transaction
from django.db.models import Count, DecimalField, Exists, F, OuterRef, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core import idempotency
from apps.core.exceptions import (
    BusinessRuleViolation,
    Codes,
    Conflict,
    NotFound,
    ValidationFailed,
)
from apps.core.money import ZERO, D, ROUNDING_TOLERANCE, round2, round4
from apps.core.numbering import allocate_number
from apps.core.pagination import envelope
from apps.core.printing import PdfNotAvailable, print_payload, send_payload
from apps.core.viewsets import ReadOnlyTenantViewSet, TenantModelViewSet
from apps.inventory import services as stock
from apps.masters import uom
from apps.sales.views import PostedReturnGuardMixin, SalesDocumentViewSet, _clone_document

from .. import dyeing_dispatch, fabric_flow, services
from ..models import (
    PURCHASE_TYPE_FINAL_JOB_WORK,
    PURCHASE_TYPE_VENDOR_A,
    PAYMENT_MODES,
    VENDOR_BILL_PENDING_STATUSES,
    Expense,
    GoodsReceipt,
    PaymentOut,
    PurchaseBill,
    PurchaseBillLine,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseReturn,
    PurchaseReturnLine,
    SupplierDelivery,
    VendorBill,
    VendorBillLine,
)
from ..serializers import (
    ExpenseSerializer,
    GoodsReceiptSerializer,
    PaymentOutSerializer,
    PurchaseBillSerializer,
    PurchaseOrderSerializer,
    PurchaseReturnSerializer,
    QcSerializer,
    ReceiveGoodsSerializer,
    SupplierDeliverySerializer,
    VendorBillSerializer,
)
from .base import MONEY, money_sum

logger = logging.getLogger(__name__)


class GoodsReceiptViewSet(TenantModelViewSet):

    """``GET /purchase/receipts/`` and ``POST /purchase/receipts/`` (api.md §6.3, Phase 2.2).



    Supports standalone GRN creation against POs or suppliers, worklist filtering,

    reconciliation report, and supplier ledger.

    """



    queryset = GoodsReceipt.objects.select_related("party", "purchase_bill", "purchase_order", "location").prefetch_related("lines", "lines__item")

    serializer_class = GoodsReceiptSerializer

    required_permissions = ["view_purchase"]

    permission_map = {"read": ["view_purchase"], "write": ["receive_goods"]}

    status_field = "qc_status"

    default_date_field = "receipt_date"

    ordering = ["-receipt_date", "-created_at"]

    filter_map = {

        "partyId": "party_id",

        "vendorId": "party_id",

        "purchaseOrderId": "purchase_order_id",

        "poId": "purchase_order_id",

        "po": "purchase_order_id",

        "qcStatus": "qc_status",

    }

    def get_object(self):
        lookup_url_kwarg = self.lookup_url_kwarg or self.lookup_field
        pk = self.kwargs.get(lookup_url_kwarg)
        try:
            import uuid
            uuid.UUID(str(pk))
        except (ValueError, TypeError, AttributeError):
            obj = self.get_queryset().filter(grn_number__iexact=str(pk)).first()
            if obj:
                self.check_object_permissions(self.request, obj)
                return obj
        return super().get_object()

    def list(self, request, *args, **kwargs):

        if request.query_params.get("worklist") == "true":

            pending_bills = PurchaseBill.objects.filter(

                client_id=request.client_id, deleted_at__isnull=True, goods_received=False

            ).exclude(status="Cancelled").select_related("party")



            held_receipts = self.filter_queryset(self.get_queryset()).exclude(

                qc_status="Approved"

            )



            rows = [

                {

                    "kind": "bill",

                    "id": str(bill.id),

                    "billId": str(bill.id),

                    "billNumber": bill.bill_number,

                    "vendorName": bill.party_name or bill.party.name,

                    "date": bill.doc_date,

                    "total": round2(bill.total),

                    "qcStatus": bill.qc_status,

                    "goodsReceived": False,

                }

                for bill in pending_bills

            ] + [

                {

                    "kind": "receipt",

                    "id": str(receipt.id),

                    "billId": str(receipt.purchase_bill_id) if receipt.purchase_bill_id else None,

                    "billNumber": (

                        receipt.purchase_bill.bill_number if receipt.purchase_bill_id else None

                    ),

                    "grnNumber": receipt.grn_number,

                    "vendorName": receipt.party.name,

                    "date": receipt.receipt_date,

                    "qcStatus": receipt.qc_status,

                    "goodsReceived": True,

                }

                for receipt in held_receipts

            ]



            return Response(

                envelope(

                    rows,

                    aggregates={

                        "awaitingReceipt": pending_bills.count(),

                        "heldAtQc": held_receipts.count(),

                    },

                )

            )

        return super().list(request, *args, **kwargs)



    def create(self, request, *args, **kwargs):

        from apps.core.permissions import require_permission



        require_permission(request.user, "receive_goods")



        data = request.data

        party_id = data.get("party") or data.get("partyId") or data.get("vendorId")

        location_id = data.get("location") or data.get("locationId")

        receipt_date = data.get("receipt_date") or data.get("receiptDate") or data.get("date")

        supplier_challan_no = data.get("supplier_challan_no") or data.get("supplierChallanNo")

        supplier_challan_date = data.get("supplier_challan_date") or data.get("supplierChallanDate")

        purchase_order_id = data.get("purchase_order") or data.get("purchaseOrderId") or data.get("poId")

        lines = data.get("lines") or []



        # A double-clicked Save replays the first response instead of posting

        # the stock twice (api.md §1.8).

        try:

            record = idempotency.begin(request, request.client_id, request.path, request.data)

        except idempotency.Replay as replay:

            response = Response(replay.body, status=replay.status)

            response["Idempotency-Replayed"] = "true"

            return response

        try:

            with transaction.atomic():

                receipt = services.create_standalone_grn(

                    request.user.client,

                    party_id=party_id,

                    location_id=location_id,

                    receipt_date=receipt_date,

                    supplier_challan_no=supplier_challan_no,

                    supplier_challan_date=supplier_challan_date,

                    purchase_order_id=purchase_order_id,

                    lines_payload=lines,

                    user=request.user,

                    fabric_type=data.get("fabricType"),
                )

                # A factory fabric order received here books the batch's cost
                # (received qty x rate + its share of the PO's costs) exactly as
                # a receipt from the PO screen does.
                if receipt.purchase_order_id and receipt.purchase_order.fabric_flow:
                    fabric_flow.allocate_costs(receipt.purchase_order, user=request.user)

                try:
                    from apps.pms import loom_services
                    loom_services.sync_yarn_receipt_to_loom_stock(receipt)
                except Exception as exc:
                    logger.exception(
                        "Failed to sync yarn receipt %s to loom stock during standalone GRN creation: %s",
                        receipt.pk,
                        exc,
                    )

                self.write_audit("create", receipt, description=f"Standalone GRN created: {receipt.grn_number}")

        except Exception as exc:
            logger.exception("Failed to create standalone GRN: %s", exc)
            idempotency.fail(record)
            raise

        serializer = self.get_serializer(receipt)

        return idempotency.finish(

            record, Response(serializer.data, status=status.HTTP_201_CREATED), receipt

        )



    @action(detail=False, methods=["get"], url_path="reconciliation")

    def reconciliation(self, request):

        vendor_id = request.query_params.get("vendorId") or request.query_params.get("vendor_id")

        date_from = request.query_params.get("dateFrom") or request.query_params.get("date_from")

        date_to = request.query_params.get("dateTo") or request.query_params.get("date_to")

        data = services.supplier_reconciliation(

            request.client_id,

            vendor_id=vendor_id,

            date_from=date_from,

            date_to=date_to,

        )

        return Response(data)



    @action(detail=False, methods=["get"], url_path="supplier-ledger")

    def supplier_ledger(self, request):

        vendor_id = request.query_params.get("vendorId") or request.query_params.get("vendor_id")

        if not vendor_id:

            raise ValidationFailed("vendorId query parameter is required.", field_errors={"vendorId": ["Required."]})

        date_from = request.query_params.get("dateFrom") or request.query_params.get("date_from")

        date_to = request.query_params.get("dateTo") or request.query_params.get("date_to")

        data = services.supplier_ledger(

            request.client_id,

            vendor_id=vendor_id,

            date_from=date_from,

            date_to=date_to,

        )

        return Response(data)



    @action(detail=True, methods=["post"])

    def qc(self, request, pk=None):

        """``POST /purchase/receipts/{id}/qc/`` -- ``{ status, note }``."""

        from apps.core.permissions import require_permission



        require_permission(request.user, "approve_qc")



        receipt = self.get_object()

        serializer = QcSerializer(data=request.data)

        serializer.is_valid(raise_exception=True)



        # The verdict moves the goods out of QC Hold with real movements and

        # updates receipt, bill and taka statuses together (Phase 1.4).

        from apps.inventory import qc as qc_service



        qc_service.dispose_all(

            receipt,

            serializer.validated_data["status"],

            user=request.user,

            note=serializer.validated_data.get("note"),

        )

        receipt.refresh_from_db()



        return Response(self.get_serializer(receipt).data)

