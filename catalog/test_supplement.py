"""Supplement integration checks against privately archived primary files."""

import hashlib
import json
import os
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from django.core.management import call_command
from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import Client, TestCase, TransactionTestCase
from django.test import override_settings
from django.utils.html import strip_tags

from catalog.models import (
    CatalogBatch, SupplementApplication, SupplementAuditEvent, SupplementBatch,
    SupplementOffer, SupplementProduct, SupplementPublication,
    SupplementSourceArtifact, SupplementSpecification,
)
from catalog.bootstrap_supplement import acquire_supplement
from catalog.evidence_importing import import_evidence
from catalog.importing import import_catalog
from catalog.publication import current_source_pair
from catalog.supplement_importing import (
    SupplementImportError, import_supplement, parse_supplement,
)
from catalog.supplement_publication import (
    SupplementPublicationError, publicly_available_supplement, publish_supplement,
)
from projects.models import FinancePlan, Project, SimulationRun
from projects.forms import WorkloadProfileForm
from projects.selection_refs import selection_key, selection_ref, workload_matches_selection
from projects.sizing import size_project
from projects.task_profiles import process_for
from django.urls import reverse


class SupplementSourceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        manifest_path = os.environ.get("SUPPLEMENT_MANIFEST_PATH")
        asset_dir = os.environ.get("SUPPLEMENT_ASSET_DIR")
        if not manifest_path or not asset_dir:
            raise RuntimeError(
                "SUPPLEMENT_MANIFEST_PATH и SUPPLEMENT_ASSET_DIR обязательны для проверки первичных источников"
            )
        cls.manifest_path = Path(manifest_path)
        cls.content = cls.manifest_path.read_bytes()
        cls.document = json.loads(cls.content)
        cls.asset_paths = {}
        cls.assets = {}
        for source in cls.document["sources"]:
            matches = list(Path(asset_dir).glob(f"*{source['sha256']}*"))
            if len(matches) != 1:
                raise RuntimeError(f"Нужен один сохранённый снимок {source['source_ref']}")
            cls.asset_paths[source["source_ref"]] = matches[0]
            cls.assets[source["source_ref"]] = matches[0].read_bytes()
        cls.checksum = hashlib.sha256(cls.content).hexdigest()

    def _import(self):
        return import_supplement(
            self.content, expected_checksum=self.checksum, source_assets=self.assets,
            source_label=self.manifest_path.name,
            rights_basis="Закрытая исследовательская обработка первичных сведений",
            actor_name="source-research-operator",
        )

    def _upload_payload(self, *, checksum=None, changed_source=False):
        assets = []
        for index, raw in enumerate(self.assets.values()):
            if index == 0 and changed_source:
                raw = raw[:-1]
            assets.append(SimpleUploadedFile(f"primary-{index}.bin", raw))
        return {
            "action": "import",
            "manifest_file": SimpleUploadedFile(self.manifest_path.name, self.content),
            "expected_checksum": checksum or self.checksum,
            "source_assets": assets,
            "rights_basis": "Закрытая исследовательская обработка первичных сведений",
            "rights_attested": "on",
        }

    def test_admin_upload_and_separate_publication_use_exact_primary_bytes(self):
        url = reverse("catalog_supplement_management")
        self.assertEqual(self.client.get(url).status_code, 302)
        user_model = get_user_model()
        ordinary = user_model.objects.create_user(username="source-upload-reader")
        self.client.force_login(ordinary)
        self.assertEqual(self.client.get(url).status_code, 403)
        staff = user_model.objects.create_user(username="source-upload-operator", is_staff=True)
        self.client.force_login(staff)
        self.assertEqual(self.client.get(url).status_code, 403)
        staff.user_permissions.add(Permission.objects.get(
            content_type__app_label="catalog", codename="add_supplementbatch",
        ))
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.post(url, self._upload_payload(
            checksum="0" * 64,
        )).status_code, 400)
        self.assertEqual(self.client.post(url, self._upload_payload(
            changed_source=True,
        )).status_code, 400)
        without_rights = self._upload_payload()
        without_rights.pop("rights_attested")
        self.assertEqual(self.client.post(url, without_rights).status_code, 400)
        with_extra_file = self._upload_payload()
        with_extra_file["source_assets"].append(SimpleUploadedFile(
            "extra.bin", self.content,
        ))
        self.assertEqual(self.client.post(url, with_extra_file).status_code, 400)
        self.assertEqual(SupplementBatch.objects.count(), 0)
        self.assertEqual(SupplementAuditEvent.objects.count(), 0)
        self.assertEqual(self.client.post(url, self._upload_payload()).status_code, 302)
        self.assertEqual(SupplementBatch.objects.get().checksum, self.checksum)
        self.assertEqual(SupplementAuditEvent.objects.filter(kind="import").count(), 1)
        self.assertEqual(SupplementPublication.objects.count(), 0)
        self.assertEqual(self.client.post(url, {
            "action": "publish", "checksum": self.checksum,
            "reason": "Право публичного отображения сведений подтверждено оператором",
        }).status_code, 403)
        staff.user_permissions.add(Permission.objects.get(
            content_type__app_label="catalog", codename="change_supplementpublication",
        ))
        self.assertEqual(self.client.post(url, {
            "action": "publish", "checksum": self.checksum,
            "reason": "Право публичного отображения сведений подтверждено оператором",
        }).status_code, 400)
        self.assertEqual(self.client.post(url, {
            "action": "publish", "checksum": self.checksum, "reason": "коротко",
            "publication_rights_attested": "on",
        }).status_code, 400)
        self.assertEqual(SupplementPublication.objects.count(), 0)
        self.assertEqual(self.client.post(url, {
            "action": "publish", "checksum": self.checksum,
            "reason": "Право публичного отображения сведений подтверждено оператором",
            "publication_rights_attested": "on",
        }).status_code, 302)
        self.assertEqual(SupplementPublication.objects.get().batch.checksum, self.checksum)
        self.assertEqual(SupplementAuditEvent.objects.filter(kind="publish").count(), 1)
        product = SupplementProduct.objects.get(batch__checksum=self.checksum,
                                                product_ref="aethon_t3")
        self.assertEqual(self.client.get(reverse("catalog_supplement_detail", args=[
            self.checksum, product.product_ref,
        ])).status_code, 200)

    def test_admin_upload_requires_csrf_token(self):
        user = get_user_model().objects.create_user(username="source-csrf-operator", is_staff=True)
        user.user_permissions.add(Permission.objects.get(
            content_type__app_label="catalog", codename="add_supplementbatch",
        ))
        client = Client(enforce_csrf_checks=True)
        client.force_login(user)
        self.assertEqual(client.post(reverse("catalog_supplement_management"),
                                     self._upload_payload()).status_code, 403)
        self.assertEqual(SupplementBatch.objects.count(), 0)

    def test_real_sources_are_archived_without_publishing_or_touching_v4(self):
        batch, created = self._import()
        self.assertTrue(created)
        self.assertEqual(batch.checksum, self.checksum)
        self.assertEqual(bytes(batch.raw_source), self.content)
        self.assertEqual(CatalogBatch.objects.count(), 0)
        self.assertEqual(SupplementBatch.objects.count(), 1)
        self.assertEqual(SupplementPublication.objects.count(), 0)
        self.assertEqual(SupplementProduct.objects.count(), len(self.document["products"]))
        self.assertEqual(SupplementApplication.objects.count(), sum(
            len(product["applications"]) for product in self.document["products"]
        ))
        self.assertEqual(SupplementSourceArtifact.objects.count(), len(self.document["sources"]))
        for source in batch.source_artifacts.all():
            self.assertEqual(hashlib.sha256(bytes(source.raw_source)).hexdigest(), source.checksum)
        self.assertEqual(SupplementOffer.objects.count(), 0)
        specs = {item.attribute: item for item in SupplementSpecification.objects.all()}
        self.assertEqual(specs["payload_kg"].value, Decimal("340"))
        self.assertEqual(specs["max_cart_length_mm"].value, Decimal("1117.6"))
        self.assertEqual(specs["max_cart_width_mm"].value, Decimal("813"))
        self.assertEqual(specs["manufacturer_max_speed_m_s"].value, Decimal("0.76"))
        self.assertEqual(specs["payload_kg"].source_page, 2)

    def test_same_bytes_are_idempotent_but_each_import_is_audited(self):
        first, created = self._import()
        self.assertTrue(created)
        second, created = self._import()
        self.assertFalse(created)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(SupplementProduct.objects.count(), len(self.document["products"]))
        self.assertEqual(list(SupplementAuditEvent.objects.values_list("created_new", flat=True)),
                         [False, True])

    def test_changed_normalized_spec_blocks_reimport_and_publication(self):
        batch, _ = self._import()
        spec = batch.products.get(product_ref="aethon_t3").specifications.get(attribute="payload_kg")
        SupplementSpecification.objects.filter(pk=spec.pk).update(status="conflicted")
        with self.assertRaises(SupplementImportError):
            self._import()
        self.assertEqual(SupplementAuditEvent.objects.filter(kind="import").count(), 1)
        publisher = get_user_model().objects.create_user(username="source-integrity-publisher", is_staff=True)
        publisher.user_permissions.add(Permission.objects.get(
            content_type__app_label="catalog", codename="change_supplementpublication",
        ))
        with self.assertRaises(SupplementPublicationError):
            publish_supplement(
                checksum=batch.checksum, actor=publisher,
                reason="Проверка целостности сведений перед публикацией",
            )
        self.assertEqual(SupplementPublication.objects.count(), 0)
        SupplementSpecification.objects.filter(pk=spec.pk).update(status=spec.status)
        application = batch.products.get(product_ref="aethon_t3").applications.get()
        different_source = batch.source_artifacts.get(source_ref="aethon_t3_specification")
        SupplementApplication.objects.filter(pk=application.pk).update(source=different_source)
        with self.assertRaises(SupplementPublicationError):
            publish_supplement(
                checksum=batch.checksum, actor=publisher,
                reason="Проверка связи области применения с источником",
            )
        SupplementApplication.objects.filter(pk=application.pk).update(source=application.source)
        self.assertTrue(publish_supplement(
            checksum=batch.checksum, actor=publisher,
            reason="Сведения сверены с сохранёнными первичными файлами",
        )[1])
        self.assertIsNotNone(publicly_available_supplement(batch.checksum))
        SupplementSpecification.objects.filter(pk=spec.pk).update(status="conflicted")
        self.assertIsNone(publicly_available_supplement(batch.checksum))

    def test_invalid_checksum_source_and_variant_leave_no_partial_batch(self):
        with self.assertRaises(SupplementImportError):
            import_supplement(
                self.content, expected_checksum="0" * 64, source_assets=self.assets,
                source_label=self.manifest_path.name, rights_basis="Закрытая проверка",
                actor_name="source-research-operator",
            )
        changed_assets = dict(self.assets)
        source_ref = next(iter(changed_assets))
        changed_assets[source_ref] = changed_assets[source_ref][:-1]
        with self.assertRaises(SupplementImportError):
            parse_supplement(self.content, expected_checksum=self.checksum,
                             source_assets=changed_assets)
        duplicated = json.loads(self.content)
        duplicated["products"].append(dict(duplicated["products"][0]))
        payload = json.dumps(duplicated, ensure_ascii=False).encode("utf-8")
        with self.assertRaises(SupplementImportError):
            parse_supplement(payload, expected_checksum=hashlib.sha256(payload).hexdigest(),
                             source_assets=self.assets)
        wrong_unit = json.loads(self.content)
        wrong_unit["products"][0]["specifications"][0]["source_unit"] = "cm"
        payload = json.dumps(wrong_unit, ensure_ascii=False).encode("utf-8")
        with self.assertRaises(SupplementImportError):
            parse_supplement(payload, expected_checksum=hashlib.sha256(payload).hexdigest(),
                             source_assets=self.assets)
        self.assertEqual(SupplementBatch.objects.count(), 0)
        self.assertEqual(SupplementAuditEvent.objects.count(), 0)

    def test_cli_reads_exact_primary_files_and_does_not_publish(self):
        bindings = [f"{ref}={path}" for ref, path in self.asset_paths.items()]
        arguments = [str(self.manifest_path), "--expected-sha256", self.checksum]
        for binding in bindings:
            arguments.extend(("--source-asset", binding))
        arguments.extend(("--rights-basis", "Закрытая исследовательская обработка первичных сведений"))
        call_command("import_supplement", *arguments, verbosity=0)
        self.assertEqual(SupplementBatch.objects.count(), 1)
        self.assertEqual(SupplementPublication.objects.count(), 0)

    def test_acquisition_rechecks_archived_bytes_without_network(self):
        with TemporaryDirectory() as directory:
            asset_dir = Path(directory)
            for source in self.document["sources"]:
                (asset_dir / f"{source['sha256']}.bin").write_bytes(
                    self.assets[source["source_ref"]]
                )
            manifest, artifacts = acquire_supplement(
                manifest_path=self.manifest_path, manifest_url="",
                expected_checksum=self.checksum, asset_dir=asset_dir,
            )
            self.assertEqual(manifest, self.manifest_path)
            self.assertEqual(len(list(artifacts.iterdir())), len(self.document["sources"]))

    def test_publication_requires_privilege_and_keeps_both_source_versions(self):
        first, _ = self._import()
        user_model = get_user_model()
        ordinary = user_model.objects.create_user(username="supplement-reader")
        with self.assertRaises(PermissionDenied):
            publish_supplement(checksum=first.checksum, actor=ordinary,
                               reason="Проверка прав публикации сведений")
        self.assertEqual(SupplementPublication.objects.count(), 0)
        admin = user_model.objects.create_user(username="supplement-editor", is_staff=True)
        admin.user_permissions.add(Permission.objects.get(
            content_type__app_label="catalog", codename="change_supplementpublication",
        ))
        with self.assertRaises(PermissionDenied):
            publish_supplement(checksum=first.checksum, actor=ordinary,
                               reason="Проверка прав публикации сведений")
        self.assertTrue(publish_supplement(
            checksum=first.checksum, actor=admin,
            reason="Уполномоченный оператор подтверждает право на метаданные",
        )[1])
        reformatted = json.dumps(self.document, ensure_ascii=False, indent=2).encode("utf-8")
        second, created = import_supplement(
            reformatted, expected_checksum=hashlib.sha256(reformatted).hexdigest(),
            source_assets=self.assets, source_label=self.manifest_path.name,
            rights_basis="Закрытая исследовательская обработка первичных сведений",
            actor_name="source-research-operator",
        )
        self.assertTrue(created)
        self.assertNotEqual(first.checksum, second.checksum)
        self.assertEqual(SupplementPublication.objects.get().batch_id, first.pk)
        publish_supplement(
            checksum=second.checksum, actor=admin,
            reason="Уполномоченный оператор обновляет опубликованные метаданные",
        )
        publish_supplement(
            checksum=first.checksum, actor=admin,
            reason="Уполномоченный оператор возвращает прежние метаданные",
        )
        self.assertEqual(SupplementPublication.objects.get().batch_id, first.pk)
        self.assertEqual(SupplementBatch.objects.count(), 2)
        self.assertEqual(SupplementAuditEvent.objects.filter(kind="publish").count(), 3)


class SupplementProjectSelectionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        catalog_path = Path(os.environ["CATALOG_SOURCE_PATH"])
        evidence_path = Path(os.environ["RESEARCH_CLAIMS_PATH"])
        manifest_path = Path(os.environ["SUPPLEMENT_MANIFEST_PATH"])
        asset_dir = Path(os.environ["SUPPLEMENT_ASSET_DIR"])
        catalog_bytes = catalog_path.read_bytes()
        evidence_bytes = evidence_path.read_bytes()
        cls.catalog, _ = import_catalog(
            catalog_bytes, source_label=catalog_path.name, source_kind="organizer_v4",
            expected_checksum=hashlib.sha256(catalog_bytes).hexdigest(),
        )
        cls.evidence, _ = import_evidence(
            evidence_bytes, source_label=evidence_path.name,
            expected_checksum=hashlib.sha256(evidence_bytes).hexdigest(),
        )
        manifest = manifest_path.read_bytes()
        document = json.loads(manifest)
        assets = {}
        for source in document["sources"]:
            matches = list(asset_dir.glob(f"*{source['sha256']}*"))
            if len(matches) != 1:
                raise RuntimeError(f"Нужен точный снимок источника {source['source_ref']}")
            assets[source["source_ref"]] = matches[0].read_bytes()
        cls.supplement, _ = import_supplement(
            manifest, expected_checksum=hashlib.sha256(manifest).hexdigest(),
            source_assets=assets, source_label=manifest_path.name,
            rights_basis="Закрытая исследовательская обработка первичных сведений",
            actor_name="source-research-operator",
        )
        cls.product = cls.supplement.products.get(product_ref="aethon_t3")
        cls.owner = get_user_model().objects.create_user(username="selection-owner")
        cls.other = get_user_model().objects.create_user(username="selection-other")
        cls.publisher = get_user_model().objects.create_user(
            username="selection-publisher", is_staff=True,
        )
        cls.publisher.user_permissions.add(Permission.objects.get(
            content_type__app_label="catalog", codename="change_supplementpublication",
        ))

    def setUp(self):
        self.client.force_login(self.owner)

    def _publish(self):
        publish_supplement(
            checksum=self.supplement.checksum, actor=self.publisher,
            reason="Проверка маршрута публикации на сохранённых первичных сведениях",
        )

    def _create_hospital_project(self):
        response = self.client.post(
            reverse("project_create", args=["hospital"]), {"name": "Логистика питания"},
        )
        self.assertEqual(response.status_code, 302)
        project = Project.objects.get(owner=self.owner, name="Логистика питания")
        self.assertEqual(self.client.post(reverse("project_task", args=[project.id]), {
            "process": "hospital_meal_delivery", "base_revision": "1",
        }).status_code, 302)
        return project

    def test_exact_product_card_requires_publication_and_hides_source_internals(self):
        url = reverse("catalog_supplement_detail", args=[
            self.supplement.checksum, self.product.product_ref,
        ])
        self.assertEqual(self.client.get(url).status_code, 404)
        self._publish()
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.product.model)
        self.assertContains(response, self.product.product_url)
        self.assertContains(response, "Стоимость по запросу")
        self.assertContains(response, "Создать проект")
        self.assertNotContains(response, self.supplement.checksum)
        self.assertNotContains(response, "supplement_products")
        self.assertEqual(self.client.get(reverse("catalog_supplement_detail", args=[
            self.supplement.checksum, "unknown_product",
        ])).status_code, 404)

    def test_published_products_join_search_without_borrowing_v4_prices(self):
        exact_url = reverse("catalog_supplement_detail", args=[
            self.supplement.checksum, self.product.product_ref,
        ])
        self.assertNotContains(self.client.get(reverse("catalog_index")), exact_url)
        self._publish()
        catalog = self.client.get(reverse("catalog_index"))
        self.assertContains(catalog, exact_url)
        self.assertEqual(catalog.context["total_count"],
                         catalog.context["page"].paginator.count)
        self.assertEqual(len(catalog.context["supplement_cards"]), self.supplement.product_count)
        self.assertContains(catalog, "Стоимость по запросу")
        self.assertNotIn(self.supplement.checksum, strip_tags(catalog.content.decode()))
        self.assertContains(self.client.get(reverse("catalog_index"), {
            "q": self.product.model,
        }), exact_url)
        self.assertContains(self.client.get(reverse("catalog_index"), {
            "category": "Доставка питания",
        }), exact_url)
        self.assertContains(self.client.get(reverse("catalog_index"), {
            "industry": "Медицинское учреждение",
        }), exact_url)
        self.assertNotContains(self.client.get(reverse("catalog_index"), {
            "q": "nonexistent-exact-product-name",
        }), exact_url)
        with override_settings(CATALOG_PAGE_SIZE=1):
            first_page = self.client.get(reverse("catalog_index"))
            pages = [first_page] + [self.client.get(reverse("catalog_index"), {
                "page": number,
            }) for number in range(2, first_page.context["page"].paginator.num_pages + 1)]
            for product in self.supplement.products.all():
                url = reverse("catalog_supplement_detail", args=[
                    self.supplement.checksum, product.product_ref,
                ])
                self.assertEqual(sum(url in page.content.decode() for page in pages), 1)

    def test_only_published_pinned_product_can_be_selected_without_numeric_result(self):
        project = self._create_hospital_project()
        task_url = reverse("project_task", args=[project.id])
        choice_url = reverse("project_robot_selection", args=[project.id])
        self.assertNotContains(self.client.get(task_url), self.product.product_ref)
        self.assertEqual(self.client.post(choice_url, {
            "action": "select", "source_kind": "manufacturer_supplement",
            "product_ref": self.product.product_ref, "base_revision": "2",
        }).status_code, 409)
        self._publish()
        self.assertNotContains(self.client.get(task_url), self.product.product_ref)
        self.assertEqual(self.client.post(choice_url, {
            "action": "select", "source_kind": "manufacturer_supplement",
            "product_ref": self.product.product_ref, "base_revision": "2",
        }).status_code, 409)
        refresh_url = reverse("project_supplement_refresh", args=[project.id])
        self.assertContains(self.client.get(task_url), "Обновить подбор моделей")
        self.assertEqual(self.client.post(refresh_url, {"base_revision": "1"}).status_code, 409)
        self.assertEqual(self.client.post(refresh_url, {"base_revision": "2"}).status_code, 302)
        self.assertEqual(project.revisions.get(number=3).scenario_snapshot["supplement_checksum"],
                         self.supplement.checksum)
        self.assertContains(self.client.get(task_url), self.product.product_url)
        self.assertEqual(self.client.post(choice_url, {
            "action": "select", "source_kind": "manufacturer_supplement",
            "product_ref": "not_in_published_source", "base_revision": "3",
        }).status_code, 400)
        self.assertEqual(self.client.post(choice_url, {
            "action": "select", "source_kind": "manufacturer_supplement",
            "product_ref": self.product.product_ref, "base_revision": "3",
        }).status_code, 302)
        snapshot = project.revisions.get(number=4).scenario_snapshot
        selected = snapshot["robot_selection"]
        self.assertEqual(selected["status"], "requires_verification")
        self.assertEqual(selected["source_specifications"][0]["source_sha256"],
                         self.supplement.source_artifacts.get(
                             source_ref="aethon_t3_specification",
                         ).checksum)
        self.assertEqual(selection_ref(selected, snapshot), selected["selection_ref"])
        self.assertEqual(selection_key(selected["selection_ref"]),
                         selection_key(selection_ref(selected, snapshot)))
        self.assertNotIn("record_index", selected)
        self.assertIsNone(size_project(snapshot)["fleet"])
        self.assertEqual(self.client.post(reverse("project_simulation", args=[project.id]), {
            "base_revision": "4",
        }).status_code, 409)
        self.assertEqual(SimulationRun.objects.filter(project=project).count(), 0)
        self.assertEqual(FinancePlan.objects.filter(project=project).count(), 0)
        process = process_for("hospital", "hospital_meal_delivery")
        workload_form = WorkloadProfileForm(
            {}, process=process, selection=selected, selection_ref=selected["selection_ref"],
        )
        self.assertTrue(workload_form.is_valid())
        workload = workload_form.to_profile(
            user_id=self.owner.pk, recorded_at=project.revisions.get(number=4).created_at,
        )
        self.assertNotIn("robot_record_index", workload)
        self.assertTrue(workload_matches_selection(workload, selected, snapshot))
        with_workload = deepcopy(snapshot)
        with_workload["workload_profile"] = workload
        self.assertIsNone(size_project(with_workload)["fleet"])
        self.assertIn("Подтвердите обязательные характеристики",
                      size_project(with_workload)["reasons"][0])
        changed_source = deepcopy(snapshot)
        changed_source["supplement_checksum"] = self.catalog.checksum
        self.assertIsNone(selection_ref(selected, changed_source))
        self.assertFalse(workload_matches_selection(workload, selected, changed_source))
        self.assertNotIn("supplement_checksum", project.revisions.get(number=2).scenario_snapshot)
        self.assertNotIn("robot_selection", project.revisions.get(number=3).scenario_snapshot)
        self.assertEqual(self.client.post(choice_url, {
            "action": "select", "source_kind": "manufacturer_supplement",
            "product_ref": self.product.product_ref, "base_revision": "3",
        }).status_code, 409)
        self.client.force_login(self.other)
        self.assertEqual(self.client.post(choice_url, {
            "action": "remove", "base_revision": "4",
        }).status_code, 404)

    def test_published_product_is_scoped_to_its_documented_process(self):
        self._publish()
        for slug, process in (("warehouse", "warehouse_pallet_transfer"),
                              ("airport", "airport_baggage_transport")):
            with self.subTest(object=slug):
                self.assertEqual(self.client.post(
                    reverse("project_create", args=[slug]), {"name": slug},
                ).status_code, 302)
                project = Project.objects.get(owner=self.owner, name=slug)
                self.assertEqual(project.revisions.get(number=1).scenario_snapshot[
                    "supplement_checksum"], self.supplement.checksum)
                self.assertEqual(self.client.post(reverse("project_task", args=[project.id]), {
                    "process": process, "base_revision": "1",
                }).status_code, 302)
                self.assertNotContains(self.client.get(reverse("project_task", args=[project.id])),
                                       self.product.product_url)
                self.assertEqual(self.client.post(reverse("project_robot_selection", args=[project.id]), {
                    "action": "select", "source_kind": "manufacturer_supplement",
                    "product_ref": self.product.product_ref, "base_revision": "2",
                }).status_code, 400)

    def test_airport_eztow_preserves_source_and_blocks_unverified_run(self):
        eztow = self.supplement.products.filter(product_ref="tld_eztow").first()
        if eztow is None:
            self.skipTest("Этот снимок дополнения не содержит первичный источник EZTow")
        self._publish()
        self.assertEqual(self.client.post(
            reverse("project_create", args=["airport"]), {"name": "Перевозка багажа"},
        ).status_code, 302)
        project = Project.objects.get(owner=self.owner, name="Перевозка багажа")
        self.assertEqual(self.client.post(reverse("project_task", args=[project.id]), {
            "process": "airport_baggage_transport", "base_revision": "1",
        }).status_code, 302)
        task_url = reverse("project_task", args=[project.id])
        self.assertContains(self.client.get(task_url), eztow.product_url)
        self.assertEqual(self.client.post(reverse("project_robot_selection", args=[project.id]), {
            "action": "select", "source_kind": "manufacturer_supplement",
            "product_ref": eztow.product_ref, "base_revision": "2",
        }).status_code, 302)
        snapshot = project.revisions.get(number=3).scenario_snapshot
        selected = snapshot["robot_selection"]
        self.assertEqual(selected["status"], "requires_verification")
        self.assertEqual(selected["selection_ref"]["product_ref"], "tld_eztow")
        self.assertEqual(selected["source_specifications"][0]["attribute"], "drawbar_pull_n")
        self.assertEqual(Decimal(selected["source_specifications"][0]["value"]), Decimal("20000"))
        self.assertEqual(selected["source_specifications"][0]["unit"], "N")
        tampered = deepcopy(snapshot)
        tampered["robot_selection"]["selection_ref"]["product_ref"] = "aethon_t3"
        self.assertIsNone(selection_ref(tampered["robot_selection"], tampered))
        self.assertIsNone(size_project(tampered)["fleet"])
        self.assertIsNone(size_project(snapshot)["fleet"])
        self.assertEqual(self.client.post(reverse("project_simulation", args=[project.id]), {
            "base_revision": "3",
        }).status_code, 409)
        self.assertFalse(SimulationRun.objects.filter(project=project).exists())
        self.assertFalse(FinancePlan.objects.filter(project=project).exists())
        self.assertEqual(self.client.post(
            reverse("project_create", args=["hospital"]), {"name": "Доставка питания"},
        ).status_code, 302)
        hospital = Project.objects.get(owner=self.owner, name="Доставка питания")
        self.assertEqual(self.client.post(reverse("project_task", args=[hospital.id]), {
            "process": "hospital_meal_delivery", "base_revision": "1",
        }).status_code, 302)
        self.assertEqual(self.client.post(reverse("project_robot_selection", args=[hospital.id]), {
            "action": "select", "source_kind": "manufacturer_supplement",
            "product_ref": eztow.product_ref, "base_revision": "2",
        }).status_code, 400)
        self.assertEqual(hospital.revisions.count(), 2)


