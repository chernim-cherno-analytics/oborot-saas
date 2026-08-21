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
APP_PORT = 8804
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
    check("посчитана упущенная выручка", tight["lost_revenue"] > 0,
          f"lost={tight['lost_revenue']}")
    broke = op.plan_order(snap, mk_brief(budget=1200), ctx, ONE_STAGE)
    check("совсем не влезшие позиции перечислены отдельно",
          len(broke["not_included"]) > 0 and broke["not_included"][0]["lost_revenue"] > 0,
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
    check("план сообщает, до какой даты закрыт спрос",
          p7["covered_until"] == (TODAY + timedelta(days=45 + 44)).isoformat())

    print("\n14. Покрытие истории (деплой П1: сервис работает, история грузится)")
    # Слепок ПЛАНА ДО правки (снят на коде HEAD d2072d8 на этих же данных):
    # на полном покрытии ни порядок, ни количества, ни причины меняться не
    # должны — иначе правка «честности» тихо переписала бы сам заказ.
    PLAN_BEFORE = [
        ("Топ A", 10, 30000, "кончится до прихода заказа; срезано лимитом на позицию"),
        ("Топ Б", 15, 30000,
         "кончится до прихода заказа; добор до потребности; срезано лимитом на позицию"),
        ("Средний", 20, 30000,
         "кончится до прихода заказа; добор до потребности; срезано лимитом на позицию"),
    ]
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
          and isinstance(p_full["lost_revenue"], (int, float))
          and "provisional" not in p_full and p_full.get("sensitivity"),
          f"partial={p_full['coverage']['partial']} lost={p_full['lost_revenue']}")
    check("(c) на полном покрытии план побайтно тот же, что до правки",
          [(i["base_name"], i["qty"], i["cost_total"], i["why_text"]) for i in p_full["items"]]
          == PLAN_BEFORE
          and p_full["totals"] == {"positions": 3, "units": 45,
                                   "expected_profit": 170000, "expected_revenue": 260000}
          and p_full["lost_revenue"] == 434000 and p_full["spent"] == 90000,
          str([(i["base_name"], i["qty"], i["cost_total"]) for i in p_full["items"]]))
    p_legacy = op.plan_order(mk_snap(cov_items), mk_brief(budget=100000), ctx_cov, ONE_STAGE)
    check("аккаунт без coverage_start (старый/полный) считается как год истории",
          p_legacy["coverage"]["days"] == 365 and p_legacy["coverage"]["partial"] is False
          and {k: v for k, v in p_legacy.items() if k != "coverage"}
          == {k: v for k, v in p_full.items() if k != "coverage"})

    check("(a) 30 дней истории при нужных 89 — покрытие частичное",
          p_part["coverage"]["partial"] is True and p_part["coverage"]["days"] == 30,
          str(p_part["coverage"]))
    check("(a) на обрезке истории упущенная выручка не выдумывается",
          p_part["lost_revenue"] is None, f"lost={p_part['lost_revenue']}")
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

        print("\n14c. Мастер заказа на догружаемой истории (деплой П1)")
        eta_full = (date.today() + timedelta(days=45)).isoformat()
        body = {"eta_date": eta_full, "budget": 300000, "budget_scope": "full",
                "strategy": "balance"}
        before = c.post("/api/order-plan/preview", json=body).json()
        check("демо-аккаунт: история полная, план уверенный",
              before["coverage"]["partial"] is False
              and before["coverage"]["days"] >= before["coverage"]["needed_days"]
              and isinstance(before["lost_revenue"], (int, float))
              and "provisional" not in before,
              f"coverage={before['coverage']} lost={before['lost_revenue']}")

        _truncate_history(30)
        after = c.post("/api/order-plan/preview", json=body).json()
        check("(a) превью на 30 днях истории остаётся доступным и честным",
              after["coverage"]["partial"] is True and after["coverage"]["days"] == 30
              and after["lost_revenue"] is None and after.get("provisional") is True
              and "sensitivity" not in after,
              f"coverage={after['coverage']} lost={after['lost_revenue']}")
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
