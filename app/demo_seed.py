"""Детерминированный генератор демо-данных: синтетический fashion-бренд.

~55 базовых позиций в 9 категориях, размеры S/M/L/XL у одежды, One Size у
остального; 400 дней истории stock_days и sales с сезонностью, недельным
циклом, бестселлерами (turnover >= 5000 ₽/день), распроданными в ноль
позициями (cs=0, need>0) и неликвидом без продаж. random.seed(42) — при
повторном подключении демо данные пересоздаются идентично.
"""
import math
import random
from datetime import date, timedelta

from sqlalchemy import delete, insert
from sqlalchemy.orm import Session

from app.models import (
    Org,
    OrderedQty,
    Product,
    ProductionOrder,
    Sale,
    StockDay,
    Warehouse,
    WarehouseStock,
)

HISTORY_DAYS = 400
APPAREL_SIZES = ["S", "M", "L", "XL"]
SIZE_WEIGHTS = {"S": 0.20, "M": 0.35, "L": 0.30, "XL": 0.15}
WAREHOUSES = [
    ("wh-flagship", "Флагман", 0.40),
    ("wh-atrium", "ТЦ Атриум", 0.25),
    ("wh-online", "Интернет-магазин", 0.35),
]

# (base_name, category, sale_price, daily_rate, flags)
# daily_rate — среднесуточные нетто-продажи базы (шт/день) до сезонных множителей.
# flags: 'sellout' — производство остановлено, распродан в ноль;
#        'dead' — продаж нет вовсе (неликвид); 'discount' — глубокие скидки.
CATALOG: list[tuple[str, str, int, float, str]] = [
    # Худи и свитшоты (зимний пик) — 2 бестселлера
    ('Худи «Скетч»', "Худи и свитшоты", 9800, 1.30, ""),
    ('Худи «Штрих»', "Худи и свитшоты", 8900, 1.05, ""),
    ('Свитшот «Академия»', "Худи и свитшоты", 7400, 0.55, ""),
    ('Худи «Молния»', "Худи и свитшоты", 9200, 0.40, "sellout"),
    ('Свитшот «Туман»', "Худи и свитшоты", 6900, 0.28, ""),
    ('Худи «Вечер»', "Худи и свитшоты", 8600, 0.16, ""),
    ('Свитшот «Грань»', "Худи и свитшоты", 6400, 0.10, "discount"),
    ('Худи «Основа»', "Худи и свитшоты", 7900, 0.07, ""),
    # Футболки (летний пик) — 1 бестселлер
    ('Футболка «Манифест»', "Футболки", 3900, 1.80, ""),
    ('Футболка «Чёрным по белому»', "Футболки", 3600, 0.90, ""),
    ('Футболка «Курсив»', "Футболки", 3400, 0.60, "sellout"),
    ('Футболка «Архив 01»', "Футболки", 4200, 0.45, ""),
    ('Футболка «Полночь»', "Футболки", 3300, 0.30, ""),
    ('Футболка «Без слов»', "Футболки", 2900, 0.22, ""),
    ('Футболка «Тираж»', "Футболки", 3100, 0.14, "discount"),
    ('Футболка «Эскиз»', "Футболки", 2800, 0.09, ""),
    ('Футболка «Грифель»', "Футболки", 3200, 0.06, ""),
    ('Футболка «Оттиск»', "Футболки", 2600, 0.0, "dead"),
    # Рубашки
    ('Рубашка «Оверсайз чёрная»', "Рубашки", 8400, 0.85, ""),
    ('Рубашка «Молочная»', "Рубашки", 7900, 0.42, ""),
    ('Рубашка «Клетка серая»', "Рубашки", 8900, 0.26, ""),
    ('Рубашка «Деним»', "Рубашки", 9400, 0.32, "sellout"),
    ('Рубашка «Фланель»', "Рубашки", 7200, 0.11, ""),
    ('Рубашка «Лён»', "Рубашки", 6900, 0.07, "discount"),
    # Брюки — 1 бестселлер
    ('Брюки «Карго широкие»', "Брюки", 9600, 1.10, ""),
    ('Брюки «Прямые чёрные»', "Брюки", 8800, 0.55, ""),
    ('Джоггеры «База»', "Брюки", 6900, 0.38, ""),
    ('Брюки «Шерсть серые»', "Брюки", 11800, 0.20, ""),
    ('Шорты «Лето»', "Брюки", 4900, 0.24, ""),
    ('Брюки «Вельвет»', "Брюки", 9200, 0.10, ""),
    ('Джинсы «Свободные»', "Брюки", 10400, 0.06, "discount"),
    # Верхняя одежда (зимний пик) — 1 бестселлер по деньгам
    ('Пуховик «Норд»', "Верхняя одежда", 32000, 0.42, ""),
    ('Куртка «Бомбер чёрный»', "Верхняя одежда", 18500, 0.35, ""),
    ('Пальто «Кокон»', "Верхняя одежда", 28000, 0.16, ""),
    ('Куртка «Ветровка»', "Верхняя одежда", 12500, 0.28, "sellout"),
    ('Тренч «Классика»', "Верхняя одежда", 24500, 0.07, ""),
    # Платья (летний пик)
    ('Платье «Комбинация»', "Платья", 11500, 0.55, ""),
    ('Платье «Рубашка»', "Платья", 10900, 0.32, ""),
    ('Платье «Макси чёрное»', "Платья", 13400, 0.20, ""),
    ('Сарафан «Полоса»', "Платья", 8900, 0.16, ""),
    ('Платье «Мини»', "Платья", 9800, 0.09, "discount"),
    ('Платье «Футляр»', "Платья", 12800, 0.0, "dead"),
    # Сумки (One Size)
    ('Сумка «Шопер холст»', "Сумки", 4900, 0.60, ""),
    ('Сумка «Кросс-боди»', "Сумки", 8900, 0.30, ""),
    ('Рюкзак «Сити»', "Сумки", 11900, 0.15, ""),
    ('Сумка «Тоут кожа»', "Сумки", 16900, 0.08, ""),
    # Украшения (One Size) — 1 бестселлер по штукам
    ("Серьга 12 мм", "Украшения", 2900, 2.20, ""),
    ('Кольцо «Печатка»', "Украшения", 4400, 0.80, ""),
    ('Цепь «Панцирная»', "Украшения", 5900, 0.45, ""),
    ("Серьга-каффа", "Украшения", 3400, 0.25, ""),
    ('Браслет «Звенья»', "Украшения", 4900, 0.12, ""),
    # Аксессуары
    ('Шапка «Бини»', "Аксессуары", 2900, 0.70, ""),
    ('Шарф «Мохер»', "Аксессуары", 4500, 0.30, ""),
    ('Ремень «Классика»', "Аксессуары", 3900, 0.16, ""),
    ('Носки «Лого» (3 пары)', "Аксессуары", 2000, 0.55, ""),
]

