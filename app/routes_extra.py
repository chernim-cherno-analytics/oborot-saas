"""Роуты портированной аналитики: Бюджет (OTB), Прогноз, Размеры.

Страницы (/budget, /forecast, /sizes) — тонкие Jinja2-шаблоны, вся математика
в app.analytics_extra; JSON API — под require_auth_api, данные только своей org.
Паттерн страниц повторяет app.main._authed_page (main импортирует этот модуль,
поэтому импортировать main отсюда нельзя — свой экземпляр Jinja2Templates).
"""
from datetime import date, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Mapped, Session, mapped_column

from pydantic import BaseModel, Field
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, delete, select

import json
import os

from app import analytics, analytics_extra, analytics_markdown, auth
from app.api import _require_known_base, apply_production_rules, production_conditions
from app.auth import AuthContext, require_auth_api, require_owner_api
from app.db import Base, get_db
from app.models import (
    CategoryMerge,
    Connection,
    Product,
    ReplenishDraft,
    SkuCategoryOverride,
    SkuDiscount,
    SkuHidden,
)

router = APIRouter()

_BASE_DIR = Path(__file__).resolve().parent.parent
# Тот же порядок, что в main.py: настоящие шаблоны перекрывают заглушки.
_templates = Jinja2Templates(
    directory=[str(_BASE_DIR / "templates"), str(_BASE_DIR / "_stub_templates")]
)


def _authed_page(request: Request, db: Session, template: str, active: str, page_title: str,
                 extra: dict | None = None):
    """Страница под сессией: без авторизации — redirect /login (как в main.py).

    extra — дополнительные переменные шаблона (нужны «Обучению»: контакт
    поддержки берётся из env и в общий контекст страниц не входит).
    """
    ctx = auth.resolve_auth(request, db)
    if ctx is None:
        return RedirectResponse("/login", status_code=302)
    org = ctx.org
    return _templates.TemplateResponse(
        request,
        template,
        {
            "user": {"name": ctx.user.name, "email": ctx.user.email},
            "org": {
                "name": org.name,
                "plan": org.plan,
                "trial_ends_at": org.trial_ends_at.date().isoformat() if org.trial_ends_at else None,
            },
            "active": active,
            "page_title": page_title,
            **(extra or {}),
        },
    )


# ── Страницы ─────────────────────────────────────────────────────────────────

# Скрытые разделы: продукт сфокусирован на Оборачиваемости, Активном стоке и
# заказах. Код страниц сохранён в репозитории — чтобы вернуть раздел, убери его
# из этого множества (роуты и API снова откроются, пункт меню — в base.html).
# «Скидки» скрыты (код сохранён); «Заказы» убраны как страница — заказы
# создаются и управляются прямо на странице «Заказ» (блок «в производстве»).
HIDDEN_PAGES = frozenset({"discounts", "orders"})


def _hidden_404(active: str):
    if active in HIDDEN_PAGES:
        raise HTTPException(status_code=404, detail="Раздел отключён")


@router.get("/assistant", response_class=HTMLResponse)
def assistant_page(request: Request, db: Session = Depends(get_db)):
    """«Мастер заказа»: анкета → план под бюджет → заказ на производство."""
    return _authed_page(request, db, "assistant.html", "assistant", "Мастер заказа")


@router.get("/budget", response_class=HTMLResponse)
def budget_page(request: Request, db: Session = Depends(get_db)):
    return _authed_page(request, db, "budget.html", "budget", "Бюджет закупки")


@router.get("/lessons", response_class=HTMLResponse)
def lessons_page(request: Request, db: Session = Depends(get_db)):
    """«Обучение»: пять коротких уроков по страницам, прогресс, FAQ.

    Контакт поддержки — из окружения (OBOROT_SUPPORT_URL — ссылка, иначе
    OBOROT_SUPPORT_EMAIL). Не задан ни один — блок рисуется без кнопки:
    выдумывать адрес нельзя.
    """
    return _authed_page(
        request, db, "lessons.html", "lessons", "Обучение",
        extra={
            "support_url": (os.environ.get("OBOROT_SUPPORT_URL") or "").strip(),
            "support_email": (os.environ.get("OBOROT_SUPPORT_EMAIL") or "").strip(),
        },
    )


@router.get("/forecast", response_class=HTMLResponse)
def forecast_page(request: Request, db: Session = Depends(get_db)):
    return _authed_page(request, db, "forecast.html", "forecast", "Прогноз")


@router.get("/sizes", response_class=HTMLResponse)
def sizes_page(request: Request, db: Session = Depends(get_db)):
    return _authed_page(request, db, "sizes.html", "sizes", "Заказ отдельной позиции")


@router.get("/discounts", response_class=HTMLResponse)
def discounts_page(request: Request, db: Session = Depends(get_db)):
    _hidden_404("discounts")
    return _authed_page(request, db, "discounts.html", "discounts", "Скидки")


@router.get("/revenue", response_class=HTMLResponse)
def revenue_page(request: Request, db: Session = Depends(get_db)):
    return _authed_page(request, db, "revenue.html", "revenue", "Оборот")


# ── Юридические страницы (публичные, без авторизации) ────────────────────────
# Тексты — рабочая редакция; перед публичным запуском вычитывает юрист.

@router.get("/legal/offer", response_class=HTMLResponse)
def legal_offer(request: Request):
    return _templates.TemplateResponse(request, "legal_offer.html", {})


@router.get("/legal/privacy", response_class=HTMLResponse)
def legal_privacy(request: Request):
    return _templates.TemplateResponse(request, "legal_privacy.html", {})


# ── JSON API ─────────────────────────────────────────────────────────────────

