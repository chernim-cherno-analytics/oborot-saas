# -*- coding: utf-8 -*-
"""Контрольные мутации формул: проверка того, что проверка работает.

Зачем. `tests/test_formula_contract.py` характеризует текущее поведение формул,
поэтому на неизменённом коде он зелёный ПО ПОСТРОЕНИЮ. Такой набор легко
перепутать с работающим: тест, зелёный всегда, хуже отсутствия теста — он
выдаёт молчание за доказательство. Здесь в КОПИЮ настоящего продакшн-кода по
одной вносятся ровно те правки, ради которых замок писался, и каждая обязана
уронить контракт — и уронить ИМЕННО ту проверку, ради которой мутация внесена.

Заодно это второе (и более сильное) доказательство живого пути: если бы
контракт проверял свою копию формулы, правка `app/analytics.py` и
`app/order_planner.py` оставила бы его зелёным.

ИЗОЛЯЦИЯ. Копируется дерево (`app/` + сам контракт) во временный каталог,
мутируется файл В КОПИИ, набор запускается из копии. Настоящие
`app/analytics.py`, `app/analytics_extra.py`, `app/order_planner.py` не
трогаются ни на байт: этот скрипт вообще не открывает их на запись.
`__pycache__` в копию не переносится, а интерпретатор запускается с
`PYTHONDONTWRITEBYTECODE=1` — иначе чужой .pyc мог бы подменить мутацию.

ЧЕСТНОСТЬ МАТРИЦЫ. Провалом (а НЕ пропуском) считается каждый из случаев:
  * мутация не применилась — искомого текста в продакшн-файле нет;
  * применилась неоднозначно — совпадений больше одного, и какое из них
    заменилось, сказать нельзя;
  * контракт остался зелёным — дефект вернули, а замок промолчал;
  * контракт покраснел НЕ ПО ТОЙ причине — упало что-то другое, а проверка,
    ради которой мутация внесена, промолчала.
Молча пропущенная мутация — та же ложная зелень, ради борьбы с которой всё
это и написано (та же логика, что в `tests/check_runner_mutations.py`).

БАЗОВЫЙ ПРОГОН. Перед мутациями копия запускается НЕТРОНУТОЙ. Если она красная
сама по себе, то любая «краснота под мутацией» ничего не значит, и продолжать
нельзя — скрипт останавливается.

Восемь обязательных критериев (PROPOSAL/ACK по DATA-11, Issue #2). Критерий 4
(снятый пол) физически проверяется двумя подмутациями — отдельно у аналитики
и отдельно у мастера: одна правка не может снять пол в двух файлах сразу, а
доказать обязаны обе стороны.

Запуск из корня репозитория:  python tests/check_formula_mutations.py
В CI не гоняется: это проверка проверки, её место — рядом с правкой формул.
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUITE_REL = Path("tests") / "test_formula_contract.py"

ANALYTICS = "app/analytics.py"
EXTRA = "app/analytics_extra.py"
PLANNER = "app/order_planner.py"

# критерий, заголовок, файл, что ломаем, во что, чем обязан покраснеть
#
# «Чем обязан покраснеть» — кортеж подстрок: достаточно, чтобы хоть одна
# нашлась среди строк «  FAIL » в выводе. Второй элемент там, где мутация
# способна не вернуть неверное число, а бросить исключение: тогда падает
# блок целиком, и это ровно та же причина, названная иначе.
MUTATIONS: list[tuple[str, str, str, str, str, tuple[str, ...]]] = [
    (
        "1",
        "аналитика: порядок округления сведён к мастеру round(итог − остаток)",
        ANALYTICS,
        'item["need"] = max(0, round(rate * horizon) - round(proj_stock))',
        'item["need"] = max(0, round(rate * horizon - proj_stock))',
        ("потребность = round(темп×горизонт) − round(остаток) = 22 − 8 = 14",),
    ),
    (
        "2",
        "мастер: порядок округления сведён к аналитике (раздельный round)",
        PLANNER,
        "need = max(0, int(round(r_cover * cover - proj_stock)))",
        "need = max(0, int(round(r_cover * cover) - round(proj_stock)))",
        ("мастер: потребность = int(round(темп×покрытие − остаток)) = 15",),
    ),
    (
        "3",
        "мастер: потребность снова считается темпом до прихода (r_cover → r_lead)",
        PLANNER,
        "need = max(0, int(round(r_cover * cover - proj_stock)))",
        "need = max(0, int(round(r_lead * cover - proj_stock)))",
        ("потребность считается темпом ПОКРЫТИЯ: 38, а не 15",),
    ),
    (
        "4a",
        "аналитика: снят пол max(0, …) — потребность уходит в минус",
        ANALYTICS,
        'item["need"] = max(0, round(rate * horizon) - round(proj_stock))',
        'item["need"] = round(rate * horizon) - round(proj_stock)',
        ("затоварка: пол max(0, …) держит потребность на нуле",
         "нет продаж → потребность 0, а не отрицательная"),
    ),
    (
        "4b",
        "мастер: снят пол max(0, …) — потребность уходит в минус",
        PLANNER,
        "need = max(0, int(round(r_cover * cover - proj_stock)))",
        "need = int(round(r_cover * cover - proj_stock))",
        ("мастер: затоварка → потребность 0, а не −955",
         "мастер: нулевой темп → потребность 0, а не −10",
         "8. Мастер: ноль и пол"),
    ),
    (
        "5",
        "size_split: удалён fallback равного деления при нулевых весах",
        ANALYTICS,
        "    if sum(weights) <= 0:\n        weights = [1.0] * len(grid)\n",
        "",
        ("нулевые веса → деление поровну",
         "10. Контракт analytics.size_split"),
    ),
    (
        "6",
        "size_split: у тай-брейка отобран второй ключ (остаток, вес) → (остаток)",
        ANALYTICS,
        "range(len(grid)), key=lambda i: (exact[i] - alloc[i], weights[i]), reverse=True",
        "range(len(grid)), key=lambda i: exact[i] - alloc[i], reverse=True",
        ("тай-брейк по второму ключу (остаток, вес): {S:0, L:2}",),
    ),
    (
        "7",
        "_largest_remainder: нулевые веса снова делятся поровну вместо нулей",
        EXTRA,
        "    wsum = sum(max(0.0, w) for w in weights)\n"
        "    if wsum <= 0:\n"
        "        return [0] * n\n",
        "    wsum = sum(max(0.0, w) for w in weights)\n"
        "    if wsum <= 0:\n"
        "        weights = [1.0] * n\n"
        "        wsum = float(n)\n",
        ("нулевые веса → нули (а НЕ деление поровну)",),
    ),
    (
        "8",
        "size_split объявлен алиасом _largest_remainder (функции «свели»)",
        ANALYTICS,
        '    weights = [max(0.0, float(sizes[s].get("sold365") or 0)) for s in grid]\n'
        "    if sum(weights) <= 0:\n"
        "        weights = [1.0] * len(grid)\n"
        "    wsum = sum(weights)\n"
        "    exact = [total * w / wsum for w in weights]\n"
        "    alloc = [int(x) for x in exact]\n"
        "    remainders = sorted(\n"
        "        range(len(grid)), key=lambda i: (exact[i] - alloc[i], weights[i]), reverse=True\n"
        "    )\n"
        "    left = total - sum(alloc)\n"
        "    for i in range(left):\n"
        "        alloc[remainders[i % len(remainders)]] += 1\n"
        "    return {s: a for s, a in zip(grid, alloc)}\n",
        # Импорт локальный: analytics_extra импортирует analytics на уровне
        # модуля, и импорт наверху файла дал бы цикл вместо мутации.
        "    from app.analytics_extra import _largest_remainder\n"
        '    weights = [max(0.0, float(sizes[s].get("sold365") or 0)) for s in grid]\n'
        "    alloc = _largest_remainder(weights, total)\n"
        "    return {s: a for s, a in zip(grid, alloc)}\n",
        ("две реализации НЕ взаимозаменяемы ни на одном из двух входов",
         "нулевые веса → деление поровну"),
    ),
]


def _make_copy(dst: Path) -> None:
    """Изолированная копия: app/ целиком и сам контракт. Без __pycache__."""
    shutil.copytree(ROOT / "app", dst / "app",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (dst / "tests").mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / SUITE_REL, dst / SUITE_REL)


def _run(copy_root: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    # PYTHONPATH намеренно не наследуется: набор сам кладёт свой ROOT первым
    # в sys.path, и чужой путь к настоящему app/ здесь только помешал бы.
    env.pop("PYTHONPATH", None)
    return subprocess.run(
        [sys.executable, str(SUITE_REL)],
        cwd=str(copy_root), env=env, capture_output=True, text=True, timeout=900,
    )


def _fail_lines(out: str) -> list[str]:
    return [ln for ln in out.splitlines() if ln.startswith("  FAIL ")]


def main() -> int:
    ok, bad = 0, []
    with tempfile.TemporaryDirectory(prefix="oborot-formula-mut-") as tmp:
        copy_root = Path(tmp) / "repo"
        _make_copy(copy_root)

        # ── База: нетронутая копия обязана быть зелёной ──────────────────────
        # Без этого «красный под мутацией» не значит ничего: копия могла бы
        # падать сама по себе, и матрица 8/8 оказалась бы матрицей 8 совпадений.
        base = _run(copy_root)
        if base.returncode != 0:
            print("  FAIL базовый прогон: нетронутая копия НЕ зелёная — "
                  "мутации доказывать нечем")
            print(base.stdout[-4000:])
            print(base.stderr[-2000:])
            print("\nИТОГО: 0 OK, 1 FAIL")
            return 1
        print("  OK   базовый прогон: нетронутая копия зелёная")
        ok += 1

        for tag, title, rel, old, new, expect in MUTATIONS:
            target = copy_root / rel
            source = target.read_text(encoding="utf-8")
            name = f"[{tag}] {title}"

            hits = source.count(old)
            if hits != 1:
                bad.append(f"{name}: мутация применяется неоднозначно "
                           f"(совпадений {hits}) — текст {rel} изменился, "
                           f"проверьте её вручную")
                print(f"  FAIL {name}  [совпадений {hits}, а нужно ровно 1]")
                continue

            try:
                target.write_text(source.replace(old, new, 1), encoding="utf-8")
                p = _run(copy_root)
            finally:
                # Копия возвращается к исходному виду: мутации не копятся.
                target.write_text(source, encoding="utf-8")

            broke = _fail_lines(p.stdout)
            if p.returncode == 0:
                bad.append(f"{name}: дефект вернули, а контракт остался зелёным")
                print(f"  FAIL {name}  [контракт не заметил дефект]")
                continue

            matched = [e for e in expect if any(e in ln for ln in broke)]
            if not matched:
                first = broke[0].strip() if broke else "(без строки FAIL)"
                bad.append(f"{name}: контракт покраснел, но НЕ по ожидаемой "
                           f"причине; ждали {expect!r}, первый провал: {first}")
                print(f"  FAIL {name}  [красный не по той причине: {first[:70]}]")
                continue

            ok += 1
            print(f"  OK   {name}  [{len(broke)} провалов, ожидаемый: "
                  f"«{matched[0][:60]}»]")

    # В счёт идут базовый прогон и каждая мутация, доказавшая красноту
    # по ожидаемой причине: 1 + 9 = 10 при полной матрице.
    print(f"\nИТОГО: {ok} OK, {len(bad)} FAIL")
    for line in bad:
        print(f"  - {line}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
