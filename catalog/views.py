from django.contrib import messages
from django.contrib.auth.decorators import login_required
import hashlib
import logging

from django.conf import settings
from django.core.paginator import Paginator
from django.core.exceptions import PermissionDenied
from django.db import DatabaseError, transaction
from django.db.models import BooleanField, Case, IntegerField, Q, Value, When
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET
from django.views.decorators.http import require_http_methods
from django.views.decorators.cache import never_cache

from catalog.evidence_importing import EvidenceImportError, import_evidence
from catalog.forms import SourceUploadForm, SupplementUploadForm
from catalog.importing import CatalogImportError, import_catalog
from catalog.limits import (
    MAX_CATALOG_BYTES, MAX_EVIDENCE_BYTES, MAX_SUPPLEMENT_MANIFEST_BYTES,
)
from catalog.models import (
    CatalogBatch, CatalogEvidenceBatch, CatalogEvidenceClaim, CatalogFamily,
    CatalogImportEvent, CatalogPublication, CatalogPublicationEvent, SupplementAuditEvent,
    SupplementBatch, SupplementProduct, SupplementPublication,
)
from catalog.publication import (
    current_source_pair, publicly_available_evidence, visible_families,
)
from catalog.supplement_importing import SupplementImportError, import_supplement
from catalog.supplement_publication import (
    SupplementPublicationError, publicly_available_supplement, publish_supplement,
)
from catalog.supplement_uploads import source_assets_from_uploads
from projects.models import Project
from projects.task_profiles import process_for
from django.utils import timezone


logger = logging.getLogger(__name__)


ATTRIBUTE_LABELS = {
    "payload_kg": "Грузоподъёмность",
    "minimum_passage_mm": "Минимальная ширина прохода",
    "case_fleet_count": "Парк в опубликованном кейсе",
    "case_active_fleet_count": "Активные роботы в опубликованном кейсе",
    "wet_cleaning_runtime_h": "Время работы при влажной уборке",
    "cleaning_rate_m2_h": "Темп уборки",
    "case_cleaned_area_m2_2h": "Площадь уборки в испытании",
    "s2p_monthly_fee": "Абонентская плата модуля S2P",
    "p2p_monthly_fee": "Абонентская плата модуля P2P",
    "dimensions_lwh_mm": "Габариты: длина × ширина × высота",
    "runtime_h": "Время автономной работы",
    "runtime_80_to_20_h": "Автономная работа (80→20%, до)",
    "manufacturer_max_speed_m_s": "Максимальная скорость",
    "lift_height_mm": "Высота подъёма",
    "pallet_platform_transport": "Перевозка паллеты на платформе",
    "pallet_floor_pickup": "Подхват паллеты с пола",
    "full_charge_h": "Полная зарядка",
    "rental_price_floor": "Объявленная начальная цена аренды",
    "case_reported_payload_kg": "Заявленная грузовая масса в кейсе",
    "case_planned_fleet_count": "Планируемый парк в опубликованном кейсе",
    "purchase_price": "Цена покупки",
    "purchase_price_floor": "Цена покупки",
    "commissioning_price_floor": "Пусконаладка",
    "rental_first_month": "Аренда в первый месяц",
    "rental_following_month": "Аренда в последующие месяцы",
    "cleaning_rate_range_m2_h": "Расчётный темп уборки",
}
USE_LABELS = {
    "matching_limit": "Характеристика производителя; применима только к указанной модели",
    "case_context": "Показатель конкретного опубликованного внедрения",
    "topology_context": "Пример предметного маршрута",
    "offer_specific": "Условие конкретного коммерческого предложения",
    "blocked": "Противоречие или несовпадение модели; значение не используется",
    "manufacturer_link": "Сайт производителя",
    "product_copy": "Описание производителя",
    "candidate_application": "Область применения",
}
UNIT_LABELS = {
    "kg": "кг", "mm": "мм", "robots": "роботов", "tractors": "тягача",
    "h": "ч", "m/s": "м/с", "m2/h": "м²/ч", "m2 per 2h": "м² за 2 ч",
    "RUB/month": "₽/мес.", "RUB/month before VAT": "₽/мес. без НДС",
    "RUB/unit": "₽/шт.", "RUB": "₽", "N": "Н",
}

