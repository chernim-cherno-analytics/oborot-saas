"""JSON API. Все эндпоинты — под сессией; данные строго текущей организации."""
import hashlib
import json
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import analytics, lessons, ms_writeback, order_planner
from app.auth import AuthContext, require_auth_api, require_owner_api
from app.crypto import encrypt_token
from app.db import get_db
from app.demo_seed import seed_demo
from app.models import (
    Connection,
    Membership,
    OrderedQty,
    Org,
    Product,
    Production,
    ProductionAssign,
    ProductionOrder,
    StockDay,
    User,
    Warehouse,
)

router = APIRouter(prefix="/api")

# Аддитивная мини-миграция «Обучения»: у баз, созданных до фичи, нет таблиц
# user_lessons и user_prefs. Создаём их на импорте модуля, не дожидаясь
# init_db на старте (паттерн — app/routes_connect.py): create_all с
# checkfirst идемпотентен, свежую БД это не трогает.
lessons.ensure_schema()

# Границы для числовых id в пути: без них слишком большое число (например,
# /api/orders/999999999999999999999) валит SQLite (OverflowError при попытке
# положить его в INTEGER-колонку) вместо аккуратного 422.
# ВАЖНО: один и тот же объект Path(...) нельзя переиспользовать для нескольких
# параметров — FastAPI мутирует его .alias при разборе сигнатуры, и все
# параметры, использующие общий экземпляр, получают alias первого из них
# (в нашем случае все стали бы «order_id», ломая /warehouses/{warehouse_id}
# и /productions/{pid}). Поэтому — фабрика, отдельный экземпляр на каждый вызов.
def _id_path() -> int:
    return Path(ge=1, le=2_147_483_647)


# ── Аналитика ────────────────────────────────────────────────────────────────

@router.get("/summary")
def api_summary(ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)):
    return analytics.build_summary(analytics.get_snapshot(db, ctx.org))


@router.get("/replenish")
def api_replenish(ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)):
    data = analytics.build_replenish(analytics.get_snapshot(db, ctx.org))
    apply_production_rules(db, ctx.org.id, data)
    # Для пустого состояния: какие заказы «В производстве» закрывают потребность.
    sent = db.execute(
        select(ProductionOrder.id, ProductionOrder.name)
        .where(ProductionOrder.org_id == ctx.org.id, ProductionOrder.status == "sent")
        .order_by(ProductionOrder.created_at.desc())
    ).all()
    data["orders_in_production"] = [{"id": o.id, "name": o.name} for o in sent]
    return data


@router.get("/turnover")
def api_turnover(ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)):
    return analytics.build_turnover(analytics.get_snapshot(db, ctx.org))


@router.get("/stocks")
def api_stocks(ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)):
    return analytics.build_stocks(analytics.get_snapshot(db, ctx.org))


# ── Каталог ──────────────────────────────────────────────────────────────────

def _known_base_names(db: Session, org_id: int, names) -> set[str]:
    """Из присланных базовых имён — те, что реально есть в каталоге организации."""
    wanted = {str(n or "").strip() for n in names if str(n or "").strip()}
    if not wanted:
        return set()
    return set(db.execute(
        select(Product.base_name).where(
            Product.org_id == org_id, Product.base_name.in_(wanted)
        ).distinct()
    ).scalars())


def _require_known_base(db: Session, org_id: int, base_name: str) -> str:
    """Проверяет, что позиция есть в каталоге. Иначе — 404 вместо «ok»: раньше
    такие запросы отвечали успехом, а запись потом нигде не появлялась."""
    base = str(base_name or "").strip()
    if base and base in _known_base_names(db, org_id, [base]):
        return base
    raise HTTPException(
        status_code=404,
        detail=f"Товара «{base_name}» нет в вашем каталоге. Проверьте название "
               f"или дождитесь синхронизации со складом.",
    )


# ── Заказы на производство ───────────────────────────────────────────────────

class OrderItemIn(BaseModel):
    base_name: str
    qty: int = Field(ge=0, le=100_000)
    sizes: dict[str, int] = Field(default_factory=dict)
    cost: float = 0.0

    @field_validator("sizes")
    @classmethod
    def _sizes_sane(cls, v: dict[str, int]) -> dict[str, int]:
        for size, q in v.items():
            if q < 0:
                raise ValueError(f"Отрицательное количество в размере {size!r}")
            if q > 100_000:
                raise ValueError(f"Неправдоподобное количество в размере {size!r}")
        return v


class OrderIn(BaseModel):
    name: str = Field(default="", max_length=120)
    eta_date: str | None = None
    items: list[OrderItemIn]
    # «да, второй такой же заказ нужен» — осознанное повторение состава
    # в обход защиты от случайного дубля (см. api_create_order).
    allow_duplicate: bool = False

    @field_validator("eta_date")
    @classmethod
    def _eta_date_valid(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError:
            raise ValueError("Дата прихода должна быть в формате ГГГГ-ММ-ДД")
        return v


def _order_out(order: ProductionOrder) -> dict:
    items = order.items
    return {
        "id": order.id,
        "name": order.name,
        "created_at": order.created_at.isoformat() if order.created_at else None,
        "eta_date": order.eta_date,
        "status": order.status,
        "items": items,
        "positions": len(items),
        "total_qty": sum(int(i.get("qty") or 0) for i in items),
        "total_cost": round(sum(float(i.get("cost") or 0) * int(i.get("qty") or 0) for i in items)),
        "production_id": order.production_id,
        "created_by": order.created_by,
    }


# ── Открытые заказы и календарь денег ────────────────────────────────────────
# Аудит 22.08.2026: мастер считал заказ так, будто у организации нет других
# обязательств. На практике «у меня есть 200 000» — это ДО того, как вспомнили
# про два открытых заказа, по которым на следующей неделе платить остаток.
# Поэтому: список открытых заказов по каналу (плашка в мастере) и сводный
# календарь платежей ОРГАНИЗАЦИИ по неделям с накопительным сальдо.

OPEN_STATUSES = ("draft", "sent")
CASH_WEEKS = 16


def _order_stages(db: Session, order: ProductionOrder, settings: dict) -> list[dict]:
    """Этапы канала заказа (или один этап на общий срок производства)."""
    raw = None
    if order.production_id:
        prod = db.get(Production, order.production_id)
        # Сверка организации обязательна: это единственное место, где
        # производство берётся по id из ЧУЖИХ данных (id лежит в заказе, а туда
        # попадает из брифа плана). Без проверки условия подрядчика другой
        # организации утекли бы в календарь платежей.
        if prod is not None and prod.org_id == order.org_id:
            raw = prod.stages
    return order_planner.normalize_stages(raw, int(settings.get("lead_time_days") or 45))


def _order_payments(db: Session, order: ProductionOrder, settings: dict) -> list[dict]:
    """Календарь платежей по уже существующему заказу."""
    cost = sum(float(i.get("cost") or 0) * int(i.get("qty") or 0) for i in order.items)
    if cost <= 0:
        return []
    started = (order.created_at or datetime.utcnow()).date()
    return order_planner.payment_plan(started, _order_stages(db, order, settings), cost)


def _open_orders(db: Session, org_id: int) -> list[ProductionOrder]:
    return db.execute(
        select(ProductionOrder).where(
            ProductionOrder.org_id == org_id,
            ProductionOrder.status.in_(OPEN_STATUSES),
        ).order_by(ProductionOrder.created_at.desc())
    ).scalars().all()


@router.get("/orders/open")
def api_orders_open(
    production_id: int | None = None,
    ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db),
):
    """Что уже заказано и не закрыто — для плашки и колонки «Едет» в мастере.

    production_id — фильтр по каналу; без него отдаём все открытые заказы.
    by_base — сколько штук каждой модели уже лежит в открытых заказах: мастер
    помечает такие строки, чтобы не заказать то же самое второй раз.
    """
    settings = analytics.extra_settings(ctx.org)
    rows, out, by_base, total = _open_orders(db, ctx.org.id), [], {}, 0.0
    for o in rows:
        if production_id and (o.production_id or 0) != int(production_id):
            continue
        pays = _order_payments(db, o, settings)
        left = sum(p["amount"] for p in pays if p["date"] >= date.today().isoformat())
        total += left
        for i in o.items:
            base = str(i.get("base_name") or "")
            if base:
                by_base[base] = by_base.get(base, 0) + int(i.get("qty") or 0)
        out.append({**_order_out(o), "payments": pays, "left_to_pay": round(left)})
    return {"orders": out, "by_base": by_base, "left_to_pay": round(total),
            "count": len(out), "today": date.today().isoformat()}


def _week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


@router.get("/cash-calendar")
def api_cash_calendar(
    ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)
):
    """Платежи по всем открытым заказам организации, по неделям.

    Текущий (ещё не созданный) заказ мастер добавляет в эту же сетку на клиенте —
    поэтому здесь только то, что уже есть в базе.
    """
    settings = analytics.extra_settings(ctx.org)
    today = date.today()
    start = _week_start(today)
    weeks = [{"start": (start + timedelta(days=7 * i)).isoformat(),
              "end": (start + timedelta(days=7 * i + 6)).isoformat(),
              "amount": 0, "items": []} for i in range(CASH_WEEKS)]
    last = start + timedelta(days=7 * CASH_WEEKS - 1)
    overdue = {"amount": 0, "items": []}
    for o in _open_orders(db, ctx.org.id):
        for p in _order_payments(db, o, settings):
            d = date.fromisoformat(p["date"])
            item = {"order_id": o.id, "name": o.name, "label": p["label"],
                    "date": p["date"], "amount": p["amount"]}
            if d < start:
                overdue["amount"] += p["amount"]
                overdue["items"].append(item)
                continue
            if d > last:
                continue
            w = weeks[(d - start).days // 7]
            w["amount"] += p["amount"]
            w["items"].append(item)
    # Накопительное считаем ТОЛЬКО по будущим деньгам: платежи с прошедшими
    # датами уже случились (или заказ давно висит черновиком) — если добавить
    # их в сальдо, календарь на боевых данных открывается цифрой в 6 млн и
    # читается как «вы должны это на следующей неделе».
    running = 0
    for w in weeks:
        running += w["amount"]
        w["cumulative"] = running
    return {"weeks": weeks, "overdue": overdue, "total": running,
            "week_start": start.isoformat(), "today": today.isoformat()}


@router.get("/orders")
def api_orders(ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)):
    orders = db.execute(
        select(ProductionOrder)
        .where(ProductionOrder.org_id == ctx.org.id)
        .order_by(ProductionOrder.created_at.desc())
    ).scalars().all()
    # Имена авторов одним запросом: в списке заказов «кто это создал» —
    # первый вопрос, когда с системой работает больше одного человека.
    ids = {o.created_by for o in orders if o.created_by}
    authors = {}
    if ids:
        authors = {
            str(uid): (name or email or "")
            for uid, name, email in db.execute(
                select(User.id, User.name, User.email).where(User.id.in_(ids))
            ).all()
        }
    return {"orders": [_order_out(o) for o in orders], "authors": authors}


# Окно защиты от случайного повтора заказа. Двойной тап на телефоне, ретрай
# при дрожащей связи и повторная отправка формы укладываются в эти минуты, а
# осознанный второй такой же заказ человек делает не за две минуты — и если
# всё-таки делает, ему достаточно изменить название (см. ответ ниже).
ORDER_DEDUP_WINDOW_SEC = 120

_DUPLICATE_ORDER_MESSAGE = (
    "Такой же заказ уже создан только что — показываем его, чтобы на производство "
    "не ушёл дубль. Если второй такой заказ нужен на самом деле, измените название "
    "заказа и отправьте снова."
)


