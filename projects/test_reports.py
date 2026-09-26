"""Injection boundaries for downloaded spreadsheets and SVG frames."""

import csv
import io
from xml.etree import ElementTree

from django.test import SimpleTestCase

from projects.reports import _csv_bytes, _svg, safe_csv_cell


class ReportEncodingTests(SimpleTestCase):
    def test_formula_prefixes_are_literal_while_verified_numbers_remain_numeric(self):
        for value in ("=1+1", "+SUM(1,1)", "-CMD", "@SUM(1,1)", "\t=1+1", "  =1+1"):
            with self.subTest(value=value):
                self.assertTrue(safe_csv_cell(value).startswith("'"))
        data = _csv_bytes(("amount", "source_ref"),
                          [{"amount": "-12.50", "source_ref": "=HYPERLINK(\"x\")"}],
                          numeric_columns={"amount"})
        rows = list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))
        self.assertEqual(rows[0]["amount"], "-12.50")
        self.assertTrue(rows[0]["source_ref"].startswith("'="))

    def test_svg_escapes_owner_supplied_labels(self):
        label = '<script>alert("x")</script>'
        scene = {"floors": [{"label": label,
                              "nodes": [{"id": "a", "label": label, "x": "100", "y": "100"}],
                              "edges": []}]}
        svg = _svg(scene, [], None, "0")
        self.assertNotIn(b"<script>", svg)
        root = ElementTree.fromstring(svg)
        self.assertIn(label, "".join(root.itertext()))
