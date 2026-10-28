"""Owner Dashboards & MIS reporting engine (PDF §15).

Provides pure database computations for:
- Annual Commercial Leakage (₹ and % of sales)
- Party Profitability
- Design & Colour Fabric Analysis
- Comprehensive Stock Ageing (0-30, 31-60, 61-90, 91-180, 180+)
"""
from datetime import date, timedelta
from decimal import Decimal
from django.db.models import Count, DecimalField, F, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.core.money import ZERO, round2, round4, D
from apps.inventory.models import LotTaka, Sample, Trip
from apps.pms.models import IncentiveCalculation
from apps.sales.models import BrokerCommission, SalesInvoice, SalesInvoiceLine, SalesReturn

MONEY = DecimalField(max_digits=18, decimal_places=2)


def annual_commercial_leakage(client_id, date_from=None, date_to=None):
    """Annual Commercial Leakage report (PDF §15).

    Returns:
    - Gross sales
    - Party discount (₹ and %)
    - Sales incentive (₹ and %)
    - Brokerage (₹ and %)
    - Sales returns (₹ and %)
    - Sampling cost (₹ and %)
    - Transport support (₹ and %)
    - Net contribution (₹ and %)
    """
    today = timezone.localdate()
    if not date_from:
        # Default to financial year starting April 1
        fy_year = today.year if today.month >= 4 else today.year - 1
        date_from = date(fy_year, 4, 1)
    if not date_to:
        date_to = today

    if isinstance(date_from, str):
        date_from = date.fromisoformat(date_from[:10])
    if isinstance(date_to, str):
        date_to = date.fromisoformat(date_to[:10])

    # 1. Gross Sales
    inv_qs = SalesInvoice.objects.filter(
        client_id=client_id,
        doc_date__gte=date_from,
        doc_date__lte=date_to,
        deleted_at__isnull=True,
    ).exclude(status__in=["Draft", "Cancelled"])

    gross_sales = inv_qs.aggregate(val=Coalesce(Sum("total"), Value(Decimal("0.00")), output_field=MONEY))["val"]
    gross_sales = D(gross_sales)

    # 2. Party Discount
    party_discount = inv_qs.aggregate(val=Coalesce(Sum("total_discount"), Value(Decimal("0.00")), output_field=MONEY))["val"]
    party_discount = D(party_discount)

    # 3. Sales Incentive
    inc_qs = IncentiveCalculation.objects.filter(
        client_id=client_id,
        deleted_at__isnull=True,
        status__in=["Approved", "Posted", "Paid"],
        period_start__gte=date_from,
        period_end__lte=date_to,
    )
    sales_incentive = inc_qs.aggregate(val=Coalesce(Sum("incentive_amount"), Value(Decimal("0.00")), output_field=MONEY))["val"]
    sales_incentive = D(sales_incentive)

    # 4. Brokerage
    brk_qs = BrokerCommission.objects.filter(
        client_id=client_id,
        deleted_at__isnull=True,
        invoice_date__gte=date_from,
        invoice_date__lte=date_to,
    ).exclude(status__in=["Cancelled", "Hold"])
    brokerage = brk_qs.aggregate(val=Coalesce(Sum("commission_amount"), Value(Decimal("0.00")), output_field=MONEY))["val"]
    brokerage = D(brokerage)

    # 5. Sales Returns
    ret_qs = SalesReturn.objects.filter(
        client_id=client_id,
        doc_date__gte=date_from,
        doc_date__lte=date_to,
        deleted_at__isnull=True,
    ).exclude(status__in=["Draft", "Cancelled"])
    sales_returns = ret_qs.aggregate(val=Coalesce(Sum("total"), Value(Decimal("0.00")), output_field=MONEY))["val"]
    sales_returns = D(sales_returns)

    # 6. Sampling Cost
    sample_qs = Sample.objects.filter(
        client_id=client_id,
        sample_date__gte=date_from,
        sample_date__lte=date_to,
        deleted_at__isnull=True,
    )
    sampling_cost = sample_qs.aggregate(val=Coalesce(Sum("estimated_cost"), Value(Decimal("0.00")), output_field=MONEY))["val"]
    sampling_cost = D(sampling_cost)

    # 7. Transport Support (Freight paid by Company)
    trip_qs = Trip.objects.filter(
        client_id=client_id,
        trip_date__gte=date_from,
        trip_date__lte=date_to,
        paid_by="Company",
        deleted_at__isnull=True,
    )
    transport_support = trip_qs.aggregate(val=Coalesce(Sum("total_charges"), Value(Decimal("0.00")), output_field=MONEY))["val"]
    transport_support = D(transport_support)

    # 8. Net Contribution
    deductions = party_discount + sales_incentive + brokerage + sales_returns + sampling_cost + transport_support
    net_contribution = gross_sales - deductions

    # 9. Payroll salary cost (PDF §02: annual cost of salary against sales).
    # Fixed salary from approved/paid payslips. Shown for visibility only;
    # §15 defines net contribution without it.
    from apps.hrms.models import Payslip

    salary_cost = D(Payslip.objects.filter(
        client_id=client_id,
        period_month__gte=date_from.replace(day=1),
        period_month__lte=date_to,
        status__in=["Approved", "Paid"],
        deleted_at__isnull=True,
    ).aggregate(val=Coalesce(Sum("earned_salary"), Value(Decimal("0.00")), output_field=MONEY))["val"])

    def pct(val):
        if gross_sales > ZERO:
            return float(round2(val / gross_sales * D(100)))
        return 0.0

    items = [
        {"key": "grossSales", "label": "Gross Billed Sales", "amount": float(round2(gross_sales)), "pct": 100.0, "type": "revenue"},
        {"key": "partyDiscount", "label": "Party / Scheme Discount", "amount": float(round2(party_discount)), "pct": pct(party_discount), "type": "leakage"},
        {"key": "salesIncentive", "label": "Salesperson Incentives", "amount": float(round2(sales_incentive)), "pct": pct(sales_incentive), "type": "leakage"},
        {"key": "brokerage", "label": "Broker Commission", "amount": float(round2(brokerage)), "pct": pct(brokerage), "type": "leakage"},
        {"key": "salesReturns", "label": "Sales Returns & Rejections", "amount": float(round2(sales_returns)), "pct": pct(sales_returns), "type": "leakage"},
        {"key": "samplingCost", "label": "Sampling & Cut-Piece Cost", "amount": float(round2(sampling_cost)), "pct": pct(sampling_cost), "type": "leakage"},
        {"key": "transportSupport", "label": "Transport / Tempo Freight Support", "amount": float(round2(transport_support)), "pct": pct(transport_support), "type": "leakage"},
        {"key": "netContribution", "label": "Net Contribution Margin", "amount": float(round2(net_contribution)), "pct": pct(net_contribution), "type": "net"},
        {"key": "salaryCost", "label": "Payroll Salary Cost (not in net contribution)", "amount": float(round2(salary_cost)), "pct": pct(salary_cost), "type": "cost"},
    ]

    return {
        "periodFrom": date_from.isoformat(),
        "periodTo": date_to.isoformat(),
        "grossSales": float(round2(gross_sales)),
        "totalLeakage": float(round2(deductions)),
        "leakagePct": pct(deductions),
        "netContribution": float(round2(net_contribution)),
        "netContributionPct": pct(net_contribution),
        "salaryCost": float(round2(salary_cost)),
        "salaryCostPct": pct(salary_cost),
        "items": items,
    }


