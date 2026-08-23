# -*- coding: utf-8 -*-
"""Fail closed when the exact dependency lock is missing or not installed in CI."""
from __future__ import annotations

import importlib.metadata
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "requirements.lock"
INTENT = ROOT / "requirements.txt"
PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    print(f"  {'OK  ' if condition else 'FAIL'} {name}" + (f"  [{detail}]" if detail else ""))


def normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def pins() -> tuple[dict[str, str], list[str]]:
    result, invalid = {}, []
    for raw in LOCK.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^\s;]+)", line)
        if not match:
            invalid.append(line)
            continue
        result[normalized(match.group(1))] = match.group(2)
    return result, invalid


def direct_names() -> set[str]:
    result = set()
    for raw in INTENT.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name = re.split(r"[<>=!~\s\[]", line, maxsplit=1)[0]
        result.add(normalized(name))
    return result


def main() -> int:
    print("\n== Exact dependency lock ==")
    check("requirements.lock существует и не пуст", LOCK.is_file() and LOCK.stat().st_size > 0)
    locked, invalid = pins() if LOCK.is_file() else ({}, ["missing"])
    check("каждая исполнимая строка — точный == pin", not invalid, str(invalid[:5]))
    check("lock содержит полное замыкание, а не только прямой список",
          len(locked) > len(direct_names()), f"lock={len(locked)} direct={len(direct_names())}")
    missing_direct = direct_names() - set(locked)
    check("каждая прямая зависимость присутствует в lock",
          not missing_direct, str(sorted(missing_direct)))

    pip_check = subprocess.run([sys.executable, "-m", "pip", "check"],
                               capture_output=True, text=True)
    check("pip check не находит несовместимых зависимостей",
          pip_check.returncode == 0, (pip_check.stdout + pip_check.stderr)[-300:])

    mismatches = []
    for name, expected in sorted(locked.items()):
        try:
            actual = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            actual = "missing"
        if actual != expected:
            mismatches.append(f"{name}: {actual} != {expected}")

    strict = os.environ.get("CI", "").lower() == "true" \
        or os.environ.get("OBOROT_ENFORCE_LOCK") == "1"
    if strict:
        check("CI установил ровно версии requirements.lock",
              not mismatches, "; ".join(mismatches[:8]))
    else:
        check("локальное окружение сверено; точное совпадение обязательно в CI",
              True, "совпадает" if not mismatches else f"отклонений: {len(mismatches)}")

    print(f"\nИТОГО: {len(PASS)} OK, {len(FAIL)} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
