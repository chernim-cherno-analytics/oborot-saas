# -*- coding: utf-8 -*-
"""Полнота удаления арендатора: что именно стирается и почему это полный список.

Зачем отдельный модуль. Удаление организации и пользователя (`app.main._purge_org`,
`app.main._purge_user`) — это обещание «данные стёрты», и держится оно на ручном
перечислении моделей. Пока перечисление жило литералом внутри функции, ничто не
связывало его с реальным набором моделей: новая ORM-модель с `org_id`, забытая в
списке, оставляла бы осиротевшие строки с чужим `org_id` — и ни один тест этого
бы не заметил, потому что тесты проверяют таблицы, в которых строки ЕСТЬ.

Поэтому здесь три вещи и ничего кроме них:
  * НАБОРЫ УДАЛЕНИЯ — те же модели и в том же порядке, что раньше стояли в
    `_purge_org` / `_purge_user`. Порядок значим: от зависимых таблиц к
    корневым, иначе в Postgres упадёт внешний ключ.
  * ALLOWLIST ссылок на `orgs.id` / `users.id`, которые владением НЕ являются
    (авторство), — с причиной у каждой записи.
  * СТОРОЖ `purge_completeness_violations`: сверяет наборы с фактическим
    реестром SQLAlchemy. Семантически — по колонкам и внешним ключам, а не по
    тексту исходников: grep по `org_id` не увидел бы модель, которая
    ссылается на организацию колонкой с другим именем.

Сам сторож вызывается только из тестов (`tests/test_tenancy.py`), в рантайме
приложения он не работает.
"""
from __future__ import annotations

import importlib
import pkgutil

# Корневые таблицы арендатора. Они не входят в наборы удаления: строку `orgs`
# стирает отдельный DELETE в конце `_purge_org`, строку `users` — отдельный
# DELETE в конце `_purge_user`. Ссылки на самих себя у них нет.
ORG_ROOT_TABLE = "orgs"
USER_ROOT_TABLE = "users"


def org_purge_models() -> tuple:
    """Модели, удаляемые вместе с организацией. ПОРЯДОК ЗНАЧИМ.

    От зависимых таблиц к `orgs`: в Postgres внешние ключи проверяются
    (в SQLite по умолчанию нет), и обратный порядок упал бы.

    `OrderPlan` и `OrderReceipt` идут ПЕРЕД `ProductionOrder`: у обеих внешний
    ключ на заказ. `Production` идёт ПОСЛЕ `ProductionOrder`, а не перед ним:
    у заказа есть `production_id -> productions.id`, и прежний порядок (сначала
    `Production`, потом `ProductionOrder`) в Postgres нарушил бы внешний ключ.
    В SQLite, на котором работает прод, ключи не проверяются, поэтому дефект
    ничего не ломал и был не виден — его нашёл сторож порядка в
    `tests/test_tenancy.py`, а не отчёт с прода.

    Импорты внутри функции, а не наверху модуля: `app.routes_extra` тянет за
    собой половину приложения, и импорт на уровне модуля завязал бы порядок
    импортов приложения на этот вспомогательный файл.
    """
    from app.models import (
        CategoryMerge,
        Connection,
        Membership,
        NotifySettings,
        OrderedQty,
        OrderPlan,
        OrderReceipt,
        Product,
        Production,
        ProductionAssign,
        ProductionOrder,
        ReplenishDraft,
        Sale,
        SkuCategoryOverride,
        SkuDiscount,
        SkuHidden,
        StockDay,
        SyncState,
        Warehouse,
        WarehouseStock,
    )
    from app.routes_extra import BillingRequest

    return (
        Sale, StockDay, WarehouseStock, OrderedQty, ReplenishDraft,
        ProductionAssign, OrderPlan, OrderReceipt, ProductionOrder, Production,
        SkuHidden, SkuCategoryOverride, CategoryMerge, SkuDiscount,
        NotifySettings, SyncState, BillingRequest,
        Product, Warehouse, Connection, Membership,
    )


