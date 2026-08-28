"""Подключение к БД: engine, фабрика сессий, базовый класс моделей.

SQLite для демо; переезд на Postgres = смена env DATABASE_URL
(в коде нет SQLite-специфичных типов и SQL).

Здесь же — общие помощники аддитивных миграций (run_migration_step,
run_migration_once): на проде приложение поднимается несколькими воркерами
сразу, и все они выполняют одни и те же миграции на одной базе.
"""
import os
import time

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///oborot.db")

_engine_kwargs: dict = {"future": True}
if DATABASE_URL.startswith("sqlite"):
    # FastAPI обслуживает запросы из тредпула — отключаем проверку треда.
    _engine_kwargs["connect_args"] = {"check_same_thread": False}

engine = create_engine(DATABASE_URL, **_engine_kwargs)

if DATABASE_URL.startswith("sqlite"):
    from sqlalchemy import event as _event

    @_event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _rec):  # noqa: ANN001
        """WAL + ожидание блокировки. Ставится на КАЖДОЕ соединение.

        Зачем. Синхронизация с МойСкладом идёт в фоновом потоке того же
        процесса и пишет в ту же базу, из которой веб отдаёт страницы.
        В журнальном режиме по умолчанию (`journal_mode=delete`) пишущая
        транзакция блокирует базу целиком: читатель, попавший в это окно,
        получает не задержку, а сразу ошибку `database is locked`. На проде
        база — один файл SQLite, синк идёт минутами и пишет сотнями тысяч
        строк, так что окно совсем не теоретическое.

        WAL разводит читателей и писателя: чтение не блокируется записью.
        `busy_timeout` добавляет к этому 5 секунд ожидания вместо мгновенной
        ошибки в тех случаях, где столкновение всё-таки возможно (две записи).

        Чего здесь НЕТ намеренно:
          • `synchronous=NORMAL` — ускорил бы запись ценой риска потерять
            последние транзакции при отключении питания. Пока восстановление
            бэкапа ни разу не проверено, менять долговечность нельзя.
          • `foreign_keys=ON` — SQLite по умолчанию внешние ключи не проверяет,
            и включение сразу сделало бы ошибкой то, что сейчас проходит молча
            (например, неполный список таблиц при удалении организации).
            Это отдельная задача с миграцией данных, а не строчка в PRAGMA.

        Порядок двух PRAGMA важен и стоил одного упавшего теста: смена
        журнального режима берёт короткий ИСКЛЮЧИТЕЛЬНЫЙ лок на базу, и если
        `busy_timeout` ещё не выставлен, то при одновременном старте нескольких
        процессов (деплой без простоя, гонка миграций) один из них получает
        `database is locked` сразу и не поднимается вовсе. Поэтому сначала
        ожидание, потом WAL. И сам WAL — best-effort: база может быть уже в
        этом режиме (он записан в файле и переживает перезапуск), а на сетевой
        файловой системе он вообще недоступен; ронять из-за этого приложение
        нельзя — без WAL оно работает как раньше.
        """
        cur = dbapi_conn.cursor()
        try:
            cur.execute("PRAGMA busy_timeout=5000")
            try:
                cur.execute("PRAGMA journal_mode=WAL")
            except Exception:  # noqa: BLE001 — режим не критичен для работы
                pass
        finally:
            cur.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)


class Base(DeclarativeBase):
    """Базовый класс всех ORM-моделей."""


# ── Аддитивные миграции при одновременном старте нескольких процессов ────────
#
# Инцидент 22.08 (финальное ревью, Н1): при старте четырёх воркеров сразу
# 2–3 из них падали на `duplicate column name` / `DuplicateColumn`, плюс
# отдельная гонка на `CREATE TABLE IF NOT EXISTS migration_flags`
# (в Postgres — UniqueViolation на pg_type). Так бывает всегда: uvicorn
# --workers N, gunicorn без --preload, деплой без простоя (новый процесс
# поднимается, пока старый ещё жив).
#
# Правила, которые из этого следуют:
#   * ошибка «уже существует» означает «сосед сделал ту же работу» и не
#     должна ронять процесс;
#   * каждый шаг — в СВОЕЙ транзакции: в Postgres любая неудачная операция
#     отравляет транзакцию целиком, и всё, что после неё, тоже упадёт;
#   * SQLite при одновременной записи отвечает «database is locked» —
#     это лечится коротким повтором.

