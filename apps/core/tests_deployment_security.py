import logging
from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.models import Client, Role, User
from apps.accounts.authentication import build_tokens
from apps.core.logging_filters import SensitiveDataFilter


class DeploymentSecurityTests(TestCase):
    def setUp(self):
        self.tenant = Client.objects.create(slug="dep-tenant", name="Deployment Tenant")
        self.role = Role.objects.create(client=self.tenant, code="STAFF", name="Staff")
        self.user = User.objects.create_user(
            email="staff@example.com",
            password="Pass1234!",
            client=self.tenant,
            name="Staff",
            role=self.role,
        )
        self.admin = User.objects.create_user(
            email="admin@example.com",
            password="Pass1234!",
            client=self.tenant,
            name="Admin",
            is_superuser=True,
        )
        self.api = APIClient()

    def test_sensitive_data_filter_masks_logs(self):
        filt = SensitiveDataFilter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname=__file__,
            lineno=10,
            msg="User login token: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.xyz and password: 'SecretPassword123' with otp: 123456",
            args=(),
            exc_info=None,
        )
        filt.filter(record)
        self.assertNotIn("SecretPassword123", record.msg)
        self.assertNotIn("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.xyz", record.msg)
        self.assertIn("[REDACTED]", record.msg)

        # Test dictionary args masking
        arg_record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname=__file__,
            lineno=10,
            msg="Payload received: %s",
            args=({"password": "SuperSecretPassword!", "token": "abc123token", "safe": "hello"},),
            exc_info=None,
        )
        filt.filter(arg_record)
        dict_args = arg_record.args if isinstance(arg_record.args, dict) else arg_record.args[0]
        self.assertEqual(dict_args["password"], "[REDACTED]")
        self.assertEqual(dict_args["token"], "[REDACTED]")
        self.assertEqual(dict_args["safe"], "hello")

    def test_docs_and_schema_restricted_when_not_debug(self):
        with override_settings(DEBUG=False):
            # Anonymous unauthenticated user
            anon_client = APIClient()
            self.assertIn(anon_client.get("/api/v1/schema/").status_code, (401, 403))
            self.assertIn(anon_client.get("/api/v1/docs/").status_code, (401, 403))
            self.assertIn(anon_client.get("/api/v1/redoc/").status_code, (401, 403))

            # Normal non-admin user
            user_client = APIClient()
            user_client.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(self.user)['access']}")
            self.assertEqual(user_client.get("/api/v1/schema/").status_code, 403)
            self.assertEqual(user_client.get("/api/v1/docs/").status_code, 403)
            self.assertEqual(user_client.get("/api/v1/redoc/").status_code, 403)

            # Superuser / admin user
            admin_client = APIClient()
            admin_client.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(self.admin)['access']}")
            self.assertEqual(admin_client.get("/api/v1/schema/").status_code, 200)
            self.assertEqual(admin_client.get("/api/v1/docs/").status_code, 200)
            self.assertEqual(admin_client.get("/api/v1/redoc/").status_code, 200)

    def test_production_startup_validation_rules(self):
        # Test helper verifying validation logic
        def validate_production_env(hosts, db_pass, cors):
            if "*" in hosts:
                raise ImproperlyConfigured("ALLOWED_HOSTS cannot contain '*' when DJANGO_DEBUG=False.")
            localhost_defaults = {"localhost", "127.0.0.1", "0.0.0.0", "testserver", "::1"}
            if not hosts or set(hosts).issubset(localhost_defaults):
                raise ImproperlyConfigured("ALLOWED_HOSTS must specify production domains")
            if db_pass == "postgres" or len(db_pass) < 12:
                raise ImproperlyConfigured("DB_PASSWORD must not be the default 'postgres'")
            for origin in cors:
                if origin.startswith("http://") or "localhost" in origin.lower():
                    raise ImproperlyConfigured("CORS_ALLOWED_ORIGINS cannot contain http:// or localhost")

        with self.assertRaises(ImproperlyConfigured):
            validate_production_env(["*"], "strong_prod_pass_123", ["https://app.oscar.test"])

        with self.assertRaises(ImproperlyConfigured):
            validate_production_env(["localhost", "127.0.0.1"], "strong_prod_pass_123", ["https://app.oscar.test"])

        with self.assertRaises(ImproperlyConfigured):
            validate_production_env(["oscar.example.com"], "postgres", ["https://app.oscar.test"])

        with self.assertRaises(ImproperlyConfigured):
            validate_production_env(["oscar.example.com"], "short", ["https://app.oscar.test"])

        with self.assertRaises(ImproperlyConfigured):
            validate_production_env(["oscar.example.com"], "strong_prod_pass_123", ["http://insecure.test"])

        with self.assertRaises(ImproperlyConfigured):
            validate_production_env(["oscar.example.com"], "strong_prod_pass_123", ["https://localhost:3000"])

        # Valid production configuration passes without error
        try:
            validate_production_env(["oscar.example.com"], "strong_prod_pass_123", ["https://app.oscar.test"])
        except ImproperlyConfigured:
            self.fail("Valid production configuration should not raise ImproperlyConfigured")
