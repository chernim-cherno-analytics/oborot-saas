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

from app.db import engine

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
    """
    insp = inspect(engine)
    if not insp.has_table("products"):
        return
    cols = {c["name"] for c in insp.get_columns("products")}
    if "excluded" not in cols:
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE products ADD COLUMN excluded BOOLEAN NOT NULL DEFAULT 0"
            ))
            rows = conn.execute(text("SELECT id, base_name, category FROM products")).fetchall()
            service_ids = [r[0] for r in rows if is_service_item(r[1], r[2])]
            for pid in service_ids:
                conn.execute(text("UPDATE products SET excluded = 1 WHERE id = :pid"), {"pid": pid})
    _run_backfill_once("excl_samples_v1")


def _run_backfill_once(flag: str) -> None:
    """Разовый бэкфилл эвристикой для НОВЫХ ключей (например, sample/сэмпл).

    Флаг в migration_flags гарантирует один запуск: пользовательское решение
    вернуть позицию в аналитику после этого не перетирается.
    """
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS migration_flags (name VARCHAR(64) PRIMARY KEY)"
        ))
        done = conn.execute(
            text("SELECT 1 FROM migration_flags WHERE name = :n"), {"n": flag}
        ).first()
        if done:
            return
        rows = conn.execute(text(
            "SELECT id, base_name, category FROM products WHERE excluded = 0"
        )).fetchall()
        for pid in [r[0] for r in rows if is_service_item(r[1], r[2])]:
            conn.execute(text("UPDATE products SET excluded = 1 WHERE id = :pid"), {"pid": pid})
        conn.execute(text("INSERT INTO migration_flags (name) VALUES (:n)"), {"n": flag})
