"""Import an attested manufacturer supplement with its exact source bytes."""

import hashlib
import json
import re
from collections import Counter
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from django.db import transaction

from catalog.limits import (
    MAX_SUPPLEMENT_MANIFEST_BYTES, MAX_SUPPLEMENT_SOURCE_BYTES,
    MAX_SUPPLEMENT_TOTAL_SOURCE_BYTES,
)
from catalog.models import (
    SupplementApplication, SupplementAuditEvent, SupplementBatch, SupplementOffer,
    SupplementProduct, SupplementSourceArtifact, SupplementSpecification,
)
from projects.task_profiles import process_for


MAX_MANIFEST_BYTES = MAX_SUPPLEMENT_MANIFEST_BYTES
MAX_SOURCE_BYTES = MAX_SUPPLEMENT_SOURCE_BYTES
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
REF = re.compile(r"[a-z0-9][a-z0-9_.-]{0,99}\Z")
MONEY = re.compile(r"(?:0|[1-9][0-9]{0,15})(?:\.[0-9]{1,2})?\Z")
NUMBER = re.compile(r"(?:0|[1-9][0-9]{0,13})(?:\.[0-9]{1,4})?\Z")
UNITS = {
    "kg": ("kg", Decimal(1)),
    "t": ("kg", Decimal(1000)),
    "mm": ("mm", Decimal(1)),
    "cm": ("mm", Decimal(10)),
    "m/s": ("m/s", Decimal(1)),
    "cm/s": ("m/s", Decimal("0.01")),
    "N": ("N", Decimal(1)),
    "daN": ("N", Decimal(10)),
}
ATTRIBUTE_UNITS = {
    "payload_kg": "kg",
    "tow_mass_kg": "kg",
    "max_cart_length_mm": "mm",
    "max_cart_width_mm": "mm",
    "minimum_passage_mm": "mm",
    "turning_diameter_mm": "mm",
    "manufacturer_max_speed_m_s": "m/s",
    "drawbar_pull_n": "N",
}
CONTENT_TYPES = {"text/html", "application/pdf"}


class SupplementImportError(ValueError):
    pass


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise SupplementImportError(f"Повторено поле {key}")
        result[key] = value
    return result


def _fields(value, required, label):
    if not isinstance(value, dict) or set(value) != set(required):
        raise SupplementImportError(f"{label}: неверный состав полей")


def _text(value, label, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or value != value.strip():
        raise SupplementImportError(f"{label}: требуется непустой текст до {limit} символов")
    return value


def _ref(value, label):
    if not isinstance(value, str) or not REF.fullmatch(value):
        raise SupplementImportError(f"{label}: неверный идентификатор")
    return value


def _sha(value, label):
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise SupplementImportError(f"{label}: неверный SHA-256")
    return value


def _url(value, label):
    _text(value, label, 1000)
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise SupplementImportError(f"{label}: неверный HTTPS-адрес") from None
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.fragment or port == 0):
        raise SupplementImportError(f"{label}: требуется прямой HTTPS-адрес")
    return value


def _page(value, source, label):
    if value is None and source["content_type"] == "text/html":
        return None
    if type(value) is not int or value < 1 or value > 10000:
        raise SupplementImportError(f"{label}: нужна страница первичного PDF")
    return value


def _source(value, sources, label):
    ref = _ref(value, label)
    if ref not in sources:
        raise SupplementImportError(f"{label}: источник не найден")
    return sources[ref]


def _decimal(value, pattern, label):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise SupplementImportError(f"{label}: неверный десятичный формат")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise SupplementImportError(f"{label}: неверный десятичный формат") from None
    if not number.is_finite():
        raise SupplementImportError(f"{label}: число должно быть конечным")
    return number


