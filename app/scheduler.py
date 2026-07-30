"""Продовый планировщик: ежедневная синхронизация МойСклад + Telegram-дайджест.

Каждый день в 06:00 по Москве (Europe/Moscow) обходим все организации
с активным moysklad-подключением и для каждой:
  1) запускаем инкрементальный синк (переиспользуем ms_sync; ждём завершения —
     организации обрабатываются СТРОГО ПОСЛЕДОВАТЕЛЬНО, чтобы не выедать
     общие лимиты МойСклад несколькими аккаунтами разом);
  2) шлём Telegram-дайджест (notify.send_daily_digest — сам молча скипает
     org без настроенного чата).

Ошибка одной организации не валит остальных: синк пишет свой результат
в sync_state (state=error + человекочитаемый текст), обход продолжается.

Интеграция: main.py (зона оркестратора) вызывает scheduler.attach(app) —
это вешает startup/shutdown-хендлеры FastAPI. Env SCHEDULER_ENABLED=0
отключает планировщик (тесты, dev-запуски).

Защита от двойного запуска: планировщик стартует не более одного раза
на процесс (_started). При нескольких uvicorn-воркерах каждый процесс
поднял бы свой планировщик и джоб выполнился бы дважды — для MVP прод
крутится в ОДИН воркер (Render, python run.py), поэтому межпроцессный
лок не делаем; при переходе на несколько воркеров нужен лок в БД/Redis
или вынос планировщика в отдельный процесс.
"""
import logging
import os
import time
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from app import ms_sync, notify
from app.db import SessionLocal
from app.models import Connection, Org

log = logging.getLogger("oborot.scheduler")

MOSCOW_TZ = ZoneInfo("Europe/Moscow")
DAILY_HOUR = 6  # 06:00 МСК
SYNC_WAIT_TIMEOUT = 60 * 60  # ждать завершения синка одной org не дольше часа
SYNC_POLL_SEC = 2.0

_scheduler: BackgroundScheduler | None = None
_started = False


def _enabled() -> bool:
    return os.environ.get("SCHEDULER_ENABLED", "1").strip().lower() not in ("0", "false", "no", "")


# ── Джоб ─────────────────────────────────────────────────────────────────────

def _orgs_with_active_moysklad() -> list[int]:
    """org_id организаций с активным moysklad-подключением.

    Организации со status='suspended' (Uninstall/Suspend приложения из
    каталога МойСклад — см. routes_ms_vendor) пропускаются: их access_token
    отозван МС, синк только зря молотил бы ошибки.
    """
    db = SessionLocal()
    try:
        rows = db.execute(
            select(Connection.org_id)
            .join(Org, Org.id == Connection.org_id)
            .where(
                Connection.kind == "moysklad",
                Connection.status == "active",
                Org.status != "suspended",
            )
            .order_by(Connection.org_id)
        ).all()
        return [org_id for (org_id,) in rows]
    finally:
        db.close()


def _wait_sync_finished(org_id: int, timeout: float = SYNC_WAIT_TIMEOUT) -> dict:
    """Блокирующе ждёт конца синка org (ms_sync работает в своём потоке)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not ms_sync.is_running(org_id):
            return ms_sync.get_status(org_id)
        time.sleep(SYNC_POLL_SEC)
    return ms_sync.get_status(org_id)  # таймаут: вернём как есть, идём дальше


def run_daily_job() -> dict:
    """Тело ежедневного джоба; вызывается и планировщиком, и тестами напрямую.

    Возвращает сводку {org_id: 'done'|'error'|'skipped'|...} — удобно в логах
    и тестах; каждая организация обёрнута в try/except, ошибка одной не
    прерывает обход остальных.
    """
    results: dict[int, str] = {}
    org_ids = _orgs_with_active_moysklad()
    log.info("ежедневный синк: %d организаций", len(org_ids))
    for org_id in org_ids:
        # Последовательно, не параллельно: щадим лимиты МойСклад (45 req/3 c
        # на аккаунт, но и общий пул соединений сервиса не раздуваем).
        try:
            if not ms_sync.start_sync(org_id, mode="incremental"):
                results[org_id] = "skipped_already_running"
                log.warning("org=%s: синк уже идёт, пропуск", org_id)
                continue
            status = _wait_sync_finished(org_id)
            results[org_id] = status.get("state", "unknown")
            if status.get("state") == "error":
                log.warning("org=%s: синк упал: %s", org_id, status.get("error", ""))
        except Exception:  # noqa: BLE001 — одна org не валит остальных
            results[org_id] = "error"
            log.exception("org=%s: необработанная ошибка синка", org_id)
        try:
            notify.send_daily_digest(org_id)
        except Exception:  # noqa: BLE001 — дайджест не должен ломать обход
            log.exception("org=%s: ошибка отправки дайджеста", org_id)
    return results


# ── Жизненный цикл ───────────────────────────────────────────────────────────

def start() -> None:
    """Поднимает BackgroundScheduler с ежедневным джобом (идемпотентно)."""
    global _scheduler, _started
    if _started:
        return
    if not _enabled():
        log.info("планировщик выключен (SCHEDULER_ENABLED=0)")
        return
    _scheduler = BackgroundScheduler(timezone=MOSCOW_TZ)
    _scheduler.add_job(
        run_daily_job,
        CronTrigger(hour=DAILY_HOUR, minute=0, timezone=MOSCOW_TZ),
        id="daily-sync-digest",
        name="Ежедневный синк МойСклад + Telegram-дайджест",
        coalesce=True,        # пропущенные срабатывания схлопываем в одно
        max_instances=1,      # второй запуск не стартует, пока идёт первый
        misfire_grace_time=3600,
    )
    _scheduler.start()
    _started = True
    log.info("планировщик запущен: ежедневно в %02d:00 МСК", DAILY_HOUR)


def shutdown() -> None:
    """Останавливает планировщик (без ожидания работающего джоба)."""
    global _scheduler, _started
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
        except Exception:  # noqa: BLE001 — shutdown не должен ронять приложение
            log.exception("ошибка остановки планировщика")
    _scheduler = None
    _started = False


def attach(app) -> None:
    """Вешает запуск/остановку планировщика на lifecycle FastAPI-приложения.

    Вызывается из main.py: `from app import scheduler; scheduler.attach(app)`.
    """
    # Starlette 1.0 убрал add_event_handler; on_event-шим FastAPI ещё работает
    # (main.py использует его же). При переходе на lifespan — перенести туда.
    app.on_event("startup")(start)
    app.on_event("shutdown")(shutdown)
