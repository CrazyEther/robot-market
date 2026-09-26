from django.db import connection
from django.db.utils import OperationalError
from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

from catalog.publication import current_source_pair
from projects.models import Project


OBJECT_TITLES = dict(Project.OBJECT_TYPES)


def _object_title(slug):
    try:
        return OBJECT_TITLES[slug]
    except KeyError:
        raise Http404("Тип объекта не найден") from None


@require_GET
def health(request):
    return JsonResponse({"status": "alive"})


@require_GET
def ready(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        Project.objects.exists()
        batch, evidence_batch = current_source_pair()
        if batch is None or evidence_batch is None:
            return JsonResponse({"status": "not_ready"}, status=503)
    except OperationalError:
        return JsonResponse({"status": "not_ready"}, status=503)
    except Exception:
        return JsonResponse({"status": "not_ready"}, status=503)
    return JsonResponse({"status": "ready"})


@require_GET
def index(request):
    return render(request, "demo/index.html", {"object_types": Project.OBJECT_TYPES})


@require_GET
def detail(request, slug):
    return render(request, "demo/detail.html", {
        "object_slug": slug,
        "object_title": _object_title(slug),
    })


@require_GET
def objects_api(request):
    return JsonResponse({"objects": [
        {"slug": slug, "title": title} for slug, title in Project.OBJECT_TYPES
    ]})


@require_GET
def object_api(request, slug):
    return JsonResponse({"slug": slug, "title": _object_title(slug)})
