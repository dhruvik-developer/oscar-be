from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.accounts"
    label = "accounts"
    verbose_name = "Tenants, users and access control"

    def ready(self):
        # Registers the bearer-auth description for the OpenAPI document.
        from . import schema  # noqa: F401
        from django.db.models.signals import post_migrate

        def _on_post_migrate(sender, **kwargs):
            if sender.name == "apps.accounts":
                try:
                    from .permission_catalogue import sync_permissions
                    sync_permissions()
                except Exception:
                    pass

        post_migrate.connect(_on_post_migrate, dispatch_uid="accounts.sync_permissions_on_migrate")
