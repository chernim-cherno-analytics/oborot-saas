"""Аналитика оборачиваемости: портировано из legacy (build_turnover_data, order.html).

Все метрики считаются по базовому имени (base_name) за скользящие 365 дней:

- dis  — «дней в стоке»: даты в stock_days, где суммарный по размерам qty >= min_stock_days;
- cs   — остаток на ПОСЛЕДНЮЮ имеющуюся дату (нет строки на неё = 0);
- nq/nr — нетто продано шт / нетто выручка (продажи минус возвраты);
- rate = nq/dis, turnover = nr/dis (главная метрика, ₽/день);
- wos = cs/(rate*7); stockout_date = today + cs/rate; need = rate*horizon − cs − ordered.

Окна темпа продаж (настройка rate_window, влияет на need/wos/stockout):
- 'year'   — rate_year = nq365/dis365 (как раньше, дефолт);
- 'd90'    — rate_90 = нетто-продажи за 90 дн / дни в стоке за 90 дн;
- 'season' — rate_season: аналогичное сезонное окно прошлого года
  [today+lead−365; today+lead+horizon−365] — темп периода, НА который
  заказываем (заказ приедет через lead_time_days и будет продаваться horizon
  дней). Если истории за окно нет — фолбэк на rate_year с season_fallback=True.
Порог min_stock_days («день в стоке») применяется во всех окнах.

lead_time_days — срок производства (заказ → приход на склад). gap_days —
«дыра поставки»: на сколько дней остаток кончится РАНЬШЕ прихода заказа,
max(0, (today+lead) − stockout_date).

Агрегация — SQL (GROUP BY), в Python попадают только свёрнутые строки.
Снапшот кэшируется в памяти на 10 минут per-org; запись (заказы, настройки,
переподключение) инвалидирует кэш через invalidate().
"""
import json
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
RATE_WINDOWS = ("year", "d90", "season")  # окна темпа продаж
DEFAULT_LEAD_TIME_DAYS = 45  # срок производства: заказ → приход на склад
NO_SALES_ALERT_DAYS = 120  # неликвид: столько дней без продаж при наличии стока
STOCKOUT_ALERT_DAYS = 21  # алерт: бестселлер/хороший закончится в ближайшие N дней
OVERSTOCK_WEEKS = 26  # алерт: запаса больше, чем на полгода
# Статистическая значимость: пока позиция не набрала минимум дней в стоке и
# продаж, её метрики — шум (1 день в стоке + 1 продажа дают «оборачиваемость»
# в десятки тысяч ₽/день). Такие позиции помечаются low_data: не участвуют в
# алертах, топах и трактуются в UI как «мало данных», а не как класс A.
MIN_SIGNIF_DIS = 14  # минимум дней в стоке для доверия метрикам
MIN_SIGNIF_NQ = 3  # минимум продаж, шт
STOCKOUT_RECENT_SALE_DAYS = 45  # «упускаем выручку» — только если продажи были недавно
ALERTS_CAP_PER_TYPE = 8  # каждой группы алертов показываем не больше N (по деньгам)

# «Здоровье сезона» — отраслевой норматив sell-through 70/20/10:
# здоровый сезон = ~70% выручки по полной цене, ~20% со скидкой, ~10% остаток.
# Пороги статусов даём с люфтом относительно норматива:
SEASON_NORM = (70, 20, 10)  # эталон для подписи «Норматив: 70 / 20 / 10»
SEASON_FULL_PRICE_FLOOR = 0.95  # «полная цена»: факт. цена за шт >= 95% номинала
SEASON_HEALTHY_FULL = 0.60  # healthy: полная цена >= 60% И остаток <= 20%
SEASON_HEALTHY_LEFTOVER = 0.20
SEASON_WARNING_FULL = 0.40  # warning: полная цена 40–60% ИЛИ остаток 20–35%
SEASON_WARNING_LEFTOVER = 0.35  # иначе — alarm

_SEASON_NAMES = {3: "весна", 6: "лето", 9: "осень", 12: "зима"}

_cache: dict[int, tuple[float, dict]] = {}
_cache_lock = threading.Lock()