def user_purge_models() -> tuple:
    """Модели, удаляемые вместе с пользователем. ПОРЯДОК ЗНАЧИМ.

    `Membership` входит и сюда, и в набор организации — это не исключение, а
    факт: у строки членства есть и `org_id`, и `user_id`, и она обязана уйти
    в обоих потоках. Саму строку `users` стирает отдельный DELETE после цикла.
    """
    from app.models import Membership, UserHintSeen, UserLesson, UserPrefs

    return (UserHintSeen, UserLesson, UserPrefs, Membership)


# ── Ссылки, которые владением не являются ────────────────────────────────────
#
# Ключ — (таблица, колонка), значение — причина. Причина обязательна: запись
# без объяснения — это не исключение, а забытая модель под другим именем.
# Список проверяется В ОБЕ СТОРОНЫ: если колонка исчезла или переименована,
# сторож падает и на устаревшей записи тоже. Иначе allowlist протухнет ровно
# так же, как протухает текст технического долга.

ORG_REF_ALLOWLIST: dict[tuple[str, str], str] = {
    # Пусто, и это не заглушка: все таблицы со ссылкой на организацию сегодня
    # действительно стираются вместе с ней. Пустой allowlist — самое сильное
    # состояние этого файла, и терять его без причины не следует.
}

USER_REF_ALLOWLIST: dict[tuple[str, str], str] = {
    ("production_orders", "created_by"): (
        "авторство, а не владение: строка заказа принадлежит ОРГАНИЗАЦИИ "
        "(читается только по org_id) и уходит вместе с ней"
    ),
    ("order_plans", "created_by"): (
        "авторство плана заказа; строка org-owned и удаляется в потоке "
        "организации"
    ),
    ("order_receipts", "created_by"): (
        "авторство приёмки; строка org-owned. Колонка объявлена без внешнего "
        "ключа на users.id — сторож видит её по имени, см. USER_REF_COLUMN_NAMES"
    ),
    ("billing_requests", "user_id"): (
        "кто подал заявку на счёт; заявка принадлежит организации и читается "
        "только по org_id (app/routes_extra.py), удаляется в потоке организации"
    ),
}


def import_all_models(package: str = "app") -> None:
    """Импортирует все модули пакета, чтобы реестр моделей был полным.

    Обходом, а не перечислением: перечисление пришлось бы поддерживать руками —
    ровно та болезнь, от которой лечит этот файл. Модуль, который не
    импортируется, пропускается молча: сторож не обязан чинить сломанный
    импорт, но и падать вместо него не должен.
    """
    pkg = importlib.import_module(package)
    for info in pkgutil.iter_modules(pkg.__path__):
        try:
            importlib.import_module(f"{package}.{info.name}")
        except Exception:  # noqa: BLE001 — недоступный модуль не задача сторожа
            continue


# Имена колонок, которые в этом проекте означают ссылку на арендатора даже без
# объявленного внешнего ключа. Список НАМЕРЕННО крошечный и обоснованный:
# `order_receipts.created_by` объявлен обычным Integer без ForeignKey
# (app/models.py), и по одним только внешним ключам сторож его бы не увидел —
# а это ровно тот случай, ради которого он написан. Все три `created_by` в
# проекте ссылаются на `users.id` и ни на что другое.
ORG_REF_COLUMN_NAMES = frozenset({"org_id"})
USER_REF_COLUMN_NAMES = frozenset({"user_id", "created_by"})


def _tenant_refs(table, root_table: str, column_names: frozenset) -> set[str]:
    """Колонки таблицы, которыми она ссылается на арендатора.

    Две приметы, объединение, а не пересечение:
      * внешний ключ на `orgs.id` / `users.id` — как бы колонка ни называлась
        (модель с `organization_id -> orgs.id` такая же org-owned, и сторож,
        который смотрел бы только на имя, её бы пропустил);
      * имя колонки из списка выше — для ссылок, у которых внешнего ключа нет.
    """
    found = set()
    for column in table.columns:
        if column.name in column_names:
            found.add(column.name)
            continue
        for fk in column.foreign_keys:
            if fk.column.table.name == root_table:
                found.add(column.name)
                break
    return found


