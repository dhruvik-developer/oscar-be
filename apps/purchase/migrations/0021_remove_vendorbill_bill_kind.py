from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("purchase", "0020_dyeing_bill"),
    ]

    operations = [
        migrations.RemoveField(model_name="vendorbill", name="bill_kind"),
    ]
