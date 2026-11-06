"""Flow 2: grey fabric already at OSCAR -> dyeing mill -> OSCAR warehouse.

A Flow 2 purchase order is a dyeing order on the mill. It buys no fabric: it
names a grey fabric PO (``source_purchase_order``) and, line by line, which of
that PO's batches go for dyeing and how much (``PurchaseOrderLine.source_batch``).
The line's rate is the dyeing rate. Saving a draft moves nothing.

* Approving the order is the dispatch: one job work order with one outward
  challan. The accepted grey rolls move to the mill's job work location through
  the stock ledger (``stock.relocate_lot``) and keep their grey batch.
* A return is an ordinary GRN of the mill on this PO, tied to the outward
  (``GoodsReceipt.dyeing_outward``). Each returned line opens a Dyed Fabric
  batch whose parent is the grey batch, lands at QC Hold and goes through the
  existing QC. The same metres leave the mill's job work location.
* A shortfall stays "at the mill": nothing here calls it loss or wastage.
* The mill's charge is the ordinary vendor bill on this PO; each batch's charge
  reaches that batch when the bill is approved.

All quantities are read from the ledger-backed records.
"""
from collections import defaultdict

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date

from apps.core.exceptions import BusinessRuleViolation, ValidationFailed
from apps.core.money import ZERO, D, round4

from . import fabric_flow
from .models import (
    FABRIC_FLOW_FACTORY_TO_OSCAR,
    FABRIC_FLOW_OSCAR_TO_MILL,
    GREY_STOCK_PURCHASE_TYPES,
    PO_RECEIVABLE_STATUSES,
    GoodsReceipt,
    GoodsReceiptLine,
    PurchaseOrder,
)

PASSED_QC = ("Passed", "Pass", "Approved")
#: A roll in one of these is promised to, or already on, another movement.
BLOCKED_STATUSES = (
    "Reserved", "External WIP", "Sample", "Reprocess", "Reject", "Rejected",
    "Dispatched", "QC Hold", "Scrapped", "Consumed",
)
TOLERANCE = fabric_flow.TOLERANCE


def require_flow(po):
    fabric_flow.require_flow(po, FABRIC_FLOW_OSCAR_TO_MILL)


def _source_lines(po):
    """``{grey batch id: this order's line}``."""
    lines = fabric_flow._live(po.line_items).filter(source_batch__isnull=False)
    return {line.source_batch_id: line for line in lines}


def _grey_batches(po):
    from apps.inventory.models import Batch

    return list(
        Batch.objects.filter(pk__in=list(_source_lines(po)), deleted_at__isnull=True)
        .select_related("item", "goods_receipt", "goods_receipt_line").order_by("created_at")
    )


def _eligible_takas(batch, *, lock=False):
    """QC-accepted rolls of ``batch`` in a real godown, free to move."""
    takas = batch.takas.filter(
        deleted_at__isnull=True, available_quantity__gt=ZERO, qc_status__in=PASSED_QC,
    ).exclude(status__in=BLOCKED_STATUSES).exclude(location__code__startswith="SYS-")
    if lock:
        takas = takas.select_for_update()
    return list(takas.order_by("created_at"))


def _accepted_qty(batch):
    from django.db.models import Sum

    from apps.inventory.models import StockMovement

    total = StockMovement.objects.filter(
        client_id=batch.client_id, lot_taka__batch=batch, type="QC_PASS", quantity__gt=ZERO,
        deleted_at__isnull=True, reversal_movement__isnull=True,
    ).exclude(location__code__startswith="SYS-").aggregate(v=Sum("quantity"))["v"]
    return round4(D(total or 0))


def _is_grey_source(po):
    if po.fabric_flow:
        return po.fabric_flow == FABRIC_FLOW_FACTORY_TO_OSCAR
    return po.purchase_type in GREY_STOCK_PURCHASE_TYPES


def _batch_row(batch):
    line = batch.goods_receipt_line
    return {
        "batchId": str(batch.id),
        "batchCode": batch.batch_code,
        "grnNumber": batch.goods_receipt.grn_number if batch.goods_receipt_id else batch.source_document_number,
        "itemId": str(batch.item_id) if batch.item_id else None,
        "fabric": batch.item.name if batch.item_id else None,
        "uom": batch.uom,
        "receivedQty": str(round4(D(line.received_qty))) if line else "0",
        "qcAcceptedQty": str(_accepted_qty(batch)),
    }


