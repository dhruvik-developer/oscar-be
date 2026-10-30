"""Products picked on the lead form show as the lead's fabric requirements.

    python manage.py test apps.crm.tests_form_products
"""
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, User
from apps.crm.models import LeadProduct
from apps.masters.models import Item


class FormProductsTests(TestCase):
    def setUp(self):
        self.tenant, _ = Client.objects.get_or_create(slug="form-products", defaults={"name": "Form Products"})
        self.admin = User.objects.create_user(email="fp-admin@example.com", password="pass", client=self.tenant,
                                              is_superuser=True, name="Asha")
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(self.admin)['access']}")
        self.navy = Item.objects.create(client=self.tenant, sku="COT-NAVY", name="cotton 60s cotton navy", uom="Meter",
                                        item_kind="Fabric", fabric_quality="Cotton", fabric_gsm=Decimal("120"))

    def requirements(self, lead_id):
        return list(LeadProduct.objects.filter(lead_id=lead_id, deleted_at__isnull=True).order_by("created_at"))

    def test_create_and_edit_add_requirements_once(self):
        r = self.api.post("/api/v1/crm/leads/", {
            "name": "radha", "phone": "9999999999",
            "customValues": {"products": ["cotton 60s cotton navy", "Hand-typed linen"]},
        }, format="json")
        self.assertEqual(r.status_code, 201, r.content[:400])
        lead_id = r.json()["id"]
        rows = self.requirements(lead_id)
        self.assertEqual([p.product_name for p in rows], ["cotton 60s cotton navy", "Hand-typed linen"])
        self.assertEqual(rows[0].item_id, self.navy.id)
        self.assertEqual((rows[0].fabric_code, rows[0].fabric_type, rows[0].uom), ("COT-NAVY", "Cotton", "Meter"))
        self.assertIsNone(rows[1].item_id)

        # Editing the form again adds only the new product, never a duplicate.
        r = self.api.patch(f"/api/v1/crm/leads/{lead_id}/", {
            "customValues": {"products": ["Cotton 60s Cotton Navy", "Hand-typed linen", "Silk"]},
        }, format="json")
        self.assertEqual(r.status_code, 200, r.content[:400])
        self.assertEqual([p.product_name for p in self.requirements(lead_id)],
                         ["cotton 60s cotton navy", "Hand-typed linen", "Silk"])
