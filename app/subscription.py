"""Гейт подписки: одно состояние организации — active | grace | readonly.

Зачем отдельный модуль. До сих пор оплата в «Обороте» существовала только как
заявка на счёт (billing_requests) и текст на странице тарифов: истёкший триал
ничего не закрывал, платить было не обязательно. Этот модуль вводит ровно одно
доменное состояние подписки и одно место, где оно вычисляется, — чтобы «кому
можно писать» не расползлось по роутам разными формулами (ровно так уже
разъехались need/lead time/MOQ, см. BUSINESS_LOGIC §9).

Состояния (D-24):
  active   — всё работает;
  grace    — счёт выставлен, деньги ещё не пришли: пишем как обычно, но
             интерфейс вправе показать предупреждение;
  readonly — писать нельзя, читать можно.

Что закрывает readonly (в порядке ценности, а не «всё подряд»):
  1) синхронизация с МойСклад,
  2) запись в МойСклад (заказ поставщику),
  3) расчёт и сохранение планов заказа.
Чтение, экспорт, страница тарифов и заявка на счёт НЕ закрываются никогда:
клиент с истёкшей подпиской должен видеть свои данные и иметь возможность
заплатить. Это правило проверяется тестом (tests/test_subscription.py).

Выключен по умолчанию. Включается переменной окружения
OBOROT_SUBSCRIPTION_GATE=1. Причина осторожности прозаическая: у собственной
организации владельца на проде триал истёк 13 августа, и включённый гейт
закрыл бы доступ ему же. Порядок ввода: сначала выставить paid_until живым
организациям, потом включать флаг.
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta

from fastapi import Depends, HTTPException
from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session

from app.auth import AuthContext, require_auth_api
from app.db import engine, get_db, run_migration_step

log = logging.getLogger("oborot.subscription")

# Сколько календарных дней клиент работает после отметки «счёт выставлен».
# Календарных, а не рабочих: решение владельца, 5 дней (D-24).
GRACE_DAYS = 5

GATE_ENV = "OBOROT_SUBSCRIPTION_GATE"

ACTIVE = "active"
GRACE = "grace"
READONLY = "readonly"

_TRUE = {"1", "true", "yes", "on"}


def gate_enabled() -> bool:
    """Включён ли гейт. Читается на каждом вызове — тесты меняют окружение."""
    return (os.getenv(GATE_ENV) or "").strip().lower() in _TRUE


# ── Аддитивная мини-миграция ─────────────────────────────────────────────────

def ensure_schema(bind=None) -> None:
    """orgs.paid_until и billing_requests.invoiced_at.

    Обе колонки аддитивные и nullable — старые записи остаются валидными,
    откат кода не ломает базу. Вызывается из app.main._startup вместе с
    остальными миграциями (не на импорте: несколько воркеров стартуют разом).
    """
    eng = bind or engine
    insp = inspect(eng)
    if insp.has_table("orgs"):
        cols = {c["name"] for c in insp.get_columns("orgs")}
        if "paid_until" not in cols:
            run_migration_step("ALTER TABLE orgs ADD COLUMN paid_until DATE", bind=eng)
    if insp.has_table("billing_requests"):
        cols = {c["name"] for c in insp.get_columns("billing_requests")}
        if "invoiced_at" not in cols:
            run_migration_step(
                "ALTER TABLE billing_requests ADD COLUMN invoiced_at DATETIME", bind=eng,
            )


# ── Вычисление состояния ─────────────────────────────────────────────────────

def _as_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:  # SQLite отдаёт строку, если колонка добавлена ALTER-ом
        return datetime.fromisoformat(str(value)[:19]).date()
    except ValueError:
        return None


def _grace_until(db: Session, org_id: int, today: date) -> date | None:
    """Дата конца грейса по последней отметке «счёт выставлен».

    Грейс отсчитывается от момента, когда счёт выставили МЫ, а не от даты
    заявки клиента: заявка — это намерение, а обязательство возникает после
    выставленного счёта.

    Тонкость эксплуатации: статус заявки сейчас меняется вручную (UPDATE в
    базе), и такой UPDATE не проставит invoiced_at. Поэтому при первой
    встрече заявки со статусом invoiced/paid без отметки времени мы ставим
    отметку сами — «первое наблюдение». Ошибка тут возможна только в сторону
    клиента (грейс начнётся позже фактического счёта, то есть дольше), и это
    сознательно: гейт не должен закрывать доступ из-за нашей же забывчивости.
    """
    insp = inspect(db.get_bind())
    if not insp.has_table("billing_requests"):
        return None
    cols = {c["name"] for c in insp.get_columns("billing_requests")}
    if "invoiced_at" not in cols:
        return None
    rows = db.execute(
        text(
            "SELECT id, invoiced_at FROM billing_requests "
            "WHERE org_id = :org AND status IN ('invoiced', 'paid') "
            "ORDER BY id DESC LIMIT 1"
        ),
        {"org": org_id},
    ).fetchall()
    if not rows:
        return None
    row_id, stamp = rows[0]
    marked = _as_date(stamp)
    if marked is None:
        db.execute(
            text("UPDATE billing_requests SET invoiced_at = :ts WHERE id = :id"),
            {"ts": datetime.utcnow(), "id": row_id},
        )
        db.commit()
        marked = today
    return marked + timedelta(days=GRACE_DAYS)


def subscription_state(org, db: Session) -> str:
    """active | grace | readonly. Единственное место, где это решается."""
    today = date.today()

    # Организации из каталога МойСклад платят внутри МС: их состояние —
    # то, что прислал МС (Activate/Suspend/Uninstall кладут status).
    # Своих счетов мы им не выставляем, поэтому грейса у них нет.
    if (getattr(org, "source", "saas") or "saas") == "ms_app":
        return ACTIVE if (getattr(org, "status", "active") or "active") == "active" else READONLY

    if (getattr(org, "status", "active") or "active") != "active":
        return READONLY

    paid_until = _as_date(getattr(org, "paid_until", None))
    if paid_until is not None and paid_until >= today:
        return ACTIVE

    # Дата конца триала — НАША запись, клиент её не задаёт, подделать не может.
    # Поэтому она уважается независимо от названия тарифа: организация, успевшая
    # переключить `plan` на платный, но ещё не оплатившая, доживает свой триал,
    # а не выключается на день раньше. Асимметрия ошибок здесь однозначна:
    # пропустить неплательщика — потерять месяц денег, закрыть плательщика —
    # потерять клиента (D-24, «название тарифа доказательством не является» —
    # доказательство оплаты это paid_until, а не plan).
    trial_ends = _as_date(getattr(org, "trial_ends_at", None))
    if trial_ends is not None and trial_ends >= today:
        return ACTIVE

    grace_until = _grace_until(db, org.id, today)
    if grace_until is not None and grace_until >= today:
        return GRACE

    return READONLY


def state_info(org, db: Session) -> dict:
    """Состояние + даты для интерфейса и /api/subscription."""
    state = subscription_state(org, db)
    paid_until = _as_date(getattr(org, "paid_until", None))
    trial_ends = _as_date(getattr(org, "trial_ends_at", None))
    grace_until = _grace_until(db, org.id, date.today()) if state == GRACE else None
    return {
        "state": state,
        "gate_enabled": gate_enabled(),
        "writes_blocked": gate_enabled() and state == READONLY,
        "paid_until": paid_until.isoformat() if paid_until else None,
        "trial_ends_at": trial_ends.isoformat() if trial_ends else None,
        "grace_until": grace_until.isoformat() if grace_until else None,
        "source": getattr(org, "source", "saas"),
    }


def can_sync(org, db: Session) -> bool:
    """Пускать ли организацию в плановую (фоновую) синхронизацию."""
    if not gate_enabled():
        return True
    return subscription_state(org, db) != READONLY


BLOCK_MESSAGE = (
    "Доступ к записи приостановлен: подписка не оплачена. "
    "Данные и отчёты открыты, синхронизация и заказы возобновятся после оплаты. "
    "Выставить счёт — на странице «Тарифы»."
)


# ── Зависимость FastAPI ──────────────────────────────────────────────────────

def require_write_access(
    ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)
) -> AuthContext:
    """402, если организация в readonly и гейт включён.

    Вешается через dependencies=[...] в декораторе роута — сигнатуры
    обработчиков не меняются, роль (require_owner_api) проверяется отдельно
    и по-прежнему. Состояние grace пропускает запись: счёт выставлен,
    закрывать доступ до истечения грейса нельзя.
    """
    if not gate_enabled():
        return ctx
    state = subscription_state(ctx.org, db)
    if state == READONLY:
        raise HTTPException(status_code=402, detail=BLOCK_MESSAGE)
    return ctx


# ── Предпросмотр перед включением флага ──────────────────────────────────────

def preview(db: Session) -> dict:
    """Кого закроет гейт, если его включить. Ничего не меняет.

    Существует ради одного сценария: флаг включают, и сервис молча закрывается
    тем, кого закрывать не собирались. Считается на старте приложения и пишется
    в лог — так «посмотреть перед тем, как щёлкнуть» не требует ни доступа
    к базе, ни отдельной ручки.
    """
    from app.models import Org

    counts = {ACTIVE: 0, GRACE: 0, READONLY: 0}
    readonly_ids: list[int] = []
    for org in db.execute(select(Org)).scalars():
        state = subscription_state(org, db)
        counts[state] = counts.get(state, 0) + 1
        if state == READONLY:
            readonly_ids.append(org.id)
    return {"counts": counts, "readonly_org_ids": readonly_ids,
            "gate_enabled": gate_enabled()}


def log_preview() -> None:
    """Пишет предпросмотр в лог на старте. Ошибки глушит: это диагностика."""
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        info = preview(db)
        counts = info["counts"]
        if info["gate_enabled"]:
            log.warning(
                "гейт подписки ВКЛЮЧЁН: active=%d grace=%d readonly=%d; "
                "закрыты организации %s",
                counts[ACTIVE], counts[GRACE], counts[READONLY],
                info["readonly_org_ids"] or "—",
            )
        else:
            log.info(
                "гейт подписки выключен; если включить: active=%d grace=%d "
                "readonly=%d (закрылись бы %s)",
                counts[ACTIVE], counts[GRACE], counts[READONLY],
                info["readonly_org_ids"] or "—",
            )
    except Exception:  # noqa: BLE001 — диагностика не имеет права валить старт
        log.exception("предпросмотр гейта подписки не удался")
    finally:
        db.close()
