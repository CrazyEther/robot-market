"""Independent arithmetic and malformed-source checks for the finance contract."""

import csv
import hashlib
import io
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from projects.finance import (
    HEADER, FINANCE_VERSION, FinanceInputError, calculate_finance,
    finance_metadata_sha256, parse_finance_csv, validate_forecast_anchor,
    verified_finance_result,
)
from projects.availability import (
    PARSER_VERSION as AVAILABILITY_VERSION, available_windows, parse_availability,
)
from projects.event_ledger import LEDGER_VERSION, schedule_observed_jobs
from projects.models import (
    AvailabilityPlan, FinancePlan, FinanceVariant, OperationLog, Project, ProjectRevision,
    SimulationRun,
)
from projects.operation_logs import PARSER_VERSION as LOG_VERSION, parse_operation_log
from projects.sizing import size_project
from projects.task_profiles import process_for


def _contract_rows():
    """Small arithmetic oracle repeated over the required sixty months."""
    rows = []
    for month in range(61):
        for scenario, scope, amount, direction in (
            ("baseline", "current_process", "100" if month else "0", "outflow"),
            ("purchase", "robot_equipment" if month == 0 else "operations",
             "1000" if month == 0 else "40", "outflow"),
            ("raas", "raas_subscription", "0" if month == 0 else "70", "outflow"),
        ):
            rows.append({
                "scenario": scenario, "month": str(month), "scope": scope,
                "direction": direction,
                "cash_class": "capex" if scope == "robot_equipment" else "opex",
                "amount": amount, "currency": "RUB",
                "vat_mode": "gross", "source_date": "2026-09-25",
                "source_ref": "test-arithmetic-oracle", "served_work_units": "0" if month == 0 else "1",
                "volume_source_ref": "test-volume-oracle",
                "included_scopes": "maintenance" if scenario == "raas" else "",
            })
    rows.append({
        **rows[-2], "scenario": "purchase", "scope": "residual_value",
        "direction": "inflow", "cash_class": "other_cash",
        "amount": "200", "month": "60",
    })
    return rows


def _csv(rows):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=HEADER, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


