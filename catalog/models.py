from django.db import models
from django.conf import settings


class CatalogBatch(models.Model):
    SOURCE_KINDS = [
        ("organizer_v4", "Конкурсный каталог v4"),
    ]

    checksum = models.CharField(max_length=64, unique=True)
    source_label = models.CharField(max_length=100)
    source_kind = models.CharField(max_length=24, choices=SOURCE_KINDS)
    row_count = models.PositiveIntegerField()
    raw_source = models.BinaryField(null=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.get_source_kind_display()} ({self.row_count})"


class CatalogFamily(models.Model):
    """A display family, not a claim that every source row is one configuration."""

    batch = models.ForeignKey(CatalogBatch, on_delete=models.CASCADE, related_name="families")
    identity_key = models.CharField(max_length=64)
    external_id = models.CharField(max_length=100)
    name = models.CharField(max_length=300)
    company = models.CharField(max_length=300)
    kind = models.CharField(max_length=32)
    type_label = models.CharField(max_length=200, blank=True)
    subtype = models.CharField(max_length=200, blank=True)
    stage = models.CharField(max_length=32)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["batch", "identity_key"], name="unique_catalog_family_in_batch")
        ]
        ordering = ["name", "id"]

    def __str__(self):
        return self.name


class CatalogSourceRow(models.Model):
    batch = models.ForeignKey(CatalogBatch, on_delete=models.CASCADE, related_name="source_rows")
    family = models.ForeignKey(CatalogFamily, on_delete=models.CASCADE, related_name="source_rows")
    record_index = models.PositiveIntegerField()
    line_start = models.PositiveIntegerField()
    line_end = models.PositiveIntegerField()
    external_id = models.CharField(max_length=100)
    raw = models.JSONField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["batch", "record_index"], name="unique_catalog_source_record")
        ]
        ordering = ["record_index"]


class CatalogApplication(models.Model):
    source_row = models.OneToOneField(CatalogSourceRow, on_delete=models.CASCADE, related_name="application")
    scenario = models.TextField()
    cases = models.TextField()
    industry = models.CharField(max_length=200)
    market_potential_raw = models.CharField(max_length=100, blank=True)


class CatalogOffer(models.Model):
    source_row = models.OneToOneField(CatalogSourceRow, on_delete=models.CASCADE, related_name="offer")
    region = models.CharField(max_length=300)
    price_raw = models.CharField(max_length=100, blank=True)
    price_value = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    price_status = models.CharField(max_length=20)
    currency_status = models.CharField(max_length=20, default="unknown")
    vat_note = models.CharField(max_length=100, default="Статус НДС не подтверждён источником")


class CatalogSpecification(models.Model):
    source_row = models.OneToOneField(CatalogSourceRow, on_delete=models.CASCADE, related_name="specification")
    attributes = models.JSONField(default=dict)
    status = models.CharField(max_length=20, default="missing")


class CatalogEvidenceBatch(models.Model):
    catalog_batch = models.ForeignKey(CatalogBatch, on_delete=models.PROTECT, related_name="evidence_batches")
    checksum = models.CharField(max_length=64, unique=True)
    source_label = models.CharField(max_length=100)
    claim_count = models.PositiveIntegerField()
    raw_source = models.BinaryField(null=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]


class CatalogEvidenceClaim(models.Model):
    evidence_batch = models.ForeignKey(CatalogEvidenceBatch, on_delete=models.PROTECT, related_name="claims")
    claim_id = models.CharField(max_length=120)
    object_slug = models.CharField(max_length=32)
    attribute = models.CharField(max_length=120)
    value = models.JSONField(null=True, blank=True)
    unit = models.CharField(max_length=80)
    scope = models.TextField()
    status = models.CharField(max_length=40)
    use = models.CharField(max_length=40)
    source_url = models.URLField(max_length=1000)
    source_sha256 = models.CharField(max_length=64)
    source_fetched_at_utc = models.DateTimeField()
    catalog_rows = models.ManyToManyField(CatalogSourceRow, related_name="evidence_claims", blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["evidence_batch", "claim_id"], name="unique_evidence_claim_in_batch"),
        ]
        ordering = ["claim_id"]


class CatalogImportEvent(models.Model):
    """Private audit of an accepted source upload, including repeated imports."""

    SOURCE_KINDS = [
        ("catalog", "Каталог"),
        ("evidence", "Первичные сведения"),
    ]

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="catalog_import_events",
    )
    actor_name = models.CharField(max_length=150)
    source_kind = models.CharField(max_length=16, choices=SOURCE_KINDS)
    checksum = models.CharField(max_length=64)
    source_label = models.CharField(max_length=100)
    rights_basis = models.CharField(max_length=500)
    created_new = models.BooleanField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]


