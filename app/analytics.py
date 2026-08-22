"""Аналитика оборачиваемости: портировано из legacy (build_turnover_data, order.html).

Все метрики считаются по базовому имени (base_name) за скользящие 365 дней:

- dis  — «дней в стоке»: даты в stock_days, где суммарный по размерам qty >= min_stock_days;
- cs   — остаток на ПОСЛЕДНЮЮ имеющуюся дату (нет строки на неё = 0);
- nq/nr — нетто продано шт / нетто выручка (продажи минус возвраты);
- rate = nq/dis, turnover = nr/dis (главная метрика, ₽/день);
- sea — сезонная оборачиваемость (правило legacy): для каждого из 4 сезонов
  ₽/день = нетто-выручка сезонных месяцев / дни в стоке в эти месяцы (окно 365);
- wos = cs/(rate*7); stockout_date = today + cs/rate;
- второй денежный слой (для финансиста, поверх розничного, ничего не заменяет):
  stock_cost = cs×себестоимость («сколько денег заморожено»), margin_unit /
  margin_pct — валовая маржа на штуке от средней фактической цены, gross_margin
  = нетто-выручка − себестоимость проданного. Позиции без себестоимости
  помечены no_cost, их маржинальные поля = None и в агрегаты они не входят;
- below_cost/loss_* — «торгуем в минус»: средняя фактическая цена продажи ниже
  себестоимости; порог значимости — тот же low_data (MIN_SIGNIF_*);
- need = rate*horizon − proj_stock, где proj_stock = max(0, cs + ordered −
  rate*lead) — ПРОГНОЗ остатка на дату прихода заказа (правило legacy
  /order: за срок производства часть стока продастся; считать потребность от
  сегодняшнего остатка — значит систематически занижать заказ на rate×lead).

Окна темпа продаж (настройка rate_window, влияет на need/wos/stockout):
- 'year'   — rate_year = nq365/dis365 (как раньше, дефолт);
- 'd90'    — rate_90 = нетто-продажи за 90 дн / дни в стоке за 90 дн;
- 'season' — rate_season: аналогичное сезонное окно прошлого года
  [today+lead−365; today+lead+horizon−365] — темп периода, НА который
  заказываем (заказ приедет через срок производства и будет продаваться
  horizon дней).
Порог min_stock_days («день в стоке») применяется во всех окнах, поэтому
«дней в стоке = 0» ещё не значит «товара не было»: позиция могла лежать
ниже порога (1–2 шт при пороге 3). Эти два случая разведены по дням
ФИЗИЧЕСКОГО наличия в окне (instock90 — дни с остатком хотя бы 1 шт):
- дней наличия нет вовсе (распродано в ноль) — продавать было нечего, темп
  окна берётся годовой, позиция помечается d90_fallback/season_fallback:
  иначе бестселлер, которого сейчас нет, выпадал из «Заказа» с причиной
  «нет продаж»;
- дни наличия есть, но ниже порога — темп считается по дням наличия. У
  неликвида (лежит 1 шт, продаж нет) он честно равен нулю, и в «Заказ» такая
  позиция не попадает НИ В ОДНОМ окне. Фолбэка здесь нет и быть не должно.
Флаг rate_fallback у позиции = темп активного окна взят из года, а не из окна.
И третий случай, появившийся вместе с прогрессивной первичной загрузкой
(история грузится фоном кусками НАЗАД от сегодня): окна темпа может ещё не
быть в базе целиком. «Дней наличия нет» тогда значит не «распродали в ноль»,
а «мы про эти дни ещё не знаем» — фолбэк на годовой темп в этом случае не
даётся (год посчитан по тем же неполным дням), позиция помечается
rate_no_history и уходит в «Не вошло и почему» с причиной «история грузится».
Покрытие окон считается один раз на снапшот от coverage_start (самая ранняя
загруженная дата) — snap["rate_window_covered"], см. _window_covered.

lead_time_days — срок производства (заказ → приход на склад). Он берётся у
производства, за которым закреплена позиция (Production.lead_time_days), и
только при пустом значении — из общей настройки; по нему считаются
proj_stock, need и gap_days. gap_days — «дыра поставки»: на сколько дней
остаток кончится РАНЬШЕ прихода заказа, max(0, (today+lead) − stockout_date).
Сезонное окно темпа остаётся общим (см. _compute_snapshot).

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

from app.categories import ru_category
from app.models import (
    CategoryMerge,
    Org,
    OrderedQty,
    Product,
    Production,
    ProductionAssign,
    Sale,
    SkuCategoryOverride,
    SkuHidden,
    StockDay,
    Warehouse,
    WarehouseStock,
)

CACHE_TTL = 600  # 10 минут
# Подписи базы денежных сумм. Одна и та же «стоимость склада» считается по трём
# разным ценам и на экране без подписи выглядит как ошибка в расчётах — каждая
# сумма в API отдаётся вместе с подписью, чтобы шаблоны её не выдумывали.
BASIS_RETAIL = "по ценам продажи из карточки"
BASIS_COST = "по себестоимости"
BASIS_AVG_SALE = "по средней цене продажи"
RATE_WINDOWS = ("year", "d90", "season")  # окна темпа продаж
# Человеческие подписи окон темпа — для бейджа на странице и шапки выгрузки.
RATE_WINDOW_RU = {
    "year": "темп за год",
    "d90": "темп за 90 дней",
    "season": "сезонный темп",
}
DEFAULT_LEAD_TIME_DAYS = 45  # срок производства: заказ → приход на склад
# Горизонт покрытия заказа (решение Влада 21.08.2026). Раньше был константой
# horizon_days=90 для всех позиций сразу — это молчаливое допущение «закажу
# один раз и больше никогда», из-за него идеальный заказ выглядел нереальным.
# Заказу надо дожить только до прихода СЛЕДУЮЩЕГО заказа: периодичность
# размещения + страховой запас. См. claude/PLAN_SaaS_assistant_2026-08-21.md §2.1.
DEFAULT_CADENCE_DAYS = 30  # как часто размещаются заказы
DEFAULT_SAFETY_DAYS = 14   # страховой запас поверх интервала
COVER_MIN_DAYS, COVER_MAX_DAYS = 21, 180
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

# Календарные сезоны по месяцу даты (правило legacy: зима = дек–фев).
_SEASON_OF_MONTH = {
    "12": "winter", "01": "winter", "02": "winter",
    "03": "spring", "04": "spring", "05": "spring",
    "06": "summer", "07": "summer", "08": "summer",
    "09": "autumn", "10": "autumn", "11": "autumn",
}
SEASONS = ("winter", "spring", "summer", "autumn")
# Доля дней сезона внутри окна 365, при которой сезон считается покрытым.
# Не 100%: самая старая загруженная дата плавает на день-другой (см.
# _season_coverage), а метрика сезона от потери одного дня не меняется.
SEASON_COVERED_SHARE = 0.9
# Столько дней в начале окна темпа разрешено «недосчитаться», прежде чем окно
# признаётся незагруженным (см. _window_covered) — тот же люфт самой ранней
# даты, из-за которого выше стоит доля, а не граница.
WINDOW_COVER_SLACK_DAYS = 3


def _is_season_new(first_stock: str | None, since: str, strict: bool) -> bool:
    """Новинка сезона: первое появление в стоке позже границы (см. минор 9)."""
    if not first_stock:
        return False
    return first_stock > since if strict else first_stock >= since


def _season_coverage(cutoff365: str, today: date,
                     coverage_start: str | None) -> dict[str, bool]:
    """Какие сезоны окна 365 дней покрыты историей (минор 9, 21.08).

    Если история загружена не вся (прогрессивная первичная загрузка), сезон,
    чьи даты начинаются раньше границы покрытия, посчитан по обрезку — раньше
    это давало «0 ₽/день» (неотличимо от «ничего не продали»). Такой сезон
    отдаём как None.

    Ревью 21.08: сравнение «coverage_start <= первый день сезона в окне»
    гасило целый сезон из-за ОДНОГО недостающего дня. Самая старая
    загруженная дата плавает: она берётся из stock_days, а день без
    положительных остатков в таблицу не попадает — у завершённого синка
    coverage_start регулярно оказывается на день-другой позже начала окна.
    Замер: при coverage_start = сегодня−363 ровно один сезон «терялся» на
    всех 53 проверенных датах. Поэтому считаем не границу, а ДОЛЮ: сезон
    покрыт, если загружено >= SEASON_COVERED_SHARE его дней внутри окна.
    Полная история (coverage_start <= cutoff365) даёт 100% по всем сезонам —
    поведение завершённого синка не меняется.
    """
    if not coverage_start:
        return {s: False for s in SEASONS}
    total: dict[str, int] = {s: 0 for s in SEASONS}
    loaded: dict[str, int] = {s: 0 for s in SEASONS}
    cur = date.fromisoformat(cutoff365)
    while cur <= today:
        season = _SEASON_OF_MONTH.get(f"{cur.month:02d}")
        if season:
            total[season] += 1
            if cur.isoformat() >= coverage_start:
                loaded[season] += 1
        cur += timedelta(days=1)
    return {s: bool(total[s]) and loaded[s] >= total[s] * SEASON_COVERED_SHARE
            for s in SEASONS}

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


# Правило дефолтных скидок (значения legacy-таблицы CC). Редактируется
# владельцем на странице «Оборачиваемость» (кнопка «Правило…»).
DEFAULT_DISCOUNT_RULE = {
    "new_days": 30,        # новинка: меньше N дней в стоке
    "new_pct": 10,         # скидка новинки при затоварке
    "top_turnover": 2000,  # порог «топ продаж», ₽/день
    "top_pct": 15,         # топ без затоварки
    "top_over_pct": 20,    # топ при затоварке
    "mid_turnover": 1000,  # порог «середины», ₽/день
    "mid_pct": 30,
    "mid_over_pct": 40,
    "weak_pct": 50,        # слабые и без продаж
    "weak_over_pct": 60,
    "overstock_days": 90,  # затоварка: запас ≥ N дней (legacy: 100% нормы 90 дн)
}


STOCK_NORM_DEFAULT = 90   # дней; та же величина, что overstock_days в правиле скидок


def stock_norm_days(settings: dict | None) -> int:
    """Норма запаса организации в днях — база колонок «Сток на N дней» и
    «Не хватает до нормы».

    Решение владельца 22.08.2026 (DECISIONS D-15): фиксированные 90 дней —
    не универсальное правило продукта. Для Chernim Cherno значение 90
    остаётся, но как настройка конкретной организации.

    Второй настройки под это НЕ заводим: подходящая уже есть — порог затоварки
    из правила скидок (`discount_rule.overstock_days`, дефолт 90, редактируется
    на «Оборачиваемости»: «Затоварка — это запас от ___ дней»). Смысл совпадает
    буквально: это и есть та норма, от которой `zat` считается в процентах.
    До этой правки владелец мог выставить там 120 — и плашка «Сток на 90 дней»
    НА ТОЙ ЖЕ СТРАНИЦЕ продолжала считать по 90.

    Это НЕ горизонт покрытия заказа (`cover_days`): тот отвечает на вопрос
    «на сколько дней продаж должен хватить заказ до следующего», а норма —
    «каким должен быть склад сегодня». Разведение принято намеренно (D-4).
    """
    rule = (settings or {}).get("discount_rule") or {}
    try:
        v = int(rule.get("overstock_days") or STOCK_NORM_DEFAULT)
    except (TypeError, ValueError):
        return STOCK_NORM_DEFAULT
    return v if 1 <= v <= 365 else STOCK_NORM_DEFAULT


def _clean_discount_rule(raw) -> dict:
    """Правило скидок из настроек org с дозаполнением дефолтов и клампами."""
    rule = dict(DEFAULT_DISCOUNT_RULE)
    if isinstance(raw, dict):
        for key, default in DEFAULT_DISCOUNT_RULE.items():
            v = raw.get(key)
            if isinstance(v, (int, float)):
                hi = 5000 if key in ("top_turnover", "mid_turnover") else (
                    365 if key.endswith("_days") else 99)
                rule[key] = int(min(max(0, v), hi if key != "top_turnover" else 10**9))
    if rule["mid_turnover"] > rule["top_turnover"]:
        rule["mid_turnover"] = rule["top_turnover"]
    return rule


def extra_settings(org: Org) -> dict:
    """Настройки сверх DEFAULT_SETTINGS (org.settings их не мерджит — models.py не трогаем).

    rate_window: 'year' | 'd90' | 'season'; lead_time_days: 1..365 (дефолт 45);
    discount_rule — правило дефолтных скидок (см. DEFAULT_DISCOUNT_RULE).
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

    def _num(key, default, lo, hi):
        v = data.get(key)
        if not isinstance(v, (int, float)):
            return default
        return int(min(max(lo, v), hi))

    cover_mode = data.get("cover_mode")
    if cover_mode not in ("cadence", "fixed"):
        cover_mode = "cadence"
    return {
        "rate_window": rate_window,
        "lead_time_days": int(lead),
        "discount_rule": _clean_discount_rule(data.get("discount_rule")),
        # Горизонт покрытия: 'cadence' — периодичность + страховой запас (дефолт),
        # 'fixed' — прежнее поведение по horizon_days.
        "cover_mode": cover_mode,
        "order_cadence_days": _num("order_cadence_days", DEFAULT_CADENCE_DAYS, 7, 365),
        "safety_days": _num("safety_days", DEFAULT_SAFETY_DAYS, 0, 120),
        # Производственные ограничения по умолчанию (мастер заказа их предзаполняет).
        "moq_units": _num("moq_units", 0, 0, 10000),
        "reserve_new_pct": _num("reserve_new_pct", 0, 0, 90),
        # Накладные расходы, % к себестоимости (доставка, таможня, брак,
        # упаковка). У многих брендов «себестоимость» в МойСкладе — это цена
        # подрядчика, а не то, во сколько партия обходится на складе.
        # Мастер заказа добавляет их к себестоимости при расчёте денег.
        "overhead_pct": _num("overhead_pct", 0, 0, 200),
        # Какие типы цен МойСклада считать ценой продажи и полной
        # себестоимостью (у каждого аккаунта они называются по-своему).
        # Пусто = угадываем по названию, см. ms_sync._price_by.
        "price_type_sale": str(data.get("price_type_sale") or ""),
        "price_type_cost": str(data.get("price_type_cost") or ""),
        # Пиковые периоды продаж бренда (см. order_planner.peak_hints).
        "peak_periods": data.get("peak_periods") if isinstance(data.get("peak_periods"), list) else [],
        # Правило распределения позиций по производствам (app/assign_rules.py).
        # ВАЖНО: ключ обязан быть здесь. Org.settings возвращает только
        # thresholds/horizon_days/min_stock_days, а сохранение настроек пишет
        # `settings.update(extra_settings(org))` — всё, чего нет в этом словаре,
        # при первом же сохранении настроек стиралось бы из settings_json.
        "assign_source": data.get("assign_source") or "manual",
        "assign_map": data.get("assign_map") if isinstance(data.get("assign_map"), dict) else {},
    }


