"""
Stock Position screens (Inventory → Stock Position), one dataset per screen.

Every quantity comes from where the rolls are now (``LotTaka`` at its
location, kept in step with the stock ledger) or from the documents that
already own the figure (samples, job-work orders, sales returns, batch losses).
Nothing is estimated and nothing is counted in two screens' "available":

* a roll's bucket is decided once, by ``batch_services._bucket`` (its
  location) and its status -- QC Hold, Reprocess, Second Quality, Reject and a
  job worker's WIP are never "available";
* customer / third-party material is shown, labelled, and never counted as
  OSCAR's saleable stock;
* cost and value fields are filled only when ``include_cost`` (ledger access).
"""
from collections import defaultdict
from datetime import date

from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from apps.core.money import ZERO, D, round2, round4

from .batch_services import _bucket, is_bulk

BUCKET_LABEL = {
    "available": "Available",
    "reserved": "Reserved",
    "qcHold": "QC Hold",
    "rework": "Reprocessing",
    "secondQuality": "Second Quality",
    "rejected": "Rejected",
    "withJobWorker": "At Job Worker",
}


def _date(value):
    if not value:
        return None
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return value


def _page(rows, params):
    try:
        page = max(1, int(params.get("page") or 1))
        size = min(200, max(1, int(params.get("page_size") or 25)))
    except (TypeError, ValueError):
        page, size = 1, 25
    if params.get("export"):
        return rows, 1, len(rows) or 1
    return rows[(page - 1) * size: page * size], page, size


def _taka_bucket(taka):
    bucket = _bucket(taka)
    if bucket == "available" and taka.status == "Reserved":
        bucket = "reserved"
    return bucket


def _rolls(client_id, params):
    """Rolls in stock, filtered by the common Stock Position filters."""
    from .models import LotTaka

    qs = LotTaka.objects.filter(
        client_id=client_id, deleted_at__isnull=True, available_quantity__gt=0,
    ).select_related("item", "design", "shade", "fabric_quality", "location", "batch", "batch__owner_party")
    q = (params.get("q") or "").strip()
    if q:
        qs = qs.filter(
            Q(batch__batch_code__icontains=q) | Q(taka_id__icontains=q) | Q(lot_number__icontains=q)
            | Q(item__name__icontains=q) | Q(item__sku__icontains=q)
        )
    for key, field in (("itemId", "item_id"), ("designId", "design_id"), ("shadeId", "shade_id"),
                       ("locationId", "location_id")):
        if params.get(key):
            qs = qs.filter(**{field: params[key]})
    if params.get("batch"):
        qs = qs.filter(batch__batch_code__icontains=params["batch"])
    ownership = params.get("ownership")
    if ownership == "OSCAR":
        qs = qs.filter(Q(batch__isnull=True) | Q(batch__ownership="OSCAR"))
    elif ownership == "CUSTOMER":
        qs = qs.filter(batch__ownership__in=["CUSTOMER", "THIRD_PARTY"])
    if _date(params.get("from")):
        qs = qs.filter(received_date__gte=_date(params["from"]))
    if _date(params.get("to")):
        qs = qs.filter(received_date__lte=_date(params["to"]))
    return list(qs.order_by("-received_date", "batch__batch_code", "taka_number"))


def _roll_row(t, include_cost):
    bucket = _taka_bucket(t)
    owned = not (t.batch_id and t.batch.ownership != "OSCAR")
    row = {
        "lotTakaId": str(t.id),
        "batchCode": t.batch.batch_code if t.batch_id else None,
        "takaId": t.taka_id,
        "lotNumber": t.lot_number,
        "fabric": t.item.name if t.item_id else None,
        "fabricQuality": t.fabric_quality.name if t.fabric_quality_id else None,
        "design": t.design.name if t.design_id else None,
        "shade": t.shade.name if t.shade_id else None,
        "location": t.location.name if t.location_id else None,
        "locationId": str(t.location_id) if t.location_id else None,
        "quantity": str(round4(D(t.available_quantity))),
        "uom": t.uom or (t.item.uom if t.item_id else None),
        "qcStatus": t.qc_status,
        "ownership": "OSCAR" if owned else t.batch.ownership,
        "ownerName": None if owned else (t.batch.owner_party.name if t.batch.owner_party_id else None),
        "bucket": bucket,
        "stockStatus": "Not yet in takas" if is_bulk(t) else BUCKET_LABEL.get(bucket, bucket),
        "receivedDate": t.received_date.isoformat() if t.received_date else None,
    }
    if include_cost:
        row["rate"] = str(round4(D(t.rate or 0)))
        row["value"] = str(round2(D(t.rate or 0) * D(t.available_quantity)))
    return row


