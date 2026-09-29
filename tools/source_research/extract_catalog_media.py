"""Map real PDF product images to v4 records in a private directory.

The output is research material. Rights to publish images must be established
separately before connecting it to a public storefront or repository.
"""

import csv
import hashlib
import io
import json
import logging
import os
import re
from collections import Counter, defaultdict
from pathlib import Path

from PIL import Image
from pypdf import PdfReader
from pypdf.generic import ContentStream


logging.getLogger("pypdf").setLevel(logging.CRITICAL)
RANGE = re.compile(r"(\d+)\s*[–-]\s*(\d+)\s+из\s+(\d+)")
TRANSFORM_VERSION = 2


def sha256(content):
    return hashlib.sha256(content).hexdigest()


def required_path(name):
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"{name} is required")
    path = Path(value).resolve()
    if not path.is_file():
        raise ValueError(f"{name} file does not exist")
    return path


def image_positions(page, reader, names):
    operations = ContentStream(page.get_contents(), reader).operations
    names = {"/" + name.rsplit(".", 1)[0]: name for name in names}
    found = {}
    def compose(parent, child):
        a, b, c, d, e, f = parent
        u, v, w, x, y, z = child
        return (a * u + c * v, b * u + d * v,
                a * w + c * x, b * w + d * x,
                a * y + c * z + e, b * y + d * z + f)

    matrix = (1, 0, 0, 1, 0, 0)
    stack = []
    for operands, operator in operations:
        if operator == b"q":
            stack.append(matrix)
        elif operator == b"Q":
            if not stack:
                raise ValueError("Unbalanced PDF graphics state")
            matrix = stack.pop()
        elif operator == b"cm":
            matrix = compose(matrix, tuple(float(value) for value in operands))
        elif operator == b"Do" and str(operands[0]) in names:
            resource = str(operands[0])
            if resource in found:
                raise ValueError("Product image resource is placed more than once")
            found[resource] = matrix[4]
    if stack:
        raise ValueError("Unbalanced PDF graphics state")
    if set(found) != set(names) or len(set(found.values())) != len(found):
        raise ValueError("Product image placements are incomplete or ambiguous")
    return {names[resource]: x for resource, x in found.items()}


def png_bytes(image):
    bitmap = image.image
    bitmap = bitmap.convert("RGBA" if "A" in bitmap.getbands() else "RGB")
    output = io.BytesIO()
    bitmap.save(output, format="PNG")
    return output.getvalue()


