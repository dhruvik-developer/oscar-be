"""
Factory fabric flows on one purchase order.

    Flow A  FACTORY_TO_OSCAR  factory -> OSCAR warehouse
    Flow B  FACTORY_TO_MILL   factory -> dyeing mill -> OSCAR warehouse

A purchase order is the order; a batch is fabric that actually arrived. So:

* Creating or approving the PO never opens a batch or posts stock.
* Factory dispatches are ``SupplierDelivery`` rows. A dispatch to a dyeing mill
  (Flow B) is traced only -- it never posts OSCAR stock or opens a grey batch.
* The mill's receipt, processing and dispatch on to OSCAR are
  ``PurchaseOrderMillProcess`` rows, again traceability only.
* Every actual receipt at OSCAR is an ordinary grey stock GRN
  (``services.create_standalone_grn``): the fabric lands at QC Hold and only
  what QC accepts becomes available stock, through the stock ledger. Each
  received line opens its own batch (Grey for Flow A, Dyed for Flow B) linked
  to the PO, the PO line and the receipt (``batch_services.assign_batch``).
* Every cost of the order (yarn, manufacturing, dyeing, freight, other) is a
  ``PurchaseOrderExpense`` on the same PO. ``allocate_costs`` books the
  received fabric's own value and each expense's share to the batches as lot
  cost entries, recorded in ``PurchaseOrderCostAllocation`` so a cost is never
  charged twice and every share is traceable to its expense.
* Quantity differences are worked out here but never classified here: a user
  splits them into pending / loss / waste / reprocess / adjustment
  (``PurchaseOrderDiscrepancy``). Nothing in this module classifies a quantity
  on its own, and no classification moves stock.

All quantities and costs are computed on the server; the screen only shows them.
"""
import json
import re
from collections import defaultdict
from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date

from apps.core.exceptions import BusinessRuleViolation, Conflict, ValidationFailed
from apps.core.money import ZERO, D, round2, round4

from .models import (
    FABRIC_FLOW_FACTORY_TO_MILL,
    FABRIC_FLOW_FACTORY_TO_OSCAR,
    FABRIC_FLOWS,
    PURCHASE_TYPE_GREY_STOCK,
    GoodsReceiptLine,
    PurchaseOrder,
    PurchaseOrderCostAllocation,
    PurchaseOrderDiscrepancy,
    PurchaseOrderExpense,
    PurchaseOrderMillProcess,
    PurchaseOrderYarnLine,
    SupplierDelivery,
)

TOLERANCE = Decimal("0.001")
#: PO statuses in which the factory may dispatch and OSCAR may receive.
ACTIVE_STATUSES = ("Approved", "Issued", "Partially Received", "Fully Received")
#: Expense category -> lot cost ledger type (the batch's cost breakdown).
LOT_COST_TYPE = {
    PurchaseOrderExpense.CATEGORY_MANUFACTURING: "Machine / production expense",
    PurchaseOrderExpense.CATEGORY_YARN: "Yarn / raw material",
    PurchaseOrderExpense.CATEGORY_DYEING: "Dyeing/Printing/Finishing",
    PurchaseOrderExpense.CATEGORY_FREIGHT: "Purchase freight/loading",
    PurchaseOrderExpense.CATEGORY_OTHER: "Other direct",
}
FABRIC_COST_TYPE = "Grey purchase"
ALLOCATION_SOURCE = "PurchaseOrderCost"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _get(data, *keys, default=None):
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return data[key]
    return default


def _text(data, *keys):
    value = _get(data, *keys)
    return str(value).strip() or None if value is not None else None


def _date(value, field, required=False):
    if value in (None, ""):
        if required:
            raise ValidationFailed("Enter the date.", field_errors={field: ["Required."]})
        return None
    if hasattr(value, "year"):
        return value
    parsed = parse_date(str(value)[:10])
    if parsed is None:
        raise ValidationFailed("Dates must be YYYY-MM-DD.", field_errors={field: ["Invalid date."]})
    return parsed


def _qty(value, field, *, required=True, allow_zero=False):
    if value in (None, ""):
        if required:
            raise ValidationFailed("Enter the quantity.", field_errors={field: ["Required."]})
        return None
    try:
        qty = round4(D(value))
    except Exception:
        raise ValidationFailed("Quantities must be numbers.", field_errors={field: ["Not a number."]})
    if qty < ZERO or (qty == ZERO and not allow_zero):
        raise ValidationFailed("Quantities must be above zero.", field_errors={field: ["Must be above 0."]})
    return qty


def _money(value, field, *, required=True):
    if value in (None, ""):
        if required:
            raise ValidationFailed("Enter the amount.", field_errors={field: ["Required."]})
        return None
    try:
        amount = round2(D(value))
    except Exception:
        raise ValidationFailed("Amounts must be numbers.", field_errors={field: ["Not a number."]})
    if amount < ZERO:
        raise ValidationFailed("Amounts cannot be negative.", field_errors={field: ["Must be 0 or more."]})
    return amount


def _party(client_id, party_id, field):
    from apps.masters.models import Party

    if not party_id:
        return None
    party = Party.objects.filter(client_id=client_id, pk=party_id, deleted_at__isnull=True).first()
    if party is None:
        raise ValidationFailed("That party was not found.", field_errors={field: ["Not found."]})
    return party


def _item(client_id, item_id, field):
    from apps.masters.models import Item

    if not item_id:
        return None
    item = Item.objects.filter(client_id=client_id, pk=item_id, deleted_at__isnull=True).first()
    if item is None:
        raise ValidationFailed("That item was not found.", field_errors={field: ["Not found."]})
    return item


def _user(user):
    return user if getattr(user, "is_authenticated", False) else None


def _audit(po, user, action, description, after=None):
    from apps.core.audit import record_audit

    record_audit(
        client=po.client_id, actor=user, action=action, entity_type="PurchaseOrder",
        entity_id=po.id, entity_label=po.po_number, description=description, after=after,
    )


def _live(qs):
    return qs.filter(deleted_at__isnull=True)


def lock_order(po):
    """Re-read the PO under a row lock: every write on a fabric flow takes it,
    so two saves at once cannot both pass a quantity check."""
    return PurchaseOrder.objects.select_for_update().get(pk=po.pk)


def require_flow(po, flow=None):
    if not po.fabric_flow:
        raise BusinessRuleViolation(
            f"{po.po_number} is not a factory fabric order.", code="NOT_A_FABRIC_FLOW_PO",
        )
    if flow and po.fabric_flow != flow:
        label = dict(FABRIC_FLOWS)[flow]
        raise BusinessRuleViolation(f"This step is only for {label} orders.", code="WRONG_FABRIC_FLOW")


