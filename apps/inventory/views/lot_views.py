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


def _is_uuid(value):
    import uuid as _uuid

    try:
        _uuid.UUID(str(value))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


class LotTakaRegisterView(APIView):
    """Lot / Taka Register endpoints for fabric lots, quantities, and movement history."""
    permission_classes = [HasModulePermission]
    module_name = "inventory"
    required_permissions = ["view_inventory"]
    permission_map = {"POST": ["adjust_stock"], "PATCH": ["view_inventory"]}

    def get(self, request, pk=None):
        client_id = request.client_id

        # Query parameters
        from_date_str = request.query_params.get("from_date")
        to_date_str = request.query_params.get("to_date")
        supplier_filter = request.query_params.get("supplier")
        quality_filter = request.query_params.get("quality")
        design_filter = request.query_params.get("design")
        shade_filter = request.query_params.get("shade")
        status_filter = request.query_params.get("status")
        qc_status_filter = request.query_params.get("qc_status")
        location_filter = request.query_params.get("location")
        search_query = request.query_params.get("search", "").strip()
        item_id_preview = request.query_params.get("item_id")

        if pk:
            lt = LotTaka.objects.filter(client_id=client_id, id=pk, deleted_at__isnull=True).select_related("item", "supplier", "location").first()
            if not lt:
                return Response({"detail": "Lot / Taka not found."}, status=status.HTTP_404_NOT_FOUND)
            # Fetch movement history
            # This taka's own ledger (every lot movement names its taka).
            mov_qs = StockMovement.objects.filter(
                client_id=client_id, lot_taka_id=lt.id
            ).order_by("movement_date", "created_at")
            running_bal = Decimal("0")
            mov_list = []
            for m in mov_qs:
                running_bal += m.quantity
                m_type = m.type
                if m.type in ["PURCHASE", "ZONE_RECEIPT"] or m.reference_type in ["GRN", "LotRegister"]:
                    m_type_label = "GRN / Inward"
                elif m.type == "ZONE_ISSUE":
                    m_type_label = "Issue Sample" if m.reference_type == "SampleIssue" else "Stock Issue"
                elif m.type == "ZONE_RETURN":
                    m_type_label = "Sample Return" if m.reference_type == "SampleReturn" else "Stock Return"
                else:
                    m_type_label = m.type

                mov_list.append({
                    "date": m.movement_date.strftime("%d-%m-%Y"),
                    "type": m_type_label,
                    "qty": float(m.quantity),
                    "balance": float(running_bal),
                    "refNo": m.reference_number,
                })

            it = lt.item
            return Response({
                "id": str(lt.id),
                "takaId": lt.taka_id or lt.barcode,
                "lotNo": lt.lot_number,
                "takaRoll": lt.taka_number,
                "itemId": str(lt.item_id),
                "tagStatus": lt.tag_status or "Pending",
                "printCount": lt.print_count or 0,
                "lastPrintedAt": lt.last_printed_at.strftime("%d-%m-%Y %H:%M") if lt.last_printed_at else None,
                "itemCode": it.sku if it else "-",
                "itemName": it.name if it else None,
                "quality": (it.fabric_quality or it.name) if it else None,
                "design": (it.fabric_design or None) if it else None,
                "shade": (it.fabric_color or None) if it else None,
                "receivedQty": float(lt.received_quantity),
                "qcStatus": lt.qc_status,
                "availableQty": float(lt.available_quantity),
                "location": lt.location.name if lt.location else None,
                "locationId": str(lt.location.id) if lt.location else "",
                "supplier": lt.supplier.name if lt.supplier else (lt.source_document_number or "-"),
                "supplierId": str(lt.supplier.id) if lt.supplier else "",
                "receivedDate": lt.received_date.strftime("%d-%m-%Y"),
                "rawDate": lt.received_date.isoformat(),
                "uom": lt.uom or (it.uom if it else None),
                "status": lt.status,
                "rate": float(lt.rate),
                "rollWeight": float(lt.roll_weight) if lt.roll_weight else None,
                "width": float(lt.width) if lt.width else None,
                "pieces": lt.pieces,
                "gsm": float(lt.gsm) if lt.gsm else None,
                "remarks": lt.remarks or "-",
                "barcode": lt.barcode or f"{lt.lot_number}-{lt.taka_number}",
                "tags": lt.tags or [],
                "imageUrl": lt.image_url or "",
                "sourceDoc": lt.source_document_type,
                "grnNo": lt.source_document_number or "-",
                "stockMovements": mov_list,
            })

        # Base queryset
        qs = LotTaka.objects.filter(client_id=client_id, deleted_at__isnull=True).select_related(
            "item", "supplier", "location", "created_by", "updated_by"
        )

        # Filters
        if from_date_str:
            try:
                from_d = date.fromisoformat(from_date_str)
                qs = qs.filter(received_date__gte=from_d)
            except (ValueError, TypeError) as exc:
                logger.exception("Invalid from_date parameter in LotTakaRegisterView: %s", exc)

        if to_date_str:
            try:
                to_d = date.fromisoformat(to_date_str)
                qs = qs.filter(received_date__lte=to_d)
            except (ValueError, TypeError) as exc:
                logger.exception("Invalid to_date parameter in LotTakaRegisterView: %s", exc)

        if supplier_filter and supplier_filter != "All":
            qs = qs.filter(supplier__name=supplier_filter)

        if quality_filter and quality_filter != "All":
            qs = qs.filter(item__fabric_quality=quality_filter)

        if design_filter and design_filter != "All":
            qs = qs.filter(item__fabric_design=design_filter)

        if shade_filter and shade_filter != "All":
            qs = qs.filter(item__fabric_color=shade_filter)

        if status_filter and status_filter != "All":
            qs = qs.filter(status=status_filter)

        if qc_status_filter and qc_status_filter != "All":
            qs = qs.filter(qc_status=qc_status_filter)

        if location_filter and location_filter != "All":
            qs = qs.filter(location__name=location_filter)

        # Used by the transfer taka picker: takas with stock at one location.
        location_id_filter = request.query_params.get("location_id")
        if location_id_filter:
            qs = qs.filter(location_id=location_id_filter)
        if request.query_params.get("available_only") == "true":
            qs = qs.filter(available_quantity__gt=0, deleted_at__isnull=True)
        item_filter = request.query_params.get("itemId")
        if item_filter:
            qs = qs.filter(item_id=item_filter)

        grn_filter = (
            request.query_params.get("grn")
            or request.query_params.get("grn_no")
            or request.query_params.get("grn_number")
            or request.query_params.get("grnNumber")
        )
        if grn_filter and grn_filter != "All":
            grn_q = Q(source_document_number__iexact=grn_filter) | Q(source_document_number__icontains=grn_filter)
            try:
                grn_uuid = uuid.UUID(str(grn_filter))
                grn_q |= Q(source_document_id=grn_uuid)
            except (ValueError, TypeError, AttributeError) as exc:
                logger.exception("GRN filter value %r is not a valid UUID: %s", grn_filter, exc)
            qs = qs.filter(grn_q)

        po_filter = (
            request.query_params.get("po")
            or request.query_params.get("po_id")
            or request.query_params.get("po_number")
            or request.query_params.get("poNumber")
            or request.query_params.get("purchaseOrderId")
        )
        if po_filter and po_filter != "All":
            from apps.purchase.models import GoodsReceipt
            po_q = Q(purchase_order_id=po_filter) | Q(purchase_order__po_number__iexact=po_filter)
            try:
                po_uuid = uuid.UUID(str(po_filter))
                po_q |= Q(purchase_order_id=po_uuid)
            except (ValueError, TypeError, AttributeError) as exc:
                logger.exception("PO filter value %r is not a valid UUID: %s", po_filter, exc)
            linked_grns = list(GoodsReceipt.objects.filter(client_id=client_id, deleted_at__isnull=True).filter(po_q).values_list("id", "grn_number"))
            grn_ids = [g[0] for g in linked_grns]
            grn_nums = [g[1] for g in linked_grns if g[1]]
            qs = qs.filter(
                Q(source_document_id__in=grn_ids)
                | Q(source_document_number__in=grn_nums)
                | Q(source_document_number__iexact=str(po_filter))
            )

        if search_query:
            qs = qs.filter(
                Q(lot_number__icontains=search_query)
                | Q(taka_number__icontains=search_query)
                | Q(item__sku__icontains=search_query)
                | Q(item__name__icontains=search_query)
                | Q(supplier__name__icontains=search_query)
            )

        all_lots = list(qs)

        # Preload movements for movement history per lot
        movements_by_lot = {}
        if all_lots:
            mov_qs = StockMovement.objects.filter(
                client_id=client_id, lot_taka_id__in=[lt.id for lt in all_lots]
            ).order_by("movement_date", "created_at")
            for m in mov_qs:
                movements_by_lot.setdefault(m.lot_taka_id, []).append(m)

        # Build records
        records = []
        tot_received = Decimal("0")
        tot_available = Decimal("0")
        tot_value = Decimal("0")
        pending_qc_qty = Decimal("0")
        pending_qc_lots = set()
        on_hold_qty = Decimal("0")
        on_hold_lots = set()
        active_count = 0
        closed_count = 0

        for lt in all_lots:
            it = lt.item
            rec_q = Decimal(str(lt.received_quantity or 0))
            avail_q = Decimal(str(lt.available_quantity or 0))
            rate_val = Decimal(str(lt.rate or 0))
            val = rec_q * rate_val

            tot_received += rec_q
            tot_available += avail_q
            tot_value += val

            if lt.status == "Active":
                active_count += 1
            else:
                closed_count += 1

            if lt.qc_status == "Pending":
                pending_qc_qty += rec_q
                pending_qc_lots.add(lt.lot_number)
            elif lt.qc_status == "On Hold":
                on_hold_qty += rec_q
                on_hold_lots.add(lt.lot_number)

            # Build movement history for this lot
            lot_movs = movements_by_lot.get(lt.id, [])
            running_bal = Decimal("0")
            mov_list = []
            for m in lot_movs:
                running_bal += m.quantity
                m_type = m.type
                if m.type in ["PURCHASE", "ZONE_RECEIPT"] or m.reference_type in ["GRN", "LotRegister"]:
                    m_type_label = "GRN / Inward"
                elif m.type == "ZONE_ISSUE":
                    m_type_label = "Issue Sample" if m.reference_type == "SampleIssue" else "Stock Issue"
                elif m.type == "ZONE_RETURN":
                    m_type_label = "Sample Return" if m.reference_type == "SampleReturn" else "Stock Return"
                else:
                    m_type_label = m.type

                mov_list.append({
                    "date": m.movement_date.strftime("%d-%m-%Y"),
                    "type": m_type_label,
                    "qty": float(m.quantity),
                    "balance": float(running_bal),
                    "refNo": m.reference_number,
                })

            records.append({
                "id": str(lt.id),
                "takaId": lt.taka_id or lt.barcode,
                "lotNo": lt.lot_number,
                "takaRoll": lt.taka_number,
                "itemId": str(lt.item_id),
                "tagStatus": lt.tag_status or "Pending",
                "printCount": lt.print_count or 0,
                "lastPrintedAt": lt.last_printed_at.strftime("%d-%m-%Y %H:%M") if lt.last_printed_at else None,
                "itemCode": it.sku if it else "-",
                "itemName": it.name if it else None,
                "quality": (it.fabric_quality or it.name) if it else None,
                "design": (it.fabric_design or None) if it else None,
                "shade": (it.fabric_color or None) if it else None,
                "receivedQty": float(rec_q),
                "qcStatus": lt.qc_status,
                "availableQty": float(avail_q),
                "location": lt.location.name if lt.location else None,
                "locationId": str(lt.location.id) if lt.location else "",
                "supplier": lt.supplier.name if lt.supplier else (lt.source_document_number or "-"),
                "supplierId": str(lt.supplier.id) if lt.supplier else "",
                "receivedDate": lt.received_date.strftime("%d-%m-%Y"),
                "rawDate": lt.received_date.isoformat(),
                "uom": lt.uom or (it.uom if it else None),
                "status": lt.status,
                "rate": float(rate_val),
                "rollWeight": float(lt.roll_weight) if lt.roll_weight else None,
                "width": float(lt.width) if lt.width else None,
                "pieces": lt.pieces,
                "gsm": float(lt.gsm) if lt.gsm else None,
                "remarks": lt.remarks or "-",
                "barcode": lt.barcode or f"{lt.lot_number}-{lt.taka_number}",
                "tags": lt.tags or [],
                "imageUrl": lt.image_url or "",
                "sourceDoc": lt.source_document_type,
                "grnNo": lt.source_document_number or "-",
                "sourceDocumentId": str(lt.source_document_id) if lt.source_document_id else None,
                "stockMovements": mov_list,
                "createdBy": (lt.created_by.name or lt.created_by.email) if lt.created_by else None,
                "createdById": str(lt.created_by_id) if lt.created_by_id else None,
                "updatedBy": (lt.updated_by.name or lt.updated_by.email) if lt.updated_by else None,
                "updatedById": str(lt.updated_by_id) if lt.updated_by_id else None,
                "createdAt": lt.created_at.isoformat() if lt.created_at else None,
                "updatedAt": lt.updated_at.isoformat() if lt.updated_at else None,
            })

        # Summary Cards -- quantities are only summed in one unit.
        lot_units = sorted({(lt.uom or (lt.item.uom if lt.item_id else "")) for lt in all_lots} - {""})
        summary_uom = lot_units[0] if len(lot_units) == 1 else ("Mixed" if lot_units else None)
        avg_rate = float(tot_value / tot_received) if tot_received > 0 else 0.0
        summary_cards = {
            "totalLots": {
                "count": len(all_lots),
                "active": active_count,
                "closed": closed_count,
            },
            "totalQuantity": {
                "qty": float(tot_received),
                "inStock": float(tot_available),
                "uom": summary_uom,
            },
            "totalValue": {
                "val": float(tot_value),
                "avgRate": avg_rate,
            },
            "pendingQC": {
                "qty": float(pending_qc_qty),
                "lots": len(pending_qc_lots),
                "uom": summary_uom,
            },
            "onHold": {
                "qty": float(on_hold_qty),
                "lots": len(on_hold_lots),
                "uom": summary_uom,
            },
        }

        # Filter option lists
        supplier_names = list(Party.objects.filter(client_id=client_id, type__in=["Vendor", "Both"]).values_list("name", flat=True).order_by("name"))
        quality_names = list(FabricQuality.objects.filter(client_id=client_id, deleted_at__isnull=True).values_list("name", flat=True).order_by("name"))
        design_names = list(Design.objects.filter(client_id=client_id, deleted_at__isnull=True).values_list("name", flat=True).order_by("name"))
        if not design_names:
            design_names = list(Item.objects.filter(client_id=client_id, fabric_design__isnull=False).values_list("fabric_design", flat=True).distinct().order_by("fabric_design"))
        shade_names = list(Shade.objects.filter(client_id=client_id, deleted_at__isnull=True).values_list("name", flat=True).order_by("name"))
        if not shade_names:
            shade_names = list(Item.objects.filter(client_id=client_id, fabric_color__isnull=False).values_list("fabric_color", flat=True).distinct().order_by("fabric_color"))
        location_names = list(Location.objects.filter(client_id=client_id).values_list("name", flat=True).order_by("name"))

        # Optional Item Preview Data for "+ Add Lot / Taka" Modal
        preview_data = None
        if item_id_preview:
            prev_item = Item.objects.filter(client_id=client_id, id=item_id_preview).first()
            if prev_item:
                curr_stock = float(StockBalance.objects.filter(client_id=client_id, item=prev_item).aggregate(tot=Sum("on_hand"))["tot"] or 0)
                recent_lots = []
                for rlt in LotTaka.objects.filter(client_id=client_id, item=prev_item, deleted_at__isnull=True).order_by("-created_at")[:5]:
                    recent_lots.append({
                        "lotNo": rlt.lot_number,
                        "takaRoll": rlt.taka_number,
                        "qty": float(rlt.received_quantity),
                        "shade": prev_item.fabric_color or None,
                        "status": rlt.status,
                    })
                preview_data = {
                    "itemCode": prev_item.sku,
                    "itemName": prev_item.name,
                    "quality": prev_item.fabric_quality or prev_item.name,
                    "design": prev_item.fabric_design or None,
                    "shade": prev_item.fabric_color or None,
                    "uom": prev_item.uom or None,
                    "currentStock": curr_stock,
                    "recentLots": recent_lots,
                }

        return Response({
            "summaryCards": summary_cards,
            "filters": {
                "suppliers": supplier_names,
                "qualities": quality_names,
                "designs": design_names,
                "shades": shade_names,
                "locations": location_names,
                "statuses": ["All", "Active", "Closed"],
                "qcStatuses": ["All", "Passed", "Pending", "On Hold", "Reprocess", "Rejected"],
            },
            "records": records,
            "itemPreview": preview_data,
        })

    @transaction.atomic
    def post(self, request):
        """Manual lot / taka entry (opening stock or a receipt with no bill).

        The taka starts empty and the PURCHASE movement fills it, so
        ``available_quantity`` comes from the ledger. ``qcStatus`` must be
        given: ``Passed`` puts the stock in the chosen godown, ``Pending``
        puts it at QC Hold until QC releases it.
        """
        client_id = request.client_id
        data = request.data
        field_errors = {}

        item = Item.objects.filter(client_id=client_id, id=data.get("itemId"), deleted_at__isnull=True).first() if data.get("itemId") else None

        takas_list = data.get("takas")
        if takas_list and isinstance(takas_list, list) and len(takas_list) > 0:
            if item is None:
                field_errors["itemId"] = ["Choose the fabric item."]
            qc_status = data.get("qcStatus") or "Pending"
            if qc_status not in ("Passed", "Pending"):
                qc_status = "Pending"
            try:
                rec_date = date.fromisoformat(str(data.get("receivedDate") or ""))
            except ValueError:
                rec_date = date.today()

            grn_id = data.get("grnId") or data.get("sourceDocumentId")
            grn = None
            if grn_id:
                from apps.purchase.models import GoodsReceipt
                grn = GoodsReceipt.objects.select_for_update().filter(client_id=client_id, id=grn_id, deleted_at__isnull=True).first()
            if not grn and data.get("grnNo"):
                from apps.purchase.models import GoodsReceipt
                grn = GoodsReceipt.objects.select_for_update().filter(client_id=client_id, grn_number__iexact=data.get("grnNo"), deleted_at__isnull=True).first()

            submitted_rolls = set()
            total_taka_meters = ZERO
            for idx, t in enumerate(takas_list):
                r_num = str(t.get("takaRoll") or t.get("takaNumber") or (idx + 1)).strip()
                if r_num in submitted_rolls:
                    field_errors[f"takas_{idx}"] = [f"Duplicate roll number '{r_num}' in submission."]
                submitted_rolls.add(r_num)
                t_code = t.get("takaId") or t.get("barcode")
                if t_code and LotTaka.objects.filter(Q(taka_id=t_code) | Q(barcode=t_code), client_id=client_id, deleted_at__isnull=True).exists():
                    field_errors[f"takas_{idx}"] = [f"Taka identifier or barcode '{t_code}' already exists."]
                t_qty = round4(D(str(t.get("receivedQty") or t.get("qty") or 0)))
                if t_qty <= ZERO:
                    field_errors[f"takas_{idx}"] = [f"Taka #{t.get('takaRoll') or idx+1} quantity must be greater than 0."]
                total_taka_meters += t_qty

            if grn:
                grn_total_qty = sum(D(str(l.received_qty or 0)) for l in grn.lines.all())
                existing_grn_takas = list(LotTaka.objects.filter(client_id=client_id, source_document_id=grn.id, deleted_at__isnull=True))
                is_single_placeholder = False
                if len(existing_grn_takas) == 1:
                    ph = existing_grn_takas[0]
                    if abs(D(str(ph.received_quantity)) - grn_total_qty) < Decimal("0.001"):
                        is_single_placeholder = True

                already_allocated = ZERO if is_single_placeholder else sum(D(str(t.received_quantity or 0)) for t in existing_grn_takas)
                available_for_split = max(ZERO, grn_total_qty - already_allocated)

                if total_taka_meters > available_for_split:
                    field_errors["takas"] = [
                        f"Total taka quantity ({total_taka_meters} m) cannot exceed the received quantity available from GRN ({available_for_split} m)."
                    ]

            if field_errors:
                raise ValidationFailed("Validation failed for lot/taka creation.", field_errors=field_errors)

            client = request.user.client
            lot_no = (data.get("lotNo") or "").strip() or allocate_number(client, "LOT", rec_date)

            existing_taka_rolls = set(
                LotTaka.objects.filter(client_id=client_id, lot_number=lot_no, deleted_at__isnull=True).values_list("taka_number", flat=True)
            )

            if grn:
                placeholder_takas = LotTaka.objects.filter(client_id=client_id, source_document_id=grn.id, deleted_at__isnull=True)
                if placeholder_takas.count() == 1:
                    ph = placeholder_takas.first()
                    if abs(D(str(ph.received_quantity)) - grn_total_qty) < Decimal("0.001"):
                        if ph.stock_movement:
                            bal = StockBalance.objects.filter(
                                client_id=client_id,
                                item_id=ph.stock_movement.item_id,
                                location_id=ph.stock_movement.location_id,
                            ).first()
                            if bal:
                                bal.on_hand = max(ZERO, (bal.on_hand or ZERO) - ph.stock_movement.effective_quantity)
                                bal.save(update_fields=["on_hand", "updated_at"])
                            ph.stock_movement.delete()
                        ph.delete()
                        existing_taka_rolls.discard(ph.taka_number)

            supplier = None
            if data.get("supplierId"):
                supplier = Party.objects.filter(client_id=client_id, id=data.get("supplierId")).first()
            elif data.get("supplier"):
                supplier = Party.objects.filter(client_id=client_id, name=data.get("supplier")).first()
            elif grn and grn.party:
                supplier = grn.party

            def dec_or_none_val(value):
                return D(str(value)) if value not in (None, "") else None

            rate = dec_or_none_val(data.get("rate")) or (grn.lines.first().unit_cost if grn and grn.lines.exists() else ZERO)
            target_location = stock.system_location(client_id, "QC_HOLD") if qc_status == "Pending" else (Location.objects.filter(client_id=client_id, id=data.get("locationId"), deleted_at__isnull=True).first() or Location.objects.filter(client_id=client_id, is_active=True).first())

            created_takas = []
            for t_idx, t in enumerate(takas_list):
                t_roll = str(t.get("takaRoll") or t.get("takaNumber") or (t_idx + 1)).strip()
                if t_roll in existing_taka_rolls:
                    raise ValidationFailed(
                        f"Taka roll '{t_roll}' already exists for Lot '{lot_no}'. Duplicate roll numbers are not permitted.",
                        field_errors={"takaRoll": [f"Roll #{t_roll} already exists."]}
                    )
                existing_taka_rolls.add(t_roll)

                taka_qty = round4(D(str(t.get("receivedQty") or t.get("qty") or 0)))
                taka_code = allocate_number(client, "TAK")
                roll_weight = dec_or_none_val(t.get("rollWeight") or t.get("weight"))
                t_width = dec_or_none_val(t.get("width") or data.get("width"))
                t_gsm = dec_or_none_val(t.get("gsm") or data.get("gsm"))
                t_remarks = t.get("remarks") or data.get("remarks") or None

                lot_taka = LotTaka.objects.create(
                    client_id=client_id,
                    taka_id=taka_code,
                    lot_number=lot_no,
                    taka_number=t_roll,
                    item=item,
                    supplier=supplier,
                    source_document_type="GRN" if grn else (data.get("sourceDoc") or "Manual"),
                    source_document_number=grn.grn_number if grn else (data.get("grnNo") or None),
                    source_document_id=grn.id if grn else None,
                    source_line_id=grn.lines.first().id if (grn and grn.lines.exists()) else None,
                    location=target_location,
                    received_date=rec_date,
                    received_quantity=taka_qty,
                    available_quantity=ZERO,
                    uom=item.uom,
                    qc_status=qc_status,
                    status="QC Hold" if qc_status == "Pending" else "Active",
                    barcode=taka_code,
                    tag_status="Pending",
                    print_count=0,
                    roll_weight=roll_weight,
                    width=t_width,
                    pieces=1,
                    gsm=t_gsm,
                    rate=rate,
                    remarks=t_remarks,
                    tags=data.get("tags") or [],
                    image_url=data.get("imageUrl") or None,
                    created_by=request.user,
                    updated_by=request.user,
                )

                mov = stock.post_movement(
                    client_id=client_id,
                    item=item,
                    location=target_location,
                    type="PURCHASE",
                    quantity=taka_qty,
                    unit_cost=rate,
                    batch_number=lot_no,
                    lot_taka=lot_taka,
                    reference_type="GoodsReceipt" if grn else "LotRegister",
                    reference_id=grn.id if grn else lot_taka.id,
                    reference_number=grn.grn_number if grn else f"{lot_no}/{t_roll}",
                    source_document_type="GoodsReceipt" if grn else "LotRegister",
                    source_document_id=grn.id if grn else lot_taka.id,
                    movement_date=rec_date,
                    notes=f"GRN intake {lot_no} / {t_roll} ({taka_code})",
                    user=request.user,
                )
                lot_taka.stock_movement = mov
                lot_taka.save(update_fields=["stock_movement", "updated_at"])
                created_takas.append({
                    "id": str(lot_taka.id),
                    "takaId": lot_taka.taka_id,
                    "lotNo": lot_taka.lot_number,
                    "takaRoll": lot_taka.taka_number,
                    "barcode": lot_taka.barcode,
                    "receivedQty": float(taka_qty),
                })

            from apps.core.audit import record_audit
            user_label = getattr(request.user, "name", None) or getattr(request.user, "email", "Authorized Manager")
            record_audit(
                client=client_id, actor=request.user, action="create", entity_type="LotTaka",
                entity_id=created_takas[0]["id"] if created_takas else None,
                entity_label=lot_no,
                description=f"Created {len(created_takas)} takas ({total_taka_meters} m) for Lot {lot_no} by {user_label} from GRN {grn.grn_number if grn else 'manual'}",
            )
            return Response({
                "success": True,
                "message": f"Successfully created {len(created_takas)} Taka rolls for Lot {lot_no} ({total_taka_meters} m) assigned to QC Hold.",
                "lotNo": lot_no,
                "takas": created_takas,
            }, status=status.HTTP_201_CREATED)

        try:
            received_qty = round4(D(data.get("receivedQty")))
        except (InvalidOperation, TypeError, ValueError) as exc:
            logger.exception("Failed parsing receivedQty in LotTakaRegisterView create: %s", exc)
            received_qty = ZERO
        if received_qty <= ZERO:
            field_errors["receivedQty"] = ["Received quantity must be greater than 0."]
        loc = Location.objects.filter(client_id=client_id, id=data.get("locationId"), deleted_at__isnull=True).first() if data.get("locationId") else None
        if loc is None:
            field_errors["locationId"] = ["Choose the storage location."]
        elif loc.code in stock.BLOCKED_LOCATION_CODES:
            field_errors["locationId"] = ["Choose a godown; system locations are filled by QC documents."]
        qc_status = data.get("qcStatus")
        if qc_status not in ("Passed", "Pending"):
            field_errors["qcStatus"] = ["Choose Passed (goes to stock) or Pending (goes to QC Hold)."]
        try:
            rec_date = date.fromisoformat(str(data.get("receivedDate") or ""))
        except ValueError:
            rec_date = None
        if rec_date is None:
            field_errors["receivedDate"] = ["Enter the received date (YYYY-MM-DD)."]
        if field_errors:
            raise ValidationFailed("The lot entry is incomplete.", field_errors=field_errors)

        # Quantities are stored in the item's stock unit.
        from apps.masters import uom as uom_service

        input_uom = (data.get("uom") or item.uom or "").strip()
        item_uom = (item.uom or "").strip()
        if input_uom and item_uom and not uom_service.same_unit_text(input_uom, item_uom):
            book = uom_service.UnitBook(client_id)
            u_in, u_item = book.resolve(input_uom), book.resolve(item_uom)
            if not (u_in and u_item and uom_service.compatible(u_in, u_item)):
                raise ValidationFailed(
                    f"UOM '{input_uom}' is incompatible with the item's stock unit '{item_uom}'.",
                    field_errors={"uom": ["Incompatible unit."]},
                )
            received_qty = round4(uom_service.convert(received_qty, u_in, u_item))

        client = request.user.client
        lot_no = (data.get("lotNo") or "").strip() or allocate_number(client, "LOT", rec_date)
        taka_roll = str(data.get("takaRoll") or "").strip() or str(
            LotTaka.objects.filter(client_id=client_id, lot_number=lot_no, deleted_at__isnull=True).count() + 1
        )
        if LotTaka.objects.filter(client_id=client_id, lot_number=lot_no, taka_number=taka_roll, deleted_at__isnull=True).exists():
            raise ValidationFailed(
                f"Lot '{lot_no}' with Taka / Roll '{taka_roll}' already exists in the register.",
                field_errors={"takaRoll": ["Already exists for this lot."]},
            )

        supplier = None
        if data.get("supplierId"):
            supplier = Party.objects.filter(client_id=client_id, id=data.get("supplierId")).first()
        elif data.get("supplier"):
            supplier = Party.objects.filter(client_id=client_id, name=data.get("supplier")).first()

        def dec_or_none(key):
            value = data.get(key)
            return D(str(value)) if value not in (None, "") else None

        rate = dec_or_none("rate") or ZERO
        taka_code = allocate_number(client, "TAK")
        target_location = loc if qc_status == "Passed" else stock.system_location(client_id, "QC_HOLD")
        source_doc = data.get("sourceDoc") or "Manual"
        reference_number = data.get("grnNo") or f"{lot_no}/{taka_roll}"

        lot_taka = LotTaka.objects.create(
            client_id=client_id,
            taka_id=taka_code,
            lot_number=lot_no,
            taka_number=taka_roll,
            item=item,
            supplier=supplier,
            source_document_type=source_doc,
            source_document_number=data.get("grnNo") or None,
            location=target_location,
            received_date=rec_date,
            received_quantity=received_qty,
            available_quantity=ZERO,
            uom=item.uom,
            qc_status=qc_status,
            status="Active",
            barcode=data.get("barcode") or taka_code,
            tag_status="Pending",
            print_count=0,
            # kg is recorded only as the roll weight -- never as the
            # movement's weighed quantity for a meter item.
            roll_weight=dec_or_none("rollWeight"),
            width=dec_or_none("width"),
            pieces=int(data.get("pieces") or 1),
            gsm=dec_or_none("gsm"),
            rate=rate,
            remarks=data.get("remarks") or None,
            tags=data.get("tags") or [],
            image_url=data.get("imageUrl") or None,
            created_by=request.user,
        )
        mov = stock.post_movement(
            client_id=client_id,
            item=item,
            location=target_location,
            type="PURCHASE",
            quantity=received_qty,
            unit_cost=rate,
            batch_number=lot_no,
            lot_taka=lot_taka,
            reference_type="LotRegister",
            reference_id=lot_taka.id,
            reference_number=reference_number,
            movement_date=rec_date,
            notes=f"Manual lot entry {lot_no} / {taka_roll} ({taka_code})",
            user=request.user,
        )
        lot_taka.stock_movement = mov
        lot_taka.save(update_fields=["stock_movement", "updated_at"])

        from apps.core.audit import record_audit

        record_audit(
            client=client_id, actor=request.user, action="create", entity_type="LotTaka",
            entity_id=lot_taka.id, entity_label=taka_code,
            description=f"Manual lot {lot_no} / {taka_roll}: {received_qty} {item.uom} at {target_location.name}",
        )
        return Response({
            "success": True,
            "message": f"Lot {lot_no} / Taka {taka_roll} ({taka_code}) created at {target_location.name}.",
            "id": str(lot_taka.id),
            "takaId": lot_taka.taka_id,
            "lotNo": lot_taka.lot_number,
            "takaRoll": lot_taka.taka_number,
            "barcode": lot_taka.barcode,
        }, status=status.HTTP_201_CREATED)

    @transaction.atomic
    def patch(self, request, pk=None):
        """Descriptive fields only. Location, rate, status and QC status change
        only through documents (transfer, adjustment, QC disposition)."""
        client_id = request.client_id
        lot_id = pk or request.data.get("id")
        lot_taka = LotTaka.objects.filter(client_id=client_id, id=lot_id, deleted_at__isnull=True).first()
        if not lot_taka:
            raise ValidationFailed("Lot record not found.", field_errors={"id": ["Not found."]})

        data = request.data
        # Quantity is never edited in place, not even by a manager: rewriting
        # the movement would leave StockBalance stale (PDF §17). A quantity
        # correction is a stock adjustment document.
        disallowed = [
            f for f in (
                "locationId", "location", "rate", "status", "qcStatus", "qc_status",
                "availableQty", "receivedQty", "qty",
            )
            if f in data
        ]
        if disallowed:
            raise ValidationFailed(
                f"Direct edits to {', '.join(disallowed)} are not allowed. "
                "Stock, location and status changes come from documents "
                "(GRN, stock transfer, stock adjustment or QC disposition).",
                field_errors={f: ["Not editable."] for f in disallowed},
            )

        before = {"remarks": lot_taka.remarks, "rollWeight": str(lot_taka.roll_weight),
                  "width": str(lot_taka.width), "gsm": str(lot_taka.gsm)}
        if "remarks" in data:
            lot_taka.remarks = data["remarks"]
        if "tags" in data:
            lot_taka.tags = data["tags"]
        if "rollWeight" in data:
            lot_taka.roll_weight = D(str(data["rollWeight"])) if data["rollWeight"] not in (None, "") else None
        if "width" in data:
            lot_taka.width = D(str(data["width"])) if data["width"] not in (None, "") else None
        if "gsm" in data:
            lot_taka.gsm = D(str(data["gsm"])) if data["gsm"] not in (None, "") else None
        if "imageUrl" in data:
            lot_taka.image_url = data["imageUrl"]
        lot_taka.updated_by = request.user
        lot_taka.save()

        from apps.core.audit import record_audit

        record_audit(
            client=client_id, actor=request.user, action="update", entity_type="LotTaka",
            entity_id=lot_taka.id, entity_label=lot_taka.taka_id, before=before,
            after={"remarks": lot_taka.remarks, "rollWeight": str(lot_taka.roll_weight),
                   "width": str(lot_taka.width), "gsm": str(lot_taka.gsm)},
        )
        return Response({
            "success": True,
            "message": f"Lot {lot_taka.lot_number} / Taka {lot_taka.taka_number} updated successfully.",
        })


