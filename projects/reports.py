"""Owner-only export renderers for a previously verified run and cash-flow plan."""

import csv
import base64
import hashlib
import io
import json
from decimal import Decimal
from html import escape as xml_escape
from pathlib import Path
from xml.sax.saxutils import escape as paragraph_escape
from zipfile import ZIP_DEFLATED, ZipFile

from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    HRFlowable, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table,
    TableStyle,
)
from reportlab.graphics.shapes import Circle, Drawing, Image as GraphicImage, Line, String
from PIL import Image as PILImage

from projects.finance import HEADER, SCENARIOS
from projects.finance_builder import initial_investment
from projects.models import FloorPlanSource
from projects.playback import measured_scene, movement_timeline, position_on_path, state_before
from projects.selection_refs import selection_key, selection_ref
from projects.sizing import sizing_fields
from projects.simulation_metrics import simulation_metrics
from projects.task_profiles import process_for


FONT_FILE = Path(__file__).resolve().parents[1] / "static" / "fonts" / "DejaVuSans.ttf"
FONT_NAME = "RobotMarket-DejaVu"
EVENT_COLUMNS = ("type", "at_s", "source_row", "robot_id", "work_units", "work_unit")
MONTH_COLUMNS = ("month", "served_work_units", "work_unit", "baseline", "purchase", "raas", "currency", "vat_mode")


def safe_csv_cell(value):
    """Keep spreadsheet apps from evaluating user-authored text as a formula."""
    value = "" if value is None else str(value)
    if value and (value[0] in "=+-@\t\r\n" or value.lstrip().startswith(("=", "+", "-", "@"))):
        return "'" + value
    return value


def _csv_bytes(columns, rows, *, numeric_columns=()):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\r\n")
    writer.writerow(columns)
    for row in rows:
        values = []
        for column in columns:
            value = row.get(column)
            if column in numeric_columns:
                number = Decimal(str(value))
                if not number.is_finite():
                    raise ValueError("Неверное число для выгрузки CSV.")
                values.append(str(value))
            else:
                values.append(safe_csv_cell(value))
        writer.writerow(values)
    return b"\xef\xbb\xbf" + stream.getvalue().encode("utf-8")


def _paragraph(value, style):
    return Paragraph(paragraph_escape(str(value)).replace("\n", "<br/>"), style)


def _font():
    if FONT_NAME not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(FONT_NAME, str(FONT_FILE)))


def _robot_positions(snapshot, sizing, ledger, scene, event_index):
    events = ledger["events"]
    if not 0 <= event_index < len(events):
        raise ValueError("Событие кадра вне сохранённого журнала.")
    moment = Decimal(events[event_index]["at_s"])
    motion = movement_timeline(
        snapshot, sizing, ledger, scene,
        page_start="0", page_end=ledger["events"][-1]["at_s"],
    )
    busy = state_before(events, event_index + 1)["active"]
    robots = []
    if motion["reason"]:
        return robots, motion["reason"]
    for cycle in motion["cycles"]:
        if busy.get(cycle["robot_id"]) != cycle["source_row"]:
            continue
        elapsed = moment - Decimal(cycle["start_s"])
        if elapsed < 0 or moment >= Decimal(cycle["end_s"]):
            continue
        cycle_stages = cycle.get("stages", motion["stages"])
        stage = next((stage for stage in cycle_stages
                      if Decimal(stage["start_s"]) <= elapsed < Decimal(stage["end_s"])), None)
        if stage is None:
            continue
        start, end = stage["from"], stage["to"]
        fraction = ((elapsed - Decimal(stage["start_s"]))
                    / (Decimal(stage["end_s"]) - Decimal(stage["start_s"]))
                    if stage["kind"] == "travel" else Decimal(0))
        x, y = position_on_path(stage, fraction)
        robots.append({
            "id": cycle["robot_id"], "source_row": cycle["source_row"],
            "floor": start["floor"],
            "x": x, "y": y,
        })
    return robots, None


