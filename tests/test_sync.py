# -*- coding: utf-8 -*-
"""Интеграционный тест синхронизации МойСклад (без pytest, просто python).

Сценарий:
  1) поднимаем mock-МойСклад (tests/mock_ms.py) на 127.0.0.1:9800;
  2) поднимаем приложение с MS_BASE_URL=mock и HISTORY_DAYS=60 на 127.0.0.1:8801;
  3) регистрируем владельца, проходим онбординг API-ручками:
     неверный токен (человеческая ошибка) → верный токен → список складов →
     выбор двух торговых → POST /api/sync/initial → поллинг /api/sync/status;
  4) сверяем БД и аналитику с эталонными числами mock-мира:
     товары/размеры, stock_days (включая явные нули у распроданных),
     нетто-продажи по позициям, /api/summary, /api/replenish;
  5) инкрементальный синк POST /api/sync/run — числа не «уезжают»;
  6) демо-режим второго пользователя (POST /api/connect/demo) не сломан.

Запуск из корня репозитория:  python tests/test_sync.py
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

DB_PATH = ROOT / "test_oborot.db"
APP_PORT = 8801

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


# ── Инфраструктура ───────────────────────────────────────────────────────────

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
    base = f"http://127.0.0.1:{APP_PORT}"
    client = httpx.Client(headers={"X-Oborot-CSRF": "1"}, base_url=base, timeout=60.0)

    print("== Онбординг ==")
    r = client.post("/register", data={
        "name": "Владелец", "email": "owner@test.io",
        "password": "secret123", "org_name": "Тестовый бренд",
    })
    check("регистрация владельца", r.status_code == 303, f"status={r.status_code}")

    r = client.post("/api/connect/moysklad", json={"token": "definitely-wrong-token"})
    check("неверный токен отклонён с человеческим текстом",
          r.status_code == 400 and "токен" in r.json().get("detail", "").lower(),
          f"status={r.status_code} detail={r.json().get('detail', '')[:60]}")

    r = client.post("/api/connect/moysklad", json={"token": mock_ms.TOKEN})
    check("верный токен принят", r.status_code == 200 and r.json().get("ok"),
          f"status={r.status_code}")

    r = client.get("/api/connect/moysklad/stores")
    stores = r.json().get("stores", [])
    check("список складов из МС", len(stores) == 3,
          f"got={[s['name'] for s in stores]}")

    r = client.post("/api/connect/moysklad/stores",
                    json={"ext_ids": ["st-flag", "st-web"]})
    check("выбор двух торговых складов",
          r.status_code == 200 and r.json().get("active") == 2
          and r.json().get("total") == 3, f"resp={r.json()}")

    r = client.get("/", follow_redirects=False)
    check("до синка дашборд недоступен (redirect на онбординг)",
          r.status_code == 302 and "/onboarding" in r.headers.get("location", ""))

    t0 = time.time()
    r = client.post("/api/sync/initial")
    check("первичный синк запущен", r.status_code == 200 and r.json().get("ok"),
          f"estimate={r.json().get('estimate_minutes')} мин")

    status = wait_sync_done(client)
    took = time.time() - t0
    check("первичный синк завершился state=done", status.get("state") == "done",
          f"state={status.get('state')} error={status.get('error', '')[:120]}")
    stats = status.get("stats", {})
    print(f"  … синк занял {took:.1f} c, stats={stats}")

    r = client.get("/", follow_redirects=False)
    check("после синка дашборд открывается", r.status_code == 200,
          f"status={r.status_code}")

    print("== Товары и размеры ==")
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    n_products = con.execute("SELECT COUNT(*) c FROM products").fetchone()["c"]
    expected_products = len(mock_ms.SKUS) + len(mock_ms.SIZED)  # + родительские product
    check("создано товаров (варианты + родители + безразмерные)",
          n_products == expected_products,
          f"got={n_products} expected={expected_products}")

    row = con.execute(
        "SELECT base_name, size FROM products WHERE ext_id='v-hoodie1-M'"
    ).fetchone()
    check("вариант: base_name без размера, size из характеристики",
          row and row["base_name"] == "Худи «Скетч»" and row["size"] == "M",
          f"got={dict(row) if row else None}")

    row = con.execute(
        "SELECT base_name, size FROM products WHERE ext_id='v-tee3-L'"
    ).fetchone()
    check("вариант без характеристики: размер распарсен из скобок имени",
          row and row["base_name"] == "Футболка «Полночь»" and row["size"] == "L",
          f"got={dict(row) if row else None}")

    row = con.execute(
        "SELECT sale_price, cost_price, category FROM products WHERE ext_id='p-bag1'"
    ).fetchone()
    check("цены из копеек в рубли + категория из pathName",
          row and row["sale_price"] == 5900 and row["cost_price"] == 2400
          and row["category"] == "Сумки", f"got={dict(row) if row else None}")

    row = con.execute("SELECT archived FROM products WHERE ext_id='p-old1'").fetchone()
    check("архивная позиция помечена archived", row and row["archived"] == 1)

    print("== Исключение расходников/сертификатов из аналитики ==")
    row = con.execute("SELECT excluded FROM products WHERE ext_id='p-pack1'").fetchone()
    check("упаковка авто-исключена (категория «Расходный материал»)",
          row and row["excluded"] == 1)
    row = con.execute("SELECT excluded FROM products WHERE ext_id='p-cert1'").fetchone()
    check("сертификат авто-исключён (слово в названии)", row and row["excluded"] == 1)
    row = con.execute("SELECT excluded FROM products WHERE ext_id='p-bag1'").fetchone()
    check("обычный товар НЕ исключён (эвристика не жадничает)",
          row and row["excluded"] == 0)

    excl_resp = client.get("/api/exclusions").json()
    excl_names = {e["base_name"] for e in excl_resp.get("excluded", [])}
    check("GET /api/exclusions отдаёт оба расходника",
          {"Брендированный пакет средний", "Подарочный сертификат на десять тысяч"} <= excl_names,
          f"got={sorted(excl_names)}")

    turno_bases = {it["base_name"] for it in client.get("/api/turnover").json()["items"]}
    check("исключённые не видны в оборачиваемости",
          "Брендированный пакет средний" not in turno_bases
          and "Подарочный сертификат на десять тысяч" not in turno_bases)

    sum_before = client.get("/api/summary").json()
    r = client.post("/api/exclusions",
                    json={"base_name": "Брендированный пакет средний", "excluded": False})
    check("возврат позиции в аналитику работает", r.status_code == 200 and r.json().get("ok"))
    turno_bases2 = {it["base_name"] for it in client.get("/api/turnover").json()["items"]}
    check("возвращённая позиция появилась в оборачиваемости (пакет распродан, nq>0)",
          "Брендированный пакет средний" in turno_bases2)
    # Сертификат в mock-мире имеет живой остаток — проверяем влияние на сток.
    r = client.post("/api/exclusions",
                    json={"base_name": "Подарочный сертификат на десять тысяч", "excluded": False})
    check("возврат сертификата работает", r.status_code == 200 and r.json().get("ok"))
    sum_after = client.get("/api/summary").json()
    check("возврат позиции с остатком увеличил сток",
          sum_after["stock_units"] > sum_before["stock_units"],
          f"units {sum_before['stock_units']} -> {sum_after['stock_units']}")
    for base in ("Брендированный пакет средний", "Подарочный сертификат на десять тысяч"):
        r = client.post("/api/exclusions", json={"base_name": base, "excluded": True})
        check(f"повторное исключение работает ({base[:20]}…)",
              r.status_code == 200 and r.json().get("ok"))
    sum_back = client.get("/api/summary").json()
    check("после исключения числа вернулись",
          sum_back["stock_units"] == sum_before["stock_units"],
          f"{sum_back['stock_units']} != {sum_before['stock_units']}")

    print("== История остатков ==")
    n_stock = con.execute("SELECT COUNT(*) c FROM stock_days").fetchone()["c"]
    n_dates = con.execute("SELECT COUNT(DISTINCT date) c FROM stock_days").fetchone()["c"]
    check("stock_days: 60 дат истории", n_dates == 60, f"dates={n_dates}")
    check("stock_days заполнены", n_stock > 1000, f"rows={n_stock}")

    sellouts = mock_ms.sellout_ext_ids()
    check("в mock-мире есть распроданные позиции", len(sellouts) > 0,
          f"n={len(sellouts)}")
    zero_ok = True
    detail = ""
    for ext in sellouts:
        pid = con.execute("SELECT id FROM products WHERE ext_id=?", (ext,)).fetchone()
        zrow = con.execute(
            "SELECT COUNT(*) c FROM stock_days WHERE product_id=? AND qty=0",
            (pid["id"],)).fetchone()
        lrow = con.execute(
            "SELECT qty FROM stock_days WHERE product_id=? ORDER BY date DESC LIMIT 1",
            (pid["id"],)).fetchone()
        if not zrow["c"] or lrow["qty"] != 0:
            zero_ok = False
            detail = f"{ext}: zeros={zrow['c']} last={lrow['qty']}"
            break
    check("явные нули записаны у распроданных (правило самоизлечения)", zero_ok, detail)

    n_ws = con.execute("SELECT COUNT(*) c FROM warehouse_stock").fetchone()["c"]
    n_wh = con.execute(
        "SELECT COUNT(DISTINCT warehouse_id) c FROM warehouse_stock").fetchone()["c"]
    check("warehouse_stock заполнен по 2 активным складам",
          n_ws > 0 and n_wh == 2, f"rows={n_ws} склады={n_wh}")
    con.close()

    print("== Продажи против эталона mock-мира ==")
    expected = mock_ms.expected_net_sales()
    turno = client.get("/api/turnover").json()["items"]
    by_base = {it["base_name"]: it for it in turno}
    qty_ok = rev_ok = True
    qdet = rdet = ""
    for base_name, (enq, enr) in sorted(expected.items()):
        it = by_base.get(base_name)
        if it is None:
            qty_ok = False
            qdet = f"нет позиции {base_name}"
            break
        if abs(it["nq"] - enq) > 0.51:
            qty_ok = False
            qdet = f"{base_name}: nq={it['nq']} expected={enq}"
        if abs(it["nr"] - enr) > 2:
            rev_ok = False
            rdet = f"{base_name}: nr={it['nr']} expected={round(enr)}"
    total_nq = sum(v[0] for v in expected.values())
    total_nr = sum(v[1] for v in expected.values())
    check(f"нетто-штуки по всем {len(expected)} позициям сходятся (всего {total_nq:.0f} шт)",
          qty_ok, qdet)
    check(f"нетто-выручка сходится (всего {total_nr:,.0f} ₽)".replace(",", " "),
          rev_ok, rdet)

    dead = by_base.get("Ремень «Ось»")
    check("неликвид без продаж: nq=0, остаток есть",
          dead is not None and dead["nq"] == 0 and dead["cs"] > 0,
          f"got={dead and (dead['nq'], dead['cs'])}")

    print("== Аналитика ==")
    summary = client.get("/api/summary").json()
    exp_stock = mock_ms.expected_stock_today()
    exp_units = round(sum(exp_stock.values()))
    check("summary: штук на складе = эталон mock-мира",
          summary["stock_units"] == exp_units,
          f"got={summary['stock_units']} expected={exp_units}")
    check("summary: продано за 30 дней > 0",
          summary["sold_30d_qty"] > 0 and summary["sold_30d_rev"] > 0,
          f"qty={summary['sold_30d_qty']} rev={summary['sold_30d_rev']}")
    check("summary: позиции и классы посчитаны",
          summary["positions"] > 0 and sum(summary["classes"].values()) == summary["positions"])

    repl = client.get("/api/replenish").json()
    items = repl["items"]
    check("replenish: есть рекомендации need>0", len(items) > 0,
          f"n={len(items)}")
    sellout_item = next((i for i in items if i["base_name"] == "Футболка «Курсив»"), None)
    check("replenish: распроданная позиция в списке заказа (cs=0, need>0)",
          sellout_item is not None and sellout_item["cs"] == 0
          and sellout_item["need"] > 0,
          f"got={sellout_item and (sellout_item['cs'], sellout_item['need'])}")
    if sellout_item:
        check("replenish: размерная сетка распроданной позиции из продаж (S/M/L)",
              set(sellout_item["sizes"].keys()) == {"S", "M", "L"}
              and sum(s["rec"] for s in sellout_item["sizes"].values())
              == sellout_item["need"],
              f"sizes={list(sellout_item['sizes'].keys())}")

    stocks = client.get("/api/stocks").json()
    check("stocks: 2 активных склада в колонках", len(stocks["warehouses"]) == 2,
          f"got={[w['name'] for w in stocks['warehouses']]}")
    hoodie = next((i for i in stocks["items"] if i["base_name"] == "Худи «Скетч»"), None)
    check("stocks: размерная разбивка по складам",
          hoodie is not None and {s["size"] for s in hoodie["sizes"]} >= {"S", "M", "L"},
          f"got={hoodie and [s['size'] for s in hoodie['sizes']]}")

    print("== Рейтинг оборачиваемости: правила legacy-таблицы ==")
    tresp = client.get("/api/turnover").json()["items"]
    groups = [it["group"] for it in tresp]
    order_ok = groups == sorted(groups, key=lambda g: {"rank": 0, "low_data": 1, "no_sales": 2}[g])
    check("порядок групп: рейтинг → мало данных → без продаж", order_ok,
          f"groups={groups}")
    check("первая строка — из рейтинга (шум не наверху)",
          tresp and tresp[0]["group"] == "rank",
          f"first={tresp and (tresp[0]['base_name'], tresp[0]['group'])}")
    cameo = next((it for it in tresp if it["base_name"] == "Бомбер «Камео»"), None)
    check("крошечный тираж («бомбер Регби»-кейс): low_data, не в рейтинге",
          cameo is not None and cameo["low_data"] and cameo["group"] == "low_data"
          and cameo["nq"] > 0,
          f"got={cameo and (cameo['group'], cameo['dis'], cameo['nq'], cameo['turnover'])}")
    belt = next((it for it in tresp if it["base_name"] == "Ремень «Ось»"), None)
    check("неликвид — в группе «без продаж»",
          belt is not None and belt["group"] == "no_sales")
    exp_tt = round(sum(it["turnover"] for it in tresp
                       if not it["archived"] and not it["low_data"]))
    check("turnover_total на дашборде без low_data-шума",
          summary["turnover_total"] == exp_tt,
          f"got={summary['turnover_total']} expected={exp_tt}")
    repl_ld = [it.get("low_data", False) for it in repl["items"]]
    check("replenish: «мало данных» не выше значимых позиций",
          repl_ld == sorted(repl_ld),
          f"flags={repl_ld}")

    print("== «Едет к нам» из заказов поставщику МС ==")
    exp_inc = mock_ms.expected_incoming()
    check("mock: seeded-заказы дают ожидания (Худи 14, Кольцо 5)",
          exp_inc == {"Худи «Скетч»": 14.0, "Кольцо «Грань»": 5.0},
          f"exp={exp_inc}")
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    ms_rows = {r["base_name"]: r["ms_qty"] for r in con.execute(
        "SELECT base_name, ms_qty FROM ordered_qty WHERE ms_qty != 0")}
    check("ordered_qty.ms_qty = эталон (частично принятый: qty−shipped)",
          ms_rows == exp_inc, f"got={ms_rows}")
    check("непроведённый, полностью принятый и старый доки не в «едет»",
          all(b not in ms_rows for b in
              ("Футболка «Манифест»", "Сумка «Тоут»", "Рубашка «Разворот»")))
    con.close()
    check("stats синка: incoming_qty=19 из 2 открытых доков",
          stats.get("incoming_qty") == 19 and stats.get("incoming_open_docs") == 2,
          f"got qty={stats.get('incoming_qty')} docs={stats.get('incoming_open_docs')}")

    r = client.post("/api/ordered", json={"base_name": "Худи «Скетч»", "qty": 3})
    check("ручная правка qty поверх ms_qty принята", r.status_code == 200)
    repl_inc = client.get("/api/replenish").json()
    hood_item = next((i for i in repl_inc["items"]
                      if i["base_name"] == "Худи «Скетч»"), None)
    hood_excl = next((e for e in repl_inc.get("excluded", [])
                      if e["base_name"] == "Худи «Скетч»"), None)
    if hood_item is not None:
        check("replenish: ordered = ручной qty + ms_qty (3+14)",
              hood_item["ordered"] == 17, f"got={hood_item['ordered']}")
    else:
        check("replenish: позиция закрыта заказом (причина упоминает заказ)",
              hood_excl is not None and "заказ" in hood_excl.get("reason", ""),
              f"got={hood_excl}")

    print("== Инкрементальный синк ==")
    # Эмулируем приёмку в МС: размер S принят полностью → у «Скетча» едет 8.
    mock_ms.PURCHASE_ORDERS[0]["positions"]["rows"][0]["shipped"] = 10.0
    r = client.post("/api/sync/run")
    check("инкрементальный синк запущен", r.status_code == 200 and r.json().get("ok"))
    status = wait_sync_done(client, timeout=120)
    check("инкрементальный синк done", status.get("state") == "done",
          f"state={status.get('state')} error={status.get('error', '')[:120]}")
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    hood = con.execute(
        "SELECT qty, ms_qty FROM ordered_qty WHERE base_name='Худи «Скетч»'"
    ).fetchone()
    check("инкремент пересобрал ms_qty после «приёмки» (14 → 8), qty=3 цел",
          hood is not None and hood["ms_qty"] == 8 and hood["qty"] == 3,
          f"got={dict(hood) if hood else None}")
    con.close()
    summary2 = client.get("/api/summary").json()
    check("после инкремента числа стабильны (остаток не «уехал»)",
          summary2["stock_units"] == summary["stock_units"],
          f"{summary['stock_units']} -> {summary2['stock_units']}")
    turno2 = {it["base_name"]: it["nq"] for it in client.get("/api/turnover").json()["items"]}
    drift = [b for b, it in by_base.items() if turno2.get(b) != it["nq"]]
    check("после инкремента нетто-продажи не изменились", not drift,
          f"drift={drift[:3]}")

    print("== Демо-режим не сломан ==")
    demo = httpx.Client(headers={"X-Oborot-CSRF": "1"}, base_url=f"http://127.0.0.1:{APP_PORT}", timeout=120.0)
    r = demo.post("/register", data={
        "name": "Демо", "email": "demo@test.io",
        "password": "secret123", "org_name": "Демо-бренд",
    })
    check("регистрация второго пользователя", r.status_code == 303)
    r = demo.post("/api/connect/demo")
    check("POST /api/connect/demo работает", r.status_code == 200 and r.json().get("ok"))
    dsum = demo.get("/api/summary").json()
    check("демо-данные посеялись (позиций ~55)", dsum["positions"] >= 45,
          f"positions={dsum['positions']}")
    check("изоляция тенантов: числа демо ≠ числа МС-организации",
          dsum["stock_units"] != summary["stock_units"])
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
