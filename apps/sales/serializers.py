"""Sales serializers (api.md §5)."""
from django.utils import timezone
from rest_framework import serializers

from apps.core.document_serializers import (
    HEADER_FIELDS,
    READ_ONLY_HEADER_FIELDS,
    DocumentLineSerializer,
    DocumentSerializer,
)
from apps.core.serializers import (
    BaseModelSerializer,
    BaseSerializer,
    ISODateTimeField,
    MoneyField,
    QuantityField,
    TenantPrimaryKeyRelatedField,
)
from apps.core.money import ZERO, D, round2

from .models import (
    BrokerCommission,
    DeliveryChallan,
    EarlyPaymentDiscountRequest,
    DeliveryChallanLine,
    PaymentAllocation,
    PaymentIn,
    ProformaInvoice,
    ProformaInvoiceLine,
    Quotation,
    QuotationActivity,
    QuotationLine,
    SalesInvoice,
    SalesInvoiceLine,
    SalesOrder,
    SalesOrderLine,
    SalesReturn,
    SalesReturnLine,
    WarrantyCard,
    WarrantyCardItem,
)
from . import services


def line_serializer_for(line_model, table_name, extra_fields=(), extra_read_only=()):
    """Build the line serializer for one document type.

    The parameter is not named ``model``: a class body does not close over the
    enclosing function's locals, so ``model = model`` inside ``class Meta``
    would resolve to the name being defined, not the argument.
    """

    class _LineSerializer(DocumentLineSerializer):
        line_table_name = table_name

        class Meta(DocumentLineSerializer.Meta):
            model = line_model
            fields = DocumentLineSerializer.Meta.fields + list(extra_fields)
            read_only_fields = DocumentLineSerializer.Meta.read_only_fields + list(
                extra_read_only
            )

    _LineSerializer.__name__ = f"{line_model.__name__}Serializer"
    return _LineSerializer


QuotationLineSerializer = line_serializer_for(QuotationLine, "quotation_lines")
SalesOrderLineSerializer = line_serializer_for(
    SalesOrderLine,
    "sales_order_lines",
    extra_fields=["dispatched_qty", "invoiced_qty"],
    extra_read_only=["dispatched_qty", "invoiced_qty"],
)
ProformaInvoiceLineSerializer = line_serializer_for(
    ProformaInvoiceLine, "proforma_invoice_lines", extra_fields=["sales_order_line"],
)
DeliveryChallanLineSerializer = line_serializer_for(
    DeliveryChallanLine, "delivery_challan_lines", extra_fields=["taka_allocations"],
)
SalesInvoiceLineSerializer = line_serializer_for(
    SalesInvoiceLine, "sales_invoice_lines",
    extra_fields=["returned_qty", "taka_allocations"], extra_read_only=["returned_qty"],
)


# ---------------------------------------------------------------------------
# Quotations (api.md §5.3)
# ---------------------------------------------------------------------------
class QuotationSerializer(DocumentSerializer):
    line_model = QuotationLine
    line_serializer = QuotationLineSerializer
    line_fk_name = "quotation"
    line_table_name = "quotation_lines"

    #: ``DocumentSerializer`` makes the party mandatory for every document;
    #: the quotation is the exception -- it can be raised against a lead that
    #: has no Customer row yet (see ``Quotation.party``).
    partyId = TenantPrimaryKeyRelatedField(
        source="party", model="masters.Party", required=False, allow_null=True
    )
    crmDeal = TenantPrimaryKeyRelatedField(
        source="crm_deal", model="crm.Deal", required=False, allow_null=True
    )
    crmLead = TenantPrimaryKeyRelatedField(
        source="crm_lead", model="crm.Lead", required=False, allow_null=True
    )

    class Meta:
        model = Quotation
        fields = HEADER_FIELDS + [
            "quotation_number", "status", "valid_until", "subject",
            "crm_deal", "crm_lead", "crmDeal", "crmLead",
        ]
        read_only_fields = READ_ONLY_HEADER_FIELDS + ["quotation_number"]


