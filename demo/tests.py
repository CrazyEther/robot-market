"""Public routes expose only object taxonomy and imported catalogue state."""

from django.test import TestCase
from django.urls import reverse

from projects.models import Project


class PublicJourneyTests(TestCase):
    def test_three_objects_link_to_project_creation(self):
        homepage = self.client.get(reverse("index"))
        self.assertEqual(homepage.status_code, 200)
        for slug, title in Project.OBJECT_TYPES:
            with self.subTest(slug=slug):
                self.assertContains(homepage, reverse("detail", args=[slug]))
                page = self.client.get(reverse("detail", args=[slug]))
                self.assertContains(page, title)
                self.assertContains(page, reverse("project_create", args=[slug]))

    def test_object_api_has_no_invented_operations(self):
        response = self.client.get(reverse("objects_api"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"objects": [
                {"slug": slug, "title": title} for slug, title in Project.OBJECT_TYPES
            ]},
        )
        self.assertEqual(self.client.get("/objects/unknown/").status_code, 404)
        self.assertEqual(self.client.post(reverse("objects_api")).status_code, 405)

    def test_readiness_fails_closed_without_catalog(self):
        self.assertEqual(self.client.get(reverse("health")).json(), {"status": "alive"})
        self.assertEqual(self.client.get(reverse("ready")).status_code, 503)
