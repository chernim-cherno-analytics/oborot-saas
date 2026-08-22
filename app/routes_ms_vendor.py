"""Lifecycle-эндпоинты приложения маркетплейса МойСклад (Vendor API 1.0).

МС дёргает нас при установке/смене тарифа/удалении приложения:

  PUT    /ms/vendor/api/moysklad/vendor/1.0/apps/{appId}/{accountId}
         (cause: Install | TariffChanged | Resume) → {"status": "Activated"}
  DELETE тот же путь (cause: Uninstall | Suspend | AccountDeleted) → 200

Оба запроса несут JWT в Authorization (HS256 нашим secret key) — битая
подпись отвергается 401 (app.ms_vendor.verify_incoming_jwt).

PUT — идемпотентный провижининг:
  1) Org по ms_account_id: нет — создаём (name=accountName, plan из маппинга
     тарифа, source='ms_app'); есть — обновляем план/тариф, НЕ дублируем;
  2) Connection(kind='moysklad'): access_token аккаунта шифруется Fernet
     (пользователь НИЧЕГО не вводит — главное УТП установки из каталога);
  3) склады: тянем ВСЕ склады аккаунта и включаем их (новые — active=True;
     выбор пользователя по уже известным складам не перетираем) — лишние
     он отключит в настройках;
  4) автозапуск первичного синка, как в онбординге (ms_sync.start_sync),
     только если подключение ещё не активно (первая установка / после
     ошибки). TariffChanged/Resume синк не перезапускают.
  Сбой похода в МС за складами НЕ валит активацию: отвечаем Activated,
  подключение остаётся pending — доберём при следующем PUT/входе.

DELETE: org.status='suspended' — планировщик такие организации пропускает
(app.scheduler), данные и вход сохраняются; повторный PUT (Resume) вернёт
status='active'. Неизвестный accountId в DELETE — тоже 200 (идемпотентность).
"""
import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import ms_sync, ms_vendor
from app.crypto import encrypt_token
from app.db import get_db
from app.models import Connection, Org, Warehouse
from app.ms_client import MoySkladClient

log = logging.getLogger("oborot.ms_vendor")

# Аддитивная мини-миграция ms_vendor.ensure_schema() (колонки orgs/users для
# баз, созданных до фичи) раньше запускалась прямо здесь, на импорте модуля —
# до include_router, то есть до старта приложения (Д4, ревью 22.08): при
# нескольких воркерах разом это роняло часть процессов ещё до первого
# запроса. Теперь выполняется в db.init_db() вместе с остальными миграциями —
# см. app/main.py:_startup.

router = APIRouter()

LIFECYCLE_PATH = "/ms/vendor/api/moysklad/vendor/1.0/apps/{path_app_id}/{account_id}"


def _check_app_id(path_app_id: str) -> None:
    if path_app_id != ms_vendor.app_id():
        raise HTTPException(status_code=404, detail="Неизвестное приложение")


def _get_ms_connection(db: Session, org_id: int) -> Connection | None:
    return db.execute(
        select(Connection).where(
            Connection.org_id == org_id, Connection.kind == "moysklad"
        )
    ).scalars().first()


def _upsert_all_warehouses(db: Session, org_id: int, stores: list[dict]) -> int:
    """Все склады аккаунта МС → Warehouse. Новые — active=True; у известных
    флаг active не трогаем (пользователь мог отключить лишние в настройках)."""
    existing = {
        w.ext_id: w
        for w in db.execute(
            select(Warehouse).where(Warehouse.org_id == org_id)
        ).scalars()
    }
    count = 0
    for store in stores:
        ext_id = store.get("id") or ""
        if not ext_id:
            continue
        row = existing.get(ext_id)
        if row is None:
            row = Warehouse(org_id=org_id, ext_id=ext_id,
                            name=store.get("name") or ext_id, active=True)
            db.add(row)
        else:
            row.name = store.get("name") or row.name
        count += 1
    db.commit()
    return count


@router.put(LIFECYCLE_PATH)
async def vendor_lifecycle_activate(
    path_app_id: str,
    account_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """Install | TariffChanged | Resume → провижининг и {"status":"Activated"}."""
    ms_vendor.verify_incoming_jwt(request.headers.get("Authorization"), account_id=account_id)
    _check_app_id(path_app_id)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — пустое/битое тело
        body = {}

    cause = str(body.get("cause") or "Install")
    subscription = body.get("subscription") or {}
    tariff_name = str(subscription.get("tariffName") or "")
    trial = bool(subscription.get("trial"))
    token = ""
    for grant in body.get("access") or []:
        if grant.get("access_token"):
            token = str(grant["access_token"])
            break

    # ── Организация: найти по ms_account_id или создать ──────────────────────
    org = db.execute(
        select(Org).where(Org.ms_account_id == account_id)
    ).scalars().first()
    if org is None:
        org = Org(
            name=str(body.get("accountName") or f"МойСклад {account_id[:8]}"),
            source="ms_app",
            ms_account_id=account_id,
        )
        db.add(org)
        db.flush()
    org.plan = ms_vendor.plan_from_tariff(tariff_name, trial)
    org.ms_tariff_name = tariff_name
    org.status = "active"  # Install/Resume снимают suspended
    if trial:
        org.trial_ends_at = ms_vendor.parse_expiry(subscription.get("expiryMoment")) \
            or org.trial_ends_at

    # ── Подключение: токен аккаунта (Fernet), статус ─────────────────────────
    conn = _get_ms_connection(db, org.id)
    if conn is None:
        conn = Connection(org_id=org.id, kind="moysklad", config_json="{}")
        db.add(conn)
    if token:
        conn.token_enc = encrypt_token(token)
    if conn.status != "active":
        conn.status = "pending"  # активирует финализация первичного синка
    db.commit()

    # ── Склады (все, автоматически) + автозапуск первичного синка ────────────
    need_initial = conn.status != "active"
    if token and need_initial:
        try:
            async with MoySkladClient(token) as client:
                stores = await client.fetch_stores()
            _upsert_all_warehouses(db, org.id, stores)
            if not ms_sync.is_running(org.id):
                ms_sync.start_sync(org.id, mode="initial")
        except (httpx.HTTPError, RuntimeError):
            # Активацию не валим: МС повторит PUT / пользователь зайдёт позже.
            log.exception("org=%s (%s): не удалось запустить первичный синк",
                          org.id, cause)

    return {"status": "Activated"}


@router.delete(LIFECYCLE_PATH)
async def vendor_lifecycle_deactivate(
    path_app_id: str,
    account_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """Uninstall | Suspend | AccountDeleted → org.status='suspended', 200.

    Данные и вход сохраняем (переустановка = Resume вернёт всё как было);
    доступ к API МС и так закрыт — МС отзывает access_token сам.
    """
    ms_vendor.verify_incoming_jwt(request.headers.get("Authorization"), account_id=account_id)
    _check_app_id(path_app_id)
    org = db.execute(
        select(Org).where(Org.ms_account_id == account_id)
    ).scalars().first()
    if org is not None:
        org.status = "suspended"
        db.commit()
    return {}
