"""Deterministic FIFO scheduling on source-attested shared route resources.

All durations, operating windows, priority and capacities are explicit inputs;
the kernel never invents travel times or infers capacity from a drawing.
"""

from bisect import bisect_left, bisect_right
from decimal import Decimal, InvalidOperation
from itertools import groupby

from projects.event_ledger import (
    EventInputError, _aware, _positive, _read_windows, _seconds,
)


RESOURCE_LEDGER_VERSION = 3


def _nonnegative(value, label):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise EventInputError(f"{label}: требуется числовое значение.") from exc
    if not number.is_finite() or number < 0:
        raise EventInputError(f"{label}: требуется конечное неотрицательное число.")
    return number


def _resources(resources, origin):
    if not isinstance(resources, (list, tuple)) or not resources:
        raise EventInputError("Для общего участка нужны подтверждённые ограничения.")
    parsed = {}
    for item in resources:
        if not isinstance(item, dict):
            raise EventInputError("Ресурс маршрута должен быть объектом.")
        name = item.get("id")
        capacity = item.get("capacity")
        if (not isinstance(name, str) or not name or name in parsed
                or not isinstance(capacity, int) or isinstance(capacity, bool)
                or not 1 <= capacity <= 10000):
            raise EventInputError("Общий ресурс требует уникальный ID и подтверждённую вместимость.")
        if item.get("direction_policy") not in {"mixed", "alternating"}:
            raise EventInputError("Не подтверждена совместимость встречного движения.")
        if item.get("occupancy_policy") != "entry_to_exit":
            raise EventInputError("Для ресурса не подтверждено время занятия от входа до выхода.")
        if item.get("priority_policy") != "fifo":
            raise EventInputError("Регламент приоритета ресурса пока не поддерживается: требуется FIFO.")
        if not isinstance(item.get("source"), str) or not item["source"].strip():
            raise EventInputError("Для ресурса нужен источник физических ограничений.")
        windows = item.get("windows")
        if not isinstance(windows, (list, tuple)) or not windows:
            raise EventInputError(f"Ресурс {name}: нет подтверждённых рабочих окон.")
        parsed_windows = []
        for window in windows:
            if not isinstance(window, dict) or not isinstance(window.get("source"), str) or not window["source"].strip():
                raise EventInputError(f"Ресурс {name}: у каждого окна должен быть источник.")
            start = _seconds(_aware(window.get("start_at"), "Открытие участка"), origin)
            end = _seconds(_aware(window.get("end_at"), "Закрытие участка"), origin)
            if start >= end:
                raise EventInputError(f"Ресурс {name}: пустой интервал работы.")
            parsed_windows.append((start, end))
        parsed_windows.sort()
        if any(a[1] > b[0] for a, b in zip(parsed_windows, parsed_windows[1:])):
            raise EventInputError(f"Ресурс {name}: рабочие окна пересекаются.")
        parsed[name] = {"capacity": capacity, "direction_policy": item["direction_policy"],
                        "windows": parsed_windows, "reservations": [],
                        "starts": [], "max_reservation_duration": Decimal(0)}
    return parsed


def _phases(phases, resources):
    if not isinstance(phases, (list, tuple)) or not phases:
        raise EventInputError("Для занятости участка нужны подтверждённые фазы маршрута.")
    results = []
    handoff = None
    elapsed = Decimal(0)
    for stage in phases:
        if not isinstance(stage, dict) or stage.get("kind") not in {
                "pickup", "travel", "elevator", "elevator_wait", "dropoff"}:
            raise EventInputError("Неизвестная фаза движения робота.")
        if not all(isinstance(stage.get(key), str) and stage[key]
                   for key in ("from", "to")):
            raise EventInputError("Для фазы нужны подтверждённые узлы маршрута.")
        duration = _nonnegative(stage.get("duration_s"), "Длительность фазы")
        if stage["kind"] in {"travel", "elevator"} and duration <= 0:
            raise EventInputError("Движение по участку должно иметь положительную длительность.")
        if stage["kind"] == "dropoff":
            if handoff is not None:
                raise EventInputError("Передача груза возможна только один раз за цикл.")
            handoff = elapsed + duration
        resource_id = stage.get("resource_id")
        direction = stage.get("direction")
        if resource_id is not None:
            if (resource_id not in resources or stage["kind"] not in {"travel", "elevator"}
                    or direction not in {"forward", "reverse"}):
                raise EventInputError("Фаза ссылается на неизвестный ресурс или направление.")
        elif direction is not None:
            raise EventInputError("Направление ресурса задано без его идентификатора.")
        results.append({"kind": stage["kind"], "from": stage["from"],
                        "to": stage["to"], "duration": duration,
                        "resource_id": resource_id, "direction": direction})
        elapsed += duration
    if handoff is None or elapsed <= 0 or handoff <= 0:
        raise EventInputError("Фазы не содержат измеренный цикл и передачу груза.")
    return results, elapsed, handoff