@router.get("/api/budget")
def api_budget(
    amount: int = Query(200_000, ge=1, le=1_000_000_000),
    max_share: int = Query(30, ge=5, le=100),
    exclude_cats: str = Query(""),
    ctx: AuthContext = Depends(require_auth_api),
    db: Session = Depends(get_db),
):
    """Бюджет закупки: жадное распределение по оборачиваемости (см. analytics_extra)."""
    snap = analytics.get_snapshot(db, ctx.org)
    excluded = {c.strip() for c in exclude_cats.split(",") if c.strip()}
    return analytics_extra.build_budget(db, ctx.org.id, snap, amount, max_share, excluded)


@router.get("/api/forecast")
def api_forecast(ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)):
    """Прогноз распродажи стока: карточки, ряд 26 недель, категории, позиции."""
    snap = analytics.get_snapshot(db, ctx.org)
    return analytics_extra.build_forecast(snap)


def _discount_overrides(db: Session, org_id: int) -> dict[str, float]:
    return dict(
        db.execute(
            select(SkuDiscount.base_name, SkuDiscount.discount).where(
                SkuDiscount.org_id == org_id, SkuDiscount.discount > 0
            )
        ).all()
    )


@router.get("/api/discounts")
def api_discounts(ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)):
    _hidden_404("discounts")
    """Markdown-рекомендации: что уценить и на сколько (см. analytics_markdown).

    Ручные скидки со страницы «Оборачиваемость» имеют приоритет.
    """
    snap = analytics.get_snapshot(db, ctx.org)
    return analytics_markdown.build_discounts(snap, _discount_overrides(db, ctx.org.id))


# ── Ручные скидки (колонка «Скидка %» на /turnover, правило legacy) ──────────

class DiscountIn(BaseModel):
    base_name: str = Field(min_length=1, max_length=255)
    discount: float = Field(ge=0, le=99)


@router.get("/api/discount-overrides")
def api_discount_overrides(
    ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)
):
    """{base_name: скидка %} — ручные скидки организации (>0)."""
    return _discount_overrides(db, ctx.org.id)


@router.post("/api/discount-overrides")
def api_set_discount_override(
    body: DiscountIn,
    ctx: AuthContext = Depends(require_auth_api),
    db: Session = Depends(get_db),
):
    """Установить/снять ручную скидку позиции (0 = снять).

    Несуществующий base_name раньше молча отвечал {"ok": true} и оседал
    фантомной строкой (или, с пробелами по краям, дублем рядом с настоящей
    позицией) — тот же дефект, что чинили у /api/replenish-draft. Сверяем
    с каталогом ДО записи, как app.api._require_known_base.
    """
    base_name = _require_known_base(db, ctx.org.id, body.base_name)
    row = db.get(SkuDiscount, (ctx.org.id, base_name))
    if body.discount <= 0:
        if row is not None:
            db.delete(row)
    elif row is None:
        db.add(SkuDiscount(org_id=ctx.org.id, base_name=base_name,
                           discount=round(body.discount)))
    else:
        row.discount = round(body.discount)
    db.commit()
    return {"ok": True, "base_name": base_name, "discount": round(body.discount)}


@router.post("/api/discount-overrides/defaults")
def api_apply_default_discounts(
    ctx: AuthContext = Depends(require_owner_api), db: Session = Depends(get_db)
):
    """Кнопка «Дефолтные скидки» (правило legacy, только владелец).

    Сбрасывает ВСЕ ручные скидки организации и расставляет заново по правилу
    от текущей оборачиваемости и запаса (analytics_markdown._recommend).
    """
    snap = analytics.get_snapshot(db, ctx.org)
    defaults = analytics_markdown.default_discounts(snap)
    db.execute(delete(SkuDiscount).where(SkuDiscount.org_id == ctx.org.id))
    for base, pct in defaults.items():
        db.add(SkuDiscount(org_id=ctx.org.id, base_name=base, discount=float(pct)))
    db.commit()
    return {"ok": True, "count": len(defaults)}


@router.get("/api/revenue")
def api_revenue(
    date_from: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    ctx: AuthContext = Depends(require_auth_api),
    db: Session = Depends(get_db),
):
    """«Оборот» за период: выручка, категории, помесячный ряд, топ позиций."""
    if date_from > date_to:
        raise HTTPException(status_code=422, detail="Дата начала позже даты конца")
    return analytics_extra.build_revenue(db, ctx.org.id, date_from, date_to)


@router.get("/api/pulse")
def api_pulse(ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)):
    """«Пульс»: этот месяц и текущий склад против среднего за 6 полных месяцев."""
    return analytics_extra.build_pulse(db, ctx.org.id)


@router.get("/api/freshness")
def api_freshness(ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)):
    """Табло свежести данных: до какого дня загружены продажи и остатки
    и чем закончился последний синк. Питает баннер на всех страницах."""
    from sqlalchemy import func as _f

    from app import ms_sync
    from app.models import Sale as _Sale, StockDay as _SD

    last_sale = db.execute(
        select(_f.max(_Sale.date)).where(_Sale.org_id == ctx.org.id)
    ).scalar()
    last_stock = db.execute(
        select(_f.max(_SD.date)).where(_SD.org_id == ctx.org.id)
    ).scalar()
    first_stock = db.execute(
        select(_f.min(_SD.date)).where(_SD.org_id == ctx.org.id)
    ).scalar()
    st = ms_sync.get_status(ctx.org.id)
    # Явный признак «источник подключён» (демо тоже считается подключением).
    # Без него страница не отличает «данные отстают» от «человек ещё ничего
    # не подключал» и пугает свежезарегистрированного владельца зря.
    connected = db.execute(
        select(Connection.id).where(
            Connection.org_id == ctx.org.id, Connection.status == "active"
        )
    ).first() is not None
    return {
        "connected": connected,
        "last_sale_date": last_sale,
        "last_stock_date": last_stock,
        "sync_state": st.get("state"),
        "sync_error": st.get("error"),
        "sync_finished_at": st.get("finished_at"),
        # Деплой П1: сколько дней истории уже на диске (прогрессивная загрузка)
        # и с какой даты она начинается — фронт подписывает колонки «за N дн.»
        # и гасит сезоны, которые ещё не загружены.
        "coverage_days": st.get("coverage_days", 0),
        "coverage_start": first_stock,
        "history_days": ms_sync.HISTORY_DAYS,
    }


