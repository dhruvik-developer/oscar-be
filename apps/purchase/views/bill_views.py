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


class PurchaseBillViewSet(SalesDocumentViewSet):

    queryset = PurchaseBill.objects.all()

    serializer_class = PurchaseBillSerializer

    audit_entity_type = "PurchaseBill"

    audit_label_field = "bill_number"

    status_field = "status"

    print_title = "Purchase Bill"

    filter_map = {

        "vendorId": "party_id",

        "purchaseOrderId": "purchase_order_id",

        "poId": "purchase_order_id",

        "po": "purchase_order_id",

        "goodsReceived": "goods_received",

        "qcStatus": "qc_status",

    }

    def get_queryset(self):
        standalone_grn = GoodsReceipt.objects.filter(
            purchase_order_id=OuterRef("purchase_order_id"),
            purchase_bill__isnull=True, deleted_at__isnull=True,
        )
        return super().get_queryset().annotate(po_already_received=Exists(standalone_grn))

    def get_object(self):
        lookup_url_kwarg = self.lookup_url_kwarg or self.lookup_field
        pk = self.kwargs.get(lookup_url_kwarg)
        try:
            import uuid
            uuid.UUID(str(pk))
        except (ValueError, TypeError, AttributeError):
            obj = self.get_queryset().filter(bill_number__iexact=str(pk)).first()
            if obj:
                self.check_object_permissions(self.request, obj)
                return obj
        return super().get_object()

    permission_map = {"read": ["view_purchase"], "write": ["create_bill"]}



    def get_aggregates(self, queryset):

        rows = queryset.aggregate(

            count=Count("id"),

            totalValue=money_sum("total"),

            paid=money_sum("amount_paid"),

            awaitingReceipt=Count("id", filter=Q(goods_received=False)),

        )

        rows["outstanding"] = round2(rows["totalValue"] - rows["paid"])

        return rows



    def perform_create(self, serializer):

        serializer.validated_data["bill_number"] = allocate_number(

            self.request.user.client, "BILL", serializer.validated_data.get("doc_date")

        )

        bill = super().perform_create(serializer)

        return bill



    def check_draft_only(self, instance, verb):

        if instance.goods_received:

            raise Conflict(

                f"Goods have been received against this bill, so it cannot be {verb}ed.",

                code=Codes.DRAFT_ONLY,

                detail="Cancel the bill instead, which reverses the stock and the ledger.",

            )

        super().check_draft_only(instance, verb)



    @action(detail=True, methods=["post"], url_path="receive-goods")

    def receive_goods(self, request, pk=None):

        """The weight-variance receive (api.md §6.3) -- ``receivePurchaseBillGoods``."""

        from apps.core.permissions import require_permission



        require_permission(request.user, "receive_goods")



        serializer = ReceiveGoodsSerializer(data=request.data)

        serializer.is_valid(raise_exception=True)

        data = serializer.validated_data



        result = services.receive_bill_goods(

            self.get_object(),

            lines_payload=data["lines"],

            qc_status=data.get("qcStatus"),

            user=request.user,

            location=data.get("locationId"),

        )

        bill = result["bill"]

        self.write_audit(

            "receive",

            bill,

            description=(

                f"Goods received, QC {result['qcStatus']}"

                + (" (weight variance)" if result["varianceBreach"] else "")

            ),

        )

        return Response(

            {

                "bill": self.get_serializer(bill).data,

                "receipt": GoodsReceiptSerializer(result["receipt"]).data,

                "qcStatus": result["qcStatus"],

                "varianceBreach": result["varianceBreach"],

            }

        )



    @action(detail=True, methods=["post"])

    def qc(self, request, pk=None):

        from apps.core.permissions import require_permission



        require_permission(request.user, "approve_qc")



        serializer = QcSerializer(data=request.data)

        serializer.is_valid(raise_exception=True)

        bill = services.update_qc_status(

            self.get_object(),

            serializer.validated_data["status"],

            note=serializer.validated_data.get("note"),

            user=request.user,

        )

        self.write_audit(

            "qc", bill, description=f"QC set to {serializer.validated_data['status']}"

        )

        return Response(self.get_serializer(bill).data)



    @action(detail=True, methods=["post"])

    def cancel(self, request, pk=None):

        from apps.core.permissions import require_permission



        require_permission(request.user, "cancel_purchase_document")



        bill = services.cancel_bill(

            self.get_object(), reason=request.data.get("reason"), user=request.user

        )

        self.write_audit("cancel", bill, description=request.data.get("reason"))

        return Response(self.get_serializer(bill).data)



    @action(detail=True, methods=["get"])

    def outstanding(self, request, pk=None):

        return Response(services.bill_outstanding(self.get_object()))



    @action(detail=True, methods=["post"], url_path="apply-advance")

    @transaction.atomic

    def apply_advance(self, request, pk=None):

        """``{ amount }`` from vendor advances (api.md §6.4)."""

        from apps.sales.models import PaymentAllocation



        bill = PurchaseBill.objects.select_for_update().get(pk=self.get_object().pk)

        amount = round2(request.data.get("amount"))

        if amount <= ZERO:

            raise ValidationFailed(

                "Enter an amount to apply.",

                field_errors={"amount": ["Must be greater than zero."]},

            )

        if bill.status in ("Draft", "Cancelled") or bill.posted_at is None:

            raise BusinessRuleViolation(

                "An advance can only be applied to a posted, open bill.",

                code=Codes.PAYMENT_ON_DRAFT if bill.status != "Cancelled" else Codes.PAYMENT_ON_CANCELLED,

            )

        outstanding = services.bill_outstanding(bill)["outstanding"]

        if amount > outstanding + ROUNDING_TOLERANCE:

            raise BusinessRuleViolation(

                f"Only {outstanding} is outstanding on this bill.",

                code=Codes.PAYMENT_EXCEEDS_BALANCE,

            )



        advance = services.vendor_advance_balance(request.client_id, bill.party_id)

        if amount > D(advance["advanceBalance"]):

            raise BusinessRuleViolation(

                f"Only {advance['advanceBalance']} is available as an advance.",

                code=Codes.PAYMENT_EXCEEDS_BALANCE,

                payload=advance,

            )



        remaining = amount

        payments = PaymentOut.objects.select_for_update().filter(

            client_id=request.client_id, party_id=bill.party_id, status="Active",

            deleted_at__isnull=True,

        ).filter(allocated_amount__lt=F("amount")).order_by("payment_date")



        for payment in payments:

            if remaining <= ZERO:

                break

            available = min(payment.unallocated_amount, remaining)

            if available <= ZERO:

                continue

            PaymentAllocation.objects.create(

                client_id=request.client_id,

                payment_id=payment.id,

                payment_side="out",

                document_type="PurchaseBill",

                document_id=bill.id,

                amount=available,

                allocated_by=request.user,

            )

            payment.allocated_amount = round2(D(payment.allocated_amount) + available)

            payment.save(update_fields=["allocated_amount", "updated_at"])

            remaining -= available



        services.refresh_bill_payment_status(bill)

        bill.refresh_from_db()

        return Response(self.get_serializer(bill).data)