def require_active(po):
    if po.status not in ACTIVE_STATUSES:
        raise Conflict(
            f"{po.po_number} is {po.status}; approve the PO before recording dispatches or receipts.",
            code="PO_NOT_APPROVED",
        )


# ---------------------------------------------------------------------------
# PO header: flow, mill, yarn lines and costs entered with the order
# ---------------------------------------------------------------------------
def validate_flow(value):
    if value in (None, ""):
        return None
    if value not in dict(FABRIC_FLOWS):
        raise ValidationFailed("Choose how the fabric reaches OSCAR.", field_errors={"fabricFlow": ["Unknown flow."]})
    return value


def apply_order_extras(po, data, *, user=None, creating=False):
    """Yarn lines and opening cost entries sent with the Create / Edit PO form.

    ``yarnLines`` replaces the PO's yarn plan when present. ``expenses`` adds
    cost entries only when the PO is created (later costs use the expense
    endpoints, so an edit can never duplicate them)."""
    if not po.fabric_flow:
        return
    if "yarnLines" in data:
        replace_yarn_lines(po, data.get("yarnLines") or [], user=user)
    if creating:
        for row in data.get("expenses") or []:
            add_expense(po, row, user=user, allocate=False)


# ---------------------------------------------------------------------------
# Yarn plan
# ---------------------------------------------------------------------------
def _yarn_fields(client_id, row, index):
    field = f"yarnLines[{index}]"
    item = _item(client_id, _get(row, "itemId", "item"), field)
    quality = _text(row, "yarnQuality", "quality")
    if item is None and not quality:
        raise ValidationFailed(
            f"Yarn line {index + 1}: choose the yarn or type its quality.",
            field_errors={field: ["Yarn is required."]},
        )
    required_qty = _qty(_get(row, "requiredQty", "quantity"), field, required=False, allow_zero=True) or ZERO
    rate = _money(_get(row, "rate"), field, required=False) or ZERO
    amount = _money(_get(row, "amount"), field, required=False)
    if amount is None:
        amount = round2(required_qty * rate)
    actual = _qty(_get(row, "actualQtyUsed"), field, required=False, allow_zero=True)
    supplier = _party(client_id, _get(row, "supplierId"), field)
    phone = _text(row, "supplierPhone")
    if phone and not re.fullmatch(r"\+?[0-9][0-9 -]{5,18}[0-9]", phone):
        raise ValidationFailed(
            f"Yarn line {index + 1}: enter a valid contact number (digits, + - and spaces).",
            field_errors={field: ["Invalid contact number."]},
        )
    return {
        "item": item,
        "yarn_quality": quality or (item.name if item else None),
        "supplier": supplier,
        "supplier_name": _text(row, "supplierName") or (supplier.name if supplier else None),
        "supplier_phone": phone,
        "yarn_lot": _text(row, "yarnLot", "lot"),
        "required_qty": required_qty,
        "uom": _text(row, "uom") or (item.uom if item else None) or "Kg",
        "rate": rate,
        "amount": amount,
        "actual_qty_used": actual,
        "remarks": _text(row, "remarks"),
    }


@transaction.atomic
def replace_yarn_lines(po, rows, *, user=None):
    """The PO's yarn plan becomes ``rows``. Planned yarn is never consumed
    stock -- issuing yarn stays with the yarn / production stock flows."""
    require_flow(po)
    fields = [_yarn_fields(po.client_id, row, i) for i, row in enumerate(rows)]
    now = timezone.now()
    _live(po.yarn_lines).update(deleted_at=now, deleted_by=_user(user))
    for f in fields:
        PurchaseOrderYarnLine.objects.create(client_id=po.client_id, purchase_order=po, created_by=_user(user), **f)
    return list(_live(po.yarn_lines))


# ---------------------------------------------------------------------------
# Factory dispatch (Flow A: to OSCAR, Flow B: to the dyeing mill)
# ---------------------------------------------------------------------------
@transaction.atomic
def record_dispatch(po, data, *, user=None):
    from apps.core.numbering import allocate_number

    po = lock_order(po)
    require_flow(po)
    require_active(po)
    to_mill = po.fabric_flow == FABRIC_FLOW_FACTORY_TO_MILL
    qty = _qty(_get(data, "dispatchedQty", "quantity"), "dispatchedQty")
    dispatch_date = _date(_get(data, "dispatchDate", "date"), "dispatchDate", required=True)
    mill = None
    if to_mill:
        mill = _party(po.client_id, _get(data, "millId"), "millId") or po.dyeing_mill
        if mill is None:
            raise ValidationFailed("Choose the dyeing mill the fabric went to.", field_errors={"millId": ["Required."]})
    ordered = ordered_qty(po)
    already = dispatched_qty(po)
    if already + qty > ordered + TOLERANCE:
        raise ValidationFailed(
            f"The factory would have dispatched {already + qty} against {ordered} ordered.",
            field_errors={"dispatchedQty": [f"Only {max(ZERO, ordered - already)} is left to dispatch."]},
        )
    delivery = SupplierDelivery.objects.create(
        client_id=po.client_id,
        delivery_number=allocate_number(po.client_id, "SD", dispatch_date),
        purchase_order=po,
        dispatch_date=dispatch_date,
        dispatched_qty=qty,
        supplier_challan_no=_text(data, "challanNo", "supplierChallanNo"),
        supplier_challan_date=_date(_get(data, "challanDate", "supplierChallanDate"), "challanDate"),
        expected_delivery_date=_date(_get(data, "expectedDeliveryDate"), "expectedDeliveryDate"),
        transporter_name=_text(data, "transporterName", "transporter"),
        vehicle_number=_text(data, "vehicleNumber"),
        lr_bilty_number=_text(data, "lrBiltyNumber", "lrNumber"),
        remarks=_text(data, "remarks"),
        destination="MILL" if to_mill else "OSCAR",
        mill=mill,
        status="Dispatched",
        created_by=_user(user),
    )
    # The PO status is left alone: it follows the receipts (Partially / Fully
    # Received), and the GRN only accepts an approved order.
    where =f"dyeing mill {mill.name}" if mill else "OSCAR"
    _audit(po, user, "fabric_dispatch", f"Factory dispatched {qty} to {where} ({delivery.delivery_number}).")
    return delivery


