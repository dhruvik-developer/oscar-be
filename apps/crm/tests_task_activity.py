"""A CRM task's activity: who changed what, in plain words.

    python manage.py test apps.crm.tests_task_activity
"""
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, User
from apps.crm.models import Lead, Stage, Task


class TaskActivityTests(TestCase):
    def setUp(self):
        self.tenant, _ = Client.objects.get_or_create(slug="task-activity", defaults={"name": "Task Activity"})
        self.admin = User.objects.create_user(email="ta-admin@example.com", password="pass", client=self.tenant,
                                              is_superuser=True, name="Asha")
        self.rakesh = User.objects.create_user(email="rakesh@example.com", password="pass", client=self.tenant, name="rakesh")
        self.suresh = User.objects.create_user(email="suresh@example.com", password="pass", client=self.tenant, name="suresh")
        self.lead = Lead.objects.create(client=self.tenant, lead_number="L-TA-1", name="radha", company="Radha Fabrics",
                                        phone="9999999999", email="radha@example.com", city="Surat",
                                        state="Gujarat", country="India",
                                        stage=Stage.objects.create(client=self.tenant, name="New Lead", sequence=1,
                                                                   is_active=True))
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(self.admin)['access']}")

    def test_status_and_reassign_show_in_activity(self):
        r = self.api.post("/api/v1/crm/tasks/", {"title": "Follow-up", "leadId": str(self.lead.id), "assigneeId": str(self.rakesh.id)}, format="json")
        self.assertEqual(r.status_code, 201, r.content[:400])
        task_id = r.json()["id"]
        r = self.api.patch(f"/api/v1/crm/tasks/{task_id}/", {"status": "In Progress"}, format="json")
        self.assertEqual(r.status_code, 200, r.content[:400])
        r = self.api.patch(f"/api/v1/crm/tasks/{task_id}/", {"assigneeId": str(self.suresh.id)}, format="json")
        self.assertEqual(r.status_code, 200, r.content[:400])
        self.assertEqual(str(Task.objects.get(pk=task_id).assignee_id), str(self.suresh.id))

        rows = self.api.get(f"/api/v1/crm/tasks/{task_id}/activity/").json()["results"]
        texts = [row["text"] for row in rows]
        self.assertEqual(texts[0], "Asha reassigned it from rakesh to suresh")
        self.assertEqual(texts[1], "Asha changed status from Open to In Progress")
        self.assertEqual(texts[-1], "Asha created this task")

    def test_automation_task_start_and_complete_show_in_activity(self):
        from apps.crm.models import AUTOMATION_SOURCE

        # Opened by stage automation in services: no create row in the audit log.
        task = Task.objects.create(client=self.tenant, title="Follow-up", lead=self.lead, assignee=self.admin,
                                   source=AUTOMATION_SOURCE)
        r = self.api.post(f"/api/v1/crm/tasks/{task.id}/start/", {}, format="json")
        self.assertEqual(r.status_code, 200, r.content[:400])
        r = self.api.post(f"/api/v1/crm/tasks/{task.id}/complete/",
                          {"outcome": "Interested", "note": "Call back next week"}, format="json")
        self.assertEqual(r.status_code, 200, r.content[:400])

        texts = [row["text"] for row in self.api.get(f"/api/v1/crm/tasks/{task.id}/activity/").json()["results"]]
        self.assertEqual(texts[0], "Asha: Task completed (Interested) — Call back next week")
        self.assertEqual(texts[1], "Asha changed status from Open to In Progress")
        self.assertEqual(texts[-1], AUTOMATION_SOURCE)
