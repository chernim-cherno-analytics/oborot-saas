# -*- coding: utf-8 -*-
"""Мастер не теряет минимальную партию канала (WIZARD-MOQ-1, 14.11.2026).

Находка прогона пользователем: поле «Мин. партия» по умолчанию 0, подпись
обещала «берётся минимальная партия производства: 20 шт», а браузер слал
moq_units: 0, и сервер (верно) читал это как «партии нет»: на 1,5 млн 18 из
37 строк были ниже партии фабрики, Excel для цеха уходил с 6 шт.
Доказывается:
  1) пустое поле (moq_units: null) берёт партию канала: ни одной строки
     плана в (0, партия);
  2) явный 0 по-прежнему значит «без партии на этот заказ» (решение аудита
     22.08: отключить партию на один заказ можно);
  3) шаблон мастера: поле пустое, пусто уходит как null, подпись называет
     оба смысла, повтор прошлого плана сохраняет явный выбор.
Формулы не меняются. Синтетическая база, демо-данные.
"""
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DB_PATH = ROOT / "test_wizard_moq.db"
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


MOQ = 20
with TestClient(app, headers={"X-Oborot-CSRF": "1"}) as c:
    c.post("/register", data={"name": "T", "email": "moq@test.io",
                              "password": "secret123", "org_name": "Партия"})
    check("демо подключено", c.post("/api/connect/demo").status_code == 200)
    prod = c.get("/api/productions").json()["productions"][0]
    r = c.post(f"/api/productions/{prod['id']}", json={"name": prod["name"], "moq": MOQ})
    check("у канала партия 20 шт", r.status_code == 200, r.text[:200])

    print("\n1. Пустое поле: партия канала")
    for budget in (300000, 1500000):
        plan = c.post("/api/order-plan/preview", json={"budget": budget, "moq_units": None, "production_id": prod["id"]}).json()
        qtys = [(i["base_name"], i["qty"]) for i in plan["items"] if i.get("qty")]
        below = [q for q in qtys if 0 < q[1] < MOQ]
        check(f"{budget:,} ₽: ни одной строки ниже партии".replace(",", " "),
              qtys and not below, f"строк {len(qtys)}, ниже партии {below[:5]}")
        check(f"{budget:,} ₽: в брифе партия канала".replace(",", " "),
              plan["brief"]["moq_units"] == MOQ and not plan["brief"]["moq_explicit"],
              str({k: plan["brief"].get(k) for k in ("moq_units", "moq_explicit")}))

    print("\n2. Явный 0: без партии на этот заказ")
    plan0 = c.post("/api/order-plan/preview", json={"budget": 300000, "moq_units": 0, "production_id": prod["id"]}).json()
    check("явный 0 остаётся явным выбором", plan0["brief"]["moq_units"] == 0
          and plan0["brief"]["moq_explicit"], str(plan0["brief"].get("moq_units")))

    print("\n2а. Метка версии поля сохраняется в брифе (для повтора старых планов)")
    plan2 = c.post("/api/order-plan/preview", json={"budget": 300000, "moq_units": None, "moq_ui": 2,
                                                    "production_id": prod["id"]}).json()
    check("новый мастер помечает бриф moq_ui=2", plan2["brief"].get("moq_ui") == 2, str(plan2["brief"].get("moq_ui")))
    check("старый бриф без метки: moq_ui пусто", plan0["brief"].get("moq_ui") is None)

print("\n3. Шаблон мастера")
html = (ROOT / "templates" / "assistant.html").read_text(encoding="utf-8")
check("поле партии по умолчанию пустое",
      re.search(r'id="moq"[^>]*value=""', html) is not None)
check("пустое поле уходит как null, а не 0",
      'moq_units: (String(document.getElementById("moq").value).trim() === "") ? null' in html)
check("подпись называет оба смысла: пусто = партия канала, 0 = без партии",
      "Пусто: партия производства" in html and "0: без минимальной партии на этот заказ" in html)
check("повтор прошлого плана сохраняет явный выбор и не превращает партию канала в явную",
      'var moqChosen = last.moq_explicit && (last.moq_ui === 2 || (last.moq_units || 0) > 0);' in html
      and 'moq_ui: 2,' in html)

for suffix in ("", "-wal", "-shm"):
    p = Path(str(DB_PATH) + suffix)
    if p.exists():
        p.unlink()
print(f"\nИТОГО: {OK} OK, {FAIL} FAIL")
sys.exit(1 if FAIL else 0)
