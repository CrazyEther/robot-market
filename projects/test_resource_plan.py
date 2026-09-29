"""A resource schedule must come from the chosen facility and measured route."""

from django.test import SimpleTestCase

from projects.event_ledger import EventInputError
from projects.resource_plan import build_route_resources
from projects.topology import empty_topology


SCHEDULE = "2026-09-28T08:00:00+05:00/2026-09-28T08:01:00+05:00"


def fixture(slug):
    process = {"warehouse": "warehouse_pallet_transfer",
               "airport": "airport_baggage_transport",
               "hospital": "hospital_meal_delivery"}[slug]
    topology = empty_topology(slug, process)
    topology["origin"], topology["destination"] = "a", "b"
    topology["nodes"] = [{"id": "a", "label": "A", "floor": "1", "zone": "restricted" if slug == "airport" else "clean"},
                         {"id": "b", "label": "B", "floor": "2" if slug == "hospital" else "1",
                          "zone": "restricted" if slug == "airport" else "clean"}]
    if slug == "hospital":
        topology["route_flow"] = "clean"
    edge = {"id": "p1", "start": "a", "end": "b", "length_m": "5",
            "length_source": "Обмер объекта", "source": "План объекта",
            "bidirectional": True, "resource_id": "narrow-passage",
            "resource_capacity": 1, "resource_direction_policy": "alternating",
            "resource_occupancy_policy": "entry_to_exit", "resource_priority_policy": "fifo",
            "resource_source": "Регламент объекта, пункт 6", "resource_schedule": SCHEDULE}
    if slug == "airport":
        edge["access"] = "restricted"
    if slug == "hospital":
        edge.update(kind="elevator", flow="clean")
    topology["edges"] = [edge]
    params = {name: {"value": value, "source": "Измерение объекта"}
              for name, value in (("observed_speed_m_s", "1"),
                                  ("pickup_time_s", "1"), ("dropoff_time_s", "1"))}
    if slug == "hospital":
        params.update({name: {"value": value, "source": "Журнал лифта"}
                       for name, value in (("outbound_elevator_wait_s", "2"),
                                           ("outbound_elevator_ride_s", "4"),
                                           ("inbound_elevator_wait_s", "3"),
                                           ("inbound_elevator_ride_s", "5"))})
    return {"topology_profile": topology, "workload_profile": {"parameters": params}}, {
        "cycle_seconds": "16" if slug == "hospital" else "12",
        "handoff_seconds": "8" if slug == "hospital" else "7",
    }


class ResourcePlanTests(SimpleTestCase):
    def test_three_objects_compile_only_measured_timed_route(self):
        for slug in ("warehouse", "airport", "hospital"):
            with self.subTest(slug=slug):
                snapshot, sizing = fixture(slug)
                plan = build_route_resources(snapshot, sizing)
                self.assertEqual(plan["resources"][0]["capacity"], 1)
                self.assertEqual(len(plan["resources"][0]["windows"]), 1)
                self.assertEqual(plan["coverage"], {"constrained_route_edges": 1,
                                                     "total_route_edges": 1})
                movement = [stage for stage in plan["phases"]
                            if stage.get("resource_id") == "narrow-passage"]
                self.assertEqual(len(movement), 2)
                self.assertEqual([stage["direction"] for stage in movement],
                                 ["forward", "reverse"])
                self.assertEqual([stage["kind"] for stage in movement],
                                 ["elevator", "elevator"] if slug == "hospital" else ["travel", "travel"])

    def test_human_description_cannot_serve_as_executable_working_window(self):
        snapshot, sizing = fixture("warehouse")
        snapshot["topology_profile"]["edges"][0]["resource_schedule"] = "В течение смены"
        with self.assertRaises(EventInputError):
            build_route_resources(snapshot, sizing)

    def test_no_attested_resources_keeps_existing_legacy_route_unmodified(self):
        snapshot, sizing = fixture("warehouse")
        for name in tuple(snapshot["topology_profile"]["edges"][0]):
            if name.startswith("resource_"):
                snapshot["topology_profile"]["edges"][0].pop(name)
        self.assertIsNone(build_route_resources(snapshot, sizing))

    def test_unconfirmed_elevator_timing_is_not_inferred_from_length(self):
        snapshot, sizing = fixture("hospital")
        snapshot["workload_profile"]["parameters"].pop("outbound_elevator_ride_s")
        with self.assertRaises(EventInputError):
            build_route_resources(snapshot, sizing)

