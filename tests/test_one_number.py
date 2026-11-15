# -*- coding: utf-8 -*-
"""Одно число на позицию (D-72, шаг 1 спецификации «один поток», 14.11.2026).

Контракт: при окне темпа «сезон» потребность позиции на «Заказе» и в мастере
(бюджет без ограничения, «Глубже в топ», лимит доли 100%) совпадает для каждой
общей позиции. Это стало правдой после D-65 («едет»), D-66 (сезон один раз),
D-67 (распроданные) и TRUTH-2 (срок канала). При окне «год» мастер применяет
сезонный индекс; с D-79 (15.11.2026) «Заказ» тоже, и контракт держится при
любом окне.

Синтетическая база, демо-данные.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DB_PATH = ROOT / "test_one_number.db"
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


def needs(c):
    rep = {i["base_name"]: i.get("need_raw", i["need"]) for i in c.get("/api/replenish").json()["items"]}
    plan = c.post("/api/order-plan/preview", json={
        "budget": 50_000_000, "strategy": "grow", "max_share_pct": 100}).json()
    mas = {i["base_name"]: i["need"] for i in plan["items"]}
    return rep, mas


with TestClient(app, headers={"X-Oborot-CSRF": "1"}) as c:
    c.post("/register", data={"name": "T", "email": "one@test.io",
                              "password": "secret123", "org_name": "Одно число"})
    check("демо подключено", c.post("/api/connect/demo").status_code == 200)

    print("\n1. Окно «сезон»: одно число")
    check("окно «сезон» выставлено",
          c.post("/api/settings", json={"rate_window": "season"}).status_code == 200)
    rep, mas = needs(c)
    both = sorted(set(rep) & set(mas))
    diff = [(b, rep[b], mas[b]) for b in both if rep[b] != mas[b]]
    check("общих позиций достаточно для вывода", len(both) >= 20, str(len(both)))
    check("потребность «Заказа» и мастера совпадает у каждой общей позиции", not diff, str(diff[:5]))

    # D-79 (решение владельца 15.11.2026, 2A): сезонный темп везде, поэтому
    # одно число держится и при окнах «год» и «90 дней».
    for window, label in (("year", "год"), ("d90", "90 дней")):
        print(f"\n2. Окно «{label}»: одно число (D-79)")
        check(f"окно «{label}» выставлено",
              c.post("/api/settings", json={"rate_window": window}).status_code == 200)
        rep, mas = needs(c)
        both = sorted(set(rep) & set(mas))
        diff = [(b, rep[b], mas[b]) for b in both if rep[b] != mas[b]]
        check(f"общих позиций достаточно ({label})", len(both) >= 20, str(len(both)))
        check(f"потребность «Заказа» и мастера совпадает у каждой общей позиции ({label})",
              not diff, f"{len(diff)} из {len(both)}: {diff[:5]}")

    print("\n3. «Бюджет» = мастер с теми же условиями (D-79, 1A)")
    bud = c.get("/api/budget", params={"amount": 300000, "max_share": 30}).json()
    plan = c.post("/api/order-plan/preview", json={
        "budget": 300000, "budget_scope": "full", "strategy": "grow", "width_days": 0,
        "max_share_pct": 30, "production_id": (bud.get("production") or {}).get("id")}).json()
    b_q = {i["base_name"]: i["qty"] for i in bud["items"]}
    m_q = {i["base_name"]: i["qty"] for i in plan["items"]}
    check("«Бюджет» считает движком мастера", bud.get("engine") == "master", str(bud.get("engine")))
    check("состав и количества «Бюджета» совпадают с мастером", b_q == m_q and b_q,
          f"бюджет={sorted(b_q.items())[:4]} мастер={sorted(m_q.items())[:4]}")
    check("сумма «Бюджета» = себестоимость плана мастера",
          bud["used"] == plan["totals"]["cost"], f"{bud['used']} {plan['totals']['cost']}")

for suffix in ("", "-wal", "-shm"):
    p = Path(str(DB_PATH) + suffix)
    if p.exists():
        p.unlink()
print(f"\nИТОГО: {OK} OK, {FAIL} FAIL")
sys.exit(1 if FAIL else 0)