SUPPLEMENT_ATTRIBUTE_LABELS = {
    "payload_kg": "Грузоподъёмность",
    "tow_mass_kg": "Масса буксируемого состава",
    "max_cart_length_mm": "Максимальная длина тележки",
    "max_cart_width_mm": "Максимальная ширина тележки",
    "minimum_passage_mm": "Минимальная ширина прохода",
    "turning_diameter_mm": "Диаметр разворота",
    "manufacturer_max_speed_m_s": "Максимальная скорость",
    "drawbar_pull_n": "Тяговое усилие",
}

PRICE_ATTRIBUTES = {
    "purchase_price", "purchase_price_floor", "commissioning_price_floor",
    "rental_first_month", "rental_following_month", "rental_price_floor",
}
CARD_PRICE_ATTRIBUTES = ("purchase_price", "purchase_price_floor", "rental_price_floor")


def _display_claim(claim):
    claim.display_label = ATTRIBUTE_LABELS.get(claim.attribute, claim.attribute)
    claim.display_unit = UNIT_LABELS.get(claim.unit, claim.unit)
    if isinstance(claim.value, bool):
        claim.display_value = "Да" if claim.value else "Нет"
        claim.display_unit = ""
    elif isinstance(claim.value, list):
        separator = "–" if claim.attribute.endswith("_range_m2_h") else " × "
        claim.display_value = separator.join(
            f"{part:,}".replace(",", " ") if isinstance(part, int) else str(part)
            for part in claim.value
        )
    elif isinstance(claim.value, int):
        claim.display_value = f"{claim.value:,}".replace(",", " ")
    else:
        claim.display_value = claim.value
    return claim


def _purchase_or_rental(claims):
    for attribute in CARD_PRICE_ATTRIBUTES:
        found = next((claim for claim in claims if claim.attribute == attribute and claim.use == "offer_specific" and claim.value is not None and claim.unit.startswith("RUB")), None)
        if found:
            return found
    return None


def _selected_batch(request):
    checksum = request.GET.get("batch")
    if checksum:
        batch = get_object_or_404(CatalogBatch, checksum=checksum, source_kind="organizer_v4")
        if not (current_source_pair()[0] == batch
                or CatalogPublicationEvent.objects.filter(
                    evidence_batch__catalog_batch=batch,
                ).exists()
                or CatalogPublicationEvent.objects.filter(
                    previous_checksum__in=batch.evidence_batches.values("checksum"),
                ).exists()):
            raise Http404("Версия каталога не опубликована")
        return batch
    return current_source_pair()[0]


def _selected_evidence(request, batch):
    checksum = request.GET.get("evidence")
    if checksum:
        evidence = publicly_available_evidence(checksum, batch)
        if evidence is None:
            raise Http404("Версия сведений не опубликована")
        return evidence
    current_batch, current_evidence = current_source_pair()
    if current_batch and batch.pk == current_batch.pk:
        return current_evidence
    return None


