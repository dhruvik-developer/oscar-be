"""Multi-warehouse inventory: warehouse stock, inter-warehouse transfers.

A *warehouse* is a top-level, non-system :class:`~apps.masters.models.Location`.
There are no zones, racks or bins in this module; stock recorded on an older
child location (Zone / Godown / Rack / Bin) is rolled up into its top-level
warehouse so historical stock still counts.

Transfers never change the company's total stock. Every move is a
``TRANSFER_OUT`` / ``TRANSFER_IN`` pair:

* request  -- the selected takas are marked Reserved at the source;
* dispatch -- source -> the in-transit store (``SYS-IN-TRANSIT``);
* receive  -- in-transit store -> destination (partial receipts allowed);
* complete -- anything still in transit is returned to the source or, with
  ``adjust_stock``, written off as a transit loss; leftover reservations are
  released.
"""

from collections import defaultdict
from decimal import Decimal

from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from apps.core.money import D, ZERO, round4
from apps.core.exceptions import BusinessRuleViolation, Conflict, ValidationFailed
from apps.core.numbering import allocate_number

from . import services as stock
from .models import RELEASED_QC_STATUSES

SYSTEM_PREFIX = "SYS-"
SELLABLE_QC = RELEASED_QC_STATUSES
#: Taka statuses that may be reserved for a transfer.
FREE_STATUSES = ("Available", "Active")
OPEN_STATUSES = ("Requested", "Approved", "In Transit", "Partially Received")
TOLERANCE = Decimal("0.0001")


# ---------------------------------------------------------------------------
# Warehouses
# ---------------------------------------------------------------------------
def is_system(location):
    return (location.code or "").startswith(SYSTEM_PREFIX) or location.type in ("Transit", "Scrap")


def warehouse_queryset(client_id, include_inactive=False):
    from apps.masters.models import Location

    qs = Location.objects.filter(
        client_id=client_id, deleted_at__isnull=True, parent__isnull=True,
    ).exclude(code__startswith=SYSTEM_PREFIX).exclude(type__in=["Transit", "Scrap"])
    if not include_inactive:
        qs = qs.filter(is_active=True)
    return qs.order_by("name")


def root_map(client_id):
    """{location_id: top-level warehouse id} for every non-system location."""
    from apps.masters.models import Location

    rows = list(
        Location.objects.filter(client_id=client_id, deleted_at__isnull=True)
        .values("id", "parent_id", "code", "type")
    )
    parent = {r["id"]: r["parent_id"] for r in rows}
    system = {
        r["id"] for r in rows
        if (r["code"] or "").startswith(SYSTEM_PREFIX) or r["type"] in ("Transit", "Scrap")
    }
    out = {}
    for r in rows:
        node, seen = r["id"], set()
        while parent.get(node) and node not in seen:
            seen.add(node)
            node = parent[node]
        if node not in system and r["id"] not in system:
            out[r["id"]] = node
    return out


def warehouse_location_ids(client_id, warehouse_id):
    """The warehouse and any legacy child locations under it."""
    return [loc for loc, root in root_map(client_id).items() if str(root) == str(warehouse_id)]


def get_warehouse(client_id, warehouse_id, field="warehouseId"):
    location = warehouse_queryset(client_id, include_inactive=True).filter(pk=warehouse_id).first()
    if location is None:
        raise ValidationFailed("Unknown warehouse.", field_errors={field: ["Not a warehouse."]})
    return location


def serialize_warehouse(location):
    address = location.address if isinstance(location.address, dict) else {}
    return {
        "id": str(location.id),
        "code": location.code,
        "name": location.name,
        "address": address.get("line1") or address.get("address") or "",
        "city": address.get("city") or "",
        "state": address.get("state") or "",
        "pincode": address.get("pincode") or address.get("pin") or "",
        "manager": location.manager or "",
        "contact": location.contact or "",
        "isActive": location.is_active,
        "type": location.type,
    }


def _transit_location(client_id):
    return stock.system_location(client_id, "IN_TRANSIT")


# ---------------------------------------------------------------------------
# Warehouse stock
# ---------------------------------------------------------------------------
def _reserved_loose(client_id):
    """{(item_id, source location id): qty} reserved by open transfers on
    lines that move loose (non-taka) quantity."""
    from .models import StockTransferLine

    out = defaultdict(lambda: ZERO)
    for line in StockTransferLine.objects.filter(
        client_id=client_id, deleted_at__isnull=True,
        stock_transfer__deleted_at__isnull=True,
        stock_transfer__status__in=("Requested", "Approved", "In Transit", "Partially Received"),
    ).select_related("stock_transfer"):
        on_takas = sum((D(r.get("qty")) for r in line.takas or []), ZERO)
        loose = D(line.qty) - on_takas
        dispatched_loose = D(line.dispatched_qty) - sum(
            (D(r.get("dispatched") or 0) for r in line.takas or []), ZERO
        )
        left = round4(loose - dispatched_loose)
        if left > ZERO:
            out[(line.item_id, line.stock_transfer.from_location_id)] += left
    return out


