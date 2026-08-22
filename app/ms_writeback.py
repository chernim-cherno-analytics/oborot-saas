"""Обратная запись заказа на производство в МойСклад.

Владелец собирает заказ в «Обороте» (страница /orders), а в МойСклад до сих
пор перебивал позиции руками. Этот модуль по кнопке «Отправить в МойСклад»
создаёт в аккаунте МС документ **«Заказ поставщику»** (entity/purchaseorder)
с позициями по вариантам-размерам.

Почему purchaseorder, а не processingorder («Заказ на производство»):
processingorder требует техкарту (processingPlan) и доступен только на
тарифах с опцией «Производство»; purchaseorder доступен на всех тарифах,
а семантика «заказали пошив у производства-подрядчика» ложится на него
без натяжек (agent = контрагент «Производство»).

Используемые эндпоинты JSON API 1.2:
  GET  /entity/assortment          — резолв href/type по ext_id наших products;
  GET  /entity/organization        — юрлицо (первое) для поля organization;
  GET  /entity/counterparty?filter=name=Производство — поиск агента;
  POST /entity/counterparty        — создание агента, если его нет;
  POST /entity/purchaseorder       — сам документ (organization, agent,
                                     positions[{assortment.meta, quantity,
                                     price-в-копейках}], deliveryPlannedMoment).

Маппинг позиций: item заказа {base_name, sizes:{size: qty}} → products
текущей org по (base_name, size) → product.ext_id → meta из ассортимента МС.
Позиции, не нашедшие вариант, не валят весь заказ — возвращаются списком
`unmatched` в ответе.

Идемпотентность обеспечивает роут: повторная отправка при заполненном
production_orders.ms_doc_href — 409 «уже отправлен» со ссылкой.
"""
from datetime import date, timedelta

import httpx
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from app.crypto import decrypt_token
from app.db import engine, run_migration_step
from app.models import Connection, OrderedQty, Product, ProductionOrder
from app.ms_client import MoySkladClient

# Имя контрагента-поставщика, на которого оформляется заказ.
AGENT_NAME = "Производство"

# Пометка «идёт отправка» в ms_doc_href (лок в routes_connect): pending:<epoch>.
PENDING_PREFIX = "pending:"


def is_pushed(href: str | None) -> bool:
    """Заказ реально отправлен в МойСклад (есть ссылка на документ, не лок).

    Такой заказ учитывается в «едет к нам» ТОЛЬКО через ordered_qty.ms_qty
    (импорт purchaseorder синком) — статусные переходы в api.py не должны
    двигать локальный qty, иначе двойной счёт.
    """
    h = href or ""
    return bool(h) and not h.startswith(PENDING_PREFIX)

# Веб-интерфейс МойСклад: ссылка на карточку документа по его uuid.
MS_UI_DOC_URL = "https://online.moysklad.ru/app/#purchaseorder/edit?id={uuid}"

DEMO_HINT = (
    "Отправка в МойСклад доступна после подключения МойСклад. "
    "Сейчас организация работает на демо-данных — подключите аккаунт "
    "МойСклад в настройках, и кнопка заработает."
)


