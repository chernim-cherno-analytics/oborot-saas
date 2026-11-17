"""PROD-1 «Производство»: где каждый запуск и когда он будет на складе.

Решение Влада 17.11.2026 (спецификация docs/SPEC_PRODUCTION_TRACKING.md):
запуск это заказ на производство (`ProductionOrder`), отдельной сущности
«партия» нет. Слой только ЧИТАЕТ заказы и приёмки и пишет свои отметки
этапов. «Едет», рекомендации, бюджет, плановая дата и статус заказа здесь не
меняются ни при каких действиях, формулы D-35 не затронуты.

Дата «на складе» считается детерминированно, без модели:
  1) по истории цеха: дата последней достигнутой точки + медиана дней от этой
     точки до прибытия у прошлых запусков того же цеха (не меньше 2 запусков
     за 365 дней);
  2) иначе плановая дата прихода заказа;
  3) иначе дата отправки + срок цеха.
Прошедшая дата без прибытия не сдвигается: показывается «опаздывает на N дн».
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
SENT_POINT = "_sent"
# Двойной тап или повтор запроса: та же отметка за эти секунды не пишется второй раз.
REPEAT_WINDOW_SEC = 15


class TrackingError(ValueError):
    """Ошибка ввода, понятная человеку. `status` уходит в HTTP-ответ."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


# ── Этапы цеха ───────────────────────────────────────────────────────────────

def _preset_stages(preset: str) -> list[dict]:
    names = PRESETS.get(preset) or PRESETS["sewing"]
    return [{"key": f"s{i + 1}", "name": n} for i, n in enumerate(names)]