def _in_transit_to(client_id):
    """{(item_id, destination warehouse id): qty} dispatched, not yet received."""
    from .models import StockTransferLine

    roots = root_map(client_id)
    out = defaultdict(lambda: ZERO)
    for line in StockTransferLine.objects.filter(
        client_id=client_id, deleted_at__isnull=True,
        stock_transfer__deleted_at__isnull=True,
        stock_transfer__status__in=("In Transit", "Partially Received"),
    ).select_related("stock_transfer"):
        left = round4(D(line.dispatched_qty) - D(line.received_qty))
        if left > ZERO:
            to = line.stock_transfer.to_location_id
            out[(line.item_id, roots.get(to, to))] += left
    return out


def stock_rows(client_id, *, warehouse_id=None, item_id=None, search=None, group_by="item",
               low_stock_only=False, ownership=None):
    """Warehouse-wise stock. ``group_by`` is ``item`` or ``batch``.

    physical    on hand at the warehouse (and its legacy child locations)
    reserved    takas reserved for a sales order or a transfer, plus loose
                quantity held by open transfers
    qcPending   takas at the warehouse that have not passed QC
    customer    customer / third-party material held for its owner
    available   physical - reserved - qcPending - customer (OSCAR stock free to use)
    inTransit   on its way into this warehouse
    """
    from apps.masters.models import Item, Location

    from .models import LotTaka, StockBalance, WarehouseStockLevel

    roots = root_map(client_id)
    warehouses = {w.id: w for w in warehouse_queryset(client_id, include_inactive=True)}
    if warehouse_id:
        warehouse_id = get_warehouse(client_id, warehouse_id).id

    def wh_of(location_id):
        root = roots.get(location_id)
        if root not in warehouses:
            return None
        if warehouse_id and root != warehouse_id:
            return None
        return root

    rows = {}
    item_ids = set()

    def row(wh, item, batch=None):
        key = (wh, item, batch)
        if key not in rows:
            rows[key] = {
                "physical": ZERO, "reserved": ZERO, "qcPending": ZERO, "customer": ZERO,
                "inTransit": ZERO, "takas": 0, "batches": set(),
            }
            item_ids.add(item)
        return rows[key]

    # Taka-level figures: everything lot-tracked, by batch if asked.
    lots = LotTaka.objects.filter(
        client_id=client_id, deleted_at__isnull=True, available_quantity__gt=0,
    ).values(
        "item_id", "location_id", "available_quantity", "status", "qc_status",
        "batch__batch_code", "batch__ownership",
    )
    if item_id:
        lots = lots.filter(item_id=item_id)
    lot_qty = defaultdict(lambda: ZERO)
    for lot in lots:
        wh = wh_of(lot["location_id"])
        if wh is None:
            continue
        own = lot["batch__ownership"] or "OSCAR"
        qty = D(lot["available_quantity"])
        lot_qty[(wh, lot["item_id"])] += qty
        if ownership and own != ownership:
            continue
        r = row(wh, lot["item_id"], lot["batch__batch_code"] if group_by == "batch" else None)
        r["physical"] += qty
        r["takas"] += 1
        if lot["batch__batch_code"]:
            r["batches"].add(lot["batch__batch_code"])
        if own != "OSCAR":
            r["customer"] += qty
        elif lot["status"] == "Reserved":
            r["reserved"] += qty
        elif lot["qc_status"] not in SELLABLE_QC:
            r["qcPending"] += qty

    # Item-level balance: covers untracked stock (no takas).
    if ownership in (None, "", "OSCAR"):
        balances = StockBalance.objects.filter(client_id=client_id, deleted_at__isnull=True)
        if item_id:
            balances = balances.filter(item_id=item_id)
        on_hand = defaultdict(lambda: ZERO)
        for b in balances.values("item_id", "location_id", "on_hand"):
            wh = wh_of(b["location_id"])
            if wh is not None:
                on_hand[(wh, b["item_id"])] += D(b["on_hand"])
        loose_reserved = defaultdict(lambda: ZERO)
        for (it, loc), qty in _reserved_loose(client_id).items():
            wh = wh_of(loc)
            if wh is not None:
                loose_reserved[(wh, it)] += qty
        for (wh, it), qty in on_hand.items():
            loose = round4(qty - lot_qty.get((wh, it), ZERO))
            if loose > ZERO or loose_reserved.get((wh, it)):
                r = row(wh, it)
                r["physical"] += max(loose, ZERO)
                r["reserved"] += min(loose_reserved.get((wh, it), ZERO), max(loose, ZERO))
        for (it, wh), qty in _in_transit_to(client_id).items():
            if wh in warehouses and (not warehouse_id or wh == warehouse_id) and (not item_id or str(it) == str(item_id)):
                row(wh, it)["inTransit"] += qty

    items = {i.id: i for i in Item.objects.filter(id__in=item_ids)}
    levels = {
        (lv.location_id, lv.item_id): lv
        for lv in WarehouseStockLevel.objects.filter(client_id=client_id, deleted_at__isnull=True)
    }
    needle = (search or "").strip().lower()
    out = []
    for (wh, it, batch), r in rows.items():
        item = items.get(it)
        if item is None:
            continue
        if needle and needle not in f"{item.sku} {item.name} {batch or ''} {warehouses[wh].name} {warehouses[wh].code}".lower():
            continue
        available = round4(max(r["physical"] - r["reserved"] - r["qcPending"] - r["customer"], ZERO))
        level = levels.get((wh, it))
        min_qty = D(level.min_qty) if level else None
        low = bool(min_qty is not None and group_by == "item" and available < min_qty)
        if low_stock_only and not low:
            continue
        out.append({
            "warehouseId": str(wh), "warehouseCode": warehouses[wh].code,
            "warehouseName": warehouses[wh].name,
            "itemId": str(it), "sku": item.sku, "itemName": item.name, "uom": item.uom,
            "batchCode": batch,
            "physical": str(round4(r["physical"])), "reserved": str(round4(r["reserved"])),
            "qcPending": str(round4(r["qcPending"])), "customerOwned": str(round4(r["customer"])),
            "available": str(available), "inTransit": str(round4(r["inTransit"])),
            "takaCount": r["takas"], "batchCount": len(r["batches"]),
            "minQty": str(round4(min_qty)) if min_qty is not None else None,
            "lowStock": low,
        })
    out.sort(key=lambda x: (x["warehouseName"], x["itemName"] or "", x["batchCode"] or ""))
    return out


