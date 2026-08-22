# -*- coding: utf-8 -*-
"""Планировщик заказа под бюджет — ядро «Мастера заказа».

Задача, которую решает модуль: превратить «идеальную потребность» (её показывает
страница «Заказ») в ИСПОЛНИМЫЙ заказ — в пределах денег, которые реально есть,
к нужной дате, с учётом минимальной партии и этапов производства.

Почему не оптимизатор (рюкзак/ЛП): строгий оптимум даёт выигрыш в единицы
процентов и при этом необъясним. Прецедент проекта — первая версия «Бюджета»
ранжировала по прибыли на рубль, вытащила наверх дешёвые аксессуары и мёртвые
коллабы и была забракована. Поэтому здесь ВОЛНЫ: каждая объясняется одной
фразой, у каждой строки плана есть причина словами.

Волны (после вычета резерва на новинки):
  0. «Дыры»      — позиции, которые кончатся ДО прихода заказа: прямые потери;
  1. «База»      — минимальная партия всем кандидатам: ассортимент не схлопывается;
  2. «Углубление»— остаток денег топам по оборачиваемости до полной потребности;
  +  must-have   — позиции, которые владелец включил руками, вне очереди.

Этапы производства (решение Влада 21.08.2026). «Под ключ» (Китай) — один этап
заказ→приход. Своё производство — сначала ткань, потом пошив, и пошив стартует
после прихода ткани. Отсюда две вещи: срок производства = СУММА сроков этапов
(значит дата размещения = дата приёмки − сумма), а деньги платятся траншами —
бюджет «сейчас» закрывает только первый этап. Модель универсальная: список
последовательных этапов у каждого производства (см. models.Production.stages).

Чистая функция plan_order(snap, brief, ctx) не ходит в БД — всё, что нужно из
базы, собирает collect_context(). Так планировщик тестируется на синтетике.
"""
from __future__ import annotations

import json
import math
from datetime import date, timedelta

from sqlalchemy.orm import Session

from app import analytics
from app.analytics import size_split, window_dis, window_nq
from app.models import Org, Production

# ── Константы ────────────────────────────────────────────────────────────────

NEED_MIN = 3            # потребность меньше — не заказываем (как в «Бюджете»)
FRESH_DAYS = 90         # без продаж за N дней позиция неактуальна
MIN_WINDOW_DIS = 7      # меньше дней в наличии в окне — окну не доверяем
MIN_WINDOW_NQ = 2       # и меньше продаж в окне — сезонный индекс не строим
# Когда потребность меньше минимальной партии, вопрос не «дотягивает ли она»,
# а «на сколько дней растянется эта партия». 50 штук при продаже 1,7 шт/день —
# это месяц запаса, нормально; те же 50 штук при 0,2 шт/день — восемь месяцев
# мёртвых денег. Поэтому критерий — срок распродажи партии, а не доля need.
MOQ_MAX_COVER_DAYS = 120  # партия дольше этого срока не окупает минимум
BASE_WAVE_DAYS = 14     # волна «База» без MOQ: минимум — покрытие двух недель
SENSITIVITY_STEPS = (1.25, 1.5)  # «а если добавить денег»
# Сколько строк отдаём в списках отсева. Было 50 «чтобы не раздувать ответ»,
# но у каталога на 1000+ позиций это молча прятало большую часть отсева, и
# экран выглядел так, будто система рассмотрела 50 позиций из 900.
# Полные счётчики отдаются рядом со списком, обрезку видно.
LIST_CAP = 500

# Профили стратегии. Аудит 22.08.2026 показал, что три плитки давали
# ОДИНАКОВЫЙ план (19 поз / 191 шт / 837 885 ₽ при любом лимите доли): фильтр
# по классам почти не срезал кандидатов, а base_days не применялся вовсе из-за
# min(need, moq). Поэтому профиль теперь задаёт ровно две понятные величины:
#   width_days   — сколько дней продаж закрывает КАЖДАЯ строка в стартовой волне
#                  (0 = сразу до полной потребности, самый узкий и глубокий заказ);
#   max_share_pct — сколько бюджета максимум может забрать одна позиция.
# Ключ сортировки по-прежнему один на всю систему — оборачиваемость ₽/день.
STRATEGIES = {
    "protect": {
        "title": "Не потерять продажи",
        "width_days": 7,      # каждому по неделе продаж — максимально широкий заказ
        "max_share_pct": 20,
    },
    "balance": {
        "title": "Баланс",
        "width_days": 21,     # три недели каждому, потом углубление по обороту
        "max_share_pct": 30,
    },
    "grow": {
        "title": "Заработать максимум",
        "width_days": 0,      # сразу до полной потребности, начиная с топов
        "max_share_pct": 60,
    },
}
WIDTH_CHOICES = (7, 14, 21, 30, 0)  # 0 = до полной потребности

# Порядок причин в подписи строки: сначала зачем позиция в заказе, потом
# что ограничило количество — так фраза читается как объяснение, а не как лог.
REASON_ORDER = ("must_have", "gap", "base", "deepen", "moq", "pack",
                "capped_share", "capped_budget")

REASON_TEXT = {
    "gap": "кончится до прихода заказа",
    "base": "стартовая партия",
    "deepen": "добор до потребности",
    "must_have": "включено вручную",
    "capped_share": "срезано лимитом на позицию",
    "capped_budget": "дальше деньги закончились",
    "moq": "округлено до минимальной партии",
    "pack": "округлено до кратности упаковки",
}

# Пресеты этапов для анкеты (клиент выбирает, дальше правит сроки).
STAGE_PRESETS = {
    "turnkey": [
        {"key": "full", "name": "Производство под ключ", "lead_days": 45,
         "cost_share": 1.0, "prepay_share": 0.5, "min_units": 0, "min_by_category": {}},
    ],
    "fabric_sewing": [
        {"key": "fabric", "name": "Закупка ткани", "lead_days": 30,
         "cost_share": 0.45, "prepay_share": 1.0, "min_units": 0, "min_by_category": {}},
        {"key": "sewing", "name": "Пошив", "lead_days": 35,
         "cost_share": 0.55, "prepay_share": 0.5, "min_units": 0, "min_by_category": {}},
    ],
    "sewing_only": [
        {"key": "sewing", "name": "Пошив (ткань своя)", "lead_days": 25,
         "cost_share": 1.0, "prepay_share": 0.5, "min_units": 0, "min_by_category": {}},
    ],
}


# ── Этапы производства ───────────────────────────────────────────────────────

