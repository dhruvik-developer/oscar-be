"""Factory fabric flows on one purchase order.

    Flow A  Factory -> OSCAR warehouse
    Flow B  Factory -> dyeing mill -> OSCAR warehouse

Run with::

    python manage.py test apps.purchase.tests_fabric_flows
"""
import uuid
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, User
from apps.inventory import qc as qc_service
from apps.inventory.models import Batch, LotCostEntry, LotTaka, StockMovement
from apps.masters.models import Item, Location, Party
from apps.purchase.models import (
    GoodsReceipt,
    PurchaseOrder,
    PurchaseOrderCostAllocation,
    PurchaseOrderExpense,
    SupplierDelivery,
)


class FabricFlowTestBase(TestCase):
    def setUp(self):
        self.tenant = Client.objects.create(name="Fabric Flow Client", slug="fabric-flow")
        self.user = User.objects.create_user(
            email="flow-buyer@example.com", password="secretpassword", client=self.tenant, is_superuser=True,
        )
        from apps.accounting.services import seed_chart_of_accounts

        seed_chart_of_accounts(self.tenant)
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(self.user)['access']}")
        self.fabric = Item.objects.create(
            client=self.tenant, sku="GREY-C60", name="Grey Cotton 60s", uom="Mtr", item_kind="Fabric",
            hsn_code="52083190",
        )
        self.yarn = Item.objects.create(client=self.tenant, sku="YRN-60", name="Cotton yarn 60s", uom="Kg", item_kind="Yarn")
        self.factory = Party.objects.create(client=self.tenant, name="Shree Looms", code="V-SL", type="Vendor")
        self.mill = Party.objects.create(client=self.tenant, name="Rang Dyeing Mill", code="V-RDM", type="Vendor")
        self.godown = Location.objects.create(client=self.tenant, name="Main Godown", code="WH-1", type="Warehouse")

    def url(self, po, path=""):
        return f"/api/v1/purchase/orders/{po.id}/fabric-flow/{path}"

    def create_po(self, flow, *, qty=1000, rate=40, extra=None):
        body = {
            "vendorId": str(self.factory.id),
            "date": "2026-10-19",
            "fabricFlow": flow,
            "lineItems": [{"itemId": str(self.fabric.id), "qty": qty, "rate": rate, "tax": 5, "uom": "Mtr"}],
            "yarnLines": [{"itemId": str(self.yarn.id), "requiredQty": 120, "uom": "Kg", "rate": 300}],
        }
        body.update(extra or {})
        res = self.api.post("/api/v1/purchase/orders/", body, format="json")
        self.assertEqual(res.status_code, 201, res.content)
        po = PurchaseOrder.objects.get(pk=res.json()["id"])
        for step in ("submit-for-approval", "approve"):
            r = self.api.post(f"/api/v1/purchase/orders/{po.id}/{step}/", {}, format="json")
            self.assertEqual(r.status_code, 200, r.content)
        po.refresh_from_db()
        return po

    def post(self, po, path, body, key=None, expect=201):
        headers = {"HTTP_IDEMPOTENCY_KEY": key} if key else {}
        res = self.api.post(self.url(po, path), body, format="json", **headers)
        self.assertEqual(res.status_code, expect, res.content)
        return res.json()

    def receive(self, po, qty, rate=None, key=None, challan=None, expect=201):
        line = po.line_items.first()
        body = {
            "locationId": str(self.godown.id),
            "receiptDate": "2026-10-25",
            "challanNo": challan,
            "lines": [{"poLineId": str(line.id), "receivedQty": qty, **({"rate": rate} if rate else {})}],
        }
        return self.post(po, "receipts/", body, key=key, expect=expect)

    def qc(self, grn_number, good):
        receipt = GoodsReceipt.objects.get(grn_number=grn_number, client=self.tenant)
        qc_service.dispose_receipt(
            receipt, quantities={"good": Decimal(str(good))}, user=self.user, require_job_work_order=False,
        )

    def batches(self, po):
        return list(Batch.objects.filter(purchase_order=po, deleted_at__isnull=True).order_by("created_at"))


