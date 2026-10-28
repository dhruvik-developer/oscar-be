"""Customer payment terms, receipts and early-payment discounts.

* payment terms: presets, custom days, due-date override with reason + audit
* receipts: full / partial, one receipt over several invoices, several
  receipts on one invoice, unallocated remainder, over-allocation and
  duplicate transaction references refused
* discount requests: nothing posts until approval; approve / reject; the
  approver can't be the requester; unauthorised users can't decide
* settlement: money received and discount kept apart; invoice balance,
  status, journal entries and the customer ledger after approval and reversal
* the receivables dashboard KPIs
"""
import uuid
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounting import services as ledger
from apps.accounting.models import BankAccount, JournalEntry, JournalLine, StandaloneNote
from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, Role, RolePermission, User
from apps.accounts.permission_catalogue import sync_permissions
from apps.core.models import AuditLog
from apps.inventory import services as stock
from apps.masters.models import Item, Location, Party

from . import services
from .models import EarlyPaymentDiscountRequest, PaymentIn, SalesInvoice

D = Decimal


class EarlyPaymentBase(TestCase):
    def setUp(self):
        sync_permissions()
        self.tenant = Client.objects.create(slug=f"epd-{uuid.uuid4().hex[:8]}", name="EPD Co")
        ledger.seed_chart_of_accounts(self.tenant)
        self.admin = self.user("admin", superuser=True)
        self.requester = self.user("requester", perms=[
            "view_sales", "record_payment_in", "request_payment_discount", "approve_payment_discount",
        ])
        self.approver = self.user("approver", perms=["view_sales", "approve_payment_discount"])
        self.clerk = self.user("clerk", perms=["view_sales", "record_payment_in", "request_payment_discount"])
        self.viewer = self.user("viewer", perms=["view_sales"])

        self.location = Location.objects.create(client=self.tenant, code="WH", name="Main", type="Warehouse")
        self.item = Item.objects.create(
            client=self.tenant, sku="FAB", name="Fabric", item_kind="Standalone", uom="Mtr", cost_price=D("10"),
        )
        stock.post_movement(
            client_id=self.tenant.id, item=self.item, location=self.location,
            type="ADJUSTMENT", quantity=1000, unit_cost=D("10"), notes="opening",
        )
        self.customer = Party.objects.create(client=self.tenant, code="C1", name="Shree Fabrics", type="Customer")
        self.other_customer = Party.objects.create(client=self.tenant, code="C2", name="Acme Mills", type="Customer")
        self.bank = BankAccount.objects.create(
            client=self.tenant, account=ledger.system_account(self.tenant.id, "bank"), name="HDFC Current",
        )
        self.today = timezone.localdate()

    # -- helpers -------------------------------------------------------------
    def user(self, name, perms=(), superuser=False):
        role = None
        if perms:
            role = Role.objects.create(client=self.tenant, code=f"R-{name}", name=name)
            for perm in perms:
                RolePermission.objects.create(role=role, permission_id=perm)
        return User.objects.create_user(
            email=f"{name}-{uuid.uuid4().hex[:6]}@example.com", password="Pass1234!",
            client=self.tenant, name=name.title(), role=role, is_superuser=superuser,
        )

    def api(self, user):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(user)['access']}")
        return client

    def post(self, path, data=None, user=None):
        return self.api(user or self.admin).post(f"/api/v1{path}", data or {}, format="json")

    def get(self, path, params=None, user=None):
        return self.api(user or self.admin).get(f"/api/v1{path}", params or {})

    def invoice(self, amount=100000, customer=None, doc_date=None, credit_days=30):
        """A finalized invoice for ``amount`` (no GST, so total == amount)."""
        doc_date = doc_date or self.today
        created = self.post("/sales/invoices/", {
            "partyId": str((customer or self.customer).id), "date": str(doc_date),
            "locationId": str(self.location.id), "creditDays": credit_days,
            "dueDate": str(doc_date + timedelta(days=credit_days)),
            "lineItems": [{"itemId": str(self.item.id), "qty": 10, "rate": amount / 10, "tax": 0}],
        })
        self.assertEqual(created.status_code, 201, created.content[:600])
        done = self.post(f"/sales/invoices/{created.json()['id']}/finalize/")
        self.assertEqual(done.status_code, 200, done.content[:600])
        inv = SalesInvoice.objects.get(pk=created.json()["id"])
        self.assertEqual(inv.total, D(amount))
        return inv

    def pay(self, amount, allocations, customer=None, user=None, mode="Bank", on=None, txn=None, expect=201):
        body = {
            "customerId": str((customer or self.customer).id), "date": str(on or self.today),
            "amount": amount, "mode": mode,
            "allocations": [{"invoiceId": str(inv.id), "amount": amt} for inv, amt in allocations],
        }
        if mode != "Cash":
            body["bankAccountId"] = str(self.bank.id)
        if txn:
            body["transactionReference"] = txn
        response = self.post("/sales/payments/", body, user=user)
        self.assertEqual(response.status_code, expect, response.content[:600])
        return PaymentIn.objects.get(pk=response.json()["id"]) if expect == 201 else response

    def request_discount(self, inv, payment, amount, user=None, submit=True, expect=201):
        response = self.post("/sales/discount-requests/", {
            "invoiceId": str(inv.id), "paymentId": str(payment.id), "requestedAmount": amount,
            "reason": "Paid 20 days early", "customerRequestDate": str(self.today), "submit": submit,
        }, user=user or self.requester)
        self.assertEqual(response.status_code, expect, response.content[:600])
        return response.json()

    def approve(self, req_id, amount=None, user=None, expect=200):
        body = {"reason": "Long-standing customer, agreed by phone"}
        if amount is not None:
            body["approvedAmount"] = amount
        response = self.post(f"/sales/discount-requests/{req_id}/approve/", body, user=user or self.approver)
        self.assertEqual(response.status_code, expect, response.content[:600])
        return response.json()

    def refreshed(self, inv):
        inv.refresh_from_db()
        return inv

    def account_balance(self, system_key, party=None):
        # As the ledgers do: a reversed entry and its mirror both drop out.
        lines = JournalLine.objects.filter(
            client_id=self.tenant.id, account__system_key=system_key
        ).exclude(journal_entry__status="Reversed").filter(journal_entry__reversal_of__isnull=True)
        if party is not None:
            lines = lines.filter(party=party)
        return sum((line.debit - line.credit for line in lines), D("0"))

    def note_entries(self):
        return JournalEntry.objects.filter(client_id=self.tenant.id, source_document_type="StandaloneNote")


