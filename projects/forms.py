from django import forms
from decimal import Decimal

from projects.task_profiles import PALLET_HANDOFF_MODES, process_for
from projects.sizing import NONNEGATIVE_FIELDS, sizing_fields


class TopologyNodeForm(forms.Form):
    label = forms.CharField(label="Название точки", max_length=100)
    floor = forms.CharField(label="Этаж или уровень", max_length=40, required=False)
    source = forms.CharField(label="План или источник расположения", max_length=500)
    x_m = forms.DecimalField(label="X, м", required=False, max_digits=12, decimal_places=3)
    y_m = forms.DecimalField(label="Y, м", required=False, max_digits=12, decimal_places=3)
    coordinate_source = forms.CharField(label="Источник координат", max_length=500, required=False)
    zone = forms.ChoiceField(label="Зона", required=False,
                             choices=(("", "Не указана"), ("clean", "Чистая"),
                                      ("dirty", "Грязная"), ("neutral", "Нейтральная")))

    def __init__(self, *args, object_slug, **kwargs):
        super().__init__(*args, **kwargs)
        self.object_slug = object_slug
        if object_slug == "airport":
            self.fields["zone"].label = "Режим зоны"
            self.fields["zone"].choices = (("", "Не указан"), ("public", "Общая"),
                                           ("restricted", "Режимная"),
                                           ("excluded", "Закрыта для маршрута"))
        elif object_slug != "hospital":
            self.fields.pop("zone")

    def clean(self):
        values = super().clean()
        if (values.get("x_m") is None) != (values.get("y_m") is None):
            raise forms.ValidationError("Для точки нужны обе измеренные координаты.")
        if values.get("x_m") is not None and not values.get("coordinate_source"):
            self.add_error("coordinate_source", "Укажите источник измеренных координат.")
        if values.get("x_m") is None and values.get("coordinate_source"):
            self.add_error("coordinate_source", "Добавьте обе координаты или удалите источник.")
        return values


class TopologyEdgeForm(forms.Form):
    start = forms.ChoiceField(label="Откуда")
    end = forms.ChoiceField(label="Куда")
    source = forms.CharField(label="План или подтверждение прохода", max_length=500)
    length_m = forms.DecimalField(label="Длина участка, м", required=False,
                                  max_digits=12, decimal_places=3, min_value=Decimal("0.001"))
    length_source = forms.CharField(label="Источник длины", max_length=500, required=False)
    bidirectional = forms.BooleanField(label="Проход в обе стороны", required=False)
    access = forms.ChoiceField(label="Доступ", required=False,
                               choices=(("", "Выберите"), ("public", "Общая зона"),
                                        ("restricted", "Режимная зона"),
                                        ("excluded", "Проход закрыт")))
    kind = forms.ChoiceField(label="Тип перехода", required=False,
                             choices=(("", "Выберите"), ("corridor", "Проход"),
                                      ("elevator", "Лифт")))
    flow = forms.ChoiceField(label="Санитарный поток", required=False,
                             choices=(("", "Выберите"), ("clean", "Чистый"),
                                      ("dirty", "Грязный")))

    def __init__(self, *args, topology, **kwargs):
        super().__init__(*args, **kwargs)
        self.topology = topology
        choices = [(node["id"], f'{node["label"]} · {node["floor"]}') for node in topology["nodes"]]
        self.fields["start"].choices = choices
        self.fields["end"].choices = choices
        if topology["object_slug"] != "airport":
            self.fields.pop("access")
        if topology["object_slug"] != "hospital":
            self.fields.pop("kind")
            self.fields.pop("flow")

    def clean(self):
        values = super().clean()
        if values.get("start") and values.get("start") == values.get("end"):
            self.add_error("end", "Выберите другую точку.")
        nodes = {node["id"]: node for node in self.topology["nodes"]}
        start, end = nodes.get(values.get("start")), nodes.get(values.get("end"))
        if self.topology["object_slug"] == "hospital":
            if not values.get("kind"):
                self.add_error("kind", "Укажите тип перехода.")
            if not values.get("flow"):
                self.add_error("flow", "Укажите санитарный поток.")
            if start and end and start["floor"] != end["floor"] and values.get("kind") != "elevator":
                self.add_error("kind", "Между этажами нужен лифт.")
        elif start and end and start["floor"] != end["floor"]:
            self.add_error("end", "Соединение между уровнями требует отдельной модели перехода.")
        if self.topology["object_slug"] == "airport" and not values.get("access"):
            self.add_error("access", "Укажите режим доступа.")
        if values.get("length_m") is not None and not values.get("length_source"):
            self.add_error("length_source", "Укажите источник измеренной длины.")
        if values.get("length_m") is None and values.get("length_source"):
            self.add_error("length_source", "Добавьте длину или удалите источник.")
        return values


