from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError

from catalog.evidence_importing import (
    EvidenceImportError, MAX_EVIDENCE_BYTES, import_evidence,
)


class Command(BaseCommand):
    help = "Import a verified primary-source claims file pinned to a catalog checksum"

    def add_arguments(self, parser):
        parser.add_argument("path", type=Path)
        parser.add_argument("--expected-sha256", required=True)

    def handle(self, *args, **options):
        path = options["path"]
        try:
            with path.open("rb") as source:
                content = source.read(MAX_EVIDENCE_BYTES + 1)
            batch, created = import_evidence(
                content, source_label=path.name,
                expected_checksum=options["expected_sha256"],
            )
        except OSError:
            raise CommandError("Не удалось прочитать реестр источников") from None
        except EvidenceImportError as exc:
            raise CommandError(str(exc)) from None
        except DatabaseError:
            raise CommandError("Не удалось записать реестр источников; транзакция отменена") from None
        action = "загружен" if created else "уже загружен"
        self.stdout.write(f"Реестр источников {action}: batch={batch.pk}, утверждений={batch.claim_count}")