def _ledger_cogs(client_id, source_type, source_ids):
    """Net COGS the ledger posted per source document (Dr - Cr on the COGS
    account, live entries only) -- the figure the P&L carries."""
    from apps.accounting.models import JournalLine

    out = {}
    lines = JournalLine.objects.filter(
        client_id=client_id,
        account__system_key="cogs",
        journal_entry__source_document_type=source_type,
        journal_entry__source_document_id__in=list(source_ids),
        journal_entry__reversal_of__isnull=True,
        journal_entry__deleted_at__isnull=True,
    ).exclude(journal_entry__status="Reversed").values_list(
        "journal_entry__source_document_id", "debit", "credit"
    )
    for source_id, dr, cr in lines:
        out[source_id] = out.get(source_id, ZERO) + D(dr) - D(cr)
    return out


def _invoice_cost_basis(invoice):
    """How well the invoice's cost is known.

    ``actual``    every stock line went out on a costed SALE movement (the
                  batch / taka landed rate, else the WAC frozen at dispatch);
    ``estimated`` some stock line had no costed movement, so posting fell back
                  to the current WAC / item cost;
    ``missing``   stock lines but no COGS at all.
    """
    from apps.accounting.services import _movement_unit_cost

    stock_lines = [
        line for line in invoice.line_items.filter(deleted_at__isnull=True).select_related("item")
        if line.item_id and line.item.holds_stock
    ]
    if not stock_lines:
        return "no-stock"
    references = [("SalesInvoice", invoice.id)]
    if invoice.delivery_challan_id:
        references.append(("DeliveryChallan", invoice.delivery_challan_id))
    from apps.sales.models import DeliveryChallanLine

    for line in stock_lines:
        if line.sales_order_line_id:
            references.extend(
                ("DeliveryChallan", cid) for cid in DeliveryChallanLine.objects.filter(
                    sales_order_line_id=line.sales_order_line_id, deleted_at__isnull=True,
                ).values_list("delivery_challan_id", flat=True)
            )
    for line in stock_lines:
        if _movement_unit_cost(invoice.client_id, line.item_id, references) is None:
            return "estimated"
    return "actual"


