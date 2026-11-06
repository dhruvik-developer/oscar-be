import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0027_batch_dyeing_colour"),
        ("purchase", "0022_dyeing_return"),
    ]

    operations = [
        migrations.AddField(
            model_name="purchaseorder",
            name="source_purchase_order",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name="dyeing_orders", to="purchase.purchaseorder",
            ),
        ),
        migrations.AddField(
            model_name="purchaseorderline",
            name="source_batch",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name="+", to="inventory.batch",
            ),
        ),
    ]
