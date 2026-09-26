"""Source-bound stationary fleet estimate; event simulation verifies the service level."""

from decimal import Decimal, InvalidOperation, ROUND_CEILING

from projects.task_profiles import process_for
from projects.topology import route_result, transport_cycle_route
from projects.selection_refs import selection_ref, workload_matches_selection


SIZING_VERSION = 2

COMMON_FIELDS = (
    ("productive_fraction", "Плановая доля времени на задания", "доля 0–1"),
    ("reliability_fraction", "Техническая готовность без учёта зарядки", "доля 0–1"),
    ("battery_work_h", "Работа от заряда в условиях объекта", "ч"),
    ("charge_h", "Полная зарядка в условиях объекта", "ч"),
)
TRANSPORT_FIELDS = (
    ("peak_jobs_per_h", "Пиковый поток", "рейсов/ч"),
    ("observed_speed_m_s", "Рабочая скорость на маршруте", "м/с"),
    ("pickup_time_s", "Время загрузки в начале рейса", "с"),
    ("dropoff_time_s", "Время передачи груза в конце прямого плеча", "с"),
)
CLEANING_FIELDS = (
    ("peak_area_m2_h", "Пиковая нагрузка", "м²/ч"),
    ("observed_cleaning_rate_m2_h", "Измеренный темп уборки на объекте", "м²/ч"),
)
HOSPITAL_TRANSPORT_FIELDS = (
    ("outbound_elevator_wait_s", "Ожидание лифта на прямом плече", "с"),
    ("outbound_elevator_ride_s", "Поездка лифта на прямом плече", "с"),
    ("inbound_elevator_wait_s", "Ожидание лифта на обратном плече", "с"),
    ("inbound_elevator_ride_s", "Поездка лифта на обратном плече", "с"),
)
NONNEGATIVE_FIELDS = frozenset({
    "peak_jobs_per_h", "peak_area_m2_h", "service_time_s", "elevator_wait_s",
    "pickup_time_s", "dropoff_time_s", "outbound_elevator_wait_s",
    "inbound_elevator_wait_s", "outbound_elevator_ride_s", "inbound_elevator_ride_s",
})


def is_transport(process):
    return any(field.key == "cargo_mass_kg" for field in process.fields)


def sizing_fields(process):
    fields = TRANSPORT_FIELDS if is_transport(process) else CLEANING_FIELDS
    if process.code == "hospital_meal_delivery":
        fields += HOSPITAL_TRANSPORT_FIELDS
    return fields + COMMON_FIELDS


def _source_number(parameter, *, allow_zero=False):
    if not isinstance(parameter, dict) or not parameter.get("source"):
        return None
    try:
        value = Decimal(str(parameter.get("value")))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return value if value.is_finite() and (value >= 0 if allow_zero else value > 0) else None


def _ceil(value):
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def hospital_leg_measurements(route, topology):
    """Keep vertical lift travel separate from measured corridor distance."""
    nodes = {node["id"]: node for node in topology["nodes"]}
    result = {}
    for leg in ("outbound", "inbound"):
        steps = route[leg]["steps"]
        elevator_steps = [step for step in steps
                          if nodes[step["from"]]["floor"] != nodes[step["to"]]["floor"]]
        result[leg] = {
            "elevator_count": len(elevator_steps),
            "ground_distance_m": sum(
                (Decimal(step["length_m"]) for step in steps if step not in elevator_steps),
                Decimal(0),
            ),
        }
    return result


