"""
Logistics / Transport / Tempo trip services (PDF §13).

Tracks transportation trips, vehicle/driver details, linked documents, and material lines.
Supports allocating transport freight & charges directly into related LotCostEntry ledgers.
Integrates live godown-wise inventory dispatch, in-transit tracking, destination receipt, and cancellation.
"""
from decimal import Decimal
import uuid

from django.db import transaction
from django.db.models import F, Q, Sum
from django.utils import timezone

from apps.accounting import services as ledger
from apps.core.exceptions import BusinessRuleViolation, NotFound, ValidationFailed
from apps.core.money import ZERO, D, round2, round4
from apps.core.numbering import allocate_number
from apps.inventory import services as stock
from apps.inventory.costing_services import post_lot_cost
from apps.inventory.models import LotTaka, Trip, TripMaterialLine
from apps.masters.models import Location


def resolve_warehouse(client_id, location_ref):
    """Resolve a Location warehouse by UUID, code, or name."""
    if not location_ref:
        return None
    ref_str = str(location_ref).strip()
    try:
        u = uuid.UUID(ref_str)
        loc = Location.objects.filter(client_id=client_id, pk=u, deleted_at__isnull=True).first()
        if loc:
            return loc
    except (ValueError, TypeError, AttributeError):
        pass

    return (
        Location.objects.filter(client_id=client_id, deleted_at__isnull=True)
        .filter(Q(name__iexact=ref_str) | Q(code__iexact=ref_str))
        .first()
    )


def get_available_godown_inventory(client_id, location_id_or_name):
    """
    Return active available batches, takas and rolls in the specified godown.
    """
    loc = resolve_warehouse(client_id, location_id_or_name)
    if not loc:
        return {"location": None, "batches": [], "lots": []}

    takas = (
        LotTaka.objects.filter(
            client_id=client_id,
            location=loc,
            available_quantity__gt=0,
            deleted_at__isnull=True,
        )
        .exclude(status__in=["Dispatched", "Reject", "QC Hold", "Sample", "External WIP"])
        .exclude(qc_status__in=["Rejected", "Reprocess", "On Hold"])
        .select_related("batch", "batch__purchase_order", "item", "shade", "fabric_quality")
        .order_by("batch__batch_code", "lot_number", "taka_number")
    )

    batches_dict = {}
    for t in takas:
        b = t.batch
        batch_code = (b.batch_code if b and b.batch_code else t.lot_number) or "UNASSIGNED"
        batch_id = str(b.id) if b else None
        po_number = b.purchase_order.po_number if (b and b.purchase_order) else None
        po_id = str(b.purchase_order_id) if (b and b.purchase_order_id) else None
        fabric_name = (
            t.item.name
            if t.item
            else (t.fabric_quality.name if t.fabric_quality else "Fabric")
        )
        shade_name = t.shade.name if t.shade else (getattr(b, "dyeing_colour_name", None) or (t.item.fabric_color if t.item else ""))

        if batch_code not in batches_dict:
            batches_dict[batch_code] = {
                "batch_id": batch_id,
                "batchId": batch_id,
                "batch_code": batch_code,
                "batchCode": batch_code,
                "lot_number": batch_code,
                "lotNumber": batch_code,
                "po_number": po_number,
                "poNumber": po_number,
                "po_id": po_id,
                "poId": po_id,
                "fabric_type": getattr(b, "fabric_type", None),
                "fabricType": getattr(b, "fabric_type", None),
                "itemId": str(t.item_id) if t.item_id else None,
                "fabric_name": fabric_name,
                "fabricName": fabric_name,
                "shade": shade_name or "-",
                "available_meters": ZERO,
                "availableMeters": 0,
                "available_takas_count": 0,
                "availableRollsCount": 0,
                "takas": [],
                "rolls": [],
            }

        roll_info = {
            "id": str(t.id),
            "lotTakaId": str(t.id),
            "lot_taka_id": str(t.id),
            "taka_id": t.taka_id or t.barcode or f"{batch_code}-{t.taka_number}",
            "takaId": t.taka_id or t.barcode or f"{batch_code}-{t.taka_number}",
            "taka_number": t.taka_number,
            "takaNumber": t.taka_number,
            "batch_code": batch_code,
            "batchCode": batch_code,
            "po_number": po_number,
            "poNumber": po_number,
            "lot_number": t.lot_number,
            "lotNumber": t.lot_number,
            "available_meters": float(round4(t.available_quantity)),
            "availableMeters": float(round4(t.available_quantity)),
            "roll_weight": float(round4(t.roll_weight)) if t.roll_weight is not None else None,
            "rollWeight": float(round4(t.roll_weight)) if t.roll_weight is not None else None,
            "qc_status": t.qc_status,
            "qcStatus": t.qc_status,
            "status": t.status,
            "item_id": str(t.item_id) if t.item_id else None,
            "itemId": str(t.item_id) if t.item_id else None,
            "item_name": t.item.name if t.item else None,
            "itemName": t.item.name if t.item else None,
            "shade": shade_name or "-",
        }
        batches_dict[batch_code]["available_meters"] += D(t.available_quantity)
        batches_dict[batch_code]["available_takas_count"] += 1
        batches_dict[batch_code]["availableRollsCount"] += 1
        batches_dict[batch_code]["takas"].append(roll_info)
        batches_dict[batch_code]["rolls"].append(roll_info)

    batches_list = []
    for b_code, data in batches_dict.items():
        data["available_meters"] = float(round4(data["available_meters"]))
        data["availableMeters"] = data["available_meters"]
        batches_list.append(data)

    return {
        "location": {
            "id": str(loc.id),
            "name": loc.name,
            "code": loc.code,
            "type": loc.type,
        },
        "batches": batches_list,
        "lots": batches_list,
    }


