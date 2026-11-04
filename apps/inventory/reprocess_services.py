"""
OSCAR ERP Reprocess Services.

Manages sending rejected/rework fabric for reprocessing to external vendors/processors
and receiving returned fabric (in full or partial returns) linked to original PO, batch,
vendor and reprocess records.
"""
import uuid
from decimal import Decimal

from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from apps.core.audit import record_audit
from apps.core.exceptions import BusinessRuleViolation, ValidationFailed
from apps.core.money import ZERO, D, round2, round4
from apps.core.numbering import allocate_number

from . import services as stock
from .models import (
    Batch,
    LotCostEntry,
    LotTaka,
    ReprocessRecord,
    ReprocessReturn,
    StockMovement,
)
from apps.masters.models import Item, Party
from apps.purchase.models import GoodsReceipt, GoodsReceiptLine, PurchaseOrder


def serialize_reprocess_return(ret):
    """Serialize a single ReprocessReturn transaction."""
    receipt_id = str(ret.goods_receipt_id) if ret.goods_receipt_id else None
    return {
        "id": str(ret.id),
        "receiptId": receipt_id,
        "goodsReceiptId": receipt_id,
        "grnNumber": ret.grn_number or (ret.goods_receipt.grn_number if ret.goods_receipt else "—"),
        "returnQty": float(ret.return_quantity),
        "returnDate": ret.return_date.strftime("%d-%m-%Y") if ret.return_date else "",
        "rawReturnDate": ret.return_date.isoformat() if ret.return_date else "",
        "vendorChallan": ret.vendor_return_challan,
        "reprocessCharges": float(ret.reprocess_charges),
        "remarks": ret.remarks or "",
        "qcStatus": ret.qc_status,
        "createdAt": ret.created_at.strftime("%d-%m-%Y %H:%M") if ret.created_at else "",
    }


def serialize_reprocess_record(record):
    """Serialize a ReprocessRecord with full history and return details."""
    returns = list(record.returns.all().order_by("-return_date", "-created_at"))
    latest_ret = returns[0] if returns else None
    pending_ret = next((r for r in returns if r.qc_status == "Pending"), None) or latest_ret
    latest_receipt_id = str(latest_ret.goods_receipt_id) if (latest_ret and latest_ret.goods_receipt_id) else None
    pending_receipt_id = str(pending_ret.goods_receipt_id) if (pending_ret and pending_ret.goods_receipt_id) else latest_receipt_id

    return {
        "id": str(record.id),
        "reprocessNo": record.reprocess_number,
        "originalPoId": str(record.original_po_id) if record.original_po_id else None,
        "originalPoNumber": record.original_po_number or (record.original_po.po_number if record.original_po else "—"),
        "originalBatchId": str(record.original_batch_id) if record.original_batch_id else None,
        "originalBatchCode": record.original_batch_code,
        "reprocessBatchId": str(record.reprocess_batch_id) if record.reprocess_batch_id else None,
        "reprocessBatchCode": record.reprocess_batch_code or f"{record.original_batch_code}-RP",
        "vendorId": str(record.vendor_id) if record.vendor_id else None,
        "vendorName": record.vendor_name or (record.vendor.name if record.vendor else "—"),
        "itemId": str(record.item_id) if record.item_id else None,
        "itemName": record.item_name or (record.item.name if record.item else "—"),
        "outwardChallan": record.outward_challan,
        "sentQty": float(record.sent_quantity),
        "sentDate": record.sent_date.strftime("%d-%m-%Y") if record.sent_date else "",
        "rawSentDate": record.sent_date.isoformat() if record.sent_date else "",
        "returnedQty": float(record.returned_quantity),
        "previouslyReturnedQty": float(record.returned_quantity),
        "outstandingQty": float(record.outstanding_quantity),
        "uom": record.uom or "m",
        "status": record.status,
        "reprocessQcStatus": record.reprocess_qc_status,
        "latestReceiptId": latest_receipt_id,
        "pendingQcReceiptId": pending_receipt_id,
        "receiptId": pending_receipt_id or latest_receipt_id,
        "reason": record.reason or "",
        "expectedReturnDate": record.expected_return_date.strftime("%d-%m-%Y") if record.expected_return_date else "",
        "rawExpectedReturnDate": record.expected_return_date.isoformat() if record.expected_return_date else "",
        "remarks": record.remarks or "",
        "canReceiveReturn": record.outstanding_quantity > ZERO and record.status != "Cancelled",
        "returns": [serialize_reprocess_return(r) for r in returns],
    }