# ---------------------------------------------------------------------------
# Dyeing mill (Flow B)
# ---------------------------------------------------------------------------
def _mill_fields(po, data, current=None):
    """Model fields from the mill form; on an update only the keys sent."""
    fields = {}
    if current is None or "millId" in data:
        fields["mill"] = _party(po.client_id, _get(data, "millId"), "millId") or (current.mill if current else po.dyeing_mill)
        if fields["mill"] is None:
            raise ValidationFailed("Choose the dyeing mill.", field_errors={"millId": ["Required."]})
    if current is None or "deliveryId" in data:
        delivery_id = _get(data, "deliveryId")
        delivery = None
        if delivery_id:
            delivery = _live(po.deliveries).filter(pk=delivery_id, destination="MILL").first()
            if delivery is None:
                raise ValidationFailed("Pick a dispatch made to the mill.", field_errors={"deliveryId": ["Not found."]})
        fields["delivery"] = delivery
    for key, field, kind in (
        ("millReceiptDate", "mill_receipt_date", "date_required"),
        ("millDispatchDate", "mill_dispatch_date", "date"),
        ("qtyReceived", "qty_received", "qty"),
        ("processedQty", "processed_qty", "qty0"),
        ("millDispatchedQty", "mill_dispatched_qty", "qty0"),
        ("processType", "process_type", "text"),
        ("shade", "shade", "text"),
        ("processDetails", "process_details", "text"),
        ("millChallanNo", "mill_challan_no", "text"),
        ("reference", "reference", "text"),
        ("remarks", "remarks", "text"),
    ):
        if current is not None and key not in data:
            continue
        value = data.get(key)
        if kind == "date_required":
            fields[field] = _date(value, key, required=True)
        elif kind == "date":
            fields[field] = _date(value, key)
        elif kind == "qty":
            fields[field] = _qty(value, key)
        elif kind == "qty0":
            fields[field] = _qty(value, key, required=False, allow_zero=True) or ZERO
        else:
            fields[field] = (str(value).strip() or None) if value not in (None, "") else None
    if "process_type" in fields and not fields["process_type"]:
        fields["process_type"] = "Dyeing"
    return fields


def _check_mill_quantities(po, record):
    received = D(record.qty_received)
    if D(record.processed_qty) > received + TOLERANCE:
        raise ValidationFailed(
            "The mill cannot process more than it received.",
            field_errors={"processedQty": [f"At most {received}."]},
        )
    if D(record.mill_dispatched_qty) > D(record.processed_qty) + TOLERANCE:
        raise ValidationFailed(
            "The mill cannot dispatch more than it processed.",
            field_errors={"millDispatchedQty": [f"At most {record.processed_qty}."]},
        )
    others = _live(po.mill_processes).exclude(pk=record.pk)
    total_received = sum((D(r.qty_received) for r in others), ZERO) + received
    to_mill = dispatched_qty(po)
    if total_received > to_mill + TOLERANCE:
        raise ValidationFailed(
            f"The mill would have received {total_received}, but the factory dispatched {to_mill} to the mill.",
            field_errors={"qtyReceived": [f"Record the factory's dispatch first; {max(ZERO, to_mill - (total_received - received))} is open."]},
        )


@transaction.atomic
def record_mill_process(po, data, *, user=None):
    po = lock_order(po)
    require_flow(po, FABRIC_FLOW_FACTORY_TO_MILL)
    require_active(po)
    record = PurchaseOrderMillProcess(client_id=po.client_id, purchase_order=po, created_by=_user(user))
    for field, value in _mill_fields(po, data).items():
        setattr(record, field, value)
    _check_mill_quantities(po, record)
    record.save()
    _audit(po, user, "mill_receipt", f"Dyeing mill {record.mill.name} received {record.qty_received}.")
    return record


@transaction.atomic
def update_mill_process(po, record_id, data, *, user=None):
    po = lock_order(po)
    require_flow(po, FABRIC_FLOW_FACTORY_TO_MILL)
    record = _live(po.mill_processes).filter(pk=record_id).first()
    if record is None:
        raise ValidationFailed("Mill record not found.", field_errors={"id": ["Not found."]})
    for field, value in _mill_fields(po, data, current=record).items():
        setattr(record, field, value)
    _check_mill_quantities(po, record)
    record.updated_by = _user(user)
    record.save()
    _audit(po, user, "mill_update",
           f"Mill record updated: processed {record.processed_qty}, dispatched {record.mill_dispatched_qty}.")
    return record


# ---------------------------------------------------------------------------
# Receipts at OSCAR
# ---------------------------------------------------------------------------
@transaction.atomic
def receive(po, data, *, user):
    """Record fabric that actually arrived at OSCAR, as a grey stock GRN.

    Flow A receives the factory's grey fabric; Flow B receives the dyed fabric
    from the mill (a line may name the dyed fabric item if it is stocked under
    its own SKU). The fabric lands at QC Hold -- only QC-accepted metres
    become available stock -- and each received line opens its own batch.
    """
    from . import services

    po = lock_order(po)
    require_flow(po)
    require_active(po)
    lines = data.get("lines") or []
    if not lines:
        raise ValidationFailed("Enter at least one received line.", field_errors={"lines": ["Required."]})
    incoming = sum((_qty(_get(row, "receivedQty", "quantity"), f"lines[{i}].receivedQty") for i, row in enumerate(lines)), ZERO)
    if po.fabric_flow == FABRIC_FLOW_FACTORY_TO_MILL:
        sent_to_mill = dispatched_qty(po)
        received = received_qty(po)
        if received + incoming > sent_to_mill + TOLERANCE:
            raise ValidationFailed(
                f"OSCAR would have received {received + incoming} of dyed fabric, but only {sent_to_mill} "
                "was dispatched from the factory to the mill.",
                field_errors={"lines": ["Record the factory's dispatch to the mill first."]},
            )
    payload = []
    for i, row in enumerate(lines):
        po_line_id = _get(row, "poLineId", "purchaseOrderLineId")
        if not po_line_id:
            raise ValidationFailed(f"Line {i + 1}: pick the PO fabric line.", field_errors={f"lines[{i}].poLineId": ["Required."]})
        if not _live(po.line_items).filter(pk=po_line_id).exists():
            raise ValidationFailed(f"Line {i + 1}: that line is not on {po.po_number}.",
                                   field_errors={f"lines[{i}].poLineId": ["Not on this PO."]})
        payload.append({
            "poLineId": po_line_id,
            "itemId": _get(row, "itemId"),
            "receivedQty": _get(row, "receivedQty", "quantity"),
            "unitCost": _get(row, "rate", "unitCost"),
            "takas": row.get("takas") or row.get("rolls") or [],
            "fabricWidth": _get(row, "fabricWidth", "width"),
            "remarks": _text(row, "remarks"),
        })
    receipt = services.create_standalone_grn(
        po.client,
        party_id=po.party_id,
        location_id=_get(data, "locationId", "location"),
        receipt_date=_get(data, "receiptDate", "date"),
        supplier_challan_no=_text(data, "challanNo", "supplierChallanNo"),
        supplier_challan_date=_get(data, "challanDate", "supplierChallanDate"),
        purchase_order_id=po.id,
        lines_payload=payload,
        user=user,
    )
    allocate_costs(po, user=user)
    _audit(po, user, "fabric_receipt", f"Received {incoming} at OSCAR on {receipt.grn_number}.")
    return receipt


