"""
Return QC-rejected purchased fabric to the supplier (PDF §08 disposition).

The rejected rolls sit at the Reject location after QC. This builds an
ordinary Purchase Return / debit note against the supplier's bill -- same
numbering, same bill-line ``returned_qty`` guard, same ledger posting -- but
moves the stock out of the Reject location roll by roll, so the batch keeps
its full history. Partial returns are fine; nothing is returned
automatically, and a roll cannot be returned twice (its stock is gone).
"""
from collections import defaultdict
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.core.audit import record_audit
from apps.core.exceptions import BusinessRuleViolation, Codes, ValidationFailed
from apps.core.money import ZERO, D, round4
from apps.core.numbering import allocate_number


def _bill_line_for(taka):
    """The supplier bill line a received roll was billed on."""
    from .models import GoodsReceipt, GoodsReceiptLine, PurchaseBill, PurchaseBillLine

    if taka.source_document_type != "GRN" or not taka.source_document_id:
        return None
    receipt = GoodsReceipt.objects.filter(pk=taka.source_document_id).select_related("purchase_bill").first()
    if receipt is None:
        return None
    if taka.source_line_id:
        grn_line = GoodsReceiptLine.objects.filter(pk=taka.source_line_id).select_related("purchase_bill_line").first()
        if grn_line is not None and grn_line.purchase_bill_line_id:
            return grn_line.purchase_bill_line
    bills = []
    if receipt.purchase_bill_id:
        bills.append(receipt.purchase_bill)
    if receipt.purchase_order_id:
        bills.extend(PurchaseBill.objects.filter(
            client_id=taka.client_id, purchase_order_id=receipt.purchase_order_id, deleted_at__isnull=True,
        ).exclude(status="Cancelled").order_by("doc_date", "created_at"))
    for bill in bills:
        line = PurchaseBillLine.objects.filter(
            purchase_bill=bill, item_id=taka.item_id, deleted_at__isnull=True,
        ).order_by("line_no").first()
        if line is not None:
            return line
    return None