def cover_days(settings: dict) -> int:
    """Сколько дней продаж должен закрыть заказ.

    'cadence' (дефолт): периодичность размещения заказов + страховой запас —
    заказу надо дожить до прихода следующего, а не абстрактные 90 дней.
    'fixed': прежнее поведение (horizon_days из настроек).
    """
    if settings.get("cover_mode") == "fixed":
        raw = settings.get("horizon_days_setting", settings.get("horizon_days"))
        try:
            return max(7, min(365, int(raw or 90)))
        except (TypeError, ValueError):
            return 90
    cadence = int(settings.get("order_cadence_days") or DEFAULT_CADENCE_DAYS)
    safety = int(settings.get("safety_days") or DEFAULT_SAFETY_DAYS)
    return max(COVER_MIN_DAYS, min(COVER_MAX_DAYS, cadence + safety))


def _wos_by_rate(cs: float, rate: float) -> float | None:
    """Покрытие остатка в неделях при заданном темпе (None — темпа нет)."""
    return round(cs / (rate * 7), 1) if rate > 0 else None


def _stockout_by_rate(today_iso: str, cs: float, rate: float) -> str | None:
    """Дата, когда кончится остаток при заданном темпе (ISO); None — темпа нет.

    Клэмп 3650 дней (~10 лет): у медленной позиции с большим стоком cs/rate
    даёт миллионы дней и роняет timedelta (OverflowError → 500 на дашборде).
    Дальше этого срока ответ один — «дефицита не предвидится».
    """
    if rate <= 0:
        return None
    days = min(int(cs / rate), 3650)
    return (date.fromisoformat(today_iso) + timedelta(days=days)).isoformat()


def _within(today: date, days: int, *dates: str | None) -> bool:
    """Хотя бы одна из дат (ISO) не старше days дней от today."""
    for iso in dates:
        if iso and (today - date.fromisoformat(iso)).days <= days:
            return True
    return False


def _window_covered(coverage_start: str | None, date_from: str) -> bool:
    """Загружена ли история на ВСЁ окно, начинающееся с date_from (деплой П1).

    coverage_start — самая ранняя загруженная дата (min stock_days, кладётся
    в снапшот). Первичная загрузка идёт кусками НАЗАД от сегодня, поэтому
    свежий конец окна есть всегда, а старый может быть ещё не загружен: окно
    считается покрытым, только если история начинается не позже его левой
    границы. Пусто (данных нет вовсе) = не покрыто.

    Допуск WINDOW_COVER_SLACK_DAYS — по той же причине, по которой у
    _season_coverage порог доля, а не граница: coverage_start берётся из
    stock_days, а день без положительных остатков в таблицу не попадает, и у
    ЗАВЕРШЁННОГО синка самая ранняя дата регулярно оказывается на день-другой
    позже начала окна. Без допуска годовое окно у здорового аккаунта время от
    времени объявлялось бы незагруженным.

    Зачем: «в окне нет ни одного дня наличия» и «окно ещё не загружено» —
    разные факты. Первый разрешает фолбэк темпа на год (позицию распродали
    в ноль, мерить было нечего), второй не разрешает ничего: мы про это
    окно просто ничего не знаем. См. фолбэк в _compute_snapshot.
    """
    if not coverage_start:
        return False
    slack = (date.fromisoformat(date_from)
             + timedelta(days=WINDOW_COVER_SLACK_DAYS)).isoformat()
    return coverage_start <= slack


