"""Owner-attested path bends and source-stable 2D/3D/report geometry."""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from projects.floor_plans import calibration_for
from projects.models import Project, ProjectRevision
from projects.path_geometry import RouteGeometryError, parse_waypoint_lines, validate_waypoints
from projects.playback import measured_scene, movement_timeline, position_on_path
from projects.test_floor_plans import topology_fixture
from projects.topology import floor_drawings, route_result


def with_turns(slug="warehouse", process="warehouse_pallet_transfer"):
    topology = topology_fixture(slug, process)
    topology["edges"][0].update({
        "waypoints_m": [{"x_m": "5", "y_m": "4"}],
        "geometry_source": "Точки геодезической съёмки",
    })
    return topology


class PathGeometryTests(SimpleTestCase):
    def test_source_and_measured_route_length_are_independent(self):
        for slug, process in (("warehouse", "warehouse_pallet_transfer"),
                              ("airport", "airport_baggage_transport"),
                              ("hospital", "hospital_meal_delivery")):
            with self.subTest(slug=slug):
                topology = topology_fixture(slug, process)
                if slug == "airport":
                    for node in topology["nodes"]:
                        node["zone"] = "restricted"
                    topology["edges"][0]["access"] = "restricted"
                input_points = parse_waypoint_lines("5;4\n")
                points = validate_waypoints(topology, "a", "b", input_points,
                                            "Обмер узла поворота", Decimal("14"))
                topology["edges"][0]["waypoints_m"] = points
                topology["edges"][0]["geometry_source"] = "Обмер узла поворота"
                outbound = route_result(topology)
                self.assertEqual(outbound["distance_m"], "14")
                drawing = floor_drawings(topology, outbound)[0]
                path = drawing["edges"][0]["path"]
                self.assertEqual(len(path), 3)
                self.assertNotEqual(path[1]["y"], path[0]["y"])
                scene = measured_scene({"topology_profile": topology})
                self.assertEqual(len(scene["floors"][0]["edges"][0]["path"]), 3)

    def test_invalid_geometry_not_promoted_to_verified_path(self):
        topo = topology_fixture("warehouse", "warehouse_pallet_transfer")
        source = "Инструментальные обмеры"
        for waypoints, length, evidence in (
            ([{"x_m": "5", "y_m": "4"}], "12", source),
            ([{"x_m": "0", "y_m": "0"}], "14", source),
            ([{"x_m": "5", "y_m": "4"}], "14", ""),
            ([{"x_m": "5", "y_m": "4"}], None, source),
            ([{"x_m": "NaN", "y_m": "4"}], "14", source),
            ([{"x_m": "5", "y_m": "4"}] * 33, "1000", source),
        ):
            with self.subTest(waypoints=waypoints, length=length, source=evidence):
                with self.assertRaises(RouteGeometryError):
                    validate_waypoints(topo, "a", "b", waypoints, evidence, length)
        topo["nodes"][1]["floor"] = "2"
        with self.assertRaises(RouteGeometryError):
            validate_waypoints(topo, "a", "b", [{"x_m": "5", "y_m": "4"}], source, "14")
        with self.assertRaises(RouteGeometryError):
            parse_waypoint_lines("4,5,6")

    def test_turns_share_exact_calibrated_positions_across_editor_and_scene(self):
        topo = with_turns()
        class Source:
            id = "00000000-0000-0000-0000-000000000001"
            floor = "1"
            width_px = 300
            height_px = 200
            png_sha256 = "b" * 64

        topo["floor_plans"] = {"1": calibration_for(
            topo, Source, "a", "b", (20, 80, 220, 80), "Точки съёмки")}
        editor = floor_drawings(topo, route_result(topo))[0]
        snapshot = measured_scene({"topology_profile": topo})
        self.assertEqual(editor["edges"][0]["svg_points"],
                         snapshot["floors"][0]["edges"][0]["svg_points"])
        self.assertEqual(editor["edges"][0]["path"][1]["y"], "10.00")
        self.assertEqual(snapshot["floors"][0]["plan"]["png_sha256"], Source.png_sha256)

    def test_turns_share_editor_projection_even_without_floor_image(self):
        for slug, process in (("warehouse", "warehouse_pallet_transfer"),
                              ("airport", "airport_baggage_transport"),
                              ("hospital", "hospital_meal_delivery")):
            with self.subTest(slug=slug):
                topo = with_turns(slug, process)
                if slug == "airport":
                    for node in topo["nodes"]:
                        node["zone"] = "restricted"
                    topo["edges"][0]["access"] = "restricted"
                editor = floor_drawings(topo, route_result(topo))[0]
                scene = measured_scene({"topology_profile": topo})["floors"][0]
                self.assertIsNone(scene["plan"])
                self.assertEqual(scene["edges"][0]["svg_points"],
                                 editor["edges"][0]["svg_points"])
                editor_nodes = {node["id"]: (node["x"], node["y"])
                                for node in editor["nodes"]}
                scene_nodes = {node["id"]: (node["x"], node["y"])
                               for node in scene["nodes"]}
                self.assertEqual(scene_nodes, editor_nodes)
                start, bend = scene["edges"][0]["path"][:2]
                # Projection must preserve measured metre ratios; otherwise
                # screen-space arc length distorts when the path turns.
                dx = abs(Decimal(bend["x"]) - Decimal(start["x"]))
                dy = abs(Decimal(bend["y"]) - Decimal(start["y"]))
                self.assertAlmostEqual(float(dy / dx), 4 / 5, delta=0.001)

    def test_v3_forward_reverse_replay_and_v2_preserve_exact_cycle_time(self):
        topology = with_turns()
        snapshot = {"topology_profile": topology, "workload_profile": {
            "parameters": {
                "observed_speed_m_s": {"value": "1", "source": "Спидометр"},
                "pickup_time_s": {"value": "1", "source": "Хронометраж"},
                "dropoff_time_s": {"value": "1", "source": "Хронометраж"},
            },
        }}
        scene = measured_scene(snapshot)
        steps = [
            {"kind": "pickup", "from": "a", "to": "a", "start_s": "0", "end_s": "1"},
            {"kind": "travel", "from": "a", "to": "b", "start_s": "1", "end_s": "15"},
            {"kind": "dropoff", "from": "b", "to": "b", "start_s": "15", "end_s": "16"},
            {"kind": "travel", "from": "b", "to": "a", "start_s": "16", "end_s": "30"},
        ]
        ledger_v3 = {"version": 3, "motion_cycles": [{
            "robot_id": "R1", "source_row": 2, "start_s": "0", "end_s": "30",
            "stages": steps,
        }]}
        timeline = movement_timeline(snapshot, {}, ledger_v3, scene, page_start="0", page_end="30")
        self.assertIsNone(timeline["reason"])
        forward, reverse = timeline["cycles"][0]["stages"][1], timeline["cycles"][0]["stages"][3]
        self.assertEqual(forward["path"], list(reversed(reverse["path"])))
        apex = forward["path"][1]
        midpoint = position_on_path(forward, Decimal("0.5"))
        self.assertAlmostEqual(float(midpoint[0]), float(apex["x"]), delta=0.05)
        self.assertAlmostEqual(float(midpoint[1]), float(apex["y"]), delta=0.05)
        self.assertEqual(forward["end_s"], "15")

        ledger_v2 = {"events": [{"type": "start", "at_s": "0",
                                 "robot_id": "R1", "source_row": 2}]}
        legacy = movement_timeline(snapshot, {"cycle_seconds": "30", "handoff_seconds": "16"},
                                   ledger_v2, scene, page_start="0", page_end="30")
        self.assertIsNone(legacy["reason"])
        self.assertEqual(legacy["stages"][1]["path"], forward["path"])
        self.assertEqual(legacy["stages"][3]["path"], reverse["path"])
        self.assertEqual(legacy["cycles"][0]["end_s"], "30")


class PathFormJourneyTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user(username="path-test-user")
        self.client.force_login(self.owner)

    def _setup(self, slug, process):
        project = Project.objects.create(owner=self.owner, object_slug=slug, name=slug)
        revision = ProjectRevision.objects.create(project=project, number=1, scenario_snapshot={
            "task_profile": {"process": process},
            "topology_profile": topology_fixture(slug, process),
        })
        return project, revision

    def _update(self, project, revision, **overrides):
        payload = {
            "action": "update_edge", "base_revision": str(revision.number),
            "edge_id": "ab", "start": "a", "end": "b", "source": "Проверенный проход",
            "length_m": "14", "length_source": "Обмер вдоль пути",
            "bidirectional": "on", "waypoints_text": "5;4",
            "geometry_source": "Точки по обследованию объекта",
        }
        if project.object_slug == "airport":
            payload["access"] = "public"
        if project.object_slug == "hospital":
            payload.update(kind="corridor", flow="clean")
        payload.update(overrides)
        return self.client.post(reverse("project_topology", args=[project.id]), payload)

    def test_persist_route_shape_and_discard_on_anchor_coordinate_change(self):
        for slug, process in (("warehouse", "warehouse_pallet_transfer"),
                              ("airport", "airport_baggage_transport"),
                              ("hospital", "hospital_meal_delivery")):
            with self.subTest(slug=slug):
                project, revision = self._setup(slug, process)
                response = self._update(project, revision)
                self.assertEqual(response.status_code, 302, response.content.decode())
                saved = project.revisions.first()
                self.assertEqual(saved.number, 2)
                edge = saved.scenario_snapshot["topology_profile"]["edges"][0]
                self.assertEqual(edge["waypoints_m"], [{"x_m": "5", "y_m": "4"}])
                self.assertEqual(edge["length_m"], "14")
                self.assertContains(self.client.get(response["Location"]), "1 поворотов")
                self.assertContains(self.client.get(response["Location"]), "<polyline", html=False)
                edit_page = self.client.get(reverse("project_topology", args=[project.id]) + "?edge=ab")
                self.assertContains(edit_page, "5;4")
                update = self.client.post(reverse("project_topology", args=[project.id]), {
                    "action": "update_node", "base_revision": "2", "node_id": "a",
                    "label": "Приём", "floor": "1", "source": "Новый обмер",
                    "x_m": "1", "y_m": "0", "coordinate_source": "Повторная съёмка",
                })
                self.assertEqual(update.status_code, 302)
                newest = project.revisions.first()
                self.assertEqual(newest.number, 3)
                self.assertNotIn("waypoints_m", newest.scenario_snapshot["topology_profile"]["edges"][0])
                self.assertIn("waypoints_m", saved.scenario_snapshot["topology_profile"]["edges"][0])

    def test_invalid_path_never_creates_revision(self):
        project, revision = self._setup("warehouse", "warehouse_pallet_transfer")
        for changes in ({"geometry_source": ""}, {"length_m": "11"},
                        {"waypoints_text": "0;0"}, {"waypoints_text": "NaN;1"},
                        {"waypoints_text": "5;4;3"}, {"waypoints_text": "", "geometry_source": "Обмер"}):
            with self.subTest(changes=changes):
                response = self._update(project, revision, **changes)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(project.revisions.count(), 1)

    def test_shape_removal_is_new_revision_and_incomplete_archive_is_rejected(self):
        project, base = self._setup("warehouse", "warehouse_pallet_transfer")
        self.assertEqual(self._update(project, base).status_code, 302)
        curved = project.revisions.first()
        cleared = self._update(project, curved, waypoints_text="", geometry_source="")
        self.assertEqual(cleared.status_code, 302)
        latest = project.revisions.first()
        self.assertNotIn("waypoints_m", latest.scenario_snapshot["topology_profile"]["edges"][0])
        self.assertIn("waypoints_m", curved.scenario_snapshot["topology_profile"]["edges"][0])
        invalid_snapshot = dict(curved.scenario_snapshot)
        invalid_topology = dict(invalid_snapshot["topology_profile"])
        invalid_topology["edges"] = [dict(edge) for edge in invalid_topology["edges"]]
        invalid_topology["edges"][0].pop("geometry_source")
        invalid_snapshot["topology_profile"] = invalid_topology
        ProjectRevision.objects.filter(pk=curved.pk).update(scenario_snapshot=invalid_snapshot)
        self.assertEqual(self.client.get(reverse("project_topology", args=[project.id]),
                                         {"revision": str(curved.number)}).status_code, 409)
        # An explicit malformed value is corrupt; only an absent key means
        # that the archived route uses the legacy straight-line rendering.
        for malformed in ({}, None, "5;4"):
            with self.subTest(malformed=malformed):
                invalid_topology["edges"][0]["waypoints_m"] = malformed
                ProjectRevision.objects.filter(pk=curved.pk).update(scenario_snapshot=invalid_snapshot)
                self.assertEqual(self.client.get(reverse("project_topology", args=[project.id]),
                                                 {"revision": str(curved.number)}).status_code, 409)