# ---------------------------------------------------------------------------
# Costs & expenses
# ---------------------------------------------------------------------------
def _expense_fields(po, data, current=None):
    fields = {}

    def has(*keys):
        return current is None or any(k in data for k in keys)

    if has("category"):
        category = _get(data, "category")
        if category not in dict(PurchaseOrderExpense.CATEGORIES):
            raise ValidationFailed("Choose the cost category.", field_errors={"category": ["Unknown category."]})
        if category == PurchaseOrderExpense.CATEGORY_DYEING and po.fabric_flow != FABRIC_FLOW_FACTORY_TO_MILL:
            raise ValidationFailed("Dyeing costs belong to Factory -> Dyeing Mill orders.",
                                   field_errors={"category": ["Not for this flow."]})
        fields["category"] = category
    if has("expenseType"):
        fields["expense_type"] = _text(data, "expenseType") or dict(PurchaseOrderExpense.CATEGORIES)[
            fields.get("category") or current.category]
    if has("quantity"):
        fields["quantity"] = _qty(_get(data, "quantity"), "quantity", required=False, allow_zero=True)
    if has("rate"):
        fields["rate"] = _money(_get(data, "rate"), "rate", required=False)
    if has("amount", "quantity", "rate"):
        amount = _money(_get(data, "amount"), "amount", required=False)
        quantity = fields.get("quantity", getattr(current, "quantity", None))
        rate = fields.get("rate", getattr(current, "rate", None))
        if amount is None and quantity is not None and rate is not None:
            amount = round2(D(quantity) * D(rate))
        if amount is None:
            amount = getattr(current, "amount", None)
        if amount is None or amount <= ZERO:
            raise ValidationFailed("Enter the amount (or quantity and rate).", field_errors={"amount": ["Must be above 0."]})
        fields["amount"] = amount
    if has("expenseDate", "date"):
        fields["expense_date"] = _date(_get(data, "expenseDate", "date"), "expenseDate") or timezone.localdate()
    if has("status"):
        status = _get(data, "status", default="ESTIMATED")
        if status not in dict(PurchaseOrderExpense.STATUSES):
            raise ValidationFailed("Status is Estimated or Actual.", field_errors={"status": ["ESTIMATED or ACTUAL."]})
        fields["status"] = status
    if has("allocationBasis"):
        basis = _get(data, "allocationBasis", default="QUANTITY")
        if basis not in dict(PurchaseOrderExpense.BASES):
            raise ValidationFailed("Allocation basis is QUANTITY or VALUE.", field_errors={"allocationBasis": ["Unknown."]})
        fields["allocation_basis"] = basis
    if has("poLineId"):
        line_id = _get(data, "poLineId")
        line = None
        if line_id:
            line = _live(po.line_items).filter(pk=line_id).first()
            if line is None:
                raise ValidationFailed("That fabric line is not on this PO.", field_errors={"poLineId": ["Not found."]})
        fields["purchase_order_line"] = line
    if has("partyId"):
        fields["party"] = _party(po.client_id, _get(data, "partyId"), "partyId")
    for key, field in (("description", "description"), ("billReference", "bill_reference"), ("remarks", "remarks")):
        if has(key):
            fields[field] = _text(data, key)
    return fields


@transaction.atomic
def add_expense(po, data, *, user=None, allocate=True):
    po = lock_order(po)
    require_flow(po)
    if po.status in ("Cancelled", "Rejected"):
        raise Conflict(f"{po.po_number} is {po.status}.", code="PO_CLOSED")
    reference = _text(data, "billReference")
    category = _get(data, "category")
    if reference and _live(po.fabric_expenses).filter(bill_reference=reference, category=category).exists():
        raise Conflict(
            f"Bill {reference} is already recorded on {po.po_number} as a {category.lower()} cost.",
            code="DUPLICATE_EXPENSE",
        )
    expense = PurchaseOrderExpense(client_id=po.client_id, purchase_order=po, created_by=_user(user))
    for field, value in _expense_fields(po, data).items():
        setattr(expense, field, value)
    expense.save()
    _audit(po, user, "po_expense_add", f"{expense.get_category_display()} cost {expense.amount} added.",
           after={"expenseId": str(expense.id), "amount": str(expense.amount)})
    if allocate:
        allocate_costs(po, user=user)
    return expense


@transaction.atomic
def update_expense(po, expense_id, data, *, user=None):
    po = lock_order(po)
    require_flow(po)
    expense = _live(po.fabric_expenses).filter(pk=expense_id).first()
    if expense is None:
        raise ValidationFailed("Cost entry not found.", field_errors={"id": ["Not found."]})
    before = str(expense.amount)
    for field, value in _expense_fields(po, data, current=expense).items():
        setattr(expense, field, value)
    expense.updated_by = _user(user)
    expense.save()
    _audit(po, user, "po_expense_update", f"{expense.get_category_display()} cost updated {before} -> {expense.amount}.")
    allocate_costs(po, user=user)
    return expense


@transaction.atomic
def delete_expense(po, expense_id, *, user=None):
    po = lock_order(po)
    require_flow(po)
    expense = _live(po.fabric_expenses).filter(pk=expense_id).first()
    if expense is None:
        raise ValidationFailed("Cost entry not found.", field_errors={"id": ["Not found."]})
    expense.deleted_at = timezone.now()
    expense.deleted_by = _user(user)
    expense.save(update_fields=["deleted_at", "deleted_by", "updated_at"])
    _audit(po, user, "po_expense_delete", f"{expense.get_category_display()} cost {expense.amount} removed.")
    allocate_costs(po, user=user)


# ---------------------------------------------------------------------------
# Quantities
# ---------------------------------------------------------------------------
def ordered_qty(po):
    return round4(sum((D(l.qty) for l in _live(po.line_items)), ZERO))


