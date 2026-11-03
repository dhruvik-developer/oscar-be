"""Phase 1 -- sales & accounting fixes.

* invoice posting posts COGS at weighted average cost (not item.cost_price)
* a sales return reverses COGS for the returned qty
* purchase bill journal: stock vs non-stock vs freight accounts
* convert-to-invoice rules (SO: dispatched qty only; challan: dispatched, once)
* order stage migration and NON_RESERVING_STAGES
* ageing buckets 0-30 / 31-60 / 61-90 / 91-120 / 120+ on outstanding
* per-report permissions
"""
import importlib
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Barrier

from django.apps import apps as django_apps
from django.db import IntegrityError, close_old_connections, transaction
from django.test import TestCase, TransactionTestCase, skipUnlessDBFeature
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounting import services as ledger
from apps.accounting.models import JournalLine
from apps.accounting.reports import ageing
from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, User
from apps.core.money import AGEING_BUCKETS, ageing_bucket
from apps.inventory import services as stock
from apps.masters.models import Broker, Item, Location, Party

from .models import (
    NON_RESERVING_STAGES,
    ORDER_STAGES,
    DeliveryChallan,
    SalesInvoice,
    SalesInvoiceLine,
    SalesOrder,
    SalesOrderLine,
)


class Phase1Base(TestCase):
    def setUp(self):
        self.tenant, _ = Client.objects.get_or_create(
            slug="phase1-sales", defaults={"name": "Phase 1 Sales"}
        )
        self.user = User.objects.create_user(
            email="phase1-sales@example.com", password="pass", client=self.tenant,
            is_superuser=True,
        )
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(self.user)['access']}")
        ledger.seed_chart_of_accounts(self.tenant)

        self.location = Location.objects.create(
            client=self.tenant, code="WH-P1", name="Main", type="Warehouse"
        )
        # cost_price deliberately differs from the WAC so the test proves
        # which one COGS uses.
        self.fabric = Item.objects.create(
            client=self.tenant, sku="FAB-P1", name="Grey Fabric", item_kind="Standalone",
            uom="Mtr", cost_price=Decimal("999"),
        )
        self.customer = Party.objects.create(
            client=self.tenant, code="C-P1", name="Customer One", type="Customer"
        )
        self.vendor = Party.objects.create(
            client=self.tenant, code="V-P1", name="Vendor One", type="Vendor"
        )
        # 100 m @ 40 + 100 m @ 60 -> WAC 50.
        for cost in (40, 60):
            stock.post_movement(
                client_id=self.tenant.id, item=self.fabric, location=self.location,
                type="ADJUSTMENT", quantity=100, unit_cost=Decimal(cost), notes="opening",
            )

    def post(self, path, data=None):
        return self.api.post(f"/api/v1{path}", data or {}, format="json")

    def balance(self, system_key, source_id=None):
        lines = JournalLine.objects.filter(
            client_id=self.tenant.id, account__system_key=system_key
        ).exclude(journal_entry__status="Reversed")
        if source_id is not None:
            lines = lines.filter(journal_entry__source_document_id=source_id)
        return sum((line.debit - line.credit for line in lines), Decimal("0"))

    def order(self, qty=10, rate=100, extra_lines=()):
        response = self.post("/sales/orders/", {
            "partyId": str(self.customer.id),
            "date": str(timezone.localdate()),
            "lineItems": [
                {"itemId": str(self.fabric.id), "qty": qty, "rate": rate, "tax": 0},
                *extra_lines,
            ],
        })
        self.assertEqual(response.status_code, 201, response.content[:600])
        return response.json()

    def dispatch(self, order_id, lines=None):
        challan = self.post(f"/sales/orders/{order_id}/convert-to-challan/",
                            {"lines": lines} if lines else {})
        self.assertEqual(challan.status_code, 201, challan.content[:600])
        challan_id = challan.json()["id"]
        self.api.patch(f"/api/v1/sales/challans/{challan_id}/",
                       {"locationId": str(self.location.id)}, format="json")
        done = self.post(f"/sales/challans/{challan_id}/dispatch/")
        self.assertIn(done.status_code, (200, 201), done.content[:600])
        return challan_id


class CogsPostingTests(Phase1Base):
    def test_sales_order_broker_is_persisted_and_returned_after_refresh_and_update(self):
        first_broker = Broker.objects.create(
            client=self.tenant, code="BRK-SO-01", name="First Sales Broker"
        )
        second_broker = Broker.objects.create(
            client=self.tenant, code="BRK-SO-02", name="Second Sales Broker"
        )
        response = self.post("/sales/orders/", {
            "partyId": str(self.customer.id),
            "date": str(timezone.localdate()),
            "brokerId": str(first_broker.id),
            "lineItems": [
                {"itemId": str(self.fabric.id), "qty": 1, "rate": 100, "tax": 0},
            ],
        })

        self.assertEqual(response.status_code, 201, response.content[:600])
        order_data = response.json()
        order = SalesOrder.objects.get(pk=order_data["id"])
        self.assertEqual(order.broker_fk_id, first_broker.id)
        self.assertEqual(order_data["brokerId"], str(first_broker.id))
        self.assertEqual(order_data["brokerName"], first_broker.name)
        self.assertEqual(order_data["brokerCode"], first_broker.code)

        refreshed = self.api.get(f"/api/v1/sales/orders/{order.id}/")
        self.assertEqual(refreshed.status_code, 200, refreshed.content[:600])
        self.assertEqual(refreshed.json()["brokerName"], first_broker.name)
        self.assertEqual(refreshed.json()["brokerCode"], first_broker.code)

        changed = self.api.patch(
            f"/api/v1/sales/orders/{order.id}/",
            {"brokerId": str(second_broker.id)},
            format="json",
        )
        self.assertEqual(changed.status_code, 200, changed.content[:600])
        order.refresh_from_db()
        self.assertEqual(order.broker_fk_id, second_broker.id)
        self.assertEqual(changed.json()["brokerName"], second_broker.name)
        self.assertEqual(changed.json()["brokerCode"], second_broker.code)

    def test_sales_order_without_broker_returns_empty_broker_fields(self):
        order_data = self.order()

        self.assertIsNone(order_data["brokerId"])
        self.assertIsNone(order_data["brokerName"])
        self.assertIsNone(order_data["brokerCode"])

    def test_existing_sales_order_broker_is_returned_from_master(self):
        broker = Broker.objects.create(
            client=self.tenant, code="BRK-SO-EXISTING", name="Existing Sales Broker"
        )
        order = SalesOrder.objects.create(
            client=self.tenant,
            party=self.customer,
            doc_date=timezone.localdate(),
            broker_fk=broker,
            broker=broker.name,
        )

        response = self.api.get(f"/api/v1/sales/orders/{order.id}/")

        self.assertEqual(response.status_code, 200, response.content[:600])
        self.assertEqual(response.json()["brokerId"], str(broker.id))
        self.assertEqual(response.json()["brokerName"], broker.name)
        self.assertEqual(response.json()["brokerCode"], broker.code)

    def test_sales_order_rejects_zero_unit_rate(self):
        response = self.post("/sales/orders/", {
            "partyId": str(self.customer.id),
            "date": str(timezone.localdate()),
            "lineItems": [
                {"itemId": str(self.fabric.id), "qty": 1, "rate": 0, "tax": 0},
            ],
        })

        self.assertEqual(response.status_code, 400, response.content[:600])
        self.assertIn("Unit rate must be greater than zero", str(response.json()))
        self.assertFalse(SalesOrder.objects.filter(client=self.tenant).exists())

    def test_direct_invoice_posts_cogs_at_weighted_average_cost(self):
        created = self.post("/sales/invoices/", {
            "partyId": str(self.customer.id),
            "date": str(timezone.localdate()),
            "locationId": str(self.location.id),
            "lineItems": [{"itemId": str(self.fabric.id), "qty": 10, "rate": 100, "tax": 0}],
        })
        self.assertEqual(created.status_code, 201, created.content[:600])
        invoice_id = created.json()["id"]
        finalized = self.post(f"/sales/invoices/{invoice_id}/finalize/")
        self.assertEqual(finalized.status_code, 200, finalized.content[:800])

        # 10 m x WAC 50 = 500 (not 10 x cost_price 999).
        self.assertEqual(self.balance("cogs", invoice_id), Decimal("500.00"))
        self.assertEqual(self.balance("stock", invoice_id), Decimal("-500.00"))
        self.assertEqual(self.balance("debtors", invoice_id), Decimal("1000.00"))

    def test_challan_billed_invoice_posts_cogs_and_return_reverses_it(self):
        order = self.order(qty=10)
        challan_id = self.dispatch(order["id"])
        invoice = self.post(f"/sales/challans/{challan_id}/convert-to-invoice/")
        self.assertEqual(invoice.status_code, 201, invoice.content[:600])
        invoice_id = invoice.json()["id"]
        self.assertEqual(self.post(f"/sales/invoices/{invoice_id}/finalize/").status_code, 200)
        self.assertEqual(self.balance("cogs", invoice_id), Decimal("500.00"))

        line = SalesInvoice.objects.get(pk=invoice_id).line_items.get()
        sales_return = self.post("/sales/returns/", {
            "salesInvoiceId": invoice_id,
            "partyId": str(self.customer.id),
            "date": str(timezone.localdate()),
            "locationId": str(self.location.id),
            "lineItems": [{
                "itemId": str(self.fabric.id), "qty": 4, "returnedQty": 4, "rate": 100,
                "tax": 0, "salesInvoiceLineId": str(line.id),
            }],
        })
        self.assertEqual(sales_return.status_code, 201, sales_return.content[:800])
        return_id = sales_return.json()["id"]
        # 4 m back at the cost the sale went out at (50).
        self.assertEqual(self.balance("cogs", return_id), Decimal("-200.00"))
        self.assertEqual(self.balance("stock", return_id), Decimal("200.00"))


