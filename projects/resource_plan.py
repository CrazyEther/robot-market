"""Compile a versioned facility route into strictly sourced DES inputs."""

from decimal import Decimal, InvalidOperation

from projects.event_ledger import EventInputError, _aware
from projects.topology import transport_cycle_route


def _source_value(parameters, key, *, zero_allowed=False):
    entry = parameters.get(key)
    if not isinstance(entry, dict) or not isinstance(entry.get("source"), str) or not entry["source"].strip():
        raise EventInputError(f"Укажите подтверждённое значение {key} и его источник.")
    try:
        value = Decimal(str(entry["value"]))
    except (InvalidOperation, TypeError, ValueError, KeyError) as exc:
        raise EventInputError(f"Неверное значение {key}.") from exc
    if not value.is_finite() or value < 0 or (not zero_allowed and value == 0):
        raise EventInputError(f"Недопустимое значение {key}.")
    return value


def parse_resource_windows(schedule, source):
    """ISO-8601 absolute open/close pairs separated by semicolons.

    A human shift description is evidence but is not executable timing data.
    """
    if not isinstance(schedule, str) or not schedule.strip():
        raise EventInputError("Для ресурса нужны рабочие окна ISO-8601 с часовым поясом.")
    windows = []
    boundaries = []
    for entry in schedule.split(";"):
        pair = entry.strip().split("/")
        if len(pair) != 2:
            raise EventInputError("Окна ресурса запишите как начало/конец в ISO-8601 через ';'.")
        start = _aware(pair[0].strip(), "Начало работы ресурса")
        end = _aware(pair[1].strip(), "Окончание работы ресурса")
        if start >= end:
            raise EventInputError("Начало работы ресурса должно быть раньше конца.")
        boundaries.append((start, end))
        windows.append({"start_at": start.isoformat(),
                        "end_at": end.isoformat(), "source": source})
    boundaries.sort()
    if any(previous[1] > following[0] for previous, following in zip(boundaries, boundaries[1:])):
        raise EventInputError("Рабочие окна одного ресурса пересекаются.")
    return windows


def build_route_resources(snapshot, sizing):
    """Return a deterministic source-grounded plan, or None for legacy v2.

    Only physical resources on the current outbound/return path are included.
    One ID shared across distinct edges has undefined direction orientation,
    so this contract rejects it until an owner-approved mapping is provided.
    """
    topology = snapshot.get("topology_profile") or {}
    if not isinstance(topology, dict):
        raise EventInputError("Схема объекта не сохранена.")
    route = transport_cycle_route(topology)
    if not route or route["status"] != "measured":
        raise EventInputError("Для учёта занятости нужен измеренный маршрут туда и обратно.")
    outbound, inbound = route["outbound"]["steps"], route["inbound"]["steps"]
    used_ids = {step["edge_id"] for step in outbound + inbound}
    edges = {edge["id"]: edge for edge in topology.get("edges", [])}
    attested = [edges[edge_id] for edge_id in sorted(used_ids)
                if edges[edge_id].get("resource_id")]
    if not attested:
        return None
    grouped = {}
    resources = []
    for edge in attested:
        resource_id = edge["resource_id"]
        if resource_id in grouped:
            raise EventInputError(
                f"Ресурс {resource_id} занимает несколько рёбер: требуется подтверждённая общая ориентация."
            )
        grouped[resource_id] = edge["id"]
        fields = ("resource_capacity", "resource_direction_policy",
                  "resource_occupancy_policy", "resource_priority_policy",
                  "resource_source", "resource_schedule")
        if any(edge.get(key) in (None, "") for key in fields):
            raise EventInputError(f"Участок {edge['id']}: неполные ограничения общего ресурса.")
        if not isinstance(edge["resource_capacity"], int) or isinstance(edge["resource_capacity"], bool):
            raise EventInputError(f"Участок {edge['id']}: неверная вместимость.")
        source = edge["resource_source"]
        resources.append({
            "id": resource_id, "capacity": edge["resource_capacity"],
            "direction_policy": edge["resource_direction_policy"],
            "occupancy_policy": edge["resource_occupancy_policy"],
            "priority_policy": edge["resource_priority_policy"],
            "source": source, "windows": parse_resource_windows(edge["resource_schedule"], source),
        })
    parameters = (snapshot.get("workload_profile") or {}).get("parameters") or {}
    if not isinstance(parameters, dict):
        raise EventInputError("Сохранённый источник параметров движения повреждён.")
    speed = _source_value(parameters, "observed_speed_m_s")
    pickup = _source_value(parameters, "pickup_time_s", zero_allowed=True)
    dropoff = _source_value(parameters, "dropoff_time_s", zero_allowed=True)
    origin, destination = topology["origin"], topology["destination"]
    nodes = {node["id"]: node for node in topology["nodes"]}
    phases = [{"kind": "pickup", "from": origin, "to": origin,
               "duration_s": str(pickup)}]
    for leg_name, steps in (("outbound", outbound), ("inbound", inbound)):
        elevators = [step for step in steps
                     if nodes[step["from"]].get("floor") != nodes[step["to"]].get("floor")]
        if len(elevators) > 1:
            raise EventInputError("Для нескольких лифтов нужны измеренные времена каждого перехода.")
        if elevators:
            wait = _source_value(parameters, f"{leg_name}_elevator_wait_s", zero_allowed=True)
            ride = _source_value(parameters, f"{leg_name}_elevator_ride_s")
        else:
            wait = ride = Decimal(0)
            for suffix in ("wait_s", "ride_s"):
                name = f"{leg_name}_elevator_{suffix}"
                if name in parameters and _source_value(parameters, name, zero_allowed=True):
                    raise EventInputError("Времена лифта не согласованы с измеренным маршрутом.")
        for step in steps:
            edge = edges[step["edge_id"]]
            elevator = step in elevators
            if elevator and wait:
                phases.append({"kind": "elevator_wait", "from": step["from"],
                               "to": step["from"], "duration_s": str(wait)})
            stage = {"kind": "elevator" if elevator else "travel",
                     "from": step["from"], "to": step["to"],
                     "duration_s": str(ride if elevator else Decimal(step["length_m"]) / speed)}
            if edge.get("resource_id"):
                stage["resource_id"] = edge["resource_id"]
                stage["direction"] = "forward" if edge["start"] == step["from"] else "reverse"
            phases.append(stage)
        if leg_name == "outbound":
            phases.append({"kind": "dropoff", "from": destination, "to": destination,
                           "duration_s": str(dropoff)})
    elapsed = sum((Decimal(stage["duration_s"]) for stage in phases), Decimal(0))
    handoff = sum((Decimal(stage["duration_s"]) for stage in phases[:
                   1 + next(i for i, stage in enumerate(phases) if stage["kind"] == "dropoff")]),
                  Decimal(0))
    if elapsed != Decimal(str(sizing.get("cycle_seconds"))) or handoff != Decimal(str(sizing.get("handoff_seconds"))):
        raise EventInputError("Фазы движения расходятся с подтверждённым расчётом цикла.")
    return {"phases": phases, "resources": resources,
            "coverage": {"constrained_route_edges": len(attested),
                         "total_route_edges": len(used_ids)}}