def extract(pdf_path, csv_path, private_root):
    if private_root.is_relative_to(Path(__file__).resolve().parents[2]):
        raise ValueError("Media output must be outside the public repository")
    pdf_bytes = pdf_path.read_bytes()
    csv_bytes = csv_path.read_bytes()
    records = list(csv.DictReader(io.StringIO(csv_bytes.decode("utf-8-sig"), newline=""),
                                  delimiter=";", strict=True))
    if not records or not {"id", "Название", "компания", "Отрасль"}.issubset(records[0]):
        raise ValueError("Catalog columns do not match v4")
    reader = PdfReader(io.BytesIO(pdf_bytes))
    products = []
    pending_images = []
    section_start = None
    section_total = None
    expected_local = None
    section_count = 0
    for page_number, page in enumerate(reader.pages, start=1):
        match = RANGE.search(page.extract_text() or "")
        if match is None:
            continue
        first, last, total = map(int, match.groups())
        if first < 1 or last < first or total < last:
            raise ValueError(f"Invalid product range on PDF page {page_number}")
        if first == 1:
            if section_start is not None and expected_local != section_total + 1:
                raise ValueError("Previous PDF section has missing products")
            section_start = len(products)
            section_total = total
            expected_local = 1
            section_count += 1
        if section_start is None or total != section_total or first != expected_local:
            raise ValueError(f"Discontinuous product range on PDF page {page_number}")
        expected_local = last + 1
        images = [item for item in page.images
                  if item.image.width > 100 and item.image.height > 80]
        if len(images) != last - first + 1:
            raise ValueError(f"Image/product count differs on PDF page {page_number}")
        try:
            positions = image_positions(page, reader, [item.name for item in images])
        except ValueError as error:
            raise ValueError(f"PDF page {page_number}: {error}") from error
        images.sort(key=lambda item: positions[item.name])
        for position, item in enumerate(images):
            record_index = section_start + first + position
            if record_index > len(records):
                raise ValueError("PDF has more products than the v4 catalog")
            record = records[record_index - 1]
            if records[section_start]["Отрасль"] != record["Отрасль"]:
                raise ValueError(f"PDF and CSV section boundary differs at record {record_index}")
            rendered = png_bytes(item)
            digest = sha256(rendered)
            relative = f"images/{record_index:03d}-{digest[:16]}.png"
            products.append({
                "record_index": record_index,
                "external_id": record["id"],
                "name": record["Название"],
                "company": record["компания"],
                "section": record["Отрасль"],
                "pdf_page": page_number,
                "pdf_section_range": [first, last, total],
                "page_column": position + 1,
                "image_resource": item.name,
                "image_x_pt": positions[item.name],
                "original_image_sha256": sha256(item.data),
                "width_px": item.image.width,
                "height_px": item.image.height,
                "derived_png": relative,
                "derived_png_sha256": digest,
            })
            pending_images.append((relative, rendered))
    if section_start is None or expected_local != section_total + 1:
        raise ValueError("Final PDF section has missing products")
    if len(products) != len(records) or [item["record_index"] for item in products] != list(range(1, len(records) + 1)):
        raise ValueError("PDF products do not cover every v4 record exactly once")
    grouped = Counter(record["Отрасль"] for record in records)
    if len(grouped) != section_count or any(
        sum(item["section"] == section for item in products) != count
        for section, count in grouped.items()
    ):
        raise ValueError("PDF sections do not match the v4 catalog")
    image_uses = defaultdict(list)
    for product in products:
        image_uses[product["derived_png_sha256"]].append(product)
    conflicting_images = [
        {
            "image_sha256": digest,
            "records": [
                {"record_index": product["record_index"], "name": product["name"]}
                for product in uses
            ],
        }
        for digest, uses in image_uses.items()
        if len({product["name"] for product in uses}) > 1
    ]
    result = {
        "transform_version": TRANSFORM_VERSION,
        "source_pdf": pdf_path.name,
        "source_pdf_sha256": sha256(pdf_bytes),
        "source_csv": csv_path.name,
        "source_csv_sha256": sha256(csv_bytes),
        "pdf_pages": len(reader.pages),
        "product_pages": len({item["pdf_page"] for item in products}),
        "sections": section_count,
        "cross_name_image_reuse": conflicting_images,
        "products": products,
    }
    private_root.mkdir(parents=True, exist_ok=True)
    for relative, content in pending_images:
        target = (private_root / relative).resolve()
        if not target.is_relative_to(private_root):
            raise ValueError("Image path escaped private output directory")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and sha256(target.read_bytes()) != sha256(content):
            raise ValueError(f"Existing image differs: {relative}")
        if not target.exists():
            target.write_bytes(content)
    manifest = private_root / "manifest.json"
    content = (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if manifest.exists() and sha256(manifest.read_bytes()) != sha256(content):
        raise ValueError("Existing manifest differs; inspect source changes before replacing it")
    if not manifest.exists():
        manifest.write_bytes(content)
    return result


def main():
    private = os.environ.get("CATALOG_MEDIA_PRIVATE_DIR")
    if not private:
        raise ValueError("CATALOG_MEDIA_PRIVATE_DIR is required")
    result = extract(
        required_path("CATALOG_PDF_SOURCE_PATH"),
        required_path("CATALOG_SOURCE_PATH"),
        Path(private).resolve(),
    )
    print(f"Mapped {len(result['products'])} images across {result['sections']} sections and {result['product_pages']} product pages")


if __name__ == "__main__":
    main()
