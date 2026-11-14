# -*- coding: utf-8 -*-
"""ЧЕРНОВИК D-65: «едет» не закрывает продажи до прихода партии.

Воспроизведение прогона пользователем 14.11.2026: разместили заказ ровно на
рекомендацию «Заказа», а система тут же просит дошить ещё. На демо-данных
до правки: 1 002 шт рекомендовано и размещено, после размещения снова
рекомендовано 288 шт по 19 позициям. Здесь проверяется, что после
размещения рекомендации дозаказ той же позиции не рекомендуется.
Запуск: python tests/test_incoming_formula.py
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DB_PATH = ROOT / "test_incoming_formula.db"
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


with TestClient(app, headers={"X-Oborot-CSRF": "1"}) as c:
    c.post("/register", data={"name": "x", "email": "inc@test.io",
                              "password": "secret123", "org_name": "Едет"})
    c.post("/api/connect/demo")
    r = c.get("/api/replenish").json()
    rec = [i for i in r["items"] if (i.get("need") or 0) > 0]
    check("демо даёт рекомендацию", len(rec) > 10, str(len(rec)))
    items = [{"base_name": i["base_name"], "qty": int(i["need"]), "sizes": {}} for i in rec]
    o = c.post("/api/orders", json={"name": "ровно рекомендация", "items": items}).json()
    c.post(f"/api/orders/{o['id']}/status", json={"status": "sent"})
    r2 = c.get("/api/replenish").json()
    again = [(i["base_name"], i["need"]) for i in r2["items"]
             if (i.get("need") or 0) > 0 and i["base_name"] in {x["base_name"] for x in rec}]
    check("после размещения рекомендации та же позиция не рекомендуется снова",
          not again, str(again[:5]))

for suffix in ("", "-wal", "-shm"):
    p = Path(str(DB_PATH) + suffix)
    if p.exists():
        p.unlink()
print(f"\nИТОГО: {OK} OK, {FAIL} FAIL")
sys.exit(1 if FAIL else 0)