# ── Оконные агрегаты (переиспользуются снапшотом и планировщиком заказа) ──────

def _join_products(org_id: int):
    return and_(
        Product.id == StockDay.product_id,
        Product.org_id == org_id,
        Product.excluded.is_(False),
    )


def window_dis(
    db: Session, org_id: int, min_stock: float, date_from: str, date_to: str | None = None
) -> dict[str, int]:
    """Дни в наличии (сумма остатка базы >= min_stock) в окне дат, по базам."""
    conds = [StockDay.org_id == org_id, StockDay.date >= date_from]
    if date_to is not None:
        conds.append(StockDay.date <= date_to)
    sub = (
        select(Product.base_name.label("base"), StockDay.date.label("d"))
        .select_from(StockDay)
        .join(Product, _join_products(org_id))
        .where(*conds)
        .group_by(Product.base_name, StockDay.date)
        .having(func.sum(StockDay.qty) >= min_stock)
        .subquery()
    )
    return dict(db.execute(select(sub.c.base, func.count()).group_by(sub.c.base)).all())


def window_nq(
    db: Session, org_id: int, date_from: str, date_to: str | None = None
) -> dict[str, float]:
    """Нетто-продажи (шт, минус возвраты) в окне дат, по базовым именам."""
    conds = [Sale.org_id == org_id, Sale.date >= date_from]
    if date_to is not None:
        conds.append(Sale.date <= date_to)
    sign_qty = case((Sale.is_return, -Sale.qty), else_=Sale.qty)
    join_sales = and_(
        Product.id == Sale.product_id,
        Product.org_id == org_id,
        Product.excluded.is_(False),
    )
    return dict(
        db.execute(
            select(Product.base_name, func.sum(sign_qty))
            .select_from(Sale)
            .join(Product, join_sales)
            .where(*conds)
            .group_by(Product.base_name)
        ).all()
    )


# ── Расчёт снапшота ───────────────────────────────────────────────────────────

