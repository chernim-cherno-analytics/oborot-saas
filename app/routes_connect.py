"""Роуты подключения и синхронизации МойСклад (онбординг-мастер).

POST /api/connect/moysklad          — проверить токен запросом к МС, сохранить (Fernet)
GET  /api/connect/moysklad/stores   — склады аккаунта МойСклад
POST /api/connect/moysklad/stores   — выбрать склады (Warehouse-записи)
POST /api/sync/initial              — фоновая первичная синхронизация
POST /api/sync/run                  — инкрементальный синк (остатки+цены+продажи 3 дн.)
GET  /api/sync/status               — прогресс для онбординга (поллинг)

Все ручки — только для владельца организации (require_owner_api).
"""
import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import ms_sync
from app.auth import AuthContext, require_owner_api
from app.crypto import decrypt_token, encrypt_token
from app.db import get_db
from app.models import Connection, Warehouse
from app.ms_client import MoySkladClient

# app/api.py (демо-скоуп) регистрирует заглушку POST /api/connect/moysklad,
# а api_router включается в приложение раньше этого роутера — заглушка
# перекрыла бы боевую ручку. api.py менять нельзя (зона другого агента),
# поэтому снимаем ровно этот маршрут с его роутера при импорте (main.py
# импортирует routes_connect ДО include_router).
from app.api import router as _api_router

_api_router.routes = [
    r for r in _api_router.routes if getattr(r, "path", "") != "/api/connect/moysklad"
]

router = APIRouter(prefix="/api")

TOKEN_HINT = ("МойСклад не принял токен. Проверьте, что токен скопирован целиком: "
              "МойСклад → Настройки → Обмен данными → Токены API.")
NETWORK_HINT = ("Не удалось связаться с МойСклад. Проверьте интернет-соединение "
                "и попробуйте ещё раз через минуту.")


def _get_ms_connection(db: Session, org_id: int) -> Connection | None:
    return db.execute(
        select(Connection).where(
            Connection.org_id == org_id, Connection.kind == "moysklad"
        )
    ).scalars().first()


def _require_token(db: Session, org_id: int) -> str:
    conn = _get_ms_connection(db, org_id)
    token = decrypt_token(conn.token_enc) if conn and conn.token_enc else None
    if not token:
        raise HTTPException(
            status_code=409,
            detail="Сначала подключите МойСклад: вставьте API-токен на шаге 1.",
        )
    return token


# ── Подключение токена ───────────────────────────────────────────────────────

class MoyskladConnectIn(BaseModel):
    token: str = Field(min_length=8)


@router.post("/connect/moysklad")
async def api_connect_moysklad(
    body: MoyskladConnectIn,
    ctx: AuthContext = Depends(require_owner_api),
    db: Session = Depends(get_db),
):
    """Проверяет токен живым запросом к МойСклад и сохраняет шифрованным."""
    token = body.token.strip()
    async with MoySkladClient(token) as client:
        try:
            await client.get("/context/employee")
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code in (401, 403):
                raise HTTPException(status_code=400, detail=TOKEN_HINT)
            raise HTTPException(
                status_code=502,
                detail=f"МойСклад ответил ошибкой {code}. Попробуйте позже.",
            )
        except httpx.HTTPError:
            raise HTTPException(status_code=502, detail=NETWORK_HINT)

    conn = _get_ms_connection(db, ctx.org.id)
    if conn is None:
        conn = Connection(org_id=ctx.org.id, kind="moysklad", config_json="{}")
        db.add(conn)
    conn.token_enc = encrypt_token(token)
    conn.status = "pending"
    db.commit()
    return {"ok": True, "note": "Токен проверен. Осталось выбрать склады."}


# ── Склады ───────────────────────────────────────────────────────────────────

