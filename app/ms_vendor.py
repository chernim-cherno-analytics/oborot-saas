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

# Возраст входящего lifecycle-JWT и анти-replay. Утёкший токен (напр. через
# прокси-логи) должен быть бесполезен уже через минуты — поэтому требуем свежий
# iat и запоминаем jti на время его жизни (best-effort, in-memory; при >1 воркере
# вынести в общий кэш — см. SECURITY-бэклог).
INCOMING_JWT_MAX_AGE_SEC = 300
_seen_jti: dict[str, float] = {}
# jti НАШИХ исходящих токенов (аудит 18.08): исходящий и входящий JWT
# подписаны ОДНИМ HS256-секретом, поэтому наш же токен, перехваченный по
# дороге к МС, проходил verify_incoming_jwt. Помечаем исходящие клеймом
# dir=out и помним их jti — отражение отбивается по обоим признакам.
_issued_jti: dict[str, float] = {}


def _remember_jti(jti: str, now: float) -> bool:
    """True — jti новый (запомнили); False — уже видели (replay)."""
    # чистим протухшие, чтобы словарь не рос без предела
    for k, exp in list(_seen_jti.items()):
        if exp < now:
            _seen_jti.pop(k, None)
    if jti in _seen_jti:
        return False
    _seen_jti[jti] = now + INCOMING_JWT_MAX_AGE_SEC
    return True


def verify_incoming_jwt(authorization: str | None,
                        account_id: str | None = None) -> dict:
    """Проверяет JWT из Authorization lifecycle-запроса МойСклад.

    Модель доверия (по итогам security-ревью):
    - подпись HS256 нашим secret key (alg зафиксирован — alg:none/RS-confusion
      не проходят);
    - ОБЯЗАТЕЛЬНЫ exp и iat (токен без срока больше не «вечный ключ»);
    - iat не старше INCOMING_JWT_MAX_AGE_SEC — узкое окно для утёкшего токена;
    - jti одноразовый (анти-replay), если присутствует.
    Принимаем «Bearer <jwt>» и голый токен. Любая невалидность → 401.
    Возвращает claims. TODO при интеграции: если МС кладёт accountId в claims —
    сверять с accountId из пути в вызывающем роуте.
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
        claims = jwt.decode(
            token, app_secret(), algorithms=["HS256"],
            options={"require": ["exp", "iat"]},
        )
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Подпись или срок JWT не прошли проверку")
    now = time.time()
    iat = float(claims.get("iat", 0))
    if iat <= 0 or now - iat > INCOMING_JWT_MAX_AGE_SEC:
        raise HTTPException(status_code=401, detail="JWT просрочен или выдан слишком давно")
    jti = claims.get("jti")
    if jti and not _remember_jti(str(jti), now):
        raise HTTPException(status_code=401, detail="JWT уже был использован (replay)")
    # Аудит 18.08: наш собственный исходящий токен (тот же секрет!) не должен
    # проходить как входящий — отражение ловим по клейму dir=out и по jti.
    if claims.get("dir") == "out" or (jti and str(jti) in _issued_jti):
        raise HTTPException(status_code=401,
                            detail="Отражён исходящий токен приложения")
    # Аудит 18.08: если МС кладёт accountId в claims — сверяем с accountId из
    # пути (закрывает подмену пути при валидном токене). Клейма может не быть —
    # тогда проверка мягко пропускается (протокол это допускает).
    if account_id:
        claim_acc = str(claims.get("accountId") or claims.get("account_id") or "")
        if claim_acc and claim_acc != str(account_id):
            raise HTTPException(status_code=401,
                                detail="accountId токена не совпадает с путём запроса")
    return claims


# ── Наш JWT (мы → МС) ────────────────────────────────────────────────────────

JWT_TTL_SEC = 300  # запас: МС требует лишь актуальный exp


def make_jwt() -> str:
    """JWT для запросов к Vendor API: HS256 {sub=appUid, iat, exp, jti, dir=out}.

    dir=out — направление токена (МС незнакомые клеймы игнорирует); jti
    запоминается, чтобы отражённый обратно токен не прошёл verify_incoming_jwt.
    """
    now = int(time.time())
    payload = {
        "sub": app_uid(),
        "iat": now,
        "exp": now + JWT_TTL_SEC,
        "jti": uuid.uuid4().hex,
        "dir": "out",
    }
    # чистим протухшие исходящие jti, чтобы словарь не рос
    for k, exp in list(_issued_jti.items()):
        if exp < now:
            _issued_jti.pop(k, None)
    _issued_jti[payload["jti"]] = now + JWT_TTL_SEC + INCOMING_JWT_MAX_AGE_SEC
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