def _entry_at(resource, arrival, duration, direction, tentative=()):
    """First valid occupancy slot, accounting for capacity at every boundary."""
    for opens_at, closes_at in resource["windows"]:
        at = max(opens_at, arrival)
        while at + duration <= closes_at:
            # Committed reservations are sorted by start. A reservation still
            # active at ``at`` must start later than at - max_duration; avoid
            # rescanning completed reservations for every subsequent job.
            left = bisect_left(resource["starts"],
                               at - resource["max_reservation_duration"])
            right = bisect_left(resource["starts"], at + duration)
            overlapping = [r for r in resource["reservations"][left:right]
                           if r["end"] > at]
            overlapping.extend(r for r in tentative
                               if r["start"] < at + duration and r["end"] > at)
            incompatible = (resource["direction_policy"] == "alternating"
                            and any(r["direction"] != direction for r in overlapping))
            if not incompatible:
                boundaries = sorted({at, at + duration, *(
                    value for r in overlapping for value in (max(at, r["start"]),
                                                              min(at + duration, r["end"]))
                )})
                saturated = any(
                    sum(r["start"] <= left < r["end"] for r in overlapping)
                    >= resource["capacity"]
                    for left, right in zip(boundaries, boundaries[1:]) if left < right
                )
            else:
                saturated = False
            if not incompatible and not saturated:
                return at
            at = min(r["end"] for r in overlapping if r["end"] > at)
    return None


def _project_cycle(phases, resources, starts_at, window_end):
    """Dry-run reservations; commit only for the selected robot."""
    # Candidate robots share the committed ledger, but stage-local claims
    # remain private until this particular robot is selected.
    tentative = {name: [] for name in resources}
    stages = []
    claims = []
    clock = starts_at
    handoff_at = None
    waiting = Decimal(0)
    for stage in phases:
        duration = stage["duration"]
        resource_id = stage["resource_id"]
        if resource_id:
            entered = _entry_at(resources[resource_id], clock, duration, stage["direction"],
                                tentative[resource_id])
            if entered is None or entered + duration > window_end:
                return None
            if entered > clock:
                stages.append({"kind": "resource_wait", "start_s": str(clock),
                               "end_s": str(entered), "from": stage["from"],
                               "to": stage["from"], "resource_id": resource_id})
                waiting += entered - clock
            clock = entered
            reservation = {"resource_id": resource_id, "start": clock,
                           "end": clock + duration, "direction": stage["direction"]}
            claims.append(reservation)
            tentative[resource_id].append(reservation)
        stages.append({"kind": stage["kind"], "start_s": str(clock),
                       "end_s": str(clock + duration), "from": stage["from"],
                       "to": stage["to"], **({"resource_id": resource_id}
                                                if resource_id else {})})
        clock += duration
        if stage["kind"] == "dropoff":
            handoff_at = clock
        if clock > window_end:
            return None
    return {"end": clock, "handoff": handoff_at, "stages": stages,
            "claims": claims, "wait": waiting}


