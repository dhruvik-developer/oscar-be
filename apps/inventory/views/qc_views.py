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
from .lot_views import _is_uuid
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


def _qc_meta(receipt):
    """The QC disposition record stored on ``GoodsReceipt.qc_note`` (JSON)."""
    note = (getattr(receipt, "qc_note", None) or "").strip()
    if note.startswith("{") and note.endswith("}"):
        try:
            return json.loads(note)
        except ValueError:
            return {}
    return {}


def _qc_remarks(receipt):
    """Free-text QC remarks of a receipt (from the JSON record or a plain note)."""
    meta = _qc_meta(receipt)
    if meta:
        return meta.get("remarks") or None
    return (getattr(receipt, "qc_note", None) or None)


def _qc_stage(request):
    """``?stage=grey|dyeing`` (or the older ``source_type`` Purchase / Job Work).

    Grey Fabric QC: purchase / manual GRNs (grey inward). Dyeing QC: job work
    inwards -- fabric back from a dyeing vendor, whoever owns it (OSCAR grey,
    purchased grey or a customer's fabric).
    """
    stage = (request.query_params.get("stage") or "").strip().lower()
    if stage in ("grey", "dyeing"):
        return stage
    legacy = (request.query_params.get("source_type") or "").strip().lower()
    if legacy.startswith("purchase"):
        return "grey"
    if legacy.startswith("job"):
        return "dyeing"
    return None


def _reprocess_stage_q(client_id, stage):
    """A reprocess raised by GRN QC carries the GRN number as ``inward_no``."""
    grn_numbers = GoodsReceipt.objects.filter(client_id=client_id).exclude(
        grn_number__isnull=True).values("grn_number")
    if stage == "grey":
        return Q(inward_no__in=grn_numbers)
    if stage == "dyeing":
        return ~Q(inward_no__in=grn_numbers)
    return Q()


class QCDashboardView(APIView):
    """
    GET /inventory/qc/dashboard/
    Live overview of fabric inspection, quality status, defect tracking and stock impact.
    Dynamically computes all figures from GoodsReceipt, GoodsReceiptLine, PurchaseBill and JWOReprocess.
    Zero mock or dump data.
    """

    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]

    def get(self, request):
        client_id = request.client_id
        today = timezone.localdate()

        # Date parameters
        start_date_str = request.query_params.get("start_date") or request.query_params.get("date_from")
        end_date_str = request.query_params.get("end_date") or request.query_params.get("date_to")

        first_day_this_month = today.replace(day=1)
        try:
            start_date = date.fromisoformat(start_date_str) if start_date_str else first_day_this_month
        except (ValueError, TypeError):
            start_date = first_day_this_month

        try:
            end_date = date.fromisoformat(end_date_str) if end_date_str else today
        except (ValueError, TypeError):
            end_date = today

        # Supplier and Source filters
        supplier_filter = request.query_params.get("supplier")
        source_type_filter = request.query_params.get("source_type")

        # Active suppliers for filter dropdown
        # Active suppliers and processors for filter dropdown
        suppliers_list = list(
            Party.objects.filter(client_id=client_id, type__in=["Vendor", "Both"], deleted_at__isnull=True)
            .values_list("name", flat=True)
            .distinct()
        )
        jwo_vendors_list = list(
            JobWorkOrder.objects.filter(client_id=client_id, vendor_name__isnull=False)
            .values_list("vendor_name", flat=True)
            .distinct()
        )
        active_suppliers = sorted(list(set([s for s in suppliers_list + jwo_vendors_list if s])))

        # Base receipts queryset (Purchase GRN)
        receipts_qs = (
            GoodsReceipt.objects.filter(
                client_id=client_id,
                deleted_at__isnull=True,
                receipt_date__range=[start_date, end_date],
            )
            .select_related("party", "purchase_bill", "qc_by")
            .prefetch_related("lines__item")
        )
        if supplier_filter and supplier_filter != "All":
            receipts_qs = receipts_qs.filter(party__name=supplier_filter)

        # Base Job Work Inward queryset
        inwards_qs = (
            JWOInward.objects.filter(
                client_id=client_id,
                date__range=[start_date, end_date],
            )
            .select_related("job_work_order", "job_work_order__vendor", "job_work_order__process_plan")
        )
        if supplier_filter and supplier_filter != "All":
            inwards_qs = inwards_qs.filter(
                Q(job_work_order__vendor_name=supplier_filter) | Q(job_work_order__vendor__name=supplier_filter)
            )
        stage = _qc_stage(request)
        if stage == "dyeing":
            receipts_qs = receipts_qs.none()
        elif stage == "grey":
            inwards_qs = inwards_qs.none()

        # Lines in date range
        lines_qs = GoodsReceiptLine.objects.filter(
            goods_receipt__in=receipts_qs
        ).select_related("item", "goods_receipt__party")

        # Compute GRN metric aggregates
        total_received_qty = lines_qs.aggregate(
            q=Coalesce(Sum(Coalesce("weighed_qty", "received_qty")), Value(Decimal("0.0000")), output_field=QTY)
        )["q"]

        first_uom = lines_qs.values_list("uom", flat=True).first()
        dominant_uom = first_uom or None

        lots_count = lines_qs.values("batch_number").distinct().count()
        takas_count = lines_qs.count()

        # Inspected GRNs (qc_status != 'Pending')
        inspected_lines = lines_qs.exclude(goods_receipt__qc_status="Pending")
        inspected_qty = inspected_lines.aggregate(
            q=Coalesce(Sum(Coalesce("weighed_qty", "received_qty")), Value(Decimal("0.0000")), output_field=QTY)
        )["q"]
        inspected_lots = inspected_lines.values("batch_number").distinct().count()

        good_qty = lines_qs.filter(goods_receipt__qc_status__in=["Approved", "Partially Approved"]).aggregate(
            q=Coalesce(Sum(Coalesce("weighed_qty", "received_qty") - F("rejected_qty")), Value(Decimal("0.0000")), output_field=QTY)
        )["q"]

        hold_qty = lines_qs.filter(goods_receipt__qc_status="On Hold").aggregate(
            q=Coalesce(Sum(Coalesce("weighed_qty", "received_qty") - F("rejected_qty")), Value(Decimal("0.0000")), output_field=QTY)
        )["q"]

        reprocess_in_range = JWOReprocess.objects.filter(
            client_id=client_id,
            date__range=[start_date, end_date],
        ).filter(_reprocess_stage_q(client_id, stage)).aggregate(
            q=Coalesce(Sum("qty"), Value(Decimal("0.0000")), output_field=QTY)
        )["q"]
        rework_qty = lines_qs.filter(goods_receipt__qc_status="Rework").aggregate(
            q=Coalesce(Sum(Coalesce("weighed_qty", "received_qty")), Value(Decimal("0.0000")), output_field=QTY)
        )["q"]
        total_reprocess = float(reprocess_in_range) + float(rework_qty)

        rejected_qty = lines_qs.aggregate(
            q=Coalesce(Sum("rejected_qty"), Value(Decimal("0.0000")), output_field=QTY)
        )["q"]
        full_rejected_qty = lines_qs.filter(goods_receipt__qc_status="Rejected").aggregate(
            q=Coalesce(Sum(Coalesce("weighed_qty", "received_qty")), Value(Decimal("0.0000")), output_field=QTY)
        )["q"]
        final_rejected = max(float(rejected_qty), float(full_rejected_qty))

        # Job Work Inward Aggregates
        jwo_rec_qty = inwards_qs.aggregate(
            q=Coalesce(Sum("qty"), Value(Decimal("0.0000")), output_field=QTY)
        )["q"]
        jwo_inspected_qty = inwards_qs.exclude(status__in=["Pending", "Pending QC", "Received"]).aggregate(
            q=Coalesce(Sum(F("accepted") + F("rejected")), Value(Decimal("0.0000")), output_field=QTY)
        )["q"]
        jwo_good_qty = inwards_qs.filter(status__in=["Approved", "Completed"]).aggregate(
            q=Coalesce(Sum("accepted"), Value(Decimal("0.0000")), output_field=QTY)
        )["q"]
        jwo_hold_qty = inwards_qs.filter(status="On Hold").aggregate(
            q=Coalesce(Sum("qty"), Value(Decimal("0.0000")), output_field=QTY)
        )["q"]
        jwo_rejected_qty = inwards_qs.aggregate(
            q=Coalesce(Sum("rejected"), Value(Decimal("0.0000")), output_field=QTY)
        )["q"]

        # Combined Totals
        tot_combined_rec = float(total_received_qty) + float(jwo_rec_qty)
        tot_combined_insp = float(inspected_qty) + float(jwo_inspected_qty)
        tot_combined_good = float(good_qty) + float(jwo_good_qty)
        tot_combined_hold = float(hold_qty) + float(jwo_hold_qty)
        tot_combined_rej = final_rejected + float(jwo_rejected_qty)

        tot_rec_f = tot_combined_rec or 1.0
        tot_insp_f = tot_combined_insp or 1.0

        summary_cards = {
            "totalReceived": {
                "qty": tot_combined_rec,
                "lots": lots_count + inwards_qs.count(),
                "takas": takas_count + inwards_qs.count(),
                "uom": dominant_uom,
            },
            "inspected": {
                "qty": tot_combined_insp,
                "lots": inspected_lots + inwards_qs.exclude(status__in=["Pending", "Pending QC", "Received"]).count(),
                "pct": round((tot_combined_insp / tot_rec_f) * 100, 1) if tot_combined_rec > 0 else 0.0,
                "uom": dominant_uom,
            },
            "good": {
                "qty": tot_combined_good,
                "pct": round((tot_combined_good / tot_insp_f) * 100, 1) if tot_combined_insp > 0 else 0.0,
                "uom": dominant_uom,
            },
            "hold": {
                "qty": tot_combined_hold,
                "pct": round((tot_combined_hold / tot_insp_f) * 100, 1) if tot_combined_insp > 0 else 0.0,
                "uom": dominant_uom,
            },
            "reprocess": {
                "qty": total_reprocess,
                "pct": round((total_reprocess / tot_insp_f) * 100, 1) if tot_combined_insp > 0 else 0.0,
                "uom": dominant_uom,
            },
            "rejected": {
                "qty": tot_combined_rej,
                "pct": round((tot_combined_rej / tot_insp_f) * 100, 1) if tot_combined_insp > 0 else 0.0,
                "uom": dominant_uom,
            },
        }

        status_distribution = {
            "inspectedTotal": tot_combined_insp,
            "good": {"qty": tot_combined_good, "pct": summary_cards["good"]["pct"]},
            "hold": {"qty": tot_combined_hold, "pct": summary_cards["hold"]["pct"]},
            "reprocess": {"qty": total_reprocess, "pct": summary_cards["reprocess"]["pct"]},
            "rejected": {"qty": tot_combined_rej, "pct": summary_cards["rejected"]["pct"]},
        }

        # 6-Month Trend
        trend_months = []
        for i in range(5, -1, -1):
            m = today.month - i
            y = today.year
            while m <= 0:
                m += 12
                y -= 1
            m_date = date(y, m, 1)
            next_m = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
            m_label = m_date.strftime("%b")
            month_end = next_m - timedelta(days=1)

            # Grey GoodsReceiptLines
            m_lines = GoodsReceiptLine.objects.filter(
                goods_receipt__client_id=client_id,
                goods_receipt__deleted_at__isnull=True,
                goods_receipt__receipt_date__range=[m_date, month_end],
            )
            if supplier_filter and supplier_filter != "All":
                m_lines = m_lines.filter(goods_receipt__party__name=supplier_filter)
            if stage == "dyeing":
                m_lines = m_lines.none()

            m_good = m_lines.filter(goods_receipt__qc_status__in=["Approved", "Partially Approved"]).aggregate(
                q=Coalesce(Sum(Coalesce("weighed_qty", "received_qty") - F("rejected_qty")), Value(Decimal("0.0000")), output_field=QTY)
            )["q"]
            m_hold = m_lines.filter(goods_receipt__qc_status="On Hold").aggregate(
                q=Coalesce(Sum(Coalesce("weighed_qty", "received_qty") - F("rejected_qty")), Value(Decimal("0.0000")), output_field=QTY)
            )["q"]
            m_rej = m_lines.aggregate(
                q=Coalesce(Sum("rejected_qty"), Value(Decimal("0.0000")), output_field=QTY)
            )["q"]
            m_rep = JWOReprocess.objects.filter(
                client_id=client_id, date__range=[m_date, month_end]
            ).filter(_reprocess_stage_q(client_id, stage)).aggregate(q=Coalesce(Sum("qty"), Value(Decimal("0.0000")), output_field=QTY))["q"]

            # Job Work Inwards (Dyeing / processing)
            jwo_m = JWOInward.objects.filter(
                client_id=client_id,
                date__range=[m_date, month_end],
            )
            if supplier_filter and supplier_filter != "All":
                jwo_m = jwo_m.filter(Q(job_work_order__vendor_name=supplier_filter) | Q(job_work_order__vendor__name=supplier_filter))
            if stage == "grey":
                jwo_m = jwo_m.none()

            jwo_m_good = jwo_m.filter(status__in=["Approved", "Completed"]).aggregate(
                q=Coalesce(Sum("accepted"), Value(Decimal("0.0000")), output_field=QTY)
            )["q"]
            jwo_m_hold = jwo_m.filter(status="On Hold").aggregate(
                q=Coalesce(Sum("qty"), Value(Decimal("0.0000")), output_field=QTY)
            )["q"]
            jwo_m_rej = jwo_m.aggregate(
                q=Coalesce(Sum("rejected"), Value(Decimal("0.0000")), output_field=QTY)
            )["q"]

            trend_months.append({
                "month": m_label,
                "good": float(m_good) + float(jwo_m_good),
                "hold": float(m_hold) + float(jwo_m_hold),
                "reprocess": float(m_rep),
                "rejected": float(m_rej) + float(jwo_m_rej),
            })

        # Supplier Rejection %
        supplier_rejection = []
        supp_groups = (
            lines_qs.values("goods_receipt__party__name")
            .annotate(
                total=Coalesce(Sum(Coalesce("weighed_qty", "received_qty")), Value(Decimal("0.0000")), output_field=QTY),
                rejected=Coalesce(Sum("rejected_qty"), Value(Decimal("0.0000")), output_field=QTY),
            )
            .order_by("-rejected")[:5]
        )
        for s in supp_groups:
            s_name = s["goods_receipt__party__name"]
            s_tot = float(s["total"]) or 1.0
            s_rej = float(s["rejected"])
            supplier_rejection.append({
                "supplier": s_name,
                "rejectedPct": round((s_rej / s_tot) * 100, 1),
            })

        # QC by Fabric Quality
        quality_breakdown = []
        qual_groups = (
            lines_qs.values("item__fabric_quality")
            .annotate(
                inspected=Coalesce(Sum(Coalesce("weighed_qty", "received_qty")), Value(Decimal("0.0000")), output_field=QTY),
                rejected=Coalesce(Sum("rejected_qty"), Value(Decimal("0.0000")), output_field=QTY),
            )
            .order_by("-inspected")[:5]
        )
        for q in qual_groups:
            q_name = q["item__fabric_quality"] or "Other"
            q_insp = float(q["inspected"])
            q_rej = float(q["rejected"])
            quality_breakdown.append({
                "quality": q_name,
                "inspected": q_insp,
                "rejected": q_rej,
                "rejectionPct": round((q_rej / (q_insp or 1.0)) * 100, 1),
            })

        # Pending Inspections Breakdown (Purchase vs Job Work)
        pending_grn_lines = lines_qs.filter(goods_receipt__qc_status="Pending")
        pending_grn_qty = pending_grn_lines.aggregate(
            q=Coalesce(Sum(Coalesce("weighed_qty", "received_qty")), Value(Decimal("0.0000")), output_field=QTY)
        )["q"]
        pending_grn_lots = pending_grn_lines.values("batch_number").distinct().count()

        jwo_pending_qs = inwards_qs.filter(status__in=["Pending", "Pending QC", "Received", "On Hold"])
        jwo_pending_qty = sum(
            float(i.qty - i.accepted - i.rejected) if (i.qty - i.accepted - i.rejected) > 0 else float(i.qty)
            for i in jwo_pending_qs
        )

        pending_inspections = {
            "totalQty": float(pending_grn_qty) + float(jwo_pending_qty),
            "totalLots": pending_grn_lots + jwo_pending_qs.count(),
            "purchase": {
                "qty": float(pending_grn_qty),
                "lots": pending_grn_lots,
            },
            "jobWork": {
                "qty": float(jwo_pending_qty),
                "lots": jwo_pending_qs.count(),
            },
            "internal": {"qty": 0.0, "lots": 0},
        }

        # On Hold Review
        on_hold_lines = lines_qs.filter(goods_receipt__qc_status="On Hold")
        on_hold_qty = on_hold_lines.aggregate(
            q=Coalesce(Sum(Coalesce("weighed_qty", "received_qty")), Value(Decimal("0.0000")), output_field=QTY)
        )["q"]
        on_hold_lots = on_hold_lines.values("goods_receipt_id").distinct().count()

        reason_map = {}
        for l in on_hold_lines.select_related("goods_receipt"):
            reason = _qc_remarks(l.goods_receipt) or "No reason recorded"
            if reason not in reason_map:
                reason_map[reason] = {"qty": Decimal("0"), "lots": set()}
            reason_map[reason]["qty"] += D(l.weighed_qty or l.received_qty or 0)
            reason_map[reason]["lots"].add(l.goods_receipt_id)

        for inw in inwards_qs.filter(status="On Hold"):
            reason = f"Challan {inw.challan_no}" if inw.challan_no else "No reason recorded"
            if reason not in reason_map:
                reason_map[reason] = {"qty": Decimal("0"), "lots": set()}
            reason_map[reason]["qty"] += D(inw.qty or 0)
            reason_map[reason]["lots"].add(inw.id)

        hold_breakdown = [
            {"reason": r, "qty": float(round2(data["qty"])), "lots": len(data["lots"])}
            for r, data in reason_map.items()
        ]

        on_hold_review = {
            "totalQty": float(on_hold_qty) + float(jwo_hold_qty),
            "totalLots": on_hold_lots + inwards_qs.filter(status="On Hold").count(),
            "breakdown": hold_breakdown,
        }

        # Live inspection records (Purchase GRNs + Job Work Inwards)
        from .. import qc as qc_service

        records = []
        for r in receipts_qs.order_by("-receipt_date", "-created_at")[:30]:
            r_lines = list(r.lines.all())
            line0 = r_lines[0] if r_lines else None
            rec_qty = sum(float(l.weighed_qty or l.received_qty or 0) for l in r_lines)
            rej_qty = sum(float(l.rejected_qty or 0) for l in r_lines)
            is_pending = r.qc_status == "Pending"
            is_good = r.qc_status in ["Approved", "Partially Approved"]
            is_hold = r.qc_status == "On Hold"
            is_rep = r.qc_status in ["Rework", "Reprocess"]
            is_rej = r.qc_status == "Rejected" or rej_qty >= rec_qty

            # Quantity still at QC Hold; a verdict can only move what is left.
            awaiting = float(qc_service.pool_quantity(r))
            can_dispose = awaiting > 0 or not qc_service.receipt_has_stock(r)

            # dispose_receipt keeps the real split in qc_note (cumulative per
            # category); use it instead of guessing from the status.
            cumulative = (_qc_meta(r) or {}).get("cumulative")
            if isinstance(cumulative, dict):
                good_q = float(cumulative.get("good") or 0)
                second_q = float(cumulative.get("second") or 0)
                rep_q = float(cumulative.get("reprocess") or 0)
                rej_q = float(cumulative.get("reject") or 0)
                hold_q = awaiting if is_hold else 0.0
                inspected_q = good_q + second_q + rep_q + rej_q + hold_q
            else:
                good_q = (rec_qty - rej_qty) if is_good else 0
                second_q = 0.0
                hold_q = (rec_qty - rej_qty) if is_hold else 0
                rep_q = rec_qty if is_rep else 0
                rej_q = rej_qty if not is_rej else rec_qty
                inspected_q = rec_qty if not is_pending else 0

            records.append({
                "id": r.grn_number,
                "receiptId": str(r.id),
                "date": r.receipt_date.strftime("%d-%m-%Y"),
                "sourceType": "Purchase",
                "refNo": r.purchase_bill.bill_number if r.purchase_bill else r.grn_number,
                "supplier": r.party.name if r.party else None,
                "itemCode": line0.item.sku if line0 and line0.item else "—",
                "itemName": line0.item.name if line0 and line0.item else "—",
                "lotNo": line0.batch_number if line0 and line0.batch_number else None,
                "takaRoll": sum(int(l.roll_count or 0) for l in r_lines) or len(r_lines) or 1,
                "received": rec_qty,
                "inspected": inspected_q,
                "good": good_q,
                "second": second_q,
                "hold": hold_q,
                "reprocess": rep_q,
                "rejected": rej_q,
                "awaiting": awaiting,
                "canDispose": can_dispose,
                "status": "Completed" if r.qc_status == "Approved" else ("On Hold" if is_hold else r.qc_status),
                "defectReason": _qc_remarks(r),
                "inspector": r.qc_by.name if r.qc_by else None,
                "uom": line0.uom if line0 and line0.uom else None,
            })

        for inw in inwards_qs.order_by("-date", "-created_at")[:20]:
            jwo = inw.job_work_order
            plan = jwo.process_plan if jwo else None
            v_name = ((jwo.vendor_name or (jwo.vendor.name if jwo.vendor else None)) if jwo else None)
            it_code = (getattr(plan, "fabric_item", None) or None)
            it_name = ((plan.fabric_item or plan.fabric_quality) or None) if plan else None
            is_pending = inw.status in ["Received", "Pending QC", "Pending"]
            is_good = inw.status in ["Approved", "Completed"]
            is_hold = inw.status == "On Hold"
            is_rej = inw.status == "Rejected"
            is_rep = inw.status in ["Rework", "Reprocess"]

            records.append({
                "id": inw.no,
                "receiptId": str(inw.id),
                "date": inw.date.strftime("%d-%m-%Y") if inw.date else "",
                "sourceType": "Job Work",
                "refNo": inw.no,
                "supplier": v_name,
                "itemCode": it_code,
                "itemName": it_name,
                "lotNo": (plan.grey_lot_no or None) if plan else None,
                "takaRoll": 1,
                "received": float(inw.qty),
                "inspected": float(inw.accepted + inw.rejected) if not is_pending else 0,
                "good": float(inw.accepted) if is_good else 0,
                "hold": float(inw.qty) if is_hold else 0,
                "reprocess": float(inw.qty) if is_rep else 0,
                "rejected": float(inw.rejected),
                "status": "Completed" if is_good else ("On Hold" if is_hold else inw.status),
                "defectReason": f"Challan: {inw.challan_no}" if inw.challan_no else None,
                "inspector": None,
                "uom": None,
            })

        return Response({
            "filters": {
                "suppliers": active_suppliers,
                "dateRange": {
                    "from": start_date.isoformat(),
                    "to": end_date.isoformat(),
                },
            },
            "summaryCards": summary_cards,
            "statusDistribution": status_distribution,
            "trend": trend_months,
            "supplierRejection": supplier_rejection,
            "qualityBreakdown": quality_breakdown,
            "pendingInspections": pending_inspections,
            "onHoldReview": on_hold_review,
            "records": records,
        })