class FlowAFactoryToOscarTests(FabricFlowTestBase):
    def test_po_creates_no_batch_or_stock(self):
        po = self.create_po("FACTORY_TO_OSCAR")
        self.assertEqual(po.purchase_type, "Grey Stock Purchase")
        self.assertEqual(Batch.objects.filter(client=self.tenant).count(), 0)
        self.assertFalse(StockMovement.objects.filter(client=self.tenant).exists())
        body = self.api.get(self.url(po)).json()
        self.assertEqual(body["fabricFlow"], "FACTORY_TO_OSCAR")
        self.assertEqual(len(body["yarnLines"]), 1)
        self.assertEqual(body["yarnLines"][0]["amount"], "36000.00")
        # Planned yarn is not consumed stock.
        self.assertFalse(StockMovement.objects.filter(item=self.yarn).exists())

    def test_three_partial_receipts_make_three_grey_batches(self):
        po = self.create_po("FACTORY_TO_OSCAR")
        self.post(po, "dispatches/", {"dispatchedQty": 1000, "dispatchDate": "2026-10-22", "challanNo": "F-1"})
        for qty, rate, challan in ((200, 40, "C-1"), (500, 56, "C-2"), (300, 40, "C-3")):
            self.receive(po, qty, rate=rate, challan=challan)

        batches = self.batches(po)
        self.assertEqual(len(batches), 3)
        self.assertEqual(len({b.batch_code for b in batches}), 3)
        self.assertTrue(all(b.fabric_type == "GREY" for b in batches))
        self.assertTrue(all(b.batch_code.startswith("OSCAR-GRY-") for b in batches))
        self.assertEqual(GoodsReceipt.objects.filter(purchase_order=po).count(), 3)
        self.assertEqual(len({b.goods_receipt_id for b in batches}), 3)
        line = po.line_items.first()
        self.assertTrue(all(b.purchase_order_line_id == line.id for b in batches))
        self.assertEqual(
            [sum(t.received_quantity for t in LotTaka.objects.filter(batch=b)) for b in batches],
            [Decimal("200"), Decimal("500"), Decimal("300")],
        )
        # Received fabric waits at QC: nothing is available until accepted.
        self.assertEqual(sum(t.available_quantity for t in LotTaka.objects.filter(batch__in=batches, location=self.godown)), 0)

        summary = self.api.get(self.url(po)).json()
        self.assertEqual(Decimal(summary["quantities"]["receivedAtOscar"]), Decimal("1000"))
        self.assertEqual(Decimal(summary["quantities"]["pending"]), Decimal("0"))
        po.refresh_from_db()
        self.assertEqual(po.status, "Fully Received")

        # Accepting the first receipt makes exactly its 200 m available.
        self.qc(summary["receipts"][0]["grnNumber"], 200)
        available = sum(t.available_quantity for t in LotTaka.objects.filter(batch=batches[0], location=self.godown))
        self.assertEqual(available, Decimal("200"))

        # Each batch carries its own receipt's fabric value (qty x its rate).
        fabric = {a.batch_id: a.amount for a in PurchaseOrderCostAllocation.objects.filter(
            purchase_order=po, expense__isnull=True, deleted_at__isnull=True)}
        self.assertEqual([fabric[b.id] for b in batches], [Decimal("8000.00"), Decimal("28000.00"), Decimal("12000.00")])

    def test_cannot_receive_more_than_ordered_or_before_approval(self):
        po = self.create_po("FACTORY_TO_OSCAR", qty=100)
        self.receive(po, 100)
        self.receive(po, 1, expect=400)
        draft = self.api.post("/api/v1/purchase/orders/", {
            "vendorId": str(self.factory.id), "date": "2026-10-19", "fabricFlow": "FACTORY_TO_OSCAR",
            "lineItems": [{"itemId": str(self.fabric.id), "qty": 10, "rate": 1, "uom": "Mtr"}],
        }, format="json").json()
        self.receive(PurchaseOrder.objects.get(pk=draft["id"]), 5, expect=409)

    def test_dyeing_costs_are_not_for_flow_a(self):
        po = self.create_po("FACTORY_TO_OSCAR")
        self.post(po, "expenses/", {"category": "DYEING", "amount": 100}, expect=400)


