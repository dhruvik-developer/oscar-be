import importlib

from django.db import migrations

_draft_guard = importlib.import_module(
    "apps.sales.migrations.0017_prevent_duplicate_active_order_invoice_drafts"
)


# One live (not deleted, not Cancelled) invoice and one live challan per sales
# order. A trigger rather than a unique index so rows that predate the rule are
# left alone: it only fires when a row *becomes* live on an order (insert,
# re-link, un-delete, un-cancel), never on ordinary updates to an existing one.
def _guard(table, label):
    return f"""
CREATE OR REPLACE FUNCTION prevent_second_{table}_per_sales_order()
RETURNS trigger AS $$
BEGIN
    IF NEW.sales_order_id IS NULL
       OR NEW.deleted_at IS NOT NULL
       OR NEW.status = 'Cancelled' THEN
        RETURN NEW;
    END IF;

    IF TG_OP = 'UPDATE'
       AND NEW.sales_order_id IS NOT DISTINCT FROM OLD.sales_order_id
       AND OLD.deleted_at IS NULL
       AND OLD.status IS DISTINCT FROM 'Cancelled' THEN
        RETURN NEW;
    END IF;

    PERFORM 1 FROM sales_orders WHERE id = NEW.sales_order_id FOR UPDATE;
    IF EXISTS (
        SELECT 1
        FROM {table}
        WHERE client_id = NEW.client_id
          AND sales_order_id = NEW.sales_order_id
          AND deleted_at IS NULL
          AND status <> 'Cancelled'
          AND id <> NEW.id
    ) THEN
        RAISE EXCEPTION 'This sales order already has a {label}'
            USING ERRCODE = '23505',
                  CONSTRAINT = 'uq_one_{table}_per_sales_order';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER prevent_second_{table}_per_sales_order
BEFORE INSERT OR UPDATE ON {table}
FOR EACH ROW
EXECUTE FUNCTION prevent_second_{table}_per_sales_order();
"""


def _drop(table):
    return f"""
DROP TRIGGER IF EXISTS prevent_second_{table}_per_sales_order ON {table};
DROP FUNCTION IF EXISTS prevent_second_{table}_per_sales_order();
"""


# 0017's draft-only invoice guard is a subset of the new one.
DROP_DRAFT_GUARD = """
DROP TRIGGER IF EXISTS prevent_duplicate_active_sales_order_invoice_draft
    ON sales_invoices;
DROP FUNCTION IF EXISTS prevent_duplicate_active_sales_order_invoice_draft();
"""


class Migration(migrations.Migration):
    dependencies = [("sales", "0017_prevent_duplicate_active_order_invoice_drafts")]

    operations = [
        migrations.RunSQL(
            DROP_DRAFT_GUARD + _guard("sales_invoices", "sales invoice"),
            _drop("sales_invoices") + _draft_guard.FORWARD_SQL,
        ),
        migrations.RunSQL(
            _guard("delivery_challans", "delivery challan"),
            _drop("delivery_challans"),
        ),
    ]
