"""D-83: частичная приёмка открытого заказа уменьшает «Едет» сразу.

Сцена QA 15.11: заказ 104 шт, приехало 60, «Едет» оставалось 104 до статуса
«Принят на склад», пришедшие штуки считались дважды. Теперь «Едет» = заказано
минус принято (как у заказов из МойСклада), не ниже нуля и не больше
заказанного; закрытие и удаление заказа не задваивают вычет.

Запуск: TZ=UTC python tests/test_receipts_incoming.py
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DB_PATH = ROOT / "test_receipts_incoming.db"
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


def ordered(c, base):
    """«Едет» позиции из снапшота (то, чем считают «Заказ», мастер и «Бюджет»)."""
    from app import analytics
    from app.db import SessionLocal
    from app.models import Org
    db = SessionLocal()
    try:
        org = db.query(Org).filter(Org.name == "Приёмка").first()
        analytics.invalidate(org.id)
        return float(analytics.get_snapshot(db, org)["items"][base]["ordered"])
    finally:
        db.close()


with TestClient(app, headers={"X-Oborot-CSRF": "1"}) as c:
    c.post("/register", data={"name": "R", "email": "rcv@test.io", "password": "secret123", "org_name": "Приёмка"})
    check("демо", c.post("/api/connect/demo").status_code == 200)
    items = c.get("/api/replenish").json()["items"]
    base, base2 = items[0]["base_name"], items[1]["base_name"]
    b0, b20 = ordered(c, base), ordered(c, base2)

    print("\n1. Частичная приёмка уменьшает «Едет»")
    o = c.post("/api/orders", json={"name": "Партия", "allow_duplicate": True,
                                    "items": [{"base_name": base, "qty": 104}, {"base_name": base2, "qty": 10}]}).json()
    c.post(f"/api/orders/{o['id']}/status", json={"status": "sent"})
    check("после отправки «Едет» +104", ordered(c, base) == b0 + 104, f"{b0} -> {ordered(c, base)}")
    r = c.post(f"/api/orders/{o['id']}/receipts", json={"lines": [{"base_name": base, "qty": 60}]})
    check("приёмка 60 записана", r.status_code == 200, r.text[:200])
    check("«Едет» = 104 − 60", ordered(c, base) == b0 + 44, f"{ordered(c, base)}")
    check("вторая позиция заказа не тронута", ordered(c, base2) == b20 + 10, f"{ordered(c, base2)}")
    c.post(f"/api/orders/{o['id']}/receipts", json={"lines": [{"base_name": base, "qty": -10}]})
    check("исправление −10 возвращает 10 в «Едет»", ordered(c, base) == b0 + 54, f"{ordered(c, base)}")
    c.post(f"/api/orders/{o['id']}/receipts", json={"lines": [{"base_name": base, "qty": 500}]})
    check("приняли больше заказанного: «Едет» по заказу 0, не меньше", ordered(c, base) == b0, f"{ordered(c, base)}")

    print("\n2. Закрытие заказа не задваивает вычет")
    c.post(f"/api/orders/{o['id']}/status", json={"status": "received"})
    check("после «Принят на склад» «Едет» по позиции как до заказа", ordered(c, base) == b0, f"{ordered(c, base)}")
    check("и по второй позиции", ordered(c, base2) == b20, f"{ordered(c, base2)}")

    print("\n3. Удаление отправленного заказа с приёмкой")
    o2 = c.post("/api/orders", json={"name": "Вторая", "allow_duplicate": True,
                                     "items": [{"base_name": base, "qty": 30}]}).json()
    c.post(f"/api/orders/{o2['id']}/status", json={"status": "sent"})
    c.post(f"/api/orders/{o2['id']}/receipts", json={"lines": [{"base_name": base, "qty": 12}]})
    check("«Едет» = 30 − 12", ordered(c, base) == b0 + 18, f"{ordered(c, base)}")
    r = c.delete(f"/api/orders/{o2['id']}")
    check("заказ удалён", r.status_code == 200, r.text[:200])
    check("после удаления «Едет» как до заказа (без минуса)", ordered(c, base) == b0, f"{ordered(c, base)}")

    print("\n4. Черновик приёмку не принимает, «Едет» не трогает")
    o3 = c.post("/api/orders", json={"name": "Черновик", "allow_duplicate": True,
                                     "items": [{"base_name": base, "qty": 5}]}).json()
    r = c.post(f"/api/orders/{o3['id']}/receipts", json={"lines": [{"base_name": base, "qty": 5}]})
    check("приёмка черновика: 422", r.status_code == 422, str(r.status_code))
    check("«Едет» не изменился", ordered(c, base) == b0, f"{ordered(c, base)}")

for suffix in ("", "-wal", "-shm"):
    p = Path(str(DB_PATH) + suffix)
    if p.exists():
        p.unlink()
print(f"\nИТОГО: {OK} OK, {FAIL} FAIL")
sys.exit(1 if FAIL else 0)
