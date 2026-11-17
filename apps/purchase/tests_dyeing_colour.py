"""Dyeing colour on a fabric PO and the batch type a GRN opens from it.

    Test A  Factory -> OSCAR warehouse: grey batch, no colour
    Test B  Factory -> dyeing mill -> OSCAR: dyed batch per receipt, PO colour
    Test C  validation: colour required, wrong type refused, no duplicates

Receipts go through ``POST /purchase/receipts/``, the endpoint the GRN screens
use. Run with::

    python manage.py test apps.purchase.tests_dyeing_colour
"""
from decimal import Decimal

from apps.inventory.models import Batch, LotCostEntry, LotTaka
from apps.masters.models import Shade
from apps.purchase.models import GoodsReceipt, PurchaseBill, PurchaseOrder
from apps.purchase.tests_fabric_flows import FabricFlowTestBase

MILL = "FACTORY_TO_MILL"
WAREHOUSE = "FACTORY_TO_OSCAR"


class DyeingColourTestBase(FabricFlowTestBase):
    def order_body(self, flow, **extra):
        return {
            "vendorId": str(self.factory.id),
            "date": "2026-10-19",
            "fabricFlow": flow,
            "lineItems": [{"itemId": str(self.fabric.id), "qty": 4000, "rate": 40, "uom": "Mtr"}],
            **({"dyeingMillId": str(self.mill.id), "dyeingRate": 15} if flow == MILL else {}),
            **extra,
        }

    def grn(self, po, qty, challan, *, key=None, expect=201, rate=None, **extra):
        line = po.line_items.first()
        headers = {"HTTP_IDEMPOTENCY_KEY": key} if key else {}
        res = self.api.post("/api/v1/purchase/receipts/", {
            "partyId": str(po.party_id),
            "locationId": str(self.godown.id),
            "purchaseOrderId": str(po.id),
            "supplierChallanNo": challan,
            "receiptDate": "2026-10-25",
            "lines": [{"itemId": str(line.item_id), "poLineId": str(line.id), "receivedQty": qty,
                       **({"unitCost": rate} if rate else {})}],
            **extra,
        }, format="json", **headers)
        self.assertEqual(res.status_code, expect, res.content)
        return res.json()

    def registry_group(self, po):
        res = self.api.get("/api/v1/inventory/batches/groups/", {
            "groupBy": "po", "poId": str(po.id), "includePendingQc": "1",
        })
        self.assertEqual(res.status_code, 200, res.content)
        return next(g for g in res.json()["groups"] if g.get("poId") == str(po.id))


