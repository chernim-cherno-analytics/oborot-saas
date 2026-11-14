# -*- coding: utf-8 -*-
"""Экран называет то, что скрывал (TRUTH-LABELS-1, 14.11.2026).

Находки прогона продукта субагентами-пользователями на демо-данных. Ни одна
формула и ни одно число расчёта не меняются; меняется только то, что экран
говорит о причинах:
  1) мастер: отсеянные «без продаж» названы по именам, распроданные (остаток
     0) первыми и помечены; число в списке совпадает с прежним счётчиком;
  2) «Бюджет»: исключённые «без продаж» названы, распроданные первыми;
  3) «Заказ позиции»: если по позиции уже едет партия, ответ это говорит
     (already_ordered), количество калькулятора не меняется;
  4) мастер подписывает колонку «Темп с сезоном», когда сезонный индекс
     реально применён (шаблон).

Синтетическая база, демо-данные. Запуск: python tests/test_truth_labels.py
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "test_truth_labels.db"
for suffix in ("", "-wal", "-shm"):
    p = Path(str(DB_PATH) + suffix)
    if p.exists():
        p.unlink()
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["SCHEDULER_ENABLED"] = "0"

from fastapi.testclient import TestClient  # noqa: E402

from app import order_planner as op  # noqa: E402
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


print("\n1. Мастер: отсеянные названы")
snap = {"items": {
    "Живая": {"cs": 5, "turnover": 900, "category": "Футболки", "cls": "good"},
    "Распродан бестселлер": {"cs": 0, "turnover": 5000, "category": "Куртки", "cls": "best"},
    "Распродана слабая": {"cs": 0, "turnover": 100, "category": "Куртки", "cls": "weak"},
}}
rows = op._stale_rows(snap, ["Живая", "Распродана слабая", "Распродан бестселлер"])
check("распроданные первыми, внутри по оборачиваемости",
      [r["base_name"] for r in rows] == ["Распродан бестселлер", "Распродана слабая", "Живая"],
      str([r["base_name"] for r in rows]))
check("признак «распродана» только у остатка 0",
      [r["sold_out"] for r in rows] == [True, True, False])
check("ни одного нового числа: только имя, категория, класс, оборот и остаток",
      set(rows[0]) == {"base_name", "category", "cls", "turnover", "cs", "sold_out"})

with TestClient(app, headers={"X-Oborot-CSRF": "1"}) as c:
    c.post("/register", data={"name": "T", "email": "truth@test.io",
                              "password": "secret123", "org_name": "Правда"})
    check("демо подключено", c.post("/api/connect/demo").status_code == 200)

    print("\n2. «Бюджет»: исключённые названы")
    b = c.get("/api/budget", params={"amount": 1500000, "max_share": 30}).json()
    stale = b.get("excluded_fresh") or []
    check("список имён есть и совпадает со счётчиком (до 50)",
          len(stale) == min(b.get("excluded_fresh_count", 0), 50), f"{len(stale)} {b.get('excluded_fresh_count')}")
    check("распроданные первыми", [r["cs"] > 0 for r in stale] == sorted(r["cs"] > 0 for r in stale),
          str(stale[:5]))

    print("\n3. «Заказ позиции»: уже едет")
    rep = c.get("/api/replenish").json()
    product = next(i["base_name"] for i in rep["items"] if i.get("sizes"))
    before = c.get("/api/sizes/calc", params={"product": product, "qty": 30}).json()
    c.post("/api/ordered", json={"base_name": product, "qty": 30})
    after = c.get("/api/sizes/calc", params={"product": product, "qty": 30}).json()
    check("отметка «Заказано» видна калькулятору", after["totals"].get("already_ordered", 0) >= 30,
          str(after["totals"]))
    check("раскладка по размерам не изменилась (формула калькулятора та же)",
          [s["order_stock"] for s in before["sizes"]] == [s["order_stock"] for s in after["sizes"]])
    page = c.get("/sizes").text
    check("страница говорит про уже едущую партию", "уже едет" in page)

    print("\n3а. Галочка у распроданной берёт её в расчёт")
    import json as _json
    prods = c.get("/api/productions").json()
    plist = prods if isinstance(prods, list) else prods.get("productions", [])
    pid = plist[0]["id"] if plist else None
    from datetime import date as _d, timedelta as _td
    body = {"production_id": pid, "eta_date": (_d.today() + _td(days=45)).isoformat(),
            "budget": 3000000, "budget_scope": "full"}
    plan = c.post("/api/order-plan/preview", json=body).json()
    # D-67 (решение владельца 14.11.2026, вариант B): распроданная позиция со
    # спросом до стокаута больше не отсеивается как «без продаж».
    rep_all = c.get("/api/replenish").json()["items"]
    soldout = [i for i in rep_all if (i.get("cs") or 0) <= 0 and (i.get("rate_year") or 0) > 0
               and not i.get("low_data") and (i.get("need") or 0) > 0]
    stale_names = {r["base_name"] for r in (plan.get("review") or {}).get("stale", [])}
    plan_names = {i["base_name"] for i in plan.get("items", [])}
    check("в демо есть распроданные позиции со спросом", bool(soldout), str(len(soldout)))
    check("D-67: ни одна из них не отсеяна как «без продаж»",
          not ({i["base_name"] for i in soldout} & stale_names),
          str(sorted({i["base_name"] for i in soldout} & stale_names)))
    check("D-67: распроданные со спросом входят в план мастера",
          {i["base_name"] for i in soldout} <= plan_names,
          str(sorted({i["base_name"] for i in soldout} - plan_names)))
    bud = c.get("/api/budget", params={"amount": 3000000, "max_share": 30}).json()
    bud_ex = {r["base_name"] for r in bud.get("excluded_fresh") or []}
    check("D-67: «Бюджет» их тоже не отсеивает",
          not ({i["base_name"] for i in soldout} & bud_ex), str(sorted(bud_ex)[:5]))

    print("\n4. Мастер: подписи на странице")
    html = c.get("/assistant").text
    check("колонка «Темп с сезоном» предусмотрена", "Темп с сезоном, шт/дн" in html)
    check("отсеянные «без продаж» рисуются по именам", "staleBlock(rev)" in html
          and "распродана" in html)

for suffix in ("", "-wal", "-shm"):
    p = Path(str(DB_PATH) + suffix)
    if p.exists():
        p.unlink()
print(f"\nИТОГО: {OK} OK, {FAIL} FAIL")
sys.exit(1 if FAIL else 0)
