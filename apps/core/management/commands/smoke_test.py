"""
End-to-end HTTP smoke test (api-integration.md §12.3).

Exercises the checklist that document names, over the real URL routing and
serializers rather than by calling services directly:

  1. log in, land on the dashboard
  2. quotation -> order -> challan -> invoice -> payment, checking stock,
     party balance and the ledger after each step
  3. force a 401 and confirm the error shape
  4. confirm the list envelope and the error contract

Run with:  python manage.py smoke_test
"""
import json
import uuid
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.test import Client as HttpClient
from django.utils import timezone


class SmokeFailure(Exception):
    pass


class Command(BaseCommand):
    help = "Run an end-to-end HTTP smoke test against the seeded demo tenant."

    def add_arguments(self, parser):
        parser.add_argument("--email", default="admin@sweven.test")
        parser.add_argument("--password", default="Sweven@2026")

    def handle(self, *args, **options):
        from django.conf import settings

        # django.test.Client sends Host: testserver. Outside a test run nothing
        # adds that to ALLOWED_HOSTS, so every request would 400 before
        # reaching a view.
        if "testserver" not in settings.ALLOWED_HOSTS:
            settings.ALLOWED_HOSTS = list(settings.ALLOWED_HOSTS) + ["testserver"]

        self.http = HttpClient()
        self.base = "/api/v1"
        # Idempotency keys live 24h (db.md 1.9). A fixed key would collide with
        # the previous run's body, so each run gets its own.
        self.run_id = uuid.uuid4().hex[:12]
        self.token = None
        self.passed = 0
        self.failed = 0

        steps = [
            ("auth: login", self.step_login),
            ("auth: me", self.step_me),
            ("auth: 401 shape", self.step_unauthenticated),
            ("error: 404 shape", self.step_not_found),
            ("list: envelope + aggregates", self.step_envelope),
            ("masters: item stock is derived", self.step_item_stock),
            ("sales: pipeline end to end", self.step_sales_pipeline),
            ("sales: business rules return 422", self.step_business_rules),
            ("inventory: movements ledger", self.step_movements),
            ("accounts: ledger and reports", self.step_accounts),
            ("crm: stage automation returns createdTasks", self.step_crm),
            ("pms: handoff-check returns blockers as data", self.step_pms),
            ("hrms: payroll formula", self.step_hrms),
            ("platform: search, notifications, audit", self.step_platform),
            ("openapi: schema generates", self.step_schema),
        ]

        for label, step in steps:
            try:
                detail = step()
                self.passed += 1
                self.stdout.write(
                    self.style.SUCCESS(f"  PASS  {label}") + (f" -- {detail}" if detail else "")
                )
            except Exception as exc:
                self.failed += 1
                self.stdout.write(self.style.ERROR(f"  FAIL  {label}: {exc}"))

        self.stdout.write("")
        summary = f"{self.passed} passed, {self.failed} failed"
        if self.failed:
            self.stdout.write(self.style.ERROR(summary))
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS(summary))

    # -- helpers -----------------------------------------------------------
    def call(self, method, path, body=None, expect=None, auth=True, headers=None):
        extra = dict(headers or {})
        if auth and self.token:
            extra["HTTP_AUTHORIZATION"] = f"Bearer {self.token}"

        handler = getattr(self.http, method.lower())
        if method.upper() in ("POST", "PATCH", "PUT"):
            response = handler(
                f"{self.base}{path}",
                data=json.dumps(body or {}, default=str),
                content_type="application/json",
                **extra,
            )
        else:
            response = handler(f"{self.base}{path}", **extra)

        if expect is not None and response.status_code != expect:
            payload = self._body(response)
            raise SmokeFailure(
                f"{method} {path} -> {response.status_code} (expected {expect}): {payload}"
            )
        return response

    @staticmethod
    def _body(response):
        try:
            return response.json()
        except Exception:
            return response.content[:300]

    @staticmethod
    def expect(condition, message):
        if not condition:
            raise SmokeFailure(message)

    # -- steps -------------------------------------------------------------
    def step_login(self):
        response = self.call(
            "POST",
            "/auth/login/",
            {"email": self.options_email, "password": self.options_password},
            expect=200,
            auth=False,
        )
        body = response.json()
        for key in ("access", "refresh", "user", "permissions", "tenant"):
            self.expect(key in body, f"login response missing '{key}'")
        self.token = body["access"]
        self.tenant = body["tenant"]
        return f"{len(body['permissions'])} permissions, tenant {body['tenant']['name']}"

    def step_me(self):
        body = self.call("GET", "/auth/me/", expect=200).json()
        self.expect(body["user"]["email"] == self.options_email, "wrong user on /auth/me/")
        self.expect(body["user"]["role"] is not None, "user has no role")
        return body["user"]["role"]["name"]

    def step_unauthenticated(self):
        """api.md §1.2 -- an invalid token is 401, never 403."""
        response = self.call(
            "GET", "/sales/invoices/", expect=401, auth=False,
            headers={"HTTP_AUTHORIZATION": "Bearer not-a-real-token"},
        )
        body = response.json()
        self.expect("message" in body, "401 body has no 'message'")
        self.expect("code" in body, "401 body has no 'code'")
        return body["code"]

    def step_not_found(self):
        response = self.call("GET", f"/sales/invoices/{uuid.uuid4()}/", expect=404)
        body = response.json()
        self.expect(body.get("code") == "NOT_FOUND", f"unexpected 404 code: {body}")
        return "NOT_FOUND"

    def step_envelope(self):
        """api.md §1.4 -- count/page/page_size/next/previous/results/aggregates."""
        body = self.call("GET", "/sales/invoices/?page_size=5", expect=200).json()
        for key in ("count", "page", "page_size", "next", "previous", "results", "aggregates"):
            self.expect(key in body, f"list envelope missing '{key}'")
        self.expect(isinstance(body["aggregates"], dict), "aggregates is not an object")
        self.expect("totalValue" in body["aggregates"], "invoice aggregates missing totalValue")
        return f"count={body['count']} aggregates={list(body['aggregates'])[:3]}"

    def step_item_stock(self):
        items = self.call("GET", "/inventory/items/?page_size=50", expect=200).json()
        self.expect(items["count"] > 0, "no items seeded")
        row = next(r for r in items["results"] if r["sku"] == "STL-2MM-CRCA")
        for key in ("availableQty", "reservedQty", "status", "hsnCode"):
            self.expect(key in row, f"item row missing '{key}'")
        self.expect(row["status"] in ("Optimal", "Low Stock", "Critical"), "bad item status")

        detail = self.call("GET", f"/inventory/items/{row['id']}/stock/", expect=200).json()
        for key in ("onHand", "reserved", "damaged", "available", "status"):
            self.expect(key in detail, f"stock payload missing '{key}'")
        return f"{row['sku']} available={detail['available']} {detail['status']}"

    def step_sales_pipeline(self):
        """The core check of api-integration.md §12.3 item 2."""
        parties = self.call("GET", "/parties/customers/?page_size=5", expect=200).json()
        customer = parties["results"][0]
        items = self.call("GET", "/inventory/items/?page_size=50", expect=200).json()
        item = next(r for r in items["results"] if r["sku"] == "MS-ANG-50")

        before = self.call("GET", f"/inventory/items/{item['id']}/stock/", expect=200).json()

        # 1. Quotation
        quotation = self.call(
            "POST",
            "/sales/quotations/",
            {
                "partyId": customer["id"],
                "date": str(timezone.localdate()),
                "lineItems": [
                    {"itemId": item["id"], "qty": 10, "rate": 400, "tax": 18}
                ],
            },
            expect=201,
        ).json()
        self.expect(quotation["quotationNumber"], "quotation has no number")
        self.expect(
            Decimal(str(quotation["total"])) == Decimal("4720.00"),
            f"quotation total wrong: {quotation['total']}",
        )

        # 2. Convert to order
        order = self.call(
            "POST", f"/sales/quotations/{quotation['id']}/convert-to-order/", {}, expect=201
        ).json()
        self.expect(order["orderNumber"], "order has no number")

        # Confirming reserves stock (api.md §5.4).
        self.call("PATCH", f"/sales/orders/{order['id']}/", {"stage": "Confirmed"}, expect=200)
        reserved = self.call(
            "GET", f"/inventory/items/{item['id']}/stock/", expect=200
        ).json()
        self.expect(
            Decimal(str(reserved["reserved"])) >= Decimal("10"),
            f"confirming an order did not reserve stock: {reserved}",
        )

        # 3. Convert to invoice, then finalize
        invoice = self.call(
            "POST", f"/sales/orders/{order['id']}/convert-to-invoice/", {}, expect=201
        ).json()
        self.expect(invoice["status"] == "Draft", "new invoice should be Draft")

        finalized = self.call(
            "POST",
            f"/sales/invoices/{invoice['id']}/finalize/",
            {},
            expect=200,
            headers={"HTTP_IDEMPOTENCY_KEY": f"smoke-{self.run_id}-finalize"},
        ).json()
        self.expect(finalized["invoiceNumber"], "finalized invoice has no number")
        self.expect(finalized["status"] == "Unpaid", f"status after finalize: {finalized['status']}")

        after = self.call("GET", f"/inventory/items/{item['id']}/stock/", expect=200).json()
        self.expect(
            Decimal(str(after["onHand"])) == Decimal(str(before["onHand"])) - Decimal("10"),
            f"stock not depleted: {before['onHand']} -> {after['onHand']}",
        )

        # 4. Payment
        banks = self.call("GET", "/accounts/bank-accounts/", expect=200).json()
        payment = self.call(
            "POST",
            "/sales/payments/",
            {
                "customerId": customer["id"],
                "date": str(timezone.localdate()),
                "amount": 1000,
                "mode": "Bank",
                "bankAccountId": banks["results"][0]["id"],
                "allocations": [{"invoiceId": invoice["id"], "amount": 1000}],
            },
            expect=201,
            headers={"HTTP_IDEMPOTENCY_KEY": f"smoke-{self.run_id}-payment"},
        ).json()
        self.expect(payment["paymentNumber"], "payment has no number")

        outstanding = self.call(
            "GET", f"/sales/invoices/{invoice['id']}/outstanding/", expect=200
        ).json()
        self.expect(
            Decimal(str(outstanding["paid"])) == Decimal("1000.00"),
            f"payment not reflected: {outstanding}",
        )

        # Idempotency replay must return the original body, not a second payment.
        replay = self.call(
            "POST",
            "/sales/payments/",
            {
                "customerId": customer["id"],
                "date": str(timezone.localdate()),
                "amount": 1000,
                "mode": "Bank",
                "bankAccountId": banks["results"][0]["id"],
                "allocations": [{"invoiceId": invoice["id"], "amount": 1000}],
            },
            expect=201,
            headers={"HTTP_IDEMPOTENCY_KEY": f"smoke-{self.run_id}-payment"},
        ).json()
        self.expect(
            replay["paymentNumber"] == payment["paymentNumber"],
            "idempotent replay allocated a second number",
        )

        self._smoke_invoice_id = invoice["id"]
        self._smoke_item_id = item["id"]
        return (
            f"{quotation['quotationNumber']} -> {order['orderNumber']} "
            f"-> {finalized['invoiceNumber']}, outstanding {outstanding['outstanding']}"
        )

    def step_business_rules(self):
        """422 with a user-facing message (api.md §1.5, api-integration.md §5.5)."""
        invoice_id = getattr(self, "_smoke_invoice_id", None)
        self.expect(invoice_id is not None, "sales pipeline step did not run")

        parties = self.call("GET", "/parties/customers/?page_size=5", expect=200).json()
        banks = self.call("GET", "/accounts/bank-accounts/", expect=200).json()

        response = self.call(
            "POST",
            "/sales/payments/",
            {
                "customerId": parties["results"][0]["id"],
                "date": str(timezone.localdate()),
                "amount": 99_999_999,
                "mode": "Bank",
                "bankAccountId": banks["results"][0]["id"],
                "allocations": [{"invoiceId": invoice_id, "amount": 99_999_999}],
            },
            expect=422,
        )
        body = response.json()
        self.expect(
            body.get("code") == "PAYMENT_EXCEEDS_BALANCE",
            f"unexpected code: {body.get('code')}",
        )
        self.expect(body.get("message"), "422 has no user-facing message")

        # A cancel with payments recorded must also be refused.
        cancel = self.call(
            "POST", f"/sales/invoices/{invoice_id}/cancel/", {"reason": "smoke"}, expect=422
        ).json()
        self.expect(
            cancel.get("code") == "INVOICE_HAS_PAYMENTS",
            f"unexpected cancel code: {cancel.get('code')}",
        )
        return f"{body['code']}, {cancel['code']}"

    def step_movements(self):
        item_id = getattr(self, "_smoke_item_id", None)
        body = self.call(
            "GET", f"/inventory/items/{item_id}/movements/?page_size=10", expect=200
        ).json()
        self.expect(body["count"] > 0, "no movements recorded")
        row = body["results"][0]
        for key in ("type", "quantity", "referenceType", "date"):
            self.expect(key in row, f"movement row missing '{key}'")

        summary = self.call("GET", "/inventory/stock/summary/", expect=200).json()
        self.expect("totalValue" in summary, "stock summary missing totalValue")
        return f"{body['count']} movements, stock value {summary['totalValue']}"

    def step_accounts(self):
        balances = self.call("GET", "/accounts/balances/", expect=200).json()
        self.expect(balances["count"] > 0, "no bank accounts")

        parties = self.call("GET", "/parties/customers/?page_size=1", expect=200).json()
        party_id = parties["results"][0]["id"]
        ledger = self.call("GET", f"/parties/{party_id}/ledger/", expect=200).json()
        for key in ("openingBalance", "entries", "closingBalance"):
            self.expect(key in ledger, f"ledger missing '{key}'")

        trial = self.call("GET", "/accounts/reports/trial-balance/", expect=200).json()
        debit = Decimal(str(trial["aggregates"]["totalDebit"]))
        credit = Decimal(str(trial["aggregates"]["totalCredit"]))
        self.expect(debit == credit, f"trial balance does not balance: {debit} vs {credit}")

        pnl = self.call("GET", "/accounts/reports/profit-and-loss/", expect=200).json()
        self.expect("grossProfit" in pnl, "P&L missing grossProfit")
        return f"trial balance Dr=Cr={debit}, revenue {pnl['revenue']}"

    def step_crm(self):
        """api.md §9.3 -- a stage change returns ``{ lead, createdTasks }``."""
        leads = self.call("GET", "/crm/leads/?page_size=5", expect=200).json()
        self.expect(leads["count"] > 0, "no leads seeded")
        row = leads["results"][0]
        for key in ("openTasksCount", "productsCount", "filesCount", "status"):
            self.expect(key in row, f"lead row missing counter '{key}'")

        stages = self.call("GET", "/crm/stages/", expect=200).json()
        target = next(
            s for s in stages["results"] if s["name"] == "Quotation Shared"
        )
        body = self.call(
            "PATCH", f"/crm/leads/{row['id']}/", {"stageId": target["id"]}, expect=200
        ).json()
        self.expect("lead" in body and "createdTasks" in body, "stage change response shape")
        return f"{len(body['createdTasks'])} task(s) auto-created on stage change"

    def step_pms(self):
        projects = self.call("GET", "/pms/projects/?page_size=5", expect=200).json()
        self.expect(projects["count"] > 0, "no PMS projects seeded")
        code = projects["results"][0]["code"]

        detail = self.call("GET", f"/pms/projects/{code}/", expect=200).json()
        self.expect(detail["stages"], "project has no stages")
        stage = detail["stages"][0]

        check = self.call(
            "GET", f"/pms/projects/{code}/stages/{stage['id']}/handoff-check/", expect=200
        ).json()
        self.expect("blockers" in check, "handoff-check has no blockers")
        self.expect("canHandoff" in check, "handoff-check has no canHandoff")
        for blocker in check["blockers"]:
            for key in ("code", "label", "hard"):
                self.expect(key in blocker, f"blocker missing '{key}'")

        # The gate must actually block: this stage has not been started.
        handoff = self.call(
            "POST", f"/pms/projects/{code}/stages/{stage['id']}/handoff/", {}, expect=422
        ).json()
        self.expect(handoff.get("code"), "blocked handoff returned no code")

        badges = self.call("GET", "/pms/nav-badges/", expect=200).json()
        self.expect("pendingApprovals" in badges, "nav badges missing pendingApprovals")
        return (
            f"{code}: {len(check['blockers'])} blocker(s), "
            f"handoff refused with {handoff['code']}"
        )

    def step_hrms(self):
        employees = self.call("GET", "/hrms/employees/?page_size=5", expect=200).json()
        self.expect(employees["count"] > 0, "no employees seeded")

        month = timezone.localdate().replace(day=1)
        run = self.call(
            "POST", "/hrms/payroll/process/", {"month": str(month)}, expect=201
        ).json()
        self.expect(run["payslips"], "payroll produced no payslips")

        slip = run["payslips"][0]
        for key in ("standardSalary", "earnedSalary", "netPayable", "attendedDays", "totalDays"):
            self.expect(key in slip, f"payslip missing '{key}'")

        # api.md §11.4: with no attendance marked, every day is absent, so the
        # earned salary must be zero -- proving the formula ran rather than
        # copying the standard salary across.
        self.expect(
            Decimal(str(slip["earnedSalary"])) < Decimal(str(slip["standardSalary"])),
            "payroll did not prorate for absence",
        )

        summary = self.call("GET", "/hrms/payroll/summary/", expect=200).json()
        self.expect("net" in summary, "payroll summary missing net")
        return f"{len(run['payslips'])} payslip(s), net {summary['net']}"

    def step_platform(self):
        search = self.call("GET", "/search/?q=CRCA", expect=200).json()
        self.expect(search["count"] >= 1, "global search found nothing")
        self.expect("type" in search["results"][0], "search row missing type")

        digest = self.call("GET", "/notifications/digest/", expect=200).json()
        self.expect("total" in digest, "digest missing total")

        audit = self.call("GET", "/audit/?page_size=5", expect=200).json()
        self.expect(audit["count"] > 0, "audit log is empty")

        dashboard = self.call("GET", "/dashboard/", expect=200).json()
        for key in ("sales", "purchase", "cash", "inventoryAlerts"):
            self.expect(key in dashboard, f"dashboard missing '{key}'")
        return f"{search['count']} search hits, {audit['count']} audit rows"

    def step_schema(self):
        response = self.call("GET", "/schema/?format=json", expect=200, auth=True)
        self.expect(len(response.content) > 10_000, "schema looks empty")
        return f"{len(response.content) // 1024} KB OpenAPI document"

    # -- option plumbing ---------------------------------------------------
    options_email = "admin@sweven.test"
    options_password = "Sweven@2026"

    def execute(self, *args, **options):
        self.options_email = options.get("email") or self.options_email
        self.options_password = options.get("password") or self.options_password
        return super().execute(*args, **options)
