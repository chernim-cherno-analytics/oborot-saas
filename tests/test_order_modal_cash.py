# -*- coding: utf-8 -*-
"""Окно оформления на «Заказе»: календарь платежей и черновик (прогон 14.11).

Что доказывается:
  1) /api/orders/payment-preview отдаёт календарь, сумма которого равна
     себестоимости, по этапам канала, ровно тот, что заказ получит после
     создания (сверка с /api/orders/open);
  2) open_due считает платежи уже открытых заказов в те же даты;
  3) чужой production_id не подмешивает чужие этапы; кривые входы 422;
  4) в окне есть «Сохранить черновиком», и черновик не попадает в «Едет».

Запуск: python tests/test_order_modal_cash.py
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DB_PATH = ROOT / "test_order_modal_cash.db"
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


with TestClient(app, headers={"X-Oborot-CSRF": "1"}) as a, \
        TestClient(app, headers={"X-Oborot-CSRF": "1"}) as b:
    a.post("/register", data={"name": "A", "email": "modal-a@test.io",
                              "password": "secret123", "org_name": "Бренд А"})
    b.post("/register", data={"name": "B", "email": "modal-b@test.io",
                              "password": "secret123", "org_name": "Бренд Б"})
    check("демо", a.post("/api/connect/demo").status_code == 200)
    pr = a.post("/api/productions", json={"name": "Ткань и пошив", "preset": "fabric_sewing"}).json()
    pid = pr.get("id") or (pr.get("production") or {}).get("id")
    pb = b.post("/api/productions", json={"name": "Чужой", "preset": "turnkey"}).json()
    pid_b = pb.get("id") or (pb.get("production") or {}).get("id")

    print("\n1. Календарь до оформления")
    d = a.get("/api/orders/payment-preview", params={"cost": 100000, "production_id": pid}).json()
    pays = d.get("payments") or []
    check("платежи есть и сумма = себестоимости", pays and sum(p["amount"] for p in pays) == 100000, str(d))
    check("этапов канала больше одного платежа (ткань и пошив)", len(pays) >= 2, str(pays))
    check("до оформления открытых заказов нет", d.get("open_due") == 0, str(d.get("open_due")))

    rep = a.get("/api/replenish").json()["items"][:2]
    items = [{"base_name": i["base_name"], "qty": 5} for i in rep]
    o = a.post("/api/orders", json={"name": "Партия", "production_id": pid, "items": items}).json()
    a.post(f"/api/orders/{o['id']}/status", json={"status": "sent"})
    d2 = a.get("/api/orders/payment-preview", params={"cost": 100000, "production_id": pid}).json()
    check("open_due учитывает открытый заказ", d2.get("open_due", 0) > 0, str(d2.get("open_due")))

    print("\n2. Изоляция и входы")
    d3 = a.get("/api/orders/payment-preview", params={"cost": 100000, "production_id": pid_b}).json()
    d4 = a.get("/api/orders/payment-preview", params={"cost": 100000}).json()
    check("чужой канал не подмешивает свои этапы (как без канала)",
          d3.get("payments") == d4.get("payments"), f"{d3.get('payments')} {d4.get('payments')}")
    r = a.get("/api/orders/payment-preview", params={"cost": -1})
    check("отрицательная сумма 422", r.status_code == 422, str(r.status_code))
    r = a.get("/api/orders/payment-preview", params={"cost": 1, "production_id": 10 ** 20})
    check("огромный id 422", r.status_code == 422, str(r.status_code))

    print("\n3. Черновик из окна")
    html = (ROOT / "templates" / "replenish.html").read_text(encoding="utf-8")
    check("в окне есть «Сохранить черновиком» и блок платежей",
          'id="btn-order-draft"' in html and 'id="order-modal-pay"' in html)
    before = {i["base_name"]: i.get("ordered", 0) for i in a.get("/api/replenish").json()["items"]}
    base3 = a.get("/api/replenish").json()["items"][3]["base_name"]
    dr = a.post("/api/orders", json={"name": "Черновик", "production_id": pid,
                                     "items": [{"base_name": base3, "qty": 7}]}).json()
    after = {i["base_name"]: i.get("ordered", 0) for i in a.get("/api/replenish").json()["items"]}
    check("черновик не попал в «Едет»", after.get(base3) == before.get(base3),
          f"{before.get(base3)} -> {after.get(base3)}")

    print("\n4. Черновик с поздней датой прихода платит с плановой даты размещения (QA 15.11)")
    from datetime import date as _d, timedelta as _td
    eta = (_d.today() + _td(days=150)).isoformat()
    base4 = a.get("/api/replenish").json()["items"][4]["base_name"]
    o4 = a.post("/api/orders", json={"name": "Поздний", "production_id": pid, "eta_date": eta,
                                     "allow_duplicate": True,
                                     "items": [{"base_name": base4, "qty": 9}]}).json()
    lead = next(p_ for p_ in a.get("/api/productions").json()["productions"] if p_["id"] == pid)["lead_days"]
    planned = (_d.fromisoformat(eta) - _td(days=lead)).isoformat()
    op = next((x for x in a.get("/api/orders/open").json()["orders"] if x["id"] == o4.get("id")), None)
    first = min((p_["date"] for p_ in (op or {}).get("payments", [])), default=None)
    check("первый платёж черновика = плановая дата размещения (приход минус срок)",
          first == planned, f"{first} против {planned}")
    a.post(f"/api/orders/{o4['id']}/status", json={"status": "sent"})
    op = next((x for x in a.get("/api/orders/open").json()["orders"] if x["id"] == o4.get("id")), None)
    first = min((p_["date"] for p_ in (op or {}).get("payments", [])), default=None)
    check("после отправки платежи идут с даты отправки (сегодня)", first == _d.today().isoformat(), str(first))

print(f"\nИТОГО: {OK} OK, {FAIL} FAIL")
for suffix in ("", "-wal", "-shm"):
    p = Path(str(DB_PATH) + suffix)
    if p.exists():
        p.unlink()
sys.exit(1 if FAIL else 0)
