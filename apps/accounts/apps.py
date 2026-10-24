from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.accounts"
    label = "accounts"
    verbose_name = "Tenants, users and access control"

    def ready(self):
        # Registers the bearer-auth description for the OpenAPI document.
        from . import schema  # noqa: F401
