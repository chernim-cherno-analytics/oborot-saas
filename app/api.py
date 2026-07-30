"""JSON API. Все эндпоинты — под сессией; данные строго текущей организации."""
import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import analytics
from app.auth import AuthContext, require_auth_api, require_owner_api
from app.crypto import encrypt_token
from app.db import get_db
from app.demo_seed import seed_demo
from app.models import Connection, Membership, OrderedQty, ProductionOrder, User, Warehouse

router = APIRouter(prefix="/api")


# ── Аналитика ────────────────────────────────────────────────────────────────

@router.get("/summary")
def api_summary(ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)):
    return analytics.build_summary(analytics.get_snapshot(db, ctx.org))


@router.get("/replenish")
def api_replenish(ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)):
    return analytics.build_replenish(analytics.get_snapshot(db, ctx.org))


@router.get("/turnover")
def api_turnover(ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)):
    return analytics.build_turnover(analytics.get_snapshot(db, ctx.org))


@router.get("/stocks")
def api_stocks(ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)):
    return analytics.build_stocks(analytics.get_snapshot(db, ctx.org))


# ── Заказы на производство ───────────────────────────────────────────────────

class OrderItemIn(BaseModel):
    base_name: str
    qty: int = Field(ge=0)
    sizes: dict[str, int] = Field(default_factory=dict)
    cost: float = 0.0


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
    order = ProductionOrder(
        org_id=ctx.org.id,
        name=name,
        eta_date=body.eta_date,
        status="draft",
        items_json=json.dumps([i.model_dump() for i in items], ensure_ascii=False),
    )
    db.add(order)
    # Заказанное автоматически попадает в «едет к нам».
    for item in items:
        row = db.get(OrderedQty, (ctx.org.id, item.base_name))
        if row is None:
            db.add(OrderedQty(org_id=ctx.org.id, base_name=item.base_name, qty=item.qty))
        else:
            row.qty += item.qty
    db.commit()
    analytics.invalidate(ctx.org.id)
    return {"ok": True, "id": order.id}


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
    return {
        "org": {
            "name": org.name,
            "plan": org.plan,
            "trial_ends_at": org.trial_ends_at.isoformat() if org.trial_ends_at else None,
        },
        "thresholds": settings["thresholds"],
        "horizon_days": settings["horizon_days"],
        "min_stock_days": settings["min_stock_days"],
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
    }


class ThresholdsIn(BaseModel):
    weak: int = Field(gt=0)
    dull: int = Field(gt=0)
    good: int = Field(gt=0)


class SettingsIn(BaseModel):
    thresholds: ThresholdsIn | None = None
    horizon_days: int | None = Field(default=None, ge=7, le=365)
    min_stock_days: int | None = Field(default=None, ge=0, le=100)


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
    org.settings_json = json.dumps(settings, ensure_ascii=False)
    db.commit()
    analytics.invalidate(org.id)
    return {"ok": True, "settings": settings}


@router.post("/warehouses/{warehouse_id}/toggle")
def api_toggle_warehouse(
    warehouse_id: int, ctx: AuthContext = Depends(require_owner_api), db: Session = Depends(get_db)
):
    wh = db.get(Warehouse, warehouse_id)
    if wh is None or wh.org_id != ctx.org.id:
        raise HTTPException(status_code=404, detail="Склад не найден")
    wh.active = not wh.active
    db.commit()
    analytics.invalidate(ctx.org.id)
    return {"ok": True, "id": wh.id, "active": wh.active}


# ── Подключение источника данных ─────────────────────────────────────────────

@router.post("/connect/demo")
def api_connect_demo(ctx: AuthContext = Depends(require_owner_api), db: Session = Depends(get_db)):
    """Создаёт demo-подключение и сеет синтетические данные (детерминированно)."""
    org = ctx.org
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
