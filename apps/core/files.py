"""
File uploads (api.md §1.12, db.md §2.4).

Two-step, so the JSON API stays JSON:

    POST /files/upload-url/       -> { uploadUrl, fileId, expiresAt }
    PUT  <uploadUrl>              binary, direct to storage
    POST /files/{fileId}/commit/  -> { id, url, fileName, fileSize, contentType }

db.md Appendix B assumes S3-compatible object storage. This ships a local
backend with the same three-step shape and signed, expiring URLs, so the
frontend integration (api-integration.md §10.4) is written once and the storage
backend can be swapped without touching it.
"""
import hashlib
import hmac
import mimetypes
import os
import time
import uuid
from pathlib import Path

from django.conf import settings
from django.core.signing import BadSignature, SignatureExpired, TimestampSigner
from django.utils import timezone

from .exceptions import NotFound, ValidationFailed

UPLOAD_SALT = "evenmore.files.upload"
DOWNLOAD_SALT = "evenmore.files.download"

#: api.md §1.12 -- default limit 25 MB; PMS design proofs are PDFs up to 50 MB.
PMS_SCOPES = {"pms_document"}

ALLOWED_CONTENT_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/msword",
    "text/csv",
    "text/plain",
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
    "image/svg+xml",
    "application/zip",
    "application/octet-stream",
}


def max_bytes_for(scope):
    if scope in PMS_SCOPES:
        return settings.PMS_PROOF_MAX_BYTES
    return settings.FILE_MAX_BYTES


def storage_root():
    root = Path(settings.MEDIA_ROOT)
    root.mkdir(parents=True, exist_ok=True)
    return root


def build_storage_key(client_id, scope, file_name):
    extension = Path(file_name).suffix.lower()[:12]
    return f"{client_id}/{scope}/{uuid.uuid4().hex}{extension}"


def local_path(storage_key):
    path = storage_root() / storage_key
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def sign_upload(file_id):
    return TimestampSigner(salt=UPLOAD_SALT).sign(str(file_id))


def verify_upload(token, max_age=None):
    signer = TimestampSigner(salt=UPLOAD_SALT)
    try:
        return signer.unsign(token, max_age=max_age or settings.UPLOAD_URL_TTL_SECONDS)
    except SignatureExpired:
        raise ValidationFailed(
            "This upload link has expired. Request a new one.", code="UPLOAD_URL_EXPIRED"
        )
    except BadSignature:
        raise ValidationFailed("Invalid upload link.", code="UPLOAD_URL_INVALID")


def sign_download(file_id, ttl=None):
    return TimestampSigner(salt=DOWNLOAD_SALT).sign(str(file_id))


def verify_download(token, max_age=None):
    signer = TimestampSigner(salt=DOWNLOAD_SALT)
    try:
        return signer.unsign(
            token,
            max_age=settings.FILE_DOWNLOAD_TTL_SECONDS
            if max_age is None
            else max_age,
        )
    except SignatureExpired:
        raise NotFound("This link has expired.")
    except BadSignature:
        raise NotFound("Invalid link.")


def validate_upload_request(*, file_name, content_type, size, scope):
    errors = {}
    if not file_name:
        errors["fileName"] = ["Required."]
    if not content_type:
        content_type = mimetypes.guess_type(file_name or "")[0] or "application/octet-stream"
    if content_type not in ALLOWED_CONTENT_TYPES:
        errors["contentType"] = [f"'{content_type}' is not an accepted file type."]

    limit = max_bytes_for(scope)
    try:
        size = int(size or 0)
    except (TypeError, ValueError):
        size = 0
    if size <= 0:
        errors["size"] = ["Required."]
    elif size > limit:
        errors["size"] = [f"Exceeds the {limit // (1024 * 1024)} MB limit for this upload."]

    if errors:
        raise ValidationFailed("This file cannot be uploaded.", field_errors=errors)
    return content_type, size


def public_url(file_row, request=None):
    """A signed, expiring URL for a committed file.

    Returned as ``previewUrl`` on PMS documents so a client proof opens on any
    device -- the IndexedDB version it replaces could only ever open on the
    machine that uploaded it (api.md §10.5).
    """
    if file_row is None or file_row.status != "committed":
        return None
    token = sign_download(file_row.id)
    path = f"{settings.API_BASE_PATH}/files/{file_row.id}/download/?token={token}"
    if request is not None:
        return request.build_absolute_uri(path)
    return path


def write_bytes(storage_key, data):
    path = local_path(storage_key)
    with open(path, "wb") as handle:
        handle.write(data)
    return path


def read_bytes(storage_key):
    path = local_path(storage_key)
    if not path.exists():
        raise NotFound("That file is no longer available.")
    with open(path, "rb") as handle:
        return handle.read()


def delete_bytes(storage_key):
    path = local_path(storage_key)
    if path.exists():
        try:
            path.unlink()
        except OSError:  # pragma: no cover - permissions / lock
            return False
    return True


def sweep_pending(older_than_hours=24):
    """db.md §2.4 -- pending rows are the easiest way to accumulate garbage.

    Hourly job: delete rows that were never committed, and their bytes.
    """
    from datetime import timedelta

    from .models import File

    cutoff = timezone.now() - timedelta(hours=older_than_hours)
    stale = File.objects.filter(status="pending", created_at__lt=cutoff)
    count = 0
    for row in stale:
        delete_bytes(row.storage_key)
        count += 1
    stale.delete()
    return count
