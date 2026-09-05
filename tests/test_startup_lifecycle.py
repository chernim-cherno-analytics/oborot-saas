# -*- coding: utf-8 -*-
"""Порядок жизненного цикла приложения: планировщик стартует ПОСЛЕ миграций.

TECH_DEBT OPS-6: «Планировщик стартует до миграций». `scheduler.attach(app)`
вызывался в main.py раньше регистрации `_startup` (init_db + остальные шаги
`ensure_schema`/`reset_stale_running`/`log_preview`), а FastAPI
выполняет `on_event("startup")`-хендлеры строго в порядке регистрации (см.
`fastapi.routing.APIRouter._startup`, `for handler in self.on_startup`).
Значит планировщик реально поднимался ДО того, как гарантированно применены
все аддитивные миграции — молчаливая гонка на холодном старте.

Этим же набором закрыта последняя migration-on-import: `lessons.ensure_schema()`
вызывался прямо на импорте `app/api.py`, до старта приложения и вне защиты от
гонки нескольких воркеров, — тем же классом дефекта, что и уже закрытые
`ms_writeback`/`ms_vendor` (см. Д4 в `app/main.py`). Теперь это девятый шаг
`_startup()`, сразу после `init_db()`.

Каждая проверка запускает НАСТОЯЩИЙ ASGI-цикл через `fastapi.testclient.TestClient`
(он сам дёргает startup/shutdown) в отдельном процессе — подмена функций
модулей обязана случиться ДО `import app.main`, потому что
`scheduler.attach()` и `from app.db import init_db` разрешают имена в момент
импорта `app.main`, а не при вызове. Патч `lessons.ensure_schema` ставится ещё
раньше — до `import app.main`, — потому что до фикса именно импорт `app.api`
(которого требует `app.main`) вызывал миграцию; если бы патч ставился позже,
проверка ловила бы ложный успех: старый вызов уже случился бы мимо счётчика.

  1) реальный порядок: все одиннадцать шагов старта (init_db,
     lessons.ensure_schema, exclusions.ensure_schema, ms_sync.ensure_schema,
     ms_sync.reset_stale_running, ms_writeback.ensure_schema,
     ms_vendor.ensure_schema, subscription.ensure_schema,
     subscription.log_preview, models.ensure_supply_schema) завершаются ДО
     scheduler.start, каждый — ровно один раз, а lessons.ensure_schema —
     немедленно после init_db;
  2) инъекция сбоя в КАЖДЫЙ из десяти шагов старта — планировщик не
     стартует вообще, и ни один из последующих по порядку шагов не выполняется;
  3) import app.api и import app.main САМИ ПО СЕБЕ (без запуска ASGI-цикла)
     не вызывают lessons.ensure_schema — миграция не должна случаться на
     импорте модуля;
  4) повторный shutdown безопасен (идемпотентен), в т.ч. после ASGI-цикла.

TECH_DEBT OPS-5 («Миграции без журнала и порядка»): те же шаги теперь
после успешного завершения пишут строку в журнал `migration_ledger`
(`app/db.record_migration_step`) — стабильный id, числовая позиция, applied_at.
Журнал НИЧЕГО не пропускает: шаги идемпотентны и выполняются на каждом старте
как раньше, а таблица служит свидетельством и замком на порядок. Отсюда ещё
пять проверок, и каждая работает на СИНТЕТИЧЕСКОЙ базе (пустой файл или
руками собранная прежняя схема), без боевых данных:

  5) чистая база: журнал содержит ровно одиннадцать объявленных шагов, позиции
     1..10 идут по возрастанию и совпадают с фактическим порядком вызовов;
  6) прежняя схема: (а) база старой формы, где новых колонок ещё нет, и
     (б) уже мигрированная база, где журнала ещё нет вовсе, — приложение
     поднимается, миграции доезжают, журнал заполняется целиком;
  7) повторный старт идемпотентен: строк по-прежнему одиннадцать, applied_at
     первой записи НЕ переписан, и при этом все одиннадцать шагов выполнились
     снова — журнал не служит основанием их пропустить;
  8) сбой шага: упавший шаг и все последующие записи в журнал не получают;
  9) конфликт id↔позиция и позиция↔id валит старт (fail closed) ДО того, как
     защищаемый шаг успел что-либо сделать: таблиц приложения не появилось,
     `orgs` нет, ни один шаг не выполнился, планировщик не стартует, и «уже
     применено» вместо конфликта не выдаётся. Отдельно — часовой: на место
     шага позиции 6 подставлена функция, создающая таблицу-метку, и её
     отсутствие доказывает, что шага НЕ БЫЛО (а не только что журнал цел).
     Плюс ограниченная проверка гонки: четыре потока пишут один и тот же
     шаг — ровно одна строка, ровно один True, ни одного исключения.

 10) сторож переносимости SQL журнала (по конструкции, не прогон на живом
     PostgreSQL);
 11) фактическая последовательность вызовов привязана к объявленному списку:
     объявленный порядок проходит целиком, два шага, переставленные ВМЕСТЕ со
     своими id, отвергаются ДО выполнения переставленного шага, а пропуск шага
     не даёт объявить старт завершённым;
 12) SUPPLY-1 (ревью PR #46, discussion_r3894000377) записан в журнал СВОЕЙ
     строкой, а не под идентичностью уже выпущенного `init_db`. База
     синтетическая и собрана как боевая в момент выката: девять выпущенных
     шагов в журнале уже есть, колонки `cc_batch_id` ещё нет. Старт добавляет
     ровно одну строку — `models.ensure_supply_schema` на позиции 10, со своим
     applied_at, — и не переписывает ни одной прежней. Отдельно проверено, что
     эта строка НЕ превращает условный backfill в one-time migration: заказ,
     созданный откатившимся старым кодом уже ПОСЛЕ записи шага, лечится
     следующим стартом.

Пункт 9 в этой форме — прямое следствие ревью жизненного цикла 28.08.2026
(PR #44, discussion_r3884250490). Прежняя версия проверяла только неизменность
строк журнала и потому пропускала настоящий дефект: конфликт ловился на ЗАПИСИ,
то есть уже ПОСЛЕ того, как шаг отработал, и `init_db()` успевал создать схему
(в синтетической базе становилось 27 таблиц). Проверка, которая не смотрит на
схему, такой замок считает исправным.

Пункт 11 — следствие второй претензии того же ревью (discussion_r3884257316), и
она про другое. Пара (id, позиция) СТАТИЧНА: если будущая правка переставит два
вызова ВМЕСТЕ с их идентификаторами, каждая пара по-прежнему совпадёт с
журналом, `record_migration_step` вернёт False, и старт пройдёт целиком — при
том что миграции выполнились в другом порядке. Замок защищал бы отображение
«id → позиция», а не ту последовательность, ради которой заведён. Проверка
работает с контрактом `_startup_step`, а не с телом `_startup()`: тело — это и
есть то, что может однажды переставить правка, подделывать его в тесте
бессмысленно.

Запуск из корня репозитория: python tests/test_startup_lifecycle.py
"""
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        PASS.append(name)
        print(f"  OK   {name}" + (f"  [{detail}]" if detail else ""))
    else:
        FAIL.append(name)
        print(f"  FAIL {name}  {detail}")


