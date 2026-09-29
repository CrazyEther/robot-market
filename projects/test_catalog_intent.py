"""Source-backed coverage for the public catalog-to-project hint journey."""

import hashlib
import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse

from catalog.evidence_importing import import_evidence
from catalog.importing import import_catalog
from catalog.models import (
    CatalogEvidenceClaim, CatalogSourceRow, SupplementPublication,
)
from catalog.publication import current_source_pair, visible_families
from catalog.supplement_importing import import_supplement
from catalog.supplement_publication import publish_supplement
from projects.catalog_intent import make_catalog_intent_token
from projects.matching import match_candidates
from projects.models import Project, ProjectRevision
from projects.task_profiles import process_for


class CatalogIntentJourneyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        catalog_path = Path(os.environ["CATALOG_SOURCE_PATH"])
        evidence_path = Path(os.environ["RESEARCH_CLAIMS_PATH"])
        manifest_path = Path(os.environ["SUPPLEMENT_MANIFEST_PATH"])
        asset_dir = Path(os.environ["SUPPLEMENT_ASSET_DIR"])
        catalog_bytes = catalog_path.read_bytes()
        cls.catalog, _ = import_catalog(
            catalog_bytes, source_label=catalog_path.name,
            source_kind="organizer_v4",
            expected_checksum=hashlib.sha256(catalog_bytes).hexdigest(),
        )
        evidence_bytes = evidence_path.read_bytes()
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
                raise RuntimeError(f"Не найден единственный снимок источника {source['source_ref']}")
            assets[source["source_ref"]] = matches[0].read_bytes()
        cls.supplement, _ = import_supplement(
            manifest, expected_checksum=hashlib.sha256(manifest).hexdigest(),
            source_assets=assets, source_label=manifest_path.name,
            rights_basis="Проверка опубликованного source-backed каталога",
            actor_name="catalog-intent-tests",
        )
        cls.owner = get_user_model().objects.create_user(username="catalog-intent-owner")
        cls.publisher = get_user_model().objects.create_user(
            username="catalog-intent-publisher", is_staff=True,
        )
        cls.publisher.user_permissions.add(Permission.objects.get(
            content_type__app_label="catalog",
            codename="change_supplementpublication",
        ))
        publish_supplement(
            checksum=cls.supplement.checksum, actor=cls.publisher,
            reason="Публикация проверенной версии для целевого теста пути",
        )

    def setUp(self):
        self.client.force_login(self.owner)

    def _v4_case(self):
        _, evidence = current_source_pair()
        claims = CatalogEvidenceClaim.objects.filter(
            evidence_batch=evidence, use="candidate_application",
        ).order_by("object_slug", "claim_id")
        for claim in claims:
            process = process_for(claim.object_slug, claim.value)
            if process is None:
                continue
            family = CatalogSourceRow.objects.filter(
                evidence_claims=claim,
            ).select_related("family").first()
            if family and any(
                item["family"].pk == family.family_id
                for item in match_candidates(evidence, process, None)
            ) and visible_families(evidence.catalog_batch, evidence).filter(
                pk=family.family_id,
            ).exists():
                token = make_catalog_intent_token(
                    claim.object_slug, process.code, "organizer_v4",
                    str(family.family_id), evidence.checksum,
                )
                return claim.object_slug, process, family.family, token
        self.fail("Нет опубликованного v4 применения, доступного в matching")

    def _journey(self, create_url, process, token, name):
        created = self.client.post(create_url, {
            "name": name, "catalog_model": token,
            "catalog_process": process.code,
        })
        self.assertEqual(created.status_code, 302)
        project = Project.objects.get(name=name)
        task_url = urlparse(created["Location"])
        query = parse_qs(task_url.query)
        self.assertEqual(query["process"], [process.code])
        self.assertEqual(query["model"], [token])
        task = self.client.get(created["Location"])
        self.assertContains(task, process.title)
        saved = self.client.post(reverse("project_task", args=[project.pk]), {
            "process": process.code, "base_revision": "1",
            "catalog_model": token,
        })
        self.assertEqual(saved.status_code, 302)
        self.assertIn("model=", saved["Location"])
        result = self.client.get(saved["Location"])
        self.assertEqual(result.status_code, 200)
        self.assertIsNone(project.revisions.get(number=2).scenario_snapshot.get(
            "robot_selection",
        ))
        hinted = [item for item in result.context["matches"] if item["is_hinted"]]
        self.assertTrue(hinted)
        self.assertTrue(result.context["matches"][0]["is_hinted"])
        self.assertContains(result, "проверьте применимость")
        self.assertNotEqual(hinted[0]["status"], "fit", "hint must not assign fit")
        return project, result, hinted[0]

    def test_v4_card_survives_registration_create_and_task_with_verified_hint(self):
        slug, process, family, _ = self._v4_case()
        current_batch, evidence = current_source_pair()
        detail_url = reverse("catalog_family_detail", args=[family.pk])
        detail = self.client.get(detail_url, {
            "batch": current_batch.checksum, "evidence": evidence.checksum,
        })
        self.assertEqual(detail.status_code, 200)
        application = next(
            app for app in detail.context["applications"]
            if app["object_slug"] == slug and app["title"] == process.title
        )
        token = parse_qs(urlparse(application["project_url"]).query)["model"][0]

        self.client.logout()
        login_redirect = self.client.get(application["project_url"])
        self.assertEqual(login_redirect.status_code, 302)
        self.assertIn("next=", login_redirect["Location"])
        registration_url = reverse("register") + "?" + urlencode({
            "next": application["project_url"],
        })
        registered = self.client.post(registration_url, {
            "username": "registered-catalog-owner",
            "password1": "Safe-test-password-493!",
            "password2": "Safe-test-password-493!",
        })
        self.assertEqual(registered.status_code, 302)
        create_page = self.client.get(registered["Location"])
        self.assertContains(create_page, family.name)
        self._journey(
            urlparse(registered["Location"]).path, process, token,
            "Проверка из карточки v4",
        )

    def test_supplement_card_keeps_published_product_hint_through_task(self):
        product = self.supplement.products.filter(
            applications__object_slug="hospital",
        ).distinct().order_by("product_ref").first()
        self.assertIsNotNone(product)
        application = product.applications.filter(
            object_slug="hospital",
        ).order_by("process_code").first()
        process = process_for("hospital", application.process_code)
        token = make_catalog_intent_token(
            "hospital", process.code, "manufacturer_supplement",
            product.product_ref, self.supplement.checksum,
        )
        detail = self.client.get(reverse("catalog_supplement_detail", args=[
            self.supplement.checksum, product.product_ref,
        ]))
        self.assertEqual(detail.status_code, 200)
        self.assertTrue(any(
            parse_qs(urlparse(item.get("project_url", "")).query).get("model") == [token]
            for item in detail.context["applications"]
        ))
        project, result, hinted = self._journey(
            reverse("project_create", args=["hospital"])
            + "?process=" + process.code + "&model=" + token,
            process, token, "Проверка из карточки дополнения",
        )
        self.assertEqual(hinted["source_kind"], "manufacturer_supplement")
        self.assertEqual(hinted["product_ref"], product.product_ref)
        self.assertTrue(result.context["catalog_intent"])
        self.assertEqual(project.object_slug, "hospital")

    def test_tampered_foreign_and_stale_intents_fail_closed(self):
        slug, process, _, token = self._v4_case()
        create_url = reverse("project_create", args=[slug])
        altered = token[:-1] + ("a" if token[-1] != "a" else "b")
        self.assertEqual(self.client.get(create_url, {
            "process": process.code, "model": altered,
        }).status_code, 404)
        other_slug = "airport" if slug != "airport" else "hospital"
        self.assertEqual(self.client.get(reverse("project_create", args=[other_slug]), {
            "process": process.code, "model": token,
        }).status_code, 404)

        project_response = self.client.post(create_url, {"name": "Устаревший hint"})
        project = Project.objects.get(name="Устаревший hint")
        revision = project.revisions.get(number=1)
        snapshot = dict(revision.scenario_snapshot)
        snapshot["evidence_checksum"] = "0" * 64
        ProjectRevision.objects.filter(pk=revision.pk).update(scenario_snapshot=snapshot)
        task_url = reverse("project_task", args=[project.pk])
        self.assertEqual(self.client.get(task_url, {
            "process": process.code, "model": token,
        }).status_code, 404)
        self.assertEqual(self.client.post(task_url, {
            "process": process.code, "base_revision": "1",
            "catalog_model": altered,
        }).status_code, 409)
        self.assertEqual(project_response.status_code, 302)

    def test_legacy_project_creation_without_intent_is_unchanged(self):
        response = self.client.post(reverse("project_create", args=["warehouse"]), {
            "name": "Обычный проект без каталога",
        })
        self.assertEqual(response.status_code, 302)
        project = Project.objects.get(name="Обычный проект без каталога")
        self.assertEqual(response["Location"], reverse("project_detail", args=[project.pk]))
        self.assertNotIn("model=", response["Location"])
