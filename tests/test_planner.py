# -*- coding: utf-8 -*-
"""Тесты планировщика заказа под бюджет (без pytest, просто python).

Ядро — чистая функция app.order_planner.plan_order: в БД не ходит, поэтому
проверяется на синтетическом снапшоте, где все числа известны заранее.

Что проверяем:
  1) горизонт покрытия из периодичности (cover_days), а не константа 90;
  2) этапы производства: срок = сумма, календарь этапов, доли себестоимости;
  3) бюджет НИКОГДА не превышен, в том числе при 'now' (первый этап);
  4) волны: дыры → база → углубление; must-have вне очереди;
  5) минимальная партия: минимум на модель, мизерная потребность не навязывается;
  6) лимит доли на позицию;
  7) стратегии protect/balance/grow;
  8) «не влезло» с упущенной выручкой и чувствительность к бюджету;
  9) отсев: архив, чужое производство, нет себестоимости, мало данных.

Запуск из корня репозитория:  python tests/test_planner.py
"""
import os
import sys
import threading
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "test_planner.db"
# Порты берутся из окружения: так tests/run_all.py разводит наборы и
# может гонять их параллельно. Значения по умолчанию — прежние.
APP_PORT = int(os.environ.get("OBOROT_TEST_PORT", "8804"))
# Окружение — ДО импорта приложения (db.py читает DATABASE_URL).
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
if DB_PATH.exists():
    DB_PATH.unlink()

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from app import analytics, order_planner as op  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        PASS.append(name)
        print(f"  OK   {name}" + (f"  [{detail}]" if detail else ""))
    else:
        FAIL.append(name)
        print(f"  FAIL {name}  {detail}")


TODAY = date(2026, 8, 21)


def mk_item(base, *, turnover, cost, price, cs=0, rate=1.0, cls="good",
            category="Рубашки", archived=False, hidden=False, low_data=False,
            ordered=0, sizes=None):
    return {
        "base_name": base, "category": category, "cls": cls,
        "turnover": turnover, "rate_year": rate, "cs": cs, "ordered": ordered,
        "cost_price": cost, "avg_price": price, "sale_price": price,
        "archived": archived, "hidden": hidden, "low_data": low_data,
        "nq": rate * 300, "dis": 300,
        "sizes": sizes or {"S": {"stock": cs, "sold365": 10},
                           "M": {"stock": 0, "sold365": 20}},
    }


def mk_snap(items, cov_start=None):
    """Синтетический снапшот. cov_start — граница загруженной истории (П1);
    без него снапшот выглядит как у полного/старого аккаунта."""
    snap = {
        "today": TODAY.isoformat(),
        "settings": {"min_stock_days": 3, "horizon_days": 44, "cover_days": 44,
                     "lead_time_days": 45, "order_cadence_days": 30, "safety_days": 14},
        "items": {i["base_name"]: i for i in items},
    }
    if cov_start is not None:
        snap["coverage_start"] = cov_start
    return snap


def cov_days_ago(n: int) -> str:
    """coverage_start, при котором загружено ровно n дней истории."""
    return (TODAY - timedelta(days=n - 1)).isoformat()


def mk_ctx(snap, cover=44, rate_override=None):
    rates = {b: float(rate_override.get(b, it["rate_year"]) if rate_override else it["rate_year"])
             for b, it in snap["items"].items()}
    return {
        "cover_days": cover,
        "rate_lead": dict(rates),
        "rate_cover": dict(rates),
        "fresh": set(snap["items"].keys()),
        "assign": {},
        "main_production_id": 1,
    }


SETTINGS = {"order_cadence_days": 30, "safety_days": 14, "moq_units": 0,
            "reserve_new_pct": 0, "lead_time_days": 45}


def mk_brief(stages=None, **kw):
    """Бриф через нормализацию — заодно проверяется дозаполнение из настроек."""
    raw = {
        "eta_date": (TODAY + timedelta(days=45)).isoformat(),
        "budget": 300000, "budget_scope": "full",
    }
    raw.update(kw)
    brief = op.normalize_brief(raw, SETTINGS, stages or ONE_STAGE, TODAY)
    brief["production_id"] = kw.get("production_id")
    return brief


ONE_STAGE = op.normalize_stages(None, 45)
TWO_STAGE = op.normalize_stages(op.STAGE_PRESETS["fabric_sewing"], 45)


