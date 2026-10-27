"""
Fabric batch traceability (OSCAR batch flow).

A ``Batch`` is the permanent ID on a physical fabric batch's QR tag. Rolls
(``LotTaka``) carry it; processing keeps it. Everything a batch "has" -- its
quantities, history, cost and profit -- is read from the records that already
exist (takas, stock movements, QC lines, job work, lot cost entries, sales
documents) rather than stored twice.

Costing is batch-specific: the approved lot cost entries of every lot the
batch's rolls belong to, spread over the batch's good output (metres sold +
metres still in stock, outside the Reject location). Processing losses and
rejected metres therefore raise the cost of the metres that remain.
"""
from collections import defaultdict
from decimal import Decimal

from django.db import transaction
from django.db.models import Q, Sum

from apps.core.money import ZERO, D, round2, round4

#: ``LotTaka.source_document_type`` -> ``Batch.origin``.
ORIGIN_BY_SOURCE = {
    "GRN": "PURCHASE",
    "PurchaseBill": "PURCHASE",
    "Production": "PRODUCTION",
    "ProductionInstruction": "PRODUCTION",
    "JobWorkInward": "JOB_WORK",
    "LotMerge": "MERGE",
}

#: Movement types that are sales of the batch, and their reversals.
SALE_TYPES = ("SALE", "SALE_REVERSAL")
RETURN_TYPES = (
    "SALES_RETURN", "SALES_RETURN_GOOD", "SALES_RETURN_HOLD", "SALES_RETURN_REPROCESS",
    "SALES_RETURN_SECOND_QUALITY", "SALES_RETURN_REJECT", "RETURN_CANCELLATION",
)
#: Where a roll sits decides which bucket its metres count in.
BUCKET_BY_LOCATION_CODE = {
    "SYS-QC-HOLD": "qcHold",
    "SYS-REPROCESS": "rework",
    "SYS-SECOND-QUALITY": "secondQuality",
    "SYS-REJECT": "rejected",
}


def assign_batch(taka):
    """Give an unsaved/batchless roll its batch (called from ``LotTaka.save``).

    1. A roll split or processed from another roll keeps that roll's batch.
    2. Another roll of the same lot number that already has a batch shares it
       (a GRN line's rolls are one batch).
    3. Otherwise a new batch is opened with the next ``OSCAR-GRY`` number.
    """
    from apps.core.numbering import allocate_number

    from .models import Batch, LotTaka

    parent = taka.parent_taka
    if parent is not None and parent.batch_id:
        taka.batch_id = parent.batch_id
        return
    # One purchase order is one batch: every receipt against the PO (partial
    # or repeat deliveries) joins the batch its first receipt opened.
    po = _purchase_order_for(taka)
    if po is not None:
        existing = Batch.objects.filter(
            client_id=taka.client_id, source_document_type="PurchaseOrder",
            source_document_id=po.id, deleted_at__isnull=True,
        ).first()
        if existing is None:
            on_date = taka.received_date if hasattr(taka.received_date, "year") else None
            existing = Batch.objects.create(
                client_id=taka.client_id,
                batch_code=allocate_number(taka.client_id, "BAT", on_date),
                origin="PURCHASE",
                item_id=taka.item_id,
                supplier_id=po.party_id or taka.supplier_id,
                source_document_type="PurchaseOrder",
                source_document_number=po.po_number,
                source_document_id=po.id,
                **({"created_on": on_date} if on_date else {}),
                uom=taka.uom,
            )
        taka.batch = existing
        return
    sibling = (
        LotTaka.objects.filter(
            client_id=taka.client_id,
            lot_number=taka.lot_number,
            batch__isnull=False,
            deleted_at__isnull=True,
        )
        .exclude(pk=taka.pk)
        .values_list("batch_id", flat=True)
        .first()
    )
    if sibling:
        taka.batch_id = sibling
        return
    on_date = taka.received_date if hasattr(taka.received_date, "year") else None
    taka.batch = Batch.objects.create(
        client_id=taka.client_id,
        batch_code=allocate_number(taka.client_id, "BAT", on_date),
        origin=ORIGIN_BY_SOURCE.get(taka.source_document_type or "", "MANUAL"),
        item_id=taka.item_id,
        supplier_id=taka.supplier_id,
        source_document_type=taka.source_document_type,
        source_document_number=taka.source_document_number,
        source_document_id=taka.source_document_id,
        **({"created_on": on_date} if on_date else {}),
        uom=taka.uom,
    )


def _purchase_order_for(taka):
    """The PO a purchase-receipt roll was received against, if any."""
    if taka.source_document_type != "GRN" or not taka.source_document_id:
        return None
    from apps.purchase.models import GoodsReceipt

    receipt = (
        GoodsReceipt.objects.filter(pk=taka.source_document_id)
        .select_related("purchase_order")
        .first()
    )
    return receipt.purchase_order if receipt is not None and receipt.purchase_order_id else None


def find_batch(client_id, code):
    """A batch by its code, or by any of its rolls' taka ID / barcode / lot."""
    from .models import Batch, LotTaka

    code = (code or "").strip()
    if not code:
        return None
    batch = Batch.objects.filter(
        client_id=client_id, batch_code__iexact=code, deleted_at__isnull=True
    ).first()
    if batch is not None:
        return batch
    taka = (
        LotTaka.objects.filter(client_id=client_id, deleted_at__isnull=True, batch__isnull=False)
        .filter(Q(taka_id__iexact=code) | Q(barcode__iexact=code) | Q(lot_number__iexact=code))
        .select_related("batch")
        .first()
    )
    return taka.batch if taka else None