@router.get("/connect/moysklad/stores")
async def api_moysklad_stores(
    ctx: AuthContext = Depends(require_owner_api), db: Session = Depends(get_db)
):
    """Список складов аккаунта МойСклад + текущий выбор (если был)."""
    token = _require_token(db, ctx.org.id)
    async with MoySkladClient(token) as client:
        try:
            stores = await client.fetch_stores()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 403):
                raise HTTPException(status_code=400, detail=TOKEN_HINT)
            raise HTTPException(
                status_code=502,
                detail=f"МойСклад ответил ошибкой {exc.response.status_code}.",
            )
        except httpx.HTTPError:
            raise HTTPException(status_code=502, detail=NETWORK_HINT)

    existing = {
        w.ext_id: w
        for w in db.execute(
            select(Warehouse).where(Warehouse.org_id == ctx.org.id)
        ).scalars()
    }
    out = []
    for store in stores:
        ext_id = store.get("id") or ""
        if not ext_id:
            continue
        known = existing.get(ext_id)
        out.append({
            "ext_id": ext_id,
            "name": store.get("name") or ext_id,
            # по умолчанию предлагаем включить все склады
            "selected": known.active if known is not None else True,
        })
    return {"stores": out}


class StoresSelectIn(BaseModel):
    ext_ids: list[str] = Field(min_length=1)


@router.post("/connect/moysklad/stores")
async def api_moysklad_stores_select(
    body: StoresSelectIn,
    ctx: AuthContext = Depends(require_owner_api),
    db: Session = Depends(get_db),
):
    """Сохраняет выбор складов: Warehouse на каждый склад МС, выбранные active=1."""
    token = _require_token(db, ctx.org.id)
    async with MoySkladClient(token) as client:
        try:
            stores = await client.fetch_stores()
        except httpx.HTTPError:
            raise HTTPException(status_code=502, detail=NETWORK_HINT)

    ms_ids = {s.get("id"): (s.get("name") or s.get("id")) for s in stores if s.get("id")}
    selected = [ext for ext in body.ext_ids if ext in ms_ids]
    if not selected:
        raise HTTPException(
            status_code=422,
            detail="Выберите хотя бы один склад из списка МойСклад.",
        )

    existing = {
        w.ext_id: w
        for w in db.execute(
            select(Warehouse).where(Warehouse.org_id == ctx.org.id)
        ).scalars()
    }
    for ext_id, name in ms_ids.items():
        row = existing.get(ext_id)
        if row is None:
            row = Warehouse(org_id=ctx.org.id, ext_id=ext_id, name=name)
            db.add(row)
        row.name = name
        row.active = ext_id in selected
    db.commit()
    return {"ok": True, "active": len(selected), "total": len(ms_ids)}


# ── Синхронизация ────────────────────────────────────────────────────────────

@router.post("/sync/initial")
def api_sync_initial(
    ctx: AuthContext = Depends(require_owner_api), db: Session = Depends(get_db)
):
    """Фоновый запуск первичной синхронизации (история остатков + продажи)."""
    _require_token(db, ctx.org.id)
    active = db.execute(
        select(Warehouse).where(
            Warehouse.org_id == ctx.org.id,
            Warehouse.active.is_(True),
            Warehouse.ext_id != "",
        )
    ).scalars().all()
    if not active:
        raise HTTPException(status_code=409, detail="Сначала выберите склады.")
    if not ms_sync.start_sync(ctx.org.id, mode="initial"):
        raise HTTPException(status_code=409, detail="Синхронизация уже идёт.")
    return {
        "ok": True,
        "estimate_minutes": ms_sync.estimate_minutes(ms_sync.HISTORY_DAYS, len(active)),
    }


@router.post("/sync/run")
def api_sync_run(
    ctx: AuthContext = Depends(require_owner_api), db: Session = Depends(get_db)
):
    """Инкрементальный синк: остатки на сегодня, цены, продажи за 3 дня."""
    _require_token(db, ctx.org.id)
    active = db.execute(
        select(Warehouse.id).where(
            Warehouse.org_id == ctx.org.id,
            Warehouse.active.is_(True),
            Warehouse.ext_id != "",
        )
    ).first()
    if active is None:
        raise HTTPException(status_code=409, detail="Сначала выберите склады.")
    if not ms_sync.start_sync(ctx.org.id, mode="incremental"):
        raise HTTPException(status_code=409, detail="Синхронизация уже идёт.")
    return {"ok": True}


@router.get("/sync/status")
def api_sync_status(ctx: AuthContext = Depends(require_owner_api)):
    """Состояние синхронизации для онбординга (поллинг раз в 1–2 секунды)."""
    return ms_sync.get_status(ctx.org.id)