def source_batches(po):
    """Grey batches of a grey fabric PO that hold stock free to send."""
    from apps.inventory.models import Batch

    rows = []
    batches = Batch.objects.filter(purchase_order=po, fabric_type="GREY", deleted_at__isnull=True)
    for batch in batches.select_related("item", "goods_receipt", "goods_receipt_line").order_by("created_at"):
        available = sum((D(t.available_quantity) for t in _eligible_takas(batch)), ZERO)
        if available > ZERO:
            rows.append({**_batch_row(batch), "availableToSend": str(round4(available))})
    return rows


def grey_sources(client_id):
    """Grey fabric POs that still have stock to send for dyeing."""
    orders = PurchaseOrder.objects.filter(
        client_id=client_id, deleted_at__isnull=True, status__in=PO_RECEIVABLE_STATUSES,
    ).order_by("-doc_date", "-created_at")
    rows = []
    for po in orders:
        if not _is_grey_source(po):
            continue
        batches = source_batches(po)
        if batches:
            rows.append({
                "id": str(po.id),
                "poNumber": po.po_number,
                "vendorName": po.party_name,
                "date": po.doc_date.isoformat() if po.doc_date else None,
                "fabric": ", ".join(sorted({b["fabric"] for b in batches if b["fabric"]})),
                "availableQty": str(round4(sum((D(b["availableToSend"]) for b in batches), ZERO))),
                "batchCount": len(batches),
            })
    return rows


def set_source(po, data):
    """Tie a Flow 2 order's lines to the grey batches they send. ``dyeingBatches``
    runs in the same order as the PO's lines."""
    from apps.inventory.models import Batch

    source_id = data.get("sourcePurchaseOrderId")
    rows = data.get("dyeingBatches")
    if rows is None and po.source_purchase_order_id and not source_id:
        return
    source = PurchaseOrder.objects.filter(client_id=po.client_id, pk=source_id or None, deleted_at__isnull=True).first()
    if source is None or source.pk == po.pk or not _is_grey_source(source):
        raise ValidationFailed("Select the grey fabric PO.", field_errors={"sourcePurchaseOrderId": ["Required."]})
    lines = list(fabric_flow._live(po.line_items).order_by("line_no"))
    rows = rows or []
    if not rows or len(rows) != len(lines):
        raise ValidationFailed("Keep at least one batch to send.", field_errors={"dyeingBatches": ["Required."]})
    seen = set()
    for line, row in zip(lines, rows):
        batch = Batch.objects.filter(
            pk=row.get("batchId") or None, purchase_order=source, fabric_type="GREY", deleted_at__isnull=True,
        ).first()
        if batch is None:
            raise ValidationFailed(f"That batch is not on {source.po_number}.", field_errors={"dyeingBatches": ["Wrong batch."]})
        if batch.id in seen:
            raise ValidationFailed(f"{batch.batch_code} is listed twice.", field_errors={"dyeingBatches": ["Duplicate batch."]})
        seen.add(batch.id)
        available = sum((D(t.available_quantity) for t in _eligible_takas(batch)), ZERO)
        if D(line.qty) > available + TOLERANCE:
            raise ValidationFailed(
                f"{batch.batch_code} has only {round4(available)} available.",
                field_errors={"dyeingBatches": [f"{batch.batch_code}: at most {round4(available)}."]},
            )
        if line.item_id != batch.item_id:
            raise ValidationFailed(f"{batch.batch_code} is a different fabric.", field_errors={"dyeingBatches": ["Wrong fabric."]})
        line.source_batch = batch
        line.save(update_fields=["source_batch", "updated_at"])
    po.source_purchase_order = source
    po.save(update_fields=["source_purchase_order", "updated_at"])


def _outwards(po):
    from apps.jobwork.models import JWOOutward

    return list(
        JWOOutward.objects.filter(job_work_order__purchase_order=po, deleted_at__isnull=True)
        .select_related("job_work_order", "job_work_order__vendor").order_by("date", "created_at")
    )