class FlowBFactoryToMillTests(FabricFlowTestBase):
    def test_mill_flow_traces_grey_and_receives_dyed(self):
        po = self.create_po("FACTORY_TO_MILL", extra={"dyeingMillId": str(self.mill.id)})
        # Fabric cannot arrive at OSCAR before the factory sent it to the mill.
        self.receive(po, 100, expect=400)

        self.post(po, "dispatches/", {"dispatchedQty": 1000, "dispatchDate": "2026-10-21", "challanNo": "FM-1",
                                      "transporterName": "Fast Roadways"})
        delivery = SupplierDelivery.objects.get(purchase_order=po)
        self.assertEqual(delivery.destination, "MILL")
        self.assertEqual(delivery.mill_id, self.mill.id)
        # The factory -> mill movement opens no grey batch and posts no stock.
        self.assertEqual(Batch.objects.filter(client=self.tenant).count(), 0)
        self.assertFalse(StockMovement.objects.filter(client=self.tenant).exists())

        mill = self.post(po, "mill-processes/", {"millReceiptDate": "2026-10-22", "qtyReceived": 1000,
                                                 "processType": "Dyeing", "shade": "Navy"})["millProcesses"][0]
        res = self.api.patch(self.url(po, f"mill-processes/{mill['id']}/"),
                             {"processedQty": 960, "millDispatchedQty": 950, "millDispatchDate": "2026-10-28"},
                             format="json")
        self.assertEqual(res.status_code, 200, res.content)
        bad = self.api.patch(self.url(po, f"mill-processes/{mill['id']}/"), {"millDispatchedQty": 999}, format="json")
        self.assertEqual(bad.status_code, 400)

        summary = self.receive(po, 950, challan="MILL-OUT-1")
        batches = self.batches(po)
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0].fabric_type, "DYED")
        self.assertTrue(batches[0].batch_code.startswith("OSCAR-DYD-"))
        self.assertFalse(Batch.objects.filter(client=self.tenant, fabric_type="GREY").exists())

        disc = summary["quantities"]["discrepancy"]
        self.assertEqual(Decimal(disc["difference"]), Decimal("50"))
        self.assertEqual(Decimal(disc["unresolved"]), Decimal("50"))
        # The system never classifies the difference on its own.
        self.assertEqual(Decimal(disc["classifiedTotal"]), Decimal("0"))

        # The user splits it; more than is unresolved is refused.
        self.post(po, "discrepancies/", {"entries": [{"category": "LOSS", "quantity": 60, "reason": "x"}]}, expect=400)
        summary = self.post(po, "discrepancies/", {"reason": "Mill shrinkage report", "entries": [
            {"category": "LOSS", "quantity": 20}, {"category": "WASTE", "quantity": 10},
            {"category": "REPROCESS", "quantity": 15}, {"category": "PENDING", "quantity": 5},
        ]})
        disc = summary["quantities"]["discrepancy"]
        self.assertEqual(Decimal(disc["unresolved"]), Decimal("0"))
        self.assertEqual(Decimal(disc["classified"]["LOSS"]), Decimal("20"))
        # Classification moves no stock.
        self.assertEqual(
            sum(t.received_quantity for t in LotTaka.objects.filter(client=self.tenant)), Decimal("950"),
        )

        # QR / Batch 360 shows the PO and the factory -> mill journey.
        detail = self.api.get(f"/api/v1/inventory/batches/{batches[0].batch_code}/").json()
        trace = detail["purchaseTrace"]
        self.assertEqual(trace["poNumber"], po.po_number)
        self.assertEqual(trace["factoryToMill"][0]["dispatchedQty"], "1000.0000")
        self.assertEqual(trace["millProcesses"][0]["shade"], "Navy")
        self.assertEqual(len(trace["yarnLines"]), 1)

    def test_mill_cannot_receive_more_than_dispatched(self):
        po = self.create_po("FACTORY_TO_MILL", extra={"dyeingMillId": str(self.mill.id)})
        self.post(po, "dispatches/", {"dispatchedQty": 100, "dispatchDate": "2026-10-21"})
        self.post(po, "mill-processes/", {"millReceiptDate": "2026-10-22", "qtyReceived": 101}, expect=400)
        self.post(po, "dispatches/", {"dispatchedQty": 901, "dispatchDate": "2026-10-21"}, expect=400)


