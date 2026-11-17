# Records the type of discount granted with a customer receipt (Early Payment,
# Special, Settlement or Other Approved). Historical rows are all early-payment
# discounts, so the default keeps their meaning intact.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0022_alter_deliverychallan_status"),
    ]

    operations = [
        migrations.AddField(
            model_name="earlypaymentdiscountrequest",
            name="discount_type",
            field=models.TextField(
                choices=[
                    ("Early Payment Discount", "Early Payment Discount"),
                    ("Special Discount", "Special Discount"),
                    ("Settlement Discount", "Settlement Discount"),
                    ("Other Approved Discount", "Other Approved Discount"),
                ],
                default="Early Payment Discount",
            ),
        ),
    ]