class PurchaseBillJournalTests(Phase1Base):
    def test_stock_non_stock_and_freight_accounts(self):
        from apps.purchase.models import PurchaseBill, PurchaseBillLine

        bill = PurchaseBill.objects.create(
            client=self.tenant, party=self.vendor, party_name=self.vendor.name,
            doc_date=timezone.localdate(), bill_number="PB-P1-1", status="Unpaid",
            subtotal=Decimal("1500"), taxable_value=Decimal("1500"), total_tax=Decimal("180"),
            freight_charges=Decimal("100"), other_charges=Decimal("20"),
            round_off=Decimal("0.40"), total=Decimal("1800.40"),
        )
        PurchaseBillLine.objects.create(
            client=self.tenant, purchase_bill=bill, line_no=1, item=self.fabric,
            qty=Decimal("20"), rate=Decimal("50"), amount=Decimal("1000"),
        )
        PurchaseBillLine.objects.create(
            # Every item kind holds stock now (masters 0025: ITEM_KINDS is Fabric
            # only, STOCKLESS_ITEM_KINDS is empty); a free-text line is non-stock.
            client=self.tenant, purchase_bill=bill, line_no=2, item=None, item_name="Stitching",
            qty=Decimal("1"), rate=Decimal("500"), amount=Decimal("500"),
        )
        entry = ledger.post_purchase_bill(bill)
        by_key = {
            line.account.system_key: line.debit - line.credit
            for line in entry.lines.select_related("account")
        }
        self.assertEqual(by_key["stock"], Decimal("1000.40"))      # stock + round-off
        self.assertEqual(by_key["purchases"], Decimal("500.00"))   # non-stock item
        self.assertEqual(by_key["freight"], Decimal("120.00"))     # freight + other
        self.assertEqual(by_key["gst_input"], Decimal("180.00"))
        self.assertEqual(by_key["creditors"], Decimal("-1800.40"))