class WarehouseDeliveryTests(DyeingColourTestBase):
    """Test A."""

    def test_receipt_opens_a_grey_batch_without_colour(self):
        po = self.create_po(WAREHOUSE, qty=4000)
        self.assertIsNone(po.dyeing_colour_name)

        receipt = self.grn(po, 2000, "CH-A1")

        batch, = self.batches(po)
        self.assertEqual(batch.fabric_type, "GREY")
        self.assertTrue(batch.batch_code.startswith("OSCAR-GRY"), batch.batch_code)
        self.assertIsNone(batch.dyeing_colour_name)
        self.assertEqual(receipt["fabricType"], "GREY")
        self.assertIsNone(receipt["dyeingColourName"])

    def test_a_colour_sent_for_a_warehouse_order_is_not_kept(self):
        res = self.api.post("/api/v1/purchase/orders/", self.order_body(
            WAREHOUSE, dyeingColourName="Red", dyeingColourHex="#FF0000",
        ), format="json")
        self.assertEqual(res.status_code, 201, res.content)
        self.assertIsNone(res.json()["dyeingColourName"])
        self.assertIsNone(res.json()["dyeingColourHex"])

    def test_switching_a_draft_to_warehouse_clears_the_colour(self):
        po = self.api.post("/api/v1/purchase/orders/", self.order_body(
            MILL, dyeingColourName="Red", dyeingColourHex="#FF0000",
        ), format="json").json()
        res = self.api.patch(f"/api/v1/purchase/orders/{po['id']}/", {"fabricFlow": WAREHOUSE}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertIsNone(res.json()["dyeingColourName"])
        self.assertIsNone(PurchaseOrder.objects.get(pk=po["id"]).dyeing_colour_hex)


class DyeingMillDeliveryTests(DyeingColourTestBase):
    """Test B."""

    def test_colour_persists_on_po_detail_and_list(self):
        po = self.create_po(MILL, qty=4000)
        detail = self.api.get(f"/api/v1/purchase/orders/{po.id}/").json()
        self.assertEqual(detail["dyeingColourName"], "Navy Blue")
        self.assertEqual(detail["dyeingColourHex"], "#1F3A5F")
        rows = self.api.get("/api/v1/purchase/orders/").json()["results"]
        row = next(r for r in rows if r["id"] == str(po.id))
        self.assertEqual(row["dyeingColourName"], "Navy Blue")

    def test_custom_colour_needs_no_hex(self):
        res = self.api.post("/api/v1/purchase/orders/", self.order_body(
            MILL, dyeingColourName="  Customer Shade 204 ",
        ), format="json")
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.json()["dyeingColourName"], "Customer Shade 204")
        self.assertIsNone(res.json()["dyeingColourHex"])

    def test_two_partial_receipts_are_two_dyed_batches_of_one_po(self):
        po = self.create_po(MILL, qty=4000)

        first = self.grn(po, 2000, "CH-B1", rate=41)
        po.refresh_from_db()
        self.assertEqual(po.status, "Partially Received")
        second = self.grn(po, 2000, "CH-B2", rate=43)
        po.refresh_from_db()
        self.assertEqual(po.status, "Fully Received")

        one, two = self.batches(po)
        self.assertNotEqual(one.pk, two.pk)
        for batch in (one, two):
            self.assertEqual(batch.fabric_type, "DYED")
            self.assertTrue(batch.batch_code.startswith("OSCAR-DYD"), batch.batch_code)
            self.assertEqual(batch.dyeing_colour_name, "Navy Blue")
            self.assertEqual(batch.dyeing_colour_hex, "#1F3A5F")
            self.assertEqual(batch.purchase_order_id, po.id)
            self.assertEqual(batch.supplier_id, po.party_id)
        self.assertEqual({one.goods_receipt.grn_number, two.goods_receipt.grn_number},
                         {first["grnNumber"], second["grnNumber"]})
        self.assertFalse(Batch.objects.filter(client=self.tenant, fabric_type="GREY").exists())

        by_code = {b["batchCode"]: b for b in self.registry_group(po)["batches"]}
        self.assertEqual(Decimal(by_code[one.batch_code]["batchQty"]), Decimal("2000"))
        self.assertEqual(Decimal(by_code[one.batch_code]["actualRate"]), Decimal("41"))
        self.assertEqual(Decimal(by_code[two.batch_code]["actualRate"]), Decimal("43"))
        self.assertEqual(Decimal(by_code[two.batch_code]["batchAmount"]), Decimal("86000"))
        self.assertEqual(by_code[one.batch_code]["dyeingColourName"], "Navy Blue")
        self.assertEqual(by_code[one.batch_code]["poNumber"], po.po_number)
        self.assertEqual(by_code[one.batch_code]["receiptNumbers"], [one.goods_receipt.grn_number])

    def test_batch_value_is_booked_once(self):
        po = self.create_po(MILL, qty=4000)
        self.grn(po, 2000, "CH-B1", rate=41)
        batch, = self.batches(po)
        lots = LotTaka.objects.filter(batch=batch).values_list("lot_number", flat=True)
        booked = sum(
            (e.amount for e in LotCostEntry.objects.filter(
                client=self.tenant, lot_number__in=list(lots), cost_type="Grey purchase", deleted_at__isnull=True,
            )),
            Decimal("0"),
        )
        self.assertEqual(booked, Decimal("82000"))

    def test_grn_api_exposes_the_inherited_colour(self):
        po = self.create_po(MILL, qty=4000)
        receipt = self.grn(po, 500, "CH-B1")
        self.assertEqual(receipt["fabricType"], "DYED")
        self.assertEqual(receipt["fabricFlow"], MILL)
        self.assertEqual(receipt["dyeingColourName"], "Navy Blue")
        self.assertEqual(self.api.get(f"/api/v1/purchase/receipts/{receipt['id']}/").json()["dyeingColourHex"], "#1F3A5F")

    def test_roll_takes_the_master_shade_of_the_colour_when_there_is_one(self):
        shade = Shade.objects.create(client=self.tenant, name="navy blue", code="SH-NB")
        po = self.create_po(MILL, qty=4000)
        self.grn(po, 500, "CH-B1")
        self.assertEqual(LotTaka.objects.get(batch__purchase_order=po).shade_id, shade.id)

    def test_batch_stays_pending_until_qc_and_keeps_po_and_colour_after(self):
        po = self.create_po(MILL, qty=4000)
        receipt = self.grn(po, 1000, "CH-B1")
        batch, = self.batches(po)

        row = self.api.get(f"/api/v1/inventory/batches/{batch.batch_code}/").json()
        row = row.get("batch", row)
        self.assertEqual(Decimal(row["quantities"]["available"]), Decimal("0"))
        self.assertEqual(Decimal(row["quantities"]["qcHold"]), Decimal("1000"))
        self.assertEqual(self.registry_group(po)["batches"][0]["qcStatus"], "Pending")

        self.qc(receipt["grnNumber"], 1000)

        row = self.api.get(f"/api/v1/inventory/batches/{batch.batch_code}/").json()
        row = row.get("batch", row)
        self.assertEqual(Decimal(row["quantities"]["available"]), Decimal("1000"))
        self.assertEqual(row["dyeingColourName"], "Navy Blue")
        self.assertEqual(row["poNumber"], po.po_number)
        batch.refresh_from_db()
        self.assertEqual(batch.purchase_order_id, po.id)

    def test_older_mill_po_without_colour_can_still_be_received(self):
        po = self.create_po(MILL, qty=4000)
        PurchaseOrder.objects.filter(pk=po.pk).update(dyeing_colour_name=None, dyeing_colour_hex=None)
        po.refresh_from_db()
        self.grn(po, 500, "CH-B1")
        batch, = self.batches(po)
        self.assertEqual(batch.fabric_type, "DYED")
        self.assertIsNone(batch.dyeing_colour_name)


