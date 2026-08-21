"""Роуты подключения и синхронизации МойСклад (онбординг-мастер).

POST /api/connect/moysklad          — проверить токен запросом к МС, сохранить (Fernet)
GET  /api/connect/moysklad/stores   — склады аккаунта МойСклад
POST /api/connect/moysklad/stores   — выбрать склады (Warehouse-записи)
POST /api/sync/initial              — фоновая первичная синхронизация (прогрессивная, П1)
POST /api/sync/run                  — инкрементальный синк (остатки+цены+продажи 3 дн.)
GET  /api/sync/status               — прогресс для онбординга (поллинг)
POST /api/orders/{id}/push-to-ms    — создать «Заказ поставщику» в МойСклад
GET  /api/orders/{id}/ms-doc        — ссылка на созданный документ МС (если был)
GET  /api/notify/settings           — Telegram-настройки + имя бота для инструкции
POST /api/notify/settings           — сохранить chat_id и флаги уведомлений
POST /api/notify/test               — тестовое сообщение «Оборот подключён»

Все ручки — только для владельца организации (require_owner_api).
"""
import html
import time as _time

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import ms_sync, ms_writeback, notify
from app.auth import AuthContext, require_auth_api, require_owner_api
from app.crypto import decrypt_token, encrypt_token
from app.db import get_db
from app.models import Connection, ProductionOrder, Warehouse
from app.ms_client import MoySkladClient

# Аддитивная мини-миграция: у баз, созданных до фичи обратной записи,
# в production_orders нет колонок ms_doc_href/ms_doc_name — добавляем.
# Свежая БД (таблиц ещё нет) — no-op, колонки создаст init_db из модели.
ms_writeback.ensure_schema()
# …и ms_qty в ordered_qty («едет к нам» из заказов поставщику МойСклад).
ms_sync.ensure_schema()

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

# Пометка «идёт отправка в МойСклад» в ms_doc_href: pending:<epoch-старта>.
# Несёт время → зависшую дольше TTL отправку можно переиграть (воркер умер
# между захватом лока и сетевым вызовом), не создавая дубль финансового документа.
# Константа живёт в ms_writeback: по ней же api.py отличает реально
# отправленные заказы (is_pushed) от помеченных «идёт отправка».
_PENDING_PREFIX = ms_writeback.PENDING_PREFIX
_PENDING_TTL_SEC = 180

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
    has_warehouses = conn is not None and db.execute(
        select(Warehouse.id).where(
            Warehouse.org_id == ctx.org.id,
            Warehouse.active.is_(True),
            Warehouse.ext_id != "",
        )
    ).first() is not None
    if conn is None:
        conn = Connection(org_id=ctx.org.id, kind="moysklad", config_json="{}")
        db.add(conn)
    conn.token_enc = encrypt_token(token)
    resume_pending = has_warehouses and ms_sync.has_resume_point(ctx.org.id)
    if has_warehouses and (conn.status == "active" or resume_pending
                           or ms_sync.needs_full_rebuild(ctx.org.id)):
        # Инцидент 21.08: смена токена из Настроек сбрасывала status в pending —
        # планировщик синкает только active, кнопки «синхронизировать» в
        # Настройках не было, и пользователь застревал до ручного /onboarding.
        # Живое подключение (active) — статус не трогаем и запускаем инкремент;
        # прерванная первичная загрузка — продолжаем её. Ревью 21.08: орг,
        # который ещё ни разу не синкался (pending без точки продолжения),
        # НЕ стартуем — иначе он стал бы active с одним днём истории.
        db.commit()
        started = ms_sync.start_sync(
            ctx.org.id, "initial" if resume_pending else "incremental")
        note = ("Токен обновлён, синхронизация запущена." if started
                else "Токен обновлён. Синхронизация уже идёт.")
        return {"ok": True, "note": note, "sync_started": bool(started)}
    conn.status = "pending"
    db.commit()
    note = ("Токен проверен. Запустите синхронизацию." if has_warehouses
            else "Токен проверен. Осталось выбрать склады.")
    return {"ok": True, "note": note, "sync_started": False}


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
    if ms_sync.is_running(ctx.org.id):
        raise HTTPException(status_code=409, detail="Дождитесь окончания синхронизации")
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
    # Ревью 21.08: частичная история прерванной первичной загрузки считалась
    # по старому набору складов — продолжать её нельзя.
    ms_sync.clear_resume_point(ctx.org.id)
    return {"ok": True, "active": len(selected), "total": len(ms_ids)}


# ── Синхронизация ────────────────────────────────────────────────────────────

@router.post("/sync/initial")
def api_sync_initial(
    ctx: AuthContext = Depends(require_owner_api), db: Session = Depends(get_db)
):
    """Фоновый запуск первичной синхронизации.

    Деплой П1: загрузка прогрессивная — сервис открывается через секунды
    (товары, остатки на сегодня, окно INITIAL_WINDOW_DAYS), история за год
    догружается фоном; прогресс — /api/sync/progress и полоска под шапкой.
    """
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
    # force_full: настоящая пересборка с нуля, а не продолжение прерванной.
    if not ms_sync.start_sync(ctx.org.id, mode="initial", force_full=True):
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


