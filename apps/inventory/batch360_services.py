"""
Batch 360°: every operation and every figure of one fabric batch, by tab.

The batch screen reads what already exists -- rolls, the stock movement
ledger, QC dispositions, job work, production runs, sales documents, lot cost
entries -- and adds only the operations a user starts *from* a batch:

    send to QC  -> rolls in a godown move to QC Hold (QC is optional)
    record QC   -> rolls at QC Hold are routed good / second / reprocess /
                   reject / hold, with the shared QC disposition
    rework done -> reprocessed rolls go back to QC Hold for re-inspection,
                   with the reprocessing cost on the batch
    yarn        -> hand-entered yarn traceability (no stock, no cost)

Sale, dyeing (job work), purchase return and scrap keep their own documents;
the batch shows them, it never copies them.
"""
import uuid
from collections import defaultdict

from django.db import transaction
from django.db.models import Q

from apps.core.audit import record_audit
from apps.core.exceptions import BusinessRuleViolation, ValidationFailed
from apps.core.money import ZERO, D, round2, round4
from apps.core.numbering import allocate_number

from . import qc as qc_service
from . import services as stock
from .batch_services import _date, _sale_rates, _takas

#: Movement types of a QC result, and the category each one records.
QC_RESULT_TYPES = {
    "QC_PASS": "good",
    "QC_SECOND_QUALITY": "second",
    "QC_REPROCESS": "reprocess",
    "QC_REJECT": "reject",
}
#: Rolls that arrived on a purchase receipt are inspected on the purchase QC
#: screen: its verdict also settles the bill, so the batch must not bypass it.
PURCHASE_QC_SOURCES = ("GRN", "PurchaseBill")


def _code(location):
    return (getattr(location, "code", "") or "").upper()


def _s(value):
    return str(round4(D(value))) if value is not None else None


# ---------------------------------------------------------------------------
# Read side: one function per tab
# ---------------------------------------------------------------------------
def qc_summary(batch, takas):
    """Where the batch stands on QC now, and every QC result it has had."""
    from .models import StockMovement

    now = defaultdict(lambda: ZERO)
    for t in takas:
        qty = D(t.available_quantity or 0)
        if qty <= ZERO:
            continue
        code = _code(t.location)
        if code == "SYS-QC-HOLD":
            now["onHold" if t.qc_status == "On Hold" else "pending"] += qty
        elif code == "SYS-REPROCESS":
            now["reprocess"] += qty
        elif code == "SYS-REJECT":
            now["rejected"] += qty
        elif code == "SYS-SECOND-QUALITY":
            now["secondQuality"] += qty
        elif t.qc_status == "Not Inspected":
            now["notInspected"] += qty
        elif code.startswith("SYS-WIP"):
            now["atJobWorker"] += qty
        else:
            now["passed"] += qty

    results = {}
    for m in (
        StockMovement.objects.filter(
            lot_taka__in=takas, type__in=list(QC_RESULT_TYPES), quantity__gt=0, deleted_at__isnull=True,
        ).select_related("created_by").order_by("movement_date", "created_at")
    ):
        key = (m.reference_type, m.reference_number or str(m.reference_id))
        row = results.setdefault(key, {
            "reference": m.reference_number, "referenceType": m.reference_type, "date": _date(m.movement_date),
            "by": getattr(m.created_by, "name", None), "good": ZERO, "second": ZERO, "reprocess": ZERO,
            "reject": ZERO, "notes": m.notes,
        })
        row[QC_RESULT_TYPES[m.type]] += D(m.quantity)
    rows = list(results.values())
    totals = defaultdict(lambda: ZERO)
    for row in rows:
        for k in ("good", "second", "reprocess", "reject"):
            totals[k] += row[k]
            row[k] = _s(row[k])

    scrapped = StockMovement.objects.filter(
        lot_taka__in=takas, reference_type="ScrapDisposal", quantity__lt=0, deleted_at__isnull=True,
    )
    wasted = -sum((D(m.quantity) for m in scrapped), ZERO)
    blocked = [t for t in takas if D(t.available_quantity or 0) > ZERO and _code(t.location) == "SYS-QC-HOLD"
               and t.source_document_type in PURCHASE_QC_SOURCES]
    if now["pending"] + now["onHold"] > ZERO:
        state = "Pending Inspection"
    elif not rows:
        state = "QC Not Done"
    elif now["rejected"] > ZERO and now["passed"] + now["secondQuality"] <= ZERO:
        state = "Rejected"
    elif now["reprocess"] > ZERO:
        state = "Rework / Reprocess"
    else:
        state = "Passed / Accepted"
    return {
        "state": state,
        "now": {k: _s(now[k]) for k in ("notInspected", "pending", "onHold", "passed", "secondQuality",
                                        "reprocess", "rejected", "atJobWorker")},
        "inspected": {k: _s(totals[k]) for k in ("good", "second", "reprocess", "reject")},
        "wasted": _s(wasted),
        "results": rows,
        # Rolls waiting at QC Hold that this screen may inspect.
        "inspectableQty": _s(sum((D(t.available_quantity) for t in qc_units(batch, takas=takas, lock=False)), ZERO)),
        "purchaseQcPending": _s(sum((D(t.available_quantity) for t in blocked), ZERO)),
        "purchaseQcReferences": sorted({t.source_document_number for t in blocked if t.source_document_number}),
    }


