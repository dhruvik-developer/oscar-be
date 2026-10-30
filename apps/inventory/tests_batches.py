"""Fabric batch traceability: a permanent batch ID kept through split, job
work and sale, with derived quantities, a timeline and batch-specific cost.

    python manage.py test apps.inventory.tests_batches
"""
import uuid
from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, User
from apps.inventory import batch_services
from apps.inventory import services as stock
from apps.inventory.models import Batch, LotCostEntry, LotTaka
from apps.masters.models import Item, Location, Party


class BatchTestBase(TestCase):
    def setUp(self):
        self.tenant, _ = Client.objects.get_or_create(slug="batch-tenant", defaults={"name": "Batch Tenant"})
        self.user = User.objects.create_user(
            email="batch-admin@example.com", password="pass", client=self.tenant,
            is_superuser=True, name="Batch Admin",
        )
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(self.user)['access']}")
        self.vendor = Party.objects.create(client=self.tenant, name="Grey Mills", code="V-GM", type="Vendor")
        self.customer = Party.objects.create(client=self.tenant, name="Buyer Co", code="C-BC", type="Customer")
        self.item = Item.objects.create(
            client=self.tenant, name="Grey Cambric", sku=f"GC-{uuid.uuid4().hex[:5]}",
            item_kind="Fabric", uom="Meter",
        )
        self.godown = Location.objects.create(client=self.tenant, name="Godown", code="GD-1", type="Godown", is_active=True)
        self.reject = Location.objects.create(client=self.tenant, name="Reject", code="SYS-REJECT", type="Scrap", is_active=True)
        self.wip = Location.objects.create(client=self.tenant, name="Dyer WIP", code="SYS-WIP-DYER", type="Transit", is_active=True)

    def receive(self, lot="L-001", taka_no="1", meters="100"):
        taka = LotTaka.objects.create(
            client=self.tenant, item=self.item, location=self.godown, lot_number=lot,
            taka_number=taka_no, taka_id=f"TAK-{uuid.uuid4().hex[:6]}", supplier=self.vendor,
            received_quantity=Decimal(meters), available_quantity=Decimal("0"),
            source_document_type="GRN", source_document_number="GRN-1", uom="Meter",
        )
        stock.post_movement(
            client_id=self.tenant.id, item=self.item, location=self.godown, type="PURCHASE",
            quantity=Decimal(meters), reference_type="GoodsReceipt", reference_id=uuid.uuid4(),
            reference_number="GRN-1", lot_taka=taka, unit_cost=Decimal("50"),
        )
        taka.refresh_from_db()
        return taka


class BatchTraceabilityTests(BatchTestBase):
    def test_rolls_of_one_lot_share_a_batch_with_a_series_code(self):
        a = self.receive(taka_no="1")
        b = self.receive(taka_no="2")
        self.assertIsNotNone(a.batch_id)
        self.assertEqual(a.batch_id, b.batch_id)
        self.assertRegex(a.batch.batch_code, r"^OSCAR-GRY-\d{4}-\d{4}$")
        self.assertEqual(a.batch.origin, "PURCHASE")
        other = self.receive(lot="L-002")
        self.assertNotEqual(other.batch_id, a.batch_id)

    def test_split_and_job_work_inward_keep_the_batch(self):
        grey = self.receive()
        child = stock.split_lot(grey, location_id=self.godown.id, suffix="S")
        self.assertEqual(child.batch_id, grey.batch_id)
        dyed = LotTaka.objects.create(
            client=self.tenant, item=self.item, location=self.godown, lot_number="L-001-P",
            taka_number="1", taka_id=f"TAK-{uuid.uuid4().hex[:6]}", parent_taka=grey,
            received_quantity=Decimal("95"), source_document_type="JobWorkInward",
        )
        self.assertEqual(dyed.batch_id, grey.batch_id)
        self.assertEqual(Batch.objects.filter(client=self.tenant).count(), 1)

    def test_cost_and_profit_are_batch_specific(self):
        grey = self.receive(meters="100")
        # Dyeing returned 90 m into a processed roll of the same batch.
        stock.post_movement(
            client_id=self.tenant.id, item=self.item, location=self.godown, type="JOBWORK_OUTWARD",
            quantity=Decimal("-100"), reference_type="JobWorkOrder", reference_id=uuid.uuid4(),
            reference_number="JWO-1", lot_taka=grey,
        )
        dyed = LotTaka.objects.create(
            client=self.tenant, item=self.item, location=self.godown, lot_number="L-001-P",
            taka_number="1", taka_id=f"TAK-{uuid.uuid4().hex[:6]}", parent_taka=grey,
            received_quantity=Decimal("90"), source_document_type="JobWorkInward", uom="Meter",
        )
        stock.post_movement(
            client_id=self.tenant.id, item=self.item, location=self.godown, type="JOBWORK_INWARD",
            quantity=Decimal("90"), reference_type="JobWorkOrder", reference_id=uuid.uuid4(),
            reference_number="JWO-1", lot_taka=dyed,
        )
        # Grey 5,000 + dyeing 900 + freight 100 = 6,000 over 90 good metres.
        for lot, kind, amount in (("L-001", "Grey purchase", "5000"),
                                  ("L-001-P", "Dyeing/Printing/Finishing", "900"),
                                  ("L-001-P", "Processing transport", "100")):
            LotCostEntry.objects.create(client=self.tenant, lot_number=lot, cost_type=kind, amount=Decimal(amount))

        from apps.sales.models import SalesInvoice, SalesInvoiceLine

        invoice = SalesInvoice.objects.create(
            client=self.tenant, party=self.customer, party_name=self.customer.name,
            doc_date=date(2026, 10, 1), invoice_number=f"INV-{uuid.uuid4().hex[:5]}",
            status="Unpaid", posted_at=timezone.now(),
        )
        SalesInvoiceLine.objects.create(
            sales_invoice=invoice, client=self.tenant, item=self.item, qty=Decimal("30"),
            rate=Decimal("100"), amount=Decimal("3000"),
        )
        stock.post_movement(
            client_id=self.tenant.id, item=self.item, location=self.godown, type="SALE",
            quantity=Decimal("-30"), reference_type="SalesInvoice", reference_id=invoice.id,
            reference_number=invoice.invoice_number, lot_taka=dyed,
        )

        batch = Batch.objects.get(client=self.tenant)
        qty = batch_services.batch_quantities(batch)
        self.assertEqual(qty["received"], Decimal("100.0000"))
        self.assertEqual(qty["onHand"], Decimal("60.0000"))
        self.assertEqual(qty["sold"], Decimal("30.0000"))
        cost = batch_services.batch_costing(batch)
        self.assertEqual(cost["totalCost"], "6000.00")
        self.assertEqual(cost["costPerUnit"], "66.6667")      # 6000 / (30 sold + 60 in stock)
        self.assertEqual(cost["revenue"], "3000.00")
        self.assertEqual(cost["costOfSold"], "2000.00")
        self.assertEqual(cost["realizedProfit"], "1000.00")
        self.assertEqual(cost["unsoldInventoryValue"], "4000.00")

    def test_detail_api_by_batch_code_or_taka_and_cost_gating(self):
        grey = self.receive()
        LotCostEntry.objects.create(client=self.tenant, lot_number="L-001", cost_type="Grey purchase", amount=Decimal("5000"))
        code = grey.batch.batch_code

        resp = self.api.get(f"/api/v1/inventory/batches/{code}/")
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual(body["batchCode"], code)
        self.assertTrue(body["canSeeCost"])
        self.assertIn("costing", body)
        kinds = {e["kind"] for e in body["timeline"]}
        self.assertTrue({"BATCH_CREATED", "MOVEMENT", "COST"} <= kinds)

        by_taka = self.api.get(f"/api/v1/inventory/batches/{grey.taka_id}/")
        self.assertEqual(by_taka.json()["batchCode"], code)
        self.assertEqual(self.api.get("/api/v1/inventory/batches/NOPE-1/").status_code, 404)

        listing = self.api.get("/api/v1/inventory/batches/?q=L-001").json()
        self.assertEqual(listing["count"], 1)

        # A user without view_ledger sees the history but not cost or profit.
        clerk = User.objects.create_user(
            email="clerk@example.com", password="pass", client=self.tenant, name="Clerk",
        )
        clerk.permission_ids = ["view_inventory"]
        from unittest.mock import patch

        with patch.object(User, "effective_permissions", lambda self: {"view_inventory"}):
            api = APIClient()
            api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(clerk)['access']}")
            limited = api.get(f"/api/v1/inventory/batches/{code}/")
        self.assertEqual(limited.status_code, 200, limited.content)
        self.assertFalse(limited.json()["canSeeCost"])
        self.assertNotIn("costing", limited.json())
        self.assertNotIn("COST", {e["kind"] for e in limited.json()["timeline"]})