STEPS = [
    "init_db",
    "lessons.ensure_schema",
    "exclusions.ensure_schema",
    "ms_sync.ensure_schema",
    "ms_sync.reset_stale_running",
    "ms_writeback.ensure_schema",
    "ms_vendor.ensure_schema",
    "subscription.ensure_schema",
    "subscription.log_preview",
    # SUPPLY-1: терминальный шаг, дописанный В КОНЕЦ append-only реестра
    # (ревью PR #46, discussion_r3894000377). Позиция 10 — новая, девять
    # прежних пар (id, позиция) не тронуты.
    "models.ensure_supply_schema",
    # SUPPLY-3: тот же append-only контракт ещё раз. Позиция 11 новая, десять
    # прежних пар не тронуты — иначе старт на боевой базе упал бы
    # MigrationLedgerConflict, и это замок, а не дефект.
    "models.ensure_supply_planning_schema",
]

# Девять шагов, ВЫПУЩЕННЫХ до SUPPLY-1: ровно то, что журнал боевой базы уже
# содержит. Список выписан явно, а не срезом STEPS[:9]: его смысл именно в
# том, что эти строки не меняются, и срез от растущего списка этого бы не
# удержал — он «поехал» бы вместе с любой будущей вставкой.
RELEASED_BEFORE_SUPPLY = [
    ("init_db", 1),
    ("lessons.ensure_schema", 2),
    ("exclusions.ensure_schema", 3),
    ("ms_sync.ensure_schema", 4),
    ("ms_sync.reset_stale_running", 5),
    ("ms_writeback.ensure_schema", 6),
    ("ms_vendor.ensure_schema", 7),
    ("subscription.ensure_schema", 8),
    ("subscription.log_preview", 9),
]
SUPPLY_STEP = ("models.ensure_supply_schema", 10)

# Десять шагов, ВЫПУЩЕННЫХ до SUPPLY-3. Список снова выписан явно, а не срезом:
# смысл ровно в том, что эти строки журнала боевой базы не меняются.
RELEASED_BEFORE_PLANNING = RELEASED_BEFORE_SUPPLY + [SUPPLY_STEP]
PLANNING_STEP = ("models.ensure_supply_planning_schema", 11)

# Общий пролог дочернего процесса: подменяет одиннадцать шагов старта и
# scheduler.start/shutdown ДО импорта app.main (см. докстринг файла — почему
# именно до, а не после). lessons.ensure_schema патчится первым из
# ensure_schema-шагов и раньше `import app.main`, чтобы поймать и старый
# вызов на импорте app.api, если он ещё есть.
_CHILD_PREAMBLE = """
import os, sys
sys.path.insert(0, {root!r})
os.environ["DATABASE_URL"] = "sqlite:///" + {db!r}
os.environ["SCHEDULER_ENABLED"] = "1"
os.environ.pop("WEB_CONCURRENCY", None)
os.environ.pop("OBOROT_ALLOW_MULTIPROC", None)

order = []

import app.db as _db_mod
_orig_init_db = _db_mod.init_db
def _w_init_db():
    order.append("init_db")
    return _orig_init_db()
_db_mod.init_db = _w_init_db

import app.lessons as _lessons
_orig_lessons = _lessons.ensure_schema
def _w_lessons():
    order.append("lessons.ensure_schema")
    return _orig_lessons()
_lessons.ensure_schema = _w_lessons

import app.scheduler as scheduler_mod
_orig_start = scheduler_mod.start
def _w_start():
    order.append("scheduler.start")
    return _orig_start()
scheduler_mod.start = _w_start

import app.main as m  # attach() и `from app.db import init_db` резолвятся здесь;
                      # если lessons.ensure_schema ещё вызывается на импорте
                      # app.api — он случится прямо на этой строке.

import app.exclusions as _excl
_orig_excl = _excl.ensure_schema
def _w_excl():
    order.append("exclusions.ensure_schema")
    return _orig_excl()
_excl.ensure_schema = _w_excl

import app.ms_sync as _mssync
_orig_mssync_schema = _mssync.ensure_schema
def _w_mssync_schema():
    order.append("ms_sync.ensure_schema")
    return _orig_mssync_schema()
_mssync.ensure_schema = _w_mssync_schema

_orig_reset_stale = _mssync.reset_stale_running
def _w_reset_stale():
    order.append("ms_sync.reset_stale_running")
    return _orig_reset_stale()
_mssync.reset_stale_running = _w_reset_stale

import app.ms_writeback as _mswb
_orig_mswb = _mswb.ensure_schema
def _w_mswb():
    order.append("ms_writeback.ensure_schema")
    return _orig_mswb()
_mswb.ensure_schema = _w_mswb

import app.ms_vendor as _msv
_orig_msv = _msv.ensure_schema
def _w_msv():
    order.append("ms_vendor.ensure_schema")
    return _orig_msv()
_msv.ensure_schema = _w_msv

import app.subscription as _sub
_orig_sub_schema = _sub.ensure_schema
def _w_sub_schema():
    order.append("subscription.ensure_schema")
    return _orig_sub_schema()
_sub.ensure_schema = _w_sub_schema

_orig_sub_preview = _sub.log_preview
def _w_sub_preview():
    order.append("subscription.log_preview")
    return _orig_sub_preview()
_sub.log_preview = _w_sub_preview

import app.models as _models
_orig_supply = _models.ensure_supply_schema
def _w_supply(*a, **kw):
    order.append("models.ensure_supply_schema")
    return _orig_supply(*a, **kw)
_models.ensure_supply_schema = _w_supply
_orig_planning = _models.ensure_supply_planning_schema
def _w_planning(*a, **kw):
    order.append("models.ensure_supply_planning_schema")
    return _orig_planning(*a, **kw)
_models.ensure_supply_planning_schema = _w_planning
"""


def _run_child(code: str, timeout: int = 60) -> tuple[int, str]:
    p = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                        capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout + p.stderr)


def _fresh_db(name: str) -> Path:
    p = ROOT / name
    if p.exists():
        p.unlink()
    return p


def check_order() -> None:
    """Проверка 1: реальный порядок одиннадцати шагов старта и scheduler.start."""
    db = _fresh_db("test_startup_order.db")
    code = _CHILD_PREAMBLE.format(root=str(ROOT), db=str(db)) + """
from fastapi.testclient import TestClient
with TestClient(m.app) as c:
    r = c.get("/health/ready")
    print("READY", r.status_code, r.json().get("scheduler"))
for step in order:
    print("ORDER:" + step)
"""
    rc, out = _run_child(code)
    check("дочерний процесс завершился успешно (проверка порядка)", rc == 0, out[-400:])
    order = [ln.split("ORDER:", 1)[1] for ln in out.splitlines() if ln.startswith("ORDER:")]
    check("зафиксированы все одиннадцать шагов старта",
          set(STEPS + ["scheduler.start"]) <= set(order), f"order={order}")
    check("lessons.ensure_schema вызван РОВНО ОДИН раз",
          order.count("lessons.ensure_schema") == 1, f"order={order}")
    if "init_db" in order and "lessons.ensure_schema" in order:
        idx_init = order.index("init_db")
        idx_lessons = order.index("lessons.ensure_schema")
        check("lessons.ensure_schema идёт СРАЗУ после init_db",
              idx_lessons == idx_init + 1, f"order={order}")
    else:
        check("lessons.ensure_schema идёт СРАЗУ после init_db", False, f"order={order}")
    if "scheduler.start" in order:
        idx_sched = order.index("scheduler.start")
        idx_last_step = max((order.index(s) for s in STEPS if s in order), default=-1)
        check("scheduler.start выполняется ПОСЛЕ всех миграций/схем",
              idx_sched > idx_last_step, f"order={order}")
    else:
        check("scheduler.start выполняется ПОСЛЕ всех миграций/схем", False, "scheduler.start не вызван")
    check("/health/ready подтверждает: планировщик реально стартовал",
          "READY 200 True" in out, out[-200:])
    if db.exists():
        db.unlink()


