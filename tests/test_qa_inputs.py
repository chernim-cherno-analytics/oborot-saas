# -*- coding: utf-8 -*-
"""Краевые входы API (QA-прогон 14.11.2026, утро). Формулы не меняются.

Что доказывается:
  1) огромное «Едет» отклоняется 422, «Прогноз» не падает;
  2) встречное слияние категорий отклоняется, категории не меняются местами;
  3) id больше INTEGER SQLite: 422, а не 500 (Поставки, применение плана);
  4) мастер: eta_date 9999 года и мусор отклоняются 422, огромный production_id 422;
  5) заказ: дата прихода 1901 и 9999 года отклоняется;
  6) «Оборот за период»: несуществующая дата 422 с понятным текстом;
  7) калькулятор размеров: несуществующая дата и неизвестный режим 422;
  8) производство с пустым именем 422;
  9) черновик ростовки: несуществующий размер 422.

Запуск: python tests/test_qa_inputs.py
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "test_qa_inputs.db"
for suffix in ("", "-wal", "-shm"):
    p = Path(str(DB_PATH) + suffix)
    if p.exists():
        p.unlink()
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["SCHEDULER_ENABLED"] = "0"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

OK = FAIL = 0
BIG = 99999999999999999999


def check(name, cond, detail=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  OK   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


with TestClient(app, headers={"X-Oborot-CSRF": "1"}, raise_server_exceptions=False) as a:
    a.post("/register", data={"name": "A", "email": "qa-in@test.io",
                              "password": "secret123", "org_name": "Бренд QA"})
    r = a.post("/api/connect/demo")
    check("демо-данные подключены", r.status_code == 200, r.text[:200])
    rep = a.get("/api/replenish").json()
    item = next(i for i in rep["items"] if i.get("sizes"))
    base = item["base_name"]

    print("\n1. Огромное «Едет»")
    r = a.post("/api/ordered", json={"base_name": base, "qty": 10_000_000})
    check("10 млн шт в «Едет» отклонено 422", r.status_code == 422, str(r.status_code))
    r = a.post("/api/ordered/add", json={"base_name": base, "qty": 1e15})
    check("прибавка 1e15 отклонена 422", r.status_code == 422, str(r.status_code))
    r = a.post("/api/ordered", json={"base_name": base, "qty": 1_000_000})
    check("1 млн шт (граница) принят", r.status_code == 200, r.text[:200])
    r = a.get("/api/forecast")
    check("«Прогноз» после границы не падает", r.status_code == 200, r.text[:200])
    a.post("/api/ordered", json={"base_name": base, "qty": 0})

    print("\n2. Встречное слияние категорий")
    cats = sorted({i.get("category") for i in rep["items"] if i.get("category")})
    c1, c2, c3 = cats[0], cats[1], cats[2]
    r = a.post("/api/categories/merge", json={"from_category": c1, "to_category": c2})
    check("первое слияние принято", r.status_code == 200, r.text[:200])
    r = a.post("/api/categories/merge", json={"from_category": c2, "to_category": c1})
    check("встречное слияние отклонено 422", r.status_code == 422, r.text[:200])
    r = a.post("/api/categories/merge", json={"from_category": c3, "to_category": c1})
    check("влить в уже влитую категорию нельзя", r.status_code == 422, r.text[:200])
    r = a.post("/api/categories/merge", json={"from_category": c2, "to_category": c3})
    check("цель, в которую уже влиты, саму влить нельзя", r.status_code == 422, r.text[:200])
    merges = a.get("/api/categories").json()
    check("в слияниях осталась одна связь", str(merges).count(c2) >= 1 and c3 not in str(merges.get("merges", merges)),
          str(merges)[:300])
    r = a.post("/api/categories/merge", json={"from_category": c1, "to_category": ""})
    check("отмена слияния работает", r.status_code == 200, r.text[:200])

    print("\n3. id больше INTEGER")
    for path in (f"/api/supply/planning/sketches/{BIG}",
                 f"/api/supply/planning/sketches/{BIG}/thumb",
                 f"/api/supply/planning/events?entity=item&id={BIG}"):
        r = a.get(path)
        check(f"GET {path.split('/api')[1][:40]}: не 500", r.status_code in (404, 422), str(r.status_code))
    r = a.post(f"/api/supply/planning/materials/{BIG}/update", json={})
    check("материал с огромным id: не 500", r.status_code in (403, 404, 422), str(r.status_code))
    r = a.post(f"/api/order-plan/{BIG}/apply", json={})
    check("применение плана с огромным id: 422", r.status_code == 422, str(r.status_code))

    print("\n4. Мастер: краевые входы анкеты")
    r = a.post("/api/order-plan/preview", json={"eta_date": "9999-12-31"})
    check("eta 9999 года: 422", r.status_code == 422, str(r.status_code))
    r = a.post("/api/order-plan/preview", json={"eta_date": "garbage"})
    check("eta мусор: 422", r.status_code == 422, str(r.status_code))
    r = a.post("/api/order-plan/preview", json={"production_id": 10**20})
    check("огромный production_id: 422", r.status_code == 422, str(r.status_code))
    r = a.post("/api/order-plan/preview", json={"eta_date": ""})
    check("пустая eta по-прежнему значит «по сроку»", r.status_code == 200, r.text[:200])

    print("\n5. Заказ: дата прихода")
    for bad in ("1901-01-01", "9999-12-31"):
        r = a.post("/api/orders", json={"name": "qa", "eta_date": bad,
                                         "items": [{"base_name": base, "qty": 1}]})
        check(f"дата прихода {bad}: 422", r.status_code == 422, str(r.status_code))

    print("\n5б. Одна модель одной строкой")
    r = a.post("/api/orders", json={"name": "дубль строк", "allow_duplicate": True, "items": [
        {"base_name": base, "qty": 3, "sizes": {"S": 3}}, {"base_name": base, "qty": 4, "sizes": {"M": 4}}]})
    card = a.get(f"/api/orders/{r.json().get('id')}").json() if r.status_code == 200 else {}
    rows = [i for i in card.get("items", []) if i["base_name"] == base]
    check("две строки одной модели склеены в одну: 7 шт, S3 M4",
          len(rows) == 1 and rows[0]["qty"] == 7 and rows[0].get("sizes") == {"S": 3, "M": 4}, str(rows)[:200])
    r = a.post("/api/orders", json={"name": "дубль без размеров", "allow_duplicate": True, "items": [
        {"base_name": base, "qty": 3}, {"base_name": base, "qty": 4}]})
    card = a.get(f"/api/orders/{r.json().get('id')}").json() if r.status_code == 200 else {}
    rows = [i for i in card.get("items", []) if i["base_name"] == base]
    check("две безразмерные строки: одна строка 7 шт", len(rows) == 1 and rows[0]["qty"] == 7, str(rows)[:200])
    r = a.post("/api/orders", json={"name": "смесь", "allow_duplicate": True, "items": [
        {"base_name": base, "qty": 3, "sizes": {}}, {"base_name": base, "qty": 4, "sizes": {"M": 4}}]})
    check("строка без размеров + строка с размерами: 422, а не потеря 3 шт",
          r.status_code == 422 and "разбивка" in r.text, f"{r.status_code} {r.text[:200]}")
    r = a.post("/api/orders", json={"name": "лимит", "allow_duplicate": True, "items": [
        {"base_name": base, "qty": 60000}, {"base_name": base, "qty": 60000}]})
    check("60000+60000 одной модели: 422, а не молча 100000", r.status_code == 422, f"{r.status_code} {r.text[:200]}")
    r = a.post("/api/orders", json={"name": "лимит размеры", "allow_duplicate": True, "items": [
        {"base_name": base, "qty": 60000, "sizes": {"S": 60000}},
        {"base_name": base, "qty": 60000, "sizes": {"M": 60000}}]})
    check("размеры 60000+60000: 422", r.status_code == 422, f"{r.status_code} {r.text[:200]}")

    print("\n6. «Оборот за период»")
    r = a.get("/api/revenue?date_from=2026-02-30&date_to=2026-03-01")
    check("30 февраля: 422", r.status_code == 422, str(r.status_code))
    r = a.get("/api/revenue?date_from=2026-13-01&date_to=2026-12-31")
    check("13-й месяц: текст про неверную дату", r.status_code == 422 and "Неверная дата" in r.text, r.text[:200])

    print("\n7. Калькулятор размеров")
    r = a.get("/api/sizes/calc", params={"product": base, "arrival": "2026-02-30"})
    check("несуществующая дата прихода: 422", r.status_code == 422, str(r.status_code))
    r = a.get("/api/sizes/calc", params={"product": base, "mode": "zz"})
    check("неизвестный режим: 422", r.status_code == 422, str(r.status_code))
    r = a.get("/api/sizes/calc", params={"product": base, "mode": "pure"})
    check("режим pure работает", r.status_code == 200, r.text[:200])

    print("\n8. Производство с пустым именем")
    r = a.post("/api/productions", json={"name": "   "})
    check("имя из пробелов: 422", r.status_code == 422, str(r.status_code))
    pid = a.post("/api/productions", json={"name": "Нулевой срок"}).json().get("id")
    r = a.post(f"/api/productions/{pid}/setup", json={"stages": [
        {"name": "Ткань", "lead_days": 0, "cost_share": 0.5, "prepay_share": 0.5},
        {"name": "Пошив", "lead_days": 0, "cost_share": 0.5, "prepay_share": 0.5}]})
    check("все этапы со сроком 0: 422 (QA 15.11)", r.status_code == 422 and "срок" in r.text,
          f"{r.status_code} {r.text[:200]}")
    r = a.post(f"/api/productions/{pid}/setup", json={"stages": [
        {"name": "Ткань", "lead_days": 0, "cost_share": 0.5, "prepay_share": 0.5},
        {"name": "Пошив", "lead_days": 20, "cost_share": 0.5, "prepay_share": 0.5}]})
    check("этап 0 + этап 20: принимается", r.status_code == 200, f"{r.status_code} {r.text[:200]}")

    print("\n8б. Excel «Заказа» называет сезонную поправку (D-79)")
    import io as _io
    from openpyxl import load_workbook as _lw
    a.post("/api/settings", json={"rate_window": "year"})
    wb = _lw(_io.BytesIO(a.get("/api/export/replenish.xlsx").content))
    head = " ".join(str(c.value or "") for ws in wb.worksheets for row in ws.iter_rows(max_row=3) for c in row)
    check("окно «год»: в шапке «с поправкой на сезон»", "поправкой на сезон" in head, head[:300])

    print("\n9. Черновик ростовки")
    r = a.post("/api/replenish-draft", json={"base_name": base, "sizes": {"НЕТРАЗМЕРА": 5}})
    check("несуществующий размер: 422", r.status_code == 422, str(r.status_code))
    real = next(iter(item["sizes"]))
    r = a.post("/api/replenish-draft", json={"base_name": base, "sizes": {real: 5}})
    check("настоящий размер сохраняется", r.status_code == 200, r.text[:200])
    a.post("/api/replenish-draft/reset", json={"base_name": base})

print(f"\nИТОГО: {OK} OK, {FAIL} FAIL")
for suffix in ("", "-wal", "-shm"):
    p = Path(str(DB_PATH) + suffix)
    if p.exists():
        p.unlink()
sys.exit(1 if FAIL else 0)
