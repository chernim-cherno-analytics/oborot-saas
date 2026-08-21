"""Синхронизация данных МойСклад → БД «Оборота».

Первичный синк (mode='initial'), этапы и доли прогресса:
  products      (0–8%)   — entity/assortment: товары и модификации, цены;
  stock_history (8–70%)  — report/stock/all с moment ВНУТРИ filter, по каждой
                           дате за HISTORY_DAYS × каждому активному складу,
                           чанками по STOCK_CHUNK_DATES с записью после каждого
                           (инцидент 21.08: сбой не теряет загруженное, следующий
                           запуск продолжает с stats.stock_loaded_until);
                           суммарно → stock_days, последняя дата → warehouse_stock;
  sales         (70–95%) — retaildemand + demand + salesreturn c expand=positions
                           за HISTORY_DAYS, фильтр по выбранным складам → sales;
  incoming      (95–97%) — «едет к нам» из МС: entity/purchaseorder за
                           HISTORY_DAYS, по позициям quantity − shipped
                           (проведённые доки) → ordered_qty.ms_qty;
  finalize      (97–100%)— connection.status='active', сброс кэша аналитики.

Инкрементальный синк (mode='incremental'): обновление цен из ассортимента,
живые остатки на сегодня (+ явные нули), перезапись продаж за последние
SYNC_DAYS_BACK дней (окно — так legacy чинил дыры от опоздавших документов).

Приёмы, портированные из legacy (проверены на реальном бренде):
- «явный ноль»: позиция с прошлым остатком >0, исчезнувшая из отчёта, получает
  qty=0 — иначе фронт вечно тянет последний положительный остаток, а dis тикает
  (правило самоизлечивающееся: после записи нуля позиция выпадает из prev);
- цены МойСклад — в копейках (везде /100);
- base_name/size: характеристика «Размер» модификации, иначе финальные скобки
  имени как в legacy _canon_name.

Прогресс пишется в таблицу sync_state; запуск — фоновым потоком со своим
event loop (start_sync). APScheduler намеренно не подключён — планировщик
прода отдельная задача, есть только ручка POST /api/sync/run.
"""
import asyncio
import json
import os
import re
import threading
from datetime import date, datetime, timedelta

from sqlalchemy import delete, insert, inspect, select, text, update

from app import analytics, exclusions
from app.crypto import decrypt_token
from app.db import SessionLocal, engine
from app.models import (
    Connection,
    OrderedQty,
    Product,
    Sale,
    StockDay,
    SyncState,
    Warehouse,
    WarehouseStock,
)
from app.ms_client import MoySkladClient, _env_int

HISTORY_DAYS = int(os.environ.get("HISTORY_DAYS", "365"))
SALES_RESYNC_DAYS = int(os.environ.get("SYNC_DAYS_BACK", "3"))
# Инцидент 21.08: история остатков качается и ПИШЕТСЯ чанками по N дат —
# сбой на середине года не теряет уже загруженное (см. _sync_stock_history).
STOCK_CHUNK_DATES = _env_int("STOCK_CHUNK_DATES", 30, minimum=1)
try:
    CHUNK_PAUSE_SECONDS = max(0.0, float(os.environ.get("MS_CHUNK_PAUSE", "2")))
except ValueError:
    CHUNK_PAUSE_SECONDS = 2.0

# Ожидаемый темп с учётом лимитов МойСклад (45 req / 3 c ≈ 15 rps, берём с запасом).
EFFECTIVE_RPS = 12.0

_SIZE_SUFFIX_RE = re.compile(r"\s*\(([^)]*)\)\s*$")

_threads: dict[int, threading.Thread] = {}
_threads_lock = threading.Lock()


def ensure_schema() -> None:
    """Аддитивные мини-миграции: ordered_qty.ms_qty, sync_state.fail_streak,
    productions.stages_json/moq_units.

    Base.metadata.create_all не изменяет существующие таблицы (паттерн —
    app.ms_writeback.ensure_schema). Свежая БД получает колонки из моделей.
    """
    insp = inspect(engine)
    if insp.has_table("ordered_qty"):
        cols = {c["name"] for c in insp.get_columns("ordered_qty")}
        if "ms_qty" not in cols:
            with engine.begin() as conn:
                conn.execute(text(
                    "ALTER TABLE ordered_qty ADD COLUMN ms_qty FLOAT NOT NULL DEFAULT 0"
                ))
    if insp.has_table("sync_state"):
        cols = {c["name"] for c in insp.get_columns("sync_state")}
        # Инцидент 21.08: счётчики для алерта о падающем синке.
        for col in ("fail_streak", "alerted_streak"):
            if col not in cols:
                with engine.begin() as conn:
                    conn.execute(text(
                        f"ALTER TABLE sync_state ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0"
                    ))
    if insp.has_table("products"):
        cols = {c["name"] for c in insp.get_columns("products")}
        # 21.08: полная себестоимость отдельно от закупочной (см. models.Product).
        if "cost_full" not in cols:
            with engine.begin() as conn:
                conn.execute(text(
                    "ALTER TABLE products ADD COLUMN cost_full FLOAT NOT NULL DEFAULT 0"
                ))
    if insp.has_table("productions"):
        cols = {c["name"] for c in insp.get_columns("productions")}
        if "cadence_days" not in cols:
            with engine.begin() as conn:
                conn.execute(text(
                    "ALTER TABLE productions ADD COLUMN cadence_days INTEGER NOT NULL DEFAULT 0"
                ))
        # Мастер заказа 21.08: этапы производства и минимальная партия.
        if "stages_json" not in cols:
            with engine.begin() as conn:
                conn.execute(text(
                    "ALTER TABLE productions ADD COLUMN stages_json TEXT NOT NULL DEFAULT ''"
                ))
        if "moq_units" not in cols:
            with engine.begin() as conn:
                conn.execute(text(
                    "ALTER TABLE productions ADD COLUMN moq_units INTEGER NOT NULL DEFAULT 0"
                ))


# ── Имена и размеры (как в legacy _canon_name) ───────────────────────────────

def strip_size(name: str) -> str:
    """Каноническое имя без финальных скобок-размера: 'Худи (S)' → 'Худи'."""
    return _SIZE_SUFFIX_RE.sub("", str(name or "")).strip()


def parse_size_suffix(name: str) -> str:
    """Размер из финальных скобок имени: 'Худи (S)' → 'S'; нет скобок — ''."""
    m = _SIZE_SUFFIX_RE.search(str(name or ""))
    return m.group(1).strip() if m else ""


def _href_id(href: str | None) -> str:
    """UUID сущности из meta.href (query-параметры отбрасываются)."""
    if not href:
        return ""
    return href.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]


def _kopecks(value) -> float:
    """Копейки МойСклад → рубли."""
    try:
        return float(value or 0) / 100.0
    except (TypeError, ValueError):
        return 0.0


# ── Состояние синка (sync_state) ─────────────────────────────────────────────

def _set_state(org_id: int, **fields) -> None:
    db = SessionLocal()
    try:
        row = db.get(SyncState, org_id)
        if row is None:
            row = SyncState(org_id=org_id)
            db.add(row)
        for key, value in fields.items():
            setattr(row, key, value)
        db.commit()
    finally:
        db.close()


