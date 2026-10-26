"""A quotation addressed by hand to anyone: no customer or lead row needed;
the customer is made from it when it is approved."""
import uuid

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, User
from apps.masters.models import Party
from apps.sales.models import Quotation


class ManualQuotationTests(TestCase):
    def setUp(self):
        self.tenant = Client.objects.create(slug=f"mq-{uuid.uuid4().hex[:8]}", name="Manual Q")
        admin = User.objects.create_user(
            email=f"a-{uuid.uuid4().hex[:6]}@example.com", password="Pass1234!",
            client=self.tenant, name="Admin", is_superuser=True,
        )
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(admin)['access']}")

    def create(self, **header):
        body = {
            "date": str(timezone.localdate()),
            "lineItems": [{"description": "Cotton 60s floral navy", "qty": 100, "rate": 100, "tax": 5}],
            **header,
        }
        return self.api.post("/api/v1/sales/quotations/", body, format="json")

    def manual(self):
        return self.create(
            partyName="Shree Ganesh Textiles",
            partyGstin="24ABCDE1234F1Z5",
            placeOfSupply="Gujarat",
            billingAddress={
                "line1": "Ring Road", "city": "Surat", "state": "Gujarat",
                "contactPerson": "Mahesh", "phone": "9876543210", "email": "mahesh@sgt.example",
            },
        )

    def test_quotation_with_a_typed_name_saves(self):
        resp = self.manual()
        self.assertEqual(resp.status_code, 201, resp.content[:500])
        q = Quotation.objects.get(pk=resp.data["id"])
        self.assertIsNone(q.party_id)
        self.assertIsNone(q.crm_lead_id)
        self.assertEqual(q.party_name, "Shree Ganesh Textiles")
        self.assertEqual(q.billing_address["phone"], "9876543210")
        self.assertEqual(str(q.total), "10500.00")

    def test_no_customer_lead_or_name_is_refused(self):
        resp = self.create()
        self.assertEqual(resp.status_code, 400, resp.content[:300])
        self.assertIn("partyName", resp.data["field_errors"])

    def test_approval_makes_the_customer_once(self):
        qid = self.manual().data["id"]
        resp = self.api.post(f"/api/v1/sales/quotations/{qid}/approve/", {}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content[:500])
        q = Quotation.objects.get(pk=qid)
        party = Party.objects.get(pk=q.party_id)
        self.assertEqual(party.name, "Shree Ganesh Textiles")
        self.assertEqual((party.phone, party.email, party.gstin), ("9876543210", "mahesh@sgt.example", "24ABCDE1234F1Z5"))
        self.assertEqual(party.gst_treatment, "Registered Business")
        self.assertNotIn("phone", party.billing_address)
        self.assertEqual(party.billing_address["city"], "Surat")

        # A second manual quotation to the same name reuses that customer.
        qid2 = self.manual().data["id"]
        self.api.post(f"/api/v1/sales/quotations/{qid2}/approve/", {}, format="json")
        self.assertEqual(Party.objects.filter(client=self.tenant, name="Shree Ganesh Textiles").count(), 1)
