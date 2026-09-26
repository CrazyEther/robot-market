"""Project isolation and source-bound import checks."""

import os
import csv
import hashlib
import io
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase
from django.test import Client
from django.urls import reverse

from catalog.importing import import_catalog
from catalog.evidence_importing import import_evidence
from catalog.models import CatalogEvidenceClaim, CatalogSourceRow
from projects.input_profiles import profile_for_revision
from projects.matching import match_candidates
from projects.selection_refs import selection_ref
from projects.models import AvailabilityPlan, OperationLog, Project, ProjectRevision
from projects.operation_logs import MAX_UPLOAD_BYTES, OperationLogError, parse_operation_log
from projects.event_ledger import EventInputError, schedule_observed_jobs
from projects.playback import availability_at_events, measured_scene, movement_timeline, state_before
from projects.availability import (
    AvailabilityError, available_windows, parse_availability, verified_availability_rows,
)
from projects.sizing import (
    calculate_stationary_fleet, hospital_leg_measurements, manufacturer_speed_issue,
    size_project,
)
from projects.task_profiles import process_for
from projects.topology import empty_topology, route_result, transport_cycle_route


class AccountOnboardingTests(TestCase):
    def test_registration_returns_to_requested_project_and_authenticates(self):
        target = reverse("project_create", args=["warehouse"])
        response = self.client.get(target)
        self.assertRedirects(response, f"{reverse('login')}?next={target}")
        response = self.client.get(response["Location"])
        self.assertContains(response, f"{reverse('register')}?next={target}")
        response = self.client.post(reverse("register"), {
            "username": "warehouse-owner",
            "password1": "Unique-and-strong-passphrase-829",
            "password2": "Unique-and-strong-passphrase-829",
            "next": target,
        })
        self.assertRedirects(response, target)
        self.assertEqual(get_user_model().objects.count(), 1)
        self.assertEqual(int(self.client.session["_auth_user_id"]),
                         get_user_model().objects.get(username="warehouse-owner").pk)

    def test_invalid_registration_does_not_create_account_or_redirect_off_site(self):
        endpoint = reverse("register")
        invalid = self.client.post(endpoint, {
            "username": "owner", "password1": "short", "password2": "short",
            "next": "https://other.example/steal",
        })
        self.assertEqual(invalid.status_code, 200)
        self.assertEqual(get_user_model().objects.count(), 0)
        valid = self.client.post(endpoint, {
            "username": "owner", "password1": "Unique-and-strong-passphrase-829",
            "password2": "Unique-and-strong-passphrase-829",
            "next": "https://other.example/steal",
        })
        self.assertRedirects(valid, reverse("project_list"))
        self.assertEqual(get_user_model().objects.count(), 1)
        self.client.logout()
        duplicate = self.client.post(endpoint, {
            "username": "owner", "password1": "Another-strong-passphrase-927",
            "password2": "Another-strong-passphrase-927",
        })
        self.assertEqual(duplicate.status_code, 200)
        self.assertEqual(get_user_model().objects.count(), 1)


class OperationLogIngestionTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user(username="log-owner")
        self.other = get_user_model().objects.create_user(username="log-other")
        self.client.force_login(self.owner)

    def _project(self, slug, process_code):
        project = Project.objects.create(owner=self.owner, name=slug, object_slug=slug)
        ProjectRevision.objects.create(
            project=project, number=1,
            scenario_snapshot={"object_slug": slug, "task_profile": {"process": process_code}},
        )
        return project

    def _upload(self, project, raw, revision=1):
        return self.client.post(reverse("project_operation_log", args=[project.id]), {
            "base_revision": revision,
            "file": SimpleUploadedFile("operations.csv", raw, content_type="text/csv"),
            "period_start": "2026-09-25T07:00:00+05:00",
            "period_end": "2026-09-25T09:00:00+05:00",
            "source_description": "Журнал системы объекта за проверенный период",
            "source_attested": "on",
        })

    def test_each_object_preserves_raw_log_and_reopens_revision(self):
        for slug, process_code in (
            ("warehouse", "warehouse_pallet_transfer"),
            ("airport", "airport_baggage_transport"),
            ("hospital", "hospital_meal_delivery"),
            ("airport", "airport_terminal_cleaning"),
            ("hospital", "hospital_floor_cleaning"),
        ):
            with self.subTest(process_code=process_code):
                project = self._project(slug, process_code)
                units = b"1" if "cleaning" not in process_code else b"12.5"
                raw = (b"requested_at,work_units\n"
                       + b"2026-09-25T08:00:00+05:00," + units + b"\n"
                       + b"2026-09-25T08:02:00+05:00," + units + b"\n")
                response = self._upload(project, raw)
                self.assertEqual(response.status_code, 302)
                log = OperationLog.objects.get(project=project)
                self.assertEqual(bytes(log.raw_csv), raw)
                self.assertEqual(log.sha256, hashlib.sha256(raw).hexdigest())
                self.assertEqual(log.rows[0]["source_row"], 2)
                self.assertEqual(log.rows[0]["requested_at_utc"], "2026-09-25T03:00:00+00:00")
                self.assertEqual(log.period_start_at.isoformat(), "2026-09-25T02:00:00+00:00")
                self.assertEqual(log.period_end_at.isoformat(), "2026-09-25T04:00:00+00:00")
                latest = project.revisions.first()
                self.assertEqual(latest.scenario_snapshot["operation_log_ref"]["id"], str(log.id))
                self.assertNotIn("operation_log_ref", project.revisions.get(number=1).scenario_snapshot)
                self.assertContains(self.client.get(response["Location"]), "2 записей")
                self.assertEqual(self._upload(project, raw, latest.number).status_code, 302)
                self.assertEqual(project.revisions.count(), 2)
                self.assertEqual(OperationLog.objects.filter(project=project).count(), 1)

    def test_invalid_or_private_log_and_stale_revision_are_rejected(self):
        project = self._project("warehouse", "warehouse_pallet_transfer")
        endpoint = reverse("project_operation_log", args=[project.id])
        invalid_rows = (
            b"requested_at,work_units,patient_name\n2026-09-25T08:00:00+05:00,1,Person\n",
            b"requested_at,work_units\n2026-09-25T08:00:00,1\n",
            b"requested_at,work_units\n2026-09-25T08:02:00+05:00,1\n2026-09-25T08:00:00+05:00,1\n",
            b"requested_at,work_units\n2026-09-25T08:00:00+05:00,NaN\n",
            b"requested_at,work_units\n2026-09-25T08:00:00+05:00,2\n",
        )
        for raw in invalid_rows:
            with self.subTest(raw=raw):
                self.assertEqual(self._upload(project, raw).status_code, 400)
        self.assertEqual(OperationLog.objects.count(), 0)
        outside = b"requested_at,work_units\n2026-09-25T09:00:00+05:00,1\n"
        self.assertEqual(self._upload(project, outside).status_code, 400)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(endpoint).status_code, 404)
        self.assertEqual(self._upload(project, invalid_rows[0]).status_code, 404)
        self.client.force_login(self.owner)
        valid = b"requested_at,work_units\n2026-09-25T08:00:00+05:00,1\n"
        self.assertEqual(self._upload(project, valid).status_code, 302)
        self.assertEqual(self._upload(project, valid, 1).status_code, 409)
        self.assertEqual(OperationLog.objects.count(), 1)

    def test_parser_rejects_empty_oversized_and_excessive_precision(self):
        process = process_for("hospital", "hospital_floor_cleaning")
        for raw in (
            b"", b"\xef\xbb\xbf", b"x" * (MAX_UPLOAD_BYTES + 1),
            "requested_at,work_units\n".encode("utf-16"),
            b"requested_at,work_units\n2026-09-25T08:00:00+05:00,1e9999\n",
            b"requested_at,work_units\n2026-09-25T08:00:00+05:00,1.00001\n",
        ):
            with self.subTest(length=len(raw)):
                with self.assertRaises(OperationLogError):
                    parse_operation_log(raw, process)
        accepted = parse_operation_log(
            b'"requested_at";"work_units"\n"2026-09-25T08:00:00+05:00";"12.5"\n',
            process,
        )
        self.assertEqual(accepted[0]["unit"], "м²")

    def test_changing_operation_drops_old_log_from_new_revision(self):
        project = self._project("airport", "airport_baggage_transport")
        raw = b"requested_at,work_units\n2026-09-25T08:00:00+05:00,1\n"
        self.assertEqual(self._upload(project, raw).status_code, 302)
        old = project.revisions.first()
        response = self.client.post(reverse("project_task", args=[project.id]), {
            "process": "airport_terminal_cleaning", "base_revision": old.number,
        })
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("operation_log_ref", project.revisions.first().scenario_snapshot)
        self.assertIn("operation_log_ref", old.scenario_snapshot)

    def test_upload_requires_csrf_and_valid_revision_query(self):
        project = self._project("hospital", "hospital_meal_delivery")
        endpoint = reverse("project_operation_log", args=[project.id])
        strict_client = Client(enforce_csrf_checks=True)
        strict_client.force_login(self.owner)
        response = strict_client.post(endpoint, {
            "base_revision": 1,
            "file": SimpleUploadedFile(
                "operations.csv",
                b"requested_at,work_units\n2026-09-25T08:00:00+05:00,1\n",
                content_type="text/csv",
            ),
            "source_description": "Журнал объекта",
            "period_start": "2026-09-25T07:00:00+05:00",
            "period_end": "2026-09-25T09:00:00+05:00",
            "source_attested": "on",
        })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(OperationLog.objects.count(), 0)
        self.assertEqual(self.client.get(endpoint + "?revision=" + "9" * 5000).status_code, 404)
        availability = reverse("project_availability", args=[project.id])
        simulation = reverse("project_simulation", args=[project.id])
        self.assertContains(self.client.get(availability), "Календарь роботов")
        self.assertContains(self.client.get(simulation), "Симуляция")
        self.assertEqual(self.client.post(availability, {"base_revision": "1"}).status_code, 409)
        self.assertEqual(self.client.post(simulation, {"base_revision": "1"}).status_code, 409)
        self.assertEqual(self.client.get(availability + "?revision=" + "9" * 5000).status_code, 404)
        self.assertEqual(self.client.get(simulation + "?run=invalid").status_code, 404)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(availability).status_code, 404)
        self.assertEqual(self.client.get(simulation).status_code, 404)


