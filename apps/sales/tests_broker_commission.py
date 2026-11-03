from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import Client, User
from apps.core.models import Notification
from apps.crm.models import Lead, Stage
from apps.masters.models import Broker, Party

from . import broker_services
from .models import SalesInvoice, SalesOrder


class BrokerCollectionCommissionTests(TestCase):
    def setUp(self):
        self.tenant = Client.objects.create(slug="broker-commission", name="Broker Commission")
        self.manager = User.objects.create_user(
            email="broker-manager@example.com",
            password="test-password",
            client=self.tenant,
            is_superuser=True,
        )
        self.unrelated_user = User.objects.create_user(
            email="unrelated@example.com",
            password="test-password",
            client=self.tenant,
        )
        self.customer = Party.objects.create(
            client=self.tenant,
            code="BROKER-CUSTOMER",
            name="Broker Customer",
            type="Customer",
        )
        self.broker = Broker.objects.create(
            client=self.tenant,
            code="BROKER-01",
            name="Lead Broker",
            commission_type="percent",
            default_value=Decimal("10"),
        )
        self.stage = Stage.objects.create(client=self.tenant, name="Negotiation")
        self.lead = Lead.objects.create(
            client=self.tenant,
            lead_number="LEAD-BROKER-01",
            name="Lead Contact",
            company=self.customer.name,
            stage=self.stage,
            custom_values={
                "broker_id": str(self.broker.id),
                "commission_basis": "percent",
                "commission_rate": "10",
            },
        )
        self.order = SalesOrder.objects.create(
            client=self.tenant,
            party=self.customer,
            doc_date=timezone.localdate(),
            crm_lead=self.lead,
        )
        self.invoice = SalesInvoice.objects.create(
            client=self.tenant,
            party=self.customer,
            sales_order=self.order,
            doc_date=timezone.localdate(),
            total=Decimal("1000"),
            taxable_value=Decimal("1000"),
        )

    def test_commission_is_earned_pro_rata_and_notified_on_customer_receipts(self):
        commission = broker_services.calculate_broker_commission(self.invoice)
        self.assertEqual(commission.broker_id, self.broker.id)
        self.assertEqual(commission.commission_amount, Decimal("0.00"))
        self.assertEqual(commission.status, "Awaiting Collection")

        self.invoice.amount_paid = Decimal("500")
        self.invoice.save(update_fields=["amount_paid", "updated_at"])
        broker_services.sync_broker_commissions_for_invoice(self.invoice, user=self.manager)
        commission.refresh_from_db()
        self.assertEqual(commission.commission_amount, Decimal("50.00"))
        self.assertEqual(commission.status, "Accrued")
        self.assertEqual(
            Notification.objects.filter(
                recipient=self.manager, type="broker_commission_due"
            ).count(),
            1,
        )
        self.assertFalse(
            Notification.objects.filter(
                recipient=self.unrelated_user, type="broker_commission_due"
            ).exists()
        )

        broker_services.sync_broker_commissions_for_invoice(self.invoice, user=self.manager)
        self.assertEqual(
            Notification.objects.filter(
                recipient=self.manager, type="broker_commission_due"
            ).count(),
            1,
        )

        self.invoice.amount_paid = Decimal("1000")
        self.invoice.save(update_fields=["amount_paid", "updated_at"])
        broker_services.sync_broker_commissions_for_invoice(self.invoice, user=self.manager)
        commission.refresh_from_db()
        self.assertEqual(commission.commission_amount, Decimal("100.00"))
        self.assertEqual(
            Notification.objects.filter(
                recipient=self.manager, type="broker_commission_due"
            ).count(),
            2,
        )

    def test_lost_lead_cannot_earn_commission(self):
        commission = broker_services.calculate_broker_commission(self.invoice)
        self.stage.is_lost = True
        self.stage.save(update_fields=["is_lost", "updated_at"])

        broker_services.sync_broker_commissions_for_invoice(self.invoice)
        commission.refresh_from_db()
        self.assertEqual(commission.status, "Cancelled")
        self.assertEqual(commission.commission_amount, Decimal("0.00"))
        self.assertFalse(
            Notification.objects.filter(type="broker_commission_due").exists()
        )

    def test_receipt_cancellation_reverses_unapproved_commission(self):
        commission = broker_services.calculate_broker_commission(self.invoice)
        self.invoice.amount_paid = Decimal("500")
        self.invoice.save(update_fields=["amount_paid", "updated_at"])
        broker_services.sync_broker_commissions_for_invoice(self.invoice)

        self.invoice.amount_paid = Decimal("0")
        self.invoice.save(update_fields=["amount_paid", "updated_at"])
        broker_services.sync_broker_commissions_for_invoice(self.invoice)
        commission.refresh_from_db()

        self.assertEqual(commission.commission_amount, Decimal("0.00"))
        self.assertEqual(commission.status, "Awaiting Collection")
