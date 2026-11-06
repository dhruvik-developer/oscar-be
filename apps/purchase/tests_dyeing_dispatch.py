"""Flow 2: grey fabric at OSCAR -> dyeing mill -> OSCAR warehouse.

The PO is a dyeing order on the mill for batches of a grey fabric PO.

Run with::

    python manage.py test apps.purchase.tests_dyeing_dispatch
"""
from decimal import Decimal

from apps.inventory import services as stock
from apps.inventory.models import Batch, LotTaka, StockMovement
from apps.purchase.models import GoodsReceipt, PurchaseOrder, PurchaseOrderCostAllocation, VendorBill
from apps.purchase.tests_dyeing_colour import MILL, WAREHOUSE, DyeingColourTestBase

FLOW2 = "OSCAR_TO_MILL"
DYE_RATE = 15


class DyeingDispatchTestBase(DyeingColourTestBase):
    def grey_po(self, qtys=(2000, 2000)):
        po = self.create_po(WAREHOUSE, qty=sum(qtys))
        for index, qty in enumerate(qtys):
            receipt = self.grn(po, qty, f"CH-{po.po_number}-{index}")
            self.qc(receipt["grnNumber"], qty)
        return po

    def order_payload(self, grey, rows, **extra):
        return {
            "vendorId": str(self.mill.id), "date": "2026-10-25", "fabricFlow": FLOW2,
            "dyeingColourName": "Navy Blue", "dyeingColourHex": "#1F3A5F",
            "sourcePurchaseOrderId": str(grey.id),
            "dyeingBatches": [{"batchId": str(batch.id)} for batch, _ in rows],
            "lineItems": [
                {"itemId": str(batch.item_id), "qty": qty, "rate": DYE_RATE, "uom": "Mtr"} for batch, qty in rows
            ],
            **extra,
        }

    def dyeing_order(self, grey, rows):
        res = self.api.post("/api/v1/purchase/orders/", self.order_payload(grey, rows), format="json")
        self.assertEqual(res.status_code, 201, res.content)
        po = PurchaseOrder.objects.get(pk=res.json()["id"])
        for step in ("submit-for-approval", "approve"):
            r = self.api.post(f"/api/v1/purchase/orders/{po.id}/{step}/", {}, format="json")
            self.assertEqual(r.status_code, 200, r.content)
        po.refresh_from_db()
        return po

    def order_for_all(self):
        grey = self.grey_po()
        one, two = self.batches(grey)
        return grey, one, two, self.dyeing_order(grey, [(one, 1000), (two, 500)])

    def dyeing_url(self, po, path=""):
        return f"/api/v1/purchase/orders/{po.id}/dyeing/{path}"

    def summary(self, po):
        res = self.api.get(self.dyeing_url(po))
        self.assertEqual(res.status_code, 200, res.content)
        return res.json()

    def take_back(self, po, dispatch_id, rows, key=None, expect=201, **extra):
        headers = {"HTTP_IDEMPOTENCY_KEY": key} if key else {}
        res = self.api.post(self.dyeing_url(po, f"dispatches/{dispatch_id}/returns/"), {
            "returnDate": "2026-10-30", "colourName": "Navy Blue", "colourHex": "#1F3A5F",
            "millChallanNo": "MC-1", "locationId": str(self.godown.id),
            "lines": [{"batchId": str(batch.id), "qty": qty} for batch, qty in rows],
            **extra,
        }, format="json", **headers)
        self.assertEqual(res.status_code, expect, res.content)
        return res.json()

    def godown_qty(self, batch):
        return sum(
            (t.available_quantity for t in LotTaka.objects.filter(batch=batch, location=self.godown, deleted_at__isnull=True)),
            Decimal("0"),
        )

    def mill_qty(self, batch):
        wip = stock.wip_location_for_party(self.tenant.id, self.mill)
        return sum(
            (t.available_quantity for t in LotTaka.objects.filter(batch=batch, location=wip, deleted_at__isnull=True)),
            Decimal("0"),
        )


