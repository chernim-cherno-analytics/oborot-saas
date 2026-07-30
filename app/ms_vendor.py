"""Vendor API 1.0 МойСклад: конфиг, JWT в обе стороны, обмен contextKey.

«Оборот» как приложение каталога МойСклад. Контракт (из исследования):

- МС → нам (lifecycle): PUT/DELETE /apps/{appId}/{accountId} c JWT в
  Authorization. JWT подписан HS256 нашим secret key из ЛК разработчика —
  здесь проверяем подпись (битая/чужая → 401, без исключений).
- Мы → МС: POST {vendor_api_base}/context/{contextKey} с нашим JWT
  (HS256: sub=appUid, iat, exp, jti) → контекст пользователя iframe
  {accountId, uid, ...} — это SSO-вход без пароля.

Конфиг из env (все значения выдаёт ЛК разработчика МойСклад):
  MS_APP_ID          — UUID приложения (сегмент пути lifecycle-запросов);
  MS_APP_UID         — appUid вида «имя.вендор» (sub нашего JWT);
  MS_APP_SECRET      — secret key (HS256 в обе стороны);
  MS_VENDOR_API_BASE — база Vendor API, дефолт
                       https://apps-api.moysklad.ru/api/vendor/1.0
                       (в тестах подменяется на mock).

Здесь же аддитивная мини-миграция ensure_schema(): колонки
orgs.ms_account_id/source/status/ms_tariff_name и users.ms_uid для баз,
созданных до этой фичи (паттерн — app.ms_writeback.ensure_schema).
"""
import os
import time
import uuid
from datetime import datetime

import httpx
import jwt
from fastapi import HTTPException
from sqlalchemy import inspect, text

from app.db import engine

DEFAULT_VENDOR_API_BASE = "https://apps-api.moysklad.ru/api/vendor/1.0"


# ── Конфиг ───────────────────────────────────────────────────────────────────

def app_id() -> str:
    return os.environ.get("MS_APP_ID", "").strip()


def app_uid() -> str:
    return os.environ.get("MS_APP_UID", "").strip()


def app_secret() -> str:
    return os.environ.get("MS_APP_SECRET", "").strip()


def vendor_api_base() -> str:
    return os.environ.get("MS_VENDOR_API_BASE", DEFAULT_VENDOR_API_BASE).rstrip("/")


def configured() -> bool:
    """Заданы ли все три ключа приложения (без них lifecycle-ручки отвечают 503)."""
    return bool(app_id() and app_uid() and app_secret())


# ── Входящий JWT (МС → нам) ──────────────────────────────────────────────────

def verify_incoming_jwt(authorization: str | None) -> dict:
    """Проверяет JWT из Authorization lifecycle-запроса МойСклад.

    Подпись HS256 нашим secret key; exp/nbf, если присутствуют, проверяет
    pyjwt. Принимаем и «Bearer <jwt>», и голый токен (в документации МС
    формат заголовка не фиксирован). Любая невалидность → HTTPException 401.
    Возвращает claims (для аудита/логов).
    """
    if not configured():
        raise HTTPException(
            status_code=503,
            detail="Приложение МойСклад не сконфигурировано на сервере "
                   "(нужны MS_APP_ID, MS_APP_UID, MS_APP_SECRET).",
        )
    token = (authorization or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Нет JWT в заголовке Authorization")
    try:
        return jwt.decode(token, app_secret(), algorithms=["HS256"])
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Подпись JWT не прошла проверку")


# ── Наш JWT (мы → МС) ────────────────────────────────────────────────────────

JWT_TTL_SEC = 300  # запас: МС требует лишь актуальный exp


def make_jwt() -> str:
    """JWT для запросов к Vendor API: HS256 {sub=appUid, iat, exp, jti}."""
    now = int(time.time())
    payload = {
        "sub": app_uid(),
        "iat": now,
        "exp": now + JWT_TTL_SEC,
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, app_secret(), algorithm="HS256")


# ── Обмен contextKey → контекст пользователя iframe ──────────────────────────

async def get_context(context_key: str) -> dict:
    """POST /context/{contextKey} → {accountId, uid, ...} (SSO из iframe).

    Ошибки переводятся в человекочитаемые HTTPException:
    404 от МС = ключ не найден/устарел (нормально при F5 старой вкладки).
    """
    if not configured():
        raise HTTPException(
            status_code=503,
            detail="Приложение МойСклад не сконфигурировано на сервере.",
        )
    url = f"{vendor_api_base()}/context/{context_key}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                url, headers={"Authorization": f"Bearer {make_jwt()}"}
            )
    except httpx.HTTPError:
        raise HTTPException(
            status_code=502,
            detail="Не удалось связаться с МойСклад. Обновите страницу приложения.",
        )
    if resp.status_code == 404:
        raise HTTPException(
            status_code=401,
            detail="Сессия МойСклад не подтверждена: ключ входа не найден или "
                   "устарел. Обновите страницу приложения в МойСклад.",
        )
    if resp.status_code in (401, 403):
        raise HTTPException(
            status_code=502,
            detail="МойСклад не принял ключ приложения — проверьте "
                   "MS_APP_UID/MS_APP_SECRET на сервере.",
        )
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"МойСклад ответил ошибкой {resp.status_code}. Попробуйте позже.",
        )
    return resp.json()