def purge_completeness_violations(registry=None) -> list[str]:
    """Список нарушений полноты удаления. Пустой список — всё объяснено.

    Сверяет фактический реестр SQLAlchemy с наборами удаления и allowlist.
    Возвращает человеческие строки: сообщение сторожа читает тот, кто добавил
    модель и не знал про этот файл, — оно обязано говорить, что именно сделать.
    """
    if registry is None:
        import_all_models()
        from app.db import Base

        registry = Base.registry

    org_models = org_purge_models()
    user_models = user_purge_models()
    org_tables = {m.__tablename__: m for m in org_models}
    user_tables = {m.__tablename__: m for m in user_models}

    problems: list[str] = []
    seen_org_refs: set[tuple[str, str]] = set()
    seen_user_refs: set[tuple[str, str]] = set()

    for mapper in registry.mappers:
        cls = mapper.class_
        table = mapper.local_table
        if table is None:
            continue
        name = table.name
        where = f"{cls.__module__}.{cls.__name__} (таблица {name})"

        org_refs = _tenant_refs(table, ORG_ROOT_TABLE, ORG_REF_COLUMN_NAMES)
        for column in sorted(org_refs):
            seen_org_refs.add((name, column))
            if name == ORG_ROOT_TABLE:
                continue
            if name in org_tables and column == "org_id":
                continue
            if (name, column) in ORG_REF_ALLOWLIST:
                continue
            problems.append(
                f"{where}: колонка {column} ссылается на организацию, но модель "
                f"не удаляется вместе с организацией. Добавь её в "
                f"app.tenancy.org_purge_models() в правильном по внешним ключам "
                f"месте — либо, если строка организации не принадлежит, объясни "
                f"это записью в ORG_REF_ALLOWLIST"
            )

        user_refs = _tenant_refs(table, USER_ROOT_TABLE, USER_REF_COLUMN_NAMES)
        for column in sorted(user_refs):
            seen_user_refs.add((name, column))
            if name == USER_ROOT_TABLE:
                continue
            if name in user_tables and column == "user_id":
                continue
            if (name, column) in USER_REF_ALLOWLIST:
                continue
            problems.append(
                f"{where}: колонка {column} ссылается на пользователя, но модель "
                f"не удаляется вместе с пользователем. Добавь её в "
                f"app.tenancy.user_purge_models() — либо, если это авторство "
                f"org-owned строки, а не личные данные, объясни это записью "
                f"в USER_REF_ALLOWLIST"
            )

    # Наборы удаления не должны разъезжаться с реестром в обратную сторону:
    # модель, которую цикл удаления трогает по несуществующей колонке, упала бы
    # в рантайме — в момент, когда человек нажал «удалить аккаунт».
    mapped_tables = {m.local_table.name for m in registry.mappers if m.local_table is not None}
    for model in org_models:
        name = getattr(model, "__tablename__", str(model))
        if name not in mapped_tables:
            problems.append(f"org_purge_models(): {name} нет в реестре моделей")
        elif "org_id" not in model.__table__.c:
            problems.append(
                f"org_purge_models(): у {name} нет колонки org_id, а цикл удаления "
                f"обращается к model.org_id — удаление организации упало бы"
            )
    for model in user_models:
        name = getattr(model, "__tablename__", str(model))
        if name not in mapped_tables:
            problems.append(f"user_purge_models(): {name} нет в реестре моделей")
        elif "user_id" not in model.__table__.c:
            problems.append(
                f"user_purge_models(): у {name} нет колонки user_id, а цикл удаления "
                f"обращается к model.user_id — удаление аккаунта упало бы"
            )

    # Протухший allowlist опаснее отсутствующего: он молча разрешает то, чего
    # уже нет, и вместе с этим — то, что появится под старым именем.
    for key, reason in ORG_REF_ALLOWLIST.items():
        if key not in seen_org_refs:
            problems.append(
                f"ORG_REF_ALLOWLIST: записи {key[0]}.{key[1]} больше нет в моделях "
                f"— удали её (причина была: {reason})"
            )
    for key, reason in USER_REF_ALLOWLIST.items():
        if key not in seen_user_refs:
            problems.append(
                f"USER_REF_ALLOWLIST: записи {key[0]}.{key[1]} больше нет в моделях "
                f"— удали её (причина была: {reason})"
            )

    return sorted(problems)
