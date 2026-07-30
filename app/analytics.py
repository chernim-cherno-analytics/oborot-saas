"""Аналитика оборачиваемости: портировано из legacy (build_turnover_data, order.html).

Все метрики считаются по базовому имени (base_name) за скользящие 365 дней:

- dis  — «дней в стоке»: даты в stock_days, где суммарный по размерам qty >= min_stock_days;
- cs   — остаток на ПОСЛЕДНЮЮ имеющуюся дату (нет строки на неё = 0);
- nq/nr — нетто продано шт / нетто выручка (продажи минус возвраты);
- rate = nq/dis, turnover = nr/dis (главная метрика, ₽/день);
- wos = cs/(rate*7); stockout_date = today + cs/rate; need = rate*horizon − cs − ordered.

Агрегация — SQL (GROUP BY), в Python попадают только свёрнутые строки.
Снапшот кэшируется в памяти на 10 минут per-org; запись (заказы, настройки,
переподключение) инвалидирует кэш через invalidate().
"""
import threading
import time
from datetime import date, timedelta

from sqlalchemy import and_, case, func, select
from sqlalchemy.orm import Session

from app.models import (
    Org,
    OrderedQty,
    Product,
    Sale,
    StockDay,
    Warehouse,
    WarehouseStock,
)

CACHE_TTL = 600  # 10 минут
NO_SALES_ALERT_DAYS = 120  # неликвид: столько дней без продаж при наличии стока
STOCKOUT_ALERT_DAYS = 21  # алерт: бестселлер/хороший закончится в ближайшие N дней
OVERSTOCK_WEEKS = 26  # алерт: запаса больше, чем на полгода

_cache: dict[int, tuple[float, dict]] = {}
_cache_lock = threading.Lock()


def invalidate(org_id: int) -> None:
    """Сбрасывает кэш аналитики организации (вызывать при любой записи данных)."""
    with _cache_lock:
        _cache.pop(org_id, None)


def get_snapshot(db: Session, org: Org) -> dict:
    """Возвращает аналитический снапшот организации (из кэша или пересчётом)."""
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(org.id)
        if hit and hit[0] > now:
            return hit[1]
    snap = _compute_snapshot(db, org)
    with _cache_lock:
        _cache[org.id] = (time.monotonic() + CACHE_TTL, snap)
    return snap


# ── Расчёт снапшота ───────────────────────────────────────────────────────────

