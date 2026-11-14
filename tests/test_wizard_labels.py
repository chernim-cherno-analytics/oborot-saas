# -*- coding: utf-8 -*-
"""Мастер называет базу своих сумм (WIZARD-LABELS-1, 14.11.2026).

Прогон пользователем: плашка «по открытым заказам запланировано 3 729 065 ₽»
и календарь денег «Открытые заказы 4 593 685 ₽» на одной странице, без
объяснения: плашка считает канал, календарь всю организацию. И ритм из
Настроек (60 дн) молча заменялся ритмом прошлого плана. Проверяем, что
страница называет обе базы. Запуск: python tests/test_wizard_labels.py
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DB_PATH = ROOT / "test_wizard_labels.db"
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
    c.post("/register", data={"name": "w", "email": "wiz@test.io",
                              "password": "secret123", "org_name": "Мастер"})
    html = c.get("/assistant").text
    check("плашка открытых заказов говорит «этого канала»", "открытым заказам этого канала" in html)
    check("плашка отсылает к календарю всех каналов", "календарь денег ниже считает все каналы" in html)
    check("колонка календаря «Открытые заказы (все каналы)»", "Открытые заказы (все каналы)" in html)
    check("подсказка ритма сравнивает с Настройками", "В Настройках указан заказ раз в" in html
          and "STATE.settingsCadence" in html)

for suffix in ("", "-wal", "-shm"):
    p = Path(str(DB_PATH) + suffix)
    if p.exists():
        p.unlink()
print(f"\nИТОГО: {OK} OK, {FAIL} FAIL")
sys.exit(1 if FAIL else 0)