class DyeingOrderTests(DyeingDispatchTestBase):
    def test_only_grey_pos_with_free_stock_are_offered(self):
        grey = self.grey_po()
        empty = self.create_po(WAREHOUSE, qty=100)
        mill_po = self.create_po(MILL, qty=100)
        self.grn(mill_po, 100, "CH-M")
        rows = self.api.get("/api/v1/purchase/orders/grey-sources/").json()["results"]
        self.assertEqual([r["id"] for r in rows], [str(grey.id)])
        self.assertEqual(Decimal(rows[0]["availableQty"]), Decimal("4000"))
        self.assertNotIn(str(empty.id), [r["id"] for r in rows])

    def test_batches_of_the_chosen_po_are_listed(self):
        grey = self.grey_po()
        rows = self.api.get(f"/api/v1/purchase/orders/{grey.id}/grey-batches/").json()["results"]
        self.assertEqual([r["batchCode"] for r in rows], [b.batch_code for b in self.batches(grey)])
        self.assertEqual({Decimal(r["availableToSend"]) for r in rows}, {Decimal("2000")})

    def test_order_sends_only_the_kept_batches(self):
        grey = self.grey_po()
        one, two = self.batches(grey)
        po = self.dyeing_order(grey, [(two, 500)])
        self.assertEqual(po.source_purchase_order_id, grey.id)
        self.assertEqual(po.party_id, self.mill.id)
        body = self.api.get(f"/api/v1/purchase/orders/{po.id}/").json()
        self.assertEqual(body["sourcePoNumber"], grey.po_number)
        self.assertEqual([b["batchCode"] for b in body["dyeingBatches"]], [two.batch_code])
        self.assertEqual(self.godown_qty(one), Decimal("2000"))
        self.assertEqual([b["batchCode"] for b in self.summary(po)["greyBatches"]], [two.batch_code])

    def test_colour_is_required_and_saved(self):
        grey = self.grey_po()
        one, _ = self.batches(grey)
        body = self.order_payload(grey, [(one, 10)])
        body.pop("dyeingColourName")
        self.assertEqual(self.api.post("/api/v1/purchase/orders/", body, format="json").status_code, 400)
        po = self.dyeing_order(grey, [(one, 10)])
        self.assertEqual(self.summary(po)["colourName"], "Navy Blue")

    def test_source_po_is_required(self):
        grey = self.grey_po()
        one, _ = self.batches(grey)
        body = self.order_payload(grey, [(one, 10)])
        body.pop("sourcePurchaseOrderId")
        self.assertEqual(self.api.post("/api/v1/purchase/orders/", body, format="json").status_code, 400)
        self.assertFalse(PurchaseOrder.objects.filter(fabric_flow=FLOW2).exists())

    def test_more_than_the_batch_holds_is_refused(self):
        grey = self.grey_po()
        one, _ = self.batches(grey)
        res = self.api.post("/api/v1/purchase/orders/", self.order_payload(grey, [(one, 2001)]), format="json")
        self.assertEqual(res.status_code, 400, res.content)

    def test_a_batch_of_another_po_is_refused(self):
        grey, other = self.grey_po(), self.grey_po((100,))
        batch, = self.batches(other)
        res = self.api.post("/api/v1/purchase/orders/", self.order_payload(grey, [(batch, 10)]), format="json")
        self.assertEqual(res.status_code, 400, res.content)

    def test_a_dyeing_po_is_not_a_source(self):
        grey, one, two, po = self.order_for_all()
        res = self.api.post("/api/v1/purchase/orders/", self.order_payload(po, [(one, 10)]), format="json")
        self.assertEqual(res.status_code, 400, res.content)

    def test_no_ordinary_grn_on_a_dyeing_order(self):
        grey, one, two, po = self.order_for_all()
        res = self.api.post("/api/v1/purchase/receipts/", {
            "partyId": str(self.mill.id), "locationId": str(self.godown.id), "purchaseOrderId": str(po.id),
            "supplierChallanNo": "X-1", "lines": [{"itemId": str(one.item_id), "receivedQty": 5}],
        }, format="json")
        self.assertEqual(res.status_code, 409, res.content)


