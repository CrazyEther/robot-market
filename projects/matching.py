"""Explainable matching against one immutable catalog/evidence version."""

from collections import defaultdict
from decimal import Decimal, InvalidOperation

from catalog.models import CatalogEvidenceClaim, SupplementApplication, SupplementSpecification
from projects.task_profiles import PALLET_SPEC_BY_HANDOFF_MODE

MATCHING_VERSION = 2


REQUIREMENT_LABELS = {
    "payload_kg": "Грузоподъёмность",
    "minimum_passage_mm": "Ширина маршрута",
    "pallet_loading_interface": "Загрузка и снятие паллеты",
    "pallet_platform_transport": "Перевозка паллеты на платформе",
    "pallet_floor_pickup": "Подхват паллеты с пола",
    "airside_authorization": "Допуск в режимную зону",
    "tow_interface": "Сцепка с багажной тележкой",
    "public_zone_safety": "Движение в пассажирской зоне",
    "floor_compatibility": "Совместимость с покрытием",
    "food_hygiene": "Санитарная совместимость с доставкой питания",
    "elevator_interface": "Работа с лифтом",
    "hospital_disinfection_protocol": "Санитарная обработка между рейсами",
}
MEASURED_REQUIREMENTS = {
    "payload_kg": ("cargo_mass_kg", "kg", "Масса груза"),
    "minimum_passage_mm": ("route_width_mm", "mm", "Ширина маршрута"),
}


def _positive_decimal(raw):
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return value if value.is_finite() and value > 0 else None


def _check_requirement(attribute, claims, parameters):
    label = REQUIREMENT_LABELS[attribute]
    if any(claim.attribute == attribute and claim.use == "blocked" for claim in claims):
        return {"code": "source_conflict", "status": "requires_verification", "label": label,
                "message": "Сведения производителя расходятся; уточните паспорт конкретной комплектации."}
    verified = [claim for claim in claims if claim.attribute == attribute
                and claim.use == "matching_limit" and claim.value is not None]
    if not verified:
        return {"code": "missing_spec", "status": "requires_verification", "label": label,
                "message": "Для этой модели нужно подтвердить характеристику у производителя."}
    if len({(str(claim.value), claim.unit) for claim in verified}) != 1:
        return {"code": "source_conflict", "status": "requires_verification", "label": label,
                "message": "Для этой модели опубликованы разные значения; требуется уточнение."}
    claim = verified[0]
    source = claim.source_url
    if attribute not in MEASURED_REQUIREMENTS:
        if claim.unit == "boolean" and claim.value is True:
            return {"code": "verified", "status": "pass", "label": label,
                    "message": "Подтверждено для указанной модели.", "source_url": source}
        if claim.unit == "boolean" and claim.value is False:
            return {"code": "incompatible", "status": "reject", "label": label,
                    "message": "Производитель указывает несовместимость для этой модели.", "source_url": source}
        return {"code": "unusable_spec", "status": "requires_verification", "label": label,
                "message": "Формат характеристики не позволяет подтвердить условие.", "source_url": source}
    input_key, unit, input_label = MEASURED_REQUIREMENTS[attribute]
    reported = parameters.get(input_key) or {}
    if not isinstance(reported, dict):
        reported = {}
    if claim.unit != unit:
        return {"code": "unit_mismatch", "status": "requires_verification", "label": label,
                "message": "Единицы в паспорте модели требуют уточнения.", "source_url": source}
    capacity = _positive_decimal(claim.value)
    if capacity is None:
        return {"code": "unusable_spec", "status": "requires_verification", "label": label,
                "message": "Число в паспорте модели требует уточнения.", "source_url": source}
    observed = _positive_decimal(reported.get("value"))
    if observed is None or reported.get("unit") != unit or not reported.get("source"):
        return {"code": "missing_input", "status": "requires_verification", "label": label,
                "message": f"Укажите «{input_label}» в {unit} и источник значения.", "source_url": source}
    rejected = observed > capacity if attribute == "payload_kg" else observed < capacity
    if rejected:
        message = (
            f"Ширина узкого участка — {observed:g} {unit}; модели нужно не менее {capacity:g} {unit}."
            if attribute == "minimum_passage_mm" else
            f"Груз {observed:g} {unit} превышает предел модели {capacity:g} {unit}."
        )
        return {"code": "physical_limit", "status": "reject", "label": label,
                "message": message, "source_url": source, "input_source": reported["source"]}
    return {"code": "verified", "status": "pass", "label": label,
            "message": f"{input_label}: {observed:g} {unit}; предел модели: {capacity:g} {unit}.",
            "source_url": source, "input_source": reported["source"]}


def _pallet_handoff_checks(claims, parameters):
    reported = parameters.get("pallet_handoff_mode")
    label = "Способ передачи паллеты"
    if not isinstance(reported, dict):
        return [{"code": "missing_input", "status": "requires_verification", "label": label,
                 "message": "Укажите схему передачи паллеты и её источник."}]
    mode = reported.get("value")
    if (mode not in PALLET_SPEC_BY_HANDOFF_MODE
            or reported.get("unit") != "handoff_mode"
            or not isinstance(reported.get("source"), str)
            or not reported["source"].strip()
            or reported.get("status") != "user_attested"):
        return [{"code": "invalid_input", "status": "requires_verification", "label": label,
                 "message": "Подтвердите схему передачи паллеты по документу объекта."}]
    attribute = PALLET_SPEC_BY_HANDOFF_MODE[mode]
    return [
        {"code": "verified", "status": "pass", "label": label,
         "message": "Схема передачи паллеты указана владельцем объекта.",
         "input_source": reported["source"]},
        _check_requirement(attribute, claims, parameters),
    ]


