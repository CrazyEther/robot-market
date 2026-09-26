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


class OperationLog(models.Model):
    """Immutable raw source and its PII-free normalized rows for one project."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="operation_logs")
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    process = models.CharField(max_length=64)
    sha256 = models.CharField(max_length=64)
    source_description = models.CharField(max_length=500)
    period_start_at = models.DateTimeField(null=True)
    period_end_at = models.DateTimeField(null=True)
    raw_csv = models.BinaryField()
    rows = models.JSONField()
    parser_version = models.PositiveSmallIntegerField()
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["project", "process", "sha256", "parser_version",
                    "period_start_at", "period_end_at"],
            name="unique_project_operation_log",
        )]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Сохранённый журнал нельзя изменять.")
        super().save(*args, **kwargs)


class AvailabilityPlan(models.Model):
    """Owner-attested per-robot operating, charging and downtime intervals."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="availability_plans")
    operation_log = models.ForeignKey(OperationLog, on_delete=models.CASCADE)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    process = models.CharField(max_length=64)
    robot_record_index = models.PositiveIntegerField(null=True)
    selection_key = models.CharField(max_length=64, null=True)
    fleet = models.PositiveIntegerField()
    sha256 = models.CharField(max_length=64)
    source_description = models.CharField(max_length=500)
    raw_csv = models.BinaryField()
    rows = models.JSONField()
    parser_version = models.PositiveSmallIntegerField()
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["project", "operation_log", "process", "robot_record_index",
                    "fleet", "sha256", "parser_version"],
            condition=models.Q(selection_key__isnull=True),
            name="unique_project_availability_plan",
        ), models.UniqueConstraint(
            fields=["project", "operation_log", "process", "selection_key",
                    "fleet", "sha256", "parser_version"],
            name="unique_project_availability_selection",
        ), models.CheckConstraint(
            condition=(models.Q(robot_record_index__isnull=False)
                       | models.Q(selection_key__isnull=False)),
            name="availability_has_robot_identity",
        )]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Сохранённый календарь нельзя изменять.")
        super().save(*args, **kwargs)


class SimulationRun(models.Model):
    """Immutable event result pinned to one revision and both raw sources."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="simulation_runs")
    revision = models.ForeignKey(ProjectRevision, on_delete=models.CASCADE)
    operation_log = models.ForeignKey(OperationLog, on_delete=models.CASCADE)
    availability_plan = models.ForeignKey(AvailabilityPlan, on_delete=models.CASCADE)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    ledger_version = models.PositiveSmallIntegerField()
    input_snapshot = models.JSONField()
    ledger = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["revision", "operation_log", "availability_plan", "ledger_version"],
            name="unique_simulation_run_inputs",
        )]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Сохранённый прогон нельзя изменять.")
        super().save(*args, **kwargs)


class FinancePlan(models.Model):
    """Immutable customer-approved cash-flow source bound to one observed run."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="finance_plans")
    simulation_run = models.ForeignKey(SimulationRun, on_delete=models.CASCADE, related_name="finance_plans")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    raw_csv = models.BinaryField()
    sha256 = models.CharField(max_length=64)
    source_filename = models.CharField(max_length=255, default="")
    metadata_sha256 = models.CharField(max_length=64)
    source_description = models.CharField(max_length=500)
    forecast_basis = models.CharField(max_length=1000)
    horizon_months = models.PositiveSmallIntegerField()
    monthly_discount_rate = models.DecimalField(max_digits=10, decimal_places=8)
    discount_rate_source = models.CharField(max_length=500)
    parser_version = models.PositiveSmallIntegerField()
    rows = models.JSONField()
    result = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["simulation_run", "sha256", "metadata_sha256",
                    "horizon_months", "monthly_discount_rate", "parser_version"],
            name="unique_finance_plan_inputs",
        )]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Сохранённый финансовый план нельзя изменять.")
        super().save(*args, **kwargs)


class FinanceVariant(models.Model):
    """One immutable, source-backed cash-line change to an imported plan."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    base_plan = models.ForeignKey(FinancePlan, on_delete=models.CASCADE, related_name="variants")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    source_row = models.PositiveIntegerField()
    amount = models.DecimalField(max_digits=22, decimal_places=4)
    source_date = models.DateField()
    source_ref = models.CharField(max_length=1000)
    checksum = models.CharField(max_length=64)
    result = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["base_plan", "checksum"], name="unique_finance_variant_inputs",
        )]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Сохранённый вариант нельзя изменять.")
        super().save(*args, **kwargs)
