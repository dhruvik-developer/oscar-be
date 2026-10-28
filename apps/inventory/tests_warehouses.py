"""Multi-warehouse inventory: warehouse stock, transfers through the
in-transit store, shortage transfers, minimum stock and the warehouse ledger."""
from decimal import Decimal

from apps.accounting.models import JournalEntry
from apps.core.exceptions import BusinessRuleViolation, Conflict, ValidationFailed
from apps.masters.models import Location
from apps.inventory import services as stock
from apps.inventory import warehouse_services as wh
from apps.inventory.models import LotTaka, StockMovement
from apps.inventory.tests_stock_integrity import StockBase
from apps.inventory.views import StockTransferViewSet
from apps.inventory import warehouse_views

D = Decimal


class WarehouseBase(StockBase):
    def total(self, item):
        return D(sum(D(m.quantity) for m in StockMovement.objects.filter(client_id=self.client_obj.id, item=item)))

    def transfer(self, lots, qty, draft=False, item=None):
        from django.utils import timezone

        return wh.create_transfer(
            client=self.client_obj, user=self.user, from_location=self.godown, to_location=self.godown2,
            transfer_date=timezone.localdate(), draft=draft,
            lines=[{"item": item or self.fabric, "qty": D(qty),
                    "takas": [{"lotTakaId": l.id, "qty": D(q)} for l, q in lots]}],
        )

    def line(self, transfer):
        return transfer.items.get()

    def transit(self):
        return stock.system_location(self.client_obj.id, "IN_TRANSIT")