WINTER_CATS = {"Худи и свитшоты", "Верхняя одежда", "Аксессуары"}
SUMMER_CATS = {"Футболки", "Платья"}
ONE_SIZE_CATS = {"Сумки", "Украшения", "Аксессуары"}


def _season_mult(d: date, category: str) -> float:
    """Сезонный множитель спроса: зимой растут худи/куртки, летом — футболки/платья."""
    m = d.month
    winter = m in (11, 12, 1, 2)
    summer = m in (5, 6, 7, 8)
    if category in WINTER_CATS:
        return 1.7 if winter else (0.55 if summer else 1.0)
    if category in SUMMER_CATS:
        return 1.7 if summer else (0.55 if winter else 1.0)
    return 1.15 if winter or m in (6, 7) else 1.0


def _weekday_mult(d: date) -> float:
    """Недельный цикл: пик в пятницу-субботу, спад в начале недели."""
    return {0: 0.80, 1: 0.85, 2: 0.95, 3: 1.05, 4: 1.45, 5: 1.55, 6: 1.05}[d.weekday()]


def _poisson(rng: random.Random, lam: float) -> int:
    """Пуассоновская выборка (Кнут); lam у нас всегда мал (<3)."""
    if lam <= 0:
        return 0
    threshold = math.exp(-lam)
    k, p = 0, 1.0
    while True:
        p *= rng.random()
        if p <= threshold:
            return k
        k += 1


def _largest_remainder(total: int, weights: list[float]) -> list[int]:
    """Целочисленное распределение total по весам (метод наибольших остатков)."""
    if total <= 0 or not weights:
        return [0] * len(weights)
    wsum = sum(weights) or 1.0
    exact = [total * w / wsum for w in weights]
    alloc = [int(x) for x in exact]
    order = sorted(range(len(weights)), key=lambda i: exact[i] - alloc[i], reverse=True)
    for i in range(total - sum(alloc)):
        alloc[order[i % len(order)]] += 1
    return alloc


def clear_org_data(db: Session, org_id: int) -> None:
    """Удаляет все бизнес-данные организации (перед повторным сидированием)."""
    for model in (Sale, StockDay, WarehouseStock, OrderedQty, ProductionOrder, Product, Warehouse):
        db.execute(delete(model).where(model.org_id == org_id))


