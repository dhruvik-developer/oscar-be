"""
Oscar ERP — PMS, Inventory & Purchase Bridge Service.

Provides real-time synchronized touchpoints between:
1. PMS Loom Weaving Engine / Production Instructions
2. Inventory Ledger (Stock Movements, Balances, Batches, LotTakas)
3. Purchase Module (POs, Goods Receipts, 3-Way Matching)
"""
import logging
from decimal import Decimal
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.text import slugify

from apps.core.money import ZERO, D, round4
from apps.masters.models import Item, Location, ItemCategory, Party, FabricQuality

logger = logging.getLogger(__name__)


def sync_yarn_master_to_inventory(yarn_master, category_id=None):
    """
    Synchronizes PMS YarnMaster with masters.Item & masters.ItemCategory.
    Updates or creates Item so yarn specs, rates, and category are matched.
    """
    if not yarn_master:
        return None
    client_id = yarn_master.client_id
    category = None
    if category_id:
        category = ItemCategory.objects.filter(client_id=client_id, id=category_id, deleted_at__isnull=True).first()
    if not category and yarn_master.category_id:
        category = yarn_master.category

    if not category:
        category = ItemCategory.objects.filter(client_id=client_id, code="YARN-RAW", deleted_at__isnull=True).first()
        if not category:
            category, _ = ItemCategory.objects.get_or_create(
                client_id=client_id,
                code="YARN-RAW",
                defaults={
                    "name": "Yarn Raw Material",
                    "kind": "stock",
                    "description": "Raw yarn for weaving and loom production",
                },
            )

    yarn_master.category = category

    item = yarn_master.item
    if not item:
        item = Item.objects.filter(client_id=client_id, name__iexact=yarn_master.name, deleted_at__isnull=True).first()

    if item:
        item.category = category
        item.cost_price = yarn_master.rate or ZERO
        item.selling_price = yarn_master.rate or ZERO
        item.save(update_fields=["category", "cost_price", "selling_price", "updated_at"])
    else:
        safe_slug = slugify(yarn_master.name).upper().replace("-", "")[:12]
        sku = f"YARN-{safe_slug or 'SPEC'}-{str(yarn_master.id)[:4].upper()}"
        item = Item.objects.create(
            client_id=client_id,
            sku=sku,
            name=yarn_master.name,
            item_kind="Yarn",
            category=category,
            uom="kg",
            purchase_unit="kg",
            sales_unit="kg",
            tracking_type="NONE",
            tracking_mode="Quantity",
            cost_price=yarn_master.rate or ZERO,
            selling_price=yarn_master.rate or ZERO,
            theoretical_weight=Decimal("1.0000"),
            weight_unit="kg",
        )

    yarn_master.item = item
    yarn_master.save(update_fields=["item", "category", "updated_at"])
    return item


