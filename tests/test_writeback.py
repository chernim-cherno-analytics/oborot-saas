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
# Порты берутся из окружения: так tests/run_all.py разводит наборы и
# может гонять их параллельно. Значения по умолчанию — прежние.
APP_PORT = int(os.environ.get("OBOROT_TEST_PORT", "8802"))

# Окружение — ДО импорта приложения (db.py и ms_client читают env).
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["MS_BASE_URL"] = f"http://127.0.0.1:{os.environ.get('OBOROT_MOCK_PORT', '9800')}"
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


def tracked_map() -> dict:
    """{base_name: (ms_qty, ms_qty_tracked)} — два потока «едет к нам» (D-28)."""
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = {r["base_name"]: (r["ms_qty"], r["ms_qty_tracked"]) for r in con.execute(
        "SELECT base_name, ms_qty, ms_qty_tracked FROM ordered_qty")}
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

    # ── Два потока: наш заказ отделим от чужих (решение владельца D-28) ──
    # Мок отдаёт и seeded-заказы поставщику (их «Оборот» не создавал), и наш,
    # созданный кнопкой push-to-ms. Синк обязан развести их по доказуемой
    # связи и НЕ приписать себе чужие.
    print("== Два потока «едет к нам»: наш заказ против чужих ==")
    tr_push = tracked_map()
    ours = next(iter(pushed_by_base))
    check("сразу после push наш заказ помечен как свой",
          tr_push.get(ours, (0, 0))[1] >= pushed_by_base[ours],
          f"{ours}: ms_qty_tracked={tr_push.get(ours, (0, 0))[1]} "
          f"отправлено={pushed_by_base[ours]}")

    r = client.post("/api/sync/run")
    check("инкрементальный синк запущен", r.status_code == 200, f"status={r.status_code}")
    st = wait_sync_done(client)
    check("синк завершён", st.get("state") == "done",
          f"state={st.get('state')} error={str(st.get('error'))[:120]}")
    stats = st.get("stats") or {}
    tr = tracked_map()
    check("после синка наш заказ по-прежнему свой",
          tr.get(ours, (0, 0))[1] > 0,
          f"{ours}: ms_qty={tr.get(ours, (0, 0))[0]} tracked={tr.get(ours, (0, 0))[1]}")
    check("tracked никогда не больше общего «едет»",
          all(t <= m + 1e-6 for m, t in tr.values()),
          f"нарушения={[b for b, (m, t) in tr.items() if t > m + 1e-6][:3]}")
    ext_total = stats.get("incoming_qty_external")
    check("чужие заказы посчитаны отдельно и не пусты",
          isinstance(ext_total, (int, float)) and ext_total > 0,
          f"incoming_qty_external={ext_total}")
    check("сумма двух потоков равна общему «едет»",
          (stats.get("incoming_qty_tracked") or 0) + (ext_total or 0)
          == stats.get("incoming_qty"),
          f"tracked={stats.get('incoming_qty_tracked')} external={ext_total} "
          f"total={stats.get('incoming_qty')}")
    check("наших открытых документов ровно один",
          stats.get("incoming_open_docs_tracked") == 1,
          f"got={stats.get('incoming_open_docs_tracked')}")
    # Главная проверка правила: чужой документ со СКОПИРОВАННЫМ маркером
    # (po-seed-6: маркер [oborot#1] есть, заказ №1 существует, но ссылка чужая)
    # не должен считаться нашим. Иначе продукт приписывает себе чужие решения.
    # po-seed-6: чужой документ на 12 шт с маркером НЕСУЩЕСТВУЮЩЕГО заказа
    # [oborot#9091]. У этой же позиции есть и наш вклад, поэтому проверяем не
    # «tracked == 0», а что внешняя часть (ms_qty − tracked) вобрала эти 12.
    tee = tracked_map().get("Футболка «Манифест»", (0, 0))
    check("документ с маркером несуществующего заказа посчитан внешним",
          tee[0] - tee[1] >= 12,
          f"ms_qty={tee[0]} tracked={tee[1]} внешних={tee[0] - tee[1]} (ждали >=12)")

    # Правило принадлежности — прямыми вызовами, все четыре комбинации.
    # Через живой сценарий их не проверить: сид с меткой СУЩЕСТВУЮЩЕГО заказа
    # ломает дедупликацию push-а, которая ищет документ по той же метке.
    from app.ms_sync import _is_oborot_doc
    href_ok = "http://x/entity/purchaseorder/po-0001"
    ours = {1: href_ok}
    check("маркер + наш заказ + совпадающая ссылка → наш",
          _is_oborot_doc({"description": "x [oborot#1]",
                          "meta": {"href": href_ok}}, ours) is True)
    check("маркер есть, ссылка ЧУЖАЯ (копия документа) → не наш",
          _is_oborot_doc({"description": "x [oborot#1]",
                          "meta": {"href": "http://x/entity/purchaseorder/po-9999"}},
                         ours) is False)
    check("маркер есть, заказа с таким id у организации нет → не наш",
          _is_oborot_doc({"description": "x [oborot#42]",
                          "meta": {"href": href_ok}}, ours) is False)
    check("маркера нет, ссылка совпадает → не наш",
          _is_oborot_doc({"description": "обычный заказ",
                          "meta": {"href": href_ok}}, ours) is False)
    check("пустое описание не роняет классификатор",
          _is_oborot_doc({"meta": {"href": href_ok}}, ours) is False)
    # Шаг 0 модели исполнения: диагностика поля shipped доезжает в статус синка.
    check("диагностика shipped посчитана",
          isinstance(stats.get("incoming_positions"), int)
          and stats["incoming_positions"] > 0
          and isinstance(stats.get("incoming_positions_shipped"), int),
          f"positions={stats.get('incoming_positions')} "
          f"shipped={stats.get('incoming_positions_shipped')}")

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
    # Позиция вне каталога организации теперь отсекается при создании заказа
    # (см. api_create_order), поэтому «несопоставленность» здесь моделируем
    # реальным товаром с размером, которого нет среди синканых вариантов
    # (в mock-МойСклад у SIZED-товаров только S/M/L) — сам base_name известен,
    # а вариант «Футболка «Манифест» (XL)» в products нет.
    r = client.post("/api/orders", json={
        "name": "Частичный заказ", "eta_date": None, "items": [
            {"base_name": "Худи «Скетч»", "qty": 3, "sizes": {"S": 2, "M": 1}, "cost": 3900},
            {"base_name": "Футболка «Манифест»", "qty": 3, "sizes": {"XL": 3}, "cost": 100},
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
          d.get("unmatched") == ["Футболка «Манифест» (XL)"], f"unmatched={d.get('unmatched')}")
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
    # Тот же приём: base_name реальный (заказ создастся), а размер — вне
    # синканых вариантов, так что при push не сопоставится вообще ничего.
    r = client.post("/api/orders", json={
        "name": "Мимо ассортимента", "eta_date": None, "items": [
            {"base_name": "Брюки «Чертёж»", "qty": 2, "sizes": {"XXL": 2}, "cost": 100},
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

    print("== Слишком длинный пароль при регистрации (bcrypt: лимит 72 байта) ==")
    # Отдельный клиент: register переустанавливает сессионную куку на нового
    # пользователя — на "client" это увело бы владельца из его же организации.
    pw_client = httpx.Client(headers={"X-Oborot-CSRF": "1"}, base_url=base, timeout=60.0)
    # Реальный кейс: русская фраза 42 символа = 80 байт в UTF-8 — валила
    # bcrypt.hashpw необработанным ValueError (голая страница 500).
    long_pw = "мойоченьнадёжныйпарольдлясервисаоборот2026"
    check("пароль-репродюсер действительно > 72 байт", len(long_pw.encode("utf-8")) > 72,
          f"bytes={len(long_pw.encode('utf-8'))}")
    r = pw_client.post("/register", data={
        "name": "Длинный", "email": "longpw@wb.io",
        "password": long_pw, "org_name": "Длинный пароль",
    })
    check("длинный пароль → аккуратная 200-форма с ошибкой, не 500",
          r.status_code == 200 and "72 байт" in r.text,
          f"status={r.status_code}")
    r = pw_client.post("/register", data={
        "name": "Короткий", "email": "shortpw@wb.io",
        "password": "secret123", "org_name": "Короткий пароль",
    })
    check("обычный пароль после этого по-прежнему регистрирует", r.status_code == 303,
          f"status={r.status_code}")
    pw_client.close()

    print("== Огромный id в пути не валит сервер (ge/le на Path) ==")
    huge = "999999999999999999999"
    r = client.get(f"/api/orders/{huge}")
    check("GET /api/orders/<огромный> → 422, не 500", r.status_code == 422,
          f"status={r.status_code}")
    r = client.get(f"/api/orders/{order_id}")
    check("обычный id по-прежнему работает", r.status_code == 200,
          f"status={r.status_code}")

    print("== Rate-limit логина: 5 попыток/300с, чужой аккаунт не страдает ==")
    lock_client = httpx.Client(headers={"X-Oborot-CSRF": "1"}, base_url=base, timeout=60.0)
    r = lock_client.post("/register", data={
        "name": "Жертва перебора", "email": "bruteforced@wb.io",
        "password": "secret123", "org_name": "Жертва",
    })
    check("регистрация аккаунта для теста rate-limit", r.status_code == 303)
    last = None
    for _ in range(6):
        last = lock_client.post("/login", data={"email": "bruteforced@wb.io", "password": "wrong"})
    check("6-я неудачная попытка подряд → блокировка с точным сроком ожидания",
          last.status_code == 200 and "Слишком много неудачных попыток" in last.text
          and "через 5 минут" in last.text,
          f"status={last.status_code}")
    r = lock_client.post("/login", data={"email": "shortpw@wb.io", "password": "secret123"})
    check("другой аккаунт с того же IP всё ещё может войти", r.status_code == 303,
          f"status={r.status_code}")
    lock_client.close()

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
