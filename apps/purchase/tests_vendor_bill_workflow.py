"""PO -> Pay Vendor Bill: check, create, match, approve, pay.

Run with::

    python manage.py test apps.purchase.tests_vendor_bill_workflow
"""
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounting.models import JournalEntry
from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, Permission, Role, RolePermission, User
from apps.masters.models import FabricQuality, Item, Location, Party
from apps.purchase.models import GoodsReceipt, PaymentOut, PurchaseBill, PurchaseOrder, VendorBill


class VendorBillWorkflowTests(TestCase):
    def setUp(self):
        self.tenant = Client.objects.create(name="Bill Flow Client", slug="bill-flow")
        self.user = User.objects.create_user(
            email="owner@example.com", password="secretpassword",
            client=self.tenant, is_superuser=True,
        )
        from apps.accounting.services import seed_chart_of_accounts
        seed_chart_of_accounts(self.tenant)
        self.api = self._client_for(self.user)

        quality = FabricQuality.objects.create(client=self.tenant, name="Rayon 60x60", code="RY60")
        self.fabric = Item.objects.create(
            client=self.tenant, sku="GREY-RY60", name="Grey Rayon 60x60", uom="Mtr",
            item_kind="Standalone", fabric_quality=quality, cost_price=Decimal("12.00"),
        )
        self.vendor = Party.objects.create(client=self.tenant, name="anami", code="V-AN", type="Vendor")
        self.other_vendor = Party.objects.create(client=self.tenant, name="Other", code="V-OT", type="Vendor")
        self.godown = Location.objects.create(
            client=self.tenant, name="Main Godown", code="WH-MAIN", type="Warehouse",
        )

    # -- helpers -----------------------------------------------------------
    def _client_for(self, user):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(user)['access']}")
        return client

    def _clerk(self, *permissions):
        role = Role.objects.create(client=self.tenant, code=f"role-{len(permissions)}", name="Clerk")
        for code in permissions:
            permission, _ = Permission.objects.get_or_create(
                id=code, defaults={"label": code, "module": "purchase"}
            )
            RolePermission.objects.create(role=role, permission=permission)
        return User.objects.create_user(
            email=f"clerk{len(permissions)}@example.com", password="secretpassword",
            client=self.tenant, role=role,
        )

    def _po(self, qty=4000, rate=12, received=4000):
        res = self.api.post("/api/v1/purchase/orders/", {
            "vendorId": str(self.vendor.id),
            "date": "2026-11-04",
            "purchaseType": "Grey Stock Purchase",
            "lineItems": [{"itemId": str(self.fabric.id), "qty": qty, "rate": rate, "tax": 5, "uom": "Mtr"}],
        }, format="json")
        self.assertEqual(res.status_code, 201, res.content)
        po = PurchaseOrder.objects.get(pk=res.json()["id"])
        for step in ("submit-for-approval", "approve"):
            res = self.api.post(f"/api/v1/purchase/orders/{po.id}/{step}/", {}, format="json")
            self.assertEqual(res.status_code, 200, res.content)
        if received:
            res = self.api.post("/api/v1/purchase/receipts/", {
                "vendorId": str(po.party_id),
                "purchaseOrderId": str(po.id),
                "locationId": str(self.godown.id),
                "receiptDate": "2026-11-04",
                "supplierChallanNo": f"CH-{po.po_number}",
                "lines": [{
                    "itemId": str(self.fabric.id),
                    "poLineId": str(po.line_items.first().id),
                    "receivedQty": received,
                    "takas": [{"taka_number": "1", "qty": received}],
                }],
            }, format="json")
            self.assertEqual(res.status_code, 201, res.content)
        return po

    def _details(self, po):
        res = self.api.get(f"/api/v1/purchase/vendor-bills/po-details/?poId={po.id}")
        self.assertEqual(res.status_code, 200, res.content)
        return res.json()

    def _post_bill(self, po, number, qty, rate, gst=5, api=None, **extra):
        return (api or self.api).post("/api/v1/purchase/vendor-bills/", {
            "vendorBillNumber": number,
            "billDate": "2026-11-05",
            "purchaseOrderId": str(po.id),
            "gstPct": gst,
            "lineItems": [{"itemId": str(self.fabric.id), "qty": qty, "rate": rate}],
            **extra,
        }, format="json")

    def _bill(self, po, number, qty, rate, **kwargs):
        res = self._post_bill(po, number, qty, rate, **kwargs)
        self.assertEqual(res.status_code, 201, res.content)
        return VendorBill.objects.get(pk=res.json()["id"])

    def _review(self, bill, api=None):
        res = (api or self.api).post(f"/api/v1/purchase/vendor-bills/{bill.id}/recheck-matching/", {}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        return res.json()

    def _approve(self, bill, remarks="", api=None, payable=True):
        return (api or self.api).post(
            f"/api/v1/purchase/vendor-bills/{bill.id}/approve/",
            {"remarks": remarks, "createPayable": payable}, format="json",
        )

    def _pay(self, bill, amount, api=None, headers=None, **extra):
        return (api or self.api).post(
            f"/api/v1/purchase/vendor-bills/{bill.id}/pay/",
            {"amount": amount, "mode": "Cash", **extra}, format="json", **(headers or {}),
        )

    def _row(self, po, bill):
        return next(row for row in self._details(po)["bills"] if row["id"] == str(bill.id))

    def _approved_bill(self, po, number="INV-001", qty=4000, rate=12):
        bill = self._bill(po, number, qty, rate)
        self._review(bill)
        res = self._approve(bill)
        self.assertEqual(res.status_code, 200, res.content)
        bill.refresh_from_db()
        return bill

    # -- checking a PO ------------------------------------------------------
    def test_po_without_bill_reports_none_and_opening_creates_nothing(self):
        po = self._po()
        for _ in range(2):
            data = self._details(po)
        self.assertEqual(data["bills"], [])
        self.assertEqual(data["poSummary"]["receivedQty"], 4000)
        self.assertEqual(data["poSummary"]["remainingBillableQty"], 4000)
        self.assertTrue(data["poSummary"]["canCreateBill"])
        self.assertEqual(sum(row["receivedQty"] for row in data["batches"]), 4000)
        self.assertTrue(all(row["grnNumber"] for row in data["batches"]))
        self.assertFalse(VendorBill.objects.filter(purchase_order=po).exists())
        self.assertFalse(PurchaseBill.objects.filter(purchase_order=po).exists())

    def test_nothing_received_is_not_reported_as_the_ordered_quantity(self):
        data = self._details(self._po(received=0))
        self.assertEqual(data["poSummary"]["orderedQty"], 4000)
        self.assertEqual(data["poSummary"]["receivedQty"], 0)
        self.assertEqual(data["batches"], [])
        self.assertFalse(data["poSummary"]["canCreateBill"])
        self.assertIn("received", data["poSummary"]["createBlockedReason"])

    def test_header_without_lines_is_not_a_bill(self):
        po = self._po()
        ghost = VendorBill.objects.create(
            client=self.tenant, vendor_bill_number="VB-TEST-1", bill_date="2026-11-04",
            party=self.vendor, party_name=self.vendor.name, purchase_order=po,
            subtotal=Decimal("1000"), gst_amount=Decimal("50"), total=Decimal("1050"),
            status="Approved", match_status="Matched",
        )
        data = self._details(po)
        self.assertEqual(data["bills"], [])
        self.assertEqual(data["poSummary"]["totalBilledAmount"], 0)
        self.assertTrue(data["poSummary"]["canCreateBill"])

        res = self._pay(ghost, "100")
        self.assertEqual(res.status_code, 409, res.content)
        self.assertEqual(res.json()["code"], "BILL_HAS_NO_LINES")
        ghost.status = "Pending"
        ghost.save(update_fields=["status", "updated_at"])
        self.assertEqual(self._approve(ghost, "x").json()["code"], "BILL_HAS_NO_LINES")
        self.assertFalse(PaymentOut.objects.filter(party=self.vendor).exists())

    # -- creating a bill ----------------------------------------------------
    def test_bill_saves_once_and_reopening_shows_it(self):
        po = self._po()
        headers = {"HTTP_IDEMPOTENCY_KEY": "bill-create-1"}
        body = {
            "vendorBillNumber": "INV-001", "billDate": "2026-11-05", "purchaseOrderId": str(po.id),
            "gstPct": 5, "lineItems": [{"itemId": str(self.fabric.id), "qty": 4000, "rate": 12}],
        }
        first = self.api.post("/api/v1/purchase/vendor-bills/", body, format="json", **headers)
        again = self.api.post("/api/v1/purchase/vendor-bills/", body, format="json", **headers)
        self.assertEqual(first.status_code, 201, first.content)
        self.assertEqual(again.json()["id"], first.json()["id"])
        self.assertEqual(self._post_bill(po, "INV-001", 10, 12).status_code, 400)

        bill = VendorBill.objects.get(purchase_order=po)
        self.assertEqual(bill.party_id, self.vendor.id)
        rows = self._details(po)["bills"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["billQuantity"], 4000)
        self.assertEqual(rows[0]["billAmount"], 50400)
        self.assertEqual(rows[0]["lines"][0]["qty"], 4000)
        self.assertEqual(rows[0]["lines"][0]["poRate"], 12)
        self.assertEqual(VendorBill.objects.filter(purchase_order=po).count(), 1)

    def test_bill_is_validated_against_the_po(self):
        po = self._po()
        res = self._post_bill(po, "INV-V", 10, 12, vendorId=str(self.other_vendor.id))
        self.assertEqual(res.status_code, 400, res.content)
        self.assertEqual(self._post_bill(po, "INV-R", 10, 0).status_code, 400)
        self.assertEqual(self._post_bill(po, "INV-C", 10, 12, otherCharges=-5).status_code, 400)
        self.assertFalse(VendorBill.objects.filter(purchase_order=po).exists())

    # -- matching -----------------------------------------------------------
    def test_new_bill_needs_matching_and_cannot_be_paid(self):
        po = self._po()
        bill = self._bill(po, "INV-001", 4000, 12)
        row = self._row(po, bill)
        self.assertEqual(row["workflow"]["label"], "Vendor Bill Created — Matching Required")
        self.assertEqual(row["workflow"]["actions"], ["view", "review"])
        self.assertEqual(row["matchingStatus"], "Pending Matching")
        res = self._pay(bill, 100)
        self.assertEqual(res.status_code, 409, res.content)
        self.assertEqual(res.json()["code"], "BILL_NOT_PAYABLE")

    def test_clean_bill_matches_without_changing_anything_else(self):
        po = self._po()
        bill = self._bill(po, "INV-001", 4000, 12)
        result = self._review(bill)
        self.assertEqual(result["overallStatus"], "Match")
        line = result["lines"][0]
        self.assertEqual(
            (line["poQty"], line["grnQty"], line["previouslyBilledQty"], line["billQty"], line["poRate"], line["billRate"]),
            (4000, 4000, 0, 4000, 12, 12),
        )
        row = self._row(po, bill)
        self.assertEqual(row["workflow"]["label"], "Matched")
        self.assertIn("approve", row["workflow"]["actions"])
        self.assertNotIn("pay", row["workflow"]["actions"])

    def test_mismatch_needs_approval_before_payment(self):
        po = self._po()
        rate_before = po.line_items.first().rate
        bill = self._bill(po, "INV-001", 4000, 15)
        self.assertEqual(self._review(bill)["overallStatus"], "Mismatch")
        row = self._row(po, bill)
        self.assertEqual(row["workflow"]["label"], "Mismatch — Approval Required")
        self.assertEqual(self._pay(bill, 100).status_code, 409)

        clerk = self._client_for(self._clerk("view_purchase", "create_bill"))
        res = self._approve(bill, "Market rate agreed", api=clerk)
        self.assertEqual(res.status_code, 409, res.content)
        self.assertEqual(res.json()["code"], "APPROVAL_REQUIRED")
        row = self._row(po, bill)
        self.assertEqual(row["workflow"]["stage"], "approval_pending")
        self.assertNotIn("approve", row["workflow"]["actions"])
        self.assertNotIn("pay", row["workflow"]["actions"])

        self.assertEqual(self._approve(bill).status_code, 400, "A mismatch needs a reason")
        self.assertEqual(self._approve(bill, "Market rate agreed").status_code, 200)
        row = self._row(po, bill)
        self.assertEqual(row["workflow"]["label"], "Approved")
        self.assertEqual(row["matchingStatus"], "Mismatch")
        self.assertIn("pay", row["workflow"]["actions"])
        self.assertEqual(row["billAmount"], 63000, "The bill keeps its own amount")
        po.refresh_from_db()
        self.assertEqual(po.line_items.first().rate, rate_before, "The PO is not changed")

    def test_editing_a_bill_clears_its_match(self):
        po = self._po()
        bill = self._bill(po, "INV-001", 4000, 12)
        self._review(bill)
        res = self.api.patch(f"/api/v1/purchase/vendor-bills/{bill.id}/", {
            "lineItems": [{"itemId": str(self.fabric.id), "qty": 4000, "rate": 13}],
        }, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(self._row(po, bill)["matchingStatus"], "Pending Matching")

    def test_other_charges_reach_the_total_and_need_approval(self):
        po = self._po()
        bill = self._bill(po, "INV-001", 4000, 12, otherCharges=500)
        self.assertEqual(bill.total, Decimal("50900.00"))
        result = self._review(bill)
        self.assertEqual(result["overallStatus"], "Mismatch")
        self.assertTrue(result["summary"]["totalMatch"])
        self.assertEqual(self._approve(bill, "Freight agreed").status_code, 200)
        bill.refresh_from_db()
        self.assertEqual(bill.purchase_bill.total, Decimal("50900.00"))

    # -- several bills ------------------------------------------------------
    def test_two_bills_stay_separate_and_totals_come_from_them(self):
        po = self._po()
        first = self._bill(po, "INV-001", 2000, 90)
        second = self._bill(po, "INV-002", 2000, 12)
        self._review(first)
        self._review(second)
        data = self._details(po)
        self.assertEqual(len(data["bills"]), 2)
        self.assertEqual(self._row(po, first)["matchingStatus"], "Mismatch")
        self.assertEqual(self._row(po, second)["matchingStatus"], "Matched")
        summary = data["poSummary"]
        self.assertEqual(summary["previouslyBilledQty"], 4000)
        self.assertEqual(summary["remainingBillableQty"], 0)
        self.assertEqual(summary["totalBilledAmount"], 189000 + 25200)
        self.assertFalse(summary["canCreateBill"])

    def test_rejected_bill_frees_its_quantity(self):
        po = self._po()
        bill = self._bill(po, "INV-001", 4000, 15)
        res = self.api.post(f"/api/v1/purchase/vendor-bills/{bill.id}/reject/", {"remarks": "Wrong rate"}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        data = self._details(po)
        self.assertEqual(data["bills"][0]["workflow"], {"stage": "rejected", "label": "Rejected", "actions": ["view"]})
        self.assertEqual(data["poSummary"]["remainingBillableQty"], 4000)
        self.assertEqual(data["poSummary"]["totalBilledAmount"], 0)

    def test_payable_does_not_double_count_billed_quantity(self):
        po = self._po()
        self._approved_bill(po, qty=1000)
        self.assertEqual(self._details(po)["poSummary"]["previouslyBilledQty"], 1000)

    # -- paying -------------------------------------------------------------
    def test_payment_updates_the_vendor_ledger(self):
        po = self._po()
        bill = self._approved_bill(po)
        payable = bill.purchase_bill
        self.assertIsNotNone(payable.posted_at)
        self.assertTrue(payable.goods_received, "The GRN already stocked these goods")
        self.assertTrue(JournalEntry.objects.filter(
            source_document_type="PurchaseBill", source_document_id=payable.id
        ).exists())
        self.vendor.refresh_from_db()
        owed = abs(self.vendor.balance)
        self.assertEqual(owed, Decimal("50400.00"))

        res = self._pay(bill, "20400")
        self.assertEqual(res.status_code, 200, res.content)
        row = self._row(po, bill)
        self.assertEqual(row["workflow"]["label"], "Partially Paid")
        self.assertEqual((row["amountPaid"], row["outstandingAmount"]), (20400, 30000))
        self.assertIn("pay", row["workflow"]["actions"])
        self.vendor.refresh_from_db()
        self.assertEqual(abs(self.vendor.balance), Decimal("30000.00"))

        self.assertEqual(self._pay(bill, "30001").status_code, 422, "Over the outstanding amount")
        self.assertEqual(self._pay(bill, "30000").status_code, 200)
        row = self._row(po, bill)
        self.assertEqual((row["paymentStatus"], row["approvalStatus"]), ("Paid", "Approved"))
        self.assertEqual(row["workflow"]["actions"], ["view"])
        self.vendor.refresh_from_db()
        self.assertEqual(self.vendor.balance, Decimal("0.00"))
        self.assertEqual(self._pay(bill, "1").status_code, 409)

    def test_pay_without_a_payable_still_posts_the_ledger(self):
        po = self._po()
        bill = self._bill(po, "INV-001", 4000, 12)
        self.assertEqual(self._approve(bill, payable=False).status_code, 200)
        bill.refresh_from_db()
        self.assertIsNone(bill.purchase_bill_id)
        self.assertEqual(self._pay(bill, "50400").status_code, 200)
        bill.refresh_from_db()
        self.assertIsNotNone(bill.purchase_bill.posted_at)
        self.vendor.refresh_from_db()
        self.assertEqual(self.vendor.balance, Decimal("0.00"))

    def test_duplicate_vouchers_are_refused(self):
        bill = self._approved_bill(self._po())
        headers = {"HTTP_IDEMPOTENCY_KEY": "pay-1"}
        first = self._pay(bill, "1000", headers=headers)
        again = self._pay(bill, "1000", headers=headers)
        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual(again.json()["paymentId"], first.json()["paymentId"])
        self.assertEqual(PaymentOut.objects.filter(party=self.vendor).count(), 1)

        self.assertEqual(self._pay(bill, "1000", reference="UTR-9").status_code, 200)
        res = self._pay(bill, "1000", reference="utr-9")
        self.assertEqual(res.status_code, 409, res.content)
        self.assertEqual(res.json()["code"], "DUPLICATE_PAYMENT_REFERENCE")
        self.assertEqual(PaymentOut.objects.filter(party=self.vendor).count(), 2)

    def test_cancelling_a_payment_reopens_the_bill(self):
        po = self._po()
        bill = self._approved_bill(po)
        payment_id = self._pay(bill, "50400").json()["paymentId"]
        self.assertEqual(self._row(po, bill)["paymentStatus"], "Paid")
        res = self.api.post(f"/api/v1/purchase/payments/{payment_id}/cancel/", {"reason": "Bounced"}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        row = self._row(po, bill)
        self.assertEqual(row["paymentStatus"], "Unpaid")
        self.assertIn("pay", row["workflow"]["actions"])

    def test_paying_needs_the_payment_permission_and_a_valid_mode(self):
        bill = self._approved_bill(self._po())
        clerk = self._client_for(self._clerk("view_purchase", "create_bill"))
        self.assertEqual(self._pay(bill, "100", api=clerk).status_code, 403)
        self.assertEqual(self._pay(bill, "100", mode="Bank Transfer (NEFT/RTGS)").status_code, 400)
        self.assertEqual(self._pay(bill, "100", mode="Bank Transfer").status_code, 400, "Needs a bank account")
        self.assertEqual(self._pay(bill, "abc").status_code, 400)

    def test_decided_bill_cannot_be_edited_or_deleted(self):
        bill = self._approved_bill(self._po())
        url = f"/api/v1/purchase/vendor-bills/{bill.id}/"
        self.assertEqual(self.api.patch(url, {"remarks": "x"}, format="json").status_code, 409)
        self.assertEqual(self.api.delete(url).status_code, 409)

    def _draft_payable(self, po):
        bill = self._bill(po, "INV-D", 4000, 12)
        self._review(bill)
        res = self.api.post(f"/api/v1/purchase/vendor-bills/{bill.id}/send-for-matching/", {}, format="json")
        self.assertEqual(res.status_code, 201, res.content)
        return res.json()["purchaseBillId"]

    def test_goods_on_a_grn_cannot_be_received_again_from_the_bill(self):
        po = self._po()
        payable_id = self._draft_payable(po)
        self.assertTrue(self.api.get(f"/api/v1/purchase/bills/{payable_id}/").json()["poAlreadyReceived"])
        res = self.api.post(f"/api/v1/purchase/bills/{payable_id}/receive-goods/", {"lines": []}, format="json")
        self.assertEqual(res.status_code, 409, res.content)
        self.assertEqual(GoodsReceipt.objects.filter(purchase_order=po).count(), 1)

    def test_payment_out_refuses_a_draft_bill_but_allows_an_advance(self):
        payable_id = self._draft_payable(self._po())
        body = {"vendorId": str(self.vendor.id), "date": "2026-11-05", "amount": 100, "mode": "Cash"}
        res = self.api.post("/api/v1/purchase/payments/", {**body, "billId": payable_id}, format="json")
        self.assertEqual(res.status_code, 422, res.content)
        self.assertEqual(res.json()["code"], "PAYMENT_ON_DRAFT")
        self.assertFalse(PaymentOut.objects.filter(party=self.vendor).exists())
        self.assertEqual(self.api.post("/api/v1/purchase/payments/", body, format="json").status_code, 201)

    def test_state_survives_a_fresh_read(self):
        po = self._po()
        bill = self._approved_bill(po)
        self._pay(bill, "400")
        before = self._row(po, bill)
        self.assertEqual(self._row(po, bill), before)
        self.assertEqual(VendorBill.objects.filter(purchase_order=po).count(), 1)
        self.assertEqual(PurchaseBill.objects.filter(purchase_order=po).count(), 1)
        self.assertEqual(PaymentOut.objects.filter(party=self.vendor).count(), 1)
