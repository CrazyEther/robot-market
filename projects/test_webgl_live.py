"""Optional real-page WebGL integration using Django's isolated test database.

Run explicitly on a developer machine with Playwright + Edge installed:
    $env:ROBOT_MARKET_BROWSER_E2E='1'
    .\.venv\Scripts\python.exe manage.py test projects.test_webgl_live

No permanent users, fake catalogue publications or production data are made.
"""

import hashlib
import os
import unittest

from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse

from projects.models import SimulationRun


@unittest.skipUnless(os.environ.get("ROBOT_MARKET_BROWSER_E2E") == "1",
                     "Запускается вручную с браузером и независимой тестовой БД")
class SavedSimulationWebGLTests(StaticLiveServerTestCase):
    def test_real_saved_v3_page_switches_webgl_without_changing_the_ledger(self):
        from playwright.sync_api import sync_playwright
        from projects.test_resource_integration import ResourceRunJourneyTests

        owner, project, _, revision = ResourceRunJourneyTests().make_resource_project()
        self.client.force_login(owner)
        url = reverse("project_simulation", args=[project.id])
        result = self.client.post(url, {"base_revision": str(revision.number)})
        self.assertEqual(result.status_code, 302, result.content.decode())
        run = SimulationRun.objects.get(project=project, revision=revision)
        self.assertEqual(run.ledger_version, 3)
        session_id = self.client.cookies[settings.SESSION_COOKIE_NAME].value

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                channel=os.environ.get("ROBOT_MARKET_BROWSER_CHANNEL", "msedge"),
                headless=True,
                args=["--enable-unsafe-swiftshader", "--use-angle=swiftshader"],
            )
            try:
                context = browser.new_context(viewport={"width": 1440, "height": 900})
                context.add_cookies([{"name": settings.SESSION_COOKIE_NAME,
                                      "value": session_id, "url": self.live_server_url}])
                page = context.new_page()
                page_errors = []
                page.on("pageerror", lambda error: page_errors.append(str(error)))
                response = page.goto(
                    f"{self.live_server_url}{url}?revision={revision.number}&run={run.id}",
                    wait_until="domcontentloaded",
                )
                self.assertEqual(response.status, 200)
                self.assertEqual(page.locator('[data-playback]').count(), 1)
                payload = page.evaluate("JSON.parse(document.querySelector('#playback-data').textContent)")
                self.assertEqual(payload["resource_reservations"],
                                 run.ledger["resource_reservations"])
                self.assertEqual(payload["motion"]["cycles"][0]["source_row"], 2)

                page.locator('button[data-view="3d"]').click()
                page.wait_for_function("window.playback3d && window.playback3d.isWebGL",
                                       timeout=20000)
                webgl = page.locator("canvas[data-webgl-scene]")
                self.assertTrue(webgl.is_visible())
                empty = hashlib.sha256(webgl.screenshot()).digest()
                first_start = next(index + 1 for index, event in enumerate(payload["events"])
                                   if event["type"] == "start")
                page.locator("input[data-position]").evaluate(
                    "(element, index) => {element.value = index; element.dispatchEvent(new Event('input',{bubbles:true}));}",
                    first_start,
                )
                self.assertEqual(int(page.locator("input[data-position]").input_value()),
                                 first_start)
                self.assertEqual(page.locator("[data-active]").inner_text(), "1")
                self.assertNotEqual(empty, hashlib.sha256(webgl.screenshot()).digest())
                self.assertIn("Заняты участки:",
                              page.locator("[data-resource-status]").inner_text())
                active = page.locator("[data-active]").inner_text()
                page.locator('button[data-view="2d"]').click()
                self.assertTrue(page.locator('[data-view-panel="3d"]').is_hidden())
                self.assertEqual(page.locator("[data-active]").inner_text(), active)
                page.locator('button[data-view="3d"]').click()
                self.assertTrue(page.evaluate("window.playback3d.isWebGL"))
                self.assertEqual(page.locator("[data-active]").inner_text(), active)
                page.set_viewport_size({"width": 390, "height": 844})
                self.assertTrue(webgl.is_visible())
                bounds = webgl.bounding_box()
                self.assertGreater(bounds["width"], 100)
                self.assertLessEqual(bounds["x"] + bounds["width"], 391)
                self.assertGreater(page.evaluate(
                    "document.querySelector('[data-webgl-scene]').width"), 100)
                self.assertFalse(page_errors, page_errors)
            finally:
                browser.close()
