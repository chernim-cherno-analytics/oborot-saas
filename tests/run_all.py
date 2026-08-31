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

ПРИГОВОР ПО НАБОРУ (D-42, 24.08.2026). Раньше набор считался хорошим по правилу
«код возврата 0 и в отчёте нет FAIL». Под это правило попадал и набор, не
выполнивший НИ ОДНОЙ проверки: `test_ui.py` возвращал 0, не найдя playwright,
`test_backup.py` и `test_deploy.py` печатали «ИТОГО: 0 OK, 0 FAIL» при
отсутствии sqlite3 или flock. В прогоне CI 32716761631 набор ui показал 0/0
при общем зелёном итоге — то есть проверка, которой не было, выглядела как
пройденная. Теперь приговор выносит `classify()` и он ровно такой:

  PASS      канонический числовой отчёт «ИТОГО: N OK, M FAIL» (регистр слова
            не важен: четыре набора пишут «Итого»), ok > 0, fail = 0, rc = 0;
  SKIP      код возврата 77 И маркер «ПРОПУЩЕНО: <причина>» ОДНОВРЕМЕННО.
            Один сигнал без второго — не пропуск, а провал: пропуск без
            причины неотличим от поломки, а причина без кода — от набора,
            который написал «ПРОПУЩЕНО» про одну свою проверку и продолжил;
  NO_REPORT отчёта нет вовсе — сказать, что набор сделал, нечем;
  TIMEOUT   набор не уложился в потолок;
  FAIL      всё остальное, включая 0 OK / 0 FAIL и падения поверх пропуска.

Подсчёт строк «  OK   » остался, но только как диагностика (`counted_ok`):
по строкам не видно, дошёл набор до конца или оборвался на середине, поэтому
приговор он не выносит и в `ok` не попадает.

Запуск:
    python tests/run_all.py              # параллельно, столько же по времени,
                                         # сколько самый долгий набор
    python tests/run_all.py --serial     # по одному, если нужен чистый вывод
    python tests/run_all.py sync planner # только выбранные наборы
    python tests/run_all.py --require-all  # как в CI: пропуск = падение

Набор `sync` разрезан на четыре шарда (`sync`, `sync_p1`, `sync_resume`,
`sync_rebuild`) плюс обязательный сторож полноты `sync_parity`. Весь сценарий
целиком по-прежнему запускается одной командой: `python tests/test_sync.py`.

