"""2D event playback derived only from a saved route and saved event ledger."""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from projects.topology import transport_cycle_route


class PlaybackDataError(ValueError):
    pass


CALENDAR_STATE_LABELS = {
    "available": "Готов к работе",
    "charging": "На зарядке",
    "maintenance": "На обслуживании",
    "downtime": "Простой",
}


def _coordinate(node):
    if not node.get("coordinate_source"):
        return None
    try:
        x, y = Decimal(str(node["x_m"])), Decimal(str(node["y_m"]))
    except (InvalidOperation, TypeError, ValueError, KeyError):
        return None
    return (x, y) if x.is_finite() and y.is_finite() else None


def measured_scene(snapshot):
    """Draw straight schematic links; travel distances stay the measured edge lengths."""
    topology = snapshot.get("topology_profile") or {}
    route = transport_cycle_route(topology)
    if route is None or route["status"] != "measured":
        return {"floors": [], "transitions": [], "missing_coordinates": []}
    route_steps = route["outbound"]["steps"] + route["inbound"]["steps"]
    route_ids = {step["edge_id"] for step in route_steps}
    route_points = {point for step in route_steps for point in (step["from"], step["to"])}
    nodes = {node["id"]: node for node in topology["nodes"]}
    missing = [node["label"] for node in topology["nodes"]
               if node["id"] in route_points and _coordinate(node) is None]
    groups = {}
    for node in topology["nodes"]:
        point = _coordinate(node)
        if point is not None:
            groups.setdefault(node.get("floor") or "Общий уровень", []).append((node, point))
    floors = []
    for floor_label, measured in sorted(groups.items(), key=lambda item: item[0]):
        xs = [point[0] for _, point in measured]
        ys = [point[1] for _, point in measured]
        extent_x, extent_y = max(xs) - min(xs), max(ys) - min(ys)
        scale = min(Decimal(520) / max(extent_x, Decimal(1)),
                    Decimal(320) / max(extent_y, Decimal(1)))
        center_x, center_y = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
        positions = {}
        for node, (x, y) in measured:
            positions[node["id"]] = {
                "id": node["id"], "label": node["label"],
                "x": str((Decimal(300) + (x - center_x) * scale).quantize(Decimal("0.01"))),
                "y": str((Decimal(200) - (y - center_y) * scale).quantize(Decimal("0.01"))),
                "source": node["coordinate_source"],
                "on_route": node["id"] in route_points,
            }
        edges = []
        for edge in topology["edges"]:
            if edge["start"] in positions and edge["end"] in positions:
                edges.append({
                    "id": edge["id"], "start": positions[edge["start"]],
                    "end": positions[edge["end"]],
                    "on_route": edge["id"] in route_ids,
                    "length_m": edge.get("length_m"),
                    "length_source": edge.get("length_source"),
                })
        floors.append({"label": floor_label, "nodes": list(positions.values()),
                       "edges": edges})
    transitions = []
    seen = set()
    for step in route_steps:
        from_node, to_node = nodes[step["from"]], nodes[step["to"]]
        if (from_node.get("floor") or "Общий уровень") != (to_node.get("floor") or "Общий уровень"):
            key = (step["edge_id"], step["from"], step["to"])
            if key not in seen:
                transitions.append({"from_label": from_node["label"],
                                    "to_label": to_node["label"],
                                    "from_floor": from_node.get("floor"),
                                    "to_floor": to_node.get("floor"),
                                    "length_m": step["length_m"],
                                    "length_source": step["length_source"]})
                seen.add(key)
    return {"floors": floors, "transitions": transitions,
            "missing_coordinates": missing}


