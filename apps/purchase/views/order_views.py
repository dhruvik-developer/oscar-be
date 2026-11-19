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


class PurchaseOrderViewSet(SalesDocumentViewSet):

    queryset = PurchaseOrder.objects.select_related("project")

    serializer_class = PurchaseOrderSerializer

    audit_entity_type = "PurchaseOrder"

    audit_label_field = "po_number"

    status_field = "status"

    print_title = "Purchase Order"

    filter_map = {

        "vendorId": "party_id",

        "purchaseOrderId": "purchase_order_id",

        "poId": "purchase_order_id",

        "po": "purchase_order_id",

        "vendor_id": "party_id",

        "purchaseType": "purchase_type",

        "projectId": "project_id",

    }

    permission_map = {"read": ["view_purchase"], "write": ["create_purchase_order"]}

    draft_values = ("Draft", "Issued")



    def get_serializer_context(self):

        context = super().get_serializer_context()

        context["include_billed_status"] = self.action == "retrieve"

        return context



    @transaction.atomic
    def perform_create(self, serializer):

        serializer.validated_data["po_number"] = allocate_number(

            self.request.user.client, "PO", serializer.validated_data.get("doc_date")

        )

        instance = super().perform_create(serializer)

        from apps.masters.rate_services import check_po_rate_tolerance

        check_po_rate_tolerance(instance, user=self.request.user)

        # Yarn plan and opening costs of a factory fabric order, saved with it.
        fabric_flow.apply_order_extras(instance, self.request.data, user=self.request.user, creating=True)

        return instance



    @transaction.atomic
    def perform_update(self, serializer):

        instance = super().perform_update(serializer)

        from apps.masters.rate_services import check_po_rate_tolerance

        check_po_rate_tolerance(instance, user=self.request.user)

        fabric_flow.apply_order_extras(instance, self.request.data, user=self.request.user)

        return instance

    # -- factory fabric flows (Factory -> OSCAR, Factory -> Dyeing Mill -> OSCAR)

    def _flow_order(self, pk):
        return self.get_queryset().select_related("party", "dyeing_mill").get(pk=pk)

    def _flow_write(self, request, pk, permission, work, *, status_code=status.HTTP_200_OK, answer=None):
        """Run one fabric-flow write under the idempotency key (a replayed
        Save returns the first response instead of writing twice) and answer
        with the PO's refreshed fabric-flow summary."""
        from apps.core.permissions import require_permission

        require_permission(request.user, permission)
        po = self._flow_order(pk)
        try:
            record = idempotency.begin(request, request.client_id, request.path, request.data)
        except idempotency.Replay as replay:
            response = Response(replay.body, status=replay.status)
            response["Idempotency-Replayed"] = "true"
            return response
        try:
            with transaction.atomic():
                work(po)
                body = (answer or fabric_flow.summary)(self._flow_order(pk))
        except Exception as exc:
            logger.exception("Error executing purchase order flow action for PO %s: %s", pk, exc)
            idempotency.fail(record)
            raise
        return idempotency.finish(record, Response(body, status=status_code))

    @action(detail=False, methods=["get"], url_path="grey-sources")
    def grey_sources(self, request):
        """Grey fabric POs with stock free to send for dyeing."""
        return Response({"results": dyeing_dispatch.grey_sources(request.client_id)})

    @action(detail=True, methods=["get"], url_path="grey-batches")
    def grey_batches(self, request, pk=None):
        return Response({"results": dyeing_dispatch.source_batches(self._flow_order(pk))})

    @action(detail=True, methods=["get"], url_path="dyeing")
    def dyeing_summary(self, request, pk=None):
        """Grey batches, dyeing dispatches and dyed batches of an OSCAR -> mill order."""
        return Response(dyeing_dispatch.summary(self._flow_order(pk)))

    @action(detail=True, methods=["post"], url_path="dyeing/dispatches")
    def dyeing_send(self, request, pk=None):
        return self._flow_write(request, pk, "receive_goods",
                                lambda po: dyeing_dispatch.dispatch_planned(po, user=request.user),
                                status_code=status.HTTP_201_CREATED, answer=dyeing_dispatch.summary)

    @action(detail=True, methods=["post"], url_path=r"dyeing/dispatches/(?P<outward_id>[^/.]+)/returns")
    def dyeing_return(self, request, pk=None, outward_id=None):
        return self._flow_write(request, pk, "receive_goods",
                                lambda po: dyeing_dispatch.receive_return(po, outward_id, request.data, user=request.user),
                                status_code=status.HTTP_201_CREATED, answer=dyeing_dispatch.summary)

    @action(detail=True, methods=["get"], url_path="fabric-flow")
    def fabric_flow_summary(self, request, pk=None):
        """Quantities, yarn, dispatches, mill, receipts & batches, costs and
        discrepancies of a factory fabric order."""
        return Response(fabric_flow.summary(self._flow_order(pk)))

    @action(detail=True, methods=["put"], url_path="fabric-flow/yarn-lines")
    def fabric_flow_yarn(self, request, pk=None):
        return self._flow_write(request, pk, "create_purchase_order",
                                lambda po: fabric_flow.replace_yarn_lines(po, request.data.get("yarnLines") or [], user=request.user))

    @action(detail=True, methods=["post"], url_path="fabric-flow/dispatches")
    def fabric_flow_dispatch(self, request, pk=None):
        return self._flow_write(request, pk, "create_purchase_order",
                                lambda po: fabric_flow.record_dispatch(po, request.data, user=request.user),
                                status_code=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="fabric-flow/mill-processes")
    def fabric_flow_mill(self, request, pk=None):
        return self._flow_write(request, pk, "create_purchase_order",
                                lambda po: fabric_flow.record_mill_process(po, request.data, user=request.user),
                                status_code=status.HTTP_201_CREATED)

    @action(detail=True, methods=["patch"], url_path=r"fabric-flow/mill-processes/(?P<record_id>[^/.]+)")
    def fabric_flow_mill_update(self, request, pk=None, record_id=None):
        return self._flow_write(request, pk, "create_purchase_order",
                                lambda po: fabric_flow.update_mill_process(po, record_id, request.data, user=request.user))

    @action(detail=True, methods=["post"], url_path="fabric-flow/receipts")
    def fabric_flow_receive(self, request, pk=None):
        def work(po):
            receipt = fabric_flow.receive(po, request.data, user=request.user)
            self.write_audit("create", receipt, description=f"Fabric receipt {receipt.grn_number} against {po.po_number}")

        return self._flow_write(request, pk, "receive_goods", work, status_code=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="fabric-flow/expenses")
    def fabric_flow_expense_add(self, request, pk=None):
        return self._flow_write(request, pk, "create_purchase_order",
                                lambda po: fabric_flow.add_expense(po, request.data, user=request.user),
                                status_code=status.HTTP_201_CREATED)

    @action(detail=True, methods=["patch", "delete"], url_path=r"fabric-flow/expenses/(?P<expense_id>[^/.]+)")
    def fabric_flow_expense(self, request, pk=None, expense_id=None):
        if request.method == "DELETE":
            return self._flow_write(request, pk, "create_purchase_order",
                                    lambda po: fabric_flow.delete_expense(po, expense_id, user=request.user))
        return self._flow_write(request, pk, "create_purchase_order",
                                lambda po: fabric_flow.update_expense(po, expense_id, request.data, user=request.user))

    @action(detail=True, methods=["post"], url_path="fabric-flow/discrepancies")
    def fabric_flow_classify(self, request, pk=None):
        return self._flow_write(request, pk, "receive_goods",
                                lambda po: fabric_flow.classify_discrepancy(po, request.data, user=request.user),
                                status_code=status.HTTP_201_CREATED)

    @action(detail=True, methods=["delete"], url_path=r"fabric-flow/discrepancies/(?P<entry_id>[^/.]+)")
    def fabric_flow_release(self, request, pk=None, entry_id=None):
        return self._flow_write(request, pk, "receive_goods",
                                lambda po: fabric_flow.release_discrepancy(
                                    po, entry_id, user=request.user, reason=request.query_params.get("reason")))

    @action(detail=True, methods=["post"], url_path="fabric-flow/allocate-costs")
    def fabric_flow_allocate(self, request, pk=None):
        return self._flow_write(request, pk, "create_purchase_order",
                                lambda po: fabric_flow.allocate_costs(po, user=request.user))



    @action(detail=True, methods=["get"], url_path="rate-check")

    def rate_check(self, request, pk=None):

        order = self.get_object()

        from apps.masters.rate_services import evaluate_po_rates

        return Response(evaluate_po_rates(order))



    @action(detail=True, methods=["post"], url_path="submit-for-approval")

    @transaction.atomic

    def submit_for_approval(self, request, pk=None):

        from apps.core import approval_services

        fabric_flow.require_dyeing_details(self.get_object())

        res = self._transition(request, from_statuses=("Draft", "Issued"), to_status="Pending Approval")

        order = PurchaseOrder.objects.get(pk=self.get_object().pk)

        approval_services.create_approval_request(

            client_id=order.client_id,

            document_type="purchase_order",

            document_id=order.id,

            document_number=order.po_number,

            requested_by=request.user,

            reason=request.data.get("remarks") or f"Purchase Order {order.po_number} submitted for approval",

            force=True,

        )

        return res



    @action(detail=True, methods=["post"])

    @transaction.atomic

    def approve(self, request, pk=None):

        from apps.core.permissions import require_permission, PermissionDenied

        from apps.core.models import ApprovalRequest

        from apps.core.approval_services import assert_not_pending_approval

        order = PurchaseOrder.objects.select_for_update().get(pk=self.get_object().pk)

        assert_not_pending_approval(order.client_id, "purchase_order_rate", order.id, "approve this purchase order")

        if order.created_by_id and order.created_by_id == getattr(request.user, "id", None) and not getattr(request.user, "is_superuser", False):

            raise PermissionDenied(

                "Separation of duties: You cannot approve a Purchase Order you created.",

                code="SELF_APPROVAL_NOT_PERMITTED",

            )

        try:

            require_permission(request.user, "approve_purchase_order")

        except PermissionDenied:

            require_permission(request.user, "manage_approvals", message="You do not have permission to approve Purchase Orders.")



        res = self._transition(request, from_statuses=("Pending Approval",), to_status="Approved")
        dyeing_dispatch.dispatch_planned(order, user=request.user)

        appr_req = ApprovalRequest.objects.filter(

            client_id=order.client_id,

            document_type="purchase_order",

            document_id=str(order.id),

            status="Pending",

        ).first()

        if appr_req:

            appr_req.status = "Approved"

            appr_req.decided_by = request.user

            appr_req.decided_at = timezone.now()

            appr_req.comments = request.data.get("remarks", "")

            appr_req.save(update_fields=["status", "decided_by", "decided_at", "comments"])

        return res



    @action(detail=True, methods=["post"])

    @transaction.atomic

    def reject(self, request, pk=None):

        from apps.core.permissions import require_permission, PermissionDenied

        from apps.core.models import ApprovalRequest

        try:

            require_permission(request.user, "approve_purchase_order")

        except PermissionDenied:

            require_permission(request.user, "manage_approvals", message="You do not have permission to reject Purchase Orders.")



        order = PurchaseOrder.objects.select_for_update().get(pk=self.get_object().pk)

        res = self._transition(request, from_statuses=("Pending Approval",), to_status="Rejected")

        appr_req = ApprovalRequest.objects.filter(

            client_id=order.client_id,

            document_type="purchase_order",

            document_id=str(order.id),

            status="Pending",

        ).first()

        if appr_req:

            appr_req.status = "Rejected"

            appr_req.decided_by = request.user

            appr_req.decided_at = timezone.now()

            appr_req.comments = request.data.get("remarks", "")

            appr_req.save(update_fields=["status", "decided_by", "decided_at", "comments"])

        return res



    @action(detail=True, methods=["post"], url_path="return-for-correction")

    @transaction.atomic

    def return_correction(self, request, pk=None):

        from apps.core.permissions import require_permission, PermissionDenied

        from apps.core.models import ApprovalRequest

        try:

            require_permission(request.user, "approve_purchase_order")

        except PermissionDenied:

            require_permission(request.user, "manage_approvals", message="You do not have permission to return Purchase Orders for correction.")



        order = PurchaseOrder.objects.select_for_update().get(pk=self.get_object().pk)

        res = self._transition(request, from_statuses=("Pending Approval",), to_status="Draft")

        appr_req = ApprovalRequest.objects.filter(

            client_id=order.client_id,

            document_type="purchase_order",

            document_id=str(order.id),

            status="Pending",

        ).first()

        if appr_req:

            appr_req.status = "Returned"

            appr_req.decided_by = request.user

            appr_req.decided_at = timezone.now()

            appr_req.comments = request.data.get("remarks", "")

            appr_req.save(update_fields=["status", "decided_by", "decided_at", "comments"])

        return res



    def _transition(self, request, *, from_statuses, to_status):

        order = PurchaseOrder.objects.select_for_update().get(pk=self.get_object().pk)

        if order.status == to_status:

            return Response(self.get_serializer(order).data)

        if order.status not in from_statuses:

            raise Conflict(

                f"{order.po_number} is {order.status}; it cannot move to {to_status}.",

                code=Codes.DRAFT_ONLY,

            )

        if not order.line_items.filter(deleted_at__isnull=True).exists():

            raise ValidationFailed(

                "Add at least one line before submitting the purchase order.",

                field_errors={"lineItems": ["Add at least one line."]},

            )

        previous = order.status

        order.status = to_status

        order.save(update_fields=["status", "updated_at"])

        self.write_audit(

            "status_change", order,

            description=f"{previous} -> {to_status}" + (f": {request.data.get('remarks')}" if request.data.get("remarks") else ""),

        )

        return Response(self.get_serializer(order).data)



    @action(detail=True, methods=["get"], url_path="billed-status")

    def billed_status(self, request, pk=None):

        """``getPoBilledStatus`` moved server-side (api.md §6.2)."""

        return Response(services.po_billed_status(self.get_object()))



    @action(detail=True, methods=["post"])

    def cancel(self, request, pk=None):

        order = self.get_object()

        if order.status == "Cancelled":

            raise Conflict("This order is already cancelled.", code=Codes.ALREADY_CANCELLED)



        # api.md §6.2 -- a PO with any active bill cannot be cancelled.

        services.assert_po_cancellable(order)



        reason = request.data.get("reason")

        order.status = "Cancelled"

        order.cancelled_at = timezone.now()

        order.cancelled_by = request.user

        order.cancellation_reason = reason

        order.save()

        self.write_audit("cancel", order, description=reason)

        return Response(self.get_serializer(order).data)



    @action(detail=True, methods=["post"], url_path="convert-to-bill")

    @transaction.atomic

    def convert_to_bill(self, request, pk=None):

        order = self.get_object()

        if order.status == "Cancelled":

            raise Conflict("This order is cancelled.", code=Codes.ALREADY_CANCELLED)



        bill = _clone_document(

            order,

            PurchaseBill,

            {"purchase_order": order, "status": "Draft", "location": order.location},

            number_field="bill_number",

            series="BILL",

            line_model_name="PurchaseBillLine",

            line_fk="purchase_bill",

            line_filter=lambda line: D(line.qty) > D(line.billed_qty),

            line_overrides=lambda line: {

                "qty": D(line.qty) - D(line.billed_qty),

                "purchase_order_line": line,

            },

        )

        self.write_audit("convert", order, description=f"Bill {bill.bill_number} created")

        return Response(

            PurchaseBillSerializer(bill, context=self.get_serializer_context()).data,

            status=status.HTTP_201_CREATED,

        )



    @action(detail=True, methods=["get"], url_path="production-lifecycle")

    def production_lifecycle(self, request, pk=None):

        """The 8-stage Production Lifecycle of a Vendor A Production PO (read only).



        Shared with the PMS project view so both show the same state."""

        from apps.pms.lifecycle_services import production_lifecycle



        po = self.get_object()

        if po.purchase_type != PURCHASE_TYPE_VENDOR_A:

            return Response({"purchaseOrderId": str(po.id), "applicable": False, "stages": []})

        return Response({"applicable": True, **production_lifecycle(po)})





    @action(detail=True, methods=["post"], url_path="start-production")

    @transaction.atomic

    def start_production(self, request, pk=None):

        po = self.get_object()

        from apps.pms.models import ProductionInstruction, Project

        from apps.hrms.models import Employee

        from apps.masters.models import Agency

        from apps.pms.pipeline_services import sync_project_pipeline_stages



        # Only a Vendor A production PO enters the production / daily-entry

        # cycle; a ready grey stock purchase is received through a GRN.

        if po.purchase_type != PURCHASE_TYPE_VENDOR_A:

            raise Conflict(

                f"{po.po_number} is a {po.purchase_type}; only a Vendor A Production PO can start production. "

                "Receive ready grey fabric through a GRN instead.",

                code="WRONG_PURCHASE_TYPE",

            )

        if po.status not in ("Approved", "In Production"):

            raise Conflict(

                f"Approve {po.po_number} before starting production (it is {po.status}).",

                code=Codes.DRAFT_ONLY,

            )



        po.status = "In Production"

        po.save(update_fields=["status", "updated_at"])



        existing_pi = ProductionInstruction.objects.filter(

            client_id=po.client_id,

            order_reference=po.po_number,

            deleted_at__isnull=True,

        ).first()



        if existing_pi:

            if existing_pi.status in ("Draft", "Not Started", "Pending"):

                existing_pi.status = "Running"

                existing_pi.save(update_fields=["status", "updated_at"])

            instruction = existing_pi

        else:

            first_line = po.line_items.first()

            total_qty = sum((fl.qty for fl in po.line_items.all()), Decimal("0.00"))

            fabric_name = (

                first_line.item.name

                if (first_line and first_line.item)

                else getattr(po, "notes", "Grey Fabric") or "Grey Fabric"

            )

            unit_rate = first_line.rate if first_line else Decimal("0.00")



            agency_name = po.party_name or (po.party.name if po.party else "Production Agency")

            agency_obj = Agency.objects.filter(

                client_id=po.client_id,

                name__iexact=agency_name.strip(),

                deleted_at__isnull=True,

            ).first()



            employee = Employee.objects.filter(

                client_id=po.client_id,

                status__in=["Active", "Probation"],

                deleted_at__isnull=True,

            ).first()



            instruction = ProductionInstruction.objects.create(

                client=po.client,

                instruction_number=allocate_number(po.client, "PROD"),

                agency=agency_obj,

                agency_name=agency_name,

                order_reference=po.po_number,

                order_meter=total_qty,

                tentative_rate=unit_rate,

                agreed_rate=unit_rate,

                rate_uom="Meter",

                expected_start_date=po.expected_date or timezone.localdate(),

                pi_date=po.doc_date or timezone.localdate(),

                fabric=fabric_name,

                process_type="Processing",

                employee=employee,

                status="Running",

                notes=f"Auto-generated from Purchase Order {po.po_number}",

                created_by=request.user if getattr(request.user, "is_authenticated", False) else None,

            )



        project = po.project

        if project is None and po.reference_number:

            project = Project.objects.filter(

                client_id=po.client_id,

                code=po.reference_number,

                deleted_at__isnull=True,

            ).first()

        if project:

            sync_project_pipeline_stages(project)



        self.write_audit("start_production", po, description=f"Production Instruction {instruction.instruction_number} created.")



        return Response({

            "success": True,

            "message": f"Production Instruction {instruction.instruction_number} generated for {instruction.agency_name}. Status moved to In Production.",

            "instructionId": str(instruction.id),

            "instructionNumber": instruction.instruction_number,

            "status": po.status,

            "po": self.get_serializer(po).data,

        })



    @action(detail=True, methods=["get", "post"], url_path="production-instruction")

    @transaction.atomic

    def production_instruction(self, request, pk=None):

        po = self.get_object()

        from apps.pms.models import ProductionInstruction, Project

        from apps.hrms.models import Employee

        from apps.masters.models import Agency

        from apps.core.numbering import peek_next, allocate_number

        from apps.pms.production_services import instruction_completion

        from apps.pms.pipeline_services import sync_project_pipeline_stages



        if po.purchase_type != PURCHASE_TYPE_VENDOR_A:

            raise Conflict(

                f"{po.po_number} is {po.purchase_type}; Production Instructions apply only to Vendor A Production POs.",

                code="WRONG_PURCHASE_TYPE",

            )



        lines = po.line_items.filter(deleted_at__isnull=True)

        total_qty = sum((D(l.qty) for l in lines), ZERO)

        total_amount = sum((D(l.amount or (l.qty * l.rate) or 0) for l in lines), ZERO)

        fabric_names = [

            (l.item.name if l.item else (l.description or "Grey Fabric"))

            for l in lines

        ]

        unique_fabrics = list(dict.fromkeys(fabric_names))

        fabric_name = ", ".join(unique_fabrics) if unique_fabrics else (po.notes or "Grey Fabric")

        first_line = lines.first()

        unit_rate = (total_amount / total_qty) if total_qty > 0 else (first_line.rate if first_line else ZERO)

        vendor_name = po.party_name or (po.party.name if po.party else "")



        all_pis = list(ProductionInstruction.objects.filter(

            client_id=po.client_id,

            order_reference=po.po_number,

            deleted_at__isnull=True,

        ).order_by("created_at"))



        req_pi_id = request.GET.get("instructionId")

        if req_pi_id:

            existing_pi = next((p for p in all_pis if str(p.id) == str(req_pi_id)), None)

            if not existing_pi:

                existing_pi = ProductionInstruction.objects.filter(

                    client_id=po.client_id, id=req_pi_id, deleted_at__isnull=True

                ).first()

        else:

            existing_pi = all_pis[-1] if all_pis else None



        if request.method == "GET":

            instructions_list = []

            for pi_item in all_pis:

                c = instruction_completion(pi_item)

                sup = pi_item.supervisor or pi_item.employee

                instructions_list.append({

                    "id": str(pi_item.id),

                    "instructionNumber": pi_item.instruction_number,

                    "status": pi_item.status,

                    "fabric": pi_item.fabric,

                    "orderMeter": pi_item.order_meter,

                    "supervisorId": str(sup.id) if sup else None,

                    "supervisorName": getattr(sup, "name", "") if sup else "",

                    "startDate": pi_item.expected_start_date or pi_item.pi_date,

                    "expectedCompletionDate": pi_item.expected_completion_date,

                    "completion": {

                        "poQuantity": pi_item.order_meter,

                        "totalProduced": c["totalProduced"],

                        "acceptedQty": c["acceptedQty"],

                        "rejectedQty": c["rejectedQty"],

                        "reprocessQty": c.get("reprocessQty", 0),

                        "balanceQty": c.get("balanceQty", 0),

                        "completionPercentage": c.get("completionPercentage", 0),

                        "finalAmount": c["finalAmount"],

                    },

                    "entriesCount": len(c.get("rows", [])),

                })



            pi_data = None

            if existing_pi:

                comp = instruction_completion(existing_pi)

                supervisor = existing_pi.supervisor or existing_pi.employee

                pi_data = {

                    "id": str(existing_pi.id),

                    "instructionNumber": existing_pi.instruction_number,

                    "status": existing_pi.status,

                    "employeeId": str(existing_pi.employee_id) if existing_pi.employee_id else (str(supervisor.id) if supervisor else None),

                    "supervisorId": str(supervisor.id) if supervisor else None,

                    "supervisorName": getattr(supervisor, "name", "") if supervisor else "",

                    "startDate": existing_pi.expected_start_date or existing_pi.pi_date,

                    "expectedCompletionDate": existing_pi.expected_completion_date,

                    "remarks": existing_pi.notes or "",

                    "orderMeter": existing_pi.order_meter,

                    "fabric": existing_pi.fabric,

                    "agencyName": existing_pi.agency_name,

                    "finalPoNumber": getattr(existing_pi.final_po, "po_number", None) if existing_pi.final_po else None,

                    "finalPoId": str(existing_pi.final_po_id) if existing_pi.final_po_id else None,

                    "isVerified": existing_pi.status in ("Verified", "PO Pending", "PO Created", "Closed"),

                    "completion": {

                        "poQuantity": existing_pi.order_meter,

                        "totalProduced": comp["totalProduced"],

                        "acceptedQty": comp["acceptedQty"],

                        "rejectedQty": comp["rejectedQty"],

                        "reprocessQty": comp.get("reprocessQty", 0),

                        "balanceQty": comp.get("balanceQty", 0),

                        "completionPercentage": comp.get("completionPercentage", 0),

                        "finalAmount": comp["finalAmount"],

                    },

                    "entries": comp.get("rows", []),

                }



            preview_num = (

                existing_pi.instruction_number

                if existing_pi

                else peek_next(po.client, "PROD")

            )



            return Response({

                "po": {

                    "id": str(po.id),

                    "poNumber": po.po_number,

                    "purchaseType": po.purchase_type,

                    "status": po.status,

                    "vendorName": vendor_name,

                    "fabricName": fabric_name,

                    "poQuantity": total_qty,

                    "rate": round2(unit_rate),

                    "poAmount": round2(total_amount),

                    "lineCount": lines.count(),

                    "lines": [

                        {

                            "id": str(l.id),

                            "name": l.item.name if l.item else (l.description or "Grey Fabric"),

                            "qty": l.qty,

                            "rate": l.rate,

                            "amount": l.amount,

                        }

                        for l in lines

                    ],

                },

                "nextInstructionNumber": preview_num,

                "instruction": pi_data,

                "instructions": instructions_list,

            })



        # POST: Save Draft or Submit for Approval

        from django.utils.dateparse import parse_date

        data = request.data or {}

        action_type = data.get("action", "save_draft")

        supervisor_id = data.get("supervisorId")

        raw_start_date = data.get("startDate")

        raw_completion_date = data.get("expectedCompletionDate")

        start_date = parse_date(str(raw_start_date)) if raw_start_date else None

        expected_completion_date = parse_date(str(raw_completion_date)) if raw_completion_date else None

        remarks = data.get("remarks", "")



        field_errors = {}

        if not supervisor_id:

            field_errors["supervisorId"] = ["Assigned Supervisor is required."]

        if not start_date:

            field_errors["startDate"] = ["Production Start Date is required."]

        if not expected_completion_date:

            field_errors["expectedCompletionDate"] = ["Expected Completion Date is required."]



        if field_errors:

            raise ValidationFailed("Please fill all required fields.", field_errors=field_errors)



        supervisor = Employee.objects.filter(

            client_id=po.client_id,

            id=supervisor_id,

            deleted_at__isnull=True,

        ).first()

        if not supervisor:

            raise ValidationFailed("Assigned Supervisor not found or invalid.", field_errors={"supervisorId": ["Invalid supervisor."]})



        agency_name = po.party_name or (po.party.name if po.party else "Production Agency")

        agency_obj = Agency.objects.filter(

            client_id=po.client_id,

            name__iexact=agency_name.strip(),

            deleted_at__isnull=True,

        ).first()



        if existing_pi:

            instruction = existing_pi

            instruction.supervisor = supervisor

            instruction.employee = supervisor

            instruction.expected_start_date = start_date

            instruction.expected_completion_date = expected_completion_date

            instruction.notes = remarks

            instruction.order_meter = total_qty

            instruction.fabric = fabric_name

            instruction.tentative_rate = unit_rate

            instruction.agreed_rate = unit_rate

            instruction.agreed_job_rate = unit_rate

            instruction.updated_by = request.user if getattr(request.user, "is_authenticated", False) else None



            if action_type == "submit_approval" and instruction.status in ("Draft", "Not Started"):

                instruction.status = "Approved"



            instruction.save()

            audit_action = "production_instruction_submit" if action_type == "submit_approval" else "production_instruction_update"

            self.write_audit(audit_action, po, description=f"Production Instruction {instruction.instruction_number} updated ({action_type}).")

        else:

            initial_status = "Approved" if action_type == "submit_approval" else "Draft"

            instruction = ProductionInstruction.objects.create(

                client=po.client,

                instruction_number=allocate_number(po.client, "PROD"),

                agency=agency_obj,

                agency_name=agency_name,

                order_reference=po.po_number,

                order_meter=total_qty,

                tentative_rate=unit_rate,

                agreed_rate=unit_rate,

                agreed_job_rate=unit_rate,

                rate_uom="Meter",

                expected_start_date=start_date,

                expected_completion_date=expected_completion_date,

                pi_date=start_date or timezone.localdate(),

                fabric=fabric_name,

                process_type="Processing",

                employee=supervisor,

                supervisor=supervisor,

                status=initial_status,

                notes=remarks,

                created_by=request.user if getattr(request.user, "is_authenticated", False) else None,

                updated_by=request.user if getattr(request.user, "is_authenticated", False) else None,

            )

            audit_action = "production_instruction_create_and_submit" if action_type == "submit_approval" else "production_instruction_create_draft"

            self.write_audit(audit_action, po, description=f"Production Instruction {instruction.instruction_number} created ({action_type}).")



        if action_type == "submit_approval":

            if po.status in ("Approved", "Draft"):

                po.status = "In Production"

                po.save(update_fields=["status", "updated_at"])

                self.write_audit("status_change", po, description=f"Moved to In Production via Instruction {instruction.instruction_number}")



            project = po.project

            if project is None and po.reference_number:

                project = Project.objects.filter(

                    client_id=po.client_id,

                    code=po.reference_number,

                    deleted_at__isnull=True,

                ).first()

            if project:

                sync_project_pipeline_stages(project)



        comp = instruction_completion(instruction)

        return Response({

            "success": True,

            "message": f"Production Instruction {instruction.instruction_number} {'submitted for approval' if action_type == 'submit_approval' else 'saved as draft'}.",

            "instruction": {

                "id": str(instruction.id),

                "instructionNumber": instruction.instruction_number,

                "status": instruction.status,

                "supervisorId": str(supervisor.id),

                "supervisorName": supervisor.name,

                "startDate": instruction.expected_start_date or instruction.pi_date,

                "expectedCompletionDate": instruction.expected_completion_date,

                "remarks": instruction.notes or "",

                "orderMeter": instruction.order_meter,

                "finalPoNumber": getattr(instruction.final_po, "po_number", None) if instruction.final_po else None,

                "finalPoId": str(instruction.final_po_id) if instruction.final_po_id else None,

                "isVerified": instruction.status in ("Verified", "PO Pending", "PO Created", "Closed"),

                "completion": {

                    "totalProduced": comp["totalProduced"],

                    "acceptedQty": comp["acceptedQty"],

                    "rejectedQty": comp["rejectedQty"],

                    "shortageQty": comp["shortageQty"],

                    "finalAmount": comp["finalAmount"],

                },

            },

            "po": self.get_serializer(po).data,

        })



    @action(detail=True, methods=["get", "post", "delete"], url_path="qc-inspection")

    def qc_inspection(self, request, pk=None):

        po = self.get_object()

        from apps.pms.models import ProductionInstruction, DailyProductionEntry

        from apps.inventory.models import QCInspection

        from apps.hrms.models import Employee

        from apps.core.numbering import allocate_number

        from datetime import timedelta

        from django.utils.dateparse import parse_date



        lines = po.line_items.filter(deleted_at__isnull=True)

        total_po_qty = sum((D(l.qty) for l in lines), ZERO)

        fabric_names = [(l.item.name if l.item else (l.description or "Grey Fabric")) for l in lines]

        unique_fabrics = list(dict.fromkeys(fabric_names))

        fabric_name = ", ".join(unique_fabrics) if unique_fabrics else (po.notes or "Grey Fabric")

        vendor_name = po.party_name or (po.party.name if po.party else "")



        all_pis = list(ProductionInstruction.objects.filter(

            client_id=po.client_id,

            order_reference=po.po_number,

            deleted_at__isnull=True,

        ).order_by("created_at"))



        instruction_id = request.GET.get("instructionId") or (request.data.get("instructionId") if request.method == "POST" else None)

        active_pi = None

        if instruction_id:

            active_pi = next((p for p in all_pis if str(p.id) == str(instruction_id)), None)

            if not active_pi:

                active_pi = ProductionInstruction.objects.filter(

                    client_id=po.client_id, id=instruction_id, deleted_at__isnull=True

                ).first()

        if not active_pi and all_pis:

            active_pi = all_pis[-1]



        if active_pi:

            total_produced = DailyProductionEntry.objects.filter(

                instruction=active_pi,

                deleted_at__isnull=True,

            ).exclude(status="Cancelled").aggregate(value=Sum("meters"))["value"] or ZERO

        else:

            total_produced = ZERO



        if request.method == "DELETE":

            entry_id = request.GET.get("entryId") or request.GET.get("entry_id") or request.data.get("entryId") or request.data.get("entry_id")

            if not entry_id:

                raise ValidationFailed("Entry ID required for deletion.")

            qc_entry = QCInspection.objects.filter(

                client_id=po.client_id,

                id=entry_id,

                deleted_at__isnull=True,

            ).first()

            if not qc_entry:

                raise ValidationFailed("QC Inspection record not found.")

            if qc_entry.status not in ("Draft", "Pending"):

                raise Conflict("Only Draft QC entries can be deleted.")

            qc_entry.deleted_at = timezone.now()

            qc_entry.save(update_fields=["deleted_at", "updated_at"])

            return Response({"success": True, "message": "QC entry deleted successfully."})



        created_qc_entry = None

        if request.method == "POST":

            data = request.data or {}

            action_type = data.get("action") or ("save_draft" if data.get("status") == "Draft" else "submit")

            entry_id = data.get("entryId") or data.get("entry_id")



            if action_type == "submitDraft" and entry_id:

                qc_entry = QCInspection.objects.filter(

                    client_id=po.client_id, id=entry_id, deleted_at__isnull=True

                ).first()

                if not qc_entry:

                    raise ValidationFailed("QC Inspection entry not found.")

                qc_entry.status = "Approved"

                if request.user.is_authenticated:

                    qc_entry.updated_by = request.user

                qc_entry.save(update_fields=["status", "updated_at"])

                return Response({"success": True, "message": f"QC Entry {qc_entry.inspection_number} submitted successfully."})



            raw_date = data.get("inspectionDate") or data.get("inspection_date")

            insp_date = parse_date(str(raw_date)) if raw_date else timezone.localdate()

            if not insp_date:

                insp_date = timezone.localdate()

            if insp_date > timezone.localdate():

                raise ValidationFailed("Future inspection dates are not permitted.", field_errors={"inspectionDate": ["Future dates not allowed."]})



            inspector_id = data.get("inspectorId") or data.get("inspector_id") or data.get("inspector")

            if not inspector_id:

                raise ValidationFailed("Inspector / QC Incharge is required.", field_errors={"inspectorId": ["Select an inspector."]})



            inspector_emp = Employee.objects.filter(

                client_id=po.client_id, id=inspector_id, deleted_at__isnull=True

            ).first()

            inspector_user = None

            if not inspector_emp:

                from apps.accounts.models import User

                inspector_user = User.objects.filter(client_id=po.client_id, id=inspector_id).first()

                if not inspector_user:

                    raise ValidationFailed("Selected inspector does not exist.", field_errors={"inspectorId": ["Invalid inspector."]})

            else:

                inspector_user = getattr(inspector_emp, "user", None)



            inspected_qty = D(data.get("inspectedQty") or data.get("inspected_quantity") or 0)

            if inspected_qty <= ZERO:

                raise ValidationFailed("Inspected Quantity must be greater than zero.", field_errors={"inspectedQty": ["Enter a valid inspected quantity."]})



            accepted_qty = D(data.get("acceptedQty") or data.get("accepted_quantity") or 0)

            rejected_qty = D(data.get("rejectedQty") or data.get("rejected_quantity") or 0)

            reprocess_qty = D(data.get("reprocessQty") or data.get("reprocess_quantity") or 0)

            hold_qty = D(data.get("holdQty") or data.get("hold_quantity") or 0)



            if accepted_qty < ZERO or rejected_qty < ZERO or reprocess_qty < ZERO or hold_qty < ZERO:

                raise ValidationFailed("Quantity result values cannot be negative.")



            reconciled = accepted_qty + rejected_qty + reprocess_qty + hold_qty

            if reconciled != inspected_qty:

                raise ValidationFailed(

                    f"Quantity reconciliation mismatch: breakdown sum ({reconciled} m) does not equal Inspected Quantity ({inspected_qty} m).",

                    field_errors={"acceptedQty": ["Breakdown sum must match Inspected Quantity."]}

                )



            existing_qc = QCInspection.objects.filter(

                client_id=po.client_id,

                deleted_at__isnull=True,

            ).filter(

                Q(purchase_order=po) | (Q(production_instruction=active_pi) if active_pi else Q(source_document_number=po.po_number))

            ).exclude(status="Draft")



            already_inspected = existing_qc.aggregate(val=Sum("total_inspected_qty"))["val"] or ZERO

            eligible_for_qc = max(ZERO, D(total_produced) - D(already_inspected))



            if action_type != "save_draft" and inspected_qty > eligible_for_qc and total_produced > ZERO:

                raise ValidationFailed(

                    f"Inspected quantity ({inspected_qty} m) exceeds pending produced quantity eligible for QC ({eligible_for_qc} m).",

                    field_errors={"inspectedQty": [f"Cannot inspect more than eligible pending production ({eligible_for_qc} m)."]}

                )



            recent = QCInspection.objects.filter(

                client_id=po.client_id,

                purchase_order=po,

                total_inspected_qty=inspected_qty,

                created_at__gte=timezone.now() - timedelta(seconds=5),

            ).first()

            if recent:

                raise Conflict("A duplicate QC entry was just submitted. Please wait a moment.")



            is_draft = (action_type == "save_draft")

            inspection_number = allocate_number(po.client, "QC", insp_date)



            status_val = "Draft" if is_draft else "Approved"

            if data.get("status") and data.get("status") not in ("Draft",):

                status_val = "Approved"



            created_qc_entry = QCInspection.objects.create(

                client=po.client,

                purchase_order=po,

                production_instruction=active_pi,

                source_document_type="PO",

                source_document_id=po.id,

                source_document_number=po.po_number,

                inspection_number=inspection_number,

                inspection_date=insp_date,

                inspector=inspector_user if inspector_user else (request.user if request.user.is_authenticated else None),

                inspector_employee=inspector_emp,

                status=status_val,

                total_received_qty=inspected_qty,

                total_inspected_qty=inspected_qty,

                passed_qty=accepted_qty,

                rejected_qty=rejected_qty,

                reprocess_qty=reprocess_qty,

                hold_qty=hold_qty,

                inspection_type=data.get("inspectionType") or data.get("inspection_type") or "Grey Fabric QC",

                inspection_method=data.get("inspectionMethod") or data.get("inspection_method") or "Visual + Measurement",

                gsm=data.get("gsm") or None,

                width=data.get("width") or None,

                defects_found=data.get("defectsFound") or data.get("defects_found") or "",

                quality_grade=data.get("qualityGrade") or data.get("quality_grade") or "Grade A",

                notes=data.get("remarks") or "",

                photos=data.get("photos") or [],

                created_by=request.user if request.user.is_authenticated else None,

            )



            self.write_audit("qc_inspection_create", po, description=f"QC Entry {inspection_number} created ({'Draft' if is_draft else 'Submitted'}).")



        qc_qs = QCInspection.objects.filter(

            client_id=po.client_id,

            deleted_at__isnull=True,

        ).filter(

            Q(purchase_order=po) | (Q(production_instruction=active_pi) if active_pi else Q(source_document_number=po.po_number))

        ).order_by("-inspection_date", "-created_at")



        valid_qc = [q for q in qc_qs if q.status != "Draft"]

        tot_inspected = sum((D(q.total_inspected_qty) for q in valid_qc), ZERO)

        acc_qty = sum((D(q.passed_qty) for q in valid_qc), ZERO)

        rej_qty = sum((D(q.rejected_qty) for q in valid_qc), ZERO)

        rep_qty = sum((D(q.reprocess_qty) for q in valid_qc), ZERO)

        hld_qty = sum((D(q.hold_qty) for q in valid_qc), ZERO)



        pending_for_qc = max(ZERO, D(total_produced) - D(tot_inspected))

        completion_pct = round(float((tot_inspected / total_produced) * 100), 1) if total_produced > ZERO else (100.0 if tot_inspected > ZERO else 0.0)



        entries_list = []

        for q in qc_qs:

            insp_name = getattr(q.inspector_employee, "name", None) or getattr(q.inspector, "name", None) or getattr(q.inspector, "email", "Inspector")

            entries_list.append({

                "id": str(q.id),

                "inspectionNumber": q.inspection_number,

                "inspection_number": q.inspection_number,

                "date": str(q.inspection_date),

                "inspectionDate": str(q.inspection_date),

                "inspectedQty": float(q.total_inspected_qty),

                "inspectedQuantity": float(q.total_inspected_qty),

                "acceptedQty": float(q.passed_qty),

                "acceptedQuantity": float(q.passed_qty),

                "rejectedQty": float(q.rejected_qty),

                "rejectedQuantity": float(q.rejected_qty),

                "reprocessQty": float(q.reprocess_qty),

                "reprocessQuantity": float(q.reprocess_qty),

                "holdQty": float(q.hold_qty),

                "holdQuantity": float(q.hold_qty),

                "inspectorName": insp_name,

                "inspectorId": str(q.inspector_employee_id or q.inspector_id or ""),

                "grade": q.quality_grade or "Grade A",

                "qualityGrade": q.quality_grade or "Grade A",

                "defectsFound": q.defects_found or "None / Clear",

                "inspectionType": q.inspection_type or "Grey Fabric QC",

                "inspectionMethod": q.inspection_method or "Visual + Measurement",

                "gsm": float(q.gsm) if q.gsm is not None else None,

                "width": float(q.width) if q.width is not None else None,

                "remarks": q.notes or "",

                "photos": q.photos or [],

                "status": q.status,

                "createdAt": q.created_at.isoformat() if q.created_at else None,

            })



        active_inspectors = [

            {"id": str(e.id), "name": e.name, "code": e.employee_code}

            for e in Employee.objects.filter(client_id=po.client_id, status__in=["Active", "Probation"], deleted_at__isnull=True).order_by("name")

        ]

        if not active_inspectors:

            from apps.accounts.models import User

            active_inspectors = [

                {"id": str(u.id), "name": u.name or u.email, "code": u.email.split("@")[0]}

                for u in User.objects.filter(client_id=po.client_id, status="Active").order_by("name")

            ]



        fabric_quality = (active_pi.fabric if (active_pi and active_pi.fabric) else fabric_name)



        resp_payload = {

            "success": True,

            "linkedDetails": {

                "poNumber": po.po_number,

                "instructionNumber": active_pi.instruction_number if active_pi else "PI-Pending",

                "instructionId": str(active_pi.id) if active_pi else None,

                "vendorName": vendor_name,

                "vendorState": getattr(po.party, "billing_state", "Maharashtra") if po.party else "Maharashtra",

                "fabricQuality": fabric_quality,

                "poQuantity": float(total_po_qty),

                "poQuantityMtr": float(total_po_qty),

                "totalProduced": float(total_produced),

                "totalProducedMtr": float(total_produced),

                "eligibleForQc": float(pending_for_qc),

                "pendingForQcMtr": float(pending_for_qc),

            },

            "summary": {

                "totalProduced": float(total_produced),

                "totalProducedMtr": float(total_produced),

                "totalInspected": float(tot_inspected),

                "totalInspectedMtr": float(tot_inspected),

                "acceptedQty": float(acc_qty),

                "totalAcceptedMtr": float(acc_qty),

                "rejectedQty": float(rej_qty),

                "totalRejectedMtr": float(rej_qty),

                "reprocessQty": float(rep_qty),

                "totalReprocessMtr": float(rep_qty),

                "holdQty": float(hld_qty),

                "totalHoldMtr": float(hld_qty),

                "pendingForInspection": float(pending_for_qc),

                "pendingForInspectionMtr": float(pending_for_qc),

                "completionPercentage": completion_pct,

            },

            "entries": entries_list,

            "inspectors": active_inspectors,

        }

        if created_qc_entry:

            resp_payload["entry"] = next((e for e in entries_list if e["id"] == str(created_qc_entry.id)), None)

            return Response(resp_payload, status=status.HTTP_201_CREATED)



        return Response(resp_payload)



    @action(detail=False, methods=["get"], url_path="next-jwpo-number")

    def next_jwpo_number(self, request):

        from apps.core.numbering import financial_year_label

        client = getattr(request.user, "client", None)

        year_str = financial_year_label(client, reset_policy="fy") if client else str(timezone.localdate().year)



        last_po = PurchaseOrder.objects.filter(

            client_id=request.client_id,

        ).filter(

            Q(po_number__startswith="FJWPO") | Q(po_number__startswith="JWPO")

        ).order_by("-po_number").first()



        seq = 1

        if last_po and last_po.po_number:

            parts = last_po.po_number.split("-")

            if len(parts) >= 3 and parts[-1].isdigit():

                seq = int(parts[-1]) + 1



        next_no = f"FJWPO-{year_str}-{seq:04d}"

        return Response({"nextPoNumber": next_no, "poNo": next_no})



    @action(detail=False, methods=["post"], url_path="generate-final-job-work")

    @transaction.atomic

    def generate_final_job_work(self, request):

        from apps.pms import production_services

        from apps.pms.models import ProductionInstruction



        instruction_id = request.data.get("instructionId") or request.data.get("instruction_id")

        if instruction_id:

            instruction = ProductionInstruction.objects.filter(

                client_id=request.client_id, id=instruction_id, deleted_at__isnull=True

            ).first()

            if not instruction:

                raise ValidationFailed(f"Production instruction {instruction_id} not found.")



            instruction = production_services.generate_final_job_work_po(

                instruction, user=request.user, custom_data=request.data

            )

            po = instruction.final_po

            return Response(self.get_serializer(po).data, status=status.HTTP_201_CREATED)

        else:

            from apps.masters.models import Party, Item

            vendor_id = request.data.get("vendorId")

            vendor = Party.objects.filter(client_id=request.client_id, id=vendor_id).first() if vendor_id else None

            if not vendor:

                vendor = Party.objects.filter(client_id=request.client_id, type__in=["Vendor", "Both"]).first()



            po_no = request.data.get("finalPoNo") or allocate_number(request.user.client, "JWPO")

            po = PurchaseOrder(

                client=request.user.client,

                po_number=po_no,

                party=vendor,

                doc_date=request.data.get("poDate") or timezone.localdate(),

                status="Pending Approval" if request.data.get("sendToApproval", True) else "Approved",

                notes=request.data.get("remarks") or "Final Job Work Purchase Order",

                purchase_type=PURCHASE_TYPE_FINAL_JOB_WORK,

                other_charges=Decimal(str(request.data.get("otherCharges") or 0)),

                created_by=request.user if getattr(request.user, "is_authenticated", False) else None,

            )

            po.freeze_party_snapshot()

            po.save()



            items = request.data.get("items") or []

            default_item = Item.objects.filter(client_id=request.client_id, deleted_at__isnull=True).exclude(item_kind="Service").first()

            from apps.sales.services import recalculate_document

            for idx, it in enumerate(items, start=1):

                qty = Decimal(str(it.get("billableQty") or it.get("qty") or 100))

                rate = Decimal(str(it.get("rate") or 0))

                tax_pct = Decimal(str(it.get("gstPct") or 5))

                line = PurchaseOrderLine(

                    client=request.user.client,

                    purchase_order=po,

                    line_no=idx,

                    item=default_item,

                    description=it.get("description") or f"Job Work - {default_item.name if default_item else 'Processing'}",

                    qty=qty,

                    rate=rate,

                    tax_pct=tax_pct,

                    uom=it.get("uom") or "Meter",

                )

                line.freeze_item_snapshot()

                line.save()

            recalculate_document(po)

            return Response(self.get_serializer(po).data, status=status.HTTP_201_CREATED)



    @action(detail=False, methods=["get"], url_path="final-job-work-list")

    def final_job_work_list(self, request):

        qs = self.get_queryset().filter(

            Q(po_number__startswith="JWPO")

            | Q(reference_number__startswith="PROD-")

            | Q(production_instructions__isnull=False)

            | Q(notes__icontains="Job Work")

        ).distinct()

        page = self.paginate_queryset(qs)

        if page is not None:

            serializer = self.get_serializer(page, many=True)

            return self.get_paginated_response(serializer.data)

        serializer = self.get_serializer(qs, many=True)

        return Response(serializer.data)



    @action(detail=False, methods=["get"], url_path="auto-suggestions")

    def auto_suggestions(self, request):

        """``GET /purchase/orders/auto-suggestions/`` -- the AutoPOModal feed."""

        return Response(envelope(services.auto_po_suggestions(request.client_id)))