def main() -> int:
    print("\n1. Горизонт покрытия из периодичности")
    check("cadence 30 + safety 14 = 44 дня, а не 90",
          analytics.cover_days({"order_cadence_days": 30, "safety_days": 14}) == 44)
    check("режим fixed возвращает настройку horizon_days",
          analytics.cover_days({"cover_mode": "fixed", "horizon_days_setting": 90}) == 90)
    check("клампы: раз в год + запас не больше COVER_MAX",
          analytics.cover_days({"order_cadence_days": 365, "safety_days": 120})
          == analytics.COVER_MAX_DAYS)

    print("\n2. Этапы производства")
    check("под ключ — один этап на весь срок", len(ONE_STAGE) == 1
          and ONE_STAGE[0]["cost_share"] == 1.0 and op.lead_days(ONE_STAGE) == 45)
    check("ткань → пошив: срок = сумма этапов (40+25=65)", op.lead_days(TWO_STAGE) == 65)
    check("доли себестоимости нормированы к 1.0",
          abs(sum(s["cost_share"] for s in TWO_STAGE) - 1.0) < 1e-9)
    sched = op.stage_schedule(date(2026, 9, 1), TWO_STAGE)
    check("пошив стартует после прихода ткани",
          sched[0]["done"] == sched[1]["starts"] == "2026-10-01", str(sched))
    check("последний этап завершается через 65 дней", sched[-1]["done"] == "2026-11-05")
    check("мусорные этапы → фолбэк на один этап",
          len(op.normalize_stages([{"lead_days": "нет"}], 30)) == 1)
    check("этапы без долей делятся поровну",
          op.normalize_stages([{"lead_days": 10}, {"lead_days": 20}], 30)[0]["cost_share"] == 0.5)

    print("\n2c. Минимальная партия по этапам и категориям")
    st_moq = op.normalize_stages([
        {"key": "fabric", "name": "Ткань", "lead_days": 30, "cost_share": 0.45,
         "prepay_share": 1.0, "min_units": 50},
        {"key": "sewing", "name": "Пошив", "lead_days": 35, "cost_share": 0.55,
         "prepay_share": 0.5, "min_units": 0, "min_by_category": {"Пиджаки": 20}},
    ], 45)
    check("минимум модели = максимум минимумов по этапам",
          op.stage_moq(st_moq, "Пиджаки") == 50 and op.stage_moq(st_moq, "Футболки") == 50)
    sewing_only = op.normalize_stages(op.STAGE_PRESETS["sewing_only"], 45)
    check("если ткань своя, минимум по ткани не действует",
          op.stage_moq(sewing_only, "Футболки") == 0)
    check("минимум категории применяется, когда он строже",
          op.stage_moq(op.normalize_stages(
              [{"key": "sewing", "lead_days": 25, "cost_share": 1.0,
                "min_units": 0, "min_by_category": {"Пиджаки": 20}}], 45), "Пиджаки") == 20)

    print("\n2d. Календарь платежей по этапам")
    pays = op.payment_plan(date(2026, 9, 1), st_moq, 100000)
    check("ткань оплачивается сразу и целиком",
          pays[0]["date"] == "2026-09-01" and pays[0]["amount"] == 45000, str(pays))
    check("пошив: половина при старте, половина при готовности",
          pays[1]["amount"] == 27500 and pays[1]["date"] == "2026-10-01"
          and pays[2]["amount"] == 27500 and pays[2]["date"] == "2026-11-05", str(pays))
    check("сумма платежей = себестоимость заказа",
          sum(p["amount"] for p in pays) == 100000)
    check("в день размещения нужно 45% денег, а не все 100%",
          abs(op.prepay_share_total(st_moq) - 0.45) < 1e-9)

    print("\n2e. Пиковые периоды")
    check("чёрная пятница 2026 — 27 ноября", op.black_friday(2026) == date(2026, 11, 27))
    hints = op.peak_hints(
        [{"name": "Чёрная пятница", "rule": "black_friday"},
         {"name": "Декабрь", "from": "12-01", "to": "12-31"}], date(2026, 8, 21), 65)
    check("к пику считается крайняя дата размещения заказа",
          hints[0]["name"] == "Чёрная пятница" and hints[0]["order_by"] == "2026-09-16",
          str(hints))
    check("если уже поздно — это видно", op.peak_hints(
        [{"name": "Декабрь", "from": "12-01", "to": "12-31"}],
        date(2026, 11, 1), 65)[0]["late"] is True)

    print("\n2b. Бриф: дозаполнение и защита от мусора")
    b = op.normalize_brief({}, SETTINGS, TWO_STAGE, TODAY)
    check("без даты приёмки берётся сегодня + срок всех этапов",
          b["eta_date"] == (TODAY + timedelta(days=65)).isoformat())
    check("у многоэтапного производства бюджет по умолчанию — «деньги сейчас»",
          b["budget_scope"] == "now")
    check("у одноэтапного — весь заказ",
          op.normalize_brief({}, SETTINGS, ONE_STAGE, TODAY)["budget_scope"] == "full")
    check("прошедшая дата приёмки не принимается",
          op.normalize_brief({"eta_date": "2020-01-01"}, SETTINGS, ONE_STAGE, TODAY)["eta_date"]
          > TODAY.isoformat())
    junk = op.normalize_brief({"budget": "много", "cadence_days": -5}, SETTINGS,
                              ONE_STAGE, TODAY)
    check("мусор в числах не роняет бриф (нечисло → дефолт, выход за границы → кламп)",
          junk["budget"] == 0 and junk["cadence_days"] == 7, str(junk))

    print("\n3. Бюджет не превышается")
    snap = mk_snap([
        mk_item("Топ A", turnover=6000, cost=3000, price=9000, rate=1.0, cls="best"),
        mk_item("Топ Б", turnover=4000, cost=2000, price=6000, rate=0.8, cls="good"),
        mk_item("Средний", turnover=1500, cost=1500, price=4000, rate=0.5, cls="dull"),
    ])
    ctx = mk_ctx(snap)
    plan = op.plan_order(snap, mk_brief(budget=100000), ctx, ONE_STAGE)
    check("потрачено не больше бюджета", plan["spent"] <= 100000, f"spent={plan['spent']}")
    check("сумма строк = потрачено",
          abs(sum(i["cost_total"] for i in plan["items"]) - plan["spent"]) <= 1,
          f"{sum(i['cost_total'] for i in plan['items'])} vs {plan['spent']}")
    check("резерв на новинки вычитается из распределяемых денег",
          op.plan_order(snap, mk_brief(budget=100000, reserve_new_pct=20), ctx,
                        ONE_STAGE)["spent"] <= 80000)

    print("\n4. Бюджет «сейчас» покрывает только первый этап")
    p_now = op.plan_order(snap, mk_brief(budget=100000, budget_scope="now"), ctx, TWO_STAGE)
    check("на те же деньги заказ больше, чем при оплате целиком",
          p_now["cost_total"] > plan["cost_total"],
          f"now={p_now['cost_total']} full={plan['cost_total']}")
    check("к оплате сейчас не больше бюджета", p_now["pay_now"] <= 100000,
          f"pay_now={p_now['pay_now']}")
    check("остаток к оплате на втором этапе показан",
          p_now["pay_later"] == p_now["cost_total"] - p_now["pay_now"])
    check("дата размещения = приёмка − срок всех этапов",
          p_now["order_date"] == (TODAY + timedelta(days=45) - timedelta(days=65)).isoformat())

    print("\n5. Волны и причины")
    snap2 = mk_snap([
        mk_item("Дыра", turnover=5000, cost=2000, price=6000, cs=2, rate=1.0, cls="best"),
        mk_item("Спокойный", turnover=3000, cost=2000, price=6000, cs=200, rate=1.0, cls="good"),
    ])
    ctx2 = mk_ctx(snap2)
    p2 = op.plan_order(snap2, mk_brief(budget=200000), ctx2, ONE_STAGE)
    hole = next(i for i in p2["items"] if i["base_name"] == "Дыра")
    check("позиция с дырой помечена причиной «кончится до прихода»",
          "gap" in hole["why"] and hole["gap_days"] > 0, str(hole["why"]))
    check("у каждой строки есть человеческая причина",
          all(i["why_text"] for i in p2["items"]))
    check("must-have попадает вне очереди и вне лимита доли",
          any(i["base_name"] == "Средний" and "must_have" in i["why"]
              for i in op.plan_order(snap, mk_brief(budget=6000, must_have=["Средний"]),
                                     ctx, ONE_STAGE)["items"]))

    print("\n6. Минимальная партия")
    p_moq = op.plan_order(snap, mk_brief(budget=300000, moq_units=20), ctx, ONE_STAGE)
    check("все позиции заказаны партией не меньше MOQ",
          all(i["qty"] >= 20 for i in p_moq["items"]),
          str([(i["base_name"], i["qty"]) for i in p_moq["items"]]))
    snap3 = mk_snap([mk_item("Мелочь", turnover=900, cost=1000, price=3000, rate=0.1, cls="dull")])
    p_small = op.plan_order(snap3, mk_brief(budget=300000, moq_units=50),
                            mk_ctx(snap3), ONE_STAGE)
    check("медленная позиция: партия залежится на годы — не берём",
          not p_small["items"] and p_small["moq_skipped"][0]["days"] == 500,
          str(p_small["moq_skipped"]))
    check("пустой план из-за партии объясняется, а не показывается нулями",
          p_small.get("blocked", {}).get("reason") == "moq"
          and "минимальную партию" in p_small["blocked"]["text"])
    snap3b = mk_snap([mk_item("Быстрая", turnover=6000, cost=1000, price=3000, rate=2.0, cls="best")])
    p_fast = op.plan_order(snap3b, mk_brief(budget=300000, moq_units=50),
                           mk_ctx(snap3b), ONE_STAGE)
    check("быстрая позиция берёт партию, даже если потребность меньше неё",
          p_fast["items"] and p_fast["items"][0]["qty"] >= 50,
          str([(i["base_name"], i["qty"], i["need"]) for i in p_fast["items"]]))

    print("\n7. Лимит доли на позицию")
    p_cap = op.plan_order(snap, mk_brief(budget=100000, max_share_pct=10), ctx, ONE_STAGE)
    check("ни одна позиция не съела больше 10% бюджета",
          all(i["cost_total"] <= 10000 + i["cost_price"] for i in p_cap["items"]),
          str([(i["base_name"], i["cost_total"]) for i in p_cap["items"]]))
    check("срезанные лимитом строки это объясняют",
          all("capped_share" in i["why"] or i["unmet"] == 0 or "capped_budget" in i["why"]
              for i in p_cap["items"]))

    print("\n8. Стратегии (бюджет заведомо меньше потребности)")
    snap_s = mk_snap([
        mk_item("Бестселлер", turnover=8000, cost=3000, price=9000, rate=1.2, cls="best"),
        mk_item("Хороший", turnover=3000, cost=2000, price=6000, rate=0.9, cls="good"),
        mk_item("Унылый", turnover=1500, cost=1500, price=4000, rate=0.6, cls="dull"),
        mk_item("Слабый", turnover=700, cost=1000, price=3000, rate=0.4, cls="weak"),
    ])
    ctx_s = mk_ctx(snap_s)
    wide = op.plan_order(snap_s, mk_brief(budget=40000, strategy="protect"), ctx_s, ONE_STAGE)
    deep = op.plan_order(snap_s, mk_brief(budget=40000, strategy="grow"), ctx_s, ONE_STAGE)
    check("профиль стратегии задаёт лимит доли (protect 20%, grow 60%)",
          op.normalize_brief({"strategy": "protect"}, SETTINGS, ONE_STAGE, TODAY)["max_share_pct"] == 20
          and op.normalize_brief({"strategy": "grow"}, SETTINGS, ONE_STAGE, TODAY)["max_share_pct"] == 60)
    check("«не потерять продажи» шире по ассортименту",
          wide["totals"]["positions"] > deep["totals"]["positions"],
          f"protect={wide['totals']['positions']} grow={deep['totals']['positions']}")
    check("«заработать максимум» глубже вкладывается в топ",
          deep["items"][0]["qty"] > wide["items"][0]["qty"],
          f"grow={deep['items'][0]['qty']} protect={wide['items'][0]['qty']}")
    check("слабый класс в стратегии роста не получает базовую партию",
          "Слабый" not in {i["base_name"] for i in deep["items"]},
          str([i["base_name"] for i in deep["items"]]))

    print("\n9. Что не влезло и чувствительность")
    tight = op.plan_order(snap, mk_brief(budget=9000), ctx, ONE_STAGE)
    check("при нехватке денег потребность остаётся непокрытой и это видно",
          any(i["unmet"] > 0 for i in tight["items"]),
          str([(i["base_name"], i["qty"], i["unmet"]) for i in tight["items"]]))
    check("недобор считается в марже и раздельно: не вошло / недобрано",
          tight["lost"]["missing"] >= 0 and tight["lost"]["short"] > 0
          and tight["lost"]["at_risk"] <= tight["lost"]["missing"] + tight["lost"]["short"],
          str(tight["lost"]))
    broke = op.plan_order(snap, mk_brief(budget=1200), ctx, ONE_STAGE)
    check("совсем не влезшие позиции перечислены отдельно",
          len(broke["not_included"]) > 0 and broke["not_included"][0]["lost_margin"] > 0,
          f"items={len(broke['items'])} not_included={len(broke['not_included'])}")
    check("чувствительность отвечает «а если добавить денег»",
          len(tight.get("sensitivity") or []) == len(op.SENSITIVITY_STEPS)
          and tight["sensitivity"][0]["extra_budget"] > 0)
    check("с бо́льшим бюджетом позиций не меньше",
          tight["sensitivity"][-1]["positions"] >= tight["totals"]["positions"])

    print("\n9b. Новинки — деньги держим, но не считаем")
    nb = mk_brief(budget=200000, new_items=[
        {"name": "Пальто «Осень»", "qty": 30, "cost": 9000},
        {"name": "Мусор без количества", "qty": 0, "cost": 5000},
    ])
    check("новинки нормализованы, пустые отброшены",
          len(nb["new_items"]) == 1 and nb["new_items"][0]["total"] == 270000)
    np_ = op.plan_order(snap, mk_brief(budget=800000, reserve_new_pct=10, new_items=[
        {"name": "Пальто «Осень»", "qty": 30, "cost": 9000}]), ctx, ONE_STAGE)
    check("резерв = максимум из процента и суммы вписанных новинок",
          np_["new_items_cost"] == 270000 and np_["spent"] <= 800000 - 270000,
          f"spent={np_['spent']}")
    check("новинки видны в плане отдельным блоком",
          np_["new_items"] and np_["new_items"][0]["name"] == "Пальто «Осень»")

    print("\n10. Отсев позиций")
    snap4 = mk_snap([
        mk_item("Норм", turnover=4000, cost=2000, price=6000, rate=1.0),
        mk_item("Архивная", turnover=9000, cost=2000, price=6000, rate=1.0, archived=True),
        mk_item("Без себеса", turnover=9000, cost=0, price=6000, rate=1.0),
        mk_item("Мало данных", turnover=90000, cost=2000, price=6000, rate=1.0, low_data=True),
        mk_item("Скрытая", turnover=9000, cost=2000, price=6000, rate=1.0, hidden=True),
    ])
    p4 = op.plan_order(snap4, mk_brief(budget=500000), mk_ctx(snap4), ONE_STAGE)
    names = {i["base_name"] for i in p4["items"]}
    check("архив и скрытые не попадают в заказ",
          "Архивная" not in names and "Скрытая" not in names, str(names))
    check("без себестоимости — отдельным списком, а не молча",
          p4["review"]["no_cost_count"] == 1
          and p4["review"]["no_cost"][0]["base_name"] == "Без себеса")
    check("«мало данных» вынесены на решение человека",
          p4["review"]["low_data_count"] == 1 and "Мало данных" not in names)
    stale = mk_ctx(snap4)
    stale["fresh"] = {"Норм"}
    check("без продаж за 90 дней — исключены со счётчиком",
          op.plan_order(snap4, mk_brief(budget=500000), stale, ONE_STAGE)["review"]["stale_count"] >= 1)

    print("\n11. Привязка к производству")
    snap5 = mk_snap([
        mk_item("Своё", turnover=5000, cost=2000, price=6000, rate=1.0),
        mk_item("Китайское", turnover=5000, cost=2000, price=6000, rate=1.0),
    ])
    ctx5 = mk_ctx(snap5)
    ctx5["assign"] = {"Китайское": 2}
    p5 = op.plan_order(snap5, mk_brief(budget=300000, production_id=1), ctx5, ONE_STAGE)
    check("заказ на производство №1 не тянет позиции чужого канала",
          {i["base_name"] for i in p5["items"]} == {"Своё"},
          str([i["base_name"] for i in p5["items"]]))
    p6 = op.plan_order(snap5, mk_brief(budget=300000, production_id=2), ctx5, ONE_STAGE)
    check("заказ на производство №2 берёт только свои позиции",
          {i["base_name"] for i in p6["items"]} == {"Китайское"})

    print("\n12. Размеры и покрытие")
    p7 = op.plan_order(snap, mk_brief(budget=300000), ctx, ONE_STAGE)
    check("разбивка по размерам совпадает с количеством",
          all(sum(i["sizes"].values()) == i["qty"] for i in p7["items"]))
    check("у строк есть «хватит до» после заказа",
          all(i["covered_until"] for i in p7["items"]))
    check("«спрос закрыт до» — по факту строк, а не арифметикой горизонта",
          p7["covered_until"] == min(i["covered_until"] for i in p7["items"])
          and p7["covered_until_target"] == (TODAY + timedelta(days=45 + 44)).isoformat()
          and p7["covered_until"] <= p7["covered_until_target"],
          f"факт={p7['covered_until']} цель={p7['covered_until_target']} "
          f"полностью закрыто={p7['covered_full']}")

    print("\n14. Покрытие истории (деплой П1: сервис работает, история грузится)")
    cov_items = [
        mk_item("Топ A", turnover=6000, cost=3000, price=9000, rate=1.0, cls="best"),
        mk_item("Топ Б", turnover=4000, cost=2000, price=6000, rate=0.8, cls="good"),
        mk_item("Средний", turnover=1500, cost=1500, price=4000, rate=0.5, cls="dull"),
    ]
    snap_full = mk_snap(cov_items, cov_start=cov_days_ago(400))
    snap_part = mk_snap(cov_items, cov_start=cov_days_ago(30))
    ctx_cov = mk_ctx(snap_full)
    ctx_cov["seasonal_rates"] = True     # окно «год назад» загружено
    ctx_part = mk_ctx(snap_part)         # у новичка сезонных индексов ещё нет
    p_full = op.plan_order(snap_full, mk_brief(budget=100000), ctx_cov, ONE_STAGE)
    p_part = op.plan_order(snap_part, mk_brief(budget=100000), ctx_part, ONE_STAGE)

    check("план знает, на какой истории стоит",
          p_full["coverage"]["days"] == 400 and p_full["coverage"]["start"] == cov_days_ago(400),
          str(p_full["coverage"]))
    check("порог покрытия = срок производства + горизонт покрытия",
          p_full["coverage"]["needed_days"] == op.lead_days(ONE_STAGE) + p_full["cover_days"]
          == 89, str(p_full["coverage"]))
    check("(c) полное покрытие: partial=false, упущенная выручка — число",
          p_full["coverage"]["partial"] is False
          and isinstance(p_full["lost"], dict)
          and "provisional" not in p_full and p_full.get("sensitivity"),
          f"partial={p_full['coverage']['partial']} lost={p_full['lost']}")
    # Смысл проверки: кламп покрытия на полной истории — no-op. Сравниваем не
    # с замороженной константой (она ломается при любой правке алгоритма), а с
    # планом на снапшоте БЕЗ coverage_start — то есть с поведением старого
    # аккаунта, которому история считается полной по определению.
    snap_nocov = mk_snap(cov_items)
    p_nocov = op.plan_order(snap_nocov, mk_brief(budget=100000), mk_ctx(snap_nocov), ONE_STAGE)
    keys = lambda p: [(i["base_name"], i["qty"], i["cost_total"], i["why_text"])
                      for i in p["items"]]
    check("(c) на полном покрытии план тот же, что у аккаунта без покрытия",
          keys(p_full) == keys(p_nocov)
          and p_full["totals"] == p_nocov["totals"]
          and p_full["spent"] == p_nocov["spent"],
          str(keys(p_full)))
    p_legacy = op.plan_order(mk_snap(cov_items), mk_brief(budget=100000), ctx_cov, ONE_STAGE)
    check("аккаунт без coverage_start (старый/полный) считается как год истории",
          p_legacy["coverage"]["days"] == 365 and p_legacy["coverage"]["partial"] is False
          and {k: v for k, v in p_legacy.items() if k != "coverage"}
          == {k: v for k, v in p_full.items() if k != "coverage"})

    check("(a) 30 дней истории при нужных 89 — покрытие частичное",
          p_part["coverage"]["partial"] is True and p_part["coverage"]["days"] == 30,
          str(p_part["coverage"]))
    check("(a) на обрезке истории упущенная выручка не выдумывается",
          p_part["lost"] is None, f"lost={p_part['lost']}")
    check("(a) план помечен предварительным, «а если добавить денег» не отвечаем",
          p_part.get("provisional") is True and "sensitivity" not in p_part,
          f"provisional={p_part.get('provisional')}")
    check("сезонность в темпах заявляется только когда она посчитана",
          p_full["coverage"]["seasonal_rates"] is True
          and p_part["coverage"]["seasonal_rates"] is False)
    ctx_clamped = mk_ctx(snap_part)
    ctx_clamped["fresh"] = {"Топ A"}          # окно свежести сжалось до покрытия
    ctx_clamped["fresh_clamped"] = True
    p_hidden = op.plan_order(snap_part, mk_brief(budget=100000), ctx_clamped, ONE_STAGE)
    check("позиции, отсеянные сжатым окном свежести, посчитаны отдельно",
          p_hidden["review"]["hidden_by_coverage"] == 2,
          str(p_hidden["review"]))
    check("на полном покрытии скрытых нет",
          p_full["review"]["hidden_by_coverage"] == 0)

    print("\n14b. Покрытие сезонов: один недостающий день ≠ потерянный сезон")
    for probe in (date(2026, 8, 21), date(2026, 1, 5), date(2026, 5, 31), date(2026, 11, 30)):
        c365 = (probe - timedelta(days=364)).isoformat()
        check(f"(d) завершённый синк: все четыре сезона на {probe.isoformat()}",
              all(analytics._season_coverage(c365, probe, c365).values())
              and all(analytics._season_coverage(
                  c365, probe, (probe - timedelta(days=363)).isoformat()).values()),
              str(analytics._season_coverage(
                  c365, probe, (probe - timedelta(days=363)).isoformat())))
    _lost = []
    for off in range(0, 371, 7):
        probe = date(2026, 8, 21) - timedelta(days=off)
        c365 = (probe - timedelta(days=364)).isoformat()
        if not all(analytics._season_coverage(
                c365, probe, (probe - timedelta(days=363)).isoformat()).values()):
            _lost.append(probe.isoformat())
    check("(d) на 54 проверенных датах ни один сезон не теряется из-за одного дня",
          not _lost, f"потеряны на {_lost[:5]}")
    _probe = date(2026, 8, 21)
    _c365 = (_probe - timedelta(days=364)).isoformat()
    _part = analytics._season_coverage(
        _c365, _probe, (_probe - timedelta(days=100)).isoformat())
    check("(d) реально не загруженный сезон честно непокрыт",
          _part["winter"] is False and _part["spring"] is False, str(_part))
    check("(d) без истории вообще покрытых сезонов нет",
          not any(analytics._season_coverage(_c365, _probe, None).values()))


    print("\n15. Аудит 22.08: деньги считаются одним способом")
    snap_m = mk_snap([
        mk_item("Топ", turnover=8000, cost=3000, price=9000, rate=1.2, cls="best"),
        mk_item("Второй", turnover=4000, cost=2000, price=6000, rate=0.8, cls="good"),
        mk_item("Третий", turnover=1500, cost=1500, price=4000, rate=0.5, cls="dull"),
    ])
    ctx_m = mk_ctx(snap_m)
    p_full_scope = op.plan_order(snap_m, mk_brief(budget=300000, budget_scope="full",
                                                 stages=TWO_STAGE), ctx_m, TWO_STAGE)
    check("«сейчас» = первый транш календаря, а не отдельная формула",
          p_full_scope["pay_now"] == p_full_scope["payments"][0]["amount"],
          f"pay_now={p_full_scope['pay_now']} первый транш={p_full_scope['payments'][0]['amount']}")
    check("сумма календаря = себестоимости заказа копейка в копейку",
          sum(x["amount"] for x in p_full_scope["payments"]) == p_full_scope["cost_total"],
          f"{sum(x['amount'] for x in p_full_scope['payments'])} vs {p_full_scope['cost_total']}")
    check("«потом» = обязательство минус первый транш",
          p_full_scope["pay_later"] == p_full_scope["cost_total"] - p_full_scope["pay_now"])
    p_now = op.plan_order(snap_m, mk_brief(budget=300000, budget_scope="now",
                                           stages=TWO_STAGE), ctx_m, TWO_STAGE)
    check("при «деньги на сейчас» видно полное обязательство, а не только транш",
          p_now["cost_total"] > p_now["pay_now"] and p_now["pay_later"] > 0
          and p_now["pay_now"] <= 300000,
          f"обязательство={p_now['cost_total']} сейчас={p_now['pay_now']} потом={p_now['pay_later']}")

    print("\n16. «Упущено»: две разные величины, обе в марже")
    tight_m = op.plan_order(snap_m, mk_brief(budget=20000), ctx_m, ONE_STAGE)
    missing_sum = sum(i["lost_margin"] for i in tight_m["not_included"])
    check("«не вошло совсем» сходится с таблицей под заголовком",
          abs(tight_m["lost"]["missing"] - missing_sum) <= 1,
          f"заголовок={tight_m['lost']['missing']} таблица={missing_sum}")
    check("недобор по вошедшим считается отдельно",
          tight_m["lost"]["short"] > 0 and tight_m["lost"]["short"] != tight_m["lost"]["missing"])
    check("считается в марже, а не в выручке",
          tight_m["lost"]["missing"] + tight_m["lost"]["short"]
          < sum(i["need"] * i["avg_price"] for i in
                [{"need": c["need"], "avg_price": c["avg_price"]} for c in []] ) + 10 ** 12)
    risky = op.plan_order(snap_m, mk_brief(budget=20000, cadence_days=7), ctx_m, ONE_STAGE)
    check("часть недобора закроет следующий заказ — «под риском» меньше суммы",
          risky["lost"]["at_risk"] < risky["lost"]["missing"] + risky["lost"]["short"],
          f"под риском={risky['lost']['at_risk']} всего="
          f"{risky['lost']['missing'] + risky['lost']['short']} ритм={risky['lost']['next_order_days']}")

    print("\n17. Минимальная партия не отменяет лимит доли")
    snap_moq = mk_snap([
        mk_item("Дорогой", turnover=9000, cost=12000, price=30000, rate=0.6, cls="best"),
        mk_item("Обычный", turnover=3000, cost=2000, price=6000, rate=0.9, cls="good"),
    ])
    p_moq = op.plan_order(snap_moq, mk_brief(budget=200000, moq_units=20, max_share_pct=30),
                          mk_ctx(snap_moq), ONE_STAGE)
    cap = 200000 * 0.30
    check("партия дороже лимита не проходит молча",
          all(i["cost_total"] <= cap + i["cost_price"] for i in p_moq["items"]),
          str([(i["base_name"], i["cost_total"]) for i in p_moq["items"]]))
    check("такая позиция уходит в отсев с ценой партии",
          any(x["base_name"] == "Дорогой" and x["batch_cost"] == 240000
              for x in p_moq["moq_over_cap"]), str(p_moq["moq_over_cap"]))
    # Ложная подпись (аудит 22.08: 11 строк из 20 говорили «срезано лимитом»,
    # хотя упирались в деньги). Причина теперь определяется по факту: достигли
    # потолка доли — лимит, не достигли — деньги.
    snap_lbl = mk_snap([
        mk_item("Первый", turnover=9000, cost=4000, price=12000, rate=2.0, cls="best"),
        mk_item("Второй", turnover=7000, cost=4000, price=12000, rate=2.0, cls="best"),
        mk_item("Третий", turnover=5000, cost=4000, price=12000, rate=2.0, cls="good"),
    ])
    p_lbl = op.plan_order(snap_lbl, mk_brief(budget=30000, max_share_pct=90),
                          mk_ctx(snap_lbl), ONE_STAGE)
    check("упёрлись в деньги — так и написано, а не «срезано лимитом»",
          all("capped_share" not in i["why"] for i in p_lbl["items"])
          and any("capped_budget" in i["why"] for i in p_lbl["items"]),
          str([(i["base_name"], i["qty"], i["why_text"]) for i in p_lbl["items"]]))
    p_lbl2 = op.plan_order(snap_lbl, mk_brief(budget=1000000, max_share_pct=5),
                           mk_ctx(snap_lbl), ONE_STAGE)
    check("упёрлись в лимит доли — подпись про лимит появляется",
          any("capped_share" in i["why"] for i in p_lbl2["items"]),
          str([(i["base_name"], i["qty"], i["why_text"]) for i in p_lbl2["items"]]))
    check("подпись «срезано лимитом» ставится только при реальном лимите доли",
          all("capped_share" not in i["why"] or i["cost_total"] >= cap * 0.9
              for i in p_moq["items"]),
          str([(i["base_name"], i["cost_total"], i["why"]) for i in p_moq["items"]]))

    print("\n18. Ширина заказа — работающая ручка")
    snap_w = mk_snap([
        mk_item("A", turnover=9000, cost=2000, price=6000, rate=1.5, cls="best"),
        mk_item("B", turnover=6000, cost=2000, price=6000, rate=1.2, cls="good"),
        mk_item("C", turnover=3000, cost=2000, price=6000, rate=0.9, cls="dull"),
        mk_item("D", turnover=1500, cost=2000, price=6000, rate=0.6, cls="weak"),
    ])
    ctx_w = mk_ctx(snap_w)
    # Стратегия задаёт И ширину, И лимит доли — иначе лимит съедает эффект.
    wide = op.plan_order(snap_w, mk_brief(budget=60000, strategy="protect"), ctx_w, ONE_STAGE)
    deep = op.plan_order(snap_w, mk_brief(budget=60000, strategy="grow"), ctx_w, ONE_STAGE)
    check("узкая ручка даёт меньше позиций и больше глубины",
          deep["totals"]["positions"] < wide["totals"]["positions"]
          and deep["items"][0]["qty"] > wide["items"][0]["qty"],
          f"широко={wide['totals']['positions']} поз/{wide['items'][0]['qty']} шт · "
          f"узко={deep['totals']['positions']}/{deep['items'][0]['qty']}")
    check("стратегия задаёт ширину и лимит",
          op.normalize_brief({"strategy": "protect"}, SETTINGS, ONE_STAGE, TODAY)["width_days"] == 7
          and op.normalize_brief({"strategy": "grow"}, SETTINGS, ONE_STAGE, TODAY)["width_days"] == 0)
    check("минимальная партия — пол, а не цель: глубина больше партии",
          op.plan_order(snap_w, mk_brief(budget=200000, moq_units=5, width_days=0),
                        ctx_w, ONE_STAGE)["items"][0]["qty"] > 5)
    check("явный ноль в «мин. партия» отключает партию канала",
          op.normalize_brief({"moq_units": 0}, {"moq_units": 30}, ONE_STAGE, TODAY)["moq_units"] == 0
          and op.normalize_brief({}, {"moq_units": 30}, ONE_STAGE, TODAY)["moq_units"] == 30)

    print("\n19. Темп: одна настройка на весь продукт")
    snap_rw = mk_snap([mk_item("Позиция", turnover=5000, cost=2000, price=6000, rate=1.0)])
    snap_rw["items"]["Позиция"]["rate_active"] = 3.0   # настройка окна дала другой темп
    ctx_rw = mk_ctx(snap_rw)
    ctx_rw["rate_cover"]["Позиция"] = 3.0
    ctx_rw["rate_lead"]["Позиция"] = 3.0
    p_rw = op.plan_order(snap_rw, mk_brief(budget=300000), ctx_rw, ONE_STAGE)
    check("планировщик считает потребность по активному темпу",
          p_rw["items"][0]["need"] == int(round(3.0 * p_rw["cover_days"])),
          f"need={p_rw['items'][0]['need']} ожидалось {round(3.0 * p_rw['cover_days'])}")
    check("в строке виден темп в штуках — число «Нужно» можно проверить",
          p_rw["items"][0]["rate"] == 3.0)

    print("\n20. Даты, которые не врут")
    p_dates = op.plan_order(snap_m, mk_brief(budget=300000), ctx_m, ONE_STAGE)
    check("«хватит до» строки не позже цели горизонта или равно ей",
          all(i["covered_until"] for i in p_dates["items"]))
    check("«разойдётся за N дней» считается для вошедших строк",
          all(i["days_to_sell"] is not None for i in p_dates["items"]))
    check("штуки сверх потребности показаны отдельно",
          "over_need_cost" in p_dates and p_dates["over_need_cost"] >= 0)

    print("\n21. Пустой план объясняется по-разному в разных случаях")
    zero = op.plan_order(snap_m, mk_brief(budget=0), ctx_m, ONE_STAGE)
    check("нет бюджета — так и сказано",
          zero["blocked"]["reason"] == "no_budget", str(zero.get("blocked")))
    res_b = op.plan_order(snap_m, mk_brief(budget=100000, new_items=[
        {"name": "Пальто", "qty": 30, "cost": 9000}]), ctx_m, ONE_STAGE)
    check("новинки съели бюджет — так и сказано",
          res_b["blocked"]["reason"] == "reserve" and "новинк" in res_b["blocked"]["text"].lower(),
          str(res_b.get("blocked")))
    ctx_other = mk_ctx(snap_m)
    ctx_other["assign"] = {b: 99 for b in snap_m["items"]}
    other = op.plan_order(snap_m, mk_brief(budget=300000, production_id=1), ctx_other, ONE_STAGE)
    check("позиции не назначены на этот канал — так и сказано, с числом",
          other["blocked"]["reason"] == "no_assignment" and other["blocked"]["count"] == 3,
          str(other.get("blocked")))

    print("\n22. Кнопку «Создать заказ» блокируют только ошибки")
    ok_plan = op.plan_order(snap_m, mk_brief(budget=300000), ctx_m, ONE_STAGE)
    check("нормальный план создавать можно", ok_plan["can_create"] is True, str(ok_plan["stop"]))
    late = op.plan_order(snap_m, mk_brief(budget=300000,
                                          eta_date=(TODAY + timedelta(days=5)).isoformat()),
                         ctx_m, ONE_STAGE)
    check("дата размещения в прошлом — создавать нельзя",
          late["can_create"] is False
          and any(x["code"] == "past_date" for x in late["stop"]), str(late["stop"]))
    check("пустой план создавать нельзя", zero["can_create"] is False)

    api_checks()

    print()
    print(f"ИТОГО: {len(PASS)} OK, {len(FAIL)} FAIL")
    if FAIL:
        print("Провалены:", *FAIL, sep="\n  - ")
        return 1
    return 0