class BatchChargeAllocationTests(BatchTestBase):
    """One charge split over batches by metre, by taka or by hand."""

    def setUp(self):
        super().setUp()
        self.a = self.receive(lot="A", meters="300").batch
        self.b = self.receive(lot="B", taka_no="1", meters="50").batch
        self.receive(lot="B", taka_no="2", meters="50")

    def charge(self, **body):
        payload = {"batches": [self.a.batch_code, self.b.batch_code], "amount": "1000",
                   "costType": "Processing transport"}
        payload.update(body)
        return self.api.post("/api/v1/inventory/batches/allocate-charge/", payload, format="json")

    def amounts(self, response):
        return {r["batchCode"]: r["amount"] for r in response.json()["allocations"]}

    def test_split_by_metre(self):
        resp = self.charge(basis="METER", reference="LR-1")
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(self.amounts(resp), {self.a.batch_code: "750.00", self.b.batch_code: "250.00"})

    def test_split_by_taka(self):
        resp = self.charge(basis="TAKA")
        self.assertEqual(self.amounts(resp), {self.a.batch_code: "333.33", self.b.batch_code: "666.67"})

    def test_manual_split_must_add_up(self):
        bad = self.charge(basis="MANUAL", manual={self.a.batch_code: "100", self.b.batch_code: "100"})
        self.assertEqual(bad.status_code, 400, bad.content)
        ok = self.charge(basis="MANUAL", manual={self.a.batch_code: "900", self.b.batch_code: "100"})
        self.assertEqual(self.amounts(ok), {self.a.batch_code: "900.00", self.b.batch_code: "100.00"})

    def test_same_reference_is_not_charged_twice(self):
        self.assertEqual(self.charge(basis="METER", reference="LR-9").status_code, 201)
        again = self.charge(basis="METER", reference="LR-9")
        self.assertEqual(again.status_code, 422, again.content)
        self.assertEqual(LotCostEntry.objects.filter(source_document_number="LR-9").count(), 2)

    def test_charge_shows_in_batch_cost(self):
        self.charge(basis="METER")
        cost = batch_services.batch_costing(self.a)
        self.assertEqual(cost["costByType"]["Processing transport"], "750.00")


class BatchOwnershipTests(BatchTestBase):
    """Customer-owned material is never sold as OSCAR stock."""

    def setUp(self):
        super().setUp()
        self.item.tracking_type = "TAKA"
        self.item.save(update_fields=["tracking_type"])
        self.taka = self.receive(lot="CUST-1", meters="100")
        self.taka.qc_status = "Passed"
        self.taka.save(update_fields=["qc_status"])
        self.batch = self.taka.batch

    def test_ownership_needs_an_owner_and_hides_stock_from_sale(self):
        url = f"/api/v1/inventory/batches/{self.batch.batch_code}/"
        self.assertEqual(self.api.patch(url, {"ownership": "CUSTOMER"}, format="json").status_code, 400)
        ok = self.api.patch(url, {"ownership": "CUSTOMER", "ownerPartyId": str(self.customer.id)}, format="json")
        self.assertEqual(ok.status_code, 200, ok.content)
        self.assertEqual(ok.json()["ownerPartyName"], "Buyer Co")
        eligible = self.api.get(f"/api/v1/inventory/batches/eligible/?itemId={self.item.id}").json()["results"]
        self.assertEqual(eligible, [])

    def test_customer_roll_is_never_sold_even_to_its_owner(self):
        from types import SimpleNamespace

        from apps.core.exceptions import BusinessRuleViolation
        from apps.sales.services import allocate_takas

        batch_services.set_ownership(self.batch, ownership="CUSTOMER", owner_party_id=self.customer.id)
        self.taka.refresh_from_db()
        selected = SimpleNamespace(
            item=self.item, item_id=self.item.id, item_name=self.item.name,
            taka_allocations=[{"lotTakaId": str(self.taka.id), "qty": "10"}], reserved_takas=[],
        )
        with self.assertRaises(BusinessRuleViolation) as ctx:
            allocate_takas(self.tenant.id, selected, Decimal("10"), self.godown.id)
        self.assertEqual(ctx.exception.code, "NOT_OSCAR_STOCK")
        fifo = SimpleNamespace(item=self.item, item_id=self.item.id, item_name=self.item.name,
                               taka_allocations=[], reserved_takas=[])
        with self.assertRaises(BusinessRuleViolation):     # nothing of OSCAR's to draw on
            allocate_takas(self.tenant.id, fifo, Decimal("10"), self.godown.id)


