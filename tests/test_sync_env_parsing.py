# -*- coding: utf-8 -*-
"""OPS-6: разбор HISTORY_DAYS и SYNC_DAYS_BACK терпит мусор в окружении.

До правки обе переменные читались в `app/ms_sync.py` голым
`int(os.environ.get(NAME, default))`: мусорное значение ("abc") роняет
импорт модуля исключением, а `0`/отрицательное значение проходит как есть и
ломает вниз по коду окна истории и инкремента (см. `HISTORY_DAYS` и
`SALES_RESYNC_DAYS` в `app/ms_sync.py`). `app/ms_client._env_int` уже решает
эту задачу для других переменных (терпимость к пробелам и мусору, нижняя
граница) — здесь то же самое проверяется для двух оставшихся.

Каждый случай — отдельный дочерний процесс с чистым окружением: импорт
модуля не должен зависеть от состояния родителя, а падение импорта в одном
случае не должно портить остальные.

Запуск из корня репозитория:  python tests/test_sync_env_parsing.py
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        PASS.append(name)
        print(f"  OK   {name}" + (f"  [{detail}]" if detail else ""))
    else:
        FAIL.append(name)
        print(f"  FAIL {name}  {detail}")


CODE = (
    "import app.ms_sync as s\n"
    "print('RESULT', s.HISTORY_DAYS, s.SALES_RESYNC_DAYS)\n"
)


def _child(env_extra: dict) -> tuple[int, str]:
    """Импортирует app.ms_sync в чистом дочернем процессе с заданным окружением."""
    env = dict(os.environ)
    env["DATABASE_URL"] = "sqlite:///:memory:"
    env.pop("HISTORY_DAYS", None)
    env.pop("SYNC_DAYS_BACK", None)
    env.update(env_extra)
    p = subprocess.run([sys.executable, "-c", CODE], cwd=str(ROOT), env=env,
                        capture_output=True, text=True, timeout=60)
    return p.returncode, (p.stdout + p.stderr)


def _result(env_extra: dict) -> tuple[int, int] | None:
    rc, out = _child(env_extra)
    if rc != 0:
        return None
    for line in out.splitlines():
        if line.startswith("RESULT "):
            _, history, resync = line.split()
            return int(history), int(resync)
    return None


# (метка кейса, env, ожидаемый HISTORY_DAYS, ожидаемый SALES_RESYNC_DAYS)
CASES = [
    ("unset -> дефолты 730/3",
     {}, 730, 3),
    ("валидные значения",
     {"HISTORY_DAYS": "400", "SYNC_DAYS_BACK": "10"}, 400, 10),
    ("пробелы вокруг значения",
     {"HISTORY_DAYS": "  400  ", "SYNC_DAYS_BACK": "  10  "}, 400, 10),
    ("мусор -> откат на дефолты 730/3",
     {"HISTORY_DAYS": "not-a-number", "SYNC_DAYS_BACK": "abc"}, 730, 3),
    ("ноль -> нижняя граница 1",
     {"HISTORY_DAYS": "0", "SYNC_DAYS_BACK": "0"}, 1, 1),
    ("отрицательное -> нижняя граница 1",
     {"HISTORY_DAYS": "-5", "SYNC_DAYS_BACK": "-30"}, 1, 1),
]


def main() -> int:
    print("\n== HISTORY_DAYS / SYNC_DAYS_BACK: разбор env (OPS-6) ==")
    for label, env_extra, exp_history, exp_resync in CASES:
        res = _result(env_extra)
        check(f"{label}: импорт не падает", res is not None, f"env={env_extra}")
        if res is None:
            continue
        history, resync = res
        check(f"{label}: HISTORY_DAYS == {exp_history}", history == exp_history,
              f"got={history}")
        check(f"{label}: SALES_RESYNC_DAYS == {exp_resync}", resync == exp_resync,
              f"got={resync}")

    print(f"\nИТОГО: {len(PASS)} OK, {len(FAIL)} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
