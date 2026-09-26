"""Import curated primary-source claims as an immutable, catalog-pinned batch."""

import hashlib
import json
import math
import re
from datetime import datetime
from urllib.parse import urlparse

from django.db import transaction
from django.utils import timezone

from catalog.limits import MAX_EVIDENCE_BYTES
from catalog.models import (
    CatalogBatch, CatalogEvidenceBatch, CatalogEvidenceClaim, CatalogPublication,
    CatalogSourceRow,
)
from projects.models import Project
from projects.task_profiles import QUALITATIVE_SPECS, process_for


HEX_SHA = re.compile(r"[0-9a-fA-F]{64}")
CLAIM_ID = re.compile(r"[a-z0-9_\-]{1,120}")
STATUSES = {
    "manufacturer_spec", "vendor_case_reported", "source_conflict",
    "model_variant_uncertain", "vendor_offer_reported", "industry_case_reported",
    "manufacturer_website", "manufacturer_description", "manufacturer_application",
}
USES = {"matching_limit", "case_context", "topology_context", "offer_specific", "blocked", "manufacturer_link", "product_copy", "candidate_application"}
REQUIRED = {
    "id", "object", "attribute", "value", "unit", "scope", "status", "use",
    "source_url", "source_sha256", "source_fetched_at_utc", "catalog_sha256",
    "catalog_rows", "catalog_refs",
}


class EvidenceImportError(ValueError):
    pass


def _reject_json_constant(value):
    raise EvidenceImportError(f"Недопустимое JSON-значение: {value}")


def _valid_value(value):
    if value is None:
        return True
    if isinstance(value, bool):
        return True
    if isinstance(value, int):
        return len(str(value)) <= 32
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, str):
        return 0 < len(value) <= 200
    if isinstance(value, list):
        return 0 < len(value) <= 12 and all(_valid_value(item) and item is not None for item in value)
    return False


