from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("purchase", "0018_vendorbill_other_charges"),
    ]

    operations = [
        migrations.AddField(
            model_name="purchaseorder",
            name="dyeing_colour_name",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="purchaseorder",
            name="dyeing_colour_hex",
            field=models.TextField(blank=True, null=True),
        ),
    ]
