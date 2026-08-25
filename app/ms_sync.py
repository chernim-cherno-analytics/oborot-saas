"""Синхронизация данных МойСклад → БД «Оборота».

Первичный синк (mode='initial') — ПРОГРЕССИВНЫЙ (деплой П1, 21.08): пользователь
может работать с сервисом через секунды после подключения, история догружается
фоном, полоска под шапкой показывает прогресс на всех страницах. Фазы:
  products  (0–5%)   — entity/assortment: товары и модификации, цены;
  today     (5–10%)  — остатки ТОЛЬКО на сегодня (1 дата × активные склады)
                       → warehouse_stock + stock_days(сегодня); при свежей/
                       принудительной пересборке здесь же стирается старая
                       история — в одной транзакции с первой записью новой
                       (инвариант 18.08: никакого wipe до первой удачной загрузки);
  month     (10–25%) — окно W = INITIAL_WINDOW_DAYS последних дат (хронологически,
                       с явными нулями) И продажи окна — ОДНОЙ транзакцией
                       (D-38), затем «едет к нам»; затем
                       FINALIZE-LITE: connection.status='active', last_sync_at,
                       сброс кэша аналитики, stats.coverage_days=W,
                       stats.history_loaded_from=начало окна, stats.phase='history'.
                       С этого момента все страницы работают на W днях истории;
  history   (25–98%) — остальные даты от (W_start−1) НАЗАД к (today−HISTORY_DAYS+1)
                       чанками по STOCK_CHUNK_DATES, новые → старые. Чанк: скачать
                       остатки → явные нули хронологически внутри чанка → скачать
                       продажи за даты чанка → ЗАПИСАТЬ ОБЕ ПОЛОВИНЫ ОДНОЙ
                       ТРАНЗАКЦИЕЙ (D-38: дня остатков без своих продаж в базе не
                       существует ни на секунду);
                       ГРАНИЧНАЯ ЗАПЛАТКА в той же транзакции: день D = chunk_end+1
                       (уже записан, более новый сосед) получает qty=0 для позиций,
                       которые были >0 на chunk_end и не имеют строки на D — ровно
                       то, что дал бы прямой хронологический проход;
                       затем stats.history_loaded_from=chunk_start,
                       coverage_days, months[], сброс кэша аналитики;
  finalize  (98–100%)— как раньше: done, точка продолжения снимается.

Продолжение (resume): точка — ОБА конца загруженного отрезка,
stats.history_loaded_from (самая старая дата) и stats.history_loaded_to (самая
новая), плюс stats.resume_fp (отпечаток складов/окна). Прерванный на фазе
history синк оставляет status='active' и coverage_days; любой следующий запуск
(ручной, планировщик, почасовой догон) сначала догружает ХРОНОЛОГИЧЕСКИ хвост
[history_loaded_to+1 … сегодня] (упали 18-го, продолжаем 21-го — 19-е и 20-е
обязаны появиться), при незакрытом окне (stats.window_done) добирает продажи
окна и «едет к нам», и только потом идёт назад с history_loaded_from−1.
Статус подключения никогда не понижается.

Инкрементальный синк (mode='incremental'): обновление цен из ассортимента,
живые остатки на сегодня (+ явные нули) и перезапись продаж за последние
SYNC_DAYS_BACK дней (окно — так legacy чинил дыры от опоздавших документов) —
тоже ОДНОЙ транзакцией (D-38). Упавший на документах инкремент не обновляет и
остатки: свежий остаток при вчерашних продажах — это заниженный темп, а не
«частично свежие данные». Остатки по складам (warehouse_stock) обновляются
отдельно и сразу: измерения «день» у них нет, в оборачиваемость они не входят.

Приёмы, портированные из legacy (проверены на реальном бренде):
- «явный ноль»: позиция с прошлым остатком >0, исчезнувшая из отчёта, получает
  qty=0 — иначе фронт вечно тянет последний положительный остаток, а dis тикает
  (правило самоизлечивающееся: после записи нуля позиция выпадает из prev);
- цены МойСклад — в копейках (везде /100);
- base_name/size: характеристика «Размер» модификации, иначе финальные скобки
  имени как в legacy _canon_name.

Прогресс пишется в таблицу sync_state; запуск — фоновым потоком со своим
event loop (start_sync). Планировщик — app/scheduler.py.
"""
import asyncio
import json
import logging
import os
import re
import threading
import time
from datetime import date, datetime, timedelta

from sqlalchemy import delete, func, insert, inspect, or_, select, update

from app import analytics, exclusions, logging_conf
from app.crypto import decrypt_token
from app.db import SessionLocal, engine, run_migration_step
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

# Окно загружаемой истории = окну канона оборачиваемости (D-35, 23.08.2026):
# 2 года. Аккаунты, загруженные до этого решения (365 дней), добирают второй
# год «Полной пересборкой» — автоматической дозагрузки назад у done-аккаунтов
# нет (точка продолжения снимается при финализации); до пересборки страницы
# честно подписывают фактическое окно («за N дн.»).
HISTORY_DAYS = int(os.environ.get("HISTORY_DAYS", "730"))
# «Едет к нам» НЕ следует за окном истории (ревью PR #12): заказ поставщику
# старше года — брошенный документ, а не товар в пути (shipped в проде не
# заполняется, D-26). Двухлетний cutoff молча показывал бы такие заказы как
# «едет» и занижал потребность. Окно заморожено на прежнем поведении:
# min(год, HISTORY_DAYS) — короткая история (тесты, HISTORY_DAYS=60) даёт
# прежние 60 дней, боевые 730 не расширяют окно дальше года.
INCOMING_ORDERS_DAYS = int(
    os.environ.get("INCOMING_ORDERS_DAYS", str(min(365, HISTORY_DAYS)))
)
SALES_RESYNC_DAYS = int(os.environ.get("SYNC_DAYS_BACK", "3"))
# Деплой П1: окно «быстрого старта» — столько последних дат загружается до
# finalize-lite (пользователь получает рабочие страницы), остальное — фоном.
INITIAL_WINDOW_DAYS = _env_int("INITIAL_WINDOW_DAYS", 30, minimum=1)
# Инцидент 21.08: история остатков качается и ПИШЕТСЯ чанками по N дат —
# сбой на середине года не теряет уже загруженное (см. _run_initial).
STOCK_CHUNK_DATES = _env_int("STOCK_CHUNK_DATES", 30, minimum=1)
try:
    CHUNK_PAUSE_SECONDS = max(0.0, float(os.environ.get("MS_CHUNK_PAUSE", "2")))
except ValueError:
    CHUNK_PAUSE_SECONDS = 2.0

# Ожидаемый темп с учётом лимитов МойСклад (45 req / 3 c ≈ 15 rps, берём с запасом).
EFFECTIVE_RPS = 12.0

_SIZE_SUFFIX_RE = re.compile(r"\s*\(([^)]*)\)\s*$")

log = logging.getLogger("oborot.ms_sync")

_threads: dict[int, threading.Thread] = {}
_threads_lock = threading.Lock()

# Этапы первичной загрузки в порядке выполнения (для /api/sync/progress).
STAGE_TITLES = (
    ("products", "Товары и цены"),
    ("today", "Остатки на сегодня"),
    ("month", "Продажи и остатки за последние дни"),
    ("history", "История за год"),
)


def ensure_schema(bind=None) -> None:
    """Аддитивные мини-миграции: ordered_qty.ms_qty, sync_state.fail_streak,
    products.cost_full/supplier, productions.cadence_days/stages_json/moq_units.

    Base.metadata.create_all не изменяет существующие таблицы (паттерн —
    app.ms_writeback.ensure_schema). Свежая БД получает колонки из моделей.

    Ревью 22.08 (Д4): раньше ALTER выполнялся напрямую и падал на «duplicate
    column» при одновременном старте нескольких воркеров. Теперь — через
    run_migration_step (см. app/db.py), который переживает гонку и на
    SQLite, и на Postgres.

    bind — необязательный engine (нужен тестам для «старой» схемы отдельной
    базы); по умолчанию — engine приложения.
    """
    eng = bind or engine
    insp = inspect(eng)
    if insp.has_table("ordered_qty"):
        cols = {c["name"] for c in insp.get_columns("ordered_qty")}
        if "ms_qty" not in cols:
            run_migration_step(
                "ALTER TABLE ordered_qty ADD COLUMN ms_qty FLOAT NOT NULL DEFAULT 0",
                bind=eng,
            )
        # D-28: часть «едет к нам», приехавшая по заказам самого «Оборота».
        # Нулевое значение по умолчанию честное: до первого синка с новым кодом
        # мы не знаем происхождение документов и не имеем права угадывать.
        if "ms_qty_tracked" not in cols:
            run_migration_step(
                "ALTER TABLE ordered_qty ADD COLUMN ms_qty_tracked "
                "FLOAT NOT NULL DEFAULT 0",
                bind=eng,
            )
    if insp.has_table("sync_state"):
        cols = {c["name"] for c in insp.get_columns("sync_state")}
        # Инцидент 21.08: счётчики для алерта о падающем синке.
        for col in ("fail_streak", "alerted_streak"):
            if col not in cols:
                run_migration_step(
                    f"ALTER TABLE sync_state ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0",
                    bind=eng,
                )
    if insp.has_table("products"):
        cols = {c["name"] for c in insp.get_columns("products")}
        # 21.08: полная себестоимость отдельно от закупочной (см. models.Product).
        if "cost_full" not in cols:
            run_migration_step(
                "ALTER TABLE products ADD COLUMN cost_full FLOAT NOT NULL DEFAULT 0",
                bind=eng,
            )
        if "supplier" not in cols:
            run_migration_step(
                "ALTER TABLE products ADD COLUMN supplier VARCHAR(255) NOT NULL DEFAULT ''",
                bind=eng,
            )
    if insp.has_table("production_orders"):
        cols = {c["name"] for c in insp.get_columns("production_orders")}
        # 22.08: заказ помнит канал производства (см. models.ProductionOrder).
        if "production_id" not in cols:
            run_migration_step(
                "ALTER TABLE production_orders ADD COLUMN production_id INTEGER",
                bind=eng,
            )
        # 22.08 (очередь 3): кто создал заказ.
        if "created_by" not in cols:
            run_migration_step(
                "ALTER TABLE production_orders ADD COLUMN created_by INTEGER",
                bind=eng,
            )
        # D-25 (23.08): даты переходов статуса и обратная ссылка на план.
        # Без дат реальный срок производства не измерить: у заказа был только
        # статус, а когда он его сменил — не хранилось нигде.
        for col, ddl in (
            ("sent_at", "DATETIME"),
            ("received_at", "DATETIME"),
            ("order_plan_id", "INTEGER"),
        ):
            if col not in cols:
                run_migration_step(
                    f"ALTER TABLE production_orders ADD COLUMN {col} {ddl}", bind=eng,
                )
    if insp.has_table("order_plans"):
        cols = {c["name"] for c in insp.get_columns("order_plans")}
        if "created_by" not in cols:
            run_migration_step(
                "ALTER TABLE order_plans ADD COLUMN created_by INTEGER",
                bind=eng,
            )
    if insp.has_table("productions"):
        cols = {c["name"] for c in insp.get_columns("productions")}
        if "cadence_days" not in cols:
            run_migration_step(
                "ALTER TABLE productions ADD COLUMN cadence_days INTEGER NOT NULL DEFAULT 0",
                bind=eng,
            )
        # Мастер заказа 21.08: этапы производства и минимальная партия.
        if "stages_json" not in cols:
            run_migration_step(
                "ALTER TABLE productions ADD COLUMN stages_json TEXT NOT NULL DEFAULT ''",
                bind=eng,
            )
        if "moq_units" not in cols:
            run_migration_step(
                "ALTER TABLE productions ADD COLUMN moq_units INTEGER NOT NULL DEFAULT 0",
                bind=eng,
            )


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