def _compute_snapshot(db: Session, org: Org) -> dict:
    settings = org.settings
    min_stock = settings["min_stock_days"]
    horizon = settings["horizon_days"]
    thresholds = settings["thresholds"]
    today = date.today()
    cutoff365 = (today - timedelta(days=365)).isoformat()
    cutoff30 = (today - timedelta(days=30)).isoformat()

    latest_date = db.scalar(select(func.max(StockDay.date)).where(StockDay.org_id == org.id))

    join_products = and_(Product.id == StockDay.product_id, Product.org_id == org.id)

    # Мета позиций: категория, цены, «архивность» базы (все размеры в архиве).
    meta_rows = db.execute(
        select(
            Product.base_name,
            func.max(Product.category),
            func.max(Product.sale_price),
            func.max(Product.cost_price),
            func.min(case((Product.archived, 1), else_=0)),
        )
        .where(Product.org_id == org.id)
        .group_by(Product.base_name)
    ).all()

    # dis: даты, где суммарный остаток базы >= порога.
    day_totals = (
        select(Product.base_name.label("base"), StockDay.date.label("d"))
        .select_from(StockDay)
        .join(Product, join_products)
        .where(StockDay.org_id == org.id, StockDay.date >= cutoff365)
        .group_by(Product.base_name, StockDay.date)
        .having(func.sum(StockDay.qty) >= min_stock)
        .subquery()
    )
    dis_by_base = dict(
        db.execute(select(day_totals.c.base, func.count()).group_by(day_totals.c.base)).all()
    )

    # cs: остаток на последнюю дату, по размерам.
    cs_rows: list = []
    if latest_date:
        cs_rows = db.execute(
            select(Product.base_name, Product.size, func.sum(StockDay.qty))
            .select_from(StockDay)
            .join(Product, join_products)
            .where(StockDay.org_id == org.id, StockDay.date == latest_date)
            .group_by(Product.base_name, Product.size)
        ).all()

    # Нетто-продажи за 365 дней, по размерам.
    sign_qty = case((Sale.is_return, -Sale.qty), else_=Sale.qty)
    sign_rev = case((Sale.is_return, -Sale.revenue), else_=Sale.revenue)
    join_sales = and_(Product.id == Sale.product_id, Product.org_id == org.id)
    sales_rows = db.execute(
        select(Product.base_name, Product.size, func.sum(sign_qty), func.sum(sign_rev))
        .select_from(Sale)
        .join(Product, join_sales)
        .where(Sale.org_id == org.id, Sale.date >= cutoff365)
        .group_by(Product.base_name, Product.size)
    ).all()

    # Последняя продажа (для алертов о неликвиде).
    last_sale_by_base = dict(
        db.execute(
            select(Product.base_name, func.max(Sale.date))
            .select_from(Sale)
            .join(Product, join_sales)
            .where(Sale.org_id == org.id, Sale.is_return.is_(False))
            .group_by(Product.base_name)
        ).all()
    )

    # Продажи за 30 дней (сводка дашборда).
    sold30_qty, sold30_rev = db.execute(
        select(func.coalesce(func.sum(sign_qty), 0), func.coalesce(func.sum(sign_rev), 0)).where(
            Sale.org_id == org.id, Sale.date >= cutoff30
        )
    ).one()

    ordered_by_base = dict(
        db.execute(
            select(OrderedQty.base_name, OrderedQty.qty).where(
                OrderedQty.org_id == org.id, OrderedQty.qty > 0
            )
        ).all()
    )

    # Склады и текущие остатки по ним (только активные — для страницы «Остатки»).
    warehouses = db.execute(
        select(Warehouse).where(Warehouse.org_id == org.id).order_by(Warehouse.id)
    ).scalars().all()
    active_wh_ids = [w.id for w in warehouses if w.active]
    wh_rows: list = []
    if active_wh_ids:
        wh_rows = db.execute(
            select(
                Product.base_name,
                Product.size,
                WarehouseStock.warehouse_id,
                func.sum(WarehouseStock.qty),
            )
            .select_from(WarehouseStock)
            .join(Product, and_(Product.id == WarehouseStock.product_id, Product.org_id == org.id))
            .where(
                WarehouseStock.org_id == org.id,
                WarehouseStock.warehouse_id.in_(active_wh_ids),
            )
            .group_by(Product.base_name, Product.size, WarehouseStock.warehouse_id)
        ).all()

    # ── Сборка по базовым именам ─────────────────────────────────────────────
    items: dict[str, dict] = {}
    for base, category, sale_price, cost_price, archived in meta_rows:
        items[base] = {
            "base_name": base,
            "category": category or "",
            "sale_price": float(sale_price or 0),
            "cost_price": float(cost_price or 0),
            "archived": bool(archived),
            "dis": int(dis_by_base.get(base, 0)),
            "cs": 0,
            "nq": 0.0,
            "nr": 0.0,
            "ordered": float(ordered_by_base.get(base, 0)),
            "last_sale": last_sale_by_base.get(base),
            "sizes": {},  # size -> {stock, sold365}
            "wh_stock": {},  # size -> {warehouse_id: qty}
        }

    def _size_rec(item: dict, size: str) -> dict:
        return item["sizes"].setdefault(size, {"stock": 0, "sold365": 0})

    for base, size, qty in cs_rows:
        item = items.get(base)
        if item is None:
            continue
        q = int(round(qty or 0))
        item["cs"] += q
        _size_rec(item, size)["stock"] = q

    for base, size, nq, nr in sales_rows:
        item = items.get(base)
        if item is None:
            continue
        item["nq"] += float(nq or 0)
        item["nr"] += float(nr or 0)
        _size_rec(item, size)["sold365"] = float(nq or 0)

    for base, size, wh_id, qty in wh_rows:
        item = items.get(base)
        if item is None:
            continue
        item["wh_stock"].setdefault(size, {})[wh_id] = int(round(qty or 0))

    # ── Производные метрики ──────────────────────────────────────────────────
    for item in items.values():
        dis, cs, nq, nr = item["dis"], item["cs"], item["nq"], item["nr"]
        rate = nq / dis if dis > 0 else 0.0
        turnover = nr / dis if dis > 0 else 0.0
        item["rate"] = round(rate, 4)
        item["turnover"] = round(turnover)
        item["cls"] = classify(turnover, thresholds)
        item["wos"] = round(cs / (rate * 7), 1) if rate > 0 else None
        item["stockout_date"] = (
            (today + timedelta(days=int(cs / rate))).isoformat() if rate > 0 else None
        )
        item["avg_price"] = round(nr / nq) if nq > 0 else None
        sale_price = item["sale_price"]
        item["discount_fact"] = (
            round(1 - (nr / nq) / sale_price, 3) if nq > 0 and sale_price > 0 else None
        )
        item["need"] = max(0, round(rate * horizon) - cs - int(item["ordered"]))
        item["nq"] = round(nq)
        item["nr"] = round(nr)

    return {
        "generated_at": time.time(),
        "today": today.isoformat(),
        "latest_date": latest_date,
        "settings": settings,
        "items": items,
        "warehouses": [{"id": w.id, "name": w.name, "active": w.active} for w in warehouses],
        "sold_30d_qty": round(float(sold30_qty or 0)),
        "sold_30d_rev": round(float(sold30_rev or 0)),
    }


