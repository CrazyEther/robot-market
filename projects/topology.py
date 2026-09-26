"""Owner-attested facility graph and route feasibility for a project revision.

An edge asserts connectivity. Coordinates are optional measured drawing positions;
they never create an edge or a distance on their own.
"""

from collections import deque
from decimal import Decimal, InvalidOperation
from heapq import heappop, heappush

from projects.task_profiles import process_for


def empty_topology(object_slug, process):
    return {"version": 1, "object_slug": object_slug, "process": process,
            "nodes": [], "edges": [], "origin": None, "destination": None}


def _measured_length(edge):
    raw = edge.get("length_m")
    if raw is None:
        return None
    try:
        length = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return length if length.is_finite() and length > 0 else None


def _route_steps(parent, nodes, origin, destination):
    steps = []
    current = destination
    while current != origin:
        previous, edge = parent[current]
        length = _measured_length(edge)
        steps.append({
            "edge_id": edge["id"], "from": previous, "to": current,
            "from_label": nodes[previous].get("label", previous),
            "to_label": nodes[current].get("label", current),
            "length_m": str(length) if length is not None else None,
            "source": edge.get("source"), "length_source": edge.get("length_source"),
        })
        current = previous
    return list(reversed(steps))


def route_result(topology):
    nodes = {node["id"]: node for node in topology.get("nodes", [])}
    origin, destination = topology.get("origin"), topology.get("destination")
    if not origin or not destination:
        return {"status": "needs_endpoints", "message": "Выберите начало и конец маршрута.", "edges": [], "distance_m": None}
    if origin not in nodes or destination not in nodes or origin == destination:
        return {"status": "invalid_endpoints", "message": "Начало и конец должны быть разными существующими точками.", "edges": [], "distance_m": None}
    if topology["object_slug"] == "hospital" and topology.get("route_flow") not in {"clean", "dirty"}:
        return {"status": "needs_flow", "message": "Выберите санитарный поток маршрута.", "edges": [], "distance_m": None}
    if topology["object_slug"] == "airport" and any(not nodes[point].get("zone") for point in (origin, destination)):
        return {"status": "needs_access", "message": "Укажите режим доступа для начальной и конечной точек.", "edges": [], "distance_m": None}
    airport_access = None
    if topology["object_slug"] == "airport":
        process = process_for("airport", topology.get("process"))
        if process is None:
            return {"status": "invalid_process", "message": "Выберите операцию аэропорта.", "edges": [], "distance_m": None}
        airport_access = "restricted" if "airside_authorization" in process.required_specs else "public"
        if any(nodes[point]["zone"] != airport_access for point in (origin, destination)):
            return {"status": "blocked", "message": "Точки не соответствуют режиму доступа выбранной операции.",
                    "edges": [], "distance_m": None}
    if topology["object_slug"] == "hospital" and any(not nodes[point].get("floor") for point in (origin, destination)):
        return {"status": "needs_floor", "message": "Укажите этаж для каждой точки маршрута.", "edges": [], "distance_m": None}
    if topology["object_slug"] == "hospital" and any(not nodes[point].get("zone") for point in (origin, destination)):
        return {"status": "needs_zone", "message": "Укажите санитарную зону для каждой точки маршрута.", "edges": [], "distance_m": None}
    adjacency = {node_id: [] for node_id in nodes}
    for edge in topology.get("edges", []):
        start, end = nodes.get(edge["start"]), nodes.get(edge["end"])
        if not start or not end:
            continue
        if topology["object_slug"] == "airport":
            if (edge["access"] != airport_access or start.get("zone") != airport_access
                    or end.get("zone") != airport_access):
                continue
        if topology["object_slug"] == "hospital":
            if not start.get("floor") or not end.get("floor") or not start.get("zone") or not end.get("zone"):
                continue
            if start["floor"] != end["floor"] and edge["kind"] != "elevator":
                continue
            flow = topology["route_flow"]
            if edge["flow"] != flow:
                continue
            if start["zone"] not in {flow, "neutral"} or end["zone"] not in {flow, "neutral"}:
                continue
        adjacency[start["id"]].append((end["id"], edge))
        if edge["bidirectional"]:
            adjacency[end["id"]].append((start["id"], edge))
    queue = deque([origin])
    parent = {origin: None}
    while queue:
        current = queue.popleft()
        if current == destination:
            break
        for neighbor, edge in adjacency[current]:
            if neighbor not in parent:
                parent[neighbor] = (current, edge)
                queue.append(neighbor)
    if destination not in parent:
        return {"status": "blocked", "message": "По заданным соединениям доступного маршрута нет.", "edges": [], "distance_m": None}
    best = {origin: (Decimal(0), 0)}
    measured_parent = {origin: None}
    pending = [(Decimal(0), 0, origin)]
    while pending:
        distance, hops, current = heappop(pending)
        if (distance, hops) != best[current]:
            continue
        if current == destination:
            break
        for neighbor, edge in sorted(adjacency[current], key=lambda item: (item[0], item[1]["id"])):
            length = _measured_length(edge)
            if length is None:
                continue
            candidate = (distance + length, hops + 1)
            if neighbor not in best or candidate < best[neighbor]:
                best[neighbor] = candidate
                measured_parent[neighbor] = (current, edge)
                heappush(pending, (*candidate, neighbor))
    if destination not in best:
        steps = _route_steps(parent, nodes, origin, destination)
        return {"status": "needs_measurement", "message": "Маршрут есть. Измерьте длину каждого его участка для расчёта.",
                "edges": [step["edge_id"] for step in steps], "steps": steps, "distance_m": None}
    steps = _route_steps(measured_parent, nodes, origin, destination)
    return {"status": "measured", "message": "Маршрут измерен.",
            "edges": [step["edge_id"] for step in steps], "steps": steps,
            "distance_m": str(best[destination][0])}