def _sent_by_batch(outward):
    sent = defaultdict(lambda: ZERO)
    for line in outward.takas.filter(deleted_at__isnull=True).select_related("lot_taka"):
        sent[line.lot_taka.batch_id] += D(line.meters)
    return sent


def _returned_by_batch(outward):
    returned = defaultdict(lambda: ZERO)
    lines = GoodsReceiptLine.objects.filter(
        goods_receipt__dyeing_outward=outward, goods_receipt__deleted_at__isnull=True, deleted_at__isnull=True,
    )
    for batch_id, qty in lines.values_list("source_batch_id", "received_qty"):
        returned[batch_id] += D(qty)
    return returned


def _outward_status(sent, returned):
    if returned <= ZERO:
        return "Dispatched"
    return "Fully Returned" if returned >= sent - TOLERANCE else "Partially Returned"


def summary(po):
    """Everything the Send to Dyeing drawer and the Batch Registry show."""
    from apps.inventory.batch_services import batch_quantities
    from apps.inventory.models import Batch

    require_flow(po)
    outwards = _outwards(po)
    sent_total, returned_total = defaultdict(lambda: ZERO), defaultdict(lambda: ZERO)
    dispatch_of_batch = defaultdict(list)
    movements = []
    for outward in outwards:
        sent, returned = _sent_by_batch(outward), _returned_by_batch(outward)
        order = outward.job_work_order
        lines = []
        for batch_id, qty in sent.items():
            back = returned.get(batch_id, ZERO)
            sent_total[batch_id] += qty
            returned_total[batch_id] += back
            dispatch_of_batch[batch_id].append(outward.no)
            batch = Batch.objects.filter(pk=batch_id).first()
            lines.append({
                "batchId": str(batch_id),
                "batchCode": batch.batch_code if batch else None,
                "sentQty": str(round4(qty)),
                "returnedQty": str(round4(back)),
                "pendingQty": str(round4(max(ZERO, qty - back))),
            })
        total_sent = sum(sent.values(), ZERO)
        total_back = sum(returned.values(), ZERO)
        movements.append({
            "id": str(outward.id),
            "dispatchNumber": outward.no,
            "jobWorkOrderId": str(order.id),
            "jobWorkOrderNumber": order.jwo_no,
            "millId": str(order.vendor_id) if order.vendor_id else None,
            "millName": order.vendor_name,
            "date": outward.date.isoformat(),
            "vehicleNo": outward.vehicle_no,
            "millChallanRef": outward.challan_no,
            "remarks": outward.remarks,
            "sentQty": str(round4(total_sent)),
            "returnedQty": str(round4(total_back)),
            "pendingQty": str(round4(max(ZERO, total_sent - total_back))),
            "status": _outward_status(total_sent, total_back),
            "lines": lines,
        })

    grey = []
    plan = _source_lines(po)
    for batch in _grey_batches(po):
        sent = sent_total.get(batch.id, ZERO)
        back = returned_total.get(batch.id, ZERO)
        planned = D(plan[batch.id].qty)
        stock_free = sum((D(t.available_quantity) for t in _eligible_takas(batch)), ZERO)
        available = max(ZERO, min(stock_free, planned - sent))
        grey.append({
            **_batch_row(batch),
            "plannedQty": str(round4(planned)),
            "sentQty": str(round4(sent)),
            "returnedQty": str(round4(back)),
            "atMillQty": str(round4(max(ZERO, sent - back))),
            "availableToSend": str(round4(available)),
            "dispatchNumbers": dispatch_of_batch.get(batch.id, []),
        })

    dyed = []
    dyed_batches = Batch.objects.filter(
        purchase_order=po, deleted_at__isnull=True, goods_receipt__dyeing_outward__isnull=False,
    ).select_related("goods_receipt", "goods_receipt__dyeing_outward", "goods_receipt_line__source_batch")
    for batch in dyed_batches.order_by("created_at"):
        quantities = batch_quantities(batch)
        receipt = batch.goods_receipt
        source = batch.goods_receipt_line.source_batch if batch.goods_receipt_line_id else None
        dyed.append({
            "batchId": str(batch.id),
            "batchCode": batch.batch_code,
            "greyBatchCode": source.batch_code if source else None,
            "dispatchNumber": receipt.dyeing_outward.no,
            "grnNumber": receipt.grn_number,
            "colourName": batch.dyeing_colour_name,
            "colourHex": batch.dyeing_colour_hex,
            "returnedQty": str(round4(D(batch.goods_receipt_line.received_qty))),
            "qcStatus": receipt.qc_status,
            "availableQty": str(quantities["available"]),
        })

    received = sum((D(row["receivedQty"]) for row in grey), ZERO)
    sent = sum(sent_total.values(), ZERO)
    back = sum(returned_total.values(), ZERO)
    return {
        "poId": str(po.id),
        "poNumber": po.po_number,
        "vendorName": po.source_purchase_order.party_name if po.source_purchase_order_id else None,
        "sourcePoNumber": po.source_purchase_order.po_number if po.source_purchase_order_id else None,
        "millId": str(po.party_id),
        "millName": po.party_name,
        "colourName": po.dyeing_colour_name,
        "colourHex": po.dyeing_colour_hex,
        "fabric": ", ".join(sorted({row["fabric"] for row in grey if row["fabric"]})) or None,
        "orderedQty": str(fabric_flow.ordered_qty(po)),
        "greyReceivedQty": str(round4(received)),
        "greyAvailableQty": str(round4(sum((D(row["availableToSend"]) for row in grey), ZERO))),
        "sentQty": str(round4(sent)),
        "returnedQty": str(round4(back)),
        "atMillQty": str(round4(max(ZERO, sent - back))),
        "greyBatches": grey,
        "dispatches": movements,
        "dyedBatches": dyed,
    }