def _order_fingerprint(name: str, eta_date: str | None, items: list[dict]) -> str:
    """Отпечаток заказа: название, срок и позиции с количествами по размерам.

    Себестоимость в отпечаток не входит — она подставляется из справочника и
    к вопросу «это тот же самый заказ или другой» отношения не имеет.
    """
    rows = sorted(
        [
            str(i.get("base_name") or ""),
            int(i.get("qty") or 0),
            sorted((str(k), int(v or 0)) for k, v in (i.get("sizes") or {}).items()),
        ]
        for i in items
    )
    raw = json.dumps([str(name or ""), eta_date or "", rows], ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _find_twin_order(db: Session, org_id: int, fingerprint: str, before_id: int | None = None):
    """Ищет такой же заказ, созданный в окне защиты от повтора.

    before_id — искать только среди более ранних заказов: так два запроса,
    пришедшие одновременно, договариваются без блокировок (остаётся заказ с
    меньшим id).

    Принятый на склад (received) заказ в поиск не входит: это уже история,
    и повтор того же состава после приёмки почти наверняка значит «нужен
    новый такой же заказ», а не «я по ошибке продублировал форму». Раньше
    склейка с received-заказом отдавала клиенту уже принятый заказ, на
    который дальше пытались перевести статус «В производстве» — переход
    received → sent запрещён, и человек получал 422 не по делу.
    """
    since = datetime.utcnow() - timedelta(seconds=ORDER_DEDUP_WINDOW_SEC)
    q = select(ProductionOrder).where(
        ProductionOrder.org_id == org_id,
        ProductionOrder.created_at >= since,
        ProductionOrder.status != "received",
    )
    if before_id is not None:
        q = q.where(ProductionOrder.id < before_id)
    for o in db.execute(q.order_by(ProductionOrder.id)).scalars():
        if _order_fingerprint(o.name, o.eta_date, o.items) == fingerprint:
            return o
    return None


@router.post("/orders")
def api_create_order(
    body: OrderIn, ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)
):
    """Создаёт заказ на производство. Повторная отправка того же заказа в
    течение пары минут не плодит дубли: возвращается уже созданный заказ.

    Осознанный второй такой же заказ делается либо другим названием, либо
    полем allow_duplicate=true в теле запроса.
    """
    items = [i for i in body.items if i.qty > 0]
    if not items:
        raise HTTPException(status_code=422, detail="В заказе нет позиций с количеством > 0")
    # Позиций, которых нет в каталоге организации, в заказе быть не может —
    # иначе создаётся «призрачная» позиция, для которой ниже неоткуда взять
    # свою себестоимость, и сервер был вынужден верить присланной клиентом.
    known = _known_base_names(db, ctx.org.id, [i.base_name for i in items])
    unknown = sorted({i.base_name for i in items if i.base_name not in known})
    if unknown:
        raise HTTPException(
            status_code=404,
            detail="Такого товара нет в вашем каталоге: "
                   + ", ".join(f"«{b}»" for b in unknown)
                   + ". Проверьте название или дождитесь синхронизации со складом.",
        )
    name = body.name.strip() or f"Заказ от {datetime.now():%d.%m.%Y}"
    # Себестоимость всегда берём из БД, присланной клиентом не доверяем ни при
    # каких обстоятельствах (раньше для позиций вне каталога это правило не
    # действовало — теперь такие позиции отсеяны проверкой выше).
    cost_by_base = {
        p.base_name: float(p.cost_price or 0)
        for p in db.execute(
            select(Product).where(Product.org_id == ctx.org.id)
        ).scalars()
    }
    payload = []
    for i in items:
        d = i.model_dump()
        d["cost"] = cost_by_base.get(i.base_name, 0.0)
        payload.append(d)
    fingerprint = _order_fingerprint(name, body.eta_date, payload)
    if not body.allow_duplicate:
        twin = _find_twin_order(db, ctx.org.id, fingerprint)
        if twin is not None:
            return {"ok": True, "id": twin.id, "status": twin.status,
                    "duplicate": True, "message": _DUPLICATE_ORDER_MESSAGE}
    order = ProductionOrder(
        org_id=ctx.org.id,
        name=name,
        eta_date=body.eta_date,
        status="draft",
        items_json=json.dumps(payload, ensure_ascii=False),
    )
    db.add(order)
    # ВАЖНО (фикс P0): черновик НЕ попадает в «едет к нам» — рекомендации
    # «Что заказать» уменьшаются только после перевода заказа «В производство».
    db.commit()
    if not body.allow_duplicate:
        # Два одновременных запроса могли не увидеть друг друга до вставки:
        # тот, у кого id больше, убирает свой заказ и отдаёт чужой.
        twin = _find_twin_order(db, ctx.org.id, fingerprint, before_id=order.id)
        if twin is not None:
            db.delete(order)
            db.commit()
            return {"ok": True, "id": twin.id, "status": twin.status,
                    "duplicate": True, "message": _DUPLICATE_ORDER_MESSAGE}
    # Снапшот не роняем: черновик не меняет ни остатков, ни «едет к нам»
    # (см. api_order_plan_apply) — кэш сбрасывается при переводе в производство.
    return {"ok": True, "id": order.id, "status": "draft"}


def _apply_order_to_incoming(db: Session, org_id: int, order: ProductionOrder, sign: int) -> None:
    """Прибавляет (sign=+1) или вычитает (sign=-1) позиции заказа из «едет к нам»."""
    for item in order.items:
        base, qty = item.get("base_name"), int(item.get("qty") or 0)
        if not base or qty <= 0:
            continue
        row = db.get(OrderedQty, (org_id, base))
        if row is None:
            db.add(OrderedQty(org_id=org_id, base_name=base, qty=max(0, sign * qty)))
        else:
            row.qty = max(0, row.qty + sign * qty)


class OrderStatusIn(BaseModel):
    status: str


@router.post("/orders/{order_id}/status")
def api_order_status(
    body: OrderStatusIn,
    order_id: int = _id_path(),
    ctx: AuthContext = Depends(require_auth_api),
    db: Session = Depends(get_db),
):
    """Переходы статуса: draft → sent («В производстве») → received («Принят на склад»).

    Семантика «едет к нам»: draft не учитывается; sent прибавляет; received
    вычитает (пришедшие остатки подтянет синхронизация со складом).

    Заказ, отправленный в МойСклад (ms_doc_href), локальный qty НЕ двигает:
    его считает импорт purchaseorder (ordered_qty.ms_qty), а принятое снимают
    приёмки в МС — иначе был бы двойной счёт.
    """
    order = db.get(ProductionOrder, order_id)
    if order is None or order.org_id != ctx.org.id:
        raise HTTPException(status_code=404, detail="Заказ не найден")
    if body.status == order.status:
        # Повторная отправка того же статуса (двойной клик, ретрай запроса,
        # заказ, отданный защитой от дубля) ничего не меняет и не считается
        # ошибкой — иначе «едет к нам» посчиталось бы дважды.
        return {"ok": True, "status": order.status, "unchanged": True}
    allowed = {("draft", "sent"), ("sent", "received")}
    if (order.status, body.status) not in allowed:
        raise HTTPException(
            status_code=422,
            detail=f"Недопустимый переход статуса: {order.status} → {body.status}",
        )
    if not ms_writeback.is_pushed(order.ms_doc_href):
        if body.status == "sent":
            _apply_order_to_incoming(db, ctx.org.id, order, +1)
        else:  # received
            _apply_order_to_incoming(db, ctx.org.id, order, -1)
    order.status = body.status
    db.commit()
    analytics.invalidate(ctx.org.id)
    return {"ok": True, "status": order.status}


@router.delete("/orders/{order_id}")
def api_order_delete(
    order_id: int = _id_path(), ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)
):
    """Удаление заказа: draft — свободно; sent — с вычетом из «едет»; received — нельзя."""
    order = db.get(ProductionOrder, order_id)
    if order is None or order.org_id != ctx.org.id:
        raise HTTPException(status_code=404, detail="Заказ не найден")
    if order.status == "received":
        raise HTTPException(status_code=422, detail="Принятый на склад заказ удалить нельзя")
    if order.status == "sent" and not ms_writeback.is_pushed(order.ms_doc_href):
        # Отправленный в МС заказ в qty не входил (его считает ms_qty; сам
        # документ в МойСклад при локальном удалении никуда не девается).
        _apply_order_to_incoming(db, ctx.org.id, order, -1)
    db.delete(order)
    db.commit()
    analytics.invalidate(ctx.org.id)
    return {"ok": True}


@router.get("/orders/{order_id}")
def api_order_detail(
    order_id: int = _id_path(), ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)
):
    order = db.get(ProductionOrder, order_id)
    if order is None or order.org_id != ctx.org.id:
        raise HTTPException(status_code=404, detail="Заказ не найден")
    return _order_out(order)


class OrderedIn(BaseModel):
    base_name: str
    qty: float = Field(ge=0)


@router.post("/ordered")
def api_set_ordered(
    body: OrderedIn, ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)
):
    """Ручная правка «едет к нам» (перезапись значения по базовому имени)."""
    _require_known_base(db, ctx.org.id, body.base_name)
    row = db.get(OrderedQty, (ctx.org.id, body.base_name))
    if row is None:
        db.add(OrderedQty(org_id=ctx.org.id, base_name=body.base_name, qty=body.qty))
    else:
        row.qty = body.qty
    db.commit()
    analytics.invalidate(ctx.org.id)
    return {"ok": True}


# ── Настройки ────────────────────────────────────────────────────────────────

def _price_types_seen(db: Session, org_id: int) -> list[str]:
    """Типы цен, встреченные в ассортименте МойСклада при последнем синке.

    Нужны выпадающему списку в настройках: «какой тип цены считать полной
    себестоимостью». Своего справочника не заводим — берём то, что синк уже
    видел (ms_sync кладёт в stats и переносит между прогонами).
    """
    from app.models import SyncState
    row = db.get(SyncState, org_id)
    if row is None:
        return []
    names = (row.stats or {}).get("price_types") or []
    return [str(x) for x in names if str(x).strip()][:40]


@router.get("/settings")
def api_settings(ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)):
    org = ctx.org
    warehouses = db.execute(
        select(Warehouse).where(Warehouse.org_id == org.id).order_by(Warehouse.id)
    ).scalars().all()
    conn = db.execute(
        select(Connection).where(Connection.org_id == org.id).order_by(Connection.id.desc())
    ).scalars().first()
    members = db.execute(
        select(User.name, User.email, Membership.role)
        .join(Membership, Membership.user_id == User.id)
        .where(Membership.org_id == org.id)
    ).all()
    settings = org.settings
    extra = analytics.extra_settings(org)
    return {
        "org": {
            "name": org.name,
            "plan": org.plan,
            "trial_ends_at": org.trial_ends_at.isoformat() if org.trial_ends_at else None,
        },
        "thresholds": settings["thresholds"],
        # Горизонт заказа отдаётся ТРЕМЯ разными именами намеренно (D-27).
        # Раньше под одним словом `horizon_days` жили два разных числа: здесь —
        # сырая пользовательская настройка (90), а в снапшоте аналитики — уже
        # посчитанное эффективное значение (по умолчанию 44). Спорить об этом
        # можно было бесконечно, потому что оба ответа «правильные».
        #   horizon_days_fixed     — что человек выставил руками;
        #   horizon_days_effective — по чему СЕЙЧАС считается заказ;
        #   horizon_source         — какой режим дал это число.
        # `horizon_days` оставлен только как устаревший синоним «фиксированного»
        # для старых клиентов; новый код обязан называть величину явно.
        "horizon_days": settings["horizon_days"],
        "horizon_days_fixed": settings["horizon_days"],
        "horizon_days_effective": analytics.cover_days({**settings, **extra}),
        "horizon_source": extra["cover_mode"],
        "min_stock_days": settings["min_stock_days"],
        "rate_window": extra["rate_window"],
        "lead_time_days": extra["lead_time_days"],
        # Горизонт покрытия заказа: периодичность размещения + страховой запас.
        "cover_mode": extra["cover_mode"],
        "order_cadence_days": extra["order_cadence_days"],
        "safety_days": extra["safety_days"],
        "cover_days": analytics.cover_days({**settings, **extra}),
        "moq_units": extra["moq_units"],
        "reserve_new_pct": extra["reserve_new_pct"],
        "overhead_pct": extra["overhead_pct"],
        "price_type_sale": extra["price_type_sale"],
        "price_type_cost": extra["price_type_cost"],
        # Что за типы цен вообще встретились в ассортименте МойСклада —
        # чтобы выбирать из списка, а не угадывать написание руками.
        "price_types": _price_types_seen(db, org.id),
        "peak_periods": extra["peak_periods"],
        "warehouses": [{"id": w.id, "name": w.name, "active": w.active} for w in warehouses],
        "connection": (
            {
                "kind": conn.kind,
                "status": conn.status,
                "last_sync_at": conn.last_sync_at.isoformat() if conn.last_sync_at else None,
            }
            if conn
            else None
        ),
        "members": [{"name": n, "email": e, "role": r} for n, e, r in members],
        "role": ctx.role,  # фронт прячет owner-only кнопки (синк) от участников
    }


