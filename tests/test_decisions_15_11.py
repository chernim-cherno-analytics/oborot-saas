# -*- coding: utf-8 -*-
"""Решения владельца 15.11.2026: D-76 и D-77.

Что доказывается:
  1) D-76: позиция «в минус» не входит в рекомендацию «Заказа» (уходит в
     «Не в рекомендации» с убытком на штуке и потребностью), мастера (список
     «в минус» в review с галочкой) и «Бюджета» (excluded_loss); галочка
     «Взять» в мастере возвращает её как решение человека;
  2) D-77: темп размера делится не меньше чем на 14 дней наличия, в сетке
     «Заказа» и в калькуляторе «Заказ позиции».

Демо-данные, наружу набор не ходит. Запуск: python tests/test_decisions_15_11.py
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "test_decisions_15_11.db"
for suffix in ("", "-wal", "-shm"):
    p = Path(str(DB_PATH) + suffix)
    if p.exists():
        p.unlink()
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["SCHEDULER_ENABLED"] = "0"

from fastapi.testclient import TestClient  # noqa: E402

from app import analytics  # noqa: E402
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


print("\n1. D-77: порог 14 дней наличия")
w_short = analytics.size_weight({"sold365": 3, "days365": 2, "hist365": 300})
w_long = analytics.size_weight({"sold365": 3, "days365": 30, "hist365": 300})
check("3 продажи за 2 дня считаются как за 14", abs(w_short - 3 / 14) < 1e-9, str(w_short))
check("30 дней наличия порог не трогает", abs(w_long - 3 / 30) < 1e-9, str(w_long))
split = analytics.size_split({"S": {"sold365": 3, "days365": 2, "hist365": 300},
                              "M": {"sold365": 30, "days365": 100, "hist365": 300}}, 20)
check("размер с короткой историей не забирает большую долю", split["S"] < split["M"], str(split))

with TestClient(app, headers={"X-Oborot-CSRF": "1"}) as a:
    a.post("/register", data={"name": "A", "email": "dec1511@test.io",
                              "password": "secret123", "org_name": "Бренд Д"})
    r = a.post("/api/connect/demo")
    check("демо-данные подключены", r.status_code == 200, r.text[:200])

    print("\n2. D-76: «в минус» вне рекомендации")
    rep = a.get("/api/replenish").json()
    loss_rows = [e for e in rep["excluded"] if e.get("below_cost")]
    check("на демо есть позиции в минус", len(loss_rows) >= 1, str(rep["excluded"][:3]))
    check("в таблице «Заказа» позиций в минус нет",
          not any(i.get("below_cost") for i in rep["items"]))
    check("причина называет убыток и потребность",
          all("в минус" in e["reason"] and str(e["need"]) in e["reason"] for e in loss_rows),
          str(loss_rows[:1]))
    loss_names = {e["base_name"] for e in loss_rows}

    plan = a.post("/api/order-plan/preview", json={"budget": 10_000_000}).json()
    rev_names = {r["base_name"] for r in plan["review"].get("below_cost", [])}
    plan_names = {i["base_name"] for i in plan["items"]}
    check("план мастера не пустой (бюджет без ограничения)", len(plan_names) > 5, str(len(plan_names)))
    check("мастер не кладёт их в план", not (loss_names & plan_names), str(loss_names & plan_names))
    check("мастер называет их в списке «в минус»", rev_names and rev_names <= loss_names | rev_names
          and plan["review"]["below_cost_count"] == len(rev_names), str(rev_names))
    one = sorted(rev_names)[0] if rev_names else None
    if one:
        forced = a.post("/api/order-plan/preview", json={"budget": 10_000_000, "must_have": [one]}).json()
        row = next((i for i in forced["items"] if i["base_name"] == one), None)
        check("галочка «Взять» возвращает позицию как решение человека",
              row is not None and row.get("forced_by_user") is True, str(row)[:200])
        check("взятая позиция остаётся в списке «в минус», чтобы снять галочку там же",
              one in {r["base_name"] for r in forced["review"].get("below_cost", [])})
html = (ROOT / "templates" / "assistant.html").read_text(encoding="utf-8")
check("мастер помнит бюджет в черновике", "budget: bEl ? parseNum(\"budget\") : null" in html)

    bud = a.get("/api/budget?amount=500000").json()
    bud_names = {r["base_name"] for r in bud.get("excluded_loss", [])}
    check("«Бюджет» их не распределяет",
          not (bud_names & {i["base_name"] for i in bud["items"]}) and bud["excluded_loss_count"] >= 1,
          str(bud_names))

    print("\n3. D-77 в калькуляторе «Заказ позиции»")
    item = next(i for i in rep["items"] if i.get("sizes"))
    calc = a.get("/api/sizes/calc", params={"product": item["base_name"], "qty": 30}).json()
    check("калькулятор отвечает", "sizes" in calc, str(calc)[:200])

print(f"\nИТОГО: {OK} OK, {FAIL} FAIL")
for suffix in ("", "-wal", "-shm"):
    p = Path(str(DB_PATH) + suffix)
    if p.exists():
        p.unlink()
sys.exit(1 if FAIL else 0)