class QuotationActivitySerializer(BaseModelSerializer):
    class Meta:
        model = QuotationActivity
        fields = ["id", "event", "actor_label", "comment", "ip", "created_at"]


class ShareRequestSerializer(BaseSerializer):
    expiryDays = serializers.IntegerField(required=False, default=14, min_value=1, max_value=365)
    recipients = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    channel = serializers.ChoiceField(
        choices=["email", "whatsapp", "link"], required=False, default="link"
    )


# ---------------------------------------------------------------------------
# Sales orders (api.md §5.4)
# ---------------------------------------------------------------------------
class SalesOrderSerializer(DocumentSerializer):
    line_model = SalesOrderLine
    line_serializer = SalesOrderLineSerializer
    line_fk_name = "sales_order"

    brokerCode = serializers.CharField(
        source="broker_fk.code", read_only=True, allow_null=True
    )
    line_table_name = "sales_order_lines"

    vehicleNo = serializers.CharField(source="vehicle_number", required=False, allow_blank=True, allow_null=True)
    dispatchDate = serializers.DateField(source="dispatch_date", required=False, allow_null=True)
    invoiceStatus = serializers.SerializerMethodField()
    hasInvoice = serializers.SerializerMethodField()
    linkedInvoiceId = serializers.SerializerMethodField()
    linkedInvoiceNumber = serializers.SerializerMethodField()
    linkedInvoice = serializers.SerializerMethodField()

    def get_invoiceStatus(self, obj):
        if obj.stage == "Invoiced":
            return "Invoiced"
        active_inv = obj.invoices.filter(deleted_at__isnull=True).exclude(status="Cancelled").first()
        if active_inv:
            return "Invoiced"
        return None

    def get_hasInvoice(self, obj):
        if obj.stage == "Invoiced":
            return True
        return obj.invoices.filter(deleted_at__isnull=True).exclude(status="Cancelled").exists()

    def get_linkedInvoiceId(self, obj):
        inv = obj.invoices.filter(deleted_at__isnull=True).exclude(status="Cancelled").order_by("-created_at").first()
        return str(inv.id) if inv else None

    def get_linkedInvoiceNumber(self, obj):
        inv = obj.invoices.filter(deleted_at__isnull=True).exclude(status="Cancelled").order_by("-created_at").first()
        return inv.invoice_number if inv else None

    def get_linkedInvoice(self, obj):
        inv = obj.invoices.filter(deleted_at__isnull=True).exclude(status="Cancelled").order_by("-created_at").first()
        if not inv:
            return None
        return {
            "id": str(inv.id),
            "invoiceNumber": inv.invoice_number,
            "status": inv.status,
            "total": str(inv.total) if inv.total is not None else "0",
        }

    hasChallan = serializers.SerializerMethodField()
    linkedChallanId = serializers.SerializerMethodField()
    linkedChallanNumber = serializers.SerializerMethodField()
    linkedChallan = serializers.SerializerMethodField()

    def get_hasChallan(self, obj):
        return obj.challans.filter(deleted_at__isnull=True).exclude(status="Cancelled").exists()

    def get_linkedChallanId(self, obj):
        dc = obj.challans.filter(deleted_at__isnull=True).exclude(status="Cancelled").order_by("-created_at").first()
        return str(dc.id) if dc else None

    def get_linkedChallanNumber(self, obj):
        dc = obj.challans.filter(deleted_at__isnull=True).exclude(status="Cancelled").order_by("-created_at").first()
        return dc.challan_number if dc else None

    def get_linkedChallan(self, obj):
        dc = obj.challans.filter(deleted_at__isnull=True).exclude(status="Cancelled").order_by("-created_at").first()
        if not dc:
            return None
        return {
            "id": str(dc.id),
            "challanNumber": dc.challan_number,
            "status": dc.status,
        }


    class Meta:
        model = SalesOrder
        fields = HEADER_FIELDS + [
            "order_number", "stage", "payment_status", "delivery_date", "brokerCode",
            "quotation", "pms_project", "reference_number",
            "dispatch_date", "dispatchDate", "vehicle_number", "vehicleNo", "transporter",
            "invoiceStatus", "hasInvoice", "linkedInvoiceId", "linkedInvoiceNumber", "linkedInvoice",
            "hasChallan", "linkedChallanId", "linkedChallanNumber", "linkedChallan",
        ]
        read_only_fields = READ_ONLY_HEADER_FIELDS + [
            "order_number", "payment_status", "brokerCode",
            "invoiceStatus", "hasInvoice", "linkedInvoiceId", "linkedInvoiceNumber", "linkedInvoice",
            "hasChallan", "linkedChallanId", "linkedChallanNumber", "linkedChallan",
        ]