class ThresholdsIn(BaseModel):
    weak: int = Field(gt=0)
    dull: int = Field(gt=0)
    good: int = Field(gt=0)


class SettingsIn(BaseModel):
    thresholds: ThresholdsIn | None = None
    horizon_days: int | None = Field(default=None, ge=7, le=365)
    min_stock_days: int | None = Field(default=None, ge=0, le=100)
    rate_window: str | None = None  # 'year' | 'd90' | 'season'
    lead_time_days: int | None = Field(default=None, ge=1, le=365)
    cover_mode: str | None = None  # 'cadence' | 'fixed'
    order_cadence_days: int | None = Field(default=None, ge=7, le=365)
    safety_days: int | None = Field(default=None, ge=0, le=120)
    moq_units: int | None = Field(default=None, ge=0, le=10000)
    reserve_new_pct: int | None = Field(default=None, ge=0, le=90)
    overhead_pct: int | None = Field(default=None, ge=0, le=200)
    price_type_sale: str | None = Field(default=None, max_length=128)
    price_type_cost: str | None = Field(default=None, max_length=128)
    peak_periods: list[dict] | None = None

    @field_validator("cover_mode")
    @classmethod
    def _cover_mode_known(cls, v: str | None) -> str | None:
        if v is not None and v not in ("cadence", "fixed"):
            raise ValueError("cover_mode должен быть 'cadence' или 'fixed'")
        return v

    @field_validator("rate_window")
    @classmethod
    def _rate_window_known(cls, v: str | None) -> str | None:
        if v is not None and v not in analytics.RATE_WINDOWS:
            raise ValueError("rate_window должен быть 'year', 'd90' или 'season'")
        return v


@router.post("/settings")
def api_update_settings(
    body: SettingsIn, ctx: AuthContext = Depends(require_owner_api), db: Session = Depends(get_db)
):
    org = db.merge(ctx.org)
    # Поверх сырых настроек, а не только известных ключей: Org.settings отдаёт
    # три поля из DEFAULT_SETTINGS, и любое сохранение затирало бы всё
    # остальное, что клали туда другие разделы (правило распределения, типы
    # цен, пики). Сначала берём то, что реально лежит в БД.
    try:
        settings = json.loads(org.settings_json or "{}")
    except ValueError:
        settings = {}
    settings.update(org.settings)
    if body.thresholds is not None:
        t = body.thresholds
        if not t.weak < t.dull < t.good:
            raise HTTPException(status_code=422, detail="Пороги должны возрастать: weak < dull < good")
        settings["thresholds"] = {"weak": t.weak, "dull": t.dull, "good": t.good}
    if body.horizon_days is not None:
        settings["horizon_days"] = body.horizon_days
    if body.min_stock_days is not None:
        settings["min_stock_days"] = body.min_stock_days
    # Ключи сверх DEFAULT_SETTINGS (org.settings их не возвращает) — сохраняем,
    # не затирая при частичных POST'ах (например, только rate_window с /replenish).
    extra = analytics.extra_settings(org)
    if body.rate_window is not None:
        extra["rate_window"] = body.rate_window
    if body.lead_time_days is not None:
        extra["lead_time_days"] = body.lead_time_days
    for key in ("cover_mode", "order_cadence_days", "safety_days",
                "moq_units", "reserve_new_pct", "overhead_pct", "price_type_sale",
                "price_type_cost", "peak_periods"):
        val = getattr(body, key)
        if val is not None:
            extra[key] = val
    settings.update(extra)
    org.settings_json = json.dumps(settings, ensure_ascii=False)
    db.commit()
    analytics.invalidate(org.id)
    return {"ok": True, "settings": settings}


# ── Исключение позиций из аналитики (упаковка, сертификаты, расходники) ──────

@router.get("/exclusions")
def api_exclusions(
    q: str = "", ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)
):
    """Список исключённых баз (+ по q — поиск кандидатов среди участвующих)."""
    excluded = db.execute(
        select(Product.base_name, func.count(Product.id),
               func.min(Product.category))
        .where(Product.org_id == ctx.org.id, Product.excluded.is_(True))
        .group_by(Product.base_name)
        .order_by(Product.base_name)
    ).all()
    # Что отложила эвристика синка, а что руками выбрал владелец. Отдельного
    # флага в базе нет и заводить его ради этого не стоит: правило —
    # чистая функция, достаточно переспросить её. Аудит 22.08: пользователь
    # видел «исключено 47 позиций» без объяснения и без способа вернуть.
    from app import exclusions as _excl
    result = {"excluded": [
        {"base_name": b, "variants": int(n),
         "by_rule": _excl.is_service_item(b, c or ""),
         "reason": _excl.exclude_reason(b, c or "")}
        for b, n, c in excluded
    ]}
    result["by_rule_count"] = sum(1 for x in result["excluded"] if x["by_rule"])
    query = (q or "").strip().lower()
    if len(query) >= 2:
        candidates = db.execute(
            select(Product.base_name)
            .where(
                Product.org_id == ctx.org.id,
                Product.excluded.is_(False),
                func.lower(Product.base_name).like(f"%{query}%"),
            )
            .group_by(Product.base_name)
            .order_by(Product.base_name)
            .limit(20)
        ).scalars().all()
        result["candidates"] = candidates
    return result


class ExclusionIn(BaseModel):
    base_name: str
    excluded: bool


@router.post("/exclusions")
def api_set_exclusion(
    body: ExclusionIn, ctx: AuthContext = Depends(require_owner_api), db: Session = Depends(get_db)
):
    """Включить/исключить базовую позицию (все размеры разом)."""
    rows = db.execute(
        select(Product).where(
            Product.org_id == ctx.org.id, Product.base_name == body.base_name
        )
    ).scalars().all()
    if not rows:
        raise HTTPException(status_code=404, detail="Позиция не найдена")
    for p in rows:
        p.excluded = body.excluded
    db.commit()
    analytics.invalidate(ctx.org.id)
    return {"ok": True, "base_name": body.base_name, "excluded": body.excluded, "variants": len(rows)}


@router.post("/warehouses/{warehouse_id}/toggle")
def api_toggle_warehouse(
    warehouse_id: int = _id_path(), ctx: AuthContext = Depends(require_owner_api), db: Session = Depends(get_db)
):
    wh = db.get(Warehouse, warehouse_id)
    if wh is None or wh.org_id != ctx.org.id:
        raise HTTPException(status_code=404, detail="Склад не найден")
    from app import ms_sync as _ms_sync
    if _ms_sync.is_running(ctx.org.id):
        # Ревью 21.08: набор складов — вход идущего синка; менять его на лету
        # значит получить историю, посчитанную по двум разным наборам.
        raise HTTPException(status_code=409, detail="Дождитесь окончания синхронизации")
    wh.active = not wh.active
    db.commit()
    analytics.invalidate(ctx.org.id)
    # Ревью 21.08: точка продолжения прерванной первичной загрузки привязана
    # к набору складов — после его смены продолжать нельзя, только пересборка.
    _ms_sync.clear_resume_point(ctx.org.id)
    # История остатков (StockDay) хранится суммой по складам, активным на момент
    # синка: смена набора складов требует пересборки истории, иначе цифры
    # остатков/дней-в-стоке молча остаются старыми до полного пересинка.
    needs_resync = (
        db.execute(
            select(Connection.id).where(
                Connection.org_id == ctx.org.id,
                Connection.kind == "moysklad",
                Connection.status == "active",
            )
        ).first()
        is not None
    )
    return {"ok": True, "id": wh.id, "active": wh.active, "needs_resync": needs_resync}


# ── Подключение источника данных ─────────────────────────────────────────────

@router.post("/connect/demo")
def api_connect_demo(ctx: AuthContext = Depends(require_owner_api), db: Session = Depends(get_db)):
    """Создаёт demo-подключение и сеет синтетические данные (детерминированно)."""
    org = ctx.org
    # Аудит 18.08: seed_demo начинается с clear_org_data — раньше кнопка демо
    # без всякого предохранителя стирала ВСЕ данные боевой организации,
    # включая невосстановимые заказы на производство и ручное «Заказано».
    # Блокируем только подключения, которые РЕАЛЬНО синкали данные (ревью
    # 18.08): строка kind='moysklad' создаётся со status='pending' уже при
    # сохранении токена — до первого синка данных нет, и демо безопасно
    # (пользователь, передумавший на онбординге, не попадает в тупик).
    # Ревью 21.08 (мажор 4): проверки «active или был синк» НЕ ХВАТАЛО. При
    # прогрессивной первичной загрузке подключение до finalize-lite ещё
    # 'pending', last_sync_at пуст, а «/» до этого момента вело на онбординг,
    # где по умолчанию выбраны «Демо-данные»: один клик стирал таблицы живой
    # организации ПРЯМО ВО ВРЕМЯ записи их синком. Отказываем и когда синк
    # идёт, и когда в БД уже есть остатки (сервис работает на них).
    from app import ms_sync as _ms_sync

    ms_conn = db.execute(
        select(Connection).where(Connection.org_id == org.id,
                                 Connection.kind == "moysklad")
    ).scalars().first()
    if ms_conn is not None:
        has_stock = db.execute(
            select(StockDay.product_id).where(StockDay.org_id == org.id).limit(1)
        ).first() is not None
        if (ms_conn.status == "active" or ms_conn.last_sync_at is not None
                or _ms_sync.is_running(org.id) or has_stock):
            raise HTTPException(
                status_code=409,
                detail="У организации подключён МойСклад — его данные уже "
                       "загружаются или загружены, а демо-данные сотрут их "
                       "безвозвратно (включая заказы на производство и ручное "
                       "«Заказано»). Дождитесь окончания синхронизации; если "
                       "демо всё же нужно, напишите в поддержку — поможем "
                       "безопасно.")
    counters = seed_demo(db, org)
    conn = db.execute(
        select(Connection).where(Connection.org_id == org.id, Connection.kind == "demo")
    ).scalars().first()
    if conn is None:
        conn = Connection(org_id=org.id, kind="demo", token_enc="", config_json="{}")
        db.add(conn)
    conn.status = "active"
    conn.last_sync_at = datetime.utcnow()
    db.commit()
    analytics.invalidate(org.id)
    return {"ok": True, "seeded": counters}


