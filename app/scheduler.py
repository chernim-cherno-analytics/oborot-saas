"""Продовый планировщик: ежедневная синхронизация МойСклад + Telegram-дайджест.

Каждый день в 06:00 по Москве (Europe/Moscow) обходим все организации
с активным moysklad-подключением и для каждой:
  1) запускаем инкрементальный синк (переиспользуем ms_sync; ждём завершения —
     организации обрабатываются СТРОГО ПОСЛЕДОВАТЕЛЬНО, чтобы не выедать
     общие лимиты МойСклад несколькими аккаунтами разом);
  2) шлём Telegram-дайджест (notify.send_daily_digest — сам молча скипает
     org без настроенного чата).

Ждём завершения ТОЛЬКО инкремента (от него зависит дайджест); прогон,
промотированный в первичную загрузку (продолжение прерванной истории),
запускается и оставляется работать фоном — см. _started_as_initial.

Ошибка одной организации не валит остальных: синк пишет свой результат
в sync_state (state=error + человекочитаемый текст), обход продолжается.
Если авто-синк упал второй раз подряд (sync_state.fail_streak == 2) —
владельцу уходит Telegram-алерт (notify.send_sync_failure_alert, инцидент 21.08).

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
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from app import logging_conf, ms_sync, notify
from app.db import SessionLocal
from app.models import Connection, Org

log = logging.getLogger("oborot.scheduler")

MOSCOW_TZ = ZoneInfo("Europe/Moscow")
DAILY_HOUR = 6  # 06:00 МСК
SYNC_WAIT_TIMEOUT = 60 * 60  # исторический потолок ожидания (остался для тестов)
# Ревью 21.08 (мажор 5): ждём только НАСТОЯЩИЙ инкремент — он занимает секунды,
# и от него зависит дайджест. Первичная загрузка (в т.ч. продолжение прерванной)
# теперь легально идёт 30+ минут: блокироваться на ней нельзя — обход
# организаций последовательный, а max_instances=1 просто съедал бы следующие
# срабатывания (после деплоя, убившего несколько фоновых историй, ежедневный
# джоб в 06:00 вставал на часы и задерживал синки и дайджесты других org).
SYNC_WAIT_TIMEOUT_INCREMENTAL = 300
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


def _started_as_initial(org_id: int) -> bool:
    """Запущенный синк промотирован в первичную загрузку (start_sync решает сам).

    Такие прогоны длятся десятки минут (год истории чанками) — джоб их
    ЗАПУСКАЕТ и идёт дальше, не блокируя обход остальных организаций.
    """
    return ms_sync.get_status(org_id).get("mode") == "initial"


def _alert_if_failing(org_id: int, status: dict) -> None:
    """Telegram-алерт владельцу на втором провале подряд (инцидент 21.08).

    Основной вызов — в ms_sync._thread_main (после каждого провала, включая
    ручные); здесь страховка-no-op: notify.send_sync_failure_alert сам
    проверяет fail_streak/alerted_streak, настройки чата и глотает ошибки.
    """
    try:
        notify.send_sync_failure_alert(org_id, status)
    except Exception:  # noqa: BLE001
        log.exception("ошибка отправки алерта о падении синка")


def _run_daily_job() -> dict:
    """Тело ежедневного джоба. Снаружи зовут обёртку run_daily_job — она
    снимает метку организации в логе через finally; напрямую эту функцию не
    вызывают ни планировщик, ни тесты.

    Возвращает сводку {org_id: 'done'|'error'|'skipped'|...} — удобно в логах
    и тестах; каждая организация обёрнута в try/except, ошибка одной не
    прерывает обход остальных.
    """
    results: dict[int, str] = {}
    org_ids = _orgs_with_active_moysklad()
    log.info("ежедневный синк: %d организаций", len(org_ids))
    for org_id in org_ids:
        # Планировщик обходит организации ПО ОЧЕРЕДИ в одном потоке, поэтому
        # метку ставим на каждой итерации, а после цикла снимаем — иначе
        # последняя организация «прилипла» бы к служебным записям.
        logging_conf.set_org(org_id)
        # Последовательно, не параллельно: щадим лимиты МойСклад (45 req/3 c
        # на аккаунт, но и общий пул соединений сервиса не раздуваем).
        try:
            if not ms_sync.start_sync(org_id, mode="incremental"):
                results[org_id] = "skipped_already_running"
                log.warning("синк уже идёт, пропуск")
                continue
            if _started_as_initial(org_id):
                # Первичная загрузка/её продолжение: запустили — идём дальше.
                # Дайджест уйдёт по тем данным, что уже на диске (сервис на них
                # и работает), а не через час ожидания.
                results[org_id] = "started_initial"
                log.info("запущена первичная загрузка, не ждём")
            else:
                # Дайджест считается по свежему инкременту — его дожидаемся.
                status = _wait_sync_finished(org_id, SYNC_WAIT_TIMEOUT_INCREMENTAL)
                results[org_id] = status.get("state", "unknown")
                if status.get("state") == "error":
                    log.warning("синк упал: %s", status.get("error", ""))
                    _alert_if_failing(org_id, status)
        except Exception:  # noqa: BLE001 — одна org не валит остальных
            results[org_id] = "error"
            log.exception("необработанная ошибка синка")
        try:
            notify.send_daily_digest(org_id)
        except Exception:  # noqa: BLE001 — дайджест не должен ломать обход
            log.exception("ошибка отправки дайджеста")
    return results


CATCHUP_STALE_HOURS = 26  # догонять, если успешного синка не было дольше суток


def _run_catchup_job() -> dict:
    """Почасовой «догоняющий» джоб: чинит молчаливое отставание данных.

    Если у организации последний успешный синк старше CATCHUP_STALE_HOURS
    (упал в 06:00, приложение было в рестарте, токен только что поменяли) —
    пробуем инкрементальный синк ещё раз. Организации с прерванной первичной
    загрузкой (деплой П1, stats.history_loaded_from) догоняются всегда:
    start_sync сам промотирует запуск в продолжение initial. Ошибка (например, всё ещё битый
    токен) стоит секунды и остаётся видимой в sync_state/на табло свежести;
    как только причину устранят — данные догонятся в течение часа, а не
    на следующее утро.
    """
    results: dict[int, str] = {}
    cutoff = datetime.utcnow() - timedelta(hours=CATCHUP_STALE_HOURS)
    db = SessionLocal()
    try:
        rows = db.execute(
            select(Connection.org_id, Connection.last_sync_at)
            .join(Org, Org.id == Connection.org_id)
            .where(
                Connection.kind == "moysklad",
                Connection.status == "active",
                Org.status != "suspended",
            )
            .order_by(Connection.org_id)
        ).all()
    finally:
        db.close()
    stale = [org_id for org_id, ts in rows if ts is None or ts < cutoff]
    # Деплой П1: прерванная прогрессивная загрузка истории («продолжим
    # автоматически в течение часа») — догоняем независимо от last_sync_at
    # и от статуса подключения (до finalize-lite оно ещё pending).
    for org_id in ms_sync.orgs_with_resume_point():
        if org_id not in stale:
            stale.append(org_id)
    if not stale:
        return results
    log.info("догоняющий синк: %d отставших организаций", len(stale))
    for org_id in stale:
        logging_conf.set_org(org_id)
        try:
            if not ms_sync.start_sync(org_id, mode="incremental"):
                results[org_id] = "skipped_already_running"
                continue
            if _started_as_initial(org_id):
                # Продолжение прерванной первичной — самый частый случай этого
                # джоба; ждать его час означало бы не догнать остальных.
                results[org_id] = "started_initial"
                log.info("продолжение первичной загрузки запущено, не ждём")
                continue
            status = _wait_sync_finished(org_id, SYNC_WAIT_TIMEOUT_INCREMENTAL)
            results[org_id] = status.get("state", "unknown")
            if status.get("state") == "error":
                log.warning("догоняющий синк упал: %s", status.get("error", ""))
                _alert_if_failing(org_id, status)
        except Exception:  # noqa: BLE001 — одна org не валит остальных
            results[org_id] = "error"
            log.exception("необработанная ошибка догоняющего синка")
    return results


def run_daily_job() -> dict:
    """Ежедневный джоб. Метка организации в логе снимается в любом случае.

    `finally`, а не строка после цикла: потоки планировщика переиспользуются,
    и значение contextvar живёт в потоке до его конца. Выход мимо последней
    строки (BaseException, KeyboardInterrupt при остановке сервиса) оставил бы
    метку последней организации на всех последующих служебных записях — и
    разбор инцидента пошёл бы по чужой метке. Это то же свойство, что и
    изоляция данных, только для логов.
    """
    try:
        return _run_daily_job()
    finally:
        logging_conf.set_org(None)


def run_catchup_job() -> dict:
    """Почасовой догоняющий джоб. Метка снимается так же — см. run_daily_job."""
    try:
        return _run_catchup_job()
    finally:
        logging_conf.set_org(None)


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
    _scheduler.add_job(
        run_catchup_job,
        CronTrigger(minute=30, timezone=MOSCOW_TZ),  # каждый час в :30
        id="hourly-catchup-sync",
        name="Догоняющий синк: повтор после сбоя/пропуска ежедневного",
        coalesce=True,
        max_instances=1,
        misfire_grace_time=1800,
    )
    _scheduler.start()
    _started = True
    log.info("планировщик запущен: ежедневно в %02d:00 МСК + почасовой догон", DAILY_HOUR)


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
