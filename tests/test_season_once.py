# -*- coding: utf-8 -*-
"""ЧЕРНОВИК D-66: при окне темпа «сезон» сезонность учитывается один раз.

На демо-данных до правки при окне «сезон» мастер просил 1 435 шт против
1 123 на «Заказе»; «Худи «Скетч»» 179 против 101. Здесь проверяется, что
потребность одной и той же позиции в мастере и на «Заказе» совпадает.
Запуск: python tests/test_season_once.py
"""
import os
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DB_PATH = ROOT / "test_season_once.db"
for suffix in ("", "-wal", "-shm"):
    p = Path(str(DB_PATH) + suffix)
    if p.exists():
        p.unlink()
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["SCHEDULER_ENABLED"] = "0"

from fastapi.testclient import TestClient  # noqa: E402

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


KEYS = ("thresholds", "horizon_days", "min_stock_days", "lead_time_days",
        "order_cadence_days", "safety_days", "moq_units", "reserve_new_pct",
        "overhead_pct", "price_type_sale", "price_type_cost", "cover_mode")
with TestClient(app, headers={"X-Oborot-CSRF": "1"}) as c:
    c.post("/register", data={"name": "x", "email": "season@test.io",
                              "password": "secret123", "org_name": "Сезон"})
    c.post("/api/connect/demo")
    st = c.get("/api/settings").json()
    body = {k: st[k] for k in KEYS if k in st}
    body["rate_window"] = "season"
    check("окно темпа «сезон» сохранено", c.post("/api/settings", json=body).status_code == 200
          and c.get("/api/settings").json().get("rate_window") == "season")
    rep = {i["base_name"]: i["need"] for i in c.get("/api/replenish").json()["items"]}
    prods = c.get("/api/productions").json()
    plist = prods if isinstance(prods, list) else prods.get("productions", [])
    plan = c.post("/api/order-plan/preview", json={
        "production_id": plist[0]["id"] if plist else None,
        "eta_date": (date.today() + timedelta(days=45)).isoformat(),
        "budget": 50_000_000, "budget_scope": "full", "max_share_pct": 100}).json()
    wiz = {i["base_name"]: i["need"] for i in plan.get("items", [])}
    common = sorted(set(rep) & set(wiz))
    diff = [(n, rep[n], wiz[n]) for n in common if abs(rep[n] - wiz[n]) > 1]
    check("в плане мастера есть позиции для сравнения", len(common) >= 10, str(len(common)))
    check("при окне «сезон» потребность в мастере = потребности на «Заказе» (±1 шт)",
          not diff, str(diff[:5]))

for suffix in ("", "-wal", "-shm"):
    p = Path(str(DB_PATH) + suffix)
    if p.exists():
        p.unlink()
print(f"\nИТОГО: {OK} OK, {FAIL} FAIL")
sys.exit(1 if FAIL else 0)
