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
import json
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


def _order_count() -> int:
    """Сколько строк заказов в базе — чтобы отказ входа был виден как «не сохранён»."""
    con = sqlite3.connect(DB_PATH)
    try:
        return con.execute("SELECT COUNT(*) FROM production_orders").fetchone()[0]
    finally:
        con.close()


def _order_href(order_id: int) -> str | None:
    con = sqlite3.connect(DB_PATH)
    try:
        row = con.execute("SELECT ms_doc_href FROM production_orders WHERE id = ?",
                          (order_id,)).fetchone()
        return row[0] if row else None
    finally:
        con.close()


def _set_items_json(order_id: int, items: list[dict]) -> None:
    """Вписать состав заказа МИМО API — так расхождение и попадало в базу раньше.

    Нужно ровно для одного: смоделировать УЖЕ СОХРАНЁННЫЙ заказ, который вход
    API сегодня не принял бы. Настоящие старые записи этим тестом не трогаются
    и не мигрируются — проверяется только то, что наружу они не уезжают.
    """
    con = sqlite3.connect(DB_PATH, timeout=30)
    try:
        con.execute("UPDATE production_orders SET items_json = ? WHERE id = ?",
                    (json.dumps(items, ensure_ascii=False), order_id))
        con.commit()
    finally:
        con.close()


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
    # Описание документа пишет посторонний человек. Число из 4301 цифры
    # int() разобрать отказывается — без ограничения длины это уронило бы
    # весь синк организации, а не одну строку.
    check("абсурдно длинный номер в метке не роняет синк",
          _is_oborot_doc({"description": "x [oborot#" + "1" * 4301 + "]",
                          "meta": {"href": href_ok}}, ours) is False)
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
    tee0 = ordered_map().get("Футболка «Манифест»", (0, 0))
    r = client.post(f"/api/orders/{order2}/status", json={"status": "sent"})
    check("заказ переведён в sent (push доступен и для sent)",
          r.status_code == 200 and r.json().get("status") == "sent")
    hood_sent = ordered_map().get("Худи «Скетч»", (0, 0))
    tee_sent = ordered_map().get("Футболка «Манифест»", (0, 0))
    check("sent непушенного заказа прибавил qty (+3)",
          hood_sent[0] == hood0[0] + 3 and hood_sent[1] == hood0[1],
          f"{hood0} -> {hood_sent}")
    check("sent прибавил qty и несопоставленной пока позиции (+3)",
          tee_sent[0] == tee0[0] + 3 and tee_sent[1] == tee0[1],
          f"{tee0} -> {tee_sent}")
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
    check("push sent-заказа: сопоставленный base_name — вклад переехал из qty в ms_qty (дедуп)",
          hood_pushed[0] == hood0[0] and hood_pushed[1] == hood0[1] + 3,
          f"{hood_sent} -> {hood_pushed}")
    tee_pushed = ordered_map().get("Футболка «Манифест»", (0, 0))
    # DATA-7: base_name БЕЗ единого сопоставления в документ не попал вообще
    # (pushed_by_base для него пуст) — снимать из qty нечего, весь вклад
    # обязан остаться локальным, а ms_qty не должен вырасти ни на штуку.
    # Старый код вычитал здесь item["qty"]=3 безусловно и обнулял «едет».
    check("DATA-7: несопоставленный base_name НЕ теряет вклад при push "
          "(qty остаётся, ms_qty не растёт)",
          tee_pushed[0] == tee_sent[0] and tee_pushed[1] == tee_sent[1],
          f"{tee_sent} -> {tee_pushed} (ожидали без изменений)")

    print("== Частичное сопоставление размеров ВНУТРИ одного base_name (DATA-7) ==")
    # «Худи «Штрих»» — тоже sized-товар с вариантами S/M/L в mock-МойСкладе.
    # XL среди них нет: 2 позиции этого base_name сопоставятся (S,M=3 шт),
    # одна (XL=4 шт) — нет. All-or-nothing per base_name замаскировал бы это:
    # тут в отличие от Футболки выше сопоставляется ЧАСТЬ размеров одного
    # и того же товара, а не товар целиком.
    r = client.post("/api/orders", json={
        "name": "Частичные размеры", "eta_date": None, "items": [
            {"base_name": "Худи «Штрих»", "qty": 7,
             "sizes": {"S": 2, "M": 1, "XL": 4}, "cost": 3600},
        ],
    })
    order2b = r.json()["id"]
    strih0 = ordered_map().get("Худи «Штрих»", (0, 0))
    r = client.post(f"/api/orders/{order2b}/status", json={"status": "sent"})
    check("заказ с частичным размерным рядом переведён в sent",
          r.status_code == 200 and r.json().get("status") == "sent")
    strih_sent = ordered_map().get("Худи «Штрих»", (0, 0))
    check("sent прибавил ПОЛНОЕ количество заказа (+7, все размеры)",
          strih_sent[0] == strih0[0] + 7 and strih_sent[1] == strih0[1],
          f"{strih0} -> {strih_sent}")
    r = client.post(f"/api/orders/{order2b}/push-to-ms")
    d = r.json()
    check("push с частичным размерным рядом → 200", r.status_code == 200 and d.get("ok"),
          f"status={r.status_code} body={r.text[:200]}")
    check("несопоставленный размер (XL) в unmatched, сопоставленные — нет",
          d.get("unmatched") == ["Худи «Штрих» (XL)"], f"unmatched={d.get('unmatched')}")
    check("в документ попали только 2 сопоставленных размера (S, M)",
          d.get("positions_pushed") == 2, f"positions_pushed={d.get('positions_pushed')}")
    strih_pushed = ordered_map().get("Худи «Штрих»", (0, 0))
    # Точная арифметика, а не all-or-nothing: снято РОВНО 3 (S+M), а не 7
    # (весь заказ) и не 0 (полный отказ от вычитания). 4 шт непринятого XL
    # обязаны остаться в qty — в МойСкладе документа на них нет.
    check("DATA-7: снята РОВНО сопоставленная часть (3), несопоставленные "
          "4 шт (XL) остались в qty",
          strih_pushed[0] == strih_sent[0] - 3 and strih_pushed[1] == strih_sent[1] + 3,
          f"{strih_sent} -> {strih_pushed} (ожидали qty-3, ms_qty+3)")

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
    check("документ при 422 не создан", len(mock_ms.CREATED_PURCHASE_ORDERS) == 3)

    print("== Статус received: снятие unmatched-остатка (DATA-7 Package A) ==")
    # order2 («Частичный заказ»): «Худи «Скетч»» сопоставлен целиком — его
    # вклад уже переехал из qty в ms_qty на push, receive его трогать не
    # должен. «Футболка «Манифест» (XL)» НЕ сопоставлена вовсе (pushed_by_base
    # для неё пуст) — весь вклад (3) остался в qty, и receive обязан снять
    # РОВНО его: не 0 (иначе остаток навсегда зависает в «едет к нам») и не
    # qty целиком (задвоило бы уже перенесённую часть — здесь её и нет).
    tee_before_recv = ordered_map().get("Футболка «Манифест»", (0, 0))
    hood_before_recv = ordered_map().get("Худи «Скетч»", (0, 0))
    r = client.post(f"/api/orders/{order2}/status", json={"status": "received"})
    check("заказ принят на склад", r.status_code == 200, f"status={r.status_code}")
    tee_after_recv = ordered_map().get("Футболка «Манифест»", (0, 0))
    check("DATA-7: receive снял РОВНО несопоставленный остаток (3)",
          tee_after_recv[0] == tee_before_recv[0] - 3
          and tee_after_recv[1] == tee_before_recv[1],
          f"{tee_before_recv} -> {tee_after_recv} (ждали qty-3, ms_qty без изменений)")
    check("…сопоставленную позицию того же заказа (Худи «Скетч») receive не тронул "
          "(её вклад давно переехал в ms_qty при push)",
          ordered_map().get("Худи «Скетч»", (0, 0)) == hood_before_recv,
          f"{hood_before_recv} -> {ordered_map().get('Худи «Скетч»')}")

    print("== Повторный received — идемпотентность ==")
    om_recv_repeat0 = ordered_map()
    r = client.post(f"/api/orders/{order2}/status", json={"status": "received"})
    check("повторный received того же заказа отвечает unchanged",
          r.status_code == 200 and r.json().get("unchanged") is True,
          f"status={r.status_code} body={r.text[:150]}")
    check("…и НЕ снимает остаток второй раз",
          ordered_map() == om_recv_repeat0, "ordered_map изменился на повторе")

    print("== Статус received: частичный размерный ряд (order2b, DATA-7) ==")
    # order2b («Частичные размеры»): «Худи «Штрих»» S+M=3 шт сопоставлены и
    # уже в ms_qty, XL=4 шт остались в qty как unmatched. receive обязан
    # снять РОВНО эти 4, а не все 7 (задвоило бы matched-часть) и не 0.
    strih_before_recv = ordered_map().get("Худи «Штрих»", (0, 0))
    r = client.post(f"/api/orders/{order2b}/status", json={"status": "received"})
    check("частично сопоставленный заказ принят на склад", r.status_code == 200)
    strih_after_recv = ordered_map().get("Худи «Штрих»", (0, 0))
    check("DATA-7: receive снял РОВНО unmatched-остаток (4), не все 7 и не 0",
          strih_after_recv[0] == strih_before_recv[0] - 4
          and strih_after_recv[1] == strih_before_recv[1],
          f"{strih_before_recv} -> {strih_after_recv} (ждали qty-4, ms_qty без изменений)")

    r = client.post(f"/api/orders/{order2}/push-to-ms")
    check("push received-заказа → 422", r.status_code == 422,
          f"status={r.status_code} body={r.text[:120]}")

    print("== Дубль имени: агрегированный remainder, не построчный (DATA-7) ==")
    # Две строки заказа с ОДНИМ base_name (D-25 «дубль имени»): первая (S=2)
    # сопоставляется, вторая (XL=2) — нет. push агрегирует pushed_by_base по
    # base_name (см. ms_writeback.push_order: `pushed_by_base[base] += qty`),
    # а не по строке — remainder обязан считаться так же: СНАЧАЛА суммарный
    # заказанный qty по base_name (2+2=4), ПОТОМ вычесть агрегированный
    # pushed (2) ОДИН раз. Старый построчный код вычитал agg-pushed (2) из
    # КАЖДОЙ строки (2-2=0 и 2-2=0) и терял остаток (2 шт) насовсем.
    r = client.post("/api/orders", json={
        "name": "Дубль имени — remainder (DATA-7)", "eta_date": None, "items": [
            {"base_name": "Худи «Скетч»", "qty": 2, "sizes": {"S": 2}, "cost": 3900},
            {"base_name": "Худи «Скетч»", "qty": 2, "sizes": {"XL": 2}, "cost": 3900},
        ],
    })
    order_dup = r.json()["id"]
    check("заказ-дубль создан", r.status_code == 200 and order_dup, r.text[:150])
    dup0 = ordered_map().get("Худи «Скетч»", (0, 0))
    r = client.post(f"/api/orders/{order_dup}/status", json={"status": "sent"})
    check("заказ-дубль переведён в sent", r.status_code == 200)
    dup_sent = ordered_map().get("Худи «Скетч»", (0, 0))
    check("sent прибавил ПОЛНОЕ количество обеих строк (2+2=4)",
          dup_sent[0] == dup0[0] + 4 and dup_sent[1] == dup0[1],
          f"{dup0} -> {dup_sent}")
    r = client.post(f"/api/orders/{order_dup}/push-to-ms")
    d = r.json()
    check("push заказа-дубля → 200", r.status_code == 200 and d.get("ok"),
          f"status={r.status_code} body={r.text[:200]}")
    check("несопоставленный размер второй строки (XL) в unmatched",
          d.get("unmatched") == ["Худи «Скетч» (XL)"], f"unmatched={d.get('unmatched')}")
    dup_pushed = ordered_map().get("Худи «Скетч»", (0, 0))
    check("push перенёс в ms_qty только сопоставленную часть первой строки (2)",
          dup_pushed[0] == dup_sent[0] - 2 and dup_pushed[1] == dup_sent[1] + 2,
          f"{dup_sent} -> {dup_pushed}")
    dup_before_recv = ordered_map().get("Худи «Скетч»", (0, 0))
    r = client.post(f"/api/orders/{order_dup}/status", json={"status": "received"})
    check("заказ-дубль принят на склад", r.status_code == 200)
    dup_after_recv = ordered_map().get("Худи «Скетч»", (0, 0))
    check("DATA-7: агрегированный remainder = 4 (заказано) − 2 (pushed) = 2, "
          "не 0 (как дал бы построчный вычет)",
          dup_after_recv[0] == dup_before_recv[0] - 2
          and dup_after_recv[1] == dup_before_recv[1],
          f"{dup_before_recv} -> {dup_after_recv} (ждали qty-2, ms_qty без изменений)")

    print("== Удаление после частичного push снимает ровно remainder (DATA-7) ==")
    r = client.post("/api/orders", json={
        "name": "Удаление после частичного push (DATA-7)", "eta_date": None, "items": [
            {"base_name": "Худи «Скетч»", "qty": 5, "sizes": {"M": 2, "XL": 3}, "cost": 3900},
        ],
    })
    order_del = r.json()["id"]
    check("заказ на удаление создан", r.status_code == 200 and order_del, r.text[:150])
    r = client.post(f"/api/orders/{order_del}/status", json={"status": "sent"})
    check("заказ на удаление переведён в sent", r.status_code == 200)
    r = client.post(f"/api/orders/{order_del}/push-to-ms")
    d = r.json()
    check("push заказа на удаление → 200", r.status_code == 200 and d.get("ok"),
          f"status={r.status_code} body={r.text[:200]}")
    check("несопоставленный XL в unmatched",
          d.get("unmatched") == ["Худи «Скетч» (XL)"], f"unmatched={d.get('unmatched')}")
    del_before = ordered_map().get("Худи «Скетч»", (0, 0))
    r = client.delete(f"/api/orders/{order_del}")
    check("удаление отправленного частично-сопоставленного заказа → 200",
          r.status_code == 200, f"status={r.status_code} body={r.text[:150]}")
    del_after = ordered_map().get("Худи «Скетч»", (0, 0))
    check("DATA-7: удаление сняло РОВНО unmatched-остаток (3), не все 5 и не 0",
          del_after[0] == del_before[0] - 3 and del_after[1] == del_before[1],
          f"{del_before} -> {del_after} (ждали qty-3, ms_qty без изменений)")

    print("== DATA-7 corrective: пушенный черновик с частичным сопоставлением, "
          "draft→sent добавляет remainder ==")
    # «Рубашка «Разворот»» нигде выше в этом файле не используется — свежий
    # base_name, чтобы не путать вклад с уже накопленным состоянием других
    # секций теста.
    #
    # Сначала — НЕСВЯЗАННЫЙ заказ на тот же base_name (обычный sent, без
    # push). Его вклад в qty обязан пережить весь сценарий ниже: движение
    # remainder ОДНОГО заказа не должно трогать чужой вклад по тому же
    # base_name — OrderedQty агрегирован по base_name, а не по заказу.
    r = client.post("/api/orders", json={
        "name": "Другой заказ на той же базе (DATA-7 isolation)", "eta_date": None,
        "items": [{"base_name": "Рубашка «Разворот»", "qty": 2, "sizes": {"S": 2},
                   "cost": 2800}],
    })
    order_other = r.json()["id"]
    check("несвязанный заказ создан", r.status_code == 200 and order_other, r.text[:150])
    r = client.post(f"/api/orders/{order_other}/status", json={"status": "sent"})
    check("несвязанный заказ переведён в sent", r.status_code == 200)
    other_contrib = ordered_map().get("Рубашка «Разворот»", (0, 0))
    check("несвязанный заказ дал вклад +2 в qty", other_contrib[0] >= 2, f"{other_contrib}")

    # Черновик с частичным сопоставлением (M есть среди синканых вариантов,
    # XL — нет). Пушим его СРАЗУ, пока он ещё draft (push доступен и без
    # перевода в sent — см. ms_writeback.PUSHABLE_STATUSES) — это и есть
    # сценарий бага: matched-часть уедет в ms_qty ДО первого прихода заказа
    # в «едет к нам».
    r = client.post("/api/orders", json={
        "name": "Черновик с частичным push (DATA-7 corrective)", "eta_date": None,
        "items": [{"base_name": "Рубашка «Разворот»", "qty": 5,
                   "sizes": {"M": 2, "XL": 3}, "cost": 2800}],
    })
    order_pd = r.json()["id"]
    check("черновик для частичного push создан", r.status_code == 200 and order_pd,
          r.text[:150])
    before_push = ordered_map().get("Рубашка «Разворот»", (0, 0))
    check("черновик перед push не тронул qty/ms_qty (заказ ещё draft)",
          before_push == other_contrib, f"{other_contrib} -> {before_push}")

    r = client.post(f"/api/orders/{order_pd}/push-to-ms")
    d = r.json()
    check("push черновика с частичным сопоставлением → 200",
          r.status_code == 200 and d.get("ok"), f"status={r.status_code} body={r.text[:200]}")
    check("несопоставленный XL в unmatched",
          d.get("unmatched") == ["Рубашка «Разворот» (XL)"], f"unmatched={d.get('unmatched')}")
    after_push = ordered_map().get("Рубашка «Разворот»", (0, 0))
    check("push черновика: matched-часть (2) сразу в ms_qty, локальный qty НЕ тронут "
          "(draft ещё ничего не вносил в «едет»)",
          after_push[0] == before_push[0] and after_push[1] == before_push[1] + 2,
          f"{before_push} -> {after_push} (ждали qty без изменений, ms_qty+2)")

    r = client.post(f"/api/orders/{order_pd}/status", json={"status": "sent"})
    check("пушенный черновик переведён в sent",
          r.status_code == 200 and r.json().get("status") == "sent",
          f"status={r.status_code} body={r.text[:150]}")
    after_sent = ordered_map().get("Рубашка «Разворот»", (0, 0))
    check("DATA-7 corrective: draft→sent добавил РОВНО unmatched-остаток (5-2=3), "
          "не 0 (старый баг — remainder терялся навсегда) и не 5 (задвоило бы "
          "уже перенесённую в ms_qty часть)",
          after_sent[0] == after_push[0] + 3 and after_sent[1] == after_push[1],
          f"{after_push} -> {after_sent} (ждали qty+3, ms_qty без изменений)")
    check("чужой вклад по тому же base_name не пострадал (delta ровно +3)",
          after_sent[0] - other_contrib[0] == 3,
          f"other={other_contrib} after_sent={after_sent}")

    print("== Идемпотентность: повтор draft→sent не задваивает remainder ==")
    r = client.post(f"/api/orders/{order_pd}/status", json={"status": "sent"})
    check("повторный sent того же заказа отвечает unchanged",
          r.status_code == 200 and r.json().get("unchanged") is True,
          f"status={r.status_code} body={r.text[:150]}")
    check("…и НЕ прибавляет remainder второй раз",
          ordered_map().get("Рубашка «Разворот»") == after_sent,
          "ordered_map изменился на повторе")

    print("== sent→received снимает тот же remainder ровно один раз ==")
    r = client.post(f"/api/orders/{order_pd}/status", json={"status": "received"})
    check("пушенный частично сопоставленный заказ принят на склад", r.status_code == 200)
    after_recv = ordered_map().get("Рубашка «Разворот»", (0, 0))
    check("DATA-7 corrective: received снял РОВНО тот же remainder (3) — qty "
          "вернулся к уровню сразу после push, ms_qty не изменился",
          after_recv == after_push,
          f"{after_sent} -> {after_recv} (ждали {after_push})")
    check("чужой вклад по тому же base_name пережил весь цикл push→sent→received "
          "без единого изменения",
          after_recv[0] == other_contrib[0],
          f"other={other_contrib} after_recv={after_recv}")

    print("== Идемпотентность: повтор received не снимает remainder второй раз ==")
    r = client.post(f"/api/orders/{order_pd}/status", json={"status": "received"})
    check("повторный received отвечает unchanged",
          r.status_code == 200 and r.json().get("unchanged") is True,
          f"status={r.status_code} body={r.text[:150]}")
    check("…и НЕ снимает остаток второй раз",
          ordered_map().get("Рубашка «Разворот»") == after_recv,
          "ordered_map изменился на повторе")

    print("== Legacy без маркера (до DATA-7): no-guess, remainder не трогаем ==")
    # Заказ отправлялся до появления маркера pushed_by_base — items_json
    # остаётся ГОЛЫМ списком (в проде такой ряд возник бы до этой фичи; здесь
    # он смоделирован прямой записью ms_doc_href мимо push, items_json при
    # этом никто не трогал и marker в нём никогда не было). Какая часть
    # реально уехала — неизвестно, и receive НЕ ИМЕЕТ ПРАВА гадать: qty/ms_qty
    # обязаны остаться как есть, а не «предположить всё» или «предположить 0».
    r = client.post("/api/orders", json={
        "name": "Legacy без маркера (DATA-7 no-guess)", "eta_date": None, "items": [
            {"base_name": "Худи «Скетч»", "qty": 4, "sizes": {"S": 4}, "cost": 3900},
        ],
    })
    order_legacy = r.json()["id"]
    check("legacy-заказ создан", r.status_code == 200 and order_legacy, r.text[:150])
    r = client.post(f"/api/orders/{order_legacy}/status", json={"status": "sent"})
    check("legacy-заказ переведён в sent", r.status_code == 200)
    con = sqlite3.connect(DB_PATH)
    try:
        con.execute(
            "UPDATE production_orders SET ms_doc_href=?, ms_lookup_mode='sync' "
            "WHERE id=?",
            ("http://127.0.0.1:9800/entity/purchaseorder/legacy-po-no-marker",
             order_legacy))
        con.commit()
    finally:
        con.close()
    legacy_before_recv = ordered_map().get("Худи «Скетч»", (0, 0))
    r = client.post(f"/api/orders/{order_legacy}/status", json={"status": "received"})
    check("legacy-заказ (без маркера) принят на склад", r.status_code == 200)
    legacy_after_recv = ordered_map().get("Худи «Скетч»", (0, 0))
    check("DATA-7 no-guess: legacy без маркера — receive НЕ трогает qty/ms_qty",
          legacy_after_recv == legacy_before_recv,
          f"{legacy_before_recv} -> {legacy_after_recv}")

    print("== Переименование переносит маркер pushed_by_base (DATA-7 rename) ==")
    # После push «Худи «Скетч»» несёт маркер {"Худи «Скетч»": 2} (см. дубль
    # выше — но берём отдельный свежий заказ, чтобы не путать с уже принятым
    # order_dup). Переименование товара обязано перенести И base_name внутри
    # items, И ключ в маркере — иначе remainder на будущем receive считался
    # бы по имени, которого в заказе больше нет, и вся сопоставленная часть
    # ошибочно превратилась бы в «unmatched» (задвоение).
    r = client.post("/api/orders", json={
        "name": "На переименование (DATA-7 rename)", "eta_date": None, "items": [
            {"base_name": "Худи «Скетч»", "qty": 3, "sizes": {"S": 1, "M": 1, "XL": 1},
             "cost": 3900},
        ],
    })
    order_ren = r.json()["id"]
    check("заказ на переименование создан", r.status_code == 200 and order_ren,
          r.text[:150])
    r = client.post(f"/api/orders/{order_ren}/status", json={"status": "sent"})
    check("заказ на переименование переведён в sent", r.status_code == 200)
    r = client.post(f"/api/orders/{order_ren}/push-to-ms")
    d = r.json()
    check("push заказа на переименование → 200", r.status_code == 200 and d.get("ok"),
          f"status={r.status_code} body={r.text[:200]}")
    check("несопоставленный XL в unmatched (перед переименованием)",
          d.get("unmatched") == ["Худи «Скетч» (XL)"], f"unmatched={d.get('unmatched')}")

    from app import ms_sync as _ms
    from app.db import SessionLocal as _SL
    con = sqlite3.connect(DB_PATH)
    try:
        con.execute(
            "UPDATE products SET base_name=? WHERE org_id=1 AND base_name=?",
            ("Худи «Скетч» v2", "Худи «Скетч»"))
        con.commit()
    finally:
        con.close()
    dbx = _SL()
    try:
        _ms._migrate_renames(dbx, 1, {"Худи «Скетч»": {"Худи «Скетч» v2"}}, {})
        dbx.commit()
    finally:
        dbx.close()
    con = sqlite3.connect(DB_PATH)
    try:
        items_json_ren = con.execute(
            "SELECT items_json FROM production_orders WHERE id=?",
            (order_ren,)).fetchone()[0]
    finally:
        con.close()
    from app.models import parse_items_payload as _parse_items_payload
    items_ren, pushed_ren = _parse_items_payload(items_json_ren)
    check("rename перенёс base_name внутри items заказа",
          all(it.get("base_name") == "Худи «Скетч» v2" for it in items_ren),
          f"items={items_ren}")
    check("rename перенёс КЛЮЧ маркера pushed_by_base на новое имя, значение (2) сохранено",
          pushed_ren == {"Худи «Скетч» v2": 2},
          f"pushed_by_base={pushed_ren}")

    ren_before_recv = ordered_map().get("Худи «Скетч» v2", (0, 0))
    r = client.post(f"/api/orders/{order_ren}/status", json={"status": "received"})
    check("переименованный заказ принят на склад", r.status_code == 200)
    ren_after_recv = ordered_map().get("Худи «Скетч» v2", (0, 0))
    check("DATA-7: remainder под НОВЫМ именем посчитан верно (3 заказано − 2 pushed = 1)",
          ren_after_recv[0] == ren_before_recv[0] - 1
          and ren_after_recv[1] == ren_before_recv[1],
          f"{ren_before_recv} -> {ren_after_recv} (ждали qty-1, ms_qty без изменений)")

    print("== A06: количество против размерной разбивки ==")
    # Дефект, воспроизведённый на BASE ea1caff: строка «qty=10, sizes={M:20}»
    # принималась входом API (размеры проверялись только поштучно), ложилась в
    # заказ как есть, а документ поставщику собирается ИЗ РАЗБИВКИ
    # (_item_size_breakdown) — подрядчику ушло бы 20, а в заказе стояло бы 10.
    #
    # Здесь проверяются ОБА конца: вход больше такую строку не принимает, а уже
    # сохранённый «старый» заказ с таким расхождением не уезжает наружу — и
    # останавливается это ДО первого сетевого вызова, то есть без документа.
    a06_orders_before = _order_count()
    r = client.post("/api/orders", json={
        "name": "A06 расхождение", "eta_date": None, "items": [
            {"base_name": "Худи «Штрих»", "qty": 10, "sizes": {"M": 20}, "cost": 3600},
        ],
    })
    check("вход API отклоняет qty=10 против sizes={M:20} → 422",
          r.status_code == 422, f"status={r.status_code} body={r.text[:200]}")
    check("отказ называет ОБА числа и не подставляет «правильное» сам",
          "10" in r.text and "20" in r.text and "не выбираем" in r.text,
          r.text[:220])
    check("отклонённый заказ не сохранён (ни одной новой строки)",
          _order_count() == a06_orders_before,
          f"{a06_orders_before} -> {_order_count()}")

    # Согласованная строка проходит ровно как раньше — payload не менялся.
    r = client.post("/api/orders", json={
        "name": "A06 согласованный", "eta_date": None, "items": [
            {"base_name": "Худи «Штрих»", "qty": 2, "sizes": {"S": 2}, "cost": 3600},
        ],
    })
    check("согласованный заказ (qty=2, sizes={S:2}) по-прежнему создаётся",
          r.status_code == 200, f"status={r.status_code} body={r.text[:200]}")
    order_a06 = r.json()["id"]

    # Безразмерная позиция — поддержанный контракт, правкой не тронут.
    r = client.post("/api/orders", json={
        "name": "A06 безразмерный", "eta_date": None, "items": [
            {"base_name": "Худи «Штрих»", "qty": 10, "sizes": {}, "cost": 3600},
        ],
    })
    check("безразмерный заказ (qty=10, sizes={}) по-прежнему создаётся",
          r.status_code == 200, f"status={r.status_code} body={r.text[:200]}")
    r = client.post("/api/orders", json={
        "name": "A06 без ключа sizes", "eta_date": None, "items": [
            {"base_name": "Худи «Штрих»", "qty": 10, "cost": 3600},
        ],
    })
    check("заказ без ключа sizes вовсе — тоже создаётся",
          r.status_code == 200, f"status={r.status_code} body={r.text[:200]}")
    # Полностью нулевые размеры разбивкой не считаются: это та же безразмерная
    # позиция, и так её читает _item_size_breakdown — контракт не меняется.
    r = client.post("/api/orders", json={
        "name": "A06 нулевые размеры", "eta_date": None, "items": [
            {"base_name": "Худи «Штрих»", "qty": 10, "sizes": {"M": 0}, "cost": 3600},
        ],
    })
    check("qty=10 при sizes={M:0} — принимается (нулевые размеры это не разбивка)",
          r.status_code == 200, f"status={r.status_code} body={r.text[:200]}")

    # «Старый» заказ: расхождение вписано мимо API, как оно и могло попасть в
    # базу до этой правки. Ничего не мигрируем и не чиним — только не пускаем.
    docs_before_a06 = len(mock_ms.CREATED_PURCHASE_ORDERS)
    _set_items_json(order_a06, [
        {"base_name": "Худи «Штрих»", "qty": 2, "sizes": {"S": 5}, "cost": 3600}])
    r = client.post(f"/api/orders/{order_a06}/push-to-ms")
    check("сохранённый ранее mismatch не уходит в МойСклад → 409",
          r.status_code == 409, f"status={r.status_code} body={r.text[:200]}")
    check("отказ называет позицию и оба числа",
          "Худи «Штрих»" in r.text and "2" in r.text and "5" in r.text, r.text[:250])
    check("документ НЕ создан — отказ встал перед POST создания документа",
          len(mock_ms.CREATED_PURCHASE_ORDERS) == docs_before_a06,
          f"{docs_before_a06} -> {len(mock_ms.CREATED_PURCHASE_ORDERS)}")
    check("пометка отправки снята: заказ не заперт отказом",
          not (_order_href(order_a06) or ""), f"href={_order_href(order_a06)!r}")

    # Проверка стоит на ветке СОЗДАНИЯ документа, а не в начале push_order:
    # до find_own_document неизвестно, создаём мы документ или подбираем уже
    # созданный, и ранний отказ закрыл бы recovered-путь — заказ с реально
    # существующим документом остался бы сиротой навсегда. Что recovered
    # переживает расхождение локального снимка, проверяет DATA-7 Package B
    # (tests/test_writeback_idempotency.py, сценарий 30а).
    #
    # Тот же заказ с исправленной разбивкой уходит штатно: закрыт mismatch,
    # а не механизм отправки.
    _set_items_json(order_a06, [
        {"base_name": "Худи «Штрих»", "qty": 2, "sizes": {"S": 2}, "cost": 3600}])
    r = client.post(f"/api/orders/{order_a06}/push-to-ms")
    check("после исправления разбивки тот же заказ отправляется → 200",
          r.status_code == 200 and r.json().get("ok"),
          f"status={r.status_code} body={r.text[:200]}")
    check("и документ в МойСкладе появился ровно один",
          len(mock_ms.CREATED_PURCHASE_ORDERS) == docs_before_a06 + 1,
          f"{docs_before_a06} -> {len(mock_ms.CREATED_PURCHASE_ORDERS)}")

    print("== Демо-режим и изоляция ==")
    docs_before_demo = len(mock_ms.CREATED_PURCHASE_ORDERS)
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
    check("демо-push не создал документов",
          len(mock_ms.CREATED_PURCHASE_ORDERS) == docs_before_demo,
          f"было={docs_before_demo} стало={len(mock_ms.CREATED_PURCHASE_ORDERS)}")
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