# ── Обратная запись заказа в МойСклад ────────────────────────────────────────

def _order_of_org(db: Session, org_id: int, order_id: int) -> ProductionOrder:
    order = db.get(ProductionOrder, order_id)
    if order is None or order.org_id != org_id:
        raise HTTPException(status_code=404, detail="Заказ не найден")
    return order


def _release_push_lock(db: Session, order_id: int) -> None:
    """Снимает временную пометку отправки (pending:*) при сбое.

    Возвращает заказ в состояние «можно отправить», но только если документ
    не успел создаться (href всё ещё начинается с pending:) — чтобы не затереть
    реальную ссылку в редком случае гонки.
    """
    from sqlalchemy import update as _sa_update
    try:
        db.rollback()
        db.execute(
            _sa_update(ProductionOrder)
            .where(
                ProductionOrder.id == order_id,
                ProductionOrder.ms_doc_href.like(f"{_PENDING_PREFIX}%"),
            )
            .values(ms_doc_href="")
        )
        db.commit()
    except Exception:  # noqa: BLE001 — освобождение лока не должно маскировать исходную ошибку
        db.rollback()


def _clean_href(href: str | None) -> str:
    """Внутренняя пометка pending:* наружу не показывается — только реальная ссылка."""
    h = href or ""
    return "" if h.startswith(_PENDING_PREFIX) else h


def _ms_doc_out(order: ProductionOrder) -> dict:
    href = _clean_href(order.ms_doc_href)
    return {
        "ms_doc_href": href,
        "ms_doc_name": order.ms_doc_name if href else "",
        "ms_doc_ui_url": ms_writeback.ui_url(href=href) if href else "",
    }


@router.get("/orders/{order_id}/ms-doc")
def api_order_ms_doc(
    order_id: int,
    ctx: AuthContext = Depends(require_auth_api),
    db: Session = Depends(get_db),
):
    """Ссылка на документ МойСклад, созданный из заказа (пусто, если не было)."""
    return _ms_doc_out(_order_of_org(db, ctx.org.id, order_id))


@router.post("/orders/{order_id}/push-to-ms")
async def api_order_push_to_ms(
    order_id: int,
    ctx: AuthContext = Depends(require_owner_api),
    db: Session = Depends(get_db),
):
    """Создаёт в МойСклад документ «Заказ поставщику» из позиций заказа.

    Идемпотентность: повторная отправка при заполненном ms_doc_href — 409
    «уже отправлен» со ссылкой на существующий документ. Позиции, не нашедшие
    вариант в МС, не валят заказ — возвращаются списком unmatched.
    """
    order = _order_of_org(db, ctx.org.id, order_id)
    if order.status not in ("draft", "sent"):
        raise HTTPException(
            status_code=422,
            detail="Заказ уже принят на склад — отправлять его в МойСклад поздно.",
        )
    current = order.ms_doc_href or ""
    if current and not current.startswith(_PENDING_PREFIX):
        # Реальная ссылка на документ — уже отправлен.
        name = f" ({order.ms_doc_name})" if order.ms_doc_name else ""
        return JSONResponse(
            status_code=409,
            content={
                "detail": f"Заказ уже отправлен в МойСклад{name}. "
                          "Откройте существующий документ по ссылке.",
                **_ms_doc_out(order),
            },
        )
    now = int(_time.time())
    if current.startswith(_PENDING_PREFIX):
        # Идёт отправка. Если пометка свежая — второй клик отклоняем. Если
        # «зависла» дольше PENDING_TTL (воркер умер между захватом лока и
        # сетевым вызовом) — считаем сорванной и разрешаем переотправку.
        try:
            started = int(current.split(":", 1)[1])
        except (ValueError, IndexError):
            started = 0
        if now - started < _PENDING_TTL_SEC:
            return JSONResponse(
                status_code=409,
                content={
                    "detail": "Заказ уже отправляется в МойСклад — дождитесь "
                              "завершения и обновите страницу.",
                    **_ms_doc_out(order),
                },
            )
    # Атомарный захват «замка» ДО любых сетевых вызовов: помечаем заказ как
    # отправляемый одним условным UPDATE с проверкой прежнего значения (CAS).
    # Второй одновременный клик/ретрай не обновит ни строки (значение уже
    # изменилось) и получит 409 — иначе гонка создала бы ДВА финансовых
    # документа. Метка несёт время старта → возможна переотправка после сбоя.
    from sqlalchemy import update as _sa_update
    lock = db.execute(
        _sa_update(ProductionOrder)
        .where(
            ProductionOrder.id == order.id,
            ProductionOrder.ms_doc_href == current,  # CAS: ровно то, что прочитали
        )
        .values(ms_doc_href=f"{_PENDING_PREFIX}{now}")
    )
    db.commit()
    if lock.rowcount == 0:
        db.refresh(order)
        return JSONResponse(
            status_code=409,
            content={
                "detail": "Заказ уже отправляется в МойСклад — дождитесь завершения "
                          "и обновите страницу.",
                **_ms_doc_out(order),
            },
        )
    try:
        result = await ms_writeback.push_order(db, ctx.org.id, order)
    except ms_writeback.WritebackError as exc:
        _release_push_lock(db, order.id)
        raise HTTPException(status_code=exc.status, detail=exc.detail)
    except httpx.HTTPStatusError as exc:
        _release_push_lock(db, order.id)
        code = exc.response.status_code
        if code in (401, 403):
            raise HTTPException(status_code=400, detail=TOKEN_HINT)
        raise HTTPException(
            status_code=502,
            detail=f"МойСклад ответил ошибкой {code}. Документ не создан — "
                   "попробуйте ещё раз позже.",
        )
    except httpx.HTTPError:
        _release_push_lock(db, order.id)
        raise HTTPException(status_code=502, detail=NETWORK_HINT)
    db.commit()
    # Аудит 18.08: push_order переносит вклад заказа между qty и ms_qty
    # (а при черновике/частичном матче меняет и сумму «едет к нам») — без
    # инвалидации страницы 10 минут отдавали старый снапшот и потребность.
    from app import analytics as _an
    _an.invalidate(ctx.org.id)
    return result