Код возврата: 0 — все наборы честно зелёные (локально допускается ЯВНЫЙ
пропуск, он виден в таблице вместе с причиной), 1 — есть падения, молчание,
0/0, отсутствие отчёта или незавершённый набор, 2 — ошибка вызова.
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
# Диапазоны намеренно не пересекаются со «своими» портами файлов (8801–8809,
# 9800, 9801, 9810, 9811), чтобы раннер можно было запустить, пока идёт ручной прогон.
#
# Пятое поле (необязательное) — аргументы командной строки набора. Оно есть
# ровно ради одного случая: `test_sync.py` — единственный сценарий на 413
# проверок, который в CI шёл 448.9 с и в одиночку задавал критический путь
# (прогон 32953333557: остальные 27 наборов вместе весят столько же, но идут по
# трём дорожкам). Сценарий разрезан на шарды: каждый исполняется своим
# процессом, со своей базой `test_oborot_<шард>.db` и своими портами отсюда,
# а недостающую подготовку доигрывает сам. Ни одна проверка при этом не
# потеряна и не выполняется дважды в зачёт: замок — `test_sync_shards.py`,
# он же обязательный набор `sync_parity`.
SUITES = [
    # имя,          файл,                     нужен мок МС, нужен мок телеграма
    ("runner",      "test_runner.py",          False, False),
    ("sync",        "test_sync.py",            True,  False, ("--shard", "sync")),
    ("sync_p1",     "test_sync.py",            True,  False, ("--shard", "sync_p1")),
    ("sync_resume", "test_sync.py",            True,  False, ("--shard", "sync_resume")),
    ("sync_rebuild", "test_sync.py",           True,  False, ("--shard", "sync_rebuild")),
    ("sync_parity", "test_sync_shards.py",     False, False),
    ("planner",     "test_planner.py",         False, False),
    ("account",     "test_account.py",         False, False),
    ("lessons",     "test_lessons.py",         False, False),
    ("isolation",   "test_isolation.py",       False, False),
    ("auth_csrf",   "test_auth_csrf.py",       False, False),
    ("tenancy",     "test_tenancy.py",         False, False),
    ("writeback",   "test_writeback.py",       True,  False),
    ("vendor",      "test_vendor.py",          True,  False),
    ("decision",    "test_decision_record.py", False, False),
    ("notify",      "test_notify.py",          True,  True),
    ("sync_wipe",   "test_sync_wipe.py",       True,  False),
    ("sync_supplier", "test_sync_supplier.py", True,  False),
    ("sync_late",   "test_sync_late_product.py", True, False),
    ("sync_diag_store", "test_sync_diag_store.py", True, False),
    ("sync_atomic", "test_sync_atomic.py",     True,  False),
    ("price_types", "test_price_types.py",     True,  False),
    ("wb_dup",      "test_writeback_dup.py",   True,  False),
    ("wb_idem",     "test_writeback_idempotency.py", True, False),
    ("wb_race",     "test_writeback_race.py",  True,  False),
    ("ms_client",   "test_ms_client.py",       False, False),
    ("sync_env",    "test_sync_env_parsing.py", False, False),
    ("ops",         "test_ops.py",             False, False),
    ("startup",     "test_startup_lifecycle.py", False, False),
    ("templates",   "test_template_stubs.py",  False, False),
    ("logging",     "test_logging.py",         False, False),
    ("subscr",      "test_subscription.py",    False, False),
    ("exec",        "test_execution.py",       False, False),
    # SUPPLY-1 (D-49/D-50): неизменяемый CC_BATCH_ID партии — миграция и
    # условный backfill на старой схеме, единый идентификатор во всех ручках
    # заказов, показ на /replenish. Набор офлайновый: ни мока МойСклада, ни
    # телеграма ему не нужно — внешних систем этот слой не касается вовсе, и
    # проверка этого входит в сам набор.
    ("supply",      "test_supply.py",          False, False),
    ("consist",     "test_consistency.py",     False, False),
    ("canon",       "test_turnover_canon.py",  False, False),
    # Замок на формулы потребности и largest-remainder (DATA-11). Стоит рядом
    # с `canon` намеренно: оба набора ничего не улучшают, оба фиксируют канон
    # как есть. Ключа --allow-skip у него нет и не будет: набор офлайновый,
    # ни сети, ни моков ему не нужно, и пропуститься ему не на чем (D-42).
    ("formula",     "test_formula_contract.py", False, False),
    # Операторская сверка месяца продаж с эталоном первой таблицы
    # (DATA-4/DATA-5). Набор офлайновый: ни мока МойСклада, ни телеграма ему
    # не нужно — эталон он поднимает сам локальным сервером на эфемерном
    # порту, поэтому раздавать ему порт не требуется.
    ("reconcile",   "test_reconcile_sales.py", False, False),
    ("ui",          "test_ui.py",              False, False),
    # Owner-only предпросмотр онбординга: доступ по ролям, ноль записей
    # (отпечаток всей базы до и после полного прохода), честность экрана,
    # адаптив и клавиатура. Набор браузерный, как и `ui`, поэтому стоит рядом
    # с ним; мока МойСклада и телеграма ему не нужно — данные синтетические,
    # из демо-сида самого проекта.
    ("onbprev",     "test_onboarding_preview.py", False, False),
    ("backup",      "test_backup.py",          False, False),
    ("deploy",      "test_deploy.py",          False, False),
    ("offsite",     "test_offsite.py",         False, False),
    ("deps",        "test_dependencies.py",    False, False),
]