def dashboard(client_id):
    from .models import StockTransfer

    rows = stock_rows(client_id)
    by_uom = {}
    for r in rows:
        u = by_uom.setdefault(r["uom"] or "", {"uom": r["uom"] or "", "physical": ZERO, "available": ZERO, "reserved": ZERO})
        for k in ("physical", "available", "reserved"):
            u[k] += D(r[k])
    per_wh = {}
    for r in rows:
        w = per_wh.setdefault(r["warehouseId"], {"items": 0, "lowStock": 0})
        w["items"] += 1
        w["lowStock"] += 1 if r["lowStock"] else 0
    transfers = StockTransfer.objects.filter(client_id=client_id, deleted_at__isnull=True)
    warehouses = list(warehouse_queryset(client_id, include_inactive=True))
    return {
        "totalWarehouses": sum(1 for w in warehouses if w.is_active),
        "inactiveWarehouses": sum(1 for w in warehouses if not w.is_active),
        "byUom": [
            {k: (str(round4(v)) if isinstance(v, Decimal) else v) for k, v in u.items()}
            for u in sorted(by_uom.values(), key=lambda u: u["uom"])
        ],
        "pendingTransfers": transfers.filter(status__in=OPEN_STATUSES).count(),
        "draftTransfers": transfers.filter(status="Draft").count(),
        "inTransitTransfers": transfers.filter(status__in=("In Transit", "Partially Received")).count(),
        "lowStockItems": sum(1 for r in rows if r["lowStock"]),
        "warehouses": [
            {**serialize_warehouse(w), **per_wh.get(str(w.id), {"items": 0, "lowStock": 0})}
            for w in warehouses
        ],
    }


def availability(client_id, item_id, required_qty=None, warehouse_id=None):
    """Free stock of one item in every warehouse, and the shortage at
    ``warehouse_id`` for ``required_qty``."""
    rows = [r for r in stock_rows(client_id, item_id=item_id)]
    required = round4(D(required_qty or 0))
    at = next((r for r in rows if r["warehouseId"] == str(warehouse_id)), None) if warehouse_id else None
    available_here = D(at["available"]) if at else ZERO
    shortage = round4(max(required - available_here, ZERO)) if warehouse_id else ZERO
    others = [r for r in rows if r["warehouseId"] != str(warehouse_id) and D(r["available"]) > ZERO]
    others.sort(key=lambda r: -D(r["available"]))
    return {
        "itemId": str(item_id),
        "required": str(required),
        "warehouseId": str(warehouse_id) if warehouse_id else None,
        "availableHere": str(round4(available_here)),
        "shortage": str(shortage),
        "warehouses": rows,
        "otherWarehouses": others,
        "canCover": bool(shortage > ZERO and sum((D(r["available"]) for r in others), ZERO) >= shortage),
    }


def set_stock_level(client_id, warehouse_id, item, min_qty):
    from .models import WarehouseStockLevel

    warehouse = get_warehouse(client_id, warehouse_id)
    min_qty = round4(D(min_qty))
    if min_qty < ZERO:
        raise ValidationFailed("Minimum stock cannot be negative.", field_errors={"minQty": ["Must be 0 or more."]})
    level = WarehouseStockLevel.objects.filter(
        client_id=client_id, location=warehouse, item=item, deleted_at__isnull=True
    ).first()
    if level is None:
        level = WarehouseStockLevel.objects.create(
            client_id=client_id, location=warehouse, item=item, min_qty=min_qty
        )
    else:
        level.min_qty = min_qty
        level.save(update_fields=["min_qty", "updated_at"])
    return level


def stock_levels(client_id, warehouse_id=None):
    from .models import WarehouseStockLevel

    qs = WarehouseStockLevel.objects.filter(
        client_id=client_id, deleted_at__isnull=True
    ).select_related("location", "item")
    if warehouse_id:
        qs = qs.filter(location_id=warehouse_id)
    return [
        {"id": str(lv.id), "warehouseId": str(lv.location_id), "warehouseName": lv.location.name,
         "itemId": str(lv.item_id), "sku": lv.item.sku, "itemName": lv.item.name,
         "uom": lv.item.uom, "minQty": str(round4(lv.min_qty))}
        for lv in qs.order_by("location__name", "item__name")
    ]


