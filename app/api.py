"""JSON API. Все эндпоинты — под сессией; данные строго текущей организации."""
import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import analytics, ms_writeback
from app.auth import AuthContext, require_auth_api, require_owner_api
from app.crypto import encrypt_token
from app.db import get_db
from app.demo_seed import seed_demo
from app.models import Connection, Membership, OrderedQty, Product, ProductionOrder, User, Warehouse

router = APIRouter(prefix="/api")


# ── Аналитика ────────────────────────────────────────────────────────────────

@router.get("/summary")
def api_summary(ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)):
    return analytics.build_summary(analytics.get_snapshot(db, ctx.org))


@router.get("/replenish")
def api_replenish(ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)):
    data = analytics.build_replenish(analytics.get_snapshot(db, ctx.org))
    # Для пустого состояния: какие заказы «В производстве» закрывают потребность.
    sent = db.execute(
        select(ProductionOrder.id, ProductionOrder.name)
        .where(ProductionOrder.org_id == ctx.org.id, ProductionOrder.status == "sent")
        .order_by(ProductionOrder.created_at.desc())
    ).all()
    data["orders_in_production"] = [{"id": o.id, "name": o.name} for o in sent]
    return data


@router.get("/turnover")
def api_turnover(ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)):
    return analytics.build_turnover(analytics.get_snapshot(db, ctx.org))


@router.get("/stocks")
def api_stocks(ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)):
    return analytics.build_stocks(analytics.get_snapshot(db, ctx.org))


# ── Заказы на производство ───────────────────────────────────────────────────

class OrderItemIn(BaseModel):
    base_name: str
    qty: int = Field(ge=0, le=100_000)
    sizes: dict[str, int] = Field(default_factory=dict)
    cost: float = 0.0

    @field_validator("sizes")
    @classmethod
    def _sizes_sane(cls, v: dict[str, int]) -> dict[str, int]:
        for size, q in v.items():
            if q < 0:
                raise ValueError(f"Отрицательное количество в размере {size!r}")
            if q > 100_000:
                raise ValueError(f"Неправдоподобное количество в размере {size!r}")
        return v


class OrderIn(BaseModel):
    name: str = ""
    eta_date: str | None = None
    items: list[OrderItemIn]


def _order_out(order: ProductionOrder) -> dict:
    items = order.items
    return {
        "id": order.id,
        "name": order.name,
        "created_at": order.created_at.isoformat() if order.created_at else None,
        "eta_date": order.eta_date,
        "status": order.status,
        "items": items,
        "positions": len(items),
        "total_qty": sum(int(i.get("qty") or 0) for i in items),
        "total_cost": round(sum(float(i.get("cost") or 0) * int(i.get("qty") or 0) for i in items)),
    }


@router.get("/orders")
def api_orders(ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)):
    orders = db.execute(
        select(ProductionOrder)
        .where(ProductionOrder.org_id == ctx.org.id)
        .order_by(ProductionOrder.created_at.desc())
    ).scalars().all()
    return {"orders": [_order_out(o) for o in orders]}


@router.post("/orders")
def api_create_order(
    body: OrderIn, ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)
):
    items = [i for i in body.items if i.qty > 0]
    if not items:
        raise HTTPException(status_code=422, detail="В заказе нет позиций с количеством > 0")
    name = body.name.strip() or f"Заказ от {datetime.now():%d.%m.%Y}"
    # Себестоимость берём из БД (клиентской не доверяем), клиентская — фолбэк.
    cost_by_base = {
        p.base_name: float(p.cost_price or 0)
        for p in db.execute(
            select(Product).where(Product.org_id == ctx.org.id)
        ).scalars()
        if p.cost_price
    }
    payload = []
    for i in items:
        d = i.model_dump()
        if cost_by_base.get(i.base_name):
            d["cost"] = cost_by_base[i.base_name]
        payload.append(d)
    order = ProductionOrder(
        org_id=ctx.org.id,
        name=name,
        eta_date=body.eta_date,
        status="draft",
        items_json=json.dumps(payload, ensure_ascii=False),
    )
    db.add(order)
    # ВАЖНО (фикс P0): черновик НЕ попадает в «едет к нам» — рекомендации
    # «Что заказать» уменьшаются только после перевода заказа «В производство».
    db.commit()
    analytics.invalidate(ctx.org.id)
    return {"ok": True, "id": order.id, "status": "draft"}