def party_profitability_report(client_id, date_from=None, date_to=None):
    """Party-wise sales, actual COGS, gross profit and contribution (PDF §15).

    COGS is what the ledger posted for each invoice (less what returns
    reversed): the stock that actually left, at its batch / taka landed rate
    or the WAC frozen at dispatch -- never a fixed ratio. Sales are net of GST
    (taxable value) and already net of discounts, so discount is shown, not
    deducted again. Parties whose cost is not fully known are flagged.
    """
    today = timezone.localdate()
    date_from = date_from or (today - timedelta(days=90))
    date_to = date_to or today

    if isinstance(date_from, str): date_from = date.fromisoformat(date_from[:10])
    if isinstance(date_to, str): date_to = date.fromisoformat(date_to[:10])

    invoices = list(SalesInvoice.objects.filter(
        client_id=client_id,
        doc_date__gte=date_from,
        doc_date__lte=date_to,
        deleted_at__isnull=True,
    ).exclude(status__in=["Draft", "Cancelled"]).select_related("party"))
    invoice_cogs = _ledger_cogs(client_id, "SalesInvoice", [inv.id for inv in invoices])

    party_data = {}

    def group_for(pid, pname):
        return party_data.setdefault(pid, {
            "partyId": pid, "partyName": pname, "invoiceCount": 0,
            "sales": ZERO, "salesInclTax": ZERO, "cogs": ZERO, "discount": ZERO,
            "brokerage": ZERO, "returns": ZERO, "returnsCogs": ZERO, "transport": ZERO,
            "costStatus": "actual", "invoicesWithoutActualCost": [],
        })

    for inv in invoices:
        pid = str(inv.party_id)
        group = group_for(pid, inv.party.name if inv.party else (inv.party_name or "Direct"))
        group["invoiceCount"] += 1
        taxable = D(inv.taxable_value or 0) or (D(inv.total or 0) - D(inv.total_tax or 0))
        group["sales"] += taxable
        group["salesInclTax"] += D(inv.total or 0)
        group["discount"] += D(inv.total_discount or 0)
        posted = invoice_cogs.get(inv.id)
        basis = _invoice_cost_basis(inv)
        if basis == "no-stock":
            continue
        if posted is None or posted <= ZERO:
            basis = "missing"
        else:
            group["cogs"] += posted
        if basis != "actual":
            group["invoicesWithoutActualCost"].append(inv.invoice_number or str(inv.id))
            if basis == "missing" or group["costStatus"] == "actual":
                group["costStatus"] = "incomplete" if basis == "missing" else "estimated"

    commissions = BrokerCommission.objects.filter(
        client_id=client_id, invoice_date__gte=date_from, invoice_date__lte=date_to, deleted_at__isnull=True,
    ).exclude(status="Cancelled")
    for bc in commissions:
        pid = str(bc.party_id)
        if pid in party_data:
            party_data[pid]["brokerage"] += D(bc.commission_amount or 0)

    returns = list(SalesReturn.objects.filter(
        client_id=client_id, doc_date__gte=date_from, doc_date__lte=date_to, deleted_at__isnull=True,
    ).exclude(status="Cancelled"))
    return_cogs = _ledger_cogs(client_id, "SalesReturn", [r.id for r in returns])
    for sr in returns:
        pid = str(sr.party_id)
        if pid in party_data:
            party_data[pid]["returns"] += D(getattr(sr, "taxable_value", None) or 0) or (D(sr.total or 0) - D(getattr(sr, "total_tax", 0) or 0))
            # A return's ledger entry credits COGS: negative here.
            party_data[pid]["returnsCogs"] += -return_cogs.get(sr.id, ZERO)

    from apps.sales.models import SalesOrder
    for so in SalesOrder.objects.filter(
        client_id=client_id, doc_date__gte=date_from, doc_date__lte=date_to, deleted_at__isnull=True,
    ).exclude(stage="Cancelled"):
        pid = str(so.party_id)
        if pid in party_data and getattr(so, "allocated_freight_cost", None):
            party_data[pid]["transport"] += D(so.allocated_freight_cost)

    rows = []
    for p in party_data.values():
        net_sales = p["sales"] - p["returns"]
        cogs = round2(p["cogs"] - p["returnsCogs"])
        gross = round2(net_sales - cogs)
        net = round2(gross - p["brokerage"] - p["transport"])
        rows.append({
            "partyId": p["partyId"],
            "partyName": p["partyName"],
            "invoiceCount": p["invoiceCount"],
            "sales": float(round2(p["sales"])),
            "salesInclTax": float(round2(p["salesInclTax"])),
            "returns": float(round2(p["returns"])),
            "netSales": float(round2(net_sales)),
            "cogs": float(cogs),
            "grossProfit": float(gross),
            "grossMarginPct": float(round2(gross / net_sales * Decimal("100"))) if net_sales > ZERO else 0.0,
            "discount": float(round2(p["discount"])),
            "brokerage": float(round2(p["brokerage"])),
            "transport": float(round2(p["transport"])),
            "netContribution": float(net),
            "marginPct": float(round2(net / net_sales * Decimal("100"))) if net_sales > ZERO else 0.0,
            "costStatus": p["costStatus"],
            "invoicesWithoutActualCost": p["invoicesWithoutActualCost"],
        })

    rows.sort(key=lambda x: x["sales"], reverse=True)
    return {
        "periodFrom": date_from.isoformat(),
        "periodTo": date_to.isoformat(),
        "costMethod": "Ledger COGS per invoice (batch / taka landed rate or WAC frozen at dispatch), "
                      "less COGS reversed by returns. Sales are net of GST and discounts.",
        "rows": rows,
        "results": rows,
    }