class WritebackError(Exception):
    """Ошибка обратной записи с HTTP-статусом и человеческим текстом."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


# ── Аддитивная мини-миграция ─────────────────────────────────────────────────

def ensure_schema(bind=None) -> None:
    """Добавляет ms_doc_href/ms_doc_name в существующие production_orders.

    Base.metadata.create_all не изменяет существующие таблицы, поэтому у баз,
    созданных до этой фичи, колонок нет. ALTER TABLE ADD COLUMN — аддитивно и
    одинаково работает в SQLite и Postgres. Свежая БД получает колонки из
    модели, тогда таблицы ещё нет — выходим без действий.

    Ревью 22.08 (Д4): раньше ALTER выполнялся напрямую и падал на «duplicate
    column» при одновременном старте нескольких воркеров. Теперь — через
    run_migration_step (см. app/db.py), который переживает гонку и на
    SQLite, и на Postgres. Вызывается из app.main._startup() на старте
    приложения, а не на импорте модуля (раньше — routes_connect.py, см. Д4).

    bind — необязательный engine (нужен тестам для «старой» схемы отдельной
    базы); по умолчанию — engine приложения.
    """
    eng = bind or engine
    insp = inspect(eng)
    if not insp.has_table("production_orders"):
        return
    cols = {c["name"] for c in insp.get_columns("production_orders")}
    if "ms_doc_href" not in cols:
        run_migration_step(
            "ALTER TABLE production_orders "
            "ADD COLUMN ms_doc_href VARCHAR(512) NOT NULL DEFAULT ''",
            bind=eng,
        )
    if "ms_doc_name" not in cols:
        run_migration_step(
            "ALTER TABLE production_orders "
            "ADD COLUMN ms_doc_name VARCHAR(255) NOT NULL DEFAULT ''",
            bind=eng,
        )


# ── Вспомогательное ──────────────────────────────────────────────────────────

def _href_uuid(href: str) -> str:
    """UUID сущности из meta.href (query-параметры отбрасываются)."""
    if not href:
        return ""
    return href.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]

def ui_url(doc: dict | None = None, href: str = "") -> str:
    """Ссылка на документ в веб-интерфейсе МойСклад.

    Предпочитаем meta.uuidHref из ответа МС; если его нет (или известен
    только сохранённый href) — строим по шаблону из uuid href-а.
    """
    meta = (doc or {}).get("meta") or {}
    uuid_href = meta.get("uuidHref")
    if uuid_href:
        return str(uuid_href)
    uuid = _href_uuid(meta.get("href") or href)
    return MS_UI_DOC_URL.format(uuid=uuid) if uuid else ""


def _kopecks_of(rub: float) -> int:
    """Рубли → копейки (цены МойСклад — в копейках)."""
    try:
        return int(round(float(rub or 0) * 100))
    except (TypeError, ValueError):
        return 0


def _item_size_breakdown(item: dict) -> list[tuple[str, int]]:
    """Разбивка позиции заказа: [(size, qty>0)].

    sizes может содержать ключ '' (безразмерный товар). Если sizes пуст —
    вся позиция идёт одной строкой с size='' и количеством item.qty.
    """
    sizes = item.get("sizes") or {}
    out = [(str(s), int(q)) for s, q in sizes.items() if int(q or 0) > 0]
    if out:
        return out
    qty = int(item.get("qty") or 0)
    return [("", qty)] if qty > 0 else []


def _position_label(base_name: str, size: str) -> str:
    return f"{base_name} ({size})" if size else base_name


def _move_incoming_to_ms(db: Session, org_id: int, order: ProductionOrder,
                         pushed_by_base: dict[str, int]) -> None:
    """Перенос вклада заказа в «едет к нам» с локального qty на ms_qty.

    С момента отправки источник истины по этому заказу — документ в МойСклад
    (следующий синк посчитает его из purchaseorder, приёмки снимут принятое).
    Здесь: (а) если заказ уже был «В производстве» — снимаем его прежний
    локальный вклад из qty (зеркало _apply_order_to_incoming(+1) в api.py,
    полное количество позиции — как и добавлялось); (б) отправленные позиции
    сразу прибавляем к ms_qty, чтобы «едет» не мигал до ближайшего синка.

    Документ создали мы сами, поэтому та же величина идёт и в ms_qty_tracked
    (D-28): между отправкой и ближайшим синком «едет по заказам „Оборота“» не
    должно проваливаться в ноль. Синк потом пересчитает обе величины заново —
    уже по доказуемой связи, а не по нашему знанию в моменте.
    """
    was_sent = order.status == "sent"
    touched: dict[str, OrderedQty] = {}

    def _row(base: str) -> OrderedQty:
        if base not in touched:
            row = db.get(OrderedQty, (org_id, base))
            if row is None:
                row = OrderedQty(org_id=org_id, base_name=base, qty=0.0,
                                 ms_qty=0.0, ms_qty_tracked=0.0)
                db.add(row)
            touched[base] = row
        return touched[base]

    if was_sent:
        for item in order.items:
            base, qty = str(item.get("base_name") or ""), int(item.get("qty") or 0)
            if base and qty > 0:
                row = _row(base)
                row.qty = max(0.0, row.qty - qty)
    for base, qty in pushed_by_base.items():
        row = _row(base)
        row.ms_qty = row.ms_qty + qty
        row.ms_qty_tracked = (row.ms_qty_tracked or 0.0) + qty


# ── Основной сценарий ────────────────────────────────────────────────────────

def _get_ms_token(db: Session, org_id: int) -> str:
    """Токен активного подключения МойСклад; демо/отсутствие — честный отказ."""
    conn = db.execute(
        select(Connection).where(
            Connection.org_id == org_id, Connection.kind == "moysklad"
        )
    ).scalars().first()
    token = decrypt_token(conn.token_enc) if conn and conn.token_enc else None
    if not token:
        raise WritebackError(409, DEMO_HINT)
    return token


def _product_map(db: Session, org_id: int) -> dict[tuple[str, str], Product]:
    """(base_name, size) → Product с непустым ext_id (варианты/товары МС)."""
    rows = db.execute(
        select(Product).where(Product.org_id == org_id, Product.ext_id != "")
    ).scalars().all()
    return {(p.base_name, p.size): p for p in rows}


# Сколько дней назад искать «свой» документ перед созданием. Заказ отправляют
# в день оформления; две недели — запас на «нажал, не дошло, вернулся завтра».
LOOKBACK_DAYS = 14


def order_marker(order_id: int) -> str:
    """Метка нашего заказа в описании документа МойСклад.

    Формат намеренно машинный и стабильный: имя заказа человек может
    переименовать, а метка остаётся. По ней документ узнаётся при повторе.
    """
    return f"[oborot#{int(order_id)}]"


class AmbiguousExistingOrder(Exception):
    """Маркер нашёлся больше чем у одного документа — связывать вслепую нельзя.

    `after_create` различает два очень разных случая:
      • False — искали ПЕРЕД созданием: нового документа точно нет;
      • True  — искали ПОСЛЕ неудачной отправки: документ мог быть создан,
        и обещать «ничего не создано» здесь было бы враньём. Хуже того,
        совет «уберите метку у лишних» в этом случае опасен: сняв метку с
        только что созданного (его не отличить), человек получит второй заказ
        поставщику — ровно тот дубль, ради которого маркер и заведён.
    """

    def __init__(self, docs: list[dict], after_create: bool = False):
        self.docs = docs
        self.after_create = after_create
        names = ", ".join(str(d.get("name") or "?") for d in docs[:5])
        super().__init__(names)


async def find_existing_order(client, marker: str, *,
                              after_create: bool = False) -> dict | None:
    """Ищет в МойСкладе документ, созданный нами по этому заказу.

    Смотрим описания «Заказов поставщику» за последние LOOKBACK_DAYS дней.
    Фильтровать по подстроке на стороне МС нельзя, поэтому тянем список без
    позиций (дёшево) и сверяем описания у себя. Ошибку поиска НЕ проглатываем
    молча наверх: если мы не смогли проверить, лучше не создавать документ
    вслепую — пусть вызывающий решает.

    Найдено НЕСКОЛЬКО — поднимаем AmbiguousExistingOrder вместо того, чтобы
    взять первый попавшийся. Так бывает не в теории: «Копировать документ»
    в МойСкладе переносит и описание вместе с маркером, и тогда у двух разных
    документов одна и та же метка. Взять любой означало бы привязать наш заказ
    к чужой бумаге и дальше считать по ней «едет к нам». Отказ с перечислением
    номеров — единственное честное поведение: разобраться может только человек,
    который эти документы видит.
    """
    since = (date.today() - timedelta(days=LOOKBACK_DAYS)).isoformat()
    found = [row for row in await client.search_purchase_orders(since)
             if marker in str(row.get("description") or "")]
    if len(found) > 1:
        raise AmbiguousExistingOrder(found, after_create=after_create)
    return found[0] if found else None


async def push_order(db: Session, org_id: int, order: ProductionOrder) -> dict:
    """Создаёт «Заказ поставщику» в МойСклад из позиций заказа.

    Возвращает {ok, ms_doc_name, ms_doc_href, ms_doc_ui_url,
    positions_pushed, unmatched:[...]}. Коммит БД — на вызывающей стороне.
    """
    token = _get_ms_token(db, org_id)
    products = _product_map(db, org_id)

    async with MoySkladClient(token) as client:
        # 1) Ассортимент МС: ext_id → meta (точный href и type variant/product).
        #    Не строим href руками — берём как отдаёт МС, это защищает от
        #    рассинхрона (удалённые/архивные позиции просто не найдутся).
        assortment_meta: dict[str, dict] = {}
        for row in await client.fetch_assortment():
            ext = row.get("id") or _href_uuid(((row.get("meta") or {}).get("href")) or "")
            meta = row.get("meta") or {}
            if ext and meta.get("href"):
                assortment_meta[ext] = {
                    "href": meta["href"],
                    "type": meta.get("type") or "product",
                    "mediaType": "application/json",
                }

        # 2) Позиции документа: base_name+size → product.ext_id → meta МС.
        positions: list[dict] = []
        unmatched: list[str] = []
        pushed_by_base: dict[str, int] = {}  # для переноса вклада в ms_qty
        for item in order.items:
            base = str(item.get("base_name") or "")
            cost_kopecks = _kopecks_of(item.get("cost"))
            for size, qty in _item_size_breakdown(item):
                product = products.get((base, size))
                meta = assortment_meta.get(product.ext_id) if product else None
                if meta is None:
                    unmatched.append(_position_label(base, size))
                    continue
                positions.append({
                    "assortment": {"meta": meta},
                    "quantity": qty,
                    "price": cost_kopecks,
                })
                pushed_by_base[base] = pushed_by_base.get(base, 0) + qty

        if not positions:
            raise WritebackError(
                422,
                "Ни одна позиция заказа не сопоставилась с товарами МойСклад. "
                "Проверьте, что синхронизация выполнена и названия позиций "
                "совпадают с ассортиментом МС.",
            )

        # 3) Юрлицо (organization) — первое в аккаунте.
        orgs = await client.fetch_organizations()
        if not orgs:
            raise WritebackError(
                409, "В аккаунте МойСклад не найдено юрлицо (organization) — "
                     "создайте его в МойСклад и повторите.",
            )
        org_meta = (orgs[0].get("meta") or {})

        # 4) Контрагент «Производство»: найти или создать.
        agent = await client.find_counterparty_by_name(AGENT_NAME)
        if agent is None:
            agent = await client.create_counterparty(AGENT_NAME)
        agent_meta = (agent.get("meta") or {})

        # 5) Сам документ — но сначала проверяем, не создан ли он уже.
        #
        # У JSON API 1.2 нет ключа идемпотентности, а сеть даёт три исхода,
        # а не два: «создан», «не создан» и «неизвестно» (таймаут, 502, обрыв).
        # В третьем случае документ у клиента может уже существовать, и вторая
        # попытка сделала бы ДУБЛЬ заказа поставщику — с деньгами и с обещанием
        # подрядчику. Поэтому маркер в описании + поиск по нему до создания.
        marker = order_marker(order.id)
        existing = await find_existing_order(client, marker)
        if existing is not None:
            doc, recovered = existing, True
        else:
            payload: dict = {
                "organization": {"meta": org_meta},
                "agent": {"meta": agent_meta},
                "positions": positions,
                "description": f"Создано в «Обороте»: заказ «{order.name}» {marker}",
            }
            if order.eta_date:
                # Планируемая дата приёмки — из ETA заказа.
                payload["deliveryPlannedMoment"] = f"{order.eta_date} 00:00:00"
            recovered = False
            try:
                doc = await client.create_purchase_order(payload)
            except (httpx.HTTPError, httpx.HTTPStatusError):
                # Ответ не дошёл — «создан или нет» отсюда не видно.
                # Единственный честный способ узнать: спросить у МойСклада.
                found = await find_existing_order(client, marker, after_create=True)
                if found is None:
                    raise
                # Документ всё-таки создан — потерялся только ответ.
                doc, recovered = found, True

    href = ((doc.get("meta") or {}).get("href")) or ""
    _move_incoming_to_ms(db, org_id, order, pushed_by_base)
    order.ms_doc_href = href
    order.ms_doc_name = str(doc.get("name") or "")
    return {
        "ok": True,
        "ms_doc_name": order.ms_doc_name,
        "ms_doc_href": href,
        "ms_doc_ui_url": ui_url(doc),
        "positions_pushed": len(positions),
        "unmatched": unmatched,
        # True — документ уже существовал в МойСкладе и был подобран по маркеру,
        # а не создан заново. Значит, прошлая попытка на самом деле удалась,
        # просто ответ до нас не дошёл.
        "recovered": recovered,
    }
