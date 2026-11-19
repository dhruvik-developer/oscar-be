"""Inventory views submodule."""
import json
import logging
import uuid
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Count, DecimalField, F, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import User
from apps.core.exceptions import (
    BusinessRuleViolation,
    Codes,
    Conflict,
    NotFound,
    PermissionDenied,
    ValidationFailed,
)
from apps.core.money import ZERO, D, round2, round4
from apps.core.numbering import allocate_number
from apps.core.pagination import envelope
from apps.core.permissions import HasModulePermission, require_permission
from apps.core.viewsets import ReadOnlyTenantViewSet, TenantModelViewSet
from apps.jobwork.models import JWOInward, JWOOutward, JWOReprocess, JobWorkOrder
from apps.masters import uom
from apps.masters.models import (
    Design,
    Fabric,
    FabricQuality,
    Item,
    ItemCategory,
    ItemPart,
    Location,
    Party,
    Shade,
)
from apps.purchase.models import (
    GoodsReceipt,
    GoodsReceiptLine,
    PurchaseBill,
    PurchaseBillLine,
    PurchaseOrder,
)
from apps.sales.models import SalesReturn, SalesReturnLine

from .. import costing_services, logistics_services, sampling_services
from .. import services as stock
from ..models import (
    FaultyPart,
    LotCostEntry,
    LotTaka,
    QCInspection,
    QCInspectionLine,
    QCInspectionLineDefect,
    QualityStandard,
    Sample,
    ServiceUsage,
    StockAudit,
    StockAuditLine,
    StockBalance,
    StockMovement,
    StockTransfer,
    StockTransferLine,
    TakaTagPrintLog,
    Trip,
    TripMaterialLine,
    ZoneRequest,
    ZoneRequestLine,
)
from ..serializers import (
    FaultyPartSerializer,
    ItemPartSerializer,
    LotCostEntrySerializer,
    QCInspectionLineDefectSerializer,
    QCInspectionLineSerializer,
    QCInspectionSerializer,
    QualityStandardSerializer,
    SampleSerializer,
    ServiceUsageSerializer,
    StockAdjustmentSerializer,
    StockAuditSerializer,
    StockMovementSerializer,
    StockTransferSerializer,
    TripMaterialLineSerializer,
    TripSerializer,
    ZoneRequestSerializer,
)

logger = logging.getLogger(__name__)

QTY = DecimalField(max_digits=18, decimal_places=4)
MONEY = DecimalField(max_digits=18, decimal_places=2)

STOCK_AGE_BUCKETS = ("0-30", "31-60", "61-90", "91-180", "180+")

def stock_age_bucket(days):
    days = int(days or 0)
    if days <= 30:
        return "0-30"
    if days <= 60:
        return "31-60"
    if days <= 90:
        return "61-90"
    if days <= 180:
        return "91-180"
    return "180+"


class ValuationView(APIView):
    """``GET /inventory/valuation/`` -- ``?method=WAC``, ``?bucket=``, ``?locationId=``.

    Value is WAC from the movement ``unit_cost`` (db.md Appendix B decision 5);
    FIFO valuation would need per-receipt cost layers and is reported as
    unsupported rather than silently returning WAC under a FIFO label.

    Ageing is real: each lot/taka with stock is aged from its received date;
    stock that is not held on takas is aged by FIFO over the location's
    inward movements (the newest receipts are assumed to be what is left).
    """

    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]

    INWARD_TYPES = (
        "PURCHASE", "TRANSFER_IN", "JOBWORK_INWARD", "ADJUSTMENT", "SALES_RETURN",
        "SALES_RETURN_GOOD", "SAMPLE_RETURN", "QC_PASS",
    )

    def get(self, request):
        method = (request.query_params.get("method") or "WAC").upper()
        if method not in ("WAC", "FIFO"):
            raise ValidationFailed(
                "Unknown valuation method.",
                field_errors={"method": ["Expected WAC or FIFO."]},
            )
        if method == "FIFO":
            raise ValidationFailed(
                "FIFO valuation is not available on this workspace.",
                code="VALUATION_METHOD_UNSUPPORTED",
                detail="FIFO requires per-receipt cost layers; WAC is available.",
            )
        bucket_filter = request.query_params.get("bucket")
        if bucket_filter and bucket_filter not in STOCK_AGE_BUCKETS:
            raise ValidationFailed(
                "Unknown ageing bucket.",
                field_errors={"bucket": [f"Expected one of {', '.join(STOCK_AGE_BUCKETS)}."]},
            )
        location_filter = request.query_params.get("locationId")

        client_id = request.client_id
        today = timezone.localdate()
        balances = (
            StockBalance.objects.filter(client_id=client_id, deleted_at__isnull=True)
            .select_related("item", "item__category", "location")
            .exclude(item__item_kind="Service")
        )
        if location_filter:
            balances = balances.filter(location_id=location_filter)

        lots_by_key = {}
        # Customer-owned rolls are held, not owned: they are taken out of the
        # quantity valued and reported separately (never in OSCAR's value).
        customer_qty = {}
        for lot in LotTaka.objects.filter(
            client_id=client_id, deleted_at__isnull=True, available_quantity__gt=0
        ).select_related("batch").order_by("received_date"):
            if lot.batch_id and lot.batch.ownership != "OSCAR":
                key = (lot.item_id, lot.location_id)
                customer_qty[key] = customer_qty.get(key, ZERO) + D(lot.available_quantity)
                continue
            lots_by_key.setdefault((lot.item_id, lot.location_id), []).append(lot)

        rows = []
        bucket_totals = {label: {"qty": ZERO, "value": ZERO} for label in STOCK_AGE_BUCKETS}
        total_value = ZERO

        def add_row(balance, qty, unit_cost, inward_date, lot=None):
            nonlocal total_value
            qty = round4(qty)
            if qty <= ZERO:
                return
            age_days = max(0, (today - inward_date).days) if inward_date else None
            bucket = stock_age_bucket(age_days) if age_days is not None else None
            value = round2(qty * D(unit_cost))
            if bucket is not None:
                bucket_totals[bucket]["qty"] += qty
                bucket_totals[bucket]["value"] += value
            total_value += value
            item = balance.item
            rows.append({
                "id": str(lot.id) if lot else f"{balance.item_id}:{balance.location_id}:{inward_date}",
                "itemId": str(balance.item_id),
                "sku": item.sku,
                "name": item.name,
                "category": item.category.name if item.category_id else None,
                "locationId": str(balance.location_id),
                "location": balance.location.name,
                "lotNo": lot.lot_number if lot else None,
                "takaId": (lot.taka_id or lot.barcode) if lot else None,
                "uom": item.uom,
                "qty": qty,
                "unitCost": round4(unit_cost),
                "value": value,
                "inwardDate": inward_date.isoformat() if inward_date else None,
                "ageDays": age_days,
                "agingBucket": bucket,
            })

        for balance in balances:
            on_hand = D(balance.on_hand) - customer_qty.get((balance.item_id, balance.location_id), ZERO)
            if on_hand <= ZERO:
                continue
            unit_cost = balance.weighted_average_cost or D(balance.item.cost_price)
            remaining = on_hand
            for lot in lots_by_key.get((balance.item_id, balance.location_id), []):
                if remaining <= ZERO:
                    break
                qty = min(D(lot.available_quantity), remaining)
                add_row(balance, qty, unit_cost, lot.received_date, lot)
                remaining -= qty
            if remaining <= ZERO:
                continue
            # FIFO layers for the part not held on takas: walk inward
            # movements newest-first until the remaining quantity is covered.
            inwards = StockMovement.objects.filter(
                client_id=client_id,
                item_id=balance.item_id,
                location_id=balance.location_id,
                quantity__gt=0,
                type__in=self.INWARD_TYPES,
                lot_taka__isnull=True,
            ).order_by("-movement_date", "-created_at").values_list("movement_date", "quantity")
            for movement_date, quantity in inwards:
                if remaining <= ZERO:
                    break
                qty = min(D(quantity), remaining)
                add_row(balance, qty, unit_cost, movement_date)
                remaining -= qty
            if remaining > ZERO:
                # Stock with no inward record at this location (e.g. opening
                # balance) has no known age; it is valued but not bucketed.
                add_row(balance, remaining, unit_cost, None)

        if bucket_filter:
            rows = [row for row in rows if row["agingBucket"] == bucket_filter]
        rows.sort(key=lambda row: (row["ageDays"] is None, -(row["ageDays"] or 0)))
        return Response(
            envelope(
                rows,
                aggregates={
                    "method": "WAC",
                    "totalValuation": round2(total_value),
                    "customerOwnedQtyExcluded": round4(sum(customer_qty.values(), ZERO)),
                    "buckets": [
                        {
                            "bucket": label,
                            "qty": round4(bucket_totals[label]["qty"]),
                            "value": round2(bucket_totals[label]["value"]),
                        }
                        for label in STOCK_AGE_BUCKETS
                    ],
                    "currentStockValue": round2(bucket_totals["0-30"]["value"]),
                    "staleValuation": round2(
                        bucket_totals["91-180"]["value"] + bucket_totals["180+"]["value"]
                    ),
                },
            )
        )


