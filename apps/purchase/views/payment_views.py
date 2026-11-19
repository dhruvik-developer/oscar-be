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


class PaymentOutViewSet(TenantModelViewSet):

    queryset = PaymentOut.objects.select_related("party", "bank_account")

    serializer_class = PaymentOutSerializer

    audit_entity_type = "PaymentOut"

    audit_label_field = "payment_number"

    idempotent_create = True

    status_field = "status"

    default_date_field = "payment_date"

    search_fields = ["payment_number", "reference_number", "party__name"]

    ordering = ["-payment_date", "-created_at"]

    filter_map = {"vendorId": "party_id", "mode": "mode"}

    permission_map = {"read": ["view_purchase"], "write": ["record_payment_out"]}

    # A payment is posted on create; it is changed only by /cancel/, never

    # edited or deleted (its ledger entry and allocations would not follow).

    http_method_names = ["get", "post", "head", "options"]



    def get_aggregates(self, queryset):

        rows = queryset.aggregate(

            count=Count("id"),

            totalPaid=money_sum("amount"),

            allocated=money_sum("allocated_amount"),

        )

        rows["advances"] = round2(rows["totalPaid"] - rows["allocated"])

        return rows



    @transaction.atomic

    def perform_create(self, serializer):

        data = serializer.validated_data

        bill = None

        bill_id = self.request.data.get("billId")

        if bill_id:

            bill = PurchaseBill.objects.filter(

                pk=bill_id, client_id=self.get_client_id(), deleted_at__isnull=True

            ).first()

            if bill is None:

                raise NotFound("That bill no longer exists.")



        payment = services.record_payment_out(

            client=self.request.user.client,

            party=data["party"],

            amount=data["amount"],

            payment_date=data["payment_date"],

            mode=data["mode"],

            bank_account=data.get("bank_account"),

            reference_number=data.get("reference_number"),

            notes=data.get("notes"),

            allocations=self.request.data.get("allocations") or [],

            bill=bill,

            user=self.request.user,

        )

        serializer.instance = payment

        self._created_instance = payment

        self._concurrency_instance = payment

        self.write_audit("create", payment, description="Payment recorded")

        return payment



    @action(detail=True, methods=["post"])

    def cancel(self, request, pk=None):

        payment = services.cancel_payment_out(

            self.get_object(), reason=request.data.get("reason"), user=request.user

        )

        self.write_audit("cancel", payment, description=request.data.get("reason"))

        return Response(self.get_serializer(payment).data)



class ExpenseViewSet(TenantModelViewSet):

    queryset = Expense.objects.select_related("category", "party", "bank_account")

    serializer_class = ExpenseSerializer

    audit_entity_type = "Expense"

    audit_label_field = "expense_number"

    status_field = "status"

    default_date_field = "expense_date"

    search_fields = ["expense_number", "notes", "reference_number"]

    ordering = ["-expense_date"]

    filter_map = {"categoryId": "category_id", "vendorId": "party_id", "paymentMode": "payment_mode"}

    permission_map = {"read": ["view_purchase"], "write": ["view_purchase"]}



    def get_aggregates(self, queryset):

        return queryset.aggregate(count=Count("id"), totalValue=money_sum("total"))



    @transaction.atomic

    def perform_create(self, serializer):

        from apps.accounting import services as ledger



        data = serializer.validated_data

        data["expense_number"] = allocate_number(

            self.request.user.client, "EXP", data.get("expense_date")

        )

        data["total"] = round2(D(data.get("amount")) + D(data.get("tax_amount")))

        expense = super().perform_create(serializer)



        entry = ledger.post_expense(expense, user=self.request.user)

        if entry is not None:

            expense.journal_entry = entry

            expense.save(update_fields=["journal_entry", "updated_at"])

        return expense



    @transaction.atomic

    def perform_update(self, serializer):

        from apps.accounting import services as ledger



        expense = serializer.instance

        if expense.journal_entry_id:

            # A posted expense is corrected by reversing and re-posting, never

            # by editing the entry (db.md §8).

            ledger.reverse_entry(expense.journal_entry, user=self.request.user)



        serializer.validated_data["total"] = round2(

            D(serializer.validated_data.get("amount", expense.amount))

            + D(serializer.validated_data.get("tax_amount", expense.tax_amount))

        )

        expense = super().perform_update(serializer)



        entry = ledger.post_expense(expense, user=self.request.user)

        expense.journal_entry = entry

        expense.save(update_fields=["journal_entry", "updated_at"])

        return expense





    @transaction.atomic

    def perform_destroy(self, instance):

        """A deleted expense takes its ledger entry with it (by reversal, never

        by editing the posted entry) -- otherwise the books keep the cost."""

        from apps.accounting import services as ledger



        if instance.journal_entry_id:

            ledger.reverse_entry(instance.journal_entry, user=self.request.user)

        super().perform_destroy(instance)