def _takas(batch):
    return list(
        batch.takas.filter(deleted_at__isnull=True)
        .select_related("item", "location", "fabric_quality", "design", "shade", "supplier")
        .order_by("created_at")
    )


def _bucket(taka):
    code = (getattr(taka.location, "code", "") or "").upper()
    if code in BUCKET_BY_LOCATION_CODE:
        return BUCKET_BY_LOCATION_CODE[code]
    if code.startswith("SYS-WIP"):
        return "withJobWorker"
    return "available"


def batch_quantities(batch, takas=None):
    """Metres by bucket, from where each roll is now plus the movement ledger."""
    from .models import StockMovement

    takas = takas if takas is not None else _takas(batch)
    buckets = defaultdict(lambda: ZERO)
    for taka in takas:
        qty = D(taka.available_quantity or 0)
        if qty <= ZERO:
            continue
        bucket = _bucket(taka)
        if bucket == "available" and taka.status == "Reserved":
            bucket = "reserved"
        buckets[bucket] += qty
    roots = [t for t in takas if t.parent_taka_id is None or t.parent_taka_id not in {x.id for x in takas}]
    received = sum((D(t.received_quantity or 0) for t in roots), ZERO)

    moves = StockMovement.objects.filter(lot_taka__in=takas, deleted_at__isnull=True)
    sold = -(moves.filter(type__in=SALE_TYPES).aggregate(v=Sum("quantity"))["v"] or ZERO)
    returned = moves.filter(type__in=RETURN_TYPES).aggregate(v=Sum("quantity"))["v"] or ZERO
    jw_out = -(moves.filter(type="JOBWORK_OUTWARD", quantity__lt=0).aggregate(v=Sum("quantity"))["v"] or ZERO)
    jw_in = sum(
        (D(t.received_quantity or 0) for t in takas if t.source_document_type == "JobWorkInward"),
        ZERO,
    )
    on_hand = sum(buckets.values(), ZERO)
    return {
        "received": round4(received),
        "onHand": round4(on_hand),
        "available": round4(buckets["available"]),
        "reserved": round4(buckets["reserved"]),
        "qcHold": round4(buckets["qcHold"]),
        "rework": round4(buckets["rework"]),
        "secondQuality": round4(buckets["secondQuality"]),
        "rejected": round4(buckets["rejected"]),
        "withJobWorker": round4(buckets["withJobWorker"]),
        "sentForJobWork": round4(jw_out),
        "returnedFromJobWork": round4(jw_in),
        "sold": round4(D(sold) - D(returned)),
        "uom": batch.uom or (takas[0].uom if takas else None),
    }


def batch_status(qty):
    if qty["onHand"] <= ZERO:
        return "Sold Out" if qty["sold"] > ZERO else "Closed"
    if qty["withJobWorker"] > ZERO:
        return "At Job Worker"
    if qty["qcHold"] > ZERO and qty["available"] <= ZERO:
        return "QC Hold"
    if qty["available"] > ZERO or qty["reserved"] > ZERO:
        return "In Stock"
    return "Blocked"


def _sale_rates(movements):
    """Selling rate per stock unit for each SALE movement, from its document line."""
    from apps.sales.models import DeliveryChallanLine, SalesInvoiceLine

    by_doc = defaultdict(list)
    for m in movements:
        by_doc[(m.reference_type, m.reference_id)].append(m)
    rates = {}
    for (ref_type, ref_id), moves in by_doc.items():
        if ref_type == "DeliveryChallan":
            lines = DeliveryChallanLine.objects.filter(delivery_challan_id=ref_id, deleted_at__isnull=True)
        elif ref_type == "SalesInvoice":
            lines = SalesInvoiceLine.objects.filter(sales_invoice_id=ref_id, deleted_at__isnull=True)
        else:
            continue
        per_item = defaultdict(lambda: [ZERO, ZERO])  # net value, stock qty
        for line in lines:
            stock_qty = D(line.qty or 0) * D(line.uom_factor or 1)
            per_item[line.item_id][0] += D(line.amount or 0) - D(line.discount_amount or 0)
            per_item[line.item_id][1] += stock_qty
        for m in moves:
            value, qty = per_item.get(m.item_id, (ZERO, ZERO))
            rates[m.id] = round4(value / qty) if qty > ZERO else None
    return rates


REJECT_IN_TYPES = ("QC_REJECT", "SALES_RETURN_REJECT", "SAMPLE_RETURN_REJECT")


def rejected_total(batch, takas=None):
    """Metres of the batch ever routed to Reject (QC or returns)."""
    from .models import StockMovement

    takas = takas if takas is not None else _takas(batch)
    value = StockMovement.objects.filter(
        lot_taka__in=takas, type__in=REJECT_IN_TYPES, quantity__gt=0, deleted_at__isnull=True,
    ).aggregate(v=Sum("quantity"))["v"]
    return round4(D(value or 0))