def list_reprocess_records(client_id, query_params=None):
    """List all ReprocessRecords for client, supporting search, filters and date ranges."""
    query_params = query_params or {}
    q = (query_params.get("q") or query_params.get("search") or "").strip().lower()
    status_filter = query_params.get("status") or "All"
    vendor_filter = query_params.get("vendor") or "All"
    batch_filter = (query_params.get("batch") or query_params.get("batchCode") or "").strip()
    from_date = query_params.get("from_date") or query_params.get("fromDate")
    to_date = query_params.get("to_date") or query_params.get("toDate")

    qs = (
        ReprocessRecord.objects.filter(client_id=client_id, deleted_at__isnull=True)
        .select_related("original_po", "original_batch", "reprocess_batch", "vendor", "item")
        .prefetch_related("returns")
        .order_by("-sent_date", "-created_at")
    )

    if from_date:
        qs = qs.filter(sent_date__gte=from_date)
    if to_date:
        qs = qs.filter(sent_date__lte=to_date)
    if vendor_filter != "All":
        qs = qs.filter(Q(vendor_name__icontains=vendor_filter) | Q(vendor__name__icontains=vendor_filter))
    if batch_filter:
        qs = qs.filter(
            Q(original_batch_code__icontains=batch_filter) | Q(reprocess_batch_code__icontains=batch_filter)
        )
    if status_filter != "All":
        qs = qs.filter(status=status_filter)

    records = list(qs)
    if q:
        records = [
            r for r in records
            if any(
                q in str(field or "").lower()
                for field in [
                    r.reprocess_number,
                    r.original_batch_code,
                    r.reprocess_batch_code,
                    r.original_po_number,
                    r.vendor_name,
                    r.item_name,
                    r.outward_challan,
                ]
            )
        ]

    return [serialize_reprocess_record(r) for r in records]


def get_reprocess_record_detail(client_id, record_id_or_number):
    """Retrieve full details of a specific ReprocessRecord."""
    qs = (
        ReprocessRecord.objects.filter(client_id=client_id, deleted_at__isnull=True)
        .select_related("original_po", "original_batch", "reprocess_batch", "vendor", "item")
        .prefetch_related("returns")
    )
    record = qs.filter(Q(pk=record_id_or_number) | Q(reprocess_number=record_id_or_number)).first()
    if not record:
        return None
    return serialize_reprocess_record(record)