# «ИТОГО» и «Итого» — один и тот же отчёт: половина наборов написана так,
# половина эдак. Регистр слова не должен решать, засчитана работа или нет.
TOTAL_RE = re.compile(r"ИТОГО:\s*(\d+)\s*OK,\s*(\d+)\s*FAIL", re.IGNORECASE)
# Причина обязана быть непустой: «ПРОПУЩЕНО:» без текста — это не причина.
#
# Маркер читается ТОЛЬКО с начала строки, без отступа: это заявление самого
# набора, а не строка из вывода вложенного скрипта. Случай живой:
# `tests/test_backup.py` запускает `deploy/backup.sh`, тот печатает
# «   ПРОПУЩЕНО: BACKUP_REMOTE не задан.» про свой необязательный шаг — и с
# отступом-безразличным правилом полностью отработавший набор из 21 проверки
# получал приговор «маркер при коде возврата 0», то есть падение на ровном
# месте. Прод-скрипт при этом трогать нечем и незачем: он говорит правду про
# себя, просто не за набор.
SKIP_RE = re.compile(r"^ПРОПУЩЕНО:[ \t]*(\S.*?)\s*$", re.MULTILINE)
OK_LINE_RE = re.compile(r"^ {2}OK {3}", re.MULTILINE)
FAIL_LINE_RE = re.compile(r"^ {2}FAIL ", re.MULTILINE)

SKIP_RC = 77       # код «набор сознательно не выполнялся»
TIMEOUT_RC = 124   # код таймаута, как у одноимённой утилиты

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"
NO_REPORT = "NO_REPORT"
TIMEOUT = "TIMEOUT"


def classify(out: str, rc: int) -> dict:
    """Приговор по выводу набора и коду возврата. Единственное место, где он есть.

    Функция чистая: ни файлов, ни процессов, ни времени — её и проверяет
    `tests/test_runner.py` таблицей истинности.
    """
    # ПОСЛЕДНИЙ отчёт, а не первый: итог набор подводит в конце. Первый же
    # найденный — это иногда строка, которую набор разбирает или цитирует
    # (`tests/test_runner.py` печатает «ИТОГО: 0 OK, 0 FAIL» в названии
    # проверки, и раннер засчитывал ему 3 OK вместо 42).
    matches = TOTAL_RE.findall(out)
    has_report = bool(matches)
    ok, fail = (int(matches[-1][0]), int(matches[-1][1])) if has_report else (0, 0)
    sm = SKIP_RE.search(out)
    reason = sm.group(1) if sm else ""
    res = {
        "ok": ok, "fail": fail, "rc": rc, "reason": reason,
        "report": has_report,
        # Диагностика: сколько строк проверок видно глазами. Приговора не
        # выносит — оборванный набор печатает такие же строки, что и целый.
        "counted_ok": len(OK_LINE_RE.findall(out)),
        "counted_fail": len(FAIL_LINE_RE.findall(out)),
    }

    if rc == TIMEOUT_RC:
        res["verdict"] = TIMEOUT
    elif fail:
        # Падение важнее пропуска: набор что-то выполнил и что-то уронил.
        res["verdict"] = FAIL
    elif rc == SKIP_RC or reason:
        # Заявка на пропуск. Засчитывается только целиком: код И причина.
        res["verdict"] = SKIP if (rc == SKIP_RC and reason) else FAIL
    elif not has_report:
        res["verdict"] = NO_REPORT
    elif rc != 0 or ok == 0:
        res["verdict"] = FAIL
    else:
        res["verdict"] = PASS
    return res