def classify(turnover: float, thresholds: dict) -> str:
    """Класс оборачиваемости по порогам: weak | dull | good | best."""
    if turnover < thresholds["weak"]:
        return "weak"
    if turnover < thresholds["dull"]:
        return "dull"
    if turnover < thresholds["good"]:
        return "good"
    return "best"


def size_split(sizes: dict[str, dict], total: int) -> dict[str, int]:
    """Разбивка заказа по размерам пропорционально нетто-продажам (largest remainder).

    Сетка = union(размеры стока, размеры продаж) — распроданные размеры не выпадают.
    Если продаж по размерам нет вовсе, делим поровну.
    """
    grid = list(sizes.keys())
    if not grid or total <= 0:
        return {}
    weights = [max(0.0, float(sizes[s].get("sold365") or 0)) for s in grid]
    if sum(weights) <= 0:
        weights = [1.0] * len(grid)
    wsum = sum(weights)
    exact = [total * w / wsum for w in weights]
    alloc = [int(x) for x in exact]
    remainders = sorted(
        range(len(grid)), key=lambda i: (exact[i] - alloc[i], weights[i]), reverse=True
    )
    left = total - sum(alloc)
    for i in range(left):
        alloc[remainders[i % len(remainders)]] += 1
    return {s: a for s, a in zip(grid, alloc)}


# ── Построители ответов API ───────────────────────────────────────────────────

def _live_items(snap: dict) -> list[dict]:
    """Неархивные позиции, у которых есть хоть какая-то активность."""
    return [
        it
        for it in snap["items"].values()
        if not it["archived"] and (it["cs"] > 0 or it["nq"] > 0 or it["dis"] > 0)
    ]


