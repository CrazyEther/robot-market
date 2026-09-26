"""Which source-linked families can be shown as buyer-facing products."""

from django.db.models import Value
from django.db.models.functions import Coalesce, NullIf

from catalog.models import (
    CatalogBatch, CatalogEvidenceBatch, CatalogFamily, CatalogImportEvent,
    CatalogPublication, CatalogPublicationEvent,
)


def current_source_pair():
    """Return the published source pair; preserve pre-publication startup behavior."""
    publication = CatalogPublication.objects.select_related(
        "evidence_batch", "evidence_batch__catalog_batch",
    ).filter(pk="storefront").first()
    if publication:
        evidence = publication.evidence_batch
        return evidence.catalog_batch, evidence
    # Existing installations were pinned by the migration. Keep the CLI
    # bootstrap path, but an administrator upload must await publication.
    if CatalogImportEvent.objects.exists():
        return None, None
    batch = CatalogBatch.objects.filter(source_kind="organizer_v4").first()
    return batch, batch.evidence_batches.first() if batch else None


def publicly_available_evidence(checksum, batch):
    """Only the current or a previously published revision is public."""
    candidate = CatalogEvidenceBatch.objects.filter(
        checksum=checksum, catalog_batch=batch,
    ).first()
    if candidate is None:
        return None
    _, current = current_source_pair()
    if current and current.pk == candidate.pk:
        return candidate
    if CatalogPublicationEvent.objects.filter(evidence_batch=candidate).exists():
        return candidate
    if CatalogPublicationEvent.objects.filter(previous_checksum=checksum).exists():
        return candidate
    return None


def visible_families(batch, evidence_batch):
    return CatalogFamily.objects.filter(
        batch=batch,
        source_rows__evidence_claims__evidence_batch=evidence_batch,
        source_rows__evidence_claims__use__in=(
            "matching_limit", "offer_specific", "product_copy",
        ),
        source_rows__evidence_claims__value__isnull=False,
    ).distinct().annotate(
        display_category=Coalesce(NullIf("subtype", Value("")), "type_label"),
    )
