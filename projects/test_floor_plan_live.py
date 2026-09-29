"""Optional browser proof: a verified saved run displays its private floor raster in 2D/3D."""

from copy import deepcopy
import hashlib
import math
import os
import unittest

from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse

from projects.floor_plans import calibration_for, sanitize_floor_plan
from projects.models import FloorPlanSource, ProjectRevision, SimulationRun
from projects.models import Project
from projects.test_floor_plans import image_upload, topology_fixture


@unittest.skipUnless(os.environ.get("ROBOT_MARKET_BROWSER_E2E") == "1",
                     "Browser E2E выполняется отдельно")
class CalibratedRunBrowserTests(StaticLiveServerTestCase):
    def test_clicking_a_plan_fills_anchor_pixels_without_saving_until_confirmed(self):
        from django.contrib.auth import get_user_model
        from playwright.sync_api import sync_playwright

        owner = get_user_model().objects.create_user(username="calibration-ui")
        project = Project.objects.create(owner=owner, name="Тест привязки", object_slug="warehouse")
        ProjectRevision.objects.create(project=project, number=1, scenario_snapshot={
            "task_profile": {"process": "warehouse_pallet_transfer"},
            "topology_profile": topology_fixture("warehouse", "warehouse_pallet_transfer"),
        })
        self.client.force_login(owner)
        url = reverse("project_topology", args=[project.id])
        upload = self.client.post(url, {
            "action": "upload_plan", "base_revision": "1", "floor": "1",
            "file": image_upload(), "source_description": "План обмера",
            "source_attested": "on",
        })
        self.assertEqual(upload.status_code, 302)
        source = project.floor_plan_sources.get()
        session_id = self.client.cookies[settings.SESSION_COOKIE_NAME].value

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel=os.environ.get(
                "ROBOT_MARKET_BROWSER_CHANNEL", "msedge"), headless=True)
            try:
                context = browser.new_context(viewport={"width": 1280, "height": 900})
                context.add_cookies([{"name": settings.SESSION_COOKIE_NAME,
                                      "value": session_id, "url": self.live_server_url}])
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                self.assertEqual(page.goto(self.live_server_url + url).status, 200)
                form = page.locator("[data-floor-plan-calibration]")
                form.locator("select[name='plan_id']").select_option(str(source.id))
                form.locator("select[name='anchor_a']").select_option("a")
                form.locator("select[name='anchor_b']").select_option("b")
                preview = form.locator("[data-plan-preview]")
                preview.wait_for(state="visible")
                page.wait_for_function("document.querySelector('[data-plan-preview]').naturalWidth === 300")
                box = preview.bounding_box()
                preview.click(position={"x": box["width"] * 0.2, "y": box["height"] * 0.4})
                self.assertAlmostEqual(float(form.locator("input[name='pixel_a_x']").input_value()),
                                       60, delta=0.5)
                self.assertAlmostEqual(float(form.locator("input[name='pixel_a_y']").input_value()),
                                       80, delta=0.5)
                form.locator("[data-pick-anchor='b']").click()
                preview.click(position={"x": box["width"] * 0.8, "y": box["height"] * 0.4})
                self.assertAlmostEqual(float(form.locator("input[name='pixel_b_x']").input_value()),
                                       240, delta=0.5)
                self.assertEqual(page.locator("input[name='base_revision']").first.input_value(), "1")
                form.locator("input[name='evidence']").fill("Две промеренные метки, правки оператора")
                with page.expect_navigation():
                    form.get_by_role("button", name="Привязать план к этажу").click()
                self.assertIn("revision=2", page.url)
                self.assertFalse(errors, errors)
            finally:
                browser.close()
        self.assertEqual(project.revisions.count(), 2)

    def test_attested_plan_is_read_by_2d_and_webgl_at_saved_kpi(self):
        from playwright.sync_api import sync_playwright
        from projects.test_resource_integration import ResourceRunJourneyTests

        owner, project, _, previous = ResourceRunJourneyTests().make_resource_project()
        snapshot = deepcopy(previous.scenario_snapshot)
        topo = snapshot["topology_profile"]
        # Length comes from a separate measurement; geometry drives visuals only.
        route_edge = topo["edges"][0]
        route_edge["length_m"] = "14"
        route_edge["waypoints_m"] = [{"x_m": "5", "y_m": "4"}]
        route_edge["geometry_source"] = "Натурное положение поворота"
        measured = [item for item in topo["nodes"] if item.get("coordinate_source")
                    and item.get("x_m") is not None]
        self.assertGreaterEqual(len(measured), 2)
        self.assertEqual(measured[0]["floor"], measured[1]["floor"])
        normalized = sanitize_floor_plan(image_upload(width=420, height=250))
        source = FloorPlanSource.objects.create(
            project=project, uploaded_by=owner, floor=measured[0]["floor"],
            filename="warehouse.png", source_description="Тестовый обмерный план",
            **normalized,
        )
        topo.setdefault("floor_plans", {})[source.floor] = calibration_for(
            topo, source, measured[0]["id"], measured[1]["id"],
            (30, 100, 390, 100), "Две измеренные метки",
        )
        revision = ProjectRevision.objects.create(
            project=project, number=previous.number + 1, scenario_snapshot=snapshot,
        )
        self.client.force_login(owner)
        sim_url = reverse("project_simulation", args=[project.id])
        result = self.client.post(sim_url, {"base_revision": str(revision.number)})
        self.assertEqual(result.status_code, 302, result.content.decode())
        run = SimulationRun.objects.get(project=project, revision=revision)
        self.assertEqual(run.ledger_version, 3)
        private_url = reverse("project_floor_plan", args=[project.id, source.id])
        session_id = self.client.cookies[settings.SESSION_COOKIE_NAME].value

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                channel=os.environ.get("ROBOT_MARKET_BROWSER_CHANNEL", "msedge"),
                headless=True, args=["--enable-unsafe-swiftshader", "--use-angle=swiftshader"],
            )
            try:
                context = browser.new_context(viewport={"width": 1400, "height": 900})
                context.add_cookies([{"name": settings.SESSION_COOKIE_NAME,
                                      "value": session_id, "url": self.live_server_url}])
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                response = page.goto(f"{self.live_server_url}{sim_url}?run={run.id}")
                self.assertEqual(response.status, 200)
                backdrop = page.locator(".playback-floor svg image").first
                self.assertEqual(backdrop.get_attribute("href"), private_url)
                self.assertTrue(backdrop.is_visible())
                path = page.locator(".playback-floor polyline.playback-route").first
                self.assertEqual(len(path.get_attribute("points").split()), 3)
                self.assertNotEqual(path.evaluate("node => getComputedStyle(node).stroke"), "none")
                image_response = page.request.get(f"{self.live_server_url}{private_url}")
                self.assertEqual(image_response.status, 200)
                self.assertEqual(hashlib.sha256(image_response.body()).hexdigest(),
                                 source.png_sha256)
                kpi = page.locator("[data-delivered]").inner_text()
                page.locator('button[data-view="3d"]').click()
                page.wait_for_function("window.playback3d && window.playback3d.isWebGL",
                                       timeout=20000)
                self.assertTrue(page.locator("canvas[data-webgl-scene]").is_visible())
                self.assertEqual(page.locator("[data-delivered]").inner_text(), kpi)
                self.assertEqual(page.evaluate("JSON.parse(document.querySelector('#playback-scene').textContent).floors[0].plan.url"),
                                 private_url)
                self.assertEqual(page.evaluate(
                    "JSON.parse(document.querySelector('#playback-scene').textContent).floors[0].edges[0].path.length"), 3)
                motion = page.evaluate("""() => {
                    const payload = JSON.parse(document.querySelector('#playback-data').textContent);
                    const cycle = payload.motion.cycles.find(item => item.stages.some(
                        stage => stage.kind === 'travel' && stage.path?.length === 3));
                    if (!cycle) return null;
                    return {key: `${cycle.robot_id}:${cycle.source_row}`,
                        title: `${cycle.robot_id} · задание ${cycle.source_row}`,
                        initial: Number(payload.initial_at_s),
                        routes: cycle.stages.filter(stage => stage.kind === 'travel'
                            && stage.path?.length === 3).map(stage => ({
                                start: Number(cycle.start_s) + Number(stage.start_s),
                                duration: Number(stage.end_s) - Number(stage.start_s),
                                points: stage.path,
                            }))};
                }""")
                self.assertIsNotNone(motion)
                self.assertEqual(len(motion["routes"]), 2)
                self.assertEqual(motion["routes"][0]["points"],
                                 list(reversed(motion["routes"][1]["points"])))
                # Advance the actual playback loop deterministically between ledger events;
                # stepping only across events would never prove interpolation at a bend.
                page.evaluate("""() => {
                    let id = 0;
                    const frames = new Map();
                    window.requestAnimationFrame = callback => {
                        frames.set(++id, callback);
                        return id;
                    };
                    window.cancelAnimationFrame = frame => frames.delete(frame);
                    window.__tickPlayback = timestamp => {
                        const [nextId, callback] = frames.entries().next().value;
                        frames.delete(nextId);
                        callback(timestamp);
                    };
                }""")
                page.locator("[data-toggle-play]").click()
                page.evaluate("window.__tickPlayback(0)")
                for route in motion["routes"]:
                    for fraction in (0.25, 0.5, 0.75):
                        clock = route["start"] + route["duration"] * fraction
                        page.evaluate("""({clock, initial}) => window.__tickPlayback(
                            (clock - initial) * 1000 / Number(document.querySelector('[data-speed]').value))""",
                                      {"clock": clock, "initial": motion["initial"]})
                        a, bend, b = route["points"]
                        if fraction == 0.25:
                            expected_x = (float(a["x"]) + float(bend["x"])) / 2
                            expected_y = (float(a["y"]) + float(bend["y"])) / 2
                        elif fraction == 0.5:
                            expected_x, expected_y = float(bend["x"]), float(bend["y"])
                        else:
                            expected_x = (float(bend["x"]) + float(b["x"])) / 2
                            expected_y = (float(bend["y"]) + float(b["y"])) / 2
                        actual = page.evaluate("""({key, title}) => {
                            const circle = [...document.querySelectorAll('.playback-robot')].find(
                                node => node.parentNode.querySelector('title')?.textContent === title);
                            return {svg: circle ? [Number(circle.getAttribute('cx')),
                                Number(circle.getAttribute('cy'))] : null,
                                webgl: window.playback3d.robotAt(key),
                                yaw: window.playback3d.robotYawAt(key)};
                        }""", {"key": motion["key"], "title": motion["title"]})
                        self.assertIsNotNone(actual["svg"], actual)
                        self.assertIsNotNone(actual["webgl"], actual)
                        self.assertAlmostEqual(actual["svg"][0], expected_x, delta=0.06)
                        self.assertAlmostEqual(actual["svg"][1], expected_y, delta=0.06)
                        self.assertAlmostEqual(actual["webgl"][0], (expected_x - 300) / 95, delta=0.001)
                        self.assertAlmostEqual(actual["webgl"][2], (expected_y - 200) / 95, delta=0.001)
                        if fraction != 0.5:
                            segment_start, segment_end = (a, bend) if fraction < 0.5 else (bend, b)
                            expected_yaw = math.atan2(float(segment_end["x"]) - float(segment_start["x"]),
                                                      float(segment_end["y"]) - float(segment_start["y"]))
                            self.assertAlmostEqual(actual["yaw"], expected_yaw, delta=0.001)
                self.assertFalse(errors, errors)
            finally:
                browser.close()
