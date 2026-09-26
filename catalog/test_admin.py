"""Administrator import boundaries against the supplied source files."""

import hashlib
import json
import os
from pathlib import Path

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from catalog.importing import import_catalog
from catalog.models import (
    CatalogBatch, CatalogEvidenceBatch, CatalogEvidenceClaim, CatalogImportEvent,
    CatalogPublication, CatalogPublicationEvent,
)
from catalog.publication import current_source_pair
from projects.models import Project, ProjectRevision


class CatalogAdministrationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        catalog_path = Path(os.environ["CATALOG_SOURCE_PATH"])
        evidence_path = Path(os.environ["RESEARCH_CLAIMS_PATH"])
        cls.catalog_bytes = catalog_path.read_bytes()
        cls.evidence_bytes = evidence_path.read_bytes()
        cls.catalog_name = catalog_path.name
        cls.evidence_name = evidence_path.name
        cls.catalog_batch, _ = import_catalog(
            cls.catalog_bytes, source_label=cls.catalog_name,
            source_kind="organizer_v4",
        )
        user_model = get_user_model()
        cls.user = user_model.objects.create_user(username="catalog-reader")
        cls.unprivileged_staff = user_model.objects.create_user(
            username="catalog-staff", is_staff=True,
        )
        cls.admin = user_model.objects.create_user(
            username="catalog-editor", is_staff=True,
        )
        cls.admin.user_permissions.add(*Permission.objects.filter(
            content_type__app_label="catalog",
            codename__in=("add_catalogbatch", "add_catalogevidencebatch",
                          "change_catalogpublication"),
        ))
        cls.superuser = user_model.objects.create_superuser(
            username="catalog-superuser", password="sufficiently-long-test-password",
        )

    def _upload(self, kind, content, name, *, checksum=None):
        return self.client.post(reverse("catalog_source_management"), {
            "action": "upload",
            "source_kind": kind,
            "source_file": SimpleUploadedFile(name, content),
            "expected_checksum": checksum or hashlib.sha256(content).hexdigest(),
            "rights_basis": "Источник разрешён для закрытой проверки проекта",
            "rights_attested": "on",
        })

    def test_only_staff_with_both_import_permissions_can_access(self):
        url = reverse("catalog_source_management")
        self.assertEqual(self.client.get(url).status_code, 302)
        for user in (self.user, self.unprivileged_staff):
            self.client.force_login(user)
            self.assertEqual(self.client.get(url).status_code, 403)
            self.assertEqual(self._upload(
                "catalog", self.catalog_bytes, self.catalog_name,
            ).status_code, 403)
        self.assertEqual(CatalogImportEvent.objects.count(), 0)
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(url), "Версии источников")

    def test_source_upload_requires_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin)
        response = client.post(reverse("catalog_source_management"), {
            "action": "upload",
            "source_kind": "catalog",
            "source_file": SimpleUploadedFile(self.catalog_name, self.catalog_bytes),
            "expected_checksum": hashlib.sha256(self.catalog_bytes).hexdigest(),
            "rights_basis": "Источник разрешён для закрытой проверки проекта",
            "rights_attested": "on",
        })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(CatalogImportEvent.objects.count(), 0)

    def test_imports_archive_exact_bytes_and_audit_repeated_actions(self):
        self.client.force_login(self.admin)
        CatalogBatch.objects.filter(pk=self.catalog_batch.pk).update(raw_source=None)
        self.assertEqual(self._upload(
            "catalog", self.catalog_bytes, self.catalog_name,
        ).status_code, 302)
        self.assertEqual(CatalogBatch.objects.count(), 1)
        self.assertFalse(CatalogImportEvent.objects.get().created_new)
        self.assertEqual(bytes(CatalogBatch.objects.get().raw_source), self.catalog_bytes)

        self.assertEqual(self._upload(
            "evidence", self.evidence_bytes, self.evidence_name,
        ).status_code, 302)
        batch = CatalogEvidenceBatch.objects.get()
        self.assertEqual(bytes(batch.raw_source), self.evidence_bytes)
        self.assertEqual(batch.catalog_batch_id, self.catalog_batch.id)
        self.assertTrue(CatalogImportEvent.objects.filter(
            checksum=batch.checksum, created_new=True, actor=self.admin,
        ).exists())
        project = Project.objects.create(owner=self.user, name="Исходная редакция", object_slug="warehouse")
        revision = ProjectRevision.objects.create(project=project, number=1, scenario_snapshot={
            "catalog_checksum": self.catalog_batch.checksum,
            "evidence_checksum": batch.checksum,
        })
        CatalogEvidenceBatch.objects.filter(pk=batch.pk).update(raw_source=None)
        self.assertEqual(self._upload(
            "evidence", self.evidence_bytes, self.evidence_name,
        ).status_code, 302)
        self.assertEqual(CatalogEvidenceBatch.objects.count(), 1)
        self.assertEqual(bytes(CatalogEvidenceBatch.objects.get().raw_source), self.evidence_bytes)
        self.assertEqual(CatalogImportEvent.objects.filter(checksum=batch.checksum).count(), 2)
        revision.refresh_from_db()
        self.assertEqual(revision.scenario_snapshot["evidence_checksum"], batch.checksum)

    def test_invalid_checksum_and_truncated_real_source_leave_no_partial_batch(self):
        self.client.force_login(self.admin)
        self.assertEqual(self._upload(
            "evidence", self.evidence_bytes, self.evidence_name,
            checksum="0" * 64,
        ).status_code, 400)
        truncated = self.evidence_bytes[:100]
        self.assertEqual(self._upload(
            "evidence", truncated, self.evidence_name,
            checksum=hashlib.sha256(self.evidence_bytes).hexdigest(),
        ).status_code, 400)
        self.assertEqual(CatalogEvidenceBatch.objects.count(), 0)
        self.assertEqual(CatalogImportEvent.objects.count(), 0)

    def test_django_admin_cannot_edit_or_delete_history(self):
        self.client.force_login(self.admin)
        self.assertEqual(self._upload(
            "evidence", self.evidence_bytes, self.evidence_name,
        ).status_code, 302)
        claim = CatalogEvidenceClaim.objects.first()
        original = claim.value
        self.client.force_login(self.superuser)
        change_url = reverse("admin:catalog_catalogevidenceclaim_change", args=[claim.pk])
        delete_url = reverse("admin:catalog_catalogevidenceclaim_delete", args=[claim.pk])
        self.assertEqual(self.client.post(change_url, {"value": "changed"}).status_code, 403)
        self.assertEqual(self.client.post(delete_url, {"post": "yes"}).status_code, 403)
        claim.refresh_from_db()
        self.assertEqual(claim.value, original)

    def test_import_requires_separate_publication_and_rollback_preserves_projects(self):
        self.client.force_login(self.admin)
        self.assertEqual(self._upload(
            "evidence", self.evidence_bytes, self.evidence_name,
        ).status_code, 302)
        old_batch = CatalogEvidenceBatch.objects.get()
        self.assertIsNone(CatalogPublication.objects.first())
        self.assertEqual(self.client.get(reverse("catalog_index")).status_code, 503)
        unpublished_family = old_batch.claims.filter(
            catalog_rows__isnull=False,
        ).first().catalog_rows.first().family
        self.assertEqual(self.client.get(reverse("catalog_family_detail", args=[unpublished_family.pk])).status_code, 503)
        self.assertEqual(self.client.post(reverse("catalog_source_management"), {
            "action": "publish", "checksum": old_batch.checksum,
            "reason": "Первая утверждённая публикация сведений",
        }).status_code, 302)
        old_project = Project.objects.create(
            owner=self.user, name="Исходный выбор", object_slug="warehouse",
        )
        old_revision = ProjectRevision.objects.create(
            project=old_project, number=1,
            scenario_snapshot={"catalog_checksum": self.catalog_batch.checksum,
                               "evidence_checksum": old_batch.checksum},
        )

        # Reformat only the actual source assertions; the test invents no product facts.
        next_bytes = json.dumps(json.loads(self.evidence_bytes), ensure_ascii=False,
                                indent=2).encode("utf-8")
        next_checksum = hashlib.sha256(next_bytes).hexdigest()
        self.assertNotEqual(next_checksum, old_batch.checksum)
        with override_settings(RESEARCH_CLAIMS_SHA256=next_checksum):
            self.assertEqual(self._upload(
                "evidence", next_bytes, self.evidence_name,
            ).status_code, 302)
        self.assertEqual(CatalogEvidenceBatch.objects.count(), 2)
        self.assertEqual(current_source_pair()[1].checksum, old_batch.checksum)
        self.assertEqual(self.client.get(reverse("catalog_index"), {
            "batch": self.catalog_batch.checksum, "evidence": next_checksum,
        }).status_code, 404)
        family = old_batch.claims.filter(catalog_rows__isnull=False).first().catalog_rows.first().family
        self.assertEqual(self.client.get(reverse("catalog_family_detail", args=[family.pk]), {
            "evidence": next_checksum,
        }).status_code, 404)

        publish_url = reverse("catalog_source_management")
        published = self.client.post(publish_url, {
            "action": "publish", "checksum": next_checksum,
            "reason": "Утверждённая новая редакция сведений",
        })
        self.assertEqual(published.status_code, 302)
        self.assertEqual(current_source_pair()[1].checksum, next_checksum)
        self.assertEqual(self.client.get(reverse("catalog_index"), {
            "batch": self.catalog_batch.checksum, "evidence": old_batch.checksum,
        }).status_code, 200)
        self.client.force_login(self.user)
        self.assertEqual(self.client.post(publish_url, {
            "action": "publish", "checksum": old_batch.checksum,
            "reason": "Возврат к предыдущей проверенной версии",
        }).status_code, 403)
        self.assertEqual(self.client.post(reverse("project_create", args=["warehouse"]), {
            "name": "Следующий выбор",
        }).status_code, 302)
        new_revision = Project.objects.get(owner=self.user, name="Следующий выбор").revisions.get()
        self.assertEqual(new_revision.scenario_snapshot["evidence_checksum"], next_checksum)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.post(publish_url, {
            "action": "publish", "checksum": old_batch.checksum,
            "reason": "Возврат к предыдущей проверенной версии",
        }).status_code, 302)
        self.assertEqual(current_source_pair()[1].checksum, old_batch.checksum)
        old_revision.refresh_from_db()
        new_revision.refresh_from_db()
        self.assertEqual(old_revision.scenario_snapshot["evidence_checksum"], old_batch.checksum)
        self.assertEqual(new_revision.scenario_snapshot["evidence_checksum"], next_checksum)
        self.assertEqual(CatalogPublicationEvent.objects.count(), 3)

    def test_publication_rejects_catalog_not_approved_for_environment(self):
        self.client.force_login(self.admin)
        self.assertEqual(self._upload(
            "evidence", self.evidence_bytes, self.evidence_name,
        ).status_code, 302)
        evidence = CatalogEvidenceBatch.objects.get()
        with override_settings(CATALOG_SOURCE_SHA256="0" * 64):
            response = self.client.post(reverse("catalog_source_management"), {
                "action": "publish", "checksum": evidence.checksum,
                "reason": "Проверка утверждения конкурсного каталога",
            })
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, "не утверждённым", status_code=409)
        self.assertEqual(CatalogPublicationEvent.objects.count(), 0)