def normalize_stages(raw: list | None, fallback_lead: int) -> list[dict]:
    """Этапы производства с дозаполнением: доли нормируются к 1.0, сроки ≥ 0.

    Пусто/мусор → один этап на весь срок (прежнее поведение системы).
    """
    stages: list[dict] = []
    for i, st in enumerate(raw or []):
        if not isinstance(st, dict):
            continue
        try:
            lead = int(st.get("lead_days") or 0)
        except (TypeError, ValueError):
            lead = 0
        try:
            share = float(st.get("cost_share") or 0)
        except (TypeError, ValueError):
            share = 0.0
        try:
            prepay = float(st.get("prepay_share", 1.0))
        except (TypeError, ValueError):
            prepay = 1.0
        try:
            min_units = int(st.get("min_units") or 0)
        except (TypeError, ValueError):
            min_units = 0
        by_cat = st.get("min_by_category")
        min_by_category = {}
        if isinstance(by_cat, dict):
            for cat, val in by_cat.items():
                try:
                    min_by_category[str(cat)] = max(0, min(10000, int(val)))
                except (TypeError, ValueError):
                    continue
        stages.append({
            "key": str(st.get("key") or f"stage{i + 1}")[:32],
            "name": str(st.get("name") or f"Этап {i + 1}")[:64],
            "lead_days": max(0, min(365, lead)),
            "cost_share": max(0.0, min(1.0, share)),
            "prepay_share": max(0.0, min(1.0, prepay)),
            "min_units": max(0, min(10000, min_units)),
            "min_by_category": min_by_category,
        })
    if not stages:
        return [{
            "key": "full", "name": "Производство",
            "lead_days": max(1, int(fallback_lead or analytics.DEFAULT_LEAD_TIME_DAYS)),
            "cost_share": 1.0, "prepay_share": 1.0, "min_units": 0, "min_by_category": {},
        }]
    total = sum(s["cost_share"] for s in stages)
    if total <= 0:  # доли не заданы — делим поровну
        for s in stages:
            s["cost_share"] = 1.0 / len(stages)
    elif abs(total - 1.0) > 1e-6:  # нормируем, чтобы сумма была ровно 1
        for s in stages:
            s["cost_share"] = s["cost_share"] / total
    return stages


def stage_moq(stages: list[dict], category: str) -> int:
    """Минимальная партия модели = максимум минимумов по этапам.

    Минимумы разные у разных этапов и у разных категорий: закупка ткани
    может требовать 50 изделий, пошив пиджака — 20, футболки — ноль.
    Ограничение выполняется, только если выполнены ВСЕ этапы, поэтому берём
    максимум. Если у канала ткань уже закуплена (этап «Пошив (ткань своя)»),
    минимум ткани в расчёт не идёт — этапа просто нет.
    """
    best = 0
    for st in stages:
        val = st.get("min_by_category", {}).get(category, st.get("min_units", 0))
        best = max(best, int(val or 0))
    return best


def prepay_share_total(stages: list[dict]) -> float:
    """Доля себестоимости, которую надо заплатить В ДЕНЬ РАЗМЕЩЕНИЯ заказа.

    Платится предоплата первого этапа; остальные этапы стартуют позже.
    """
    if not stages:
        return 1.0
    return float(stages[0]["cost_share"]) * float(stages[0].get("prepay_share", 1.0))


def payment_plan(order_date: date, stages: list[dict], cost_total: float) -> list[dict]:
    """Календарь платежей по заказу: предоплата этапа при старте, остаток в конце."""
    out, cursor = [], order_date
    for st in stages:
        done = cursor + timedelta(days=st["lead_days"])
        stage_cost = cost_total * st["cost_share"]
        prepay = stage_cost * float(st.get("prepay_share", 1.0))
        if prepay > 0.5:
            out.append({"date": cursor.isoformat(), "amount": round(prepay),
                        "label": f"{st['name']} — предоплата"
                        if prepay < stage_cost - 0.5 else st["name"]})
        rest = stage_cost - prepay
        if rest > 0.5:
            out.append({"date": done.isoformat(), "amount": round(rest),
                        "label": f"{st['name']} — остаток"})
        cursor = done
    # Последний транш = остаток, а не round своей доли: иначе сумма календаря
    # расходилась с себестоимостью заказа на рубль и читалась как ошибка.
    if out:
        diff = round(cost_total) - sum(x["amount"] for x in out)
        out[-1]["amount"] += diff
    return out


def lead_days(stages: list[dict]) -> int:
    """Срок производства = сумма сроков последовательных этапов."""
    return sum(s["lead_days"] for s in stages) or 1


def stage_schedule(order_date: date, stages: list[dict]) -> list[dict]:
    """Календарь этапов: когда стартует, когда завершается, какая доля денег.

    Этапы последовательные: пошив начинается после прихода ткани.
    """
    out, cursor = [], order_date
    for st in stages:
        done = cursor + timedelta(days=st["lead_days"])
        out.append({
            "key": st["key"],
            "name": st["name"],
            "starts": cursor.isoformat(),
            "done": done.isoformat(),
            "lead_days": st["lead_days"],
            "cost_share": round(st["cost_share"], 4),
            "prepay_share": round(float(st.get("prepay_share", 1.0)), 4),
        })
        cursor = done
    return out


# ── Пиковые периоды продаж ───────────────────────────────────────────────────
# У каждого бренда свои пики (у Chernim Cherno: чёрная пятница, декабрь,
# апрель, июнь). Поэтому это НАСТРОЙКА организации, а не константа в коде:
# список периодов вида {"name": "Декабрь", "from": "12-01", "to": "12-31"}
# либо подвижная дата правилом {"name": "Чёрная пятница", "rule": "black_friday"}.
# Мастер по ним считает одно: до какого числа надо разместить заказ, чтобы
# успеть к пику, и предупреждает, если уже поздно.

def black_friday(year: int) -> date:
    """Последняя пятница ноября."""
    d = date(year, 11, 30)
    return d - timedelta(days=(d.weekday() - 4) % 7)


def peak_windows(periods: list | None, today: date, years: int = 2) -> list[dict]:
    """Ближайшие вхождения пиковых периодов начиная с сегодня."""
    out = []
    for p in (periods or [])[:20]:
        if not isinstance(p, dict):
            continue
        name = str(p.get("name") or "Пик")[:64]
        for y in range(today.year, today.year + years):
            try:
                if p.get("rule") == "black_friday":
                    bf = black_friday(y)
                    start, end = bf - timedelta(days=7), bf + timedelta(days=3)
                else:
                    start = date.fromisoformat(f"{y}-{p['from']}")
                    end = date.fromisoformat(f"{y}-{p['to']}")
                    if end < start:  # период через новый год
                        end = date.fromisoformat(f"{y + 1}-{p['to']}")
            except (ValueError, KeyError, TypeError):
                continue
            if end >= today:
                out.append({"name": name, "from": start.isoformat(), "to": end.isoformat()})
                break
    return sorted(out, key=lambda x: x["from"])


def peak_hints(periods: list | None, today: date, lead: int) -> list[dict]:
    """Для каждого ближайшего пика — крайняя дата размещения заказа."""
    hints = []
    for w in peak_windows(periods, today):
        start = date.fromisoformat(w["from"])
        deadline = start - timedelta(days=lead)
        days_left = (deadline - today).days
        hints.append({
            "name": w["name"], "from": w["from"], "to": w["to"],
            "order_by": deadline.isoformat(),
            "days_left": days_left,
            "late": days_left < 0,
        })
    return hints[:4]


# ── Бриф ─────────────────────────────────────────────────────────────────────