@router.get("/api/sync/progress")
def api_sync_progress(ctx: AuthContext = Depends(require_auth_api)):
    """Прогресс загрузки данных для полоски под шапкой (деплой П1).

    Доступен ЛЮБОМУ участнику организации (в отличие от owner-only
    /api/sync/status) и не раскрывает внутренние stats: только состояние,
    фазу, покрытие истории, месяцы, этапы и оценку остатка.
    """
    from app import ms_sync

    out = ms_sync.get_progress(ctx.org.id)
    out["can_manage"] = ctx.role == "owner"  # кнопки «Повторить»/«Исправить» — владельцу
    return out


@router.get("/api/sizes/products")
def api_sizes_products(ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)):
    """Список позиций для поиска на странице «Размеры»."""
    snap = analytics.get_snapshot(db, ctx.org)
    return analytics_extra.sizes_products(db, ctx.org.id, snap)


@router.get("/api/sizes/calc")
def api_sizes_calc(
    product: str = Query(..., min_length=1),
    qty: int = Query(30, ge=0, le=1_000_000),
    period: str = Query("12m"),
    mode: str = Query("stock"),
    arrival: str = Query("", pattern=r"^(\d{4}-\d{2}-\d{2})?$"),
    ctx: AuthContext = Depends(require_auth_api),
    db: Session = Depends(get_db),
):
    """Распределение заказа позиции по размерам: темпы по дням наличия,
    остатки прогнозируются на дату прихода (arrival, иначе today + срок ТОГО
    производства, за которым закреплена позиция — тот же срок, что и на
    «Заказе», а не общий срок из настроек: раньше это чинилось вторым
    запросом из браузера, сервер отдавал неверную дату).

    За период «12 мес» (дефолт страницы) режим «чистая пропорция» отдаёт ту
    же раскладку, что и «Заказ» — через ту же analytics.size_split от тех
    же годовых продаж по размерам, а не через свой пересчёт по календарным
    месяцам: на одинаковом количестве страницы не должны расходиться.
    Режим «с учётом остатков» это НЕ дублирует и специально: он единственный
    учитывает остаток по каждому размеру отдельно (на «Заказе» такого нет —
    там весь остаток закрывается ДО расчёта, раскладка чисто по весам), так
    что расхождение с «Заказом» здесь ожидаемо и осмысленно.
    """
    snap = analytics.get_snapshot(db, ctx.org)
    item = snap["items"].get(product)
    default_lead = analytics.extra_settings(ctx.org)["lead_time_days"]
    cond = production_conditions(db, ctx.org.id)
    prod = cond["by_id"].get(cond["assign"].get(product, cond["main_id"]))
    lead = int(getattr(prod, "lead_time_days", 0) or 0) or default_lead
    result = analytics_extra.build_sizes_calc(
        db, ctx.org.id, snap, product, qty, period, mode,
        arrival=arrival or None, lead_time_days=lead,
    )
    if result.get("error"):
        raise HTTPException(status_code=422, detail=result["error"])
    result["lead_time_days"] = lead
    result["production_id"] = prod.id if prod else None
    result["production_name"] = prod.name if prod else ""

    result["split_matches_order_page"] = False
    if period == "12m" and qty > 0:
        raw_sizes = (item or {}).get("sizes") or {}
        common_sizes: dict[str, dict] = {}
        for sz, rec in raw_sizes.items():
            key = analytics_extra._norm_size(sz)
            bucket = common_sizes.setdefault(key, {"stock": 0, "sold365": 0})
            bucket["stock"] += int(rec.get("stock") or 0)
            bucket["sold365"] += float(rec.get("sold365") or 0)
        common_split = analytics.size_split(common_sizes, qty) if common_sizes else {}
        if common_split:
            for row in result["sizes"]:
                row["order_pure"] = common_split.get(row["size"], 0)
            result["split_matches_order_page"] = True
    return result


# ── «Активный сток» + архив + категории + правило скидок (порт legacy) ───────

@router.get("/api/active-stock")
def api_active_stock(ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)):
    """Страница «Активный сток»: классы, склады, размеры, «Заказано», сигналы."""
    snap = analytics.get_snapshot(db, ctx.org)
    return analytics.build_active_stock(snap)


class HiddenIn(BaseModel):
    base_name: str = Field(min_length=1, max_length=255)
    hidden: bool


@router.get("/api/hidden")
def api_hidden(ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)):
    """Список позиций в архиве «Оборота» (кнопка «в архив» на Оборачиваемости)."""
    rows = db.execute(
        select(SkuHidden.base_name).where(SkuHidden.org_id == ctx.org.id)
    ).scalars().all()
    return {"hidden": rows}


@router.post("/api/hidden")
def api_set_hidden(
    body: HiddenIn, ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)
):
    """Убрать в архив / вернуть из архива.

    Несуществующий base_name раньше молча отвечал {"ok": true}, а запись
    оседала фантомом (или, с пробелами по краям, второй строкой рядом с
    настоящей позицией) — человек не видел эффекта и решал, что архив
    сломан. Сверяем с каталогом ДО записи, как app.api._require_known_base.
    """
    base_name = _require_known_base(db, ctx.org.id, body.base_name)
    row = db.get(SkuHidden, (ctx.org.id, base_name))
    if body.hidden and row is None:
        db.add(SkuHidden(org_id=ctx.org.id, base_name=base_name))
    elif not body.hidden and row is not None:
        db.delete(row)
    db.commit()
    analytics.invalidate(ctx.org.id)
    return {"ok": True, "base_name": base_name, "hidden": body.hidden}


