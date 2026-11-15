# -*- coding: utf-8 -*-
"""D-85..D-88 (решения владельца 15.11.2026 23:28). Было: «Одно число на позицию (D-72, шаг 1 спецификации «один поток», 14.11.2026).

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
DB_PATH = ROOT / "test_drafts_need.db"
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


def rep_items(c):
    return {i["base_name"]: i for i in c.get("/api/replenish").json()["items"]}


def rep_full(c):
    return c.get("/api/replenish").json()


def plan_items(c):
    plan = c.post("/api/order-plan/preview", json={
        "budget": 50_000_000, "strategy": "grow", "max_share_pct": 100}).json()
    return {i["base_name"]: i for i in plan["items"]}, plan


with TestClient(app, headers={"X-Oborot-CSRF": "1"}) as c:
    c.post("/register", data={"name": "T", "email": "drafts@test.io",
                              "password": "secret123", "org_name": "Черновики"})
    check("демо подключено", c.post("/api/connect/demo").status_code == 200)
    c.post("/api/settings", json={"rate_window": "season"})

    print("\n1. D-85: черновик уменьшает потребность на «Заказе» и в мастере одинаково")
    rep0 = rep_items(c)
    mas0, _ = plan_items(c)
    both = [b for b in rep0 if b in mas0 and rep0[b].get("need_raw", rep0[b]["need"]) >= 10]
    check("есть позиция с потребностью от 10 шт", bool(both), str(len(rep0)))
    b = both[0]
    n0 = rep0[b].get("need_raw", rep0[b]["need"])
    m0 = mas0[b]["need"]
    r = c.post("/api/orders", json={"name": "Черновик", "allow_duplicate": True,
                                    "items": [{"base_name": b, "qty": 4}]})
    check("черновик создан", r.status_code == 200, r.text[:200])
    oid = r.json().get("id")
    rep1 = rep_items(c)
    mas1, _ = plan_items(c)
    check("«Заказ»: потребность −4, пометка в строке",
          rep1[b].get("need_raw", rep1[b]["need"]) == n0 - 4 and rep1[b].get("in_drafts") == 4,
          f"{n0} -> {rep1[b].get('need_raw', rep1[b]['need'])}, in_drafts={rep1[b].get('in_drafts')}")
    check("мастер: потребность −4, пометка в строке",
          mas1[b]["need"] == m0 - 4 and mas1[b].get("in_drafts") == 4, f"{m0} -> {mas1[b]['need']}")
    check("«Едет» черновик не трогает", rep1[b]["ordered"] == rep0[b]["ordered"],
          f"{rep0[b]['ordered']} -> {rep1[b]['ordered']}")
    common = [x for x in rep1 if x in mas1]
    diff = [(x, rep1[x].get("need_raw", rep1[x]["need"]), mas1[x]["need"]) for x in common
            if rep1[x].get("need_raw", rep1[x]["need"]) != mas1[x]["need"]]
    check("одно число держится и с черновиком", not diff, str(diff[:5]))

    print("\n2. Черновик закрывает потребность целиком")
    c.delete(f"/api/orders/{oid}")
    check("после удаления черновика потребность вернулась",
          rep_items(c)[b].get("need_raw", rep_items(c)[b]["need"]) == n0)
    r = c.post("/api/orders", json={"name": "Целиком", "allow_duplicate": True,
                                    "items": [{"base_name": b, "qty": n0 + 5}]})
    oid2 = r.json().get("id")
    full = rep_full(c)
    check("позиция ушла из списка «Заказа»", b not in {i["base_name"] for i in full["items"]})
    check("с причиной «уже в черновике заказа»",
          any(e["base_name"] == b and "черновике" in e["reason"] for e in full.get("excluded") or []),
          str([e for e in full.get("excluded") or [] if e["base_name"] == b]))
    mas2, plan2 = plan_items(c)
    check("в мастере её тоже нет", b not in mas2)
    check("мастер считает такие позиции", (plan2.get("review") or {}).get("in_drafts_count", 0) >= 1,
          str((plan2.get("review") or {}).get("in_drafts_count")))

    print("\n3. Отправленный в производство заказ уже не черновик")
    c.post(f"/api/orders/{oid2}/status", json={"status": "sent"})
    rep3 = rep_full(c)
    row = {i["base_name"]: i for i in rep3["items"]}.get(b)
    check("пометки «в черновике» больше нет", row is None or not row.get("in_drafts"), str(row and row.get("in_drafts")))
    check("причины «в черновике» больше нет",
          not any(e["base_name"] == b and "черновике" in e["reason"] for e in rep3.get("excluded") or []))
    c.delete(f"/api/orders/{oid2}")

    print("\n4. D-88: накладные на «Заказе», как в мастере")
    check("накладные 20 %", c.post("/api/settings", json={"overhead_pct": 20}).status_code == 200)
    rep4 = rep_full(c)
    mas4, _ = plan_items(c)
    check("корень ответа знает накладные", float(rep4.get("overhead_pct") or 0) == 20, str(rep4.get("overhead_pct")))
    withc = [i for i in rep4["items"] if (i.get("cost_base") or 0) > 0]
    check("есть позиции с себестоимостью", bool(withc))
    bad = [i["base_name"] for i in withc if i["cost_price"] != round(i["cost_base"] * 1.2)]
    check("себестоимость «Заказа» = себестоимость × 1,2", not bad, str(bad[:3]))
    diff = [(i["base_name"], i["cost_price"], mas4[i["base_name"]]["cost_price"]) for i in withc
            if i["base_name"] in mas4 and abs(i["cost_price"] - mas4[i["base_name"]]["cost_price"]) > 1]
    check("на «Заказе» и в мастере одна себестоимость штуки", not diff, str(diff[:3]))
    c.post("/api/settings", json={"overhead_pct": 0})

    print("\n5. D-87: «дыра поставки» одна на «Заказе» и в мастере")
    rep5 = rep_items(c)
    mas5, _ = plan_items(c)
    common = [x for x in rep5 if x in mas5 and not rep5[x]["ordered"]]
    holes = [x for x in common if rep5[x]["gap_days"] > 0]
    check("есть позиции с дырой", bool(holes), str(len(common)))
    diff = [(x, rep5[x]["gap_days"], mas5[x]["gap_days"]) for x in common if rep5[x]["gap_days"] != mas5[x]["gap_days"]]
    check("дыра совпадает у каждой общей позиции без «Едет»", not diff, f"{len(diff)} из {len(common)}: {diff[:5]}")

for suffix in ("", "-wal", "-shm"):
    p = Path(str(DB_PATH) + suffix)
    if p.exists():
        p.unlink()
print(f"\nИТОГО: {OK} OK, {FAIL} FAIL")
