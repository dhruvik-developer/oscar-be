"""Dyeing as an independent service: customer-fabric service orders, OSCAR
grey + dyeing orders, and the Grey / Dyeing QC split."""
from decimal import Decimal

from rest_framework.test import force_authenticate

from apps.accounting import services as ledger
from apps.accounting.models import JournalLine
from apps.core.exceptions import BusinessRuleViolation
from apps.inventory import qc as qc_service
from apps.inventory.models import Batch, LotTaka, StockMovement
from apps.inventory.tests_stock_integrity import StockBase
from apps.jobwork import service_orders as svc
from apps.jobwork.models import JWOInward, ServiceOrder
from apps.jobwork.service_order_views import ServiceOrderDetailView, ServiceOrderListView
from apps.masters.models import Party

D = Decimal


class ServiceOrderBase(StockBase):
    def setUp(self):
        super().setUp()
        self.dyer = Party.objects.create(client=self.client_obj, code="DY-1", type="Vendor", name="Shree Dyers")

    def api(self, view, method, data=None, **kw):
        request = getattr(self.factory, method)("/jobwork/service-orders/", data or {}, format="json")
        request.client_id = self.client_obj.id
        force_authenticate(request, user=self.user)
        res = view.as_view()(request, **kw)
        return res

    def act(self, order_id, action, data=None):
        res = self.api(ServiceOrderDetailView, "post", data, pk=order_id, action_name=action)
        self.assertEqual(res.status_code, 200, (action, getattr(res, "data", None)))
        return res.data

    def new_order(self, order_type, **extra):
        res = self.api(ServiceOrderListView, "post", {
            "orderType": order_type, "partyId": str(self.customer.id), "itemId": str(self.fabric.id),
            "quantity": "100", "serviceRate": "12", "vendorRate": "8", "vendorId": str(self.dyer.id),
            "shade": "Navy", **extra,
        })
        self.assertEqual(res.status_code, 201, res.data)
        return res.data

    def send_and_return(self, order, returned):
        """Open a JWO, send every free roll, get ``returned`` metres per roll back."""
        data = self.act(order["id"], "create-jwo", {})
        jwo = data["jobWorkOrders"][0]
        rolls = [r for r in data["availableRolls"] if r["status"] != "External WIP"]
        data = self.act(order["id"], "send", {"jwoId": jwo["id"], "takas": [
            {"lotTakaId": r["lotTakaId"], "meters": r["qty"]} for r in rolls]})
        self.assertEqual(data["status"], "At Vendor")
        at_vendor = data["jobWorkOrders"][0]["atVendorRolls"]
        data = self.act(order["id"], "receive-return", {"jwoId": jwo["id"], "rows": [
            {"sourceTakaId": r["lotTakaId"], "meters": str(m)} for r, m in zip(at_vendor, returned)]})
        self.assertEqual(data["status"], "Dyeing QC")
        return data, JWOInward.objects.get(job_work_order_id=jwo["id"])

    def pass_qc(self, inward, qty):
        qc_service.dispose_jobwork_inward(inward, quantities={"good": qty}, good_location_id=self.godown.id,
                                          user=self.user)


class CustomerFabricServiceTests(ServiceOrderBase):
    def test_full_service_flow_bills_only_the_service(self):
        order = self.new_order("CUSTOMER_FABRIC")
        self.assertEqual(order["serviceValue"], "1200.00")  # 100 x 12, entered rates only
        self.act(order["id"], "confirm")
        data = self.act(order["id"], "receive-fabric", {
            "locationId": str(self.godown.id), "rolls": [{"meters": "60"}, {"meters": "40"}]})
        self.assertEqual(data["status"], "Fabric Received")
        batch = Batch.objects.get(batch_code=data["batchCodes"][0])
        self.assertEqual((batch.ownership, batch.owner_party_id), ("CUSTOMER", self.customer.id))

        data, inward = self.send_and_return(order, ["58", "40"])
        # Dyed rolls keep the customer's batch, so they stay customer-owned.
        dyed = LotTaka.objects.filter(source_document_type="JobWorkInward", deleted_at__isnull=True)
        self.assertEqual({l.batch_id for l in dyed}, {batch.id})
        # The vendor's dyeing charge is a cost of the service, not stock.
        cogs = ledger.system_account(self.client_obj.id, "cogs")
        self.assertEqual(
            sum((l.debit for l in JournalLine.objects.filter(client_id=self.client_obj.id, account=cogs)), D(0)),
            D("784.00"),  # 98 m x 8
        )
        self.pass_qc(inward, D("98"))
        data = self.act(order["id"], "deliver", {"rolls": [
            {"lotTakaId": r["lotTakaId"], "qty": r["qty"]} for r in svc.deliverable_rolls(ServiceOrder.objects.get(pk=order["id"]))]})
        self.assertEqual(data["status"], "Delivered")
        self.assertEqual(data["quantities"]["delivered"], "98.0000")

        data = self.act(order["id"], "invoice")
        from apps.sales.models import SalesInvoice

        invoice = SalesInvoice.objects.get(pk=data["salesInvoiceId"])
        lines = list(invoice.line_items.filter(deleted_at__isnull=True))
        self.assertEqual(invoice.status, "Draft")
        self.assertEqual(len(lines), 1)
        self.assertIsNone(lines[0].item_id)  # a service line: no stock, no COGS
        self.assertEqual((lines[0].qty, lines[0].rate), (D("98.0000"), D("12.0000")))
        self.assertEqual(data["costing"]["actual"]["dyeingCharges"], "784.00")

    def test_oscar_batch_refused_and_foreign_roll_refused(self):
        order = self.new_order("CUSTOMER_FABRIC")
        self.act(order["id"], "confirm")
        lot = self.make_lot("30")
        res = self.api(ServiceOrderDetailView, "post", {"batchCode": lot.batch.batch_code},
                       pk=order["id"], action_name="link-batch")
        self.assertIn(res.status_code, (409, 422), res.data)
        self.act(order["id"], "receive-fabric", {"locationId": str(self.godown.id), "totalMeters": "50"})
        data = self.act(order["id"], "create-jwo")
        res = self.api(ServiceOrderDetailView, "post", {
            "jwoId": data["jobWorkOrders"][0]["id"], "takas": [{"lotTakaId": str(lot.id), "meters": "30"}]},
            pk=order["id"], action_name="send")
        self.assertNotEqual(res.status_code, 200)  # an OSCAR roll cannot ride on a customer's order
        lot.refresh_from_db()
        self.assertEqual(lot.location_id, self.godown.id)


