"""Metrics derived only from a verified event ledger and robot calendar."""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from itertools import groupby


METRICS_VERSION = "metrics1"
MICROSECONDS = Decimal(1_000_000)
SECONDS_PER_DAY = 86_400
CALENDAR_STATES = {"available", "charging", "maintenance", "downtime"}
EVENT_TYPES = {"arrival", "start", "handoff", "complete"}


class SimulationMetricsError(ValueError):
    """Raised when ledger or calendar data cannot safely produce metrics."""


def _aware(value, label):
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    except (TypeError, ValueError) as exc:
        raise SimulationMetricsError(f"{label}: требуется время ISO 8601.") from exc
    if not isinstance(parsed, datetime) or parsed.utcoffset() is None:
        raise SimulationMetricsError(f"{label}: требуется часовой пояс.")
    return parsed.astimezone(timezone.utc)


def _decimal(value, label, *, positive=False):
    if isinstance(value, bool):
        raise SimulationMetricsError(f"{label}: требуется конечное число.")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise SimulationMetricsError(f"{label}: требуется конечное число.") from exc
    if not number.is_finite() or number < 0 or (positive and number == 0):
        qualifier = "положительное " if positive else "неотрицательное "
        raise SimulationMetricsError(f"{label}: требуется {qualifier}конечное число.")
    return number


def _seconds(value, origin):
    delta = value - origin
    return (Decimal(delta.days * SECONDS_PER_DAY + delta.seconds)
            + Decimal(delta.microseconds) / MICROSECONDS)


def _integer(value, label, *, minimum=0):
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise SimulationMetricsError(f"{label}: неверное целое значение.")
    return value


def _string(number):
    return str(number)


def _read_calendar(rows, origin, horizon):
    if not isinstance(rows, (list, tuple)) or not rows:
        raise SimulationMetricsError("Календарь должен содержать строки роботов.")
    by_robot = {}
    seen_sources = set()
    for row in rows:
        if not isinstance(row, dict):
            raise SimulationMetricsError("Строка календаря должна быть объектом.")
        slot = _integer(row.get("robot_slot"), "Номер робота", minimum=1)
        source_row = _integer(row.get("source_row"), "Номер строки календаря", minimum=1)
        if source_row in seen_sources:
            raise SimulationMetricsError("Номера строк календаря должны быть уникальны.")
        seen_sources.add(source_row)
        state = row.get("state")
        if not isinstance(state, str) or state not in CALENDAR_STATES:
            raise SimulationMetricsError("Неизвестное состояние робота в календаре.")
        start = _seconds(_aware(row.get("start_at_utc"), "Начало календаря"), origin)
        end = _seconds(_aware(row.get("end_at_utc"), "Конец календаря"), origin)
        if start < 0 or end > horizon or start >= end:
            raise SimulationMetricsError("Интервал календаря вне периода или пуст.")
        by_robot.setdefault(f"slot-{slot}", []).append((start, end, state))

    totals = {state: Decimal(0) for state in CALENDAR_STATES}
    for robot_id, intervals in by_robot.items():
        intervals.sort(key=lambda item: item[0])
        cursor = Decimal(0)
        for start, end, state in intervals:
            if start != cursor:
                raise SimulationMetricsError(
                    f"Календарь {robot_id} имеет пробел или пересечение."
                )
            totals[state] += end - start
            cursor = end
        if cursor != horizon:
            raise SimulationMetricsError(f"Календарь {robot_id} не покрывает период.")
    return by_robot, totals


def _percentile_95(values):
    if not values:
        return None
    ordered = sorted(values)
    # Nearest-rank percentile: rank = ceil(0.95 * n).
    rank = (95 * len(ordered) + 99) // 100
    return ordered[rank - 1]


