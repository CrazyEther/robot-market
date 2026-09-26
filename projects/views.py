from copy import deepcopy
import hashlib
import logging
from decimal import Decimal, InvalidOperation
from time import time
from uuid import UUID, uuid4

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from catalog.models import (
    CatalogEvidenceBatch, CatalogEvidenceClaim, CatalogSourceRow,
    SupplementPublication, SupplementSpecification,
)
from catalog.publication import current_source_pair, visible_families
from catalog.supplement_publication import publicly_available_supplement
from projects.forms import (
    ProjectCreateForm, ProjectInputsForm, TaskProfileForm, WorkloadProfileForm,
    OperationLogUploadForm, AvailabilityUploadForm, FinancePlanUploadForm, FinanceVariantForm,
    DemandRevisionForm,
    TopologyNodeForm, TopologyEdgeForm, TopologyRouteForm,
)
from projects.input_profiles import (
    SHEETS, ProfileValidationError, normalize_value, parse_upload,
    profile_for_revision,
)
from projects.models import AvailabilityPlan, FinancePlan, FinanceVariant, OperationLog, Project, ProjectRevision, SimulationRun
from projects.playback import (PlaybackDataError, availability_at_events,
                               measured_scene, movement_timeline, playback_events,
                               state_before)
from projects.reports import build_report_bundle
from projects.availability import (
    PARSER_VERSION as AVAILABILITY_PARSER_VERSION,
    AvailabilityError, available_windows, parse_availability, verified_availability_rows,
)
from projects.finance import (
    FINANCE_VERSION, HEADER as FINANCE_HEADER, FinanceInputError,
    calculate_finance, derive_finance_variant, finance_metadata_sha256,
    finance_variant_checksum,
    parse_finance_csv, validate_forecast_anchor,
    verified_finance_result, verified_finance_variant,
)
from projects.event_ledger import LEDGER_VERSION, EventInputError, schedule_observed_jobs
from projects.demand import DemandRevisionError, minimum_observed_peak, revised_demand_snapshot
from projects.operation_logs import (
    MAX_UPLOAD_BYTES, MAX_ROWS, PARSER_VERSION, OperationLogError, parse_operation_log,
    validate_observation_period, verified_operation_rows,
)
from projects.matching import MATCHING_VERSION, match_candidates, match_supplement_candidates
from projects.selection_refs import (
    availability_matches_selection, selection_key, selection_ref,
    workload_matches_selection,
)
from projects.task_profiles import process_for, processes_for
from projects.topology import empty_topology, floor_drawings, route_result, transport_cycle_route
from projects.sizing import is_transport, size_project, sizing_fields


logger = logging.getLogger(__name__)


@login_required
@require_GET
def project_list(request):
    projects = Project.objects.filter(owner=request.user)
    return render(request, "projects/list.html", {
        "projects": projects, "object_types": Project.OBJECT_TYPES,
    })