def normalize_brief(raw: dict | None, settings: dict, stages: list[dict], today: date) -> dict:
    """Ответы анкеты с дозаполнением из настроек организации.

    Обязательных полей два: дата, когда товар нужен на складе, и бюджет.
    Остальное система предзаполняет и даёт поправить (см. план §2.5).
    """
    raw = dict(raw or {})
    lead = lead_days(stages)

    def _int(key, default, lo, hi):
        v = raw.get(key)
        if v is None or v == "":
            v = default
        try:
            return int(min(max(lo, float(v)), hi))
        except (TypeError, ValueError):
            return default

    strategy = raw.get("strategy")
    if strategy not in STRATEGIES:
        strategy = "balance"
    profile = STRATEGIES[strategy]

    def _width(value, default):
        if value is None or value == "":
            return default
        try:
            v = int(value)
        except (TypeError, ValueError):
            return default
        return v if v in WIDTH_CHOICES else default

    eta = raw.get("eta_date")
    try:
        eta_date = date.fromisoformat(eta) if eta else today + timedelta(days=lead)
    except ValueError:
        eta_date = today + timedelta(days=lead)
    if eta_date <= today:
        eta_date = today + timedelta(days=1)

    scope = raw.get("budget_scope")
    if scope not in ("now", "full"):
        scope = "now" if len(stages) > 1 else "full"

    cats = raw.get("exclude_categories") or []
    must = raw.get("must_have") or []
    # Новинки система не считает принципиально: у модели без истории продаж
    # нет ни темпа, ни размерной кривой. Владелец вписывает их руками, а мы
    # честно показываем их отдельным блоком и держим под них деньги.
    new_items = []
    for it in (raw.get("new_items") or [])[:200]:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "").strip()[:255]
        if not name:
            continue
        try:
            qty = max(0, int(float(it.get("qty") or 0)))
            cost = max(0.0, float(it.get("cost") or 0))
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            continue
        new_items.append({"name": name, "qty": qty, "cost": round(cost),
                          "category": str(it.get("category") or "Новинки")[:128],
                          "total": round(qty * cost)})
    return {
        "production_id": raw.get("production_id"),
        "eta_date": eta_date.isoformat(),
        "budget": max(0, _int("budget", 0, 0, 10 ** 12)),
        "budget_scope": scope,
        "cadence_days": _int("cadence_days", settings.get("order_cadence_days", 30), 7, 365),
        "safety_days": _int("safety_days", settings.get("safety_days", 14), 0, 120),
        # Режим горизонта организации (D-27) переносится в бриф, чтобы мастер
        # считал ТЕМ ЖЕ правилом, что и страница «Заказ». Раньше он строил свой
        # горизонт из ритма и страховки всегда — то есть режим «фиксированный»
        # молча игнорировал (BUSINESS_LOGIC §9.4). Пользователь его включить не
        # мог, поэтому расхождение никого не задевало; включаем режим и правило
        # одним пакетом, иначе оно задело бы сразу.
        "cover_mode": (raw.get("cover_mode")
                       if raw.get("cover_mode") in ("cadence", "fixed")
                       else (settings.get("cover_mode")
                             if settings.get("cover_mode") in ("cadence", "fixed")
                             else "cadence")),
        "horizon_days_fixed": _int(
            "horizon_days_fixed",
            settings.get("horizon_days_setting", settings.get("horizon_days", 90)),
            7, 365),
        "strategy": strategy,
        # Ширина заказа: сколько дней продаж закрывает каждая строка стартовой
        # волной. 0 = сразу до полной потребности. Задаётся профилем стратегии,
        # но пользователь может выставить явно — это единственная ручка,
        # которая реально меняет форму заказа.
        "width_days": _width(raw.get("width_days"), profile["width_days"]),
        "max_share_pct": _int("max_share_pct", profile["max_share_pct"], 5, 100),
        # Ноль здесь — осознанный выбор «партии нет», а не пустое поле:
        # раньше он считался отсутствием значения и подменялся минимумом канала.
        "moq_units": _int("moq_units", settings.get("moq_units", 0), 0, 10000),
        "moq_explicit": raw.get("moq_units") is not None and raw.get("moq_units") != "",
        "reserve_new_pct": _int("reserve_new_pct", settings.get("reserve_new_pct", 0), 0, 90),
        # Накладные расходы к себестоимости: дефолт — настройка организации,
        # но на конкретный заказ (растаможка, срочная доставка) их можно
        # поменять прямо в анкете.
        "overhead_pct": _int("overhead_pct", settings.get("overhead_pct", 0), 0, 200),
        "exclude_categories": [str(c) for c in cats if str(c).strip()],
        "must_have": [str(b) for b in must if str(b).strip()],
        "new_items": new_items,
    }


# ── Контекст из БД ───────────────────────────────────────────────────────────

def coverage_days(snap: dict) -> int:
    """Сколько дней истории реально загружено (деплой П1).

    coverage_start кладёт в снапшот analytics (самая старая дата в stock_days).
    Его нет у старых/полных аккаунтов — тогда считаем, что истории год: ровно
    столько окон и метрик строит аналитика, и именно так система вела себя
    до прогрессивной первичной загрузки.
    """
    start = snap.get("coverage_start")
    if not start:
        return 365
    try:
        first = date.fromisoformat(start)
    except (TypeError, ValueError):
        return 365
    today = date.fromisoformat(snap["today"])
    return max(1, (today - first).days + 1)


def _seasonal_rates(
    db: Session, org: Org, snap: dict, min_stock: float,
    date_from: date, date_to: date,
) -> tuple[dict[str, float], dict[str, float]]:
    """Темп продаж на будущее окно = активный темп позиции × сезонный индекс окна.

    Аудит 22.08.2026: раньше планировщик строил СВОЙ, четвёртый темп (окно того
    же периода год назад) и полностью игнорировал настройку «окно темпа». Из-за
    этого страница «Заказ» и мастер давали разные ответы про одну позицию в один
    день — на боевых настройках (d90) «Заказ» говорил «запаса достаточно», а
    мастер ставил ту же позицию первой строкой. Теперь база — тот же
    rate_active, что и на «Заказе», а сезонность применяется как ИНДЕКС:

      индекс позиции   = темп её окна год назад ÷ её годовой темп
                         (только если в окне есть и дни наличия, и продажи);
      индекс категории = то же по всей категории — фолбэк для позиций
                         моложе года и для тех, у кого окно пустое;
      нет ни того ни другого → индекс 1.0, сезонность просто не участвует.

    Отдельно чинит тихую дыру: позиция, которая год назад лежала на складе, но
    не продавалась (только завезли), раньше получала темп 0 и молча выпадала
    из заказа. Теперь ноль продаж в окне — это «индекса нет», а не «спроса нет».

    Возвращает (темпы по базам, сезонные индексы категорий).
    """
    w_from = (date_from - timedelta(days=365)).isoformat()
    w_to = (date_to - timedelta(days=365)).isoformat()
    dis_w = window_dis(db, org.id, min_stock, w_from, w_to)
    nq_w = window_nq(db, org.id, w_from, w_to)

    # Сезонный индекс категории = темп категории в окне ÷ её годовой темп.
    cat_w: dict[str, list[float]] = {}
    cat_y: dict[str, list[float]] = {}
    for base, it in snap["items"].items():
        cat = it.get("category") or "Без категории"
        w = cat_w.setdefault(cat, [0.0, 0.0])
        w[0] += float(nq_w.get(base) or 0)
        w[1] += float(dis_w.get(base) or 0)
        y = cat_y.setdefault(cat, [0.0, 0.0])
        y[0] += float(it.get("nq") or 0)
        y[1] += float(it.get("dis") or 0)
    cat_index: dict[str, float] = {}
    for cat, (nq_win, dis_win) in cat_w.items():
        nq_year, dis_year = cat_y.get(cat, (0.0, 0.0))
        if dis_win >= MIN_WINDOW_DIS and dis_year > 0 and nq_year > 0:
            rate_win = nq_win / dis_win
            rate_year = nq_year / dis_year
            if rate_year > 0:
                cat_index[cat] = max(0.2, min(3.0, rate_win / rate_year))

    rates: dict[str, float] = {}
    for base, it in snap["items"].items():
        base_rate = float(it.get("rate_active") or it.get("rate_year") or 0)
        rate_year = float(it.get("rate_year") or 0)
        dis_b = float(dis_w.get(base) or 0)
        nq_b = float(nq_w.get(base) or 0)
        idx = None
        if dis_b >= MIN_WINDOW_DIS and nq_b >= MIN_WINDOW_NQ and rate_year > 0:
            idx = max(0.2, min(3.0, (nq_b / dis_b) / rate_year))
        if idx is None:
            idx = cat_index.get(it.get("category") or "Без категории")
        rates[base] = max(0.0, base_rate * idx) if idx else base_rate
    return rates, cat_index