# ---------------------------------------------------------------------------
# Warehouse ledger
# ---------------------------------------------------------------------------
def ledger(client_id, *, warehouse_id=None, item_id=None, batch_code=None,
           date_from=None, date_to=None, limit=500):
    """Movements at warehouses with a running balance per (warehouse, item)."""
    from .models import StockMovement

    roots = root_map(client_id)
    warehouses = {w.id: w for w in warehouse_queryset(client_id, include_inactive=True)}
    location_ids = [loc for loc, root in roots.items() if root in warehouses]
    if warehouse_id:
        get_warehouse(client_id, warehouse_id)
        location_ids = warehouse_location_ids(client_id, warehouse_id)
    qs = StockMovement.objects.filter(
        client_id=client_id, deleted_at__isnull=True, location_id__in=location_ids,
    )
    if item_id:
        qs = qs.filter(item_id=item_id)
    if batch_code:
        qs = qs.filter(lot_taka__batch__batch_code=batch_code)

    opening = defaultdict(lambda: ZERO)
    if date_from:
        for m in qs.filter(movement_date__lt=date_from).values("location_id", "item_id").annotate(q=Sum("quantity")):
            opening[(roots[m["location_id"]], m["item_id"])] += D(m["q"])
        qs = qs.filter(movement_date__gte=date_from)
    if date_to:
        qs = qs.filter(movement_date__lte=date_to)

    qs = qs.select_related(
        "item", "location", "lot_taka__batch__owner_party", "created_by"
    ).order_by("movement_date", "created_at", "id")
    total = qs.count()
    balance = dict(opening)
    entries = []
    for m in qs.iterator():
        wh = roots[m.location_id]
        key = (wh, m.item_id)
        balance[key] = balance.get(key, ZERO) + D(m.quantity)
        batch = m.lot_taka.batch if m.lot_taka_id and m.lot_taka and m.lot_taka.batch_id else None
        user = m.created_by
        entries.append({
            "id": str(m.id), "date": m.movement_date.isoformat(),
            "warehouseId": str(wh), "warehouseName": warehouses[wh].name,
            "itemId": str(m.item_id), "sku": m.item.sku, "itemName": m.item.name, "uom": m.uom or m.item.uom,
            "type": m.type,
            "in": str(round4(m.quantity)) if m.quantity > 0 else "0",
            "out": str(round4(-m.quantity)) if m.quantity < 0 else "0",
            "balance": str(round4(balance[key])),
            "batchCode": batch.batch_code if batch else m.batch_number,
            "takaId": (m.lot_taka.taka_id or m.lot_taka.taka_number) if m.lot_taka_id and m.lot_taka else None,
            "ownership": batch.ownership if batch else "OSCAR",
            "ownerPartyName": batch.owner_party.name if batch and batch.owner_party_id else None,
            "reference": m.reference_number, "referenceType": m.reference_type,
            "user": (user.name or user.email) if user else None,
            "notes": m.notes,
        })
    entries.reverse()  # newest first, balances already computed oldest-first
    return {"count": total, "truncated": total > limit, "results": entries[:limit]}


# ---------------------------------------------------------------------------
# Transfers
# ---------------------------------------------------------------------------
def _lock(transfer):
    from .models import StockTransfer

    return StockTransfer.objects.select_for_update().get(pk=transfer.pk)


def _lines(transfer):
    return list(transfer.items.filter(deleted_at__isnull=True).select_related("item").order_by("created_at", "id"))


def _need_status(transfer, allowed, action):
    if transfer.status not in allowed:
        raise Conflict(
            f"Cannot {action} a transfer that is {transfer.status}.", code="BAD_TARGET"
        )


def _common(transfer, user, notes=None):
    return dict(
        client_id=transfer.client_id,
        reference_type="StockTransfer",
        reference_id=transfer.id,
        reference_number=transfer.transfer_number,
        source_document_type="StockTransfer",
        source_document_id=transfer.id,
        movement_date=timezone.localdate(),
        user=user,
        notes=notes,
    )


def validate_warehouses(client_id, from_location, to_location):
    """Both ends must be active warehouses (or legacy child locations of one)."""
    roots = root_map(client_id)
    for loc, field in ((from_location, "sourceLocationId"), (to_location, "destLocationId")):
        if loc.id not in roots:
            raise ValidationFailed(
                f"{loc.name} is a system store, not a warehouse.",
                field_errors={field: ["Pick a warehouse."]},
            )
        if not loc.is_active:
            raise ValidationFailed(f"{loc.name} is inactive.", field_errors={field: ["Inactive warehouse."]})
    if roots[from_location.id] == roots[to_location.id]:
        raise ValidationFailed(
            "Source and destination are the same warehouse.",
            field_errors={"destLocationId": ["Pick a different warehouse."]},
        )