class CatalogPublication(models.Model):
    """The evidence revision currently shown to new visitors and projects."""

    key = models.CharField(primary_key=True, max_length=16, default="storefront", editable=False)
    evidence_batch = models.ForeignKey(CatalogEvidenceBatch, on_delete=models.PROTECT)
    selected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
    )
    selected_at = models.DateTimeField(auto_now=True)


class CatalogPublicationEvent(models.Model):
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
    )
    actor_name = models.CharField(max_length=150)
    previous_checksum = models.CharField(max_length=64, blank=True)
    evidence_batch = models.ForeignKey(CatalogEvidenceBatch, on_delete=models.PROTECT)
    reason = models.CharField(max_length=500)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]


class SupplementBatch(models.Model):
    """Immutable manufacturer supplement, independent of the organizer CSV."""

    checksum = models.CharField(max_length=64, unique=True)
    source_label = models.CharField(max_length=100)
    rights_basis = models.CharField(max_length=500)
    raw_source = models.BinaryField(editable=False)
    product_count = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)


class SupplementSourceArtifact(models.Model):
    batch = models.ForeignKey(SupplementBatch, on_delete=models.PROTECT, related_name="source_artifacts")
    source_ref = models.CharField(max_length=100)
    url = models.URLField(max_length=1000)
    checksum = models.CharField(max_length=64)
    fetched_at_utc = models.DateTimeField()
    content_type = models.CharField(max_length=32)
    raw_source = models.BinaryField(editable=False)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["batch", "source_ref"], name="unique_supplement_source_ref",
        )]


class SupplementProduct(models.Model):
    batch = models.ForeignKey(SupplementBatch, on_delete=models.PROTECT, related_name="products")
    product_ref = models.CharField(max_length=100)
    manufacturer = models.CharField(max_length=300)
    model = models.CharField(max_length=300)
    variant = models.CharField(max_length=300)
    product_url = models.URLField(max_length=1000)
    product_source = models.ForeignKey(SupplementSourceArtifact, on_delete=models.PROTECT)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["batch", "product_ref"], name="unique_supplement_product_ref",
        )]


class SupplementApplication(models.Model):
    product = models.ForeignKey(SupplementProduct, on_delete=models.PROTECT, related_name="applications")
    object_slug = models.CharField(max_length=32)
    process_code = models.CharField(max_length=80)
    source = models.ForeignKey(SupplementSourceArtifact, on_delete=models.PROTECT)
    source_page = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["product", "process_code"], name="unique_supplement_application",
        )]


class SupplementSpecification(models.Model):
    product = models.ForeignKey(SupplementProduct, on_delete=models.PROTECT, related_name="specifications")
    attribute = models.CharField(max_length=120)
    value = models.DecimalField(max_digits=18, decimal_places=4)
    unit = models.CharField(max_length=32)
    source_value = models.CharField(max_length=100)
    source_unit = models.CharField(max_length=32)
    source = models.ForeignKey(SupplementSourceArtifact, on_delete=models.PROTECT)
    source_page = models.PositiveIntegerField(null=True, blank=True)
    status = models.CharField(max_length=32)

    @property
    def use(self):
        return "blocked" if self.status == "conflicted" else "matching_limit"

    @property
    def source_url(self):
        return self.source.url


class SupplementOffer(models.Model):
    KIND_CHOICES = [
        ("", "Не указан"),
        ("purchase", "Покупка"),
        ("raas", "RaaS"),
        ("rental", "Аренда"),
    ]
    product = models.ForeignKey(SupplementProduct, on_delete=models.PROTECT, related_name="offers")
    offer_ref = models.CharField(max_length=100)
    kind = models.CharField(max_length=16, choices=KIND_CHOICES, default="")
    price_basis = models.CharField(max_length=100, default="")
    scope = models.CharField(max_length=1000, default="")
    price = models.DecimalField(max_digits=18, decimal_places=2)
    currency = models.CharField(max_length=3)
    vat_status = models.CharField(max_length=80)
    valid_from = models.DateField()
    valid_until = models.DateField()
    source = models.ForeignKey(SupplementSourceArtifact, on_delete=models.PROTECT)
    source_page = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["product", "offer_ref"], name="unique_supplement_offer_ref",
        )]


class SupplementPublication(models.Model):
    key = models.CharField(primary_key=True, max_length=16, default="storefront", editable=False)
    batch = models.ForeignKey(SupplementBatch, on_delete=models.PROTECT)
    selected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
    )
    selected_at = models.DateTimeField(auto_now=True)


class SupplementAuditEvent(models.Model):
    KIND_CHOICES = [("import", "Импорт"), ("publish", "Публикация")]
    kind = models.CharField(max_length=16, choices=KIND_CHOICES)
    batch = models.ForeignKey(SupplementBatch, on_delete=models.PROTECT)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
    )
    actor_name = models.CharField(max_length=150)
    reason = models.CharField(max_length=500)
    previous_checksum = models.CharField(max_length=64, blank=True)
    created_new = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