class ConvertLinesSerializer(BaseSerializer):
    lines = serializers.ListField(child=serializers.DictField(), required=False, default=list)
    date = serializers.DateField(required=False)
    locationId = serializers.CharField(required=False, allow_null=True)
    notes = serializers.CharField(required=False, allow_blank=True)
    directInvoice = serializers.BooleanField(required=False, default=False)
    direct = serializers.BooleanField(required=False, default=False)


# ---------------------------------------------------------------------------
# Proforma invoices (api.md §5.5)
# ---------------------------------------------------------------------------
class ProformaInvoiceSerializer(DocumentSerializer):
    line_model = ProformaInvoiceLine
    line_serializer = ProformaInvoiceLineSerializer
    line_fk_name = "proforma_invoice"
    line_table_name = "proforma_invoice_lines"

    class Meta:
        model = ProformaInvoice
        fields = HEADER_FIELDS + [
            "proforma_number", "status", "valid_until", "sales_order",
        ]
        read_only_fields = READ_ONLY_HEADER_FIELDS + ["proforma_number"]


# ---------------------------------------------------------------------------
# Delivery challans (api.md §5.6)
# ---------------------------------------------------------------------------
class DeliveryChallanSerializer(DocumentSerializer):
    line_model = DeliveryChallanLine
    line_serializer = DeliveryChallanLineSerializer
    line_fk_name = "delivery_challan"
    line_table_name = "delivery_challan_lines"

    vehicleNo = serializers.CharField(source="vehicle_number", required=False, allow_blank=True, allow_null=True)
    dispatchDate = serializers.DateField(source="dispatch_date", required=False, allow_null=True)
    ewayBillNumber = serializers.CharField(source="eway_bill_number", required=False, allow_blank=True, allow_null=True)
    ewayBillDate = serializers.DateTimeField(source="eway_bill_date", required=False, allow_null=True)
    ewayBillValidUntil = serializers.DateTimeField(source="eway_bill_valid_until", required=False, allow_null=True)
    salesOrderId = serializers.CharField(source="sales_order_id", read_only=True)
    salesOrderNumber = serializers.CharField(source="sales_order.order_number", read_only=True)

    class Meta:
        model = DeliveryChallan
        fields = HEADER_FIELDS + [
            "challan_number", "status", "sales_order", "salesOrderId", "salesOrderNumber", "quotation",
            "dispatch_date", "dispatchDate", "vehicle_number", "vehicleNo", "transporter", "lr_number",
            "delivery_location", "location", "delivered_at",
            "eway_bill_number", "ewayBillNumber", "eway_bill_date", "ewayBillDate",
            "eway_bill_valid_until", "ewayBillValidUntil",
        ]
        read_only_fields = READ_ONLY_HEADER_FIELDS + ["challan_number", "delivered_at"]


