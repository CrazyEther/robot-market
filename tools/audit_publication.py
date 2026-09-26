"""Reject private source files, legacy seed directories and credentials from Git input.

The audit reads the Git index and visible untracked files, so it works before
commit as well as on the checked-out tree in CI. It reports paths only.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRIVATE_SUFFIXES = frozenset({
    ".csv", ".tsv", ".xlsx", ".xls", ".docx", ".pdf", ".parquet",
    ".sqlite3", ".db", ".pem", ".key", ".p12", ".pfx",
})
PRIVATE_PREFIXES = (
    "source/", "research/raw/", "research/derived/",
    "demo/scenario_data/", "demo/robot_data/",
)
TOKEN_PATTERNS = (
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"\bghp_[A-Za-z0-9]{30,}\b"),
    re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{30,}\b"),
    re.compile(rb"\bsk-[A-Za-z0-9_-]{32,}\b"),
    re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
)


def candidate_paths() -> list[str]:
    completed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT, check=True, capture_output=True,
    )
    return sorted({part.decode("utf-8", errors="surrogateescape")
                   for part in completed.stdout.split(b"\0") if part})


def audit() -> list[tuple[str, str]]:
    findings = []
    for relative in candidate_paths():
        normalized = relative.replace("\\", "/")
        path = ROOT / relative
        if path.is_symlink():
            findings.append((relative, "символическая ссылка в публичном наборе"))
            continue
        if not path.is_file():
            continue
        if (normalized == ".env" or normalized.startswith(PRIVATE_PREFIXES)
                or path.suffix.lower() in PRIVATE_SUFFIXES):
            findings.append((relative, "закрытый исходник, старый seed или файл ключа"))
            continue
        try:
            content = path.read_bytes()
        except OSError:
            findings.append((relative, "файл нельзя прочитать для проверки"))
            continue
        if any(pattern.search(content) for pattern in TOKEN_PATTERNS):
            findings.append((relative, "возможный секрет"))
    if (ROOT / "static/fonts/DejaVuSans.ttf").is_file() and not (
        ROOT / "static/fonts/LICENSE-DejaVu.txt"
    ).is_file():
        findings.append(("static/fonts/DejaVuSans.ttf", "нет файла лицензии шрифта"))
    return findings


def main() -> int:
    findings = audit()
    for relative, reason in findings:
        print(f"{relative}: {reason}", file=sys.stderr)
    if findings:
        print(f"Публикационный аудит: {len(findings)} проблем.", file=sys.stderr)
        return 1
    print("Публикационный аудит: закрытых файлов, старых seed-каталогов и секретов не найдено.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
