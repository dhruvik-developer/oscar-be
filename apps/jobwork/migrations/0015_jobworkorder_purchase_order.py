import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("purchase", "0021_remove_vendorbill_bill_kind"),
        ("jobwork", "0014_service_orders"),
    ]

    operations = [
        migrations.AddField(
            model_name="jobworkorder",
            name="purchase_order",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name="dyeing_job_orders", to="purchase.purchaseorder",
            ),
        ),
    ]
