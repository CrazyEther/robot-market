"""Obtain the permitted v4 source from a mounted file or private HTTPS URL."""

import hashlib
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from catalog.limits import MAX_CATALOG_BYTES


DEFAULT_SOURCE_PATH = Path(tempfile.gettempdir()) / "robot-market-catalog-v4.csv"
CHECKSUM_PATTERN = re.compile(r"[0-9a-fA-F]{64}")


class CatalogBootstrapError(ValueError):
    pass


class HTTPSRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        target = urlparse(newurl)
        if target.scheme != "https" or not target.hostname or target.username or target.password:
            raise CatalogBootstrapError("Перенаправление источника нарушило HTTPS")
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def verify_bytes(content, expected, *, max_bytes=MAX_CATALOG_BYTES,
                 checksum_variable="CATALOG_SOURCE_SHA256"):
    if not CHECKSUM_PATTERN.fullmatch(expected or ""):
        raise CatalogBootstrapError(f"{checksum_variable} должен содержать 64 шестнадцатеричных символа")
    if not content or len(content) > max_bytes:
        raise CatalogBootstrapError("Файл источника пуст или превышает допустимый размер")
    if hashlib.sha256(content).hexdigest() != expected.lower():
        raise CatalogBootstrapError("Контрольная сумма источника не совпадает с ожидаемой")


def _read_local(path, max_bytes):
    try:
        with path.open("rb") as source:
            return source.read(max_bytes + 1)
    except OSError as exc:
        raise CatalogBootstrapError("Не удалось прочитать подключённый источник") from exc


def _download(url, max_bytes, accept):
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise CatalogBootstrapError("CATALOG_SOURCE_URL должен быть HTTPS-адресом без встроенных учётных данных")
    request = urllib.request.Request(url, headers={"Accept": accept})
    try:
        opener = urllib.request.build_opener(HTTPSRedirectHandler)
        with opener.open(request, timeout=30) as response:
            if urlparse(response.geturl()).scheme != "https":
                raise CatalogBootstrapError("Перенаправление источника нарушило HTTPS")
            content = response.read(max_bytes + 1)
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        raise CatalogBootstrapError("Не удалось получить файл из закрытого HTTPS-источника") from exc
    return content


def ensure_source(*, path, url, expected, max_bytes=MAX_CATALOG_BYTES,
                  path_variable="CATALOG_SOURCE_PATH", url_variable="CATALOG_SOURCE_URL",
                  checksum_variable="CATALOG_SOURCE_SHA256",
                  accept="text/csv,application/octet-stream"):
    if path.is_file():
        content = _read_local(path, max_bytes)
        verify_bytes(content, expected, max_bytes=max_bytes, checksum_variable=checksum_variable)
        return path
    if not url:
        raise CatalogBootstrapError(f"Нужен подключённый {path_variable} или {url_variable}")
    content = _download(url, max_bytes, accept)
    verify_bytes(content, expected, max_bytes=max_bytes, checksum_variable=checksum_variable)
    temp_name = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=".catalog-", delete=False) as target:
            temp_name = target.name
            os.chmod(temp_name, 0o600)
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temp_name, path)
    except OSError as exc:
        raise CatalogBootstrapError("Не удалось сохранить проверенный каталог") from exc
    finally:
        if temp_name is not None:
            Path(temp_name).unlink(missing_ok=True)
    return path


def main():
    path = Path(os.getenv("CATALOG_SOURCE_PATH") or DEFAULT_SOURCE_PATH)
    try:
        ensure_source(
            path=path,
            url=os.getenv("CATALOG_SOURCE_URL", ""),
            expected=os.getenv("CATALOG_SOURCE_SHA256", ""),
        )
    except CatalogBootstrapError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print("Источник каталога проверен по SHA-256")
    return 0


if __name__ == "__main__":
    sys.exit(main())
