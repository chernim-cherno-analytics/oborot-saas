"""Демо: «Принят на склад» кладёт пришедшее на остаток (QA 15.11).

В демо нет синка МойСклада, и после закрытия заказа остаток не рос, а «Заказ»
снова просил те же штуки. Живую организацию (есть подключение МойСклада) это
не трогает.

Запуск: TZ=UTC python tests/test_demo_receipt.py
"""
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DB_PATH = ROOT / "test_demo_receipt.db"
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


def item(c, base):
    return next((i for i in c.get("/api/replenish").json()["items"] if i["base_name"] == base), None)


def cs_of(org_name, base):
    with sqlite3.connect(DB_PATH) as con:
        oid = con.execute("SELECT id FROM orgs WHERE name=?", (org_name,)).fetchone()[0]
        latest = con.execute("SELECT max(date) FROM stock_days WHERE org_id=?", (oid,)).fetchone()[0]
        return con.execute(
            "SELECT coalesce(sum(s.qty),0) FROM stock_days s JOIN products p ON p.id=s.product_id "
            "WHERE s.org_id=? AND s.date=? AND p.base_name=?", (oid, latest, base)).fetchone()[0]


with TestClient(app, headers={"X-Oborot-CSRF": "1"}) as c:
    c.post("/register", data={"name": "D", "email": "demo-rcv@test.io", "password": "secret123", "org_name": "Демо-приёмка"})
    check("демо", c.post("/api/connect/demo").status_code == 200)
    items = [i for i in c.get("/api/replenish").json()["items"] if i["need"] > 5]
    it = items[0]
    base, need = it["base_name"], it["need"]
    sizes = {k: v.get("rec", 0) for k, v in (it.get("sizes") or {}).items()} if isinstance(it.get("sizes"), dict) else {}
    cs0 = cs_of("Демо-приёмка", base)

    print("\n1. Заказ на всю потребность, отправлен, принят")
    o = c.post("/api/orders", json={"name": "Партия", "allow_duplicate": True,
                                    "items": [{"base_name": base, "qty": need}]}).json()
    c.post(f"/api/orders/{o['id']}/status", json={"status": "sent"})
    check("в производстве: позиция больше не просит дошить", (item(c, base) or {"need": 0})["need"] == 0,
          str((item(c, base) or {}).get("need")))
    c.post(f"/api/orders/{o['id']}/status", json={"status": "received"})
    cs1 = cs_of("Демо-приёмка", base)
    check("после «Принят на склад» остаток вырос на заказанное", cs1 == cs0 + need, f"{cs0} -> {cs1}, заказ {need}")
    after = item(c, base)
    check("«Заказ» не просит те же штуки снова", after is None or after["need"] < need, str(after and after["need"]))

    print("\n2. Названное принятое кладётся вместо заказанного")
    base2 = items[1]["base_name"]
    cs20 = cs_of("Демо-приёмка", base2)
    o2 = c.post("/api/orders", json={"name": "Вторая", "allow_duplicate": True,
                                     "items": [{"base_name": base2, "qty": 20}]}).json()
    c.post(f"/api/orders/{o2['id']}/status", json={"status": "sent"})
    c.post(f"/api/orders/{o2['id']}/status", json={"status": "received",
                                                   "received": [{"base_name": base2, "qty": 13}]})
    check("остаток вырос на принятые 13, а не на заказанные 20", cs_of("Демо-приёмка", base2) == cs20 + 13,
          f"{cs20} -> {cs_of('Демо-приёмка', base2)}")

    print("\n3. Живая организация (МойСклад подключён) не трогается")
    c2 = TestClient(app, headers={"X-Oborot-CSRF": "1"})
    c2.post("/register", data={"name": "L", "email": "live-rcv@test.io", "password": "secret123", "org_name": "Живая"})
    c2.post("/api/connect/demo")
    with sqlite3.connect(DB_PATH) as con:
        oid = con.execute("SELECT id FROM orgs WHERE name='Живая'").fetchone()[0]
        con.execute("INSERT INTO connections (org_id, kind, status, token_enc, config_json) "
                    "VALUES (?, 'moysklad', 'active', '', '{}')", (oid,))
        con.commit()
    base3 = c2.get("/api/replenish").json()["items"][0]["base_name"]
    cs30 = cs_of("Живая", base3)
    o3 = c2.post("/api/orders", json={"name": "Живой", "allow_duplicate": True,
                                      "items": [{"base_name": base3, "qty": 10}]}).json()
    c2.post(f"/api/orders/{o3['id']}/status", json={"status": "sent"})
    c2.post(f"/api/orders/{o3['id']}/status", json={"status": "received"})
    check("остаток живой организации не изменился", cs_of("Живая", base3) == cs30, f"{cs30} -> {cs_of('Живая', base3)}")
    c2.close()

for suffix in ("", "-wal", "-shm"):
    p = Path(str(DB_PATH) + suffix)
    if p.exists():
        p.unlink()
print(f"\nИТОГО: {OK} OK, {FAIL} FAIL")
sys.exit(1 if FAIL else 0)