class MoyskladConnectIn(BaseModel):
    token: str = Field(min_length=8)


@router.post("/connect/moysklad")
def api_connect_moysklad(
    body: MoyskladConnectIn,
    ctx: AuthContext = Depends(require_owner_api),
    db: Session = Depends(get_db),
):
    """Сохраняет шифрованный токен МойСклад; синхронизация — вне демо-скоупа."""
    org = ctx.org
    conn = db.execute(
        select(Connection).where(Connection.org_id == org.id, Connection.kind == "moysklad")
    ).scalars().first()
    if conn is None:
        conn = Connection(org_id=org.id, kind="moysklad", config_json="{}")
        db.add(conn)
    conn.token_enc = encrypt_token(body.token.strip())
    conn.status = "pending"
    db.commit()
    return {"ok": True, "note": "Синхронизация начнётся автоматически"}


# ── Онбординг-инструкции страниц (значок «?») ────────────────────────────────

class HintSeenIn(BaseModel):
    page: str = Field(min_length=1, max_length=64)


@router.get("/hints/seen")
def api_hints_seen(ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)):
    """Страницы, инструкции которых пользователь уже видел."""
    from app.models import UserHintSeen
    rows = db.execute(
        select(UserHintSeen.page).where(UserHintSeen.user_id == ctx.user.id)
    ).scalars().all()
    return {"seen": list(rows)}


@router.post("/hints/seen")
def api_hint_mark_seen(
    body: HintSeenIn,
    ctx: AuthContext = Depends(require_auth_api),
    db: Session = Depends(get_db),
):
    """Отметить инструкцию страницы просмотренной (идемпотентно)."""
    from app.models import UserHintSeen
    row = db.get(UserHintSeen, (ctx.user.id, body.page))
    if row is None:
        db.add(UserHintSeen(user_id=ctx.user.id, page=body.page))
        db.commit()
    return {"ok": True}


# ── Обучение: пять уроков по страницам (страница /lessons) ───────────────────
#
# Уроки личные (per-user, не per-org) и проходятся сколько угодно раз: роль
# здесь не проверяется — учиться может любой участник организации.


class HintsPrefIn(BaseModel):
    enabled: bool


def _lessons_done(db: Session, user_id: int) -> set[str]:
    """Ключи пройденных уроков пользователя (только те, что есть в каталоге)."""
    from app.models import UserLesson
    rows = db.execute(
        select(UserLesson.lesson).where(UserLesson.user_id == user_id)
    ).scalars().all()
    return {k for k in rows if k in lessons.KEYS}


def _hints_enabled(db: Session, user_id: int) -> bool:
    """Тумблер «показывать подсказки»: нет строки — значит включено (дефолт)."""
    from app.models import UserPrefs
    row = db.get(UserPrefs, user_id)
    return True if row is None else bool(row.hints_enabled)


def _known_lesson(key: str) -> dict:
    """Урок каталога или 404 — чтобы опечатка в ключе не копилась в БД."""
    for lesson in lessons.CATALOGUE:
        if lesson["key"] == key:
            return lesson
    raise HTTPException(status_code=404, detail="Неизвестный урок")


@router.get("/lessons")
def api_lessons(ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)):
    """Каталог уроков с отметками пройденности + состояние тумблера подсказок."""
    done = _lessons_done(db, ctx.user.id)
    items = [dict(lesson, done=lesson["key"] in done) for lesson in lessons.CATALOGUE]
    return {
        "lessons": items,
        "done_count": sum(1 for it in items if it["done"]),
        "total": lessons.TOTAL,
        "hints_enabled": _hints_enabled(db, ctx.user.id),
    }


@router.post("/lessons/{key}/done")
def api_lesson_done(
    key: str,
    ctx: AuthContext = Depends(require_auth_api),
    db: Session = Depends(get_db),
):
    """Отметить урок пройденным (идемпотентно: повтор не меняет ничего)."""
    from app.models import UserLesson
    _known_lesson(key)
    if db.get(UserLesson, (ctx.user.id, key)) is None:
        db.add(UserLesson(user_id=ctx.user.id, lesson=key))
        db.commit()
    return {"ok": True, "done_count": len(_lessons_done(db, ctx.user.id))}


@router.post("/lessons/reset")
def api_lessons_reset_all(
    ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)
):
    """Сбросить прогресс целиком («Пройти всё заново»)."""
    from app.models import UserLesson
    db.query(UserLesson).filter(UserLesson.user_id == ctx.user.id).delete()
    db.commit()
    return {"ok": True, "done_count": 0}


@router.post("/lessons/{key}/reset")
def api_lesson_reset(
    key: str,
    ctx: AuthContext = Depends(require_auth_api),
    db: Session = Depends(get_db),
):
    """Снять отметку с одного урока («Пройти заново»); идемпотентно."""
    from app.models import UserLesson
    _known_lesson(key)
    row = db.get(UserLesson, (ctx.user.id, key))
    if row is not None:
        db.delete(row)
        db.commit()
    return {"ok": True, "done_count": len(_lessons_done(db, ctx.user.id))}


@router.get("/lessons/sample")
def api_lessons_sample(
    ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)
):
    """«Живой» пример для тура: бестселлер с наименьшим запасом в днях.

    Считаем из того же снапшота, что и страница «Оборачиваемость», — цифры в
    уроке совпадут с тем, что человек видит в таблице. Пока организация
    синкается (данных ещё нет), возвращаем sample=null: тур покажет fallback
    и не станет придумывать примеры. Поле rate — оборачиваемость в ₽/день.
    """
    snap = analytics.get_snapshot(db, ctx.org)
    return {"sample": lessons.pick_sample(analytics.build_turnover(snap))}


@router.post("/prefs/hints")
def api_prefs_hints(
    body: HintsPrefIn,
    ctx: AuthContext = Depends(require_auth_api),
    db: Session = Depends(get_db),
):
    """Тумблер «Показывать подсказки на страницах» (личный, per-user)."""
    from app.models import UserPrefs
    row = db.get(UserPrefs, ctx.user.id)
    if row is None:
        row = UserPrefs(user_id=ctx.user.id, hints_enabled=body.enabled)
        db.add(row)
    else:
        row.hints_enabled = body.enabled
    db.commit()
    return {"ok": True, "hints_enabled": bool(body.enabled)}


# ── Производства: основное + добавляемые (Китай / Москва / Екатеринбург…) ────

class ProductionIn(BaseModel):
    """Производство: имя + необязательные условия подрядчика.

    Все три условия опциональны и «стираемы»: не прислали ключ — значение не
    трогаем, прислали null или 0 — возвращаем к «как в общих настройках»
    (срок) / «ограничения нет» (партия, кратность).
    """

    name: str = Field(min_length=1, max_length=120)
    lead_time_days: int | None = Field(default=None, ge=0, le=365)
    moq: int | None = Field(default=None, ge=0, le=100_000)
    pack_multiple: int | None = Field(default=None, ge=0, le=10_000)
    # Этапы канала сразу при создании: turnkey (Китай под ключ) |
    # fabric_sewing (своё производство: ткань → пошив). Можно задать позже.
    preset: str | None = None
    moq_units: int | None = Field(default=None, ge=0, le=10000)


class ProductionAssignIn(BaseModel):
    base_name: str = Field(min_length=1, max_length=255)
    # None = вернуть на основное производство; границы — как у id в пути,
    # иначе слишком большое число валит SQLite (OverflowError) вместо 422
    production_id: int | None = Field(default=None, ge=1, le=2_147_483_647)


# Потребность ниже этой доли минимальной партии — «заказывать невыгодно»:
# фабрика примет только партию целиком, и человек должен решить сам, стоит ли
# везти 30 штук ради двух проданных.
MOQ_LOW_SHARE = 0.5