@transaction.atomic
def create_trip(
    *,
    client_id,
    from_location,
    to_location,
    from_warehouse_id=None,
    to_warehouse_id=None,
    vehicle_number=None,
    vehicle_id=None,
    driver_id=None,
    trip_date=None,
    transporter_id=None,
    transporter_party_id=None,
    driver_name=None,
    driver_mobile=None,
    lr_number=None,
    lr_date=None,
    trip_type="OUTWARD",
    freight_amount=0,
    loading_charges=0,
    unloading_charges=0,
    other_charges=0,
    paid_by="Company",
    cost_allocation_target="LOT",
    material_lines=None,
    remarks=None,
    user=None,
):
    """Create a new transport trip with material lines and real-time inventory deduction."""
    trip_number = allocate_number(client_id, "TRP")
    freight = D(freight_amount or 0)
    loading = D(loading_charges or 0)
    unloading = D(unloading_charges or 0)
    other = D(other_charges or 0)
    total_charges = round2(freight + loading + unloading + other)

    if vehicle_id and not vehicle_number:
        from apps.masters.models import Vehicle
        v_obj = Vehicle.objects.filter(client_id=client_id, pk=vehicle_id).first()
        if v_obj:
            vehicle_number = v_obj.vehicle_number
            if not transporter_id and v_obj.transporter_id:
                transporter_id = v_obj.transporter_id

    if driver_id and (not driver_name or not driver_mobile):
        from apps.masters.models import Driver
        d_obj = Driver.objects.filter(client_id=client_id, pk=driver_id).first()
        if d_obj:
            if not driver_name:
                driver_name = d_obj.name
            if not driver_mobile:
                driver_mobile = d_obj.mobile

    vehicle_number = vehicle_number or "VEHICLE"

    from_wh = resolve_warehouse(client_id, from_warehouse_id or from_location)
    to_wh = resolve_warehouse(client_id, to_warehouse_id or to_location)

    from_location_str = (from_wh.name if from_wh else (from_location or "").strip()) or "Main Godown"
    to_location_str = (to_wh.name if to_wh else (to_location or "").strip()) or "Party Godown / Job Worker"
    lr_date = lr_date or None

    trip = Trip.objects.create(
        client_id=client_id,
        trip_number=trip_number,
        trip_date=trip_date or timezone.localdate(),
        from_location=from_location_str,
        to_location=to_location_str,
        from_warehouse=from_wh,
        to_warehouse=to_wh,
        transporter_id=transporter_id,
        transporter_party_id=transporter_party_id,
        vehicle_id=vehicle_id,
        driver_id=driver_id,
        vehicle_number=vehicle_number,
        driver_name=driver_name,
        driver_mobile=driver_mobile,
        lr_number=lr_number,
        lr_date=lr_date,
        trip_type=trip_type,
        status="Draft",
        freight_amount=freight,
        loading_charges=loading,
        unloading_charges=unloading,
        other_charges=other,
        total_charges=total_charges,
        paid_by=paid_by,
        cost_allocation_target=cost_allocation_target,
        remarks=remarks,
    )

    if material_lines:
        transit_loc = stock.system_location(client_id, "IN_TRANSIT")
        has_dispatched_stock = False

        for line_data in material_lines:
            taka_id = line_data.get("lotTakaId") or line_data.get("lot_taka_id")
            lot_taka = None
            if taka_id:
                lot_taka = LotTaka.objects.select_for_update().filter(client_id=client_id, pk=taka_id, deleted_at__isnull=True).first()
            elif line_data.get("batchCode") or line_data.get("batch_code") or line_data.get("lotNumber"):
                code = line_data.get("batchCode") or line_data.get("batch_code") or line_data.get("lotNumber")
                taka_qs = LotTaka.objects.select_for_update().filter(
                    client_id=client_id,
                    deleted_at__isnull=True,
                ).filter(
                    Q(batch__batch_code=code) | Q(lot_number=code)
                )
                if from_wh:
                    taka_qs = taka_qs.filter(location=from_wh)
                taka_num = line_data.get("takaNumber") or line_data.get("taka_number")
                if taka_num:
                    taka_qs = taka_qs.filter(taka_number=taka_num)
                lot_taka = taka_qs.first()

            qty = D(line_data.get("quantity") or line_data.get("qty") or line_data.get("meters") or 0)
            weight = line_data.get("rollWeight") or line_data.get("roll_weight") or line_data.get("weightKg")
            weight = D(weight) if weight else None

            target_taka = lot_taka
            if lot_taka and qty > ZERO:
                available = round4(lot_taka.available_quantity)
                batch_label = getattr(lot_taka.batch, 'batch_code', None) or lot_taka.lot_number
                roll_label = lot_taka.taka_id or f"Roll #{lot_taka.taka_number}"
                if qty > available:
                    raise BusinessRuleViolation(
                        f"Roll / Taka {roll_label} (Batch {batch_label}) has only {available} M available; cannot dispatch {qty} M.",
                        code="INSUFFICIENT_STOCK",
                        payload={"lotTakaId": str(lot_taka.id), "available": str(available), "requested": str(qty)},
                    )

                if from_wh and lot_taka.location_id != from_wh.id:
                    raise ValidationFailed(
                        f"Roll / Taka {roll_label} is in {lot_taka.location.name}, not {from_wh.name}."
                    )

                # Move quantity into the in-transit store.
                # stock.relocate_lot handles splitting if partial dispatch!
                target_taka, movements = stock.relocate_lot(
                    client_id=client_id,
                    lot=lot_taka,
                    quantity=qty,
                    to_location_id=transit_loc.id,
                    out_type="TRANSFER_OUT",
                    in_type="TRANSFER_IN",
                    reference_type="Trip",
                    reference_id=trip.id,
                    reference_number=trip.trip_number,
                    split_suffix="TR",
                    notes=f"Trip {trip.trip_number} dispatch: {from_location_str} → {to_location_str}",
                    user=user,
                )
                target_taka.status = "Reserved"
                target_taka.save(update_fields=["status", "updated_at"])
                has_dispatched_stock = True

            batch_code = ""
            po_number = ""
            po_id = None
            if target_taka and target_taka.batch:
                batch_code = target_taka.batch.batch_code or ""
                if target_taka.batch.purchase_order:
                    po_number = target_taka.batch.purchase_order.po_number or ""
                    po_id = target_taka.batch.purchase_order_id
            if not batch_code:
                batch_code = line_data.get("batchCode") or line_data.get("batch_code") or line_data.get("lotNumber") or (target_taka.lot_number if target_taka else "")
            if not po_number:
                po_number = line_data.get("poNumber") or line_data.get("documentReferenceNumber") or ""

            TripMaterialLine.objects.create(
                client_id=client_id,
                trip=trip,
                lot_taka=target_taka,
                lot_number=batch_code,
                taka_number=line_data.get("takaNumber") or (target_taka.taka_number if target_taka else ""),
                item_id=line_data.get("itemId") or (target_taka.item_id if target_taka else None),
                quantity=qty,
                roll_weight=weight or (target_taka.roll_weight if target_taka else None),
                document_reference_type=line_data.get("documentReferenceType") or line_data.get("document_reference_type") or ("PURCHASE_ORDER" if po_number else None),
                document_reference_id=line_data.get("documentReferenceId") or line_data.get("document_reference_id") or po_id,
                document_reference_number=po_number or line_data.get("documentReferenceNumber"),
            )

        if has_dispatched_stock:
            trip.status = "In Transit"
            trip.dispatch_time = timezone.now()
            trip.is_stock_deducted = True
            trip.save(update_fields=["status", "dispatch_time", "is_stock_deducted", "updated_at"])

    return trip