def _apply_order_to_incoming(db: Session, org_id: int, order: ProductionOrder, sign: int) -> None:
    """Прибавляет (sign=+1) или вычитает (sign=-1) позиции заказа из «едет к нам»."""
    for item in order.items:
        base, qty = item.get("base_name"), int(item.get("qty") or 0)
        if not base or qty <= 0:
            continue
        row = db.get(OrderedQty, (org_id, base))
        if row is None:
            db.add(OrderedQty(org_id=org_id, base_name=base, qty=max(0, sign * qty)))
        else:
            row.qty = max(0, row.qty + sign * qty)


class OrderStatusIn(BaseModel):
    status: str


@router.post("/orders/{order_id}/status")
def api_order_status(
    order_id: int,
    body: OrderStatusIn,
    ctx: AuthContext = Depends(require_auth_api),
    db: Session = Depends(get_db),
):
    """Переходы статуса: draft → sent («В производстве») → received («Принят на склад»).

    Семантика «едет к нам»: draft не учитывается; sent прибавляет; received
    вычитает (пришедшие остатки подтянет синхронизация со складом).

    Заказ, отправленный в МойСклад (ms_doc_href), локальный qty НЕ двигает:
    его считает импорт purchaseorder (ordered_qty.ms_qty), а принятое снимают
    приёмки в МС — иначе был бы двойной счёт.
    """
    order = db.get(ProductionOrder, order_id)
    if order is None or order.org_id != ctx.org.id:
        raise HTTPException(status_code=404, detail="Заказ не найден")
    allowed = {("draft", "sent"), ("sent", "received")}
    if (order.status, body.status) not in allowed:
        raise HTTPException(
            status_code=422,
            detail=f"Недопустимый переход статуса: {order.status} → {body.status}",
        )
    if not ms_writeback.is_pushed(order.ms_doc_href):
        if body.status == "sent":
            _apply_order_to_incoming(db, ctx.org.id, order, +1)
        else:  # received
            _apply_order_to_incoming(db, ctx.org.id, order, -1)
    order.status = body.status
    db.commit()
    analytics.invalidate(ctx.org.id)
    return {"ok": True, "status": order.status}


@router.delete("/orders/{order_id}")
def api_order_delete(
    order_id: int, ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)
):
    """Удаление заказа: draft — свободно; sent — с вычетом из «едет»; received — нельзя."""
    order = db.get(ProductionOrder, order_id)
    if order is None or order.org_id != ctx.org.id:
        raise HTTPException(status_code=404, detail="Заказ не найден")
    if order.status == "received":
        raise HTTPException(status_code=422, detail="Принятый на склад заказ удалить нельзя")
    if order.status == "sent" and not ms_writeback.is_pushed(order.ms_doc_href):
        # Отправленный в МС заказ в qty не входил (его считает ms_qty; сам
        # документ в МойСклад при локальном удалении никуда не девается).
        _apply_order_to_incoming(db, ctx.org.id, order, -1)
    db.delete(order)
    db.commit()
    analytics.invalidate(ctx.org.id)
    return {"ok": True}


@router.get("/orders/{order_id}")
def api_order_detail(
    order_id: int, ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)
):
    order = db.get(ProductionOrder, order_id)
    if order is None or order.org_id != ctx.org.id:
        raise HTTPException(status_code=404, detail="Заказ не найден")
    return _order_out(order)


class OrderedIn(BaseModel):
    base_name: str
    qty: float = Field(ge=0)


@router.post("/ordered")
def api_set_ordered(
    body: OrderedIn, ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)
):
    """Ручная правка «едет к нам» (перезапись значения по базовому имени)."""
    row = db.get(OrderedQty, (ctx.org.id, body.base_name))
    if row is None:
        db.add(OrderedQty(org_id=ctx.org.id, base_name=body.base_name, qty=body.qty))
    else:
        row.qty = body.qty
    db.commit()
    analytics.invalidate(ctx.org.id)
    return {"ok": True}


# ── Настройки ────────────────────────────────────────────────────────────────