@transaction.atomic
def return_rejected_rolls(*, client, rolls, reason, user=None, return_date=None, loss_id=None):
    """``rolls`` = [{lotTakaId, qty}] in stock units. One debit note per bill."""
    from apps.accounting import services as ledger
    from apps.inventory import services as stock
    from apps.inventory.models import LotTaka
    from apps.masters import uom
    from apps.sales.services import recalculate_document

    from .models import PurchaseReturn, PurchaseReturnLine

    client_id = getattr(client, "id", client)
    if not (reason or "").strip():
        raise ValidationFailed("Give the rejection reason for the supplier.", field_errors={"reason": ["Required."]})
    reject_location = stock.system_location(client_id, "REJECT")
    groups = defaultdict(list)  # bill -> [(taka, stock_qty, bill_line)]
    for row in rolls or []:
        qty = round4(D(row.get("qty") or 0))
        if qty <= ZERO:
            continue
        taka = LotTaka.objects.filter(
            client_id=client_id, pk=row.get("lotTakaId"), deleted_at__isnull=True,
        ).select_for_update(of=("self",)).select_related("item", "batch").first()
        if taka is None:
            raise ValidationFailed("A selected roll was not found.", field_errors={"rolls": ["Unknown roll."]})
        label = taka.taka_id or taka.taka_number
        if str(taka.location_id) != str(reject_location.id):
            raise BusinessRuleViolation(
                f"Roll {label} is not in Reject stock; only QC-rejected material is returned from here.",
                code="NOT_REJECTED_STOCK",
            )
        if qty > D(taka.available_quantity):
            raise BusinessRuleViolation(
                f"Roll {label} has {round4(D(taka.available_quantity))} rejected left; {qty} selected.",
                code=Codes.INSUFFICIENT_STOCK,
            )
        bill_line = _bill_line_for(taka)
        if bill_line is None:
            raise BusinessRuleViolation(
                f"Roll {label} has no supplier bill to raise a debit note against. "
                "Record the bill first, then return.",
                code="NO_BILL_FOR_RETURN",
            )
        groups[bill_line.purchase_bill_id].append((taka, qty, bill_line))
    if not groups:
        raise ValidationFailed("Pick the rejected rolls and quantities to return.", field_errors={"rolls": ["Required."]})

    returns = []
    on_date = return_date or timezone.localdate()
    for rows in groups.values():
        bill = rows[0][2].purchase_bill
        if bill.status == "Cancelled":
            raise BusinessRuleViolation("Cannot return against a cancelled bill.", code=Codes.PAYMENT_ON_CANCELLED)
        number = allocate_number(client, "PR", on_date)
        purchase_return = PurchaseReturn.objects.create(
            client_id=client_id, party=bill.party, party_name=bill.party_name, party_gstin=bill.party_gstin,
            place_of_supply=bill.place_of_supply, purchase_bill=bill, doc_date=on_date,
            return_number=number, debit_note_number=number, status="Posted",
            reason=f"QC rejection: {reason}", location=reject_location,
            created_by=user if getattr(user, "is_authenticated", False) else None,
        )
        per_line = defaultdict(lambda: ZERO)
        for _taka, qty, bill_line in rows:
            per_line[bill_line] += qty
        for index, (bill_line, stock_qty) in enumerate(per_line.items(), start=1):
            factor = D(bill_line.uom_factor or 1) or Decimal("1")
            bill_qty = round4(stock_qty / factor)
            remaining = D(bill_line.qty) - D(bill_line.returned_qty)
            if bill_qty > remaining + Decimal("0.0001"):
                raise BusinessRuleViolation(
                    f"{bill_line.item_name}: only {round4(remaining)} remains returnable on bill {bill.bill_number}.",
                    code=Codes.OVER_RETURN,
                )
            PurchaseReturnLine.objects.create(
                client_id=client_id, purchase_return=purchase_return, purchase_bill_line=bill_line,
                line_no=index, item=bill_line.item, item_name=bill_line.item_name, sku=bill_line.sku,
                hsn_code=bill_line.hsn_code, uom=bill_line.uom, uom_factor=bill_line.uom_factor,
                qty=bill_qty, returned_qty=bill_qty, rate=bill_line.rate,
                discount_pct=bill_line.discount_pct, tax_pct=bill_line.tax_pct,
            )
            bill_line.returned_qty = round4(D(bill_line.returned_qty) + bill_qty)
            bill_line.save(update_fields=["returned_qty", "updated_at"])
        recalculate_document(purchase_return)

        for taka, qty, bill_line in rows:
            factor = D(bill_line.uom_factor or 1) or Decimal("1")
            stock.post_movement(
                client_id=client_id, item=taka.item, location=reject_location, type="PURCHASE_RETURN",
                quantity=-qty,
                unit_cost=(D(bill_line.landed_unit_cost) / factor) if bill_line.landed_unit_cost else None,
                reference_type="PurchaseReturn", reference_id=purchase_return.id,
                reference_number=purchase_return.return_number, movement_date=on_date, lot_taka=taka,
                notes=f"QC reject returned to {bill.party_name or 'supplier'}: {reason}", user=user,
            )
        entry = ledger.post_purchase_return(purchase_return, user=user)
        if entry is not None:
            PurchaseReturn.objects.filter(pk=purchase_return.pk).update(journal_entry=entry)
        record_audit(
            client=client_id, actor=user, action="qc_purchase_return", entity_type="PurchaseReturn",
            entity_id=purchase_return.id, entity_label=number,
            description=f"QC-rejected rolls returned on {number}: {reason}",
            after={"rolls": [{"taka": t.taka_id, "qty": str(q), "batch": t.batch.batch_code if t.batch_id else None}
                             for t, q, _ in rows]},
        )
        returns.append(purchase_return)

    if loss_id:
        from apps.inventory.batch_services import update_loss
        from apps.inventory.models import BatchLoss

        loss = BatchLoss.objects.filter(client_id=client_id, pk=loss_id).first()
        if loss is not None:
            update_loss(loss, user=user, outcome="VENDOR_RETURN",
                        reference=", ".join(r.return_number for r in returns))
    return returns
