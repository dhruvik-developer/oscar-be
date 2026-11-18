"""Security tests for file uploads, downloads, path traversal prevention and safe handling.
"""
import io
import os
import uuid
from pathlib import Path

from django.conf import settings
from django.test import TestCase, override_settings
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, Role, RolePermission, User
from apps.accounts.permission_catalogue import seed_roles, sync_permissions
from apps.core.exceptions import NotFound, ValidationFailed
from apps.core import files as file_service
from apps.core.models import File


def uid(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


class FileSecurityTests(TestCase):
    def setUp(self):
        self.tenant = Client.objects.create(slug=uid("sec-files"), name="Security Files Tenant")
        sync_permissions()
        seed_roles(self.tenant)
        self.user = User.objects.create_user(
            email=f"{uid('u')}@example.com",
            password="Password123!",
            client=self.tenant,
            name="File Tester",
        )
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(self.user)['access']}")

    def test_sanitize_filename(self):
        # Path traversal sequences must be stripped to pure basename
        self.assertEqual(file_service.sanitize_filename("../../etc/passwd"), "passwd")
        self.assertEqual(file_service.sanitize_filename("..\\..\\win.ini"), "win.ini")
        self.assertEqual(file_service.sanitize_filename("a/b/c.pdf"), "c.pdf")
        self.assertEqual(file_service.sanitize_filename("/var/log/app.log"), "app.log")
        self.assertEqual(file_service.sanitize_filename("\x00../../etc/shadow"), "shadow")
        
        # Disallowed characters replaced with '_'
        self.assertEqual(file_service.sanitize_filename("test file (1).pdf"), "test_file__1_.pdf")
        
        # Leading dots stripped
        self.assertEqual(file_service.sanitize_filename(".hidden.txt"), "hidden.txt")
        self.assertEqual(file_service.sanitize_filename("..."), "file")
        self.assertEqual(file_service.sanitize_filename(""), "file")
        self.assertEqual(file_service.sanitize_filename(None), "file")
        
        # Very long name limited to ~100 chars
        long_name = "a" * 150 + ".pdf"
        sanitized = file_service.sanitize_filename(long_name)
        self.assertLessEqual(len(sanitized), 100)
        self.assertTrue(sanitized.endswith(".pdf"))

    def test_local_path_path_traversal_prevention(self):
        # Attempting to resolve a path outside MEDIA_ROOT must raise NotFound
        with self.assertRaises(NotFound):
            file_service.local_path("../../../../etc/passwd")

        with self.assertRaises(NotFound):
            file_service.local_path("..\\..\\..\\windows\\win.ini")

        with self.assertRaises(NotFound):
            file_service.read_bytes("../../../../etc/passwd")

        with self.assertRaises(NotFound):
            file_service.write_bytes("../../../../etc/passwd", b"malicious")

        with self.assertRaises(NotFound):
            file_service.delete_bytes("../../../../etc/passwd")

    def test_svg_and_octet_stream_rejected(self):
        # SVG and application/octet-stream must be rejected
        with self.assertRaises(ValidationFailed) as cm:
            file_service.validate_upload_request(
                file_name="image.svg",
                content_type="image/svg+xml",
                size=1024,
                scope="other",
            )
        self.assertIn("contentType", cm.exception.field_errors)

        with self.assertRaises(ValidationFailed) as cm:
            file_service.validate_upload_request(
                file_name="data.bin",
                content_type="application/octet-stream",
                size=1024,
                scope="other",
            )
        self.assertIn("contentType", cm.exception.field_errors)

    def test_content_type_extension_mismatch_rejected(self):
        # Mismatch between extension and declared content type
        with self.assertRaises(ValidationFailed) as cm:
            file_service.validate_upload_request(
                file_name="malicious.pdf",
                content_type="image/png",
                size=1024,
                scope="other",
            )
        self.assertIn("contentType", cm.exception.field_errors)

    def test_magic_bytes_validation_in_upload(self):
        # Create a pending file record
        file_row = File.objects.create(
            client=self.tenant,
            scope="other",
            storage_key=file_service.build_storage_key(self.tenant.id, "other", "doc.pdf"),
            file_name="doc.pdf",
            content_type="application/pdf",
            file_size=50,
            status="pending",
            uploaded_by=self.user,
        )
        token = file_service.sign_upload(file_row.id)
        url = f"/api/v1/files/{file_row.id}/upload/?token={token}"

        # Upload HTML content disguised as PDF (matching declared size)
        bad_content = b"<html><script>alert(1)</script></html>"
        bad_content += b" " * (50 - len(bad_content))
        client = APIClient()
        resp = client.put(url, bad_content, content_type="application/pdf")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data.get("code"), "INVALID_FILE_SIGNATURE")

        # Upload valid PDF magic bytes
        good_content = b"%PDF-1.4 " + b"0" * 41
        file_row.file_size = len(good_content)
        file_row.save(update_fields=["file_size"])
        resp = client.put(url, good_content, content_type="application/pdf")
        self.assertEqual(resp.status_code, 204)

    def test_file_download_security_headers(self):
        content = b"%PDF-1.4 test content"
        storage_key = file_service.build_storage_key(self.tenant.id, "other", "invoice.pdf")
        file_service.write_bytes(storage_key, content)
        
        file_row = File.objects.create(
            client=self.tenant,
            scope="other",
            storage_key=storage_key,
            file_name="invoice.pdf",
            content_type="application/pdf",
            file_size=len(content),
            status="committed",
            uploaded_by=self.user,
        )
        token = file_service.sign_download(file_row.id)
        url = f"/api/v1/files/{file_row.id}/download/?token={token}"

        client = APIClient()
        resp = client.get(url)
        self.assertEqual(resp.status_code, 200)
        # Default download must be attachment
        self.assertIn("attachment", resp["Content-Disposition"].lower())
        self.assertEqual(resp["X-Content-Type-Options"], "nosniff")
        self.assertEqual(resp["Content-Security-Policy"], "default-src 'none'; sandbox")
        self.assertEqual(resp["X-Frame-Options"], "DENY")

        # Explicit inline preview for PDF
        inline_resp = client.get(f"{url}&inline=true")
        self.assertEqual(inline_resp.status_code, 200)
        self.assertIn("inline", inline_resp["Content-Disposition"].lower())
        self.assertEqual(inline_resp["X-Content-Type-Options"], "nosniff")
        self.assertEqual(inline_resp["Content-Security-Policy"], "default-src 'none'; sandbox")
        self.assertEqual(inline_resp["X-Frame-Options"], "SAMEORIGIN")