def salesperson_profitability_report(client_id, date_from=None, date_to=None):
    """Salesperson-wise sales, actual COGS, collection, returns and incentive (PDF §15).

    Same sources as the party report: sales net of GST, COGS from the ledger,
    collection from payment allocations on the invoices, incentive from the
    incentive calculations whose period overlaps the report period. No ratio
    is assumed; unknown cost is flagged.
    """
    from apps.sales.models import PaymentAllocation

    today = timezone.localdate()
    date_from = date_from or (today - timedelta(days=90))
    date_to = date_to or today
    if isinstance(date_from, str): date_from = date.fromisoformat(date_from[:10])
    if isinstance(date_to, str): date_to = date.fromisoformat(date_to[:10])

    invoices = list(SalesInvoice.objects.filter(
        client_id=client_id, doc_date__gte=date_from, doc_date__lte=date_to, deleted_at__isnull=True,
    ).exclude(status__in=["Draft", "Cancelled"]).select_related("salesperson_fk"))
    cogs_by_invoice = _ledger_cogs(client_id, "SalesInvoice", [inv.id for inv in invoices])
    collected = {}
    for doc_id, amount in PaymentAllocation.objects.filter(
        client_id=client_id, payment_side="in", document_type="SalesInvoice",
        document_id__in=[inv.id for inv in invoices], deleted_at__isnull=True,
    ).values_list("document_id", "amount"):
        collected[doc_id] = collected.get(doc_id, ZERO) + D(amount)

    people = {}

    def row_for(key, name):
        return people.setdefault(key, {
            "employeeId": key if key != "-" else None, "name": name, "invoiceCount": 0,
            "sales": ZERO, "discount": ZERO, "cogs": ZERO, "collection": ZERO,
            "returns": ZERO, "returnsCogs": ZERO, "incentive": ZERO,
            "costStatus": "actual", "invoicesWithoutActualCost": [],
        })

    invoice_owner = {}
    for inv in invoices:
        key = str(inv.salesperson_fk_id) if inv.salesperson_fk_id else (inv.salesperson or "-")
        name = inv.salesperson_fk.name if inv.salesperson_fk_id else (inv.salesperson or "Not assigned")
        row = row_for(key, name)
        invoice_owner[inv.id] = key
        row["invoiceCount"] += 1
        row["sales"] += D(inv.taxable_value or 0) or (D(inv.total or 0) - D(inv.total_tax or 0))
        row["discount"] += D(inv.total_discount or 0)
        row["collection"] += collected.get(inv.id, ZERO)
        basis = _invoice_cost_basis(inv)
        if basis == "no-stock":
            continue
        posted = cogs_by_invoice.get(inv.id)
        if posted is None or posted <= ZERO:
            basis = "missing"
        else:
            row["cogs"] += posted
        if basis != "actual":
            row["invoicesWithoutActualCost"].append(inv.invoice_number or str(inv.id))
            if basis == "missing" or row["costStatus"] == "actual":
                row["costStatus"] = "incomplete" if basis == "missing" else "estimated"

    returns = list(SalesReturn.objects.filter(
        client_id=client_id, doc_date__gte=date_from, doc_date__lte=date_to, deleted_at__isnull=True,
    ).exclude(status="Cancelled"))
    return_cogs = _ledger_cogs(client_id, "SalesReturn", [r.id for r in returns])
    for sr in returns:
        key = invoice_owner.get(sr.sales_invoice_id)
        if key is None:
            continue
        people[key]["returns"] += D(sr.taxable_value or 0) or (D(sr.total or 0) - D(sr.total_tax or 0))
        people[key]["returnsCogs"] += -return_cogs.get(sr.id, ZERO)

    for calc in IncentiveCalculation.objects.filter(
        client_id=client_id, deleted_at__isnull=True, period_start__lte=date_to, period_end__gte=date_from,
    ).exclude(status="Reversed"):
        key = str(calc.salesperson_id or calc.employee_id)
        if key in people:
            people[key]["incentive"] += D(calc.incentive_amount or 0)

    rows = []
    for p in people.values():
        net_sales = p["sales"] - p["returns"]
        cogs = round2(p["cogs"] - p["returnsCogs"])
        gross = round2(net_sales - cogs)
        contribution = round2(gross - p["incentive"])
        rows.append({
            "employeeId": p["employeeId"],
            "name": p["name"],
            "invoiceCount": p["invoiceCount"],
            "netSales": float(round2(net_sales)),
            "discount": float(round2(p["discount"])),
            "returns": float(round2(p["returns"])),
            "collection": float(round2(p["collection"])),
            "cogs": float(cogs),
            "grossProfit": float(gross),
            "gpMarginPct": float(round2(gross / net_sales * Decimal("100"))) if net_sales > ZERO else 0.0,
            "incentive": float(round2(p["incentive"])),
            "contribution": float(contribution),
            "costStatus": p["costStatus"],
            "invoicesWithoutActualCost": p["invoicesWithoutActualCost"],
        })
    rows.sort(key=lambda r: r["netSales"], reverse=True)
    return {
        "periodFrom": date_from.isoformat(),
        "periodTo": date_to.isoformat(),
        "results": rows,
        "rows": rows,
    }