@transaction.atomic
def reserve(transfer):
    """Draft -> Requested: hold the stock at the source."""
    from .models import LotTaka

    transfer = _lock(transfer)
    _need_status(transfer, ("Draft",), "request")
    reserved_loose = _reserved_loose(transfer.client_id)
    for line in _lines(transfer):
        rows = []
        for raw in line.takas or []:
            lot = LotTaka.objects.select_for_update(of=("self",)).filter(
                client_id=transfer.client_id, pk=raw["lotTakaId"], deleted_at__isnull=True
            ).first()
            label = (lot.taka_id or lot.taka_number) if lot else raw["lotTakaId"]
            if lot is None or str(lot.location_id) != str(transfer.from_location_id):
                raise BusinessRuleViolation(f"Taka {label} is no longer at the source warehouse.", code="TAKA_MOVED")
            if lot.status == "Reserved":
                raise BusinessRuleViolation(
                    f"Taka {label} is already reserved for an order or another transfer.", code="TAKA_RESERVED"
                )
            if lot.status not in FREE_STATUSES:
                raise BusinessRuleViolation(f"Taka {label} is {lot.status}; it cannot be transferred.", code="TAKA_NOT_FREE")
            qty = round4(D(raw["qty"]))
            if qty > D(lot.available_quantity):
                raise BusinessRuleViolation(
                    f"Taka {label} has only {round4(lot.available_quantity)} left.", code="INSUFFICIENT_STOCK"
                )
            rows.append({"lotTakaId": str(lot.id), "qty": str(qty), "dispatched": "0", "priorStatus": lot.status})
            lot.status = "Reserved"
            lot.save(update_fields=["status", "updated_at"])
        loose = round4(D(line.qty) - sum((D(r["qty"]) for r in rows), ZERO))
        if loose > ZERO:
            on_hand = D(stock.balances_for(transfer.client_id, [line.item_id], transfer.from_location_id)
                        .get(line.item_id, {}).get("on_hand") or ZERO)
            on_takas = D(LotTaka.objects.filter(
                client_id=transfer.client_id, item_id=line.item_id, location_id=transfer.from_location_id,
                deleted_at__isnull=True, available_quantity__gt=0,
            ).aggregate(q=Sum("available_quantity"))["q"] or ZERO)
            free = on_hand - on_takas - reserved_loose.get((line.item_id, transfer.from_location_id), ZERO)
            if loose > free + TOLERANCE:
                raise BusinessRuleViolation(
                    f"Only {round4(max(free, ZERO))} {line.item.uom or ''} of {line.item.name} is free "
                    f"at {transfer.from_location.name}; {loose} requested.",
                    code="INSUFFICIENT_STOCK",
                )
            reserved_loose[(line.item_id, transfer.from_location_id)] += loose
        line.takas = rows
        line.save(update_fields=["takas", "updated_at"])
    transfer.status = "Requested"
    transfer.save(update_fields=["status", "updated_at"])
    return transfer


@transaction.atomic
def create_transfer(*, client, user, from_location, to_location, transfer_date, lines,
                    notes=None, shipped_by=None, draft=False, reference=None):
    """``lines``: ``[{"item": Item, "qty": Decimal, "takas": [{"lotTakaId", "qty"}]}]``
    already validated against the source (see the viewset)."""
    from .models import StockTransfer, StockTransferLine

    validate_warehouses(client.id, from_location, to_location)
    if not lines:
        raise ValidationFailed("A transfer needs at least one item.", field_errors={"items": ["Add at least one line."]})
    reference = reference or {}
    transfer = StockTransfer.objects.create(
        client=client, transfer_number=allocate_number(client, "TR", transfer_date),
        from_location=from_location, to_location=to_location, transfer_date=transfer_date,
        status="Draft", requested_by=user, notes=notes, shipped_by=shipped_by,
        reference_type=reference.get("type"), reference_id=reference.get("id"),
        reference_number=reference.get("number"), created_by=user,
    )
    StockTransferLine.objects.bulk_create([
        StockTransferLine(
            client=client, stock_transfer=transfer, item=line["item"], qty=round4(D(line["qty"])),
            takas=[{"lotTakaId": str(r["lotTakaId"]), "qty": str(round4(D(r["qty"])))} for r in line.get("takas") or []],
        )
        for line in lines
    ])
    if not draft:
        transfer = reserve(transfer)
    return transfer


@transaction.atomic
def approve(transfer, user):
    transfer = _lock(transfer)
    _need_status(transfer, ("Requested",), "approve")
    transfer.status = "Approved"
    transfer.approved_by = user
    transfer.approved_at = timezone.now()
    transfer.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
    return transfer


def _restore(lot_id, status):
    from .models import LotTaka

    LotTaka.objects.filter(pk=lot_id, status="Reserved", deleted_at__isnull=True).update(
        status=status or "Available", updated_at=timezone.now()
    )


