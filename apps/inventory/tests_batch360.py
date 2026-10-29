"""Batch 360°: optional QC, grey / dyed batches, yarn traceability and
day-by-day production, all started from the batch.

    python manage.py test apps.inventory.tests_batch360
"""
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import User
from apps.inventory import batch_services
from apps.inventory import services as stock
from apps.inventory.models import Batch, LotCostEntry, LotTaka, StockBalance
from apps.masters.models import Item

from .tests_batches import BatchTestBase

BATCHES = "/api/v1/inventory/batches/"
RUNS = "/api/v1/inventory/production-runs/"


class Batch360TestBase(BatchTestBase):
    def manual(self, meters="100", **extra):
        body = {"itemId": str(self.item.id), "locationId": str(self.godown.id), "totalMeters": meters,
                "rate": "40", "reason": "Opening stock", **extra}
        r = self.api.post(BATCHES, body, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()

    def action(self, code, name, payload=None):
        return self.api.post(f"{BATCHES}{code}/actions/{name}/", payload or {}, format="json")


class OptionalQcTests(Batch360TestBase):
    def test_manual_batch_without_qc_is_usable_but_not_marked_passed(self):
        body = self.manual()
        self.assertEqual(body["fabricType"], "GREY")
        self.assertRegex(body["batchCode"], r"^OSCAR-GRY-\d{4}-\d{4}$")
        taka = LotTaka.objects.get(batch__batch_code=body["batchCode"])
        self.assertEqual(taka.qc_status, "Not Inspected")
        self.assertEqual(body["qc"]["state"], "QC Not Done")
        self.assertEqual(body["qc"]["now"]["notInspected"], "100.0000")
        self.assertEqual(body["qc"]["now"]["passed"], "0.0000")
        self.assertIn("permissions", body)
        # Sellable without QC: the sales batch picker offers it...
        rows = self.api.get(f"{BATCHES}eligible/?itemId={self.item.id}").json()["results"]
        self.assertEqual(rows, [])  # ...once registered as takas (bulk is never sold).
        self.api.post(f"{BATCHES}{body['batchCode']}/register-takas/", {"takas": [{"meters": "100"}]}, format="json")
        rows = self.api.get(f"{BATCHES}eligible/?itemId={self.item.id}").json()["results"]
        self.assertEqual(rows[0]["batchCode"], body["batchCode"])
        self.assertEqual(rows[0]["available"], "100.0000")

    def test_dyed_batch_gets_its_own_series_and_can_be_filtered(self):
        dyed = self.manual(fabricType="DYED")
        self.assertRegex(dyed["batchCode"], r"^OSCAR-DYD-\d{4}-\d{4}$")
        self.assertEqual(dyed["fabricTypeLabel"], "Dyed fabric")
        self.manual()
        listing = self.api.get(f"{BATCHES}?fabricType=DYED").json()
        self.assertEqual([r["batchCode"] for r in listing["results"]], [dyed["batchCode"]])
        bad = self.api.post(BATCHES, {"itemId": str(self.item.id), "locationId": str(self.godown.id),
                                      "totalMeters": "5", "rate": "1", "reason": "x", "fabricType": "BLUE"},
                            format="json")
        self.assertEqual(bad.status_code, 400, bad.content)

    def test_send_to_qc_then_record_result_with_waste_classified(self):
        code = self.manual()["batchCode"]
        sent = self.action(code, "send-to-qc", {"quantity": "60", "remarks": "Buyer wants inspection"})
        self.assertEqual(sent.status_code, 200, sent.content)
        body = sent.json()
        self.assertTrue(body["reference"].startswith("BQC-"))
        self.assertEqual(body["qc"]["state"], "Pending Inspection")
        self.assertEqual(body["qc"]["now"]["pending"], "60.0000")
        self.assertEqual(body["qc"]["now"]["notInspected"], "40.0000")

        # The split must cover everything waiting.
        short = self.action(code, "qc", {"goodQty": "10", "locationId": str(self.godown.id)})
        self.assertEqual(short.status_code, 400, short.content)

        done = self.action(code, "qc", {
            "goodQty": "45", "reprocessQty": "5", "rejectedQty": "8", "holdQty": "2",
            "locationId": str(self.godown.id), "remarks": "Holes",
            "loss": {"cause": "SUPPLIER_QUALITY", "responsiblePartyId": str(self.vendor.id), "outcome": "SCRAP"},
        })
        self.assertEqual(done.status_code, 200, done.content)
        body = done.json()
        q = body["quantities"]
        self.assertEqual(q["available"], "85.0000")      # 40 not inspected + 45 passed
        self.assertEqual(q["rework"], "5.0000")
        self.assertEqual(q["rejected"], "8.0000")
        self.assertEqual(q["qcHold"], "2.0000")
        self.assertEqual(body["qc"]["now"]["passed"], "45.0000")
        self.assertEqual(body["qc"]["now"]["onHold"], "2.0000")
        self.assertEqual(body["qc"]["inspected"], {"good": "45.0000", "second": "0.0000",
                                                   "reprocess": "5.0000", "reject": "8.0000"})
        self.assertEqual(body["qc"]["results"][0]["reference"], body["reference"])
        loss = body["costing"]["losses"][0]
        self.assertEqual((loss["quantity"], loss["cause"], loss["outcome"]), ("8.0000", "SUPPLIER_QUALITY", "SCRAP"))
        # Every metre is in exactly one place.
        total = sum(Decimal(q[k]) for k in ("available", "reserved", "qcHold", "rework", "secondQuality",
                                            "rejected", "withJobWorker"))
        self.assertEqual(total, Decimal(q["received"]))

        # Scrapping the rejected metres shows as waste on the batch.
        reject = LotTaka.objects.get(batch__batch_code=code, location__code="SYS-REJECT")
        r = self.api.post("/api/v1/inventory/stock-position/scrap/dispose/",
                          {"lotTakaId": str(reject.id), "quantity": "8", "reason": "Unusable"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(self.api.get(f"{BATCHES}{code}/").json()["qc"]["wasted"], "8.0000")

    def test_rework_done_returns_rolls_to_qc_with_cost(self):
        code = self.manual()["batchCode"]
        self.action(code, "send-to-qc")
        self.action(code, "qc", {"goodQty": "90", "reprocessQty": "10", "locationId": str(self.godown.id)})
        r = self.action(code, "rework-done", {"quantity": "10", "cost": "250", "remarks": "Re-washed"})
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertEqual(body["quantities"]["rework"], "0.0000")
        self.assertEqual(body["qc"]["now"]["pending"], "10.0000")
        self.assertEqual(body["costing"]["costByType"]["Reprocess"], "250.00")
        again = self.action(code, "rework-done", {})
        self.assertEqual(again.status_code // 100, 4, again.content)

    def test_nothing_to_send_and_purchase_qc_stays_on_purchase_screen(self):
        taka = self.receive()
        code = taka.batch.batch_code
        qc_hold = stock.system_location(self.tenant.id, "QC_HOLD")
        stock.relocate_lot(client_id=self.tenant.id, lot=taka, quantity=Decimal("100"), to_location_id=qc_hold.id,
                           out_type="QC_HOLD", in_type="QC_HOLD", reference_type="GoodsReceipt",
                           reference_id=uuid.uuid4(), qc_status="Pending")
        body = self.api.get(f"{BATCHES}{code}/").json()
        self.assertEqual(body["qc"]["purchaseQcPending"], "100.0000")
        self.assertEqual(body["qc"]["inspectableQty"], "0.0000")
        r = self.action(code, "qc", {"goodQty": "100", "locationId": str(self.godown.id)})
        self.assertGreaterEqual(r.status_code, 400)
        self.assertEqual(self.action(code, "send-to-qc").status_code // 100, 4)

    def test_qc_actions_need_qc_rights(self):
        code = self.manual()["batchCode"]
        clerk = User.objects.create_user(email="b360-clerk@example.com", password="pass", client=self.tenant,
                                         name="Clerk")
        with patch.object(User, "effective_permissions", lambda self: {"view_inventory"}):
            api = APIClient()
            api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(clerk)['access']}")
            r = api.post(f"{BATCHES}{code}/actions/send-to-qc/", {}, format="json")
            detail = api.get(f"{BATCHES}{code}/").json()
        self.assertEqual(r.status_code, 403, r.content)
        self.assertFalse(detail["permissions"]["qc"])
        self.assertFalse(detail["canSeeCost"])
        self.assertIsNone(detail["yarn"]["costedAmount"])


class DyeingFromBatchTests(Batch360TestBase):
    """The Dyeing tab's calls: job work order, outward with the rolls, inward of the dyed rolls."""

    def test_send_for_dyeing_and_receive_back_keeps_the_batch(self):
        code = self.manual(rolls=[{"meters": "100"}, {"meters": "80"}], totalMeters=None)["batchCode"]
        rolls = list(LotTaka.objects.filter(batch__batch_code=code).order_by("taka_number"))
        order = self.api.post("/api/v1/jobwork/orders/", {
            "process": "Dyeing", "vendorId": str(self.vendor.id), "vendor": self.vendor.name,
            "orderDate": str(timezone.localdate()), "rate": "12", "remarks": f"Batch {code}",
        }, format="json")
        self.assertEqual(order.status_code, 201, order.content)
        oid = order.json()["id"]
        out = self.api.post(f"/api/v1/jobwork/orders/{oid}/outwards/", {
            "date": str(timezone.localdate()), "qty": "180", "status": "Sent",
            "takas": [{"lotTakaId": str(t.id), "meters": str(t.available_quantity)} for t in rolls],
        }, format="json")
        self.assertEqual(out.status_code, 201, out.content)
        body = self.api.get(f"{BATCHES}{code}/").json()
        self.assertEqual(body["quantities"]["withJobWorker"], "180.0000")
        self.assertEqual(body["jobWork"][0]["sent"], "180.0000")

        back = self.api.post(f"/api/v1/jobwork/orders/{oid}/inwards/", {
            "date": str(timezone.localdate()), "qty": "95", "rejected": "0",
            "takas": [{"sourceTakaId": str(rolls[0].id), "meters": "95"}],
        }, format="json")
        self.assertEqual(back.status_code, 201, back.content)
        body = self.api.get(f"{BATCHES}{code}/").json()
        jw = body["jobWork"][0]
        self.assertEqual((jw["process"], jw["vendor"], jw["sent"], jw["received"], jw["pending"]),
                         ("Dyeing", self.vendor.name, "180.0000", "95.0000", "85.0000"))
        self.assertTrue(body["processed"])
        # The dyed roll is in the same batch, waiting at QC Hold -- and QC stays optional.
        self.assertEqual(body["qc"]["inspectableQty"], "95.0000")
        self.assertEqual(body["costing"]["costByType"]["Dyeing/Printing/Finishing"], "1140.00")  # 95 x 12
        r = self.action(code, "qc", {"goodQty": "95", "locationId": str(self.godown.id)})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["quantities"]["available"], "95.0000")


class RegistryStockFilterTests(Batch360TestBase):
    """The batch registry answers "how much navy is in Surat?" batch-wise."""

    def test_design_shade_godown_and_holding_filters_with_totals(self):
        from apps.masters.models import Design, Location, Shade

        navy = Shade.objects.create(client=self.tenant, code="NAVY", name="Navy")
        floral = Design.objects.create(client=self.tenant, code="FLORAL", name="Floral")
        surat = Location.objects.create(client=self.tenant, name="Surat", code="GD-SRT", type="Godown", is_active=True)
        a = self.manual("100", shadeId=str(navy.id), designId=str(floral.id))["batchCode"]
        b = self.manual("60", shadeId=str(navy.id), locationId=str(surat.id))["batchCode"]
        self.manual("40")

        body = self.api.get(f"{BATCHES}?shadeId={navy.id}").json()
        self.assertEqual(sorted(r["batchCode"] for r in body["results"]), sorted([a, b]))
        self.assertEqual(body["summary"]["available"], "160.0000")
        body = self.api.get(f"{BATCHES}?shadeId={navy.id}&locationId={surat.id}").json()
        self.assertEqual([r["batchCode"] for r in body["results"]], [b])
        self.assertEqual(body["summary"]["totalPhysical"], "60.0000")
        self.assertEqual([r["batchCode"] for r in self.api.get(f"{BATCHES}?designId={floral.id}").json()["results"]], [a])

        self.action(a, "send-to-qc", {"quantity": "30"})
        body = self.api.get(f"{BATCHES}?holding=qcHold").json()
        self.assertEqual([r["batchCode"] for r in body["results"]], [a])
        self.assertEqual(body["summary"]["qcHold"], "30.0000")
        everything = self.api.get(BATCHES).json()["summary"]
        self.assertEqual((everything["totalPhysical"], everything["available"], everything["qcHold"]),
                         ("200.0000", "170.0000", "30.0000"))


class YarnTraceabilityTests(Batch360TestBase):
    def setUp(self):
        super().setUp()
        self.yarn = Item.objects.create(client=self.tenant, name="Cotton Yarn 30s",
                                        sku=f"YRN-{uuid.uuid4().hex[:5]}", item_kind="Raw Material", uom="Kg")

    def test_hand_entered_yarn_is_recorded_and_validated(self):
        code = self.manual()["batchCode"]
        bad = self.action(code, "yarn", {"yarnItemId": str(self.yarn.id), "issuedQty": "10",
                                         "consumedQty": "9", "wasteQty": "2"})
        self.assertEqual(bad.status_code, 400, bad.content)
        r = self.action(code, "yarn", {"yarnItemId": str(self.yarn.id), "yarnLot": "YL-77", "yarnCount": "30s",
                                       "issuedQty": "50", "returnedQty": "4", "wasteQty": "1", "rate": "280"})
        self.assertEqual(r.status_code, 200, r.content)
        y = r.json()["yarn"]
        self.assertEqual(y["manual"][0]["consumed"], "45.0000")   # issued - returned - waste
        self.assertEqual(y["totals"], {"issued": "50.0000", "returned": "4.0000", "waste": "1.0000",
                                       "consumed": "45.0000"})
        self.assertIn("YARN", {e["kind"] for e in r.json()["timeline"]})
        # Traceability only: no stock moved, no cost added.
        self.assertFalse(StockBalance.objects.filter(item=self.yarn).exists())
        self.assertEqual(r.json()["costing"]["totalCost"], "4000.00")
        gone = self.action(code, "yarn-delete", {"yarnId": y["manual"][0]["id"]})
        self.assertEqual(gone.json()["yarn"]["manual"], [])


class DailyProductionTests(Batch360TestBase):
    def setUp(self):
        super().setUp()
        self.yarn = Item.objects.create(client=self.tenant, name="Cotton Yarn 40s",
                                        sku=f"YRN-{uuid.uuid4().hex[:5]}", item_kind="Raw Material", uom="Kg")
        stock.post_movement(
            client_id=self.tenant.id, item=self.yarn, location=self.godown, type="PURCHASE",
            quantity=Decimal("200"), unit_cost=Decimal("300"), reference_type="GoodsReceipt",
            reference_id=uuid.uuid4(), reference_number="GRN-Y1",
        )

    def test_run_opened_with_its_batch_takes_daily_output_and_yarn_returns(self):
        r = self.api.post(RUNS, {"outputItemId": str(self.item.id), "plannedQty": "500", "openBatch": True,
                                 "runDate": str(timezone.localdate() - timedelta(days=3))}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        run = r.json()
        code = run["batchCode"]
        self.assertRegex(code, r"^OSCAR-GRY-")
        batch = Batch.objects.get(client=self.tenant, batch_code=code)
        self.assertEqual((batch.origin, batch.fabric_type, batch.source_document_number),
                         ("PRODUCTION", "GREY", run["runNumber"]))
        act = lambda name, payload: self.api.post(f"{RUNS}{run['id']}/{name}/", payload, format="json")

        r = act("issue", {"itemId": str(self.yarn.id), "locationId": str(self.godown.id), "quantity": "60",
                          "yarnLot": "LOT-A1"})
        self.assertEqual(r.status_code, 200, r.content)
        issue_id = r.json()["issues"][0]["id"]
        day1, day2 = timezone.localdate() - timedelta(days=2), timezone.localdate() - timedelta(days=1)
        self.assertEqual(act("output", {"rolls": [{"meters": "100"}, {"meters": "95"}], "date": str(day1)}).status_code, 200)
        self.assertEqual(act("output", {"rolls": [{"meters": "110"}], "date": str(day2)}).status_code, 200)
        future = act("output", {"rolls": [{"meters": "1"}], "date": str(timezone.localdate() + timedelta(days=1))})
        self.assertEqual(future.status_code, 400, future.content)

        r = act("return", {"issueId": issue_id, "quantity": "8"})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["issues"][0]["returnedQty"], "8.0000")
        over = act("return", {"issueId": issue_id, "quantity": "53"})
        self.assertEqual(over.status_code // 100, 4, over.content)
        on_hand = sum(b.on_hand for b in StockBalance.objects.filter(item=self.yarn))
        self.assertEqual(on_hand, Decimal("148.0000"))   # 200 - 60 + 8

        body = self.api.get(f"{BATCHES}{code}/").json()
        self.assertEqual(LotTaka.objects.filter(batch=batch).count(), 3)   # every roll joined the opened batch
        self.assertEqual(Batch.objects.filter(client=self.tenant).count(), 1)
        prod = body["productionRuns"][0]
        self.assertEqual(prod["daily"], [{"date": str(day1), "rolls": 2, "meters": "195.0000"},
                                         {"date": str(day2), "rolls": 1, "meters": "110.0000"}])
        self.assertEqual(body["yarn"]["issues"][0]["yarnLot"], "LOT-A1")
        self.assertEqual(body["yarn"]["totals"]["issued"], "60.0000")
        self.assertEqual(body["yarn"]["totals"]["returned"], "8.0000")

        r = act("qc", {"goodQty": "300", "rejectedQty": "5", "locationId": str(self.godown.id)})
        self.assertEqual(r.status_code, 200, r.content)
        r = act("complete", {"wastageQty": "2", "wastageUom": "Kg"})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(r.json()["accounting"]["reconciled"], r.json()["accounting"])
        # Yarn cost on the batch is net of the return: 52 kg x 300.
        cost = LotCostEntry.objects.get(source_document_type="ProductionRun", cost_type="Yarn / raw material")
        self.assertEqual(cost.amount, Decimal("15600.0000"))
        body = self.api.get(f"{BATCHES}{code}/").json()
        self.assertEqual(body["yarn"]["totals"]["waste"], "2.0000")
        self.assertEqual(body["yarn"]["totals"]["consumed"], "50.0000")   # 60 - 8 - 2
        self.assertEqual(batch_services.batch_quantities(batch)["available"], Decimal("300.0000"))
