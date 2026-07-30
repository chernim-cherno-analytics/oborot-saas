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
from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session

from app.crypto import decrypt_token
from app.db import engine
from app.models import Connection, Product, ProductionOrder
from app.ms_client import MoySkladClient

# Имя контрагента-поставщика, на которого оформляется заказ.
AGENT_NAME = "Производство"

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

def ensure_schema() -> None:
    """Добавляет ms_doc_href/ms_doc_name в существующие production_orders.

    Base.metadata.create_all не изменяет существующие таблицы, поэтому у баз,
    созданных до этой фичи, колонок нет. ALTER TABLE ADD COLUMN — аддитивно и
    одинаково работает в SQLite и Postgres. Свежая БД получает колонки из
    модели, тогда таблицы ещё нет — выходим без действий.
    """
    insp = inspect(engine)
    if not insp.has_table("production_orders"):
        return
    cols = {c["name"] for c in insp.get_columns("production_orders")}
    with engine.begin() as conn:
        if "ms_doc_href" not in cols:
            conn.execute(text(
                "ALTER TABLE production_orders "
                "ADD COLUMN ms_doc_href VARCHAR(512) NOT NULL DEFAULT ''"
            ))
        if "ms_doc_name" not in cols:
            conn.execute(text(
                "ALTER TABLE production_orders "
                "ADD COLUMN ms_doc_name VARCHAR(255) NOT NULL DEFAULT ''"
            ))


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

        # 5) Сам документ.
        payload: dict = {
            "organization": {"meta": org_meta},
            "agent": {"meta": agent_meta},
            "positions": positions,
            "description": f"Создано в «Обороте»: заказ «{order.name}»",
        }
        if order.eta_date:
            # Планируемая дата приёмки — из ETA заказа.
            payload["deliveryPlannedMoment"] = f"{order.eta_date} 00:00:00"
        doc = await client.create_purchase_order(payload)

    href = ((doc.get("meta") or {}).get("href")) or ""
    order.ms_doc_href = href
    order.ms_doc_name = str(doc.get("name") or "")
    return {
        "ok": True,
        "ms_doc_name": order.ms_doc_name,
        "ms_doc_href": href,
        "ms_doc_ui_url": ui_url(doc),
        "positions_pushed": len(positions),
        "unmatched": unmatched,
    }