def _plural_ru(n: int, one: str, few: str, many: str) -> str:
    """Русская форма слова по числу: 1 неделя / 2 недели / 5 недель."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _pct_100(shares: list[float]) -> list[int]:
    """Целые проценты из долей [0..1], гарантированно суммой ровно 100
    (метод наибольшего остатка) — чтобы «9 + 36 + 56» не давало 101%."""
    if not shares or sum(shares) <= 0:
        return [0 for _ in shares]
    raw = [s * 100 for s in shares]
    floors = [int(x) for x in raw]
    rem = 100 - sum(floors)
    order = sorted(range(len(raw)), key=lambda i: raw[i] - floors[i], reverse=True)
    for i in order[:max(0, rem)]:
        floors[i] += 1
    return floors


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


def season_bounds(today: date) -> tuple[date, str]:
    """Начало текущего календарного сезона и человеческая метка («лето 2026»).

    Сезоны: весна мар–май, лето июн–авг, осень сен–ноя, зима дек–фев
    (в январе-феврале зима началась 1 декабря прошлого года).
    """
    m = today.month
    if m in (3, 4, 5):
        start = date(today.year, 3, 1)
    elif m in (6, 7, 8):
        start = date(today.year, 6, 1)
    elif m in (9, 10, 11):
        start = date(today.year, 9, 1)
    elif m == 12:
        start = date(today.year, 12, 1)
    else:  # январь–февраль
        start = date(today.year - 1, 12, 1)
    name = _SEASON_NAMES[start.month]
    if start.month == 12:
        label = f"{name} {start.year}/{(start.year + 1) % 100:02d}"
    else:
        label = f"{name} {start.year}"
    return start, label


def extra_settings(org: Org) -> dict:
    """Настройки сверх DEFAULT_SETTINGS (org.settings их не мерджит — models.py не трогаем).

    rate_window: 'year' | 'd90' | 'season'; lead_time_days: 1..365 (дефолт 45).
    """
    try:
        data = json.loads(org.settings_json or "{}")
    except ValueError:
        data = {}
    rate_window = data.get("rate_window")
    if rate_window not in RATE_WINDOWS:
        rate_window = "year"
    lead = data.get("lead_time_days")
    if not isinstance(lead, (int, float)) or not 1 <= int(lead) <= 365:
        lead = DEFAULT_LEAD_TIME_DAYS
    return {"rate_window": rate_window, "lead_time_days": int(lead)}


# ── Расчёт снапшота ───────────────────────────────────────────────────────────

def _compute_snapshot(db: Session, org: Org) -> dict:
    settings = dict(org.settings)
    settings.update(extra_settings(org))
    min_stock = settings["min_stock_days"]
    horizon = settings["horizon_days"]
    thresholds = settings["thresholds"]
    rate_window = settings["rate_window"]
    lead_time = settings["lead_time_days"]
    today = date.today()
    # Ровно 365 дат в окне (today−364 … today включительно) — иначе «дней в
    # стоке» доходило до 366 при подписи «за 365 дней».
    cutoff365 = (today - timedelta(days=364)).isoformat()
    cutoff90 = (today - timedelta(days=90)).isoformat()
    cutoff30 = (today - timedelta(days=30)).isoformat()
    # Сезонное окно прошлого года: период, НА который заказываем (заказ приедет
    # через lead_time и будет продаваться horizon дней), минус год.
    season_from = (today + timedelta(days=lead_time - 365)).isoformat()
    season_to = (today + timedelta(days=lead_time + horizon - 365)).isoformat()

    latest_date = db.scalar(select(func.max(StockDay.date)).where(StockDay.org_id == org.id))

    # excluded=False — упаковка/сертификаты/расходники не участвуют в аналитике
    # (см. app/exclusions.py); фильтр применяется во ВСЕХ запросах снапшота.
    join_products = and_(
        Product.id == StockDay.product_id,
        Product.org_id == org.id,
        Product.excluded.is_(False),
    )

    # Мета позиций: категория, цены, «архивность» базы (все размеры в архиве).
    meta_rows = db.execute(
        select(
            Product.base_name,
            func.max(Product.category),
            func.max(Product.sale_price),
            func.max(Product.cost_price),
            func.min(case((Product.archived, 1), else_=0)),
        )
        .where(Product.org_id == org.id, Product.excluded.is_(False))
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

    def _dis_window(date_from: str, date_to: str | None = None) -> dict:
        """Дни в стоке (sum qty >= min_stock) в окне дат, по базовым именам."""
        conds = [StockDay.org_id == org.id, StockDay.date >= date_from]
        if date_to is not None:
            conds.append(StockDay.date <= date_to)
        sub = (
            select(Product.base_name.label("base"), StockDay.date.label("d"))
            .select_from(StockDay)
            .join(Product, join_products)
            .where(*conds)
            .group_by(Product.base_name, StockDay.date)
            .having(func.sum(StockDay.qty) >= min_stock)
            .subquery()
        )
        return dict(db.execute(select(sub.c.base, func.count()).group_by(sub.c.base)).all())

    dis90_by_base = _dis_window(cutoff90)
    dis_season_by_base = _dis_window(season_from, season_to)

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
    join_sales = and_(
        Product.id == Sale.product_id,
        Product.org_id == org.id,
        Product.excluded.is_(False),
    )
    sales_rows = db.execute(
        select(Product.base_name, Product.size, func.sum(sign_qty), func.sum(sign_rev))
        .select_from(Sale)
        .join(Product, join_sales)
        .where(Sale.org_id == org.id, Sale.date >= cutoff365)
        .group_by(Product.base_name, Product.size)
    ).all()

    # Нетто-продажи в окнах «90 дней» и «сезон прошлого года», по базам.
    def _nq_window(date_from: str, date_to: str | None = None) -> dict:
        conds = [Sale.org_id == org.id, Sale.date >= date_from]
        if date_to is not None:
            conds.append(Sale.date <= date_to)
        return dict(
            db.execute(
                select(Product.base_name, func.sum(sign_qty))
                .select_from(Sale)
                .join(Product, join_sales)
                .where(*conds)
                .group_by(Product.base_name)
            ).all()
        )

    nq90_by_base = _nq_window(cutoff90)
    nq_season_by_base = _nq_window(season_from, season_to)

    # «Здоровье сезона» (sell-through 70/20/10): продажи ТЕКУЩЕГО календарного
    # сезона в разбивке «полная цена / скидка». Полная цена — факт. цена за шт
    # >= SEASON_FULL_PRICE_FLOOR от номинала позиции; возвраты (sign_rev < 0)
    # проходят тот же ценовой тест и вычитаются из своей корзины.
    cur_season_start, cur_season_label = season_bounds(today)
    cur_season_iso = cur_season_start.isoformat()
    is_full_price = and_(
        Sale.qty > 0,
        Sale.revenue >= SEASON_FULL_PRICE_FLOOR * Sale.qty * Product.sale_price,
    )
    season_split_rows = db.execute(
        select(
            Product.base_name,
            func.sum(case((is_full_price, sign_rev), else_=0.0)),
            func.sum(case((is_full_price, 0.0), else_=sign_rev)),
        )
        .select_from(Sale)
        .join(Product, join_sales)
        .where(Sale.org_id == org.id, Sale.date >= cur_season_iso)
        .group_by(Product.base_name)
    ).all()
    season_split = {b: (float(f or 0), float(d or 0)) for b, f, d in season_split_rows}

    # Первое появление позиции в стоке (qty > 0) — чтобы посчитать в остатке
    # сезона и новинки, которые пришли на склад в сезоне, но ещё не продавались.
    first_stock_by_base = dict(
        db.execute(
            select(Product.base_name, func.min(StockDay.date))
            .select_from(StockDay)
            .join(Product, join_products)
            .where(StockDay.org_id == org.id, StockDay.qty > 0)
            .group_by(Product.base_name)
        ).all()
    )

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

    # «Едет к нам» = локальные заказы/ручные правки (qty) + документы
    # «Заказ поставщику» из МойСклад (ms_qty, пересобирается синком).
    ordered_by_base = dict(
        db.execute(
            select(OrderedQty.base_name, OrderedQty.qty + OrderedQty.ms_qty).where(
                OrderedQty.org_id == org.id, OrderedQty.qty + OrderedQty.ms_qty > 0
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
            .join(
                Product,
                and_(
                    Product.id == WarehouseStock.product_id,
                    Product.org_id == org.id,
                    Product.excluded.is_(False),
                ),
            )
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
            # здоровье сезона: нетто-выручка сезона по корзинам + признак новинки
            "season_full_rev": season_split.get(base, (0.0, 0.0))[0],
            "season_disc_rev": season_split.get(base, (0.0, 0.0))[1],
            "season_new": (first_stock_by_base.get(base) or "") >= cur_season_iso,
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
    arrival = today + timedelta(days=lead_time)  # дата прихода заказа, сделанного сегодня
    for item in items.values():
        base = item["base_name"]
        dis, cs, nq, nr = item["dis"], item["cs"], item["nq"], item["nr"]
        rate_year = nq / dis if dis > 0 else 0.0
        turnover = nr / dis if dis > 0 else 0.0

        # Темп за 90 дней: нетто-продажи за 90 дн / дни в стоке за 90 дн.
        dis90 = int(dis90_by_base.get(base, 0))
        nq90 = float(nq90_by_base.get(base) or 0)
        rate_90 = max(0.0, nq90 / dis90) if dis90 > 0 else 0.0

        # Сезонный темп: окно прошлого года, на которое придётся заказ.
        dis_season = int(dis_season_by_base.get(base, 0))
        nq_season = float(nq_season_by_base.get(base) or 0)
        season_fallback = dis_season <= 0
        rate_season = rate_year if season_fallback else max(0.0, nq_season / dis_season)

        rate = {"year": rate_year, "d90": rate_90, "season": rate_season}[rate_window]

        item["rate"] = round(rate_year, 4)  # темп за год (обратная совместимость)
        item["rate_year"] = round(rate_year, 4)
        item["rate_90"] = round(rate_90, 4)
        item["rate_season"] = round(rate_season, 4)
        item["season_fallback"] = season_fallback
        item["rate_active"] = round(rate, 4)
        item["turnover"] = round(turnover)
        item["cls"] = classify(turnover, thresholds)
        # «Мало данных»: метрикам нельзя доверять, пока не набралась статистика
        # (см. MIN_SIGNIF_*). Алерты/топы такие позиции пропускают, UI помечает.
        item["low_data"] = dis < MIN_SIGNIF_DIS or nq < MIN_SIGNIF_NQ
        # Покрытие/стокаут/потребность — по АКТИВНОМУ окну темпа.
        item["wos"] = round(cs / (rate * 7), 1) if rate > 0 else None
        # Клэмп: у медленной позиции с большим стоком cs/rate может дать
        # миллионы дней и уронить timedelta (OverflowError → 500 на дашборде).
        # 3650 дней (~10 лет) — «дефицита не предвидится», дальше не считаем.
        stockout = (
            today + timedelta(days=min(int(cs / rate), 3650)) if rate > 0 else None
        )
        item["stockout_date"] = stockout.isoformat() if stockout else None
        # «Дыра поставки»: остаток кончится раньше, чем приедет заказ.
        item["gap_days"] = max(0, (arrival - stockout).days) if stockout else 0
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
        "season_from": season_from,
        "season_to": season_to,
        "cur_season_start": cur_season_iso,
        "cur_season_label": cur_season_label,
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


def _season_health(snap: dict, items: list[dict]) -> dict:
    """«Здоровье сезона»: sell-through текущего сезона против норматива 70/20/10.

    - full_price_rev / discounted_rev — нетто-выручка сезона по корзинам
      «полная цена» (факт. цена >= 95% номинала) и «скидка»; возвраты уже
      вычтены из своей корзины (см. _compute_snapshot).
    - leftover_value — текущий сток позиций, участвовавших в сезоне (были
      продажи в сезоне ИЛИ первое появление в стоке в сезоне), по номиналу.
    - Доли считаются от (выручка сезона + остаток), статус — по порогам
      SEASON_* (люфт вокруг норматива 70/20/10).
    """
    full = disc = leftover = 0.0
    for it in items:
        f = float(it.get("season_full_rev") or 0)
        d = float(it.get("season_disc_rev") or 0)
        full += f
        disc += d
        if f != 0 or d != 0 or it.get("season_new"):
            leftover += it["cs"] * it["sale_price"]
    # Возвраты могли увести корзину в минус — доли не бывают отрицательными.
    full = max(0.0, full)
    disc = max(0.0, disc)
    leftover = max(0.0, leftover)
    total = full + disc + leftover

    full_share = disc_share = leftover_share = 0.0
    status, reason = "no_data", None
    if total > 0:
        full_share = full / total
        disc_share = disc / total
        leftover_share = leftover / total
        if full_share >= SEASON_HEALTHY_FULL and leftover_share <= SEASON_HEALTHY_LEFTOVER:
            status = "healthy"
        elif full_share >= SEASON_WARNING_FULL and leftover_share <= SEASON_WARNING_LEFTOVER:
            status = "warning"
        else:
            status = "alarm"
        if status != "healthy":
            # Что выбилось сильнее относительно «здоровых» порогов:
            discount_gap = SEASON_HEALTHY_FULL - full_share  # мало полной цены → скидки
            leftover_gap = leftover_share - SEASON_HEALTHY_LEFTOVER  # много остатка
            reason = "leftover" if leftover_gap >= discount_gap else "discount"

    full_pct, disc_pct, leftover_pct = _pct_100([full_share, disc_share, leftover_share])
    return {
        "label": snap.get("cur_season_label", ""),
        "date_from": snap.get("cur_season_start"),
        "date_to": snap["today"],
        "full_price_rev": round(full),
        "discounted_rev": round(disc),
        "leftover_value": round(leftover),
        "full_share": round(full_share, 3),
        "disc_share": round(disc_share, 3),
        "leftover_share": round(leftover_share, 3),
        # целые проценты суммой ровно 100 — для подписей на дашборде
        "full_pct": full_pct,
        "disc_pct": disc_pct,
        "leftover_pct": leftover_pct,
        "status": status,
        "reason": reason,
        "norm": list(SEASON_NORM),
    }


def build_summary(snap: dict) -> dict:
    """GET /api/summary — карточки дашборда, алерты, топ, классы, категории."""
    items = _live_items(snap)
    today = date.fromisoformat(snap["today"])

    stock_value_retail = sum(it["cs"] * it["sale_price"] for it in items)
    stock_value_cost = sum(it["cs"] * it["cost_price"] for it in items)
    stock_units = sum(it["cs"] for it in items)

    # Классы считаем только по значимым позициям; «мало данных» — отдельно,
    # чтобы шумовые новинки не раздували число «бестселлеров».
    classes = {"weak": 0, "dull": 0, "good": 0, "best": 0, "low_data": 0}
    for it in items:
        if it.get("low_data"):
            classes["low_data"] += 1
        else:
            classes[it["cls"]] += 1

    # Алерты: только статистически значимые позиции (без low_data-шума),
    # каждая группа ранжируется ПО ДЕНЬГАМ и ограничена ALERTS_CAP_PER_TYPE.
    stockouts: list[dict] = []
    overstocks: list[dict] = []
    no_sales: list[dict] = []
    for it in items:
        base = it["base_name"]
        # ── Стокауты: под угрозой реальные продавцы (класс A/B, темп подтверждён).
        if (
            not it["low_data"]
            and it["cls"] in ("best", "good")
            and it["stockout_date"]
            and it["cs"] >= 0
        ):
            days_left = (date.fromisoformat(it["stockout_date"]) - today).days
            last_sale = it["last_sale"]
            sale_recent = (
                last_sale is not None
                and (today - date.fromisoformat(last_sale)).days <= STOCKOUT_RECENT_SALE_DAYS
            )
            lost_per_day = it["turnover"]  # ₽/день, которые приносит позиция
            if it["cs"] == 0 and it["rate"] > 0 and sale_recent:
                stockouts.append(
                    {
                        "type": "stockout",
                        "base_name": base,
                        "money": lost_per_day,
                        "text": f"{base}: распродан, теряем ≈{lost_per_day:,.0f} ₽/день".replace(",", " "),
                        "severity": "red",
                    }
                )
            elif it["cs"] > 0 and days_left <= STOCKOUT_ALERT_DAYS:
                stockouts.append(
                    {
                        "type": "stockout",
                        "base_name": base,
                        "money": lost_per_day,
                        "text": f"{base}: остатка на {days_left} "
                                f"{_plural_ru(days_left, 'день', 'дня', 'дней')} "
                                f"(≈{lost_per_day:,.0f} ₽/день) — пора заказывать".replace(",", " "),
                        "severity": "red",
                    }
                )
        # ── Заморозка денег: считаем по цене продажи, ранжируем по сумме.
        if it["cs"] > 0:
            frozen = round(it["cs"] * it["sale_price"])
            last_sale = it["last_sale"]
            no_sales_days = (
                (today - date.fromisoformat(last_sale)).days if last_sale else None
            )
            if last_sale is None or (no_sales_days or 0) > NO_SALES_ALERT_DAYS:
                since = f"{no_sales_days} дн." if no_sales_days is not None else "за всю историю"
                no_sales.append(
                    {
                        "type": "no_sales",
                        "base_name": base,
                        "money": frozen,
                        "text": f"{base}: {it['cs']} шт без продаж ({since}) — "
                                f"заморожено ≈{frozen:,.0f} ₽, не заказывать".replace(",", " "),
                        "severity": "yellow",
                    }
                )
            elif (
                not it["low_data"]
                and it["wos"] is not None
                and it["wos"] > OVERSTOCK_WEEKS
            ):
                overstocks.append(
                    {
                        "type": "overstock",
                        "base_name": base,
                        "money": frozen,
                        "text": f"{base}: запаса на {it['wos']:.0f} "
                                f"{_plural_ru(round(it['wos']), 'неделю', 'недели', 'недель')} — "
                                f"заморожено ≈{frozen:,.0f} ₽".replace(",", " "),
                        "severity": "yellow",
                    }
                )
    for group in (stockouts, overstocks, no_sales):
        group.sort(key=lambda a: -a["money"])
    alerts = (
        stockouts[:ALERTS_CAP_PER_TYPE]
        + overstocks[:ALERTS_CAP_PER_TYPE]
        + no_sales[:ALERTS_CAP_PER_TYPE]
    )

    # Топ-5 — только по позициям с достаточной статистикой (без dis=1-шумов).
    top = [
        {"base_name": it["base_name"], "turnover": it["turnover"]}
        for it in sorted(
            (i for i in items if not i["low_data"]), key=lambda x: -x["turnover"]
        )[:5]
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
        "season_health": _season_health(snap, items),
    }


_NO_SALES_REASON = {
    "year": "нет продаж за 365 дней",
    "d90": "нет продаж за последние 90 дней",
    "season": "нет продаж в сезонном окне прошлого года",
}


def build_replenish(snap: dict) -> dict:
    """GET /api/replenish — потребность в заказе, сортировка по turnover desc.

    need/wos/stockout/gap считаются по активному окну темпа (settings.rate_window);
    все три темпа (rate_year / rate_90 / rate_season) отдаются в каждом item.
    """
    settings = snap["settings"]
    horizon = settings["horizon_days"]
    rate_window = settings.get("rate_window", "year")
    lead_time = settings.get("lead_time_days", DEFAULT_LEAD_TIME_DAYS)
    result, excluded = [], []
    for it in sorted(snap["items"].values(), key=lambda x: -x["turnover"]):
        base = it["base_name"]
        if it["archived"]:
            excluded.append({"base_name": base, "reason": "архивная позиция"})
            continue
        if it["cs"] == 0 and it["nq"] <= 0 and it["dis"] == 0:
            continue  # мусорная запись без активности
        if it["need"] <= 0:
            if it["rate_active"] <= 0:
                if rate_window == "season" and it["season_fallback"]:
                    reason = _NO_SALES_REASON["year"]  # фолбэк на годовой темп
                else:
                    reason = _NO_SALES_REASON.get(rate_window, _NO_SALES_REASON["year"])
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
                "rate": it["rate_active"],
                "rate_year": it["rate_year"],
                "rate_90": it["rate_90"],
                "rate_season": it["rate_season"],
                "season_fallback": it["season_fallback"],
                "cs": it["cs"],
                "ordered": int(it["ordered"]),
                "wos": it["wos"],
                "stockout_date": it["stockout_date"],
                "gap_days": it["gap_days"],
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
    return {
        "horizon_days": horizon,
        "rate_window": rate_window,
        "lead_time_days": lead_time,
        "season_from": snap.get("season_from"),
        "season_to": snap.get("season_to"),
        "items": result,
        "excluded": excluded,
    }


def build_turnover(snap: dict) -> dict:
    """GET /api/turnover — все позиции, сортировка по turnover desc.

    Позиции с low_data (мало дней в стоке/продаж) уходят в конец списка: их
    «оборачиваемость» — арифметический шум, а не рейтинг.
    """
    items = []
    for it in sorted(
        snap["items"].values(), key=lambda x: (x.get("low_data", False), -x["turnover"])
    ):
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
                "low_data": it.get("low_data", False),
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