def get_status(org_id: int) -> dict:
    """GET /api/sync/status: текущее состояние синхронизации организации."""
    db = SessionLocal()
    try:
        row = db.get(SyncState, org_id)
    finally:
        db.close()
    if row is None:
        return {"state": "idle", "stage": "", "progress_pct": 0, "detail": "",
                "started_at": None, "finished_at": None, "stats": {}, "error": "",
                "fail_streak": 0, "alerted_streak": 0}
    return {
        "state": row.state,
        "mode": row.mode,
        "stage": row.stage,
        "progress_pct": round(row.progress, 1),
        "detail": row.detail,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "stats": row.stats,
        "error": row.error,
        "fail_streak": int(row.fail_streak or 0),
        "alerted_streak": int(row.alerted_streak or 0),
    }


def _bump_fail_streak(org_id: int) -> int:
    """+1 к числу подряд упавших синков; возвращает новое значение."""
    db = SessionLocal()
    try:
        row = db.get(SyncState, org_id)
        if row is None:
            return 0
        row.fail_streak = int(row.fail_streak or 0) + 1
        db.commit()
        return row.fail_streak
    finally:
        db.close()


def claim_failure_alert(org_id: int) -> bool:
    """Атомарно «забирает» право на алерт о падающей серии (ревью 21.08).

    True ровно один раз за серию: когда fail_streak >= 2 и alerted_streak == 0
    (после done оба сбрасываются). Считает и ручные, и авто-запуски — раньше
    планировщик проверял только `== 2` и пропускал серию, если второй провал
    был ручным.
    """
    db = SessionLocal()
    try:
        n = db.execute(
            update(SyncState)
            .where(SyncState.org_id == org_id, SyncState.fail_streak >= 2,
                   SyncState.alerted_streak == 0)
            .values(alerted_streak=SyncState.fail_streak)
        ).rowcount
        db.commit()
        return n == 1
    finally:
        db.close()


def clear_resume_point(org_id: int) -> None:
    """Снимает точку продолжения прерванной первичной загрузки (ревью 21.08).

    Зовётся при смене набора складов: история, записанная до прерывания,
    считалась по старому набору — продолжать её нельзя, нужна полная пересборка.
    """
    db = SessionLocal()
    try:
        row = db.get(SyncState, org_id)
        if row is None:
            return
        stats = row.stats
        if "stock_loaded_until" in stats or "resume_fp" in stats:
            stats.pop("stock_loaded_until", None)
            stats.pop("resume_fp", None)
            # Частичная история уже на диске: молчаливой дыры не оставляем —
            # следующий запуск (даже инкрементный) станет полной пересборкой.
            stats["needs_full_rebuild"] = True
            row.stats_json = json.dumps(stats, ensure_ascii=False)
            db.commit()
    finally:
        db.close()


def _resume_fingerprint(active_wh: list) -> str:
    """Отпечаток условий загрузки: активные склады + окно истории."""
    return ",".join(sorted(w.ext_id for w in active_wh)) + f"|{HISTORY_DAYS}"


def reset_stale_running() -> None:
    """Сброс зависших состояний при старте процесса (аудит 18.08).

    'running' снимался только except-веткой _thread_main — если процесс убили
    (деплой, OOM, рестарт хостинга), строка оставалась 'running' навсегда,
    is_running() врал, и авто-/ручные синки организации блокировались до
    ручного вмешательства. На старте живых потоков нет по определению —
    все 'running' в БД заведомо мёртвые.
    """
    db = SessionLocal()
    try:
        n = db.execute(
            update(SyncState).where(SyncState.state == "running").values(
                state="error",
                error="Синхронизация прервана перезапуском сервера — запустите ещё раз",
                finished_at=datetime.utcnow(),
            )
        ).rowcount
        db.commit()
        if n:
            print(f"ms_sync: сброшено зависших состояний running: {n}")
    finally:
        db.close()


def is_running(org_id: int) -> bool:
    with _threads_lock:
        thread = _threads.get(org_id)
        if thread is not None and thread.is_alive():
            return True
    return get_status(org_id)["state"] == "running"


def estimate_minutes(n_dates: int, n_stores: int) -> float:
    """Честная оценка длительности: даты×склады + ассортимент + документы."""
    requests = n_dates * max(1, n_stores) + 30
    return round(requests / EFFECTIVE_RPS / 60.0, 1)


# Ключи stats, переживающие перезапуск синка до успешного done (ревью 21.08).
_CARRIED_STATS = ("stock_loaded_until", "resume_fp", "needs_full_rebuild")


def _pending_resume(org_id: int) -> str | None:
    """Дата, до которой дошёл прерванный первичный синк (stats.stock_loaded_until).

    Инцидент 21.08: прерванный на середине первичный синк оставляет ЧАСТИЧНУЮ
    новую историю (старая уже стёрта первым _flush). Пока она не дозагружена,
    любой следующий запуск — initial ИЛИ incremental (кнопка «Синхронизировать
    сейчас», планировщик) — продолжает первичную загрузку со следующей даты:
    инкремент поверх дыры в истории дал бы ложные нули и битую оборачиваемость.
    Исключение — явная «Полная пересборка» (start_sync(force_full=True)):
    она всегда начинает с нуля и снимает точку продолжения (ревью 21.08).
    """
    st = get_status(org_id)
    if st.get("state") != "error":
        return None
    until = (st.get("stats") or {}).get("stock_loaded_until")
    return str(until) if until else None


def needs_full_rebuild(org_id: int) -> bool:
    """Помечена ли организация на полную пересборку (clear_resume_point)."""
    return bool((get_status(org_id).get("stats") or {}).get("needs_full_rebuild"))


def has_resume_point(org_id: int) -> bool:
    """Есть ли прерванная первичная загрузка, которую надо продолжить."""
    return _pending_resume(org_id) is not None


def start_sync(org_id: int, mode: str, *, force_full: bool = False) -> bool:
    """Запускает фоновый синк (initial | incremental). False — уже идёт.

    force_full=True — настоящая полная пересборка (кнопка «Полная пересборка»,
    подсказка после смены складов): точка продолжения игнорируется и снимается.
    """
    with _threads_lock:
        thread = _threads.get(org_id)
        if thread is not None and thread.is_alive():
            return False
        resume_from = None if force_full else _pending_resume(org_id)
        prev_stats = get_status(org_id).get("stats") or {}
        prev_fp = prev_stats.get("resume_fp", "")
        if resume_from:
            mode = "initial"  # дозагрузка прерванной истории (см. _pending_resume)
        elif prev_stats.get("needs_full_rebuild"):
            # Набор складов сменился при частичной истории (clear_resume_point):
            # любой вызов — полная пересборка с нуля; флаг снимается при done.
            mode = "initial"
            resume_from = None
        # Ревью 21.08 (2): переносимые ключи НЕ обнуляем на старте — если запуск
        # упадёт ещё до истории остатков (429/401 на ассортименте), точка
        # продолжения / флаг пересборки обязаны пережить и этот провал. Снимает
        # их только done (или реально случившийся wipe в _flush).
        carried = {k: prev_stats[k] for k in _CARRIED_STATS if k in prev_stats}
        carried["mode"] = mode
        _set_state(
            org_id,
            state="running", mode=mode, stage="queued", progress=0.0,
            detail="Синхронизация поставлена в очередь", error="",
            stats_json=json.dumps(carried, ensure_ascii=False),
            started_at=datetime.utcnow(), finished_at=None,
        )
        thread = threading.Thread(
            target=_thread_main, args=(org_id, mode, resume_from, prev_fp),
            name=f"ms-sync-{mode}-{org_id}", daemon=True,
        )
        _threads[org_id] = thread
        thread.start()
        return True