def _svg(scene, robots, reason, moment):
    floors = scene["floors"]
    height = max(180, len(floors) * 440)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 {height}" role="img">',
        '<rect width="100%" height="100%" fill="#f6f8f5"/>',
        '<style>text{font-family:DejaVu Sans,Arial,sans-serif;fill:#162923}'
        '.route{stroke:#2b6049;stroke-width:4}.edge{stroke:#8aa69a;stroke-width:2}'
        '.node{fill:#fff;stroke:#2b6049;stroke-width:2}'
        '.robot{fill:#b9dc44;stroke:#162923;stroke-width:2}</style>',
    ]
    if not floors:
        parts.append('<text x="24" y="55" font-size="19">Координаты маршрута не предоставлены</text>')
    for index, floor in enumerate(floors):
        offset = index * 440
        parts.append(f'<g transform="translate(20 {offset + 16})">')
        parts.append(f'<text x="8" y="24" font-size="20">{xml_escape(str(floor["label"]), quote=True)}</text>')
        parts.append('<rect x="0" y="38" width="600" height="380" rx="10" fill="#fff" stroke="#dce8e0"/>')
        parts.append('<g transform="translate(0 38)">')
        raster = floor.get("plan") or {}
        if raster.get("image_png"):
            encoded = base64.b64encode(raster["image_png"]).decode("ascii")
            parts.append(f'<image x="{raster["x"]}" y="{raster["y"]}" '
                         f'width="{raster["width"]}" height="{raster["height"]}" '
                         f'href="data:image/png;base64,{encoded}" opacity="0.70"/>')
        for edge in floor["edges"]:
            style = "route" if edge["on_route"] else "edge"
            parts.append(f'<polyline points="{edge["svg_points"]}" fill="none" class="{style}"/>')
        for node in floor["nodes"]:
            label = xml_escape(str(node["label"]), quote=True)
            parts.append(f'<circle cx="{node["x"]}" cy="{node["y"]}" r="5" class="node"/>')
            parts.append(f'<text x="{node["x"]}" y="{node["y"]}" dx="9" dy="-9" font-size="12">{label}</text>')
        for robot in robots:
            if robot["floor"] == floor["label"]:
                label = xml_escape(str(robot["id"]), quote=True)
                parts.append(f'<circle cx="{robot["x"]}" cy="{robot["y"]}" r="10" class="robot"/>')
                parts.append(f'<text x="{robot["x"]}" y="{robot["y"]}" dx="14" dy="4" font-size="12">{label}</text>')
        parts.append('</g></g>')
    label = reason or "Схематический кадр по измеренным точкам и сохранённому событию"
    parts.append(f'<desc>{xml_escape(str(label), quote=True)}; время {moment} с.</desc></svg>')
    return "".join(parts).encode("utf-8")


def _map_drawing(floor, robots):
    drawing = Drawing(510, 340)
    scale = Decimal("0.85")
    raster = floor.get("plan") or {}
    if raster.get("image_png"):
        drawing.add(GraphicImage(
            float(Decimal(raster["x"]) * scale),
            float((Decimal(400) - Decimal(raster["y"]) - Decimal(raster["height"])) * scale),
            float(Decimal(raster["width"]) * scale),
            float(Decimal(raster["height"]) * scale),
            PILImage.open(io.BytesIO(raster["image_png"])),
        ))
    for edge in floor["edges"]:
        points = edge["path"]
        for start, end in zip(points, points[1:]):
            drawing.add(Line(float(Decimal(start["x"]) * scale),
                             float((Decimal(400) - Decimal(start["y"])) * scale),
                             float(Decimal(end["x"]) * scale),
                             float((Decimal(400) - Decimal(end["y"])) * scale),
                             strokeColor=colors.HexColor("#2b6049" if edge["on_route"] else "#a8c0b3"),
                             strokeWidth=2 if edge["on_route"] else 1))
    for node in floor["nodes"]:
        x, y = float(Decimal(node["x"]) * scale), float((Decimal(400) - Decimal(node["y"])) * scale)
        drawing.add(Circle(x, y, 4, fillColor=colors.white,
                           strokeColor=colors.HexColor("#2b6049")))
    for robot in robots:
        if robot["floor"] == floor["label"]:
            x, y = float(robot["x"] * scale), float((Decimal(400) - robot["y"]) * scale)
            drawing.add(Circle(x, y, 9, fillColor=colors.HexColor("#b9dc44"),
                               strokeColor=colors.HexColor("#162923")))
            drawing.add(String(x + 12, y + 2, str(robot["id"]), fontName=FONT_NAME,
                               fontSize=8, fillColor=colors.HexColor("#162923")))
    return drawing