class DispatchTests(DyeingDispatchTestBase):
    def test_a_draft_moves_nothing_and_approval_sends_the_listed_quantities(self):
        grey = self.grey_po()
        one, two = self.batches(grey)
        res = self.api.post("/api/v1/purchase/orders/", self.order_payload(grey, [(one, 1000), (two, 500)]), format="json")
        po = PurchaseOrder.objects.get(pk=res.json()["id"])
        self.assertEqual(self.godown_qty(one), Decimal("2000"))
        for step in ("submit-for-approval", "approve"):
            self.assertEqual(self.api.post(f"/api/v1/purchase/orders/{po.id}/{step}/", {}, format="json").status_code, 200)

        body = self.summary(po)
        self.assertEqual(Decimal(body["sentQty"]), Decimal("1500"))
        self.assertEqual(Decimal(body["atMillQty"]), Decimal("1500"))
        self.assertEqual(self.godown_qty(one), Decimal("1000"))
        self.assertEqual(self.godown_qty(two), Decimal("1500"))
        self.assertEqual(self.mill_qty(one), Decimal("1000"))
        self.assertEqual(self.mill_qty(two), Decimal("500"))
        dispatch = body["dispatches"][0]
        self.assertEqual(dispatch["status"], "Dispatched")
        self.assertEqual(dispatch["millName"], self.mill.name)
        self.assertTrue(dispatch["dispatchNumber"])
        self.assertEqual(len(self.batches(grey)), 2)
        self.assertFalse(Batch.objects.filter(client=self.tenant, fabric_type="DYED").exists())
        self.assertTrue(StockMovement.objects.filter(
            type="JOBWORK_OUTWARD", reference_number=dispatch["dispatchNumber"], lot_taka__batch=one,
        ).exists())
        self.assertEqual({t.batch_id for t in LotTaka.objects.filter(status="External WIP")}, {one.id, two.id})

    def test_it_is_sent_only_once(self):
        grey, one, two, po = self.order_for_all()
        self.assertEqual(self.api.post(self.dyeing_url(po, "dispatches/"), {}, format="json").status_code, 201)
        self.assertEqual(len(self.summary(po)["dispatches"]), 1)
        self.assertEqual(self.godown_qty(one), Decimal("1000"))

    def test_an_already_approved_order_can_still_go_out(self):
        grey = self.grey_po()
        one, _ = self.batches(grey)
        res = self.api.post("/api/v1/purchase/orders/", self.order_payload(grey, [(one, 100)]), format="json")
        po = PurchaseOrder.objects.get(pk=res.json()["id"])
        PurchaseOrder.objects.filter(pk=po.pk).update(status="Approved")
        self.assertEqual(self.api.post(self.dyeing_url(po, "dispatches/"), {}, format="json").status_code, 201)
        self.assertEqual(self.mill_qty(one), Decimal("100"))

    def test_two_orders_cannot_send_the_same_metres(self):
        grey = self.grey_po((1000,))
        batch, = self.batches(grey)
        self.dyeing_order(grey, [(batch, 800)])
        res = self.api.post("/api/v1/purchase/orders/", self.order_payload(grey, [(batch, 201)]), format="json")
        self.assertEqual(res.status_code, 400, res.content)

    def test_stock_gone_before_approval_stops_the_approval(self):
        grey = self.grey_po()
        one, _ = self.batches(grey)
        res = self.api.post("/api/v1/purchase/orders/", self.order_payload(grey, [(one, 10)]), format="json")
        po = PurchaseOrder.objects.get(pk=res.json()["id"])
        self.api.post(f"/api/v1/purchase/orders/{po.id}/submit-for-approval/", {}, format="json")
        LotTaka.objects.filter(batch=one).update(status="Reserved")
        res = self.api.post(f"/api/v1/purchase/orders/{po.id}/approve/", {}, format="json")
        self.assertEqual(res.status_code, 422, res.content)
        po.refresh_from_db()
        self.assertEqual(po.status, "Pending Approval")
        self.assertEqual(self.summary(po)["dispatches"], [])

    def test_order_with_fabric_at_the_mill_cannot_be_cancelled(self):
        grey, one, two, po = self.order_for_all()
        res = self.api.post(f"/api/v1/purchase/orders/{po.id}/cancel/", {}, format="json")
        self.assertEqual(res.status_code, 409, res.content)

    def test_other_flows_have_no_dyeing_dispatch(self):
        for flow in (WAREHOUSE, MILL):
            po = self.create_po(flow, qty=100)
            self.assertEqual(self.api.get(self.dyeing_url(po)).status_code, 422)