class ConvertToInvoiceTests(Phase1Base):
    def test_order_invoices_only_dispatched_qty(self):
        order = self.order(qty=10)
        blocked = self.post(f"/sales/orders/{order['id']}/convert-to-invoice/")
        self.assertEqual(blocked.status_code, 422, blocked.content[:400])
        self.assertEqual(blocked.json()["code"], "NOT_DISPATCHED")

        line_id = order["lineItems"][0]["id"]
        self.dispatch(order["id"], lines=[{"lineId": line_id, "qty": 4}])
        self.assertEqual(SalesOrder.objects.get(pk=order["id"]).stage, "Partially Dispatched")

        too_much = self.post(f"/sales/orders/{order['id']}/convert-to-invoice/",
                             {"lines": [{"lineId": line_id, "qty": 5}]})
        self.assertEqual(too_much.status_code, 422, too_much.content[:400])

        invoice = self.post(f"/sales/orders/{order['id']}/convert-to-invoice/")
        self.assertEqual(invoice.status_code, 201, invoice.content[:400])
        self.assertEqual(Decimal(str(invoice.json()["lineItems"][0]["qty"])), Decimal("4"))

        repeated = self.post(f"/sales/orders/{order['id']}/convert-to-invoice/")
        self.assertEqual(repeated.status_code, 200, repeated.content[:400])
        self.assertEqual(repeated.json()["id"], invoice.json()["id"])
        direct = self.post("/sales/invoices/", {
            "salesOrder": order["id"],
            "partyId": str(self.customer.id),
            "date": str(timezone.localdate()),
        })
        self.assertEqual(direct.status_code, 200, direct.content[:400])
        self.assertEqual(direct.json()["id"], invoice.json()["id"])
        self.assertEqual(SalesOrder.objects.get(pk=order["id"]).invoices.count(), 1)

    def test_partial_draft_invoice_reserves_quantity_but_allows_remaining(self):
        order = self.order(qty=10)
        line_id = order["lineItems"][0]["id"]
        self.dispatch(order["id"], lines=[{"lineId": line_id, "qty": 10}])

        first = self.post(
            f"/sales/orders/{order['id']}/convert-to-invoice/",
            {"lines": [{"lineId": line_id, "qty": 4}]},
        )
        self.assertEqual(first.status_code, 201, first.content[:400])
        self.assertEqual(Decimal(str(first.json()["lineItems"][0]["qty"])), Decimal("4"))

        remaining = self.post(
            f"/sales/orders/{order['id']}/convert-to-invoice/",
            {"lines": [{"lineId": line_id, "qty": 6}]},
        )
        self.assertEqual(remaining.status_code, 200, remaining.content[:400])
        self.assertEqual(
            Decimal(str(remaining.json()["lineItems"][0]["qty"])), Decimal("4")
        )
        self.assertEqual(remaining.json()["id"], first.json()["id"])

        finalized = self.post(f"/sales/invoices/{first.json()['id']}/finalize/")
        self.assertEqual(finalized.status_code, 200, finalized.content[:600])
        self.assertEqual(finalized.json()["status"], "Unpaid")

        remaining = self.post(
            f"/sales/orders/{order['id']}/convert-to-invoice/",
            {"lines": [{"lineId": line_id, "qty": 6}]},
        )
        self.assertEqual(remaining.status_code, 201, remaining.content[:400])
        self.assertEqual(
            Decimal(str(remaining.json()["lineItems"][0]["qty"])), Decimal("6")
        )
        repeated = self.post(
            f"/sales/orders/{order['id']}/convert-to-invoice/",
            {"lines": [{"lineId": line_id, "qty": 1}]},
        )
        self.assertEqual(repeated.status_code, 200, repeated.content[:400])
        self.assertEqual(repeated.json()["id"], remaining.json()["id"])
        self.assertEqual(SalesOrder.objects.get(pk=order["id"]).invoices.count(), 2)

    def test_database_guard_blocks_a_second_active_draft(self):
        order = self.order(qty=2)
        first = SalesInvoice.objects.create(
            client=self.tenant,
            party=self.customer,
            party_name=self.customer.name,
            doc_date=timezone.localdate(),
            sales_order_id=order["id"],
            status="Draft",
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                SalesInvoice.objects.create(
                    client=self.tenant,
                    party=self.customer,
                    party_name=self.customer.name,
                    doc_date=timezone.localdate(),
                    sales_order_id=order["id"],
                    status="Draft",
                )
        first.refresh_from_db()
        self.assertEqual(first.status, "Draft")
        self.assertEqual(
            SalesInvoice.objects.filter(sales_order_id=order["id"], status="Draft").count(),
            1,
        )

    def test_draft_order_can_be_converted_to_invoice_directly(self):
        # 1. Create a Draft Sales Order
        response = self.post("/sales/orders/", {
            "partyId": str(self.customer.id),
            "date": str(timezone.localdate()),
            "stage": "Draft",
            "lineItems": [
                {"itemId": str(self.fabric.id), "qty": 5, "rate": 200, "tax": 0},
            ],
        })
        self.assertEqual(response.status_code, 201)
        order_data = response.json()
        order_id = order_data["id"]
        SalesOrder.objects.filter(pk=order_id).update(stage="Draft")

        # 2. Create its Sales Invoice
        res = self.post(f"/sales/orders/{order_id}/convert-to-invoice/")
        self.assertEqual(res.status_code, 201, res.content[:400])
        invoice_data = res.json()
        invoice_id = invoice_data["id"]

        # 3. Verify that the invoice is saved and linked to the correct SO
        inv = SalesInvoice.objects.get(pk=invoice_id)
        self.assertEqual(str(inv.sales_order_id), str(order_id))
        self.assertEqual(inv.status, "Draft")

        # A saved Draft does not advance the Sales Order lifecycle.
        order = SalesOrder.objects.get(pk=order_id)
        self.assertEqual(order.stage, "Draft")
        finalized = self.post(f"/sales/invoices/{invoice_id}/finalize/")
        self.assertEqual(finalized.status_code, 200, finalized.content[:600])
        self.assertEqual(finalized.json()["status"], "Unpaid")

        # Only successful finalization advances the Sales Order lifecycle.
        order.refresh_from_db()
        self.assertEqual(order.stage, "Invoiced")
        self.assertEqual(order.payment_status, "Unpaid")

        # 5. Verify the API returns the updated Sales Order and invoice status
        order_res = self.api.get(f"/api/v1/sales/orders/{order_id}/")
        self.assertEqual(order_res.status_code, 200)
        so_data = order_res.json()
        self.assertEqual(so_data["stage"], "Invoiced")
        self.assertEqual(so_data["paymentStatus"], "Unpaid")
        self.assertTrue(so_data["hasInvoice"])
        self.assertEqual(so_data["invoiceStatus"], "Invoiced")
        self.assertEqual(so_data["linkedInvoiceId"], str(invoice_id))

        # 6. Test repeated clicks and duplicate invoice prevention
        duplicate = self.post(f"/sales/orders/{order_id}/convert-to-invoice/")
        self.assertEqual(duplicate.status_code, 409, duplicate.content[:400])
        self.assertEqual(order.invoices.count(), 1)

    def test_non_stock_lines_need_no_dispatch(self):
        order = self.order(qty=10, extra_lines=[
            {"description": "Installation fee", "qty": 1, "rate": 300, "tax": 0},
        ])
        invoice = self.post(f"/sales/orders/{order['id']}/convert-to-invoice/")
        self.assertEqual(invoice.status_code, 201, invoice.content[:400])
        rows = invoice.json()["lineItems"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["description"], "Installation fee")

    def test_challan_must_be_dispatched_and_billed_once(self):
        order = self.order(qty=10)
        draft = self.post(f"/sales/orders/{order['id']}/convert-to-challan/")
        draft_id = draft.json()["id"]
        refused = self.post(f"/sales/challans/{draft_id}/convert-to-invoice/")
        self.assertEqual(refused.status_code, 422, refused.content[:400])
        self.post(f"/sales/challans/{draft_id}/cancel/", {"reason": "redo"})

        challan_id = self.dispatch(order["id"])
        # In Transit / Delivered challans are still billable.
        DeliveryChallan.objects.filter(pk=challan_id).update(status="Delivered")
        first = self.post(f"/sales/challans/{challan_id}/convert-to-invoice/")
        self.assertEqual(first.status_code, 201, first.content[:400])
        second = self.post(f"/sales/challans/{challan_id}/convert-to-invoice/")
        self.assertEqual(second.status_code, 200, second.content[:400])
        self.assertEqual(second.json()["id"], first.json()["id"])
        finalized = self.post(f"/sales/invoices/{first.json()['id']}/finalize/")
        self.assertEqual(finalized.status_code, 200, finalized.content[:600])
        repeated_after_finalize = self.post(
            f"/sales/challans/{challan_id}/convert-to-invoice/"
        )
        self.assertEqual(repeated_after_finalize.status_code, 200, repeated_after_finalize.content[:400])
        self.assertEqual(repeated_after_finalize.json()["id"], first.json()["id"])

    def test_pod_confirmation_persists_delivery_status(self):
        order = self.order(qty=10)
        challan_id = self.dispatch(order["id"])

        confirmed = self.post(f"/sales/challans/{challan_id}/confirm-pod/")
        self.assertEqual(confirmed.status_code, 200, confirmed.content[:600])
        self.assertEqual(confirmed.json()["status"], "Delivered")
        self.assertIsNotNone(confirmed.json()["delivered_at"])

        challan = DeliveryChallan.objects.get(pk=challan_id)
        self.assertEqual(challan.status, "Delivered")
        self.assertIsNotNone(challan.delivered_at)

        repeated = self.post(f"/sales/challans/{challan_id}/confirm-pod/")
        self.assertEqual(repeated.status_code, 200, repeated.content[:600])
        self.assertEqual(repeated.json()["status"], "Delivered")

    def test_one_active_challan_per_sales_order(self):
        order = self.order(qty=10)
        line_id = order["lineItems"][0]["id"]
        first_challan_id = self.dispatch(
            order["id"], lines=[{"lineId": line_id, "qty": 4}]
        )

        duplicate = self.post(
            f"/sales/orders/{order['id']}/convert-to-challan/",
            {"lines": [{"lineId": line_id, "qty": 6}]},
        )
        self.assertEqual(duplicate.status_code, 409, duplicate.content[:600])
        self.assertEqual(
            DeliveryChallan.objects.filter(sales_order_id=order["id"])
            .exclude(status__in=["Delivered", "Cancelled"])
            .count(),
            1,
        )
        direct_duplicate = self.api.post(
            "/api/v1/sales/challans/",
            {
                "partyId": str(self.customer.id),
                "salesOrder": order["id"],
                "date": str(timezone.localdate()),
                "lineItems": [{
                    "itemId": str(self.fabric.id),
                    "qty": 6,
                    "rate": 100,
                    "tax": 0,
                }],
            },
            format="json",
        )
        self.assertEqual(direct_duplicate.status_code, 409, direct_duplicate.content[:600])

        pod = self.post(f"/sales/challans/{first_challan_id}/confirm-pod/")
        self.assertEqual(pod.status_code, 200, pod.content[:600])
        next_challan = self.post(f"/sales/orders/{order['id']}/convert-to-challan/")
        self.assertEqual(next_challan.status_code, 201, next_challan.content[:600])
        self.assertEqual(
            Decimal(str(next_challan.json()["lineItems"][0]["qty"])),
            Decimal("6"),
        )


    def test_draft_invoice_from_order_challan_is_reused_and_waits_for_dispatch(self):
        order = self.order(qty=5)
        challan_response = self.post(
            f"/sales/orders/{order['id']}/convert-to-challan/"
        )
        self.assertEqual(challan_response.status_code, 201, challan_response.content[:600])
        challan_id = challan_response.json()["id"]

        repeated_challan = self.post(
            f"/sales/orders/{order['id']}/convert-to-challan/"
        )
        self.assertEqual(repeated_challan.status_code, 200, repeated_challan.content[:600])
        self.assertEqual(repeated_challan.json()["id"], challan_id)

        created = self.post(
            f"/sales/challans/{challan_id}/create-draft-invoice/"
        )
        self.assertEqual(created.status_code, 201, created.content[:600])
        invoice_id = created.json()["id"]
        invoice = SalesInvoice.objects.get(pk=invoice_id)
        self.assertEqual(invoice.sales_order_id, SalesOrder.objects.get(pk=order["id"]).id)
        self.assertEqual(str(invoice.delivery_challan_id), str(challan_id))
        self.assertEqual(invoice.status, "Draft")
        self.assertIsNone(invoice.posted_at)

        repeated_invoice = self.post(
            f"/sales/challans/{challan_id}/create-draft-invoice/"
        )
        self.assertEqual(repeated_invoice.status_code, 200, repeated_invoice.content[:600])
        self.assertEqual(repeated_invoice.json()["id"], invoice_id)
        self.assertTrue(repeated_invoice.json()["reused"])
        self.assertEqual(
            DeliveryChallan.objects.filter(sales_order_id=order["id"], status="Draft").count(),
            1,
        )
        self.assertEqual(
            SalesInvoice.objects.filter(
                sales_order_id=order["id"], status="Draft", posted_at__isnull=True
            ).count(),
            1,
        )

        blocked = self.post(f"/sales/invoices/{invoice_id}/finalize/")
        self.assertEqual(blocked.status_code, 422, blocked.content[:600])
        self.assertEqual(blocked.json()["code"], "NOT_DISPATCHED")
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, "Draft")
        self.assertIsNone(invoice.posted_at)

        self.api.patch(
            f"/api/v1/sales/challans/{challan_id}/",
            {"locationId": str(self.location.id)},
            format="json",
        )
        dispatched = self.post(f"/sales/challans/{challan_id}/dispatch/")
        self.assertIn(dispatched.status_code, (200, 201), dispatched.content[:600])
        finalized = self.post(f"/sales/invoices/{invoice_id}/finalize/")
        self.assertEqual(finalized.status_code, 200, finalized.content[:600])
        self.assertEqual(finalized.json()["id"], invoice_id)
        self.assertEqual(finalized.json()["status"], "Unpaid")
        invoice.refresh_from_db()
        self.assertEqual(str(invoice.delivery_challan_id), str(challan_id))
        self.assertIsNotNone(invoice.posted_at)

    def test_challan_invoice_creation_reuses_existing_order_draft(self):
        order = self.order(qty=5)
        challan = self.post(f"/sales/orders/{order['id']}/convert-to-challan/")
        self.assertEqual(challan.status_code, 201, challan.content[:600])
        existing = SalesInvoice.objects.create(
            client=self.tenant,
            party=self.customer,
            party_name=self.customer.name,
            doc_date=timezone.localdate(),
            sales_order_id=order["id"],
            status="Draft",
        )

        response = self.post(
            f"/sales/challans/{challan.json()['id']}/create-draft-invoice/"
        )

        self.assertEqual(response.status_code, 200, response.content[:600])
        self.assertEqual(response.json()["id"], str(existing.id))
        self.assertTrue(response.json()["reused"])
        existing.refresh_from_db()
        self.assertEqual(str(existing.delivery_challan_id), str(challan.json()["id"]))
        self.assertEqual(existing.line_items.count(), 1)
        self.assertEqual(
            str(existing.line_items.get().delivery_challan_line.delivery_challan_id),
            str(challan.json()["id"]),
        )
        self.assertEqual(
            SalesInvoice.objects.filter(
                sales_order_id=order["id"], status="Draft", posted_at__isnull=True
            ).count(),
            1,
        )

    def test_incompatible_order_draft_does_not_duplicate_challan_or_invoice(self):
        order = self.order(qty=5)
        challan = self.post(f"/sales/orders/{order['id']}/convert-to-challan/")
        self.assertEqual(challan.status_code, 201, challan.content[:600])
        existing = SalesInvoice.objects.create(
            client=self.tenant,
            party=self.customer,
            party_name=self.customer.name,
            doc_date=timezone.localdate(),
            sales_order_id=order["id"],
            status="Draft",
        )
        SalesInvoiceLine.objects.create(
            client=self.tenant,
            sales_invoice=existing,
            sales_order_line_id=order["lineItems"][0]["id"],
            item=self.fabric,
            item_name=self.fabric.name,
            qty=Decimal("4"),
            rate=Decimal("100"),
        )

        rejected = self.post(
            f"/sales/challans/{challan.json()['id']}/create-draft-invoice/"
        )
        self.assertEqual(rejected.status_code, 409, rejected.content[:600])
        self.assertEqual(
            DeliveryChallan.objects.filter(sales_order_id=order["id"], status="Draft").count(),
            1,
        )
        self.assertEqual(
            SalesInvoice.objects.filter(
                sales_order_id=order["id"], status="Draft", posted_at__isnull=True
            ).count(),
            1,
        )

        existing.status = "Cancelled"
        existing.save(update_fields=["status", "updated_at"])
        retried = self.post(
            f"/sales/challans/{challan.json()['id']}/create-draft-invoice/"
        )
        self.assertEqual(retried.status_code, 201, retried.content[:600])
        self.assertEqual(
            DeliveryChallan.objects.filter(sales_order_id=order["id"], status="Draft").count(),
            1,
        )

    def test_partial_fulfilments_can_create_later_invoice_drafts(self):
        order = self.order(qty=10)
        line_id = order["lineItems"][0]["id"]
        first_challan_id = self.dispatch(
            order["id"], lines=[{"lineId": line_id, "qty": 4}]
        )
        first_invoice = self.post(
            f"/sales/challans/{first_challan_id}/convert-to-invoice/"
        )
        self.assertEqual(first_invoice.status_code, 201, first_invoice.content[:600])
        finalized = self.post(
            f"/sales/invoices/{first_invoice.json()['id']}/finalize/"
        )
        self.assertEqual(finalized.status_code, 200, finalized.content[:600])
        pod = self.post(f"/sales/challans/{first_challan_id}/confirm-pod/")
        self.assertEqual(pod.status_code, 200, pod.content[:600])

        second_challan = self.post(
            f"/sales/orders/{order['id']}/convert-to-challan/"
        )
        self.assertEqual(second_challan.status_code, 201, second_challan.content[:600])
        second_invoice = self.post(
            f"/sales/challans/{second_challan.json()['id']}/create-draft-invoice/"
        )
        self.assertEqual(second_invoice.status_code, 201, second_invoice.content[:600])
        self.assertNotEqual(second_invoice.json()["id"], first_invoice.json()["id"])
        self.assertEqual(
            Decimal(str(second_invoice.json()["lineItems"][0]["qty"])),
            Decimal("6"),
        )
        self.assertEqual(
            SalesInvoice.objects.filter(
                sales_order_id=order["id"], status="Unpaid", posted_at__isnull=False
            ).count(),
            1,
        )
        self.assertEqual(
            SalesInvoice.objects.filter(
                sales_order_id=order["id"], status="Draft", posted_at__isnull=True
            ).count(),
            1,
        )


