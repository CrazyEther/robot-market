"""Bind administrator-uploaded primary files to manifest sources by their bytes."""

import hashlib
import json
import re

from catalog.limits import (
    MAX_SUPPLEMENT_MANIFEST_BYTES, MAX_SUPPLEMENT_SOURCE_BYTES,
    MAX_SUPPLEMENT_TOTAL_SOURCE_BYTES,
)
from catalog.supplement_importing import SupplementImportError


SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def source_assets_from_uploads(manifest_bytes, uploads):
    if (not isinstance(manifest_bytes, bytes)
            or not 0 < len(manifest_bytes) <= MAX_SUPPLEMENT_MANIFEST_BYTES):
        raise SupplementImportError("Файл дополнения пуст или слишком велик")
    try:
        document = json.loads(manifest_bytes.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SupplementImportError("Файл дополнения должен быть корректным JSON") from exc
    sources = document.get("sources") if isinstance(document, dict) else None
    if not isinstance(sources, list) or not 0 < len(sources) <= 100:
        raise SupplementImportError("Нет допустимого списка первичных источников")
    if not isinstance(uploads, (list, tuple)) or not 0 < len(uploads) <= 100:
        raise SupplementImportError("Передайте первичные файлы производителя")
    by_checksum = {}
    total = 0
    for upload in uploads:
        raw = upload.read(MAX_SUPPLEMENT_SOURCE_BYTES + 1)
        total += len(raw)
        if not 0 < len(raw) <= MAX_SUPPLEMENT_SOURCE_BYTES or total > MAX_SUPPLEMENT_TOTAL_SOURCE_BYTES:
            raise SupplementImportError("Первичные файлы пусты или превышают допустимый объём")
        checksum = hashlib.sha256(raw).hexdigest()
        if checksum in by_checksum:
            raise SupplementImportError("Один первичный файл передан повторно")
        by_checksum[checksum] = raw
    assets = {}
    used = set()
    for source in sources:
        if not isinstance(source, dict):
            raise SupplementImportError("Неверная запись первичного источника")
        ref, checksum = source.get("source_ref"), source.get("sha256")
        if (not isinstance(ref, str) or not ref or ref in assets
                or not isinstance(checksum, str) or not SHA256.fullmatch(checksum)
                or checksum not in by_checksum):
            raise SupplementImportError("Первичный файл не совпадает с источником дополнения")
        assets[ref] = by_checksum[checksum]
        used.add(checksum)
    if used != set(by_checksum):
        raise SupplementImportError("Передан файл, не указанный в дополнении")
    return assets
