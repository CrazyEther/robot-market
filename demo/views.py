from django.db import connection
from django.db.utils import OperationalError
from django.http import Http404, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET

from demo.models import DemoScenario
from demo.catalog import load_demo_catalog
from demo.matching import evaluate_match

PARAMETER_LABELS = {
    "demand": "Интенсивность перевозок",
    "pallet_mass": "Масса паллеты",
    "shift_hours": "Продолжительность смены",
    "robot_payload": "Грузоподъёмность робота",
    "robot_price": "Стоимость робота",
    "container_mass": "Масса контейнера",
    "access_permission": "Разрешение на доступ",
    "lift_wait": "Ожидание лифта",
}


@require_GET
def health(request):
    return JsonResponse({"status": "alive"})


@require_GET
def ready(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        # A schema check catches an unmigrated installation.
        DemoScenario.objects.exists()
    except OperationalError:
        return JsonResponse({"status": "not_ready"}, status=503)
    except Exception:  # Missing schema or database connectivity is not readiness.
        return JsonResponse({"status": "not_ready"}, status=503)
    return JsonResponse({"status": "ready"})


@require_GET
def index(request):
    return render(request, "demo/index.html", {"scenarios": DemoScenario.objects.all()})


@require_GET
def detail(request, slug):
    scenario = get_object_or_404(DemoScenario, slug=slug)
    parameters = [
        {"label": PARAMETER_LABELS.get(code, code), **field}
        for code, field in scenario.parameters.items()
    ]
    return render(request, "demo/detail.html", {"scenario": scenario, "parameters": parameters})


def _matches_for(scenario):
    return [evaluate_match(scenario, robot) for robot in load_demo_catalog()]


@require_GET
def market(request, slug):
    scenario = get_object_or_404(DemoScenario, slug=slug)
    matches = _matches_for(scenario)
    selected_slug = request.GET.get("robot")
    selected = None
    if selected_slug:
        selected = next(
            (item for item in matches if item["robot"]["slug"] == selected_slug),
            None,
        )
        if selected is None:
            raise Http404("Демонстрационная модель не найдена")
        if selected["status"] == "reject":
            return HttpResponseBadRequest("Модель не подходит для выбранного процесса")
    return render(
        request,
        "demo/market.html",
        {"scenario": scenario, "matches": matches, "selected": selected},
    )


@require_GET
def matches_api(request, slug):
    scenario = get_object_or_404(DemoScenario, slug=slug)
    return JsonResponse({"object": scenario.slug, "matches": _matches_for(scenario)})


def serialize(scenario):
    return {
        "slug": scenario.slug,
        "title": scenario.title,
        "process": scenario.process,
        "unit": scenario.unit,
        "constraint": scenario.constraint,
        "topology": scenario.topology,
        "parameters": scenario.parameters,
        "data_status": scenario.data_status,
    }


@require_GET
def objects_api(request):
    return JsonResponse({"objects": [serialize(s) for s in DemoScenario.objects.all()]})


@require_GET
def object_api(request, slug):
    scenario = get_object_or_404(DemoScenario, slug=slug)
    return JsonResponse(serialize(scenario))
