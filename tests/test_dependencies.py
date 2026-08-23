# -*- coding: utf-8 -*-
"""Зависимости: lock должен быть правдой, а не файлом с правильным названием.

Зачем это набор. requirements.lock существует ради одного свойства — чтобы в
CI, на сервере и у разработчика стоял ОДИН И ТОТ ЖЕ набор версий. Свойство это
проверяемое, и проверять его надо, потому что ломается оно тихо: файл не
обновили, строку потеряли, requirements.txt называет связку, которой нет ни в
lock, ни на проде. Ничего не падает — просто «зелёный CI» и «то, что поехало в
бой» перестают быть одним и тем же.

Идея завести это отдельным набором — из ветки Codex codex/p20-integration-publish.
У меня та же проверка жила скриптом внутри .github/workflows/ci.yml, то есть
локально не запускалась и до пуша не падала. Реализация здесь своя.

Сверх источника проверяются две вещи:
  - дубли в lock. Разбор в словарь молча оставляет последнюю строку из двух,
    и противоречивый lock выглядит исправным;
  - согласие requirements.txt и requirements.lock ПО ГРАНИЦАМ. Именно этой
    проверки не хватало 23.08, когда список прямых зависимостей называл
    starlette 1.6.0 и pydantic 2.13.4 — версии, которых не было ни в lock, ни
    на сервере. Проверка «имя есть в lock» такую ложь пропускает.

Строгость: локально расхождение окружения с lock — предупреждение (у
разработчика может стоять другое), в CI (CI=true) и при OBOROT_ENFORCE_LOCK=1
— падение.

Запуск из корня репозитория:  python tests/test_dependencies.py
"""
import os
import re
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "requirements.lock"
DIRECT = ROOT / "requirements.txt"

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        PASS.append(name)
        print(f"  OK   {name}" + (f"  [{detail}]" if detail else ""))
    else:
        FAIL.append(name)
        print(f"  FAIL {name}  {detail}")


def norm(name: str) -> str:
    """PEP 503: Flask_SQLAlchemy, flask-sqlalchemy и Flask.SQLAlchemy — одно."""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def strip_comment(line: str) -> str:
    return line.split("#", 1)[0].strip()


def vkey(v: str) -> tuple:
    """Ключ сравнения версий: числовые части релиза, затем метка пред-релиза.

    Достаточно для того, что реально встречается в наших файлах: 0.141.1,
    2.0.52, 46.0.7. Пред-релиз (rc, b, a) считается МЛАДШЕ финальной версии —
    это единственная тонкость PEP 440, из-за которой наивное сравнение строк
    ошибается.
    """
    head = re.split(r"[^0-9.]", v, maxsplit=1)[0].rstrip(".")
    nums = tuple(int(x) for x in head.split(".") if x != "")
    pre = 0 if head == v else -1
    return (nums, pre)


def satisfies(version: str, op: str, want: str) -> bool:
    a, b = vkey(version), vkey(want)
    if op == "==":
        return version == want or a == b
    if op == "!=":
        return not (version == want or a == b)
    if op == ">=":
        return a >= b
    if op == "<=":
        return a <= b
    if op == ">":
        return a > b
    if op == "<":
        return a < b
    if op == "~=":
        # ~=1.2.3 значит «>=1.2.3 и совпадает по всем частям, кроме последней»
        head = len(b[0]) - 1
        return a >= b and a[0][:head] == b[0][:head]
    return True


def read_lock() -> tuple[dict[str, str], list[str], list[str]]:
    """Возвращает (пины, строки-не-пины, имена-дубли)."""
    pins: dict[str, str] = {}
    bad: list[str] = []
    dups: list[str] = []
    for raw in LOCK.read_text(encoding="utf-8").splitlines():
        line = strip_comment(raw)
        if not line:
            continue
        m = re.fullmatch(r"([A-Za-z0-9_.\-]+)\s*==\s*([^\s;]+)", line)
        if not m:
            bad.append(line)
            continue
        key = norm(m.group(1))
        if key in pins and pins[key] != m.group(2):
            dups.append(f"{key}: {pins[key]} и {m.group(2)}")
        elif key in pins:
            dups.append(f"{key}: строка повторяется")
        pins[key] = m.group(2)
    return pins, bad, dups


def read_direct() -> list[str]:
    out = []
    for raw in DIRECT.read_text(encoding="utf-8").splitlines():
        line = strip_comment(raw)
        if line:
            out.append(line)
    return out


