"""
Batch Profitability Service (OSCAR ERP)
Calculates TRUE realized profit and loss on a per-batch basis.

Realized Profit/Loss =
    Allocated Sales Revenue
    - Cost of Sold Quantity (base fabric purchase / GRN landed cost)
    - Allocated Job Work / Dyeing / Printing Cost
    - Allocated Transport / Freight Cost
    - Other Direct Batch Expenses
    - Applicable Returns / Claims / Adjustments

Unsold stock is strictly preserved as "Not Yet Realized" inventory and
is NEVER counted as revenue or realized profit.
"""
from collections import defaultdict
from decimal import Decimal
from django.db.models import Q
from apps.inventory.models import Batch, BatchLoss, LotTaka, LotCostEntry, StockMovement
from apps.inventory.batch_services import batch_costing, batch_quantities, _date, _sale_rates
from apps.sales.models import DeliveryChallan, SalesInvoice, SalesOrder, DeliveryChallanLine, SalesInvoiceLine

ZERO = Decimal("0")
D = Decimal


def round2(val):
    if val is None:
        return ZERO
    return Decimal(str(val)).quantize(Decimal("0.01"))


def round4(val):
    if val is None:
        return ZERO
    return Decimal(str(val)).quantize(Decimal("0.0001"))


def get_batch_profitability_summary(client_id, day=None):
    """
    Computes summary metrics, top 5 profitable batches, and top 5 loss-making batches.
    Used directly by the Dashboard's compact 'Batch-wise Profitability' section.
    """
    batches = list(
        Batch.objects.filter(client_id=client_id, deleted_at__isnull=True)
        .select_related("item", "purchase_order", "goods_receipt")
        .order_by("-created_at")
    )

    # Bulk preloads: a handful of queries for every batch instead of one
    # round trip per batch per relation.
    batch_ids = [b.id for b in batches]
    takas_by_batch = defaultdict(list)
    if batch_ids:
        for t in LotTaka.objects.filter(
            batch_id__in=batch_ids, deleted_at__isnull=True
        ).select_related(
            "item", "item__category", "location", "fabric_quality", "design", "shade", "supplier"
        ).order_by("created_at"):
            takas_by_batch[t.batch_id].append(t)

    all_takas = [t for ts in takas_by_batch.values() for t in ts]
    taka_ids = [t.id for t in all_takas]
    movements_by_taka = defaultdict(list)
    if taka_ids:
        for m in StockMovement.objects.filter(
            lot_taka_id__in=taka_ids, deleted_at__isnull=True
        ):
            movements_by_taka[m.lot_taka_id].append(m)

    lot_numbers = {t.lot_number for t in all_takas if t.lot_number}
    entries_by_lot = defaultdict(list)
    if lot_numbers:
        for e in LotCostEntry.objects.filter(
            client_id=client_id, lot_number__in=lot_numbers, deleted_at__isnull=True
        ).order_by("date", "created_at"):
            entries_by_lot[e.lot_number].append(e)

    losses_by_batch = defaultdict(list)
    if batch_ids:
        for l in BatchLoss.objects.filter(
            batch_id__in=batch_ids, deleted_at__isnull=True
        ).select_related("responsible_party"):
            losses_by_batch[l.batch_id].append(l)

    profitable_rows = []
    loss_rows = []
    unsold_count = 0
    total_unsold_val = ZERO
    total_realized_profit = ZERO
    total_realized_loss = ZERO

    for b in batches:
        takas = takas_by_batch.get(b.id, [])
        movements = [m for t in takas for m in movements_by_taka.get(t.id, ())]
        lots = {t.lot_number for t in takas}
        cost_entries = [e for lot in lots for e in entries_by_lot.get(lot, ())]
        losses = losses_by_batch.get(b.id, [])
        c = batch_costing(
            b, takas=takas, movements=movements,
            cost_entries=cost_entries, losses=losses,
        )
        sold_qty = D(c.get("soldQty", "0"))
        rev = D(c.get("revenue", "0"))
        cogs = D(c.get("costOfSold", "0"))
        net = D(c.get("netResult", "0"))
        unsold_val = D(c.get("unsoldInventoryValue", "0"))
        total_cost = D(c.get("totalCost", "0"))
        cost_per_unit = D(c.get("costPerUnit", "0"))
        qty = batch_quantities(b, takas, movements=movements)
        rem_qty = D(qty.get("onHand", "0"))

        is_sold = sold_qty > ZERO

        row = {
            "id": str(b.id),
            "batchCode": b.batch_code,
            "fabricName": b.item.name if b.item else "—",
            "fabricType": (b.fabric_type or "Fabric").capitalize(),
            "stage": (b.fabric_type or "Fabric").capitalize(),
            "origin": b.origin or "—",
            "createdOn": _date(b.created_on) if b.created_on else None,
            "soldQty": str(round4(sold_qty)),
            "remainingQty": str(round4(rem_qty)),
            "remainingValue": str(round2(unsold_val)),
            "costPerMeter": str(round4(cost_per_unit)),
            "totalAllocatedCost": str(round2(total_cost)),
            "salesRevenue": str(round2(rev)),
            "costOfSold": str(round2(cogs)),
            "netResult": str(round2(net)) if is_sold else None,
            "marginPct": str(round2(c["marginPct"])) if (is_sold and c.get("marginPct") is not None) else None,
            "isSold": is_sold,
            "status": "Profitable" if (is_sold and net > ZERO) else ("Loss-making" if (is_sold and net < ZERO) else ("Break-even" if is_sold else "Not Yet Realized")),
        }

        if is_sold:
            if net > ZERO:
                total_realized_profit += net
                profitable_rows.append(row)
            elif net < ZERO:
                total_realized_loss += abs(net)
                loss_rows.append(row)
        else:
            unsold_count += 1
            total_unsold_val += unsold_val

    # Sort profitable highest first
    profitable_rows.sort(key=lambda r: D(r["netResult"]), reverse=True)
    # Sort loss-making biggest loss first (most negative)
    loss_rows.sort(key=lambda r: D(r["netResult"]))

    return {
        "totalRealizedProfit": str(round2(total_realized_profit)),
        "totalRealizedLoss": str(round2(total_realized_loss)),
        "netRealizedContribution": str(round2(total_realized_profit - total_realized_loss)),
        "profitableCount": len(profitable_rows),
        "lossMakingCount": len(loss_rows),
        "unsoldBatchesCount": unsold_count,
        "totalBatches": len(batches),
        "totalUnsoldValue": str(round2(total_unsold_val)),
        "profitable": profitable_rows[:5],
        "lossMaking": loss_rows[:5],
        # Backward compatibility
        "batches": len(profitable_rows) + len(loss_rows),
        "worst": loss_rows[:5],
        "best": profitable_rows[:5],
        "net": str(round2(total_realized_profit - total_realized_loss)),
    }


