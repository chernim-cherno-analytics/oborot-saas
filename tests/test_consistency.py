# -*- coding: utf-8 -*-
"""Числа, которые обязаны совпадать между экранами.

Зачем отдельный набор. Главный список `BUSINESS_LOGIC.md` §9 — «где логика
спорит сама с собой» — до сих пор был документом, а не проверкой: расхождения
находились чтением кода и жили годами. Здесь закреплены те из них, что уже
починены, — чтобы они не вернулись молча.

Проверяется:
  1) §9.1 — «Продано за 30 дней» уважает исключённые позиции. Это был
     единственный запрос снапшота без join'а к товарам: упаковка, сертификаты
     и пробники, выброшенные отовсюду ещё, оставались в карточке дашборда и
     в Telegram-дайджесте;
  2) ручной архив («в архив» на «Оборачиваемости») одинаково меняет «выручку
     за год» и на дашборде, и на странице «Оборот» — раньше один клик разводил
     две одинаково подписанные цифры на сотни тысяч рублей;
  3) §9.10 — «Оборот» знает пользовательские категории: слияние и перенос
     позиции видны и в блоке категорий, и в помесячном ряду, и набор категорий
     совпадает с «Оборачиваемостью»;
  4) §9.5 — срок производства берётся по ИТОГОВОМУ распределению. Позиция,
     отданная цеху ПРАВИЛОМ, обязана считаться так же, как отданная руками:
     раньше правило меняло подпись срока на экране, но не расчёт;
  5) признак `lead_time_by_production` честен — раньше он подтверждал то,
     чего не было, и страница по нему переключала подписи.

Запуск из корня репозитория:  python tests/test_consistency.py
"""
import datetime
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "test_consistency.db"
APP_PORT = int(os.environ.get("OBOROT_TEST_PORT", "8814"))

os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["SCHEDULER_ENABLED"] = "0"

if DB_PATH.exists():
    DB_PATH.unlink()

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from app.main import app as oborot_app  # noqa: E402


class ServerThread:
    def __init__(self, asgi_app, port: int):
        self.config = uvicorn.Config(asgi_app, host="127.0.0.1", port=port,
                                     log_level="warning")
        self.server = uvicorn.Server(self.config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self):
        self.thread.start()
        deadline = time.time() + 15
        while time.time() < deadline:
            if self.server.started:
                return
            time.sleep(0.05)
        raise RuntimeError(f"сервер на порту {self.config.port} не поднялся")

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=10)


PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        PASS.append(name)
        print(f"  OK   {name}" + (f"  [{detail}]" if detail else ""))
    else:
        FAIL.append(name)
        print(f"  FAIL {name}  {detail}")


def main() -> int:
    srv = ServerThread(oborot_app, APP_PORT)
    srv.start()
    try:
        return run()
    finally:
        srv.stop()
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(DB_PATH) + suffix)
            if p.exists():
                p.unlink()