def build_summary(snap: dict) -> dict:
    """GET /api/summary — карточки дашборда, алерты, топ, классы, категории."""
    items = _live_items(snap)
    today = date.fromisoformat(snap["today"])

    stock_value_retail = sum(it["cs"] * it["sale_price"] for it in items)
    stock_value_cost = sum(it["cs"] * it["cost_price"] for it in items)
    stock_units = sum(it["cs"] for it in items)

    classes = {"weak": 0, "dull": 0, "good": 0, "best": 0}
    for it in items:
        classes[it["cls"]] += 1

    alerts = []
    for it in sorted(items, key=lambda x: -x["turnover"]):
        base = it["base_name"]
        if it["cls"] in ("best", "good") and it["stockout_date"] and it["cs"] >= 0:
            days_left = (date.fromisoformat(it["stockout_date"]) - today).days
            if it["cs"] == 0 and it["rate"] > 0:
                alerts.append(
                    {
                        "type": "stockout",
                        "base_name": base,
                        "text": f"{base} распродан в ноль, а продажи шли — упускаем выручку",
                        "severity": "red",
                    }
                )
            elif days_left <= STOCKOUT_ALERT_DAYS:
                alerts.append(
                    {
                        "type": "stockout",
                        "base_name": base,
                        "text": f"{base} закончится примерно {it['stockout_date']} — пора заказывать",
                        "severity": "red",
                    }
                )
        if it["cs"] > 0:
            last_sale = it["last_sale"]
            no_sales_days = (
                (today - date.fromisoformat(last_sale)).days if last_sale else None
            )
            if last_sale is None or no_sales_days > NO_SALES_ALERT_DAYS:
                since = f"{no_sales_days} дн." if no_sales_days is not None else "за всю историю"
                alerts.append(
                    {
                        "type": "no_sales",
                        "base_name": base,
                        "text": f"{base}: {it['cs']} шт на складе, продаж нет ({since}) — неликвид",
                        "severity": "yellow",
                    }
                )
            elif it["wos"] is not None and it["wos"] > OVERSTOCK_WEEKS:
                alerts.append(
                    {
                        "type": "overstock",
                        "base_name": base,
                        "text": f"{base}: запаса на {it['wos']:.0f} недель — затоварка",
                        "severity": "yellow",
                    }
                )
    alerts.sort(key=lambda a: (a["severity"] != "red"))

    top = [
        {"base_name": it["base_name"], "turnover": it["turnover"]}
        for it in sorted(items, key=lambda x: -x["turnover"])[:5]
    ]

    cat_agg: dict[str, dict] = {}
    for it in items:
        cat = cat_agg.setdefault(it["category"] or "Без категории", {"stock_units": 0, "stock_value": 0})
        cat["stock_units"] += it["cs"]
        cat["stock_value"] += it["cs"] * it["sale_price"]
    total_value = sum(c["stock_value"] for c in cat_agg.values()) or 1
    categories = sorted(
        (
            {
                "name": name,
                "stock_units": c["stock_units"],
                "stock_value": round(c["stock_value"]),
                "share": round(c["stock_value"] / total_value, 3),
            }
            for name, c in cat_agg.items()
        ),
        key=lambda c: -c["stock_value"],
    )

    return {
        "stock_value_retail": round(stock_value_retail),
        "stock_value_cost": round(stock_value_cost),
        "stock_units": stock_units,
        "positions": len(items),
        "turnover_total": round(sum(it["turnover"] for it in items)),
        "sold_30d_qty": snap["sold_30d_qty"],
        "sold_30d_rev": snap["sold_30d_rev"],
        "alerts": alerts[:20],
        "classes": classes,
        "top": top,
        "categories": categories,
    }


