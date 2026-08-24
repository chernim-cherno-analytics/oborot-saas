# -*- coding: utf-8 -*-
"""Приговор раннера: что считается успехом, пропуском и провалом.

Зачем этот набор существует. Раннер `tests/run_all.py` — единственное, что
читает CI. Пока он считал набор хорошим по правилу «код возврата 0 и в отчёте
нет FAIL», зелёным получался и набор, не выполнивший НИ ОДНОЙ проверки:
`test_ui.py` возвращал 0, когда не находил playwright, `test_backup.py` и
`test_deploy.py` печатали «ИТОГО: 0 OK, 0 FAIL», когда в системе не было
sqlite3 или flock. В прогоне CI 32716761631 набор ui показал 0/0 при общем
зелёном итоге: проверка, которой не было, выглядела как пройденная.

Контракт, который здесь закрепляется (ACK Codex от 24.08.2026):

  * УСПЕХ — только канонический числовой отчёт «ИТОГО: N OK, M FAIL»
    (регистр слова не важен), ok > 0, fail = 0, код возврата 0;
  * ПРОПУСК — только код возврата 77 И маркер «ПРОПУЩЕНО: <причина>»
    ОДНОВРЕМЕННО. Один сигнал без второго — не пропуск, а провал;
  * тишина, 0/0, отсутствие отчёта и таймаут — провал;
  * подсчёт строк «  OK   » остаётся диагностикой и никогда не выносит
    приговор: по строкам не видно, дошёл набор до конца или оборвался.

Проверяется НАСТОЯЩИЙ раннер на фиктивных наборах из `tests/runner_fixtures/`:
каждый ведёт себя ровно одним способом, приложение не поднимает и портов не
занимает. Раннер под проверкой берётся из переменной `OBOROT_RUNNER`
(по умолчанию `tests/run_all.py`) — так же сюда подставляются контрольные
мутации: копия раннера с одним намеренно возвращённым дефектом обязана
уронить этот набор.

Запуск из корня репозитория:  python tests/test_runner.py
"""
import contextlib
import importlib.util
import io
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "runner_fixtures"
RUNNER_PATH = Path(os.environ.get("OBOROT_RUNNER") or (ROOT / "tests" / "run_all.py"))

SUITE_FILES = {
    "pass": "suite_pass.py",
    "pass_lower": "suite_pass_lower.py",
    "fail": "suite_fail.py",
    "silent": "suite_silent.py",
    "zero": "suite_zero.py",
    "skip": "suite_skip.py",
    "skip_no_marker": "suite_skip_no_marker.py",
    "skip_no_rc": "suite_skip_no_rc.py",
    "skip_empty_reason": "suite_skip_empty_reason.py",
    "skip_with_failures": "suite_skip_with_failures.py",
    "counted_only": "suite_counted_only.py",
    "timeout": "suite_timeout.py",
}

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
        print(f"  OK   {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL {name}  {detail}")


