"""PROD-1 «Производство»: API доски запусков (решение Влада 17.11.2026).

Арендатор берётся только из сессии. Чужой заказ, отметка или цех дают 404 с
одним и тем же текстом, существует строка у другой организации или нет.
Чтение: любая роль. Запись: право `orders_manage` («Вести заказы: статусы и
приёмка»), поверх этого общий гейт подписки и общий CSRF на изменяющие /api/*.
Слой не меняет заказы, приёмки, «Едет» и расчёты (см. app/production_tracking.py).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import production_tracking as pt
from app.auth import AuthContext, require_auth_api, require_perm
from app.db import get_db

_MAX_ID = 2_147_483_647

router = APIRouter(prefix="/api/production", tags=["production"])


def _fail(err: pt.TrackingError):
    raise HTTPException(status_code=err.status, detail=str(err))


class MarkIn(BaseModel):
    stage_key: str = Field(..., min_length=1, max_length=16)
    at: str | None = Field(None, max_length=10)
    qty: float | None = None
    note: str = Field("", max_length=600)


class StageIn(BaseModel):
    key: str = Field("", max_length=16)
    name: str = Field("", max_length=200)


class StagesIn(BaseModel):
    production_id: int = Field(0, ge=0, le=_MAX_ID)
    preset: str = Field("custom", max_length=16)
    stages: list[StageIn] = Field(default_factory=list, max_length=20)


@router.get("/board")
def api_board(ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)):
    data = pt.board(db, ctx.org)
    data["can_mark"] = bool(ctx.can("orders_manage"))
    return data


@router.get("/orders/{order_id}")
def api_order(order_id: int = Path(..., ge=1, le=_MAX_ID),
              ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)):
    try:
        data = pt.order_detail(db, ctx.org, order_id)
    except pt.TrackingError as err:
        _fail(err)
    data["can_mark"] = bool(ctx.can("orders_manage"))
    return data


@router.post("/orders/{order_id}/marks")
def api_add_mark(body: MarkIn, order_id: int = Path(..., ge=1, le=_MAX_ID),
                 ctx: AuthContext = Depends(require_perm("orders_manage")),
                 db: Session = Depends(get_db)):
    try:
        res = pt.add_mark(db, ctx.org, ctx.user.id, order_id, body.stage_key.strip(),
                          body.at, body.qty, body.note)
    except pt.TrackingError as err:
        db.rollback()
        _fail(err)
    db.commit()
    return res


@router.delete("/marks/{mark_id}")
def api_delete_mark(mark_id: int = Path(..., ge=1, le=_MAX_ID),
                    ctx: AuthContext = Depends(require_perm("orders_manage")),
                    db: Session = Depends(get_db)):
    try:
        res = pt.delete_mark(db, ctx.org.id, mark_id)
    except pt.TrackingError as err:
        db.rollback()
        _fail(err)
    db.commit()
    return res


@router.put("/stages")
def api_set_stages(body: StagesIn,
                   ctx: AuthContext = Depends(require_perm("orders_manage")),
                   db: Session = Depends(get_db)):
    try:
        res = pt.set_track(db, ctx.org.id, body.production_id, body.preset,
                           [s.model_dump() for s in body.stages])
    except pt.TrackingError as err:
        db.rollback()
        _fail(err)
    db.commit()
    return res
