# -*- coding: utf-8 -*-
"""Файл для цеха по зафиксированному заказу (ORDER-EXPORT-1, 14.11.2026).

Что доказывается:
  1) выгрузка отдаёт состав заказа КАК ОН СОХРАНЁН: модели, сетка размеров
     (сумма по размерам = количество модели), себестоимость и сумма из
     заказа, итог совпадает с карточкой заказа (total_cost, total_qty);
  2) строка без себестоимости не считается бесплатной: сумма пустая, итог
     подписан «без N поз.», имена названы в шапке;
  3) календарь платежей в файле совпадает с /api/orders/open;
  4) после изменения каталога и настроек файл не меняется: это снимок
     заказа, а не пересчёт;
  5) чужой и несуществующий заказ: 404, одинаково и без данных;
  6) имя модели «=cmd|…» остаётся текстом, а не формулой.

Синтетическая база, демо-данные, наружу набор не ходит.
Запуск: python tests/test_order_export.py
"""
import io
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "test_order_export.db"
for suffix in ("", "-wal", "-shm"):
    p = Path(str(DB_PATH) + suffix)
    if p.exists():
        p.unlink()
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["SCHEDULER_ENABLED"] = "0"

from fastapi.testclient import TestClient  # noqa: E402
from openpyxl import load_workbook  # noqa: E402

from app.main import app  # noqa: E402

OK = FAIL = 0


