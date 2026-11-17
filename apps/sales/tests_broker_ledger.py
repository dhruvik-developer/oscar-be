"""Broker commission, end to end.

Lead (broker picked on the lead form) -> quotation -> sales order -> invoice
-> customer payment -> commission accrual -> broker payout -> broker statement.

* the lead form's broker survives every conversion onto the invoice
* commission is earned in proportion to what the customer has paid
* paying out what is earned so far is "Partially Paid", not "Paid"
* re-processing an invoice never adds a second commission
* one broker's statement never shows another broker's commissions
"""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from io import StringIO
from threading import Barrier

from django.core.management import call_command
from django.db import close_old_connections
from django.test import TransactionTestCase, skipUnlessDBFeature
from django.utils import timezone

from apps.crm.models import Lead, Stage
from apps.masters.models import Broker
from apps.sales import broker_services

from .models import BrokerCommission, SalesInvoice, SalesOrder
from .tests_phase1 import Phase1Base


class BrokerLedgerFlowTests(Phase1Base):
    def setUp(self):
        super().setUp()
        self.broker = Broker.objects.create(
            client=self.tenant, code="DY-001", name="Rakesh",
            commission_type="percent", default_value=Decimal("1"),
        )
        self.other_broker = Broker.objects.create(
            client=self.tenant, code="BR-002", name="Testing",
            commission_type="percent", default_value=Decimal("1"),
        )
        stage = Stage.objects.create(client=self.tenant, name="New", sequence=1)
        # Saved the way the lead form saves it: nested under "fields".
        self.lead = Lead.objects.create(
            client=self.tenant, lead_number="L-BRK-1", name="Broker Lead", stage=stage,
            custom_values={"fields": {
                "broker_id": str(self.broker.id),
                "broker_name": self.broker.name,
                "commission_basis": "percent",
                "commission_rate": "2",
            }},
        )

    def get(self, path, **query):
        return self.api.get(f"/api/v1{path}", query)

    def invoice_from_lead(self):
        quote = self.post("/sales/quotations/", {
            "partyId": str(self.customer.id),
            "crmLead": str(self.lead.id),
            "date": str(timezone.localdate()),
            "lineItems": [{"itemId": str(self.fabric.id), "qty": 10, "rate": 1000, "tax": 0}],
        })
        self.assertEqual(quote.status_code, 201, quote.content[:400])
        order = self.post(f"/sales/quotations/{quote.json()['id']}/convert-to-order/")
        self.assertEqual(order.status_code, 201, order.content[:400])
        order_id = order.json()["id"]
        self.assertEqual(SalesOrder.objects.get(pk=order_id).broker_fk_id, self.broker.id)

        invoice = self.post(f"/sales/orders/{order_id}/convert-to-invoice/", {"directInvoice": True})
        self.assertEqual(invoice.status_code, 201, invoice.content[:400])
        invoice_id = invoice.json()["id"]
        self.assertEqual(SalesInvoice.objects.get(pk=invoice_id).broker_fk_id, self.broker.id)

        finalized = self.post(f"/sales/invoices/{invoice_id}/finalize/")
        self.assertEqual(finalized.status_code, 200, finalized.content[:400])
        return SalesInvoice.objects.get(pk=invoice_id)

    def pay_customer(self, invoice, amount):
        response = self.post("/sales/payments/", {
            "customerId": str(self.customer.id),
            "date": str(timezone.localdate()),
            "amount": amount,
            "mode": "Cash",
            "invoiceId": str(invoice.id),
        })
        self.assertEqual(response.status_code, 201, response.content[:400])

    def statement(self, broker):
        response = self.get("/sales/broker-commissions/statement/", brokerId=str(broker.id))
        self.assertEqual(response.status_code, 200, response.content[:400])
        return response.json()

    def test_lead_form_broker_is_lifted_for_commission_readers(self):
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.custom_values["broker_id"], str(self.broker.id))
        self.assertEqual(self.lead.custom_values["commission_rate"], "2")

    def test_full_flow_partial_collection_then_payout(self):
        invoice = self.invoice_from_lead()
        commission = BrokerCommission.objects.get(invoice=invoice)
        self.assertEqual(commission.broker_id, self.broker.id)
        self.assertEqual(commission.rate, Decimal("2"))  # the lead's agreed rate
        self.assertEqual(commission.commission_amount, Decimal("0"))
        self.assertEqual(commission.status, "Awaiting Collection")

        # Half collected -> half the brokerage (2% of 10,000 = 200) earned.
        self.pay_customer(invoice, 5000)
        commission.refresh_from_db()
        self.assertEqual(commission.commission_amount, Decimal("100.00"))
        self.assertEqual(commission.status, "Accrued")

        data = self.statement(self.broker)
        self.assertEqual(Decimal(data["totalAccrued"]), Decimal("100.00"))
        self.assertEqual(Decimal(data["totalPaid"]), Decimal("0"))
        self.assertEqual(Decimal(data["pendingBalance"]), Decimal("100.00"))
        self.assertEqual(len(data["commissions"]), 1)
        row = data["commissions"][0]
        self.assertEqual(row["invoiceNumber"], invoice.invoice_number)
        self.assertEqual(row["partyName"], self.customer.name)

        # The other broker sees none of it.
        other = self.statement(self.other_broker)
        self.assertEqual(other["commissions"], [])
        self.assertEqual(Decimal(other["totalAccrued"]), Decimal("0"))

        # Pay the broker what is earned so far: not "Paid" -- half is still to come.
        for step in ("approve", "make-payable"):
            r = self.post(f"/sales/broker-commissions/{commission.id}/{step}/")
            self.assertEqual(r.status_code, 200, r.content[:400])
        r = self.post(f"/sales/broker-commissions/{commission.id}/pay/", {"amount": 100})
        self.assertEqual(r.status_code, 200, r.content[:400])
        commission.refresh_from_db()
        self.assertEqual(commission.paid_amount, Decimal("100.00"))
        self.assertEqual(commission.status, "Partially Paid")
        data = self.statement(self.broker)
        self.assertEqual(Decimal(data["totalPaid"]), Decimal("100.00"))
        self.assertEqual(Decimal(data["pendingBalance"]), Decimal("0.00"))

        # Customer pays the rest -> the other half falls due.
        self.pay_customer(invoice, 5000)
        commission.refresh_from_db()
        self.assertEqual(commission.commission_amount, Decimal("200.00"))
        self.assertEqual(commission.status, "Payable")
        self.assertEqual(Decimal(self.statement(self.broker)["pendingBalance"]), Decimal("100.00"))

        r = self.post(f"/sales/broker-commissions/{commission.id}/pay/", {"amount": 100})
        self.assertEqual(r.status_code, 200, r.content[:400])
        commission.refresh_from_db()
        self.assertEqual(commission.status, "Paid")
        data = self.statement(self.broker)
        self.assertEqual(Decimal(data["totalPaid"]), Decimal("200.00"))
        self.assertEqual(Decimal(data["pendingBalance"]), Decimal("0.00"))
        self.assertEqual(len(data["commissions"]), 1)

    def test_reprocessing_an_invoice_keeps_one_commission(self):
        invoice = self.invoice_from_lead()
        self.pay_customer(invoice, 5000)
        first = BrokerCommission.objects.get(invoice=invoice)

        again = broker_services.calculate_broker_commission(invoice)

        self.assertEqual(again.id, first.id)
        self.assertEqual(again.commission_number, first.commission_number)
        self.assertEqual(again.commission_amount, Decimal("100.00"))
        self.assertEqual(BrokerCommission.objects.filter(invoice=invoice).count(), 1)

    def test_order_without_a_broker_stays_unassigned(self):
        self.customer.default_broker = self.broker
        self.customer.save(update_fields=["default_broker", "updated_at"])
        order = self.order()
        self.assertIsNone(SalesOrder.objects.get(pk=order["id"]).broker_fk_id)


