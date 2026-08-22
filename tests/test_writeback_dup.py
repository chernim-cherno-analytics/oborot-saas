# -*- coding: utf-8 -*-
"""Регрессия: обрыв связи при отправке заказа не создаёт ВТОРОЙ документ в МС.

Дефект, который закрывает этот тест. Отправка «Заказа поставщику» шла через тот
же механизм повторов, что и чтение: таймаут, обрыв соединения и 5xx повторялись
до десяти раз. Но у создания документа три исхода, а не два — «создан»,
«не создан» и «НЕИЗВЕСТНО»: МойСклад мог документ создать, а ответ до нас не
дошёл. Слепой повтор в этом случае создаёт клиенту второй заказ поставщику —
с деньгами, с обещанием подрядчику и без единого следа у нас. Ключа
идемпотентности у JSON API 1.2 нет.

Решение из двух частей:
  • POST повторяется только на 429 («мы даже не начали» — повтор безопасен),
    но не на таймаутах и 5xx;
  • в описание документа кладётся машинная метка `[oborot#<id>]`, и перед
    созданием мы ищем документ с этой меткой за последние две недели. Нашли —
    значит прошлая попытка на самом деле удалась, просто ответ потерялся.

Проверяется:
  1) документ создан, ответ потерян (502 после создания) → push всё равно
     успешен, документ РОВНО ОДИН, в ответе признак «подобран, а не создан»;
  2) повторная отправка того же заказа → 409, второго документа нет;
  3) сорванный лок (истёк pending) на заказе, документ по которому уже есть,
     → документ подбирается по метке, второго не появляется;
  4) 429 на создании → повтор происходит, документ ровно один;
  5) в описании документа есть метка заказа.

Запуск из корня репозитория:  python tests/test_writeback_dup.py
"""
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

DB_PATH = ROOT / "test_wb_dup.db"
APP_PORT = int(os.environ.get("OBOROT_TEST_PORT", "8809"))
MOCK_PORT = int(os.environ.get("OBOROT_MOCK_PORT", "9812"))

os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["MS_BASE_URL"] = f"http://127.0.0.1:{MOCK_PORT}"
os.environ["SCHEDULER_ENABLED"] = "0"
os.environ["HISTORY_DAYS"] = "30"
os.environ["INITIAL_WINDOW_DAYS"] = "10"
os.environ["STOCK_CHUNK_DATES"] = "10"
os.environ["MS_CHUNK_PAUSE"] = "0"

if DB_PATH.exists():
    DB_PATH.unlink()

import httpx  # noqa: E402
import uvicorn  # noqa: E402

import mock_ms  # noqa: E402
from app.main import app as oborot_app  # noqa: E402
from app.ms_writeback import order_marker  # noqa: E402

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


def exec_sql(query: str, *args) -> None:
    con = sqlite3.connect(DB_PATH)
    try:
        con.execute(query, args)
        con.commit()
    finally:
        con.close()


def wait_sync_done(c: httpx.Client, timeout: float = 240.0) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        last = c.get("/api/sync/status").json()
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
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(DB_PATH) + suffix)
            if p.exists():
                p.unlink()


def make_order(c: httpx.Client, name: str) -> int:
    """Заказ из первой рекомендации /replenish."""
    items = (c.get("/api/replenish").json() or {}).get("items") or []
    payload = []
    for it in items[:2]:
        sizes = {s: v["rec"] for s, v in (it.get("sizes") or {}).items() if v["rec"] > 0}
        if sizes:
            payload.append({"base_name": it["base_name"], "qty": it["need"],
                            "sizes": sizes, "cost": it.get("cost_price") or 0})
        if payload:
            break
    r = c.post("/api/orders", json={"name": name, "eta_date": None,
                                    "items": payload, "allow_duplicate": True})
    assert r.status_code == 200, (name, r.status_code, r.text[:200])
    return int(r.json()["id"])