_ALREADY_DONE_MARKERS = (
    "duplicate column",        # SQLite: duplicate column name: moq
    "already exists",          # Postgres: column/relation ... already exists
    "duplicate key",           # Postgres: гонка CREATE TABLE (pg_type_typname_nsp_index)
    "duplicate table",
    # SQLite: два процесса одновременно проверили «флага нет» и оба пошли его
    # вставлять — второй получает UNIQUE constraint failed. Это ровно «сосед
    # уже сделал», а не ошибка: миграция под флагом на то и под флагом.
    # Маркер НАМЕРЕННО узкий, с именем таблицы: широкое «unique constraint
    # failed» глушило бы настоящие нарушения уникальности в бизнес-данных,
    # если такой запрос когда-нибудь пройдёт через этот же помощник.
    "unique constraint failed: migration_flags",
)
_BUSY_MARKERS = ("database is locked", "database table is locked", "deadlock detected")
_BUSY_RETRIES = 5
_BUSY_PAUSE_SEC = 0.2

_FLAGS_TABLE_DDL = "CREATE TABLE IF NOT EXISTS migration_flags (name VARCHAR(64) PRIMARY KEY)"


def is_already_done_error(exc: BaseException) -> bool:
    """True, если ошибка означает «это уже сделал соседний процесс»."""
    msg = str(exc).lower()
    return any(m in msg for m in _ALREADY_DONE_MARKERS)


def _is_busy_error(exc: BaseException) -> bool:
    """True, если база временно занята другим процессом и стоит повторить."""
    msg = str(exc).lower()
    return any(m in msg for m in _BUSY_MARKERS)


def run_migration_step(sql: str, params: dict | None = None, bind=None) -> bool:
    """Выполняет один шаг миграции в отдельной транзакции, переживая гонку.

    Возвращает True, если шаг выполнил именно этот процесс, и False, если
    соседний процесс уже сделал то же самое. Прочие ошибки пробрасываются.
    """
    eng = bind or engine
    for attempt in range(_BUSY_RETRIES):
        try:
            with eng.begin() as conn:
                conn.execute(text(sql), params or {})
            return True
        except SQLAlchemyError as exc:
            if is_already_done_error(exc):
                return False
            if _is_busy_error(exc) and attempt + 1 < _BUSY_RETRIES:
                time.sleep(_BUSY_PAUSE_SEC * (attempt + 1))
                continue
            raise
    return False


def ensure_migration_flags(bind=None) -> None:
    """Создаёт таблицу флагов миграций (идемпотентно и без гонок)."""
    run_migration_step(_FLAGS_TABLE_DDL, bind=bind)


def run_migration_once(flag: str, work, bind=None) -> bool:
    """Выполняет разовую миграцию под флагом ровно один раз на базу.

    work(conn) и отметка флага идут ОДНОЙ транзакцией: если флаг успел
    поставить соседний процесс, наша транзакция откатывается целиком —
    ни двойного бэкфилла, ни половины миграции. Возвращает True, если работу
    сделал этот процесс.
    """
    eng = bind or engine
    ensure_migration_flags(eng)
    for attempt in range(_BUSY_RETRIES):
        try:
            with eng.begin() as conn:
                done = conn.execute(
                    text("SELECT 1 FROM migration_flags WHERE name = :n"), {"n": flag}
                ).first()
                if done:
                    return False
                work(conn)
                conn.execute(
                    text("INSERT INTO migration_flags (name) VALUES (:n)"), {"n": flag}
                )
            return True
        except SQLAlchemyError as exc:
            if is_already_done_error(exc):
                return False           # ту же миграцию только что сделал сосед
            if _is_busy_error(exc) and attempt + 1 < _BUSY_RETRIES:
                time.sleep(_BUSY_PAUSE_SEC * (attempt + 1))
                continue
            raise
    return False