class QCEntryView(APIView):
    """
    GET  /inventory/qc/entry/?stage=grey|dyeing   lots with rolls waiting at QC Hold
    GET  /inventory/qc/entry/?key=<lot key>        that lot's rolls (with the saved draft)
    POST /inventory/qc/entry/  {key, rolls: [{lotTakaId, actualMeter?, weightKg?, width?, gsm?,
                                result, remarks?, defects?: [{defectId, count?}]}],
                                approve?, locationId?, jwoId?, remarks?}
    Roll-by-roll QC (see ``qc_entry``). Approving moves the stock.
    """

    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]

    def get(self, request):
        from .. import qc_entry

        key = request.query_params.get("key")
        if key:
            return Response(qc_entry.lot_detail(request.client_id, key))
        stage = _qc_stage(request)
        return Response({"results": qc_entry.waiting_lots(
            request.client_id, stage=stage if stage in ("grey", "dyeing") else None)})

    def post(self, request):
        from apps.core.permissions import require_permission

        from .. import qc_entry

        approve = bool(request.data.get("approve"))
        require_permission(request.user, "approve_qc" if approve else "manage_inventory",
                           "Approving QC needs QC approval rights." if approve else None)
        return Response(qc_entry.save_qc(
            client=request.user.client, key=request.data.get("key"), rolls=request.data.get("rolls"),
            approve=approve, location_id=request.data.get("locationId") or None,
            jwo_id=request.data.get("jwoId") or None, remarks=request.data.get("remarks") or None,
            user=request.user,
        ))


class QCReprocessStockView(APIView):
    """GET /inventory/qc/reprocess-stock/ -- rolls in reprocess stock, by batch, with open reprocess orders."""

    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]

    def get(self, request):
        from ..qc_entry import reprocess_rolls

        return Response({"results": reprocess_rolls(request.client_id)})


class QCWorkView(APIView):
    """
    GET /inventory/qc/work/?stage=grey|dyeing&q=&state=waiting|inspected|hold|rework|done
    One QC worklist: every lot with its state and the one next step (see
    ``qc_work``). Saving uses /qc/disposition/ and /qc/reinspection/.
    """

    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]

    def get(self, request):
        from ..qc_work import qc_worklist

        stage = _qc_stage(request)
        return Response(qc_worklist(
            request.client_id, stage=stage if stage in ("grey", "dyeing") else None,
            q=request.query_params.get("q") or None, state=request.query_params.get("state") or None,
        ))


