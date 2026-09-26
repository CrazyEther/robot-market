"""Integration checks against the actual permitted catalog source, never a fake seed."""

import hashlib
import os
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.request import Request

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils.html import strip_tags

from catalog.bootstrap_source import (
    CatalogBootstrapError, HTTPSRedirectHandler, ensure_source, verify_bytes,
)
from catalog.evidence_importing import EvidenceImportError, import_evidence, parse_evidence
from catalog.importing import CatalogImportError, import_catalog, parse_catalog, parse_price
from catalog.models import (
    CatalogBatch, CatalogEvidenceBatch, CatalogEvidenceClaim,
    CatalogFamily, CatalogOffer, CatalogSourceRow,
)


class CatalogSourceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        source_path = os.environ.get("CATALOG_SOURCE_PATH")
        if not source_path:
            raise RuntimeError("CATALOG_SOURCE_PATH обязателен для проверки реального источника")
        cls.source_path = Path(source_path)
        cls.content = cls.source_path.read_bytes()
        cls.checksum, cls.records = parse_catalog(cls.content)
        cls.batch, created = import_catalog(
            cls.content, source_label=cls.source_path.name,
            source_kind="organizer_v4",
        )
        if not created:
            raise AssertionError("Первый импорт тестовой базы должен создать пакет")

    def test_all_source_rows_and_offers_survive_reimport(self):
        self.assertEqual(self.batch.checksum, self.checksum)
        self.assertEqual(self.batch.row_count, len(self.records))
        self.assertEqual(CatalogSourceRow.objects.filter(batch=self.batch).count(), len(self.records))
        self.assertEqual(CatalogOffer.objects.filter(source_row__batch=self.batch).count(), len(self.records))
        again, created = import_catalog(
            self.content, source_label=self.source_path.name,
            source_kind="organizer_v4", expected_checksum=self.checksum,
        )
        self.assertFalse(created)
        self.assertEqual(again.pk, self.batch.pk)
        self.assertEqual(CatalogBatch.objects.count(), 1)
        self.assertEqual(CatalogSourceRow.objects.count(), len(self.records))

    def test_duplicate_ids_keep_distinct_rows_applications_and_prices(self):
        groups = {}
        for record in self.records:
            groups.setdefault(record["raw"]["id"], []).append(record)
        duplicates = {key: rows for key, rows in groups.items() if len(rows) > 1}
        self.assertTrue(duplicates, "Источник должен содержать повторные ID для этой проверки")
        for external_id, records in duplicates.items():
            with self.subTest(external_id=external_id):
                stored = CatalogSourceRow.objects.filter(batch=self.batch, external_id=external_id)
                self.assertEqual(stored.count(), len(records))
                self.assertEqual(
                    list(stored.values_list("offer__price_value", flat=True)),
                    [record["price"] for record in records],
                )

    def test_csv_quoting_multiline_and_error_rollback(self):
        multiline = [record for record in self.records if record["line_end"] > record["line_start"]]
        self.assertTrue(multiline, "Источник должен проверить многострочные CSV поля")
        first = multiline[0]
        stored = CatalogSourceRow.objects.get(batch=self.batch, record_index=first["index"])
        self.assertEqual((stored.line_start, stored.line_end), (first["line_start"], first["line_end"]))
        before = CatalogBatch.objects.count()
        for invalid in (b"\xff", b"broken;" + self.content):
            with self.subTest(invalid=invalid[:8]):
                with self.assertRaises(CatalogImportError):
                    import_catalog(invalid, source_label="invalid", source_kind="organizer_v4")
                self.assertEqual(CatalogBatch.objects.count(), before)
        with self.assertRaisesMessage(CatalogImportError, "Контрольная сумма"):
            import_catalog(self.content, source_label=self.source_path.name,
                           source_kind="organizer_v4", expected_checksum="0" * 64)
        self.assertEqual(CatalogBatch.objects.count(), before)

    def test_price_null_and_true_zero_are_distinct(self):
        self.assertIsNone(parse_price(""))
        self.assertEqual(parse_price("0"), 0)
        with self.assertRaises(CatalogImportError):
            parse_price("неизвестно")

    def test_command_and_public_catalog_use_imported_source(self):
        call_command("import_catalog", self.source_path,
                     source_kind="organizer_v4", expected_sha256=self.checksum, verbosity=0)
        self.assertEqual(CatalogBatch.objects.count(), 1)
        self.assertEqual(self.client.get(reverse("catalog_index")).status_code, 503)
        family = CatalogFamily.objects.filter(batch=self.batch).first()
        self.assertEqual(
            self.client.get(reverse("catalog_family_detail", args=[family.pk])).status_code,
            503,
        )
        self.assertEqual(self.client.get("/ready").status_code, 503)

    def test_bootstrap_accepts_only_the_verified_source(self):
        self.assertEqual(
            ensure_source(path=self.source_path, url="", expected=self.checksum),
            self.source_path,
        )
        with self.assertRaisesMessage(CatalogBootstrapError, "Контрольная сумма"):
            verify_bytes(self.content, "0" * 64)
        with TemporaryDirectory() as directory:
            with self.assertRaisesMessage(CatalogBootstrapError, "Нужен подключённый"):
                ensure_source(
                    path=Path(directory) / "catalog.csv",
                    url="", expected=self.checksum,
                )
        with self.assertRaisesMessage(CatalogBootstrapError, "нарушило HTTPS"):
            HTTPSRedirectHandler().redirect_request(
                Request("https://ronavi-robotics.ru/catalogue/h1500"), None,
                302, "Found", {}, "http://ronavi-robotics.ru/catalogue/h1500",
            )

    def test_primary_claims_are_versioned_and_linked_to_exact_rows(self):
        claims_path = os.environ.get("RESEARCH_CLAIMS_PATH")
        if not claims_path:
            raise RuntimeError("RESEARCH_CLAIMS_PATH обязателен для проверки источников")
        content = Path(claims_path).read_bytes()
        checksum = hashlib.sha256(content).hexdigest()
        batch, created = import_evidence(
            content, source_label=Path(claims_path).name,
            expected_checksum=checksum,
        )
        self.assertTrue(created)
        self.assertEqual(batch.catalog_batch_id, self.batch.pk)
        self.assertEqual(self.client.get("/ready").status_code, 200)
        pinned_catalog = self.client.get(reverse("catalog_index"), {
            "batch": self.batch.checksum, "evidence": batch.checksum,
        })
        self.assertContains(pinned_catalog, batch.checksum)
        self.assertEqual(pinned_catalog.context["page"].paginator.count, 7)
        self.assertNotContains(pinned_catalog, "85ТК")
        self.assertNotContains(pinned_catalog, '<option value="brs"')
        self.assertNotContains(pinned_catalog, "революция в складской логистике")
        amr_page = self.client.get(reverse("catalog_index"), {"category": "AMR"})
        self.assertEqual(amr_page.context["page"].paginator.count, 2)
        mobile_page = self.client.get(reverse("catalog_index"), {"category": "Мобильные роботы"})
        self.assertEqual(mobile_page.context["page"].paginator.count, 1)
        self.assertEqual(CatalogSourceRow.objects.filter(batch=self.batch).count(), len(self.records))
        self.assertEqual(self.client.get(reverse("catalog_index"), {
            "batch": self.batch.checksum, "evidence": "0" * 64,
        }).status_code, 404)
        self.assertEqual(batch.claim_count, len(json.loads(content)))
        self.assertEqual(CatalogEvidenceClaim.objects.filter(evidence_batch=batch).count(), batch.claim_count)
        linked = CatalogEvidenceClaim.objects.get(evidence_batch=batch, claim_id="ronavi_h1500_payload")
        self.assertEqual(list(linked.catalog_rows.values_list("record_index", flat=True).order_by("record_index")), [1, 66])
        self.assertEqual(linked.use, "matching_limit")
        case_route = CatalogEvidenceClaim.objects.get(
            evidence_batch=batch, claim_id="ronavi_sostra_route_length",
        )
        self.assertEqual(case_route.use, "topology_context")
        self.assertEqual(case_route.unit, "m")
        self.assertGreater(case_route.value, 0)
        self.assertEqual(list(case_route.catalog_rows.values_list("record_index", flat=True)), [1])
        self.assertIn("sostra-fmcg", case_route.source_url)
        case_load = CatalogEvidenceClaim.objects.get(
            evidence_batch=batch, claim_id="ronavi_sostra_pallet_mass",
        )
        self.assertEqual(case_load.use, "case_context")
        self.assertNotEqual(case_load.attribute, "payload_kg")
        airport_exclusion = CatalogEvidenceClaim.objects.get(
            evidence_batch=batch, claim_id="r2b_airport_tactile_exclusion",
        )
        self.assertEqual(airport_exclusion.use, "topology_context")
        self.assertEqual(airport_exclusion.object_slug, "airport")
        self.assertEqual(list(airport_exclusion.catalog_rows.values_list("record_index", flat=True)), [21])
        hospital_zones = CatalogEvidenceClaim.objects.get(
            evidence_batch=batch, claim_id="waybot_hospital_cleaning_zones",
        )
        self.assertEqual(hospital_zones.object_slug, "hospital")
        self.assertEqual(hospital_zones.use, "topology_context")
        self.assertEqual(list(hospital_zones.catalog_rows.values_list("record_index", flat=True)), [24])
        for claim_id, indices, value, unit, label in (
            ("ronavi_h1500_no_pallet_lift", [1, 66], 0, "mm", "Высота подъёма"),
            ("ronavi_h1500_platform_transport", [1, 66], True, "boolean", "Перевозка паллеты на платформе"),
            ("ronavi_h1500_no_floor_pickup", [1, 66], False, "boolean", "Подхват паллеты с пола"),
            ("ronavi_h1500_max_speed", [1, 66], 1.5, "m/s", "Максимальная скорость"),
            ("ronavi_h1500_runtime_80_to_20", [1, 66], 10, "h", "Автономная работа (80→20%, до)"),
            ("ronavi_h2000_max_speed", [3, 67], 1, "m/s", "Максимальная скорость"),
            ("ronavi_h2000_runtime_80_to_20", [3, 67], 10, "h", "Автономная работа (80→20%, до)"),
            ("r2b_mark2se_minimum_passage", [21], 1300, "mm", "Минимальная ширина прохода"),
            ("r2b_mark2se_full_charge", [21], 2, "h", "Полная зарядка"),
            ("waybot_400pro_full_charge", [23], 1, "h", "Полная зарядка"),
        ):
            with self.subTest(claim=claim_id):
                specification = CatalogEvidenceClaim.objects.get(evidence_batch=batch, claim_id=claim_id)
                self.assertEqual(specification.value, value)
                self.assertEqual(specification.unit, unit)
                self.assertEqual(specification.use, "matching_limit")
                self.assertEqual(list(specification.catalog_rows.values_list("record_index", flat=True).order_by("record_index")), indices)
                self.assertRegex(specification.source_sha256, r"^[0-9a-f]{64}$")
                self.assertTrue(specification.source_url.startswith("https://"))
                family_for_claim = CatalogSourceRow.objects.get(batch=self.batch, record_index=indices[0]).family
                model_page = self.client.get(reverse("catalog_family_detail", args=[family_for_claim.pk]),
                                             {"evidence": batch.checksum})
                self.assertContains(model_page, label)
                self.assertContains(model_page, specification.source_url)
                if claim_id == "ronavi_h1500_no_pallet_lift":
                    self.assertContains(model_page, "0 мм")
                if claim_id == "ronavi_h1500_platform_transport":
                    self.assertContains(model_page, "Да")
                if claim_id == "ronavi_h1500_no_floor_pickup":
                    self.assertContains(model_page, "Нет")
        family = CatalogSourceRow.objects.get(batch=self.batch, record_index=1).family
        detail = self.client.get(reverse("catalog_family_detail", args=[family.pk]))
        self.assertContains(detail, "Технические данные")
        self.assertContains(detail, linked.source_url)
        self.assertContains(detail, "Грузоподъёмность")
        self.assertContains(self.client.get(reverse("catalog_index"), {"q": family.name}), family.name)
        visible = strip_tags(detail.content.decode())
        for diagnostic in (self.batch.source_label, "валюта не подтверждена",
                           "Статус НДС не подтверждён источником", "Не предоставлены в CSV",
                           "Каждая строка ниже", "Источник цены"):
            self.assertNotIn(diagnostic, visible)
        self.assertContains(
            self.client.get(reverse("catalog_family_detail", args=[family.pk]), {"evidence": batch.checksum}),
            batch.checksum,
        )
        self.assertNotIn(batch.checksum, strip_tags(detail.content.decode()))
        tractor = CatalogSourceRow.objects.get(batch=self.batch, record_index=88).family
        tractor_page = self.client.get(reverse("catalog_family_detail", args=[tractor.pk]),
                                       {"evidence": batch.checksum})
        self.assertEqual(tractor_page.status_code, 404)
        tractor_search = self.client.get(reverse("catalog_index"), {"q": tractor.name})
        self.assertEqual(tractor_search.context["page"].paginator.count, 0)
        h1500_text = strip_tags(detail.content.decode())
        self.assertIn("2 160 000", h1500_text)
        self.assertIn("от 100 роботов", h1500_text)
        carrier_row = CatalogSourceRow.objects.get(batch=self.batch, record_index=13)
        carrier_price = CatalogEvidenceClaim.objects.get(
            evidence_batch=batch, claim_id="kiit_dmr_carrier_p_price_from_vat",
        )
        self.assertEqual(carrier_price.value, 4_300_000)
        self.assertEqual(carrier_price.unit, "RUB/unit")
        self.assertEqual(list(carrier_price.catalog_rows.values_list("record_index", flat=True)), [13])
        carrier_page = self.client.get(
            reverse("catalog_family_detail", args=[carrier_row.family_id]),
            {"evidence": batch.checksum},
        )
        carrier_text = strip_tags(carrier_page.content.decode())
        self.assertIn("4 300 000 ₽/шт.", carrier_text)
        self.assertIn("с НДС", carrier_text)
        self.assertContains(carrier_page, carrier_price.source_url)
        self.assertIn("Подробнее об условиях", carrier_text)
        self.assertNotIn("Условия производителя", carrier_text)
        self.assertNotIn("1600 мм", carrier_text)
        unit_row = CatalogSourceRow.objects.get(batch=self.batch, record_index=25)
        unit_page = self.client.get(
            reverse("catalog_family_detail", args=[unit_row.family_id]),
            {"evidence": batch.checksum},
        )
        unit_text = strip_tags(unit_page.content.decode())
        self.assertIn("от 2 300 000 ₽/шт.", unit_text)
        self.assertIn("от 100 000 ₽/мес.", unit_text)
        self.assertIn("Габариты: длина × ширина × высота", unit_text)
        self.assertContains(unit_page, "https://yacuai.com/ru/byuunit/")
        for claim_id in ("yacu_unit_runtime_ru_conflict", "yacu_unit_runtime_en_conflict",
                         "yacu_unit_area_ru_conflict", "yacu_unit_area_en_conflict"):
            blocked = CatalogEvidenceClaim.objects.get(evidence_batch=batch, claim_id=claim_id)
            self.assertIsNone(blocked.value)
            self.assertEqual(blocked.use, "blocked")
        waybot = CatalogSourceRow.objects.get(batch=self.batch, record_index=24).family
        waybot_page = self.client.get(reverse("catalog_family_detail", args=[waybot.pk]),
                                      {"evidence": batch.checksum})
        waybot_text = strip_tags(waybot_page.content.decode())
        self.assertIn("2 300 000", waybot_text)
        self.assertIn("400 000", waybot_text)
        self.assertIn("120 000", waybot_text)
        self.assertIn("пусконаладка оплачивается отдельно", waybot_text)
        h2000 = CatalogSourceRow.objects.get(batch=self.batch, record_index=3).family
        h2000_page = self.client.get(reverse("catalog_family_detail", args=[h2000.pk]),
                                      {"evidence": batch.checksum})
        h2000_text = strip_tags(h2000_page.content.decode())
        self.assertIn("2 805 000", h2000_text)
        self.assertIn("от 100 роботов", h2000_text)
        self.assertIn("1 190 мм", h2000_text)
        cleanbotics_400 = CatalogSourceRow.objects.get(batch=self.batch, record_index=23).family
        cleanbotics_400_page = self.client.get(
            reverse("catalog_family_detail", args=[cleanbotics_400.pk]),
            {"evidence": batch.checksum},
        )
        cleanbotics_400_text = strip_tags(cleanbotics_400_page.content.decode())
        self.assertIn("1 500 000", cleanbotics_400_text)
        self.assertIn("260 000", cleanbotics_400_text)
        self.assertIn("700–1 200 м²/ч", cleanbotics_400_text)
        self.assertIn("Робот для влажной и сухой уборки", cleanbotics_400_text)
        mark2 = CatalogSourceRow.objects.get(batch=self.batch, record_index=21).family
        mark2_page = self.client.get(reverse("catalog_family_detail", args=[mark2.pk]),
                                     {"evidence": batch.checksum})
        mark2_text = strip_tags(mark2_page.content.decode())
        self.assertIn("Автономный робот для влажной уборки", mark2_text)
        self.assertNotIn("Fвтономный", mark2_text)
        self.assertIn("1 300 мм", mark2_text)
        self.assertNotIn("Время автономной работы", mark2_text)
        self.assertFalse(CatalogEvidenceClaim.objects.filter(
            evidence_batch=batch, claim_id="r2b_mark2se_runtime",
        ).exists())
        for conflict_id in ("r2b_mark2se_runtime_product_conflict", "r2b_mark2se_runtime_rent_conflict"):
            conflict = CatalogEvidenceClaim.objects.get(evidence_batch=batch, claim_id=conflict_id)
            self.assertIsNone(conflict.value)
            self.assertEqual(conflict.use, "blocked")
        self.assertEqual(CatalogEvidenceClaim.objects.get(
            evidence_batch=batch, claim_id="r2b_mark2_variant",
        ).attribute, "mark2_standard_passage_variant_mm")
        self.assertEqual(CatalogEvidenceClaim.objects.get(
            evidence_batch=batch, claim_id="r2b_sakhalinsk_stationless_zones",
        ).use, "topology_context")
        self.assertEqual(CatalogEvidenceClaim.objects.get(
            evidence_batch=batch, claim_id="waybot_sklif_remaining_manual_tasks",
        ).use, "case_context")

        listing_text = strip_tags(pinned_catalog.content.decode())
        self.assertIn("Автономный робот для влажной уборки", listing_text)
        self.assertNotIn("Fвтономный", listing_text)
        passage_conflict = CatalogEvidenceClaim.objects.get(
            evidence_batch=batch, claim_id="waybot_400pro_passage_conflict",
        )
        self.assertIsNone(passage_conflict.value)
        self.assertEqual(passage_conflict.use, "blocked")
        conflicted = CatalogEvidenceClaim.objects.get(evidence_batch=batch, claim_id="waybot_cleanbotics_runtime_conflict")
        self.assertIsNone(conflicted.value)
        self.assertEqual(conflicted.use, "blocked")
        again, created = import_evidence(content, source_label=Path(claims_path).name, expected_checksum=checksum)
        self.assertFalse(created)
        self.assertEqual(again.pk, batch.pk)
        self.assertEqual(CatalogEvidenceBatch.objects.count(), 1)
        with self.assertRaisesMessage(EvidenceImportError, "Контрольная сумма"):
            import_evidence(content, source_label=Path(claims_path).name, expected_checksum="0" * 64)
        altered = json.loads(content)
        alternate = next(
            item["catalog_refs"][0]["external_id"] for item in altered
            if item["catalog_refs"] and item["catalog_refs"][0]["external_id"] != altered[0]["catalog_refs"][0]["external_id"]
        )
        altered[0]["catalog_refs"][0]["external_id"] = alternate
        corrupt = json.dumps(altered, ensure_ascii=False).encode("utf-8")
        with self.assertRaisesMessage(EvidenceImportError, "ссылка на модель"):
            import_evidence(
                corrupt, source_label=Path(claims_path).name,
                expected_checksum=hashlib.sha256(corrupt).hexdigest(),
            )
        self.assertEqual(CatalogEvidenceBatch.objects.count(), 1)

    def test_malformed_evidence_fields_fail_with_domain_error(self):
        claims = json.loads(Path(os.environ["RESEARCH_CLAIMS_PATH"]).read_text(encoding="utf-8"))
        for field in ("object", "attribute", "unit", "status", "use"):
            with self.subTest(field=field):
                original = claims[0][field]
                claims[0][field] = []
                content = json.dumps(claims, ensure_ascii=False).encode("utf-8")
                with self.assertRaises(EvidenceImportError):
                    parse_evidence(content, expected_checksum=hashlib.sha256(content).hexdigest())
                claims[0][field] = original

    def test_conflicting_claim_cannot_be_promoted_to_matching_limit(self):
        claims = json.loads(Path(os.environ["RESEARCH_CLAIMS_PATH"]).read_text(encoding="utf-8"))
        product = next(item for item in claims if item["id"] == "r2b_mark2se_runtime_product_conflict")
        product.update(value=3, status="manufacturer_spec", use="matching_limit")
        content = json.dumps(claims, ensure_ascii=False).encode("utf-8")
        with self.assertRaisesMessage(EvidenceImportError, "Противоречивая характеристика"):
            parse_evidence(content, expected_checksum=hashlib.sha256(content).hexdigest())
