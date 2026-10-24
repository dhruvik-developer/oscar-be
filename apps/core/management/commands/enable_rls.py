"""
Install row-level security policies (db.md §1.3).

    "Enable RLS on every tenant table with
     ``using (client_id = current_setting('app.client_id')::uuid)`` and set
     ``app.client_id`` per request from the JWT. The ORM filter is the first
     defence; RLS is the one that holds when someone forgets it."

``apps.core.tenancy`` already publishes ``app.client_id`` on every
authenticated request. This command adds the second half.

Run it as a role that owns the tables, and connect the application as a role
that does **not** -- a table owner bypasses RLS unless ``force row level
security`` is set, which this command sets for exactly that reason.
"""
from django.apps import apps as django_apps
from django.core.management.base import BaseCommand
from django.db import connection

#: Tables that are deliberately not tenant-scoped.
GLOBAL_TABLES = {"permissions", "django_migrations", "django_content_type"}


class Command(BaseCommand):
    help = "Enable row-level security on every tenant-scoped table."

    def add_arguments(self, parser):
        parser.add_argument(
            "--drop", action="store_true", help="Remove the policies instead."
        )
        parser.add_argument(
            "--dry-run", action="store_true", help="Print the SQL without running it."
        )

    def handle(self, *args, **options):
        if connection.vendor != "postgresql":
            self.stderr.write("Row-level security requires PostgreSQL.")
            return

        tables = sorted(self._tenant_tables())
        statements = []

        for table in tables:
            policy = f"tenant_isolation_{table}"
            if options["drop"]:
                statements.append(f'drop policy if exists "{policy}" on "{table}";')
                statements.append(f'alter table "{table}" no force row level security;')
                statements.append(f'alter table "{table}" disable row level security;')
                continue

            statements.append(f'alter table "{table}" enable row level security;')
            # Without FORCE, the table owner silently bypasses every policy --
            # which would make this whole command decorative.
            statements.append(f'alter table "{table}" force row level security;')
            statements.append(f'drop policy if exists "{policy}" on "{table}";')
            statements.append(
                f'create policy "{policy}" on "{table}" using ('
                "client_id = nullif(current_setting('app.client_id', true), '')::uuid"
                ") with check ("
                "client_id = nullif(current_setting('app.client_id', true), '')::uuid"
                ");"
            )

        if options["dry_run"]:
            for statement in statements:
                self.stdout.write(statement)
            self.stdout.write(f"\n-- {len(tables)} tenant table(s)")
            return

        with connection.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)

        verb = "Dropped" if options["drop"] else "Installed"
        self.stdout.write(
            self.style.SUCCESS(f"{verb} RLS policies on {len(tables)} table(s).")
        )
        if not options["drop"]:
            self.stdout.write(
                "Connect the application as a non-owner role, or the policies "
                "will still be bypassed on tables it owns."
            )

    def _tenant_tables(self):
        for model in django_apps.get_models():
            table = model._meta.db_table
            if table in GLOBAL_TABLES:
                continue
            if not any(
                field.attname == "client_id" for field in model._meta.concrete_fields
            ):
                continue
            yield table