def round_to_batch(qty: int, moq: int = 0, multiple: int = 0) -> int:
    """Количество к заказу с учётом минимальной партии и кратности упаковки.

    Округляем ТОЛЬКО вверх: меньше минимальной партии фабрика не примет, а
    некратное упаковке количество придётся добивать всё равно. qty <= 0
    остаётся нулём — позиции, которую заказывать не нужно, партия не касается.
    """
    if qty <= 0:
        return 0
    out = max(int(qty), int(moq or 0))
    step = int(multiple or 0)
    if step > 1:
        out = -(-out // step) * step
    return out


def production_conditions(db: Session, org_id: int) -> dict:
    """Условия производств организации и привязка позиций к ним.

    Возвращает {"main_id", "by_id": {id: производство}, "assign": {позиция: id}}.
    Позиции без записи в assign (и с записью на удалённое производство) —
    на основном.
    """
    prods = db.execute(
        select(Production).where(Production.org_id == org_id)
    ).scalars().all()
    main = next((p for p in prods if p.is_main), None)
    by_id = {p.id: p for p in prods}
    # Привязка берётся ИТОГОВАЯ (правило распределения + ручные назначения
    # поверх него), а не только ручная: позиция, отданная цеху правилом, должна
    # считаться по срокам и минимальной партии ЭТОГО цеха, иначе условия
    # производства расходятся с тем, за каким цехом позиция числится на экране.
    from app import assign_rules

    org = db.get(Org, org_id)
    if org is not None:
        assign = {
            base: pid
            for base, pid in assign_rules.effective_assign(db, org).items()
            if pid in by_id
        }
    else:  # организация исчезла — читаем хотя бы ручные записи
        assign = {
            a.base_name: a.production_id
            for a in db.execute(
                select(ProductionAssign).where(ProductionAssign.org_id == org_id)
            ).scalars()
            if a.production_id in by_id
        }
    return {"main_id": main.id if main else None, "by_id": by_id, "assign": assign}


def apply_production_rules(db: Session, org_id: int, data: dict) -> dict:
    """Досчитывает ответ «Заказа» по условиям производства каждой позиции.

    Что добавляется каждой позиции:
      production_id / production_name — за каким цехом она закреплена;
      lead_time_days — срок ЭТОГО производства (или общий из настроек);
      moq / pack_multiple — его минимальная партия и кратность (0 = нет);
      need_raw — сколько было по расчёту ДО округления;
      need — сколько получилось после округления вверх (в него же приводится
      размерная сетка: сумма по размерам обязана равняться итогу позиции,
      поэтому она пересобирается тем же analytics.size_split);
      moq_applied — число выросло из-за партии/кратности, а не само по себе;
      moq_low — потребность сильно ниже минимальной партии: заказывать
      невыгодно, решение за человеком.

    Срок производства на сами метрики (прогнозный остаток, стокаут, «дыра»)
    пока НЕ влияет: эта математика живёт в app/analytics.py — см. отчёт.
    """
    default_lead = int(data.get("lead_time_days") or analytics.DEFAULT_LEAD_TIME_DAYS)
    cond = production_conditions(db, org_id)
    items = data.get("items") or []
    # Считает ли аналитика прогноз остатка/стокаут по сроку КОНКРЕТНОГО
    # производства: признак — она сама вернула срок у позиции. Пока нет —
    # страница «Заказа» не выдаёт срок подрядчика за срок, по которому
    # посчитан прогнозный остаток (см. отчёт: правка в app/analytics.py).
    data["lead_time_by_production"] = bool(items) and all(
        "lead_time_days" in it for it in items
    )
    for item in items:
        prod = cond["by_id"].get(cond["assign"].get(item["base_name"], cond["main_id"]))
        moq = int(getattr(prod, "moq", 0) or 0)
        step = int(getattr(prod, "pack_multiple", 0) or 0)
        item["production_id"] = prod.id if prod else None
        item["production_name"] = prod.name if prod else ""
        item["lead_time_days"] = int(getattr(prod, "lead_time_days", 0) or 0) or default_lead
        item["moq"] = moq
        item["pack_multiple"] = step
        need_raw = int(item["need"])
        need = round_to_batch(need_raw, moq, step)
        item["need_raw"] = need_raw
        item["need"] = need
        item["moq_applied"] = need != need_raw
        item["moq_low"] = bool(moq and need_raw < moq * MOQ_LOW_SHARE)
        if need != need_raw:
            # Размерная сетка пересобирается тем же largest-remainder, что и
            # исходная рекомендация, — иначе сумма по размерам разошлась бы с
            # итогом позиции (и с итогом страницы).
            rec = analytics.size_split(item.get("sizes") or {}, need)
            for size, cell in (item.get("sizes") or {}).items():
                cell["rec"] = rec.get(size, 0)
            avg_price = item.get("avg_price") or 0
            item["profit_potential"] = round(
                max(0, avg_price - (item.get("cost_price") or 0)) * need
            )
    return data


def _production_out(p, fallback_lead: int) -> dict:
    """Производство для фронта: условия подрядчика + этапы «Мастера».

    Слияние 22.08: до слияния было две функции с этим именем — наша (срок,
    минимальная партия, кратность; пустое отдаём нулями — «не задано») и их
    (этапы производства, суммарный срок, ритм, предоплата). Ключи не
    пересекаются, поэтому отдаём один объект с обоими наборами полей:
    lead_time_days — СОБСТВЕННЫЙ срок цеха (0 = «как в общих настройках»),
    lead_days — суммарный срок по этапам, уже с подставленным fallback_lead.
    """
    from app import order_planner
    stages = order_planner.normalize_stages(p.stages, fallback_lead)
    return {
        "id": p.id,
        "name": p.name,
        "is_main": p.is_main,
        "lead_time_days": int(p.lead_time_days or 0),
        "moq": int(p.moq or 0),
        "pack_multiple": int(p.pack_multiple or 0),
        "stages": stages,
        "lead_days": order_planner.lead_days(stages),
        "moq_units": int(p.moq_units or 0),
        "cadence_days": int(p.cadence_days or 0),
        "prepay_now_share": round(order_planner.prepay_share_total(stages), 4),
        "staged": len(stages) > 1,
    }


def _apply_production_in(p, body: ProductionIn) -> None:
    """Переносит присланные условия в производство (0/null = «не задано»)."""
    sent = body.model_fields_set
    if "lead_time_days" in sent:
        p.lead_time_days = int(body.lead_time_days) if body.lead_time_days else None
    if "moq" in sent:
        p.moq = int(body.moq) if body.moq else None
    if "pack_multiple" in sent:
        p.pack_multiple = int(body.pack_multiple) if body.pack_multiple else None


def _ensure_main_production(db: Session, org_id: int):
    """У организации всегда есть основное производство — создаём лениво."""
    from app.models import Production
    main = db.execute(
        select(Production).where(Production.org_id == org_id, Production.is_main.is_(True))
    ).scalars().first()
    if main is None:
        main = Production(org_id=org_id, name="Основное производство", is_main=True)
        db.add(main)
        db.commit()
        db.refresh(main)
    return main


@router.get("/productions")
def api_productions(ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)):
    """Список производств + распределение позиций (base_name -> production_id).

    Позиции без записи в assign — на основном производстве."""
    from app.models import Production, ProductionAssign
    _ensure_main_production(db, ctx.org.id)
    prods = db.execute(
        select(Production).where(Production.org_id == ctx.org.id)
        .order_by(Production.is_main.desc(), Production.id)
    ).scalars().all()
    valid = {p.id for p in prods}
    assigns = db.execute(
        select(ProductionAssign).where(ProductionAssign.org_id == ctx.org.id)
    ).scalars().all()
    from app import assign_rules
    fallback_lead = analytics.extra_settings(ctx.org)["lead_time_days"]
    rule = assign_rules.rule_of(ctx.org)
    return {
        "productions": [_production_out(p, fallback_lead) for p in prods],
        # Итоговое распределение = правило + ручные назначения (ручные сильнее).
        "assign": assign_rules.effective_assign(db, ctx.org),
        "assign_manual": {a.base_name: a.production_id for a in assigns if a.production_id in valid},
        "assign_source": rule["assign_source"],
        "assign_map": rule["assign_map"],
        # чем считается позиция, у производства которой срок не задан
        "default_lead_time_days": fallback_lead,
    }


@router.post("/productions")
def api_production_create(
    body: ProductionIn, ctx: AuthContext = Depends(require_owner_api), db: Session = Depends(get_db)
):
    """Добавить дополнительное производство (если заказами занимается другой отдел).

    Только владелец: срок, минимальная партия и кратность нового цеха меняют
    рекомендации и суммы заказов всей организации — как и настройки."""
    from app.models import Production
    _ensure_main_production(db, ctx.org.id)
    from app import order_planner
    fallback_lead = analytics.extra_settings(ctx.org)["lead_time_days"]
    p = Production(org_id=ctx.org.id, name=body.name.strip(), is_main=False)
    _apply_production_in(p, body)
    if body.preset:
        raw = order_planner.STAGE_PRESETS.get(body.preset)
        if raw is None:
            raise HTTPException(422, "Неизвестный пресет этапов")
        p.stages_json = json.dumps(
            order_planner.normalize_stages(raw, fallback_lead), ensure_ascii=False
        )
    if body.moq_units is not None:
        p.moq_units = int(body.moq_units)
    db.add(p)
    db.commit()
    db.refresh(p)
    analytics.invalidate(ctx.org.id)
    return _production_out(p, fallback_lead)


@router.get("/productions/assign-sources")
def api_assign_sources(
    ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)
):
    """Чем заполнены данные МойСклада и что предложить в качестве правила."""
    from app import assign_rules
    rule = assign_rules.rule_of(ctx.org)
    return {
        "current": rule,
        "sources": assign_rules.source_values(db, ctx.org.id),
        "suggest": assign_rules.suggest_rule(db, ctx.org.id),
    }


class AssignRuleIn(BaseModel):
    assign_source: str = Field(default="manual")
    assign_map: dict[str, int] = Field(default_factory=dict)

    @field_validator("assign_source")
    @classmethod
    def _known(cls, v: str) -> str:
        from app import assign_rules
        if v not in assign_rules.SOURCES:
            raise ValueError("assign_source должен быть manual, supplier или folder")
        return v


@router.post("/productions/assign-rule")
def api_assign_rule(
    body: AssignRuleIn,
    ctx: AuthContext = Depends(require_owner_api), db: Session = Depends(get_db),
):
    """Правило распределения позиций по производствам."""
    from app import assign_rules
    org = db.merge(ctx.org)
    valid = set(db.execute(
        select(Production.id).where(Production.org_id == org.id)).scalars())
    bad = [str(v) for v in body.assign_map.values() if v not in valid]
    if bad:
        raise HTTPException(422, "В правиле указаны неизвестные производства: " + ", ".join(bad))
    try:
        settings = json.loads(org.settings_json or "{}")
    except ValueError:
        settings = {}
    settings.update(org.settings)
    settings.update(analytics.extra_settings(org))
    settings["assign_source"] = body.assign_source
    settings["assign_map"] = {str(k): int(v) for k, v in body.assign_map.items()}
    org.settings_json = json.dumps(settings, ensure_ascii=False)
    db.commit()
    analytics.invalidate(org.id)
    assign = assign_rules.effective_assign(db, org)
    by_pid: dict[str, int] = {}
    for pid in assign.values():
        by_pid[str(pid)] = by_pid.get(str(pid), 0) + 1
    return {"ok": True, "assign_source": body.assign_source,
            "assigned": len(assign), "by_production": by_pid}


@router.post("/productions/assign")
def api_production_assign(
    body: ProductionAssignIn,
    ctx: AuthContext = Depends(require_owner_api), db: Session = Depends(get_db),
):
    """Перенести позицию на производство (production_id=null — на основное).

    Вместе с позицией меняются её условия (срок производства, минимальная
    партия, кратность), поэтому сбрасываем кэш аналитики. Право — как у
    настроек: только владелец организации.
    """
    from app.models import Production, ProductionAssign
    _require_known_base(db, ctx.org.id, body.base_name)
    row = db.get(ProductionAssign, (ctx.org.id, body.base_name))
    # production_id = null означает «снять ручное назначение»: позиция снова
    # подчиняется правилу распределения (а если правила нет — основному
    # производству). Пин НА ОСНОВНОЕ пишется явной записью с его id, иначе при
    # активном правиле вернуть позицию к себе было бы нечем.
    if body.production_id is None:
        if row is not None:
            db.delete(row)
            db.commit()
            analytics.invalidate(ctx.org.id)
        return {"ok": True}
    p = db.get(Production, body.production_id)
    if p is None or p.org_id != ctx.org.id:
        raise HTTPException(404, "Производство не найдено")
    if row is None:
        db.add(ProductionAssign(org_id=ctx.org.id, base_name=body.base_name, production_id=p.id))
    else:
        row.production_id = p.id
    db.commit()
    analytics.invalidate(ctx.org.id)
    return {"ok": True}


@router.post("/productions/{pid}")
def api_production_rename(
    body: ProductionIn,
    pid: int = _id_path(),
    ctx: AuthContext = Depends(require_owner_api), db: Session = Depends(get_db),
):
    """Переименовать производство и задать его условия (основное — тоже можно).

    Срок производства, минимальная партия и кратность влияют на рекомендации
    «Заказа», поэтому сбрасываем кэш аналитики. Право — как у настроек: менять
    условия, от которых зависят деньги всей организации, может только владелец.
    """
    from app.models import Production
    p = db.get(Production, pid)
    if p is None or p.org_id != ctx.org.id:
        raise HTTPException(404, "Производство не найдено")
    p.name = body.name.strip()
    _apply_production_in(p, body)
    db.commit()
    analytics.invalidate(ctx.org.id)
    return _production_out(p, analytics.extra_settings(ctx.org)["lead_time_days"])


@router.delete("/productions/{pid}")
def api_production_delete(
    pid: int = _id_path(), ctx: AuthContext = Depends(require_owner_api), db: Session = Depends(get_db)
):
    """Удалить дополнительное производство; его позиции возвращаются на основное.

    Только владелец: позиции цеха возвращаются на основное производство, то
    есть меняются условия и суммы заказов всей организации."""
    from app.models import Production, ProductionAssign
    p = db.get(Production, pid)
    if p is None or p.org_id != ctx.org.id:
        raise HTTPException(404, "Производство не найдено")
    if p.is_main:
        raise HTTPException(400, "Основное производство удалить нельзя — его можно переименовать")
    db.query(ProductionAssign).filter(
        ProductionAssign.org_id == ctx.org.id, ProductionAssign.production_id == pid
    ).delete()
    db.delete(p)
    db.commit()
    # позиции вернулись на основное производство — с его сроком и партией
    analytics.invalidate(ctx.org.id)
    return {"ok": True}


@router.post("/ordered/add")
def api_add_ordered(
    body: OrderedIn, ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)
):
    """«Заказ отправлен» со страницы «Заказ отдельной позиции»: ПРИБАВЛЯЕТ
    количество к «едет к нам» (в отличие от /ordered, который перезаписывает).
    Приёмка в МойСкладе или received-статус заказа спишут это количество."""
    _require_known_base(db, ctx.org.id, body.base_name)
    row = db.get(OrderedQty, (ctx.org.id, body.base_name))
    if row is None:
        db.add(OrderedQty(org_id=ctx.org.id, base_name=body.base_name, qty=max(0, body.qty)))
        total = max(0, body.qty)
    else:
        row.qty = max(0, (row.qty or 0) + body.qty)
        total = row.qty
    db.commit()
    analytics.invalidate(ctx.org.id)
    return {"ok": True, "base_name": body.base_name, "qty_total": total}


