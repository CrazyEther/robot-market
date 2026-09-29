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
RESOURCE_TIMELINE_VERSION = 1


def _coordinate(node):
    if not node.get("coordinate_source"):
        return None
    try:
        x, y = Decimal(str(node["x_m"])), Decimal(str(node["y_m"]))
    except (InvalidOperation, TypeError, ValueError, KeyError):
        return None
    return (x, y) if x.is_finite() and y.is_finite() else None


def measured_scene(snapshot):
    """Draw verified edge polylines when present; travel time stays measured."""
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
    from projects.topology import floor_drawings
    # The editor, 2D/3D playback, and exported frames must use the same
    # projection even when no image is attached to the measured floor.
    plan_drawings = {drawing["floor"]: drawing for drawing in floor_drawings(
        topology, route["outbound"] if route else {"edges": []})}
    floors = []
    for floor_label, measured in sorted(groups.items(), key=lambda item: item[0]):
        attached = plan_drawings.get(floor_label)
        visible_ids = {node["id"] for node, _ in measured}
        waypoint_locations = [
            (Decimal(point["x_m"]), Decimal(point["y_m"]))
            for edge in topology["edges"]
            if edge["start"] in visible_ids and edge["end"] in visible_ids
            for point in edge.get("waypoints_m") or []
        ]
        xs = [point[0] for _, point in measured] + [point[0] for point in waypoint_locations]
        ys = [point[1] for _, point in measured] + [point[1] for point in waypoint_locations]
        extent_x, extent_y = max(xs) - min(xs), max(ys) - min(ys)
        scale = min(Decimal(520) / max(extent_x, Decimal(1)),
                    Decimal(320) / max(extent_y, Decimal(1)))
        center_x, center_y = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
        positions = {}
        for node, (x, y) in measured:
            projected = next((item for item in attached["nodes"] if item["id"] == node["id"]), None) if attached else None
            positions[node["id"]] = {
                "id": node["id"], "label": node["label"],
                "x": projected["x"] if projected else str((Decimal(300) + (x - center_x) * scale).quantize(Decimal("0.01"))),
                "y": projected["y"] if projected else str((Decimal(200) - (y - center_y) * scale).quantize(Decimal("0.01"))),
                "source": node["coordinate_source"],
                "on_route": node["id"] in route_points,
            }
        edges = []
        attached_edges = {edge["id"]: edge for edge in attached["edges"]} if attached else {}
        for edge in topology["edges"]:
            if edge["start"] in positions and edge["end"] in positions:
                drawn = attached_edges.get(edge["id"])
                if drawn and edge.get("waypoints_m"):
                    intermediates = [{"floor": floor_label, "x": item["x"], "y": item["y"]}
                                     for item in drawn["path"][1:-1]]
                else:
                    intermediates = [{
                        "floor": floor_label,
                        "x": str((Decimal(300) + (Decimal(item["x_m"]) - center_x) * scale).quantize(Decimal("0.01"))),
                        "y": str((Decimal(200) - (Decimal(item["y_m"]) - center_y) * scale).quantize(Decimal("0.01"))),
                    } for item in edge.get("waypoints_m") or []]
                path = [positions[edge["start"]], *intermediates, positions[edge["end"]]]
                edges.append({
                    "id": edge["id"], "start": positions[edge["start"]],
                    "end": positions[edge["end"]],
                    "on_route": edge["id"] in route_ids,
                    "resource_id": edge.get("resource_id"),
                    "length_m": edge.get("length_m"),
                    "length_source": edge.get("length_source"),
                    "path": path,
                    "geometry_source": edge.get("geometry_source"),
                    "svg_points": " ".join(f'{point["x"]},{point["y"]}' for point in path),
                })
        floors.append({"label": floor_label, "nodes": list(positions.values()),
                       "edges": edges, "plan": attached["plan"] if attached else None})
    transitions = []
    seen = set()
    for step in route_steps:
        from_node, to_node = nodes[step["from"]], nodes[step["to"]]
        if (from_node.get("floor") or "Общий уровень") != (to_node.get("floor") or "Общий уровень"):
            key = (step["edge_id"], step["from"], step["to"])
            if key not in seen:
                transitions.append({"from_label": from_node["label"],
                                    "to_label": to_node["label"],
                                    "from_id": step["from"],
                                    "to_id": step["to"],
                                    "from_floor": from_node.get("floor"),
                                    "to_floor": to_node.get("floor"),
                                    "length_m": step["length_m"],
                                    "length_source": step["length_source"]})
                seen.add(key)
    return {"floors": floors, "transitions": transitions,
            "missing_coordinates": missing}


