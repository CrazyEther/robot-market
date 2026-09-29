"""Owner-private calibrated plans, immutable historical scene and no invented distances."""

import hashlib
import json
from copy import deepcopy
from io import BytesIO
from zipfile import ZipFile

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from PIL import Image

from projects.floor_plans import FloorPlanError, calibration_for, project_on_plan, sanitize_floor_plan
from projects.models import FloorPlanSource, FinancePlan, Project, ProjectRevision, SimulationRun
from projects.playback import measured_scene
from projects.topology import empty_topology, floor_drawings, route_result


def image_upload(*, width=300, height=200, fmt="PNG"):
    buffer = BytesIO()
    Image.new("RGB", (width, height), "#d9e1e7").save(buffer, format=fmt)
    return SimpleUploadedFile(f"plan.{fmt.lower()}", buffer.getvalue(),
                              content_type="image/png" if fmt == "PNG" else "image/jpeg")


def topology_fixture(slug, process):
    topology = empty_topology(slug, process)
    zone = "public" if slug == "airport" else "clean"
    topology["nodes"] = [
        {"id": "a", "label": "Приём", "floor": "1", "zone": zone,
         "source": "Обмер", "x_m": "0", "y_m": "0", "coordinate_source": "Обмер XYZ"},
        {"id": "b", "label": "Выдача", "floor": "1", "zone": zone,
         "source": "Обмер", "x_m": "10", "y_m": "0", "coordinate_source": "Обмер XYZ"},
        {"id": "mid", "label": "Разворот", "floor": "1", "zone": zone,
         "source": "Обмер", "x_m": "5", "y_m": "-2", "coordinate_source": "Обмер XYZ"},
    ]
    topology["edges"] = [{
        "id": "ab", "start": "a", "end": "b", "bidirectional": True,
        "source": "Проверенный проход", "length_m": "14",
        "length_source": "Измерение вдоль фактического прохода",
        "access": "public", "kind": "corridor", "flow": "clean",
    }]
    topology.update(origin="a", destination="b", route_flow="clean")
    return topology