def _display_number(value):
    return "не определено" if value is None else f"{Decimal(str(value)):,.2f}".replace(",", " ")


def _pdf(project, run, plan, result, rows, scene, robots, reason, moment, variant, metrics):
    _font()
    stream = io.BytesIO()
    doc = SimpleDocTemplate(stream, pagesize=A4, leftMargin=20 * mm,
                            rightMargin=20 * mm, topMargin=19 * mm, bottomMargin=19 * mm)
    ink = colors.HexColor("#162923")
    subtle = colors.HexColor("#5c7067")
    styles = {
        "title": ParagraphStyle("RMTitle", fontName=FONT_NAME, fontSize=19, leading=24, textColor=ink, spaceAfter=8),
        "h2": ParagraphStyle("RMH2", fontName=FONT_NAME, fontSize=12, leading=16, textColor=ink, spaceBefore=10, spaceAfter=6),
        "body": ParagraphStyle("RMBody", fontName=FONT_NAME, fontSize=8.5, leading=12, textColor=ink, spaceAfter=5),
        "small": ParagraphStyle("RMSmall", fontName=FONT_NAME, fontSize=7, leading=10, textColor=subtle, spaceAfter=2),
        "table": ParagraphStyle("RMTable", fontName=FONT_NAME, fontSize=7, leading=9,
                                textColor=ink, splitLongWords=True),
        "table_header": ParagraphStyle("RMTableHeader", fontName=FONT_NAME, fontSize=7,
                                       leading=9, textColor=colors.white),
    }
    story = [_paragraph("Результаты проекта", styles["title"]),
             _paragraph(project.name, styles["h2"]),
             _paragraph(f"{project.get_object_slug_display()} · ревизия {run.revision.number} · прогон {run.id}", styles["small"]),
             HRFlowable(width="100%", thickness=1, color=colors.HexColor("#dce8e0")),
             Spacer(1, 5 * mm)]
    scenario = run.input_snapshot["scenario"]
    selected = scenario.get("robot_selection") or {}
    robot_ref = selection_ref(selected, scenario)
    process = process_for(project.object_slug, scenario["task_profile"]["process"])
    parameters = (scenario.get("workload_profile") or {}).get("parameters") or {}
    if (process.code in {"airport_baggage_transport", "hospital_meal_delivery"}
            and scenario["task_profile"].get("version") != 3):
        story.append(_paragraph(
            "Исторический расчёт по прежнему паспорту операции. Для нового решения обновите паспорт и выбор модели.",
            styles["body"],
        ))
    story.extend([
        _paragraph("Наблюдённая операция", styles["h2"]),
        _paragraph(f"Модель: {selected.get('family_name') or selected.get('record_index') or 'Не указана'}", styles["body"]),
        _paragraph(f"Прогон сохранён: {timezone.localtime(run.created_at).strftime('%d.%m.%Y %H:%M %Z')}; "
                   f"денежный план: {timezone.localtime(plan.created_at).strftime('%d.%m.%Y %H:%M %Z')}",
                   styles["small"]),
        _paragraph(f"Период: {run.ledger['period_start_utc']} - {run.ledger['period_end_utc']}", styles["body"]),
        _paragraph(f"Поступило {run.ledger['arrivals']} · передано {run.ledger['delivered']} · "
                   f"завершено {run.ledger['completed']} · без передачи {run.ledger['unmet_at_end']} · "
                   f"максимальная очередь {run.ledger['max_queue']}", styles["body"]),
        _paragraph(f"Переданный объём: {run.ledger['delivered_work_units']} "
                   f"{run.operation_log.rows[0]['unit']} · "
                   f"парк по расчёту: {run.input_snapshot['sizing']['fleet']}", styles["body"]),
        _paragraph("Параметры расчёта парка", styles["h2"]),
    ])
    for key, label, unit in sizing_fields(process):
        parameter = parameters.get(key)
        if parameter:
            story.append(_paragraph(
                f"{label}: {parameter['value']} {parameter.get('unit') or unit} · "
                f"источник: {parameter['source']}", styles["small"],
            ))
    story.extend([
        _paragraph(f"Версия расчёта парка: {run.input_snapshot['sizing']['version']}", styles["small"]),
        _paragraph("Показатели симуляции", styles["h2"]),
        _paragraph(
            f"Переданный объём в час: {_display_number(metrics['throughput']['delivered_work_units_per_hour'])}; "
            f"средняя очередь: {_display_number(metrics['queue']['mean_jobs'])} заданий; "
            f"среднее ожидание начатых заданий: {_display_number(metrics['waiting']['started_jobs_mean_seconds'])} с; "
            f"95-й процентиль ожидания: {_display_number(metrics['waiting']['started_jobs_p95_seconds'])} с.", styles["body"]),
        _paragraph(
            f"Незавершённая очередь: {metrics['waiting']['unfinished_waiting_jobs']} заданий; "
            f"их суммарное наблюдаемое ожидание: {_display_number(metrics['waiting']['unfinished_waiting_censored_seconds'])} с. "
            f"Занятое время парка: {_display_number(metrics['fleet']['busy_robot_seconds'])} робот-с; "
            f"доступное время: {_display_number(metrics['fleet']['available_robot_seconds'])} робот-с.", styles["body"]),
        *([_paragraph(
            f"Подтверждённые общие участки: ожидание {_display_number(metrics['resources']['total_wait_seconds'])} "
            f"робот-с; резервирований {metrics['resources']['reservations']}. "
            "Время передачи и итоговый объём рассчитаны с учётом этих ожиданий.",
            styles["body"])] if "resources" in metrics else []),
        _paragraph("Денежный план", styles["h2"]),
        _paragraph(f"Горизонт: {plan.horizon_months} мес. · валюта: {rows[0]['currency']} · "
                   f"НДС: {'с НДС' if rows[0]['vat_mode'] == 'gross' else 'без НДС'} · "
                   f"месячная ставка дисконтирования: {result['monthly_discount_rate']}", styles["body"]),
        _paragraph("Наблюдаемый объём относится только к указанному периоду. Будущие месячные объёмы "
                   "и суммы являются утверждённым планом организации.", styles["small"]),
    ])
    investments = initial_investment(rows)
    summary = [["Сценарий", "TCO", "CAPEX", "OPEX", "NPV к baseline"]]
    for name, label in (("baseline", "Действующий процесс"),
                        ("purchase", "Покупка"), ("raas", "RaaS")):
        item = result["scenarios"][name]
        summary.append([label, _display_number(item["tco"]), _display_number(item["capex"]), _display_number(item["opex"]),
                        _display_number(item["npv_vs_baseline"]) if name != "baseline" else "-"])
    table = Table([[_paragraph(cell, styles["table_header"] if index == 0 else styles["table"])
                    for cell in row] for index, row in enumerate(summary)],
                  colWidths=[47 * mm, 28 * mm, 27 * mm, 27 * mm, 41 * mm], repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), ink),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7), ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("LINEBELOW", (0, 1), (-1, -1), 0.3, colors.HexColor("#dce8e0")),
    ]))
    roi = result["scenarios"]["purchase"]["roi_pct"]
    roi_label = f"{_display_number(roi)}%" if roi is not None else "не применяется"
    payback = result["scenarios"]["purchase"]["payback_month"]
    story.extend([table, _paragraph(
        "TCI — все исходящие вложения месяца 0: " + "; ".join(
            f"{label}: {_display_number(investments[name])} {rows[0]['currency']}" for name, label in
            (("baseline", "действующий процесс"), ("purchase", "покупка"), ("raas", "RaaS")))
        + ". Включает оборотный капитал; CAPEX за горизонт показан отдельно.", styles["small"]),
        _paragraph(f"Покупка: ROI {roi_label}; "
                                     f"окупаемость: {f'месяц {payback}' if payback is not None else 'не достигнута'}",
                                     styles["small"]),
                  _paragraph("Источники и версии", styles["h2"]),
                  _paragraph(f"Журнал: {run.operation_log.source_description}; SHA-256 {run.operation_log.sha256}", styles["small"]),
                  _paragraph(f"Календарь: {run.availability_plan.source_description}; SHA-256 {run.availability_plan.sha256}", styles["small"]),
                  _paragraph(f"Денежный план: {plan.source_description}; SHA-256 {plan.sha256}", styles["small"]),
                  _paragraph(f"Источник прогноза: {plan.forecast_basis}; ставка: {plan.discount_rate_source}", styles["small"]),
                  _paragraph(f"Версии: событий {run.ledger_version}; денежной модели {result['model_version']}; "
                             f"каталог {scenario.get('catalog_checksum') or 'не указан'}; "
                             f"сведения {scenario.get('evidence_checksum') or 'не указаны'}", styles["small"]),
                  _paragraph(f"Вариант: строка {variant.source_row}, источник {variant.source_ref}"
                             if variant else "Исходный утверждённый денежный план", styles["small"]),
                  _paragraph("Кадр маршрута", styles["h2"]),
                  _paragraph(f"Событие на {moment} с. Схема соединяет измеренные точки участками "
                             "или заданными контрольными поворотами; радиусы поворота, препятствия "
                             "и безопасный габарит не верифицированы.", styles["small"])])
    if robot_ref and robot_ref["catalog_source_kind"] == "manufacturer_supplement":
        story.insert(-3, _paragraph(
            f"Модель производителя: {selected.get('product_url') or 'источник не указан'}; "
            f"версия сведений {robot_ref['catalog_source_checksum']}", styles["small"],
        ))
    if reason:
        story.append(_paragraph(reason, styles["body"]))
    if not scene["floors"]:
        story.append(_paragraph("Координаты маршрута не предоставлены.", styles["body"]))
    for floor in scene["floors"]:
        story.append(KeepTogether([_paragraph(f"Этаж: {floor['label']}", styles["h2"]),
                                   _map_drawing(floor, robots)]))
    story.append(_paragraph("Подробные события, денежные строки и помесячные итоги находятся "
                            "в CSV-файлах архива. Манифест содержит точные ссылки на сохранённые входы.", styles["small"]))

    def footer(canvas, document):
        canvas.saveState()
        canvas.setFont(FONT_NAME, 7)
        canvas.setFillColor(subtle)
        canvas.drawString(20 * mm, 11 * mm, f"Проект {project.id} · прогон {run.id}")
        canvas.drawRightString(A4[0] - 20 * mm, 11 * mm, str(document.page))
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return stream.getvalue()