def _shape_for_step(scene, topology, step):
    """Only return an attested shape for the exact chosen edge and direction."""
    edge = next((item for item in topology["edges"] if item["id"] == step["edge_id"]), None)
    if not edge or not edge.get("waypoints_m") or not edge.get("geometry_source"):
        return None
    match = next((item for floor in scene["floors"] for item in floor["edges"]
                  if item["id"] == step["edge_id"]), None)
    if match is None:
        return None
    if match["start"]["id"] == step["from"] and match["end"]["id"] == step["to"]:
        path = match["path"]
    elif match["end"]["id"] == step["from"] and match["start"]["id"] == step["to"]:
        path = list(reversed(match["path"]))
    else:
        raise PlaybackDataError("Промежуточные точки не соответствуют выбранному направлению.")
    return [{"floor": item.get("floor") or match["start"].get("floor"),
             "x": item["x"], "y": item["y"]} for item in path]


def position_on_path(stage, fraction):
    """Screen-space point at a share of the shape's projected arclength."""
    points = stage.get("path") or (stage["from"], stage["to"])
    fraction = max(Decimal(0), min(Decimal(1), Decimal(str(fraction))))
    segments = []
    total = Decimal(0)
    for start, end in zip(points, points[1:]):
        dx, dy = Decimal(end["x"]) - Decimal(start["x"]), Decimal(end["y"]) - Decimal(start["y"])
        length = (dx * dx + dy * dy).sqrt()
        segments.append((start, end, length))
        total += length
    if total == 0:
        return Decimal(points[0]["x"]), Decimal(points[0]["y"])
    left = fraction * total
    for start, end, length in segments:
        if left <= length:
            if length == 0:
                return Decimal(end["x"]), Decimal(end["y"])
            portion = left / length
            return (Decimal(start["x"]) + (Decimal(end["x"]) - Decimal(start["x"])) * portion,
                    Decimal(start["y"]) + (Decimal(end["y"]) - Decimal(start["y"])) * portion)
        left -= length
    return Decimal(points[-1]["x"]), Decimal(points[-1]["y"])


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
    if ledger.get("version") == 3:
        # v3 records absolute timings for every robot. Shared passages can
        # delay a single traversal, so a global fixed-duration cycle is wrong.
        try:
            start_bound, end_bound = Decimal(str(page_start)), Decimal(str(page_end))
            if start_bound < 0 or end_bound < start_bound:
                raise PlaybackDataError("Неверный диапазон воспроизведения.")
            if not isinstance(ledger.get("motion_cycles"), list):
                raise PlaybackDataError("В журнале отсутствует движение роботов.")
            cycles = []
            for item in ledger["motion_cycles"]:
                start, end = Decimal(item["start_s"]), Decimal(item["end_s"])
                if end < start or not (start <= end_bound and end >= start_bound):
                    if end < start:
                        raise PlaybackDataError("Неверное время движения робота.")
                    continue
                stages = []
                step_index = 0
                for stage in item["stages"]:
                    left, right = Decimal(stage["start_s"]), Decimal(stage["end_s"])
                    if left < start or right > end or right < left:
                        raise PlaybackDataError("Время этапа выходит за границы цикла.")
                    if stage["from"] not in positions or stage["to"] not in positions:
                        raise PlaybackDataError("Узел движения отсутствует на измеренной схеме.")
                    stage_display = {
                        "kind": stage["kind"], "start_s": str(left - start),
                        "end_s": str(right - start),
                        "from": positions[stage["from"]], "to": positions[stage["to"]],
                        **({"resource_id": stage["resource_id"]}
                           if stage.get("resource_id") else {}),
                    }
                    if stage["kind"] in {"travel", "elevator"}:
                        if step_index >= len(route_steps):
                            raise PlaybackDataError("В сохранённой траектории больше участков, чем в маршруте.")
                        route_step = route_steps[step_index]
                        if (stage["from"], stage["to"]) != (route_step["from"], route_step["to"]):
                            raise PlaybackDataError("Сохранённая траектория расходится с маршрутом.")
                        step_index += 1
                        shape = _shape_for_step(scene, topology, route_step)
                        if shape and stage["kind"] == "travel":
                            stage_display["path"] = shape
                    stages.append(stage_display)
                if step_index != len(route_steps):
                    raise PlaybackDataError("В сохранённой траектории отсутствуют участки маршрута.")
                cycles.append({
                    "robot_id": item["robot_id"], "source_row": item["source_row"],
                    "start_s": str(start), "end_s": str(end), "stages": stages,
                })
        except (KeyError, InvalidOperation, TypeError, ValueError, IndexError) as exc:
            raise PlaybackDataError("Фазы движения сохранённого прогона повреждены.") from exc
        return {"stages": [], "cycles": cycles, "reason": None}
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

    def add_stage(kind, start_node, end_node, duration, route_step=None):
        nonlocal elapsed
        stage = {"kind": kind, "start_s": str(elapsed),
                 "end_s": str(elapsed + duration),
                 "from": positions[start_node], "to": positions[end_node]}
        if kind == "travel" and route_step:
            shape = _shape_for_step(scene, topology, route_step)
            if shape:
                stage["path"] = shape
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
            add_stage(kind, step["from"], step["to"], duration, step)
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