@skipUnlessDBFeature("has_select_for_update")
class CombinedWorkflowConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        Phase1Base.setUp(self)

    def post(self, path, data=None):
        return self.api.post(f"/api/v1{path}", data or {}, format="json")

    def _concurrent_posts(self, path):
        barrier = Barrier(2)

        def send_request(_):
            close_old_connections()
            client = APIClient()
            client.credentials(
                HTTP_AUTHORIZATION=f"Bearer {build_tokens(self.user)['access']}"
            )
            try:
                barrier.wait(timeout=10)
                response = client.post(f"/api/v1{path}", {}, format="json")
                return response.status_code, response.json()
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            return list(pool.map(send_request, range(2)))

    def test_simultaneous_combined_requests_reuse_both_documents(self):
        order = Phase1Base.order(self, qty=5)

        challan_responses = self._concurrent_posts(
            f"/sales/orders/{order['id']}/convert-to-challan/"
        )
        self.assertCountEqual(
            [status_code for status_code, _ in challan_responses], [200, 201]
        )
        challan_ids = {body["id"] for _, body in challan_responses}
        self.assertEqual(len(challan_ids), 1)
        challan_id = challan_ids.pop()

        invoice_responses = self._concurrent_posts(
            f"/sales/challans/{challan_id}/create-draft-invoice/"
        )
        self.assertCountEqual(
            [status_code for status_code, _ in invoice_responses], [200, 201]
        )
        invoice_ids = {body["id"] for _, body in invoice_responses}
        self.assertEqual(len(invoice_ids), 1)
        self.assertEqual(
            DeliveryChallan.objects.filter(sales_order_id=order["id"]).count(), 1
        )
        self.assertEqual(
            SalesInvoice.objects.filter(
                sales_order_id=order["id"], status="Draft", posted_at__isnull=True
            ).count(),
            1,
        )