class CategoryOverrideIn(BaseModel):
    base_name: str = Field(min_length=1, max_length=255)
    category: str = Field(default="", max_length=128)  # '' = вернуть категорию МС


class CategoryMergeIn(BaseModel):
    from_category: str = Field(min_length=1, max_length=128)
    to_category: str = Field(default="", max_length=128)  # '' = отменить слияние


@router.get("/api/categories")
def api_categories(ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)):
    """Пользовательские правила категорий: переносы позиций и слияния."""
    overrides = dict(db.execute(
        select(SkuCategoryOverride.base_name, SkuCategoryOverride.category)
        .where(SkuCategoryOverride.org_id == ctx.org.id)
    ).all())
    merges = dict(db.execute(
        select(CategoryMerge.from_category, CategoryMerge.to_category)
        .where(CategoryMerge.org_id == ctx.org.id)
    ).all())
    return {"overrides": overrides, "merges": merges}


@router.post("/api/categories/override")
def api_category_override(
    body: CategoryOverrideIn,
    ctx: AuthContext = Depends(require_auth_api),
    db: Session = Depends(get_db),
):
    """Перенести отдельную позицию в другую категорию ('' = сбросить).

    Несуществующий base_name раньше молча отвечал {"ok": true} и оседал
    фантомной строкой (или, с пробелами по краям, дублем рядом с настоящей
    позицией) — тот же дефект, что чинили у /api/replenish-draft. Сверяем
    с каталогом ДО записи, как app.api._require_known_base.
    """
    base_name = _require_known_base(db, ctx.org.id, body.base_name)
    row = db.get(SkuCategoryOverride, (ctx.org.id, base_name))
    cat = body.category.strip()
    if not cat:
        if row is not None:
            db.delete(row)
    elif row is None:
        db.add(SkuCategoryOverride(org_id=ctx.org.id, base_name=base_name, category=cat))
    else:
        row.category = cat
    db.commit()
    analytics.invalidate(ctx.org.id)
    return {"ok": True, "base_name": base_name, "category": cat}


@router.post("/api/categories/merge")
def api_category_merge(
    body: CategoryMergeIn,
    ctx: AuthContext = Depends(require_auth_api),
    db: Session = Depends(get_db),
):
    """Влить категорию в другую ('' = отменить слияние этой категории)."""
    row = db.get(CategoryMerge, (ctx.org.id, body.from_category))
    to = body.to_category.strip()
    if not to or to == body.from_category:
        if row is not None:
            db.delete(row)
        to = ""
    elif row is None:
        db.add(CategoryMerge(org_id=ctx.org.id, from_category=body.from_category, to_category=to))
    else:
        row.to_category = to
    db.commit()
    analytics.invalidate(ctx.org.id)
    return {"ok": True, "from_category": body.from_category, "to_category": to}


class DiscountRuleIn(BaseModel):
    new_days: int = Field(ge=0, le=365)
    new_pct: int = Field(ge=0, le=99)
    top_turnover: int = Field(ge=0)
    top_pct: int = Field(ge=0, le=99)
    top_over_pct: int = Field(ge=0, le=99)
    mid_turnover: int = Field(ge=0)
    mid_pct: int = Field(ge=0, le=99)
    mid_over_pct: int = Field(ge=0, le=99)
    weak_pct: int = Field(ge=0, le=99)
    weak_over_pct: int = Field(ge=0, le=99)
    overstock_days: int = Field(ge=1, le=365)


@router.get("/api/discount-rule")
def api_discount_rule(ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)):
    """Правило дефолтных скидок организации (+ дефолты для сброса)."""
    return {
        "rule": analytics.extra_settings(ctx.org)["discount_rule"],
        "defaults": analytics.DEFAULT_DISCOUNT_RULE,
    }


@router.post("/api/discount-rule")
def api_set_discount_rule(
    body: DiscountRuleIn,
    ctx: AuthContext = Depends(require_owner_api),
    db: Session = Depends(get_db),
):
    """Сохранить правило дефолтных скидок (только владелец)."""
    org = db.merge(ctx.org)
    try:
        data = json.loads(org.settings_json or "{}")
    except ValueError:
        data = {}
    data["discount_rule"] = analytics._clean_discount_rule(body.model_dump())
    org.settings_json = json.dumps(data, ensure_ascii=False)
    db.commit()
    analytics.invalidate(org.id)
    return {"ok": True, "rule": data["discount_rule"]}


# ── Ручная ростовка на «Заказе» (черновик правок по размерам) ────────────────
# Производственник почти всегда правит рекомендованную сетку под фабрику.
# Раньше правка жила только в памяти вкладки; здесь она хранится на организации
# (ReplenishDraft) и подставляется в поля при следующем заходе. Пишутся только
# размеры, отличающиеся от расчёта, — неправленые продолжают следовать за
# пересчётом. Всё фильтруется по org_id: чужие правки не видны и не удаляются.

MAX_DRAFT_QTY = 9999      # столько же, сколько допускает поле на странице
MAX_DRAFT_SIZES = 64      # разумный потолок числа размеров у одной позиции


class ReplenishDraftIn(BaseModel):
    base_name: str = Field(min_length=1, max_length=255)
    # {размер: штук} — только размеры, где человек отошёл от расчёта;
    # пустой словарь = правок нет, строки позиции удаляются.
    sizes: dict[str, int] = Field(default_factory=dict)


class ReplenishDraftResetIn(BaseModel):
    base_name: str = Field(default="", max_length=255)  # '' = сбросить всю таблицу


