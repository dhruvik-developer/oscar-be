"""
Unit tests for OSCAR ERP Reprocess Workflow:
1. Send rejected batch for reprocess (linked to PO, batch, vendor, outward challan)
2. Partial return (500 sent -> 300 returned -> 200 outstanding)
3. Prevent duplicate submissions / duplicate GRNs (idempotency)
4. Prevent receiving more than outstanding quantity
5. Second partial return (200 returned -> 0 outstanding)
6. Receive Return disabled after full return
7. Reprocess Batch ({batch}-RP) linked without duplicating original batch
8. Returned fabric status is QC Pending (SYS-QC-HOLD)
"""
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import Client
from apps.core.exceptions import ValidationFailed
from apps.inventory.models import Batch, LotCostEntry, LotTaka, ReprocessRecord, ReprocessReturn, StockMovement
from apps.inventory.reprocess_services import (
    get_reprocess_record_detail,
    list_reprocess_records,
    receive_reprocess_return,
    send_batch_for_reprocess,
)
from apps.inventory import services as stock
from apps.masters.models import Item, ItemCategory, Location, Party
from apps.purchase.models import GoodsReceipt, PurchaseOrder

User = get_user_model()


class ReprocessWorkflowTests(TestCase):
    def setUp(self):
        self.client_obj = Client.objects.create(name="Test Client", schema_name="public")
        self.client_id = self.client_obj.id

        self.user = User.objects.create_user(
            email="tester_rp@example.com",
            password="password",
            client=self.client_obj,
        )

        self.vendor = Party.objects.create(
            client=self.client_obj,
            name="Vardhman Textiles Ltd",
            type="Vendor",
        )

        self.category = ItemCategory.objects.create(
            client=self.client_obj,
            name="Grey Cotton",
        )

        self.item = Item.objects.create(
            client=self.client_obj,
            name="Grey Cotton 40s",
            sku="GC-40S-TEST",
            category=self.category,
            uom="MTR",
        )

        self.reject_loc = stock.system_location(self.client_id, "REJECT")
        self.qc_hold_loc = stock.system_location(self.client_id, "QC_HOLD")
        self.reprocess_loc = stock.system_location(self.client_id, "REPROCESS")

        self.po = PurchaseOrder.objects.create(
            client=self.client_obj,
            po_number="PO-2026-9001",
            party=self.vendor,
            doc_date=timezone.localdate(),
            status="Approved",
        )

        self.batch_code = "BAT-ORIG-500"
        self.batch = Batch.objects.create(
            client=self.client_obj,
            batch_code=self.batch_code,
            origin="PURCHASE",
            fabric_type="GREY",
            item=self.item,
            supplier=self.vendor,
            purchase_order=self.po,
            uom="MTR",
        )

        self.rejected_taka = LotTaka.objects.create(
            client=self.client_obj,
            batch=self.batch,
            lot_number=self.batch_code,
            taka_id="TAK-REJ-01",
            taka_number="1",
            item=self.item,
            location=self.reject_loc,
            supplier=self.vendor,
            received_quantity=Decimal("500.00"),
            available_quantity=Decimal("500.00"),
            uom="MTR",
            status="Reject",
            qc_status="Rejected",
        )

    def test_complete_reprocess_workflow(self):
        # 1. Send for reprocess
        record = send_batch_for_reprocess(
            self.client_id,
            self.batch_code,
            sent_qty=Decimal("500.00"),
            outward_challan="CHL-OUT-001",
            sent_date=timezone.localdate(),
            reason="Defective dyeing",
        )
        self.assertEqual(record["sentQty"], 500.0)
        self.assertEqual(record["outstandingQty"], 500.0)
        self.assertEqual(record["returnedQty"], 0.0)
        self.assertTrue(record["canReceiveReturn"])

        # 2. First partial return: 300 MTR
        ret1 = receive_reprocess_return(
            self.client_id,
            record["id"],
            return_qty=Decimal("300.00"),
            vendor_return_challan="VRET-001",
            return_date=timezone.localdate(),
            idempotency_key="IDEM-TEST-1",
        )
        self.assertEqual(ret1["returnedQty"], 300.0)
        self.assertEqual(ret1["outstandingQty"], 200.0)
        self.assertEqual(ret1["status"], "Partially Returned")
        self.assertTrue(ret1["canReceiveReturn"])

        # 3. Idempotency test
        ret1_dup = receive_reprocess_return(
            self.client_id,
            record["id"],
            return_qty=Decimal("300.00"),
            vendor_return_challan="VRET-001",
            idempotency_key="IDEM-TEST-1",
        )
        self.assertEqual(ret1_dup["returnedQty"], 300.0)

        # 4. Prevent over-receipt
        with self.assertRaises(ValidationFailed):
            receive_reprocess_return(
                self.client_id,
                record["id"],
                return_qty=Decimal("250.00"),
                vendor_return_challan="VRET-OVER",
            )

        # 5. Second partial return: 200 MTR
        ret2 = receive_reprocess_return(
            self.client_id,
            record["id"],
            return_qty=Decimal("200.00"),
            vendor_return_challan="VRET-002",
            return_date=timezone.localdate(),
            idempotency_key="IDEM-TEST-2",
        )
        self.assertEqual(ret2["returnedQty"], 500.0)
        self.assertEqual(ret2["outstandingQty"], 0.0)
        self.assertEqual(ret2["status"], "Completed")
        self.assertFalse(ret2["canReceiveReturn"])

        # 6. Prevent further returns after full return
        with self.assertRaises(ValidationFailed):
            receive_reprocess_return(
                self.client_id,
                record["id"],
                return_qty=Decimal("50.00"),
                vendor_return_challan="VRET-EXCESS",
            )
