"""Customer payment terms and early-payment discounts.

Payment terms
    An invoice's terms (``payment_terms`` label + ``credit_days``) and due date
    are entered per invoice. The customer's own credit days are only a default
    on the form; nothing here imposes a term. Changing them on a finalized
    invoice needs ``edit_payment_terms`` and a reason, and is audited.

Early-payment discounts
    A request ties one receipt's allocation to one invoice and records what the
    customer asked for. Nothing is granted for paying early: an authorised user
    decides the amount.

        Draft -> Submitted -> Under Review -> Approved | Rejected
        (Draft / Submitted / Under Review) -> Cancelled
        Approved -> Cancelled  (reverses the credit note)

    Approval is the only step that touches the books. It raises a credit note
    through the existing note workflow (accounting/note_services.py: create ->
    approve -> post, so Dr Sales Returns / Cr Sundry Debtors as for any customer
    credit note) and re-derives the invoice's ``settlement_discount``. The
    receipt itself is never changed: money received and discount allowed stay
    separate on the invoice, in the ledger and on the customer statement.
"""
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.core.audit import record_audit
from apps.core.exceptions import (
    BusinessRuleViolation,
    Codes,
    Conflict,
    PermissionDenied,
    ValidationFailed,
)
from apps.core.money import ZERO, D, ROUNDING_TOLERANCE, round2
from apps.core.numbering import allocate_number
from apps.core.permissions import has_permission

from .models import (
    DISCOUNT_OPEN_STATUSES,
    EarlyPaymentDiscountRequest,
    PaymentAllocation,
    PaymentIn,
    SalesInvoice,
)

#: The term choices offered on the invoice form. "Custom" takes any number of
#: days the user enters; none of these is applied unless someone picks it.
PAYMENT_TERM_OPTIONS = [
    {"label": "Immediate Payment", "days": 0},
    {"label": "7 Days", "days": 7},
    {"label": "15 Days", "days": 15},
    {"label": "30 Days", "days": 30},
    {"label": "45 Days", "days": 45},
    {"label": "60 Days", "days": 60},
    {"label": "Custom", "days": None},
]
MAX_CREDIT_DAYS = 3650


def _user(user):
    return user if getattr(user, "is_authenticated", False) else None


def _iso(value):
    return value.isoformat() if value else None


def term_label(credit_days, label=None):
    """The stored label for a term: a preset's own name, else ``Custom (N days)``."""
    if label and label != "Custom":
        return label
    preset = next((o for o in PAYMENT_TERM_OPTIONS if o["days"] == credit_days), None)
    if preset and label != "Custom":
        return preset["label"]
    return f"Custom ({credit_days} days)"


def _parse_days(value):
    try:
        days = int(value)
    except (TypeError, ValueError):
        raise ValidationFailed(
            "Enter the number of credit days.", field_errors={"creditDays": ["Must be a whole number."]}
        )
    if days < 0 or days > MAX_CREDIT_DAYS:
        raise ValidationFailed(
            f"Credit days must be between 0 and {MAX_CREDIT_DAYS}.",
            field_errors={"creditDays": ["Out of range."]},
        )
    return days


