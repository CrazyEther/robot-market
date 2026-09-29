"""Small hand-calculated checks for the simulation metrics contract."""

from django.test import SimpleTestCase

from projects.event_ledger import schedule_observed_jobs
from projects.simulation_metrics import SimulationMetricsError, simulation_metrics


START = "2026-01-01T00:00:00+00:00"


def ledger(events, seconds, *, arrivals, started, delivered, completed, units,
           completed_units=None, max_queue=0):
    from datetime import datetime, timedelta

    end = (datetime.fromisoformat(START) + timedelta(seconds=seconds)).isoformat()
    return {
        "version": 2, "period_start_utc": START, "period_end_utc": end,
        "events": events, "arrivals": arrivals, "started": started,
        "delivered": delivered, "completed": completed,
        "in_progress_at_end": started - completed,
        "unmet_at_end": arrivals - delivered,
        "max_queue": max_queue,
        "delivered_work_units": str(units),
        "throughput_work_units": str(units if completed_units is None else completed_units),
        "queued_at_end": arrivals - started,
    }


def event(kind, second, row, units="1", robot="slot-1"):
    result = {"type": kind, "at_s": str(second), "source_row": row, "work_units": units}
    if kind != "arrival":
        result["robot_id"] = robot
    return result


def calendar(slot, rows):
    from datetime import datetime, timedelta

    origin = datetime.fromisoformat(START)
    return [{
        "source_row": source, "robot_slot": slot,
        "start_at_utc": (origin + timedelta(seconds=start)).isoformat(),
        "end_at_utc": (origin + timedelta(seconds=end)).isoformat(),
        "state": state,
    } for source, start, end, state in rows]