class FinanceContractTests(SimpleTestCase):
    def test_full_horizon_has_one_volume_and_independent_cash_oracle(self):
        rows = parse_finance_csv(_csv(_contract_rows()), horizon_months=60)
        result = calculate_finance(rows, horizon_months=60, monthly_discount_rate="0")
        self.assertEqual(result["scenarios"]["baseline"]["tco"], "6000")
        purchase = result["scenarios"]["purchase"]
        self.assertEqual(purchase["tco"], "3200")
        self.assertEqual((purchase["capex"], purchase["opex"], purchase["other_cash"]),
                         ("1000", "2400", "-200"))
        self.assertEqual(purchase["net_effect_vs_baseline"], "2800")
        self.assertEqual(purchase["npv_vs_baseline"], "2800")
        self.assertEqual(purchase["roi_pct"], "280.0")
        self.assertEqual(purchase["payback_month"], 17)
        raas = result["scenarios"]["raas"]
        self.assertEqual(raas["tco"], "4200")
        self.assertEqual(raas["net_effect_vs_baseline"], "1800")
        self.assertIsNone(raas["roi_pct"])
        self.assertIsNone(raas["payback_month"])
        self.assertEqual(calculate_finance(rows, horizon_months=60,
                                           monthly_discount_rate="0.01")["scenarios"]["purchase"]["npv_vs_baseline"]
                         < purchase["npv_vs_baseline"], True)

    def test_missing_month_or_mismatched_volume_never_means_zero(self):
        rows = _contract_rows()
        with self.assertRaises(FinanceInputError):
            parse_finance_csv(_csv([row for row in rows if not (
                row["scenario"] == "baseline" and row["month"] == "3"
            )]), horizon_months=60)
        altered = deepcopy(rows)
        altered[1]["served_work_units"] = "2"
        with self.assertRaises(FinanceInputError):
            parse_finance_csv(_csv(altered), horizon_months=60)

    def test_currency_vat_and_included_raas_service_are_validated(self):
        rows = _contract_rows()
        for field, value in (("currency", "USD"), ("vat_mode", "net"),
                             ("source_ref", ""), ("amount", "NaN"),
                             ("amount", "1e999999"), ("month", "9" * 2000),
                             ("source_date", "2026-99-99"),
                             ("cash_class", "unknown")):
            altered = deepcopy(rows)
            altered[1][field] = value
            with self.subTest(field=field), self.assertRaises(FinanceInputError):
                parse_finance_csv(_csv(altered), horizon_months=60)
        altered = deepcopy(rows)
        altered.append({**altered[2], "scope": "maintenance", "amount": "10"})
        with self.assertRaises(FinanceInputError):
            parse_finance_csv(_csv(altered), horizon_months=60)
        altered = deepcopy(rows)
        altered[-1]["cash_class"] = "capex"
        with self.assertRaises(FinanceInputError):
            parse_finance_csv(_csv(altered), horizon_months=60)
        altered = deepcopy(rows)
        altered[1]["cash_class"] = "opex"
        with self.assertRaises(FinanceInputError):
            parse_finance_csv(_csv(altered), horizon_months=60)

    def test_zero_upfront_and_unreached_payback_have_explicit_status(self):
        rows = _contract_rows()
        for row in rows:
            if row["scenario"] == "purchase" and row["month"] == "0":
                row["amount"] = "0"
            if row["scenario"] == "purchase" and row["scope"] == "operations":
                row["amount"] = "200"
        result = calculate_finance(parse_finance_csv(_csv(rows), horizon_months=60),
                                   horizon_months=60, monthly_discount_rate="0")
        purchase = result["scenarios"]["purchase"]
        self.assertIsNone(purchase["roi_pct"])
        self.assertIsNone(purchase["payback_month"])
        with self.assertRaises(FinanceInputError):
            calculate_finance(parse_finance_csv(_csv(rows), horizon_months=60),
                              horizon_months=60, monthly_discount_rate="NaN")

    def test_positive_forecast_requires_observed_delivery(self):
        rows = parse_finance_csv(_csv(_contract_rows()), horizon_months=60)
        with self.assertRaises(FinanceInputError):
            validate_forecast_anchor(rows, observed_delivered_work_units="0")
        validate_forecast_anchor(rows, observed_delivered_work_units="1")

    def test_saved_result_rechecks_raw_and_source_metadata(self):
        raw = _csv(_contract_rows())
        rows = parse_finance_csv(raw, horizon_months=60)
        result = calculate_finance(rows, horizon_months=60, monthly_discount_rate="0")
        metadata = {"source_description": "test source", "forecast_basis": "test basis",
                    "discount_rate_source": "test rate", "source_filename": "test-oracle.csv"}
        plan = FinancePlan(
            parser_version=FINANCE_VERSION, raw_csv=raw,
            sha256=hashlib.sha256(raw).hexdigest(),
            metadata_sha256=finance_metadata_sha256(**metadata),
            horizon_months=60, monthly_discount_rate=Decimal("0.00000000"),
            rows=rows, result=result, **metadata,
        )
        self.assertEqual(verified_finance_result(plan), result)
        plan.forecast_basis = "changed"
        with self.assertRaises(FinanceInputError):
            verified_finance_result(plan)


