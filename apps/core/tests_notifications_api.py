"""Notification bell ↔ API: the fields the bell reads, and read / unread / read-all."""
import uuid

from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, User
from apps.core.audit import notify
from apps.core.models import Notification


class NotificationApiTests(TestCase):
    def setUp(self):
        self.tenant = Client.objects.create(slug=f"nt-{uuid.uuid4().hex[:8]}", name="Bell")
        self.me = self.user("Me")
        self.other = self.user("Other")
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(self.me)['access']}")

    def user(self, name):
        return User.objects.create_user(
            email=f"{name.lower()}-{uuid.uuid4().hex[:6]}@example.com", password="Pass1234!",
            client=self.tenant, name=name, is_superuser=True,
        )

    def raise_for(self, user, **extra):
        fields = {"type": "pms.task_assigned", "category": "pms", "title": "New task", "body": "Project P-1."}
        fields.update(extra)
        return notify(client=self.tenant, recipients=[user], **fields)[0]

    def rows(self, **query):
        body = self.api.get("/api/v1/notifications/", query).data
        return body["results"] if isinstance(body, dict) else body

    def test_feed_carries_the_fields_the_bell_reads(self):
        self.raise_for(self.me, entity_type="PmsTask", entity_id=uuid.uuid4())
        row = self.rows()[0]
        for key in ("id", "type", "category", "title", "body", "read", "entityType", "entityId", "createdAt"):
            self.assertIn(key, row)
        self.assertFalse(row["read"])

    def test_read_then_unread_round_trip(self):
        note = self.raise_for(self.me)
        self.assertEqual(self.api.post(f"/api/v1/notifications/{note.id}/read/").status_code, 204)
        self.assertTrue(self.rows()[0]["read"])

        self.assertEqual(self.api.post(f"/api/v1/notifications/{note.id}/unread/").status_code, 204)
        note.refresh_from_db()
        self.assertIsNone(note.read_at)
        self.assertEqual(len(self.rows(unread="true")), 1)

    def test_cannot_touch_someone_elses_notification(self):
        theirs = self.raise_for(self.other)
        self.assertEqual(self.api.post(f"/api/v1/notifications/{theirs.id}/read/").status_code, 404)
        self.assertEqual(self.api.post(f"/api/v1/notifications/{theirs.id}/unread/").status_code, 404)
        self.assertIsNone(Notification.objects.get(pk=theirs.id).read_at)

    def test_read_all_clears_only_my_unread(self):
        self.raise_for(self.me)
        self.raise_for(self.me, title="Second")
        theirs = self.raise_for(self.other)
        self.assertEqual(self.api.post("/api/v1/notifications/read-all/").data["updated"], 2)
        self.assertEqual(self.rows(unread="true"), [])
        self.assertIsNone(Notification.objects.get(pk=theirs.id).read_at)


class NotificationInboxActionTests(TestCase):
    """Dismiss, snooze, clear-read, bulk actions and task state in the feed."""

    setUp = NotificationApiTests.setUp
    user = NotificationApiTests.user
    raise_for = NotificationApiTests.raise_for
    rows = NotificationApiTests.rows

    def test_dismiss_moves_it_to_archived_and_restore_brings_it_back(self):
        note = self.raise_for(self.me)
        self.assertEqual(self.api.post(f"/api/v1/notifications/{note.id}/dismiss/").status_code, 204)
        self.assertEqual(self.rows(), [])
        archived = self.rows(view="archived")
        self.assertEqual([r["id"] for r in archived], [str(note.id)])
        self.assertTrue(archived[0]["dismissed"] and archived[0]["read"])
        self.assertEqual(self.api.post(f"/api/v1/notifications/{note.id}/restore/").status_code, 204)
        self.assertEqual(len(self.rows()), 1)

    def test_snooze_hides_until_due_then_returns_unread(self):
        from datetime import timedelta

        from django.utils import timezone

        note = self.raise_for(self.me)
        self.api.post(f"/api/v1/notifications/{note.id}/read/")
        res = self.api.post(f"/api/v1/notifications/{note.id}/snooze/", {"minutes": 60}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(self.rows(), [])
        self.assertEqual(len(self.rows(view="snoozed")), 1)
        self.assertEqual(self.api.get("/api/v1/notifications/digest/").data["total"], 0)
        Notification.objects.filter(pk=note.id).update(snoozed_until=timezone.now() - timedelta(minutes=1))
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["read"])  # back as unread
        bad = self.api.post(f"/api/v1/notifications/{note.id}/snooze/", {"minutes": 0}, format="json")
        self.assertEqual(bad.status_code, 400)

    def test_clear_read_and_bulk(self):
        a, b, c = self.raise_for(self.me), self.raise_for(self.me), self.raise_for(self.me)
        theirs = self.raise_for(self.other)
        self.api.post(f"/api/v1/notifications/{a.id}/read/")
        self.assertEqual(self.api.post("/api/v1/notifications/clear-read/").data["updated"], 1)
        self.assertEqual({r["id"] for r in self.rows()}, {str(b.id), str(c.id)})
        res = self.api.post("/api/v1/notifications/bulk/", {"ids": [str(b.id), str(theirs.id)], "action": "dismiss"},
                            format="json")
        self.assertEqual(res.data["updated"], 1)  # someone else's is untouched
        self.assertIsNone(Notification.objects.get(pk=theirs.id).dismissed_at)
        self.assertEqual([r["id"] for r in self.rows()], [str(c.id)])

    def test_task_notification_carries_live_task_state(self):
        from datetime import date

        from apps.crm.models import Lead, Stage, Task

        stage = Stage.objects.create(client=self.tenant, name="New", sequence=1)
        lead = Lead.objects.create(client=self.tenant, lead_number="L-1", name="Radha", stage=stage)
        task = Task.objects.create(client=self.tenant, title="Call", lead=lead, assignee=self.me,
                                   due_date=date(2026, 11, 2), status="Open")
        self.raise_for(self.me, type="crm.task_assigned", category="crm", entity_type="CrmTask", entity_id=task.id,
                       actor=self.other)
        row = self.rows()[0]
        self.assertEqual(row["meta"]["taskStatus"], "Open")
        self.assertEqual(row["meta"]["dueDate"], "2026-11-02")
        self.assertEqual(row["meta"]["leadName"], "Radha")
        self.assertEqual(row["actorName"], "Other")