class TopologyRouteForm(forms.Form):
    origin = forms.ChoiceField(label="Начало маршрута")
    destination = forms.ChoiceField(label="Конец маршрута")
    route_flow = forms.ChoiceField(label="Санитарный поток", required=False,
                                   choices=(("", "Выберите"), ("clean", "Чистый"),
                                            ("dirty", "Грязный")))

    def __init__(self, *args, topology, **kwargs):
        super().__init__(*args, **kwargs)
        choices = [(node["id"], f'{node["label"]} · {node["floor"]}') for node in topology["nodes"]]
        self.fields["origin"].choices = choices
        self.fields["destination"].choices = choices
        if topology["object_slug"] != "hospital":
            self.fields.pop("route_flow")

    def clean(self):
        values = super().clean()
        if values.get("origin") and values.get("origin") == values.get("destination"):
            self.add_error("destination", "Выберите другую точку.")
        if "route_flow" in self.fields and not values.get("route_flow"):
            self.add_error("route_flow", "Выберите санитарный поток.")
        return values


class ProjectCreateForm(forms.Form):
    name = forms.CharField(label="Название проекта", max_length=100, strip=True)


class OperationLogUploadForm(forms.Form):
    file = forms.FileField(label="CSV журнала заданий")
    period_start = forms.CharField(label="Начало наблюдения (ISO 8601 с часовым поясом)")
    period_end = forms.CharField(label="Конец наблюдения (ISO 8601 с часовым поясом)")
    source_description = forms.CharField(
        label="Источник журнала и границ периода", max_length=500, strip=True,
    )
    source_attested = forms.BooleanField(
        label="Подтверждаю, что журнал описывает эту операцию и не содержит персональных данных",
    )

    def clean(self):
        values = super().clean()
        from datetime import datetime, timezone
        for key in ("period_start", "period_end"):
            raw = values.get(key)
            if not raw:
                continue
            try:
                value = datetime.fromisoformat(raw.strip())
            except ValueError:
                self.add_error(key, "Укажите дату и время в ISO 8601.")
                continue
            if value.utcoffset() is None:
                self.add_error(key, "Укажите смещение часового пояса.")
                continue
            values[key] = value.astimezone(timezone.utc)
        start, end = values.get("period_start"), values.get("period_end")
        if isinstance(start, datetime) and isinstance(end, datetime) and start >= end:
            self.add_error("period_end", "Конец периода должен быть позже начала.")
        return values


class AvailabilityUploadForm(forms.Form):
    file = forms.FileField(label="CSV календаря роботов")
    source_description = forms.CharField(
        label="Источник сменного календаря", max_length=500, strip=True,
    )
    source_attested = forms.BooleanField(
        label="Подтверждаю интервалы и положение каждого робота в начале доступных интервалов",
    )