class PaymentTermsTests(EarlyPaymentBase):
    def test_options_offer_presets_and_custom_without_imposing_one(self):
        options = self.get("/sales/invoices/payment-term-options/").json()["options"]
        self.assertEqual([o["label"] for o in options],
                         ["Immediate Payment", "7 Days", "15 Days", "30 Days", "45 Days", "60 Days", "Custom"])

    def test_presets_and_custom_days_set_the_due_date(self):
        inv = self.invoice()
        for label, days in (("Immediate Payment", 0), ("15 Days", 15), ("60 Days", 60)):
            response = self.post(f"/sales/invoices/{inv.id}/payment-terms/",
                                 {"paymentTerms": label, "creditDays": days, "reason": "Agreed with buyer"})
            self.assertEqual(response.status_code, 200, response.content[:400])
            inv = self.refreshed(inv)
            self.assertEqual(inv.payment_terms, label)
            self.assertEqual(inv.due_date, inv.doc_date + timedelta(days=days))
        self.post(f"/sales/invoices/{inv.id}/payment-terms/",
                  {"paymentTerms": "Custom", "creditDays": 21, "reason": "Special terms"})
        inv = self.refreshed(inv)
        self.assertEqual(inv.payment_terms, "Custom (21 days)")
        self.assertEqual(inv.due_date, inv.doc_date + timedelta(days=21))

    def test_finalized_change_needs_reason_and_permission_and_is_audited(self):
        inv = self.invoice(credit_days=30)
        old_due = inv.due_date
        no_reason = self.post(f"/sales/invoices/{inv.id}/payment-terms/", {"creditDays": 45})
        self.assertIn(no_reason.status_code, (400, 422))
        denied = self.post(f"/sales/invoices/{inv.id}/payment-terms/",
                           {"creditDays": 45, "reason": "x"}, user=self.clerk)
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(self.refreshed(inv).due_date, old_due)

        override = inv.doc_date + timedelta(days=40)
        ok = self.post(f"/sales/invoices/{inv.id}/payment-terms/", {
            "paymentTerms": "30 Days", "creditDays": 30, "dueDate": str(override),
            "reason": "Customer's year-end close",
        })
        self.assertEqual(ok.status_code, 200, ok.content[:400])
        self.assertEqual(self.refreshed(inv).due_date, override)
        history = self.get(f"/sales/invoices/{inv.id}/terms-history/").json()["results"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["before"]["dueDate"], str(old_due))
        self.assertEqual(history[0]["after"]["dueDate"], str(override))
        self.assertTrue(history[0]["after"]["dueDateOverridden"])
        self.assertEqual(history[0]["reason"], "Customer's year-end close")
        self.assertEqual(history[0]["actorName"], "Admin")

    def test_due_date_before_invoice_date_is_refused(self):
        inv = self.invoice()
        response = self.post(f"/sales/invoices/{inv.id}/payment-terms/", {
            "creditDays": 0, "dueDate": str(inv.doc_date - timedelta(days=1)), "reason": "typo",
        })
        self.assertIn(response.status_code, (400, 422))