def design_colour_analysis(client_id, date_from=None, date_to=None):
    """Fabric Design & Colour Shade production, sales, balance, and ageing (PDF §15)."""
    takas = LotTaka.objects.filter(client_id=client_id, deleted_at__isnull=True).select_related("design", "shade", "fabric_quality")

    by_key = {}
    today = timezone.localdate()

    for t in takas:
        d_name = t.design.design_name if t.design else (getattr(t, 'design_code', '') or "Plain / Solid")
        s_name = t.shade.shade_name if t.shade else (getattr(t, 'shade_code', '') or "Standard")
        q_name = t.fabric_quality.name if t.fabric_quality else "Standard Quality"
        key = f"{d_name}___{s_name}"

        group = by_key.setdefault(key, {
            "design": d_name,
            "shade": s_name,
            "quality": q_name,
            "totalTakas": 0,
            "availableMeters": ZERO,
            "totalMeters": ZERO,
            "soldMeters": ZERO,
            "daysList": [],
        })

        group["totalTakas"] += 1
        m = D(t.meter or 0)
        group["totalMeters"] += m
        avail = D(t.available_quantity or 0)
        group["availableMeters"] += avail
        group["soldMeters"] += max(ZERO, m - avail)

        age = (today - (t.inward_date or t.created_at.date())).days
        group["daysList"].append(age)

    rows = []
    for g in by_key.values():
        avg_age = sum(g["daysList"]) // len(g["daysList"]) if g["daysList"] else 0
        rows.append({
            "design": g["design"],
            "shade": g["shade"],
            "quality": g["quality"],
            "totalTakas": g["totalTakas"],
            "totalMeters": float(round2(g["totalMeters"])),
            "soldMeters": float(round2(g["soldMeters"])),
            "availableMeters": float(round2(g["availableMeters"])),
            "avgAgeDays": avg_age,
        })

    rows.sort(key=lambda x: x["availableMeters"], reverse=True)
    return {"rows": rows, "results": rows}