def sync_loom_quality_to_inventory(loom_quality, category_id=None, fabric_quality_id=None):
    """
    Synchronizes PMS LoomQuality with:
    1. masters.FabricQuality (Width, GSM, Reed, PPI, Warp & Weft counts)
    2. masters.ItemCategory (Grey Fabric category)
    3. masters.Item (Grey Fabric Item in inventory with TAKA tracking)
    """
    if not loom_quality:
        return None
    client_id = loom_quality.client_id

    # 1. Sync / link Category
    category = None
    if category_id:
        category = ItemCategory.objects.filter(client_id=client_id, id=category_id, deleted_at__isnull=True).first()
    if not category and loom_quality.category_id:
        category = loom_quality.category
    if not category:
        category = ItemCategory.objects.filter(client_id=client_id, code="GREY-FABRIC", deleted_at__isnull=True).first()
        if not category:
            category, _ = ItemCategory.objects.get_or_create(
                client_id=client_id,
                code="GREY-FABRIC",
                defaults={
                    "name": "Grey Fabric",
                    "kind": "stock",
                    "description": "Off-loom grey / greige fabric",
                },
            )
    loom_quality.category = category

    # 2. Sync / link masters.FabricQuality
    fq = None
    if fabric_quality_id:
        fq = FabricQuality.objects.filter(client_id=client_id, id=fabric_quality_id, deleted_at__isnull=True).first()
    if not fq and loom_quality.fabric_quality_id:
        fq = loom_quality.fabric_quality
    if not fq:
        fq = FabricQuality.objects.filter(client_id=client_id, name__iexact=loom_quality.name, deleted_at__isnull=True).first()

    safe_slug = slugify(loom_quality.name).upper().replace("-", "")[:12]
    if fq:
        fq.width = loom_quality.reed_w
        fq.gsm = Decimal(str(round(loom_quality.gsm, 2))) if loom_quality.gsm else None
        fq.reed = loom_quality.reed
        fq.pick = int(loom_quality.ppi)
        if loom_quality.warp_yarn:
            fq.warp_count = loom_quality.warp_yarn.name
        if loom_quality.weft_yarn:
            fq.weft_count = loom_quality.weft_yarn.name
        fq.save(update_fields=["width", "gsm", "reed", "pick", "warp_count", "weft_count", "updated_at"])
    else:
        fq = FabricQuality.objects.create(
            client_id=client_id,
            code=f"FQ-{safe_slug}-{str(loom_quality.id)[:4].upper()}",
            name=loom_quality.name,
            width=loom_quality.reed_w,
            gsm=Decimal(str(round(loom_quality.gsm, 2))) if loom_quality.gsm else None,
            reed=loom_quality.reed,
            pick=int(loom_quality.ppi),
            warp_count=loom_quality.warp_yarn.name if loom_quality.warp_yarn else "",
            weft_count=loom_quality.weft_yarn.name if loom_quality.weft_yarn else "",
        )
    loom_quality.fabric_quality = fq

    # 3. Sync / link masters.Item
    item = loom_quality.item
    if not item:
        item = Item.objects.filter(client_id=client_id, fabric_quality__iexact=loom_quality.name, deleted_at__isnull=True).first()
    if not item:
        item = Item.objects.filter(client_id=client_id, name__iexact=f"Grey Fabric {loom_quality.name}", deleted_at__isnull=True).first()

    cost = Decimal(str(round(loom_quality.total_cost, 2))) if loom_quality.total_cost else ZERO
    if item:
        item.category = category
        item.fabric_quality = loom_quality.name
        item.fabric_width = loom_quality.reed_w
        item.fabric_gsm = Decimal(str(round(loom_quality.gsm, 2))) if loom_quality.gsm else None
        item.cost_price = cost
        item.selling_price = round4(cost * Decimal("1.15")) if cost > ZERO else ZERO
        item.save(update_fields=["category", "fabric_quality", "fabric_width", "fabric_gsm", "cost_price", "selling_price", "updated_at"])
    else:
        sku = f"GREY-{safe_slug or 'SPEC'}-{str(loom_quality.id)[:4].upper()}"
        item = Item.objects.create(
            client_id=client_id,
            sku=sku,
            name=f"Grey Fabric {loom_quality.name}",
            fabric_quality=loom_quality.name,
            item_kind="Fabric",
            category=category,
            uom="Meter",
            purchase_unit="Meter",
            sales_unit="Meter",
            tracking_type="TAKA",
            tracking_mode="Quantity",
            fabric_width=loom_quality.reed_w,
            fabric_gsm=Decimal(str(round(loom_quality.gsm, 2))) if loom_quality.gsm else None,
            cost_price=cost,
            selling_price=round4(cost * Decimal("1.15")) if cost > ZERO else ZERO,
        )

    loom_quality.item = item
    loom_quality.save(update_fields=["item", "category", "fabric_quality", "updated_at"])
    return fq, item


def ensure_yarn_item(yarn_master, client_id=None):
    """
    Ensure a masters.Item exists for a YarnMaster so inventory stock movements
    can be posted against the general inventory ledger.
    """
    if not yarn_master:
        return None
    client_id = client_id or yarn_master.client_id
    if getattr(yarn_master, "item_id", None) and yarn_master.item:
        return yarn_master.item

    # Find existing item by name
    item = Item.objects.filter(
        client_id=client_id,
        name__iexact=yarn_master.name,
        deleted_at__isnull=True,
    ).first()

    if not item:
        category = ItemCategory.objects.filter(
            client_id=client_id,
            code="YARN-RAW",
            deleted_at__isnull=True,
        ).first()
        if not category:
            category, _ = ItemCategory.objects.get_or_create(
                client_id=client_id,
                code="YARN-RAW",
                defaults={
                    "name": "Yarn Raw Material",
                    "kind": "stock",
                    "description": "Raw yarn for weaving and loom production",
                },
            )
        safe_slug = slugify(yarn_master.name).upper().replace("-", "")[:12]
        sku = f"YARN-{safe_slug or 'SPEC'}-{str(yarn_master.id)[:4].upper()}"
        item = Item.objects.create(
            client_id=client_id,
            sku=sku,
            name=yarn_master.name,
            item_kind="Yarn",
            category=category,
            uom="kg",
            purchase_unit="kg",
            sales_unit="kg",
            tracking_type="NONE",
            tracking_mode="Quantity",
            cost_price=yarn_master.rate or ZERO,
            selling_price=yarn_master.rate or ZERO,
            theoretical_weight=Decimal("1.0000"),
            weight_unit="kg",
        )

    if hasattr(yarn_master, "item_id") and not yarn_master.item_id:
        yarn_master.item = item
        yarn_master.save(update_fields=["item", "updated_at"])

    return item


