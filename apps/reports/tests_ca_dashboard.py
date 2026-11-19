from decimal import Decimal
from django.test import TestCase
from django.utils import timezone
from apps.accounts.models import Client, User
from apps.accounts.authentication import build_tokens
from apps.reports.dashboard import business_dashboard
from rest_framework.test import APIClient


class CADashboardTests(TestCase):
    def setUp(self):
        self.tenant, _ = Client.objects.get_or_create(
            slug="ca-dashboard-tenant", defaults={"name": "CA Dashboard Tenant"}
        )
        self.user = User.objects.create_user(
            email="owner@test.com",
            password="pass",
            client=self.tenant,
            name="Owner User",
            is_superuser=True,
        )
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(self.user)['access']}")

    def test_dashboard_returns_all_ca_ready_sections(self):
        data = business_dashboard(self.tenant.id, period="mtd")

        # 1. Period Info
        self.assertIn("periodInfo", data)
        self.assertEqual(data["periodInfo"]["period"], "mtd")

        # 2. Top 6 KPI cards
        self.assertIn("topKpiCards", data)
        self.assertEqual(len(data["topKpiCards"]), 6)
        card_ids = [c["id"] for c in data["topKpiCards"]]
        self.assertEqual(card_ids, ["sales", "collection", "grossMargin", "netProfit", "cashBank", "workingCapital"])

        # 3. Sales & Collection Charts (7D, 30D, MTD)
        self.assertIn("salesCollection", data)
        self.assertIn("7D", data["salesCollection"]["charts"])
        self.assertIn("30D", data["salesCollection"]["charts"])
        self.assertIn("MTD", data["salesCollection"]["charts"])

        # 4. Purchase & Expenses
        self.assertIn("purchaseAndExpenses", data)
        self.assertIn("proportions", data["purchaseAndExpenses"])

        # 5. Batch Profitability
        self.assertIn("batchProfitability", data)
        self.assertIn("realizedProfit", data["batchProfitability"])
        self.assertIn("unsoldBatchesCount", data["batchProfitability"])

        # 6. Cash Flow Next 30 Days
        self.assertIn("cashFlow30Days", data)
        self.assertEqual(len(data["cashFlow30Days"]["weeks"]), 4)

        # 7. Compliance Calendar
        self.assertIn("complianceCalendar", data)
        self.assertGreaterEqual(len(data["complianceCalendar"]["items"]), 4)

        # 8. GST Position
        self.assertIn("gstPosition", data)
        self.assertIn("netPayable", data["gstPosition"])

        # 9. Payable Ageing
        self.assertIn("payableAgeing", data)
        self.assertIn("creditorDays", data["payableAgeing"])

        # 10. MSME Alert
        self.assertIn("msmeAlert", data)
        self.assertIn("totalOverdue", data["msmeAlert"])

        # 11. P&L Summary
        self.assertIn("pnlSummary", data)
        self.assertEqual(data["pnlSummary"]["columns"], ["MTD", "FYTD", "Previous Year (YTD)"])

        # 12. Working Capital Ratios
        self.assertIn("workingCapitalRatios", data)
        self.assertIn("cashConversionCycle", data["workingCapitalRatios"])

        # 13. Production & Job Work
        self.assertIn("productionAndJobWork", data)

        # 14. QC & Wastage
        self.assertIn("qcAndWastage", data)
        self.assertIn("goodYieldPct", data["qcAndWastage"])

        # 15. Inventory Position (Value) & Finished Stock Ageing
        self.assertIn("inventoryPositionValue", data)
        self.assertIn("finishedStockAgeing", data["inventoryPositionValue"])

        # 16. Reconciliation & Control Checks
        self.assertIn("reconciliationChecks", data)
        self.assertEqual(len(data["reconciliationChecks"]["items"]), 9)

        # 17. Attention Required Action Bar
        self.assertIn("attentionRequired", data)
        self.assertEqual(len(data["attentionRequired"]), 8)

    def test_dashboard_supports_all_periods(self):
        for p in ["today", "mtd", "fytd"]:
            data = business_dashboard(self.tenant.id, period=p)
            self.assertEqual(data["periodInfo"]["period"], p)
            self.assertEqual(len(data["topKpiCards"]), 6)

    def test_dashboard_api_endpoint(self):
        response = self.api.get("/api/v1/reports/sales-dashboard/?period=mtd&chart_range=7d")
        self.assertEqual(response.status_code, 200, response.content[:600])
        body = response.json()
        self.assertIn("topKpiCards", body)
        self.assertIn("salesCollection", body)
        self.assertIn("purchaseAndExpenses", body)
        self.assertIn("reconciliationChecks", body)
        self.assertIn("attentionRequired", body)
