"""Purchase serializers (api.md §6)."""
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone
from rest_framework import serializers

from apps.core.document_serializers import (
    HEADER_FIELDS,
    READ_ONLY_HEADER_FIELDS,
    DocumentLineSerializer,
    DocumentSerializer,
)
from apps.core.money import ZERO, D, round2
from apps.core.serializers import (
    BaseModelSerializer,
    BaseSerializer,
    ISODateTimeField,
    MoneyField,
    PercentField,
    QuantityField,
    TenantPrimaryKeyRelatedField,
)
from apps.sales.serializers import line_serializer_for

from .services import vendor_bill_payment_summary
from .models import (
    FABRIC_FLOW_FACTORY_TO_MILL,
    PURCHASE_TYPE_FINAL_JOB_WORK,
    PURCHASE_TYPES,
    Expense,
    GoodsReceipt,
    GoodsReceiptLine,
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

PurchaseOrderLineSerializer = line_serializer_for(
    PurchaseOrderLine,
    "purchase_order_lines",
    extra_fields=["received_qty", "billed_qty"],
    extra_read_only=["received_qty", "billed_qty"],
)


class PurchaseBillLineSerializer(DocumentLineSerializer):
    """Carries the weight-receiving and landed-cost fields (api.md §6.3, §6.4)."""

    receivedQty = QuantityField(source="received_qty", required=False)
    receivedWeight = QuantityField(source="received_weight", required=False, allow_null=True)
    theoreticalWeight = QuantityField(
        source="theoretical_weight", required=False, allow_null=True, read_only=True
    )
    variationPct = serializers.DecimalField(
        source="variation_pct", max_digits=9, decimal_places=4,
        coerce_to_string=False, read_only=True,
    )
    tolerancePct = serializers.DecimalField(
        source="tolerance_pct", max_digits=7, decimal_places=4,
        coerce_to_string=False, required=False, allow_null=True,
    )
    isWeightItem = serializers.BooleanField(source="is_weight_item", read_only=True)
    batchNumber = serializers.CharField(
        source="batch_number", required=False, allow_null=True, allow_blank=True
    )
    landedUnitCost = serializers.DecimalField(
        source="landed_unit_cost", max_digits=18, decimal_places=4,
        coerce_to_string=False, read_only=True,
    )

    class Meta(DocumentLineSerializer.Meta):
        model = PurchaseBillLine
        fields = DocumentLineSerializer.Meta.fields + [
            "receivedQty", "receivedWeight", "theoreticalWeight", "variationPct",
            "tolerancePct", "isWeightItem", "batchNumber", "returned_qty",
            "base_unit_cost", "apportioned_cost", "landedUnitCost",
        ]
        read_only_fields = DocumentLineSerializer.Meta.read_only_fields + [
            "returned_qty", "base_unit_cost", "apportioned_cost",
        ]


# ---------------------------------------------------------------------------
# Purchase orders (api.md §6.2)
# ---------------------------------------------------------------------------
class PurchaseOrderSerializer(DocumentSerializer):
    line_model = PurchaseOrderLine
    line_serializer = PurchaseOrderLineSerializer
    line_fk_name = "purchase_order"
    line_table_name = "purchase_order_lines"

    #: ``DocumentSerializer`` declares ``partyId`` over ``source="party"``;
    #: api.md §6.2/§6.4 name the same column ``vendorId``. Two *writable* fields
    #: on one source makes DRF demand both on create, so ``partyId`` is demoted
    #: to a read-only mirror and ``vendorId`` carries the write.
    vendorId = TenantPrimaryKeyRelatedField(source="party", model="masters.Party", required=False)
    vendorName = serializers.CharField(source="party_name", read_only=True)
    partyId = serializers.CharField(source="party_id", read_only=True)

    #: The UI shows the derived billed status, not the stored one (api.md §6.2).
    billedStatus = serializers.SerializerMethodField()

    purchaseType = serializers.ChoiceField(
        source="purchase_type", choices=PURCHASE_TYPES, required=False
    )
    #: Optional -- a general grey stock purchase has no project.
    projectId = TenantPrimaryKeyRelatedField(
        source="project", model="pms.Project", required=False, allow_null=True
    )
    projectCode = serializers.CharField(source="project.code", read_only=True, default=None)
    projectName = serializers.CharField(source="project.customer_name", read_only=True, default=None)
    #: Factory fabric flow (Factory -> OSCAR, or Factory -> Dyeing Mill -> OSCAR).
    fabricFlow = serializers.CharField(source="fabric_flow", required=False, allow_null=True, allow_blank=True)
    dyeingMillId = TenantPrimaryKeyRelatedField(
        source="dyeing_mill", model="masters.Party", required=False, allow_null=True
    )
    dyeingMillName = serializers.CharField(source="dyeing_mill.name", read_only=True, default=None)
    dyeingColourName = serializers.CharField(
        source="dyeing_colour_name", required=False, allow_null=True, allow_blank=True
    )
    dyeingColourHex = serializers.CharField(
        source="dyeing_colour_hex", required=False, allow_null=True, allow_blank=True
    )
    dyeingRate = serializers.DecimalField(
        source="dyeing_rate", max_digits=18, decimal_places=4, required=False, allow_null=True
    )
    #: Flow 2: written through ``dyeing_dispatch.set_source``.
    sourcePurchaseOrderId = serializers.CharField(source="source_purchase_order_id", read_only=True)
    sourcePoNumber = serializers.CharField(source="source_purchase_order.po_number", read_only=True, default=None)
    dyeingBatches = serializers.SerializerMethodField()

    def get_dyeingBatches(self, order):
        if order.fabric_flow != "OSCAR_TO_MILL":
            return []
        lines = order.line_items.filter(deleted_at__isnull=True, source_batch__isnull=False).select_related("source_batch")
        return [
            {"batchId": str(line.source_batch_id), "batchCode": line.source_batch.batch_code, "qty": str(line.qty)}
            for line in lines.order_by("line_no")
        ]

    class Meta:
        model = PurchaseOrder
        fields = HEADER_FIELDS + [
            "po_number", "status", "expected_date", "location",
            "reference_number", "auto_generated", "vendorId", "vendorName",
            "billedStatus", "purchaseType", "projectId", "projectCode", "projectName",
            "fabricFlow", "dyeingMillId", "dyeingMillName", "dyeingColourName", "dyeingColourHex",
            "dyeingRate", "sourcePurchaseOrderId", "sourcePoNumber", "dyeingBatches",
        ]
        read_only_fields = READ_ONLY_HEADER_FIELDS + ["po_number", "auto_generated"]

    #: Statuses a PO may be created in. Everything after that (submit, approve,
    #: reject, cancel, receipt) is a server transition with its own permission
    #: and separation-of-duties checks, so a plain create/edit can't skip them.
    CREATE_STATUSES = ("Draft", "Issued")

    def validate_status(self, value):
        if self.instance is None:
            if value not in self.CREATE_STATUSES:
                raise serializers.ValidationError(
                    "A new PO starts as Draft; use Submit / Approve to move it on."
                )
        elif value != self.instance.status:
            raise serializers.ValidationError(
                "Status changes go through the PO actions (submit, approve, reject, cancel)."
            )
        return value

    def validate_fabricFlow(self, value):
        from .fabric_flow import validate_flow

        return validate_flow(value)

    def validate(self, attrs):
        from .fabric_flow import clean_dyeing_colour, clean_dyeing_mill, clean_dyeing_rate, mark_purchase_type

        attrs = super().validate(attrs)
        instance = self.instance
        if instance is not None and instance.status != "Draft" and "fabric_flow" in attrs \
                and (attrs["fabric_flow"] or None) != instance.fabric_flow:
            raise serializers.ValidationError(
                {"fabricFlow": "The fabric flow cannot change once the PO has left Draft."}
            )
        attrs = mark_purchase_type(attrs)
        if attrs.get("fabric_flow") is None and "fabric_flow" in attrs:
            attrs["dyeing_mill"] = None
        attrs = clean_dyeing_colour(attrs, instance)
        attrs = clean_dyeing_mill(attrs, instance)
        attrs = clean_dyeing_rate(attrs, instance)
        purchase_type = attrs.get("purchase_type")
        if instance is None:
            # Final job work POs are only generated from a verified production
            # instruction, never typed into the Create PO form.
            if purchase_type == PURCHASE_TYPE_FINAL_JOB_WORK:
                raise serializers.ValidationError(
                    {"purchaseType": "Final job work POs are generated from a verified production instruction."}
                )
        else:
            locked = instance.status not in ("Draft",)
            if locked and purchase_type and purchase_type != instance.purchase_type:
                raise serializers.ValidationError(
                    {"purchaseType": "The purchase type cannot change once the PO has left Draft."}
                )
            if locked and "project" in attrs and attrs["project"] != instance.project:
                raise serializers.ValidationError(
                    {"projectId": "The project cannot change once the PO has left Draft."}
                )
        project = attrs.get("project")
        if project is not None and not attrs.get("reference_number") and not getattr(instance, "reference_number", None):
            # Older project screens find their POs by reference_number.
            attrs["reference_number"] = project.code
        return attrs


    def get_billedStatus(self, order):
        from .services import po_billed_status

        # Only computed on the detail view -- running it per row would make the
        # list N+1 (db.md §15).
        if self.context.get("include_billed_status"):
            return po_billed_status(order)
        return None


class PurchaseBillSerializer(DocumentSerializer):
    line_model = PurchaseBillLine
    line_serializer = PurchaseBillLineSerializer
    line_fk_name = "purchase_bill"
    line_table_name = "purchase_bill_lines"

    #: ``DocumentSerializer`` declares ``partyId`` over ``source="party"``;
    #: api.md §6.2/§6.4 name the same column ``vendorId``. Two *writable* fields
    #: on one source makes DRF demand both on create, so ``partyId`` is demoted
    #: to a read-only mirror and ``vendorId`` carries the write.
    vendorId = TenantPrimaryKeyRelatedField(source="party", model="masters.Party")
    vendorName = serializers.CharField(source="party_name", read_only=True)
    partyId = serializers.CharField(source="party_id", read_only=True)

    vendorBillNumber = serializers.CharField(
        source="vendor_bill_number", required=False, allow_null=True, allow_blank=True
    )
    goodsReceived = serializers.BooleanField(source="goods_received", read_only=True)
    qcStatus = serializers.CharField(source="qc_status", read_only=True)
    purchaseOrderId = TenantPrimaryKeyRelatedField(
        source="purchase_order", model="purchase.PurchaseOrder",
        required=False, allow_null=True
    )
    purchaseOrder = serializers.CharField(source="purchase_order_id", read_only=True)
    poNumber = serializers.CharField(source="purchase_order.po_number", read_only=True, allow_null=True)

    poAlreadyReceived = serializers.SerializerMethodField()

    def get_poAlreadyReceived(self, bill):
        return bool(getattr(bill, "po_already_received", False))

    class Meta:
        model = PurchaseBill
        fields = HEADER_FIELDS + [
            "bill_number", "vendorBillNumber", "status", "due_date",
            "purchase_order", "purchaseOrderId", "purchaseOrder", "poNumber", "location", "goodsReceived", "poAlreadyReceived", "received_date",
            "qcStatus", "qc_note", "vendorId", "vendorName",
        ]
        read_only_fields = READ_ONLY_HEADER_FIELDS + [
            "bill_number", "received_date", "qc_note",
        ]



class ReceiveGoodsSerializer(BaseSerializer):
    """``POST /purchase/bills/{id}/receive-goods/`` (api.md §6.4).

    A line may be addressed by ``lineId``, ``lineIndex`` or ``sku`` -- all three
    appear in the frontend's call sites.
    """

    lines = serializers.ListField(child=serializers.DictField(), required=False, default=list)
    qcStatus = serializers.ChoiceField(
        choices=["Approved", "Pending Approval", "Rejected", "Rework"],
        required=False,
        allow_null=True,
    )
    locationId = serializers.CharField(required=False, allow_null=True)


class QcSerializer(BaseSerializer):
    status = serializers.ChoiceField(
        choices=["Approved", "Pending Approval", "Rejected", "Rework"]
    )
    note = serializers.CharField(required=False, allow_blank=True, allow_null=True)


class GoodsReceiptLineSerializer(BaseModelSerializer):
    itemId = serializers.CharField(source="item_id", read_only=True)
    sku = serializers.CharField(source="item.sku", read_only=True)
    itemName = serializers.CharField(source="item.name", read_only=True)
    unitCost = serializers.DecimalField(source="unit_cost", max_digits=14, decimal_places=4, read_only=True)
    batchCost = serializers.SerializerMethodField()

    def get_batchCost(self, obj):
        if obj.unit_cost and obj.received_qty:
            return str(round(float(obj.unit_cost) * float(obj.received_qty), 2))
        return "0.00"

    class Meta:
        model = GoodsReceiptLine
        fields = [
            "id", "itemId", "sku", "itemName", "uom", "uom_factor", "stock_uom", "stock_qty",
            "ordered_qty", "received_qty",
            "weighed_qty", "rejected_qty", "shortage_qty", "excess_qty",
            "roll_count", "weight_kg", "fabric_width", "remarks",
            "batch_number", "unit_cost", "unitCost", "batchCost", "variation_pct",
        ]


class GoodsReceiptSerializer(BaseModelSerializer):
    lines = GoodsReceiptLineSerializer(many=True, read_only=True)
    vendorName = serializers.CharField(source="party.name", read_only=True)
    vendorCode = serializers.CharField(source="party.code", read_only=True)
    billNumber = serializers.CharField(
        source="purchase_bill.bill_number", read_only=True, allow_null=True
    )
    poNumber = serializers.CharField(
        source="purchase_order.po_number", read_only=True, allow_null=True
    )
    fabricFlow = serializers.CharField(source="purchase_order.fabric_flow", read_only=True, default=None)
    dyeingColourName = serializers.CharField(source="purchase_order.dyeing_colour_name", read_only=True, default=None)
    dyeingColourHex = serializers.CharField(source="purchase_order.dyeing_colour_hex", read_only=True, default=None)
    fabricType = serializers.SerializerMethodField()

    def get_fabricType(self, receipt):
        from .fabric_flow import receipt_fabric_type

        return receipt_fabric_type(receipt.purchase_order)

    class Meta:
        model = GoodsReceipt
        fields = [
            "id", "grn_number", "purchase_order", "poNumber", "fabricFlow", "fabricType",
            "dyeingColourName", "dyeingColourHex", "purchase_bill", "billNumber",
            "party", "vendorName", "vendorCode", "receipt_date",
            "supplier_challan_no", "supplier_challan_date",
            "location", "qc_status", "qc_note", "qc_at", "lines", "created_at",
        ]


# ---------------------------------------------------------------------------
# Payments out, returns, expenses
# ---------------------------------------------------------------------------
class PaymentOutSerializer(BaseModelSerializer):
    vendorId = TenantPrimaryKeyRelatedField(source="party", model="masters.Party")
    vendorName = serializers.CharField(source="party.name", read_only=True)
    bankAccountId = TenantPrimaryKeyRelatedField(
        source="bank_account", model="accounting.BankAccount", required=False, allow_null=True
    )
    date = serializers.DateField(source="payment_date")
    unallocatedAmount = serializers.SerializerMethodField()

    class Meta:
        model = PaymentOut
        fields = [
            "id", "payment_number", "vendorId", "vendorName", "date", "amount",
            "mode", "bankAccountId", "reference_number", "notes",
            "allocated_amount", "unallocatedAmount", "status",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "payment_number", "allocated_amount", "status", "created_at", "updated_at",
        ]


    def get_unallocatedAmount(self, payment):
        return payment.unallocated_amount


class PurchaseReturnLineSerializer(DocumentLineSerializer):
    purchaseBillLineId = serializers.PrimaryKeyRelatedField(
        source="purchase_bill_line", queryset=PurchaseBillLine.objects.all()
    )
    returnedQty = QuantityField(source="returned_qty")

    class Meta(DocumentLineSerializer.Meta):
        model = PurchaseReturnLine
        fields = DocumentLineSerializer.Meta.fields + ["purchaseBillLineId", "returnedQty"]


class PurchaseReturnSerializer(DocumentSerializer):
    line_model = PurchaseReturnLine
    line_serializer = PurchaseReturnLineSerializer
    line_fk_name = "purchase_return"
    line_table_name = "purchase_return_lines"

    #: ``DocumentSerializer`` declares ``partyId`` over ``source="party"``;
    #: api.md §6.2/§6.4 name the same column ``vendorId``. Two *writable* fields
    #: on one source makes DRF demand both on create, so ``partyId`` is demoted
    #: to a read-only mirror and ``vendorId`` carries the write.
    vendorId = TenantPrimaryKeyRelatedField(source="party", model="masters.Party")
    vendorName = serializers.CharField(source="party_name", read_only=True)
    partyId = serializers.CharField(source="party_id", read_only=True)

    purchaseBillId = TenantPrimaryKeyRelatedField(
        source="purchase_bill", queryset=PurchaseBill.objects.all()
    )

    class Meta:
        model = PurchaseReturn
        fields = HEADER_FIELDS + [
            "return_number", "debit_note_number", "status", "purchaseBillId",
            "reason", "location", "vendorId", "vendorName",
        ]
        read_only_fields = READ_ONLY_HEADER_FIELDS + [
            "return_number", "debit_note_number",
        ]


class ExpenseSerializer(BaseModelSerializer):
    categoryId = TenantPrimaryKeyRelatedField(
        source="category", model="accounting.ExpenseCategory", required=False, allow_null=True
    )
    categoryName = serializers.CharField(source="category.name", read_only=True)
    vendorId = TenantPrimaryKeyRelatedField(
        source="party", model="masters.Party", required=False, allow_null=True
    )
    vendorName = serializers.CharField(source="party.name", read_only=True)
    bankAccountId = TenantPrimaryKeyRelatedField(
        source="bank_account", model="accounting.BankAccount", required=False, allow_null=True
    )
    receiptFileId = TenantPrimaryKeyRelatedField(
        source="receipt_file", model="core.File", required=False, allow_null=True
    )
    date = serializers.DateField(source="expense_date")

    class Meta:
        model = Expense
        fields = [
            "id", "expense_number", "categoryId", "categoryName", "vendorId",
            "vendorName", "date", "amount", "tax_amount", "total", "payment_mode",
            "bankAccountId", "receiptFileId", "account", "reference_number",
            "notes", "status", "created_at", "updated_at",
        ]
        read_only_fields = ["expense_number", "total", "created_at", "updated_at"]



class BillOutstandingSerializer(BaseSerializer):
    total = MoneyField()
    paid = MoneyField()
    outstanding = MoneyField()
    dueDate = serializers.DateField(allow_null=True)
    daysOverdue = serializers.IntegerField()
    ageingBucket = serializers.CharField()


# ---------------------------------------------------------------------------
# Vendor bills -- manual entry, matched later to a real PurchaseBill
# ---------------------------------------------------------------------------
class VendorBillLineSerializer(BaseModelSerializer):
    lineNo = serializers.IntegerField(source="line_no", required=False)
    batchId = TenantPrimaryKeyRelatedField(
        source="batch", model="inventory.Batch", required=False, allow_null=True
    )
    batchNumber = serializers.CharField(
        source="batch_number", required=False, allow_null=True, allow_blank=True
    )
    batchCode = serializers.CharField(
        source="batch.batch_code", read_only=True, allow_null=True
    )
    grnId = TenantPrimaryKeyRelatedField(
        source="goods_receipt", model="purchase.GoodsReceipt", required=False, allow_null=True
    )
    grnNumber = serializers.CharField(
        source="goods_receipt.grn_number", read_only=True, allow_null=True
    )
    itemId = TenantPrimaryKeyRelatedField(
        source="item", model="masters.Item", required=False, allow_null=True
    )
    itemName = serializers.CharField(
        source="item_name", required=False, allow_null=True, allow_blank=True
    )
    qty = QuantityField()
    rate = QuantityField(required=False)
    lineTotal = MoneyField(source="line_total", read_only=True)

    class Meta:
        model = VendorBillLine
        fields = [
            "id", "lineNo", "batchId", "batchNumber", "batchCode", "grnId", "grnNumber",
            "itemId", "sku", "itemName", "uom",
            "qty", "rate", "amount", "lineTotal",
        ]
        read_only_fields = ["amount"]

    def validate(self, attrs):
        qty = attrs.get("qty")
        if qty is not None and D(qty) <= ZERO:
            raise serializers.ValidationError(
                {"qty": "Quantity must be greater than zero."}
            )
        rate = attrs.get("rate")
        if rate is not None and D(rate) < ZERO:
            raise serializers.ValidationError(
                {"rate": "Rate cannot be negative."}
            )
        return attrs


class VendorBillSerializer(BaseModelSerializer):
    def to_internal_value(self, data):
        data = data.copy() if hasattr(data, "copy") else dict(data)
        po_id = data.get("purchaseOrderId") or data.get("purchase_order")
        if po_id and not data.get("vendorId"):
            from apps.purchase.models import PurchaseOrder
            po = PurchaseOrder.objects.filter(pk=po_id).select_related("party").first()
            # A dyeing-mill order is billed by its factory or its mill: the
            # user says which, it is not assumed.
            if po and po.party_id and po.fabric_flow != FABRIC_FLOW_FACTORY_TO_MILL:
                data["vendorId"] = str(po.party_id)
        return super().to_internal_value(data)
    vendorId = TenantPrimaryKeyRelatedField(source="party", model="masters.Party")
    vendorName = serializers.CharField(source="party_name", read_only=True)
    vendorBillNumber = serializers.CharField(source="vendor_bill_number")
    billDate = serializers.DateField(source="bill_date")
    purchaseOrderId = TenantPrimaryKeyRelatedField(
        source="purchase_order", model="purchase.PurchaseOrder",
        required=False, allow_null=True,
    )
    poNumber = serializers.CharField(source="purchase_order.po_number", read_only=True, allow_null=True)
    grnId = TenantPrimaryKeyRelatedField(
        source="goods_receipt", model="purchase.GoodsReceipt",
        required=False, allow_null=True,
    )
    attachmentFileId = TenantPrimaryKeyRelatedField(
        source="attachment", model="core.File", required=False, allow_null=True
    )
    billFileName = serializers.CharField(source="attachment.file_name", read_only=True)
    purchaseBillId = serializers.CharField(source="purchase_bill_id", read_only=True)
    purchaseBillNumber = serializers.CharField(
        source="purchase_bill.bill_number", read_only=True
    )
    gstPct = PercentField(source="gst_pct", required=False)
    subtotal = MoneyField(read_only=True)
    gstAmount = MoneyField(source="gst_amount", read_only=True)
    otherCharges = MoneyField(source="other_charges", required=False)
    total = MoneyField(read_only=True)
    matchStatus = serializers.CharField(source="match_status", read_only=True)
    remarks = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    approvalRemarks = serializers.CharField(
        source="approval_remarks", read_only=True
    )
    approvedAt = serializers.DateTimeField(source="approved_at", read_only=True)
    approvedByName = serializers.SerializerMethodField()
    approvalHistory = serializers.JSONField(
        source="approval_history", read_only=True
    )
    matchResult = serializers.JSONField(source="match_result", read_only=True)
    lineItems = serializers.ListField(
        child=serializers.DictField(), required=False, write_only=True
    )
    lines = VendorBillLineSerializer(source="line_items", many=True, read_only=True)
    amountPaid = serializers.SerializerMethodField()
    outstandingAmount = serializers.SerializerMethodField()
    paymentStatus = serializers.SerializerMethodField()

    def get_amountPaid(self, bill):
        return float(vendor_bill_payment_summary(bill)["paid"])

    def get_outstandingAmount(self, bill):
        return float(vendor_bill_payment_summary(bill)["outstanding"])

    def get_paymentStatus(self, bill):
        return vendor_bill_payment_summary(bill)["paymentStatus"]

    class Meta:
        model = VendorBill
        fields = [
            "id", "vendorBillNumber", "billDate", "vendorId", "vendorName",
            "purchaseOrderId", "poNumber", "grnId", "attachmentFileId", "billFileName",
            "purchaseBillId", "purchaseBillNumber", "gstPct", "subtotal",
            "gstAmount", "otherCharges", "total", "remarks", "status", "matchStatus",
            "approvalRemarks", "approvedAt", "approvedByName",
            "approvalHistory", "matchResult",
            "lineItems", "lines", "amountPaid", "outstandingAmount", "paymentStatus", "created_at", "updated_at",
        ]
        # `status` moves only through approve / reject / matching actions.
        read_only_fields = [
            "status",
            "subtotal", "gstAmount", "total", "matchStatus",
            "approvalRemarks", "approvedAt", "approvedByName",
            "approvalHistory", "matchResult",
            "lines", "amountPaid", "outstandingAmount", "paymentStatus",
            "created_at", "updated_at",
        ]

    def get_approvedByName(self, bill):
        user = getattr(bill, "approved_by", None)
        return getattr(user, "name", None) if user is not None else None

    def validate(self, attrs):
        request = self.context.get("request")
        client_id = getattr(request, "client_id", None) or self.context.get("client_id")
        number = attrs.get(
            "vendor_bill_number",
            getattr(self.instance, "vendor_bill_number", None),
        )
        if number is not None and client_id is not None:
            duplicates = VendorBill.objects.filter(
                client_id=client_id,
                vendor_bill_number=number,
                deleted_at__isnull=True,
            )
            if self.instance is not None:
                duplicates = duplicates.exclude(pk=self.instance.pk)
            if duplicates.exists():
                raise serializers.ValidationError(
                    {"vendorBillNumber": "A vendor bill with this number already exists."}
                )

        po = attrs.get("purchase_order", getattr(self.instance, "purchase_order", None))
        if self.instance is None and not po:
            raise serializers.ValidationError(
                {"purchaseOrderId": "Purchase order selection is mandatory."}
            )
        if po is not None:
            # Serialises concurrent bills on one PO: each sees what the other billed.
            PurchaseOrder.objects.select_for_update().filter(pk=po.pk).first()
            if po.status == "Cancelled":
                raise serializers.ValidationError(
                    {"purchaseOrderId": f"Purchase order {po.po_number} is cancelled."}
                )
        mill_po = po is not None and po.fabric_flow == FABRIC_FLOW_FACTORY_TO_MILL
        allowed_parties = {po.party_id, po.dyeing_mill_id} - {None} if mill_po else {getattr(po, "party_id", None)} - {None}
        chosen_party = attrs.get("party")
        if allowed_parties and chosen_party is not None and chosen_party.pk not in allowed_parties:
            raise serializers.ValidationError(
                {"vendorId": f"This vendor is not the vendor on purchase order {po.po_number}."}
            )
        if po and po.party and not mill_po:
            attrs["party"] = po.party

        other_charges = attrs.get("other_charges")
        if other_charges is not None and D(other_charges) < ZERO:
            raise serializers.ValidationError(
                {"otherCharges": "Other charges cannot be negative."}
            )
        gst_pct = attrs.get("gst_pct", getattr(self.instance, "gst_pct", None))
        if gst_pct is not None and D(gst_pct) < ZERO:
            raise serializers.ValidationError(
                {"gstPct": "GST % cannot be negative."}
            )
        lines = attrs.get("lineItems", None)
        if (self.instance is None and not lines) or lines == []:
            raise serializers.ValidationError(
                {"lineItems": "Add at least one line."}
            )

        if lines and po:
            from apps.inventory.models import Batch
            from apps.purchase.models import PurchaseBillLine
            for line in lines:
                qty = D(str(line.get("qty") or 0))
                rate = D(str(line.get("rate") or 0))
                if qty <= ZERO:
                    raise serializers.ValidationError(
                        {"lineItems": "Billed quantity must be greater than zero."}
                    )
                if rate <= ZERO:
                    raise serializers.ValidationError(
                        {"lineItems": "Enter the actual vendor rate from the invoice."}
                    )
                b_id = line.get("batchId") or line.get("batch")
                b_num = line.get("batchNumber") or line.get("batchCode")
                batch = None
                if b_id:
                    batch = Batch.objects.filter(pk=b_id, deleted_at__isnull=True).first()
                elif b_num:
                    batch = Batch.objects.filter(
                        purchase_order=po, batch_code=b_num, deleted_at__isnull=True
                    ).first()
                if mill_po and batch is None:
                    raise serializers.ValidationError(
                        {"lineItems": "Every line of this bill needs its batch."}
                    )
                if batch:
                    if batch.purchase_order_id != po.id:
                        raise serializers.ValidationError(
                            {"lineItems": f"Batch {batch.batch_code} does not belong to the selected purchase order {po.po_number}."}
                        )
                    gl = batch.goods_receipt_line
                    lot_no = gl.batch_number if gl else ""
                    recv_qty = D(str(gl.received_qty)) if gl else D("0")
                    pb_billed = D("0")
                    # A payable made from a vendor bill repeats that bill's lines.
                    for pbl in PurchaseBillLine.objects.filter(
                        purchase_bill__purchase_order=po,
                        purchase_bill__deleted_at__isnull=True
                    ).exclude(purchase_bill__status="Cancelled").exclude(
                        purchase_bill__vendor_bills__deleted_at__isnull=True
                    ):
                        if pbl.batch_number and (pbl.batch_number == batch.batch_code or pbl.batch_number == lot_no):
                            pb_billed += D(str(pbl.qty or 0))
                    vb_billed = D("0")
                    other_vbs = VendorBillLine.objects.filter(
                        vendor_bill__purchase_order=po,
                        vendor_bill__deleted_at__isnull=True
                    ).exclude(vendor_bill__status__in=["Cancelled", "Rejected"])
                    if self.instance is not None:
                        other_vbs = other_vbs.exclude(vendor_bill=self.instance)
                    for ovb in other_vbs:
                        if (ovb.batch_id and ovb.batch_id == batch.id) or (ovb.batch_number and (ovb.batch_number == batch.batch_code or ovb.batch_number == lot_no)):
                            vb_billed += D(str(ovb.qty or 0))
                    total_billed = pb_billed + vb_billed
                    rem_billable = max(ZERO, recv_qty - total_billed)
                    if rem_billable <= ZERO:
                        raise serializers.ValidationError(
                            {"lineItems": f"Batch {batch.batch_code} is already fully billed and cannot be re-billed."}
                        )
                    if qty > rem_billable + D("0.0001"):
                        raise serializers.ValidationError(
                            {"lineItems": f"Billed quantity for batch {batch.batch_code} ({qty}) cannot exceed remaining billable quantity ({rem_billable})."}
                        )

        return attrs

    def to_representation(self, instance):
        data = super().to_representation(instance)
        lines = instance.line_items.filter(deleted_at__isnull=True).order_by("line_no")
        data["lineItems"] = VendorBillLineSerializer(
            lines, many=True, context=self.context
        ).data
        return data

    def create(self, validated_data):
        line_payloads = validated_data.pop("lineItems", [])
        bill = super().create(validated_data)
        bill.freeze_party_snapshot()
        bill.save(update_fields=["party_name", "updated_at"])
        self._write_lines(bill, line_payloads)
        self._recalculate(bill)
        return bill

    def update(self, instance, validated_data):
        line_payloads = validated_data.pop("lineItems", None)
        bill = super().update(instance, validated_data)
        if "party" in validated_data:
            bill.freeze_party_snapshot()
            bill.save(update_fields=["party_name", "updated_at"])
        if line_payloads is not None:
            self._replace_lines(bill, line_payloads)
        self._recalculate(bill)
        # The earlier comparison was of a bill that no longer exists.
        bill.match_status = "Pending Matching"
        bill.match_result = None
        bill.save(update_fields=["match_status", "match_result", "updated_at"])
        return bill

    def _write_lines(self, bill, payloads):
        for index, payload in enumerate(payloads or [], start=1):
            line_serializer = VendorBillLineSerializer(
                data=payload, context=self.context
            )
            line_serializer.is_valid(raise_exception=True)
            data = dict(line_serializer.validated_data)
            data.setdefault("line_no", index)
            batch_obj = data.get("batch")
            if batch_obj:
                if not data.get("batch_number"):
                    data["batch_number"] = batch_obj.batch_code
                if not data.get("goods_receipt") and batch_obj.goods_receipt:
                    data["goods_receipt"] = batch_obj.goods_receipt
                if not data.get("item") and batch_obj.item:
                    data["item"] = batch_obj.item
            line = VendorBillLine(
                client_id=bill.client_id, vendor_bill=bill, **data
            )
            line.freeze_item_snapshot()
            line.save()

    def _replace_lines(self, bill, payloads):
        request = self.context.get("request")
        user = getattr(request, "user", None)
        if not getattr(user, "is_authenticated", False):
            user = None
        for old in bill.line_items.filter(deleted_at__isnull=True):
            old.deleted_at = timezone.now()
            old.deleted_by = user
            old.save(update_fields=["deleted_at", "deleted_by", "updated_at"])
        self._write_lines(bill, payloads)

    def _recalculate(self, bill):
        lines = list(
            bill.line_items.filter(deleted_at__isnull=True).order_by("line_no")
        )
        subtotal = ZERO
        for line in lines:
            line.amount = round2(D(line.qty) * D(line.rate))
            line.line_total = line.amount
            subtotal += line.amount
        if lines:
            VendorBillLine.objects.bulk_update(
                lines, ["amount", "line_total", "updated_at"]
            )
        bill.subtotal = round2(subtotal)
        bill.gst_amount = round2(
            bill.subtotal * D(bill.gst_pct) / Decimal("100")
        )
        bill.total = round2(bill.subtotal + bill.gst_amount + D(bill.other_charges or 0))
        bill.save(
            update_fields=["subtotal", "gst_amount", "total", "updated_at"]
        )


class SupplierDeliverySerializer(BaseModelSerializer):
    purchaseOrder = TenantPrimaryKeyRelatedField(
        source="purchase_order", queryset=PurchaseOrder.objects.all(), required=False, allow_null=True
    )
    purchaseOrderId = TenantPrimaryKeyRelatedField(
        source="purchase_order", queryset=PurchaseOrder.objects.all(), required=False, allow_null=True
    )
    deliveryNumber = serializers.CharField(source="delivery_number", read_only=True)
    supplierChallanNo = serializers.CharField(source="supplier_challan_no", required=False, allow_null=True, allow_blank=True)
    supplierChallanDate = serializers.DateField(source="supplier_challan_date", required=False, allow_null=True)
    dispatchDate = serializers.DateField(source="dispatch_date")
    expectedDeliveryDate = serializers.DateField(source="expected_delivery_date", required=False, allow_null=True)
    actualDeliveryDate = serializers.DateField(source="actual_delivery_date", required=False, allow_null=True)
    transporterName = serializers.CharField(source="transporter_name", required=False, allow_null=True, allow_blank=True)
    vehicleNumber = serializers.CharField(source="vehicle_number", required=False, allow_null=True, allow_blank=True)
    lrBiltyNumber = serializers.CharField(source="lr_bilty_number", required=False, allow_null=True, allow_blank=True)
    dispatchedQty = QuantityField(source="dispatched_qty", required=False)
    status = serializers.CharField(required=False)
    remarks = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    createdAt = ISODateTimeField(source="created_at", read_only=True)
    updatedAt = ISODateTimeField(source="updated_at", read_only=True)

    poNumber = serializers.SerializerMethodField()
    supplierName = serializers.SerializerMethodField()
    fabricName = serializers.SerializerMethodField()
    orderedQty = serializers.SerializerMethodField()
    receivedQty = serializers.SerializerMethodField()
    remainingQty = serializers.SerializerMethodField()

    class Meta:
        model = SupplierDelivery
        fields = [
            "id",
            "deliveryNumber",
            "purchaseOrder",
            "purchaseOrderId",
            "supplierChallanNo",
            "supplierChallanDate",
            "dispatchDate",
            "expectedDeliveryDate",
            "actualDeliveryDate",
            "transporterName",
            "vehicleNumber",
            "lrBiltyNumber",
            "dispatchedQty",
            "status",
            "remarks",
            "poNumber",
            "supplierName",
            "fabricName",
            "orderedQty",
            "receivedQty",
            "remainingQty",
            "createdAt",
            "updatedAt",
        ]
        read_only_fields = ["id", "deliveryNumber", "createdAt", "updatedAt"]

    def get_poNumber(self, obj):
        return obj.purchase_order.po_number if obj.purchase_order else None

    def get_supplierName(self, obj):
        po = obj.purchase_order
        if po and po.party:
            return po.party.name
        return po.party_name if po else None

    def get_fabricName(self, obj):
        po = obj.purchase_order
        if po:
            lines = list(po.line_items.all())
            if not lines:
                return None
            if len(lines) == 1:
                fl = lines[0]
                return fl.item_name or (fl.item.name if fl.item else "Grey Fabric")
            first_name = lines[0].item_name or (lines[0].item.name if lines[0].item else "Fabric")
            return f"{first_name} (+{len(lines) - 1} more)"
        return None

    def get_orderedQty(self, obj):
        po = obj.purchase_order
        if po:
            return float(po.line_items.aggregate(s=Sum("qty"))["s"] or 0)
        return 0.0

    def get_receivedQty(self, obj):
        po = obj.purchase_order
        if po:
            return float(po.line_items.aggregate(s=Sum("received_qty"))["s"] or 0)
        return 0.0

    def get_remainingQty(self, obj):
        ordered = self.get_orderedQty(obj)
        received = self.get_receivedQty(obj)
        return max(0.0, ordered - received)

    def validate(self, attrs):
        po = attrs.get("purchase_order") or getattr(self.instance, "purchase_order", None)
        qty = attrs.get("dispatched_qty")
        if qty is not None and D(qty) <= ZERO:
            raise serializers.ValidationError({"dispatchedQty": "Dispatch quantity must be greater than zero."})
        if po and qty is not None:
            ordered = D(po.line_items.aggregate(s=Sum("qty"))["s"] or ZERO)
            qs = po.deliveries.all()
            if self.instance and self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)
            already_dispatched = D(qs.aggregate(s=Sum("dispatched_qty"))["s"] or ZERO)
            remaining = max(ZERO, ordered - already_dispatched)
            if ordered > ZERO and remaining <= ZERO:
                raise serializers.ValidationError({"dispatchedQty": f"All {ordered} ordered meters have already been dispatched."})
            if ordered > ZERO and D(qty) > remaining:
                raise serializers.ValidationError({
                    "dispatchedQty": f"Dispatch quantity ({qty}) exceeds remaining order quantity ({remaining})."
                })
        return attrs

