"""Source-bound, comparable monthly cash flows for an observed robot scenario."""

import csv
import hashlib
import io
import json
import re
from copy import deepcopy
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation

from projects.operation_logs import MAX_UPLOAD_BYTES, MAX_ROWS


FINANCE_VERSION = 1
SCENARIOS = ("baseline", "purchase", "raas")
DIRECTIONS = frozenset({"outflow", "inflow"})
CASH_CLASSES = frozenset({"capex", "opex", "other_cash"})
HEADER = [
    "scenario", "month", "scope", "direction", "cash_class", "amount", "currency",
    "vat_mode", "source_date", "source_ref", "served_work_units",
    "volume_source_ref", "included_scopes",
]
SCOPE_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
MAX_HORIZON_MONTHS = 600


class FinanceInputError(ValueError):
    pass


def _number(raw, *, line, label):
    if not isinstance(raw, str) or not re.fullmatch(r"[0-9]{1,18}(?:\.[0-9]{1,4})?", raw.strip()):
        raise FinanceInputError(f"Строка {line}: неверное число в {label}.")
    try:
        value = Decimal(raw.strip())
    except (InvalidOperation, AttributeError) as exc:
        raise FinanceInputError(f"Строка {line}: неверное число в {label}.") from exc
    if not value.is_finite() or value < 0:
        raise FinanceInputError(f"Строка {line}: недопустимое число в {label}.")
    return value


def finance_metadata_sha256(*, source_description, forecast_basis, discount_rate_source,
                            source_filename=""):
    metadata = {
        "source_description": source_description,
        "forecast_basis": forecast_basis,
        "discount_rate_source": discount_rate_source,
    }
    if source_filename:
        metadata["source_filename"] = source_filename
    return hashlib.sha256(json.dumps(
        metadata, sort_keys=True, ensure_ascii=False,
    ).encode("utf-8")).hexdigest()