@require_GET
def index(request):
    batch = _selected_batch(request)
    if batch is None:
        return HttpResponse("Сервис каталога временно недоступен", status=503)
    evidence_batch = _selected_evidence(request, batch)
    if evidence_batch is None:
        return HttpResponse("Сервис каталога временно недоступен", status=503)
    query = request.GET.get("q", "").strip()[:100]
    kind = request.GET.get("kind", "").strip()[:32]
    category = request.GET.get("category", "").strip()[:200]
    industry = request.GET.get("industry", "").strip()[:200]
    published_families = visible_families(batch, evidence_batch)
    families = published_families
    if query:
        families = families.filter(
            Q(name__icontains=query) | Q(company__icontains=query)
            | Q(source_rows__application__scenario__icontains=query)
        )
    if kind:
        families = families.filter(kind=kind)
    if category:
        families = families.filter(display_category=category)
    if industry:
        families = families.filter(source_rows__application__industry=industry)
    priced_family_ids = set()
    if evidence_batch:
        price_claims = CatalogEvidenceClaim.objects.filter(
            evidence_batch=evidence_batch, attribute__in=CARD_PRICE_ATTRIBUTES,
            use="offer_specific", unit__startswith="RUB",
        ).prefetch_related("catalog_rows")
        for claim in price_claims:
            if claim.value is not None:
                priced_family_ids.update(row.family_id for row in claim.catalog_rows.all())
    families = families.distinct().annotate(
        purchase_rank=Case(When(pk__in=priced_family_ids, then=Value(0)),
                           default=Value(1), output_field=IntegerField()),
    ).order_by("purchase_rank", "name", "id")
    categories = list(published_families.exclude(display_category="").values_list(
        "display_category", flat=True,
    ).distinct().order_by("display_category"))
    industries = list(batch.source_rows.filter(family__in=published_families).values_list(
        "application__industry", flat=True,
    ).distinct().order_by("application__industry"))
    supplement_cards = []
    publication = SupplementPublication.objects.select_related("batch").filter(
        pk="storefront",
    ).first()
    supplement = (publicly_available_supplement(publication.batch.checksum)
                  if publication else None)
    if supplement and not kind:
        object_names = dict(Project.OBJECT_TYPES)
        for product in supplement.products.prefetch_related(
            "applications", "specifications", "offers",
        ).order_by("manufacturer", "model", "variant", "product_ref"):
            applications = [
                (process_for(app.object_slug, app.process_code),
                 object_names.get(app.object_slug, app.object_slug))
                for app in product.applications.all()
            ]
            applications = [(process, object_name) for process, object_name in applications
                            if process is not None]
            categories.extend(process.title for process, _ in applications)
            industries.extend(object_name for _, object_name in applications)
            if query and not any(query.casefold() in value.casefold() for value in (
                product.model, product.variant, product.manufacturer,
                *(process.title for process, _ in applications),
            )):
                continue
            if category and all(process.title != category for process, _ in applications):
                continue
            if industry and all(object_name != industry for _, object_name in applications):
                continue
            visible_specs = [spec for spec in product.specifications.all()
                             if spec.status == "manufacturer_spec"
                             and spec.attribute in SUPPLEMENT_ATTRIBUTE_LABELS]
            today = timezone.localdate()
            current_offers = [offer for offer in product.offers.all()
                              if offer.valid_from <= today <= offer.valid_until
                              and offer.kind and offer.price_basis and offer.scope]
            supplement_cards.append({
                "product": product,
                "category": applications[0][0].title if applications else "",
                "specifications": [{
                    "label": SUPPLEMENT_ATTRIBUTE_LABELS[spec.attribute],
                    "value": spec.value, "unit": UNIT_LABELS.get(spec.unit, spec.unit),
                } for spec in visible_specs[:2]],
                "offer": current_offers[0] if len(current_offers) == 1 else None,
                "multiple_offers": len(current_offers) > 1,
                "checksum": supplement.checksum,
            })
    categories = sorted(set(categories))
    industries = sorted(set(industries))
    entries = ([('supplement', card) for card in supplement_cards]
               + [('organizer_v4', family) for family in families])
    page = Paginator(entries, getattr(settings, "CATALOG_PAGE_SIZE", 24)).get_page(
        request.GET.get("page")
    )
    cards = []
    page_families = [item for source, item in page.object_list if source == 'organizer_v4']
    claims_by_family = {family.pk: [] for family in page_families}
    if evidence_batch and page_families:
        public_claims = CatalogEvidenceClaim.objects.filter(
            evidence_batch=evidence_batch,
            catalog_rows__family_id__in=claims_by_family,
            use__in=("matching_limit", "offer_specific", "product_copy"),
        ).distinct().prefetch_related("catalog_rows")
        for claim in public_claims:
            _display_claim(claim)
            for row in claim.catalog_rows.all():
                if row.family_id in claims_by_family and claim not in claims_by_family[row.family_id]:
                    claims_by_family[row.family_id].append(claim)
    for family in page_families:
        cards.append({
            "family": family,
            "description": next((claim.value for claim in claims_by_family[family.pk]
                                 if claim.use == "product_copy" and claim.attribute == "product_summary"), ""),
            "price": _purchase_or_rental(claims_by_family[family.pk]),
            "specifications": [claim for claim in claims_by_family[family.pk]
                               if claim.use == "matching_limit" and claim.value is not None][:2],
        })
    return render(request, "catalog/index.html", {
        "batch": batch, "page": page, "query": query, "kind": kind,
        "category": category, "industry": industry,
        "categories": categories, "industries": industries,
        "evidence_batch": evidence_batch, "cards": cards,
        "supplement_cards": [item for source, item in page.object_list
                             if source == 'supplement'],
        "total_count": page.paginator.count,
    })


