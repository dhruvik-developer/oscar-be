from django.apps import AppConfig


class PmsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.pms"
    label = "pms"
    verbose_name = "Project Management"
