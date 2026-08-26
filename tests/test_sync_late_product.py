# -*- coding: utf-8 -*-
"""Регрессия DATA-8 (первый сценарий): товар, СОЗДАННЫЙ в МойСкладе между
чтением ассортимента и отчётом остатков ОДНОГО И ТОГО ЖЕ прогона синка, не
должен теряться на весь прогон.

Дефект, который закрывает этот тест. `_run_sync` читает ассортимент ровно
один раз, в самом начале, и строит по нему `ext_to_pid` (`_upsert_products`).
Остатки читаются позже, тем же прогоном. Если между этими двумя чтениями
товар появился в МойСкладе (его только что создали), его ext_id есть в
отчёте остатков, но НЕТ в `ext_to_pid` — `_fetch_day_stock` не находит
товар, добавляет ext_id в `unmatched` и молча теряет остаток: ни строки в
products, ни в stock_days/warehouse_stock, только счётчик
`stats["stock_unmatched_skus"]`, неотличимый от по-настоящему нераспознанного
SKU.

Правильное поведение: ОДИН ограниченный дополнительный проход. Если после
первого чтения остатков остался unmatched ext_id, синк перечитывает
ассортимент ЕЩЁ РАЗ (сейчас товар уже виден, если он и правда только что
создан), заводит для него товар и перечитывает остатки за те же даты ещё раз
расширенным `ext_to_pid`. Если товар так и не находится (реальная гонка не
разрешилась, а не появление товара) — остаток остаётся исключён, как и
раньше: без фабрикации данных и без второй попытки (fail-closed).

Два сценария на ОДНОЙ организации:
  1) первичный синк (`_run_initial`, ветка "свежий" фазы today) с товаром,
     скрытым РОВНО на первый вызов /entity/assortment — синк обязан завести
     товар и записать его остаток;
  2) следующий инкремент (`_run_incremental`) с ДРУГИМ новым товаром, который
     не находится НИКОГДА (hidden_calls достаточно большой) — синк обязан
     дойти до done, ничего не выдумать и honестно посчитать его в
     stock_unmatched_skus, как и раньше.

Свой мок на отдельном порту (9815), чтобы файл можно было запускать, пока
идут другие наборы.

Запуск из корня репозитория:  python tests/test_sync_late_product.py
"""
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

DB_PATH = ROOT / "test_sync_late_product.db"
APP_PORT = int(os.environ.get("OBOROT_TEST_PORT", "8817"))
MOCK_PORT = int(os.environ.get("OBOROT_MOCK_PORT", "9815"))

os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["MS_BASE_URL"] = f"http://127.0.0.1:{MOCK_PORT}"
os.environ["SCHEDULER_ENABLED"] = "0"
os.environ["HISTORY_DAYS"] = "5"
os.environ["INITIAL_WINDOW_DAYS"] = "3"
os.environ["STOCK_CHUNK_DATES"] = "5"
os.environ["MS_CHUNK_PAUSE"] = "0"
os.environ["MS_MAX_RETRIES"] = "2"

if DB_PATH.exists():
    DB_PATH.unlink()

import httpx  # noqa: E402
import uvicorn  # noqa: E402

import mock_ms  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.main import app as oborot_app  # noqa: E402
from app.models import Org, Product, WarehouseStock  # noqa: E402
from sqlalchemy import select  # noqa: E402

mock_ms.PORT = MOCK_PORT
mock_ms.BASE = f"http://127.0.0.1:{MOCK_PORT}"


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


def wait_sync_done(client: httpx.Client, timeout: float = 240.0) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        last = client.get("/api/sync/status").json()
        if last.get("state") in ("done", "error"):
            return last
        time.sleep(1.0)
    return last


def product_row(org_name: str, ext_id: str) -> Product | None:
    db = SessionLocal()
    try:
        org = db.execute(select(Org).where(Org.name == org_name)).scalar_one_or_none()
        if org is None:
            return None
        return db.execute(
            select(Product).where(Product.org_id == org.id, Product.ext_id == ext_id)
        ).scalar_one_or_none()
    finally:
        db.close()