@require_GET
def family_detail(request, family_id):
    family = get_object_or_404(
        CatalogFamily.objects.select_related("batch"), pk=family_id,
    )
    rows = list(family.source_rows.select_related(
        "application", "offer", "specification"
    ).order_by("record_index"))
    evidence_batch = _selected_evidence(request, family.batch)
    if evidence_batch is None:
        return HttpResponse("Сервис каталога временно недоступен", status=503)
    family = get_object_or_404(visible_families(family.batch, evidence_batch), pk=family.pk)
    claims_by_row = {row.pk: [] for row in rows}
    if evidence_batch:
        claims = CatalogEvidenceClaim.objects.filter(
            evidence_batch=evidence_batch,
            catalog_rows__family=family,
        ).distinct().prefetch_related("catalog_rows")
        for claim in claims:
            _display_claim(claim)
            claim.use_label = USE_LABELS[claim.use]
            for source_row in claim.catalog_rows.all():
                if source_row.pk in claims_by_row:
                    claims_by_row[source_row.pk].append(claim)
    visible_claims = list({claim.pk: claim for row in rows for claim in claims_by_row[row.pk]}.values())
    specifications = [claim for claim in visible_claims if claim.use == "matching_limit" and claim.value is not None]
    prices = [claim for claim in visible_claims if claim.use == "offer_specific" and claim.attribute in PRICE_ATTRIBUTES and claim.value is not None and claim.unit.startswith("RUB")]
    product_copy = next((claim for claim in visible_claims if claim.use == "product_copy" and claim.attribute == "product_summary"), None)
    product_url = next((claim.source_url for claim in specifications), None) or (product_copy.source_url if product_copy else None)
    manufacturer_url = next((claim.source_url for claim in visible_claims if claim.use == "manufacturer_link"), None)
    description = product_copy.value if product_copy else ""
    applications = list(dict.fromkeys((row.application.industry, row.application.scenario) for row in rows if row.application.scenario))
    return render(request, "catalog/detail.html", {
        "family": family, "rows": rows, "evidence_batch": evidence_batch,
        "description": description, "applications": applications,
        "specifications": specifications, "prices": prices, "product_url": product_url,
        "manufacturer_url": manufacturer_url,
    })


