"""Портированная аналитика legacy: Бюджет закупки (OTB), Прогноз, Размеры.

Вся математика — сервером, из БД и снапшота app.analytics; страницы получают
готовый JSON. Бизнес-правила перенесены из legacy/budget.html, forecast.html,
sizes.html (проверены на реальном бренде):

Бюджет:
- ранжирование СТРОГО по оборачиваемости (nr/dis, ₽/день) — НЕ по ROI
  (ROI-версия забракована: наверх вылезали дешёвые аксессуары);
- свежесть: только позиции с нетто-продажами за последние FRESH_DAYS
  (в SaaS продажи в БД — проверяем по датам напрямую, без двухисточникового
  костыля legacy);
- потребность = ceil(rate × horizon − остаток − «едет»), порог ≥ NEED_MIN шт;
- жадное наполнение, лимит max_share% бюджета на позицию; позиция дороже
  лимита за 1 шт не выпадает — входит 1 шт с пометкой over_limit;
- позиции без себестоимости не считаются по нулю — отдельный список no_cost.

Прогноз:
- каждая позиция распродаётся своим темпом до нуля, ряд на 26 недель;
- «стока хватит до» — порог 90% от ПРОДАВАЕМОГО потенциала (rate>0):
  мёртвый сток не должен делать порог недостижимым.

Размеры:
- период «N мес» = N ПОЛНЫХ месяцев + текущий неполный; темп делится на
  ДРОБНОЕ число месяцев (текущий месяц — долей прошедших дней);
- сезоны — календарные месяцы сезона за все годы истории;
- сетка = union(вся история продаж, текущий сток); распределение — largest
  remainder; режимы «с учётом остатков» и «чистая пропорция» (обе колонки).
"""
import math
from datetime import date, timedelta

from sqlalchemy import and_, case, func, select
from sqlalchemy.orm import Session

from app.categories import ru_category
from app.models import Product, Sale, StockDay

FRESH_DAYS = 90        # бюджет: окно «свежих» продаж
NEED_MIN = 3           # бюджет: минимальная потребность, шт
FORECAST_WEEKS = 26    # прогноз: горизонт ряда
FORECAST_MONTHS = 7    # прогноз: текущий + 6 будущих месяцев (мини-кольца «Пульса» показывают будущие)
SELLOUT_SHARE = 0.9    # прогноз: «хватит до» = распродано 90% продаваемого потенциала
CAT_LOW_DAYS = 45      # прогноз: пилюля «мало»
CAT_OVER_DAYS = 120    # прогноз: пилюля «затоварка»

_sign_qty = case((Sale.is_return, -Sale.qty), else_=Sale.qty)


def _largest_remainder(weights: list[float], total: int) -> list[int]:
    """Веса → целые с суммой ровно total (метод наибольших остатков)."""
    n = len(weights)
    if n == 0 or total <= 0:
        return [0] * n
    wsum = sum(max(0.0, w) for w in weights)
    if wsum <= 0:
        return [0] * n
    raw = [max(0.0, w) / wsum * total for w in weights]
    alloc = [int(math.floor(x)) for x in raw]
    order = sorted(range(n), key=lambda i: raw[i] - alloc[i], reverse=True)
    for k in range(total - sum(alloc)):
        alloc[order[k % n]] += 1
    return alloc


def _item_price(it: dict) -> float:
    """Цена продажи позиции: средняя фактическая, при её отсутствии — номинал."""
    return float(it.get("avg_price") or it.get("sale_price") or 0)


# ── Бюджет закупки (OTB) ─────────────────────────────────────────────────────

def _fresh_bases(db: Session, org_id: int, days: int = FRESH_DAYS) -> set[str]:
    """Базовые имена с нетто-продажами > 0 за последние `days` дней."""
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    rows = db.execute(
        select(Product.base_name, func.sum(_sign_qty))
        .select_from(Sale)
        .join(Product, and_(Product.id == Sale.product_id, Product.org_id == org_id, Product.excluded.is_(False)))
        .where(Sale.org_id == org_id, Sale.date >= cutoff)
        .group_by(Product.base_name)
    ).all()
    return {base for base, q in rows if (q or 0) > 0}