def run() -> int:
    c = httpx.Client(headers={"X-Oborot-CSRF": "1"},
                     base_url=f"http://127.0.0.1:{APP_PORT}", timeout=120.0)
    today = datetime.date.today()
    frm = (today - datetime.timedelta(days=364)).isoformat()
    REV = f"/api/revenue?date_from={frm}&date_to={today.isoformat()}"

    print("\n== Подготовка ==")
    r = c.post("/register", data={"name": "Владелец", "email": "cons@test.io",
                                  "password": "secret123", "org_name": "Бренд-С"})
    check("регистрация", r.status_code in (200, 302, 303), f"status={r.status_code}")
    check("демо-данные", c.post("/api/connect/demo").status_code == 200)
    victim = c.get("/api/turnover").json()["items"][0]["base_name"]

    print("\n== §9.1: «Продано за 30 дней» уважает исключения ==")
    s0 = c.get("/api/summary").json()
    c.post("/api/exclusions", json={"base_name": victim, "excluded": True})
    s1 = c.get("/api/summary").json()
    check("исключение позиции меняет число проданного за 30 дней",
          s1["sold_30d_qty"] != s0["sold_30d_qty"],
          f'{s0["sold_30d_qty"]} -> {s1["sold_30d_qty"]}')
    check("и выручку за 30 дней тоже",
          s1["sold_30d_rev"] != s0["sold_30d_rev"],
          f'{s0["sold_30d_rev"]} -> {s1["sold_30d_rev"]}')
    check("исключённое ушло, а не прибавилось",
          s1["sold_30d_qty"] < s0["sold_30d_qty"] and s1["sold_30d_rev"] < s0["sold_30d_rev"])
    c.post("/api/exclusions", json={"base_name": victim, "excluded": False})
    s2 = c.get("/api/summary").json()
    check("отмена исключения возвращает число",
          s2["sold_30d_qty"] == s0["sold_30d_qty"], f'{s2["sold_30d_qty"]}')

    print("\n== Архив: одна цифра «выручка за год» на двух экранах ==")
    r0 = c.get(REV).json()
    # Допуск, а не строгое равенство. Две суммы складываются в разном порядке
    # (одна группирует по базовому имени в SQL, другая по имени и размеру, потом
    # в Python), и на боевом каталоге они расходятся на единицы рублей из-за
    # порядка сложения float. Проверено на проде: 130 368 021 против
    # 130 368 024 при нулевом числе архивных позиций. Требовать точного
    # совпадения значило бы поймать не расхождение экранов, а арифметику
    # плавающей точки.
    #
    # Отдельно: «Оборот» намеренно НЕ исключает позиции, архивные в МойСкладе.
    # Дашборд считает живой ассортимент, а «Оборот» — исторический отчёт:
    # товар, снятый с производства сегодня, заработал свои деньги в прошлом
    # году, и терять их нельзя. Ручной архив («в архив» на «Оборачиваемости») —
    # другое дело: это решение владельца скрыть позицию, и его уважают оба.
    diff = abs(r0["total_rev"] - s0["money"]["revenue_year"])
    check("до архива обе величины сходятся (с точностью до сложения float)",
          diff <= max(10, r0["total_rev"] * 1e-6),
          f'{r0["total_rev"]} vs {s0["money"]["revenue_year"]} (Δ {diff})')
    c.post("/api/hidden", json={"base_name": victim, "hidden": True})
    r1 = c.get(REV).json()
    s3 = c.get("/api/summary").json()
    check("архив изменил выручку на «Обороте»", r1["total_rev"] != r0["total_rev"],
          f'{r0["total_rev"]} -> {r1["total_rev"]}')
    # А вот ДЕЛЬТА обязана совпасть: это и есть проверяемый инвариант —
    # один и тот же клик двигает обе одинаково подписанные цифры одинаково.
    d_rev = r0["total_rev"] - r1["total_rev"]
    d_sum = s0["money"]["revenue_year"] - s3["money"]["revenue_year"]
    check("и ровно на столько же на дашборде",
          abs(d_rev - d_sum) <= max(10, d_rev * 1e-6),
          f'дельты: {d_rev} vs {d_sum}')
    check("архивная позиция исчезла из списка «Оборота»",
          not any(x["base_name"] == victim for x in r1["items"]))
    c.post("/api/hidden", json={"base_name": victim, "hidden": False})

    print("\n== §9.10: «Оборот» знает пользовательские категории ==")
    cats0 = {x["category"]: x["rev"] for x in c.get(REV).json()["categories"]}
    names = list(cats0)
    check("категорий больше одной", len(names) >= 2, str(len(names)))
    src, dst = names[1], names[0]
    c.post("/api/categories/merge", json={"from_category": src, "to_category": dst})
    rr = c.get(REV).json()
    cats1 = {x["category"]: x["rev"] for x in rr["categories"]}
    check("слитая категория исчезла из «Оборота»", src not in cats1, str(list(cats1))[:120])
    check("её деньги переехали целиком, а не потерялись",
          cats1.get(dst) == cats0[src] + cats0[dst],
          f'{cats1.get(dst)} vs {cats0[src] + cats0[dst]}')
    check("помесячный ряд слит так же",
          not any(src in m["by_category"] for m in rr["monthly"]))
    turnover_cats = {x["category"] for x in c.get("/api/turnover").json()["items"]}
    check("набор категорий совпал с «Оборачиваемостью»",
          set(cats1) == turnover_cats,
          f'«Оборот»: {len(cats1)}, «Оборачиваемость»: {len(turnover_cats)}')
    c.post("/api/categories/merge", json={"from_category": src, "to_category": ""})

    c.post("/api/categories/override",
           json={"base_name": victim, "category": "Особая полка"})
    rr = c.get(REV).json()
    check("перенос отдельной позиции виден в «Обороте»",
          any(x["category"] == "Особая полка" for x in rr["categories"]))
    check("и в помесячном ряду тоже",
          any("Особая полка" in m["by_category"] for m in rr["monthly"]))
    moved = next(x for x in rr["items"] if x["base_name"] == victim)
    check("у самой позиции категория новая", moved["category"] == "Особая полка",
          moved["category"])
    c.post("/api/categories/override", json={"base_name": victim, "category": ""})

    print("\n== §9.5: срок производства — по итоговому распределению ==")
    pid = c.post("/api/productions", json={"name": "Китай"}).json()["id"]
    c.post(f"/api/productions/{pid}", json={"name": "Китай", "lead_time_days": 90})
    rows = c.get("/api/replenish").json()["items"]
    # Позиция с ненулевым прогнозным остатком: только на ней срок влияет на
    # потребность (при нулевом остатке обе ветки дают одно и то же).
    target = next((x["base_name"] for x in rows if (x.get("proj_stock") or 0) > 0),
                  rows[0]["base_name"])

    c.post("/api/productions/assign", json={"base_name": target, "production_id": pid})
    by_hand = next(x for x in c.get("/api/replenish").json()["items"]
                   if x["base_name"] == target)
    c.post("/api/productions/assign", json={"base_name": target, "production_id": None})

    vals = [v["value"] for v in
            c.get("/api/productions/assign-sources").json()["sources"]["folder"]["values"]]
    res = c.post("/api/productions/assign-rule",
                 json={"assign_source": "folder",
                       "assign_map": {v: pid for v in vals}}).json()
    check("правило распределило позиции", res.get("assigned", 0) > 0, str(res)[:120])
    by_rule = next(x for x in c.get("/api/replenish").json()["items"]
                   if x["base_name"] == target)

    check("срок цеха виден в обоих случаях",
          by_hand["lead_time_days"] == 90 and by_rule["lead_time_days"] == 90,
          f'рукой {by_hand["lead_time_days"]}, правилом {by_rule["lead_time_days"]}')
    check("прогнозный остаток посчитан одинаково",
          by_hand["proj_stock"] == by_rule["proj_stock"],
          f'{by_hand["proj_stock"]} vs {by_rule["proj_stock"]}')
    check("и потребность тоже — назначение правилом не занижает заказ",
          by_hand["need"] == by_rule["need"],
          f'рукой {by_hand["need"]}, правилом {by_rule["need"]}')
    check("признак «считаем по сроку подрядчика» честен",
          c.get("/api/replenish").json().get("lead_time_by_production") is True)

    # …и наоборот: пока сроки цехов равны общему, признак не должен врать.
    c.post(f"/api/productions/{pid}", json={"name": "Китай", "lead_time_days": 0})
    check("без своего срока признак снимается",
          c.get("/api/replenish").json().get("lead_time_by_production") is False,
          str(c.get("/api/replenish").json().get("lead_time_by_production")))

    print("\n== §9.5: срок канала одинаков в мастере и на «Заказе» ==")
    # Третий способ считать срок: мастер собирал его как сумму сроков этапов,
    # а запасным значением для канала БЕЗ явных этапов брал общий срок
    # организации. Канал со сроком 90 давал мастеру lead_days = 45 и дату
    # прихода «сегодня + 45», хотя «Заказ» для той же позиции писал 90.
    # Предыдущий блок снимал срок у канала — возвращаем его.
    c.post(f"/api/productions/{pid}", json={"name": "Китай", "lead_time_days": 90})
    plan_lead = c.post("/api/order-plan/preview",
                       json={"budget": 2_000_000, "budget_scope": "full",
                             "cadence_days": 30, "safety_days": 14,
                             "production_id": pid}).json()
    plan_lead = plan_lead.get("plan") or plan_lead
    page_lead = c.get("/api/replenish").json()["items"][0]["lead_time_days"]
    check("мастер считает по сроку канала, а не по общему",
          plan_lead.get("lead_days") == 90, str(plan_lead.get("lead_days")))
    check("и это тот же срок, что на «Заказе»",
          plan_lead.get("lead_days") == page_lead,
          f'мастер {plan_lead.get("lead_days")} vs «Заказ» {page_lead}')

    print("\n== §9.6: минимальная партия одна на обоих экранах ==")
    # Два поля с одним смыслом на одной сущности: `moq` вводится на странице
    # «Заказ», `moq_units` — в Настройках. Каждый экран читал своё, и владелец,
    # заполнив одно, не влиял на второй. Цена: план мастера 2 905 500 ₽ против
    # 3 664 596 ₽ (+759 096 ₽), 29 позиций из 34 шли по 12–36 шт при партии 50 —
    # такой заказ фабрика не примет.
    c.post(f"/api/productions/{pid}", json={"name": "Китай", "moq": 50,
                                            "lead_time_days": 90})
    prods = c.get("/api/productions").json()
    rows = prods.get("items") or prods.get("productions") or prods
    row = next(x for x in rows if x.get("id") == pid)
    check("введённая партия видна обоим экранам одним числом",
          row.get("moq") == 50 and row.get("moq_units") == 50, str(row)[:140])

    rep = [x for x in c.get("/api/replenish").json()["items"] if (x.get("need") or 0) > 0]
    below_page = [x["base_name"] for x in rep if x["need"] < 50]
    check("«Заказ» не предлагает меньше минимальной партии",
          not below_page, f"нарушают: {below_page[:3]}")

    plan = c.post("/api/order-plan/preview",
                  json={"budget": 3_000_000, "budget_scope": "full",
                        "cadence_days": 30, "safety_days": 14,
                        "production_id": pid}).json()
    plan = plan.get("plan") or plan
    below_master = [i["base_name"] for i in plan["items"] if i["qty"] < 50]
    check("мастер тоже не предлагает меньше минимальной партии",
          not below_master, f"нарушают: {below_master[:3]}")
    check("в плане вообще есть позиции", len(plan["items"]) > 0, str(len(plan["items"])))

    # И обратно: партия, введённая в Настройках, обязана дойти до «Заказа».
    c.post(f"/api/productions/{pid}/setup", json={"moq_units": 80})
    rep2 = [x for x in c.get("/api/replenish").json()["items"] if (x.get("need") or 0) > 0]
    below2 = [x["base_name"] for x in rep2 if x["need"] < 80]
    check("партия из Настроек доходит до страницы «Заказ»",
          not below2, f"нарушают: {below2[:3]}")

    print(f"\nИтого: {len(PASS)} OK, {len(FAIL)} FAIL")
    for name in FAIL:
        print(f"  FAIL {name}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