class QCPendingInspectionsView(APIView):
    """
    GET /inventory/qc/pending/
    List all pending fabric inspections from Goods Receipts and Purchase Bills.
    Filtered by date, source, supplier, quality, design, and shade.
    """

    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]

    def get(self, request):
        client_id = request.client_id
        today = timezone.localdate()

        date_from_str = request.query_params.get("date_from") or request.query_params.get("start_date")
        date_to_str = request.query_params.get("date_to") or request.query_params.get("end_date")
        source_type = request.query_params.get("source_type") or "All"
        stage = _qc_stage(request)
        if stage == "grey":
            source_type = "Purchase"
        elif stage == "dyeing":
            source_type = "Job Work"
        supplier = request.query_params.get("supplier") or "All"
        quality = request.query_params.get("quality") or "All"
        design = request.query_params.get("design") or "All"
        shade = request.query_params.get("shade") or "All"

        pending_items = []

        # 1. Purchase GRNs
        if source_type in ["All", "Purchase"]:
            receipts_qs = (
                GoodsReceipt.objects.filter(
                    client_id=client_id,
                    deleted_at__isnull=True,
                    qc_status__in=["Pending", "On Hold"],
                )
                .select_related("party", "purchase_bill", "qc_by")
                .prefetch_related("lines__item")
            )

            if supplier != "All":
                receipts_qs = receipts_qs.filter(party__name=supplier)

            for r in receipts_qs.order_by("-receipt_date", "-created_at"):
                r_lines = list(r.lines.all())
                rec_date = r.receipt_date
                days_pending = (today - rec_date).days if rec_date else 0
                if days_pending < 0:
                    days_pending = 0

                for idx, line in enumerate(r_lines):
                    item = line.item
                    item_qual = getattr(item, "fabric_quality", "") or ""
                    item_des = getattr(item, "fabric_design", "") or ""
                    item_shd = getattr(item, "fabric_color", "") or ""

                    if quality != "All" and quality.lower() not in item_qual.lower():
                        continue
                    if design != "All" and design.lower() not in item_des.lower():
                        continue
                    if shade != "All" and shade.lower() not in item_shd.lower():
                        continue

                    rec_q = float(line.weighed_qty or line.received_qty or 0)
                    rej_q = float(line.rejected_qty or 0)
                    pend_q = rec_q - rej_q

                    pending_items.append({
                        "id": f"PEND-{str(r.id)[:8]}-{idx}",
                        "receiptId": str(r.id),
                        "lineId": str(line.id),
                        "billId": str(r.purchase_bill_id) if r.purchase_bill_id else None,
                        "refNo": r.grn_number,
                        "sourceType": "Purchase",
                        "grnNo": r.grn_number,
                        "date": rec_date.strftime("%d-%m-%Y") if rec_date else today.strftime("%d-%m-%Y"),
                        "rawDate": rec_date.isoformat() if rec_date else today.isoformat(),
                        "supplier": r.party.name if r.party else None,
                        "itemCode": item.sku if item else "—",
                        "itemName": item.name if item else None,
                        "quality": item_qual,
                        "design": item_des,
                        "shade": item_shd,
                        "lotNo": line.batch_number or None,
                        "takaRoll": idx + 1,
                        "receivedQty": rec_q,
                        "pendingQty": pend_q,
                        "uom": line.uom or (item.uom if item else None),
                        "days": days_pending,
                        "status": r.qc_status,
                        "inspector": r.qc_by.name if r.qc_by else None,
                        "shift": None,
                    })

        # 2. Job Work Inward
        if source_type in ["All", "Job Work"]:
            inwards_qs = (
                JWOInward.objects.filter(
                    client_id=client_id,
                    status__in=["Received", "Pending QC", "Pending", "On Hold"],
                )
                .select_related("job_work_order", "job_work_order__vendor", "job_work_order__process_plan")
            )
            if supplier != "All":
                inwards_qs = inwards_qs.filter(
                    Q(job_work_order__vendor_name=supplier) | Q(job_work_order__vendor__name=supplier)
                )

            for inw in inwards_qs.order_by("-date", "-created_at"):
                rec_date = inw.date
                days_pending = (today - rec_date).days if rec_date else 0
                if days_pending < 0:
                    days_pending = 0

                rec_q = float(inw.qty or 0)
                pend_q = float(inw.qty - inw.accepted - inw.rejected) if (inw.qty - inw.accepted - inw.rejected) > 0 else rec_q
                if pend_q <= 0 and inw.status not in ["Pending", "Pending QC", "On Hold"]:
                    continue

                jwo = inw.job_work_order
                plan = jwo.process_plan if jwo else None
                v_name = ((jwo.vendor_name or (jwo.vendor.name if jwo.vendor else None)) if jwo else None)
                it_code = (getattr(plan, "fabric_item", None) or None)
                it_name = ((plan.fabric_item or plan.fabric_quality) or None) if plan else None
                it_qual = getattr(plan, "fabric_quality", "") or ""
                it_des = ""
                it_shd = getattr(plan, "shade", "") or ""
                lot_no = (getattr(plan, "grey_lot_no", None) or None)

                if quality != "All" and quality.lower() not in (it_qual or it_name).lower():
                    continue
                if design != "All" and design.lower() not in it_des.lower():
                    continue
                if shade != "All" and shade.lower() not in it_shd.lower():
                    continue

                pending_items.append({
                    "id": f"PEND-JWO-{str(inw.id)}",
                    "receiptId": str(inw.id),
                    "lineId": str(inw.id),
                    "billId": None,
                    "refNo": inw.no,
                    "sourceType": "Job Work",
                    "grnNo": inw.no,
                    "date": rec_date.strftime("%d-%m-%Y") if rec_date else today.strftime("%d-%m-%Y"),
                    "rawDate": rec_date.isoformat() if rec_date else today.isoformat(),
                    "supplier": v_name,
                    "itemCode": it_code,
                    "itemName": it_name,
                    "quality": it_qual or it_name,
                    "design": it_des,
                    "shade": it_shd,
                    "lotNo": lot_no,
                    "takaRoll": 1,
                    "receivedQty": rec_q,
                    "pendingQty": pend_q,
                    "uom": None,
                    "days": days_pending,
                    "status": "Pending" if inw.status in ["Received", "Pending QC", "Pending"] else inw.status,
                    "inspector": "",
                    "shift": None,
                })

        # Masters for filter dropdowns
        suppliers = list(
            Party.objects.filter(client_id=client_id, type__in=["Vendor", "Both"], deleted_at__isnull=True)
            .values_list("name", flat=True)
            .distinct()
        )
        jwo_vendors = list(
            JobWorkOrder.objects.filter(client_id=client_id, vendor_name__isnull=False)
            .values_list("vendor_name", flat=True)
            .distinct()
        )
        all_suppliers = sorted(list(set([s for s in suppliers + jwo_vendors if s])))
        qualities = list(
            FabricQuality.objects.filter(client_id=client_id, deleted_at__isnull=True)
            .values_list("name", flat=True)
            .distinct()
            .order_by("name")
        )
        designs = list(
            Design.objects.filter(client_id=client_id, deleted_at__isnull=True)
            .values_list("name", flat=True)
            .distinct()
            .order_by("name")
        )
        shades = list(
            Shade.objects.filter(client_id=client_id, deleted_at__isnull=True)
            .values_list("name", flat=True)
            .distinct()
            .order_by("name")
        )
        inspectors = list(
            User.objects.filter(client_id=client_id, status="Active", deleted_at__isnull=True)
            .values_list("name", flat=True)
            .distinct()
            .order_by("name")
        )

        return Response({
            "pendingList": pending_items,
            "filters": {
                "suppliers": all_suppliers,
                "qualities": qualities,
                "designs": designs,
                "shades": shades,
                "inspectors": inspectors,
            },
        })


class QCBulkAssignView(APIView):
    """
    POST /inventory/qc/pending/bulk-assign/
    Assign inspector and shift to multiple pending inspections.
    """

    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]

    def post(self, request):
        client_id = request.client_id
        receipt_ids = request.data.get("receiptIds") or []
        inspector_name = request.data.get("inspector")
        shift = request.data.get("shift") or ""
        inspection_date = request.data.get("inspectionDate")

        if not inspector_name:
            return Response({"error": "Inspector name is required."}, status=status.HTTP_400_BAD_REQUEST)

        valid_receipts = GoodsReceipt.objects.filter(
            client_id=client_id,
            id__in=[r for r in receipt_ids if r]
        )
        for r in valid_receipts:
            r.qc_note = f"Assigned to {inspector_name} ({shift}) on {inspection_date or timezone.localdate()}"
            r.save(update_fields=["qc_note", "updated_at"])

        return Response({
            "success": True,
            "assignedCount": valid_receipts.count(),
            "message": f"Successfully assigned {len(receipt_ids)} inspections to {inspector_name} ({shift}).",
        })


class QCCompleteInspectionView(APIView):
    """
    POST /inventory/qc/complete/
    Submit the QC result of a goods receipt: the stock still at QC Hold is
    moved to Good / Second Quality / Reprocess / Reject (Hold stays) with real
    movements, across every GRN line.
    """

    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]
    permission_map = {"POST": ["approve_qc"]}

    def post(self, request):
        from .. import qc as qc_service

        client_id = request.client_id
        receipt_id = request.data.get("receiptId") or request.data.get("grnId")
        bill_id = request.data.get("billId")
        taka_items = request.data.get("takas")
        quantities = qc_service.parse_quantities(request.data)

        with transaction.atomic():
            if not receipt_id and bill_id:
                from apps.purchase import services as purchase_services

                bill = PurchaseBill.objects.filter(
                    client_id=client_id, id=bill_id, deleted_at__isnull=True
                ).first()
                if bill is None:
                    raise ValidationFailed("Bill not found.", field_errors={"billId": ["Not found."]})
                if not bill.goods_received:
                    result = purchase_services.receive_bill_goods(
                        bill, lines_payload=[], qc_status=None, user=request.user,
                    )
                    receipt_id = result["receipt"].id
                else:
                    receipt = GoodsReceipt.objects.filter(
                        client_id=client_id, purchase_bill=bill, deleted_at__isnull=True
                    ).order_by("-created_at").first()
                    receipt_id = receipt.id if receipt else None

            receipt = GoodsReceipt.objects.filter(
                client_id=client_id, id=receipt_id, deleted_at__isnull=True
            ).first() if receipt_id else None
            if receipt is None and receipt_id:
                receipt = GoodsReceipt.objects.filter(
                    client_id=client_id, grn_number__iexact=str(receipt_id), deleted_at__isnull=True
                ).first()
            inward = None
            if receipt is None and receipt_id:
                inward = JWOInward.objects.filter(client_id=client_id, id=receipt_id).first()
            if receipt is None and inward is None:
                raise ValidationFailed(
                    "Goods receipt or Job Work Inward not found.", field_errors={"receiptId": ["Not found."]}
                )

            if receipt is not None:
                jwo = None
                jwo_id = request.data.get("jwoId") or request.data.get("jobWorkOrderId")
                if jwo_id:
                    jwo = JobWorkOrder.objects.filter(client_id=client_id, id=jwo_id).first()
                    if jwo is None:
                        raise ValidationFailed("Job work order not found.", field_errors={"jwoId": ["Not found."]})

                result = qc_service.dispose_receipt(
                    receipt,
                    quantities=quantities,
                    line_quantities=request.data.get("lines") or None,
                    taka_dispositions=taka_items,
                    user=request.user,
                    remarks=request.data.get("remarks") or None,
                    job_work_order=jwo,
                    require_job_work_order=bool(jwo_id),
                    from_reprocess=bool(request.data.get("isReprocess") or request.data.get("is_reprocess")),
                )

                return Response({
                    "success": True,
                    "status": result["verdict"],
                    "stockPosted": result["stockPosted"],
                    "message": (
                        f"QC recorded for {receipt.grn_number}: {result['verdict']}."
                        + ("" if result["stockPosted"] else " This receipt has no stock posted, so no stock moved.")
                    ),
                    "goodQty": float(result["quantities"].get("good", 0)),
                    "secondQty": float(result["quantities"].get("second", 0)),
                    "reprocessQty": float(result["quantities"].get("reprocess", 0)),
                    "rejectQty": float(result["quantities"].get("reject", 0)),
                    "holdQty": float(result["quantities"].get("hold", 0)),
                    "postedStock": float(result["quantities"].get("good", 0)),
                    **{f"{k}Qty": float(v) for k, v in result["quantities"].items()},
                })
            else:
                total = sum(quantities.values(), ZERO)
                if total <= ZERO:
                    raise ValidationFailed(
                        "Enter the disposition quantities before approving.",
                        field_errors={"goodQty": ["Total must be greater than zero."]},
                    )
                verdict = qc_service.verdict_for(
                    {k: quantities[k] for k in ("good", "second", "reprocess", "reject")},
                    quantities["hold"],
                )
                inward.accepted = quantities["good"] + quantities["second"]
                inward.rejected = quantities["reject"]
                inward.status = verdict
                inward.save(update_fields=["accepted", "rejected", "status", "updated_at"])
                if quantities["reprocess"] > ZERO:
                    from apps.jobwork.models import JWOReprocess
                    if not JWOReprocess.objects.filter(client_id=client_id, inward_no=inward.no).exists():
                        on_date = timezone.localdate()
                        rp_no = allocate_number(request.user.client, "RP", on_date)
                        JWOReprocess.objects.create(
                            client_id=client_id,
                            job_work_order=inward.job_work_order,
                            no=rp_no,
                            inward_no=inward.no,
                            date=on_date,
                            qty=quantities["reprocess"],
                            reason=request.data.get("remarks") or f"Reprocess required from {inward.no}",
                            status="Pending",
                            created_by=request.user,
                        )
                return Response({
                    "success": True,
                    "status": verdict,
                    "stockPosted": False,
                    "message": f"QC recorded for Job Work Inward {inward.no}: {verdict}.",
                    "goodQty": float(quantities["good"]),
                    "secondQty": float(quantities["second"]),
                    "reprocessQty": float(quantities["reprocess"]),
                    "rejectQty": float(quantities["reject"]),
                    "holdQty": float(quantities["hold"]),
                    "postedStock": float(quantities["good"]),
                })