def build_budget(
    db: Session,
    org_id: int,
    snap: dict,
    amount: int,
    max_share: int,
    exclude_cats: set[str],
) -> dict:
    """GET /api/budget — распределение бюджета закупки по оборачиваемости."""
    horizon = snap["settings"]["horizon_days"]
    max_share = max(5, min(100, int(max_share)))
    fresh = _fresh_bases(db, org_id)

    candidates, no_cost = [], []
    excluded_fresh = 0
    all_categories: set[str] = set()

    for it in snap["items"].values():
        if it["archived"] or it.get("hidden"):
            continue
        dis, nq = it["dis"], it["nq"]
        if dis <= 0 or nq <= 0:
            continue  # нет продаж вообще
        rate = nq / dis
        stock_eff = it["cs"] + max(0, int(it["ordered"]))
        need = math.ceil(max(0.0, rate * horizon - stock_eff))
        if need < NEED_MIN:
            continue  # потребность мизерная
        category = it["category"] or "Без категории"
        all_categories.add(category)
        if category in exclude_cats:
            continue
        if it["base_name"] not in fresh:
            excluded_fresh += 1  # нет продаж за FRESH_DAYS — неактуально
            continue
        cost = float(it["cost_price"] or 0)
        entry = {
            "base_name": it["base_name"],
            "category": category,
            "cls": it["cls"],
            "turnover": it["turnover"],
            "rate": round(rate, 4),
            "stock_eff": stock_eff,
            "ordered": int(it["ordered"]),
            "days_left": round(stock_eff / rate) if rate > 0 else None,
            "need": need,
            "cost_price": round(cost),
        }
        if cost <= 0:
            # НЕ считаем по нулю — отдельным списком
            no_cost.append(entry)
            continue
        price = _item_price(it)
        entry["margin"] = price - cost
        candidates.append(entry)

    # Приоритет — строго оборачиваемость (₽/день), как на /turnover.
    candidates.sort(key=lambda x: -x["turnover"])
    no_cost.sort(key=lambda x: -x["turnover"])

    cap_sum = amount * max_share / 100.0
    rest = float(amount)
    items, not_included = [], []
    for c in candidates:
        cost = c["cost_price"]
        by_cap = int(cap_sum // cost)
        by_rest = int(rest // cost)
        qty = min(c["need"], by_rest, by_cap)
        over_limit = False
        if qty < 1 and by_cap < 1 and by_rest >= 1:
            # 1 штука дороже лимита на позицию — не выкидываем, берём минимум 1
            qty = 1
            over_limit = True
        if qty < 1:
            not_included.append(
                {
                    "base_name": c["base_name"],
                    "category": c["category"],
                    "cls": c["cls"],
                    "turnover": c["turnover"],
                    "need": c["need"],
                    "cost_price": cost,
                    "need_rub": c["need"] * cost,
                    "days_left": c["days_left"],
                }
            )
            continue
        total = qty * cost
        rest -= total
        capped = qty < c["need"]
        cap_reason = None
        if capped:
            cap_reason = "share" if (qty == by_cap and by_cap <= by_rest) else "budget"
        items.append(
            {
                "base_name": c["base_name"],
                "category": c["category"],
                "cls": c["cls"],
                "turnover": c["turnover"],
                "rate": c["rate"],
                "stock_eff": c["stock_eff"],
                "ordered": c["ordered"],
                "days_left": c["days_left"],
                "need": c["need"],
                "qty": qty,
                "cost_price": cost,
                "total": round(total),
                "capped": capped,
                "cap_reason": cap_reason,
                "over_limit": over_limit,
                "expected_profit": round(c["margin"] * qty),
            }
        )

    used = amount - rest
    return {
        "amount": amount,
        "max_share": max_share,
        "horizon_days": horizon,
        "fresh_days": FRESH_DAYS,
        "used": round(used),
        "rest": round(rest),
        "items": items,
        "not_included": not_included,
        "no_cost": [
            {k: v for k, v in x.items() if k != "margin"} for x in no_cost
        ],
        "excluded_fresh_count": excluded_fresh,
        "categories": sorted(all_categories),
        "totals": {
            "positions": len(items),
            "units": sum(i["qty"] for i in items),
            "expected_profit": sum(i["expected_profit"] for i in items),
        },
    }


# ── Прогноз ──────────────────────────────────────────────────────────────────

def build_forecast(snap: dict) -> dict:
    """GET /api/forecast — распродажа стока по неделям, карточки, категории."""
    today = date.fromisoformat(snap["today"])

    items = []
    for it in snap["items"].values():
        if it["archived"] or it.get("hidden"):
            continue
        cs = it["cs"]
        inc = max(0, int(it["ordered"]))
        if cs + inc <= 0:
            continue
        rate = it["rate"]
        price = _item_price(it)
        q0 = cs + inc
        sellout_days = round(q0 / rate) if rate > 0 else None
        items.append(
            {
                "base_name": it["base_name"],
                "category": it["category"] or "Без категории",
                "cls": it["cls"],
                "cs": cs,
                "ordered": inc,
                "rate": rate,
                "price": price,
                "pace_rub": round(rate * price),
                "sellout_days": sellout_days,
                "sellout_date": (
                    (today + timedelta(days=sellout_days)).isoformat()
                    if sellout_days is not None
                    else None
                ),
            }
        )

    stock_units = sum(x["cs"] for x in items)
    stock_value = sum(x["cs"] * x["price"] for x in items)
    incoming_units = sum(x["ordered"] for x in items)
    incoming_value = sum(x["ordered"] * x["price"] for x in items)
    # темп — только по позициям, которые реально лежат на складе
    pace_rub = sum(x["rate"] * x["price"] for x in items if x["cs"] > 0)

    # Ряд на 26 недель: остаток каждой позиции тает своим темпом до нуля.
    weeks = []
    start_value = sum((x["cs"] + x["ordered"]) * x["price"] for x in items)
    for w in range(FORECAST_WEEKS + 1):
        val = 0.0
        for x in items:
            rem = (x["cs"] + x["ordered"]) - x["rate"] * 7 * w
            if rem > 0:
                val += rem * x["price"]
        weeks.append({"date": (today + timedelta(days=7 * w)).isoformat(), "stock_value": round(val)})

    # Помесячный ряд «сток vs продажи» (FORECAST_MONTHS месяцев вперёд):
    # для каждого месяца — стоимость стока на его начало и прогнозная выручка
    # месяца. Продажи затухают сами собой: бестселлеры распродаются первыми,
    # и без перезаказа каждый следующий месяц продаёт хуже предыдущего.
    months_fc = []
    rem_m = [x["cs"] + x["ordered"] for x in items]
    my, mm_ = today.year, today.month
    for _m in range(FORECAST_MONTHS):
        stock_start = sum(rem_m[i] * x["price"] for i, x in enumerate(items))
        sales_val = 0.0
        for i, x in enumerate(items):
            if x["rate"] <= 0 or rem_m[i] <= 0:
                continue
            sold = min(rem_m[i], x["rate"] * 30.44)
            rem_m[i] -= sold
            sales_val += sold * x["price"]
        months_fc.append(
            {
                "month": f"{my:04d}-{mm_:02d}",
                "stock_value": round(stock_start),
                "sales_value": round(sales_val),
            }
        )
        mm_ += 1
        if mm_ == 13:
            my, mm_ = my + 1, 1

    # «Хватит до»: 90% ПРОДАВАЕМОГО потенциала (rate>0) — мёртвый сток
    # не должен делать порог недостижимым (правило legacy).
    sellable = sum((x["cs"] + x["ordered"]) * x["price"] for x in items if x["rate"] > 0)
    until_week, cum = None, 0.0
    rem_q = {i: x["cs"] + x["ordered"] for i, x in enumerate(items)}
    for w in range(1, FORECAST_WEEKS + 1):
        for i, x in enumerate(items):
            if x["rate"] <= 0 or rem_q[i] <= 0:
                continue
            sold = min(rem_q[i], x["rate"] * 7)
            rem_q[i] -= sold
            cum += sold * x["price"]
        if sellable > 0 and cum >= sellable * SELLOUT_SHARE:
            until_week = w
            break
    until_date = (today + timedelta(days=7 * until_week)).isoformat() if until_week else None

    # Категории
    cats: dict[str, dict] = {}
    for x in items:
        c = cats.setdefault(
            x["category"], {"stock_units": 0, "incoming": 0, "value": 0.0, "pace": 0.0}
        )
        c["stock_units"] += x["cs"]
        c["incoming"] += x["ordered"]
        c["value"] += (x["cs"] + x["ordered"]) * x["price"]
        if x["cs"] > 0:
            c["pace"] += x["rate"] * x["price"]
    categories = []
    for name, c in sorted(cats.items(), key=lambda kv: -kv[1]["value"]):
        days = round(c["value"] / c["pace"]) if c["pace"] > 0 else None
        if days is None:
            status = "none"       # нет продаж
        elif days < CAT_LOW_DAYS:
            status = "low"        # мало!
        elif days < CAT_OVER_DAYS:
            status = "ok"
        else:
            status = "over"       # затоварка
        categories.append(
            {
                "name": name,
                "stock_units": c["stock_units"],
                "incoming": c["incoming"],
                "value": round(c["value"]),
                "pace_rub": round(c["pace"]),
                "days_left": days,
                "status": status,
            }
        )

    items.sort(key=lambda x: -(x["cs"] + x["ordered"]) * x["price"])
    return {
        "today": snap["today"],
        "cards": {
            "stock_units": stock_units,
            "stock_value": round(stock_value),
            "incoming_units": incoming_units,
            "incoming_value": round(incoming_value),
            "pace_rub": round(pace_rub),
            "until_date": until_date,
            "until_weeks": until_week,
        },
        "weeks": weeks,
        "months": months_fc,
        "categories": categories,
        "items": [
            {k: v for k, v in x.items() if k != "price"} | {"rate": round(x["rate"], 3)}
            for x in items
        ],
    }


# ── Размеры ──────────────────────────────────────────────────────────────────

_SIZE_ORDER = ["XXS", "XS", "S", "S/M", "M", "M/L", "L", "XL", "XXL", "XXXL", "ONE SIZE"]

SEASONS = {
    "spring": {"name": "весна", "mm": ("03", "04", "05")},
    "summer": {"name": "лето", "mm": ("06", "07", "08")},
    "autumn": {"name": "осень", "mm": ("09", "10", "11")},
    "winter": {"name": "зима", "mm": ("12", "01", "02")},
}


def _norm_size(s: str) -> str:
    """'' / 'one size' / 'универсальный' → 'One Size'; прочее — как есть."""
    x = (s or "").strip().strip("()").strip()
    if not x or x.lower() in ("one size", "onesize", "default title") or x.lower().startswith("универсал"):
        return "One Size"
    return x


def _size_sort_key(s: str):
    """XXS…XXXL, One Size, затем числовые по числу, затем алфавит (как в legacy)."""
    u = s.upper()
    if u in _SIZE_ORDER:
        return (0, _SIZE_ORDER.index(u), 0.0, "")
    try:
        return (1, 0, float(s.replace(",", ".")), "")
    except ValueError:
        return (2, 0, 0.0, s)


def sizes_products(db: Session, org_id: int, snap: dict) -> dict:
    """GET /api/sizes/products — список позиций для поиска (продано за всю историю + сток)."""
    sold_rows = dict(
        db.execute(
            select(Product.base_name, func.sum(_sign_qty))
            .select_from(Sale)
            .join(Product, and_(Product.id == Sale.product_id, Product.org_id == org_id, Product.excluded.is_(False)))
            .where(Sale.org_id == org_id)
            .group_by(Product.base_name)
        ).all()
    )
    products = []
    for it in snap["items"].values():
        if it["archived"] or it.get("hidden"):
            continue
        sold = round(float(sold_rows.get(it["base_name"]) or 0))
        if sold <= 0 and it["cs"] <= 0:
            continue
        products.append(
            {
                "base_name": it["base_name"],
                "category": it["category"],
                "sold": sold,
                "in_stock": it["cs"],
            }
        )
    products.sort(key=lambda p: -p["sold"])
    return {"products": products}


def _months_window(all_months: list[str], period: str, today: date) -> tuple[list[str], str | None]:
    """Месяцы окна (от новых к старым) по коду периода. Возвращает (месяцы, ошибка)."""
    period = (period or "12m").lower()
    if period in SEASONS:
        mm = SEASONS[period]["mm"]
        return [m for m in all_months if m[5:7] in mm], None
    if period == "all":
        return list(all_months), None
    if period.endswith("m"):
        try:
            n = int(period[:-1])
        except ValueError:
            return [], "bad_period"
        if n < 1 or n > 36:
            return [], "bad_period"
        # N ПОЛНЫХ месяцев + текущий неполный (правило legacy: иначе окно
        # короче обещанного и темп занижен)
        return list(all_months[: n + 1]), None
    return [], "bad_period"


def _eff_months(months: list[str], today: date) -> float:
    """Дробное число месяцев окна: текущий неполный месяц — долей прошедших дней."""
    if not months:
        return 0.0
    cur = f"{today.year:04d}-{today.month:02d}"
    if cur not in months:
        return float(len(months))
    if today.month == 12:
        days_in_month = 31
    else:
        days_in_month = (date(today.year, today.month + 1, 1) - timedelta(days=1)).day
    frac = min(1.0, today.day / days_in_month)
    return (len(months) - 1) + frac


def _size_presence_days(
    db: Session, org_id: int, product: str, months: list[str]
) -> tuple[dict[str, int], int]:
    """Дни НАЛИЧИЯ каждого размера в окне месяцев (методика оборачиваемости).

    Считаем по stock_days (ежедневные снапшоты остатков per размер, нули пишутся
    явно): день засчитывается размеру, если его остаток >= 1. Возвращает
    ({размер: дней_в_наличии}, всего_дат_истории_в_окне, дней_с_любым_размером) —
    второй элемент нужен как знаменатель-фолбэк для размеров, продажи которых
    старше истории снапшотов; третий — «дни наличия позиции» для общего темпа.
    """
    if not months:
        return {}, 0, 0
    month_expr = func.substr(StockDay.date, 1, 7)
    rows = db.execute(
        select(Product.size, func.count())
        .select_from(StockDay)
        .join(Product, and_(Product.id == StockDay.product_id, Product.org_id == org_id))
        .where(
            StockDay.org_id == org_id,
            Product.base_name == product,
            StockDay.qty >= 1,
            month_expr.in_(months),
        )
        .group_by(Product.size)
    ).all()
    pres: dict[str, int] = {}
    for size, cnt in rows:
        k = _norm_size(size)
        pres[k] = pres.get(k, 0) + int(cnt or 0)
    window_days = db.scalar(
        select(func.count(func.distinct(StockDay.date)))
        .select_from(StockDay)
        .join(Product, and_(Product.id == StockDay.product_id, Product.org_id == org_id))
        .where(
            StockDay.org_id == org_id,
            Product.base_name == product,
            month_expr.in_(months),
        )
    ) or 0
    any_days = db.scalar(
        select(func.count(func.distinct(StockDay.date)))
        .select_from(StockDay)
        .join(Product, and_(Product.id == StockDay.product_id, Product.org_id == org_id))
        .where(
            StockDay.org_id == org_id,
            Product.base_name == product,
            StockDay.qty >= 1,
            month_expr.in_(months),
        )
    ) or 0
    return pres, int(window_days), int(any_days)


def build_sizes_calc(
    db: Session,
    org_id: int,
    snap: dict,
    product: str,
    qty: int,
    period: str,
    mode: str,
    arrival: str | None = None,
    lead_time_days: int = 45,
) -> dict:
    """GET /api/sizes/calc — распределение заказа по размерам.

    Методика 04.08.2026 («считаем как оборачиваемость»):
    - темп размера = нетто-продажи за окно / дни РЕАЛЬНОГО наличия размера
      (раньше делили на календарные месяцы — размер, распроданный в середине
      окна, систематически недооценивался);
    - доли заказа считаются от темпов, а не от голых продаж;
    - остатки прогнозируются на дату прихода заказа (arrival, иначе
      today + lead_time_days): режим «с учётом остатков» закрывает дыры
      относительно прогнозного остатка, а не сегодняшнего.
    """
    today = date.fromisoformat(snap["today"])
    item = snap["items"].get(product)

    # Помесячные нетто-продажи позиции по размерам — за всю историю.
    month_expr = func.substr(Sale.date, 1, 7)
    rows = db.execute(
        select(month_expr, Product.size, func.sum(_sign_qty))
        .select_from(Sale)
        .join(Product, and_(Product.id == Sale.product_id, Product.org_id == org_id, Product.excluded.is_(False)))
        .where(Sale.org_id == org_id, Product.base_name == product)
        .group_by(month_expr, Product.size)
    ).all()

    monthly: dict[str, dict[str, float]] = {}
    for month, size, q in rows:
        monthly.setdefault(month, {})
        sz = _norm_size(size)
        monthly[month][sz] = monthly[month].get(sz, 0.0) + float(q or 0)

    # Все месяцы истории организации (для окон «N мес» и eff_months) —
    # календарно, от текущего месяца до первого месяца продаж организации.
    first_month = db.scalar(
        select(func.min(func.substr(Sale.date, 1, 7))).where(Sale.org_id == org_id)
    )
    all_months: list[str] = []
    y, m = today.year, today.month
    stop = first_month or f"{y:04d}-{m:02d}"
    while True:
        key = f"{y:04d}-{m:02d}"
        all_months.append(key)
        if key <= stop or len(all_months) > 480:
            break
        m -= 1
        if m == 0:
            y, m = y - 1, 12

    months, err = _months_window(all_months, period, today)
    if err:
        return {"error": "Неизвестный период", "period": period}

    # Сетка размеров = union(ВСЯ история продаж, текущий сток) —
    # распроданные размеры не выпадают, при пустом сезоне видны нули.
    stock: dict[str, int] = {}
    for sz, rec in (item["sizes"] if item else {}).items():
        k = _norm_size(sz)
        stock[k] = stock.get(k, 0) + int(rec.get("stock") or 0)
    all_sales_sizes: set[str] = set()
    for sm in monthly.values():
        all_sales_sizes.update(sm.keys())
    sizes = sorted(set(stock) | all_sales_sizes, key=_size_sort_key)

    # Продажи за окно, нетто; отрицательные (возвраты > продаж) — в 0.
    sales: dict[str, float] = {}
    for mkey in months:
        for sz, q in monthly.get(mkey, {}).items():
            sales[sz] = sales.get(sz, 0.0) + q
    sales = {k: max(0.0, round(v * 10) / 10) for k, v in sales.items()}

    is_season = period in SEASONS
    season_name = SEASONS[period]["name"] if is_season else None
    tot_sales = sum(sales.get(s, 0.0) for s in sizes)
    tot_stock = sum(stock.get(s, 0) for s in sizes)
    warning = None

    if not sizes:
        return {
            "base_name": product,
            "qty": qty,
            "period": period,
            "mode": mode,
            "warning": "Нет данных по этой позиции",
            "sizes": [],
            "totals": {"sold_period": 0, "rate_per_month": 0, "rate_per_day": 0,
                       "days_present": 0, "stock": 0, "stock_at_arrival": 0, "order": qty},
            "months_in_period": len(months),
            "eff_months": 0,
        }

    # Дни наличия размеров в окне + прогноз остатков на дату прихода.
    pres, hist_window_days, pos_any_days = _size_presence_days(db, org_id, product, months)
    try:
        arrival_date = date.fromisoformat(arrival) if arrival else None
    except ValueError:
        arrival_date = None
    if arrival_date is None:
        arrival_date = today + timedelta(days=max(0, int(lead_time_days)))
    days_to_arrival = max(0, (arrival_date - today).days)

    # Темп размера, шт/день наличия. Если продажи есть, а дней наличия в окне 0
    # (история снапшотов короче окна) — делим на все даты истории в окне.
    rates: list[float] = []
    pres_days_list: list[int] = []
    for s in sizes:
        sold = sales.get(s, 0.0)
        p_days = pres.get(s, 0)
        pres_days_list.append(p_days)
        denom = p_days if p_days > 0 else hist_window_days
        rates.append(sold / denom if sold > 0 and denom > 0 else 0.0)
    tot_rate = sum(rates)

    # Прогнозный остаток каждого размера на дату прихода.
    stock_proj = [
        max(0.0, stock.get(s, 0) - rates[i] * days_to_arrival) for i, s in enumerate(sizes)
    ]
    tot_stock_proj = sum(stock_proj)

    if tot_rate > 0:
        shares = [r / tot_rate for r in rates]
        pure = _largest_remainder(shares, qty)
        targets = [sh * (qty + tot_stock_proj) for sh in shares]
        needs = [max(0.0, t - stock_proj[i]) for i, t in enumerate(targets)]
        with_stock = _largest_remainder(needs, qty) if sum(needs) > 0 else list(pure)
    elif is_season:
        # нет статистики за сезон — нули с предупреждением (правило legacy)
        shares = [0.0] * len(sizes)
        pure = [0] * len(sizes)
        with_stock = [0] * len(sizes)
        warning = f"За сезон «{season_name}» статистики продаж по этой позиции нет — показаны нули."
    else:
        shares = [1.0 / len(sizes)] * len(sizes)
        warning = "За выбранный период продаж не было — деление поровну по размерам. Попробуйте период «всё время»."
        pure = _largest_remainder(shares, qty)
        targets = [sh * (qty + tot_stock_proj) for sh in shares]
        needs = [max(0.0, t - stock_proj[i]) for i, t in enumerate(targets)]
        with_stock = _largest_remainder(needs, qty) if sum(needs) > 0 else list(pure)

    eff = _eff_months(months, today)
    rate = tot_sales / eff if eff > 0 else 0.0
    pos_days = pos_any_days
    day_rate = tot_sales / pos_days if pos_days > 0 else 0.0

    if is_season:
        label = f"{season_name} · за все годы"
    elif period == "all":
        label = "всё время"
    else:
        label = f"{period[:-1]} мес"

    return {
        "base_name": product,
        "qty": qty,
        "period": period,
        "period_label": label,
        "mode": mode if mode in ("stock", "pure") else "stock",
        "months_in_period": len(months),
        "eff_months": round(eff, 2),
        "warning": warning,
        "arrival_date": arrival_date.isoformat(),
        "days_to_arrival": days_to_arrival,
        "totals": {
            "sold_period": round(tot_sales),
            "rate_per_month": round(rate, 1),
            "rate_per_day": round(day_rate, 2),
            "days_present": pos_days,
            "stock": tot_stock,
            "stock_at_arrival": round(tot_stock_proj),
            "order": qty,
        },
        "sizes": [
            {
                "size": s,
                "sold": round(sales.get(s, 0.0), 1),
                "days_present": pres_days_list[i],
                "rate_day": round(rates[i], 3),
                "share": round(shares[i], 4),
                "stock": stock.get(s, 0),
                "stock_at_arrival": round(stock_proj[i]),
                "order_stock": with_stock[i],
                "order_pure": pure[i],
            }
            for i, s in enumerate(sizes)
        ],
    }


# ── «Оборот» за период (порт legacy/revenue.html) ────────────────────────────
#
# Аналитическая сводка продаж, НЕ инструмент работы со стоком: выручка за
# произвольный период, категории с долями, помесячный ряд по категориям
# (последние REVENUE_MONTHS месяцев, независимо от выбранного периода — как в
# legacy), топ позиций. Всё нетто (продажи минус возвраты). Исключённые из
# аналитики позиции (упаковка/сертификаты) не участвуют.

REVENUE_MONTHS = 18


def build_revenue(db: Session, org_id: int, date_from: str, date_to: str) -> dict:
    sign_qty = case((Sale.is_return, -Sale.qty), else_=Sale.qty)
    sign_rev = case((Sale.is_return, -Sale.revenue), else_=Sale.revenue)
    join_products = and_(
        Product.id == Sale.product_id,
        Product.org_id == org_id,
        Product.excluded.is_(False),
    )

    # Позиции за период (нетто по базовым именам).
    rows = db.execute(
        select(
            Product.base_name,
            func.max(Product.category),
            func.sum(sign_qty),
            func.sum(sign_rev),
        )
        .select_from(Sale)
        .join(Product, join_products)
        .where(Sale.org_id == org_id, Sale.date >= date_from, Sale.date <= date_to)
        .group_by(Product.base_name)
    ).all()

    items = []
    total_qty = total_rev = 0.0
    cats: dict[str, dict] = {}
    for base, category, q, r in rows:
        q = float(q or 0)
        r = float(r or 0)
        if q == 0 and r == 0:
            continue
        cat = ru_category(category, base)
        items.append({"base_name": base, "category": cat,
                      "qty": round(q), "rev": round(r)})
        total_qty += q
        total_rev += r
        c = cats.setdefault(cat, {"qty": 0.0, "rev": 0.0})
        c["qty"] += q
        c["rev"] += r

    items.sort(key=lambda it: -it["rev"])
    categories = sorted(
        (
            {
                "category": name,
                "qty": round(v["qty"]),
                "rev": round(v["rev"]),
                "share": round(v["rev"] / total_rev, 3) if total_rev > 0 else 0.0,
            }
            for name, v in cats.items()
        ),
        key=lambda c: -c["rev"],
    )

    # Помесячный ряд по категориям за REVENUE_MONTHS месяцев (нетто-выручка;
    # отрицательные месяцы категория может дать при перевесе возвратов —
    # клиент клипует в 0 при отрисовке, как в legacy).
    today = date.today()
    first = date(today.year, today.month, 1)
    months = []
    y, m = first.year, first.month
    for _ in range(REVENUE_MONTHS):
        months.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    months.reverse()
    month_col = func.substr(Sale.date, 1, 7)
    month_rows = db.execute(
        select(month_col, func.max(Product.category), func.sum(sign_rev))
        .select_from(Sale)
        .join(Product, join_products)
        .where(Sale.org_id == org_id, month_col >= months[0])
        .group_by(month_col, Product.category)
    ).all()
    by_month: dict[str, dict[str, float]] = {mm: {} for mm in months}
    for mm, category, r in month_rows:
        if mm in by_month:
            cat = ru_category(category)  # разные raw могут слиться в одну русскую
            by_month[mm][cat] = by_month[mm].get(cat, 0.0) + float(r or 0)
    monthly = [
        {"month": mm,
         "total": round(sum(by_month[mm].values())),
         "by_category": {c: round(v) for c, v in by_month[mm].items()}}
        for mm in months
    ]

    best_cat = categories[0] if categories else None
    return {
        "date_from": date_from,
        "date_to": date_to,
        "total_rev": round(total_rev),
        "total_qty": round(total_qty),
        "positions": len(items),
        "avg_check": round(total_rev / total_qty) if total_qty > 0 else 0,
        "best_category": best_cat,
        "categories": categories,
        "monthly": monthly,
        "items": items[:300],  # топ-300: хватает и для топ-15, и для поиска
    }


PULSE_MONTHS = 6  # «Пульс»: сравнение с этим числом ПОЛНЫХ месяцев


def _pulse_price_map(db: Session, org_id: int, today: date) -> dict[int, float]:
    """Цена позиции для оценки склада в ₽.

    Фактическая средняя цена продажи (нетто за 365 дней); если позиция не
    продавалась — номинальная розница из карточки (sale_price). Ноль остаётся
    нулём: у позиции без цены стоимость склада честно не оцениваем.
    """
    since = (today - timedelta(days=365)).isoformat()
    sign_qty = case((Sale.is_return, -Sale.qty), else_=Sale.qty)
    sign_rev = case((Sale.is_return, -Sale.revenue), else_=Sale.revenue)
    rows = db.execute(
        select(Sale.product_id, func.sum(sign_qty), func.sum(sign_rev))
        .where(Sale.org_id == org_id, Sale.date >= since)
        .group_by(Sale.product_id)
    ).all()
    sold: dict[int, float] = {}
    for pid, q, r in rows:
        q, r = float(q or 0), float(r or 0)
        if q >= 1 and r > 0:
            sold[pid] = r / q
    prices: dict[int, float] = {}
    for pid, nominal in db.execute(
        select(Product.id, Product.sale_price)
        .where(Product.org_id == org_id, Product.excluded.is_(False))
    ).all():
        prices[pid] = sold.get(pid, float(nominal or 0))
    return prices


def build_pulse(db: Session, org_id: int, today: date | None = None) -> dict:
    """«Пульс»: этот месяц против среднего за 6 полных месяцев.

    Две шкалы, обе в ₽:
    - продажи: нетто-выручка текущего месяца, экстраполированная на полный
      месяц по прошедшим дням, против средней нетто-выручки за 6 полных
      месяцев;
    - склад: текущая стоимость остатка (шт × цена) против средней стоимости
      склада за те же 6 месяцев (среднее дневных сумм по StockDay).

    pct = текущее/среднее; 360° на круге = среднее за 6 месяцев.
    Месяцы без данных в среднее не входят (молодой аккаунт), их число видно
    по len(months) с v != None.
    """
    today = today or date.today()
    cur_month = f"{today.year:04d}-{today.month:02d}"
    months: list[str] = []
    y, m = today.year, today.month
    for _ in range(PULSE_MONTHS):
        m -= 1
        if m == 0:
            y, m = y - 1, 12
        months.append(f"{y:04d}-{m:02d}")
    months.reverse()

    # --- Продажи: помесячная нетто-выручка -------------------------------
    sign_rev = case((Sale.is_return, -Sale.revenue), else_=Sale.revenue)
    join_products = and_(
        Product.id == Sale.product_id,
        Product.org_id == org_id,
        Product.excluded.is_(False),
    )
    month_col = func.substr(Sale.date, 1, 7)
    sale_rows = dict(
        db.execute(
            select(month_col, func.sum(sign_rev))
            .select_from(Sale)
            .join(Product, join_products)
            .where(Sale.org_id == org_id, month_col >= months[0])
            .group_by(month_col)
        ).all()
    )
    sales_months = [
        {"month": mm, "v": round(float(sale_rows[mm]))} if mm in sale_rows
        else {"month": mm, "v": None}
        for mm in months
    ]
    known_sales = [x["v"] for x in sales_months if x["v"] is not None]
    sales_avg6 = sum(known_sales) / len(known_sales) if known_sales else 0.0

    mtd = float(sale_rows.get(cur_month, 0) or 0)
    days_in_month = (
        (date(today.year + (today.month == 12), today.month % 12 + 1, 1)
         - date(today.year, today.month, 1)).days
    )
    # Дни для экстраполяции — по ДАННЫМ, а не по календарю: если синк отстал
    # (последняя продажа/снапшот — 4-е число, а сегодня 7-е), делить на 7
    # значит занижать прогноз. Берём последний день, за который есть данные
    # (продажи или остатки) в текущем месяце, но не позже сегодня.
    last_sale = db.execute(
        select(func.max(Sale.date)).where(Sale.org_id == org_id)
    ).scalar()
    last_stock = db.execute(
        select(func.max(StockDay.date)).where(StockDay.org_id == org_id)
    ).scalar()
    last_data = max(filter(None, [last_sale, last_stock]), default=None)
    if last_data and last_data[:7] == cur_month:
        days_passed = min(today.day, int(last_data[8:10]))
    else:
        days_passed = today.day
    projected = mtd / days_passed * days_in_month if days_passed else mtd

    # --- Склад: дневные суммы qty × цена → средние по месяцам -------------
    prices = _pulse_price_map(db, org_id, today)
    stock_rows = db.execute(
        select(StockDay.date, StockDay.product_id, StockDay.qty)
        .where(StockDay.org_id == org_id,
               func.substr(StockDay.date, 1, 7) >= months[0])
    ).all()
    day_val: dict[str, float] = {}
    for d, pid, qty in stock_rows:
        if pid in prices and qty:
            day_val[d] = day_val.get(d, 0.0) + float(qty) * prices[pid]
        else:
            day_val.setdefault(d, 0.0)
    month_vals: dict[str, list[float]] = {}
    for d, v in day_val.items():
        month_vals.setdefault(d[:7], []).append(v)
    stock_months = [
        {"month": mm,
         "v": round(sum(month_vals[mm]) / len(month_vals[mm])) if mm in month_vals else None}
        for mm in months
    ]
    known_stock = [x["v"] for x in stock_months if x["v"] is not None]
    stock_avg6 = sum(known_stock) / len(known_stock) if known_stock else 0.0

    last_day = max(day_val) if day_val else None
    stock_now = day_val.get(last_day, 0.0) if last_day else 0.0

    def _pct(cur: float, avg: float):
        return round(cur / avg, 3) if avg > 0 else None

    return {
        "months": months,
        "last_sale_date": last_sale,
        "last_stock_date": last_stock,
        "sales": {
            "months": sales_months,
            "avg6": round(sales_avg6),
            "mtd": round(mtd),
            "projected": round(projected),
            "days_passed": days_passed,
            "days_in_month": days_in_month,
            "pct": _pct(projected, sales_avg6),
        },
        "stock": {
            "months": stock_months,
            "avg6": round(stock_avg6),
            "current": round(stock_now),
            "as_of": last_day,
            "pct": _pct(stock_now, stock_avg6),
        },
    }
