"""PROD-1 «Производство»: где каждый заказ и когда он будет на складе.

Решение Влада 17.11.2026 (спецификация docs/SPEC_PRODUCTION_TRACKING.md):
запуск это заказ на производство (`ProductionOrder`), отдельной сущности
«партия» нет. Слой только ЧИТАЕТ заказы и приёмки и пишет свои записи: отметки
этапов и сроки от цеха. «Едет», рекомендации, бюджет, плановая дата и статус
заказа здесь не меняются, формулы D-35 не затронуты.

Две даты, и они не смешиваются (итог прогона критиков 17.11):
  * СРОК это договорённость: последний срок, который назвал цех, иначе
    плановая дата заказа, иначе отправка + срок цеха. Только от него
    считается «опаздывает» (красное).
  * ОЦЕНКА это расчёт по истории цеха: дата последней отмеченной точки +
    медиана дней от этой точки до прихода у закрытых заказов того же цеха
    (не меньше 2 за 365 дней). Она подсказка: красным не бывает, а если
    выходит позже срока, показывается предупреждение.
Прошедший срок без прихода не сдвигается: «опаздывает на N дн».
"""
from __future__ import annotations

import json
import math
import re
import statistics
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    OrderDueDate,
    OrderReceipt,
    OrderStageMark,
    Production,
    ProductionOrder,
    ProductionTrack,
    User,
)

PRESETS: dict[str, list[str]] = {
    # Свой или знакомый цех: ткань отдают в цех, там кроят и шьют.
    "sewing": ["Ткань у цеха", "Крой", "Пошив", "Сдано цехом"],
    # Фабрика «под ключ» (часто Китай): ткань и пошив на стороне фабрики.
    "turnkey": ["Производство", "Проверка", "Отправлено"],
}
PRESET_LABELS = {"sewing": "Свой пошив", "turnkey": "Под ключ", "custom": "Свои этапы"}

MAX_STAGES = 8
STAGE_NAME_MAX = 40
NOTE_MAX = 300
QTY_MAX = 100_000
HISTORY_DAYS = 365
MIN_HISTORY = 2
RECEIVED_SHOW_DAYS = 30
ARRIVING_WINDOW_DAYS = 14
DUE_AHEAD_DAYS = 730
SENT_POINT = "_sent"
# Двойной тап или повтор запроса: та же запись за эти секунды не пишется второй раз.
REPEAT_WINDOW_SEC = 15
_ISO_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