class TransferWorkflowTests(WarehouseBase):
    def test_reserve_partial_dispatch_partial_receipt_and_return(self):
        lot_a = self.make_lot("60", taka="A")
        lot_b = self.make_lot("40", taka="B")
        before = self.total(self.fabric)

        t = self.transfer([(lot_a, "60"), (lot_b, "15")], "75", draft=True)
        self.assertEqual(t.status, "Draft")
        lot_a.refresh_from_db()
        self.assertEqual(lot_a.status, "Available")  # a draft holds nothing

        t = wh.reserve(t)
        lot_a.refresh_from_db()
        lot_b.refresh_from_db()
        self.assertEqual((t.status, lot_a.status, lot_b.status), ("Requested", "Reserved", "Reserved"))
        rows = {r["warehouseId"]: r for r in wh.stock_rows(self.client_obj.id, item_id=self.fabric.id)}
        self.assertEqual(D(rows[str(self.godown.id)]["reserved"]), D("100"))  # whole takas are held
        self.assertEqual(D(rows[str(self.godown.id)]["available"]), D("0"))

        with self.assertRaises(Conflict):  # not approved yet
            wh.dispatch(t, self.user)
        t = wh.approve(t, self.user)

        line = self.line(t)
        t = wh.dispatch(t, self.user, lines=[{"lineId": line.id, "qty": "70"}])
        line.refresh_from_db()
        self.assertEqual((t.status, line.dispatched_qty), ("In Transit", D("70")))
        self.assertEqual(self.on_hand(self.fabric, self.transit()), D("70"))
        self.assertEqual(self.on_hand(self.fabric, self.godown2), D("0"))
        lot_b.refresh_from_db()
        self.assertEqual(lot_b.status, "Reserved")  # 5 m of it still to go

        t = wh.dispatch(t, self.user)  # the rest
        line.refresh_from_db()
        lot_b.refresh_from_db()
        self.assertEqual(line.dispatched_qty, D("75"))
        self.assertEqual((lot_b.available_quantity, lot_b.status), (D("25"), "Available"))
        with self.assertRaises(ValidationFailed):  # nothing left: no double posting
            wh.dispatch(t, self.user)

        # Receive 60 of the 75.
        t = wh.receive(t, self.user, lines=[{"lineId": line.id, "qty": "60"}])
        self.assertEqual(t.status, "Partially Received")
        self.assertEqual(self.on_hand(self.fabric, self.godown2), D("60"))
        self.assertEqual(self.on_hand(self.fabric, self.transit()), D("15"))
        received = LotTaka.objects.filter(location=self.godown2, deleted_at__isnull=True, available_quantity__gt=0)
        self.assertTrue(all(l.status == "Available" for l in received))
        self.assertEqual({l.batch_id for l in received}, {lot_a.batch_id})  # batch kept

        with self.assertRaises(ValidationFailed):  # 15 still in transit needs settling
            wh.complete(t, self.user)
        t = wh.complete(t, self.user, settlements=[{"lineId": line.id, "action": "RETURN", "reason": "Short loaded"}])
        line.refresh_from_db()
        self.assertEqual(t.status, "Completed")
        self.assertEqual((line.discrepancy_qty, line.discrepancy_action), (D("15"), "RETURNED"))
        self.assertEqual(self.on_hand(self.fabric, self.transit()), D("0"))
        self.assertEqual(self.on_hand(self.fabric, self.godown), D("40"))
        self.assertEqual(self.on_hand(self.fabric, self.godown2), D("60"))
        self.assertEqual(self.total(self.fabric), before)  # transfers never change the total

    def test_write_off_in_transit_posts_adjustment_journal(self):
        lot = self.make_lot("50")
        t = wh.approve(self.transfer([(lot, "50")], "50"), self.user)
        t = wh.dispatch(t, self.user)
        line = self.line(t)
        t = wh.receive(t, self.user, lines=[{"lineId": line.id, "qty": "48"}])
        t = wh.complete(t, self.user, settlements=[{"lineId": line.id, "action": "WRITE_OFF", "reason": "Damaged in truck"}])
        self.assertEqual(self.on_hand(self.fabric, self.godown2), D("48"))
        self.assertEqual(self.on_hand(self.fabric, self.transit()), D("0"))
        adj = StockMovement.objects.get(reference_id=t.id, type="ADJUSTMENT")
        self.assertEqual(adj.quantity, D("-2"))
        self.assertTrue(JournalEntry.objects.filter(source_document_type="StockAdjustment", source_document_id=adj.id).exists())

    def test_cancel_after_dispatch_restores_and_after_receipt_is_refused(self):
        lot = self.make_lot("30")
        t = wh.dispatch(wh.approve(self.transfer([(lot, "30")], "30"), self.user), self.user)
        t = wh.cancel(t, self.user)
        lot.refresh_from_db()
        self.assertEqual((t.status, lot.location_id, lot.status), ("Cancelled", self.godown.id, "Available"))

        t2 = wh.dispatch(wh.approve(self.transfer([(lot, "30")], "30"), self.user), self.user)
        wh.receive(t2, self.user, lines=[{"lineId": self.line(t2).id, "qty": "10"}])
        with self.assertRaises(Conflict):
            wh.cancel(t2, self.user)

    def test_taka_reserved_for_transfer_cannot_be_reserved_again(self):
        lot = self.make_lot("30")
        self.transfer([(lot, "30")], "30")
        with self.assertRaises(BusinessRuleViolation):
            self.transfer([(lot, "30")], "30")

    def test_loose_item_transfer(self):
        stock.post_movement(client_id=self.client_obj.id, item=self.plain, location=self.godown,
                            type="ADJUSTMENT", quantity=D("20"), notes="opening", user=self.user)
        with self.assertRaises(BusinessRuleViolation):
            self.transfer([], "25", item=self.plain)
        t = self.transfer([], "12", item=self.plain)
        rows = {r["warehouseId"]: r for r in wh.stock_rows(self.client_obj.id, item_id=self.plain.id)}
        self.assertEqual(D(rows[str(self.godown.id)]["reserved"]), D("12"))
        self.assertEqual(D(rows[str(self.godown.id)]["available"]), D("8"))
        t = wh.receive(wh.dispatch(wh.approve(t, self.user), self.user), self.user)
        self.assertEqual(t.status, "Received")
        self.assertEqual(self.on_hand(self.plain, self.godown2), D("12"))
        self.assertEqual(wh.complete(t, self.user).status, "Completed")

    def test_viewset_flow_and_permissioned_approve(self):
        lot = self.make_lot("20")
        res = self.call(StockTransferViewSet, "post", "/inventory/transfers/", {
            "sourceLocationId": str(self.godown.id), "destLocationId": str(self.godown2.id),
            "asDraft": True, "items": [{"itemId": str(self.fabric.id), "qty": "20",
                                        "takas": [{"lotTakaId": str(lot.id), "qty": "20"}]}],
        }, actions={"post": "create"})
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["status"], "Draft")
        tid = res.data["id"]
        for action, expected in (("submit", "Requested"), ("approve", "Approved"),
                                 ("dispatch_action", "In Transit"), ("receive", "Received"),
                                 ("complete", "Completed")):
            res = self.call(StockTransferViewSet, "post", f"/inventory/transfers/{tid}/x/",
                            actions={"post": action}, pk=tid)
            self.assertEqual(res.status_code, 200, (action, res.data))
            self.assertEqual(res.data["status"], expected)
        self.assertEqual(res.data["lines"][0]["receivedQty"], "20.0000")


