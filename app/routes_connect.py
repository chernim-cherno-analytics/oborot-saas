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
from fastapi import APIRouter, Depends, HTTPException, Path
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import ms_sync, ms_writeback, notify, subscription
from app.auth import AuthContext, require_auth_api, require_owner_api
from app.crypto import decrypt_token, encrypt_token
from app.db import get_db
from app.models import Connection, ProductionOrder, Warehouse
from app.ms_client import MoySkladClient

# Аддитивные мини-миграции ms_writeback.ensure_schema()/ms_sync.ensure_schema()
# раньше запускались прямо здесь, на импорте модуля — до include_router, то
# есть до старта приложения. Обращение к базе на импорте было опасно само по
# себе (Д4, ревью 22.08): достаточно было запустить несколько воркеров разом,
# чтобы 2-3 из них упали ещё до принятия первого запроса. Теперь обе миграции
# выполняются в db.init_db() вместе с остальными — см. app/main.py:_startup.


# Границы для числовых id в пути: без них слишком большое число (например,
# /api/orders/999999999999999999999/ms-doc) валит SQLite (OverflowError при
# попытке положить его в INTEGER-колонку) вместо аккуратного 422.
# ВАЖНО: один и тот же объект Path(...) нельзя переиспользовать для нескольких
# параметров — FastAPI мутирует его .alias при разборе сигнатуры. Поэтому —
# фабрика, отдельный экземпляр на каждый вызов (как в app/api.py).
def _id_path() -> int:
    return Path(ge=1, le=2_147_483_647)

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
_UNKNOWN_PREFIX = ms_writeback.UNKNOWN_PREFIX
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
        if started:
            note = "Токен обновлён, синхронизация запущена."
        elif ms_sync.is_running(ctx.org.id):
            note = "Токен обновлён. Синхронизация уже идёт."
        else:
            # Отказал гейт подписки (см. ms_sync.start_sync). Раньше здесь в
            # любом случае писалось «синхронизация уже идёт» — человек ждал
            # данных, которых не будет, и настоящей причины не видел.
            note = ("Токен обновлён, но синхронизация приостановлена: "
                    "подписка не оплачена. Данные и отчёты открыты.")
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


def _release_push_lock(db: Session, order_id: int, pending_href: str,
                       restore_href: str = "") -> None:
    """Снимает СВОЮ временную пометку отправки при сбое.

    Возвращает заказ в состояние «можно отправить», но только если пометка всё
    ещё ровно та, которую поставил наш собственный T1 (`pending_href`).
    Сравнение — равенством, а не `LIKE pending:%`.

    `restore_href` — значение, КОТОРОЕ БЫЛО до нашего T1, и по умолчанию оно
    пустое. Непустым оно бывает ровно в одном случае: попытка началась поверх
    устойчивого «неизвестно». Тогда снятие лока обязано вернуть заказ в ТО ЖЕ
    «неизвестно», а не в чистое «неотправлен».

    Это второй вход в ту же дыру, что и discussion_r3858173475, и я нашёл его
    сам, когда проверял, чем кончается повтор. Заказ уже в `unknown:` (документ
    в МойСкладе, скорее всего, есть), владелец жмёт «отправить» ещё раз, и
    попытка срывается ДО POST — например, в аккаунте нашлись два документа с
    нашим ключом, или упала сеть. Пустая строка здесь стирала знание, добытое
    прошлой попыткой: заказ снова выглядел неотправленным и становился
    удаляемым, а удаление уносило `ms_sync_id`. Сбой ДО POST по-прежнему
    снимает свой точный токен — он просто больше не выдумывает состояние
    чище того, что было.

    Ревью Codex, раунд 3, блокер. `LIKE` снимал ЛЮБУЮ пометку, в том числе
    чужую. Пометка живёт TTL, и по истечении TTL её законно перехватывает
    соседняя попытка — так и задумано. Отсюда воспроизведённое interleaving:
    A взяла `pending:t0`, провисела дольше TTL, B честно перехватила CAS на
    `pending:t1` — и поздний сбой A снимал лок B. После этого в открытое окно
    входила третья попытка, пока B ещё была в сети: взаимное исключение
    исчезало ровно там, где оно и нужно. Стабильный `ms_sync_id` спасал от
    второго документа НАРУЖУ, но владение локом у себя он не восстанавливает.

    Не наша пометка — не наш лок: rowcount=0 и молчание. Это не
    перестраховка: чужую идущую отправку мы отменять не вправе.
    """
    from sqlalchemy import update as _sa_update
    try:
        db.rollback()
        db.execute(
            _sa_update(ProductionOrder)
            .where(
                ProductionOrder.id == order_id,
                ProductionOrder.ms_doc_href == pending_href,  # ТОЧНЫЙ токен T1
            )
            .values(ms_doc_href=restore_href or "")
        )
        db.commit()
    except Exception:  # noqa: BLE001 — освобождение лока не должно маскировать исходную ошибку
        db.rollback()