class TakaTagPrintView(APIView):
    """Endpoints for Taka Tag Print register, summary metrics, filters, and print event recording."""
    permission_classes = [HasModulePermission]
    module_name = "inventory"
    required_permissions = ["view_inventory"]
    permission_map = {"read": ["view_inventory"], "write": ["view_inventory"]}

    def get(self, request):
        client_id = request.client_id

        # Query filters
        search_query = (request.query_params.get("search") or "").strip()
        from_date_str = request.query_params.get("from_date")
        to_date_str = request.query_params.get("to_date")
        supplier_filter = request.query_params.get("supplier")
        item_filter = request.query_params.get("item")
        quality_filter = request.query_params.get("quality")
        design_filter = request.query_params.get("design")
        shade_filter = request.query_params.get("shade")
        qc_status_filter = request.query_params.get("qc_status")
        tag_status_filter = request.query_params.get("tag_status")

        # Base queryset
        base_qs = LotTaka.objects.filter(client_id=client_id, deleted_at__isnull=True).select_related(
            "item", "supplier", "location", "last_printed_by"
        )

        # Summary cards from real records
        total_takas = base_qs.count()
        tags_printed = base_qs.filter(tag_status__in=["Printed", "Reprinted"]).count()
        pending_print = base_qs.filter(tag_status="Pending").count()
        reprints_count = TakaTagPrintLog.objects.filter(client_id=client_id, action_type="REPRINT").count()
        if reprints_count == 0:
            reprints_count = base_qs.filter(print_count__gt=1).count()

        summary_cards = {
            "totalTakas": total_takas,
            "tagsPrinted": tags_printed,
            "pendingPrint": pending_print,
            "reprints": reprints_count,
        }

        # Filter option lists
        supplier_names = list(Party.objects.filter(client_id=client_id, type__in=["Vendor", "Both"]).values_list("name", flat=True).order_by("name"))
        quality_names = list(FabricQuality.objects.filter(client_id=client_id, deleted_at__isnull=True).values_list("name", flat=True).order_by("name"))
        design_names = list(Design.objects.filter(client_id=client_id, deleted_at__isnull=True).values_list("name", flat=True).order_by("name"))
        shade_names = list(Shade.objects.filter(client_id=client_id, deleted_at__isnull=True).values_list("name", flat=True).order_by("name"))
        fabric_items = list(Item.objects.filter(client_id=client_id, fabric_quality__isnull=False).values("id", "sku", "name")[:50])
        if not fabric_items:
            fabric_items = list(Item.objects.filter(client_id=client_id).values("id", "sku", "name")[:50])

        qs = base_qs
        if from_date_str:
            try:
                from_d = date.fromisoformat(from_date_str)
                qs = qs.filter(received_date__gte=from_d)
            except (ValueError, TypeError) as exc:
                logger.exception("Invalid from_date parameter in TakaTagPrintView: %s", exc)

        if to_date_str:
            try:
                to_d = date.fromisoformat(to_date_str)
                qs = qs.filter(received_date__lte=to_d)
            except (ValueError, TypeError) as exc:
                logger.exception("Invalid to_date parameter in TakaTagPrintView: %s", exc)

        if supplier_filter and supplier_filter != "All":
            qs = qs.filter(supplier__name=supplier_filter)

        if item_filter and item_filter != "All":
            qs = qs.filter(Q(item__sku__icontains=item_filter) | Q(item__name__icontains=item_filter))

        if quality_filter and quality_filter != "All":
            qs = qs.filter(item__fabric_quality=quality_filter)

        if design_filter and design_filter != "All":
            qs = qs.filter(item__fabric_design=design_filter)

        if shade_filter and shade_filter != "All":
            qs = qs.filter(item__fabric_color=shade_filter)

        if qc_status_filter and qc_status_filter != "All":
            qs = qs.filter(qc_status=qc_status_filter)

        if tag_status_filter and tag_status_filter != "All":
            qs = qs.filter(tag_status=tag_status_filter)

        if search_query:
            qs = qs.filter(
                Q(taka_id__icontains=search_query)
                | Q(barcode__icontains=search_query)
                | Q(lot_number__icontains=search_query)
                | Q(taka_number__icontains=search_query)
                | Q(source_document_number__icontains=search_query)
                | Q(item__sku__icontains=search_query)
                | Q(item__name__icontains=search_query)
                | Q(supplier__name__icontains=search_query)
            )

        records = []
        for lt in qs:
            it = lt.item
            taka_id = lt.taka_id or lt.barcode
            records.append({
                "id": str(lt.id),
                "takaId": taka_id,
                "lotNo": lt.lot_number,
                "takaRoll": lt.taka_number,
                "itemCode": it.sku if it else "-",
                "itemName": it.name if it else None,
                "quality": (it.fabric_quality or it.name) if it else None,
                "design": (it.fabric_design or None) if it else None,
                "shade": (it.fabric_color or None) if it else None,
                "supplier": lt.supplier.name if lt.supplier else (lt.source_document_number or "-"),
                "supplierId": str(lt.supplier.id) if lt.supplier else "",
                "receivedQty": float(lt.received_quantity),
                "availableQty": float(lt.available_quantity),
                "uom": lt.uom or (it.uom if it else None),
                "qcStatus": lt.qc_status,
                "tagStatus": lt.tag_status or "Pending",
                "barcode": lt.barcode or taka_id,
                "printCount": lt.print_count or 0,
                "lastPrintedAt": lt.last_printed_at.strftime("%d-%m-%Y %H:%M") if lt.last_printed_at else None,
                "lastPrintedBy": getattr(lt.last_printed_by, "name", None) or getattr(lt.last_printed_by, "email", None) or None,
                "location": lt.location.name if lt.location else None,
                "locationId": str(lt.location.id) if lt.location else "",
                "receivedDate": lt.received_date.strftime("%d-%m-%Y"),
                "grnNo": lt.source_document_number or "-",
                "rollWeight": float(lt.roll_weight) if lt.roll_weight else None,
                "width": float(lt.width) if lt.width else None,
                "gsm": float(lt.gsm) if lt.gsm else None,
                "rate": float(lt.rate),
            })

        return Response({
            "summaryCards": summary_cards,
            "filters": {
                "suppliers": supplier_names,
                "items": [f"{it['sku']} ({it['name']})" for it in fabric_items],
                "qualities": quality_names,
                "designs": design_names,
                "shades": shade_names,
                "qcStatuses": ["All", "Passed", "Pending", "On Hold", "Reprocess", "Rejected"],
                "tagStatuses": ["All", "Pending", "Printed", "Reprinted"],
            },
            "records": records,
        })

    @transaction.atomic
    def post(self, request):
        """Record print or reprint event for selected takas (never alters stock)."""
        client_id = request.client_id
        data = request.data
        ids = data.get("ids") or data.get("takaIds") or []
        if isinstance(ids, str):
            ids = [ids]
        action_type = data.get("actionType") or ("REPRINT" if data.get("isReprint") else "PRINT")
        notes = data.get("notes") or ""

        if not ids:
            return Response({"detail": "No takas selected for printing."}, status=status.HTTP_400_BAD_REQUEST)

        import uuid
        uuid_ids = []
        code_ids = []
        for val in ids:
            v_str = str(val).strip()
            try:
                uuid.UUID(v_str)
                uuid_ids.append(v_str)
            except (ValueError, AttributeError):
                code_ids.append(v_str)

        q_cond = Q()
        if uuid_ids:
            q_cond |= Q(id__in=uuid_ids)
        if code_ids:
            q_cond |= Q(taka_id__in=code_ids) | Q(barcode__in=code_ids)

        takas = list(LotTaka.objects.filter(client_id=client_id, deleted_at__isnull=True).filter(q_cond))
        if not takas:
            return Response({"detail": "Selected taka records not found."}, status=status.HTTP_404_NOT_FOUND)

        now = timezone.now()
        user = request.user if request.user.is_authenticated else None
        user_name = getattr(user, "name", None) or getattr(user, "email", None)

        for lt in takas:
            old_count = lt.print_count
            lt.print_count += 1
            if action_type == "REPRINT" or old_count >= 1:
                lt.tag_status = "Reprinted"
                record_action = "REPRINT"
            else:
                lt.tag_status = "Printed"
                record_action = "PRINT" if len(takas) == 1 else "BULK_PRINT"

            lt.last_printed_at = now
            lt.last_printed_by = user
            lt.save(update_fields=["print_count", "tag_status", "last_printed_at", "last_printed_by", "updated_at"])

            TakaTagPrintLog.objects.create(
                client_id=client_id,
                lot_taka=lt,
                action_type=record_action,
                printed_at=now,
                printed_by=user,
                printed_by_name=user_name,
                taka_id=lt.taka_id or lt.barcode,
                lot_number=lt.lot_number,
                taka_number=lt.taka_number,
                notes=notes,
            )

        return Response({
            "success": True,
            "message": f"Recorded print event for {len(takas)} taka(s).",
            "count": len(takas),
        })