@require_GET
def supplement_detail(request, checksum, product_ref):
    batch = publicly_available_supplement(checksum)
    if batch is None:
        raise Http404("Модель не найдена")
    product = get_object_or_404(
        SupplementProduct.objects.select_related("product_source"),
        batch=batch, product_ref=product_ref,
    )
    object_names = dict(Project.OBJECT_TYPES)
    applications = []
    for application in product.applications.all().order_by("object_slug", "process_code"):
        process = process_for(application.object_slug, application.process_code)
        if process is not None:
            applications.append({
                "object_slug": application.object_slug,
                "object_name": object_names.get(application.object_slug, application.object_slug),
                "title": process.title,
            })
    specifications = [{
        "label": SUPPLEMENT_ATTRIBUTE_LABELS[spec.attribute],
        "value": spec.value, "unit": UNIT_LABELS.get(spec.unit, spec.unit),
        "source_url": spec.source.url,
    } for spec in product.specifications.select_related("source").filter(
        status="manufacturer_spec",
    ).order_by("attribute") if spec.attribute in SUPPLEMENT_ATTRIBUTE_LABELS]
    today = timezone.localdate()
    offers = product.offers.select_related("source").filter(
        valid_from__lte=today, valid_until__gte=today,
    ).exclude(kind="").exclude(price_basis="").exclude(scope="").order_by(
        "kind", "offer_ref",
    )
    return render(request, "catalog/supplement_detail.html", {
        "product": product, "applications": applications,
        "specifications": specifications, "offers": offers,
    })


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def source_management(request):
    """Import an immutable source batch and record the responsible administrator."""
    if (not request.user.is_staff
            or not request.user.has_perm("catalog.add_catalogbatch")
            or not request.user.has_perm("catalog.add_catalogevidencebatch")):
        raise PermissionDenied
    form = SourceUploadForm(request.POST or None, request.FILES or None)
    status = 200
    publication_error = None
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "publish":
            if not request.user.has_perm("catalog.change_catalogpublication"):
                raise PermissionDenied
            checksum = request.POST.get("checksum", "").lower()
            reason = request.POST.get("reason", "").strip()
            candidate = CatalogEvidenceBatch.objects.select_related("catalog_batch").filter(
                checksum=checksum,
            ).first() if len(checksum) == 64 else None
            if candidate is None or not 10 <= len(reason) <= 500:
                publication_error, status = "Выберите сохранённую версию и укажите причину переключения.", 400
            elif (not settings.CATALOG_SOURCE_SHA256
                  or candidate.catalog_batch.checksum != settings.CATALOG_SOURCE_SHA256):
                publication_error, status = "Версия связана с каталогом, не утверждённым для этого окружения.", 409
            elif candidate.raw_source is None or candidate.catalog_batch.raw_source is None:
                publication_error, status = "Для публикации нужны архивированные исходные байты обоих источников.", 409
            else:
                try:
                    with transaction.atomic():
                        previous = CatalogPublication.objects.select_for_update().filter(
                            pk="storefront",
                        ).first()
                        old_checksum = previous.evidence_batch.checksum if previous else ""
                        if old_checksum != candidate.checksum:
                            CatalogPublication.objects.update_or_create(
                                pk="storefront",
                                defaults={"evidence_batch": candidate,
                                          "selected_by": request.user},
                            )
                            CatalogPublicationEvent.objects.create(
                                actor=request.user,
                                actor_name=request.user.get_username()[:150],
                                previous_checksum=old_checksum,
                                evidence_batch=candidate,
                                reason=reason,
                            )
                except DatabaseError:
                    logger.exception("Catalog publication failed for administrator %s", request.user.pk)
                    publication_error, status = "Не удалось переключить версию каталога.", 503
                else:
                    messages.success(request, "Версия опубликована." if old_checksum != candidate.checksum
                                     else "Эта версия уже опубликована.")
                    return redirect("catalog_source_management")
            form = SourceUploadForm()
        elif action == "upload" and form.is_valid():
            upload = form.cleaned_data["source_file"]
            kind = form.cleaned_data["source_kind"]
            limit = MAX_CATALOG_BYTES if kind == "catalog" else MAX_EVIDENCE_BYTES
            try:
                approved = (settings.CATALOG_SOURCE_SHA256 if kind == "catalog"
                            else settings.RESEARCH_CLAIMS_SHA256)
                if (not approved
                        or form.cleaned_data["expected_checksum"].lower() != approved):
                    raise CatalogImportError(
                        "Контрольная сумма источника не утверждена для этого окружения."
                    )
                raw = upload.read(limit + 1)
                if len(raw) > limit:
                    raise CatalogImportError("Файл превышает разрешённый размер")
                with transaction.atomic():
                    if kind == "catalog":
                        batch, created = import_catalog(
                            raw, source_label=upload.name,
                            source_kind="organizer_v4",
                            expected_checksum=form.cleaned_data["expected_checksum"],
                        )
                    else:
                        batch, created = import_evidence(
                            raw, source_label=upload.name,
                            expected_checksum=form.cleaned_data["expected_checksum"],
                            publish_initial=False,
                        )
                    CatalogImportEvent.objects.create(
                        actor=request.user, actor_name=request.user.get_username()[:150],
                        source_kind=kind, checksum=batch.checksum,
                        source_label=upload.name,
                        rights_basis=form.cleaned_data["rights_basis"],
                        created_new=created,
                    )
            except (CatalogImportError, EvidenceImportError, OSError) as exc:
                form.add_error("source_file", str(exc))
                status = 400
            except DatabaseError:
                logger.exception("Catalog source import failed for administrator %s", request.user.pk)
                form.add_error(None, "Не удалось сохранить источник. Повторите попытку позже.")
                status = 503
            else:
                messages.success(request, "Новая версия источника сохранена." if created
                                 else "Этот источник уже загружен; повторное действие записано в журнал.")
                return redirect("catalog_source_management")
        else:
            status = 400
            if action != "upload":
                publication_error = "Неизвестное действие."
    _, current_evidence = current_source_pair()
    evidence_batches = Paginator(
        CatalogEvidenceBatch.objects.select_related("catalog_batch").defer(
            "raw_source", "catalog_batch__raw_source",
        ).annotate(
            has_raw_source=Case(When(raw_source__isnull=False, then=Value(True)),
                                default=Value(False), output_field=BooleanField()),
        ), 20,
    ).get_page(request.GET.get("evidence_page"))
    return render(request, "catalog/source_management.html", {
        "form": form,
        "publication_error": publication_error,
        "current_evidence": current_evidence,
        "catalog_batches": CatalogBatch.objects.defer("raw_source").annotate(
            has_raw_source=Case(When(raw_source__isnull=False, then=Value(True)),
                                default=Value(False), output_field=BooleanField()),
        )[:20],
        "evidence_batches": evidence_batches,
        "import_events": CatalogImportEvent.objects.all()[:30],
        "publication_events": CatalogPublicationEvent.objects.select_related(
            "evidence_batch",
        )[:30],
    }, status=status)


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def supplement_management(request):
    if (not request.user.is_staff
            or not request.user.has_perm("catalog.add_supplementbatch")):
        raise PermissionDenied
    form = SupplementUploadForm(request.POST or None, request.FILES or None)
    status = 200
    publication_error = None
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "import":
            if form.is_valid():
                upload = form.cleaned_data["manifest_file"]
                try:
                    raw = upload.read(MAX_SUPPLEMENT_MANIFEST_BYTES + 1)
                    if hashlib.sha256(raw).hexdigest() != form.cleaned_data["expected_checksum"].lower():
                        raise SupplementImportError("Контрольная сумма файла дополнения не совпадает")
                    assets = source_assets_from_uploads(
                        raw, form.cleaned_data["source_assets"],
                    )
                    batch, created = import_supplement(
                        raw,
                        expected_checksum=form.cleaned_data["expected_checksum"].lower(),
                        source_assets=assets, source_label=upload.name,
                        rights_basis=form.cleaned_data["rights_basis"],
                        actor=request.user,
                    )
                except (SupplementImportError, OSError) as exc:
                    form.add_error(None, str(exc))
                    status = 400
                except DatabaseError:
                    logger.exception("Supplement import failed for administrator %s", request.user.pk)
                    form.add_error(None, "Не удалось сохранить дополнение. Повторите попытку позже.")
                    status = 503
                else:
                    messages.success(request, "Версия моделей сохранена." if created
                                     else "Эта версия уже загружена; действие записано в журнал.")
                    return redirect("catalog_supplement_management")
            else:
                status = 400
        elif action == "publish":
            if not request.user.has_perm("catalog.change_supplementpublication"):
                raise PermissionDenied
            form = SupplementUploadForm()
            if request.POST.get("publication_rights_attested") != "on":
                publication_error, status = "Подтвердите право публичного отображения сведений.", 400
            else:
                try:
                    _, changed = publish_supplement(
                        checksum=request.POST.get("checksum", "").lower(),
                        actor=request.user, reason=request.POST.get("reason", ""),
                    )
                except SupplementPublicationError as exc:
                    publication_error, status = str(exc), 400
                except DatabaseError:
                    logger.exception("Supplement publication failed for administrator %s", request.user.pk)
                    publication_error, status = "Не удалось опубликовать версию.", 503
                else:
                    messages.success(request, "Версия опубликована." if changed
                                     else "Эта версия уже опубликована.")
                    return redirect("catalog_supplement_management")
        else:
            form = SupplementUploadForm()
            publication_error, status = "Неизвестное действие.", 400
    publication = SupplementPublication.objects.select_related("batch").filter(
        pk="storefront",
    ).first()
    return render(request, "catalog/supplement_management.html", {
        "form": form, "publication_error": publication_error,
        "current_publication": publication,
        "batches": SupplementBatch.objects.defer("raw_source").order_by("-created_at")[:20],
        "events": SupplementAuditEvent.objects.select_related("batch").order_by("-created_at")[:30],
    }, status=status)
