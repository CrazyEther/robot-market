import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class Project(models.Model):
    OBJECT_TYPES = [
        ("warehouse", "Склад"),
        ("airport", "Аэропорт"),
        ("hospital", "Медицинское учреждение"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="robot_projects"
    )
    name = models.CharField(max_length=100)
    object_slug = models.CharField(max_length=32, choices=OBJECT_TYPES)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.name


class ProjectRevision(models.Model):
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="revisions"
    )
    number = models.PositiveIntegerField()
    scenario_snapshot = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["project", "number"], name="unique_project_revision"
            )
        ]
        ordering = ["-number"]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Сохранённую ревизию нельзя изменять.")
        super().save(*args, **kwargs)
