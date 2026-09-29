"""Build auditable cash flows from explicit user terms and a verified run."""

import csv
import io
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP

from django import forms

from projects.finance import HEADER, FinanceInputError, parse_finance_csv


class FinanceBuilderForm(forms.Form):
    initial_classes = (("", "Выберите статью по источнику"), ("capex", "CAPEX — капитальные затраты"), ("opex", "OPEX — операционные затраты"), ("other_cash", "Прочий денежный поток"))
    baseline_initial_class = forms.ChoiceField(label="Статья первоначальных затрат действующего процесса", choices=initial_classes)
    raas_initial_class = forms.ChoiceField(label="Статья разового платежа RaaS", choices=initial_classes)
    horizon_months = forms.IntegerField(label="Горизонт, месяцев", min_value=60, max_value=600, initial=60)
    currency = forms.RegexField(r"^[A-Z]{3}$", label="Валюта договора (RUB, USD…)", max_length=3)
    vat_mode = forms.ChoiceField(label="НДС", choices=(("", "Выберите"), ("gross", "Все суммы с НДС"), ("net", "Все суммы без НДС")))
    source_date = forms.DateField(label="Дата коммерческих условий", widget=forms.DateInput(attrs={"type": "date"}))
    monthly_discount_rate = forms.DecimalField(label="Месячная ставка дисконтирования, доля", min_value=0, max_value=1, max_digits=10, decimal_places=8)
    discount_rate_source = forms.CharField(label="Основание ставки", max_length=500)
    periods_per_month = forms.DecimalField(label="Число повторений периода прогона в месяц", min_value=Decimal("0.0001"), max_digits=12, decimal_places=4)
    forecast_basis = forms.CharField(label="Обоснование повторения нагрузки и календаря", max_length=600, widget=forms.Textarea(attrs={"rows": 2}))
    baseline_source = forms.CharField(label="Источник затрат действующего процесса", max_length=500)
    purchase_source = forms.CharField(label="Источник цены и затрат покупки", max_length=500)
    raas_source = forms.CharField(label="Источник условий RaaS и состава услуг", max_length=500)
    source_attested = forms.BooleanField(label="Подтверждаю коммерческие условия и сопоставимый объём работ; дополнительные затраты RaaS не включены в подписку. Нулевые суммы указаны явно.")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        labels = {
            "baseline_initial": "Действующий процесс: вложения в месяце 0",
            "baseline_fixed": "Действующий процесс: постоянные затраты в месяц",
            "baseline_per_job": "Действующий процесс: переменные затраты на выполненный рейс",
            "robot_price": "Цена одного робота выбранной комплектации",
            "deployment": "Интеграция, инфраструктура и ввод всего парка",
            "working_capital": "Возвратный оборотный капитал покупки",
            "purchase_fixed": "Покупка: постоянные затраты всего парка в месяц",
            "purchase_per_job": "Покупка: переменные затраты на выполненный рейс",
            "raas_initial": "RaaS: разовый платёж подключения всего парка",
            "raas_per_robot": "RaaS: подписка за одного робота в месяц",
            "raas_extra_fixed": "RaaS: дополнительные затраты в месяц вне подписки",
            "raas_per_job": "RaaS: дополнительные переменные затраты на рейс",
        }
        for name, label in labels.items():
            self.fields[name] = forms.DecimalField(label=label, min_value=0, max_digits=16, decimal_places=4)
        self.order_fields([key for key in self.fields if key != "source_attested"] + ["source_attested"])

    def clean_currency(self):
        value = self.cleaned_data["currency"]
        if value == "XXX":
            raise forms.ValidationError("Укажите валюту договора.")
        return value

    @property
    def sections(self):
        groups = (
            ("Период и условия сравнения", ("horizon_months", "currency", "vat_mode", "source_date", "monthly_discount_rate", "discount_rate_source", "periods_per_month", "forecast_basis")),
            ("Действующий процесс", ("baseline_initial", "baseline_initial_class", "baseline_fixed", "baseline_per_job", "baseline_source")),
            ("Покупка оборудования", ("robot_price", "deployment", "working_capital", "purchase_fixed", "purchase_per_job", "purchase_source")),
            ("Роботы как услуга · RaaS", ("raas_initial", "raas_initial_class", "raas_per_robot", "raas_extra_fixed", "raas_per_job", "raas_source")),
            ("Подтверждение условий", ("source_attested",)),
        )
        return [(title, [self[name] for name in names]) for title, names in groups]


