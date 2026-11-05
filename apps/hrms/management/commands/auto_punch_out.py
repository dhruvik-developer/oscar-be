"""
Management command: python manage.py auto_punch_out [--date YYYY-MM-DD] [--force]
Automatically punches out any employee who forgot to punch out at day end.
"""
from django.core.management.base import BaseCommand
from django.utils import timezone
from apps.hrms.services import auto_punch_out_pending_sessions
from datetime import datetime


class Command(BaseCommand):
    help = "Automatically punches out employees who forgot to punch out at day end"

    def add_arguments(self, parser):
        parser.add_argument("--date", type=str, help="Target date (YYYY-MM-DD), defaults to today")
        parser.add_argument("--force", action="store_true", help="Force punch out for today even before day end")

    def handle(self, *args, **options):
        as_of = None
        if options.get("date"):
            as_of = datetime.strptime(options["date"], "%Y-%m-%d").date()
        else:
            as_of = timezone.localdate()

        force = options.get("force", False)
        self.stdout.write(f"Running auto punch-out check (as_of={as_of}, force={force})...")
        closed = auto_punch_out_pending_sessions(as_of_date=as_of, force_today=force)
        self.stdout.write(self.style.SUCCESS(f"Successfully auto-punched out {len(closed)} open session(s)."))
