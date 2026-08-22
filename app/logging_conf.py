"""Конфигурация логирования и привязка записей к организации.

Зачем это понадобилось. В коде уже были аккуратные логгеры (`oborot.auth`,
`oborot.scheduler`, `oborot.notify`, `oborot.ms_vendor`), но настройки
логирования в проекте не было НИ ОДНОЙ строчки. Прод поднимается через
`python run.py` → `uvicorn.run(...)`, а uvicorn настраивает только свои
собственные логгеры (`uvicorn`, `uvicorn.error`, `uvicorn.access`) и не трогает
корневой. Следствия проверены запуском, а не выведены на бумаге:

  • у корневого логгера НЕТ обработчиков, а уровень по умолчанию WARNING —
    значит **все `log.info(...)` в приложении никуда не попадали**: «планировщик
    запущен», «ежедневный синк: N организаций», «продолжение первичной загрузки
    запущено» не существовали для эксплуатации;
  • `log.warning(...)` и `log.exception(...)` выводились через аварийный
    `logging.lastResort` — то есть голой строкой в stderr, без времени, без
    уровня и без имени модуля. В journalctl «org=7: синк упал» лежит без
    отметки времени рядом с чужими строками.

Инцидент 03–21.08 (синк восемнадцать дней молча падал на протухшем токене)
стоил месяца устаревших данных. Он и не мог быть замечен по логам: сообщения
о падении были, но в неразличимом виде.

Что здесь настраивается:

  * корневой логгер получает обработчик в stderr (systemd/journalctl забирает
    его сам, файлов не заводим — на машине один процесс и есть journal);
  * формат с временем, уровнем, именем логгера и **идентификатором
    организации**;
  * уровень берётся из `OBOROT_LOG_LEVEL` (по умолчанию INFO).

Про организацию. Продукт многоарендный: одна и та же строка «синк упал»
бессмысленна, пока не известно, у КОГО. Идентификатор кладётся в
`contextvars.ContextVar`, а фильтр подставляет его в каждую запись. ContextVar,
а не глобальная переменная, потому что:

  * веб-запросы синхронные и выполняются в пуле потоков — anyio копирует
    контекст в поток, поэтому значение, выставленное в middleware, доезжает
    до обработчика;
  * фоновый синк идёт в отдельном потоке, а у нового потока контекст ПУСТОЙ,
    то есть чужое значение туда не протечёт: каждый поток выставляет своё.

Если организация неизвестна (страница логина, health, служебные задачи) —
в записи стоит `org=-`, и это честно.
"""
from __future__ import annotations

import logging
import logging.config
import os
from contextlib import contextmanager
from contextvars import ContextVar

_org_var: ContextVar[str] = ContextVar("oborot_org", default="-")

_CONFIGURED = False

DEFAULT_LEVEL = "INFO"
FORMAT = "%(asctime)s %(levelname)-7s %(name)s org=%(org)s | %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"


def set_org(org_id: int | str | None) -> None:
    """Привязывает последующие записи текущего контекста к организации."""
    _org_var.set(str(org_id) if org_id not in (None, "") else "-")


def current_org() -> str:
    """Идентификатор организации в текущем контексте («-», если неизвестна)."""
    return _org_var.get()


@contextmanager
def use_org(org_id: int | str | None):
    """Временно помечает записи организацией и возвращает прежнее значение.

    Нужен фоновым задачам: планировщик в одном потоке обходит организации по
    очереди, и без восстановления прежнего значения последняя организация
    «прилипла» бы ко всем последующим служебным записям.
    """
    token = _org_var.set(str(org_id) if org_id not in (None, "") else "-")
    try:
        yield
    finally:
        _org_var.reset(token)


class OrgFilter(logging.Filter):
    """Подставляет org в каждую запись — иначе формат упадёт на KeyError.

    Фильтр, а не адаптер: адаптер пришлось бы протаскивать через каждый вызов,
    а записи приходят и из чужого кода (SQLAlchemy, httpx), у которого никакого
    адаптера нет.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D102
        if not hasattr(record, "org"):
            record.org = _org_var.get()
        return True


def _level() -> str:
    raw = (os.environ.get("OBOROT_LOG_LEVEL") or DEFAULT_LEVEL).strip().upper()
    return raw if raw in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL") else DEFAULT_LEVEL


def setup_logging(force: bool = False) -> None:
    """Настраивает корневой логгер. Идемпотентна: повторный вызов ничего не ломает.

    Вызывается на импорте `app.main`, то есть ПОСЛЕ того, как uvicorn применил
    свою конфигурацию (он делает это до импорта приложения). `disable_existing_
    loggers: False` обязателен: иначе уже созданные логгеры uvicorn были бы
    выключены и пропал бы лог доступа.

    Логгеры uvicorn намеренно не трогаем: у них свои обработчики и
    `propagate=False`, поэтому их строки не задвоятся через корневой.
    """
    global _CONFIGURED
    if _CONFIGURED and not force:
        return
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "filters": {"org": {"()": "app.logging_conf.OrgFilter"}},
            "formatters": {
                "oborot": {"format": FORMAT, "datefmt": DATEFMT},
            },
            "handlers": {
                "stderr": {
                    "class": "logging.StreamHandler",
                    "stream": "ext://sys.stderr",
                    "formatter": "oborot",
                    "filters": ["org"],
                },
            },
            "root": {"level": _level(), "handlers": ["stderr"]},
            "loggers": {
                # Шумные библиотеки: их INFO не нужен, а WARNING нужен.
                "httpx": {"level": "WARNING"},
                "httpcore": {"level": "WARNING"},
                "sqlalchemy.engine": {"level": "WARNING"},
                "apscheduler": {"level": "WARNING"},
            },
        }
    )
    _CONFIGURED = True
