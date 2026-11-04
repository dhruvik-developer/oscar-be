"""Batch Registry grouping backed by purchase, receipt and stock records."""
from collections import defaultdict
from decimal import Decimal

from django.db.models import Prefetch, Q

from apps.purchase.fabric_flow import quantities as po_quantities
from apps.purchase.models import GoodsReceipt, PurchaseBill, PurchaseOrder

from . import batch_services
from .models import Batch, LotTaka


def _linked_purchase_order_id(batch, purchase_order_ids, receipt_po_ids, bill_po_ids):
    if batch.purchase_order_id:
        return str(batch.purchase_order_id)
    if batch.purchase_order_line_id:
        return str(batch.purchase_order_line.purchase_order_id)
    source_type = (batch.source_document_type or "").casefold()
    source_id = str(batch.source_document_id or "")
    if source_type == "purchaseorder" and source_id in purchase_order_ids:
        return source_id
    if batch.goods_receipt_id:
        return receipt_po_ids.get(str(batch.goods_receipt_id))
    if source_type in ("grn", "goodsreceipt"):
        return receipt_po_ids.get(source_id)
    if source_type == "purchasebill":
        return bill_po_ids.get(source_id)
    return None


def grouped_batch_registry(client_id, params, *, with_cost=False):
    group_by = params.get("groupBy") or "po"
    if group_by not in ("po", "fabric"):
        raise ValueError("groupBy must be 'po' or 'fabric'.")

    purchase_orders = list(
        PurchaseOrder.objects.filter(client_id=client_id, deleted_at__isnull=True)
        .select_related("party")
        .prefetch_related("line_items")
        .order_by("po_number")
    )
    po_by_id = {str(po.id): po for po in purchase_orders}
    po_ids_by_number = {str(po.po_number).casefold(): str(po.id) for po in purchase_orders if po.po_number}
    purchase_order_options = [
        {"id": str(po.id), "poNumber": po.po_number or str(po.id)}
        for po in purchase_orders
    ]
    if params.get("optionsOnly") == "1":
        return {
            "groups": [],
            "batchCount": 0,
            "summary": batch_services.stock_totals(client_id, [], eligible_only=True),
            "purchaseOrders": purchase_order_options,
        }
    bill_po_ids = {
        str(bill_id): str(po_id)
        for bill_id, po_id in PurchaseBill.objects.filter(
            client_id=client_id,
            deleted_at__isnull=True,
            purchase_order__isnull=False,
        ).values_list("id", "purchase_order_id")
    }
    receipts = list(
        GoodsReceipt.objects.filter(client_id=client_id, deleted_at__isnull=True)
        .values("id", "purchase_order_id", "purchase_bill_id", "grn_number", "receipt_date", "qc_status")
    )
    grn_by_id = {str(row["id"]): row for row in receipts}
    receipt_po_ids = {
        str(row["id"]): str(row["purchase_order_id"] or bill_po_ids.get(str(row["purchase_bill_id"])))
        for row in receipts if row["purchase_order_id"] or bill_po_ids.get(str(row["purchase_bill_id"]))
    }

    eligible_takas = batch_services.registry_eligible_takas(client_id).select_related(
        "item", "item__category", "location", "fabric_quality", "design", "shade", "supplier"
    ).order_by("created_at")
    all_takas = LotTaka.objects.filter(client_id=client_id, deleted_at__isnull=True).select_related(
        "item", "item__category", "location", "fabric_quality", "design", "shade", "supplier"
    ).order_by("created_at")
    include_pending_qc = str(params.get("includePendingQc", "")).lower() in ("1", "true")
    group_takas = all_takas if include_pending_qc else eligible_takas
    base_batch_filter = (
        (
            Q(pk__in=group_takas.values("batch_id"))
            | Q(goods_receipt__isnull=False)
            | Q(purchase_order__isnull=False)
            | Q(purchase_order_line__isnull=False)
        )
        if include_pending_qc
        else Q(pk__in=group_takas.values("batch_id"))
    )
    qs = Batch.objects.filter(
        client_id=client_id,
        deleted_at__isnull=True,
    ).filter(base_batch_filter).select_related(
        "item", "supplier", "owner_party", "purchase_order", "purchase_order_line", "goods_receipt"
    ).prefetch_related(
        Prefetch("takas", queryset=eligible_takas, to_attr="registry_eligible_takas"),
        Prefetch("takas", queryset=all_takas, to_attr="registry_all_takas"),
    )
    q = (params.get("q") or "").strip()
    if q:
        matching_po_ids = [
            str(po.id) for po in purchase_orders
            if q.casefold() in str(po.po_number or "").casefold()
        ]
        matching_receipt_ids = [
            receipt_id for receipt_id, po_id in receipt_po_ids.items()
            if po_id in matching_po_ids
        ]
        matching_bill_ids = [
            bill_id for bill_id, po_id in bill_po_ids.items()
            if po_id in matching_po_ids
        ]
        qs = qs.filter(
            Q(batch_code__icontains=q)
            | Q(source_document_number__icontains=q)
            | Q(item__name__icontains=q)
            | Q(supplier__name__icontains=q)
            | Q(purchase_order__po_number__icontains=q)
            | Q(purchase_order_line__purchase_order__po_number__icontains=q)
            | Q(goods_receipt__grn_number__icontains=q)
            | Q(takas__lot_number__icontains=q)
            | Q(takas__taka_id__icontains=q)
            | Q(takas__source_document_number__icontains=q)
            | Q(source_document_type__iexact="PurchaseOrder", source_document_id__in=matching_po_ids)
            | Q(purchase_order_line__purchase_order_id__in=matching_po_ids)
            | Q(goods_receipt_id__in=matching_receipt_ids)
            | Q(source_document_type__iexact="GRN", source_document_id__in=matching_receipt_ids)
            | Q(source_document_type__iexact="GoodsReceipt", source_document_id__in=matching_receipt_ids)
            | Q(source_document_type__iexact="PurchaseBill", source_document_id__in=matching_bill_ids)
        ).distinct()
    for param, field in (
        ("origin", "origin"),
        ("ownership", "ownership"),
        ("fabricType", "fabric_type"),
        ("supplierId", "supplier_id"),
        ("itemId", "item_id"),
    ):
        if params.get(param):
            qs = qs.filter(**{field: params[param]})
    if params.get("from"):
        qs = qs.filter(created_on__gte=params["from"])
    if params.get("to"):
        qs = qs.filter(created_on__lte=params["to"])
    if params.get("qcStatus"):
        qs = qs.filter(
            pk__in=group_takas.filter(qc_status=params["qcStatus"]).values("batch_id")
        )

    roll = batch_services.roll_filter(params)
    if roll is not None:
        qs = qs.filter(
            pk__in=group_takas.filter(roll).values("batch_id")
        )

    po_filter = params.get("poId") or params.get("purchaseOrderId") or params.get("po") or params.get("poNumber")
    selected_po_id = None
    if po_filter:
        selected_po_id = po_filter if po_filter in po_by_id else po_ids_by_number.get(po_filter.casefold())
        if selected_po_id:
            receipt_ids = [receipt_id for receipt_id, po_id in receipt_po_ids.items() if po_id == selected_po_id]
            bill_ids = [bill_id for bill_id, po_id in bill_po_ids.items() if po_id == selected_po_id]
            qs = qs.filter(
                Q(purchase_order_id=selected_po_id)
                | Q(purchase_order_line__purchase_order_id=selected_po_id)
                | Q(source_document_type__iexact="PurchaseOrder", source_document_id=selected_po_id)
                | Q(goods_receipt__purchase_order_id=selected_po_id)
                | Q(goods_receipt_id__in=receipt_ids)
                | Q(source_document_type__iexact="GRN", source_document_id__in=receipt_ids)
                | Q(source_document_type__iexact="GoodsReceipt", source_document_id__in=receipt_ids)
                | Q(source_document_type__iexact="PurchaseBill", source_document_id__in=bill_ids)
            ).distinct()
        else:
            qs = qs.none()

    groups_by_key = {}
    batch_ids = []
    batch_count = 0
    wanted_status = params.get("status")
    for batch in qs.order_by("-created_on", "-created_at"):
        po_id = _linked_purchase_order_id(batch, po_by_id, receipt_po_ids, bill_po_ids)
        takas = batch.registry_eligible_takas
        all_batch_takas = batch.registry_all_takas
        row = batch_services.serialize_batch(batch, takas=takas, include_cost=with_cost)
        if wanted_status and row["status"] != wanted_status:
            continue

        row["qcStatus"] = "Not Inspected" if include_pending_qc else "Passed"
        receipt_ids = {
            str(taka.source_document_id)
            for taka in (all_batch_takas if include_pending_qc else takas)
            if taka.source_document_type in ("GRN", "GoodsReceipt") and taka.source_document_id
        }
        if batch.goods_receipt_id:
            receipt_ids.add(str(batch.goods_receipt_id))
        pending_receipt_ids = []
        receipt_statuses = set()
        if include_pending_qc:
            receipt_statuses = {
                grn_by_id[receipt_id]["qc_status"]
                for receipt_id in receipt_ids
                if receipt_id in grn_by_id
            }
            pending_receipt_ids = sorted(
                receipt_id for receipt_id in receipt_ids
                if receipt_id in grn_by_id
                and grn_by_id[receipt_id]["qc_status"] in ("Pending", "Draft", "On Hold")
            )
            if pending_receipt_ids:
                row["qcStatus"] = "Pending"
                row["pendingQcReceiptId"] = pending_receipt_ids[0]
            elif "Rejected" in receipt_statuses:
                row["qcStatus"] = "Rejected"
            elif "Rework" in receipt_statuses:
                row["qcStatus"] = "Rework"
            elif "Partially Approved" in receipt_statuses:
                row["qcStatus"] = "Partially Approved"
            elif receipt_statuses and receipt_statuses <= {"Approved"}:
                row["qcStatus"] = "Passed"
            elif receipt_statuses:
                row["qcStatus"] = sorted(receipt_statuses)[0]
        display_takas = all_batch_takas if include_pending_qc else takas
        row["receiptNumbers"] = sorted({
            grn_by_id[receipt_id]["grn_number"]
            for receipt_id in receipt_ids
            if receipt_id in grn_by_id and grn_by_id[receipt_id]["grn_number"]
        } | {
            str(taka.source_document_number)
            for taka in display_takas
            if taka.source_document_type in ("GRN", "GoodsReceipt") and taka.source_document_number
        })
        row["receiptDates"] = sorted({
            grn_by_id[receipt_id]["receipt_date"].isoformat()
            for receipt_id in receipt_ids
            if receipt_id in grn_by_id and grn_by_id[receipt_id]["receipt_date"]
        } | {
            taka.received_date.isoformat()
            for taka in display_takas
            if taka.received_date
        })
        row["locationNames"] = sorted({
            taka.location.name
            for taka in display_takas
            if taka.location_id and taka.location and taka.location.name
        })
        child_received = defaultdict(Decimal)
        all_taka_ids = {taka.id for taka in all_batch_takas}
        for taka in all_batch_takas:
            if taka.parent_taka_id in all_taka_ids:
                child_received[taka.parent_taka_id] += Decimal(str(taka.received_quantity or 0))
        accepted_qty = Decimal("0")
        rejected_qty = Decimal("0")
        reprocess_qty = Decimal("0")
        current_locations = set()
        for taka in (all_batch_takas if include_pending_qc else takas):
            effective_qty = max(
                Decimal("0"),
                Decimal(str(taka.received_quantity or 0)) - child_received[taka.id],
            )
            if effective_qty > Decimal("0") and taka.location and taka.location.name:
                current_locations.add(taka.location.name)
            qc_status = (taka.qc_status or "").strip().casefold()
            loc_name = (taka.location.name if taka.location else "").lower()
            if "reprocess" in loc_name or qc_status in ("reprocess", "rework"):
                if effective_qty > Decimal("0"):
                    reprocess_qty += effective_qty
            if qc_status in ("passed", "pass", "approved"):
                accepted_qty += effective_qty
            elif qc_status in ("rejected", "reject"):
                rejected_qty += effective_qty

        if current_locations:
            row["locationNames"] = sorted(current_locations)
        row["reprocessQty"] = str(reprocess_qty) if reprocess_qty > Decimal("0") else "0"

        # If reprocess fabric remains, mark Partially Approved; otherwise if QC disposition completed, mark Passed
        # Determine final QC Status and Reprocess Status:
        # If there are active receipts still pending QC:
        if pending_receipt_ids:
            row["qcStatus"] = "Pending"
            row["pendingQcReceiptId"] = pending_receipt_ids[0]
            row["reprocessStatus"] = "In Reprocess" if reprocess_qty > Decimal("0") else "None"
        elif reprocess_qty > Decimal("0"):
            row["qcStatus"] = "In Reprocess"
            row["reprocessStatus"] = "In Reprocess"
        elif accepted_qty > Decimal("0"):
            row["qcStatus"] = "Passed"
            row["reprocessStatus"] = "None"
        elif rejected_qty > Decimal("0") and accepted_qty == Decimal("0"):
            row["qcStatus"] = "Rejected"
            row["reprocessStatus"] = "None"
        elif "Approved" in receipt_statuses or row.get("qcStatus") in ("Approved", "Partially Approved"):
            row["qcStatus"] = "Passed"
            row["reprocessStatus"] = "None"
        row["acceptedQty"] = str(accepted_qty)
        row["qcRejectedQty"] = str(rejected_qty)
        row["batchQty"] = row["quantities"]["received"]
        if include_pending_qc:
            batch_taka_sum = sum(
                (
                    max(
                        Decimal("0"),
                        Decimal(str(taka.received_quantity or 0)) - child_received[taka.id],
                    )
                    for taka in all_batch_takas
                ),
                Decimal("0"),
            )
            if batch_taka_sum > Decimal("0"):
                row["batchQty"] = str(batch_taka_sum)

        if Decimal(str(row.get("batchQty") or "0")) <= Decimal("0"):
            fallback_qty = Decimal("0")
            if batch.goods_receipt_line_id and getattr(batch.goods_receipt_line, "received_qty", None) is not None:
                fallback_qty = Decimal(str(batch.goods_receipt_line.received_qty))
            elif receipt_ids:
                from apps.purchase.models import GoodsReceiptLine
                grn_l = GoodsReceiptLine.objects.filter(
                    goods_receipt_id__in=receipt_ids, item_id=batch.item_id
                ).first()
                if grn_l and grn_l.received_qty is not None:
                    fallback_qty = Decimal(str(grn_l.received_qty))
            elif batch.purchase_order_line_id and getattr(batch.purchase_order_line, "qty", None) is not None:
                fallback_qty = Decimal(str(batch.purchase_order_line.qty))
            if fallback_qty > Decimal("0"):
                row["batchQty"] = str(fallback_qty)

        # For batch-level inventory when QC passed without individual roll takas
        if row.get("qcStatus") in ("Passed", "Approved") and Decimal(str(row.get("acceptedQty") or "0")) <= Decimal("0"):
            row["acceptedQty"] = row["batchQty"]
            row["availableQty"] = row["batchQty"]
        else:
            row["availableQty"] = row["quantities"]["available"]
        row["reworkQty"] = row["quantities"].get("rework", "0.0000")
        row["rejectedQty"] = row["quantities"].get("rejected", "0.0000")
        row["qcHoldQty"] = row["quantities"].get("qcHold", "0.0000")
        row["uom"] = row["quantities"]["uom"] or "m"

        # Determine actual rate and batch amount
        actual_rate = None
        for taka in display_takas:
            if taka.rate is not None and Decimal(str(taka.rate)) > Decimal("0"):
                actual_rate = Decimal(str(taka.rate))
                break
        if actual_rate is None and batch.goods_receipt_line_id and getattr(batch.goods_receipt_line, "unit_cost", None) is not None:
            actual_rate = Decimal(str(batch.goods_receipt_line.unit_cost))
        if actual_rate is None and receipt_ids:
            from apps.purchase.models import GoodsReceiptLine
            grn_l = GoodsReceiptLine.objects.filter(
                goods_receipt_id__in=receipt_ids, item_id=batch.item_id
            ).first()
            if grn_l and grn_l.unit_cost is not None and Decimal(str(grn_l.unit_cost)) > Decimal("0"):
                actual_rate = Decimal(str(grn_l.unit_cost))
        if actual_rate is None and batch.purchase_order_line_id and getattr(batch.purchase_order_line, "rate", None) is not None:
            actual_rate = Decimal(str(batch.purchase_order_line.rate))

        rate_val = round(actual_rate if actual_rate is not None else Decimal("0"), 4)
        row["actualRate"] = str(rate_val)
        received_meters = Decimal(str(row["batchQty"] or "0"))
        row["batchAmount"] = str(round(received_meters * rate_val, 2))
        row["reprocessStatus"] = "In Reprocess" if reprocess_qty > Decimal("0") else "None"

        batch_ids.append(batch.id)
        batch_count += 1
        if group_by == "po" and po_id:
            key = f"po:{po_id}"
            group = groups_by_key.setdefault(key, {
                "key": key, "type": "po", "poId": po_id, "batches": [],
            })
        elif group_by == "po":
            key = "unlinked"
            group = groups_by_key.setdefault(key, {
                "key": key, "type": "unlinked",
                "title": "Unlinked / Legacy Batches", "batches": [],
            })
        else:
            key = f"fabric:{batch.item_id}" if batch.item_id else "fabric:unknown"
            group = groups_by_key.setdefault(key, {
                "key": key, "type": "fabric",
                "title": batch.item.name if batch.item_id else "Unknown fabric",
                "batches": [],
            })
        group["batches"].append(row)

    groups = list(groups_by_key.values())
    if group_by == "po":
        po_groups = []
        unlinked_group = groups_by_key.get("unlinked")
        for group in groups:
            if group["type"] == "unlinked":
                continue
            po = po_by_id.get(group["poId"])
            if po is None:
                if unlinked_group is None:
                    unlinked_group = {
                        "key": "unlinked", "type": "unlinked",
                        "title": "Unlinked / Legacy Batches", "batches": [],
                    }
                unlinked_group["batches"].extend(group["batches"])
                continue
            quantities = po_quantities(po)
            ordered = quantities.get("ordered", 0)
            received = quantities.get("receivedAtOscar", 0)
            po_lines = list(po.line_items.filter(deleted_at__isnull=True).select_related("item"))
            first_line = po_lines[0] if po_lines else None
            total_batch_amount = sum(
                (Decimal(str(row.get("batchAmount") or "0")) for row in group["batches"]),
                Decimal("0"),
            )
            qc_pending_qty = sum(
                (Decimal(str(row.get("batchQty") or "0")) for row in group["batches"] if row.get("qcStatus") in ("Pending", "Not Inspected") or row.get("awaitingQc")),
                Decimal("0"),
            )
            total_accepted_qty = sum(
                (Decimal(str(row.get("acceptedQty") or "0")) for row in group["batches"]),
                Decimal("0"),
            )
            total_rejected_qty = sum(
                (Decimal(str(row.get("qcRejectedQty") or "0")) for row in group["batches"]),
                Decimal("0"),
            )
            total_available_qty = sum(
                (Decimal(str(row.get("availableQty") or row["quantities"]["available"])) for row in group["batches"]),
                Decimal("0"),
            )
            group.update({
                "poNumber": po.po_number or str(po.id),
                "vendorName": po.party_name or (po.party.name if po.party_id else ""),
                "status": po.status,
                "fabricFlow": po.fabric_flow,
                "fabricFlowLabel": po.get_fabric_flow_display() if po.fabric_flow else ("Factory -> OSCAR Warehouse" if po.purchase_type == "GREY_STOCK" else ""),
                "dyeingMillName": (po.dyeing_mill.name if po.dyeing_mill_id else "") if hasattr(po, "dyeing_mill") and po.dyeing_mill else "",
                "fabricNames": sorted({
                    line.item_name or line.description or line.item.name
                    for line in po_lines
                    if line.item_name or line.description or line.item_id
                } | {
                    row["item"] for row in group["batches"] if row.get("item")
                }),
                "orderedQty": str(ordered),
                "receivedQty": str(received),
                "remainingQty": str(quantities.get("pending", max(0, ordered - received))),
                "uom": first_line.uom if first_line and first_line.uom else "m",
                "batchCount": len(group["batches"]),
                "totalBatchAmount": str(round(total_batch_amount, 2)),
                "qcPendingQty": str(qc_pending_qty),
                "qcPassedQty": str(total_accepted_qty),
                "acceptedQty": str(total_accepted_qty),
                "rejectedQty": str(total_rejected_qty),
                "availableQty": str(total_available_qty),
                "availableStock": str(total_available_qty),
            })
            po_groups.append(group)
        groups = sorted(po_groups, key=lambda group: group["poNumber"])
        if unlinked_group and not selected_po_id:
            groups.append(unlinked_group)
    else:
        for group in groups:
            group["batchCount"] = len(group["batches"])
            group["receivedQty"] = str(sum(
                (Decimal(str(row["batchQty"])) for row in group["batches"]), Decimal("0")
            ))
            group["qcPassedQty"] = str(sum(
                (Decimal(str(row["acceptedQty"])) for row in group["batches"]),
                Decimal("0"),
            ))
            group["availableQty"] = str(sum(
                (Decimal(str(row["quantities"]["available"])) for row in group["batches"]),
                Decimal("0"),
            ))
        groups.sort(key=lambda group: group["title"].casefold())

    return {
        "groups": groups,
        "batchCount": batch_count,
        "summary": batch_services.stock_totals(client_id, batch_ids, roll, eligible_only=True),
        "purchaseOrders": purchase_order_options,
    }
