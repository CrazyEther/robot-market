"""An attested route resource never acquires made-up traffic constraints."""

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from projects.forms import TopologyEdgeForm
from projects.models import Project, ProjectRevision
from projects.topology import empty_topology


POLICY = {
    "resource_id": "passage-01",
    "resource_capacity": "1",
    "resource_direction_policy": "alternating",
    "resource_occupancy_policy": "entry_to_exit",
    "resource_priority_policy": "fifo",
    "resource_schedule": "2026-09-28T08:00:00+05:00/2026-09-28T20:00:00+05:00",
    "resource_source": "План управления движением объекта, раздел 4",
}


class TopologyResourceFormTests(SimpleTestCase):
    def test_missing_and_partial_policies_are_not_silently_confirmed(self):
        for slug in ("warehouse", "airport", "hospital"):
            with self.subTest(slug=slug):
                topology = empty_topology(slug, "test_process")
                topology["nodes"] = [
                    {"id": "a", "label": "A", "floor": "1"},
                    {"id": "b", "label": "B", "floor": "1"},
                ]
                required = {"start": "a", "end": "b", "source": "План объекта"}
                if slug == "airport":
                    required["access"] = "public"
                if slug == "hospital":
                    required.update(kind="corridor", flow="clean")

                self.assertTrue(TopologyEdgeForm(required, topology=topology).is_valid())
                self.assertFalse(TopologyEdgeForm(
                    {**required, "resource_capacity": "1"}, topology=topology,
                ).is_valid())
                self.assertFalse(TopologyEdgeForm(
                    {**required, **POLICY, "resource_source": ""}, topology=topology,
                ).is_valid())
                self.assertFalse(TopologyEdgeForm(
                    {**required, **POLICY, "resource_capacity": "0"}, topology=topology,
                ).is_valid())
                self.assertTrue(TopologyEdgeForm(
                    {**required, **POLICY}, topology=topology,
                ).is_valid())
                for wrong in (
                        "В пределах подтверждённых смен объекта",
                        "2026-09-28T08:00:00/2026-09-28T20:00:00",
                        "2026-09-28T20:00:00+05:00/2026-09-28T08:00:00+05:00",
                        "2026-09-28T08:00:00+05:00/2026-09-28T12:00:00+05:00;"
                        "2026-09-28T11:00:00+05:00/2026-09-28T20:00:00+05:00",
                ):
                    with self.subTest(wrong=wrong):
                        form = TopologyEdgeForm(
                            {**required, **POLICY, "resource_schedule": wrong},
                            topology=topology,
                        )
                        self.assertFalse(form.is_valid())
                        self.assertIn("resource_schedule", form.errors)

    def test_one_resource_cannot_have_two_conflicting_policies(self):
        topology = empty_topology("warehouse", "warehouse_pallet_transfer")
        topology["nodes"] = [{"id": "a", "label": "A", "floor": "1"},
                             {"id": "b", "label": "B", "floor": "1"}]
        topology["edges"] = [{"id": "existing", "start": "a", "end": "b",
                              **{name: int(value) if name == "resource_capacity" else value
                                 for name, value in POLICY.items()}}]
        invalid = TopologyEdgeForm({"start": "a", "end": "b", "source": "План",
                                    **POLICY, "resource_capacity": "2"}, topology=topology)
        self.assertFalse(invalid.is_valid())
        self.assertIn("resource_id", invalid.errors)
        repeated = TopologyEdgeForm({"start": "a", "end": "b", "source": "План",
                                     **POLICY}, topology=topology)
        self.assertFalse(repeated.is_valid())
        self.assertIn("resource_id", repeated.errors)
        editing = TopologyEdgeForm({"start": "a", "end": "b", "source": "План",
                                    **POLICY, "resource_capacity": "2"}, topology=topology,
                                   editing_edge_id="existing")
        self.assertTrue(editing.is_valid())


class TopologyResourceJourneyTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user(username="resource-owner", password="strong-pass-123")
        self.client.force_login(self.owner)

    def test_resource_contract_is_versioned_on_each_object_without_retroactively_upgrading_legacy(self):
        for slug, process_code in (("warehouse", "warehouse_pallet_transfer"),
                                   ("airport", "airport_terminal_cleaning"),
                                   ("hospital", "hospital_floor_cleaning")):
            with self.subTest(slug=slug):
                project = Project.objects.create(owner=self.owner, name=slug, object_slug=slug)
                topology = empty_topology(slug, process_code)
                topology["nodes"] = [
                    {"id": "start", "label": "Начало", "floor": "1", "zone": "public" if slug == "airport" else "clean"},
                    {"id": "end", "label": "Конец", "floor": "1", "zone": "public" if slug == "airport" else "clean"},
                ]
                topology["edges"] = [{"id": "edge-01", "start": "start", "end": "end",
                                      "source": "План объекта", "length_m": "10",
                                      "length_source": "Обмер", "bidirectional": True,
                                      "access": "public", "kind": "corridor", "flow": "clean"}]
                ProjectRevision.objects.create(project=project, number=1, scenario_snapshot={
                    "task_profile": {"process": process_code}, "topology_profile": topology,
                    "availability_ref": {"id": "pinned-calendar"},
                })
                url = reverse("project_topology", args=[project.id])
                old_page = self.client.get(url)
                self.assertContains(old_page, "Ограничения занятости: не подтверждены")
                payload = {"action": "update_edge", "base_revision": "1", "edge_id": "edge-01",
                           "start": "start", "end": "end", "source": "План объекта",
                           "length_m": "10", "length_source": "Обмер", "bidirectional": "on"}
                if slug == "airport":
                    payload["access"] = "public"
                if slug == "hospital":
                    payload.update(kind="corridor", flow="clean")
                partial = self.client.post(url, {**payload, "resource_id": "passage-01"})
                self.assertEqual(partial.status_code, 400)
                self.assertEqual(project.revisions.count(), 1)
                saved = self.client.post(url, {**payload, **POLICY})
                self.assertEqual(saved.status_code, 302)
                current = project.revisions.first()
                self.assertEqual(current.number, 2)
                self.assertEqual(current.scenario_snapshot["topology_profile"]["edges"][0]["resource_capacity"], 1)
                self.assertNotIn("availability_ref", current.scenario_snapshot)
                self.assertNotIn("resource_id", project.revisions.get(number=1).scenario_snapshot[
                    "topology_profile"]["edges"][0])
                self.assertContains(self.client.get(saved["Location"]), "Вместимость: 1")
                self.assertContains(self.client.get(url + "?edge=edge-01"), "passage-01")
                self.assertContains(self.client.get(url + "?revision=1"), "Ограничения занятости: не подтверждены")