def check_failure_prevents_scheduler_start() -> None:
    """Проверка 2: сбой на КАЖДОМ из десяти шагов старта — планировщик не стартует,
    и ни один из шагов, идущих ПОСЛЕ упавшего по порядку, не выполняется."""
    for failing_step in STEPS:
        db = _fresh_db(f"test_startup_fail_{failing_step.replace('.', '_')}.db")
        # init_db разрешается в app.main через `from app.db import ... init_db`
        # ПРИ ИМПОРТЕ — патчить app.db.init_db ПОСЛЕ import app.main бесполезно,
        # у app.main уже своя копия имени. Патчим саму копию — m.init_db.
        # Остальные девять читаются внутри _startup() заново на каждый вызов
        # (`from app import X as _x; _x.метод()`) — патч атрибута субмодуля
        # после импорта app.main их ловит как положено.
        target_var = {
            "init_db": "m.init_db",
            "lessons.ensure_schema": "_lessons.ensure_schema",
            "exclusions.ensure_schema": "_excl.ensure_schema",
            "ms_sync.ensure_schema": "_mssync.ensure_schema",
            "ms_sync.reset_stale_running": "_mssync.reset_stale_running",
            "ms_writeback.ensure_schema": "_mswb.ensure_schema",
            "ms_vendor.ensure_schema": "_msv.ensure_schema",
            "subscription.ensure_schema": "_sub.ensure_schema",
            "subscription.log_preview": "_sub.log_preview",
            "models.ensure_supply_schema": "_models.ensure_supply_schema",
            "models.ensure_supply_planning_schema":
                "_models.ensure_supply_planning_schema",
        }[failing_step]
        code = _CHILD_PREAMBLE.format(root=str(ROOT), db=str(db)) + f"""
def _boom(*a, **kw):
    order.append("BOOM:{failing_step}")
    raise RuntimeError("injected startup failure: {failing_step}")
{target_var} = _boom

from fastapi.testclient import TestClient
raised = False
try:
    with TestClient(m.app) as c:
        pass
except Exception as exc:
    raised = True
    print("RAISED", type(exc).__name__)
print("STARTED_FLAG", scheduler_mod._started)
print("SCHEDULER_START_CALLED", "scheduler.start" in order)
for step in order:
    print("ORDER:" + step)
print("RAISED_FLAG", raised)
"""
        rc, out = _run_child(code)
        check(f"дочерний процесс завершился (инъекция сбоя в {failing_step})",
              rc == 0, out[-400:])
        check(f"старт приложения падает на инъекции сбоя в {failing_step}",
              "RAISED_FLAG True" in out, out[-300:])
        check(f"планировщик НЕ стартовал при сбое в {failing_step}",
              "STARTED_FLAG False" in out and "SCHEDULER_START_CALLED False" in out,
              out[-300:])
        order_lines = [ln.split("ORDER:", 1)[1] for ln in out.splitlines() if ln.startswith("ORDER:")]
        idx_fail = STEPS.index(failing_step)
        later_steps = STEPS[idx_fail + 1:]
        check(f"ни один шаг ПОСЛЕ {failing_step} не выполнился",
              not any(s in order_lines for s in later_steps),
              f"order={order_lines}")
        if db.exists():
            db.unlink()


def check_import_has_no_schema_side_effect() -> None:
    """Проверка 3: import app.api и import app.main САМИ ПО СЕБЕ (без ASGI-цикла)
    не вызывают lessons.ensure_schema.

    Патч ставится ДО первого импорта app.api/app.main в ЧИСТОМ дочернем
    процессе: если бы патч ставился после импорта в этом же интерпретаторе,
    уже случившийся на импорте вызов прошёл бы мимо счётчика, и проверка
    ловила бы ложный успех вместо реального дефекта.
    """
    db = _fresh_db("test_startup_import_side_effect.db")
    code = f"""
import os, sys
sys.path.insert(0, {str(ROOT)!r})
os.environ["DATABASE_URL"] = "sqlite:///" + {str(db)!r}
os.environ["SCHEDULER_ENABLED"] = "1"
os.environ.pop("WEB_CONCURRENCY", None)
os.environ.pop("OBOROT_ALLOW_MULTIPROC", None)

calls = []
import app.lessons as _lessons
_orig = _lessons.ensure_schema
def _w():
    calls.append(1)
    return _orig()
_lessons.ensure_schema = _w

import app.api  # noqa: F401 — сам факт импорта не должен трогать схему
print("CALLS_AFTER_API_IMPORT", len(calls))
import app.main  # noqa: F401 — main импортирует api заново (уже в sys.modules)
print("CALLS_AFTER_MAIN_IMPORT", len(calls))
"""
    rc, out = _run_child(code)
    check("дочерний процесс завершился успешно (импорт без побочного эффекта)", rc == 0, out[-400:])
    check("import app.api НЕ вызывает lessons.ensure_schema",
          "CALLS_AFTER_API_IMPORT 0" in out, out[-300:])
    check("import app.main НЕ вызывает lessons.ensure_schema",
          "CALLS_AFTER_MAIN_IMPORT 0" in out, out[-300:])
    if db.exists():
        db.unlink()


def check_repeated_shutdown_safe() -> None:
    """Проверка 4: повторный shutdown идемпотентен и не роняет процесс."""
    db = _fresh_db("test_startup_shutdown.db")
    code = _CHILD_PREAMBLE.format(root=str(ROOT), db=str(db)) + """
from fastapi.testclient import TestClient
with TestClient(m.app) as c:
    c.get("/health/live")
print("AFTER_ASGI_SHUTDOWN_STARTED", scheduler_mod._started)

# ASGI-цикл уже вызвал shutdown ровно один раз. Дёргаем ещё дважды руками —
# идемпотентность не должна зависеть от того, что вызывающая сторона больше
# не тронет planировщик после первого shutdown.
scheduler_mod.shutdown()
scheduler_mod.shutdown()
print("STARTED_AFTER_DOUBLE_SHUTDOWN", scheduler_mod._started)
print("NO_EXCEPTION", True)
"""
    rc, out = _run_child(code)
    check("дочерний процесс завершился успешно (повторный shutdown)", rc == 0, out[-400:])
    check("после ASGI shutdown планировщик остановлен",
          "AFTER_ASGI_SHUTDOWN_STARTED False" in out, out[-300:])
    check("повторный ручной shutdown не бросает исключение",
          "NO_EXCEPTION True" in out, out[-300:])
    check("после двойного shutdown планировщик остаётся остановленным",
          "STARTED_AFTER_DOUBLE_SHUTDOWN False" in out, out[-300:])


# ── OPS-5: журнал применённых шагов старта ───────────────────────────────────
#
# Объявленный порядок продублирован здесь НАМЕРЕННО: тест не импортирует
# app.main, чтобы сверяться с той же переменной, которую проверяет. Если
# app.main.STARTUP_SCHEMA_STEPS разойдётся с этим списком, проверка 5 упадёт —
# ровно этого от неё и ждут.
LEDGER_STEPS = [(name, pos) for pos, name in enumerate(STEPS, 1)]

