"""Load three independent synthetic demonstration fixtures."""
import json
from pathlib import Path


FIXTURE_DIR = Path(__file__).resolve().parent / "scenario_data"


def load_scenarios():
    scenarios = []
    for slug in ("warehouse", "airport", "hospital"):
        with (FIXTURE_DIR / f"{slug}.json").open(encoding="utf-8") as source:
            item = json.load(source)
        if item["slug"] != slug:
            raise ValueError(f"Fixture slug mismatch: {slug}")
        scenarios.append(item)
    return scenarios


SCENARIOS = load_scenarios()