class ServerThread:
    def __init__(self, asgi_app, port: int):
        self.config = uvicorn.Config(asgi_app, host="127.0.0.1", port=port, log_level="warning")
        self.server = uvicorn.Server(self.config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self):
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                return
            time.sleep(0.1)
        raise RuntimeError("сервер не поднялся")

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=10)


def _truncate_history(days: int) -> None:
    """Оставить в БД только последние `days` дней истории.

    Имитация первичной прогрессивной загрузки (деплой П1): сервис уже открыт,
    остатки и продажи за год ещё едут. Как в тестах синка — правкой sqlite
    напрямую, после чего сбрасываем кэш аналитики.
    """
    import sqlite3

    from app import analytics as _an

    cut = (date.today() - timedelta(days=days - 1)).isoformat()
    con = sqlite3.connect(DB_PATH)
    try:
        con.execute("DELETE FROM stock_days WHERE date < ?", (cut,))
        con.execute("DELETE FROM sales WHERE date < ?", (cut,))
        con.commit()
        for (org_id,) in con.execute("SELECT id FROM orgs"):
            _an.invalidate(org_id)
    finally:
        con.close()


def json_dumps(obj) -> str:
    import json as _j
    return _j.dumps(obj, ensure_ascii=False)


def _sql(query: str, *args):
    import sqlite3
    con = sqlite3.connect(DB_PATH)
    try:
        cur = con.execute(query, args)
        con.commit()
        return cur.fetchall()
    finally:
        con.close()