def _thread_main(org_id: int, mode: str, resume_from: str | None = None,
                 prev_fp: str = "") -> None:
    try:
        asyncio.run(_run_sync(org_id, mode, resume_from, prev_fp))
    except Exception as exc:  # noqa: BLE001 — любой сбой фиксируем в состоянии
        stats = get_status(org_id).get("stats") or {}
        stats["error_cause"] = error_cause(exc)  # для подсказки в Telegram-алерте
        _set_state(
            org_id,
            state="error", error=_human_error(exc),
            detail=str(exc)[:500], finished_at=datetime.utcnow(),
            stats_json=json.dumps(stats, ensure_ascii=False),
        )
        try:
            _bump_fail_streak(org_id)
            # Алерт шлём отсюда (и ручные, и авто-запуски), планировщик — страховка.
            from app import notify as _notify
            _notify.send_sync_failure_alert(org_id, get_status(org_id))
        except Exception:  # noqa: BLE001 — счётчик/алерт не должны маскировать ошибку синка
            pass


class SyncInterrupted(Exception):
    """Первичная загрузка прервана после того, как часть истории уже записана.

    str(exc) — готовый человеческий текст для sync_state.error (инцидент 21.08);
    cause — класс причины (token | transient | internal) для подсказок.
    """

    def __init__(self, message: str, cause: str = "transient") -> None:
        super().__init__(message)
        self.cause = cause


def _is_rate_limited(exc: Exception) -> bool:
    import httpx
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429


def _human_error(exc: Exception) -> str:
    import httpx

    if isinstance(exc, SyncInterrupted):
        return str(exc)
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 403):
            return ("МойСклад не принял токен доступа. Проверьте токен в настройках "
                    "и запустите синхронизацию заново.")
        if code == 429:
            return ("МойСклад ограничил частоту запросов (429). Подождите пару минут "
                    "и запустите синхронизацию ещё раз.")
        return f"МойСклад ответил ошибкой {code}. Попробуйте повторить синхронизацию позже."
    if isinstance(exc, httpx.HTTPError):
        return "Не удалось связаться с МойСклад: проблема с сетью. Попробуйте позже."
    if isinstance(exc, RuntimeError):
        return f"Синхронизация прервана: {exc}"  # наши собственные понятные тексты
    # Ревью 21.08: внутренности (SQL, KeyError) пользователю не показываем.
    print(f"ms_sync: внутренняя ошибка синка: {exc!r}")
    return "Синхронизация прервана внутренней ошибкой — мы уже смотрим."


def error_cause(exc: Exception) -> str:
    """Класс причины для подсказок: token | transient | internal."""
    import httpx

    if isinstance(exc, SyncInterrupted):
        return exc.cause
    if isinstance(exc, httpx.HTTPStatusError):
        if exc.response.status_code in (401, 403):
            return "token"
        return "transient"
    if isinstance(exc, httpx.HTTPError):
        return "transient"
    return "internal"


# ── Основной прогон ──────────────────────────────────────────────────────────

