from django.db import migrations


FORWARD_SQL = """
CREATE OR REPLACE FUNCTION prevent_duplicate_active_sales_order_invoice_draft()
RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'UPDATE'
       AND NEW.status IS NOT DISTINCT FROM OLD.status
       AND NEW.sales_order_id IS NOT DISTINCT FROM OLD.sales_order_id
       AND NEW.deleted_at IS NOT DISTINCT FROM OLD.deleted_at
       AND NEW.posted_at IS NOT DISTINCT FROM OLD.posted_at THEN
        RETURN NEW;
    END IF;

    IF NEW.status = 'Draft'
       AND NEW.sales_order_id IS NOT NULL
       AND NEW.deleted_at IS NULL
       AND NEW.posted_at IS NULL THEN
        PERFORM 1 FROM sales_orders WHERE id = NEW.sales_order_id FOR UPDATE;
        IF EXISTS (
            SELECT 1
            FROM sales_invoices
            WHERE client_id = NEW.client_id
              AND sales_order_id = NEW.sales_order_id
              AND status = 'Draft'
              AND deleted_at IS NULL
              AND posted_at IS NULL
              AND id <> NEW.id
        ) THEN
            RAISE EXCEPTION 'An active draft invoice already exists for this sales order'
                USING ERRCODE = '23505',
                      CONSTRAINT = 'uq_active_sales_order_invoice_draft';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER prevent_duplicate_active_sales_order_invoice_draft
BEFORE INSERT OR UPDATE ON sales_invoices
FOR EACH ROW
EXECUTE FUNCTION prevent_duplicate_active_sales_order_invoice_draft();
"""

REVERSE_SQL = """
DROP TRIGGER IF EXISTS prevent_duplicate_active_sales_order_invoice_draft
    ON sales_invoices;
DROP FUNCTION IF EXISTS prevent_duplicate_active_sales_order_invoice_draft();
"""


class Migration(migrations.Migration):
    dependencies = [("sales", "0016_early_payment_discounts")]

    operations = [
        migrations.RunSQL(FORWARD_SQL, REVERSE_SQL),
    ]