@router.get("/settings")
def api_settings(ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)):
    org = ctx.org
    warehouses = db.execute(
        select(Warehouse).where(Warehouse.org_id == org.id).order_by(Warehouse.id)
    ).scalars().all()
    conn = db.execute(
        select(Connection).where(Connection.org_id == org.id).order_by(Connection.id.desc())
    ).scalars().first()
    members = db.execute(
        select(User.name, User.email, Membership.role)
        .join(Membership, Membership.user_id == User.id)
        .where(Membership.org_id == org.id)
    ).all()
    settings = org.settings
    extra = analytics.extra_settings(org)
    return {
        "org": {
            "name": org.name,
            "plan": org.plan,
            "trial_ends_at": org.trial_ends_at.isoformat() if org.trial_ends_at else None,
        },
        "thresholds": settings["thresholds"],
        "horizon_days": settings["horizon_days"],
        "min_stock_days": settings["min_stock_days"],
        "rate_window": extra["rate_window"],
        "lead_time_days": extra["lead_time_days"],
        # Горизонт покрытия заказа: периодичность размещения + страховой запас.
        "cover_mode": extra["cover_mode"],
        "order_cadence_days": extra["order_cadence_days"],
        "safety_days": extra["safety_days"],
        "cover_days": analytics.cover_days({**settings, **extra}),
        "moq_units": extra["moq_units"],
        "reserve_new_pct": extra["reserve_new_pct"],
        "price_type_sale": extra["price_type_sale"],
        "price_type_cost": extra["price_type_cost"],
        "peak_periods": extra["peak_periods"],
        "warehouses": [{"id": w.id, "name": w.name, "active": w.active} for w in warehouses],
        "connection": (
            {
                "kind": conn.kind,
                "status": conn.status,
                "last_sync_at": conn.last_sync_at.isoformat() if conn.last_sync_at else None,
            }
            if conn
            else None
        ),
        "members": [{"name": n, "email": e, "role": r} for n, e, r in members],
        "role": ctx.role,  # фронт прячет owner-only кнопки (синк) от участников
    }


class ThresholdsIn(BaseModel):
    weak: int = Field(gt=0)
    dull: int = Field(gt=0)
    good: int = Field(gt=0)


class SettingsIn(BaseModel):
    thresholds: ThresholdsIn | None = None
    horizon_days: int | None = Field(default=None, ge=7, le=365)
    min_stock_days: int | None = Field(default=None, ge=0, le=100)
    rate_window: str | None = None  # 'year' | 'd90' | 'season'
    lead_time_days: int | None = Field(default=None, ge=1, le=365)
    cover_mode: str | None = None  # 'cadence' | 'fixed'
    order_cadence_days: int | None = Field(default=None, ge=7, le=365)
    safety_days: int | None = Field(default=None, ge=0, le=120)
    moq_units: int | None = Field(default=None, ge=0, le=10000)
    reserve_new_pct: int | None = Field(default=None, ge=0, le=90)
    price_type_sale: str | None = Field(default=None, max_length=128)
    price_type_cost: str | None = Field(default=None, max_length=128)
    peak_periods: list[dict] | None = None

    @field_validator("cover_mode")
    @classmethod
    def _cover_mode_known(cls, v: str | None) -> str | None:
        if v is not None and v not in ("cadence", "fixed"):
            raise ValueError("cover_mode должен быть 'cadence' или 'fixed'")
        return v

    @field_validator("rate_window")
    @classmethod
    def _rate_window_known(cls, v: str | None) -> str | None:
        if v is not None and v not in analytics.RATE_WINDOWS:
            raise ValueError("rate_window должен быть 'year', 'd90' или 'season'")
        return v


