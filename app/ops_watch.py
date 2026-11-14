"""Наблюдение оператора: падающие синки клиентов и необработанные ошибки сервера.

ЗАЧЕМ (OPS-WATCH-1, 14.11.2026). Синхронизация организации владельца молчала
46 дней: МойСклад перестал принимать токен 28.09, синк провалился 1 146 раз
подряд, и не ушло ни одного оповещения. Причина не в коде алерта, а в его
адресате: `notify.send_sync_failure_alert` пишет только в чат ОРГАНИЗАЦИИ, и
если клиент не подключил Telegram (а подключают единицы), о беде не узнаёт
никто. Для продаваемого сервиса это неприемлемо: «каждое утро» в обещании
лендинга ломается тихо.

Этот модуль добавляет второй адресат: служебный канал оператора
(`notify.send_ops`, только `OBOROT_OPS_CHAT_ID`). Правило канала прежнее:
текст фиксированный, в нём только число и время, без имён организаций, ошибок
и строк извне. Кто именно падает, оператор смотрит на сервере:
`python tools/ops_report.py`.

ЧТО СЧИТАЕТСЯ «СИНК НЕ ПРОХОДИТ». Организация с активным подключением
МойСклада, не приостановленная (`orgs.status`), у которой:
  * `sync_state.fail_streak >= FAIL_STREAK_MIN` (несколько провалов подряд), или
  * последний успешный синк (`connections.last_sync_at`) старше STALE_HOURS.
Демо-подключения и организации без МойСклада не считаются: им нечего
синхронизировать. Не считаются и те, кого планировщик не синкает намеренно:
незавершённое подключение (pending), организация в readonly при включённом
гейте, идущая первичная загрузка.

КОГДА ШЛЁМ. Не чаще одного раза на один и тот же набор падающих организаций
в REMIND_HOURS. Новый набор (кто-то добавился) — сразу. Пустой набор —
молчим и забываем прошлое, следующий сбой снова придёт сразу.

Состояние «что уже отправлено» живёт в памяти процесса. Это сознательно:
прод работает строго в один процесс (OPS-1, D-17), а после перезапуска одно
повторное оповещение лучше, чем таблица и миграция ради дедупликации.

НЕОБРАБОТАННЫЕ ОШИБКИ. Обработчик в app/main.py зовёт `record_server_error`;
здесь только счётчик моментов в памяти. Раз в цикл наблюдения, если за
последний час были ошибки, которых оператор ещё не видел, уходит одно
оповещение с их числом.
"""
from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import Connection, Org, SyncState

log = logging.getLogger("oborot.ops_watch")

FAIL_STREAK_MIN = 3
STALE_HOURS = 30
REMIND_HOURS = 24
ERROR_WINDOW = timedelta(hours=1)
_ERRORS_CAP = 10_000  # потолок памяти на шквал ошибок

_lock = threading.Lock()
_errors: deque[datetime] = deque(maxlen=_ERRORS_CAP)
_errors_alerted_until: datetime | None = None
_sync_alerted: tuple[frozenset[int], datetime] | None = None


def _now() -> datetime:
    return datetime.utcnow()


# ── Синки ────────────────────────────────────────────────────────────────────