class InHouseProductionTests(BatchTestBase):
    """Scenario B: yarn -> issue -> grey fabric -> QC -> stock, with batch cost;
    scenario C: the same for a customer."""

    def setUp(self):
        super().setUp()
        self.yarn = Item.objects.create(
            client=self.tenant, name="Cotton Yarn 40s", sku=f"YRN-{uuid.uuid4().hex[:5]}",
            item_kind="Raw Material", uom="Kg",
        )
        # 200 kg of yarn in stock at Rs 300/kg.
        stock.post_movement(
            client_id=self.tenant.id, item=self.yarn, location=self.godown, type="PURCHASE",
            quantity=Decimal("200"), unit_cost=Decimal("300"), reference_type="GoodsReceipt",
            reference_id=uuid.uuid4(), reference_number="GRN-Y1",
        )
        self.base = "/api/v1/inventory/production-runs/"

    def run_flow(self, customer=None):
        body = {"outputItemId": str(self.item.id), "plannedQty": "1000"}
        if customer:
            body["customerId"] = str(customer.id)
        run = self.api.post(self.base, body, format="json")
        self.assertEqual(run.status_code, 201, run.content)
        rid = run.json()["id"]
        act = lambda name, payload: self.api.post(f"{self.base}{rid}/{name}/", payload, format="json")
        r = act("issue", {"itemId": str(self.yarn.id), "locationId": str(self.godown.id), "quantity": "100"})
        self.assertEqual(r.status_code, 200, r.content)
        r = act("output", {"rolls": [{"meters": "500"}, {"meters": "450"}]})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["waitingQcQty"], "950.0000")
        r = act("expense", {"costType": "Production labour", "amount": "5000"})
        self.assertEqual(r.status_code, 200, r.content)
        r = act("qc", {"goodQty": "900", "rejectedQty": "50", "locationId": str(self.godown.id)})
        self.assertEqual(r.status_code, 200, r.content)
        r = act("complete", {"wastageQty": "4", "wastageUom": "Kg"})
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def test_yarn_to_grey_fabric_batch_with_cost(self):
        run = self.run_flow()
        self.assertEqual(run["status"], "Completed")
        self.assertEqual(run["issues"][0]["amount"], "30000.00")  # 100 kg x 300
        batch = Batch.objects.get(client=self.tenant, batch_code=run["batchCode"])
        self.assertEqual(batch.origin, "PRODUCTION")
        qty = batch_services.batch_quantities(batch)
        self.assertEqual(qty["available"], Decimal("900.0000"))
        self.assertEqual(qty["rejected"], Decimal("50.0000"))
        cost = batch_services.batch_costing(batch)
        self.assertEqual(cost["totalCost"], "35000.00")          # yarn 30,000 + labour 5,000
        self.assertEqual(cost["costPerUnit"], "38.8889")          # over 900 good metres
        # Yarn stock went down.
        from apps.inventory.models import StockBalance
        on_hand = sum(b.on_hand for b in StockBalance.objects.filter(item=self.yarn))
        self.assertEqual(on_hand, Decimal("100.0000"))
        # Costs are booked once even if completion is retried.
        again = self.api.post(f"{self.base}{run['id']}/complete/", {}, format="json")
        self.assertEqual(again.status_code, 409)

    def test_customer_manufacturing_marks_the_batch_as_customer_material(self):
        run = self.run_flow(customer=self.customer)
        batch = Batch.objects.get(client=self.tenant, batch_code=run["batchCode"])
        self.assertEqual(batch.ownership, "CUSTOMER")
        self.assertEqual(batch.owner_party_id, self.customer.id)
        self.assertEqual(
            self.api.get(f"/api/v1/inventory/batches/eligible/?itemId={self.item.id}").json()["results"], []
        )

    def test_issue_more_yarn_than_in_stock_is_refused(self):
        rid = self.api.post(self.base, {"outputItemId": str(self.item.id), "plannedQty": "10"}, format="json").json()["id"]
        r = self.api.post(f"{self.base}{rid}/issue/", {
            "itemId": str(self.yarn.id), "locationId": str(self.godown.id), "quantity": "500",
        }, format="json")
        self.assertEqual(r.status_code, 422, r.content)


class PurchaseOrderBatchTests(BatchTestBase):
    """One purchase order is one batch, however many receipts it takes."""

    def grn_taka(self, receipt, lot, meters="100"):
        return LotTaka.objects.create(
            client=self.tenant, item=self.item, location=self.godown, lot_number=lot, taka_number="1",
            taka_id=f"TAK-{uuid.uuid4().hex[:6]}", received_quantity=Decimal(meters),
            source_document_type="GRN", source_document_id=receipt.id, source_document_number=receipt.grn_number,
        )

    def test_two_receipts_of_one_po_share_a_batch(self):
        from apps.purchase.models import GoodsReceipt, PurchaseOrder

        po = PurchaseOrder.objects.create(client=self.tenant, party=self.vendor, po_number="PO-77", doc_date=date(2026, 9, 1))
        other_po = PurchaseOrder.objects.create(client=self.tenant, party=self.vendor, po_number="PO-78", doc_date=date(2026, 9, 1))
        receipts = [
            GoodsReceipt.objects.create(client=self.tenant, party=self.vendor, purchase_order=p, grn_number=n,
                                        receipt_date=date(2026, 9, 5), location=self.godown)
            for p, n in ((po, "GRN-1"), (po, "GRN-2"), (other_po, "GRN-3"))
        ]
        first = self.grn_taka(receipts[0], "LOT-A")
        second = self.grn_taka(receipts[1], "LOT-B")
        third = self.grn_taka(receipts[2], "LOT-C")
        self.assertEqual(first.batch_id, second.batch_id)
        self.assertNotEqual(first.batch_id, third.batch_id)
        self.assertEqual(first.batch.source_document_number, "PO-77")
        self.assertEqual(first.batch.supplier_id, self.vendor.id)

    def test_each_hsn_of_a_po_gets_its_own_batch(self):
        from apps.purchase.models import GoodsReceipt, PurchaseOrder

        self.item.hsn_code = "52083190"
        self.item.save(update_fields=["hsn_code"])
        poly = Item.objects.create(
            client=self.tenant, name="Grey Poly", sku=f"GP-{uuid.uuid4().hex[:5]}",
            item_kind="Fabric", uom="Meter", hsn_code="54075290",
        )
        po = PurchaseOrder.objects.create(client=self.tenant, party=self.vendor, po_number="PO-79", doc_date=date(2026, 9, 1))
        receipts = [
            GoodsReceipt.objects.create(client=self.tenant, party=self.vendor, purchase_order=po, grn_number=n,
                                        receipt_date=date(2026, 9, 5), location=self.godown)
            for n in ("GRN-4", "GRN-5")
        ]
        cotton = self.grn_taka(receipts[0], "LOT-D")
        polyester = LotTaka.objects.create(
            client=self.tenant, item=poly, location=self.godown, lot_number="LOT-F", taka_number="1",
            taka_id=f"TAK-{uuid.uuid4().hex[:6]}", received_quantity=Decimal("100"),
            source_document_type="GRN", source_document_id=receipts[0].id, source_document_number="GRN-4",
        )
        more_cotton = self.grn_taka(receipts[1], "LOT-G")
        self.assertNotEqual(cotton.batch_id, polyester.batch_id)
        self.assertEqual(cotton.batch_id, more_cotton.batch_id)
        self.assertEqual(batch_services.serialize_batch(cotton.batch)["hsnCodes"], ["52083190"])
        self.assertEqual(batch_services.serialize_batch(polyester.batch)["hsnCodes"], ["54075290"])


