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
    check("после синка «/» ведёт в Оборачиваемость (дашборд скрыт)",
          r.status_code == 302 and (r.headers.get("location") or "") == "/turnover",
          f"status={r.status_code} loc={r.headers.get('location')}")
    r = client.get("/turnover", follow_redirects=False)
    check("Оборачиваемость открывается", r.status_code == 200,
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
    # Сезонная оборачиваемость: история mock-мира — 60 дней (конец мая–июль),
    # т.е. лето (+хвост весны); зимы и осени в данных нет.
    sea0 = tresp[0].get("sea") or {}
    check("сезонная оборачиваемость: лето > 0, зима и осень = 0",
          sea0.get("summer", 0) > 0 and sea0.get("winter", 0) == 0
          and sea0.get("autumn", 0) == 0,
          f"sea={sea0}")
    # Формула заказа (правило legacy): need = темп×горизонт − прогнозный остаток
    # к приходу заказа; proj_stock = max(0, cs + едет − темп×lead_time).
    lead = repl.get("lead_time_days")
    hor = repl.get("horizon_days")
    check("replenish отдаёт lead_time_days и proj_stock",
          isinstance(lead, int) and lead > 0
          and all("proj_stock" in it for it in repl["items"]))
    bad = [
        it["base_name"] for it in repl["items"]
        if abs(it["need"] - max(0, round(it["rate"] * hor) - it["proj_stock"])) > 1
    ]
    check("need = темп×горизонт − прогнозный остаток (все позиции)", not bad,
          f"bad={bad[:3]}")
    proj_bad = [
        it["base_name"] for it in repl["items"]
        if it["proj_stock"] > it["cs"] + it["ordered"]
    ]
    check("прогнозный остаток не больше (остаток + едет)", not proj_bad,
          f"bad={proj_bad[:3]}")

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

    print("== Русские категории и sample-исключения (чистые функции) ==")
    from app.categories import ru_category
    from app.exclusions import is_service_item
    check("латинская категория переводится (Shirts → Рубашки)",
          ru_category("Shirts") == "Рубашки")
    check("без категории — по имени товара (бомбер → Верхняя одежда)",
          ru_category("", "Черный бомбер \"Регби\"") == "Верхняя одежда")
    check("кириллическая категория не трогается",
          ru_category("Худи и свитшоты", "х") == "Худи и свитшоты")
    check("нераспознанная латиница остаётся как есть",
          ru_category("Vintage", "Штука") == "Vintage")
    check("sample-позиции исключаются эвристикой",
          is_service_item("Пальто \"Скала\" sample", "Samples")
          and is_service_item("Молочные брюки сэмпл", "")
          and not is_service_item("Пальто \"Скала\"", "Одежда"))

    print("== «Оборот» за период (раздел снова открыт) ==")
    r = client.get(f"/api/revenue?date_from={mock_ms.DATES[0]}&date_to={mock_ms.DATES[-1]}")
    check("GET /api/revenue отдаёт 200", r.status_code == 200,
          f"status={r.status_code}")
    rev = r.json()
    exp_sales = mock_ms.expected_net_sales()
    exp_rev = round(sum(v[1] for v in exp_sales.values()))
    exp_qty = round(sum(v[0] for v in exp_sales.values()))
    check("выручка за весь период = эталон mock-мира",
          abs(rev["total_rev"] - exp_rev) <= 2 and abs(rev["total_qty"] - exp_qty) <= 1,
          f"got={rev['total_rev']}/{rev['total_qty']} exp={exp_rev}/{exp_qty}")
    top_base = max(exp_sales.items(), key=lambda kv: kv[1][1])[0]
    check("топ позиций: первый = максимум по выручке",
          rev["items"] and rev["items"][0]["base_name"] == top_base,
          f"got={rev['items'] and rev['items'][0]['base_name']} exp={top_base}")
    check("категории с долями, сумма долей ≈ 1",
          rev["categories"] and abs(sum(c["share"] for c in rev["categories"]) - 1) < 0.02)
    check("помесячный ряд: 18 месяцев, последний не пустой",
          len(rev["monthly"]) == 18 and rev["monthly"][-1]["total"] > 0,
          f"n={len(rev['monthly'])} last={rev['monthly'][-1]['total']}")
    r = client.get("/api/revenue?date_from=2026-01-31&date_to=2026-01-01")
    check("обратный период отдаёт 422", r.status_code == 422, f"status={r.status_code}")
    r = client.get("/orders")
    check("страница «Заказы» убрана: /orders отдаёт 404", r.status_code == 404,
          f"status={r.status_code}")
    for _pg in ("/budget", "/forecast", "/revenue"):
        r = client.get(_pg)
        check(f"страница {_pg} открыта (200)", r.status_code == 200, f"status={r.status_code}")
    r = client.get("/api/forecast")
    check("прогноз: помесячный ряд «сток vs продажи» на 6 месяцев",
          r.status_code == 200 and len(r.json().get("months", [])) == 6
          and all("stock_value" in m and "sales_value" in m for m in r.json()["months"]))

    print("== «Пульс»: этот месяц против среднего за 6 мес ==")
    r = client.get("/api/pulse")
    check("GET /api/pulse отвечает", r.status_code == 200, f"status={r.status_code}")
    pulse = r.json()
    from datetime import date as _date
    _cur = f"{_date.today().year:04d}-{_date.today().month:02d}"
    check("окно пульса: 6 полных месяцев до текущего",
          len(pulse["months"]) == 6 and pulse["months"][-1] < _cur
          and pulse["months"][0] < pulse["months"][-1],
          f"months={pulse['months']}")
    ps = pulse["sales"]
    exp_proj = round(ps["mtd"] / ps["days_passed"] * ps["days_in_month"]) \
        if ps["days_passed"] else ps["mtd"]
    check("прогноз месяца = продано ÷ прошло дней × дней в месяце",
          abs(ps["projected"] - exp_proj) <= 1,
          f"got={ps['projected']} exp={exp_proj}")
    known = [m["v"] for m in ps["months"] if m["v"] is not None]
    check("среднее продаж = среднее известных месяцев",
          known and abs(ps["avg6"] - sum(known) / len(known)) <= 1,
          f"avg6={ps['avg6']} known={known}")
    rev_by_month = {m["month"]: m["total"] for m in rev["monthly"]}
    diverged = [m["month"] for m in ps["months"]
                if m["v"] is not None and m["month"] in rev_by_month
                and abs(m["v"] - rev_by_month[m["month"]]) > 2]
    check("помесячные продажи пульса сходятся с рядом /api/revenue",
          not diverged, f"расхождение в {diverged}")
    if ps["avg6"] > 0:
        check("pct продаж = прогноз/среднее",
              abs(ps["pct"] - ps["projected"] / ps["avg6"]) < 0.01,
              f"pct={ps['pct']}")
    pst = pulse["stock"]
    check("склад сейчас: положительная стоимость и свежая дата",
          pst["current"] > 0 and pst["as_of"] is not None
          and pst["as_of"] >= mock_ms.DATES[-1],
          f"current={pst['current']} as_of={pst['as_of']}")
    known_st = [m["v"] for m in pst["months"] if m["v"] is not None]
    check("средний склад = среднее известных месяцев, все > 0",
          known_st and all(v > 0 for v in known_st)
          and abs(pst["avg6"] - sum(known_st) / len(known_st)) <= 1,
          f"avg6={pst['avg6']} known={known_st}")
    if pst["avg6"] > 0:
        check("pct склада = сейчас/среднее",
              abs(pst["pct"] - pst["current"] / pst["avg6"]) < 0.01,
              f"pct={pst['pct']}")

    print("== Ручные скидки и «Дефолтные скидки» ==")
    r = client.post("/api/discount-overrides",
                    json={"base_name": "Худи «Скетч»", "discount": 25})
    check("ручная скидка сохраняется", r.status_code == 200 and r.json().get("ok"))
    r = client.get("/api/discounts")
    check("скрытый раздел «Скидки»: API отдаёт 404", r.status_code == 404,
          f"status={r.status_code}")
    # Отчёт скрыт из продукта, но расчёт остаётся в коде — проверяем напрямую.
    from app import analytics as _an, analytics_markdown as _amd
    from app.db import SessionLocal as _SL
    from app.models import Org as _Org
    _con = sqlite3.connect(DB_PATH)
    _org_id = _con.execute(
        "SELECT org_id FROM sales GROUP BY org_id ORDER BY COUNT(*) DESC LIMIT 1"
    ).fetchone()[0]
    _con.close()
    _db = _SL()
    try:
        _org = _db.get(_Org, _org_id)
        _snap = _an.get_snapshot(_db, _org)
        _overrides = client.get("/api/discount-overrides").json()
        d_items = {it["base_name"]: it
                   for it in _amd.build_discounts(_snap, _overrides)["items"]}
    finally:
        _db.close()
    hood_d = d_items.get("Худи «Скетч»")
    check("отчёт «Скидки»: ручная скидка приоритетна (25%, manual)",
          hood_d is not None and hood_d["discount_pct"] == 25 and hood_d["manual"]
          and "Ручная" in hood_d["reason"],
          f"got={hood_d and (hood_d['discount_pct'], hood_d.get('manual'))}")
    r = client.post("/api/discount-overrides",
                    json={"base_name": "Худи «Скетч»", "discount": 0})
    check("нулевая скидка снимает ручную",
          r.status_code == 200
          and "Худи «Скетч»" not in client.get("/api/discount-overrides").json())
    r = client.post("/api/discount-overrides/defaults")
    check("«Дефолтные скидки» расставились (owner)",
          r.status_code == 200 and r.json().get("count", 0) > 0,
          f"resp={r.text[:80]}")
    dover = client.get("/api/discount-overrides").json()
    belt_disc = dover.get("Ремень «Ось»")
    check("неликвид без продаж → глубокая скидка 60% (правило legacy)",
          belt_disc == 60, f"got={belt_disc}")

    print("== «По нулям N дн» на остатках ==")
    stocks_resp = client.get("/api/stocks").json()
    all_sizes = [s for it in stocks_resp["items"] for s in it["sizes"]]
    check("у размеров есть поле zero_days",
          all_sizes and all("zero_days" in s for s in all_sizes))
    zero_sizes = [s for s in all_sizes if s["total"] == 0]
    check("размеры «по нулям» несут дни с последнего остатка (или None без истории)",
          all(s["zero_days"] is None or s["zero_days"] >= 0 for s in zero_sizes),
          f"n_zero={len(zero_sizes)}")

    print("== Активный сток / архив / категории / правило скидок ==")
    ast = client.get("/api/active-stock").json()
    check("active-stock: 2 склада и позиции", len(ast["warehouses"]) == 2
          and len(ast["items"]) > 0)
    hood_a = next((i for i in ast["items"] if i["base_name"] == "Худи «Скетч»"), None)
    check("active-stock: «Заказано» раздельно (ручное 3 + из МС 14)",
          hood_a is not None and hood_a["ordered_manual"] == 3
          and hood_a["ordered_ms"] == 14,
          f"got={hood_a and (hood_a['ordered_manual'], hood_a['ordered_ms'])}")
    check("active-stock: у позиций есть per_wh/zat/defq/sizes",
          all(k in ast["items"][0] for k in ("per_wh", "zat", "defq", "sizes")))
    first_groups = [i["group"] for i in ast["items"]]
    check("active-stock: без продаж — в конце",
          first_groups == sorted(first_groups,
                                 key=lambda g: {"rank": 0, "low_data": 1, "no_sales": 2}[g]))

    r = client.post("/api/hidden", json={"base_name": "Ремень «Ось»", "hidden": True})
    check("архив: позиция убрана", r.status_code == 200)
    t_items = {i["base_name"]: i for i in client.get("/api/turnover").json()["items"]}
    check("архив: hidden=true в оборачиваемости",
          t_items.get("Ремень «Ось»", {}).get("hidden") is True)
    ast2 = client.get("/api/active-stock").json()
    check("архив: позиции нет в активном стоке",
          all(i["base_name"] != "Ремень «Ось»" for i in ast2["items"]))
    r = client.post("/api/hidden", json={"base_name": "Ремень «Ось»", "hidden": False})
    check("архив: возврат работает", r.status_code == 200)

    r = client.post("/api/categories/merge",
                    json={"from_category": "Украшения", "to_category": "Аксессуары"})
    check("слияние категорий сохраняется", r.status_code == 200)
    ast3 = client.get("/api/active-stock").json()
    ring = next((i for i in ast3["items"] if i["base_name"] == "Кольцо «Грань»"), None)
    check("слияние: Украшения показываются как Аксессуары",
          ring is not None and ring["category"] == "Аксессуары",
          f"got={ring and ring['category']}")
    r = client.post("/api/categories/override",
                    json={"base_name": "Кольцо «Грань»", "category": "Витрина"})
    ring2 = next((i for i in client.get("/api/active-stock").json()["items"]
                  if i["base_name"] == "Кольцо «Грань»"), None)
    check("перенос позиции приоритетнее слияния",
          r.status_code == 200 and ring2 and ring2["category"] == "Витрина",
          f"got={ring2 and ring2['category']}")
    client.post("/api/categories/override", json={"base_name": "Кольцо «Грань»", "category": ""})
    client.post("/api/categories/merge", json={"from_category": "Украшения", "to_category": ""})

    r = client.get("/api/discount-rule").json()
    check("правило скидок отдаётся с дефолтами",
          r["rule"]["weak_over_pct"] == 60 and r["defaults"]["top_pct"] == 15)
    new_rule = dict(r["rule"]); new_rule["weak_over_pct"] = 70
    r = client.post("/api/discount-rule", json=new_rule)
    check("правило скидок сохраняется (owner)", r.status_code == 200
          and r.json()["rule"]["weak_over_pct"] == 70)
    client.post("/api/discount-overrides/defaults")
    belt2 = client.get("/api/discount-overrides").json().get("Ремень «Ось»")
    check("дефолтные скидки считаются по новому правилу (60 → 70)",
          belt2 == 70, f"got={belt2}")
    client.post("/api/discount-rule", json=dict(r.json()["rule"], weak_over_pct=60))

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
