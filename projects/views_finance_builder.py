"""User-entered commercial terms, saved through the existing finance contract."""

import hashlib
from decimal import InvalidOperation
from uuid import UUID

from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from projects.finance import FINANCE_VERSION, calculate_finance, finance_metadata_sha256
from projects.finance_builder import FinanceBuilderForm, build_cashflow
from projects.models import FinancePlan, Project, SimulationRun


@login_required
@require_http_methods(["GET", "POST"])
def project_finance_builder(request, project_id):
    # Keep the existing transport verifier as the sole acceptance boundary.
    from projects.views import _obsolete_physical_contract, _verified_transport_run

    project = get_object_or_404(Project, id=project_id, owner=request.user)
    try:
        run_id = UUID(request.POST.get("run") or request.GET.get("run") or "")
    except (ValueError, TypeError, AttributeError):
        raise Http404("Прогон не найден")
    run = get_object_or_404(SimulationRun.objects.select_related(
        "revision", "operation_log", "availability_plan"), id=run_id, project=project)
    form = FinanceBuilderForm(request.POST or None)
    error, status, sizing = None, 200, {}
    try:
        _, sizing, _, _ = _verified_transport_run(run, project)
        if _obsolete_physical_contract(run, project):
            raise ValueError("Обновите паспорт операции перед новым финансовым расчётом.")
    except (ValueError, KeyError, TypeError, AttributeError, InvalidOperation) as exc:
        error, status = str(exc), 409
    if request.method == "POST" and error is None:
        if not form.is_valid():
            status = 400
        else:
            values = form.cleaned_data
            seconds = (run.operation_log.period_end_at - run.operation_log.period_start_at).total_seconds()
            try:
                raw, rows = build_cashflow(values, fleet=sizing["fleet"],
                                          delivered=run.ledger["delivered_work_units"],
                                          period_seconds=seconds, run_id=run.id)
                result = calculate_finance(rows, horizon_months=values["horizon_months"],
                                           monthly_discount_rate=values["monthly_discount_rate"])
            except (ValueError, InvalidOperation) as exc:
                form.add_error(None, str(exc))
                status = 400
            else:
                metadata = {
                    "source_description": "Коммерческие условия введены пользователем; источники указаны у денежных статей.",
                    "forecast_basis": f"Повторений периода в месяц: {values['periods_per_month']}. {values['forecast_basis']}",
                    "discount_rate_source": values["discount_rate_source"],
                    "source_filename": "financial-plan.csv",
                }
                plan, _ = FinancePlan.objects.get_or_create(
                    project=project, simulation_run=run, sha256=hashlib.sha256(raw).hexdigest(),
                    metadata_sha256=finance_metadata_sha256(**metadata),
                    horizon_months=values["horizon_months"],
                    monthly_discount_rate=values["monthly_discount_rate"], parser_version=FINANCE_VERSION,
                    defaults={"created_by": request.user, "raw_csv": raw, "rows": rows, "result": result, **metadata},
                )
                return redirect(f"{reverse('project_finance', args=[project.id])}?run={run.id}&plan={plan.id}")
    return render(request, "projects/finance_builder.html", {
        "project": project, "run": run, "form": form, "error": error, "sizing": sizing,
    }, status=status)