def batch_costing(batch, takas=None, qty=None):
    """Batch-specific cost and profit, with rejections split by cause.

    * Normal production / process loss is absorbed: its metres simply are not
      good output, so the batch cost spreads over fewer metres.
    * A claimable rejection (supplier, job worker or process fault) keeps its
      share of the cost, shown as a claim to recover rather than loaded onto
      the good fabric.
    Cost per metre = approved cost / (good output + claimable rejected metres);
    good output = sold + in stock outside Reject. Every rupee of approved cost
    lands in exactly one place: sold, unsold stock, or claims.
    """
    from .models import LotCostEntry, StockMovement

    takas = takas if takas is not None else _takas(batch)
    qty = qty or batch_quantities(batch, takas)
    lots = sorted({t.lot_number for t in takas})
    entries = LotCostEntry.objects.filter(
        client_id=batch.client_id, lot_number__in=lots, deleted_at__isnull=True
    ).order_by("date", "created_at")
    by_type = defaultdict(lambda: ZERO)
    pending = ZERO
    for e in entries:
        if e.is_approved:
            by_type[e.cost_type] += D(e.amount)
        else:
            pending += D(e.amount)
    total_cost = sum(by_type.values(), ZERO)

    sale_moves = list(
        StockMovement.objects.filter(lot_taka__in=takas, type="SALE", deleted_at__isnull=True)
    )
    rates = _sale_rates(sale_moves)
    gross_sold = sum((-D(m.quantity) for m in sale_moves), ZERO)
    priced = [(-D(m.quantity), rates.get(m.id)) for m in sale_moves if rates.get(m.id) is not None]
    priced_qty = sum((q for q, _ in priced), ZERO)
    avg_rate = (sum((q * r for q, r in priced), ZERO) / priced_qty) if priced_qty > ZERO else ZERO
    net_sold = D(qty["sold"])
    revenue = round2(avg_rate * net_sold)

    losses = list(batch.losses.filter(deleted_at__isnull=True).select_related("responsible_party"))
    claimable_qty = sum((D(l.quantity) for l in losses if l.claimable), ZERO)
    rejected = rejected_total(batch, takas)
    absorbed_qty = max(ZERO, rejected - claimable_qty)

    good_output = net_sold + D(qty["onHand"]) - D(qty["rejected"])
    base_qty = good_output + claimable_qty
    cost_per_unit = round4(total_cost / base_qty) if base_qty > ZERO else ZERO
    cogs = round2(cost_per_unit * net_sold)
    unsold_value = round2(cost_per_unit * (D(qty["onHand"]) - D(qty["rejected"])))
    # What the absorbed normal loss added to each good metre (for display).
    no_loss_rate = total_cost / (base_qty + absorbed_qty) if (base_qty + absorbed_qty) > ZERO else ZERO
    absorbed_cost = round2(no_loss_rate * absorbed_qty)

    claims = []
    claim_value = recovered = written_off = outstanding = ZERO
    for loss in losses:
        value = round2(cost_per_unit * D(loss.quantity)) if loss.claimable else ZERO
        got = D(loss.recovered_amount)
        claims.append({
            "id": str(loss.id),
            "date": _date(loss.loss_date),
            "quantity": str(loss.quantity),
            "cause": loss.cause,
            "causeLabel": loss.get_cause_display(),
            "claimable": loss.claimable,
            "responsiblePartyId": str(loss.responsible_party_id) if loss.responsible_party_id else None,
            "responsibleParty": loss.responsible_party.name if loss.responsible_party_id else None,
            "process": loss.process,
            "outcome": loss.outcome,
            "outcomeLabel": loss.get_outcome_display(),
            "claimStatus": loss.claim_status,
            "claimValue": str(value),
            "recoveredAmount": str(round2(got)),
            "reference": loss.reference,
            "notes": loss.notes,
        })
        if not loss.claimable:
            continue
        claim_value += value
        recovered += got
        if loss.claim_status == "WRITTEN_OFF":
            written_off += max(ZERO, value - got)
        elif loss.claim_status != "RECOVERED":
            outstanding += max(ZERO, value - got)

    realized = round2(revenue - cogs)
    return {
        "lots": lots,
        "costByType": {k: str(round2(v)) for k, v in by_type.items()},
        "totalCost": str(round2(total_cost)),
        "pendingCost": str(round2(pending)),
        "goodOutput": str(round4(good_output)),
        "costPerUnit": str(cost_per_unit),
        "soldQty": str(round4(net_sold)),
        "unpricedSoldQty": str(round4(gross_sold - priced_qty)),
        "averageSellingRate": str(round4(avg_rate)),
        "revenue": str(revenue),
        "costOfSold": str(cogs),
        "realizedProfit": str(realized),
        "unsoldInventoryValue": str(unsold_value),
        "rejectedQty": str(rejected),
        "normalLossQty": str(round4(absorbed_qty)),
        "normalLossAbsorbedCost": str(absorbed_cost),
        "claimableQty": str(round4(claimable_qty)),
        "claimValue": str(round2(claim_value)),
        "recoveredAmount": str(round2(recovered)),
        "outstandingClaim": str(round2(outstanding)),
        "writtenOffLoss": str(round2(written_off)),
        "netResult": str(round2(realized - written_off)),
        "losses": claims,
        "rule": (
            "Batch-specific. Cost per metre = approved cost ÷ (good output + claimable rejected metres). "
            "Normal production loss is absorbed by the good fabric; supplier / job-worker / process "
            "rejections keep their share as a claim to recover. Written-off claims are a batch loss."
        ),
    }


