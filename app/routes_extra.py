"""Роуты портированной аналитики: Бюджет (OTB), Прогноз, Размеры.

Страницы (/budget, /forecast, /sizes) — тонкие Jinja2-шаблоны, вся математика
в app.analytics_extra; JSON API — под require_auth_api, данные только своей org.
Паттерн страниц повторяет app.main._authed_page (main импортирует этот модуль,
поэтому импортировать main отсюда нельзя — свой экземпляр Jinja2Templates).
"""
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from pydantic import BaseModel, Field
from sqlalchemy import delete, select

import json

from app import analytics, analytics_extra, analytics_markdown, auth
from app.auth import AuthContext, require_auth_api, require_owner_api
from app.db import get_db
from app.models import CategoryMerge, SkuCategoryOverride, SkuDiscount, SkuHidden

router = APIRouter()

_BASE_DIR = Path(__file__).resolve().parent.parent
# Тот же порядок, что в main.py: настоящие шаблоны перекрывают заглушки.
_templates = Jinja2Templates(
    directory=[str(_BASE_DIR / "templates"), str(_BASE_DIR / "_stub_templates")]
)


def _authed_page(request: Request, db: Session, template: str, active: str, page_title: str):
    """Страница под сессией: без авторизации — redirect /login (как в main.py)."""
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


@router.get("/budget", response_class=HTMLResponse)
def budget_page(request: Request, db: Session = Depends(get_db)):
    return _authed_page(request, db, "budget.html", "budget", "Бюджет закупки")


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
    data = analytics_extra.build_forecast(snap)
    data.update(analytics_extra.forecast_refs(db, ctx.org.id, snap))
    return data


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
    """Установить/снять ручную скидку позиции (0 = снять)."""
    row = db.get(SkuDiscount, (ctx.org.id, body.base_name))
    if body.discount <= 0:
        if row is not None:
            db.delete(row)
    elif row is None:
        db.add(SkuDiscount(org_id=ctx.org.id, base_name=body.base_name,
                           discount=round(body.discount)))
    else:
        row.discount = round(body.discount)
    db.commit()
    return {"ok": True, "base_name": body.base_name, "discount": round(body.discount)}


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
    остатки прогнозируются на дату прихода (arrival, иначе today+lead_time)."""
    snap = analytics.get_snapshot(db, ctx.org)
    lead = analytics.extra_settings(ctx.org)["lead_time_days"]
    result = analytics_extra.build_sizes_calc(
        db, ctx.org.id, snap, product, qty, period, mode,
        arrival=arrival or None, lead_time_days=lead,
    )
    if result.get("error"):
        raise HTTPException(status_code=422, detail=result["error"])
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
    """Убрать в архив / вернуть из архива."""
    row = db.get(SkuHidden, (ctx.org.id, body.base_name))
    if body.hidden and row is None:
        db.add(SkuHidden(org_id=ctx.org.id, base_name=body.base_name))
    elif not body.hidden and row is not None:
        db.delete(row)
    db.commit()
    analytics.invalidate(ctx.org.id)
    return {"ok": True, "base_name": body.base_name, "hidden": body.hidden}


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
    """Перенести отдельную позицию в другую категорию ('' = сбросить)."""
    row = db.get(SkuCategoryOverride, (ctx.org.id, body.base_name))
    cat = body.category.strip()
    if not cat:
        if row is not None:
            db.delete(row)
    elif row is None:
        db.add(SkuCategoryOverride(org_id=ctx.org.id, base_name=body.base_name, category=cat))
    else:
        row.category = cat
    db.commit()
    analytics.invalidate(ctx.org.id)
    return {"ok": True, "base_name": body.base_name, "category": cat}


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


# ── Экспорт в Excel (.xlsx) ──────────────────────────────────────────────────
# Данные — те же билдеры, что у JSON API (без дублирования расчётов);
# export_xlsx только раскладывает готовый ответ по ячейкам и оформляет.

from app import export_xlsx  # noqa: E402  (секция добавлена в конец модуля)


@router.get("/api/export/replenish.xlsx")
def export_replenish_xlsx(
    ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)
):
    """«Что заказать» в Excel: позиции + размерные сетки, итог по сумме заказа."""
    data = analytics.build_replenish(analytics.get_snapshot(db, ctx.org))
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