# ---------------------------------------------------------------------------
# Payment terms
# ---------------------------------------------------------------------------
@transaction.atomic
def change_payment_terms(invoice, *, credit_days, label=None, due_date=None, reason=None, user=None):
    """Set an invoice's terms and due date.

    The due date is the invoice date plus the credit days unless ``due_date``
    is given; a due date that differs from that is a manual override and needs
    a reason. A finalized invoice always needs ``edit_payment_terms`` and a
    reason. Every change is written to the audit log with the old and new values.
    """
    invoice = SalesInvoice.objects.select_for_update().get(pk=invoice.pk)
    if invoice.status == "Cancelled":
        raise Conflict("This invoice has been cancelled.", code=Codes.ALREADY_CANCELLED)

    days = _parse_days(credit_days)
    computed_due = invoice.doc_date + timedelta(days=days)
    new_due = due_date or computed_due
    if new_due < invoice.doc_date:
        raise ValidationFailed(
            "The due date can't be before the invoice date.",
            field_errors={"dueDate": ["Before the invoice date."]},
        )
    reason = (reason or "").strip()
    finalized = invoice.posted_at is not None
    overridden = new_due != computed_due

    if finalized and not has_permission(user, "edit_payment_terms"):
        raise PermissionDenied("You don't have permission to change payment terms on a finalized invoice.")
    if not finalized and not (has_permission(user, "create_invoice") or has_permission(user, "edit_payment_terms")):
        raise PermissionDenied("You don't have permission to change this invoice.")
    if (finalized or overridden) and not reason:
        raise ValidationFailed(
            "Give a reason for this change.",
            field_errors={"reason": ["Required when changing a finalized invoice or overriding the due date."]},
        )

    before = {
        "paymentTerms": invoice.payment_terms,
        "creditDays": invoice.credit_days,
        "dueDate": _iso(invoice.due_date),
    }
    invoice.payment_terms = term_label(days, label)
    invoice.credit_days = days
    invoice.due_date = new_due
    after = {
        "paymentTerms": invoice.payment_terms,
        "creditDays": invoice.credit_days,
        "dueDate": _iso(invoice.due_date),
        "dueDateOverridden": overridden,
    }
    if all(before[k] == after[k] for k in before):
        return invoice
    invoice.save(update_fields=["payment_terms", "credit_days", "due_date", "updated_at"])

    record_audit(
        client=invoice.client_id,
        actor=user,
        action="payment_terms_change",
        entity_type="SalesInvoice",
        entity_id=invoice.id,
        entity_label=invoice.invoice_number,
        description=reason or "Payment terms set",
        before=before,
        after=after,
        from_value=before["dueDate"],
        to_value=after["dueDate"],
        comments=reason or None,
    )
    return invoice


# ---------------------------------------------------------------------------
# Early-payment discount requests
# ---------------------------------------------------------------------------
def _allocation(invoice, payment):
    return PaymentAllocation.objects.filter(
        client_id=invoice.client_id, payment_side="in", payment_id=payment.id,
        document_type="SalesInvoice", document_id=invoice.id, deleted_at__isnull=True,
    ).first()


def _amount(value, field):
    try:
        amount = round2(D(value))
    except Exception:  # noqa: BLE001 - any unparsable input is the same user error
        raise ValidationFailed("Enter a valid amount.", field_errors={field: ["Not a number."]})
    if amount <= ZERO:
        raise ValidationFailed("The amount must be greater than zero.", field_errors={field: ["Must be > 0."]})
    return amount


def _audit(req, user, action, description, before=None, after=None, comments=None):
    record_audit(
        client=req.client_id,
        actor=user,
        action=action,
        entity_type="EarlyPaymentDiscountRequest",
        entity_id=req.id,
        entity_label=req.request_number,
        description=description,
        before=before,
        after=after,
        comments=comments,
    )


def _lock(req):
    return EarlyPaymentDiscountRequest.objects.select_for_update().get(pk=req.pk)


def _require_status(req, allowed, verb):
    if req.status not in allowed:
        raise Conflict(
            f"A {req.status.lower()} request can't be {verb}.",
            code="INVALID_STATUS",
            payload={"status": req.status},
        )


def _discount_room(invoice, exclude_id=None):
    """How much more discount this invoice can take: its total less approved discounts."""
    approved = EarlyPaymentDiscountRequest.objects.filter(
        client_id=invoice.client_id, invoice=invoice, status="Approved", deleted_at__isnull=True,
    )
    if exclude_id:
        approved = approved.exclude(pk=exclude_id)
    taken = sum((D(r.approved_amount) for r in approved), ZERO)
    return round2(D(invoice.total) - taken)


