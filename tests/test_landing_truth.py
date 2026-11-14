# -*- coding: utf-8 -*-
"""Лендинг и тарифы не обещают того, чего нет (LANDING-TRUTH-1, 14.11.2026).

Сторож от возврата старых обещаний: API в тарифе «Про» (его нет), «токен
только на чтение» (запись заказа поставщику есть), «заказ на производство»
вместо документа «Заказ поставщику» (D-21), «доступ приостановится» вместо
режима просмотра (D-24). И что блок сравнения с «Управлением закупками»
МойСклада на месте. Запуск: python tests/test_landing_truth.py
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DB_PATH = ROOT / "test_landing_truth.db"
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


with TestClient(app) as c:
    html = c.get("/").text
    check("лендинг отвечает", "Оборот" in html)
    check("нет «Доступ к API»", "Доступ к API" not in html)
    check("нет «токен только на чтение»", "только на чтение" not in html)
    check("нет «заказ на производство» как документа в МойСкладе",
          "готовый заказ на производство прямо" not in html
          and "единственная запись — заказ на производство" not in html)
    check("запись названа «Заказ поставщику»", "«Заказ поставщику»" in html)
    check("после триала — режим просмотра, а не «доступ приостановится»",
          "доступ приостановится" not in html and "режим просмотра" in html)
    check("блок сравнения с «Управлением закупками» на месте",
          'id="vs-moysklad"' in html and "Управления закупками" in html)
    check("в блоке сравнения нет «единственный» и «точнее»",
          "единственн" not in html.split('id="vs-moysklad"')[1].split("</section>")[0]
          and "точнее" not in html.split('id="vs-moysklad"')[1].split("</section>")[0])
    c2 = TestClient(app, headers={"X-Oborot-CSRF": "1"})
    c2.post("/register", data={"name": "L", "email": "land@test.io",
                               "password": "secret123", "org_name": "Лендинг"})
    plans = c2.get("/api/plans").json()
    feats = [f for p in plans["plans"] for f in p["features"]]
    check("тарифы в приложении без API", not any("API" in f for f in feats), str(feats))

for suffix in ("", "-wal", "-shm"):
    p = Path(str(DB_PATH) + suffix)
    if p.exists():
        p.unlink()
print(f"\nИТОГО: {OK} OK, {FAIL} FAIL")
sys.exit(1 if FAIL else 0)
