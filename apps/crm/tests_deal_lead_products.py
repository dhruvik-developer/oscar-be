"""The products captured on a lead show on its deal.

    python manage.py test apps.crm.tests_deal_lead_products
"""
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, User
from apps.crm.models import Deal, Lead, LeadProduct, Stage


class DealLeadProductsTests(TestCase):
    def setUp(self):
        self.tenant, _ = Client.objects.get_or_create(slug="deal-lead-products", defaults={"name": "Deal Lead Products"})
        self.user = User.objects.create_user(email="dlp@example.com", password="pass", client=self.tenant,
                                             is_superuser=True, name="Admin")
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(self.user)['access']}")
        stage = Stage.objects.create(client=self.tenant, name="New Lead", sequence=1, is_active=True)
        self.lead = Lead.objects.create(client=self.tenant, lead_number="L-DLP-1", name="radha", company="Radha",
                                        phone="9999999999", email="radha@example.com", city="Surat",
                                        state="Gujarat", country="India", stage=stage)
        self.deal = Deal.objects.create(client=self.tenant, deal_number="DEAL-DLP-1", title="radha", lead=self.lead)

    def lines(self):
        body = self.api.get(f"/api/v1/crm/deals/{self.deal.id}/lines/").json()
        return body["results"] if isinstance(body, dict) else body

    def test_lead_products_are_copied_once_and_a_removed_line_stays_removed(self):
        LeadProduct.objects.create(client=self.tenant, lead=self.lead, product_name="cotton 60s floral navy",
                                   fabric_design="Floral", fabric_color="Navy", fabric_width=Decimal("56"),
                                   qty=Decimal("500"), uom="Meter", expected_rate=Decimal("82"))
        rows = self.lines()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "cotton 60s floral navy")
        self.assertIn('56"', rows[0]["description"])
        self.assertEqual(self.lines(), rows)  # opening again does not copy twice

        # A product added to the lead later shows up too.
        LeadProduct.objects.create(client=self.tenant, lead=self.lead, product_name="rayon plain", qty=Decimal("100"))
        rows = self.lines()
        self.assertEqual([r["name"] for r in rows], ["cotton 60s floral navy", "rayon plain"])

        # Removed from the deal -> not brought back.
        r = self.api.delete(f"/api/v1/crm/deals/{self.deal.id}/lines/{rows[1]['id']}/")
        self.assertIn(r.status_code, (200, 204), r.content[:300])
        self.assertEqual([r["name"] for r in self.lines()], ["cotton 60s floral navy"])
