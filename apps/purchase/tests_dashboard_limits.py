"""Purchase Overview Dashboard list limits (api.md §1.3, §1.4).

The dashboard's four cards each render five rows. They ask the server for
exactly those five rows -- ``?limit=5`` with a server-side ``ordering`` /
``status`` -- instead of walking the whole collection, while the totals the
cards render come from the envelope's complete-data ``aggregates``.

Run with::

    python manage.py test apps.purchase.tests_dashboard_limits
"""
from datetime import date
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, User
from apps.masters.models import Item, Party
from apps.purchase.models import PurchaseOrder


class DashboardLimitTests(TestCase):
    """The contract the dashboard's five-row cards rely on."""

    def setUp(self):
        self.tenant = Client.objects.create(
            name="Oscar Dashboard Client", slug="oscar-dashboard"
        )
        self.user = User.objects.create_user(
            email="dashboard@example.com",
            password="secretpassword",
            client=self.tenant,
            is_superuser=True,
        )
        from apps.accounting.services import seed_chart_of_accounts

        seed_chart_of_accounts(self.tenant)
        self.api = APIClient()
        self.api.credentials(
            HTTP_AUTHORIZATION=f"Bearer {build_tokens(self.user)['access']}"
        )

        self.supplier = Party.objects.create(
            client=self.tenant,
            name="Apex Weaving Mills",
            code="V-APEX",
            type="Vendor",
        )
        self.fabric = Item.objects.create(
            client=self.tenant,
            sku="GREY-DASH",
            name="Grey Cotton Cambric",
            uom="Mtr",
            item_kind="Standalone",
            cost_price=Decimal("45.00"),
        )

        # Six orders: the newest five are what the Recent card asks for, the
        # open/dated ones are what the delivery and receipt cards ask for.
        self.specs = [
            # doc_date,     status,               expected_date, qty
            ("2026-01-05", "Approved", "2026-02-01", 100),
            ("2026-01-10", "Partially Received", "2026-01-20", 200),
            ("2026-01-15", "In Production", "2026-03-01", 300),
            ("2026-01-20", "Draft", None, 400),
            ("2026-01-25", "Fully Received", "2026-02-10", 500),
            ("2026-01-30", "Cancelled", "2026-02-15", 600),
        ]
        self.total_value = Decimal("0")
        for doc_date, status, expected, qty in self.specs:
            res = self.api.post(
                "/api/v1/purchase/orders/",
                {
                    "vendorId": str(self.supplier.id),
                    "date": doc_date,
                    "expectedDate": expected,
                    "lineItems": [
                        {"itemId": str(self.fabric.id), "qty": qty, "rate": 50, "uom": "Mtr"}
                    ],
                },
                format="json",
            )
            self.assertEqual(res.status_code, 201, res.content)
            po = PurchaseOrder.objects.get(pk=res.json()["id"])
            # Status moves through the PO actions; the dashboard fixtures set
            # them directly so every state is represented.
            po.status = status
            po.save(update_fields=["status"])
            self.total_value += Decimal(str(res.json()["total"]))

    def test_recent_card_reads_one_server_page(self):
        body = self.api.get(
            "/api/v1/purchase/orders/?limit=5&ordering=-doc_date,-created_at"
        ).json()

        self.assertEqual(body["page_size"], 5)
        self.assertEqual(len(body["results"]), 5)
        # The newest five by PO date -- not the first five in the table.
        self.assertEqual(
            [row["date"] for row in body["results"]],
            ["2026-01-30", "2026-01-25", "2026-01-20", "2026-01-15", "2026-01-10"],
        )
        self.assertTrue(body["next"])

    def test_envelope_totals_cover_every_order_not_the_page(self):
        body = self.api.get("/api/v1/purchase/orders/?limit=5").json()

        self.assertEqual(body["count"], 6)
        self.assertEqual(len(body["results"]), 5)
        # The card's "Showing latest 5 of N orders" and the KPI tiles need the
        # complete figures, which the aggregates carry regardless of the page.
        self.assertEqual(body["aggregates"]["count"], 6)
        self.assertEqual(
            Decimal(str(body["aggregates"]["totalValue"])), self.total_value
        )

    def test_delivery_card_orders_by_expected_date_and_drops_closed_orders(self):
        body = self.api.get(
            "/api/v1/purchase/orders/",
            {
                "limit": 5,
                "ordering": "expected_date",
                # Open statuses, repeated: the card never wants a closed order.
                "status": [
                    "Draft",
                    "Pending Approval",
                    "Approved",
                    "Issued",
                    "In Production",
                    "Partially Received",
                ],
            },
        ).json()

        self.assertEqual(len(body["results"]), 4)
        # Ascending expected date; a dated order always beats an undated one.
        self.assertEqual(
            [row["expectedDate"] for row in body["results"]],
            ["2026-01-20", "2026-02-01", "2026-03-01", None],
        )
        numbers = {row["poNumber"] for row in body["results"]}
        closed = PurchaseOrder.objects.filter(
            status__in=["Fully Received", "Cancelled"]
        ).values_list("po_number", flat=True)
        self.assertFalse(numbers & set(closed))

    def test_pending_receipt_card_keeps_only_open_receivable_orders(self):
        body = self.api.get(
            "/api/v1/purchase/orders/",
            {
                "limit": 5,
                "ordering": "expected_date",
                "status": ["Approved", "Issued", "In Production", "Partially Received"],
            },
        ).json()

        self.assertEqual(len(body["results"]), 3)
        self.assertEqual(
            {row["status"] for row in body["results"]},
            {"Approved", "In Production", "Partially Received"},
        )

    def test_limit_is_capped_and_never_silently_drops_rows(self):
        # A card asks for five; the envelope always says how many exist.
        body = self.api.get("/api/v1/purchase/orders/?limit=5&page=2").json()

        self.assertEqual(body["page"], 2)
        self.assertEqual(len(body["results"]), 1)
        self.assertIsNone(body["next"])
        self.assertEqual(body["count"], 6)
