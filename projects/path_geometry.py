"""Source-attested intermediate route coordinates, independent of edge travel length."""

from decimal import Decimal, InvalidOperation


MAX_WAYPOINTS = 32
DISTANCE_TOLERANCE_M = Decimal("0.05")


class RouteGeometryError(ValueError):
    pass


def _xy(value, label):
    try:
        x, y = Decimal(str(value["x_m"])), Decimal(str(value["y_m"]))
    except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
        raise RouteGeometryError(f"{label}: требуются координаты X и Y в метрах.") from exc
    if not all(number.is_finite() and abs(number) <= 1_000_000 for number in (x, y)):
        raise RouteGeometryError(f"{label}: координаты вне диапазона ±1 000 000 м.")
    return x, y


def validate_waypoints(topology, start_id, end_id, points, source, measured_length):
    """Return JSON-safe waypoints after geometry/source/length consistency checks.

    Schematic direct edges remain legal without these optional points. If a path
    is supplied, the measured length cannot be less than the geometric lower
    bound, but no distance is inferred from pixel or point positions.
    """
    if not isinstance(points, list):
        raise RouteGeometryError("Контрольные точки должны быть списком координат.")
    if not points:
        if source:
            raise RouteGeometryError("Удалите источник формы маршрута, если нет промежуточных точек.")
        return []
    if not isinstance(points, list) or len(points) > MAX_WAYPOINTS:
        raise RouteGeometryError("На одном участке допускается не более 32 контрольных точек.")
    if not isinstance(source, str) or not source.strip():
        raise RouteGeometryError("Укажите источник измеренных координат поворотов.")
    if measured_length is None:
        raise RouteGeometryError("Для формы пути нужна независимо измеренная длина участка.")
    nodes = {node["id"]: node for node in topology["nodes"]}
    start, end = nodes.get(start_id), nodes.get(end_id)
    if (start is None or end is None or start.get("floor") != end.get("floor")
            or not start.get("floor")):
        raise RouteGeometryError("Промежуточные точки допустимы только на одном известном этаже.")
    if any(not node.get("coordinate_source") or node.get("x_m") is None
           or node.get("y_m") is None for node in (start, end)):
        raise RouteGeometryError("Нужны измеренные координаты обеих крайних точек.")
    positions = [_xy(start, "Начало")]
    try:
        for index, point in enumerate(points, 1):
            positions.append(_xy(point, f"Поворот № {index}"))
        positions.append(_xy(end, "Конец"))
    except (TypeError, AttributeError) as exc:
        raise RouteGeometryError("Неверный формат контрольной точки.") from exc
    length = Decimal(0)
    for prev, nxt in zip(positions, positions[1:]):
        dx, dy = nxt[0] - prev[0], nxt[1] - prev[1]
        segment = (dx * dx + dy * dy).sqrt()
        if segment < Decimal("0.001"):
            raise RouteGeometryError("Соседние контрольные точки должны быть разными.")
        length += segment
    try:
        measured = Decimal(str(measured_length))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise RouteGeometryError("Укажите измеренную длину участка.") from exc
    if not measured.is_finite() or measured <= 0 or length > measured + DISTANCE_TOLERANCE_M:
        raise RouteGeometryError(
            "Измеренная длина участка короче геометрического пути через указанные точки. "
            "Сверьте длину, координаты и их источники."
        )
    return [{"x_m": str(x), "y_m": str(y)} for x, y in positions[1:-1]]


def parse_waypoint_lines(raw):
    if not isinstance(raw, str) or len(raw) > 2048:
        raise RouteGeometryError("Список промежуточных точек слишком длинный.")
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if len(lines) > MAX_WAYPOINTS:
        raise RouteGeometryError("Не более 32 поворотов на одном участке.")
    result = []
    for line in lines:
        parts = [part.strip().replace(",", ".") for part in line.split(";")]
        if len(parts) != 2:
            raise RouteGeometryError("Каждая строка: X;Y в метрах. Например: 4.5;7.2")
        result.append({"x_m": parts[0], "y_m": parts[1]})
    return result


def screen_path(topology, edge, coordinates):
    """Projection expects a callback from measured (x_m, y_m) to SVG x, y."""
    return [coordinates(point["x_m"], point["y_m"])
            for point in edge.get("waypoints_m") or []]