@transaction.atomic
def record_loss(batch, *, quantity, cause, responsible_party_id=None, process=None, outcome="PENDING",
                reference=None, notes=None, loss_date=None, user=None):
    """Classify rejected metres of a batch (see ``BatchLoss``)."""
    from apps.core.audit import record_audit
    from apps.core.exceptions import ValidationFailed
    from apps.masters.models import Party

    from .models import Batch, BatchLoss

    batch = Batch.objects.select_for_update().get(pk=batch.pk)
    quantity = round4(D(quantity or 0))
    if quantity <= ZERO:
        raise ValidationFailed("Enter the rejected quantity.", field_errors={"quantity": ["Must be above 0."]})
    if cause not in dict(BatchLoss.CAUSES):
        raise ValidationFailed("Choose the cause.", field_errors={"cause": [c for c, _ in BatchLoss.CAUSES]})
    if outcome not in dict(BatchLoss.OUTCOMES):
        raise ValidationFailed("Unknown outcome.", field_errors={"outcome": [c for c, _ in BatchLoss.OUTCOMES]})
    rejected = rejected_total(batch)
    classified = sum((D(l.quantity) for l in batch.losses.filter(deleted_at__isnull=True)), ZERO)
    if classified + quantity > rejected:
        raise ValidationFailed(
            f"Only {round4(rejected - classified)} rejected metres of {batch.batch_code} are not yet "
            f"classified (rejected {rejected}, classified {round4(classified)}).",
            field_errors={"quantity": ["More than the batch's unclassified rejection."]},
        )
    party = None
    if cause != "NORMAL_LOSS":
        party = Party.objects.filter(client_id=batch.client_id, pk=responsible_party_id).first() \
            if responsible_party_id else None
        if party is None and cause in ("SUPPLIER_QUALITY", "JOB_WORKER_FAULT"):
            raise ValidationFailed("Choose the supplier / job worker responsible.",
                                   field_errors={"responsiblePartyId": ["Required for a claim."]})
    loss = BatchLoss.objects.create(
        client_id=batch.client_id, batch=batch, quantity=quantity, cause=cause, responsible_party=party,
        process=process or None, outcome=outcome,
        claim_status="NOT_APPLICABLE" if cause == "NORMAL_LOSS" else "OPEN",
        reference=reference or None, notes=notes or None,
        **({"loss_date": loss_date} if loss_date else {}),
        created_by=user if getattr(user, "is_authenticated", False) else None,
    )
    record_audit(
        client=batch.client_id, actor=user, action="batch_loss", entity_type="Batch", entity_id=batch.id,
        entity_label=batch.batch_code, description=f"{quantity} rejected: {loss.get_cause_display()}",
        after={"lossId": str(loss.id), "cause": cause, "quantity": str(quantity), "outcome": outcome},
    )
    return loss


@transaction.atomic
def update_loss(loss, *, user=None, **changes):
    """Move a claim along: outcome, claim status, amount recovered, reference."""
    from apps.core.audit import record_audit
    from apps.core.exceptions import ValidationFailed

    from .models import BatchLoss

    loss = BatchLoss.objects.select_for_update().get(pk=loss.pk)
    before = {"outcome": loss.outcome, "claimStatus": loss.claim_status, "recovered": str(loss.recovered_amount)}
    if "outcome" in changes and changes["outcome"] is not None:
        if changes["outcome"] not in dict(BatchLoss.OUTCOMES):
            raise ValidationFailed("Unknown outcome.", field_errors={"outcome": ["Invalid."]})
        loss.outcome = changes["outcome"]
    if "claim_status" in changes and changes["claim_status"] is not None:
        if not loss.claimable:
            raise ValidationFailed("A normal loss has no claim.", field_errors={"claimStatus": ["Not applicable."]})
        if changes["claim_status"] not in dict(BatchLoss.CLAIM_STATUSES) or changes["claim_status"] == "NOT_APPLICABLE":
            raise ValidationFailed("Unknown claim status.", field_errors={"claimStatus": ["Invalid."]})
        loss.claim_status = changes["claim_status"]
    if "recovered_amount" in changes and changes["recovered_amount"] not in (None, ""):
        amount = round2(D(changes["recovered_amount"]))
        if amount < ZERO:
            raise ValidationFailed("Recovered amount cannot be negative.", field_errors={"recoveredAmount": ["≥ 0."]})
        if not loss.claimable and amount > ZERO:
            raise ValidationFailed("A normal loss has nothing to recover.", field_errors={"recoveredAmount": ["Not applicable."]})
        # The total recovered, not an addition -- re-saving cannot double it.
        loss.recovered_amount = amount
    for field in ("reference", "notes"):
        if changes.get(field) is not None:
            setattr(loss, field, changes[field] or None)
    loss.save()
    record_audit(
        client=loss.client_id, actor=user, action="batch_loss_update", entity_type="Batch",
        entity_id=loss.batch_id, entity_label=loss.batch.batch_code, description="Rejection / claim updated",
        before=before,
        after={"outcome": loss.outcome, "claimStatus": loss.claim_status, "recovered": str(loss.recovered_amount)},
    )
    return loss


