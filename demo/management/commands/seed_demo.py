from django.core.management.base import BaseCommand

from demo.models import DemoScenario
from demo.scenarios import SCENARIOS


class Command(BaseCommand):
    help = "Create the three independent, synthetic demonstration scenarios."

    def handle(self, *args, **kwargs):
        for scenario in SCENARIOS:
            slug = scenario["slug"]
            DemoScenario.objects.get_or_create(
                slug=slug,
                defaults={**scenario, "data_status": "assumption"},
            )
        self.stdout.write(self.style.SUCCESS("Проверено наличие 3 синтетических сценариев"))