# ── Мастер заказа: план под бюджет ───────────────────────────────────────────
# Анкета → план (волны, MOQ, этапы производства) → заказ. Ядро — чистая функция
# app/order_planner.py; здесь только транспорт и сохранение.

class OrderPlanIn(BaseModel):
    production_id: int | None = None
    eta_date: str | None = None            # когда товар нужен на складе
    budget: int = Field(default=0, ge=0)   # деньги на этот заказ, ₽
    budget_scope: str | None = None        # now (первый этап) | full (весь заказ)
    cadence_days: int | None = None        # как часто размещаются заказы
    safety_days: int | None = None
    # Режим горизонта и фиксированное число дней. Нужны, чтобы «Повторить»
    # старый план считал его ТЕМ ЖЕ горизонтом, а не сегодняшней настройкой
    # организации: иначе план, посчитанный на 44 дня, при повторе после
    # переключения режима вырастает в разы без единой правки в анкете.
    # None = берём настройку организации (обычное поведение нового плана).
    cover_mode: str | None = None          # cadence | fixed
    horizon_days_fixed: int | None = Field(default=None, ge=7, le=365)
    strategy: str | None = None            # protect | balance | grow
    width_days: int | None = None          # сколько дней продаж закрывает строка (0 = всю потребность)
    max_share_pct: int | None = None
    moq_units: int | None = None
    reserve_new_pct: int | None = None
    # Накладные к себестоимости на ЭТОТ заказ (растаможка, срочная доставка).
    # None = берём общую настройку организации.
    overhead_pct: int | None = Field(default=None, ge=0, le=200)
    exclude_categories: list[str] = Field(default_factory=list)
    must_have: list[str] = Field(default_factory=list)
    # Новинки без истории продаж: владелец вписывает их руками
    # [{"name": "Пальто Осень", "qty": 30, "cost": 9000, "category": "Верхняя одежда"}]
    new_items: list[dict] = Field(default_factory=list)
    # Ручные правки количеств в готовом плане: {base_name: qty}. Применяются
    # ПОСЛЕ расчёта — владелец всегда сильнее алгоритма.
    overrides: dict[str, int] = Field(default_factory=dict)

    @field_validator("cover_mode")
    @classmethod
    def _plan_cover_mode_known(cls, v: str | None) -> str | None:
        if v is not None and v not in ("cadence", "fixed"):
            raise ValueError("cover_mode должен быть 'cadence' или 'fixed'")
        return v


_EXCLUDED_KEEP = ("base_name", "category", "cls", "turnover", "need",
                  "cost_price", "need_rub", "lost_margin", "moq_cost", "gap_days")


def _slim_excluded(rows) -> list:
    """Отсев в сжатом виде: имя, причина и цена отказа, без лишних полей.

    База — один файл SQLite, а каталог у клиента может быть на 1000+ позиций.
    Хранить полную строку кандидата (вместе с ростовкой) по каждой не вошедшей
    позиции — это мегабайты на каждый план ни за чем.
    """
    if not isinstance(rows, list):
        return []
    return [{k: r.get(k) for k in _EXCLUDED_KEEP if k in r}
            for r in rows if isinstance(r, dict)]


# Что сохраняем про позицию, которую человек обнулил вручную. Полная строка
# плана тащит за собой ростовку и календарь — здесь они бессмысленны, а объём
# result_json растёт на каждой правке.
_ZEROED_KEEP = (
    "base_name", "category", "cls", "need", "qty_recommended",
    "cost_price", "avg_price", "turnover", "why", "why_text",
)


def _plan(db: Session, ctx: AuthContext, body: OrderPlanIn) -> dict:
    from app import order_planner
    snap = analytics.get_snapshot(db, ctx.org)
    plan = order_planner.build_plan(db, ctx.org, snap, body.model_dump())
    # Рекомендация системы фиксируется ДО правок и у КАЖДОЙ строки.
    # Раньше `qty_recommended` появлялся только у строк, которых коснулся
    # человек, — то есть у остальных «что советовала система» и «что решил
    # человек» были одним и тем же полем `qty`, и три величины (решение
    # владельца D-25) формально не различались даже там, где всё в порядке.
    must = set((plan.get("brief") or {}).get("must_have") or [])
    for it in plan.get("items") or []:
        it.setdefault("qty_recommended", it.get("qty"))
        # Позиция, которую человек втащил галочкой «Взять» из блока «система
        # считать не берётся», прошла расчёт — но попала в план по решению
        # ЧЕЛОВЕКА. Без этой пометки она в истории неотличима от честной
        # рекомендации, и метрика качества рекомендаций (D-25) загрязняется
        # с первого же дня.
        if it.get("base_name") in must:
            it["forced_by_user"] = True
    # Ключ есть всегда, а не только после ручных правок: потребитель не должен
    # гадать, «нет позиций без себестоимости» это или «поле не посчитали».
    plan["budget_incomplete"] = None
    plan["zeroed"] = []
    if body.overrides:
        _apply_overrides(plan, body.overrides, snap)
    plan["record"] = _decision_record(db, ctx, snap)
    return plan


def _decision_record(db: Session, ctx: AuthContext, snap: dict) -> dict:
    """Обстоятельства решения: версия алгоритма, настройки, качество данных.

    Строки плана и так хранят точечный снимок входов (остаток, товар в пути,
    оба темпа, себестоимость, цена, потребность). Но три вещи в них не попадают,
    а без них план через полгода нельзя ни объяснить, ни сравнить с другим:

      • версия алгоритма — иначе разница между двумя планами неотличима от
        правки кода (см. app/version.py, там же почему версий две);
      • настройки, которые реально применялись — окно темпа, режим горизонта,
        ритм, накладные, пороги классов. Они живут в настройках организации и
        МЕНЯЮТСЯ; через месяц восстановить «а что стояло тогда» нечем;
      • качество данных на момент расчёта — сколько было истории, у скольких
        позиций не заполнена себестоимость, когда последний раз синхронизировались.
        Это же ответ на вопрос «почему тогда посчитали именно так».

    Всё компактное: несколько чисел, не снимок базы. Сырые продажи и остатки
    восстанавливаются из самих таблиц по дате создания плана.
    """
    from app import version

    settings = dict(snap.get("settings") or {})
    keys = ("rate_window", "cover_mode", "horizon_days", "horizon_days_setting",
            "order_cadence_days", "safety_days", "lead_time_days", "min_stock_days",
            "overhead_pct", "reserve_new_pct", "moq_units", "thresholds")
    return {
        "algo": version.algo_version(),
        "settings": {k: settings.get(k) for k in keys if k in settings},
        "data_quality": _data_quality(db, ctx, snap),
    }


def _data_quality(db: Session, ctx: AuthContext, snap: dict) -> dict:
    """Факты о данных, на которых посчитан план. Одни и те же на экране и в истории.

    Решение владельца D-23: если данных не хватает, система не придумывает
    число, а говорит, ЧЕГО не хватает. Значит и «всё в порядке» надо говорить
    теми же категориями фактов, а не одной обобщающей оценкой.
    """
    from app import order_planner
    from app.models import SyncState

    raw_items = snap.get("items") or {}
    items = list(raw_items.values()) if isinstance(raw_items, dict) else list(raw_items)
    st = db.get(SyncState, ctx.org.id)
    return {
        "coverage_days": order_planner.coverage_days(snap),
        # Дата, с которой вообще есть история остатков. Число дней без даты
        # человеку ничего не говорит: «400 дней» и «с 19 июля прошлого года» —
        # одно и то же, но проверить можно только второе.
        "coverage_start": snap.get("coverage_start"),
        "positions_total": len(items),
        # Полная себестоимость (из выбранного типа цены МС) против закупочной:
        # в закупочной у многих брендов нет ткани, и заказ выходит дешевле,
        # чем на самом деле.
        "positions_cost_full": sum(1 for it in items if it.get("cost_is_full")),
        # Себестоимость — вход, без которого позиция вообще не попадает
        # в заказ. Доля незаполненных объясняет план лучше любой оценки.
        "positions_no_cost": sum(1 for it in items if it.get("no_cost")),
        "last_sync_at": (st.finished_at.isoformat()
                         if st is not None and st.finished_at else None),
        "sync_state": (st.state if st is not None else None),
    }


MAX_MANUAL_QTY = 1_000_000


def _manual_addable(plan: dict) -> set:
    """Позиции, которые человек вправе вписать в этот план руками.

    Ровно те, по которым система отказалась считать из-за отсутствия
    себестоимости (`review.no_cost`). Прочие отсеянные — чужой канал
    производства, исключённая категория, нет продаж — отсеяны бизнес-правилами,
    а не нехваткой данных, и «последнее слово за пользователем» на них не
    распространяется: заказ ушёл бы на другое производство с чужими сроками
    и предоплатой.
    """
    rev = (plan.get("review") or {}).get("no_cost") or []
    return {r.get("base_name") for r in rev if isinstance(r, dict)}


def _manual_item(base: str, qty: int, snap: dict, plan: dict) -> dict | None:
    """Строка заказа, добавленная человеком вручную (D-23).

    Отличается от строк плана одним принципиальным полем: `qty_recommended`
    здесь **None**. Система эту позицию не рекомендовала — она вообще не
    смогла её посчитать, — и выдавать решение человека за свою рекомендацию
    нельзя: именно на этом различии держится вся будущая оценка качества
    рекомендаций (D-25).

    Возвращает None, если позиции нет в каталоге организации, она в архиве
    или скрыта. Проверка по снапшоту, а не по присланному имени: снапшот
    построен по org_id, и это единственное место, где ручное добавление
    могло бы стать дырой в изоляции арендаторов.
    """
    src = (snap.get("items") or {}).get(base)
    if not src or src.get("archived") or src.get("hidden"):
        return None
    cost = float(src.get("cost_price") or 0)
    # Накладные (доставка, таможня, брак) плановые строки уже включают
    # (order_planner). Без этого ручная строка систематически дешевле соседних.
    overhead = float((plan.get("brief") or {}).get("overhead_pct") or 0)
    if cost > 0 and overhead:
        cost = cost * (1 + overhead / 100.0)
    price = float(src.get("avg_price") or src.get("sale_price") or 0)
    margin = max(0.0, price - cost) if cost > 0 else 0.0
    stages = plan.get("stages") or []
    pay_share = stages[0].get("cost_share", 1.0) * stages[0].get("prepay_share", 1.0) \
        if stages else 1.0
    return {
        "base_name": base,
        "category": src.get("category") or "Без категории",
        "cls": src.get("cls"),
        "turnover": round(float(src.get("turnover") or 0)),
        "rate": float(src.get("rate_active") or src.get("rate") or 0),
        "cs": int(src.get("cs") or 0),
        "ordered": int(float(src.get("ordered") or 0)),
        "proj_stock": float(src.get("cs") or 0),
        "gap_days": None,
        # Потребности система не считала — ставим 0, а не выдуманное число.
        # Из этого следует, что вся партия числится «сверх потребности», и
        # прибыль по ней НЕ обещается: обещать её было бы ровно тем враньём,
        # против которого написано правило «маржа только по спросу».
        "need": 0,
        "qty": qty,
        "qty_recommended": None,
        "unmet": 0,
        "sizes": analytics.size_split(src.get("sizes") or {}, qty),
        "cost_price": round(cost),
        "avg_price": round(price),
        "cost_total": round(qty * cost),
        "pay_now": round(qty * cost * pay_share),
        "expected_profit": 0,
        "over_need_profit": round(qty * margin),
        "over_need": qty,
        "days_to_sell": None,
        "no_supplier": False,
        "no_cost": cost <= 0,
        "runs_out": None,
        "covered_until": None,
        "why": ["manual_add"],
        "why_text": ("добавлено вручную; себестоимость не заполнена"
                     if cost <= 0 else "добавлено вручную"),
    }