def _drop_orphan_drafts(db: Session, org_id: int) -> None:
    """Убирает правки по позициям и размерам, которых больше нет в каталоге.

    После синка позиция может исчезнуть или переименоваться — черновик не
    должен воскрешать несуществующее. Пары (позиция, размер) сверяются с
    товарами организации; если каталог ещё пуст (синк не проходил), правки не
    трогаем — иначе потеряли бы работу человека из-за неготовых данных.
    """
    known = set(
        db.execute(
            select(Product.base_name, Product.size).where(Product.org_id == org_id)
        ).all()
    )
    if not known:
        return
    rows = db.execute(
        select(ReplenishDraft).where(ReplenishDraft.org_id == org_id)
    ).scalars().all()
    dropped = False
    for row in rows:
        if (row.base_name, row.size) not in known:
            db.delete(row)
            dropped = True
    if dropped:
        db.commit()


@router.get("/api/replenish-draft")
def api_replenish_draft(
    ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)
):
    """Ручные правки ростовки организации: {позиция: {размер: штук}}."""
    _drop_orphan_drafts(db, ctx.org.id)
    rows = db.execute(
        select(ReplenishDraft.base_name, ReplenishDraft.size, ReplenishDraft.qty)
        .where(ReplenishDraft.org_id == ctx.org.id)
    ).all()
    drafts: dict[str, dict[str, int]] = {}
    for base, size, qty in rows:
        drafts.setdefault(base, {})[size] = int(qty)
    return {"drafts": drafts}


@router.post("/api/replenish-draft")
def api_save_replenish_draft(
    body: ReplenishDraftIn,
    ctx: AuthContext = Depends(require_auth_api),
    db: Session = Depends(get_db),
):
    """Сохранить ручную ростовку одной позиции (заменяет прежнюю целиком)."""
    # Раньше несуществующий base_name всё равно отвечал {"ok": true}: запись
    # писалась, но следующий GET её тут же вычищал (_drop_orphan_drafts) —
    # человек считал, что сохранил, а не сохранил. Сверяем с каталогом ДО
    # записи, как это уже сделано в app.api._require_known_base.
    base_name = _require_known_base(db, ctx.org.id, body.base_name)
    if len(body.sizes) > MAX_DRAFT_SIZES:
        raise HTTPException(status_code=422, detail="Слишком много размеров в одной позиции")
    clean: dict[str, int] = {}
    for size, qty in body.sizes.items():
        size = (size or "").strip()
        if len(size) > 32:
            raise HTTPException(status_code=422, detail="Слишком длинное название размера")
        if qty < 0 or qty > MAX_DRAFT_QTY:
            raise HTTPException(
                status_code=422,
                detail=f"Количество по размеру — целое от 0 до {MAX_DRAFT_QTY} шт",
            )
        clean[size] = int(qty)
    db.execute(
        delete(ReplenishDraft).where(
            ReplenishDraft.org_id == ctx.org.id, ReplenishDraft.base_name == base_name
        )
    )
    for size, qty in clean.items():
        db.add(ReplenishDraft(org_id=ctx.org.id, base_name=base_name, size=size, qty=qty))
    db.commit()
    return {"ok": True, "base_name": base_name, "sizes": clean}


@router.post("/api/replenish-draft/reset")
def api_reset_replenish_draft(
    body: ReplenishDraftResetIn,
    ctx: AuthContext = Depends(require_auth_api),
    db: Session = Depends(get_db),
):
    """Вернуть ростовку к расчёту: одной позиции или всей таблицы ('')."""
    stmt = delete(ReplenishDraft).where(ReplenishDraft.org_id == ctx.org.id)
    if body.base_name:
        stmt = stmt.where(ReplenishDraft.base_name == body.base_name)
    removed = db.execute(stmt).rowcount
    db.commit()
    return {"ok": True, "base_name": body.base_name, "removed": int(removed or 0)}


def moq_conflict_note(item: dict) -> str:
    """Чем итоговое количество расходится с условиями подрядчика (пусто — не расходится).

    Появляется только после ручной правки: расчёт условия соблюдает всегда
    (их применяет apply_production_rules), а человек вправе поставить своё
    число — но узнать об этом он должен здесь, а не от фабрики.
    """
    need = int(item.get("need") or 0)
    moq = int(item.get("moq") or 0)
    step = int(item.get("pack_multiple") or 0)
    if need <= 0:
        return ""
    bad = []
    if moq and need < moq:
        bad.append(f"минимальная партия {moq} шт")
    if step > 1 and need % step:
        bad.append(f"кратность {step} шт")
    if not bad:
        return ""
    name = item.get("production_name")
    where = f" производства «{name}»" if name else " производства"

    return (
        f"{need} шт не проходит по условиям{where} ({' и '.join(bad)}) — "
        f"согласуйте количество с фабрикой или верните расчёт"
    )


