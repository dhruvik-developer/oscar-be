"""Serializers for the platform endpoints (files, notifications, audit, settings)."""
from rest_framework import serializers

from .files import public_url
from .models import AuditLog, ExportJob, File, Notification, Setting, SupportTicket
from .serializers import BaseModelSerializer, BaseSerializer


class UploadUrlRequestSerializer(BaseSerializer):
    fileName = serializers.CharField()
    contentType = serializers.CharField(required=False, allow_blank=True)
    size = serializers.IntegerField()
    scope = serializers.ChoiceField(choices=[value for value, _ in File.SCOPES], default="other")


class FileSerializer(BaseModelSerializer):
    url = serializers.SerializerMethodField()

    class Meta:
        model = File
        fields = [
            "id", "file_name", "content_type", "file_size", "scope", "status",
            "url", "created_at", "committed_at",
        ]

    def get_url(self, file_row):
        return public_url(file_row, self.context.get("request"))


class AuditLogSerializer(BaseModelSerializer):
    """api.md §1.10 -- feeds the PMS activity tab, CRM lead timeline and
    Administration -> Audit Logs."""

    actorId = serializers.CharField(source="actor_id", read_only=True)
    actorName = serializers.CharField(source="actor_name", read_only=True)
    timestamp = serializers.DateTimeField(source="created_at", read_only=True)
    entityId = serializers.CharField(source="entity_id", read_only=True)

    class Meta:
        model = AuditLog
        fields = [
            "id", "actorId", "actorName", "action", "entity_type", "entityId",
            "entity_label", "description", "before", "after", "from_value",
            "to_value", "comments", "timestamp",
        ]


class NotificationSerializer(BaseModelSerializer):
    """Mirrors ``services/crmEventNotifications.js`` (api.md §1.13)."""

    read = serializers.SerializerMethodField()
    actorId = serializers.CharField(source="actor_id", read_only=True)
    actorName = serializers.SerializerMethodField()
    entityId = serializers.CharField(source="entity_id", read_only=True)
    dismissed = serializers.SerializerMethodField()
    snoozedUntil = serializers.DateTimeField(source="snoozed_until", read_only=True)
    meta = serializers.SerializerMethodField()

    class Meta:
        model = Notification
        fields = [
            "id", "type", "entity_type", "entityId", "actorId", "actorName", "title", "body",
            "channels", "read", "dismissed", "snoozedUntil", "payload", "category", "meta",
            "created_at",
        ]

    def get_read(self, notification):
        return notification.read_at is not None

    def get_dismissed(self, notification):
        return notification.dismissed_at is not None

    def get_actorName(self, notification):
        actor = notification.actor
        return (getattr(actor, "name", None) or getattr(actor, "email", None)) if actor else None

    def _task_meta(self):
        """Current state of the CRM tasks the listed notifications point at,
        read once per page rather than once per row."""
        cache = self.context.setdefault("_task_meta", None)
        if cache is not None:
            return cache
        from apps.crm.models import Task

        rows = getattr(getattr(self, "parent", None), "instance", None)
        rows = list(rows) if rows is not None else [self.instance]
        ids = {n.entity_id for n in rows if getattr(n, "entity_type", None) == "CrmTask" and n.entity_id}
        cache = {
            t.id: {"taskStatus": t.status, "dueDate": t.due_date.isoformat() if t.due_date else None,
                   "dueTime": t.due_time, "leadName": t.lead.name if t.lead_id else None}
            for t in Task.objects.filter(id__in=ids).select_related("lead")
        } if ids else {}
        self.context["_task_meta"] = cache
        return cache

    def get_meta(self, notification):
        """Live state of what the notification is about (e.g. a task's status
        and due date), so the bell can show it and offer the right action."""
        if notification.entity_type == "CrmTask" and notification.entity_id:
            return self._task_meta().get(notification.entity_id)
        return None


class SettingSerializer(BaseModelSerializer):
    class Meta:
        model = Setting
        fields = ["key", "value", "updated_at"]


class SupportTicketSerializer(BaseModelSerializer):
    class Meta:
        model = SupportTicket
        fields = [
            "id", "subject", "message", "page_url", "app_version", "status",
            "resolution", "created_at", "resolved_at",
        ]
        read_only_fields = ["status", "resolution", "resolved_at", "created_at"]


class ExportJobSerializer(BaseModelSerializer):
    downloadUrl = serializers.SerializerMethodField()

    class Meta:
        model = ExportJob
        fields = [
            "id", "report_key", "format", "params", "status", "error",
            "downloadUrl", "created_at", "completed_at",
        ]

    def get_downloadUrl(self, job):
        return public_url(job.file, self.context.get("request")) if job.file_id else None
