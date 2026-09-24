import re
from copy import deepcopy
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen

from django.core.management import call_command
from django.db.utils import OperationalError
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import TestCase, override_settings
from django.urls import reverse

from demo.models import DemoScenario
from demo.catalog import load_demo_catalog
from demo.matching import evaluate_match


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
                self.assertIn(f"/objects/{slug}/market/", detail)
                status, data = self.request(f"/api/v1/objects/{slug}/")
                self.assertEqual(status, 200)
                self.assertIn(f'"slug": "{slug}"', data)
                status, market = self.request(f"/objects/{slug}/market/")
                self.assertEqual(status, 200)
                self.assertIn("Демонстрационные модели", market)
                status, matches = self.request(f"/api/v1/objects/{slug}/matches/")
                self.assertEqual(status, 200)
                self.assertIn('"requires_verification"', matches)

    def test_health_and_ready_over_http(self):
        self.assertEqual(self.request("/health")[0], 200)
        self.assertEqual(self.request("/ready")[0], 200)
        with self.assertRaises(HTTPError) as error:
            self.request("/objects/missing/")
        self.assertEqual(error.exception.code, 404)


class MarketJourneyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)

    def test_three_objects_explain_candidates_and_keep_price_unknown(self):
        expected = {
            "warehouse": ("demo-pallet", "requires_verification"),
            "airport": ("demo-baggage", "requires_verification"),
            "hospital": ("demo-care", "requires_verification"),
        }
        for slug, (robot_slug, status) in expected.items():
            with self.subTest(slug=slug):
                page = self.client.get(f"/objects/{slug}/market/")
                self.assertEqual(page.status_code, 200)
                self.assertContains(page, "Демонстрационные модели")
                self.assertContains(page, robot_slug)
                api = self.client.get(f"/api/v1/objects/{slug}/matches/")
                self.assertEqual(api.status_code, 200)
                matches = {item["robot"]["slug"]: item for item in api.json()["matches"]}
                self.assertEqual(matches[robot_slug]["status"], status)
                self.assertTrue(matches[robot_slug]["reasons"])
                self.assertIsNone(matches[robot_slug]["robot"]["price"]["value"])
                self.assertEqual(matches[robot_slug]["robot"]["price"]["status"], "missing")

    def test_selection_and_rejection_follow_the_same_match(self):
        selected = self.client.get("/objects/warehouse/market/?robot=demo-pallet")
        self.assertEqual(selected.status_code, 200)
        self.assertContains(selected, "Выбран для сценария")
        self.assertEqual(
            self.client.get("/objects/warehouse/market/?robot=demo-compact").status_code,
            400,
        )
        self.assertEqual(
            self.client.get("/objects/warehouse/market/?robot=unknown").status_code,
            404,
        )
        api = self.client.get("/api/v1/objects/warehouse/matches/")
        self.assertEqual(api.status_code, 200)
        matches = {item["robot"]["slug"]: item for item in api.json()["matches"]}
        self.assertEqual(matches["demo-compact"]["status"], "reject")
        self.assertIn("грузоподъём", " ".join(matches["demo-compact"]["reasons"]).lower())

    def test_critical_unknowns_do_not_become_fit(self):
        for slug, robot_slug, missing_text in (
            ("warehouse", "demo-pallet", "нагрузк"),
            ("airport", "demo-baggage", "доступ"),
            ("hospital", "demo-care", "лифт"),
        ):
            with self.subTest(slug=slug):
                api = self.client.get(f"/api/v1/objects/{slug}/matches/")
                self.assertEqual(api.status_code, 200)
                matches = api.json()["matches"]
                chosen = next(item for item in matches if item["robot"]["slug"] == robot_slug)
                self.assertEqual(chosen["status"], "requires_verification")
                self.assertIn(missing_text, " ".join(chosen["reasons"]).lower())

    def test_object_specific_rejections_and_unverified_demo_evidence(self):
        robots = {item["slug"]: item for item in load_demo_catalog()}
        warehouse = DemoScenario.objects.get(slug="warehouse")
        wide_robot = deepcopy(robots["demo-pallet"])
        wide_robot["width"]["value"] = 4
        self.assertEqual(evaluate_match(warehouse, wide_robot)["status"], "reject")

        airport = DemoScenario.objects.get(slug="airport")
        airport.parameters["access_permission"]["value"] = False
        self.assertEqual(evaluate_match(airport, robots["demo-baggage"])["status"], "reject")

        hospital = DemoScenario.objects.get(slug="hospital")
        incompatible = deepcopy(robots["demo-care"])
        incompatible["clean_transport"]["value"] = False
        self.assertEqual(evaluate_match(hospital, incompatible)["status"], "reject")
        hospital.parameters["lift_wait"]["value"] = 4
        incompatible["clean_transport"]["value"] = True
        incompatible["lift_compatible"]["value"] = False
        self.assertEqual(evaluate_match(hospital, incompatible)["status"], "reject")
        incompatible["lift_compatible"]["value"] = True
        result = evaluate_match(hospital, incompatible)
        self.assertEqual(result["status"], "requires_verification")
        self.assertIn("допущен", " ".join(result["reasons"]).lower())

    def test_floor_area_rating_cannot_be_compared_with_robot_total_mass(self):
        warehouse = DemoScenario.objects.get(slug="warehouse")
        warehouse.parameters["floor_capacity"] = {
            "value": 5000,
            "unit": "кг/м²",
            "status": "assumption",
            "source": "Синтетический пример",
        }
        robot = next(item for item in load_demo_catalog() if item["slug"] == "demo-pallet")
        result = evaluate_match(warehouse, robot)
        self.assertEqual(result["status"], "requires_verification")
        self.assertIn("нагрузк", " ".join(result["reasons"]).lower())
