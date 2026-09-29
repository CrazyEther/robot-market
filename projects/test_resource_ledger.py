"""Independent, source-bound timing contracts for shared physical passages."""

import hashlib
import json
from datetime import datetime, timedelta

from django.test import SimpleTestCase

from projects.event_ledger import EventInputError, schedule_observed_jobs
from projects.resource_ledger import schedule_resource_jobs


class SharedResourceLedgerTests(SimpleTestCase):
    def setUp(self):
        self.start = datetime.fromisoformat("2026-09-28T08:00:00+05:00")
        self.end = self.start + timedelta(seconds=45)
        self.window = {
            "start_at": self.start.isoformat(), "end_at": self.end.isoformat(),
            "source": "Официальный журнал работы объекта, строки 2–4",
        }
        self.robots = [{"id": f"slot-{i}", "windows": [self.window]} for i in (1, 2)]
        self.phases = [
            {"kind": "travel", "from": "a", "to": "b", "duration_s": "5",
             "resource_id": "passage", "direction": "forward"},
            {"kind": "dropoff", "from": "b", "to": "b", "duration_s": "1"},
            {"kind": "travel", "from": "b", "to": "a", "duration_s": "5",
             "resource_id": "passage", "direction": "reverse"},
        ]
        self.jobs = [
            {"source_row": row, "requested_at_utc": self.start.isoformat(),
             "work_units": "1", "service_seconds": "11", "handoff_seconds": "6"}
            for row in (2, 3)
        ]

    def run_case(self, *, capacity=1, directions="alternating", jobs=None, windows=None):
        return schedule_resource_jobs(
            self.jobs if jobs is None else jobs, self.robots,
            self.phases, [{
                "id": "passage", "capacity": capacity,
                "direction_policy": directions, "occupancy_policy": "entry_to_exit",
                "priority_policy": "fifo", "source": "Схема движения, пункт 6",
                "windows": [self.window] if windows is None else windows,
            }], period_start=self.start, period_end=self.end,
        )

    def test_capacity_one_never_overlaps_traversal_and_delays_handoff(self):
        ledger = self.run_case()
        self.assertEqual(ledger["version"], 3)
        self.assertEqual((ledger["arrivals"], ledger["delivered"], ledger["completed"]), (2, 2, 2))
        self.assertEqual([event["at_s"] for event in ledger["events"]
                          if event["type"] == "handoff"], ["6", "17"])
        self.assertEqual(ledger["resource_wait_seconds"], "11")
        spans = ledger["resource_reservations"]
        self.assertEqual([(item["start_s"], item["end_s"]) for item in spans],
                         [("0", "5"), ("6", "11"), ("11", "16"), ("17", "22")])
        second = next(c for c in ledger["motion_cycles"] if c["source_row"] == 3)
        self.assertEqual(second["stages"][0]["kind"], "resource_wait")
        self.assertEqual(second["stages"][0]["end_s"], "11")

    def test_shared_passage_reduces_delivered_jobs_when_observation_ends(self):
        """A bottleneck must change observed output, not merely draw a waiting icon."""
        short_end = self.start + timedelta(seconds=17)
        short_window = {**self.window, "end_at": short_end.isoformat()}
        constrained = schedule_resource_jobs(
            self.jobs, [{"id": f"slot-{i}", "windows": [short_window]} for i in (1, 2)],
            self.phases,
            [{"id": "passage", "capacity": 1, "direction_policy": "alternating",
              "occupancy_policy": "entry_to_exit", "priority_policy": "fifo",
              "source": "Регламент проезда", "windows": [short_window]}],
            period_start=self.start, period_end=short_end,
        )
        unconstrained = schedule_observed_jobs(
            self.jobs, [{"id": f"slot-{i}", "windows": [short_window]} for i in (1, 2)],
            period_start=self.start, period_end=short_end,
        )
        self.assertEqual(constrained["delivered"], 1)
        self.assertEqual(unconstrained["delivered"], 2)
        self.assertEqual(constrained["delivered_work_units"], "1")
        self.assertEqual(unconstrained["delivered_work_units"], "2")

    def test_two_resources_multiple_windows_match_preindex_event_fingerprint(self):
        """Reference output captured from the original unindexed v3 scheduler."""
        end = self.start + timedelta(seconds=600)
        window = {"start_at": self.start.isoformat(), "end_at": end.isoformat(),
                  "source": "Контрольный сценарий"}
        robots = [{"id": f"slot-{slot}", "windows": [window]} for slot in (1, 2)]
        phases = [
            {"kind": "pickup", "from": "a", "to": "a", "duration_s": "1"},
            {"kind": "travel", "from": "a", "to": "b", "duration_s": "2",
             "resource_id": "lane-a", "direction": "forward"},
            {"kind": "travel", "from": "b", "to": "c", "duration_s": "3",
             "resource_id": "lane-b", "direction": "forward"},
            {"kind": "dropoff", "from": "c", "to": "c", "duration_s": "2"},
            {"kind": "travel", "from": "c", "to": "b", "duration_s": "3",
             "resource_id": "lane-b", "direction": "reverse"},
            {"kind": "travel", "from": "b", "to": "a", "duration_s": "2",
             "resource_id": "lane-a", "direction": "reverse"},
        ]
        resources = [
            {"id": "lane-a", "capacity": 1, "direction_policy": "alternating",
             "occupancy_policy": "entry_to_exit", "priority_policy": "fifo",
             "source": "Контрольный регламент", "windows": [
                 {"start_at": self.start.isoformat(),
                  "end_at": (self.start + timedelta(seconds=140)).isoformat(),
                  "source": "Контрольный сценарий"},
                 {"start_at": (self.start + timedelta(seconds=200)).isoformat(),
                  "end_at": end.isoformat(), "source": "Контрольный сценарий"},
             ]},
            {"id": "lane-b", "capacity": 2, "direction_policy": "mixed",
             "occupancy_policy": "entry_to_exit", "priority_policy": "fifo",
             "source": "Контрольный регламент", "windows": [window]},
        ]
        jobs = [
            {"source_row": row + 2,
             "requested_at_utc": (self.start + timedelta(seconds=7 * row)).isoformat(),
             "work_units": "1", "service_seconds": "13", "handoff_seconds": "8"}
            for row in range(70)
        ]
        ledger = schedule_resource_jobs(jobs, robots, phases, resources,
                                        period_start=self.start, period_end=end)
        digest = hashlib.sha256(json.dumps(
            ledger, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        self.assertEqual(digest, "0931c95f01ed60bad0d74822f17db25ee6abce99f86948bb6f3922d5df6a269c")
        self.assertEqual(ledger["resource_wait_seconds"], "166")
        self.assertEqual(len(ledger["resource_reservations"]), 280)

    def test_mixed_directions_can_overlap_with_attested_capacity_two(self):
        ledger = self.run_case(capacity=2, directions="mixed")
        self.assertEqual(ledger["resource_wait_seconds"], "0")
        self.assertEqual([event["at_s"] for event in ledger["events"]
                          if event["type"] == "handoff"], ["6", "6"])
        self.assertEqual(ledger["completed"], 2)

    def test_alternating_direction_waits_even_when_capacity_two(self):
        jobs = [self.jobs[0], {
            **self.jobs[1], "requested_at_utc":
            (self.start + timedelta(seconds=6)).isoformat(),
        }]
        alternate = self.run_case(capacity=2, directions="alternating", jobs=jobs)
        mixed = self.run_case(capacity=2, directions="mixed", jobs=jobs)
        self.assertEqual([e["at_s"] for e in alternate["events"]
                          if e["type"] == "handoff"], ["6", "17"])
        self.assertEqual([e["at_s"] for e in mixed["events"]
                          if e["type"] == "handoff"], ["6", "12"])

    def test_no_default_windows_unsupported_priority_or_unauthenticated_limits(self):
        for windows in ([], [{"start_at": "2026-09-28T08:00:00",
                              "end_at": self.end.isoformat(), "source": "Документ"}],
                        [{**self.window, "source": ""}]):
            with self.subTest(windows=windows), self.assertRaises(EventInputError):
                self.run_case(windows=windows)
        for priority in ("operator_defined", "random"):
            with self.subTest(priority=priority), self.assertRaises(EventInputError):
                schedule_resource_jobs(self.jobs, self.robots, self.phases, [{
                    "id": "passage", "capacity": 1, "direction_policy": "alternating",
                    "occupancy_policy": "entry_to_exit", "priority_policy": priority,
                    "source": "Документ", "windows": [self.window],
                }], period_start=self.start, period_end=self.end)