def apply_replenish_drafts(db: Session, org_id: int, data: dict) -> dict:
    """Накладывает сохранённые ручные правки ростовки на ответ «Заказа».

    Порядок ровно тот же, что человек видит на странице: сначала расчёт
    округляется по условиям подрядчика (apply_production_rules), и уже поверх
    готовой сетки ложится правка руками. Ручная правка — последнее слово:
    минимальная партия её НЕ переписывает. Иначе вписанные 12 шт молча стали
    бы 30, и заказать ровно столько, сколько человек решил, было бы нельзя.
    Но и молчать о нарушенном условии не годится — такие позиции получают
    moq_note, который виден и на экране, и в примечании выгрузки.

    Позиция получает поля: need_calc (сколько было до правки), manual
    (правлена ли ростовка), manual_sizes (какие размеры), moq_note. Правленый
    размер — rec_calc (расчёт) и manual.
    """
    rows = db.execute(
        select(ReplenishDraft.base_name, ReplenishDraft.size, ReplenishDraft.qty)
        .where(ReplenishDraft.org_id == org_id)
    ).all()
    drafts: dict[str, dict[str, int]] = {}
    for base, size, qty in rows:
        drafts.setdefault(base, {})[size] = int(qty)
    for item in data.get("items") or []:
        item["need_calc"] = int(item.get("need") or 0)
        item["manual"] = False
        item["manual_sizes"] = []
        draft = drafts.get(item["base_name"]) or {}
        sizes = item.get("sizes") or {}
        edited = []
        for size, cell in sizes.items():
            # размеров, которых уже нет в позиции, в сетке просто нет —
            # правка по ним не воскресает (так же ведёт себя страница)
            if size not in draft:
                continue
            cell["rec_calc"] = int(cell.get("rec") or 0)
            cell["rec"] = int(draft[size])
            cell["manual"] = True
            edited.append(size)
        if edited:
            item["manual"] = True
            item["manual_sizes"] = edited
            # итог позиции = сумма по сетке, как и на странице: иначе строка
            # «Заказать» разошлась бы с размерами внутри неё
            item["need"] = sum(int(c.get("rec") or 0) for c in sizes.values())
        item["moq_note"] = moq_conflict_note(item)
    return data


def replenish_as_on_screen(db: Session, org) -> dict:
    """«Что заказать» ровно в том виде, в каком человек видит его на странице.

    Один путь для выгрузки: расчёт → условия подрядчика → ручные правки.
    Раньше выгрузка звала build_replenish напрямую и не знала ни про то, ни
    про другое — файл для фабрики расходился с экраном.
    """
    data = analytics.build_replenish(analytics.get_snapshot(db, org))
    apply_production_rules(db, org.id, data)
    apply_replenish_drafts(db, org.id, data)
    return data


# ── Экспорт в Excel (.xlsx) ──────────────────────────────────────────────────
# Данные — те же билдеры, что у JSON API (без дублирования расчётов);
# export_xlsx только раскладывает готовый ответ по ячейкам и оформляет.

from app import export_xlsx  # noqa: E402  (секция добавлена в конец модуля)


class ReplenishExportIn(BaseModel):
    """Тело частичной выгрузки — позиции, отмеченные галочкой на странице.

    Список едет в теле POST, а не в query-параметре: у клиента может быть
    и 500 позиций, и имена не короткие («Худи «Скетч» оверсайз, чёрный»),
    в адресную строку GET такой список не влезает (лимит ~2000-8000
    символов у браузеров/прокси). Полная выгрузка (по умолчанию всё
    отмечено) по-прежнему простой GET без тела — адрес остаётся коротким.
    """
    base_names: list[str] = Field(default_factory=list, max_length=5000)


def _replenish_export_data(db: Session, org, selected: list[str] | None) -> dict:
    """Готовит данные книги «Что заказать»: целиком или по выбранным позициям.

    selected=None — полная выгрузка (все позиции вкладок), как было раньше.
    selected=[...] — только эти base_name идут в лист заказа; остальные
    переносятся на лист «Не вошло и почему» с понятной причиной, а не
    молча пропадают — товаровед должна видеть, что часть позиций убрали
    вручную, а не решить, что расчёт их «потерял».
    """
    data = replenish_as_on_screen(db, org)
    if selected is None:
        return data
    wanted = {n.strip() for n in selected if n and n.strip()}
    if not wanted:
        raise HTTPException(
            status_code=422,
            detail="Ни одна позиция не отмечена — выгружать в Excel нечего. "
                   "Отметьте галочкой хотя бы одну позицию.",
        )
    kept, dropped = [], []
    totals_by_prod: dict[str, int] = {}
    for it in data.get("items") or []:
        prod = it.get("production_name") or ""
        totals_by_prod[prod] = totals_by_prod.get(prod, 0) + 1
        (kept if it["base_name"] in wanted else dropped).append(it)
    data["items"] = kept
    if dropped:
        data["excluded"] = list(data.get("excluded") or []) + [
            {
                "base_name": it["base_name"],
                "reason": "Галочка снята вручную на странице «Заказ» — "
                          "в эту партию позиция не идёт",
            }
            for it in dropped
        ]
    # Для правдивой шапки листа: сколько позиций ЭТОГО производства было
    # отмечено из скольких на вкладке (не общий счёт по всей книге — иначе
    # лист, где сняли вручную ничего не убирали, ошибочно сообщал бы о
    # выборке из-за исключений на чужой вкладке).
    data["selection_partial"] = True
    data["selection_totals_by_production"] = totals_by_prod
    return data


@router.get("/api/export/replenish.xlsx")
def export_replenish_xlsx(
    ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)
):
    """«Что заказать» в Excel, ПОЛНЫЙ состав: позиции + размерные сетки, итог.

    Данные берутся тем же путём, что и страница (replenish_as_on_screen):
    файл, по которому шьёт фабрика, обязан совпадать с тем, что человек видел
    и утвердил на экране — вместе с условиями подрядчика и правками ростовки.
    Это выгрузка «всё отмечено» (состояние страницы по умолчанию) — простой
    GET без параметров, адрес остаётся коротким и его можно сохранить.
    Выборка по снятым галочкам — см. POST на этот же путь.
    """
    data = _replenish_export_data(db, ctx.org, None)
    wb = export_xlsx.replenish_workbook(ctx.org.name, data)
    return export_xlsx.xlsx_response(wb, "Что заказать.xlsx", "replenish.xlsx")


