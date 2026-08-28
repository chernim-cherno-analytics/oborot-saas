# -*- coding: utf-8 -*-
"""Канон формулы оборачиваемости (D-35, решение владельца 23.08.2026).

Что закреплено (BUSINESS_LOGIC.md §0, докстринг app/analytics.py):

  оборачиваемость = (продажи − возвраты) ÷ дни в стоке,
  числитель и знаменатель — ПО ОДНИМ И ТЕМ ЖЕ ДНЯМ:

  * окно — скользящие TURNOVER_WINDOW_DAYS (2 года); что старше — не входит;
  * «день в стоке» = дата с суммарным ПО БАЗЕ остатком >= min_stock_days
    (порог глубины по базе, дефолт 3), день за днём, БЕЗ каких-либо весов;
  * числитель (nris/nqis) — нетто-продажи ТОЛЬКО этих дней: продажи дат,
    когда вещи не было или глубина ниже порога, скорость не завышают;
  * денежный слой (nq/nr, средняя цена, маржа) остаётся годовым,
    rate_year планировщика = nq / dis365 (годовое поле дней в стоке).

Формулы не меняются никем, кроме владельца, — эти тесты и есть замок:
если какая-нибудь «оптимизация» их сломала, откатывайте оптимизацию,
а не правьте ожидания (правка ожиданий — только новой записью в DECISIONS.md).

Замок держится на трёх вещах (ревью PR #12, раунд 2):

  * окно 730 закреплено ЯВНЫМ числом в тесте (CANON_WINDOW_DAYS), а не берётся
    из продуктовой константы — иначе тест переезжает вслед за правкой, которую
    он должен ловить;
  * есть даты ВТОРОГО года (≈500 дней назад и точные границы 729/730), на
    которых годовое окно и канонное расходятся;
  * есть отрицательный контроль: тот же расчёт с окном 365 обязан УРОНИТЬ
    канонные ожидания, оставив годовой слой (nq/nr/dis365/rate_year) прежним.

Два краевых контракта канона добавлены отдельно (D35-EDGE-CONTRACTS):

  * нетто может быть ОТРИЦАТЕЛЬНЫМ (возвраты больше продаж), и знак
    сохраняется в nris/nqis и годовых nr/nq — при dis > 0 нулём становится
    только turnover, по уже действующему max(0, nris)/dis;
  * деньги берутся из фактической выручки документов, а не из текущей цены
    карточки: nris/nr и avg_price складываются из Sale.revenue, даже когда
    Product.sale_price отличается от всех фактических цен продаж.

Запуск из корня репозитория:  python tests/test_turnover_canon.py
"""
import os
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "test_turnover_canon.db"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["SCHEDULER_ENABLED"] = "0"

for _p in (DB_PATH, Path(str(DB_PATH) + "-wal"), Path(str(DB_PATH) + "-shm")):
    if _p.exists():
        _p.unlink()

from app import analytics  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.models import Org, Product, Sale, StockDay  # noqa: E402

Base.metadata.create_all(engine)

# Решение владельца (D-35): окно канона — ровно два года, 730 дат
# (today−729 … today включительно). Число здесь ЯВНОЕ и продуктовой константе
# не наследуется: тест обязан ловить её правку, а не переезжать вместе с ней.
CANON_WINDOW_DAYS = 730
# Прежнее (годовое) окно — им же считается отрицательный контроль ниже.
YEAR_WINDOW_DAYS = 365
# Дата глубоко во втором году: в канон входит, в год — нет.
DEEP_DAYS_AGO = 500

FAILED = []
PASSED = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  OK   " if ok else "  FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    (PASSED if ok else FAILED).append(name)