def transport_cycle_route(topology, outbound=None):
    """Measure both legs without assuming that a directed edge permits return."""
    process = process_for(topology.get("object_slug"), topology.get("process"))
    if process is None or not any(field.key == "cargo_mass_kg" for field in process.fields):
        return None
    outbound = outbound if outbound is not None else route_result(topology)
    inbound = route_result({**topology, "origin": topology.get("destination"),
                            "destination": topology.get("origin")})
    measured = outbound["status"] == inbound["status"] == "measured"
    return {
        "status": "measured" if measured else "needs_route_data",
        "outbound": outbound,
        "inbound": inbound,
        "distance_m": str(Decimal(outbound["distance_m"]) + Decimal(inbound["distance_m"]))
        if measured else None,
    }


def floor_drawings(topology, route):
    """Scale only user-supplied coordinates into SVG; preserve separate floors."""
    route_edges = set(route["edges"])
    drawings = []
    floors = sorted({node["floor"] for node in topology["nodes"] if node.get("floor")})
    for floor in floors:
        measured = [node for node in topology["nodes"] if node.get("x_m") is not None
                    and node.get("y_m") is not None and node["floor"] == floor]
        if not measured:
            continue
        xs = [Decimal(node["x_m"]) for node in measured]
        ys = [Decimal(node["y_m"]) for node in measured]
        min_x, max_x, min_y, max_y = min(xs), max(xs), min(ys), max(ys)
        span = max(max_x - min_x, max_y - min_y, Decimal(1))
        positioned = {}
        for node in measured:
            positioned[node["id"]] = {
                "id": node["id"], "label": node["label"],
                "x": format(40 + (Decimal(node["x_m"]) - min_x) * 520 / span, ".2f"),
                "y": format(360 - (Decimal(node["y_m"]) - min_y) * 320 / span, ".2f"),
            }
        lines = []
        for edge in topology["edges"]:
            if edge["start"] in positioned and edge["end"] in positioned:
                lines.append({"id": edge["id"], "start": positioned[edge["start"]],
                              "end": positioned[edge["end"]], "on_route": edge["id"] in route_edges})
        drawings.append({"floor": floor, "nodes": list(positioned.values()), "edges": lines})
    return drawings
