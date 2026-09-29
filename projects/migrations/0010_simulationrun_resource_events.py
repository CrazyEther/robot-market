from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("projects", "0009_availabilityplan_selection_key_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="simulationrun",
            name="resource_events",
            field=models.JSONField(default=list),
        ),
    ]
