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


class PurchaseReturnViewSet(PostedReturnGuardMixin, SalesDocumentViewSet):

    queryset = PurchaseReturn.objects.select_related("purchase_bill")

    serializer_class = PurchaseReturnSerializer

    audit_entity_type = "PurchaseReturn"

    audit_label_field = "return_number"

    status_field = "status"

    print_title = "Debit Note"

    draft_only_writes = False

    filter_map = {"vendorId": "party_id", "purchaseBillId": "purchase_bill_id"}

    permission_map = {"read": ["view_purchase"], "write": ["cancel_purchase_document"]}



    @transaction.atomic

    def perform_create(self, serializer):

        from apps.accounting import services as ledger



        bill = serializer.validated_data["purchase_bill"]

        if bill.status == "Cancelled":

            raise BusinessRuleViolation(

                "Cannot return against a cancelled bill.",

                code=Codes.PAYMENT_ON_CANCELLED,

            )



        serializer.validated_data["return_number"] = allocate_number(

            self.request.user.client, "PR", serializer.validated_data.get("doc_date")

        )

        serializer.validated_data["debit_note_number"] = serializer.validated_data[

            "return_number"

        ]

        serializer.validated_data.setdefault("party", bill.party)



        purchase_return = super().perform_create(serializer)

        location_id = purchase_return.location_id or bill.location_id



        for line in purchase_return.line_items.select_related(

            "purchase_bill_line", "item"

        ).all():

            bill_line = line.purchase_bill_line

            remaining = D(bill_line.qty) - D(bill_line.returned_qty)

            if D(line.returned_qty) > remaining + Decimal("0.0001"):

                raise BusinessRuleViolation(

                    f"{line.item_name}: only {round4(remaining)} remains returnable.",

                    code=Codes.OVER_RETURN,

                    payload={"billLineId": str(bill_line.id)},

                )

            bill_line.returned_qty = round4(

                D(bill_line.returned_qty) + D(line.returned_qty)

            )

            bill_line.save(update_fields=["returned_qty", "updated_at"])



            if line.item_id and line.item.holds_stock and location_id:

                # returned_qty is counted in the bill line's unit.

                factor = D(bill_line.uom_factor or 1)

                stock_qty = uom.to_stock_qty(line.returned_qty, factor)

                stock.assert_sufficient_stock(

                    purchase_return.client_id, line.item, stock_qty,

                    location_id, line.item_name,

                )

                stock.post_movement(

                    client_id=purchase_return.client_id,

                    item=line.item,

                    location=location_id,

                    type="PURCHASE_RETURN",

                    quantity=-stock_qty,

                    unit_cost=(

                        D(bill_line.landed_unit_cost) / factor

                        if bill_line.landed_unit_cost else line.item.cost_price

                    ),

                    reference_type="PurchaseReturn",

                    reference_id=purchase_return.id,

                    reference_number=purchase_return.return_number,

                    movement_date=purchase_return.doc_date,

                    user=self.request.user,

                )



        entry = ledger.post_purchase_return(purchase_return, user=self.request.user)

        if entry is not None:

            purchase_return.journal_entry = entry

            purchase_return.save(update_fields=["journal_entry", "updated_at"])

        services.refresh_bill_payment_status(bill)



        # Handle lot/taka selection on return (Phase 2.2)

        taka_ids = self.request.data.get("taka_ids") or self.request.data.get("takaIds") or []

        if taka_ids:

            from apps.inventory.models import LotTaka

            for tid in taka_ids:

                taka = LotTaka.objects.filter(pk=tid, client_id=self.get_client_id()).first()

                if taka and taka.available_quantity > ZERO:

                    stock.post_movement(

                        client_id=purchase_return.client_id,

                        item=taka.item,

                        location=taka.location_id,

                        type="PURCHASE_RETURN",

                        quantity=-taka.available_quantity,

                        lot_taka=taka,

                        batch_number=taka.lot_number,

                        reference_type="PurchaseReturn",

                        reference_id=purchase_return.id,

                        reference_number=purchase_return.return_number,

                        movement_date=purchase_return.doc_date,

                        user=self.request.user,

                    )

                    taka.status = "Returned"

                    taka.available_quantity = ZERO

                    taka.save(update_fields=["status", "available_quantity", "updated_at"])



        return purchase_return



    @action(detail=True, methods=["post"])

    @transaction.atomic

    def cancel(self, request, pk=None):

        from apps.sales.services import _reverse_document



        purchase_return = self.get_object()

        if purchase_return.status == "Cancelled":

            raise Conflict("This return is already cancelled.", code=Codes.ALREADY_CANCELLED)



        reason = request.data.get("reason")

        _reverse_document(purchase_return, "PurchaseReturn", user=request.user, reason=reason)



        for line in purchase_return.line_items.select_related("purchase_bill_line").all():

            bill_line = line.purchase_bill_line

            bill_line.returned_qty = max(

                round4(D(bill_line.returned_qty) - D(line.returned_qty)), ZERO

            )

            bill_line.save(update_fields=["returned_qty", "updated_at"])



        self.write_audit("cancel", purchase_return, description=reason)

        return Response(self.get_serializer(purchase_return).data)