def match_candidates(evidence_batch, process, task_profile):
    """Return source-linked rows; never infer candidate applications from keywords."""
    applications = CatalogEvidenceClaim.objects.filter(
        evidence_batch=evidence_batch, use="candidate_application",
        object_slug=process.object_slug, value=process.code,
    ).prefetch_related("catalog_rows__family")
    candidate_rows = {}
    for claim in applications:
        for row in claim.catalog_rows.all():
            candidate = candidate_rows.setdefault(row.pk, {"row": row, "applications": []})
            candidate["applications"].append(claim)
    if not candidate_rows:
        return []
    claims_by_row = defaultdict(list)
    specifications = CatalogEvidenceClaim.objects.filter(
        evidence_batch=evidence_batch,
        catalog_rows__in=[item["row"] for item in candidate_rows.values()],
        use__in=("matching_limit", "blocked"),
    ).distinct().prefetch_related("catalog_rows")
    for claim in specifications:
        for row in claim.catalog_rows.all():
            if row.pk in candidate_rows:
                claims_by_row[row.pk].append(claim)
    parameters = task_profile.get("parameters", {}) if isinstance(task_profile, dict) else {}
    if not isinstance(parameters, dict):
        parameters = {}
    results = []
    for candidate in candidate_rows.values():
        row = candidate["row"]
        applications = candidate["applications"]
        checks = [_check_requirement(attribute, claims_by_row[row.pk], parameters)
                  for attribute in process.required_specs]
        if process.code == "warehouse_pallet_transfer":
            checks.extend(_pallet_handoff_checks(claims_by_row[row.pk], parameters))
        statuses = {check["status"] for check in checks}
        status = "reject" if "reject" in statuses else "requires_verification" if "requires_verification" in statuses else "fit"
        results.append({
            "family": row.family, "source_row": row,
            "application_sources": [
                {"claim_id": application.claim_id, "source_url": application.source_url,
                 "scope": application.scope, "status": application.status}
                for application in applications
            ],
            "status": status, "checks": checks,
            "reason_codes": list(dict.fromkeys(check["code"] for check in checks if check["code"] != "verified")),
        })
    rank = {"fit": 0, "requires_verification": 1, "reject": 2}
    return sorted(results, key=lambda result: (
        rank[result["status"]], result["family"].name, result["source_row"].record_index,
    ))


def match_supplement_candidates(batch, process, task_profile):
    """Match exact manufacturer products without borrowing a v4 row or offer."""
    applications = SupplementApplication.objects.filter(
        product__batch=batch, object_slug=process.object_slug, process_code=process.code,
    ).select_related("product", "source")
    products = {}
    for application in applications:
        item = products.setdefault(application.product_id, {
            "product": application.product, "applications": [],
        })
        item["applications"].append(application)
    if not products:
        return []
    specifications = SupplementSpecification.objects.filter(
        product_id__in=products,
    ).select_related("source")
    claims_by_product = defaultdict(list)
    for spec in specifications:
        claims_by_product[spec.product_id].append(spec)
    parameters = task_profile.get("parameters", {}) if isinstance(task_profile, dict) else {}
    if not isinstance(parameters, dict):
        parameters = {}
    results = []
    for product_id, item in products.items():
        product = item["product"]
        claims = claims_by_product[product_id]
        checks = [_check_requirement(attribute, claims, parameters)
                  for attribute in process.required_specs]
        if process.code == "warehouse_pallet_transfer":
            checks.extend(_pallet_handoff_checks(claims, parameters))
        statuses = {check["status"] for check in checks}
        status = ("reject" if "reject" in statuses else
                  "requires_verification" if "requires_verification" in statuses else "fit")
        results.append({
            "source_kind": "manufacturer_supplement", "product": product,
            "name": f"{product.model} · {product.variant}" if product.variant != product.model else product.model,
            "company": product.manufacturer,
            "product_ref": product.product_ref,
            "catalog_source_checksum": batch.checksum,
            "application_sources": [{
                "source_url": application.source.url,
                "source_sha256": application.source.checksum,
                "source_page": application.source_page,
            } for application in item["applications"]],
            "specifications": [{
                "attribute": spec.attribute, "value": str(spec.value), "unit": spec.unit,
                "status": spec.status, "source_url": spec.source.url,
                "source_sha256": spec.source.checksum, "source_page": spec.source_page,
            } for spec in claims],
            "status": status, "checks": checks,
            "reason_codes": list(dict.fromkeys(check["code"] for check in checks
                                             if check["code"] != "verified")),
            "catalog_card_available": False,
        })
    rank = {"fit": 0, "requires_verification": 1, "reject": 2}
    return sorted(results, key=lambda item: (rank[item["status"]],
                                             item["company"], item["name"],
                                             item["product_ref"]))