def simulation_metrics(ledger, calendar_rows):
    """Return dimensioned metrics from the complete ledger and attested calendar.

    The caller is responsible for verifying the ledger's provenance. Calendar
    rows use the exact ``availability.py`` row contract and must cover each
    represented robot's whole ledger period without gaps or overlaps.
    """
    if not isinstance(ledger, dict) or ledger.get("version") not in (2, 3):
        raise SimulationMetricsError("Ожидается полный журнал событий версии 2 или 3.")
    origin = _aware(ledger.get("period_start_utc"), "Начало периода")
    finish = _aware(ledger.get("period_end_utc"), "Конец периода")
    horizon = _seconds(finish, origin)
    if horizon <= 0:
        raise SimulationMetricsError("Конец периода должен быть позже начала.")

    calendar, calendar_totals = _read_calendar(calendar_rows, origin, horizon)
    events = ledger.get("events")
    if not isinstance(events, (list, tuple)):
        raise SimulationMetricsError("Журнал должен содержать полный список событий.")

    jobs = {}
    grouped = []
    for event in events:
        if (not isinstance(event, dict) or not isinstance(event.get("type"), str)
                or event["type"] not in EVENT_TYPES):
            raise SimulationMetricsError("Неверное событие журнала.")
        kind = event["type"]
        row = _integer(event.get("source_row"), "Строка задания", minimum=2)
        at = _decimal(event.get("at_s"), "Время события")
        if at > horizon or (kind == "arrival" and at >= horizon):
            raise SimulationMetricsError("Событие выходит за границы периода.")
        units = _decimal(event.get("work_units"), "Объём задания", positive=True)
        job = jobs.setdefault(row, {"units": units})
        if job["units"] != units or kind in job:
            raise SimulationMetricsError("События задания имеют дубликат или разный объём.")
        job[kind] = at
        if kind in {"start", "handoff", "complete"}:
            robot_id = event.get("robot_id")
            if not isinstance(robot_id, str) or robot_id not in calendar:
                raise SimulationMetricsError("Событие ссылается на неизвестного робота.")
            job.setdefault("robots", {})[kind] = robot_id
            if len(set(job["robots"].values())) != 1:
                raise SimulationMetricsError("Задание меняет робота внутри цикла.")
        grouped.append((at, kind, row))

    starts_by_robot = {robot_id: [] for robot_id in calendar}
    waits_started = []
    waits_censored = []
    delivered_units = Decimal(0)
    completed_units = Decimal(0)
    for row, job in jobs.items():
        if "arrival" not in job:
            raise SimulationMetricsError("У задания отсутствует поступление.")
        arrival = job["arrival"]
        for kind in ("start", "handoff", "complete"):
            if kind in job and "start" not in job:
                raise SimulationMetricsError("Событие задания произошло до его старта.")
            if kind in job and job[kind] < (job.get("start", arrival)):
                raise SimulationMetricsError("Нарушен порядок событий задания.")
        if "start" in job:
            if job["start"] < arrival:
                raise SimulationMetricsError("Старт задания раньше его поступления.")
            waits_started.append(job["start"] - arrival)
            robot_id = job["robots"]["start"]
            cycle_end = job.get("complete", horizon)
            if cycle_end > horizon:
                raise SimulationMetricsError("Цикл завершён за пределами периода.")
            if "complete" in job and cycle_end < job["start"]:
                raise SimulationMetricsError("Конец цикла раньше его начала.")
            starts_by_robot[robot_id].append((job["start"], cycle_end, row))
        else:
            if "handoff" in job or "complete" in job:
                raise SimulationMetricsError("Незапущенное задание имеет результат.")
            waits_censored.append(horizon - arrival)
        if "handoff" in job:
            delivered_units += job["units"]
        elif "complete" in job:
            raise SimulationMetricsError("Завершённый цикл не имеет передачи груза.")
        if "complete" in job:
            completed_units += job["units"]

    busy_by_robot = {}
    for robot_id, cycles in starts_by_robot.items():
        cycles.sort()
        busy = Decimal(0)
        previous_end = Decimal(0)
        intervals = calendar[robot_id]
        for start, end, _row in cycles:
            if start < previous_end or end <= start:
                raise SimulationMetricsError("Циклы робота пересекаются или имеют неверную длину.")
            if not any(state == "available" and left <= start and end <= right
                       for left, right, state in intervals):
                raise SimulationMetricsError("Цикл выходит за доступный интервал робота.")
            busy += end - start
            previous_end = end
        busy_by_robot[robot_id] = busy

    queue = 0
    maximum_queue = 0
    queue_area = Decimal(0)
    last_at = Decimal(0)
    arrivals_at = {}
    starts_at = {}
    for at, kind, _row in grouped:
        if kind == "arrival":
            arrivals_at[at] = arrivals_at.get(at, 0) + 1
        elif kind == "start":
            starts_at[at] = starts_at.get(at, 0) + 1
    for at in sorted(set(arrivals_at) | set(starts_at)):
        queue_area += Decimal(queue) * (at - last_at)
        queue += arrivals_at.get(at, 0) - starts_at.get(at, 0)
        if queue < 0:
            raise SimulationMetricsError("Число стартов превышает поступившие задания.")
        # event_ledger updates max_queue after processing the entire timestamp
        # group, so a same-second arrival/start does not create a queue peak.
        maximum_queue = max(maximum_queue, queue)
        last_at = at
    queue_area += Decimal(queue) * (horizon - last_at)

    counts = {kind: sum(kind in job for job in jobs.values())
              for kind in ("arrival", "start", "handoff", "complete")}
    expected = {
        "arrivals": counts["arrival"], "started": counts["start"],
        "delivered": counts["handoff"], "completed": counts["complete"],
        "delivered_work_units": _string(delivered_units),
        "throughput_work_units": _string(completed_units),
        "queued_at_end": counts["arrival"] - counts["start"],
        "in_progress_at_end": counts["start"] - counts["complete"],
        "unmet_at_end": counts["arrival"] - counts["handoff"],
        "max_queue": maximum_queue,
    }
    for key, value in expected.items():
        actual = ledger.get(key)
        if key.endswith("work_units"):
            if _decimal(actual, key) != _decimal(value, key):
                raise SimulationMetricsError(f"Поле {key} расходится с событиями.")
        elif _integer(actual, key) != value:
            raise SimulationMetricsError(f"Поле {key} расходится с событиями.")
    if not counts["complete"] <= counts["handoff"] <= counts["start"] <= counts["arrival"]:
        raise SimulationMetricsError("Счётчики событий внутренне противоречивы.")

    delivered_count = counts["handoff"]
    started_mean = (sum(waits_started, Decimal(0)) / len(waits_started)
                    if waits_started else None)
    observed_mean = (sum(waits_started + waits_censored, Decimal(0)) / counts["arrival"]
                     if counts["arrival"] else None)
    available = calendar_totals["available"]
    busy_total = sum(busy_by_robot.values(), Decimal(0))
    per_robot = []
    for robot_id in sorted(calendar):
        robot_available = sum((end - start for start, end, state in calendar[robot_id]
                               if state == "available"), Decimal(0))
        robot_busy = busy_by_robot[robot_id]
        per_robot.append({
            "robot_id": robot_id,
            "busy_robot_seconds": _string(robot_busy),
            "available_robot_seconds": _string(robot_available),
            "utilization_ratio": (_string(robot_busy / robot_available)
                                  if robot_available else None),
        })

    result = {
        "version": METRICS_VERSION,
        "period": {
            "start_utc": origin.isoformat(), "end_utc": finish.isoformat(),
            "duration_seconds": _string(horizon),
        },
        "jobs": {
            "arrivals_jobs": counts["arrival"], "started_jobs": counts["start"],
            "delivered_jobs": delivered_count, "completed_cycles": counts["complete"],
            "delivery_ratio": (_string(Decimal(delivered_count) / counts["arrival"])
                               if counts["arrival"] else None),
        },
        "throughput": {
            "delivered_work_units": _string(delivered_units),
            "delivered_work_units_per_hour": _string(delivered_units * 3600 / horizon),
        },
        "waiting": {
            "started_jobs_mean_seconds": _string(started_mean) if started_mean is not None else None,
            "started_jobs_max_seconds": _string(max(waits_started)) if waits_started else None,
            "started_jobs_p95_seconds": (_string(_percentile_95(waits_started))
                                         if waits_started else None),
            "unfinished_waiting_jobs": len(waits_censored),
            "unfinished_waiting_censored_seconds": _string(sum(waits_censored, Decimal(0))),
            "all_arrivals_observed_mean_seconds": (_string(observed_mean)
                                                    if observed_mean is not None else None),
        },
        "queue": {
            "mean_jobs": _string(queue_area / horizon),
            "queue_job_seconds": _string(queue_area),
        },
        "fleet": {
            "busy_robot_seconds": _string(busy_total),
            "available_robot_seconds": _string(available),
            "utilization_ratio": (_string(busy_total / available) if available else None),
        },
        "calendar": {
            "charging_robot_seconds": _string(calendar_totals["charging"]),
            "maintenance_robot_seconds": _string(calendar_totals["maintenance"]),
            "downtime_robot_seconds": _string(calendar_totals["downtime"]),
        },
        "robots": per_robot,
    }
    if ledger["version"] == 3:
        cycles = ledger.get("motion_cycles")
        reservations = ledger.get("resource_reservations")
        if not isinstance(cycles, list) or not isinstance(reservations, list):
            raise SimulationMetricsError("В журнале v3 отсутствуют фазы и занятость ресурсов.")
        waiting = Decimal(0)
        reserved_stages = 0
        for cycle in cycles:
            if not isinstance(cycle, dict) or not isinstance(cycle.get("stages"), list):
                raise SimulationMetricsError("Фазы цикла ресурса повреждены.")
            for stage in cycle["stages"]:
                if not isinstance(stage, dict):
                    raise SimulationMetricsError("Фаза ресурса повреждена.")
                if stage.get("kind") == "resource_wait":
                    end = _decimal(stage.get("end_s"), "Конец ожидания ресурса")
                    start = _decimal(stage.get("start_s"), "Начало ожидания ресурса")
                    if end < start:
                        raise SimulationMetricsError("Ожидание ресурса имеет отрицательную длительность.")
                    waiting += end - start
                if stage.get("resource_id") and stage.get("kind") in ("travel", "elevator"):
                    reserved_stages += 1
        if (waiting != _decimal(ledger.get("resource_wait_seconds"), "Ожидание ресурсов")
                or reserved_stages != len(reservations)):
            raise SimulationMetricsError("Занятость и ожидание ресурсов расходятся с фазами движения.")
        result["resources"] = {
            "total_wait_seconds": _string(waiting),
            "reservations": len(reservations),
            "constrained_cycles": sum(any(s.get("resource_id") for s in c["stages"])
                                      for c in cycles),
        }
    return result
