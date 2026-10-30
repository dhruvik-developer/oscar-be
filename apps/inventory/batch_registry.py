"""Batch Registry grouping backed by purchase, receipt and stock records."""
from decimal import Decimal

from django.db.models import Q

from apps.purchase.fabric_flow import quantities as po_quantities
from apps.purchase.models import GoodsReceipt, PurchaseBill, PurchaseOrder

from . import batch_services
from .models import Batch


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
            "summary": batch_services.stock_totals(client_id, []),
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
        .values("id", "purchase_order_id", "purchase_bill_id", "grn_number")
    )
    grn_by_id = {str(row["id"]): row for row in receipts}
    receipt_po_ids = {
        str(row["id"]): str(row["purchase_order_id"] or bill_po_ids.get(str(row["purchase_bill_id"])))
        for row in receipts if row["purchase_order_id"] or bill_po_ids.get(str(row["purchase_bill_id"]))
    }

    qs = Batch.objects.filter(client_id=client_id, deleted_at__isnull=True).select_related(
        "item", "supplier", "owner_party", "purchase_order", "purchase_order_line", "goods_receipt"
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
        qs = qs.filter(takas__qc_status=params["qcStatus"])

    roll = batch_services.roll_filter(params, prefix="takas__")
    if roll is not None:
        qs = qs.filter(roll).distinct()

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
        takas = batch_services._takas(batch)
        row = batch_services.serialize_batch(batch, takas=takas, include_cost=with_cost)
        if wanted_status and row["status"] != wanted_status:
            continue

        qc_values = sorted({str(taka.qc_status).strip() for taka in takas if taka.qc_status})
        row["qcStatus"] = qc_values[0] if len(qc_values) == 1 else ("Partially Approved" if any(v in ("Passed", "Approved") for v in qc_values) else ("Mixed" if qc_values else "Not Inspected"))
        receipt_ids = {
            str(taka.source_document_id)
            for taka in takas
            if taka.source_document_type in ("GRN", "GoodsReceipt") and taka.source_document_id
        }
        if batch.goods_receipt_id:
            receipt_ids.add(str(batch.goods_receipt_id))
        row["receiptNumbers"] = sorted({
            grn_by_id[receipt_id]["grn_number"]
            for receipt_id in receipt_ids
            if receipt_id in grn_by_id and grn_by_id[receipt_id]["grn_number"]
        } | {
            str(taka.source_document_number)
            for taka in takas
            if taka.source_document_type in ("GRN", "GoodsReceipt") and taka.source_document_number
        })
        row["batchQty"] = row["quantities"]["received"]
        row["availableQty"] = row["quantities"]["available"]
        row["uom"] = row["quantities"]["uom"] or "m"

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
        if selected_po_id and selected_po_id in po_by_id:
            po = po_by_id[selected_po_id]
            key = f"po:{selected_po_id}"
            groups_by_key.setdefault(key, {
                "key": key, "type": "po", "poId": selected_po_id, "batches": [],
            })
            groups = list(groups_by_key.values())
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
                "qcPendingQty": str(quantities.get("awaitingQc", 0)),
                "qcPassedQty": str(quantities.get("accepted", 0)),
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
        groups.sort(key=lambda group: group["title"].casefold())

    return {
        "groups": groups,
        "batchCount": batch_count,
        "summary": batch_services.stock_totals(client_id, batch_ids, roll),
        "purchaseOrders": purchase_order_options,
    }