class FinanceAccessTests(TestCase):
    def test_projects_finance_requires_owner_and_a_saved_run(self):
        owner = get_user_model().objects.create_user(username="finance-owner")
        other = get_user_model().objects.create_user(username="finance-other")
        project = Project.objects.create(owner=owner, name="Тест доступа", object_slug="warehouse")
        url = reverse("project_finance", args=[project.id])
        self.assertRedirects(self.client.get(url), f"{reverse('login')}?next={url}")
        self.client.force_login(other)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.get(reverse("project_finance_schema", args=[project.id])).status_code, 404)
        self.client.force_login(owner)
        self.assertRedirects(self.client.get(url), reverse("project_simulation", args=[project.id]),
                             fetch_redirect_response=False)
        schema = self.client.get(reverse("project_finance_schema", args=[project.id]))
        self.assertEqual(schema.status_code, 200)
        self.assertEqual(schema.content.decode("utf-8"), ",".join(HEADER) + "\r\n")


class FinanceSourceJourneyTests(TestCase):
    def test_attested_run_to_raw_plan_to_reopened_result(self):
        owner = get_user_model().objects.create_user(username="finance-journey-owner")
        project = Project.objects.create(owner=owner, name="Расчёт", object_slug="warehouse")
        process = process_for("warehouse", "warehouse_pallet_transfer")
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
            "topology_profile": {
                "object_slug": "warehouse", "process": process.code,
                "origin": "origin", "destination": "destination",
                "nodes": [{"id": "origin", "label": "Начало", "floor": "1"},
                          {"id": "destination", "label": "Конец", "floor": "1"}],
                "edges": [{"id": "route", "start": "origin", "end": "destination",
                           "length_m": "10", "source": "test route source",
                           "length_source": "test route measurement", "bidirectional": True}],
            },
            "workload_profile": {
                "process": process.code, "robot_record_index": 1,
                "parameters": {key: {"value": value, "unit": unit, "source": "test measurement"}
                               for key, (value, unit) in raw_values.items()},
            },
        }
        sizing = size_project(snapshot)
        self.assertEqual(sizing["status"], "estimated")
        start = datetime.fromisoformat("2026-09-25T08:00:00+00:00")
        end = start + timedelta(minutes=10)
        log_raw = b"requested_at,work_units\n2026-09-25T08:00:00+00:00,1\n"
        log_sha = hashlib.sha256(log_raw).hexdigest()
        log = OperationLog.objects.create(
            project=project, uploaded_by=owner, process=process.code,
            sha256=log_sha, source_description="test-only log",
            period_start_at=start, period_end_at=end, raw_csv=log_raw,
            rows=parse_operation_log(log_raw, process), parser_version=LOG_VERSION,
        )
        availability_raw = (
            b"robot_slot,start_at,end_at,state,ready_at_node\n"
            b"1,2026-09-25T08:00:00+00:00,2026-09-25T08:10:00+00:00,available,origin\n"
            b"2,2026-09-25T08:00:00+00:00,2026-09-25T08:10:00+00:00,available,origin\n"
        )
        availability_sha = hashlib.sha256(availability_raw).hexdigest()
        availability_rows = parse_availability(
            availability_raw, period_start=start, period_end=end, fleet=sizing["fleet"],
            origin_node="origin",
        )
        availability = AvailabilityPlan.objects.create(
            project=project, operation_log=log, uploaded_by=owner,
            process=process.code, robot_record_index=1, fleet=sizing["fleet"],
            sha256=availability_sha, source_description="test-only calendar",
            raw_csv=availability_raw, rows=availability_rows,
            parser_version=AVAILABILITY_VERSION,
        )
        snapshot["operation_log_ref"] = {
            "id": str(log.id), "sha256": log.sha256, "process": process.code,
            "parser_version": LOG_VERSION, "period_start_at": start.isoformat(),
            "period_end_at": end.isoformat(),
        }
        snapshot["availability_ref"] = {
            "id": str(availability.id), "sha256": availability.sha256,
            "operation_log_id": str(log.id), "process": process.code,
            "robot_record_index": 1, "fleet": sizing["fleet"],
            "parser_version": AVAILABILITY_VERSION, "origin_node": "origin",
        }
        revision = ProjectRevision.objects.create(project=project, number=1,
                                                   scenario_snapshot=deepcopy(snapshot))
        source = f"{availability.source_description}; SHA-256 {availability_sha}"
        ledger = schedule_observed_jobs(
            [{**row, "service_seconds": sizing["cycle_seconds"],
              "handoff_seconds": sizing["handoff_seconds"]} for row in log.rows],
            available_windows(availability_rows, source=source),
            period_start=start, period_end=end,
        )
        run = SimulationRun.objects.create(
            project=project, revision=revision, operation_log=log,
            availability_plan=availability, created_by=owner,
            ledger_version=LEDGER_VERSION, ledger=ledger,
            input_snapshot={"scenario": snapshot, "sizing": sizing,
                            "operation_log_sha256": log_sha,
                            "availability_sha256": availability_sha,
                            "delivery_semantics": "handoff_after_outbound_and_unloading"},
        )
        self.client.force_login(owner)
        url = reverse("project_finance", args=[project.id])
        raw_plan = _csv(_contract_rows())
        saved = self.client.post(url, {
            "run": str(run.id), "file": SimpleUploadedFile("cashflows.csv", raw_plan),
            "horizon_months": "60", "monthly_discount_rate": "0",
            "discount_rate_source": "test-only rate source",
            "source_description": "test-only arithmetic oracle",
            "forecast_basis": "test-only volume oracle",
            "source_attested": "on",
        })
        self.assertEqual(saved.status_code, 302)
        plan = FinancePlan.objects.get(project=project)
        self.assertEqual(plan.sha256, hashlib.sha256(raw_plan).hexdigest())
        self.assertEqual(plan.source_filename, "cashflows.csv")
        self.assertEqual(plan.result["scenarios"]["purchase"]["tco"], "3200")
        reopened = self.client.get(saved["Location"])
        self.assertContains(reopened, "Плановый чистый эффект")
        self.assertContains(reopened, "CAPEX")
        source_url = reverse("project_finance_source", args=[project.id]) + f"?plan={plan.id}"
        self.assertEqual(self.client.get(source_url).content, raw_plan)
        variant_input = {
            "action": "variant", "run": str(run.id), "plan": str(plan.id),
            "source_row": "3", "amount": "2000", "source_date": "2026-09-25",
            "source_ref": "test-only revised purchase source", "source_attested": "on",
        }
        for invalid in ({"source_row": "99999"}, {"amount": "1000"},
                        {"source_ref": ""}, {"amount": "NaN"}):
            rejected = self.client.post(url, {**variant_input, **invalid})
            self.assertEqual(rejected.status_code, 400)
            self.assertFalse(FinanceVariant.objects.exists())
        variant_response = self.client.post(url, variant_input)
        self.assertEqual(variant_response.status_code, 302)
        variant = FinanceVariant.objects.get(base_plan=plan)
        comparison = self.client.get(variant_response["Location"])
        self.assertContains(comparison, "Исходный план и вариант")
        self.assertContains(comparison, "test-only revised purchase source")
        self.assertEqual(Decimal(variant.result["scenarios"]["purchase"]["tco"]), Decimal("4200"))
        self.assertEqual(Decimal(plan.result["scenarios"]["purchase"]["tco"]), Decimal("3200"))
        self.assertEqual(bytes(plan.raw_csv), raw_plan)
        self.assertEqual(self.client.post(url, variant_input)["Location"], variant_response["Location"])
        other = get_user_model().objects.create_user(username="finance-variant-other")
        self.client.force_login(other)
        self.assertEqual(self.client.get(variant_response["Location"]).status_code, 404)
        self.assertEqual(self.client.get(source_url).status_code, 404)
        self.client.force_login(owner)
        FinanceVariant.objects.filter(pk=variant.pk).update(result={"tampered": True})
        self.assertEqual(self.client.get(variant_response["Location"]).status_code, 409)
        FinancePlan.objects.filter(pk=plan.pk).update(result={"tampered": True})
        self.assertEqual(self.client.get(saved["Location"]).status_code, 409)
