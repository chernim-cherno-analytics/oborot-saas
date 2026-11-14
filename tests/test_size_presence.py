# -*- coding: utf-8 -*-
"""D-68 (решение владельца 14.11.2026): размерная сетка по темпу в днях наличия.

Доказывается:
  1) размер, которого долго не было, получает долю по своему темпу, а не по
     голым продажам (было: меньше нужного);
  2) без сведений о днях (старый формат, суммы по позициям) вес прежний;
  3) продажи без дней наличия в истории делятся на даты истории позиции;
  4) итог сетки всегда равен количеству (largest remainder сохраняется);
  5) на демо снапшот несёт дни наличия, а «Заказ» и мастер делят одинаково.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DB_PATH = ROOT / "test_size_presence.db"
for suffix in ("", "-wal", "-shm"):
    p = Path(str(DB_PATH) + suffix)
    if p.exists():
        p.unlink()
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["SCHEDULER_ENABLED"] = "0"

from fastapi.testclient import TestClient  # noqa: E402

from app import analytics  # noqa: E402
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


print("\n1. Размер, которого долго не было")
sizes = {
    "S": {"stock": 5, "sold365": 40, "days365": 365, "hist365": 365},
    "M": {"stock": 0, "sold365": 20, "days365": 65, "hist365": 365},
}
split = analytics.size_split(sizes, 30)
old = analytics.size_split({k: {"stock": v["stock"], "sold365": v["sold365"]} for k, v in sizes.items()}, 30)
check("по голым продажам M получал треть (10 из 30)", old == {"S": 20, "M": 10}, str(old))
check("по темпу в днях наличия M получает больше S (0,31 против 0,11 шт/день)",
      split["M"] > split["S"] and sum(split.values()) == 30, str(split))

print("\n2-4. Края")
check("без days365 вес = продажи за год (старый формат и суммы по позициям)",
      analytics.size_weight({"sold365": 12}) == 12)
check("продажи без дней наличия: делим на даты истории позиции",
      abs(analytics.size_weight({"sold365": 10, "days365": 0, "hist365": 50}) - 0.2) < 1e-9)
check("нет продаж: вес 0", analytics.size_weight({"sold365": 0, "days365": 100, "hist365": 365}) == 0)
for total in (1, 7, 31, 100):
    check(f"итог сетки = {total}", sum(analytics.size_split(sizes, total).values()) == total)

print("\n5. Демо: снапшот и экраны")
with TestClient(app, headers={"X-Oborot-CSRF": "1"}) as c:
    c.post("/register", data={"name": "T", "email": "sz@test.io",
                              "password": "secret123", "org_name": "Сетка"})
    c.post("/api/connect/demo")
    rep = c.get("/api/replenish").json()["items"]
    with_sizes = [i for i in rep if i.get("sizes") and i.get("need")]
    check("в ответе «Заказа» у размеров есть дни наличия",
          with_sizes and all("days365" in v for i in with_sizes for v in i["sizes"].values()))
    check("сетка «Заказа» сходится с потребностью",
          all(sum(v["rec"] for v in i["sizes"].values()) == i["need"] for i in with_sizes))
    item = with_sizes[0]
    recomputed = analytics.size_split(item["sizes"], item["need"])
    calc = c.get("/api/sizes/calc", params={"product": item["base_name"], "qty": item["need"],
                                            "period": "12m"}).json()
    calc_split = {r["size"]: r["order_pure"] for r in calc.get("sizes", [])}
    want = analytics.size_split({s: dict(v) for s, v in item["sizes"].items()}, item["need"])
    check("калькулятор «Заказа позиции» (12 мес) делит так же, как «Заказ» (ревью Codex #95)",
          calc.get("split_matches_order_page") and all(calc_split.get(s, 0) == q for s, q in want.items()),
          f"{calc_split} {want}")
    check("пересборка сетки по ответу API даёт ту же сетку (после партии не расходится)",
          recomputed == {s: v["rec"] for s, v in item["sizes"].items()},
          f"{recomputed} {item['sizes']}")

for suffix in ("", "-wal", "-shm"):
    p = Path(str(DB_PATH) + suffix)
    if p.exists():
        p.unlink()
print(f"\nИТОГО: {OK} OK, {FAIL} FAIL")
sys.exit(1 if FAIL else 0)