def sales(batch, takas):
    """Sales of the batch by document, with the customer and price."""
    from apps.sales.models import DeliveryChallan, SalesInvoice

    from .batch_services import RETURN_TYPES, SALE_TYPES
    from .models import StockMovement

    moves = list(
        StockMovement.objects.filter(
            lot_taka__in=takas, type__in=SALE_TYPES + RETURN_TYPES, deleted_at__isnull=True,
        ).order_by("movement_date", "created_at")
    )
    rates = _sale_rates([m for m in moves if m.type == "SALE"])
    docs = {}
    for model, ref_type, number_field in (
        (DeliveryChallan, "DeliveryChallan", "challan_number"),
        (SalesInvoice, "SalesInvoice", "invoice_number"),
    ):
        ids = {m.reference_id for m in moves if m.reference_type == ref_type and m.reference_id}
        for doc in model.objects.filter(pk__in=ids).select_related("party"):
            docs[(ref_type, doc.id)] = (getattr(doc, number_field), doc.party.name if doc.party_id else None,
                                        doc.status if hasattr(doc, "status") else None)
    rows = {}
    for m in moves:
        key = (m.reference_type, m.reference_id, m.type in RETURN_TYPES)
        number, customer, doc_status = docs.get((m.reference_type, m.reference_id), (m.reference_number, None, None))
        row = rows.setdefault(key, {
            "date": _date(m.movement_date), "documentType": m.reference_type, "documentNumber": number,
            "documentId": str(m.reference_id) if m.reference_id else None, "customer": customer,
            "status": doc_status, "kind": "Return" if m.type in RETURN_TYPES else "Sale",
            "quantity": ZERO, "amount": ZERO, "priced": True,
        })
        qty = -D(m.quantity) if m.type in SALE_TYPES else D(m.quantity)
        row["quantity"] += qty
        if m.type == "SALE":
            rate = rates.get(m.id)
            if rate is None:
                row["priced"] = False
            else:
                row["amount"] += D(rate) * qty
    out = []
    for row in rows.values():
        row["rate"] = _s(row["amount"] / row["quantity"]) if row["quantity"] and row["kind"] == "Sale" else None
        row["quantity"] = _s(row["quantity"])
        row["amount"] = str(round2(row["amount"])) if row["kind"] == "Sale" else None
        out.append(row)
    return out