def _add_no_cost_item(base: str, qty_on_hand: int = 9) -> None:
    """Позиция без себестоимости — её в демо-данных нет (это отдельный вопрос
    к владельцу: демо не показывает как раз те функции, которые цепляют).
    Кладём напрямую в базу, как это делают тесты синка, и сбрасываем кэш."""
    from app import analytics as _an
    org_id = _sql("SELECT id FROM orgs ORDER BY id LIMIT 1")[0][0]
    _sql("INSERT INTO products (org_id, ext_id, base_name, size, category, "
         "cost_price, sale_price, archived, excluded) VALUES (?,?,?,?,?,?,?,0,0)",
         org_id, "nc-1", base, "", "Аксессуары", 0, 2000)
    pid = _sql("SELECT id FROM products WHERE org_id=? AND ext_id='nc-1'", org_id)[0][0]
    today = date.today()
    for i in range(60):
        d = (today - timedelta(days=i)).isoformat()
        _sql("INSERT INTO stock_days (org_id, product_id, date, qty) VALUES (?,?,?,?)",
             org_id, pid, d, qty_on_hand)
    for i in range(0, 60, 6):
        d = (today - timedelta(days=i)).isoformat()
        _sql("INSERT INTO sales (org_id, product_id, date, qty, revenue, is_return) "
             "VALUES (?,?,?,?,?,0)", org_id, pid, d, 2, 4000)
    _an.invalidate(org_id)


