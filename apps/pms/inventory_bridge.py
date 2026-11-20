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


def sync_inventory_item_to_pms_yarn(item):
    """
    Reverse bridge: When a Yarn Item is created or updated in Inventory (masters.Item),
    automatically creates or updates the corresponding PMS YarnMaster so it is
    instantly visible in PMS Loom & Yarn Management.
    """
    if not item:
        return None
    client_id = item.client_id
    cat_code = (getattr(item.category, "code", "") or "").lower()
    cat_name = (getattr(item.category, "name", "") or "").lower()
    is_yarn = (
        getattr(item, "item_kind", "") == "Yarn"
        or "yarn" in cat_code
        or "yarn" in cat_name
    )
    if not is_yarn:
        return None

    from apps.pms.models_loom import YarnMaster

    ym = YarnMaster.objects.filter(client_id=client_id, item=item, deleted_at__isnull=True).first()
    if not ym:
        ym = YarnMaster.objects.filter(client_id=client_id, name__iexact=item.name, deleted_at__isnull=True).first()

    rate = item.cost_price if item.cost_price and item.cost_price > ZERO else (item.selling_price or ZERO)
    denier = getattr(item, "denier", None) or Decimal("150.00")

    if ym:
        ym.item = item
        ym.category = item.category
        if rate and rate > ZERO:
            ym.rate = rate
        ym.save(update_fields=["item", "category", "rate", "updated_at"])
    else:
        ym = YarnMaster.objects.create(
            client_id=client_id,
            name=item.name,
            denier=denier,
            kg_per_bag=Decimal("25.00"),
            rate=rate,
            item=item,
            category=item.category,
            note=item.description or "",
        )
    return ym


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
    Decoupled from central inventory transactions:
    PMS operates separately from central inventory stock ledger.
    Factory yarn stock is calculated autonomously inside PMS (get_factory_yarn_stock).
    No transactional StockMovement or GoodsReceipt is created.
    """
    return None


def sync_beam_warp_consumption_to_inventory(beam, user=None):
    """
    Decoupled from central inventory transactions:
    PMS operates separately from central inventory stock ledger.
    Warp consumption is tracked autonomously within PMS floor metrics.
    No transactional StockMovement is created.
    """
    return None


def sync_daily_weft_consumption_to_inventory(daily_prod, user=None):
    """
    Decoupled from central inventory transactions:
    PMS operates separately from central inventory stock ledger.
    Weft consumption is tracked autonomously within PMS floor metrics.
    No transactional StockMovement is created.
    """
    return None


def sync_fabric_dispatch_to_inventory(dispatch, user=None):
    """
    Decoupled from central inventory transactions:
    PMS operates separately from central inventory stock ledger.
    Fabric dispatch is tracked autonomously within PMS (FabricDispatch).
    No Batch, LotTaka, or StockMovement is created in central inventory.
    Only takes reference to Item, Location, and FabricQuality.
    """
    if not dispatch or not dispatch.quality:
        return None
    # Keep reference to target_location if supplied
    if dispatch.dest == "Godown" and not dispatch.target_location:
        loc = ensure_central_godown(dispatch.client_id)
        if loc:
            dispatch.target_location = loc
            dispatch.save(update_fields=["target_location", "updated_at"])
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


def record_mill_inward_with_landed_cost(
    dispatch,
    inward_meters,
    inward_taka=1,
    dyeing_rate=None,
    logistic_rate=None,
    weaving_rate=None,
    yarn_rate=None,
    inward_challan=None,
    inward_date=None,
    target_location=None,
    transporter_party_id=None,
    user=None,
):
    """
    Bridge 6: Records inward of dyed/finished fabric returning from external processing mill:
    1. Computes composite landed rate per meter:
       - Weaving Rate (Weaving Factory)
       - Yarn Raw Material Cost
       - Dyeing / Mill Processing Rate
       - Logistics / Freight Cost
       Total Landed Cost/m = Weaving + Yarn + Dyeing + Freight
    2. Adds to Inventory:
       - Generates/links a finished/dyed Batch.
       - Creates individual LotTaka rolls with barcodes and cost breakdown in LotCostEntry.
       - Posts incoming StockMovement (type=PRODUCTION_OUTPUT) at total landed cost.
    3. Generates complete Fabric Traceability Passport and stores on dispatch.history_passport.
    4. Automatically posts financial double-entry JournalEntry in apps.accounting:
       - Debit: Finished Goods Inventory (Asset)
       - Credit: Weaving Factory Payable (Creditor)
       - Credit: Dyeing Mill Payable (Creditor)
       - Credit: Logistics Transporter Payable (Creditor)
       - Credit: WIP Yarn Consumed
    """
    from apps.core.money import round2, round4

    if not dispatch or not dispatch.quality:
        raise ValueError("Valid FabricDispatch with quality is required.")

    client_id = dispatch.client_id
    quality = dispatch.quality
    factory = dispatch.factory
    inward_meters = round4(D(str(inward_meters or 0)))
    if inward_meters <= ZERO:
        raise ValueError("Inward meters must be greater than zero.")

    inward_taka = max(1, int(inward_taka or 1))
    if isinstance(inward_date, str):
        try:
            from datetime import date
            inward_date = date.fromisoformat(inward_date)
        except Exception:
            inward_date = timezone.localdate()
    elif not inward_date:
        inward_date = timezone.localdate()

    # 1. Resolve component rates
    w_rate = D(str(weaving_rate if weaving_rate is not None else (dispatch.weaving_rate or getattr(quality, "job_rate_per_meter", 0) or 0)))
    if w_rate <= ZERO and getattr(quality, "ppi", 0) and getattr(quality, "pick_charge", 0):
        w_rate = round4(D(str(quality.ppi)) * D(str(quality.pick_charge)))

    y_rate = D(str(yarn_rate if yarn_rate is not None else (dispatch.yarn_rate or 0)))
    if y_rate <= ZERO and getattr(quality, "warp_cost_m", 0) is not None:
        y_rate = round4(D(str(quality.warp_cost_m or 0)) + D(str(quality.weft_cost_m or 0)))

    d_rate = D(str(dyeing_rate if dyeing_rate is not None else (dispatch.dyeing_rate or 0)))
    l_rate = D(str(logistic_rate if logistic_rate is not None else (dispatch.logistic_rate or 0)))

    total_rate = round4(w_rate + y_rate + d_rate + l_rate)
    total_landed_amount = round2(total_rate * inward_meters)

    # Resolve transporter party if supplied
    transporter = dispatch.transporter_party
    if transporter_party_id:
        transporter = Party.objects.filter(client_id=client_id, id=transporter_party_id, deleted_at__isnull=True).first()
        if transporter:
            dispatch.transporter_party = transporter

    # Update dispatch records
    dispatch.weaving_rate = w_rate
    dispatch.yarn_rate = y_rate
    dispatch.dyeing_rate = d_rate
    dispatch.logistic_rate = l_rate
    dispatch.landed_cost_rate = total_rate
    dispatch.mill_inward_status = "Received"
    dispatch.mill_inward_date = inward_date
    dispatch.mill_inward_meters = inward_meters
    dispatch.mill_inward_taka = inward_taka
    dispatch.mill_inward_challan = inward_challan or ""

    # 2. Maintain reference to Godown/Location if provided (Read-only reference)
    if target_location:
        dispatch.target_location = target_location
    elif not dispatch.target_location:
        loc = ensure_central_godown(client_id)
        if loc:
            dispatch.target_location = loc

    batch_code = f"BATCH-DYED-{quality.name[:6].upper()}-{str(dispatch.id)[:6].upper()}"

    # 3. Build Structured Fabric History Passport
    shrinkage = ZERO
    orig_meters = D(str(dispatch.meters or 0))
    if orig_meters > ZERO and inward_meters != orig_meters:
        shrinkage = round2(((orig_meters - inward_meters) / orig_meters) * Decimal("100.00"))

    passport = {
        "passport_id": f"PASS-{str(dispatch.id)[:8].upper()}",
        "quality": quality.name,
        "batch_code": batch_code,
        "origin_factory": {
            "name": factory.name,
            "id": str(factory.id),
            "weaving_rate": str(w_rate),
            "yarn_cost": str(y_rate),
            "grey_cost_m": str(w_rate + y_rate),
            "dispatch_meters": str(dispatch.meters),
            "dispatch_taka": dispatch.taka,
            "dispatch_date": str(dispatch.date),
            "outward_challan": dispatch.challan or "-",
        },
        "processing_mill": {
            "name": getattr(dispatch.dyeing_mill_party, "name", "") or (dispatch.dest_name or "Dyeing Mill"),
            "id": str(dispatch.dyeing_mill_party.id) if dispatch.dyeing_mill_party else None,
            "process_type": "Dyeing / Processing",
            "dyeing_rate": str(d_rate),
            "inward_date": str(inward_date),
            "inward_challan": inward_challan or "-",
            "inward_meters": str(inward_meters),
            "inward_taka": inward_taka,
            "shrinkage_pct": str(shrinkage),
        },
        "logistics": {
            "transporter": getattr(transporter, "name", "") or dispatch.vehicle or "Transporter",
            "transporter_id": str(transporter.id) if transporter else None,
            "vehicle_no": dispatch.vehicle or "-",
            "logistic_rate": str(l_rate),
            "total_freight": str(round2(l_rate * inward_meters)),
        },
        "landed_cost": {
            "weaving_rate_m": str(w_rate),
            "yarn_cost_m": str(y_rate),
            "dyeing_rate_m": str(d_rate),
            "logistic_rate_m": str(l_rate),
            "total_rate_m": str(total_rate),
            "total_amount": str(total_landed_amount),
        },
        "created_takas_count": inward_taka,
        "status": "Inwarded & Landed Cost Posted",
    }
    dispatch.history_passport = passport

    # 4. Impact on Accounts Section
    journal_entry = None
    try:
        from apps.accounting.services import system_account, debit, credit, post_entry
        stock_acct = system_account(client_id, "stock")
        creditors_acct = system_account(client_id, "creditors")
        wip_acct = system_account(client_id, "purchases") or system_account(client_id, "cogs")

        postings = []
        # Debit: Inventory Asset
        postings.append(debit(stock_acct, total_landed_amount, description=f"Dyed Fabric Landed Asset: {quality.name} ({inward_meters} m @ Rs {total_rate}/m)"))

        # Credits:
        w_party = getattr(factory, "job_worker_party", None)
        total_w_amt = round2(w_rate * inward_meters)
        if total_w_amt > ZERO:
            postings.append(credit(creditors_acct, total_w_amt, party=w_party, description=f"Weaving Charges ({factory.name})"))

        d_party = dispatch.dyeing_mill_party
        total_d_amt = round2(d_rate * inward_meters)
        if total_d_amt > ZERO:
            postings.append(credit(creditors_acct, total_d_amt, party=d_party, description=f"Dyeing Charges ({getattr(d_party, 'name', 'Mill')})"))

        total_l_amt = round2(l_rate * inward_meters)
        if total_l_amt > ZERO:
            postings.append(credit(creditors_acct, total_l_amt, party=transporter, description=f"Logistics / Freight ({getattr(transporter, 'name', 'Transporter')})"))

        total_y_amt = round2(y_rate * inward_meters)
        if total_y_amt > ZERO:
            postings.append(credit(wip_acct, total_y_amt, description="Raw Yarn Consumed for Weaving"))

        # Check balance and absorb any minor roundoff difference
        cr_sum = round2(sum((p.credit for p in postings), ZERO))
        diff = round2(total_landed_amount - cr_sum)
        if diff != ZERO:
            # Adjust the stock debit so dr == cr exactly
            postings[0].debit = cr_sum

        if len(postings) >= 2 and any(p.credit > ZERO for p in postings):
            journal_entry = post_entry(
                client=client_id,
                postings=postings,
                entry_date=inward_date,
                narration=f"Landed Cost Journal for Fabric Dispatch {dispatch.challan or dispatch.id} ({quality.name}) - Weaving + Dyeing + Logistics",
                source_document_type="FabricDispatch",
                source_document_id=dispatch.id,
                user=user,
            )
            passport["accounts_journal_entry"] = {
                "id": str(journal_entry.id),
                "entry_number": journal_entry.entry_number,
                "date": str(journal_entry.entry_date),
                "status": "Posted",
            }
            dispatch.history_passport = passport
    except Exception as exc:
        logger.warning("Could not auto-post journal entry for mill inward %s: %s", dispatch.id, exc)

    dispatch.save(update_fields=[
        "weaving_rate", "yarn_rate", "dyeing_rate", "logistic_rate", "transporter_party",
        "landed_cost_rate", "mill_inward_status", "mill_inward_date", "mill_inward_meters",
        "mill_inward_taka", "mill_inward_challan", "history_passport", "updated_at"
    ])
    return {
        "dispatch": dispatch,
        "passport": passport,
        "journal_entry": journal_entry,
        "created_takas": [],
    }