def ensure_fabric_item(loom_quality, client_id=None):
    """
    Ensure a masters.Item exists for a LoomQuality so grey fabric rolls
    can be received into the warehouse inventory ledger.
    """
    if not loom_quality:
        return None
    client_id = client_id or loom_quality.client_id
    if getattr(loom_quality, "item_id", None) and loom_quality.item:
        return loom_quality.item

    item = Item.objects.filter(
        client_id=client_id,
        deleted_at__isnull=True,
    ).filter(
        fabric_quality__iexact=loom_quality.name
    ).first()

    if not item:
        item = Item.objects.filter(
            client_id=client_id,
            name__iexact=f"Grey Fabric {loom_quality.name}",
            deleted_at__isnull=True,
        ).first()

    if not item:
        category = ItemCategory.objects.filter(
            client_id=client_id,
            code="GREY-FABRIC",
            deleted_at__isnull=True,
        ).first()
        if not category:
            category, _ = ItemCategory.objects.get_or_create(
                client_id=client_id,
                code="GREY-FABRIC",
                defaults={
                    "name": "Grey Fabric",
                    "kind": "stock",
                    "description": "Off-loom grey / greige fabric",
                },
            )
        safe_slug = slugify(loom_quality.name).upper().replace("-", "")[:12]
        sku = f"GREY-{safe_slug or 'SPEC'}-{str(loom_quality.id)[:4].upper()}"
        cost = Decimal(str(round(loom_quality.total_cost, 2))) if loom_quality.total_cost else ZERO
        item = Item.objects.create(
            client_id=client_id,
            sku=sku,
            name=f"Grey Fabric {loom_quality.name}",
            fabric_quality=loom_quality.name,
            item_kind="Fabric",
            category=category,
            uom="Meter",
            purchase_unit="Meter",
            sales_unit="Meter",
            tracking_type="TAKA",
            tracking_mode="Quantity",
            fabric_width=loom_quality.reed_w,
            fabric_gsm=Decimal(str(round(loom_quality.gsm, 2))) if loom_quality.gsm else None,
            cost_price=cost,
            selling_price=round4(cost * Decimal("1.15")) if cost > ZERO else ZERO,
        )

    if not loom_quality.item_id:
        loom_quality.item = item
        loom_quality.save(update_fields=["item", "updated_at"])

    return item


def ensure_factory_location(factory, client_id=None):
    """Ensure a masters.Location exists for a LoomFactory shed floor."""
    if not factory:
        return None
    client_id = client_id or factory.client_id
    if factory.warehouse_id and factory.warehouse:
        return factory.warehouse

    code = f"SHED-{str(factory.id)[:6].upper()}"
    loc = Location.objects.filter(
        client_id=client_id,
        code=code,
        deleted_at__isnull=True,
    ).first()
    if not loc:
        loc = Location.objects.filter(
            client_id=client_id,
            name__iexact=f"{factory.name} Shed Godown",
            deleted_at__isnull=True,
        ).first()

    if not loc:
        loc = Location.objects.create(
            client_id=client_id,
            code=code,
            name=f"{factory.name} Shed Godown",
            type="Warehouse",
            description=f"On-site yarn & loom floor store for {factory.name}",
            is_active=True,
        )

    factory.warehouse = loc
    factory.save(update_fields=["warehouse", "updated_at"])
    return loc


