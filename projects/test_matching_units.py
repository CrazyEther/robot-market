from types import SimpleNamespace

from django.test import SimpleTestCase

from projects.matching import _candidate_checks, _check_requirement
from projects.task_profiles import process_for


class MatchingLocalizedUnitTests(SimpleTestCase):
    def test_measured_task_accepts_units_displayed_by_the_form(self):
        cases = (
            ("payload_kg", "cargo_mass_kg", "кг", "kg", "1500", "500"),
            ("minimum_passage_mm", "route_width_mm", "мм", "mm", "1200", "1800"),
            ("drawbar_pull_n", "required_drawbar_pull_n", "Н", "N", "500", "450"),
        )
        for attribute, input_key, displayed_unit, required_unit, limit, observed in cases:
            with self.subTest(attribute=attribute):
                claim = SimpleNamespace(
                    attribute=attribute, use="matching_limit", value=limit,
                    unit=required_unit, source_url="https://example.test/specification",
                )
                result = _check_requirement(attribute, [claim], {
                    input_key: {
                        "value": observed,
                        "unit": displayed_unit,
                        "source": "Синтетический тест",
                    },
                })
                self.assertEqual(result["code"], "verified")
                self.assertEqual(result["status"], "pass")

    def test_pallet_mode_uses_its_specific_manufacturer_claim(self):
        process = process_for("warehouse", "warehouse_pallet_transfer")
        source = "https://example.test/specification"
        claims = [
            SimpleNamespace(attribute="payload_kg", use="matching_limit",
                            value=1500, unit="kg", source_url=source),
            SimpleNamespace(attribute="minimum_passage_mm", use="matching_limit",
                            value=750, unit="mm", source_url=source),
            SimpleNamespace(attribute="pallet_platform_transport", use="matching_limit",
                            value=True, unit="boolean", source_url=source),
        ]
        parameters = {
            "cargo_mass_kg": {"value": 500, "unit": "кг", "source": "Синтетический тест"},
            "route_width_mm": {"value": 1800, "unit": "мм", "source": "Синтетический тест"},
            "pallet_handoff_mode": {
                "value": "platform_transfer", "unit": "handoff_mode",
                "source": source, "status": "user_attested",
            },
        }

        checks = _candidate_checks(process, claims, parameters)
        self.assertTrue(all(check["status"] == "pass" for check in checks), checks)
        platform_check = next(check for check in checks
                              if check["label"] == "Перевозка паллеты на платформе")
        self.assertEqual(platform_check["source_url"], source)

        without_platform_claim = _candidate_checks(process, claims[:-1], parameters)
        self.assertTrue(any(check["code"] == "missing_spec" for check in without_platform_claim))
        floor_parameters = {
            "pallet_handoff_mode": {
                "value": "floor_pickup", "unit": "handoff_mode",
                "source": source, "status": "user_attested",
            },
        }
        floor_claim = SimpleNamespace(attribute="pallet_floor_pickup", use="matching_limit",
                                      value=False, unit="boolean", source_url=source)
        floor_checks = _candidate_checks(process, [*claims, floor_claim], floor_parameters)
        self.assertTrue(any(check["code"] == "incompatible" for check in floor_checks))