async def _run_sync(org_id: int, mode: str, resume_from: str | None = None,
                    prev_fp: str = "") -> None:
    """Полный прогон синка. resume_from — дата, до которой (включительно) история
    остатков уже записана прерванным первичным синком: качаем дальше без wipe
    (только если prev_fp — отпечаток складов/окна той загрузки — совпадает).
    """
    db = SessionLocal()
    try:
        conn = db.execute(
            select(Connection).where(
                Connection.org_id == org_id, Connection.kind == "moysklad"
            )
        ).scalars().first()
        if conn is None:
            raise RuntimeError("Подключение МойСклад не настроено")
        token = decrypt_token(conn.token_enc)
        if not token:
            raise RuntimeError("Токен МойСклад не читается — подключите заново")
        warehouses = db.execute(
            select(Warehouse).where(Warehouse.org_id == org_id, Warehouse.ext_id != "")
        ).scalars().all()
        active_wh = [w for w in warehouses if w.active]
        if not active_wh:
            raise RuntimeError("Не выбран ни один склад для синхронизации")
    finally:
        db.close()

    # Стартуем от сохранённых stats (start_sync оставил там переносимые ключи),
    # чтобы каждый _set_state(stats_json=...) нёс их до самого done.
    stats: dict = {k: v for k, v in (get_status(org_id).get("stats") or {}).items()
                   if k in _CARRIED_STATS}
    stats["mode"] = mode
    # Аудит 18.08: инкремент раньше качал ровно 1 день остатков и 3 дня продаж
    # НЕЗАВИСИМО от того, сколько сервис простоял — простой >3 суток оставлял
    # невосполнимые дыры. Теперь окно растягивается по фактическому разрыву
    # с последнего УСПЕШНОГО синка (+1 день перекрытия), в пределах HISTORY_DAYS.
    gap_days = 0
    if mode != "initial" and conn.last_sync_at is not None:
        gap_days = max(0, (date.today() - conn.last_sync_at.date()).days)
    history_days = HISTORY_DAYS if mode == "initial" else min(HISTORY_DAYS, max(1, gap_days + 1))
    sales_days = HISTORY_DAYS if mode == "initial" else min(
        HISTORY_DAYS, max(SALES_RESYNC_DAYS, gap_days + 1))
    if gap_days > 1:
        stats["gap_days"] = gap_days

    async with MoySkladClient(token) as client:
        # ── Этап 1: товары ──────────────────────────────────────────────────
        _set_state(org_id, stage="products", progress=1.0,
                   detail="Загружаем ассортимент (товары и размеры)…")
        assortment = await client.fetch_assortment()
        # Какие типы цен считать «ценой продажи» и «полной себестоимостью»:
        # выбор организации, иначе угадываем по названию (см. _price_by).
        _load_price_types(org_id)
        stats["price_types"] = price_type_names(assortment)[:20]
        ext_to_pid = _upsert_products(org_id, assortment, stats)
        _set_state(org_id, stage="products", progress=8.0,
                   detail=f"Товары обновлены: {stats['products_total']} позиций",
                   stats_json=json.dumps(stats, ensure_ascii=False))

        # ── Этап 2: история остатков ────────────────────────────────────────
        today = date.today()
        dates = [
            (today - timedelta(days=offset)).isoformat()
            for offset in range(history_days - 1, -1, -1)
        ]
        fingerprint = _resume_fingerprint(active_wh)
        if (mode == "initial" and resume_from and _has_stock_rows(org_id)
                and resume_from >= dates[0] and prev_fp == fingerprint):
            # Продолжение прерванной загрузки (инцидент 21.08): со следующей
            # даты после записанной; сегодня перекачиваем всегда — из него
            # строится warehouse_stock. Ревью 21.08: точка старше окна или
            # записанная при другом наборе складов/окне — не продолжаем,
            # а пересобираем с нуля.
            dates = [d for d in dates if d > resume_from] or [today.isoformat()]
            stats["resumed_from"] = resume_from
            # Точку продолжения сохраняем СРАЗУ: если и этот запуск упадёт до
            # первого чанка (429 на первом запросе, 401 после смены токена),
            # следующий всё равно продолжит, а не сделает инкремент над дырой.
            stats["stock_loaded_until"] = resume_from
            stats["resume_fp"] = fingerprint
            _set_state(org_id, stats_json=json.dumps(stats, ensure_ascii=False))
        else:
            resume_from = None
        # Свежая/принудительная пересборка: прежние stock_loaded_until/resume_fp/
        # needs_full_rebuild живут до первого реального wipe в _flush (там и
        # снимаются), новый отпечаток пишется с первого чанка.
        try:
            await _sync_stock_history(org_id, client, active_wh, dates, ext_to_pid,
                                      stats, initial=(mode == "initial"),
                                      resume_from=resume_from)

            # ── Этап 3: продажи ─────────────────────────────────────────────
            await _sync_sales(org_id, client, active_wh, sales_days, ext_to_pid,
                              stats, initial=(mode == "initial"))

            # ── Этап 4: «едет к нам» из МС (заказы поставщику) ──────────────
            await _sync_incoming(org_id, client, ext_to_pid, stats)
        except Exception as exc:  # noqa: BLE001 — фиксируем прогресс и пробрасываем
            stats["ms_client"] = dict(client.stats)
            loaded_until = stats.get("stock_loaded_until") or resume_from
            if mode != "initial" or not loaded_until:
                _set_state(org_id, stats_json=json.dumps(stats, ensure_ascii=False))
                raise
            # Часть новой истории уже записана — сохраняем точку для продолжения
            # и объясняем пользователю, что ничего не потеряно.
            stats["stock_loaded_until"] = loaded_until
            _set_state(org_id, stats_json=json.dumps(stats, ensure_ascii=False))
            cause = error_cause(exc)
            if cause == "token":
                # Ревью 21.08: «нажмите ещё раз» при 401/403 бессмысленно —
                # сначала токен; точка продолжения при этом сохранена.
                raise SyncInterrupted(
                    f"Загрузка прервана на {loaded_until}: {_human_error(exc)} "
                    "Загруженное сохранено — после исправления токена загрузка "
                    "продолжится с этой даты.", cause) from exc
            reason = ("МойСклад ограничил частоту запросов." if _is_rate_limited(exc)
                      else _human_error(exc))
            raise SyncInterrupted(
                f"Загрузка прервана на {loaded_until}: {reason} Загруженное "
                "сохранено — нажмите «Синхронизировать» ещё раз, загрузка продолжится.",
                cause) from exc
        stats["ms_client"] = dict(client.stats)

    # ── Финализация ─────────────────────────────────────────────────────────
    _set_state(org_id, stage="finalize", progress=98.0,
               detail="Пересчитываем аналитику…")
    db = SessionLocal()
    try:
        conn = db.execute(
            select(Connection).where(
                Connection.org_id == org_id, Connection.kind == "moysklad"
            )
        ).scalars().first()
        if conn is not None:
            conn.status = "active"
            conn.last_sync_at = datetime.utcnow()
        db.commit()
    finally:
        db.close()
    analytics.invalidate(org_id)
    stats.pop("stock_loaded_until", None)  # всё загружено — точка продолжения не нужна
    stats.pop("resume_fp", None)
    stats.pop("needs_full_rebuild", None)  # (stats_json перезаписывается — флаг снят)
    _set_state(
        org_id,
        state="done", stage="done", progress=100.0,
        detail="Синхронизация завершена",
        stats_json=json.dumps(stats, ensure_ascii=False),
        finished_at=datetime.utcnow(), fail_streak=0, alerted_streak=0,
    )


def _has_stock_rows(org_id: int) -> bool:
    db = SessionLocal()
    try:
        return db.execute(
            select(StockDay.product_id).where(StockDay.org_id == org_id).limit(1)
        ).first() is not None
    finally:
        db.close()


# ── Товары ───────────────────────────────────────────────────────────────────

def _parse_assortment(rows: list[dict], org_id: int = 0) -> list[dict]:
    """Строки ассортимента → атрибуты наших products.

    product: base_name = имя, size=''; variant: base_name = имя родительского
    product (или имя без финальных скобок), size = характеристика «Размер»
    либо финальные скобки имени. Цены в копейках → рубли.
    """
    parent_meta: dict[str, dict] = {}
    for row in rows:
        if (row.get("meta") or {}).get("type") == "product":
            pid = row.get("id") or _href_id((row.get("meta") or {}).get("href"))
            parent_meta[pid] = {
                "name": row.get("name") or "",
                "category": _category_of(row),
                "sale_price": _sale_price_of(row, org_id),
                "cost_price": _kopecks((row.get("buyPrice") or {}).get("value")),
                "cost_full": _cost_full_of(row, org_id),
                "archived": bool(row.get("archived")),
            }

    out: list[dict] = []
    for row in rows:
        meta_type = (row.get("meta") or {}).get("type")
        if meta_type not in ("product", "variant"):
            continue  # услуги, комплекты и серии в аналитике не участвуют
        ext_id = row.get("id") or _href_id((row.get("meta") or {}).get("href"))
        if not ext_id:
            continue
        name = row.get("name") or ""
        if meta_type == "product":
            parent = parent_meta[ext_id]
            out.append({
                "ext_id": ext_id,
                "base_name": name,
                "size": "",
                "category": parent["category"],
                "sale_price": parent["sale_price"],
                "cost_price": parent["cost_price"],
                "cost_full": parent["cost_full"],
                "archived": parent["archived"],
            })
            continue
        # variant
        parent_id = _href_id(((row.get("product") or {}).get("meta") or {}).get("href"))
        parent = parent_meta.get(parent_id)
        size = ""
        for ch in row.get("characteristics") or []:
            if str(ch.get("name") or "").strip().lower() in ("размер", "size"):
                size = str(ch.get("value") or "").strip()
                break
        if not size:
            size = parse_size_suffix(name)
        base_name = parent["name"] if parent else strip_size(name)
        sale_price = _sale_price_of(row, org_id) or (parent["sale_price"] if parent else 0.0)
        cost_price = _kopecks((row.get("buyPrice") or {}).get("value")) or (
            parent["cost_price"] if parent else 0.0
        )
        cost_full = _cost_full_of(row, org_id) or (parent["cost_full"] if parent else 0.0)
        archived = bool(row.get("archived")) or bool(parent and parent["archived"])
        out.append({
            "ext_id": ext_id,
            "base_name": base_name,
            "size": size,
            "category": parent["category"] if parent else _category_of(row),
            "sale_price": sale_price,
            "cost_price": cost_price,
            "cost_full": cost_full,
            "archived": archived,
        })
    return out


