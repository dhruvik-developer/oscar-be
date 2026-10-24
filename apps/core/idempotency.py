"""
Idempotency-Key handling (api.md §1.8, db.md §1.9).

Every POST that creates a financial document accepts an ``Idempotency-Key``
header; a replay returns the original 201 body. The frontend generates the key
once per user intent and reuses it on retry (api-integration.md §5.3), so a
double-click or a retried 5xx must not allocate two invoice numbers.
"""
import hashlib
import json
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from .exceptions import Codes, Conflict

HEADER = "HTTP_IDEMPOTENCY_KEY"
TTL = timedelta(hours=24)


def get_key(request):
    key = request.META.get(HEADER)
    return key.strip() if key else None


def hash_request(endpoint, payload):
    """sha256 of the normalised body (db.md §1.9)."""
    normalised = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(f"{endpoint}\n{normalised}".encode()).hexdigest()


class Replay(Exception):
    """Raised internally when a stored response should be returned as-is."""

    def __init__(self, status, body):
        self.status = status
        self.body = body
        super().__init__("idempotent replay")


def begin(request, client_id, endpoint, payload):
    """Claim an idempotency key.

    Returns the ``IdempotencyKey`` row to finish later, or ``None`` when no key
    was supplied. Raises :class:`Replay` when the original response is already
    stored, and 409 when the same key arrives with a different body.
    """
    from .models import IdempotencyKey

    key = get_key(request)
    if not key or client_id is None:
        return None

    request_hash = hash_request(endpoint, payload)
    now = timezone.now()

    try:
        with transaction.atomic():
            return IdempotencyKey.objects.create(
                client_id=client_id,
                key=key,
                endpoint=endpoint,
                request_hash=request_hash,
                expires_at=now + TTL,
            )
    except IntegrityError:
        pass

    record = IdempotencyKey.objects.filter(
        client_id=client_id, key=key, endpoint=endpoint
    ).first()
    if record is None:  # pragma: no cover - lost a race and the row vanished
        return None

    if record.request_hash != request_hash:
        raise Conflict(
            "This idempotency key was already used with a different request.",
            code=Codes.IDEMPOTENCY_KEY_REUSED,
            detail=f"Key {key} is bound to an earlier body on {endpoint}.",
        )

    if record.state == "completed":
        raise Replay(record.response_status or 201, record.response_body)

    if record.state == "in_progress":
        raise Conflict(
            "That request is still being processed. Please wait a moment.",
            code=Codes.IDEMPOTENCY_KEY_REUSED,
        )

    # A previous attempt failed -- let this one retry under the same key.
    record.state = "in_progress"
    record.request_hash = request_hash
    record.save(update_fields=["state", "request_hash"])
    return record


def finish(record, response, entity=None):
    """Store the response so a replay can return it verbatim."""
    if record is None:
        return response

    # Render through DRF's encoder before storing: `response.data` still holds
    # Decimals, dates and UUIDs, which jsonb cannot take -- and rendering here
    # means a replay returns exactly the bytes the first caller received.
    from rest_framework.renderers import JSONRenderer

    record.state = "completed"
    record.response_status = response.status_code
    record.response_body = json.loads(JSONRenderer().render(response.data))
    if entity is not None:
        record.entity_type = entity.__class__.__name__
        record.entity_id = getattr(entity, "id", None)
    record.save(
        update_fields=[
            "state",
            "response_status",
            "response_body",
            "entity_type",
            "entity_id",
        ]
    )
    return response


def fail(record):
    if record is None:
        return
    record.state = "failed"
    record.save(update_fields=["state"])


def sweep_expired():
    """Hourly job (db.md §13) -- delete rows past ``expires_at``."""
    from .models import IdempotencyKey

    deleted, _ = IdempotencyKey.objects.filter(expires_at__lt=timezone.now()).delete()
    return deleted