@transaction.atomic
def allocate_trip_freight(trip, *, user=None):
    """
    Apportion trip charges across material lines and post directly into the lot cost ledger (PDF §09, §13).
    """
    trip = Trip.objects.select_for_update().get(pk=trip.pk)
    if trip.is_cost_allocated:
        raise BusinessRuleViolation("Cost for this trip has already been allocated.")

    total_charges = D(trip.total_charges)
    if total_charges <= ZERO:
        trip.is_cost_allocated = True
        trip.save(update_fields=["is_cost_allocated", "updated_at"])
        return trip

    lines = list(trip.materials.all())
    if not lines and trip.cost_allocation_target != "EXPENSE":
        raise ValidationFailed(
            f"Cannot allocate cost to {trip.cost_allocation_target}: this trip has no material lines. "
            "Add lot/taka lines to the trip, or record it with Cost Allocation Target = EXPENSE."
        )

    total_meters = sum([D(line.quantity) for line in lines])
    total_weight = sum([D(line.roll_weight or 0) for line in lines])

    use_weight = total_weight > ZERO and total_meters <= ZERO
    divisor = total_weight if use_weight else total_meters

    cost_type = (
        "Processing transport"
        if trip.trip_type in ("JOBWORK_OUT", "JOBWORK_IN")
        else "Purchase freight/loading"
    )

    allocated_so_far = ZERO
    for idx, line in enumerate(lines, start=1):
        basis_qty = D(line.roll_weight or 0) if use_weight else D(line.quantity)
        if idx == len(lines):
            allocated = round4(total_charges - allocated_so_far)
        else:
            allocated = round4((basis_qty / divisor) * total_charges) if divisor > ZERO else ZERO

        allocated_so_far += allocated
        line.allocated_freight = allocated
        line.save(update_fields=["allocated_freight", "updated_at"])

        lot_num = line.lot_number or (line.lot_taka.lot_number if line.lot_taka else None)
        if lot_num and trip.cost_allocation_target == "LOT":
            post_lot_cost(
                client_id=trip.client_id,
                lot_number=lot_num,
                lot_taka=line.lot_taka,
                cost_type=cost_type,
                amount=allocated,
                quantity=line.quantity,
                source_document_type="TRIP",
                source_document_id=trip.id,
                source_document_number=trip.trip_number,
                user=user,
                is_approved=True,
                remarks=f"Freight allocation from Trip {trip.trip_number} ({trip.vehicle_number})",
            )

        if trip.cost_allocation_target == "SALES_ORDER":
            from apps.sales.models import SalesOrder
            so = None
            if line.document_reference_id:
                so = SalesOrder.objects.filter(client_id=trip.client_id, pk=line.document_reference_id).first()
            if not so and line.document_reference_number:
                so = SalesOrder.objects.filter(client_id=trip.client_id, order_number=line.document_reference_number).first()
            if so:
                so.allocated_freight_cost = F("allocated_freight_cost") + round2(allocated)
                so.save(update_fields=["allocated_freight_cost", "updated_at"])

    if (trip.paid_by == "Company" or trip.cost_allocation_target == "EXPENSE") and total_charges > ZERO:
        freight_acc = ledger.system_account(trip.client_id, "freight")
        creditors_acc = ledger.system_account(trip.client_id, "creditors")
        transporter_party = trip.transporter_party
        if not transporter_party and trip.transporter:
            from apps.masters.models import Party
            transporter_party = Party.objects.filter(
                client_id=trip.client_id,
                name__iexact=trip.transporter.name,
                deleted_at__isnull=True,
            ).first()

        ledger.post_entry(
            client=trip.client_id,
            entry_date=trip.trip_date or timezone.localdate(),
            narration=f"Freight allocation for Trip {trip.trip_number} ({trip.vehicle_number})",
            source_document_type="TRIP",
            source_document_id=trip.id,
            postings=[
                ledger.debit(
                    freight_acc,
                    total_charges,
                    description=f"Freight charge Trip {trip.trip_number}",
                ),
                ledger.credit(
                    creditors_acc,
                    total_charges,
                    party=transporter_party,
                    description=f"Freight payable Trip {trip.trip_number}",
                ),
            ],
            user=user,
        )

    trip.is_cost_allocated = True
    trip.save(update_fields=["is_cost_allocated", "updated_at"])
    return trip