class QCRegisterView(APIView):
    """
    GET /inventory/qc/register/
    POST /inventory/qc/register/manual/
    Handles QC Inspection Register listing.
    """

    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]

    def get(self, request):
        client_id = request.client_id
        from_date = request.query_params.get("from_date")
        to_date = request.query_params.get("to_date")
        qc_status = request.query_params.get("status") or request.query_params.get("qc_status") or "All"
        supplier = request.query_params.get("supplier") or "All"
        quality = request.query_params.get("quality") or "All"
        design = request.query_params.get("design") or "All"
        shade = request.query_params.get("shade") or "All"

        qs = (
            GoodsReceipt.objects.filter(client_id=client_id, deleted_at__isnull=True)
            .select_related("party", "purchase_bill", "purchase_order", "qc_by", "created_by")
            .prefetch_related("lines__item")
            .order_by("-receipt_date", "-created_at")
        )
        stage = _qc_stage(request)
        if stage == "dyeing":
            qs = qs.none()

        if from_date:
            qs = qs.filter(receipt_date__gte=from_date)
        if to_date:
            qs = qs.filter(receipt_date__lte=to_date)
        if supplier != "All":
            qs = qs.filter(party__name=supplier)
        if qc_status != "All":
            if qc_status == "Completed":
                qs = qs.filter(qc_status="Approved")
            else:
                qs = qs.filter(qc_status=qc_status)

        records = []
        for r in qs:
            r_lines = list(r.lines.all())
            line0 = r_lines[0] if r_lines else None
            item = line0.item if line0 else None

            item_qual = getattr(item, "fabric_quality", "") or ""
            item_des = getattr(item, "fabric_design", "") or ""
            item_shd = getattr(item, "fabric_color", "") or ""

            if quality != "All" and quality.lower() not in item_qual.lower():
                continue
            if design != "All" and design.lower() not in item_des.lower():
                continue
            if shade != "All" and shade.lower() not in item_shd.lower():
                continue

            rec_qty = sum(float(l.weighed_qty or l.received_qty or 0) for l in r_lines)
            rej_qty = sum(float(l.rejected_qty or 0) for l in r_lines)

            meta = {}
            if r.qc_note and r.qc_note.strip().startswith("{") and r.qc_note.strip().endswith("}"):
                try:
                    meta = json.loads(r.qc_note)
                except (json.JSONDecodeError, TypeError, ValueError) as exc:
                    logger.exception("Failed parsing QC inspection meta JSON for receipt %s: %s", r.id, exc)
                    meta = {}

            display_status = "Completed" if r.qc_status == "Approved" else r.qc_status

            if meta and "goodQty" in meta:
                # Recorded by the QC disposition service (cumulative figures).
                good_qty = float(meta.get("goodQty") or 0) + float(meta.get("secondQualityQty") or 0)
                hold_qty = float(meta.get("holdQty") or 0)
                rep_qty = float(meta.get("reprocessQty") or 0)
                rej_qty = float(meta.get("rejectedQty") or 0)
                insp_qty = good_qty + hold_qty + rep_qty + rej_qty
            elif r.qc_status == "Pending":
                insp_qty = good_qty = hold_qty = rep_qty = 0.0
                rej_qty = 0.0
            else:
                # Older receipts carry only the verdict and the per-line
                # rejected quantity; nothing else was recorded.
                insp_qty = rec_qty
                good_qty = rec_qty - rej_qty if r.qc_status == "Approved" else 0.0
                hold_qty = rec_qty - rej_qty if r.qc_status == "On Hold" else 0.0
                rep_qty = rec_qty - rej_qty if r.qc_status == "Rework" else 0.0

            measurements = meta.get("measurements") or []
            if not measurements and r_lines:
                for idx, l in enumerate(r_lines):
                    l_item = l.item
                    measurements.append({
                        "srNo": idx + 1,
                        "lineId": str(l.id),
                        "takaNo": None,
                        "actualQty": float(l.weighed_qty or l.received_qty or 0),
                        "width": float(l_item.fabric_width) if (l_item and getattr(l_item, "fabric_width", None)) else None,
                        "weight": float(l.weighed_qty) if l.weighed_qty else None,
                        "rejected": float(l.rejected_qty or 0),
                        "remarks": None,
                    })

            created_at = timezone.localtime(r.created_at) if r.created_at else None
            audit_trail = [
                {
                    "dateTime": created_at.strftime("%d-%m-%Y %H:%M") if created_at else "",
                    "action": "Created",
                    "user": r.created_by.name if r.created_by_id else None,
                    "remarks": f"GRN {r.grn_number} received",
                }
            ]
            if r.qc_at:
                audit_trail.append({
                    "dateTime": timezone.localtime(r.qc_at).strftime("%d-%m-%Y %H:%M"),
                    "action": display_status,
                    "user": r.qc_by.name if r.qc_by else None,
                    "remarks": _qc_remarks(r),
                })

            records.append({
                "id": str(r.id),
                "qcNo": r.grn_number,
                "receiptId": str(r.id),
                "date": r.receipt_date.strftime("%d-%m-%Y") if r.receipt_date else "",
                "rawDate": r.receipt_date.isoformat() if r.receipt_date else "",
                "sourceType": "Purchase" if (r.purchase_bill_id or r.purchase_order_id) else "Manual",
                "refNo": r.grn_number,
                "grnNo": r.grn_number,
                "supplier": r.party.name if r.party else None,
                "itemCode": item.sku if item else "—",
                "itemName": item.name if item else None,
                "quality": item_qual or (item.name if item else ""),
                "design": item_des or None,
                "shade": item_shd or None,
                "lotNo": line0.batch_number if line0 and line0.batch_number else None,
                "takaRoll": len(r_lines) or 1,
                "receivedQty": rec_qty,
                "inspectedQty": insp_qty,
                "goodQty": good_qty,
                "holdQty": hold_qty,
                "reprocessQty": rep_qty,
                "rejectedQty": rej_qty,
                "uom": line0.uom if line0 and line0.uom else None,
                "status": display_status,
                "inspector": r.qc_by.name if r.qc_by else None,
                "remarks": meta.get("remarks") or (r.qc_note if not meta else ""),
                "measurements": measurements,
                "auditTrail": audit_trail,
            })

        # Include Job Work Inward records in register
        inwards_qs = (
            JWOInward.objects.filter(client_id=client_id)
            .select_related("job_work_order", "job_work_order__vendor", "job_work_order__process_plan")
            .order_by("-date", "-created_at")
        )
        if stage == "grey":
            inwards_qs = inwards_qs.none()
        if from_date:
            inwards_qs = inwards_qs.filter(date__gte=from_date)
        if to_date:
            inwards_qs = inwards_qs.filter(date__lte=to_date)
        if supplier != "All":
            inwards_qs = inwards_qs.filter(
                Q(job_work_order__vendor_name=supplier) | Q(job_work_order__vendor__name=supplier)
            )

        for inw in inwards_qs:
            jwo = inw.job_work_order
            plan = jwo.process_plan if jwo else None
            v_name = ((jwo.vendor_name or (jwo.vendor.name if jwo.vendor else None)) if jwo else None)
            it_code = (getattr(plan, "fabric_item", None) or None)
            it_name = ((plan.fabric_item or plan.fabric_quality) or None) if plan else None
            it_qual = getattr(plan, "fabric_quality", "") or ""
            it_des = ""
            it_shd = getattr(plan, "shade", "") or ""
            lot_no = (getattr(plan, "grey_lot_no", None) or None)

            if quality != "All" and quality.lower() not in (it_qual or it_name).lower():
                continue
            if design != "All" and design.lower() not in it_des.lower():
                continue
            if shade != "All" and shade.lower() not in it_shd.lower():
                continue

            disp_status = "Completed" if inw.status in ["Completed", "Approved"] else ("On Hold" if inw.status == "On Hold" else inw.status)
            if qc_status != "All" and disp_status != qc_status:
                continue

            rec_q = float(inw.qty or 0)
            good_q = float(inw.accepted) if disp_status == "Completed" else 0.0
            hold_q = rec_q if disp_status == "On Hold" else 0.0
            rep_q = rec_q if inw.status in ["Rework", "Reprocess"] else 0.0
            rej_q = float(inw.rejected)
            insp_q = rec_q if disp_status in ["Completed", "On Hold", "Rejected", "Rework", "Reprocess"] else (float(inw.accepted + inw.rejected) if (inw.accepted or inw.rejected) else 0.0)

            measurements = [
                {
                    "srNo": 1,
                    "takaNo": 1,
                    "actualQty": rec_q,
                    "width": float(plan.width) if (plan and getattr(plan, "width", None)) else None,
                    "weight": float(plan.weight_kg) if (plan and getattr(plan, "weight_kg", None)) else None,
                    "good": good_q,
                    "hold": hold_q,
                    "reprocess": rep_q,
                    "rejected": rej_q,
                    "remarks": inw.challan_no or "-",
                }
            ]

            created_at = timezone.localtime(inw.created_at) if inw.created_at else None
            audit_trail = [
                {
                    "dateTime": created_at.strftime("%d-%m-%Y %H:%M") if created_at else "",
                    "action": "Created",
                    "user": inw.created_by.name if inw.created_by_id else None,
                    "remarks": f"Job Work Inward {inw.no} received",
                }
            ]
            if inw.status in ["Completed", "Approved"] and inw.updated_at:
                audit_trail.append({
                    "dateTime": timezone.localtime(inw.updated_at).strftime("%d-%m-%Y %H:%M"),
                    "action": "Completed",
                    "user": inw.updated_by.name if inw.updated_by_id else None,
                    "remarks": None,
                })

            records.append({
                "id": str(inw.id),
                "qcNo": f"QC-JW-{inw.no}",
                "receiptId": str(inw.id),
                "date": inw.date.strftime("%d-%m-%Y") if inw.date else "",
                "rawDate": inw.date.isoformat() if inw.date else "",
                "sourceType": "Job Work",
                "refNo": inw.no,
                "grnNo": inw.no,
                "supplier": v_name,
                "itemCode": it_code,
                "itemName": it_name,
                "quality": it_qual or it_name,
                "design": it_des,
                "shade": it_shd,
                "lotNo": lot_no,
                "takaRoll": 1,
                "receivedQty": rec_q,
                "inspectedQty": insp_q,
                "goodQty": good_q,
                "holdQty": hold_q,
                "reprocessQty": rep_q,
                "rejectedQty": rej_q,
                "uom": None,
                "status": disp_status,
                "inspector": inw.updated_by.name if inw.updated_by_id else None,
                "remarks": f"Challan: {inw.challan_no}" if inw.challan_no else "Job Work Inward",
                "measurements": measurements,
                "auditTrail": audit_trail,
            })

        suppliers = list(
            Party.objects.filter(client_id=client_id, type__in=["Vendor", "Both"], deleted_at__isnull=True)
            .values_list("name", flat=True)
            .distinct()
        )
        jwo_vendors = list(
            JobWorkOrder.objects.filter(client_id=client_id, vendor_name__isnull=False)
            .values_list("vendor_name", flat=True)
            .distinct()
        )
        all_suppliers = sorted(list(set([s for s in suppliers + jwo_vendors if s])))
        qualities = list(
            FabricQuality.objects.filter(client_id=client_id, deleted_at__isnull=True)
            .values_list("name", flat=True)
            .distinct()
            .order_by("name")
        )
        designs = list(
            Design.objects.filter(client_id=client_id, deleted_at__isnull=True)
            .values_list("name", flat=True)
            .distinct()
            .order_by("name")
        )
        shades = list(
            Shade.objects.filter(client_id=client_id, deleted_at__isnull=True)
            .values_list("name", flat=True)
            .distinct()
            .order_by("name")
        )

        return Response({
            "records": records,
            "filters": {
                "statuses": ["All", "Completed", "Partially Approved", "Pending", "On Hold", "Rework", "Rejected"],
                "suppliers": all_suppliers,
                "qualities": qualities,
                "designs": designs,
                "shades": shades,
            },
        })

    def post(self, request):
        """Manual QC register entry: a goods receipt *record* for inspection.

        It posts no stock (goods received without a bill have no valuation
        document); the QC disposition of such a receipt records the result
        only and says so. Every input is explicit -- no defaults.
        """
        client_id = request.client_id
        data = request.data
        field_errors = {}

        party = None
        if data.get("supplierId"):
            party = Party.objects.filter(client_id=client_id, id=data.get("supplierId"), deleted_at__isnull=True).first()
        elif data.get("supplier"):
            party = Party.objects.filter(client_id=client_id, name=data.get("supplier"), deleted_at__isnull=True).first()
        if party is None:
            field_errors["supplierId"] = ["Choose the supplier."]

        location = None
        if data.get("locationId"):
            location = Location.objects.filter(client_id=client_id, id=data.get("locationId"), deleted_at__isnull=True).first()
        if location is None:
            field_errors["locationId"] = ["Choose the receiving godown."]

        item = None
        if data.get("itemId"):
            item = Item.objects.filter(client_id=client_id, id=data.get("itemId"), deleted_at__isnull=True).first()
        elif data.get("itemCode"):
            item = Item.objects.filter(client_id=client_id, sku=data.get("itemCode"), deleted_at__isnull=True).first()
        if item is None:
            field_errors["itemId"] = ["Choose the item."]

        # Blank: allocated from the LOT series once the entry is valid.
        lot_no = (data.get("lotNo") or "").strip()
        try:
            received_qty = round4(D(data.get("receivedQty")))
        except (InvalidOperation, TypeError, ValueError) as exc:
            logger.exception("Failed parsing received quantity in manual QC entry: %s", exc)
            received_qty = ZERO
        if received_qty <= ZERO:
            field_errors["receivedQty"] = ["Enter the received quantity."]
        try:
            taka_count = int(data.get("takaRoll") or 0)
        except (TypeError, ValueError):
            taka_count = 0
        if taka_count <= 0:
            field_errors["takaRoll"] = ["Enter the number of takas / rolls."]
        try:
            rec_date = date.fromisoformat(str(data.get("inspectionDate") or data.get("receiptDate") or ""))
        except ValueError:
            rec_date = None
        if rec_date is None:
            field_errors["inspectionDate"] = ["Enter the receipt date (YYYY-MM-DD)."]
        if field_errors:
            raise ValidationFailed("The QC register entry is incomplete.", field_errors=field_errors)

        with transaction.atomic():
            grn_no = allocate_number(request.user.client, "GRN", rec_date)
            if not lot_no:
                lot_no = allocate_number(request.user.client, "LOT", rec_date)
            grn = GoodsReceipt.objects.create(
                client_id=client_id,
                grn_number=grn_no,
                party=party,
                receipt_date=rec_date,
                location=location,
                qc_status="Pending",
                created_by=request.user,
            )
            # One line for the received quantity; the taka count is recorded
            # on the line rather than inventing an equal split per taka.
            line_kwargs = dict(
                client_id=client_id,
                goods_receipt=grn,
                item=item,
                uom=item.uom,
                stock_uom=item.uom,
                stock_qty=received_qty,
                ordered_qty=received_qty,
                received_qty=received_qty,
                batch_number=lot_no,
            )
            if "roll_count" in {f.name for f in GoodsReceiptLine._meta.get_fields()}:
                line_kwargs["roll_count"] = taka_count
            GoodsReceiptLine.objects.create(**line_kwargs)

        return Response({
            "success": True,
            "message": f"QC inspection record {grn_no} created (no stock posted).",
            "receiptId": str(grn.id),
            "grnNo": grn_no,
        }, status=status.HTTP_201_CREATED)


