from django.db import migrations


def lift(apps, schema_editor):
    """Mirror broker / salesperson terms from ``custom_values["fields"]`` to the
    top level for leads saved before ``Lead.save`` started doing it."""
    from apps.crm.models import lift_attribution

    Lead = apps.get_model("crm", "Lead")
    for lead in Lead.objects.filter(custom_values__has_key="fields").only("id", "custom_values"):
        lifted = lift_attribution(lead.custom_values)
        if lifted != lead.custom_values:
            Lead.objects.filter(pk=lead.pk).update(custom_values=lifted)


class Migration(migrations.Migration):
    dependencies = [("crm", "0020_deal_line_lead_product")]

    operations = [migrations.RunPython(lift, migrations.RunPython.noop)]
