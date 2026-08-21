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
# Когда потребность меньше минимальной партии, вопрос не «дотягивает ли она»,
# а «на сколько дней растянется эта партия». 50 штук при продаже 1,7 шт/день —
# это месяц запаса, нормально; те же 50 штук при 0,2 шт/день — восемь месяцев
# мёртвых денег. Поэтому критерий — срок распродажи партии, а не доля need.
MOQ_MAX_COVER_DAYS = 120  # партия дольше этого срока не окупает минимум
BASE_WAVE_DAYS = 14     # волна «База» без MOQ: минимум — покрытие двух недель
SENSITIVITY_STEPS = (1.25, 1.5)  # «а если добавить денег»

# Профили стратегии: чем отличается «не потерять продажи» от «заработать максимум».
STRATEGIES = {
    "protect": {
        "title": "Не потерять продажи",
        "max_share_pct": 20,
        "base_classes": None,                    # база — всем кандидатам
        "base_days": 21,                         # стартовая партия = 3 недели продаж
        "deepen_factor": 1.0,
    },
    "balance": {
        "title": "Баланс",
        "max_share_pct": 30,
        "base_classes": ("best", "good", "dull"),
        "base_days": 14,
        "deepen_factor": 1.0,
    },
    "grow": {
        "title": "Заработать максимум",
        "max_share_pct": 60,
        "base_classes": ("best", "good"),
        "base_days": 7,                          # почти без «размазывания» — сразу вглубь
        "deepen_factor": 1.2,                    # топам можно взять с запасом
    },
}

# Порядок причин в подписи строки: сначала зачем позиция в заказе, потом
# что ограничило количество — так фраза читается как объяснение, а не как лог.
REASON_ORDER = ("must_have", "gap", "base", "deepen", "moq", "moq_over_limit",
                "capped_share", "capped_budget")

