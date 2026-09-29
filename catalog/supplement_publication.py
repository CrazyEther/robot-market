"""Controlled publication of immutable manufacturer supplement batches."""

from django.core.exceptions import PermissionDenied
from django.db import transaction

from catalog.models import SupplementAuditEvent, SupplementBatch, SupplementPublication
from catalog.supplement_importing import SupplementImportError, verify_imported_supplement


class SupplementPublicationError(ValueError):
    pass


def publish_supplement(*, checksum, actor, reason):
    if (actor is None or not actor.is_authenticated or not actor.is_staff
            or not actor.has_perm("catalog.change_supplementpublication")):
        raise PermissionDenied
    if not isinstance(reason, str) or not 20 <= len(reason.strip()) <= 500:
        raise SupplementPublicationError("Укажите основание публикации и право на метаданные")
    with transaction.atomic():
        batch = SupplementBatch.objects.filter(checksum=checksum).first()
        if batch is None:
            raise SupplementPublicationError("Версия дополнения не найдена")
        try:
            verify_imported_supplement(batch)
        except SupplementImportError as exc:
            raise SupplementPublicationError(
                "Сохранённые сведения дополнения не совпадают с первичными источниками"
            ) from exc
        previous = SupplementPublication.objects.select_for_update().filter(pk="storefront").first()
        old_checksum = previous.batch.checksum if previous else ""
        if old_checksum == checksum:
            return batch, False
        SupplementPublication.objects.update_or_create(
            pk="storefront", defaults={"batch": batch, "selected_by": actor},
        )
        SupplementAuditEvent.objects.create(
            kind="publish", batch=batch, actor=actor,
            actor_name=actor.get_username()[:150], reason=reason.strip(),
            previous_checksum=old_checksum,
        )
    return batch, True


def publicly_available_supplement(checksum):
    if not isinstance(checksum, str) or len(checksum) != 64:
        return None
    batch = SupplementBatch.objects.filter(checksum=checksum).first()
    if batch is None:
        return None
    if (SupplementPublication.objects.filter(pk="storefront", batch=batch).exists()
            or SupplementAuditEvent.objects.filter(kind="publish", batch=batch).exists()):
        try:
            verify_imported_supplement(batch)
        except SupplementImportError:
            return None
        return batch
    return None