class TakaTagScanView(APIView):
    """Fast barcode scanner lookup endpoint for takas (Code 128 / Taka ID)."""
    permission_classes = [HasModulePermission]
    module_name = "inventory"
    required_permissions = ["view_inventory"]
    permission_map = {"read": ["view_inventory"], "write": ["view_inventory"]}

    def get(self, request):
        client_id = request.client_id
        code = (request.query_params.get("code") or request.query_params.get("search") or "").strip()
        if not code:
            return Response({"detail": "Please provide a barcode or Taka ID to scan."}, status=status.HTTP_400_BAD_REQUEST)

        lt = LotTaka.objects.filter(
            client_id=client_id, deleted_at__isnull=True
        ).filter(
            Q(taka_id__iexact=code)
            | Q(barcode__iexact=code)
            | Q(lot_number__iexact=code)
        ).select_related("item", "supplier", "location", "last_printed_by").first()

        if not lt:
            return Response({
                "found": False,
                "detail": f"No active Taka record found matching barcode/ID '{code}'.",
            }, status=status.HTTP_404_NOT_FOUND)

        it = lt.item
        taka_id = lt.taka_id or lt.barcode

        taka_dict = {
            "found": True,
            "id": str(lt.id),
            "takaId": taka_id,
            "lotNo": lt.lot_number,
            "takaRoll": lt.taka_number,
            "itemCode": it.sku if it else "-",
            "itemName": it.name if it else None,
            "quality": (it.fabric_quality or it.name) if it else None,
            "design": (it.fabric_design or None) if it else None,
            "shade": (it.fabric_color or None) if it else None,
            "supplier": lt.supplier.name if lt.supplier else (lt.source_document_number or "-"),
            "supplierId": str(lt.supplier.id) if lt.supplier else "",
            "receivedQty": float(lt.received_quantity),
            "availableQty": float(lt.available_quantity),
            "uom": lt.uom or (it.uom if it else None),
            "qcStatus": lt.qc_status,
            "tagStatus": lt.tag_status or "Pending",
            "barcode": lt.barcode or taka_id,
            "printCount": lt.print_count or 0,
            "lastPrintedAt": lt.last_printed_at.strftime("%d-%m-%Y %H:%M") if lt.last_printed_at else None,
            "location": lt.location.name if lt.location else None,
            "locationId": str(lt.location.id) if lt.location else "",
            "receivedDate": lt.received_date.strftime("%d-%m-%Y"),
            "grnNo": lt.source_document_number or "-",
            "rollWeight": float(lt.roll_weight) if lt.roll_weight else None,
            "width": float(lt.width) if lt.width else None,
            "gsm": float(lt.gsm) if lt.gsm else None,
            "rate": float(lt.rate),
        }
        res_payload = dict(taka_dict)
        res_payload["taka"] = taka_dict
        return Response(res_payload)