# ── OPS-5: журнал успешно применённых шагов старта ───────────────────────────
#
# Что это НЕ такое. Это не механизм «применять или пропускать миграцию»: шаги
# старта (init_db и восемь ensure_schema/reset_stale_running/log_preview)
# остаются идемпотентными и выполняются на КАЖДОМ старте как сейчас. Состояние
# схемы по-прежнему определяется интроспекцией самой схемы, а не этой таблицей,
# и ни один шаг по журналу не пропускается. Иначе журнал стал бы единственным
# источником правды о схеме — а он ведётся приложением и может отстать от базы
# (восстановление из бэкапа, ручная правка), и тогда «в журнале записано» тихо
# отменило бы настоящую миграцию.
#
# Что это такое. Машиночитаемое свидетельство «шаг с таким идентификатором и
# такой позицией на этой базе успешно завершился» — плюс замок на порядок.
# До сих пор состояние миграций боевой базы восстанавливалось только
# интроспекцией, а порядок шагов существовал лишь как порядок строк в
# `app/main.py` и ничем не проверялся.
#
# Правила журнала:
#   * объявленная пара (id, позиция) сверяется с журналом ДО шага
#     (validate_migration_step), а запись делается ТОЛЬКО после успешного
#     возврата самого шага (record_migration_step). Порядок именно такой:
#     иначе конфликт обнаруживался бы уже после того, как шаг отработал —
#     см. докстринг validate_migration_step;
#   * повторный старт — no-op: та же пара (id, позиция) ничего не меняет,
#     applied_at первой записи сохраняется;
#   * тот же id на другой позиции ИЛИ та же позиция под другим id — это
#     конфликт, и он валит старт (fail closed). Такое расхождение означает,
#     что порядок шагов переписали задним числом (AGENTS.md §1: «только новая
#     миграция сверху»), и делать вид, что «уже применено», здесь нельзя;
#   * append-only: новый шаг получает НОВЫЙ id и НОВУЮ позицию, старая строка
#     не переписывается.
#
# Портируемость. Ни одного SQLite- или Postgres-специфичного выражения:
# `CREATE TABLE IF NOT EXISTS` и `CREATE UNIQUE INDEX IF NOT EXISTS` понимают
# оба (Postgres — с 9.5), типы VARCHAR/INTEGER общие, время подставляется
# из Python, а не из `CURRENT_TIMESTAMP`/`now()` (у них разный формат и разная
# зона). ON CONFLICT/UPSERT намеренно не используется: он различается диалектами
# и, главное, замаскировал бы конфликт под «уже сделано».

_LEDGER_TABLE_DDL = (
    "CREATE TABLE IF NOT EXISTS migration_ledger ("
    "step_id VARCHAR(128) NOT NULL PRIMARY KEY, "
    "step_order INTEGER NOT NULL, "
    "applied_at VARCHAR(32) NOT NULL)"
)
_LEDGER_ORDER_INDEX_DDL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_migration_ledger_order "
    "ON migration_ledger (step_order)"
)


class MigrationLedgerConflict(RuntimeError):
    """Журнал старта противоречит объявленному порядку шагов.

    Отдельный тип, а не голый RuntimeError: это стоп-условие («миграции
    переписали задним числом»), и вызывающая сторона обязана иметь возможность
    отличить его от временной ошибки базы, не разбирая текст сообщения.
    """


def ensure_migration_ledger(bind=None) -> None:
    """Создаёт таблицу журнала и уникальный индекс позиции (идемпотентно)."""
    run_migration_step(_LEDGER_TABLE_DDL, bind=bind)
    run_migration_step(_LEDGER_ORDER_INDEX_DDL, bind=bind)


def read_migration_ledger(bind=None) -> list[tuple[str, int, str]]:
    """Возвращает журнал как список (step_id, step_order, applied_at) по позиции."""
    eng = bind or engine
    ensure_migration_ledger(eng)
    with eng.connect() as conn:
        rows = conn.execute(
            text("SELECT step_id, step_order, applied_at FROM migration_ledger "
                 "ORDER BY step_order")
        ).all()
    return [(r[0], int(r[1]), r[2]) for r in rows]


def _ledger_rows_for(conn, step_id: str, step_order: int):
    """Строки журнала, конкурирующие за этот id или эту позицию."""
    by_id = conn.execute(
        text("SELECT step_id, step_order FROM migration_ledger WHERE step_id = :i"),
        {"i": step_id},
    ).first()
    by_order = conn.execute(
        text("SELECT step_id, step_order FROM migration_ledger WHERE step_order = :o"),
        {"o": step_order},
    ).first()
    return by_id, by_order


def _check_ledger_conflict(by_id, by_order, step_id: str, step_order: int) -> bool:
    """True, если пара (id, позиция) уже записана ровно так же.

    Бросает MigrationLedgerConflict, если id занят другой позицией или позиция
    занята другим id. Возвращаемый False означает «записи ещё нет, вставляем».
    """
    if by_id is not None and int(by_id[1]) != step_order:
        raise MigrationLedgerConflict(
            f"шаг старта {step_id!r} уже записан на позиции {int(by_id[1])}, "
            f"а объявлен на позиции {step_order}: порядок миграций изменён "
            "задним числом"
        )
    if by_order is not None and by_order[0] != step_id:
        raise MigrationLedgerConflict(
            f"позиция {step_order} уже занята шагом {by_order[0]!r}, "
            f"а объявлена за {step_id!r}: порядок миграций изменён задним числом"
        )
    return by_id is not None


