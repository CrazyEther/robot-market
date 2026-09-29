"""Cross-object HTTP regression for saved events, commercial terms and exports.

All values in this test are isolated test oracles, never facility evidence or
data presented as a validated robot investment case.
"""

import io
import json
import hashlib
from datetime import datetime, timedelta
from decimal import Decimal
from zipfile import ZipFile

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from projects.availability import PARSER_VERSION as AVAILABILITY_VERSION, parse_availability
from projects.models import (
    AvailabilityPlan, FinancePlan, OperationLog, Project, ProjectRevision, SimulationRun,
)
from projects.operation_logs import parse_operation_log
from projects.operation_logs import PARSER_VERSION as LOG_VERSION
from projects.sizing import size_project
from projects.test_finance_builder import commercial_input
from projects.test_resource_plan import fixture as resource_fixture


OBJECT_PROCESSES = {
    "warehouse": "warehouse_pallet_transfer",
    "airport": "airport_baggage_transport",
    "hospital": "hospital_meal_delivery",
}


class ThreeObjectSavedJourneyTests(TestCase):
    def make_revision(self, object_slug):
        # Independent source-shaped test data for each real domain adapter.
        # No previous revision or object type is changed to fit the test.
        process_code = OBJECT_PROCESSES[object_slug]
        owner = get_user_model().objects.create_user(username=f"owner-{object_slug}")
        project = Project.objects.create(owner=owner, name="Test-only journey",
                                         object_slug=object_slug)
        snapshot, _ = resource_fixture(object_slug)
        snapshot.update(
            schema_version=8, object_slug=object_slug,
            catalog_checksum="test-only-catalog", evidence_checksum="test-only-evidence",
            task_profile={"process": process_code, "version": 3},
            robot_selection={
                "process": process_code, "record_index": 1,
                "catalog_checksum": "test-only-catalog",
                "evidence_checksum": "test-only-evidence", "status": "fit",
            },
        )
        topology = snapshot["topology_profile"]
        for index, node in enumerate(topology["nodes"]):
            node.update(x_m=str(index * 5), y_m="0",
                        coordinate_source="Test-only position measurement")
        values = {
            "peak_jobs_per_h": ("1", "рейсов/ч"),
            "observed_speed_m_s": ("1", "м/с"),
            "pickup_time_s": ("1", "с"),
            "dropoff_time_s": ("1", "с"),
            "productive_fraction": ("1", "доля 0–1"),
            "reliability_fraction": ("1", "доля 0–1"),
            "battery_work_h": ("10", "ч"), "charge_h": ("1", "ч"),
        }
        if object_slug == "hospital":
            values.update({
                "outbound_elevator_wait_s": ("2", "с"),
                "outbound_elevator_ride_s": ("4", "с"),
                "inbound_elevator_wait_s": ("3", "с"),
                "inbound_elevator_ride_s": ("5", "с"),
            })
        snapshot["workload_profile"] = {
            "process": process_code, "robot_record_index": 1,
            "parameters": {
                field: {"value": number, "unit": unit, "source": "Test-only measurement"}
                for field, (number, unit) in values.items()
            },
        }
        sizing = size_project(snapshot)
        self.assertEqual(sizing["status"], "estimated", sizing["reasons"])
        start = datetime.fromisoformat("2026-09-28T08:00:00+05:00")
        end = start + timedelta(seconds=60)
        raw = ("requested_at,work_units\n"
               f"{start.isoformat()},1\n"
               f"{(start + timedelta(seconds=1)).isoformat()},1\n").encode()
        from projects.task_profiles import process_for
        process = process_for(object_slug, process_code)
        operation_log = OperationLog.objects.create(
            project=project, uploaded_by=owner, process=process.code,
            sha256=hashlib.sha256(raw).hexdigest(),
            source_description="Test-only operations",
            period_start_at=start, period_end_at=end,
            raw_csv=raw, rows=parse_operation_log(raw, process),
            parser_version=LOG_VERSION,
        )
        calendar_raw = (
            "robot_slot,start_at,end_at,state,ready_at_node\n"
            + "".join(
                f"{slot},{start.isoformat()},{end.isoformat()},available,a\n"
                for slot in range(1, sizing["fleet"] + 1)
            )
        ).encode()
        calendar = AvailabilityPlan.objects.create(
            project=project, operation_log=operation_log, uploaded_by=owner,
            process=process.code, robot_record_index=1,
            fleet=sizing["fleet"], sha256=hashlib.sha256(calendar_raw).hexdigest(),
            source_description="Test-only availability", raw_csv=calendar_raw,
            rows=parse_availability(calendar_raw, period_start=start,
                                    period_end=end, fleet=sizing["fleet"],
                                    origin_node="a"),
            parser_version=AVAILABILITY_VERSION,
        )
        snapshot["operation_log_ref"] = {
            "id": str(operation_log.id), "sha256": operation_log.sha256,
            "process": process.code, "parser_version": LOG_VERSION,
            "period_start_at": start.isoformat(), "period_end_at": end.isoformat(),
        }
        snapshot["availability_ref"] = {
            "id": str(calendar.id), "sha256": calendar.sha256,
            "operation_log_id": str(operation_log.id), "process": process.code,
            "robot_record_index": 1, "fleet": sizing["fleet"],
            "parser_version": AVAILABILITY_VERSION, "origin_node": "a",
        }
        revision = ProjectRevision.objects.create(
            project=project, number=1, scenario_snapshot=snapshot,
        )
        return owner, project, revision

    def assert_saved_journey(self, object_slug):
        owner, project, revision = self.make_revision(object_slug)
        self.client.force_login(owner)

        simulation_url = reverse("project_simulation", args=[project.id])
        submitted = self.client.post(simulation_url, {"base_revision": str(revision.number)})
        self.assertEqual(submitted.status_code, 302, submitted.content.decode())
        run = SimulationRun.objects.get(project=project, revision=revision)
        self.assertEqual(run.ledger_version, 3)
        self.assertEqual(run.ledger["arrivals"], 2)
        self.assertGreater(Decimal(run.ledger["resource_wait_seconds"]), 0)

        replay = self.client.get(submitted["Location"])
        self.assertEqual(replay.status_code, 200, replay.content[:250])
        self.assertContains(replay, f'data-facility="{object_slug}"')
        self.assertEqual(replay.context["metrics"]["jobs"]["delivered_jobs"],
                         run.ledger["delivered"])
        self.assertEqual(replay.context["playback"]["resource_reservations"],
                         run.ledger["resource_reservations"])

        builder_url = reverse("project_finance_builder", args=[project.id])
        self.assertEqual(self.client.get(builder_url, {"run": str(run.id)}).status_code, 200)
        submitted_finance = self.client.post(builder_url, {
            **commercial_input(), "run": str(run.id),
        })
        self.assertEqual(submitted_finance.status_code, 302,
                         submitted_finance.content[:500])
        plan = FinancePlan.objects.get(project=project, simulation_run=run)
        saved_finance = self.client.get(submitted_finance["Location"])
        self.assertEqual(saved_finance.status_code, 200)
        self.assertEqual(saved_finance.context["plan"].id, plan.id)

        bundle_url = reverse("project_report_bundle", args=[project.id])
        query = {"run": str(run.id), "plan": str(plan.id)}
        archive_response = self.client.get(bundle_url, query)
        self.assertEqual(archive_response.status_code, 200,
                         archive_response.content[:500])
        with ZipFile(io.BytesIO(archive_response.content)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest["object_type"], object_slug)
            self.assertEqual(manifest["run_id"], str(run.id))
            self.assertEqual(manifest["finance_plan_id"], str(plan.id))
            self.assertEqual(manifest["ledger_version"], 3)
            self.assertEqual(manifest["resource_wait_seconds"],
                             run.ledger["resource_wait_seconds"])
            self.assertEqual(manifest["simulation_metrics"],
                             replay.context["metrics"])
            self.assertEqual(manifest["currency"], "RUB")
            self.assertTrue(archive.read("report.pdf").startswith(b"%PDF-"))
            self.assertIn(b"<svg", archive.read("frame.svg"))
            self.assertIn("monthly_totals.csv", archive.namelist())
            self.assertIn("resource_reservations.csv", archive.namelist())
            stable_manifest = archive.read("manifest.json")
        with ZipFile(io.BytesIO(self.client.get(bundle_url, query).content)) as archive:
            self.assertEqual(archive.read("manifest.json"), stable_manifest)

        another_user = get_user_model().objects.create_user(username="other-three-object-user")
        self.client.force_login(another_user)
        self.assertEqual(self.client.get(simulation_url,
                                         {"revision": revision.number, "run": str(run.id)}).status_code, 404)
        self.assertEqual(self.client.get(builder_url, {"run": str(run.id)}).status_code, 404)
        self.assertEqual(self.client.get(bundle_url, query).status_code, 404)

    def test_warehouse_saved_run_finance_and_export(self):
        self.assert_saved_journey("warehouse")

    def test_airport_saved_run_finance_and_export(self):
        self.assert_saved_journey("airport")

    def test_hospital_saved_run_finance_and_export(self):
        self.assert_saved_journey("hospital")