def load_runner():
    spec = importlib.util.spec_from_file_location("oborot_runner_under_test", RUNNER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def classify(runner, out: str, rc: int) -> dict:
    """Приговор раннера по выводу и коду возврата.

    Раннер обязан уметь выносить его отдельной функцией: приговор — это
    контракт, а не побочный эффект печати таблицы.
    """
    fn = getattr(runner, "classify", None)
    if fn is None:
        raise AttributeError("у раннера нет функции classify(out, rc)")
    return fn(out, rc)


def run_fixture(runner, key: str, timeout: int = 60) -> dict:
    """Прогон фиктивного набора настоящей функцией раннера, без подмен."""
    old_tests = runner.TESTS
    runner.TESTS = FIXTURES
    try:
        return runner.run_one(0, key, SUITE_FILES[key], False, False, timeout)
    finally:
        runner.TESTS = old_tests


@contextlib.contextmanager
def sandbox(runner, names):
    """Раннер смотрит в каталог фикстур и в пустой временный корень.

    Корень подменяется не для красоты: `main()` чистит `test_*.db` в корне
    репозитория, а этот набор гоняется вместе с остальными и не имеет права
    трогать их базы.
    """
    old = (runner.SUITES, runner.ROOT, runner.TESTS, sys.argv)
    with tempfile.TemporaryDirectory() as tmp:
        runner.SUITES = [(n, SUITE_FILES[n], False, False) for n in names]
        runner.ROOT = Path(tmp)
        runner.TESTS = FIXTURES
        try:
            yield
        finally:
            runner.SUITES, runner.ROOT, runner.TESTS, sys.argv = old


def run_main(runner, names, argv) -> tuple:
    """Возвращает (код возврата, напечатанное). SystemExit — тоже ответ."""
    buf = io.StringIO()
    with sandbox(runner, names):
        sys.argv = ["run_all.py", *argv]
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                rc = runner.main()
        except SystemExit as exc:  # argparse на незнакомом ключе
            rc = exc.code if isinstance(exc.code, int) else 2
    return rc, buf.getvalue()


def main() -> int:  # noqa: C901 — сценарный набор: проверок много, ветвлений мало
    runner = load_runner()
    print(f"раннер под проверкой: {RUNNER_PATH}")

    print("\n== приговор по одному набору: успех, провал, пропуск ==")
    cases = [
        ("зелёный набор с числовым отчётом — PASS", "pass", "PASS"),
        ("«Итого» в другом регистре — тоже PASS", "pass_lower", "PASS"),
        ("отчёт с падениями — FAIL", "fail", "FAIL"),
        ("тишина при коде 0 — не успех", "silent", "NO_REPORT"),
        ("0 OK, 0 FAIL — не успех", "zero", "FAIL"),
        ("код 77 и причина вместе — SKIP", "skip", "SKIP"),
        ("код 77 без причины — не пропуск", "skip_no_marker", "FAIL"),
        ("причина без кода 77 — не пропуск", "skip_no_rc", "FAIL"),
        ("маркер с пустой причиной — не пропуск", "skip_empty_reason", "FAIL"),
        ("пропуск поверх падений — падение важнее", "skip_with_failures", "FAIL"),
        ("строки проверок без отчёта — не успех", "counted_only", "NO_REPORT"),
    ]
    seen = {}
    for title, key, want in cases:
        try:
            r = run_fixture(runner, key)
            seen[key] = r
            check(title, r.get("verdict") == want,
                  f"ожидали {want}, получили {r.get('verdict')!r}; "
                  f"ok={r.get('ok')} fail={r.get('fail')} rc={r.get('rc')}")
        except Exception as exc:  # noqa: BLE001 — красный отчёт важнее падения
            check(title, False, f"ожидали {want}, раннер не смог: {exc}")

    print("\n== таймаут — это провал, а не медленный успех ==")
    try:
        r = run_fixture(runner, "timeout", timeout=3)
        check("набор не завершился — TIMEOUT", r.get("verdict") == "TIMEOUT",
              f"получили {r.get('verdict')!r}, rc={r.get('rc')}")
        check("частичный вывод таймаута не выдаётся за отчёт", r.get("ok") == 0,
              f"ok={r.get('ok')}")
    except Exception as exc:  # noqa: BLE001
        check("набор не завершился — TIMEOUT", False, f"раннер не смог: {exc}")
        check("частичный вывод таймаута не выдаётся за отчёт", False, str(exc))

    print("\n== причина пропуска доезжает до отчёта ==")
    r = seen.get("skip") or {}
    check("причина пропуска сохранена дословно",
          r.get("reason") == "в системе нет клиента sqlite3 — проверять нечего",
          f"reason={r.get('reason')!r}")

    print("\n== подсчёт строк — диагностика, а не приговор ==")
    r = seen.get("counted_only") or {}
    check("подсчёт строк не попадает в ok", r.get("ok") == 0, f"ok={r.get('ok')}")
    check("подсчёт строк виден как диагностика", r.get("counted_ok") == 2,
          f"counted_ok={r.get('counted_ok')!r}")

    print("\n== та же таблица истинности отдельной функцией classify ==")
    truth = [
        ("ИТОГО: 3 OK, 0 FAIL", 0, "PASS"),
        ("Итого: 4 OK, 0 FAIL", 0, "PASS"),
        ("ИТОГО: 1 OK, 2 FAIL", 1, "FAIL"),
        ("ИТОГО: 0 OK, 0 FAIL", 0, "FAIL"),
        ("", 0, "NO_REPORT"),
        ("ПРОПУЩЕНО: нет sqlite3", 77, "SKIP"),
        ("ПРОПУЩЕНО: нет sqlite3", 0, "FAIL"),
        ("нет маркера", 77, "FAIL"),
        ("ИТОГО: 5 OK, 0 FAIL", 1, "FAIL"),
        ("ИТОГО: 5 OK, 0 FAIL", 124, "TIMEOUT"),
    ]
    for out, rc, want in truth:
        title = f"classify({out[:24]!r}, rc={rc}) = {want}"
        try:
            got = classify(runner, out, rc).get("verdict")
            check(title, got == want, f"получили {got!r}")
        except Exception as exc:  # noqa: BLE001
            check(title, False, str(exc))

    print("\n== код возврата всего прогона ==")
    rc, out = run_main(runner, ["pass"], [])
    check("все наборы зелёные — 0", rc == 0, f"код {rc}")
    rc, out = run_main(runner, ["pass", "silent"], [])
    check("молчащий набор роняет прогон", rc == 1, f"код {rc}")
    check("молчащий набор назван в таблице", "silent" in out, out[-400:])
    rc, out = run_main(runner, ["pass", "zero"], [])
    check("набор 0/0 роняет прогон", rc == 1, f"код {rc}")
    rc, out = run_main(runner, ["pass", "counted_only"], [])
    check("набор без отчёта роняет прогон", rc == 1, f"код {rc}")
    rc, out = run_main(runner, ["pass", "timeout"], ["--timeout", "3"])
    check("таймаут роняет прогон", rc == 1, f"код {rc}")
    rc, out = run_main(runner, ["pass", "fail"], [])
    check("падение роняет прогон", rc == 1, f"код {rc}")

    print("\n== локально явный пропуск виден и не красит прогон в красный ==")
    rc, out = run_main(runner, ["pass", "skip"], [])
    check("явный пропуск: код возврата 0", rc == 0, f"код {rc}")
    check("явный пропуск: в таблице стоит SKIP", "SKIP" in out.upper(), out[-400:])
    check("явный пропуск: причина названа", "sqlite3" in out, out[-400:])
    rc, out = run_main(runner, ["pass", "skip_no_rc"], [])
    check("небрежный пропуск роняет прогон и локально", rc == 1, f"код {rc}")

    print("\n== --require-all: пропуск в CI не прощается ==")
    rc, out = run_main(runner, ["pass", "skip"], ["--require-all"])
    check("--require-all роняет прогон на пропуске", rc == 1, f"код {rc}")
    rc, out = run_main(runner, ["pass"], ["--require-all"])
    check("--require-all на полностью зелёном прогоне — 0", rc == 0, f"код {rc}")
    rc, out = run_main(runner, ["pass", "skip"],
                       ["--require-all", "--allow-skip", "skip"])
    check("--allow-skip как механизм: названный набор прощён", rc == 0, f"код {rc}")
    rc, out = run_main(runner, ["pass", "skip"],
                       ["--require-all", "--allow-skip", "pass"])
    check("--allow-skip не прощает набор, который не назван", rc == 1, f"код {rc}")
    rc, out = run_main(runner, ["pass", "skip"], ["--require-all", "pass"])
    check("--require-all на части наборов — отказ, а не успех", rc == 2, f"код {rc}")

    print(f"\nИТОГО: {len(PASS)} OK, {len(FAIL)} FAIL")
    if FAIL:
        print("Провалились:")
        for name in FAIL:
            print(f"  - {name}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
