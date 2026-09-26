import re
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen

from django.core.management import call_command
from django.db.utils import OperationalError
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import TestCase, override_settings
from django.urls import reverse

from demo.models import DemoScenario


class DemonstrationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)

    def test_three_distinct_objects_are_selectable_in_russian(self):
        response = self.client.get(reverse("index"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(DemoScenario.objects.count(), 3)
        for slug, title, label in [("warehouse", "Склад", "Масса паллеты"), ("airport", "Аэропорт", "Разрешение на доступ"), ("hospital", "Медицинское учреждение", "Ожидание лифта")]:
            with self.subTest(slug=slug):
                self.assertContains(response, f'/objects/{slug}/')
                detail = self.client.get(reverse("detail", args=[slug]))
                self.assertContains(detail, title)
                self.assertContains(detail, "Синтетический пример")
                self.assertContains(detail, "Нет данных")
                self.assertContains(detail, label)

    def test_api_has_separate_topology_and_preserves_unknown(self):
        response = self.client.get(reverse("objects_api"))
        self.assertEqual(response.status_code, 200)
        items = {item["slug"]: item for item in response.json()["objects"]}
        self.assertEqual(set(items), {"warehouse", "airport", "hospital"})
        self.assertEqual(items["warehouse"]["unit"], "паллетный рейс")
        self.assertEqual(items["airport"]["topology"]["nodes"][1]["zone"], "controlled")
        self.assertEqual(items["hospital"]["topology"]["nodes"][0]["flow"], "clean")
        self.assertEqual(items["hospital"]["topology"]["nodes"][1]["floor"], "1–3")
        self.assertIsNone(items["warehouse"]["parameters"]["robot_payload"]["value"])
        self.assertEqual(items["warehouse"]["parameters"]["robot_payload"]["status"], "missing")
        for item in items.values():
            self.assertEqual(item["data_status"], "assumption")
            for field in item["parameters"].values():
                self.assertEqual(set(field), {"unit", "value", "status", "source"})
                self.assertIn(field["status"], {"assumption", "missing"})

    def test_missing_object_is_404_and_api_is_read_only(self):
        self.assertEqual(self.client.get("/objects/unknown/").status_code, 404)
        self.assertEqual(self.client.get("/api/v1/objects/unknown/").status_code, 404)
        self.assertEqual(self.client.post(reverse("objects_api")).status_code, 405)
        self.assertEqual(self.client.post(reverse("detail", args=["warehouse"])).status_code, 405)

    def test_live_and_ready_are_distinct(self):
        self.assertEqual(self.client.get("/health").json(), {"status": "alive"})
        self.assertEqual(self.client.get("/ready").json(), {"status": "ready"})
        with patch("demo.views.connection.cursor", side_effect=OperationalError("offline")):
            self.assertEqual(self.client.get("/health").status_code, 200)
            response = self.client.get("/ready")
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json(), {"status": "not_ready"})

    def test_seed_is_idempotent_and_does_not_overwrite_existing_demo(self):
        scenario = DemoScenario.objects.get(slug="warehouse")
        scenario.process = "Редактируемое описание"
        scenario.save()
        call_command("seed_demo", verbosity=0)
        self.assertEqual(DemoScenario.objects.count(), 3)
        scenario.refresh_from_db()
        self.assertEqual(scenario.process, "Редактируемое описание")

    @override_settings(SECURE_SSL_REDIRECT=True, SECURE_HSTS_SECONDS=3600)
    def test_preview_requires_https_and_sends_hsts(self):
        redirect = self.client.get("/health")
        self.assertEqual(redirect.status_code, 301)
        self.assertEqual(redirect["Location"], "https://testserver/health")
        https_response = self.client.get("/health", secure=True)
        self.assertEqual(https_response.status_code, 200)
        self.assertEqual(https_response["Strict-Transport-Security"], "max-age=3600")


@override_settings(STORAGES={
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class HttpJourneyTests(StaticLiveServerTestCase):
    """Exercise the actual HTTP server used by the browser, not only Django's test client."""

    def setUp(self):
        call_command("seed_demo", verbosity=0)

    def request(self, path):
        with urlopen(f"{self.live_server_url}{path}", timeout=5) as response:
            return response.status, response.read().decode("utf-8")

    def test_select_each_object_and_open_its_api_over_http(self):
        status, landing = self.request("/")
        self.assertEqual(status, 200)
        css_url = re.search(r'href="(/static/demo/style[^" ]*\.css)"', landing)
        self.assertIsNotNone(css_url, "The homepage must load its own stylesheet")
        self.assertEqual(self.request(css_url.group(1))[0], 200)
        for slug, name in (("warehouse", "Склад"), ("airport", "Аэропорт"), ("hospital", "Медицинское учреждение")):
            with self.subTest(slug=slug):
                self.assertIn(f'/objects/{slug}/', landing)
                status, detail = self.request(f"/objects/{slug}/")
                self.assertEqual(status, 200)
                self.assertIn(name, detail)
                status, data = self.request(f"/api/v1/objects/{slug}/")
                self.assertEqual(status, 200)
                self.assertIn(f'"slug": "{slug}"', data)

    def test_health_and_ready_over_http(self):
        self.assertEqual(self.request("/health")[0], 200)
        self.assertEqual(self.request("/ready")[0], 200)
        with self.assertRaises(HTTPError) as error:
            self.request("/objects/missing/")
        self.assertEqual(error.exception.code, 404)