class CreditLimitApprovalTests(Phase1Base):
    def test_create_and_finalize_failure_returns_the_saved_draft_and_error(self):
        self.customer.credit_limit = Decimal("1")
        self.customer.save(update_fields=["credit_limit", "updated_at"])
        created = self.post("/sales/invoices/", {
            "partyId": str(self.customer.id),
            "date": str(timezone.localdate()),
            "locationId": str(self.location.id),
            "finalize": True,
            "lineItems": [{"itemId": str(self.fabric.id), "qty": 1, "rate": 100, "tax": 0}],
        })
        self.assertEqual(created.status_code, 201, created.content[:800])
        self.assertEqual(created.json()["finalizationError"]["code"], "CREDIT_LIMIT_EXCEEDED")
        saved = SalesInvoice.objects.get(pk=created.json()["id"])
        self.assertEqual(saved.status, "Draft")
        self.assertIsNone(saved.posted_at)

    def test_credit_limit_error_preserves_draft_and_approval_finalizes_same_invoice(self):
        self.customer.credit_limit = Decimal("50000")
        self.customer.balance = Decimal("11300")
        self.customer.save(update_fields=["credit_limit", "balance", "updated_at"])
        stock.post_movement(
            client_id=self.tenant.id,
            item=self.fabric,
            location=self.location,
            type="ADJUSTMENT",
            quantity=500,
            unit_cost=Decimal("40"),
            notes="credit approval test stock",
        )
        created = self.post("/sales/invoices/", {
            "partyId": str(self.customer.id),
            "date": str(timezone.localdate()),
            "locationId": str(self.location.id),
            "lineItems": [{"itemId": str(self.fabric.id), "qty": 500, "rate": 100, "tax": 0}],
        })
        self.assertEqual(created.status_code, 201, created.content[:600])
        invoice_id = created.json()["id"]

        failed = self.post(
            f"/sales/invoices/{invoice_id}/finalize/",
            {"overrideCreditLimit": True},
        )
        self.assertEqual(failed.status_code, 422, failed.content[:800])
        payload = failed.json()["payload"]
        self.assertEqual(payload["customerName"], self.customer.name)
        self.assertEqual(payload["creditLimit"], "50000.00")
        self.assertEqual(payload["currentOutstanding"], "11300.00")
        self.assertEqual(payload["currentInvoiceAmount"], "50000.00")
        self.assertEqual(payload["projectedExposure"], "61300.00")
        self.assertEqual(payload["excess"], "11300.00")
        self.assertEqual(SalesInvoice.objects.get(pk=invoice_id).status, "Draft")

        requested = self.post(
            f"/sales/invoices/{invoice_id}/request-credit-override/",
            {"reason": "Approved customer project; verified payment schedule."},
        )
        self.assertEqual(requested.status_code, 202, requested.content[:800])
        from apps.core.models import ApprovalRequest

        approval = ApprovalRequest.objects.get(pk=requested.json()["approvalRequestId"])
        self.assertEqual(approval.document_type, "credit_limit_exceeded")
        self.assertEqual(approval.reason, "Approved customer project; verified payment schedule.")
        self.assertEqual(approval.new_values["projectedExposure"], "61300.00")

        approved = self.post(
            f"/approvals/requests/{approval.id}/approve/",
            {"comments": "Credit override approved after review."},
        )
        self.assertEqual(approved.status_code, 200, approved.content[:800])
        approval.refresh_from_db()
        invoice = SalesInvoice.objects.get(pk=invoice_id)
        self.assertEqual(approval.status, "Approved")
        self.assertEqual(approval.decided_by_id, self.user.id)
        self.assertIsNotNone(approval.decided_at)
        self.assertEqual(invoice.status, "Unpaid")
        self.assertIsNotNone(invoice.posted_at)

    def test_unauthorized_user_cannot_approve_credit_override(self):
        self.customer.credit_limit = Decimal("1")
        self.customer.save(update_fields=["credit_limit", "updated_at"])
        created = self.post("/sales/invoices/", {
            "partyId": str(self.customer.id),
            "date": str(timezone.localdate()),
            "locationId": str(self.location.id),
            "lineItems": [{"itemId": str(self.fabric.id), "qty": 1, "rate": 100, "tax": 0}],
        })
        self.assertEqual(created.status_code, 201, created.content[:600])
        invoice_id = created.json()["id"]
        requested = self.post(
            f"/sales/invoices/{invoice_id}/request-credit-override/",
            {"reason": "Customer payment arrangement."},
        )
        self.assertEqual(requested.status_code, 202, requested.content[:600])

        outsider = User.objects.create_user(
            email="credit-outsider@example.com",
            password="test-pass-123",
            client=self.tenant,
        )
        outsider_api = APIClient()
        outsider_api.force_authenticate(outsider)
        denied = outsider_api.post(
            f"/api/v1/approvals/requests/{requested.json()['approvalRequestId']}/approve/",
            {"comments": "Not authorized."},
            format="json",
        )
        self.assertEqual(denied.status_code, 403, denied.content[:600])
        self.assertEqual(SalesInvoice.objects.get(pk=invoice_id).status, "Draft")