@transaction.atomic
def dispatch(transfer, user, lines=None, shipped_by=None):
    """Move (part of) the reserved stock into the in-transit store.

    ``lines``: ``[{"lineId", "qty"}]``; omitted lines are not dispatched now.
    No ``lines`` at all dispatches everything still waiting.
    """
    transfer = _lock(transfer)
    _need_status(transfer, ("Approved", "In Transit", "Partially Received"), "dispatch")
    transit = _transit_location(transfer.client_id)
    wanted = None
    if lines:
        wanted = {str(r["lineId"]): round4(D(r.get("qty") or 0)) for r in lines}
    common = _common(transfer, user, notes=f"Dispatched to {transfer.to_location.name}")
    moved_any = False
    for line in _lines(transfer):
        remaining = round4(D(line.qty) - D(line.dispatched_qty))
        qty = remaining if wanted is None else wanted.get(str(line.id), ZERO)
        if qty <= ZERO:
            continue
        if qty > remaining + TOLERANCE:
            raise ValidationFailed(
                f"{line.item.name}: only {remaining} is left to dispatch.",
                field_errors={"lines": [f"At most {remaining} for {line.item.name}."]},
            )
        left = qty
        rows = [dict(r) for r in line.takas or []]
        in_transit = list(line.in_transit or [])
        for r in rows:
            open_qty = round4(D(r["qty"]) - D(r.get("dispatched") or 0))
            if left <= ZERO or open_qty <= ZERO:
                continue
            take = min(open_qty, left)
            holder, _ = stock.relocate_lot(
                lot=r["lotTakaId"], quantity=take, to_location_id=transit.id,
                out_type="TRANSFER_OUT", in_type="TRANSFER_IN", split_suffix="TR", **common,
            )
            if holder.status != "Reserved":
                holder.status = "Reserved"
                holder.save(update_fields=["status", "updated_at"])
            in_transit.append({
                "lotTakaId": str(holder.id), "sourceTakaId": r["lotTakaId"], "qty": str(take),
                "received": "0", "priorStatus": r.get("priorStatus") or "Available",
            })
            r["dispatched"] = str(round4(D(r.get("dispatched") or 0) + take))
            if D(r["dispatched"]) >= D(r["qty"]) and str(holder.id) != r["lotTakaId"]:
                # The rest of a partly moved taka is no longer held.
                _restore(r["lotTakaId"], r.get("priorStatus"))
            left -= take
        if left > ZERO:
            stock.relocate_item_qty(
                item=line.item, quantity=left, from_location_id=transfer.from_location_id,
                to_location_id=transit.id, out_type="TRANSFER_OUT", in_type="TRANSFER_IN",
                unit_cost=line.item.cost_price, **common,
            )
        line.takas = rows
        line.in_transit = in_transit
        line.dispatched_qty = round4(D(line.dispatched_qty) + qty)
        line.save(update_fields=["takas", "in_transit", "dispatched_qty", "updated_at"])
        moved_any = True
    if not moved_any:
        raise ValidationFailed("Nothing to dispatch.", field_errors={"lines": ["Enter a quantity to dispatch."]})
    if transfer.status == "Approved":
        transfer.status = "In Transit"
    transfer.dispatched_at = timezone.now()
    if shipped_by:
        transfer.shipped_by = shipped_by
    transfer.save(update_fields=["status", "dispatched_at", "shipped_by", "updated_at"])
    return transfer


@transaction.atomic
def receive(transfer, user, lines=None):
    """Move dispatched stock from the in-transit store into the destination.

    ``lines``: ``[{"lineId", "qty"?, "takas": [{"lotTakaId", "qty"}]?}]``.
    Taka lines name the in-transit takas received (default: oldest first up
    to ``qty``); loose lines take ``qty``. No ``lines`` receives everything in
    transit.
    """
    transfer = _lock(transfer)
    _need_status(transfer, ("In Transit", "Partially Received"), "receive")
    transit = _transit_location(transfer.client_id)
    common = _common(transfer, user, notes=f"Received from {transfer.from_location.name}")
    wanted = {str(r["lineId"]): r for r in lines} if lines else None
    received_any = False
    for line in _lines(transfer):
        waiting = round4(D(line.dispatched_qty) - D(line.received_qty))
        if waiting <= ZERO:
            continue
        spec = None if wanted is None else wanted.get(str(line.id))
        if wanted is not None and spec is None:
            continue
        rows = [dict(r) for r in line.in_transit or []]
        picks = {}
        if spec and spec.get("takas"):
            for t in spec["takas"]:
                picks[str(t["lotTakaId"])] = round4(D(t["qty"]))
            unknown = set(picks) - {r["lotTakaId"] for r in rows}
            if unknown:
                raise ValidationFailed("A taka is not in transit on this line.", field_errors={"takas": ["Unknown taka."]})
            qty = sum(picks.values(), ZERO)
        else:
            qty = waiting if spec is None or spec.get("qty") in (None, "") else round4(D(spec["qty"]))
        if qty <= ZERO:
            continue
        if qty > waiting + TOLERANCE:
            raise ValidationFailed(
                f"{line.item.name}: only {waiting} is in transit.",
                field_errors={"lines": [f"At most {waiting} for {line.item.name}."]},
            )
        left = qty
        new_rows = []
        for r in rows:
            open_qty = round4(D(r["qty"]) - D(r.get("received") or 0))
            take = picks.get(r["lotTakaId"], ZERO) if picks else min(open_qty, left)
            if take > open_qty + TOLERANCE:
                raise ValidationFailed("More than is in transit on a taka.", field_errors={"takas": ["Too much."]})
            if take > ZERO:
                holder, _ = stock.relocate_lot(
                    lot=r["lotTakaId"], quantity=take, to_location_id=transfer.to_location_id,
                    out_type="TRANSFER_OUT", in_type="TRANSFER_IN", split_suffix="TR", **common,
                )
                holder.status = r.get("priorStatus") or "Available"
                holder.save(update_fields=["status", "updated_at"])
                r["received"] = str(round4(D(r.get("received") or 0) + take))
                r.setdefault("receivedTakaIds", []).append(str(holder.id))
                left -= take
            new_rows.append(r)
        if left > TOLERANCE and not picks:
            stock.relocate_item_qty(
                item=line.item, quantity=left, from_location_id=transit.id,
                to_location_id=transfer.to_location_id, out_type="TRANSFER_OUT", in_type="TRANSFER_IN",
                unit_cost=line.item.cost_price, **common,
            )
        line.in_transit = new_rows
        line.received_qty = round4(D(line.received_qty) + qty)
        line.save(update_fields=["in_transit", "received_qty", "updated_at"])
        received_any = True
    if not received_any:
        raise ValidationFailed("Nothing to receive.", field_errors={"lines": ["Enter a quantity received."]})
    all_lines = _lines(transfer)
    done = all(D(l.received_qty) >= D(l.qty) - TOLERANCE for l in all_lines)
    transfer.status = "Received" if done else "Partially Received"
    transfer.received_by = user
    transfer.received_at = timezone.now()
    transfer.save(update_fields=["status", "received_by", "received_at", "updated_at"])
    return transfer


