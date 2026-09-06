#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Включить или выключить предпросмотр производственной таблицы у ОДНОЙ организации.

ЗАЧЕМ ЭТОТ ФАЙЛ СУЩЕСТВУЕТ. Вкладка «Предпросмотр таблицы» и ручки
`/api/supply/sheets*` закрыты флагом организации `settings.supply_sheets_preview`
(SUPPLY-FIX-1, F-08): парсер разбирает ОДНУ конкретную таблицу — точные
заголовки на фиксированных колонках, ровно два листа, fail-closed, — и всем
остальным организациям он показывает вкладку, за которой для них ничего нет.
Флаг временный: он стоит до развилки владельца Р-1 (судьба парсера Google
Sheets) и исчезнет вместе с ней.

Экрана и ручки у флага нет намеренно: это не настройка продукта, а операторская
мера на время развилки, и заводить под неё элемент интерфейса значило бы
объявить временное постоянным. Но и ручной `UPDATE` на боевой базе запрещён
(`AGENTS.md` §1: секреты, боевые данные и несанкционированные операции руками не
трогаем). Этот инструмент закрывает разрыв: одно действие, названное словами,
проверенное тестом, показывающее состояние до и после и обратимое тем же
вызовом с `--off`.

ЧТО ОН ДЕЛАЕТ И ЧЕГО НЕ ДЕЛАЕТ.

* Меняет РОВНО один ключ `supply_sheets_preview` в `orgs.settings_json` одной
  названной организации. Остальные ключи читаются и записываются как есть:
  пороги, горизонт, типы цен, правило распределения и пики переживают вызов
  без изменения — это проверяется тестом, а не обещанием.
* Не создаёт и не удаляет организации, не трогает подключения, снимок
  предпросмотра, подписку, `paid_until` и вообще любую другую колонку.
* Не выполняет миграций и ничего не создаёт в схеме: колонки под флаг нет и не
  будет, он живёт в существующем JSON.
* Не является частью продукта: ни роута, ни страницы, ни планировщика.
* Идемпотентен: повторный вызов с тем же значением ничего не пишет и честно
  говорит «уже так».

КАК ЗАПУСКАТЬ (на сервере, из каталога приложения, окружением приложения):

    DATABASE_URL=... python tools/supply_sheets_preview.py --show
    DATABASE_URL=... python tools/supply_sheets_preview.py --org-id 3 --on
    DATABASE_URL=... python tools/supply_sheets_preview.py --org-id 3 --off

`--show` без `--org-id` печатает список организаций с состоянием флага и НИЧЕГО
не пишет — с него разумно начинать, чтобы называть организацию номером, а не
догадкой по имени. Имена организаций — данные их владельцев, поэтому вывод
идёт в stdout оператора и никуда не сохраняется.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

FLAG = "supply_sheets_preview"


def _load(raw: str | None) -> dict:
    """Разбор `settings_json` без исключений: битая строка не должна ронять правку.

    Пустой и нечитаемый JSON здесь равны пустому словарю — ровно так же, как их
    читает само приложение (`Org.settings`, `Org.supply_sheets_preview`). Иначе
    инструмент отказывал бы там, где приложение работает.
    """
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def show(session, org_id: int | None) -> int:
    from app.models import Org

    rows = session.query(Org).order_by(Org.id).all()
    if org_id is not None:
        rows = [o for o in rows if o.id == org_id]
        if not rows:
            print(f"Организации с id={org_id} нет.")
            return 2
    for org in rows:
        state = "ВКЛ" if _load(org.settings_json).get(FLAG) is True else "выкл"
        print(f"  id={org.id:<5} {state:<5} {org.name}")
    if not rows:
        print("  (организаций нет)")
    return 0


def switch(session, org_id: int, on: bool) -> int:
    from app.models import Org

    org = session.get(Org, org_id)
    if org is None:
        print(f"Организации с id={org_id} нет — ничего не изменено.")
        return 2
    settings = _load(org.settings_json)
    was = settings.get(FLAG) is True
    print(f"Организация id={org.id} «{org.name}»")
    print(f"  было:  {'ВКЛ' if was else 'выкл'}")
    if was == on:
        print(f"  стало: {'ВКЛ' if on else 'выкл'} (уже так — ничего не записано)")
        return 0
    if on:
        settings[FLAG] = True
    else:
        # Выключение УДАЛЯЕТ ключ, а не пишет false: отсутствие ключа — это то
        # же состояние, в котором живут все прочие организации, и после отката
        # инструмента в базе не остаётся следа временной меры.
        settings.pop(FLAG, None)
    org.settings_json = json.dumps(settings, ensure_ascii=False)
    session.commit()
    fresh = _load(session.get(Org, org_id).settings_json).get(FLAG) is True
    print(f"  стало: {'ВКЛ' if fresh else 'выкл'}")
    if fresh != on:
        print("  ОШИБКА: значение после записи не совпало с запрошенным.")
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Флаг предпросмотра производственной таблицы у одной организации.")
    ap.add_argument("--org-id", type=int, default=None,
                    help="номер организации (см. --show)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--on", action="store_true", help="включить флаг")
    mode.add_argument("--off", action="store_true", help="выключить флаг")
    mode.add_argument("--show", action="store_true",
                      help="только показать состояние, ничего не писать")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not (args.on or args.off or args.show):
        print("Укажите одно из: --show, --on, --off.")
        return 2
    if (args.on or args.off) and args.org_id is None:
        print("Для --on/--off нужен --org-id (список организаций: --show).")
        return 2
    if not os.environ.get("DATABASE_URL"):
        print("DATABASE_URL не задан — не понимаю, какую базу править.")
        return 2

    from app.db import SessionLocal

    session = SessionLocal()
    try:
        if args.show:
            return show(session, args.org_id)
        return switch(session, args.org_id, on=bool(args.on))
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
