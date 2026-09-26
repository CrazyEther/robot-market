from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from demo.models import DemoScenario
from demo.snapshots import PARAMETER_LABELS, snapshot_scenario
from projects.forms import ProjectCreateForm
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
            project = Project.objects.create(
                owner=request.user,
                name=form.cleaned_data["name"],
                object_slug=scenario.slug,
            )
            ProjectRevision.objects.create(
                project=project,
                number=1,
                scenario_snapshot=snapshot_scenario(scenario),
            )
        return redirect("project_detail", project_id=project.id)
    return render(
        request, "projects/create.html", {"scenario": scenario, "form": form}
    )


@login_required
@require_GET
def project_detail(request, project_id):
    project = get_object_or_404(Project, id=project_id, owner=request.user)
    revision = project.revisions.first()
    parameters = [
        {"label": PARAMETER_LABELS.get(code, code), **field}
        for code, field in revision.scenario_snapshot["parameters"].items()
    ]
    return render(
        request, "projects/detail.html",
        {"project": project, "revision": revision, "parameters": parameters},
    )


@login_required
@require_POST
def project_delete(request, project_id):
    project = get_object_or_404(Project, id=project_id, owner=request.user)
    project.delete()
    return redirect("project_list")
