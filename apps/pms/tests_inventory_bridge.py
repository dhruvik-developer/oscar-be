"""
Integration tests for PMS <-> Inventory & Purchase Bridge.
"""
from decimal import Decimal
from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import Client, User
from apps.masters.models import Party, Location, Item
from apps.pms.models_loom import (
    LoomFactory, LoomMachine, YarnMaster, LoomQuality, LoomBeam,
    DailyLoomProduction, YarnBooking, YarnDelivery, FabricDispatch
)
from apps.inventory.models import StockMovement, StockBalance, LotTaka, Batch
from apps.pms.inventory_bridge import (
    ensure_yarn_item,
    ensure_fabric_item,
    ensure_factory_location,
    sync_yarn_delivery_receipt_to_inventory,
    sync_beam_warp_consumption_to_inventory,
    sync_daily_weft_consumption_to_inventory,
    sync_fabric_dispatch_to_inventory,
)


class PMSInventoryBridgeTestCase(TestCase):
    def setUp(self):
        self.client_obj = Client.objects.create(name="Test Textile Mill", schema_name="test_textile")
        self.user = User.objects.create_user(
            username="coordinator", email="coord@mill.com", password="password123", client=self.client_obj
        )
        self.factory = LoomFactory.objects.create(
            client=self.client_obj, name="Unit 1 Weaving Shed"
        )
        self.machine = LoomMachine.objects.create(
            client=self.client_obj, factory=self.factory, no=1, reed=84
        )
        self.supplier = Party.objects.create(
            client=self.client_obj, name="Gujarat Spinners Ltd", type="Vendor"
        )
        self.yarn_warp = YarnMaster.objects.create(
            client=self.client_obj, name="Cotton 60s Warp", denier=Decimal("88.58"), rate=Decimal("240.00")
        )
        self.yarn_weft = YarnMaster.objects.create(
            client=self.client_obj, name="Poly 30s Weft", denier=Decimal("177.16"), rate=Decimal("180.00")
        )
        self.quality = LoomQuality.objects.create(
            client=self.client_obj,
            name="Poplin 60x30",
            warp_yarn=self.yarn_warp,
            ends=4800,
            crimp=Decimal("5.00"),
            weft_yarn=self.yarn_weft,
            reed=84,
            reed_w=Decimal("58.00"),
            ppi=Decimal("56.00"),
            wastage=Decimal("8.00"),
            pick_charge=Decimal("0.15"),
        )

    def test_ensure_items_and_locations(self):
        yarn_item = ensure_yarn_item(self.yarn_warp, self.client_obj.id)
        self.assertIsNotNone(yarn_item)
        self.assertEqual(yarn_item.item_kind, "Yarn")
        self.assertEqual(yarn_item.uom, "kg")

        fabric_item = ensure_fabric_item(self.quality, self.client_obj.id)
        self.assertIsNotNone(fabric_item)
        self.assertEqual(fabric_item.item_kind, "Fabric")
        self.assertEqual(fabric_item.tracking_type, "TAKA")

        loc = ensure_factory_location(self.factory, self.client_obj.id)
        self.assertIsNotNone(loc)
        self.assertEqual(self.factory.warehouse_id, loc.id)

    def test_yarn_delivery_receipt_bridge(self):
        booking = YarnBooking.objects.create(
            client=self.client_obj,
            supplier=self.supplier.name,
            supplier_party=self.supplier,
            yarn=self.yarn_warp,
            kgs=Decimal("1000.00"),
            rate=Decimal("240.00"),
        )
        delivery = YarnDelivery.objects.create(
            client=self.client_obj,
            booking=booking,
            factory=self.factory,
            kgs=Decimal("1000.00"),
            rate=Decimal("240.00"),
            due=timezone.localdate(),
            recd_kgs=Decimal("1000.00"),
            recd_date=timezone.localdate(),
            status="Received",
        )

        movement = sync_yarn_delivery_receipt_to_inventory(delivery, user=self.user)
        self.assertIsNotNone(movement)
        self.assertEqual(movement.type, "PURCHASE")
        self.assertEqual(movement.quantity, Decimal("1000.0000"))

        balance = StockBalance.objects.get(
            client_id=self.client_obj.id, item_id=movement.item_id, location_id=movement.location_id
        )
        self.assertEqual(balance.on_hand, Decimal("1000.0000"))

    def test_beam_warp_and_daily_weft_consumption_bridge(self):
        # 1. Inward yarn first
        booking = YarnBooking.objects.create(
            client=self.client_obj,
            supplier=self.supplier.name,
            supplier_party=self.supplier,
            yarn=self.yarn_warp,
            kgs=Decimal("500.00"),
            rate=Decimal("240.00"),
        )
        deliv = YarnDelivery.objects.create(
            client=self.client_obj,
            booking=booking,
            factory=self.factory,
            kgs=Decimal("500.00"),
            rate=Decimal("240.00"),
            due=timezone.localdate(),
            recd_kgs=Decimal("500.00"),
            recd_date=timezone.localdate(),
            status="Received",
        )
        sync_yarn_delivery_receipt_to_inventory(deliv, user=self.user)

        # 2. Warp beam consumption
        beam = LoomBeam.objects.create(
            client=self.client_obj,
            beam_no="B101",
            factory=self.factory,
            quality=self.quality,
            length=Decimal("1000.00"),
            status="On loom",
            machine=self.machine,
        )
        warp_mv = sync_beam_warp_consumption_to_inventory(beam, user=self.user)
        self.assertIsNotNone(warp_mv)
        self.assertEqual(warp_mv.type, "PRODUCTION_ISSUE")
        self.assertTrue(warp_mv.quantity < Decimal("0"))

        # 3. Daily weft consumption
        daily_prod = DailyLoomProduction.objects.create(
            client=self.client_obj,
            factory=self.factory,
            machine=self.machine,
            beam=beam,
            date=timezone.localdate(),
            meters=Decimal("150.00"),
        )
        weft_mv = sync_daily_weft_consumption_to_inventory(daily_prod, user=self.user)
        self.assertIsNotNone(weft_mv)
        self.assertEqual(weft_mv.type, "PRODUCTION_ISSUE")
        self.assertTrue(weft_mv.quantity < Decimal("0"))

    def test_fabric_dispatch_to_godown_generates_lot_taka_and_stock(self):
        dispatch = FabricDispatch.objects.create(
            client=self.client_obj,
            factory=self.factory,
            quality=self.quality,
            date=timezone.localdate(),
            taka=3,
            meters=Decimal("300.00"),
            dest="Godown",
            challan="SL/101",
        )

        takas = sync_fabric_dispatch_to_inventory(dispatch, user=self.user)
        self.assertIsNotNone(takas)
        self.assertEqual(len(takas), 3)

        total_taka_m = sum(t.available_quantity for t in takas)
        self.assertEqual(total_taka_m, Decimal("300.0000"))

        self.assertIsNotNone(dispatch.batch)
        self.assertTrue(dispatch.batch.batch_code.startswith("BATCH-GREY-"))

        # Verify stock movement into godown
        movements = StockMovement.objects.filter(
            client=self.client_obj, reference_type="FabricDispatch", reference_id=dispatch.id
        )
        self.assertEqual(movements.count(), 3)
        self.assertEqual(sum(m.quantity for m in movements), Decimal("300.0000"))