def job_work(batch, takas):
    """Dyeing / processing at job workers: sent, received back and pending, by order."""
    from apps.jobwork.models import JWOCharge, JWOInwardTaka, JWOOutwardTaka

    ids = [t.id for t in takas]
    orders = {}

    def row_for(order):
        return orders.setdefault(order.id, {
            "orderId": str(order.id), "jwoNo": order.jwo_no, "process": order.process or None,
            "vendor": (order.vendor.name if order.vendor_id else order.vendor_name) or None,
            "rate": str(order.rate), "status": order.status, "orderDate": _date(order.order_date),
            "sent": ZERO, "received": ZERO, "outwards": [], "inwards": [], "charges": ZERO,
        })

    for row in JWOOutwardTaka.objects.filter(lot_taka_id__in=ids).select_related(
        "outward", "outward__job_work_order", "outward__job_work_order__vendor", "lot_taka",
    ):
        out = row.outward
        if out.deleted_at or getattr(out.job_work_order, "deleted_at", None):
            continue
        entry = row_for(out.job_work_order)
        entry["sent"] += D(row.meters)
        entry["outwards"].append({"no": out.no, "date": _date(out.date), "challan": out.challan_no,
                                  "taka": row.lot_taka.taka_id, "meters": _s(row.meters), "status": out.status})
    for row in JWOInwardTaka.objects.filter(Q(source_taka_id__in=ids) | Q(received_taka_id__in=ids)).select_related(
        "inward", "inward__job_work_order", "inward__job_work_order__vendor", "received_taka",
    ):
        inward = row.inward
        if inward.deleted_at:
            continue
        entry = row_for(inward.job_work_order)
        entry["received"] += D(row.meters)
        entry["inwards"].append({"no": inward.no, "date": _date(inward.date), "challan": inward.challan_no,
                                 "taka": row.received_taka.taka_id if row.received_taka_id else None,
                                 "meters": _s(row.meters),
                                 "qcStatus": row.received_taka.qc_status if row.received_taka_id else None})
    for charge in JWOCharge.objects.filter(job_work_order_id__in=list(orders), deleted_at__isnull=True) \
            .exclude(status="Cancelled"):
        orders[charge.job_work_order_id]["charges"] += D(charge.amount or 0)
    rows = []
    for entry in orders.values():
        entry["pending"] = _s(max(ZERO, entry["sent"] - entry["received"]))
        entry["sent"], entry["received"] = _s(entry["sent"]), _s(entry["received"])
        entry["charges"] = str(round2(entry["charges"]))
        rows.append(entry)
    rows.sort(key=lambda r: r["orderDate"] or "")
    return rows


def vendor_returns(takas):
    from .models import StockMovement

    rows = defaultdict(lambda: {"quantity": ZERO})
    for m in StockMovement.objects.filter(
        lot_taka__in=takas, type="PURCHASE_RETURN", deleted_at__isnull=True,
    ).order_by("movement_date"):
        row = rows[m.reference_number or str(m.reference_id)]
        row.update({"reference": m.reference_number, "date": _date(m.movement_date), "notes": m.notes})
        row["quantity"] += -D(m.quantity)
    return [{**r, "quantity": _s(r["quantity"])} for r in rows.values()]


def yarn(batch):
    """Yarn behind the batch: production-run issues (costed) and hand entries."""
    from .models import ProductionMaterialIssue, ProductionRun

    runs = ProductionRun.objects.filter(client_id=batch.client_id, batch=batch, deleted_at__isnull=True)
    issued = []
    for i in ProductionMaterialIssue.objects.filter(run__in=runs, deleted_at__isnull=True).select_related(
        "item", "run", "location",
    ).order_by("issued_on", "created_at"):
        issued.append({
            "id": str(i.id), "source": "PRODUCTION_RUN", "runNumber": i.run.run_number, "date": _date(i.issued_on),
            "yarn": i.item.name, "yarnLot": i.yarn_lot, "quantity": _s(i.quantity), "uom": i.item.uom,
            "isReturn": D(i.quantity) < ZERO, "unitCost": _s(i.unit_cost), "amount": str(round2(D(i.amount))),
            "location": i.location.name, "customerSupplied": i.customer_supplied,
        })
    manual = [{
        "id": str(y.id), "source": "MANUAL", "date": _date(y.entry_date),
        "yarn": y.yarn_item.name if y.yarn_item_id else y.yarn_name, "yarnItemId": str(y.yarn_item_id or "") or None,
        "yarnLot": y.yarn_lot, "yarnCount": y.yarn_count,
        "supplier": y.supplier.name if y.supplier_id else None, "uom": y.uom,
        "issued": _s(y.issued_qty), "consumed": _s(y.consumed_qty), "returned": _s(y.returned_qty),
        "waste": _s(y.waste_qty), "rate": _s(y.rate) if y.rate is not None else None, "remarks": y.remarks,
    } for y in batch.yarns.filter(deleted_at__isnull=True).select_related("yarn_item", "supplier")]

    net_issued = sum((D(r["quantity"]) for r in issued if not r["isReturn"]), ZERO)
    returned = -sum((D(r["quantity"]) for r in issued if r["isReturn"]), ZERO)
    run_waste = sum((D(r.wastage_qty or 0) for r in runs), ZERO)
    totals = {
        "issued": net_issued + sum((D(r["issued"]) for r in manual), ZERO),
        "returned": returned + sum((D(r["returned"]) for r in manual), ZERO),
        "waste": run_waste + sum((D(r["waste"]) for r in manual), ZERO),
        "consumedManual": sum((D(r["consumed"]) for r in manual), ZERO),
    }
    # Consumed = issued - returned - waste for run issues; as entered for hand entries.
    totals["consumed"] = max(ZERO, net_issued - returned - run_waste) + totals["consumedManual"]
    return {
        "issues": issued,
        "manual": manual,
        "totals": {k: _s(v) for k, v in totals.items() if k != "consumedManual"},
        "costedAmount": str(round2(sum((D(r["amount"]) for r in issued), ZERO))),
    }