class QCDispositionView(APIView):
    """
    GET  /inventory/qc/disposition/  -- receipts (and job-work inwards) to disposition
    POST /inventory/qc/disposition/  -- save a draft, or approve and move stock

    Approval goes through ``apps.inventory.qc.dispose_receipt``: every GRN line
    and every taka still at QC Hold is moved to Good / Second Quality /
    Reprocess / Reject with real movements; Hold stays at QC Hold.
    """

    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]
    permission_map = {"POST": ["approve_qc"]}

    def _receipt_row(self, r):
        from .. import qc as qc_service

        r_lines = list(r.lines.all())
        line0 = r_lines[0] if r_lines else None
        item = line0.item if line0 else None
        meta = _qc_meta(r)
        pending = qc_service.pool_quantity(r)
        has_stock = qc_service.receipt_has_stock(r)
        tot_rec = sum(float(l.stock_qty or l.received_qty or 0) for l in r_lines)
        # What the form must account for now: what is still at QC Hold, or
        # (for a record-only receipt) the received quantity.
        to_inspect = float(pending) if has_stock else (tot_rec if r.qc_status in ("Pending", "On Hold") else 0.0)
        draft = meta.get("draft") or {}
        return {
            "id": str(r.id),
            "qcNo": r.grn_number,
            "grnNo": r.grn_number,
            "date": r.receipt_date.isoformat() if r.receipt_date else "",
            "status": "Completed" if r.qc_status == "Approved" else r.qc_status,
            "sourceType": "Purchase (GRN)",
            "supplier": r.party.name if r.party_id else None,
            "itemCode": item.sku if item else None,
            "itemName": item.name if item else None,
            "quality": getattr(item, "fabric_quality", None) or None,
            "design": getattr(item, "fabric_design", None) or None,
            "shade": getattr(item, "fabric_color", None) or None,
            "lotNo": line0.batch_number if line0 else None,
            # Takas received on the GRN (QC splits carry another source type).
            "takaRoll": LotTaka.objects.filter(
                client_id=r.client_id, source_document_id=r.id, source_document_type="GRN",
                deleted_at__isnull=True,
            ).count(),
            "receivedQty": tot_rec,
            "inspectedQty": to_inspect,
            "pendingQcQty": float(pending),
            "stockPosted": has_stock,
            # The form starts from the saved draft, else everything pending
            # is unassigned (0) -- the inspector enters the split.
            "goodQty": float(draft.get("goodQty", 0) or 0),
            "secondQualityQty": float(draft.get("secondQualityQty", 0) or 0),
            "holdQty": float(draft.get("holdQty", 0) or 0),
            "reprocessQty": float(draft.get("reprocessQty", 0) or 0),
            "rejectedQty": float(draft.get("rejectedQty", 0) or 0),
            "previousDisposition": {
                k: meta.get(k) for k in ("goodQty", "secondQualityQty", "reprocessQty", "rejectedQty", "holdQty")
            } if meta.get("cumulative") else None,
            "remarks": (draft.get("remarks") if draft else None) or _qc_remarks(r),
            "lines": [
                {
                    "lineId": str(l.id),
                    "itemCode": l.item.sku if l.item_id else None,
                    "itemName": l.item.name if l.item_id else None,
                    "lotNo": l.batch_number,
                    "uom": l.stock_uom or l.uom,
                    "receivedQty": float(l.stock_qty or l.received_qty or 0),
                    "rejectedQty": float(l.rejected_qty or 0),
                }
                for l in r_lines
            ],
            "measurements": draft.get("measurements") or meta.get("measurements") or [],
        }

    def get(self, request):
        client_id = request.client_id
        receipt_id = request.query_params.get("receipt_id")

        receipts_qs = (
            GoodsReceipt.objects.filter(client_id=client_id, deleted_at__isnull=True)
            .select_related("party", "purchase_bill", "qc_by")
            .prefetch_related("lines__item")
            .order_by("-receipt_date", "-created_at")
        )
        stage = _qc_stage(request)
        if receipt_id:
            # A link to one record opens the stage that record belongs to.
            if GoodsReceipt.objects.filter(client_id=client_id, id=receipt_id).exists():
                stage = "grey"
            elif JWOInward.objects.filter(client_id=client_id, id=receipt_id).exists():
                stage = "dyeing"
        receipts = [] if stage == "dyeing" else list(receipts_qs[:40])
        if receipt_id and stage != "dyeing" and not any(str(r.id) == str(receipt_id) for r in receipts):
            receipts += list(receipts_qs.filter(id=receipt_id))

        receipts_list = []
        target_receipt = None
        for r in receipts:
            row = self._receipt_row(r)
            receipts_list.append(row)
            if receipt_id and str(r.id) == str(receipt_id):
                target_receipt = row

        # Job Work Inwards are listed for their QC status; their stock is
        # posted by the job-work module.
        inwards_qs = (
            JWOInward.objects.filter(client_id=client_id)
            .select_related("job_work_order", "job_work_order__vendor", "job_work_order__process_plan")
            .order_by("-date", "-created_at")
        )
        for inw in ([] if stage == "grey" else inwards_qs[:40]):
            jwo = inw.job_work_order
            plan = jwo.process_plan if jwo else None
            rec_q = float(inw.qty or 0)
            inw_obj = {
                "id": str(inw.id),
                "qcNo": inw.no,
                "grnNo": inw.no,
                "date": inw.date.isoformat() if inw.date else "",
                "status": "Completed" if inw.status in ["Approved", "Completed"] else inw.status,
                "sourceType": "Job Work",
                "supplier": ((jwo.vendor_name or (jwo.vendor.name if jwo.vendor else None)) if jwo else None),
                "itemCode": (plan.fabric_item or None) if plan else None,
                "itemName": (plan.fabric_item or plan.fabric_quality or None) if plan else None,
                "quality": (plan.fabric_quality or None) if plan else None,
                "design": None,
                "shade": (plan.shade or None) if plan else None,
                "lotNo": (plan.grey_lot_no or None) if plan else None,
                "takaRoll": (plan.no_of_rolls if plan else None),
                "inspectedQty": rec_q,
                "receivedQty": rec_q,
                "goodQty": float(inw.accepted) if inw.status in ["Approved", "Completed"] else 0.0,
                "holdQty": 0.0,
                "reprocessQty": 0.0,
                "rejectedQty": float(inw.rejected),
                "remarks": inw.challan_no or None,
                "measurements": [],
            }
            receipts_list.append(inw_obj)
            if receipt_id and str(inw.id) == str(receipt_id):
                target_receipt = inw_obj

        if not target_receipt and receipts_list:
            target_receipt = receipts_list[0]

        return Response({"inspections": receipts_list, "selected": target_receipt})

    def post(self, request):
        from .. import qc as qc_service

        client_id = request.client_id
        receipt_id = request.data.get("receiptId")
        if not receipt_id:
            raise ValidationFailed("receiptId is required.", field_errors={"receiptId": ["Required."]})

        is_draft = bool(request.data.get("isDraft", False))
        quantities = qc_service.parse_quantities(request.data)
        remarks = request.data.get("remarks") or ""
        measurements = request.data.get("measurements") or []

        receipt = GoodsReceipt.objects.filter(client_id=client_id, id=receipt_id, deleted_at__isnull=True).first()
        inward = None
        if receipt is None:
            inward = JWOInward.objects.filter(client_id=client_id, id=receipt_id).first()
        if receipt is None and inward is None:
            raise ValidationFailed("Inspection source record not found.", field_errors={"receiptId": ["Not found."]})

        with transaction.atomic():
            if receipt is not None:
                if is_draft:
                    # A draft changes nothing but the saved form: status and
                    # stock stay exactly as they are.
                    meta = _qc_meta(receipt)
                    meta["draft"] = {
                        "goodQty": float(quantities["good"]),
                        "secondQualityQty": float(quantities["second"]),
                        "holdQty": float(quantities["hold"]),
                        "reprocessQty": float(quantities["reprocess"]),
                        "rejectedQty": float(quantities["reject"]),
                        "remarks": remarks,
                        "measurements": measurements,
                        "savedAt": timezone.now().isoformat(),
                    }
                    if not _qc_meta(receipt) and receipt.qc_note:
                        meta["remarks"] = receipt.qc_note
                    receipt.qc_note = json.dumps(meta)
                    receipt.save(update_fields=["qc_note", "updated_at"])
                    return Response({
                        "success": True, "isDraft": True, "status": receipt.qc_status,
                        "message": "Draft saved. Stock is not moved until the disposition is approved.",
                    })

                jwo = None
                jwo_id = request.data.get("jwoId") or request.data.get("jobWorkOrderId")
                if jwo_id:
                    jwo = JobWorkOrder.objects.filter(client_id=client_id, id=jwo_id).first()
                    if jwo is None:
                        raise ValidationFailed("Job work order not found.", field_errors={"jwoId": ["Not found."]})
                result = qc_service.dispose_receipt(
                    receipt,
                    quantities=quantities,
                    line_quantities=request.data.get("lines") or None,
                    user=request.user,
                    remarks=remarks or None,
                    measurements=measurements,
                    job_work_order=jwo,
                )
                message = (
                    f"QC disposition approved ({result['verdict']}). "
                    + (f"{result['movements']} stock movement(s) posted."
                       if result["stockPosted"] else "This receipt has no stock posted, so no stock moved.")
                )
                if result["reprocessOrder"] is not None:
                    message += f" Reprocess order {result['reprocessOrder'].no} created."
                return Response({
                    "success": True, "isDraft": False, "status": result["verdict"],
                    "stockPosted": result["stockPosted"], "message": message,
                })

            # Job work inward: the processed rolls wait at QC Hold; approval
            # routes them like a purchase receipt (good -> godown, the rest to
            # their stock locations) and records the verdict on the inward.
            total = sum(quantities.values(), ZERO)
            if not is_draft and total <= ZERO:
                raise ValidationFailed(
                    "Enter the disposition quantities before approving.",
                    field_errors={"goodQty": ["Total must be greater than zero."]},
                )
            if is_draft:
                return Response({
                    "success": True, "isDraft": True, "status": inward.status,
                    "message": "Draft not stored for job-work inwards; nothing changed.",
                })
            verdict = qc_service.verdict_for(
                {k: quantities[k] for k in ("good", "second", "reprocess", "reject")},
                quantities["hold"],
            )
            waiting, _ = qc_service.jobwork_inward_pool(inward)
            if not waiting and inward.status not in ("Received", "Pending", "QC Pending", "On Hold"):
                raise Conflict(
                    f"QC for {inward.no} is already done ({inward.status}).",
                    code="QC_ALREADY_DONE",
                )
            good_location_id = request.data.get("locationId") or Location.objects.filter(
                client_id=client_id, is_active=True, deleted_at__isnull=True, type__in=["Warehouse", "Godown"],
            ).exclude(code__startswith="SYS-").order_by("created_at").values_list("id", flat=True).first()
            moved = qc_service.dispose_jobwork_inward(
                inward, quantities=quantities, good_location_id=good_location_id,
                user=request.user, remarks=remarks or None,
            )
            inward.accepted = quantities["good"] + quantities["second"]
            inward.rejected = quantities["reject"]
            inward.status = verdict
            inward.save(update_fields=["accepted", "rejected", "status", "updated_at"])
            rp_no = None
            if quantities["reprocess"] > ZERO:
                from apps.jobwork.models import JWOReprocess

                if not JWOReprocess.objects.filter(client_id=client_id, inward_no=inward.no).exists():
                    on_date = timezone.localdate()
                    rp_no = allocate_number(request.user.client, "RP", on_date)
                    JWOReprocess.objects.create(
                        client_id=client_id,
                        job_work_order=inward.job_work_order,
                        no=rp_no,
                        inward_no=inward.no,
                        date=on_date,
                        qty=quantities["reprocess"],
                        reason=remarks or f"Reprocess required from {inward.no}",
                        status="Pending",
                        created_by=request.user,
                    )
            return Response({
                "success": True, "isDraft": False, "status": verdict,
                "stockPosted": moved > 0,
                "message": f"Job work QC recorded ({verdict})."
                + (f" {moved} stock movement(s) posted." if moved else "")
                + (f" Reprocess order {rp_no} created." if rp_no else ""),
            })


class QCPurchaseReturnView(APIView):
    """
    POST /inventory/qc/purchase-return/
         { rolls: [{lotTakaId, qty}], reason, date?, lossId? }
    Return QC-rejected purchased rolls to the supplier as a debit note
    (apps.purchase.qc_returns). Partial quantities allowed.
    """
    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]

    def post(self, request):
        from apps.core.permissions import require_permission
        from apps.purchase.qc_returns import return_rejected_rolls

        require_permission(request.user, "cancel_purchase_document",
                           "Raising a purchase return needs purchase return rights.")
        returns = return_rejected_rolls(
            client=request.user.client, rolls=request.data.get("rolls"), reason=request.data.get("reason"),
            user=request.user, return_date=request.data.get("date") or None, loss_id=request.data.get("lossId"),
        )
        return Response({"returns": [{
            "id": str(r.id), "returnNumber": r.return_number, "supplier": r.party_name,
            "billNumber": r.purchase_bill.bill_number, "total": str(r.total),
        } for r in returns]}, status=status.HTTP_201_CREATED)