@transaction.atomic
def create_request(
    *, client, invoice, payment, requested_amount, reason, customer_request_date=None,
    notes=None, attachments=None, submit=False, user=None,
):
    """Record a customer's early-payment discount request against one receipt."""
    invoice = SalesInvoice.objects.select_for_update().get(pk=invoice.pk)
    payment = PaymentIn.objects.select_for_update().get(pk=payment.pk)

    if invoice.posted_at is None or invoice.status in ("Draft", "Cancelled"):
        raise BusinessRuleViolation(
            "A discount can only be requested on a finalized, active invoice.", code=Codes.NOT_FINALIZED
        )
    if payment.status != "Active":
        raise BusinessRuleViolation("That receipt has been cancelled.", code=Codes.ALREADY_CANCELLED)
    if invoice.party_id != payment.party_id:
        raise ValidationFailed(
            "The receipt and the invoice belong to different customers.",
            field_errors={"paymentId": ["Customer mismatch."]},
        )
    allocation = _allocation(invoice, payment)
    if allocation is None:
        raise ValidationFailed(
            "That receipt has not been allocated to this invoice. Allocate it first.",
            field_errors={"paymentId": ["Not allocated to this invoice."]},
        )
    reason = (reason or "").strip()
    if not reason:
        raise ValidationFailed("Give the customer's reason for the request.", field_errors={"reason": ["Required."]})
    amount = _amount(requested_amount, "requestedAmount")
    if amount > D(invoice.total):
        raise ValidationFailed(
            "The requested discount is more than the invoice value.",
            field_errors={"requestedAmount": ["Exceeds the invoice total."]},
        )
    duplicate = EarlyPaymentDiscountRequest.objects.filter(
        client_id=invoice.client_id, invoice=invoice, payment=payment, deleted_at__isnull=True,
    ).exclude(status__in=["Rejected", "Cancelled"]).first()
    if duplicate is not None:
        raise Conflict(
            f"Request {duplicate.request_number} already covers this receipt on this invoice.",
            code="DUPLICATE_DISCOUNT_REQUEST",
            payload={"requestId": str(duplicate.id), "requestNumber": duplicate.request_number},
        )

    due = invoice.due_date
    req = EarlyPaymentDiscountRequest.objects.create(
        client=client,
        request_number=allocate_number(client, "EPD"),
        party_id=invoice.party_id,
        invoice=invoice,
        payment=payment,
        invoice_number=invoice.invoice_number,
        invoice_date=invoice.doc_date,
        invoice_total=invoice.total,
        payment_terms=invoice.payment_terms,
        credit_days=invoice.credit_days,
        due_date=due,
        payment_date=payment.payment_date,
        eligible_amount=allocation.amount,
        days_paid_early=(due - payment.payment_date).days if due else 0,
        requested_amount=amount,
        customer_request_date=customer_request_date,
        reason=reason,
        notes=notes or None,
        attachments=list(attachments or []),
        status="Draft",
        requested_by=_user(user),
        created_by=_user(user),
    )
    _audit(req, user, "discount_request_create",
           f"Discount request {req.request_number} for {invoice.invoice_number}: {amount} requested.",
           after={"status": "Draft", "requestedAmount": str(amount)})
    if submit:
        req = submit_request(req, user=user)
    return req


@transaction.atomic
def update_request(req, *, user=None, **changes):
    """Edit a draft (the requester's own fields)."""
    req = _lock(req)
    _require_status(req, ["Draft"], "edited")
    before, after = {}, {}
    if "requested_amount" in changes and changes["requested_amount"] is not None:
        amount = _amount(changes["requested_amount"], "requestedAmount")
        if amount > D(req.invoice_total):
            raise ValidationFailed(
                "The requested discount is more than the invoice value.",
                field_errors={"requestedAmount": ["Exceeds the invoice total."]},
            )
        before["requestedAmount"], after["requestedAmount"] = str(req.requested_amount), str(amount)
        req.requested_amount = amount
    for field, key in (("reason", "reason"), ("notes", "notes"),
                       ("customer_request_date", "customerRequestDate"), ("attachments", "attachments")):
        if field in changes:
            value = changes[field]
            if field == "reason":
                value = (value or "").strip()
                if not value:
                    raise ValidationFailed("Give the customer's reason.", field_errors={"reason": ["Required."]})
            before[key] = _iso(getattr(req, field)) if field == "customer_request_date" else getattr(req, field)
            setattr(req, field, value)
            after[key] = _iso(value) if field == "customer_request_date" else value
    req.updated_by = _user(user)
    req.save()
    _audit(req, user, "discount_request_update", "Draft edited", before=before, after=after)
    return req


@transaction.atomic
def submit_request(req, *, user=None):
    req = _lock(req)
    _require_status(req, ["Draft"], "submitted")
    req.status = "Submitted"
    req.submitted_at = timezone.now()
    req.save(update_fields=["status", "submitted_at", "updated_at"])
    _audit(req, user, "discount_request_submit", "Submitted for approval",
           before={"status": "Draft"}, after={"status": "Submitted"})
    return req