class TakaTagHistoryView(APIView):
    """View print/reprint history audit logs for a specific taka."""
    permission_classes = [HasModulePermission]
    module_name = "inventory"
    required_permissions = ["view_inventory"]
    permission_map = {"read": ["view_inventory"], "write": ["view_inventory"]}

    def get(self, request, pk=None):
        client_id = request.client_id
        target = pk or request.query_params.get("takaId") or request.query_params.get("taka_id") or request.query_params.get("id")
        if not target:
            return Response({"detail": "Taka ID or UUID required."}, status=status.HTTP_400_BAD_REQUEST)

        import uuid
        target_str = str(target).strip()
        is_uuid = False
        try:
            uuid.UUID(target_str)
            is_uuid = True
        except (ValueError, AttributeError):
            is_uuid = False

        if is_uuid:
            lt = LotTaka.objects.filter(client_id=client_id, deleted_at__isnull=True, id=target_str).first()
        else:
            lt = LotTaka.objects.filter(client_id=client_id, deleted_at__isnull=True).filter(
                Q(taka_id__iexact=target_str) | Q(barcode__iexact=target_str) | Q(lot_number__iexact=target_str)
            ).first()

        if not lt:
            return Response({"detail": "Taka record not found."}, status=status.HTTP_404_NOT_FOUND)

        logs = TakaTagPrintLog.objects.filter(client_id=client_id, lot_taka=lt).order_by("-printed_at")
        history = [
            {
                "id": str(log.id),
                "actionType": log.action_type,
                "printedAt": log.printed_at.strftime("%d-%m-%Y %H:%M:%S"),
                "printedBy": log.printed_by_name or (log.printed_by.name if log.printed_by else None),
                "takaId": log.taka_id,
                "lotNo": log.lot_number,
                "takaRoll": log.taka_number,
                "notes": log.notes or "-",
            }
            for log in logs
        ]
        return Response({
            "takaId": lt.taka_id or lt.barcode,
            "lotNo": lt.lot_number,
            "takaRoll": lt.taka_number,
            "printCount": lt.print_count,
            "history": history,
            "logs": history,
        })