class QCReportsView(APIView):
    """
    GET /inventory/qc/reports/
    Provides comprehensive QC analytics and 6 detailed drill-down reports:
    1. Supplier-wise Rejection Report
    2. Fabric Item / Quality-wise Defect Report
    3. Hold & Pending Inspection Report
    4. Reprocess History & Recovery Report
    5. Inspector Performance Report
    6. Date-wise QC Summary
    Calculated 100% dynamically from database transactions with zero mock or dump data.
    """

    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]

    def get(self, request):
        client_id = request.client_id
        today = timezone.localdate()

        date_from_str = request.query_params.get("date_from") or request.query_params.get("start_date")
        date_to_str = request.query_params.get("date_to") or request.query_params.get("end_date")
        supplier_filter = request.query_params.get("supplier") or "All"
        quality_filter = request.query_params.get("quality") or "All"

        start_date = today.replace(day=1) - timedelta(days=90)
        try:
            if date_from_str:
                start_date = date.fromisoformat(date_from_str)
        except (ValueError, TypeError) as exc:
            logger.exception("Invalid date_from parameter in QC reports: %s", exc)

        end_date = today
        try:
            if date_to_str:
                end_date = date.fromisoformat(date_to_str)
        except (ValueError, TypeError) as exc:
            logger.exception("Invalid date_to parameter in QC reports: %s", exc)

        # 1. Base querysets
        receipts_qs = (
            GoodsReceipt.objects.filter(
                client_id=client_id,
                deleted_at__isnull=True,
                receipt_date__range=[start_date, end_date],
            )
            .select_related("party", "purchase_bill", "qc_by")
            .prefetch_related("lines__item")
        )
        if supplier_filter != "All":
            receipts_qs = receipts_qs.filter(party__name=supplier_filter)

        inwards_qs = (
            JWOInward.objects.filter(
                client_id=client_id,
                date__range=[start_date, end_date],
            )
            .select_related("job_work_order", "job_work_order__vendor", "job_work_order__process_plan")
        )
        if supplier_filter != "All":
            inwards_qs = inwards_qs.filter(
                Q(job_work_order__vendor_name=supplier_filter) | Q(job_work_order__vendor__name=supplier_filter)
            )

        reprocess_qs = (
            JWOReprocess.objects.filter(
                client_id=client_id,
                date__range=[start_date, end_date],
            )
            .select_related("job_work_order", "job_work_order__vendor", "job_work_order__process_plan")
            .order_by("-date", "-created_at")
        )
        stage = _qc_stage(request)
        reprocess_qs = reprocess_qs.filter(_reprocess_stage_q(client_id, stage))
        if stage == "dyeing":
            receipts_qs = receipts_qs.none()
        elif stage == "grey":
            inwards_qs = inwards_qs.none()

        # 2. Aggregations for Reports

        # Report 1: Supplier-wise Rejection
        supplier_map = {}
        for r in receipts_qs:
            s_name = r.party.name if r.party else None
            top_def = _qc_remarks(r)
            if s_name not in supplier_map:
                supplier_map[s_name] = {
                    "supplier": s_name, "receiptsCount": 0, "receivedQty": 0.0,
                    "inspectedQty": 0.0, "goodQty": 0.0, "holdQty": 0.0,
                    "reprocessQty": 0.0, "rejectedQty": 0.0, "topDefect": top_def,
                }
            r_lines = list(r.lines.all())
            rec_q = sum(float(l.weighed_qty or l.received_qty or 0) for l in r_lines)
            rej_q = sum(float(l.rejected_qty or 0) for l in r_lines)
            supplier_map[s_name]["receiptsCount"] += 1
            supplier_map[s_name]["receivedQty"] += rec_q
            if r.qc_status != "Pending":
                supplier_map[s_name]["inspectedQty"] += rec_q
            if r.qc_status in ("Approved", "Partially Approved"):
                supplier_map[s_name]["goodQty"] += max(0.0, rec_q - rej_q)
            elif r.qc_status == "On Hold":
                supplier_map[s_name]["holdQty"] += max(0.0, rec_q - rej_q)
            elif r.qc_status in ["Rework", "Reprocess"]:
                supplier_map[s_name]["reprocessQty"] += rec_q
            supplier_map[s_name]["rejectedQty"] += rej_q

        for inw in inwards_qs:
            jwo = inw.job_work_order
            v_name = ((jwo.vendor_name or (jwo.vendor.name if jwo.vendor else None)) if jwo else None)
            if v_name not in supplier_map:
                supplier_map[v_name] = {
                    "supplier": v_name, "receiptsCount": 0, "receivedQty": 0.0,
                    "inspectedQty": 0.0, "goodQty": 0.0, "holdQty": 0.0,
                    "reprocessQty": 0.0, "rejectedQty": 0.0, "topDefect": None,
                }
            rec_q = float(inw.qty or 0)
            supplier_map[v_name]["receiptsCount"] += 1
            supplier_map[v_name]["receivedQty"] += rec_q
            if inw.status not in ["Pending", "Pending QC", "Received"]:
                supplier_map[v_name]["inspectedQty"] += rec_q
            if inw.status in ["Approved", "Completed"]:
                supplier_map[v_name]["goodQty"] += float(inw.accepted)
            elif inw.status == "On Hold":
                supplier_map[v_name]["holdQty"] += rec_q
            elif inw.status in ["Rework", "Reprocess"]:
                supplier_map[v_name]["reprocessQty"] += rec_q
            supplier_map[v_name]["rejectedQty"] += float(inw.rejected)

        supplier_rejection_report = []
        for s in supplier_map.values():
            insp = s["inspectedQty"] or s["receivedQty"] or 1.0
            rej_pct = round((s["rejectedQty"] / insp) * 100, 1)
            grade = "A" if rej_pct < 2.0 else ("B" if rej_pct < 5.0 else ("C" if rej_pct < 10.0 else "D"))
            supplier_rejection_report.append({
                **s,
                "rejectionPct": rej_pct,
                "grade": grade,
                "passPct": round((s["goodQty"] / insp) * 100, 1) if insp else 0.0,
            })
        supplier_rejection_report.sort(key=lambda x: x["rejectionPct"], reverse=True)

        # Report 2: Fabric Item / Quality-wise Defect
        quality_map = {}
        for r in receipts_qs:
            for l in r.lines.all():
                item = l.item
                q_name = getattr(item, "fabric_quality", "") or (item.name if item else None)
                sku = getattr(item, "sku", None)
                if quality_filter != "All" and quality_filter.lower() not in q_name.lower():
                    continue
                if q_name not in quality_map:
                    quality_map[q_name] = {
                        "quality": q_name, "itemCode": sku, "inspectedQty": 0.0,
                        "goodQty": 0.0, "defectiveQty": 0.0, "lotsCount": 0,
                        "primaryDefect": None,
                    }
                rec_q = float(l.weighed_qty or l.received_qty or 0)
                rej_q = float(l.rejected_qty or 0)
                quality_map[q_name]["inspectedQty"] += rec_q
                quality_map[q_name]["lotsCount"] += 1
                if r.qc_status in ("Approved", "Partially Approved"):
                    quality_map[q_name]["goodQty"] += max(0.0, rec_q - rej_q)
                quality_map[q_name]["defectiveQty"] += rej_q

        for inw in inwards_qs:
            jwo = inw.job_work_order
            plan = jwo.process_plan if jwo else None
            q_name = (getattr(plan, "fabric_quality", "") or getattr(plan, "fabric_item", "") or None)
            sku = (getattr(plan, "fabric_item", None) or None)
            if quality_filter != "All" and quality_filter.lower() not in q_name.lower():
                continue
            if q_name not in quality_map:
                quality_map[q_name] = {
                    "quality": q_name, "itemCode": sku, "inspectedQty": 0.0,
                    "goodQty": 0.0, "defectiveQty": 0.0, "lotsCount": 0,
                    "primaryDefect": None,
                }
            rec_q = float(inw.qty or 0)
            quality_map[q_name]["inspectedQty"] += rec_q
            quality_map[q_name]["lotsCount"] += 1
            if inw.status in ["Approved", "Completed"]:
                quality_map[q_name]["goodQty"] += float(inw.accepted)
            quality_map[q_name]["defectiveQty"] += float(inw.rejected)

        fabric_defects_report = []
        for q in quality_map.values():
            insp = q["inspectedQty"] or 1.0
            def_pct = round((q["defectiveQty"] / insp) * 100, 1)
            fabric_defects_report.append({
                **q,
                "defectPct": def_pct,
                "passPct": round((q["goodQty"] / insp) * 100, 1),
            })
        fabric_defects_report.sort(key=lambda x: x["defectPct"], reverse=True)

        # Report 3: Hold & Pending Inspection
        hold_pending_report = []
        for r in receipts_qs.filter(qc_status__in=["Pending", "On Hold"]):
            r_lines = list(r.lines.all())
            line0 = r_lines[0] if r_lines else None
            rec_q = sum(float(l.weighed_qty or l.received_qty or 0) for l in r_lines)
            days = (today - r.receipt_date).days if r.receipt_date else 0
            aging = "0-3 Days" if days <= 3 else ("4-7 Days" if days <= 7 else ("8-14 Days" if days <= 14 else "15+ Days"))
            hold_pending_report.append({
                "refNo": r.grn_number,
                "sourceType": "Purchase",
                "date": r.receipt_date.strftime("%d-%m-%Y") if r.receipt_date else "",
                "supplier": r.party.name if r.party else None,
                "itemCode": line0.item.sku if line0 and line0.item else "—",
                "itemName": line0.item.name if line0 and line0.item else None,
                "lotNo": line0.batch_number if line0 and line0.batch_number else None,
                "qty": rec_q,
                "status": r.qc_status,
                "daysPending": max(0, days),
                "agingBucket": aging,
                "remarks": _qc_remarks(r),
            })

        for inw in inwards_qs.filter(status__in=["Received", "Pending QC", "Pending", "On Hold"]):
            jwo = inw.job_work_order
            plan = jwo.process_plan if jwo else None
            v_name = ((jwo.vendor_name or (jwo.vendor.name if jwo.vendor else None)) if jwo else None)
            rec_q = float(inw.qty or 0)
            days = (today - inw.date).days if inw.date else 0
            aging = "0-3 Days" if days <= 3 else ("4-7 Days" if days <= 7 else ("8-14 Days" if days <= 14 else "15+ Days"))
            hold_pending_report.append({
                "refNo": inw.no,
                "sourceType": "Job Work",
                "date": inw.date.strftime("%d-%m-%Y") if inw.date else "",
                "supplier": v_name,
                "itemCode": (getattr(plan, "fabric_item", None) or None),
                "itemName": (getattr(plan, "fabric_quality", "") or None),
                "lotNo": (getattr(plan, "grey_lot_no", None) or None),
                "qty": rec_q,
                "status": "Pending" if inw.status in ["Received", "Pending QC", "Pending"] else inw.status,
                "daysPending": max(0, days),
                "agingBucket": aging,
                "remarks": f"Challan {inw.challan_no}" if inw.challan_no else None,
            })
        hold_pending_report.sort(key=lambda x: x["daysPending"], reverse=True)

        # Report 4: Reprocess History & Recovery
        reprocess_recovery_report = []
        for rp in reprocess_qs:
            jwo = rp.job_work_order
            v_name = ((jwo.vendor_name or (jwo.vendor.name if jwo.vendor else None)) if jwo else None)
            plan = jwo.process_plan if jwo else None
            sent_q = float(rp.qty or 0)
            # Only reinspected reprocess orders have a recorded result; until
            # then received/loss are unknown (None), never estimated.
            rp_meta = {}
            if rp.remarks and rp.remarks.strip().startswith("{"):
                try:
                    rp_meta = json.loads(rp.remarks)
                except ValueError:
                    rp_meta = {}
            if rp.status == "Completed" and rp_meta:
                rec_q = float(rp_meta.get("goodQty", 0) or 0) + float(rp_meta.get("holdQty", 0) or 0) + float(rp_meta.get("reprocessQty", 0) or 0)
                loss_q = float(rp_meta.get("rejectedQty", 0) or 0)
                rec_pct = round((rec_q / sent_q) * 100, 1) if sent_q else None
            else:
                rec_q = None
                loss_q = None
                rec_pct = None
            reprocess_recovery_report.append({
                "rpNo": rp.no,
                "sourceRef": rp.inward_no or (jwo.jwo_no if jwo else "—"),
                "vendor": v_name,
                "fabricItem": (getattr(plan, "fabric_quality", "") or None),
                "sentQty": sent_q,
                "receivedQty": rec_q,
                "recoveryPct": rec_pct,
                "lossQty": loss_q,
                "status": rp.status,
                "goodQty": float(rp_meta.get("goodQty", 0) or 0) if rp_meta else None,
                "rejectedQty": loss_q,
                "reason": rp.reason or None,
            })

        # Report 5: Inspector Performance
        inspector_map = {}
        for r in receipts_qs.exclude(qc_status="Pending"):
            insp_name = r.qc_by.name if r.qc_by else "Not recorded"
            if insp_name not in inspector_map:
                inspector_map[insp_name] = {
                    "inspector": insp_name, "inspectionsCount": 0, "inspectedQty": 0.0,
                    "approvedCount": 0, "holdCount": 0, "rejectedCount": 0,
                }
            r_lines = list(r.lines.all())
            rec_q = sum(float(l.weighed_qty or l.received_qty or 0) for l in r_lines)
            inspector_map[insp_name]["inspectionsCount"] += 1
            inspector_map[insp_name]["inspectedQty"] += rec_q
            if r.qc_status in ("Approved", "Partially Approved"):
                inspector_map[insp_name]["approvedCount"] += 1
            elif r.qc_status == "On Hold":
                inspector_map[insp_name]["holdCount"] += 1
            elif r.qc_status == "Rejected":
                inspector_map[insp_name]["rejectedCount"] += 1

        for inw in inwards_qs.exclude(status__in=["Pending", "Pending QC", "Received"]):
            insp_name = inw.updated_by.name if inw.updated_by_id else "Not recorded"
            if insp_name not in inspector_map:
                inspector_map[insp_name] = {
                    "inspector": insp_name, "inspectionsCount": 0, "inspectedQty": 0.0,
                    "approvedCount": 0, "holdCount": 0, "rejectedCount": 0,
                }
            rec_q = float(inw.qty or 0)
            inspector_map[insp_name]["inspectionsCount"] += 1
            inspector_map[insp_name]["inspectedQty"] += rec_q
            if inw.status in ["Approved", "Completed"]:
                inspector_map[insp_name]["approvedCount"] += 1
            elif inw.status == "On Hold":
                inspector_map[insp_name]["holdCount"] += 1
            elif inw.status == "Rejected":
                inspector_map[insp_name]["rejectedCount"] += 1

        inspector_performance_report = []
        for insp in inspector_map.values():
            cnt = insp["inspectionsCount"] or 1
            inspector_performance_report.append({
                **insp,
                "passPct": round((insp["approvedCount"] / cnt) * 100, 1),
            })

        # Report 6: Date-wise QC Summary
        date_map = {}
        for r in receipts_qs:
            d_str = r.receipt_date.strftime("%d-%m-%Y") if r.receipt_date else ""
            if not d_str:
                continue
            if d_str not in date_map:
                date_map[d_str] = {
                    "date": d_str, "receiptsCount": 0, "receivedQty": 0.0,
                    "inspectedQty": 0.0, "goodQty": 0.0, "holdQty": 0.0,
                    "reprocessQty": 0.0, "rejectedQty": 0.0,
                }
            r_lines = list(r.lines.all())
            rec_q = sum(float(l.weighed_qty or l.received_qty or 0) for l in r_lines)
            rej_q = sum(float(l.rejected_qty or 0) for l in r_lines)
            date_map[d_str]["receiptsCount"] += 1
            date_map[d_str]["receivedQty"] += rec_q
            if r.qc_status != "Pending":
                date_map[d_str]["inspectedQty"] += rec_q
            if r.qc_status in ("Approved", "Partially Approved"):
                date_map[d_str]["goodQty"] += max(0.0, rec_q - rej_q)
            elif r.qc_status == "On Hold":
                date_map[d_str]["holdQty"] += max(0.0, rec_q - rej_q)
            date_map[d_str]["rejectedQty"] += rej_q

        for inw in inwards_qs:
            d_str = inw.date.strftime("%d-%m-%Y") if inw.date else ""
            if not d_str:
                continue
            if d_str not in date_map:
                date_map[d_str] = {
                    "date": d_str, "receiptsCount": 0, "receivedQty": 0.0,
                    "inspectedQty": 0.0, "goodQty": 0.0, "holdQty": 0.0,
                    "reprocessQty": 0.0, "rejectedQty": 0.0,
                }
            rec_q = float(inw.qty or 0)
            date_map[d_str]["receiptsCount"] += 1
            date_map[d_str]["receivedQty"] += rec_q
            if inw.status not in ["Pending", "Pending QC", "Received"]:
                date_map[d_str]["inspectedQty"] += rec_q
            if inw.status in ["Approved", "Completed"]:
                date_map[d_str]["goodQty"] += float(inw.accepted)
            elif inw.status == "On Hold":
                date_map[d_str]["holdQty"] += rec_q
            date_map[d_str]["rejectedQty"] += float(inw.rejected)

        date_wise_summary_report = []
        for d in date_map.values():
            insp = d["inspectedQty"] or d["receivedQty"] or 1.0
            date_wise_summary_report.append({
                **d,
                "passPct": round((d["goodQty"] / insp) * 100, 1),
                "rejectionPct": round((d["rejectedQty"] / insp) * 100, 1),
            })

        done_rp = [r for r in reprocess_recovery_report if r["receivedQty"] is not None]

        # Summary KPIs across all audited records
        total_rcv = sum(s["receivedQty"] for s in supplier_rejection_report)
        total_insp = sum(s["inspectedQty"] for s in supplier_rejection_report)
        total_gd = sum(s["goodQty"] for s in supplier_rejection_report)
        total_rej = sum(s["rejectedQty"] for s in supplier_rejection_report)
        total_hld = sum(s["holdQty"] for s in supplier_rejection_report)

        summary_kpis = {
            "totalReceipts": sum(s["receiptsCount"] for s in supplier_rejection_report),
            "totalReceived": round(total_rcv, 2),
            "totalInspected": round(total_insp, 2),
            "passRate": round((total_gd / (total_insp or 1.0)) * 100, 1) if total_insp else 0.0,
            "rejectionRate": round((total_rej / (total_insp or 1.0)) * 100, 1) if total_insp else 0.0,
            "pendingHoldQty": round(total_hld + sum(h["qty"] for h in hold_pending_report if h["status"] == "Pending"), 2),
            "reprocessRecovery": round(
                (sum(r["receivedQty"] for r in done_rp) / (sum(r["sentQty"] for r in done_rp) or 1.0)) * 100, 1
            ) if done_rp else None,
        }

        # Filter dropdown options
        suppliers_list = list(
            Party.objects.filter(client_id=client_id, type__in=["Vendor", "Both"], deleted_at__isnull=True)
            .values_list("name", flat=True)
            .distinct()
        )
        jwo_vendors_list = list(
            JobWorkOrder.objects.filter(client_id=client_id, vendor_name__isnull=False)
            .values_list("vendor_name", flat=True)
            .distinct()
        )
        all_suppliers = sorted(list(set([s for s in suppliers_list + jwo_vendors_list if s])))
        qualities_list = list(
            FabricQuality.objects.filter(client_id=client_id, deleted_at__isnull=True)
            .values_list("name", flat=True)
            .distinct()
            .order_by("name")
        )

        return Response({
            "summaryKPIs": summary_kpis,
            "reports": {
                "supplierRejection": supplier_rejection_report,
                "fabricDefects": fabric_defects_report,
                "holdPending": hold_pending_report,
                "reprocessRecovery": reprocess_recovery_report,
                "inspectorPerformance": inspector_performance_report,
                "dateWiseSummary": date_wise_summary_report,
            },
            "filters": {
                "suppliers": all_suppliers,
                "qualities": qualities_list,
                "dateRange": {
                    "from": start_date.isoformat(),
                    "to": end_date.isoformat(),
                },
            },
        })