@router.post("/settings")
def api_update_settings(
    body: SettingsIn, ctx: AuthContext = Depends(require_owner_api), db: Session = Depends(get_db)
):
    org = db.merge(ctx.org)
    settings = org.settings
    if body.thresholds is not None:
        t = body.thresholds
        if not t.weak < t.dull < t.good:
            raise HTTPException(status_code=422, detail="Пороги должны возрастать: weak < dull < good")
        settings["thresholds"] = {"weak": t.weak, "dull": t.dull, "good": t.good}
    if body.horizon_days is not None:
        settings["horizon_days"] = body.horizon_days
    if body.min_stock_days is not None:
        settings["min_stock_days"] = body.min_stock_days
    # Ключи сверх DEFAULT_SETTINGS (org.settings их не возвращает) — сохраняем,
    # не затирая при частичных POST'ах (например, только rate_window с /replenish).
    extra = analytics.extra_settings(org)
    if body.rate_window is not None:
        extra["rate_window"] = body.rate_window
    if body.lead_time_days is not None:
        extra["lead_time_days"] = body.lead_time_days
    for key in ("cover_mode", "order_cadence_days", "safety_days",
                "moq_units", "reserve_new_pct", "price_type_sale",
                "price_type_cost", "peak_periods"):
        val = getattr(body, key)
        if val is not None:
            extra[key] = val
    settings.update(extra)
    org.settings_json = json.dumps(settings, ensure_ascii=False)
    db.commit()
    analytics.invalidate(org.id)
    return {"ok": True, "settings": settings}


# ── Исключение позиций из аналитики (упаковка, сертификаты, расходники) ──────

@router.get("/exclusions")
def api_exclusions(
    q: str = "", ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)
):
    """Список исключённых баз (+ по q — поиск кандидатов среди участвующих)."""
    excluded = db.execute(
        select(Product.base_name, func.count(Product.id))
        .where(Product.org_id == ctx.org.id, Product.excluded.is_(True))
        .group_by(Product.base_name)
        .order_by(Product.base_name)
    ).all()
    result = {"excluded": [{"base_name": b, "variants": int(n)} for b, n in excluded]}
    query = (q or "").strip().lower()
    if len(query) >= 2:
        candidates = db.execute(
            select(Product.base_name)
            .where(
                Product.org_id == ctx.org.id,
                Product.excluded.is_(False),
                func.lower(Product.base_name).like(f"%{query}%"),
            )
            .group_by(Product.base_name)
            .order_by(Product.base_name)
            .limit(20)
        ).scalars().all()
        result["candidates"] = candidates
    return result


class ExclusionIn(BaseModel):
    base_name: str
    excluded: bool


@router.post("/exclusions")
def api_set_exclusion(
    body: ExclusionIn, ctx: AuthContext = Depends(require_owner_api), db: Session = Depends(get_db)
):
    """Включить/исключить базовую позицию (все размеры разом)."""
    rows = db.execute(
        select(Product).where(
            Product.org_id == ctx.org.id, Product.base_name == body.base_name
        )
    ).scalars().all()
    if not rows:
        raise HTTPException(status_code=404, detail="Позиция не найдена")
    for p in rows:
        p.excluded = body.excluded
    db.commit()
    analytics.invalidate(ctx.org.id)
    return {"ok": True, "base_name": body.base_name, "excluded": body.excluded, "variants": len(rows)}


@router.post("/warehouses/{warehouse_id}/toggle")
def api_toggle_warehouse(
    warehouse_id: int, ctx: AuthContext = Depends(require_owner_api), db: Session = Depends(get_db)
):
    wh = db.get(Warehouse, warehouse_id)
    if wh is None or wh.org_id != ctx.org.id:
        raise HTTPException(status_code=404, detail="Склад не найден")
    from app import ms_sync as _ms_sync
    if _ms_sync.is_running(ctx.org.id):
        # Ревью 21.08: набор складов — вход идущего синка; менять его на лету
        # значит получить историю, посчитанную по двум разным наборам.
        raise HTTPException(status_code=409, detail="Дождитесь окончания синхронизации")
    wh.active = not wh.active
    db.commit()
    analytics.invalidate(ctx.org.id)
    # Ревью 21.08: точка продолжения прерванной первичной загрузки привязана
    # к набору складов — после его смены продолжать нельзя, только пересборка.
    _ms_sync.clear_resume_point(ctx.org.id)
    # История остатков (StockDay) хранится суммой по складам, активным на момент
    # синка: смена набора складов требует пересборки истории, иначе цифры
    # остатков/дней-в-стоке молча остаются старыми до полного пересинка.
    needs_resync = (
        db.execute(
            select(Connection.id).where(
                Connection.org_id == ctx.org.id,
                Connection.kind == "moysklad",
                Connection.status == "active",
            )
        ).first()
        is not None
    )
    return {"ok": True, "id": wh.id, "active": wh.active, "needs_resync": needs_resync}