def _iso(d: date) -> str:
    return d.isoformat()


def _today() -> date:
    """Сегодняшняя дата — ЕДИНСТВЕННЫЙ источник «сегодня» во всём модуле.

    Ревью 21.08: продолжение прерванной загрузки надо уметь проверять на
    многодневном разрыве («упали 18-го, продолжили 21-го»), а для этого тесту
    нужен шов — подмена ms_sync._today. Прямых date.today() в модуле нет.
    """
    return date.today()


def _day_after(day_iso: str) -> str:
    return _iso(date.fromisoformat(day_iso) + timedelta(days=1))


def _dates_between(from_iso: str, to_iso: str) -> list[date]:
    """Даты [from_iso … to_iso] включительно, хронологически ([] если пусто)."""
    start, end = date.fromisoformat(from_iso), date.fromisoformat(to_iso)
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def _days_since(day_iso: str, today: date | None = None) -> int:
    """Покрытие в днях: от day_iso до сегодня включительно."""
    today = today or _today()
    return max(0, (today - date.fromisoformat(day_iso)).days + 1)


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


def _persist(org_id: int, stats: dict, **fields) -> None:
    """_set_state с сериализацией stats одной строкой (сахар)."""
    _set_state(org_id, stats_json=json.dumps(stats, ensure_ascii=False), **fields)


def _coverage_days(org_id: int) -> int:
    """Сколько дней истории сейчас на диске — ПО БАЗЕ, а не по stats.

    Ревью 21.08 (минор 8): раньше при state='done' или mode='incremental'
    возвращалось HISTORY_DAYS «на доверии». Организация, у которой первичная
    загрузка умерла на фазе products, а потом владелец нажал «Синхронизировать
    сейчас» (инкремент пишет ровно один день остатков), рапортовала «за год» —
    и таблица считала оборачиваемость по одному дню как по 365. Считаем
    честно: сколько РАЗНЫХ дат stock_days лежит у организации (в пределах окна).
    """
    db = SessionLocal()
    try:
        n = db.execute(
            select(func.count(func.distinct(StockDay.date)))
            .where(StockDay.org_id == org_id)
        ).scalar()
    finally:
        db.close()
    return int(min(HISTORY_DAYS, max(0, int(n or 0))))


