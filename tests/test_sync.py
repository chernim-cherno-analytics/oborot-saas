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

    print("== Инкрементальный синк ==")
    r = client.post("/api/sync/run")
    check("инкрементальный синк запущен", r.status_code == 200 and r.json().get("ok"))
    status = wait_sync_done(client, timeout=120)
    check("инкрементальный синк done", status.get("state") == "done",
          f"state={status.get('state')} error={status.get('error', '')[:120]}")
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