# ---------------------------------------------------------------------------
# 1. Fabric Stock Overview
# ---------------------------------------------------------------------------
def fabric_overview(client_id, params, include_cost=False):
    rolls = _rolls(client_id, params)
    kpi = defaultdict(lambda: ZERO)
    for t in rolls:
        qty = D(t.available_quantity)
        bucket = _taka_bucket(t)
        kpi["physical"] += qty
        if t.batch_id and t.batch.ownership != "OSCAR":
            kpi["customerHeld"] += qty
        elif bucket == "available":
            kpi["available"] += qty
        if bucket == "reserved":
            kpi["reserved"] += qty
        if bucket in ("qcHold", "rejected"):
            kpi["qcHoldRejected"] += qty
        if bucket in ("withJobWorker", "rework"):
            kpi["inProcess"] += qty
    status = params.get("status")
    rows = [_roll_row(t, include_cost) for t in rolls]
    if status:
        rows = [r for r in rows if r["bucket"] == status]
    page_rows, page, size = _page(rows, params)
    return {"kpis": {k: str(round4(v)) for k, v in kpi.items()}, "results": page_rows,
            "count": len(rows), "page": page, "page_size": size}


# ---------------------------------------------------------------------------
# 4. Usable Fabric Stock
# ---------------------------------------------------------------------------
def usable_stock(client_id, params, include_cost=False):
    """OSCAR-owned, QC-passed rolls in a godown. Reserved is shown apart;
    bulk (not yet in takas) is usable for production but not for sale."""
    from apps.masters.models import Item

    rolls = [
        t for t in _rolls(client_id, {**params, "ownership": "OSCAR"})
        if _taka_bucket(t) in ("available", "reserved") and t.qc_status in ("Passed", "Pass")
    ]
    kpi = defaultdict(lambda: ZERO)
    per_item = defaultdict(lambda: ZERO)
    for t in rolls:
        qty = D(t.available_quantity)
        if _taka_bucket(t) == "reserved":
            kpi["reserved"] += qty
            continue
        kpi["forProduction"] += qty
        per_item[t.item_id] += qty
        if not is_bulk(t):
            kpi["forSale"] += qty
    low = []
    for item in Item.objects.filter(client_id=client_id, deleted_at__isnull=True, reorder_level__gt=0):
        if per_item.get(item.id, ZERO) <= D(item.reorder_level):
            low.append({"itemId": str(item.id), "fabric": item.name, "available": str(round4(per_item.get(item.id, ZERO))),
                        "reorderLevel": str(round4(D(item.reorder_level)))})
    kpis = {k: str(round4(v)) for k, v in kpi.items()}
    kpis["lowStockItems"] = len(low)
    if include_cost:
        kpis["availableValue"] = str(round2(sum(
            (D(t.rate or 0) * D(t.available_quantity) for t in rolls if _taka_bucket(t) == "available"), ZERO)))
    rows = [_roll_row(t, include_cost) for t in rolls]
    if params.get("status"):
        rows = [r for r in rows if r["bucket"] == params["status"]]
    page_rows, page, size = _page(rows, params)
    return {"kpis": kpis, "lowStock": low, "results": page_rows, "count": len(rows), "page": page, "page_size": size}