def run() -> int:
    mock_ms.reset_writeback_state()
    mock_ms.reset_faults()
    base = f"http://127.0.0.1:{APP_PORT}"
    c = httpx.Client(headers={"X-Oborot-CSRF": "1"}, base_url=base, timeout=120.0)
    mock_api = httpx.Client(base_url=f"http://127.0.0.1:{MOCK_PORT}", timeout=30.0)

    print("\n== Подготовка ==")
    r = c.post("/register", data={"name": "Владелец", "email": "owner@dup.io",
                                  "password": "secret123", "org_name": "Дубль-бренд"})
    check("регистрация", r.status_code in (200, 302, 303), f"status={r.status_code}")
    r = c.post("/api/connect/moysklad", json={"token": mock_ms.TOKEN})
    check("токен принят", r.status_code == 200, f"status={r.status_code}")
    c.post("/api/connect/moysklad/stores", json={"ext_ids": ["st-flag", "st-web"]})
    c.post("/api/sync/initial")
    st = wait_sync_done(c)
    check("синк завершился", st.get("state") == "done", f"state={st.get('state')}")

    print("\n== 1. Документ создан, ответ потерян ==")
    order_id = make_order(c, "Заказ с потерянным ответом")
    mock_api.post("/__test/faults", json={"po_create_then_fail": 1})
    r = c.post(f"/api/orders/{order_id}/push-to-ms")
    d = r.json() if r.status_code < 500 else {}
    check("push завершился успешно, а не ошибкой", r.status_code == 200,
          f"status={r.status_code} {r.text[:160]}")
    check("В МОЙСКЛАДЕ РОВНО ОДИН ДОКУМЕНТ (без исправления их было бы несколько)",
          len(mock_ms.CREATED_PURCHASE_ORDERS) == 1,
          f"создано={len(mock_ms.CREATED_PURCHASE_ORDERS)}")
    check("ответ помечен как «документ подобран, а не создан заново»",
          d.get("recovered") is True, f"recovered={d.get('recovered')}")
    check("ссылка на документ сохранена в заказе", bool(d.get("ms_doc_href")),
          f"href={d.get('ms_doc_href')}")

    doc = mock_ms.CREATED_PURCHASE_ORDERS[0]
    check("в описании документа есть метка заказа",
          order_marker(order_id) in str(doc.get("description") or ""),
          f"description={doc.get('description')}")

    print("\n== 2. Повторная отправка того же заказа ==")
    r = c.post(f"/api/orders/{order_id}/push-to-ms")
    check("повторная отправка отклонена (409)", r.status_code == 409,
          f"status={r.status_code}")
    check("второго документа не появилось",
          len(mock_ms.CREATED_PURCHASE_ORDERS) == 1,
          f"создано={len(mock_ms.CREATED_PURCHASE_ORDERS)}")

    print("\n== 3. Сорванный лок: документ уже есть, метка «отправляется» протухла ==")
    # Ровно тот случай, ради которого метка и нужна: ссылка не сохранилась
    # (упал коммит, умер процесс), лок протух, владелец жмёт ещё раз.
    exec_sql("UPDATE production_orders SET ms_doc_href=? WHERE id=?",
             "pending:1", order_id)
    r = c.post(f"/api/orders/{order_id}/push-to-ms")
    check("push прошёл (документ подобран по метке)", r.status_code == 200,
          f"status={r.status_code} {r.text[:160]}")
    check("документ по-прежнему один",
          len(mock_ms.CREATED_PURCHASE_ORDERS) == 1,
          f"создано={len(mock_ms.CREATED_PURCHASE_ORDERS)}")
    check("и он опознан как подобранный",
          (r.json() or {}).get("recovered") is True,
          f"recovered={(r.json() or {}).get('recovered')}")

    print("\n== 4. 429 на создании: повтор безопасен и обязан произойти ==")
    order2 = make_order(c, "Заказ с лимитом частоты")
    mock_api.post("/__test/faults", json={"po_429_burst": 3})
    r = c.post(f"/api/orders/{order2}/push-to-ms")
    check("push пережил три отказа по частоте", r.status_code == 200,
          f"status={r.status_code} {r.text[:160]}")
    check("создан ровно один новый документ (итого два)",
          len(mock_ms.CREATED_PURCHASE_ORDERS) == 2,
          f"создано={len(mock_ms.CREATED_PURCHASE_ORDERS)}")
    check("второй документ — это новый заказ, а не подобранный",
          (r.json() or {}).get("recovered") is False,
          f"recovered={(r.json() or {}).get('recovered')}")
    doc2 = mock_ms.CREATED_PURCHASE_ORDERS[1]
    check("у второго документа своя метка",
          order_marker(order2) in str(doc2.get("description") or ""),
          f"description={doc2.get('description')}")

    mock_api.post("/__test/faults", json={})
    c.close()
    mock_api.close()
    print(f"\nИТОГО: {len(PASS)} OK, {len(FAIL)} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
