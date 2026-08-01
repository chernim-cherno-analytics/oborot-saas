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

from app import analytics, analytics_extra, analytics_markdown, auth
from app.auth import AuthContext, require_auth_api, require_owner_api
from app.db import get_db
from app.models import SkuDiscount

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

@router.get("/budget", response_class=HTMLResponse)
def budget_page(request: Request, db: Session = Depends(get_db)):
    return _authed_page(request, db, "budget.html", "budget", "Бюджет закупки")


@router.get("/forecast", response_class=HTMLResponse)
def forecast_page(request: Request, db: Session = Depends(get_db)):
    return _authed_page(request, db, "forecast.html", "forecast", "Прогноз")


@router.get("/sizes", response_class=HTMLResponse)
def sizes_page(request: Request, db: Session = Depends(get_db)):
    return _authed_page(request, db, "sizes.html", "sizes", "Размеры")


@router.get("/discounts", response_class=HTMLResponse)
def discounts_page(request: Request, db: Session = Depends(get_db)):
    return _authed_page(request, db, "discounts.html", "discounts", "Скидки")


@router.get("/revenue", response_class=HTMLResponse)
def revenue_page(request: Request, db: Session = Depends(get_db)):
    return _authed_page(request, db, "revenue.html", "revenue", "Оборот")


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
    ctx: AuthContext = Depends(require_auth_api),
    db: Session = Depends(get_db),
):
    """Распределение заказа позиции по размерам на основе истории продаж."""
    snap = analytics.get_snapshot(db, ctx.org)
    result = analytics_extra.build_sizes_calc(db, ctx.org.id, snap, product, qty, period, mode)
    if result.get("error"):
        raise HTTPException(status_code=422, detail=result["error"])
    return result


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