def production(batch, takas):
    """Production runs that made the batch, with the output day by day."""
    from .models import ProductionRun
    from .production_services import serialize_run

    runs = []
    for run in ProductionRun.objects.filter(client_id=batch.client_id, batch=batch, deleted_at__isnull=True) \
            .select_related("output_item", "customer", "batch").order_by("run_date", "created_at"):
        data = serialize_run(run)
        daily = defaultdict(lambda: {"rolls": 0, "meters": ZERO})
        for t in takas:
            if t.source_document_type == "Production" and t.source_document_id == run.id and t.parent_taka_id is None:
                day = daily[_date(t.received_date)]
                day["rolls"] += 1
                day["meters"] += D(t.received_quantity)
        data["daily"] = [{"date": d, "rolls": v["rolls"], "meters": _s(v["meters"])} for d, v in sorted(daily.items())]
        runs.append(data)
    return runs


def documents(batch, takas, sales_rows, jw_rows, returns, qc_rows, runs):
    """Every document linked to the batch, for the Documents tab."""
    docs = []
    if batch.source_document_number:
        docs.append({"type": batch.source_document_type, "number": batch.source_document_number, "role": "Source"})
    seen = {(batch.source_document_type, batch.source_document_number)}
    for t in takas:
        key = (t.source_document_type, t.source_document_number)
        if t.source_document_number and key not in seen:
            seen.add(key)
            docs.append({"type": t.source_document_type, "number": t.source_document_number, "role": "Roll source"})
    docs += [{"type": r["documentType"], "number": r["documentNumber"], "role": r["kind"], "date": r["date"],
              "party": r["customer"]} for r in sales_rows]
    docs += [{"type": "JobWorkOrder", "number": r["jwoNo"], "role": r["process"] or "Job work", "date": r["orderDate"],
              "party": r["vendor"]} for r in jw_rows]
    docs += [{"type": "PurchaseReturn", "number": r["reference"], "role": "Vendor return", "date": r["date"]}
             for r in returns]
    docs += [{"type": r["referenceType"], "number": r["reference"], "role": "QC", "date": r["date"]} for r in qc_rows]
    docs += [{"type": "ProductionRun", "number": r["runNumber"], "role": "Production", "date": r["runDate"]}
             for r in runs]
    return docs


def batch_360(batch, *, takas=None, include_cost=False):
    """The tab data added to the batch detail."""
    takas = takas if takas is not None else _takas(batch)
    qc = qc_summary(batch, takas)
    sales_rows = sales(batch, takas)
    jw_rows = job_work(batch, takas)
    returns = vendor_returns(takas)
    runs = production(batch, takas)
    if not include_cost:
        for r in sales_rows:
            r["rate"] = r["amount"] = None
        for r in jw_rows:
            r["rate"] = r["charges"] = None
        for r in runs:
            r.pop("accounting", None)
            for i in r.get("issues", []):
                i["unitCost"] = i["amount"] = None
            for e in r.get("expenses", []):
                e["amount"] = None
    yarn_data = yarn(batch)
    if not include_cost:
        yarn_data["costedAmount"] = None
        for i in yarn_data["issues"]:
            i["unitCost"] = i["amount"] = None
        for m in yarn_data["manual"]:
            m["rate"] = None
    return {
        "qc": qc,
        "sales": sales_rows,
        "jobWork": jw_rows,
        "vendorReturns": returns,
        "productionRuns": runs,
        "yarn": yarn_data,
        "documents": documents(batch, takas, sales_rows, jw_rows, returns, qc["results"], runs),
    }


