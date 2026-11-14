# -*- coding: utf-8 -*-
"""Файл для цеха: частичная сетка размеров называется (реестр NIGHT-14-11).

Что доказывается:
  1) позиция без размеров вовсе названа в «Без размерной сетки»;
  2) позиция, у которой сумма по размерам меньше или больше количества,
     названа в «Сетка не сходится» с обоими числами;
  3) позиция с полной сеткой не названа нигде;
  4) срок канала по этапам держит тот же коридор 0..365 на этап, что мастер.

Запуск: python tests/test_grid_mismatch.py
"""
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from openpyxl import load_workbook  # noqa: E402

from app import order_planner  # noqa: E402
from app.export_xlsx import order_workbook  # noqa: E402
from app.models import Production  # noqa: E402

OK = FAIL = 0


def check(name, cond, detail=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  OK   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


order = {"id": 7, "name": "Тест", "status": "draft", "created_at": "2026-11-14",
         "items": [
             {"base_name": "Полная", "qty": 6, "sizes": {"S": 3, "M": 3}, "cost": 100},
             {"base_name": "Неполная", "qty": 10, "sizes": {"S": 4}, "cost": 100},
             {"base_name": "Лишняя", "qty": 2, "sizes": {"S": 2, "M": 1}, "cost": 100},
             {"base_name": "Пустая", "qty": 5, "sizes": {}, "cost": 100},
         ]}
wb = order_workbook("Бренд", order, "Цех", [], size_key=lambda s: s)
buf = io.BytesIO()
wb.save(buf)
rows = [[c.value for c in r] for r in load_workbook(io.BytesIO(buf.getvalue()))["Заказ"].iter_rows()]
info = {str(r[0]): str(r[1]) for r in rows if r[0] in ("Без размерной сетки", "Сетка не сходится")}

print("\n1. Сетка размеров в файле для цеха")
check("без размеров названа отдельно", "Пустая (5 шт)" in info.get("Без размерной сетки", ""), str(info))
check("неполная сетка названа с числами",
      "Неполная (по размерам 4 из 10 шт)" in info.get("Сетка не сходится", ""), str(info))
check("лишние штуки по размерам тоже названы",
      "Лишняя (по размерам 3 из 2 шт)" in info.get("Сетка не сходится", ""), str(info))
check("полная сетка не названа нигде", all("Полная" not in v for v in info.values()), str(info))

print("\n2. Срок канала по этапам, коридор на этап")
p = Production(name="Канал", lead_time_days=30)
p.stages_json = '[{"name": "Ткань", "lead_days": 500}, {"name": "Пошив", "lead_days": 20}]'
planner = order_planner.lead_days(order_planner.normalize_stages(p.stages, 30))
check("«Заказ» и мастер дают один срок при этапе длиннее года",
      p.effective_lead_days() == planner == 385, f"{p.effective_lead_days()} {planner}")

print(f"\nИтого: {OK} OK, {FAIL} FAIL")
sys.exit(1 if FAIL else 0)
