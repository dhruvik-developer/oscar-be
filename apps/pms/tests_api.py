import json
import uuid
from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, User
from apps.accounting.services import seed_chart_of_accounts
from apps.masters.models import Location, Party, Item, FabricQuality
from apps.pms.models import Project, ProjectStage, Settings
from apps.pms.models_loom import (
    LoomCoordinator,
    LoomFactory,
    LoomMachine,
    YarnMaster,
    LoomQuality,
    LoomBeam,
    DailyLoomProduction,
    FactoryDayLog,
    YarnBooking,
    YarnDelivery,
    ProductionProgram,
    FabricDispatch,
)
from apps.sales.models import DeliveryChallan, DeliveryChallanLine


class PmsApiTests(TestCase):
    def setUp(self):
        self.suffix = uuid.uuid4().hex[:8]
        self.tenant = Client.objects.create(
            name=f"Test Tenant {self.suffix}",
            slug=f"tenant-{self.suffix}",
        )
        self.admin = User.objects.create_user(
            email=f"admin-{self.suffix}@example.com",
            password="testpassword",
            client=self.tenant,
            is_superuser=True,
            name="Admin User",
        )
        self.plain_user = User.objects.create_user(
            email=f"user-{self.suffix}@example.com",
            password="testpassword",
            client=self.tenant,
            is_superuser=False,
            name="Plain User",
        )
        seed_chart_of_accounts(self.tenant)

        self.client_api = APIClient()
        self.client_api.credentials(
            HTTP_AUTHORIZATION=f"Bearer {build_tokens(self.admin)['access']}"
        )

        self.unauth_client = APIClient()
        self.unauth_client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {build_tokens(self.plain_user)['access']}"
        )

    def test_get_loom_list_endpoints_and_overview(self):
        """All loom list endpoints and dashboard overview return HTTP 200."""
        endpoints = [
            "/api/v1/pms/loom/dashboard-overview/",
            "/api/v1/pms/loom/coordinators/",
            "/api/v1/pms/loom/factories/",
            "/api/v1/pms/loom/machines/",
            "/api/v1/pms/loom/yarns/",
            "/api/v1/pms/loom/qualities/",
            "/api/v1/pms/loom/beams/",
            "/api/v1/pms/loom/daily-production/",
            "/api/v1/pms/loom/yarn-bookings/",
            "/api/v1/pms/loom/yarn-deliveries/",
            "/api/v1/pms/loom/programs/",
            "/api/v1/pms/loom/fabric-dispatches/",
        ]
        for ep in endpoints:
            res = self.client_api.get(ep)
            self.assertEqual(res.status_code, 200, f"Failed at {ep}: {res.data if hasattr(res, 'data') else res}")

    def test_demo_data_permissions_and_reset(self):
        """Demo data endpoint rejects non-privileged user with 403, allows privileged user with 200."""
        # Non-privileged user
        res_denied = self.unauth_client.post("/api/v1/pms/loom/demo-data/", {"action": "reset"})
        self.assertEqual(res_denied.status_code, 403)
        self.assertIn("code", res_denied.data)

        # Superuser
        res_ok = self.client_api.post("/api/v1/pms/loom/demo-data/", {"action": "reset"})
        self.assertEqual(res_ok.status_code, 200)
        self.assertEqual(res_ok.data.get("detail"), "Demo dataset reseeded successfully")

    def test_machines_batch_create_validation(self):
        """POST /pms/loom/machines/batch_create/ rejects invalid input with 400 and creates with valid input."""
        # Missing factory
        res_missing = self.client_api.post("/api/v1/pms/loom/machines/batch_create/", {
            "from_no": 1,
            "to_no": 3,
        })
        self.assertEqual(res_missing.status_code, 400)
        self.assertIn("factory", res_missing.data.get("field_errors", {}))

        # Invalid range (from_no > to_no)
        factory = LoomFactory.objects.create(client=self.tenant, name="Test Fact 1", code=f"TF1-{self.suffix}")
        res_bad_range = self.client_api.post("/api/v1/pms/loom/machines/batch_create/", {
            "factory": str(factory.id),
            "from_no": 10,
            "to_no": 5,
        })
        self.assertEqual(res_bad_range.status_code, 400)

        # Valid payload (accepts factory or factory_id)
        res_valid = self.client_api.post("/api/v1/pms/loom/machines/batch_create/", {
            "factory": str(factory.id),
            "from_no": 1,
            "to_no": 3,
            "reed": 84,
        })
        self.assertEqual(res_valid.status_code, 201)
        self.assertEqual(res_valid.data.get("created"), 3)
        self.assertEqual(LoomMachine.objects.filter(factory=factory, client=self.tenant).count(), 3)

    def test_daily_production_bulk_save_validation(self):
        """POST /pms/loom/daily-production/bulk_save/ returns 400 on bad input, 200 on valid."""
        # Missing factory_id
        res_missing = self.client_api.post("/api/v1/pms/loom/daily-production/bulk_save/", {
            "date": "2026-11-17",
            "power": 0,
            "rows": [],
        })
        self.assertEqual(res_missing.status_code, 400)

        factory = LoomFactory.objects.create(client=self.tenant, name="Test Fact 2", code=f"TF2-{self.suffix}")
        machine = LoomMachine.objects.create(client=self.tenant, factory=factory, no=1, reed=84)
        yarn = YarnMaster.objects.create(client=self.tenant, name=f"Cotton 40s {self.suffix}", denier=150, rate=Decimal("250.00"))
        quality = LoomQuality.objects.create(
            client=self.tenant,
            name=f"Standard Sheeting {self.suffix}",
            warp_yarn=yarn,
            weft_yarn=yarn,
            ends=4000,
            reed=84,
            ppi=Decimal("60"),
        )
        beam = LoomBeam.objects.create(
            client=self.tenant,
            beam_no=f"B-{self.suffix}",
            factory=factory,
            quality=quality,
            length=Decimal("1000"),
            machine=machine,
            status="On loom",
        )

        # Malformed date
        res_bad_date = self.client_api.post("/api/v1/pms/loom/daily-production/bulk_save/", {
            "factory_id": str(factory.id),
            "date": "invalid-date",
            "power": 0,
            "rows": [],
        })
        self.assertEqual(res_bad_date.status_code, 400)

        # Valid save
        res_valid = self.client_api.post("/api/v1/pms/loom/daily-production/bulk_save/", {
            "factory_id": str(factory.id),
            "date": "2026-11-17",
            "power": 12.5,
            "rows": [
                {
                    "machine_id": str(machine.id),
                    "beam_id": str(beam.id),
                    "meters": 150.5,
                    "rpm": 650,
                    "hours": 22,
                    "stop": 0,
                    "damage": 0,
                }
            ],
        }, format="json")
        self.assertEqual(res_valid.status_code, 200)
        self.assertEqual(res_valid.data.get("saved_count"), 1)
        self.assertTrue(DailyLoomProduction.objects.filter(factory=factory, date="2026-11-17").exists())

    def test_get_generic_pms_endpoints(self):
        """Generic PMS dashboard, delays, watchlist, settings, reports endpoints return 200."""
        Settings.objects.get_or_create(client=self.tenant, defaults={"at_risk_threshold_pct": 70})
        endpoints = [
            "/api/v1/pms/dashboard/",
            "/api/v1/pms/delays/",
            "/api/v1/pms/delays/watchlist/",
            "/api/v1/pms/settings/",
            "/api/v1/pms/reports/delay-reason-pareto/",
        ]
        for ep in endpoints:
            res = self.client_api.get(ep)
            self.assertEqual(res.status_code, 200, f"Failed at {ep}: {res.data if hasattr(res, 'data') else res}")

    def test_bug1_crm_order_id_free_project_detail(self):
        """Bug 1 Regression: Project without sales_order serializes without AttributeError / 500."""
        project = Project.objects.create(
            client=self.tenant,
            code=f"PRJ-TEST-{self.suffix}",
            customer_name="Test Customer",
            status="In Progress",
            quantity=Decimal("500"),
            requirement_meters=Decimal("500"),
        )
        res = self.client_api.get(f"/api/v1/pms/projects/{project.id}/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data.get("code"), project.code)

        # Also customer-returns workspace endpoint
        res_ret = self.client_api.get(f"/api/v1/pms/projects/{project.id}/customer-returns/")
        self.assertEqual(res_ret.status_code, 200)

    def test_bug2_zero_req_qty_customer_dispatch(self):
        """Bug 2 Regression: Zero requirement_meters and positive dispatched qty does NOT divide by zero."""
        project = Project.objects.create(
            client=self.tenant,
            code=f"PRJ-ZERO-{self.suffix}",
            customer_name="Test Zero Customer",
            status="In Progress",
            quantity=Decimal("0"),
            requirement_meters=Decimal("0"),
        )
        # Create delivery challan with positive quantity and required customer party
        customer = Party.objects.create(
            client=self.tenant,
            code=f"CUST-{self.suffix}",
            name="Test Customer",
            type="Customer",
        )
        loc = Location.objects.create(client=self.tenant, name="WH-MAIN", code=f"WH-MAIN-{self.suffix}")
        item = Item.objects.create(client=self.tenant, sku=f"ITEM-{self.suffix}", name="Test Fabric", uom="Meters")
        dc = DeliveryChallan.objects.create(
            client=self.tenant,
            party=customer,
            challan_number=f"DC-{self.suffix}",
            doc_date=timezone.localdate(),
            status="Dispatched",
            notes=f"Project: {project.code}",
        )
        DeliveryChallanLine.objects.create(
            client=self.tenant,
            delivery_challan=dc,
            item=item,
            line_no=1,
            qty=Decimal("150"),
        )

        # GET /pms/projects/{id}/ evaluates Customer Dispatch stage
        res = self.client_api.get(f"/api/v1/pms/projects/{project.id}/")
        self.assertEqual(res.status_code, 200)

    def test_tenant_scoping_loom_data(self):
        """Two tenants only see their own loom data (no cross-tenant leakage)."""
        suffix2 = uuid.uuid4().hex[:8]
        tenant2 = Client.objects.create(name=f"Tenant Two {suffix2}", slug=f"tenant-two-{suffix2}")
        admin2 = User.objects.create_user(
            email=f"admin2-{suffix2}@example.com",
            password="testpassword",
            client=tenant2,
            is_superuser=True,
            name="Admin Two",
        )
        seed_chart_of_accounts(tenant2)
        client2_api = APIClient()
        client2_api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(admin2)['access']}")

        # Tenant 1 creates factory
        f1 = LoomFactory.objects.create(client=self.tenant, name="Factory T1", code=f"F1-{self.suffix}")
        # Tenant 2 creates factory
        f2 = LoomFactory.objects.create(client=tenant2, name="Factory T2", code=f"F2-{suffix2}")

        # Tenant 1 list
        r1 = self.client_api.get("/api/v1/pms/loom/factories/")
        self.assertEqual(r1.status_code, 200)
        t1_fact_ids = [row["id"] for row in (r1.data.get("results") or r1.data)]
        self.assertIn(str(f1.id), t1_fact_ids)
        self.assertNotIn(str(f2.id), t1_fact_ids)

        # Tenant 2 list
        r2 = client2_api.get("/api/v1/pms/loom/factories/")
        self.assertEqual(r2.status_code, 200)
        t2_fact_ids = [row["id"] for row in (r2.data.get("results") or r2.data)]
        self.assertIn(str(f2.id), t2_fact_ids)
        self.assertNotIn(str(f1.id), t2_fact_ids)

        # Tenant 2 querying Tenant 1 factory in factory_day gets 404
        r_cross = client2_api.get(f"/api/v1/pms/loom/daily-production/factory_day/?factory={f1.id}")
        self.assertEqual(r_cross.status_code, 404)