# ---------------------------------------------------------------------------
# Write side: operations started from the batch
# ---------------------------------------------------------------------------
def _locked_takas(batch, ids=None):
    from .models import LotTaka

    qs = LotTaka.objects.select_for_update(of=("self",)).filter(
        client_id=batch.client_id, batch=batch, deleted_at__isnull=True, available_quantity__gt=0,
    ).select_related("location").order_by("created_at", "taka_number")
    if ids:
        qs = qs.filter(pk__in=ids)
    return list(qs)


def _quantity_plan(takas, quantity):
    """[(taka, qty)] -- whole rolls, or ``quantity`` spread over them in order."""
    if quantity in (None, ""):
        return [(t, D(t.available_quantity)) for t in takas]
    left = round4(D(quantity))
    total = sum((D(t.available_quantity) for t in takas), ZERO)
    if left <= ZERO:
        raise ValidationFailed("Enter the quantity.", field_errors={"quantity": ["Must be above 0."]})
    if left > total:
        raise BusinessRuleViolation(f"Only {round4(total)} is available on the chosen rolls; {left} asked.",
                                    code="INSUFFICIENT_STOCK")
    plan = []
    for t in takas:
        if left <= ZERO:
            break
        take = min(left, D(t.available_quantity))
        plan.append((t, round4(take)))
        left -= take
    return plan


@transaction.atomic
def send_to_qc(batch, *, lot_taka_ids=None, quantity=None, remarks=None, user=None):
    """Move rolls (or part of them) from a godown to QC Hold, pending inspection.

    Only usable stock in a godown can go: not reserved rolls, not rolls at a
    job worker, nor stock already at a QC / reprocess / reject location.
    Customer material may be inspected too -- it stays the customer's.
    """
    takas = [t for t in _locked_takas(batch, lot_taka_ids)
             if not _code(t.location).startswith("SYS-") and t.status != "Reserved"]
    if not takas:
        raise BusinessRuleViolation(f"No rolls of {batch.batch_code} are in a godown to send for QC.",
                                    code="NOTHING_TO_QC")
    plan = _quantity_plan(takas, quantity)
    qc_location = stock.system_location(batch.client_id, "QC_HOLD")
    reference_no = allocate_number(batch.client_id, "BQC")
    reference_id = uuid.uuid4()
    moved = ZERO
    for taka, qty in plan:
        holder, _ = stock.relocate_lot(
            client_id=batch.client_id, lot=taka, quantity=qty, to_location_id=qc_location.id,
            out_type="QC_HOLD", in_type="QC_HOLD", reference_type="BatchQC", reference_id=reference_id,
            reference_number=reference_no, notes="Sent for QC" + (f": {remarks}" if remarks else ""),
            user=user, qc_status="Pending", split_suffix="Q",
        )
        type(holder).objects.filter(pk=holder.pk).update(status="QC Hold", qc_status="Pending")
        moved += qty
    record_audit(
        client=batch.client_id, actor=user, action="batch_send_to_qc", entity_type="Batch", entity_id=batch.id,
        entity_label=batch.batch_code, description=f"{round4(moved)} sent for QC ({reference_no})",
        comments=remarks or None,
    )
    return reference_no


def qc_units(batch, *, takas=None, lock=True):
    """Rolls of the batch waiting at QC Hold that the batch screen may inspect."""
    if lock:
        takas = _locked_takas(batch)
    return [t for t in takas if D(t.available_quantity or 0) > ZERO and _code(t.location) == "SYS-QC-HOLD"
            and t.source_document_type not in PURCHASE_QC_SOURCES]