class WarehouseStockTests(WarehouseBase):
    def test_rows_split_reserved_qc_customer_and_low_stock(self):
        from apps.inventory.models import Batch

        free = self.make_lot("40", taka="F")
        held = self.make_lot("10", taka="H")
        pending = self.make_lot("5", taka="P")
        LotTaka.objects.filter(pk=held.pk).update(status="Reserved")
        LotTaka.objects.filter(pk=pending.pk).update(qc_status="Pending")
        owned = self.make_lot("7", taka="C")
        Batch.objects.filter(pk=owned.batch_id).update(ownership="CUSTOMER", owner_party=self.customer)
        LotTaka.objects.filter(pk=owned.pk).update(batch_id=owned.batch_id)
        # The customer roll needs a batch of its own.
        if owned.batch_id == free.batch_id:
            b = Batch.objects.create(client=self.client_obj, batch_code="CUST-1", item=self.fabric,
                                     ownership="CUSTOMER", owner_party=self.customer, origin="CUSTOMER")
            LotTaka.objects.filter(pk=owned.pk).update(batch=b)
            Batch.objects.filter(pk=free.batch_id).update(ownership="OSCAR", owner_party=None)
        wh.set_stock_level(self.client_obj.id, self.godown.id, self.fabric, "50")

        row = next(r for r in wh.stock_rows(self.client_obj.id) if r["warehouseId"] == str(self.godown.id))
        self.assertEqual(D(row["physical"]), D("62"))
        self.assertEqual(D(row["reserved"]), D("10"))
        self.assertEqual(D(row["qcPending"]), D("5"))
        self.assertEqual(D(row["customerOwned"]), D("7"))
        self.assertEqual(D(row["available"]), D("40"))
        self.assertTrue(row["lowStock"])
        dash = wh.dashboard(self.client_obj.id)
        self.assertEqual(dash["totalWarehouses"], 2)
        self.assertEqual(dash["lowStockItems"], 1)
        # System stores are not warehouses.
        stock.system_location(self.client_obj.id, "QC_HOLD")
        self.assertEqual(wh.dashboard(self.client_obj.id)["totalWarehouses"], 2)

    def test_legacy_child_location_rolls_up_into_its_warehouse(self):
        rack = Location.objects.create(client=self.client_obj, code="GD-1-R1", name="Rack 1", type="Rack", parent=self.godown)
        self.make_lot("9", location=rack)
        row = next(r for r in wh.stock_rows(self.client_obj.id) if r["warehouseId"] == str(self.godown.id))
        self.assertEqual(D(row["physical"]), D("9"))

    def test_availability_and_shortage_transfer(self):
        self.make_lot("30", location=self.godown2, taka="X")
        info = wh.availability(self.client_obj.id, self.fabric.id, "20", self.godown.id)
        self.assertEqual(D(info["shortage"]), D("20"))
        self.assertTrue(info["canCover"])
        self.assertEqual(info["otherWarehouses"][0]["warehouseId"], str(self.godown2.id))
        t = wh.shortage_transfer(client=self.client_obj, user=self.user, item=self.fabric, qty="20",
                                 from_warehouse_id=self.godown2.id, to_warehouse_id=self.godown.id,
                                 reference={"type": "SalesOrder", "id": None, "number": "SO-1"})
        self.assertEqual((t.status, t.reference_number), ("Requested", "SO-1"))
        with self.assertRaises(BusinessRuleViolation):
            wh.shortage_transfer(client=self.client_obj, user=self.user, item=self.fabric, qty="20",
                                 from_warehouse_id=self.godown2.id, to_warehouse_id=self.godown.id)

    def test_ledger_running_balance(self):
        lot = self.make_lot("30")
        t = wh.receive(wh.dispatch(wh.approve(self.transfer([(lot, "10")], "10"), self.user), self.user), self.user)
        out = wh.ledger(self.client_obj.id, warehouse_id=self.godown.id, item_id=self.fabric.id)
        balances = [D(e["balance"]) for e in out["results"]]
        self.assertEqual(balances, [D("20"), D("30")])  # newest first
        self.assertEqual(out["results"][0]["out"], "10.0000")
        self.assertEqual(out["results"][0]["reference"], t.transfer_number)
        dest = wh.ledger(self.client_obj.id, warehouse_id=self.godown2.id)
        self.assertEqual(D(dest["results"][0]["balance"]), D("10"))
        # In-transit movements are not shown as warehouse stock.
        self.assertTrue(all(e["warehouseId"] in (str(self.godown.id), str(self.godown2.id))
                            for e in wh.ledger(self.client_obj.id)["results"]))