class OrderStageTests(Phase1Base):
    def test_non_reserving_stages(self):
        self.assertEqual(
            set(NON_RESERVING_STAGES),
            {"Draft", "Pending Approval", "Fully Dispatched", "Invoiced", "Closed", "Cancelled"},
        )
        self.assertTrue(set(NON_RESERVING_STAGES) <= set(ORDER_STAGES))

        order = self.order(qty=10)
        SalesOrder.objects.filter(pk=order["id"]).update(stage="Draft")
        reserved = stock.reserved_quantity(self.tenant.id, [self.fabric.id])
        self.assertEqual(reserved.get(self.fabric.id, 0), 0)  # Draft reserves nothing

        SalesOrder.objects.filter(pk=order["id"]).update(stage="Confirmed")
        reserved = stock.reserved_quantity(self.tenant.id, [self.fabric.id])
        self.assertEqual(reserved[self.fabric.id], Decimal("10"))

        self.dispatch(order["id"])
        self.assertEqual(SalesOrder.objects.get(pk=order["id"]).stage, "Fully Dispatched")
        reserved = stock.reserved_quantity(self.tenant.id, [self.fabric.id])
        self.assertEqual(reserved.get(self.fabric.id, 0), 0)

    def test_stage_migration_maps_legacy_values(self):
        migration = importlib.import_module("apps.sales.migrations.0006_unify_order_stages")
        full = self.order(qty=10)
        part = self.order(qty=10)
        none = self.order(qty=10)
        SalesOrderLine.objects.filter(sales_order_id=full["id"]).update(dispatched_qty=10)
        SalesOrderLine.objects.filter(sales_order_id=part["id"]).update(dispatched_qty=3)
        SalesOrder.objects.filter(pk=full["id"]).update(stage="Delivered")
        SalesOrder.objects.filter(pk=part["id"]).update(stage="Dispatched")
        SalesOrder.objects.filter(pk=none["id"]).update(stage="Delivered")

        migration.forwards(django_apps, None)

        stages = dict(SalesOrder.objects.filter(
            pk__in=[full["id"], part["id"], none["id"]]
        ).values_list("id", "stage"))
        self.assertEqual(stages[SalesOrder.objects.get(pk=full["id"]).id], "Fully Dispatched")
        self.assertEqual(stages[SalesOrder.objects.get(pk=part["id"]).id], "Partially Dispatched")
        self.assertEqual(stages[SalesOrder.objects.get(pk=none["id"]).id], "Fully Dispatched")
        self.assertFalse(
            SalesOrder.objects.exclude(stage__in=ORDER_STAGES).exists()
        )


class AgeingTests(Phase1Base):
    def test_bucket_labels(self):
        self.assertEqual(AGEING_BUCKETS, ["0-30", "31-60", "61-90", "91-120", "120+"])
        cases = {-5: "0-30", 0: "0-30", 30: "0-30", 31: "31-60", 60: "31-60",
                 61: "61-90", 90: "61-90", 91: "91-120", 120: "91-120", 121: "120+"}
        for days, bucket in cases.items():
            self.assertEqual(ageing_bucket(days), bucket, days)

    def _invoice(self, number, doc_date, total, paid=0, due_date=None, credit_days=0):
        return SalesInvoice.objects.create(
            client=self.tenant, party=self.customer, party_name=self.customer.name,
            invoice_number=number, doc_date=doc_date, due_date=due_date,
            credit_days=credit_days, status="Unpaid", posted_at=timezone.now(),
            subtotal=Decimal(total), taxable_value=Decimal(total), total=Decimal(total),
            amount_paid=Decimal(paid),
        )

    def test_report_uses_outstanding_and_credit_days(self):
        today = timezone.localdate()
        # No due date: invoice date + 30 credit days -> 70 - 30 = 40 days overdue.
        self._invoice("INV-A", today - timedelta(days=70), 1000, paid=400, credit_days=30)
        # Due date given: 130 days overdue.
        self._invoice("INV-B", today - timedelta(days=140), 500,
                      due_date=today - timedelta(days=130))
        # Fully paid: excluded.
        self._invoice("INV-C", today - timedelta(days=200), 300, paid=300)
        # Not yet due.
        self._invoice("INV-D", today, 200, due_date=today + timedelta(days=15))

        report = ageing(self.tenant.id, "receivable", today)
        self.assertEqual(list(report["buckets"]), AGEING_BUCKETS)
        self.assertEqual(report["buckets"]["31-60"], Decimal("600.00"))
        self.assertEqual(report["buckets"]["120+"], Decimal("500.00"))
        self.assertEqual(report["buckets"]["0-30"], Decimal("200.00"))
        self.assertEqual(report["total"], Decimal("1300.00"))
        self.assertNotIn("INV-C", [row["documentNumber"] for row in report["rows"]])