# ---------------------------------------------------------------------------
# 3. Scrap & Waste
# ---------------------------------------------------------------------------
def scrap_waste(client_id, params, include_cost=False):
    """QC-rejected rolls still in Reject stock (with how each batch's
    rejection was classified), second-quality stock, recorded production
    wastage and approved job-work process loss. Rolls routed to rework have
    left Reject stock, so they are not counted here."""
    from apps.jobwork.models import JWOLossEntry

    from .models import BatchLoss, ProductionRun

    rolls = [t for t in _rolls(client_id, params) if _taka_bucket(t) in ("rejected", "secondQuality")]
    losses_by_batch = defaultdict(list)
    for loss in BatchLoss.objects.filter(
        client_id=client_id, deleted_at__isnull=True, batch_id__in={t.batch_id for t in rolls if t.batch_id},
    ):
        losses_by_batch[loss.batch_id].append(loss)

    rows = []
    kpi = defaultdict(lambda: ZERO)
    for t in rolls:
        qty = D(t.available_quantity)
        bucket = _taka_bucket(t)
        row = _roll_row(t, include_cost)
        losses = losses_by_batch.get(t.batch_id, [])
        if bucket == "secondQuality":
            row.update(kind="Second quality", reusable=True, reason="QC graded second quality",
                       disposition="Downgrade (sellable as second)")
            kpi["reusable"] += qty
        else:
            kpi["qcRejected"] += qty
            outcomes = {l.outcome for l in losses}
            row.update(
                kind="QC rejected",
                reusable=bool(outcomes & {"REWORK", "DOWNGRADE"}),
                reason=", ".join(sorted({l.get_cause_display() for l in losses})) or "Not classified yet",
                disposition=", ".join(sorted({l.get_outcome_display() for l in losses})) or "Pending review",
            )
            if "SCRAP" in outcomes:
                kpi["scrap"] += qty
            if row["reusable"]:
                kpi["reusable"] += qty
        if include_cost:
            kpi["value"] += D(t.rate or 0) * qty
        rows.append(row)

    for run in ProductionRun.objects.filter(client_id=client_id, deleted_at__isnull=True, wastage_qty__gt=0) \
            .select_related("output_item", "batch"):
        kpi["productionWaste"] += D(run.wastage_qty)
        rows.append({
            "kind": "Production wastage", "batchCode": run.batch.batch_code if run.batch_id else None,
            "fabric": run.output_item.name, "quantity": str(round4(D(run.wastage_qty))),
            "uom": run.wastage_uom, "reason": f"Production {run.run_number}", "reusable": False,
            "disposition": "Recorded loss", "stockStatus": "Written into batch cost",
            "receivedDate": run.completed_at.date().isoformat() if run.completed_at else None,
        })
    for entry in JWOLossEntry.objects.filter(client_id=client_id, status="Approved") \
            .select_related("job_work_order", "loss_reason"):
        kpi["processLoss"] += D(entry.qty)
        order = entry.job_work_order
        rows.append({
            "kind": "Process loss", "batchCode": None, "fabric": order.process if order else None,
            "quantity": str(round4(D(entry.qty))), "uom": "m",
            "reason": entry.loss_reason.name if entry.loss_reason_id else (entry.remarks or "Approved loss"),
            "reusable": False, "disposition": "Approved loss",
            "stockStatus": f"{order.jwo_no} ({order.vendor_name})" if order else None,
            "receivedDate": entry.approved_at.date().isoformat() if entry.approved_at else None,
        })
    if params.get("kind"):
        rows = [r for r in rows if r.get("kind") == params["kind"]]
    kpis = {k: str(round2(v) if k == "value" else round4(v)) for k, v in kpi.items()}
    page_rows, page, size = _page(rows, params)
    return {"kpis": kpis, "results": page_rows, "count": len(rows), "page": page, "page_size": size}


@transaction.atomic
def route_to_rework(client_id, *, lot_taka_id, quantity, reason, user=None):
    """Move rejected metres to Reprocess stock (they then leave Scrap & Waste)."""
    from apps.core.exceptions import BusinessRuleViolation, ValidationFailed

    from . import services as stock
    from .models import LotTaka

    taka = LotTaka.objects.select_for_update(of=("self",)).filter(
        client_id=client_id, pk=lot_taka_id, deleted_at__isnull=True).select_related("location").first()
    qty = round4(D(quantity or 0))
    if taka is None or qty <= ZERO:
        raise ValidationFailed("Pick the roll and quantity.", field_errors={"quantity": ["Required."]})
    if not (reason or "").strip():
        raise ValidationFailed("Give the reason.", field_errors={"reason": ["Required."]})
    if (taka.location.code or "") != "SYS-REJECT":
        raise BusinessRuleViolation("Only rejected stock is routed to rework from here.", code="NOT_REJECTED_STOCK")
    if qty > D(taka.available_quantity):
        raise BusinessRuleViolation(f"Only {round4(D(taka.available_quantity))} is in Reject stock.",
                                    code="INSUFFICIENT_STOCK")
    holder, _ = stock.relocate_lot(
        client_id=client_id, lot=taka, quantity=qty,
        to_location_id=stock.system_location(client_id, "REPROCESS").id,
        out_type="QC_REPROCESS", in_type="QC_REPROCESS", reference_type="ScrapReview",
        reference_id=taka.id, reference_number=f"RW-{taka.taka_id}", user=user,
        notes=f"Routed to rework: {reason}", qc_status="Reprocess", split_suffix="RW",
    )
    LotTaka.objects.filter(pk=holder.pk).update(status="Reprocess", updated_at=timezone.now())
    return holder