# ---------------------------------------------------------------------------
# Sales invoices (api.md §5.7)
# ---------------------------------------------------------------------------
class SalesInvoiceSerializer(DocumentSerializer):
    line_model = SalesInvoiceLine
    line_serializer = SalesInvoiceLineSerializer
    line_fk_name = "sales_invoice"
    line_table_name = "sales_invoice_lines"

    vehicleNo = serializers.CharField(source="vehicle_number", required=False, allow_blank=True, allow_null=True)
    dispatchDate = serializers.DateField(source="dispatch_date", required=False, allow_null=True)
    ackNo = serializers.CharField(source="ack_no", required=False, allow_blank=True, allow_null=True)
    ackDate = serializers.DateTimeField(source="ack_date", required=False, allow_null=True)
    signedQrcode = serializers.CharField(source="signed_qrcode", required=False, allow_blank=True, allow_null=True)
    einvoiceStatus = serializers.CharField(source="einvoice_status", required=False, allow_blank=True, allow_null=True)
    ewayBillNumber = serializers.CharField(source="eway_bill_number", required=False, allow_blank=True, allow_null=True)
    ewayBillDate = serializers.DateTimeField(source="eway_bill_date", required=False, allow_null=True)
    ewayBillValidUntil = serializers.DateTimeField(source="eway_bill_valid_until", required=False, allow_null=True)
    salesOrderId = serializers.CharField(source="sales_order_id", read_only=True)
    salesOrderNumber = serializers.CharField(source="sales_order.order_number", read_only=True)
    challanId = serializers.CharField(source="delivery_challan_id", read_only=True)
    challanNumber = serializers.CharField(source="delivery_challan.challan_number", read_only=True)

    #: ``Overdue`` is layered on at read time, never stored (db.md §12).
    displayStatus = serializers.SerializerMethodField()

    class Meta:
        model = SalesInvoice
        fields = HEADER_FIELDS + [
            "invoice_number", "status", "displayStatus", "due_date",
            "sales_order", "salesOrderId", "salesOrderNumber", "delivery_challan", "challanId", "challanNumber", "proforma_invoice", "location",
            "irn", "eway_bill_number", "ewayBillNumber",
            "ack_no", "ackNo", "ack_date", "ackDate", "signed_qrcode", "signedQrcode",
            "einvoice_status", "einvoiceStatus",
            "eway_bill_date", "ewayBillDate", "eway_bill_valid_until", "ewayBillValidUntil",
            "dispatch_date", "dispatchDate", "vehicle_number", "vehicleNo", "transporter",
        ]
        read_only_fields = READ_ONLY_HEADER_FIELDS + ["invoice_number", "status"]

    def get_displayStatus(self, invoice):
        return services.display_status(invoice)


class InvoiceOutstandingSerializer(BaseSerializer):
    total = MoneyField()
    paid = MoneyField()
    outstanding = MoneyField()
    dueDate = serializers.DateField(allow_null=True)
    daysOverdue = serializers.IntegerField()
    ageingBucket = serializers.CharField()


