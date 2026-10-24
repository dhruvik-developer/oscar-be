"""
Audit trail (api.md §1.10, db.md §2.5) and notification fan-out (api.md §1.13).

Every write records ``{ actorId, actorName, action, entityType, entityId,
description, timestamp, before, after }``. This module is the *single* service
function db.md §2.5 asks for -- called inside the same transaction as the
mutation, never from a post-commit hook.
"""
import uuid
from decimal import Decimal

from django.utils import timezone


def _jsonable(value):
    """Snapshots go into jsonb, so Decimals, dates and UUIDs must be text."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def snapshot(instance, fields=None):
    """A jsonb-safe ``before``/``after`` snapshot of a model instance."""
    if instance is None:
        return None
    data = {}
    for field in instance._meta.concrete_fields:
        if fields is not None and field.name not in fields:
            continue
        if field.name in ("created_by", "updated_by", "deleted_by"):
            continue
        data[field.name] = _jsonable(getattr(instance, field.attname, None))
    return data


def record_audit(
    *,
    client,
    actor=None,
    action,
    entity_type,
    entity_id=None,
    entity_label=None,
    description=None,
    before=None,
    after=None,
    from_value=None,
    to_value=None,
    comments=None,
    ip=None,
):
    """Write one audit row. Returns it so callers can chain a notification."""
    from .models import AuditLog

    client_id = getattr(client, "id", client)
    if client_id is None:
        return None

    actor_name = "SYSTEM"
    actor_obj = None
    if actor is not None and getattr(actor, "is_authenticated", False):
        actor_obj = actor
        actor_name = getattr(actor, "name", None) or getattr(actor, "email", "") or "SYSTEM"

    return AuditLog.objects.create(
        client_id=client_id,
        actor=actor_obj,
        actor_name=actor_name,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id if isinstance(entity_id, (uuid.UUID, type(None))) else _coerce_uuid(entity_id),
        entity_label=entity_label,
        description=description,
        before=_jsonable(before),
        after=_jsonable(after),
        from_value=None if from_value is None else str(from_value),
        to_value=None if to_value is None else str(to_value),
        comments=comments,
        ip=ip,
    )


def _coerce_uuid(value):
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def request_ip(request):
    if request is None:
        return None
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


# ---------------------------------------------------------------------------
# Notifications (api.md §1.13)
# ---------------------------------------------------------------------------
def notify(
    *,
    client,
    recipients,
    type,
    category,
    title,
    body=None,
    entity_type=None,
    entity_id=None,
    actor=None,
    channels=None,
    payload=None,
):
    """Create in-app notifications for a set of users.

    ``recipients`` may contain users, user ids or None entries -- None and
    duplicates are dropped so callers can pass ``[stage.assigned_user,
    project.project_manager]`` without filtering first.
    """
    from .models import Notification

    client_id = getattr(client, "id", client)
    if client_id is None:
        return []

    seen = set()
    targets = []
    for recipient in recipients or []:
        if recipient is None:
            continue
        recipient_id = getattr(recipient, "id", recipient)
        if recipient_id is None or recipient_id in seen:
            continue
        seen.add(recipient_id)
        targets.append(recipient_id)

    if not targets:
        return []

    actor_id = getattr(actor, "id", None) if getattr(actor, "is_authenticated", False) else None
    rows = [
        Notification(
            client_id=client_id,
            recipient_id=recipient_id,
            type=type,
            category=category,
            title=title,
            body=body,
            entity_type=entity_type,
            entity_id=entity_id if isinstance(entity_id, uuid.UUID) else _coerce_uuid(entity_id),
            actor_id=actor_id,
            channels=channels or ["in_app"],
            payload=_jsonable(payload),
        )
        for recipient_id in targets
    ]
    return Notification.objects.bulk_create(rows)


def mark_read(notification, at=None):
    notification.read_at = at or timezone.now()
    notification.save(update_fields=["read_at"])
    return notification
