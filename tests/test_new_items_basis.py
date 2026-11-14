# -*- coding: utf-8 -*-
"""Новинки в базе бюджета (прогон пользователями 14.11.2026, P2).

При «деньгах на сейчас» новинка занимает в бюджете долю предоплаты, а не
полную стоимость: мастер писал «свободно 184 ₽», хотя сегодня реально
недоиспользовано 99 184 ₽. При «весь заказ» ничего не меняется.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ["SCHEDULER_ENABLED"] = "0"

from app import order_planner as op  # noqa: E402

OK = FAIL = 0


def check(name, cond, detail=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  OK   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


stages = op.normalize_stages(op.STAGE_PRESETS["fabric_sewing"], 45)
share = op.prepay_share_total(stages)
cands = [{"base_name": f"p{i}", "category": "c", "cost_price": 1000, "need": 400, "rate_cover": 2,
          "gap_days": 0, "must_have": False, "turnover": 1 - i * 0.01} for i in range(10)]
new = [{"name": "Пальто", "qty": 30, "cost": 6000, "total": 180000}]
for scope, expect in (("now", round(180000 * share)), ("full", 180000)):
    brief = {"moq_units": 0, "width_days": 0, "budget": 1_500_000, "reserve_new_pct": 0,
             "max_share_pct": 100, "budget_scope": scope, "new_items": new}
    res = op._allocate(cands, brief, {"pack_multiple": 0}, stages)
    check(f"{scope}: новинка занимает {expect:,} ₽ бюджета".replace(",", " "),
          res["new_cost"] == expect and res["reserve"] == expect and res["new_cost_full"] == 180000,
          str({k: res.get(k) for k in ("new_cost", "reserve", "new_cost_full")}))
check("доля предоплаты канала «ткань → пошив» меньше 1", 0 < share < 1, str(share))

print(f"\nИТОГО: {OK} OK, {FAIL} FAIL")
sys.exit(1 if FAIL else 0)