def movement_timeline(snapshot, sizing, ledger, scene, *, page_start, page_end):
    """Project source-bound cycle phases onto the schematic measured route."""
    unavailable = lambda reason: {"stages": [], "cycles": [], "reason": reason}
    topology = snapshot.get("topology_profile") or {}
    route = transport_cycle_route(topology)
    if route is None or route["status"] != "measured":
        return unavailable("Для движения нужен измеренный маршрут в обе стороны.")
    if scene["missing_coordinates"]:
        return unavailable("Для движения нужны измеренные координаты всех точек маршрута.")
    positions = {node["id"]: {"floor": floor["label"], "x": node["x"], "y": node["y"]}
                 for floor in scene["floors"] for node in floor["nodes"]}
    route_steps = route["outbound"]["steps"] + route["inbound"]["steps"]
    if any(point not in positions for step in route_steps
           for point in (step["from"], step["to"])):
        return unavailable("Для движения нужны измеренные координаты всех точек маршрута.")
    profile = snapshot.get("workload_profile") or {}
    parameters = profile.get("parameters") or {}
    required = ("observed_speed_m_s", "pickup_time_s", "dropoff_time_s")
    if any(not isinstance(parameters.get(key), dict) or not parameters[key].get("source")
           for key in required):
        return unavailable("Для движения нужны подтверждённые скорость и времена операций.")
    try:
        speed = Decimal(str(parameters["observed_speed_m_s"]["value"]))
        pickup = Decimal(str(parameters["pickup_time_s"]["value"]))
        dropoff = Decimal(str(parameters["dropoff_time_s"]["value"]))
        waits = []
        for key in ("outbound_elevator_wait_s", "inbound_elevator_wait_s"):
            parameter = parameters.get(key)
            if parameter is None:
                waits.append(Decimal(0))
            elif isinstance(parameter, dict) and parameter.get("source"):
                waits.append(Decimal(str(parameter["value"])))
            else:
                return unavailable("Для движения нужно подтверждённое время ожидания лифта.")
        rides = []
        for key in ("outbound_elevator_ride_s", "inbound_elevator_ride_s"):
            parameter = parameters.get(key)
            if parameter is None:
                rides.append(Decimal(0))
            elif isinstance(parameter, dict) and parameter.get("source"):
                rides.append(Decimal(str(parameter["value"])))
            else:
                return unavailable("Для движения нужно подтверждённое время поездки лифта.")
        cycle = Decimal(str(sizing["cycle_seconds"]))
        handoff = Decimal(str(sizing["handoff_seconds"]))
        start_bound, end_bound = Decimal(str(page_start)), Decimal(str(page_end))
    except (InvalidOperation, KeyError, TypeError, ValueError):
        return unavailable("Параметры движения сохранены в неверном формате.")
    if (not all(value.is_finite() for value in (speed, pickup, dropoff, *waits, *rides, cycle, handoff))
            or speed <= 0 or min(pickup, dropoff, *waits) < 0
            or min(rides) < 0
            or cycle <= 0 or handoff <= 0 or start_bound > end_bound):
        return unavailable("Параметры движения не согласованы.")
    outbound_steps = route["outbound"]["steps"]
    inbound_steps = route["inbound"]["steps"]
    nodes = {node["id"]: node for node in topology["nodes"]}
    stages = []
    elapsed = Decimal(0)

    def add_stage(kind, start_node, end_node, duration):
        nonlocal elapsed
        stage = {"kind": kind, "start_s": str(elapsed),
                 "end_s": str(elapsed + duration),
                 "from": positions[start_node], "to": positions[end_node]}
        stages.append(stage)
        elapsed += duration

    origin = topology["origin"]
    destination = topology["destination"]
    add_stage("pickup", origin, origin, pickup)
    for leg, wait, ride, label in ((outbound_steps, waits[0], rides[0], "outbound"),
                                   (inbound_steps, waits[1], rides[1], "inbound")):
        elevators = [step for step in leg if nodes[step["from"]].get("floor")
                     != nodes[step["to"]].get("floor")]
        if elevators and any(not isinstance(parameters.get(f"{label}_elevator_{part}_s"), dict)
                             or not parameters[f"{label}_elevator_{part}_s"].get("source")
                             for part in ("wait", "ride")):
            return unavailable("Для движения нужны измеренные ожидание и поездка лифта.")
        if len(elevators) > 1 or (elevators and ride <= 0) or (not elevators and (wait or ride)):
            return unavailable("Время поездки и ожидания не согласовано с переходами через лифт.")
        for step in leg:
            if wait and step is elevators[0]:
                add_stage("elevator_wait", step["from"], step["from"], wait)
            kind = ("elevator" if positions[step["from"]]["floor"]
                    != positions[step["to"]]["floor"] else "travel")
            duration = ride if kind == "elevator" else Decimal(step["length_m"]) / speed
            add_stage(kind, step["from"], step["to"], duration)
        if leg is outbound_steps:
            add_stage("dropoff", destination, destination, dropoff)
    if (elapsed != cycle or Decimal(stages[[stage["kind"] for stage in stages].index("dropoff")]["end_s"])
            != handoff):
        return unavailable("Времена движения расходятся с сохранённым расчётом.")

    starts = [event for event in ledger["events"] if event["type"] == "start"]
    cycles = []
    for event in starts:
        at = Decimal(event["at_s"])
        if at <= end_bound and at + cycle >= start_bound:
            cycles.append({"robot_id": event["robot_id"],
                           "source_row": event["source_row"],
                           "start_s": str(at), "end_s": str(at + cycle)})
    return {"stages": stages, "cycles": cycles, "reason": None}