class ReceivableRowSerializer(BaseModelSerializer):
    customerId = serializers.CharField(source="party_id", read_only=True)
    customerName = serializers.CharField(source="party.name", read_only=True)
    invoiceNumber = serializers.CharField(source="invoice_number", read_only=True)
    invoiceDate = serializers.DateField(source="doc_date", read_only=True)
    dueDate = serializers.DateField(read_only=True, allow_null=True)
    paymentTerms = serializers.CharField(read_only=True, allow_null=True)
    creditDays = serializers.IntegerField(read_only=True, allow_null=True)
    invoiceAmount = MoneyField(source="total", read_only=True)
    paidAmount = MoneyField(source="amount_paid", read_only=True)
    approvedDiscount = MoneyField(source="settlement_discount", read_only=True)
    outstandingAmount = serializers.SerializerMethodField()
    customerCredit = serializers.SerializerMethodField()
    paymentStatus = serializers.SerializerMethodField()
    daysOverdue = serializers.SerializerMethodField()
    openDiscountRequests = serializers.SerializerMethodField()
    hasEarlyPayment = serializers.SerializerMethodField()

    class Meta:
        model = SalesInvoice
        fields = [
            "id", "customerId", "customerName", "invoiceNumber", "invoiceDate",
            "dueDate", "paymentTerms", "creditDays", "invoiceAmount", "paidAmount",
            "approvedDiscount", "outstandingAmount", "customerCredit", "paymentStatus",
            "daysOverdue", "openDiscountRequests", "hasEarlyPayment",
        ]

    def get_outstandingAmount(self, invoice):
        return services.invoice_balance(invoice)

    def get_customerCredit(self, invoice):
        return max(
            round2(
                D(invoice.amount_paid)
                + D(invoice.settlement_discount)
                - D(invoice.total)
            ),
            ZERO,
        )

    def get_paymentStatus(self, invoice):
        outstanding = services.invoice_balance(invoice)
        if outstanding <= ZERO:
            return "Paid"
        if invoice.due_date and invoice.due_date < timezone.localdate():
            return "Overdue"
        if invoice.due_date:
            return "Due"
        return invoice.status

    def get_daysOverdue(self, invoice):
        if invoice.due_date and services.invoice_balance(invoice) > ZERO:
            return max(0, (timezone.localdate() - invoice.due_date).days)
        return 0

    def get_openDiscountRequests(self, invoice):
        return invoice.discount_requests.filter(
            status__in=["Draft", "Submitted", "Under Review"],
            deleted_at__isnull=True,
        ).count()

    def get_hasEarlyPayment(self, invoice):
        if invoice.due_date is None:
            return False
        allocations = PaymentAllocation.objects.filter(
            client_id=invoice.client_id,
            payment_side="in",
            document_type="SalesInvoice",
            document_id=invoice.id,
            deleted_at__isnull=True,
        ).values("payment_id")
        return PaymentIn.objects.filter(
            client_id=invoice.client_id,
            pk__in=allocations,
            status="Active",
            payment_date__lt=invoice.due_date,
            deleted_at__isnull=True,
        ).exists()


class EarlyPaymentDiscountRequestSerializer(BaseModelSerializer):
    requestNumber = serializers.CharField(source="request_number", read_only=True)
    customerName = serializers.CharField(source="party.name", read_only=True)
    invoiceNumber = serializers.CharField(source="invoice_number", read_only=True)
    invoiceDate = serializers.DateField(source="invoice_date", read_only=True)
    invoiceTotal = MoneyField(source="invoice_total", read_only=True)
    paymentTerms = serializers.CharField(source="payment_terms", read_only=True, allow_null=True)
    dueDate = serializers.DateField(source="due_date", read_only=True, allow_null=True)
    paymentDate = serializers.DateField(source="payment_date", read_only=True)
    paymentNumber = serializers.CharField(source="payment.payment_number", read_only=True)
    eligibleAmount = MoneyField(source="eligible_amount", read_only=True)
    daysPaidEarly = serializers.IntegerField(source="days_paid_early", read_only=True)
    requestedAmount = MoneyField(source="requested_amount", read_only=True)
    proposedAmount = MoneyField(source="proposed_amount", read_only=True, allow_null=True)
    approvedAmount = MoneyField(source="approved_amount", read_only=True, allow_null=True)
    customerRequestDate = serializers.DateField(source="customer_request_date", read_only=True, allow_null=True)
    requestedById = serializers.CharField(source="requested_by_id", read_only=True, allow_null=True)
    requestedByName = serializers.CharField(source="requested_by.name", read_only=True, allow_null=True)
    decidedByName = serializers.CharField(source="decided_by.name", read_only=True, allow_null=True)
    decidedAt = ISODateTimeField(source="decided_at", read_only=True, allow_null=True)
    decisionReason = serializers.CharField(source="decision_reason", read_only=True, allow_null=True)
    cancellationReason = serializers.CharField(source="cancellation_reason", read_only=True, allow_null=True)
    creditNoteNumber = serializers.CharField(source="credit_note.note_number", read_only=True, allow_null=True)
    invoiceOutstanding = serializers.SerializerMethodField()

    class Meta:
        model = EarlyPaymentDiscountRequest
        fields = [
            "id", "requestNumber", "customerName", "invoice", "invoiceNumber",
            "invoiceDate", "invoiceTotal", "paymentTerms", "dueDate", "payment",
            "paymentNumber", "paymentDate", "eligibleAmount", "daysPaidEarly",
            "requestedAmount", "proposedAmount", "approvedAmount", "customerRequestDate",
            "reason", "notes", "attachments", "status", "requestedById",
            "requestedByName", "decidedByName", "decidedAt", "decisionReason",
            "cancellationReason", "creditNoteNumber", "invoiceOutstanding",
            "created_at", "updated_at",
        ]
        read_only_fields = fields

    def get_invoiceOutstanding(self, request):
        return services.invoice_balance(request.invoice)