def calculate_stationary_fleet(process, values, *, cycle_distance_m=None, outbound_distance_m=None):
    """Arithmetic core. Inputs are validated measured quantities, not catalog defaults."""
    productive = values["productive_fraction"]
    reliability = values["reliability_fraction"]
    if is_transport(process):
        service_seconds = values.get("service_time_s")
        if service_seconds is None:
            service_seconds = values["pickup_time_s"] + values["dropoff_time_s"]
        lift_seconds = values.get("elevator_wait_s")
        if lift_seconds is None:
            lift_seconds = (values.get("outbound_elevator_wait_s", Decimal(0))
                            + values.get("inbound_elevator_wait_s", Decimal(0))
                            + values.get("outbound_elevator_ride_s", Decimal(0))
                            + values.get("inbound_elevator_ride_s", Decimal(0)))
        cycle_seconds = (cycle_distance_m / values["observed_speed_m_s"]
                         + service_seconds + lift_seconds)
        handoff_seconds = (
            values["pickup_time_s"] + outbound_distance_m / values["observed_speed_m_s"]
            + values.get("outbound_elevator_wait_s", Decimal(0))
            + values.get("outbound_elevator_ride_s", Decimal(0))
            + values["dropoff_time_s"]
            if outbound_distance_m is not None
            and "pickup_time_s" in values and "dropoff_time_s" in values else None
        )
        active = _ceil(values["peak_jobs_per_h"] * cycle_seconds / (Decimal(3600) * productive))
        capacity = Decimal(active) * Decimal(3600) * productive / cycle_seconds
        demand_unit = "рейсов/ч"
        peak_demand = values["peak_jobs_per_h"]
    else:
        cycle_seconds = None
        handoff_seconds = None
        active = _ceil(values["peak_area_m2_h"] / (values["observed_cleaning_rate_m2_h"] * productive))
        capacity = Decimal(active) * values["observed_cleaning_rate_m2_h"] * productive
        demand_unit = "м²/ч"
        peak_demand = values["peak_area_m2_h"]
    charging = _ceil(Decimal(active) * values["charge_h"] / values["battery_work_h"])
    installed = _ceil(Decimal(active + charging) / reliability)
    return {
        "status": "estimated", "active_robots": active, "chargers": charging,
        "charging_robots": charging, "reserve_robots": installed - active - charging,
        "fleet": installed, "capacity_per_hour": str(capacity),
        "cycle_seconds": str(cycle_seconds) if cycle_seconds is not None else None,
        "handoff_seconds": str(handoff_seconds) if handoff_seconds is not None else None,
        "demand_unit": demand_unit, "peak_demand": str(peak_demand),
    }


def manufacturer_speed_issue(observed_speed, claims):
    if not claims:
        return None
    if any(item.get("use") == "blocked" for item in claims):
        return "Паспортные сведения о скорости выбранной модели требуют уточнения."
    limits = {(str(item["value"]), item["unit"]) for item in claims}
    if len(limits) != 1:
        return "Паспортные пределы скорости выбранной модели расходятся."
    reported, unit = next(iter(limits))
    maximum = _source_number({"value": reported, "source": claims[0].get("source_url")})
    if unit != "m/s" or maximum is None:
        return "Уточните единицу паспортной скорости выбранной модели."
    if observed_speed > maximum:
        return "Рабочая скорость превышает паспортный максимум выбранной модели."
    return None