def parse_finance_csv(raw, *, horizon_months):
    """Require an explicit amount and source for every scenario and month.

    The CSV represents customer-approved cash amounts, not guessed equipment
    prices or automatically monetized labour time. Month zero is investment.
    """
    if (not isinstance(horizon_months, int) or isinstance(horizon_months, bool)
            or not 60 <= horizon_months <= MAX_HORIZON_MONTHS):
        raise FinanceInputError("Горизонт должен составлять от 60 до 600 месяцев.")
    if not raw or len(raw) > MAX_UPLOAD_BYTES:
        raise FinanceInputError("Файл пуст или превышает 2 МиБ.")
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise FinanceInputError("Сохраните CSV в кодировке UTF-8.") from exc
    if "\x00" in content:
        raise FinanceInputError("Файл содержит недопустимые байты.")
    reader = None
    for delimiter in (",", ";"):
        candidate = csv.reader(io.StringIO(content, newline=""), delimiter=delimiter, strict=True)
        try:
            header = [cell.strip() for cell in next(candidate)]
        except (StopIteration, csv.Error):
            continue
        if header == HEADER:
            reader = candidate
            break
    if reader is None:
        raise FinanceInputError(f"Нужны ровно колонки {','.join(HEADER)}.")

    rows = []
    observed = set()
    coverage = set()
    volumes = {}
    currencies = set()
    vat_modes = set()
    raas_included = defaultdict(set)
    raas_extra = defaultdict(set)
    try:
        for cells in reader:
            line = reader.line_num
            if len(rows) >= MAX_ROWS:
                raise FinanceInputError("План превышает 10 000 строк.")
            if len(cells) != len(HEADER):
                raise FinanceInputError(f"Строка {line}: неверное число колонок.")
            (scenario, month_raw, scope, direction, cash_class, amount_raw, currency,
             vat_mode, source_date, source_ref, units_raw, volume_source_ref,
             included_raw) = [cell.strip() for cell in cells]
            if scenario not in SCENARIOS:
                raise FinanceInputError(f"Строка {line}: неизвестный сценарий.")
            if not re.fullmatch(r"[0-9]{1,3}", month_raw) or int(month_raw) > horizon_months:
                raise FinanceInputError(f"Строка {line}: месяц вне горизонта.")
            month = int(month_raw)
            if not SCOPE_PATTERN.fullmatch(scope):
                raise FinanceInputError(f"Строка {line}: неверный код статьи затрат.")
            if direction not in DIRECTIONS:
                raise FinanceInputError(f"Строка {line}: направление должно быть outflow или inflow.")
            if cash_class not in CASH_CLASSES or (direction == "inflow" and cash_class != "other_cash"):
                raise FinanceInputError(f"Строка {line}: неверная классификация денежного потока.")
            if scope == "robot_equipment" and (
                scenario != "purchase" or direction != "outflow" or cash_class != "capex"
            ):
                raise FinanceInputError(f"Строка {line}: покупка робота относится к CAPEX сценария purchase.")
            if scope == "raas_subscription" and (
                scenario != "raas" or direction != "outflow" or cash_class != "opex"
            ):
                raise FinanceInputError(f"Строка {line}: подписка RaaS относится к OPEX сценария raas.")
            amount = _number(amount_raw, line=line, label="amount")
            units = _number(units_raw, line=line, label="served_work_units")
            if month == 0 and units != 0:
                raise FinanceInputError(f"Строка {line}: в месяце 0 объём услуг должен быть нулевым.")
            if not re.fullmatch(r"[A-Z]{3}", currency) or currency == "XXX":
                raise FinanceInputError(f"Строка {line}: укажите трёхбуквенный код валюты из источника.")
            if vat_mode not in {"gross", "net"}:
                raise FinanceInputError(f"Строка {line}: укажите единый режим НДС gross или net.")
            if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", source_date):
                raise FinanceInputError(f"Строка {line}: неверная дата источника.")
            try:
                date.fromisoformat(source_date)
            except ValueError as exc:
                raise FinanceInputError(f"Строка {line}: неверная дата источника.") from exc
            if not source_ref or len(source_ref) > 1000 or not volume_source_ref or len(volume_source_ref) > 1000:
                raise FinanceInputError(f"Строка {line}: требуются источники суммы и объёма.")
            included = [value.strip() for value in included_raw.split("|") if value.strip()]
            if any(not SCOPE_PATTERN.fullmatch(value) for value in included) or len(included) != len(set(included)):
                raise FinanceInputError(f"Строка {line}: неверный состав услуг RaaS.")
            if included and (scenario != "raas" or scope != "raas_subscription" or direction != "outflow"):
                raise FinanceInputError(f"Строка {line}: состав услуг допустим только у подписки RaaS.")
            key = (scenario, month, scope)
            if key in observed:
                raise FinanceInputError(f"Строка {line}: статья повторяется в том же сценарии и месяце.")
            observed.add(key)
            coverage.add((scenario, month))
            volume_key = (month, units, volume_source_ref)
            if month in volumes and volumes[month] != volume_key:
                raise FinanceInputError(f"Строка {line}: сценарии используют разный обслуженный объём.")
            volumes[month] = volume_key
            currencies.add(currency)
            vat_modes.add(vat_mode)
            if scenario == "raas":
                raas_included[month].update(included)
                if scope != "raas_subscription" and direction == "outflow":
                    raas_extra[month].add(scope)
            rows.append({
                "source_row": line, "scenario": scenario, "month": month,
                "scope": scope, "direction": direction, "cash_class": cash_class,
                "amount": str(amount),
                "currency": currency, "vat_mode": vat_mode,
                "source_date": source_date, "source_ref": source_ref,
                "served_work_units": str(units),
                "volume_source_ref": volume_source_ref,
                "included_scopes": included,
            })
    except csv.Error as exc:
        raise FinanceInputError("CSV содержит некорректное экранирование или структуру.") from exc

    expected = {(scenario, month) for scenario in SCENARIOS for month in range(horizon_months + 1)}
    if coverage != expected:
        raise FinanceInputError("Для каждого сценария и месяца нужна подтверждённая денежная строка, включая явный ноль.")
    if len(currencies) != 1 or len(vat_modes) != 1:
        raise FinanceInputError("Все сценарии должны использовать одну валюту и один режим НДС.")
    if any(raas_included[month] & raas_extra[month] for month in range(horizon_months + 1)):
        raise FinanceInputError("Услуга уже входит в подписку RaaS и не может оплачиваться повторно.")
    return rows


