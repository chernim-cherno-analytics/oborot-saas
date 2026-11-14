# -*- coding: utf-8 -*-
"""Повторный прогон пользователями 14.11.2026: экраны говорят одно (TRUTH-2).

Доказывается (формулы потребности не меняются, D-72 сводит входы):
  1) срок канала с заданными этапами одинаков на «Заказе», в «Заказе
     позиции» и в мастере: сумма этапов (было: «Заказ» 45, мастер 65);
  2) «в минус» (средняя цена ниже себестоимости) помечено на «Заказе» и в
     строках мастера тем же флагом, что в аналитике; количества не меняются;
  3) партия дороже лимита доли названа с ценой, даже если потолок волны или
     деньги обрезали позицию раньше (было: бестселлер в «не вошло» без цены);
     распределение штук при этом прежнее (сравнение на 20 000 планов в PR);
  4) мастер после создания заказа не считает его деньги дважды (шаблон);
  5) Excel «Бюджета»: темп в месяц тем же множителем 30,44, что на экране;
  6) «Заказ позиции» стартует с рекомендации «Заказа» (поле need в списке);
  7) «Заказ»: заголовок открытых заказов отличает черновики от отправленных.
Синтетическая база, демо-данные.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DB_PATH = ROOT / "test_truth_2.db"
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


with TestClient(app, headers={"X-Oborot-CSRF": "1"}) as c:
    c.post("/register", data={"name": "T", "email": "t2@test.io",
                              "password": "secret123", "org_name": "Правда-2"})
    check("демо подключено", c.post("/api/connect/demo").status_code == 200)

    print("\n1. Срок канала одинаков на всех экранах")
    r = c.post("/api/productions", json={"name": "Ткань и пошив", "preset": "fabric_sewing"})
    check("канал «ткань → пошив» создан", r.status_code == 200, r.text[:200])
    pid = r.json().get("id") or r.json().get("production", {}).get("id")
    rep = c.get("/api/replenish").json()
    base = next(i["base_name"] for i in rep["items"] if i.get("need", 0) > 0 and i.get("sizes"))
    r = c.post("/api/productions/assign", json={"base_name": base, "production_id": pid})
    check("позиция переведена на канал", r.status_code == 200, r.text[:200])
    plan = c.post("/api/order-plan/preview", json={"budget": 1500000, "production_id": pid}).json()
    rep = c.get("/api/replenish").json()
    row = next(i for i in rep["items"] if i["base_name"] == base)
    check("«Заказ» берёт срок канала = сумме этапов мастера (65)",
          row.get("lead_time_days") == plan["lead_days"] == 65,
          f"Заказ {row.get('lead_time_days')}, мастер {plan['lead_days']}")
    calc = c.get("/api/sizes/calc", params={"product": base, "qty": 30}).json()
    check("«Заказ позиции» берёт тот же срок",
          str(65) in str(calc.get("lead_time_days", calc.get("lead_days", ""))) or calc.get("lead_time_days") == 65,
          str({k: calc.get(k) for k in ("lead_time_days", "lead_days", "arrival")}))

    print("\n2. «В минус» назван везде одинаково")
    turn = c.get("/api/turnover").json()
    loss_names = {i["base_name"] for i in turn.get("items", []) if i.get("below_cost") and not i.get("low_data")}
    rep_loss = {i["base_name"] for i in rep["items"] if i.get("below_cost")}
    check("в демо есть позиции «в минус»", bool(loss_names), str(len(loss_names)))
    check("«Заказ» помечает ровно позиции «в минус» из аналитики (среди своих строк)",
          rep_loss == loss_names & {i["base_name"] for i in rep["items"]}, f"{rep_loss} {loss_names}")
    plan_main = c.post("/api/order-plan/preview", json={"budget": 3000000}).json()
    plan_loss = {i["base_name"] for i in plan_main["items"] if i.get("below_cost")}
    check("строки мастера несут тот же флаг",
          plan_loss and plan_loss <= loss_names, f"{plan_loss} {loss_names}")

    print("\n6. «Заказ позиции» стартует с рекомендации")
    prods = c.get("/api/sizes/products").json()["products"]
    by = {p["base_name"]: p for p in prods}
    check("в списке позиций есть рекомендация «Заказа»",
          base in by and by[base].get("need") == row.get("need"), f"{by.get(base)} need={row.get('need')}")

print("\n3. Партия дороже лимита названа с ценой")
stages = op.normalize_stages([{"name": "a", "days": 30, "prepay_pct": 100}], 45)
# Пример из сравнения 20 000 планов: прежний код ставил «Норд» в «не вошло»
# без цены партии (потребность 23 меньше партии 30, потолок волны обрезал
# раньше проверки партии), новый называет партию с ценой; штуки те же.
cands = [
    {"base_name": "p0", "category": "c", "cost_price": 8000, "need": 77, "rate_cover": 1,
     "gap_days": 0, "must_have": False, "turnover": 1.0},
    {"base_name": "Норд", "category": "c", "cost_price": 12299, "need": 23, "rate_cover": 3,
     "gap_days": 0, "must_have": False, "turnover": 0.9},
    {"base_name": "p2", "category": "c", "cost_price": 8000, "need": 120, "rate_cover": 1,
     "gap_days": 0, "must_have": False, "turnover": 0.8},
]
brief = {"moq_units": 30, "width_days": 0, "budget": 100000, "reserve_new_pct": 0,
         "max_share_pct": 10, "budget_scope": "now", "new_items": []}
res = op._allocate(cands, brief, {"pack_multiple": 0}, stages)
check("Норд не заказан и стоит в «партия дороже лимита» с ценой 30 шт",
      "Норд" not in res["alloc"] and res["moq_over_cap_qty"].get("Норд") == 30
      and res["moq_over_cap"].get("Норд") == 30 * 12299, str(res["moq_over_cap"]))

print("\n4, 5, 7. Шаблоны и Excel")
html = (ROOT / "templates" / "assistant.html").read_text(encoding="utf-8")
check("мастер: после создания заказа его платежи не прибавляются второй раз",
      "if(STATE.createdOrder){ payments = []; }" in html and "уже в открытых" in html)
check("мастер: плашка канала перерисовывается после создания",
      "function refreshAfterCreate" in html and 'id="openBanner"' in html)
xl = (ROOT / "app" / "export_xlsx.py").read_text(encoding="utf-8")
check("Excel «Бюджета»: темп в месяц × 30,44, как на экране",
      'round(it["rate"] * 30.44, 1)' in xl and 'round(it["rate"] * 30, 1)' not in xl)
rep_html = (ROOT / "templates" / "replenish.html").read_text(encoding="utf-8")
check("«Заказ»: черновики названы отдельно и в «Едет» не входят",
      "в «Едет» не входят, пока не отправлены в производство" in rep_html)

for suffix in ("", "-wal", "-shm"):
    p = Path(str(DB_PATH) + suffix)
    if p.exists():
        p.unlink()
print(f"\nИТОГО: {OK} OK, {FAIL} FAIL")
sys.exit(1 if FAIL else 0)