def state_before(events, offset):
    """Fold job events in a timeline prefix, preserving counts across pages."""
    if not 0 <= offset <= len(events):
        raise PlaybackDataError("Страница событий вне журнала.")
    arrivals = started = delivered = completed = 0
    active = {}
    for event in events[:offset]:
        kind = event["type"]
        if kind == "arrival":
            arrivals += 1
        elif kind == "start":
            started += 1
            active[event["robot_id"]] = event["source_row"]
        elif kind == "handoff":
            delivered += 1
        elif kind == "complete":
            completed += 1
            active.pop(event["robot_id"], None)
        elif kind in {"calendar_state", "period_end"}:
            continue
        else:
            raise PlaybackDataError("Неизвестный тип сохранённого события.")
    if completed > started or started > arrivals:
        raise PlaybackDataError("Нарушена последовательность событий.")
    return {"arrivals": arrivals, "started": started,
            "delivered": delivered, "completed": completed,
            "queued": arrivals - started, "active": active}


def playback_events(ledger, calendar_rows):
    """Interleave attested calendar changes with saved job events for playback.

    Calendar boundaries are presentation events. They do not modify the saved
    operation ledger or its financial measures.
    """
    origin = datetime.fromisoformat(ledger["period_start_utc"]).astimezone(timezone.utc)
    end = datetime.fromisoformat(ledger["period_end_utc"]).astimezone(timezone.utc)

    def seconds(at):
        delta = at - origin
        return (Decimal(delta.days * 86400 + delta.seconds)
                + Decimal(delta.microseconds) / Decimal(1_000_000))

    by_robot = {}
    for row in calendar_rows:
        by_robot.setdefault(row["robot_slot"], []).append(row)
    events = list(ledger["events"])
    for slot, rows in by_robot.items():
        previous_state = None
        for row in sorted(rows, key=lambda item: item["start_at_utc"]):
            state = row["state"]
            if previous_state is not None and state != previous_state:
                at = datetime.fromisoformat(row["start_at_utc"]).astimezone(timezone.utc)
                events.append({
                    "type": "calendar_state", "at_s": str(seconds(at)),
                    "robot_id": f"slot-{slot}", "state": state,
                    "state_label": CALENDAR_STATE_LABELS[state],
                    "source_row": row["source_row"],
                })
            previous_state = state
    events.append({"type": "period_end", "at_s": str(seconds(end))})
    priority = {"calendar_state": 0, "arrival": 1, "handoff": 2,
                "complete": 3, "start": 4, "period_end": 5}
    events.sort(key=lambda event: (Decimal(event["at_s"]),
                                   priority[event["type"]],
                                   event.get("source_row", 0)))
    summary = state_before(events, len(events))
    if any(summary[name] != ledger[name]
           for name in ("arrivals", "started", "delivered", "completed")):
        raise PlaybackDataError("Шкала воспроизведения расходится с сохранённым прогоном.")
    return events


def availability_at_events(rows, events, period_start):
    """Counts at each event timestamp from the attested availability intervals."""
    origin = datetime.fromisoformat(period_start).astimezone(timezone.utc)
    def seconds(value):
        delta = value - origin
        return (Decimal(delta.days * 86400 + delta.seconds)
                + Decimal(delta.microseconds) / Decimal(1_000_000))
    intervals = []
    for row in rows:
        start = datetime.fromisoformat(row["start_at_utc"]).astimezone(timezone.utc)
        end = datetime.fromisoformat(row["end_at_utc"]).astimezone(timezone.utc)
        intervals.append((seconds(start), seconds(end), row["state"]))
    cache = {}
    result = []
    for event in events:
        at = Decimal(event["at_s"])
        if at not in cache:
            counts = {"available": 0, "charging": 0,
                      "maintenance": 0, "downtime": 0}
            for start, end, state in intervals:
                if start <= at < end:
                    counts[state] += 1
            cache[at] = counts
        result.append(cache[at])
    return result