class EarlyPaymentDiscountCreateSerializer(BaseSerializer):
    invoiceId = serializers.UUIDField(source="invoice_id")
    paymentId = serializers.UUIDField(source="payment_id")
    requestedAmount = MoneyField(source="requested_amount")
    customerRequestDate = serializers.DateField(source="customer_request_date", required=False, allow_null=True)
    reason = serializers.CharField()
    notes = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    attachments = serializers.ListField(child=serializers.CharField(), required=False)
    submit = serializers.BooleanField(required=False, default=False)


class EarlyPaymentDiscountUpdateSerializer(BaseSerializer):
    requestedAmount = MoneyField(source="requested_amount", required=False)
    customerRequestDate = serializers.DateField(source="customer_request_date", required=False, allow_null=True)
    reason = serializers.CharField(required=False)
    notes = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    attachments = serializers.ListField(child=serializers.CharField(), required=False)


class PaymentTermsChangeSerializer(BaseSerializer):
    paymentTerms = serializers.CharField(source="payment_terms", required=False, allow_blank=True)
    creditDays = serializers.IntegerField(source="credit_days", min_value=0)
    dueDate = serializers.DateField(source="due_date", required=False, allow_null=True)
    reason = serializers.CharField(required=False, allow_blank=True)


class DiscountReviewSerializer(BaseSerializer):
    comments = serializers.CharField(required=False, allow_blank=True)


class DiscountProposeSerializer(BaseSerializer):
    proposedAmount = MoneyField(source="amount")
    reason = serializers.CharField(required=False, allow_blank=True)


class DiscountDecisionSerializer(BaseSerializer):
    approvedAmount = MoneyField(source="amount", required=False)
    reason = serializers.CharField(required=False, allow_blank=True)


# ---------------------------------------------------------------------------
# Payments in (api.md §5.8)
# ---------------------------------------------------------------------------
class PaymentAllocationSerializer(BaseModelSerializer):
    documentId = serializers.CharField(source="document_id", read_only=True)

    class Meta:
        model = PaymentAllocation
        fields = ["id", "document_type", "documentId", "amount", "allocated_at"]


class PaymentInSerializer(BaseModelSerializer):
    customerId = TenantPrimaryKeyRelatedField(source="party", model="masters.Party")
    customerName = serializers.CharField(source="party.name", read_only=True)
    bankAccountId = TenantPrimaryKeyRelatedField(
        source="bank_account", model="accounting.BankAccount", required=False, allow_null=True
    )
    date = serializers.DateField(source="payment_date")
    allocations = serializers.SerializerMethodField()
    unallocatedAmount = serializers.SerializerMethodField()
    referenceNumber = serializers.CharField(source="reference_number", required=False, allow_null=True, allow_blank=True)
    #: Write-only: the target invoice(s) this payment settles.
    invoiceId = serializers.CharField(required=False, allow_null=True, write_only=True)
    allocationsInput = serializers.ListField(
        child=serializers.DictField(), required=False, write_only=True
    )

    class Meta:
        model = PaymentIn
        fields = [
            "id", "payment_number", "customerId", "customerName", "date", "amount",
            "mode", "bankAccountId", "reference_number", "referenceNumber", "notes",
            "allocated_amount", "unallocatedAmount", "allocations", "status",
            "invoiceId", "allocationsInput", "created_at", "updated_at",
        ]
        read_only_fields = [
            "payment_number", "allocated_amount", "status", "created_at", "updated_at",
        ]


    def to_internal_value(self, data):
        mutable_data = data.copy() if hasattr(data, "copy") else dict(data)
        if not mutable_data.get("customerId") and mutable_data.get("invoiceId"):
            from .models import SalesInvoice
            inv = SalesInvoice.objects.filter(pk=mutable_data.get("invoiceId"), deleted_at__isnull=True).first()
            if inv:
                mutable_data["customerId"] = str(inv.party_id)
        if "referenceNumber" in mutable_data and "reference_number" not in mutable_data:
            mutable_data["reference_number"] = mutable_data["referenceNumber"]
        return super().to_internal_value(mutable_data)

    def get_allocations(self, payment):
        rows = PaymentAllocation.objects.filter(
            client_id=payment.client_id,
            payment_side="in",
            payment_id=payment.id,
            deleted_at__isnull=True,
        )
        return PaymentAllocationSerializer(rows, many=True).data

    def get_unallocatedAmount(self, payment):
        return payment.unallocated_amount


