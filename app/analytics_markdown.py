"""«Скидки» — markdown-рекомендации: что уценить и на сколько, чтобы разморозить деньги.

Базовое правило портировано из legacy/turnover.html (defaultDiscount, проверено
на реальном бренде):

- нет остатка (cs <= 0) — скидка не ставится, позиции нет в отчёте;
- новинка (dis < 30 дней в стоке) → 10 %;
- оборачиваемость > 2000 ₽/день → 15 % (запас < 100 дней) / 20 % (запас >= 100);
- 1000–2000 ₽/день → 30 % / 40 % по тому же порогу запаса;
- < 1000 ₽/день (в т.ч. совсем без продаж) → 50 % / 60 %;
- нет продаж вовсе (rate = 0, запас в днях не считается) — трактуем как
  затоварку (legacy: zat=null → 999), т.е. глубокая ветка каждого правила.

Развитие поверх legacy (фича про уценку, а не про тотальную распродажу):

- позиции, которым уценка НЕ нужна, получают 0 % и в отчёт не входят:
  * новинка без затоварки (dis < 30 и запаса < 100 дней) — рано скидывать;
  * бестселлер (класс best по порогам организации) с запасом < 100 дней —
    распродастся сам, скидка только съест маржу.
  Их число отдаётся счётчиком fresh_excluded_count.
- «замороженные деньги» frozen = остаток × себестоимость; если себестоимости
  нет — по цене продажи с пометкой no_cost=True (и отдельной суммой в карточке);
- days_since_sale — дней с последней продажи (None, если продаж не было);
- новая цена new_price = цена × (1 − скидка), округлённая вниз до 10 ₽
  «психологически»: кратные 100 ₽ сдвигаются на шаг вниз (4990, а не 5000);
- expected_recovery = остаток × new_price — сколько денег вернёт распродажа
  по рекомендованной цене;
- каждая строка снабжена человекочитаемой причиной рекомендации;
- сортировка по замороженным деньгам (frozen) по убыванию.
"""
from datetime import date

NEW_DAYS = 30          # новинка: меньше месяца в стоке (dis < 30)
TOP_TURNOVER = 2000    # legacy: «топ продаж» → 15/20 %
MID_TURNOVER = 1000    # legacy: середина → 30/40 %
OVERSTOCK_DAYS = 90    # запас в днях; больше — затоварка, скидка глубже
                       # (legacy: zat ≥ 100% от нормы 90 дней = запас ≥ 90 дн)
NO_SALES_DAYS = 999    # нет продаж → как затоварка (legacy: zat=null → 999)
PRICE_STEP = 10        # шаг округления новой цены, ₽