@transaction.atomic
def dispose_scrap(client_id, *, lot_taka_id, quantity, reason, user=None):
    """Approved disposal of rejected metres: stock out at the roll's cost and
    Dr Stock Adjustment | Cr Inventory. Partial quantities allowed."""
    import uuid

    from apps.accounting import services as ledger
    from apps.core.exceptions import BusinessRuleViolation, ValidationFailed

    from . import services as stock
    from .models import LotTaka

    taka = LotTaka.objects.select_for_update(of=("self",)).filter(
        client_id=client_id, pk=lot_taka_id, deleted_at__isnull=True).select_related("location", "item").first()
    qty = round4(D(quantity or 0))
    if taka is None or qty <= ZERO:
        raise ValidationFailed("Pick the roll and quantity.", field_errors={"quantity": ["Required."]})
    if not (reason or "").strip():
        raise ValidationFailed("Give the approval reason for disposal.", field_errors={"reason": ["Required."]})
    if (taka.location.code or "") != "SYS-REJECT":
        raise BusinessRuleViolation("Only rejected stock can be disposed of from here.", code="NOT_REJECTED_STOCK")
    if qty > D(taka.available_quantity):
        raise BusinessRuleViolation(f"Only {round4(D(taka.available_quantity))} is in Reject stock.",
                                    code="INSUFFICIENT_STOCK")
    customer_material = bool(taka.batch_id and taka.batch.ownership != "OSCAR")
    rate = ZERO if customer_material else D(taka.rate or 0)
    movement = stock.post_movement(
        client_id=client_id, item=taka.item, location=taka.location, type="ADJUSTMENT", quantity=-qty,
        unit_cost=rate, reference_type="ScrapDisposal", reference_id=uuid.uuid4(),
        reference_number=f"DSP-{taka.taka_id}", lot_taka=taka, user=user, notes=f"Scrap disposal: {reason}",
    )
    if rate > ZERO:
        ledger.post_stock_adjustment(movement, user=user)
    return movement


