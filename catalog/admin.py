"""Read-only inspection of immutable catalog versions and import history."""

from django.contrib import admin

from catalog.models import (
    CatalogBatch, CatalogEvidenceBatch, CatalogEvidenceClaim, CatalogImportEvent,
    CatalogOffer, CatalogPublication, CatalogPublicationEvent, CatalogSourceRow,
    SupplementApplication, SupplementAuditEvent, SupplementBatch, SupplementOffer,
    SupplementProduct, SupplementPublication, SupplementSourceArtifact,
    SupplementSpecification,
)


class ImmutableSourceAdmin(admin.ModelAdmin):
    actions = None

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(CatalogBatch)
class CatalogBatchAdmin(ImmutableSourceAdmin):
    list_display = ("checksum", "source_kind", "row_count", "created_at")
    fields = ("checksum", "source_label", "source_kind", "row_count", "created_at")
    readonly_fields = fields

    def get_queryset(self, request):
        return super().get_queryset(request).defer("raw_source")


@admin.register(CatalogEvidenceBatch)
class CatalogEvidenceBatchAdmin(ImmutableSourceAdmin):
    list_display = ("checksum", "catalog_batch", "claim_count", "created_at")
    fields = ("checksum", "catalog_batch", "source_label", "claim_count", "created_at")
    readonly_fields = fields

    def get_queryset(self, request):
        return super().get_queryset(request).defer("raw_source")


@admin.register(CatalogEvidenceClaim)
class CatalogEvidenceClaimAdmin(ImmutableSourceAdmin):
    list_display = ("claim_id", "attribute", "object_slug", "status", "use", "evidence_batch")
    search_fields = ("claim_id", "attribute")
    fields = (
        "evidence_batch", "claim_id", "object_slug", "attribute", "value", "unit",
        "scope", "status", "use", "source_url", "source_sha256", "source_fetched_at_utc",
    )
    readonly_fields = fields


@admin.register(CatalogSourceRow)
class CatalogSourceRowAdmin(ImmutableSourceAdmin):
    list_display = ("batch", "record_index", "external_id", "family")
    fields = ("batch", "family", "record_index", "line_start", "line_end", "external_id")
    readonly_fields = fields


@admin.register(CatalogOffer)
class CatalogOfferAdmin(ImmutableSourceAdmin):
    list_display = ("source_row", "price_value", "price_status", "currency_status")
    fields = ("source_row", "region", "price_raw", "price_value", "price_status",
              "currency_status", "vat_note")
    readonly_fields = fields


@admin.register(CatalogImportEvent)
class CatalogImportEventAdmin(ImmutableSourceAdmin):
    list_display = ("created_at", "actor_name", "source_kind", "checksum", "created_new")
    fields = ("created_at", "actor_name", "source_kind", "checksum", "source_label",
              "rights_basis", "created_new")
    readonly_fields = fields


@admin.register(CatalogPublication)
class CatalogPublicationAdmin(ImmutableSourceAdmin):
    list_display = ("key", "evidence_batch", "selected_by", "selected_at")
    fields = ("key", "evidence_batch", "selected_by", "selected_at")
    readonly_fields = fields


@admin.register(CatalogPublicationEvent)
class CatalogPublicationEventAdmin(ImmutableSourceAdmin):
    list_display = ("created_at", "actor_name", "evidence_batch", "previous_checksum")
    fields = ("created_at", "actor_name", "previous_checksum", "evidence_batch", "reason")
    readonly_fields = fields


@admin.register(SupplementBatch)
class SupplementBatchAdmin(ImmutableSourceAdmin):
    list_display = ("checksum", "source_label", "product_count", "created_at")
    fields = ("checksum", "source_label", "rights_basis", "product_count", "created_at")
    readonly_fields = fields

    def get_queryset(self, request):
        return super().get_queryset(request).defer("raw_source")


@admin.register(SupplementSourceArtifact)
class SupplementSourceArtifactAdmin(ImmutableSourceAdmin):
    list_display = ("source_ref", "url", "checksum", "fetched_at_utc")
    fields = ("batch", "source_ref", "url", "checksum", "fetched_at_utc", "content_type")
    readonly_fields = fields

    def get_queryset(self, request):
        return super().get_queryset(request).defer("raw_source")


@admin.register(SupplementProduct)
class SupplementProductAdmin(ImmutableSourceAdmin):
    list_display = ("product_ref", "manufacturer", "model", "variant", "batch")
    fields = ("batch", "product_ref", "manufacturer", "model", "variant",
              "product_url", "product_source")
    readonly_fields = fields


@admin.register(SupplementApplication)
class SupplementApplicationAdmin(ImmutableSourceAdmin):
    list_display = ("product", "object_slug", "process_code", "source")
    fields = ("product", "object_slug", "process_code", "source", "source_page")
    readonly_fields = fields


@admin.register(SupplementSpecification)
class SupplementSpecificationAdmin(ImmutableSourceAdmin):
    list_display = ("product", "attribute", "value", "unit", "status")
    fields = ("product", "attribute", "value", "unit", "source_value", "source_unit",
              "source", "source_page", "status")
    readonly_fields = fields


@admin.register(SupplementOffer)
class SupplementOfferAdmin(ImmutableSourceAdmin):
    list_display = ("product", "offer_ref", "kind", "price", "currency", "valid_until")
    fields = ("product", "offer_ref", "kind", "price_basis", "scope",
              "price", "currency", "vat_status",
              "valid_from", "valid_until", "source", "source_page")
    readonly_fields = fields


@admin.register(SupplementPublication)
class SupplementPublicationAdmin(ImmutableSourceAdmin):
    list_display = ("key", "batch", "selected_by", "selected_at")
    fields = ("key", "batch", "selected_by", "selected_at")
    readonly_fields = fields


@admin.register(SupplementAuditEvent)
class SupplementAuditEventAdmin(ImmutableSourceAdmin):
    list_display = ("created_at", "kind", "batch", "actor_name", "created_new")
    fields = ("created_at", "kind", "batch", "actor", "actor_name", "reason",
              "previous_checksum", "created_new")
    readonly_fields = fields