def warehouse_qty_total(org_name: str, product_id: int) -> float:
    db = SessionLocal()
    try:
        org = db.execute(select(Org).where(Org.name == org_name)).scalar_one_or_none()
        if org is None:
            return 0.0
        rows = db.execute(
            select(WarehouseStock.qty).where(
                WarehouseStock.org_id == org.id, WarehouseStock.product_id == product_id
            )
        ).scalars().all()
        return sum(rows)
    finally:
        db.close()


def register_and_connect(c: httpx.Client, email: str, org_name: str) -> None:
    r = c.post("/register", data={"name": "Владелец", "email": email,
                                  "password": "secret123", "org_name": org_name})
    check(f"регистрация {org_name}", r.status_code in (200, 302, 303),
          f"status={r.status_code}")
    r = c.post("/api/connect/moysklad", json={"token": mock_ms.TOKEN})
    check(f"токен принят {org_name}", r.status_code == 200, f"status={r.status_code}")
    c.post("/api/connect/moysklad/stores", json={"ext_ids": ["st-flag", "st-web"]})


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
    a = httpx.Client(headers={"X-Oborot-CSRF": "1"}, base_url=base, timeout=120.0)
    mock_api = httpx.Client(base_url=f"http://127.0.0.1:{MOCK_PORT}", timeout=30.0)

    print("\n== Первичный синк: товар появился МЕЖДУ ассортиментом и остатками ==")
    mock_api.post("/__test/late_product", json={
        "ext": "p-late1", "name": "Худи «Только что приехало»",
        "price_rub": 5900, "cost_rub": 2200, "qty": 7.0,
        "store": "st-flag", "hidden_calls": 1,
    })
    register_and_connect(a, "owner-a@test.io", "Организация A")

    r = a.post("/api/sync/initial")
    check("первичный синк запущен", r.status_code == 200, f"status={r.status_code}")
    st = wait_sync_done(a)
    check("первичный синк дошёл до done", st.get("state") == "done",
          f"state={st.get('state')} error={str(st.get('error'))[:150]}")

    row = product_row("Организация A", "p-late1")
    check("ТОВАР, СОЗДАННЫЙ МЕЖДУ АССОРТИМЕНТОМ И ОСТАТКАМИ, ЗАВЕДЁН",
          row is not None, f"row={row}")
    if row is not None:
        qty = warehouse_qty_total("Организация A", row.id)
        check("ЕГО ОСТАТОК НЕ ПОТЕРЯН (записан в warehouse_stock)",
              qty == 7.0, f"qty={qty}")

    stats = st.get("stats") or {}
    check("реконсиляция отражена в stats (late_products_recovered)",
          stats.get("late_products_recovered") == 1,
          f"late_products_recovered={stats.get('late_products_recovered')}")
    check("этот товар НЕ попал в stock_unmatched_skus (разрешился)",
          not stats.get("stock_unmatched_skus"),
          f"stock_unmatched_skus={stats.get('stock_unmatched_skus')}")

    print("\n== Инкремент: товар, который так и не находится — fail-closed ==")
    mock_api.post("/__test/late_product", json={
        "ext": "p-late2", "name": "Товар-призрак", "price_rub": 3000,
        "cost_rub": 1000, "qty": 4.0, "store": "st-flag",
        "hidden_calls": 999_999,
    })
    r = a.post("/api/sync/run")
    check("инкремент (неразрешимый поздний товар) запущен", r.status_code == 200,
          f"status={r.status_code}")
    st2 = wait_sync_done(a)
    check("инкремент всё равно доходит до done (без фабрикации и без зависания)",
          st2.get("state") == "done",
          f"state={st2.get('state')} error={str(st2.get('error'))[:150]}")

    row2 = product_row("Организация A", "p-late2")
    check("НЕРАЗРЕШИМЫЙ ТОВАР НЕ ЗАВЕДЁН (fail-closed, без фабрикации)",
          row2 is None, f"row={row2}")
    stats2 = st2.get("stats") or {}
    check("остаток честно посчитан как unmatched, как и раньше",
          stats2.get("stock_unmatched_skus") == 1,
          f"stock_unmatched_skus={stats2.get('stock_unmatched_skus')}")

    a.close()
    mock_api.close()
    print(f"\nИТОГО: {len(PASS)} OK, {len(FAIL)} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
