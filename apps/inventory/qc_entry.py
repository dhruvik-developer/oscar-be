"""
QC Entry -- roll-by-roll quality check (requirement PDF §08, §18).

Every roll / taka waiting at QC Hold belongs to a "lot to check": the
document it arrived on (a purchase receipt, a job-work inward, a production
run, a batch sent for QC). For each roll the checker records:

    measurements  actual meter, kg, width, GSM
    appearance    defects from the Defect Master (shade variation, hole ...)
    result        Pass, Second Quality, Hold, Reprocess, Reject
    evidence      remarks, checked by / date (the user and the day)

Saving keeps a draft QC inspection; approving moves each roll with a real
stock movement (Pass -> godown, Second -> second quality, Reprocess ->
reprocess stock, Reject -> reject, Hold stays at QC Hold). A roll measured
shorter than its book metres has the difference moved to Reject as
"short meter" and recorded as a normal loss, so stock and wastage agree.
The source document gets its QC verdict, and a job-work inward with rework
gets its reprocess order. Every QC stays in the history (repeat QC too).
"""
from collections import defaultdict

from django.db import transaction
from django.utils import timezone

from apps.core.audit import record_audit
from apps.core.exceptions import ValidationFailed
from apps.core.money import ZERO, D, round4
from apps.core.numbering import allocate_number

from . import services as stock

RESULTS = ("Pass", "Second Quality", "Hold", "Reprocess", "Reject")
#: Result -> (stock condition, movement type, roll status, qc category).
ROUTING = {
    "Pass": ("Good", "QC_PASS", "Available", "good"),
    "Second Quality": ("Second Quality", "QC_SECOND_QUALITY", "Second Quality", "second"),
    "Reprocess": ("Reprocess", "QC_REPROCESS", "Reprocess", "reprocess"),
    "Reject": ("Reject", "QC_REJECT", "Reject", "reject"),
}
#: Defect points by severity (4-point system style; editable per roll as a count).
SEVERITY_POINTS = {"Minor": 1, "Major": 2, "Critical": 4}
SOURCE_LABELS = {
    "GRN": "Purchase receipt",
    "JobWorkInward": "Back from job work",
    "Production": "In-house production",
    "ManualBatch": "Batch sent for QC",
    "CustomerMaterialReceipt": "Customer material",
}


def _key(taka):
    return f"{taka.source_document_type or 'Other'}|{taka.source_document_number or taka.source_document_id or ''}"