def _apply_overrides(plan: dict, overrides: dict, snap: dict) -> None:
    """Ручные правки количеств поверх расчёта.

    Аудит 22.08.2026: раньше пересчитывались только строки и итоги, а календарь
    платежей, «хватит до», «упущено» и чувствительность оставались от исходного
    расчёта — после правки 10 → 5000 календарь расходился с планом на 20,1 млн ₽.
    Теперь правка пересобирает ВСЁ, что зависит от количеств.
    """
    from app import order_planner as op
    from app.analytics import size_split
    items = {i["base_name"]: i for i in plan["items"]}
    for base, qty in overrides.items():
        try:
            # Верхняя граница обязательна: без неё целое из JSON произвольной
            # длины уходит в арифметику с float и роняет запрос OverflowError.
            # Миллион штук на позицию — заведомо больше любого настоящего
            # заказа, но не мешает работать.
            qty = max(0, min(int(qty), MAX_MANUAL_QTY))
        except (TypeError, ValueError):
            continue
        it = items.get(base)
        if it is None:
            # Позиции нет в плане, а человек назвал количество. Решение
            # владельца (D-23, дополнение 22.08): отказ системы считать —
            # это не запрет человеку действовать. Классический случай —
            # позиция без себестоимости: экономику по ней «Оборот» посчитать
            # не может и рекомендацию не даёт, но заказать её владелец вправе.
            #
            # Добавляем ТОЛЬКО то, что реально есть в каталоге этой
            # организации (snap["items"] уже отфильтрован по org_id) — иначе
            # через тело запроса можно было бы вписать в заказ что угодно,
            # включая чужое название.
            if qty <= 0:
                continue
            if base not in _manual_addable(plan):
                # Добавить можно ТОЛЬКО то, что система показала в блоке
                # «нет себестоимости»: по этим позициям она сознательно не даёт
                # рекомендацию, и решение остаётся за человеком (D-23).
                # Всё остальное отсеяно по другим правилам — по чужому каналу
                # производства, по исключённой категории, по отсутствию продаж —
                # и обходить их через тело запроса нельзя: заказ уехал бы не на
                # то производство и не с теми сроками и предоплатой.
                continue
            it = _manual_item(base, qty, snap, plan)
            if it is None:
                continue
            plan["items"].append(it)
            items[base] = it
            plan["manual_edit"] = True
            continue
        # Рекомендация системы сохраняется рядом с правкой, а не затирается ею.
        # Без этого исчезает единственный сигнал, ради которого записи решений
        # вообще нужны: где человек систематически исправляет алгоритм.
        it.setdefault("qty_recommended", it["qty"])
        it["qty"] = qty
        it["unmet"] = max(0, it["need"] - qty)
        it["over_need"] = max(0, qty - it["need"])
        it["cost_total"] = round(qty * it["cost_price"])
        # Маржа — только по спросу (как в планировщике): штуки сверх
        # потребности в горизонте заказа не продадутся.
        margin = max(0, it["avg_price"] - it["cost_price"])
        it["expected_profit"] = round(min(qty, it["need"]) * margin)
        it["over_need_profit"] = round(max(0, qty - it["need"]) * margin)
        src = (snap["items"].get(base) or {}).get("sizes") or {}
        it["sizes"] = size_split(src, qty)
        rate = it.get("rate") or 0
        it["days_to_sell"] = int(qty / rate) if rate > 0 else None
        it["covered_until"] = (
            (date.fromisoformat(plan["eta_date"])
             + timedelta(days=min(int((it["proj_stock"] + qty) / rate), 3650))).isoformat()
            if rate > 0 else None
        )
        it["why"] = list(dict.fromkeys(list(it.get("why") or []) + ["manual"]))
        it["why_text"] = "изменено вручную"
    # Строки, обнулённые человеком, из плана уходят (в таблице заказа им не
    # место), но НЕ исчезают: «система советовала 48 — человек сказал ноль» —
    # самый ценный сигнал качества рекомендаций, и раньше он не сохранялся
    # нигде. В `not_included` такая позиция тоже не попадала: тот список
    # собирается раньше, внутри планировщика, и правок не видит.
    zeroed = [
        {k: i.get(k) for k in _ZEROED_KEEP if k in i}
        for i in plan["items"]
        if i["qty"] <= 0 and (i.get("qty_recommended") or 0) > 0
    ]
    plan["zeroed"] = zeroed
    plan["items"] = [i for i in plan["items"] if i["qty"] > 0]
    plan["cost_total"] = sum(i["cost_total"] for i in plan["items"])
    # Нельзя молча показывать общий бюджет как полный, если в заказе есть
    # позиции без себестоимости (решение владельца D-23). Их стоимость равна
    # нулю не потому, что они бесплатны, а потому что цифры нет — и об этом
    # обязана говорить сама выдача, а не только подсказка на экране.
    no_cost_rows = [i for i in plan["items"]
                    if i.get("no_cost") or float(i.get("cost_price") or 0) <= 0]
    plan["budget_incomplete"] = ({
        "positions": len(no_cost_rows),
        "units": sum(int(i["qty"]) for i in no_cost_rows),
        "names": [i["base_name"] for i in no_cost_rows[:10]],
    } if no_cost_rows else None)
    # Календарь платежей пересобираем от новой себестоимости, «сейчас» —
    # снова первый транш календаря, а не отдельная формула.
    stages = plan.get("stages") or []
    plan["payments"] = op.payment_plan(
        date.fromisoformat(plan["order_date"]),
        [{"name": st["name"], "lead_days": st["lead_days"],
          "cost_share": st["cost_share"], "prepay_share": st.get("prepay_share", 1.0)}
         for st in stages],
        plan["cost_total"],
    )
    plan["pay_now"] = plan["payments"][0]["amount"] if plan["payments"] else plan["cost_total"]
    plan["pay_later"] = max(0, plan["cost_total"] - plan["pay_now"])
    # Сравниваем с той же базой, что и движок: при нулевой предоплате
    # «деньги на сейчас» не существуют и бюджет меряется полной стоимостью.
    plan["spent"] = (plan["pay_now"] if plan.get("budget_basis") == "now"
                     else plan["cost_total"])
    plan["rest"] = plan["budget"] - plan["reserve_new"] - plan["spent"]
    plan["over_need_cost"] = sum(round(i["over_need"] * i["cost_price"]) for i in plan["items"])
    plan["over_need_profit"] = sum(i.get("over_need_profit", 0) for i in plan["items"])
    plan["covered_until"] = min(
        (i["covered_until"] for i in plan["items"] if i["covered_until"]), default=None)
    plan["covered_full"] = sum(
        1 for i in plan["items"]
        if i["covered_until"] and i["covered_until"] >= (plan.get("covered_until_target") or ""))
    if isinstance(plan.get("lost"), dict):
        short = sum(round(i["unmet"] * max(0, i["avg_price"] - i["cost_price"]))
                    for i in plan["items"])
        plan["lost"]["short"] = short
        cover = plan.get("cover_days") or 1
        share = min(1.0, (plan["lost"].get("next_order_days") or cover) / cover)
        plan["lost"]["at_risk"] = round((plan["lost"]["missing"] + short) * share)
    plan["totals"] = {
        "positions": len(plan["items"]),
        "units": sum(i["qty"] for i in plan["items"]),
        "cost": sum(i["cost_total"] for i in plan["items"]),
        "expected_profit": sum(i["expected_profit"] for i in plan["items"]),
        "expected_revenue": sum(i["qty"] * i["avg_price"] for i in plan["items"]),
    }
    # «А если добавить денег» после ручных правок отвечает на другой вопрос —
    # честнее не показывать, чем показывать устаревшее.
    plan.pop("sensitivity", None)
    stop = []
    if not plan["items"] and not plan.get("new_items"):
        stop.append({"code": "empty", "text": "В плане нет позиций"})
    if plan["rest"] < 0:
        stop.append({"code": "over_budget", "text":
                     f"Заказ выходит за бюджет на {op.fmt_rub(-plan['rest'])}"})
    if plan["order_date"] < plan["today"]:
        stop.append({"code": "past_date", "text":
                     f"Заказ пришлось бы разместить {plan['order_date']} — эта дата уже прошла."})
    plan["stop"] = stop
    plan["can_create"] = not stop
    plan["manual_edit"] = True


@router.get("/order-plan/options")
def api_order_plan_options(
    ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)
):
    """Справочники для анкеты мастера: категории и позиции без себестоимости."""
    snap = analytics.get_snapshot(db, ctx.org)
    cats: dict[str, int] = {}
    no_cost = 0
    for it in snap["items"].values():
        if it.get("archived") or it.get("hidden"):
            continue
        cats[it.get("category") or "Без категории"] = cats.get(
            it.get("category") or "Без категории", 0) + 1
        if not it.get("cost_price"):
            no_cost += 1
    settings = snap.get("settings") or {}
    return {
        "categories": sorted(cats),
        "no_cost_count": no_cost,
        "cost_source_full": sum(1 for it in snap["items"].values() if it.get("cost_is_full")),
        "positions": len(snap["items"]),
        # D-27: в режиме «фиксированный горизонт» ритм заказов в анкете на
        # расчёт не влияет, и об этом надо сказать словами прямо в анкете —
        # иначе человек крутит ручку, а число не меняется.
        "horizon_source": settings.get("cover_mode", "cadence"),
        "horizon_days_effective": settings.get("cover_days"),
        # D-23: чек-лист «что известно о данных этого расчёта». Ровно те же
        # факты, что уходят в запись решения (_decision_record.data_quality) —
        # чтобы на экране и в истории стояло одно и то же. Никаких процентов
        # уверенности и букв HIGH/MEDIUM/LOW: буква читается как вероятность и
        # прячет, ЧЕГО именно не хватает.
        "data_quality": _data_quality(db, ctx, snap),
    }


@router.post("/order-plan/preview")
def api_order_plan_preview(
    body: OrderPlanIn, ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)
):
    """Предпросмотр плана: ничего не сохраняет, дергается на каждое изменение анкеты."""
    return _plan(db, ctx, body)


@router.post("/order-plan")
def api_order_plan_save(
    body: OrderPlanIn, ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)
):
    """Сохранить план (бриф + вывод системы + результат) для истории и предзаполнения."""
    from app.models import OrderPlan
    plan = _plan(db, ctx, body)
    row = OrderPlan(
        org_id=ctx.org.id,
        status="draft",
        created_by=ctx.user.id,
        brief_json=json.dumps(plan["brief"], ensure_ascii=False),
        computed_json=json.dumps(
            {
                "cover_days": plan["cover_days"],
                "order_date": plan["order_date"],
                "covered_until": plan["covered_until"],
                "stages": plan["stages"],
                "lead_days": plan["lead_days"],
                # На какой истории посчитан план (деплой П1): apply спросит
                # осознанное подтверждение, если истории было мало.
                "coverage": plan.get("coverage"),
                # Версия алгоритма, применённые настройки и качество данных —
                # см. _decision_record.
                **(plan.get("record") or {}),
            },
            ensure_ascii=False,
        ),
        result_json=json.dumps(
            {"items": plan["items"], "totals": plan["totals"],
             "spent": plan["spent"], "lost": plan.get("lost"),
             "manual_edit": bool(plan.get("manual_edit")),
             # Позиции, которые человек обнулил вручную, вместе с тем, что
             # по ним рекомендовала система (см. _apply_overrides).
             "zeroed": plan.get("zeroed") or [],
             # Заказ содержит позиции без себестоимости — значит сумма заказа
             # неполна, и в истории это должно быть видно так же, как на экране.
             "budget_incomplete": plan.get("budget_incomplete"),
             # ОТКАЗ — ТОЖЕ РЕШЕНИЕ. Раньше сохранялись только строки плана,
             # а «что не вошло и почему» считалось, показывалось на экране и
             # пропадало. Без этого в истории остаются одни лишь товары,
             # прошедшие фильтры: нельзя увидеть ни того, что система
             # систематически отсеивает целый класс позиций, ни того, во что
             # обошёлся отказ. Хранится в сжатом виде — без ростовок и
             # промежуточных полей.
             "not_included": _slim_excluded(plan.get("not_included")),
             "review": plan.get("review"),
             "moq_skipped": plan.get("moq_skipped"),
             "moq_over_cap": plan.get("moq_over_cap"),
             "blocked": plan.get("blocked")},
            ensure_ascii=False,
        ),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"ok": True, "id": row.id, "plan": plan}


