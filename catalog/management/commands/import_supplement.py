"""Archive a verified manufacturer supplement without publishing it."""

from pathlib import Path
from getpass import getuser

from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError

from catalog.supplement_importing import (
    MAX_MANIFEST_BYTES, MAX_SOURCE_BYTES, SupplementImportError, import_supplement,
)


class Command(BaseCommand):
    help = "Import a private, source-attested manufacturer supplement"

    def add_arguments(self, parser):
        parser.add_argument("manifest", type=Path)
        parser.add_argument("--expected-sha256", required=True)
        parser.add_argument("--source-asset", action="append", required=True,
                            help="source_ref=absolute_path; repeat for every referenced source")
        parser.add_argument("--rights-basis", required=True)

    def handle(self, *args, **options):
        try:
            with options["manifest"].open("rb") as source:
                content = source.read(MAX_MANIFEST_BYTES + 1)
            assets = {}
            for binding in options["source_asset"]:
                ref, separator, path = binding.partition("=")
                if not separator or not ref or not path or ref in assets:
                    raise SupplementImportError("Неверная или повторная привязка исходного файла")
                with Path(path).open("rb") as source:
                    assets[ref] = source.read(MAX_SOURCE_BYTES + 1)
            batch, created = import_supplement(
                content, expected_checksum=options["expected_sha256"],
                source_assets=assets, source_label=options["manifest"].name,
                rights_basis=options["rights_basis"], actor_name=getuser(),
            )
        except OSError:
            raise CommandError("Не удалось прочитать дополнение или первичный снимок") from None
        except (SupplementImportError, DatabaseError) as exc:
            raise CommandError(str(exc)) from None
        action = "сохранено" if created else "уже сохранено"
        self.stdout.write(
            f"Дополнение {action}: batch={batch.pk}, моделей={batch.product_count}; публикация не изменена"
        )