class AllocateSerializer(BaseSerializer):
    allocations = serializers.ListField(child=serializers.DictField(), allow_empty=False)


# ---------------------------------------------------------------------------
# Sales returns (api.md §5.9)
# ---------------------------------------------------------------------------
class SalesReturnLineSerializer(DocumentLineSerializer):
    salesInvoiceLineId = serializers.PrimaryKeyRelatedField(
        source="sales_invoice_line", queryset=SalesInvoiceLine.objects.all()
    )
    returnedQty = QuantityField(source="returned_qty")
    condition = serializers.ChoiceField(
        choices=["Good", "Hold", "Reprocess", "Second Quality", "Reject"],
        required=False,
    )
    lotTakaId = serializers.UUIDField(source="lot_taka_id", required=False, allow_null=True)

    class Meta(DocumentLineSerializer.Meta):
        model = SalesReturnLine
        fields = DocumentLineSerializer.Meta.fields + [
            "salesInvoiceLineId", "returnedQty", "condition", "lotTakaId",
        ]


class SalesReturnSerializer(DocumentSerializer):
    line_model = SalesReturnLine
    line_serializer = SalesReturnLineSerializer
    line_fk_name = "sales_return"
    line_table_name = "sales_return_lines"

    salesInvoiceId = TenantPrimaryKeyRelatedField(
        source="sales_invoice", queryset=SalesInvoice.objects.all()
    )

    class Meta:
        model = SalesReturn
        fields = HEADER_FIELDS + [
            "return_number", "credit_note_number", "status", "salesInvoiceId",
            "reason", "location",
        ]
        read_only_fields = READ_ONLY_HEADER_FIELDS + [
            "return_number", "credit_note_number",
        ]


# ---------------------------------------------------------------------------
# Warranty cards (api.md §5.10)
# ---------------------------------------------------------------------------
class WarrantyCardItemSerializer(BaseModelSerializer):
    itemId = TenantPrimaryKeyRelatedField(source="item", model="masters.Item")
    serials = serializers.ListField(
        child=serializers.CharField(), required=False, default=list
    )

    class Meta:
        model = WarrantyCardItem
        fields = ["id", "itemId", "sku", "item_name", "qty", "serials"]



