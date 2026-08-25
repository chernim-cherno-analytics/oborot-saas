# -*- coding: utf-8 -*-
"""Контрольные мутации: проверка того, что проверка работает.

Зачем. `tests/test_runner.py` защищает контракт приговора — но кто проверил,
что он вообще способен что-то поймать? Тест, который зелен всегда, хуже
отсутствия теста: он выдаёт молчание за доказательство. Здесь в копию
раннера по одному возвращаются РОВНО ТЕ дефекты, ради которых контракт
писался, и каждый обязан уронить `test_runner.py`.

Копия — во временном каталоге; настоящий `tests/run_all.py` не трогается.
Раннер под проверкой подставляется набору через `OBOROT_RUNNER`.

Если мутация перестала применяться (текст в раннере переписан), это тоже
провал, а не пропуск: молча пропущенная мутация — та же ложная зелень.

Запуск из корня репозитория:  python tests/check_runner_mutations.py
В CI не гоняется: это проверка проверки, её место — рядом с правкой раннера.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "tests" / "run_all.py"
SUITE = ROOT / "tests" / "test_runner.py"

# имя, что ломаем, из чего, во что
MUTATIONS = [
    (
        "0 OK / 0 FAIL снова считается успехом",
        "elif rc != 0 or ok == 0:",
        "elif rc != 0:",
    ),
    (
        "тишина снова считается успехом",
        '    elif not has_report:\n        res["verdict"] = NO_REPORT',
        '    elif not has_report:\n        res["verdict"] = PASS',
    ),
    (
        "для пропуска снова достаточно кода 77 без причины",
        'res["verdict"] = SKIP if (rc == SKIP_RC and reason) else FAIL',
        'res["verdict"] = SKIP if rc == SKIP_RC else FAIL',
    ),
    (
        "для пропуска снова достаточно маркера без кода 77",
        'res["verdict"] = SKIP if (rc == SKIP_RC and reason) else FAIL',
        'res["verdict"] = SKIP if reason else FAIL',
    ),
    (
        "регистр слова «Итого» снова решает, засчитана ли работа",
        'r"ИТОГО:\\s*(\\d+)\\s*OK,\\s*(\\d+)\\s*FAIL", re.IGNORECASE',
        'r"ИТОГО:\\s*(\\d+)\\s*OK,\\s*(\\d+)\\s*FAIL"',
    ),
    (
        "подсчёт строк снова выносит приговор вместо отчёта",
        '    ok, fail = (int(matches[-1][0]), int(matches[-1][1])) if has_report else (0, 0)',
        '    ok, fail = ((int(matches[-1][0]), int(matches[-1][1])) if has_report else\n'
        '                (len(OK_LINE_RE.findall(out)), len(FAIL_LINE_RE.findall(out))))\n'
        '    has_report = has_report or bool(ok or fail)',
    ),
    (
        "приговор снова выносится по первому отчёту, а не по последнему",
        "ok, fail = (int(matches[-1][0]), int(matches[-1][1])) if has_report else (0, 0)",
        "ok, fail = (int(matches[0][0]), int(matches[0][1])) if has_report else (0, 0)",
    ),
    (
        "маркер снова ловится с любым отступом, включая вложенные скрипты",
        'SKIP_RE = re.compile(r"^ПРОПУЩЕНО:',
        'SKIP_RE = re.compile(r"^\\s*ПРОПУЩЕНО:',
    ),
    (
        "таймаут снова считается успехом",
        '    if rc == TIMEOUT_RC:\n        res["verdict"] = TIMEOUT',
        '    if rc == TIMEOUT_RC:\n        res["verdict"] = PASS',
    ),
    (
        "--require-all снова прощает пропуск",
        '        return (not require_all) or (r["name"] in allow_skip)',
        '        return True',
    ),
    (
        "--require-all снова соглашается на часть наборов",
        '    if args.require_all and args.only and set(args.only) != known:',
        '    if False:',
    ),
]


def main() -> int:
    source = RUNNER.read_text(encoding="utf-8")
    ok, bad = 0, []
    with tempfile.TemporaryDirectory() as tmp:
        for title, old, new in MUTATIONS:
            if old not in source:
                bad.append(f"{title}: мутация не применяется — текст раннера "
                           f"изменился, проверьте её вручную")
                print(f"  FAIL {title}  [мутация не применилась]")
                continue
            mutant = Path(tmp) / "run_all_mutant.py"
            mutant.write_text(source.replace(old, new, 1), encoding="utf-8")
            env = dict(os.environ, OBOROT_RUNNER=str(mutant))
            p = subprocess.run([sys.executable, str(SUITE)], cwd=str(ROOT),
                               env=env, capture_output=True, text=True, timeout=600)
            broke = [ln for ln in p.stdout.splitlines() if ln.startswith("  FAIL ")]
            if p.returncode == 0:
                bad.append(f"{title}: дефект вернули, а набор остался зелёным")
                print(f"  FAIL {title}  [набор не заметил дефект]")
            else:
                ok += 1
                first = broke[0].strip() if broke else "(без строки FAIL)"
                print(f"  OK   {title}  [{len(broke)} провалов, первый: {first[:70]}]")

    print(f"\nИТОГО: {ok} OK, {len(bad)} FAIL")
    for line in bad:
        print(f"  - {line}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