def stock_ageing_mis(client_id, as_of=None):
    """Stock ageing buckets (0-30, 31-60, 61-90, 91-180, 180+) with meters and values."""
    today = timezone.localdate()
    as_of = as_of or today
    if isinstance(as_of, str): as_of = date.fromisoformat(as_of[:10])

    buckets = {
        "0-30": {"label": "0 - 30 Days", "takas": 0, "meters": ZERO, "value": ZERO},
        "31-60": {"label": "31 - 60 Days", "takas": 0, "meters": ZERO, "value": ZERO},
        "61-90": {"label": "61 - 90 Days", "takas": 0, "meters": ZERO, "value": ZERO},
        "91-180": {"label": "91 - 180 Days", "takas": 0, "meters": ZERO, "value": ZERO},
        "180+": {"label": "180+ Days (Slow)", "takas": 0, "meters": ZERO, "value": ZERO},
    }

    takas = LotTaka.objects.filter(
        client_id=client_id,
        available_quantity__gt=0,
        deleted_at__isnull=True,
    ).select_related("item")

    for t in takas:
        inward = t.inward_date or t.created_at.date()
        age = (as_of - inward).days
        if age <= 30: b_key = "0-30"
        elif age <= 60: b_key = "31-60"
        elif age <= 90: b_key = "61-90"
        elif age <= 180: b_key = "91-180"
        else: b_key = "180+"

        m = D(t.available_quantity)
        rate = D(getattr(t, "unit_cost", None) or (t.item.cost_price if t.item else 0) or 50)
        val = round2(m * rate)

        b = buckets[b_key]
        b["takas"] += 1
        b["meters"] += m
        b["value"] += val

    result_buckets = [
        {
            "bucket": k,
            "label": v["label"],
            "takas": v["takas"],
            "meters": float(round2(v["meters"])),
            "value": float(round2(v["value"])),
        }
        for k, v in buckets.items()
    ]

    total_val = sum((b["value"] for b in result_buckets), 0.0)
    total_m = sum((b["meters"] for b in result_buckets), 0.0)
    total_t = sum((b["takas"] for b in result_buckets), 0)

    return {
        "asOf": as_of.isoformat(),
        "totalValue": float(round2(Decimal(str(total_val)))),
        "totalMeters": float(round2(Decimal(str(total_m)))),
        "summary": {
            "totalValue": float(round2(Decimal(str(total_val)))),
            "totalMeters": float(round2(Decimal(str(total_m)))),
            "totalTakas": total_t,
        },
        "buckets": result_buckets,
    }