def validate_forecast_anchor(rows, *, observed_delivered_work_units):
    """Refuse a positive five-year plan from a run that delivered nothing."""
    observed = _number(str(observed_delivered_work_units), line=0, label="наблюдаемый объём")
    if observed == 0 and any(
        _number(row["served_work_units"], line=row["source_row"], label="served_work_units") > 0
        for row in rows
    ):
        raise FinanceInputError(
            "Прогон не подтвердил доставку; положительный финансовый объём не обоснован."
        )


def calculate_finance(rows, *, horizon_months, monthly_discount_rate):
    """Compute signed cash flows and baseline deltas from validated rows."""
    try:
        rate = Decimal(str(monthly_discount_rate))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise FinanceInputError("Укажите месячную ставку дисконтирования.") from exc
    if not rate.is_finite() or rate < 0 or rate > 1:
        raise FinanceInputError("Месячная ставка дисконтирования должна быть от 0 до 1.")
    rate = rate.normalize()
    if not 60 <= horizon_months <= MAX_HORIZON_MONTHS:
        raise FinanceInputError("Горизонт должен составлять от 60 до 600 месяцев.")
    cash = {scenario: [Decimal(0)] * (horizon_months + 1) for scenario in SCENARIOS}
    by_class = {scenario: {cash_class: Decimal(0) for cash_class in CASH_CLASSES}
                for scenario in SCENARIOS}
    for row in rows:
        scenario, month = row["scenario"], row["month"]
        if (scenario not in cash or not isinstance(month, int) or isinstance(month, bool)
                or not 0 <= month <= horizon_months):
            raise FinanceInputError("Финансовый снимок содержит неизвестный сценарий или месяц.")
        amount = _number(row["amount"], line=row["source_row"], label="amount")
        signed = amount if row["direction"] == "outflow" else -amount
        cash[scenario][month] += signed
        by_class[scenario][row["cash_class"]] += signed
    baseline = cash["baseline"]
    result = {}
    for scenario in SCENARIOS:
        flow = cash[scenario]
        comparison = [before - after for before, after in zip(baseline, flow)]
        cumulative = Decimal(0)
        payback = None
        initial_investment = flow[0] - baseline[0]
        for month, benefit in enumerate(comparison):
            cumulative += benefit
            if scenario != "baseline" and month > 0 and initial_investment > 0 and cumulative >= 0 and payback is None:
                payback = month
        npv = sum((benefit / (Decimal(1) + rate) ** month
                   for month, benefit in enumerate(comparison)), Decimal(0))
        roi = (cumulative / initial_investment * 100
               if scenario == "purchase" and initial_investment > 0 else None)
        result[scenario] = {
            "monthly_cash_cost": [str(value) for value in flow],
            "tco": str(sum(flow, Decimal(0))),
            "capex": str(by_class[scenario]["capex"]),
            "opex": str(by_class[scenario]["opex"]),
            "other_cash": str(by_class[scenario]["other_cash"]),
            "net_effect_vs_baseline": str(cumulative),
            "npv_vs_baseline": str(npv),
            "payback_month": payback,
            "roi_pct": str(roi) if roi is not None else None,
        }
    return {"model_version": FINANCE_VERSION, "horizon_months": horizon_months,
            "monthly_discount_rate": str(rate), "scenarios": result}