class QualityStandardViewSet(TenantModelViewSet):
    queryset = QualityStandard.objects.select_related("category")
    serializer_class = QualityStandardSerializer
    audit_entity_type = "QualityStandard"
    audit_label_field = "name"
    required_permissions = ["view_purchase"]
    status_field = None
    search_fields = ["name"]
    ordering = ["name"]


class InventoryDashboardView(APIView):
    """
    GET /inventory/dashboard/
    Live overview of fabric stock, QC status, movement and value.
    Computes all figures dynamically from the database - zero dump/dummy data.
    """

    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]

    def get(self, request):
        client_id = request.client_id
        today = timezone.localdate()

        # Date parameters (default: 1st of current month to today)
        start_date_str = request.query_params.get("start_date") or request.query_params.get("date_from")
        end_date_str = request.query_params.get("end_date") or request.query_params.get("date_to")

        first_day_this_month = today.replace(day=1)
        last_day_prev_month = first_day_this_month - timedelta(days=1)

        try:
            start_date = date.fromisoformat(start_date_str) if start_date_str else first_day_this_month
        except (ValueError, TypeError):
            start_date = first_day_this_month

        try:
            end_date = date.fromisoformat(end_date_str) if end_date_str else today
        except (ValueError, TypeError):
            end_date = today

        # Master filter options
        godown_id = request.query_params.get("godown_id") or request.query_params.get("location_id")
        category_id = request.query_params.get("category_id")
        fabric_filter = request.query_params.get("fabric")
        colour_filter = request.query_params.get("colour") or request.query_params.get("color")

        godowns_list = [
            {"id": str(loc.id), "name": loc.name, "code": loc.code, "type": loc.type}
            for loc in Location.objects.filter(
                client_id=client_id, deleted_at__isnull=True, is_active=True
            ).exclude(code__startswith="SYS-").exclude(type__in=["Transit", "Scrap", "Zone"]).order_by("name")
        ]
        categories_list = [
            {"id": str(cat.id), "name": cat.name, "code": cat.code}
            for cat in ItemCategory.objects.filter(client_id=client_id, deleted_at__isnull=True).order_by("name")
        ]
        fabrics_list = [
            {"id": str(fab.id), "name": fab.name, "code": fab.code}
            for fab in Fabric.objects.filter(client_id=client_id, deleted_at__isnull=True).order_by("name")
        ]
        shades_qs = Shade.objects.filter(client_id=client_id, deleted_at__isnull=True).order_by("name")
        colours_list = [
            {"id": str(s.id), "name": s.name, "code": s.code, "hex_color": s.hex_color}
            for s in shades_qs
        ]
        if not colours_list:
            item_colours = (
                Item.objects.filter(client_id=client_id, deleted_at__isnull=True, fabric_color__isnull=False)
                .exclude(fabric_color="")
                .values_list("fabric_color", flat=True)
                .distinct()
            )
            colours_list = [{"id": c, "name": c, "code": c, "hex_color": None} for c in item_colours]

        # Base item queryset
        items_qs = Item.objects.filter(client_id=client_id, deleted_at__isnull=True).exclude(item_kind="Service")
        if category_id:
            items_qs = items_qs.filter(category_id=category_id)
        if fabric_filter:
            items_qs = items_qs.filter(
                Q(fabric_quality__iexact=fabric_filter)
                | Q(name__icontains=fabric_filter)
                | Q(sku__icontains=fabric_filter)
            )
        if colour_filter:
            items_qs = items_qs.filter(fabric_color__iexact=colour_filter)

        item_map = {item.id: item for item in items_qs}
        item_ids = list(item_map.keys())

        # Group the items in scope by their real stock unit ("Mtr" and "Meter"
        # are one unit). Quantities are only summed when there is one unit;
        # otherwise each card reports per unit instead of a meaningless total.
        book = uom.UnitBook(client_id)

        def unit_label(item):
            unit = book.resolve(item.uom)
            return unit.code if unit else ((item.uom or "").strip() or "Unit")

        item_unit = {item_id: unit_label(item) for item_id, item in item_map.items()}

        # Balances
        balances_qs = StockBalance.objects.filter(client_id=client_id, deleted_at__isnull=True, item_id__in=item_ids)
        if godown_id:
            balances_qs = balances_qs.filter(location_id=godown_id)

        balance_rows = list(balances_qs.select_related("item", "location"))

        # Determine real stock units for items that have physical stock on hand.
        active_stock_units = sorted(set(
            item_unit.get(b.item_id) for b in balance_rows if D(b.on_hand) > ZERO and item_unit.get(b.item_id)
        ))
        units_in_scope = active_stock_units if active_stock_units else sorted(set(item_unit.values()))
        mixed_units = len(units_in_scope) > 1
        dominant_uom = units_in_scope[0] if len(units_in_scope) == 1 else ("Mixed" if mixed_units else "Mtr")
        if dominant_uom in ("Meter", "meter"):
            dominant_uom = "Mtr"

        # Every stock figure below comes from stock_balances, split by the
        # *location* the stock is in: QC Hold / Reprocess / Second Quality /
        # Reject are system locations, everything else is good stock. The
        # categories partition on_hand, so nothing is counted twice.
        # Every stock figure below comes from stock_balances, split by the
        # location the stock is in: QC Hold / Reprocess / Second Quality /
        # Reject are system locations, SYS-WIP-* are external processor/agency
        # WIP locations, and standard godowns hold good/available stock.
        role_by_code = {
            code: role for role, (code, _, _) in stock.SYSTEM_LOCATIONS.items()
        }
        all_locations = list(Location.objects.filter(client_id=client_id, deleted_at__isnull=True))
        location_role = {}
        wip_locations = {}
        for loc in all_locations:
            if loc.code in role_by_code:
                location_role[loc.id] = role_by_code[loc.code]
            elif (loc.code and loc.code.startswith("SYS-WIP-")) or (loc.name and loc.name.startswith("WIP - ")):
                location_role[loc.id] = "EXTERNAL_WIP"
                wip_locations[loc.id] = loc
            else:
                location_role[loc.id] = "GOOD"

        def wac(balance):
            return D(balance.weighted_average_cost or get_item_cost(balance.item_id))

        def get_item_cost(item_id):
            item = item_map.get(item_id)
            return D(item.cost_price or 0) if item else ZERO

        role_qty = defaultdict(lambda: ZERO)
        role_val = defaultdict(lambda: ZERO)
        for r in ("GOOD", "EXTERNAL_WIP", "QC_HOLD", "REPROCESS", "SECOND_QUALITY", "REJECT", "IN_TRANSIT"):
            role_qty[r] = ZERO
            role_val[r] = ZERO

        wip_by_party_dict = {}
        parties = {p.code: p for p in Party.objects.filter(client_id=client_id, deleted_at__isnull=True) if p.code}
        parties_by_name = {p.name: p for p in Party.objects.filter(client_id=client_id, deleted_at__isnull=True)}

        for b in balance_rows:
            role = location_role.get(b.location_id, "GOOD")
            on_hand = D(b.on_hand)
            val = on_hand * wac(b)
            role_qty[role] += on_hand
            role_val[role] += val

            if role == "EXTERNAL_WIP":
                loc = wip_locations.get(b.location_id)
                loc_code = loc.code if loc else ""
                loc_name = loc.name if loc else ""
                suffix = loc_code.replace("SYS-WIP-", "") if loc_code.startswith("SYS-WIP-") else ""
                party = parties.get(suffix)
                if not party and loc_name.startswith("WIP - "):
                    party = parties_by_name.get(loc_name[6:].strip())
                party_id = str(party.id) if party else None
                party_name = party.name if party else (loc_name[6:].strip() if loc_name.startswith("WIP - ") else (loc_name or "External Processor"))
                entry = wip_by_party_dict.setdefault(b.location_id, {
                    "partyId": party_id,
                    "partyName": party_name,
                    "locationCode": loc_code,
                    "locationName": loc_name,
                    "qty": ZERO,
                    "value": ZERO,
                })
                entry["qty"] += on_hand
                entry["value"] += val

        wip_by_party = [
            {
                "partyId": v["partyId"],
                "partyName": v["partyName"],
                "locationCode": v["locationCode"],
                "locationName": v["locationName"],
                "qty": float(v["qty"]),
                "value": float(round2(v["value"])),
            }
            for v in wip_by_party_dict.values()
        ]

        total_on_hand = sum(role_qty.values(), ZERO)
        total_damaged = sum((D(b.damaged) for b in balance_rows), ZERO)

        good_qty, good_val = role_qty["GOOD"], role_val["GOOD"]
        wip_qty, wip_val = role_qty["EXTERNAL_WIP"], role_val["EXTERNAL_WIP"]
        qc_hold_qty, qc_hold_val = role_qty["QC_HOLD"], role_val["QC_HOLD"]
        reprocess_qty, reprocess_val = role_qty["REPROCESS"], role_val["REPROCESS"]
        second_qty, second_val = role_qty["SECOND_QUALITY"], role_val["SECOND_QUALITY"]
        rejected_qty, rejected_val = role_qty["REJECT"], role_val["REJECT"]
        in_transit_qty, in_transit_val = role_qty["IN_TRANSIT"], role_val["IN_TRANSIT"]

        # Taka-level analysis of GOOD stock for real Grey / Finished / Reserved partitioning (Phase 2.3)
        good_takas = LotTaka.objects.filter(
            client_id=client_id,
            deleted_at__isnull=True,
            available_quantity__gt=0,
            item_id__in=item_ids,
        )
        if godown_id:
            good_takas = good_takas.filter(location_id=godown_id)

        reserved_qty = ZERO
        reserved_val = ZERO
        finished_qty = ZERO
        finished_val = ZERO
        grey_qty = ZERO
        grey_val = ZERO

        good_takas_list = [lt for lt in good_takas if location_role.get(lt.location_id, "GOOD") == "GOOD"]
        taka_on_hand_by_item_loc = {}
        for lt in good_takas_list:
            taka_on_hand_by_item_loc[(lt.item_id, lt.location_id)] = (
                taka_on_hand_by_item_loc.get((lt.item_id, lt.location_id), ZERO) + D(lt.available_quantity)
            )
            lt_qty = D(lt.available_quantity)
            lt_cost = D(lt.rate) if lt.rate else get_item_cost(lt.item_id)
            lt_val = lt_qty * lt_cost

            if lt.status == "Reserved":
                reserved_qty += lt_qty
                reserved_val += lt_val
            elif (
                lt.status == "Finished"
                or getattr(item_map.get(lt.item_id), "fabric_stage", "") == "Finished"
                or (lt.source_document_type in ("JobWorkInward", "JWOInward", "Production"))
                or lt.parent_taka_id is not None
            ):
                finished_qty += lt_qty
                finished_val += lt_val
            else:
                grey_qty += lt_qty
                grey_val += lt_val

        for b in balance_rows:
            if location_role.get(b.location_id, "GOOD") != "GOOD":
                continue
            loose = D(b.on_hand) - taka_on_hand_by_item_loc.get((b.item_id, b.location_id), ZERO)
            if loose > ZERO:
                loose_val = loose * wac(b)
                it = item_map.get(b.item_id)
                if getattr(it, "fabric_stage", "") == "Finished" or "finished" in (it.fabric_quality or "").lower():
                    finished_qty += loose
                    finished_val += loose_val
                else:
                    grey_qty += loose
                    grey_val += loose_val

        # Wastage = FAULTY (damaged) stock, tracked separately from on_hand.
        wastage_qty = total_damaged
        wastage_val = sum((D(b.damaged) * wac(b) for b in balance_rows if D(b.damaged) > ZERO), ZERO)

        # Samples out with customers: issued minus returned. They have left
        # the godown, so they are reported but not part of the stock total.
        sample_issue_qs = StockMovement.objects.filter(
            client_id=client_id, item_id__in=item_ids, reference_type="SampleIssue",
        )
        sample_return_qs = StockMovement.objects.filter(
            client_id=client_id, item_id__in=item_ids, reference_type="SampleReturn",
        )
        if godown_id:
            sample_issue_qs = sample_issue_qs.filter(location_id=godown_id)
        sample_issued = abs(sample_issue_qs.aggregate(q=Coalesce(Sum("quantity"), Value(Decimal("0.0000")), output_field=QTY))["q"])
        sample_returned = sample_return_qs.filter(
            source_document_id__in=sample_issue_qs.values("id")
        ).aggregate(q=Coalesce(Sum("quantity"), Value(Decimal("0.0000")), output_field=QTY))["q"]
        sample_qty = max(ZERO, sample_issued - sample_returned)
        sample_val = round2(sum(
            (abs(D(m.quantity)) * D(m.unit_cost or get_item_cost(m.item_id)) for m in sample_issue_qs),
            ZERO,
        ) * (sample_qty / sample_issued if sample_issued > ZERO else ZERO))

        # Customer returns posted (all conditions) -- informational; the goods
        # themselves are inside the location categories above.
        ret_lines = SalesReturnLine.objects.filter(
            client_id=client_id,
            sales_return__status="Posted",
            sales_return__deleted_at__isnull=True,
            item_id__in=item_ids,
        )
        if godown_id:
            ret_lines = ret_lines.filter(sales_return__location_id=godown_id)
        customer_return_qty = ret_lines.aggregate(
            qty=Coalesce(Sum("returned_qty"), Value(Decimal("0.0000")), output_field=QTY)
        )["qty"]
        customer_return_val = ret_lines.aggregate(
            val=Coalesce(Sum(F("returned_qty") * F("rate")), Value(Decimal("0.00")), output_field=MONEY)
        )["val"]

        total_stock_qty = total_on_hand
        total_stock_val = sum(role_val.values(), ZERO)

        # Prior month comparison (ledger position at the end of last month).
        prev_qs = StockMovement.objects.filter(
            client_id=client_id, item_id__in=item_ids, movement_date__lte=last_day_prev_month,
        )
        if godown_id:
            prev_qs = prev_qs.filter(location_id=godown_id)
        prev_total = prev_qs.exclude(type="FAULTY").aggregate(
            qty=Coalesce(Sum(Coalesce("weighed_qty", "quantity")), Value(Decimal("0.0000")), output_field=QTY)
        )["qty"]
        prev_good = prev_qs.exclude(type="FAULTY").exclude(
            location__code__in=stock.BLOCKED_LOCATION_CODES
        ).aggregate(
            qty=Coalesce(Sum(Coalesce("weighed_qty", "quantity")), Value(Decimal("0.0000")), output_field=QTY)
        )["qty"]

        def calc_pct_change(curr, prev):
            curr_f = float(curr)
            prev_f = float(prev)
            if prev_f > 0:
                return round(((curr_f - prev_f) / prev_f) * 100, 1)
            return None  # no prior stock -- a percentage would be meaningless

        total_change_pct = calc_pct_change(total_stock_qty, prev_total)
        good_change_pct = calc_pct_change(good_qty, prev_good)

        tot_q = float(total_stock_qty) or 1.0
        tot_v = float(total_stock_val) or 1.0

        def dist(name, qty, val, color):
            return {
                "name": name,
                "qty": float(qty),
                "value": float(round2(val)),
                "pct": round((float(qty) / tot_q) * 100, 1) if float(total_stock_qty) > 0 else 0.0,
                "pctValue": round((float(val) / tot_v) * 100, 1) if float(total_stock_val) > 0 else 0.0,
                "color": color,
            }

        # The slices partition on-hand stock exactly.
        dist_items = [
            dist("Grey Fabric", grey_qty, grey_val, "#6366f1"),
            dist("Finished Fabric", finished_qty, finished_val, "#2563eb"),
            dist("External WIP", wip_qty, wip_val, "#8b5cf6"),
            dist("QC Hold", qc_hold_qty, qc_hold_val, "#94a3b8"),
            dist("Reprocessing", reprocess_qty, reprocess_val, "#f59e0b"),
            dist("Second Quality", second_qty, second_val, "#06b6d4"),
            dist("Rejected", rejected_qty, rejected_val, "#f43f5e"),
        ]
        if in_transit_qty > ZERO:
            dist_items.append(dist("In Transit", in_transit_qty, in_transit_val, "#0ea5e9"))

        # Stock Trend: last 6 months, the ledger position at each month end
        # split by location role.
        trend_months = []
        curr_m = today.replace(day=1)
        code_role = {code: role for code, role in role_by_code.items()}
        for i in range(5, -1, -1):
            y = curr_m.year
            m = curr_m.month - i
            while m <= 0:
                m += 12
                y -= 1
            next_m = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
            m_end = next_m - timedelta(days=1)

            m_mov = StockMovement.objects.filter(
                client_id=client_id, item_id__in=item_ids, movement_date__lte=m_end,
            ).exclude(type="FAULTY")
            if godown_id:
                m_mov = m_mov.filter(location_id=godown_id)
            by_role = defaultdict(lambda: ZERO)
            for r in ("GOOD", "EXTERNAL_WIP", "QC_HOLD", "REPROCESS", "SECOND_QUALITY", "REJECT", "IN_TRANSIT"):
                by_role[r] = ZERO
            for row in m_mov.values("location__code").annotate(
                qty=Coalesce(Sum(Coalesce("weighed_qty", "quantity")), Value(Decimal("0.0000")), output_field=QTY)
            ):
                loc_c = row["location__code"] or ""
                r = role_by_code.get(loc_c) or ("EXTERNAL_WIP" if loc_c.startswith("SYS-WIP-") else "GOOD")
                by_role[r] += D(row["qty"])
            trend_months.append({
                "month": m_end.strftime("%b"),
                "year": m_end.year,
                "date": m_end.isoformat(),
                "good": float(max(ZERO, by_role["GOOD"])),
                "greyWip": float(max(ZERO, by_role["QC_HOLD"] + by_role["EXTERNAL_WIP"])),
                "reprocessing": float(max(ZERO, by_role["REPROCESS"])),
                "secondQuality": float(max(ZERO, by_role["SECOND_QUALITY"])),
                "wastage": float(max(ZERO, by_role["REJECT"])),
                "total": float(max(ZERO, sum(by_role.values(), ZERO))),
            })

        # Category-wise Stock
        cat_stock = {}
        for b in balance_rows:
            item = item_map.get(b.item_id)
            cat_name = (
                item.fabric_quality if item and item.fabric_quality else (item.category.name if item and item.category else "Other")
            )
            cost = b.weighted_average_cost or (item.cost_price if item else ZERO)
            if cat_name not in cat_stock:
                cat_stock[cat_name] = {"qty": ZERO, "value": ZERO}
            cat_stock[cat_name]["qty"] += D(b.on_hand)
            cat_stock[cat_name]["value"] += D(b.on_hand) * D(cost)

        category_wise_stock = []
        for name, data in sorted(cat_stock.items(), key=lambda x: x[1]["qty"], reverse=True):
            category_wise_stock.append({
                "category": name,
                "qty": float(data["qty"]),
                "value": float(round2(data["value"])),
                "pct": round((float(data["qty"]) / tot_q) * 100, 1) if float(total_stock_qty) > 0 else 0.0,
            })

        # Stock by Godown -- each location's on-hand lands in exactly one
        # category (its role); wastage is the damaged (FAULTY) figure.
        stock_by_godown = []
        locations_qs = Location.objects.filter(client_id=client_id, deleted_at__isnull=True)
        if godown_id:
            locations_qs = locations_qs.filter(id=godown_id)
        loc_map = {loc.id: loc.name for loc in locations_qs}
        loc_balances = {}
        for b in balance_rows:
            lb = loc_balances.setdefault(b.location_id, {
                "total": ZERO, "good": ZERO, "externalWip": ZERO, "wastage": ZERO, "reprocess": ZERO,
                "second": ZERO, "rejected": ZERO, "grey": ZERO, "val": ZERO,
            })
            role = location_role.get(b.location_id, "GOOD")
            on_hand = D(b.on_hand)
            lb["total"] += on_hand
            lb["wastage"] += D(b.damaged)
            lb["val"] += on_hand * wac(b)
            key = {
                "GOOD": "good",
                "EXTERNAL_WIP": "externalWip",
                "QC_HOLD": "grey",
                "REPROCESS": "reprocess",
                "SECOND_QUALITY": "second",
                "REJECT": "rejected",
                "IN_TRANSIT": "inTransit",
            }.get(role, "good")
            lb[key] = lb.get(key, ZERO) + on_hand

        for lid, lname in loc_map.items():
            lb = loc_balances.get(lid)
            if lb is None:
                continue
            stock_by_godown.append({
                "locationId": str(lid),
                "godown": lname,
                "totalStock": float(lb["total"]),
                "totalValue": float(round2(lb["val"])),
                "good": float(lb.get("good", ZERO)),
                "externalWip": float(lb.get("externalWip", ZERO)),
                "wastage": float(lb.get("wastage", ZERO)),
                "reprocess": float(lb.get("reprocess", ZERO)),
                "secondQuality": float(lb.get("second", ZERO)),
                "rejected": float(lb.get("rejected", ZERO)),
                "greyWip": float(lb.get("grey", ZERO)),
            })

        # QC Summary (This Month / Selected Date Range)
        grn_date_qs = GoodsReceipt.objects.filter(
            client_id=client_id,
            deleted_at__isnull=True,
            receipt_date__range=[start_date, end_date],
        )
        if godown_id:
            grn_date_qs = grn_date_qs.filter(location_id=godown_id)

        grn_lines_qs = GoodsReceiptLine.objects.filter(
            goods_receipt__in=grn_date_qs,
            item_id__in=item_ids,
        )

        total_received_qty = grn_lines_qs.aggregate(
            q=Coalesce(Sum(Coalesce("weighed_qty", "received_qty")), Value(Decimal("0.0000")), output_field=QTY)
        )["q"]
        challans_count = grn_date_qs.count()

        qc_passed_qty = grn_lines_qs.filter(
            goods_receipt__qc_status__in=["Approved", "Partially Approved"]
        ).aggregate(
            q=Coalesce(Sum(Coalesce("weighed_qty", "received_qty") - F("rejected_qty")), Value(Decimal("0.0000")), output_field=QTY)
        )["q"]

        qc_on_hold_qty = grn_lines_qs.filter(
            goods_receipt__qc_status="On Hold"
        ).aggregate(
            q=Coalesce(Sum(Coalesce("weighed_qty", "received_qty") - F("rejected_qty")), Value(Decimal("0.0000")), output_field=QTY)
        )["q"]

        qc_rejected_qty = grn_lines_qs.aggregate(
            q=Coalesce(Sum("rejected_qty"), Value(Decimal("0.0000")), output_field=QTY)
        )["q"]

        reprocess_in_range = JWOReprocess.objects.filter(
            client_id=client_id,
            date__range=[start_date, end_date],
        ).aggregate(
            q=Coalesce(Sum("qty"), Value(Decimal("0.0000")), output_field=QTY)
        )["q"]

        tot_rec_f = float(total_received_qty) or 1.0
        qc_summary = {
            "totalReceived": {
                "qty": float(total_received_qty),
                "challans": challans_count,
                "pct": 100.0,
            },
            "qcPassed": {
                "qty": float(qc_passed_qty),
                "pct": round((float(qc_passed_qty) / tot_rec_f) * 100, 1) if float(total_received_qty) > 0 else 0.0,
            },
            "qcOnHold": {
                "qty": float(qc_on_hold_qty),
                "pct": round((float(qc_on_hold_qty) / tot_rec_f) * 100, 1) if float(total_received_qty) > 0 else 0.0,
            },
            "qcRejected": {
                "qty": float(qc_rejected_qty),
                "pct": round((float(qc_rejected_qty) / tot_rec_f) * 100, 1) if float(total_received_qty) > 0 else 0.0,
            },
            "sentForReprocessing": {
                "qty": float(reprocess_in_range),
                "pct": round((float(reprocess_in_range) / tot_rec_f) * 100, 1) if float(total_received_qty) > 0 else 0.0,
            },
        }

        # Recent Stock Movements
        recent_mov_qs = (
            StockMovement.objects.filter(client_id=client_id, item_id__in=item_ids)
            .select_related("item", "location")
            .order_by("-movement_date", "-created_at")
        )
        if godown_id:
            recent_mov_qs = recent_mov_qs.filter(location_id=godown_id)

        type_category = {
            "FAULTY": "Wastage",
            "TRANSFER_IN": "Transfer",
            "TRANSFER_OUT": "Transfer",
            "QC_PASS": "QC",
            "QC_HOLD": "QC",
            "QC_REPROCESS": "QC",
            "QC_REJECT": "QC",
            "QC_SECOND_QUALITY": "QC",
            "SAMPLE_ISSUE": "Sample",
        }
        recent_movements = []
        for m in recent_mov_qs[:12]:
            qty_val = float(m.effective_quantity)
            if m.type in type_category:
                category_label = type_category[m.type]
            elif m.type.startswith("SALES_RETURN"):
                category_label = "Customer Return"
            elif m.type.startswith("SAMPLE_RETURN") or m.reference_type in ("SampleIssue", "SampleReturn"):
                category_label = "Sample"
            else:
                category_label = "Inward" if qty_val > 0 else "Outward"

            recent_movements.append({
                "id": str(m.id),
                "date": m.movement_date.strftime("%d %b %Y"),
                "type": "GRN" if m.type == "PURCHASE" else m.type.replace("_", " ").title(),
                "refNo": m.reference_number,
                "fabric": m.item.fabric_quality or m.item.name,
                "qty": abs(qty_val),
                "category": category_label,
                "godown": m.location.name if m.location else None,
            })

        def per_unit(rows, qty_of):
            totals = {}
            for row in rows:
                label = item_unit.get(row.item_id)
                if label is None:
                    continue
                totals[label] = totals.get(label, ZERO) + D(qty_of(row))
            return [{"uom": k, "qty": float(round4(v))} for k, v in sorted(totals.items())]

        def card(qty, val, change=None, of_total=True):
            return {
                "qty": float(qty),
                "uom": dominant_uom,
                "value": float(round2(val)),
                "pct": round((float(qty) / tot_q) * 100, 1) if (of_total and float(total_stock_qty) > 0) else None,
                "changePct": change,
            }

        summary_cards = {
            "totalStock": card(total_stock_qty, total_stock_val, total_change_pct),
            "goodStock": card(good_qty, good_val, good_change_pct),
            "greyStock": card(grey_qty, grey_val),
            "finishedStock": card(finished_qty, finished_val),
            "reservedStock": card(reserved_qty, reserved_val),
            "externalWipStock": card(wip_qty, wip_val),
            "wipStock": card(wip_qty, wip_val),
            # Samples are out with customers and returns are already inside
            # the location categories: neither is a share of on-hand stock.
            "sampleStock": card(sample_qty, sample_val, of_total=False),
            "wastageStock": card(wastage_qty, wastage_val, of_total=False),
            "reprocessingStock": card(reprocess_qty, reprocess_val),
            "secondQualityStock": card(second_qty, second_val),
            "customerReturnStock": card(customer_return_qty, customer_return_val, of_total=False),
            "qcHoldStock": card(qc_hold_qty, qc_hold_val),
            "rejectedStock": card(rejected_qty, rejected_val),
            "inTransitStock": card(in_transit_qty, in_transit_val),
        }

        # 10 Real Location/Status-based Stock Buckets (Phase 2.3)
        stock_buckets = {
            "grey": card(grey_qty, grey_val),
            "externalWip": {
                **card(wip_qty, wip_val),
                "byParty": wip_by_party,
            },
            "qcHold": card(qc_hold_qty, qc_hold_val),
            "reprocess": card(reprocess_qty, reprocess_val),
            "finished": card(finished_qty, finished_val),
            "reserved": card(reserved_qty, reserved_val),
            "secondQuality": card(second_qty, second_val),
            "reject": card(rejected_qty, rejected_val),
            "inTransit": card(in_transit_qty, in_transit_val),
            "sample": card(sample_qty, sample_val, of_total=False),
            "customerReturn": card(customer_return_qty, customer_return_val, of_total=False),
        }

        # Per-item figures for the quality table and the records below.
        items_with_stock = list(item_map.values())
        stock.annotate_items_with_stock(client_id, items_with_stock)
        sample_out_by_item = {}
        for m in sample_issue_qs:
            sample_out_by_item[m.item_id] = sample_out_by_item.get(m.item_id, ZERO) + abs(D(m.quantity))
        for row in sample_return_qs.filter(source_document_id__in=sample_issue_qs.values("id")).values("item_id").annotate(q=Sum("quantity")):
            sample_out_by_item[row["item_id"]] = sample_out_by_item.get(row["item_id"], ZERO) - D(row["q"] or 0)
        return_by_item = {
            row["item_id"]: D(row["q"] or 0)
            for row in ret_lines.values("item_id").annotate(q=Sum("returned_qty"))
        }

        # 1. Quality-wise stock breakdown (for Fabric Stock Overview table)
        quality_stock = []
        quality_groups = {}
        for item in item_map.values():
            quality_groups.setdefault(item.fabric_quality or item.name, []).append(item)
        for q_name, group in sorted(quality_groups.items(), key=lambda kv: kv[0] or ""):
            ids = {i.id for i in group}
            by_role = defaultdict(lambda: ZERO, {role: ZERO for role in role_qty})
            damaged = ZERO
            for b in balance_rows:
                if b.item_id in ids:
                    by_role[location_role.get(b.location_id, "GOOD")] += D(b.on_hand)
                    damaged += D(b.damaged)
            total = sum(by_role.values(), ZERO)
            if total == ZERO and damaged == ZERO:
                continue
            quality_stock.append({
                "id": str(group[0].id),
                "qualityCode": group[0].sku if len(group) == 1 else None,
                "qualityName": q_name,
                "goodStock": float(round4(by_role["GOOD"])),
                "sample": float(round4(max(ZERO, sum((sample_out_by_item.get(i, ZERO) for i in ids), ZERO)))),
                "wastage": float(round4(damaged)),
                "reprocess": float(round4(by_role["REPROCESS"])),
                "secondQuality": float(round4(by_role["SECOND_QUALITY"])),
                "return": float(round4(sum((return_by_item.get(i, ZERO) for i in ids), ZERO))),
                "qcHold": float(round4(by_role["QC_HOLD"])),
                "rejected": float(round4(by_role["REJECT"])),
                "inTransit": float(round4(by_role["IN_TRANSIT"])),
                "total": float(round4(total)),
            })

        # 2. Sample Stock Records
        sample_issues_qs = StockMovement.objects.filter(
            client_id=client_id,
            item_id__in=item_ids,
            reference_type="SampleIssue",
            quantity__lt=0,
        ).select_related("item", "location", "lot_taka").order_by("-movement_date", "-created_at")

        sample_returns_all = StockMovement.objects.filter(
            client_id=client_id,
            item_id__in=item_ids,
            reference_type="SampleReturn",
            quantity__gt=0,
        ).select_related("location").order_by("movement_date", "created_at")

        return_map = {}
        return_history_map = {}
        for r in sample_returns_all:
            s_doc_id = str(r.source_document_id) if r.source_document_id else ""
            if not s_doc_id:
                continue
            return_map[s_doc_id] = return_map.get(s_doc_id, ZERO) + D(r.quantity or 0)
            note = r.notes or ""
            condition = note[1:note.index("]")] if note.startswith("[") and "]" in note else None
            return_history_map.setdefault(s_doc_id, []).append({
                "id": str(r.id),
                "returnNo": r.reference_number,
                "qty": float(r.quantity),
                "uom": r.uom,
                "date": r.movement_date.strftime("%d-%m-%Y"),
                "condition": condition,
                "location": r.location.name if r.location_id else None,
                "remarks": note[note.index("]") + 1:].strip() if condition else note,
            })

        sample_records = []
        for m in sample_issues_qs[:100]:
            it = m.item
            qty_val = abs(D(m.quantity))
            ret_val = return_map.get(str(m.id), ZERO)
            rem_val = max(ZERO, qty_val - ret_val)

            cust_name = None
            expected_date = ""
            purpose = m.notes or ""
            if m.notes and m.notes.strip().startswith("{") and m.notes.strip().endswith("}"):
                try:
                    meta = json.loads(m.notes)
                    cust_name = meta.get("customer") or None
                    expected_date = meta.get("expectedReturnDate") or ""
                    purpose = meta.get("purpose") or ""
                except ValueError:
                    pass

            if ret_val <= ZERO:
                s_status = "Issued"
            elif rem_val <= ZERO:
                s_status = "Returned"
            else:
                s_status = "Partial"
            if expected_date and rem_val > ZERO:
                try:
                    if date.fromisoformat(expected_date) < today:
                        s_status = "Overdue"
                except ValueError:
                    pass

            sample_records.append({
                "id": str(m.id),
                "issueNo": m.reference_number,
                "itemCode": it.sku if it else None,
                "itemName": it.name if it else None,
                "quality": (it.fabric_quality or it.name) if it else None,
                "design": (it.fabric_design or None) if it else None,
                "shade": (it.fabric_color or None) if it else None,
                "lotNo": m.batch_number or None,
                "takaId": (m.lot_taka.taka_id or m.lot_taka.barcode) if m.lot_taka_id else None,
                "locationId": str(m.location_id),
                "issuedQty": float(qty_val),
                "returnedQty": float(ret_val),
                "remainingQty": float(rem_val),
                "customer": cust_name,
                "issueDate": m.movement_date.strftime("%d-%m-%Y"),
                "rawDate": m.movement_date.isoformat(),
                "expectedReturnDate": expected_date,
                "availableGoodStock": float(getattr(item_map.get(m.item_id), "available_qty", ZERO) or ZERO),
                "status": s_status,
                "uom": m.uom or (it.uom if it else None),
                "remarks": purpose,
                "history": return_history_map.get(str(m.id), []),
            })

        # 3. Wastage / Reject Stock Records: FAULTY (damaged) movements plus
        # everything routed to the Reject location.
        wastage_records = []
        waste_movs = (
            StockMovement.objects.filter(client_id=client_id, item_id__in=item_ids)
            .filter(Q(type="FAULTY") | Q(location__code=stock.SYSTEM_LOCATIONS["REJECT"][0], quantity__gt=0))
            .select_related("item", "location")
            .order_by("-movement_date", "-created_at")
        )
        if godown_id:
            waste_movs = waste_movs.filter(location_id=godown_id)
        for m in waste_movs[:50]:
            it = m.item
            wastage_records.append({
                "id": str(m.id),
                "itemCode": it.sku if it else None,
                "quality": (it.fabric_quality or it.name) if it else None,
                "design": (it.fabric_design or None) if it else None,
                "shade": (it.fabric_color or None) if it else None,
                "lotNo": m.batch_number or None,
                "quantity": abs(float(m.effective_quantity)),
                "uom": m.uom or (it.uom if it else None),
                "reason": m.notes or None,
                "type": m.type,
                "sourceDoc": m.reference_number,
                "date": m.movement_date.strftime("%d-%m-%Y"),
            })

        # 4. Good Fabric Stock Records (taka-level from LotTaka at good
        # locations, plus balances not held on takas)
        active_takas = list(
            LotTaka.objects.filter(
                client_id=client_id,
                deleted_at__isnull=True,
                status="Active",
                item_id__in=item_ids,
                available_quantity__gt=0,
            ).select_related("item", "location")
        )
        taka_by_item_loc = {}
        for lt in active_takas:
            taka_by_item_loc.setdefault((lt.item_id, lt.location_id), []).append(lt)

        def lot_status(item, qty):
            reorder = D(getattr(item, "reorder_level", 0) or 0)
            if qty <= ZERO:
                return "Nil"
            return "Low Stock" if reorder > ZERO and qty <= reorder else "Available"

        good_records = []
        for b in balance_rows:
            if D(b.on_hand) <= ZERO or location_role.get(b.location_id, "GOOD") != "GOOD":
                continue
            it = b.item
            takas = taka_by_item_loc.get((b.item_id, b.location_id), [])
            on_takas = ZERO
            for lt in takas:
                lt_avail = D(lt.available_quantity)
                on_takas += lt_avail
                good_records.append({
                    "id": str(lt.id),
                    "takaId": lt.taka_id or lt.barcode,
                    "itemCode": it.sku if it else None,
                    "quality": (it.fabric_quality or it.name) if it else None,
                    "design": (it.fabric_design or None) if it else None,
                    "shade": (it.fabric_color or None) if it else None,
                    "lotNo": f"{lt.lot_number} / #{lt.taka_number}",
                    "totalQty": float(lt.received_quantity),
                    "available": float(lt_avail),
                    "reserved": None,
                    "uom": lt.uom or (it.uom if it else None),
                    "godown": lt.location.name if lt.location else None,
                    "status": lot_status(it, lt_avail),
                })
            loose = D(b.on_hand) - on_takas
            if loose > ZERO:
                good_records.append({
                    "id": str(b.id),
                    "takaId": None,
                    "itemCode": it.sku if it else None,
                    "quality": (it.fabric_quality or it.name) if it else None,
                    "design": (it.fabric_design or None) if it else None,
                    "shade": (it.fabric_color or None) if it else None,
                    "lotNo": None,
                    "totalQty": float(loose),
                    "available": float(loose),
                    "reserved": None,
                    "uom": it.uom if it else None,
                    "godown": b.location.name if b.location else None,
                    "status": lot_status(it, loose),
                })

        # 5. Reprocessing Stock Records (reprocess orders)
        reprocess_records = []
        for rp in JWOReprocess.objects.filter(client_id=client_id).select_related(
            "job_work_order", "job_work_order__vendor", "job_work_order__process_plan"
        ).order_by("-date", "-created_at")[:50]:
            jwo = rp.job_work_order
            plan = jwo.process_plan if jwo else None
            sent_q = float(rp.qty or 0)
            rec_q = sent_q if rp.status in ["Received", "Completed"] else 0.0
            reprocess_records.append({
                "id": str(rp.id),
                "reprocessNo": rp.no,
                "itemCode": (plan.fabric_item or None) if plan else None,
                "quality": (plan.fabric_quality or plan.fabric_item or None) if plan else None,
                "design": None,
                "shade": (plan.shade or None) if plan else None,
                "lotNo": (plan.grey_lot_no or None) if plan else None,
                "sentQty": sent_q,
                "receivedQty": rec_q,
                "pendingQty": max(0.0, sent_q - rec_q),
                "process": (jwo.process or None) if jwo else None,
                "vendor": ((jwo.vendor_name or (jwo.vendor.name if jwo.vendor else None)) if jwo else None),
                "status": rp.status,
            })

        # 6. Customer Return Stock Records
        return_records = []
        for ret in ret_lines.select_related("item", "sales_return", "sales_return__party")[:50]:
            it = ret.item
            return_records.append({
                "id": str(ret.id),
                "returnNo": ret.sales_return.return_number if ret.sales_return_id else None,
                "itemCode": it.sku if it else None,
                "quality": (it.fabric_quality or it.name) if it else None,
                "design": (it.fabric_design or None) if it else None,
                "shade": (it.fabric_color or None) if it else None,
                "lotNo": None,
                "returnQty": float(ret.returned_qty or 0),
                "uom": ret.uom or (it.uom if it else None),
                "reason": getattr(ret, "reason", None) or None,
                "qcStatus": getattr(ret, "condition", None),
                "customer": ret.sales_return.party.name if (ret.sales_return_id and ret.sales_return.party_id) else None,
                "date": ret.sales_return.doc_date.strftime("%d-%m-%Y") if ret.sales_return_id and ret.sales_return.doc_date else "",
            })

        on_hand_by_uom = per_unit(balance_rows, lambda b: b.on_hand)
        damaged_by_uom = per_unit(balance_rows, lambda b: b.damaged)
        if len(on_hand_by_uom) <= 1:
            mixed_units = False
            dominant_uom = on_hand_by_uom[0]["uom"] if on_hand_by_uom else "Mtr"
            if dominant_uom in ("Meter", "meter"):
                dominant_uom = "Mtr"
            for key, card_row in summary_cards.items():
                card_row["uom"] = dominant_uom
            summary_cards["totalStock"]["byUom"] = on_hand_by_uom
            summary_cards["wastageStock"]["byUom"] = damaged_by_uom
        elif mixed_units:
            for key, card_row in summary_cards.items():
                card_row["qty"] = None
                card_row["changePct"] = None
                card_row["byUom"] = []
            summary_cards["totalStock"]["byUom"] = on_hand_by_uom
            summary_cards["wastageStock"]["byUom"] = damaged_by_uom

        # Takas available for sample issue: good locations only.
        available_lots = [
            {
                "id": str(lt.id),
                "lotTakaId": str(lt.id),
                "takaId": lt.taka_id or lt.barcode,
                "itemId": str(lt.item_id),
                "sku": lt.item.sku if lt.item else None,
                "name": lt.item.name if lt.item else None,
                "quality": (lt.item.fabric_quality or lt.item.name) if lt.item else None,
                "design": (lt.item.fabric_design or None) if lt.item else None,
                "shade": (lt.item.fabric_color or None) if lt.item else None,
                "uom": lt.uom or (lt.item.uom if lt.item else None),
                "locationId": str(lt.location_id),
                "godown": lt.location.name if lt.location else None,
                "available": float(lt.available_quantity),
                "lotNo": f"{lt.lot_number} / #{lt.taka_number}",
            }
            for lt in active_takas
            if location_role.get(lt.location_id, "GOOD") == "GOOD"
        ]
        # Items with stock not held on takas are issued by location only.
        for b in balance_rows:
            if location_role.get(b.location_id, "GOOD") != "GOOD" or D(b.on_hand) <= ZERO:
                continue
            on_takas = sum((D(lt.available_quantity) for lt in taka_by_item_loc.get((b.item_id, b.location_id), [])), ZERO)
            loose = D(b.on_hand) - on_takas
            if loose > ZERO:
                available_lots.append({
                    "id": None,
                    "lotTakaId": None,
                    "takaId": None,
                    "itemId": str(b.item_id),
                    "sku": b.item.sku if b.item else None,
                    "name": b.item.name if b.item else None,
                    "quality": (b.item.fabric_quality or b.item.name) if b.item else None,
                    "design": (b.item.fabric_design or None) if b.item else None,
                    "shade": (b.item.fabric_color or None) if b.item else None,
                    "uom": b.item.uom if b.item else None,
                    "locationId": str(b.location_id),
                    "godown": b.location.name if b.location else None,
                    "available": float(loose),
                    "lotNo": None,
                })

        # Combined Grey Fabric Stock metrics & alert status
        grey_calc = stock.calculate_combined_grey_stock(client_id, location_id=godown_id)
        grey_setting = stock.get_combined_grey_stock_setting(client_id)
        threshold = grey_setting["threshold"]
        avail_grey = grey_calc["available_grey_stock"]

        shortage = ZERO
        if not grey_setting["is_configured"]:
            status_val = "Unconfigured"
        elif not grey_setting["is_enabled"]:
            status_val = "Disabled"
        elif avail_grey < threshold:
            shortage = round4(threshold - avail_grey)
            status_val = "Critical" if avail_grey <= ZERO else "Low Stock"
        else:
            status_val = "Sufficient"

        combined_grey_stock = {
            "physicalGrey": float(grey_calc["physical_grey_stock"]),
            "reservedGrey": float(grey_calc["reserved_grey_stock"]),
            "availableGrey": float(grey_calc["available_grey_stock"]),
            "externalWip": float(grey_calc["external_wip"]),
            "wipByParty": grey_calc["wip_by_party"],
            "uom": grey_calc["uom"],
            "qualitiesCount": grey_calc["qualities_count"],
            "takasCount": grey_calc["takas_count"],
            "threshold": float(threshold),
            "shortage": float(shortage),
            "status": status_val,
            "isConfigured": grey_setting["is_configured"],
            "isEnabled": grey_setting["is_enabled"],
            "channels": grey_setting["channels"],
            "incompatibleItems": grey_calc["incompatible_items"],
        }

        return Response({
            "units": {"inScope": units_in_scope, "mixed": mixed_units},
            "filters": {
                "godowns": godowns_list,
                "categories": categories_list,
                "fabrics": fabrics_list,
                "colours": colours_list,
                "dateRange": {
                    "from": start_date.isoformat(),
                    "to": end_date.isoformat(),
                },
                "availableLots": available_lots,
            },
            "stockBuckets": stock_buckets,
            "summaryCards": summary_cards,
            "stockDistribution": dist_items,
            "stockTrend": trend_months,
            "categoryWiseStock": category_wise_stock,
            "stockByGodown": stock_by_godown,
            "qcSummary": qc_summary,
            "recentMovements": recent_movements,
            "qualityStock": quality_stock,
            "sampleRecords": sample_records,
            "wastageRecords": wastage_records,
            "goodRecords": good_records,
            "reprocessRecords": reprocess_records,
            "returnRecords": return_records,
            "combinedGreyStock": combined_grey_stock,
        })

    @transaction.atomic
    def post(self, request):
        action = request.data.get("action") or "issue_sample"
        if action == "issue_sample":
            return self._issue_sample(request)
        if action == "return_sample":
            return self._return_sample(request)
        raise ValidationFailed("Invalid action.", field_errors={"action": ["Expected issue_sample or return_sample."]})

    def _issue_sample(self, request):
        """A sample cut from good stock: SAMPLE_ISSUE out of the godown (and
        off the taka, when one is named or the item is taka-tracked)."""
        client_id = request.client_id
        data = request.data
        field_errors = {}
        item = Item.objects.filter(client_id=client_id, id=data.get("itemId"), deleted_at__isnull=True).first()
        if item is None:
            field_errors["itemId"] = ["Choose the item."]
        customer_name = (data.get("customer") or "").strip()
        if not customer_name:
            field_errors["customer"] = ["Who is the sample issued to?"]
        try:
            qty = round4(D(data.get("qty")))
        except (InvalidOperation, TypeError, ValueError) as exc:
            logger.exception("Failed parsing scrap disposal quantity: %s", exc)
            qty = ZERO
        if qty <= ZERO:
            field_errors["qty"] = ["Enter a quantity greater than zero."]
        lot = None
        if data.get("lotTakaId"):
            lot = LotTaka.objects.filter(
                client_id=client_id, id=data.get("lotTakaId"), deleted_at__isnull=True
            ).first()
            if lot is None:
                field_errors["lotTakaId"] = ["That taka does not exist."]
        location = None
        location_id = (lot.location_id if lot else None) or data.get("locationId")
        if location_id:
            location = Location.objects.filter(client_id=client_id, id=location_id).first()
        if location is None:
            field_errors["locationId"] = ["Choose the godown the sample is cut from."]
        if field_errors:
            raise ValidationFailed("Sample issue is incomplete.", field_errors=field_errors)
        if location.code in stock.BLOCKED_LOCATION_CODES:
            raise ValidationFailed(
                "Samples are issued from good stock only.",
                field_errors={"locationId": [f"{location.name} is not a good-stock location."]},
            )

        stock.assert_sufficient_stock(
            client_id=client_id, item=item, quantity=qty, location_id=location.id,
            label=f"{item.name} ({lot.lot_number if lot else location.name})",
        )
        issue_date = timezone.localdate()
        issue_ref = allocate_number(request.user.client, "SMPL", issue_date)
        import uuid as _uuid

        mov = stock.post_movement(
            client_id=client_id,
            item=item,
            location=location,
            type="SAMPLE_ISSUE",
            quantity=-qty,
            unit_cost=D(lot.rate) if lot and lot.rate else item.cost_price,
            reference_type="SampleIssue",
            reference_id=_uuid.uuid4(),
            reference_number=issue_ref,
            batch_number=lot.lot_number if lot else (data.get("lotNo") or None),
            lot_taka=lot,
            movement_date=issue_date,
            notes=json.dumps({
                "customer": customer_name,
                "expectedReturnDate": data.get("expectedReturnDate") or "",
                "purpose": data.get("notes") or "",
            }),
            user=request.user,
        )
        return Response({
            "success": True,
            "message": f"Sample {issue_ref}: {qty} {item.uom} issued to {customer_name}.",
            "movementId": str(mov.id),
            "issueNo": issue_ref,
        })

    def _return_sample(self, request):
        """Route a returned sample by condition (Phase 1.4): Good -> back to the
        godown it came from, Hold -> QC Hold, Reprocess -> Reprocess, Second
        Quality -> Second Quality, Reject/Scrap -> Reject."""
        client_id = request.client_id
        data = request.data
        movement_id = data.get("movementId") or data.get("issueId")
        try:
            return_qty = round4(D(data.get("returnQty")))
        except (InvalidOperation, TypeError, ValueError) as exc:
            logger.exception("Failed parsing return quantity: %s", exc)
            return_qty = ZERO
        if return_qty <= ZERO:
            raise ValidationFailed(
                "Return quantity must be greater than 0.",
                field_errors={"returnQty": ["Must be greater than zero."]},
            )
        condition = stock.normalize_condition(data.get("condition"))
        remarks = (data.get("remarks") or "").strip()

        orig_mov = StockMovement.objects.select_related("item", "lot_taka").filter(
            client_id=client_id, id=movement_id, reference_type="SampleIssue"
        ).first()
        if orig_mov is None:
            raise ValidationFailed(
                "Original sample issue not found.", field_errors={"movementId": ["Not found."]}
            )

        already_returned = StockMovement.objects.filter(
            client_id=client_id,
            reference_type="SampleReturn",
            source_document_id=orig_mov.id,
            quantity__gt=0,
        ).aggregate(tot=Sum("quantity"))["tot"] or ZERO
        remaining = abs(D(orig_mov.quantity)) - D(already_returned)
        if return_qty > remaining:
            raise ValidationFailed(
                f"Return quantity ({return_qty}) cannot exceed the outstanding "
                f"quantity ({remaining} {orig_mov.uom or ''}).",
                field_errors={"returnQty": [f"At most {remaining}."]},
            )

        destination = stock.condition_location_id(client_id, condition, orig_mov.location_id)
        _, lot_qc_status = stock.CONDITION_ROUTING[condition]
        movement_type = {
            "Good": "SAMPLE_RETURN",
            "Hold": "SAMPLE_RETURN_HOLD",
            "Reprocess": "SAMPLE_RETURN_REPROCESS",
            "Second Quality": "SAMPLE_RETURN_SECOND_QUALITY",
            "Reject": "SAMPLE_RETURN_REJECT",
        }[condition]
        return_no = allocate_number(request.user.client, "SMPR")
        common = dict(
            client_id=client_id,
            reference_type="SampleReturn",
            reference_id=orig_mov.id,
            reference_number=return_no,
            source_document_type="StockMovement",
            source_document_id=orig_mov.id,
            movement_date=timezone.localdate(),
            notes=f"[{condition}] {remarks}".strip(),
            user=request.user,
        )
        if orig_mov.lot_taka_id:
            _, ret_mov = stock.receive_into_lot(
                lot=orig_mov.lot_taka, quantity=return_qty, location_id=destination,
                type=movement_type, qc_status=lot_qc_status, split_suffix="SR",
                unit_cost=orig_mov.unit_cost, **common,
            )
        else:
            ret_mov = stock.post_movement(
                item=orig_mov.item_id, location=destination, type=movement_type,
                quantity=return_qty, unit_cost=orig_mov.unit_cost,
                batch_number=orig_mov.batch_number, **common,
            )
        return Response({
            "success": True,
            "message": f"Sample return {return_no}: {return_qty} {orig_mov.uom or ''} routed as {condition}.",
            "movementId": str(ret_mov.id),
            "returnNo": return_no,
            "condition": condition,
            "remainingOutstanding": float(remaining - return_qty),
        })


class StockAgeingView(APIView):
    """
    GET /inventory/stock/ageing/
    Real ageing breakdown into buckets (0-30, 31-60, 61-90, 91-180, 180+) computed
    from received_date of each LotTaka (PDF §07, Phase 2.3).
    """
    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]

    def get(self, request):
        loc_id = request.query_params.get("locationId")
        item_id = request.query_params.get("itemId")
        data = stock.get_stock_ageing(request.client_id, location_id=loc_id, item_id=item_id)
        return Response(data)


class SlowMovingStockView(APIView):
    """
    GET /inventory/stock/slow-moving/
    List takas held longer than threshold days with zero recent movements (Phase 2.3).
    """
    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]

    def get(self, request):
        threshold = int(request.query_params.get("thresholdDays") or 60)
        data = stock.get_slow_moving_stock(request.client_id, threshold_days=threshold)
        return Response(data)