def build_cashflow(values, *, fleet, delivered, period_seconds, run_id):
    """Repeat an explicitly attested period, never monetize unserved jobs.

    Amounts stay in constant nominal prices; custom escalation belongs to the
    detailed CSV path. Round each payable line once to four decimal places.
    """
    fleet = Decimal(str(fleet))
    delivered = Decimal(str(delivered))
    seconds = Decimal(str(period_seconds))
    repeats = values["periods_per_month"]
    if (not fleet.is_finite() or fleet <= 0 or fleet != fleet.to_integral_value()
            or not delivered.is_finite() or delivered < 0
            or not seconds.is_finite() or seconds <= 0):
        raise FinanceInputError("Недопустимые показатели сохранённого прогона.")
    # A repeated period must fit even the shortest calendar month. This is
    # an upper bound, not evidence that the site's future calendar repeats.
    if repeats * seconds > Decimal(28 * 86400):
        raise FinanceInputError("Повторения периода превышают 28 суток в месяц. Для переменного календаря используйте помесячный план.")
    if delivered == 0:
        raise FinanceInputError("Прогон не выполнил доставку. Исправьте сценарий перед прогнозом экономики.")
    units = (delivered * repeats).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
    if units <= 0:
        raise FinanceInputError("Месячный объём слишком мал для расчёта.")
    volume_source = f"run={run_id}; repetitions={repeats}; {values['forecast_basis']}"
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(HEADER)

    def row(scenario, month, scope, cash_class, amount, direction="outflow"):
        writer.writerow([
            scenario, month, scope, direction, cash_class,
            format(amount.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP), "f"),
            values["currency"], values["vat_mode"], values["source_date"].isoformat(),
            values[f"{scenario}_source"], "0" if month == 0 else format(units, "f"),
            volume_source, "",
        ])

    row("baseline", 0, "initial_process", values["baseline_initial_class"], values["baseline_initial"])
    row("purchase", 0, "robot_equipment", "capex", values["robot_price"] * fleet)
    row("purchase", 0, "deployment", "capex", values["deployment"])
    row("purchase", 0, "working_capital", "other_cash", values["working_capital"])
    row("raas", 0, "connection", values["raas_initial_class"], values["raas_initial"])
    for month in range(1, values["horizon_months"] + 1):
        row("baseline", month, "fixed_operations", "opex", values["baseline_fixed"])
        row("baseline", month, "variable_operations", "opex", values["baseline_per_job"] * units)
        row("purchase", month, "fixed_operations", "opex", values["purchase_fixed"])
        row("purchase", month, "variable_operations", "opex", values["purchase_per_job"] * units)
        row("raas", month, "raas_subscription", "opex", values["raas_per_robot"] * fleet)
        row("raas", month, "extra_operations", "opex", values["raas_extra_fixed"])
        row("raas", month, "variable_operations", "opex", values["raas_per_job"] * units)
    row("purchase", values["horizon_months"], "working_capital_return", "other_cash", values["working_capital"], "inflow")
    raw = stream.getvalue().encode("utf-8")
    rows = parse_finance_csv(raw, horizon_months=values["horizon_months"])
    return raw, rows


def initial_investment(rows):
    """TCI: all initial outflows, separately from total-horizon CAPEX."""
    return {scenario: sum((Decimal(row["amount"]) for row in rows
                          if row["scenario"] == scenario and row["month"] == 0
                          and row["direction"] == "outflow"), Decimal(0))
            for scenario in ("baseline", "purchase", "raas")}