def ensure_central_godown(client_id):
    """Find or create default central godown for finished/grey fabric."""
    loc = Location.objects.filter(
        client_id=client_id,
        deleted_at__isnull=True,
    ).filter(type__in=["Warehouse", "Godown"]).first()

    if not loc:
        loc = Location.objects.create(
            client_id=client_id,
            code="CENTRAL-GDN",
            name="Central Grey Godown",
            type="Godown",
            description="Main grey fabric warehouse",
            is_active=True,
        )
    return loc


def sync_yarn_delivery_receipt_to_inventory(delivery, user=None):
    """
    Bridge 1: When Yarn Delivery is received in PMS:
    1. Posts inward StockMovement (type=PURCHASE) into factory shed godown.
    2. Updates stock_balances table.
    3. If delivery has linked purchase_order, generates/links a GoodsReceipt in Purchase.
    """
    from apps.inventory.services import post_movement
    from apps.inventory.models import StockMovement

    if not delivery or delivery.status != "Received":
        return None

    client_id = delivery.client_id
    factory = delivery.factory
    yarn = delivery.booking.yarn
    recd_kgs = D(str(delivery.recd_kgs or delivery.kgs or 0))
    if recd_kgs <= ZERO:
        return None

    item = ensure_yarn_item(yarn, client_id)
    location = ensure_factory_location(factory, client_id)

    # Check if already posted for this delivery
    existing_movement = StockMovement.objects.filter(
        client_id=client_id,
        reference_type="YarnDelivery",
        reference_id=delivery.id,
        deleted_at__isnull=True,
    ).first()

    if existing_movement:
        return existing_movement

    movement = post_movement(
        client_id=client_id,
        item=item,
        location=location,
        type="PURCHASE",
        quantity=round4(recd_kgs),
        unit_cost=round4(D(str(delivery.rate or yarn.rate or 0))),
        reference_type="YarnDelivery",
        reference_id=delivery.id,
        reference_number=f"YDEL-{str(delivery.id)[:8].upper()}",
        movement_date=delivery.recd_date or timezone.localdate(),
        notes=f"PMS Yarn Inward Delivery: {yarn.name} ({recd_kgs} kg) at {factory.name}",
        user=user,
    )

    # Cross-link to Purchase GoodsReceipt if PO is attached
    if getattr(delivery, "purchase_order_id", None):
        try:
            from apps.purchase.models import GoodsReceipt, GoodsReceiptLine
            grn, created = GoodsReceipt.objects.get_or_create(
                client_id=client_id,
                purchase_order=delivery.purchase_order,
                grn_number=f"GRN-YARN-{str(delivery.id)[:6].upper()}",
                defaults={
                    "party": delivery.booking.supplier_party or delivery.purchase_order.party,
                    "receipt_date": delivery.recd_date or timezone.localdate(),
                    "location": location,
                    "supplier_challan_no": f"CH-{str(delivery.id)[:6].upper()}",
                    "qc_status": "Approved",
                },
            )
            if created:
                GoodsReceiptLine.objects.create(
                    client_id=client_id,
                    goods_receipt=grn,
                    item=item,
                    ordered_qty=recd_kgs,
                    received_qty=recd_kgs,
                    stock_qty=recd_kgs,
                    unit_cost=delivery.rate,
                    uom="kg",
                )
        except Exception as exc:
            logger.warning("Failed to auto-create purchase GRN for yarn delivery %s: %s", delivery.id, exc)

    return movement


def sync_beam_warp_consumption_to_inventory(beam, user=None):
    """
    Bridge 2: When a LoomBeam is loaded onto a machine:
    Deducts warp yarn weight (Meters * Warp kg/m) from factory shed inventory
    via StockMovement (type=PRODUCTION_ISSUE).
    """
    from apps.inventory.services import post_movement
    from apps.inventory.models import StockMovement

    if not beam or not beam.quality or not beam.quality.warp_yarn:
        return None

    client_id = beam.client_id
    factory = beam.factory
    warp_yarn = beam.quality.warp_yarn
    warp_kg = float(beam.length) * beam.quality.warp_kg_m
    warp_kg_dec = round4(D(str(warp_kg)))

    if warp_kg_dec <= ZERO:
        return None

    # Check if warp already issued for this beam
    existing = StockMovement.objects.filter(
        client_id=client_id,
        reference_type="LoomBeam",
        reference_id=beam.id,
        deleted_at__isnull=True,
    ).first()
    if existing:
        return existing

    item = ensure_yarn_item(warp_yarn, client_id)
    location = ensure_factory_location(factory, client_id)

    movement = post_movement(
        client_id=client_id,
        item=item,
        location=location,
        type="PRODUCTION_ISSUE",
        quantity=-warp_kg_dec,  # Negative quantity for consumption
        unit_cost=round4(D(str(warp_yarn.rate or 0))),
        reference_type="LoomBeam",
        reference_id=beam.id,
        reference_number=f"BEAM-{beam.beam_no}",
        movement_date=beam.date or timezone.localdate(),
        notes=f"Warp yarn consumption for beam {beam.beam_no} ({beam.quality.name}, {beam.length} m)",
        user=user,
    )
    return movement


