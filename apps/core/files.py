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
import re
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
    "application/zip",
}

EXTENSION_TO_CONTENT_TYPES = {
    ".pdf": ["application/pdf"],
    ".png": ["image/png"],
    ".jpg": ["image/jpeg"],
    ".jpeg": ["image/jpeg"],
    ".webp": ["image/webp"],
    ".gif": ["image/gif"],
    ".csv": ["text/csv"],
    ".txt": ["text/plain"],
    ".docx": ["application/vnd.openxmlformats-officedocument.wordprocessingml.document"],
    ".xlsx": ["application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"],
    ".doc": ["application/msword"],
    ".xls": ["application/vnd.ms-excel"],
    ".zip": ["application/zip"],
}


def sanitize_filename(name):
    """Sanitizes a user-provided file name to prevent path traversal and unsafe characters.

    - Keeps only the basename (strips any path components)
    - Strips null bytes
    - Replaces any character not in [A-Za-z0-9._-] with '_'
    - Strips leading dots
    - Limits length to ~100 chars
    - Falls back to 'file' if empty or invalid
    """
    if not name or not isinstance(name, str):
        return "file"

    # Strip null bytes
    name = name.replace("\x00", "")

    # Normalize slashes and extract basename
    name = name.replace("\\", "/")
    basename = name.split("/")[-1].strip()

    # Replace anything not [A-Za-z0-9._-] with '_'
    sanitized = re.sub(r"[^A-Za-z0-9._-]", "_", basename)

    # Strip leading dots (prevents hidden files, '.', '..')
    sanitized = sanitized.lstrip(".")

    # If empty or containing no alphanumeric character, fallback to "file"
    if not sanitized or not re.search(r"[A-Za-z0-9]", sanitized):
        ext = os.path.splitext(sanitized)[1] if "." in sanitized else ""
        ext = re.sub(r"[^A-Za-z0-9]", "", ext)
        return f"file.{ext}" if ext else "file"

    # Limit length to ~100 characters while preserving extension if present
    if len(sanitized) > 100:
        stem, ext = os.path.splitext(sanitized)
        ext = ext[:20]
        max_stem = max(1, 100 - len(ext))
        sanitized = stem[:max_stem] + ext

    return sanitized or "file"