def schedule_resource_jobs(jobs, robots, phases, resources, *, period_start, period_end):
    """FIFO by source row; reservations delay traversal and handoff."""
    origin = _aware(period_start, "Начало периода")
    horizon = _seconds(_aware(period_end, "Конец периода"), origin)
    if horizon <= 0 or not isinstance(jobs, (list, tuple)):
        raise EventInputError("Неверный период или список наблюдённых заданий.")
    robots = _read_windows(robots, origin)
    shared = _resources(resources, origin)
    stages, cycle_seconds, handoff_seconds = _phases(phases, shared)
    events = []
    movements = []
    reservations = []
    prev_arrival = last_start = Decimal(0)
    seen = set()
    queue_blocked = False
    total_wait = Decimal(0)
    for job in jobs:
        if not isinstance(job, dict):
            raise EventInputError("Неверная строка журнала заданий.")
        row = job.get("source_row")
        if not isinstance(row, int) or isinstance(row, bool) or row < 2 or row in seen:
            raise EventInputError("Номер строки задания должен быть уникальным.")
        seen.add(row)
        arrival = _seconds(_aware(job.get("requested_at_utc"), "Время задания"), origin)
        if not 0 <= arrival < horizon or arrival < prev_arrival:
            raise EventInputError("Задания должны идти по времени внутри периода наблюдения.")
        prev_arrival = arrival
        units = _positive(job.get("work_units"), f"Строка {row}, объём")
        if (_positive(job.get("service_seconds"), f"Строка {row}, цикл") != cycle_seconds
                or _positive(job.get("handoff_seconds"), f"Строка {row}, передача") != handoff_seconds):
            raise EventInputError("Времена фаз расходятся с измеренным циклом.")
        common = {"source_row": row, "work_units": str(units)}
        events.append({"type": "arrival", "at_s": str(arrival), **common})
        if queue_blocked:
            continue
        candidates = []
        for robot in robots:
            for begins, ends in robot["windows"]:
                begins_at = max(begins, arrival, robot["free_at"], last_start)
                if begins_at >= horizon or begins_at >= ends:
                    continue
                candidate = _project_cycle(stages, shared, begins_at, ends)
                if candidate:
                    candidates.append((begins_at, candidate["end"], robot["id"], robot, candidate))
                    break
        if not candidates:
            queue_blocked = True
            continue
        began, finished, robot_id, robot, chosen = min(candidates,
                                                      key=lambda result: result[:3])
        robot["free_at"] = finished
        last_start = began
        total_wait += chosen["wait"]
        events.append({"type": "start", "at_s": str(began), "robot_id": robot_id, **common})
        if chosen["handoff"] <= horizon:
            events.append({"type": "handoff", "at_s": str(chosen["handoff"]),
                           "robot_id": robot_id, **common})
        if finished <= horizon:
            events.append({"type": "complete", "at_s": str(finished),
                           "robot_id": robot_id, **common})
        movements.append({"source_row": row, "robot_id": robot_id,
                          "start_s": str(began), "end_s": str(finished),
                          "handoff_s": str(chosen["handoff"]),
                          "stages": chosen["stages"]})
        for reservation in chosen["claims"]:
            saved = {"resource_id": reservation["resource_id"],
                     "direction": reservation["direction"],
                     "start_s": str(reservation["start"]),
                     "end_s": str(reservation["end"]),
                     "source_row": row, "robot_id": robot_id}
            reservations.append(saved)
            resource = shared[reservation["resource_id"]]
            position = bisect_right(resource["starts"], reservation["start"])
            resource["starts"].insert(position, reservation["start"])
            resource["reservations"].insert(position, reservation)
            resource["max_reservation_duration"] = max(
                resource["max_reservation_duration"], reservation["end"] - reservation["start"]
            )
    event_priority = {"arrival": 0, "handoff": 1, "complete": 2, "start": 3}
    events.sort(key=lambda e: (Decimal(e["at_s"]), event_priority[e["type"]], e["source_row"]))
    arrivals = started = delivered = completed = queue = max_queue = 0
    delivered_units = completed_units = Decimal(0)
    for _, at_time in groupby(events, key=lambda e: Decimal(e["at_s"])):
        for event in at_time:
            if event["type"] == "arrival":
                arrivals += 1
                queue += 1
            elif event["type"] == "start":
                started += 1
                queue -= 1
            elif event["type"] == "handoff":
                delivered += 1
                delivered_units += Decimal(event["work_units"])
            elif event["type"] == "complete":
                completed += 1
                completed_units += Decimal(event["work_units"])
        max_queue = max(max_queue, queue)
    if not completed <= delivered <= started <= arrivals:
        raise EventInputError("Нарушена согласованность журнала событий.")
    return {
        "version": RESOURCE_LEDGER_VERSION,
        "period_start_utc": origin.isoformat(),
        "period_end_utc": _aware(period_end, "Конец периода").isoformat(),
        "events": events, "arrivals": arrivals, "started": started,
        "delivered": delivered, "completed": completed,
        "delivered_work_units": str(delivered_units),
        "throughput_work_units": str(completed_units),
        "queued_at_end": arrivals - started,
        "in_progress_at_end": started - completed,
        "unmet_at_end": arrivals - delivered,
        "max_queue": max_queue,
        "resource_wait_seconds": str(total_wait),
        "resource_reservations": sorted(reservations,
            key=lambda r: (Decimal(r["start_s"]), r["resource_id"], r["source_row"])),
        "motion_cycles": movements,
    }