class GreyPlusDyeingTests(ServiceOrderBase):
    def test_grey_batch_dyed_and_invoiced_as_fabric_plus_dyeing(self):
        lot = self.make_lot("100")
        order = self.new_order("OSCAR_GREY", fabricRate="70")
        self.act(order["id"], "confirm")
        data = self.act(order["id"], "link-batch", {"batchCode": lot.batch.batch_code})
        self.assertEqual(data["batchCodes"], [lot.batch.batch_code])
        data, inward = self.send_and_return(order, ["97"])
        self.pass_qc(inward, D("97"))
        stock_acct = ledger.system_account(self.client_obj.id, "stock")
        self.assertTrue(JournalLine.objects.filter(account=stock_acct, debit=D("776.00")).exists())  # 97 x 8 into stock

        data = self.act(order["id"], "invoice")
        from apps.sales.models import SalesInvoice

        lines = list(SalesInvoice.objects.get(pk=data["salesInvoiceId"]).line_items.filter(deleted_at__isnull=True).order_by("line_no"))
        self.assertEqual(len(lines), 2)
        self.assertEqual((lines[0].item_id, lines[0].qty, lines[0].rate), (self.fabric.id, D("97.0000"), D("70.0000")))
        self.assertTrue(lines[0].taka_allocations)
        self.assertIsNone(lines[1].item_id)
        self.assertEqual(lines[1].rate, D("12.0000"))
        costing = data["costing"]["actual"]
        self.assertEqual(costing["greyFabricCost"], "5000.00")  # 100 m sent x 50 landed
        self.assertEqual(costing["dyeingCharges"], "776.00")

    def test_customer_batch_refused_for_grey_sale(self):
        order = self.new_order("OSCAR_GREY")
        self.act(order["id"], "confirm")
        other = self.new_order("CUSTOMER_FABRIC")
        self.act(other["id"], "confirm")
        data = self.act(other["id"], "receive-fabric", {"locationId": str(self.godown.id), "totalMeters": "10"})
        with self.assertRaises(BusinessRuleViolation):
            svc.link_batch(ServiceOrder.objects.get(pk=order["id"]), data["batchCodes"][0])


class QcStageSplitTests(ServiceOrderBase):
    def test_grey_and_dyeing_qc_lists_do_not_mix(self):
        from apps.inventory.views import QCPendingInspectionsView

        bill = self.make_bill()
        self.receive(bill)
        from apps.purchase.models import GoodsReceipt

        GoodsReceipt.objects.filter(purchase_bill=bill).update(qc_status="Pending")
        order = self.new_order("CUSTOMER_FABRIC")
        self.act(order["id"], "confirm")
        self.act(order["id"], "receive-fabric", {"locationId": str(self.godown.id), "rolls": [{"meters": "20"}]})
        _, inward = self.send_and_return(order, ["20"])

        def pending(stage):
            request = self.factory.get("/inventory/qc/pending/", {"stage": stage})
            request.client_id = self.client_obj.id
            force_authenticate(request, user=self.user)
            res = QCPendingInspectionsView.as_view()(request)
            self.assertEqual(res.status_code, 200, res.data)
            return res.data["pendingList"]

        grey, dyeing = pending("grey"), pending("dyeing")
        self.assertTrue(grey)
        self.assertTrue(all(r.get("sourceType") == "Purchase" for r in grey))
        self.assertTrue(dyeing)
        self.assertTrue(all(r.get("sourceType") == "Job Work" for r in dyeing))
        self.assertIn(inward.no, {r["refNo"] for r in dyeing})
        self.assertNotIn(inward.no, {r["refNo"] for r in grey})