def _clean_href(href: str | None) -> str:
    """Внутренняя пометка pending:* наружу не показывается — только реальная ссылка."""
    h = href or ""
    return "" if ms_writeback.is_internal_href(h) else h


def _ms_doc_out(order: ProductionOrder) -> dict:
    href = _clean_href(order.ms_doc_href)
    return {
        "ms_doc_href": href,
        "ms_doc_name": order.ms_doc_name if href else "",
        "ms_doc_ui_url": ms_writeback.ui_url(href=href) if href else "",
    }


@router.get("/orders/{order_id}/ms-doc")
def api_order_ms_doc(
    order_id: int = _id_path(),
    ctx: AuthContext = Depends(require_auth_api),
    db: Session = Depends(get_db),
):
    """Ссылка на документ МойСклад, созданный из заказа (пусто, если не было)."""
    return _ms_doc_out(_order_of_org(db, ctx.org.id, order_id))


@router.post("/orders/{order_id}/push-to-ms")
async def api_order_push_to_ms(
    order_id: int = _id_path(),
    ctx: AuthContext = Depends(require_owner_api),
    db: Session = Depends(get_db),
):
    """Создаёт в МойСклад документ «Заказ поставщику» из позиций заказа.

    Идемпотентность: повторная отправка при заполненном ms_doc_href — 409
    «уже отправлен» со ссылкой на существующий документ. Позиции, не нашедшие
    вариант в МС, не валят заказ — возвращаются списком unmatched.
    """
    order = _order_of_org(db, ctx.org.id, order_id)
    # Быстрый и понятный ответ пользователю — но НЕ единственная защита:
    # между этим чтением и T1 помещается чужой коммит `sent → received`.
    # Настоящая защита стоит условием внутри самого T1
    # (ms_writeback.pushable_status_clause).
    if order.status not in ms_writeback.PUSHABLE_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=ms_writeback.ORDER_ALREADY_RECEIVED,
        )
    current = order.ms_doc_href or ""
    if current and not ms_writeback.is_internal_href(current):
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
    if not ms_sync.try_acquire_incoming_lock(ctx.org.id):
        # DATA-6: синк держит организационный лок вокруг чтения+перезаписи
        # ordered_qty.ms_qty (см. ms_sync._sync_incoming) — если пустить push
        # параллельно, свежий вклад заказа рискует быть стёрт перезаписью
        # синка ДО следующего цикла. Неблокирующий отказ — тот же текст 409,
        # что и у соседних ручек при активном синке (см. api_moysklad_stores_select).
        return JSONResponse(
            status_code=409,
            content={
                "detail": "Идёт синхронизация с МойСкладом — дождитесь "
                          "завершения и повторите отправку.",
                **_ms_doc_out(order),
            },
        )
    try:
        return await _push_order_locked(db, ctx, order, order_id, current)
    finally:
        ms_sync.release_incoming_lock(ctx.org.id)