def dispatched_qty(po):
    """What the factory dispatched for this flow (to OSCAR, or to the mill)."""
    destination = "MILL" if po.fabric_flow == FABRIC_FLOW_FACTORY_TO_MILL else "OSCAR"
    return round4(sum((D(d.dispatched_qty) for d in _live(po.deliveries).filter(destination=destination)), ZERO))


def _receipt_lines(po):
    return list(
        GoodsReceiptLine.objects.filter(
            goods_receipt__purchase_order=po, goods_receipt__deleted_at__isnull=True, deleted_at__isnull=True,
        ).select_related("goods_receipt", "item", "purchase_order_line").order_by("goods_receipt__receipt_date", "created_at")
    )


def received_qty(po):
    return round4(sum((D(l.received_qty) for l in _receipt_lines(po)), ZERO))


def _qc_totals(receipt):
    """QC result of one receipt from its recorded disposition."""
    try:
        meta = json.loads(receipt.qc_note or "{}")
    except (TypeError, ValueError):
        meta = {}
    cumulative = meta.get("cumulative") or {}
    return {k: round4(D(str(cumulative.get(k, 0) or 0))) for k in ("good", "second", "reprocess", "reject")}


def discrepancy_totals(po):
    totals = defaultdict(lambda: ZERO)
    for entry in _live(po.discrepancies):
        totals[entry.category] += D(entry.quantity)
    return totals


def quantities(po, receipt_lines=None):
    lines = receipt_lines if receipt_lines is not None else _receipt_lines(po)
    ordered = ordered_qty(po)
    received = round4(sum((D(l.received_qty) for l in lines), ZERO))
    accepted = second = reprocess = rejected = ZERO
    for receipt in {l.goods_receipt_id: l.goods_receipt for l in lines}.values():
        qc = _qc_totals(receipt)
        accepted += qc["good"]
        second += qc["second"]
        reprocess += qc["reprocess"]
        rejected += qc["reject"]
    awaiting_qc = max(ZERO, received - accepted - second - reprocess - rejected)
    classified = discrepancy_totals(po)
    classified_total = sum(classified.values(), ZERO)
    written_off = sum((classified[c] for c in PurchaseOrderDiscrepancy.WRITTEN_OFF), ZERO)
    data = {
        "ordered": ordered,
        "receivedAtOscar": received,
        "accepted": round4(accepted),
        "secondQuality": round4(second),
        "reprocessAtQc": round4(reprocess),
        "rejectedAtQc": round4(rejected),
        "awaitingQc": round4(awaiting_qc),
        "pending": round4(max(ZERO, ordered - received - written_off)),
    }
    if po.fabric_flow == FABRIC_FLOW_FACTORY_TO_MILL:
        mills = list(_live(po.mill_processes))
        sent = dispatched_qty(po)
        data.update({
            "dispatchedToMill": sent,
            "receivedAtMill": round4(sum((D(m.qty_received) for m in mills), ZERO)),
            "processed": round4(sum((D(m.processed_qty) for m in mills), ZERO)),
            "dispatchedByMill": round4(sum((D(m.mill_dispatched_qty) for m in mills), ZERO)),
        })
        basis_label = "Dispatched by the factory to the mill"
    else:
        sent = dispatched_qty(po)
        data["dispatchedToOscar"] = sent
        basis_label = "Dispatched by the factory to OSCAR"
    difference = round4(max(ZERO, sent - received))
    data["discrepancy"] = {
        "basisLabel": basis_label,
        "basisQty": sent,
        "receivedQty": received,
        "difference": difference,
        "classified": {code: str(round4(classified[code])) for code, _ in PurchaseOrderDiscrepancy.CATEGORIES},
        "classifiedTotal": round4(classified_total),
        "unresolved": round4(max(ZERO, difference - classified_total)),
        # Fabric classified as pending that has since arrived: release the
        # pending entry -- the system does not do it on its own.
        "overClassified": round4(max(ZERO, classified_total - difference)),
        "writtenOff": round4(written_off),
    }
    return data


# ---------------------------------------------------------------------------
# Discrepancies (classified by a user, never by the system)
# ---------------------------------------------------------------------------
@transaction.atomic
def classify_discrepancy(po, data, *, user=None):
    """Split part of the unresolved difference into one or more categories.

    ``data`` is either one entry or ``{"entries": [...]}``; all entries are
    checked together so the split can never exceed the unresolved quantity.
    """
    po = lock_order(po)
    require_flow(po)
    rows = data.get("entries") if isinstance(data.get("entries"), list) else [data]
    if not rows:
        raise ValidationFailed("Enter at least one quantity to classify.", field_errors={"entries": ["Required."]})
    entry_date = _date(_get(data, "date", "entryDate"), "date") or timezone.localdate()
    parsed = []
    for i, row in enumerate(rows):
        category = _get(row, "category")
        if category not in dict(PurchaseOrderDiscrepancy.CATEGORIES):
            raise ValidationFailed(f"Entry {i + 1}: choose pending, loss, waste, reprocess or adjustment.",
                                   field_errors={f"entries[{i}].category": ["Unknown category."]})
        reason = _text(row, "reason") or _text(data, "reason")
        if not reason:
            raise ValidationFailed(f"Entry {i + 1}: give the reason.", field_errors={f"entries[{i}].reason": ["Required."]})
        parsed.append((category, _qty(_get(row, "quantity", "qty"), f"entries[{i}].quantity"), reason, row))
    total = sum((q for _, q, _, _ in parsed), ZERO)
    unresolved = quantities(po)["discrepancy"]["unresolved"]
    if total > unresolved + TOLERANCE:
        raise ValidationFailed(
            f"Only {unresolved} is unresolved; the entries add up to {total}.",
            field_errors={"entries": [f"Classify at most {unresolved}."]},
        )
    created = []
    for category, qty, reason, row in parsed:
        created.append(PurchaseOrderDiscrepancy.objects.create(
            client_id=po.client_id, purchase_order=po, category=category, quantity=qty, reason=reason,
            entry_date=_date(_get(row, "date"), "date") or entry_date,
            reference=_text(row, "reference") or _text(data, "reference"),
            remarks=_text(row, "remarks") or _text(data, "remarks"),
            created_by=_user(user),
        ))
    _audit(po, user, "po_discrepancy",
           "Quantity difference classified: " + ", ".join(f"{q} {c.lower()}" for c, q, _, _ in parsed),
           after={"entries": [{"category": c, "quantity": str(q)} for c, q, _, _ in parsed]})
    allocate_costs(po, user=user)
    return created