def sync_daily_weft_consumption_to_inventory(daily_prod, user=None):
    """
    Bridge 3: When daily loom production is entered:
    Deducts weft yarn weight (Meters Woven * Weft kg/m) from factory shed inventory
    via StockMovement (type=PRODUCTION_ISSUE).
    """
    from apps.inventory.services import post_movement
    from apps.inventory.models import StockMovement

    if not daily_prod or not daily_prod.beam or not daily_prod.beam.quality:
        return None

    quality = daily_prod.beam.quality
    weft_yarn = quality.weft_yarn
    if not weft_yarn:
        return None

    meters = float(daily_prod.meters or 0)
    if meters <= 0:
        return None

    weft_kg = meters * quality.weft_kg_m
    weft_kg_dec = round4(D(str(weft_kg)))
    if weft_kg_dec <= ZERO:
        return None

    client_id = daily_prod.client_id
    factory = daily_prod.factory
    item = ensure_yarn_item(weft_yarn, client_id)
    location = ensure_factory_location(factory, client_id)

    existing = StockMovement.objects.filter(
        client_id=client_id,
        reference_type="DailyLoomProduction",
        reference_id=daily_prod.id,
        deleted_at__isnull=True,
    ).first()

    if existing:
        if abs(existing.quantity) == weft_kg_dec:
            return existing
        # If quantity changed, adjust difference
        diff = -weft_kg_dec - existing.quantity
        if diff != ZERO:
            return post_movement(
                client_id=client_id,
                item=item,
                location=location,
                type="PRODUCTION_ISSUE",
                quantity=round4(diff),
                unit_cost=round4(D(str(weft_yarn.rate or 0))),
                reference_type="DailyLoomProduction",
                reference_id=daily_prod.id,
                reference_number=f"DAILY-{daily_prod.date}-MCN{daily_prod.machine.no}",
                movement_date=daily_prod.date,
                notes=f"Weft yarn adjustment for machine MCN {daily_prod.machine.no} on {daily_prod.date}",
                user=user,
            )
        return existing

    movement = post_movement(
        client_id=client_id,
        item=item,
        location=location,
        type="PRODUCTION_ISSUE",
        quantity=-weft_kg_dec,  # Negative quantity for consumption
        unit_cost=round4(D(str(weft_yarn.rate or 0))),
        reference_type="DailyLoomProduction",
        reference_id=daily_prod.id,
        reference_number=f"DAILY-{daily_prod.date}-MCN{daily_prod.machine.no}",
        movement_date=daily_prod.date,
        notes=f"Weft yarn consumption for machine MCN {daily_prod.machine.no} on {daily_prod.date} ({meters} m)",
        user=user,
    )
    return movement


