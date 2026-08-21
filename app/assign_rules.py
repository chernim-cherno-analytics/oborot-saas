# -*- coding: utf-8 -*-
"""Распределение позиций по производствам — правилом, а не руками.

Задача (Влад, 21.08.2026): у Chernim Cherno в карточке товара МойСклада стоит
контрагент-поставщик; у того, что шьётся в Китае, это «Китай», у остального —
своё производство. Раскладывать 557 позиций руками бессмысленно, а у будущих
клиентов данные лежат по-своему — поэтому правило универсальное.

Как это устроено:

  источник (assign_source)          что берём из МойСклада
  ─────────────────────────         ───────────────────────────────────────
  supplier                          контрагент-поставщик в карточке товара
  folder                            папка товара (группа МС)
  manual                            только ручные назначения

  карта (assign_map): {значение источника → id производства}
  всё, что не совпало → основное производство.

Приоритет, от сильного к слабому:
  1) ручное назначение позиции (ProductionAssign) — владелец сильнее правила;
  2) правило по источнику;
  3) основное производство.

Правило для НОВЫХ организаций (suggest_rule): после первого синка смотрим, чем
реально заполнены данные, и предлагаем готовую раскладку — но не применяем её
молча, а показываем таблицей «значение → канал» со счётчиками позиций.
"""
from __future__ import annotations

import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Org, Product, Production, ProductionAssign

SOURCES = ("manual", "supplier", "folder")
# Имя поставщика/папки, по которому видно производство «под ключ».
TURNKEY_HINTS = ("китай", "china", "фабрик", "под ключ", "turnkey")
MIN_FILLED_SHARE = 0.15  # источник предлагаем, если заполнен хотя бы у 15% позиций


def rule_of(org: Org) -> dict:
    """Правило распределения из настроек организации (с дефолтами)."""
    try:
        data = json.loads(org.settings_json or "{}")
    except ValueError:
        data = {}
    src = data.get("assign_source")
    if src not in SOURCES:
        src = "manual"
    raw = data.get("assign_map")
    amap: dict[str, int] = {}
    if isinstance(raw, dict):
        for key, val in raw.items():
            try:
                amap[str(key)] = int(val)
            except (TypeError, ValueError):
                continue
    return {"assign_source": src, "assign_map": amap}


def _value_by_base(db: Session, org_id: int, source: str) -> dict[str, str]:
    """Значение источника по базовому имени позиции.

    У позиции несколько размеров-товаров; берём НАИБОЛЕЕ ЧАСТОЕ непустое
    значение — один размер с незаполненным поставщиком не должен ломать
    распределение всей модели.
    """
    col = Product.supplier if source == "supplier" else Product.category
    rows = db.execute(
        select(Product.base_name, col, func.count())
        .where(Product.org_id == org_id, Product.excluded.is_(False))
        .group_by(Product.base_name, col)
    ).all()
    best: dict[str, tuple[int, str]] = {}
    for base, value, cnt in rows:
        value = str(value or "").strip()
        if not value:
            continue
        cur = best.get(base)
        if cur is None or cnt > cur[0]:
            best[base] = (int(cnt), value)
    return {base: v for base, (_, v) in best.items()}


def main_production_id(db: Session, org_id: int) -> int | None:
    row = db.execute(
        select(Production.id).where(
            Production.org_id == org_id, Production.is_main.is_(True))
    ).scalars().first()
    if row is not None:
        return row
    return db.execute(
        select(Production.id).where(Production.org_id == org_id)
        .order_by(Production.id)
    ).scalars().first()


def effective_assign(db: Session, org: Org) -> dict[str, int]:
    """Итоговое распределение {base_name: production_id} — правило + ручное."""
    valid = set(db.execute(
        select(Production.id).where(Production.org_id == org.id)).scalars())
    main_id = main_production_id(db, org.id)
    rule = rule_of(org)
    out: dict[str, int] = {}
    if rule["assign_source"] != "manual" and rule["assign_map"]:
        values = _value_by_base(db, org.id, rule["assign_source"])
        for base, value in values.items():
            pid = rule["assign_map"].get(value)
            if pid in valid:
                out[base] = pid
    # Ручные назначения сильнее правила.
    for row in db.execute(
        select(ProductionAssign).where(ProductionAssign.org_id == org.id)
    ).scalars():
        if row.production_id in valid:
            out[row.base_name] = row.production_id
    # Пин на ОСНОВНОЕ производство остаётся в карте: при активном правиле это
    # осмысленное решение владельца («эту позицию шьём у себя, хотя поставщик
    # говорит иначе»), и его нельзя путать с отсутствием записи.
    return out


def source_values(db: Session, org_id: int) -> dict:
    """Что вообще есть в данных: значения источников со счётчиками позиций.

    По этому экрану владелец за один проход раскладывает каналы, а мы ничего
    не выдумываем за него — показываем ровно то, что стоит в МойСкладе.
    """
    out = {}
    for source in ("supplier", "folder"):
        values = _value_by_base(db, org_id, source)
        counts: dict[str, int] = {}
        for value in values.values():
            counts[value] = counts.get(value, 0) + 1
        total = db.execute(
            select(func.count(func.distinct(Product.base_name)))
            .where(Product.org_id == org_id, Product.excluded.is_(False))
        ).scalar() or 0
        out[source] = {
            "values": [{"value": v, "positions": c}
                       for v, c in sorted(counts.items(), key=lambda kv: -kv[1])][:60],
            "filled": len(values),
            "total": int(total),
            "empty": max(0, int(total) - len(values)),
        }
    return out


def suggest_rule(db: Session, org_id: int) -> dict:
    """Что предложить новой организации после первого синка.

    Логика: берём источник, который РЕАЛЬНО заполнен (порог MIN_FILLED_SHARE),
    поставщик приоритетнее папки. Значения, похожие на «Китай»/«фабрика»,
    предлагаем как канал «под ключ», остальные — как «ткань → пошив».
    Незаполненные позиции остаются на основном производстве.
    """
    stats = source_values(db, org_id)
    for source in ("supplier", "folder"):
        info = stats[source]
        total = info["total"] or 1
        if info["filled"] / total < MIN_FILLED_SHARE:
            continue
        # Один-единственный вариант распределять незачем.
        if len(info["values"]) < 2 and info["empty"] == 0:
            continue
        channels = []
        for row in info["values"][:8]:
            low = row["value"].lower()
            turnkey = any(h in low for h in TURNKEY_HINTS)
            channels.append({
                "value": row["value"],
                "positions": row["positions"],
                "suggest_name": row["value"],
                "suggest_preset": "turnkey" if turnkey else "fabric_sewing",
            })
        return {"assign_source": source, "channels": channels,
                "empty_positions": info["empty"], "reason": (
                    "поставщик заполнен у большинства позиций"
                    if source == "supplier" else
                    "товары разложены по папкам, а поставщик не заполнен")}
    return {"assign_source": "manual", "channels": [], "empty_positions": 0,
            "reason": "в данных нет признака, по которому видно производство — "
                      "распределите позиции вручную на странице «Заказ»"}
