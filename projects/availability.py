"""Read exact, owner-attested robot availability instead of synthesizing it."""

import csv
import hashlib
import io
from datetime import datetime, timezone

from projects.operation_logs import MAX_UPLOAD_BYTES, MAX_ROWS


PARSER_VERSION = 2
LEGACY_HEADER = ["robot_slot", "start_at", "end_at", "state"]
HEADER = [*LEGACY_HEADER, "ready_at_node"]
STATES = frozenset({"available", "charging", "maintenance", "downtime"})


class AvailabilityError(ValueError):
    pass


def _timestamp(raw, line):
    try:
        parsed = datetime.fromisoformat(raw.strip())
    except ValueError as exc:
        raise AvailabilityError(f"Строка {line}: неверное время ISO 8601.") from exc
    if parsed.utcoffset() is None:
        raise AvailabilityError(f"Строка {line}: укажите часовой пояс.")
    return parsed.astimezone(timezone.utc)


def _parse_availability(raw, *, period_start, period_end, fleet, parser_version, origin_node=None):
    if not raw or len(raw) > MAX_UPLOAD_BYTES:
        raise AvailabilityError("Файл пуст или превышает 2 МиБ.")
    if not isinstance(fleet, int) or isinstance(fleet, bool) or not 0 < fleet <= MAX_ROWS:
        raise AvailabilityError("Размер парка для календаря должен быть в допустимых пределах.")
    if period_start is None or period_end is None or period_start >= period_end:
        raise AvailabilityError("Укажите корректный период наблюдения в журнале заданий.")
    if parser_version == PARSER_VERSION and (not isinstance(origin_node, str) or not origin_node.strip()):
        raise AvailabilityError("Для календаря требуется начальная точка сохранённого маршрута.")
    expected_header = HEADER if parser_version == PARSER_VERSION else LEGACY_HEADER
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise AvailabilityError("Сохраните CSV в кодировке UTF-8.") from exc
    if "\x00" in content:
        raise AvailabilityError("Файл содержит недопустимые байты.")
    reader = None
    for delimiter in (",", ";"):
        candidate = csv.reader(io.StringIO(content, newline=""), delimiter=delimiter, strict=True)
        try:
            header = [cell.strip() for cell in next(candidate)]
        except (StopIteration, csv.Error):
            continue
        if header == expected_header:
            reader = candidate
            break
    if reader is None:
        raise AvailabilityError(f"Нужны ровно колонки {','.join(expected_header)}.")
    by_slot = {slot: [] for slot in range(1, fleet + 1)}
    rows = []
    try:
        for cells in reader:
            if len(rows) >= MAX_ROWS:
                raise AvailabilityError("Календарь превышает 10 000 строк.")
            if len(cells) != len(expected_header):
                raise AvailabilityError(f"Строка {reader.line_num}: неверное число колонок.")
            try:
                slot = int(cells[0].strip())
            except ValueError as exc:
                raise AvailabilityError(f"Строка {reader.line_num}: неверный номер робота.") from exc
            if slot not in by_slot:
                raise AvailabilityError(f"Строка {reader.line_num}: номер робота вне расчётного парка.")
            start, end = _timestamp(cells[1], reader.line_num), _timestamp(cells[2], reader.line_num)
            if start < period_start or end > period_end or start >= end:
                raise AvailabilityError(f"Строка {reader.line_num}: интервал вне периода или имеет неверные границы.")
            state = cells[3].strip().lower()
            if state not in STATES:
                raise AvailabilityError(f"Строка {reader.line_num}: неизвестное состояние робота.")
            if parser_version == PARSER_VERSION:
                ready_at_node = cells[4].strip()
                if state == "available" and ready_at_node != origin_node:
                    raise AvailabilityError(
                        f"Строка {reader.line_num}: для доступного робота подтвердите начальную точку маршрута."
                    )
                if state != "available" and ready_at_node:
                    raise AvailabilityError(
                        f"Строка {reader.line_num}: точка готовности указывается только для available."
                    )
            row = {"source_row": reader.line_num, "robot_slot": slot,
                   "start_at_utc": start.isoformat(), "end_at_utc": end.isoformat(),
                   "state": state}
            if parser_version == PARSER_VERSION:
                row["ready_at_node"] = ready_at_node or None
            rows.append(row)
            by_slot[slot].append((start, end, reader.line_num))
    except csv.Error as exc:
        raise AvailabilityError("CSV содержит некорректное экранирование или структуру.") from exc
    for slot, intervals in by_slot.items():
        intervals.sort()
        expected_start = period_start
        for start, end, line in intervals:
            if start != expected_start:
                raise AvailabilityError(
                    f"Робот {slot}, строка {line}: календарь должен покрывать период без пробелов и пересечений."
                )
            expected_start = end
        if expected_start != period_end:
            raise AvailabilityError(f"Робот {slot}: календарь не покрывает конец периода.")
    return rows


def parse_availability(raw, *, period_start, period_end, fleet, origin_node):
    return _parse_availability(
        raw, period_start=period_start, period_end=period_end,
        fleet=fleet, parser_version=PARSER_VERSION, origin_node=origin_node,
    )


def available_windows(rows, *, source):
    """Convert attested rows into the event kernel's resource contract."""
    by_slot = {}
    for row in rows:
        slot = row["robot_slot"]
        robot = by_slot.setdefault(slot, {"id": f"slot-{slot}", "windows": []})
        if row["state"] == "available":
            robot["windows"].append({
                "start_at": row["start_at_utc"],
                "end_at": row["end_at_utc"],
                "source": source,
                "source_row": row["source_row"],
            })
    return [by_slot[slot] for slot in sorted(by_slot)]


def verified_availability_rows(plan, *, period_start, period_end, origin_node=None):
    """Reparse the immutable source before it can influence a saved run."""
    if plan.parser_version not in {1, PARSER_VERSION}:
        raise AvailabilityError("Для этой версии календаря нужен соответствующий парсер.")
    raw = bytes(plan.raw_csv)
    if hashlib.sha256(raw).hexdigest() != plan.sha256:
        raise AvailabilityError("Исходный календарь не совпадает с сохранённой контрольной суммой.")
    rows = _parse_availability(
        raw, period_start=period_start, period_end=period_end, fleet=plan.fleet,
        parser_version=plan.parser_version, origin_node=origin_node,
    )
    if rows != plan.rows:
        raise AvailabilityError("Обработанные интервалы расходятся с исходным файлом.")
    return rows