def default_preset(production: Production | None) -> str:
    """Шаблон этапов для цеха, у которого их ещё не задавали.

    Не догадка о цехе, а чтение его же настроек: если в условиях канала
    (`Production.stages`) один этап на весь срок, это фабрика «под ключ»;
    если этапов несколько (ткань, потом пошив) или их нет, это пошив.
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


def org_tracks(db: Session, org_id: int) -> tuple[dict, list[dict]]:
    """Этапы всех цехов организации.

    Возвращает (tracks, channels): tracks[production_id или 0] =
    {"preset", "stages"}; channels это список цехов для экрана, включая
    «Без цеха» (id 0), если такие заказы бывают. Строки в базе нет, значит
    этапы по шаблону (`default_preset`), и они ничего не записывают.
    """
    prods = list(db.execute(
        select(Production).where(Production.org_id == org_id).order_by(Production.id)
    ).scalars())
    rows = {
        (r.production_id or 0): r
        for r in db.execute(
            select(ProductionTrack).where(ProductionTrack.org_id == org_id)
            .order_by(ProductionTrack.id)
        ).scalars()
    }
    tracks: dict[int, dict] = {}
    channels: list[dict] = []
    for p in [None, *prods]:
        pid = p.id if p is not None else 0
        row = rows.get(pid)
        stages = []
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


def set_track(db: Session, org_id: int, production_id: int, preset: str,
              stages_in: list) -> dict:
    """Сохранить этапы цеха. 0 = заказы без цеха.

    Ключи существующих этапов сохраняются (переименование не теряет
    отметки), новым выдаются свободные. Этап, по которому уже есть отметки,
    удалить нельзя: история перестала бы читаться.
    """
    if production_id:
        p = db.get(Production, production_id)
        if p is None or p.org_id != org_id:
            raise TrackingError("Цех не найден", 404)
    if preset not in PRESET_LABELS:
        raise TrackingError("Неизвестный шаблон этапов")
    tracks, _ = org_tracks(db, org_id)
    current = tracks.get(production_id) or {"stages": []}
    if preset in PRESETS and not stages_in:
        names = PRESETS[preset]
        stages_in = [{"name": n} for n in names]
    if not isinstance(stages_in, list) or not stages_in:
        raise TrackingError("Нужен хотя бы один этап")
    if len(stages_in) > MAX_STAGES:
        raise TrackingError(f"Этапов не больше {MAX_STAGES}")
    used = {s["key"] for s in current["stages"]}
    known_keys = set(used)
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
        marked = _keys_with_marks(db, org_id, production_id, removed)
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
        ).order_by(ProductionTrack.id).limit(1)
    ).scalar_one_or_none()
    if row is None:
        row = ProductionTrack(org_id=org_id, production_id=production_id or None)
        db.add(row)
    row.preset = preset
    row.stages_json = json.dumps(result, ensure_ascii=False)
    row.updated_at = datetime.utcnow()
    return {"preset": preset, "stages": result}


def _keys_with_marks(db: Session, org_id: int, production_id: int, keys: set[str]) -> set[str]:
    q = (select(OrderStageMark.stage_key, OrderStageMark.order_batch, ProductionOrder.cc_batch_id)
         .join(ProductionOrder, ProductionOrder.id == OrderStageMark.order_id)
         .where(OrderStageMark.org_id == org_id, ProductionOrder.org_id == org_id,
                OrderStageMark.stage_key.in_(keys)))
    if production_id:
        q = q.where(ProductionOrder.production_id == production_id)
    else:
        q = q.where(ProductionOrder.production_id.is_(None))
    return {k for k, mb, ob in db.execute(q).all() if _same_batch(mb, ob)}


def _same_batch(mark_batch: str, order_batch: str) -> bool:
    """Отметка принадлежит заказу, только если совпадает и партия (D-50).

    Пустой идентификатор у отметки или у заказа бывает только у строк,
    созданных откатившимся старым кодом; тогда сверяемся по id, как раньше.
    """
    return not mark_batch or not order_batch or mark_batch == order_batch


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


def _median_days(values: list[int]) -> int:
    return int(math.ceil(statistics.median(values)))


def _channel_of(order: ProductionOrder, tracks: dict) -> int:
    pid = order.production_id or 0
    return pid if pid in tracks else 0


def _lead_days(order: ProductionOrder, prods: dict, org_lead: int) -> int:
    p = prods.get(order.production_id or 0)
    days = p.effective_lead_days() if p is not None else 0
    return int(days or org_lead or 0)


# ── Заказ целиком ────────────────────────────────────────────────────────────

def _receipt_facts(rows: list[OrderReceipt]) -> dict:
    """Что известно о приходе по строкам приёмки.

    Источники не складываются (тот же приоритет, что у сверки приёмки,
    `api._received_by_base`): два свидетельства об одном приходе это не
    двойная поставка. Позиции со спором источников в сумму не входят, а
    флаг `conflict` говорит, что разбор нужен человеку.
    """
    from app import api as _api
    received = _api._received_by_base(rows)
    conflicts = {c["base_name"] for c in _api._source_conflicts(rows)}
    total = sum(v for b, v in received.items() if b not in conflicts and v > 0)
    first = None
    for r in rows:
        if float(r.qty or 0) > 0 and r.at is not None:
            d = _d(r.at)
            if d is not None and (first is None or d < first):
                first = d
    return {"qty": round(total, 3), "first_at": first, "conflict": bool(conflicts),
            "from_ms": any(r.source != "manual" for r in rows)}


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
    receipts: dict[int, list[OrderReceipt]] = {}
    marks: dict[int, list[OrderStageMark]] = {}
    if ids:
        for r in db.execute(
            select(OrderReceipt).where(OrderReceipt.org_id == org_id,
                                       OrderReceipt.order_id.in_(ids))
            .order_by(OrderReceipt.id)
        ).scalars():
            receipts.setdefault(r.order_id, []).append(r)
        batch = {o.id: o.cc_batch_id or "" for o in orders}
        for m in db.execute(
            select(OrderStageMark).where(OrderStageMark.org_id == org_id,
                                         OrderStageMark.order_id.in_(ids))
            .order_by(OrderStageMark.at, OrderStageMark.id)
        ).scalars():
            if m.at is not None and _same_batch(m.order_batch, batch.get(m.order_id, "")):
                marks.setdefault(m.order_id, []).append(m)
    return orders, receipts, marks


def _progress(order: ProductionOrder, stages: list[dict], marks: list[OrderStageMark],
              facts: dict) -> dict:
    keys = [s["key"] for s in stages]
    first_at: dict[str, date] = {}
    for m in marks:
        if m.stage_key in keys:
            d = _d(m.at)
            if d is not None and (m.stage_key not in first_at or d < first_at[m.stage_key]):
                first_at[m.stage_key] = d
    reached = -1
    for idx, k in enumerate(keys):
        if k in first_at:
            reached = idx
    last_key = keys[-1] if keys else ""
    last_marks = [m for m in marks if m.stage_key == last_key]
    handed_all = any(m.qty is None for m in last_marks)
    handed_qty = round(sum(float(m.qty or 0) for m in last_marks if m.qty is not None), 3)
    total = _order_total(order)
    done = order.status == "received" or (total > 0 and not facts["conflict"]
                                          and facts["qty"] >= total - 1e-9)
    if done:
        phase = "done"
    elif (keys and last_key in first_at) or facts["qty"] > 0:
        phase = "shipped"
    elif first_at:
        phase = "work"
    else:
        phase = "ordered"
    sent = _d(order.sent_at) or _d(order.created_at)
    if reached >= 0:
        point_key, point_at = keys[reached], first_at[keys[reached]]
    else:
        point_key, point_at = SENT_POINT, sent
    nxt = None
    if not done and reached + 1 < len(stages):
        nxt = stages[reached + 1]
    elif not done and stages and last_key in first_at and not handed_all:
        # Сдача частями: последний этап можно отмечать ещё раз, с количеством.
        nxt = stages[-1]
    return {
        "first_at": first_at, "reached": reached, "phase": phase, "done": done,
        "point_key": point_key, "point_at": point_at, "sent": sent, "next": nxt,
        "handed_all": handed_all, "handed_qty": handed_qty, "total": total,
    }


def _arrival(order: ProductionOrder, facts: dict) -> date | None:
    return facts["first_at"] or _d(order.received_at)


def _history(orders, receipts, marks, tracks, today: date) -> dict:
    """Медианы «точка → прибытие» по цехам из прошлых запусков."""
    samples: dict[int, dict[str, list[int]]] = {}
    for o in orders:
        facts = _receipt_facts(receipts.get(o.id, []))
        arrived = _arrival(o, facts)
        if arrived is None or (today - arrived).days > HISTORY_DAYS:
            continue
        ch = _channel_of(o, tracks)
        bucket = samples.setdefault(ch, {})
        sent = _d(o.sent_at)
        if sent is not None and arrived >= sent:
            bucket.setdefault(SENT_POINT, []).append((arrived - sent).days)
        keys = {s["key"] for s in tracks[ch]["stages"]}
        first: dict[str, date] = {}
        for m in marks.get(o.id, []):
            d = _d(m.at)
            if m.stage_key in keys and d is not None and (m.stage_key not in first or d < first[m.stage_key]):
                first[m.stage_key] = d
        for k, d in first.items():
            if arrived >= d:
                bucket.setdefault(k, []).append((arrived - d).days)
    return samples


def _forecast(order, prog, samples, tracks, prods, org_lead, today) -> dict:
    ch = _channel_of(order, tracks)
    plan = _d(order.eta_date)
    hist = samples.get(ch, {}).get(prog["point_key"], [])
    basis, n, when = "none", 0, None
    if prog["point_at"] is not None and len(hist) >= MIN_HISTORY:
        when = prog["point_at"] + timedelta(days=_median_days(hist))
        basis, n = "history", len(hist)
    elif plan is not None:
        when, basis = plan, "plan"
    elif prog["sent"] is not None:
        lead = _lead_days(order, prods, org_lead)
        if lead > 0:
            when, basis = prog["sent"] + timedelta(days=lead), "lead"
    late = None
    if when is not None and not prog["done"] and when < today:
        late = (today - when).days
    vs_plan = (when - plan).days if (when is not None and plan is not None and basis != "plan") else None
    return {"date": when.isoformat() if when else None, "basis": basis, "n": n,
            "late_days": late, "vs_plan_days": vs_plan,
            "plan_date": plan.isoformat() if plan else None}


def _order_out(order, stages, prog, facts, fc, channel_name, marks, authors=None) -> dict:
    arrived = _arrival(order, facts)
    out = {
        "id": order.id,
        "name": order.name,
        "status": order.status,
        "channel_id": order.production_id or 0,
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
    if authors is not None:
        out["marks"] = [{
            "id": m.id, "stage_key": m.stage_key, "stage_name": m.stage_name,
            "at": _d(m.at).isoformat() if _d(m.at) else None,
            "qty": m.qty, "note": m.note,
            "by": authors.get(m.created_by, "") if m.created_by else "",
            "created_at": m.created_at.isoformat() if m.created_at else None,
        } for m in marks]
    return out


def board(db: Session, org, today: date | None = None) -> dict:
    """Всё для экрана «Производство» одним ответом."""
    from app import analytics
    today = today or date.today()
    tracks, channels = org_tracks(db, org.id)
    prods = {p.id: p for p in db.execute(
        select(Production).where(Production.org_id == org.id)).scalars()}
    org_lead = int(analytics.extra_settings(org).get("lead_time_days") or 0)
    orders, receipts, marks = _load(db, org.id)
    samples = _history(orders, receipts, marks, tracks, today)
    names = {c["id"]: c["name"] for c in channels}
    out, used_channels = [], set()
    speed_acc: dict[int, dict] = {}
    for o in orders:
        ch = _channel_of(o, tracks)
        stages = tracks[ch]["stages"]
        facts = _receipt_facts(receipts.get(o.id, []))
        prog = _progress(o, stages, marks.get(o.id, []), facts)
        fc = _forecast(o, prog, samples, tracks, prods, org_lead, today)
        arrived = _arrival(o, facts)
        acc = speed_acc.setdefault(ch, {"done": [], "on_time": [], "in_work": 0, "late": 0})
        if prog["done"]:
            sent = _d(o.sent_at)
            if arrived is not None and (today - arrived).days <= HISTORY_DAYS:
                if sent is not None and arrived >= sent:
                    acc["done"].append((arrived - sent).days)
                plan = _d(o.eta_date)
                if plan is not None:
                    acc["on_time"].append(arrived <= plan)
            # Принятое показываем 30 дней, дальше это история (она в скорости цеха).
            if arrived is None or (today - arrived).days > RECEIVED_SHOW_DAYS:
                continue
        else:
            acc["in_work"] += 1
            if fc["late_days"]:
                acc["late"] += 1
        used_channels.add(ch)
        out.append(_order_out(o, stages, prog, facts, fc, names.get(ch, ""), []))
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
    horizon = today + timedelta(days=ARRIVING_WINDOW_DAYS)

    def _left(x):
        return max(0.0, x["total_qty"] - x["received_qty"])

    summary = {
        "in_work": len(active),
        "in_work_qty": round(sum(_left(x) for x in active)),
        "late": sum(1 for x in active if x["forecast"]["late_days"]),
        "arriving_qty": round(sum(
            _left(x) for x in active
            if x["forecast"]["date"] and x["forecast"]["date"] <= horizon.isoformat())),
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
    """Окно запуска: то же, что на доске, плюс история отметок с авторами."""
    order = _own_order(db, org.id, order_id)
    data = board(db, org, today)
    card = next((x for x in data["orders"] if x["id"] == order.id), None)
    tracks, channels = org_tracks(db, org.id)
    ch = _channel_of(order, tracks)
    marks = [m for m in db.execute(
        select(OrderStageMark).where(OrderStageMark.org_id == org.id,
                                     OrderStageMark.order_id == order.id)
        .order_by(OrderStageMark.at, OrderStageMark.id)
    ).scalars() if m.at is not None and _same_batch(m.order_batch, order.cc_batch_id or "")]
    ids = {m.created_by for m in marks if m.created_by}
    authors = {}
    if ids:
        authors = {uid: (name or email or "") for uid, name, email in db.execute(
            select(User.id, User.name, User.email).where(User.id.in_(ids))).all()}
    if card is None:
        # Принят давно (вне 30 дней доски): собираем карточку отдельно.
        _, receipts, mk = _load(db, org.id)
        facts = _receipt_facts(receipts.get(order.id, []))
        prog = _progress(order, tracks[ch]["stages"], mk.get(order.id, []), facts)
        fc = {"date": None, "basis": "none", "n": 0, "late_days": None,
              "vs_plan_days": None, "plan_date": order.eta_date}
        card = _order_out(order, tracks[ch]["stages"], prog, facts, fc,
                          next((c["name"] for c in channels if c["id"] == ch), ""), [])
    card = dict(card)
    card["marks"] = _order_out(order, [], {"total": 0, "sent": None, "phase": "", "first_at": {},
                                          "reached": -1, "next": None, "handed_qty": 0,
                                          "handed_all": False},
                               {"qty": 0, "conflict": False, "from_ms": False, "first_at": None},
                               {}, "", marks, authors)["marks"]
    card["today"] = data["today"]
    return card


def _own_order(db: Session, org_id: int, order_id: int) -> ProductionOrder:
    order = db.get(ProductionOrder, order_id)
    if order is None or order.org_id != org_id:
        raise TrackingError("Заказ не найден", 404)
    return order


# ── Отметки ──────────────────────────────────────────────────────────────────

def add_mark(db: Session, org, user_id: int | None, order_id: int, stage_key: str,
             at: str | None, qty: float | None, note: str, today: date | None = None) -> dict:
    today = today or date.today()
    order = _own_order(db, org.id, order_id)
    if order.status not in ("sent", "received"):
        raise TrackingError("Заказ ещё не отправлен в производство. Отправьте его на «Заказе».")
    tracks, _ = org_tracks(db, org.id)
    stages = tracks[_channel_of(order, tracks)]["stages"]
    stage = next((s for s in stages if s["key"] == stage_key), None)
    if stage is None:
        raise TrackingError("У этого цеха нет такого этапа. Обновите страницу.")
    when = _d(at) if at else today
    if when is None:
        raise TrackingError("Дата задана неверно")
    if when > today + timedelta(days=1):
        raise TrackingError("Этап нельзя отметить будущей датой")
    floor = (_d(order.created_at) or today) - timedelta(days=HISTORY_DAYS)
    if when < floor:
        raise TrackingError("Дата слишком ранняя для этого заказа")
    note = re.sub(r"\s+", " ", str(note or "")).strip()
    if len(note) > NOTE_MAX:
        raise TrackingError(f"Заметка не длиннее {NOTE_MAX} знаков")
    is_last = stage["key"] == stages[-1]["key"]
    if qty is not None:
        if not is_last:
            raise TrackingError("Количество отмечается только на последнем этапе (сдача частями)")
        try:
            qty = float(qty)
        except (TypeError, ValueError):
            raise TrackingError("Количество задано неверно")
        if not (qty > 0) or qty > QTY_MAX or not math.isfinite(qty):
            raise TrackingError(f"Количество от 1 до {QTY_MAX}")
        if abs(qty - round(qty)) > 1e-9:
            raise TrackingError("Количество целым числом штук")
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
        if not is_last:
            first = min(_d(m.at) for m in existing if _d(m.at))
            raise TrackingError(
                f"«{stage['name']}» уже отмечен {first:%d.%m.%Y}. Чтобы поменять дату, "
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
    return {"ok": True, "id": mark.id}


def delete_mark(db: Session, org_id: int, mark_id: int) -> dict:
    mark = db.get(OrderStageMark, mark_id)
    if mark is None or mark.org_id != org_id:
        raise TrackingError("Отметка не найдена", 404)
    order = db.get(ProductionOrder, mark.order_id)
    if order is None or order.org_id != org_id or not _same_batch(mark.order_batch, order.cc_batch_id or ""):
        raise TrackingError("Отметка не найдена", 404)
    db.delete(mark)
    return {"ok": True, "order_id": order.id}
