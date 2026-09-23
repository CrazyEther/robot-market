from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = []
    operations = [
        migrations.CreateModel(
            name="DemoScenario",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("slug", models.SlugField(max_length=32, unique=True)),
                ("title", models.CharField(max_length=100)),
                ("process", models.CharField(max_length=200)),
                ("unit", models.CharField(max_length=60)),
                ("topology", models.JSONField()),
                ("parameters", models.JSONField()),
                ("constraint", models.CharField(max_length=400)),
                ("data_status", models.CharField(default="assumption", max_length=32)),
            ],
            options={"ordering": ["slug"]},
        ),
    ]
