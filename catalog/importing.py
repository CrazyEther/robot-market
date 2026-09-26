"""Lossless source-row import with separate family, application and offer records."""

import csv
import hashlib
import io
import json
import re
from decimal import Decimal, InvalidOperation

from django.db import transaction

from catalog.limits import MAX_CATALOG_BYTES
from catalog.models import (
    CatalogApplication, CatalogBatch, CatalogFamily, CatalogOffer,
    CatalogSourceRow, CatalogSpecification,
)


HEADERS = (
    "id", "Название", "тип", "статус", "компания", "описание", "Тип",
    "Подтип", "Сценарий", "Кейсы", "УГТ", "Рын Потенциал", "Регион",
    "Отрасль", "Цена изделия",
)
PRICE_PATTERN = re.compile(r"^\d+(?:\.\d{1,2})?$")
FIELD_LIMITS = {
    "id": 100, "Название": 300, "тип": 32, "статус": 32,
    "компания": 300, "Тип": 200, "Подтип": 200,
    "Рын Потенциал": 100, "Регион": 300, "Отрасль": 200,
    "Цена изделия": 100,
}


class CatalogImportError(ValueError):
    pass


def parse_price(raw):
    if raw == "":
        return None
    cleaned = raw.replace(" ", "").replace("\u00a0", "").replace("\u202f", "").replace(",", ".")
    if not PRICE_PATTERN.fullmatch(cleaned):
        raise CatalogImportError("Цена имеет неверный числовой формат")
    try:
        price = Decimal(cleaned)
    except InvalidOperation:
        raise CatalogImportError("Цена имеет неверный числовой формат") from None
    if price >= Decimal("10000000000000000"):
        raise CatalogImportError("Цена превышает поддерживаемый диапазон")
    return price


def parse_catalog(content):
    if len(content) > MAX_CATALOG_BYTES:
        raise CatalogImportError("Каталог превышает предел 10 МБ")
    try:
        decoded = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise CatalogImportError("Каталог должен быть в UTF-8") from None
    reader = csv.DictReader(io.StringIO(decoded, newline=""), delimiter=";", strict=True)
    if tuple(reader.fieldnames or ()) != HEADERS:
        raise CatalogImportError("Заголовки каталога не соответствуют контракту v4")
    records = []
    try:
        while True:
            before = reader.line_num
            try:
                raw = next(reader)
            except StopIteration:
                break
            index = len(records) + 1
            if None in raw or any(raw[key] is None for key in HEADERS):
                raise CatalogImportError(f"Запись {index}: неверное число колонок")
            for field, limit in FIELD_LIMITS.items():
                if len(raw[field]) > limit:
                    raise CatalogImportError(f"Запись {index}: поле «{field}» длиннее {limit} символов")
            if not raw["id"].strip() or not raw["Название"].strip() or not raw["компания"].strip():
                raise CatalogImportError(f"Запись {index}: нет ID, названия или компании")
            try:
                price = parse_price(raw["Цена изделия"])
            except CatalogImportError as exc:
                raise CatalogImportError(f"Запись {index}: {exc}") from None
            records.append({
                "index": index,
                "line_start": before + 1,
                "line_end": reader.line_num,
                "raw": raw,
                "price": price,
            })
    except csv.Error as exc:
        raise CatalogImportError(f"Некорректное CSV quoting: {exc}") from None
    if not records:
        raise CatalogImportError("Каталог пуст")
    return hashlib.sha256(content).hexdigest(), records


def _identity(raw):
    values = [raw[key].strip() for key in (
        "id", "Название", "компания", "тип", "статус", "Тип", "Подтип",
    )]
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode("utf-8")).hexdigest()


def import_catalog(content, *, source_label, source_kind, expected_checksum=None):
    """Validate every row before opening a transaction, then persist atomically."""
    if source_kind not in {choice for choice, _ in CatalogBatch.SOURCE_KINDS}:
        raise CatalogImportError("Неизвестный тип источника")
    checksum, records = parse_catalog(content)
    if expected_checksum is not None and checksum != expected_checksum.lower():
        raise CatalogImportError("Контрольная сумма каталога не совпадает с ожидаемой")
    if not source_label or len(source_label) > 100:
        raise CatalogImportError("Необходимо полное имя источника до 100 символов")
    with transaction.atomic():
        existing = CatalogBatch.objects.filter(checksum=checksum).first()
        if existing:
            if existing.source_kind != source_kind:
                raise CatalogImportError("Этот checksum уже связан с другим типом источника")
            if existing.raw_source is None:
                CatalogBatch.objects.filter(pk=existing.pk, raw_source__isnull=True).update(
                    raw_source=content,
                )
            return existing, False
        batch = CatalogBatch.objects.create(
            checksum=checksum, source_label=source_label,
            source_kind=source_kind, row_count=len(records), raw_source=content,
        )
        families = {}
        for record in records:
            raw = record["raw"]
            identity = _identity(raw)
            family = families.get(identity)
            if family is None:
                family = CatalogFamily.objects.create(
                    batch=batch, identity_key=identity,
                    external_id=raw["id"].strip(), name=raw["Название"].strip(),
                    company=raw["компания"].strip(), kind=raw["тип"].strip(),
                    type_label=raw["Тип"].strip(), subtype=raw["Подтип"].strip(),
                    stage=raw["статус"].strip(),
                )
                families[identity] = family
            source_row = CatalogSourceRow.objects.create(
                batch=batch, family=family, record_index=record["index"],
                line_start=record["line_start"], line_end=record["line_end"],
                external_id=raw["id"], raw=raw,
            )
            CatalogApplication.objects.create(
                source_row=source_row, scenario=raw["Сценарий"],
                cases=raw["Кейсы"], industry=raw["Отрасль"],
                market_potential_raw=raw["Рын Потенциал"],
            )
            CatalogOffer.objects.create(
                source_row=source_row, region=raw["Регион"],
                price_raw=raw["Цена изделия"], price_value=record["price"],
                price_status="missing" if record["price"] is None else "reported",
                vat_note="Статус НДС не подтверждён источником",
            )
            CatalogSpecification.objects.create(
                source_row=source_row, attributes={
                    field: {"value": None, "status": "missing", "source": "Нет подтверждённой ТТХ в CSV"}
                    for field in ("payload_kg", "width_m", "access_zones", "clean_transport", "lift_compatible")
                },
            )
    return batch, True