class ReceiptTests(EarlyPaymentBase):
    def test_full_and_partial_payments_drive_status(self):
        inv = self.invoice(100000)
        self.pay(40000, [(inv, 40000)])
        self.assertEqual(self.refreshed(inv).status, "Partially Paid")
        self.pay(60000, [(inv, 60000)])
        inv = self.refreshed(inv)
        self.assertEqual((inv.status, inv.amount_paid, inv.balance_due), ("Paid", D("100000"), D("0")))

    def test_one_receipt_over_several_invoices_with_unallocated_remainder(self):
        a, b = self.invoice(30000), self.invoice(50000)
        payment = self.pay(100000, [(a, 30000), (b, 20000)], txn="UTR-1")
        self.assertEqual(payment.allocated_amount, D("50000"))
        self.assertEqual(payment.unallocated_amount, D("50000"))   # held as a customer advance
        self.assertEqual(payment.transaction_reference, "UTR-1")
        self.assertEqual(self.refreshed(a).status, "Paid")
        self.assertEqual(self.refreshed(b).balance_due, D("30000"))
        # Bank holds the full receipt; debtors fall by the full receipt.
        self.assertEqual(self.account_balance("bank"), D("100000"))

    def test_over_allocation_and_duplicate_transaction_are_refused(self):
        inv = self.invoice(10000)
        over = self.pay(12000, [(inv, 12000)], expect=422)
        self.assertEqual(over.json().get("code"), "PAYMENT_EXCEEDS_BALANCE")
        self.pay(5000, [(inv, 5000)], txn="UTR-77")
        dup = self.pay(5000, [(inv, 5000)], txn="utr-77", expect=409)
        self.assertEqual(dup.json()["code"], "DUPLICATE_RECEIPT")
        self.assertEqual(PaymentIn.objects.filter(client=self.tenant).count(), 1)
        self.assertEqual(self.refreshed(inv).amount_paid, D("5000"))


