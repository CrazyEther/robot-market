from copy import deepcopy


PARAMETER_LABELS = {
    "demand": "Интенсивность перевозок",
    "pallet_mass": "Масса паллеты",
    "shift_hours": "Продолжительность смены",
    "robot_payload": "Грузоподъёмность робота",
    "robot_price": "Стоимость робота",
    "container_mass": "Масса контейнера",
    "access_permission": "Разрешение на доступ",
    "lift_wait": "Ожидание лифта",
}


def snapshot_scenario(scenario):
    """Capture the data visible when a project is created."""
    return {
        "slug": scenario.slug,
        "title": scenario.title,
        "process": scenario.process,
        "unit": scenario.unit,
        "constraint": scenario.constraint,
        "topology": deepcopy(scenario.topology),
        "parameters": deepcopy(scenario.parameters),
        "data_status": scenario.data_status,
    }