# ---------------------------------------------------------------------------
# 5. Reprocessing (job work at vendors, and the in-house Reprocess store)
# ---------------------------------------------------------------------------
def reprocessing(client_id, params, include_cost=False):
    from apps.jobwork.models import JobWorkOrder, JWOCharge, JWOInwardTaka, JWOLossEntry, JWOOutwardTaka

    from . import services as stock
    from .models import LotTaka

    today = timezone.localdate()
    qc_hold = stock.system_location(client_id, "QC_HOLD")
    orders = JobWorkOrder.objects.filter(client_id=client_id).exclude(status__in=["Cancelled"]).order_by("-order_date")
    if params.get("q"):
        q = params["q"]
        orders = orders.filter(Q(jwo_no__icontains=q) | Q(vendor_name__icontains=q) | Q(process__icontains=q))
    rows = []
    kpi = defaultdict(lambda: ZERO)
    for order in orders:
        out = sum((D(o.qty) for o in order.outwards.all() if o.status in ("Sent", "Confirmed")), ZERO)
        back = sum((D(i.qty) for i in order.inwards.all()), ZERO)
        loss = sum((D(l.qty) for l in JWOLossEntry.objects.filter(job_work_order=order, status="Approved")), ZERO)
        pending = max(ZERO, out - back - loss)
        batches = sorted({
            code for code in JWOOutwardTaka.objects.filter(outward__job_work_order=order)
            .values_list("lot_taka__batch__batch_code", flat=True) if code
        })
        awaiting_qc = LotTaka.objects.filter(
            client_id=client_id, source_document_type="JobWorkInward", source_document_id=order.id,
            location=qc_hold, available_quantity__gt=0,
        ).aggregate(v=Sum("available_quantity"))["v"] or ZERO
        overdue = bool(order.expected_completion and order.expected_completion < today and pending > ZERO)
        if out <= ZERO and back <= ZERO:
            continue
        if params.get("status") == "pending" and pending <= ZERO:
            continue
        if params.get("status") == "overdue" and not overdue:
            continue
        row = {
            "jobWorkOrderId": str(order.id), "jwoNo": order.jwo_no, "batches": batches,
            "vendor": order.vendor_name or (order.vendor.name if order.vendor_id else None),
            "process": order.process, "outwardQty": str(round4(out)), "returnedQty": str(round4(back)),
            "processLoss": str(round4(loss)), "pendingQty": str(round4(pending)),
            "awaitingQcQty": str(round4(D(awaiting_qc))),
            "dueDate": order.expected_completion.isoformat() if order.expected_completion else None,
            "overdue": overdue, "status": order.status,
        }
        if include_cost:
            charges = D(order.rate or 0) * back + sum(
                (D(c.amount) for c in JWOCharge.objects.filter(job_work_order=order).exclude(status="Cancelled")), ZERO)
            row["charges"] = str(round2(charges))
        rows.append(row)
        if pending > ZERO:
            kpi["lotsUnderProcessing"] += 1
            kpi["pendingReturn"] += pending
        kpi["returnedAwaitingQc"] += D(awaiting_qc)
        if overdue:
            kpi["overdueLots"] += 1
    vendor_held = LotTaka.objects.filter(
        client_id=client_id, location__code__startswith="SYS-WIP-", available_quantity__gt=0, deleted_at__isnull=True,
    ).aggregate(v=Sum("available_quantity"))["v"] or ZERO
    in_store = LotTaka.objects.filter(
        client_id=client_id, location__code="SYS-REPROCESS", available_quantity__gt=0, deleted_at__isnull=True,
    ).aggregate(v=Sum("available_quantity"))["v"] or ZERO
    kpis = {k: (int(v) if k in ("lotsUnderProcessing", "overdueLots") else str(round4(v))) for k, v in kpi.items()}
    kpis["vendorHeld"] = str(round4(D(vendor_held)))
    kpis["awaitingReprocessInStore"] = str(round4(D(in_store)))
    page_rows, page, size = _page(rows, params)
    return {"kpis": kpis, "results": page_rows, "count": len(rows), "page": page, "page_size": size}


# ---------------------------------------------------------------------------
# 6. Customer Return Stock
# ---------------------------------------------------------------------------
CONDITION_STATE = {
    "Good": ("Accepted", "Back in stock"),
    "Second Quality": ("Accepted", "Second quality stock"),
    "Hold": ("Awaiting QC", "QC Hold"),
    "Reprocess": ("Reprocessing required", "Reprocess store"),
    "Reject": ("Rejected", "Awaiting decision"),
}


def customer_returns(client_id, params, include_cost=False):
    from apps.sales.models import SalesReturnLine

    lines = SalesReturnLine.objects.filter(
        client_id=client_id, deleted_at__isnull=True, sales_return__deleted_at__isnull=True,
    ).exclude(sales_return__status="Cancelled").select_related(
        "sales_return", "sales_return__party", "sales_return__sales_invoice", "lot_taka", "lot_taka__batch", "item",
    ).order_by("-sales_return__doc_date")
    if params.get("q"):
        q = params["q"]
        lines = lines.filter(Q(sales_return__return_number__icontains=q) | Q(sales_return__party_name__icontains=q)
                             | Q(lot_taka__batch__batch_code__icontains=q) | Q(item_name__icontains=q))
    if _date(params.get("from")):
        lines = lines.filter(sales_return__doc_date__gte=_date(params["from"]))
    if _date(params.get("to")):
        lines = lines.filter(sales_return__doc_date__lte=_date(params["to"]))
    rows = []
    kpi = defaultdict(lambda: ZERO)
    for line in lines:
        ret = line.sales_return
        condition = line.condition or "Hold"
        qc, disposition = CONDITION_STATE.get(condition, ("Awaiting QC", "QC Hold"))
        qty = D(line.returned_qty) * D(line.uom_factor or 1)
        kpi["total"] += qty
        kpi[{"Accepted": "accepted", "Awaiting QC": "awaitingQc", "Reprocessing required": "reprocess",
             "Rejected": "rejected"}[qc]] += qty
        if params.get("status") and params["status"] != qc:
            continue
        row = {
            "returnId": str(ret.id), "returnNumber": ret.return_number,
            "date": ret.doc_date.isoformat() if ret.doc_date else None,
            "customer": ret.party.name if ret.party_id else ret.party_name,
            "invoiceNumber": ret.sales_invoice.invoice_number if ret.sales_invoice_id else None,
            "batchCode": line.lot_taka.batch.batch_code if line.lot_taka_id and line.lot_taka.batch_id else None,
            "takaId": line.lot_taka.taka_id if line.lot_taka_id else None,
            "fabric": line.item_name or (line.item.name if line.item_id else None),
            "quantity": str(round4(qty)), "reason": ret.reason, "qcStatus": qc, "disposition": disposition,
            "creditNote": ret.credit_note_number,
        }
        if include_cost:
            row["creditValue"] = str(round2(D(line.line_total or 0)))
        rows.append(row)
    page_rows, page, size = _page(rows, params)
    return {"kpis": {k: str(round4(v)) for k, v in kpi.items()}, "results": page_rows,
            "count": len(rows), "page": page, "page_size": size}