# Как понять, какая из цен МойСклада — цена продажи, а какая — себестоимость.
# Типы цен у каждого аккаунта свои, поэтому: (1) если организация выбрала тип
# в настройках — берём его по имени; (2) иначе угадываем по названию;
# (3) иначе цена продажи = первая в списке, себестоимость = не задана
# (тогда деньги считаются по закупочной цене, как раньше).
_SALE_HINTS = ("цена продажи", "розниц", "sale", "retail")
_COST_HINTS = ("себестоим", "cost")
_PRICE_TYPES: dict[int, dict] = {}  # org_id → {"sale": name, "cost": name}


def _load_price_types(org_id: int) -> None:
    """Читает выбор типов цен из настроек организации в кэш модуля."""
    db = SessionLocal()
    try:
        from app.models import Org as _Org
        org = db.get(_Org, org_id)
        data = {}
        if org is not None:
            try:
                data = json.loads(org.settings_json or "{}")
            except ValueError:
                data = {}
        set_price_types(org_id, str(data.get("price_type_sale") or ""),
                        str(data.get("price_type_cost") or ""))
    finally:
        db.close()


def set_price_types(org_id: int, sale: str = "", cost: str = "") -> None:
    """Выбор типов цен организации (вызывается синком из настроек org)."""
    _PRICE_TYPES[org_id] = {"sale": (sale or "").strip().lower(),
                            "cost": (cost or "").strip().lower()}


def _price_by(row: dict, exact: str, hints: tuple[str, ...]) -> float:
    prices = row.get("salePrices") or []
    if exact:
        for pr in prices:
            name = str(((pr or {}).get("priceType") or {}).get("name") or "").strip().lower()
            if name == exact:
                return _kopecks((pr or {}).get("value"))
        return 0.0
    for pr in prices:
        name = str(((pr or {}).get("priceType") or {}).get("name") or "").strip().lower()
        if any(h in name for h in hints):
            return _kopecks((pr or {}).get("value"))
    return 0.0


def price_type_names(rows: list[dict]) -> list[str]:
    """Все типы цен, встреченные в ассортименте — для выбора в настройках."""
    seen: dict[str, None] = {}
    for row in rows:
        for pr in row.get("salePrices") or []:
            name = str(((pr or {}).get("priceType") or {}).get("name") or "").strip()
            if name:
                seen.setdefault(name, None)
    return list(seen)


def _sale_price_of(row: dict, org_id: int = 0) -> float:
    cfg = _PRICE_TYPES.get(org_id) or {}
    val = _price_by(row, cfg.get("sale") or "", _SALE_HINTS)
    if val:
        return val
    prices = row.get("salePrices") or []
    if not prices:
        return 0.0
    return _kopecks((prices[0] or {}).get("value"))


def _cost_full_of(row: dict, org_id: int = 0) -> float:
    """Полная себестоимость из выбранного типа цены (0 = не задана)."""
    cfg = _PRICE_TYPES.get(org_id) or {}
    return _price_by(row, cfg.get("cost") or "", _COST_HINTS)


def _category_of(row: dict) -> str:
    """Категория = последний сегмент pathName группы товаров МойСклад."""
    path = row.get("pathName") or ""
    if path:
        return path.split("/")[-1].strip()
    folder = (row.get("productFolder") or {}).get("name")
    return str(folder or "").strip()


def _upsert_products(org_id: int, assortment: list[dict], stats: dict) -> dict[str, int]:
    """Создаёт/обновляет products; возвращает карту ext_id → наш product.id."""
    parsed = _parse_assortment(assortment, org_id)
    db = SessionLocal()
    try:
        existing = {
            p.ext_id: p
            for p in db.execute(
                select(Product).where(Product.org_id == org_id, Product.ext_id != "")
            ).scalars()
        }
        created = updated = 0
        renames: dict[str, set[str]] = {}  # старое base_name → новые (аудит 18.08)
        for item in parsed:
            row = existing.get(item["ext_id"])
            if row is None:
                row = Product(org_id=org_id, ext_id=item["ext_id"])
                # Авто-эвристика ТОЛЬКО при создании: упаковка/сертификаты/расходники
                # не участвуют в аналитике. Ручной выбор пользователя не перетираем.
                row.excluded = exclusions.is_service_item(item["base_name"], item["category"])
                db.add(row)
                created += 1
            else:
                updated += 1
                if row.base_name and row.base_name != item["base_name"]:
                    renames.setdefault(row.base_name, set()).add(item["base_name"])
            row.base_name = item["base_name"]
            row.size = item["size"]
            row.category = item["category"]
            row.sale_price = item["sale_price"]
            row.cost_full = item.get("cost_full") or 0.0
            row.cost_price = item["cost_price"]
            row.archived = item["archived"]
        # Миграция пользовательских данных — ДО commit, в одной транзакции с
        # обновлением base_name (ревью 18.08): иначе сбой между коммитами
        # оставлял товары с новыми именами, а данные — со старыми, навсегда.
        if renames:
            _migrate_renames(db, org_id, renames, stats)
        db.commit()
        ext_to_pid = {
            ext: pid
            for ext, pid in db.execute(
                select(Product.ext_id, Product.id).where(
                    Product.org_id == org_id, Product.ext_id != ""
                )
            )
        }
    finally:
        db.close()
    stats["products_total"] = len(parsed)
    stats["products_created"] = created
    stats["products_updated"] = updated
    return ext_to_pid