def parse_supplement(content, *, expected_checksum, source_assets):
    if not isinstance(content, bytes) or not 0 < len(content) <= MAX_MANIFEST_BYTES:
        raise SupplementImportError("Файл дополнения пуст или превышает предел 2 МБ")
    checksum = hashlib.sha256(content).hexdigest()
    if checksum != _sha(expected_checksum, "Ожидаемая контрольная сумма"):
        raise SupplementImportError("Контрольная сумма дополнения не совпадает")
    try:
        document = json.loads(content.decode("utf-8-sig"), object_pairs_hook=_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SupplementImportError("Дополнение должно быть корректным UTF-8 JSON") from exc
    _fields(document, ("schema_version", "source_kind", "sources", "products"), "Дополнение")
    if type(document["schema_version"]) is not int or document["schema_version"] not in (1, 2):
        raise SupplementImportError("Неподдерживаемая версия формата дополнения")
    if document["source_kind"] != "manufacturer_supplement":
        raise SupplementImportError("Дополнение не является независимым источником производителей")
    if not isinstance(document["sources"], list) or not document["sources"]:
        raise SupplementImportError("Нет первичных снимков")
    if not isinstance(document["products"], list) or not document["products"]:
        raise SupplementImportError("Нет моделей")
    if len(document["sources"]) > 100 or len(document["products"]) > 1000:
        raise SupplementImportError("Слишком много источников или моделей")
    if not isinstance(source_assets, dict):
        raise SupplementImportError("Нужны байты всех первичных снимков")

    sources = {}
    total_source_bytes = 0
    for raw in document["sources"]:
        _fields(raw, ("source_ref", "url", "sha256", "fetched_at_utc", "content_type"), "Источник")
        ref = _ref(raw["source_ref"], "Источник")
        if ref in sources:
            raise SupplementImportError("Повторён источник")
        url = _url(raw["url"], "URL источника")
        sha = _sha(raw["sha256"], "SHA источника")
        try:
            fetched = datetime.fromisoformat(raw["fetched_at_utc"].replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError):
            raise SupplementImportError("Неверное время получения источника") from None
        if fetched.tzinfo is None or fetched.utcoffset().total_seconds() != 0:
            raise SupplementImportError("Время источника должно быть в UTC")
        media_type = raw["content_type"]
        if media_type not in CONTENT_TYPES:
            raise SupplementImportError("Неподдерживаемый тип первичного снимка")
        asset = source_assets.get(ref)
        if not isinstance(asset, bytes) or not 0 < len(asset) <= MAX_SOURCE_BYTES:
            raise SupplementImportError("Первичный снимок не передан или превышает предел")
        total_source_bytes += len(asset)
        if total_source_bytes > MAX_SUPPLEMENT_TOTAL_SOURCE_BYTES:
            raise SupplementImportError("Общий объём первичных снимков превышает предел")
        if hashlib.sha256(asset).hexdigest() != sha:
            raise SupplementImportError("Первичный снимок не совпадает с SHA-256")
        if media_type == "application/pdf" and not asset.startswith(b"%PDF-"):
            raise SupplementImportError("Первичный PDF имеет неверную сигнатуру")
        sources[ref] = {"source_ref": ref, "url": url, "checksum": sha,
                        "fetched_at_utc": fetched, "content_type": media_type,
                        "raw_source": asset}
    if set(source_assets) != set(sources):
        raise SupplementImportError("Переданы лишние или неучтённые исходные файлы")

    products = []
    refs, identities = set(), set()
    for raw in document["products"]:
        _fields(raw, ("product_ref", "manufacturer", "model", "variant", "product_url",
                      "product_source_ref", "applications", "specifications", "offers"), "Модель")
        ref = _ref(raw["product_ref"], "Модель")
        maker = _text(raw["manufacturer"], "Производитель", 300)
        model = _text(raw["model"], "Модель", 300)
        variant = _text(raw["variant"], "Комплектация", 300)
        identity = (maker.casefold(), model.casefold(), variant.casefold())
        if ref in refs or identity in identities:
            raise SupplementImportError("Повторены модель или точная комплектация")
        refs.add(ref)
        identities.add(identity)
        product_source = _source(raw["product_source_ref"], sources, "Источник модели")
        product_url = _url(raw["product_url"], "URL модели")
        if product_url != product_source["url"]:
            raise SupplementImportError("Ссылка модели должна совпадать с архивированным источником")
        if not isinstance(raw["applications"], list) or not raw["applications"]:
            raise SupplementImportError("Для модели нужна подтверждённая область применения")
        if not isinstance(raw["specifications"], list) or not isinstance(raw["offers"], list):
            raise SupplementImportError("Спецификации и предложения должны быть списками")
        apps, app_codes = [], set()
        for item in raw["applications"]:
            _fields(item, ("object_slug", "process_code", "source_ref", "source_page"), "Применение")
            obj = _text(item["object_slug"], "Тип объекта", 32)
            process = _text(item["process_code"], "Процесс", 80)
            if process_for(obj, process) is None or process in app_codes:
                raise SupplementImportError("Процесс применения неизвестен или повторён")
            app_codes.add(process)
            source = _source(item["source_ref"], sources, "Источник применения")
            apps.append({"object_slug": obj, "process_code": process,
                         "source_ref": source["source_ref"],
                         "source_page": _page(item["source_page"], source, "Применение")})
        specifications = []
        for item in raw["specifications"]:
            _fields(item, ("attribute", "source_value", "source_unit", "source_ref",
                           "source_page", "status"), "Характеристика")
            attribute = item["attribute"]
            if attribute not in ATTRIBUTE_UNITS:
                raise SupplementImportError("Характеристика не входит в проверенный контракт")
            unit = item["source_unit"]
            if unit not in UNITS or UNITS[unit][0] != ATTRIBUTE_UNITS[attribute]:
                raise SupplementImportError("Единица характеристики не соответствует атрибуту")
            source = _source(item["source_ref"], sources, "Источник характеристики")
            value = _decimal(item["source_value"], NUMBER, "Характеристика") * UNITS[unit][1]
            if value <= 0 or value >= Decimal("100000000000000"):
                raise SupplementImportError("Характеристика вне поддерживаемого диапазона")
            status = item["status"]
            if status not in ("manufacturer_spec", "conflicted"):
                raise SupplementImportError("Неизвестный статус характеристики")
            specifications.append({"attribute": attribute, "value": value,
                                   "unit": ATTRIBUTE_UNITS[attribute],
                                   "source_value": item["source_value"], "source_unit": unit,
                                   "source_ref": source["source_ref"],
                                   "source_page": _page(item["source_page"], source, "Характеристика"),
                                   "status": status})
        offers, offer_refs = [], set()
        for item in raw["offers"]:
            required = ("offer_ref", "price", "currency", "vat_status", "valid_from",
                        "valid_until", "source_ref", "source_page")
            if document["schema_version"] == 2:
                required += ("kind", "price_basis", "scope")
            _fields(item, required, "Предложение")
            offer_ref = _ref(item["offer_ref"], "Предложение")
            if offer_ref in offer_refs:
                raise SupplementImportError("Повторено коммерческое предложение")
            offer_refs.add(offer_ref)
            if document["schema_version"] == 2:
                kind = item["kind"]
                if kind not in ("purchase", "raas", "rental"):
                    raise SupplementImportError("Неверный вид коммерческого предложения")
                price_basis = _text(item["price_basis"], "База цены", 100)
                scope = _text(item["scope"], "Условия предложения", 1000)
            else:
                kind, price_basis, scope = "", "", ""
            price = _decimal(item["price"], MONEY, "Цена")
            currency = item["currency"]
            if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
                raise SupplementImportError("Для цены нужна валюта ISO 4217")
            vat = _text(item["vat_status"], "НДС", 80)
            try:
                start, end = date.fromisoformat(item["valid_from"]), date.fromisoformat(item["valid_until"])
            except (TypeError, ValueError):
                raise SupplementImportError("У предложения неверный срок действия") from None
            if end < start:
                raise SupplementImportError("Срок действия предложения обратный")
            source = _source(item["source_ref"], sources, "Источник цены")
            offers.append({"offer_ref": offer_ref, "kind": kind,
                           "price_basis": price_basis, "scope": scope,
                           "price": price, "currency": currency,
                           "vat_status": vat, "valid_from": start, "valid_until": end,
                           "source_ref": source["source_ref"],
                           "source_page": _page(item["source_page"], source, "Цена")})
        products.append({"product_ref": ref, "manufacturer": maker, "model": model,
                         "variant": variant, "product_url": product_url,
                         "product_source_ref": product_source["source_ref"],
                         "applications": apps, "specifications": specifications, "offers": offers})
    return checksum, sources, products


def verify_imported_supplement(batch):
    """Reconcile every stored normalized fact with the attested source bytes."""
    stored_sources = list(batch.source_artifacts.all())
    assets = {source.source_ref: bytes(source.raw_source) for source in stored_sources}
    if len(assets) != len(stored_sources):
        raise SupplementImportError("Повторён сохранённый первичный источник")
    _, sources, products = parse_supplement(
        bytes(batch.raw_source), expected_checksum=batch.checksum,
        source_assets=assets,
    )
    if batch.product_count != len(products) or set(assets) != set(sources):
        raise SupplementImportError("Сохранённый пакет расходится с первичным файлом")
    source_refs = {source.pk: source.source_ref for source in stored_sources}
    for source in stored_sources:
        original = sources[source.source_ref]
        if (source.url != original["url"] or source.checksum != original["checksum"]
                or source.fetched_at_utc != original["fetched_at_utc"]
                or source.content_type != original["content_type"]):
            raise SupplementImportError("Сохранённый источник расходится с первичным файлом")

    stored_products = list(batch.products.all())
    by_ref = {product.product_ref: product for product in stored_products}
    if len(by_ref) != len(stored_products) or set(by_ref) != {item["product_ref"] for item in products}:
        raise SupplementImportError("Сохранённые модели расходятся с первичным файлом")

    def compare_rows(expected, actual, fields):
        if Counter(tuple(item[field] for field in fields) for item in expected) != Counter(actual):
            raise SupplementImportError("Сохранённые сведения о модели расходятся с первичным файлом")

    for original in products:
        product = by_ref[original["product_ref"]]
        if (product.manufacturer != original["manufacturer"]
                or product.model != original["model"]
                or product.variant != original["variant"]
                or product.product_url != original["product_url"]
                or source_refs.get(product.product_source_id) != original["product_source_ref"]):
            raise SupplementImportError("Сохранённая модель расходится с первичным файлом")
        compare_rows(original["applications"], (
            (item.object_slug, item.process_code, source_refs.get(item.source_id), item.source_page)
            for item in product.applications.all()
        ), ("object_slug", "process_code", "source_ref", "source_page"))
        compare_rows(original["specifications"], (
            (item.attribute, item.value, item.unit, item.source_value, item.source_unit,
             source_refs.get(item.source_id), item.source_page, item.status)
            for item in product.specifications.all()
        ), ("attribute", "value", "unit", "source_value", "source_unit",
            "source_ref", "source_page", "status"))
        compare_rows(original["offers"], (
            (item.offer_ref, item.kind, item.price_basis, item.scope,
             item.price, item.currency, item.vat_status,
             item.valid_from, item.valid_until, source_refs.get(item.source_id), item.source_page)
            for item in product.offers.all()
        ), ("offer_ref", "kind", "price_basis", "scope", "price", "currency", "vat_status", "valid_from",
            "valid_until", "source_ref", "source_page"))


def import_supplement(content, *, expected_checksum, source_assets, source_label,
                      rights_basis, actor=None, actor_name=""):
    label = _text(source_label, "Имя дополнения", 100)
    rights = _text(rights_basis, "Основание обработки", 500)
    name = _text(actor_name or (actor.get_username() if actor else ""), "Оператор", 150)
    checksum, sources, products = parse_supplement(
        content, expected_checksum=expected_checksum, source_assets=source_assets,
    )
    with transaction.atomic():
        batch = SupplementBatch.objects.filter(checksum=checksum).first()
        created = batch is None
        if created:
            batch = SupplementBatch.objects.create(
                checksum=checksum, source_label=label, rights_basis=rights,
                raw_source=content, product_count=len(products),
            )
            source_rows = {ref: SupplementSourceArtifact.objects.create(
                batch=batch, **value,
            ) for ref, value in sources.items()}
            for row in products:
                product = SupplementProduct.objects.create(
                    batch=batch, product_ref=row["product_ref"],
                    manufacturer=row["manufacturer"], model=row["model"],
                    variant=row["variant"], product_url=row["product_url"],
                    product_source=source_rows[row["product_source_ref"]],
                )
                for app in row["applications"]:
                    source_ref = app.pop("source_ref")
                    SupplementApplication.objects.create(
                        product=product, source=source_rows[source_ref], **app,
                    )
                for spec in row["specifications"]:
                    source_ref = spec.pop("source_ref")
                    SupplementSpecification.objects.create(
                        product=product, source=source_rows[source_ref], **spec,
                    )
                for offer in row["offers"]:
                    source_ref = offer.pop("source_ref")
                    SupplementOffer.objects.create(
                        product=product, source=source_rows[source_ref], **offer,
                    )
        else:
            verify_imported_supplement(batch)
        SupplementAuditEvent.objects.create(
            kind="import", batch=batch, actor=actor, actor_name=name,
            reason=rights, created_new=created,
        )
    return batch, created