def _assert_approver(req, user):
    if not has_permission(user, "approve_payment_discount"):
        raise PermissionDenied("You don't have permission to review early payment discounts.")
    # Same separation of duties as the approvals inbox
    # (core/approval_services.decide_approval_request).
    if req.requested_by_id and req.requested_by_id == getattr(user, "id", None) and not getattr(user, "is_superuser", False):
        raise PermissionDenied("You can't decide a discount request you raised yourself.")


@transaction.atomic
def start_review(req, *, user=None, proposed_amount=None, comments=None):
    req = _lock(req)
    _require_status(req, ["Submitted"], "taken into review")
    _assert_approver(req, user)
    req.status = "Under Review"
    req.reviewed_by = _user(user)
    req.reviewed_at = timezone.now()
    fields = ["status", "reviewed_by", "reviewed_at", "updated_at"]
    after = {"status": "Under Review"}
    if proposed_amount not in (None, ""):
        req.proposed_amount = _amount(proposed_amount, "proposedAmount")
        fields.append("proposed_amount")
        after["proposedAmount"] = str(req.proposed_amount)
    req.save(update_fields=fields)
    _audit(req, user, "discount_request_review", comments or "Review started",
           before={"status": "Submitted"}, after=after, comments=comments)
    return req


@transaction.atomic
def propose_amount(req, *, amount, reason=None, user=None):
    """An approver revises the amount they intend to grant (old and new kept in the audit)."""
    req = _lock(req)
    _require_status(req, ["Submitted", "Under Review"], "revised")
    _assert_approver(req, user)
    amount = _amount(amount, "proposedAmount")
    before = {"proposedAmount": str(req.proposed_amount) if req.proposed_amount is not None else None}
    req.proposed_amount = amount
    if req.status == "Submitted":
        req.status = "Under Review"
        req.reviewed_by = _user(user)
        req.reviewed_at = timezone.now()
    req.save(update_fields=["proposed_amount", "status", "reviewed_by", "reviewed_at", "updated_at"])
    _audit(req, user, "discount_request_propose", reason or "Proposed amount revised",
           before=before, after={"proposedAmount": str(amount)}, comments=reason)
    return req


@transaction.atomic
def approve_request(req, *, user=None, amount=None, reason=None):
    """Grant the discount: credit note through the note workflow, then settle the invoice."""
    from apps.accounting import note_services

    from .services import refresh_invoice_payment_status

    req = _lock(req)
    _require_status(req, ["Submitted", "Under Review"], "approved")
    _assert_approver(req, user)
    if req.credit_note_id:
        raise Conflict("This discount has already been posted.", code=Codes.ALREADY_DONE)

    invoice = SalesInvoice.objects.select_for_update().get(pk=req.invoice_id)
    payment = PaymentIn.objects.get(pk=req.payment_id)
    if invoice.status == "Cancelled":
        raise BusinessRuleViolation("The invoice has been cancelled.", code=Codes.ALREADY_CANCELLED)
    if payment.status != "Active" or _allocation(invoice, payment) is None:
        raise BusinessRuleViolation(
            "The receipt behind this request is no longer allocated to the invoice.",
            code="PAYMENT_NOT_ALLOCATED",
        )

    chosen = amount if amount not in (None, "") else (
        req.proposed_amount if req.proposed_amount is not None else req.requested_amount
    )
    granted = _amount(chosen, "approvedAmount")
    room = _discount_room(invoice)
    if granted > room + ROUNDING_TOLERANCE:
        raise BusinessRuleViolation(
            f"The discount ({granted}) is more than the invoice can take ({room}).",
            code="DISCOUNT_EXCEEDS_INVOICE",
            payload={"granted": str(granted), "available": str(room)},
        )
    reason = (reason or "").strip()
    if not reason:
        raise ValidationFailed("Record the reason for the decision.", field_errors={"reason": ["Required."]})

    note = note_services.create_standalone_note(
        client=invoice.client,
        party=invoice.party,
        note_type="CREDIT_NOTE",
        taxable_amount=granted,
        against_document_type="INVOICE",
        against_document_id=invoice.id,
        against_document_number=invoice.invoice_number,
        reason_notes=f"Early payment discount {req.request_number}",
        notes=reason,
        user=user,
    )
    # The discount decision is the approval; the note's own approve step
    # records the same approver before it is posted.
    note_services.approve_standalone_note(note, user=user)
    note = note_services.post_standalone_note(note, user=user)

    before = {"status": req.status}
    req.status = "Approved"
    req.approved_amount = granted
    req.decided_by = _user(user)
    req.decided_at = timezone.now()
    req.decision_reason = reason
    req.credit_note = note
    req.save(update_fields=[
        "status", "approved_amount", "decided_by", "decided_at", "decision_reason",
        "credit_note", "updated_at",
    ])
    refresh_invoice_payment_status(invoice)
    from apps.accounting import services as ledger

    ledger.recalculate_party_balance(invoice.client_id, invoice.party)
    _audit(req, user, "discount_request_approve",
           f"Approved {granted} ({note.note_number}).", before=before,
           after={"status": "Approved", "approvedAmount": str(granted), "creditNote": note.note_number},
           comments=reason)
    return req