class TakaTagPdfView(APIView):
    """Generate printable PDF labels (60mm x 40mm) with Code 128 barcode using ReportLab."""
    permission_classes = [HasModulePermission]
    module_name = "inventory"
    required_permissions = ["view_inventory"]
    permission_map = {"read": ["view_inventory"], "write": ["view_inventory"]}

    def get(self, request):
        import io
        import uuid
        from reportlab.lib.units import mm
        from reportlab.pdfgen import canvas
        from reportlab.graphics.barcode import code128

        client_id = request.client_id
        ids_str = request.query_params.get("ids") or request.query_params.get("takaIds") or ""
        ids_str = ids_str.strip()
        if not ids_str:
            return Response({"detail": "No taka IDs specified."}, status=status.HTTP_400_BAD_REQUEST)

        ids = [i.strip() for i in ids_str.split(",") if i.strip()]
        uuid_ids = []
        code_ids = []
        for val in ids:
            v_str = str(val).strip()
            try:
                uuid.UUID(v_str)
                uuid_ids.append(v_str)
            except (ValueError, AttributeError):
                code_ids.append(v_str)

        q_cond = Q()
        if uuid_ids:
            q_cond |= Q(id__in=uuid_ids)
        if code_ids:
            q_cond |= Q(taka_id__in=code_ids) | Q(barcode__in=code_ids)

        takas = list(
            LotTaka.objects.filter(client_id=client_id, deleted_at__isnull=True).filter(
                q_cond
            ).select_related("item", "supplier", "location")
        )
        if not takas:
            return Response({"detail": "No matching takas found."}, status=status.HTTP_404_NOT_FOUND)

        buffer = io.BytesIO()
        label_size = (60 * mm, 40 * mm)
        pdf = canvas.Canvas(buffer, pagesize=label_size)

        for lt in takas:
            it = lt.item
            taka_id = lt.taka_id or lt.barcode
            item_code = it.sku if it else "-"
            item_name = it.name if it else "-"
            quality = (it.fabric_quality or it.name) if it else "-"
            design = (it.fabric_design or "-") if it else "-"
            shade = (it.fabric_color or "-") if it else "-"
            supplier = lt.supplier.name if lt.supplier else (lt.source_document_number or "-")
            grn_no = lt.source_document_number or "-"
            qty_str = f"{float(lt.received_quantity):.2f} {lt.uom or (it.uom if it else '')}"
            qc_st = lt.qc_status or "-"

            # Top Header: OSCAR TEXTILE ERP | QC Status
            pdf.setFont("Helvetica-Bold", 7.5)
            pdf.drawString(2.5 * mm, 36.5 * mm, "OSCAR TEXTILE ERP")
            pdf.setFont("Helvetica-Bold", 6.5)
            pdf.drawRightString(57.5 * mm, 36.5 * mm, f"QC: {qc_st.upper()}")

            pdf.setStrokeColorRGB(0.7, 0.7, 0.7)
            pdf.setLineWidth(0.5)
            pdf.line(2.5 * mm, 35 * mm, 57.5 * mm, 35 * mm)

            # Code 128 Barcode
            try:
                bc = code128.Code128(taka_id or f"{lt.lot_number}-{lt.taka_number}", barHeight=7.5 * mm, barWidth=0.25 * mm, humanReadable=False)
                bc.drawOn(pdf, 3.5 * mm, 26 * mm)
            except Exception as exc:
                # Non-fatal: if barcode rendering fails, PDF is still generated with printed text ID
                logger.exception("Failed generating barcode on taka tag PDF for taka %s: %s", taka_id, exc)

            # Human readable Taka ID
            pdf.setFont("Helvetica-Bold", 7.5)
            pdf.drawCentredString(30 * mm, 22.5 * mm, taka_id or '-')

            # Details
            pdf.setFont("Helvetica-Bold", 5.5)
            pdf.drawString(2.5 * mm, 18.5 * mm, f"Lot: {lt.lot_number} | Roll: #{lt.taka_number}")

            pdf.setFont("Helvetica", 5.5)
            item_desc = f"{item_code} - {item_name}"
            if len(item_desc) > 34:
                item_desc = item_desc[:32] + "..."
            pdf.drawString(2.5 * mm, 15 * mm, f"Item: {item_desc}")

            qds = f"{quality} | {design} | {shade}"
            if len(qds) > 35:
                qds = qds[:33] + "..."
            pdf.drawString(2.5 * mm, 11.5 * mm, f"Q/D/S: {qds}")

            supp_line = f"Supp: {supplier[:18]} | GRN: {grn_no[:10]}"
            pdf.drawString(2.5 * mm, 8 * mm, supp_line)

            # Quantity and UOM
            pdf.setFont("Helvetica-Bold", 6.5)
            pdf.drawString(2.5 * mm, 4 * mm, f"Qty: {qty_str}")
            pdf.setFont("Helvetica", 5)
            pdf.drawRightString(57.5 * mm, 4 * mm, lt.received_date.strftime("%d/%m/%Y"))

            pdf.showPage()

        pdf.save()
        buffer.seek(0)
        response = HttpResponse(buffer.getvalue(), content_type="application/pdf")
        response["Content-Disposition"] = 'inline; filename="taka_tags.pdf"'
        return response