class SimulationMetricsTests(SimpleTestCase):
    def test_overload_queue_and_censored_waits_are_in_all_arrivals_mean(self):
        events = [
            event("arrival", 0, 2), event("start", 0, 2),
            event("arrival", 1, 3), event("arrival", 2, 4),
            event("complete", 4, 2), event("handoff", 4, 2),
        ]
        data = ledger(events, 10, arrivals=3, started=1, delivered=1, completed=1,
                      units=1, max_queue=2)
        result = simulation_metrics(data, calendar(1, [(2, 0, 10, "available")]))
        self.assertEqual(result["queue"]["queue_job_seconds"], "17")
        self.assertEqual(result["queue"]["mean_jobs"], "1.7")
        self.assertEqual(result["waiting"]["unfinished_waiting_jobs"], 2)
        self.assertEqual(result["waiting"]["unfinished_waiting_censored_seconds"], "17")
        self.assertEqual(result["waiting"]["all_arrivals_observed_mean_seconds"], "5.666666666666666666666666667")
        self.assertEqual(result["jobs"]["delivery_ratio"], "0.3333333333333333333333333333")

    def test_simultaneous_arrival_start_and_delivery_are_counted_once(self):
        events = [
            event("arrival", 0, 2), event("start", 0, 2),
            event("handoff", 2, 2), event("complete", 3, 2),
            event("arrival", 3, 3), event("start", 3, 3),
        ]
        data = ledger(events, 8, arrivals=2, started=2, delivered=1, completed=1,
                      units=1, max_queue=0)
        result = simulation_metrics(data, calendar(1, [(2, 0, 8, "available")]))
        self.assertEqual(result["queue"]["queue_job_seconds"], "0")
        self.assertEqual(result["waiting"]["started_jobs_mean_seconds"], "0")
        self.assertEqual(result["jobs"]["delivered_jobs"], 1)
        self.assertEqual(result["jobs"]["completed_cycles"], 1)
        self.assertEqual(result["fleet"]["busy_robot_seconds"], "8")

    def test_incomplete_cycle_is_clipped_at_horizon_and_delivery_is_distinct(self):
        events = [event("arrival", 2, 2), event("start", 4, 2), event("handoff", 6, 2)]
        data = ledger(events, 10, arrivals=1, started=1, delivered=1, completed=0,
                      units=1, completed_units=0, max_queue=1)
        result = simulation_metrics(data, calendar(1, [(2, 0, 10, "available")]))
        self.assertEqual(result["fleet"]["busy_robot_seconds"], "6")
        self.assertEqual(result["throughput"]["delivered_work_units"], "1")
        self.assertEqual(result["jobs"]["completed_cycles"], 0)

    def test_started_wait_percentile_uses_nearest_rank(self):
        events = []
        for row, arrival, start, end in ((2, 0, 0, 1), (3, 0, 2, 3), (4, 0, 5, 6)):
            events.extend((event("arrival", arrival, row), event("start", start, row),
                           event("handoff", end, row), event("complete", end, row)))
        data = ledger(events, 8, arrivals=3, started=3, delivered=3, completed=3,
                      units=3, max_queue=2)
        result = simulation_metrics(data, calendar(1, [(2, 0, 8, "available")]))
        self.assertEqual(result["waiting"]["started_jobs_mean_seconds"], "2.333333333333333333333333333")
        self.assertEqual(result["waiting"]["started_jobs_max_seconds"], "5")
        self.assertEqual(result["waiting"]["started_jobs_p95_seconds"], "5")

    def test_zero_arrivals_and_no_available_calendar_yield_null_ratios(self):
        data = ledger([], 4, arrivals=0, started=0, delivered=0, completed=0, units=0)
        result = simulation_metrics(data, calendar(1, [(2, 0, 4, "downtime")]))
        self.assertIsNone(result["jobs"]["delivery_ratio"])
        self.assertIsNone(result["fleet"]["utilization_ratio"])
        self.assertIsNone(result["robots"][0]["utilization_ratio"])
        self.assertEqual(result["calendar"]["downtime_robot_seconds"], "4")
        self.assertEqual(result["throughput"]["delivered_work_units_per_hour"], "0")

    def test_multiple_robots_union_busy_and_account_calendar_states(self):
        events = [
            event("arrival", 0, 2, robot="slot-1"), event("start", 0, 2, robot="slot-1"),
            event("handoff", 2, 2, robot="slot-1"), event("complete", 3, 2, robot="slot-1"),
            event("arrival", 0, 3, robot="slot-2"), event("start", 3, 3, robot="slot-2"),
            event("handoff", 4, 3, robot="slot-2"), event("complete", 5, 3, robot="slot-2"),
        ]
        data = ledger(events, 8, arrivals=2, started=2, delivered=2, completed=2,
                      units=2, max_queue=1)
        rows = calendar(1, [(2, 0, 8, "available")]) + calendar(2, [
            (3, 0, 2, "available"), (4, 2, 3, "charging"),
            (5, 3, 5, "available"), (6, 5, 6, "maintenance"),
            (7, 6, 8, "downtime"),
        ])
        result = simulation_metrics(data, rows)
        self.assertEqual(result["fleet"]["busy_robot_seconds"], "5")
        self.assertEqual(result["fleet"]["available_robot_seconds"], "12")
        self.assertEqual(result["calendar"], {
            "charging_robot_seconds": "1", "maintenance_robot_seconds": "1",
            "downtime_robot_seconds": "2",
        })
        self.assertEqual([r["busy_robot_seconds"] for r in result["robots"]], ["3", "2"])

    def test_malformed_negative_or_calendar_inconsistent_input_fails_closed(self):
        data = ledger([event("arrival", 0, 2), event("start", 0, 2)], 4,
                      arrivals=1, started=1, delivered=0, completed=0, units=0)
        with self.assertRaises(SimulationMetricsError):
            simulation_metrics(data, calendar(1, [(2, 0, 3, "available"), (3, 2, 4, "downtime")]))
        data["events"][1]["at_s"] = "-1"
        with self.assertRaises(SimulationMetricsError):
            simulation_metrics(data, calendar(1, [(2, 0, 4, "available")]))

    def test_same_timestamp_queue_peak_matches_event_ledger(self):
        from datetime import datetime, timedelta

        origin = datetime.fromisoformat(START)
        finish = origin + timedelta(seconds=10)
        source = [{
            "id": "slot-1",
            "windows": [{"start_at": origin.isoformat(), "end_at": finish.isoformat(),
                         "source": "handwritten test calendar"}],
        }]
        observed = [
            {"source_row": 2, "requested_at_utc": origin.isoformat(),
             "service_seconds": "1", "handoff_seconds": "1", "work_units": "1"},
            {"source_row": 3, "requested_at_utc": (origin + timedelta(seconds=1)).isoformat(),
             "service_seconds": "1", "handoff_seconds": "1", "work_units": "1"},
        ]
        scheduled = schedule_observed_jobs(
            observed, source, period_start=origin, period_end=finish,
        )
        self.assertEqual(scheduled["max_queue"], 0)
        result = simulation_metrics(scheduled, calendar(1, [(2, 0, 10, "available")]))
        self.assertEqual(result["queue"]["queue_job_seconds"], "0")