def get_batch_profitability_list(client_id, *, search=None, fabric_type=None, status=None, sort_by=None, date_from=None, date_to=None):
    """
    Returns full filtered and sorted list of batch profitability records for the dedicated page.
    """
    qs = (
        Batch.objects.filter(client_id=client_id, deleted_at__isnull=True)
        .select_related("item", "purchase_order", "goods_receipt", "supplier")
        .order_by("-created_at")
    )

    if fabric_type and fabric_type.upper() != "ALL":
        qs = qs.filter(fabric_type__iexact=fabric_type)

    if date_from:
        qs = qs.filter(created_on__gte=date_from)
    if date_to:
        qs = qs.filter(created_on__lte=date_to)

    raw_rows = []
    total_realized_profit = ZERO
    total_realized_loss = ZERO
    total_sold_meters = ZERO
    total_revenue = ZERO
    total_unsold_val = ZERO
    profitable_count = 0
    loss_count = 0
    unsold_count = 0

    for b in qs:
        # Search filter
        if search:
            q_str = str(search).strip().lower()
            code_match = q_str in (b.batch_code or "").lower()
            item_match = b.item and (q_str in b.item.name.lower())
            if not (code_match or item_match):
                continue

        c = batch_costing(b)
        sold_qty = D(c.get("soldQty", "0"))
        rev = D(c.get("revenue", "0"))
        cogs = D(c.get("costOfSold", "0"))
        net = D(c.get("netResult", "0"))
        unsold_val = D(c.get("unsoldInventoryValue", "0"))
        total_cost = D(c.get("totalCost", "0"))
        cost_per_unit = D(c.get("costPerUnit", "0"))
        qty = batch_quantities(b)
        rem_qty = D(qty.get("onHand", "0"))

        is_sold = sold_qty > ZERO

        if is_sold:
            total_sold_meters += sold_qty
            total_revenue += rev
            if net > ZERO:
                b_status = "PROFITABLE"
                profitable_count += 1
                total_realized_profit += net
            elif net < ZERO:
                b_status = "LOSS_MAKING"
                loss_count += 1
                total_realized_loss += abs(net)
            else:
                b_status = "BREAK_EVEN"
        else:
            b_status = "UNSOLD"
            unsold_count += 1
            total_unsold_val += unsold_val

        # Status filter
        if status and status.upper() != "ALL":
            if status.upper() == "PROFITABLE" and b_status != "PROFITABLE":
                continue
            if status.upper() == "LOSS_MAKING" and b_status != "LOSS_MAKING":
                continue
            if status.upper() == "UNSOLD" and b_status != "UNSOLD":
                continue

        raw_rows.append({
            "id": str(b.id),
            "batchCode": b.batch_code,
            "fabricName": b.item.name if b.item else "—",
            "fabricType": b.fabric_type or "—",
            "stage": (b.fabric_type or "Fabric").capitalize(),
            "origin": b.origin or "—",
            "supplier": b.supplier.name if b.supplier else (b.goods_receipt.party.name if b.goods_receipt and b.goods_receipt.party else "—"),
            "createdOn": _date(b.created_on) if b.created_on else None,
            "receivedQty": str(round4(D(qty.get("received", "0")))),
            "soldQty": str(round4(sold_qty)),
            "remainingQty": str(round4(rem_qty)),
            "remainingValue": str(round2(unsold_val)),
            "costPerMeter": str(round4(cost_per_unit)),
            "totalAllocatedCost": str(round2(total_cost)),
            "costByType": c.get("costByType", {}),
            "salesRevenue": str(round2(rev)),
            "costOfSold": str(round2(cogs)),
            "writtenOffLoss": str(round2(D(c.get("writtenOffLoss", "0")))),
            "netResult": str(round2(net)) if is_sold else None,
            "marginPct": str(round2(c["marginPct"])) if (is_sold and c.get("marginPct") is not None) else None,
            "isSold": is_sold,
            "status": b_status,
            "statusLabel": "Profitable" if b_status == "PROFITABLE" else ("Loss-making" if b_status == "LOSS_MAKING" else ("Break-even" if b_status == "BREAK_EVEN" else "Not Yet Realized")),
            "purchaseOrder": b.purchase_order.po_number if b.purchase_order else None,
            "goodsReceipt": b.goods_receipt.grn_number if b.goods_receipt else None,
        })

    # Sort
    if sort_by == "highest_profit":
        raw_rows.sort(key=lambda r: D(r["netResult"] or "-999999999"), reverse=True)
    elif sort_by == "highest_loss":
        raw_rows.sort(key=lambda r: D(r["netResult"] or "999999999"))
    elif sort_by == "highest_revenue":
        raw_rows.sort(key=lambda r: D(r["salesRevenue"]), reverse=True)
    elif sort_by == "highest_stock_value":
        raw_rows.sort(key=lambda r: D(r["remainingValue"]), reverse=True)
    elif sort_by == "batch_code":
        raw_rows.sort(key=lambda r: r["batchCode"])

    summary = {
        "totalBatches": len(raw_rows),
        "totalRealizedProfit": str(round2(total_realized_profit)),
        "totalRealizedLoss": str(round2(total_realized_loss)),
        "netRealizedContribution": str(round2(total_realized_profit - total_realized_loss)),
        "profitableCount": profitable_count,
        "lossMakingCount": loss_count,
        "unsoldBatchesCount": unsold_count,
        "totalSoldMeters": str(round4(total_sold_meters)),
        "totalRealizedRevenue": str(round2(total_revenue)),
        "totalUnsoldValue": str(round2(total_unsold_val)),
    }

    return {
        "summary": summary,
        "rows": raw_rows,
    }