# ── Подключение источника данных ─────────────────────────────────────────────

@router.post("/connect/demo")
def api_connect_demo(ctx: AuthContext = Depends(require_owner_api), db: Session = Depends(get_db)):
    """Создаёт demo-подключение и сеет синтетические данные (детерминированно)."""
    org = ctx.org
    # Аудит 18.08: seed_demo начинается с clear_org_data — раньше кнопка демо
    # без всякого предохранителя стирала ВСЕ данные боевой организации,
    # включая невосстановимые заказы на производство и ручное «Заказано».
    # Блокируем только подключения, которые РЕАЛЬНО синкали данные (ревью
    # 18.08): строка kind='moysklad' создаётся со status='pending' уже при
    # сохранении токена — до первого синка данных нет, и демо безопасно
    # (пользователь, передумавший на онбординге, не попадает в тупик).
    ms_conn = db.execute(
        select(Connection).where(Connection.org_id == org.id,
                                 Connection.kind == "moysklad")
    ).scalars().first()
    if ms_conn is not None and (ms_conn.status == "active"
                                or ms_conn.last_sync_at is not None):
        raise HTTPException(
            status_code=409,
            detail="У организации уже подключён МойСклад с реальными данными — "
                   "демо-данные их безвозвратно сотрут (включая заказы на "
                   "производство и ручное «Заказано»). Если демо всё же нужно, "
                   "напишите в поддержку — поможем безопасно.")
    counters = seed_demo(db, org)
    conn = db.execute(
        select(Connection).where(Connection.org_id == org.id, Connection.kind == "demo")
    ).scalars().first()
    if conn is None:
        conn = Connection(org_id=org.id, kind="demo", token_enc="", config_json="{}")
        db.add(conn)
    conn.status = "active"
    conn.last_sync_at = datetime.utcnow()
    db.commit()
    analytics.invalidate(org.id)
    return {"ok": True, "seeded": counters}


class MoyskladConnectIn(BaseModel):
    token: str = Field(min_length=8)


@router.post("/connect/moysklad")
def api_connect_moysklad(
    body: MoyskladConnectIn,
    ctx: AuthContext = Depends(require_owner_api),
    db: Session = Depends(get_db),
):
    """Сохраняет шифрованный токен МойСклад; синхронизация — вне демо-скоупа."""
    org = ctx.org
    conn = db.execute(
        select(Connection).where(Connection.org_id == org.id, Connection.kind == "moysklad")
    ).scalars().first()
    if conn is None:
        conn = Connection(org_id=org.id, kind="moysklad", config_json="{}")
        db.add(conn)
    conn.token_enc = encrypt_token(body.token.strip())
    conn.status = "pending"
    db.commit()
    return {"ok": True, "note": "Синхронизация начнётся автоматически"}


# ── Онбординг-инструкции страниц (значок «?») ────────────────────────────────

class HintSeenIn(BaseModel):
    page: str = Field(min_length=1, max_length=64)


@router.get("/hints/seen")
def api_hints_seen(ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)):
    """Страницы, инструкции которых пользователь уже видел."""
    from app.models import UserHintSeen
    rows = db.execute(
        select(UserHintSeen.page).where(UserHintSeen.user_id == ctx.user.id)
    ).scalars().all()
    return {"seen": list(rows)}


@router.post("/hints/seen")
def api_hint_mark_seen(
    body: HintSeenIn,
    ctx: AuthContext = Depends(require_auth_api),
    db: Session = Depends(get_db),
):
    """Отметить инструкцию страницы просмотренной (идемпотентно)."""
    from app.models import UserHintSeen
    row = db.get(UserHintSeen, (ctx.user.id, body.page))
    if row is None:
        db.add(UserHintSeen(user_id=ctx.user.id, page=body.page))
        db.commit()
    return {"ok": True}


# ── Производства: основное + добавляемые (Китай / Москва / Екатеринбург…) ────

class ProductionIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    # Этапы канала сразу при создании: turnkey (Китай под ключ) |
    # fabric_sewing (своё производство: ткань → пошив). Можно задать позже.
    preset: str | None = None
    moq_units: int | None = Field(default=None, ge=0, le=10000)