def verified_finance_result(plan):
    """Reparse immutable raw terms before displaying a saved calculation."""
    if plan.parser_version != FINANCE_VERSION:
        raise FinanceInputError("Для этой версии финансового плана нужен соответствующий парсер.")
    raw = bytes(plan.raw_csv)
    if hashlib.sha256(raw).hexdigest() != plan.sha256:
        raise FinanceInputError("Исходный финансовый план не совпадает с сохранённой контрольной суммой.")
    metadata_sha = finance_metadata_sha256(
        source_description=plan.source_description,
        forecast_basis=plan.forecast_basis,
        discount_rate_source=plan.discount_rate_source,
        source_filename=plan.source_filename,
    )
    if metadata_sha != plan.metadata_sha256:
        raise FinanceInputError("Источники финансового плана расходятся с сохранённой контрольной суммой.")
    rows = parse_finance_csv(raw, horizon_months=plan.horizon_months)
    if rows != plan.rows:
        raise FinanceInputError("Обработанные финансовые строки расходятся с исходным файлом.")
    calculated = calculate_finance(
        rows, horizon_months=plan.horizon_months,
        monthly_discount_rate=plan.monthly_discount_rate,
    )
    if calculated != plan.result:
        raise FinanceInputError("Сохранённый финансовый результат расходится с исходными строками.")
    return calculated


def derive_finance_variant(plan, *, source_row, amount, source_date, source_ref):
    """Recalculate one sourced cash line without mutating the imported plan."""
    verified_finance_result(plan)
    if not isinstance(source_row, int) or isinstance(source_row, bool):
        raise FinanceInputError("Выберите строку денежного плана.")
    original = next((row for row in plan.rows if row["source_row"] == source_row), None)
    if original is None:
        raise FinanceInputError("Строка не принадлежит выбранному финансовому плану.")
    new_amount = _number(str(amount), line=source_row, label="сумма").quantize(Decimal("0.0001"))
    if new_amount == Decimal(original["amount"]):
        raise FinanceInputError("Укажите сумму, отличающуюся от исходной.")
    if not isinstance(source_ref, str) or not source_ref.strip() or len(source_ref.strip()) > 1000:
        raise FinanceInputError("Укажите источник новой суммы.")
    if not isinstance(source_date, date):
        raise FinanceInputError("Укажите дату источника новой суммы.")
    rows = deepcopy(plan.rows)
    changed = next(row for row in rows if row["source_row"] == source_row)
    changed.update(amount=str(new_amount), source_date=source_date.isoformat(),
                   source_ref=source_ref.strip())
    result = calculate_finance(rows, horizon_months=plan.horizon_months,
                               monthly_discount_rate=plan.monthly_discount_rate)
    return rows, result


def finance_variant_checksum(*, plan, source_row, amount, source_date, source_ref):
    payload = {
        "plan_id": str(plan.pk), "plan_sha256": plan.sha256,
        "source_row": source_row,
        "amount": str(_number(str(amount), line=source_row, label="сумма").quantize(Decimal("0.0001"))),
        "source_date": source_date.isoformat(), "source_ref": source_ref,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def verified_finance_variant(variant):
    """Verify both the base import and the immutable one-line amendment."""
    rows, result = derive_finance_variant(
        variant.base_plan, source_row=variant.source_row,
        amount=variant.amount, source_date=variant.source_date,
        source_ref=variant.source_ref,
    )
    checksum = finance_variant_checksum(
        plan=variant.base_plan, source_row=variant.source_row,
        amount=variant.amount, source_date=variant.source_date,
        source_ref=variant.source_ref,
    )
    if checksum != variant.checksum or result != variant.result:
        raise FinanceInputError("Сохранённый вариант расходится с исходным планом и источником изменения.")
    return rows, result