class ReturnTests(DyeingDispatchTestBase):
    def sent(self):
        grey, one, two, po = self.order_for_all()
        return grey, po, one, two, self.summary(po)["dispatches"][0]["id"]

    def test_return_opens_a_dyed_batch_linked_to_all_that_it_came_from(self):
        grey, po, one, two, dispatch = self.sent()
        body = self.take_back(po, dispatch, [(one, 600)], processingDetails="Reactive dye")

        dyed = Batch.objects.get(client=self.tenant, fabric_type="DYED")
        self.assertTrue(dyed.batch_code.startswith("OSCAR-DYD"))
        self.assertEqual(dyed.purchase_order_id, po.id)
        self.assertEqual(list(dyed.parent_batches.all()), [one])
        self.assertEqual(dyed.dyeing_colour_name, "Navy Blue")
        receipt = dyed.goods_receipt
        self.assertEqual(receipt.party_id, self.mill.id)
        self.assertEqual(receipt.qc_status, "Pending")
        row = body["dyedBatches"][0]
        self.assertEqual(row["greyBatchCode"], one.batch_code)
        self.assertEqual(row["dispatchNumber"], body["dispatches"][0]["dispatchNumber"])
        self.assertEqual(Decimal(row["availableQty"]), Decimal("0"))
        self.assertEqual(self.mill_qty(one), Decimal("400"))
        self.assertEqual(Decimal(body["atMillQty"]), Decimal("900"))
        self.assertEqual(body["dispatches"][0]["status"], "Partially Returned")
        po.refresh_from_db()
        self.assertEqual(po.status, "Partially Received")

    def test_the_grey_po_is_not_changed_by_the_return(self):
        grey, po, one, _, dispatch = self.sent()
        before = PurchaseOrder.objects.get(pk=grey.pk).status
        self.take_back(po, dispatch, [(one, 600)])
        grey.refresh_from_db()
        self.assertEqual(grey.status, before)
        self.assertEqual(grey.line_items.first().received_qty, Decimal("4000"))

    def test_dyed_stock_is_available_only_after_qc(self):
        grey, po, one, _, dispatch = self.sent()
        self.take_back(po, dispatch, [(one, 600)])
        dyed = Batch.objects.get(client=self.tenant, fabric_type="DYED")
        self.assertEqual(self.godown_qty(dyed), Decimal("0"))
        self.qc(dyed.goods_receipt.grn_number, 600)
        self.assertEqual(self.godown_qty(dyed), Decimal("600"))
        self.assertEqual(Decimal(self.summary(po)["dyedBatches"][0]["availableQty"]), Decimal("600"))

    def test_return_above_what_is_at_the_mill_is_refused(self):
        grey, po, one, _, dispatch = self.sent()
        self.assertEqual(self.take_back(po, dispatch, [(one, 1001)], expect=422)["code"], "OVER_RETURN")
        self.take_back(po, dispatch, [(one, 900)], millChallanNo="MC-1")
        self.assertEqual(self.take_back(po, dispatch, [(one, 101)], millChallanNo="MC-2", expect=422)["code"], "OVER_RETURN")
        self.assertEqual(Batch.objects.filter(client=self.tenant, fabric_type="DYED").count(), 1)

    def test_batch_not_on_the_dispatch_is_refused(self):
        grey = self.grey_po()
        one, two = self.batches(grey)
        po = self.dyeing_order(grey, [(one, 100)])
        self.take_back(po, self.summary(po)["dispatches"][0]["id"], [(two, 10)], expect=400)

    def test_colour_is_required(self):
        grey, po, one, _, dispatch = self.sent()
        self.take_back(po, dispatch, [(one, 10)], colourName="", expect=400)

    def test_replayed_return_makes_one_batch(self):
        grey, po, one, _, dispatch = self.sent()
        self.take_back(po, dispatch, [(one, 100)], key="r-1")
        self.take_back(po, dispatch, [(one, 100)], key="r-1")
        self.assertEqual(Batch.objects.filter(client=self.tenant, fabric_type="DYED").count(), 1)
        self.assertEqual(self.mill_qty(one), Decimal("900"))

    def test_full_return_finishes_the_order(self):
        grey, po, one, two, dispatch = self.sent()
        body = self.take_back(po, dispatch, [(one, 1000), (two, 500)])
        self.assertEqual(body["dispatches"][0]["status"], "Fully Returned")
        self.assertEqual(Decimal(body["atMillQty"]), Decimal("0"))
        self.assertEqual(Batch.objects.filter(client=self.tenant, fabric_type="DYED").count(), 2)
        po.refresh_from_db()
        self.assertEqual(po.status, "Fully Received")

    def test_fabric_value_moves_from_the_grey_batch_to_the_dyed_batch(self):
        grey, po, one, two, dispatch = self.sent()
        self.take_back(po, dispatch, [(one, 600)])
        dyed = Batch.objects.get(client=self.tenant, fabric_type="DYED")

        def booked(batch):
            rows = PurchaseOrderCostAllocation.objects.filter(batch=batch, expense__isnull=True, deleted_at__isnull=True)
            return sum((a.amount for a in rows), Decimal("0"))

        self.assertEqual(booked(one), Decimal("56000.00"))  # 1400 still grey x 40
        self.assertEqual(booked(dyed), Decimal("24000.00"))  # 600 dyed x 40
        self.assertEqual(booked(two), Decimal("80000.00"))
        self.assertEqual(GoodsReceipt.objects.filter(purchase_order=po, dyeing_outward__isnull=False).count(), 1)

    def test_the_mills_bill_charges_the_dyed_batch(self):
        grey, po, one, _, dispatch = self.sent()
        self.take_back(po, dispatch, [(one, 600)])
        dyed = Batch.objects.get(client=self.tenant, fabric_type="DYED")
        base = "/api/v1/purchase/vendor-bills/"
        details = self.api.get(f"{base}po-details/", {"poId": str(po.id)}).json()
        self.assertEqual([b["batchCode"] for b in details["batches"]], [dyed.batch_code])
        self.assertEqual(details["batches"][0]["originalPoRate"], float(DYE_RATE))
        res = self.api.post(base, {
            "purchaseOrderId": str(po.id), "vendorBillNumber": "MILL-1", "billDate": "2026-11-01", "gstPct": 5,
            "lineItems": [{"batchId": str(dyed.id), "itemId": str(dyed.item_id), "qty": 600, "rate": DYE_RATE}],
        }, format="json")
        self.assertEqual(res.status_code, 201, res.content)
        bill = VendorBill.objects.get(pk=res.json()["id"])
        self.assertEqual(bill.party_id, self.mill.id)
        self.assertEqual(self.api.post(f"{base}{bill.id}/recheck-matching/", {}, format="json").json()["overallStatus"], "Match")
        res = self.api.post(f"{base}{bill.id}/approve/", {"remarks": "", "createPayable": True}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        charged = PurchaseOrderCostAllocation.objects.filter(batch=dyed, expense__category="DYEING", deleted_at__isnull=True)
        self.assertEqual(sum((a.amount for a in charged), Decimal("0")), Decimal("9000.00"))


class InventoryListTests(DyeingDispatchTestBase):
    def codes(self, fabric_type):
        res = self.api.get("/api/v1/inventory/batches/", {"fabricType": fabric_type})
        self.assertEqual(res.status_code, 200, res.content)
        return {row["batchCode"] for row in res.json()["results"]}

    def test_one_batch_goes_to_dyeing_and_the_other_stays_grey(self):
        grey = self.grey_po()
        one, two = self.batches(grey)
        self.assertEqual(self.codes("GREY"), {one.batch_code, two.batch_code})
        po = self.dyeing_order(grey, [(one, 2000)])
        dispatch = self.summary(po)["dispatches"][0]["id"]
        self.assertEqual(self.codes("GREY"), {two.batch_code})
        self.assertEqual(self.codes("DYED"), {one.batch_code})

        self.take_back(po, dispatch, [(one, 2000)])
        dyed = Batch.objects.get(client=self.tenant, fabric_type="DYED")
        self.qc(dyed.goods_receipt.grn_number, 2000)
        self.assertEqual(self.codes("GREY"), {two.batch_code})
        self.assertEqual(self.codes("DYED"), {dyed.batch_code})

    def test_a_part_sent_shows_on_both_sides(self):
        grey = self.grey_po((1000,))
        batch, = self.batches(grey)
        self.dyeing_order(grey, [(batch, 400)])
        self.assertEqual(self.codes("GREY"), {batch.batch_code})
        self.assertEqual(self.codes("DYED"), {batch.batch_code})
        row = self.api.get("/api/v1/inventory/batches/", {"fabricType": "DYED"}).json()["results"][0]
        self.assertEqual(Decimal(row["quantities"]["withJobWorker"]), Decimal("400"))


class OtherFlowsTests(DyeingDispatchTestBase):
    def test_flow_1_is_unchanged(self):
        po = self.create_po(WAREHOUSE, qty=100)
        self.grn(po, 100, "CH-1")
        batch, = self.batches(po)
        self.assertEqual(batch.fabric_type, "GREY")

    def test_flow_3_is_unchanged(self):
        po = self.create_po(MILL, qty=100)
        self.grn(po, 100, "CH-1")
        batch, = self.batches(po)
        self.assertEqual(batch.fabric_type, "DYED")
        self.assertEqual(batch.dyeing_colour_name, "Navy Blue")