def run_one(idx: int, name: str, filename: str, needs_ms: bool, needs_tg: bool,
            timeout: int, extra_args: tuple = ()) -> dict:
    env = dict(os.environ)
    env["OBOROT_TEST_PORT"] = str(8811 + idx)
    # Набор бэкапов поднимает приложение сам, скриптом restore_test.sh, — свой
    # порт ему нужен отдельно, иначе при параллельном прогоне он столкнётся с
    # приложением соседнего набора.
    env["BACKUP_TEST_PORT"] = str(8861 + idx)
    env["OFFSITE_TEST_PORT"] = str(8881 + idx)
    if needs_ms:
        env["OBOROT_MOCK_PORT"] = str(9821 + idx)
    if needs_tg:
        env["OBOROT_TG_PORT"] = str(9841 + idx)
    env["SCHEDULER_ENABLED"] = "0"
    started = time.time()
    try:
        p = subprocess.run([sys.executable, str(TESTS / filename), *extra_args],
                           cwd=str(ROOT),
                           env=env, capture_output=True, text=True, timeout=timeout)
        out, rc = p.stdout + p.stderr, p.returncode
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or "") + (exc.stderr or "") if isinstance(exc.stdout, str) else ""
        out += f"\n[раннер] набор не завершился за {timeout} с"
        rc = TIMEOUT_RC
    res = classify(out, rc)
    res.update({"name": name, "sec": round(time.time() - started, 1), "out": out})
    return res


def is_good(r: dict, require_all: bool, allow_skip: set) -> bool:
    """Засчитан ли набор.

    Локально явный пропуск не красит прогон в красный — но он и не зелёный:
    в таблице он стоит как SKIP с причиной. В CI (`--require-all`) пропуск
    засчитан только если набор назван в `--allow-skip` поимённо.
    """
    if r["verdict"] == PASS:
        return True
    if r["verdict"] == SKIP:
        return (not require_all) or (r["name"] in allow_skip)
    return False