class ReportPermissionTests(Phase1Base):
    def setUp(self):
        super().setUp()
        from apps.accounts.models import Permission, Role, RolePermission
        from apps.accounts.permission_catalogue import sync_permissions

        sync_permissions()
        role = Role.objects.create(client=self.tenant, code="P1CLERK", name="Phase1 Sales Clerk")
        RolePermission.objects.create(role=role, permission=Permission.objects.get(pk="view_sales"))
        self.clerk = User.objects.create_user(
            email="phase1-clerk@example.com", password="pass", client=self.tenant, role=role,
        )
        self.clerk_api = APIClient()
        self.clerk_api.credentials(
            HTTP_AUTHORIZATION=f"Bearer {build_tokens(self.clerk)['access']}"
        )

    def test_financial_reports_need_financial_permission(self):
        self.assertEqual(self.clerk.effective_permissions(), {"view_sales"})
        for key in ("profit-and-loss", "balance-sheet", "gst-summary", "ar-ap-ageing"):
            response = self.clerk_api.get(f"/api/v1/reports/{key}/")
            self.assertEqual(response.status_code, 403, (key, response.content[:200]))
            response = self.clerk_api.get(f"/api/v1/accounts/reports/{key if key != 'ar-ap-ageing' else 'ageing'}/")
            self.assertEqual(response.status_code, 403, (key, response.content[:200]))
        ok = self.clerk_api.get("/api/v1/reports/sales-register/")
        self.assertEqual(ok.status_code, 200, ok.content[:200])
        catalogue = self.clerk_api.get("/api/v1/reports/").json()["results"]
        keys = {row["key"] for row in catalogue}
        self.assertIn("sales-register", keys)
        self.assertNotIn("profit-and-loss", keys)

    def test_sales_dashboard_endpoint(self):
        response = self.api.get("/api/v1/reports/sales-dashboard/")
        self.assertEqual(response.status_code, 200, response.content[:600])
        body = response.json()
        self.assertIn("kpis", body)
        self.assertEqual(body["receivableAgeing"]["bucketOrder"], AGEING_BUCKETS)
        # 200 m on hand at WAC 50.
        self.assertEqual(Decimal(str(body["stockValue"])), Decimal("10000.00"))


class LotTrackedDispatchTests(Phase1Base):
    def setUp(self):
        super().setUp()
        from apps.inventory.models import LotTaka

        self.taka_item = Item.objects.create(
            client=self.tenant, sku="FAB-TAKA", name="Taka Fabric", item_kind="Fabric",
            uom="Mtr", tracking_type="TAKA",
        )
        today = timezone.localdate()
        self.takas = []
        for number, qty, rate, age in (("1", 30, 40, 10), ("2", 50, 60, 2)):
            taka = LotTaka.objects.create(
                client=self.tenant, lot_number="LOT-P1", taka_number=number,
                taka_id=f"TK-P1-{number}", item=self.taka_item, location=self.location,
                received_date=today - timedelta(days=age), received_quantity=qty,
                available_quantity=0, rate=Decimal(rate), qc_status="Passed",
            )
            stock.post_movement(
                client_id=self.tenant.id, item=self.taka_item, location=self.location,
                type="ADJUSTMENT", quantity=qty, unit_cost=Decimal(rate),
                notes="opening", lot_taka=taka,
            )
            self.takas.append(taka)

    def _taka_order(self, qty):
        response = self.post("/sales/orders/", {
            "partyId": str(self.customer.id),
            "date": str(timezone.localdate()),
            "lineItems": [{"itemId": str(self.taka_item.id), "qty": qty, "rate": 100, "tax": 0}],
        })
        self.assertEqual(response.status_code, 201, response.content[:600])
        return response.json()

    def test_fifo_dispatch_draws_oldest_takas_and_costs_at_lot_rate(self):
        order = self._taka_order(40)
        challan_id = self.dispatch(order["id"])
        for taka in self.takas:
            taka.refresh_from_db()
        self.assertEqual(self.takas[0].available_quantity, Decimal("0"))
        self.assertEqual(self.takas[1].available_quantity, Decimal("40"))

        invoice = self.post(f"/sales/challans/{challan_id}/convert-to-invoice/").json()
        self.assertEqual(self.post(f"/sales/invoices/{invoice['id']}/finalize/").status_code, 200)
        # 30 m @ 40 + 10 m @ 60 = 1800.
        self.assertEqual(self.balance("cogs", invoice["id"]), Decimal("1800.00"))

        # Cancelling the invoice first, then the challan, puts the takas back.
        self.post(f"/sales/invoices/{invoice['id']}/cancel/", {"reason": "test"})
        self.post(f"/sales/challans/{challan_id}/cancel/", {"reason": "test"})
        for taka in self.takas:
            taka.refresh_from_db()
        self.assertEqual(self.takas[0].available_quantity, Decimal("30"))
        self.assertEqual(self.takas[1].available_quantity, Decimal("50"))

    def test_selected_takas_are_used(self):
        order = self._taka_order(20)
        line_id = order["lineItems"][0]["id"]
        self.dispatch(order["id"], lines=[{
            "lineId": line_id, "qty": 20,
            "takaAllocations": [{"lotTakaId": str(self.takas[1].id), "qty": 20}],
        }])
        for taka in self.takas:
            taka.refresh_from_db()
        self.assertEqual(self.takas[0].available_quantity, Decimal("30"))
        self.assertEqual(self.takas[1].available_quantity, Decimal("30"))

    def _reserved_order(self, qty, taka):
        response = self.post("/sales/orders/", {
            "partyId": str(self.customer.id),
            "date": str(timezone.localdate()),
            "lineItems": [{
                "itemId": str(self.taka_item.id), "qty": qty, "rate": 100, "tax": 0,
                "reservedTakas": [{"lotTakaId": str(taka.id), "qty": qty}],
            }],
        })
        self.assertEqual(response.status_code, 201, response.content[:600])
        return response.json()

    def test_fifo_skips_takas_reserved_for_another_order(self):
        # Order A reserves the oldest taka; a FIFO sale for order B must not take it.
        self._reserved_order(30, self.takas[0])
        self.takas[0].refresh_from_db()
        self.assertEqual(self.takas[0].status, "Reserved")
        order_b = self._taka_order(20)
        self.dispatch(order_b["id"])
        for taka in self.takas:
            taka.refresh_from_db()
        self.assertEqual(self.takas[0].available_quantity, Decimal("30"))
        self.assertEqual(self.takas[1].available_quantity, Decimal("30"))

    def test_own_reservation_is_drawn_first_and_released(self):
        order = self._reserved_order(20, self.takas[1])
        self.dispatch(order["id"])
        for taka in self.takas:
            taka.refresh_from_db()
        self.assertEqual(self.takas[0].available_quantity, Decimal("30"))
        self.assertEqual(self.takas[1].available_quantity, Decimal("30"))
        self.assertEqual(self.takas[1].status, "Available")

    def test_selecting_another_orders_reserved_taka_is_refused(self):
        self._reserved_order(30, self.takas[0])
        order_b = self._taka_order(10)
        line_id = order_b["lineItems"][0]["id"]
        challan = self.post(f"/sales/orders/{order_b['id']}/convert-to-challan/", {"lines": [{
            "lineId": line_id, "qty": 10,
            "takaAllocations": [{"lotTakaId": str(self.takas[0].id), "qty": 10}],
        }]}).json()
        self.api.patch(f"/api/v1/sales/challans/{challan['id']}/",
                       {"locationId": str(self.location.id)}, format="json")
        refused = self.post(f"/sales/challans/{challan['id']}/dispatch/")
        self.assertEqual(refused.status_code, 422, refused.content[:400])

    def test_eligible_stock_is_grouped_by_batch(self):
        self._reserved_order(30, self.takas[0])
        rows = self.api.get(
            f"/api/v1/inventory/batches/eligible/?itemId={self.taka_item.id}"
        ).json()["results"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["batchCode"], self.takas[1].batch.batch_code)
        self.assertEqual([t["takaId"] for t in rows[0]["takas"]], ["TK-P1-2"])

    def test_insufficient_taka_stock_is_refused(self):
        order = self._taka_order(90)
        challan = self.post(f"/sales/orders/{order['id']}/convert-to-challan/").json()
        self.api.patch(f"/api/v1/sales/challans/{challan['id']}/",
                       {"locationId": str(self.location.id)}, format="json")
        refused = self.post(f"/sales/challans/{challan['id']}/dispatch/")
        self.assertEqual(refused.status_code, 422, refused.content[:400])