def failing_syncs(db: Session, now: datetime | None = None) -> list[dict]:
    """Организации, у которых синк МойСклада не проходит. Только чтение."""
    now = now or _now()
    stale_before = now - timedelta(hours=STALE_HOURS)
    rows = db.execute(
        select(Org, Connection, SyncState)
        .join(Connection, (Connection.org_id == Org.id) & (Connection.kind == "moysklad"))
        .outerjoin(SyncState, SyncState.org_id == Org.id)
    ).all()
    out = []
    from app import subscription

    for org, conn, state in rows:
        if (org.status or "active") != "active":
            continue
        # Те же правила, что у планировщика (ревью критика 14.11, P2): кого
        # планировщик НАМЕРЕННО не синкает, тот не «падает».
        #  * pending без единого провала: подключение не доведено (склады не
        #    выбраны, загрузка не запускалась). Брошенный онбординг, не сбой;
        #  * гейт подписки закрыл запись (readonly): синк остановлен нами;
        #  * идёт первичная загрузка: старый аккаунт, только что подключивший
        #    МойСклад, иначе помечался бы сразу (у подключения нет своей даты).
        streak = int(getattr(state, "fail_streak", 0) or 0) if state else 0
        # Pending пропускаем, только пока не было ни одного провала: первичная
        # загрузка, упавшая после фазы «сегодня», остаётся pending, а почасовой
        # догон её ретраит (ms_sync.orgs_with_resume_point) и каждый час
        # роняет. Это самый ценный клиент, первая неделя (ревью 14.11, P1).
        if conn.status == "pending" and streak == 0:
            continue
        if subscription.gate_enabled():
            try:
                if subscription.subscription_state(org, db) == subscription.READONLY:
                    continue
            except Exception:  # noqa: BLE001 — сомнение трактуем как «смотреть»
                pass
        # Зазор: продолжение, запущенное догоном в :30, может ещё идти в :50.
        # Он временный: при следующем провале серия вырастет, и алерт придёт.
        if state is not None and state.state == "running" and (state.mode or "") == "initial":
            continue
        last_ok = conn.last_sync_at
        if last_ok is None:
            # Ни одного успешного синка у активного подключения: тревога,
            # только если организация подключилась давно.
            created = org.created_at
            stale = created is not None and created < stale_before
        else:
            stale = last_ok < stale_before
        if streak >= FAIL_STREAK_MIN or stale:
            out.append({
                "org_id": org.id,
                "fail_streak": streak,
                "last_sync_at": last_ok,
                "state": getattr(state, "state", "") if state else "",
                "error": (getattr(state, "error", "") or "") if state else "",
                "stale": stale,
            })
    return out


def _sync_alert_due(ids: frozenset[int], now: datetime) -> bool:
    global _sync_alerted
    if not ids:
        _sync_alerted = None
        return False
    if _sync_alerted is None:
        return True
    prev_ids, sent_at = _sync_alerted
    if not ids <= prev_ids:
        return True  # появилась новая падающая организация
    return now - sent_at >= timedelta(hours=REMIND_HOURS)


# ── Ошибки сервера ───────────────────────────────────────────────────────────

def record_server_error(now: datetime | None = None) -> None:
    """Отметить необработанную ошибку. Ничего не шлёт и не ходит в базу."""
    with _lock:
        _errors.append(now or _now())


def recent_server_errors(now: datetime | None = None) -> int:
    now = now or _now()
    with _lock:
        return sum(1 for t in _errors if now - t <= ERROR_WINDOW)


def _unseen_errors(now: datetime) -> int:
    with _lock:
        since = _errors_alerted_until
        return sum(1 for t in _errors
                   if now - t <= ERROR_WINDOW and (since is None or t > since))


# ── Цикл наблюдения ──────────────────────────────────────────────────────────

def run_once(now: datetime | None = None, send=None) -> dict:
    """Один проход наблюдения. Возвращает, что решено, для журнала и тестов.

    `send(kind, unit)` по умолчанию `notify.send_ops`. Любая ошибка здесь
    глотается и пишется в журнал: наблюдатель не должен ронять планировщик.
    """
    global _sync_alerted, _errors_alerted_until
    from app import notify

    now = now or _now()
    send = send or notify.send_ops
    result = {"failing": 0, "sync_sent": False, "errors": 0, "errors_sent": False}
    try:
        db = SessionLocal()
        try:
            failing = failing_syncs(db, now)
        finally:
            db.close()
        ids = frozenset(f["org_id"] for f in failing)
        result["failing"] = len(ids)
        if _sync_alert_due(ids, now):
            ok, reason = send("sync-failing", str(len(ids)))
            result["sync_sent"] = ok
            if ok:
                _sync_alerted = (ids, now)
            else:
                log.warning("оповещение о падающих синках не отправлено: %s", reason)
        unseen = _unseen_errors(now)
        result["errors"] = unseen
        if unseen:
            ok, reason = send("server-errors", str(unseen))
            result["errors_sent"] = ok
            if ok:
                _errors_alerted_until = now
            else:
                log.warning("оповещение об ошибках сервера не отправлено: %s", reason)
    except Exception:  # noqa: BLE001 — наблюдатель никогда не роняет планировщик
        log.exception("наблюдение оператора: необработанная ошибка")
    return result


def reset_for_tests() -> None:
    global _sync_alerted, _errors_alerted_until
    with _lock:
        _errors.clear()
    _sync_alerted = None
    _errors_alerted_until = None