def _migrate_renames(db, org_id: int, renames: dict[str, set[str]],
                     stats: dict) -> None:
    """Переносит пользовательские данные при переименовании товара в МС.

    Аудит 18.08: OrderedQty/SkuHidden/SkuDiscount/SkuCategoryOverride/
    ProductionAssign и items_json заказов ключованы строкой base_name —
    после переименования в МойСкладе всё это «осиротевало» (ручное
    «Заказано» пропадало из аналитики, скидки/архив/производства слетали).

    Мигрируем только однозначный случай: ВСЕ товары старого имени переехали
    на ОДНО новое имя и под старым не осталось ни одного товара. Разъезд по
    разным именам или частичное переименование — пропускаем (в stats).

    Вызывается ДО db.commit() вызывающего — в одной транзакции с обновлением
    base_name товаров. Сессия работает с autoflush=False, поэтому: (1) в
    начале db.flush(), чтобы SELECT'ы видели свежие имена товаров; (2) слияние
    НЕСКОЛЬКИХ старых имён в одно новое (пользователь схлопнул дубли в МС)
    агрегируется по новому имени одним проходом — раньше вторая вставка того
    же PK падала IntegrityError и валила весь синк (ревью 18.08).
    """
    from app.models import (ProductionAssign, ProductionOrder, SkuCategoryOverride,
                            SkuDiscount, SkuHidden)
    db.flush()  # pending-переименования товаров должны быть видны SELECT'ам
    migrated, skipped = [], []

    # Однозначные пары old→new; затем группировка по new (N старых → 1 новое).
    by_new: dict[str, list[str]] = {}
    for old, news in renames.items():
        if len(news) != 1:
            skipped.append(old)
            continue
        new = next(iter(news))
        left = db.execute(
            select(Product.id).where(Product.org_id == org_id,
                                     Product.base_name == old).limit(1)
        ).first()
        if left is not None:
            skipped.append(old)  # часть размеров осталась под старым именем
            continue
        by_new.setdefault(new, []).append(old)

    for new, olds in by_new.items():
        # OrderedQty: слить ВСЕ старые строки в одну новую (qty/ms_qty суммой)
        add_qty = add_ms = 0.0
        for old in olds:
            old_oq = db.get(OrderedQty, (org_id, old))
            if old_oq is not None:
                add_qty += old_oq.qty
                add_ms += old_oq.ms_qty
                db.delete(old_oq)
        if add_qty or add_ms:
            db.flush()  # удаления старых строк — до вставки новой
            new_oq = db.get(OrderedQty, (org_id, new))
            if new_oq is None:
                db.add(OrderedQty(org_id=org_id, base_name=new,
                                  qty=add_qty, ms_qty=add_ms))
            else:
                new_oq.qty += add_qty
                new_oq.ms_qty += add_ms
            db.flush()
        # Простые таблицы: под новым именем остаётся ровно одна запись —
        # существующая, либо первая из переносимых; остальные удаляются.
        for model in (SkuHidden, SkuDiscount, SkuCategoryOverride, ProductionAssign):
            taken = db.execute(select(model).where(
                model.org_id == org_id, model.base_name == new).limit(1)
            ).scalars().first() is not None
            for old in olds:
                old_rows = db.execute(select(model).where(
                    model.org_id == org_id, model.base_name == old)).scalars().all()
                for r in old_rows:
                    if not taken:
                        r.base_name = new
                        taken = True
                    else:
                        db.delete(r)
            db.flush()
        # Заказы на производство: правим items_json незакрытых заказов,
        # чтобы push-to-ms и просмотр матчились по актуальному имени
        olds_set = set(olds)
        orders = db.execute(select(ProductionOrder).where(
            ProductionOrder.org_id == org_id,
            ProductionOrder.status != "received")).scalars().all()
        for order in orders:
            try:
                items = json.loads(order.items_json or "[]")
            except ValueError:
                continue
            changed = False
            for it in items:
                if it.get("base_name") in olds_set:
                    it["base_name"] = new
                    changed = True
            if changed:
                order.items_json = json.dumps(items, ensure_ascii=False)
        migrated.extend(f"{old} → {new}" for old in olds)

    if migrated:
        stats["renames_migrated"] = migrated
        print(f"ms_sync org={org_id}: перенесены данные переименованных: {migrated}")
    if skipped:
        stats["renames_skipped"] = skipped


# ── История остатков ─────────────────────────────────────────────────────────

async def _fetch_day_stock(client: MoySkladClient, active_wh: list[Warehouse],
                           day_iso: str, ext_to_pid: dict[str, int],
                           unmatched: set[str]) -> tuple[dict[int, float], dict[int, dict[int, float]]]:
    """Остатки на конец дня: (суммарно по товару, по складам).

    Возвращает ({product_id: qty>0}, {warehouse.id: {product_id: qty}}).
    """
    per_wh_rows = await asyncio.gather(
        *[client.fetch_stock_on(day_iso, w.ext_id) for w in active_wh]
    )
    totals: dict[int, float] = {}
    by_wh: dict[int, dict[int, float]] = {}
    for warehouse, rows in zip(active_wh, per_wh_rows):
        wh_map = by_wh.setdefault(warehouse.id, {})
        for row in rows:
            ext = _href_id((row.get("meta") or {}).get("href"))
            pid = ext_to_pid.get(ext)
            if pid is None:
                if ext:
                    unmatched.add(ext)
                continue
            qty = float(row.get("stock") or 0)
            if not qty:
                continue
            totals[pid] = totals.get(pid, 0.0) + qty
            wh_map[pid] = wh_map.get(pid, 0.0) + qty
    return totals, by_wh