def _text(value, label, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or any(ord(char) < 32 for char in value):
        raise EvidenceImportError(f"Некорректное поле {label}")
    return value.strip()


def parse_evidence(content, *, expected_checksum):
    if not content or len(content) > MAX_EVIDENCE_BYTES:
        raise EvidenceImportError("Реестр источников пуст или превышает 2 МБ")
    checksum = hashlib.sha256(content).hexdigest()
    if not HEX_SHA.fullmatch(expected_checksum or "") or checksum != expected_checksum.lower():
        raise EvidenceImportError("Контрольная сумма реестра источников не совпадает")
    try:
        claims = json.loads(content.decode("utf-8"), parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise EvidenceImportError("Реестр источников должен быть корректным UTF-8 JSON") from None
    if not isinstance(claims, list) or not 0 < len(claims) <= 5000:
        raise EvidenceImportError("Реестр источников должен содержать от 1 до 5000 утверждений")
    catalog_checksums = set()
    seen = set()
    allowed_objects = dict(Project.OBJECT_TYPES)
    parsed = []
    for number, raw in enumerate(claims, 1):
        if not isinstance(raw, dict) or not REQUIRED.issubset(raw):
            raise EvidenceImportError(f"Утверждение {number}: отсутствуют обязательные поля")
        claim_id = _text(raw["id"], "id", 120)
        if not CLAIM_ID.fullmatch(claim_id) or claim_id in seen:
            raise EvidenceImportError(f"Утверждение {number}: неверный или повторный id")
        seen.add(claim_id)
        object_slug = _text(raw["object"], "object", 32)
        attribute = _text(raw["attribute"], "attribute", 120)
        unit = _text(raw["unit"], "unit", 80)
        status = _text(raw["status"], "status", 40)
        use = _text(raw["use"], "use", 40)
        if object_slug not in allowed_objects and not (object_slug == "catalog" and use == "manufacturer_link"):
            raise EvidenceImportError(f"Утверждение {number}: неизвестный тип объекта")
        if status not in STATUSES or use not in USES:
            raise EvidenceImportError(f"Утверждение {number}: неизвестный статус или применение")
        if (raw["value"] is None) != (use == "blocked"):
            raise EvidenceImportError(f"Утверждение {number}: заблокированное значение должно быть неизвестным")
        if not _valid_value(raw["value"]):
            raise EvidenceImportError(f"Утверждение {number}: неверный формат значения")
        if status in {"source_conflict", "model_variant_uncertain"} and use != "blocked":
            raise EvidenceImportError(f"Утверждение {number}: конфликт нельзя применять")
        if status == "manufacturer_website" and use != "manufacturer_link":
            raise EvidenceImportError(f"Утверждение {number}: сайт производителя не является характеристикой")
        if status == "manufacturer_description" and use != "product_copy":
            raise EvidenceImportError(f"Утверждение {number}: описание производителя не является характеристикой")
        if use == "candidate_application" and (
            attribute != "process_application" or unit != "process_code"
            or status not in {"manufacturer_application", "vendor_case_reported"}
            or not isinstance(raw["value"], str)
            or process_for(object_slug, raw["value"]) is None
        ):
            raise EvidenceImportError(f"Утверждение {number}: неизвестное применение модели к процессу")
        if status == "manufacturer_application" and use != "candidate_application":
            raise EvidenceImportError(f"Утверждение {number}: применение производителя нельзя считать паспортным ограничением")
        if use == "product_copy" and (status != "manufacturer_description" or attribute != "product_summary" or unit != "text" or not isinstance(raw["value"], str)):
            raise EvidenceImportError(f"Утверждение {number}: неверное описание модели")
        if use == "matching_limit" and status != "manufacturer_spec":
            raise EvidenceImportError(f"Утверждение {number}: для ограничения подбора нужен паспорт производителя")
        if use == "matching_limit" and attribute in QUALITATIVE_SPECS:
            if type(raw["value"]) is not bool or unit != "boolean":
                raise EvidenceImportError(f"Утверждение {number}: качественное ограничение должно быть подтверждено как boolean")
        elif type(raw["value"]) is bool:
            raise EvidenceImportError(f"Утверждение {number}: boolean допустим только для качественного ограничения")
        url = _text(raw["source_url"], "source_url", 1000)
        parsed_url = urlparse(url)
        if parsed_url.scheme != "https" or not parsed_url.hostname or parsed_url.username or parsed_url.password:
            raise EvidenceImportError(f"Утверждение {number}: нужен первичный HTTPS-адрес")
        if use == "manufacturer_link" and (status != "manufacturer_website" or attribute != "manufacturer_homepage" or raw["value"] != url or unit != "url"):
            raise EvidenceImportError(f"Утверждение {number}: неверная ссылка производителя")
        source_sha = _text(raw["source_sha256"], "source_sha256", 64)
        catalog_sha = _text(raw["catalog_sha256"], "catalog_sha256", 64)
        if not HEX_SHA.fullmatch(source_sha) or not HEX_SHA.fullmatch(catalog_sha):
            raise EvidenceImportError(f"Утверждение {number}: неверный SHA-256")
        catalog_checksums.add(catalog_sha.lower())
        try:
            fetched = datetime.fromisoformat(raw["source_fetched_at_utc"])
        except (TypeError, ValueError):
            raise EvidenceImportError(f"Утверждение {number}: неверная дата получения") from None
        if timezone.is_naive(fetched):
            raise EvidenceImportError(f"Утверждение {number}: дата получения должна содержать часовой пояс")
        rows, refs = raw["catalog_rows"], raw["catalog_refs"]
        if not isinstance(rows, list) or not isinstance(refs, list) or len(rows) != len(refs):
            raise EvidenceImportError(f"Утверждение {number}: неверные ссылки на каталог")
        if len(rows) != len(set(str(row) for row in rows)):
            raise EvidenceImportError(f"Утверждение {number}: повторная запись каталога")
        for row, ref in zip(rows, refs):
            if type(row) is not int or row < 1 or not isinstance(ref, dict) or ref.get("record_index") != row:
                raise EvidenceImportError(f"Утверждение {number}: неверный номер записи каталога")
            for key in ("external_id", "name", "company"):
                _text(ref.get(key), key, 300)
        parsed.append({
            "claim_id": claim_id,
            "object_slug": object_slug,
            "attribute": attribute,
            "value": raw["value"],
            "unit": unit,
            "scope": _text(raw["scope"], "scope", 1000),
            "status": status, "use": use,
            "source_url": url,
            "source_sha256": source_sha.lower(),
            "source_fetched_at_utc": fetched,
            "catalog_refs": refs,
        })
    if len(catalog_checksums) != 1:
        raise EvidenceImportError("Утверждения относятся к разным версиям каталога")
    limits = {
        (claim["object_slug"], claim["attribute"], ref["record_index"])
        for claim in parsed if claim["use"] == "matching_limit"
        for ref in claim["catalog_refs"]
    }
    conflicts = {
        (claim["object_slug"], claim["attribute"], ref["record_index"])
        for claim in parsed if claim["use"] == "blocked"
        for ref in claim["catalog_refs"]
    }
    if limits & conflicts:
        raise EvidenceImportError("Противоречивая характеристика не может одновременно использоваться в подборе")
    return checksum, catalog_checksums.pop(), parsed


def import_evidence(content, *, source_label, expected_checksum, publish_initial=True):
    label = _text(source_label, "source_label", 100)
    checksum, catalog_sha, claims = parse_evidence(content, expected_checksum=expected_checksum)
    with transaction.atomic():
        batch = CatalogBatch.objects.filter(checksum=catalog_sha, source_kind="organizer_v4").first()
        if batch is None:
            raise EvidenceImportError("Версия каталога для утверждений не загружена")
        existing = CatalogEvidenceBatch.objects.filter(checksum=checksum).first()
        if existing:
            if existing.catalog_batch_id != batch.pk:
                raise EvidenceImportError("Реестр уже связан с другой версией каталога")
            if existing.raw_source is None:
                CatalogEvidenceBatch.objects.filter(
                    pk=existing.pk, raw_source__isnull=True,
                ).update(raw_source=content)
            if publish_initial:
                CatalogPublication.objects.get_or_create(
                    pk="storefront", defaults={"evidence_batch": existing},
                )
            return existing, False
        source_rows = {
            row.record_index: row for row in CatalogSourceRow.objects.filter(batch=batch).select_related("family")
        }
        for claim in claims:
            for ref in claim["catalog_refs"]:
                row = source_rows.get(ref["record_index"])
                if row is None or row.external_id != ref["external_id"] or row.family.name != ref["name"] or row.family.company != ref["company"]:
                    raise EvidenceImportError(f"Утверждение {claim['claim_id']}: ссылка на модель не совпадает с v4")
        evidence_batch = CatalogEvidenceBatch.objects.create(
            catalog_batch=batch, checksum=checksum, source_label=label, claim_count=len(claims),
            raw_source=content,
        )
        for claim in claims:
            refs = claim.pop("catalog_refs")
            evidence = CatalogEvidenceClaim.objects.create(evidence_batch=evidence_batch, **claim)
            evidence.catalog_rows.add(*(source_rows[ref["record_index"]] for ref in refs))
        if publish_initial:
            CatalogPublication.objects.get_or_create(
                pk="storefront", defaults={"evidence_batch": evidence_batch},
            )
    return evidence_batch, True