class LotMergeView(APIView):
    """``POST /inventory/lot-register/merge/`` -- combine lots into one batch.

    Body: ``{ lotTakaIds: [...], lotNumber?, remarks? }``. Same item, same
    godown, available stock only. Returns the merged taka.
    """

    permission_classes = [HasModulePermission]
    required_permissions = ["adjust_stock"]

    def post(self, request):
        from .. import lot_merge

        data = request.data
        merged, sources = lot_merge.merge_lots(
            client_id=request.client_id,
            lot_taka_ids=data.get("lotTakaIds") or data.get("lot_taka_ids") or [],
            lot_number=(data.get("lotNumber") or "").strip() or None,
            remarks=data.get("remarks"),
            user=request.user,
        )
        from apps.core.audit import record_audit

        record_audit(
            client=request.client_id, actor=request.user, action="merge", entity_type="LotTaka",
            entity_id=merged.id, entity_label=merged.taka_id,
            description=(
                f"Merged {len(sources)} takas ({', '.join(s.taka_id or s.taka_number for s in sources)}) "
                f"into lot {merged.lot_number}: {merged.received_quantity} {merged.uom or ''}"
            ),
        )
        return Response({
            "id": str(merged.id),
            "takaId": merged.taka_id,
            "lotNo": merged.lot_number,
            "quantity": str(merged.available_quantity),
            "rate": str(merged.rate),
            "parentLots": [
                {"id": str(s.id), "takaId": s.taka_id, "lotNo": s.lot_number} for s in sources
            ],
            "message": f"Merged {len(sources)} takas into lot {merged.lot_number}.",
        }, status=status.HTTP_201_CREATED)