def _date(value, field):
    parsed = value if hasattr(value, "year") else parse_date(str(value or "")[:10])
    if parsed is None:
        raise ValidationFailed("Enter the date.", field_errors={field: ["Required."]})
    return parsed


def _lines(po, rows):
    """``[(batch, qty)]`` from the screen's rows: only this PO's grey batches,
    each once, with a quantity above zero. Rows left at zero are ignored."""
    batches = {str(b.id): b for b in _grey_batches(po)}
    picked = []
    seen = set()
    for row in rows or []:
        batch_id = str(row.get("batchId") or "")
        raw = row.get("qty", row.get("sendQty", row.get("returnedQty")))
        if raw in (None, "", 0, "0"):
            continue
        batch = batches.get(batch_id)
        if batch is None:
            raise ValidationFailed(f"That batch is not on {po.po_number}.", field_errors={"lines": ["Wrong batch."]})
        if batch_id in seen:
            raise ValidationFailed(f"{batch.batch_code} is listed twice.", field_errors={"lines": ["Duplicate batch."]})
        seen.add(batch_id)
        picked.append((batch, fabric_flow._qty(raw, "lines")))
    if not picked:
        raise ValidationFailed("Enter the quantity for at least one batch.", field_errors={"lines": ["Required."]})
    return picked