class ProductionAssignIn(BaseModel):
    base_name: str = Field(min_length=1, max_length=255)
    production_id: int | None = None  # None = вернуть на основное производство


def _ensure_main_production(db: Session, org_id: int):
    """У организации всегда есть основное производство — создаём лениво."""
    from app.models import Production
    main = db.execute(
        select(Production).where(Production.org_id == org_id, Production.is_main.is_(True))
    ).scalars().first()
    if main is None:
        main = Production(org_id=org_id, name="Основное производство", is_main=True)
        db.add(main)
        db.commit()
        db.refresh(main)
    return main


@router.get("/productions")
def api_productions(ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)):
    """Список производств + распределение позиций (base_name -> production_id).

    Позиции без записи в assign — на основном производстве."""
    from app.models import Production, ProductionAssign
    _ensure_main_production(db, ctx.org.id)
    prods = db.execute(
        select(Production).where(Production.org_id == ctx.org.id)
        .order_by(Production.is_main.desc(), Production.id)
    ).scalars().all()
    valid = {p.id for p in prods}
    assigns = db.execute(
        select(ProductionAssign).where(ProductionAssign.org_id == ctx.org.id)
    ).scalars().all()
    fallback_lead = analytics.extra_settings(ctx.org)["lead_time_days"]
    return {
        "productions": [_production_out(p, fallback_lead) for p in prods],
        "assign": {a.base_name: a.production_id for a in assigns if a.production_id in valid},
    }


@router.post("/productions")
def api_production_create(
    body: ProductionIn, ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)
):
    """Добавить дополнительное производство (если заказами занимается другой отдел)."""
    from app.models import Production
    _ensure_main_production(db, ctx.org.id)
    from app import order_planner
    fallback_lead = analytics.extra_settings(ctx.org)["lead_time_days"]
    p = Production(org_id=ctx.org.id, name=body.name.strip(), is_main=False)
    if body.preset:
        raw = order_planner.STAGE_PRESETS.get(body.preset)
        if raw is None:
            raise HTTPException(422, "Неизвестный пресет этапов")
        p.stages_json = json.dumps(
            order_planner.normalize_stages(raw, fallback_lead), ensure_ascii=False
        )
    if body.moq_units is not None:
        p.moq_units = int(body.moq_units)
    db.add(p)
    db.commit()
    db.refresh(p)
    return _production_out(p, fallback_lead)


@router.post("/productions/assign")
def api_production_assign(
    body: ProductionAssignIn,
    ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db),
):
    """Перенести позицию на производство (production_id=null — на основное)."""
    from app.models import Production, ProductionAssign
    row = db.get(ProductionAssign, (ctx.org.id, body.base_name))
    if body.production_id is None:
        if row is not None:
            db.delete(row)
            db.commit()
        return {"ok": True}
    p = db.get(Production, body.production_id)
    if p is None or p.org_id != ctx.org.id:
        raise HTTPException(404, "Производство не найдено")
    if p.is_main:
        # на основное переносим удалением записи, а не ссылкой на него
        if row is not None:
            db.delete(row)
            db.commit()
        return {"ok": True}
    if row is None:
        db.add(ProductionAssign(org_id=ctx.org.id, base_name=body.base_name, production_id=p.id))
    else:
        row.production_id = p.id
    db.commit()
    return {"ok": True}


@router.post("/productions/{pid}")
def api_production_rename(
    pid: int, body: ProductionIn,
    ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db),
):
    """Переименовать производство (основное — тоже можно)."""
    from app.models import Production
    p = db.get(Production, pid)
    if p is None or p.org_id != ctx.org.id:
        raise HTTPException(404, "Производство не найдено")
    p.name = body.name.strip()
    db.commit()
    return {"id": p.id, "name": p.name, "is_main": p.is_main}


@router.delete("/productions/{pid}")
def api_production_delete(
    pid: int, ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)
):
    """Удалить дополнительное производство; его позиции возвращаются на основное."""
    from app.models import Production, ProductionAssign
    p = db.get(Production, pid)
    if p is None or p.org_id != ctx.org.id:
        raise HTTPException(404, "Производство не найдено")
    if p.is_main:
        raise HTTPException(400, "Основное производство удалить нельзя — его можно переименовать")
    db.query(ProductionAssign).filter(
        ProductionAssign.org_id == ctx.org.id, ProductionAssign.production_id == pid
    ).delete()
    db.delete(p)
    db.commit()
    return {"ok": True}