def collect_context(db: Session, org: Org, snap: dict, brief: dict) -> dict:
    """Всё, что планировщику нужно из БД: сезонные темпы, свежесть, привязка к производству."""
    from app.analytics_extra import _fresh_bases

    today = date.fromisoformat(snap["today"])
    eta = date.fromisoformat(brief["eta_date"])
    # Одна функция на оба места: и страница «Заказ», и мастер спрашивают
    # analytics.cover_days. Раньше здесь стояла копия формулы «ритм + страховка»,
    # и режим «фиксированный горизонт» до мастера не доезжал.
    cover = analytics.cover_days({
        "cover_mode": brief.get("cover_mode", "cadence"),
        "horizon_days_setting": brief.get("horizon_days_fixed"),
        "order_cadence_days": brief["cadence_days"],
        "safety_days": brief["safety_days"],
    })
    min_stock = snap["settings"]["min_stock_days"]

    rate_lead, idx_lead = _seasonal_rates(db, org, snap, min_stock, today, eta)
    rate_cover, idx_cover = _seasonal_rates(
        db, org, snap, min_stock, eta, eta + timedelta(days=cover))

    # Свежесть: «нет продаж за FRESH_DAYS дней» — суждение, которое можно
    # выносить, только если эти дни ЗАГРУЖЕНЫ. При частичном покрытии окно
    # свежести молча сжимается до загруженного (запрос просто не видит более
    # старых продаж), и медленные позиции отсеиваются как «мёртвые».
    # Решение: отсев оставляем (иначе на 30 днях истории такие позиции пошли
    # бы в заказ по выдуманному темпу и съели реальные деньги), но клампим
    # окно явно и считаем отсеянных в review.hidden_by_coverage — чтобы это
    # было видно, а не молча. На полном покрытии кламп — no-op, план
    # побайтно тот же.
    cov_days = coverage_days(snap)
    fresh_days = min(FRESH_DAYS, cov_days)

    assign = {}
    if brief.get("production_id") is not None:
        from app import assign_rules
        assign = assign_rules.effective_assign(db, org)
    try:
        peaks = json.loads(org.settings_json or "{}").get("peak_periods")
    except (ValueError, AttributeError):
        peaks = None
    extra = analytics.extra_settings(org)
    return {
        "cover_days": cover,
        "overhead_pct": extra["overhead_pct"],
        # Правило распределения включено — значит «нет признака» это факт,
        # о котором стоит сказать в строке плана, а не молчание.
        "assign_source_on": extra["assign_source"] != "manual",
        "pack_multiple": _pack_multiple(db, brief.get("production_id")),
        "peak_periods": peaks if isinstance(peaks, list) else [],
        "rate_lead": rate_lead,
        "rate_cover": rate_cover,
        "fresh": _fresh_bases(db, org.id, fresh_days),
        "fresh_days": fresh_days,
        "fresh_clamped": fresh_days < FRESH_DAYS,
        # сезонность в темпах реально участвовала (окно «год назад» загружено)
        "seasonal_rates": bool(idx_lead or idx_cover),
        "assign": assign,
        "main_production_id": _main_production_id(db, org.id),
    }


def _pack_multiple(db: Session, pid) -> int:
    """Кратность упаковки канала: короб, рулон, пачка. 0/1 — кратности нет.

    Заказать 37 футболок, когда подрядчик пакует по 12, нельзя: 37 всё равно
    превратятся в 48, только про эти 11 штук узнают уже после отгрузки.
    """
    if not pid:
        return 0
    prod = db.get(Production, int(pid))
    if prod is None:
        return 0
    return max(0, int(getattr(prod, "pack_multiple", 0) or 0))


def _main_production_id(db: Session, org_id: int) -> int | None:
    row = (
        db.query(Production)
        .filter(Production.org_id == org_id, Production.is_main.is_(True))
        .first()
    )
    return row.id if row else None


# ── Планировщик (чистая функция) ─────────────────────────────────────────────

def _candidates(snap: dict, brief: dict, ctx: dict) -> tuple[list[dict], dict]:
    """Кандидаты на заказ + причины, по которым позиции не попали."""
    today = date.fromisoformat(snap["today"])
    eta = date.fromisoformat(brief["eta_date"])
    days_to_eta = max(0, (eta - today).days)
    cover = ctx["cover_days"]
    exclude = set(brief["exclude_categories"])
    must = set(brief["must_have"])
    pid = brief.get("production_id")
    assign = ctx.get("assign") or {}
    main_pid = ctx.get("main_production_id")
    # Анкета сильнее общей настройки: на конкретный заказ накладные могут
    # отличаться (растаможка, срочная доставка).
    overhead = float(brief.get("overhead_pct", ctx.get("overhead_pct") or 0) or 0)

    rows, skipped = [], {
        "archived": [], "hidden": [], "stale": [], "small_need": [],
        "no_cost": [], "low_data": [], "other_production": [], "excluded_category": [],
    }
    for base, it in snap["items"].items():
        if it.get("archived"):
            skipped["archived"].append(base)
            continue
        if it.get("hidden"):
            skipped["hidden"].append(base)
            continue
        # Позиция закреплена за другим производством — её закажем отдельным заказом.
        if pid is not None:
            own = assign.get(base, main_pid)
            if own != pid:
                skipped["other_production"].append(base)
                continue
        category = it.get("category") or "Без категории"
        if category in exclude and base not in must:
            skipped["excluded_category"].append(base)
            continue
        if base not in ctx["fresh"] and base not in must:
            skipped["stale"].append(base)
            continue

        r_lead = float(ctx["rate_lead"].get(base) or 0)
        r_cover = float(ctx["rate_cover"].get(base) or 0)
        cs = float(it.get("cs") or 0)
        ordered = max(0.0, float(it.get("ordered") or 0))
        proj_stock = max(0.0, cs + ordered - r_lead * days_to_eta)
        need = max(0, int(round(r_cover * cover - proj_stock)))
        # Дыра: остатка не хватит дожить до прихода заказа.
        gap_days = 0
        if r_lead > 0 and cs + ordered < r_lead * days_to_eta:
            gap_days = int(round(days_to_eta - (cs + ordered) / r_lead))
        if need < NEED_MIN and base not in must:
            skipped["small_need"].append(base)
            continue
        cost = float(it.get("cost_price") or 0)
        # Накладные (доставка, таможня, брак, упаковка) — настройка организации.
        # У многих брендов «себестоимость» в МойСкладе это цена подрядчика,
        # а партия обходится дороже; без этого мастер систематически занижал
        # и деньги заказа, и требуемый бюджет.
        if cost > 0 and overhead > 0:
            cost = cost * (1.0 + overhead / 100.0)
        price = float(it.get("avg_price") or it.get("sale_price") or 0)
        row = {
            "base_name": base,
            "category": category,
            "cls": it.get("cls"),
            "low_data": bool(it.get("low_data")),
            "turnover": float(it.get("turnover") or 0),
            "rate_cover": round(r_cover, 4),
            "rate_lead": round(r_lead, 4),
            "cs": int(cs),
            "ordered": int(ordered),
            "proj_stock": int(round(proj_stock)),
            "gap_days": gap_days,
            "need": need,
            "cost_price": round(cost),
            "avg_price": round(price),
            "margin": max(0.0, price - cost),
            "sizes": it.get("sizes") or {},
            "must_have": base in must,
            # Позиция без признака распределения (поставщик/папка не заполнены):
            # она попала в этот канал «по умолчанию», а не потому что так решили.
            "no_supplier": bool(ctx.get("assign_source_on")
                                and not ctx.get("assign", {}).get(base)),
        }
        if cost <= 0:
            skipped["no_cost"].append(row)   # деньги посчитать нельзя — отдельным списком
            continue
        if row["low_data"] and base not in must:
            skipped["low_data"].append(row)  # темп из 1–2 продаж — решает человек
            continue
        rows.append(row)
    # Порядок один на весь планировщик: оборачиваемость ₽/день (правило legacy).
    rows.sort(key=lambda r: (-r["turnover"], r["base_name"]))
    return rows, skipped