class QCReinspectionView(APIView):
    """
    GET  /inventory/qc/reinspection/  -- reprocess orders and their reinspection
    POST /inventory/qc/reinspection/  -- create (explicit inputs only) or record
                                         the reinspection disposition

    A reprocess order raised by a GRN QC disposition is linked to the goods
    sitting at the Reprocess location (by GRN); its reinspection moves them on
    with real movements. Orders with no such stock record the result only, and
    the response says so.
    """

    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]
    permission_map = {"POST": ["approve_qc"]}

    @staticmethod
    def _meta(reprocess):
        if reprocess.remarks and reprocess.remarks.strip().startswith("{"):
            try:
                return json.loads(reprocess.remarks)
            except ValueError:
                return {}
        return {}

    @staticmethod
    def _reprocess_units(reprocess):
        """Takas / item quantities of this order's GRN still at Reprocess."""
        receipt = GoodsReceipt.objects.filter(
            client_id=reprocess.client_id, grn_number=reprocess.inward_no, deleted_at__isnull=True
        ).first() if reprocess.inward_no else None
        if receipt is None:
            return None, []
        location = stock.system_location(reprocess.client_id, "REPROCESS")
        units = [
            {"lot": lot, "item_id": lot.item_id, "qty": round4(lot.available_quantity), "line": None,
             "from": location.id}
            for lot in LotTaka.objects.filter(
                client_id=reprocess.client_id, source_document_id=receipt.id,
                location_id=location.id, available_quantity__gt=0, deleted_at__isnull=True,
            ).order_by("created_at")
        ]
        lot_items = {u["item_id"] for u in units}
        for row in StockMovement.objects.filter(
            client_id=reprocess.client_id, source_document_id=receipt.id,
            location_id=location.id, lot_taka__isnull=True,
        ).values("item_id").annotate(q=Sum("quantity")):
            if row["item_id"] not in lot_items and D(row["q"] or 0) > ZERO:
                units.append({"lot": None, "item_id": row["item_id"], "qty": round4(row["q"]),
                              "line": None, "from": location.id})
        return receipt, units

    def get(self, request):
        client_id = request.client_id
        tab = request.query_params.get("tab") or "Pending Reprocess"
        from_date = request.query_params.get("from_date")
        to_date = request.query_params.get("to_date")
        vendor = request.query_params.get("vendor") or "All"
        status_filter = request.query_params.get("status") or "All"

        reprocess_qs = (
            JWOReprocess.objects.filter(client_id=client_id)
            .select_related(
                "job_work_order", "job_work_order__vendor", "job_work_order__process_plan",
                "created_by", "updated_by",
            )
            .order_by("-date", "-created_at")
        ).filter(_reprocess_stage_q(client_id, _qc_stage(request)))
        if from_date:
            reprocess_qs = reprocess_qs.filter(date__gte=from_date)
        if to_date:
            reprocess_qs = reprocess_qs.filter(date__lte=to_date)
        if vendor != "All":
            reprocess_qs = reprocess_qs.filter(
                Q(job_work_order__vendor_name=vendor) | Q(job_work_order__vendor__name=vendor)
            )

        records = []
        for r in reprocess_qs:
            jwo = r.job_work_order
            plan = jwo.process_plan if jwo else None
            if tab == "Pending Reprocess" and r.status in ["Completed", "Cancelled"]:
                continue
            elif tab == "Sent for Reprocess" and r.status not in ["Sent to Vendor", "In Process", "Pending"]:
                continue
            elif tab == "Received from Reprocess" and r.status not in ["Received", "Completed"]:
                continue

            re_status = "Pending"
            if r.status == "Completed":
                re_status = "Completed"
            elif r.status in ["In Process", "Received"]:
                re_status = "In Progress"
            if status_filter != "All" and re_status != status_filter:
                continue

            meta = self._meta(r)
            sent_q = float(r.qty or 0)
            result = meta if r.status == "Completed" else (meta.get("draft") or {})
            created_at = timezone.localtime(r.created_at) if r.created_at else None
            audit = [{
                "dateTime": created_at.strftime("%d-%m-%Y %H:%M") if created_at else "",
                "action": "Created",
                "user": r.created_by.name if r.created_by_id else None,
                "remarks": r.reason or None,
            }]
            if r.status != "Pending" and r.updated_at:
                audit.append({
                    "dateTime": timezone.localtime(r.updated_at).strftime("%d-%m-%Y %H:%M"),
                    "action": re_status,
                    "user": r.updated_by.name if r.updated_by_id else None,
                    "remarks": meta.get("remarks") or None,
                })
            records.append({
                "id": str(r.id),
                "reprocessNo": r.no,
                "status": r.status,
                "jwoId": str(jwo.id) if jwo else None,
                "jwoNo": (jwo.jwo_no or None) if jwo else None,
                "reason": r.reason or None,
                "date": r.date.strftime("%d-%m-%Y") if r.date else "",
                "rawDate": r.date.isoformat() if r.date else "",
                "sourceQcNo": r.inward_no or (jwo.jwo_no if jwo else None),
                "itemCode": (plan.fabric_item or None) if plan else None,
                "fabricItem": (plan.fabric_item or plan.fabric_quality or None) if plan else None,
                "sentQty": sent_q,
                "receivedQty": float(sum(float(result.get(k, 0) or 0) for k in ("goodQty", "holdQty", "reprocessQty", "rejectedQty"))) if r.status == "Completed" else None,
                "pendingQty": 0.0 if r.status == "Completed" else sent_q,
                "vendor": ((jwo.vendor_name or (jwo.vendor.name if jwo.vendor else None)) if jwo else None),
                "reinspectionStatus": re_status,
                "lotNo": (plan.grey_lot_no or None) if plan else None,
                "goodQty": float(result.get("goodQty", 0) or 0),
                "holdQty": float(result.get("holdQty", 0) or 0),
                "reprocessQty": float(result.get("reprocessQty", 0) or 0),
                "rejectedQty": float(result.get("rejectedQty", 0) or 0),
                "remarks": result.get("remarks") or (r.remarks if not meta else None),
                "measurements": result.get("measurements") or [],
                "auditTrail": audit,
            })

        vendors = list(
            Party.objects.filter(client_id=client_id, type__in=["Vendor", "Both"], deleted_at__isnull=True)
            .values_list("name", flat=True).distinct().order_by("name")
        )
        items = list(
            Item.objects.filter(client_id=client_id, deleted_at__isnull=True)
            .values_list("sku", flat=True).distinct().order_by("sku")
        )
        return Response({
            "records": records,
            "filters": {
                "vendors": vendors,
                "statuses": ["All", "Pending", "In Progress", "Completed"],
                "items": items,
            },
        })

    def post(self, request):
        from .. import qc as qc_service

        client_id = request.client_id
        action_type = request.data.get("actionType") or "create"

        if action_type == "disposition":
            reprocess_id = request.data.get("reprocessId")
            reprocess = JWOReprocess.objects.filter(client_id=client_id, id=reprocess_id).first() if reprocess_id else None
            if reprocess is None:
                raise ValidationFailed("Reprocess record not found.", field_errors={"reprocessId": ["Not found."]})
            if reprocess.status == "Completed":
                raise Conflict(f"Reprocess {reprocess.no} is already reinspected.", code=Codes.ALREADY_DONE)

            is_draft = bool(request.data.get("isDraft", False))
            quantities = qc_service.parse_quantities(request.data)
            remarks = request.data.get("remarks") or ""
            measurements = request.data.get("measurements") or []
            result = {
                "goodQty": float(quantities["good"]),
                "secondQualityQty": float(quantities["second"]),
                "holdQty": float(quantities["hold"]),
                "reprocessQty": float(quantities["reprocess"]),
                "rejectedQty": float(quantities["reject"]),
                "remarks": remarks,
                "measurements": measurements,
                "updatedAt": timezone.now().isoformat(),
            }

            with transaction.atomic():
                meta = self._meta(reprocess)
                if is_draft:
                    meta["draft"] = result
                    reprocess.remarks = json.dumps(meta)
                    reprocess.save(update_fields=["remarks", "updated_at"])
                    return Response({
                        "success": True, "status": reprocess.status,
                        "message": "Reinspection draft saved. Stock is not moved until approval.",
                    })

                total = sum(quantities.values(), ZERO)
                if total <= ZERO:
                    raise ValidationFailed(
                        "Enter the reinspection quantities.",
                        field_errors={"goodQty": ["Total must be greater than zero."]},
                    )
                receipt, units = self._reprocess_units(reprocess)
                pool = round4(sum((u["qty"] for u in units), ZERO))
                moved = 0
                if units:
                    if abs(total - pool) > Decimal("0.001"):
                        raise ValidationFailed(
                            f"Reinspection total ({total}) must equal the quantity at the "
                            f"Reprocess location for {reprocess.inward_no} ({pool}).",
                            field_errors={"goodQty": [f"Expected a total of {pool}."]},
                        )
                    good_location_id = receipt.location_id
                    for unit, category, qty in qc_service._allocate(units, quantities):
                        if category == "reprocess":
                            continue  # stays at Reprocess for another round
                        condition = qc_service.CATEGORY_CONDITION[category]
                        _, lot_qc = stock.CONDITION_ROUTING[condition]
                        destination = stock.condition_location_id(client_id, condition, good_location_id)
                        move_type = qc_service.CATEGORY_MOVEMENT[category]
                        common = dict(
                            client_id=client_id, reference_type="JWOReprocess",
                            reference_id=reprocess.id, reference_number=reprocess.no,
                            source_document_type="GoodsReceipt", source_document_id=receipt.id,
                            notes=f"Reinspection {reprocess.no}" + (f": {remarks}" if remarks else ""),
                            user=request.user,
                        )
                        if unit["lot"] is not None:
                            _, movements = stock.relocate_lot(
                                lot=unit["lot"], quantity=qty, to_location_id=destination,
                                out_type=move_type, in_type=move_type, qc_status=lot_qc,
                                split_suffix="RI", **common,
                            )
                        else:
                            movements = stock.relocate_item_qty(
                                item=unit["item_id"], quantity=qty, from_location_id=unit["from"],
                                to_location_id=destination, out_type=move_type, in_type=move_type,
                                **common,
                            )
                        moved += len(movements)

                # "Repeat": the failed quantity goes round again as a new
                # reprocess order on the same job work order and source.
                repeat = None
                if quantities["reprocess"] > ZERO:
                    on_date = timezone.localdate()
                    repeat = JWOReprocess.objects.create(
                        client_id=client_id,
                        job_work_order=reprocess.job_work_order,
                        no=allocate_number(request.user.client, "RP", on_date),
                        inward_no=reprocess.inward_no,
                        date=on_date,
                        qty=quantities["reprocess"],
                        reason=remarks or f"Repeat reprocess after reinspection of {reprocess.no}",
                        status="Pending",
                        created_by=request.user,
                    )
                    result["repeatNo"] = repeat.no

                result["stockPosted"] = bool(units)
                reprocess.status = "Completed"
                reprocess.remarks = json.dumps(result)
                reprocess.updated_by = request.user
                reprocess.save(update_fields=["status", "remarks", "updated_by", "updated_at"])

            message = (
                f"Reinspection of {reprocess.no} approved; {moved} stock movement(s) posted."
                if units else
                f"Reinspection of {reprocess.no} recorded. No stock of this order is held at the "
                "Reprocess location, so no stock moved."
            )
            if repeat is not None:
                message += f" Repeat reprocess {repeat.no} created for {quantities['reprocess']} m."
            return Response({"success": True, "status": reprocess.status, "stockPosted": bool(units), "message": message})

        # Create a reprocess / reinspection entry -- explicit inputs only.
        jwo_id = request.data.get("jwo_id") or request.data.get("jwoId") or request.data.get("jobWorkOrderId")
        challan_no = request.data.get("challan_no") or request.data.get("challanNo")
        meters_raw = request.data.get("meters") or request.data.get("sentQty") or request.data.get("qty")
        reason = (request.data.get("reason") or "").strip()

        field_errors = {}
        if not jwo_id:
            field_errors["jwo_id"] = ["Job Work Order is required."]
        if not challan_no:
            field_errors["challan_no"] = ["Challan number is required."]
        try:
            sent_qty = round4(D(meters_raw))
        except (InvalidOperation, TypeError, ValueError) as exc:
            logger.exception("Failed parsing reinspection sent quantity: %s", exc)
            sent_qty = ZERO
        if sent_qty <= ZERO:
            field_errors["meters"] = ["Meters must be greater than zero."]
        try:
            r_date = date.fromisoformat(str(request.data.get("date") or ""))
        except ValueError:
            r_date = None
        if r_date is None:
            field_errors["date"] = ["Enter the date (YYYY-MM-DD)."]
        if field_errors:
            raise ValidationFailed("Missing required reinspection fields.", field_errors=field_errors)

        jwo = JobWorkOrder.objects.filter(client_id=client_id, id=jwo_id).first() if _is_uuid(jwo_id) else None
        if jwo is None:
            jwo = JobWorkOrder.objects.filter(client_id=client_id, jwo_no=jwo_id).first()
        if jwo is None:
            raise ValidationFailed(
                f"Job Work Order '{jwo_id}' does not exist.",
                field_errors={"jwo_id": ["Invalid Job Work Order."]},
            )

        with transaction.atomic():
            rp_no = allocate_number(request.user.client, "RP", r_date)
            reprocess = JWOReprocess.objects.create(
                client_id=client_id,
                job_work_order=jwo,
                no=rp_no,
                date=r_date,
                qty=sent_qty,
                reason=f"{reason} (Challan: {challan_no})" if reason else f"Challan: {challan_no}",
                status="Pending",
                created_by=request.user,
            )
        return Response({
            "success": True,
            "message": f"Reinspection record {rp_no} created.",
            "reprocessId": str(reprocess.id),
            "reprocessNo": rp_no,
        }, status=status.HTTP_201_CREATED)


