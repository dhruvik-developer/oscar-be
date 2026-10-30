"""Roll-by-roll QC entry (requirement PDF §08): measurements, defects and a
result per roll; approval moves each roll and settles the receipt.

    python manage.py test apps.inventory.tests_qc_entry
"""
import uuid
from datetime import date
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, User
from apps.inventory import services as stock
from apps.inventory.models import BatchLoss, LotTaka, QCInspection
from apps.masters.models import DefectMaster, Item, Location, Party
from apps.purchase.models import GoodsReceipt, GoodsReceiptLine

ENTRY = "/api/v1/inventory/qc/entry/"


class QcEntryTests(TestCase):
    def setUp(self):
        self.tenant, _ = Client.objects.get_or_create(slug="qc-entry-tenant", defaults={"name": "QC Entry"})
        self.user = User.objects.create_user(
            email="qc-entry@example.com", password="pass", client=self.tenant, is_superuser=True, name="Checker",
        )
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(self.user)['access']}")
        self.vendor = Party.objects.create(client=self.tenant, name="Grey Mills", code="V-GM", type="Vendor")
        self.item = Item.objects.create(client=self.tenant, name="Cotton 60s", sku=f"C60-{uuid.uuid4().hex[:5]}",
                                        item_kind="Fabric", uom="Meter")
        self.godown = Location.objects.create(client=self.tenant, name="Godown", code="GD-1", type="Godown", is_active=True)
        self.hole = DefectMaster.objects.create(client=self.tenant, code="HOLE", name="Hole", severity="Major")
        self.grn = GoodsReceipt.objects.create(
            client=self.tenant, grn_number=f"GRN-QE-{uuid.uuid4().hex[:4]}", party=self.vendor,
            location=self.godown, receipt_date=date.today(), qc_status="Pending",
        )
        GoodsReceiptLine.objects.create(client=self.tenant, goods_receipt=self.grn, item=self.item,
                                        ordered_qty=Decimal("300"), received_qty=Decimal("300"))
        qc_hold = stock.system_location(self.tenant.id, "QC_HOLD")
        self.rolls = []
        for n in ("1", "2", "3"):
            taka = LotTaka.objects.create(
                client=self.tenant, item=self.item, location=qc_hold, lot_number="L-QE", taka_number=n,
                taka_id=f"TAK-QE-{n}-{uuid.uuid4().hex[:4]}", supplier=self.vendor, received_quantity=Decimal("100"),
                available_quantity=Decimal("0"), status="QC Hold", qc_status="Pending",
                source_document_type="GRN", source_document_id=self.grn.id, source_document_number=self.grn.grn_number,
            )
            stock.post_movement(client_id=self.tenant.id, item=self.item, location=qc_hold, type="PURCHASE",
                                quantity=Decimal("100"), reference_type="GoodsReceipt", reference_id=self.grn.id,
                                reference_number=self.grn.grn_number, lot_taka=taka, unit_cost=Decimal("50"))
            self.rolls.append(taka)
        self.key = f"GRN|{self.grn.grn_number}"

    def roll(self, i, result, **extra):
        return {"lotTakaId": str(self.rolls[i].id), "result": result, **extra}

    def test_draft_then_approve_routes_each_roll_and_settles_the_receipt(self):
        lots = self.api.get(ENTRY).json()["results"]
        lot = next(l for l in lots if l["key"] == self.key)
        self.assertEqual((lot["rolls"], lot["meters"], lot["sourceLabel"]), (3, "300.0000", "Purchase receipt"))

        # Draft: nothing moves, the checker can come back to it.
        r = self.api.post(ENTRY, {"key": self.key, "rolls": [
            self.roll(0, "Pass", actualMeter="98", width="58", gsm="120"),
        ]}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertFalse(r.json()["approved"])
        detail = self.api.get(ENTRY, {"key": self.key}).json()
        self.assertEqual(detail["rolls"][0]["saved"]["actualMeter"], "98.0000")
        self.assertTrue(next(l for l in self.api.get(ENTRY).json()["results"] if l["key"] == self.key)["hasDraft"])

        r = self.api.post(ENTRY, {"key": self.key, "approve": True, "locationId": str(self.godown.id), "rolls": [
            self.roll(0, "Pass", actualMeter="98"),
            self.roll(1, "Reject", remarks="big holes", defects=[{"defectId": str(self.hole.id), "count": 3}]),
            self.roll(2, "Hold"),
        ]}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertEqual((body["approved"], body["status"], body["shortMeter"]), (True, "On Hold", "2.0000"))
        self.assertEqual(body["quantities"]["good"], "98.0000")
        self.assertEqual(body["quantities"]["reject"], "102.0000")  # 100 rejected + 2 short meter

        good = LotTaka.objects.filter(client=self.tenant, location=self.godown, lot_number="L-QE")
        self.assertEqual(sum(t.available_quantity for t in good), Decimal("98.0000"))
        self.assertEqual(LotTaka.objects.get(pk=self.rolls[2].pk).qc_status, "On Hold")
        self.grn.refresh_from_db()
        self.assertEqual(self.grn.qc_status, "On Hold")
        loss = BatchLoss.objects.get(batch=self.rolls[0].batch)
        self.assertEqual((loss.quantity, loss.cause), (Decimal("2.0000"), "NORMAL_LOSS"))
        insp = QCInspection.objects.get(inspection_number=body["inspectionNumber"])
        line = insp.lines.get(lot_taka=self.rolls[1])
        self.assertEqual((line.disposition, line.penalty_points), ("Reject", 6))  # 3 x Major(2)

        # The roll on hold is still waiting; checking it settles the receipt.
        lot = next(l for l in self.api.get(ENTRY).json()["results"] if l["key"] == self.key)
        self.assertEqual((lot["rolls"], lot["onHoldRolls"]), (1, 1))
        r = self.api.post(ENTRY, {"key": self.key, "approve": True, "locationId": str(self.godown.id),
                                  "rolls": [self.roll(2, "Pass")]}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.grn.refresh_from_db()
        self.assertEqual(self.grn.qc_status, "Partially Approved")
        self.assertFalse(any(l["key"] == self.key for l in self.api.get(ENTRY).json()["results"]))
        self.assertEqual(QCInspection.objects.filter(source_document_id=self.grn.id).count(), 2)  # QC history

    def test_reprocess_rolls_are_listed_for_rework(self):
        r = self.api.post(ENTRY, {"key": self.key, "approve": True, "locationId": str(self.godown.id), "rolls": [
            self.roll(0, "Reprocess", remarks="shade off"), self.roll(1, "Pass"), self.roll(2, "Pass"),
        ]}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        rows = self.api.get("/api/v1/inventory/qc/reprocess-stock/").json()["results"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["meters"], "100.0000")
        self.assertEqual(rows[0]["rolls"][0]["remarks"], "shade off")

    def test_every_checked_roll_needs_a_result(self):
        r = self.api.post(ENTRY, {"key": self.key, "rolls": [{"lotTakaId": str(self.rolls[0].id)}]}, format="json")
        self.assertEqual(r.status_code, 400, r.content)