@transaction.atomic
def record_qc(batch, *, quantities, good_location_id, remarks=None, loss=None, user=None):
    """Inspect the batch's rolls at QC Hold and route them by result.

    ``quantities`` = {good, second, reprocess, reject, hold} covering all of
    the inspectable quantity (what stays undecided is ``hold``). ``loss``
    optionally classifies the rejected metres at once (cause, responsible
    party, outcome) so the claim / normal-loss split is not forgotten.
    """
    from .batch_services import record_loss

    units = [{"line": None, "item_id": t.item_id, "lot": t, "qty": round4(D(t.available_quantity)),
              "from": t.location_id} for t in qc_units(batch)]
    if not units:
        raise BusinessRuleViolation(
            f"No rolls of {batch.batch_code} are waiting for QC here. Send rolls for QC first "
            "(purchase receipts are inspected on the purchase QC screen).",
            code="QC_NOTHING_PENDING",
        )
    reference_no = allocate_number(batch.client_id, "BQC")
    reference_id = uuid.uuid4()
    moved = qc_service.dispose_lot_units(
        units, client_id=batch.client_id, quantities=quantities, good_location_id=good_location_id,
        reference_type="BatchQC", reference_id=reference_id, reference_number=reference_no,
        source_document_type="Batch", source_document_id=batch.id, label=f"{batch.batch_code} ({reference_no})",
        user=user, remarks=remarks,
    )
    rejected = D(quantities.get("reject") or 0)
    if loss and rejected > ZERO:
        record_loss(
            batch, quantity=loss.get("quantity") or rejected, cause=loss.get("cause") or "NORMAL_LOSS",
            responsible_party_id=loss.get("responsiblePartyId"), process=loss.get("process") or "QC",
            outcome=loss.get("outcome") or "PENDING", reference=reference_no, notes=remarks, user=user,
        )
    record_audit(
        client=batch.client_id, actor=user, action="batch_qc", entity_type="Batch", entity_id=batch.id,
        entity_label=batch.batch_code, description=f"QC {reference_no}: "
        + ", ".join(f"{k} {round4(D(v))}" for k, v in quantities.items() if D(v or 0) > ZERO),
        comments=remarks or None,
    )
    return reference_no, moved


@transaction.atomic
def rework_done(batch, *, lot_taka_ids=None, quantity=None, cost=None, remarks=None, user=None):
    """Reprocessed (reworked in-house) rolls go back to QC Hold to be inspected
    again; ``cost`` of the rework is added to the batch as "Reprocess"."""
    from . import costing_services

    takas = [t for t in _locked_takas(batch, lot_taka_ids) if _code(t.location) == "SYS-REPROCESS"]
    if not takas:
        raise BusinessRuleViolation(f"{batch.batch_code} has no rolls in Reprocess stock.", code="NOTHING_TO_REWORK")
    plan = _quantity_plan(takas, quantity)
    qc_location = stock.system_location(batch.client_id, "QC_HOLD")
    reference_no = allocate_number(batch.client_id, "BQC")
    reference_id = uuid.uuid4()
    moved = ZERO
    lots = set()
    for taka, qty in plan:
        holder, _ = stock.relocate_lot(
            client_id=batch.client_id, lot=taka, quantity=qty, to_location_id=qc_location.id,
            out_type="QC_HOLD", in_type="QC_HOLD", reference_type="BatchQC", reference_id=reference_id,
            reference_number=reference_no, notes="Rework done, back for QC" + (f": {remarks}" if remarks else ""),
            user=user, qc_status="Pending", split_suffix="W",
        )
        type(holder).objects.filter(pk=holder.pk).update(status="QC Hold", qc_status="Pending")
        moved += qty
        lots.add(holder.lot_number)
    cost = round2(D(cost or 0))
    if cost < ZERO:
        raise ValidationFailed("Rework cost cannot be negative.", field_errors={"cost": ["≥ 0."]})
    if cost > ZERO:
        costing_services.post_lot_cost(
            client_id=batch.client_id, lot_number=sorted(lots)[0], cost_type="Reprocess", amount=cost,
            quantity=moved, source_document_type="BatchRework", source_document_id=reference_id,
            source_document_number=reference_no, user=user, remarks=remarks or "In-house rework",
        )
    record_audit(
        client=batch.client_id, actor=user, action="batch_rework_done", entity_type="Batch", entity_id=batch.id,
        entity_label=batch.batch_code, description=f"{round4(moved)} reworked, back to QC ({reference_no})"
        + (f", cost {cost}" if cost > ZERO else ""), comments=remarks or None,
    )
    return reference_no