def validate_migration_step(step_id: str, step_order: int, bind=None) -> None:
    """Сверяет объявленную пару (id, позиция) с журналом ДО выполнения шага.

    Зачем отдельная функция, а не проверка внутри записи. Ревью жизненного
    цикла 28.08.2026 (PR #44, discussion_r3884250490) показало на
    воспроизведении: если конфликт обнаруживается только в момент ЗАПИСИ, то
    сам шаг к этому времени уже отработал. С журналом, где позицию 1 занимает
    чужой шаг, старт действительно падал — но `init_db()` успевал создать
    схему, и число таблиц в синтетической базе вырастало до 27. Для аддитивных
    шагов это безобидно, а для переставленного НЕАДДИТИВНОГО шага замок
    опаздывал бы ровно на ту операцию, ради которой он заведён.

    Поэтому порядок теперь такой: preflight (эта функция) → сам шаг → запись
    успеха. Единственное, что preflight меняет на базе без журнала, — заводит
    саму таблицу журнала и её индекс: это аддитивно и без этого проверять
    нечем.

    Ничего не возвращает: «конфликта нет» — это отсутствие исключения.
    Отметка «шаг уже был записан» здесь намеренно не используется как решение
    пропустить шаг — шаги остаются идемпотентными и выполняются всегда.
    """
    eng = bind or engine
    ensure_migration_ledger(eng)
    with eng.connect() as conn:
        by_id, by_order = _ledger_rows_for(conn, step_id, step_order)
    _check_ledger_conflict(by_id, by_order, step_id, step_order)


def record_migration_step(step_id: str, step_order: int, bind=None) -> bool:
    """Отмечает успешно завершённый шаг старта в журнале.

    Вызывается ТОЛЬКО после того, как сам шаг вернул управление без ошибки.
    Возвращает True, если строку записал этот процесс, и False, если она уже
    была (повторный старт или соседний воркер). Конфликт id↔позиция поднимает
    MigrationLedgerConflict и старт не продолжается.
    """
    eng = bind or engine
    ensure_migration_ledger(eng)
    applied_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for attempt in range(_BUSY_RETRIES):
        try:
            with eng.begin() as conn:
                by_id, by_order = _ledger_rows_for(conn, step_id, step_order)
                if _check_ledger_conflict(by_id, by_order, step_id, step_order):
                    return False           # уже записано ровно так же — no-op
                conn.execute(
                    text("INSERT INTO migration_ledger (step_id, step_order, applied_at) "
                         "VALUES (:i, :o, :a)"),
                    {"i": step_id, "o": step_order, "a": applied_at},
                )
            return True
        except MigrationLedgerConflict:
            raise
        except SQLAlchemyError as exc:
            if _is_busy_error(exc) and attempt + 1 < _BUSY_RETRIES:
                time.sleep(_BUSY_PAUSE_SEC * (attempt + 1))
                continue
            # Гонка вставки: соседний воркер записал ту же строку между нашим
            # SELECT и INSERT. Молча «уже сделано» тут отвечать нельзя — та же
            # ошибка уникальности возникает и когда сосед занял НАШУ позицию
            # ЧУЖИМ шагом. Поэтому перечитываем и судим по фактическим строкам:
            # совпало — no-op, разошлось — конфликт.
            with eng.connect() as conn:
                by_id, by_order = _ledger_rows_for(conn, step_id, step_order)
            if _check_ledger_conflict(by_id, by_order, step_id, step_order):
                return False
            raise
    return False


def init_db() -> None:
    """Создаёт таблицы и прогоняет аддитивные миграции. Вызывается на старте.

    Идемпотентно и безопасно при одновременном старте нескольких воркеров:
    create_all и миграции терпят гонку «сосед уже создал».
    """
    from app import models  # noqa: F401 — регистрирует модели в metadata

    for attempt in range(_BUSY_RETRIES):
        try:
            Base.metadata.create_all(engine)
            break
        except SQLAlchemyError as exc:
            # Гонка create_all: соседний воркер создаёт те же таблицы.
            if (is_already_done_error(exc) or _is_busy_error(exc)) and attempt + 1 < _BUSY_RETRIES:
                time.sleep(_BUSY_PAUSE_SEC * (attempt + 1))
                continue
            raise
    # Миграция условий производства раньше стояла на импорте app.api — при
    # одновременном старте воркеров процесс не поднимался вообще («Error
    # loading ASGI app»). Её место — здесь, вместе с остальными миграциями.
    models.ensure_schema()


def get_db():
    """FastAPI-зависимость: сессия БД на время запроса."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