class SupplementMigrationTests(TransactionTestCase):
    def test_existing_v4_and_evidence_survive_additive_migration(self):
        catalog_path = os.environ.get("CATALOG_SOURCE_PATH")
        evidence_path = os.environ.get("RESEARCH_CLAIMS_PATH")
        if not catalog_path or not evidence_path:
            raise RuntimeError("Для миграции нужны настоящие CATALOG_SOURCE_PATH и RESEARCH_CLAIMS_PATH")
        previous = [("catalog", "0004_catalogpublication_catalogpublicationevent")]
        current = [("catalog", "0005_supplementbatch_supplementauditevent_and_more")]
        MigrationExecutor(connection).migrate(previous)
        try:
            catalog_bytes = Path(catalog_path).read_bytes()
            evidence_bytes = Path(evidence_path).read_bytes()
            catalog, _ = import_catalog(
                catalog_bytes, source_label=Path(catalog_path).name,
                source_kind="organizer_v4", expected_checksum=hashlib.sha256(catalog_bytes).hexdigest(),
            )
            evidence, _ = import_evidence(
                evidence_bytes, source_label=Path(evidence_path).name,
                expected_checksum=hashlib.sha256(evidence_bytes).hexdigest(),
            )
            self.assertEqual(catalog.row_count, 223)
            self.assertEqual(current_source_pair()[1].checksum, evidence.checksum)
            MigrationExecutor(connection).migrate(current)
            catalog.refresh_from_db()
            evidence.refresh_from_db()
            self.assertEqual(catalog.row_count, 223)
            self.assertEqual(current_source_pair()[1].checksum, evidence.checksum)
            self.assertEqual(SupplementBatch.objects.count(), 0)
        finally:
            MigrationExecutor(connection).migrate(current)
