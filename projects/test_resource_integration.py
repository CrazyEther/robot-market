"""A sourced, constrained route reaches saved playback and financial inputs."""

import hashlib
import io
import json
from copy import deepcopy
from datetime import datetime, timedelta
from zipfile import ZipFile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase
from django.test import TestCase
from django.urls import reverse

from projects.models import AvailabilityPlan, FinancePlan, OperationLog, ProjectRevision, SimulationRun
from projects.operation_logs import parse_operation_log
from projects.task_profiles import process_for
from projects.playback import measured_scene, movement_timeline
from projects.resource_ledger import schedule_resource_jobs
from projects.resource_plan import build_route_resources
from projects.simulation_metrics import simulation_metrics
from projects.test_resource_plan import fixture


class ThreeDomainResourcePlaybackTests(SimpleTestCase):
    def test_all_three_objects_replay_measured_constraints_with_real_stages(self):
        start = datetime.fromisoformat("2026-09-28T08:00:00+05:00")
        end = start + timedelta(seconds=60)
        for slug in ("warehouse", "airport", "hospital"):
            with self.subTest(object_type=slug):
                snapshot, sizing = fixture(slug)
                for index, node in enumerate(snapshot["topology_profile"]["nodes"]):
                    node.update(x_m=str(index * 5), y_m="0",
                                coordinate_source="Обмер объекта")
                plan = build_route_resources(snapshot, sizing)
                window = {"start_at": start.isoformat(), "end_at": end.isoformat(),
                          "source": "Журнал смены объекта"}
                robots = [{"id": f"slot-{i}", "windows": [window]} for i in (1, 2)]
                jobs = [{"source_row": i + 2,
                         "requested_at_utc": (start + timedelta(seconds=i)).isoformat(),
                         "work_units": "1", "service_seconds": sizing["cycle_seconds"],
                         "handoff_seconds": sizing["handoff_seconds"]} for i in (0, 1)]
                ledger = schedule_resource_jobs(
                    jobs, robots, plan["phases"], plan["resources"],
                    period_start=start, period_end=end,
                )
                self.assertGreater(float(ledger["resource_wait_seconds"]), 0)
                rows = [{"robot_slot": i, "source_row": i + 1, "state": "available",
                         "start_at_utc": start.isoformat(), "end_at_utc": end.isoformat()}
                        for i in (1, 2)]
                metrics = simulation_metrics(ledger, rows)
                self.assertEqual(metrics["jobs"]["delivered_jobs"], ledger["delivered"])
                self.assertEqual(metrics["resources"]["total_wait_seconds"],
                                 ledger["resource_wait_seconds"])
                scene = measured_scene(snapshot)
                self.assertTrue(all(transition["from_id"] and transition["to_id"]
                                    for transition in scene["transitions"]))
                motion = movement_timeline(
                    snapshot, sizing, ledger, scene, page_start="0", page_end="60",
                )
                self.assertIsNone(motion["reason"])
                self.assertEqual(len(motion["cycles"]), len(ledger["motion_cycles"]))
                self.assertTrue(any(stage["kind"] == "resource_wait" for cycle in motion["cycles"]
                                    for stage in cycle["stages"]))