class PlanProjectionTests(SimpleTestCase):
    def test_pixel_projection_is_not_assumed_route_distance(self):
        for slug, process in (("warehouse", "warehouse_pallet_transfer"),
                              ("airport", "airport_terminal_cleaning"),
                              ("hospital", "hospital_floor_cleaning")):
            with self.subTest(slug=slug):
                topology = topology_fixture(slug, process)
                class Source:
                    id = "00000000-0000-0000-0000-000000000001"
                    floor = "1"
                    width_px = 300
                    height_px = 200
                    png_sha256 = "a" * 64

                ref = calibration_for(topology, Source, "a", "b",
                                      (20, 80, 220, 80), "Привязка по контрольным меткам")
                topology["floor_plans"] = {"1": ref}
                drawing = floor_drawings(topology, route_result(topology))[0]
                lookup = {node["id"]: node for node in drawing["nodes"]}
                self.assertEqual((lookup["a"]["x"], lookup["a"]["y"]), ("53.00", "162.00"))
                self.assertEqual((lookup["b"]["x"], lookup["b"]["y"]), ("433.00", "162.00"))
                self.assertEqual((lookup["mid"]["x"], lookup["mid"]["y"]), ("243.00", "238.00"))
                self.assertEqual(drawing["plan"]["width"], "570.00")
                self.assertEqual(route_result(topology)["distance_m"], "14")

    def test_bad_anchor_calibration_is_never_silent(self):
        topology = topology_fixture("warehouse", "warehouse_pallet_transfer")
        class Source:
            id = "00000000-0000-0000-0000-000000000001"
            floor = "1"
            width_px = 300
            height_px = 200
            png_sha256 = "a" * 64

        for a, b, pixels in (("a", "a", (20, 80, 220, 80)),
                             ("a", "b", (20, 80, 20, 80)),
                             ("a", "b", (20, 80, 390, 80))):
            with self.subTest(a=a, b=b, pixels=pixels):
                with self.assertRaises(FloorPlanError):
                    calibration_for(topology, Source, a, b, pixels, "Контроль")
        topology["nodes"][1]["floor"] = "2"
        with self.assertRaises(FloorPlanError):
            calibration_for(topology, Source, "a", "b", (20, 80, 220, 80), "Контроль")
        self.assertIsNone(project_on_plan(topology, "1", {
            "anchor_a": "a", "anchor_b": "b", "pixel_a": ["20", "80"],
            "pixel_b": ["220", "80"], "width_px": 300, "height_px": 200,
        }, topology["nodes"]))

    def test_reject_untrusted_images(self):
        for upload in (SimpleUploadedFile("x.png", b"fake", content_type="image/png"),
                       SimpleUploadedFile("huge.png", b"x" * (8 * 1024 * 1024 + 1)),
                       image_upload(width=4100, height=2)):
            with self.subTest(name=upload.name):
                with self.assertRaises(FloorPlanError):
                    sanitize_floor_plan(upload)
        jpeg = sanitize_floor_plan(image_upload(fmt="JPEG"))
        self.assertEqual(jpeg["image_png"][:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(hashlib.sha256(jpeg["image_png"]).hexdigest(), jpeg["png_sha256"])
        translucent = BytesIO()
        Image.new("RGBA", (30, 20), (0, 0, 0, 0)).save(translucent, format="PNG")
        normalized = sanitize_floor_plan(SimpleUploadedFile("transparent.png", translucent.getvalue()))
        with Image.open(BytesIO(normalized["image_png"])) as image:
            self.assertEqual(image.getpixel((15, 10)), (255, 255, 255))


class PlanJourneyTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user(username="plan-owner")
        self.stranger = get_user_model().objects.create_user(username="plan-stranger")
        self.client.force_login(self.owner)

    def make_project(self, slug, process):
        project = Project.objects.create(owner=self.owner, name=slug, object_slug=slug)
        ProjectRevision.objects.create(project=project, number=1, scenario_snapshot={
            "task_profile": {"process": process},
            "topology_profile": topology_fixture(slug, process),
        })
        return project

    def upload(self, url):
        response = self.client.post(url, {
            "action": "upload_plan", "base_revision": "1", "floor": "1",
            "file": image_upload(), "source_description": "Обмерный план, ревизия 3",
            "source_attested": "on",
        })
        self.assertEqual(response.status_code, 302, response.content.decode())
        return FloorPlanSource.objects.latest("uploaded_at")

    def calibration_payload(self, source, **overrides):
        data = {
            "action": "calibrate_plan", "base_revision": "1", "plan_id": str(source.id),
            "anchor_a": "a", "anchor_b": "b", "pixel_a_x": "20", "pixel_a_y": "80",
            "pixel_b_x": "220", "pixel_b_y": "80", "evidence": "Две съёмочные марки, обмер",
        }
        data.update(overrides)
        return data

    def test_three_object_workflow_and_private_image_access(self):
        for slug, process in (("warehouse", "warehouse_pallet_transfer"),
                              ("airport", "airport_terminal_cleaning"),
                              ("hospital", "hospital_floor_cleaning")):
            with self.subTest(slug=slug):
                project = self.make_project(slug, process)
                topology_url = reverse("project_topology", args=[project.id])
                source = self.upload(topology_url)
                self.assertEqual(project.revisions.count(), 1)
                image_url = reverse("project_floor_plan", args=[project.id, source.id])
                owner_image = self.client.get(image_url)
                self.assertEqual(owner_image.status_code, 200)
                self.assertEqual(owner_image["Content-Type"], "image/png")
                self.assertEqual(owner_image["Cache-Control"], "private, no-store")
                self.assertEqual(hashlib.sha256(owner_image.content).hexdigest(), source.png_sha256)
                self.client.force_login(self.stranger)
                self.assertEqual(self.client.get(image_url).status_code, 404)
                self.assertEqual(self.client.get(topology_url).status_code, 404)
                self.client.force_login(self.owner)
                calibrated = self.client.post(topology_url, self.calibration_payload(source))
                self.assertEqual(calibrated.status_code, 302, calibrated.content.decode())
                current = project.revisions.first()
                self.assertEqual(current.number, 2)
                self.assertNotIn("floor_plans", project.revisions.get(number=1).scenario_snapshot[
                    "topology_profile"])
                self.assertEqual(current.scenario_snapshot["topology_profile"]["floor_plans"]["1"]["png_sha256"],
                                 source.png_sha256)
                page = self.client.get(calibrated["Location"])
                self.assertContains(page, image_url)
                self.assertContains(page, "Подложка привязана")
                self.assertEqual(route_result(current.scenario_snapshot["topology_profile"])["distance_m"],
                                 "14")
                if slug == "warehouse":
                    scene = measured_scene(current.scenario_snapshot)
                    self.assertEqual(scene["floors"][0]["plan"]["id"], str(source.id))
                    positions = {p["id"]: p for p in scene["floors"][0]["nodes"]}
                    self.assertEqual(positions["a"]["x"], "53.00")
                    self.assertEqual(positions["b"]["x"], "433.00")

    def test_invalid_binding_keeps_revision_and_files(self):
        project = self.make_project("warehouse", "warehouse_pallet_transfer")
        url = reverse("project_topology", args=[project.id])
        source = self.upload(url)
        for updates in ({"anchor_a": "a", "anchor_b": "a"},
                        {"pixel_b_x": "800"}, {"pixel_b_x": "20", "pixel_b_y": "80"},
                        {"evidence": ""}, {"plan_id": "00000000-0000-0000-0000-000000000009"}):
            with self.subTest(updates=updates):
                response = self.client.post(url, self.calibration_payload(source, **updates))
                self.assertEqual(response.status_code, 400)
                self.assertEqual(project.revisions.count(), 1)
                self.assertEqual(FloorPlanSource.objects.count(), 1)
        self.assertEqual(self.client.post(url, {
            "action": "upload_plan", "base_revision": "1", "floor": "1",
            "file": image_upload(), "source_description": "Не подтверждено",
        }).status_code, 400)
        self.assertEqual(FloorPlanSource.objects.count(), 1)

    def test_editing_anchor_unpins_new_revision_without_rewriting_old_plan(self):
        project = self.make_project("warehouse", "warehouse_pallet_transfer")
        url = reverse("project_topology", args=[project.id])
        source = self.upload(url)
        self.assertEqual(self.client.post(url, self.calibration_payload(source)).status_code, 302)
        old = project.revisions.first()
        updated = self.client.post(url, {
            "action": "update_node", "base_revision": "2", "node_id": "a",
            "label": "Приём", "floor": "1", "source": "Новый обмер",
            "x_m": "2", "y_m": "0", "coordinate_source": "Повторная съёмка",
        })
        self.assertEqual(updated.status_code, 302, updated.content.decode())
        newest = project.revisions.first()
        self.assertEqual(newest.number, 3)
        self.assertFalse(newest.scenario_snapshot["topology_profile"].get("floor_plans"))
        self.assertIn("1", old.scenario_snapshot["topology_profile"]["floor_plans"])
        self.assertContains(self.client.get(url + "?revision=2"), "Подложка привязана")
        self.assertNotContains(self.client.get(updated["Location"]), "Подложка привязана")
        self.assertEqual(FloorPlanSource.objects.get(pk=source.pk).png_sha256, source.png_sha256)

    def test_report_packages_exact_archived_plan_in_pdf_svg_and_manifest(self):
        from projects.test_resource_integration import ResourceRunJourneyTests
        from projects.test_finance import _contract_rows, _csv

        owner, project, _, previous = ResourceRunJourneyTests().make_resource_project()
        topology = deepcopy(previous.scenario_snapshot["topology_profile"])
        topology["edges"][0]["length_m"] = "14"
        topology["edges"][0]["waypoints_m"] = [{"x_m": "5", "y_m": "4"}]
        topology["edges"][0]["geometry_source"] = "Натурный обмер поворота"
        measured = [node for node in topology["nodes"] if node.get("coordinate_source")
                    and node.get("x_m") is not None]
        source_data = sanitize_floor_plan(image_upload())
        source = FloorPlanSource.objects.create(
            project=project, uploaded_by=owner, floor=measured[0]["floor"],
            filename="level-one.jpg", source_description="Замеры собственника", **source_data,
        )
        topology["floor_plans"] = {source.floor: calibration_for(
            topology, source, measured[0]["id"], measured[1]["id"],
            (20, 80, 220, 80), "Съёмочные марки обследования",
        )}
        snapshot = deepcopy(previous.scenario_snapshot)
        snapshot["topology_profile"] = topology
        revision = ProjectRevision.objects.create(
            project=project, number=previous.number + 1, scenario_snapshot=snapshot,
        )
        self.client.force_login(owner)
        simulation = self.client.post(reverse("project_simulation", args=[project.id]),
                                      {"base_revision": str(revision.number)})
        self.assertEqual(simulation.status_code, 302, simulation.content.decode())
        run = SimulationRun.objects.get(project=project, revision=revision)
        forecast = _contract_rows()
        for row in forecast:
            if row["month"] != "0":
                row["served_work_units"] = str(run.ledger["delivered_work_units"])
                row["volume_source_ref"] = "Обоснованный производственный прогноз"
        finance_response = self.client.post(reverse("project_finance", args=[project.id]), {
            "run": str(run.id), "file": SimpleUploadedFile("plan.csv", _csv(forecast)),
            "horizon_months": "60", "monthly_discount_rate": "0",
            "discount_rate_source": "Решение организации",
            "source_description": "Исходные коммерческие условия",
            "forecast_basis": "Повторение наблюдаемой нагрузки, подтвердил заказчик",
            "source_attested": "on",
        })
        self.assertEqual(finance_response.status_code, 302, finance_response.content.decode())
        finance = FinancePlan.objects.get(project=project, simulation_run=run)
        bundle = self.client.get(reverse("project_report_bundle", args=[project.id]),
                                 {"run": str(run.id), "plan": str(finance.id)})
        self.assertEqual(bundle.status_code, 200, bundle.content[:500])
        with ZipFile(BytesIO(bundle.content)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            recorded_route = manifest["scenario_snapshot"]["topology_profile"]["edges"][0]
            self.assertEqual(recorded_route["geometry_source"], "Натурный обмер поворота")
            expected_scene = measured_scene({"topology_profile": topology})
            expected_polyline = expected_scene["floors"][0]["edges"][0]["svg_points"].encode()
            self.assertIn(b'<polyline points="' + expected_polyline + b'"',
                          archive.read("frame.svg"))
            self.assertEqual(len(manifest["floor_plan_sources"]), 1)
            entry = manifest["floor_plan_sources"][0]
            self.assertEqual(entry["png_sha256"], source.png_sha256)
            self.assertEqual(entry["calibration"]["evidence"], "Съёмочные марки обследования")
            self.assertEqual(archive.read(entry["archive_name"]), bytes(source.image_png))
            self.assertIn(b"data:image/png;base64,", archive.read("frame.svg"))
            self.assertTrue(archive.read("report.pdf").startswith(b"%PDF-"))
        FloorPlanSource.objects.filter(pk=source.pk).update(image_png=b"corrupted")
        self.assertEqual(self.client.get(reverse("project_floor_plan", args=[project.id, source.id])).status_code,
                         404)
        self.assertEqual(self.client.get(reverse("project_topology", args=[project.id]),
                                         {"revision": str(revision.number)}).status_code, 409)
        self.assertEqual(self.client.get(reverse("project_simulation", args=[project.id]),
                                         {"revision": str(revision.number), "run": str(run.id)}).status_code, 409)
        self.assertEqual(self.client.get(reverse("project_report_bundle", args=[project.id]),
                                         {"run": str(run.id), "plan": str(finance.id)}).status_code, 409)