@router.get("/order-plan/last")
def api_order_plan_last(
    ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)
):
    """Последний бриф — чтобы второй заказ оформлялся в два клика."""
    from app.models import OrderPlan
    row = db.execute(
        select(OrderPlan).where(OrderPlan.org_id == ctx.org.id)
        .order_by(OrderPlan.created_at.desc(), OrderPlan.id.desc())
    ).scalars().first()
    if row is None:
        return {"brief": None}
    return {"brief": row.brief, "id": row.id, "created_at": row.created_at.isoformat()}


def _plan_row_out(row, names: dict, prods: dict) -> dict:
    """Строка истории планов: что решили, на сколько и чем кончилось."""
    brief = row.brief
    try:
        result = json.loads(row.result_json or "{}")
    except ValueError:
        result = {}
    totals = result.get("totals") or {}
    return {
        "id": row.id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "status": row.status,
        "order_id": row.production_order_id,
        "author": names.get(row.created_by or 0, ""),
        "production_id": brief.get("production_id"),
        "production_name": prods.get(brief.get("production_id") or 0, ""),
        "budget": int(brief.get("budget") or 0),
        "eta_date": brief.get("eta_date"),
        "positions": int(totals.get("positions") or 0),
        "units": int(totals.get("units") or 0),
        "cost": int(totals.get("cost") or 0),
    }


@router.get("/order-plan/history")
def api_order_plan_history(
    limit: int = 20, ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db)
):
    """Прошлые планы: «повторить как в прошлый раз» и разбор задним числом.

    Аудит 22.08: единственным следом решения был сам заказ — без бюджета, срока
    и условий, при которых он собирался. Бриф хранился, но достать его можно
    было только последним (`/order-plan/last`).
    """
    from app.models import OrderPlan
    rows = db.execute(
        select(OrderPlan).where(OrderPlan.org_id == ctx.org.id)
        .order_by(OrderPlan.created_at.desc(), OrderPlan.id.desc())
        .limit(max(1, min(100, int(limit or 20))))
    ).scalars().all()
    ids = {r.created_by for r in rows if r.created_by}
    names = {}
    if ids:
        names = {
            uid: (name or email or "")
            for uid, name, email in db.execute(
                select(User.id, User.name, User.email).where(User.id.in_(ids))
            ).all()
        }
    prods = {
        p.id: p.name
        for p in db.execute(
            select(Production).where(Production.org_id == ctx.org.id)
        ).scalars()
    }
    return {"plans": [_plan_row_out(r, names, prods) for r in rows]}


@router.get("/order-plan/{plan_id}/brief")
def api_order_plan_brief(
    plan_id: int = _id_path(),
    ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db),
):
    """Анкета прошлого плана — для кнопки «Повторить»."""
    from app.models import OrderPlan
    row = db.get(OrderPlan, plan_id)
    if row is None or row.org_id != ctx.org.id:
        raise HTTPException(404, "План не найден")
    return {"brief": row.brief, "id": row.id,
            "created_at": row.created_at.isoformat() if row.created_at else None}


class PlanApplyIn(BaseModel):
    name: str = Field(default="", max_length=255)
    # Явное «да, это новый заказ» — снимает защиту от дубля.
    force: bool = False
    # Осознанное согласие оформить заказ по плану, посчитанному на неполной
    # истории (первичная загрузка ещё идёт). Предпросмотр остаётся открытым,
    # но превращение плана в заказ на производство — деньги, тут спрашиваем.
    confirm_partial: bool = False


DUP_OVERLAP = 0.6      # доля совпадающих позиций, при которой это похоже на дубль
DUP_WINDOW_DAYS = 14   # и только среди свежих открытых заказов


def _find_duplicate_order(db: Session, org_id: int, production_id, bases: set) -> dict | None:
    """Есть ли уже открытый заказ того же канала с тем же составом."""
    if not bases:
        return None
    since = datetime.utcnow() - timedelta(days=DUP_WINDOW_DAYS)
    rows = db.execute(
        select(ProductionOrder).where(
            ProductionOrder.org_id == org_id,
            ProductionOrder.status.in_(("draft", "sent")),
            ProductionOrder.created_at >= since,
        ).order_by(ProductionOrder.created_at.desc())
    ).scalars().all()
    for o in rows:
        if production_id and o.production_id and o.production_id != int(production_id):
            continue
        other = {str(i.get("base_name")) for i in o.items}
        if not other:
            continue
        overlap = len(bases & other)
        if overlap / max(1, len(bases)) >= DUP_OVERLAP:
            return {"id": o.id, "name": o.name, "overlap": overlap,
                    "created": o.created_at.strftime("%d.%m") if o.created_at else "",
                    "status_text": "черновик" if o.status == "draft" else "в производстве"}
    return None


@router.post("/order-plan/{plan_id}/apply")
def api_order_plan_apply(
    plan_id: int, body: PlanApplyIn,
    ctx: AuthContext = Depends(require_auth_api), db: Session = Depends(get_db),
):
    """План → «Заказ на производство» (дальше существующий путь: в МойСклад, Excel)."""
    from app.models import OrderPlan
    row = db.get(OrderPlan, plan_id)
    if row is None or row.org_id != ctx.org.id:
        raise HTTPException(404, "План не найден")
    if row.production_order_id:
        raise HTTPException(409, "По этому плану заказ уже создан")
    try:
        computed = json.loads(row.computed_json or "{}")
    except ValueError:
        computed = {}
    cov = computed.get("coverage") or {}
    if cov.get("partial") and not body.confirm_partial:
        raise HTTPException(
            409,
            f"План посчитан по {cov.get('days')} дн. истории из "
            f"{cov.get('needed_days')} — она ещё загружается. "
            f"Дождитесь загрузки или подтвердите осознанно",
        )
    try:
        result = json.loads(row.result_json or "{}")
    except ValueError:
        result = {}
    items = [
        {"base_name": i["base_name"], "qty": int(i["qty"]),
         "sizes": i.get("sizes") or {}, "cost": float(i.get("cost_price") or 0)}
        for i in (result.get("items") or []) if int(i.get("qty") or 0) > 0
    ]
    # Новинки, вписанные вручную, — такие же строки заказа (в МойСкладе у них
    # может ещё не быть карточки: писбэк вернёт их в списке unmatched).
    brief = row.brief
    for n in (brief.get("new_items") or []):
        if int(n.get("qty") or 0) > 0:
            items.append({"base_name": n["name"], "qty": int(n["qty"]),
                          "sizes": {}, "cost": float(n.get("cost") or 0)})
    if not items:
        raise HTTPException(422, "В плане нет позиций с количеством > 0")
    pid = brief.get("production_id")
    # Защита от дубля. Аудит 22.08: кнопку «Создать заказ» можно было нажимать
    # сколько угодно раз, и каждый клик делал новый заказ — на потоке это
    # оплаченный дважды заказ. 409 у самого плана защищал только повторное
    # применение ОДНОГО плана, а мастер каждый раз создавал новый.
    if not body.force:
        dup = _find_duplicate_order(db, ctx.org.id, pid, {i["base_name"] for i in items})
        if dup is not None:
            raise HTTPException(409, (
                f"Похоже, такой заказ уже есть: №{dup['id']} «{dup['name']}» от "
                f"{dup['created']} ({dup['status_text']}), {dup['overlap']} из "
                f"{len(items)} позиций совпадают. Проверьте его на странице «Заказ» — "
                f"или подтвердите, что это новый заказ."))
    order = ProductionOrder(
        org_id=ctx.org.id,
        name=(body.name.strip() or f"Заказ от {datetime.now():%d.%m.%Y}"),
        eta_date=brief.get("eta_date"),
        production_id=int(pid) if pid else None,
        created_by=ctx.user.id,
        status="draft",
        items_json=json.dumps(items, ensure_ascii=False),
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    row.production_order_id = order.id
    row.status = "applied"
    db.commit()
    # Снапшот НЕ инвалидируем: черновик заказа не меняет ни остатков, ни продаж,
    # ни «едет к нам» (это происходит при переводе в «в производстве», там
    # invalidate и стоит). Раньше каждый созданный заказ ронял кэш, и следующий
    # экран считался с холодного снапшота — до 30 секунд ожидания на каждый
    # заказ у менеджера, который оформляет их пачкой.
    return {"ok": True, "order_id": order.id, "status": "draft"}


class ProductionSetupIn(BaseModel):
    """Настройка канала производства: этапы и минимальная партия.

    preset — быстрый выбор в анкете: turnkey (под ключ) | fabric_sewing (ткань → пошив).
    stages переопределяет preset, если передан.
    """
    preset: str | None = None
    stages: list[dict] | None = None
    moq_units: int | None = Field(default=None, ge=0, le=10000)
    # Ритм заказов В ЭТОТ канал (0 = общая настройка организации): своё
    # производство часто догружают еженедельно, Китай заказывают раз в сезон.
    cadence_days: int | None = Field(default=None, ge=0, le=365)


@router.post("/productions/{pid}/setup")
def api_production_setup(
    body: ProductionSetupIn,
    pid: int = _id_path(),
    # Этапы, сроки и минимальная партия — это деньги и обязательства перед
    # подрядчиком: менять их может владелец, как и переименование с удалением.
    ctx: AuthContext = Depends(require_owner_api), db: Session = Depends(get_db),
):
    from app import order_planner
    from app.models import Production
    p = db.get(Production, pid)
    if p is None or p.org_id != ctx.org.id:
        raise HTTPException(404, "Производство не найдено")
    raw = body.stages
    if raw is None and body.preset:
        raw = order_planner.STAGE_PRESETS.get(body.preset)
        if raw is None:
            raise HTTPException(422, "Неизвестный пресет этапов")
    if raw is not None:
        # Минимумы по категориям задаются только через API и в экране настроек
        # не показываются — не даём форме молча их стереть: если в присланном
        # этапе поля нет, берём прежнее значение этапа с тем же ключом.
        if body.stages is not None:
            prev = {st.get("key"): st.get("min_by_category")
                    for st in (p.stages or []) if isinstance(st, dict)}
            raw = [
                ({**st, "min_by_category": prev.get(st.get("key")) or {}}
                 if isinstance(st, dict) and "min_by_category" not in st else st)
                for st in raw
            ]
        stages = order_planner.normalize_stages(
            raw, analytics.extra_settings(ctx.org)["lead_time_days"]
        )
        p.stages_json = json.dumps(stages, ensure_ascii=False)
    if body.moq_units is not None:
        p.moq_units = int(body.moq_units)
    if body.cadence_days is not None:
        p.cadence_days = int(body.cadence_days)
    db.commit()
    analytics.invalidate(ctx.org.id)
    return _production_out(p, analytics.extra_settings(ctx.org)["lead_time_days"])