class CostTests(FabricFlowTestBase):
    def test_costs_live_on_the_po_and_are_allocated_once(self):
        po = self.create_po("FACTORY_TO_MILL", extra={
            "dyeingMillId": str(self.mill.id),
            "expenses": [{"category": "YARN", "expenseType": "Yarn 60s", "amount": 30000, "status": "ESTIMATED"}],
        })
        self.post(po, "dispatches/", {"dispatchedQty": 1000, "dispatchDate": "2026-10-21"})
        for row in (
            {"category": "MANUFACTURING", "expenseType": "Weaving charges", "amount": 5000},
            {"category": "DYEING", "expenseType": "Dyeing charges", "quantity": 1000, "rate": 12},
            {"category": "FREIGHT", "expenseType": "Factory-to-mill freight", "amount": 2000, "billReference": "LR-9"},
            {"category": "OTHER", "expenseType": "Loading / unloading", "amount": 1000},
        ):
            self.post(po, "expenses/", row)
        # The same bill cannot be recorded twice as the same cost.
        self.post(po, "expenses/", {"category": "FREIGHT", "amount": 2000, "billReference": "LR-9"}, expect=409)

        summary = self.api.get(self.url(po)).json()
        self.assertEqual(len(summary["expenses"]), 5)
        self.assertEqual(summary["costs"]["dyeing"], "12000.00")
        self.assertEqual(summary["costs"]["landedCostStatus"], "NOT_STARTED")

        # Half the order arrives: half of every PO cost is allocated, half stays open.
        summary = self.receive(po, 500, challan="D-1")
        costs = summary["costs"]
        self.assertEqual(Decimal(costs["allocated"]), Decimal("20000") + Decimal("25000"))  # fabric 500x40 + half of 50000
        self.assertEqual(Decimal(costs["unallocated"]), Decimal("25000"))
        self.assertEqual(costs["landedCostStatus"], "PROVISIONAL")

        # Re-running the allocation changes nothing (no duplicate cost).
        before = LotCostEntry.objects.filter(client=self.tenant, deleted_at__isnull=True).count()
        self.post(po, "allocate-costs/", {}, expect=200)
        self.assertEqual(LotCostEntry.objects.filter(client=self.tenant, deleted_at__isnull=True).count(), before)

        # A cost updated later re-books only that cost's share.
        yarn = PurchaseOrderExpense.objects.get(purchase_order=po, category="YARN")
        res = self.api.patch(self.url(po, f"expenses/{yarn.id}/"), {"amount": 32000, "status": "ACTUAL"}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        yarn_share = PurchaseOrderCostAllocation.objects.get(expense=yarn, deleted_at__isnull=True)
        self.assertEqual(yarn_share.amount, Decimal("16000.00"))
        self.assertEqual(PurchaseOrderCostAllocation.objects.filter(expense=yarn).count(), 2)  # old one kept, soft-deleted

        # The rest arrives; 50 m is written off as loss, so the full cost lands on the 950 m that came.
        self.receive(po, 450, challan="D-2")
        self.post(po, "discrepancies/", {"entries": [{"category": "LOSS", "quantity": 50, "reason": "Burnt in dyeing"}]})
        costs = self.api.get(self.url(po)).json()["costs"]
        total_expenses = Decimal("32000") + Decimal("5000") + Decimal("12000") + Decimal("2000") + Decimal("1000")
        fabric = Decimal("950") * Decimal("40")
        self.assertEqual(Decimal(costs["allocated"]), fabric + total_expenses)
        self.assertEqual(Decimal(costs["unallocated"]), Decimal("0"))

        # Batch landed cost = its allocations, through the lot cost ledger.
        for batch in self.batches(po):
            allocated = sum(a.amount for a in PurchaseOrderCostAllocation.objects.filter(batch=batch, deleted_at__isnull=True))
            detail = self.api.get(f"/api/v1/inventory/batches/{batch.batch_code}/").json()
            self.assertEqual(Decimal(detail["costing"]["totalCost"]), allocated)
            self.assertEqual(Decimal(detail["purchaseTrace"]["allocatedTotal"]), allocated)


class DuplicateSubmissionTests(FabricFlowTestBase):
    def test_same_receipt_twice_posts_once(self):
        po = self.create_po("FACTORY_TO_OSCAR")
        key = str(uuid.uuid4())
        first = self.receive(po, 200, key=key, challan="C-77")
        res = self.api.post(self.url(po, "receipts/"), {
            "locationId": str(self.godown.id), "receiptDate": "2026-10-25", "challanNo": "C-77",
            "lines": [{"poLineId": str(po.line_items.first().id), "receivedQty": 200}],
        }, format="json", HTTP_IDEMPOTENCY_KEY=key)
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res["Idempotency-Replayed"], "true")
        self.assertEqual(res.json()["receipts"], first["receipts"])
        self.assertEqual(GoodsReceipt.objects.filter(purchase_order=po).count(), 1)
        self.assertEqual(len(self.batches(po)), 1)
        self.assertEqual(StockMovement.objects.filter(client=self.tenant, type="PURCHASE").count(), 1)
        # Without the key, the same supplier challan is still refused.
        self.receive(po, 200, challan="C-77", expect=409)
        self.assertEqual(GoodsReceipt.objects.filter(purchase_order=po).count(), 1)


