"""Discounts granted inside the Record Customer Payment (receipt) workflow.

The whole point of this module is the accounting contract:

* the bank/cash account is debited ONLY with the cash actually received;
* the discount is a separate credit note (Dr Sales Returns | Cr Debtors), so
  it reduces the customer's receivable without ever touching the bank;
* invoice ``amount_paid`` and ``settlement_discount`` are kept apart;
* both postings are atomic -- any failure rolls the receipt back completely.
"""
from decimal import Decimal

from .models import EarlyPaymentDiscountRequest, PaymentIn, SalesInvoice
from .services import invoice_balance
from .tests_early_payment import EarlyPaymentBase

D = Decimal


class IntegratedDiscountTests(EarlyPaymentBase):
    def pay_with_discounts(self, cash, allocations, discounts, expect=201, user=None, txn=None):
        """POST a receipt with cash allocations and per-invoice discounts."""
        body = {
            "customerId": str(self.customer.id),
            "date": str(self.today),
            "amount": cash,
            "mode": "Bank",
            "bankAccountId": str(self.bank.id),
            "allocations": [
                {"invoiceId": str(inv.id), "amount": amt} for inv, amt in allocations
            ],
            "discountAllocations": [
                {
                    "invoiceId": str(inv.id),
                    "discountAmount": amt,
                    "discountType": dtype,
                    "discountReason": reason,
                }
                for inv, amt, dtype, reason in discounts
            ],
        }
        if txn:
            body["transactionReference"] = txn
        response = self.post("/sales/payments/", body, user=user or self.admin)
        self.assertEqual(response.status_code, expect, response.content[:800])
        return response

    # 1 -------------------------------------------------------------------
    def test_payment_without_discount_is_unchanged(self):
        inv = self.invoice(10500)
        response = self.pay_with_discounts(10500, [(inv, 10500)], [])
        data = response.json()

        self.assertEqual(D(str(data["amount"])), D("10500"))
        self.assertEqual(D(str(data["discountTotal"])), D("0"))
        self.assertEqual(D(str(data["totalSettled"])), D("10500"))
        self.assertEqual(data["discounts"], [])

        inv = self.refreshed(inv)
        self.assertEqual(inv.amount_paid, D("10500"))
        self.assertEqual(inv.settlement_discount, D("0"))
        self.assertEqual(invoice_balance(inv), D("0"))
        self.assertEqual(inv.status, "Paid")
        self.assertEqual(self.account_balance("bank"), D("10500"))
        self.assertEqual(self.account_balance("sales_return"), D("0"))
        self.assertEqual(EarlyPaymentDiscountRequest.objects.count(), 0)

    # 2 + 3 + 8 + 11 -------------------------------------------------------
    def test_partial_discount_full_settlement_and_ledgers(self):
        inv = self.invoice(10500)
        response = self.pay_with_discounts(
            10000, [(inv, 10000)],
            [(inv, 500, "Early Payment Discount", "Paid before the due date")],
        )
        data = response.json()

        # Cash and discount are reported separately in the saved receipt.
        self.assertEqual(D(str(data["amount"])), D("10000"))
        self.assertEqual(D(str(data["discountTotal"])), D("500"))
        self.assertEqual(D(str(data["totalSettled"])), D("10500"))
        self.assertEqual(len(data["discounts"]), 1)
        self.assertEqual(data["discounts"][0]["discountType"], "Early Payment Discount")
        self.assertEqual(D(str(data["discounts"][0]["approvedAmount"])), D("500"))
        self.assertTrue(data["discounts"][0]["creditNoteNumber"])

        # Invoice: cash and discount kept apart, fully settled.
        inv = self.refreshed(inv)
        self.assertEqual(inv.amount_paid, D("10000"))
        self.assertEqual(inv.settlement_discount, D("500"))
        self.assertEqual(invoice_balance(inv), D("0"))
        self.assertEqual(inv.status, "Paid")

        # Accounting: bank sees ONLY cash; discount hits Sales Returns; the
        # customer's receivable is reduced by the full 10,500.
        self.assertEqual(self.account_balance("bank"), D("10000"))
        self.assertEqual(self.account_balance("sales_return"), D("500"))
        self.assertEqual(self.account_balance("debtors", party=self.customer), D("0"))
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.balance, D("0"))

        # Exactly one discount record and one credit note, no duplicate JE.
        req = EarlyPaymentDiscountRequest.objects.get()
        self.assertEqual(req.status, "Approved")
        self.assertEqual(req.approved_amount, D("500"))
        self.assertEqual(req.discount_type, "Early Payment Discount")
        self.assertEqual(self.note_entries().count(), 1)
        self.assertEqual(PaymentIn.objects.count(), 1)

    # 4 + 5 ----------------------------------------------------------------
    def test_multiple_invoices_with_different_discounts(self):
        inv1 = self.invoice(10500)
        inv2 = self.invoice(8000)
        response = self.pay_with_discounts(
            17000,
            [(inv1, 10000), (inv2, 7000)],
            [
                (inv1, 500, "Early Payment Discount", "Paid 20 days early"),
                (inv2, 1000, "Settlement Discount", "Final settlement agreed"),
            ],
        )
        data = response.json()
        self.assertEqual(D(str(data["amount"])), D("17000"))
        self.assertEqual(D(str(data["discountTotal"])), D("1500"))
        self.assertEqual(D(str(data["totalSettled"])), D("18500"))

        # Each invoice settles against its own cash + its own discount.
        inv1, inv2 = self.refreshed(inv1), self.refreshed(inv2)
        self.assertEqual((inv1.amount_paid, inv1.settlement_discount), (D("10000"), D("500")))
        self.assertEqual((inv2.amount_paid, inv2.settlement_discount), (D("7000"), D("1000")))
        self.assertEqual(invoice_balance(inv1), D("0"))
        self.assertEqual(invoice_balance(inv2), D("0"))

        by_invoice = {r.invoice_id: r for r in EarlyPaymentDiscountRequest.objects.all()}
        self.assertEqual(by_invoice[inv1.id].discount_type, "Early Payment Discount")
        self.assertEqual(by_invoice[inv1.id].approved_amount, D("500"))
        self.assertEqual(by_invoice[inv2.id].discount_type, "Settlement Discount")
        self.assertEqual(by_invoice[inv2.id].approved_amount, D("1000"))

        self.assertEqual(self.account_balance("bank"), D("17000"))
        self.assertEqual(self.account_balance("sales_return"), D("1500"))
        self.assertEqual(self.account_balance("debtors", party=self.customer), D("0"))
        self.assertEqual(self.note_entries().count(), 2)

    # 6 --------------------------------------------------------------------
    def test_discount_plus_payment_cannot_exceed_outstanding_and_rolls_back(self):
        inv = self.invoice(10500)
        response = self.pay_with_discounts(
            10000, [(inv, 10000)],
            [(inv, 600, "Special Discount", "Too much")],
            expect=422,
        )
        self.assertEqual(response.json()["code"], "DISCOUNT_EXCEEDS_OUTSTANDING")

        # Nothing persisted: no receipt, no allocation, no credit note, no
        # invoice movement, customer still owes the whole invoice.
        self.assertEqual(PaymentIn.objects.count(), 0)
        self.assertEqual(EarlyPaymentDiscountRequest.objects.count(), 0)
        self.assertEqual(self.note_entries().count(), 0)
        inv = self.refreshed(inv)
        self.assertEqual(inv.amount_paid, D("0"))
        self.assertEqual(inv.settlement_discount, D("0"))
        self.assertEqual(invoice_balance(inv), D("10500"))
        self.assertEqual(self.account_balance("bank"), D("0"))
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.balance, D("10500"))

    # 7 (duplicate payment) -------------------------------------------------
    def test_duplicate_transaction_reference_rolls_back_discount(self):
        inv1 = self.invoice(4000)
        inv2 = self.invoice(10500)
        self.pay_with_discounts(4000, [(inv1, 4000)], [], txn="UTR-UNIQUE-1")

        response = self.pay_with_discounts(
            10000, [(inv2, 10000)],
            [(inv2, 500, "Settlement Discount", "Duplicate txn attempt")],
            expect=409, txn="UTR-UNIQUE-1",
        )
        self.assertEqual(response.json()["code"], "DUPLICATE_RECEIPT")
        self.assertEqual(PaymentIn.objects.count(), 1)  # only the first one
        self.assertEqual(EarlyPaymentDiscountRequest.objects.count(), 0)
        inv2 = self.refreshed(inv2)
        self.assertEqual(inv2.amount_paid, D("0"))
        self.assertEqual(self.account_balance("bank"), D("4000"))

    # 7 (failed discount posting) ------------------------------------------
    def test_missing_discount_reason_rolls_back_receipt(self):
        inv = self.invoice(10500)
        response = self.pay_with_discounts(
            10000, [(inv, 10000)],
            [(inv, 500, "Special Discount", "  ")],
            expect=400,
        )
        self.assertEqual(response.json()["code"], "VALIDATION_FAILED")
        self.assertEqual(PaymentIn.objects.count(), 0)
        self.assertEqual(EarlyPaymentDiscountRequest.objects.count(), 0)
        self.assertEqual(self.account_balance("bank"), D("0"))
        self.assertEqual(invoice_balance(self.refreshed(inv)), D("10500"))

    def test_unsupported_discount_type_rolls_back_receipt(self):
        inv = self.invoice(10500)
        response = self.pay_with_discounts(
            10000, [(inv, 10000)],
            [(inv, 500, "Made Up Discount", "Not an allowed type")],
            expect=400,
        )
        self.assertEqual(response.json()["code"], "VALIDATION_FAILED")
        self.assertEqual(PaymentIn.objects.count(), 0)
        self.assertEqual(self.note_entries().count(), 0)

    def test_user_without_discount_authority_cannot_grant_one(self):
        inv = self.invoice(10500)
        # clerk can record a payment but has no approve_payment_discount.
        response = self.pay_with_discounts(
            10000, [(inv, 10000)],
            [(inv, 500, "Special Discount", "No authority")],
            expect=403, user=self.clerk,
        )
        self.assertEqual(response.json()["code"], "PERMISSION_DENIED")
        self.assertEqual(PaymentIn.objects.count(), 0)
        self.assertEqual(EarlyPaymentDiscountRequest.objects.count(), 0)
        self.assertEqual(invoice_balance(self.refreshed(inv)), D("10500"))

    # 9 + 12 ---------------------------------------------------------------
    def test_saved_discount_details_are_persisted_and_readable(self):
        inv = self.invoice(10500)
        response = self.pay_with_discounts(
            10000, [(inv, 10000)],
            [(inv, 500, "Other Approved Discount", "Goodwill adjustment")],
        )
        payment_id = response.json()["id"]

        detail = self.get(f"/sales/payments/{payment_id}/").json()
        self.assertEqual(len(detail["discounts"]), 1)
        self.assertEqual(detail["discounts"][0]["discountType"], "Other Approved Discount")
        self.assertEqual(detail["discounts"][0]["reason"], "Goodwill adjustment")
        self.assertEqual(D(str(detail["discountTotal"])), D("500"))
        self.assertEqual(D(str(detail["totalSettled"])), D("10500"))

        receivable = self.get("/sales/receivables/", {"customerId": str(self.customer.id)}).json()
        row = next(r for r in (receivable if isinstance(receivable, list) else receivable["results"])
                   if r["id"] == str(inv.id))
        self.assertEqual(D(str(row["approvedDiscount"])), D("500"))
        self.assertEqual(D(str(row["outstandingAmount"])), D("0"))
        self.assertEqual(row["paymentStatus"], "Paid")

    # Reversal path: cancelling the receipt is blocked until the approved
    # discount is cancelled first, so no orphaned receivable is left behind.
    def test_cancelling_receipt_with_approved_discount_is_blocked(self):
        inv = self.invoice(10500)
        response = self.pay_with_discounts(
            10000, [(inv, 10000)],
            [(inv, 500, "Early Payment Discount", "Early payment")],
        )
        payment_id = response.json()["id"]
        blocked = self.post(f"/sales/payments/{payment_id}/cancel/", {"reason": "Bounced"})
        self.assertEqual(blocked.status_code, 422)
        self.assertEqual(PaymentIn.objects.get(pk=payment_id).status, "Active")