class ObservedEventLedgerTests(SimpleTestCase):
    def test_fifo_queue_and_period_boundary_follow_independent_arithmetic(self):
        start = datetime.fromisoformat("2026-09-25T08:00:00+05:00")
        end = start + timedelta(minutes=7)
        jobs = [
            {"source_row": number, "requested_at_utc": (start + timedelta(minutes=minute)).astimezone(timezone.utc).isoformat(),
             "work_units": "1", "service_seconds": "180", "handoff_seconds": "90"}
            for number, minute in ((2, 0), (3, 1), (4, 2))
        ]
        robots = [{"id": "robot-1", "windows": [{
            "start_at": start.isoformat(), "end_at": (start + timedelta(minutes=10)).isoformat(),
            "source": "Подтверждённый календарь доступности",
        }]}]
        result = schedule_observed_jobs(jobs, robots, period_start=start, period_end=end)
        self.assertEqual((result["arrivals"], result["started"], result["completed"]), (3, 3, 2))
        self.assertEqual((result["queued_at_end"], result["in_progress_at_end"], result["unmet_at_end"]),
                         (0, 1, 1))
        self.assertEqual(result["max_queue"], 2)
        self.assertEqual([event["at_s"] for event in result["events"] if event["type"] == "start"],
                         ["0", "180", "360"])
        self.assertEqual([event["at_s"] for event in result["events"] if event["type"] == "complete"],
                         ["180", "360"])
        self.assertEqual([event["at_s"] for event in result["events"] if event["type"] == "handoff"],
                         ["90", "270"])
        self.assertEqual(result["delivered"], 2)
        before_second = state_before(result["events"], 2)
        self.assertEqual(before_second["queued"], 0)
        self.assertEqual(before_second["active"], {"robot-1": 2})
        self.assertEqual(state_before(result["events"], len(result["events"]))["completed"],
                         result["completed"])

    def test_second_real_resource_window_can_reduce_queue_without_new_arrivals(self):
        start = datetime.fromisoformat("2026-09-25T08:00:00+05:00")
        end = start + timedelta(minutes=5)
        jobs = [
            {"source_row": row, "requested_at_utc": start.isoformat(),
             "work_units": "1", "service_seconds": "180", "handoff_seconds": "90"}
            for row in (2, 3)
        ]
        window = {"start_at": start.isoformat(), "end_at": end.isoformat(),
                  "source": "Подтверждённый календарь доступности"}
        one = schedule_observed_jobs(jobs, [{"id": "r1", "windows": [window]}],
                                     period_start=start, period_end=end)
        two = schedule_observed_jobs(jobs, [{"id": "r1", "windows": [window]},
                                             {"id": "r2", "windows": [window]}],
                                     period_start=start, period_end=end)
        self.assertEqual(one["completed"], 1)
        self.assertEqual(two["completed"], 2)
        self.assertGreaterEqual(one["max_queue"], two["max_queue"])

    def test_unverified_or_overlapping_resource_windows_are_rejected(self):
        start = datetime.fromisoformat("2026-09-25T08:00:00+05:00")
        end = start + timedelta(minutes=10)
        jobs = [{"source_row": 2, "requested_at_utc": start.isoformat(),
                 "work_units": "1", "service_seconds": "60", "handoff_seconds": "30"}]
        with self.assertRaises(EventInputError):
            schedule_observed_jobs(jobs, [{"id": "r1", "windows": [{
                "start_at": start.isoformat(), "end_at": end.isoformat(), "source": "",
            }]}], period_start=start, period_end=end)
        with self.assertRaises(EventInputError):
            schedule_observed_jobs(jobs, [{"id": "r1", "windows": [
                {"start_at": start.isoformat(), "end_at": end.isoformat(), "source": "План"},
                {"start_at": (start + timedelta(minutes=2)).isoformat(),
                 "end_at": end.isoformat(), "source": "План"},
            ]}], period_start=start, period_end=end)

    def test_handoff_before_period_end_counts_delivery_even_if_robot_has_not_returned(self):
        start = datetime.fromisoformat("2026-09-25T08:00:00+05:00")
        end = start + timedelta(minutes=2)
        jobs = [{"source_row": 2, "requested_at_utc": start.isoformat(),
                 "work_units": "1", "service_seconds": "180", "handoff_seconds": "60"}]
        robots = [{"id": "r1", "windows": [{"start_at": start.isoformat(),
                   "end_at": (start + timedelta(minutes=5)).isoformat(),
                   "source": "Измеренный календарь"}]}]
        ledger = schedule_observed_jobs(jobs, robots, period_start=start, period_end=end)
        self.assertEqual((ledger["arrivals"], ledger["delivered"], ledger["completed"]),
                         (1, 1, 0))
        self.assertEqual((ledger["unmet_at_end"], ledger["in_progress_at_end"]), (0, 1))
        self.assertEqual(ledger["delivered_work_units"], "1")