def get_batch_profitability_detail(client_id, batch_code_or_id):
    """
    Returns full audit breakdown for a single batch modal/slideout.
    """
    import uuid
    is_uuid = False
    try:
        uuid.UUID(str(batch_code_or_id))
        is_uuid = True
    except (ValueError, AttributeError):
        is_uuid = False

    filter_q = Q(id=batch_code_or_id) if is_uuid else Q(batch_code=batch_code_or_id)
    batch = Batch.objects.filter(
        client_id=client_id, deleted_at__isnull=True
    ).filter(filter_q).select_related(
        "item", "purchase_order", "goods_receipt", "supplier"
    ).first()

    if not batch:
        return None

    c = batch_costing(batch)
    takas = list(batch.takas.filter(deleted_at__isnull=True).select_related("location"))
    qty = batch_quantities(batch, takas)
    lots = sorted({t.lot_number for t in takas})

    # Direct cost entries
    entries = (
        LotCostEntry.objects.filter(
            client_id=client_id, lot_number__in=lots, deleted_at__isnull=True
        )
        .order_by("date", "created_at")
    )
    cost_rows = []
    for e in entries:
        cost_rows.append({
            "id": str(e.id),
            "date": _date(e.date),
            "costType": e.cost_type,
            "costTypeLabel": e.get_cost_type_display() if hasattr(e, "get_cost_type_display") else e.cost_type,
            "amount": str(round2(D(e.amount))),
            "rate": str(round4(D(e.cost_per_meter))) if (hasattr(e, "cost_per_meter") and e.cost_per_meter) else None,
            "quantity": str(round4(D(e.quantity))) if e.quantity else None,
            "isApproved": e.is_approved,
            "sourceDocumentType": e.source_document_type,
            "sourceDocumentNumber": e.source_document_number,
            "remarks": e.remarks,
        })

    # Linked sales documents
    sale_moves = list(
        StockMovement.objects.filter(lot_taka__in=takas, type="SALE", deleted_at__isnull=True)
        .order_by("movement_date", "created_at")
    )
    rates = _sale_rates(sale_moves)
    sales_docs = []
    for sm in sale_moves:
        rate = rates.get(sm.id)
        dispatched = abs(D(sm.quantity))
        line_rev = round2(dispatched * D(rate)) if rate is not None else ZERO
        sales_docs.append({
            "date": _date(sm.movement_date),
            "referenceType": sm.reference_type,
            "referenceNumber": sm.reference_number,
            "quantity": str(round4(dispatched)),
            "sellingRate": str(round4(D(rate))) if rate is not None else "Unpriced",
            "allocatedRevenue": str(line_rev),
        })

    sold_qty = D(c.get("soldQty", "0"))
    rev = D(c.get("revenue", "0"))
    cogs = D(c.get("costOfSold", "0"))
    net = D(c.get("netResult", "0"))
    is_sold = sold_qty > ZERO

    return {
        "id": str(batch.id),
        "batchCode": batch.batch_code,
        "fabricName": batch.item.name if batch.item else "—",
        "fabricType": (batch.fabric_type or "Fabric").capitalize(),
        "origin": batch.origin or "—",
        "uom": batch.uom or "MTR",
        "createdOn": _date(batch.created_on) if batch.created_on else None,
        "supplier": batch.supplier.name if batch.supplier else (batch.goods_receipt.party.name if batch.goods_receipt and batch.goods_receipt.party else "—"),
        "purchaseOrder": batch.purchase_order.po_number if batch.purchase_order else None,
        "goodsReceipt": batch.goods_receipt.grn_number if batch.goods_receipt else None,
        "receiptDate": _date(batch.goods_receipt.receipt_date) if batch.goods_receipt and batch.goods_receipt.receipt_date else None,
        "totalTakas": len(takas),
        "receivedQty": str(round4(D(qty.get("received", "0")))),
        "availableQty": str(round4(D(qty.get("onHand", "0")))),
        "qcHoldQty": str(round4(D(qty.get("qcHold", "0")))),
        "reworkQty": str(round4(D(qty.get("rework", "0")))),
        "rejectedQty": str(round4(D(qty.get("rejected", "0")))),
        "soldQty": str(round4(sold_qty)),
        "costPerMeter": str(round4(D(c.get("costPerUnit", "0")))),
        "totalAllocatedCost": str(round2(D(c.get("totalCost", "0")))),
        "costByType": c.get("costByType", {}),
        "costEntries": cost_rows,
        "salesDocuments": sales_docs,
        "salesRevenue": str(round2(rev)),
        "costOfSold": str(round2(cogs)),
        "writtenOffLoss": str(round2(D(c.get("writtenOffLoss", "0")))),
        "remainingInventoryValue": str(round2(D(c.get("unsoldInventoryValue", "0")))),
        "netResult": str(round2(net)) if is_sold else None,
        "marginPct": str(round2(c["marginPct"])) if (is_sold and c.get("marginPct") is not None) else None,
        "isSold": is_sold,
        "status": "PROFITABLE" if (is_sold and net > ZERO) else ("LOSS_MAKING" if (is_sold and net < ZERO) else ("BREAK_EVEN" if is_sold else "UNSOLD")),
        "statusLabel": "Profitable" if (is_sold and net > ZERO) else ("Loss-making" if (is_sold and net < ZERO) else ("Break-even" if is_sold else "Not Yet Realized")),
    }