@transaction.atomic
def release_discrepancy(po, entry_id, *, user=None, reason=None):
    """Undo a classification (e.g. pending fabric that has now arrived)."""
    po = lock_order(po)
    require_flow(po)
    entry = _live(po.discrepancies).filter(pk=entry_id).first()
    if entry is None:
        raise ValidationFailed("Entry not found.", field_errors={"id": ["Not found."]})
    entry.deleted_at = timezone.now()
    entry.deleted_by = _user(user)
    entry.save(update_fields=["deleted_at", "deleted_by", "updated_at"])
    _audit(po, user, "po_discrepancy_release",
           f"Released {entry.quantity} {entry.category.lower()}" + (f": {reason}" if reason else "."))
    allocate_costs(po, user=user)


# ---------------------------------------------------------------------------
# Cost allocation to batches
# ---------------------------------------------------------------------------
def _line_batches(lines):
    from apps.inventory.models import Batch

    batches = Batch.objects.filter(goods_receipt_line__in=lines, deleted_at__isnull=True)
    return {b.goods_receipt_line_id: b for b in batches}


def _expected_basis(po, expense, lines, written_off):
    """What the expense is spread over: the quantity (or fabric value) the
    order is expected to deliver -- ordered, less what a user wrote off as
    loss / waste / adjustment -- or what actually arrived, if that is more.
    A cost entered early therefore keeps the same per-metre share as fabric
    arrives, and the share of fabric still to come stays unallocated."""
    scope_lines = [expense.purchase_order_line] if expense.purchase_order_line_id else list(_live(po.line_items))
    po_ordered = ordered_qty(po)
    ordered = sum((D(l.qty) for l in scope_lines), ZERO)
    share_written_off = written_off * ordered / po_ordered if po_ordered > ZERO else ZERO
    expected_qty = max(ZERO, ordered - share_written_off)
    if expense.allocation_basis == "VALUE":
        ordered_value = sum((D(l.qty) * D(l.rate) for l in scope_lines), ZERO)
        expected = ordered_value * (expected_qty / ordered) if ordered > ZERO else ZERO
        actual = sum((D(l.received_qty) * D(l.unit_cost) for l in lines), ZERO)
    else:
        expected = expected_qty
        actual = sum((D(l.received_qty) for l in lines), ZERO)
    return max(expected, actual), actual


def _desired_allocations(po):
    """{(expense_id | None, grn_line_id): (amount, basis_qty, basis_total, batch, line, cost_type, label)}"""
    lines = _receipt_lines(po)
    batches = _line_batches(lines)
    lines = [l for l in lines if l.id in batches]
    desired = {}
    # The received fabric itself: what was received x the receipt rate.
    for line in lines:
        amount = round2(D(line.received_qty) * D(line.unit_cost))
        if amount > ZERO:
            desired[(None, line.id)] = (amount, D(line.received_qty), D(line.received_qty), batches[line.id], line,
                                        FABRIC_COST_TYPE, "Fabric received")
    written_off = sum((v for c, v in discrepancy_totals(po).items() if c in PurchaseOrderDiscrepancy.WRITTEN_OFF), ZERO)
    for expense in _live(po.fabric_expenses).select_related("purchase_order_line"):
        scoped = [l for l in lines if not expense.purchase_order_line_id or l.purchase_order_line_id == expense.purchase_order_line_id]
        if not scoped:
            continue
        total, actual = _expected_basis(po, expense, scoped, written_off)
        if total <= ZERO:
            continue
        amount = D(expense.amount)
        allotted = ZERO
        for index, line in enumerate(scoped):
            basis = D(line.received_qty) if expense.allocation_basis == "QUANTITY" else D(line.received_qty) * D(line.unit_cost)
            if index == len(scoped) - 1 and actual >= total - TOLERANCE:
                share = amount - allotted  # everything expected is in: the last line takes the rounding paisa
            else:
                share = round2(amount * basis / total)
            allotted += share
            if share > ZERO:
                desired[(expense.id, line.id)] = (share, round4(basis), round4(total), batches[line.id], line,
                                                  LOT_COST_TYPE[expense.category], expense.expense_type)
    return desired


@transaction.atomic
def allocate_costs(po, *, user=None):
    """Bring the batches' lot cost entries in line with the PO's costs.

    Idempotent: a share already booked with the same amount is left alone; a
    changed or no longer applicable share is reversed (its allocation and lot
    cost entry soft-deleted, so the history stays) and the new one booked.
    Running it twice never charges anything twice.
    """
    from apps.inventory import costing_services
    from apps.inventory.models import LotCostEntry

    po = lock_order(po)
    if not po.fabric_flow:
        return []
    desired = _desired_allocations(po)
    now = timezone.now()
    touched_lots = set()
    live = {(a.expense_id, a.goods_receipt_line_id): a for a in _live(po.cost_allocations)}
    for key, allocation in live.items():
        want = desired.get(key)
        if want is not None and round2(D(allocation.amount)) == round2(want[0]) and allocation.batch_id == want[3].id:
            desired.pop(key)
            continue
        allocation.deleted_at = now
        allocation.deleted_by = _user(user)
        allocation.save(update_fields=["deleted_at", "deleted_by", "updated_at"])
        if allocation.lot_cost_entry_id:
            LotCostEntry.objects.filter(pk=allocation.lot_cost_entry_id).update(deleted_at=now, deleted_by=_user(user))
        touched_lots.add(allocation.lot_number)
    for (expense_id, line_id), (amount, basis, total, batch, line, cost_type, label) in desired.items():
        entry = costing_services.post_lot_cost(
            client_id=po.client_id,
            lot_number=line.batch_number,
            cost_type=cost_type,
            amount=amount,
            quantity=line.received_qty,
            source_document_type=ALLOCATION_SOURCE,
            source_document_id=po.id,
            source_document_number=po.po_number,
            user=user,
            remarks=f"{label} -- {po.po_number} / {line.goods_receipt.grn_number}"
                    + ("" if expense_id is None else f" [share {basis} of {total}]"),
        )
        PurchaseOrderCostAllocation.objects.create(
            client_id=po.client_id, purchase_order=po, expense_id=expense_id, batch=batch,
            goods_receipt_line=line, lot_number=line.batch_number, basis_quantity=basis, basis_total=total,
            amount=amount, lot_cost_entry=entry, created_by=_user(user),
        )
        touched_lots.discard(line.batch_number)
    for lot in touched_lots:
        costing_services.recompute_lot_rate(po.client_id, lot)
    return list(_live(po.cost_allocations))