def validate_file_content(data, declared_content_type):
    """Inspects file bytes to verify magic numbers / signatures match the declared content type."""
    if not data:
        raise ValidationFailed(
            "File content is empty.",
            code="FILE_EMPTY",
            field_errors={"file": ["File is empty."]},
        )

    content_type = (declared_content_type or "").lower()

    # 1. PDF
    if content_type == "application/pdf":
        if not data.startswith(b"%PDF-") and not data.startswith(b"%PDF"):
            raise ValidationFailed(
                "File content does not match PDF signature.",
                code="INVALID_FILE_SIGNATURE",
                field_errors={"file": ["Expected a valid PDF document."]},
            )
        return

    # 2. PNG
    if content_type == "image/png":
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValidationFailed(
                "File content does not match PNG signature.",
                code="INVALID_FILE_SIGNATURE",
                field_errors={"file": ["Expected a valid PNG image."]},
            )
        return

    # 3. JPEG
    if content_type == "image/jpeg":
        if not (data.startswith(b"\xff\xd8\xff") or data.startswith(b"\xff\xd8")):
            raise ValidationFailed(
                "File content does not match JPEG signature.",
                code="INVALID_FILE_SIGNATURE",
                field_errors={"file": ["Expected a valid JPEG image."]},
            )
        return

    # 4. GIF
    if content_type == "image/gif":
        if not (data.startswith(b"GIF87a") or data.startswith(b"GIF89a")):
            raise ValidationFailed(
                "File content does not match GIF signature.",
                code="INVALID_FILE_SIGNATURE",
                field_errors={"file": ["Expected a valid GIF image."]},
            )
        return

    # 5. WEBP
    if content_type == "image/webp":
        if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
            raise ValidationFailed(
                "File content does not match WEBP signature.",
                code="INVALID_FILE_SIGNATURE",
                field_errors={"file": ["Expected a valid WEBP image."]},
            )
        return

    # 6. ZIP and OOXML (DOCX, XLSX)
    zip_types = {
        "application/zip",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }
    if content_type in zip_types:
        if not (
            data.startswith(b"PK\x03\x04")
            or data.startswith(b"PK\x05\x06")
            or data.startswith(b"PK\x07\x08")
        ):
            raise ValidationFailed(
                "File content does not match ZIP/Office package signature.",
                code="INVALID_FILE_SIGNATURE",
                field_errors={"file": ["Expected a valid ZIP or Office package."]},
            )
        return

    # 7. Legacy Office (DOC, XLS)
    ole_types = {
        "application/msword",
        "application/vnd.ms-excel",
    }
    if content_type in ole_types:
        if not data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
            raise ValidationFailed(
                "File content does not match Office document signature.",
                code="INVALID_FILE_SIGNATURE",
                field_errors={"file": ["Expected a valid Word/Excel document."]},
            )
        return

    # 8. Plain text and CSV
    if content_type in ("text/csv", "text/plain"):
        try:
            if b"\x00" in data:
                raise ValueError("Null bytes in text")
            sample = data[:8192].decode("utf-8")
        except UnicodeDecodeError:
            try:
                sample = data[:8192].decode("latin-1")
            except Exception:
                raise ValidationFailed(
                    "Invalid text encoding.",
                    code="INVALID_FILE_ENCODING",
                    field_errors={"file": ["File is not valid text."]},
                )
        except ValueError:
            raise ValidationFailed(
                "Binary data in text file.",
                code="INVALID_FILE_ENCODING",
                field_errors={"file": ["File contains invalid characters."]},
            )

        lower_sample = sample.lower()
        forbidden_tags = (
            "<script",
            "<!doctype html",
            "<html",
            "<svg",
            "<?xml",
            "<iframe",
            "<object",
            "<embed",
            "<applet",
            "<meta",
            "javascript:",
        )
        if any(tag in lower_sample for tag in forbidden_tags):
            raise ValidationFailed(
                "HTML and script tags are not allowed in text/csv files.",
                code="UNSAFE_TEXT_CONTENT",
                field_errors={"file": ["Contains forbidden HTML/script markup."]},
            )
        return

    raise ValidationFailed(
        f"'{content_type}' is not an accepted file type.",
        code="UNSUPPORTED_CONTENT_TYPE",
        field_errors={"contentType": [f"'{content_type}' is not accepted."]},
    )


def max_bytes_for(scope):
    if scope in PMS_SCOPES:
        return settings.PMS_PROOF_MAX_BYTES
    return settings.FILE_MAX_BYTES


def storage_root():
    root = Path(settings.MEDIA_ROOT)
    root.mkdir(parents=True, exist_ok=True)
    return root


def build_storage_key(client_id, scope, file_name):
    clean_name = sanitize_filename(file_name)
    extension = Path(clean_name).suffix.lower()[:12]
    clean_scope = re.sub(r"[^A-Za-z0-9_-]", "_", str(scope))
    clean_client = re.sub(r"[^A-Za-z0-9_-]", "_", str(client_id))
    return f"{clean_client}/{clean_scope}/{uuid.uuid4().hex}{extension}"


def local_path(storage_key):
    root = storage_root().resolve()
    clean_key = str(storage_key).lstrip("/\\")
    path = (root / clean_key).resolve()
    if not path.is_relative_to(root):
        raise NotFound("Invalid file path.")
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
        clean_name = "file"
    else:
        clean_name = sanitize_filename(file_name)

    ext = Path(clean_name).suffix.lower() if clean_name else ""
    if not ext or ext not in EXTENSION_TO_CONTENT_TYPES:
        errors["fileName"] = [f"File extension '{ext}' is not allowed."]

    if not content_type:
        guessed = mimetypes.guess_type(clean_name or "")[0]
        content_type = guessed or "application/octet-stream"

    if content_type not in ALLOWED_CONTENT_TYPES:
        errors["contentType"] = [f"'{content_type}' is not an accepted file type."]
    elif ext in EXTENSION_TO_CONTENT_TYPES and content_type not in EXTENSION_TO_CONTENT_TYPES[ext]:
        errors["contentType"] = [f"Content type '{content_type}' does not match extension '{ext}'."]

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
    if not path.exists() or not path.is_file():
        raise NotFound("That file is no longer available.")
    with open(path, "rb") as handle:
        return handle.read()


def delete_bytes(storage_key):
    path = local_path(storage_key)
    if path.exists() and path.is_file():
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
