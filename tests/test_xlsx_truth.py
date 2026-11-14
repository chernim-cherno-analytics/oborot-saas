# -*- coding: utf-8 -*-
"""Excel «Что заказать» не выдаёт неполную сумму за полную (XLSX-TRUTH-1).

Прогон пользователем 14.11: на экране «СУММА НЕПОЛНАЯ: 2 поз. (30 шт) без
себестоимости», в Excel у этих строк «Сумма заказа = 0», примечание пустое,
итог без пометки. Проверяем на синтетической книге.
Запуск: python tests/test_xlsx_truth.py
"""
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from openpyxl import load_workbook  # noqa: E402

from app import export_xlsx  # noqa: E402

OK = FAIL = 0


def check(name, cond, detail=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  OK   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


def item(name, need, cost):
    return {"base_name": name, "category": "Футболки", "cls": "good", "turnover": 1000,
            "rate": 1.0, "cs": 5, "ordered": 0, "wos": 1.0, "stockout_date": None,
            "need": need, "cost_price": cost, "sizes": {}, "production_name": "Цех"}


data = {"items": [item("Есть цена", 10, 500), item("Пробник «Аромат»", 20, 0), item("Значок", 10, None)],
        "excluded": [{"base_name": "x"}] * 19, "rate_window": "year"}
wb = export_xlsx.replenish_workbook("Бренд", data)
buf = io.BytesIO()
wb.save(buf)
ws = load_workbook(io.BytesIO(buf.getvalue())).worksheets[0]
rows = [[c.value for c in r] for r in ws.iter_rows()]
by = {r[0]: r for r in rows if r[0] in ("Есть цена", "Пробник «Аромат»", "Значок")}
check("у строки с ценой сумма 5 000", by["Есть цена"][12] == 5000, str(by["Есть цена"][12]))
check("у строк без себестоимости сумма пустая, а не 0",
      by["Пробник «Аромат»"][12] is None and by["Значок"][12] is None)
check("примечание называет причину", "себестоимость не указана" in (by["Значок"][14] or ""))
total = next(r for r in rows if isinstance(r[0], str) and r[0].startswith("Итого"))
check("итог подписан как неполный", "сумма неполная: без 2 поз." in total[0], total[0])
check("итог суммы только по строкам с ценой", total[12] == 5000, str(total[12]))
note = next((r[0] for r in rows if isinstance(r[0], str) and "не попавшие в заказ" in r[0]), "")
check("«не вошло» считается в позициях", "(19 поз.)" in note, note)
print(f"\nИТОГО: {OK} OK, {FAIL} FAIL")
sys.exit(1 if FAIL else 0)