class BrokerCommissionNoDuplicateTests(BrokerLedgerFlowTests):
    """One commission per invoice and broker, whatever is repeated."""

    def commissions(self):
        return BrokerCommission.objects.filter(client_id=self.tenant.id, deleted_at__isnull=True)

    def test_creating_a_lead_creates_no_commission(self):
        self.assertEqual(self.commissions().count(), 0)

    def test_repeated_finalize_and_installments_keep_one_commission(self):
        invoice = self.invoice_from_lead()
        again = self.post(f"/sales/invoices/{invoice.id}/finalize/")
        self.assertIn(again.status_code, (200, 409), again.content[:300])
        for amount in (2000, 3000, 5000):
            self.pay_customer(invoice, amount)
        self.assertEqual(self.commissions().count(), 1)
        commission = self.commissions().get()
        # Fully collected: the whole 2% of 10,000, never more.
        self.assertEqual(commission.commission_amount, Decimal("200.00"))

    def test_repeated_payout_requests_cannot_overpay(self):
        invoice = self.invoice_from_lead()
        self.pay_customer(invoice, 10000)
        commission = self.commissions().get()
        for step in ("approve", "make-payable"):
            self.post(f"/sales/broker-commissions/{commission.id}/{step}/")
        first = self.post(f"/sales/broker-commissions/{commission.id}/pay/", {"amount": 200})
        second = self.post(f"/sales/broker-commissions/{commission.id}/pay/", {"amount": 200})
        self.assertEqual(first.status_code, 200, first.content[:300])
        self.assertNotEqual(second.status_code, 200)
        commission.refresh_from_db()
        self.assertEqual(commission.paid_amount, Decimal("200.00"))

    def test_reading_the_registers_creates_nothing(self):
        invoice = self.invoice_from_lead()
        self.pay_customer(invoice, 5000)
        before = list(self.commissions().values_list("id", "commission_amount", "updated_at"))
        for path, query in (
            ("/sales/broker-commissions/", {}),
            ("/sales/broker-commissions/leads/", {}),
            ("/sales/broker-commissions/analytics/", {}),
            ("/sales/broker-commissions/statement/", {"brokerId": str(self.broker.id)}),
        ):
            for _ in range(2):
                self.assertEqual(self.get(path, **query).status_code, 200)
        self.assertEqual(list(self.commissions().values_list("id", "commission_amount", "updated_at")), before)

    def test_totals_count_each_commission_once(self):
        invoice = self.invoice_from_lead()
        self.pay_customer(invoice, 5000)
        statement = self.statement(self.broker)
        analytics = self.get("/sales/broker-commissions/analytics/").json()
        stats = next(b for b in analytics["brokerStats"] if b["brokerId"] == str(self.broker.id))
        self.assertEqual(Decimal(statement["totalAccrued"]), Decimal("100.00"))
        self.assertEqual(Decimal(stats["accruedCommission"]), Decimal("100.00"))
        self.assertEqual(Decimal(analytics["totalAccrued"]), Decimal("100.00"))
        lead_row = next(r for r in self.get("/sales/broker-commissions/leads/").json() if r["id"] == str(self.lead.id))
        self.assertEqual(Decimal(lead_row["earnedCommission"]), Decimal("100.00"))
        self.assertEqual(len(lead_row["invoices"]), 1)

    def test_another_invoice_of_the_same_customer_is_not_the_leads(self):
        """The customer of a broker lead buys again, unrelated: no broker, no commission."""
        self.lead.party = self.customer
        self.lead.save(update_fields=["party", "updated_at"])
        order = self.order()
        invoice = self.post(f"/sales/orders/{order['id']}/convert-to-invoice/", {"directInvoice": True})
        self.assertEqual(invoice.status_code, 201, invoice.content[:300])
        self.post(f"/sales/invoices/{invoice.json()['id']}/finalize/")
        unrelated = SalesInvoice.objects.get(pk=invoice.json()["id"])
        self.assertIsNone(unrelated.broker_fk_id)
        self.assertFalse(self.commissions().filter(invoice=unrelated).exists())

    def test_test_data_stays_out_of_the_registers(self):
        invoice = self.invoice_from_lead()
        self.pay_customer(invoice, 5000)
        call_command("broker_commission_reconcile", "--flag-test", self.lead.lead_number, stdout=StringIO())
        commission = BrokerCommission.objects.get(invoice=invoice)
        self.assertTrue(commission.is_test_data)
        self.assertEqual(commission.commission_amount, Decimal("100.00"))  # kept as it was

        register = self.get("/sales/broker-commissions/").json()
        self.assertEqual(register.get("results", register.get("data")), [])
        self.assertEqual(self.statement(self.broker)["commissions"], [])
        self.assertEqual(Decimal(self.get("/sales/broker-commissions/analytics/").json()["totalAccrued"]), Decimal("0"))
        self.assertFalse(any(r["id"] == str(self.lead.id) for r in self.get("/sales/broker-commissions/leads/").json()))
        shown = self.get("/sales/broker-commissions/statement/", brokerId=str(self.broker.id), includeTestData="1").json()
        self.assertEqual(len(shown["commissions"]), 1)


