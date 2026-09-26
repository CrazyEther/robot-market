"""Acquire a bounded private snapshot of explicitly reviewed HTTPS sources."""

import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


RESEARCH_DIR = os.environ.get("SOURCE_RESEARCH_DIR")
if not RESEARCH_DIR:
    raise RuntimeError("SOURCE_RESEARCH_DIR должен указывать на закрытый каталог вне Git")
ROOT = Path(RESEARCH_DIR).resolve()
if ROOT.is_relative_to(Path(__file__).resolve().parents[2]):
    raise RuntimeError("SOURCE_RESEARCH_DIR должен находиться вне публичного репозитория")
MAX_BYTES = 5_000_000


def fetch(source):
    source_id = source["id"]
    url = source["url"]
    if not re.fullmatch(r"[a-z0-9_]+", source_id):
        raise ValueError("Неверный идентификатор источника")
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Требуется публичный HTTPS-адрес без учётных данных")
    request = urllib.request.Request(url, headers={
        "User-Agent": "RobotMarketSourceResearch/1.0 (+private verification)",
        "Accept": "text/html,application/xhtml+xml,application/pdf",
    })
    with urllib.request.urlopen(request, timeout=20) as response:
        final_url = response.geturl()
        final = urlparse(final_url)
        if final.scheme != "https" or final.hostname not in {parsed.hostname, f"www.{parsed.hostname}"}:
            raise ValueError("Перенаправление на другой источник требует отдельной проверки")
        mime = response.headers.get_content_type()
        extensions = {
            "text/html": ".html",
            "application/xhtml+xml": ".html",
            "application/pdf": ".pdf",
        }
        if mime not in extensions:
            raise ValueError(f"Неожиданный тип ответа: {mime}")
        body = response.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise ValueError("Ответ превышает лимит 5 МБ")
        if not body:
            raise ValueError("Источник вернул пустой документ")
        if mime == "application/pdf" and not body.startswith(b"%PDF-"):
            raise ValueError("Источник вернул файл без заголовка PDF")
        digest = hashlib.sha256(body).hexdigest()
        (ROOT / "raw").mkdir(parents=True, exist_ok=True)
        path = ROOT / "raw" / f"{source_id}-{digest}{extensions[mime]}"
        path.write_bytes(body)
        return {
            "status": "acquired", "final_url": final_url,
            "http_status": response.status, "content_type": mime,
            "content_length": len(body), "sha256": digest,
            "raw_file": str(path.relative_to(ROOT)),
            "charset": response.headers.get_content_charset(),
            "last_modified": response.headers.get("Last-Modified"),
            "etag": response.headers.get("ETag"),
        }


def main():
    sources = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))
    selected = {item for item in os.environ.get("SOURCE_IDS", "").split(",") if item}
    if selected:
        sources = [source for source in sources if source["id"] in selected]
        if {source["id"] for source in sources} != selected:
            raise ValueError("Unknown source ID in SOURCE_IDS")
    journal = ROOT / "acquisition.jsonl"
    failures = 0
    with journal.open("a", encoding="utf-8", newline="\n") as output:
        for source in sources:
            record = {**source, "fetched_at_utc": datetime.now(timezone.utc).isoformat()}
            try:
                record.update(fetch(source))
            except (OSError, ValueError, urllib.error.HTTPError, urllib.error.URLError) as exc:
                failures += 1
                record.update({"status": "failed", "error_type": type(exc).__name__,
                               "error": str(exc)[:300]})
            output.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            print(f"{source['id']}: {record['status']}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
