# -*- coding: utf-8 -*-
"""Интеграционный тест обратной записи заказа в МойСклад (без pytest).

Сценарий:
  1) mock-МойСклад (tests/mock_ms.py) на 127.0.0.1:9800 + приложение
     с MS_BASE_URL=mock на 127.0.0.1:8802, чистая БД;
  2) онбординг: токен → выбор складов → первичный синк;
  3) заказ из рекомендаций /api/replenish → POST /api/orders;
  4) POST /api/orders/{id}/push-to-ms → в mock создан purchaseorder:
     контрагент «Производство» создан, organization = юрлицо аккаунта,
     позиции = варианты-размеры с количествами и ценой (себес в копейках);
  5) повторный push → 409 «уже отправлен» со ссылкой;
  6) заказ с несопоставимой позицией → 200 + список unmatched (частично);
  7) заказ целиком из неизвестных позиций → 422;
  8) статус received → 422 (отправлять поздно);
  9) демо-организация без МойСклад → 409 «доступно после подключения»;
 10) изоляция тенантов: чужой заказ → 404.

Запуск из корня репозитория:  python tests/test_writeback.py
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

DB_PATH = ROOT / "test_writeback.db"
APP_PORT = 8802

# Окружение — ДО импорта приложения (db.py и ms_client читают env).
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["MS_BASE_URL"] = "http://127.0.0.1:9800"
os.environ["HISTORY_DAYS"] = "60"
os.environ["SYNC_DAYS_BACK"] = "3"

if DB_PATH.exists():
    DB_PATH.unlink()

import httpx  # noqa: E402
import uvicorn  # noqa: E402

import mock_ms  # noqa: E402
from app.main import app as oborot_app  # noqa: E402


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
    last = {}
    while time.time() < deadline:
        last = client.get("/api/sync/status").json()
        if last.get("state") in ("done", "error"):
            return last
        time.sleep(1.0)
    return last


def ordered_map() -> dict:
    """{base_name: (qty, ms_qty)} из ordered_qty — для проверок «едет к нам»."""
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = {r["base_name"]: (r["qty"], r["ms_qty"]) for r in con.execute(
        "SELECT base_name, qty, ms_qty FROM ordered_qty")}
    con.close()
    return rows


def sku_ext(base: str, size: str) -> str | None:
    """ext_id SKU mock-мира по (base_name, size); None, если нет."""
    for sku in mock_ms.SKUS:
        if sku["base"] == base and sku["size"] == size:
            return sku["ext"]
    return None


def order_payload_from_replenish(items: list[dict], limit: int) -> dict:
    """Payload POST /api/orders как его собирает /replenish (rec по размерам)."""
    out = []
    for it in items[:limit]:
        sizes = {s: v["rec"] for s, v in (it.get("sizes") or {}).items() if v["rec"] > 0}
        if not sizes:
            continue
        out.append({"base_name": it["base_name"], "qty": it["need"],
                    "sizes": sizes, "cost": it.get("cost_price") or 0})
    return {"name": "Тестовый заказ writeback", "eta_date": None, "items": out}


def main() -> int:
    mock_srv = ServerThread(mock_ms.app, mock_ms.PORT)
    app_srv = ServerThread(oborot_app, APP_PORT)
    mock_srv.start()
    app_srv.start()
    try:
        return run_scenario()
    finally:
        app_srv.stop()
        mock_srv.stop()


def run_scenario() -> int:
    mock_ms.reset_writeback_state()
    base = f"http://127.0.0.1:{APP_PORT}"
    client = httpx.Client(headers={"X-Oborot-CSRF": "1"}, base_url=base, timeout=60.0)

    print("== Онбординг и синк ==")
    r = client.post("/register", data={
        "name": "Владелец", "email": "owner@wb.io",
        "password": "secret123", "org_name": "Writeback-бренд",
    })
    check("регистрация владельца", r.status_code == 303, f"status={r.status_code}")
    r = client.post("/api/connect/moysklad", json={"token": mock_ms.TOKEN})
    check("токен принят", r.status_code == 200 and r.json().get("ok"))
    r = client.post("/api/connect/moysklad/stores",
                    json={"ext_ids": ["st-flag", "st-web"]})
    check("выбраны торговые склады", r.status_code == 200 and r.json().get("active") == 2)
    r = client.post("/api/sync/initial")
    check("первичный синк запущен", r.status_code == 200 and r.json().get("ok"))
    status = wait_sync_done(client)
    check("синк завершился state=done", status.get("state") == "done",
          f"state={status.get('state')} error={status.get('error', '')[:120]}")

    print("== Заказ из рекомендаций ==")
    repl = client.get("/api/replenish").json()
    items = repl["items"]
    check("в replenish есть рекомендации", len(items) >= 2, f"n={len(items)}")
    payload = order_payload_from_replenish(items, limit=4)
    n_expected_positions = sum(len(i["sizes"]) for i in payload["items"])
    r = client.post("/api/orders", json=payload)
    check("заказ создан (draft)", r.status_code == 200 and r.json().get("ok"),
          f"resp={r.text[:100]}")
    order_id = r.json()["id"]

    print("== Push в МойСклад ==")
    om0 = ordered_map()
    r = client.post(f"/api/orders/{order_id}/push-to-ms")
    d = r.json()
    check("push вернул 200 ok", r.status_code == 200 and d.get("ok"),
          f"status={r.status_code} body={r.text[:200]}")
    check("номер документа МС присвоен", d.get("ms_doc_name") == "00001",
          f"got={d.get('ms_doc_name')}")
    check("ссылка на веб-интерфейс МС",
          str(d.get("ms_doc_ui_url", "")).startswith(
              "https://online.moysklad.ru/app/#purchaseorder/edit?id="),
          f"got={d.get('ms_doc_ui_url')}")
    check("все позиции сопоставлены (unmatched пуст)", d.get("unmatched") == [],
          f"unmatched={d.get('unmatched')}")
    check("количество позиций в ответе", d.get("positions_pushed") == n_expected_positions,
          f"got={d.get('positions_pushed')} expected={n_expected_positions}")

    check("в mock создан ровно один purchaseorder",
          len(mock_ms.CREATED_PURCHASE_ORDERS) == 1)
    doc = mock_ms.CREATED_PURCHASE_ORDERS[0]
    org_href = doc["organization"]["meta"]["href"]
    check("organization = юрлицо аккаунта", org_href.endswith(mock_ms.ORG_EXT_ID),
          f"href={org_href}")
    cp = next((c for c in mock_ms.COUNTERPARTIES if c["name"] == "Производство"), None)
    check("контрагент «Производство» создан", cp is not None,
          f"counterparties={[c['name'] for c in mock_ms.COUNTERPARTIES]}")
    agent_href = doc["agent"]["meta"]["href"]
    check("agent документа = «Производство»", cp and agent_href.endswith(cp["id"]),
          f"href={agent_href}")

    # Позиции: вариант-размер → количество и цена (себес в копейках).
    expected = {}
    for it in payload["items"]:
        for size, qty in it["sizes"].items():
            ext = sku_ext(it["base_name"], size)
            expected[ext] = (qty, round(it["cost"] * 100))
    got = {}
    for pos in doc["positions"]:
        href = pos["assortment"]["meta"]["href"]
        ext = href.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
        got[ext] = (pos["quantity"], pos["price"])
    check(f"позиции документа = варианты с количествами ({len(expected)} шт)",
          got == expected,
          f"diff={ {k: (got.get(k), expected.get(k)) for k in set(got) ^ set(expected) or set()} }"
          if got != expected else "")

    r = client.get(f"/api/orders/{order_id}/ms-doc")
    check("GET ms-doc отдаёт сохранённую ссылку",
          r.status_code == 200 and r.json().get("ms_doc_href", "").endswith("po-0001"),
          f"resp={r.text[:150]}")

    print("== «Едет к нам» после push (дедуп) ==")
    om1 = ordered_map()
    pushed_by_base = {}
    for it in payload["items"]:
        pushed_by_base[it["base_name"]] = (
            pushed_by_base.get(it["base_name"], 0) + sum(it["sizes"].values()))
    ms_ok = all(
        om1.get(b, (0, 0))[1] - om0.get(b, (0, 0))[1] == q
        for b, q in pushed_by_base.items())
    qty_ok = all(
        om1.get(b, (0, 0))[0] == om0.get(b, (0, 0))[0] for b in pushed_by_base)
    check("push draft: ms_qty вырос на отправленное, локальный qty не тронут",
          ms_ok and qty_ok,
          f"pushed={pushed_by_base} om0={om0} om1={om1}")

    om_before = ordered_map()
    r = client.post(f"/api/orders/{order_id}/status", json={"status": "sent"})
    check("отправленный в МС заказ переводится в sent", r.status_code == 200)
    check("…и НЕ двигает локальный qty (дедуп: его считает ms_qty)",
          ordered_map() == om_before)

    print("== Идемпотентность ==")
    r = client.post(f"/api/orders/{order_id}/push-to-ms")
    body = r.json()
    check("повторный push → 409", r.status_code == 409, f"status={r.status_code}")
    check("текст «уже отправлен» + ссылка",
          "уже отправлен" in str(body.get("detail", "")) and body.get("ms_doc_href"),
          f"body={r.text[:200]}")
    check("повторный push не создал второй документ",
          len(mock_ms.CREATED_PURCHASE_ORDERS) == 1)

    print("== Частичное сопоставление ==")
    r = client.post("/api/orders", json={
        "name": "Частичный заказ", "eta_date": None, "items": [
            {"base_name": "Худи «Скетч»", "qty": 3, "sizes": {"S": 2, "M": 1}, "cost": 3900},
            {"base_name": "Тапки «Фантом»", "qty": 3, "sizes": {"S": 3}, "cost": 100},
        ],
    })
    order2 = r.json()["id"]
    hood0 = ordered_map().get("Худи «Скетч»", (0, 0))
    r = client.post(f"/api/orders/{order2}/status", json={"status": "sent"})
    check("заказ переведён в sent (push доступен и для sent)",
          r.status_code == 200 and r.json().get("status") == "sent")
    hood_sent = ordered_map().get("Худи «Скетч»", (0, 0))
    check("sent непушенного заказа прибавил qty (+3)",
          hood_sent[0] == hood0[0] + 3 and hood_sent[1] == hood0[1],
          f"{hood0} -> {hood_sent}")
    r = client.post(f"/api/orders/{order2}/push-to-ms")
    d = r.json()
    check("push частичного заказа → 200", r.status_code == 200 and d.get("ok"),
          f"status={r.status_code} body={r.text[:200]}")
    check("несопоставленная позиция в unmatched",
          d.get("unmatched") == ["Тапки «Фантом» (S)"], f"unmatched={d.get('unmatched')}")
    check("в документ попали только сопоставленные позиции",
          d.get("positions_pushed") == 2
          and len(mock_ms.CREATED_PURCHASE_ORDERS) == 2
          and len(mock_ms.CREATED_PURCHASE_ORDERS[1]["positions"]) == 2,
          f"pushed={d.get('positions_pushed')}")
    hood_pushed = ordered_map().get("Худи «Скетч»", (0, 0))
    check("push sent-заказа: вклад переехал из qty в ms_qty (дедуп)",
          hood_pushed[0] == hood0[0] and hood_pushed[1] == hood0[1] + 3,
          f"{hood_sent} -> {hood_pushed}")

    print("== Заказ целиком из неизвестных позиций ==")
    r = client.post("/api/orders", json={
        "name": "Мимо ассортимента", "eta_date": None, "items": [
            {"base_name": "Пальто «Мираж»", "qty": 2, "sizes": {"L": 2}, "cost": 100},
        ],
    })
    order3 = r.json()["id"]
    r = client.post(f"/api/orders/{order3}/push-to-ms")
    check("ничего не сопоставилось → 422", r.status_code == 422,
          f"status={r.status_code} body={r.text[:150]}")
    check("документ при 422 не создан", len(mock_ms.CREATED_PURCHASE_ORDERS) == 2)

    print("== Статус received ==")
    om_recv0 = ordered_map()
    r = client.post(f"/api/orders/{order2}/status", json={"status": "received"})
    check("заказ принят на склад", r.status_code == 200)
    check("received пушенного заказа не двигает qty/ms_qty "
          "(принятое снимет приёмка в МС через синк)",
          ordered_map() == om_recv0)
    r = client.post(f"/api/orders/{order2}/push-to-ms")
    check("push received-заказа → 422", r.status_code == 422,
          f"status={r.status_code} body={r.text[:120]}")

    print("== Демо-режим и изоляция ==")
    demo = httpx.Client(headers={"X-Oborot-CSRF": "1"}, base_url=base, timeout=120.0)
    r = demo.post("/register", data={
        "name": "Демо", "email": "demo@wb.io",
        "password": "secret123", "org_name": "Демо-бренд",
    })
    check("регистрация демо-пользователя", r.status_code == 303)
    r = demo.post("/api/connect/demo")
    check("демо-данные посеяны", r.status_code == 200 and r.json().get("ok"))
    drepl = demo.get("/api/replenish").json()
    dpayload = order_payload_from_replenish(drepl["items"], limit=2)
    r = demo.post("/api/orders", json=dpayload)
    demo_order = r.json()["id"]
    r = demo.post(f"/api/orders/{demo_order}/push-to-ms")
    check("демо-организация: 409 «доступно после подключения МойСклад»",
          r.status_code == 409 and "подключения МойСклад" in r.json().get("detail", ""),
          f"status={r.status_code} body={r.text[:160]}")
    check("демо-push не создал документов", len(mock_ms.CREATED_PURCHASE_ORDERS) == 2)
    r = demo.post(f"/api/orders/{order_id}/push-to-ms")
    check("чужой заказ → 404 (изоляция тенантов)", r.status_code == 404,
          f"status={r.status_code}")
    demo.close()
    client.close()

    print()
    print(f"ИТОГО: {len(PASS)} OK, {len(FAIL)} FAIL")
    if FAIL:
        print("Провалены:", *FAIL, sep="\n  - ")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
