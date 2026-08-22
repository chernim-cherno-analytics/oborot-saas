# -*- coding: utf-8 -*-
"""Регрессия: полная пересборка не оставляет «один день остатков и год продаж».

Дефект, который закрывает этот тест. Оборачиваемость — главная метрика продукта —
считается как «нетто-выручка за год / дни в стоке», и эти две величины лежат в
РАЗНЫХ таблицах. При полной пересборке истории остатки обнулялись на фазе
«сегодня», а продажи переписывались только на следующей фазе, «окно месяца».
Падение между этими точками (429 от МойСклада, рестарт процесса, OOM) оставляло
организацию с ОДНИМ днём остатков и ПОЛНЫМ годом продаж: знаменатель падал в
365 раз, оборачиваемость вырастала в десятки раз, а сервис при этом оставался
в статусе «работает». Это худший вид дефекта в аналитическом продукте: система
не падает, а уверенно врёт, и заметить это можно только сверкой с источником.

Сценарий:
  1) обычный первичный синк до конца — в базе год истории и продажи;
  2) ломаем загрузку ДОКУМЕНТОВ (429 без конца) и запускаем ПОЛНУЮ пересборку;
  3) синк доходит до фазы «сегодня» (остатки стёрты и записан один день) и
     падает на продажах окна;
  4) проверяем, что продаж не осталось: обе таблицы обнулены одной транзакцией.
     До исправления здесь были сотни строк продаж при одной дате остатков.
  5) точка продолжения на месте — потерянные данные догрузятся автоматически.

Свой мок на отдельном порту (9810), чтобы файл можно было запускать, пока идёт
tests/test_sync.py — тот занимает 9800.

Запуск из корня репозитория:  python tests/test_sync_wipe.py
"""
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

DB_PATH = ROOT / "test_sync_wipe.db"
APP_PORT = 8807
MOCK_PORT = 9810

os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["MS_BASE_URL"] = f"http://127.0.0.1:{MOCK_PORT}"
os.environ["SCHEDULER_ENABLED"] = "0"
os.environ["HISTORY_DAYS"] = "60"
os.environ["INITIAL_WINDOW_DAYS"] = "10"
os.environ["STOCK_CHUNK_DATES"] = "10"
os.environ["MS_CHUNK_PAUSE"] = "0"
os.environ["MS_MAX_RETRIES"] = "2"

if DB_PATH.exists():
    DB_PATH.unlink()

import httpx  # noqa: E402
import uvicorn  # noqa: E402

import mock_ms  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.main import app as oborot_app  # noqa: E402
from app.models import Sale, StockDay  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

mock_ms.PORT = MOCK_PORT


class ServerThread:
    def __init__(self, asgi_app, port: int):
        self.config = uvicorn.Config(asgi_app, host="127.0.0.1", port=port,
                                     log_level="warning")
        self.server = uvicorn.Server(self.config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self):
        self.thread.start()
        deadline = time.time() + 15
        while time.time() < deadline:
            if self.server.started:
                return
            time.sleep(0.05)
        raise RuntimeError(f"сервер на порту {self.config.port} не поднялся")

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=10)


PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        PASS.append(name)
        print(f"  OK   {name}" + (f"  [{detail}]" if detail else ""))
    else:
        FAIL.append(name)
        print(f"  FAIL {name}  {detail}")


def counts() -> tuple[int, int, int]:
    """(дат остатков, строк остатков, строк продаж) — как их видит приложение."""
    db = SessionLocal()
    try:
        dates = db.execute(select(func.count(func.distinct(StockDay.date)))).scalar() or 0
        rows = db.execute(select(func.count()).select_from(StockDay)).scalar() or 0
        sales = db.execute(select(func.count()).select_from(Sale)).scalar() or 0
        return int(dates), int(rows), int(sales)
    finally:
        db.close()


def wait_sync_done(client: httpx.Client, timeout: float = 240.0) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        last = client.get("/api/sync/status").json()
        if last.get("state") in ("done", "error"):
            return last
        time.sleep(1.0)
    return last


