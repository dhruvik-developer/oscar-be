import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0027_batch_dyeing_colour"),
        ("jobwork", "0015_jobworkorder_purchase_order"),
        ("purchase", "0021_remove_vendorbill_bill_kind"),
    ]

    operations = [
        migrations.AlterField(
            model_name="purchaseorder",
            name="fabric_flow",
            field=models.TextField(
                blank=True,
                choices=[
                    ("FACTORY_TO_OSCAR", "Factory -> OSCAR Warehouse"),
                    ("OSCAR_TO_MILL", "OSCAR Warehouse -> Dyeing Mill -> OSCAR Warehouse"),
                    ("FACTORY_TO_MILL", "Factory -> Dyeing Mill -> OSCAR"),
                ],
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="goodsreceipt",
            name="dyeing_outward",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name="dyeing_receipts", to="jobwork.jwooutward",
            ),
        ),
        migrations.AddField(
            model_name="goodsreceiptline",
            name="source_batch",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name="+", to="inventory.batch",
            ),
        ),
    ]