class ExistingPurchaseTypesTests(FabricFlowTestBase):
    def test_ready_grey_po_keeps_one_batch_per_po(self):
        res = self.api.post("/api/v1/purchase/orders/", {
            "vendorId": str(self.factory.id), "date": "2026-10-19", "purchaseType": "Grey Stock Purchase",
            "lineItems": [{"itemId": str(self.fabric.id), "qty": 300, "rate": 40, "uom": "Mtr"}],
        }, format="json")
        po = PurchaseOrder.objects.get(pk=res.json()["id"])
        self.assertIsNone(po.fabric_flow)
        for step in ("submit-for-approval", "approve"):
            self.api.post(f"/api/v1/purchase/orders/{po.id}/{step}/", {}, format="json")
        line = po.line_items.first()
        for challan in ("R-1", "R-2"):
            r = self.api.post("/api/v1/purchase/receipts/", {
                "vendorId": str(self.factory.id), "purchaseOrderId": str(po.id), "locationId": str(self.godown.id),
                "receiptDate": "2026-10-20", "supplierChallanNo": challan,
                "lines": [{"itemId": str(self.fabric.id), "poLineId": str(line.id), "receivedQty": 100}],
            }, format="json")
            self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(Batch.objects.filter(source_document_id=po.id).count(), 1)
        # The fabric-flow screens are only for factory fabric orders.
        self.assertEqual(self.api.get(self.url(po)).status_code, 422)

    def test_flow_cannot_change_after_draft(self):
        po = self.create_po("FACTORY_TO_OSCAR")
        res = self.api.patch(f"/api/v1/purchase/orders/{po.id}/", {"fabricFlow": "FACTORY_TO_MILL"}, format="json")
        self.assertEqual(res.status_code, 400)
