"""Check a mounted evidence manifest or obtain it from private HTTPS storage."""

import os
import sys
import tempfile
from pathlib import Path

from catalog.bootstrap_source import CatalogBootstrapError, ensure_source
from catalog.limits import MAX_EVIDENCE_BYTES


DEFAULT_EVIDENCE_PATH = Path(tempfile.gettempdir()) / "robot-market-verified-claims.json"


def main():
    path = Path(os.getenv("RESEARCH_CLAIMS_PATH") or DEFAULT_EVIDENCE_PATH)
    try:
        ensure_source(
            path=path,
            url=os.getenv("RESEARCH_CLAIMS_URL", ""),
            expected=os.getenv("RESEARCH_CLAIMS_SHA256", ""),
            max_bytes=MAX_EVIDENCE_BYTES,
            path_variable="RESEARCH_CLAIMS_PATH",
            url_variable="RESEARCH_CLAIMS_URL",
            checksum_variable="RESEARCH_CLAIMS_SHA256",
            accept="application/json,application/octet-stream",
        )
    except CatalogBootstrapError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print("Реестр первичных источников проверен по SHA-256")
    return 0


if __name__ == "__main__":
    sys.exit(main())
