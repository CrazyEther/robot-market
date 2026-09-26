"""Acquire a private supplement and every cited official source by checksum."""

import json
import os
import re
import sys
import tempfile
from pathlib import Path

from catalog.bootstrap_source import CatalogBootstrapError, ensure_source
from catalog.limits import (
    MAX_SUPPLEMENT_MANIFEST_BYTES, MAX_SUPPLEMENT_SOURCE_BYTES,
    MAX_SUPPLEMENT_TOTAL_SOURCE_BYTES,
)


SHA256 = re.compile(r"[0-9a-f]{64}\Z")
DEFAULT_MANIFEST_PATH = Path(tempfile.gettempdir()) / "robot-market-supplement.json"
DEFAULT_ASSET_DIR = Path(tempfile.gettempdir()) / "robot-market-supplement-assets"


def acquire_supplement(*, manifest_path, manifest_url, expected_checksum, asset_dir):
    ensure_source(
        path=manifest_path, url=manifest_url, expected=expected_checksum,
        max_bytes=MAX_SUPPLEMENT_MANIFEST_BYTES, path_variable="SUPPLEMENT_MANIFEST_PATH",
        url_variable="SUPPLEMENT_MANIFEST_URL", checksum_variable="SUPPLEMENT_MANIFEST_SHA256",
        accept="application/json,application/octet-stream",
    )
    try:
        document = json.loads(manifest_path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CatalogBootstrapError("Дополнение должно быть корректным JSON") from exc
    sources = document.get("sources") if isinstance(document, dict) else None
    if not isinstance(sources, list) or not 0 < len(sources) <= 100:
        raise CatalogBootstrapError("Нет допустимого списка первичных источников")
    asset_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    total_source_bytes = 0
    for source in sources:
        if not isinstance(source, dict):
            raise CatalogBootstrapError("Неверная запись первичного источника")
        checksum = source.get("sha256")
        url = source.get("url")
        media_type = source.get("content_type")
        if not isinstance(checksum, str) or not SHA256.fullmatch(checksum):
            raise CatalogBootstrapError("Первичный источник требует SHA-256")
        if media_type not in {"text/html", "application/pdf"}:
            raise CatalogBootstrapError("Неподдерживаемый тип первичного источника")
        if not isinstance(url, str) or not url:
            raise CatalogBootstrapError("Нужен HTTPS-адрес первичного источника")
        path = ensure_source(
            path=asset_dir / f"{checksum}.bin", url=url, expected=checksum,
            max_bytes=MAX_SUPPLEMENT_SOURCE_BYTES, path_variable="SUPPLEMENT_ASSET_DIR",
            url_variable="source.url", checksum_variable="source.sha256",
            accept=media_type,
        )
        total_source_bytes += path.stat().st_size
        if total_source_bytes > MAX_SUPPLEMENT_TOTAL_SOURCE_BYTES:
            raise CatalogBootstrapError("Общий объём первичных снимков превышает предел")
    return manifest_path, asset_dir


def main():
    try:
        acquire_supplement(
            manifest_path=Path(os.getenv("SUPPLEMENT_MANIFEST_PATH") or DEFAULT_MANIFEST_PATH),
            manifest_url=os.getenv("SUPPLEMENT_MANIFEST_URL", ""),
            expected_checksum=os.getenv("SUPPLEMENT_MANIFEST_SHA256", ""),
            asset_dir=Path(os.getenv("SUPPLEMENT_ASSET_DIR") or DEFAULT_ASSET_DIR),
        )
    except (CatalogBootstrapError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print("Дополнение и первичные снимки проверены по SHA-256")
    return 0


if __name__ == "__main__":
    sys.exit(main())
