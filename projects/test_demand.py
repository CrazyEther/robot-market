"""Demand revision integration: immutable source -> fleet -> events."""

import hashlib
import io
import json
from copy import deepcopy
from datetime import datetime, timedelta
from xml.etree import ElementTree
from zipfile import ZipFile

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from projects.availability import (
    PARSER_VERSION as AVAILABILITY_VERSION, available_windows, parse_availability,
)
from projects.demand import minimum_observed_peak
from projects.event_ledger import LEDGER_VERSION, schedule_observed_jobs
from projects.models import (
    AvailabilityPlan, FinancePlan, OperationLog, Project, ProjectRevision, SimulationRun,
)
from projects.operation_logs import PARSER_VERSION as LOG_VERSION, parse_operation_log
from projects.sizing import size_project
from projects.task_profiles import process_for
from projects.test_finance import _contract_rows, _csv


class DemandJourneyTests(TestCase):
    def test_new_source_creates_revision_and_recalculates_events_without_reusing_calendar(self):
        owner = get_user_model().objects.create_user(username="demand-owner")
        other = get_user_model().objects.create_user(username="demand-other")
        project = Project.objects.create(owner=owner, name="Нагрузка", object_slug="warehouse")
        process = process_for("warehouse", "warehouse_pallet_transfer")
        start = datetime.fromisoformat("2026-09-25T08:00:00+00:00")
        end = start + timedelta(minutes=10)
        topology = {
            "object_slug": "warehouse", "process": process.code,
            "origin": "origin", "destination": "destination",
            "nodes": [{"id": "origin", "label": "Начало", "floor": "1"},
                      {"id": "destination", "label": "Конец", "floor": "1"}],
            "edges": [{"id": "route", "start": "origin", "end": "destination",
                       "length_m": "10", "source": "test route source",
                       "length_source": "test route measurement", "bidirectional": True}],
        }
        raw_values = {
            "peak_jobs_per_h": ("1", "рейсов/ч"),
            "observed_speed_m_s": ("1", "м/с"),
            "pickup_time_s": ("0", "с"), "dropoff_time_s": ("0", "с"),
            "productive_fraction": ("1", "доля 0–1"),
            "reliability_fraction": ("1", "доля 0–1"),
            "battery_work_h": ("10", "ч"), "charge_h": ("1", "ч"),
        }
        snapshot = {
            "schema_version": 8, "object_slug": "warehouse",
            "catalog_checksum": "test-catalog", "evidence_checksum": "test-evidence",
            "task_profile": {"process": process.code},
            "robot_selection": {"process": process.code, "record_index": 1,
                                "catalog_checksum": "test-catalog",
                                "evidence_checksum": "test-evidence", "status": "fit"},
            "topology_profile": topology,
            "workload_profile": {
                "process": process.code, "robot_record_index": 1,
                "parameters": {key: {"value": value, "unit": unit, "source": "test measurement"}
                               for key, (value, unit) in raw_values.items()},
            },
        }
        sizing = size_project(snapshot)
        self.assertEqual(sizing["status"], "estimated")
        raw = b"requested_at,work_units\n2026-09-25T08:00:00+00:00,1\n"
        original = OperationLog.objects.create(
            project=project, uploaded_by=owner, process=process.code,
            sha256=hashlib.sha256(raw).hexdigest(), source_description="test observed source",
            period_start_at=start, period_end_at=end, raw_csv=raw,
            rows=parse_operation_log(raw, process), parser_version=LOG_VERSION,
        )
        calendar_raw = (
            b"robot_slot,start_at,end_at,state,ready_at_node\n"
            b"1,2026-09-25T08:00:00+00:00,2026-09-25T08:10:00+00:00,available,origin\n"
            b"2,2026-09-25T08:00:00+00:00,2026-09-25T08:10:00+00:00,available,origin\n"
        )
        calendar_rows = parse_availability(
            calendar_raw, period_start=start, period_end=end,
            fleet=sizing["fleet"], origin_node="origin",
        )
        calendar = AvailabilityPlan.objects.create(
            project=project, operation_log=original, uploaded_by=owner,
            process=process.code, robot_record_index=1, fleet=sizing["fleet"],
            sha256=hashlib.sha256(calendar_raw).hexdigest(),
            source_description="test observed calendar", raw_csv=calendar_raw,
            rows=calendar_rows, parser_version=AVAILABILITY_VERSION,
        )
        snapshot["operation_log_ref"] = {"id": str(original.id), "sha256": original.sha256,
                                         "process": process.code, "parser_version": LOG_VERSION,
                                         "period_start_at": start.isoformat(),
                                         "period_end_at": end.isoformat()}
        snapshot["availability_ref"] = {
            "id": str(calendar.id), "sha256": calendar.sha256,
            "operation_log_id": str(original.id), "process": process.code,
            "robot_record_index": 1, "fleet": sizing["fleet"],
            "parser_version": AVAILABILITY_VERSION, "origin_node": "origin",
        }
        revision = ProjectRevision.objects.create(
            project=project, number=1, scenario_snapshot=deepcopy(snapshot),
        )
        source = f"{calendar.source_description}; SHA-256 {calendar.sha256}"
        ledger = schedule_observed_jobs(
            [{**job, "service_seconds": sizing["cycle_seconds"],
              "handoff_seconds": sizing["handoff_seconds"]} for job in original.rows],
            available_windows(calendar_rows, source=source),
            period_start=start, period_end=end,
        )
        run = SimulationRun.objects.create(
            project=project, revision=revision, operation_log=original,
            availability_plan=calendar, created_by=owner,
            ledger_version=LEDGER_VERSION, ledger=ledger,
            input_snapshot={"scenario": deepcopy(snapshot), "sizing": sizing,
                            "operation_log_sha256": original.sha256,
                            "availability_sha256": calendar.sha256,
                            "delivery_semantics": "handoff_after_outbound_and_unloading"},
        )
        self.client.force_login(owner)
        url = reverse("project_demand_revision", args=[project.id])
        self.assertContains(self.client.get(url, {"run": str(run.id)}), "Сравнить спрос")
        simulation_url = reverse("project_simulation", args=[project.id])
        run_query = {"revision": "1", "run": str(run.id)}
        self.assertEqual(self.client.get(simulation_url, run_query).status_code, 200)
        SimulationRun.objects.filter(pk=run.pk).update(ledger={"tampered": True})
        self.assertEqual(self.client.get(url, {"run": str(run.id)}).status_code, 409)
        self.assertEqual(self.client.get(simulation_url, run_query).status_code, 409)
        changed_ledger = deepcopy(ledger)
        changed_ledger["delivered_work_units"] = str(
            int(changed_ledger["delivered_work_units"]) + 1
        )
        SimulationRun.objects.filter(pk=run.pk).update(ledger=changed_ledger)
        self.assertEqual(self.client.get(simulation_url, run_query).status_code, 409)
        finance_url = reverse("project_finance", args=[project.id])
        self.assertEqual(self.client.get(finance_url, {"run": str(run.id)}).status_code, 409)
        SimulationRun.objects.filter(pk=run.pk).update(ledger=ledger)
        changed_input = deepcopy(run.input_snapshot)
        changed_input["sizing"]["cycle_seconds"] = "1"
        SimulationRun.objects.filter(pk=run.pk).update(input_snapshot=changed_input)
        self.assertEqual(self.client.get(simulation_url, run_query).status_code, 409)
        self.assertEqual(self.client.get(finance_url, {"run": str(run.id)}).status_code, 409)
        SimulationRun.objects.filter(pk=run.pk).update(input_snapshot=run.input_snapshot)
        new_raw = (
            b"requested_at,work_units\n"
            b"2026-09-25T08:00:00+00:00,1\n"
            b"2026-09-25T08:01:00+00:00,1\n"
        )
        self.assertEqual(minimum_observed_peak(parse_operation_log(new_raw, process)), 2)

        def submit(data, payload=new_raw):
            return self.client.post(url, {
                "run": str(run.id), "base_revision": "1",
                "peak_jobs_per_h": "2", "peak_source": "test peak source",
                "source_description": "test revised journal", "source_attested": "on",
                "file": SimpleUploadedFile("jobs.csv", payload), **data,
            })

        self.assertEqual(submit({"peak_jobs_per_h": "1"}).status_code, 400)
        self.assertEqual(submit({}, raw).status_code, 400)
        self.assertEqual(project.revisions.count(), 1)
        self.client.force_login(other)
        self.assertEqual(self.client.get(url, {"run": str(run.id)}).status_code, 404)
        self.client.force_login(owner)
        saved = submit({})
        self.assertEqual(saved.status_code, 302)
        next_revision = project.revisions.get(number=2)
        revised = next_revision.scenario_snapshot
        self.assertEqual(revised["workload_profile"]["parameters"]["peak_jobs_per_h"]["value"], "2")
        self.assertEqual(revised["demand_what_if"]["parent_run_id"], str(run.id))
        self.assertNotIn("availability_ref", revised)
        self.assertEqual(revised["robot_selection"], snapshot["robot_selection"])
        self.assertEqual(revised["topology_profile"], snapshot["topology_profile"])
        self.assertEqual(revision.scenario_snapshot, snapshot)
        self.assertEqual(SimulationRun.objects.count(), 1)
        self.assertContains(self.client.get(saved["Location"]), "Новый парк")
        self.assertEqual(submit({}).status_code, 409)

        available_url = reverse("project_availability", args=[project.id])
        calendar_saved = self.client.post(available_url, {
            "base_revision": "2", "file": SimpleUploadedFile("calendar.csv", calendar_raw),
            "source_description": "test revised calendar", "source_attested": "on",
        })
        self.assertEqual(calendar_saved.status_code, 302)
        self.assertEqual(project.revisions.first().number, 3)
        compared = self.client.post(simulation_url, {"base_revision": "3"})
        self.assertEqual(compared.status_code, 302)
        new_run = SimulationRun.objects.exclude(pk=run.pk).get()
        self.assertEqual(new_run.revision.number, 3)
        self.assertNotEqual(new_run.operation_log_id, run.operation_log_id)
        self.assertNotEqual(new_run.availability_plan_id, run.availability_plan_id)
        self.assertEqual(new_run.ledger["arrivals"], 2)
        self.assertContains(self.client.get(compared["Location"]), "Исходный и новый прогон")
        finance_page = self.client.get(reverse("project_finance", args=[project.id]),
                                       {"run": str(new_run.id)})
        self.assertContains(finance_page, "Денежный план для нового потока")
        self.assertEqual(project.finance_plans.count(), 0)
        for observed_run, served in ((run, "1"), (new_run, "2")):
            source_rows = _contract_rows()
            for row in source_rows:
                if row["month"] != "0":
                    row["served_work_units"] = served
                    row["volume_source_ref"] = f"test-volume-{served}"
            response = self.client.post(finance_url, {
                "run": str(observed_run.id),
                "file": SimpleUploadedFile("financial-plan.csv", _csv(source_rows)),
                "horizon_months": "60", "monthly_discount_rate": "0",
                "discount_rate_source": "test rate source",
                "source_description": "test cash source",
                "forecast_basis": f"test volume source {served}",
                "source_attested": "on",
            })
            self.assertEqual(response.status_code, 302)
        new_plan = project.finance_plans.get(simulation_run=new_run)
        compared_finance = self.client.get(finance_url, {
            "run": str(new_run.id), "plan": str(new_plan.id),
        })
        self.assertContains(compared_finance, "Денежные результаты двух прогонов")
        self.assertEqual(project.finance_plans.get(simulation_run=run).simulation_run_id, run.id)
        report_url = reverse("project_report_bundle", args=[project.id])
        report_query = {"run": str(new_run.id), "plan": str(new_plan.id)}
        bundle = self.client.get(report_url, report_query)
        self.assertEqual(bundle.status_code, 200)
        self.assertEqual(bundle["Content-Type"], "application/zip")
        with ZipFile(io.BytesIO(bundle.content)) as archive:
            self.assertEqual(set(archive.namelist()), {
                "report.pdf", "events.csv", "financial_rows.csv",
                "monthly_totals.csv", "frame.svg", "manifest.json",
            })
            self.assertTrue(archive.read("report.pdf").startswith(b"%PDF-"))
            self.assertEqual(len(archive.read("events.csv").decode("utf-8-sig").splitlines()),
                             len(new_run.ledger["events"]) + 1)
            self.assertIn("purchase", archive.read("monthly_totals.csv").decode("utf-8-sig"))
            self.assertEqual(ElementTree.fromstring(archive.read("frame.svg")).tag,
                             "{http://www.w3.org/2000/svg}svg")
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest["run_id"], str(new_run.id))
            self.assertEqual(manifest["finance_plan_id"], str(new_plan.id))
            self.assertEqual(manifest["operation_log_sha256"], new_run.operation_log.sha256)
            self.assertEqual(manifest["frame_at_s"], new_run.ledger["events"][manifest["event_index"]]["at_s"])
            stable_files = {name: archive.read(name) for name in (
                "events.csv", "financial_rows.csv", "monthly_totals.csv", "frame.svg", "manifest.json",
            )}
        reopened = self.client.get(report_url, report_query)
        self.assertEqual(reopened.status_code, 200)
        with ZipFile(io.BytesIO(reopened.content)) as archive:
            self.assertEqual({name: archive.read(name) for name in stable_files}, stable_files)
        self.assertEqual(self.client.get(report_url, {**report_query, "event": "99999"}).status_code, 404)
        self.client.force_login(other)
        self.assertEqual(self.client.get(report_url, report_query).status_code, 404)
        self.client.force_login(owner)
        FinancePlan.objects.filter(pk=new_plan.pk).update(result={"tampered": True})
        self.assertEqual(self.client.get(report_url, report_query).status_code, 409)
        FinancePlan.objects.filter(pk=new_plan.pk).update(result=new_plan.result)
        mismatched_rows = _contract_rows()
        for row in mismatched_rows:
            row["currency"] = "USD"
            if row["month"] != "0":
                row["served_work_units"] = "2"
                row["volume_source_ref"] = "test-volume-2"
        mismatched_upload = self.client.post(finance_url, {
            "run": str(new_run.id),
            "file": SimpleUploadedFile("financial-plan-usd.csv", _csv(mismatched_rows)),
            "horizon_months": "60", "monthly_discount_rate": "0",
            "discount_rate_source": "test rate source",
            "source_description": "test alternate cash source",
            "forecast_basis": "test volume source 2",
            "source_attested": "on",
        })
        self.assertEqual(mismatched_upload.status_code, 302)
        mismatched_page = self.client.get(mismatched_upload["Location"])
        self.assertContains(mismatched_page, "одинаковые валюта, НДС, горизонт и ставка")
        self.assertNotContains(mismatched_page, "Денежные результаты двух прогонов")
        edited_workload = {"base_revision": "3", "source_attested": "on"}
        for key, (value, _) in raw_values.items():
            edited_workload[key] = "3" if key == "peak_jobs_per_h" else value
            edited_workload[f"{key}_source"] = "test revised measurement"
        changed_again = self.client.post(
            reverse("project_sizing", args=[project.id]), edited_workload,
        )
        self.assertEqual(changed_again.status_code, 302)
        self.assertNotIn("demand_what_if", project.revisions.first().scenario_snapshot)
        self.assertIn("demand_what_if", next_revision.scenario_snapshot)