def _date(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


def _qc_rolls(client_id):
    from .models import LotTaka

    qc_hold = stock.system_location(client_id, "QC_HOLD")
    return (
        LotTaka.objects.filter(
            client_id=client_id, location=qc_hold, available_quantity__gt=0, deleted_at__isnull=True,
        )
        .select_related("item", "batch", "supplier", "fabric_quality", "design", "shade")
        .order_by("received_date", "lot_number", "taka_number")
    )


def _inspection_source(taka):
    """(QCInspection.source_document_type, document id, document number)."""
    if taka.source_document_type == "GRN":
        return "GRN", taka.source_document_id, taka.source_document_number
    if taka.source_document_type == "JobWorkInward":
        from apps.jobwork.models import JWOInward

        inward = JWOInward.objects.filter(
            client_id=taka.client_id, job_work_order_id=taka.source_document_id, no=taka.source_document_number,
        ).first()
        return "JWO_INWARD", inward.id if inward else None, taka.source_document_number
    return "INTERNAL", taka.source_document_id, taka.source_document_number


def _draft_for(client_id, key):
    from .models import QCInspection

    return QCInspection.objects.filter(
        client_id=client_id, status="Draft", deleted_at__isnull=True, notes__startswith=f"[{key}]",
    ).prefetch_related("lines__defects").order_by("-created_at").first()


def waiting_lots(client_id, *, stage=None):
    """Lots with rolls at QC Hold, oldest first, with how much is on hold."""
    groups = defaultdict(list)
    for taka in _qc_rolls(client_id):
        dyed = taka.source_document_type == "JobWorkInward"
        if stage == "grey" and dyed or stage == "dyeing" and not dyed:
            continue
        groups[_key(taka)].append(taka)
    from .models import QCInspection

    drafts = {
        (i.notes or "")[1:].split("]", 1)[0]
        for i in QCInspection.objects.filter(client_id=client_id, status="Draft", deleted_at__isnull=True)
        .only("notes")
    }
    lots = []
    for key, rolls in groups.items():
        first = rolls[0]
        meters = sum((D(t.available_quantity) for t in rolls), ZERO)
        on_hold = [t for t in rolls if t.qc_status == "On Hold"]
        lots.append({
            "key": key,
            "sourceType": first.source_document_type,
            "sourceLabel": SOURCE_LABELS.get(first.source_document_type, first.source_document_type or "Other"),
            "reference": first.source_document_number,
            "stage": "dyeing" if first.source_document_type == "JobWorkInward" else "grey",
            "fabric": ", ".join(sorted({t.item.name for t in rolls if t.item_id})),
            "lots": sorted({t.lot_number for t in rolls}),
            "batches": sorted({t.batch.batch_code for t in rolls if t.batch_id}),
            "supplier": first.supplier.name if first.supplier_id else None,
            "rolls": len(rolls),
            "meters": str(round4(meters)),
            "onHoldRolls": len(on_hold),
            "since": _date(min(t.received_date for t in rolls)),
            "hasDraft": key in drafts,
        })
    lots.sort(key=lambda lot: lot["since"] or "")
    return lots


def lot_detail(client_id, key):
    """The rolls of one lot to check, with the saved draft (if any) filled in."""
    rolls = [t for t in _qc_rolls(client_id) if _key(t) == key]
    if not rolls:
        raise ValidationFailed("Nothing of this lot is waiting for QC any more.", field_errors={"key": ["Not found."]})
    draft = _draft_for(client_id, key)
    saved = {}
    if draft is not None:
        for line in draft.lines.all():
            saved[str(line.lot_taka_id)] = {
                "actualMeter": str(line.actual_meter), "weightKg": str(line.roll_weight or "") or None,
                "width": str(line.width or "") or None, "gsm": str(line.gsm or "") or None,
                "result": line.disposition, "remarks": line.remarks,
                "defects": [{"defectId": str(d.defect_id), "count": d.defect_count} for d in line.defects.all()],
            }
    lot = next(lot for lot in waiting_lots(client_id) if lot["key"] == key)
    return {
        "lot": lot,
        "draftNumber": draft.inspection_number if draft is not None else None,
        "rolls": [{
            "lotTakaId": str(t.id),
            "takaId": t.taka_id,
            "takaNumber": t.taka_number,
            "lotNumber": t.lot_number,
            "batchCode": t.batch.batch_code if t.batch_id else None,
            "fabric": t.item.name if t.item_id else None,
            "shade": t.shade.name if t.shade_id else None,
            "bookMeter": str(round4(D(t.available_quantity))),
            "width": str(t.width) if t.width is not None else None,
            "gsm": str(t.gsm) if t.gsm is not None else None,
            "weightKg": str(t.roll_weight) if t.roll_weight is not None else None,
            "qcStatus": t.qc_status,
            "saved": saved.get(str(t.id)),
        } for t in rolls],
    }


def _num(value, field, *, positive=False):
    if value in (None, ""):
        return None
    try:
        number = round4(D(value))
    except Exception:  # noqa: BLE001 - reported as a field error
        raise ValidationFailed("Enter a number.", field_errors={field: ["Not a number."]})
    if number < ZERO or (positive and number <= ZERO):
        raise ValidationFailed("Measurements must be above zero.", field_errors={field: ["Must be above 0."]})
    return number


@transaction.atomic
def save_qc(*, client, key, rolls, approve=False, location_id=None, jwo_id=None, remarks=None, user=None):
    """Save (draft) or approve the roll-by-roll QC of one lot.

    ``rolls`` = [{lotTakaId, actualMeter?, weightKg?, width?, gsm?, result, remarks?,
    defects?: [{defectId, count?}]}] -- the rolls checked now; rolls left out
    stay waiting. Returns a summary for the screen.
    """
    from apps.masters.models import DefectMaster

    from .batch_services import record_loss
    from .models import LotTaka, QCInspection, QCInspectionLine, QCInspectionLineDefect

    client_id = getattr(client, "id", client)
    waiting = {str(t.id): t for t in _qc_rolls(client_id) if _key(t) == key}
    if not waiting:
        raise ValidationFailed("Nothing of this lot is waiting for QC any more.", field_errors={"key": ["Not found."]})
    entries = [r for r in (rolls or []) if r.get("lotTakaId")]
    if not entries:
        raise ValidationFailed("Check at least one roll.", field_errors={"rolls": ["Required."]})
    for r in entries:
        if str(r["lotTakaId"]) not in waiting:
            raise ValidationFailed("A roll is not part of this lot or is no longer waiting for QC.",
                                   field_errors={"rolls": [str(r["lotTakaId"])]})
        if r.get("result") not in RESULTS:
            raise ValidationFailed("Choose the result of every roll you checked.",
                                   field_errors={"rolls": [f"{waiting[str(r['lotTakaId'])].taka_id}: no result."]})
    defects = {str(d.id): d for d in DefectMaster.objects.filter(client_id=client_id, deleted_at__isnull=True)}

    first = waiting[str(entries[0]["lotTakaId"])]
    source_type, source_id, source_no = _inspection_source(first)
    inspection = _draft_for(client_id, key)
    if inspection is None:
        inspection = QCInspection.objects.create(
            client_id=client_id,
            inspection_number=allocate_number(client, "QC"),
            source_document_type=source_type,
            source_document_id=source_id,
            source_document_number=source_no,
            inspection_type="Dyeing QC" if source_type == "JWO_INWARD" else "Grey Fabric QC",
            inspection_method="Roll-by-roll",
            fabric_item_id=first.item_id,
            status="Draft",
            inspector=user if getattr(user, "is_authenticated", False) else None,
            notes=f"[{key}]",
        )
    else:
        for line in inspection.lines.all():
            line.defects.all().delete()
            line.delete()
    if remarks:
        inspection.notes = f"[{key}] {remarks}"

    plan = []
    for r in entries:
        taka = waiting[str(r["lotTakaId"])]
        book = round4(D(taka.available_quantity))
        actual = _num(r.get("actualMeter"), "actualMeter", positive=True)
        actual = book if actual is None else actual
        line = QCInspectionLine.objects.create(
            client_id=client_id, inspection=inspection, lot_taka=taka, taka_number=taka.taka_number,
            lot_number=taka.lot_number, original_meter=book, actual_meter=actual,
            roll_weight=_num(r.get("weightKg"), "weightKg"), width=_num(r.get("width"), "width"),
            gsm=_num(r.get("gsm"), "gsm"),
            grade={"Pass": "A", "Second Quality": "Second"}.get(r["result"], "C"),
            disposition=r["result"], remarks=(r.get("remarks") or "").strip() or None,
        )
        points = 0
        for d in r.get("defects") or []:
            master = defects.get(str(d.get("defectId")))
            if master is None:
                continue
            count = max(1, int(d.get("count") or 1))
            each = SEVERITY_POINTS.get(master.severity, 1)
            QCInspectionLineDefect.objects.create(
                client_id=client_id, inspection_line=line, defect=master, defect_count=count, points=each,
            )
            points += count * each
        line.penalty_points = points
        line.save(update_fields=["penalty_points", "updated_at"])
        plan.append((taka, line, book, actual))

    totals = defaultdict(lambda: ZERO)
    for _, line, book, actual in plan:
        totals["checked"] += min(actual, book)
        totals[line.disposition] += min(actual, book)
        totals["short"] += max(ZERO, book - actual)
    inspection.total_received_qty = sum((p[2] for p in plan), ZERO)
    inspection.total_inspected_qty = totals["checked"]
    inspection.passed_qty = totals["Pass"]
    inspection.second_quality_qty = totals["Second Quality"]
    inspection.reprocess_qty = totals["Reprocess"]
    inspection.rejected_qty = totals["Reject"] + totals["short"]
    inspection.hold_qty = totals["Hold"]
    inspection.inspection_date = timezone.localdate()
    inspection.inspector = user if getattr(user, "is_authenticated", False) else inspection.inspector
    inspection.save()

    result = {"inspectionNumber": inspection.inspection_number, "approved": False,
              "checkedRolls": len(plan), "waitingRolls": len(waiting) - len(plan)}
    if not approve:
        return result

    if totals["Pass"] > ZERO and not location_id:
        from apps.masters.models import Location

        location_id = Location.objects.filter(
            client_id=client_id, is_active=True, deleted_at__isnull=True, type__in=["Warehouse", "Godown"],
        ).exclude(code__startswith="SYS-").order_by("created_at").values_list("id", flat=True).first()
        if not location_id:
            raise ValidationFailed("Choose the godown passed fabric goes to.", field_errors={"locationId": ["Required."]})
    reference = dict(reference_type="QCInspection", reference_id=inspection.id,
                     reference_number=inspection.inspection_number, user=user)
    losses = defaultdict(lambda: ZERO)
    for taka, line, book, actual in plan:
        holder = taka
        short = max(ZERO, book - actual)
        if short > ZERO:
            # Measured shorter than the book metres: the difference is a loss.
            stock.relocate_lot(
                client_id=client_id, lot=taka, quantity=short,
                to_location_id=stock.system_location(client_id, "REJECT").id,
                out_type="QC_REJECT", in_type="QC_REJECT", qc_status="Rejected", split_suffix="SM",
                notes=f"Short meter at QC {inspection.inspection_number}", **reference,
            )
            if taka.batch_id:
                losses[taka.batch_id] += short
            holder = LotTaka.objects.get(pk=taka.pk)
        if line.disposition == "Hold":
            LotTaka.objects.filter(pk=holder.pk).update(
                status="QC Hold", qc_status="On Hold", qc_notes=line.remarks, updated_at=timezone.now())
            continue
        condition, movement, status_value, _ = ROUTING[line.disposition]
        target = stock.condition_location_id(client_id, condition, location_id)
        holder, _ = stock.relocate_lot(
            client_id=client_id, lot=holder, quantity=min(actual, book), to_location_id=target,
            out_type=movement, in_type=movement, qc_status=stock.CONDITION_ROUTING[condition][1],
            split_suffix="QC", notes=f"QC {inspection.inspection_number}"
            + (f": {line.remarks}" if line.remarks else ""), **reference,
        )
        LotTaka.objects.filter(pk=holder.pk).update(status=status_value, qc_notes=line.remarks, updated_at=timezone.now())

    from .models import Batch

    for batch_id, qty in losses.items():
        record_loss(Batch.objects.get(pk=batch_id), quantity=qty, cause="NORMAL_LOSS", process="QC",
                    outcome="SCRAP", reference=inspection.inspection_number,
                    notes="Short meter found at QC", user=user)

    quantities = {
        "good": totals["Pass"], "second": totals["Second Quality"], "reprocess": totals["Reprocess"],
        "reject": totals["Reject"] + totals["short"], "hold": totals["Hold"],
    }
    reprocess_no = _update_source(first, source_type, source_id, quantities, jwo_id=jwo_id,
                                  remarks=remarks, user=user)
    from . import qc as qc_service

    verdict = qc_service.verdict_for({k: v for k, v in quantities.items() if k != "hold"}, quantities["hold"])
    inspection.status = verdict
    inspection.save(update_fields=["status", "updated_at"])
    record_audit(
        client=client_id, actor=user, action="qc_entry", entity_type="QCInspection", entity_id=inspection.id,
        entity_label=inspection.inspection_number, description=f"Roll QC approved: {verdict}",
        after={k: str(v) for k, v in quantities.items()}, comments=remarks or None,
    )
    result.update({
        "approved": True, "status": inspection.status, "reprocessNo": reprocess_no,
        "quantities": {k: str(round4(v)) for k, v in quantities.items()},
        "shortMeter": str(round4(totals["short"])),
    })
    return result


def _update_source(first, source_type, source_id, quantities, *, jwo_id=None, remarks=None, user=None):
    """Give the purchase receipt / job-work inward its QC verdict once its
    rolls are checked; open a reprocess order where there is a processor."""
    from apps.jobwork.models import JobWorkOrder, JWOInward, JWOReprocess
    from apps.purchase.models import GoodsReceipt

    from . import qc as qc_service

    if source_type == "GRN" and source_id:
        receipt = GoodsReceipt.objects.select_for_update().filter(pk=source_id).first()
        if receipt is None:
            return None
        order = None
        if quantities["reprocess"] > ZERO and jwo_id:
            jwo = JobWorkOrder.objects.filter(client_id=receipt.client_id, pk=jwo_id).first()
            if jwo is None:
                raise ValidationFailed("Job work order not found.", field_errors={"jwoId": ["Not found."]})
            order = qc_service._create_reprocess_order(receipt, jwo, quantities["reprocess"], remarks, user)
        qc_service.record_receipt_verdict(receipt, quantities, user=user, remarks=remarks, reprocess_order=order)
        return order.no if order else None
    if source_type == "JWO_INWARD" and source_id:
        inward = JWOInward.objects.select_for_update().filter(pk=source_id).first()
        if inward is None:
            return None
        inward.accepted = D(inward.accepted or 0) + quantities["good"] + quantities["second"]
        inward.rejected = D(inward.rejected or 0) + quantities["reject"]
        inward.status = qc_service.verdict_for(
            {k: v for k, v in quantities.items() if k != "hold"}, quantities["hold"])
        inward.save(update_fields=["accepted", "rejected", "status", "updated_at"])
        if quantities["reprocess"] > ZERO:
            rp = JWOReprocess.objects.create(
                client_id=inward.client_id, job_work_order=inward.job_work_order,
                no=allocate_number(inward.client_id, "RP"), inward_no=inward.no, date=timezone.localdate(),
                qty=quantities["reprocess"], reason=remarks or f"Reprocess required from {inward.no}",
                status="Pending", created_by=user if getattr(user, "is_authenticated", False) else None,
            )
            return rp.no
    return None


def reprocess_rolls(client_id):
    """Rolls in reprocess stock, by batch: what to send for rework or re-check."""
    from apps.jobwork.models import JWOReprocess

    from .models import LotTaka

    location = stock.system_location(client_id, "REPROCESS")
    groups = defaultdict(list)
    for t in LotTaka.objects.filter(client_id=client_id, location=location, available_quantity__gt=0,
                                    deleted_at__isnull=True).select_related("item", "batch").order_by("updated_at"):
        groups[t.batch.batch_code if t.batch_id else t.lot_number].append(t)
    open_orders = defaultdict(list)
    for rp in JWOReprocess.objects.filter(client_id=client_id, deleted_at__isnull=True).exclude(status="Completed") \
            .select_related("job_work_order"):
        open_orders[rp.inward_no].append({"no": rp.no, "status": rp.status, "qty": str(rp.qty),
                                          "jwoNo": rp.job_work_order.jwo_no if rp.job_work_order_id else None})
    rows = []
    for code, rolls in groups.items():
        sources = {t.source_document_number for t in rolls if t.source_document_number}
        rows.append({
            "batchCode": code if rolls[0].batch_id else None,
            "lots": sorted({t.lot_number for t in rolls}),
            "fabric": ", ".join(sorted({t.item.name for t in rolls if t.item_id})),
            "rolls": [{"lotTakaId": str(t.id), "takaId": t.taka_id, "meters": str(round4(D(t.available_quantity))),
                       "remarks": t.qc_notes} for t in rolls],
            "meters": str(round4(sum((D(t.available_quantity) for t in rolls), ZERO))),
            "since": _date(min(t.updated_at for t in rolls).date()),
            "reprocessOrders": [o for s in sources for o in open_orders.get(s, [])],
        })
    rows.sort(key=lambda r: r["since"] or "")
    return rows