def months_progress(loaded_from: str | None, running: bool,
                    today: date | None = None) -> list[dict]:
    """Полоска месяцев для UI: [{ym, state: done|running|todo}], новые первыми.

    loaded_from — самая старая загруженная дата (None — ничего не загружено).
    Месяц done, если загружен целиком (в пределах окна HISTORY_DAYS);
    running — частично загруженный или ближайший к загрузке при идущем синке.
    """
    today = today or _today()
    oldest = today - timedelta(days=HISTORY_DAYS - 1)
    out: list[dict] = []
    cur = date(today.year, today.month, 1)
    while True:
        m_end = (date(cur.year + (cur.month // 12), cur.month % 12 + 1, 1)
                 - timedelta(days=1))
        a = max(cur, oldest)
        b = min(m_end, today)
        if a > b:
            break
        if loaded_from is None:
            state = "todo"
        elif loaded_from <= _iso(a):
            state = "done"
        elif loaded_from <= _iso(b + timedelta(days=1)):
            state = "running" if running else "todo"
        else:
            state = "todo"
        out.append({"ym": f"{cur.year:04d}-{cur.month:02d}", "state": state})
        if cur <= oldest:
            break
        cur = (cur - timedelta(days=1)).replace(day=1)
    # Идущий синк: первый ещё не начатый месяц тоже «в работе» (ближайший).
    if running and loaded_from is not None and not any(m["state"] == "running" for m in out):
        for m in out:
            if m["state"] == "todo":
                m["state"] = "running"
                break
    return out


def _stages_out(state: str, stats: dict) -> list[dict]:
    """Этапы первичной загрузки с секундами и счётчиками (для панели «Подробнее»)."""
    times = stats.get("stage_times") or {}
    wh_names = stats.get("warehouses") or []
    counts_by_key = {
        "products": {"products_total": stats.get("products_total")},
        "today": {"warehouses": len(wh_names), "warehouse_names": wh_names,
                  "warehouse_stock_rows": stats.get("warehouse_stock_rows")},
        "month": {"window_days": stats.get("window_days"),
                  "sales_docs": stats.get("window_sales_docs"),
                  "sales_rows": stats.get("window_sales_rows"),
                  "return_rows": stats.get("window_return_rows"),
                  "incoming_qty": stats.get("incoming_qty")},
        "history": {"chunks_done": stats.get("history_chunks_done"),
                    "chunks_total": stats.get("history_chunks_total"),
                    "dates": stats.get("history_dates"),
                    "sales_docs": stats.get("sales_docs"),
                    "stock_rows": stats.get("stock_rows")},
    }
    out = []
    for key, title in STAGE_TITLES:
        t = times.get(key) or {}
        start, end = t.get("start"), t.get("end")
        if end is not None:
            st, seconds = "done", max(0.0, end - (start or end))
        elif start is not None and state == "running":
            st, seconds = "running", max(0.0, time.time() - start)
        else:
            st, seconds = "todo", None
        if key == "month" and stats.get("window_days") and st != "todo":
            title = f"Продажи и остатки за последние {int(stats['window_days'])} дн."
        out.append({"key": key, "title": title, "state": st,
                    "seconds": round(seconds) if seconds is not None else None,
                    "counts": {k: v for k, v in counts_by_key[key].items() if v is not None}})
    return out


def _eta_sec(state: str, stats: dict) -> int | None:
    """Оценка остатка: средние секунды на чанк истории × оставшиеся чанки."""
    if state != "running":
        return None
    done = int(stats.get("history_chunks_done") or 0)
    total = int(stats.get("history_chunks_total") or 0)
    secs = float(stats.get("history_seconds") or 0.0)
    if done < 1 or total <= done:
        return None
    return int(round(secs / done * (total - done)))


def get_status(org_id: int) -> dict:
    """GET /api/sync/status: текущее состояние синхронизации организации.

    Деплой П1: плюс phase, coverage_days, history_loaded_from, months[],
    stages[], eta_sec — из них же собирается публичный /api/sync/progress.
    """
    db = SessionLocal()
    try:
        row = db.get(SyncState, org_id)
    finally:
        db.close()
    if row is None:
        # Строки состояния нет — значит синк ни разу не запускался. Но история
        # на диске быть МОЖЕТ: демо-данные засеваются напрямую, минуя синк.
        # Возвращая здесь 0, чип свежести писал «история 0 из 365 дн.» на
        # аккаунте, где все таблицы честно считали за год. Спрашиваем базу.
        return {"state": "idle", "mode": "", "stage": "", "progress_pct": 0, "detail": "",
                "started_at": None, "finished_at": None, "stats": {}, "error": "",
                "fail_streak": 0, "alerted_streak": 0,
                "phase": "", "coverage_days": _coverage_days(org_id),
                "history_loaded_from": None,
                "months": months_progress(None, False), "stages": [], "eta_sec": None}
    stats = row.stats
    coverage = _coverage_days(org_id)
    hlf = stats.get("history_loaded_from")
    if hlf:
        loaded_from: str | None = str(hlf)
    elif coverage >= HISTORY_DAYS:
        loaded_from = _iso(_today() - timedelta(days=HISTORY_DAYS - 1))
    elif coverage > 0:
        loaded_from = _iso(_today() - timedelta(days=coverage - 1))
    else:
        loaded_from = None
    return {
        "state": row.state,
        "mode": row.mode,
        "stage": row.stage,
        "progress_pct": round(row.progress, 1),
        "detail": row.detail,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "stats": stats,
        "error": row.error,
        "fail_streak": int(row.fail_streak or 0),
        "alerted_streak": int(row.alerted_streak or 0),
        "phase": str(stats.get("phase") or ""),
        "coverage_days": coverage,
        "history_loaded_from": hlf,
        "months": months_progress(loaded_from, row.state == "running"),
        "stages": _stages_out(row.state, stats) if row.mode == "initial" else [],
        "eta_sec": _eta_sec(row.state, stats),
    }


def get_progress(org_id: int) -> dict:
    """GET /api/sync/progress (любой участник организации): без внутренних stats."""
    st = get_status(org_id)
    return {
        "state": st["state"],
        # mode нужен полоске, чтобы отличить первичную загрузку от обычного
        # инкремента в 06:00 (у инкремента фаз нет — phase пустая).
        "mode": st["mode"],
        "phase": st["phase"],
        "progress_pct": st["progress_pct"],
        "detail": st["detail"],
        "error": st["error"],
        "error_cause": str((st.get("stats") or {}).get("error_cause") or ""),
        "coverage_days": st["coverage_days"],
        "history_days": HISTORY_DAYS,
        # окно быстрого старта — полоска не должна зашивать «30 дней» в вёрстку
        "window_days": INITIAL_WINDOW_DAYS,
        "months": st["months"],
        "stages": st["stages"],
        "eta_sec": st["eta_sec"],
        "started_at": st["started_at"],
        "finished_at": st["finished_at"],
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
        if "history_loaded_from" in stats or "resume_fp" in stats:
            stats.pop("history_loaded_from", None)
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
    все 'running' в БД заведомо мёртвые. Точка продолжения (stats) при этом
    сохраняется — догоняющий джоб продолжит загрузку истории.
    """
    db = SessionLocal()
    try:
        rows = db.execute(
            select(SyncState).where(SyncState.state == "running")
        ).scalars().all()
        for row in rows:
            row.state = "error"
            row.error = ("Синхронизация прервана перезапуском сервера — "
                         "продолжим автоматически")
            row.finished_at = datetime.utcnow()
            # Ревью 21.08 (минор 11): без error_cause фронт предлагал не ту
            # кнопку («проверьте токен» вместо «продолжим сами»).
            stats = row.stats
            stats["error_cause"] = "transient"
            row.stats_json = json.dumps(stats, ensure_ascii=False)
        db.commit()
        if rows:
            log.warning("сброшено зависших состояний running: %d", len(rows))
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
# coverage_days — чтобы полоска прогресса не «обнулялась» между запусками;
# history_loaded_to — САМАЯ НОВАЯ загруженная дата (ревью 21.08, мажор 1):
# без неё продолжение через сутки теряло дни между «сегодня» прерванного
# запуска и «сегодня» продолжения; window_done — окно быстрого старта
# доехало целиком, включая продажи и «едет к нам» (мажор 2).
_CARRIED_STATS = ("history_loaded_from", "history_loaded_to", "resume_fp",
                  "needs_full_rebuild", "coverage_days", "window_done",
                  # Список типов цен МойСклада: его показывает экран настроек
                  # («какой тип цены считать полной себестоимостью»). Без
                  # переноса он жил ровно до следующего прогона синка, и
                  # выпадающий список оказывался пустым.
                  "price_types")


def _pending_resume(org_id: int) -> str | None:
    """Самая старая загруженная дата прерванной первичной загрузки
    (stats.history_loaded_from) — или None, если продолжать нечего.

    Инцидент 21.08 / деплой П1: прерванная первичная загрузка оставляет ЧАСТИЧНУЮ
    историю (от history_loaded_from до сегодня). Пока она не дозагружена, любой
    следующий запуск — initial ИЛИ incremental (кнопка «Синхронизировать сейчас»,
    планировщик, почасовой догон) — продолжает первичную загрузку назад:
    инкремент поверх короткой истории дал бы битую оборачиваемость «за год».
    Исключение — явная «Полная пересборка» (start_sync(force_full=True)):
    она всегда начинает с нуля и снимает точку продолжения (ревью 21.08).
    """
    st = get_status(org_id)
    if st.get("state") != "error":
        return None
    loaded_from = (st.get("stats") or {}).get("history_loaded_from")
    return str(loaded_from) if loaded_from else None


def needs_full_rebuild(org_id: int) -> bool:
    """Помечена ли организация на полную пересборку (clear_resume_point)."""
    return bool((get_status(org_id).get("stats") or {}).get("needs_full_rebuild"))


def has_resume_point(org_id: int) -> bool:
    """Есть ли прерванная первичная загрузка, которую надо продолжить."""
    return _pending_resume(org_id) is not None


def orgs_with_resume_point() -> list[int]:
    """org_id с прерванной первичной загрузкой (для почасового догона)."""
    db = SessionLocal()
    try:
        rows = db.execute(
            select(SyncState.org_id).where(
                SyncState.state == "error",
                SyncState.stats_json.like('%"history_loaded_from"%'),
            )
        ).all()
        return [org_id for (org_id,) in rows]
    finally:
        db.close()


def _subscription_allows_sync(org_id: int) -> bool:
    """False — организация в readonly и гейт включён (см. start_sync)."""
    from app import subscription

    if not subscription.gate_enabled():
        return True
    from app.models import Org

    db = SessionLocal()
    try:
        org = db.get(Org, org_id)
        if org is None:
            return False
        if subscription.subscription_state(org, db) == subscription.READONLY:
            log.info("синк не запущен: подписка не оплачена (org=%s)", org_id)
            return False
        return True
    except Exception:  # noqa: BLE001 — гейт не имеет права ронять синк
        log.exception("проверка подписки перед синком не удалась (org=%s)", org_id)
        return True
    finally:
        db.close()


def start_sync(org_id: int, mode: str, *, force_full: bool = False) -> bool:
    """Запускает фоновый синк (initial | incremental). False — уже идёт.

    force_full=True — настоящая полная пересборка (кнопка «Полная пересборка»,
    подсказка после смены складов): точка продолжения игнорируется и снимается.

    Гейт подписки (D-24) проверяется ЗДЕСЬ, а не на роутах, и это осознанно.
    Синк стоит нам денег и общих лимитов МойСклада, а запустить его можно не
    только кнопкой «Синхронизировать»: его дёргают повторное сохранение токена,
    выбор складов, ночной планировщик и почасовой догон. Закрывать каждую
    дверь по отдельности — гарантированно забыть одну (ревью так и нашло
    открытую: сохранение того же самого токена в Настройках запускало полный
    синк организации, которой мы уже отказали в записи). Одна проверка в точке,
    через которую проходят ВСЕ запуски, надёжнее шести проверок на входах.
    При выключенном флаге не стоит ни одного запроса к базе.
    """
    if not _subscription_allows_sync(org_id):
        return False
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
        # их только done (или реально случившийся wipe в фазе today).
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
    # У нового потока контекст ПУСТОЙ, поэтому метку организации ставим
    # здесь: иначе все записи многочасового синка ушли бы в лог без
    # указания, чьи это данные. Восстанавливать нечего — поток наш.
    logging_conf.set_org(org_id)
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
            if _is_background_history_stop(exc, org_id, stats):
                # Ревью 21.08 (минор 10): фоновая догрузка истории у РАБОТАЮЩЕГО
                # сервиса — не «падающий синк». Текст пользователю говорит
                # «продолжим автоматически», алерт «второй раз подряд» тут врёт.
                log.info("фоновая догрузка истории прервана (%s) — "
                         "серия провалов не засчитана", exc)
            else:
                _bump_fail_streak(org_id)
                # Алерт шлём отсюда (и ручные, и авто), планировщик — страховка.
                from app import notify as _notify
                _notify.send_sync_failure_alert(org_id, get_status(org_id))
        except Exception:  # noqa: BLE001 — счётчик/алерт не должны маскировать ошибку синка
            pass


def _connection_status(org_id: int) -> str:
    db = SessionLocal()
    try:
        return str(db.execute(
            select(Connection.status).where(
                Connection.org_id == org_id, Connection.kind == "moysklad")
        ).scalar() or "")
    finally:
        db.close()


def _is_background_history_stop(exc: Exception, org_id: int, stats: dict) -> bool:
    """Сбой фоновой догрузки истории при уже работающем сервисе (минор 10)."""
    return (isinstance(exc, SyncInterrupted)
            and str(stats.get("phase") or "") == "history"
            and _connection_status(org_id) == "active")


class PriceTypesGone(RuntimeError):
    """Выбранного типа цены нет ни у одного товара — чинит только владелец.

    Ревью 25.08.2026 (PR #10, discussion_r3849074704). Остановка D-40
    поднималась голым `RuntimeError`, и `error_cause()` относил её к
    `internal` — к сбоям, в которых пользователю остаётся ждать. Дальше это
    расходилось с реальностью в двух местах сразу: Telegram-алерт дописывал
    «Мы уже разбираемся» поверх текста, который зовёт владельца в Настройки, а
    полоска синка предлагала кнопку «Повторить» — повтор без смены настройки
    даёт ровно ту же ошибку.

    Отдельный класс, а не переклассификация всех `RuntimeError`: под ним в
    этом модуле ходят и настоящие внутренние сбои, и для них «мы уже
    разбираемся» — правда. Наследование от `RuntimeError` сохранено намеренно:
    `_human_error` показывает такие ошибки владельцу как есть, и текст
    остановки (`price_types_gone_message`) написан именно для этого.
    """


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
    log.exception("внутренняя ошибка синка: %r", exc)
    return "Синхронизация прервана внутренней ошибкой — мы уже смотрим."


def error_cause(exc: Exception) -> str:
    """Класс причины для подсказок: token | settings | transient | internal.

    `settings` — ошибка, которую исправляет владелец в Настройках, и никто
    другой. Отличается от `internal` тем, что ждать нечего, и от `transient`
    тем, что повтор без вмешательства даст тот же результат.
    """
    import httpx

    if isinstance(exc, SyncInterrupted):
        return exc.cause
    if isinstance(exc, PriceTypesGone):
        return "settings"
    if isinstance(exc, httpx.HTTPStatusError):
        if exc.response.status_code in (401, 403):
            return "token"
        return "transient"
    if isinstance(exc, httpx.HTTPError):
        return "transient"
    return "internal"


def _interrupted_message(exc: Exception, coverage: int) -> SyncInterrupted:
    """Текст для прерванной прогрессивной загрузки (деплой П1, п. A.6)."""
    cause = error_cause(exc)
    head = f"История загружена за {coverage} дней из {HISTORY_DAYS}"
    if cause == "token":
        # Ревью 21.08: «нажмите ещё раз» при 401/403 бессмысленно — сначала токен.
        return SyncInterrupted(
            f"{head} — МойСклад не принял токен доступа. Проверьте токен в "
            "настройках: после исправления загрузка продолжится автоматически.",
            cause)
    if _is_rate_limited(exc):
        hint = "МойСклад ограничил частоту запросов."
    elif cause == "internal":
        hint = "Внутренняя ошибка — мы уже смотрим."
    else:
        hint = _human_error(exc)
    return SyncInterrupted(
        f"{head} — продолжим автоматически в течение часа. {hint}", cause)


# ── Хронометраж этапов (панель «Подробнее») ──────────────────────────────────

def _stage_begin(stats: dict, key: str) -> None:
    stats.setdefault("stage_times", {})[key] = {"start": time.time()}


def _stage_end(stats: dict, key: str) -> None:
    t = stats.setdefault("stage_times", {}).setdefault(key, {"start": time.time()})
    t["end"] = time.time()


def _stage_skip(stats: dict, key: str) -> None:
    """Этап не нужен в этом запуске (продолжение): done с нулевой длительностью."""
    now = time.time()
    stats.setdefault("stage_times", {})[key] = {"start": now, "end": now, "skipped": True}


# ── Основной прогон ──────────────────────────────────────────────────────────

async def _run_sync(org_id: int, mode: str, resume_from: str | None = None,
                    prev_fp: str = "") -> None:
    """Полный прогон синка. resume_from — самая старая дата, до которой история
    остатков уже записана прерванным первичным синком: качаем дальше НАЗАД без
    wipe (только если prev_fp — отпечаток складов/окна той загрузки — совпадает).
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
    stats["warehouses"] = [w.name for w in active_wh]
    initial = mode == "initial"
    if initial:
        stats["phase"] = "products"
    # Аудит 18.08: инкремент раньше качал ровно 1 день остатков и 3 дня продаж
    # НЕЗАВИСИМО от того, сколько сервис простоял — простой >3 суток оставлял
    # невосполнимые дыры. Теперь окно растягивается по фактическому разрыву
    # с последнего УСПЕШНОГО синка (+1 день перекрытия), в пределах HISTORY_DAYS.
    gap_days = 0
    if not initial and conn.last_sync_at is not None:
        gap_days = max(0, (_today() - conn.last_sync_at.date()).days)
    if gap_days > 1:
        stats["gap_days"] = gap_days

    async with MoySkladClient(token) as client:
        # ── Этап 1: товары ──────────────────────────────────────────────────
        _stage_begin(stats, "products")
        _persist(org_id, stats, stage="products", progress=1.0,
                 detail="Загружаем ассортимент (товары и размеры)…")
        assortment = await client.fetch_assortment()
        # Имена контрагентов: в ассортименте у товара только ссылка на
        # поставщика, а правило распределения по производствам работает с именем.
        try:
            _SUPPLIERS[org_id] = {
                (row.get("id") or ""): str(row.get("name") or "")
                for row in await client.fetch_counterparties()
            }
        except Exception as exc:  # справочник недоступен — не роняем синк
            _SUPPLIERS[org_id] = {}
            stats["suppliers_error"] = str(exc)[:200]
        # Какие типы цен считать «ценой продажи» и «полной себестоимостью»:
        # выбор организации, иначе угадываем по названию (см. _price_by).
        _load_price_types(org_id)
        available_price_types = price_type_names(assortment)
        # Сохраняется ТОТ ЖЕ список, по которому работает замок ниже, и целиком.
        # Ревью 25.08.2026 (discussion_r3852672410): здесь стояла обрезка до
        # двадцати имён, а экран настроек показывает ровно то, что сохранено.
        # На аккаунте, где типов цен много, годная замена пропавшему типу
        # оказывалась за границей обрезки и не выбиралась в продукте вовсе:
        # синк останавливался правильно, а починить его владельцу было нечем —
        # ровно тот тупик, который D-40 обещает не допускать. Список типов цен
        # это справочник уровня аккаунта, а не данные по товарам, и полностью
        # он всё равно уже посчитан здесь же, в памяти.
        stats["price_types"] = available_price_types
        # DATA-10. Выбранного типа цены нет НИ У ОДНОГО товара — значит его
        # переименовали или удалили (либо в имени опечатка). Останавливаемся
        # ДО _upsert_products: там `row.cost_full = item.get("cost_full") or 0.0`
        # записал бы ноль поверх прежней полной себестоимости по всему
        # ассортименту, а sale_price откатился бы на первую цену в списке — то
        # есть на ЧУЖОЙ тип. Тихая порча денег хуже остановки: остановку видно,
        # подменённую цену — нет. Продажа и себестоимость падают одинаково:
        # обе публикуются одной операцией, и «одну обновим, другую оставим»
        # это ровно та несогласованная пара, из-за которой маржа считается
        # по разным прогонам.
        gone_price_types = missing_price_types(org_id, available_price_types)
        if gone_price_types:
            # Свежий список типов сохраняем ДО подъёма ошибки: его показывает
            # выпадающий список настроек (api._price_types_seen), а обработчик
            # в _thread_main перечитывает stats из базы. Без этой строки
            # владелец видел бы ошибку «тип исчез» и старый список, в котором
            # исчезнувший тип всё ещё есть, а нового нет. Переносимые ключи
            # (_CARRIED_STATS) уже лежат в stats и сохраняются вместе с ним.
            _persist(org_id, stats)
            raise PriceTypesGone(price_types_gone_message(gone_price_types,
                                                          available_price_types))
        ext_to_pid = _upsert_products(org_id, assortment, stats)
        _stage_end(stats, "products")
        _persist(org_id, stats, stage="products", progress=5.0 if initial else 8.0,
                 detail=f"Товары обновлены: {stats['products_total']} позиций")

        try:
            if initial:
                await _run_initial(org_id, client, active_wh, ext_to_pid, stats,
                                   resume_from, prev_fp)
            else:
                await _run_incremental(org_id, client, active_wh, ext_to_pid, stats,
                                       gap_days)
        except Exception as exc:  # noqa: BLE001 — фиксируем прогресс и пробрасываем
            stats["ms_client"] = dict(client.stats)
            loaded_from = stats.get("history_loaded_from")
            if not initial or not loaded_from:
                _persist(org_id, stats)
                raise
            # Часть новой истории уже записана — точка продолжения (и покрытие)
            # уже в stats; объясняем пользователю, что ничего не потеряно.
            _persist(org_id, stats)
            raise _interrupted_message(exc, _coverage_days(org_id)) from exc
        stats["ms_client"] = dict(client.stats)

    # ── Финализация ─────────────────────────────────────────────────────────
    _set_state(org_id, stage="finalize", progress=98.0,
               detail="Пересчитываем аналитику…")
    _activate_connection(org_id)
    analytics.invalidate(org_id)
    stats.pop("history_loaded_from", None)  # всё загружено — точка продолжения не нужна
    stats.pop("history_loaded_to", None)
    stats.pop("window_done", None)
    stats.pop("resume_fp", None)
    stats.pop("needs_full_rebuild", None)  # (stats_json перезаписывается — флаг снят)
    if initial:
        stats["coverage_days"] = HISTORY_DAYS
    _persist(
        org_id, stats,
        state="done", stage="done", progress=100.0,
        detail="Синхронизация завершена",
        finished_at=datetime.utcnow(), fail_streak=0, alerted_streak=0,
    )


def _activate_connection(org_id: int) -> None:
    """connection.status='active' + last_sync_at=now (идемпотентно, статус не понижается)."""
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


def _finalize_lite(org_id: int, stats: dict, loaded_from: str, active_wh: list,
                   progress: float) -> None:
    """Деплой П1: сервис открывается на частичной истории.

    Подключение становится active, кэш аналитики сбрасывается, в stats —
    покрытие и точка продолжения; sync_state остаётся running (stage=history).
    """
    _activate_connection(org_id)
    analytics.invalidate(org_id)
    stats["history_loaded_from"] = loaded_from
    stats["resume_fp"] = _resume_fingerprint(active_wh)
    stats["coverage_days"] = _days_since(loaded_from)
    stats["phase"] = "history"
    _persist(org_id, stats, stage="history", progress=progress,
             detail=f"Сервис готов: {stats['coverage_days']} дн. истории; "
                    "догружаем остальное фоном")


async def _run_initial(org_id: int, client: MoySkladClient, active_wh: list,
                       ext_to_pid: dict[str, int], stats: dict,
                       resume_from: str | None, prev_fp: str) -> None:
    """Фазы today → month → finalize-lite → history (см. docstring модуля)."""
    today = _today()
    today_iso = _iso(today)
    all_dates = [_iso(today - timedelta(days=off)) for off in range(HISTORY_DAYS - 1, -1, -1)]
    oldest = all_dates[0]
    fingerprint = _resume_fingerprint(active_wh)
    unmatched: set[str] = set()
    stats["stock_dates"] = 0
    stats["stock_rows"] = 0
    stats["stock_zeroed"] = 0

    if resume_from and resume_from < oldest:
        # Ревью 21.08 (минор 7): точка СТАРШЕ окна («упали на самом старом
        # чанке, продолжили назавтра») значит «всё окно уже загружено» —
        # раньше это уводило в полную пересборку и выбрасывало ~364 верных дня.
        # Прижимаем к началу окна: остаётся обновить сегодня и закрыть загрузку.
        resume_from = oldest
    resumed = bool(resume_from and _has_stock_rows(org_id)
                   and prev_fp == fingerprint)
    # Ревью 21.08: точка, записанная при другом наборе складов/окне, — не
    # продолжаем, а пересобираем с нуля (прежние history_loaded_from/
    # resume_fp/needs_full_rebuild живут до реального wipe в фазе today).
    activated = False
    window = min(INITIAL_WINDOW_DAYS, HISTORY_DAYS)

    # ── Фаза today: остатки на сегодня ──────────────────────────────────────
    stats["phase"] = "today"
    _stage_begin(stats, "today")
    if resumed:
        stats["resumed_from"] = resume_from
        # Точку продолжения сохраняем СРАЗУ: если и этот запуск упадёт до
        # первого чанка (429 на первом запросе, 401 после смены токена),
        # следующий всё равно продолжит, а не сделает инкремент над дырой.
        stats["history_loaded_from"] = resume_from
        stats["resume_fp"] = fingerprint
        # Ревью 21.08 (мажор 1): «сегодня» прерванного запуска могло быть
        # НЕСКОЛЬКО ДНЕЙ НАЗАД (упали 18-го, продолжаем 21-го). Догружаем весь
        # хвост [history_loaded_to+1 … сегодня] ХРОНОЛОГИЧЕСКИ — с явными
        # нулями от последней записанной даты, ровно как прямой проход; раньше
        # качался только «сегодня», а промежуточные дни терялись навсегда,
        # хотя загрузка объявляла coverage_days=HISTORY_DAYS и state=done.
        loaded_to = str(stats.get("history_loaded_to") or today_iso)
        loaded_to = min(max(loaded_to, resume_from), today_iso)
        gap_dates = [_iso(d) for d in _dates_between(_day_after(loaded_to), today_iso)]
        if not gap_dates:
            gap_dates = [today_iso]
        _persist(org_id, stats, stage="today", progress=6.0,
                 detail=("Обновляем остатки на сегодня…" if len(gap_dates) == 1
                         else f"Догружаем остатки за пропущенные "
                              f"{len(gap_dates)} дн.…"))
        day_results = await _fetch_dates(client, active_wh, gap_dates, ext_to_pid, unmatched)
        prev_positive = _positive_before(org_id, gap_dates[0])
        batch, zeroed = _rows_for_dates(org_id, gap_dates, day_results, prev_positive)
        # DATA-3: дни продолжения публикуются вместе со СВОИМИ продажами — тем
        # же правилом, что окно, кусок истории и инкремент. Раньше остатки gap
        # коммитились здесь, граница history_loaded_to уезжала на сегодня, а
        # продажи ехали отдельным вызовом в фазе month. Следствий было два, и
        # оба тихие. Падение между этими точками оставляло опубликованные дни
        # остатков без продаж, и починить это было уже нечем: следующий запуск
        # видел границу продвинутой и пропущенным такой gap не считал. А при
        # ОДНОДНЕВНОМ gap с window_done=true продажи не грузились здесь вообще
        # (heal_from оставался None) — новый день остатков попадал в базу с
        # пустым числителем штатно, без всякого сбоя.
        _persist(org_id, stats, stage="today", progress=8.0,
                 detail=(f"Загружаем продажи за {gap_dates[0]}…" if len(gap_dates) == 1
                         else f"Загружаем продажи за пропущенные "
                              f"{len(gap_dates)} дн.…"))
        sales_rows = await _collect_sales(org_id, client, active_wh, ext_to_pid,
                                          stats, initial=True, date_from=gap_dates[0],
                                          date_to=gap_dates[-1], replace_all=False)
        _write_stock_rows(org_id, gap_dates, batch, sales_rows=sales_rows,
                          sales_from=gap_dates[0], sales_to=gap_dates[-1])
        _sales_written(org_id, stats, sales_rows, gap_dates[0])
        stats["stock_zeroed"] += zeroed
        stats["history_loaded_to"] = today_iso
        stats["stock_dates"] += len(gap_dates) - 1
    else:
        stats.pop("resumed_from", None)
        _persist(org_id, stats, stage="today", progress=6.0,
                 detail="Загружаем остатки на сегодня…")
        day_results = await _fetch_dates(client, active_wh, [today_iso], ext_to_pid, unmatched)
        batch, _ = _rows_for_dates(org_id, [today_iso], day_results, set())
        # Первая удачная загрузка скачана — только теперь стираем старую историю
        # (инвариант 18.08) и ставим точку продолжения «с сегодня»: провал на
        # любой следующей фазе продолжится назад, а не инкрементом над дырой.
        _write_stock_rows(org_id, [today_iso], batch, wipe=True, wipe_sales=True)
        stats.pop("needs_full_rebuild", None)
        stats.pop("window_done", None)  # история стёрта — окно надо набрать заново
        stats["history_loaded_from"] = today_iso
        stats["history_loaded_to"] = today_iso
        stats["resume_fp"] = fingerprint
        stats["coverage_days"] = 1
    _, by_wh = day_results[today_iso]
    _write_warehouse_stock(org_id, by_wh, stats)
    stats["stock_dates"] += 1
    stats["stock_rows"] += len(batch)
    _stage_end(stats, "today")
    _persist(org_id, stats, stage="today", progress=10.0,
             detail="Остатки на сегодня обновлены")

    # ── Фаза month: окно быстрого старта ────────────────────────────────────
    if resumed:
        _stage_skip(stats, "month")
        remaining = [d for d in all_dates if d < resume_from]
        # Ревью 21.08 (мажор 2): продажи окна могли не доехать (запуск упал
        # между записью остатков окна и продажами) — точка продолжения при
        # этом уже опубликована, и раньше фаза month пропускалась НАВСЕГДА:
        # до 30 дней продаж оставались пустыми, а синк рапортовал done.
        # window_done ставится только после успешных продаж И «едет к нам».
        # DATA-3: продажи самих дней gap здесь больше не догоняются — они уже
        # опубликованы одной транзакцией с остатками этих дней в фазе today.
        # Осталась ровно одна причина ходить за продажами здесь: незакрытое
        # окно быстрого старта.
        heal_from = None
        if not stats.get("window_done"):
            # Ревью 21.08 (повторное): догон начинается от СТАРОГО начала окна
            # (resume_from), а не от окна, пересчитанного на сегодняшнюю дату.
            # Иначе при продолжении через N дней терялось ровно N дней продаж:
            # незагруженным остаётся отрезок [history_loaded_from … today],
            # а all_dates[-window] за эти дни уехал вперёд. Стоимость догона
            # ограничена: пустой window_done означает, что ни один чанк ещё не
            # отработал, то есть resume_from не старше окна на момент падения.
            heal_from = resume_from
        if heal_from:
            _persist(org_id, stats, stage="month", progress=11.0,
                     detail=f"Догружаем продажи с {heal_from}…")
            await _sync_sales(org_id, client, active_wh, ext_to_pid, stats,
                              initial=True, date_from=heal_from, date_to=today_iso,
                              replace_all=False, progress=(11.0, 20.0))
            if not stats.get("window_done"):
                await _sync_incoming(org_id, client, ext_to_pid, stats,
                                     progress=(20.0, 24.0))
                stats["window_done"] = True
        if _days_since(resume_from) >= INITIAL_WINDOW_DAYS or not remaining:
            _finalize_lite(org_id, stats, resume_from, active_wh, progress=25.0)
            activated = True
    else:
        stats["phase"] = "month"
        _stage_begin(stats, "month")
        w_start = all_dates[-window]
        month_dates = all_dates[-window:]
        stats["window_days"] = window
        _persist(org_id, stats, stage="month", progress=11.0,
                 detail=f"Загружаем остатки за последние {window} дн.…")
        if len(month_dates) > 1:
            day_results.update(await _fetch_dates(
                client, active_wh, month_dates[:-1], ext_to_pid, unmatched))
        batch, zeroed = _rows_for_dates(org_id, month_dates, day_results, set())
        _persist(org_id, stats, stage="month", progress=16.0,
                 detail=f"Загружаем продажи за последние {window} дн.…")
        # DATA-3: продажи окна СНАЧАЛА скачиваются, и только потом окно
        # публикуется целиком — остатки и продажи одной транзакцией. Раньше
        # остатки окна коммитились здесь, а продажи ехали следующим вызовом:
        # падение между ними оставляло месяц остатков при нуле продаж, что
        # читается как «товар не продаётся» и обнуляет заказ.
        sales_rows = await _collect_sales(org_id, client, active_wh, ext_to_pid,
                                          stats, initial=True, date_from=w_start,
                                          date_to=today_iso, replace_all=True,
                                          progress=(16.0, 21.0))
        _write_stock_rows(org_id, month_dates, batch,  # «сегодня» перезаписывается с нулями
                          sales_rows=sales_rows, sales_from=w_start,
                          sales_to=today_iso, sales_replace_all=True)
        _sales_written(org_id, stats, sales_rows, w_start, (16.0, 21.0))
        stats["stock_dates"] += len(month_dates) - 1
        stats["stock_rows"] += len(batch)
        stats["stock_zeroed"] += zeroed
        stats["history_loaded_from"] = w_start
        stats["coverage_days"] = window
        stats["window_sales_docs"] = stats.get("sales_docs")
        stats["window_sales_rows"] = stats.get("sales_rows")
        stats["window_return_rows"] = stats.get("return_rows")
        await _sync_incoming(org_id, client, ext_to_pid, stats, progress=(21.0, 24.0))
        # Мажор 2: окно закрыто ЦЕЛИКОМ (остатки + продажи + «едет») — только
        # теперь продолжение вправе пропустить фазу month.
        stats["window_done"] = True
        _stage_end(stats, "month")
        _finalize_lite(org_id, stats, w_start, active_wh, progress=25.0)
        activated = True
        remaining = [d for d in all_dates if d < w_start]

    # ── Фаза history: назад чанками ─────────────────────────────────────────
    stats["phase"] = "history"
    _stage_begin(stats, "history")
    chunks: list[list[str]] = []
    end = len(remaining)
    while end > 0:
        start = max(0, end - STOCK_CHUNK_DATES)
        chunks.append(remaining[start:end])  # хронологически внутри, новые чанки первыми
        end = start
    stats["history_chunks_total"] = len(chunks)
    stats["history_chunks_done"] = 0
    stats["history_dates"] = 0
    stats["history_seconds"] = 0.0
    if chunks:
        _persist(org_id, stats, stage="history", progress=25.0,
                 detail=f"Загружаем историю: 0/{len(chunks)} частей")
    for chunk in chunks:
        t0 = time.monotonic()
        hits_429_before = client.stats.get("429", 0)
        day_results = await _fetch_dates(client, active_wh, chunk, ext_to_pid, unmatched)
        batch, zeroed = _rows_for_dates(org_id, chunk, day_results, set())
        # DATA-3: сначала скачиваем ОБЕ половины куска, потом публикуем их
        # одной транзакцией. Прежний порядок (коммит остатков → десятки секунд
        # сети → коммит продаж) на всё это время расширял знаменатель «дней в
        # стоке» днями, продаж за которые ещё нет: темп занижен, «хватит на N
        # дней» завышено. Ревью 21.08 (минор 6) лечило здесь кэш аналитики —
        # это лечило симптом, числа врали и без кэша. Смерть до коммита теперь
        # оставляет кусок просто не загруженным.
        sales_rows = await _collect_sales(org_id, client, active_wh, ext_to_pid,
                                          stats, initial=True, date_from=chunk[0],
                                          date_to=chunk[-1], replace_all=False)
        _write_stock_rows(org_id, chunk, batch, boundary_next=_day_after(chunk[-1]),
                          sales_rows=sales_rows, sales_from=chunk[0],
                          sales_to=chunk[-1])
        _sales_written(org_id, stats, sales_rows, chunk[0])
        stats["history_loaded_from"] = chunk[0]
        stats["coverage_days"] = _days_since(chunk[0])
        stats["history_chunks_done"] += 1
        stats["history_dates"] += len(chunk)
        stats["history_seconds"] = round(stats["history_seconds"] + (time.monotonic() - t0), 2)
        stats["stock_dates"] += len(chunk)
        stats["stock_rows"] += len(batch)
        stats["stock_zeroed"] += zeroed
        analytics.invalidate(org_id)
        if not activated and stats["coverage_days"] >= INITIAL_WINDOW_DAYS:
            # Продолжение у ещё не активированного подключения (провал до
            # finalize-lite): окно набрано — открываем сервис.
            _activate_connection(org_id)
            activated = True
        done_n, total_n = stats["history_chunks_done"], stats["history_chunks_total"]
        # stats_json пишем каждым чанком: если процесс убьют (деплой, OOM),
        # reset_stale_running оставит state=error с history_loaded_from —
        # и следующий запуск продолжит, а не начнёт заново.
        _persist(org_id, stats, stage="history",
                 progress=25.0 + 73.0 * done_n / total_n,
                 detail=f"История: {stats['coverage_days']} дн. из {HISTORY_DAYS} "
                        f"({done_n}/{total_n} частей)")
        if (client.stats.get("429", 0) > hits_429_before
                and done_n < total_n and CHUNK_PAUSE_SECONDS > 0):
            # Лимит только что закрывался — дадим ему восстановиться,
            # прежде чем выпускать следующую пачку запросов.
            await asyncio.sleep(CHUNK_PAUSE_SECONDS)
    _stage_end(stats, "history")
    if not activated:
        _activate_connection(org_id)
    if unmatched:
        stats["stock_unmatched_skus"] = len(unmatched)


async def _run_incremental(org_id: int, client: MoySkladClient, active_wh: list,
                           ext_to_pid: dict[str, int], stats: dict, gap_days: int) -> None:
    """Инкремент: остатки за окно разрыва (обычно сегодня), продажи за SYNC_DAYS_BACK."""
    today = _today()
    history_days = min(HISTORY_DAYS, max(1, gap_days + 1))
    sales_days = min(HISTORY_DAYS, max(SALES_RESYNC_DAYS, gap_days + 1))
    dates = [_iso(today - timedelta(days=off)) for off in range(history_days - 1, -1, -1)]
    unmatched: set[str] = set()
    _set_state(org_id, stage="stock_today", progress=12.0,
               detail="Обновляем остатки на сегодня…")
    day_results = await _fetch_dates(client, active_wh, dates, ext_to_pid, unmatched)
    prev_positive = _positive_before(org_id, dates[0])
    batch, zeroed = _rows_for_dates(org_id, dates, day_results, prev_positive)
    # Остатки по складам к оборачиваемости отношения не имеют (нет измерения
    # «день»), поэтому обновляются сразу и от продаж не зависят.
    _, by_wh = day_results[dates[-1]]
    _write_warehouse_stock(org_id, by_wh, stats)
    _set_state(org_id, stage="stock_today", progress=40.0,
               detail="Остатки на сегодня обновлены")
    # DATA-3: тот же инвариант, что и в первичной загрузке, — новый день
    # остатков и продажи за него публикуются одной транзакцией. Иначе каждый
    # час между записью остатков и приездом документов «дней в стоке» на день
    # больше, чем дней с продажами.
    sales_from = _iso(today - timedelta(days=sales_days - 1))
    sales_rows = await _collect_sales(org_id, client, active_wh, ext_to_pid, stats,
                                      initial=False, date_from=sales_from,
                                      date_to=None, replace_all=False,
                                      progress=(50.0, 90.0))
    _write_stock_rows(org_id, dates, batch, sales_rows=sales_rows,
                      sales_from=sales_from, sales_to=None)
    _sales_written(org_id, stats, sales_rows, sales_from, (50.0, 90.0))
    stats["stock_dates"] = len(dates)
    stats["stock_rows"] = len(batch)
    stats["stock_zeroed"] = zeroed
    if unmatched:
        stats["stock_unmatched_skus"] = len(unmatched)
    await _sync_incoming(org_id, client, ext_to_pid, stats, progress=(95.5, 97.0))


def _has_stock_rows(org_id: int) -> bool:
    db = SessionLocal()
    try:
        return db.execute(
            select(StockDay.product_id).where(StockDay.org_id == org_id).limit(1)
        ).first() is not None
    finally:
        db.close()


# ── Товары ───────────────────────────────────────────────────────────────────

# Строки ассортимента, которые доезжают до products. Список ОДИН на весь
# модуль, и это не косметика: по нему же считаются типы цен (price_type_names),
# а на них стоит замок DATA-10. Пока список был литералом в одном месте и
# отсутствовал в другом, критерий «выбранного типа нет ни у одного ТОВАРА»
# (D-40) проверялся на строках, которых в products нет вовсе: тип, оставшийся
# только у услуги, засчитывался как присутствующий, синк проходил, и у
# импортируемого товара цена продажи откатывалась на чужой тип, а cost_full
# записывался нулём. Два места разъехаться больше не могут.
_IMPORTED_TYPES = ("product", "variant")


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
                "supplier": _supplier_of(row, org_id),
                "archived": bool(row.get("archived")),
            }

    out: list[dict] = []
    for row in rows:
        meta_type = (row.get("meta") or {}).get("type")
        if meta_type not in _IMPORTED_TYPES:
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
                "supplier": parent["supplier"],
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
        supplier = _supplier_of(row, org_id) or (parent["supplier"] if parent else "")
        archived = bool(row.get("archived")) or bool(parent and parent["archived"])
        out.append({
            "ext_id": ext_id,
            "base_name": base_name,
            "size": size,
            "category": parent["category"] if parent else _category_of(row),
            "sale_price": sale_price,
            "cost_price": cost_price,
            "cost_full": cost_full,
            "supplier": supplier,
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
_SUPPLIERS: dict[int, dict] = {}    # org_id → {counterparty_id: name}


def _supplier_of(row: dict, org_id: int) -> str:
    """Имя поставщика товара (контрагент в карточке МойСклада)."""
    href = ((row.get("supplier") or {}).get("meta") or {}).get("href")
    if not href:
        return ""
    return (_SUPPLIERS.get(org_id) or {}).get(_href_id(href), "")


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
    """Выбор типов цен организации (вызывается синком из настроек org).

    Сравнение имён идёт в нижнем регистре, а владельцу в сообщении об ошибке
    нужно ЕГО написание: в тексте «тип «полная себестоимость» исчез» человек
    не узнаёт свою настройку. Поэтому храним обе формы.
    """
    _PRICE_TYPES[org_id] = {"sale": (sale or "").strip().lower(),
                            "cost": (cost or "").strip().lower(),
                            "sale_raw": (sale or "").strip(),
                            "cost_raw": (cost or "").strip()}


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
    """Типы цен ИМПОРТИРУЕМЫХ строк ассортимента (_IMPORTED_TYPES).

    Два потребителя, и обоим нужен один и тот же список.

    Замок DATA-10 (D-40) спрашивает: «есть ли выбранный тип хоть у одного
    ТОВАРА». Услуги, комплекты и серии товарами не являются — `_parse_assortment`
    их не импортирует, в products их нет. Их типы цен были бы ложным
    свидетельством: выбранный тип, оставшийся только у услуги, у товара
    отсутствует, и молчание замка заканчивалось бы ровно той тихой порчей
    денег, ради которой он написан (чужая цена продажи и cost_full = 0).

    Выпадающий список настроек берёт этот же список (stats["price_types"] →
    api._price_types_seen). Иначе интерфейс предлагал бы к выбору тип, который
    синк тут же отвергает, — а расхождение показанного и посчитанного само по
    себе стоп-условие. Сохранённый выбор при этом не теряется: шаблон
    дорисовывает его отдельной строкой «(нет в ассортименте)».
    """
    seen: dict[str, None] = {}
    for row in rows:
        if (row.get("meta") or {}).get("type") not in _IMPORTED_TYPES:
            continue
        for pr in row.get("salePrices") or []:
            name = str(((pr or {}).get("priceType") or {}).get("name") or "").strip()
            if name:
                seen.setdefault(name, None)
    return list(seen)


# Роли типов цен в порядке, в котором они называются владельцу.
_PRICE_ROLES = (("sale", "цена продажи"), ("cost", "полная себестоимость"))


def missing_price_types(org_id: int, available: list[str]) -> list[tuple[str, str]]:
    """Явно выбранные типы цен, которых нет НИ У ОДНОГО товара: [(имя, роль)].

    available — ПОЛНЫЙ список типов импортируемых товаров (price_type_names),
    не обрезанный для stats: выбранный тип, не попавший в первую двадцатку, —
    не пропавший тип.
    Пустая настройка сюда не попадает: за неё отвечают эвристики (_price_by),
    и они работают как раньше. Отсутствие типа у ОТДЕЛЬНЫХ карточек — тоже не
    наш случай: непроставленная цена на части ассортимента это нормальное
    состояние каталога, а не авария.
    """
    cfg = _PRICE_TYPES.get(org_id) or {}
    have = {str(name).strip().lower() for name in available}
    out: list[tuple[str, str]] = []
    for key, role in _PRICE_ROLES:
        chosen = cfg.get(key) or ""
        if chosen and chosen not in have:
            out.append((cfg.get(f"{key}_raw") or chosen, role))
    return out


def price_types_gone_message(missing: list[tuple[str, str]],
                             available: list[str]) -> str:
    """Текст остановки: что пропало, почему остановились и из чего выбирать."""
    what = ", ".join(f"«{name}» ({role})" for name, role in missing)
    plural = len(missing) > 1
    shown = [str(n).strip() for n in available if str(n).strip()]
    if not shown:
        tail = "Ни одного типа цены в ассортименте МойСклада сейчас нет."
    else:
        tail = "Сейчас в ассортименте есть: " + ", ".join(shown[:12])
        tail += " и другие." if len(shown) > 12 else "."
    return (
        f"{'Выбранные типы цен' if plural else 'Выбранный тип цены'} {what} "
        f"{'больше не встречаются' if plural else 'больше не встречается'} "
        "в ассортименте МойСклада — тип переименован или удалён. Цены товаров "
        "оставлены прежними: пересчёт по чужому типу цены исказил бы "
        "себестоимость и прибыль во всей аналитике. Выберите тип заново в "
        f"Настройках и запустите синхронизацию. {tail}"
    )


def _sale_price_of(row: dict, org_id: int = 0) -> float:
    """Цена продажи карточки (0 = выбранного типа в ней нет).

    Ревью 25.08.2026 (PR #10, discussion_r3848821144). Замок D-40 сверяет
    ГЛОБАЛЬНОЕ отсутствие типа, и это сознательная граница: непроставленная
    цена на ЧАСТИ ассортимента — нормальное состояние каталога, останавливать
    на ней синк нельзя. Но в этом нормальном состоянии откат на «первую цену в
    списке» записывал карточке ЧУЖОЙ тип: у товара, где выбранной цены продажи
    нет, а полная себестоимость есть, первой в списке оказывалась именно она.
    Замок при этом молчал законно, синк рапортовал «завершена» — то есть дыра
    была уже исходной, но тише.

    Поэтому откат действует ТОЛЬКО там, где выбора нет. Явный выбор означает,
    что подставлять вместо него нечего: отсутствие представляется нулём, и
    ноль здесь честнее подмены — ноль видно, чужую цену нет.

    Наследование модификацией цены родителя (`_parse_assortment`) правилом не
    затрагивается: родитель считается этим же выбранным типом, значит наследуется
    цена ТОГО ЖЕ типа, а не чужого.
    """
    cfg = _PRICE_TYPES.get(org_id) or {}
    exact = cfg.get("sale") or ""
    val = _price_by(row, exact, _SALE_HINTS)
    if val or exact:
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
            row.supplier = item.get("supplier") or ""
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
    from app.models import (OrderReceipt, ProductionAssign, ProductionOrder,
                            SkuCategoryOverride, SkuDiscount, SkuHidden)
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
        # OrderedQty: слить ВСЕ старые строки в одну новую (суммой по всем
        # трём величинам). ms_qty_tracked обязан переезжать вместе с ms_qty:
        # иначе после переименования позиции её «едет по заказам „Оборота“»
        # обнуляется, а «едет всего» остаётся — и инвариант tracked ≤ ms_qty
        # держится только потому, что синк тут же всё пересчитает. Между
        # этапами (или при обрыве) картина была бы неверной.
        add_qty = add_ms = add_tracked = 0.0
        for old in olds:
            old_oq = db.get(OrderedQty, (org_id, old))
            if old_oq is not None:
                add_qty += old_oq.qty
                add_ms += old_oq.ms_qty
                add_tracked += (old_oq.ms_qty_tracked or 0.0)
                db.delete(old_oq)
        if add_qty or add_ms or add_tracked:
            db.flush()  # удаления старых строк — до вставки новой
            new_oq = db.get(OrderedQty, (org_id, new))
            if new_oq is None:
                db.add(OrderedQty(org_id=org_id, base_name=new, qty=add_qty,
                                  ms_qty=add_ms, ms_qty_tracked=add_tracked))
            else:
                new_oq.qty += add_qty
                new_oq.ms_qty += add_ms
                new_oq.ms_qty_tracked = (new_oq.ms_qty_tracked or 0.0) + add_tracked
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
        # Заказы на производство: правим items_json, чтобы push-to-ms и
        # просмотр матчились по актуальному имени.
        #
        # Раньше принятые заказы (status == "received") пропускались: их уже
        # никуда не отправишь, и имя в них никого не волновало. С появлением
        # приёмок это перестало быть правдой: приёмки переезжают на новое имя,
        # и заказ, оставшийся со старым, распадался в сверке надвое —
        # «заказано 11, принято 0» под одним именем и «заказано 0, принято 11»
        # под другим. Обе половины обязаны жить под одним именем.
        olds_set = set(olds)
        orders = db.execute(select(ProductionOrder).where(
            ProductionOrder.org_id == org_id)).scalars().all()
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
        # Приёмки (D-25) ключованы тем же base_name. Без переноса переименование
        # рвало сверку надвое: заказ показывал новое имя с «принято 0», а рядом
        # висела фантомная строка со старым именем и «заказано 0». Хуже того,
        # машинный источник считает уже записанное ПО ИМЕНИ — после
        # переименования он не находил своих строк и записывал всё
        # накопленное «отгружено» заново, удваивая принятое.
        for old in olds:
            db.execute(
                update(OrderReceipt)
                .where(OrderReceipt.org_id == org_id, OrderReceipt.base_name == old)
                .values(base_name=new)
            )
        db.flush()
        migrated.extend(f"{old} → {new}" for old in olds)

    if migrated:
        stats["renames_migrated"] = migrated
        log.info("перенесены данные переименованных позиций: %s", migrated)
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


async def _fetch_dates(client: MoySkladClient, active_wh: list[Warehouse],
                       dates: list[str], ext_to_pid: dict[str, int],
                       unmatched: set[str]) -> dict[str, tuple[dict, dict]]:
    """ПАРАЛЛЕЛЬНАЯ загрузка остатков за список дат: {date: (totals, by_wh)}.

    Все пары дата×склад отдаются пулу сразу, темп ограничивает RateLimiter
    клиента (тяжёлые отчёты — через узкий семафор). Один запрос исчерпал
    ретраи — соседей гасим, а не даём им дальше молотить уже закрытый лимит
    (инцидент 21.08).
    """
    async def _one_day(day_iso: str):
        return day_iso, await _fetch_day_stock(client, active_wh, day_iso, ext_to_pid, unmatched)

    tasks = [asyncio.ensure_future(_one_day(d)) for d in dates]
    try:
        return dict(await asyncio.gather(*tasks))
    except BaseException:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


def _positive_before(org_id: int, day_iso: str) -> set[int]:
    """Позиции с остатком >0 на последнюю записанную дату ДО day_iso (prev для нулей)."""
    db = SessionLocal()
    try:
        prev_date = db.execute(
            select(StockDay.date)
            .where(StockDay.org_id == org_id, StockDay.date < day_iso)
            .order_by(StockDay.date.desc())
            .limit(1)
        ).scalar()
        if not prev_date:
            return set()
        return {
            pid for pid, in db.execute(
                select(StockDay.product_id).where(
                    StockDay.org_id == org_id,
                    StockDay.date == prev_date,
                    StockDay.qty > 0,
                )
            )
        }
    finally:
        db.close()


def _rows_for_dates(org_id: int, dates: list[str], day_results: dict,
                    prev_positive: set[int]) -> tuple[list[dict], int]:
    """Строки stock_days за даты (хронологически) с явными нулями.

    Явные нули — правило из legacy sync.py: позиция с прошлым остатком >0,
    отсутствующая в отчёте текущей даты, получает qty=0. prev_positive —
    положительные позиции на дату перед первой (пустое множество — нулей в
    первый день не будет; для чанка истории их добавит граничная заплатка
    следующего чанка).
    """
    batch: list[dict] = []
    zeroed = 0
    for day_iso in dates:
        totals, _ = day_results[day_iso]
        for pid, qty in totals.items():
            batch.append({"org_id": org_id, "product_id": pid, "date": day_iso, "qty": qty})
        for gone in prev_positive - set(totals):
            batch.append({"org_id": org_id, "product_id": gone, "date": day_iso, "qty": 0.0})
            zeroed += 1
        prev_positive = {pid for pid, qty in totals.items() if qty > 0}
    return batch, zeroed


def _write_stock_rows(org_id: int, dates: list[str], batch: list[dict], *,
                      wipe: bool = False, wipe_sales: bool = False,
                      boundary_next: str | None = None,
                      sales_rows: list[dict] | None = None,
                      sales_from: str | None = None,
                      sales_to: str | None = None,
                      sales_replace_all: bool = False) -> None:
    """Одна транзакция: (wipe всей истории | delete дат) + insert + граничная заплатка.

    wipe=True — полная пересборка: старая история стирается здесь, когда новые
    данные уже скачаны (инвариант 18.08). Иначе даты диапазона перезаписываются
    (защита от дублей при продолжении/инкременте).

    wipe_sales=True — вместе с историей остатков стираются и продажи, В ТОЙ ЖЕ
    транзакции. Причина: оборачиваемость считается как «нетто-выручка за год /
    дни в стоке», и эти две величины берутся из РАЗНЫХ таблиц. При полной
    пересборке остатки обнулялись здесь, а продажи переписывались только на
    следующей фазе — и падение между этими точками (429, рестарт процесса, OOM)
    оставляло организацию с одним днём остатков и годом продаж. Метрика при
    этом не ломалась, а вырастала в десятки раз, и сервис продолжал считаться
    рабочим: худший вид дефекта — система не падает, а уверенно врёт.
    Теперь обе таблицы обнуляются вместе: в худшем случае человек видит нули и
    надпись «история загружается», а не выдуманные цифры.

    boundary_next — дата D = dates[-1]+1, уже записанная более новым чанком
    (деплой П1): D получает qty=0 для позиций, которые были >0 на dates[-1] и
    не имеют строки на D — ровно то, что дал бы хронологический проход.

    sales_rows (DATA-3) — продажи ЗА ТЕ ЖЕ ДНИ, уже скачанные, но ещё не
    записанные: попадают в ЭТУ ЖЕ транзакцию. Причина та же, что у wipe_sales,
    только шов ниже: раньше кусок публиковался в два приёма — остатки коммитом
    здесь, продажи отдельным коммитом десятками секунд позже. Между этими
    точками граница загруженной истории (analytics: min(StockDay.date)) уже
    уехала назад, а продаж за новые дни ещё нет: знаменатель «дней в стоке»
    шире числителя, темп занижен, «хватит на N дней» завышено. Штатно это
    видел каждый, кто смотрел на страницу во время пересборки; падение в
    этом окне (429, OOM, рестарт) оставляло такую базу до следующего прогона.
    Инвариант: в базе не существует дня остатков, продажи за который не
    загружены. Смерть до коммита оставляет кусок просто не загруженным.
    """
    db = SessionLocal()
    try:
        if wipe:
            db.execute(delete(StockDay).where(StockDay.org_id == org_id))
            if wipe_sales:
                db.execute(delete(Sale).where(Sale.org_id == org_id))
        elif dates:
            db.execute(delete(StockDay).where(
                StockDay.org_id == org_id,
                StockDay.date >= dates[0], StockDay.date <= dates[-1]))
        if batch:
            db.execute(insert(StockDay), batch)
        if boundary_next and dates:
            last = dates[-1]
            positive_last = {r["product_id"] for r in batch
                             if r["date"] == last and r["qty"] > 0}
            if positive_last:
                present = {
                    pid for pid, in db.execute(
                        select(StockDay.product_id).where(
                            StockDay.org_id == org_id, StockDay.date == boundary_next))
                }
                patch = [{"org_id": org_id, "product_id": pid, "date": boundary_next, "qty": 0.0}
                         for pid in positive_last - present]
                if patch:
                    db.execute(insert(StockDay), patch)
        if sales_rows is not None:
            _apply_sales(db, org_id, sales_rows, cutoff=sales_from or (dates[0] if dates else ""),
                         date_to=sales_to, replace_all=sales_replace_all)
        db.commit()
    finally:
        db.close()


def _write_warehouse_stock(org_id: int, by_wh: dict[int, dict[int, float]],
                           stats: dict) -> None:
    """Текущие остатки по складам — из сегодняшнего отчёта (полная перезапись)."""
    wh_rows = [
        {"org_id": org_id, "product_id": pid, "warehouse_id": wh_id, "qty": qty}
        for wh_id, wh_map in by_wh.items()
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
    stats["warehouse_stock_rows"] = len(wh_rows)


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


# Длина числа ограничена намеренно: int() в Python отказывается разбирать
# строку длиннее 4300 цифр и бросает ValueError, а этот код выполняется в цикле
# по документам МойСклада, содержимое которых пишет посторонний человек.
# Строка «[oborot#111…1]» на 4301 цифру в описании любого заказа поставщику
# роняла бы весь синк организации — ровно тот тихий отказ, из-за которого
# данные протухают неделями.
_OBOROT_MARKER_RE = re.compile(r"\[oborot#(\d{1,18})\]")


def _is_oborot_doc(doc: dict, our_docs: dict[int, str]) -> bool:
    """Документ МойСклада создан заказом «Оборота»? Только при ТРЁХ совпадениях.

    Решение владельца D-28: приёмка и заказ засчитываются «Оборотом» себе лишь
    при доказуемой связи. Одного признака мало ни одного:

      • маркер `[oborot#N]` живёт в описании, а описание человек может
        скопировать в другой документ вместе с текстом;
      • ссылка `ms_doc_href` в нашей базе может остаться от документа, который
        в МойСкладе удалили и пересоздали.

    Поэтому требуется всё сразу: маркер есть, заказ с таким id существует
    У ЭТОЙ организации (карта строится по org_id) и его сохранённая ссылка
    совпадает с href пришедшего документа.

    Совпадение SKU, количества, поставщика или даты не рассматривается вовсе.
    Ошибка в сторону «не наш» безопасна (недосчитаем своё), ошибка в сторону
    «наш» — нет (припишем себе чужое решение и испортим статистику качества
    рекомендаций именно там, где она должна быть честной).
    """
    return _oborot_order_id(doc, our_docs) is not None


def _oborot_order_id(doc: dict, our_docs: dict[int, str]) -> int | None:
    """id нашего заказа, породившего документ, или None. Условия — см. выше.

    Отдельная функция понадобилась, когда появился учёт исполнения: знать
    «документ наш» стало мало, нужен конкретный заказ, к которому писать
    строки приёмки.
    """
    m = _OBOROT_MARKER_RE.search(str(doc.get("description") or ""))
    if not m:
        return None
    href = ((doc.get("meta") or {}).get("href")) or ""
    if not href:
        return None
    order_id = int(m.group(1))
    saved = our_docs.get(order_id)
    if not saved or _href_id(saved) != _href_id(href):
        return None
    return order_id


async def _sync_incoming(org_id: int, client: MoySkladClient,
                         ext_to_pid: dict[str, int], stats: dict,
                         progress: tuple[float, float] = (95.5, 97.0)) -> None:
    """entity/purchaseorder → ordered_qty.ms_qty (полная пересборка вклада МС).

    «Едет» по документу = Σ по позициям (quantity − shipped): shipped растёт
    с каждой приёмкой, привязанной к заказу, поэтому принятое отпадает само.
    Учитываются только проведённые (applicable) документы за HISTORY_DAYS —
    брошенный годовалый заказ не должен вечно занижать рекомендации.

    В ms_qty входят и документы, созданные нашей кнопкой «Отправить в
    МойСклад»: их локальный вклад в qty снят при отправке (app/ms_writeback),
    двойного счёта нет.

    Два потока (решение владельца D-28). Здесь же документы РАЗДЕЛЯЮТСЯ на
    «oborot_tracked» (заказ создал сам «Оборот») и «external» (всё остальное:
    и заказы, размещённые до подключения, и те, что клиент завёл в МойСкладе
    сам, уже пользуясь «Оборотом»). Термин «external», а не «legacy»: дело
    в происхождении решения, а не в дате.

    Принадлежность доказывается ТРЕМЯ условиями одновременно, потому что
    любого одного мало: маркер `[oborot#N]` можно скопировать вместе с
    документом, а наша ссылка может остаться от заказа, который в МойСкладе
    пересоздали. Совпадение SKU, количества, поставщика или даты не
    учитывается вовсе — угадывать здесь запрещено, и лучше недосчитать своё,
    чем приписать себе чужое.

    В сумму «едет к нам» идут ОБА потока: товар приедет независимо от того,
    кто принял решение, и следующий заказ обязан это учитывать. Разделение
    нужно для другого вопроса — «насколько хорошо рекомендует „Оборот“».

    Здесь же считается диагностика по полю `shipped`: заполняет его сам
    МойСклад и только из приёмок, созданных «на основании» заказа. От того,
    заполняется ли оно у конкретного клиента, зависит, возможен ли
    автоматический учёт исполнения вообще (шаг 0 модели исполнения).
    """
    from app.models import ProductionOrder

    _set_state(org_id, stage="incoming", progress=progress[0],
               detail="Загружаем заказы поставщику («едет к нам»)…")
    # Окно заказов поставщику — ГОД (INCOMING_ORDERS_DAYS), не окно истории:
    # см. комментарий у константы (ревью PR #12).
    cutoff = (_today() - timedelta(days=INCOMING_ORDERS_DAYS - 1)).isoformat()
    docs = await client.fetch_purchase_orders(cutoff)

    # product_id → base_name (агрегируем «едет» по базовому имени).
    db = SessionLocal()
    try:
        base_by_pid = dict(db.execute(
            select(Product.id, Product.base_name).where(Product.org_id == org_id)
        ).all())
    finally:
        db.close()

    # Ссылки на документы МойСклада, созданные нашими заказами: id → href.
    # Нужны для встречной проверки маркера (см. _is_oborot_doc).
    db = SessionLocal()
    try:
        our_docs = {
            int(oid): href
            for oid, href in db.execute(
                select(ProductionOrder.id, ProductionOrder.ms_doc_href).where(
                    ProductionOrder.org_id == org_id,
                    ProductionOrder.ms_doc_href.is_not(None),
                )
            ).all()
            if href
        }
    finally:
        db.close()

    incoming: dict[str, float] = {}
    incoming_tracked: dict[str, float] = {}
    # (order_id, href документа) → {base_name: суммарно отгружено по документу}
    shipped_by_order: dict[tuple[int, str], dict[str, float]] = {}
    open_docs = 0
    tracked_docs = 0
    docs_with_shipped = 0
    positions_with_shipped = 0
    positions_total = 0
    unmatched: set[str] = set()
    for doc in docs:
        if doc.get("applicable") is False:  # черновик/непроведённый — не едет
            continue
        tracked_order_id = _oborot_order_id(doc, our_docs)
        tracked = tracked_order_id is not None
        doc_href = ((doc.get("meta") or {}).get("href")) or ""
        # Ключ дедупликации — нормализованный id документа, а не сырой href:
        # принадлежность документа проверяется через _href_id (query-параметры
        # отбрасываются), и сырой href в ключе рассогласовал бы одно с другим —
        # смена формы ссылки заставила бы записать всё «отгруженное» заново.
        doc_ref = _href_id(doc_href) or doc_href
        doc_qty = 0.0
        doc_shipped = False
        for pos in await _full_positions(client, "purchaseorder", doc, stats):
            positions_total += 1
            if float(pos.get("shipped") or 0) > 0:
                positions_with_shipped += 1
                doc_shipped = True
            ext = _href_id(((pos.get("assortment") or {}).get("meta") or {}).get("href"))
            pid = ext_to_pid.get(ext)
            if pid is None:
                if ext:
                    unmatched.add(ext)
                continue
            base_of_pos = base_by_pid.get(pid)
            # Исполнение (D-25): «отгружено» по НАШЕМУ заказу — машинный факт
            # приёмки. Копим по документу, записываем дельтой после цикла:
            # синк идёт каждую ночь, а таблица приёмок только пополняется.
            if tracked_order_id is not None and base_of_pos:
                shipped_qty = float(pos.get("shipped") or 0)
                # Ноль тоже кладём: если в МойСкладе приёмку исправили или
                # распровели и «отгружено» вернулось к нулю, дельта обязана
                # уйти в минус. Пропуская нули, мы бы записали приход навсегда.
                if shipped_qty >= 0:
                    key = (tracked_order_id, doc_ref)
                    bucket = shipped_by_order.setdefault(key, {})
                    bucket[base_of_pos] = bucket.get(base_of_pos, 0.0) + shipped_qty
            left = float(pos.get("quantity") or 0) - float(pos.get("shipped") or 0)
            if left <= 0:
                continue  # позиция принята полностью (или переполучена)
            base = base_of_pos
            if not base:
                continue
            incoming[base] = incoming.get(base, 0.0) + left
            if tracked:
                incoming_tracked[base] = incoming_tracked.get(base, 0.0) + left
            doc_qty += left
        if doc_shipped:
            docs_with_shipped += 1
        if doc_qty > 0:
            open_docs += 1
            if tracked:
                tracked_docs += 1

    db = SessionLocal()
    try:
        # Полная пересборка вклада МС: обнуляем и пишем свежие значения.
        db.execute(update(OrderedQty).where(
            OrderedQty.org_id == org_id,
            or_(OrderedQty.ms_qty != 0, OrderedQty.ms_qty_tracked != 0),
        ).values(ms_qty=0.0, ms_qty_tracked=0.0))
        existing = {
            row.base_name: row
            for row in db.execute(
                select(OrderedQty).where(OrderedQty.org_id == org_id)
            ).scalars()
        }
        for base, qty in incoming.items():
            tr = incoming_tracked.get(base, 0.0)
            row = existing.get(base)
            if row is None:
                db.add(OrderedQty(org_id=org_id, base_name=base, qty=0.0,
                                  ms_qty=qty, ms_qty_tracked=tr))
            else:
                row.ms_qty = qty
                row.ms_qty_tracked = tr
        db.commit()
    finally:
        db.close()

    stats["incoming_docs"] = len(docs)
    stats["incoming_open_docs"] = open_docs
    stats["incoming_qty"] = round(sum(incoming.values()))
    # Два потока: сколько «едет» по нашим заказам и сколько — по чужим (D-28).
    stats["incoming_open_docs_tracked"] = tracked_docs
    stats["incoming_qty_tracked"] = round(sum(incoming_tracked.values()))
    # Из УЖЕ ОКРУГЛЁННЫХ величин: три независимых округления дают 2 = 0 + 1
    # на дробных остатках, и тогда две цифры на экране не сходятся с третьей.
    stats["incoming_qty_external"] = (
        stats["incoming_qty"] - stats["incoming_qty_tracked"])
    # Шаг 0 модели исполнения: заполняет ли МойСклад поле «отгружено» у этого
    # клиента. Если оно почти везде нулевое при существующих приёмках, значит
    # приёмки заводят отдельными документами — и автоматический учёт исполнения
    # для этого workflow не работает в принципе. Цифра нужна, чтобы это был
    # ФАКТ, а не догадка: без неё мы бы проектировали автоматику вслепую.
    stats["incoming_positions"] = positions_total
    stats["incoming_positions_shipped"] = positions_with_shipped
    stats["incoming_docs_with_shipped"] = docs_with_shipped
    stats["receipts_added"] = _write_shipped_receipts(org_id, shipped_by_order)
    if unmatched:
        stats["incoming_unmatched_skus"] = len(unmatched)
    _set_state(org_id, stage="incoming", progress=progress[1],
               detail=f"«Едет к нам»: {stats['incoming_qty']} шт "
                      f"из {open_docs} заказов поставщику")


def _write_shipped_receipts(
    org_id: int, shipped_by_order: dict[tuple[int, str], dict[str, float]]
) -> int:
    """Пишет факты приёмки по полю «отгружено» наших заказов (D-25).

    Синк идёт каждую ночь и каждый раз видит НАКОПЛЕННОЕ значение `shipped`,
    а таблица приёмок только пополняется. Поэтому пишется ДЕЛЬТА: сколько
    отгружено сейчас минус сколько уже записано этим же источником по этому
    же документу. Пока цифра в МойСкладе не меняется, новых строк не
    появляется; выросла — появится ровно одна строка на разницу.

    Уменьшение (в МойСкладе исправили приёмку в меньшую сторону, вплоть до
    нуля) тоже записывается — компенсирующей строкой с минусом. Править
    существующую строку нельзя: это факт, а не текущее состояние.

    Чего компенсация НЕ покрывает и почему: документ сняли с проведения
    (`applicable = false`), удалили или он вышел за окно истории. В этих
    случаях позиции до нас просто не доезжают, и «отгруженное» остаётся
    записанным. Отличить «документ исчез» от «синк его не привёз» мы не можем,
    а списывать факт приёмки по молчанию источника — худший из двух вариантов.

    Ручные подтверждения этот код не трогает вовсе: у них другой источник,
    и человек с машиной не спорят за одну и ту же строку. Чтобы они не
    складывались в двойной счёт, отметка «принят целиком» для заказа,
    ушедшего в МойСклад, вообще не пишется (см. api._record_execution):
    исполнение по такому заказу принадлежит машинному источнику. Явно
    названные человеком количества всё же уважаются, и тогда по позиции
    могут оказаться два источника — поэтому выдача показывает разбивку
    `by_source`: расхождение между «сказал человек» и «прислал МойСклад»
    должно быть видно, а не спрятано в одной сумме.

    Чего мы НЕ узнаем: документ, пересозданный в МойСкладе с новой ссылкой,
    перестаёт опознаваться (`_oborot_order_id` сверяет href), и машинные
    приёмки по такому заказу просто прекращаются. Двойного счёта здесь нет —
    только тихая потеря источника; поднять её нечем, пока нет обратной
    диагностики «наша ссылка ведёт в никуда».

    Возвращает число добавленных строк.
    """
    if not shipped_by_order:
        return 0
    from app.models import OrderReceipt, ProductionOrder

    added = 0
    db = SessionLocal()
    try:
        for (order_id, doc_href), lines in shipped_by_order.items():
            order = db.get(ProductionOrder, order_id)
            if order is None or order.org_id != org_id:
                continue  # заказ удалили — приписывать некуда
            # Сравниваем по НОРМАЛИЗОВАННОЙ ссылке, а не по строке. Ключ
            # менялся (сначала писался полный href, потом — id документа), и
            # строгое сравнение не нашло бы своих же прежних строк: всё
            # накопленное «отгружено» записалось бы заново, удвоив принятое.
            # Заодно это переживает смену query-параметров и базового адреса.
            wanted = _href_id(doc_href) or doc_href
            already: dict[str, float] = {}
            for base, qty, ref in db.execute(
                select(OrderReceipt.base_name, OrderReceipt.qty,
                       OrderReceipt.source_ref).where(
                    OrderReceipt.org_id == org_id,
                    OrderReceipt.order_id == order_id,
                    OrderReceipt.source == "ms_order_shipped",
                )
            ).all():
                if (_href_id(ref or "") or ref or "") != wanted:
                    continue
                already[base] = already.get(base, 0.0) + float(qty or 0)
            now = datetime.utcnow()
            for base, total in lines.items():
                delta = round(float(total) - already.get(base, 0.0), 6)
                if delta == 0:
                    continue
                db.add(OrderReceipt(
                    org_id=org_id, order_id=order_id, base_name=base,
                    qty=delta, at=now, source="ms_order_shipped",
                    precision="by_position", source_ref=str(wanted)[:512],
                    created_by=None, created_at=now,
                ))
                added += 1
        if added:
            db.commit()
    finally:
        db.close()
    return added


# ── Продажи ──────────────────────────────────────────────────────────────────

def _apply_sales(db, org_id: int, rows: list[dict], *, cutoff: str,
                 date_to: str | None, replace_all: bool) -> None:
    """Перезапись диапазона продаж В УЖЕ ОТКРЫТОЙ транзакции, без коммита.

    Вынесено из _sync_sales, чтобы продажи куска истории можно было положить
    в ту же транзакцию, что и остатки этого куска (DATA-3, _write_stock_rows).
    """
    if replace_all:
        db.execute(delete(Sale).where(Sale.org_id == org_id))
    elif date_to:
        db.execute(delete(Sale).where(Sale.org_id == org_id, Sale.date >= cutoff,
                                      Sale.date <= date_to))
    else:
        db.execute(delete(Sale).where(Sale.org_id == org_id, Sale.date >= cutoff))
    if rows:
        db.execute(insert(Sale), rows)


def _sales_written(org_id: int, stats: dict, rows: list[dict], cutoff: str,
                   progress: tuple[float, float] | None = None) -> None:
    """Счётчики и прогресс ПОСЛЕ того, как продажи легли в базу."""
    stats["sales_rows"] += sum(1 for r in rows if not r["is_return"])
    stats["return_rows"] += sum(1 for r in rows if r["is_return"])
    stats["sales_window_from"] = cutoff
    base_progress, end_progress = progress or (0.0, 0.0)
    if end_progress - base_progress:
        _set_state(org_id, stage="sales", progress=end_progress,
                   detail=f"Продажи записаны: {len(rows)} строк с {cutoff}",
                   stats_json=json.dumps(stats, ensure_ascii=False))


async def _collect_sales(org_id: int, client: MoySkladClient,
                         active_wh: list[Warehouse], ext_to_pid: dict[str, int],
                         stats: dict, initial: bool, *, date_from: str,
                         date_to: str | None, replace_all: bool,
                         progress: tuple[float, float] | None = None) -> list[dict]:
    """Продажи и возвраты из документов МойСклад → строки для таблицы sales.

    СКАЧИВАЕТ И СВОРАЧИВАЕТ, НО НЕ ПИШЕТ. Разделение сбора и записи (DATA-3)
    нужно затем, чтобы вызывающий мог опубликовать продажи куска одной
    транзакцией с остатками того же куска и не оставить в базе день остатков,
    продажи за который не загружены.

    Документы: retaildemand (розница) + demand (отгрузки) — продажи,
    salesreturn — возвраты. Фильтр по выбранным складам (store документа).
    Выручка позиции — после скидки: price*qty*(1-discount/100), копейки → ₽.
    Диапазон [date_from, date_to] (date_to=None — по сегодня) перезаписывается
    целиком (так legacy чинил дыры от опоздавших документов); replace_all=True —
    первичная загрузка окна: таблица очищается полностью.

    Деплой П1: в initial зовётся на каждый чанк истории (диапазон чанка),
    счётчики sales_docs/sales_rows/return_rows — накопительные за запуск.
    """
    cutoff = date_from
    active_store_ids = {w.ext_id for w in active_wh}

    # progress=None — тихий режим (чанк истории): sync_state не трогаем.
    base_progress, end_progress = progress or (0.0, 0.0)
    span = end_progress - base_progress

    agg: dict[tuple[int, str, bool], list[float]] = {}
    if replace_all or not initial:
        stats["sales_docs"] = 0
        stats["sales_docs_skipped_store"] = 0
        stats["sales_rows"] = 0
        stats["return_rows"] = 0
    else:
        stats.setdefault("sales_docs", 0)
        stats.setdefault("sales_docs_skipped_store", 0)
        stats.setdefault("sales_rows", 0)
        stats.setdefault("return_rows", 0)
    # Аудит 18.08: добавлен retailsalesreturn — возврат по РОЗНИЧНОЙ продаже
    # в МойСкладе отдельная сущность, и без неё розничные возвраты не
    # синхронизировались вовсе (выручка и темп завышались). Тот же баг чинили
    # в оригинале в июле.
    entities = (("retaildemand", False), ("demand", False),
                ("salesreturn", True), ("retailsalesreturn", True))
    # Три типа документов качаются ПАРАЛЛЕЛЬНО (пагинация каждого — последовательная,
    # но друг друга они не ждут) — ещё минус пара минут первого синка.
    if span:
        _set_state(org_id, stage="sales", progress=base_progress,
                   detail="Загружаем документы продаж (розница, отгрузки, возвраты)…")
    docs_lists = await asyncio.gather(
        *[client.fetch_documents(entity, cutoff, date_to) for entity, _ in entities]
    )
    if span:
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
            if not day or day < cutoff or (date_to and day > date_to):
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

    return [
        {"org_id": org_id, "product_id": pid, "date": day, "qty": qty,
         "revenue": round(revenue, 2), "is_return": is_return}
        for (pid, day, is_return), (qty, revenue) in agg.items()
    ]


async def _sync_sales(org_id: int, client: MoySkladClient,
                      active_wh: list[Warehouse], ext_to_pid: dict[str, int],
                      stats: dict, initial: bool, *, date_from: str,
                      date_to: str | None, replace_all: bool,
                      progress: tuple[float, float] | None = None) -> None:
    """Скачать и записать продажи диапазона (сбор + собственная транзакция).

    Так продажи грузятся там, где остатков за те же дни никто не пишет:
    инкремент, догон окна после продолжения. Куски первичной истории ходят
    другим путём — через _collect_sales + _write_stock_rows (DATA-3).
    """
    rows = await _collect_sales(org_id, client, active_wh, ext_to_pid, stats,
                                initial, date_from=date_from, date_to=date_to,
                                replace_all=replace_all, progress=progress)
    db = SessionLocal()
    try:
        _apply_sales(db, org_id, rows, cutoff=date_from, date_to=date_to,
                     replace_all=replace_all)
        db.commit()
    finally:
        db.close()
    _sales_written(org_id, stats, rows, date_from, progress)