class BatchRegistryGroupingTests(BatchTestBase):
    def test_po_groups_include_only_linked_batches_and_real_po_summaries(self):
        from apps.purchase.models import GoodsReceipt, GoodsReceiptLine, PurchaseOrder, PurchaseOrderLine

        po = PurchaseOrder.objects.create(
            client=self.tenant, party=self.vendor, po_number="PO-GROUP-1",
            doc_date=date(2026, 9, 1),
        )
        po_line = PurchaseOrderLine.objects.create(
            client=self.tenant, purchase_order=po, line_no=1, item=self.item,
            item_name=self.item.name, uom="Meter", qty=Decimal("1000"),
        )
        linked_batches = []
        for index, (grn_number, received_qty) in enumerate((("GRN-GROUP-1", "200"), ("GRN-GROUP-2", "300")), 1):
            receipt = GoodsReceipt.objects.create(
                client=self.tenant, party=self.vendor, purchase_order=po,
                grn_number=grn_number, receipt_date=date(2026, 9, index + 1),
                location=self.godown,
                qc_status="Approved" if index == 1 else "Pending",
                qc_note='{"cumulative":{"good":"200","second":"0","reprocess":"0","reject":"0"}}' if index == 1 else "",
            )
            GoodsReceiptLine.objects.create(
                client=self.tenant, goods_receipt=receipt, purchase_order_line=po_line,
                item=self.item, uom="Meter", ordered_qty=Decimal("1000"),
                received_qty=Decimal(received_qty),
            )
            taka = LotTaka.objects.create(
                client=self.tenant, item=self.item, location=self.godown,
                lot_number=f"PO-GROUP-{index}", taka_number="1",
                taka_id=f"TAK-{uuid.uuid4().hex[:6]}",
                received_quantity=Decimal(received_qty),
                source_document_type="GRN", source_document_id=receipt.id,
                source_document_number=receipt.grn_number,
            )
            batch = taka.batch
            batch.purchase_order = po if index == 1 else None
            batch.purchase_order_line = po_line if index == 1 else None
            batch.goods_receipt = receipt if index == 1 else None
            batch.source_document_type = "GRN"
            batch.source_document_id = receipt.id
            batch.source_document_number = receipt.grn_number
            batch.save()
            linked_batches.append(batch.batch_code)

        other_po = PurchaseOrder.objects.create(
            client=self.tenant, party=self.vendor, po_number="PO-GROUP-2",
            doc_date=date(2026, 9, 1),
        )
        other_line = PurchaseOrderLine.objects.create(
            client=self.tenant, purchase_order=other_po, line_no=1, item=self.item,
            item_name=self.item.name, uom="Meter", qty=Decimal("100"),
        )
        other_receipt = GoodsReceipt.objects.create(
            client=self.tenant, party=self.vendor, purchase_order=other_po,
            grn_number="GRN-GROUP-OTHER", receipt_date=date(2026, 9, 4),
            location=self.godown,
        )
        GoodsReceiptLine.objects.create(
            client=self.tenant, goods_receipt=other_receipt, purchase_order_line=other_line,
            item=self.item, uom="Meter", ordered_qty=Decimal("100"),
            received_qty=Decimal("100"),
        )
        other_taka = LotTaka.objects.create(
            client=self.tenant, item=self.item, location=self.godown,
            lot_number="PO-GROUP-OTHER", taka_number="1",
            taka_id=f"TAK-{uuid.uuid4().hex[:6]}", received_quantity=Decimal("100"),
            source_document_type="GRN", source_document_id=other_receipt.id,
            source_document_number=other_receipt.grn_number,
        )
        other_batch_code = other_taka.batch.batch_code
        legacy = self.receive(lot="PO-GROUP-UNLINKED", meters="40").batch
        response = self.api.get("/api/v1/inventory/batches/groups/", {"groupBy": "po"})
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        po_group = next(group for group in body["groups"] if group.get("poId") == str(po.id))
        other_po_group = next(group for group in body["groups"] if group.get("poId") == str(other_po.id))
        unlinked_group = next(group for group in body["groups"] if group["type"] == "unlinked")
        self.assertEqual({row["batchCode"] for row in po_group["batches"]}, set(linked_batches))
        self.assertEqual([row["batchCode"] for row in other_po_group["batches"]], [other_batch_code])
        self.assertEqual(po_group["batchCount"], 2)
        self.assertEqual(po_group["poNumber"], "PO-GROUP-1")
        self.assertEqual(po_group["vendorName"], self.vendor.name)
        self.assertEqual(po_group["orderedQty"], "1000.0000")
        self.assertEqual(po_group["receivedQty"], "500.0000")
        self.assertEqual(po_group["remainingQty"], "500.0000")
        self.assertEqual(po_group["qcPassedQty"], "200.0000")
        self.assertEqual(po_group["qcPendingQty"], "300.0000")
        self.assertEqual(
            {grn for row in po_group["batches"] for grn in row["receiptNumbers"]},
            {"GRN-GROUP-1", "GRN-GROUP-2"},
        )
        self.assertEqual([row["batchCode"] for row in unlinked_group["batches"]], [legacy.batch_code])

        filtered = self.api.get(
            "/api/v1/inventory/batches/groups/",
            {"groupBy": "po", "poId": str(po.id)},
        )
        self.assertEqual(filtered.status_code, 200, filtered.content)
        self.assertEqual(len(filtered.json()["groups"]), 1)
        self.assertEqual(filtered.json()["groups"][0]["batchCount"], 2)
        self.assertEqual(
            {row["batchCode"] for row in filtered.json()["groups"][0]["batches"]},
            set(linked_batches),
        )

        flat = self.api.get("/api/v1/inventory/batches/", {"poId": str(po.id)})
        self.assertEqual(flat.status_code, 200, flat.content)
        self.assertEqual({row["batchCode"] for row in flat.json()["results"]}, set(linked_batches))
        searched = self.api.get("/api/v1/inventory/batches/", {"q": "PO-GROUP-1"})
        self.assertEqual(searched.status_code, 200, searched.content)
        self.assertEqual({row["batchCode"] for row in searched.json()["results"]}, set(linked_batches))

    def test_selected_po_without_batches_returns_an_empty_group(self):
        from apps.purchase.models import PurchaseOrder, PurchaseOrderLine

        po = PurchaseOrder.objects.create(
            client=self.tenant, party=self.vendor, po_number="PO-GROUP-EMPTY",
            doc_date=date(2026, 9, 1),
        )
        PurchaseOrderLine.objects.create(
            client=self.tenant, purchase_order=po, line_no=1, item=self.item,
            item_name=self.item.name, uom="Meter", qty=Decimal("500"),
        )
        response = self.api.get(
            "/api/v1/inventory/batches/groups/",
            {"groupBy": "po", "poId": str(po.id)},
        )
        self.assertEqual(response.status_code, 200, response.content)
        group = response.json()["groups"][0]
        self.assertEqual(group["poId"], str(po.id))
        self.assertEqual(group["poNumber"], "PO-GROUP-EMPTY")
        self.assertEqual(group["batchCount"], 0)
        self.assertEqual(group["batches"], [])