def resource_state_events(calendar_rows, period_start):
    """Preserve every attested state interval boundary in a saved run."""
    try:
        origin = datetime.fromisoformat(period_start).astimezone(timezone.utc)
        events = []
        for row in calendar_rows:
            start = datetime.fromisoformat(row["start_at_utc"]).astimezone(timezone.utc)
            end = datetime.fromisoformat(row["end_at_utc"]).astimezone(timezone.utc)
            state = row["state"]
            delta = start - origin
            end_delta = end - origin
            at_s = (Decimal(delta.days * 86400 + delta.seconds)
                    + Decimal(delta.microseconds) / Decimal(1_000_000))
            end_s = (Decimal(end_delta.days * 86400 + end_delta.seconds)
                     + Decimal(end_delta.microseconds) / Decimal(1_000_000))
            if at_s < 0 or end_s <= at_s:
                raise PlaybackDataError("Границы календаря расходятся с периодом прогона.")
            events.append({
                "type": "calendar_state", "at_s": str(at_s), "end_s": str(end_s),
                "robot_id": f"slot-{row['robot_slot']}", "state": state,
                "state_label": CALENDAR_STATE_LABELS[state],
                "source_row": row["source_row"],
            })
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise PlaybackDataError("Сохранённый календарь содержит неверное событие ресурса.") from exc
    events.sort(key=lambda event: (Decimal(event["at_s"]), event["robot_id"], event["source_row"]))
    return events


def playback_events(ledger, calendar_rows, *, resource_events=None):
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

    events = list(ledger["events"])
    if resource_events is not None:
        events.extend(resource_events)
    else:
        by_robot = {}
        for row in calendar_rows:
            by_robot.setdefault(row["robot_slot"], []).append(row)
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