def _date(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


def batch_timeline(batch, takas=None, include_cost=False):
    """One chronological list of everything that happened to the batch."""
    from apps.jobwork.models import JWOInwardTaka, JWOOutwardTaka

    from .models import LotCostEntry, QCInspectionLine, StockMovement

    takas = takas if takas is not None else _takas(batch)
    events = [{
        "date": _date(batch.created_on),
        "kind": "BATCH_CREATED",
        "title": f"Batch {batch.batch_code} created ({batch.get_origin_display()})",
        "reference": batch.source_document_number,
        "referenceType": batch.source_document_type,
    }]

    for m in (
        StockMovement.objects.filter(lot_taka__in=takas, deleted_at__isnull=True)
        .select_related("location", "lot_taka", "created_by")
        .order_by("movement_date", "created_at")
    ):
        event = {
            "date": _date(m.movement_date),
            "at": _date(m.created_at),
            "kind": "MOVEMENT",
            "type": m.type,
            "title": m.type.replace("_", " ").title(),
            "quantity": str(m.quantity),
            "uom": m.uom,
            "location": m.location.name if m.location_id else None,
            "taka": m.lot_taka.taka_id if m.lot_taka_id else None,
            "reference": m.reference_number,
            "referenceType": m.reference_type,
            "by": getattr(m.created_by, "name", None),
            "notes": m.notes,
        }
        if include_cost and m.unit_cost is not None:
            event["unitCost"] = str(m.unit_cost)
        events.append(event)

    qc_lines = (
        QCInspectionLine.objects.filter(lot_taka__in=takas, deleted_at__isnull=True)
        .select_related("inspection", "lot_taka")
    )
    for line in qc_lines:
        insp = line.inspection
        events.append({
            "date": _date(insp.inspection_date or insp.created_at),
            "kind": "QC",
            "title": f"QC {insp.inspection_number} — {line.disposition}",
            "quantity": str(line.actual_meter or line.original_meter or ""),
            "taka": line.lot_taka.taka_id if line.lot_taka_id else None,
            "reference": insp.inspection_number,
            "referenceType": insp.source_document_type,
            "sourceReference": insp.source_document_number,
            "status": insp.status,
            "grade": line.grade,
        })

    for row in JWOOutwardTaka.objects.filter(lot_taka__in=takas).select_related(
        "outward", "outward__job_work_order", "lot_taka"
    ):
        out = row.outward
        order = out.job_work_order
        events.append({
            "date": _date(out.date),
            "kind": "JOB_WORK_OUT",
            "title": f"Sent for {getattr(order, 'process', None) or 'job work'}",
            "quantity": str(row.meters),
            "taka": row.lot_taka.taka_id if row.lot_taka_id else None,
            "reference": out.no or getattr(order, "jwo_no", None),
            "challan": out.challan_no,
            "referenceType": "JobWorkOrder",
        })
    for row in JWOInwardTaka.objects.filter(
        Q(source_taka__in=takas) | Q(received_taka__in=takas)
    ).select_related("inward", "inward__job_work_order", "received_taka"):
        inward = row.inward
        order = inward.job_work_order
        events.append({
            "date": _date(inward.date),
            "kind": "JOB_WORK_IN",
            "title": f"Received back from {getattr(order, 'process', None) or 'job work'}",
            "quantity": str(row.meters),
            "taka": row.received_taka.taka_id if row.received_taka_id else None,
            "reference": inward.no,
            "challan": inward.challan_no,
            "referenceType": "JobWorkOrder",
        })

    if include_cost:
        lots = {t.lot_number for t in takas}
        for e in LotCostEntry.objects.filter(
            client_id=batch.client_id, lot_number__in=lots, deleted_at__isnull=True
        ).order_by("date"):
            events.append({
                "date": _date(e.date),
                "kind": "COST",
                "title": e.cost_type,
                "amount": str(round2(D(e.amount))),
                "status": e.approval_status,
                "reference": e.source_document_number,
                "referenceType": e.source_document_type,
                "notes": e.remarks,
            })

    for loss in batch.losses.filter(deleted_at__isnull=True).select_related("responsible_party"):
        events.append({
            "date": _date(loss.loss_date),
            "kind": "LOSS",
            "title": f"Rejection classified: {loss.get_cause_display()} — {loss.get_outcome_display()}"
                     + (f" ({loss.responsible_party.name})" if loss.responsible_party_id else ""),
            "quantity": str(loss.quantity),
            "reference": loss.reference,
            "status": loss.get_claim_status_display() if loss.claimable else None,
            "notes": loss.notes,
        })

    events.sort(key=lambda ev: (str(ev.get("date") or ""), str(ev.get("at") or "")))
    return events


def serialize_batch(batch, *, takas=None, include_cost=False, detail=False):
    takas = takas if takas is not None else _takas(batch)
    qty = batch_quantities(batch, takas)
    first = takas[0] if takas else None
    data = {
        "id": str(batch.id),
        "batchCode": batch.batch_code,
        "origin": batch.origin,
        "ownership": batch.ownership,
        "ownerPartyName": batch.owner_party.name if batch.owner_party_id else None,
        "item": (batch.item or getattr(first, "item", None)).name if (batch.item_id or first) else None,
        "fabricQuality": getattr(getattr(first, "fabric_quality", None), "name", None),
        "design": getattr(getattr(first, "design", None), "name", None),
        "shade": getattr(getattr(first, "shade", None), "name", None),
        "width": str(first.width) if first and first.width is not None else None,
        "gsm": str(first.gsm) if first and first.gsm is not None else None,
        "supplierName": batch.supplier.name if batch.supplier_id else None,
        "sourceDocumentType": batch.source_document_type,
        "sourceDocumentNumber": batch.source_document_number,
        "createdOn": _date(batch.created_on),
        "lots": sorted({t.lot_number for t in takas}),
        "takaCount": len(takas),
        "quantities": {k: (str(v) if isinstance(v, Decimal) else v) for k, v in qty.items()},
        "status": batch_status(qty),
        "parentBatches": [b.batch_code for b in batch.parent_batches.all()],
        "childBatches": [b.batch_code for b in batch.child_batches.all()],
    }
    if detail:
        data["takas"] = [{
            "id": str(t.id),
            "takaId": t.taka_id,
            "lotNumber": t.lot_number,
            "takaNumber": t.taka_number,
            "location": t.location.name if t.location_id else None,
            "locationCode": t.location.code if t.location_id else None,
            "status": t.status,
            "qcStatus": t.qc_status,
            "receivedQuantity": str(t.received_quantity),
            "availableQuantity": str(t.available_quantity),
            "parentTakaId": str(t.parent_taka_id) if t.parent_taka_id else None,
            "sourceDocumentType": t.source_document_type,
            "sourceDocumentNumber": t.source_document_number,
        } for t in takas]
        data["timeline"] = batch_timeline(batch, takas, include_cost=include_cost)
    if include_cost:
        data["costing"] = batch_costing(batch, takas, qty)
    return data


def eligible_sale_stock(client_id, item_id, *, location_id=None, batch_code=None, order_id=None):
    """Batches of ``item_id`` that can be sold now, with their takas.

    Sellable = QC passed, in stock, not in a QC/reprocess/reject/job-worker
    location, owned by OSCAR, and not reserved for another order (takas
    reserved for ``order_id`` are included and flagged).
    """
    from apps.sales.services import SELLABLE_LOT_QC, _taka_ids

    from .models import LotTaka

    takas = (
        LotTaka.objects.filter(
            client_id=client_id, item_id=item_id, deleted_at__isnull=True,
            available_quantity__gt=0, qc_status__in=SELLABLE_LOT_QC,
        )
        .exclude(location__code__startswith="SYS-")
        .exclude(batch__ownership__in=["CUSTOMER", "THIRD_PARTY"])
        .select_related("batch", "location")
        .order_by("received_date", "created_at", "taka_number")
    )
    if location_id:
        takas = takas.filter(location_id=location_id)
    if batch_code:
        batch = find_batch(client_id, batch_code)
        takas = takas.filter(batch=batch) if batch else takas.none()

    own = set()
    if order_id:
        from apps.sales.models import SalesOrderLine

        for line in SalesOrderLine.objects.filter(sales_order_id=order_id, deleted_at__isnull=True):
            own |= _taka_ids(line.reserved_takas) | _taka_ids(getattr(line, "taka_allocations", None))

    grouped = {}
    for t in takas:
        reserved_here = str(t.id) in own
        if t.status == "Reserved" and not reserved_here:
            continue
        b = grouped.setdefault(t.batch_id, {
            "batchCode": t.batch.batch_code if t.batch_id else t.lot_number,
            "batchId": str(t.batch_id) if t.batch_id else None,
            "available": ZERO,
            "takas": [],
        })
        b["available"] += D(t.available_quantity)
        b["takas"].append({
            "lotTakaId": str(t.id),
            "takaId": t.taka_id,
            "lotNumber": t.lot_number,
            "takaNumber": t.taka_number,
            "available": str(round4(D(t.available_quantity))),
            "location": t.location.name if t.location_id else None,
            "locationId": str(t.location_id),
            "reservedForThisOrder": reserved_here,
        })
    rows = list(grouped.values())
    for row in rows:
        row["available"] = str(round4(row["available"]))
    return rows


CHARGE_BASES = ("METER", "TAKA", "MANUAL")


def _posting_lot(batch, takas):
    """The lot a new batch charge is booked to: where the batch's stock is now."""
    in_stock = [t for t in takas if D(t.available_quantity or 0) > ZERO]
    pool = in_stock or takas
    return max(pool, key=lambda t: t.created_at).lot_number if pool else None


def allocate_charge(*, client_id, batch_codes, amount, cost_type, basis, manual=None,
                    reference=None, remarks=None, user=None):
    """Spread one charge (e.g. a tempo's freight) over several batches.

    ``basis`` is the user's choice: METER (by metres in hand), TAKA (by rolls in
    hand) or MANUAL (``manual`` = {batchCode: amount}, adding up to ``amount``).
    Each share becomes an approved lot cost entry on the batch, so it shows in
    the batch's cost and profit. A ``reference`` (bill / LR / trip number) can
    be charged to a batch only once per cost type.
    """
    from django.db import transaction

    from apps.core.exceptions import BusinessRuleViolation, ValidationFailed

    from . import costing_services
    from .models import LotCostEntry

    amount = round2(D(amount or 0))
    if amount <= ZERO:
        raise ValidationFailed("Enter the charge amount.", field_errors={"amount": ["Must be above 0."]})
    basis = (basis or "").upper()
    if basis not in CHARGE_BASES:
        raise ValidationFailed("Choose how to split the charge.", field_errors={"basis": ["METER, TAKA or MANUAL."]})
    if cost_type not in dict(LotCostEntry.COST_TYPES):
        raise ValidationFailed("Unknown cost type.", field_errors={"costType": ["Not a lot cost type."]})
    codes = [c for c in dict.fromkeys(batch_codes or []) if c]
    if not codes:
        raise ValidationFailed("Pick at least one batch.", field_errors={"batches": ["Required."]})

    with transaction.atomic():
        batches = []
        for code in codes:
            batch = find_batch(client_id, code)
            if batch is None:
                raise ValidationFailed(f"Batch {code} was not found.", field_errors={"batches": [code]})
            batches.append(batch)

        weights = []
        for batch in batches:
            takas = _takas(batch)
            in_hand = [t for t in takas if D(t.available_quantity or 0) > ZERO]
            if basis == "METER":
                weight = sum((D(t.available_quantity) for t in in_hand), ZERO)
            elif basis == "TAKA":
                weight = D(len(in_hand))
            else:
                weight = round2(D((manual or {}).get(batch.batch_code) or 0))
            weights.append((batch, takas, weight))

        total_weight = sum((w for _, _, w in weights), ZERO)
        if basis == "MANUAL":
            if total_weight != amount:
                raise ValidationFailed(
                    f"The batch amounts add up to {total_weight}, the charge is {amount}.",
                    field_errors={"manual": ["Must add up to the charge amount."]},
                )
        elif total_weight <= ZERO:
            raise BusinessRuleViolation(
                "None of these batches has stock in hand to split the charge by; use a manual split.",
                code="NOTHING_TO_SPLIT_BY",
            )

        rows = []
        allotted = ZERO
        for index, (batch, takas, weight) in enumerate(weights):
            if basis == "MANUAL":
                share = weight
            elif index == len(weights) - 1:
                share = amount - allotted  # the last batch takes the rounding paisa
            else:
                share = round2(amount * weight / total_weight)
            allotted += share
            if share <= ZERO:
                continue
            lots = {t.lot_number for t in takas}
            if reference and LotCostEntry.objects.filter(
                client_id=client_id, lot_number__in=lots, cost_type=cost_type,
                source_document_type="BATCH_CHARGE", source_document_number=reference,
                deleted_at__isnull=True,
            ).exists():
                raise BusinessRuleViolation(
                    f"{reference} is already charged to batch {batch.batch_code} as {cost_type}.",
                    code="DUPLICATE_CHARGE",
                )
            entry = costing_services.post_lot_cost(
                client_id=client_id,
                lot_number=_posting_lot(batch, takas),
                cost_type=cost_type,
                amount=share,
                quantity=weight if basis == "METER" else ZERO,
                source_document_type="BATCH_CHARGE",
                source_document_number=reference,
                user=user,
                remarks=(remarks or "") + f" [split by {basis.lower()}: {weight} of {total_weight}]",
            )
            rows.append({
                "batchCode": batch.batch_code,
                "lotNumber": entry.lot_number,
                "basisQuantity": str(weight),
                "amount": str(round2(share)),
                "entryId": str(entry.id),
            })
    return rows


def set_ownership(batch, *, ownership, owner_party_id=None, user=None, reason=None):
    """Mark who owns a batch's material (PDF: customer manufacturing).

    Customer or third-party material is never sold as OSCAR stock and is
    delivered only to its owner; OSCAR's own batches carry no owner.
    """
    from apps.core.audit import record_audit
    from apps.core.exceptions import ValidationFailed
    from apps.masters.models import Party

    ownership = (ownership or "").upper()
    if ownership not in dict(batch.OWNERSHIPS):
        raise ValidationFailed("Unknown ownership.", field_errors={"ownership": ["OSCAR, CUSTOMER or THIRD_PARTY."]})
    party = None
    if ownership != "OSCAR":
        party = Party.objects.filter(client_id=batch.client_id, pk=owner_party_id, deleted_at__isnull=True).first() \
            if owner_party_id else None
        if party is None:
            raise ValidationFailed("Choose who owns this material.", field_errors={"ownerPartyId": ["Required."]})
        # Material OSCAR bought or made from its own yarn is OSCAR's asset;
        # relabelling it would drop it from the books without an entry.
        from .models import LotCostEntry

        bought = LotCostEntry.objects.filter(
            client_id=batch.client_id, lot_number__in={t.lot_number for t in _takas(batch)},
            cost_type__in=["Grey purchase", "Yarn / raw material"], is_approved=True, deleted_at__isnull=True,
        ).exists()
        if bought:
            raise ValidationFailed(
                f"{batch.batch_code} was bought or made by OSCAR (it carries purchase / yarn cost), "
                "so it cannot be marked as customer material.",
                field_errors={"ownership": ["Batch has OSCAR purchase cost."]},
            )
    before = {"ownership": batch.ownership, "ownerParty": str(batch.owner_party_id or "")}
    batch.ownership = ownership
    batch.owner_party = party
    batch.save(update_fields=["ownership", "owner_party", "updated_at"])
    record_audit(
        client=batch.client_id, actor=user, action="batch_ownership", entity_type="Batch",
        entity_id=batch.id, entity_label=batch.batch_code,
        description=f"Ownership set to {ownership}" + (f" ({party.name})" if party else ""),
        before=before, after={"ownership": ownership, "ownerParty": str(party.id) if party else ""},
        comments=reason or None,
    )
    return batch



@transaction.atomic
def receive_customer_material(*, client, customer, item, location, rolls, reference=None, user=None,
                              received_on=None):
    """Customer's own fabric arriving (for processing / job work on their behalf).

    Opens a CUSTOMER-owned batch; the rolls carry no cost and post no journal,
    so OSCAR's inventory value and books are untouched.
    """
    import uuid

    from apps.core.exceptions import ValidationFailed
    from apps.core.numbering import allocate_number

    from . import services as stock
    from .models import Batch, LotTaka

    rolls = [r for r in (rolls or []) if D(r.get("meters") or 0) > ZERO]
    if not rolls:
        raise ValidationFailed("Enter at least one roll with metres.", field_errors={"rolls": ["Required."]})
    client_id = getattr(client, "id", client)
    receipt_no = allocate_number(client, "CMR", received_on)
    receipt_id = uuid.uuid4()
    lot_number = allocate_number(client, "LOT", received_on)
    batch = Batch.objects.create(
        client_id=client_id,
        batch_code=allocate_number(client, "BAT", received_on),
        origin="MANUAL",
        ownership="CUSTOMER",
        owner_party=customer,
        item=item,
        source_document_type="CustomerMaterialReceipt",
        source_document_number=receipt_no,
        source_document_id=receipt_id,
        uom=item.uom,
        notes=reference,
        **({"created_on": received_on} if received_on else {}),
    )
    created = []
    for index, roll in enumerate(rolls, start=1):
        meters = round4(D(roll.get("meters")))
        code = allocate_number(client, "TAK")
        taka = LotTaka.objects.create(
            client_id=client_id, batch=batch, item=item, location=location, lot_number=lot_number,
            taka_number=str(index), taka_id=code, barcode=code, received_quantity=meters,
            available_quantity=ZERO, uom=item.uom, status="Available", qc_status="Passed",
            source_document_type="CustomerMaterialReceipt", source_document_id=receipt_id,
            source_document_number=receipt_no, supplier=customer,
            **({"received_date": received_on} if received_on else {}),
        )
        stock.post_movement(
            client_id=client_id, item=item, location=location, type="CUSTOMER_IN", quantity=meters,
            unit_cost=None, reference_type="CustomerMaterialReceipt", reference_id=receipt_id,
            reference_number=receipt_no, lot_taka=taka, user=user,
            notes=f"Customer material from {customer.name}" + (f" ({reference})" if reference else ""),
        )
        created.append(taka)
    return batch, receipt_no


@transaction.atomic
def deliver_to_owner(batch, *, rolls, reference=None, user=None):
    """Hand customer material back to its owner: [{lotTakaId, qty}]."""
    import uuid

    from apps.core.exceptions import BusinessRuleViolation, ValidationFailed
    from apps.core.numbering import allocate_number

    from . import services as stock
    from .models import LotTaka

    if batch.ownership == "OSCAR":
        raise BusinessRuleViolation("This batch is OSCAR stock; sell it through a sales document.",
                                    code="NOT_CUSTOMER_MATERIAL")
    delivery_no = allocate_number(batch.client_id, "CMD")
    delivery_id = uuid.uuid4()
    moved = []
    for row in rolls or []:
        taka = LotTaka.objects.select_for_update().filter(
            client_id=batch.client_id, batch=batch, pk=row.get("lotTakaId"), deleted_at__isnull=True,
        ).first()
        qty = round4(D(row.get("qty") or 0))
        if taka is None or qty <= ZERO:
            raise ValidationFailed("Pick the rolls and quantities to deliver.", field_errors={"rolls": ["Invalid row."]})
        if (taka.location.code or "").startswith("SYS-"):
            raise BusinessRuleViolation(
                f"Roll {taka.taka_id} is at {taka.location.name}; it can be delivered once it is back in a godown.",
                code="ROLL_NOT_READY",
            )
        moved.append(stock.post_movement(
            client_id=batch.client_id, item=taka.item, location=taka.location, type="CUSTOMER_OUT",
            quantity=-qty, unit_cost=None, reference_type="CustomerMaterialDelivery", reference_id=delivery_id,
            reference_number=delivery_no, lot_taka=taka, user=user,
            notes=f"Delivered to {batch.owner_party.name if batch.owner_party_id else 'owner'}"
                  + (f" ({reference})" if reference else ""),
        ))
    if not moved:
        raise ValidationFailed("Pick the rolls to deliver.", field_errors={"rolls": ["Required."]})
    return delivery_no


def customer_owned_qty_by_item_location(client_id):
    """{(item_id, location_id): metres} of material OSCAR holds for others."""
    from .models import LotTaka

    out = defaultdict(lambda: ZERO)
    for item_id, location_id, qty in LotTaka.objects.filter(
        client_id=client_id, deleted_at__isnull=True, available_quantity__gt=0,
        batch__ownership__in=["CUSTOMER", "THIRD_PARTY"],
    ).values_list("item_id", "location_id", "available_quantity"):
        out[(item_id, location_id)] += D(qty)
    return out


def physical_stock_summary(client_id):
    """Everything physically held, split OSCAR-owned vs each customer's
    material, by state; customers also show what was delivered back."""
    from .models import LotTaka, StockMovement

    states = ("available", "reserved", "qcHold", "rework", "secondQuality", "rejected", "withJobWorker")
    summary = {}

    def row_for(ownership, party_id, party_name):
        key = "OSCAR" if ownership == "OSCAR" else f"{ownership}:{party_id}"
        return summary.setdefault(key, {
            "ownership": ownership, "ownerPartyId": str(party_id) if party_id else None,
            "ownerPartyName": party_name, **{state: ZERO for state in states},
            "deliveredBack": ZERO, "batches": set(),
        })

    for t in LotTaka.objects.filter(
        client_id=client_id, deleted_at__isnull=True, available_quantity__gt=0,
    ).select_related("location", "batch", "batch__owner_party"):
        batch = t.batch
        ownership = batch.ownership if batch else "OSCAR"
        row = row_for(ownership, batch.owner_party_id if batch else None,
                      batch.owner_party.name if batch and batch.owner_party_id else None)
        bucket = _bucket(t)
        if bucket == "available" and t.status == "Reserved":
            bucket = "reserved"
        row[bucket] += D(t.available_quantity)
        row["batches"].add(t.batch_id)

    for ownership, party_id, party_name, qty in StockMovement.objects.filter(
        client_id=client_id, type="CUSTOMER_OUT", deleted_at__isnull=True,
    ).values_list("lot_taka__batch__ownership", "lot_taka__batch__owner_party_id",
                  "lot_taka__batch__owner_party__name", "quantity"):
        row_for(ownership or "CUSTOMER", party_id, party_name)["deliveredBack"] += -D(qty)

    rows = []
    for row in summary.values():
        row["batches"] = len(row["batches"])
        row["inHand"] = sum((row[s] for s in states), ZERO)
        rows.append({k: (str(round4(v)) if isinstance(v, Decimal) else v) for k, v in row.items()})
    rows.sort(key=lambda r: (r["ownership"] != "OSCAR", r.get("ownerPartyName") or ""))
    return rows
