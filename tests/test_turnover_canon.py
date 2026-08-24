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

FAILED = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  OK   " if ok else "  FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILED.append(name)


def main() -> int:
    db = SessionLocal()
    today = date.today()

    org = Org(name="canon-test")
    db.add(org)
    db.flush()

    prod = Product(org_id=org.id, base_name="Рубашка Канон", size="M",
                   category="shirts", sale_price=10000, cost_price=3000)
    db.add(prod)
    db.flush()

    def iso(days_ago: int) -> str:
        return (today - timedelta(days=days_ago)).isoformat()

    # Сток: 15 последних дней; дни 5..7 назад — остаток 0 (вещи не было),
    # остальные 12 дней — остаток 5 (>= порога 3).
    out_of_stock = {5, 6, 7}
    for d in range(15):
        db.add(StockDay(org_id=org.id, product_id=prod.id, date=iso(d),
                        qty=0.0 if d in out_of_stock else 5.0))
    # Древний сток вне окна канона (2 года): не должен попасть в dis.
    for d in range(analytics.TURNOVER_WINDOW_DAYS + 5,
                   analytics.TURNOVER_WINDOW_DAYS + 10):
        db.add(StockDay(org_id=org.id, product_id=prod.id, date=iso(d), qty=5.0))

    # Продажи: 1000 ₽/шт в каждый из 15 дней (включая дни отсутствия),
    # плюс продажа в древнюю эру (вне окна) и один возврат в день «в стоке».
    for d in range(15):
        db.add(Sale(org_id=org.id, product_id=prod.id, date=iso(d),
                    qty=1, revenue=1000, is_return=False))
    db.add(Sale(org_id=org.id, product_id=prod.id,
                date=iso(analytics.TURNOVER_WINDOW_DAYS + 6),
                qty=1, revenue=1000, is_return=False))
    db.add(Sale(org_id=org.id, product_id=prod.id, date=iso(1),
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
    # В стоке 12 дней; в день iso(1) продажа 1000 и возврат 1000 → нетто 0.
    # nris = 12×1000 − 1000(возврат) = 11000; продажи дней отсутствия (3×1000)
    # и древняя продажа в числитель не входят.
    check("nris — нетто-продажи только дней «в стоке»",
          abs(it["nris"] - 11000) < 1e-6, f"nris={it['nris']}")
    check("nqis согласован (12 − 1 возврат = 11)",
          abs(it["nqis"] - 11) < 1e-6, f"nqis={it['nqis']}")
    check("оборачиваемость = nris/dis, округлённая",
          it["turnover"] == round(11000 / 12), f"turnover={it['turnover']}")

    print("== Денежный слой остаётся годовым ==")
    # nq/nr за год: 15 продаж − 1 возврат = 14 шт / 14000 ₽ (древняя продажа
    # старше года и не входит).
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

    db.close()
    print(f"\nИтого: {'OK' if not FAILED else 'FAIL'} "
          f"({len(FAILED)} падений)" if FAILED else "\nИтого: все проверки канона зелёные")
    return 1 if FAILED else 0


if __name__ == "__main__":
    code = main()
    for _p in (DB_PATH, Path(str(DB_PATH) + "-wal"), Path(str(DB_PATH) + "-shm")):
        if _p.exists():
            _p.unlink()
    sys.exit(code)