class ValidationTests(DyeingColourTestBase):
    """Test C."""

    def test_mill_po_needs_a_colour(self):
        for extra in ({}, {"dyeingColourName": ""}, {"dyeingColourName": "   "}, {"dyeingColourHex": "#112233"}):
            res = self.api.post("/api/v1/purchase/orders/", self.order_body(MILL, **extra), format="json")
            self.assertEqual(res.status_code, 400, res.content)
        self.assertFalse(PurchaseOrder.objects.filter(client=self.tenant).exists())

    def test_mill_po_needs_a_dyeing_mill(self):
        res = self.api.post("/api/v1/purchase/orders/", self.order_body(
            MILL, dyeingColourName="Navy", dyeingMillId=None,
        ), format="json")
        self.assertEqual(res.status_code, 400, res.content)
        self.assertFalse(PurchaseOrder.objects.filter(client=self.tenant).exists())

    def test_bad_hex_is_refused(self):
        res = self.api.post("/api/v1/purchase/orders/", self.order_body(
            MILL, dyeingColourName="Navy", dyeingColourHex="navy",
        ), format="json")
        self.assertEqual(res.status_code, 400, res.content)

    def test_switching_a_draft_to_mill_needs_a_colour(self):
        po = self.api.post("/api/v1/purchase/orders/", self.order_body(WAREHOUSE), format="json").json()
        res = self.api.patch(f"/api/v1/purchase/orders/{po['id']}/", {"fabricFlow": MILL}, format="json")
        self.assertEqual(res.status_code, 400, res.content)

    def test_older_draft_without_colour_cannot_be_submitted(self):
        po = self.api.post("/api/v1/purchase/orders/", self.order_body(
            MILL, dyeingColourName="Navy Blue",
        ), format="json").json()
        PurchaseOrder.objects.filter(pk=po["id"]).update(dyeing_colour_name=None)
        res = self.api.post(f"/api/v1/purchase/orders/{po['id']}/submit-for-approval/", {}, format="json")
        self.assertEqual(res.status_code, 400, res.content)
        self.assertEqual(PurchaseOrder.objects.get(pk=po["id"]).status, "Draft")

    def test_colour_is_locked_after_draft(self):
        po = self.create_po(MILL, qty=4000)
        res = self.api.patch(f"/api/v1/purchase/orders/{po.id}/", {"dyeingColourName": "Red"}, format="json")
        self.assertEqual(res.status_code, 400, res.content)
        po.refresh_from_db()
        self.assertEqual(po.dyeing_colour_name, "Navy Blue")

    def test_dyed_batch_is_refused_on_a_warehouse_po(self):
        po = self.create_po(WAREHOUSE, qty=4000)
        res = self.grn(po, 500, "CH-C1", fabricType="DYED", expect=422)
        self.assertEqual(res["code"], "WRONG_FABRIC_TYPE")
        self.assertFalse(GoodsReceipt.objects.filter(purchase_order=po).exists())
        self.assertEqual(self.batches(po), [])

    def test_grey_batch_is_refused_on_a_mill_po(self):
        po = self.create_po(MILL, qty=4000)
        res = self.grn(po, 500, "CH-C1", fabricType="GREY", expect=422)
        self.assertEqual(res["code"], "WRONG_FABRIC_TYPE")
        self.assertEqual(self.batches(po), [])

    def test_a_line_cannot_ask_for_the_wrong_type_either(self):
        po = self.create_po(WAREHOUSE, qty=4000)
        line = po.line_items.first()
        res = self.api.post("/api/v1/purchase/receipts/", {
            "partyId": str(po.party_id), "locationId": str(self.godown.id), "purchaseOrderId": str(po.id),
            "supplierChallanNo": "CH-C2",
            "lines": [{"itemId": str(line.item_id), "poLineId": str(line.id), "receivedQty": 5, "fabricType": "DYED"}],
        }, format="json")
        self.assertEqual(res.status_code, 422, res.content)

    def test_dyed_receipt_without_a_po_is_refused(self):
        res = self.api.post("/api/v1/purchase/receipts/", {
            "partyId": str(self.factory.id), "locationId": str(self.godown.id), "supplierChallanNo": "CH-C3",
            "fabricType": "DYED",
            "lines": [{"itemId": str(self.fabric.id), "receivedQty": 5, "unitCost": 10}],
        }, format="json")
        self.assertEqual(res.status_code, 400, res.content)
        self.assertFalse(Batch.objects.filter(client=self.tenant).exists())

    def test_a_po_batch_cannot_be_flipped_to_the_other_type(self):
        po = self.create_po(MILL, qty=4000)
        self.grn(po, 500, "CH-C1")
        batch, = self.batches(po)
        res = self.api.post(
            f"/api/v1/inventory/batches/{batch.batch_code}/actions/fabric-type/", {"fabricType": "GREY"}, format="json",
        )
        self.assertEqual(res.status_code, 422, res.content)
        batch.refresh_from_db()
        self.assertEqual(batch.fabric_type, "DYED")

    def test_receipt_cannot_exceed_what_remains_on_the_po(self):
        po = self.create_po(MILL, qty=4000)
        self.grn(po, 3000, "CH-C1")
        self.grn(po, 1001, "CH-C2", expect=400)
        self.assertEqual(len(self.batches(po)), 1)

    def test_a_fabric_off_the_po_is_refused(self):
        po = self.create_po(MILL, qty=4000)
        res = self.api.post("/api/v1/purchase/receipts/", {
            "partyId": str(po.party_id), "locationId": str(self.godown.id), "purchaseOrderId": str(po.id),
            "supplierChallanNo": "CH-C4",
            "lines": [{"itemId": str(self.yarn.id), "receivedQty": 9000, "unitCost": 10}],
        }, format="json")
        self.assertEqual(res.status_code, 400, res.content)
        self.assertEqual(self.batches(po), [])

    def test_replayed_save_opens_one_batch(self):
        po = self.create_po(MILL, qty=4000)
        first = self.grn(po, 500, "CH-C1", key="grn-1")
        replay = self.grn(po, 500, "CH-C1", key="grn-1")
        self.assertEqual(first["id"], replay["id"])
        self.assertEqual(len(self.batches(po)), 1)
        self.assertEqual(GoodsReceipt.objects.filter(purchase_order=po).count(), 1)

    def test_same_challan_saved_twice_opens_one_batch(self):
        po = self.create_po(MILL, qty=4000)
        self.grn(po, 500, "CH-C1", key="grn-1")
        res = self.grn(po, 500, "CH-C1", key="grn-2", expect=409)
        self.assertEqual(res["code"], "DUPLICATE_GRN")
        self.assertEqual(len(self.batches(po)), 1)

    def test_a_bill_on_a_mill_po_cannot_open_a_grey_batch(self):
        po = self.create_po(MILL, qty=4000)
        bill = PurchaseBill.objects.create(
            client=self.tenant, party=self.factory, purchase_order=po, bill_number="BILL-T1",
            doc_date="2026-10-20", party_name=self.factory.name,
        )
        res = self.api.post(f"/api/v1/purchase/bills/{bill.id}/receive-goods/", {"lines": []}, format="json")
        self.assertEqual(res.status_code, 409, res.content)
        self.assertEqual(res.json()["code"], "WRONG_FABRIC_TYPE")
        self.assertEqual(self.batches(po), [])
