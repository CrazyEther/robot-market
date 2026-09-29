"""Strict ingestion of owner-attested, identifier-free operation arrivals."""

import csv
import hashlib
import io
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from projects.sizing import is_transport


PARSER_VERSION = 1
MAX_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_ROWS = 10_000


class OperationLogError(ValueError):
    pass


def parse_operation_log(raw, process):
    if not raw or len(raw) > MAX_UPLOAD_BYTES:
        raise OperationLogError("Файл пуст или превышает допустимый размер 2 МиБ.")
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise OperationLogError("Сохраните CSV в кодировке UTF-8.") from exc
    if "\x00" in content:
        raise OperationLogError("Файл содержит недопустимые байты.")
    reader = None
    for delimiter in (",", ";"):
        candidate = csv.reader(io.StringIO(content, newline=""), delimiter=delimiter, strict=True)
        try:
            header = [cell.strip() for cell in next(candidate)]
        except (StopIteration, csv.Error):
            continue
        if header == ["requested_at", "work_units"]:
            reader = candidate
            break
    if reader is None:
        raise OperationLogError("Нужны ровно две колонки: requested_at,work_units. Личные идентификаторы не загружайте.")
    try:
        rows = []
        previous_time = None
        for cells in reader:
            if len(rows) >= MAX_ROWS:
                raise OperationLogError("Журнал превышает предел 10 000 записей.")
            if len(cells) != 2:
                raise OperationLogError(f"Строка {reader.line_num}: ожидаются две колонки.")
            try:
                requested_at = datetime.fromisoformat(cells[0].strip())
            except ValueError as exc:
                raise OperationLogError(f"Строка {reader.line_num}: неверное время поступления задания.") from exc
            if requested_at.utcoffset() is None:
                raise OperationLogError(f"Строка {reader.line_num}: укажите часовой пояс в ISO 8601.")
            requested_at = requested_at.astimezone(timezone.utc)
            if previous_time is not None and requested_at < previous_time:
                raise OperationLogError(f"Строка {reader.line_num}: задания должны идти по времени поступления.")
            previous_time = requested_at
            try:
                units = Decimal(cells[1].strip())
            except InvalidOperation as exc:
                raise OperationLogError(f"Строка {reader.line_num}: неверный объём задания.") from exc
            if not units.is_finite() or units <= 0:
                raise OperationLogError(f"Строка {reader.line_num}: объём задания должен быть положительным конечным числом.")
            if (len(units.as_tuple().digits) > 15 or units.adjusted() > 11
                    or units.as_tuple().exponent < -3):
                raise OperationLogError(f"Строка {reader.line_num}: превышена точность объёма задания (12 целых и 3 дробных знака).")
            if is_transport(process) and units != 1:
                raise OperationLogError(f"Строка {reader.line_num}: один транспортный заказ должен содержать 1 рейс.")
            rows.append({
                "source_row": reader.line_num,
                "requested_at_utc": requested_at.isoformat(),
                "work_units": str(units),
                "unit": "рейс" if is_transport(process) else "м²",
            })
    except csv.Error as exc:
        raise OperationLogError("CSV содержит некорректное экранирование или структуру.") from exc
    if not rows:
        raise OperationLogError("Журнал не содержит заданий.")
    return rows


def validate_observation_period(rows, start, end):
    """Use a half-open observed interval, never the last arrival as its end."""
    for row in rows:
        requested_at = datetime.fromisoformat(row["requested_at_utc"])
        if not start <= requested_at < end:
            raise OperationLogError(
                f"Строка {row['source_row']}: время задания вне указанного периода наблюдения."
            )


def verified_operation_rows(log, process):
    """Reject changed raw bytes or derived rows before any calculation."""
    if log.parser_version != PARSER_VERSION:
        raise OperationLogError("Для этой версии журнала нужен соответствующий парсер.")
    raw = bytes(log.raw_csv)
    if hashlib.sha256(raw).hexdigest() != log.sha256:
        raise OperationLogError("Исходный журнал не совпадает с сохранённой контрольной суммой.")
    rows = parse_operation_log(raw, process)
    if rows != log.rows:
        raise OperationLogError("Обработанные строки журнала расходятся с исходным файлом.")
    if log.period_start_at is None or log.period_end_at is None:
        raise OperationLogError("У журнала не указан период наблюдения.")
    validate_observation_period(rows, log.period_start_at, log.period_end_at)
    return rows
