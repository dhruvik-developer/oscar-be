"""One vendor bill on a Factory -> Dyeing Mill -> OSCAR order.

The PO keeps the fabric rate and the agreed dyeing rate apart. The invoice does
not: each batch line carries one rate for fabric and dyeing together, typed by
hand, matched against the sum of the two. What the bill charges above the
batch's GRN value is its dyeing and lands on that batch only. The bill may come
from the PO's factory or its mill.

Run with::

    python manage.py test apps.purchase.tests_dyeing_bill
"""
from decimal import Decimal

from apps.accounting.models import JournalEntry
from apps.masters.models import Party
from apps.purchase import services
from apps.purchase.models import (
    PaymentOut,
    PurchaseBill,
    PurchaseOrder,
    PurchaseOrderCostAllocation,
    PurchaseOrderExpense,
    VendorBill,
)
from apps.purchase.tests_dyeing_colour import MILL, WAREHOUSE, DyeingColourTestBase

BILLS = "/api/v1/purchase/vendor-bills/"
FABRIC_RATE = 40
DYEING_RATE = 15
BILL_RATE = FABRIC_RATE + DYEING_RATE


class OneBillTestBase(DyeingColourTestBase):
    def received_po(self):
        po = self.create_po(MILL, qty=1000)
        self.grn(po, 100, "CH-1")
        self.grn(po, 900, "CH-2")
        return po

    def post_bill(self, po, number, rows, vendor=None, **extra):
        body = {
            "purchaseOrderId": str(po.id), "vendorBillNumber": number, "billDate": "2026-10-28", "gstPct": 5,
            "lineItems": [
                {"batchId": str(batch.id), "itemId": str(batch.item_id), "qty": qty, "rate": rate}
                for batch, qty, rate in rows
            ],
            **extra,
        }
        if vendor is not None:
            body["vendorId"] = str(vendor.id)
        return self.api.post(BILLS, body, format="json")

    def bill(self, po, number, rows, vendor=None, **extra):
        res = self.post_bill(po, number, rows, vendor or self.mill, **extra)
        self.assertEqual(res.status_code, 201, res.content)
        return VendorBill.objects.get(pk=res.json()["id"])

    def review(self, bill):
        res = self.api.post(f"{BILLS}{bill.id}/recheck-matching/", {}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        return res.json()

    def approve(self, bill, remarks=""):
        self.review(bill)
        res = self.api.post(f"{BILLS}{bill.id}/approve/", {"remarks": remarks, "createPayable": True}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        bill.refresh_from_db()
        return bill

    def details(self, po):
        res = self.api.get(f"{BILLS}po-details/", {"poId": str(po.id)})
        self.assertEqual(res.status_code, 200, res.content)
        return res.json()

    def dyeing_cost(self, batch):
        return sum(
            (a.amount for a in PurchaseOrderCostAllocation.objects.filter(
                batch=batch, expense__category="DYEING", deleted_at__isnull=True,
            )),
            Decimal("0"),
        )


class PoDyeingRateTests(OneBillTestBase):
    def test_rate_is_required_on_a_mill_po_and_persists(self):
        body = self.order_body(MILL, dyeingColourName="Navy Blue")
        body.pop("dyeingRate")
        self.assertEqual(self.api.post("/api/v1/purchase/orders/", body, format="json").status_code, 400)
        res = self.api.post("/api/v1/purchase/orders/", {**body, "dyeingRate": 0}, format="json")
        self.assertEqual(res.status_code, 400, res.content)
        res = self.api.post("/api/v1/purchase/orders/", {**body, "dyeingRate": "15.50"}, format="json")
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(Decimal(self.api.get(f"/api/v1/purchase/orders/{res.json()['id']}/").json()["dyeingRate"]),
                         Decimal("15.5"))

    def test_rate_is_not_kept_on_a_warehouse_po(self):
        res = self.api.post("/api/v1/purchase/orders/", self.order_body(WAREHOUSE, dyeingRate=15), format="json")
        self.assertEqual(res.status_code, 201, res.content)
        self.assertIsNone(res.json()["dyeingRate"])

    def test_rate_is_locked_after_draft(self):
        po = self.create_po(MILL, qty=1000)
        res = self.api.patch(f"/api/v1/purchase/orders/{po.id}/", {"dyeingRate": 20}, format="json")
        self.assertEqual(res.status_code, 400, res.content)

    def test_older_draft_without_rate_cannot_be_submitted(self):
        po = self.api.post("/api/v1/purchase/orders/", self.order_body(MILL, dyeingColourName="Navy"), format="json").json()
        PurchaseOrder.objects.filter(pk=po["id"]).update(dyeing_rate=None)
        res = self.api.post(f"/api/v1/purchase/orders/{po['id']}/submit-for-approval/", {}, format="json")
        self.assertEqual(res.status_code, 400, res.content)


class OneBillTests(OneBillTestBase):
    def test_workspace_offers_each_batch_at_fabric_plus_dyeing_rate(self):
        po = self.received_po()
        details = self.details(po)
        self.assertEqual({b["originalPoRate"] for b in details["batches"]}, {float(BILL_RATE)})
        self.assertEqual(details["poSummary"]["originalPoAmount"], 1000.0 * BILL_RATE)
        self.assertEqual([v["id"] for v in details["billVendors"]], [str(self.factory.id), str(self.mill.id)])

    def test_only_pos_with_something_left_to_bill_are_offered(self):
        po = self.received_po()
        one, two = self.batches(po)
        nothing_received = self.create_po(WAREHOUSE, qty=100)

        def offered():
            return self.api.get(f"{BILLS}billable-pos/").json()["results"]

        self.assertEqual(offered(), [str(po.id)])
        self.assertNotIn(str(nothing_received.id), offered())
        self.bill(po, "M-1", [(one, 100, BILL_RATE)])
        self.assertEqual(offered(), [str(po.id)])  # one batch is still unbilled
        self.bill(po, "M-2", [(two, 900, BILL_RATE)])
        self.assertEqual(offered(), [])

    def test_warehouse_po_is_unchanged(self):
        po = self.create_po(WAREHOUSE, qty=1000)
        self.grn(po, 100, "CH-1")
        batch, = self.batches(po)
        details = self.details(po)
        self.assertEqual(details["billVendors"], [])
        self.assertEqual(details["batches"][0]["originalPoRate"], float(FABRIC_RATE))
        self.assertEqual(self.post_bill(po, "W-2", [(batch, 1, 40)], vendor=self.mill).status_code, 400)
        res = self.post_bill(po, "W-1", [(batch, 100, FABRIC_RATE)])
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(VendorBill.objects.get(pk=res.json()["id"]).party_id, self.factory.id)

    def test_the_vendor_must_be_chosen_and_be_the_factory_or_the_mill(self):
        po = self.received_po()
        one, _ = self.batches(po)
        self.assertEqual(self.post_bill(po, "M-0", [(one, 100, BILL_RATE)]).status_code, 400)
        other = Party.objects.create(client=self.tenant, name="Someone", code="V-X", type="Vendor")
        self.assertEqual(self.post_bill(po, "M-1", [(one, 100, BILL_RATE)], vendor=other).status_code, 400)
        self.assertEqual(self.bill(po, "M-2", [(one, 100, BILL_RATE)], vendor=self.mill).party_id, self.mill.id)

    def test_a_line_needs_its_batch(self):
        po = self.received_po()
        res = self.api.post(BILLS, {
            "purchaseOrderId": str(po.id), "vendorId": str(self.mill.id), "vendorBillNumber": "M-1",
            "billDate": "2026-10-28", "gstPct": 5,
            "lineItems": [{"itemId": str(self.fabric.id), "qty": 100, "rate": BILL_RATE}],
        }, format="json")
        self.assertEqual(res.status_code, 400, res.content)

    def test_one_bill_for_all_batches_at_the_combined_rate(self):
        po = self.received_po()
        one, two = self.batches(po)
        bill = self.bill(po, "M-1", [(one, 100, BILL_RATE), (two, 900, BILL_RATE)])
        result = self.review(bill)
        self.assertEqual(result["overallStatus"], "Match", result["reasons"])
        bill = self.approve(bill)

        payable = bill.purchase_bill
        self.assertEqual(payable.party_id, self.mill.id)
        self.assertIsNotNone(payable.posted_at)
        self.assertEqual(payable.total, Decimal("57750.00"))
        self.assertTrue(JournalEntry.objects.filter(source_document_type="PurchaseBill", source_document_id=payable.id).exists())
        self.assertEqual(self.dyeing_cost(one), Decimal("1500.00"))
        self.assertEqual(self.dyeing_cost(two), Decimal("13500.00"))
        details = self.details(po)
        self.assertEqual(details["poSummary"]["remainingBillableQty"], 0.0)
        self.assertFalse(details["poSummary"]["canCreateBill"])

    def test_only_the_billed_batch_is_charged(self):
        po = self.received_po()
        one, two = self.batches(po)
        self.approve(self.bill(po, "M-1", [(one, 100, BILL_RATE)]))
        self.assertEqual(self.dyeing_cost(one), Decimal("1500.00"))
        self.assertEqual(self.dyeing_cost(two), Decimal("0"))

    def test_fabric_rate_alone_is_a_mismatch(self):
        po = self.received_po()
        one, _ = self.batches(po)
        bill = self.bill(po, "M-1", [(one, 100, FABRIC_RATE)])
        self.assertEqual(self.review(bill)["overallStatus"], "Mismatch")

    def test_rate_above_the_po_needs_remarks_and_the_batch_takes_the_actual(self):
        po = self.received_po()
        one, _ = self.batches(po)
        bill = self.bill(po, "M-1", [(one, 100, BILL_RATE + 1)])
        self.assertEqual(self.review(bill)["overallStatus"], "Mismatch")
        res = self.api.post(f"{BILLS}{bill.id}/approve/", {"remarks": ""}, format="json")
        self.assertEqual(res.status_code, 400, res.content)
        self.approve(bill, remarks="Shade correction agreed")
        self.assertEqual(self.dyeing_cost(one), Decimal("1600.00"))

    def test_a_batch_cannot_be_billed_twice_or_above_what_arrived(self):
        po = self.received_po()
        one, _ = self.batches(po)
        self.assertEqual(self.post_bill(po, "M-0", [(one, 101, BILL_RATE)], vendor=self.mill).status_code, 400)
        self.bill(po, "M-1", [(one, 100, BILL_RATE)])
        self.assertEqual(self.post_bill(po, "M-2", [(one, 1, BILL_RATE)], vendor=self.factory).status_code, 400)

    def test_payment_goes_to_the_vendor_on_the_bill(self):
        po = self.received_po()
        one, _ = self.batches(po)
        bill = self.approve(self.bill(po, "M-1", [(one, 100, BILL_RATE)]))
        res = self.api.post(f"{BILLS}{bill.id}/pay/", {"amount": 5775, "mode": "Cash"}, format="json")
        self.assertIn(res.status_code, (200, 201), res.content)
        self.assertTrue(PaymentOut.objects.filter(party=self.mill).exists())
        self.assertFalse(PaymentOut.objects.filter(party=self.factory).exists())
        self.assertEqual(self.details(po)["bills"][0]["paymentStatus"], "Paid")

    def test_bill_from_the_factory_works_the_same(self):
        po = self.received_po()
        one, _ = self.batches(po)
        bill = self.approve(self.bill(po, "F-1", [(one, 100, BILL_RATE)], vendor=self.factory))
        self.assertEqual(bill.purchase_bill.party_id, self.factory.id)
        self.assertEqual(self.dyeing_cost(one), Decimal("1500.00"))
        res = self.api.post(f"{BILLS}{bill.id}/pay/", {"amount": 5775, "mode": "Cash"}, format="json")
        self.assertIn(res.status_code, (200, 201), res.content)

    def test_an_estimate_is_replaced_once_a_bill_is_approved(self):
        po = self.received_po()
        one, two = self.batches(po)
        self.post(po, "expenses/", {"category": "DYEING", "expenseType": "Dyeing charges", "amount": 10000})
        self.assertGreater(self.dyeing_cost(one), Decimal("0"))
        self.approve(self.bill(po, "M-1", [(one, 100, BILL_RATE), (two, 900, BILL_RATE)]))
        self.assertEqual(self.dyeing_cost(one), Decimal("1500.00"))
        self.assertEqual(self.dyeing_cost(two), Decimal("13500.00"))

    def test_charge_from_a_bill_cannot_be_edited_in_costs(self):
        po = self.received_po()
        one, _ = self.batches(po)
        self.approve(self.bill(po, "M-1", [(one, 100, BILL_RATE)]))
        expense = PurchaseOrderExpense.objects.get(purchase_order=po, vendor_bill__isnull=False)
        res = self.api.delete(self.url(po, f"expenses/{expense.id}/"))
        self.assertEqual(res.status_code, 409, res.content)

    def test_cancelling_the_payable_takes_the_charge_off_the_batch(self):
        po = self.received_po()
        one, _ = self.batches(po)
        bill = self.approve(self.bill(po, "M-1", [(one, 100, BILL_RATE)]))
        services.cancel_bill(PurchaseBill.objects.get(pk=bill.purchase_bill_id), user=self.user)
        self.assertEqual(self.dyeing_cost(one), Decimal("0"))

    def test_a_rejected_bill_frees_the_batch(self):
        po = self.received_po()
        one, _ = self.batches(po)
        bill = self.bill(po, "M-1", [(one, 100, BILL_RATE)])
        res = self.api.post(f"{BILLS}{bill.id}/reject/", {"remarks": "Wrong invoice"}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(self.dyeing_cost(one), Decimal("0"))
        self.assertEqual(self.post_bill(po, "M-2", [(one, 100, BILL_RATE)], vendor=self.mill).status_code, 201)