def psych_price(raw: float) -> int:
    """Психологическая цена: вниз до 10 ₽, круглые сотни — ещё на шаг ниже.

    4990 остаётся 4990, 5000 → 4990, 5004 → 4990, 1200 → 1190, 347 → 340.
    """
    p = int(raw // PRICE_STEP) * PRICE_STEP
    if p >= 100 and p % 100 == 0:
        p -= PRICE_STEP
    return max(p, 0)


def _recommend(cls: str, dis: int, turnover: float, days_left: int | None) -> int:
    """Рекомендованная скидка, %. 0 — уценка не нужна (в отчёт не попадает)."""
    eff = NO_SALES_DAYS if days_left is None else days_left
    over = eff >= OVERSTOCK_DAYS
    if dis < NEW_DAYS:
        return 10 if over else 0        # новинка без затоварки — рано скидывать
    if cls == "best" and not over:
        return 0                        # бестселлер с малым запасом — сам уйдёт
    if turnover > TOP_TURNOVER:
        return 15 if not over else 20
    if turnover >= MID_TURNOVER:
        return 30 if not over else 40
    return 50 if not over else 60


def _days_ru(n: int) -> str:
    """Число с правильной формой слова «день»: 1 день, 3 дня, 158 дней."""
    if n % 10 == 1 and n % 100 != 11:
        w = "день"
    elif n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        w = "дня"
    else:
        w = "дней"
    return f"{n} {w}"


def _reason(
    pct: int,
    dis: int,
    turnover: float,
    days_left: int | None,
    days_since_sale: int | None,
) -> str:
    """Человекочитаемое объяснение рекомендации."""
    turn_txt = f"оборачиваемость {round(turnover):,} ₽/день".replace(",", " ")
    if days_left is None:
        stock_txt = "продаж нет — запас не тает"
    else:
        stock_txt = f"запаса на {_days_ru(days_left)}"
    if pct == 10:
        return f"Новинка ({dis} дн в стоке), но уже {stock_txt} — освежающая скидка 10%"
    if pct in (15, 20):
        depth = "символическая скидка 15%" if pct == 15 else "скидка 20% ускорит оборот"
        return f"Продаётся хорошо ({turn_txt}), {stock_txt} — {depth}"
    if pct in (30, 40):
        return f"Вялые продажи ({turn_txt}), {stock_txt} — уценка {pct}%"
    # 50/60 — слабые: акцент на днях без продаж (если они были давно)
    if days_since_sale is None:
        return f"Продаж не было ни разу, {turn_txt} — глубокая уценка {pct}%"
    if days_since_sale >= 30:
        return f"{_days_ru(days_since_sale)} без продаж, {turn_txt} — глубокая уценка {pct}%"
    return f"Слабые продажи ({turn_txt}), {stock_txt} — глубокая уценка {pct}%"


def default_discounts(snap: dict) -> dict[str, int]:
    """Скидки по правилу legacy для ВСЕХ живых позиций с остатком ({base: pct>0}).

    Кнопка «Дефолтные скидки»: результат полностью замещает ручные значения
    (как /api/discounts/bulk в первой таблице).
    """
    out: dict[str, int] = {}
    for it in snap["items"].values():
        if it["archived"]:
            continue
        cs = int(it["cs"])
        if cs <= 0:
            continue
        rate = it["rate"]
        days_left = round(cs / rate) if rate > 0 else None
        pct = _recommend(it["cls"], it["dis"], it["turnover"], days_left)
        if pct > 0:
            out[it["base_name"]] = pct
    return out


def build_discounts(snap: dict, overrides: dict[str, float] | None = None) -> dict:
    """GET /api/discounts — рекомендации по уценке из снапшота app.analytics.

    Возвращает только позиции со скидкой > 0 (остаток есть и уценка нужна),
    отсортированные по замороженным деньгам. Архивные исключены.

    overrides — ручные скидки со страницы «Оборачиваемость» ({base: pct}):
    имеют приоритет над рекомендацией и попадают в отчёт даже там, где
    правило дало бы 0 (владелец решил — значит, уценяем).
    """
    today = date.fromisoformat(snap["today"])
    overrides = overrides or {}
    items = []
    fresh_excluded = 0

    for it in snap["items"].values():
        if it["archived"]:
            continue
        cs = int(it["cs"])
        if cs <= 0:
            continue  # нет остатка — нечего скидывать (правило legacy)

        rate = it["rate"]
        days_left = round(cs / rate) if rate > 0 else None
        last_sale = it["last_sale"]
        days_since_sale = (
            (today - date.fromisoformat(last_sale)).days if last_sale else None
        )

        manual = overrides.get(it["base_name"])
        pct = int(manual) if manual and manual > 0 else _recommend(
            it["cls"], it["dis"], it["turnover"], days_left
        )
        if pct <= 0:
            fresh_excluded += 1  # новинка без затоварки / бестселлер с малым запасом
            continue

        # Цена для расчёта: средняя фактическая, при отсутствии продаж — номинал.
        price = float(it["avg_price"] or it["sale_price"] or 0)
        cost = float(it["cost_price"] or 0)
        no_cost = cost <= 0
        frozen = round(cs * (price if no_cost else cost))
        new_price = psych_price(price * (1 - pct / 100))
        is_manual = bool(manual and manual > 0)
        items.append(
            {
                "base_name": it["base_name"],
                "category": it["category"] or "Без категории",
                "cls": it["cls"],
                "turnover": it["turnover"],
                "cs": cs,
                "stock_days_left": days_left,
                "days_since_sale": days_since_sale,
                "frozen": frozen,
                "no_cost": no_cost,
                "discount_pct": pct,
                "manual": is_manual,
                "reason": (
                    f"Ручная скидка {pct}% (задана на странице «Оборачиваемость»)"
                    if is_manual
                    else _reason(pct, it["dis"], it["turnover"], days_left, days_since_sale)
                ),
                "avg_price": round(price),
                "new_price": new_price,
                "expected_recovery": round(cs * new_price),
            }
        )

    items.sort(key=lambda x: -x["frozen"])
    return {
        "cards": {
            "frozen_total": sum(x["frozen"] for x in items),
            "frozen_no_cost": sum(x["frozen"] for x in items if x["no_cost"]),
            "positions": len(items),
            "expected_recovery": sum(x["expected_recovery"] for x in items),
        },
        "items": items,
        "fresh_excluded_count": fresh_excluded,
    }
