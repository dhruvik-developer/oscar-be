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
from datetime import timedelta
from decimal import Decimal

from django.apps import apps as django_apps
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounting import services as ledger
from apps.accounting.models import JournalLine
from apps.accounting.reports import ageing
from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, User
from apps.core.money import AGEING_BUCKETS, ageing_bucket
from apps.inventory import services as stock
from apps.masters.models import Item, Location, Party

from .models import (
    NON_RESERVING_STAGES,
    ORDER_STAGES,
    DeliveryChallan,
    SalesInvoice,
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

    def test_non_stock_lines_need_no_dispatch(self):
        order = self.order(qty=10, extra_lines=[
            # Free-text (item-less) line: the only non-stock line left since
            # masters 0025 made every item kind stock-holding.
            {"itemName": "Stitching", "description": "Stitching", "qty": 1, "rate": 300, "tax": 0},
        ])
        invoice = self.post(f"/sales/orders/{order['id']}/convert-to-invoice/")
        self.assertEqual(invoice.status_code, 201, invoice.content[:400])
        rows = invoice.json()["lineItems"]
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["itemId"])
        self.assertEqual(rows[0]["itemName"], "Stitching")

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
        self.assertEqual(second.status_code, 409, second.content[:400])


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
