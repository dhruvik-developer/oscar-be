from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0026_reprocessrecord_reprocessreturn_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="batch",
            name="dyeing_colour_name",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="batch",
            name="dyeing_colour_hex",
            field=models.TextField(blank=True, null=True),
        ),
    ]
