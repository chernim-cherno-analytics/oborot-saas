# -*- coding: utf-8 -*-
"""Реестр шардов набора `sync` и сторож полноты — «ничего не потерялось».

ЗАЧЕМ. `tests/test_sync.py` — один сценарий на 413 проверок, который в CI шёл
448.9 с (прогон 32953333557) и в одиночку определял критический путь всего
строгого набора: остальные 27 наборов вместе весят столько же, но идут по трём
дорожкам. Разрезать сценарий на независимые процессы можно; молча потерять при
этом сценарий, ожидание или отрицательный контроль — нельзя. Этот файл и есть
замок на «нельзя».

КАК УСТРОЕНО.
  * `ACTS` — реестр АКТОВ в исходном порядке сценария. Акт — существующий
    кусок `run_scenario()`, обёрнутый в `if begin("<акт>"):` без единой правки
    внутри (доказывается `git diff -w`).
  * `NEEDS` — что акту нужно от соседей. Пролог считается автоматически:
    шард сам доигрывает недостающие акты МОЛЧА (те же запросы, те же реальные
    ожидания), но их проверки не засчитывает — иначе одна и та же проверка
    попала бы в отчёт дважды и «413» перестало бы что-либо значить.
  * `SHARDS` — какой шард какие акты ЗАПИСЫВАЕТ. Каждый акт записан ровно в
    одном шарде, объединение шардов = весь реестр. Сумма OK по шардам обязана
    дать ровно `LEGACY_TOTAL`.
  * `sync_baseline_checks.txt` — замороженный эталон: 413 имён проверок в
    исходном порядке, разложенных по актам. Снят с exact BASE
    31771a62c5dcde0d2250090697b4331dced02f69.

FAIL-CLOSED. Пропала проверка, переехала в другой акт, поменялся порядок,
акт выпал из шардов или из реестра, шард не зарегистрирован в раннере —
набор красный. Ослабить это можно только осознанно, правя эталонный файл
руками, и тогда правка видна в diff.

Запуск как набора (сторож, доли секунды):  python tests/test_sync_shards.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
BASELINE = TESTS / "sync_baseline_checks.txt"
SCENARIO = TESTS / "test_sync.py"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"

# Столько проверок сценарий выполнял на BASE и обязан выполнять после разреза.
# Число зафиксировано по CI 32953333557 (sync 413 OK / 0 FAIL / 448.9 сек).
# +1 (DATA-6 round 4, discussion_r3868006778): миграция production_orders
# получила отдельную ALTER TABLE-проверку в акте «tail» (ms_reconcile_state/
# ms_reconcile_href), см. tests/test_sync.py и tests/sync_baseline_checks.txt.
LEGACY_TOTAL = 414

# Акты в порядке исполнения сценария. `core` — онбординг, первичный синк,
# аналитика, инкремент и переименование (исполняется всегда, он же пролог всех
# остальных). `tail` — терминальный хвост: новые организации, демо, выгрузки,
# условия производства и прочее.
ACTS = (
    "core",
    "a", "p2ref", "p2back", "p3", "p7", "p11", "p8", "p1", "p5p6",
    "b", "c", "c1", "c2", "c3", "c4", "c6c7", "c8", "c5", "c9", "d", "e",
    "tail",
)

# Что акт берёт у соседей. Пусто — хватает `core`.
#   p2ref  даёт ref_stock/ref_sales — эталонный прямой проход истории;
#   p3     даёт good_fp — отпечаток прерванной загрузки;
#   c6c7   даёт wh_lab — сервисный склад, который c8 дёргает во время синка.
NEEDS = {
    "p2back": ("p2ref",),
    "p3": ("p2ref",),
    "p7": ("p2ref",),
    "p11": ("p2ref",),
    "p8": ("p2ref", "p3"),
    "p1": ("p2ref",),
    "c2": ("p2ref",),
    "c3": ("p2ref",),
    "c9": ("p2ref",),
    "c8": ("c6c7",),
}

# Разбиение по независимым процессам. Балансировка — по измеренному профилю
# BASE (локально 438.7 с на весь сценарий): a=73.6, p2ref=7.2, p2back=19.4,
# p3=11.3, p7=21.5, p11=11.3, p8=1.1, p1=11.0, p5p6=0.6, b=1.0, c=14.3,
# c1=1.1, c2=7.3, c3=24.5, c4=39.0, c6c7=49.3, c8=10.3, c5=24.6, c9=22.5,
# d=1.1, e=12.3, core=16.7, tail=57.5.
SHARDS = {
    "sync": ("core", "tail"),
    "sync_p1": ("a", "p2ref", "p2back", "p3", "p8", "b", "d"),
    "sync_resume": ("p7", "p11", "p1", "p5p6", "c", "c1", "c2", "c3", "e"),
    "sync_rebuild": ("c4", "c6c7", "c8", "c5", "c9"),
}

ALL = "all"  # `python tests/test_sync.py` без аргументов — весь сценарий

_ACT_INDEX = {a: i for i, a in enumerate(ACTS)}


def plan_for(shard: str) -> dict:
    """{акт: записывать ли проверки} в порядке реестра.

    True — акт исполняется и засчитывается. False — акт исполняется как
    подготовка (пролог) и НЕ засчитывается. Отсутствие ключа — акт пропущен.
    """
    if shard == ALL:
        return {a: True for a in ACTS}
    if shard not in SHARDS:
        raise SystemExit(f"неизвестный шард {shard!r}; известны: "
                         f"{ALL}, {', '.join(sorted(SHARDS))}")
    recorded = set(SHARDS[shard])
    needed = set(recorded) | {"core"}
    stack = list(needed)
    while stack:
        for dep in NEEDS.get(stack.pop(), ()):
            if dep not in needed:
                needed.add(dep)
                stack.append(dep)
    return {a: (a in recorded) for a in ACTS if a in needed}


def load_baseline() -> dict:
    """{акт: [имена проверок в исходном порядке]} из замороженного эталона."""
    if not BASELINE.is_file():
        raise SystemExit(f"нет эталона {BASELINE} — сверять полноту нечем")
    out, cur = {}, None
    for raw in BASELINE.read_text(encoding="utf-8").splitlines():
        if raw.startswith("# акт: "):
            cur = raw[len("# акт: "):].strip()
            if cur in out:
                raise SystemExit(f"эталон: акт {cur!r} объявлен дважды")
            out[cur] = []
            continue
        if not raw.strip() or raw.startswith("#"):
            continue
        if cur is None:
            raise SystemExit("эталон: имя проверки до первого «# акт:»")
        out[cur].append(raw)
    return out


# ── Сторож ───────────────────────────────────────────────────────────────────

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        PASS.append(name)
        print(f"  OK   {name}" + (f"  [{detail}]" if detail else ""))
    else:
        FAIL.append(name)
        print(f"  FAIL {name}  {detail}")


def main() -> int:
    print("== Реестр актов и шардов ==")
    check("акты реестра не повторяются", len(set(ACTS)) == len(ACTS),
          f"актов={len(ACTS)} уникальных={len(set(ACTS))}")

    covered = [a for s in SHARDS.values() for a in s]
    check("каждый акт записан ровно в одном шарде",
          sorted(covered) == sorted(set(covered)) == sorted(ACTS),
          f"дубли={sorted({a for a in covered if covered.count(a) > 1})} "
          f"без шарда={sorted(set(ACTS) - set(covered))} "
          f"лишние={sorted(set(covered) - set(ACTS))}")

    bad_order = [s for s, acts in SHARDS.items()
                 if list(acts) != sorted(acts, key=_ACT_INDEX.__getitem__)]
    check("акты внутри шарда перечислены в порядке сценария", not bad_order,
          f"нарушено в {bad_order}")

    unknown_dep = {a: d for a, deps in NEEDS.items() for d in deps
                   if d not in _ACT_INDEX or _ACT_INDEX[d] >= _ACT_INDEX.get(a, -1)}
    check("зависимости акта указывают только назад по сценарию", not unknown_dep,
          f"плохие={unknown_dep}")

    plans = {s: plan_for(s) for s in SHARDS}
    missed = {s: sorted(set(SHARDS[s]) - set(p)) for s, p in plans.items()}
    check("план шарда содержит все его записываемые акты",
          not any(missed.values()), f"потеряно={missed}")
    no_core = [s for s, p in plans.items() if "core" not in p]
    check("в каждом шарде есть core (общая подготовка организации)", not no_core,
          f"без core={no_core}")

    print("\n== Замороженный эталон проверок ==")
    base = load_baseline()
    check("акты эталона совпадают с реестром и порядком",
          list(base) == list(ACTS),
          f"эталон={list(base)[:4]}… реестр={list(ACTS)[:4]}…")
    total = sum(len(v) for v in base.values())
    check(f"в эталоне ровно {LEGACY_TOTAL} проверок "
          f"(CI 32953333557: sync 413/0, +1 DATA-6 round 4)",
          total == LEGACY_TOTAL, f"в файле={total}")
    names = [n for v in base.values() for n in v]
    dups = sorted({n for n in names if names.count(n) > 1})
    check("имена проверок в эталоне уникальны — иначе сверка не докажет ничего",
          not dups, f"повторы={dups[:3]}")
    empty = [a for a, v in base.items() if not v]
    check("у каждого акта эталона есть хотя бы одна проверка", not empty,
          f"пустые={empty}")

    print("\n== Сценарий и раннер знают те же акты и шарды ==")
    src = SCENARIO.read_text(encoding="utf-8")
    gates = re.findall(r'begin\("([^"]+)"\)', src)
    check("в tests/test_sync.py каждый акт открыт ровно один раз",
          sorted(gates) == sorted(ACTS),
          f"в сценарии={sorted(set(gates) ^ set(ACTS))} повторы="
          f"{sorted({g for g in gates if gates.count(g) > 1})}")

    sys.path.insert(0, str(TESTS))
    import run_all  # noqa: E402  — реестр наборов раннера

    registered = {}
    for entry in run_all.SUITES:
        name, filename = entry[0], entry[1]
        extra = tuple(entry[4]) if len(entry) > 4 else ()
        if filename == "test_sync.py":
            registered[name] = extra
    check("в раннере зарегистрированы ровно шарды реестра",
          sorted(registered) == sorted(SHARDS),
          f"в раннере={sorted(registered)} в реестре={sorted(SHARDS)}")
    wrong = {n: a for n, a in registered.items() if a != ("--shard", n)}
    check("каждый шард запускается своим --shard", not wrong, f"плохие={wrong}")
    check("сторож полноты сам зарегистрирован в раннере",
          any(e[1] == "test_sync_shards.py" for e in run_all.SUITES),
          "иначе замок не выполняется в CI")

    print("\n== Контракт CI ==")
    wf = WORKFLOW.read_text(encoding="utf-8")
    jobs = re.findall(r"^  (\w[\w-]*):$", wf.split("\njobs:\n", 1)[-1], re.MULTILINE)
    check("в workflow ровно одна job и она называется tests", jobs == ["tests"],
          f"jobs={jobs}")
    head = wf.split("\njobs:\n", 1)[0]
    check("concurrency объявлена на уровне workflow, а не job",
          re.search(r"^concurrency:$", head, re.MULTILINE) is not None,
          "иначе очередь режется по job, а не по ветке")
    check("группа очереди — workflow + ref",
          "${{ github.workflow }}-${{ github.ref }}" in head, "")
    check("cancel-in-progress: true",
          re.search(r"^  cancel-in-progress: true$", head, re.MULTILINE) is not None,
          "иначе устаревшие прогоны продолжают занимать раннеры")
    # Только строки вызова, не комментарии: в шапке workflow слово
    # «--allow-skip» стоит как раз в объяснении, почему его там нет.
    # Запрет на сам ключ держит tests/test_runner.py; здесь — что разрез на
    # шарды не изменил строгий вызов раннера.
    run_lines = [ln for ln in wf.splitlines()
                 if "run_all.py" in ln and not ln.lstrip().startswith("#")]
    check("разрез не изменил строгий вызов: --jobs 3 --require-all",
          bool(run_lines) and all("--jobs 3 --require-all" in ln for ln in run_lines),
          f"строки={run_lines}")

    print()
    print(f"ИТОГО: {len(PASS)} OK, {len(FAIL)} FAIL")
    if FAIL:
        print("Провалены:", *FAIL, sep="\n  - ")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