class AvailabilitySourceTests(SimpleTestCase):
    def test_exact_calendar_coverage_and_state_windows(self):
        start = datetime.fromisoformat("2026-09-25T08:00:00+05:00")
        end = start + timedelta(minutes=10)
        raw = (b"robot_slot,start_at,end_at,state,ready_at_node\n"
               b"1,2026-09-25T08:00:00+05:00,2026-09-25T08:05:00+05:00,available,origin\n"
               b"1,2026-09-25T08:05:00+05:00,2026-09-25T08:10:00+05:00,charging,\n"
               b"2,2026-09-25T08:00:00+05:00,2026-09-25T08:10:00+05:00,available,origin\n")
        rows = parse_availability(raw, period_start=start, period_end=end, fleet=2, origin_node="origin")
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["start_at_utc"], "2026-09-25T03:00:00+00:00")
        self.assertEqual(rows[0]["ready_at_node"], "origin")
        self.assertIsNone(rows[1]["ready_at_node"])
        windows = available_windows(rows, source="Подтверждённый сменный календарь")
        self.assertEqual(len(windows), 2)
        self.assertEqual(len(windows[0]["windows"]), 1)
        self.assertEqual(windows[0]["windows"][0]["source_row"], 2)
        counts = availability_at_events(rows, [
            {"at_s": "0"}, {"at_s": "300"}, {"at_s": "600"},
        ], start.isoformat())
        self.assertEqual(counts[0], {"available": 2, "charging": 0,
                                     "maintenance": 0, "downtime": 0})
        self.assertEqual((counts[1]["available"], counts[1]["charging"]), (1, 1))
        self.assertEqual(sum(counts[2].values()), 0)

    def test_measured_scene_keeps_floors_separate_and_never_draws_missing_coordinates(self):
        for slug, process, zone in (
            ("warehouse", "warehouse_pallet_transfer", None),
            ("airport", "airport_baggage_transport", "restricted"),
        ):
            with self.subTest(slug=slug):
                flat = empty_topology(slug, process)
                flat.update({
                    "origin": "a", "destination": "b",
                    "nodes": [
                        {"id": "a", "label": "A", "floor": "",
                         "zone": zone, "x_m": "0", "y_m": "0",
                         "coordinate_source": "Измерение A"},
                        {"id": "b", "label": "B", "floor": "",
                         "zone": zone, "x_m": "10", "y_m": "0",
                         "coordinate_source": "Измерение B"},
                    ],
                    "edges": [{"id": "passage", "start": "a", "end": "b",
                               "bidirectional": True, "access": zone,
                               "length_m": "10", "length_source": "Измерение участка"}],
                })
                scene = measured_scene({"topology_profile": flat})
                self.assertEqual(len(scene["floors"]), 1)
                self.assertEqual(len(scene["floors"][0]["edges"]), 1)
                self.assertTrue(scene["floors"][0]["edges"][0]["on_route"])
                self.assertEqual(scene["transitions"], [])
        topology = empty_topology("hospital", "hospital_meal_delivery")
        topology.update({
            "origin": "a", "destination": "b", "route_flow": "clean",
            "nodes": [
                {"id": "a", "label": "A", "floor": "1", "zone": "clean",
                 "x_m": "0", "y_m": "0", "coordinate_source": "Измерение A"},
                {"id": "b", "label": "B", "floor": "2", "zone": "clean",
                 "x_m": "0", "y_m": "0", "coordinate_source": "Измерение B"},
            ],
            "edges": [{"id": "lift", "start": "a", "end": "b", "kind": "elevator",
                       "flow": "clean", "bidirectional": True, "length_m": "10",
                       "length_source": "Измерение лифта"}],
        })
        scene = measured_scene({"topology_profile": topology})
        self.assertEqual(len(scene["floors"]), 2)
        self.assertTrue(all(not floor["edges"] for floor in scene["floors"]))
        self.assertEqual(len(scene["transitions"]), 2)
        self.assertEqual([(step["from_floor"], step["to_floor"])
                          for step in scene["transitions"]], [("1", "2"), ("2", "1")])
        topology["nodes"][1]["coordinate_source"] = None
        scene = measured_scene({"topology_profile": topology})
        self.assertEqual(scene["missing_coordinates"], ["B"])
        self.assertEqual(len(scene["floors"]), 1)

    def test_motion_keeps_saved_cycle_times_and_floor_transitions(self):
        topology = empty_topology("hospital", "hospital_meal_delivery")
        topology.update({
            "origin": "a", "destination": "b", "route_flow": "clean",
            "nodes": [
                {"id": "a", "label": "A", "floor": "1", "zone": "clean",
                 "x_m": "0", "y_m": "0", "coordinate_source": "Измерение A"},
                {"id": "b", "label": "B", "floor": "2", "zone": "clean",
                 "x_m": "0", "y_m": "0", "coordinate_source": "Измерение B"},
            ],
            "edges": [{"id": "lift", "start": "a", "end": "b", "kind": "elevator",
                       "flow": "clean", "bidirectional": True, "length_m": "10",
                       "length_source": "Измерение лифта"}],
        })
        snapshot = {"topology_profile": topology, "workload_profile": {"parameters": {
            "observed_speed_m_s": {"value": "1", "source": "Наблюдение"},
            "pickup_time_s": {"value": "10", "source": "Наблюдение"},
            "dropoff_time_s": {"value": "20", "source": "Наблюдение"},
            "outbound_elevator_wait_s": {"value": "5", "source": "Наблюдение"},
            "inbound_elevator_wait_s": {"value": "7", "source": "Наблюдение"},
            "outbound_elevator_ride_s": {"value": "5", "source": "Наблюдение"},
            "inbound_elevator_ride_s": {"value": "7", "source": "Наблюдение"},
        }}}
        sizing = {"cycle_seconds": "54", "handoff_seconds": "40"}
        legs = hospital_leg_measurements(transport_cycle_route(topology), topology)
        self.assertEqual([legs[leg]["ground_distance_m"] for leg in ("outbound", "inbound")],
                         [Decimal(0), Decimal(0)])
        self.assertEqual([legs[leg]["elevator_count"] for leg in ("outbound", "inbound")],
                         [1, 1])
        ledger = {"events": [
            {"type": "start", "at_s": "30", "robot_id": "r1", "source_row": 2},
            {"type": "start", "at_s": "40", "robot_id": "r2", "source_row": 3},
        ]}
        scene = measured_scene(snapshot)
        motion = movement_timeline(snapshot, sizing, ledger, scene,
                                   page_start="35", page_end="50")
        self.assertIsNone(motion["reason"])
        self.assertEqual({cycle["robot_id"] for cycle in motion["cycles"]}, {"r1", "r2"})
        self.assertEqual(motion["stages"][-1]["end_s"], sizing["cycle_seconds"])
        self.assertEqual(next(stage for stage in motion["stages"]
                              if stage["kind"] == "dropoff")["end_s"], sizing["handoff_seconds"])
        self.assertEqual([(stage["from"]["floor"], stage["to"]["floor"])
                          for stage in motion["stages"] if stage["kind"] == "elevator"],
                         [("1", "2"), ("2", "1")])
        self.assertEqual([stage["end_s"] for stage in motion["stages"]
                          if stage["kind"] == "elevator"], ["20", "54"])
        snapshot["workload_profile"]["parameters"].pop("outbound_elevator_ride_s")
        self.assertIn("измеренные ожидание и поездка", movement_timeline(
            snapshot, sizing, ledger, scene, page_start="35", page_end="50",
        )["reason"])
        topology["nodes"][1]["coordinate_source"] = None
        self.assertIsNotNone(movement_timeline(snapshot, sizing, ledger, measured_scene(snapshot),
                                               page_start="35", page_end="50")["reason"])

    def test_missing_slot_gap_overlap_and_extra_identifier_are_rejected(self):
        start = datetime.fromisoformat("2026-09-25T08:00:00+05:00")
        end = start + timedelta(minutes=10)
        header = b"robot_slot,start_at,end_at,state,ready_at_node\n"
        first = b"1,2026-09-25T08:00:00+05:00,2026-09-25T08:05:00+05:00,available,origin\n"
        for raw, fleet in (
            (header + first, 2),
            (header + first + b"1,2026-09-25T08:06:00+05:00,2026-09-25T08:10:00+05:00,charging,\n", 1),
            (header + first + b"1,2026-09-25T08:04:00+05:00,2026-09-25T08:10:00+05:00,charging,\n", 1),
            (b"robot_slot,start_at,end_at,state,patient_id\n", 1),
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(AvailabilityError):
                    parse_availability(raw, period_start=start, period_end=end,
                                       fleet=fleet, origin_node="origin")

    def test_every_available_interval_must_confirm_route_origin(self):
        start = datetime.fromisoformat("2026-09-25T08:00:00+05:00")
        end = start + timedelta(minutes=10)
        header = b"robot_slot,start_at,end_at,state,ready_at_node\n"
        charging = b"1,2026-09-25T08:00:00+05:00,2026-09-25T08:05:00+05:00,charging,\n"
        second = b"1,2026-09-25T08:05:00+05:00,2026-09-25T08:10:00+05:00,available,origin\n"
        for object_slug, process_code in (
            ("warehouse", "warehouse_pallet_transfer"),
            ("airport", "airport_baggage_transport"),
            ("hospital", "hospital_meal_delivery"),
        ):
            with self.subTest(object_slug=object_slug):
                topology = empty_topology(object_slug, process_code)
                topology["origin"] = "origin"
                rows = parse_availability(header + charging + second,
                                          period_start=start, period_end=end, fleet=1,
                                          origin_node=topology["origin"])
                self.assertEqual(rows[-1]["ready_at_node"], topology["origin"])
                for invalid in (b"", b"other"):
                    with self.assertRaises(AvailabilityError):
                        parse_availability(header + charging + second.replace(b"origin", invalid),
                                           period_start=start, period_end=end, fleet=1,
                                           origin_node=topology["origin"])
                with self.assertRaises(AvailabilityError):
                    parse_availability(header + charging.replace(b"charging,", b"charging,origin") + second,
                                       period_start=start, period_end=end, fleet=1,
                                       origin_node=topology["origin"])

    def test_legacy_calendar_is_readable_only_through_its_versioned_saved_plan(self):
        start = datetime.fromisoformat("2026-09-25T08:00:00+05:00")
        end = start + timedelta(minutes=10)
        raw = (b"robot_slot,start_at,end_at,state\n"
               b"1,2026-09-25T08:00:00+05:00,2026-09-25T08:10:00+05:00,available\n")
        with self.assertRaises(AvailabilityError):
            parse_availability(raw, period_start=start, period_end=end,
                               fleet=1, origin_node="origin")
        plan = AvailabilityPlan(parser_version=1, raw_csv=raw,
                                sha256=hashlib.sha256(raw).hexdigest(), fleet=1,
                                rows=[{"source_row": 2, "robot_slot": 1,
                                       "start_at_utc": start.astimezone(timezone.utc).isoformat(),
                                       "end_at_utc": end.astimezone(timezone.utc).isoformat(),
                                       "state": "available"}])
        self.assertEqual(verified_availability_rows(plan, period_start=start,
                                                    period_end=end), plan.rows)


class AirportAccessRulesTests(TestCase):
    def test_route_access_follows_the_selected_airport_operation(self):
        topology = empty_topology("airport", "airport_baggage_transport")
        topology.update({
            "nodes": [{"id": "origin", "zone": "restricted"},
                      {"id": "destination", "zone": "restricted"}],
            "edges": [{"id": "connection", "start": "origin", "end": "destination",
                       "access": "restricted", "bidirectional": False, "length_m": None}],
            "origin": "origin", "destination": "destination",
        })
        self.assertEqual(route_result(topology)["status"], "needs_measurement")
        self.assertEqual(transport_cycle_route(topology)["inbound"]["status"], "blocked")
        topology["edges"][0]["bidirectional"] = True
        self.assertEqual(transport_cycle_route(topology)["inbound"]["status"], "needs_measurement")
        topology["edges"][0]["access"] = "excluded"
        self.assertEqual(route_result(topology)["status"], "blocked")
        topology["edges"][0]["access"] = "restricted"
        topology["nodes"][1]["zone"] = "public"
        self.assertEqual(route_result(topology)["status"], "blocked")
        topology["process"] = "airport_terminal_cleaning"
        topology["nodes"][0]["zone"] = "public"
        topology["edges"][0]["access"] = "public"
        self.assertEqual(route_result(topology)["status"], "needs_measurement")
        self.assertIsNone(transport_cycle_route(topology))
        topology["process"] = "unknown"
        self.assertEqual(route_result(topology)["status"], "invalid_process")


class HospitalCycleRulesTests(TestCase):
    def test_return_requires_the_same_lift_and_clean_flow_rules(self):
        topology = empty_topology("hospital", "hospital_meal_delivery")
        topology.update({
            "nodes": [{"id": "origin", "floor": "1", "zone": "clean"},
                      {"id": "destination", "floor": "2", "zone": "clean"}],
            "edges": [{"id": "lift", "start": "origin", "end": "destination",
                       "kind": "elevator", "flow": "clean", "bidirectional": False,
                       "length_m": None}],
            "origin": "origin", "destination": "destination", "route_flow": "clean",
        })
        self.assertEqual(transport_cycle_route(topology)["inbound"]["status"], "blocked")
        topology["edges"][0]["bidirectional"] = True
        self.assertEqual(transport_cycle_route(topology)["inbound"]["status"], "needs_measurement")
        topology["edges"][0]["flow"] = "dirty"
        self.assertEqual(transport_cycle_route(topology)["inbound"]["status"], "blocked")
        topology["edges"][0]["flow"] = "clean"
        topology["edges"][0]["kind"] = "corridor"
        self.assertEqual(transport_cycle_route(topology)["inbound"]["status"], "blocked")


class MeasuredRouteRulesTests(TestCase):
    def test_shortest_measured_route_is_independent_of_edge_order(self):
        claims = json.loads(Path(os.environ["RESEARCH_CLAIMS_PATH"]).read_text(encoding="utf-8"))
        reported = next(item for item in claims if item["id"] == "ronavi_sostra_route_length")
        length = Decimal(str(reported["value"]))
        topology = empty_topology("warehouse", "warehouse_pallet_transfer")
        topology.update({
            "nodes": [{"id": name} for name in ("origin", "via", "destination")],
            "origin": "origin", "destination": "destination",
            "edges": [
                {"id": "direct", "start": "origin", "end": "destination",
                 "bidirectional": False, "length_m": str(length * 3)},
                {"id": "first", "start": "origin", "end": "via",
                 "bidirectional": False, "length_m": str(length)},
                {"id": "second", "start": "via", "end": "destination",
                 "bidirectional": False, "length_m": str(length)},
            ],
        })
        for edges in (topology["edges"], list(reversed(topology["edges"]))):
            topology["edges"] = edges
            route = route_result(topology)
            self.assertEqual(route["status"], "measured")
            self.assertEqual(route["edges"], ["first", "second"])
            self.assertEqual(Decimal(route["distance_m"]), length * 2)
        cycle = transport_cycle_route(topology)
        self.assertEqual(cycle["inbound"]["status"], "blocked")
        self.assertIsNone(cycle["distance_m"])
        topology["edges"][2]["bidirectional"] = True
        cycle = transport_cycle_route(topology)
        self.assertEqual(cycle["inbound"]["edges"], ["direct"])
        self.assertEqual(cycle["inbound"]["steps"][0]["from"], "destination")
        self.assertEqual(cycle["inbound"]["steps"][0]["to"], "origin")
        self.assertEqual(Decimal(cycle["distance_m"]), length * 5)
        topology["edges"][0]["length_m"] = "NaN"
        topology["edges"][1]["length_m"] = None
        topology["edges"][2]["length_m"] = None
        self.assertEqual(route_result(topology)["status"], "needs_measurement")
        self.assertIsNone(transport_cycle_route(topology)["distance_m"])
        topology["object_slug"] = "airport"
        topology["process"] = "airport_terminal_cleaning"
        self.assertIsNone(transport_cycle_route(topology))


class FleetSizingArithmeticTests(TestCase):
    def test_site_speed_cannot_exceed_the_pinned_manufacturer_limit(self):
        claims = json.loads(Path(os.environ["RESEARCH_CLAIMS_PATH"]).read_text(encoding="utf-8"))
        published = next(item for item in claims if item["id"] == "ronavi_h1500_max_speed")
        limit = [{"value": published["value"], "unit": published["unit"],
                  "source_url": published["source_url"]}]
        maximum = Decimal(str(published["value"]))
        self.assertIsNone(manufacturer_speed_issue(maximum, limit))
        self.assertIn("превышает", manufacturer_speed_issue(maximum + Decimal("0.001"), limit))
        self.assertIn("расходятся", manufacturer_speed_issue(maximum, limit + [
            {**limit[0], "value": str(maximum + Decimal("0.001"))},
        ]))

    def test_peak_demand_and_cycle_time_never_reduce_the_required_fleet(self):
        claims = json.loads(Path(os.environ["RESEARCH_CLAIMS_PATH"]).read_text(encoding="utf-8"))
        published_length = Decimal(str(next(item["value"] for item in claims
                                            if item["id"] == "ronavi_sostra_route_length")))
        common = {"productive_fraction": Decimal("0.8"),
                  "reliability_fraction": Decimal("0.9"),
                  "battery_work_h": Decimal("10"), "charge_h": Decimal("1")}
        warehouse = process_for("warehouse", "warehouse_pallet_transfer")
        transport = {**common, "peak_jobs_per_h": Decimal("10"),
                     "observed_speed_m_s": Decimal("1"), "service_time_s": Decimal("0")}
        base = calculate_stationary_fleet(warehouse, transport, cycle_distance_m=published_length)
        peak = calculate_stationary_fleet(warehouse, {**transport, "peak_jobs_per_h": Decimal("20")},
                                         cycle_distance_m=published_length)
        longer = calculate_stationary_fleet(warehouse, transport, cycle_distance_m=published_length * 2)
        self.assertEqual(base["fleet"], base["active_robots"] + base["charging_robots"] + base["reserve_robots"])
        self.assertGreaterEqual(peak["fleet"], base["fleet"])
        self.assertGreaterEqual(longer["fleet"], base["fleet"])
        self.assertEqual(calculate_stationary_fleet(
            warehouse, {**transport, "peak_jobs_per_h": Decimal("0")},
            cycle_distance_m=published_length,
        )["fleet"], 0)
        hospital = process_for("hospital", "hospital_meal_delivery")
        hospital_result = calculate_stationary_fleet(
            hospital, {**transport, "elevator_wait_s": Decimal("60")},
            cycle_distance_m=published_length,
        )
        self.assertEqual(Decimal(hospital_result["cycle_seconds"]),
                         Decimal(base["cycle_seconds"]) + Decimal("60"))
        self.assertGreaterEqual(hospital_result["fleet"], base["fleet"])
        split = calculate_stationary_fleet(
            hospital,
            {**common, "peak_jobs_per_h": Decimal("10"),
             "observed_speed_m_s": Decimal("1"),
             "pickup_time_s": Decimal("10"), "dropoff_time_s": Decimal("20"),
             "outbound_elevator_wait_s": Decimal("5"),
             "inbound_elevator_wait_s": Decimal("7"),
             "outbound_elevator_ride_s": Decimal("5"),
             "inbound_elevator_ride_s": Decimal("7")},
            cycle_distance_m=published_length,
            outbound_distance_m=published_length / 2,
        )
        self.assertEqual(Decimal(split["handoff_seconds"]), published_length / 2 + 40)
        self.assertEqual(Decimal(split["cycle_seconds"]), published_length + 54)
        cleaning = process_for("airport", "airport_terminal_cleaning")
        clean = calculate_stationary_fleet(cleaning, {
            **common, "peak_area_m2_h": Decimal("1386"),
            "observed_cleaning_rate_m2_h": Decimal("693"),
        })
        self.assertEqual(clean["demand_unit"], "м²/ч")
        self.assertGreaterEqual(Decimal(clean["capacity_per_hour"]), Decimal(clean["peak_demand"]))


class ProjectJourneyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        source_path = os.environ.get("CATALOG_SOURCE_PATH")
        if not source_path:
            raise RuntimeError("CATALOG_SOURCE_PATH обязателен для проверки проекта")
        content = Path(source_path).read_bytes()
        cls.batch, _ = import_catalog(
            content, source_label=Path(source_path).name, source_kind="organizer_v4",
        )
        claims_path = os.environ.get("RESEARCH_CLAIMS_PATH")
        if not claims_path:
            raise RuntimeError("RESEARCH_CLAIMS_PATH обязателен для проверки проекта")
        claims_content = Path(claims_path).read_bytes()
        cls.evidence_batch, _ = import_evidence(
            claims_content, source_label=Path(claims_path).name,
            expected_checksum=hashlib.sha256(claims_content).hexdigest(),
        )
        cls.owner = get_user_model().objects.create_user(username="project-owner")
        cls.other = get_user_model().objects.create_user(username="project-other")

    def setUp(self):
        self.client.force_login(self.owner)

    def test_each_object_starts_without_assumed_measurements(self):
        for slug, title in Project.OBJECT_TYPES:
            with self.subTest(slug=slug):
                response = self.client.post(
                    reverse("project_create", args=[slug]), {"name": title},
                )
                self.assertEqual(response.status_code, 302)
                project = Project.objects.get(owner=self.owner, object_slug=slug)
                revision = project.revisions.get(number=1)
                snapshot = revision.scenario_snapshot
                self.assertEqual(snapshot["catalog_checksum"], self.batch.checksum)
                self.assertEqual(snapshot["evidence_checksum"], self.evidence_batch.checksum)
                self.assertEqual(snapshot["input_profile"]["fields"], [])
                self.assertEqual(profile_for_revision(revision)["fields"], [])
                detail = self.client.get(response["Location"])
                self.assertEqual(detail.status_code, 200)
                self.assertContains(detail, f"batch={self.batch.checksum}&amp;evidence={self.evidence_batch.checksum}")
                self.assertRedirects(
                    self.client.get(reverse("project_inputs", args=[project.id])),
                    reverse("project_import", args=[project.id]),
                )

    def test_import_requires_attestation_and_owner(self):
        response = self.client.post(
            reverse("project_create", args=["warehouse"]), {"name": "Склад"},
        )
        project = Project.objects.get(owner=self.owner)
        url = reverse("project_import", args=[project.id])
        upload = SimpleUploadedFile("catalog_export_v4.csv", Path(os.environ["CATALOG_SOURCE_PATH"]).read_bytes())
        rejected = self.client.post(url, {"action": "preview", "file": upload})
        self.assertEqual(rejected.status_code, 400)
        self.assertContains(rejected, "Подтвердите происхождение", status_code=400)
        self.assertEqual(project.revisions.count(), 1)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.get(response["Location"]).status_code, 404)

    def test_saved_revision_is_immutable_and_delete_is_owner_only(self):
        self.client.post(reverse("project_create", args=["warehouse"]), {"name": "Склад"})
        project = Project.objects.get(owner=self.owner)
        revision = project.revisions.get(number=1)
        with self.assertRaises(ValidationError):
            revision.save()
        delete_url = reverse("project_delete", args=[project.id])
        self.assertEqual(self.client.get(delete_url).status_code, 405)
        self.client.force_login(self.other)
        self.assertEqual(self.client.post(delete_url).status_code, 404)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.post(delete_url).status_code, 302)
        self.assertFalse(Project.objects.filter(pk=project.pk).exists())

    def test_catalog_csv_cannot_be_misread_as_object_profile(self):
        self.client.post(reverse("project_create", args=["airport"]), {"name": "Аэропорт"})
        project = Project.objects.get(owner=self.owner)
        upload = SimpleUploadedFile("catalog_export_v4.csv", Path(os.environ["CATALOG_SOURCE_PATH"]).read_bytes())
        response = self.client.post(
            reverse("project_import", args=[project.id]),
            {"action": "preview", "file": upload, "source_attested": "yes"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "CSV должен иметь колонки", status_code=400)
        self.assertEqual(project.revisions.count(), 1)

    def test_import_rejects_unknown_action_and_commit_without_preview(self):
        self.client.post(reverse("project_create", args=["hospital"]), {"name": "Медицинское учреждение"})
        project = Project.objects.get(owner=self.owner)
        url = reverse("project_import", args=[project.id])
        unknown = self.client.post(url, {"action": "unexpected"})
        self.assertContains(unknown, "Неизвестное действие", status_code=400)
        empty_commit = self.client.post(url, {"action": "commit"})
        self.assertContains(empty_commit, "Предпросмотр устарел", status_code=400)
        self.assertEqual(project.revisions.count(), 1)

    def test_published_observation_survives_import_as_attributed_case_data(self):
        claims_path = os.environ.get("RESEARCH_CLAIMS_PATH")
        if not claims_path:
            raise RuntimeError("RESEARCH_CLAIMS_PATH обязателен для проверки реального импорта")
        claims = json.loads(Path(claims_path).read_text(encoding="utf-8"))
        claim = next(item for item in claims if item["id"] == "r2b_airport_test_area")
        self.assertEqual(claim["catalog_sha256"], self.batch.checksum)
        self.assertEqual(claim["use"], "case_context")
        self.client.post(
            reverse("project_create", args=[claim["object"]]),
            {"name": claim["source_id"]},
        )
        project = Project.objects.get(owner=self.owner)
        stream = io.StringIO(newline="")
        writer = csv.writer(stream, delimiter=";")
        writer.writerow(("object_slug", "label", "unit", "value", "min", "max", "source"))
        writer.writerow((
            claim["object"], claim["attribute"], claim["unit"],
            claim["value"], "", "", f"{claim['source_url']}#sha256={claim['source_sha256']}",
        ))
        upload = SimpleUploadedFile("source-case.csv", stream.getvalue().encode("utf-8"))
        url = reverse("project_import", args=[project.id])
        preview = self.client.post(
            url, {"action": "preview", "source_attested": "yes", "file": upload},
        )
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(project.revisions.count(), 1)
        saved = self.client.post(url, {"action": "commit"})
        self.assertEqual(saved.status_code, 302)
        revision = project.revisions.get(number=2)
        self.assertEqual(revision.scenario_snapshot["catalog_checksum"], self.batch.checksum)
        self.assertEqual(revision.scenario_snapshot["evidence_checksum"], self.evidence_batch.checksum)
        profile = profile_for_revision(revision)
        field = profile["fields"][0]
        self.assertEqual(field["value"], claim["value"])
        self.assertEqual(field["unit"], claim["unit"])
        self.assertIn(claim["source_sha256"], field["source"])
        self.assertEqual(profile["attested_by_user_id"], self.owner.pk)
        self.assertEqual(profile["source_filename"], "source-case.csv")
        self.assertEqual(self.client.get(saved["Location"]).status_code, 200)

    def test_task_routes_cover_all_three_objects_without_assumed_values(self):
        for slug, process_code in (
            ("warehouse", "warehouse_pallet_transfer"),
            ("airport", "airport_baggage_transport"),
            ("airport", "airport_terminal_cleaning"),
            ("hospital", "hospital_meal_delivery"),
            ("hospital", "hospital_floor_cleaning"),
        ):
            with self.subTest(process=process_code):
                self.client.post(reverse("project_create", args=[slug]), {"name": process_code})
                project = Project.objects.get(owner=self.owner, name=process_code)
                url = reverse("project_task", args=[project.id])
                response = self.client.get(url, {"process": process_code})
                self.assertContains(response, process_for(slug, process_code).title)
                self.assertNotContains(response, "value=\"1500\"")
                invalid = self.client.post(url, {
                    "process": process_code, "base_revision": "1",
                    process_for(slug, process_code).fields[0].key: "0",
                })
                self.assertEqual(invalid.status_code, 400)
                self.assertEqual(project.revisions.count(), 1)
                saved = self.client.post(url, {"process": process_code, "base_revision": "1"})
                self.assertEqual(saved.status_code, 302)
                revision = project.revisions.get(number=2)
                self.assertEqual(revision.scenario_snapshot["task_profile"]["process"], process_code)
                self.assertEqual(revision.scenario_snapshot["task_profile"]["parameters"], {})
                self.assertEqual(revision.scenario_snapshot["catalog_checksum"], self.batch.checksum)
                self.assertEqual(revision.scenario_snapshot["evidence_checksum"], self.evidence_batch.checksum)
                self.assertEqual(self.client.get(saved["Location"]).status_code, 200)
                detail = self.client.get(reverse("project_detail", args=[project.id]))
                self.assertContains(detail, reverse("project_simulation", args=[project.id]))
                self.assertContains(detail, "Финансовое сравнение")
                self.assertNotContains(detail, reverse("project_report_bundle", args=[project.id]))
                stale = self.client.post(url, {"process": process_code, "base_revision": "1"})
                self.assertEqual(stale.status_code, 409)
                self.assertEqual(project.revisions.count(), 2)
                self.client.force_login(self.other)
                self.assertEqual(self.client.get(url).status_code, 404)
                self.assertEqual(self.client.post(url, {"process": process_code}).status_code, 404)
                self.client.force_login(self.owner)

    def test_unpublished_airport_candidate_keeps_evidence_without_storefront_link(self):
        self.client.post(reverse("project_create", args=["airport"]), {"name": "Аэропорт"})
        project = Project.objects.get(owner=self.owner, name="Аэропорт")
        self.client.post(reverse("project_task", args=[project.id]), {
            "process": "airport_baggage_transport", "base_revision": "1",
        })
        candidate = match_candidates(
            self.evidence_batch,
            process_for("airport", "airport_baggage_transport"), None,
        )[0]
        self.assertEqual(candidate["status"], "requires_verification")
        family_url = reverse("catalog_family_detail", args=[candidate["family"].pk])
        self.assertEqual(self.client.get(family_url).status_code, 404)
        task_page = self.client.get(reverse("project_task", args=[project.id]))
        self.assertContains(task_page, candidate["family"].name)
        self.assertContains(task_page, candidate["application_sources"][0]["source_url"])
        self.assertNotContains(task_page, f'href="{family_url}')

        selected = self.client.post(reverse("project_robot_selection", args=[project.id]), {
            "action": "select", "record_index": candidate["source_row"].record_index,
            "base_revision": "2",
        })
        self.assertEqual(selected.status_code, 302)
        detail = self.client.get(reverse("project_detail", args=[project.id]))
        self.assertContains(detail, candidate["family"].name)
        self.assertNotContains(detail, f'href="{family_url}')

        self.client.post(reverse("project_create", args=["warehouse"]), {"name": "Склад"})
        warehouse = Project.objects.get(owner=self.owner, name="Склад")
        self.client.post(reverse("project_task", args=[warehouse.id]), {
            "process": "warehouse_pallet_transfer", "base_revision": "1",
        })
        verified_family = CatalogSourceRow.objects.get(batch=self.batch, record_index=1).family
        warehouse_page = self.client.get(reverse("project_task", args=[warehouse.id]))
        self.assertContains(warehouse_page, f'href="{reverse("catalog_family_detail", args=[verified_family.pk])}')

    def test_topology_starts_empty_and_is_owner_only_for_all_objects(self):
        for slug, process_code in (("warehouse", "warehouse_pallet_transfer"),
                                   ("airport", "airport_terminal_cleaning"),
                                   ("hospital", "hospital_floor_cleaning")):
            with self.subTest(slug=slug):
                self.client.post(reverse("project_create", args=[slug]), {"name": process_code})
                project = Project.objects.get(owner=self.owner, name=process_code)
                self.client.post(reverse("project_task", args=[project.id]),
                                 {"process": process_code, "base_revision": "1"})
                url = reverse("project_topology", args=[project.id])
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Выберите начало и конец маршрута")
                self.assertNotContains(response, "<svg")
                self.assertNotIn("topology_profile", project.revisions.first().scenario_snapshot)
                self.client.force_login(self.other)
                self.assertEqual(self.client.get(url).status_code, 404)
                self.assertEqual(self.client.post(url, {"action": "add_node"}).status_code, 404)
                self.client.force_login(self.owner)

    def test_published_warehouse_route_keeps_source_and_history(self):
        claims = {item["id"]: item for item in json.loads(
            Path(os.environ["RESEARCH_CLAIMS_PATH"]).read_text(encoding="utf-8"))}
        origin = claims["ronavi_sostra_route_origin"]
        destination = claims["ronavi_sostra_route_destination"]
        length = claims["ronavi_sostra_route_length"]
        self.client.post(reverse("project_create", args=["warehouse"]),
                         {"name": "Кейс Состра"})
        project = Project.objects.get(owner=self.owner, name="Кейс Состра")
        self.client.post(reverse("project_task", args=[project.id]),
                         {"process": "warehouse_pallet_transfer", "base_revision": "1"})
        url = reverse("project_topology", args=[project.id])
        for claim in (origin, destination):
            latest = project.revisions.first()
            response = self.client.post(url, {"action": "add_node", "base_revision": latest.number,
                                              "label": claim["value"], "source": claim["source_url"]})
            self.assertEqual(response.status_code, 302)
        topology = project.revisions.first().scenario_snapshot["topology_profile"]
        start, end = [node["id"] for node in topology["nodes"]]
        base = project.revisions.first().number
        invalid = self.client.post(url, {"action": "add_edge", "base_revision": base,
                                         "start": start, "end": "unknown", "source": length["source_url"]})
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(project.revisions.first().number, base)
        response = self.client.post(url, {"action": "add_edge", "base_revision": base,
                                          "start": start, "end": end, "source": length["source_url"],
                                          "length_m": length["value"], "length_source": length["source_url"],
                                          "bidirectional": "on"})
        self.assertEqual(response.status_code, 302)
        edge_revision = project.revisions.first()
        self.assertEqual(edge_revision.scenario_snapshot["topology_profile"]["edges"][0]["length_m"],
                         str(length["value"]))
        response = self.client.post(url, {"action": "set_route", "base_revision": edge_revision.number,
                                          "origin": start, "destination": end})
        self.assertEqual(response.status_code, 302)
        route_page = self.client.get(response["Location"])
        self.assertContains(route_page, f'{length["value"]} м')
        self.assertContains(route_page, f'Полный транспортный рейс: {2 * length["value"]} м')
        self.assertContains(route_page, "Основание измерения")
        self.assertNotContains(route_page, "<svg")
        current = project.revisions.first()
        edge_id = current.scenario_snapshot["topology_profile"]["edges"][0]["id"]
        invalid_edit = self.client.post(url, {"action": "update_edge", "base_revision": current.number,
                                              "edge_id": edge_id, "start": start, "end": end,
                                              "source": length["source_url"], "length_m": "0",
                                              "length_source": length["source_url"]})
        self.assertEqual(invalid_edit.status_code, 400)
        self.assertEqual(project.revisions.first().number, current.number)
        unmeasured = self.client.post(url, {"action": "update_edge", "base_revision": current.number,
                                            "edge_id": edge_id, "start": start, "end": end,
                                            "source": length["source_url"], "bidirectional": "on"})
        self.assertEqual(unmeasured.status_code, 302)
        self.assertContains(self.client.get(unmeasured["Location"]), "Измерьте длину")
        updated = self.client.post(url, {"action": "update_edge",
                                         "base_revision": project.revisions.first().number,
                                         "edge_id": edge_id, "start": start, "end": end,
                                         "source": length["source_url"],
                                         "length_m": length["value"], "length_source": length["source_url"],
                                         "bidirectional": "on"})
        self.assertEqual(updated.status_code, 302)
        self.assertContains(self.client.get(updated["Location"]), f'{length["value"]} м')
        self.assertEqual(self.client.post(url, {"action": "remove_edge", "base_revision": base,
                                                "edge_id": edge_revision.scenario_snapshot["topology_profile"]["edges"][0]["id"]}).status_code, 409)
        current = project.revisions.first()
        edge_id = current.scenario_snapshot["topology_profile"]["edges"][0]["id"]
        removed = self.client.post(url, {"action": "remove_edge", "base_revision": current.number,
                                         "edge_id": edge_id})
        self.assertEqual(removed.status_code, 302)
        self.assertContains(self.client.get(removed["Location"]), "доступного маршрута нет")
        historical = self.client.get(url, {"revision": current.number})
        self.assertContains(historical, f'{length["value"]} м')
        self.assertNotContains(historical, "Сохранить точку")

    def test_airport_and_hospital_cases_do_not_gain_unpublished_geometry(self):
        claims = {item["id"]: item for item in json.loads(
            Path(os.environ["RESEARCH_CLAIMS_PATH"]).read_text(encoding="utf-8"))}
        airport_claims = (claims["r2b_airport_public_hall_zone"],
                          claims["r2b_airport_registration_exclusion"])
        self.client.post(reverse("project_create", args=["airport"]), {"name": "Кейс аэропорта"})
        project = Project.objects.get(owner=self.owner, name="Кейс аэропорта")
        self.client.post(reverse("project_task", args=[project.id]),
                         {"process": "airport_terminal_cleaning", "base_revision": "1"})
        url = reverse("project_topology", args=[project.id])
        for claim, zone in zip(airport_claims, ("public", "excluded")):
            self.assertEqual(self.client.post(url, {
                "action": "add_node", "base_revision": project.revisions.first().number,
                "label": claim["value"], "source": claim["source_url"], "zone": zone,
            }).status_code, 302)
        topology = project.revisions.first().scenario_snapshot["topology_profile"]
        self.assertTrue(all(node["x_m"] is None and node["y_m"] is None and not node["floor"]
                            for node in topology["nodes"]))
        start, end = [node["id"] for node in topology["nodes"]]
        bad_edge = self.client.post(url, {"action": "add_edge", "base_revision": project.revisions.first().number,
                                          "start": start, "end": end,
                                          "source": airport_claims[1]["source_url"]})
        self.assertEqual(bad_edge.status_code, 400)
        self.assertContains(bad_edge, "Укажите режим доступа", status_code=400)
        self.assertEqual(project.revisions.first().scenario_snapshot["topology_profile"]["edges"], [])
        route = self.client.post(url, {"action": "set_route", "base_revision": project.revisions.first().number,
                                       "origin": start, "destination": end})
        self.assertEqual(route.status_code, 302)
        self.assertContains(self.client.get(route["Location"]), "Точки не соответствуют режиму доступа")

        hospital = claims["waybot_hospital_cleaning_zones"]
        self.client.post(reverse("project_create", args=["hospital"]), {"name": "Кейс больницы"})
        project = Project.objects.get(owner=self.owner, name="Кейс больницы")
        self.client.post(reverse("project_task", args=[project.id]),
                         {"process": "hospital_floor_cleaning", "base_revision": "1"})
        url = reverse("project_topology", args=[project.id])
        for label in hospital["value"][:2]:
            self.assertEqual(self.client.post(url, {
                "action": "add_node", "base_revision": project.revisions.first().number,
                "label": label, "source": hospital["source_url"],
            }).status_code, 302)
        topology = project.revisions.first().scenario_snapshot["topology_profile"]
        self.assertTrue(all(node["zone"] is None and not node["floor"] for node in topology["nodes"]))
        start, end = [node["id"] for node in topology["nodes"]]
        response = self.client.post(url, {
            "action": "set_route", "base_revision": project.revisions.first().number,
            "origin": start, "destination": end, "route_flow": "clean",
        })
        self.assertEqual(response.status_code, 302)
        self.assertContains(self.client.get(response["Location"]), "Укажите этаж")
        self.assertNotContains(self.client.get(response["Location"]), "<svg")

    def test_matching_uses_primary_limits_and_never_promotes_missing_specs(self):
        payloads = {
            claim.claim_id: claim for claim in CatalogEvidenceClaim.objects.filter(
                evidence_batch=self.evidence_batch,
                claim_id__in=("ronavi_h1500_payload", "ronavi_h2000_payload", "ronavi_h2000_passage"),
            )
        }
        self.assertEqual(len(payloads), 3)
        # Contract boundary values are taken verbatim from primary manufacturer pages.
        profile = {"parameters": {
            "cargo_mass_kg": {"value": payloads["ronavi_h2000_payload"].value,
                              "unit": "kg", "source": payloads["ronavi_h2000_payload"].source_url},
            "route_width_mm": {"value": payloads["ronavi_h2000_passage"].value,
                               "unit": "mm", "source": payloads["ronavi_h2000_passage"].source_url},
        }}
        results = match_candidates(
            self.evidence_batch, process_for("warehouse", "warehouse_pallet_transfer"), profile,
        )
        self.assertEqual(len(results), 3)
        by_name = {item["family"].name: item for item in results}
        h1500 = next(item for name, item in by_name.items() if "H1500" in name)
        h2000 = next(item for name, item in by_name.items() if "H2000" in name)
        carrier = by_name["DMR Carrier P"]
        self.assertEqual(h1500["status"], "reject")
        self.assertIn("physical_limit", h1500["reason_codes"])
        self.assertEqual(h2000["status"], "requires_verification")
        self.assertIn("missing_spec", h2000["reason_codes"])
        self.assertEqual(carrier["status"], "reject")
        self.assertIn("physical_limit", carrier["reason_codes"])
        self.assertEqual(carrier["source_row"].record_index, 13)
        self.assertEqual(
            next(check for check in h1500["checks"] if check["code"] == "physical_limit")["source_url"],
            payloads["ronavi_h1500_payload"].source_url,
        )
        airport = match_candidates(
            self.evidence_batch, process_for("airport", "airport_baggage_transport"), None,
        )
        hospital = match_candidates(
            self.evidence_batch, process_for("hospital", "hospital_floor_cleaning"), None,
        )
        self.assertEqual(len(airport), 1)
        self.assertEqual(len(hospital), 2)
        self.assertTrue(all(item["status"] == "requires_verification" for item in airport + hospital))
        airport_cleaning = match_candidates(
            self.evidence_batch, process_for("airport", "airport_terminal_cleaning"), None,
        )
        unit = next(item for item in airport_cleaning if item["source_row"].record_index == 25)
        self.assertEqual(unit["status"], "requires_verification")
        self.assertIn("missing_spec", unit["reason_codes"])
        self.assertEqual(len(airport_cleaning), 2)
        self.assertEqual(match_candidates(
            self.evidence_batch, process_for("hospital", "hospital_meal_delivery"), None,
        ), [])

    def test_warehouse_handoff_rejects_floor_pickup_for_platform_robot(self):
        process = process_for("warehouse", "warehouse_pallet_transfer")
        floor_claim = CatalogEvidenceClaim.objects.get(
            evidence_batch=self.evidence_batch, claim_id="ronavi_h1500_no_floor_pickup",
        )
        platform_claim = CatalogEvidenceClaim.objects.get(
            evidence_batch=self.evidence_batch, claim_id="ronavi_h1500_platform_transport",
        )
        case_claim = CatalogEvidenceClaim.objects.get(
            evidence_batch=self.evidence_batch, claim_id="ronavi_sostra_route_length",
        )

        def result_for(mode, source):
            profile = {"parameters": {"pallet_handoff_mode": {
                "value": mode, "unit": "handoff_mode", "source": source,
                "status": "user_attested",
            }}}
            return next(item for item in match_candidates(self.evidence_batch, process, profile)
                        if item["source_row"].record_index == 1)

        floor = result_for("floor_pickup", floor_claim.source_url)
        self.assertEqual(floor["status"], "reject")
        self.assertIn("incompatible", floor["reason_codes"])
        self.assertEqual(next(check for check in floor["checks"]
                              if check["label"] == "Подхват паллеты с пола")["source_url"],
                         floor_claim.source_url)
        platform = result_for("platform_transfer", case_claim.source_url)
        self.assertEqual(platform["status"], "requires_verification")
        self.assertIn("missing_spec", platform["reason_codes"])
        self.assertEqual(next(check for check in platform["checks"]
                              if check["label"] == "Перевозка паллеты на платформе")["source_url"],
                         platform_claim.source_url)
        unknown = result_for("floor_pickup", "")
        self.assertEqual(unknown["status"], "requires_verification")
        self.assertIn("invalid_input", unknown["reason_codes"])
        legacy = next(item for item in match_candidates(self.evidence_batch, process, {"parameters": {}})
                      if item["source_row"].record_index == 1)
        self.assertIn("missing_input", legacy["reason_codes"])

    def test_mark2se_passage_limit_uses_exact_model_and_a_source_bound_boundary(self):
        limit = CatalogEvidenceClaim.objects.get(
            evidence_batch=self.evidence_batch, claim_id="r2b_mark2se_minimum_passage",
        )
        independent_width = CatalogEvidenceClaim.objects.get(
            evidence_batch=self.evidence_batch, claim_id="waybot_cleanbotics_passage",
        )
        self.assertLess(independent_width.value, limit.value)
        process = process_for("airport", "airport_terminal_cleaning")
        profile = {"parameters": {"route_width_mm": {
            "value": independent_width.value, "unit": "mm",
            "source": independent_width.source_url,
        }}}
        candidate = next(item for item in match_candidates(self.evidence_batch, process, profile)
                         if item["source_row"].record_index == 21)
        self.assertEqual(candidate["status"], "reject")
        passage = next(check for check in candidate["checks"]
                       if check["label"] == "Ширина маршрута")
        self.assertEqual(passage["code"], "physical_limit")
        self.assertEqual(passage["source_url"], limit.source_url)
        self.assertEqual(passage["input_source"], independent_width.source_url)
        unmeasured = next(item for item in match_candidates(self.evidence_batch, process, None)
                          if item["source_row"].record_index == 21)
        self.assertEqual(unmeasured["status"], "requires_verification")

    def test_warehouse_handoff_form_versions_selection_and_rejects_invalid_sources(self):
        self.client.post(reverse("project_create", args=["warehouse"]),
                         {"name": "Маршрут из опубликованного кейса Sostra"})
        project = Project.objects.get(owner=self.owner, object_slug="warehouse")
        task_url = reverse("project_task", args=[project.id])
        base = {"process": "warehouse_pallet_transfer", "base_revision": "1"}
        case_source = CatalogEvidenceClaim.objects.get(
            evidence_batch=self.evidence_batch, claim_id="ronavi_sostra_route_length",
        ).source_url
        self.assertContains(self.client.get(task_url, {"process": base["process"]}),
                            "Как устроены загрузка и снятие паллеты?")
        self.assertContains(self.client.post(task_url, {**base,
            "pallet_handoff_mode": "platform_transfer", "source_attested": "on",
        }), "Укажите план", status_code=400)
        self.assertContains(self.client.post(task_url, {**base,
            "pallet_handoff_mode_source": case_source, "source_attested": "on",
        }), "Выберите способ", status_code=400)
        self.assertContains(self.client.post(task_url, {**base,
            "pallet_handoff_mode": "unexpected", "pallet_handoff_mode_source": case_source,
            "source_attested": "on",
        }), "Выберите корректный вариант", status_code=400)
        self.assertContains(self.client.post(task_url, {**base,
            "pallet_handoff_mode": "platform_transfer", "pallet_handoff_mode_source": case_source,
        }), "Подтвердите происхождение", status_code=400)
        self.assertEqual(project.revisions.count(), 1)
        saved = self.client.post(task_url, {**base,
            "pallet_handoff_mode": "platform_transfer", "pallet_handoff_mode_source": case_source,
            "source_attested": "on",
        })
        self.assertEqual(saved.status_code, 302)
        snapshot = project.revisions.get(number=2).scenario_snapshot
        self.assertEqual(snapshot["task_profile"]["version"], 2)
        self.assertEqual(snapshot["task_profile"]["parameters"]["pallet_handoff_mode"], {
            "value": "platform_transfer", "unit": "handoff_mode", "source": case_source,
            "status": "user_attested",
        })
        candidate = next(item for item in match_candidates(self.evidence_batch,
            process_for("warehouse", "warehouse_pallet_transfer"), snapshot["task_profile"])
            if item["source_row"].record_index == 1)
        self.assertEqual(candidate["status"], "requires_verification")
        selected = self.client.post(reverse("project_robot_selection", args=[project.id]), {
            "action": "select", "base_revision": "2", "record_index": "1",
        })
        self.assertEqual(selected.status_code, 302)
        comparison_source = CatalogEvidenceClaim.objects.get(
            evidence_batch=self.evidence_batch, claim_id="ronavi_h1500_no_floor_pickup",
        ).source_url
        changed = self.client.post(task_url, {
            "process": "warehouse_pallet_transfer", "base_revision": "3",
            "pallet_handoff_mode": "floor_pickup",
            "pallet_handoff_mode_source": comparison_source, "source_attested": "on",
        })
        self.assertEqual(changed.status_code, 302)
        self.assertNotIn("robot_selection", project.revisions.get(number=4).scenario_snapshot)
        self.assertIn("robot_selection", project.revisions.get(number=3).scenario_snapshot)
        self.assertEqual(project.revisions.get(number=4).scenario_snapshot["task_profile"]
                         ["parameters"]["pallet_handoff_mode"]["value"], "floor_pickup")
        self.assertContains(self.client.get(task_url, {"revision": 4}), "Не подходит")
        self.assertEqual(self.client.post(task_url, {**base,
            "pallet_handoff_mode": "platform_transfer", "pallet_handoff_mode_source": case_source,
            "source_attested": "on",
        }).status_code, 409)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(task_url).status_code, 404)
        self.assertEqual(self.client.post(task_url, {**base}).status_code, 404)
        self.client.force_login(self.owner)

    def test_documented_airport_area_is_saved_without_invented_route_width(self):
        published = CatalogEvidenceClaim.objects.get(
            evidence_batch=self.evidence_batch, claim_id="r2b_airport_test_area",
        )
        self.assertEqual(published.object_slug, "airport")
        self.assertGreater(published.value, 0)
        self.assertEqual(published.unit, "m2 per 2h")
        self.client.post(reverse("project_create", args=["airport"]), {
            "name": "Опубликованное испытание R2B",
        })
        project = Project.objects.get(owner=self.owner, name="Опубликованное испытание R2B")
        url = reverse("project_task", args=[project.id])
        form_data = {
            "process": "airport_terminal_cleaning", "base_revision": "1",
            "cleaning_area_m2": str(published.value),
            "cleaning_area_m2_source": published.source_url,
        }
        missing_attestation = self.client.post(url, form_data)
        self.assertContains(missing_attestation, "Подтвердите происхождение", status_code=400)
        self.assertEqual(project.revisions.count(), 1)
        saved = self.client.post(url, {**form_data, "source_attested": "on"})
        self.assertEqual(saved.status_code, 302)
        revision = project.revisions.get(number=2)
        profile = revision.scenario_snapshot["task_profile"]
        self.assertEqual(profile["parameters"]["cleaning_area_m2"]["value"], str(published.value))
        self.assertEqual(profile["parameters"]["cleaning_area_m2"]["unit"], "м²")
        self.assertEqual(profile["parameters"]["cleaning_area_m2"]["source"], published.source_url)
        self.assertNotIn("route_width_mm", profile["parameters"])
        self.assertEqual(profile["attested_by_user_id"], self.owner.pk)
        self.assertEqual(project.revisions.get(number=1).scenario_snapshot.get("task_profile"), None)
        results = match_candidates(self.evidence_batch, process_for("airport", "airport_terminal_cleaning"), profile)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(item["status"] == "requires_verification" for item in results))

    def test_selection_is_pinned_to_exact_catalog_row_for_each_object(self):
        cases = (
            ("warehouse", "warehouse_pallet_transfer"),
            ("airport", "airport_terminal_cleaning"),
            ("hospital", "hospital_floor_cleaning"),
        )
        for slug, process_code in cases:
            with self.subTest(object=slug):
                self.client.post(reverse("project_create", args=[slug]), {"name": f"Выбор · {slug}"})
                project = Project.objects.get(owner=self.owner, name=f"Выбор · {slug}")
                task_url = reverse("project_task", args=[project.id])
                choice_url = reverse("project_robot_selection", args=[project.id])
                self.assertEqual(self.client.post(task_url, {
                    "process": process_code, "base_revision": "1",
                }).status_code, 302)
                matches = match_candidates(
                    self.evidence_batch, process_for(slug, process_code),
                    project.revisions.get(number=2).scenario_snapshot["task_profile"],
                )
                candidate = next(item for item in matches if item["status"] != "reject")
                self.assertContains(self.client.get(task_url), "Добавить для проверки")
                selected = self.client.post(choice_url, {
                    "action": "select", "base_revision": "2",
                    "record_index": str(candidate["source_row"].record_index),
                })
                self.assertEqual(selected.status_code, 302)
                revision = project.revisions.get(number=3)
                selection = revision.scenario_snapshot["robot_selection"]
                self.assertEqual(selection["catalog_checksum"], self.batch.checksum)
                self.assertEqual(selection["evidence_checksum"], self.evidence_batch.checksum)
                self.assertEqual(selection["record_index"], candidate["source_row"].record_index)
                self.assertEqual(selection_ref(selection, revision.scenario_snapshot),
                                 selection["selection_ref"])
                self.assertEqual(selection["external_id"], candidate["source_row"].external_id)
                self.assertEqual(selection["application_sources"], candidate["application_sources"])
                self.assertEqual(selection["status"], "requires_verification")
                self.assertEqual(selection["selected_by_user_id"], self.owner.pk)
                self.assertIsNone(project.revisions.get(number=2).scenario_snapshot.get("robot_selection"))
                self.assertContains(self.client.get(selected["Location"]), "Выбрана в этой ревизии")
                self.assertContains(self.client.get(reverse("project_detail", args=[project.id])),
                                    candidate["family"].name)
                self.assertContains(self.client.get(reverse("project_sizing", args=[project.id])),
                                    "Парк роботов")
                self.assertEqual(self.client.post(choice_url, {
                    "action": "select", "base_revision": "2",
                    "record_index": str(candidate["source_row"].record_index),
                }).status_code, 409)
                self.assertEqual(project.revisions.count(), 3)
                self.client.force_login(self.other)
                self.assertEqual(self.client.post(choice_url, {
                    "action": "remove", "base_revision": "3",
                }).status_code, 404)
                self.client.force_login(self.owner)
                removed = self.client.post(choice_url, {
                    "action": "remove", "base_revision": "3",
                })
                self.assertEqual(removed.status_code, 302)
                self.assertIsNone(project.revisions.get(number=4).scenario_snapshot["robot_selection"])
                self.assertEqual(project.revisions.get(number=3).scenario_snapshot["robot_selection"], selection)

    def test_workload_is_versioned_and_requires_sources_without_bypassing_matching(self):
        self.client.post(reverse("project_create", args=["warehouse"]), {"name": "Проверка парка"})
        project = Project.objects.get(owner=self.owner, name="Проверка парка")
        self.client.post(reverse("project_task", args=[project.id]), {
            "process": "warehouse_pallet_transfer", "base_revision": "1",
        })
        candidate = match_candidates(
            self.evidence_batch, process_for("warehouse", "warehouse_pallet_transfer"), None,
        )[0]
        self.client.post(reverse("project_robot_selection", args=[project.id]), {
            "action": "select", "base_revision": "2",
            "record_index": candidate["source_row"].record_index,
        })
        url = reverse("project_sizing", args=[project.id])
        self.assertContains(self.client.get(url), "Данные для расчёта")
        self.assertEqual(self.client.post(url, {
            "base_revision": "3", "peak_jobs_per_h": "0", "source_attested": "on",
        }).status_code, 400)
        self.assertEqual(project.revisions.count(), 3)
        saved = self.client.post(url, {
            "base_revision": "3", "peak_jobs_per_h": "0",
            "peak_jobs_per_h_source": "unit-test: empty observed interval",
            "source_attested": "on",
        })
        self.assertEqual(saved.status_code, 302)
        revision = project.revisions.get(number=4)
        profile = revision.scenario_snapshot["workload_profile"]
        self.assertEqual(profile["parameters"]["peak_jobs_per_h"]["value"], "0")
        self.assertEqual(profile["attested_by_user_id"], self.owner.pk)
        self.assertNotIn("workload_profile", project.revisions.get(number=3).scenario_snapshot)
        self.assertIsNone(size_project(revision.scenario_snapshot)["fleet"])
        self.assertContains(self.client.get(saved["Location"]), "Подтвердите обязательные характеристики")
        self.assertContains(self.client.get(url, {"revision": 4}), "unit-test: empty observed interval")
        self.assertEqual(self.client.post(url, {
            "base_revision": "3", "peak_jobs_per_h": "0",
            "peak_jobs_per_h_source": "unit-test: empty observed interval",
            "source_attested": "on",
        }).status_code, 409)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(url, {"base_revision": "4"}).status_code, 404)
        self.client.force_login(self.owner)
        removed = self.client.post(reverse("project_robot_selection", args=[project.id]), {
            "action": "remove", "base_revision": "4",
        })
        self.assertEqual(removed.status_code, 302)
        self.assertNotIn("workload_profile", project.revisions.get(number=5).scenario_snapshot)
        self.assertEqual(project.revisions.get(number=4).scenario_snapshot["workload_profile"], profile)

    def test_selection_rejects_non_candidates_and_missing_csrf(self):
        self.client.post(reverse("project_create", args=["warehouse"]), {"name": "Проверка выбора"})
        project = Project.objects.get(owner=self.owner, name="Проверка выбора")
        self.client.post(reverse("project_task", args=[project.id]), {
            "process": "warehouse_pallet_transfer", "base_revision": "1",
        })
        url = reverse("project_robot_selection", args=[project.id])
        unrelated = self.batch.source_rows.exclude(
            record_index__in=[item["source_row"].record_index for item in match_candidates(
                self.evidence_batch, process_for("warehouse", "warehouse_pallet_transfer"), None,
            )],
        ).first()
        self.assertIsNotNone(unrelated)
        self.assertEqual(self.client.post(url, {
            "action": "select", "base_revision": "2", "record_index": unrelated.record_index,
        }).status_code, 400)
        same_family_other_row = self.batch.source_rows.get(record_index=66)
        self.assertEqual(same_family_other_row.family_id,
                         self.batch.source_rows.get(record_index=1).family_id)
        self.assertEqual(self.client.post(url, {
            "action": "select", "base_revision": "2",
            "record_index": same_family_other_row.record_index,
        }).status_code, 400)
        self.assertEqual(self.client.post(url, {
            "action": "select", "base_revision": "2", "record_index": "invalid",
        }).status_code, 400)
        self.assertEqual(self.client.get(url).status_code, 405)
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.owner)
        self.assertEqual(strict.post(url, {"action": "remove", "base_revision": "2"}).status_code, 403)
        self.assertEqual(project.revisions.count(), 2)

    def test_changing_process_clears_selection_without_rewriting_history(self):
        self.client.post(reverse("project_create", args=["airport"]), {"name": "Смена операции"})
        project = Project.objects.get(owner=self.owner, name="Смена операции")
        task_url = reverse("project_task", args=[project.id])
        choice_url = reverse("project_robot_selection", args=[project.id])
        self.client.post(task_url, {"process": "airport_terminal_cleaning", "base_revision": "1"})
        candidate = match_candidates(
            self.evidence_batch, process_for("airport", "airport_terminal_cleaning"), None,
        )[0]
        self.client.post(choice_url, {"action": "select", "base_revision": "2",
                                      "record_index": candidate["source_row"].record_index})
        self.assertIsNotNone(project.revisions.get(number=3).scenario_snapshot["robot_selection"])
        changed = self.client.post(task_url, {"process": "airport_baggage_transport", "base_revision": "3"})
        self.assertEqual(changed.status_code, 302)
        self.assertNotIn("robot_selection", project.revisions.get(number=4).scenario_snapshot)
        self.assertIsNotNone(project.revisions.get(number=3).scenario_snapshot["robot_selection"])
        self.assertContains(self.client.get(task_url, {"revision": 3}), "Выбрана в этой ревизии")

    def test_imported_source_observation_clears_active_robot_choice(self):
        claim = CatalogEvidenceClaim.objects.get(
            evidence_batch=self.evidence_batch, claim_id="r2b_airport_test_area",
        )
        self.client.post(reverse("project_create", args=["airport"]), {
            "name": "Опубликованная зона R2B",
        })
        project = Project.objects.get(owner=self.owner, name="Опубликованная зона R2B")
        self.client.post(reverse("project_task", args=[project.id]), {
            "process": "airport_terminal_cleaning", "base_revision": "1",
        })
        candidate = match_candidates(
            self.evidence_batch, process_for("airport", "airport_terminal_cleaning"), None,
        )[0]
        self.client.post(reverse("project_robot_selection", args=[project.id]), {
            "action": "select", "base_revision": "2",
            "record_index": candidate["source_row"].record_index,
        })
        stream = io.StringIO(newline="")
        writer = csv.writer(stream, delimiter=";")
        writer.writerow(("object_slug", "label", "unit", "value", "min", "max", "source"))
        writer.writerow((claim.object_slug, claim.attribute, claim.unit,
                         claim.value, "", "", claim.source_url))
        upload = SimpleUploadedFile("published-airport-area.csv", stream.getvalue().encode("utf-8"))
        import_url = reverse("project_import", args=[project.id])
        self.assertEqual(self.client.post(import_url, {
            "action": "preview", "source_attested": "yes", "file": upload,
        }).status_code, 200)
        self.assertEqual(self.client.post(import_url, {"action": "commit"}).status_code, 302)
        latest = project.revisions.get(number=4).scenario_snapshot
        self.assertNotIn("robot_selection", latest)
        self.assertEqual(latest["task_profile"]["process"], "airport_terminal_cleaning")
        self.assertIsNotNone(project.revisions.get(number=3).scenario_snapshot["robot_selection"])
