"""Approval decisions for sales documents (see core.approval_services)."""
from apps.core.approval_services import approval_handler, document_uuid
from apps.core.exceptions import BusinessRuleViolation, Conflict
from apps.sales.discount_control import APPROVAL_TYPES

PENDING = "Pending Approval"


@approval_handler("brokerage_rate_change")
def on_brokerage_rate_change(req, decision):
    """Apply an approved broker / commission rate; a rejected one never applies."""
    from apps.sales import broker_services

    if decision == "Approved" and document_uuid(req) is not None:
        broker_services.apply_rate_change(req)


@approval_handler("credit_limit_exceeded")
def on_credit_limit_override(req, decision):
    """Finalize the same Draft only after an authorized credit approval."""
    if decision != "Approved":
        return

    from decimal import Decimal

    from apps.sales import services
    from apps.sales.models import SalesInvoice

    invoice_id = document_uuid(req)
    if invoice_id is None:
        raise Conflict("The credit approval is not linked to a valid invoice.")
    invoice = SalesInvoice.objects.select_for_update().filter(
        client_id=req.client_id, id=invoice_id, deleted_at__isnull=True
    ).first()
    if invoice is None or invoice.status != "Draft" or invoice.posted_at is not None:
        raise Conflict("The approved Draft invoice is no longer available to finalize.")

    approved_values = req.new_values or {}
    approved_amount = Decimal(str(approved_values.get("currentInvoiceAmount", "0")))
    approved_exposure = Decimal(str(approved_values.get("projectedExposure", "0")))
    approved_limit = Decimal(str(approved_values.get("creditLimit", "0")))
    current_snapshot = services.credit_limit_snapshot(invoice.party, invoice.total)
    if (
        Decimal(current_snapshot["currentInvoiceAmount"]) != approved_amount
        or Decimal(current_snapshot["projectedExposure"]) > approved_exposure
        or Decimal(current_snapshot["creditLimit"]) < approved_limit
    ):
        raise BusinessRuleViolation(
            "The invoice or credit exposure changed after approval was requested. "
            "Request a new credit approval.",
            code="CREDIT_APPROVAL_STALE",
            payload={"approved": approved_values, "current": current_snapshot},
        )

    services.finalize_invoice(
        invoice,
        user=req.decided_by,
        override_credit_limit=True,
    )


@approval_handler(*APPROVAL_TYPES)
def on_sales_discount(req, decision):
    """Release the document that was waiting for a discount / margin approval.

    Approved: a sales order is confirmed (and reserves its takas); a quotation
    goes back to Draft, ready to send. Rejected or returned: the document goes
    back to Draft for the salesperson to correct. An invoice never moves on
    its own -- it stays Draft and is finalized again.
    """
    from apps.sales import services
    from apps.sales.models import Quotation, SalesOrder

    kind = (req.new_values or {}).get("documentKind")
    doc_id = document_uuid(req)
    if doc_id is None:
        return
    if kind == "Sales Order":
        order = SalesOrder.objects.select_for_update().filter(
            client_id=req.client_id, id=doc_id, deleted_at__isnull=True
        ).first()
        if order is None or order.stage != PENDING:
            return
        if decision == "Approved":
            order.stage = "Confirmed"
            order.save(update_fields=["stage", "updated_at"])
            services.reserve_order_takas(order)
        else:
            order.stage = "Draft"
            order.save(update_fields=["stage", "updated_at"])
    elif kind == "Quotation":
        quotation = Quotation.objects.filter(
            client_id=req.client_id, id=doc_id, deleted_at__isnull=True
        ).first()
        if quotation is not None and quotation.status == PENDING:
            quotation.status = "Draft"
            quotation.save(update_fields=["status", "updated_at"])