async def _sync_stock_history(org_id: int, client: MoySkladClient,
                              active_wh: list[Warehouse], dates: list[str],
                              ext_to_pid: dict[str, int], stats: dict,
                              initial: bool, resume_from: str | None = None) -> None:
    """stock_days по датам + warehouse_stock на последнюю дату.

    Явные нули — правило из legacy sync.py: позиция с прошлым остатком >0,
    отсутствующая в отчёте текущей даты, получает qty=0.

    Инцидент 21.08: раньше ВСЕ даты качались в память и только потом писались.
    Три подряд падения на ~110-й дате из 365 (429 от МойСклада) каждый раз
    теряли 4 минуты загрузки целиком. Теперь даты идут хронологическими чанками
    по STOCK_CHUNK_DATES: чанк скачан (параллельно, под лимитером) → сразу
    явные нули + запись. Семантика аудита 18.08 сохранена: старая история
    стирается в первом _flush первого чанка, т.е. только после первой удачной
    загрузки. СЛЕДСТВИЕ: сбой на середине в initial-режиме оставляет ЧАСТИЧНУЮ
    новую историю (от начала окна до последнего записанного чанка). Смягчение:
    при любом исключении в stats фиксируется stock_loaded_until = последняя
    полностью записанная дата; _run_sync превращает это в понятное сообщение,
    а следующий запуск (resume_from) продолжает с соседней даты без wipe.
    """
    unmatched: set[str] = set()
    written = zeroed = 0

    db = SessionLocal()
    try:
        if initial and not resume_from:
            # Полная пересборка: prev с нуля. Старая история НЕ стирается здесь
            # (аудит 18.08): раньше delete+commit шли ДО многоминутной загрузки
            # из МойСклада, и любой сбой (таймаут, 401, 429) оставлял клиента
            # вовсе без истории. Теперь чистка отложена в первый _flush первого
            # чанка, когда его данные уже скачаны и лежат в памяти.
            prev_positive: set[int] = set()
        else:
            # Инкремент / продолжение: prev = последняя дата ДО первой из окна.
            first_day = dates[0]
            if resume_from:
                # Защита от дублей: хвост за точкой продолжения (частичный
                # батч чанка, перекачиваемое «сегодня») перезаписывается.
                db.execute(delete(StockDay).where(
                    StockDay.org_id == org_id, StockDay.date >= first_day))
                db.commit()
            prev_date = db.execute(
                select(StockDay.date)
                .where(StockDay.org_id == org_id, StockDay.date < first_day)
                .order_by(StockDay.date.desc())
                .limit(1)
            ).scalar()
            prev_positive = set()
            if prev_date:
                prev_positive = {
                    pid
                    for pid, in db.execute(
                        select(StockDay.product_id).where(
                            StockDay.org_id == org_id,
                            StockDay.date == prev_date,
                            StockDay.qty > 0,
                        )
                    )
                }
    finally:
        db.close()

    total_dates = len(dates)

    # ── Фаза 1 (внутри чанка): ПАРАЛЛЕЛЬНАЯ загрузка дат чанка ───────────────
    # Даты качаются параллельно (все пары дата×склад чанка отдаются пулу сразу),
    # темп ограничивает RateLimiter клиента (~12–15 rps, тяжёлые отчёты — через
    # узкий семафор). Явные нули требуют последовательности prev→next, поэтому
    # расчёт нулей и запись идут по готовым данным чанка хронологически.
    async def _one_day(day_iso: str):
        res = await _fetch_day_stock(client, active_wh, day_iso, ext_to_pid, unmatched)
        return day_iso, res

    async def _fetch_chunk(chunk: list[str]) -> dict:
        tasks = [asyncio.ensure_future(_one_day(d)) for d in chunk]
        try:
            return dict(await asyncio.gather(*tasks))
        except BaseException:
            # Один запрос исчерпал ретраи — соседей гасим, а не даём им
            # дальше молотить уже закрытый лимит (инцидент 21.08).
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    # ── Фаза 2 (внутри чанка): явные нули + запись (хронологически) ──────────
    last_by_wh: dict[int, dict[int, float]] = {}
    batch: list[dict] = []
    batch_days: list[str] = []
    need_wipe = initial and not resume_from  # чистка старой истории — в одной
    # транзакции с первой вставкой новой (первый чанк уже успешно скачан)

    def _flush() -> None:
        nonlocal batch, batch_days, need_wipe
        if not batch and (initial or not batch_days) and not need_wipe:
            # Ревью 21.08: при need_wipe пустой батч НЕ пропускаем — первый чанк
            # скачан успешно (инвариант 18.08 соблюдён), а без чистки старые
            # строки пережили бы запуск и отравили бы точку продолжения.
            batch_days = []
            return
        db = SessionLocal()
        try:
            if need_wipe:
                db.execute(delete(StockDay).where(StockDay.org_id == org_id))
                # Старая история реально стёрта — прежняя точка продолжения и
                # флаг пересборки больше не актуальны (новый отпечаток ниже).
                stats.pop("stock_loaded_until", None)
                stats.pop("needs_full_rebuild", None)
            if not initial and batch_days:
                db.execute(delete(StockDay).where(
                    StockDay.org_id == org_id, StockDay.date.in_(batch_days)
                ))
            if batch:
                db.execute(insert(StockDay), batch)
            db.commit()
            need_wipe = False
        finally:
            db.close()
        batch = []
        batch_days = []

    done_count = 0
    # При исключении stock_loaded_until уже указывает на последний целиком
    # записанный чанк (или отсутствует) — _run_sync сохранит его в stats.
    for start in range(0, total_dates, STOCK_CHUNK_DATES):
        chunk = dates[start:start + STOCK_CHUNK_DATES]  # хронологически
        hits_429_before = client.stats.get("429", 0)
        day_results = await _fetch_chunk(chunk)
        for day_iso in chunk:
            totals, by_wh = day_results[day_iso]
            for pid, qty in totals.items():
                batch.append({"org_id": org_id, "product_id": pid,
                              "date": day_iso, "qty": qty})
            # Явный ноль для распроданных: были >0, из отчёта исчезли.
            for gone in prev_positive - set(totals):
                batch.append({"org_id": org_id, "product_id": gone,
                              "date": day_iso, "qty": 0.0})
                zeroed += 1
            written += len(totals) + len(prev_positive - set(totals))
            batch_days.append(day_iso)
            prev_positive = {pid for pid, qty in totals.items() if qty > 0}
            last_by_wh = by_wh
            if len(batch) >= 20_000:
                _flush()
        _flush()
        if initial:
            stats["stock_loaded_until"] = chunk[-1]  # чанк записан целиком
            stats["resume_fp"] = _resume_fingerprint(active_wh)
        done_count += len(chunk)
        if initial:
            # stats_json пишем каждым чанком: если процесс убьют (деплой, OOM),
            # reset_stale_running оставит state=error с stock_loaded_until —
            # и следующий запуск продолжит, а не начнёт заново.
            progress = 8.0 + 56.0 * done_count / total_dates
            _set_state(org_id, stage="stock_history", progress=progress,
                       detail=f"История остатков: {done_count}/{total_dates} дат",
                       stats_json=json.dumps(stats, ensure_ascii=False))
        if (client.stats.get("429", 0) > hits_429_before
                and done_count < total_dates and CHUNK_PAUSE_SECONDS > 0):
            # Лимит только что закрывался — дадим ему восстановиться,
            # прежде чем выпускать следующую пачку запросов.
            await asyncio.sleep(CHUNK_PAUSE_SECONDS)
    if need_wipe:
        # Ревью 18.08: все даты всех чанков оказались пустыми (например,
        # аккаунт МС опустел) — _flush ни разу не вставлял и wipe не сработал. Загрузка
        # успешна, «пусто» — тоже результат: фиксируем его, иначе останется
        # смесь старой истории с новыми пустыми продажами/остатками.
        db = SessionLocal()
        try:
            db.execute(delete(StockDay).where(StockDay.org_id == org_id))
            db.commit()
            need_wipe = False
            stats.pop("needs_full_rebuild", None)
        finally:
            db.close()

    if not initial:
        _set_state(org_id, stage="stock_today", progress=40.0,
                   detail="Остатки на сегодня обновлены")

    # Текущие остатки по складам — из последней даты (сегодня).
    wh_rows = [
        {"org_id": org_id, "product_id": pid, "warehouse_id": wh_id, "qty": qty}
        for wh_id, wh_map in last_by_wh.items()
        for pid, qty in wh_map.items()
        if qty
    ]
    db = SessionLocal()
    try:
        db.execute(delete(WarehouseStock).where(WarehouseStock.org_id == org_id))
        if wh_rows:
            db.execute(insert(WarehouseStock), wh_rows)
        db.commit()
    finally:
        db.close()

    stats["stock_dates"] = total_dates
    stats["stock_rows"] = written
    stats["stock_zeroed"] = zeroed
    stats["warehouse_stock_rows"] = len(wh_rows)
    if unmatched:
        stats["stock_unmatched_skus"] = len(unmatched)


# ── «Едет к нам» из МС: заказы поставщику ────────────────────────────────────

async def _full_positions(client: MoySkladClient, entity: str, doc: dict,
                          stats: dict) -> list[dict]:
    """Полный список позиций документа. Аудит 18.08: expand=positions отдаёт
    не более ~100 вложенных строк (или вовсе одну meta-ссылку) — сверяем с
    meta.size и при неполноте дочитываем /entity/{e}/{id}/positions."""
    posmeta = doc.get("positions") or {}
    rows = posmeta.get("rows") or []
    try:
        size = int(((posmeta.get("meta") or {}).get("size")) or 0)
    except (TypeError, ValueError):
        size = 0
    if size > len(rows) and doc.get("id"):
        rows = await client.fetch_positions(entity, str(doc["id"]))
        stats["positions_refetched"] = stats.get("positions_refetched", 0) + 1
    return rows


