"""Alert notifications reach the users who can see alerts (PDF §19)."""
import uuid

from django.test import TestCase

from apps.accounts.models import Client, Role, RolePermission, User, UserPermission
from apps.accounts.permission_catalogue import sync_permissions
from apps.core.alert_services import trigger_incident
from apps.core.models import Notification


def uid(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


class AlertRecipientTests(TestCase):
    def setUp(self):
        sync_permissions()
        self.tenant = Client.objects.create(slug=uid("t"), name="Tenant")
        self.other = Client.objects.create(slug=uid("o"), name="Other")
        self.watcher_role = self.role(self.tenant, "WATCH", ["view_alerts"])
        self.plain_role = self.role(self.tenant, "PLAIN", [])

    def role(self, client, code, perms):
        role = Role.objects.create(client=client, code=code, name=code)
        for perm in perms:
            RolePermission.objects.create(role=role, permission_id=perm)
        return role

    def user(self, name, client=None, role=None, **extra):
        return User.objects.create_user(
            email=f"{uid(name)}@example.com",
            password="Pass1234!",
            client=client or self.tenant,
            name=name,
            role=role,
            **extra,
        )

    def test_notifications_follow_view_alerts_permission(self):
        by_role = self.user("by-role", role=self.watcher_role)
        no_perm = self.user("no-perm", role=self.plain_role)
        granted = self.user("granted", role=self.plain_role)
        UserPermission.objects.create(user=granted, permission_id="view_alerts", effect="grant")
        denied = self.user("denied", role=self.watcher_role)
        UserPermission.objects.create(user=denied, permission_id="view_alerts", effect="deny")
        owner = self.user("owner", is_superuser=True)
        inactive = self.user("inactive", role=self.watcher_role, status="Inactive")
        other_role = self.role(self.other, "WATCH", ["view_alerts"])
        elsewhere = self.user("elsewhere", client=self.other, role=other_role)

        incident = trigger_incident(
            self.tenant.id, "negative_stock", "High", "Stock below zero", "Item X is at -5."
        )
        self.assertIsNotNone(incident)

        notified = set(
            Notification.objects.filter(type="system_alert").values_list("recipient_id", flat=True)
        )
        self.assertEqual(notified, {by_role.id, granted.id, owner.id})
        for user in (no_perm, denied, inactive, elsewhere):
            self.assertNotIn(user.id, notified, user.name)

    def test_refreshing_an_active_incident_does_not_notify_again(self):
        self.user("by-role", role=self.watcher_role)
        args = (self.tenant.id, "negative_stock", "High", "Stock below zero", "Item X is at -5.")
        kwargs = {"entity_type": "Item", "entity_id": "item-1"}
        trigger_incident(*args, **kwargs)
        trigger_incident(*args, **kwargs)
        self.assertEqual(Notification.objects.filter(type="system_alert").count(), 1)