def sync_fabric_dispatch_to_inventory(dispatch, user=None):
    """
    Bridge 4: When grey fabric is dispatched from the loom factory:
    1. If dest == 'Godown':
       - Generates/links an inventory.Batch.
       - Generates inventory.LotTaka rolls with unique barcodes.
       - Posts incoming StockMovement (type=PRODUCTION_OUTPUT) to target warehouse.
    2. If dest == 'Dyeing mill':
       - Auto-links/creates a jobwork.JobWorkOrder so dyeing processing is tracked.
    """
    from apps.inventory.services import post_movement
    from apps.inventory.models import Batch, LotTaka, StockMovement

    if not dispatch or not dispatch.quality:
        return None

    client_id = dispatch.client_id
    total_meters = D(str(dispatch.meters or 0))
    taka_count = int(dispatch.taka or 1)
    if total_meters <= ZERO:
        return None

    item = ensure_fabric_item(dispatch.quality, client_id)

    # 1. Routing to Godown (Finished Greige Storage)
    if dispatch.dest == "Godown":
        target_loc = dispatch.target_location or ensure_central_godown(client_id)
        if not dispatch.target_location:
            dispatch.target_location = target_loc
            dispatch.save(update_fields=["target_location", "updated_at"])

        # Check if batch exists or create
        batch = dispatch.batch
        if not batch:
            batch_code = f"BATCH-GREY-{dispatch.quality.name[:6].upper()}-{str(dispatch.id)[:6].upper()}"
            batch, _ = Batch.objects.get_or_create(
                client_id=client_id,
                batch_code=batch_code,
                defaults={
                    "origin": "IN_HOUSE",
                    "fabric_type": "GREY",
                    "ownership": "OSCAR",
                    "item": item,
                    "created_on": dispatch.date or timezone.localdate(),
                    "notes": f"Dispatched from {dispatch.factory.name} via challan {dispatch.challan or '-'}",
                },
            )
            dispatch.batch = batch
            dispatch.save(update_fields=["batch", "updated_at"])

        # Avoid duplicate posting
        if StockMovement.objects.filter(
            client_id=client_id,
            reference_type="FabricDispatch",
            reference_id=dispatch.id,
            deleted_at__isnull=True,
        ).exists():
            return None

        meters_per_taka = round4(total_meters / D(str(taka_count)))
        created_takas = []
        cost_price = item.cost_price or ZERO

        for i in range(1, taka_count + 1):
            taka_no = f"{i:02d}"
            taka_code = f"{batch.batch_code}-T{taka_no}"
            
            # Last taka absorbs any fractional rounding difference
            if i == taka_count:
                roll_m = round4(total_meters - (meters_per_taka * (taka_count - 1)))
            else:
                roll_m = meters_per_taka

            lot_taka = LotTaka.objects.create(
                client_id=client_id,
                batch=batch,
                lot_number=batch.batch_code,
                taka_number=taka_no,
                taka_id=taka_code,
                item=item,
                fabric_quality=dispatch.quality.fabric_quality,
                location=target_loc,
                received_date=dispatch.date or timezone.localdate(),
                received_quantity=roll_m,
                available_quantity=ZERO,  # post_movement will add roll_m to available_quantity
                uom="Meter",
                status="Available",
                remarks=f"Loom dispatch from {dispatch.factory.name}",
            )

            post_movement(
                client_id=client_id,
                item=item,
                location=target_loc,
                type="PRODUCTION_OUTPUT",
                quantity=roll_m,
                unit_cost=cost_price,
                reference_type="FabricDispatch",
                reference_id=dispatch.id,
                reference_number=dispatch.challan or f"DISP-{str(dispatch.id)[:8]}",
                batch_number=batch.batch_code,
                lot_taka=lot_taka,
                movement_date=dispatch.date or timezone.localdate(),
                notes=f"Off-loom roll {taka_code} from {dispatch.factory.name}",
                user=user,
            )
            lot_taka.refresh_from_db()
            created_takas.append(lot_taka)

        logger.info(
            "Created %d LotTaka rolls totaling %s meters for FabricDispatch %s",
            len(created_takas),
            total_meters,
            dispatch.id,
        )
        return created_takas

    # 2. Routing to Dyeing Mill (External Jobwork)
    elif dispatch.dest == "Dyeing mill":
        if not dispatch.job_work_order and dispatch.dyeing_mill_party:
            try:
                from apps.jobwork.models import JobWorkOrder
                jwo = JobWorkOrder.objects.create(
                    client_id=client_id,
                    order_number=f"JWO-{str(dispatch.id)[:6].upper()}",
                    processor=dispatch.dyeing_mill_party,
                    order_date=dispatch.date or timezone.localdate(),
                    process_type="Dyeing",
                    ordered_qty=total_meters,
                    rate=dispatch.quality.pick_charge or ZERO,
                    status="Pending",
                    notes=f"Auto-generated from Loom Fabric Dispatch ({dispatch.quality.name})",
                )
                dispatch.job_work_order = jwo
                dispatch.save(update_fields=["job_work_order", "updated_at"])
            except Exception as exc:
                logger.warning("Failed to auto-create JobWorkOrder for dispatch %s: %s", dispatch.id, exc)

    return None


