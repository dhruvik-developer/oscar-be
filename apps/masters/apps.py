from django.apps import AppConfig


class MastersConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.masters"
    label = "masters"
    verbose_name = "Shared masters"
