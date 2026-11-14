# -*- coding: utf-8 -*-
"""Лендинг и тарифы не обещают того, чего нет (LANDING-TRUTH-1, 14.11.2026).

Сторож от возврата старых обещаний: API в тарифе «Про» (его нет), «токен
только на чтение» (запись заказа поставщику есть), «заказ на производство»
вместо документа «Заказ поставщику» (D-21), «доступ приостановится» вместо
режима просмотра (D-24). И что блок сравнения с «Управлением закупками»
МойСклада на месте. Запуск: python tests/test_landing_truth.py
"""
import os
import re
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
    # D-71 (14.11.2026): один тариф и платная настройка, лендинг и приложение совпадают.
    check("D-71: в приложении один тариф 9 900 и настройка 24 900",
          [p["price_month"] for p in plans["plans"]] == [9900]
          and plans.get("setup", {}).get("price") == 24900, str(plans.get("setup")))
    check("D-71: на лендинге нет «Старт» и «Про», есть настройка 24 900 с зачётом",
          "<h3>Старт</h3>" not in html and "<h3>Про</h3>" not in html
          and "24 900 ₽" in html and "засчитывается в оплату подписки" in html
          and not re.search(r"(?<![\d ])4 900", html) and "19 900" not in html)
    offer = c2.get("/legal/offer").text
    check("D-71: оферта называет тот же тариф и настройку",
          "19 900" not in offer and not re.search(r"(?<![\d ])4 900", offer) and "24 900" in offer)
    check("D-71: каждая позиция тарифа на лендинге есть в приложении",
          all(f in html for f in feats), str([f for f in feats if f not in html]))

for suffix in ("", "-wal", "-shm"):
    p = Path(str(DB_PATH) + suffix)
    if p.exists():
        p.unlink()
print(f"\nИТОГО: {OK} OK, {FAIL} FAIL")
sys.exit(1 if FAIL else 0)