# ── Тариф каталога МС → наш план ─────────────────────────────────────────────

_PLAN_MARKERS = (
    ("старт", "start"), ("start", "start"),
    ("бренд", "brand"), ("brand", "brand"),
    ("про", "pro"), ("pro", "pro"),
)


def plan_from_tariff(tariff_name: str, trial: bool) -> str:
    """Маппинг имени тарифа каталога МС на наш план; триал МС = наш trial."""
    if trial:
        return "trial"
    name = (tariff_name or "").strip().lower()
    for marker, plan in _PLAN_MARKERS:
        if marker in name:
            return plan
    return "trial"  # незнакомый тариф — не даём лишнего, разбираемся руками


def parse_expiry(moment: str | None) -> datetime | None:
    """expiryMoment МС ('YYYY-MM-DD HH:MM:SS[.mmm]') → datetime; мусор → None."""
    raw = str(moment or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


# ── Аддитивная мини-миграция ─────────────────────────────────────────────────

def ensure_schema() -> None:
    """Добавляет колонки Vendor-фичи в существующие orgs/users.

    Base.metadata.create_all не изменяет существующие таблицы. ALTER TABLE
    ADD COLUMN — аддитивно, работает и в SQLite, и в Postgres. UNIQUE при
    ADD COLUMN SQLite не умеет — в старых БД уникальность ms_account_id/ms_uid
    обеспечивает логика поиска-перед-вставкой; новые БД получают constraint
    из модели. Свежая БД (таблиц нет) — no-op.
    """
    insp = inspect(engine)
    if insp.has_table("orgs"):
        cols = {c["name"] for c in insp.get_columns("orgs")}
        with engine.begin() as conn:
            if "ms_account_id" not in cols:
                conn.execute(text("ALTER TABLE orgs ADD COLUMN ms_account_id VARCHAR(64)"))
            if "source" not in cols:
                conn.execute(text(
                    "ALTER TABLE orgs ADD COLUMN source VARCHAR(16) NOT NULL DEFAULT 'saas'"
                ))
            if "status" not in cols:
                conn.execute(text(
                    "ALTER TABLE orgs ADD COLUMN status VARCHAR(16) NOT NULL DEFAULT 'active'"
                ))
            if "ms_tariff_name" not in cols:
                conn.execute(text(
                    "ALTER TABLE orgs ADD COLUMN ms_tariff_name VARCHAR(128) NOT NULL DEFAULT ''"
                ))
    if insp.has_table("users"):
        cols = {c["name"] for c in insp.get_columns("users")}
        if "ms_uid" not in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE users ADD COLUMN ms_uid VARCHAR(255)"))