async def _sync_incoming(org_id: int, client: MoySkladClient,
                         ext_to_pid: dict[str, int], stats: dict) -> None:
    """entity/purchaseorder → ordered_qty.ms_qty (полная пересборка вклада МС).

    «Едет» по документу = Σ по позициям (quantity − shipped): shipped растёт
    с каждой приёмкой, привязанной к заказу, поэтому принятое отпадает само.
    Учитываются только проведённые (applicable) документы за HISTORY_DAYS —
    брошенный годовалый заказ не должен вечно занижать рекомендации.

    В ms_qty входят и документы, созданные нашей кнопкой «Отправить в
    МойСклад»: их локальный вклад в qty снят при отправке (app/ms_writeback),
    двойного счёта нет.
    """
    _set_state(org_id, stage="incoming", progress=95.5,
               detail="Загружаем заказы поставщику («едет к нам»)…")
    cutoff = (date.today() - timedelta(days=HISTORY_DAYS - 1)).isoformat()
    docs = await client.fetch_purchase_orders(cutoff)

    # product_id → base_name (агрегируем «едет» по базовому имени).
    db = SessionLocal()
    try:
        base_by_pid = dict(db.execute(
            select(Product.id, Product.base_name).where(Product.org_id == org_id)
        ).all())
    finally:
        db.close()

    incoming: dict[str, float] = {}
    open_docs = 0
    unmatched: set[str] = set()
    for doc in docs:
        if doc.get("applicable") is False:  # черновик/непроведённый — не едет
            continue
        doc_qty = 0.0
        for pos in await _full_positions(client, "purchaseorder", doc, stats):
            ext = _href_id(((pos.get("assortment") or {}).get("meta") or {}).get("href"))
            pid = ext_to_pid.get(ext)
            if pid is None:
                if ext:
                    unmatched.add(ext)
                continue
            left = float(pos.get("quantity") or 0) - float(pos.get("shipped") or 0)
            if left <= 0:
                continue  # позиция принята полностью (или переполучена)
            base = base_by_pid.get(pid)
            if not base:
                continue
            incoming[base] = incoming.get(base, 0.0) + left
            doc_qty += left
        if doc_qty > 0:
            open_docs += 1

    db = SessionLocal()
    try:
        # Полная пересборка вклада МС: обнуляем и пишем свежие значения.
        db.execute(update(OrderedQty).where(
            OrderedQty.org_id == org_id, OrderedQty.ms_qty != 0
        ).values(ms_qty=0.0))
        existing = {
            row.base_name: row
            for row in db.execute(
                select(OrderedQty).where(OrderedQty.org_id == org_id)
            ).scalars()
        }
        for base, qty in incoming.items():
            row = existing.get(base)
            if row is None:
                db.add(OrderedQty(org_id=org_id, base_name=base, qty=0.0, ms_qty=qty))
            else:
                row.ms_qty = qty
        db.commit()
    finally:
        db.close()

    stats["incoming_docs"] = len(docs)
    stats["incoming_open_docs"] = open_docs
    stats["incoming_qty"] = round(sum(incoming.values()))
    if unmatched:
        stats["incoming_unmatched_skus"] = len(unmatched)
    _set_state(org_id, stage="incoming", progress=97.0,
               detail=f"«Едет к нам»: {stats['incoming_qty']} шт "
                      f"из {open_docs} заказов поставщику")


# ── Продажи ──────────────────────────────────────────────────────────────────

async def _sync_sales(org_id: int, client: MoySkladClient,
                      active_wh: list[Warehouse], days_back: int,
                      ext_to_pid: dict[str, int], stats: dict,
                      initial: bool) -> None:
    """Продажи и возвраты из документов МойСклад → таблица sales.

    Документы: retaildemand (розница) + demand (отгрузки) — продажи,
    salesreturn — возвраты. Фильтр по выбранным складам (store документа).
    Выручка позиции — после скидки: price*qty*(1-discount/100), копейки → ₽.
    Окно дат перезаписывается целиком (так legacy чинил дыры от опоздавших
    документов).
    """
    today = date.today()
    cutoff = (today - timedelta(days=days_back - 1)).isoformat()
    active_store_ids = {w.ext_id for w in active_wh}

    base_progress = 70.0 if initial else 50.0
    span = 25.0 if initial else 40.0

    agg: dict[tuple[int, str, bool], list[float]] = {}
    stats["sales_docs"] = 0
    stats["sales_docs_skipped_store"] = 0
    # Аудит 18.08: добавлен retailsalesreturn — возврат по РОЗНИЧНОЙ продаже
    # в МойСкладе отдельная сущность, и без неё розничные возвраты не
    # синхронизировались вовсе (выручка и темп завышались). Тот же баг чинили
    # в оригинале в июле.
    entities = (("retaildemand", False), ("demand", False),
                ("salesreturn", True), ("retailsalesreturn", True))
    # Три типа документов качаются ПАРАЛЛЕЛЬНО (пагинация каждого — последовательная,
    # но друг друга они не ждут) — ещё минус пара минут первого синка.
    _set_state(org_id, stage="sales", progress=base_progress,
               detail="Загружаем документы продаж (розница, отгрузки, возвраты)…")
    docs_lists = await asyncio.gather(
        *[client.fetch_documents(entity, cutoff) for entity, _ in entities]
    )
    _set_state(org_id, stage="sales", progress=base_progress + span * 0.8,
               detail="Считаем продажи по документам…")
    for (entity, is_return), docs in zip(entities, docs_lists):
        for doc in docs:
            if doc.get("applicable") is False:
                # Аудит 18.08: черновики/распроведённые НЕ продажи — зеркально
                # фильтру в _sync_incoming (раньше черновик отгрузки завышал
                # выручку, черновик возврата занижал).
                continue
            store_ext = _href_id(((doc.get("store") or {}).get("meta") or {}).get("href"))
            if store_ext not in active_store_ids:
                stats["sales_docs_skipped_store"] += 1
                continue
            day = str(doc.get("moment") or "")[:10]
            if not day or day < cutoff:
                continue
            positions = await _full_positions(client, entity, doc, stats)
            for pos in positions:
                ext = _href_id(((pos.get("assortment") or {}).get("meta") or {}).get("href"))
                pid = ext_to_pid.get(ext)
                if pid is None:
                    continue
                qty = float(pos.get("quantity") or 0)
                if qty <= 0:
                    continue
                discount = float(pos.get("discount") or 0)
                revenue = _kopecks(pos.get("price")) * qty * (1 - discount / 100.0)
                key = (pid, day, is_return)
                cur = agg.setdefault(key, [0.0, 0.0])
                cur[0] += qty
                cur[1] += revenue
            stats["sales_docs"] += 1

    rows = [
        {"org_id": org_id, "product_id": pid, "date": day, "qty": qty,
         "revenue": round(revenue, 2), "is_return": is_return}
        for (pid, day, is_return), (qty, revenue) in agg.items()
    ]
    db = SessionLocal()
    try:
        if initial:
            db.execute(delete(Sale).where(Sale.org_id == org_id))
        else:
            db.execute(delete(Sale).where(Sale.org_id == org_id, Sale.date >= cutoff))
        if rows:
            db.execute(insert(Sale), rows)
        db.commit()
    finally:
        db.close()

    stats["sales_rows"] = sum(1 for r in rows if not r["is_return"])
    stats["return_rows"] = sum(1 for r in rows if r["is_return"])
    stats["sales_window_from"] = cutoff
    _set_state(org_id, stage="sales", progress=base_progress + span,
               detail=f"Продажи записаны: {len(rows)} строк с {cutoff}",
               stats_json=json.dumps(stats, ensure_ascii=False))