# ---------------------------------------------------------------------------
# 2. Sample Inventory
# ---------------------------------------------------------------------------
def samples(client_id, params, include_cost=False):
    from .models import Sample, StockMovement

    qs = Sample.objects.filter(client_id=client_id, deleted_at__isnull=True).select_related(
        "party", "source_lot_taka", "source_lot_taka__batch", "fabric_quality", "design", "shade",
    ).order_by("-sample_date", "-created_at")
    if params.get("q"):
        q = params["q"]
        qs = qs.filter(Q(sample_number__icontains=q) | Q(party__name__icontains=q)
                       | Q(source_lot_taka__batch__batch_code__icontains=q))
    if _date(params.get("from")):
        qs = qs.filter(sample_date__gte=_date(params["from"]))
    if _date(params.get("to")):
        qs = qs.filter(sample_date__lte=_date(params["to"]))
    kpi = defaultdict(lambda: ZERO)
    rows = []
    today = timezone.localdate()
    for s in qs:
        out = D(s.quantity) - D(s.returned_quantity) - D(s.consumed_quantity)
        kpi["total"] += 1
        kpi["issuedQty"] += D(s.quantity)
        kpi["returnedQty"] += D(s.returned_quantity)
        kpi["consumedQty"] += D(s.consumed_quantity)
        if out > ZERO:
            kpi["outstanding"] += 1
        status = s.status
        if out > ZERO and s.is_returnable and s.due_date and s.due_date < today:
            status = "Overdue"
        if params.get("status") and params["status"] != status:
            continue
        row = {
            "sampleId": str(s.id), "sampleNumber": s.sample_number, "date": s.sample_date.isoformat(),
            "batchCode": s.source_lot_taka.batch.batch_code if s.source_lot_taka_id and s.source_lot_taka.batch_id else None,
            "takaId": s.source_lot_taka.taka_id if s.source_lot_taka_id else None,
            "party": s.party.name if s.party_id else None,
            "fabricQuality": s.fabric_quality.name if s.fabric_quality_id else None,
            "design": s.design.name if s.design_id else None, "shade": s.shade.name if s.shade_id else None,
            "type": s.get_sample_type_display(), "quantity": str(round4(D(s.quantity))),
            "returned": str(round4(D(s.returned_quantity))), "consumed": str(round4(D(s.consumed_quantity))),
            "outstanding": str(round4(max(ZERO, out))), "uom": s.uom, "returnable": s.is_returnable,
            "dueDate": s.due_date.isoformat() if s.due_date else None, "status": status,
        }
        if include_cost:
            row["estimatedCost"] = str(round2(D(s.estimated_cost or 0)))
        rows.append(row)
    under_qc = StockMovement.objects.filter(
        client_id=client_id, type="SAMPLE_RETURN_HOLD", quantity__gt=0, deleted_at__isnull=True,
    ).aggregate(v=Sum("quantity"))["v"] or ZERO
    kpis = {k: (int(v) if k in ("total", "outstanding") else str(round4(v))) for k, v in kpi.items()}
    kpis["returnedUnderQcQty"] = str(round4(D(under_qc)))
    page_rows, page, size = _page(rows, params)
    return {"kpis": kpis, "results": page_rows, "count": len(rows), "page": page, "page_size": size}


SCREENS = {
    "overview": fabric_overview,
    "samples": samples,
    "scrap": scrap_waste,
    "usable": usable_stock,
    "reprocessing": reprocessing,
    "customer-returns": customer_returns,
}
