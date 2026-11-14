# -*- coding: utf-8 -*-
"""Одно число на позицию (D-72, шаг 1 спецификации «один поток», 14.11.2026).

Контракт: при окне темпа «сезон» потребность позиции на «Заказе» и в мастере
(бюджет без ограничения, «Глубже в топ», лимит доли 100%) совпадает для каждой
общей позиции. Это стало правдой после D-65 («едет»), D-66 (сезон один раз),
D-67 (распроданные) и TRUTH-2 (срок канала). При окне «год» мастер применяет
сезонный индекс категории, а «Заказ» нет: расхождение печатается для
сведения, но не проверяется, пока владелец не решит вопрос 2 спецификации.

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

    print("\n2. Окно «год»: для сведения (ждёт решения владельца, вопрос 2)")
    c.post("/api/settings", json={"rate_window": "year"})
    rep, mas = needs(c)
    both = sorted(set(rep) & set(mas))
    diff = [(b, rep[b], mas[b]) for b in both if rep[b] != mas[b]]
    print(f"  ИНФО окно «год»: расходятся {len(diff)} из {len(both)}, примеры {diff[:3]}")

for suffix in ("", "-wal", "-shm"):
    p = Path(str(DB_PATH) + suffix)
    if p.exists():
        p.unlink()
print(f"\nИТОГО: {OK} OK, {FAIL} FAIL")
sys.exit(1 if FAIL else 0)