@transaction.atomic
def reject_request(req, *, user=None, reason=None):
    req = _lock(req)
    _require_status(req, ["Submitted", "Under Review"], "rejected")
    _assert_approver(req, user)
    reason = (reason or "").strip()
    if not reason:
        raise ValidationFailed("Record the reason for the rejection.", field_errors={"reason": ["Required."]})
    before = {"status": req.status}
    req.status = "Rejected"
    req.decided_by = _user(user)
    req.decided_at = timezone.now()
    req.decision_reason = reason
    req.save(update_fields=["status", "decided_by", "decided_at", "decision_reason", "updated_at"])
    _audit(req, user, "discount_request_reject", reason, before=before,
           after={"status": "Rejected"}, comments=reason)
    return req


@transaction.atomic
def cancel_request(req, *, user=None, reason=None, system=False):
    """Withdraw an open request, or reverse an approved one (its credit note is cancelled)."""
    from apps.accounting import note_services

    from .services import refresh_invoice_payment_status

    req = _lock(req)
    _require_status(req, DISCOUNT_OPEN_STATUSES + ["Approved"], "cancelled")
    reason = (reason or "").strip()
    if not reason:
        raise ValidationFailed("Give a reason for cancelling.", field_errors={"reason": ["Required."]})
    if not system:
        if req.status == "Approved":
            if not has_permission(user, "approve_payment_discount"):
                raise PermissionDenied("Only a discount approver can reverse an approved discount.")
        elif not (
            has_permission(user, "approve_payment_discount")
            or (req.requested_by_id and req.requested_by_id == getattr(user, "id", None))
        ):
            raise PermissionDenied("Only the requester or a discount approver can cancel this request.")

    before = {"status": req.status}
    was_approved = req.status == "Approved"
    req.status = "Cancelled"
    req.cancelled_by = _user(user)
    req.cancelled_at = timezone.now()
    req.cancellation_reason = reason
    req.save(update_fields=["status", "cancelled_by", "cancelled_at", "cancellation_reason", "updated_at"])

    if was_approved:
        if req.credit_note_id:
            note_services.cancel_standalone_note(req.credit_note, user=user, reason=reason)
        invoice = SalesInvoice.objects.select_for_update().get(pk=req.invoice_id)
        refresh_invoice_payment_status(invoice)
        from apps.accounting import services as ledger

        ledger.recalculate_party_balance(invoice.client_id, invoice.party)
    _audit(req, user, "discount_request_cancel", reason, before=before,
           after={"status": "Cancelled", "reversed": was_approved}, comments=reason)
    return req


def cancel_open_requests_for_payment(payment, *, user=None, reason):
    """A cancelled receipt takes its undecided requests with it."""
    for req in EarlyPaymentDiscountRequest.objects.filter(
        client_id=payment.client_id, payment=payment, status__in=DISCOUNT_OPEN_STATUSES,
        deleted_at__isnull=True,
    ):
        cancel_request(req, user=user, reason=reason, system=True)


def request_history(req):
    from apps.core.models import AuditLog

    return [
        {
            "action": row.action,
            "description": row.description,
            "actorName": row.actor_name,
            "before": row.before,
            "after": row.after,
            "comments": row.comments,
            "at": row.created_at.isoformat(),
        }
        for row in AuditLog.objects.filter(
            client_id=req.client_id, entity_type="EarlyPaymentDiscountRequest", entity_id=req.id
        ).order_by("created_at", "id")
    ]