class QCInspectionViewSet(TenantModelViewSet):
    """Piece-by-piece fabric inspection register (PDF §08, Phase 2.6).

    Calculates 4-point penalty score per roll, assigns grade (A, B, C, Second Quality),
    logs defects against DefectMaster, and posts real stock movements upon disposition.
    """
    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]
    permission_map = {
        "write": ["manage_inventory"],
        "approve": ["approve_qc"],
        "dispose": ["approve_qc"],
    }
    queryset = QCInspection.objects.prefetch_related(
        "lines__defects__defect", "lines__lot_taka"
    )
    serializer_class = QCInspectionSerializer
    audit_entity_type = "QCInspection"
    audit_label_field = "inspection_number"
    status_field = "status"
    default_date_field = "inspection_date"
    search_fields = ["inspection_number", "source_document_number"]
    ordering = ["-inspection_date", "-created_at"]

    def filter_queryset(self, queryset):
        """``?stage=grey`` = grey inward inspections, ``?stage=dyeing`` = job
        work inward (finished / dyed) inspections."""
        queryset = super().filter_queryset(queryset)
        stage = _qc_stage(self.request)
        if stage == "dyeing":
            queryset = queryset.filter(source_document_type="JWO_INWARD")
        elif stage == "grey":
            queryset = queryset.filter(source_document_type__in=["GRN", "PO", "INTERNAL"])
        return queryset

    @transaction.atomic
    def perform_create(self, serializer):
        lines_data = serializer.validated_data.pop("lines", [])
        serializer.validated_data["inspection_number"] = allocate_number(
            self.request.user.client, "QC", serializer.validated_data.get("inspection_date")
        )
        if not serializer.validated_data.get("inspector") and not serializer.validated_data.get("inspector_employee"):
            serializer.validated_data["inspector"] = self.request.user

        inspection = super().perform_create(serializer)
        self._save_lines(inspection, lines_data)
        self._recompute_totals(inspection)
        return inspection

    @transaction.atomic
    def perform_update(self, serializer):
        lines_data = serializer.validated_data.pop("lines", None)
        inspection = super().perform_update(serializer)
        if lines_data is not None:
            for line in inspection.lines.all():
                line.defects.all().delete()
                line.delete()
            self._save_lines(inspection, lines_data)
            self._recompute_totals(inspection)
        return inspection

    def _save_lines(self, inspection, lines_data):
        for line_d in lines_data:
            line_dict = dict(line_d)
            defects_data = line_dict.pop("defects", [])
            lot_taka = line_dict.get("lot_taka")

            actual_meter = D(line_dict.get("actual_meter") or (lot_taka.available_quantity if lot_taka else 0) or 0)
            line_dict["actual_meter"] = actual_meter

            width = D(line_dict.get("width") or (lot_taka.width if lot_taka else 0) or 44)
            line_dict["width"] = width

            total_points = sum(int(d.get("points") or 1) * int(d.get("defect_count") or 1) for d in defects_data)
            line_dict["penalty_points"] = total_points

            width_meters = width * Decimal("0.0254")
            area = actual_meter * width_meters
            if area > ZERO:
                pts_100m = round2((Decimal(total_points) * Decimal("100")) / area)
            else:
                pts_100m = Decimal("0.00")
            line_dict["points_per_100_sq_meter"] = pts_100m

            if not line_dict.get("grade"):
                if pts_100m <= Decimal("20.0"):
                    line_dict["grade"] = "A"
                elif pts_100m <= Decimal("28.0"):
                    line_dict["grade"] = "B"
                elif pts_100m <= Decimal("40.0"):
                    line_dict["grade"] = "C"
                else:
                    line_dict["grade"] = "Second Quality"

            if not line_dict.get("disposition") or line_dict.get("disposition") == "Pass":
                if line_dict["grade"] in ("A", "B"):
                    line_dict["disposition"] = "Pass"
                elif line_dict["grade"] == "C":
                    line_dict["disposition"] = "Second Quality"
                else:
                    line_dict["disposition"] = "Reject"

            line_obj = QCInspectionLine.objects.create(
                client_id=inspection.client_id,
                inspection=inspection,
                **line_dict,
            )

            for def_d in defects_data:
                QCInspectionLineDefect.objects.create(
                    client_id=inspection.client_id,
                    inspection_line=line_obj,
                    **def_d,
                )

    def _recompute_totals(self, inspection):
        lines = list(inspection.lines.all())
        inspection.total_inspected_qty = sum((l.actual_meter or ZERO for l in lines), ZERO)
        inspection.passed_qty = sum((l.actual_meter or ZERO for l in lines if l.disposition == "Pass"), ZERO)
        inspection.second_quality_qty = sum((l.actual_meter or ZERO for l in lines if l.disposition == "Second Quality"), ZERO)
        inspection.reprocess_qty = sum((l.actual_meter or ZERO for l in lines if l.disposition == "Reprocess"), ZERO)
        inspection.rejected_qty = sum((l.actual_meter or ZERO for l in lines if l.disposition == "Reject"), ZERO)
        inspection.hold_qty = sum((l.actual_meter or ZERO for l in lines if l.disposition == "Hold"), ZERO)
        inspection.save(update_fields=[
            "total_inspected_qty", "passed_qty", "second_quality_qty",
            "reprocess_qty", "rejected_qty", "hold_qty", "updated_at",
        ])

    @action(detail=True, methods=["post"], url_path="approve")
    @transaction.atomic
    def approve(self, request, pk=None):
        inspection = self.get_object()
        if inspection.status in ("Approved", "Completed"):
            raise Conflict("Inspection is already approved.", code="ALREADY_APPROVED")

        lines = list(inspection.lines.select_related("lot_taka", "lot_taka__location", "lot_taka__item"))
        # Passed fabric goes to the godown the inspector picks, else the
        # first real godown -- never a hard-coded code.
        good_location_id = request.data.get("locationId") or Location.objects.filter(
            client_id=inspection.client_id, is_active=True, deleted_at__isnull=True,
            type__in=["Warehouse", "Godown"],
        ).exclude(code__startswith="SYS-").order_by("created_at").values_list("id", flat=True).first()
        # Line disposition -> (stock condition, movement type, taka status).
        routing = {
            "Pass": ("Good", "QC_PASS", "Available"),
            "Second Quality": ("Second Quality", "QC_SECOND_QUALITY", "Second Quality"),
            "Reprocess": ("Reprocess", "QC_REPROCESS", "Reprocess"),
            "Reject": ("Reject", "QC_REJECT", "Reject"),
        }

        for line in lines:
            lt = line.lot_taka
            if not lt:
                continue
            m = min(D(line.actual_meter or lt.available_quantity or ZERO), D(lt.available_quantity or ZERO))
            if m <= ZERO:
                continue

            if line.disposition not in routing:  # Hold: stays where it is
                lt.status = "QC Hold"
                lt.qc_status = "On Hold"
                lt.save(update_fields=["status", "qc_status", "updated_at"])
                continue
            condition, mov_type, status_val = routing[line.disposition]
            if line.disposition == "Pass" and not good_location_id:
                raise ValidationFailed(
                    "Choose the godown passed fabric goes to.", field_errors={"locationId": ["Required."]}
                )
            target_id = stock.condition_location_id(inspection.client_id, condition, good_location_id)
            holder = lt
            if str(lt.location_id) != str(target_id):
                holder, _ = stock.relocate_lot(
                    client_id=inspection.client_id,
                    lot=lt,
                    quantity=m,
                    to_location_id=target_id,
                    out_type=mov_type,
                    in_type=mov_type,
                    reference_type="QCInspection",
                    reference_id=inspection.id,
                    reference_number=inspection.inspection_number,
                    user=request.user,
                    qc_status=stock.CONDITION_ROUTING[condition][1],
                    split_suffix="QC",
                )
            holder.refresh_from_db()
            holder.status = status_val
            holder.qc_status = stock.CONDITION_ROUTING[condition][1]
            holder.save(update_fields=["status", "qc_status", "updated_at"])

        inspection.status = "Approved"
        inspection.inspector = request.user
        inspection.save(update_fields=["status", "inspector", "updated_at"])
        return Response(self.get_serializer(inspection).data)

