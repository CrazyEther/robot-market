"""Audit the visible product headings in the supplied PDF against the v4 media map.

The PDF's text layer contains U+FFFD for some glyphs. Those glyphs are treated
as unknown single characters, never as evidence of an exact letter match.
"""

import hashlib
import json
import os
import re
import unicodedata
from collections import defaultdict
from pathlib import Path

from pypdf import PdfReader


def normalized(value):
    value = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    return re.sub(r"[^a-zа-я0-9\ufffd]+", "", value)


def compatible(printed, expected):
    return all(actual == required or actual == "\ufffd"
               for actual, required in zip(printed, expected))


def audit(pdf_path, manifest_path):
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    pdf_bytes = pdf_path.read_bytes()
    if hashlib.sha256(pdf_bytes).hexdigest() != manifest["source_pdf_sha256"]:
        raise ValueError("PDF checksum differs from the media manifest")
    reader = PdfReader(pdf_path)
    by_page = defaultdict(list)
    for record in manifest["products"]:
        by_page[record["pdf_page"]].append(record)
    if len(by_page) != manifest["product_pages"]:
        raise ValueError("Media manifest product page count differs")
    visible_prefixes = []
    for page_number, products in sorted(by_page.items()):
        # Measured heading anchors of this exact PDF template, in points.
        anchors = (65.0, 517.5, 970.0)[:len(products)]
        fragments = defaultdict(list)

        def text_fragment(value, _cm, text_matrix, _font, _size):
            # This catalog template places headings in the band below its photos.
            x = round(text_matrix[4], 1)
            if value.strip() and x in anchors and 375 < text_matrix[5] < 420:
                fragments[x].append(value)

        reader.pages[page_number - 1].extract_text(visitor_text=text_fragment)
        titles = ["".join(fragments[x]) for x in anchors]
        if any(not title.strip() for title in titles):
            raise ValueError(f"PDF page {page_number}: visible heading count differs")
        for product in products:
            original = normalized(product["name"])
            printed = normalized(titles[product["page_column"] - 1])
            if len(printed) == len(original) and compatible(printed, original):
                continue
            if 20 <= len(printed) < len(original) and compatible(printed, original):
                visible_prefixes.append({
                    "record_index": product["record_index"],
                    "pdf_page": page_number,
                    "visible_pdf_heading": titles[product["page_column"] - 1],
                    "catalog_name": product["name"],
                })
                continue
            raise ValueError(f"PDF page {page_number}, catalog row {product['record_index']}: heading differs")
    return {
        "source_pdf_sha256": manifest["source_pdf_sha256"],
        "media_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "record_count": len(manifest["products"]),
        "full_heading_matches": len(manifest["products"]) - len(visible_prefixes),
        "visible_prefix_only": visible_prefixes,
    }


def main():
    pdf = os.environ.get("CATALOG_PDF_SOURCE_PATH")
    manifest = os.environ.get("CATALOG_MEDIA_MANIFEST_PATH")
    if not pdf or not manifest:
        raise ValueError("CATALOG_PDF_SOURCE_PATH and CATALOG_MEDIA_MANIFEST_PATH are required")
    result = audit(Path(pdf), Path(manifest))
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
