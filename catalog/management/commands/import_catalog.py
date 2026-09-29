from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError

from catalog.importing import CatalogImportError, MAX_CATALOG_BYTES, import_catalog


class Command(BaseCommand):
    help = "Import a v4-format catalog from a permitted local file without copying it into Git"

    def add_arguments(self, parser):
        parser.add_argument("path", type=Path)
        parser.add_argument("--expected-sha256", required=True)
        parser.add_argument(
            "--source-kind", choices=("organizer_v4",),
            default="organizer_v4",
        )

    def handle(self, *args, **options):
        path = options["path"]
        try:
            if not path.is_file():
                raise CommandError("Файл каталога не найден")
            with path.open("rb") as source:
                content = source.read(MAX_CATALOG_BYTES + 1)
            if len(content) > MAX_CATALOG_BYTES:
                raise CommandError("Каталог превышает предел 10 МБ")
            batch, created = import_catalog(
                content, source_label=path.name,
                source_kind=options["source_kind"],
                expected_checksum=options["expected_sha256"],
            )
        except CatalogImportError as exc:
            raise CommandError(str(exc)) from None
        except OSError:
            raise CommandError("Не удалось прочитать файл каталога") from None
        except DatabaseError:
            raise CommandError("Не удалось записать каталог; транзакция отменена") from None
        action = "загружен" if created else "уже загружен"
        self.stdout.write(f"Каталог {action}: batch={batch.pk}, записей={batch.row_count}")