@router.post("/ordered/add")
def api_add_ordered(
    body: OrderedIn, ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)
):
    """«Заказ отправлен» со страницы «Заказ отдельной позиции»: ПРИБАВЛЯЕТ
    количество к «едет к нам» (в отличие от /ordered, который перезаписывает).
    Приёмка в МойСкладе или received-статус заказа спишут это количество."""
    row = db.get(OrderedQty, (ctx.org.id, body.base_name))
    if row is None:
        db.add(OrderedQty(org_id=ctx.org.id, base_name=body.base_name, qty=max(0, body.qty)))
        total = max(0, body.qty)
    else:
        row.qty = max(0, (row.qty or 0) + body.qty)
        total = row.qty
    db.commit()
    analytics.invalidate(ctx.org.id)
    return {"ok": True, "base_name": body.base_name, "qty_total": total}


# ── Мастер заказа: план под бюджет ───────────────────────────────────────────
# Анкета → план (волны, MOQ, этапы производства) → заказ. Ядро — чистая функция
# app/order_planner.py; здесь только транспорт и сохранение.

class OrderPlanIn(BaseModel):
    production_id: int | None = None
    eta_date: str | None = None            # когда товар нужен на складе
    budget: int = Field(default=0, ge=0)   # деньги на этот заказ, ₽
    budget_scope: str | None = None        # now (первый этап) | full (весь заказ)
    cadence_days: int | None = None        # как часто размещаются заказы
    safety_days: int | None = None
    strategy: str | None = None            # protect | balance | grow
    max_share_pct: int | None = None
    moq_units: int | None = None
    reserve_new_pct: int | None = None
    exclude_categories: list[str] = Field(default_factory=list)
    must_have: list[str] = Field(default_factory=list)
    # Новинки без истории продаж: владелец вписывает их руками
    # [{"name": "Пальто Осень", "qty": 30, "cost": 9000, "category": "Верхняя одежда"}]
    new_items: list[dict] = Field(default_factory=list)


def _plan(db: Session, ctx: AuthContext, body: OrderPlanIn) -> dict:
    from app import order_planner
    snap = analytics.get_snapshot(db, ctx.org)
    return order_planner.build_plan(db, ctx.org, snap, body.model_dump())


@router.post("/order-plan/preview")
def api_order_plan_preview(
    body: OrderPlanIn, ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)
):
    """Предпросмотр плана: ничего не сохраняет, дергается на каждое изменение анкеты."""
    return _plan(db, ctx, body)


@router.post("/order-plan")
def api_order_plan_save(
    body: OrderPlanIn, ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)
):
    """Сохранить план (бриф + вывод системы + результат) для истории и предзаполнения."""
    from app.models import OrderPlan
    plan = _plan(db, ctx, body)
    row = OrderPlan(
        org_id=ctx.org.id,
        status="draft",
        brief_json=json.dumps(plan["brief"], ensure_ascii=False),
        computed_json=json.dumps(
            {
                "cover_days": plan["cover_days"],
                "order_date": plan["order_date"],
                "covered_until": plan["covered_until"],
                "stages": plan["stages"],
                "lead_days": plan["lead_days"],
            },
            ensure_ascii=False,
        ),
        result_json=json.dumps(
            {"items": plan["items"], "totals": plan["totals"],
             "spent": plan["spent"], "lost_revenue": plan["lost_revenue"]},
            ensure_ascii=False,
        ),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"ok": True, "id": row.id, "plan": plan}


@router.get("/order-plan/last")
def api_order_plan_last(
    ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)
):
    """Последний бриф — чтобы второй заказ оформлялся в два клика."""
    from app.models import OrderPlan
    row = db.execute(
        select(OrderPlan).where(OrderPlan.org_id == ctx.org.id)
        .order_by(OrderPlan.created_at.desc(), OrderPlan.id.desc())
    ).scalars().first()
    if row is None:
        return {"brief": None}
    return {"brief": row.brief, "id": row.id, "created_at": row.created_at.isoformat()}