_LEDGER_DDL = (
    "CREATE TABLE IF NOT EXISTS migration_ledger ("
    "step_id VARCHAR(128) NOT NULL PRIMARY KEY, "
    "step_order INTEGER NOT NULL, "
    "applied_at VARCHAR(32) NOT NULL)"
)


def _purge_db(name: str) -> Path:
    """Свежий путь синтетической базы: убирает и файл, и хвосты WAL."""
    p = ROOT / name
    for suffix in ("", "-wal", "-shm"):
        f = Path(str(p) + suffix)
        if f.exists():
            f.unlink()
    return p


def _read_ledger(db: Path) -> list[tuple[str, int, str]]:
    """Журнал синтетической базы как список (step_id, step_order, applied_at)."""
    con = sqlite3.connect(str(db))
    try:
        rows = con.execute(
            "SELECT step_id, step_order, applied_at FROM migration_ledger "
            "ORDER BY step_order"
        ).fetchall()
    except sqlite3.OperationalError:
        return []                      # таблицы нет вовсе — это тоже ответ
    finally:
        con.close()
    return [(r[0], int(r[1]), r[2]) for r in rows]


def _tables(db: Path) -> set[str]:
    """Имена таблиц синтетической базы (без служебных sqlite_*)."""
    if not db.exists():
        return set()
    con = sqlite3.connect(str(db))
    try:
        return {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")
            if not r[0].startswith("sqlite_")}
    finally:
        con.close()


def _batch_ids(db: Path) -> dict:
    """`{id: cc_batch_id}` из синтетической базы; {'ERR': ...} — колонки нет."""
    con = sqlite3.connect(str(db))
    try:
        return {r[0]: r[1] for r in con.execute(
            "SELECT id, cc_batch_id FROM production_orders ORDER BY id")}
    except sqlite3.OperationalError as exc:
        return {"ERR": str(exc)}
    finally:
        con.close()


def _seed_ledger(db: Path, rows: list[tuple[str, int, str]]) -> None:
    """Кладёт в синтетическую базу заранее заданные строки журнала."""
    con = sqlite3.connect(str(db))
    try:
        con.execute(_LEDGER_DDL)
        con.executemany(
            "INSERT INTO migration_ledger (step_id, step_order, applied_at) "
            "VALUES (?, ?, ?)", rows)
        con.commit()
    finally:
        con.close()


_BOOT_CHILD = """
from fastapi.testclient import TestClient
raised = ""
try:
    with TestClient(m.app) as c:
        c.get("/health/ready")
except Exception as exc:
    raised = type(exc).__name__ + ": " + str(exc)
print("RAISED", raised)
print("SCHEDULER_START_CALLED", "scheduler.start" in order)
for step in order:
    print("ORDER:" + step)
"""


def _boot(db: Path, extra: str = "") -> tuple[int, str, list[str]]:
    """Поднимает приложение на указанной базе в отдельном процессе."""
    code = _CHILD_PREAMBLE.format(root=str(ROOT), db=str(db)) + extra + _BOOT_CHILD
    rc, out = _run_child(code)
    order = [ln.split("ORDER:", 1)[1] for ln in out.splitlines() if ln.startswith("ORDER:")]
    return rc, out, order


def check_ledger_clean_db() -> None:
    """Проверка 5: на чистой базе журнал содержит ровно одиннадцать шагов."""
    db = _purge_db("test_startup_ledger_clean.db")
    rc, out, order = _boot(db)
    check("дочерний процесс завершился успешно (журнал, чистая база)", rc == 0, out[-400:])
    check("старт на чистой базе не упал", "RAISED \n" in out or "RAISED\n" in out, out[-300:])
    rows = _read_ledger(db)
    check("журнал содержит ровно одиннадцать строк", len(rows) == 11, f"rows={rows}")
    check("id и позиции журнала совпадают с объявленным порядком",
          [(r[0], r[1]) for r in rows] == LEDGER_STEPS, f"rows={rows}")
    check("позиции идут 1..11 по возрастанию без пропусков",
          [r[1] for r in rows] == list(range(1, 12)), f"rows={rows}")
    check("у каждой строки непустой applied_at",
          all(r[2] and r[2].endswith("Z") for r in rows), f"rows={rows}")
    exec_order = [st for st in order if st in STEPS]
    check("порядок в журнале совпадает с фактическим порядком вызовов",
          [r[0] for r in rows] == exec_order, f"ledger={[r[0] for r in rows]} exec={exec_order}")
    check("scheduler.start вызван ПОСЛЕ последней записи в журнал",
          order and order[-1] == "scheduler.start", f"order={order}")
    _purge_db("test_startup_ledger_clean.db")


def check_ledger_legacy_db() -> None:
    """Проверка 6: база прежней схемы и база без журнала поднимаются и журналируются.

    (а) синтетическая база СТАРОЙ формы — те же таблицы, что в smoke-шаге CI
        «миграции на базе прежней схемы»: новых колонок ещё нет;
    (б) уже мигрированная база, у которой журнала ещё нет вовсе, — это ровно
        то, что увидит прод в момент выката: схема на месте, таблицы журнала
        не существует.
    """
    # (а) прежняя схема
    db = _purge_db("test_startup_ledger_legacy.db")
    con = sqlite3.connect(str(db))
    con.executescript("""
      CREATE TABLE orgs (id INTEGER PRIMARY KEY, name VARCHAR(255) NOT NULL,
                         plan VARCHAR(32) NOT NULL DEFAULT 'trial',
                         settings_json TEXT NOT NULL DEFAULT '{}');
      CREATE TABLE productions (id INTEGER PRIMARY KEY, org_id INTEGER NOT NULL,
                         name VARCHAR(120) NOT NULL,
                         is_main BOOLEAN NOT NULL DEFAULT 0);
      INSERT INTO orgs (name) VALUES ('Синтетическая организация');
      INSERT INTO productions (org_id, name, is_main) VALUES (1, 'Синтетический цех', 1);
    """)
    con.commit()
    con.close()
    check("до старта журнала на базе прежней схемы нет", _read_ledger(db) == [])
    rc, out, order = _boot(db)
    check("дочерний процесс завершился успешно (база прежней схемы)", rc == 0, out[-400:])
    check("приложение поднялось на базе прежней схемы",
          "SCHEDULER_START_CALLED True" in out, out[-300:])
    con = sqlite3.connect(str(db))
    cols = {r[1] for r in con.execute("PRAGMA table_info(productions)")}
    con.close()
    missing = {"lead_time_days", "moq", "pack_multiple", "stages_json"} - cols
    check("аддитивные миграции доехали на базе прежней схемы (колонки на месте)",
          not missing, f"missing={missing}")
    check("журнал заполнен целиком на базе прежней схемы",
          [(r[0], r[1]) for r in _read_ledger(db)] == LEDGER_STEPS,
          f"rows={_read_ledger(db)}")
    _purge_db("test_startup_ledger_legacy.db")

    # (б) мигрированная база без журнала — что увидит прод в момент выката
    db2 = _purge_db("test_startup_ledger_dropped.db")
    rc, out, _ = _boot(db2)
    check("дочерний процесс завершился успешно (подготовка базы без журнала)",
          rc == 0, out[-400:])
    con = sqlite3.connect(str(db2))
    try:
        con.execute("DROP TABLE migration_ledger")
        con.commit()
        dropped = ""
    except sqlite3.OperationalError as exc:
        dropped = str(exc)     # таблицы нет вовсе — журнал не ведётся
    finally:
        con.close()
    check("журнал у подготовленной базы удалён",
          not dropped and _read_ledger(db2) == [], dropped)
    rc, out, order = _boot(db2)
    check("дочерний процесс завершился успешно (база без журнала)", rc == 0, out[-400:])
    check("приложение поднялось на мигрированной базе без журнала",
          "SCHEDULER_START_CALLED True" in out, out[-300:])
    check("журнал восстановлен целиком и в объявленном порядке",
          [(r[0], r[1]) for r in _read_ledger(db2)] == LEDGER_STEPS,
          f"rows={_read_ledger(db2)}")
    check("все одиннадцать шагов выполнились и на базе без журнала",
          [st for st in order if st in STEPS] == STEPS, f"order={order}")
    _purge_db("test_startup_ledger_dropped.db")


