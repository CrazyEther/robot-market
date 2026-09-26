"""Source-bound demand revisions for observed transport operations."""

from collections import deque
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from projects.selection_refs import workload_matches_selection


class DemandRevisionError(ValueError):
    pass


def minimum_observed_peak(rows):
    """Maximum arrivals in any rolling hour of the supplied source log."""
    window = deque()
    maximum = 0
    for row in rows:
        at = datetime.fromisoformat(row["requested_at_utc"])
        while window and at - window[0] >= timedelta(hours=1):
            window.popleft()
        window.append(at)
        maximum = max(maximum, len(window))
    return maximum


def revised_demand_snapshot(snapshot, *, process, peak, peak_source,
                            log_ref, parent_run_id, parent_revision, user_id, at):
    """Change demand only; make old availability unusable in the new revision."""
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("workload_profile"), dict):
        raise DemandRevisionError("У исходного прогона нет подтверждённого профиля нагрузки.")
    profile = snapshot["workload_profile"]
    selection = snapshot.get("robot_selection") or {}
    if (profile.get("process") != process.code
            or not workload_matches_selection(profile, selection, snapshot)
            or not isinstance(profile.get("parameters"), dict)):
        raise DemandRevisionError("Профиль нагрузки не соответствует выбранной операции и модели.")
    current = profile["parameters"].get("peak_jobs_per_h")
    if not isinstance(current, dict) or current.get("unit") != "рейсов/ч" or not current.get("source"):
        raise DemandRevisionError("У исходного прогона нет подтверждённого пикового потока.")
    try:
        old_peak = Decimal(str(current["value"]))
        new_peak = Decimal(str(peak))
    except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
        raise DemandRevisionError("Пиковый поток должен быть числом.") from exc
    if not old_peak.is_finite() or not new_peak.is_finite() or new_peak <= 0:
        raise DemandRevisionError("Пиковый поток должен быть положительным конечным числом.")
    if not isinstance(peak_source, str) or not peak_source.strip():
        raise DemandRevisionError("Укажите источник нового пикового потока.")
    if not isinstance(log_ref, dict) or not log_ref.get("sha256"):
        raise DemandRevisionError("Нужен новый журнал заданий.")
    previous_schema = snapshot.get("schema_version", 0)
    if not isinstance(previous_schema, int) or isinstance(previous_schema, bool):
        raise DemandRevisionError("Версия исходного проекта повреждена.")
    revised = deepcopy(snapshot)
    revised["schema_version"] = max(previous_schema, 9)
    revised["workload_profile"]["parameters"]["peak_jobs_per_h"] = {
        "value": str(new_peak), "unit": "рейсов/ч", "source": peak_source.strip(),
        "status": "user_attested",
    }
    revised["workload_profile"]["attested_by_user_id"] = user_id
    revised["workload_profile"]["attested_at"] = at.isoformat()
    revised["operation_log_ref"] = log_ref
    revised.pop("availability_ref", None)
    revised["demand_what_if"] = {
        "parent_run_id": str(parent_run_id),
        "parent_revision": parent_revision,
    }
    return revised