class PlanApplyIn(BaseModel):
    name: str = Field(default="", max_length=255)


@router.post("/order-plan/{plan_id}/apply")
def api_order_plan_apply(
    plan_id: int, body: PlanApplyIn,
    ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db),
):
    """План → «Заказ на производство» (дальше существующий путь: в МойСклад, Excel)."""
    from app.models import OrderPlan
    row = db.get(OrderPlan, plan_id)
    if row is None or row.org_id != ctx.org.id:
        raise HTTPException(404, "План не найден")
    if row.production_order_id:
        raise HTTPException(409, "По этому плану заказ уже создан")
    try:
        result = json.loads(row.result_json or "{}")
    except ValueError:
        result = {}
    items = [
        {"base_name": i["base_name"], "qty": int(i["qty"]),
         "sizes": i.get("sizes") or {}, "cost": float(i.get("cost_price") or 0)}
        for i in (result.get("items") or []) if int(i.get("qty") or 0) > 0
    ]
    # Новинки, вписанные вручную, — такие же строки заказа (в МойСкладе у них
    # может ещё не быть карточки: писбэк вернёт их в списке unmatched).
    brief = row.brief
    for n in (brief.get("new_items") or []):
        if int(n.get("qty") or 0) > 0:
            items.append({"base_name": n["name"], "qty": int(n["qty"]),
                          "sizes": {}, "cost": float(n.get("cost") or 0)})
    if not items:
        raise HTTPException(422, "В плане нет позиций с количеством > 0")
    order = ProductionOrder(
        org_id=ctx.org.id,
        name=(body.name.strip() or f"Заказ от {datetime.now():%d.%m.%Y}"),
        eta_date=brief.get("eta_date"),
        status="draft",
        items_json=json.dumps(items, ensure_ascii=False),
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    row.production_order_id = order.id
    row.status = "applied"
    db.commit()
    analytics.invalidate(ctx.org.id)
    return {"ok": True, "order_id": order.id, "status": "draft"}


class ProductionSetupIn(BaseModel):
    """Настройка канала производства: этапы и минимальная партия.

    preset — быстрый выбор в анкете: turnkey (под ключ) | fabric_sewing (ткань → пошив).
    stages переопределяет preset, если передан.
    """
    preset: str | None = None
    stages: list[dict] | None = None
    moq_units: int | None = Field(default=None, ge=0, le=10000)
    # Ритм заказов В ЭТОТ канал (0 = общая настройка организации): своё
    # производство часто догружают еженедельно, Китай заказывают раз в сезон.
    cadence_days: int | None = Field(default=None, ge=0, le=365)


@router.post("/productions/{pid}/setup")
def api_production_setup(
    pid: int, body: ProductionSetupIn,
    ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db),
):
    from app import order_planner
    from app.models import Production
    p = db.get(Production, pid)
    if p is None or p.org_id != ctx.org.id:
        raise HTTPException(404, "Производство не найдено")
    raw = body.stages
    if raw is None and body.preset:
        raw = order_planner.STAGE_PRESETS.get(body.preset)
        if raw is None:
            raise HTTPException(422, "Неизвестный пресет этапов")
    if raw is not None:
        stages = order_planner.normalize_stages(
            raw, analytics.extra_settings(ctx.org)["lead_time_days"]
        )
        p.stages_json = json.dumps(stages, ensure_ascii=False)
    if body.moq_units is not None:
        p.moq_units = int(body.moq_units)
    if body.cadence_days is not None:
        p.cadence_days = int(body.cadence_days)
    db.commit()
    analytics.invalidate(ctx.org.id)
    return _production_out(p, analytics.extra_settings(ctx.org)["lead_time_days"])


def _production_out(p, fallback_lead: int) -> dict:
    from app import order_planner
    stages = order_planner.normalize_stages(p.stages, fallback_lead)
    return {
        "id": p.id, "name": p.name, "is_main": p.is_main,
        "stages": stages,
        "lead_days": order_planner.lead_days(stages),
        "moq_units": int(p.moq_units or 0),
        "cadence_days": int(p.cadence_days or 0),
        "prepay_now_share": round(order_planner.prepay_share_total(stages), 4),
        "staged": len(stages) > 1,
    }