def _release_undispatched(transfer, line):
    for r in line.takas or []:
        if D(r.get("dispatched") or 0) < D(r["qty"]):
            _restore(r["lotTakaId"], r.get("priorStatus"))


@transaction.atomic
def complete(transfer, user, settlements=None):
    """Close the transfer. Stock still in transit on a line needs a
    settlement ``{"lineId", "action": "RETURN"|"WRITE_OFF", "reason"}``: RETURN
    sends it back to the source, WRITE_OFF (``adjust_stock``) books it as a
    transit loss. Undispatched reservations are released."""
    from apps.accounting.services import post_stock_adjustment

    from .models import LotTaka

    transfer = _lock(transfer)
    _need_status(transfer, ("Received", "Partially Received", "In Transit"), "complete")
    if transfer.status == "In Transit" and not any(D(l.received_qty) > 0 for l in _lines(transfer)):
        raise Conflict("Nothing has been received yet: receive it, or cancel the transfer.", code="BAD_TARGET")
    settle = {str(s["lineId"]): s for s in settlements or []}
    transit = _transit_location(transfer.client_id)
    for line in _lines(transfer):
        waiting = round4(D(line.dispatched_qty) - D(line.received_qty))
        if waiting > ZERO:
            s = settle.get(str(line.id))
            action = (s or {}).get("action")
            reason = ((s or {}).get("reason") or "").strip()
            if action not in ("RETURN", "WRITE_OFF") or not reason:
                raise ValidationFailed(
                    f"{line.item.name}: {waiting} {line.item.uom or ''} was dispatched but not received. "
                    "Choose return to source or write-off, with a reason.",
                    field_errors={"settlements": [f"Settle {line.item.name}."]},
                    payload={"lineId": str(line.id), "waiting": str(waiting)},
                )
            if action == "WRITE_OFF":
                from apps.core.permissions import require_permission

                require_permission(user, "adjust_stock", "Writing off stock lost in transit needs Adjust stock.")
            note = f"Transfer discrepancy ({'returned' if action == 'RETURN' else 'written off'}): {reason}"
            common = _common(transfer, user, notes=note)
            rows = [dict(r) for r in line.in_transit or []]
            left = waiting
            for r in rows:
                open_qty = round4(D(r["qty"]) - D(r.get("received") or 0))
                if open_qty <= ZERO:
                    continue
                left -= open_qty
                if action == "RETURN":
                    holder, _ = stock.relocate_lot(
                        lot=r["lotTakaId"], quantity=open_qty, to_location_id=transfer.from_location_id,
                        out_type="TRANSFER_OUT", in_type="TRANSFER_IN", split_suffix="TR", **common,
                    )
                    holder.status = r.get("priorStatus") or "Available"
                    holder.save(update_fields=["status", "updated_at"])
                else:
                    lot = LotTaka.objects.get(pk=r["lotTakaId"])
                    movement = stock.post_movement(
                        location=transit.id, item=lot.item_id, type="ADJUSTMENT", quantity=-open_qty,
                        lot_taka=lot, unit_cost=D(lot.rate) if lot.rate else line.item.cost_price,
                        **common,
                    )
                    post_stock_adjustment(movement, user=user)
                r["settled"] = action
            if left > TOLERANCE:
                if action == "RETURN":
                    stock.relocate_item_qty(
                        item=line.item, quantity=left, from_location_id=transit.id,
                        to_location_id=transfer.from_location_id, out_type="TRANSFER_OUT",
                        in_type="TRANSFER_IN", unit_cost=line.item.cost_price, **common,
                    )
                else:
                    movement = stock.post_movement(
                        location=transit.id, item=line.item, type="ADJUSTMENT", quantity=-left,
                        unit_cost=line.item.cost_price, **common,
                    )
                    post_stock_adjustment(movement, user=user)
            line.in_transit = rows
            line.discrepancy_qty = waiting
            line.discrepancy_reason = reason
            line.discrepancy_action = "RETURNED" if action == "RETURN" else "WRITTEN_OFF"
            line.save(update_fields=["in_transit", "discrepancy_qty", "discrepancy_reason",
                                     "discrepancy_action", "updated_at"])
        _release_undispatched(transfer, line)
    transfer.status = "Completed"
    transfer.completed_at = timezone.now()
    transfer.save(update_fields=["status", "completed_at", "updated_at"])
    return transfer


