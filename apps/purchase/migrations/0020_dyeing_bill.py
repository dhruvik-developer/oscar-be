import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0027_batch_dyeing_colour"),
        ("purchase", "0019_purchaseorder_dyeing_colour"),
    ]

    operations = [
        migrations.AddField(
            model_name="purchaseorder",
            name="dyeing_rate",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=18, null=True),
        ),
        migrations.AddField(
            model_name="vendorbill",
            name="bill_kind",
            field=models.TextField(choices=[("FABRIC", "FABRIC"), ("DYEING", "DYEING")], default="FABRIC"),
        ),
        migrations.AddField(
            model_name="purchaseorderexpense",
            name="batch",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name="+", to="inventory.batch",
            ),
        ),
        migrations.AddField(
            model_name="purchaseorderexpense",
            name="vendor_bill",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name="dyeing_expenses", to="purchase.vendorbill",
            ),
        ),
    ]
