from copy import deepcopy
from time import time

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from demo.models import DemoScenario
from demo.snapshots import snapshot_scenario
from projects.forms import ProjectCreateForm, ProjectInputsForm
from projects.input_profiles import (
    SHEETS, ProfileValidationError, normalize_value, parse_upload,
    profile_for_revision, profile_from_demo,
)
from projects.models import Project, ProjectRevision


@login_required
@require_GET
def project_list(request):
    projects = Project.objects.filter(owner=request.user)
    return render(request, "projects/list.html", {"projects": projects})


@login_required
@require_http_methods(["GET", "POST"])
def project_create(request, slug):
    scenario = get_object_or_404(DemoScenario, slug=slug)
    form = ProjectCreateForm(request.POST or None)
    if request.method == "POST":
        if not form.is_valid():
            return render(
                request, "projects/create.html",
                {"scenario": scenario, "form": form}, status=400,
            )
        with transaction.atomic():
            snapshot = snapshot_scenario(scenario)
            snapshot["input_profile"] = profile_from_demo(snapshot)
            project = Project.objects.create(
                owner=request.user,
                name=form.cleaned_data["name"],
                object_slug=scenario.slug,
            )
            ProjectRevision.objects.create(
                project=project,
                number=1,
                scenario_snapshot=snapshot,
            )
        return redirect("project_detail", project_id=project.id)
    return render(
        request, "projects/create.html", {"scenario": scenario, "form": form}
    )


@login_required
@require_GET
def project_detail(request, project_id):
    project = get_object_or_404(Project, id=project_id, owner=request.user)
    number = request.GET.get("revision")
    if number is None:
        revision = project.revisions.first()
    elif number.isdecimal():
        revision = get_object_or_404(project.revisions, number=int(number))
    else:
        raise Http404("Ревизия не найдена")
    parameters = profile_for_revision(revision)["fields"]
    return render(
        request, "projects/detail.html",
        {
            "project": project,
            "revision": revision,
            "parameters": parameters,
            "revisions": project.revisions.all(),
        },
    )


def _append_revision(project, profile, expected_number):
    with transaction.atomic():
        locked = Project.objects.select_for_update().get(pk=project.pk)
        latest = locked.revisions.first()
        if latest.number != expected_number:
            raise ProfileValidationError("Проект изменился: откройте актуальную ревизию")
        snapshot = deepcopy(latest.scenario_snapshot)
        profile.pop("errors", None)
        snapshot["input_profile"] = profile
        revision = ProjectRevision.objects.create(
            project=locked, number=latest.number + 1, scenario_snapshot=snapshot,
        )
        Project.objects.filter(pk=locked.pk).update(updated_at=timezone.now())
    return revision


@login_required
@require_http_methods(["GET", "POST"])
def project_inputs(request, project_id):
    project = get_object_or_404(Project, id=project_id, owner=request.user)
    latest = project.revisions.first()
    profile = profile_for_revision(latest)
    form = ProjectInputsForm(request.POST or None, profile=profile)
    rows = list(zip(profile["fields"], form.visible_fields()))
    if request.method == "POST":
        if not form.is_valid():
            return render(request, "projects/inputs.html", {
                "project": project, "form": form, "rows": rows,
                "base_revision": latest.number,
            }, status=400)
        updated = deepcopy(profile)
        changed = False
        for number, field in enumerate(updated["fields"]):
            raw = form.cleaned_data[f"field_{number}"]
            try:
                value = normalize_value(raw, field["kind"], field["min"], field["max"])
            except ProfileValidationError as exc:
                form.add_error(f"field_{number}", str(exc))
                continue
            if value != field["value"]:
                changed = True
                field["raw_value"] = raw
                field["value"] = value
                field["status"] = "missing" if value is None else "user_input"
                field["original_source"] = field.get("original_source", field["source"])
                field["source"] = "Введено пользователем"
                field["override"] = True
        if form.errors:
            return render(request, "projects/inputs.html", {
                "project": project, "form": form, "rows": rows,
                "base_revision": latest.number,
            }, status=400)
        if not changed:
            return redirect("project_detail", project_id=project.id)
        try:
            revision = _append_revision(project, updated, int(request.POST.get("base_revision", "0")))
        except (ProfileValidationError, ValueError):
            form.add_error(None, "Проект изменился: откройте актуальную ревизию")
            return render(request, "projects/inputs.html", {
                "project": project, "form": form, "rows": rows,
                "base_revision": latest.number,
            }, status=409)
        return redirect(f"/projects/{project.id}/?revision={revision.number}")
    return render(request, "projects/inputs.html", {
        "project": project, "form": form, "rows": rows,
        "base_revision": latest.number,
    })


@login_required
@require_http_methods(["GET", "POST"])
def project_import(request, project_id):
    project = get_object_or_404(Project, id=project_id, owner=request.user)
    latest = project.revisions.first()
    pending_key = f"profile-import-{project.id}"
    context = {
        "project": project,
        "base_revision": latest.number,
        "expected_sheet": SHEETS[project.object_slug],
    }
    if request.method == "POST" and request.POST.get("action") == "preview":
        request.session.pop(pending_key, None)
        upload = request.FILES.get("file")
        if upload is None:
            context["error"] = "Выберите файл XLSX или CSV"
        else:
            try:
                profile = parse_upload(upload, project.object_slug)
                context["preview"] = profile
                if not profile["errors"]:
                    request.session[pending_key] = {
                        "profile": profile, "revision": latest.number,
                        "created_at": time(),
                    }
            except ProfileValidationError as exc:
                context["error"] = str(exc)
        return render(request, "projects/import.html", context, status=400 if context.get("error") or context.get("preview", {}).get("errors") else 200)
    if request.method == "POST" and request.POST.get("action") == "commit":
        pending = request.session.pop(pending_key, None)
        if not pending or time() - pending["created_at"] > 600:
            context["error"] = "Предпросмотр устарел: загрузите файл снова"
            return render(request, "projects/import.html", context, status=400)
        try:
            revision = _append_revision(project, pending["profile"], pending["revision"])
        except ProfileValidationError as exc:
            context["error"] = str(exc)
            return render(request, "projects/import.html", context, status=409)
        return redirect(f"/projects/{project.id}/?revision={revision.number}")
    return render(request, "projects/import.html", context)


@login_required
@require_POST
def project_delete(request, project_id):
    project = get_object_or_404(Project, id=project_id, owner=request.user)
    project.delete()
    return redirect("project_list")