@transaction.atomic
def cancel(transfer, user, reason=None):
    """Only before anything is received: reverses the dispatch movements (back
    to the source) and releases every reservation."""
    from .models import LotTaka

    transfer = _lock(transfer)
    _need_status(transfer, ("Draft", "Requested", "Approved", "In Transit"), "cancel")
    lines = _lines(transfer)
    if any(D(l.received_qty) > ZERO for l in lines):
        raise Conflict(
            "Part of this transfer was received; complete it and return the rest instead.", code="BAD_TARGET"
        )
    if any(D(l.dispatched_qty) > ZERO for l in lines):
        stock.reverse_movements(
            reference_type="StockTransfer", reference_id=transfer.id,
            client_id=transfer.client_id, user=user, notes=reason or "Transfer cancelled",
        )
        LotTaka.objects.filter(
            client_id=transfer.client_id, source_document_type="StockTransfer",
            source_document_id=transfer.id, available_quantity=0, deleted_at__isnull=True,
        ).update(deleted_at=timezone.now(), status="Closed", updated_at=timezone.now())
    for line in lines:
        for r in line.takas or []:
            _restore(r["lotTakaId"], r.get("priorStatus"))
    transfer.status = "Cancelled"
    transfer.save(update_fields=["status", "updated_at"])
    return transfer


def pick_takas(client_id, item, qty, warehouse_id):
    """Oldest free, QC-passed, OSCAR-owned takas at the warehouse for ``qty``."""
    from .batch_services import BULK_TAG
    from .models import LotTaka

    locations = warehouse_location_ids(client_id, warehouse_id)
    picks, left = [], round4(D(qty))
    for lot in LotTaka.objects.filter(
        client_id=client_id, item=item, location_id__in=locations, deleted_at__isnull=True,
        available_quantity__gt=0, status__in=FREE_STATUSES, qc_status__in=SELLABLE_QC,
    ).filter(Q(batch__isnull=True) | Q(batch__ownership="OSCAR")).order_by("received_date", "created_at"):
        if left <= ZERO:
            break
        if BULK_TAG in (lot.tags or []):
            continue
        take = min(D(lot.available_quantity), left)
        picks.append((lot, round4(take)))
        left -= take
    return picks, round4(max(left, ZERO))


@transaction.atomic
def shortage_transfer(*, client, user, item, qty, from_warehouse_id, to_warehouse_id, reference=None, notes=None):
    """Raise a Requested transfer covering a shortage at ``to_warehouse_id``.

    The takas come from one source location (a transfer has one source), so
    the oldest stock of that warehouse's main location is used first.
    """
    from apps.masters.models import Location

    source = get_warehouse(client.id, from_warehouse_id, "fromWarehouseId")
    dest = get_warehouse(client.id, to_warehouse_id, "toWarehouseId")
    qty = round4(D(qty))
    if qty <= ZERO:
        raise ValidationFailed("Quantity must be greater than zero.", field_errors={"qty": ["Must be > 0."]})
    picks, short = pick_takas(client.id, item, qty, source.id)
    by_location = defaultdict(list)
    for lot, take in picks:
        by_location[lot.location_id].append({"lotTakaId": lot.id, "qty": take})
    if item.tracking_type in ("LOT", "TAKA") or picks:
        if short > ZERO or len(by_location) > 1:
            available = sum((t for _, t in picks), ZERO)
            if short > ZERO:
                raise BusinessRuleViolation(
                    f"{source.name} has only {available} {item.uom or ''} of {item.name} free; {qty} needed.",
                    code="INSUFFICIENT_STOCK",
                )
            raise BusinessRuleViolation(
                f"The stock at {source.name} is spread over several old sub-locations; raise the transfer manually.",
                code="SPLIT_SOURCE",
            )
        location_id, takas = next(iter(by_location.items()))
    else:
        location_id, takas = source.id, []
    return create_transfer(
        client=client, user=user, from_location=Location.objects.get(pk=location_id), to_location=dest,
        transfer_date=timezone.localdate(), lines=[{"item": item, "qty": qty, "takas": takas}],
        notes=notes or f"Shortage for {reference.get('number') if reference else item.name}",
        reference=reference,
    )


def serialize_transfer_extra(transfer):
    """Per-line dispatch / transit / receipt figures for the transfer screen."""
    from .models import LotTaka

    lines = []
    ids = set()
    for line in transfer.items.filter(deleted_at__isnull=True):
        for r in (line.takas or []) + (line.in_transit or []):
            ids.add(r["lotTakaId"])
    labels = {
        str(l.id): (l.taka_id or l.taka_number, l.batch.batch_code if l.batch_id else None)
        for l in LotTaka.objects.filter(pk__in=ids).select_related("batch")
    }
    for line in transfer.items.filter(deleted_at__isnull=True).select_related("item").order_by("created_at", "id"):
        def label(rows):
            return [{**r, "takaId": labels.get(r["lotTakaId"], (None, None))[0],
                     "batchCode": labels.get(r["lotTakaId"], (None, None))[1]} for r in rows or []]
        lines.append({
            "id": str(line.id), "itemId": str(line.item_id), "sku": line.item.sku, "name": line.item.name,
            "uom": line.item.uom, "qty": str(round4(line.qty)),
            "dispatchedQty": str(round4(line.dispatched_qty)), "receivedQty": str(round4(line.received_qty)),
            "inTransitQty": str(round4(max(D(line.dispatched_qty) - D(line.received_qty) - (D(line.discrepancy_qty) if line.discrepancy_action else ZERO), ZERO))),
            "pendingDispatchQty": str(round4(max(D(line.qty) - D(line.dispatched_qty), ZERO))),
            "discrepancyQty": str(round4(line.discrepancy_qty)), "discrepancyReason": line.discrepancy_reason,
            "discrepancyAction": line.discrepancy_action,
            "takas": label(line.takas), "inTransit": label(line.in_transit),
        })
    return lines