def explain(r: dict) -> str:
    """Короткая приписка к строке таблицы — почему набор не засчитан."""
    if r["verdict"] == PASS:
        return ""
    if r["verdict"] == SKIP:
        return f"SKIP — ПРОПУЩЕНО: {r['reason']}"
    if r["verdict"] == TIMEOUT:
        return "TIMEOUT — набор не завершился"
    if r["verdict"] == NO_REPORT:
        return (f"NO_REPORT — канонического «ИТОГО: N OK, M FAIL» нет "
                f"(строк проверок видно: {r['counted_ok']} OK / "
                f"{r['counted_fail']} FAIL)")
    if r["fail"]:
        # Падения называются первыми: приписка «маркер при коде возврата 1»
        # поверх пяти настоящих падений уводит читателя не туда.
        return f"FAIL — падений {r['fail']}, код возврата {r['rc']}"
    if r["rc"] == SKIP_RC and not r["reason"]:
        return "FAIL — код 77 без маркера «ПРОПУЩЕНО: <причина>»"
    if r["reason"] and r["rc"] != SKIP_RC:
        return f"FAIL — маркер «ПРОПУЩЕНО» при коде возврата {r['rc']}"
    if r["report"] and r["ok"] == 0 and r["fail"] == 0:
        return "FAIL — 0 OK, 0 FAIL: не выполнено ни одной проверки"
    return f"FAIL — код возврата {r['rc']}, падений {r['fail']}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Прогон всех наборов тестов «Оборота»")
    ap.add_argument("only", nargs="*", help="имена наборов (по умолчанию все)")
    ap.add_argument("--serial", action="store_true", help="по одному, а не параллельно")
    ap.add_argument("--jobs", type=int, default=0,
                    help="сколько наборов гнать одновременно (0 = все; на слабой "
                         "машине и в CI ставьте 2-3, иначе наборы мешают друг другу)")
    ap.add_argument("--timeout", type=int, default=1200, help="потолок на набор, с")
    ap.add_argument("-v", "--verbose", action="store_true", help="печатать вывод наборов")
    ap.add_argument("--require-all", action="store_true",
                    help="режим CI: обязателен ВЕСЬ набор наборов, каждый должен "
                         "быть честно зелёным; пропуск считается падением")
    ap.add_argument("--allow-skip", action="append", default=[], metavar="НАБОР",
                    help="разрешить ЯВНЫЙ пропуск названного набора при "
                         "--require-all (можно повторять). В CI «Оборота» не "
                         "используется ни разу — исключение здесь заводится "
                         "только вместе с записью в TECH_DEBT.md")
    args = ap.parse_args()

    known = {s[0] for s in SUITES}
    unknown = set(args.only) - known
    if unknown:
        print(f"Неизвестные наборы: {', '.join(sorted(unknown))}")
        print(f"Доступные: {', '.join(s[0] for s in SUITES)}")
        return 2
    unknown_skip = set(args.allow_skip) - known
    if unknown_skip:
        print(f"--allow-skip называет несуществующие наборы: "
              f"{', '.join(sorted(unknown_skip))}")
        return 2
    if args.require_all and args.only and set(args.only) != known:
        # «Все зарегистрированные наборы обязательны» и «гоним три штуки» —
        # несовместимые требования. Молча сузить набор здесь опаснее отказа:
        # получился бы зелёный CI по трети проверок.
        print("--require-all требует полного набора наборов, а названы только: "
              f"{', '.join(sorted(args.only))}")
        print("Уберите имена наборов или уберите --require-all.")
        return 2

    suites = [s for s in SUITES if not args.only or s[0] in args.only]
    for pattern in ("test_*.db", "test_*.db-wal", "test_*.db-shm"):
        for f in ROOT.glob(pattern):
            f.unlink()

    mode = ("последовательно" if args.serial
            else f"параллельно по {args.jobs}" if args.jobs else "параллельно")
    strict = " · режим CI: обязательны все наборы" if args.require_all else ""
    print(f"Наборов: {len(suites)} · режим: {mode}{strict}\n")
    started = time.time()
    # Пятое поле необязательное: старые записи из четырёх полей продолжают
    # работать (ими же подменяет реестр tests/test_runner.py).
    jobs = [(i, s[0], s[1], s[2], s[3], args.timeout,
             tuple(s[4]) if len(s) > 4 else ())
            for i, s in enumerate(suites)]
    if args.serial:
        results = [run_one(*j) for j in jobs]
    else:
        workers = args.jobs if args.jobs and args.jobs > 0 else len(jobs)
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            results = list(pool.map(lambda j: run_one(*j), jobs))

    allow_skip = set(args.allow_skip)
    total_ok = total_fail = 0
    print(f"{'набор':<12} {'OK':>6} {'FAIL':>6} {'сек':>7}  приговор")
    print("-" * 44)
    for r in sorted(results, key=lambda x: -x["ok"]):
        total_ok += r["ok"]
        total_fail += r["fail"]
        note = explain(r)
        mark = f"  ← {note}" if note else f"  {PASS}"
        print(f"{r['name']:<12} {r['ok']:>6} {r['fail']:>6} {r['sec']:>7}{mark}")
    print("-" * 44)
    print(f"{'ВСЕГО':<12} {total_ok:>6} {total_fail:>6} "
          f"{round(time.time()-started,1):>7}")

    skipped = [r for r in results if r["verdict"] == SKIP]
    bad = [r for r in results if not is_good(r, args.require_all, allow_skip)]

    if skipped:
        print("\nПропущено (это НЕ зелёный результат, а отсутствие проверки):")
        for r in skipped:
            print(f"  {r['name']}: ПРОПУЩЕНО: {r['reason']}")

    for r in bad:
        print(f"\n===== {r['name']} · {r['verdict']} (код возврата {r['rc']}) =====")
        lines = [ln for ln in r["out"].splitlines() if "FAIL" in ln or "Traceback" in ln]
        print("\n".join(lines[-30:]) or r["out"][-2000:])
    if args.verbose:
        for r in results:
            print(f"\n===== {r['name']} =====\n{r['out']}")

    if bad:
        print(f"\nНЕ ЗАСЧИТАНО наборов: {len(bad)} — "
              f"{', '.join(r['name'] + ' (' + r['verdict'] + ')' for r in bad)}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
