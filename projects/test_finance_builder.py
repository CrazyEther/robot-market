"""Independent cash and throughput oracles for the commercial input form."""

from decimal import Decimal

from django.test import SimpleTestCase

from projects.finance import FinanceInputError, calculate_finance
from projects.finance_builder import FinanceBuilderForm, build_cashflow, initial_investment


def commercial_input():
    return {
        "horizon_months": "60", "currency": "RUB", "vat_mode": "gross",
        "source_date": "2026-09-28", "monthly_discount_rate": "0",
        "discount_rate_source": "test rate", "periods_per_month": "20",
        "forecast_basis": "test repeated shift", "baseline_source": "test payroll",
        "purchase_source": "test quote", "raas_source": "test subscription",
        "source_attested": "on", "baseline_initial": "0", "baseline_fixed": "100",
        "baseline_per_job": "2", "robot_price": "500", "deployment": "200",
        "working_capital": "100", "purchase_fixed": "40", "purchase_per_job": "1",
        "raas_initial": "10", "raas_per_robot": "30", "raas_extra_fixed": "20",
        "raas_per_job": "0.5",
        "baseline_initial_class": "capex", "raas_initial_class": "capex",
    }


class FinanceBuilderTests(SimpleTestCase):
    def values(self, **changes):
        data = {**commercial_input(), **changes}
        form = FinanceBuilderForm(data)
        self.assertTrue(form.is_valid(), form.errors)
        return form.cleaned_data

    def test_fleet_volume_and_cash_have_independent_oracles(self):
        raw, rows = build_cashflow(self.values(), fleet=2, delivered="3",
                                  period_seconds=28800, run_id="test-only")
        result = calculate_finance(rows, horizon_months=60, monthly_discount_rate="0")
        # 60 delivered trips/month; baseline 220, purchase 100, RaaS 110.
        self.assertEqual(Decimal(result["scenarios"]["baseline"]["tco"]), 13200)
        self.assertEqual(Decimal(result["scenarios"]["purchase"]["tco"]), 7200)
        self.assertEqual(Decimal(result["scenarios"]["raas"]["tco"]), 6610)
        self.assertEqual(initial_investment(rows)["purchase"], 1300)
        self.assertEqual(Decimal(result["scenarios"]["purchase"]["capex"]), 1200)
        self.assertEqual(result["scenarios"]["purchase"]["payback_month"], 11)
        self.assertEqual({Decimal(r["served_work_units"]) for r in rows if r["month"]}, {Decimal(60)})
        self.assertIn(b"test quote", raw)

    def test_less_delivered_work_reduces_all_variable_costs(self):
        _, rows = build_cashflow(self.values(), fleet=2, delivered="1",
                                period_seconds=28800, run_id="test-only")
        costs = {r["scenario"]: Decimal(r["amount"]) for r in rows
                 if r["scope"] == "variable_operations" and r["month"] == 1}
        self.assertEqual(costs, {"baseline": Decimal(40), "purchase": Decimal(20), "raas": Decimal(10)})

    def test_fractional_volume_is_never_rounded_up_and_initial_class_is_explicit(self):
        _, rows = build_cashflow(self.values(periods_per_month="1.5555", baseline_initial="7",
                                             baseline_initial_class="other_cash", raas_initial_class="opex"),
                                fleet=2, delivered="0.001", period_seconds=28800, run_id="test")
        self.assertEqual({Decimal(r["served_work_units"]) for r in rows if r["month"]}, {Decimal("0.0015")})
        initial = {(r["scenario"], r["scope"]): r for r in rows if r["month"] == 0}
        self.assertEqual(initial["baseline", "initial_process"]["cash_class"], "other_cash")
        self.assertEqual(initial["raas", "connection"]["cash_class"], "opex")
        self.assertEqual(initial_investment(rows)["raas"], 10)

    def test_impossible_repeat_calendar_and_zero_delivery_fail(self):
        for changes in ({"delivered": "0"}, {"period_seconds": 28 * 86400}, {"fleet": "1.5"}):
            args = {"fleet": 2, "delivered": "3", "period_seconds": 28800, "run_id": "test"}
            with self.subTest(changes=changes), self.assertRaises(FinanceInputError):
                build_cashflow(self.values(), **{**args, **changes})

    def test_missing_amount_or_source_is_not_zero_and_attestation_required(self):
        for field in ("robot_price", "raas_per_job", "baseline_source", "source_attested", "baseline_initial_class", "raas_initial_class"):
            data = commercial_input()
            data.pop(field)
            self.assertFalse(FinanceBuilderForm(data).is_valid(), field)
        for bad in ("NaN", "Infinity", "-1", "1e99"):
            self.assertFalse(FinanceBuilderForm({**commercial_input(), "robot_price": bad}).is_valid())

    def test_max_horizon_is_valid_and_deterministic(self):
        args = dict(fleet=2, delivered="3", period_seconds=28800, run_id="test")
        first = build_cashflow(self.values(horizon_months="600"), **args)
        self.assertEqual(first, build_cashflow(self.values(horizon_months="600"), **args))
        self.assertEqual(len(first[1]), 4206)