class TrackingError(ValueError):
    """Ошибка ввода, понятная человеку. `status` уходит в HTTP-ответ."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


# ── Даты ─────────────────────────────────────────────────────────────────────

def _d(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def parse_day(value, what: str = "Дата") -> date:
    """Строго ГГГГ-ММ-ДД: «2026-W47-1» и «20261117» не принимаются."""
    text = str(value or "").strip()
    if not _ISO_DAY.fullmatch(text):
        raise TrackingError(f"{what}: нужен формат ГГГГ-ММ-ДД")
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise TrackingError(f"{what} задана неверно")


def _median_days(values: list[int]) -> int:
    return int(math.ceil(statistics.median(values)))


# ── Этапы цеха ───────────────────────────────────────────────────────────────

def _preset_stages(preset: str) -> list[dict]:
    names = PRESETS.get(preset) or PRESETS["sewing"]
    return [{"key": f"s{i + 1}", "name": n} for i, n in enumerate(names)]


def default_preset(production: Production | None) -> str:
    """Шаблон этапов для цеха, у которого их ещё не задавали.

    Не догадка о цехе, а чтение его же условий: один этап на весь срок
    (`Production.stages`) это фабрика «под ключ», иначе пошив.
    """
    if production is not None:
        planning = [s for s in production.stages if isinstance(s, dict)]
        if len(planning) == 1:
            return "turnkey"
    return "sewing"


def _clean_stages(raw) -> list[dict]:
    out, seen = [], set()
    if not isinstance(raw, list):
        return out
    for st in raw:
        if not isinstance(st, dict):
            continue
        key = str(st.get("key") or "").strip()
        name = str(st.get("name") or "").strip()
        if not key or not name or key in seen or not re.fullmatch(r"s\d{1,3}", key):
            continue
        seen.add(key)
        out.append({"key": key, "name": name[:STAGE_NAME_MAX]})
    return out[:MAX_STAGES]


def _productions(db: Session, org_id: int) -> dict[int, Production]:
    return {p.id: p for p in db.execute(
        select(Production).where(Production.org_id == org_id).order_by(Production.id)
    ).scalars()}


def org_tracks(db: Session, org_id: int, prods: dict | None = None) -> tuple[dict, list[dict]]:
    """Этапы всех цехов организации.

    tracks[id цеха или 0] = {"preset", "stages"}; 0 это заказы без цеха и
    заказы удалённого цеха. Строки нет, значит этапы по шаблону.
    Строка этапов, записанная раньше, чем создан цех с этим id, принадлежала
    удалённому цеху (SQLite переиспользует id) и новому цеху не передаётся.
    """
    prods = prods if prods is not None else _productions(db, org_id)
    rows: dict[int, ProductionTrack] = {}
    for r in db.execute(select(ProductionTrack).where(ProductionTrack.org_id == org_id)
                        .order_by(ProductionTrack.id)).scalars():
        pid = r.production_id or 0
        p = prods.get(pid)
        if pid and (p is None or (p.created_at and r.updated_at and r.updated_at < p.created_at)):
            continue
        rows[pid] = r
    tracks: dict[int, dict] = {}
    channels: list[dict] = []
    for p in [None, *prods.values()]:
        pid = p.id if p is not None else 0
        row = rows.get(pid)
        stages: list[dict] = []
        preset = default_preset(p)
        if row is not None:
            try:
                stages = _clean_stages(json.loads(row.stages_json or "[]"))
            except ValueError:
                stages = []
            preset = row.preset or preset
        if not stages:
            stages = _preset_stages(preset if preset in PRESETS else "sewing")
        tracks[pid] = {"preset": preset, "stages": stages}
        channels.append({"id": pid, "name": p.name if p is not None else "Без цеха",
                         "is_main": bool(p.is_main) if p is not None else False,
                         "preset": preset, "stages": stages})
    return tracks, channels


def _channel_of(order: ProductionOrder, tracks: dict) -> int:
    pid = order.production_id or 0
    return pid if pid in tracks else 0


def set_track(db: Session, org_id: int, production_id: int, preset: str,
              stages_in: list) -> dict:
    """Сохранить этапы цеха. 0 = заказы без цеха.

    Ключи существующих этапов сохраняются (переименование не теряет
    отметки), новым выдаются свободные. Этап, по которому уже есть отметки,
    удалить нельзя: история перестала бы читаться.
    """
    prods = _productions(db, org_id)
    if production_id and production_id not in prods:
        raise TrackingError("Цех не найден", 404)
    if preset not in PRESET_LABELS:
        raise TrackingError("Неизвестный шаблон этапов")
    tracks, _ = org_tracks(db, org_id, prods)
    current = tracks.get(production_id) or {"stages": []}
    if preset in PRESETS and not stages_in:
        stages_in = [{"key": s["key"], "name": n}
                     for s, n in zip(current["stages"] + [{"key": ""}] * MAX_STAGES, PRESETS[preset])]
    if not isinstance(stages_in, list) or not stages_in:
        raise TrackingError("Нужен хотя бы один этап")
    if len(stages_in) > MAX_STAGES:
        raise TrackingError(f"Этапов не больше {MAX_STAGES}")
    known_keys = {s["key"] for s in current["stages"]}
    taken: set[str] = set()
    result = []
    for st in stages_in:
        if not isinstance(st, dict):
            raise TrackingError("Этап задан неверно")
        name = re.sub(r"\s+", " ", str(st.get("name") or "")).strip()
        if not name:
            raise TrackingError("У этапа должно быть название")
        if len(name) > STAGE_NAME_MAX:
            raise TrackingError(f"Название этапа не длиннее {STAGE_NAME_MAX} знаков")
        if name.lower() in {r["name"].lower() for r in result}:
            raise TrackingError(f"Этап «{name}» повторяется")
        key = str(st.get("key") or "").strip()
        if key not in known_keys or key in taken:
            key = ""
        if not key:
            n = 1
            while f"s{n}" in known_keys or f"s{n}" in taken:
                n += 1
            key = f"s{n}"
        taken.add(key)
        result.append({"key": key, "name": name})
    removed = known_keys - taken
    if removed:
        marked = _keys_with_marks(db, org_id, production_id, removed, prods)
        if marked:
            names = [s["name"] for s in current["stages"] if s["key"] in marked]
            raise TrackingError(
                "Нельзя убрать этап, по которому уже есть отметки: "
                + ", ".join(f"«{n}»" for n in names)
                + ". Его можно переименовать.", 409)
    row = db.execute(
        select(ProductionTrack).where(
            ProductionTrack.org_id == org_id,
            (ProductionTrack.production_id == production_id) if production_id
            else ProductionTrack.production_id.is_(None),
        ).order_by(ProductionTrack.id.desc()).limit(1)
    ).scalar_one_or_none()
    if row is None:
        row = ProductionTrack(org_id=org_id, production_id=production_id or None)
        db.add(row)
    row.preset = preset
    row.stages_json = json.dumps(result, ensure_ascii=False)
    row.updated_at = datetime.utcnow()
    return {"preset": preset, "stages": result}


def _keys_with_marks(db: Session, org_id: int, production_id: int, keys: set[str],
                     prods: dict) -> set[str]:
    """Ключи этапов, по которым у заказов ЭТОГО цеха есть отметки.

    Заказы удалённого цеха относятся к «Без цеха» (0), как и на доске:
    иначе защиту можно было обойти, удалив цех.
    """
    q = (select(OrderStageMark.stage_key, OrderStageMark.order_batch,
                ProductionOrder.cc_batch_id, ProductionOrder.production_id)
         .join(ProductionOrder, ProductionOrder.id == OrderStageMark.order_id)
         .where(OrderStageMark.org_id == org_id, ProductionOrder.org_id == org_id,
                OrderStageMark.stage_key.in_(keys)))
    out = set()
    for k, mb, ob, pid in db.execute(q).all():
        channel = (pid or 0) if (pid or 0) in prods else 0
        if channel == production_id and _same_batch(mb, ob):
            out.add(k)
    return out


def _same_batch(mark_batch: str, order_batch: str) -> bool:
    """Запись принадлежит заказу, только если совпадает и партия (D-50).

    Пустой идентификатор бывает только у строк, созданных откатившимся
    старым кодом; тогда сверяемся по id, как раньше.
    """
    return not mark_batch or not order_batch or mark_batch == order_batch


def _lead_days(order: ProductionOrder, prods: dict, org_lead: int) -> int:
    p = prods.get(order.production_id or 0)
    days = p.effective_lead_days() if p is not None else 0
    return int(days or org_lead or 0)


# ── Заказ целиком ────────────────────────────────────────────────────────────

def _ordered_by_base(order: ProductionOrder) -> dict[str, float]:
    out: dict[str, float] = {}
    for i in order.items:
        base = str(i.get("base_name") or "").strip()
        if base:
            out[base] = out.get(base, 0.0) + float(i.get("qty") or 0)
    return out


def _receipt_facts(order: ProductionOrder, rows: list[OrderReceipt]) -> dict:
    """Что известно о приходе по строкам приёмки.

    Источники не складываются (тот же приоритет, что у сверки приёмки,
    `api._received_by_base`). Пришедшее считается ПОСТРОЧНО по позициям заказа
    и не больше заказанного: лишнее по одной позиции не закрывает недостачу
    по другой, а позиция не из заказа в «пришло» не идёт. Заказ пришёл
    целиком, только если каждая позиция пришла полностью и источники не спорят.
    """
    from app import api as _api
    received = _api._received_by_base(rows)
    conflicts = {c["base_name"] for c in _api._source_conflicts(rows)}
    ordered = _ordered_by_base(order)
    got = 0.0
    complete = bool(ordered) and not conflicts
    for base, need in ordered.items():
        have = received.get(base, 0.0) if base not in conflicts else 0.0
        got += max(0.0, min(have, need))
        if have + 1e-9 < need:
            complete = False
    first = None
    for r in rows:
        if float(r.qty or 0) > 0 and r.at is not None:
            d = _d(r.at)
            if d is not None and (first is None or d < first):
                first = d
    return {"qty": round(got, 3), "complete": complete, "first_at": first,
            "conflict": bool(conflicts), "from_ms": any(r.source != "manual" for r in rows)}


def _order_total(order: ProductionOrder) -> float:
    return round(sum(float(i.get("qty") or 0) for i in order.items), 3)


def _items_out(order: ProductionOrder) -> list[dict]:
    out = []
    for i in order.items:
        sizes = {str(k): int(v or 0) for k, v in (i.get("sizes") or {}).items() if int(v or 0) > 0}
        out.append({"base_name": str(i.get("base_name") or ""),
                    "qty": float(i.get("qty") or 0), "sizes": sizes})
    return out


def _load(db: Session, org_id: int):
    orders = list(db.execute(
        select(ProductionOrder).where(
            ProductionOrder.org_id == org_id,
            ProductionOrder.status.in_(("sent", "received")),
        ).order_by(ProductionOrder.id)
    ).scalars())
    ids = [o.id for o in orders]
    batch = {o.id: o.cc_batch_id or "" for o in orders}
    receipts: dict[int, list[OrderReceipt]] = {}
    marks: dict[int, list[OrderStageMark]] = {}
    dues: dict[int, list[OrderDueDate]] = {}
    if ids:
        for r in db.execute(
            select(OrderReceipt).where(OrderReceipt.org_id == org_id,
                                       OrderReceipt.order_id.in_(ids))
            .order_by(OrderReceipt.id)
        ).scalars():
            receipts.setdefault(r.order_id, []).append(r)
        for m in db.execute(
            select(OrderStageMark).where(OrderStageMark.org_id == org_id,
                                         OrderStageMark.order_id.in_(ids))
            .order_by(OrderStageMark.at, OrderStageMark.id)
        ).scalars():
            if _d(m.at) is not None and _same_batch(m.order_batch, batch.get(m.order_id, "")):
                marks.setdefault(m.order_id, []).append(m)
        for d in db.execute(
            select(OrderDueDate).where(OrderDueDate.org_id == org_id,
                                       OrderDueDate.order_id.in_(ids))
            .order_by(OrderDueDate.id)
        ).scalars():
            if _d(d.due) is not None and _same_batch(d.order_batch, batch.get(d.order_id, "")):
                dues.setdefault(d.order_id, []).append(d)
    return orders, receipts, marks, dues


def _first_marks(stages: list[dict], marks: list[OrderStageMark]) -> dict[str, date]:
    keys = {s["key"] for s in stages}
    first: dict[str, date] = {}
    for m in marks:
        d = _d(m.at)
        if m.stage_key in keys and d is not None and (m.stage_key not in first or d < first[m.stage_key]):
            first[m.stage_key] = d
    return first


def _progress(order: ProductionOrder, stages: list[dict], marks: list[OrderStageMark],
              facts: dict) -> dict:
    keys = [s["key"] for s in stages]
    first_at = _first_marks(stages, marks)
    reached = -1
    for idx, k in enumerate(keys):
        if k in first_at:
            reached = idx
    last_key = keys[-1] if keys else ""
    last_marks = [m for m in marks if m.stage_key == last_key]
    total = _order_total(order)
    handed_qty = round(sum(float(m.qty or 0) for m in last_marks if m.qty is not None), 3)
    # Сдано всё: отметка «целиком» или сумма сдач не меньше заказанного.
    handed_all = any(m.qty is None for m in last_marks) or (total > 0 and handed_qty >= total - 1e-9)
    done = order.status == "received" or facts["complete"]
    if done:
        phase = "done"
    elif (keys and last_key in first_at) or facts["qty"] > 0:
        phase = "shipped"
    elif first_at:
        phase = "work"
    else:
        phase = "ordered"
    sent = _d(order.sent_at) or _d(order.created_at)
    points = [(SENT_POINT, sent)] + [(k, first_at[k]) for k in keys[:reached + 1] if k in first_at]
    nxt = None
    if not done and reached + 1 < len(stages):
        nxt = stages[reached + 1]
    elif not done and stages and last_key in first_at and not handed_all:
        nxt = stages[-1]   # сдача частями: последний этап отмечается ещё раз, с количеством
    return {"first_at": first_at, "reached": reached, "phase": phase, "done": done,
            "points": points, "sent": sent, "next": nxt,
            "handed_all": handed_all, "handed_qty": handed_qty, "total": total}


def _arrival(order: ProductionOrder, facts: dict) -> date | None:
    return facts["first_at"] or _d(order.received_at)


def _history(orders, receipts, marks, tracks, today: date) -> dict:
    """Дни «точка → прибытие» по цехам, только по ЗАКРЫТЫМ заказам за 365 дней.

    Незакрытый заказ с частичным приходом в историю не идёт: иначе он
    прогнозировал бы сам себя, а счётчик «по N заказам» расходился бы с
    таблицей скорости цехов.
    """
    samples: dict[int, dict[str, list[int]]] = {}
    for o in orders:
        facts = _receipt_facts(o, receipts.get(o.id, []))
        if not (o.status == "received" or facts["complete"]):
            continue
        arrived = _arrival(o, facts)
        if arrived is None or (today - arrived).days > HISTORY_DAYS:
            continue
        ch = _channel_of(o, tracks)
        bucket = samples.setdefault(ch, {})
        sent = _d(o.sent_at)
        if sent is not None and arrived >= sent:
            bucket.setdefault(SENT_POINT, []).append((arrived - sent).days)
        for k, d in _first_marks(tracks[ch]["stages"], marks.get(o.id, [])).items():
            if arrived >= d:
                bucket.setdefault(k, []).append((arrived - d).days)
    return samples


def _dates(order, prog, dues, samples, tracks, prods, org_lead, today) -> dict:
    """Срок (договорённость) и оценка по истории цеха. См. докстринг модуля."""
    ch = _channel_of(order, tracks)
    stage_names = {s["key"]: s["name"] for s in tracks[ch]["stages"]}
    plan = _d(order.eta_date)
    due_rows = [d for d in dues if _d(d.due) is not None]
    agreed = _d(due_rows[-1].due) if due_rows else None
    lead = _lead_days(order, prods, org_lead)
    if agreed is not None:
        expected, basis = agreed, "due"
    elif plan is not None:
        expected, basis = plan, "plan"
    elif prog["sent"] is not None and lead > 0:
        expected, basis = prog["sent"] + timedelta(days=lead), "lead"
    else:
        expected, basis = None, "none"

    est = None
    hist = samples.get(ch, {})
    # С самой поздней точки назад: если по последнему этапу истории мало,
    # берётся предыдущий, по которому она есть (отметка не ухудшает оценку).
    for key, at in reversed(prog["points"]):
        vals = hist.get(key, [])
        if at is not None and len(vals) >= MIN_HISTORY:
            est = {"date": (at + timedelta(days=_median_days(vals))).isoformat(), "n": len(vals),
                   "from": "отправки" if key == SENT_POINT else f"«{stage_names.get(key, key)}»"}
            break
    done = prog["done"]
    late = (today - expected).days if (expected is not None and not done and expected < today) else None
    risk = None
    est_overdue = False
    if est is not None and not done and not late:
        ed = date.fromisoformat(est["date"])
        if expected is not None and ed > expected:
            risk = (ed - expected).days
        if ed < today:
            est_overdue = True
    main = expected.isoformat() if expected else (est["date"] if est else None)
    return {
        "date": main, "basis": basis if expected else ("history" if est else "none"),
        "late_days": late, "plan_date": plan.isoformat() if plan else None,
        "due_date": agreed.isoformat() if agreed else None, "due_moves": max(0, len(due_rows) - 1),
        "estimate": est, "risk_days": risk, "estimate_overdue": est_overdue,
    }


def _order_out(order, stages, prog, facts, fc, channel_id, channel_name) -> dict:
    arrived = _arrival(order, facts)
    return {
        "id": order.id,
        "name": order.name,
        "status": order.status,
        "channel_id": channel_id,
        "channel_name": channel_name,
        "items": _items_out(order),
        "total_qty": prog["total"],
        "sent_at": prog["sent"].isoformat() if prog["sent"] else None,
        "phase": prog["phase"],
        "stages": [{"key": s["key"], "name": s["name"],
                    "at": prog["first_at"][s["key"]].isoformat() if s["key"] in prog["first_at"] else None}
                   for s in stages],
        "reached": prog["reached"],
        "next": prog["next"],
        "handed_qty": prog["handed_qty"],
        "handed_all": prog["handed_all"],
        "received_qty": facts["qty"],
        "receipt_conflict": facts["conflict"],
        "receipts_from_ms": facts["from_ms"],
        "arrived_at": arrived.isoformat() if arrived else None,
        "in_ms": bool(order.ms_doc_href),
        "forecast": fc,
    }


def _context(db: Session, org, today: date):
    from app import analytics
    prods = _productions(db, org.id)
    tracks, channels = org_tracks(db, org.id, prods)
    org_lead = int(analytics.extra_settings(org).get("lead_time_days") or 0)
    orders, receipts, marks, dues = _load(db, org.id)
    samples = _history(orders, receipts, marks, tracks, today)
    return prods, tracks, channels, org_lead, orders, receipts, marks, dues, samples


def _card(o, tracks, channels, prods, org_lead, receipts, marks, dues, samples, today):
    ch = _channel_of(o, tracks)
    stages = tracks[ch]["stages"]
    facts = _receipt_facts(o, receipts.get(o.id, []))
    prog = _progress(o, stages, marks.get(o.id, []), facts)
    fc = _dates(o, prog, dues.get(o.id, []), samples, tracks, prods, org_lead, today)
    name = next((c["name"] for c in channels if c["id"] == ch), "")
    return _order_out(o, stages, prog, facts, fc, ch, name), prog, facts


def board(db: Session, org, today: date | None = None) -> dict:
    """Всё для экрана «Производство» одним ответом."""
    today = today or date.today()
    prods, tracks, channels, org_lead, orders, receipts, marks, dues, samples = _context(db, org, today)
    out, used_channels = [], set()
    speed_acc: dict[int, dict] = {}
    for o in orders:
        card, prog, facts = _card(o, tracks, channels, prods, org_lead, receipts, marks, dues,
                                  samples, today)
        ch = card["channel_id"]
        acc = speed_acc.setdefault(ch, {"done": [], "on_time": [], "in_work": 0, "late": 0})
        arrived = _arrival(o, facts)
        if prog["done"]:
            sent = _d(o.sent_at)
            if arrived is not None and (today - arrived).days <= HISTORY_DAYS:
                if sent is not None and arrived >= sent:
                    acc["done"].append((arrived - sent).days)
                # «Вовремя» против первой договорённости: первый срок от цеха,
                # иначе плановая дата заказа.
                first_due = next((_d(d.due) for d in dues.get(o.id, []) if _d(d.due)), None)
                promise = first_due or _d(o.eta_date)
                if promise is not None:
                    acc["on_time"].append(arrived <= promise)
            # Принятое на доске 30 дней, дальше это история (она в скорости цеха).
            if arrived is None or (today - arrived).days > RECEIVED_SHOW_DAYS:
                continue
        else:
            acc["in_work"] += 1
            if card["forecast"]["late_days"]:
                acc["late"] += 1
        used_channels.add(ch)
        out.append(card)
    out.sort(key=lambda x: (x["forecast"]["date"] or "9999-99-99", x["id"]))

    speed = []
    for c in channels:
        acc = speed_acc.get(c["id"])
        if acc is None:
            continue
        hist = samples.get(c["id"], {})
        speed.append({
            "channel_id": c["id"], "channel_name": c["name"],
            "done": len(acc["done"]),
            "median_days": _median_days(acc["done"]) if acc["done"] else None,
            "on_time_share": (round(sum(acc["on_time"]) / len(acc["on_time"]), 2)
                              if acc["on_time"] else None),
            "on_time_n": len(acc["on_time"]),
            "stages": [{"key": s["key"], "name": s["name"],
                        "median_to_arrival": (_median_days(hist[s["key"]])
                                              if len(hist.get(s["key"], [])) >= MIN_HISTORY else None),
                        "n": len(hist.get(s["key"], []))}
                       for s in c["stages"]],
            "in_work": acc["in_work"], "late": acc["late"],
        })

    active = [x for x in out if x["phase"] != "done"]
    horizon = (today + timedelta(days=ARRIVING_WINDOW_DAYS)).isoformat()

    def _left(x):
        return max(0.0, x["total_qty"] - x["received_qty"])

    summary = {
        "in_work": len(active),
        "in_work_qty": round(sum(_left(x) for x in active)),
        "late": sum(1 for x in active if x["forecast"]["late_days"]),
        # Только то, что ждём в ближайшие 14 дней по сроку: опоздавшие сюда не
        # входят, у них нового срока нет, и обещать их нельзя.
        "waiting_qty": round(sum(
            _left(x) for x in active
            if not x["forecast"]["late_days"] and x["forecast"]["date"]
            and today.isoformat() <= x["forecast"]["date"] <= horizon)),
        "received_qty": round(sum(x["received_qty"] or x["total_qty"]
                                  for x in out if x["phase"] == "done")),
    }
    drafts = len(list(db.execute(
        select(ProductionOrder.id).where(ProductionOrder.org_id == org.id,
                                         ProductionOrder.status == "draft")
    ).scalars()))
    return {
        "today": today.isoformat(),
        "channels": [c for c in channels if c["id"] in used_channels or c["id"] != 0],
        "presets": [{"key": k, "label": PRESET_LABELS[k], "stages": PRESETS[k]} for k in PRESETS],
        "orders": out,
        "speed": speed,
        "summary": summary,
        "drafts": drafts,
        "history_days": HISTORY_DAYS,
        "min_history": MIN_HISTORY,
    }


def order_detail(db: Session, org, order_id: int, today: date | None = None) -> dict:
    """Окно заказа: то же, что на доске, плюс история отметок и сроков с авторами."""
    today = today or date.today()
    order = _own_order(db, org.id, order_id)
    if order.status not in ("sent", "received"):
        raise TrackingError("Заказ не найден", 404)
    prods, tracks, channels, org_lead, orders, receipts, marks, dues, samples = _context(db, org, today)
    card, _, _ = _card(order, tracks, channels, prods, org_lead, receipts, marks, dues, samples, today)
    my_marks = marks.get(order.id, [])
    my_dues = dues.get(order.id, [])
    ids = {m.created_by for m in my_marks if m.created_by} | {d.created_by for d in my_dues if d.created_by}
    authors = {}
    if ids:
        authors = {uid: (name or email or "") for uid, name, email in db.execute(
            select(User.id, User.name, User.email).where(User.id.in_(ids))).all()}
    card = dict(card)
    card["marks"] = [{
        "id": m.id, "stage_key": m.stage_key, "stage_name": m.stage_name,
        "at": _d(m.at).isoformat(), "qty": m.qty, "note": m.note,
        "by": authors.get(m.created_by, "") if m.created_by else "",
    } for m in my_marks]
    card["dues"] = [{
        "id": d.id, "due": _d(d.due).isoformat(), "note": d.note,
        "by": authors.get(d.created_by, "") if d.created_by else "",
        "at": d.created_at.date().isoformat() if d.created_at else None,
    } for d in my_dues]
    card["today"] = today.isoformat()
    return card


def _own_order(db: Session, org_id: int, order_id: int) -> ProductionOrder:
    order = db.get(ProductionOrder, order_id)
    if order is None or order.org_id != org_id:
        raise TrackingError("Заказ не найден", 404)
    return order


def _clean_note(note) -> str:
    note = re.sub(r"\s+", " ", str(note or "")).strip()
    if len(note) > NOTE_MAX:
        raise TrackingError(f"Заметка не длиннее {NOTE_MAX} знаков")
    return note


# ── Отметки этапов ───────────────────────────────────────────────────────────

def add_mark(db: Session, org, user_id: int | None, order_id: int, stage_key: str,
             at: str | None, qty, note: str, today: date | None = None) -> dict:
    today = today or date.today()
    order = _own_order(db, org.id, order_id)
    if order.status not in ("sent", "received"):
        raise TrackingError("Заказ ещё не отправлен в производство. Отправьте его на «Заказе».")
    tracks, _ = org_tracks(db, org.id)
    stages = tracks[_channel_of(order, tracks)]["stages"]
    stage = next((s for s in stages if s["key"] == stage_key), None)
    if stage is None:
        raise TrackingError("У этого цеха нет такого этапа. Обновите страницу.")
    # Дату присылает страница по календарю человека: у сервера свой пояс, и
    # ночью «сегодня» сервера бывает вчерашним днём пользователя.
    when = parse_day(at) if at else today
    if when > today + timedelta(days=1):
        raise TrackingError("Этап нельзя отметить будущей датой")
    floor = (_d(order.created_at) or today) - timedelta(days=HISTORY_DAYS)
    if when < floor:
        raise TrackingError("Дата слишком ранняя для этого заказа")
    note = _clean_note(note)
    is_last = stage["key"] == stages[-1]["key"]
    if qty is not None:
        if not is_last:
            raise TrackingError("Количество отмечается только на последнем этапе (сдача частями)")
        if isinstance(qty, bool) or not isinstance(qty, (int, float)):
            raise TrackingError("Количество задано неверно")
        if not math.isfinite(qty) or qty != int(qty):
            raise TrackingError("Количество целым числом штук")
        qty = int(qty)
        if not (0 < qty <= QTY_MAX):
            raise TrackingError(f"Количество от 1 до {QTY_MAX}")
    existing = [m for m in db.execute(
        select(OrderStageMark).where(OrderStageMark.org_id == org.id,
                                     OrderStageMark.order_id == order.id,
                                     OrderStageMark.stage_key == stage["key"])
        .order_by(OrderStageMark.id)
    ).scalars() if _same_batch(m.order_batch, order.cc_batch_id or "")]
    recent = datetime.utcnow() - timedelta(seconds=REPEAT_WINDOW_SEC)
    for m in existing:
        if (m.created_at and m.created_at >= recent and _d(m.at) == when
                and (m.qty or None) == (qty or None) and (m.note or "") == note):
            return {"ok": True, "id": m.id, "repeat": True}
    if existing:
        dates = [d for d in (_d(m.at) for m in existing) if d]
        if not is_last:
            when_txt = f" {min(dates):%d.%m.%Y}" if dates else ""
            raise TrackingError(
                f"«{stage['name']}» уже отмечен{when_txt}. Чтобы поменять дату, "
                "отмените прежнюю отметку.", 409)
        if any(m.qty is None for m in existing):
            raise TrackingError(
                f"«{stage['name']}» уже отмечен целиком. Чтобы записать сдачу частями, "
                "отмените отметку «целиком».", 409)
        if qty is None:
            raise TrackingError("Для следующей сдачи укажите, сколько штук сдали", 422)
    mark = OrderStageMark(
        org_id=org.id, order_id=order.id, order_batch=order.cc_batch_id or "",
        stage_key=stage["key"], stage_name=stage["name"], at=when, qty=qty,
        note=note, created_by=user_id,
    )
    db.add(mark)
    db.flush()
    warn = ""
    if is_last and qty is not None:
        handed = sum(float(m.qty or 0) for m in existing if m.qty is not None) + qty
        total = _order_total(order)
        if total > 0 and handed > total + 1e-9:
            warn = f"Сдано больше заказанного: {int(handed)} из {int(total)} шт"
    return {"ok": True, "id": mark.id, "warning": warn}


def delete_mark(db: Session, org_id: int, mark_id: int) -> dict:
    mark = db.get(OrderStageMark, mark_id)
    if mark is None or mark.org_id != org_id:
        raise TrackingError("Отметка не найдена", 404)
    order = db.get(ProductionOrder, mark.order_id)
    if order is None or order.org_id != org_id or not _same_batch(mark.order_batch, order.cc_batch_id or ""):
        raise TrackingError("Отметка не найдена", 404)
    db.delete(mark)
    return {"ok": True, "order_id": order.id}


# ── Срок от цеха ─────────────────────────────────────────────────────────────

def add_due(db: Session, org, user_id: int | None, order_id: int, due: str, note: str,
            today: date | None = None) -> dict:
    """Записать срок, который назвал цех. Новый срок не стирает прежний."""
    today = today or date.today()
    order = _own_order(db, org.id, order_id)
    if order.status != "sent":
        raise TrackingError("Срок записывается у заказа в производстве")
    when = parse_day(due, "Срок")
    sent = _d(order.sent_at) or _d(order.created_at) or today
    if when < sent:
        raise TrackingError("Срок не может быть раньше отправки заказа в производство")
    if when > today + timedelta(days=DUE_AHEAD_DAYS):
        raise TrackingError("Срок дальше двух лет, проверьте дату")
    note = _clean_note(note)
    rows = [d for d in db.execute(
        select(OrderDueDate).where(OrderDueDate.org_id == org.id, OrderDueDate.order_id == order.id)
        .order_by(OrderDueDate.id)
    ).scalars() if _same_batch(d.order_batch, order.cc_batch_id or "")]
    if rows and _d(rows[-1].due) == when and (rows[-1].note or "") == note:
        return {"ok": True, "id": rows[-1].id, "repeat": True}
    row = OrderDueDate(org_id=org.id, order_id=order.id, order_batch=order.cc_batch_id or "",
                       due=when, note=note, created_by=user_id)
    db.add(row)
    db.flush()
    return {"ok": True, "id": row.id}


def delete_due(db: Session, org_id: int, due_id: int) -> dict:
    row = db.get(OrderDueDate, due_id)
    if row is None or row.org_id != org_id:
        raise TrackingError("Срок не найден", 404)
    order = db.get(ProductionOrder, row.order_id)
    if order is None or order.org_id != org_id or not _same_batch(row.order_batch, order.cc_batch_id or ""):
        raise TrackingError("Срок не найден", 404)
    db.delete(row)
    return {"ok": True, "order_id": order.id}