def _qty(data, key):
    raw = data.get(key)
    try:
        value = round4(D(raw or 0))
    except Exception:  # noqa: BLE001 - reported as a field error
        raise ValidationFailed("Enter a number.", field_errors={key: ["Not a number."]})
    if value < ZERO:
        raise ValidationFailed("Quantities cannot be negative.", field_errors={key: ["≥ 0."]})
    return value


@transaction.atomic
def add_yarn(batch, data, *, user=None):
    """Record yarn used for the batch by hand (see ``BatchYarn``)."""
    from apps.masters.models import Item, Party

    from .models import BatchYarn

    item = Item.objects.filter(client_id=batch.client_id, pk=data.get("yarnItemId"), deleted_at__isnull=True).first() \
        if data.get("yarnItemId") else None
    name = (data.get("yarnName") or "").strip()
    if item is None and not name:
        raise ValidationFailed("Choose the yarn or type its name.", field_errors={"yarnItemId": ["Required."]})
    issued, consumed = _qty(data, "issuedQty"), _qty(data, "consumedQty")
    returned, waste = _qty(data, "returnedQty"), _qty(data, "wasteQty")
    if issued <= ZERO and consumed <= ZERO:
        raise ValidationFailed("Enter the yarn issued or consumed.", field_errors={"issuedQty": ["Required."]})
    if issued > ZERO and consumed + returned + waste > issued:
        raise ValidationFailed(
            f"Consumed + returned + waste ({round4(consumed + returned + waste)}) is more than issued ({issued}).",
            field_errors={"consumedQty": ["More than issued."]},
        )
    rate = data.get("rate")
    row = BatchYarn.objects.create(
        client_id=batch.client_id, batch=batch, yarn_item=item, yarn_name=name or None,
        yarn_lot=(data.get("yarnLot") or "").strip() or None, yarn_count=(data.get("yarnCount") or "").strip() or None,
        supplier=Party.objects.filter(client_id=batch.client_id, pk=data.get("supplierId")).first()
        if data.get("supplierId") else None,
        uom=data.get("uom") or (item.uom if item else None) or "Kg",
        issued_qty=issued, consumed_qty=consumed or max(ZERO, issued - returned - waste),
        returned_qty=returned, waste_qty=waste,
        rate=_qty(data, "rate") if rate not in (None, "") else None,
        remarks=data.get("remarks") or None,
        **({"entry_date": data["date"]} if data.get("date") else {}),
        created_by=user if getattr(user, "is_authenticated", False) else None,
    )
    record_audit(
        client=batch.client_id, actor=user, action="batch_yarn", entity_type="Batch", entity_id=batch.id,
        entity_label=batch.batch_code,
        description=f"Yarn recorded: {item.name if item else name} {row.consumed_qty} {row.uom} consumed",
    )
    return row


@transaction.atomic
def delete_yarn(batch, yarn_id, *, user=None):
    from django.utils import timezone

    from .models import BatchYarn

    row = BatchYarn.objects.filter(client_id=batch.client_id, batch=batch, pk=yarn_id, deleted_at__isnull=True).first()
    if row is None:
        raise ValidationFailed("Yarn entry not found.", field_errors={"yarnId": ["Not found."]})
    row.deleted_at = timezone.now()
    row.save(update_fields=["deleted_at", "updated_at"])
    record_audit(
        client=batch.client_id, actor=user, action="batch_yarn_delete", entity_type="Batch", entity_id=batch.id,
        entity_label=batch.batch_code, description="Yarn entry removed",
    )


@transaction.atomic
def set_fabric_type(batch, fabric_type, *, reason=None, user=None):
    """Correct what a batch came in as. The batch code does not change."""
    from .batch_services import _fabric_type

    fabric_type = _fabric_type(fabric_type)
    before = batch.fabric_type
    batch.fabric_type = fabric_type
    batch.save(update_fields=["fabric_type", "updated_at"])
    record_audit(
        client=batch.client_id, actor=user, action="batch_fabric_type", entity_type="Batch", entity_id=batch.id,
        entity_label=batch.batch_code, description=f"Fabric type {before} -> {fabric_type}",
        before={"fabricType": before}, after={"fabricType": fabric_type}, comments=reason or None,
    )
    return batch