@transaction.atomic
def dispatch(po, data, *, user=None):
    """Send accepted grey fabric to a dyeing mill: one job work order, one
    outward challan, stock moved through the ledger."""
    from apps.core.numbering import allocate_number
    from apps.inventory import services as stock
    from apps.jobwork.models import JobWorkOrder, JWOMaterial, JWOOutward, JWOOutwardTaka

    po = fabric_flow.lock_order(po)
    require_flow(po)
    fabric_flow.require_active(po)
    mill = fabric_flow._party(po.client_id, data.get("millId"), "millId") or po.party
    on_date = _date(data.get("dispatchDate") or data.get("date"), "dispatchDate")
    lines = _lines(po, data.get("lines"))

    plan = []
    planned = _source_lines(po)
    already = defaultdict(lambda: ZERO)
    for earlier in _outwards(po):
        for batch_id, qty in _sent_by_batch(earlier).items():
            already[batch_id] += qty
    for batch, qty in lines:
        takas = _eligible_takas(batch, lock=True)
        stock_free = sum((D(t.available_quantity) for t in takas), ZERO)
        available = max(ZERO, min(stock_free, D(planned[batch.id].qty) - already[batch.id]))
        if qty > available + TOLERANCE:
            raise BusinessRuleViolation(
                f"{batch.batch_code} has only {round4(available)} available to send, not {qty}.",
                code="INSUFFICIENT_STOCK",
                payload={"batchCode": batch.batch_code, "available": str(round4(available))},
            )
        plan.append((batch, qty, takas))

    total = sum((qty for _, qty, _ in plan), ZERO)
    actor = fabric_flow._user(user)
    order = JobWorkOrder.objects.create(
        client_id=po.client_id,
        jwo_no=allocate_number(po.client, "JWO", on_date),
        purchase_order=po,
        process="Dyeing",
        vendor=mill,
        vendor_name=mill.name,
        order_date=on_date,
        planned_qty=total,
        status="In-Process",
        remarks=f"Dyeing of {po.po_number}",
        created_by=actor,
    )
    outward = JWOOutward.objects.create(
        client_id=po.client_id,
        job_work_order=order,
        no=allocate_number(po.client, "JWOUT", on_date),
        date=on_date,
        qty=total,
        challan_no=fabric_flow._text(data, "millChallanRef", "challanNo"),
        vehicle_no=fabric_flow._text(data, "vehicleNo", "vehicleNumber"),
        remarks=fabric_flow._text(data, "remarks"),
        status="Sent",
        created_by=actor,
    )
    wip = stock.wip_location_for_party(po.client_id, mill)
    for batch, qty, takas in plan:
        JWOMaterial.objects.create(
            client_id=po.client_id, job_work_order=order,
            fabric_item=batch.item.name if batch.item_id else "", lot_no=batch.batch_code, qty=qty,
        )
        left = qty
        for taka in takas:
            take = min(left, D(taka.available_quantity))
            if take <= ZERO:
                break
            holder, _ = stock.relocate_lot(
                client_id=po.client_id, lot=taka, quantity=take, to_location_id=wip.id,
                out_type="JOBWORK_OUTWARD", in_type="JOBWORK_OUTWARD",
                reference_type="JobWorkOrder", reference_id=order.id, reference_number=outward.no,
                movement_date=on_date, user=user,
            )
            holder.status = "External WIP"
            holder.save(update_fields=["status", "updated_at"])
            JWOOutwardTaka.objects.create(client_id=po.client_id, outward=outward, lot_taka=holder, meters=take)
            left -= take
    fabric_flow._audit(po, user, "dyeing_dispatch", f"{total} sent to {mill.name} on {outward.no}.")
    return outward


def dispatch_planned(po, *, user=None):
    """Send what the order lists. Runs once: an order that already went out
    keeps its dispatch."""
    if po.fabric_flow != FABRIC_FLOW_OSCAR_TO_MILL or _outwards(po):
        return None
    planned = _source_lines(po)
    return dispatch(po, {
        "dispatchDate": timezone.localdate(),
        "lines": [{"batchId": str(batch_id), "qty": line.qty} for batch_id, line in planned.items()],
    }, user=user)