@transaction.atomic
def confirm_trip_delivery(trip, *, delivery_time=None, proof_file_id=None, remarks=None, user=None):
    """
    Confirm delivery for a transport trip (PDF §13).
    Sets status to 'Delivered', and updates destination warehouse stock if internal/warehouse transfer.
    Prevents duplicate delivery and duplicate stock receipts.
    """
    trip = Trip.objects.select_for_update().get(pk=trip.pk)
    if trip.status == "Delivered" and trip.is_stock_received:
        return trip

    dest_loc = trip.to_warehouse
    if not dest_loc and trip.to_location:
        dest_loc = (
            Location.objects.filter(client_id=trip.client_id, deleted_at__isnull=True)
            .filter(Q(name__iexact=trip.to_location.strip()) | Q(code__iexact=trip.to_location.strip()))
            .exclude(code__startswith="SYS-")
            .exclude(type__in=("Transit", "Scrap"))
            .first()
        )

    if trip.is_stock_deducted and not trip.is_stock_received:
        if dest_loc:
            for line in trip.materials.select_for_update().all():
                if line.lot_taka:
                    holder = line.lot_taka
                    received_holder, movements = stock.relocate_lot(
                        client_id=trip.client_id,
                        lot=holder,
                        quantity=line.quantity,
                        to_location_id=dest_loc.id,
                        out_type="TRANSFER_OUT",
                        in_type="TRANSFER_IN",
                        reference_type="TripDelivery",
                        reference_id=trip.id,
                        reference_number=trip.trip_number,
                        split_suffix="RC",
                        notes=f"Trip {trip.trip_number} receipt at {dest_loc.name}",
                        user=user,
                    )
                    received_holder.status = "Available"
                    received_holder.save(update_fields=["status", "updated_at"])
            trip.to_warehouse = dest_loc
            trip.is_stock_received = True
        else:
            for line in trip.materials.select_for_update().all():
                if line.lot_taka:
                    line.lot_taka.status = "Dispatched"
                    line.lot_taka.save(update_fields=["status", "updated_at"])
            trip.is_stock_received = True

    trip.status = "Delivered"
    trip.delivery_time = delivery_time or timezone.now()
    if proof_file_id:
        trip.proof_file_id = proof_file_id
    if remarks:
        trip.remarks = f"{trip.remarks}\n{remarks}" if trip.remarks else remarks
    trip.save(update_fields=["status", "delivery_time", "proof_file", "remarks", "to_warehouse", "is_stock_received", "updated_at"])
    return trip


