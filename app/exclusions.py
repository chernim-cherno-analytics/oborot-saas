"""Исключение нетоварных позиций из аналитики (упаковка, сертификаты, расходники).

Зачем: у продавцов в МойСклад лежат не только товары — пакеты, коробки, бирки,
подарочные сертификаты. Если считать их «товаром», сток и метрики раздуваются
(у Chernim Cherno сертификаты давали +20,6 млн ₽ «стока в рознице»).

Механика:
- Product.excluded — флаг «не участвует в аналитике» (остатки, продажи, метрики,
  алерты). Управляется по базовому имени (все размеры разом).
- Авто-эвристика is_service_item() срабатывает ТОЛЬКО при создании позиции синком
  (и один раз при миграции старой БД) — ручной выбор пользователя не перетирается.
- Эвристика универсальная (не только fashion): категории-расходники + словарь
  подстрок в названии. Консервативная: сомнительное не исключаем.
"""
from __future__ import annotations

import re

from sqlalchemy import inspect, text

from app.db import engine, run_migration_once, run_migration_step

# Категории МойСклад (productFolder), которые почти наверняка не товар для аналитики.
SERVICE_CATEGORIES = {
    "расходный материал",
    "расходные материалы",
    "упаковка",
    "gift cards",
    "подарочные сертификаты",
    "сертификаты",
    "samples",
    "сэмплы",
    "образцы",
}

# Подстроки в названии (lower). Слова подобраны так, чтобы не задевать одежду,
# обувь, украшения, косметику: «пакет» не входит в названия вещей и т.п.
_NAME_PATTERNS = [
    "сертификат",
    "пакет",            # брендированный/упаковочный пакет
    "коробк",           # коробка/коробки
    "конверт",
    "бирк",             # бирка/бирки
    "вешалк",
    "кофр",
    "наклейк",
    "этикет",           # этикетка/этикеточная лента
    "пломб",
    "бумага тишью",
    "холдер",
    "чехол для коробки",
    "салфетк",
    "нитки",
    "нитка",
    "упаковоч",
    "sample",           # сэмплы моделей: не продаются как товар, шумят в аналитике
    "сэмпл",
]
_NAME_RE = re.compile("|".join(re.escape(p) for p in _NAME_PATTERNS))


def is_service_item(name: str, category: str = "") -> bool:
    """True, если позиция похожа на расходник/сертификат, а не на товар."""
    if (category or "").strip().lower() in SERVICE_CATEGORIES:
        return True
    return bool(_NAME_RE.search((name or "").lower()))


def ensure_schema() -> None:
    """Аддитивная миграция: products.excluded + разовый бэкфилл эвристикой.

    ALTER TABLE ADD COLUMN работает в SQLite и Postgres. Бэкфилл выполняется
    только в момент добавления колонки (существующие базы), чтобы не трогать
    последующий ручной выбор пользователя.

    Ревью 22.08: колонка булева, поэтому значения — DEFAULT FALSE и параметры
    True/False, а не 0/1 (Postgres не сравнивает boolean с integer и на этом
    месте не давал приложению стартовать вовсе). Шаги выполняются через
    помощники app/db.py и переживают одновременный старт нескольких воркеров.
    """
    insp = inspect(engine)
    if not insp.has_table("products"):
        return
    cols = {c["name"] for c in insp.get_columns("products")}
    if "excluded" not in cols:
        added = run_migration_step(
            "ALTER TABLE products ADD COLUMN excluded BOOLEAN NOT NULL DEFAULT FALSE"
        )
        if added:
            # Колонку добавили мы — наш и бэкфилл (если опередил соседний
            # процесс, бэкфилл делает он, и повторять нечего).
            with engine.begin() as conn:
                _mark_service_items(conn)
    _run_backfill_once("excl_samples_v1")


def _mark_service_items(conn) -> None:
    """Помечает excluded=True всё, что эвристика считает не товаром."""
    rows = conn.execute(text(
        "SELECT id, base_name, category FROM products WHERE excluded = :no"
    ), {"no": False}).fetchall()
    for pid in [r[0] for r in rows if is_service_item(r[1], r[2])]:
        conn.execute(
            text("UPDATE products SET excluded = :yes WHERE id = :pid"),
            {"yes": True, "pid": pid},
        )


def _run_backfill_once(flag: str) -> None:
    """Разовый бэкфилл эвристикой для НОВЫХ ключей (например, sample/сэмпл).

    Флаг в migration_flags гарантирует один запуск: пользовательское решение
    вернуть позицию в аналитику после этого не перетирается.
    """
    run_migration_once(flag, _mark_service_items)