def check(name, cond, detail=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  OK   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


def sql(q, *args):
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute(q, args)
        con.commit()
        return cur.fetchall()


def sheet_rows(content: bytes, title: str):
    wb = load_workbook(io.BytesIO(content))
    ws = wb[title]
    return ws, [[c.value for c in row] for row in ws.iter_rows()]


with TestClient(app, headers={"X-Oborot-CSRF": "1"}) as a, \
        TestClient(app, headers={"X-Oborot-CSRF": "1"}) as b:
    a.post("/register", data={"name": "A", "email": "export-a@test.io",
                              "password": "secret123", "org_name": "Бренд А"})
    r = a.post("/api/connect/demo")
    check("демо-данные подключены", r.status_code == 200, r.text[:200])
    b.post("/register", data={"name": "B", "email": "export-b@test.io",
                              "password": "secret123", "org_name": "Бренд Б"})

    rep = a.get("/api/replenish").json()
    items_src = [i for i in rep.get("items", []) if i.get("sizes")][:3]
    check("в демо есть позиции с сеткой", len(items_src) == 3, str(len(items_src)))
    items = []
    for i in items_src:
        sizes = {s: 2 for s in list(i["sizes"])[:3]}
        items.append({"base_name": i["base_name"], "qty": sum(sizes.values()), "sizes": sizes})
    created = a.post("/api/orders", json={"name": "Партия к весне", "items": items})
    check("заказ создан", created.status_code == 200, created.text[:300])
    order_id = created.json().get("id")
    # Строка без себестоимости и с опасным именем: через API такую не создать
    # (каталог проверяется), но заказ, сохранённый раньше или из «новинки»,
    # может её нести. Дописываем в сохранённый состав напрямую.
    import json as _json
    raw = _json.loads(sql("SELECT items_json FROM production_orders WHERE id=?", order_id)[0][0])
    extra = {"base_name": "=cmd|' /C calc'!A0", "qty": 4, "sizes": {}, "cost": 0}
    if isinstance(raw, list):
        raw.append(extra)
    else:
        raw["items"].append(extra)
    sql("UPDATE production_orders SET items_json=? WHERE id=?",
        _json.dumps(raw, ensure_ascii=False), order_id)
    card = a.get(f"/api/orders/{order_id}").json()

    print("\n1. Состав как сохранён")
    resp = a.get(f"/api/orders/{order_id}/export.xlsx")
    check("выгрузка отдаёт xlsx", resp.status_code == 200
          and "spreadsheetml" in resp.headers.get("content-type", ""), str(resp.status_code))
    check("имя файла с номером заказа",
          f"order-{order_id}.xlsx" in resp.headers.get("content-disposition", ""))
    ws, rows = sheet_rows(resp.content, "Заказ")
    header = rows[1]
    total_idx = header.index("Итого, шт")
    size_cols = header[1:total_idx]
    by_name = {str(r[0]): r for r in rows[2:] if r[0] and not str(r[0]).startswith("Итого")
               and r[0] not in ("Заказ", "Статус", "Создан", "Производство",
                                "Плановая дата прихода", "ID партии", "Без себестоимости")}
    saved = {i["base_name"]: i for i in card["items"]}
    check("в файле ровно модели заказа", set(by_name) == set(saved),
          f"{sorted(by_name)} против {sorted(saved)}")
    sizes_ok = all(
        sum(int(by_name[n][1 + k] or 0) for k in range(len(size_cols))) == int(saved[n]["qty"])
        and int(by_name[n][total_idx] or 0) == int(saved[n]["qty"])
        for n in saved if saved[n].get("sizes"))
    check("сетка размеров складывается в количество модели", sizes_ok,
          str({n: (by_name[n][1:total_idx + 1], saved[n].get("sizes")) for n in saved}))
    total_row = next(r for r in rows if r[0] and str(r[0]).startswith("Итого"))
    check("итог штук совпадает с карточкой заказа",
          int(total_row[total_idx]) == int(card["total_qty"]), f"{total_row} {card['total_qty']}")
    check("итог суммы совпадает с карточкой заказа (без позиций без себестоимости)",
          int(total_row[-1]) == int(card["total_cost"]), f"{total_row[-1]} {card['total_cost']}")

    print("\n2. Без себестоимости не значит бесплатно")
    inj = by_name.get("=cmd|' /C calc'!A0")
    check("строка без себестоимости: сумма пустая, а не ноль", inj is not None and inj[-1] is None
          and inj[-2] is None, str(inj))
    check("итог подписан как неполный", "без 1 поз." in str(total_row[0]), str(total_row[0]))
    check("имя без себестоимости названо в шапке",
          any(r[0] == "Без себестоимости" and "=cmd" in str(r[1]) for r in rows))
    cell = next(c for row in ws.iter_rows() for c in row if c.value == "=cmd|' /C calc'!A0")
    check("имя «=cmd…» записано текстом, не формулой",
          cell.data_type == "s" and cell.quotePrefix, f"{cell.data_type} {cell.quotePrefix}")

    print("\n3. Платежи")
    _, prow = sheet_rows(resp.content, "Платежи")
    open_order = next(o for o in a.get("/api/orders/open").json()["orders"] if o["id"] == order_id)
    file_amounts = [int(r[2]) for r in prow[2:] if r[0] and r[0] != "Итого" and isinstance(r[2], (int, float))]
    check("платежи в файле совпадают с экраном",
          file_amounts == [round(p["amount"]) for p in open_order["payments"]],
          f"{file_amounts} против {[p['amount'] for p in open_order['payments']]}")

    print("\n4. Снимок, а не пересчёт")
    first = resp.content
    name0 = items[0]["base_name"]
    sql("UPDATE products SET cost_price = cost_price * 3 WHERE base_name = ?", name0)
    again = a.get(f"/api/orders/{order_id}/export.xlsx").content
    _, rows2 = sheet_rows(again, "Заказ")
    check("после изменения каталога состав и суммы файла те же",
          [r for r in rows2[1:]] == [r for r in rows[1:]])

    print("\n5. Чужой и несуществующий заказ")
    foreign = b.get(f"/api/orders/{order_id}/export.xlsx")
    missing = b.get("/api/orders/999999/export.xlsx")
    check("чужой заказ: 404", foreign.status_code == 404, str(foreign.status_code))
    check("несуществующий заказ: тот же 404 и то же тело",
          missing.status_code == 404 and missing.text == foreign.text, missing.text)
    check("в отказе нет данных заказа", "Партия к весне" not in foreign.text
          and name0 not in foreign.text)
    anon = TestClient(app).get(f"/api/orders/{order_id}/export.xlsx")
    check("без входа: не отдаётся", anon.status_code in (401, 403), str(anon.status_code))

    print("\n6. Кнопки в интерфейсе")
    page = a.get("/replenish").text
    check("на «Заказе» есть кнопка «Excel для цеха»", "Excel для цеха" in page
          and "/export.xlsx" in page)
    check("в мастере после создания заказа есть ссылка на файл",
          "Excel для цеха" in a.get("/assistant").text)

for suffix in ("", "-wal", "-shm"):
    p = Path(str(DB_PATH) + suffix)
    if p.exists():
        p.unlink()
print(f"\nИТОГО: {OK} OK, {FAIL} FAIL")
sys.exit(1 if FAIL else 0)
