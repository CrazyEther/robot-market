"""Independent synthetic models used only to demonstrate explainable matching."""

import json
from pathlib import Path


CATALOG_PATH = Path(__file__).resolve().parent / "robot_data" / "catalog.json"


def load_demo_catalog():
    with CATALOG_PATH.open(encoding="utf-8") as source:
        return json.load(source)
