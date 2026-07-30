"""Подключение к БД: engine, фабрика сессий, базовый класс моделей.

SQLite для демо; переезд на Postgres = смена env DATABASE_URL
(в коде нет SQLite-специфичных типов и SQL).
"""
import os

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///oborot.db")

_engine_kwargs: dict = {"future": True}
if DATABASE_URL.startswith("sqlite"):
    # FastAPI обслуживает запросы из тредпула — отключаем проверку треда.
    _engine_kwargs["connect_args"] = {"check_same_thread": False}

engine = create_engine(DATABASE_URL, **_engine_kwargs)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)


class Base(DeclarativeBase):
    """Базовый класс всех ORM-моделей."""


def init_db() -> None:
    """Создаёт таблицы (идемпотентно). Вызывается на старте приложения."""
    from app import models  # noqa: F401 — регистрирует модели в metadata

    Base.metadata.create_all(engine)


def get_db():
    """FastAPI-зависимость: сессия БД на время запроса."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