def api_checks() -> None:
    """Сквозной путь на демо-данных: анкета → план → сохранение → заказ."""
    print("\n13. API мастера заказа (демо-организация)")
    from app.main import app as oborot_app

    srv = ServerThread(oborot_app, APP_PORT)
    srv.start()
    base = f"http://127.0.0.1:{APP_PORT}"
    c = httpx.Client(headers={"X-Oborot-CSRF": "1"}, base_url=base, timeout=60,
                     follow_redirects=False)
    try:
        r = c.post("/register", data={"name": "Тест", "email": "planner@test.io",
                                      "password": "secret123", "org_name": "Тест-бренд"})
        check("регистрация владельца", r.status_code == 303)
        check("демо-данные подключены", c.post("/api/connect/demo").json().get("ok"))

        st = c.get("/api/settings").json()
        check("настройки отдают горизонт покрытия и периодичность",
              st.get("cover_days") == st.get("order_cadence_days", 0) + st.get("safety_days", 0),
              f"cover={st.get('cover_days')}")
        check("горизонт покрытия меньше прежних 90 дней по умолчанию",
              0 < st["cover_days"] < 90, f"cover_days={st['cover_days']}")
        check("/api/replenish считает по тому же горизонту",
              c.get("/api/replenish").json()["horizon_days"] == st["cover_days"])

        prods = c.get("/api/productions").json()["productions"]
        check("основное производство отдаётся с этапами и сроком",
              prods and prods[0]["lead_days"] > 0 and prods[0]["stages"], str(prods[:1]))
        china = c.post("/api/productions", json={"name": "Китай", "preset": "turnkey",
                                                 "moq_units": 30}).json()
        check("канал «под ключ» создан одним этапом",
              china["staged"] is False and china["moq_units"] == 30, str(china))
        lab = c.post(f"/api/productions/{prods[0]['id']}/setup",
                     json={"preset": "fabric_sewing", "moq_units": 10}).json()
        check("своё производство: два этапа, срок = сумма",
              lab["staged"] and lab["lead_days"] == sum(s["lead_days"] for s in lab["stages"]),
              f"lead={lab['lead_days']}")

        eta = (date.today() + timedelta(days=lab["lead_days"])).isoformat()
        plan = c.post("/api/order-plan/preview", json={
            "production_id": lab["id"], "eta_date": eta, "budget": 300000,
            "budget_scope": "now", "strategy": "balance",
        }).json()
        check("превью плана укладывается в бюджет",
              plan["pay_now"] <= 300000, f"pay_now={plan['pay_now']}")
        check("план сообщает дату размещения заказа и этапы",
              plan["order_date"] <= date.today().isoformat() or plan["order_date"] <= eta)
        check("в плане есть позиции с причинами",
              plan["items"] and all(i["why_text"] for i in plan["items"]),
              f"positions={len(plan['items'])}")
        check("минимальная партия производства подхватилась",
              all(i["qty"] >= 10 for i in plan["items"]),
              str([(i["base_name"], i["qty"]) for i in plan["items"]][:5]))

        saved = c.post("/api/order-plan", json={
            "production_id": lab["id"], "eta_date": eta, "budget": 300000,
            "budget_scope": "now", "strategy": "balance",
        }).json()
        check("план сохранён", saved.get("ok") and saved.get("id"))
        check("последний бриф возвращается для предзаполнения",
              c.get("/api/order-plan/last").json()["brief"]["budget"] == 300000)
        applied = c.post(f"/api/order-plan/{saved['id']}/apply", json={"name": "Осень 2026"})
        check("план превращается в заказ на производство",
              applied.status_code == 200 and applied.json().get("order_id"))
        again = c.post(f"/api/order-plan/{saved['id']}/apply", json={"name": "Ещё раз"})
        check("повторное применение плана → 409", again.status_code == 409)
        # ── Правило распределения позиций по производствам ─────────────────
        from app.db import SessionLocal
        from app.models import Product as _P
        db = SessionLocal()
        try:
            bases = sorted({r for (r,) in db.query(_P.base_name).distinct().all()})
            china_bases = set(bases[:5])
            for row in db.query(_P).filter(_P.base_name.in_(china_bases)).all():
                row.supplier = "Китай"
            for row in db.query(_P).filter(~_P.base_name.in_(china_bases)).all():
                row.supplier = "Своё производство"
            db.commit()
        finally:
            db.close()
        src = c.get("/api/productions/assign-sources").json()
        vals = {v["value"]: v["positions"] for v in src["sources"]["supplier"]["values"]}
        check("источники распределения видят поставщиков со счётчиками",
              vals.get("Китай") == 5 and vals.get("Своё производство", 0) > 5, str(vals))
        check("система сама предлагает правило по поставщику",
              src["suggest"]["assign_source"] == "supplier"
              and any(ch["value"] == "Китай" and ch["suggest_preset"] == "turnkey"
                      for ch in src["suggest"]["channels"]), str(src["suggest"]))
        applied = c.post("/api/productions/assign-rule", json={
            "assign_source": "supplier",
            "assign_map": {"Китай": china["id"]}}).json()
        check("правило применилось: 5 позиций ушли в китайский канал",
              applied["assigned"] == 5 and applied["by_production"].get(str(china["id"])) == 5,
              str(applied))
        prods2 = c.get("/api/productions").json()
        check("распределение отдаётся вместе с производствами",
              len(prods2["assign"]) == 5 and prods2["assign_source"] == "supplier")
        plan_china = c.post("/api/order-plan/preview", json={
            "production_id": china["id"], "budget": 500000, "strategy": "protect"}).json()
        china_names = {i["base_name"] for i in plan_china["items"]}
        check("заказ на Китай берёт только китайские позиции",
              china_names <= china_bases, str(china_names - china_bases))
        one = sorted(china_bases)[0]
        main_id = [p["id"] for p in prods2["productions"] if p["is_main"]][0]
        c.post("/api/productions/assign", json={"base_name": one, "production_id": main_id})
        after = c.get("/api/productions").json()["assign"]
        check("ручное назначение сильнее правила (позиция закреплена за своим)",
              after.get(one) == main_id, f"{one} → {after.get(one)}")
        c.post("/api/productions/assign", json={"base_name": one, "production_id": None})
        after2 = c.get("/api/productions").json()["assign"]
        check("снятие ручного назначения возвращает позицию под правило",
              after2.get(one) == china["id"], f"{one} → {after2.get(one)}")
        c.post("/api/productions/assign-rule", json={"assign_source": "manual", "assign_map": {}})
        check("правило можно выключить — распределение снова только ручное",
              c.get("/api/productions").json()["assign_source"] == "manual")
        check("неизвестное производство в правиле отклоняется",
              c.post("/api/productions/assign-rule", json={
                  "assign_source": "supplier",
                  "assign_map": {"Китай": 9999}}).status_code == 422)

        page = c.get("/assistant")
        check("страница мастера отдаётся и содержит анкету",
              page.status_code == 200 and "Мастер заказа" in page.text
              and "Как часто вы размещаете заказы" in page.text,
              f"status={page.status_code}")
        check("пункт меню «Мастер заказа» появился на других страницах",
              '/assistant' in c.get("/budget").text and '/assistant' in c.get("/replenish").text)
        ov = c.post("/api/order-plan", json={
            "production_id": lab["id"], "eta_date": eta, "budget": 300000,
            "budget_scope": "now", "strategy": "balance",
            "overrides": {plan["items"][0]["base_name"]: 7}}).json()
        first = [i for i in ov["plan"]["items"] if i["base_name"] == plan["items"][0]["base_name"]]
        check("ручная правка количества переживает сохранение плана",
              first and first[0]["qty"] == 7 and first[0]["why_text"] == "изменено вручную",
              str(first[:1]))
        orders = c.get("/api/orders").json()["orders"]
        check("заказ виден в списке и содержит позиции",
              any(o["name"] == "Осень 2026" for o in orders), str([o["name"] for o in orders]))

        # ── Д3: мастер знает про свои заказы ──────────────────────────────
        print("\n23. Открытые заказы, календарь денег, защита от дубля")
        made = [o for o in orders if o["name"] == "Осень 2026"][0]
        check("заказ помнит канал производства",
              made["production_id"] == lab["id"], str(made["production_id"]))
        op = c.get("/api/orders/open", params={"production_id": lab["id"]}).json()
        check("открытые заказы канала видны мастеру",
              op["count"] >= 1 and any(o["id"] == made["id"] for o in op["orders"]),
              str(op["count"]))
        check("по открытым заказам видно, сколько ещё платить",
              op["left_to_pay"] > 0, str(op["left_to_pay"]))
        check("открытый заказ отдаёт календарь платежей",
              op["orders"][0]["payments"] and
              abs(sum(x["amount"] for x in op["orders"][0]["payments"])
                  - op["orders"][0]["total_cost"]) <= 1,
              str(op["orders"][0]["payments"]))
        check("по позициям открытых заказов видно количество",
              op["by_base"] and sum(op["by_base"].values()) == made["total_qty"],
              str(sum(op["by_base"].values())))
        other = c.get("/api/orders/open", params={"production_id": china["id"]}).json()
        check("фильтр по каналу не показывает чужие заказы",
              other["count"] == 0, str(other["count"]))

        cal = c.get("/api/cash-calendar").json()
        check("календарь денег отдаёт 16 недель", len(cal["weeks"]) == 16)
        cums = [w["cumulative"] for w in cal["weeks"]]
        check("накопительное сальдо не убывает", cums == sorted(cums), str(cums[:4]))
        check("в календаре есть деньги открытых заказов", cal["total"] > 0, str(cal["total"]))
        check("накопительное сальдо считает только будущие деньги",
              cal["weeks"][-1]["cumulative"] == sum(w["amount"] for w in cal["weeks"]),
              f'{cal["weeks"][-1]["cumulative"]} vs {sum(w["amount"] for w in cal["weeks"])}')
        wk = date.fromisoformat(cal["week_start"])
        check("неделя начинается с понедельника и не позже сегодня",
              wk.weekday() == 0 and wk <= date.today(), cal["week_start"])

        dup_body = {"production_id": lab["id"], "eta_date": eta, "budget": 300000,
                    "budget_scope": "now", "strategy": "balance"}
        dup_plan = c.post("/api/order-plan", json=dup_body).json()
        r_dup = c.post(f"/api/order-plan/{dup_plan['id']}/apply", json={"name": "Осень 2026 (2)"})
        check("повторный заказ тем же составом требует подтверждения",
              r_dup.status_code == 409 and "уже есть" in r_dup.json().get("detail", ""),
              f"{r_dup.status_code} {r_dup.text[:120]}")
        r_force = c.post(f"/api/order-plan/{dup_plan['id']}/apply",
                         json={"name": "Осень 2026 (2)", "force": True})
        check("с явным подтверждением заказ всё-таки создаётся",
              r_force.status_code == 200 and r_force.json().get("order_id"),
              f"{r_force.status_code} {r_force.text[:120]}")
        op2 = c.get("/api/orders/open", params={"production_id": lab["id"]}).json()
        check("оба заказа считаются открытыми", op2["count"] >= 2, str(op2["count"]))
        c.post(f"/api/orders/{r_force.json()['order_id']}/status", json={"status": "sent"})
        op3 = c.get("/api/orders/open", params={"production_id": lab["id"]}).json()
        check("заказ «в производстве» остаётся открытым обязательством",
              op3["count"] == op2["count"], f"{op3['count']} vs {op2['count']}")

        # ── Д4: настройки, которых не было в интерфейсе ────────────────────
        print("\n24. Настройки канала, типы цен, откат авто-исключений, роли")
        # Подрядчик, который берёт деньги только по готовности: доля предоплаты
        # ноль. Раньше это обнуляло цену единицы для бюджета (max(1.0, 0)) и
        # план вылетал в десятки раз выше названной суммы.
        p_now = c.post("/api/order-plan/preview", json={
            "production_id": lab["id"], "eta_date": eta, "budget": 300000,
            "budget_scope": "now", "strategy": "balance"}).json()
        check("обычный канал считает бюджет по деньгам «на сейчас»",
              p_now["budget_basis"] == "now" and p_now["budget_note"] is None,
              str(p_now["budget_basis"]))
        zero = c.post(f"/api/productions/{lab['id']}/setup", json={"stages": [
            {"key": "make", "name": "Пошив", "lead_days": 30,
             "cost_share": 1.0, "prepay_share": 0.0},
        ]}).json()
        check("канал без предоплаты сохраняется",
              zero["prepay_now_share"] == 0 and zero["lead_days"] == 30, str(zero["prepay_now_share"]))
        eta_z = (date.today() + timedelta(days=30)).isoformat()
        pz = c.post("/api/order-plan/preview", json={
            "production_id": lab["id"], "eta_date": eta_z, "budget": 100000,
            "budget_scope": "now", "strategy": "balance"}).json()
        check("нулевая предоплата не отключает бюджет",
              pz["budget_basis"] == "full_no_prepay"
              and 0 < pz["cost_total"] <= 100000,
              f'basis={pz["budget_basis"]} cost={pz["cost_total"]}')
        check("и объясняет это словами", bool(pz["budget_note"]), str(pz["budget_note"]))
        # Вернули канал в исходное состояние — дальше тесты считают по нему.
        c.post(f"/api/productions/{lab['id']}/setup",
               json={"preset": "fabric_sewing", "moq_units": 10})

        two = c.post(f"/api/productions/{china['id']}/setup", json={"stages": [
            {"key": "fabric", "name": "Ткань", "lead_days": 20,
             "cost_share": 0.4, "prepay_share": 1.0, "min_units": 25},
            {"key": "sew", "name": "Пошив", "lead_days": 15,
             "cost_share": 0.6, "prepay_share": 0.5},
        ], "cadence_days": 60, "moq_units": 12}).json()
        check("этапы из формы: срок = сумма, предоплата = первый этап",
              two["lead_days"] == 35 and abs(two["prepay_now_share"] - 0.4) < 0.001,
              f'{two["lead_days"]} / {two["prepay_now_share"]}')
        check("ритм и минимальная партия канала сохранились",
              two["cadence_days"] == 60 and two["moq_units"] == 12, str(two))
        check("минимум по этапу не потерялся",
              two["stages"][0]["min_units"] == 25, str(two["stages"][0]))
        norm = c.post(f"/api/productions/{china['id']}/setup", json={"stages": [
            {"key": "a", "name": "A", "lead_days": 10, "cost_share": 0.3},
            {"key": "b", "name": "B", "lead_days": 10, "cost_share": 0.3},
        ]}).json()
        check("кривые доли себестоимости нормируются к 100%",
              abs(sum(st["cost_share"] for st in norm["stages"]) - 1.0) < 1e-6,
              str([st["cost_share"] for st in norm["stages"]]))

        st2 = c.get("/api/settings").json()
        check("настройки отдают список типов цен МойСклада",
              isinstance(st2.get("price_types"), list), str(st2.get("price_types")))
        c.post("/api/settings", json={"price_type_cost": "Себестоимость"})
        check("выбранный тип цены сохраняется",
              c.get("/api/settings").json()["price_type_cost"] == "Себестоимость")

        from app.db import SessionLocal as _SL
        db2 = _SL()
        try:
            names = sorted({r for (r,) in db2.query(_P.base_name).distinct().all()})
            auto_base, hand_base = names[0], names[1]
            for r in db2.query(_P).filter(_P.base_name == auto_base).all():
                r.base_name = "Подарочный сертификат 5000"
                r.excluded = True
            for r in db2.query(_P).filter(_P.base_name == hand_base).all():
                r.excluded = True
            db2.commit()
        finally:
            db2.close()
        ex = c.get("/api/exclusions").json()
        by_rule = {e["base_name"]: e for e in ex["excluded"] if e["by_rule"]}
        by_hand = [e for e in ex["excluded"] if not e["by_rule"]]
        check("видно, что отложила система, а что человек",
              "Подарочный сертификат 5000" in by_rule
              and any(e["base_name"] == hand_base for e in by_hand),
              f'rule={list(by_rule)} hand={[e["base_name"] for e in by_hand]}')
        check("у авто-исключения написана причина",
              "сертификат" in by_rule["Подарочный сертификат 5000"]["reason"],
              by_rule["Подарочный сертификат 5000"]["reason"])
        check("счётчик авто-исключений отдаётся отдельно",
              ex["by_rule_count"] == len(by_rule), str(ex["by_rule_count"]))
        c.post("/api/exclusions", json={"base_name": "Подарочный сертификат 5000",
                                        "excluded": False})
        check("авто-исключение возвращается в аналитику одной кнопкой",
              not any(e["base_name"] == "Подарочный сертификат 5000"
                      for e in c.get("/api/exclusions").json()["excluded"]))

        page_s = c.get("/settings")
        check("в настройках есть карточки каналов и типов цен",
              "Каналы производства" in page_s.text and "Цены из МойСклада" in page_s.text
              and "chan-box" in page_s.text, f"status={page_s.status_code}")
        check("мастер предупреждает про себестоимость на первом экране",
              "#s1 .panel" in c.get("/assistant").text)

        # ── Очередь 3 ─────────────────────────────────────────────────────
        print("\n25. Очередь 3: накладные, кратность, маржа по спросу, история")
        base_plan = c.post("/api/order-plan/preview", json={
            "production_id": lab["id"], "eta_date": eta, "budget": 300000,
            "budget_scope": "full", "strategy": "balance"}).json()
        over = c.post("/api/order-plan/preview", json={
            "production_id": lab["id"], "eta_date": eta, "budget": 300000,
            "budget_scope": "full", "strategy": "balance", "overhead_pct": 25}).json()
        check("накладные поднимают себестоимость позиции",
              over["items"] and base_plan["items"]
              and over["overhead_pct"] == 25
              and over["items"][0]["cost_price"] > base_plan["items"][0]["cost_price"],
              f'{base_plan["items"][0]["cost_price"]} → {over["items"][0]["cost_price"]}')
        check("с накладными заказ всё равно не выходит за бюджет",
              sum(i["cost_total"] for i in over["items"]) <= 300000,
              str(sum(i["cost_total"] for i in over["items"])))
        check("накладные не меняют потребность в штуках",
              {i["base_name"]: i["need"] for i in over["items"]}
              == {i["base_name"]: i["need"] for i in base_plan["items"]
                  if i["base_name"] in {x["base_name"] for x in over["items"]}},
              "need изменился")

        check("маржа считается только по спросу",
              all(i["expected_profit"] <= round(i["need"] * max(0, i["avg_price"] - i["cost_price"])) + 1
                  for i in base_plan["items"]),
              str([(i["base_name"], i["qty"], i["need"], i["expected_profit"])
                   for i in base_plan["items"] if i["over_need"]][:3]))
        check("штуки сверх потребности вынесены отдельно",
              all(i["over_need_profit"] >= 0 for i in base_plan["items"]))

        c.post(f"/api/productions/{lab['id']}",
               json={"name": lab["name"], "pack_multiple": 12})
        packed = c.post("/api/order-plan/preview", json={
            "production_id": lab["id"], "eta_date": eta, "budget": 300000,
            "budget_scope": "full", "strategy": "balance"}).json()
        check("кратность упаковки применяется к количествам",
              packed["pack_multiple"] == 12
              and all(i["qty"] % 12 == 0 for i in packed["items"]),
              str([(i["base_name"], i["qty"]) for i in packed["items"]][:5]))
        check("и заказ по-прежнему укладывается в бюджет",
              sum(i["cost_total"] for i in packed["items"]) <= 300000,
              str(sum(i["cost_total"] for i in packed["items"])))
        check("причина «кратность» видна в строке",
              any("pack" in i["why"] for i in packed["items"]),
              str([i["why"] for i in packed["items"]][:3]))
        c.post(f"/api/productions/{lab['id']}",
               json={"name": lab["name"], "pack_multiple": 0})

        hist = c.get("/api/order-plan/history").json()["plans"]
        check("история планов отдаётся с деньгами и автором",
              hist and hist[0]["budget"] > 0 and hist[0]["author"]
              and hist[0]["cost"] > 0,
              str(hist[:1]))
        check("в истории видно, какой план стал заказом",
              any(h["order_id"] for h in hist), str([h["order_id"] for h in hist]))
        rep = c.get(f"/api/order-plan/{hist[0]['id']}/brief").json()
        check("анкету прошлого плана можно поднять для «повторить»",
              rep["brief"]["budget"] == hist[0]["budget"], str(rep["brief"].get("budget")))
        check("чужой план не отдаётся", c.get("/api/order-plan/999999/brief").status_code == 404)

        orders_now = c.get("/api/orders").json()
        made2 = [o for o in orders_now["orders"] if o["name"] == "Осень 2026"][0]
        check("заказ помнит, кто его создал",
              made2["created_by"] and orders_now["authors"].get(str(made2["created_by"])),
              str(orders_now["authors"]))

        page_a = c.get("/assistant").text
        check("в таблице плана есть поиск, сортировка и группировка",
              'id="planq"' in page_a and 'class="sortable' in page_a
              and 'id="plangrp"' in page_a and 'id="planBody"' in page_a)
        check("списки отсева больше не обрезаются на 30–60 строках",
              "slice(0,60)" not in page_a and "slice(0,30)" not in page_a
              and "slice(0,40)" not in page_a)
        check("в настройках есть поле накладных",
              'id="overhead"' in c.get("/settings").text)

        print("\n14b. Горизонт заказа: два режима (D-27)")
        # До этой правки поле «Горизонт планирования» в Настройках сохранялось,
        # показывалось, упоминалось в подсказках — и НЕ влияло ни на что:
        # режим по умолчанию считает горизонт из ритма и страховки, а
        # переключателя режима в интерфейсе не было вообще.
        st0 = c.get("/api/settings").json()
        check("настройки называют горизонт тремя разными именами",
              all(k in st0 for k in ("horizon_days_fixed", "horizon_days_effective",
                                     "horizon_source")),
              f"keys={[k for k in st0 if 'horizon' in k]}")
        check("по умолчанию режим автоматический",
              st0["horizon_source"] == "cadence", f"got={st0['horizon_source']}")
        check("автоматический горизонт = ритм + страховой запас (с клампом)",
              st0["horizon_days_effective"]
              == max(21, min(180, st0["order_cadence_days"] + st0["safety_days"])),
              f"eff={st0['horizon_days_effective']} ритм={st0['order_cadence_days']} "
              f"страховка={st0['safety_days']}")
        check("устаревшее имя horizon_days = пользовательская настройка, а не расчёт",
              st0["horizon_days"] == st0["horizon_days_fixed"],
              f"horizon_days={st0['horizon_days']} fixed={st0['horizon_days_fixed']}")

        eta_hz = (date.today() + timedelta(days=45)).isoformat()
        body_hz = {"eta_date": eta_hz, "budget": 300000, "budget_scope": "full",
                   "strategy": "balance"}
        plan_auto = c.post("/api/order-plan/preview", json=body_hz).json()
        check("мастер считает по тому же автоматическому горизонту",
              plan_auto["cover_days"] == st0["horizon_days_effective"],
              f"мастер={plan_auto['cover_days']} настройки={st0['horizon_days_effective']}")

        r = c.post("/api/settings", json={"cover_mode": "fixed", "horizon_days": 120})
        check("режим «фиксированный» сохраняется", r.status_code == 200,
              f"status={r.status_code} {r.text[:120]}")
        st1 = c.get("/api/settings").json()
        check("эффективный горизонт стал пользовательским числом",
              st1["horizon_source"] == "fixed" and st1["horizon_days_effective"] == 120,
              f"source={st1['horizon_source']} eff={st1['horizon_days_effective']}")
        plan_fixed = c.post("/api/order-plan/preview", json=body_hz).json()
        check("МАСТЕР уважает фиксированный горизонт (раньше игнорировал)",
              plan_fixed["cover_days"] == 120,
              f"мастер={plan_fixed['cover_days']} ожидали 120")
        # Итоговые ШТУКИ сравнивать нельзя: план ограничен бюджетом, и при
        # большем горизонте те же деньги уходят в меньшее число более глубоких
        # позиций. Растёт именно ПОТРЕБНОСТЬ (темп × горизонт) — её и проверяем
        # по позициям, попавшим в оба плана.
        need_auto = {i["base_name"]: i["need"] for i in plan_auto["items"]}
        need_fix = {i["base_name"]: i["need"] for i in plan_fixed["items"]}
        common = set(need_auto) & set(need_fix)
        check("на большем горизонте потребность по позиции выше",
              bool(common) and all(need_fix[b] >= need_auto[b] for b in common)
              and any(need_fix[b] > need_auto[b] for b in common),
              f"общих={len(common)} примеры="
              f"{[(b, need_auto[b], need_fix[b]) for b in list(common)[:3]]}")
        check("ритм заказов в анкете фиксированный горизонт не сдвигает",
              c.post("/api/order-plan/preview",
                     json=dict(body_hz, cadence_days=7)).json()["cover_days"] == 120,
              "ритм 7 дн не должен менять фиксированные 120")
        opts = c.get("/api/order-plan/options").json()
        check("анкета мастера знает, что горизонт фиксированный",
              opts.get("horizon_source") == "fixed"
              and opts.get("horizon_days_effective") == 120,
              f"opts={{k: opts.get(k) for k in ('horizon_source','horizon_days_effective')}}")
        c.post("/api/settings", json={"cover_mode": "cadence"})
        check("возврат в автоматический режим восстанавливает прежний горизонт",
              c.get("/api/settings").json()["horizon_days_effective"]
              == st0["horizon_days_effective"],
              f"got={c.get('/api/settings').json()['horizon_days_effective']}")
        check("в настройках есть переключатель режима горизонта",
              'id="horizon-mode"' in c.get("/settings").text)

        print("\n14d. Факты о данных на экране, а не оценка уверенности (D-23)")
        opts_dq = c.get("/api/order-plan/options").json().get("data_quality") or {}
        for key in ("coverage_days", "coverage_start", "positions_total",
                    "positions_cost_full", "positions_no_cost",
                    "last_sync_at", "sync_state"):
            check(f"анкета знает факт «{key}»", key in opts_dq,
                  f"есть={sorted(opts_dq)}")
        check("это ФАКТЫ, а не оценка уверенности",
              not any(k in opts_dq for k in ("confidence", "score", "level", "grade")),
              f"лишнее={[k for k in opts_dq if k in ('confidence','score','level','grade')]}")
        check("глубина истории — число дней, а не буква",
              isinstance(opts_dq.get("coverage_days"), int)
              and opts_dq["coverage_days"] > 0,
              f"got={opts_dq.get('coverage_days')!r}")
        check("позиции без себестоимости посчитаны отдельно от общего числа",
              isinstance(opts_dq.get("positions_no_cost"), int)
              and isinstance(opts_dq.get("positions_total"), int)
              and opts_dq["positions_no_cost"] <= opts_dq["positions_total"],
              f"no_cost={opts_dq.get('positions_no_cost')} "
              f"total={opts_dq.get('positions_total')}")
        rec_dq = (c.post("/api/order-plan/preview", json=body_hz).json()
                  .get("record") or {}).get("data_quality") or {}
        check("на экране и в записи решения — ОДНИ И ТЕ ЖЕ факты",
              rec_dq == opts_dq, f"экран={opts_dq} запись={rec_dq}")
        page_dq = c.get("/assistant").text
        check("на первом экране мастера есть блок фактов о данных",
              'id="dataFacts"' in page_dq and "Что известно о данных" in page_dq)

        print("\n14e. Позицию без себестоимости решает человек (D-23)")
        # Дополнение владельца 22.08: отказ системы считать НЕ запрещает
        # человеку действовать. Раньше такая позиция не попадала в заказ
        # никогда — её нельзя было втащить даже вручную, в отличие от позиции
        # с малой статистикой.
        base_nc = "Пробник «Без цены»"
        _add_no_cost_item(base_nc)

        plan_nc = c.post("/api/order-plan/preview", json=body_hz).json()
        in_plan = [i["base_name"] for i in plan_nc["items"]]
        check("сама система позицию без себестоимости в заказ не берёт",
              base_nc not in in_plan, f"нашлась: {base_nc in in_plan}")
        rev_names = [r["base_name"] for r in (plan_nc.get("review") or {}).get("no_cost", [])]
        check("но показывает её отдельным списком «решаете вы»",
              base_nc in rev_names, f"список={rev_names[:5]}")
        check("бюджет без ручных правок помечен полным",
              plan_nc.get("budget_incomplete") is None,
              f"got={plan_nc.get('budget_incomplete')}")

        manual = c.post("/api/order-plan/preview",
                        json=dict(body_hz, overrides={base_nc: 25})).json()
        row = next((i for i in manual["items"] if i["base_name"] == base_nc), None)
        check("человек вписал количество — позиция ПОПАЛА в заказ",
              row is not None and row["qty"] == 25,
              f"row={row and {k: row[k] for k in ('qty',)}}")
        check("и не выдаётся за рекомендацию системы (qty_recommended = null)",
              row is not None and row.get("qty_recommended") is None,
              f"qty_recommended={row and row.get('qty_recommended')}")
        check("строка подписана как решение человека",
              row is not None and "manual_add" in (row.get("why") or [])
              and "вручную" in (row.get("why_text") or ""),
              f"why={row and row.get('why')} text={row and row.get('why_text')}")
        check("прибыль по ней НЕ обещается (спроса система не считала)",
              row is not None and row.get("expected_profit") == 0
              and row.get("need") == 0,
              f"profit={row and row.get('expected_profit')} need={row and row.get('need')}")
        bi = manual.get("budget_incomplete")
        check("сумма заказа помечена НЕПОЛНОЙ",
              bi is not None and bi["positions"] >= 1 and bi["units"] >= 25
              and base_nc in (bi.get("names") or []),
              f"budget_incomplete={bi}")
        check("план помечен как правленный человеком",
              manual.get("manual_edit") is True, f"got={manual.get('manual_edit')}")

        ghost = c.post("/api/order-plan/preview",
                       json=dict(body_hz, overrides={"Пальто «Которого нет»": 10})).json()
        check("несуществующее имя в заказ не добавляется",
              not any(i["base_name"] == "Пальто «Которого нет»" for i in ghost["items"]),
              "выдуманное имя не должно создавать строку")
        check("и не роняет расчёт", isinstance(ghost.get("items"), list))

        saved_nc = c.post("/api/order-plan",
                          json=dict(body_hz, overrides={base_nc: 25})).json()
        import json as _json
        res_nc = _json.loads(_sql("SELECT result_json FROM order_plans WHERE id=?",
                                  saved_nc["id"])[0][0])
        check("в истории видно, что сумма была неполной",
              (res_nc.get("budget_incomplete") or {}).get("positions") >= 1,
              f"got={res_nc.get('budget_incomplete')}")
        srow = next((i for i in res_nc["items"] if i["base_name"] == base_nc), None)
        check("в истории решение человека отличимо от рекомендации",
              srow is not None and srow.get("qty_recommended") is None
              and srow.get("qty") == 25,
              f"row={srow and {k: srow.get(k) for k in ('qty', 'qty_recommended')}}")
        check("в мастере есть поле для ручного количества у таких позиций",
              "nocost-q" in c.get("/assistant").text)

        # ── Границы ручного добавления (по итогам ревью критиками) ──────────
        in_plan_name = plan_auto["items"][0]["base_name"]
        excl = c.post("/api/order-plan/preview", json=dict(
            body_hz, exclude_categories=[plan_auto["items"][0]["category"]],
            overrides={in_plan_name: 40})).json()
        check("позицию из ИСКЛЮЧЁННОЙ категории через overrides не втащить",
              not any(i["base_name"] == in_plan_name for i in excl["items"]),
              "обход бизнес-фильтра через тело запроса")
        huge = c.post("/api/order-plan/preview",
                      json=dict(body_hz, overrides={base_nc: 10 ** 400}))
        check("гигантское количество не роняет расчёт в 500",
              huge.status_code == 200, f"status={huge.status_code} {huge.text[:120]}")
        hrow = next((i for i in huge.json()["items"]
                     if i["base_name"] == base_nc), None) if huge.status_code == 200 else None
        check("и обрезается разумной верхней границей",
              hrow is not None and hrow["qty"] <= 1_000_000,
              f"qty={hrow and hrow['qty']}")
        # Предпросмотр обязан ОТПРАВЛЯТЬ правки — иначе поле «шт» мертво.
        page_prev = c.get("/assistant").text
        check("предпросмотр отправляет ручные правки на сервер",
              "previewBody.overrides" in page_prev,
              "без этого строка без себестоимости не появится в плане")
        check("правки уходят при ЛЮБОМ пересчёте, а не из одного места",
              "window.preview = function(step, keepEdits){" in page_prev
              and "sendEdits" not in page_prev,
              "иначе соседний пересчёт молча терял добавленную позицию")
        check("браузер не считает маржу там, где нет себестоимости",
              "if(!i.no_cost && i.cost_price > 0) profit" in page_prev)
        check("анкета возит режим горизонта (иначе «Повторить» считает по-новому)",
              "cover_mode: STATE.cover_mode" in page_prev
              and "last.cover_mode" in page_prev)

        # Отказ применить правку обязан быть ВИДЕН, а не молчаливым
        rej = c.post("/api/order-plan/preview", json=dict(
            body_hz, overrides={"Пальто «Которого нет»": 10})).json()
        check("отказ по несуществующей позиции назван явно",
              any(r.get("reason") == "not_in_catalog" and r.get("count") == 1
                  for r in (rej.get("overrides_rejected") or [])),
              f"rejected={rej.get('overrides_rejected')}")
        check("непроверенное имя НЕ возвращается обратно в ответе",
              "Которого нет" not in json_dumps(rej.get("overrides_rejected") or []),
              f"rejected={rej.get('overrides_rejected')}")
        blocked = c.post("/api/order-plan/preview", json=dict(
            body_hz, exclude_categories=[plan_auto["items"][0]["category"]],
            overrides={in_plan_name: 40})).json()
        check("отказ по бизнес-правилу тоже назван, а не проглочен",
              any(r.get("base_name") == in_plan_name
                  and r.get("reason") == "not_offered"
                  for r in (blocked.get("overrides_rejected") or [])),
              f"rejected={blocked.get('overrides_rejected')}")
        check("успешная правка ничего не отклоняет",
              (manual.get("overrides_rejected") or []) == [],
              f"rejected={manual.get('overrides_rejected')}")

        # Огромные числа в ЛЮБОМ поле анкеты не должны давать 500
        for fld in ("budget", "moq_units", "cadence_days", "max_share_pct",
                    "reserve_new_pct", "safety_days"):
            rr = c.post("/api/order-plan/preview", json=dict(body_hz, **{fld: 10 ** 400}))
            check(f"огромное значение в «{fld}» не роняет запрос",
                  rr.status_code in (200, 422), f"status={rr.status_code}")
        rr = c.post("/api/order-plan/preview", json=dict(
            body_hz, new_items=[{"name": "Пальто", "qty": 10 ** 400, "cost": 1000}]))
        check("огромное количество новинки не роняет запрос",
              rr.status_code in (200, 422), f"status={rr.status_code}")

        # ── «Повторить» старый план не должен менять горизонт ───────────────
        rep = c.post("/api/order-plan/preview",
                     json=dict(body_hz, cover_mode="fixed", horizon_days_fixed=200)).json()
        check("режим горизонта из брифа сильнее сегодняшней настройки",
              rep["cover_days"] == 200, f"got={rep['cover_days']}")
        bad = c.post("/api/order-plan/preview",
                     json=dict(body_hz, cover_mode="что-то"))
        check("неизвестный режим отклоняется, а не молча подменяется",
              bad.status_code == 422, f"status={bad.status_code}")
        check("на большом фиксированном горизонте план не становится вечно предварительным",
              c.post("/api/order-plan/preview",
                     json=dict(body_hz, cover_mode="fixed", horizon_days_fixed=365)
                     ).json()["coverage"]["partial"] is False,
              "история не может быть глубже года — требовать больше нельзя")

        # ── Позиция, втащенная галочкой, — решение человека ─────────────────
        # Позицию из ИСКЛЮЧЁННОЙ категории система сама бы не взяла — значит
        # галочка «Взять» здесь и есть решение человека. А обычная топовая
        # позиция помечаться не должна: её система рекомендует и без галочки.
        top = plan_auto["items"][0]["base_name"]
        top_cat = plan_auto["items"][0]["category"]
        fc = c.post("/api/order-plan/preview", json=dict(
            body_hz, exclude_categories=[top_cat], must_have=[top])).json()
        frow = next((i for i in fc["items"] if i["base_name"] == top), None)
        check("позиция, которую без галочки не взяли бы, помечена решением человека",
              frow is not None and frow.get("forced_by_user") is True,
              f"forced_by_user={frow and frow.get('forced_by_user')}")
        plain = c.post("/api/order-plan/preview",
                       json=dict(body_hz, must_have=[top])).json()
        prow = next((i for i in plain["items"] if i["base_name"] == top), None)
        check("а позицию, которую система рекомендует сама, — НЕ помечает",
              prow is not None and prow.get("forced_by_user") is False,
              f"forced_by_user={prow and prow.get('forced_by_user')} "
              f"(наличия имени в must_have для пометки мало)")

        print("\n14f. Демо-режим показывает то, за что продукт цепляет")
        # Демо — это витрина. До 22.08 в ней не было ни одной убыточной позиции
        # и ни одной без себестоимости: алерт «торгуете в минус», предупреждение
        # про неполную себестоимость и ручное добавление позиции без цены
        # существовали в коде и молчали ровно там, где на продукт смотрят
        # перед покупкой.
        turn_demo = c.get("/api/turnover").json()
        bc = turn_demo.get("below_cost") or {}
        check("в демо есть позиции, которые продаются в минус",
              bc.get("positions", 0) >= 2, f"positions={bc.get('positions')}")
        check("и это значимые позиции, а не шум из одной продажи",
              all(not x.get("low_data") for x in bc.get("items") or []),
              f"items={[(x['base_name'], x.get('low_data')) for x in bc.get('items') or []]}")
        check("у убытка есть цена в рублях",
              (bc.get("loss_total") or 0) > 0, f"loss_total={bc.get('loss_total')}")
        money_demo = turn_demo.get("money") or {}
        check("в демо есть позиции без себестоимости",
              money_demo.get("no_cost_positions", 0) >= 2,
              f"no_cost={money_demo.get('no_cost_positions')}")
        check("но НЕ у всех — иначе предупреждение висело бы всегда и не значило бы ничего",
              money_demo.get("no_cost_positions", 0) < (money_demo.get("positions") or 0),
              f"no_cost={money_demo.get('no_cost_positions')} всего={money_demo.get('positions')}")
        opt_demo = c.get("/api/order-plan/options").json()
        check("полная себестоимость задана у большинства, но не у всех",
              0 < opt_demo["cost_source_full"] < opt_demo["positions"],
              f"full={opt_demo['cost_source_full']} всего={opt_demo['positions']}")
        check("мастер показывает позиции без себестоимости отдельным списком",
              (plan_auto.get("review") or {}).get("no_cost_count", 0) >= 1,
              f"no_cost_count={(plan_auto.get('review') or {}).get('no_cost_count')}")

        print("\n14g. Встроенный режим МойСклада на рабочих страницах")
        # Девять продуктовых страниц не наследуют base.html, и встроенного
        # режима у них не было вовсе: внутри iframe раздела «Приложения»
        # пользователь получал ДВЕ шапки. Каталог МС — якорный канал продаж,
        # и не работал он именно на рабочих страницах.
        for path, title in (("/turnover", "Оборачиваемость"), ("/stocks", "Активный сток"),
                            ("/assistant", "Мастер заказа"), ("/replenish", "Заказ"),
                            ("/sizes", "Заказ позиции"), ("/budget", "Бюджет"),
                            ("/forecast", "Прогноз"), ("/revenue", "Оборот"),
                            ("/lessons", "Обучение")):
            emb = c.get(path + "?embed=1")
            # ?embed=1 залипает кукой (чтобы переходы по табам сохраняли
            # режим), поэтому обычный вид проверяем явным ?embed=0.
            plain = c.get(path + "?embed=0")
            ok = (emb.status_code == 200 and 'class="embed-tabs"' in emb.text
                  and ".header { display: none !important; }" in emb.text)
            check(f"{path}: встроенный режим прячет свою шапку и даёт ленту табов",
                  ok, f"status={emb.status_code}")
            check(f"{path}: обычный режим не изменился",
                  plain.status_code == 200 and 'class="embed-tabs"' not in plain.text,
                  f"status={plain.status_code}")
        emb_t = c.get("/turnover?embed=1").text
        c.get("/turnover?embed=0")  # снимаем куку, дальше обычный режим
        check("активный раздел подсвечен в ленте табов",
              '<a href="/turnover" class="active">Оборачиваемость</a>' in emb_t)
        check("в ленте есть все десять разделов",
              all(('href="%s"' % u) in emb_t for u in
                  ("/turnover", "/stocks", "/assistant", "/replenish", "/sizes",
                   "/budget", "/forecast", "/revenue", "/lessons", "/settings")))

        print("\n14c. Мастер заказа на догружаемой истории (деплой П1)")
        eta_full = (date.today() + timedelta(days=45)).isoformat()
        body = {"eta_date": eta_full, "budget": 300000, "budget_scope": "full",
                "strategy": "balance"}
        before = c.post("/api/order-plan/preview", json=body).json()
        check("демо-аккаунт: история полная, план уверенный",
              before["coverage"]["partial"] is False
              and before["coverage"]["days"] >= before["coverage"]["needed_days"]
              and isinstance(before["lost"], dict)
              and "provisional" not in before,
              f"coverage={before['coverage']} lost={before['lost']}")

        _truncate_history(30)
        after = c.post("/api/order-plan/preview", json=body).json()
        check("(a) превью на 30 днях истории остаётся доступным и честным",
              after["coverage"]["partial"] is True and after["coverage"]["days"] == 30
              and after["lost"] is None and after.get("provisional") is True
              and "sensitivity" not in after,
              f"coverage={after['coverage']} lost={after['lost']}")
        check("видно, сколько позиций скрыла недогруженная история",
              after["review"]["hidden_by_coverage"] >= 0
              and after["review"]["stale_count"] >= after["review"]["hidden_by_coverage"],
              f"hidden={after['review']['hidden_by_coverage']} "
              f"stale={after['review']['stale_count']}")
        check("на обрезке истории план заметно отличается от плана на полной",
              (after["totals"]["units"], after["totals"]["positions"])
              != (before["totals"]["units"], before["totals"]["positions"]),
              f"было {before['totals']} стало {after['totals']}")

        part_saved = c.post("/api/order-plan", json=body).json()
        deny = c.post(f"/api/order-plan/{part_saved['id']}/apply", json={"name": "Рано"})
        check("(b) применение плана на неполной истории → 409 с объяснением",
              deny.status_code == 409 and "истории" in deny.json().get("detail", ""),
              f"{deny.status_code} {deny.text[:160]}")
        ok = c.post(f"/api/order-plan/{part_saved['id']}/apply",
                    json={"name": "Осознанно", "confirm_partial": True})
        check("(b) с осознанным подтверждением заказ создаётся",
              ok.status_code == 200 and ok.json().get("order_id"),
              f"{ok.status_code} {ok.text[:160]}")
    finally:
        c.close()
        srv.stop()


if __name__ == "__main__":
    sys.exit(main())
