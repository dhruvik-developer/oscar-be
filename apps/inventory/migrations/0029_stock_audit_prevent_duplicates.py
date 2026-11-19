from django.db import migrations

TRIGGER_SQL = """
CREATE OR REPLACE FUNCTION check_duplicate_active_stock_audit()
RETURNS TRIGGER AS $func$
BEGIN
    IF NEW.status IN ('Draft', 'Counting') AND NEW.deleted_at IS NULL THEN
        IF EXISTS (
            SELECT 1 FROM stock_audits
            WHERE client_id = NEW.client_id
              AND (
                  (location_id IS NULL AND NEW.location_id IS NULL)
                  OR (location_id = NEW.location_id)
              )
              AND period_month = NEW.period_month
              AND status IN ('Draft', 'Counting')
              AND deleted_at IS NULL
              AND id != NEW.id
        ) THEN
            RAISE EXCEPTION 'ACTIVE_AUDIT_EXISTS: An active audit cycle already exists for this warehouse and period.';
        END IF;
    END IF;
    RETURN NEW;
END;
$func$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_check_duplicate_active_stock_audit ON stock_audits;
CREATE TRIGGER trg_check_duplicate_active_stock_audit
BEFORE INSERT OR UPDATE ON stock_audits
FOR EACH ROW
EXECUTE FUNCTION check_duplicate_active_stock_audit();
"""

REVERSE_SQL = """
DROP TRIGGER IF EXISTS trg_check_duplicate_active_stock_audit ON stock_audits;
DROP FUNCTION IF EXISTS check_duplicate_active_stock_audit();
"""

class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0028_trip_from_warehouse_trip_is_stock_deducted_and_more'),
    ]

    operations = [
        migrations.RunSQL(TRIGGER_SQL, reverse_sql=REVERSE_SQL),
    ]