REASON_TEXT = {
    "gap": "кончится до прихода заказа",
    "base": "стартовая партия",
    "deepen": "добор до потребности",
    "must_have": "включено вручную",
    "capped_share": "срезано лимитом на позицию",
    "capped_budget": "дальше деньги закончились",
    "moq": "округлено до минимальной партии",
    "moq_over_limit": "партия больше лимита на позицию",
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
        "strategy": strategy,
        "max_share_pct": _int("max_share_pct", profile["max_share_pct"], 5, 100),
        "moq_units": _int("moq_units", settings.get("moq_units", 0), 0, 10000),
        "reserve_new_pct": _int("reserve_new_pct", settings.get("reserve_new_pct", 0), 0, 90),
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
    """Темп продаж на будущее окно по статистике того же окна ГОД НАЗАД.

    Каскад фолбэков (в одежде половина моделей моложе года):
      1) окно самой позиции, если в нём набралось MIN_WINDOW_DIS дней наличия;
      2) годовой темп позиции × сезонный индекс её КАТЕГОРИИ на это окно;
      3) годовой темп позиции.

    Возвращает (темпы по базам, сезонные индексы категорий). Индексы нужны
    вызывающему, чтобы честно сказать: сезонность в расчёте участвовала или
    её не из чего было взять (у нового аккаунта окна «год назад» ещё нет).
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
        rate_year = float(it.get("rate_year") or 0)
        dis_b = float(dis_w.get(base) or 0)
        nq_b = float(nq_w.get(base) or 0)
        if dis_b >= MIN_WINDOW_DIS:
            rates[base] = max(0.0, nq_b / dis_b)
        else:
            idx = cat_index.get(it.get("category") or "Без категории")
            rates[base] = rate_year * idx if idx else rate_year
    return rates, cat_index


def collect_context(db: Session, org: Org, snap: dict, brief: dict) -> dict:
    """Всё, что планировщику нужно из БД: сезонные темпы, свежесть, привязка к производству."""
    from app.analytics_extra import _fresh_bases

    today = date.fromisoformat(snap["today"])
    eta = date.fromisoformat(brief["eta_date"])
    cover = max(
        analytics.COVER_MIN_DAYS,
        min(analytics.COVER_MAX_DAYS, brief["cadence_days"] + brief["safety_days"]),
    )
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
    return {
        "cover_days": cover,
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
        price = float(it.get("avg_price") or it.get("sale_price") or 0)
        row = {
            "base_name": base,
            "category": category,
            "cls": it.get("cls"),
            "low_data": bool(it.get("low_data")),
            "turnover": float(it.get("turnover") or 0),
            "rate_cover": round(r_cover, 4),
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
    profile = STRATEGIES[brief["strategy"]]
    brief_moq = int(brief["moq_units"] or 0)
    cover = ctx["cover_days"]

    def moq_of(c: dict) -> int:
        """Минимальная партия позиции: максимум из общей и минимумов этапов
        (у этапа могут быть свои минимумы по категориям — см. stage_moq)."""
        return max(brief_moq, stage_moq(stages, c["category"]))

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

    alloc: dict[str, int] = {}
    reasons: dict[str, list[str]] = {}
    capped: dict[str, str] = {}   # почему позиция не добрана до потребности
    moq_skipped: dict[str, int] = {}  # позиция → на сколько дней хватило бы партии
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
        limit = cap_per_item if (cap is not None and cap < 0) else cap
        if limit is not None and limit > 0:
            by_cap = int(math.floor(limit / unit))
            if target > by_cap:
                target = by_cap
                if limit == cap_per_item:
                    capped[base] = "capped_share"
        if target <= have:
            return
        rest = money - spent
        afford = int(math.floor(rest / unit))
        add = min(target - have, afford)
        if add < target - have:
            capped.setdefault(base, "capped_budget")
        if add <= 0:
            # Минимальная партия целиком не влезла — позиция остаётся без заказа.
            return
        qty = have + add
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
            if (moq - have) * unit > rest:
                return  # партия не по карману
            qty = moq
            reasons.setdefault(base, []).append("moq")
            if limit is not None and limit > 0 and qty * unit > limit + 1:
                # Партия физически больше лимита доли — берём, но говорим об этом.
                reasons.setdefault(base, []).append("moq_over_limit")
        spent += (qty - have) * unit
        alloc[base] = qty
        reasons.setdefault(base, []).append(reason)

    allowed = profile["base_classes"]

    def in_focus(c: dict) -> bool:
        """Позиция в фокусе выбранной стратегии (protect — все классы)."""
        return allowed is None or c["cls"] in allowed

    def base_batch(c: dict) -> int:
        """Минимально осмысленная партия: MOQ или столько-то дней продаж.

        Сколько дней — задаёт стратегия: «не потерять продажи» даёт стартовую
        партию шире (3 недели каждому), «заработать максимум» почти не
        размазывает (неделя) и сразу уходит вглубь топа.
        """
        moq = moq_of(c)
        if moq > 0:
            return min(c["need"], moq)
        days = profile.get("base_days", BASE_WAVE_DAYS)
        return min(c["need"], max(1, int(math.ceil(c["rate_cover"] * days))))

    # + must-have: решение владельца выше алгоритма
    for c in cands:
        if c["must_have"]:
            take(c, c["need"], "must_have", cap=None)
    # Потолок стартовых волн: базовая партия по определению не должна съедать
    # заметную долю бюджета — иначе три первые позиции забирают всё, и волна
    # «База» перестаёт делать то, ради чего она есть.
    focus_n = max(1, sum(1 for c in cands if in_focus(c)))
    # Лимит доли на позицию — обещание пользователю, его стартовая волна
    # нарушать не имеет права: берём наиболее строгий из двух потолков.
    base_cap = min(money / focus_n, cap_per_item) if money > 0 else 0.0
    # 0. Дыры — позиции, которые кончатся до прихода: сначала останавливаем
    #    потери (базовая партия), глубину доберём волной 2.
    for c in cands:
        if c["gap_days"] > 0 and in_focus(c):
            take(c, base_batch(c), "gap", cap=base_cap)
    # 1. База — ассортимент не схлопывается в три модели
    for c in cands:
        if in_focus(c):
            take(c, base_batch(c), "base", cap=base_cap)
    # 2. Углубление — деньги туда, где быстрее оборот
    for c in cands:
        if in_focus(c):
            take(c, int(round(c["need"] * profile["deepen_factor"])), "deepen")
    # 3. Остаток — если после фокуса стратегии деньги ещё есть, пускаем их
    #    в остальные позиции по оборачиваемости (бюджет не должен «зависать»).
    for c in cands:
        if not in_focus(c):
            take(c, c["need"], "deepen")

    return {
        "alloc": alloc, "reasons": reasons, "capped": capped,
        "moq_skipped": moq_skipped, "spent": spent,
        "reserve": reserve, "new_cost": new_cost, "new_over_budget": over_budget,
        "money": money, "pay_share": pay_share,
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
    needed = lead_days(stages) + int(ctx["cover_days"])
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
    lost_revenue = 0.0
    for c in cands:
        qty = alloc.get(c["base_name"], 0)
        unmet = max(0, c["need"] - qty)
        if qty <= 0:
            lost_revenue += unmet * c["avg_price"]
            not_included.append({
                "base_name": c["base_name"], "category": c["category"], "cls": c["cls"],
                "turnover": round(c["turnover"]), "need": c["need"],
                "cost_price": c["cost_price"], "need_rub": c["need"] * c["cost_price"],
                "lost_revenue": round(unmet * c["avg_price"]),
                "gap_days": c["gap_days"],
            })
            continue
        why = list(reasons.get(c["base_name"], []))
        if unmet > 0:
            why.append(res["capped"].get(c["base_name"], "capped_budget"))
        # «партия больше лимита» и «срезано лимитом» вместе читаются как
        # противоречие — оставляем первую, она объясняет и то и другое.
        if "moq_over_limit" in why and "capped_share" in why:
            why.remove("capped_share")
        lost_revenue += unmet * c["avg_price"]
        rate = c["rate_cover"]
        # «Хватит до» — до и после заказа (то же в UI показывается полосами).
        days_now = int((c["cs"] + c["ordered"]) / rate) if rate > 0 else None
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
            "expected_profit": round(qty * c["margin"]),
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
    pay_now = sum(i["pay_now"] for i in items)
    plan = {
        "today": snap["today"],
        "eta_date": brief["eta_date"],
        "order_date": (eta - timedelta(days=lead_days(stages))).isoformat(),
        "cover_days": cover,
        "covered_until": (eta + timedelta(days=cover)).isoformat(),
        "strategy": brief["strategy"],
        "strategy_title": STRATEGIES[brief["strategy"]]["title"],
        "budget": brief["budget"],
        "budget_scope": brief["budget_scope"],
        "reserve_new": res["reserve"],
        "spent": round(res["spent"]),
        "rest": round(res["money"] - res["spent"]),
        "cost_total": cost_total,
        "pay_now": pay_now,
        "pay_later": max(0, cost_total - pay_now),
        "stages": stage_schedule(eta - timedelta(days=lead_days(stages)), stages),
        "payments": payment_plan(eta - timedelta(days=lead_days(stages)), stages, cost_total),
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
        "lost_revenue": None if coverage["partial"] else round(lost_revenue),
        "totals": {
            "positions": len(items),
            "units": sum(i["qty"] for i in items),
            "expected_profit": sum(i["expected_profit"] for i in items),
            "expected_revenue": sum(i["qty"] * i["avg_price"] for i in items),
        },
        "moq_skipped": [
            {"base_name": b, "days": d} for b, d in
            sorted(res["moq_skipped"].items(), key=lambda kv: kv[1])[:50]
        ],
        "review": {  # то, что система считать не берётся — решает человек
            "low_data": [_short(r) for r in skipped["low_data"]][:50],
            "no_cost": [_short(r) for r in skipped["no_cost"]][:50],
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
    # Честная диагностика вместо пустого экрана: минимальная партия может не
    # сходиться с ритмом заказов (партия на 50 шт при заказах раз в неделю —
    # это запас на месяцы). Тогда прямо говорим, что упёрлись именно в неё.
    if not items and res["moq_skipped"]:
        need_days = min(res["moq_skipped"].values())
        plan["blocked"] = {
            "reason": "moq",
            "count": len(res["moq_skipped"]),
            "suggest_cover_days": need_days,
            "text": (
                f"Ни одна позиция не набирает минимальную партию: при нынешнем "
                f"горизонте {cover} дн такая партия — запас на {need_days} дн и "
                f"дольше. Либо увеличьте интервал между заказами, либо снизьте "
                f"минимальную партию, либо закажите этот канал реже."
            ),
        }
    if coverage["partial"]:
        # Пометка для UI и для api_order_plan_apply: план предварительный —
        # not_included неполон, «а если добавить денег» на обрезке истории
        # ответит «ничего не изменится», что неправда. Лучше не отвечать.
        plan["provisional"] = True
    elif with_sensitivity and brief["budget"] > 0:
        plan["sensitivity"] = _sensitivity(snap, brief, ctx, stages, plan)
    return plan


def _short(row: dict) -> dict:
    return {
        "base_name": row["base_name"], "category": row["category"],
        "need": row["need"], "turnover": round(row["turnover"]),
        "cost_price": row["cost_price"],
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
        if not (raw_brief or {}).get("moq_units") and prod.moq_units:
            brief["moq_units"] = int(prod.moq_units)
    ctx = collect_context(db, org, snap, brief)
    plan = plan_order(snap, brief, ctx, stages)
    plan["brief"] = brief
    plan["production"] = (
        {"id": prod.id, "name": prod.name, "is_main": prod.is_main} if prod else None
    )
    plan["lead_days"] = lead_days(stages)
    return plan
