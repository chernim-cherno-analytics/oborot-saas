"""«Поставки»: удалённое за 30 дней видно и возвращается (QA 15.11).

Раньше удалённую партию или материал можно было вернуть только из тоста за 6
секунд, хотя сервер умеет возвращать. Доска отдаёт `trash`, страница рисует
список с «Вернуть».

Запуск: TZ=UTC python tests/test_supply_trash.py
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DB_PATH = ROOT / "test_supply_trash.db"
for suffix in ("", "-wal", "-shm"):
    p = Path(str(DB_PATH) + suffix)
    if p.exists():
        p.unlink()
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["SCHEDULER_ENABLED"] = "0"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

OK = FAIL = 0
P = "/api/supply/planning"


def check(name, cond, detail=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  OK   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


with TestClient(app, headers={"X-Oborot-CSRF": "1"}) as c, TestClient(app, headers={"X-Oborot-CSRF": "1"}) as other:
    c.post("/register", data={"name": "S", "email": "trash@test.io", "password": "secret123", "org_name": "Корзина"})
    other.post("/register", data={"name": "O", "email": "trash-o@test.io", "password": "secret123", "org_name": "Чужая"})
    b = c.get(P).json()
    check("пустая корзина", b.get("trash") == [], str(b.get("trash")))
    m = c.post(P + "/materials", json={"title": "Ткань серая", "qty": "50", "unit": "м", "op_id": "t-m1"}).json()["materials"][0]
    r = c.post(P + f"/materials/{m['id']}/archive", json={"rev": m["rev"], "op_id": "t-a1"})
    check("материал удалён", r.status_code == 200, r.text[:160])
    tr = c.get(P).json()["trash"]
    check("в корзине материал с названием и датой",
          len(tr) == 1 and tr[0]["kind"] == "material" and tr[0]["title"] == "Ткань серая" and tr[0]["archived_at"],
          str(tr))
    check("чужая организация корзину не видит", other.get(P).json()["trash"] == [])
    r = other.post(P + f"/materials/{m['id']}/restore", json={"op_id": "t-x"})
    check("чужая организация вернуть не может", r.status_code == 404, str(r.status_code))
    r = c.post(P + f"/materials/{m['id']}/restore", json={"op_id": "t-r1"})
    check("возврат работает", r.status_code == 200 and any(x["id"] == m["id"] for x in r.json()["materials"]),
          r.text[:160])
    check("после возврата корзина пуста", r.json()["trash"] == [], str(r.json()["trash"]))
    print("партия с назначением")
    it = [i for i in c.post(P + "/items", json={"title": "Куртка", "kind": "draft", "op_id": "t-i1"}).json()["items"]
          if i["title"] == "Куртка"][0]
    bt = [b for b in c.post(P + "/batches", json={"item_id": it["id"], "title": "Осень", "plan_qty": 10,
                                                  "op_id": "t-b1"}).json()["batches"] if b["title"] == "Осень"][0]
    m2 = [m for m in c.post(P + "/materials", json={"title": "Подклад", "qty": "30", "unit": "м",
                                                    "op_id": "t-m2"}).json()["materials"] if m["title"] == "Подклад"][0]
    r = c.post(P + "/assignments", json={"material_id": m2["id"], "batch_id": bt["id"], "qty": 12, "op_id": "t-as1"})
    check("назначение создано", r.status_code == 200, r.text[:160])
    bt = next(x for x in r.json()["batches"] if x["id"] == bt["id"])
    c.post(P + f"/batches/{bt['id']}/archive", json={"rev": bt["rev"], "op_id": "t-ab1"})
    tr = c.get(P).json()["trash"]
    check("партия в корзине", any(x["kind"] == "batch" and x["id"] == bt["id"] for x in tr), str(tr))
    r = c.post(P + f"/batches/{bt['id']}/restore", json={"op_id": "t-rb1"})
    rs = r.json().get("restored") or {}
    check("возврат партии сообщает, сколько снова в распределении", r.status_code == 200 and rs.get("assignments") == 1
          and float(rs.get("qty") or 0) == 12, str(rs))
    # Страница «Поставки» заменена «Производством» (PROD-1, D-91): проверки её
    # разметки сняты, ручки корзины выше проверяются по-прежнему.

for suffix in ("", "-wal", "-shm"):
    p = Path(str(DB_PATH) + suffix)
    if p.exists():
        p.unlink()
print(f"\nИТОГО: {OK} OK, {FAIL} FAIL")
sys.exit(1 if FAIL else 0)