def _allocate(cands: list[dict], brief: dict, ctx: dict, stages: list[dict]) -> dict:
    """Волновое распределение бюджета. Возвращает alloc и причины по каждой позиции."""
    brief_moq = int(brief["moq_units"] or 0)
    width_days = int(brief.get("width_days") or 0)

    def moq_of(c: dict) -> int:
        """Минимальная партия позиции: максимум из общей и минимумов этапов
        (у этапа могут быть свои минимумы по категориям — см. stage_moq)."""
        return max(brief_moq, stage_moq(stages, c["category"]))

    pack = max(0, int(ctx.get("pack_multiple") or 0))
    budget = float(brief["budget"])
    # Резерв на новинки: либо процент от бюджета, либо сумма вписанных вручную
    # новинок — что больше. Эти деньги планировщик не распределяет.
    new_cost = sum(i["total"] for i in brief.get("new_items") or [])
    reserve = int(max(round(budget * brief["reserve_new_pct"] / 100.0), new_cost))
    # Новинки могут не влезать в бюджет — это не повод молча съесть все деньги:
    # режем резерв по бюджету и отдельно сообщаем, на сколько вышли за него.
    over_budget = max(0, reserve - int(budget))
    reserve = min(reserve, int(budget))
    money = budget - reserve
    cap_per_item = money * brief["max_share_pct"] / 100.0

    # Цена единицы для БЮДЖЕТА: 'now' — только то, что платится в день
    # размещения (предоплата первого этапа: у своего производства это ткань,
    # пошив оплачивается после её прихода), 'full' — весь заказ целиком.
    pay_share = prepay_share_total(stages) if brief["budget_scope"] == "now" else 1.0
    # Канал с нулевой предоплатой (подрядчик берёт деньги только по готовности)
    # ломал режим «деньги на сейчас»: доля = 0 → цена единицы падала до рубля
    # (max(1.0, ...)) → бюджет переставал ограничивать что-либо и план выходил
    # в десятки раз выше названной суммы. В такой ситуации «денег на сейчас»
    # просто не существует как ограничения — считаем по полной стоимости
    # заказа и говорим об этом словами.
    budget_basis = brief["budget_scope"]
    if brief["budget_scope"] == "now" and pay_share <= 1e-9:
        pay_share = 1.0
        budget_basis = "full_no_prepay"

    alloc: dict[str, int] = {}
    reasons: dict[str, list[str]] = {}
    # Сколько штук позиции вообще разрешает лимит доли. Аудит 22.08: подпись
    # «срезано лимитом на позицию» ставилась, как только лимит срезал ЦЕЛЬ
    # волны, — даже если фактическое количество упёрлось в деньги, а не в
    # лимит (11 ложных подписей из 20). Теперь причину определяем по факту:
    # достигли потолка доли — значит лимит, не достигли — значит деньги.
    cap_units: dict[str, int] = {}
    moq_skipped: dict[str, int] = {}   # позиция → на сколько дней хватило бы партии
    moq_over_cap: dict[str, int] = {}  # позиция → во сколько ₽ обходится партия
    spent = 0.0

    def unit_pay(c: dict) -> float:
        return max(1.0, c["cost_price"] * pay_share)

    def take(c: dict, target: int, reason: str, cap: float | None = -1.0) -> None:
        """Довести позицию до target штук, если хватает денег и лимита.

        cap — потолок денег на позицию В ЭТОЙ ВОЛНЕ: -1 = общий лимит доли,
        None = без лимита (must-have), число = потолок волны.
        """
        nonlocal spent
        base = c["base_name"]
        have = alloc.get(base, 0)
        unit = unit_pay(c)
        if cap_per_item > 0:
            cap_units[base] = int(math.floor(cap_per_item / unit))
        limit = cap_per_item if (cap is not None and cap < 0) else cap
        if limit is not None and limit > 0:
            by_cap = int(math.floor(limit / unit))
            if target > by_cap:
                target = by_cap
        if target <= have:
            return
        # Кратность упаковки канала: подрядчик отгружает коробами/рулонами,
        # поэтому цель округляем ВВЕРХ до кратного и деньги считаем уже по ней.
        # Округлять постфактум нельзя — заказ вылезал бы за бюджет после того,
        # как план показан.
        if pack > 1:
            target = -(-target // pack) * pack
        rest = money - spent
        afford = int(math.floor(rest / unit))
        add = min(target - have, afford)
        if pack > 1 and have + add > 0:
            # По карману может быть меньше кратного — берём ближайший вниз
            # полный короб; ноль означает «даже один короб не влезает».
            add = (((have + add) // pack) * pack) - have
        if add <= 0:
            # Минимальная партия целиком не влезла — позиция остаётся без заказа.
            return
        qty = have + add
        if pack > 1:
            reasons.setdefault(base, []).append("pack")
        # Минимальная партия — это МИНИМУМ на модель, а не кратность: меньше неё
        # производство не берёт, выше — можно любое количество.
        moq = moq_of(c)
        if moq > 0 and qty < moq:
            rate = c["rate_cover"]
            if rate > 0 and moq / rate > MOQ_MAX_COVER_DAYS:
                moq_skipped[base] = int(moq / rate)  # партия залежится — не берём
                return
            if rate <= 0:
                return
            # Лимит доли на позицию — обещание пользователю, и минимальная
            # партия его не отменяет (аудит: 60 шт × 4 029 = 80,6% бюджета при
            # лимите 30%). Проверяем ДО денег: иначе дорогая партия молча
            # уходила в «не по карману» и человек не понимал, что упёрся
            # в собственный лимит, а не в бюджет.
            if cap_per_item > 0 and moq * unit > cap_per_item + 1:
                moq_over_cap[base] = int(round(moq * unit))
                return
            if (moq - have) * unit > rest:
                return  # партия не по карману
            qty = -(-moq // pack) * pack if pack > 1 else moq
            reasons.setdefault(base, []).append("moq")
        spent += (qty - have) * unit
        alloc[base] = qty
        reasons.setdefault(base, []).append(reason)

    def base_batch(c: dict) -> int:
        """Сколько взять в стартовой волне.

        Аудит 22.08: раньше было min(need, moq) — минимальная партия становилась
        ЦЕЛЬЮ, а не полом. Из-за этого три стратегии давали одинаковый план,
        сезонность стиралась (декабрьский заказ совпадал с июльским), и заказ
        получался «всем по минимальной партии». Теперь ширину задаёт ручка
        width_days, а MOQ работает полом внутри take().
        """
        if width_days <= 0:
            return c["need"]
        return min(c["need"], max(1, int(math.ceil(c["rate_cover"] * width_days))))

    # + must-have: решение владельца выше алгоритма
    for c in cands:
        if c["must_have"]:
            take(c, c["need"], "must_have", cap=None)
    # Потолок стартовых волн: стартовая партия по определению не должна съедать
    # заметную долю бюджета — иначе первые позиции забирают всё и волна «База»
    # перестаёт делать то, ради чего она есть. При «до полной потребности»
    # (width_days = 0) стартовой волны нет — сразу углубляемся по обороту.
    n_cands = max(1, len(cands))
    base_cap = min(money / n_cands, cap_per_item) if (money > 0 and width_days > 0) else 0.0
    if width_days > 0:
        # 0. Дыры — позиции, которые кончатся до прихода: сначала останавливаем
        #    потери (стартовая партия), глубину доберём волной 2.
        for c in cands:
            if c["gap_days"] > 0:
                take(c, base_batch(c), "gap", cap=base_cap)
        # 1. База — ассортимент не схлопывается в три модели
        for c in cands:
            take(c, base_batch(c), "base", cap=base_cap)
    # 2. Углубление — деньги туда, где быстрее оборот
    for c in cands:
        take(c, c["need"], "deepen" if width_days > 0 else "gap" if c["gap_days"] > 0 else "deepen")

    return {
        "alloc": alloc, "reasons": reasons, "cap_units": cap_units,
        "moq_skipped": moq_skipped, "moq_over_cap": moq_over_cap, "spent": spent,
        "reserve": reserve, "new_cost": new_cost, "new_over_budget": over_budget,
        "money": money, "pay_share": pay_share, "budget_basis": budget_basis,
        "cap_per_item": cap_per_item,
    }


def _coverage(snap: dict, ctx: dict, stages: list[dict]) -> dict:
    """На какой истории стоит план и хватает ли её (деплой П1).

    Порог — lead_days + cover_days: план экстраполирует спрос ровно на столько
    дней вперёд (пока заказ едет + пока он должен закрывать спрос), поэтому
    столько же истории нужно НАЗАД. Меньше — темпы взяты с обрезка, сезонность
    из окна «год назад» недоступна, и часть позиций отсеялась до расчёта.
    """
    cov_days = coverage_days(snap)
    # Больше, чем система вообще умеет хранить, требовать нельзя. Глубина
    # истории ограничена HISTORY_DAYS (год), а при фиксированном горизонте
    # сумма «срок производства + горизонт» легко даёт 410 дней. Тогда план
    # НАВСЕГДА помечался бы предварительным: блок «упущено» исчезал бы с
    # экрана, а оформление каждого заказа требовало бы подтверждения с
    # текстом «история ещё загружается», который никогда не станет правдой.
    from app.ms_sync import HISTORY_DAYS
    needed = min(lead_days(stages) + int(ctx["cover_days"]), int(HISTORY_DAYS))
    return {
        "start": snap.get("coverage_start"),
        "days": cov_days,
        "needed_days": needed,
        "partial": cov_days < needed,
        "seasonal_rates": bool(ctx.get("seasonal_rates")),
    }


def plan_order(snap: dict, brief: dict, ctx: dict, stages: list[dict],
               with_sensitivity: bool = True) -> dict:
    """Бриф → план заказа. Чистая функция: в БД не ходит."""
    today = date.fromisoformat(snap["today"])
    eta = date.fromisoformat(brief["eta_date"])
    cover = ctx["cover_days"]
    coverage = _coverage(snap, ctx, stages)
    cands, skipped = _candidates(snap, brief, ctx)
    res = _allocate(cands, brief, ctx, stages)
    alloc, reasons = res["alloc"], res["reasons"]

    items, not_included = [], []
    # Аудит 22.08: «упущено» складывало недобор по позициям, которые В ЗАКАЗЕ,
    # и показывалось над таблицей, где их нет (4,6 млн над таблицей на 671 тыс).
    # Считаем две РАЗНЫЕ величины и обе — в марже, а не в выручке.
    lost_missing = 0.0   # позиции, не вошедшие совсем
    lost_short = 0.0     # недобор по позициям, которые в заказе
    for c in cands:
        qty = alloc.get(c["base_name"], 0)
        unmet = max(0, c["need"] - qty)
        if qty <= 0:
            lost_missing += unmet * c["margin"]
            not_included.append({
                "base_name": c["base_name"], "category": c["category"], "cls": c["cls"],
                "turnover": round(c["turnover"]), "need": c["need"],
                "cost_price": c["cost_price"], "need_rub": c["need"] * c["cost_price"],
                "lost_margin": round(unmet * c["margin"]),
                "moq_cost": res["moq_over_cap"].get(c["base_name"]),
                "gap_days": c["gap_days"],
            })
            continue
        why = list(reasons.get(c["base_name"], []))
        if unmet > 0:
            by_cap = res["cap_units"].get(c["base_name"])
            why.append("capped_share" if (by_cap is not None and qty >= by_cap > 0)
                       else "capped_budget")
        lost_short += unmet * c["margin"]
        rate = c["rate_cover"]
        # «Хватит до» считаем ДО прихода темпом окна до прихода (тем же, каким
        # считается дыра), а после прихода — темпом окна покрытия. Раньше обе
        # даты брались из rate_cover, и полоса покрытия противоречила подписи
        # строки в 17 случаях из 19.
        rate_now = c.get("rate_lead") or rate
        days_now = int((c["cs"] + c["ordered"]) / rate_now) if rate_now > 0 else None
        days_after = int((c["proj_stock"] + qty) / rate) if rate > 0 else None
        items.append({
            "base_name": c["base_name"],
            "category": c["category"],
            "cls": c["cls"],
            "turnover": round(c["turnover"]),
            "rate": rate,
            "cs": c["cs"],
            "ordered": c["ordered"],
            "proj_stock": c["proj_stock"],
            "gap_days": c["gap_days"],
            "need": c["need"],
            "qty": qty,
            "unmet": unmet,
            "sizes": size_split(c["sizes"], qty),
            "cost_price": c["cost_price"],
            "avg_price": c["avg_price"],
            "cost_total": round(qty * c["cost_price"]),
            "pay_now": round(qty * c["cost_price"] * res["pay_share"]),
            # Маржа считается ТОЛЬКО по спросу: штуки сверх потребности
            # горизонта в нём не продадутся, и обещать по ним прибыль —
            # то же самое, что обещать её по неликвиду.
            "expected_profit": round(min(qty, c["need"]) * c["margin"]),
            "over_need_profit": round(max(0, qty - c["need"]) * c["margin"]),
            # Сколько дней разойдётся заказанная партия. Это защита от ловушки
            # «дорогая медленная позиция с высокой маржой»: она видна прямо
            # в строке, а не только в отсеве по минимальной партии.
            "days_to_sell": int(qty / rate) if rate > 0 else None,
            # Штуки сверх потребности (следствие минимальной партии) — деньги,
            # которые лягут в запас, а не отработают в горизонте заказа.
            "over_need": max(0, qty - c["need"]),
            "no_supplier": c.get("no_supplier", False),
            "runs_out": (today + timedelta(days=min(days_now, 3650))).isoformat()
            if days_now is not None else None,
            "covered_until": (eta + timedelta(days=min(days_after, 3650))).isoformat()
            if days_after is not None else None,
            "why": why,
            "why_text": "; ".join(
                REASON_TEXT.get(w, w) for w in sorted(
                    dict.fromkeys(why),
                    key=lambda r: REASON_ORDER.index(r) if r in REASON_ORDER else 99,
                )
            ),
        })

    cost_total = sum(i["cost_total"] for i in items)
    order_date = eta - timedelta(days=lead_days(stages))
    payments = payment_plan(order_date, stages, cost_total)
    # Единственный источник правды про «сколько платить сейчас» — первый транш
    # календаря. Раньше карточка считала его по pay_share и при «весь заказ
    # целиком» показывала «сейчас 299 699 · потом 0» рядом с календарём
    # 134 865 / 82 417 / 82 417.
    pay_now = payments[0]["amount"] if payments else cost_total
    pay_later = max(0, cost_total - pay_now)
    over_need_cost = sum(round(i["over_need"] * i["cost_price"]) for i in items)
    over_need_profit = sum(i["over_need_profit"] for i in items)
    # Что из недобора реально теряется, а что закроет следующий заказ: при ритме
    # 7 дней и окне 21 день до прихода следующего успевает потеряться примерно
    # треть. Показываем и то и другое, чтобы «упущено» не звало занимать деньги.
    cadence = int(brief.get("cadence_days") or cover)
    lost_share = min(1.0, cadence / cover) if cover > 0 else 1.0
    plan = {
        "today": snap["today"],
        "eta_date": brief["eta_date"],
        "order_date": (eta - timedelta(days=lead_days(stages))).isoformat(),
        "cover_days": cover,
        # «Спрос закрыт до» — по факту строк, а не арифметикой горизонта:
        # аудит показал, что обещанную дату не доживала НИ ОДНА строка плана.
        "covered_until": min((i["covered_until"] for i in items if i["covered_until"]),
                             default=None),
        "covered_until_target": (eta + timedelta(days=cover)).isoformat(),
        "covered_full": sum(
            1 for i in items
            if i["covered_until"] and i["covered_until"] >= (eta + timedelta(days=cover)).isoformat()
        ),
        "strategy": brief["strategy"],
        "strategy_title": STRATEGIES[brief["strategy"]]["title"],
        "budget": brief["budget"],
        "budget_scope": brief["budget_scope"],
        # По какой сумме бюджет сравнивался на самом деле. full_no_prepay —
        # канал ничего не берёт в день размещения, поэтому «деньги на сейчас»
        # нечем ограничивать и мы считаем по полной стоимости заказа.
        "budget_basis": res["budget_basis"],
        "budget_note": (
            "По этому каналу в день размещения заказа не платится ничего — "
            f"первый платёж {payments[0]['date'] if payments else '—'}. "
            "Поэтому бюджет сравниваем с полной стоимостью заказа, "
            "а не с деньгами «на сейчас»."
            if res["budget_basis"] == "full_no_prepay" else None
        ),
        "reserve_new": res["reserve"],
        "spent": round(res["spent"]),
        "rest": round(res["money"] - res["spent"]),
        "cost_total": cost_total,
        "pay_now": pay_now,
        "pay_later": pay_later,
        "over_need_cost": over_need_cost,
        "over_need_profit": over_need_profit,
        # Накладные и кратность — чтобы экран мог объяснить, откуда цифры.
        "overhead_pct": int(brief.get("overhead_pct", ctx.get("overhead_pct") or 0) or 0),
        "pack_multiple": int(ctx.get("pack_multiple") or 0),
        "no_supplier_count": sum(1 for i in items if i.get("no_supplier")),
        "stages": stage_schedule(order_date, stages),
        "payments": payments,
        "new_items": list(brief.get("new_items") or []),
        "new_items_cost": res["new_cost"],
        "new_items_over_budget": res["new_over_budget"],
        "reserve_unassigned": max(0, res["reserve"] - res["new_cost"]),
        "peaks": peak_hints(ctx.get("peak_periods"), today, lead_days(stages)),
        "coverage": coverage,
        "items": items,
        "not_included": not_included,
        # На обрезанной истории «упущенная выручка» посчитана только по тем
        # позициям, что дожили до расчёта: отсеянные раньше (stale, small_need)
        # в cands не попадают, поэтому 0 ₽ здесь означал бы «всё влезло» —
        # ровно противоположное правде. Честнее не число, а None.
        # Три разных числа вместо одного, которое смешивало всё:
        #   missing — маржа позиций, не вошедших в заказ совсем (это и есть
        #             то, что лежит в таблице «не влезло»);
        #   short   — недобор по позициям, которые в заказе;
        #   at_risk — та часть обоих, которую НЕ закроет следующий заказ.
        "lost": None if coverage["partial"] else {
            "missing": round(lost_missing),
            "short": round(lost_short),
            "at_risk": round((lost_missing + lost_short) * lost_share),
            "next_order_days": cadence,
        },
        "totals": {
            "positions": len(items),
            "units": sum(i["qty"] for i in items),
            # Себестоимость заказа в итогах: её читает история планов и
            # выгрузка — раньше приходилось складывать строки заново.
            "cost": cost_total,
            "expected_profit": sum(i["expected_profit"] for i in items),
            "expected_revenue": sum(i["qty"] * i["avg_price"] for i in items),
        },
        "moq_skipped": [
            {"base_name": b, "days": d} for b, d in
            sorted(res["moq_skipped"].items(), key=lambda kv: kv[1])[:LIST_CAP]
        ],
        # Партия физически дороже лимита доли на позицию — раньше она молча
        # его нарушала (80,6% бюджета при лимите 30%), теперь позиция уходит
        # сюда с ценой партии, и человек решает сам.
        "moq_over_cap": [
            {"base_name": b, "batch_cost": v, "limit": round(res["cap_per_item"])}
            for b, v in sorted(res["moq_over_cap"].items(), key=lambda kv: -kv[1])[:LIST_CAP]
        ],
        "review": {  # то, что система считать не берётся — решает человек
            "low_data": [_short(r) for r in skipped["low_data"]][:LIST_CAP],
            "no_cost": [_short(r) for r in skipped["no_cost"]][:LIST_CAP],
            "no_cost_count": len(skipped["no_cost"]),
            "low_data_count": len(skipped["low_data"]),
            "stale_count": len(skipped["stale"]),
            "small_need_count": len(skipped["small_need"]),
            "other_production_count": len(skipped["other_production"]),
            # Сколько позиций скрыто самим фактом недогруженной истории:
            # окно свежести сжалось до покрытия, и позиции без продаж внутри
            # него отсеяны как «мёртвые», хотя судить об этом не по чему.
            "hidden_by_coverage": len(skipped["stale"]) if ctx.get("fresh_clamped") else 0,
        },
        "categories": _by_category(items),
    }
    # Честная диагностика вместо пустого экрана. Аудит 22.08: одна и та же
    # фраза про минимальную партию показывалась в трёх РАЗНЫХ ситуациях, и в
    # самой частой (позиции не назначены на этот канал — состояние по умолчанию
    # у нового аккаунта со вторым производством) она была просто неверна.
    if not items:
        if brief["budget"] <= 0:
            plan["blocked"] = {"reason": "no_budget", "text":
                "Не указан бюджет — вернитесь на первый шаг и напишите, "
                "сколько денег готовы потратить на этот заказ."}
        elif res["reserve"] >= brief["budget"] and res["new_cost"] > 0:
            plan["blocked"] = {"reason": "reserve", "text":
                f"Новинки заняли весь бюджет ({fmt_rub(res['new_cost'])}), "
                f"на остальное не осталось ничего. Уменьшите количество новинок "
                f"или поднимите бюджет."}
        elif skipped["other_production"] and not cands:
            plan["blocked"] = {"reason": "no_assignment", "count":
                len(skipped["other_production"]), "text":
                f"На это производство не назначена ни одна позиция — все "
                f"{len(skipped['other_production'])} закреплены за другими. "
                f"Настройте распределение в разделе «Настройки» или перенесите "
                f"позиции кнопками на странице «Заказ»."}
        elif res["moq_skipped"] or res["moq_over_cap"]:
            days = min(res["moq_skipped"].values()) if res["moq_skipped"] else None
            reach = min(analytics.COVER_MAX_DAYS, days) if days else None
            tail = (f"Либо снизьте минимальную партию, либо заказывайте этот канал "
                    f"реже: чтобы партия окупалась, между заказами нужно около "
                    f"{days} дн.") if days and days > analytics.COVER_MAX_DAYS else (
                    f"Либо снизьте минимальную партию, либо увеличьте интервал "
                    f"между заказами до {reach} дн." if reach else
                    "Партия дороже лимита на позицию — поднимите лимит или бюджет.")
            plan["blocked"] = {
                "reason": "moq",
                "count": len(res["moq_skipped"]) + len(res["moq_over_cap"]),
                "suggest_cover_days": days,
                "text": (f"Ни одна позиция не набирает минимальную партию: при "
                         f"горизонте {cover} дн такая партия — запас на {days} дн "
                         f"и дольше. {tail}") if days else
                        (f"Ни одна позиция не проходит: минимальная партия дороже "
                         f"лимита на позицию ({fmt_rub(res['cap_per_item'])}). {tail}"),
            }
        else:
            plan["blocked"] = {"reason": "no_candidates", "text":
                "Ни одна позиция не прошла отбор: нет продаж за последние "
                f"{ctx.get('fresh_days', FRESH_DAYS)} дн, либо потребность меньше "
                f"{NEED_MIN} шт, либо не заполнена себестоимость. "
                "Раскройте блок «Что система считать не берётся» — там видно, кто именно."}
    # Что мешает нажать «Создать заказ». Блокируем ТОЛЬКО ошибки, а не риски:
    # предупреждать можно о многом, но не пускать — лишь там, где заказ заведомо
    # неверен. Неполная себестоимость сюда осознанно НЕ входит: у маленького
    # бренда её нет почти нигде, и блокировка не дала бы ему сделать ни одного
    # заказа.
    stop = []
    if not items and not plan.get("new_items"):
        stop.append({"code": "empty", "text": "В плане нет позиций"})
    if plan["rest"] < 0:
        stop.append({"code": "over_budget", "text":
                     f"Заказ выходит за бюджет на {fmt_rub(-plan['rest'])}"})
    if plan["order_date"] < snap["today"]:
        stop.append({"code": "past_date", "text":
                     f"Заказ пришлось бы разместить {plan['order_date']} — "
                     f"эта дата уже прошла. Сдвиньте дату приёмки."})
    plan["stop"] = stop
    plan["can_create"] = not stop
    if coverage["partial"]:
        # Пометка для UI и для api_order_plan_apply: план предварительный —
        # not_included неполон, «а если добавить денег» на обрезке истории
        # ответит «ничего не изменится», что неправда. Лучше не отвечать.
        plan["provisional"] = True
    elif with_sensitivity and brief["budget"] > 0:
        plan["sensitivity"] = _sensitivity(snap, brief, ctx, stages, plan)
    return plan


def fmt_rub(value) -> str:
    """Число рублей строкой с разделителями — для сообщений пользователю."""
    return f"{round(float(value or 0)):,} ₽".replace(",", " ")


def _short(row: dict) -> dict:
    return {
        "base_name": row["base_name"], "category": row["category"],
        "need": row["need"], "turnover": round(row["turnover"]),
        "cost_price": row["cost_price"],
        # Остаток нужен на экране рядом с полем «сколько заказать вручную»
        # (D-23): человек решает за систему, и без сегодняшнего остатка
        # решать не из чего.
        "cs": int(row.get("cs") or 0),
    }


def _by_category(items: list[dict]) -> list[dict]:
    agg: dict[str, dict] = {}
    for i in items:
        rec = agg.setdefault(i["category"], {"category": i["category"], "cost": 0, "units": 0, "positions": 0})
        rec["cost"] += i["cost_total"]
        rec["units"] += i["qty"]
        rec["positions"] += 1
    return sorted(agg.values(), key=lambda r: -r["cost"])


def _sensitivity(snap: dict, brief: dict, ctx: dict, stages: list[dict], base_plan: dict) -> list[dict]:
    """«А если добавить денег»: что войдёт при бюджете +25% и +50%."""
    have = {i["base_name"] for i in base_plan["items"]}
    out = []
    for k in SENSITIVITY_STEPS:
        b = dict(brief)
        b["budget"] = int(brief["budget"] * k)
        alt = plan_order(snap, b, ctx, stages, with_sensitivity=False)
        added = [i["base_name"] for i in alt["items"] if i["base_name"] not in have]
        out.append({
            "extra_budget": b["budget"] - brief["budget"],
            "budget": b["budget"],
            "positions": alt["totals"]["positions"],
            "units": alt["totals"]["units"],
            "added": added[:10],
            "added_count": len(added),
            "extra_profit": alt["totals"]["expected_profit"] - base_plan["totals"]["expected_profit"],
        })
    return out


# ── Сборка «под ключ» для API ────────────────────────────────────────────────

def build_plan(db: Session, org: Org, snap: dict, raw_brief: dict) -> dict:
    """Полный путь: производство → бриф → контекст → план."""
    settings = snap["settings"]
    prod = None
    pid = (raw_brief or {}).get("production_id")
    if pid is not None:
        prod = db.get(Production, int(pid))
        if prod is None or prod.org_id != org.id:
            prod = None
    stages = normalize_stages(
        prod.stages if prod is not None else None, settings.get("lead_time_days")
    )
    today = date.fromisoformat(snap["today"])
    # Ритм заказов — свойство канала: своё производство можно догружать
    # еженедельно, Китай заказывается раз в сезон. 0 = общая настройка org.
    prod_settings = dict(settings)
    if prod is not None and prod.cadence_days:
        prod_settings["order_cadence_days"] = int(prod.cadence_days)
    brief = normalize_brief(raw_brief, prod_settings, stages, today)
    if prod is not None:
        brief["production_id"] = prod.id
        if not brief.get("moq_explicit") and prod.moq_units:
            brief["moq_units"] = int(prod.moq_units)
    else:
        # Производство не наше (или его нет). Расчёт его уже игнорирует, но
        # ЧУЖОЙ id нельзя оставлять в брифе: бриф сохраняется в order_plans, а
        # оформление плана переносит production_id в заказ — и календарь
        # платежей начинает читать этапы чужой организации (сроки, доли
        # себестоимости, размеры предоплат). Затираем здесь, у источника.
        brief["production_id"] = None
    ctx = collect_context(db, org, snap, brief)
    plan = plan_order(snap, brief, ctx, stages)
    plan["brief"] = brief
    plan["production"] = (
        {"id": prod.id, "name": prod.name, "is_main": prod.is_main} if prod else None
    )
    plan["lead_days"] = lead_days(stages)
    return plan
