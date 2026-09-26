"""User-selected operations and their physical inputs.

Process names come from SPEC-001 §3. No facility measurements or equipment
performance are supplied by this module; every value comes from the owner.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class TaskField:
    key: str
    label: str
    unit: str


@dataclass(frozen=True)
class ProcessDefinition:
    code: str
    object_slug: str
    title: str
    description: str
    fields: tuple[TaskField, ...]
    required_specs: tuple[str, ...]


PROCESSES = (
    ProcessDefinition(
        "warehouse_pallet_transfer", "warehouse", "Перемещение паллет",
        "Перевозка грузовой единицы между выбранными точками склада.",
        (TaskField("cargo_mass_kg", "Масса паллеты с грузом", "кг"),
         TaskField("route_width_mm", "Самый узкий участок маршрута", "мм")),
        ("payload_kg", "minimum_passage_mm", "pallet_loading_interface"),
    ),
    ProcessDefinition(
        "airport_baggage_transport", "airport", "Перевозка багажа",
        "Перемещение багажной тележки между конкретными режимными зонами.",
        (TaskField("cargo_mass_kg", "Масса тележки с багажом", "кг"),
         TaskField("route_width_mm", "Самый узкий участок маршрута", "мм")),
        ("payload_kg", "minimum_passage_mm", "airside_authorization", "tow_interface"),
    ),
    ProcessDefinition(
        "airport_terminal_cleaning", "airport", "Уборка терминала",
        "Уборка выбранной зоны пассажирского терминала.",
        (TaskField("route_width_mm", "Самый узкий участок маршрута", "мм"),
         TaskField("cleaning_area_m2", "Площадь уборки", "м²")),
        ("minimum_passage_mm", "public_zone_safety", "floor_compatibility"),
    ),
    ProcessDefinition(
        "hospital_meal_delivery", "hospital", "Доставка питания",
        "Перевозка тележки из пищеблока в выбранное отделение.",
        (TaskField("cargo_mass_kg", "Масса тележки с питанием", "кг"),
         TaskField("route_width_mm", "Самый узкий участок маршрута", "мм")),
        ("payload_kg", "minimum_passage_mm", "food_hygiene", "elevator_interface"),
    ),
    ProcessDefinition(
        "hospital_floor_cleaning", "hospital", "Уборка помещений",
        "Уборка выбранной зоны больницы с учётом санитарного режима.",
        (TaskField("route_width_mm", "Самый узкий участок маршрута", "мм"),
         TaskField("cleaning_area_m2", "Площадь уборки", "м²")),
        ("minimum_passage_mm", "hospital_disinfection_protocol", "floor_compatibility"),
    ),
)


PALLET_HANDOFF_MODES = (
    ("platform_transfer", "Перевозка на платформе или транспортировочном столике"),
    ("floor_pickup", "Подхват и снятие паллеты с пола"),
)
PALLET_SPEC_BY_HANDOFF_MODE = {
    "platform_transfer": "pallet_platform_transport",
    "floor_pickup": "pallet_floor_pickup",
}


BY_CODE = {process.code: process for process in PROCESSES}
QUALITATIVE_SPECS = frozenset(
    spec for process in PROCESSES for spec in process.required_specs
    if spec not in {"payload_kg", "minimum_passage_mm"}
) | {"pallet_platform_transport", "pallet_floor_pickup"}


def process_for(object_slug, code):
    process = BY_CODE.get(code)
    return process if process and process.object_slug == object_slug else None


def processes_for(object_slug):
    return tuple(process for process in PROCESSES if process.object_slug == object_slug)