@router.post("/api/export/replenish.xlsx")
def export_replenish_xlsx_selected(
    body: ReplenishExportIn,
    ctx: AuthContext = Depends(require_auth_api),
    db: Session = Depends(get_db),
):
    """«Что заказать» в Excel — только позиции, отмеченные на странице.

    Человек несёт этот файл на фабрику: в него должно попасть ровно то, что
    он оставил отмеченным, а не всё содержимое вкладки (раньше файл не знал
    про галочки вообще). Снятые вручную позиции не пропадают молча — они
    переезжают на лист «Не вошло и почему» со своей причиной.
    """
    data = _replenish_export_data(db, ctx.org, body.base_names)
    wb = export_xlsx.replenish_workbook(ctx.org.name, data)
    return export_xlsx.xlsx_response(wb, "Что заказать.xlsx", "replenish.xlsx")


@router.get("/api/export/turnover.xlsx")
def export_turnover_xlsx(
    ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)
):
    """«Оборачиваемость» в Excel: все колонки страницы /turnover."""
    data = analytics.build_turnover(analytics.get_snapshot(db, ctx.org))
    wb = export_xlsx.turnover_workbook(ctx.org.name, data)
    return export_xlsx.xlsx_response(wb, "Оборачиваемость.xlsx", "turnover.xlsx")


@router.get("/api/export/discounts.xlsx")
def export_discounts_xlsx(
    ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)
):
    _hidden_404("discounts")
    """Прайс уценки в Excel: скидки, старая/новая цена, причина."""
    data = analytics_markdown.build_discounts(analytics.get_snapshot(db, ctx.org))
    wb = export_xlsx.discounts_workbook(ctx.org.name, data)
    return export_xlsx.xlsx_response(wb, "Уценка.xlsx", "discounts.xlsx")


@router.get("/api/export/budget.xlsx")
def export_budget_xlsx(
    amount: int = Query(200_000, ge=1, le=1_000_000_000),
    max_share: int = Query(30, ge=5, le=100),
    exclude_cats: str = Query(""),
    ctx: AuthContext = Depends(require_auth_api),
    db: Session = Depends(get_db),
):
    """Распределение бюджета закупки в Excel (параметры — как у /api/budget)."""
    snap = analytics.get_snapshot(db, ctx.org)
    excluded = {c.strip() for c in exclude_cats.split(",") if c.strip()}
    data = analytics_extra.build_budget(db, ctx.org.id, snap, amount, max_share, excluded)
    wb = export_xlsx.budget_workbook(ctx.org.name, data)
    return export_xlsx.xlsx_response(wb, "Бюджет закупки.xlsx", "budget.xlsx")


# ── Тарифы и заявка на счёт ──────────────────────────────────────────────────
# ВАЖНО: PLANS — ЕДИНСТВЕННОЕ место в коде, где задан состав тарифной сетки.
# Чтобы убрать тариф, добавить новый или переписать состав — правится только
# этот список: и страница /plans, и подсказка в Настройках, и API /api/plans
# читают его. Цены и формулировки синхронизированы с лендингом
# (templates/landing.html, секция «Тарифы») — при правке меняйте оба места.

PLANS = [
    {
        "code": "start",
        "name": "Старт",
        "tagline": "Навести порядок в остатках",
        "price_month": 4900,
        "price_year_month": 3920,  # ₽/мес при оплате за год (−20%)
        "popular": False,
        "features": [
            "1 организация МойСклад",
            "Остатки по складам с историей",
            "Оборачиваемость по каждой позиции",
            "Что уценить: неликвид и контроль 70/20/10",
        ],
    },
    {
        "code": "brand",
        "name": "Бренд",
        "tagline": "Ваш товарный аналитик на полную ставку",
        "price_month": 9900,
        "price_year_month": 7920,
        "popular": True,
        "features": [
            "Всё из тарифа «Старт»",
            "Что заказать: расчёт с размерными сетками",
            "Сезонность и срок производства в прогнозе",
            "Заказ в один клик в МойСклад",
            "Telegram-дайджест каждое утро",
        ],
    },
    {
        "code": "pro",
        "name": "Про",
        "tagline": "Для растущих и мультиканальных",
        "price_month": 19900,
        "price_year_month": 15920,
        "popular": False,
        "features": [
            "Всё из тарифа «Бренд»",
            "Маркетплейсы и Shopify — скоро",
            "Доступ к API",
            "Приоритетная поддержка",
        ],
    },
]

PLAN_CODES = {p["code"] for p in PLANS}
# Сколько рабочих дней мы обещаем на выставление счёта — показывается на
# странице и повторяется в подтверждении заявки, чтобы обещание было одно.
INVOICE_DAYS = 1


def _plan_by_code(code: str) -> dict | None:
    for p in PLANS:
        if p["code"] == code:
            return p
    return None


class BillingRequest(Base):
    """Заявка на счёт: «хочу тариф N, вот реквизиты, выставьте счёт».

    Полноценного биллинга нет и не планируется в этом релизе: деньги ходят
    по счёту от юрлица/ИП. Заявка — единственный способ для клиента заявить
    о намерении платить и получить подтверждение, что мы это увидели.
    Обрабатывается вручную (статус new → invoiced → paid).
    """

    __tablename__ = "billing_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("orgs.id"), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    plan: Mapped[str] = mapped_column(String(16), nullable=False)          # код из PLANS
    period: Mapped[str] = mapped_column(String(8), nullable=False, default="month")  # month|year
    amount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # ₽/мес на момент заявки
    company: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    inn: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    email: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    phone: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    comment: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="new")  # new|invoiced|paid
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


def _trial_days_left(org) -> int | None:
    """Сколько полных дней осталось до конца триала (0 — уже кончился)."""
    if org.plan != "trial" or not org.trial_ends_at:
        return None
    return max(0, (org.trial_ends_at.date() - date.today()).days)