def check_ledger_repeat_startup() -> None:
    """Проверка 7: повторный старт — no-op для журнала, но НЕ пропуск шагов."""
    db = _purge_db("test_startup_ledger_repeat.db")
    rc, out, order_first = _boot(db)
    check("дочерний процесс завершился успешно (первый старт)", rc == 0, out[-400:])
    first = _read_ledger(db)
    # applied_at пишется с секундной точностью: без паузы повторная запись
    # (если бы она случилась) могла бы совпасть по времени с первой, и
    # проверка «время не переписано» ничего бы не доказала.
    time.sleep(1.1)
    rc, out, order_second = _boot(db)
    check("дочерний процесс завершился успешно (повторный старт)", rc == 0, out[-400:])
    second = _read_ledger(db)
    check("повторный старт не добавил строк в журнал", len(second) == 11, f"rows={second}")
    check("повторный старт не переписал journal (строки идентичны первым)",
          second == first, f"first={first} second={second}")
    check("повторный старт не изменил applied_at ни одной строки",
          [r[2] for r in second] == [r[2] for r in first],
          f"first={[r[2] for r in first]} second={[r[2] for r in second]}")
    check("повторный старт ВЫПОЛНИЛ все одиннадцать шагов (журнал не повод пропускать)",
          [st for st in order_second if st in STEPS] == STEPS, f"order={order_second}")
    check("повторный старт довёл дело до планировщика",
          "scheduler.start" in order_second, f"order={order_second}")
    check("порядок шагов на повторном старте тот же, что на первом",
          [st for st in order_second if st in STEPS] ==
          [st for st in order_first if st in STEPS])
    _purge_db("test_startup_ledger_repeat.db")


def check_ledger_not_recorded_on_failure() -> None:
    """Проверка 8: упавший шаг и все следующие за ним записи в журнал не получают."""
    # Последний в списке — SUPPLY-1: терминальный шаг проверяется отдельно и
    # намеренно. У него нет «следующего», чьё отсутствие в журнале выдало бы
    # проблему за компанию, поэтому ложную запись о нём ловит ровно одно —
    # отсутствие его собственной строки.
    for failing_step in ("init_db", "ms_writeback.ensure_schema",
                         "subscription.log_preview",
                         "models.ensure_supply_planning_schema"):
        db = _purge_db(f"test_startup_ledger_fail_{failing_step.replace('.', '_')}.db")
        target_var = {
            "init_db": "m.init_db",
            "ms_writeback.ensure_schema": "_mswb.ensure_schema",
            "subscription.log_preview": "_sub.log_preview",
            "models.ensure_supply_planning_schema":
                "_models.ensure_supply_planning_schema",
        }[failing_step]
        extra = f"""
def _boom(*a, **kw):
    order.append("BOOM:{failing_step}")
    raise RuntimeError("injected startup failure: {failing_step}")
{target_var} = _boom
"""
        rc, out, order = _boot(db, extra)
        check(f"дочерний процесс завершился (журнал, сбой в {failing_step})",
              rc == 0, out[-400:])
        check(f"планировщик НЕ стартовал при сбое в {failing_step} (журнал)",
              "SCHEDULER_START_CALLED False" in out, out[-300:])
        idx = STEPS.index(failing_step)
        expected = [(name, pos) for name, pos in LEDGER_STEPS if pos <= idx]
        rows = [(r[0], r[1]) for r in _read_ledger(db)]
        check(f"в журнале ровно шаги ДО {failing_step} и ни одного после",
              rows == expected, f"rows={rows} expected={expected}")
        _purge_db(f"test_startup_ledger_fail_{failing_step.replace('.', '_')}.db")