def build_replenish(snap: dict) -> dict:
    """GET /api/replenish — потребность в заказе, сортировка по turnover desc."""
    horizon = snap["settings"]["horizon_days"]
    result, excluded = [], []
    for it in sorted(snap["items"].values(), key=lambda x: -x["turnover"]):
        base = it["base_name"]
        if it["archived"]:
            excluded.append({"base_name": base, "reason": "архивная позиция"})
            continue
        if it["cs"] == 0 and it["nq"] <= 0 and it["dis"] == 0:
            continue  # мусорная запись без активности
        if it["need"] <= 0:
            if it["rate"] <= 0:
                reason = "нет продаж за 365 дней"
            elif it["ordered"] > 0:
                reason = "потребность закрыта заказом в производстве"
            else:
                reason = "запаса достаточно"
            excluded.append({"base_name": base, "reason": reason})
            continue
        rec = size_split(it["sizes"], it["need"])
        avg_price = it["avg_price"] or it["sale_price"]
        result.append(
            {
                "base_name": base,
                "category": it["category"],
                "cls": it["cls"],
                "turnover": it["turnover"],
                "rate": it["rate"],
                "cs": it["cs"],
                "ordered": int(it["ordered"]),
                "wos": it["wos"],
                "stockout_date": it["stockout_date"],
                "need": it["need"],
                "sizes": {
                    s: {
                        "stock": v["stock"],
                        "sold365": round(v["sold365"]),
                        "rec": rec.get(s, 0),
                    }
                    for s, v in sorted(it["sizes"].items(), key=lambda kv: _size_order(kv[0]))
                },
                "avg_price": avg_price,
                "cost_price": it["cost_price"],
                "profit_potential": round(max(0, avg_price - it["cost_price"]) * it["need"]),
            }
        )
    return {"horizon_days": horizon, "items": result, "excluded": excluded}


def build_turnover(snap: dict) -> dict:
    """GET /api/turnover — все позиции, сортировка по turnover desc."""
    items = []
    for it in sorted(snap["items"].values(), key=lambda x: -x["turnover"]):
        if it["cs"] == 0 and it["nq"] <= 0 and it["dis"] == 0 and not it["archived"]:
            continue
        items.append(
            {
                "base_name": it["base_name"],
                "category": it["category"],
                "dis": it["dis"],
                "cs": it["cs"],
                "nq": it["nq"],
                "nr": it["nr"],
                "turnover": it["turnover"],
                "cls": it["cls"],
                "avg_price": it["avg_price"],
                "sale_price": it["sale_price"],
                "discount_fact": it["discount_fact"],
                "wos": it["wos"],
                "stockout_date": it["stockout_date"],
                "archived": it["archived"],
            }
        )
    return {"items": items}


def build_stocks(snap: dict) -> dict:
    """GET /api/stocks — остатки по активным складам с разбивкой по размерам."""
    active = [w for w in snap["warehouses"] if w["active"]]
    wh_ids = [w["id"] for w in active]
    items = []
    for it in sorted(snap["items"].values(), key=lambda x: -x["cs"]):
        if it["archived"] or (it["cs"] == 0 and not it["wh_stock"]):
            continue
        sizes = []
        totals = [0] * len(wh_ids)
        for size in sorted(it["sizes"].keys() | it["wh_stock"].keys(), key=_size_order):
            per_wh = [int(it["wh_stock"].get(size, {}).get(wid, 0)) for wid in wh_ids]
            for i, q in enumerate(per_wh):
                totals[i] += q
            sizes.append({"size": size, "per_wh": per_wh, "total": sum(per_wh)})
        items.append(
            {
                "base_name": it["base_name"],
                "category": it["category"],
                "per_wh": totals,
                "total": sum(totals),
                "sizes": sizes,
            }
        )
    return {
        "warehouses": [{"id": w["id"], "name": w["name"]} for w in active],
        "items": items,
    }


_SIZE_ORDER = {"XS": 0, "S": 1, "M": 2, "L": 3, "XL": 4, "XXL": 5, "One Size": 90}


def _size_order(size: str) -> tuple:
    """Ключ сортировки размеров: XS…XXL, затем прочие по алфавиту."""
    return (_SIZE_ORDER.get(size, 50), size)