@skipUnlessDBFeature("has_select_for_update")
class ConcurrentCommissionTests(TransactionTestCase):
    """Two requests processing one invoice at the same moment: one commission."""

    def test_concurrent_calculation_creates_one_commission(self):
        from apps.accounts.models import Client
        from apps.masters.models import Party

        tenant = Client.objects.create(slug="brk-concurrent", name="Broker Concurrent")
        broker = Broker.objects.create(
            client=tenant, code="BRK-C", name="Concurrent", commission_type="percent",
            default_value=Decimal("2"),
        )
        party = Party.objects.create(client=tenant, code="C-C", name="Buyer", type="Customer")
        invoice = SalesInvoice.objects.create(
            client=tenant, party=party, doc_date=timezone.localdate(), status="Unpaid",
            subtotal=Decimal("1000"), total=Decimal("1000"), taxable_value=Decimal("1000"),
            broker_fk=broker, broker=broker.name,
        )
        barrier = Barrier(2)

        def run():
            close_old_connections()
            try:
                barrier.wait()
                return broker_services.calculate_broker_commission(
                    SalesInvoice.objects.get(pk=invoice.pk)
                ).id
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            ids = [f.result() for f in [pool.submit(run), pool.submit(run)]]

        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(BrokerCommission.objects.filter(invoice=invoice).count(), 1)