def _compute_snapshot(db: Session, org: Org) -> dict:
    settings = dict(org.settings)
    settings.update(extra_settings(org))
    min_stock = settings["min_stock_days"]
    # Эффективный горизонт покрытия (см. cover_days). Сырое значение настройки
    # сохраняем отдельно, а settings["horizon_days"] делаем ЭФФЕКТИВНЫМ — чтобы
    # все потребители снапшота (заказ, бюджет, экспорт) считали по одному числу.
    settings["horizon_days_setting"] = settings["horizon_days"]
    horizon = cover_days(settings)
    settings["horizon_days"] = horizon
    settings["cover_days"] = horizon
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
    # Окно ОБЩЕЕ, по сроку из настроек, даже если у цехов сроки разные: иначе
    # на каждый срок пришлось бы гонять свою пару SQL-окон по всей истории
    # продаж и остатков. Сдвиг окна на разницу сроков (недели) меняет сезонный
    # темп в пределах погрешности самой сезонности, поэтому цена точности
    # здесь несоизмерима с ценой запросов; в подписи окна это оговорено.
    season_from = (today + timedelta(days=lead_time - 365)).isoformat()
    season_to = (today + timedelta(days=lead_time + horizon - 365)).isoformat()

    latest_date = db.scalar(select(func.max(StockDay.date)).where(StockDay.org_id == org.id))
    # Ревью 21.08 (минор 9): граница загруженной истории. При прогрессивной
    # первичной загрузке (деплой П1) её может быть 10 дней вместо года, и
    # сезонные метрики обязаны различать «не продавалось» и «не загружено».
    coverage_start = db.scalar(select(func.min(StockDay.date)).where(StockDay.org_id == org.id))
    season_covered = _season_coverage(cutoff365, today, coverage_start)
    # Покрыты ли загруженной историей ОКНА ТЕМПА (см. фолбэк в цикле ниже).
    # Первичная загрузка идёт кусками назад от сегодня, поэтому у клиента,
    # который синхронизировался вчера, окна «90 дней» и «сезон прошлого года»
    # могут быть пустыми не потому, что товара не было, а потому что этих дней
    # ещё нет в базе. Пустое окно и НЕЗАГРУЖЕННОЕ окно — разные вещи, и
    # фолбэк темпа (годовой темп вместо оконного) полагается только первому.
    d90_covered = _window_covered(coverage_start, cutoff90)
    season_win_covered = _window_covered(coverage_start, season_from)

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
            func.max(Product.cost_full),
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

    def _dis_window(
        date_from: str, date_to: str | None = None, min_qty: int | None = None
    ) -> dict:
        """Дни в стоке (sum qty >= порога) в окне дат, по базовым именам.

        min_qty=None — порог min_stock («день в стоке» по настройке);
        min_qty=1 — дни ФИЗИЧЕСКОГО наличия: хоть одна штука на полке.
        Считает общий window_dis (им же пользуется мастер заказа) — порог у
        него параметр, так что оба режима идут через один и тот же запрос.
        """
        floor = min_stock if min_qty is None else min_qty
        return window_dis(db, org.id, floor, date_from, date_to)

    dis90_by_base = _dis_window(cutoff90)
    dis_season_by_base = _dis_window(season_from, season_to)
    # Дни ФИЗИЧЕСКОГО наличия в тех же окнах (хоть одна штука на складе).
    # Нужны, чтобы отличить два разных случая с dis = 0: «товара не было ни
    # дня — продавать было нечего» (фолбэк на годовой темп оправдан) и «товар
    # лежал, но ниже порога min_stock_days» (хвост неликвида в 1–2 шт —
    # фолбэк вреден, нулевой темп у такой позиции честный). Те же два
    # GROUP BY по тем же индексам, что и dis-окна выше.
    inst90_by_base = _dis_window(cutoff90, min_qty=1)
    inst_season_by_base = _dis_window(season_from, season_to, min_qty=1)

    # Сезонная оборачиваемость (правило legacy /turnover): дни в стоке и
    # нетто-выручка по МЕСЯЦАМ сезона внутри окна 365 дней. Реиспользуем
    # day_totals (пары base×дата, прошедшие порог min_stock).
    month_of_day = func.substr(day_totals.c.d, 6, 2)
    sea_dis_by_base: dict[str, dict[str, int]] = {}
    for base, mm, cnt in db.execute(
        select(day_totals.c.base, month_of_day, func.count())
        .group_by(day_totals.c.base, month_of_day)
    ).all():
        season = _SEASON_OF_MONTH.get(mm)
        if season:
            rec = sea_dis_by_base.setdefault(base, {})
            rec[season] = rec.get(season, 0) + int(cnt)

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
        return window_nq(db, org.id, date_from, date_to)

    nq90_by_base = _nq_window(cutoff90)
    nq_season_by_base = _nq_window(season_from, season_to)

    # Нетто-выручка по месяцам (для сезонной оборачиваемости), окно 365.
    sale_month = func.substr(Sale.date, 6, 2)
    sea_rev_by_base: dict[str, dict[str, float]] = {}
    for base, mm, rev in db.execute(
        select(Product.base_name, sale_month, func.sum(sign_rev))
        .select_from(Sale)
        .join(Product, join_sales)
        .where(Sale.org_id == org.id, Sale.date >= cutoff365)
        .group_by(Product.base_name, sale_month)
    ).all():
        season = _SEASON_OF_MONTH.get(mm)
        if season:
            rec = sea_rev_by_base.setdefault(base, {})
            rec[season] = rec.get(season, 0.0) + float(rev or 0)

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

    # «Новинка сезона» — по первому появлению в стоке. Если история загружена
    # ПОЗЖЕ начала сезона, самая ранняя дата в данных ничего не доказывает
    # (товар мог лежать и до неё), поэтому граница — max(начало сезона,
    # начало покрытия), и в этом случае сравнение СТРОГОЕ: иначе новинками
    # становились разом все позиции (минор 9, 21.08).
    season_new_since = max(cur_season_iso, coverage_start or cur_season_iso)
    season_new_strict = bool(coverage_start and coverage_start > cur_season_iso)

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

    # Последняя дата с положительным остатком по размеру — для «по нулям уже
    # N дн» на /stocks (правило legacy: видно, сколько дней размер теряет
    # выручку). Прикрепляется только к уже существующим размерам сетки.
    last_pos_rows = db.execute(
        select(Product.base_name, Product.size, func.max(StockDay.date))
        .select_from(StockDay)
        .join(Product, join_products)
        .where(StockDay.org_id == org.id, StockDay.qty > 0)
        .group_by(Product.base_name, Product.size)
    ).all()

    # Последний день, когда позиция ФИЗИЧЕСКИ лежала на складе (по всем
    # размерам). Отвечает на вопрос «когда товар ушёл с полки» — по нему
    # видно, распродали позицию только что или её нет уже полгода
    # (см. фолбэк темпа ниже). Считается из тех же строк, без лишнего запроса.
    last_instock_by_base: dict[str, str] = {}
    for base, _size, last_pos in last_pos_rows:
        if last_pos and last_pos > last_instock_by_base.get(base, ""):
            last_instock_by_base[base] = last_pos

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
    # Раздельно — чтобы «Активный сток» показывал ручное поле и МС-часть.
    ordered_rows = db.execute(
        select(OrderedQty.base_name, OrderedQty.qty, OrderedQty.ms_qty).where(
            OrderedQty.org_id == org.id,
            OrderedQty.qty + OrderedQty.ms_qty > 0,
        )
    ).all()
    ordered_by_base = {b: float(q or 0) + float(m or 0) for b, q, m in ordered_rows}
    ordered_manual = {b: float(q or 0) for b, q, m in ordered_rows}
    ordered_ms = {b: float(m or 0) for b, q, m in ordered_rows}

    # Срок производства по подрядчику. У цехов он разный (свой цех шьёт
    # 21 день, Иваново 45, Бишкек 70), и от него зависят прогнозный остаток к
    # приходу партии, «дыра поставки» и сам размер заказа — считать всё по
    # одному общему сроку значит врать по половине каталога.
    # Два лёгких запроса (десятки строк) и БЕЗ импорта app.api — иначе цикл
    # импорта api → analytics → api. Правило выбора срока то же, что в
    # app/api.py:apply_production_rules, иначе страница «Заказ» показала бы
    # один срок, а расчёт шёл бы по другому.
    prod_rows = db.execute(
        select(Production.id, Production.lead_time_days, Production.is_main)
        .where(Production.org_id == org.id)
    ).all()
    prod_lead_by_id = {int(pid): int(lead or 0) for pid, lead, _m in prod_rows}
    main_prod_id = next((int(pid) for pid, _l, is_main in prod_rows if is_main), None)
    # Позиция без записи в production_assign — на основном производстве;
    # пустой срок у производства = «как в общих настройках».
    default_lead = prod_lead_by_id.get(main_prod_id) or lead_time
    lead_by_base = {
        base: (prod_lead_by_id.get(int(pid)) or lead_time)
        for base, pid in db.execute(
            select(ProductionAssign.base_name, ProductionAssign.production_id)
            .where(ProductionAssign.org_id == org.id)
        ).all()
        if int(pid) in prod_lead_by_id  # запись на удалённый цех = основное
    }

    # Архив («в архив» на Оборачиваемости) и пользовательские категории.
    hidden_set = {
        b for b, in db.execute(
            select(SkuHidden.base_name).where(SkuHidden.org_id == org.id)
        )
    }
    cat_override = dict(db.execute(
        select(SkuCategoryOverride.base_name, SkuCategoryOverride.category)
        .where(SkuCategoryOverride.org_id == org.id)
    ).all())
    cat_merge = dict(db.execute(
        select(CategoryMerge.from_category, CategoryMerge.to_category)
        .where(CategoryMerge.org_id == org.id)
    ).all())

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
    for base, category, sale_price, cost_purchase, cost_full, archived in meta_rows:
        # Деньги считаем по ПОЛНОЙ себестоимости; если она не задана — по
        # закупочной цене (у брендов «под ключ» это одно и то же число, у
        # брендов со своим производством закупочная = только пошив, и тогда
        # бюджет занижен вдвое — UI обязан об этом предупреждать).
        cost_price = float(cost_full or 0) or float(cost_purchase or 0)
        # Категория для отображения: русская (перевод латинских групп МС или
        # keyword-категоризация по имени), поверх — пользовательские правила:
        # слияние категорий и перенос отдельной позиции («ведут МС черти как»).
        cat = ru_category(category, base)
        cat = cat_merge.get(cat, cat)
        cat = cat_override.get(base, cat)
        items[base] = {
            "base_name": base,
            "category": cat,
            "hidden": base in hidden_set,
            "ordered_manual": ordered_manual.get(base, 0.0),
            "ordered_ms": ordered_ms.get(base, 0.0),
            "sale_price": float(sale_price or 0),
            "cost_price": float(cost_price or 0),
            "cost_purchase": float(cost_purchase or 0),
            "cost_is_full": bool(cost_full),
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
            "season_new": _is_season_new(first_stock_by_base.get(base),
                                         season_new_since, season_new_strict),
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

    for base, size, last_pos in last_pos_rows:
        item = items.get(base)
        if item is not None and size in item["sizes"]:
            item["sizes"][size]["last_pos"] = last_pos

    # ── Производные метрики ──────────────────────────────────────────────────
    for item in items.values():
        base = item["base_name"]
        # Срок производства ЭТОЙ позиции и дата прихода заказа, сделанного
        # сегодня: по ним считаются proj_stock, need и gap_days.
        lead = lead_by_base.get(base, default_lead)
        arrival = today + timedelta(days=lead)
        item["lead_time_days"] = lead
        dis, cs, nq, nr = item["dis"], item["cs"], item["nq"], item["nr"]
        rate_year = nq / dis if dis > 0 else 0.0
        # Оборачиваемость — скорость, с которой позиция приносит деньги;
        # отрицательной она не бывает. Если возвраты за период перевесили
        # продажи, скорость честно нулевая (а сама нетто-выручка остаётся
        # в «Выручке» со знаком минус — это факт, его не прячем).
        turnover = max(0.0, nr) / dis if dis > 0 else 0.0

        # Свежесть позиции: когда её последний раз видели на полке и когда
        # последний раз покупали. Нужны, чтобы отличить «распродали» от
        # «умерло» (см. фолбэк ниже).
        last_instock = last_instock_by_base.get(base)
        last_sale_date = item["last_sale"]

        # Продажи в сезонном окне прошлого года — в ровно том периоде, на
        # который приедет и будет продаваться этот заказ. Это второй, помимо
        # свежести, признак «вещь ещё нужна» (см. фолбэк ниже).
        nq_season = float(nq_season_by_base.get(base) or 0)
        season_demand = nq_season > 0
        # Новинка, которой в сезонном окне прошлого года ещё не существовало:
        # судить по этому окну о ней нельзя, и «в сезон не продавалась» —
        # не улика. Такую позицию фолбэк подхватывает.
        # Но «первое появление позже сезонного окна» доказывает новизну ТОЛЬКО
        # на загруженной истории: при частичной загрузке (деплой П1) самая
        # ранняя дата в данных — это дата загрузки, а не рождения товара, и
        # новинками разом становился бы весь каталог, снимая проверку «позиция
        # жива» со всех позиций сразу. Нет истории — нет и вывода о новизне.
        first_stock = first_stock_by_base.get(base)
        season_unknown = season_win_covered and (
            not first_stock or first_stock > season_to
        )

        # Темп за 90 дней: нетто-продажи за 90 дн / дни в стоке за 90 дн.
        # dis90 = 0 бывает по двум РАЗНЫМ причинам, и лечатся они по-разному:
        #   1) товара на складе не было ни дня (распродан в ноль) — продавать
        #      было нечего, делить не на что. Берём годовой темп и помечаем
        #      d90_fallback: иначе бестселлер, которого сейчас нет, выпадает
        #      из «Заказа» с причиной «нет продаж» — ровно тот товар, который
        #      нужнее всего дозаказать;
        #   2) товар лежал, но меньше порога min_stock_days (хвост неликвида
        #      в 1–2 шт). Здесь годовой темп затянул бы мёртвый товар в заказ.
        #      Дни физического наличия есть — считаем темп по ним, и у
        #      непродающейся позиции он честно равен нулю.
        # Случая (1) мало: «товара не было на складе» одинаково верно и для
        # бестселлера, распроданного на прошлой неделе, и для зимнего пальто,
        # которого нет с января. Годовой темп годится только для первого —
        # второму он в августе выписывает заказ на 30 шт (ревью, «сезонное
        # эхо»). Поэтому фолбэк требует ещё одного из трёх подтверждений:
        #   • позиция была жива внутри окна (лежала на полке или продавалась)
        #     — темп мерить было не на чем именно потому, что её раскупили;
        #   • в сезонном окне прошлого года по ней были продажи — вещь как раз
        #     входит в свой сезон, и дошить её правильно, даже если на полке
        #     её нет давно (тот самый бестселлер, распроданный полгода назад,
        #     из-за которого проверку по свежести и не стали вводить раньше);
        #   • сезонного окна у позиции просто нет — она новее его: новинку,
        #     распроданную в ноль, отсекать не за что.
        # Ни одного подтверждения — темп окна остаётся нулевым, и позиция
        # уходит в «Не вошло и почему» с объяснением, а не в заказ.
        # И ещё одно условие, поверх всех трёх: окно должно быть ЗАГРУЖЕНО.
        # «Ни дня наличия за 90 дней» на клиенте, у которого истории пока 10
        # дней, не значит «распродали в ноль» — значит «мы ещё не знаем».
        # Выдавать это за отсутствие товара и заказывать по годовому темпу
        # нельзя: годовой темп там посчитан по тем же 10 дням. Такие позиции
        # уходят в «Не вошло и почему» с честной причиной «история грузится».
        dis90 = int(dis90_by_base.get(base, 0))
        inst90 = int(inst90_by_base.get(base, 0))
        nq90 = float(nq90_by_base.get(base) or 0)
        d90_empty = dis90 <= 0 and inst90 <= 0
        d90_no_history = d90_empty and not d90_covered
        d90_fallback = d90_empty and d90_covered and (
            _within(today, 90, last_instock, last_sale_date)
            or season_demand
            or season_unknown
        )
        if dis90 > 0:
            rate_90 = max(0.0, nq90 / dis90)
        elif inst90 > 0:
            rate_90 = max(0.0, nq90 / inst90)
        else:
            rate_90 = rate_year if d90_fallback else 0.0

        # Сезонный темп: окно прошлого года, на которое придётся заказ.
        # Ровно та же развилка и то же требование подтверждения, что у «90 дней».
        dis_season = int(dis_season_by_base.get(base, 0))
        inst_season = int(inst_season_by_base.get(base, 0))
        season_empty = dis_season <= 0 and inst_season <= 0
        season_no_history = season_empty and not season_win_covered
        season_fallback = season_empty and season_win_covered and (
            _within(today, horizon, last_instock, last_sale_date)
            or season_demand
            or season_unknown
        )
        if dis_season > 0:
            rate_season = max(0.0, nq_season / dis_season)
        elif inst_season > 0:
            rate_season = max(0.0, nq_season / inst_season)
        else:
            rate_season = rate_year if season_fallback else 0.0

        rate = {"year": rate_year, "d90": rate_90, "season": rate_season}[rate_window]

        item["rate"] = round(rate_year, 4)  # темп за год (обратная совместимость)
        item["rate_year"] = round(rate_year, 4)
        item["rate_90"] = round(rate_90, 4)
        item["rate_season"] = round(rate_season, 4)
        item["season_fallback"] = season_fallback
        item["d90_fallback"] = d90_fallback
        # Окно активного темпа ещё не загружено целиком (прогрессивная
        # первичная загрузка): темпа по нему нет и фолбэка не дали — про это
        # окно мы просто ничего не знаем. Отдельный флаг, чтобы страница
        # сказала «история грузится», а не «позиции не было на складе».
        item["rate_no_history"] = {
            "year": False, "d90": d90_no_history, "season": season_no_history
        }[rate_window]
        # Позиции не было на складе всё окно, и фолбэк ей НЕ дали: продаж в
        # окне нет, на полке давно нет, в сезон заказа не продаётся. Отдельный
        # признак нужен, чтобы объяснить исключение по-человечески. Окно при
        # этом обязано быть загружено (иначе это rate_no_history выше).
        item["rate_stale"] = {
            "year": False,
            "d90": d90_empty and d90_covered and not d90_fallback,
            "season": season_empty and season_win_covered and not season_fallback,
        }[rate_window]
        item["dis90"] = dis90
        item["instock90"] = inst90
        # Темп активного окна взят не из окна, а из года (данных в окне нет).
        item["rate_fallback"] = {
            "year": False, "d90": d90_fallback, "season": season_fallback
        }[rate_window]
        item["rate_active"] = round(rate, 4)
        item["turnover"] = round(turnover)
        item["cls"] = classify(turnover, thresholds)
        # «Мало данных»: метрикам нельзя доверять, пока не набралась статистика
        # (см. MIN_SIGNIF_*). Алерты/топы такие позиции пропускают, UI помечает.
        item["low_data"] = dis < MIN_SIGNIF_DIS or nq < MIN_SIGNIF_NQ
        # Покрытие/стокаут/потребность — по АКТИВНОМУ окну темпа.
        item["wos"] = _wos_by_rate(cs, rate)
        item["stockout_date"] = _stockout_by_rate(today.isoformat(), cs, rate)
        # «Дыра поставки»: остаток кончится раньше, чем приедет заказ
        # (по сроку производства ЭТОЙ позиции).
        item["gap_days"] = (
            max(0, (arrival - date.fromisoformat(item["stockout_date"])).days)
            if item["stockout_date"] else 0
        )
        # Средняя фактическая цена продажи. nr > 0 обязательно: возвраты
        # вычитаются из выручки, и у базы, где они перевесили продажи,
        # нетто-выручка отрицательна — «средняя цена −4 888 ₽» отравляла
        # весь денежный слой (себестоимость, маржу, «заморожено», алерт
        # «торгуете в минус»). Отрицательной цены не бывает: такой базе
        # средней цены продажи просто НЕТ (None), деньги считаются по
        # номиналу из карточки, а сам факт отдаётся флагом returns_over_sales.
        item["returns_over_sales"] = bool(nq > 0 and nr <= 0)
        item["avg_price"] = round(nr / nq) if nq > 0 and nr > 0 else None
        sale_price = item["sale_price"]
        item["discount_fact"] = (
            round(1 - (nr / nq) / sale_price, 3)
            if nq > 0 and nr > 0 and sale_price > 0
            else None
        )
        # ── Себестоимость и валовая маржа (второй денежный слой) ─────────
        # Розничные цифры выше остаются как были; здесь считается то, о чём
        # спрашивает финансист: сколько денег вложено (себестоимость) и сколько
        # на них заработано (маржа). Позиции без себестоимости (её может не
        # быть в МойСкладе) помечаются no_cost и НЕ считаются по нулю: все
        # маржинальные поля у них None и в агрегаты они не входят — так же,
        # как это уже сделано на «Скидках» (analytics_markdown.py).
        cost = item["cost_price"]
        no_cost = cost <= 0
        item["no_cost"] = no_cost
        # Цена, от которой считаем маржу: фактическая средняя (после скидок),
        # а если продаж не было ИЛИ возвраты съели всю выручку — номинал из
        # карточки (средней цены продажи у такой базы нет, см. avg_price).
        # Цены нет вовсе — маржи просто не существует.
        avg_price = item["avg_price"]
        margin_price = float(avg_price if avg_price is not None else (sale_price or 0))
        price_known = margin_price > 0 or avg_price is not None
        item["margin_price"] = round(margin_price) if price_known else None
        # Деньги в остатке: одни и те же штуки в трёх разных ценах.
        item["stock_retail"] = round(cs * sale_price)
        item["stock_sale"] = round(cs * margin_price)
        item["stock_cost"] = None if no_cost else round(cs * cost)
        if no_cost or not price_known:
            item["margin_unit"] = None
            item["margin_pct"] = None
            item["stock_margin"] = None
        else:
            item["margin_unit"] = round(margin_price - cost)
            # Процент — от цены продажи; при нулевой цене процента не бывает.
            item["margin_pct"] = (
                round((margin_price - cost) / margin_price, 3)
                if margin_price > 0
                else None
            )
            item["stock_margin"] = round(cs * (margin_price - cost))
        # Валовая маржа за год = нетто-выручка − себестоимость проданного.
        item["gross_margin"] = None if no_cost else round(nr - nq * cost)
        # «Торгуем в минус»: средняя фактическая цена ниже себестоимости.
        item["below_cost"] = bool(
            not no_cost and avg_price is not None and avg_price < cost
        )
        if item["below_cost"]:
            item["loss_unit"] = round(cost - avg_price)
            item["loss_total"] = round((cost - avg_price) * nq)
            item["loss_stock"] = round((cost - avg_price) * cs)
        else:
            item["loss_unit"] = 0
            item["loss_total"] = 0
            item["loss_stock"] = 0
        # Сезонная оборачиваемость ₽/день: 0 = «по сезону нет продаж»,
        # None = «сезон не покрыт загруженной историей» (фронт красит серым).
        # Отрицательной она не бывает: у зимней вещи летом возвраты легко
        # перевешивают продажи, и «−882 ₽/день» в колонке «Лето» читается как
        # ошибка расчёта, а не как факт. Сезоны, где так вышло, перечислены
        # в sea_returns — их подписываем словами. Непокрытый сезон в
        # sea_returns не попадает: там показано «нет данных», а не ноль.
        s_dis = sea_dis_by_base.get(base, {})
        s_rev = sea_rev_by_base.get(base, {})
        item["sea"] = {
            s: ((round(max(0.0, s_rev.get(s, 0.0)) / s_dis[s]) if s_dis.get(s) else 0)
                if season_covered.get(s) else None)
            for s in SEASONS
        }
        item["sea_returns"] = [
            s for s in SEASONS
            if season_covered.get(s) and s_dis.get(s) and s_rev.get(s, 0.0) < 0
        ]
        # Прогноз остатка к дате прихода заказа (правило legacy /order): за
        # срок производства часть стока продастся; «едет» приходуется к той
        # же дате. Срок — свой у каждого подрядчика (lead выше).
        proj_stock = max(0.0, cs + float(item["ordered"]) - rate * lead)
        item["proj_stock"] = round(proj_stock)
        item["need"] = max(0, round(rate * horizon) - round(proj_stock))
        item["nq"] = round(nq)
        item["nr"] = round(nr)

    return {
        "generated_at": time.time(),
        "today": today.isoformat(),
        "latest_date": latest_date,
        # деплой П1: с какой даты история загружена (частичное покрытие)
        "coverage_start": coverage_start,
        "season_covered": season_covered,
        # Покрыты ли загруженной историей окна темпа: по ним «Заказ» отличает
        # «позиции не было на складе» от «этих дней ещё нет в базе».
        "rate_window_covered": {
            "year": _window_covered(coverage_start, cutoff365),
            "d90": d90_covered,
            "season": season_win_covered,
        },
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
        if not it["archived"] and not it.get("hidden") and (it["cs"] > 0 or it["nq"] > 0 or it["dis"] > 0)
    ]


def money_totals(items: list[dict]) -> dict:
    """Денежный свод по списку позиций: розница, себестоимость, валовая маржа.

    Складываются УЖЕ ОКРУГЛЁННЫЕ значения позиций — сумма по строкам таблицы
    сходится с итогом до рубля, без «расхождений из-за округления».

    Позиции без себестоимости (no_cost) в себестоимость, маржу и проценты НЕ
    входят вовсе: показать по ним ноль — значит соврать про замороженные деньги.
    Сколько их и на какую сумму по рознице — отдельными полями, чтобы человек
    видел, какая часть склада осталась за пределами расчёта.
    """
    with_cost = [it for it in items if not it.get("no_cost")]
    no_cost = [it for it in items if it.get("no_cost")]
    stock_cost = sum(it["stock_cost"] or 0 for it in with_cost)
    stock_margin = sum(it["stock_margin"] or 0 for it in with_cost)
    # «За сколько продастся» считаем по ВСЕМ позициям: для этой суммы
    # себестоимость не нужна, и молча терять здесь позиции без неё нельзя.
    # Поэтому stock_sale − stock_cost ≠ stock_margin, когда такие позиции
    # есть: процент маржи ниже считается от своей базы (с/с + маржа).
    stock_sale = sum(it["stock_sale"] for it in items)
    stock_sale_with_cost = stock_cost + stock_margin
    revenue_with_cost = sum(it["nr"] for it in with_cost)
    gross_margin = sum(it["gross_margin"] or 0 for it in with_cost)
    return {
        "positions": len(items),
        "stock_units": sum(it["cs"] for it in items),
        # Розница считается по ВСЕМ позициям — это старая, привычная сумма.
        "stock_retail": sum(it["stock_retail"] for it in items),
        "stock_retail_basis": BASIS_RETAIL,
        "stock_cost": stock_cost,
        "stock_cost_basis": BASIS_COST,
        # Потенциальная маржа в остатке: продать по средней фактической цене.
        "stock_sale": stock_sale,
        "stock_sale_basis": BASIS_AVG_SALE,
        "stock_margin": stock_margin,
        "stock_margin_pct": (
            round(stock_margin / stock_sale_with_cost, 3)
            if stock_sale_with_cost > 0
            else None
        ),
        # Заработано за 365 дней: нетто-выручка − себестоимость проданного.
        "revenue_year": sum(it["nr"] for it in items),
        "revenue_year_with_cost": revenue_with_cost,
        "gross_margin": gross_margin,
        "gross_margin_pct": (
            round(gross_margin / revenue_with_cost, 3) if revenue_with_cost > 0 else None
        ),
        # Себестоимость проданного за 365 дней и оборачиваемость КАПИТАЛА —
        # профессиональная метрика финансиста (сколько раз за год провернулись
        # вложенные в товар деньги). Знаменатель — сток по себестоимости
        # СЕГОДНЯ, а не средний за период: истории себестоимости у нас нет,
        # поэтому подпись на экране говорит об этом прямо. Метрика добавлена
        # рядом, «оборачиваемость ₽/день» на страницах не заменяет.
        "cogs_year": revenue_with_cost - gross_margin,
        "capital_turns": (
            round((revenue_with_cost - gross_margin) / stock_cost, 2)
            if stock_cost > 0
            else None
        ),
        # Позиции, по которым себестоимости нет — расчёт их не касается.
        "no_cost_positions": len(no_cost),
        "no_cost_units": sum(it["cs"] for it in no_cost),
        "no_cost_retail": sum(it["stock_retail"] for it in no_cost),
    }


def _money_by_category(items: list[dict]) -> list[dict]:
    """Тот же денежный свод в разрезе категорий (сумма по ним = общий итог)."""
    groups: dict[str, list[dict]] = {}
    for it in items:
        groups.setdefault(it["category"] or "Без категории", []).append(it)
    out = [dict(money_totals(v), name=name) for name, v in groups.items()]
    out.sort(key=lambda c: -c["stock_cost"])
    return out


def below_cost_report(items: list[dict]) -> dict:
    """«Торгуете в минус»: средняя фактическая цена продажи ниже себестоимости.

    Как считаем потерю (одна позиция):
      средняя цена = нетто-выручка за 365 дней ÷ нетто-штуки за 365 дней
      (то есть уже с учётом скидок и возвратов);
      потеря на штуке = себестоимость − средняя цена;
      потеря за год   = потеря на штуке × нетто-штуки;
      потеря в остатке = потеря на штуке × текущий остаток — столько ещё
      потеряете, если распродадите остаток по той же цене.

    Шум отсекаем тем же порогом значимости, что и весь продукт (low_data,
    MIN_SIGNIF_*): одна случайная продажа по бартерной цене тревогу не поднимает.
    Такие позиции не выбрасываются — они уходят в отдельный счётчик low_data_*,
    чтобы «мало данных» не превратилось в «мы это скрыли».
    """
    strong, weak = [], []
    for it in items:
        if not it.get("below_cost"):
            continue
        row = {
            "base_name": it["base_name"],
            "category": it["category"] or "Без категории",
            "cls": it["cls"],
            "low_data": bool(it.get("low_data")),
            "avg_price": it["avg_price"],
            "sale_price": round(it["sale_price"]),
            "cost_price": round(it["cost_price"]),
            "discount_fact": it["discount_fact"],
            "nq": int(it["nq"]),
            "cs": it["cs"],
            "loss_unit": it["loss_unit"],
            "loss_total": it["loss_total"],
            "loss_stock": it["loss_stock"],
        }
        (weak if row["low_data"] else strong).append(row)
    strong.sort(key=lambda x: -x["loss_total"])
    weak.sort(key=lambda x: -x["loss_total"])
    return {
        "items": strong,
        "positions": len(strong),
        "loss_total": sum(x["loss_total"] for x in strong),
        "loss_stock": sum(x["loss_stock"] for x in strong),
        # позиции в минусе, но со слабой статистикой — показываем числом
        "low_data_positions": len(weak),
        "low_data_loss": sum(x["loss_total"] for x in weak),
        "price_basis": BASIS_AVG_SALE,
        "cost_basis": BASIS_COST,
        "method": (
            "потеря = (себестоимость − средняя цена продажи за 365 дней) × "
            "проданные штуки; себестоимость берётся текущая из МойСклада"
        ),
    }


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
    # Минор 9 (21.08): если история начинается ПОЗЖЕ старта сезона, выручка
    # сезона обрезана, а текущий сток — полный: leftover_share завышался и
    # здоровье срывалось в alarm/«распродавайте остатки» на ровном месте.
    # Такой сезон честно помечаем no_data.
    cov_start = snap.get("coverage_start")
    season_partial = bool(cov_start and cov_start > (snap.get("cur_season_start") or ""))
    if total > 0 and not season_partial:
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
        "coverage_start": cov_start,
        "partial_coverage": season_partial,
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
        "stock_value_retail_basis": BASIS_RETAIL,
        "stock_value_cost": round(stock_value_cost),
        "stock_value_cost_basis": BASIS_COST,
        # Денежный свод по себестоимости и валовой марже + позиции без с/с
        # (для карточек «сколько денег заморожено» и «сколько заработано»).
        "money": money_totals(items),
        # «Торгуете в минус» — позиции дешевле себестоимости (без low_data-шума).
        "below_cost": below_cost_report(items),
        "stock_units": stock_units,
        "positions": len(items),
        # «Оборот в день» — только статистически значимые позиции: у low_data
        # оборачиваемость — шум (1 продажа / 2 дня в стоке = тысячи ₽/день),
        # он раздувал сумму на дашборде (фидбэк Влада 30.07, п.3 бэклога).
        "turnover_total": round(
            sum(it["turnover"] for it in items if not it.get("low_data"))
        ),
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

# Позиции не было на складе всё окно, но и «распроданным бестселлером» она не
# является: с полки ушла давно и в сезон, на который считается заказ, не
# продаётся. Годовой темп ей не даём (см. фолбэк в _compute_snapshot) — и
# объясняем это словами, а не общим «нет продаж».
_STALE_REASON = {
    "d90": "нет продаж за последние 90 дней: позиции не было на складе, "
           "а в сезон, на который считается заказ, она в прошлом году не продавалась",
    "season": "в сезонном окне прошлого года позиции не было на складе и продаж "
              "по ней не было",
}

# Окно темпа ещё не загружено целиком (прогрессивная первичная загрузка,
# история идёт кусками назад). Про такую позицию мы не знаем НИЧЕГО — ни что
# её распродали, ни что она мёртвая; годовой темп ей тоже не считается, потому
# что посчитан он по тем же неполным дням. Говорим об этом прямо, а не
# «нет продаж»: цифры появятся сами, когда догрузится история.
_NO_HISTORY_REASON = {
    "d90": "история за последние 90 дней ещё загружается — темп за это окно "
           "появится, когда синхронизация догрузит эти дни",
    "season": "сезонное окно прошлого года ещё не загружено — темп за него "
              "появится, когда синхронизация догрузит историю; пока считайте "
              "по темпу за год",
}


def build_replenish(snap: dict) -> dict:
    """GET /api/replenish — потребность в заказе, сортировка по turnover desc.

    need/wos/stockout/gap считаются по активному окну темпа (settings.rate_window);
    все три темпа (rate_year / rate_90 / rate_season) отдаются в каждом item.
    proj_stock, need и gap_days считаются по сроку производства ТОЙ позиции
    (у каждого цеха он свой) — он же отдаётся в item.lead_time_days; общий
    срок из настроек остаётся в корне ответа как дефолт для страницы.

    window_covered/coverage_start/no_history_count — про прогрессивную
    первичную загрузку: окно активного темпа может быть загружено не целиком,
    и тогда позиции без данных в нём не «распроданы в ноль», а неизвестны
    (excluded[].no_history). Страница обязана сказать это словами.
    """
    settings = snap["settings"]
    horizon = settings["horizon_days"]
    rate_window = settings.get("rate_window", "year")
    lead_time = settings.get("lead_time_days", DEFAULT_LEAD_TIME_DAYS)
    result, excluded = [], []
    # Правило legacy-таблицы: рейтинг по оборачиваемости, но позиции с малой
    # статистикой (low_data) — в конце, а не наверху: их темп из 1–2 продаж
    # завышен, и рекомендация заказа по нему — предположение, а не расчёт.
    for it in sorted(
        snap["items"].values(),
        key=lambda x: (bool(x.get("low_data")), -x["turnover"]),
    ):
        base = it["base_name"]
        if it["archived"]:
            excluded.append({"base_name": base, "reason": "архивная позиция"})
            continue
        if it["cs"] == 0 and it["nq"] <= 0 and it["dis"] == 0:
            continue  # мусорная запись без активности
        if it["need"] <= 0:
            if it["rate_active"] <= 0:
                if it["rate_fallback"]:
                    # данных за окно нет, темп взят из года — и год тоже пустой
                    reason = _NO_SALES_REASON["year"]
                elif it.get("rate_no_history"):
                    # окно ещё не загружено: это не «нет продаж», это «не знаем»
                    reason = _NO_HISTORY_REASON.get(
                        rate_window, _NO_SALES_REASON.get(rate_window, _NO_SALES_REASON["year"])
                    )
                elif it.get("rate_stale"):
                    # окно пустое, но фолбэк на год не дали: товар ушёл с полки
                    # давно и в сезон заказа не продаётся («сезонное эхо»)
                    reason = _STALE_REASON.get(
                        rate_window, _NO_SALES_REASON.get(rate_window, _NO_SALES_REASON["year"])
                    )
                else:
                    reason = _NO_SALES_REASON.get(rate_window, _NO_SALES_REASON["year"])
            elif it["ordered"] > 0:
                reason = "потребность закрыта заказом в производстве"
            else:
                reason = "запаса достаточно"
            row = {"base_name": base, "reason": reason}
            # Позиция выпала не по расчёту, а из-за незагруженной истории —
            # отдельным признаком, чтобы страница могла собрать их вместе
            # («ждём догрузки», а не «не нужны»).
            if it.get("rate_no_history") and it["rate_active"] <= 0:
                row["no_history"] = True
            excluded.append(row)
            continue
        rec = size_split(it["sizes"], it["need"])
        avg_price = it["avg_price"] or it["sale_price"]
        result.append(
            {
                "base_name": base,
                "category": it["category"],
                "cls": it["cls"],
                "low_data": it.get("low_data", False),
                "turnover": it["turnover"],
                "rate": it["rate_active"],
                "rate_year": it["rate_year"],
                "rate_90": it["rate_90"],
                "rate_season": it["rate_season"],
                "season_fallback": it["season_fallback"],
                # темп посчитан не по активному окну, а по году (данных за окно нет)
                "rate_fallback": it["rate_fallback"],
                "cs": it["cs"],
                "ordered": int(it["ordered"]),
                "proj_stock": int(it.get("proj_stock", it["cs"])),
                "wos": it["wos"],
                "stockout_date": it["stockout_date"],
                "gap_days": it["gap_days"],
                # Срок производства ЭТОЙ позиции — тот же, по которому
                # посчитаны proj_stock, need и gap_days. По наличию поля
                # страница «Заказ» понимает, что расчёт уже идёт по сроку
                # подрядчика (ответ lead_time_by_production), и переключает
                # подписи. Значение совпадает с тем, что подставит
                # app/api.py:apply_production_rules — правило выбора одно.
                "lead_time_days": it.get("lead_time_days", lead_time),
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
        # подпись активного окна для бейджа и шапки выгрузки
        "rate_window_label": RATE_WINDOW_RU.get(rate_window, RATE_WINDOW_RU["year"]),
        # сколько позиций в заказе посчитаны по годовому темпу вместо окна
        "fallback_count": sum(1 for x in result if x["rate_fallback"]),
        "lead_time_days": lead_time,
        "season_from": snap.get("season_from"),
        "season_to": snap.get("season_to"),
        # Загружена ли история на всё окно активного темпа (деплой П1). False —
        # окно считается по неполным данным: страница обязана сказать об этом
        # словами, а не показывать заказ, посчитанный «по тому, что успело
        # приехать». coverage_start — с какой даты история есть.
        "coverage_start": snap.get("coverage_start"),
        "window_covered": bool(
            snap.get("rate_window_covered", {}).get(rate_window, True)
        ),
        # сколько позиций не посчитаны вовсе: их окно ещё не загружено
        "no_history_count": sum(
            1 for e in excluded if e.get("no_history")
        ),
        "items": result,
        "excluded": excluded,
    }


def turnover_group(it: dict) -> str:
    """Группа позиции в рейтинге оборачиваемости (правила legacy-таблицы CC).

    'rank'     — рейтинг: есть продажи и достаточная статистика; ранжируется
                 по оборачиваемости и получает класс A–D;
    'low_data' — есть продажи, но статистики мало (MIN_SIGNIF_*): 1–2 продажи
                 при паре дней в стоке дают «оборачиваемость» в десятки тысяч
                 ₽/день — это шум, а не рейтинг; в списке всегда ПОСЛЕ рейтинга;
    'no_sales' — за 365 дней продаж не было (в legacy — серые строки в конце).
    """
    if it["nr"] <= 0 and it["nq"] <= 0:
        return "no_sales"
    if it.get("low_data"):
        return "low_data"
    return "rank"


_GROUP_ORDER = {"rank": 0, "low_data": 1, "no_sales": 2}


def build_turnover(snap: dict) -> dict:
    """GET /api/turnover — все позиции тремя группами (как в legacy-таблице):

    рейтинг (по turnover desc) → «мало данных» → «без продаж». Позиции с
    низкой статистикой никогда не стоят выше рейтинга: их «оборачиваемость» —
    арифметический шум, а таблица говорит бизнесу об эффективности и перезаказах.

    Вся страница считается по ГОДОВОМУ темпу: и «Оборач. за год», и «Запас,
    дней», и «Сток на N дней», и «Не хватает до нормы» (N — норма запаса
    организации, см. stock_norm_days). Поэтому покрытие
    (wos) и дата стокаута здесь тоже годовые, а не по активному окну темпа:
    в ячейке «Запас» стоят рядом «сколько дней хватит» и «до какого числа»,
    и посчитанные по разным темпам они противоречили друг другу.
    Активное окно темпа живёт на странице «Заказ» — там оно и влияет на числа.
    """
    items = []
    for it in sorted(
        snap["items"].values(),
        key=lambda x: (_GROUP_ORDER[turnover_group(x)], -x["turnover"], -x["cs"]),
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
                "sea": it.get("sea", {}),
                # Периоды, где возвраты перевесили продажи: ₽/день по ним
                # показаны нулём, и это нужно подписать словами, а не
                # оставлять читателю гадать (см. export_xlsx._turnover_note).
                "sea_returns": it.get("sea_returns", []),
                "returns_over_sales": it.get("returns_over_sales", False),
                "cls": it["cls"],
                "low_data": it.get("low_data", False),
                "group": turnover_group(it),
                "avg_price": it["avg_price"],
                "sale_price": it["sale_price"],
                "discount_fact": it["discount_fact"],
                "rate": it["rate_year"],
                # покрытие и стокаут — по тому же годовому темпу, что и
                # колонка «Запас, дней» на странице (см. докстринг)
                "wos": _wos_by_rate(it["cs"], it["rate_year"]),
                "stockout_date": _stockout_by_rate(
                    snap["today"], it["cs"], it["rate_year"]
                ),
                "archived": it["archived"],
                "hidden": it.get("hidden", False),
                # ── второй денежный слой: себестоимость и валовая маржа ──
                "cost_price": round(it["cost_price"]),
                "no_cost": it["no_cost"],
                "margin_unit": it["margin_unit"],
                "margin_pct": it["margin_pct"],
                "gross_margin": it["gross_margin"],
                "stock_cost": it["stock_cost"],
                "stock_retail": it["stock_retail"],
                "stock_sale": it["stock_sale"],
                "stock_margin": it["stock_margin"],
                "below_cost": it["below_cost"],
                "loss_unit": it["loss_unit"],
                "loss_total": it["loss_total"],
            }
        )
    # Все числа этой страницы (и её выгрузки) — годовые, включая покрытие и
    # дату стокаута. Отдаём подпись «темп за год», чтобы шапка Excel не
    # обещала окно, по которому здесь ничего не считается.
    rate_window = "year"
    # Итоги считаем по тем же позициям, что и дашборд (_live_items): без архива
    # и без скрытых — иначе «заморожено по себестоимости» на этой странице и на
    # главной разошлись бы, и обе цифры перестали бы стоить доверия.
    live = _live_items(snap)
    return {
        "items": items,
        "rate_window": rate_window,
        "rate_window_label": RATE_WINDOW_RU.get(rate_window, RATE_WINDOW_RU["year"]),
        # Норма запаса организации. «Оборачиваемость» считает «Сток на N дней»
        # и «Не хватает до нормы» в браузере (BUSINESS_LOGIC §9.12), поэтому
        # число обязано приехать с сервера — иначе настройка меняется, а
        # страница продолжает делить на 90.
        "stock_norm_days": stock_norm_days(snap.get("settings")),
        # coverage_start/season_covered — чтобы таблица гасила сезонные колонки,
        # не покрытые загруженной историей (sea[s] = null), а не рисовала
        # «0 ₽/день».
        "coverage_start": snap.get("coverage_start"),
        "season_covered": snap.get("season_covered", {}),
        # деньги: итог по всем живым позициям и разрез по категориям
        "money": money_totals(live),
        "money_by_category": _money_by_category(live),
        "below_cost": below_cost_report(live),
        "money_basis": {
            "stock_retail": BASIS_RETAIL,
            "stock_cost": BASIS_COST,
            "stock_margin": BASIS_AVG_SALE,
            "margin_unit": BASIS_AVG_SALE,
            "gross_margin": BASIS_AVG_SALE,
        },
    }


def build_active_stock(snap: dict) -> dict:
    """GET /api/active-stock — страница «Активный сток» (порт legacy /analytics).

    Все неархивные позиции с активностью: класс/оборачиваемость, остатки по
    складам, разбивка по размерам с сигналами («!» — размер есть на одном
    складе и 0 на другом; «по нулям N дн»), сток на норму запаса (%), недостаток
    до нормы (без вычета «Заказано» — как в legacy), «едет к нам» раздельно
    (ручное поле + документы МойСклад).
    """
    active = [w for w in snap["warehouses"] if w["active"]]
    wh_ids = [w["id"] for w in active]
    today = date.fromisoformat(snap["today"])
    norm = stock_norm_days(snap.get("settings"))
    items = []
    for it in snap["items"].values():
        if it["archived"] or it.get("hidden"):
            continue
        if it["cs"] <= 0 and it["nq"] <= 0 and float(it.get("ordered") or 0) <= 0:
            continue  # ни остатка, ни продаж за год, ни заказанного
        rate = it["rate_year"]
        sup = round(it["cs"] / rate) if rate > 0 else None          # запас, дней
        zat = round(sup / norm * 100) if sup is not None else None  # сток на норму, %
        defq = max(0, round(rate * norm) - it["cs"])                # недостаток
        sizes = []
        row_alert = False
        for size in sorted(it["sizes"].keys() | it["wh_stock"].keys(), key=_size_order):
            per_wh = [int(it["wh_stock"].get(size, {}).get(wid, 0)) for wid in wh_ids]
            total = sum(per_wh)
            # «!»: размер есть хотя бы на одном складе и 0 на другом.
            alert = total > 0 and any(q == 0 for q in per_wh) and len(per_wh) > 1
            if alert:
                row_alert = True
            zero_days = None
            if total == 0:
                last_pos = (it["sizes"].get(size) or {}).get("last_pos")
                if last_pos:
                    zero_days = max(0, (today - date.fromisoformat(last_pos)).days)
            sizes.append({"size": size, "per_wh": per_wh, "total": total,
                          "alert": alert, "zero_days": zero_days})
        per_wh_totals = [sum(s["per_wh"][i] for s in sizes) for i in range(len(wh_ids))]
        items.append({
            "base_name": it["base_name"],
            "category": it["category"],
            "cls": it["cls"],
            "group": turnover_group(it),
            "low_data": it.get("low_data", False),
            "turnover": it["turnover"],
            "nr": it["nr"],
            "avg_price": it["avg_price"] or it["sale_price"],
            "cs": it["cs"],
            "per_wh": per_wh_totals,
            "zat": zat,
            "defq": defq,
            "ordered_manual": round(float(it.get("ordered_manual") or 0)),
            "ordered_ms": round(float(it.get("ordered_ms") or 0)),
            "row_alert": row_alert,
            "sizes": sizes,
            # ── второй денежный слой: себестоимость и валовая маржа ──
            "cost_price": round(it["cost_price"]),
            "no_cost": it["no_cost"],
            "margin_unit": it["margin_unit"],
            "margin_pct": it["margin_pct"],
            "stock_cost": it["stock_cost"],
            "stock_retail": it["stock_retail"],
            "stock_sale": it["stock_sale"],
            "stock_margin": it["stock_margin"],
            "below_cost": it["below_cost"],
            "loss_unit": it["loss_unit"],
            "loss_total": it["loss_total"],
        })
    # Как в legacy: сортировка по оборачиваемости, без продаж — в конец.
    items.sort(key=lambda x: (_GROUP_ORDER[x["group"]], -x["turnover"], -x["cs"]))
    # «Едет к нам» карточки страницы считают вместе с остатком — значит и в
    # шапке эти деньги должны стоять отдельной суммой, иначе итог не сойдётся
    # с суммой карточек. Заказанное без себестоимости в сумму не идёт.
    incoming_units = sum(x["ordered_manual"] + x["ordered_ms"] for x in items)
    incoming_cost = sum(
        (x["ordered_manual"] + x["ordered_ms"]) * x["cost_price"]
        for x in items
        if not x["no_cost"]
    )
    incoming_sale = sum(
        (x["ordered_manual"] + x["ordered_ms"]) * x["avg_price"] for x in items
    )
    live = _live_items(snap)
    return {
        "warehouses": [{"id": w["id"], "name": w["name"]} for w in active],
        "items": items,
        # Норма запаса организации: по ней посчитаны zat и defq. Отдаём наружу,
        # чтобы заголовок колонки не обещал 90 дней, когда считали по другому
        # числу (иначе получается ровно то, чего мы не хотим: цифра выглядит
        # точной и при этом не сообщает, от чего она посчитана).
        "stock_norm_days": norm,
        # деньги: тот же свод, что на «Оборачиваемости» (сходится до рубля)
        "money": money_totals(live),
        "money_by_category": _money_by_category(live),
        "below_cost": below_cost_report(live),
        "incoming": {
            "units": incoming_units,
            "cost": round(incoming_cost),
            "sale": round(incoming_sale),
        },
        "money_basis": {
            "stock_retail": BASIS_RETAIL,
            "stock_cost": BASIS_COST,
            "stock_margin": BASIS_AVG_SALE,
            "margin_unit": BASIS_AVG_SALE,
            "incoming_cost": BASIS_COST,
            "incoming_sale": BASIS_AVG_SALE,
        },
    }


def build_stocks(snap: dict) -> dict:
    """GET /api/stocks — остатки по активным складам с разбивкой по размерам.

    У размеров с нулевым суммарным остатком отдаётся zero_days — сколько дней
    размер «по нулям» (от последней даты с положительным остатком, правило
    legacy: видно, сколько дней размер теряет выручку).
    """
    active = [w for w in snap["warehouses"] if w["active"]]
    wh_ids = [w["id"] for w in active]
    today = date.fromisoformat(snap["today"])
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
            total = sum(per_wh)
            zero_days = None
            if total == 0:
                last_pos = (it["sizes"].get(size) or {}).get("last_pos")
                if last_pos:
                    zero_days = max(0, (today - date.fromisoformat(last_pos)).days)
            sizes.append({"size": size, "per_wh": per_wh, "total": total,
                          "zero_days": zero_days})
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