def check_ledger_supply_step_is_distinct() -> None:
    """Проверка 12: у SUPPLY-1 собственное свидетельство, отдельное от init_db.

    Прямое следствие ревью PR #46 (discussion_r3894000377). Пока схемная работа
    SUPPLY-1 шла внутри `models.ensure_schema()`, она исполнялась под
    идентичностью УЖЕ ВЫПУЩЕННОГО шага `init_db` позиции 1: на базе, где эта
    строка журнала давно есть, о применении SUPPLY-1 не появлялось никакого
    свидетельства вовсе, и порядок относительно будущих миграций журнал не
    удерживал. Проверки 5–8 такого дефекта не видят: они работают на базе, где
    журнала ещё нет, и там разницы между «шаг свой» и «шаг чужой» не возникает.

    База здесь собрана ровно как боевая в момент выката:
      * `production_orders` СТАРОЙ формы — колонки `cc_batch_id` ещё нет;
      * журнал уже содержит девять выпущенных шагов, включая `init_db`.

    Доказывается три вещи, и третья — самая важная:
      * старт добавляет РОВНО ОДНУ строку, и это `models.ensure_supply_schema`
        на позиции 10, со своим собственным applied_at;
      * девять прежних строк не переписаны — ни id, ни позиция, ни время;
      * записанная строка НЕ становится основанием пропустить работу: заказ,
        вставленный ПОСЛЕ неё откатившимся старым кодом, лечится следующим
        стартом. Ledger здесь свидетельство, а не one-time migration flag —
        разница ровно та, ради которой backfill сделан условным.
    """
    db = _purge_db("test_startup_ledger_supply.db")
    con = sqlite3.connect(str(db))
    con.executescript("""
      CREATE TABLE orgs (id INTEGER PRIMARY KEY, name VARCHAR(255) NOT NULL,
                         plan VARCHAR(32) NOT NULL DEFAULT 'trial',
                         settings_json TEXT NOT NULL DEFAULT '{}');
      CREATE TABLE production_orders (
        id INTEGER PRIMARY KEY, org_id INTEGER NOT NULL, name VARCHAR(255) NOT NULL,
        created_at DATETIME, eta_date VARCHAR(10),
        status VARCHAR(16) NOT NULL DEFAULT 'draft',
        items_json TEXT NOT NULL DEFAULT '[]');
      INSERT INTO orgs (name) VALUES ('Синтетическая организация');
      INSERT INTO production_orders (id, org_id, name, created_at)
        VALUES (1, 1, 'Партия до SUPPLY-1', '2025-04-17 10:00:00');
    """)
    con.commit()
    con.close()
    seeded = [(sid, pos, "2026-01-01T00:00:00Z") for sid, pos in RELEASED_BEFORE_SUPPLY]
    _seed_ledger(db, seeded)
    check("до старта в журнале ровно девять выпущенных шагов, SUPPLY-1 среди них нет",
          _read_ledger(db) == seeded, f"rows={_read_ledger(db)}")
    check("до старта колонки партии в таблице заказов нет",
          "ERR" in _batch_ids(db), f"orders={_batch_ids(db)}")

    rc, out, order = _boot(db)
    check("дочерний процесс завершился успешно (SUPPLY-1 на базе с журналом)",
          rc == 0, out[-400:])
    check("старт на базе с девятью выпущенными шагами не упал",
          "SCHEDULER_START_CALLED True" in out, out[-300:])
    rows = _read_ledger(db)
    # На базе с девятью выпущенными шагами дописываются ДВА терминальных:
    # SUPPLY-1 (позиция 10) и SUPPLY-3 (позиция 11). Их ровно два, они идут
    # своим порядком, и у каждого своё время.
    check("журнал прирос ровно двумя строками", len(rows) == 11, f"rows={rows}")
    check("девять выпущенных строк не переписаны (id, позиция и applied_at те же)",
          rows[:9] == seeded, f"rows={rows[:9]} seeded={seeded}")
    fresh = [r for r in rows if (r[0], r[1]) not in {(s[0], s[1]) for s in seeded}]
    check("новые строки — SUPPLY-1 на позиции 10 и SUPPLY-3 на позиции 11",
          len(fresh) == 2 and (fresh[0][0], fresh[0][1]) == SUPPLY_STEP
          and (fresh[1][0], fresh[1][1]) == PLANNING_STEP, f"new={fresh}")
    check("у обоих собственный applied_at, а не время выпущенных шагов",
          len(fresh) == 2 and all(f[2].endswith("Z") and f[2] != seeded[0][2]
                                  for f in fresh), f"new={fresh}")
    check("шаг SUPPLY-1 действительно выполнился на этом старте",
          "models.ensure_supply_schema" in order, f"order={order}")
    check("SUPPLY-1 выполнился ПОСЛЕ init_db, а не вместо него",
          "init_db" in order and "models.ensure_supply_schema" in order
          and order.index("models.ensure_supply_schema") > order.index("init_db"),
          f"order={order}")
    healed = _batch_ids(db)
    check("схемная работа доехала: старый заказ получил идентификатор партии",
          bool(healed.get(1)), f"orders={healed}")

    # Откатившийся старый код: INSERT без колонки, значение приходит из
    # server_default=''. Это происходит УЖЕ ПОСЛЕ того, как шаг записан в
    # журнал, — ровно здесь одноразовая миграция оставила бы партию без имени.
    con = sqlite3.connect(str(db))
    con.execute("INSERT INTO production_orders (id, org_id, name, created_at) "
                "VALUES (2, 1, 'Заказ откатившегося кода', '2026-08-31 12:00:00')")
    con.commit()
    con.close()
    check("старый код создал строку с пустым идентификатором уже после записи шага",
          _batch_ids(db).get(2) == "", f"orders={_batch_ids(db)}")

    rc, out, order = _boot(db)
    check("дочерний процесс завершился успешно (старт после отката)", rc == 0, out[-400:])
    check("шаг SUPPLY-1 выполнился снова, хотя в журнале уже записан",
          "models.ensure_supply_schema" in order, f"order={order}")
    after = _batch_ids(db)
    check("запись в журнале НЕ стала основанием пропустить backfill",
          bool(after.get(2)), f"orders={after}")
    check("и уже выданный идентификатор соседа не переписан",
          after.get(1) == healed.get(1), f"было={healed} стало={after}")
    check("повторный старт журнал не изменил: те же одиннадцать строк и то же время",
          _read_ledger(db) == rows, f"rows={_read_ledger(db)} было={rows}")
    _purge_db("test_startup_ledger_supply.db")


def check_ledger_conflict_fails_closed() -> None:
    """Проверка 9: конфликт объявленного порядка и журнала валит старт.

    Два направления конфликта, оба обязаны падать, а не выдаваться за
    «уже применено»:
      * тот же id записан на ДРУГОЙ позиции (шаг переставили);
      * та же позиция занята ДРУГИМ id (на место шага встал чужой).
    """
    cases = [
        ("id на чужой позиции", [("init_db", 99, "2026-01-01T00:00:00Z")], "init_db"),
        ("позиция под чужим id", [("legacy.step", 1, "2026-01-01T00:00:00Z")], "legacy.step"),
    ]
    for idx, (title, seeded, _marker) in enumerate(cases, 1):
        name = f"test_startup_ledger_conflict_{idx}.db"
        db = _purge_db(name)
        _seed_ledger(db, seeded)
        rc, out, order = _boot(db)
        check(f"дочерний процесс завершился ({title})", rc == 0, out[-400:])
        check(f"старт упал на конфликте: {title}",
              "RAISED MigrationLedgerConflict" in out, out[-400:])
        check(f"конфликт не выдан за «уже применено» (старт не дошёл до конца): {title}",
              "SCHEDULER_START_CALLED False" in out, out[-300:])
        rows = _read_ledger(db)
        check(f"журнал остался ровно таким, каким был до старта: {title}",
              rows == seeded, f"rows={rows} seeded={seeded}")
        # Ревью 28.08.2026 (discussion_r3884250490): проверять неизменность
        # строк журнала мало. Пока конфликт ловился только на ЗАПИСИ, init_db
        # успевал создать схему до отказа — таблиц становилось 27, и `orgs`
        # существовала. Замок обязан останавливать процесс ДО этого.
        tables = _tables(db)
        check(f"конфликт не дал создать таблицы приложения: {title}",
              tables <= {"migration_ledger"}, f"tables={sorted(tables)}")
        check(f"таблицы orgs после отказа не появилось: {title}",
              "orgs" not in tables, f"tables={sorted(tables)}")
        check(f"ни один шаг старта не выполнился при конфликте: {title}",
              not [st for st in order if st in STEPS], f"order={order}")
        _purge_db(name)


def check_ledger_conflict_blocks_mutation() -> None:
    """Проверка 9в: защищаемый шаг НЕ выполняется, если его пара конфликтует.

    Прямое следствие ревью жизненного цикла 28.08.2026 (PR #44,
    discussion_r3884250490). Проверяется не «журнал не изменился», а то, что
    шага НЕ БЫЛО: на его место подставлен часовой — функция, которая создаёт
    таблицу `sentinel_ops5_must_not_exist`. Если конфликт останавливает
    процесс слишком поздно, часовой успеет отработать и таблица появится.

    Конфликт объявлен на позиции 6 (`ms_writeback.ensure_schema`), то есть
    в СЕРЕДИНЕ списка: раньше это был как раз тот случай, когда пять
    предыдущих шагов уже отработали, прежде чем замок сработал.
    """
    db = _purge_db("test_startup_ledger_sentinel.db")
    _seed_ledger(db, [("legacy.step", 6, "2026-01-01T00:00:00Z")])
    extra = """
import sqlalchemy as _sa
import app.db as _dbm
def _sentinel(*a, **kw):
    order.append("SENTINEL:ms_writeback.ensure_schema")
    with _dbm.engine.begin() as conn:
        conn.execute(_sa.text("CREATE TABLE sentinel_ops5_must_not_exist (x INTEGER)"))
_mswb.ensure_schema = _sentinel
"""
    rc, out, order = _boot(db, extra)
    check("дочерний процесс завершился (часовой на конфликтном шаге)", rc == 0, out[-400:])
    check("старт упал на конфликте позиции 6",
          "RAISED MigrationLedgerConflict" in out, out[-400:])
    tables = _tables(db)
    check("часовой НЕ выполнился: таблицы-метки нет",
          "sentinel_ops5_must_not_exist" not in tables, f"tables={sorted(tables)}")
    check("часовой не отметился в порядке вызовов",
          not any(st.startswith("SENTINEL:") for st in order), f"order={order}")
    check("конфликт на позиции 6 не дал выполниться и предыдущим шагам",
          not [st for st in order if st in STEPS], f"order={order}")
    check("таблиц приложения не создано (конфликт на позиции 6)",
          tables <= {"migration_ledger"}, f"tables={sorted(tables)}")
    check("журнал не пополнился ни одной строкой",
          _read_ledger(db) == [("legacy.step", 6, "2026-01-01T00:00:00Z")],
          f"rows={_read_ledger(db)}")
    check("планировщик не стартовал", "SCHEDULER_START_CALLED False" in out, out[-300:])
    _purge_db("test_startup_ledger_sentinel.db")


