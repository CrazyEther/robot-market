"""Validate curated claims against the acquired private source snapshots."""

import csv
import hashlib
import html
import json
import os
import re
import sys
import unicodedata
from decimal import Decimal, InvalidOperation
from pathlib import Path


RESEARCH_DIR = os.environ.get("SOURCE_RESEARCH_DIR")
if not RESEARCH_DIR:
    raise RuntimeError("SOURCE_RESEARCH_DIR должен указывать на закрытый каталог вне Git")
ROOT = Path(RESEARCH_DIR).resolve()
if ROOT.is_relative_to(Path(__file__).resolve().parents[2]):
    raise RuntimeError("SOURCE_RESEARCH_DIR должен находиться вне публичного репозитория")


def normalized(value):
    value = unicodedata.normalize("NFKC", html.unescape(value)).casefold()
    value = re.sub(r"<[^>]+>", " ", value)
    return " ".join(value.split())


def price_appears_in_evidence(value, evidence):
    if not isinstance(value, int):
        return False
    for match in re.finditer(r"\d{1,3}(?:[ \u00a0\u202f]\d{3})+(?:[,.]\d+)?|\d+(?:[,.]\d+)?", evidence):
        try:
            amount = Decimal(re.sub(r"\s", "", match.group()).replace(",", "."))
        except InvalidOperation:
            continue
        if evidence[match.end():match.end() + 8].lstrip().startswith("млн"):
            amount *= 1_000_000
        if amount == value:
            return True
    return False


def main():
    catalog_path = os.environ.get("CATALOG_SOURCE_PATH")
    if not catalog_path:
        raise ValueError("CATALOG_SOURCE_PATH is required")
    catalog_content = Path(catalog_path).read_bytes()
    catalog_sha256 = hashlib.sha256(catalog_content).hexdigest()
    catalog_rows = list(csv.DictReader(
        catalog_content.decode("utf-8-sig").splitlines(keepends=True),
        delimiter=";", strict=True,
    ))
    acquisitions = [json.loads(line) for line in
                    (ROOT / "acquisition.jsonl").read_text(encoding="utf-8").splitlines()]
    latest = {item["id"]: item for item in acquisitions if item["status"] == "acquired"}
    claims = json.loads((ROOT / "claims_input.json").read_text(encoding="utf-8"))
    curated = []
    for claim in claims:
        if claim["use"] == "offer_specific" and "RUB" in claim["unit"]:
            if not price_appears_in_evidence(claim["value"], claim["evidence"]):
                raise ValueError(f"Price and evidence disagree: {claim['id']}")
        source = latest[claim["source_id"]]
        raw_path = (ROOT / source["raw_file"].replace("\\", "/")).resolve()
        if not raw_path.is_relative_to((ROOT / "raw").resolve()):
            raise ValueError("Raw source path escaped the research directory")
        content = raw_path.read_bytes()
        if hashlib.sha256(content).hexdigest() != source["sha256"]:
            raise ValueError(f"Snapshot checksum mismatch: {claim['source_id']}")
        try:
            source_text = normalized(content.decode(source.get("charset") or "utf-8"))
        except (LookupError, UnicodeDecodeError):
            raise ValueError(f"Cannot decode source snapshot: {claim['source_id']}") from None
        for term in re.split(r"\s*/\s*|\s*;\s*", claim["evidence"]):
            if normalized(term) not in source_text:
                raise ValueError(f"Evidence not present: {claim['id']} / {term}")
        catalog_refs = []
        for row in claim["catalog_rows"]:
            if row < 1 or row > len(catalog_rows):
                raise ValueError(f"Catalog record out of range: {claim['id']}")
            source_row = catalog_rows[row - 1]
            catalog_refs.append({
                "record_index": row,
                "external_id": source_row["id"],
                "name": source_row["Название"],
                "company": source_row["компания"],
            })
        if claim["value"] is None and claim["use"] != "blocked":
            raise ValueError(f"Missing value cannot be used: {claim['id']}")
        curated.append({
            **claim,
            "source_url": source["final_url"],
            "source_sha256": source["sha256"],
            "source_fetched_at_utc": source["fetched_at_utc"],
            "source_file_private": source["raw_file"],
            "catalog_sha256": catalog_sha256,
            "catalog_refs": catalog_refs,
        })
    output = ROOT / "verified_claims.json"
    output.write_text(json.dumps(curated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Validated {len(curated)} claims against {len(latest)} source snapshots")
    return 0


if __name__ == "__main__":
    sys.exit(main())
