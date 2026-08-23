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
