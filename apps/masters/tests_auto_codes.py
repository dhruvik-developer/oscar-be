"""Codes are never typed: a blank code is generated, and a clash gets -2, -3.

    python manage.py test apps.masters.tests_auto_codes
"""
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, User


class AutoCodeTests(TestCase):
    def setUp(self):
        self.tenant, _ = Client.objects.get_or_create(slug="auto-code-tenant", defaults={"name": "Auto Code"})
        self.user = User.objects.create_user(
            email="auto-code@example.com", password="pass", client=self.tenant, is_superuser=True, name="Admin",
        )
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(self.user)['access']}")

    def post(self, path, data):
        r = self.api.post(f"/api/v1/{path}/", data, format="json")
        self.assertEqual(r.status_code, 201, r.content[:500])
        return r.json()

    def test_textile_masters_and_variant_codes_are_generated(self):
        q = self.post("inventory/fabric-qualities", {"name": "Cotton 60s"})
        self.assertEqual(q["code"], "COTTON-60S")
        # A different name with the same slug no longer blocks the save.
        self.assertEqual(self.post("inventory/fabric-qualities", {"name": "Cotton-60s"})["code"], "COTTON-60S-2")
        d = self.post("inventory/designs", {"name": "Floral", "patternType": "Printed"})
        s = self.post("inventory/shades", {"name": "Navy"})
        v = self.post("inventory/fabric-variants", {"qualityId": q["id"], "designId": d["id"], "shadeId": s["id"]})
        self.assertEqual(v["code"], "COTTON-60S-FLORAL-NAVY")

    def test_category_location_and_uom_codes_are_generated(self):
        self.assertEqual(self.post("inventory/categories", {"name": "Grey Fabric"})["code"], "GREY-FABRIC")
        self.assertEqual(self.post("inventory/categories", {"name": "Grey  Fabric"})["code"], "GREY-FABRIC-2")
        self.assertEqual(self.post("inventory/locations", {"name": "Main Godown", "type": "Warehouse"})["code"], "MAIN-GODOWN")
        uom = self.post("inventory/units", {"label": "Bundle", "uom_type": "Piece", "is_base_unit": True})
        self.assertEqual(uom["code"], "BUNDLE")

    def test_account_and_department_codes_are_generated(self):
        a = self.post("accounts/chart-of-accounts", {"name": "Dyeing charges", "type": "Expense"})
        b = self.post("accounts/chart-of-accounts", {"name": "Freight", "type": "Expense"})
        self.assertTrue(a["code"].isdigit())
        self.assertEqual(int(b["code"]), int(a["code"]) + 1)
        child = self.post("accounts/chart-of-accounts", {"name": "Local freight", "type": "Expense", "parent": b["id"]})
        self.assertEqual(child["code"], f"{b['code']}1")
        self.assertEqual(self.post("hrms/departments", {"name": "Dyeing"})["code"], "DYEING")