@login_required
@require_http_methods(["GET", "POST"])
def project_create(request, slug):
    object_title = dict(Project.OBJECT_TYPES).get(slug)
    if object_title is None:
        raise Http404("Тип объекта не найден")
    form = ProjectCreateForm(request.POST or None)
    if request.method == "POST":
        if not form.is_valid():
            return render(
                request, "projects/create.html",
                {"object_slug": slug, "object_title": object_title, "form": form}, status=400,
            )
        batch, evidence_batch = current_source_pair()
        if batch is None or evidence_batch is None:
            return render(request, "projects/create.html", {
                "object_slug": slug, "object_title": object_title,
                "form": form, "source_error": "Источники каталога недоступны",
            }, status=503)
        with transaction.atomic():
            snapshot = {
                "schema_version": 2,
                "object_slug": slug,
                "object_title": object_title,
                "catalog_checksum": batch.checksum,
                "evidence_checksum": evidence_batch.checksum,
                "input_profile": {
                    "version": 2, "object_slug": slug,
                    "source_type": "not_supplied", "fields": [],
                },
            }
            supplement = SupplementPublication.objects.select_related("batch").filter(
                pk="storefront",
            ).first()
            if supplement:
                snapshot["supplement_checksum"] = supplement.batch.checksum
            project = Project.objects.create(
                owner=request.user,
                name=form.cleaned_data["name"],
                object_slug=slug,
            )
            ProjectRevision.objects.create(
                project=project,
                number=1,
                scenario_snapshot=snapshot,
            )
        return redirect("project_detail", project_id=project.id)
    return render(
        request, "projects/create.html", {
            "object_slug": slug, "object_title": object_title, "form": form,
        }
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
    task_profile = revision.scenario_snapshot.get("task_profile")
    selected_process = process_for(project.object_slug, task_profile.get("process")) if isinstance(task_profile, dict) else None
    selection = revision.scenario_snapshot.get("robot_selection")
    log_ref = revision.scenario_snapshot.get("operation_log_ref") or {}
    operation_log = (OperationLog.objects.filter(
        project=project, pk=log_ref.get("id"), sha256=log_ref.get("sha256"),
    ).first() if isinstance(log_ref, dict) and log_ref.get("id") else None)
    latest_run = (SimulationRun.objects.filter(project=project, revision=revision)
                  .order_by("-created_at").first())
    latest_plan = (FinancePlan.objects.filter(
        project=project, simulation_run__revision=revision,
    ).order_by("-created_at").first()) if latest_run else None
    is_current_revision = revision.number == project.revisions.first().number
    selected_family = None
    selected_family_has_catalog_card = False
    if (isinstance(selection, dict)
            and selection.get("catalog_checksum") == revision.scenario_snapshot.get("catalog_checksum")
            and selection.get("evidence_checksum") == revision.scenario_snapshot.get("evidence_checksum")):
        row = CatalogSourceRow.objects.filter(
            batch__checksum=revision.scenario_snapshot.get("catalog_checksum"),
            record_index=selection.get("record_index"),
        ).select_related("family").first()
        if row and row.external_id == selection.get("external_id"):
            selected_family = row.family
            selected_evidence = CatalogEvidenceBatch.objects.filter(
                checksum=selection.get("evidence_checksum"),
                catalog_batch=row.batch,
            ).first()
            if selected_evidence:
                selected_family_has_catalog_card = visible_families(
                    row.batch, selected_evidence,
                ).filter(pk=selected_family.pk).exists()
    return render(
        request, "projects/detail.html",
        {
            "project": project,
            "revision": revision,
            "parameters": parameters,
            "selected_process": selected_process,
            "robot_selection": selection,
            "selected_family": selected_family,
            "selected_family_has_catalog_card": selected_family_has_catalog_card,
            "operation_log": operation_log,
            "latest_run": latest_run,
            "latest_plan": latest_plan,
            "is_current_revision": is_current_revision,
            "revisions": project.revisions.all(),
        },
    )


def _append_snapshot_change(project, key, value, expected_number):
    with transaction.atomic():
        locked = Project.objects.select_for_update().get(pk=project.pk)
        latest = locked.revisions.first()
        if latest.number != expected_number:
            raise ProfileValidationError("Проект изменился: откройте актуальную ревизию")
        snapshot = deepcopy(latest.scenario_snapshot)
        if key != "availability_ref":
            snapshot.pop("demand_what_if", None)
        if key == "task_profile":
            snapshot["schema_version"] = 3
        if key in {"task_profile", "input_profile", "supplement_checksum"}:
            snapshot.pop("robot_selection", None)
        if key in {"task_profile", "input_profile", "robot_selection", "supplement_checksum"}:
            snapshot.pop("workload_profile", None)
        if key in {"task_profile", "input_profile", "robot_selection", "supplement_checksum", "topology_profile",
                   "workload_profile", "operation_log_ref"}:
            snapshot.pop("availability_ref", None)
        if key == "task_profile" and (latest.scenario_snapshot.get("task_profile") or {}).get("process") != value.get("process"):
            snapshot.pop("topology_profile", None)
            snapshot.pop("operation_log_ref", None)
        if key == "robot_selection":
            snapshot["schema_version"] = 4
        if key == "topology_profile":
            snapshot["schema_version"] = 5
        if key == "workload_profile":
            snapshot["schema_version"] = 6
        if key == "operation_log_ref":
            snapshot["schema_version"] = 7
        if key == "availability_ref":
            snapshot["schema_version"] = 8
        snapshot[key] = value
        revision = ProjectRevision.objects.create(
            project=locked, number=latest.number + 1, scenario_snapshot=snapshot,
        )
        Project.objects.filter(pk=locked.pk).update(updated_at=timezone.now())
    return revision


def _append_revision(project, profile, expected_number):
    profile.pop("errors", None)
    return _append_snapshot_change(project, "input_profile", profile, expected_number)


@login_required
@require_http_methods(["GET", "POST"])
def project_task(request, project_id):
    project = get_object_or_404(Project, id=project_id, owner=request.user)
    latest = project.revisions.first()
    revision_text = request.GET.get("revision") if request.method == "GET" else None
    if revision_text is not None:
        if not revision_text.isdecimal():
            raise Http404("Ревизия не найдена")
        revision = get_object_or_404(project.revisions, number=int(revision_text))
    else:
        revision = latest
    saved = revision.scenario_snapshot.get("task_profile")
    historical = revision.pk != latest.pk
    process_code = (request.POST.get("process") if request.method == "POST" else
                    request.GET.get("process") or (saved or {}).get("process"))
    if historical:
        process_code = (saved or {}).get("process")
    process = process_for(project.object_slug, process_code)
    if process_code and process is None:
        raise Http404("Процесс для объекта не найден")
    form = None
    if process and not historical:
        previous = saved if saved and saved.get("process") == process.code else None
        form = TaskProfileForm(
            request.POST if request.method == "POST" else None,
            object_slug=project.object_slug, process_code=process.code, previous=previous,
        )
    if request.method == "POST":
        if form is None:
            raise Http404("Процесс для объекта не найден")
        if form.is_valid():
            profile = form.to_profile(user_id=request.user.pk, recorded_at=timezone.now())
            def comparable(item):
                return (item or {}).get("process"), (item or {}).get("parameters", {})
            try:
                expected = int(request.POST.get("base_revision", "0"))
                if expected != latest.number:
                    raise ProfileValidationError("Проект изменился: откройте актуальную ревизию")
                if comparable(profile) == comparable(saved):
                    return redirect("project_task", project_id=project.id)
                new_revision = _append_snapshot_change(project, "task_profile", profile, expected)
            except (ProfileValidationError, ValueError):
                form.add_error(None, "Проект изменился: откройте актуальную ревизию")
                status = 409
            else:
                return redirect(f"/projects/{project.id}/task/?revision={new_revision.number}")
        else:
            status = 400
    else:
        status = 200
    active_process = process_for(project.object_slug, saved.get("process")) if isinstance(saved, dict) else None
    matches = None
    if active_process:
        catalog_checksum = revision.scenario_snapshot.get("catalog_checksum")
        evidence_checksum = revision.scenario_snapshot.get("evidence_checksum")
        evidence = get_object_or_404(
            CatalogEvidenceBatch,
            checksum=evidence_checksum, catalog_batch__checksum=catalog_checksum,
        )
        matches = match_candidates(evidence, active_process, saved)
        published_ids = set(visible_families(
            evidence.catalog_batch, evidence,
        ).filter(pk__in=[item["family"].pk for item in matches]).values_list("pk", flat=True))
        for item in matches:
            item["catalog_card_available"] = item["family"].pk in published_ids
            item["source_kind"] = "organizer_v4"
            item["name"] = item["family"].name
            item["company"] = item["family"].company
        supplement = publicly_available_supplement(
            revision.scenario_snapshot.get("supplement_checksum")
        )
        if supplement:
            matches.extend(match_supplement_candidates(supplement, active_process, saved))
            rank = {"fit": 0, "requires_verification": 1, "reject": 2}
            matches.sort(key=lambda item: (rank[item["status"]], item["company"], item["name"]))
        selected = revision.scenario_snapshot.get("robot_selection") or {}
        if not isinstance(selected, dict):
            selected = {}
        for item in matches:
            item["is_selected"] = (
                selected.get("catalog_source_kind") == "manufacturer_supplement"
                and item["source_kind"] == "manufacturer_supplement"
                and selected.get("catalog_source_checksum") == item["catalog_source_checksum"]
                and selected.get("product_ref") == item["product_ref"]
            ) if item["source_kind"] == "manufacturer_supplement" else (
                selected.get("catalog_source_kind") in (None, "organizer_v4")
                and selected.get("record_index") == item["source_row"].record_index
            )
    current_supplement = SupplementPublication.objects.select_related("batch").filter(
        pk="storefront",
    ).first()
    supplement_update_available = bool(
        not historical and current_supplement
        and current_supplement.batch.checksum != revision.scenario_snapshot.get("supplement_checksum")
    )
    return render(request, "projects/task.html", {
        "project": project, "revision": revision, "historical": historical,
        "processes": processes_for(project.object_slug), "process": process,
        "active_process": active_process, "form": form,
        "field_rows": [(field, form[field.key], form[f"{field.key}_source"])
                       for field in process.fields] if form else [],
        "matches": matches, "base_revision": latest.number,
        "robot_selection": revision.scenario_snapshot.get("robot_selection"),
        "evidence_checksum": revision.scenario_snapshot.get("evidence_checksum"),
        "supplement_update_available": supplement_update_available,
    }, status=status)


@login_required
@require_POST
def project_supplement_refresh(request, project_id):
    project = get_object_or_404(Project, id=project_id, owner=request.user)
    latest = project.revisions.first()
    try:
        expected = int(request.POST.get("base_revision", ""))
    except ValueError:
        expected = None
    if expected != latest.number:
        return _selection_error(request, project, "Проект изменился. Откройте актуальную версию.", 409)
    publication = SupplementPublication.objects.select_related("batch").filter(pk="storefront").first()
    if publication is None:
        return _selection_error(request, project, "Подбор моделей сейчас недоступен.", 503)
    if latest.scenario_snapshot.get("supplement_checksum") == publication.batch.checksum:
        return redirect("project_task", project_id=project.id)
    try:
        revision = _append_snapshot_change(
            project, "supplement_checksum", publication.batch.checksum, expected,
        )
    except ProfileValidationError:
        return _selection_error(request, project, "Проект изменился. Откройте актуальную версию.", 409)
    return redirect(f"/projects/{project.id}/task/?revision={revision.number}")


def _selection_error(request, project, message, status):
    return render(request, "projects/selection_error.html", {
        "project": project, "message": message,
    }, status=status)


@login_required
@require_POST
def project_robot_selection(request, project_id):
    project = get_object_or_404(Project, id=project_id, owner=request.user)
    latest = project.revisions.first()
    try:
        expected = int(request.POST.get("base_revision", ""))
    except ValueError:
        return _selection_error(request, project, "Откройте актуальную версию проекта и повторите выбор.", 400)
    if expected != latest.number:
        return _selection_error(request, project, "Проект изменился. Откройте актуальную версию и повторите выбор.", 409)
    action = request.POST.get("action")
    if action == "remove":
        if latest.scenario_snapshot.get("robot_selection") is None:
            return _selection_error(request, project, "В проекте не выбрана модель.", 400)
        choice = None
    elif action == "select":
        snapshot = latest.scenario_snapshot
        task = snapshot.get("task_profile")
        process = process_for(project.object_slug, task.get("process")) if isinstance(task, dict) else None
        if process is None:
            return _selection_error(request, project, "Сначала сохраните операцию проекта.", 400)
        source_kind = request.POST.get("source_kind") or "organizer_v4"
        if source_kind == "organizer_v4":
            try:
                record_index = int(request.POST.get("record_index", ""))
            except ValueError:
                return _selection_error(request, project, "Модель не найдена в подборе для этой задачи.", 400)
            evidence = get_object_or_404(
                CatalogEvidenceBatch,
                checksum=snapshot.get("evidence_checksum"),
                catalog_batch__checksum=snapshot.get("catalog_checksum"),
            )
            candidate = next((item for item in match_candidates(evidence, process, task)
                              if item["source_row"].record_index == record_index), None)
            if candidate is None:
                return _selection_error(request, project, "Модель не найдена в подборе для этой задачи.", 400)
            if candidate["status"] == "reject":
                return _selection_error(request, project, "Эта модель не подходит по подтверждённому ограничению.", 422)
            row = candidate["source_row"]
            ref = {
                "catalog_source_kind": "organizer_v4",
                "catalog_source_checksum": snapshot["catalog_checksum"],
                "evidence_checksum": snapshot["evidence_checksum"],
                "record_index": row.record_index,
            }
            choice = {
                "version": 2, "matching_version": MATCHING_VERSION,
                "process": process.code, "selection_ref": ref,
                "catalog_source_kind": "organizer_v4",
                "catalog_source_checksum": snapshot["catalog_checksum"],
                "catalog_checksum": snapshot["catalog_checksum"],
                "evidence_checksum": snapshot["evidence_checksum"],
                "record_index": row.record_index, "external_id": row.external_id,
                "family_name": row.family.name, "company": row.family.company,
                "application_sources": candidate["application_sources"],
                "status": candidate["status"], "reason_codes": candidate["reason_codes"],
                "checks": candidate["checks"],
            }
        elif source_kind == "manufacturer_supplement":
            supplement = publicly_available_supplement(snapshot.get("supplement_checksum"))
            if supplement is None:
                return _selection_error(request, project, "Модель не опубликована для этой версии проекта.", 409)
            product_ref = request.POST.get("product_ref", "")
            candidate = next((item for item in match_supplement_candidates(supplement, process, task)
                              if item["product_ref"] == product_ref), None)
            if candidate is None:
                return _selection_error(request, project, "Модель не найдена в подборе для этой задачи.", 400)
            if candidate["status"] == "reject":
                return _selection_error(request, project, "Эта модель не подходит по подтверждённому ограничению.", 422)
            ref = {
                "catalog_source_kind": "manufacturer_supplement",
                "catalog_source_checksum": supplement.checksum,
                "evidence_checksum": supplement.checksum,
                "product_ref": candidate["product_ref"], "offer_ref": None,
            }
            choice = {
                "version": 2, "matching_version": MATCHING_VERSION,
                "process": process.code, "selection_ref": ref,
                "catalog_source_kind": "manufacturer_supplement",
                "catalog_source_checksum": supplement.checksum,
                "evidence_checksum": supplement.checksum,
                "product_ref": candidate["product_ref"], "offer_ref": None,
                "family_name": candidate["name"], "company": candidate["company"],
                "product_url": candidate["product"].product_url,
                "application_sources": candidate["application_sources"],
                "source_specifications": candidate["specifications"],
                "status": candidate["status"], "reason_codes": candidate["reason_codes"],
                "checks": candidate["checks"],
            }
        else:
            return _selection_error(request, project, "Неизвестный источник модели.", 400)
        choice["selected_by_user_id"] = request.user.pk
        choice["selected_at"] = timezone.now().isoformat()
        previous = snapshot.get("robot_selection")
        if isinstance(previous, dict) and all(
            previous.get(key) == value for key, value in choice.items()
            if key not in {"selected_at", "selected_by_user_id"}
        ):
            return redirect("project_task", project_id=project.id)
    else:
        return _selection_error(request, project, "Неизвестное действие выбора модели.", 400)
    try:
        new_revision = _append_snapshot_change(project, "robot_selection", choice, expected)
    except ProfileValidationError:
        return _selection_error(request, project, "Проект изменился. Откройте актуальную версию и повторите выбор.", 409)
    return redirect(f"/projects/{project.id}/task/?revision={new_revision.number}")


@login_required
@require_http_methods(["GET", "POST"])
def project_topology(request, project_id):
    project = get_object_or_404(Project, id=project_id, owner=request.user)
    latest = project.revisions.first()
    revision_text = request.GET.get("revision") if request.method == "GET" else None
    if revision_text is not None:
        if not revision_text.isdecimal():
            raise Http404("Ревизия не найдена")
        revision = get_object_or_404(project.revisions, number=int(revision_text))
    else:
        revision = latest
    task = revision.scenario_snapshot.get("task_profile")
    process = process_for(project.object_slug, task.get("process")) if isinstance(task, dict) else None
    if process is None:
        return redirect("project_task", project_id=project.id)
    topology = deepcopy(revision.scenario_snapshot.get("topology_profile") or
                        empty_topology(project.object_slug, process.code))
    historical = revision.pk != latest.pk
    action = request.POST.get("action") if request.method == "POST" else None
    edit_node_id = (request.POST.get("node_id") if action == "update_node" else
                    request.GET.get("node"))
    edit_edge_id = (request.POST.get("edge_id") if action == "update_edge" else
                    request.GET.get("edge"))
    editing_node = next((node for node in topology["nodes"] if node["id"] == edit_node_id), None)
    editing_edge = next((edge for edge in topology["edges"] if edge["id"] == edit_edge_id), None)
    if (edit_node_id and editing_node is None) or (edit_edge_id and editing_edge is None):
        raise Http404("Элемент маршрута не найден")
    node_form = TopologyNodeForm(request.POST if action in {"add_node", "update_node"} else None,
                                 object_slug=project.object_slug, initial=editing_node)
    edge_form = TopologyEdgeForm(request.POST if action in {"add_edge", "update_edge"} else None,
                                 topology=topology, initial=editing_edge)
    route_form = TopologyRouteForm(request.POST if action == "set_route" else None, topology=topology,
                                   initial={"origin": topology["origin"],
                                            "destination": topology["destination"],
                                            "route_flow": topology.get("route_flow")})
    status = 200
    if request.method == "POST":
        if historical:
            return _selection_error(request, project, "Откройте актуальную ревизию проекта.", 409)
        try:
            expected = int(request.POST.get("base_revision", ""))
        except ValueError:
            expected = None
        if expected != latest.number:
            return _selection_error(request, project, "Проект изменился. Откройте актуальную версию.", 409)
        if action in {"add_node", "update_node"} and node_form.is_valid():
            data = node_form.cleaned_data
            node = {
                "id": edit_node_id if action == "update_node" else str(uuid4()),
                "label": data["label"], "floor": data["floor"],
                "source": data["source"], "zone": data.get("zone") or None,
                "coordinate_source": data.get("coordinate_source") or None,
                "x_m": str(data["x_m"]) if data["x_m"] is not None else None,
                "y_m": str(data["y_m"]) if data["y_m"] is not None else None,
            }
            if action == "update_node":
                nodes = {item["id"]: item for item in topology["nodes"]}
                for edge in topology["edges"]:
                    other = nodes[edge["end"] if edge["start"] == edit_node_id else edge["start"]] if edit_node_id in {edge["start"], edge["end"]} else None
                    if other and node["floor"] != other["floor"] and (
                            project.object_slug != "hospital" or edge["kind"] != "elevator"):
                        node_form.add_error("floor", "Существующий участок не допускает такой переход между уровнями.")
                        break
                if not node_form.errors:
                    topology["nodes"] = [node if item["id"] == edit_node_id else item
                                         for item in topology["nodes"]]
                else:
                    status = 400
            else:
                topology["nodes"].append(node)
        elif action in {"add_edge", "update_edge"} and edge_form.is_valid():
            data = edge_form.cleaned_data
            edge = {
                "id": edit_edge_id if action == "update_edge" else str(uuid4()),
                "start": data["start"], "end": data["end"],
                "source": data["source"], "length_m": str(data["length_m"]) if data["length_m"] is not None else None,
                "length_source": data.get("length_source") or None,
                "bidirectional": data["bidirectional"], "access": data.get("access"),
                "kind": data.get("kind") or "passage", "flow": data.get("flow"),
            }
            if action == "update_edge":
                topology["edges"] = [edge if item["id"] == edit_edge_id else item
                                     for item in topology["edges"]]
            else:
                topology["edges"].append(edge)
        elif action == "set_route" and route_form.is_valid():
            data = route_form.cleaned_data
            topology["origin"] = data["origin"]
            topology["destination"] = data["destination"]
            topology["route_flow"] = data.get("route_flow")
        elif action == "remove_node":
            node_id = request.POST.get("node_id")
            if not any(node["id"] == node_id for node in topology["nodes"]):
                return _selection_error(request, project, "Точка не найдена.", 400)
            topology["nodes"] = [node for node in topology["nodes"] if node["id"] != node_id]
            topology["edges"] = [edge for edge in topology["edges"]
                                 if node_id not in {edge["start"], edge["end"]}]
            if node_id in {topology["origin"], topology["destination"]}:
                topology["origin"] = topology["destination"] = None
        elif action == "remove_edge":
            edge_id = request.POST.get("edge_id")
            if not any(edge["id"] == edge_id for edge in topology["edges"]):
                return _selection_error(request, project, "Соединение не найдено.", 400)
            topology["edges"] = [edge for edge in topology["edges"] if edge["id"] != edge_id]
        else:
            status = 400
        if status == 200:
            topology["attested_by_user_id"] = request.user.pk
            topology["attested_at"] = timezone.now().isoformat()
            try:
                new_revision = _append_snapshot_change(project, "topology_profile", topology, expected)
            except ProfileValidationError:
                return _selection_error(request, project, "Проект изменился. Откройте актуальную версию.", 409)
            return redirect(f"/projects/{project.id}/topology/?revision={new_revision.number}")
    route = route_result(topology)
    cycle = transport_cycle_route(topology, route)
    return render(request, "projects/topology.html", {
        "project": project, "revision": revision, "process": process,
        "historical": historical, "topology": topology, "route": route, "cycle": cycle,
        "drawings": floor_drawings(topology, route),
        "node_form": node_form, "edge_form": edge_form, "route_form": route_form,
        "base_revision": latest.number, "form_error": status == 400,
        "editing_node": editing_node, "editing_edge": editing_edge,
    }, status=status)


def _manufacturer_speed_limits(snapshot):
    selection = snapshot.get("robot_selection") or {}
    ref = selection_ref(selection, snapshot)
    if ref is None:
        return []
    if ref["catalog_source_kind"] == "manufacturer_supplement":
        batch = publicly_available_supplement(ref["catalog_source_checksum"])
        if batch is None:
            return []
        return [{"value": spec.value, "unit": spec.unit, "source_url": spec.source.url,
                 "use": spec.use}
                for spec in SupplementSpecification.objects.filter(
                    product__batch=batch, product__product_ref=ref["product_ref"],
                    attribute="manufacturer_max_speed_m_s",
                ).select_related("source")]
    return list(CatalogEvidenceClaim.objects.filter(
        evidence_batch__checksum=snapshot.get("evidence_checksum"),
        evidence_batch__catalog_batch__checksum=snapshot.get("catalog_checksum"),
        catalog_rows__record_index=ref["record_index"],
        attribute="manufacturer_max_speed_m_s", use__in=("matching_limit", "blocked"),
    ).distinct().values("value", "unit", "source_url", "use"))


@login_required
@require_http_methods(["GET", "POST"])
def project_sizing(request, project_id):
    project = get_object_or_404(Project, id=project_id, owner=request.user)
    latest = project.revisions.first()
    revision_text = request.GET.get("revision") if request.method == "GET" else None
    if revision_text is not None:
        if not revision_text.isdecimal():
            raise Http404("Ревизия не найдена")
        revision = get_object_or_404(project.revisions, number=int(revision_text))
    else:
        revision = latest
    snapshot = revision.scenario_snapshot
    task = snapshot.get("task_profile") or {}
    process = process_for(project.object_slug, task.get("process")) if isinstance(task, dict) else None
    if process is None:
        return redirect("project_task", project_id=project.id)
    selection = snapshot.get("robot_selection") or {}
    profile = snapshot.get("workload_profile") or {}
    robot_ref = selection_ref(selection, snapshot)
    previous = profile if (profile.get("process") == process.code
                           and workload_matches_selection(profile, selection, snapshot)) else None
    form = WorkloadProfileForm(request.POST if request.method == "POST" else None,
                               process=process, selection=selection,
                               selection_ref=robot_ref, previous=previous)
    status = 200
    if request.method == "POST":
        if form.is_valid():
            workload = form.to_profile(user_id=request.user.pk, recorded_at=timezone.now())
            try:
                expected = int(request.POST.get("base_revision", ""))
                if expected != latest.number:
                    raise ProfileValidationError("Проект изменился")
                if workload["parameters"] == (previous or {}).get("parameters", {}):
                    return redirect("project_sizing", project_id=project.id)
                new_revision = _append_snapshot_change(project, "workload_profile", workload, expected)
            except (ProfileValidationError, ValueError):
                form.add_error(None, "Проект изменился: откройте актуальную ревизию")
                status = 409
            else:
                return redirect(f"/projects/{project.id}/sizing/?revision={new_revision.number}")
        else:
            status = 400
    return render(request, "projects/sizing.html", {
        "project": project, "revision": revision, "historical": revision.pk != latest.pk,
        "process": process, "selection": selection, "form": form,
        "field_rows": [(label, unit, form[key], form[f"{key}_source"])
                       for key, label, unit in sizing_fields(process)],
        "result": size_project(snapshot, manufacturer_speed_limits=_manufacturer_speed_limits(snapshot)),
        "base_revision": latest.number,
    }, status=status)


@login_required
@require_http_methods(["GET", "POST"])
def project_operation_log(request, project_id):
    project = get_object_or_404(Project, id=project_id, owner=request.user)
    latest = project.revisions.first()
    revision_text = request.GET.get("revision") if request.method == "GET" else None
    if revision_text is not None:
        if len(revision_text) > 9 or not revision_text.isdecimal():
            raise Http404("Ревизия не найдена")
        revision = get_object_or_404(project.revisions, number=int(revision_text))
    else:
        revision = latest
    task = revision.scenario_snapshot.get("task_profile") or {}
    process = process_for(project.object_slug, task.get("process")) if isinstance(task, dict) else None
    if process is None:
        return redirect("project_task", project_id=project.id)
    log_ref = revision.scenario_snapshot.get("operation_log_ref") or {}
    saved_log = (OperationLog.objects.filter(
        project=project, process=process.code,
        pk=log_ref.get("id"), sha256=log_ref.get("sha256"),
    ).first() if isinstance(log_ref, dict) and log_ref.get("id") else None)
    form = OperationLogUploadForm(
        request.POST if request.method == "POST" else None,
        request.FILES if request.method == "POST" else None,
    )
    status = 200
    if request.method == "POST":
        if form.is_valid():
            try:
                expected = int(request.POST.get("base_revision", ""))
            except ValueError:
                expected = None
            if expected != latest.number:
                form.add_error(None, "Проект изменился: откройте актуальную ревизию.")
                status = 409
            else:
                upload = form.cleaned_data["file"]
                if upload.size > MAX_UPLOAD_BYTES:
                    form.add_error("file", "Файл превышает допустимый размер 2 МиБ.")
                    status = 400
                else:
                    raw = upload.read(MAX_UPLOAD_BYTES + 1)
                    try:
                        rows = parse_operation_log(raw, process)
                        validate_observation_period(
                            rows, form.cleaned_data["period_start"],
                            form.cleaned_data["period_end"],
                        )
                    except OperationLogError as exc:
                        form.add_error("file", str(exc))
                        status = 400
                    else:
                        checksum = hashlib.sha256(raw).hexdigest()
                        try:
                            with transaction.atomic():
                                log, _ = OperationLog.objects.get_or_create(
                                    project=project, process=process.code, sha256=checksum,
                                    parser_version=PARSER_VERSION,
                                    period_start_at=form.cleaned_data["period_start"],
                                    period_end_at=form.cleaned_data["period_end"],
                                    defaults={
                                        "uploaded_by": request.user,
                                        "source_description": form.cleaned_data["source_description"],
                                        "raw_csv": raw, "rows": rows,
                                    },
                                )
                                ref = {"id": str(log.id), "sha256": checksum,
                                       "process": process.code, "parser_version": PARSER_VERSION,
                                       "period_start_at": log.period_start_at.isoformat(),
                                       "period_end_at": log.period_end_at.isoformat()}
                                if ref == (latest.scenario_snapshot.get("operation_log_ref") or {}):
                                    return redirect("project_operation_log", project_id=project.id)
                                saved_revision = _append_snapshot_change(
                                    project, "operation_log_ref", ref, expected,
                                )
                        except ProfileValidationError:
                            form.add_error(None, "Проект изменился: откройте актуальную ревизию.")
                            status = 409
                        else:
                            return redirect(
                                f"/projects/{project.id}/operations/?revision={saved_revision.number}"
                            )
        else:
            status = 400
    return render(request, "projects/operation_log.html", {
        "project": project, "revision": revision, "process": process,
        "form": form, "saved_log": saved_log,
        "first_at": saved_log.rows[0]["requested_at_utc"] if saved_log else None,
        "last_at": saved_log.rows[-1]["requested_at_utc"] if saved_log else None,
        "historical": revision.pk != latest.pk, "base_revision": latest.number,
        "max_upload_mib": MAX_UPLOAD_BYTES // (1024 * 1024),
    }, status=status)


@login_required
@require_http_methods(["GET", "POST"])
def project_availability(request, project_id):
    project = get_object_or_404(Project, id=project_id, owner=request.user)
    latest = project.revisions.first()
    revision_text = request.GET.get("revision") if request.method == "GET" else None
    if revision_text is not None:
        if len(revision_text) > 9 or not revision_text.isdecimal():
            raise Http404("Ревизия не найдена")
        revision = get_object_or_404(project.revisions, number=int(revision_text))
    else:
        revision = latest
    snapshot = revision.scenario_snapshot
    task = snapshot.get("task_profile") or {}
    process = process_for(project.object_slug, task.get("process")) if isinstance(task, dict) else None
    if process is None:
        return redirect("project_task", project_id=project.id)
    log_ref = snapshot.get("operation_log_ref") or {}
    log = (OperationLog.objects.filter(
        project=project, process=process.code,
        pk=log_ref.get("id"), sha256=log_ref.get("sha256"),
    ).first() if isinstance(log_ref, dict) and log_ref.get("id") else None)
    sizing = size_project(snapshot, manufacturer_speed_limits=_manufacturer_speed_limits(snapshot))
    selection = snapshot.get("robot_selection") or {}
    robot_ref = selection_ref(selection, snapshot)
    record_index = robot_ref.get("record_index") if robot_ref else None
    robot_key = selection_key(robot_ref) if robot_ref else None
    fleet = sizing.get("fleet")
    topology = snapshot.get("topology_profile") or {}
    origin_node = topology.get("origin") if isinstance(topology, dict) else None
    ready = (log is not None and log.period_start_at is not None
             and log.period_end_at is not None and sizing.get("status") == "estimated"
             and isinstance(fleet, int) and not isinstance(fleet, bool) and fleet > 0
             and robot_ref is not None and isinstance(origin_node, str) and bool(origin_node))
    availability_ref = snapshot.get("availability_ref") or {}
    saved_plan = (AvailabilityPlan.objects.filter(
        project=project, operation_log=log, process=process.code,
        pk=availability_ref.get("id"), sha256=availability_ref.get("sha256"),
    ).first() if log and isinstance(availability_ref, dict) and availability_ref.get("id") else None)
    form = AvailabilityUploadForm(
        request.POST if request.method == "POST" else None,
        request.FILES if request.method == "POST" else None,
    )
    status = 200
    if request.method == "POST":
        if revision.pk != latest.pk or not ready:
            form.add_error(None, "Сначала сохраните журнал, проверенный маршрут и расчёт парка в текущей ревизии.")
            status = 409
        elif form.is_valid():
            try:
                expected = int(request.POST.get("base_revision", ""))
            except ValueError:
                expected = None
            if expected != latest.number:
                form.add_error(None, "Проект изменился: откройте актуальную ревизию.")
                status = 409
            else:
                upload = form.cleaned_data["file"]
                if upload.size > MAX_UPLOAD_BYTES:
                    form.add_error("file", "Файл превышает допустимый размер 2 МиБ.")
                    status = 400
                else:
                    raw = upload.read(MAX_UPLOAD_BYTES + 1)
                    try:
                        verified_operation_rows(log, process)
                        rows = parse_availability(
                            raw, period_start=log.period_start_at,
                            period_end=log.period_end_at, fleet=fleet,
                            origin_node=origin_node,
                        )
                    except (AvailabilityError, OperationLogError) as exc:
                        form.add_error("file", str(exc))
                        status = 400
                    else:
                        checksum = hashlib.sha256(raw).hexdigest()
                        try:
                            with transaction.atomic():
                                plan, _ = AvailabilityPlan.objects.get_or_create(
                                    project=project, operation_log=log, process=process.code,
                                    robot_record_index=record_index, selection_key=robot_key, fleet=fleet,
                                    sha256=checksum, parser_version=AVAILABILITY_PARSER_VERSION,
                                    defaults={
                                        "uploaded_by": request.user,
                                        "source_description": form.cleaned_data["source_description"],
                                        "raw_csv": raw, "rows": rows,
                                    },
                                )
                                ref = {
                                    "id": str(plan.id), "sha256": checksum,
                                    "operation_log_id": str(log.id), "process": process.code,
                                    "robot_record_index": record_index, "fleet": fleet,
                                    "selection_ref": robot_ref, "selection_key": robot_key,
                                    "parser_version": AVAILABILITY_PARSER_VERSION,
                                    "origin_node": origin_node,
                                }
                                if ref == (latest.scenario_snapshot.get("availability_ref") or {}):
                                    return redirect("project_availability", project_id=project.id)
                                saved_revision = _append_snapshot_change(
                                    project, "availability_ref", ref, expected,
                                )
                        except ProfileValidationError:
                            form.add_error(None, "Проект изменился: откройте актуальную ревизию.")
                            status = 409
                        else:
                            return redirect(
                                f"/projects/{project.id}/availability/?revision={saved_revision.number}"
                            )
        else:
            status = 400
    return render(request, "projects/availability.html", {
        "project": project, "revision": revision, "process": process,
        "log": log, "sizing": sizing, "ready": ready, "saved_plan": saved_plan,
        "form": form, "historical": revision.pk != latest.pk,
        "base_revision": latest.number,
        "max_upload_mib": MAX_UPLOAD_BYTES // (1024 * 1024),
        "origin_node": origin_node,
    }, status=status)


@login_required
@require_http_methods(["GET", "POST"])
def project_simulation(request, project_id):
    project = get_object_or_404(Project, id=project_id, owner=request.user)
    latest = project.revisions.first()
    revision_text = request.GET.get("revision") if request.method == "GET" else None
    if revision_text is not None:
        if len(revision_text) > 9 or not revision_text.isdecimal():
            raise Http404("Ревизия не найдена")
        revision = get_object_or_404(project.revisions, number=int(revision_text))
    else:
        revision = latest
    snapshot = revision.scenario_snapshot
    task = snapshot.get("task_profile") or {}
    process = process_for(project.object_slug, task.get("process")) if isinstance(task, dict) else None
    if process is None:
        return redirect("project_task", project_id=project.id)
    log_ref = snapshot.get("operation_log_ref") or {}
    log = (OperationLog.objects.filter(
        project=project, process=process.code,
        pk=log_ref.get("id"), sha256=log_ref.get("sha256"),
    ).first() if isinstance(log_ref, dict) and log_ref.get("id") else None)
    plan_ref = snapshot.get("availability_ref") or {}
    plan = (AvailabilityPlan.objects.filter(
        project=project, operation_log=log, process=process.code,
        pk=plan_ref.get("id"), sha256=plan_ref.get("sha256"),
    ).first() if log and isinstance(plan_ref, dict) and plan_ref.get("id") else None)
    sizing = size_project(snapshot, manufacturer_speed_limits=_manufacturer_speed_limits(snapshot))
    topology = snapshot.get("topology_profile") or {}
    origin_node = topology.get("origin") if isinstance(topology, dict) else None
    selection = snapshot.get("robot_selection") or {}
    if not isinstance(selection, dict):
        selection = {}
    valid_plan = bool(plan and plan_ref.get("operation_log_id") == str(log.id)
                      and availability_matches_selection(plan_ref, plan, selection, snapshot)
                      and plan_ref.get("fleet") == plan.fleet
                      and plan_ref.get("parser_version") == plan.parser_version
                      and plan_ref.get("origin_node") == origin_node
                      and plan.parser_version == AVAILABILITY_PARSER_VERSION
                      and plan.fleet == sizing.get("fleet"))
    ready = (is_transport(process) and valid_plan and sizing.get("status") == "estimated"
             and sizing.get("cycle_seconds") is not None
             and sizing.get("handoff_seconds") is not None
             and log.period_start_at is not None and log.period_end_at is not None)
    run = None
    run_text = request.GET.get("run") if request.method == "GET" else None
    if run_text is not None:
        try:
            run_id = UUID(run_text)
        except (ValueError, AttributeError):
            raise Http404("Прогон не найден")
        run = get_object_or_404(SimulationRun, id=run_id, project=project, revision=revision)
    elif request.method == "GET":
        run = SimulationRun.objects.filter(project=project, revision=revision).order_by("-created_at").first()
    event_page = []
    page_number = page_count = 1
    status = 200
    error = None
    scene = None
    playback = None
    if run:
        try:
            _, _, _, calendar_rows = _verified_transport_run(run, project)
            timeline = playback_events(run.ledger, calendar_rows)
            page_text = request.GET.get("page", "1")
            if len(page_text) > 7 or not page_text.isdecimal() or int(page_text) < 1:
                raise Http404("Страница событий не найдена")
            page_number = int(page_text)
            page_count = (len(timeline) + 99) // 100
            if page_number > page_count:
                raise Http404("Страница событий не найдена")
            event_page = timeline[(page_number - 1) * 100:page_number * 100]
            saved_snapshot = run.input_snapshot["scenario"]
            scene = measured_scene(saved_snapshot)
            initial_time = (
                timeline[(page_number - 1) * 100 - 1]["at_s"]
                if page_number > 1 else "0"
            )
            playback = {
                "initial": state_before(timeline, (page_number - 1) * 100),
                "initial_at_s": initial_time,
                "initial_availability": availability_at_events(
                    calendar_rows, [{"at_s": initial_time}],
                    run.ledger["period_start_utc"],
                )[0],
                "events": event_page,
                "availability": availability_at_events(
                    calendar_rows, event_page, run.ledger["period_start_utc"],
                ),
                "motion": movement_timeline(
                    saved_snapshot, run.input_snapshot["sizing"], run.ledger, scene,
                    page_start=initial_time,
                    page_end=event_page[-1]["at_s"] if event_page else initial_time,
                ),
            }
        except (OperationLogError, AvailabilityError, EventInputError,
                PlaybackDataError, KeyError, TypeError, AttributeError,
                InvalidOperation, ValueError) as exc:
            error, status = str(exc), 409
            run = None
    if request.method == "POST":
        try:
            expected = int(request.POST.get("base_revision", ""))
        except ValueError:
            expected = None
        if expected != latest.number:
            error, status = "Проект изменился: откройте актуальную ревизию.", 409
        elif not ready:
            error, status = "Для прогона сохраните журнал, календарь и измеренный транспортный цикл.", 409
        else:
            try:
                jobs = verified_operation_rows(log, process)
                rows = verified_availability_rows(
                    plan, period_start=log.period_start_at, period_end=log.period_end_at,
                    origin_node=origin_node,
                )
                source = f"{plan.source_description}; SHA-256 {plan.sha256}"
                ledger = schedule_observed_jobs(
                    [{**job, "service_seconds": sizing["cycle_seconds"],
                      "handoff_seconds": sizing["handoff_seconds"]} for job in jobs],
                    available_windows(rows, source=source),
                    period_start=log.period_start_at, period_end=log.period_end_at,
                )
            except (OperationLogError, AvailabilityError, EventInputError) as exc:
                error, status = str(exc), 409
            else:
                input_snapshot = {
                    "scenario": deepcopy(snapshot), "sizing": sizing,
                    "selection_key": selection_key(selection_ref(selection, snapshot)),
                    "operation_log_sha256": log.sha256,
                    "availability_sha256": plan.sha256,
                    "operation_log_parser_version": log.parser_version,
                    "availability_parser_version": plan.parser_version,
                    "cycle_semantics": "complete_robot_cycle",
                    "delivery_semantics": "handoff_after_outbound_and_unloading",
                }
                run, _ = SimulationRun.objects.get_or_create(
                    project=project, revision=revision, operation_log=log,
                    availability_plan=plan, ledger_version=LEDGER_VERSION,
                    defaults={"created_by": request.user,
                              "input_snapshot": input_snapshot, "ledger": ledger},
                )
                return redirect(
                    f"/projects/{project.id}/simulation/?revision={revision.number}&run={run.id}"
                )
    parent_run = None
    if run:
        change = (run.input_snapshot.get("scenario") or {}).get("demand_what_if") or {}
        if change:
            try:
                parent_id = UUID(change["parent_run_id"])
                parent_run = SimulationRun.objects.select_related(
                    "revision", "operation_log", "availability_plan",
                ).get(id=parent_id, project=project,
                      revision__number=change["parent_revision"])
                _verified_transport_run(parent_run, project)
            except (KeyError, TypeError, ValueError, SimulationRun.DoesNotExist,
                    PlaybackDataError, DemandRevisionError, OperationLogError, AvailabilityError,
                    EventInputError):
                error, status, parent_run = "Исходный прогон сравнения недоступен.", 409, None
    return render(request, "projects/simulation.html", {
        "project": project, "revision": revision, "process": process,
        "log": log, "plan": plan, "sizing": sizing, "ready": ready,
        "run": run, "parent_run": parent_run, "event_page": event_page,
        "scene": scene, "playback": playback,
        "page_number": page_number, "page_count": page_count,
        "previous_page": page_number - 1 if page_number > 1 else None,
        "next_page": page_number + 1 if page_number < page_count else None,
        "historical": revision.pk != latest.pk,
        "base_revision": latest.number, "error": error,
    }, status=status)


def _verified_transport_run(run, project):
    """Reproduce a saved transport run from its pinned raw inputs."""
    snapshot = run.revision.scenario_snapshot
    if not isinstance(snapshot, dict) or not isinstance(run.input_snapshot, dict):
        raise PlaybackDataError("Снимок сохранённого прогона повреждён.")
    task = snapshot.get("task_profile") or {}
    if not isinstance(task, dict):
        raise PlaybackDataError("Операция сохранённого прогона повреждена.")
    process = process_for(project.object_slug, task.get("process"))
    if process is None or not is_transport(process):
        raise PlaybackDataError("Для этой операции нет транспортного прогона.")
    log_ref = snapshot.get("operation_log_ref") or {}
    plan_ref = snapshot.get("availability_ref") or {}
    if not isinstance(log_ref, dict) or not isinstance(plan_ref, dict):
        raise PlaybackDataError("Источники сохранённого прогона повреждены.")
    if (run.input_snapshot.get("scenario") != snapshot
            or not availability_matches_selection(
                plan_ref, run.availability_plan, snapshot.get("robot_selection"), snapshot,
            )
            or (run.availability_plan.selection_key is not None
                and run.input_snapshot.get("selection_key") != run.availability_plan.selection_key)
            or run.input_snapshot.get("operation_log_sha256") != run.operation_log.sha256
            or run.input_snapshot.get("availability_sha256") != run.availability_plan.sha256
            or log_ref.get("id") != str(run.operation_log_id)
            or log_ref.get("sha256") != run.operation_log.sha256
            or plan_ref.get("id") != str(run.availability_plan_id)
            or plan_ref.get("sha256") != run.availability_plan.sha256
            or plan_ref.get("operation_log_id") != str(run.operation_log_id)
            or run.ledger_version != LEDGER_VERSION
            or run.operation_log.project_id != project.id
            or run.availability_plan.project_id != project.id
            or run.availability_plan.operation_log_id != run.operation_log_id
            or run.operation_log.process != process.code
            or run.availability_plan.process != process.code
            or run.availability_plan.parser_version != AVAILABILITY_PARSER_VERSION
            or run.input_snapshot.get("delivery_semantics") != "handoff_after_outbound_and_unloading"):
        raise PlaybackDataError("Исходный прогон расходится с сохранёнными данными.")
    sizing = size_project(snapshot, manufacturer_speed_limits=_manufacturer_speed_limits(snapshot))
    if sizing != run.input_snapshot.get("sizing") or sizing.get("status") != "estimated":
        raise PlaybackDataError("Исходный расчёт парка не воспроизводится.")
    jobs = verified_operation_rows(run.operation_log, process)
    calendar = verified_availability_rows(
        run.availability_plan,
        period_start=run.operation_log.period_start_at,
        period_end=run.operation_log.period_end_at,
        origin_node=(snapshot.get("topology_profile") or {}).get("origin"),
    )
    source = f"{run.availability_plan.source_description}; SHA-256 {run.availability_plan.sha256}"
    ledger = schedule_observed_jobs(
        [{**job, "service_seconds": sizing["cycle_seconds"],
          "handoff_seconds": sizing["handoff_seconds"]} for job in jobs],
        available_windows(calendar, source=source),
        period_start=run.operation_log.period_start_at,
        period_end=run.operation_log.period_end_at,
    )
    if ledger != run.ledger:
        raise PlaybackDataError("События исходного прогона не воспроизводятся.")
    return process, sizing, jobs, calendar


@login_required
@require_http_methods(["GET", "POST"])
def project_demand_revision(request, project_id):
    project = get_object_or_404(Project, id=project_id, owner=request.user)
    run_text = request.GET.get("run") if request.method == "GET" else request.POST.get("run")
    try:
        run_id = UUID(run_text)
    except (TypeError, ValueError, AttributeError):
        raise Http404("Прогон не найден")
    run = get_object_or_404(
        SimulationRun.objects.select_related("revision", "operation_log", "availability_plan"),
        id=run_id, project=project,
    )
    latest = project.revisions.first()
    form = DemandRevisionForm(request.POST if request.method == "POST" else None,
                              request.FILES if request.method == "POST" else None)
    error = None
    status = 200
    parent_sizing = None
    if run.revision_id != latest.id:
        error, status = "Проект изменился: откройте актуальный прогон.", 409
    else:
        try:
            process, parent_sizing, parent_jobs, _ = _verified_transport_run(run, project)
        except (PlaybackDataError, DemandRevisionError, OperationLogError, AvailabilityError,
                EventInputError, KeyError, TypeError, AttributeError, ValueError) as exc:
            error, status = str(exc), 409
    if request.method == "POST" and error is None:
        if not form.is_valid():
            status = 400
        elif str(latest.number) != request.POST.get("base_revision"):
            error, status = "Проект изменился: откройте актуальный прогон.", 409
        else:
            upload = form.cleaned_data["file"]
            if upload.size > MAX_UPLOAD_BYTES:
                form.add_error("file", "Файл превышает допустимый размер 2 МиБ.")
                status = 400
            else:
                raw = upload.read(MAX_UPLOAD_BYTES + 1)
                try:
                    jobs = parse_operation_log(raw, process)
                    validate_observation_period(
                        jobs, run.operation_log.period_start_at,
                        run.operation_log.period_end_at,
                    )
                    if jobs == parent_jobs:
                        raise DemandRevisionError("Журнал заданий совпадает с исходным прогоном.")
                    peak = form.cleaned_data["peak_jobs_per_h"]
                    if peak <= 0 or peak < minimum_observed_peak(jobs):
                        raise DemandRevisionError("Пиковый поток меньше числа заданий в одном часе нового журнала.")
                    checksum = hashlib.sha256(raw).hexdigest()
                    ref = {
                        "sha256": checksum, "process": process.code,
                        "parser_version": PARSER_VERSION,
                        "period_start_at": run.operation_log.period_start_at.isoformat(),
                        "period_end_at": run.operation_log.period_end_at.isoformat(),
                    }
                    revised = revised_demand_snapshot(
                        latest.scenario_snapshot, process=process, peak=peak,
                        peak_source=form.cleaned_data["peak_source"],
                        log_ref=ref, parent_run_id=run.id,
                        parent_revision=latest.number, user_id=request.user.pk,
                        at=timezone.now(),
                    )
                    sizing = size_project(
                        revised, manufacturer_speed_limits=_manufacturer_speed_limits(revised),
                    )
                    if sizing.get("status") != "estimated" or not 0 < sizing["fleet"] <= MAX_ROWS:
                        raise DemandRevisionError("Новый поток нельзя обслужить в пределах допустимого парка.")
                except (OperationLogError, DemandRevisionError, KeyError,
                        TypeError, AttributeError, ValueError) as exc:
                    form.add_error("file", str(exc))
                    status = 400
                else:
                    try:
                        with transaction.atomic():
                            locked = Project.objects.select_for_update().get(pk=project.pk)
                            if locked.revisions.first().pk != latest.pk:
                                raise ProfileValidationError("Проект изменился")
                            log, _ = OperationLog.objects.get_or_create(
                                project=project, process=process.code, sha256=checksum,
                                parser_version=PARSER_VERSION,
                                period_start_at=run.operation_log.period_start_at,
                                period_end_at=run.operation_log.period_end_at,
                                defaults={
                                    "uploaded_by": request.user,
                                    "source_description": form.cleaned_data["source_description"],
                                    "raw_csv": raw, "rows": jobs,
                                },
                            )
                            revised["operation_log_ref"]["id"] = str(log.id)
                            revision = ProjectRevision.objects.create(
                                project=project, number=latest.number + 1,
                                scenario_snapshot=revised,
                            )
                            Project.objects.filter(pk=project.pk).update(updated_at=timezone.now())
                    except ProfileValidationError:
                        error, status = "Проект изменился: откройте актуальный прогон.", 409
                    else:
                        return redirect(
                            f"/projects/{project.id}/availability/?revision={revision.number}"
                        )
    return render(request, "projects/demand_revision.html", {
        "project": project, "run": run, "form": form,
        "base_revision": latest.number, "parent_sizing": parent_sizing,
        "error": error, "max_upload_mib": MAX_UPLOAD_BYTES // (1024 * 1024),
    }, status=status)


@login_required
@require_http_methods(["GET", "POST"])
def project_finance(request, project_id):
    project = get_object_or_404(Project, id=project_id, owner=request.user)
    run_text = request.GET.get("run") or request.POST.get("run")
    if run_text:
        try:
            run_id = UUID(run_text)
        except (TypeError, ValueError, AttributeError):
            raise Http404("Прогон не найден")
        run = get_object_or_404(SimulationRun.objects.select_related(
            "revision", "operation_log", "availability_plan",
        ), id=run_id, project=project)
    else:
        run = (SimulationRun.objects.filter(project=project)
               .select_related("revision", "operation_log", "availability_plan")
               .order_by("-created_at").first())
        if run is None:
            return redirect("project_simulation", project_id=project.id)

    is_variant_post = request.method == "POST" and request.POST.get("action") == "variant"
    form = FinancePlanUploadForm(
        request.POST if request.method == "POST" and not is_variant_post else None,
        request.FILES if request.method == "POST" and not is_variant_post else None,
    )
    variant_form = FinanceVariantForm(request.POST if is_variant_post else None)
    selected_plan = None
    plan_text = request.POST.get("plan") if is_variant_post else request.GET.get("plan")
    if plan_text:
        try:
            plan_id = UUID(plan_text)
        except (TypeError, ValueError, AttributeError):
            raise Http404("Финансовый план не найден")
        selected_plan = get_object_or_404(FinancePlan, id=plan_id, project=project, simulation_run=run)
    elif request.method == "GET":
        selected_plan = FinancePlan.objects.filter(
            project=project, simulation_run=run,
        ).order_by("-created_at").first()

    error = None
    status = 200
    result = None
    selected_variant = None
    variant_result = None
    try:
        _verified_transport_run(run, project)
        delivered = Decimal(str(run.ledger["delivered_work_units"]))
        if not delivered.is_finite() or delivered < 0:
            raise FinanceInputError("Неверный доставленный объём сохранённого прогона.")
        if selected_plan:
            result = verified_finance_result(selected_plan)
            variant_text = request.GET.get("variant") if request.method == "GET" else None
            if variant_text:
                try:
                    variant_id = UUID(variant_text)
                except (TypeError, ValueError, AttributeError):
                    raise Http404("Вариант не найден")
                selected_variant = get_object_or_404(
                    FinanceVariant.objects.select_related("base_plan"),
                    id=variant_id, base_plan=selected_plan,
                )
                _, variant_result = verified_finance_variant(selected_variant)
    except (KeyError, TypeError, AttributeError, InvalidOperation,
            OperationLogError, AvailabilityError, EventInputError,
            PlaybackDataError, FinanceInputError) as exc:
        error, status = str(exc), 409

    if is_variant_post and error is None:
        if selected_plan is None:
            raise Http404("Финансовый план не найден")
        if not variant_form.is_valid():
            status = 400
        else:
            cleaned = variant_form.cleaned_data
            try:
                _, variant_calculation = derive_finance_variant(
                    selected_plan, source_row=cleaned["source_row"],
                    amount=cleaned["amount"], source_date=cleaned["source_date"],
                    source_ref=cleaned["source_ref"],
                )
            except FinanceInputError as exc:
                variant_form.add_error(None, str(exc))
                status = 400
            else:
                ref = cleaned["source_ref"].strip()
                checksum = finance_variant_checksum(
                    plan=selected_plan, source_row=cleaned["source_row"],
                    amount=cleaned["amount"], source_date=cleaned["source_date"],
                    source_ref=ref,
                )
                variant, _ = FinanceVariant.objects.get_or_create(
                    base_plan=selected_plan, checksum=checksum,
                    defaults={
                        "created_by": request.user, "source_row": cleaned["source_row"],
                        "amount": cleaned["amount"], "source_date": cleaned["source_date"],
                        "source_ref": ref, "result": variant_calculation,
                    },
                )
                return redirect(
                    f"/projects/{project.id}/finance/?run={run.id}&plan={selected_plan.id}&variant={variant.id}"
                )
    if request.method == "POST" and not is_variant_post and error is None:
        if not form.is_valid():
            status = 400
        else:
            upload = form.cleaned_data["file"]
            if upload.size > MAX_UPLOAD_BYTES:
                form.add_error("file", "Файл превышает допустимый размер 2 МиБ.")
                status = 400
            else:
                raw = upload.read(MAX_UPLOAD_BYTES + 1)
                horizon = form.cleaned_data["horizon_months"]
                rate = form.cleaned_data["monthly_discount_rate"]
                try:
                    rows = parse_finance_csv(raw, horizon_months=horizon)
                    validate_forecast_anchor(
                        rows, observed_delivered_work_units=delivered,
                    )
                    calculation = calculate_finance(
                        rows, horizon_months=horizon, monthly_discount_rate=rate,
                    )
                except FinanceInputError as exc:
                    form.add_error("file", str(exc))
                    status = 400
                else:
                    metadata = {
                        "source_description": form.cleaned_data["source_description"],
                        "forecast_basis": form.cleaned_data["forecast_basis"],
                        "discount_rate_source": form.cleaned_data["discount_rate_source"],
                        "source_filename": upload.name[:255],
                    }
                    metadata_sha = finance_metadata_sha256(**metadata)
                    plan, _ = FinancePlan.objects.get_or_create(
                        project=project, simulation_run=run,
                        sha256=hashlib.sha256(raw).hexdigest(),
                        metadata_sha256=metadata_sha,
                        horizon_months=horizon, monthly_discount_rate=rate,
                        parser_version=FINANCE_VERSION,
                        defaults={
                            "created_by": request.user, "raw_csv": raw, "rows": rows,
                            "result": calculation, **metadata,
                        },
                    )
                    return redirect(
                        f"/projects/{project.id}/finance/?run={run.id}&plan={plan.id}"
                    )
    month_rows = []
    if result is not None and selected_plan is not None:
        volumes = {row["month"]: row["served_work_units"]
                   for row in selected_plan.rows if row["scenario"] == "baseline"}
        for month in range(result["horizon_months"] + 1):
            month_rows.append({
                "month": month, "units": volumes[month],
                **{scenario: result["scenarios"][scenario]["monthly_cash_cost"][month]
                   for scenario in ("baseline", "purchase", "raas")},
            })
    parent_finance_plan = None
    parent_finance_result = None
    finance_compare_error = None
    demand_change = (run.input_snapshot.get("scenario") or {}).get("demand_what_if") or {}
    if selected_plan and result and demand_change:
        try:
            parent_run = SimulationRun.objects.select_related(
                "revision", "operation_log", "availability_plan",
            ).get(id=UUID(demand_change["parent_run_id"]), project=project,
                  revision__number=demand_change["parent_revision"])
            _verified_transport_run(parent_run, project)
            if any(parent_run.revision.scenario_snapshot.get(key) != run.revision.scenario_snapshot.get(key)
                   for key in ("catalog_checksum", "evidence_checksum", "robot_selection", "task_profile")):
                raise FinanceInputError("У сравниваемых прогонов различается выбор модели или операция.")
            parent_finance_plan = FinancePlan.objects.filter(
                project=project, simulation_run=parent_run,
            ).order_by("-created_at").first()
            if parent_finance_plan:
                parent_finance_result = verified_finance_result(parent_finance_plan)
                if (parent_finance_plan.rows[0]["currency"] != selected_plan.rows[0]["currency"]
                        or parent_finance_plan.rows[0]["vat_mode"] != selected_plan.rows[0]["vat_mode"]
                        or parent_finance_plan.horizon_months != selected_plan.horizon_months
                        or parent_finance_plan.monthly_discount_rate != selected_plan.monthly_discount_rate):
                    parent_finance_result = None
                    finance_compare_error = "Для денежного сравнения нужны одинаковые валюта, НДС, горизонт и ставка дисконтирования."
        except (KeyError, TypeError, ValueError, SimulationRun.DoesNotExist,
                PlaybackDataError, DemandRevisionError, OperationLogError, AvailabilityError,
                EventInputError, FinanceInputError) as exc:
            error, status = str(exc), 409
            parent_finance_plan = parent_finance_result = None
    return render(request, "projects/finance.html", {
        "project": project, "run": run, "plan": selected_plan,
        "result": result, "month_rows": month_rows,
        "form": form, "variant_form": variant_form, "error": error,
        "variant": selected_variant, "variant_result": variant_result,
        "demand_change": demand_change,
        "parent_finance_plan": parent_finance_plan,
        "parent_finance_result": parent_finance_result,
        "finance_compare_error": finance_compare_error,
        "variant_source_row": next((row for row in selected_plan.rows
                                    if selected_variant and row["source_row"] == selected_variant.source_row), None)
                                    if selected_plan else None,
        "variants": FinanceVariant.objects.filter(base_plan=selected_plan).order_by("-created_at")
                    if selected_plan else (),
        "observed_delivered": run.ledger.get("delivered_work_units") if status == 200 else None,
        "max_upload_mib": MAX_UPLOAD_BYTES // (1024 * 1024),
    }, status=status)


@login_required
@require_GET
def project_finance_schema(request, project_id):
    get_object_or_404(Project, id=project_id, owner=request.user)
    response = HttpResponse(",".join(FINANCE_HEADER) + "\r\n", content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="financial-plan-columns.csv"'
    return response


@login_required
@require_GET
def project_finance_source(request, project_id):
    project = get_object_or_404(Project, id=project_id, owner=request.user)
    try:
        plan_id = UUID(request.GET.get("plan", ""))
    except (TypeError, ValueError, AttributeError):
        raise Http404("Финансовый план не найден")
    plan = get_object_or_404(FinancePlan, id=plan_id, project=project)
    try:
        verified_finance_result(plan)
    except FinanceInputError:
        return HttpResponse("Исходный план не прошёл проверку целостности.", status=409)
    response = HttpResponse(bytes(plan.raw_csv), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="financial-plan.csv"'
    return response


@login_required
@require_GET
def project_report_bundle(request, project_id):
    project = get_object_or_404(Project, id=project_id, owner=request.user)
    try:
        run_id = UUID(request.GET.get("run", ""))
        plan_id = UUID(request.GET.get("plan", ""))
    except (TypeError, ValueError, AttributeError):
        raise Http404("Отчёт не найден")
    run = get_object_or_404(
        SimulationRun.objects.select_related("revision", "operation_log", "availability_plan"),
        id=run_id, project=project,
    )
    plan = get_object_or_404(FinancePlan, id=plan_id, project=project, simulation_run=run)
    variant = None
    if request.GET.get("variant"):
        try:
            variant_id = UUID(request.GET["variant"])
        except (TypeError, ValueError, AttributeError):
            raise Http404("Вариант отчёта не найден")
        variant = get_object_or_404(FinanceVariant, id=variant_id, base_plan=plan)
    try:
        _verified_transport_run(run, project)
        result = verified_finance_result(plan)
        if variant:
            finance_rows, result = verified_finance_variant(variant)
        else:
            finance_rows = plan.rows
        validate_forecast_anchor(
            finance_rows, observed_delivered_work_units=run.ledger["delivered_work_units"],
        )
        events = run.ledger["events"]
        if not events:
            raise PlaybackDataError("В сохранённом прогоне нет событий для кадра.")
        requested_event = request.GET.get("event")
        if requested_event is None:
            event_index = next((index for index, event in enumerate(events)
                                if event["type"] == "handoff"),
                               next((index for index, event in enumerate(events)
                                     if event["type"] == "start"), 0))
        elif (len(requested_event) > 8 or not requested_event.isdecimal()
              or int(requested_event) >= len(events)):
            raise Http404("Событие кадра не найдено")
        else:
            event_index = int(requested_event)
        archive = build_report_bundle(
            project, run, plan, result, finance_rows,
            event_index=event_index, variant=variant,
        )
    except Http404:
        raise
    except (OperationLogError, AvailabilityError, EventInputError, PlaybackDataError,
            FinanceInputError, KeyError, TypeError, AttributeError,
            InvalidOperation, ValueError, IndexError) as exc:
        return HttpResponse(str(exc), status=409, content_type="text/plain; charset=utf-8")
    except Exception:
        logger.exception("Report generation failed for run %s", run.id)
        return HttpResponse("Не удалось сформировать отчёт. Повторите попытку позже.",
                            status=503, content_type="text/plain; charset=utf-8")
    response = HttpResponse(archive, content_type="application/zip")
    response["Content-Disposition"] = f'attachment; filename="robot-market-{run.id}.zip"'
    response["Cache-Control"] = "private, no-store"
    return response


@login_required
@require_http_methods(["GET", "POST"])
def project_inputs(request, project_id):
    project = get_object_or_404(Project, id=project_id, owner=request.user)
    latest = project.revisions.first()
    profile = profile_for_revision(latest)
    if not profile["fields"]:
        return redirect("project_import", project_id=project.id)
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
        if request.POST.get("source_attested") != "yes":
            context["error"] = "Подтвердите происхождение данных и отсутствие персональных сведений"
        elif upload is None:
            context["error"] = "Выберите файл XLSX или CSV"
        else:
            try:
                profile = parse_upload(upload, project.object_slug)
                profile["attested_by_user_id"] = request.user.pk
                profile["attested_at"] = timezone.now().isoformat()
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
        if (
            not isinstance(pending, dict)
            or not isinstance(pending.get("created_at"), (int, float))
            or not isinstance(pending.get("revision"), int)
            or not isinstance(pending.get("profile"), dict)
            or not 0 <= time() - pending["created_at"] <= 600
        ):
            context["error"] = "Предпросмотр устарел: загрузите файл снова"
            return render(request, "projects/import.html", context, status=400)
        try:
            revision = _append_revision(project, pending["profile"], pending["revision"])
        except ProfileValidationError as exc:
            context["error"] = str(exc)
            return render(request, "projects/import.html", context, status=409)
        return redirect(f"/projects/{project.id}/?revision={revision.number}")
    if request.method == "POST":
        context["error"] = "Неизвестное действие импорта"
        return render(request, "projects/import.html", context, status=400)
    return render(request, "projects/import.html", context)


@login_required
@require_POST
def project_delete(request, project_id):
    project = get_object_or_404(Project, id=project_id, owner=request.user)
    project.delete()
    return redirect("project_list")
