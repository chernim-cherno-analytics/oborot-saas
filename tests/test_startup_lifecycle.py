# -*- coding: utf-8 -*-
"""Порядок жизненного цикла приложения: планировщик стартует ПОСЛЕ миграций.

TECH_DEBT OPS-6: «Планировщик стартует до миграций». `scheduler.attach(app)`
вызывался в main.py раньше регистрации `_startup` (init_db + шесть
`ensure_schema`/`reset_stale_running`/`log_preview`), а FastAPI выполняет
`on_event("startup")`-хендлеры строго в порядке регистрации (см.
`fastapi.routing.APIRouter._startup`, `for handler in self.on_startup`).
Значит планировщик реально поднимался ДО того, как гарантированно применены
все аддитивные миграции — молчаливая гонка на холодном старте.

Каждая проверка запускает НАСТОЯЩИЙ ASGI-цикл через `fastapi.testclient.TestClient`
(он сам дёргает startup/shutdown) в отдельном процессе — подмена функций
модулей обязана случиться ДО `import app.main`, потому что
`scheduler.attach()` и `from app.db import init_db` разрешают имена в момент
импорта `app.main`, а не при вызове.

  1) реальный порядок: все семь шагов старта (init_db, exclusions,
     ms_sync.ensure_schema, reset_stale_running, ms_writeback, ms_vendor,
     subscription.ensure_schema, subscription.log_preview) завершаются ДО
     scheduler.start;
  2) инъекция сбоя в любой из шагов старта — планировщик не стартует вообще;
  3) повторный shutdown безопасен (идемпотентен), в т.ч. после ASGI-цикла.

Запуск из корня репозитория: python tests/test_startup_lifecycle.py
"""
import os
import subprocess
import sys
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
    "exclusions.ensure_schema",
    "ms_sync.ensure_schema",
    "ms_sync.reset_stale_running",
    "ms_writeback.ensure_schema",
    "ms_vendor.ensure_schema",
    "subscription.ensure_schema",
    "subscription.log_preview",
]

# Общий пролог дочернего процесса: подменяет семь шагов старта и
# scheduler.start/shutdown ДО импорта app.main (см. докстринг файла — почему
# именно до, а не после).
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

import app.scheduler as scheduler_mod
_orig_start = scheduler_mod.start
def _w_start():
    order.append("scheduler.start")
    return _orig_start()
scheduler_mod.start = _w_start

import app.main as m  # attach() и `from app.db import init_db` резолвятся здесь

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
    """Проверка 1: реальный порядок семи шагов старта и scheduler.start."""
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
    check("зафиксированы все восемь шагов старта",
          set(STEPS + ["scheduler.start"]) <= set(order), f"order={order}")
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
    """Проверка 2: сбой на любом шаге старта — планировщик не стартует."""
    for failing_step in ("init_db", "exclusions.ensure_schema", "ms_vendor.ensure_schema",
                          "subscription.log_preview"):
        db = _fresh_db(f"test_startup_fail_{failing_step.replace('.', '_')}.db")
        # init_db разрешается в app.main через `from app.db import ... init_db`
        # ПРИ ИМПОРТЕ — патчить app.db.init_db ПОСЛЕ import app.main бесполезно,
        # у app.main уже своя копия имени. Патчим саму копию — m.init_db.
        # Остальные три читаются внутри _startup() заново на каждый вызов
        # (`from app import X as _x; _x.метод()`) — патч атрибута субмодуля
        # после импорта app.main их ловит как положено.
        target_var = {
            "init_db": "m.init_db",
            "exclusions.ensure_schema": "_excl.ensure_schema",
            "ms_vendor.ensure_schema": "_msv.ensure_schema",
            "subscription.log_preview": "_sub.log_preview",
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
        if db.exists():
            db.unlink()


def check_repeated_shutdown_safe() -> None:
    """Проверка 3: повторный shutdown идемпотентен и не роняет процесс."""
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


def main() -> int:
    print("\n== Реальный порядок старта: миграции/схемы ДО планировщика ==")
    check_order()

    print("\n== Инъекция сбоя старта — планировщик не должен стартовать ==")
    check_failure_prevents_scheduler_start()

    print("\n== Повторный shutdown безопасен ==")
    check_repeated_shutdown_safe()

    print(f"\nИТОГО: {len(PASS)} OK, {len(FAIL)} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