def main() -> int:
    db = SessionLocal()
    today = date.today()

    print("== Ширина окна канона зафиксирована решением владельца ==")
    check("TURNOVER_WINDOW_DAYS == 730 (два года, D-35)",
          analytics.TURNOVER_WINDOW_DAYS == CANON_WINDOW_DAYS,
          f"TURNOVER_WINDOW_DAYS={analytics.TURNOVER_WINDOW_DAYS}")
    check("окно канона шире годового (иначе весь второй год теряется)",
          analytics.TURNOVER_WINDOW_DAYS > YEAR_WINDOW_DAYS)

    org = Org(name="canon-test")
    db.add(org)
    db.flush()

    prod = Product(org_id=org.id, base_name="Рубашка Канон", size="M",
                   category="shirts", sale_price=10000, cost_price=3000)
    db.add(prod)
    db.flush()

    def iso(days_ago: int) -> str:
        return (today - timedelta(days=days_ago)).isoformat()

    def season_of(days_ago: int) -> str:
        m = (today - timedelta(days=days_ago)).month
        return {12: "winter", 1: "winter", 2: "winter",
                3: "spring", 4: "spring", 5: "spring",
                6: "summer", 7: "summer", 8: "summer"}.get(m, "autumn")

    # Блок из 15 дат целиком внутри ОДНОГО сезона (иначе сезонные проверки
    # ниже зависят от календаря запуска). k <= 14 — сдвиг от сегодня.
    k = 0
    while len({season_of(k + i) for i in range(15)}) > 1:
        k += 1
    test_season = season_of(k)

    # Сток: 15 дат блока; дни k+5..k+7 — остаток 0 (вещи не было),
    # остальные 12 — остаток 5 (>= порога 3).
    out_of_stock = {k + 5, k + 6, k + 7}
    for d in range(k, k + 15):
        db.add(StockDay(org_id=org.id, product_id=prod.id, date=iso(d),
                        qty=0.0 if d in out_of_stock else 5.0))
    # Древний сток вне окна канона (2 года): не должен попасть в dis.
    # Даты считаются от ЯВНОГО 730, а не от продуктовой константы, — иначе
    # «древняя эра» уезжает вслед за подменой окна и ничего не проверяет.
    ancient = range(CANON_WINDOW_DAYS + 5, CANON_WINDOW_DAYS + 10)
    for d in ancient:
        db.add(StockDay(org_id=org.id, product_id=prod.id, date=iso(d), qty=5.0))

    # Продажи: 1000 ₽/шт в каждую из 15 дат блока (включая дни отсутствия),
    # плюс продажа в древнюю эру (вне окна) и один возврат в день «в стоке».
    for d in range(k, k + 15):
        db.add(Sale(org_id=org.id, product_id=prod.id, date=iso(d),
                    qty=1, revenue=1000, is_return=False))
    db.add(Sale(org_id=org.id, product_id=prod.id,
                date=iso(CANON_WINDOW_DAYS + 6),
                qty=1, revenue=1000, is_return=False))
    db.add(Sale(org_id=org.id, product_id=prod.id, date=iso(k + 1),
                qty=1, revenue=1000, is_return=True))
    db.commit()

    snap = analytics.get_snapshot(db, org)
    it = snap["items"]["Рубашка Канон"]

    print("== Канон: окно, день за днём, порог по базе ==")
    check("dis: только окно канона и только дни с остатком >= порога (15−3=12)",
          it["dis"] == 12, f"dis={it['dis']}")
    check("древние дни (старше 2 лет) в dis не входят", it["dis"] == 12)
    check("dis365 (годовое поле для темпа планировщика) совпадает здесь с dis",
          it["dis365"] == 12, f"dis365={it['dis365']}")

    print("== Канон: числитель выровнен со знаменателем ==")
    # В стоке 12 дней; в день iso(k+1) продажа 1000 и возврат 1000 → нетто 0.
    # nris = 12×1000 − 1000(возврат) = 11000; продажи дней отсутствия (3×1000)
    # и древняя продажа в числитель не входят.
    check("nris — нетто-продажи только дней «в стоке»",
          abs(it["nris"] - 11000) < 1e-6, f"nris={it['nris']}")
    check("nqis согласован (12 − 1 возврат = 11)",
          abs(it["nqis"] - 11) < 1e-6, f"nqis={it['nqis']}")
    check("оборачиваемость = nris/dis, округлённая",
          it["turnover"] == round(11000 / 12), f"turnover={it['turnover']}")

    print("== Сезонная оборачиваемость выровнена так же ==")
    # Все даты блока — один сезон: сезонный ₽/день обязан совпасть с
    # turnover (тот же выровненный числитель ÷ те же дни), а не быть выше
    # него за счёт продаж дней «не в стоке».
    sea = it.get("sea") or {}
    check("сезонный ₽/день = nris/dis (числитель сезона тоже выровнен)",
          sea.get(test_season) == round(11000 / 12),
          f"sea[{test_season}]={sea.get(test_season)}")
    check("в остальных сезонах продаж нет (0 или сезон не покрыт)",
          all(not v for s_, v in sea.items() if s_ != test_season),
          f"sea={sea}")

    print("== Денежный слой остаётся годовым ==")
    # nq/nr за год: 15 продаж − 1 возврат = 14 шт / 14000 ₽ (древняя продажа
    # старше года и не входит; блок дат k..k+14 всегда внутри года, k <= 14).
    check("nq за год (продажи минус возвраты, дни отсутствия входят)",
          abs(it["nq"] - 14) < 1e-6, f"nq={it['nq']}")
    check("nr за год", abs(it["nr"] - 14000) < 1e-6, f"nr={it['nr']}")
    check("rate_year = nq/dis365",
          abs(it["rate_year"] - round(14 / 12, 4)) < 1e-3,
          f"rate_year={it['rate_year']}")

    print("== Продажи дней «ниже порога глубины» не завышают скорость ==")
    org2 = Org(name="canon-test-2")
    db.add(org2)
    db.flush()
    p2 = Product(org_id=org2.id, base_name="Хвост Неликвида", size="",
                 category="shirts", sale_price=5000)
    db.add(p2)
    db.flush()
    # 10 дней лежит 1 шт (ниже порога 3), в один из дней — продажа.
    for d in range(10):
        db.add(StockDay(org_id=org2.id, product_id=p2.id, date=iso(d), qty=1.0))
    db.add(Sale(org_id=org2.id, product_id=p2.id, date=iso(3),
                qty=1, revenue=5000, is_return=False))
    db.commit()
    snap2 = analytics.get_snapshot(db, org2)
    it2 = snap2["items"]["Хвост Неликвида"]
    check("глубина ниже порога: dis = 0", it2["dis"] == 0, f"dis={it2['dis']}")
    check("и оборачиваемость честно 0 (а не 5000/1 день)",
          it2["turnover"] == 0, f"turnover={it2['turnover']}")
    check("выручка при этом видна в годовом nr (не потеряна)",
          abs(it2["nr"] - 5000) < 1e-6, f"nr={it2['nr']}")

    print("== Второй год: канон его видит, годовой слой — нет ==")
    # Реальная дата ≈500 дней назад: внутри окна канона (730) и вне года (365).
    # Именно на ней два окна расходятся — без такой даты годовая реализация
    # проходит весь набор (ревью PR #12, раунд 2).
    org3 = Org(name="canon-test-deep")
    db.add(org3)
    db.flush()
    p3 = Product(org_id=org3.id, base_name="Пальто Двухлетка", size="M",
                 category="outerwear", sale_price=20000, cost_price=6000)
    db.add(p3)
    db.flush()
    # 10 свежих дней «в стоке» + один день во втором году.
    for d in range(10):
        db.add(StockDay(org_id=org3.id, product_id=p3.id, date=iso(d), qty=5.0))
    db.add(StockDay(org_id=org3.id, product_id=p3.id,
                    date=iso(DEEP_DAYS_AGO), qty=5.0))
    db.add(Sale(org_id=org3.id, product_id=p3.id, date=iso(2),
                qty=1, revenue=2000, is_return=False))
    db.add(Sale(org_id=org3.id, product_id=p3.id, date=iso(DEEP_DAYS_AGO),
                qty=1, revenue=7000, is_return=False))
    db.commit()
    it3 = analytics.get_snapshot(db, org3)["items"]["Пальто Двухлетка"]

    check("dis включает день второго года (10 свежих + 1 = 11)",
          it3["dis"] == 11, f"dis={it3['dis']}")
    check("nqis включает продажу второго года (1 + 1 = 2)",
          abs(it3["nqis"] - 2) < 1e-6, f"nqis={it3['nqis']}")
    check("nris включает выручку второго года (2000 + 7000)",
          abs(it3["nris"] - 9000) < 1e-6, f"nris={it3['nris']}")
    check("turnover посчитан с учётом второго года (9000/11)",
          it3["turnover"] == round(9000 / 11), f"turnover={it3['turnover']}")
    check("годовой dis365 день второго года НЕ считает (10)",
          it3["dis365"] == 10, f"dis365={it3['dis365']}")
    check("годовой nq продажу второго года НЕ считает (1)",
          abs(it3["nq"] - 1) < 1e-6, f"nq={it3['nq']}")
    check("годовой nr продажу второго года НЕ считает (2000)",
          abs(it3["nr"] - 2000) < 1e-6, f"nr={it3['nr']}")
    check("rate_year = nq/dis365 — без второго года (1/10)",
          abs(it3["rate_year"] - round(1 / 10, 4)) < 1e-6,
          f"rate_year={it3['rate_year']}")
    check("turnover канона и «годовой turnover» (2000/10) — разные числа",
          it3["turnover"] != round(2000 / 10), f"turnover={it3['turnover']}")

    print("== Границы окна: 729-й день внутри, 730-й уже нет ==")
    # Окно — ровно 730 дат: today−729 … today. Значит день «минус 729»
    # последний внутри, а день «минус 730» — первый снаружи.
    org4 = Org(name="canon-test-edge")
    db.add(org4)
    db.flush()
    p4 = Product(org_id=org4.id, base_name="Свеча Границы", size="",
                 category="decor", sale_price=3000)
    db.add(p4)
    db.flush()
    for d, rev in ((CANON_WINDOW_DAYS - 1, 3000), (CANON_WINDOW_DAYS, 4000)):
        db.add(StockDay(org_id=org4.id, product_id=p4.id, date=iso(d), qty=5.0))
        db.add(Sale(org_id=org4.id, product_id=p4.id, date=iso(d),
                    qty=1, revenue=rev, is_return=False))
    db.commit()
    it4 = analytics.get_snapshot(db, org4)["items"]["Свеча Границы"]

    check("в dis попал ровно один день — 729-й (730-й вне окна)",
          it4["dis"] == 1, f"dis={it4['dis']}")
    check("nqis = 1 (продажа 730-го дня не вошла)",
          abs(it4["nqis"] - 1) < 1e-6, f"nqis={it4['nqis']}")
    check("nris = 3000 (выручка 729-го; 4000 за границей окна)",
          abs(it4["nris"] - 3000) < 1e-6, f"nris={it4['nris']}")
    check("turnover = 3000/1", it4["turnover"] == 3000,
          f"turnover={it4['turnover']}")
    check("годовой слой обе даты не видит: dis365 = 0, nq = 0, nr = 0",
          it4["dis365"] == 0 and abs(it4["nq"]) < 1e-6 and abs(it4["nr"]) < 1e-6,
          f"dis365={it4['dis365']} nq={it4['nq']} nr={it4['nr']}")

    print("== Порог глубины — по БАЗЕ, а не по размеру ==")
    # Канон: «день в стоке» — дата, где СУММАРНЫЙ по базе остаток >= порога.
    # Реализация с порогом по SKU потеряла бы день, где каждый размер лежит
    # по 2 шт (ниже порога 3), а вместе их 4. И наоборот: join к дням «в
    # стоке» не должен удваивать продажу за то, что размеров два.
    org5 = Org(name="canon-test-sizes")
    db.add(org5)
    db.flush()
    p5m = Product(org_id=org5.id, base_name="Свитер Два Размера", size="M",
                  category="knitwear", sale_price=8000)
    p5l = Product(org_id=org5.id, base_name="Свитер Два Размера", size="L",
                  category="knitwear", sale_price=8000)
    db.add_all([p5m, p5l])
    db.flush()
    # День 1: M = 2 и L = 2 → по отдельности ниже порога 3, вместе 4 → день в стоке.
    db.add(StockDay(org_id=org5.id, product_id=p5m.id, date=iso(1), qty=2.0))
    db.add(StockDay(org_id=org5.id, product_id=p5l.id, date=iso(1), qty=2.0))
    # День 2: только M = 2 → суммарно 2 < 3 → день НЕ в стоке (контроль).
    db.add(StockDay(org_id=org5.id, product_id=p5m.id, date=iso(2), qty=2.0))
    db.add(Sale(org_id=org5.id, product_id=p5m.id, date=iso(1),
                qty=1, revenue=6000, is_return=False))
    db.add(Sale(org_id=org5.id, product_id=p5m.id, date=iso(2),
                qty=1, revenue=9000, is_return=False))
    db.commit()
    it5 = analytics.get_snapshot(db, org5)["items"]["Свитер Два Размера"]

    check("два размера по 2 шт дают день «в стоке» (порог по базе): dis = 1",
          it5["dis"] == 1, f"dis={it5['dis']}")
    check("продажа этого дня учтена РОВНО один раз (nqis = 1, не 2)",
          abs(it5["nqis"] - 1) < 1e-6, f"nqis={it5['nqis']}")
    check("выручка не удвоена размерами (nris = 6000, не 12000)",
          abs(it5["nris"] - 6000) < 1e-6, f"nris={it5['nris']}")
    check("turnover = 6000/1", it5["turnover"] == 6000,
          f"turnover={it5['turnover']}")
    check("день ниже порога и его продажа в канон не вошли",
          abs(it5["nris"] - 6000) < 1e-6 and it5["dis"] == 1)
    check("при этом обе продажи видны в годовом nr (15000)",
          abs(it5["nr"] - 15000) < 1e-6, f"nr={it5['nr']}")

    print("== Возвраты больше продаж: знак сохраняется, turnover ровно 0 ==")
    # Нулевая оборачиваемость бывает ДВУХ разных происхождений, и выше
    # проверено только одно: у «Хвоста Неликвида» ноль приходит из
    # ЗНАМЕНАТЕЛЯ (dis = 0 — делить не на что). Здесь дней в стоке пять, и
    # ноль приходит из ЧИСЛИТЕЛЯ. Канон (BUSINESS_LOGIC §0 и §1: «Нетто =
    # продажи минус возвраты… обе величины могут быть отрицательными») велит
    # держать nris/nqis и годовые nr/nq отрицательными, а оборачиваемость
    # отдавать как max(0, nris)/dis. Ни одна из величин не подчищает знак у
    # другой: минус в нетто — факт склада, ноль в скорости — правило показа,
    # и подменять первое вторым нельзя. Замерено RED-контролем (подмена
    # исходника app/analytics.py в памяти, файл на диске не менялся):
    # реализация, которая «на всякий случай» обрезала бы отрицательное нетто
    # в ноль (max(0, ·) на nris_by_base/nqis_by_base), проходила все 44
    # прежние проверки; из нынешних падают ровно две — 55 OK / 2 FAIL.
    org6 = Org(name="canon-test-returns")
    db.add(org6)
    db.flush()
    p6 = Product(org_id=org6.id, base_name="Юбка Возврат", size="M",
                 category="skirts", sale_price=4000, cost_price=1500)
    db.add(p6)
    db.flush()
    # 5 свежих дней «в стоке» (5 шт >= порога 3) — знаменатель заведомо не ноль.
    for d in range(1, 6):
        db.add(StockDay(org_id=org6.id, product_id=p6.id, date=iso(d), qty=5.0))
    # Продажа 1 шт / 1000 и возврат 3 шт / 3000 — оба в дни «в стоке».
    db.add(Sale(org_id=org6.id, product_id=p6.id, date=iso(1),
                qty=1, revenue=1000, is_return=False))
    db.add(Sale(org_id=org6.id, product_id=p6.id, date=iso(2),
                qty=3, revenue=3000, is_return=True))
    db.commit()
    it6 = analytics.get_snapshot(db, org6)["items"]["Юбка Возврат"]

    check("дней в стоке пять — ноль скорости придёт НЕ из знаменателя",
          it6["dis"] == 5, f"dis={it6['dis']}")
    check("nqis сохраняет минус (1 − 3 = −2), а не обнуляется",
          abs(it6["nqis"] - (-2)) < 1e-6, f"nqis={it6['nqis']}")
    check("nris сохраняет минус (1000 − 3000 = −2000)",
          abs(it6["nris"] - (-2000)) < 1e-6, f"nris={it6['nris']}")
    check("turnover ровно 0 по max(0, nris)/dis (а не −400)",
          it6["turnover"] == 0, f"turnover={it6['turnover']}")
    check("годовой nq тоже минус (−2): денежный слой знак не теряет",
          abs(it6["nq"] - (-2)) < 1e-6, f"nq={it6['nq']}")
    check("годовой nr тоже минус (−2000)",
          abs(it6["nr"] - (-2000)) < 1e-6, f"nr={it6['nr']}")

    print("== Цена берётся из выручки документов, а не из карточки ==")
    # §1 задаёт выручку позиции документа как «цена × количество ×
    # (1 − скидка)» — то есть как ФАКТ продажи. Текущая цена карточки
    # (Product.sale_price) — сегодняшний прайс: её меняет переоценка, и
    # историю продаж эта правка не переписывает. Проверяем на позиции,
    # проданной тремя документами по разным фактическим ценам, при цене
    # карточки, не равной ни одной из них: nris/nr и средняя цена обязаны
    # сложиться из выручки документов, а произведение «штуки × цена
    # карточки» не должно всплыть ни в одном поле.
    #
    # Где именно был пробел — замерено RED-контролем (подмена исходника
    # app/analytics.py в памяти, файл на диске не менялся):
    #   * avg_price прежний набор не проверял ВООБЩЕ. Реализация, отдающая
    #     среднюю цену продажи как цену карточки, проходила все 44 прежние
    #     проверки; из нынешних падает ровно одна — 56 OK / 1 FAIL;
    #   * суммы nris/nr, пересчитанные по прайсу, прежний набор ловил и сам
    #     (37 OK / 20 FAIL). Здесь они закреплены дополнительно и явно — на
    #     позиции, где цена карточки не равна ни одной фактической цене
    #     документа, — чтобы контракт держался формулировкой, а не
    #     совпадением чисел в чужой фикстуре.
    org7 = Org(name="canon-test-actual-price")
    db.add(org7)
    db.flush()
    p7 = Product(org_id=org7.id, base_name="Платье Переоценка", size="M",
                 category="dresses", sale_price=9000, cost_price=2000)
    db.add(p7)
    db.flush()
    for d in range(1, 4):
        db.add(StockDay(org_id=org7.id, product_id=p7.id, date=iso(d), qty=5.0))
    # 1 шт за 1200, 1 шт за 800, 2 шт за 2200 → 4 шт на 4200 ₽.
    # Средняя 1050 не совпадает ни с одной ценой документа (1200 / 800 /
    # 1100 за штуку) и тем более с ценой карточки 9000: случайным
    # совпадением такая проверка позеленеть не может.
    for d, qty, rev in ((1, 1, 1200), (2, 1, 800), (3, 2, 2200)):
        db.add(Sale(org_id=org7.id, product_id=p7.id, date=iso(d),
                    qty=qty, revenue=rev, is_return=False))
    db.commit()
    it7 = analytics.get_snapshot(db, org7)["items"]["Платье Переоценка"]

    check("цена карточки отдана как есть (9000) — её никто не пересчитывает",
          abs(it7["sale_price"] - 9000) < 1e-6, f"sale_price={it7['sale_price']}")
    check("nqis = 4 (три документа, четыре штуки)",
          abs(it7["nqis"] - 4) < 1e-6, f"nqis={it7['nqis']}")
    check("nris = 4200 — сумма фактической выручки, а не 4 × 9000",
          abs(it7["nris"] - 4200) < 1e-6, f"nris={it7['nris']}")
    check("nris не равен «штуки × цена карточки» (36000)",
          abs(it7["nris"] - it7["nqis"] * it7["sale_price"]) > 1e-6,
          f"nris={it7['nris']} nqis*sale_price={it7['nqis'] * it7['sale_price']}")
    check("годовой nr = 4200 (тот же факт в денежном слое)",
          abs(it7["nr"] - 4200) < 1e-6, f"nr={it7['nr']}")
    check("avg_price = nr/nq = 1050, а не цена карточки 9000",
          it7["avg_price"] == 1050, f"avg_price={it7['avg_price']}")
    check("turnover = 4200/3 — из фактической выручки дней «в стоке»",
          it7["turnover"] == round(4200 / 3), f"turnover={it7['turnover']}")

    print("== Отрицательный контроль: окно 365 обязано уронить канон ==")
    # Ревью PR #12 (раунд 2): прежний набор оставался ЗЕЛЁНЫМ, если подменить
    # TURNOVER_WINDOW_DAYS на 365 — в нём просто не было дат между 365 и 730,
    # а «древняя эра» вычислялась из самой подменённой константы. Проверяем
    # это прямо: пересчитываем те же организации с годовым окном и требуем,
    # чтобы канонные числа СЛОМАЛИСЬ, а годовой слой не дрогнул.
    # (Первый блок, org «canon-test», к подмене нечувствителен по построению —
    # все его даты внутри года; замок держат org3/org4 ниже.)
    saved_window = analytics.TURNOVER_WINDOW_DAYS
    try:
        analytics.TURNOVER_WINDOW_DAYS = YEAR_WINDOW_DAYS
        for o in (org3, org4, org5):
            analytics.invalidate(o.id)
        bad3 = analytics.get_snapshot(db, org3)["items"]["Пальто Двухлетка"]
        bad4 = analytics.get_snapshot(db, org4)["items"]["Свеча Границы"]
        bad5 = analytics.get_snapshot(db, org5)["items"]["Свитер Два Размера"]
    finally:
        analytics.TURNOVER_WINDOW_DAYS = saved_window
        for o in (org3, org4, org5):
            analytics.invalidate(o.id)

    check("с окном 365 dis второго года ломается (11 → 10)",
          bad3["dis"] == 10 and bad3["dis"] != it3["dis"], f"dis={bad3['dis']}")
    check("с окном 365 nris второго года ломается (9000 → 2000)",
          abs(bad3["nris"] - 2000) < 1e-6, f"nris={bad3['nris']}")
    check("с окном 365 turnover второго года ломается (818 → 200)",
          bad3["turnover"] != it3["turnover"] and bad3["turnover"] == round(2000 / 10),
          f"turnover={bad3['turnover']}")
    check("с окном 365 граничный 729-й день выпадает (dis 1 → 0)",
          bad4["dis"] == 0 and bad4["turnover"] == 0,
          f"dis={bad4['dis']} turnover={bad4['turnover']}")
    check("годовой слой org3 от подмены окна не зависит (nq/nr/dis365/rate_year)",
          (bad3["nq"], bad3["nr"], bad3["dis365"], bad3["rate_year"])
          == (it3["nq"], it3["nr"], it3["dis365"], it3["rate_year"]),
          f"было {(it3['nq'], it3['nr'], it3['dis365'], it3['rate_year'])}, "
          f"стало {(bad3['nq'], bad3['nr'], bad3['dis365'], bad3['rate_year'])}")
    check("годовой слой org4 от подмены окна не зависит",
          (bad4["nq"], bad4["nr"], bad4["dis365"], bad4["rate_year"])
          == (it4["nq"], it4["nr"], it4["dis365"], it4["rate_year"]),
          f"стало {(bad4['nq'], bad4['nr'], bad4['dis365'], bad4['rate_year'])}")
    check("проверка порога по базе от ширины окна не зависит (даты свежие)",
          (bad5["dis"], bad5["nqis"], bad5["nris"], bad5["nr"])
          == (it5["dis"], it5["nqis"], it5["nris"], it5["nr"]),
          f"стало {(bad5['dis'], bad5['nqis'], bad5['nris'], bad5['nr'])}")
    check("константа восстановлена после контроля",
          analytics.TURNOVER_WINDOW_DAYS == CANON_WINDOW_DAYS,
          f"TURNOVER_WINDOW_DAYS={analytics.TURNOVER_WINDOW_DAYS}")

    db.close()
    # Числовой отчёт того же вида, что у остальных наборов: раннер выносит
    # приговор по нему (D-42). Раньше здесь была фраза без чисел — раннер не
    # мог её прочитать, откатывался на подсчёт строк и в принципе не отличил
    # бы оборвавшийся набор от прошедшего целиком.
    print(f"\nИТОГО: {len(PASSED)} OK, {len(FAILED)} FAIL")
    if FAILED:
        for name in FAILED:
            print(f"  FAIL {name}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    code = main()
    for _p in (DB_PATH, Path(str(DB_PATH) + "-wal"), Path(str(DB_PATH) + "-shm")):
        if _p.exists():
            _p.unlink()
    sys.exit(code)