class FinancePlanUploadForm(forms.Form):
    file = forms.FileField(label="CSV финансового плана")
    horizon_months = forms.IntegerField(label="Горизонт, месяцев", min_value=60, max_value=600)
    monthly_discount_rate = forms.DecimalField(
        label="Месячная ставка дисконтирования (доля)", min_value=0, max_value=1,
        max_digits=10, decimal_places=8,
    )
    discount_rate_source = forms.CharField(label="Источник ставки дисконтирования", max_length=500)
    source_description = forms.CharField(label="Источник денежных строк", max_length=500)
    forecast_basis = forms.CharField(
        label="Источник и метод прогноза месячных объёмов", max_length=1000,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    source_attested = forms.BooleanField(
        label="Подтверждаю суммы, состав платежей, валюту, НДС, источники объёмов и фактическое снижение денежных расходов; файл не содержит персональных данных",
    )


class FinanceVariantForm(forms.Form):
    source_row = forms.IntegerField(label="Номер строки в финансовом плане", min_value=2)
    amount = forms.DecimalField(label="Новая сумма", min_value=0,
                                max_digits=22, decimal_places=4)
    source_date = forms.DateField(label="Дата документа", widget=forms.DateInput(attrs={"type": "date"}))
    source_ref = forms.CharField(label="Документ или ссылка, подтверждающие сумму",
                                 max_length=1000)
    source_attested = forms.BooleanField(
        label="Подтверждаю новую сумму, её источник и отсутствие персональных данных",
    )


class DemandRevisionForm(forms.Form):
    peak_jobs_per_h = forms.DecimalField(
        label="Новый пиковый поток, рейсов/ч", min_value=0,
        max_digits=15, decimal_places=3,
    )
    peak_source = forms.CharField(label="Источник пикового потока", max_length=500)
    file = forms.FileField(label="CSV нового потока заданий")
    source_description = forms.CharField(label="Источник журнала заданий", max_length=500)
    source_attested = forms.BooleanField(
        label="Подтверждаю происхождение данных и отсутствие персональных сведений",
    )


class ProjectInputsForm(forms.Form):
    def __init__(self, *args, profile, **kwargs):
        super().__init__(*args, **kwargs)
        for number, field in enumerate(profile["fields"]):
            if field["kind"] == "boolean":
                self.fields[f"field_{number}"] = forms.ChoiceField(
                    label=field["label"], required=False,
                    choices=(("", "Нет данных"), ("true", "Да"), ("false", "Нет")),
                    initial="true" if field["value"] is True else "false" if field["value"] is False else "",
                )
            else:
                self.fields[f"field_{number}"] = forms.CharField(
                    label=field["label"], max_length=200, required=False,
                    initial=field["value"],
                )


class TaskProfileForm(forms.Form):
    source_attested = forms.BooleanField(
        label="Подтверждаю, что введённые значения относятся к этому проекту и имеют указанные источники",
        required=False,
    )

    def __init__(self, *args, object_slug, process_code, previous=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.process = process_for(object_slug, process_code)
        if self.process is None:
            raise ValueError("Неизвестный процесс для объекта")
        previous_values = (previous or {}).get("parameters", {})
        for field in self.process.fields:
            saved = previous_values.get(field.key, {})
            self.fields[field.key] = forms.DecimalField(
                label=field.label, required=False,
                max_digits=15, decimal_places=3, initial=saved.get("value"),
                widget=forms.NumberInput(attrs={"step": "any", "min": "0"}),
            )
            self.fields[f"{field.key}_source"] = forms.CharField(
                label=f"Источник: {field.label.casefold()}", required=False,
                max_length=500, initial=saved.get("source", ""),
            )
        if self.process.code == "warehouse_pallet_transfer":
            handoff = previous_values.get("pallet_handoff_mode", {})
            self.fields["pallet_handoff_mode"] = forms.ChoiceField(
                label="Как устроены загрузка и снятие паллеты?", required=False,
                choices=(("", "Выберите способ передачи"), *PALLET_HANDOFF_MODES),
                initial=handoff.get("value"),
            )
            self.fields["pallet_handoff_mode_source"] = forms.CharField(
                label="Источник выбранной схемы передачи паллеты", required=False,
                max_length=500, initial=handoff.get("source", ""),
            )

    def clean(self):
        cleaned = super().clean()
        for field in self.process.fields:
            value = cleaned.get(field.key)
            source = (cleaned.get(f"{field.key}_source") or "").strip()
            if value is not None and value <= 0:
                self.add_error(field.key, "Значение должно быть больше нуля")
            if value is not None and not source:
                self.add_error(f"{field.key}_source", "Укажите документ, измерение или другой проверяемый источник")
            if value is None and source:
                self.add_error(field.key, "Укажите измеренное значение или удалите описание источника")
        handoff_mode = cleaned.get("pallet_handoff_mode")
        handoff_source = (cleaned.get("pallet_handoff_mode_source") or "").strip()
        if handoff_mode and not handoff_source:
            self.add_error("pallet_handoff_mode_source", "Укажите план, регламент или подтверждение схемы передачи")
        if handoff_source and not handoff_mode:
            self.add_error("pallet_handoff_mode", "Выберите способ передачи паллеты")
        if (any(cleaned.get(field.key) is not None for field in self.process.fields) or handoff_mode) and not cleaned.get("source_attested"):
            self.add_error("source_attested", "Подтвердите происхождение введённых значений")
        return cleaned

    def to_profile(self, *, user_id, recorded_at):
        if not self.is_valid():
            raise ValueError("Нельзя сохранить непроверенный паспорт задачи")
        parameters = {
            field.key: {
                "value": str(self.cleaned_data[field.key]),
                "unit": field.unit,
                "source": self.cleaned_data[f"{field.key}_source"].strip(),
                "status": "user_attested",
            }
            for field in self.process.fields if self.cleaned_data[field.key] is not None
        }
        if self.process.code == "warehouse_pallet_transfer" and self.cleaned_data["pallet_handoff_mode"]:
            parameters["pallet_handoff_mode"] = {
                "value": self.cleaned_data["pallet_handoff_mode"], "unit": "handoff_mode",
                "source": self.cleaned_data["pallet_handoff_mode_source"].strip(),
                "status": "user_attested",
            }
        return {
            "version": 2,
            "object_slug": self.process.object_slug,
            "process": self.process.code,
            "parameters": parameters,
            "attested_by_user_id": user_id if parameters else None,
            "attested_at": recorded_at.isoformat() if parameters else None,
        }


class WorkloadProfileForm(forms.Form):
    source_attested = forms.BooleanField(
        label="Подтверждаю происхождение эксплуатационных показателей для этого объекта и модели",
        required=False,
    )

    def __init__(self, *args, process, selection=None, selection_ref=None, previous=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.process = process
        self.selection = selection or {}
        self.selection_ref = selection_ref
        saved = (previous or {}).get("parameters", {})
        for key, label, unit in sizing_fields(process):
            self.fields[key] = forms.DecimalField(
                label=f"{label}, {unit}", required=False, max_digits=15, decimal_places=3,
                initial=(saved.get(key) or {}).get("value"),
                widget=forms.NumberInput(attrs={"step": "any", "min": "0"}),
            )
            self.fields[f"{key}_source"] = forms.CharField(
                label=f"Источник: {label.casefold()}", max_length=500, required=False,
                initial=(saved.get(key) or {}).get("source", ""),
            )

    def clean(self):
        values = super().clean()
        for key, label, _ in sizing_fields(self.process):
            value = values.get(key)
            source = (values.get(f"{key}_source") or "").strip()
            if value is not None and (value < 0 or (value == 0 and key not in NONNEGATIVE_FIELDS)):
                self.add_error(key, "Значение вне допустимого диапазона")
            if key.endswith("_fraction") and value is not None and value > 1:
                self.add_error(key, "Доля должна быть от 0 до 1")
            if value is not None and not source:
                self.add_error(f"{key}_source", "Укажите документ, измерение или журнал")
            if value is None and source:
                self.add_error(key, "Укажите значение или удалите описание источника")
        if any(values.get(key) is not None for key, _, _ in sizing_fields(self.process)) and not values.get("source_attested"):
            self.add_error("source_attested", "Подтвердите происхождение показателей")
        return values

    def to_profile(self, *, user_id, recorded_at):
        if not self.is_valid():
            raise ValueError("Нельзя сохранить непроверенные показатели")
        parameters = {
            key: {"value": str(self.cleaned_data[key]), "unit": unit,
                  "source": self.cleaned_data[f"{key}_source"].strip(), "status": "user_attested"}
            for key, _, unit in sizing_fields(self.process) if self.cleaned_data[key] is not None
        }
        profile = {
            "version": 2, "process": self.process.code,
            "parameters": parameters,
            "attested_by_user_id": user_id if parameters else None,
            "attested_at": recorded_at.isoformat() if parameters else None,
        }
        if self.selection_ref is not None:
            profile["selection_ref"] = self.selection_ref
        if self.selection_ref is None or self.selection_ref["catalog_source_kind"] == "organizer_v4":
            profile["robot_record_index"] = self.selection.get("record_index")
        return profile