class LotTraceabilityView(APIView):
    """
    GET /inventory/traceability/?target=<lot_or_taka>
    GET /inventory/lots/<lot_id>/traceability/
    Complete traceability tree, movements, job work and QC history (PDF §07, §08, Phase 2.3).
    """
    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]

    def get(self, request, lot_id=None):
        target = lot_id or request.query_params.get("target") or request.query_params.get("lotNo") or request.query_params.get("takaId")
        if not target:
            return Response({"error": "Target lot number or taka ID required."}, status=status.HTTP_400_BAD_REQUEST)

        lineage = stock.trace_lot_lineage(request.client_id, target)
        if lineage is None:
            return Response({"error": f"Lot/Taka '{target}' not found."}, status=status.HTTP_404_NOT_FOUND)

        return Response(lineage)


class LotCostEntryViewSet(TenantModelViewSet):
    queryset = LotCostEntry.objects.select_related("lot_taka")
    serializer_class = LotCostEntrySerializer
    required_permissions = ["view_inventory"]
    audit_entity_type = "LotCostEntry"
    filter_map = {
        "lotNumber": "lot_number",
        "costType": "cost_type",
        "isApproved": "is_approved",
    }

    def perform_create(self, serializer):
        entry = costing_services.post_lot_cost(
            client_id=self.request.client_id,
            lot_number=serializer.validated_data.get("lot_number"),
            cost_type=serializer.validated_data.get("cost_type"),
            amount=serializer.validated_data.get("amount"),
            quantity=serializer.validated_data.get("quantity", 0),
            source_document_type=serializer.validated_data.get("source_document_type", "MANUAL"),
            source_document_number=serializer.validated_data.get("source_document_number"),
            lot_taka=serializer.validated_data.get("lot_taka"),
            user=self.request.user,
            is_approved=serializer.validated_data.get("is_approved", True),
            remarks=serializer.validated_data.get("remarks"),
        )
        serializer.instance = entry

    @action(detail=False, methods=["get"], url_path="landed-cost")
    def landed_cost(self, request):
        lot_number = request.query_params.get("lotNumber") or request.query_params.get("lot_number")
        if not lot_number:
            raise ValidationFailed("lotNumber query parameter is required.")
        return Response(costing_services.get_lot_landed_cost(request.client_id, lot_number))

    @action(detail=False, methods=["get"], url_path="profitability")
    def profitability(self, request):
        lot_number = request.query_params.get("lotNumber") or request.query_params.get("lot_number")
        date_from = request.query_params.get("dateFrom") or request.query_params.get("date_from")
        date_to = request.query_params.get("dateTo") or request.query_params.get("date_to")
        report = costing_services.get_lot_profitability_report(
            request.client_id, lot_number=lot_number, date_from=date_from, date_to=date_to
        )
        return Response(report)