@transaction.atomic
def send_batch_for_reprocess(
    client_id,
    batch_code,
    *,
    sent_qty=None,
    outward_challan=None,
    sent_date=None,
    vendor_id=None,
    po_id=None,
    reason=None,
    expected_return_date=None,
    user=None,
):
    """Send rejected/rework fabric from a batch to a vendor for reprocessing."""
    batch_code = (batch_code or "").strip()
    if not batch_code:
        raise ValidationFailed("Batch code is required.", field_errors={"batchCode": ["Required."]})

    # Find the batch
    batch = Batch.objects.filter(client_id=client_id, batch_code__iexact=batch_code, deleted_at__isnull=True).first()
    takas = []
    if batch:
        takas = list(
            batch.takas.filter(deleted_at__isnull=True)
            .select_related("location", "item", "supplier")
            .order_by("created_at")
        )
    else:
        takas = list(
            LotTaka.objects.filter(client_id=client_id, lot_number__iexact=batch_code, deleted_at__isnull=True)
            .select_related("location", "item", "supplier")
            .order_by("created_at")
        )

    if not batch and not takas:
        raise ValidationFailed(f"Batch '{batch_code}' not found.")

    first_taka = takas[0] if takas else None

    # PO resolution
    po = None
    if po_id:
        po = PurchaseOrder.objects.filter(client_id=client_id, pk=po_id).first()
    elif batch and batch.purchase_order_id:
        po = batch.purchase_order
    elif first_taka and first_taka.source_document_type == "GRN" and first_taka.source_document_id:
        grn = GoodsReceipt.objects.filter(pk=first_taka.source_document_id).select_related("purchase_order").first()
        po = grn.purchase_order if grn else None

    # Vendor resolution
    vendor = None
    if vendor_id:
        vendor = Party.objects.filter(client_id=client_id, pk=vendor_id).first()
    elif batch and batch.supplier_id:
        vendor = batch.supplier
    elif po and po.party_id:
        vendor = po.party
    elif first_taka and first_taka.supplier_id:
        vendor = first_taka.supplier

    # Item resolution
    item = getattr(batch, "item", None) or getattr(first_taka, "item", None)
    if not item and po and po.lines.exists():
        item = po.lines.first().item

    # Quantity calculation: prioritize rejected stock, then rework stock, then available
    if takas:
        rejected_takas = [t for t in takas if getattr(t.location, "code", "") == "SYS-REJECT" and D(t.available_quantity or 0) > ZERO]
        rework_takas = [t for t in takas if getattr(t.location, "code", "") == "SYS-REPROCESS" and D(t.available_quantity or 0) > ZERO]
        hold_takas = [t for t in takas if getattr(t.location, "code", "") == "SYS-QC-HOLD" and D(t.available_quantity or 0) > ZERO]
        other_takas = [t for t in takas if D(t.available_quantity or 0) > ZERO]

        eligible_takas = rejected_takas or rework_takas or hold_takas or other_takas
        max_available = sum((D(t.available_quantity or 0) for t in eligible_takas), ZERO)
        source_loc = None
    else:
        eligible_takas = []
        source_locs = ["SYS-REJECT", "SYS-REPROCESS", "SYS-QC-HOLD"]
        batch_identifiers = [batch_code]
        if batch:
            batch_identifiers.append(batch.batch_code)
            if batch.goods_receipt_line and batch.goods_receipt_line.batch_number:
                batch_identifiers.append(batch.goods_receipt_line.batch_number)
        moves = StockMovement.objects.filter(
            client_id=client_id, deleted_at__isnull=True, batch_number__in=list(set(batch_identifiers))
        )
        source_loc = None
        max_available = ZERO
        for sl_code in source_locs:
            bal = moves.filter(location__code=sl_code).aggregate(total=Sum("quantity"))["total"] or ZERO
            if bal > ZERO:
                max_available = round4(bal)
                source_loc = stock.system_location(client_id, sl_code.replace("SYS-", "").replace("-", "_"))
                break
        if max_available <= ZERO and batch and batch.goods_receipt_line:
            max_available = round4(batch.goods_receipt_line.received_qty)
            source_loc = stock.system_location(client_id, "REJECT")

    if sent_qty is None or sent_qty == "":
        sent_qty = max_available
    else:
        sent_qty = round4(D(sent_qty))

    if sent_qty <= ZERO:
        raise ValidationFailed("Sent quantity must be greater than zero.", field_errors={"sentQty": ["Must be > 0"]})

    if sent_qty > max_available and max_available > ZERO:
        raise ValidationFailed(
            f"Sent quantity ({sent_qty}) exceeds available rejected/rework stock ({max_available}).",
            field_errors={"sentQty": [f"Maximum available is {max_available}."]},
        )

    # Generate document numbers
    rpc_no = allocate_number(client_id, "RPC")
    outward_no = (outward_challan or "").strip() or f"CHL-{rpc_no}"

    # Relocate fabric to external WIP location
    wip_loc = stock.system_location(client_id, "REPROCESS")
    if takas:
        left_to_move = sent_qty
        for taka in eligible_takas:
            if left_to_move <= ZERO:
                break
            avail = D(taka.available_quantity or 0)
            take = min(left_to_move, avail)
            stock.relocate_lot(
                client_id=client_id,
                lot=taka,
                quantity=take,
                to_location_id=wip_loc.id,
                out_type="JOBWORK_OUTWARD",
                in_type="JOBWORK_OUTWARD",
                reference_type="ReprocessRecord",
                reference_id=uuid.uuid4(),
                reference_number=rpc_no,
                notes=f"Sent for Reprocess ({rpc_no}): {reason or 'Defective fabric sent to vendor'}",
                user=user,
                qc_status="Reprocess",
                split_suffix="RP",
            )
            left_to_move -= take
    else:
        from_loc_id = source_loc.id if source_loc else stock.system_location(client_id, "REJECT").id
        stock.relocate_item_qty(
            client_id=client_id,
            item=item.id if item else (batch.item_id if batch else None),
            quantity=sent_qty,
            from_location_id=from_loc_id,
            to_location_id=wip_loc.id,
            out_type="JOBWORK_OUTWARD",
            in_type="JOBWORK_OUTWARD",
            reference_type="ReprocessRecord",
            reference_id=uuid.uuid4(),
            reference_number=rpc_no,
            notes=f"Sent for Reprocess ({rpc_no}): {reason or 'Defective fabric sent to vendor'}",
            user=user,
            batch_number=batch_code,
        )

    # Create ReprocessRecord
    record = ReprocessRecord.objects.create(
        client_id=client_id,
        reprocess_number=rpc_no,
        original_po=po,
        original_po_number=po.po_number if po else (getattr(batch, "source_document_number", None) or "—"),
        original_batch=batch,
        original_batch_code=batch_code,
        reprocess_batch_code=f"{batch_code}-RP",
        vendor=vendor,
        vendor_name=vendor.name if vendor else (getattr(batch.supplier, "name", "") if getattr(batch, "supplier", None) else "—"),
        item=item,
        item_name=item.name if item else "—",
        outward_challan=outward_no,
        sent_quantity=sent_qty,
        sent_date=sent_date or timezone.localdate(),
        returned_quantity=ZERO,
        outstanding_quantity=sent_qty,
        uom=getattr(batch, "uom", None) or (first_taka.uom if first_taka else "m"),
        status="Sent to Vendor",
        reprocess_qc_status="Pending",
        reason=reason or "QC Rejected fabric sent for reprocess",
        expected_return_date=expected_return_date or None,
        created_by=user if getattr(user, "is_authenticated", False) else None,
    )

    record_audit(
        client=client_id,
        actor=user,
        action="reprocess_outward",
        entity_type="ReprocessRecord",
        entity_id=record.id,
        entity_label=record.reprocess_number,
        description=f"Sent {sent_qty} {record.uom} of batch {batch_code} for reprocess to {record.vendor_name}",
        after={"reprocessNo": rpc_no, "sentQty": str(sent_qty), "challan": outward_no},
    )

    return serialize_reprocess_record(record)


