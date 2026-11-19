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


class VendorLookupViewSet(ReadOnlyTenantViewSet):

    """``/purchase/vendors/{id}/…`` helpers (api.md §6.5)."""



    from apps.masters.models import Party



    queryset = Party.objects.filter(type__in=["Vendor", "Both"])

    required_permissions = ["view_purchase"]

    status_field = "status"



    def get_serializer_class(self):

        from apps.masters.serializers import PartySerializer



        return PartySerializer



    @action(detail=True, methods=["get"], url_path="advance-balance")

    def advance_balance(self, request, pk=None):

        self.get_object()

        return Response(services.vendor_advance_balance(request.client_id, pk))



    @action(detail=True, methods=["get"])

    def ledger(self, request, pk=None):

        from apps.accounting.services import party_ledger



        self.get_object()

        return Response(

            party_ledger(

                request.client_id,

                pk,

                date_from=request.query_params.get("date_from"),

                date_to=request.query_params.get("date_to"),

            )

        )



class SupplierDeliveryViewSet(TenantModelViewSet):

    queryset = SupplierDelivery.objects.select_related("purchase_order", "purchase_order__party").prefetch_related("purchase_order__line_items")

    serializer_class = SupplierDeliverySerializer

    required_permissions = ["view_purchase"]

    permission_map = {"read": ["view_purchase"], "write": ["create_purchase_order"]}

    status_field = "status"

    default_date_field = "dispatch_date"

    ordering = ["-dispatch_date", "-created_at"]

    filter_map = {

        "purchaseOrderId": "purchase_order_id",

        "poId": "purchase_order_id",

        "status": "status",

    }



    def get_queryset(self):

        qs = super().get_queryset()

        project_id = self.request.query_params.get("projectId") or self.request.query_params.get("project_id")

        if project_id:

            qs = qs.filter(purchase_order__project_id=project_id)

        supplier_id = self.request.query_params.get("supplierId") or self.request.query_params.get("vendorId")

        if supplier_id:

            qs = qs.filter(purchase_order__party_id=supplier_id)

        return qs



    def perform_create(self, serializer):

        from apps.core.numbering import allocate_number

        po = serializer.validated_data.get("purchase_order")
        # On a mill PO the factory ships to the mill: fabric_flow counts only these.
        if po is not None and po.fabric_flow == fabric_flow.FABRIC_FLOW_FACTORY_TO_MILL:
            serializer.validated_data["destination"] = "MILL"
            serializer.validated_data["mill"] = po.dyeing_mill
        serializer.validated_data["delivery_number"] = allocate_number(

            self.request.user.client, "SD", serializer.validated_data.get("dispatch_date")

        )

        return super().perform_create(serializer)