class DiscountWorkflowTests(EarlyPaymentBase):
    def early_paid_invoice(self, amount=100000, paid=98000):
        inv = self.invoice(amount, doc_date=self.today - timedelta(days=5), credit_days=30)
        payment = self.pay(paid, [(inv, paid)], on=self.today)
        return inv, payment

    def test_illustrative_example_settles_without_counting_discount_as_money(self):
        inv, payment = self.early_paid_invoice()
        req = self.request_discount(inv, payment, 2000)
        self.assertEqual(req["status"], "Submitted")
        self.assertEqual(D(str(req["eligibleAmount"])), D("98000"))
        self.assertEqual(req["daysPaidEarly"], 25)
        # Submitting posts nothing.
        self.assertFalse(self.note_entries().exists())
        self.assertEqual(self.refreshed(inv).balance_due, D("2000"))

        approved = self.approve(req["id"])
        self.assertEqual(approved["status"], "Approved")
        self.assertEqual(D(str(approved["approvedAmount"])), D("2000"))
        inv = self.refreshed(inv)
        self.assertEqual(inv.total, D("100000"))              # original amount untouched
        self.assertEqual(inv.amount_paid, D("98000"))          # money received only
        self.assertEqual(inv.settlement_discount, D("2000"))
        self.assertEqual((inv.balance_due, inv.status), (D("0"), "Paid"))

        note = StandaloneNote.objects.get(pk=approved["creditNoteId"])
        self.assertEqual((note.status, note.total_amount, note.against_document_id), ("Posted", D("2000"), inv.id))
        self.assertEqual(self.note_entries().count(), 1)
        self.assertEqual(self.account_balance("sales_return"), D("2000"))
        self.assertEqual(self.account_balance("bank"), D("98000"))   # cash is what arrived
        self.assertEqual(self.account_balance("debtors", party=self.customer), D("0"))
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.balance, D("0"))
        self.assertEqual(payment.__class__.objects.get(pk=payment.pk).amount, D("98000"))

    def test_partial_payment_with_discount(self):
        inv = self.invoice(100000)
        payment = self.pay(50000, [(inv, 50000)])
        req = self.request_discount(inv, payment, 1500)
        self.approve(req["id"], amount=1000)                   # approver revises the amount down
        inv = self.refreshed(inv)
        self.assertEqual((inv.status, inv.balance_due, inv.settlement_discount), ("Partially Paid", D("49000"), D("1000")))
        over = self.pay(49500, [(inv, 49500)], expect=422)   # discount already settled part of it
        self.assertEqual(over.json()["code"], "PAYMENT_EXCEEDS_BALANCE")
        self.pay(49000, [(inv, 49000)])
        inv = self.refreshed(inv)
        self.assertEqual((inv.status, inv.amount_paid, inv.balance_due), ("Paid", D("99000"), D("0")))

    def test_discount_on_fully_paid_invoice_leaves_customer_credit(self):
        inv, payment = self.early_paid_invoice(paid=100000)
        req = self.request_discount(inv, payment, 2000)
        self.approve(req["id"])
        inv = self.refreshed(inv)
        self.assertEqual((inv.status, inv.balance_due), ("Paid", D("0")))
        row = self.get("/sales/receivables/").json()["results"][0]
        self.assertEqual(D(str(row["customerCredit"])), D("2000"))
        self.assertEqual(self.account_balance("debtors", party=self.customer), D("-2000"))

    def test_rejection_posts_nothing_and_needs_reason(self):
        inv, payment = self.early_paid_invoice()
        req = self.request_discount(inv, payment, 2000)
        no_reason = self.post(f"/sales/discount-requests/{req['id']}/reject/", {}, user=self.approver)
        self.assertIn(no_reason.status_code, (400, 422))
        done = self.post(f"/sales/discount-requests/{req['id']}/reject/",
                         {"reason": "Outside agreed terms"}, user=self.approver)
        self.assertEqual(done.status_code, 200, done.content[:400])
        self.assertEqual((done.json()["status"], done.json()["decidedByName"]), ("Rejected", "Approver"))
        self.assertFalse(self.note_entries().exists())
        self.assertEqual(self.refreshed(inv).balance_due, D("2000"))
        # A rejected request no longer blocks a fresh one for the same receipt.
        self.request_discount(inv, payment, 1000)

    def test_requester_cannot_approve_own_request_and_others_need_permission(self):
        inv, payment = self.early_paid_invoice()
        req = self.request_discount(inv, payment, 2000, user=self.requester)
        # The requester holds the approve permission, but not for their own request.
        self.assertEqual(self.post(f"/sales/discount-requests/{req['id']}/approve/",
                                   {"reason": "self"}, user=self.requester).status_code, 403)
        self.assertEqual(self.post(f"/sales/discount-requests/{req['id']}/approve/",
                                   {"reason": "x"}, user=self.clerk).status_code, 403)
        self.assertEqual(self.post(f"/sales/discount-requests/{req['id']}/approve/",
                                   {"reason": "x"}, user=self.viewer).status_code, 403)
        self.assertFalse(self.note_entries().exists())
        self.approve(req["id"], user=self.approver)

    def test_duplicates_are_refused(self):
        inv, payment = self.early_paid_invoice()
        req = self.request_discount(inv, payment, 2000)
        dup = self.request_discount(inv, payment, 500, expect=409)
        self.assertEqual(dup["code"], "DUPLICATE_DISCOUNT_REQUEST")
        self.approve(req["id"])
        again = self.post(f"/sales/discount-requests/{req['id']}/approve/", {"reason": "again"}, user=self.approver)
        self.assertEqual(again.status_code, 409)
        self.assertEqual(self.note_entries().count(), 1)
        self.assertEqual(self.refreshed(inv).settlement_discount, D("2000"))

    def test_discount_cannot_exceed_invoice(self):
        inv, payment = self.early_paid_invoice(amount=10000, paid=9000)
        self.request_discount(inv, payment, 20000, expect=400)
        req = self.request_discount(inv, payment, 500)
        over = self.approve(req["id"], amount=15000, expect=422)
        self.assertEqual(over["code"], "DISCOUNT_EXCEEDS_INVOICE")

    def test_request_needs_receipt_allocated_to_that_invoice(self):
        a, b = self.invoice(10000), self.invoice(10000)
        payment = self.pay(10000, [(a, 10000)])
        self.request_discount(b, payment, 100, expect=400)

    def test_draft_review_revise_and_history(self):
        inv, payment = self.early_paid_invoice()
        req = self.request_discount(inv, payment, 2500, submit=False)
        self.assertEqual(req["status"], "Draft")
        patched = self.api(self.requester).patch(f"/api/v1/sales/discount-requests/{req['id']}/",
                                                 {"requestedAmount": 2200}, format="json")
        self.assertEqual(D(str(patched.json()["requestedAmount"])), D("2200"))
        self.assertEqual(self.post(f"/sales/discount-requests/{req['id']}/submit/", user=self.requester).status_code, 200)
        review = self.post(f"/sales/discount-requests/{req['id']}/review/", {}, user=self.approver).json()
        self.assertEqual(review["status"], "Under Review")
        self.post(f"/sales/discount-requests/{req['id']}/propose/",
                  {"proposedAmount": 1800, "reason": "Within policy"}, user=self.approver)
        approved = self.approve(req["id"])                    # takes the proposed amount
        self.assertEqual(D(str(approved["approvedAmount"])), D("1800"))
        actions = [row["action"] for row in self.get(f"/sales/discount-requests/{req['id']}/history/").json()["results"]]
        self.assertEqual(actions, [
            "discount_request_create", "discount_request_update", "discount_request_submit",
            "discount_request_review", "discount_request_propose", "discount_request_approve",
        ])

    def test_reversing_an_approved_discount_restores_the_balance(self):
        inv, payment = self.early_paid_invoice()
        req = self.request_discount(inv, payment, 2000)
        self.approve(req["id"])
        blocked = self.post(f"/sales/payments/{payment.id}/cancel/", {"reason": "bounced"})
        self.assertEqual(blocked.status_code, 422)
        cancelled = self.post(f"/sales/discount-requests/{req['id']}/cancel/",
                              {"reason": "Cheque bounced"}, user=self.approver)
        self.assertEqual(cancelled.status_code, 200, cancelled.content[:400])
        inv = self.refreshed(inv)
        self.assertEqual((inv.settlement_discount, inv.balance_due, inv.status), (D("0"), D("2000"), "Partially Paid"))
        self.assertEqual(StandaloneNote.objects.get(pk=cancelled.json()["creditNoteId"]).status, "Cancelled")
        self.assertEqual(self.account_balance("sales_return"), D("0"))
        self.assertEqual(self.account_balance("debtors", party=self.customer), D("2000"))
        self.assertEqual(self.post(f"/sales/payments/{payment.id}/cancel/", {"reason": "bounced"}).status_code, 200)

    def test_cancelling_a_receipt_cancels_its_open_requests(self):
        inv, payment = self.early_paid_invoice()
        req = self.request_discount(inv, payment, 2000)
        self.assertEqual(self.post(f"/sales/payments/{payment.id}/cancel/", {"reason": "bounced"}).status_code, 200)
        self.assertEqual(EarlyPaymentDiscountRequest.objects.get(pk=req["id"]).status, "Cancelled")