@transaction.atomic
def cancel_trip(trip, *, user=None, reason=None):
    """
    Cancel a trip. If stock was deducted into transit, return it to the source warehouse.
    Delivered trips cannot be cancelled.
    """
    trip = Trip.objects.select_for_update().get(pk=trip.pk)
    if trip.status == "Delivered" or trip.is_stock_received:
        raise BusinessRuleViolation("Cannot cancel a trip that has already been delivered or received.")
    if trip.status == "Cancelled":
        return trip

    if trip.is_stock_deducted:
        source_loc = trip.from_warehouse
        if not source_loc and trip.from_location:
            source_loc = (
                Location.objects.filter(client_id=trip.client_id, deleted_at__isnull=True)
                .filter(Q(name__iexact=trip.from_location.strip()) | Q(code__iexact=trip.from_location.strip()))
                .first()
            )

        if source_loc:
            for line in trip.materials.select_for_update().all():
                if line.lot_taka:
                    returned, movements = stock.relocate_lot(
                        client_id=trip.client_id,
                        lot=line.lot_taka,
                        quantity=line.quantity,
                        to_location_id=source_loc.id,
                        out_type="TRANSFER_OUT",
                        in_type="TRANSFER_IN",
                        reference_type="TripCancellation",
                        reference_id=trip.id,
                        reference_number=trip.trip_number,
                        split_suffix="CN",
                        notes=f"Trip {trip.trip_number} cancelled; returned to {source_loc.name}",
                        user=user,
                    )
                    returned.status = "Available"
                    returned.save(update_fields=["status", "updated_at"])
        trip.is_stock_deducted = False

    trip.status = "Cancelled"
    if reason:
        trip.remarks = f"{trip.remarks}\nCancelled: {reason}" if trip.remarks else f"Cancelled: {reason}"
    trip.save(update_fields=["status", "is_stock_deducted", "remarks", "updated_at"])
    return trip
