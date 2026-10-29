"""Turn the Products picked on existing leads' forms into fabric requirements.

    python manage.py sync_lead_form_products            # all workspaces
    python manage.py sync_lead_form_products --dry-run  # only count

New and edited leads do this on save (``services.sync_form_products``); this
catches leads saved before that existed. Add-only and safe to run twice.
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.crm import services
from apps.crm.models import Lead


class Command(BaseCommand):
    help = "Add lead-form Products as fabric requirements on existing leads."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Count only; write nothing.")

    def handle(self, *args, dry_run=False, **options):
        leads = Lead.objects.filter(deleted_at__isnull=True, custom_values__has_key="products")
        added = touched = 0
        for lead in leads.iterator():
            with transaction.atomic():
                rows = services.sync_form_products(lead)
                if dry_run:
                    transaction.set_rollback(True)
            if rows:
                touched += 1
                added += len(rows)
        verb = "Would add" if dry_run else "Added"
        self.stdout.write(self.style.SUCCESS(f"{verb} {added} requirement(s) on {touched} lead(s)."))