async def _push_order_locked(
    db: Session, ctx: AuthContext, order: ProductionOrder, order_id: int,
    current: str,
):
    """Тело отправки под захваченным org-локом синка (см. DATA-6 выше)."""
    now = int(_time.time())
    # Пометка «неизвестно» повтор НЕ запирает и TTL не ждёт — это и есть
    # штатный выход из такого состояния. Повтор идёт с ТЕМ ЖЕ syncId, поэтому
    # find_own_document подберёт уже созданный документ, а не заведёт второй;
    # два одновременных повтора разводит тот же CAS в begin_push. Запереть
    # заказ до синка было бы лечением хуже болезни: владелец остался бы с
    # заказом, который нельзя ни отправить, ни удалить.
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
    # T1 — единственная транзакция ДО сети: CAS-захват «замка» плюс рождение
    # ключей идемпотентности (заказа и контрагента). Второй одновременный
    # клик/ретрай не обновит ни строки (значение уже изменилось) и получит
    # 409 — иначе гонка создала бы ДВА финансовых документа. Подробности и
    # причины — в ms_writeback.begin_push.
    #
    # Токен запоминается в переменной и дальше ходит по всей попытке: и снятие
    # лока при сбое, и T2 обязаны сравнивать пометку с НИМ, а не с образцом
    # `pending:%`. Пометка живёт TTL и после него законно достаётся соседней
    # попытке; попытка, вернувшаяся из сети позже своего TTL, обязана узнать
    # чужое владение и ничего не трогать (ревью Codex, раунд 3).
    pending = f"{_PENDING_PREFIX}{now}"
    # Куда возвращать заказ, если попытка сорвётся ДО создания документа.
    # Обычно — в «неотправлен». Но повтор поверх устойчивого «неизвестно»
    # обязан вернуться именно в «неизвестно»: прошлая попытка уже узнала, что
    # документ мог быть создан, и терять это знание нельзя (см.
    # _release_push_lock).
    release_to = current if ms_writeback.is_unknown(current) else ""
    locked = ms_writeback.begin_push(
        db, ctx.org.id, order.id, current, pending)
    if not locked:
        # Проигранный CAS — это НЕ одно событие, и сводить их к одному коду
        # нельзя (ревью Codex, P1). В условии T1 стоят и точный ms_doc_href, и
        # допустимый статус, поэтому «не захватили» означает ТРИ разных
        # события, и человек по ответу решает разное:
        #   • строки больше нет — заказ удалили, пока мы читали;
        #   • заказ приняли на склад — отправлять поздно, ждать нечего;
        #   • идёт соседняя отправка — вот тут и правда стоит подождать.
        #
        # Перезагрузка обязана переживать ОТСУТСТВИЕ строки (ревью Codex, P2,
        # discussion_r3857538139). Здесь стоял `db.refresh(order)`, и на
        # удалённой строке он поднимал InvalidRequestError: обычная гонка
        # отвечала 500, то есть «сломались мы», хотя сломались не мы.
        #
        # Форма взята с уже корректного пути условного удаления в
        # `api.api_order_delete`: завершить состояние транзакции, сбросить
        # identity map, перечитать заново. Проверка организации в перезагрузке
        # обязательна: без неё чужой заказ получил бы ответ, отличный от 404,
        # и это выдало бы сам факт существования строки.
        db.rollback()
        db.expire_all()
        fresh = db.get(ProductionOrder, order_id)
        if fresh is None or fresh.org_id != ctx.org.id:
            raise HTTPException(status_code=404, detail="Заказ не найден")
        if fresh.status not in ms_writeback.PUSHABLE_STATUSES:
            raise HTTPException(
                status_code=422,
                detail=ms_writeback.ORDER_ALREADY_RECEIVED,
            )
        return JSONResponse(
            status_code=409,
            content={
                "detail": "Заказ уже отправляется в МойСклад — дождитесь завершения "
                          "и обновите страницу.",
                # Данные документа — из СВЕЖЕЙ строки: устаревший ORM-объект
                # тем и плох, что описывает состояние до гонки.
                **_ms_doc_out(fresh),
            },
        )
    try:
        result = await ms_writeback.push_order(db, ctx.org.id, order, pending)
    except ms_writeback.AmbiguousExistingOrder as exc:
        # У нескольких документов МойСклада одинаковая наша метка — почти
        # наверняка чей-то «Копировать документ» вместе с описанием. Привязать
        # заказ к первому попавшемуся значит начать считать «едет к нам» по
        # чужой бумаге. Разобраться может только человек, который эти документы
        # видит, поэтому отдаём номера и ничего не создаём.
        if exc.after_create:
            # Отправка уже сорвалась, и мы не знаем, создался ли документ.
            # Обещать «ничего не создано» нельзя, и советовать снимать метки —
            # тоже: сняв её с только что созданного, человек получит дубль.
            #
            # Пометку здесь НЕ снимаем (ревью Codex, P1,
            # discussion_r3858173475). Раньше `_release_push_lock` стоял выше
            # развилки — то есть выполнялся и на этой ветке. Заказ,
            # собственный текст ответа которому говорит «один из документов
            # мог быть только что создан нами», становился удаляемым, и
            # удаление уносило `ms_sync_id`. Исход неизвестен — значит
            # состояние устойчиво неизвестное, как и на соседних ветках.
            ms_writeback.mark_unknown(
                db, order.id, pending, f"{_UNKNOWN_PREFIX}{now}")
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Отправка прервалась, а в МойСкладе несколько заказов "
                    f"поставщику с меткой этого заказа ({exc}). Один из них "
                    f"мог быть только что создан нами. НЕ отправляйте заказ "
                    f"повторно: откройте эти документы в МойСкладе и проверьте, "
                    f"какой из них настоящий."
                ),
            )
        # Искали ПЕРЕД созданием: нового документа точно нет, лок наш и снимается.
        _release_push_lock(db, order.id, pending, release_to)
        raise HTTPException(
            status_code=409,
            detail=(
                f"В МойСкладе несколько заказов поставщику с меткой этого "
                f"заказа ({exc}). Так бывает, если документ скопировали вместе "
                f"с описанием. Оставьте метку «[oborot#...]» только у нужного "
                f"документа (или удалите её у лишних) и повторите отправку. "
                f"Новый документ не создан."
            ),
        )
    except ms_writeback.AmbiguousCounterparty as exc:
        # Контрагентов «Производство» в аккаунте несколько. Выбрать за
        # владельца нельзя: заказ поставщику — обещание конкретному
        # подрядчику, и «какой-нибудь» здесь означает «не тот».
        _release_push_lock(db, order.id, pending, release_to)
        raise HTTPException(
            status_code=409,
            detail=(
                f"В МойСкладе несколько контрагентов с именем "
                f"«{ms_writeback.AGENT_NAME}» ({exc}). Заказ поставщику — "
                f"финансовый документ, и выбрать за вас, кому он уходит, мы не "
                f"вправе. Оставьте одного (лишних переименуйте или заархивируйте) "
                f"и повторите отправку. Документ не создан."
            ),
        )
    except ms_writeback.WritebackUnknown as exc:
        # Честный третий исход: документ создан, а записать это у себя не
        # вышло даже со второй попытки. Ни «получилось», ни «не получилось».
        #
        # Пометку НЕ снимаем, а переводим в устойчивое «неизвестно» (ревью
        # Codex, P1). Пустая строка здесь возвращала заказ в вид
        # «неотправленный», после чего его можно было удалить — вместе с
        # ms_sync_id, то есть с единственным ключом, по которому синк связал бы
        # документ обратно. Финансовый документ в чужом аккаунте остался бы без
        # владельца навсегда.
        #
        # Перевод идёт CAS'ом по нашему точному токену: чужую пометку мы не
        # трогаем и здесь.
        ms_writeback.mark_unknown(
            db, order.id, pending, f"{_UNKNOWN_PREFIX}{now}")
        raise HTTPException(
            status_code=502,
            detail=(
                f"Документ в МойСкладе создан ({exc.doc_name or 'без номера'}), "
                "но сохранить его у нас не удалось. Откройте документ по ссылке "
                "и проверьте. Повторная отправка безопасна: она пойдёт с тем же "
                "ключом и второго документа не создаст."
            ),
            headers={"X-Oborot-Ms-Doc": exc.doc_href[:400]},
        )
    except ms_writeback.PushOutcomeUnknown as exc:
        # Запрос на создание документа ушёл, а исход установить не удалось:
        # ответ потерян и восстановительный поиск его не подтвердил — либо не
        # нашёл (задержка видимости), либо сам не состоялся.
        #
        # Ревью Codex, P1 (discussion_r3858173475). Раньше такой исход
        # приходил сюда обычным `httpx.HTTPError`, и общий сетевой обработчик
        # снимал пометку. Заказ выглядел неотправленным и становился
        # УДАЛЯЕМЫМ, хотя финансовый документ мог существовать; удаление
        # уносило `ms_sync_id` — единственный ключ, по которому back-match
        # связал бы документ обратно.
        #
        # Обращаемся с ним ровно так же, как с уже признанным третьим исходом
        # T2: CAS в устойчивое «неизвестно» по СВОЕМУ точному токену. Ключ
        # остаётся, удаление запрещено, повтор идёт с тем же syncId и второго
        # документа не создаёт.
        ms_writeback.mark_unknown(
            db, order.id, pending, f"{_UNKNOWN_PREFIX}{now}")
        raise HTTPException(
            status_code=502,
            detail=(
                f"Отправка прервалась после того, как запрос на создание "
                f"документа уже ушёл в МойСклад ({exc}). Документ там мог быть "
                "создан, поэтому руками его не заводите: повторите отправку — "
                "она пойдёт с тем же ключом и второго документа не создаст. "
                "Пока исход неизвестен, удалить заказ нельзя."
            ),
        )
    except ms_writeback.WritebackError as exc:
        _release_push_lock(db, order.id, pending, release_to)
        raise HTTPException(status_code=exc.status, detail=exc.detail)
    except httpx.HTTPStatusError as exc:
        _release_push_lock(db, order.id, pending, release_to)
        code = exc.response.status_code
        if code in (401, 403):
            raise HTTPException(status_code=400, detail=TOKEN_HINT)
        raise HTTPException(
            status_code=502,
            detail=f"МойСклад ответил ошибкой {code}. Попробуйте ещё раз позже: "
                   "повтор пойдёт с тем же ключом и документ не задвоит.",
        )
    except httpx.HTTPError:
        _release_push_lock(db, order.id, pending, release_to)
        raise HTTPException(status_code=502, detail=NETWORK_HINT)
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