class WarehouseApiTests(WarehouseBase):
    def api(self, view, method, url, data=None, **kw):
        request = getattr(self.factory, method)(url, data or {}, format="json")
        request.client_id = self.client_obj.id
        from rest_framework.test import force_authenticate

        force_authenticate(request, user=self.user)
        return view.as_view()(request, **kw)

    def test_create_update_and_deactivate_guard(self):
        res = self.api(warehouse_views.WarehouseListView, "post", "/inventory/warehouses/", {
            "code": "wh-sur", "name": "Surat Main", "address": "Ring Road", "city": "Surat",
            "state": "Gujarat", "pincode": "395002", "manager": "Store Head", "contact": "9800000000",
        })
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual((res.data["code"], res.data["city"], res.data["pincode"]), ("WH-SUR", "Surat", "395002"))
        bad = self.api(warehouse_views.WarehouseListView, "post", "/inventory/warehouses/", {"code": "WH-SUR", "name": "Dup"})
        self.assertEqual(bad.status_code, 400)
        bad = self.api(warehouse_views.WarehouseListView, "post", "/inventory/warehouses/", {"code": "X", "name": "X", "pincode": "12"})
        self.assertEqual(bad.status_code, 400)

        self.make_lot("5")
        res = self.api(warehouse_views.WarehouseDetailView, "patch", "/x/", {"isActive": False}, pk=self.godown.id)
        self.assertIn(res.status_code, (409, 422), res.data)
        self.godown.refresh_from_db()
        self.assertTrue(self.godown.is_active)
        res = self.api(warehouse_views.WarehouseDetailView, "patch", "/x/", {"isActive": False, "city": "Mumbai"}, pk=self.godown2.id)
        self.assertEqual(res.status_code, 200, res.data)
        self.assertFalse(res.data["isActive"])