def build_report_bundle(project, run, plan, result, finance_rows, *, event_index, variant=None):
    """Render a single verified input pair; verification belongs to the view."""
    ledger = run.ledger
    scenario = run.input_snapshot["scenario"]
    robot_ref = selection_ref(scenario.get("robot_selection"), scenario)
    scene = measured_scene(run.input_snapshot["scenario"])
    floor_plan_assets = {}
    exported_plans = []
    for index, floor in enumerate(scene["floors"]):
        raster = floor.get("plan")
        if not raster:
            continue
        source = FloorPlanSource.objects.filter(
            id=raster["id"], project=project, floor=floor["label"],
            png_sha256=raster["png_sha256"],
        ).first()
        if source is None:
            raise ValueError("Закреплённый план этажа отсутствует или не совпадает с ревизией.")
        raw = bytes(source.image_png)
        if hashlib.sha256(raw).hexdigest() != raster["png_sha256"]:
            raise ValueError("Закреплённый план этажа повреждён.")
        archive_name = f"floor_plans/floor_{index + 1:02d}_{source.png_sha256[:12]}.png"
        raster["image_png"] = raw
        floor_plan_assets[archive_name] = raw
        exported_plans.append({
            "floor": floor["label"], "archive_name": archive_name,
            "png_sha256": source.png_sha256,
            "original_sha256": source.original_sha256,
            "source_description": source.source_description,
            "calibration": scenario["topology_profile"]["floor_plans"][floor["label"]],
        })
    event = ledger["events"][event_index]
    moment = event["at_s"]
    robots, reason = _robot_positions(
        run.input_snapshot["scenario"], run.input_snapshot["sizing"],
        ledger, scene, event_index,
    )
    work_units = {row["unit"] for row in run.operation_log.rows}
    if len(work_units) != 1:
        raise ValueError("В журнале операции не определена единица объёма.")
    work_unit = work_units.pop()
    currency = finance_rows[0]["currency"]
    vat_mode = finance_rows[0]["vat_mode"]
    monthly = {row["month"]: row["served_work_units"]
               for row in finance_rows if row["scenario"] == "baseline"}
    detailed_rows = [{**row, "included_scopes": "|".join(row["included_scopes"]),
                      "work_unit": work_unit}
                     for row in finance_rows]
    month_rows = [
        {"month": month, "served_work_units": monthly[month], "work_unit": work_unit,
         "currency": currency, "vat_mode": vat_mode,
         **{name: result["scenarios"][name]["monthly_cash_cost"][month]
            for name in SCENARIOS}}
        for month in range(result["horizon_months"] + 1)
    ]
    metrics = simulation_metrics(ledger, run.availability_plan.rows)
    manifest = {
        "simulation_metrics": metrics,
        "initial_investment": {key: str(value) for key, value in initial_investment(finance_rows).items()},
        "project_id": str(project.id), "object_type": project.object_slug,
        "revision": run.revision.number, "run_id": str(run.id),
        "run_created_at": timezone.localtime(run.created_at).isoformat(),
        "finance_plan_id": str(plan.id), "finance_created_at": timezone.localtime(plan.created_at).isoformat(),
        "variant_id": str(variant.id) if variant else None,
        "variant_checksum": variant.checksum if variant else None,
        "event_index": event_index, "frame_at_s": moment,
        "work_unit": work_unit, "currency": currency, "vat_mode": vat_mode,
        "catalog_sha256": run.input_snapshot["scenario"].get("catalog_checksum"),
        "evidence_sha256": run.input_snapshot["scenario"].get("evidence_checksum"),
        "robot_selection_ref": robot_ref,
        "robot_selection_key": selection_key(robot_ref),
        "supplement_sha256": scenario.get("supplement_checksum"),
        "operation_log_sha256": run.operation_log.sha256,
        "operation_log_parser_version": run.operation_log.parser_version,
        "availability_sha256": run.availability_plan.sha256,
        "availability_parser_version": run.availability_plan.parser_version,
        "ledger_version": run.ledger_version, "finance_sha256": plan.sha256,
        "finance_parser_version": plan.parser_version,
        "finance_model_version": result["model_version"],
        "sizing_model_version": run.input_snapshot["sizing"]["version"],
        "operation_source": run.operation_log.source_description,
        "availability_source": run.availability_plan.source_description,
        "finance_source": plan.source_description,
        "forecast_basis": plan.forecast_basis,
        "discount_rate_source": plan.discount_rate_source,
        "frame_note": reason or "Схема по измеренным точкам",
        "floor_plan_sources": exported_plans,
        "scenario_snapshot": run.input_snapshot["scenario"],
        "sizing": run.input_snapshot["sizing"],
    }
    if run.ledger_version == 3:
        manifest["resource_plan"] = run.input_snapshot["resource_plan"]
        manifest["resource_wait_seconds"] = ledger["resource_wait_seconds"]
        manifest["resource_reservation_count"] = len(ledger["resource_reservations"])
    files = {
        "report.pdf": _pdf(project, run, plan, result, finance_rows, scene, robots, reason, moment, variant, metrics),
        "simulation_metrics.csv": _csv_bytes(("metric", "value"), [
            {"metric": f"{section}.{key}", "value": value}
            for section, entries in metrics.items() if isinstance(entries, dict)
            for key, value in entries.items()
        ]),
        "events.csv": _csv_bytes(EVENT_COLUMNS,
                                  [{**event, "work_unit": work_unit} for event in ledger["events"]],
                                  numeric_columns={"at_s", "source_row", "work_units"}),
        "financial_rows.csv": _csv_bytes(("source_row", *HEADER, "work_unit"), detailed_rows,
                                          numeric_columns={"source_row", "month", "amount", "served_work_units"}),
        "monthly_totals.csv": _csv_bytes(MONTH_COLUMNS, month_rows,
                                         numeric_columns={"month", "served_work_units", "baseline", "purchase", "raas"}),
        "frame.svg": _svg(scene, robots, reason, moment),
        "manifest.json": json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8"),
    }
    files.update(floor_plan_assets)
    if run.ledger_version == 3:
        files["resource_reservations.csv"] = _csv_bytes(
            ("resource_id", "direction", "start_s", "end_s", "source_row", "robot_id"),
            ledger["resource_reservations"],
            numeric_columns={"start_s", "end_s", "source_row"},
        )
        files["motion_cycles.json"] = json.dumps(
            ledger["motion_cycles"], ensure_ascii=False, sort_keys=True, indent=2,
        ).encode("utf-8")
    archive = io.BytesIO()
    with ZipFile(archive, "w", compression=ZIP_DEFLATED, compresslevel=6) as output:
        for name, data in files.items():
            output.writestr(name, data)
    return archive.getvalue()