def check_ledger_helper_contract() -> None:
    """Проверка 9б: контракт помощника напрямую — повтор, оба конфликта, гонка.

    Работает на отдельной синтетической базе и без ASGI-цикла: проверяется
    сам `app.db.record_migration_step`, а не старт приложения. Гонка —
    ограниченная и внутрипроцессная (четыре потока, один и тот же шаг): это
    тот случай, который реально бывает на проде при одновременном старте
    воркеров, и он проверяется без расширения границ пакета.
    """
    db = _purge_db("test_startup_ledger_helper.db")
    code = f"""
import os, sys, threading
sys.path.insert(0, {str(ROOT)!r})
os.environ["DATABASE_URL"] = "sqlite:///" + {str(db)!r}

from app.db import (MigrationLedgerConflict, read_migration_ledger,
                    record_migration_step)

print("FIRST", record_migration_step("step.one", 1))
print("REPEAT", record_migration_step("step.one", 1))

try:
    record_migration_step("step.one", 2)
    print("CONFLICT_ID_POS none")
except MigrationLedgerConflict:
    print("CONFLICT_ID_POS raised")
except Exception as exc:
    print("CONFLICT_ID_POS wrong:" + type(exc).__name__)

try:
    record_migration_step("step.other", 1)
    print("CONFLICT_POS_ID none")
except MigrationLedgerConflict:
    print("CONFLICT_POS_ID raised")
except Exception as exc:
    print("CONFLICT_POS_ID wrong:" + type(exc).__name__)

print("ROWS_AFTER_CONFLICTS", read_migration_ledger())

results, errors = [], []
barrier = threading.Barrier(4)
def worker():
    barrier.wait()
    try:
        results.append(record_migration_step("step.concurrent", 2))
    except Exception as exc:
        errors.append(type(exc).__name__ + ": " + str(exc))
threads = [threading.Thread(target=worker) for _ in range(4)]
for t in threads:
    t.start()
for t in threads:
    t.join()
print("CONC_TRUE", results.count(True))
print("CONC_FALSE", results.count(False))
print("CONC_ERRORS", errors)
print("CONC_ROWS", read_migration_ledger())
"""
    rc, out = _run_child(code)
    # Детализация — только строки-маркеры дочернего процесса: полный хвост
    # вывода здесь печатался бы и при успехе и утопил бы отчёт.
    marks = " | ".join(ln for ln in out.splitlines()
                       if ln.startswith(("FIRST", "REPEAT", "CONFLICT", "CONC", "ROWS_")))
    check("дочерний процесс завершился успешно (контракт помощника)", rc == 0, out[-400:])
    check("первая запись шага возвращает True", "FIRST True" in out, marks)
    check("повторная запись той же пары — no-op (False)", "REPEAT False" in out, marks)
    check("тот же id на другой позиции — MigrationLedgerConflict",
          "CONFLICT_ID_POS raised" in out, marks)
    check("та же позиция под другим id — MigrationLedgerConflict",
          "CONFLICT_POS_ID raised" in out, marks)
    check("конфликты ничего не записали в журнал",
          "ROWS_AFTER_CONFLICTS [('step.one', 1, " in out, marks)
    check("гонка четырёх потоков: ровно одна запись сделана этим процессом",
          "CONC_TRUE 1" in out, marks)
    check("гонка четырёх потоков: три остальных получили no-op",
          "CONC_FALSE 3" in out, marks)
    check("гонка четырёх потоков: ни одного исключения",
          "CONC_ERRORS []" in out, marks)
    check("гонка четырёх потоков: в журнале ровно одна строка шага",
          out.count("'step.concurrent', 2") == 1, marks)
    _purge_db("test_startup_ledger_helper.db")


def check_startup_order_is_enforced_at_runtime() -> None:
    """Проверка 11: фактическая последовательность вызовов привязана к объявлению.

    Дефект, который ловит эта проверка (ревью 28.08.2026, PR #44,
    discussion_r3884257316). Замок на журнале сверял СТАТИЧЕСКУЮ пару
    (id, позиция) — а она не меняется, если будущая правка переставит два
    вызова `_startup_step` ВМЕСТЕ с их идентификаторами. Тогда каждая пара
    по-прежнему совпадает с журналом, `record_migration_step` возвращает
    False, старт проходит целиком — и при этом миграции выполнились в другом
    порядке. То есть замок защищал отображение «id → позиция», а не ту
    последовательность, ради которой заводился.

    Проверяется контракт `_startup_step`, а не тело `_startup()`: тело — это
    и есть то, что может однажды переставить будущая правка, и подделывать
    его в тесте бессмысленно. Шаги подменены безобидными функциями-метками,
    настоящие миграции не выполняются, база синтетическая.

    Три сценария:
      * шаги, вызванные в объявленном порядке, проходят целиком;
      * два шага, переставленные ВМЕСТЕ со своими id, отвергаются — и
        отвергаются ДО того, как переставленный шаг успел отработать;
      * пропущенный шаг не даёт объявить старт завершённым.
    """
    db = _purge_db("test_startup_order_runtime.db")
    code = f"""
import os, sys
sys.path.insert(0, {str(ROOT)!r})
os.environ["DATABASE_URL"] = "sqlite:///" + {str(db)!r}
os.environ["SCHEDULER_ENABLED"] = "0"
os.environ.pop("WEB_CONCURRENCY", None)
os.environ.pop("OBOROT_ALLOW_MULTIPROC", None)

import app.main as m
from app.db import read_migration_ledger

STEPS = [sid for sid, _pos in m.STARTUP_SCHEMA_STEPS]
ran = []
def mk(name):
    def _f():
        ran.append(name)
    return _f

# 1) объявленный порядок проходит целиком
# `_finish_startup_steps` берётся через getattr НАМЕРЕННО: на коде до этой
# правки его нет, и RED-прогон должен показывать сам дефект (перестановка
# принята), а не падать раньше на отсутствующем имени.
finish = getattr(m, "_finish_startup_steps", None)
print("HAS_FINISH_HOOK", finish is not None)

m._validate_startup_order()
for sid in STEPS:
    m._startup_step(sid, mk(sid))
if finish:
    finish()
print("DECLARED_OK", ran == STEPS)
print("DECLARED_LEDGER", [(r[0], r[1]) for r in read_migration_ledger()] ==
      list(m.STARTUP_SCHEMA_STEPS))

# 2) перестановка ДВУХ шагов вместе с их id: пары (id, позиция) те же,
#    журнал те же строки принимает — ловить обязана проверка порядка
swapped = list(STEPS)
swapped[5], swapped[6] = swapped[6], swapped[5]
ran2 = []
def mk2(name):
    def _f():
        ran2.append(name)
    return _f
m._validate_startup_order()
verdict = "ACCEPTED"
for sid in swapped:
    try:
        m._startup_step(sid, mk2(sid))
    except Exception as exc:
        verdict = "REJECTED:" + type(exc).__name__
        break
print("SWAP_VERDICT", verdict)
print("SWAP_RAN", ran2)
print("SWAP_MISPLACED_RAN", swapped[5] in ran2)

# 3) пропуск шага не даёт объявить старт завершённым
m._validate_startup_order()
for sid in STEPS[:-1]:
    m._startup_step(sid, mk(sid))
if finish is None:
    print("SKIP_VERDICT ACCEPTED")     # проверять нечем — замка нет
else:
    try:
        finish()
        print("SKIP_VERDICT ACCEPTED")
    except Exception as exc:
        print("SKIP_VERDICT REJECTED:" + type(exc).__name__)
"""
    rc, out = _run_child(code)
    marks = " | ".join(ln for ln in out.splitlines()
                       if ln.startswith(("HAS_FINISH", "DECLARED_", "SWAP_", "SKIP_")))
    check("дочерний процесс завершился успешно (порядок в рантайме)", rc == 0, out[-500:])
    check("объявленный порядок исполняется целиком и попадает в журнал",
          "DECLARED_OK True" in out and "DECLARED_LEDGER True" in out, marks)
    check("перестановка двух шагов вместе с их id ОТВЕРГНУТА",
          "SWAP_VERDICT REJECTED:" in out, marks)
    check("переставленный шаг НЕ успел выполниться",
          "SWAP_MISPLACED_RAN False" in out, marks)
    check("до перестановки шаги успели отработать в объявленном порядке",
          "SWAP_RAN " in out and "SWAP_VERDICT ACCEPTED" not in out, marks)
    check("пропуск шага не даёт объявить старт завершённым",
          "SKIP_VERDICT REJECTED:" in out, marks)
    _purge_db("test_startup_order_runtime.db")


