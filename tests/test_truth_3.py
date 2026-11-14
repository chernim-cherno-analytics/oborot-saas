# -*- coding: utf-8 -*-
"""Прогон пользователями на собранном main 14.11.2026 (TRUTH-3).

Доказывается:
  1) /api/productions отдаёт срок канала, по которому считают экраны
     (lead_effective = сумма этапов), и клиент берёт его, а не поле срока
     (было: окно оформления и «Заказ позиции» ставили приход на 20 дней раньше);
  2) «Заказ» учитывает минимум этапа (партия пошива), как мастер;
  3) мастер не подставляет новинки прошлого плана при автоматическом
     открытии, только по «Повторить»;
  4) «Бюджет» объясняет, чем отличается от мастера, формула в справке по D-65;
  5) на единственном тарифе нет значка «Популярный».
Синтетическая база, демо-данные.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DB_PATH = ROOT / "test_truth_3.db"
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
    c.post("/register", data={"name": "T", "email": "t3@test.io",
                              "password": "secret123", "org_name": "Правда-3"})
    check("демо подключено", c.post("/api/connect/demo").status_code == 200)
    main = c.get("/api/productions").json()["productions"][0]
    stages = [
        {"key": "fabric", "name": "Ткань", "lead_days": 30, "cost_share": 0.45, "prepay_share": 1.0, "min_units": 0},
        {"key": "sewing", "name": "Пошив", "lead_days": 35, "cost_share": 0.55, "prepay_share": 0.5, "min_units": 30},
    ]
    r = c.post(f"/api/productions/{main['id']}/setup", json={"stages": stages})
    check("этапы «ткань → пошив» с партией пошива 30 заданы", r.status_code == 200, r.text[:200])

    print("\n1. Срок канала для клиента")
    prod = next(p for p in c.get("/api/productions").json()["productions"] if p["id"] == main["id"])
    check("lead_effective = сумма этапов 65, этапы названы источником",
          prod.get("lead_effective") == 65 and prod.get("lead_from_stages") is True
          and prod.get("lead_time_days") == 0, str({k: prod.get(k) for k in ("lead_effective", "lead_from_stages", "lead_time_days")}))
    check("наибольший минимум этапов отдан клиенту", prod.get("stage_moq_max") == 30, str(prod.get("stage_moq_max")))
    rep_html = (ROOT / "templates" / "replenish.html").read_text(encoding="utf-8")
    sz_html = (ROOT / "templates" / "sizes.html").read_text(encoding="utf-8")
    check("«Заказ» (вкладка и окно оформления) берёт lead_effective",
          "function leadOf(p){ return (p&&(p.lead_effective||p.lead_time_days))||DEFAULT_LEAD; }" in rep_html)
    check("«Заказ позиции» берёт lead_effective",
          "prod.lead_effective||prod.lead_time_days||DEFAULT_LEAD" in sz_html)

    print("\n2. Минимум этапа на «Заказе»")
    rep = c.get("/api/replenish").json()["items"]
    below = [(i["base_name"], i["need"]) for i in rep if 0 < (i.get("need") or 0) < 30]
    check("ни одной строки «Заказа» ниже партии пошива 30", not below, str(below[:5]))
    check("строки, поднятые до партии, помечены", any(i.get("moq_applied") for i in rep))
    # Ревью Codex #102: пол партии позиции по её категории, и у позиций без потребности.
    stages_cat = [dict(stages[0]), dict(stages[1], min_units=0, min_by_category={"Футболки": 100})]
    c.post(f"/api/productions/{main['id']}/setup", json={"stages": stages_cat})
    prods = {p["base_name"]: p for p in c.get("/api/sizes/products").json()["products"]}
    shirts = [p for p in prods.values() if p.get("category") == "Футболки"]
    others = [p for p in prods.values() if p.get("category") not in ("Футболки", None)]
    check("«Заказ позиции»: пол 100 только у категории «Футболки», у остальных 0",
          shirts and others and all(p.get("moq") == 100 for p in shirts)
          and all(p.get("moq") == 0 for p in others),
          str([(p["base_name"], p.get("category"), p.get("moq")) for p in (shirts[:2] + others[:2])]))
    c.post(f"/api/productions/{main['id']}/setup", json={"stages": stages})
    plan = c.post("/api/order-plan/preview", json={"budget": 50_000_000, "strategy": "grow",
                                                   "max_share_pct": 100, "production_id": main["id"]}).json()
    pb = [(i["base_name"], i["qty"]) for i in plan["items"] if 0 < (i.get("qty") or 0) < 30]
    check("и в мастере ни одной строки ниже 30 (тот же пол)", not pb, str(pb[:5]))

print("\n3-5. Шаблоны")
a_html = (ROOT / "templates" / "assistant.html").read_text(encoding="utf-8")
check("мастер: новинки прошлого плана только по явному «Повторить»",
      "if(!auto){ (last.new_items||[]).forEach(function(nn){ addNew(nn.name, nn.qty, nn.cost); }); }" in a_html)
b_html = (ROOT / "templates" / "budget.html").read_text(encoding="utf-8")
check("«Бюджет» объясняет отличие от мастера и формулу по D-65",
      "Мастер заказа распределяет те же деньги иначе" in b_html and "− остаток − «едет»" not in b_html)
check("«Заказ» на телефоне прячет справочные колонки, «Заказать» остаётся на экране",
      "@media (max-width: 480px)" in rep_html and "td:nth-child(7):not([colspan])" in rep_html)
p_html = (ROOT / "templates" / "plans.html").read_text(encoding="utf-8")
check("значок «Популярный» только когда тарифов больше одного",
      "p.popular && (data.plans || []).length > 1" in p_html)

for suffix in ("", "-wal", "-shm"):
    p = Path(str(DB_PATH) + suffix)
    if p.exists():
        p.unlink()
print(f"\nИТОГО: {OK} OK, {FAIL} FAIL")
sys.exit(1 if FAIL else 0)