class BatchLossCostingTests(BatchTestBase):
    """Normal loss is absorbed by good fabric; a vendor-fault rejection is a claim."""

    def setUp(self):
        super().setUp()
        self.taka = self.receive(lot="L-LOSS", meters="100")
        self.batch = self.taka.batch
        LotCostEntry.objects.create(client=self.tenant, lot_number="L-LOSS", cost_type="Grey purchase", amount=Decimal("10000"))
        # QC: 20 m rejected out of 100 (posted the way QC posts it).
        stock.relocate_lot(
            client_id=self.tenant.id, lot=self.taka, quantity=Decimal("20"), to_location_id=self.reject.id,
            out_type="QC_REJECT", in_type="QC_REJECT", reference_type="QCInspection",
            reference_id=uuid.uuid4(), reference_number="QC-1",
        )
        self.url = f"/api/v1/inventory/batches/{self.batch.batch_code}/losses/"

    def test_all_normal_loss_is_absorbed(self):
        cost = batch_services.batch_costing(self.batch)
        self.assertEqual(cost["rejectedQty"], "20.0000")
        self.assertEqual(cost["costPerUnit"], "125.0000")        # 10,000 / 80 good
        self.assertEqual(cost["claimValue"], "0.00")

    def test_vendor_fault_is_a_claim_not_loaded_on_good_fabric(self):
        resp = self.api.post(self.url, {
            "quantity": "15", "cause": "SUPPLIER_QUALITY", "responsiblePartyId": str(self.vendor.id),
            "outcome": "VENDOR_RETURN", "reference": "DN-5",
        }, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        cost = resp.json()["costing"]
        # 10,000 over 80 good + 15 claimable = 95 m -> 105.2632 / m.
        self.assertEqual(cost["costPerUnit"], "105.2632")
        self.assertEqual(cost["claimValue"], "1578.95")
        self.assertEqual(cost["normalLossQty"], "5.0000")
        self.assertEqual(cost["outstandingClaim"], "1578.95")
        # Every rupee lands once: good stock + claim (+ rounding) = total.
        self.assertAlmostEqual(
            float(cost["unsoldInventoryValue"]) + float(cost["claimValue"]), 10000.0, delta=0.05
        )

        loss_id = cost["losses"][0]["id"]
        patch = lambda body: self.api.patch(f"/api/v1/inventory/batch-losses/{loss_id}/", body, format="json")
        self.assertEqual(patch({"claimStatus": "PARTLY_RECOVERED", "recoveredAmount": "1000"}).status_code, 200)
        cost = patch({"recoveredAmount": "1000"}).json()["costing"]       # re-save: still 1000, not 2000
        self.assertEqual(cost["recoveredAmount"], "1000.00")
        self.assertEqual(cost["outstandingClaim"], "578.95")
        cost = patch({"claimStatus": "WRITTEN_OFF"}).json()["costing"]
        self.assertEqual(cost["writtenOffLoss"], "578.95")
        self.assertEqual(cost["outstandingClaim"], "0.00")

    def test_cannot_classify_more_than_was_rejected(self):
        self.assertEqual(self.api.post(self.url, {"quantity": "12", "cause": "NORMAL_LOSS"}, format="json").status_code, 201)
        over = self.api.post(self.url, {"quantity": "9", "cause": "NORMAL_LOSS"}, format="json")
        self.assertEqual(over.status_code, 400, over.content)

    def test_claim_needs_the_responsible_party(self):
        resp = self.api.post(self.url, {"quantity": "5", "cause": "JOB_WORKER_FAULT"}, format="json")
        self.assertEqual(resp.status_code, 400, resp.content)


class ProductionAccountingTests(InHouseProductionTests):
    """Yarn issue -> WIP, labour absorbed into WIP, output -> Inventory; all reconciled."""

    def balance(self, key):
        from apps.accounting.models import JournalLine

        lines = JournalLine.objects.filter(
            journal_entry__client=self.tenant, account__system_key=key, journal_entry__deleted_at__isnull=True,
        ).exclude(journal_entry__status="Reversed").filter(journal_entry__reversal_of__isnull=True)
        return sum((l.debit - l.credit for l in lines), Decimal("0"))

    def test_journals_flow_through_wip_and_reconcile(self):
        run = self.run_flow()
        acct = run["accounting"]
        self.assertTrue(acct["reconciled"], acct)
        self.assertEqual(acct["wipBalance"], "0.00")
        self.assertEqual(acct["materialIssuedValue"], "30000.00")
        self.assertEqual(acct["costPostedToBatch"], "35000.00")
        self.assertEqual(len(acct["entries"]), 3)          # issue, labour, completion
        for entry in acct["entries"]:
            self.assertEqual(sum(Decimal(l["debit"]) for l in entry["lines"]),
                             sum(Decimal(l["credit"]) for l in entry["lines"]))
        self.assertEqual(self.balance("wip"), Decimal("0"))
        self.assertEqual(self.balance("production_absorbed"), Decimal("-5000.00"))
        # Inventory: yarn out 30,000, fabric in 35,000 (the purchase itself had no journal here).
        self.assertEqual(self.balance("stock"), Decimal("5000.00"))
        self.assertIn(run["batchCode"], acct["entries"][-1]["narration"])

    def test_customer_run_cost_goes_to_cost_of_service_not_stock(self):
        self.run_flow(customer=self.customer)
        self.assertEqual(self.balance("cogs"), Decimal("35000.00"))
        self.assertEqual(self.balance("stock"), Decimal("-30000.00"))   # only OSCAR's yarn left stock

    def test_customer_supplied_yarn_has_no_entry(self):
        rid = self.api.post(self.base, {"outputItemId": str(self.item.id), "plannedQty": "10"}, format="json").json()["id"]
        r = self.api.post(f"{self.base}{rid}/issue/", {
            "itemId": str(self.yarn.id), "locationId": str(self.godown.id), "quantity": "10", "customerSupplied": True,
        }, format="json").json()
        self.assertEqual(r["accounting"]["entries"], [])

    def test_cancel_reverses_stock_and_journals(self):
        from apps.inventory.models import StockBalance

        rid = self.api.post(self.base, {"outputItemId": str(self.item.id), "plannedQty": "100"}, format="json").json()["id"]
        act = lambda name, payload: self.api.post(f"{self.base}{rid}/{name}/", payload, format="json")
        act("issue", {"itemId": str(self.yarn.id), "locationId": str(self.godown.id), "quantity": "50"})
        act("expense", {"costType": "Production labour", "amount": "1000"})
        act("output", {"rolls": [{"meters": "100"}]})
        resp = act("cancel", {"reason": "wrong yarn"})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["status"], "Cancelled")
        self.assertEqual(self.balance("wip"), Decimal("0"))
        self.assertEqual(self.balance("stock"), Decimal("0"))
        yarn = sum(b.on_hand for b in StockBalance.objects.filter(item=self.yarn))
        self.assertEqual(yarn, Decimal("200.0000"))
        self.assertEqual(act("cancel", {}).status_code, 409)      # cannot cancel twice

    def test_profit_and_loss_shows_absorbed_labour_as_contra_not_double(self):
        from apps.accounting.reports import profit_and_loss

        self.run_flow()
        pl = profit_and_loss(self.tenant.id)
        absorbed = [a for a in pl["expenseAccounts"] if a["code"] == "5610"]
        self.assertEqual(len(absorbed), 1)
        self.assertEqual(absorbed[0]["amount"], Decimal("-5000.00"))   # labour moved into WIP / stock
        self.assertEqual(pl["operatingExpenses"], Decimal("-5000.00"))   # payroll (not run here) books the +5000
        self.assertEqual(pl["cogs"], Decimal("0"))                       # nothing sold yet

    def test_entries_are_not_duplicated_by_retrying_completion(self):
        from apps.accounting.models import JournalEntry

        run = self.run_flow()
        before = JournalEntry.objects.filter(client=self.tenant).count()
        self.api.post(f"{self.base}{run['id']}/complete/", {}, format="json")
        self.assertEqual(JournalEntry.objects.filter(client=self.tenant).count(), before)


