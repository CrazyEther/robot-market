from django.db import models


class DemoScenario(models.Model):
    slug = models.SlugField(max_length=32, unique=True)
    title = models.CharField(max_length=100)
    process = models.CharField(max_length=200)
    unit = models.CharField(max_length=60)
    topology = models.JSONField()
    parameters = models.JSONField()
    constraint = models.CharField(max_length=400)
    data_status = models.CharField(max_length=32, default="assumption")

    class Meta:
        ordering = ["slug"]

    def __str__(self):
        return self.title
