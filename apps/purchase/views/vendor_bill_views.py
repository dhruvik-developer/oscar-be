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


class VendorBillViewSet(TenantModelViewSet):

    """Manual vendor-bill entry, matched later to a real ``PurchaseBill``.



    Creating or editing a vendor bill only captures the vendor's invoice --

    it never posts stock or ledger entries. ``send-for-matching`` creates the

    ``PurchaseBill`` Draft; the bill's own receive/cancel flow posts from

    there.

    """



    queryset = VendorBill.objects.all()

    serializer_class = VendorBillSerializer

    audit_entity_type = "VendorBill"

    audit_label_field = "vendor_bill_number"

    status_field = "status"

    default_date_field = "bill_date"

    allowed_date_fields = ("bill_date", "created_at")

    search_fields = ["vendor_bill_number", "party_name", "remarks"]

    ordering = ["-bill_date", "-created_at"]

    filter_map = {

        "vendorId": "party_id",

        "purchaseOrderId": "purchase_order_id",

        "poId": "purchase_order_id",

        "po": "purchase_order_id",

        "status": "status",

        "matchStatus": "match_status",

    }

    permission_map = {"read": ["view_purchase"], "write": ["create_bill"]}

    idempotent_create = True

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        return super().create(request, *args, **kwargs)

    def _assert_editable(self, bill):
        if bill.purchase_bill_id or bill.status not in ("Draft", *VENDOR_BILL_PENDING_STATUSES):
            raise Conflict(
                f"Vendor bill {bill.vendor_bill_number} is {bill.status} and can no longer be changed.",
                code=Codes.DRAFT_ONLY,
            )

    def _assert_has_lines(self, bill):
        if not bill.line_items.filter(deleted_at__isnull=True).exists():
            raise Conflict(
                f"Vendor bill {bill.vendor_bill_number} has no lines, so there is nothing to approve or pay.",
                code="BILL_HAS_NO_LINES",
            )

    def perform_update(self, serializer):
        self._assert_editable(serializer.instance)
        return super().perform_update(serializer)

    def perform_destroy(self, instance):
        self._assert_editable(instance)
        return super().perform_destroy(instance)

    def get_queryset(self):

        return (

            super()

            .get_queryset()

            .select_related(

                "party", "purchase_order", "goods_receipt", "purchase_bill",

                "approved_by",

            )

            .prefetch_related("line_items__item")

        )



    
    @action(detail=False, methods=["get"], url_path="billable-pos")
    def billable_pos(self, request):
        """POs that still have received fabric to bill."""
        orders = PurchaseOrder.objects.filter(client_id=request.client_id, deleted_at__isnull=True).exclude(status="Cancelled")
        return Response({"results": [
            str(po.id) for po in orders.select_related("party", "dyeing_mill")
            if services.vendor_bill_po_snapshot(po)["poSummary"]["canCreateBill"]
        ]})

    @action(detail=False, methods=["get"], url_path="po-details")
    def po_details(self, request):
        """Fetch PO summary, vendor details, and batch-wise billing metrics."""
        po_id = request.query_params.get("poId") or request.query_params.get("po")
        if not po_id:
            raise ValidationFailed("poId query parameter is required.", field_errors={"poId": ["Required."]})

        po = (
            PurchaseOrder.objects.filter(pk=po_id, client_id=request.client_id, deleted_at__isnull=True)
            .select_related("party")
            .prefetch_related("line_items__item", "receipts__lines")
            .first()
        )
        if not po:
            raise NotFound("Purchase order not found.")

        party = po.party
        primary_contact = None
        if party:
            primary_contact = (
                party.contacts.filter(is_primary=True, deleted_at__isnull=True).first()
                or party.contacts.filter(deleted_at__isnull=True).first()
            )

        billing_addr = party.billing_address if party and isinstance(party.billing_address, dict) else (po.billing_address if isinstance(po.billing_address, dict) else {})
        addr_parts = [billing_addr.get("line1"), billing_addr.get("line2"), billing_addr.get("city"), billing_addr.get("state"), billing_addr.get("pincode")]
        formatted_address = ", ".join([str(p).strip() for p in addr_parts if p and str(p).strip()]) or "—"

        gstin = (party.gstin if party else po.party_gstin) or ""
        state = billing_addr.get("state") or (party.place_of_supply if party else po.place_of_supply) or "—"
        state_code = gstin[:2] if gstin and len(gstin) >= 2 and gstin[:2].isdigit() else "—"

        vendor_data = {
            "vendorId": str(party.id) if party else None,
            "vendorName": party.name if party else (po.party_name or "—"),
            "vendorCode": (party.code if party and party.code else "—"),
            "vendorGstin": gstin or "—",
            "contactPerson": primary_contact.name if primary_contact else "—",
            "phone": primary_contact.phone if (primary_contact and primary_contact.phone) else ((party.phone if party and party.phone else "—")),
            "email": primary_contact.email if (primary_contact and primary_contact.email) else ((party.email if party and party.email else "—")),
            "billingAddress": formatted_address,
            "state": state,
            "stateCode": state_code,
            "paymentTerms": (party.payment_terms if party and party.payment_terms else po.payment_terms) or "—",
            "outstandingBalance": float(party.balance or 0) if party else 0.0,
        }

        return Response({"vendor": vendor_data, **services.vendor_bill_po_snapshot(po)})

    def get_aggregates(self, queryset):

        return queryset.aggregate(

            count=Count("id"), totalValue=money_sum("total")

        )



    @action(detail=True, methods=["post"], url_path="send-for-matching")

    @transaction.atomic

    def send_for_matching(self, request, pk=None):

        """Create the ``PurchaseBill`` Draft from this vendor bill.



        Carries party, lines (qty/rate), the vendor bill number and the PO

        link (plus the PO's location); the Draft itself posts nothing.

        """

        target_pk = pk or self.kwargs.get(self.lookup_field) or (self.get_object().pk if hasattr(self, "kwargs") and self.lookup_field in self.kwargs else None)

        bill = VendorBill.objects.get(pk=target_pk, client_id=request.client_id)

        if bill.status == "Rejected":

            raise Conflict(

                "A rejected vendor bill cannot be sent for matching.",

                code=Codes.DRAFT_ONLY,

            )

        if bill.purchase_bill_id:

            raise Conflict(

                "This vendor bill is already matched.",

                code=Codes.ALREADY_DONE,

            )



        lines = list(

            bill.line_items.filter(deleted_at__isnull=True).order_by("line_no")

        )

        if not lines:

            raise ValidationFailed(

                "Add at least one line before matching.",

                field_errors={"lineItems": ["Add at least one line."]},

            )



        if bill.purchase_order_id is not None and bill.status != "Approved":

            matching = services.compute_vendor_bill_matching(

                request.client_id, bill

            )

            if matching.get("overallStatus") == "Mismatch":

                raise Conflict(

                    "Quantities or rates do not match the purchase order. "

                    "Approve the vendor bill before sending it for matching.",

                    code="BILL_MISMATCH",

                    payload={"matching": matching},

                )



        client = request.user.client

        purchase_order = bill.purchase_order

        purchase_bill = PurchaseBill(

            client_id=request.client_id,

            party=bill.party,

            doc_date=bill.bill_date,

            vendor_bill_number=bill.vendor_bill_number,

            purchase_order=purchase_order,

            location=purchase_order.location if purchase_order else None,

            status="Draft",

            bill_number=allocate_number(client, "BILL", bill.bill_date),

        )

        purchase_bill.freeze_party_snapshot()

        purchase_bill.save()



        PurchaseBillLine.objects.bulk_create(

            [

                PurchaseBillLine(

                    client_id=request.client_id,

                    purchase_bill=purchase_bill,

                    line_no=line.line_no,

                    item=line.item,
                    batch_number=line.batch_number,

                    sku=line.sku or (line.item.sku if line.item else None),

                    item_name=line.item_name or (line.item.name if line.item else None),

                    uom=line.uom or (line.item.uom if line.item else None),

                    uom_factor=uom.line_factor(

                        request.client_id, line.item,

                        line.uom or (line.item.uom if line.item else None),

                    ),

                    qty=qty,

                    rate=rate,

                    tax_pct=bill.gst_pct,

                )

                for line, qty, rate in services.vendor_bill_payable_terms(bill)

            ]

        )



        from apps.sales.services import recalculate_document



        recalculate_document(purchase_bill)



        bill.purchase_bill = purchase_bill

        bill.match_status = "Matched"

        history = list(bill.approval_history or [])

        history.append(

            {

                "at": timezone.now().isoformat(),

                "by": getattr(request.user, "name", None)

                or getattr(request.user, "email", ""),

                "action": "Sent for Approval",

                "remarks": "",

            }

        )

        bill.approval_history = history

        bill.save(

            update_fields=[

                "purchase_bill", "match_status", "approval_history", "updated_at",

            ]

        )

        # An approved bill has nothing left to wait for: post it now, or the
        # payable sits in Draft and cannot be paid from Purchase Bills.

        if bill.status == "Approved":

            services.ensure_vendor_bill_payable(bill, user=request.user)

        self.write_audit(

            "match", bill,

            description=f"PurchaseBill {purchase_bill.bill_number} created",

        )

        return Response(

            {

                "vendorBill": self.get_serializer(bill).data,

                "purchaseBillId": str(purchase_bill.id),

                "billNumber": purchase_bill.bill_number,

            },

            status=status.HTTP_201_CREATED,

        )



    MISMATCH_TYPE = "vendor_bill_mismatch"

    MISMATCH_APPROVER_ROLE_CODES = ["ACCOUNTS_MGR", "ACCOUNTS", "OWNER"]



    def _mismatch_gate(self, bill, matching, remarks, basis):

        """None when this mismatched bill may be approved now; otherwise a 409

        with the approval request that was opened for it."""

        from apps.core.approval_services import raise_approval

        from apps.core.models import ApprovalRequest

        from apps.core.permissions import has_permission



        if has_permission(self.request.user, "approve_bill_mismatch"):

            return None

        summary = matching.get("summary") or {}

        fingerprint = {
            "billTotal": str(D(bill.total or 0)),
            "amountDiff": str(summary.get("amountDiff")),
            "approvalBasis": basis,
        }

        approved = ApprovalRequest.objects.filter(

            client_id=bill.client_id,

            document_type=self.MISMATCH_TYPE,

            document_id=str(bill.id),

            status="Approved",

            new_values__system=True,

        )

        # An approval covers the bill only as it was when approved.

        if any(

            (req.new_values or {}).get("billTotal") == fingerprint["billTotal"]

            and (req.new_values or {}).get("amountDiff") == fingerprint["amountDiff"]

            and (req.new_values or {}).get("approvalBasis", services.APPROVAL_BASIS_VENDOR_BILL) == basis

            for req in approved

        ):

            return None

        req = raise_approval(

            client_id=bill.client_id,

            document_type=self.MISMATCH_TYPE,

            document_id=bill.id,

            document_number=bill.vendor_bill_number,

            requested_by=self.request.user,

            reason=(

                f"Vendor bill {bill.vendor_bill_number} does not match PO/GRN: "

                + ("; ".join((matching.get("reasons") or [])[:3]) or "variance")

                + (
                    f". Approving sets the payable to the PO amount {(matching.get('poBasis') or {}).get('total')}, not the vendor bill total"
                    if basis == services.APPROVAL_BASIS_PO
                    else f". Approving accepts the vendor bill total {D(bill.total or 0)}, not the PO amount"
                )

                + (f". Remarks: {remarks}" if remarks else "")

            ),

            approver_role_code=self.MISMATCH_APPROVER_ROLE_CODES[0],

            allowed_role_codes=self.MISMATCH_APPROVER_ROLE_CODES,

            new_values={**fingerprint, "reasons": matching.get("reasons") or []},

            variance_amount=D(str(summary.get("amountDiff") or 0)),

        )

        return Response(

            {

                "message": (

                    f"Bill {bill.vendor_bill_number} does not match and needs approval by accounts "

                    "management. The request is in the approvals inbox; approve the bill again once it is approved."

                ),

                "code": "APPROVAL_REQUIRED",

                "payload": {

                    "approvalRequestId": str(req.id),

                    "approverRoleCodes": self.MISMATCH_APPROVER_ROLE_CODES,

                    "matching": matching,

                },

            },

            status=status.HTTP_409_CONFLICT,

        )



    @action(detail=True, methods=["post"])

    @transaction.atomic

    def approve(self, request, pk=None):

        # Lock the row: two simultaneous approvals must not create two payables.

        target_pk = pk or self.kwargs.get(self.lookup_field) or (self.get_object().pk if hasattr(self, "kwargs") and self.lookup_field in self.kwargs else None)

        bill = VendorBill.objects.select_for_update().get(pk=target_pk, client_id=request.client_id)

        if bill.status not in VENDOR_BILL_PENDING_STATUSES:

            raise Conflict(

                f"Only a pending bill can be approved. This bill is {bill.status}.",

                code=Codes.DRAFT_ONLY,

            )

        self._assert_has_lines(bill)

        # Separation of duties: Creator cannot approve their own bill unless superuser

        if getattr(bill, "created_by_id", None) and bill.created_by_id == getattr(request.user, "id", None) and not getattr(request.user, "is_superuser", False):

            from apps.core.permissions import PermissionDenied

            raise PermissionDenied(

                "Separation of duties: You cannot approve a vendor bill you created.",

                code="SELF_APPROVAL_NOT_PERMITTED",

            )



        remarks = request.data.get("remarks")

        # Approving releases the payable even though PO / GRN / bill disagree,

        # so the variance and the reason are put on record. Nothing on the PO,

        # GRN or bill is changed to force a match.

        matching = services.compute_vendor_bill_matching(request.client_id, bill)

        is_mismatch = matching.get("overallStatus") == "Mismatch"

        if is_mismatch and not (remarks or "").strip():

            raise ValidationFailed(

                "This bill does not match the commercial records. Enter approval remarks explaining the variance.",

                field_errors={"remarks": ["Required to approve a mismatched bill."]},

                payload={"matching": matching},

            )

        basis = request.data.get("approvalBasis") or services.APPROVAL_BASIS_VENDOR_BILL

        at_po_amount = basis == services.APPROVAL_BASIS_PO

        po_basis = matching.get("poBasis") or {}

        if basis not in services.APPROVAL_BASES or (at_po_amount and not (is_mismatch and po_basis.get("available"))):

            message = po_basis.get("reason") or "Only a mismatched bill can be approved at the PO amount."

            raise ValidationFailed(message, field_errors={"approvalBasis": [message]})

        if is_mismatch:

            # A mismatched bill releases a payable the records do not support,

            # so it needs approve_bill_mismatch (PDF §16) -- either held by the

            # user approving now, or given through the approvals inbox.

            blocked = self._mismatch_gate(bill, matching, remarks, basis)

            if blocked is not None:

                return blocked

        now = timezone.now()

        bill.status = "Approved"

        if remarks is not None:

            bill.approval_remarks = remarks

        bill.approved_by = request.user

        bill.approved_at = now

        bill.match_result = matching

        bill.match_status = "Mismatch" if is_mismatch else "Matched"

        history = list(bill.approval_history or [])

        history.append(

            {

                "at": now.isoformat(),

                "by": getattr(request.user, "name", None) or str(request.user),

                "action": (
                    "Approved at PO amount" if at_po_amount
                    else "Approved with variance" if is_mismatch
                    else "Approved"
                ),

                "approvalBasis": basis,

                "approvedAmount": po_basis.get("total") if at_po_amount else float(D(bill.total or 0)),

                "remarks": remarks,

                "matchStatus": matching.get("overallStatus"),

                "variances": matching.get("reasons") or [],

            }

        )

        bill.approval_history = history



        auto_create_pb = request.data.get("createPayable", False) or getattr(request, "_create_payable", False)

        if auto_create_pb:
            bill.purchase_bill = services.ensure_vendor_bill_payable(
                bill, matching=matching, remarks=remarks, user=request.user
            )

        bill.save(
            update_fields=[
                "status", "approval_remarks", "approved_by", "approved_at",
                "approval_history", "match_result", "match_status",
                "purchase_bill", "updated_at",
            ]
        )

        description = remarks

        if is_mismatch:

            label = f"Approved at PO amount {po_basis.get('total')}" if at_po_amount else "Approved with variance"

            description = f"{label}: {remarks}. " + "; ".join(matching.get("reasons") or [])

        self.write_audit("approve", bill, description=description)

        return Response(self.get_serializer(bill).data)



    @action(detail=True, methods=["post"])

    @transaction.atomic

    def reject(self, request, pk=None):

        target_pk = pk or self.kwargs.get(self.lookup_field) or (self.get_object().pk if hasattr(self, "kwargs") and self.lookup_field in self.kwargs else None)

        bill = VendorBill.objects.get(pk=target_pk, client_id=request.client_id)

        if bill.status not in VENDOR_BILL_PENDING_STATUSES:

            raise Conflict(

                f"Only a pending bill can be rejected. This bill is {bill.status}.",

                code=Codes.DRAFT_ONLY,

            )

        remarks = request.data.get("remarks", request.data.get("reason"))

        now = timezone.now()

        bill.status = "Rejected"

        if remarks is not None:

            bill.approval_remarks = remarks

        bill.approved_by = request.user

        bill.approved_at = now

        history = list(bill.approval_history or [])

        history.append(

            {

                "at": now.isoformat(),

                "by": getattr(request.user, "name", None) or str(request.user),

                "action": "Rejected",

                "remarks": remarks,

            }

        )

        bill.approval_history = history

        bill.save(

            update_fields=[

                "status", "approval_remarks", "approved_by", "approved_at",

                "approval_history", "updated_at",

            ]

        )

        self.write_audit("reject", bill, description=remarks)

        return Response(self.get_serializer(bill).data)



    @action(detail=True, methods=["get"], url_path="matching")

    def matching(self, request, pk=None):

        """Live PO/GRN comparison for this vendor bill (no DB writes)."""

        bill = self.get_object()

        return Response(

            services.compute_vendor_bill_matching(request.client_id, bill)

        )



    @action(detail=True, methods=["post"], url_path="recheck-matching")

    @transaction.atomic

    def recheck_matching(self, request, pk=None):

        """Recompute the comparison and persist it on ``match_result``.

        A bill still awaiting approval also takes the result as its match
        status; a decided bill keeps the status it was decided on.
        """
        bill = self.get_object()
        result = services.compute_vendor_bill_matching(request.client_id, bill)
        bill.match_result = result
        update_fields = ["match_result", "updated_at"]
        if bill.status in VENDOR_BILL_PENDING_STATUSES:
            bill.match_status = "Matched" if result.get("overallStatus") == "Match" else "Mismatch"
            update_fields.append("match_status")
        bill.save(update_fields=update_fields)
        return Response(result)

    @action(detail=True, methods=["get"], url_path="approval-history")

    def approval_history(self, request, pk=None):

        bill = self.get_object()

        return Response({"history": bill.approval_history or []})



    @action(detail=False, methods=["post"], url_path="agency-match")

    @transaction.atomic

    def agency_match(self, request):

        """Unified Agency Bill Match action: Original PO + Final Job Work PO + Vendor Invoice.

        

        Handles preview, save_draft, validate, submit_approval, approve, and reject.

        Enforces duplicate invoice prevention, 3-document reconciliation, and payable generation.

        """

        po_id = request.data.get("purchaseOrderId") or request.data.get("poId")

        if not po_id:

            raise ValidationFailed("purchaseOrderId is required.", field_errors={"purchaseOrderId": ["Required."]})



        po = PurchaseOrder.objects.filter(client_id=request.client_id, pk=po_id, deleted_at__isnull=True).first()

        if not po:

            raise ValidationFailed("Purchase Order not found.", field_errors={"purchaseOrderId": ["Not found."]})



        req_action = request.data.get("action", "preview")

        bill_id = request.data.get("billId") or request.data.get("vendorBillId")

        inv_number = (request.data.get("vendorBillNumber") or "").strip()

        inv_date = request.data.get("billDate") or timezone.now().date().isoformat()

        qty = round4(D(str(request.data.get("qty") or 0)))

        rate = round2(D(str(request.data.get("rate") or 0)))

        gst_pct = round2(D(str(request.data.get("gstPct") or 5)))

        remarks = request.data.get("remarks", "")

        reason = request.data.get("reason", remarks)



        # Find or initialize bill

        bill = None

        if bill_id:

            bill = VendorBill.objects.filter(client_id=request.client_id, pk=bill_id, deleted_at__isnull=True).first()



        if req_action in ["save_draft", "validate", "submit_approval", "approve"]:

            if not inv_number:

                raise ValidationFailed("Vendor Invoice Number is required.", field_errors={"vendorBillNumber": ["Required."]})



            # Enforce duplicate invoice submission prevention

            dup_qs = VendorBill.objects.filter(

                client_id=request.client_id,

                party_id=po.party_id,

                vendor_bill_number=inv_number,

                deleted_at__isnull=True,

            )

            if bill:

                dup_qs = dup_qs.exclude(pk=bill.pk)

            if dup_qs.exists():

                raise ValidationFailed(

                    f"A vendor bill with invoice number '{inv_number}' already exists for this agency.",

                    field_errors={"vendorBillNumber": ["Duplicate invoice number."]},

                )



            taxable = round2(qty * rate)

            gst_amount = round2(taxable * gst_pct / Decimal("100"))

            total = round2(taxable + gst_amount)



            item = po.line_items.filter(deleted_at__isnull=True).first()

            item_obj = item.item if item else None



            if bill is None:

                bill = VendorBill.objects.create(

                    client=po.client,

                    vendor_bill_number=inv_number,

                    bill_date=inv_date,

                    party=po.party,

                    party_name=po.party_name or (po.party.name if po.party else ""),

                    purchase_order=po,

                    gst_pct=gst_pct,

                    subtotal=taxable,

                    gst_amount=gst_amount,

                    total=total,

                    remarks=remarks,

                    status="Draft" if req_action == "save_draft" else "Pending",

                    created_by=request.user if getattr(request.user, "is_authenticated", False) else None,

                )

                VendorBillLine.objects.create(

                    client=po.client,

                    vendor_bill=bill,

                    line_no=1,

                    item=item_obj,

                    item_name=item.description if item else (item_obj.name if item_obj else "Grey Fabric"),

                    sku=item_obj.sku if item_obj else None,

                    uom=item.uom if item else "Meter",

                    qty=qty,

                    rate=rate,

                    amount=taxable,

                    line_total=total,

                )

            else:

                bill.vendor_bill_number = inv_number

                bill.bill_date = inv_date

                bill.gst_pct = gst_pct

                bill.subtotal = taxable

                bill.gst_amount = gst_amount

                bill.total = total

                if remarks:

                    bill.remarks = remarks

                if req_action == "save_draft":

                    bill.status = "Draft"

                elif bill.status == "Draft":

                    # Leaving draft: validate / submit / approve put it in the pending queue.

                    bill.status = "Pending"

                bill.save()



                line = bill.line_items.filter(deleted_at__isnull=True).first()

                if line:

                    line.qty = qty

                    line.rate = rate

                    line.amount = taxable

                    line.line_total = total

                    line.save()

                else:

                    VendorBillLine.objects.create(

                        client=po.client,

                        vendor_bill=bill,

                        line_no=1,

                        item=item_obj,

                        item_name=item.description if item else "Grey Fabric",

                        qty=qty,

                        rate=rate,

                        amount=taxable,

                        line_total=total,

                    )



        if bill:

            matching = services.compute_vendor_bill_matching(request.client_id, bill)

            bill.match_result = matching

            bill.match_status = "Matched" if matching.get("overallStatus") == "Match" else "Mismatch"

            bill.save(update_fields=["match_result", "match_status", "updated_at"])



            if req_action == "submit_approval":

                bill.status = "Pending Approval"

                history = list(bill.approval_history or [])

                history.append({

                    "at": timezone.now().isoformat(),

                    "by": getattr(request.user, "name", None) or str(request.user),

                    "action": "Submitted for Approval",

                    "remarks": remarks,

                })

                bill.approval_history = history

                bill.save(update_fields=["status", "approval_history", "updated_at"])



            elif req_action == "approve":

                request._create_payable = True
                try:
                    request.data["createPayable"] = True
                except (TypeError, AttributeError, KeyError) as exc:
                    logger.exception("Could not set createPayable on request.data: %s", exc)

                self.kwargs[self.lookup_field] = str(bill.pk)

                appr_res = self.approve(request, pk=bill.pk)

                if getattr(appr_res, "status_code", 200) >= 400:

                    return appr_res

                bill.refresh_from_db()

                if bill.purchase_bill_id is None:

                    try:
                        self.send_for_matching(request, pk=bill.pk)
                        bill.refresh_from_db()
                    except Exception as exc:
                        logger.exception("Failed auto send_for_matching on bill %s: %s", bill.pk, exc)

                bill.refresh_from_db()

                data = self.get_serializer(bill).data

                data["matching"] = services.compute_vendor_bill_matching(request.client_id, bill)

                return Response(data)



            elif req_action == "reject":

                self.kwargs[self.lookup_field] = str(bill.pk)

                return self.reject(request, pk=bill.pk)



            data = self.get_serializer(bill).data

            data["matching"] = matching

            return Response(data)



        # Live preview calculation without creating a DB record

        temp_bill = VendorBill(

            client=po.client,

            vendor_bill_number=inv_number or "PREVIEW",

            bill_date=inv_date,

            party=po.party,

            party_name=po.party_name,

            purchase_order=po,

            gst_pct=gst_pct,

            subtotal=round2(qty * rate),

            gst_amount=round2(round2(qty * rate) * gst_pct / Decimal("100")),

            total=round2(round2(qty * rate) + round2(round2(qty * rate) * gst_pct / Decimal("100"))),

            remarks=remarks,

            status="Draft",

        )

        temp_line = VendorBillLine(

            vendor_bill=temp_bill,

            line_no=1,

            qty=qty,

            rate=rate,

            amount=round2(qty * rate),

            item_name="Grey Fabric",

        )

        temp_bill._preloaded_lines = [temp_line]



        # Use helper

        preview_res = services.compute_vendor_bill_matching(request.client_id, temp_bill)

        return Response({"matching": preview_res})





    @action(detail=True, methods=["post"], url_path="pay")
    @transaction.atomic
    def pay(self, request, pk=None):
        """Pay an approved vendor bill through the normal payment-out flow.

        The same Idempotency-Key replays the first voucher instead of adding
        a second one. Payment progress lives on the payable, not on
        ``bill.status``, so cancelling a payment needs nothing undone here.
        """
        from apps.accounting.models import BankAccount
        from apps.core.exceptions import PermissionDenied
        from apps.core.permissions import has_permission

        if not has_permission(request.user, "record_payment_out"):
            raise PermissionDenied(
                "You do not have permission to record vendor payments.",
                code="record_payment_out",
            )
        try:
            record = idempotency.begin(request, request.client_id, request.path, request.data)
        except idempotency.Replay as replay:
            response = Response(replay.body, status=replay.status)
            response["Idempotency-Replayed"] = "true"
            return response

        # Lock the row: two simultaneous payments must see each other's effect.
        bill = (
            VendorBill.objects.select_for_update(of=("self",))
            .select_related("purchase_bill", "purchase_order")
            .filter(pk=pk, client_id=request.client_id, deleted_at__isnull=True)
            .first()
        )
        if bill is None:
            raise NotFound("Vendor bill not found.")

        self._assert_has_lines(bill)
        workflow = services.vendor_bill_workflow(bill)
        if "pay" not in workflow["actions"]:
            if workflow["stage"] == "paid":
                raise Conflict("This vendor bill is already fully paid.", code=Codes.ALREADY_SETTLED)
            raise Conflict(
                f"Only an approved vendor bill can be paid. This bill is: {workflow['label']}.",
                code="BILL_NOT_PAYABLE",
            )
        po = bill.purchase_order
        if po is not None and po.party_id and bill.party_id not in (po.party_id, po.dyeing_mill_id):
            raise Conflict(
                f"This bill's vendor does not match purchase order {po.po_number}.",
                code="BILL_VENDOR_MISMATCH",
            )

        try:
            amount = round2(D(str(request.data.get("amount") or 0)))
        except ArithmeticError:
            amount = ZERO
        if amount <= ZERO:
            raise ValidationFailed(
                "Enter a valid payment amount greater than zero.",
                field_errors={"amount": ["Must be greater than zero."]},
            )

        mode = request.data.get("mode") or "Bank Transfer"
        if mode not in PAYMENT_MODES:
            raise ValidationFailed(
                f"{mode} is not a valid payment mode.",
                field_errors={"mode": [f"Choose one of: {', '.join(PAYMENT_MODES)}."]},
            )
        payment_date = parse_date(str(request.data.get("date") or "")) or timezone.localdate()
        bank_account = None
        bank_account_id = request.data.get("bankAccountId")
        if bank_account_id:
            bank_account = BankAccount.objects.filter(
                pk=bank_account_id, client_id=request.client_id, deleted_at__isnull=True
            ).first()

        reference_number = (request.data.get("reference") or "").strip() or None
        if reference_number and PaymentOut.objects.filter(
            client_id=request.client_id, party_id=bill.party_id, status="Active",
            reference_number__iexact=reference_number, deleted_at__isnull=True,
        ).exists():
            raise Conflict(
                f"A payment with reference {reference_number} is already recorded for this vendor.",
                code="DUPLICATE_PAYMENT_REFERENCE",
            )

        # A bill approved without one still needs a posted payable to pay into.
        payable = services.ensure_vendor_bill_payable(bill, user=request.user)
        if bill.purchase_bill_id != payable.id:
            bill.purchase_bill = payable
            bill.save(update_fields=["purchase_bill", "updated_at"])

        payment = services.record_payment_out(
            client=request.user.client,
            party=bill.party,
            amount=amount,
            payment_date=payment_date,
            mode=mode,
            bank_account=bank_account,
            reference_number=reference_number,
            notes=request.data.get("remarks") or (
                f"Payment against Vendor Bill {bill.vendor_bill_number}"
                + (f" (PO: {po.po_number})" if po else "")
            ),
            bill=payable,
            user=request.user,
        )

        payable.refresh_from_db()
        bill.purchase_bill = payable
        summary = services.vendor_bill_payment_summary(bill)

        if summary.get("paymentStatus") == "Paid":
            bill.status = "Paid"
            bill.save(update_fields=["status", "purchase_bill", "updated_at"])
        elif summary.get("paymentStatus") == "Partially Paid" and bill.status != "Partially Paid":
            bill.status = "Partially Paid"
            bill.save(update_fields=["status", "purchase_bill", "updated_at"])

        if po:
            po.refresh_from_db()
            all_bills_snapshot = services.vendor_bill_po_snapshot(po)
            po_summary = all_bills_snapshot.get("poSummary", {})
            total_paid = po_summary.get("totalPaidAmount", 0)
            po.amount_paid = D(str(total_paid))
            po.save(update_fields=["amount_paid", "updated_at"])

        self.write_audit(
            "pay", bill,
            description=f"Payment {payment.payment_number} of {amount} against {bill.vendor_bill_number}",
        )
        return idempotency.finish(
            record,
            Response({
                "success": True,
                "message": f"Payment of ₹{amount} recorded against bill {bill.vendor_bill_number}.",
                "paymentId": str(payment.id),
                "paymentNumber": payment.payment_number,
                "amountPaid": float(summary["paid"]),
                "outstandingAmount": float(summary["outstanding"]),
                "paymentStatus": summary["paymentStatus"],
            }),
            payment,
        )