def cost_summary(po):
    lines = _receipt_lines(po)
    expenses = list(_live(po.fabric_expenses))
    allocations = list(_live(po.cost_allocations))
    fabric_value = round2(sum((D(l.received_qty) * D(l.unit_cost) for l in lines), ZERO))
    by_category = defaultdict(lambda: ZERO)
    for e in expenses:
        by_category[e.category] += D(e.amount)
    total = fabric_value + sum(by_category.values(), ZERO)
    allocated = round2(sum((D(a.amount) for a in allocations), ZERO))
    unallocated = round2(max(ZERO, total - allocated))
    estimated = [e for e in expenses if e.status == "ESTIMATED"]
    if not lines:
        status, note = "NOT_STARTED", "No fabric received yet; costs are allocated as receipts arrive."
    elif estimated or unallocated > Decimal("0.01"):
        reasons = []
        if estimated:
            reasons.append(f"{len(estimated)} cost(s) still estimated")
        if unallocated > Decimal("0.01"):
            reasons.append(f"{unallocated} not yet allocated (fabric still to arrive or unclassified difference)")
        status, note = "PROVISIONAL", "; ".join(reasons) + "."
    else:
        status, note = "COMPLETE", "All recorded costs are actual and allocated to the batches."
    order_value = round2(sum((D(l.qty) * D(l.rate) for l in _live(po.line_items)), ZERO))

    # Yarn cost resolution: check planned yarn lines and actual booked expenses
    yarn_lines = list(_live(po.yarn_lines))
    yarn_planned = round2(
        sum(((D(y.amount) if y.amount is not None else (D(y.required_qty or ZERO) * D(y.rate or ZERO))) for y in yarn_lines), ZERO)
    )
    yarn_expense = round2(by_category[PurchaseOrderExpense.CATEGORY_YARN])
    yarn_cost = yarn_expense if yarn_expense > ZERO else yarn_planned
    has_yarn_details = bool(yarn_lines or yarn_expense > ZERO)

    return {
        "orderValue": str(order_value),
        "fabricReceivedValue": str(fabric_value),
        "manufacturing": str(round2(fabric_value + by_category[PurchaseOrderExpense.CATEGORY_MANUFACTURING])),
        "manufacturingCharges": str(round2(by_category[PurchaseOrderExpense.CATEGORY_MANUFACTURING])),
        "yarn": str(round2(yarn_cost)),
        "yarnPlanned": str(yarn_planned),
        "yarnExpense": str(yarn_expense),
        "hasYarnDetails": has_yarn_details,
        "dyeing": str(round2(by_category[PurchaseOrderExpense.CATEGORY_DYEING])),
        "freight": str(round2(by_category[PurchaseOrderExpense.CATEGORY_FREIGHT])),
        "other": str(round2(by_category[PurchaseOrderExpense.CATEGORY_OTHER])),
        "totalRecorded": str(round2(total)),
        "allocated": str(allocated),
        "unallocated": str(unallocated),
        "landedCostStatus": status,
        "landedCostNote": note,
        "allocationRule": (
            "Each received line carries its own fabric value (received qty x rate). Every other cost is "
            "spread by quantity (or fabric value) over what the order is expected to deliver: ordered, less "
            "what was written off as loss / waste / adjustment. Fabric still to arrive keeps its share unallocated."
        ),
    }