def check_ledger_sql_is_portable() -> None:
    """Проверка 10: в SQL журнала нет конструкций одного диалекта.

    ЧЕСТНАЯ ГРАНИЦА: это проверка ПО КОНСТРУКЦИИ, а не прогон на живом
    PostgreSQL — сервера и драйвера в окружении нет, а заводить их значило бы
    новую зависимость, которой этот пакет не предусматривает. Проверяется то,
    что проверить можно: весь SQL журнала состоит из общих для SQLite и
    PostgreSQL конструкций, время подставляется из Python, а не из
    `CURRENT_TIMESTAMP`/`now()` (разный формат и зона), и никакой
    UPSERT/`ON CONFLICT`/`INSERT OR IGNORE` не маскирует конфликт под
    «уже применено». Сторож нужен на будущее: он падает, если такую
    конструкцию однажды впишут в помощник.
    """
    import app.db as db_mod

    sql_sources = [db_mod._LEDGER_TABLE_DDL, db_mod._LEDGER_ORDER_INDEX_DDL]
    src = Path(db_mod.__file__).read_text(encoding="utf-8")
    ledger_src = src[src.index("_LEDGER_TABLE_DDL"):src.index("def init_db()")]
    # Сканируются ВСЕ строковые литералы этого участка, а не питон вокруг них:
    # `time.strftime` в Python переносим и как раз является правильным ответом,
    # а `strftime(` внутри SQL — sqlite-специфика. Брать только литералы с
    # ключевым словом нельзя: длинный запрос в коде разрезан на несколько
    # строк, и хвост `"VALUES (...)"` тогда не проверялся бы вовсе — на этом
    # первая версия сторожа и попалась (подмешанный `ON CONFLICT DO NOTHING`
    # прошёл мимо).
    sql_text = " ".join(re.findall(r'"([^"\n]*)"', ledger_src))

    forbidden = [
        "AUTOINCREMENT",          # только SQLite
        "SERIAL",                 # только PostgreSQL
        "ON CONFLICT",            # синтаксис расходится, и маскирует конфликт
        "INSERT OR IGNORE",       # только SQLite, и маскирует конфликт
        "INSERT OR REPLACE",
        "ON DUPLICATE KEY",
        "RETURNING",              # в SQLite появился только с 3.35
        "CURRENT_TIMESTAMP",      # разный формат и зона
        "PRAGMA",
        "now()",
        "datetime(",
        "strftime(",              # в SQL — sqlite-специфично (в Python можно)
    ]
    upper = sql_text.upper()
    hits = [f for f in forbidden if f.upper() in upper]
    check("в SQL журнала нет диалект-специфичных конструкций",
          not hits and sql_text, f"найдено: {hits}; sql={sql_text[:200]}")
    check("DDL журнала использует только переносимые типы",
          all(("VARCHAR" in q or "INTEGER" in q) for q in sql_sources[:1]),
          f"ddl={sql_sources[0]}")
    check("таблица и индекс журнала создаются идемпотентно (IF NOT EXISTS)",
          all("IF NOT EXISTS" in q for q in sql_sources), f"sql={sql_sources}")
    check("applied_at подставляется из Python, а не из SQL-функции времени",
          "time.strftime(" in ledger_src, "")


def main() -> int:
    print("\n== Реальный порядок старта: миграции/схемы ДО планировщика ==")
    check_order()

    print("\n== Инъекция сбоя старта — планировщик не должен стартовать ==")
    check_failure_prevents_scheduler_start()

    print("\n== Импорт модулей без ASGI-цикла не мутирует схему lessons ==")
    check_import_has_no_schema_side_effect()

    print("\n== Повторный shutdown безопасен ==")
    check_repeated_shutdown_safe()

    print("\n== OPS-5: журнал шагов старта на чистой базе ==")
    check_ledger_clean_db()

    print("\n== OPS-5: база прежней схемы и база без журнала ==")
    check_ledger_legacy_db()

    print("\n== OPS-5: повторный старт идемпотентен и шагов не пропускает ==")
    check_ledger_repeat_startup()

    print("\n== OPS-5: упавший шаг записи в журнал не получает ==")
    check_ledger_not_recorded_on_failure()

    print("\n== SUPPLY-1: собственная строка журнала, отдельная от init_db ==")
    check_ledger_supply_step_is_distinct()

    print("\n== OPS-5: конфликт порядка валит старт (fail closed) ==")
    check_ledger_conflict_fails_closed()

    print("\n== OPS-5: конфликтный шаг не успевает тронуть базу ==")
    check_ledger_conflict_blocks_mutation()

    print("\n== OPS-5: контракт помощника журнала и ограниченная гонка ==")
    check_ledger_helper_contract()

    print("\n== OPS-5: фактический порядок вызовов привязан к объявлению ==")
    check_startup_order_is_enforced_at_runtime()

    print("\n== OPS-5: переносимость SQL журнала (по конструкции) ==")
    check_ledger_sql_is_portable()

    print(f"\nИТОГО: {len(PASS)} OK, {len(FAIL)} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
