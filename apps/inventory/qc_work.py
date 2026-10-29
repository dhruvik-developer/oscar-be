"""
QC Work: one list of every lot that needs (or had) QC, with where it stands.

A lot never disappears from this list when its QC moves on -- only its state
changes, and each row says the one next step:

    waiting    received, nobody has checked it yet         -> start  (QC form)
    inspected  checked and saved, not approved yet          -> finish (QC form)
    hold       approved with part kept on hold              -> finish (QC form)
    rework     sent for rework, waiting to be checked again -> recheck (re-check form)
    done       QC complete                                  -> view

Rows come from purchase receipts (grey QC), job-work inwards (dyeing QC) and
open rework orders. Saving goes through the existing endpoints
(/qc/disposition/ and /qc/reinspection/), so this module only reads.
"""
import json
from datetime import timedelta

from django.utils import timezone

from apps.core.money import ZERO, D, round4

STATE_LABELS = {
    "waiting": "Waiting for QC",
    "inspected": "Checked, approval pending",
    "hold": "Part on hold",
    "rework": "In rework, re-check pending",
    "done": "QC done",
}
NEXT_ACTION = {"waiting": "start", "inspected": "finish", "hold": "finish", "rework": "recheck", "done": "view"}
#: How far back finished QC stays on the list (the "Done" chip).
DONE_DAYS = 30
OPEN_GRN = ("Pending", "On Hold")
OPEN_INWARD = ("Received", "Pending QC", "Pending", "QC Pending", "On Hold")


def _meta(text):
    text = (text or "").strip()
    if text.startswith("{") and text.endswith("}"):
        try:
            return json.loads(text)
        except ValueError:
            return {}
    return {}


