# -*- coding: utf-8 -*-
"""Прогон всех наборов тестов одной командой.

Зачем. Тесты в проекте — сценарные скрипты, каждый поднимает приложение (а часть
и мок МойСклада) на своём порту. Это удобно читать и удобно отлаживать, но до
сих пор не было ни одной команды «прогнать всё», ни кода возврата для CI, а
жёсткие порты не давали запускать наборы параллельно: `test_notify` и
`test_writeback` оба сидели на 8802, `test_account` и `test_planner` — на 8804,
мок МойСклада всегда на 9800 и нужен четырём наборам сразу.

Теперь порты приходят из окружения (`OBOROT_TEST_PORT`, `OBOROT_MOCK_PORT`,
`OBOROT_TG_PORT`), а этот раннер раздаёт каждому набору свои. Значения по
умолчанию в самих файлах не изменились — запуск по одному работает как раньше.

Запуск:
    python tests/run_all.py              # параллельно, столько же по времени,
                                         # сколько самый долгий набор (~5 мин)
    python tests/run_all.py --serial     # по одному, если нужен чистый вывод
    python tests/run_all.py sync planner # только выбранные наборы

Код возврата: 0 — всё зелёное, 1 — есть падения или набор не завершился.
"""
import argparse
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"

# Порты разведены с запасом: приложение 8811+, моки МойСклада 9821+, телеграм 9841+.
# Диапазоны намеренно не пересекаются со «своими» портами файлов (8801–8808,
# 9800, 9801, 9810), чтобы раннер можно было запустить, пока идёт ручной прогон.
SUITES = [
    # имя,          файл,                     нужен мок МС, нужен мок телеграма
    ("sync",        "test_sync.py",            True,  False),
    ("planner",     "test_planner.py",         False, False),
    ("account",     "test_account.py",         False, False),
    ("lessons",     "test_lessons.py",         False, False),
    ("isolation",   "test_isolation.py",       False, False),
    ("writeback",   "test_writeback.py",       True,  False),
    ("vendor",      "test_vendor.py",          True,  False),
    ("decision",    "test_decision_record.py", False, False),
    ("notify",      "test_notify.py",          True,  True),
    ("sync_wipe",   "test_sync_wipe.py",       True,  False),
    ("wb_dup",      "test_writeback_dup.py",   True,  False),
    ("ops",         "test_ops.py",             False, False),
    ("logging",     "test_logging.py",         False, False),
    ("subscr",      "test_subscription.py",    False, False),
    ("exec",        "test_execution.py",       False, False),
    ("consist",     "test_consistency.py",     False, False),
    ("ui",          "test_ui.py",              False, False),
    ("backup",      "test_backup.py",          False, False),
]

TOTAL_RE = re.compile(r"ИТОГО:\s*(\d+)\s*OK,\s*(\d+)\s*FAIL")


def run_one(idx: int, name: str, filename: str, needs_ms: bool, needs_tg: bool,
            timeout: int) -> dict:
    env = dict(os.environ)
    env["OBOROT_TEST_PORT"] = str(8811 + idx)
    # Набор бэкапов поднимает приложение сам, скриптом restore_test.sh, — свой
    # порт ему нужен отдельно, иначе при параллельном прогоне он столкнётся с
    # приложением соседнего набора.
    env["BACKUP_TEST_PORT"] = str(8861 + idx)
    if needs_ms:
        env["OBOROT_MOCK_PORT"] = str(9821 + idx)
    if needs_tg:
        env["OBOROT_TG_PORT"] = str(9841 + idx)
    env["SCHEDULER_ENABLED"] = "0"
    started = time.time()
    try:
        p = subprocess.run([sys.executable, str(TESTS / filename)], cwd=str(ROOT),
                           env=env, capture_output=True, text=True, timeout=timeout)
        out, rc = p.stdout + p.stderr, p.returncode
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or "") + (exc.stderr or "") if isinstance(exc.stdout, str) else ""
        out += f"\n[раннер] набор не завершился за {timeout} с"
        rc = 124
    m = TOTAL_RE.search(out)
    ok, fail = (int(m.group(1)), int(m.group(2))) if m else (
        out.count("\n  OK   "), out.count("\n  FAIL "))
    return {"name": name, "ok": ok, "fail": fail, "rc": rc,
            "sec": round(time.time() - started, 1), "out": out}


def main() -> int:
    ap = argparse.ArgumentParser(description="Прогон всех наборов тестов «Оборота»")
    ap.add_argument("only", nargs="*", help="имена наборов (по умолчанию все)")
    ap.add_argument("--serial", action="store_true", help="по одному, а не параллельно")
    ap.add_argument("--jobs", type=int, default=0,
                    help="сколько наборов гнать одновременно (0 = все; на слабой "
                         "машине и в CI ставьте 2-3, иначе наборы мешают друг другу)")
    ap.add_argument("--timeout", type=int, default=1200, help="потолок на набор, с")
    ap.add_argument("-v", "--verbose", action="store_true", help="печатать вывод наборов")
    args = ap.parse_args()

    suites = [s for s in SUITES if not args.only or s[0] in args.only]
    unknown = set(args.only) - {s[0] for s in SUITES}
    if unknown:
        print(f"Неизвестные наборы: {', '.join(sorted(unknown))}")
        print(f"Доступные: {', '.join(s[0] for s in SUITES)}")
        return 2
    for pattern in ("test_*.db", "test_*.db-wal", "test_*.db-shm"):
        for f in ROOT.glob(pattern):
            f.unlink()

    mode = ("последовательно" if args.serial
            else f"параллельно по {args.jobs}" if args.jobs else "параллельно")
    print(f"Наборов: {len(suites)} · режим: {mode}\n")
    started = time.time()
    jobs = [(i, *s, args.timeout) for i, s in enumerate(suites)]
    if args.serial:
        results = [run_one(*j) for j in jobs]
    else:
        workers = args.jobs if args.jobs and args.jobs > 0 else len(jobs)
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            results = list(pool.map(lambda j: run_one(*j), jobs))

    total_ok = total_fail = 0
    print(f"{'набор':<12} {'OK':>6} {'FAIL':>6} {'сек':>7}")
    print("-" * 34)
    for r in sorted(results, key=lambda x: -x["ok"]):
        total_ok += r["ok"]
        total_fail += r["fail"]
        mark = "" if r["rc"] == 0 and r["fail"] == 0 else "  ← "
        mark += "не завершился" if r["rc"] == 124 else ("падения" if r["rc"] else "")
        print(f"{r['name']:<12} {r['ok']:>6} {r['fail']:>6} {r['sec']:>7}{mark}")
    print("-" * 34)
    print(f"{'ВСЕГО':<12} {total_ok:>6} {total_fail:>6} {round(time.time()-started,1):>7}")

    bad = [r for r in results if r["rc"] != 0 or r["fail"]]
    for r in bad:
        print(f"\n===== {r['name']} (код возврата {r['rc']}) =====")
        lines = [ln for ln in r["out"].splitlines() if "FAIL" in ln or "Traceback" in ln]
        print("\n".join(lines[-30:]) or r["out"][-2000:])
    if args.verbose:
        for r in results:
            print(f"\n===== {r['name']} =====\n{r['out']}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