class WarrantyCardSerializer(BaseModelSerializer):
    """Two independent status fields -- do not collapse them (api.md §5.10)."""

    customerId = TenantPrimaryKeyRelatedField(source="party", model="masters.Party")
    customerName = serializers.CharField(source="party.name", read_only=True)
    challanNumber = serializers.CharField(
        source="delivery_challan.challan_number", read_only=True
    )
    invoiceNumber = serializers.CharField(
        source="sales_invoice.invoice_number", read_only=True
    )
    #: DERIVED on every read (api.md §5.10) -- never stored.
    coverageStatus = serializers.SerializerMethodField()
    items = WarrantyCardItemSerializer(many=True, required=False)

    class Meta:
        model = WarrantyCard
        fields = [
            "id", "card_number", "customerId", "customerName", "contact_person",
            "billing_address", "shipping_address", "gstin",
            "delivery_challan", "challanNumber", "sales_invoice", "invoiceNumber",
            "sales_order", "delivery_date", "delivery_location",
            "warranty_period", "warranty_unit", "warranty_start_event",
            "start_date", "expiry_date", "expiring_soon_days",
            "document_status", "coverageStatus",
            "suspended_reason", "cancelled_reason", "void_reason",
            "terms", "notes", "items", "created_at", "updated_at",
        ]
        read_only_fields = ["card_number", "created_at", "updated_at"]


    def get_coverageStatus(self, card):
        return services.coverage_status(card)

    def create(self, validated_data):
        items = validated_data.pop("items", [])
        card = super().create(validated_data)
        self._write_items(card, items)
        return card

    def update(self, instance, validated_data):
        items = validated_data.pop("items", None)
        card = super().update(instance, validated_data)
        if items is not None:
            card.items.all().delete()
            self._write_items(card, items)
        return card

    def _write_items(self, card, rows):
        from apps.inventory.services import resolve_serials
        from .models import WarrantyCardSerial

        for row in rows:
            serial_numbers = row.pop("serials", [])
            item = row.get("item")
            line = WarrantyCardItem.objects.create(
                client_id=card.client_id,
                warranty_card=card,
                sku=item.sku if item else row.get("sku"),
                item_name=item.name if item else row.get("item_name"),
                **row,
            )
            if serial_numbers and item is not None:
                resolved = resolve_serials(
                    card.client_id, item.id, serial_numbers, expected_status=None
                )
                WarrantyCardSerial.objects.bulk_create(
                    [
                        WarrantyCardSerial(
                            client_id=card.client_id, warranty_card=card, serial=serial
                        )
                        for serial in resolved
                    ],
                    ignore_conflicts=True,
                )


class ReasonSerializer(BaseSerializer):
    reason = serializers.CharField(required=False, allow_blank=True)


class BrokerCommissionSerializer(BaseModelSerializer):
    brokerName = serializers.CharField(source="broker.name", read_only=True)
    brokerCode = serializers.CharField(source="broker.code", read_only=True)
    partyName = serializers.CharField(source="party.name", read_only=True)
    invoiceNumber = serializers.CharField(source="invoice.invoice_number", read_only=True)
    broker_name = serializers.CharField(source="broker.name", read_only=True)
    broker_code = serializers.CharField(source="broker.code", read_only=True)
    party_name = serializers.CharField(source="party.name", read_only=True)
    invoice_number = serializers.CharField(source="invoice.invoice_number", read_only=True)
    leadNumber = serializers.SerializerMethodField()
    leadName = serializers.SerializerMethodField()
    lead_number = serializers.SerializerMethodField()
    lead_name = serializers.SerializerMethodField()

    def _lead(self, commission):
        from .models import order_lead
        inv = commission.invoice
        if inv is None:
            return None
        return order_lead(inv.sales_order) or order_lead(inv.delivery_challan) or order_lead(inv)

    def get_leadNumber(self, commission):
        lead = self._lead(commission)
        return lead.lead_number if lead else None

    def get_leadName(self, commission):
        lead = self._lead(commission)
        return lead.company or lead.name if lead else None

    def get_lead_number(self, commission):
        return self.get_leadNumber(commission)

    def get_lead_name(self, commission):
        return self.get_leadName(commission)

    class Meta:
        model = BrokerCommission
        fields = [
            "id", "commission_number", "broker", "brokerName", "brokerCode",
            "broker_name", "broker_code", "party_name", "invoice_number",
            "invoice", "invoiceNumber", "leadNumber", "leadName", "lead_number", "lead_name",
            "party", "partyName", "salesperson",
            "invoice_date", "invoice_amount", "commission_basis", "commission_type",
            "rate", "commission_amount", "paid_amount", "status",
            "approved_by", "approved_at", "paid_at", "payment_reference",
            "notes", "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]