def seed_demo(db: Session, org: Org) -> dict:
    """Сеет демо-данные организации. Возвращает счётчики строк."""
    rng = random.Random(42)
    clear_org_data(db, org.id)

    today = date.today()
    dates = [today - timedelta(days=i) for i in range(HISTORY_DAYS - 1, -1, -1)]

    # Склады
    wh_ids = []
    for ext_id, name, _share in WAREHOUSES:
        wh = Warehouse(org_id=org.id, ext_id=ext_id, name=name, active=True)
        db.add(wh)
        db.flush()
        wh_ids.append(wh.id)

    stock_rows: list[dict] = []
    sales_rows: list[dict] = []
    whstock_rows: list[dict] = []
    n_products = 0

    for idx, (base_name, category, price, daily_rate, flags) in enumerate(CATALOG):
        cost = round(price * rng.uniform(0.36, 0.44))
        sizes = ["One Size"] if category in ONE_SIZE_CATS else APPAREL_SIZES
        # Распроданные позиции: производство остановлено ~за полгода до конца,
        # чтобы остаток гарантированно успел уйти в ноль (cs=0 при живых продажах).
        sellout = "sellout" in flags
        restock_until = HISTORY_DAYS - rng.randint(160, 200) if sellout else HISTORY_DAYS
        base_discount = 0.32 if "discount" in flags else rng.uniform(0.02, 0.10)

        for size in sizes:
            share = SIZE_WEIGHTS[size] if size in SIZE_WEIGHTS else 1.0
            rate_sz = daily_rate * share * rng.uniform(0.85, 1.15)
            product = Product(
                org_id=org.id,
                ext_id=f"demo-{idx:03d}-{size}",
                base_name=base_name,
                size=size,
                category=category,
                sale_price=price,
                cost_price=cost,
                archived=False,
            )
            db.add(product)
            db.flush()
            n_products += 1

            # Стартовый запас ~ на 6-8 недель продаж; у мёртвых — фикс. остаток.
            stock = max(4, round(rate_sz * rng.uniform(42, 56))) if daily_rate > 0 else rng.randint(6, 14)
            # У снятых с производства — маленькие финальные партии, чтобы хвост распродался.
            batch = max(2 if sellout else 6, round(rate_sz * (40 if sellout else 60)))
            reorder_point = max(1 if sellout else 2, round(rate_sz * 12))

            for day_i, d in enumerate(dates):
                lam = rate_sz * _season_mult(d, category) * _weekday_mult(d)
                sold = min(stock, _poisson(rng, lam))
                if sold > 0:
                    discount = min(0.6, max(0.0, base_discount + rng.uniform(-0.03, 0.12)))
                    sales_rows.append(
                        {
                            "org_id": org.id,
                            "product_id": product.id,
                            "date": d.isoformat(),
                            "qty": sold,
                            "revenue": round(sold * price * (1 - discount)),
                            "is_return": False,
                        }
                    )
                    # ~5% продаж возвращается в течение недели
                    if rng.random() < 0.05 * sold:
                        rd = d + timedelta(days=rng.randint(1, 7))
                        if rd <= today:
                            sales_rows.append(
                                {
                                    "org_id": org.id,
                                    "product_id": product.id,
                                    "date": rd.isoformat(),
                                    "qty": 1,
                                    "revenue": round(price * (1 - discount)),
                                    "is_return": True,
                                }
                            )
                    stock -= sold
                # Пополнение с производства (пока позиция не снята)
                if day_i < restock_until and stock <= reorder_point and daily_rate > 0:
                    stock += batch
                stock_rows.append(
                    {
                        "org_id": org.id,
                        "product_id": product.id,
                        "date": d.isoformat(),
                        "qty": stock,
                    }
                )

            # Текущий остаток по складам = финальный сток, распределённый по долям.
            shares = [w[2] * rng.uniform(0.7, 1.3) for w in WAREHOUSES]
            for wh_id, q in zip(wh_ids, _largest_remainder(stock, shares)):
                whstock_rows.append(
                    {
                        "org_id": org.id,
                        "product_id": product.id,
                        "warehouse_id": wh_id,
                        "qty": q,
                    }
                )

    for model, rows in ((StockDay, stock_rows), (Sale, sales_rows), (WarehouseStock, whstock_rows)):
        for i in range(0, len(rows), 10000):
            db.execute(insert(model), rows[i : i + 10000])

    return {
        "products": n_products,
        "stock_days": len(stock_rows),
        "sales": len(sales_rows),
        "warehouse_stock": len(whstock_rows),
    }