# ---------------------------------------------------------------------------
# Read model
# ---------------------------------------------------------------------------
def _d(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


def _s(value):
    return None if value is None else str(value)


def serialize_yarn(y):
    return {
        "id": str(y.id), "itemId": _s(y.item_id), "item": y.item.name if y.item_id else None,
        "yarnQuality": y.yarn_quality, "supplierId": _s(y.supplier_id),
        "supplierName": y.supplier_name or (y.supplier.name if y.supplier_id else None),
        "supplierPhone": y.supplier_phone, "yarnLot": y.yarn_lot,
        "requiredQty": str(y.required_qty), "uom": y.uom, "rate": str(y.rate), "amount": str(y.amount),
        "actualQtyUsed": _s(y.actual_qty_used), "remarks": y.remarks,
    }


def serialize_delivery(d):
    return {
        "id": str(d.id), "deliveryNumber": d.delivery_number, "destination": d.destination,
        "millId": _s(d.mill_id), "millName": d.mill.name if d.mill_id else None,
        "dispatchDate": _d(d.dispatch_date), "dispatchedQty": str(d.dispatched_qty),
        "challanNo": d.supplier_challan_no, "challanDate": _d(d.supplier_challan_date),
        "transporterName": d.transporter_name, "vehicleNumber": d.vehicle_number,
        "lrBiltyNumber": d.lr_bilty_number, "status": d.status, "remarks": d.remarks,
    }


def serialize_mill(m):
    return {
        "id": str(m.id), "deliveryId": _s(m.delivery_id),
        "deliveryNumber": m.delivery.delivery_number if m.delivery_id else None,
        "millId": str(m.mill_id), "millName": m.mill.name, "millReceiptDate": _d(m.mill_receipt_date),
        "qtyReceived": str(m.qty_received), "processType": m.process_type, "shade": m.shade,
        "processDetails": m.process_details, "processedQty": str(m.processed_qty),
        "millDispatchDate": _d(m.mill_dispatch_date), "millDispatchedQty": str(m.mill_dispatched_qty),
        "millChallanNo": m.mill_challan_no, "reference": m.reference, "remarks": m.remarks,
    }


def serialize_expense(e, allocated=ZERO):
    return {
        "id": str(e.id), "category": e.category, "categoryLabel": e.get_category_display(),
        "expenseType": e.expense_type, "description": e.description, "partyId": _s(e.party_id),
        "partyName": e.party.name if e.party_id else None, "poLineId": _s(e.purchase_order_line_id),
        "quantity": _s(e.quantity), "rate": _s(e.rate), "amount": str(e.amount),
        "expenseDate": _d(e.expense_date), "billReference": e.bill_reference, "status": e.status,
        "allocationBasis": e.allocation_basis, "remarks": e.remarks,
        "allocated": str(round2(allocated)), "unallocated": str(round2(max(ZERO, D(e.amount) - allocated))),
    }


def serialize_discrepancy(x):
    return {
        "id": str(x.id), "category": x.category, "categoryLabel": x.get_category_display(),
        "quantity": str(x.quantity), "reason": x.reason, "date": _d(x.entry_date),
        "reference": x.reference, "remarks": x.remarks,
        "user": getattr(x.created_by, "name", None) or getattr(x.created_by, "email", None),
        "createdAt": _d(x.created_at),
    }


def summary(po):
    """Everything the PO screen shows for a factory fabric order."""
    require_flow(po)
    lines = _receipt_lines(po)
    batches = _line_batches(lines)
    allocations = list(_live(po.cost_allocations).select_related("batch"))
    by_line = defaultdict(lambda: ZERO)
    by_expense = defaultdict(lambda: ZERO)
    for a in allocations:
        by_line[a.goods_receipt_line_id] += D(a.amount)
        if a.expense_id:
            by_expense[a.expense_id] += D(a.amount)
    lines_per_receipt = defaultdict(int)
    for line in lines:
        lines_per_receipt[line.goods_receipt_id] += 1
    receipts = []
    for line in lines:
        batch = batches.get(line.id)
        receipt = line.goods_receipt
        receipts.append({
            "receiptId": str(receipt.id), "grnNumber": receipt.grn_number, "receiptDate": _d(receipt.receipt_date),
            "challanNo": receipt.supplier_challan_no, "qcStatus": receipt.qc_status,
            "lineId": str(line.id), "poLineId": _s(line.purchase_order_line_id), "item": line.item.name,
            "receivedQty": str(line.received_qty), "uom": line.uom, "rate": str(line.unit_cost),
            "fabricValue": str(round2(D(line.received_qty) * D(line.unit_cost))),
            # QC is recorded per receipt; a one-line receipt's result is the line's.
            "accepted": str(_qc_totals(receipt)["good"]) if lines_per_receipt[receipt.id] == 1 else None,
            "batchCode": batch.batch_code if batch else None, "fabricType": batch.fabric_type if batch else None,
            "allocatedCost": str(round2(by_line[line.id])),
            "landedRate": str(round4(by_line[line.id] / D(line.received_qty))) if D(line.received_qty) > ZERO else None,
        })
    po_lines = [{
        "id": str(l.id), "itemId": _s(l.item_id), "item": l.item_name or (l.item.name if l.item_id else None),
        "qty": str(l.qty), "uom": l.uom, "rate": str(l.rate), "receivedQty": str(l.received_qty),
    } for l in _live(po.line_items).select_related("item").order_by("line_no")]
    return {
        "purchaseOrderId": str(po.id),
        "poNumber": po.po_number,
        "status": po.status,
        "fabricFlow": po.fabric_flow,
        "fabricFlowLabel": dict(FABRIC_FLOWS)[po.fabric_flow],
        "factoryId": _s(po.party_id),
        "factoryName": po.party.name if po.party_id else None,
        "dyeingMillId": _s(po.dyeing_mill_id),
        "dyeingMillName": po.dyeing_mill.name if po.dyeing_mill_id else None,
        "lines": po_lines,
        "quantities": {k: (str(v) if isinstance(v, Decimal) else v) for k, v in quantities(po, lines).items()},
        "yarnLines": [serialize_yarn(y) for y in _live(po.yarn_lines).select_related("item", "supplier")],
        "dispatches": [serialize_delivery(d) for d in _live(po.deliveries).select_related("mill").order_by("dispatch_date", "created_at")],
        "millProcesses": [serialize_mill(m) for m in _live(po.mill_processes).select_related("mill", "delivery")],
        "receipts": receipts,
        "expenses": [serialize_expense(e, by_expense[e.id]) for e in _live(po.fabric_expenses).select_related("party")],
        "discrepancies": [serialize_discrepancy(x) for x in _live(po.discrepancies).select_related("created_by")],
        "costs": cost_summary(po),
        "options": {
            "expenseCategories": [
                {"value": v, "label": l} for v, l in PurchaseOrderExpense.CATEGORIES
                if v != PurchaseOrderExpense.CATEGORY_DYEING or po.fabric_flow == FABRIC_FLOW_FACTORY_TO_MILL
            ],
            "discrepancyCategories": [{"value": v, "label": l} for v, l in PurchaseOrderDiscrepancy.CATEGORIES],
        },
    }


def batch_trace(batch):
    """The order behind a factory fabric batch, for Batch 360 / the QR scan.

    For a dyed batch (Flow B) the grey fabric's factory -> mill journey is
    shown as a trace -- it never was an OSCAR grey stock batch."""
    po = batch.purchase_order
    if po is None or not po.fabric_flow:
        return None
    receipt = batch.goods_receipt
    line = batch.goods_receipt_line
    allocations = _live(batch.po_cost_allocations).select_related("expense")
    costs = defaultdict(lambda: ZERO)
    for a in allocations:
        costs[a.expense.get_category_display() if a.expense_id else "Fabric received"] += D(a.amount)
    total = sum(costs.values(), ZERO)
    received = D(line.received_qty) if line else ZERO
    trace = {
        "poNumber": po.po_number,
        "purchaseOrderId": str(po.id),
        "fabricFlow": po.fabric_flow,
        "fabricFlowLabel": dict(FABRIC_FLOWS)[po.fabric_flow],
        "factoryName": po.party.name if po.party_id else None,
        "receipt": {
            "grnNumber": receipt.grn_number if receipt else None,
            "receiptDate": _d(receipt.receipt_date) if receipt else None,
            "receivedQty": str(received),
            "acceptedQty": str(_qc_totals(receipt)["good"]) if receipt else None,
            "qcStatus": receipt.qc_status if receipt else None,
            "challanNo": receipt.supplier_challan_no if receipt else None,
        },
        "yarnLines": [serialize_yarn(y) for y in _live(po.yarn_lines).select_related("item", "supplier")],
        "allocatedCosts": [{"label": k, "amount": str(round2(v))} for k, v in costs.items()],
        "allocatedTotal": str(round2(total)),
        "landedRate": str(round4(total / received)) if received > ZERO else None,
        "landedCostStatus": cost_summary(po)["landedCostStatus"],
    }
    if po.fabric_flow == FABRIC_FLOW_FACTORY_TO_MILL:
        trace["dyeingMillName"] = po.dyeing_mill.name if po.dyeing_mill_id else None
        trace["factoryToMill"] = [serialize_delivery(d) for d in _live(po.deliveries).filter(destination="MILL").select_related("mill")]
        trace["millProcesses"] = [serialize_mill(m) for m in _live(po.mill_processes).select_related("mill", "delivery")]
    return trace


def mark_purchase_type(attrs):
    """A factory fabric PO is received through the grey stock GRN, so it is
    stored as a grey stock purchase -- every receiving screen and check
    keeps working for it."""
    if attrs.get("fabric_flow"):
        attrs["purchase_type"] = PURCHASE_TYPE_GREY_STOCK
    return attrs