def _request_out(row: BillingRequest | None) -> dict | None:
    if row is None:
        return None
    plan = _plan_by_code(row.plan)
    return {
        "id": row.id,
        "plan": row.plan,
        "plan_name": plan["name"] if plan else row.plan,
        "period": row.period,
        "amount": row.amount,
        "company": row.company,
        "inn": row.inn,
        "email": row.email,
        "phone": row.phone,
        "comment": row.comment,
        "status": row.status,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@router.get("/plans", response_class=HTMLResponse)
def plans_page(request: Request, db: Session = Depends(get_db)):
    """Тарифы внутри продукта: что сейчас, сколько осталось триала, как оплатить."""
    return _authed_page(request, db, "plans.html", "settings", "Тарифы и оплата")


@router.get("/api/plans")
def api_plans(ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)):
    """Тарифная сетка (из PLANS), текущий тариф организации и последняя заявка."""
    last = db.execute(
        select(BillingRequest)
        .where(BillingRequest.org_id == ctx.org.id)
        .order_by(BillingRequest.id.desc())
    ).scalars().first()
    return {
        "plans": PLANS,
        "current": ctx.org.plan,
        "trial_ends_at": ctx.org.trial_ends_at.date().isoformat() if ctx.org.trial_ends_at else None,
        "trial_days_left": _trial_days_left(ctx.org),
        "invoice_days": INVOICE_DAYS,
        "role": ctx.role,
        # Организация, пришедшая из каталога МойСклад (source='ms_app'), платит
        # через МС — счёт от нас ей выставлять нельзя, форму заявки прячем.
        "source": getattr(ctx.org, "source", "saas"),
        "ms_tariff_name": getattr(ctx.org, "ms_tariff_name", "") or "",
        "request": _request_out(last),
    }


class PlanRequestIn(BaseModel):
    plan: str = Field(min_length=1, max_length=16)
    period: str = Field(default="month", max_length=8)
    company: str = Field(default="", max_length=255)
    inn: str = Field(default="", max_length=16)
    email: str = Field(default="", max_length=255)
    phone: str = Field(default="", max_length=64)
    comment: str = Field(default="", max_length=2000)


@router.post("/api/plans/request")
def api_plan_request(
    body: PlanRequestIn,
    ctx: AuthContext = Depends(require_owner_api),
    db: Session = Depends(get_db),
):
    """Заявка на счёт по выбранному тарифу (обрабатываем руками, без биллинга).

    Только владелец: оплата — его решение. Проверки реквизитов человеческие:
    ошибка говорит, что именно не так и что с этим делать.

    Открытая заявка (status='new') у организации может быть только одна —
    очередь разбирает человек руками, и десять одинаковых строк в ней это
    мусор, а бесконтрольный поток — отказ в обслуживании. Повтор с теми же
    данными ничего не создаёт и отвечает понятной ошибкой; человек, который
    передумал и прислал другие данные (другой тариф, период, реквизиты),
    обновляет ту же открытую заявку — новая строка появится только после
    того, как эту обработают (status уйдёт в invoiced/paid).
    """
    if getattr(ctx.org, "source", "saas") == "ms_app":
        raise HTTPException(
            status_code=409,
            detail="Ваша подписка оформлена в маркетплейсе МойСклад — тариф и оплата "
                   "меняются там, в разделе «Приложения». Счёт от нас не нужен.",
        )
    plan = _plan_by_code(body.plan)
    if plan is None:
        raise HTTPException(status_code=422, detail="Выберите тариф из списка")
    if body.period not in ("month", "year"):
        raise HTTPException(status_code=422, detail="Выберите период оплаты: месяц или год")
    company = body.company.strip()
    if len(company) < 2:
        raise HTTPException(
            status_code=422,
            detail="Укажите название организации или ИП — так, как оно должно стоять в счёте",
        )
    inn = "".join(ch for ch in body.inn if ch.isdigit())
    if len(inn) not in (10, 12):
        raise HTTPException(
            status_code=422,
            detail="ИНН — 10 цифр у организации или 12 у ИП. Проверьте число цифр.",
        )
    email = body.email.strip().lower()
    if "@" not in email or "." not in email.split("@")[-1]:
        raise HTTPException(
            status_code=422,
            detail="Укажите почту, на которую прислать счёт — на неё уйдёт письмо с документами",
        )
    amount = plan["price_year_month"] if body.period == "year" else plan["price_month"]
    fields = dict(
        plan=plan["code"],
        period=body.period,
        amount=int(amount),
        company=company,
        inn=inn,
        email=email,
        phone=body.phone.strip(),
        comment=body.comment.strip(),
    )

    open_row = db.execute(
        select(BillingRequest)
        .where(BillingRequest.org_id == ctx.org.id, BillingRequest.status == "new")
        .order_by(BillingRequest.id.desc())
    ).scalars().first()

    if open_row is not None:
        if all(getattr(open_row, key) == value for key, value in fields.items()):
            raise HTTPException(
                status_code=409,
                detail="Такая заявка уже отправлена и ждёт обработки — присылать её ещё раз "
                       "не нужно. Если хотите выбрать другой тариф или период, поменяйте их в "
                       "форме и отправьте заново.",
            )
        # Другие данные — значит, передумал: обновляем открытую заявку, а не
        # заводим вторую. Одной организации в очереди — одна открытая строка.
        for key, value in fields.items():
            setattr(open_row, key, value)
        open_row.user_id = ctx.user.id
        open_row.created_at = datetime.utcnow()
        db.commit()
        return {"ok": True, "invoice_days": INVOICE_DAYS, "request": _request_out(open_row)}

    row = BillingRequest(org_id=ctx.org.id, user_id=ctx.user.id, status="new", **fields)
    db.add(row)
    db.commit()
    return {"ok": True, "invoice_days": INVOICE_DAYS, "request": _request_out(row)}
