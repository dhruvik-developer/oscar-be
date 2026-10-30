"""QC Work: one list where a lot keeps its row and only its state changes.

    python manage.py test apps.inventory.tests_qc_work
"""
import uuid
from datetime import date
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, User
from apps.masters.models import Item, Location, Party
from apps.purchase.models import GoodsReceipt, GoodsReceiptLine

WORK = "/api/v1/inventory/qc/work/"
DISPOSE = "/api/v1/inventory/qc/disposition/"


class QcWorklistTests(TestCase):
    def setUp(self):
        self.tenant, _ = Client.objects.get_or_create(slug="qc-work-tenant", defaults={"name": "QC Work"})
        self.user = User.objects.create_user(
            email="qc-work@example.com", password="pass", client=self.tenant, is_superuser=True, name="QC Admin",
        )
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(self.user)['access']}")
        self.vendor = Party.objects.create(client=self.tenant, name="Grey Mills", code="V-GM", type="Vendor")
        self.item = Item.objects.create(client=self.tenant, name="Cotton 60s Navy", sku=f"C60-{uuid.uuid4().hex[:5]}",
                                        item_kind="Fabric", uom="Meter")
        self.godown = Location.objects.create(client=self.tenant, name="Godown", code="GD-1", type="Godown", is_active=True)

    def grn(self, meters="300"):
        receipt = GoodsReceipt.objects.create(
            client=self.tenant, grn_number=f"GRN-QW-{uuid.uuid4().hex[:5]}", party=self.vendor,
            location=self.godown, receipt_date=date.today(), qc_status="Pending",
        )
        GoodsReceiptLine.objects.create(
            client=self.tenant, goods_receipt=receipt, item=self.item, ordered_qty=Decimal(meters),
            received_qty=Decimal(meters), batch_number="LOT-7",
        )
        return receipt

    def row(self, receipt):
        body = self.api.get(WORK).json()
        return body, next(r for r in body["results"] if r["receiptId"] == str(receipt.id))

    def test_a_lot_keeps_its_row_while_its_state_moves_on(self):
        receipt = self.grn()
        body, row = self.row(receipt)
        self.assertEqual((row["state"], row["nextAction"], row["toCheckQty"]), ("waiting", "start", 300.0))
        self.assertEqual((row["fabric"], row["lotNo"], row["supplier"]), ("Cotton 60s Navy", "LOT-7", "Grey Mills"))
        self.assertEqual(body["counts"]["waiting"], 1)

        # Checked and saved for later: same row, now waiting for approval.
        r = self.api.post(DISPOSE, {"receiptId": str(receipt.id), "goodQty": 280, "rejectedQty": 20,
                                    "remarks": "holes", "isDraft": True}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        _, row = self.row(receipt)
        self.assertEqual((row["state"], row["nextAction"]), ("inspected", "finish"))
        self.assertEqual(row["draft"]["good"], 280.0)
        self.assertEqual(row["remarks"], "holes")

        # Approved: still on the list, as done, with the result in words.
        r = self.api.post(DISPOSE, {"receiptId": str(receipt.id), "goodQty": 280, "rejectedQty": 20}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        body, row = self.row(receipt)
        self.assertEqual((row["state"], row["nextAction"]), ("done", "view"))
        self.assertIn("280 good", row["result"])
        self.assertIn("20 rejected", row["result"])
        self.assertEqual(body["counts"]["waiting"], 0)
        self.assertEqual(body["counts"]["done"], 1)
        self.assertGreaterEqual(len(row["timeline"]), 2)

    def test_rework_shows_on_the_same_row_with_a_recheck(self):
        from apps.jobwork.models import JobWorkOrder

        receipt = self.grn("100")
        dyer = JobWorkOrder.objects.create(client=self.tenant, jwo_no="JWO-QW-1", process="Dyeing",
                                           vendor=self.vendor, vendor_name=self.vendor.name, order_date=date.today())
        no_order = self.api.post(DISPOSE, {"receiptId": str(receipt.id), "goodQty": 70, "reprocessQty": 30},
                                 format="json")
        self.assertEqual(no_order.status_code, 400)  # rework needs the job work order it goes on
        r = self.api.post(DISPOSE, {"receiptId": str(receipt.id), "goodQty": 70, "reprocessQty": 30,
                                    "jwoId": str(dyer.id)}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        body, row = self.row(receipt)
        self.assertEqual((row["state"], row["nextAction"], row["toCheckQty"]), ("rework", "recheck", 30.0))
        self.assertTrue(row["reprocessId"])
        # Not listed twice (once as the GRN, once as the rework order).
        self.assertEqual(sum(1 for r in body["results"] if r["reference"] == receipt.grn_number), 1)
        self.assertFalse(any(r["kind"] == "REWORK" for r in body["results"]))

    def test_filters_by_state_search_and_stage(self):
        self.grn()
        done = self.grn("50")
        self.api.post(DISPOSE, {"receiptId": str(done.id), "goodQty": 50}, format="json")
        self.assertEqual([r["state"] for r in self.api.get(f"{WORK}?state=waiting").json()["results"]], ["waiting"])
        self.assertEqual(len(self.api.get(f"{WORK}?q={done.grn_number}").json()["results"]), 1)
        self.assertEqual(self.api.get(f"{WORK}?stage=dyeing").json()["results"], [])