class ServiceOrderCancelTests(ServiceOrderBase):
    def test_cancel_allowed_until_fabric_goes_to_the_vendor(self):
        order = self.new_order("CUSTOMER_FABRIC")
        self.act(order["id"], "confirm")
        self.act(order["id"], "receive-fabric", {"locationId": str(self.godown.id), "rolls": [{"meters": "25"}]})
        data = self.act(order["id"], "create-jwo")
        # A JWO with nothing sent does not block cancelling.
        self.assertEqual(self.act(order["id"], "cancel", {"reason": "Customer withdrew"})["status"], "Cancelled")

        other = self.new_order("CUSTOMER_FABRIC")
        self.act(other["id"], "confirm")
        self.act(other["id"], "receive-fabric", {"locationId": str(self.godown.id), "rolls": [{"meters": "25"}]})
        data = self.act(other["id"], "create-jwo")
        roll = data["availableRolls"][0]
        self.act(other["id"], "send", {"jwoId": data["jobWorkOrders"][0]["id"],
                                       "takas": [{"lotTakaId": roll["lotTakaId"], "meters": roll["qty"]}]})
        res = self.api(ServiceOrderDetailView, "post", {"reason": "x"}, pk=other["id"], action_name="cancel")
        self.assertIn(res.status_code, (409, 422), res.data)


class QcTypeFilterEveryScreenTests(ServiceOrderBase):
    """The All / Grey Fabric / Dyeing filter changes the data on every QC
    screen: Grey = goods receipts, Dyeing = job work (dyeing) inwards, All =
    both, each record once."""

    def setUp(self):
        super().setUp()
        from apps.jobwork.models import JWOReprocess
        from apps.purchase.models import GoodsReceipt

        bill = self.make_bill()
        self.receive(bill)
        self.grn = GoodsReceipt.objects.get(purchase_bill=bill)
        GoodsReceipt.objects.filter(pk=self.grn.pk).update(qc_status="Pending")
        order = self.new_order("CUSTOMER_FABRIC")
        self.act(order["id"], "confirm")
        self.act(order["id"], "receive-fabric", {"locationId": str(self.godown.id), "rolls": [{"meters": "20"}]})
        _, self.inward = self.send_and_return(order, ["20"])
        jwo = self.inward.job_work_order
        # One reprocess of each kind: raised from GRN QC (grey) and from a dyeing inward.
        JWOReprocess.objects.create(client=self.client_obj, job_work_order=jwo, no="RP-GREY", inward_no=self.grn.grn_number,
                                    date=self.grn.receipt_date, qty=D("5"))
        JWOReprocess.objects.create(client=self.client_obj, job_work_order=jwo, no="RP-DYE", inward_no=self.inward.no,
                                    date=self.inward.date, qty=D("3"))

    def fetch(self, view, url, stage, **params):
        import json

        query = dict(params)
        if stage:
            query["stage"] = stage
        request = self.factory.get(url, query)
        request.client_id = self.client_obj.id
        force_authenticate(request, user=self.user)
        res = view.as_view()(request)
        self.assertEqual(res.status_code, 200, getattr(res, "data", None))
        return json.dumps(res.data, default=str)

    def check(self, view, url, grey_marker, dyeing_marker, **params):
        everything = self.fetch(view, url, None, **params)
        grey = self.fetch(view, url, "grey", **params)
        dyeing = self.fetch(view, url, "dyeing", **params)
        self.assertIn(grey_marker, everything, url)
        self.assertIn(dyeing_marker, everything, url)
        self.assertIn(grey_marker, grey, url)
        self.assertNotIn(dyeing_marker, grey, url)
        self.assertIn(dyeing_marker, dyeing, url)
        self.assertNotIn(grey_marker, dyeing, url)

    def test_dashboard_pending_register_disposition_reports(self):
        from apps.inventory import views as v

        g, d = self.grn.grn_number, self.inward.no
        self.check(v.QCDashboardView, "/inventory/qc/dashboard/", g, d)
        self.check(v.QCPendingInspectionsView, "/inventory/qc/pending/", g, d)
        self.check(v.QCRegisterView, "/inventory/qc/register/", g, d)
        self.check(v.QCDispositionView, "/inventory/qc/disposition/", g, d)
        self.check(v.QCReportsView, "/inventory/qc/reports/", g, d)

    def test_rework_screen_splits_reprocess_by_its_origin(self):
        from apps.inventory import views as v

        self.check(v.QCReinspectionView, "/inventory/qc/reinspection/", "RP-GREY", "RP-DYE", tab="All")

    def test_pending_lists_each_record_once_under_all(self):
        from apps.inventory.views import QCPendingInspectionsView

        request = self.factory.get("/inventory/qc/pending/")
        request.client_id = self.client_obj.id
        force_authenticate(request, user=self.user)
        rows = QCPendingInspectionsView.as_view()(request).data["pendingList"]
        ids = [r["id"] for r in rows]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(sum(1 for r in rows if r["refNo"] == self.inward.no), 1)
