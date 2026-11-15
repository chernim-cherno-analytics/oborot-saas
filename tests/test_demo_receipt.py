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


from datetime import datetime as _dt  # noqa: E402

from app import api as _api  # noqa: E402

# Заказы, принятые до появления демо-приёмки, правки на остаток не двигают
# (граница DEMO_RECEIPT_SINCE). В тесте заказы принимаются «сейчас»: граница
# сдвигается в прошлое, а отдельная сцена проверяет старый заказ.
_REAL_SINCE = _api.DEMO_RECEIPT_SINCE
_api.DEMO_RECEIPT_SINCE = _dt(2000, 1, 1)
_REAL_PARTIAL = _api.DEMO_PARTIAL_SINCE
_api.DEMO_PARTIAL_SINCE = _dt(2000, 1, 1)

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

    base4 = items[2]["base_name"]
    cs40 = cs_of("Демо-приёмка", base4)
    o4 = c.post("/api/orders", json={"name": "Не пришло", "allow_duplicate": True,
                                     "items": [{"base_name": base4, "qty": 20}]}).json()
    c.post(f"/api/orders/{o4['id']}/status", json={"status": "sent"})
    c.post(f"/api/orders/{o4['id']}/status", json={"status": "received",
                                                   "received": [{"base_name": base4, "qty": 0}]})
    check("подтверждённый ноль не кладёт заказанное (ревью #131)", cs_of("Демо-приёмка", base4) == cs40,
          f"{cs40} -> {cs_of('Демо-приёмка', base4)}")

    print("\n2б. Приёмку поправили после закрытия: остаток двигается на разницу (ревью #131)")
    c.post(f"/api/orders/{o2['id']}/receipts", json={"lines": [{"base_name": base2, "qty": -5}]})
    check("исправление −5: остаток +8 от исходного", cs_of("Демо-приёмка", base2) == cs20 + 8,
          f"{cs20} -> {cs_of('Демо-приёмка', base2)}")
    base5 = items[3]["base_name"]
    cs50 = cs_of("Демо-приёмка", base5)
    o5 = c.post("/api/orders", json={"name": "Без количеств", "allow_duplicate": True,
                                     "items": [{"base_name": base5, "qty": 20}]}).json()
    c.post(f"/api/orders/{o5['id']}/status", json={"status": "sent"})
    c.post(f"/api/orders/{o5['id']}/status", json={"status": "received"})
    check("закрыли без количеств: +20", cs_of("Демо-приёмка", base5) == cs50 + 20)
    c.post(f"/api/orders/{o5['id']}/receipts", json={"lines": [{"base_name": base5, "qty": 13}]})
    check("назвали 13 позже: остаток +13, а не +20", cs_of("Демо-приёмка", base5) == cs50 + 13,
          f"{cs50} -> {cs_of('Демо-приёмка', base5)}")

    cs_f = cs_of("Демо-приёмка", base2)
    for k in range(5):
        c.post(f"/api/orders/{o2['id']}/receipts", json={"lines": [{"base_name": base2, "qty": 0.4}],
                                                        "idempotency_key": f"frac-{k}"})
    check("пять довозов по 0,4: остаток +2 (ревью #133)", cs_of("Демо-приёмка", base2) == cs_f + 2,
          f"{cs_f} -> {cs_of('Демо-приёмка', base2)}")

    print("\n2в. Заказ с разбивкой по размерам: исправление снимает ровно с этих размеров (ревью #131)")
    def sizes_of(org_name, base):
        with sqlite3.connect(DB_PATH) as con:
            oid = con.execute("SELECT id FROM orgs WHERE name=?", (org_name,)).fetchone()[0]
            latest = con.execute("SELECT max(date) FROM stock_days WHERE org_id=?", (oid,)).fetchone()[0]
            return dict(con.execute(
                "SELECT p.size, s.qty FROM stock_days s JOIN products p ON p.id=s.product_id "
                "WHERE s.org_id=? AND s.date=? AND p.base_name=?", (oid, latest, base)).fetchall())
    cand = next(i for i in c.get("/api/replenish").json()["items"][4:] if len(i.get("sizes") or {}) >= 2)
    base6 = cand["base_name"]
    sz = sorted(cand["sizes"])[0]
    s0 = sizes_of("Демо-приёмка", base6)
    o6 = c.post("/api/orders", json={"name": "Размер", "allow_duplicate": True,
                                     "items": [{"base_name": base6, "qty": 20, "sizes": {sz: 20}}]}).json()
    c.post(f"/api/orders/{o6['id']}/status", json={"status": "sent"})
    c.post(f"/api/orders/{o6['id']}/status", json={"status": "received"})
    s1 = sizes_of("Демо-приёмка", base6)
    check("закрытие кладёт 20 ровно в заказанный размер",
          s1[sz] == s0[sz] + 20 and all(s1[k] == s0[k] for k in s0 if k != sz), f"{s0} -> {s1}")
    c.post(f"/api/orders/{o6['id']}/receipts", json={"lines": [{"base_name": base6, "qty": 0}]})
    s2 = sizes_of("Демо-приёмка", base6)
    check("подтверждённый ноль после закрытия возвращает размеры как были", s2 == s0, f"{s0} -> {s2}")

    print("\n2г. Заказ, принятый до демо-приёмки, правки на остаток не двигают (ревью #133)")
    _api.DEMO_RECEIPT_SINCE = _REAL_SINCE
    with sqlite3.connect(DB_PATH) as con:
        con.execute("UPDATE production_orders SET received_at='2026-11-01 10:00:00' WHERE id=?", (o5["id"],))
        con.commit()
    before_old = cs_of("Демо-приёмка", base5)
    c.post(f"/api/orders/{o5['id']}/receipts", json={"lines": [{"base_name": base5, "qty": 2}]})
    check("старый заказ: остаток не изменился", cs_of("Демо-приёмка", base5) == before_old,
          f"{before_old} -> {cs_of('Демо-приёмка', base5)}")
    _api.DEMO_RECEIPT_SINCE = _dt(2000, 1, 1)

    print("\n2д. D-83 в демо: приёмка частями по открытому заказу сразу кладёт на остаток")
    base7 = items[5]["base_name"]
    cs70 = cs_of("Демо-приёмка", base7)
    o7 = c.post("/api/orders", json={"name": "Частями", "allow_duplicate": True,
                                     "items": [{"base_name": base7, "qty": 30}]}).json()
    c.post(f"/api/orders/{o7['id']}/status", json={"status": "sent"})
    c.post(f"/api/orders/{o7['id']}/receipts", json={"lines": [{"base_name": base7, "qty": 18}]})
    check("приехало 18 из 30: остаток +18 сразу", cs_of("Демо-приёмка", base7) == cs70 + 18,
          f"{cs70} -> {cs_of('Демо-приёмка', base7)}")
    c.post(f"/api/orders/{o7['id']}/status", json={"status": "received",
                                                   "received": [{"base_name": base7, "qty": 12}]})
    check("закрыли с остатком 12: итого +30, без двойного счёта", cs_of("Демо-приёмка", base7) == cs70 + 30,
          f"{cs70} -> {cs_of('Демо-приёмка', base7)}")

    print("\n2е. Приёмка частями, записанная до выкладки D-83, уходит на остаток при закрытии (ревью #130)")
    base8 = items[6]["base_name"]
    cs80 = cs_of("Демо-приёмка", base8)
    o8 = c.post("/api/orders", json={"name": "До выкладки", "allow_duplicate": True,
                                     "items": [{"base_name": base8, "qty": 30}]}).json()
    c.post(f"/api/orders/{o8['id']}/status", json={"status": "sent"})
    _api.DEMO_PARTIAL_SINCE = _REAL_PARTIAL
    c.post(f"/api/orders/{o8['id']}/receipts", json={"lines": [{"base_name": base8, "qty": 18}]})
    with sqlite3.connect(DB_PATH) as con:
        con.execute("UPDATE order_receipts SET at='2026-11-15 18:00:00' WHERE order_id=?", (o8["id"],))
        con.commit()
    c.post(f"/api/orders/{o8['id']}/status", json={"status": "received"})
    check("закрыли без новых строк: принятые до выкладки 18 легли на остаток при закрытии",
          cs_of("Демо-приёмка", base8) == cs80 + 18, f"{cs80} -> {cs_of('Демо-приёмка', base8)}")
    _api.DEMO_PARTIAL_SINCE = _dt(2000, 1, 1)

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
