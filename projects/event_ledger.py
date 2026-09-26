"""Deterministic scheduling against explicitly sourced availability windows.

This kernel makes no arrivals, robot calendars, charging intervals or failure
events. Its caller supplies them from a pinned project revision and source.
"""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from itertools import groupby


LEDGER_VERSION = 2
MICROSECONDS = Decimal(1_000_000)
SECONDS_PER_DAY = 86_400


class EventInputError(ValueError):
    pass


def _aware(value, label):
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    except ValueError as exc:
        raise EventInputError(f"{label}: неверное время ISO 8601.") from exc
    if not isinstance(parsed, datetime) or parsed.utcoffset() is None:
        raise EventInputError(f"{label}: нужен часовой пояс.")
    return parsed.astimezone(timezone.utc)


def _seconds(value, origin):
    delta = value - origin
    return (Decimal(delta.days * SECONDS_PER_DAY + delta.seconds)
            + Decimal(delta.microseconds) / MICROSECONDS)


def _positive(value, label):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise EventInputError(f"{label}: требуется число.") from exc
    if not result.is_finite() or result <= 0:
        raise EventInputError(f"{label}: требуется положительное конечное число.")
    return result


def _read_windows(robots, origin):
    if not isinstance(robots, (list, tuple)):
        raise EventInputError("Календарь роботов должен быть списком.")
    seen = set()
    calendars = []
    for robot in robots:
        if not isinstance(robot, dict):
            raise EventInputError("Робот должен содержать идентификатор и интервалы.")
        robot_id = robot.get("id")
        if not isinstance(robot_id, str) or not robot_id or robot_id in seen:
            raise EventInputError("Идентификатор робота должен быть непустым и уникальным.")
        seen.add(robot_id)
        windows = robot.get("windows")
        if not isinstance(windows, (list, tuple)):
            raise EventInputError("Для робота нужен список интервалов доступности.")
        parsed = []
        for window in windows:
            if not isinstance(window, dict) or not isinstance(window.get("source"), str) or not window["source"].strip():
                raise EventInputError("У каждого интервала должен быть подтверждённый источник.")
            start = _seconds(_aware(window.get("start_at"), "Начало доступности"), origin)
            end = _seconds(_aware(window.get("end_at"), "Конец доступности"), origin)
            if start >= end:
                raise EventInputError("Интервал доступности должен иметь положительную длину.")
            parsed.append((start, end))
        parsed.sort()
        if any(left[1] > right[0] for left, right in zip(parsed, parsed[1:])):
            raise EventInputError("Интервалы одного робота не должны пересекаться.")
        calendars.append({"id": robot_id, "windows": parsed, "free_at": Decimal(0)})
    return sorted(calendars, key=lambda robot: robot["id"])


def schedule_observed_jobs(jobs, robots, *, period_start, period_end):
    """Schedule arrivals FIFO over attested resource windows, clipped at end.

    Job duration denotes the entire robot operating cycle. The separately
    sourced handoff offset marks delivered work before the return leg ends.
    """
    origin = _aware(period_start, "Начало периода")
    horizon = _seconds(_aware(period_end, "Конец периода"), origin)
    if horizon <= 0:
        raise EventInputError("Конец периода должен быть позже начала.")
    if not isinstance(jobs, (list, tuple)):
        raise EventInputError("Задания должны быть списком наблюдений.")
    calendars = _read_windows(robots, origin)
    events = []
    previous_arrival = None
    seen_rows = set()
    total_arrivals = 0
    queue_blocked = False
    last_start_at = Decimal(0)
    for job in jobs:
        if not isinstance(job, dict):
            raise EventInputError("Неверная строка журнала заданий.")
        row = job.get("source_row")
        if not isinstance(row, int) or isinstance(row, bool) or row < 2 or row in seen_rows:
            raise EventInputError("Номер строки задания должен быть уникальным.")
        seen_rows.add(row)
        arrived_at = _seconds(_aware(job.get("requested_at_utc"), "Время задания"), origin)
        if arrived_at < 0 or arrived_at >= horizon:
            raise EventInputError(f"Строка {row}: задание вне периода наблюдения.")
        if previous_arrival is not None and arrived_at < previous_arrival:
            raise EventInputError("Задания должны идти по времени поступления.")
        previous_arrival = arrived_at
        duration = _positive(job.get("service_seconds"), f"Строка {row}, время цикла")
        handoff_offset = _positive(job.get("handoff_seconds"), f"Строка {row}, время передачи груза")
        if handoff_offset > duration:
            raise EventInputError(f"Строка {row}: передача груза позже окончания цикла.")
        units = _positive(job.get("work_units"), f"Строка {row}, объём")
        total_arrivals += 1
        common = {"source_row": row, "work_units": str(units)}
        events.append({"type": "arrival", "at_s": str(arrived_at), **common})
        if queue_blocked:
            continue
        candidates = []
        for robot in calendars:
            for window_start, window_end in robot["windows"]:
                possible_start = max(arrived_at, robot["free_at"],
                                     window_start, last_start_at)
                possible_finish = possible_start + duration
                if possible_start < horizon and possible_finish <= window_end:
                    candidates.append((possible_start, possible_finish, robot["id"], robot))
                    break
        if not candidates:
            queue_blocked = True
            continue
        started_at, completed_at, robot_id, robot = min(
            candidates, key=lambda item: (item[0], item[1], item[2]),
        )
        last_start_at = started_at
        robot["free_at"] = completed_at
        events.append({"type": "start", "at_s": str(started_at), "robot_id": robot_id, **common})
        handoff_at = started_at + handoff_offset
        if handoff_at <= horizon:
            events.append({"type": "handoff", "at_s": str(handoff_at), "robot_id": robot_id, **common})
        if completed_at <= horizon:
            events.append({"type": "complete", "at_s": str(completed_at), "robot_id": robot_id, **common})
    priority = {"arrival": 0, "handoff": 1, "complete": 2, "start": 3}
    events.sort(key=lambda event: (Decimal(event["at_s"]), priority[event["type"]], event["source_row"]))
    arrivals = started = delivered = completed = queued = max_queue = 0
    delivered_units = Decimal(0)
    completed_units = Decimal(0)
    for _, simultaneous in groupby(events, key=lambda event: Decimal(event["at_s"])):
        for event in simultaneous:
            if event["type"] == "arrival":
                arrivals += 1
                queued += 1
            elif event["type"] == "start":
                started += 1
                queued -= 1
            elif event["type"] == "handoff":
                delivered += 1
                delivered_units += Decimal(event["work_units"])
            elif event["type"] == "complete":
                completed += 1
                completed_units += Decimal(event["work_units"])
        max_queue = max(max_queue, queued)
    if arrivals != total_arrivals or not completed <= delivered <= started <= arrivals:
        raise EventInputError("Нарушена согласованность журнала событий.")
    return {
        "version": LEDGER_VERSION,
        "period_start_utc": origin.isoformat(),
        "period_end_utc": _aware(period_end, "Конец периода").isoformat(),
        "events": events,
        "arrivals": arrivals, "started": started,
        "delivered": delivered, "completed": completed,
        "delivered_work_units": str(delivered_units),
        "throughput_work_units": str(completed_units),
        "queued_at_end": arrivals - started,
        "in_progress_at_end": started - completed,
        "unmet_at_end": arrivals - delivered,
        "max_queue": max_queue,
    }