def sync_daily_loom_production_to_pi(daily_prod, user=None):
    """
    Bridge 5: Automatically populates ProductionInstruction (PI) progress from Loom Daily.
    Whenever daily loom production is entered in /pms/loom-daily:
    1. Looks up any active ProductionInstruction matching this quality name / fabric.
    2. Automatically creates or updates a DailyProductionEntry under that PI.
    3. Advances the PI's cumulative meter total, balance, and status.
    This eliminates double entry between /pms/loom-daily and /pms/daily-production!
    """
    if not daily_prod or not daily_prod.beam or not daily_prod.beam.quality:
        return None

    quality = daily_prod.beam.quality
    client_id = daily_prod.client_id
    meters = D(str(daily_prod.meters or 0))
    if meters <= ZERO:
        return None

    try:
        from apps.pms.models import ProductionInstruction, DailyProductionEntry
        from apps.hrms.models import Employee

        # Find active PI matching this quality name or fabric
        pi = ProductionInstruction.objects.filter(
            client_id=client_id,
            deleted_at__isnull=True,
        ).filter(
            Q(fabric__iexact=quality.name) |
            Q(fabric__icontains=quality.name) |
            Q(order_reference__icontains=quality.name)
        ).exclude(
            status__in=["Draft", "Completed", "Closed", "Verified", "PO Created"]
        ).order_by("created_at").first()

        if not pi:
            return None

        marker = f"[Auto-LoomProd:{daily_prod.id}]"
        existing_entry = DailyProductionEntry.objects.filter(
            client_id=client_id,
            instruction=pi,
            remarks__contains=marker,
            deleted_at__isnull=True,
        ).first()

        damage = D(str(daily_prod.damage or 0))
        good = max(ZERO, meters - damage)

        # Resolve an employee to attribute the entry to
        employee = pi.employee
        if not employee:
            employee = Employee.objects.filter(client_id=client_id, deleted_at__isnull=True).first()

        if existing_entry:
            if existing_entry.meters != meters or existing_entry.reject_meter != damage:
                existing_entry.meters = meters
                existing_entry.good_meter = good
                existing_entry.reject_meter = damage
                # Recalculate cumulative
                prev_entries = DailyProductionEntry.objects.filter(
                    client_id=client_id,
                    instruction=pi,
                    entry_date__lte=daily_prod.date,
                    deleted_at__isnull=True,
                ).exclude(id=existing_entry.id)
                prev_cum = sum((e.meters for e in prev_entries), ZERO)
                existing_entry.cumulative_meter = prev_cum + meters
                existing_entry.balance_meter = max(ZERO, pi.order_meter - existing_entry.cumulative_meter)
                existing_entry.save()
            return existing_entry

        # Create new entry
        prev_entries = DailyProductionEntry.objects.filter(
            client_id=client_id,
            instruction=pi,
            entry_date__lte=daily_prod.date,
            deleted_at__isnull=True,
        )
        prev_cum = sum((e.meters for e in prev_entries), ZERO)
        cum = prev_cum + meters
        bal = max(ZERO, pi.order_meter - cum)

        machine_no = daily_prod.machine.no if daily_prod.machine else "Loom"
        remarks = f"Auto-synced from Machine {machine_no} ({quality.name}) {marker}"
        entry = DailyProductionEntry.objects.create(
            client_id=client_id,
            instruction=pi,
            entry_date=daily_prod.date,
            opening_pending=max(ZERO, pi.order_meter - prev_cum),
            meters=meters,
            good_meter=good,
            reject_meter=damage,
            reprocess_meter=ZERO,
            cumulative_meter=cum,
            balance_meter=bal,
            entered_by_employee=employee,
            entered_by_user=user if (user and user.is_authenticated) else None,
            remarks=remarks,
            status="Submitted",
        )

        # Advance PI status
        if bal <= ZERO:
            pi.status = "Completed"
        elif pi.status in ("Approved", "Not Started", "Draft"):
            pi.status = "Running"
        else:
            pi.status = "Partially Completed"
        pi.save(update_fields=["status", "updated_at"])

        logger.info(
            "Auto-synced Loom daily production %s (%s m) to PI %s",
            daily_prod.id,
            meters,
            pi.instruction_number,
        )
        return entry
    except Exception as exc:
        logger.warning("Failed to auto-sync Loom production to PI: %s", exc)
        return None