# ── Telegram-уведомления ─────────────────────────────────────────────────────

def _notify_settings_out(row) -> dict:
    return {
        "tg_chat_id": row.tg_chat_id,
        "tg_enabled": row.tg_enabled,
        "alerts_stockout": row.alerts_stockout,
        "alerts_overstock": row.alerts_overstock,
        "digest_enabled": row.digest_enabled,
        # для инструкции в настройках: имя бота и признак, что токен задан
        "bot_name": notify.bot_name(),
        "bot_configured": bool(notify.bot_token()),
    }


@router.get("/notify/settings")
def api_notify_settings_get(
    ctx: AuthContext = Depends(require_owner_api), db: Session = Depends(get_db)
):
    """Текущие Telegram-настройки организации (создаёт дефолтные при первом заходе)."""
    return _notify_settings_out(notify.get_settings(db, ctx.org.id))


class NotifySettingsIn(BaseModel):
    tg_chat_id: str = Field(default="", max_length=64)
    tg_enabled: bool = False
    alerts_stockout: bool = True
    alerts_overstock: bool = True
    digest_enabled: bool = True


@router.post("/notify/settings")
def api_notify_settings_save(
    body: NotifySettingsIn,
    ctx: AuthContext = Depends(require_owner_api),
    db: Session = Depends(get_db),
):
    """Сохраняет chat_id и флаги уведомлений."""
    chat_id = body.tg_chat_id.strip()
    if body.tg_enabled and not chat_id:
        raise HTTPException(
            status_code=422,
            detail="Чтобы включить уведомления, укажите chat_id чата с ботом.",
        )
    row = notify.get_settings(db, ctx.org.id)
    row.tg_chat_id = chat_id
    row.tg_enabled = body.tg_enabled
    row.alerts_stockout = body.alerts_stockout
    row.alerts_overstock = body.alerts_overstock
    row.digest_enabled = body.digest_enabled
    db.commit()
    return {"ok": True, **_notify_settings_out(row)}


class NotifyTestIn(BaseModel):
    # chat_id можно передать явно — проверить до сохранения настроек
    tg_chat_id: str = Field(default="", max_length=64)


@router.post("/notify/test")
def api_notify_test(
    body: NotifyTestIn | None = None,
    ctx: AuthContext = Depends(require_owner_api),
    db: Session = Depends(get_db),
):
    """Шлёт тестовое сообщение «Оборот подключён» в указанный/сохранённый чат."""
    if not notify.bot_token():
        raise HTTPException(
            status_code=503,
            detail="Telegram-бот не настроен на сервере (нет OBOROT_TG_BOT_TOKEN). "
                   "Напишите в поддержку.",
        )
    chat_id = (body.tg_chat_id.strip() if body else "") or \
        notify.get_settings(db, ctx.org.id).tg_chat_id.strip()
    if not chat_id:
        raise HTTPException(
            status_code=422,
            detail="Укажите chat_id: создайте чат с ботом, отправьте /start "
                   "и вставьте свой chat_id в поле выше.",
        )
    ok, err = notify.send_message(
        chat_id,
        f"✅ <b>Оборот подключён</b>\nОрганизация: {html.escape(ctx.org.name)}\n"
        "Сюда будет приходить ежедневный дайджест по остаткам и продажам.",
    )
    if not ok:
        raise HTTPException(status_code=502, detail=err)
    return {"ok": True}