class CustomerMaterialTests(BatchTestBase):
    """Customer material is held, not owned: no value, no journal, apart in reports."""

    def receive_customer(self, meters="200"):
        resp = self.api.post("/api/v1/inventory/batches/customer-receipt/", {
            "customerId": str(self.customer.id), "itemId": str(self.item.id),
            "locationId": str(self.godown.id), "rolls": [{"meters": meters}], "reference": "Customer DC 12",
        }, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        return resp.json()

    def test_receipt_posts_no_value_and_is_kept_out_of_valuation(self):
        from apps.accounting.models import JournalEntry

        own = self.receive(lot="OWN", meters="100")              # OSCAR stock at 50/m
        body = self.receive_customer("200")
        self.assertEqual(body["ownership"], "CUSTOMER")
        self.assertFalse(JournalEntry.objects.filter(client=self.tenant).exists())
        valuation = self.api.get("/api/v1/inventory/valuation/").json()
        self.assertEqual(Decimal(str(valuation["aggregates"]["totalValuation"])), Decimal("5000.00"))
        self.assertEqual(Decimal(str(valuation["aggregates"]["customerOwnedQtyExcluded"])), Decimal("200"))
        self.assertTrue(all(r["lotNo"] != body["lots"][0] for r in valuation["results"]))

        summary = {r["ownership"]: r for r in self.api.get("/api/v1/inventory/batches/physical-stock/").json()["results"]}
        self.assertEqual(summary["OSCAR"]["available"], "100.0000")
        self.assertEqual(summary["CUSTOMER"]["available"], "200.0000")
        self.assertEqual(summary["CUSTOMER"]["ownerPartyName"], "Buyer Co")
        self.assertIsNotNone(own.batch_id)

    def test_deliver_back_to_owner(self):
        body = self.receive_customer("200")
        taka_id = body["takas"][0]["id"]
        resp = self.api.post(f"/api/v1/inventory/batches/{body['batchCode']}/deliver/", {
            "rolls": [{"lotTakaId": taka_id, "qty": "150"}],
        }, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        summary = {r["ownership"]: r for r in self.api.get("/api/v1/inventory/batches/physical-stock/").json()["results"]}
        self.assertEqual(summary["CUSTOMER"]["available"], "50.0000")
        self.assertEqual(summary["CUSTOMER"]["deliveredBack"], "150.0000")
        own = self.receive(lot="OWN2")
        refused = self.api.post(f"/api/v1/inventory/batches/{own.batch.batch_code}/deliver/", {
            "rolls": [{"lotTakaId": str(own.id), "qty": "10"}],
        }, format="json")
        self.assertEqual(refused.status_code, 422, refused.content)

    def test_bought_batch_cannot_become_customer_material(self):
        taka = self.receive(lot="BOUGHT")
        LotCostEntry.objects.create(client=self.tenant, lot_number="BOUGHT", cost_type="Grey purchase", amount=Decimal("5000"))
        resp = self.api.patch(f"/api/v1/inventory/batches/{taka.batch.batch_code}/", {
            "ownership": "CUSTOMER", "ownerPartyId": str(self.customer.id),
        }, format="json")
        self.assertEqual(resp.status_code, 400, resp.content)


class QCPurchaseReturnTests(BatchTestBase):
    """QC-rejected purchased rolls go back to the supplier as a debit note."""

    def setUp(self):
        super().setUp()
        from apps.accounting import services as ledger
        from apps.purchase.models import (
            GoodsReceipt, GoodsReceiptLine, PurchaseBill, PurchaseBillLine, PurchaseOrder,
        )

        ledger.seed_chart_of_accounts(self.tenant)
        self.item.tracking_type = "TAKA"
        self.item.save(update_fields=["tracking_type"])
        po = PurchaseOrder.objects.create(client=self.tenant, party=self.vendor, po_number="PO-R1", doc_date=date(2026, 9, 1))
        self.bill = PurchaseBill.objects.create(
            client=self.tenant, party=self.vendor, party_name=self.vendor.name, bill_number="BILL-R1",
            doc_date=date(2026, 9, 2), purchase_order=po,
        )
        self.bill_line = PurchaseBillLine.objects.create(
            client=self.tenant, purchase_bill=self.bill, line_no=1, item=self.item, item_name=self.item.name,
            qty=Decimal("100"), rate=Decimal("50"), amount=Decimal("5000"), tax_pct=Decimal("5"),
            landed_unit_cost=Decimal("50"),
        )
        receipt = GoodsReceipt.objects.create(
            client=self.tenant, party=self.vendor, purchase_order=po, purchase_bill=self.bill,
            grn_number="GRN-R1", receipt_date=date(2026, 9, 3), location=self.godown,
        )
        grn_line = GoodsReceiptLine.objects.create(goods_receipt=receipt, client=self.tenant, item=self.item,
                                                   purchase_bill_line=self.bill_line)
        self.taka = LotTaka.objects.create(
            client=self.tenant, item=self.item, location=self.godown, lot_number="L-R1", taka_number="1",
            taka_id="TAK-R1", received_quantity=Decimal("100"), source_document_type="GRN",
            source_document_id=receipt.id, source_document_number="GRN-R1", source_line_id=grn_line.id,
        )
        stock.post_movement(
            client_id=self.tenant.id, item=self.item, location=self.godown, type="PURCHASE",
            quantity=Decimal("100"), unit_cost=Decimal("50"), reference_type="GoodsReceipt",
            reference_id=receipt.id, reference_number="GRN-R1", lot_taka=self.taka,
        )
        self.rejected, _ = stock.relocate_lot(
            client_id=self.tenant.id, lot=self.taka, quantity=Decimal("30"),
            to_location_id=stock.system_location(self.tenant.id, "REJECT").id,
            out_type="QC_REJECT", in_type="QC_REJECT", reference_type="QCInspection",
            reference_id=uuid.uuid4(), reference_number="QC-R1",
        )
        self.url = "/api/v1/inventory/qc/purchase-return/"

    def test_partial_return_creates_linked_debit_note_once(self):
        from apps.accounting.models import JournalLine
        from apps.purchase.models import PurchaseReturn

        resp = self.api.post(self.url, {
            "rolls": [{"lotTakaId": str(self.rejected.id), "qty": "20"}], "reason": "Holes and shade variation",
        }, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        ret = PurchaseReturn.objects.get(pk=resp.json()["returns"][0]["id"])
        self.assertEqual(ret.purchase_bill_id, self.bill.id)
        self.assertEqual(ret.total, Decimal("1050.00"))           # 20 m x 50 + 5% GST
        self.bill_line.refresh_from_db()
        self.assertEqual(self.bill_line.returned_qty, Decimal("20.0000"))
        self.rejected.refresh_from_db()
        self.assertEqual(self.rejected.available_quantity, Decimal("10.0000"))
        lines = {l.account.system_key: (l.debit, l.credit) for l in JournalLine.objects.filter(journal_entry=ret.journal_entry)}
        self.assertEqual(lines["creditors"], (Decimal("1050.00"), Decimal("0")))
        self.assertEqual(lines["stock"], (Decimal("0"), Decimal("1000.00")))
        self.assertEqual(lines["gst_input"], (Decimal("0"), Decimal("50.00")))
        # The batch history still shows it.
        kinds = [e.get("type") for e in batch_services.batch_timeline(self.taka.batch)]
        self.assertIn("PURCHASE_RETURN", kinds)
        # Only 10 m of rejected stock is left: returning 15 again is refused.
        again = self.api.post(self.url, {
            "rolls": [{"lotTakaId": str(self.rejected.id), "qty": "15"}], "reason": "again",
        }, format="json")
        self.assertEqual(again.status_code, 422, again.content)

    def test_good_stock_cannot_be_returned_from_qc(self):
        resp = self.api.post(self.url, {
            "rolls": [{"lotTakaId": str(self.taka.id), "qty": "5"}], "reason": "not rejected",
        }, format="json")
        self.assertEqual(resp.status_code, 422, resp.content)


class ManualBatchTests(BatchTestBase):
    """A batch added by hand: generated code, stock, cost and ledger together."""

    def setUp(self):
        super().setUp()
        from apps.accounting import services as ledger

        ledger.seed_chart_of_accounts(self.tenant)

    def add(self, **extra):
        body = {"itemId": str(self.item.id), "locationId": str(self.godown.id),
                "rolls": [{"meters": "100"}, {"meters": "50"}], "rate": "40", "reason": "Opening stock"}
        body.update(extra)
        return self.api.post("/api/v1/inventory/batches/", body, format="json")

    def test_code_is_generated_and_stock_cost_ledger_agree(self):
        from apps.accounting.models import JournalLine

        resp = self.add(batchCode="MY-OWN-CODE")              # a typed code is ignored
        self.assertEqual(resp.status_code, 201, resp.content)
        body = resp.json()
        self.assertRegex(body["batchCode"], r"^OSCAR-GRY-\d{4}-\d{4}$")
        self.assertEqual(body["origin"], "MANUAL")
        self.assertEqual(body["quantities"]["available"], "150.0000")
        self.assertEqual(body["costing"]["totalCost"], "6000.00")
        self.assertEqual(body["costing"]["costPerUnit"], "40.0000")
        lines = JournalLine.objects.filter(journal_entry__source_document_type="ManualBatch")
        self.assertEqual({l.account.system_key: (l.debit, l.credit) for l in lines},
                         {"stock": (Decimal("6000.00"), Decimal("0")), "stock_adjustment": (Decimal("0"), Decimal("6000.00"))})
        second = self.add().json()["batchCode"]
        self.assertNotEqual(second, body["batchCode"])

    def test_hsn_typed_on_the_batch_fills_the_item_master_only_when_missing(self):
        self.assertEqual(self.add(hsnCode=" 52083190 ").status_code, 201)
        self.item.refresh_from_db()
        self.assertEqual(self.item.hsn_code, "52083190")
        self.assertEqual(self.add(hsnCode="99999999").status_code, 201)
        self.item.refresh_from_db()
        self.assertEqual(self.item.hsn_code, "52083190")

    def test_rate_and_reason_are_required(self):
        self.assertEqual(self.add(rate="0").status_code, 400)
        self.assertEqual(self.add(reason="").status_code, 400)

    def test_send_to_qc_holds_the_rolls(self):
        body = self.add(sendToQc=True).json()
        self.assertEqual(body["quantities"]["qcHold"], "150.0000")
        self.assertEqual(body["quantities"]["available"], "0.0000")


class TakaRegistrationTests(ManualBatchTests):
    """Batch first as total metres, takas registered out of it later."""

    def bulk_batch(self):
        resp = self.add(rolls=[], totalMeters="1000")
        self.assertEqual(resp.status_code, 201, resp.content)
        return resp.json()

    def register(self, code, metres):
        return self.api.post(f"/api/v1/inventory/batches/{code}/register-takas/",
                             {"takas": [{"meters": m} for m in metres]}, format="json")

    def test_takas_are_cut_from_the_batch_and_the_rest_stays_unregistered(self):
        from apps.accounting.models import JournalEntry

        body = self.bulk_batch()
        code = body["batchCode"]
        self.assertEqual(body["quantities"]["unregistered"], "1000.0000")
        journals = JournalEntry.objects.count()

        resp = self.register(code, ["102", "98.5", "110"])
        self.assertEqual(resp.status_code, 201, resp.content)
        after = resp.json()
        self.assertEqual(after["batchCode"], code)                       # same batch
        self.assertEqual(after["quantities"]["unregistered"], "689.5000")
        self.assertEqual(after["quantities"]["available"], "1000.0000")  # nothing lost
        self.assertEqual(after["quantities"]["received"], "1000.0000")
        new = [t for t in after["takas"] if t["takaNumber"] in ("1", "2", "3")]
        self.assertEqual(sorted(t["availableQuantity"] for t in new), ["102.0000", "110.0000", "98.5000"])
        self.assertEqual(len({t["takaId"] for t in new}), 3)
        self.assertEqual(after["costing"]["totalCost"], "40000.00")      # cost unchanged
        self.assertEqual(JournalEntry.objects.count(), journals)         # no new journal

        more = self.register(code, ["50"]).json()
        self.assertEqual(more["quantities"]["unregistered"], "639.5000")
        self.assertIn("4", {t["takaNumber"] for t in more["takas"]})

    def test_cannot_register_more_than_is_left(self):
        code = self.bulk_batch()["batchCode"]
        self.assertEqual(self.register(code, ["600", "500"]).status_code, 422)

    def test_bulk_metres_are_not_sold_before_takas_exist(self):
        from types import SimpleNamespace

        from apps.core.exceptions import BusinessRuleViolation
        from apps.sales.services import allocate_takas

        self.item.tracking_type = "TAKA"
        self.item.save(update_fields=["tracking_type"])
        code = self.bulk_batch()["batchCode"]
        bulk = LotTaka.objects.get(batch__batch_code=code)
        line = SimpleNamespace(item=self.item, item_id=self.item.id, item_name=self.item.name,
                               taka_allocations=[{"lotTakaId": str(bulk.id), "qty": "10"}], reserved_takas=[])
        with self.assertRaises(BusinessRuleViolation):
            allocate_takas(self.tenant.id, line, Decimal("10"), self.godown.id)
        self.register(code, ["100"])
        line.taka_allocations = []
        drawn = allocate_takas(self.tenant.id, line, Decimal("10"), self.godown.id)
        self.assertEqual(drawn[0][0].taka_number, "1")


class BatchHsnTests(BatchTestBase):
    """A batch shows its fabric's HSN from the item master; nothing typed on the batch."""

    def test_hsn_comes_from_item_then_category(self):
        from apps.masters.models import ItemCategory

        taka = self.receive(lot="H1")
        body = batch_services.serialize_batch(taka.batch)
        self.assertIsNone(body["hsnCode"])
        self.assertEqual(body["hsnMissingFor"], [self.item.name])

        category = ItemCategory.objects.create(client=self.tenant, name="Cotton", default_hsn_code="5208")
        self.item.category = category
        self.item.save(update_fields=["category"])
        self.assertEqual(batch_services.serialize_batch(taka.batch)["hsnCode"], "5208")

        self.item.hsn_code = "52083190"
        self.item.save(update_fields=["hsn_code"])
        body = self.api.get(f"/api/v1/inventory/batches/{taka.batch.batch_code}/").json()
        self.assertEqual(body["hsnCode"], "52083190")
        self.assertEqual(body["hsnMissingFor"], [])


class StockPositionScreenTests(BatchTestBase):
    """The six Stock Position datasets come from where rolls are; nothing double-counted."""

    def setUp(self):
        super().setUp()
        from apps.accounting import services as ledger

        ledger.seed_chart_of_accounts(self.tenant)
        self.good = self.receive(lot="SP-GOOD", meters="100")
        self.good.qc_status = "Passed"
        self.good.save(update_fields=["qc_status"])
        bad = self.receive(lot="SP-BAD", meters="40")
        self.rejected, _ = stock.relocate_lot(
            client_id=self.tenant.id, lot=bad, quantity=Decimal("40"),
            to_location_id=stock.system_location(self.tenant.id, "REJECT").id,
            out_type="QC_REJECT", in_type="QC_REJECT", reference_type="QCInspection",
            reference_id=uuid.uuid4(), reference_number="QC-SP",
        )
        self.api_get = lambda screen, **q: self.api.get(f"/api/v1/inventory/stock-position/{screen}/", q)

    def test_overview_buckets_and_customer_material_kept_apart(self):
        self.api.post("/api/v1/inventory/batches/customer-receipt/", {
            "customerId": str(self.customer.id), "itemId": str(self.item.id),
            "locationId": str(self.godown.id), "totalMeters": "30",
        }, format="json")
        body = self.api_get("overview").json()
        k = body["kpis"]
        self.assertEqual(k["physical"], "170.0000")
        self.assertEqual(k["available"], "100.0000")          # OSCAR-owned only
        self.assertEqual(k["customerHeld"], "30.0000")
        self.assertEqual(k["qcHoldRejected"], "40.0000")
        self.assertTrue(body["canSeeCost"])
        only_rejected = self.api_get("overview", status="rejected").json()
        self.assertEqual(only_rejected["count"], 1)

    def test_usable_excludes_rejected_customer_and_bulk_from_sale(self):
        self.add_bulk = self.api.post("/api/v1/inventory/batches/", {
            "itemId": str(self.item.id), "locationId": str(self.godown.id), "totalMeters": "500",
            "rate": "40", "reason": "Opening",
        }, format="json")
        k = self.api_get("usable").json()["kpis"]
        self.assertEqual(k["forSale"], "100.0000")              # bulk 500 not sellable until takas
        self.assertEqual(k["forProduction"], "600.0000")

    def test_rework_leaves_scrap_and_disposal_posts_once(self):
        from apps.accounting.models import JournalLine

        before = self.api_get("scrap").json()["kpis"]
        self.assertEqual(before["qcRejected"], "40.0000")
        r = self.api.post("/api/v1/inventory/stock-position/scrap/route-rework/", {
            "lotTakaId": str(self.rejected.id), "quantity": "15", "reason": "Can be re-dyed"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        after = self.api_get("scrap").json()["kpis"]
        self.assertEqual(after["qcRejected"], "25.0000")          # not double-counted
        self.assertEqual(self.api_get("reprocessing").json()["kpis"]["awaitingReprocessInStore"], "15.0000")

        self.rejected.refresh_from_db()
        LotTaka.objects.filter(pk=self.rejected.pk).update(rate=Decimal("50"))
        d = self.api.post("/api/v1/inventory/stock-position/scrap/dispose/", {
            "lotTakaId": str(self.rejected.id), "quantity": "25", "reason": "Approved by owner"}, format="json")
        self.assertEqual(d.status_code, 200, d.content)
        self.assertEqual(self.api_get("scrap").json()["kpis"].get("qcRejected", "0"), "0")
        lines = JournalLine.objects.filter(journal_entry__source_document_type="StockAdjustment")
        self.assertEqual(sum(l.debit for l in lines), Decimal("1250.00"))
        again = self.api.post("/api/v1/inventory/stock-position/scrap/dispose/", {
            "lotTakaId": str(self.rejected.id), "quantity": "1", "reason": "x"}, format="json")
        self.assertEqual(again.status_code, 422)

    def test_csv_export_and_unknown_screen(self):
        resp = self.api_get("overview", export="csv")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"batchCode", resp.content)
        self.assertEqual(self.api_get("nope").status_code, 404)

    def test_all_screens_respond(self):
        for screen in ("overview", "samples", "scrap", "usable", "reprocessing", "customer-returns"):
            self.assertEqual(self.api_get(screen).status_code, 200, screen)


class DashboardBatchProfitTests(BatchTestBase):
    """The business dashboard lists profit / loss per batch, losses first."""

    def sell(self, taka, qty, rate):
        from apps.sales.models import SalesInvoice, SalesInvoiceLine

        invoice = SalesInvoice.objects.create(
            client=self.tenant, party=self.customer, party_name=self.customer.name,
            doc_date=timezone.localdate(), invoice_number=f"INV-{uuid.uuid4().hex[:5]}",
            status="Unpaid", posted_at=timezone.now(),
        )
        SalesInvoiceLine.objects.create(
            sales_invoice=invoice, client=self.tenant, item=self.item, qty=Decimal(qty),
            rate=Decimal(rate), amount=Decimal(qty) * Decimal(rate),
        )
        stock.post_movement(
            client_id=self.tenant.id, item=self.item, location=self.godown, type="SALE",
            quantity=-Decimal(qty), reference_type="SalesInvoice", reference_id=invoice.id,
            reference_number=invoice.invoice_number, lot_taka=taka,
        )

    def test_loss_making_batch_leads_and_cost_is_ledger_gated(self):
        from apps.reports import dashboard

        good = self.receive(lot="L-GOOD", meters="100")
        bad = self.receive(lot="L-BAD", meters="100")
        self.receive(lot="L-UNSOLD", meters="100")
        for lot in ("L-GOOD", "L-BAD", "L-UNSOLD"):
            LotCostEntry.objects.create(client=self.tenant, lot_number=lot, cost_type="Grey purchase", amount=Decimal("5000"))
        self.sell(good, "100", "80")   # 8,000 - 5,000 = +3,000
        self.sell(bad, "100", "40")    # 4,000 - 5,000 = -1,000

        result = dashboard._batch_profit(self.tenant.id, timezone.localdate())
        self.assertEqual(result["batches"], 2)               # the unsold batch is left out
        self.assertEqual((result["profitable"], result["lossMaking"]), (1, 1))
        self.assertEqual(result["totalLoss"], Decimal("-1000.00"))
        self.assertEqual(result["net"], Decimal("2000.00"))
        worst = result["worst"][0]
        self.assertEqual(worst["batchCode"], bad.batch.batch_code)
        self.assertEqual(worst["netResult"], "-1000.00")
        self.assertEqual(worst["marginPct"], Decimal("-25.00"))
        self.assertEqual([r["batchCode"] for r in result["best"]], [good.batch.batch_code])

        body = self.api.get("/api/v1/reports/sales-dashboard/").json()
        self.assertEqual(body["batchProfit"]["lossMaking"], 1)
        self.assertNotIn("grossProfit", body["kpis"])

        self.assertIsNone(dashboard.business_dashboard(self.tenant.id)["batchProfit"])