def main() -> int:
    print("\n== 1. Файл на месте и разбирается ==")
    check("requirements.lock существует и не пуст",
          LOCK.is_file() and LOCK.stat().st_size > 0)
    if not LOCK.is_file():
        print("\nИТОГО: без lock-файла остальное проверять нечего.")
        return 1

    pins, bad, dups = read_lock()
    check("каждая строка lock — точный пин name==version", not bad, str(bad[:5]))
    check("в lock нет повторов и противоречий", not dups, str(dups[:5]))
    check("в lock есть версии", len(pins) > 0, f"пинов: {len(pins)}")

    print("\n== 2. Lock — полное замыкание, а не копия списка прямых ==")
    direct_lines = read_direct()
    direct_names = {norm(re.split(r"[<>=!~\s\[;]", ln, maxsplit=1)[0]) for ln in direct_lines}
    check("прямых зависимостей найдено", len(direct_names) > 0, f"{len(direct_names)}")
    # Транзитивные зависимости обязаны быть в lock: иначе pip доберёт их сам, и
    # «фиксация версий» окажется фиксацией только верхнего слоя.
    check("lock шире списка прямых зависимостей",
          len(pins) > len(direct_names), f"lock={len(pins)} прямых={len(direct_names)}")
    missing = sorted(direct_names - set(pins))
    check("каждая прямая зависимость есть в lock", not missing, str(missing))

    print("\n== 3. Сравнение версий (свой компаратор — проверяем его самого) ==")
    # Компаратор написан здесь же вручную, значит его самого никто не проверяет.
    # Эти случаи и есть его проверка; главный из них — предпоследний: пред-релиз
    # МЛАДШЕ финальной версии, и наивное сравнение строк на нём ошибается.
    cases = [
        ("0.141.1", ">=", "0.110", True), ("0.141.1", "<", "1", True),
        ("2.0.52", ">=", "2.0", True), ("2.0.52", "<", "3", True),
        ("46.0.7", ">=", "42", True), ("46.0.7", "<", "52", True),
        ("5.0.0", ">=", "6", False), ("0.0.8", ">=", "0.0.9", False),
        ("3.1.5", "~=", "3.1", True), ("3.0", "~=", "3.1", False),
        ("1.0.0rc1", "<", "1.0.0", True), ("1.0.0", "<", "1.0.0rc1", False),
    ]
    wrong = [f"{v}{op}{w}" for v, op, w, want in cases if satisfies(v, op, w) != want]
    check("компаратор версий отвечает верно на все известные случаи",
          not wrong, str(wrong))

    print("\n== 4. requirements.txt и requirements.lock не спорят ==")
    # Эта проверка ловит то, что «имя есть в lock» пропускает: границу, которой
    # зафиксированная версия не удовлетворяет.
    #
    # Сравнение версий сделано вручную, без `packaging`. Первый вариант его
    # импортировал — и CI упал: `packaging` вендорится внутрь pip и в чистом
    # окружении из одного requirements.lock не импортируется. Набор про
    # «зависимости должны быть объявлены» сам зависел от необъявленной
    # зависимости. Добавлять её в lock ради теста — хвост виляет собакой.
    conflicts, unparsed = [], []
    for line in direct_lines:
        m = re.fullmatch(r"([A-Za-z0-9_.\-]+)\s*((?:[<>=!~]=?[^,\s]+\s*,?\s*)*)", line)
        if not m:
            unparsed.append(line)
            continue
        locked = pins.get(norm(m.group(1)))
        if locked is None:
            continue
        for op, want in re.findall(r"([<>=!~]=?)\s*([^,\s]+)", m.group(2)):
            if not satisfies(locked, op, want):
                conflicts.append(f"{m.group(1)}: требование «{op}{want}», в lock {locked}")
    check("все строки requirements.txt разобраны", not unparsed, str(unparsed[:3]))
    check("зафиксированная версия удовлетворяет требованию из requirements.txt",
          not conflicts, "; ".join(conflicts[:5]))

    print("\n== 5. Установленное окружение ==")
    pip = subprocess.run([sys.executable, "-m", "pip", "check"],
                         capture_output=True, text=True)
    check("pip check не находит несовместимостей", pip.returncode == 0,
          (pip.stdout + pip.stderr).strip()[-300:])

    diffs = []
    for name, want in sorted(pins.items()):
        try:
            have = version(name)
        except PackageNotFoundError:
            diffs.append(f"{name}: в lock {want}, не установлен")
            continue
        if have != want:
            diffs.append(f"{name}: в lock {want}, установлено {have}")

    strict = os.environ.get("CI", "").lower() == "true" \
        or os.environ.get("OBOROT_ENFORCE_LOCK") == "1"
    if strict:
        # Lock, которому окружение не соответствует, хуже отсутствия lock:
        # он создаёт уверенность, ничего не гарантируя.
        check("окружение в точности совпадает с lock", not diffs,
              "; ".join(diffs[:8]))
    else:
        check("окружение сверено с lock (строго — только в CI)", True,
              "совпадает" if not diffs else f"локальных отклонений: {len(diffs)}")
        if diffs:
            for d in diffs[:8]:
                print(f"       {d}")

    print("\n" + "=" * 62)
    print(f"OK: {len(PASS)}   FAIL: {len(FAIL)}")
    for f in FAIL:
        print(f"  FAIL {f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