class ResourceRunJourneyTests(TestCase):
    def make_resource_project(self):
        from projects.test_finance import FinanceSourceJourneyTests
        owner, project, original = FinanceSourceJourneyTests().make_transport_run()
        process = process_for("warehouse", "warehouse_pallet_transfer")
        snapshot = deepcopy(original.revision.scenario_snapshot)
        start = datetime.fromisoformat("2026-09-25T08:00:00+00:00")
        end = start + timedelta(minutes=10)
        edge = snapshot["topology_profile"]["edges"][0]
        edge.update({
            "resource_id": "measured-corridor", "resource_capacity": 1,
            "resource_direction_policy": "alternating",
            "resource_occupancy_policy": "entry_to_exit",
            "resource_priority_policy": "fifo", "resource_source": "План склада, участок 1",
            "resource_schedule": f"{start.isoformat()}/{end.isoformat()}",
        })
        raw = ("requested_at,work_units\n"
               f"{start.isoformat()},1\n"
               f"{(start + timedelta(seconds=1)).isoformat()},1\n").encode()
        log = OperationLog.objects.create(
            project=project, uploaded_by=owner, process=process.code,
            sha256=hashlib.sha256(raw).hexdigest(), source_description="Два задания объекта",
            period_start_at=start, period_end_at=end, raw_csv=raw,
            rows=parse_operation_log(raw, process), parser_version=original.operation_log.parser_version,
        )
        old_calendar = original.availability_plan
        calendar = AvailabilityPlan.objects.create(
            project=project, operation_log=log, uploaded_by=owner, process=process.code,
            robot_record_index=old_calendar.robot_record_index, fleet=old_calendar.fleet,
            sha256=old_calendar.sha256, source_description=old_calendar.source_description,
            raw_csv=old_calendar.raw_csv, rows=deepcopy(old_calendar.rows),
            parser_version=old_calendar.parser_version,
        )
        snapshot["operation_log_ref"].update(id=str(log.id), sha256=log.sha256)
        snapshot["availability_ref"].update(
            id=str(calendar.id), sha256=calendar.sha256,
            operation_log_id=str(log.id),
        )
        revision = ProjectRevision.objects.create(
            project=project, number=2, scenario_snapshot=deepcopy(snapshot),
        )
        return owner, project, original, revision

    def test_sourced_congestion_is_persisted_replayed_and_tamper_rejected(self):
        owner, project, archived, revision = self.make_resource_project()
        self.client.force_login(owner)
        url = reverse("project_simulation", args=[project.id])
        response = self.client.post(url, {"base_revision": str(revision.number)})
        self.assertEqual(response.status_code, 302, response.content.decode())
        run = SimulationRun.objects.get(project=project, revision=revision)
        self.assertEqual(run.ledger_version, 3)
        self.assertEqual(run.ledger["version"], 3)
        self.assertEqual(run.ledger["arrivals"], 2)
        self.assertGreater(float(run.ledger["resource_wait_seconds"]), 0)
        self.assertGreater(len(run.ledger["resource_reservations"]), 0)
        self.assertEqual(run.input_snapshot["resource_plan"]["resources"][0]["capacity"], 1)

        reopened = self.client.get(response["Location"])
        self.assertEqual(reopened.status_code, 200)
        self.assertContains(reopened, "Ожидание общих участков")
        self.assertContains(reopened, "Занятость подтверждённых участков")
        self.assertContains(reopened, "Финансовый план")
        self.assertContains(reopened, "3D просмотр")
        self.assertContains(reopened, 'data-facility="warehouse"')
        self.assertContains(reopened, "projects/scene3d.js")
        self.assertEqual([edge["resource_id"] for floor in reopened.context["scene"]["floors"]
                          for edge in floor["edges"] if edge.get("resource_id")],
                         ["measured-corridor"])
        motion = reopened.context["playback"]["motion"]
        self.assertIsNone(motion["reason"])
        self.assertEqual(len(motion["cycles"]), 2)
        self.assertTrue(any(stage["kind"] == "resource_wait"
                            for cycle in motion["cycles"] for stage in cycle["stages"]))
        self.assertEqual(reopened.context["metrics"]["resources"]["total_wait_seconds"],
                         run.ledger["resource_wait_seconds"])
        self.assertEqual(reopened.context["playback"]["resource_reservations"],
                         run.ledger["resource_reservations"])
        self.assertEqual(self.client.get(reverse("project_finance", args=[project.id]),
                                         {"run": str(run.id)}).status_code, 200)
        from projects.test_finance import _contract_rows, _csv
        forecast = _contract_rows()
        for row in forecast:
            if row["month"] != "0":
                row["served_work_units"] = str(run.ledger["delivered_work_units"])
                row["volume_source_ref"] = "Прогноз предприятия на основе прогона"
        finance_response = self.client.post(reverse("project_finance", args=[project.id]), {
            "run": str(run.id), "file": SimpleUploadedFile("plan.csv", _csv(forecast)),
            "horizon_months": "60", "monthly_discount_rate": "0",
            "discount_rate_source": "Тестовое решение предприятия",
            "source_description": "Тестовые коммерческие предложения",
            "forecast_basis": "Утверждённая повторяемость наблюдённого периода",
            "source_attested": "on",
        })
        self.assertEqual(finance_response.status_code, 302, finance_response.content.decode())
        finance = FinancePlan.objects.get(project=project, simulation_run=run)
        report_url = reverse("project_report_bundle", args=[project.id])
        index = next(i for i, event in enumerate(run.ledger["events"])
                     if event["type"] == "start" and event["source_row"] == 3)
        query = {"run": str(run.id), "plan": str(finance.id), "event": str(index)}
        bundle = self.client.get(report_url, query)
        self.assertEqual(bundle.status_code, 200, bundle.content[:120])
        with ZipFile(io.BytesIO(bundle.content)) as archive:
            self.assertTrue(archive.read("report.pdf").startswith(b"%PDF-"))
            self.assertIn(b"<svg", archive.read("frame.svg"))
            self.assertIn("resource_reservations.csv", archive.namelist())
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest["ledger_version"], 3)
            self.assertEqual(manifest["resource_wait_seconds"], run.ledger["resource_wait_seconds"])
            self.assertEqual(manifest["simulation_metrics"]["resources"]["reservations"],
                             len(run.ledger["resource_reservations"]))
            self.assertEqual(json.loads(archive.read("motion_cycles.json")),
                             run.ledger["motion_cycles"])
            stable = {name: archive.read(name) for name in (
                "events.csv", "simulation_metrics.csv", "resource_reservations.csv",
                "motion_cycles.json", "frame.svg", "manifest.json",
            )}
        with ZipFile(io.BytesIO(self.client.get(report_url, query).content)) as archive:
            self.assertEqual({name: archive.read(name) for name in stable}, stable)
        archived_url = f"{url}?revision=1&run={archived.id}"
        self.assertEqual(self.client.get(archived_url).status_code, 200)

        stored = deepcopy(run.input_snapshot)
        changed = deepcopy(stored)
        changed["resource_plan"]["resources"][0]["capacity"] = 20
        SimulationRun.objects.filter(pk=run.pk).update(input_snapshot=changed)
        self.assertEqual(self.client.get(response["Location"]).status_code, 409)
        SimulationRun.objects.filter(pk=run.pk).update(input_snapshot=stored)
        changed_ledger = deepcopy(run.ledger)
        changed_ledger["resource_reservations"][0]["end_s"] = "0"
        SimulationRun.objects.filter(pk=run.pk).update(ledger=changed_ledger)
        self.assertEqual(self.client.get(response["Location"]).status_code, 409)
        SimulationRun.objects.filter(pk=run.pk).update(ledger=run.ledger)
        self.assertEqual(self.client.get(response["Location"]).status_code, 200)