def size_project(snapshot, *, manufacturer_speed_limits=()):
    """Return an estimate only if every exact-revision gate is satisfied."""
    result = {"version": SIZING_VERSION, "status": "needs_input", "reasons": [],
              "active_robots": None, "chargers": None, "reserve_robots": None,
              "fleet": None, "capacity_per_hour": None, "cycle_seconds": None,
              "handoff_seconds": None}
    object_slug = snapshot.get("object_slug")
    task = snapshot.get("task_profile") or {}
    process = process_for(object_slug, task.get("process")) if isinstance(task, dict) else None
    if process is None:
        result["reasons"].append("Сохраните операцию объекта.")
        return result
    selection = snapshot.get("robot_selection") or {}
    ref = selection_ref(selection, snapshot)
    if (not isinstance(selection, dict) or selection.get("process") != process.code
            or ref is None):
        result["reasons"].append("Выберите модель из подбора для этой операции.")
    elif selection.get("status") != "fit":
        result["reasons"].append("Подтвердите обязательные характеристики выбранной модели для этой операции.")
    topology = snapshot.get("topology_profile") or {}
    if not isinstance(topology, dict) or topology.get("process") != process.code:
        result["reasons"].append("Сохраните маршрут выбранной операции.")
        route = None
    elif is_transport(process):
        route = transport_cycle_route(topology)
        if route is None or route["status"] != "measured":
            result["reasons"].append("Измерьте доступный путь к заданию и обратный путь.")
    else:
        route = route_result(topology)
        if route["status"] != "measured":
            result["reasons"].append("Измерьте доступный маршрут зоны уборки.")
    hospital_legs = {}
    if process.code == "hospital_meal_delivery" and route and route["status"] == "measured":
        hospital_legs = hospital_leg_measurements(route, topology)
    profile = snapshot.get("workload_profile") or {}
    if (not isinstance(profile, dict) or profile.get("process") != process.code
            or not workload_matches_selection(profile, selection, snapshot)):
        result["reasons"].append("Сохраните эксплуатационные показатели для выбранной модели.")
        return result
    parameters = profile.get("parameters") or {}
    if not isinstance(parameters, dict):
        parameters = {}
    values = {}
    for key, label, unit in sizing_fields(process):
        parameter = parameters.get(key)
        elevator_leg = next((leg for leg in ("outbound", "inbound")
                             if key.startswith(f"{leg}_elevator_")), None)
        if (elevator_leg and hospital_legs.get(elevator_leg, {}).get("elevator_count") == 0
                and parameter is None):
            values[key] = Decimal(0)
            continue
        value = _source_number(parameter, allow_zero=key in NONNEGATIVE_FIELDS)
        if value is None or parameter.get("unit") != unit:
            result["reasons"].append(f"Укажите «{label}» и источник значения.")
        elif key.endswith("_fraction") and value > 1:
            result["reasons"].append(f"«{label}» должно быть в пределах 0–1.")
        else:
            values[key] = value
    if is_transport(process) and "observed_speed_m_s" in values:
        issue = manufacturer_speed_issue(values["observed_speed_m_s"], manufacturer_speed_limits)
        if issue:
            result["reasons"].append(issue)
    cycle_distance = Decimal(route["distance_m"]) if is_transport(process) and route and route["status"] == "measured" else None
    outbound_distance = Decimal(route["outbound"]["distance_m"]) if cycle_distance is not None else None
    if process.code == "hospital_meal_delivery" and cycle_distance is not None:
        ground_distances = []
        for leg_name, ride_key, wait_key in (
            ("outbound", "outbound_elevator_ride_s", "outbound_elevator_wait_s"),
            ("inbound", "inbound_elevator_ride_s", "inbound_elevator_wait_s"),
        ):
            measurement = hospital_legs[leg_name]
            if measurement["elevator_count"] > 1:
                result["reasons"].append("Для нескольких переходов через лифт нужны измерения каждого перехода.")
            elif measurement["elevator_count"] and values.get(ride_key, Decimal(0)) <= 0:
                result["reasons"].append("Укажите измеренное время поездки лифта на каждом плече.")
            elif not measurement["elevator_count"] and (values.get(ride_key, Decimal(0)) > 0
                                                         or values.get(wait_key, Decimal(0)) > 0):
                result["reasons"].append("В маршруте нет лифта для указанного времени ожидания или поездки.")
            ground_distances.append(measurement["ground_distance_m"])
        outbound_distance = ground_distances[0]
        cycle_distance = sum(ground_distances, Decimal(0))
    if result["reasons"]:
        return result
    result.update(calculate_stationary_fleet(
        process, values,
        cycle_distance_m=cycle_distance,
        outbound_distance_m=outbound_distance,
    ))
    result["route_edges"] = (
        route["outbound"]["edges"] + route["inbound"]["edges"]
        if is_transport(process) else route["edges"]
    )
    result["source_fields"] = {key: parameters[key]["source"] for key in values
                               if key in parameters}
    return result
