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
    entityId = serializers.CharField(source="entity_id", read_only=True)

    class Meta:
        model = Notification
        fields = [
            "id", "type", "entity_type", "entityId", "actorId", "title", "body",
            "channels", "read", "payload", "category", "created_at",
        ]

    def get_read(self, notification):
        return notification.read_at is not None


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
