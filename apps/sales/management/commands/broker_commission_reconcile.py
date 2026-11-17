"""Reconciliation report for broker commissions. Reads only, unless asked.

    python manage.py broker_commission_reconcile
    python manage.py broker_commission_reconcile --flag-test TEST-LEAD-COMM-001 ...

Reports, per tenant:

* duplicate live commissions on one invoice and broker (should be none: the
  unique index uq_broker_commission_invoice refuses them);
* commissions with no source for their broker -- no lead, order, quotation,
  challan or party default names one, only the invoice itself carries it (how
  the old "customer's latest lead" fallback attributed unrelated invoices);
* leads that look like test data and are not flagged yet, with their
  commissions and any payout / ledger reference on them.

``--flag-test`` marks the named leads and the commissions raised off them as
test data, which takes them out of the registers and totals. Nothing is ever
deleted or merged, and amounts, payouts and journal entries are not touched.
"""
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count, Q

from apps.crm.models import Lead
from apps.sales.models import BrokerCommission, order_lead


def _lead_for(commission):
    invoice = commission.invoice
    return (
        order_lead(invoice.sales_order)
        or order_lead(invoice.delivery_challan)
        or order_lead(invoice)
    )


def _broker_source(commission):
    """Where this commission's broker is assigned, other than on the invoice."""
    invoice = commission.invoice
    lead = _lead_for(commission)
    if lead is not None:
        return f"lead {lead.lead_number}"
    for label, doc in (
        ("order", invoice.sales_order),
        ("quotation", getattr(invoice.sales_order, "quotation", None)),
        ("challan", invoice.delivery_challan),
    ):
        if doc is not None and doc.broker_fk_id:
            return label
    if invoice.party_id and invoice.party.default_broker_id:
        return "party default"
    return None


class Command(BaseCommand):
    help = "Report duplicate, unattributed and test broker commissions; optionally flag test leads."

    def add_arguments(self, parser):
        parser.add_argument(
            "--flag-test", nargs="+", metavar="LEAD_NUMBER", default=[],
            help="Mark these leads, and the commissions raised off them, as test data.",
        )

    def handle(self, *args, flag_test, **options):
        live = BrokerCommission.objects.filter(deleted_at__isnull=True).select_related(
            "broker", "invoice__party", "invoice__sales_order__quotation", "invoice__delivery_challan",
        )
        out = self.stdout.write

        dupes = (
            live.values("client_id", "invoice_id", "broker_id")
            .annotate(n=Count("id")).filter(n__gt=1)
        )
        out(f"Duplicate live commissions (same invoice and broker): {dupes.count()}")
        for row in dupes:
            out(f"  invoice {row['invoice_id']} broker {row['broker_id']}: {row['n']} rows")

        out("\nCommissions with no source for their broker:")
        unattributed = 0
        for c in live.filter(is_test_data=False).order_by("created_at"):
            if _broker_source(c) is None:
                unattributed += 1
                out(
                    f"  {c.commission_number}  {c.broker.name}  invoice {c.invoice.invoice_number}"
                    f"  {c.invoice.party_name or ''}  accrued {c.commission_amount}"
                    f"  paid {c.paid_amount}  {c.status}"
                    f"  journal {c.journal_entry_id or '-'}"
                )
        out(f"  total: {unattributed}")

        candidates = Lead.objects.filter(deleted_at__isnull=True, is_test_data=False).filter(
            Q(lead_number__istartswith="TEST-") | Q(name__istartswith="Test Lead")
        )
        out(f"\nUnflagged leads that look like test data: {candidates.count()}")
        for lead in candidates.order_by("created_at"):
            out(f"  {lead.lead_number}  {lead.name}  created {lead.created_at:%Y-%m-%d %H:%M}")
        test_comms = [c for c in live.filter(is_test_data=False) if (l := _lead_for(c)) is not None and l in candidates]
        for c in test_comms:
            out(
                f"    commission {c.commission_number}  invoice {c.invoice.invoice_number}"
                f"  accrued {c.commission_amount}  paid {c.paid_amount}  {c.status}"
                f"  journal {c.journal_entry_id or '-'}"
            )

        if not flag_test:
            out("\nReport only -- nothing changed.")
            return

        with transaction.atomic():
            leads = list(Lead.objects.filter(lead_number__in=flag_test, deleted_at__isnull=True))
            missing = set(flag_test) - {lead.lead_number for lead in leads}
            if missing:
                raise SystemExit(f"Unknown lead number(s): {', '.join(sorted(missing))}. Nothing changed.")
            Lead.objects.filter(pk__in=[lead.pk for lead in leads]).update(is_test_data=True)
            lead_ids = {lead.pk for lead in leads}
            comm_ids = [c.pk for c in live if (l := _lead_for(c)) is not None and l.pk in lead_ids]
            BrokerCommission.objects.filter(pk__in=comm_ids).update(is_test_data=True)
        out(f"\nFlagged {len(leads)} lead(s) and {len(comm_ids)} commission(s) as test data.")