def main() -> int:
    mock_srv = ServerThread(mock_ms.app, MOCK_PORT)
    app_srv = ServerThread(oborot_app, APP_PORT)
    mock_srv.start()
    app_srv.start()
    try:
        return run()
    finally:
        app_srv.stop()
        mock_srv.stop()
        for p in (DB_PATH,):
            if p.exists():
                p.unlink()


def run() -> int:
    base = f"http://127.0.0.1:{APP_PORT}"
    c = httpx.Client(headers={"X-Oborot-CSRF": "1"}, base_url=base, timeout=120.0)
    mock_api = httpx.Client(base_url=f"http://127.0.0.1:{MOCK_PORT}", timeout=30.0)

    print("\n== Подготовка: обычный первичный синк ==")
    r = c.post("/register", data={"name": "Владелец", "email": "owner@test.io",
                                  "password": "secret123", "org_name": "Бренд"})
    check("регистрация", r.status_code in (200, 302, 303), f"status={r.status_code}")
    r = c.post("/api/connect/moysklad", json={"token": mock_ms.TOKEN})
    check("токен принят", r.status_code == 200, f"status={r.status_code}")
    c.post("/api/connect/moysklad/stores", json={"ext_ids": ["st-flag", "st-web"]})
    r = c.post("/api/sync/initial")
    check("первичный синк запущен", r.status_code == 200, f"status={r.status_code}")
    st = wait_sync_done(c)
    check("первичный синк дошёл до done", st.get("state") == "done",
          f"state={st.get('state')} error={str(st.get('error'))[:100]}")

    dates0, rows0, sales0 = counts()
    check("в базе накоплена история остатков", dates0 > 5, f"дат={dates0} строк={rows0}")
    check("в базе накоплены продажи", sales0 > 0, f"строк продаж={sales0}")

    print("\n== Полная пересборка падает сразу после стирания истории ==")
    mock_api.post("/__test/faults", json={"docs_429_burst": 100000})
    r = c.post("/api/sync/initial")
    check("полная пересборка запущена", r.status_code == 200, f"status={r.status_code}")
    st = wait_sync_done(c)
    check("пересборка упала на загрузке документов", st.get("state") == "error",
          f"state={st.get('state')} error={str(st.get('error'))[:100]}")

    dates1, rows1, sales1 = counts()
    check("история остатков стёрта — осталось только начало нового окна",
          dates1 < dates0, f"было={dates0} стало={dates1} строк={rows1}")
    check("ПРОДАЖИ СТЁРТЫ ВМЕСТЕ С ОСТАТКАМИ (иначе оборачиваемость врёт в разы)",
          sales1 == 0, f"строк продаж={sales1} при {dates1} дате(ах) остатков "
                       f"(до исправления здесь оставалось ~{sales0})")

    st = c.get("/api/sync/status").json()
    stats = st.get("stats") or {}
    check("точка продолжения на месте — данные догрузятся автоматически",
          bool(stats.get("history_loaded_from")),
          f"from={stats.get('history_loaded_from')} to={stats.get('history_loaded_to')}")

    print("\n== Продолжение восстанавливает данные ==")
    mock_api.post("/__test/faults", json={})
    r = c.post("/api/sync/run")
    check("продолжение запущено", r.status_code == 200, f"status={r.status_code}")
    st = wait_sync_done(c)
    check("продолжение дошло до done", st.get("state") == "done",
          f"state={st.get('state')} error={str(st.get('error'))[:100]}")
    dates2, rows2, sales2 = counts()
    check("история остатков восстановлена", dates2 >= dates0,
          f"было={dates0} стало={dates2}")
    check("продажи восстановлены", sales2 > 0, f"было={sales0} стало={sales2}")

    c.close()
    mock_api.close()
    print(f"\nИТОГО: {len(PASS)} OK, {len(FAIL)} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