@transaction.atomic
def receive_return(po, outward_id, data, *, user=None):
    """Dyed fabric back from the mill: a GRN that opens one Dyed Fabric batch
    per grey batch, at QC Hold, and takes the same metres off the mill."""
    from apps.core.numbering import allocate_number
    from apps.inventory import services as stock
    from apps.inventory.models import Batch, LotTaka
    from apps.jobwork.models import JWOInward, JWOOutward
    from apps.masters.models import Shade

    from . import services

    po = fabric_flow.lock_order(po)
    require_flow(po)
    outward = (
        JWOOutward.objects.select_for_update(of=("self",))
        .filter(pk=outward_id, job_work_order__purchase_order=po, deleted_at__isnull=True)
        .select_related("job_work_order", "job_work_order__vendor").first()
    )
    if outward is None:
        raise ValidationFailed("That dyeing dispatch is not on this PO.", field_errors={"dispatchId": ["Not found."]})
    order = outward.job_work_order
    on_date = _date(data.get("returnDate") or data.get("date"), "returnDate")
    colour = fabric_flow._text(data, "colourName", "shade")
    if not colour:
        raise ValidationFailed("Enter the actual colour.", field_errors={"colourName": ["Required."]})
    colour_hex = (fabric_flow._text(data, "colourHex") or "").upper() or None
    if colour_hex and not fabric_flow.HEX_COLOUR.fullmatch(colour_hex):
        raise ValidationFailed("The colour code must look like #1F3A5F.", field_errors={"colourHex": ["Invalid colour."]})

    lines = _lines(po, data.get("lines"))
    sent, returned = _sent_by_batch(outward), _returned_by_batch(outward)
    planned = _source_lines(po)
    payload = []
    for batch, qty in lines:
        pending = sent.get(batch.id, ZERO) - returned.get(batch.id, ZERO)
        if batch.id not in sent:
            raise ValidationFailed(f"{batch.batch_code} was not sent on {outward.no}.", field_errors={"lines": ["Wrong batch."]})
        if qty > pending + TOLERANCE:
            raise BusinessRuleViolation(
                f"{batch.batch_code}: only {round4(pending)} is still at the mill on {outward.no}, not {qty}.",
                code="OVER_RETURN",
                payload={"batchCode": batch.batch_code, "pending": str(round4(pending))},
            )
        grey_line = batch.goods_receipt_line
        payload.append({
            "itemId": batch.item_id,
            "poLineId": planned[batch.id].id,
            "receivedQty": qty,
            "unitCost": D(grey_line.unit_cost) if grey_line else ZERO,
            "uom": grey_line.uom if grey_line else batch.uom,
            "remarks": fabric_flow._text(data, "processingDetails", "remarks"),
            "sourceBatchId": batch.id,
        })
    receipt = services.create_standalone_grn(
        po.client,
        party_id=order.vendor_id,
        location_id=data.get("locationId"),
        receipt_date=on_date,
        supplier_challan_no=fabric_flow._text(data, "millChallanNo", "challanNo"),
        purchase_order_id=po.id,
        lines_payload=payload,
        user=user,
        dyeing_outward=outward,
    )

    inward = JWOInward.objects.create(
        client_id=po.client_id, job_work_order=order, no=allocate_number(po.client, "JWIN", on_date),
        date=on_date, qty=sum((qty for _, qty in lines), ZERO), challan_no=receipt.supplier_challan_no,
        created_by=fabric_flow._user(user),
    )
    shade = Shade.objects.filter(client_id=po.client_id, name__iexact=colour, deleted_at__isnull=True).first()
    wip = stock.wip_location_for_party(po.client_id, order.vendor)
    grn_lines = {
        line.source_batch_id: line
        for line in receipt.lines.filter(deleted_at__isnull=True)
    }
    for grey, qty in lines:
        dyed = Batch.objects.get(goods_receipt_line=grn_lines[grey.id], deleted_at__isnull=True)
        dyed.dyeing_colour_name = colour
        dyed.dyeing_colour_hex = colour_hex
        dyed.save(update_fields=["dyeing_colour_name", "dyeing_colour_hex", "updated_at"])
        dyed.parent_batches.add(grey)
        if shade is not None:
            LotTaka.objects.filter(batch=dyed).update(shade=shade)

        left = qty
        holders = outward.takas.filter(deleted_at__isnull=True, lot_taka__batch=grey).select_related("lot_taka__item")
        for line in holders.order_by("created_at"):
            taka = LotTaka.objects.select_for_update().get(pk=line.lot_taka_id)
            take = min(left, D(taka.available_quantity))
            if take <= ZERO:
                continue
            stock.post_movement(
                client_id=po.client_id, item=taka.item, location=wip, type="JOBWORK_INWARD", quantity=-take,
                lot_taka=taka, reference_type="JobWorkOrder", reference_id=order.id,
                reference_number=inward.no, movement_date=on_date, user=user,
            )
            left -= take

    all_sent = sum(_sent_by_batch(outward).values(), ZERO)
    all_back = sum(_returned_by_batch(outward).values(), ZERO)
    order.status = "Received" if all_back >= all_sent - TOLERANCE else "Partially Received"
    order.save(update_fields=["status", "updated_at"])
    # The fabric's value moves from the grey batch to the dyed one.
    fabric_flow.allocate_costs(po, user=user)
    if po.source_purchase_order and po.source_purchase_order.fabric_flow:
        fabric_flow.allocate_costs(po.source_purchase_order, user=user)
    fabric_flow._audit(po, user, "dyeing_return", f"{inward.qty} back from {order.vendor_name} on {receipt.grn_number}.")
    return GoodsReceipt.objects.get(pk=receipt.pk)