class DashboardAndHistoryTests(EarlyPaymentBase):
    def test_kpis_and_rows(self):
        overdue = self.invoice(40000, doc_date=self.today - timedelta(days=40), credit_days=30)
        due = self.invoice(60000, credit_days=30)
        paid = self.invoice(100000, doc_date=self.today - timedelta(days=2), credit_days=30)
        payment = self.pay(98000, [(paid, 98000)])
        self.pay(10000, [(due, 10000)])
        req = self.request_discount(paid, payment, 2000)
        pending = self.request_discount(due, PaymentIn.objects.get(allocated_amount=D("10000")), 300)

        body = self.get("/sales/receivables/").json()
        kpi = body["aggregates"]
        self.assertEqual(D(str(kpi["totalOutstanding"])), D("40000") + D("50000") + D("2000"))
        self.assertEqual(D(str(kpi["overdueAmount"])), D("40000"))
        self.assertEqual(kpi["overdueCount"], 1)
        self.assertEqual(D(str(kpi["receivedAmount"])), D("108000"))
        self.assertEqual(kpi["pendingDiscountCount"], 2)

        self.approve(req["id"])
        kpi = self.get("/sales/receivables/").json()["aggregates"]
        self.assertEqual(D(str(kpi["totalOutstanding"])), D("90000"))
        self.assertEqual(D(str(kpi["approvedDiscountAmount"])), D("2000"))
        self.assertEqual(kpi["pendingDiscountCount"], 1)

        rows = {r["invoiceNumber"]: r for r in self.get("/sales/receivables/").json()["results"]}
        self.assertEqual(rows[overdue.invoice_number]["paymentStatus"], "Overdue")
        settled = rows[self.refreshed(paid).invoice_number]
        self.assertEqual((settled["paymentStatus"], D(str(settled["paidAmount"])), D(str(settled["approvedDiscount"]))),
                         ("Paid", D("98000"), D("2000")))
        only_overdue = self.get("/sales/receivables/", {"status": "Overdue"}).json()["results"]
        self.assertEqual([r["invoiceNumber"] for r in only_overdue], [overdue.invoice_number])
        by_status = self.get("/sales/discount-requests/", {"status": "Submitted"}).json()
        self.assertEqual([r["id"] for r in by_status["results"]], [pending["id"]])
        self.assertEqual(self.get("/sales/discount-requests/", {"invoiceId": str(paid.id)}).json()["count"], 1)

    def test_existing_invoices_and_receipts_are_unchanged(self):
        """Rows written before this feature (no discounts) read and refresh exactly as before."""
        inv = self.invoice(50000)
        self.pay(20000, [(inv, 20000)])
        before = SalesInvoice.objects.filter(pk=inv.pk).values("total", "amount_paid", "status").get()
        services.refresh_invoice_payment_status(self.refreshed(inv))
        after = SalesInvoice.objects.filter(pk=inv.pk).values("total", "amount_paid", "status").get()
        self.assertEqual(before, after)
        inv = self.refreshed(inv)
        self.assertEqual((inv.settlement_discount, inv.balance_due), (D("0"), D("30000")))
        outstanding = self.get(f"/sales/invoices/{inv.id}/outstanding/").json()
        self.assertEqual(D(str(outstanding["outstanding"])), D("30000"))
        self.assertEqual(D(str(outstanding["approvedDiscount"])), D("0"))
        self.assertFalse(AuditLog.objects.filter(client_id=self.tenant.id, action__startswith="discount_").exists())