def _date(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


def _split(source):
    """{good, second, hold, reprocess, reject} from a stored QC result."""
    keys = {"good": "goodQty", "second": "secondQualityQty", "hold": "holdQty",
            "reprocess": "reprocessQty", "reject": "rejectedQty"}
    return {k: float(source.get(v, 0) or 0) for k, v in keys.items()}


def _summary(split, uom):
    names = (("good", "good"), ("second", "second quality"), ("hold", "on hold"),
             ("reprocess", "rework"), ("reject", "rejected"))
    parts = [f"{round(split[k], 2):g} {label}" for k, label in names if split.get(k)]
    return ", ".join(parts) + (f" ({uom})" if parts and uom else "") if parts else None


def _grn_rows(client_id, since, open_rework):
    from apps.purchase.models import GoodsReceipt

    from . import qc as qc_service

    receipts = (
        GoodsReceipt.objects.filter(client_id=client_id, deleted_at__isnull=True)
        .filter(qc_status__in=OPEN_GRN) | GoodsReceipt.objects.filter(
            client_id=client_id, deleted_at__isnull=True, receipt_date__gte=since,
        )
    )
    rows = []
    for r in receipts.distinct().select_related("party", "qc_by", "location").prefetch_related("lines__item") \
            .order_by("-receipt_date", "-created_at"):
        lines = list(r.lines.all())
        first = lines[0] if lines else None
        item = first.item if first else None
        uom = (first.stock_uom or first.uom) if first else None
        meta = _meta(r.qc_note)
        draft = meta.get("draft") or {}
        received = sum((D(l.stock_qty or l.received_qty or 0) for l in lines), ZERO)
        has_stock = qc_service.receipt_has_stock(r)
        pool = qc_service.pool_quantity(r)
        to_check = pool if has_stock else (received if r.qc_status in OPEN_GRN else ZERO)
        rework = open_rework.pop(r.grn_number, None)
        if rework is not None:
            state = "rework"
        elif r.qc_status == "On Hold":
            state = "hold"
        elif r.qc_status == "Pending":
            state = "inspected" if draft else "waiting"
        else:
            state = "done"
        result = _split(meta) if meta.get("cumulative") else None
        timeline = [{"date": _date(r.receipt_date), "label": f"Received on {r.grn_number}"
                     + (f" from {r.party.name}" if r.party_id else "")}]
        if draft:
            timeline.append({"date": (draft.get("savedAt") or "")[:10] or None, "label": "QC checked, saved for approval",
                             "detail": _summary(_split(draft), uom)})
        if r.qc_at and r.qc_status not in ("Pending",):
            timeline.append({"date": _date(timezone.localtime(r.qc_at).date()), "label": f"QC approved: {r.qc_status}",
                             "by": r.qc_by.name if r.qc_by_id else None, "detail": _summary(result, uom) if result else None})
        if rework is not None:
            timeline.append({"date": _date(rework.date), "label": f"Sent for rework ({rework.no})",
                             "detail": f"{round(float(rework.qty or 0), 2):g} {uom or ''}".strip()})
        rows.append({
            "key": f"GRN-{r.id}",
            "kind": "GRN",
            "stage": "grey",
            "receiptId": str(r.id),
            "reprocessId": str(rework.id) if rework is not None else None,
            "reference": r.grn_number,
            "lotNo": first.batch_number if first else None,
            "fabric": item.name if item else None,
            "supplier": r.party.name if r.party_id else None,
            "uom": uom,
            "date": _date(r.receipt_date),
            "receivedQty": float(round4(received)),
            "toCheckQty": float(round4(D(rework.qty or 0))) if rework is not None else float(round4(to_check)),
            "state": state,
            "draft": _split(draft) if draft else None,
            "result": _summary(result, uom) if result else None,
            "remarks": draft.get("remarks") or meta.get("remarks") or None,
            "timeline": timeline,
        })
    return rows


def _inward_rows(client_id, since, open_rework):
    from apps.jobwork.models import JWOInward

    from . import qc as qc_service

    inwards = (
        JWOInward.objects.filter(client_id=client_id, deleted_at__isnull=True, status__in=OPEN_INWARD)
        | JWOInward.objects.filter(client_id=client_id, deleted_at__isnull=True, date__gte=since)
    )
    rows = []
    for inw in inwards.distinct().select_related("job_work_order", "job_work_order__vendor",
                                                 "job_work_order__process_plan").order_by("-date", "-created_at"):
        jwo = inw.job_work_order
        plan = jwo.process_plan if jwo else None
        waiting, _ = qc_service.jobwork_inward_pool(inw)
        pool = sum((u["qty"] for u in waiting), ZERO)
        rework = open_rework.pop(inw.no, None)
        if rework is not None:
            state = "rework"
        elif inw.status == "On Hold":
            state = "hold"
        elif inw.status in OPEN_INWARD or pool > ZERO:
            state = "waiting"
        else:
            state = "done"
        vendor = (jwo.vendor_name or (jwo.vendor.name if jwo.vendor_id else None)) if jwo else None
        process = (jwo.process or "job work") if jwo else "job work"
        result = None
        if state == "done":
            result = _summary({"good": float(inw.accepted or 0), "reject": float(inw.rejected or 0),
                               "second": 0, "hold": 0, "reprocess": 0}, "m")
        timeline = [{"date": _date(inw.date), "label": f"Back from {process} ({inw.no})"
                     + (f" — {vendor}" if vendor else "")}]
        if state == "done":
            timeline.append({"date": _date(timezone.localtime(inw.updated_at).date()),
                             "label": f"QC approved: {inw.status}", "detail": result})
        if rework is not None:
            timeline.append({"date": _date(rework.date), "label": f"Sent for rework ({rework.no})"})
        rows.append({
            "key": f"JWI-{inw.id}",
            "kind": "JOB_WORK",
            "stage": "dyeing",
            "receiptId": str(inw.id),
            "reprocessId": str(rework.id) if rework is not None else None,
            "reference": inw.no,
            "lotNo": (plan.grey_lot_no or None) if plan else None,
            "fabric": ((plan.fabric_item or plan.fabric_quality) or None) if plan else None,
            "supplier": vendor,
            "uom": "m",
            "date": _date(inw.date),
            "receivedQty": float(round4(D(inw.qty or 0))),
            "toCheckQty": float(round4(D(rework.qty or 0))) if rework is not None
            else float(round4(pool if pool > ZERO else D(inw.qty or 0) if state != "done" else ZERO)),
            "state": state,
            "draft": None,
            "result": result,
            "remarks": None,
            "timeline": timeline,
        })
    return rows


def _rework_rows(open_rework):
    """Open rework orders whose source receipt is not on the list."""
    rows = []
    for rp in open_rework.values():
        jwo = rp.job_work_order
        plan = jwo.process_plan if jwo else None
        rows.append({
            "key": f"RP-{rp.id}",
            "kind": "REWORK",
            "stage": "dyeing" if jwo and not str(rp.inward_no or "").startswith("GRN") else "grey",
            "receiptId": None,
            "reprocessId": str(rp.id),
            "reference": rp.no,
            "lotNo": (plan.grey_lot_no or None) if plan else None,
            "fabric": ((plan.fabric_item or plan.fabric_quality) or None) if plan else None,
            "supplier": (jwo.vendor_name or (jwo.vendor.name if jwo.vendor_id else None)) if jwo else None,
            "uom": "m",
            "date": _date(rp.date),
            "receivedQty": float(round4(D(rp.qty or 0))),
            "toCheckQty": float(round4(D(rp.qty or 0))),
            "state": "rework",
            "draft": None,
            "result": None,
            "remarks": rp.reason,
            "timeline": [{"date": _date(rp.date), "label": f"Sent for rework ({rp.no})"
                          + (f" from {rp.inward_no}" if rp.inward_no else ""), "detail": rp.reason}],
        })
    return rows


def qc_worklist(client_id, *, stage=None, q=None, state=None):
    """Every lot to QC (and QC'd in the last ``DONE_DAYS`` days), newest first,
    with counts per state for the chips."""
    from apps.jobwork.models import JWOReprocess

    since = timezone.localdate() - timedelta(days=DONE_DAYS)
    open_rework = {
        rp.inward_no: rp
        for rp in JWOReprocess.objects.filter(client_id=client_id, deleted_at__isnull=True)
        .exclude(status="Completed").select_related("job_work_order", "job_work_order__vendor",
                                                    "job_work_order__process_plan").order_by("date")
        if rp.inward_no
    }
    rows = []
    if stage != "dyeing":
        rows += _grn_rows(client_id, since, open_rework)
    if stage != "grey":
        rows += _inward_rows(client_id, since, open_rework)
    rows += [r for r in _rework_rows(open_rework) if stage in (None, r["stage"])]

    if q:
        needle = q.strip().lower()
        rows = [r for r in rows if any(needle in str(r.get(k) or "").lower()
                                       for k in ("reference", "lotNo", "fabric", "supplier"))]
    counts = {s: 0 for s in STATE_LABELS}
    for r in rows:
        counts[r["state"]] += 1
        r["stateLabel"] = STATE_LABELS[r["state"]]
        r["nextAction"] = NEXT_ACTION[r["state"]]
    # Open lots first, the one waiting longest on top; finished QC newest first.
    order = {"waiting": 0, "inspected": 1, "hold": 2, "rework": 3}
    open_rows = sorted((r for r in rows if r["state"] != "done"), key=lambda r: (order[r["state"]], r["date"] or ""))
    done_rows = sorted((r for r in rows if r["state"] == "done"), key=lambda r: r["date"] or "", reverse=True)
    rows = open_rows + done_rows
    if state in STATE_LABELS:
        rows = [r for r in rows if r["state"] == state]
    return {"results": rows, "counts": counts, "doneDays": DONE_DAYS}