@transaction.atomic
def receive_reprocess_return(
    client_id,
    record_id,
    *,
    return_qty,
    return_date=None,
    vendor_return_challan,
    reprocess_charges=0,
    remarks=None,
    idempotency_key=None,
    user=None,
):
    """Receive reprocessed fabric returned by vendor against a ReprocessRecord.

    Validates return quantity against outstanding balance, automatically generates
    return receipt GRN, sets returned fabric to QC Pending, and links to Reprocess Batch.
    """
    if idempotency_key:
        existing = ReprocessReturn.objects.filter(
            reprocess_record_id=record_id, idempotency_key=idempotency_key
        ).first()
        if existing:
            return serialize_reprocess_record(existing.reprocess_record)

    record = (
        ReprocessRecord.objects.select_for_update()
        .filter(client_id=client_id, pk=record_id, deleted_at__isnull=True)
        .first()
    )
    if not record:
        raise ValidationFailed("Reprocess Record not found.")

    return_qty = round4(D(return_qty or 0))
    if return_qty <= ZERO:
        raise ValidationFailed("Return quantity must be greater than zero.", field_errors={"returnQty": ["Must be > 0"]})

    if return_qty > D(record.outstanding_quantity):
        raise ValidationFailed(
            f"Return quantity ({return_qty} {record.uom}) exceeds outstanding quantity ({record.outstanding_quantity} {record.uom}).",
            field_errors={"returnQty": [f"Cannot exceed outstanding quantity of {record.outstanding_quantity} {record.uom}."]},
        )

    challan = (vendor_return_challan or "").strip()
    if not challan:
        raise ValidationFailed("Vendor return challan is required.", field_errors={"vendorReturnChallan": ["Required."]})

    ret_date = return_date or timezone.localdate()

    # 1. Create return receipt / GRN transaction
    grn_no = allocate_number(client_id, "GRN")
    qc_hold_location = stock.system_location(client_id, "QC_HOLD")
    from apps.masters.models import Location
    godown_loc = (
        Location.objects.filter(client_id=client_id, is_active=True, deleted_at__isnull=True, type__in=["Warehouse", "Godown"])
        .exclude(code__startswith="SYS-").order_by("created_at").first()
        or Location.objects.filter(client_id=client_id, code="WH-MAIN").first()
    )

    receipt = GoodsReceipt.objects.create(
        client_id=client_id,
        grn_number=grn_no,
        party=record.vendor,
        purchase_order=record.original_po,
        receipt_date=ret_date,
        supplier_challan_no=challan,
        qc_status="Pending",  # CRITICAL: Returned fabric must go through Reprocess QC!
        location=godown_loc or qc_hold_location,
        qc_note=f"Reprocess return for {record.reprocess_number} (Challan: {challan}). Original Batch: {record.original_batch_code}",
        created_by=user if getattr(user, "is_authenticated", False) else None,
    )

    GoodsReceiptLine.objects.create(
        client_id=client_id,
        goods_receipt=receipt,
        item=record.item,
        received_qty=return_qty,
        rejected_qty=ZERO,
        uom=record.uom,
        batch_number=record.reprocess_batch_code or f"{record.original_batch_code}-RP",
        remarks=f"Reprocess return ({record.reprocess_number})",
    )

    # 2. Create or link to Reprocess Batch
    rp_batch_code = record.reprocess_batch_code or f"{record.original_batch_code}-RP"
    reprocess_batch = Batch.objects.filter(
        client_id=client_id, batch_code=rp_batch_code, deleted_at__isnull=True
    ).first()

    if reprocess_batch is None:
        reprocess_batch = Batch.objects.create(
            client_id=client_id,
            batch_code=rp_batch_code,
            origin="JOB_WORK",
            fabric_type=getattr(record.original_batch, "fabric_type", "GREY") if record.original_batch else "GREY",
            item=record.item,
            supplier=record.vendor,
            source_document_type="GoodsReceipt",
            source_document_number=grn_no,
            source_document_id=receipt.id,
            purchase_order=record.original_po,
            goods_receipt=receipt,
            uom=record.uom,
            created_on=ret_date,
            notes=f"Reprocess return batch for {record.original_batch_code}",
            created_by=user if getattr(user, "is_authenticated", False) else None,
        )
        if record.original_batch:
            reprocess_batch.parent_batches.add(record.original_batch)

    record.reprocess_batch = reprocess_batch
    record.reprocess_batch_code = reprocess_batch.batch_code

    # 3. Stock Movement for the returned material directly at batch level (no takas created)
    movement = StockMovement.objects.create(
        client_id=client_id,
        item=record.item,
        lot_taka=None,
        batch_number=reprocess_batch.batch_code,
        location=qc_hold_location,
        quantity=return_qty,
        type="JOBWORK_INWARD",
        movement_date=ret_date,
        reference_type="GoodsReceipt",
        reference_number=grn_no,
        reference_id=receipt.id,
        source_document_type="ReprocessRecord",
        source_document_id=record.id,
        notes=f"Reprocess return receipt {grn_no} - Challan {challan}",
        created_by=user if getattr(user, "is_authenticated", False) else None,
    )
    stock._apply_to_balance(movement)

    # 5. Optional Reprocess Charges
    charges = round2(D(reprocess_charges or 0))
    if charges > ZERO:
        LotCostEntry.objects.create(
            client_id=client_id,
            lot_number=reprocess_batch.batch_code,
            cost_type="Reprocess",
            amount=charges,
            quantity=return_qty,
            source_document_type="GoodsReceipt",
            source_document_number=grn_no,
            source_document_id=receipt.id,
            remarks=f"Reprocess charges for return challan {challan}",
            is_approved=True,
            created_by=user if getattr(user, "is_authenticated", False) else None,
        )

    # 6. Create ReprocessReturn record
    ReprocessReturn.objects.create(
        client_id=client_id,
        reprocess_record=record,
        goods_receipt=receipt,
        grn_number=grn_no,
        return_quantity=return_qty,
        return_date=ret_date,
        vendor_return_challan=challan,
        reprocess_charges=charges,
        remarks=remarks or "",
        idempotency_key=idempotency_key or None,
        qc_status="Pending",
        created_by=user if getattr(user, "is_authenticated", False) else None,
    )

    # 7. Update ReprocessRecord totals
    new_returned = round4(D(record.returned_quantity) + return_qty)
    new_outstanding = round4(D(record.sent_quantity) - new_returned)

    record.returned_quantity = new_returned
    record.outstanding_quantity = max(ZERO, new_outstanding)
    if record.outstanding_quantity <= ZERO:
        record.status = "Completed"
    else:
        record.status = "Partially Returned"

    record.reprocess_qc_status = "Pending"
    record.save(
        update_fields=[
            "returned_quantity",
            "outstanding_quantity",
            "status",
            "reprocess_qc_status",
            "reprocess_batch",
            "reprocess_batch_code",
            "updated_at",
        ]
    )

    record_audit(
        client=client_id,
        actor=user,
        action="reprocess_return_received",
        entity_type="ReprocessRecord",
        entity_id=record.id,
        entity_label=record.reprocess_number,
        description=f"Received return of {return_qty} {record.uom} on GRN {grn_no} (Challan: {challan})",
        after={
            "grnNumber": grn_no,
            "returnQty": str(return_qty),
            "outstandingQty": str(record.outstanding_quantity),
            "status": record.status,
        },
    )

    record.refresh_from_db()
    return serialize_reprocess_record(record)