class PartyProfitabilityTests(Phase1Base):
    """The party report uses the ledger's actual COGS, never a fixed ratio."""

    def invoice(self, qty, rate):
        created = self.post("/sales/invoices/", {
            "partyId": str(self.customer.id), "date": str(timezone.localdate()),
            "locationId": str(self.location.id),
            "lineItems": [{"itemId": str(self.fabric.id), "qty": qty, "rate": rate, "tax": 5}],
        })
        self.assertEqual(created.status_code, 201, created.content[:600])
        self.assertEqual(self.post(f"/sales/invoices/{created.json()['id']}/finalize/").status_code, 200)
        return created.json()["id"]

    def test_cogs_is_actual_and_sales_are_net_of_gst(self):
        from apps.reports.mis_reports import party_profitability_report

        self.invoice(10, 100)     # 1,000 + 5% GST; cost 10 x WAC 50 = 500
        self.invoice(20, 80)      # 1,600; cost 20 x 50 = 1,000
        row = party_profitability_report(self.tenant.id)["rows"][0]
        self.assertEqual(row["sales"], 2600.0)
        self.assertEqual(row["salesInclTax"], 2730.0)
        self.assertEqual(row["cogs"], 1500.0)                 # not 72% of sales
        self.assertEqual(row["cogs"], float(self.balance("cogs")))
        self.assertEqual(row["grossProfit"], 1100.0)
        self.assertEqual(row["costStatus"], "actual")

    def test_missing_cost_is_flagged_not_guessed(self):
        from apps.reports.mis_reports import party_profitability_report

        invoice_id = self.invoice(10, 100)
        # Simulate an invoice whose COGS never reached the ledger.
        JournalLine.objects.filter(journal_entry__source_document_id=invoice_id, account__system_key="cogs").delete()
        row = party_profitability_report(self.tenant.id)["rows"][0]
        self.assertEqual(row["cogs"], 0.0)
        self.assertEqual(row["costStatus"], "incomplete")
        self.assertEqual(len(row["invoicesWithoutActualCost"]), 1)

    def test_salesperson_report_uses_actual_figures(self):
        from apps.reports.mis_reports import salesperson_profitability_report

        self.invoice(10, 100)
        self.invoice(20, 80)
        resp = self.api.get("/api/v1/reports/salesperson-profitability/")
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        row = salesperson_profitability_report(self.tenant.id)["rows"][0]
        self.assertEqual(row["netSales"], 2600.0)
        self.assertEqual(row["cogs"], 1500.0)
        self.assertEqual(row["collection"], 0.0)          # nothing received yet -- not 82% of sales
        self.assertEqual(row["incentive"], 0.0)           # no incentive calculated -- not 1.5%
        self.assertEqual(row["grossProfit"], 1100.0)
        self.assertEqual(row["contribution"], 1100.0)


class ReturnNeedsQcTests(Phase1Base):
    """Returned goods land in QC Hold; only QC may accept them straight into stock."""

    def _return(self, api, condition):
        order = self.order(qty=10)
        challan_id = self.dispatch(order["id"])
        invoice_id = self.post(f"/sales/challans/{challan_id}/convert-to-invoice/").json()["id"]
        self.post(f"/sales/invoices/{invoice_id}/finalize/")
        line = SalesInvoice.objects.get(pk=invoice_id).line_items.get()
        return api.post("/api/v1/sales/returns/", {
            "salesInvoiceId": invoice_id, "partyId": str(self.customer.id), "date": str(timezone.localdate()),
            "locationId": str(self.location.id),
            "lineItems": [{"itemId": str(self.fabric.id), "qty": 4, "returnedQty": 4, "rate": 100, "tax": 0,
                           "salesInvoiceLineId": str(line.id), "condition": condition}],
        }, format="json")

    def _clerk_api(self, permissions):
        """A real role holding exactly ``permissions`` (no QC approval)."""
        from apps.accounts.models import Role, RolePermission
        from apps.accounts.permission_catalogue import sync_permissions

        sync_permissions()
        role = Role.objects.create(client=self.tenant, code="RET-CLERK", name="Returns clerk")
        for pid in permissions:
            RolePermission.objects.create(role=role, permission_id=pid)
        clerk = User.objects.create_user(email="returns-clerk@example.com", password="pass",
                                         client=self.tenant, name="Returns clerk", role=role)
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(clerk)['access']}")
        return api

    def test_clerk_without_qc_rights_cannot_accept_returns_as_good(self):
        api = self._clerk_api({"view_sales", "process_returns", "create_sales_order"})
        refused = self._return(api, "Good")
        self.assertEqual(refused.status_code, 403, refused.content[:400])
        self.assertEqual(refused.json()["code"], "approve_qc")

    def test_clerk_return_to_hold_is_allowed(self):
        api = self._clerk_api({"view_sales", "process_returns", "create_sales_order"})
        ok = self._return(api, "Hold")
        self.assertEqual(ok.status_code, 201, ok.content[:400])

    def test_qc_can_accept_as_good(self):
        self.assertEqual(self._return(self.api, "Good").status_code, 201)   # superuser
