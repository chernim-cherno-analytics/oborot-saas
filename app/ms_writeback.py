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
  GET  /entity/counterparty?filter=syncId=… / filter=name=… — поиск агента;
  POST /entity/counterparty        — создание агента (идемпотентно, syncId);
  GET  /entity/purchaseorder?filter=syncId=… — «не создан ли уже наш документ»;
  POST /entity/purchaseorder       — сам документ (organization, agent, syncId,
                                     positions[{assortment.meta, quantity,
                                     price-в-копейках}], deliveryPlannedMoment).

Маппинг позиций: item заказа {base_name, sizes:{size: qty}} → products
текущей org по (base_name, size) → product.ext_id → meta из ассортимента МС.
Позиции, не нашедшие вариант, не валят весь заказ — возвращаются списком
`unmatched` в ответе.

── Как здесь устроена безопасность (DATA-1/DATA-2) ──────────────────────────

Отправка — это создание ФИНАНСОВОГО документа, у которого три исхода, а не
два: «создан», «не создан» и «НЕИЗВЕСТНО». Поэтому она разрезана на две
транзакции с сетью строго между ними:

  T1 (begin_push) — до сети. CAS-пометка «идёт отправка» плюс рождение двух
     ключей: ms_sync_id заказа и ms_agent_sync_id организации. Оба уходят в
     МойСклад полем `syncId`; повторный POST с занятым ключом ОБНОВЛЯЕТ уже
     созданную сущность, а не заводит вторую. Ключ обязан быть закоммичен
     раньше сети — иначе смерть процесса между POST и записью снова даёт
     дубль. Ветки «отправить без ключа» нет (ms_client её запрещает).

  сеть  — ровно один POST документа, без слепых повторов на таймаутах.

  T2 (commit_push) — после сети. Ссылка на документ и перенос вклада
     «едет к нам» с локального qty на ms_qty — ОДНОЙ транзакцией. Прежний
     фолбэк «сохраним хотя бы ссылку» убран: он превращал сбой в вечный
     двойной счёт без следа в логах. Не вышло дважды — WritebackUnknown,
     честный третий исход; ближайший синк свяжет документ с заказом по
     syncId сам (app/ms_sync._backmatch_by_sync_id).

Поиск «своего» документа по метке `[oborot#N]` в описании остался ТОЛЬКО у
строк, явно помеченных миграцией как legacy: `N` — это переиспользуемый rowid
SQLite, а описание правит человек. См. find_own_document.
"""
import uuid
from datetime import date, timedelta

import httpx
from sqlalchemy import case, func, inspect, select
from sqlalchemy import update as sa_update
from sqlalchemy.orm import Session

from app.crypto import decrypt_token
from app.db import engine, run_migration_step
from app.models import Connection, OrderedQty, Product, ProductionOrder
from app.ms_client import (
    MoySkladClient,
    SyncIdLookupUnavailable,
    SyncIdNotUnique,
)

# Имя контрагента-поставщика, на которого оформляется заказ.
AGENT_NAME = "Производство"

# Пометка «идёт отправка» в ms_doc_href (лок в routes_connect): pending:<epoch>.
PENDING_PREFIX = "pending:"

# Способ поиска «своего» документа в МойСкладе (production_orders.ms_lookup_mode).
LOOKUP_SYNC = "sync"      # только по ms_sync_id — новый протокол
LOOKUP_LEGACY = "legacy"  # ещё разрешён поиск по метке [oborot#N] в описании


def is_legacy_lookup(mode: str | None) -> bool:
    """Разрешён ли этой строке поиск документа по метке в описании.

    Правило намеренно «всё, что не sync — legacy», а не наоборот. Пустое
    значение бывает ровно в одном случае: строку вставил процесс со старым
    кодом уже после ALTER TABLE, то есть это действительно заказ старого
    протокола. Новый код НИКОГДА не вставляет пустое: у модели питоновский
    default='sync', и INSERT всегда несёт колонку явно.
    """
    return (mode or "") != LOOKUP_SYNC


def is_pushed(href: str | None) -> bool:
    """Заказ реально отправлен в МойСклад (есть ссылка на документ, не лок).

    Такой заказ учитывается в «едет к нам» ТОЛЬКО через ordered_qty.ms_qty
    (импорт purchaseorder синком) — статусные переходы в api.py не должны
    двигать локальный qty, иначе двойной счёт.
    """
    h = href or ""
    return bool(h) and not h.startswith(PENDING_PREFIX)


# Текст отказа для операций, столкнувшихся с идущей отправкой. Один на всех:
# 409 обязан звучать одинаково и в статусе, и в удалении — человек читает
# его в одном и том же месте интерфейса.
PUSH_IN_PROGRESS = (
    "По этому заказу сейчас идёт отправка в МойСклад. Дождитесь её "
    "завершения и обновите страницу: пока документ создаётся, менять "
    "заказ нельзя — иначе одно и то же уедет дважды."
)


def not_pushing_clause():
    """SQL-условие «по заказу сейчас НЕ идёт отправка» — для WHERE изменения.

    Почему условием в SQL, а не проверкой перед изменением. Между «прочитали
    ms_doc_href» и «выполнили UPDATE/DELETE» помещается вся транзакция T1
    отправки: предварительная проверка честно увидит «отправки нет», а
    изменение уедет уже поверх захваченного лока (TOCTOU). Тогда у гонки два
    победителя: статус успевает добавить локальный вклад, которого T2 не
    ждёт, а удаление оставляет в МойСкладе финансовый документ, к которому у
    нас больше нет ни заказа, ни ключа для обратной привязки.

    Условие внутри самой изменяющей операции делает исход ОДНИМ: либо строка
    изменена (значит, отправка не начиналась), либо не изменена ни одна
    (значит, начиналась) — третьего состояния не существует.

    coalesce — на случай NULL из строк, вставленных до появления колонки:
    `NULL NOT LIKE …` даёт NULL, то есть строка молча выпала бы из-под
    изменения и обычное удаление сломалось бы на ровном месте.
    """
    return func.coalesce(ProductionOrder.ms_doc_href, "").notlike(
        f"{PENDING_PREFIX}%")


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


class WritebackUnknown(Exception):
    """Документ в МойСкладе создан, а сохранить это у себя не удалось.

    Третий исход, который нельзя называть ни успехом, ни отказом. Документ
    существует (мы держим в руках его номер и ссылку), но наша транзакция
    T2 — «записать ссылку и перенести вклад „едет к нам“» — не прошла ни с
    первого раза, ни с повтора. Утверждать «документ не создан» здесь было бы
    прямым враньём, а молча считать успехом — потерять деньги в отчётах.

    Ключ идемпотентности при этом остаётся в строке заказа, поэтому повтор
    отправки безопасен: он пойдёт с тем же syncId и не задвоит документ.
    """

    def __init__(self, doc_name: str, doc_href: str) -> None:
        super().__init__(doc_name or doc_href)
        self.doc_name = doc_name
        self.doc_href = doc_href


class AmbiguousCounterparty(Exception):
    """Контрагентов с именем «Производство» несколько — выбрать нельзя.

    Автоматический выбор (первый попавшийся, старейший, любой) отклонён
    сознательно: заказ поставщику — финансовый документ и обещание конкретному
    подрядчику. Отправить его «какому-нибудь Производству» хуже, чем не
    отправить вовсе, потому что ошибка обнаружится у контрагента, а не у нас.
    """

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.names = [str(r.get("name") or "?") for r in rows[:5]]
        self.ids = [str(r.get("id") or "") for r in rows[:5]]
        super().__init__(", ".join(f"{n} ({i})" for n, i in zip(self.names, self.ids)))


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
    # DATA-1: ключ идемпотентности и ЯВНЫЙ дискриминатор способа поиска.
    if "ms_sync_id" not in cols:
        run_migration_step(
            "ALTER TABLE production_orders "
            "ADD COLUMN ms_sync_id VARCHAR(36) NOT NULL DEFAULT ''",
            bind=eng,
        )
    if "ms_lookup_mode" not in cols:
        run_migration_step(
            "ALTER TABLE production_orders "
            "ADD COLUMN ms_lookup_mode VARCHAR(16) NOT NULL DEFAULT ''",
            bind=eng,
        )
    # Каждая существующая строка получает ЯВНУЮ пометку legacy: её документ
    # мог быть создан старым кодом, без syncId, и единственный его след —
    # метка в описании. Отнять у таких строк поиск по метке значит создать им
    # дубль при следующей отправке.
    #
    # Шаг выполняется на КАЖДОМ старте, а не один раз рядом с ALTER, и в этом
    # весь смысл. Деплой без простоя означает, что рядом ещё живёт процесс со
    # старым кодом: строка, вставленная им через секунду после ALTER, придёт
    # с пустым ms_lookup_mode — и это действительно заказ старого протокола,
    # который обязан получить 'legacy'. Обратная ошибка (пометить legacy
    # НОВУЮ строку) здесь невозможна: новый код всегда вставляет 'sync' явно,
    # поэтому под WHERE ms_lookup_mode='' новая строка не попадает НИКОГДА.
    run_migration_step(
        "UPDATE production_orders SET ms_lookup_mode='legacy' "
        "WHERE ms_lookup_mode IS NULL OR ms_lookup_mode=''",
        bind=eng,
    )
    # DATA-2: стабильная привязка контрагента-производства к организации.
    if insp.has_table("connections"):
        conn_cols = {c["name"] for c in insp.get_columns("connections")}
        if "ms_agent_sync_id" not in conn_cols:
            run_migration_step(
                "ALTER TABLE connections "
                "ADD COLUMN ms_agent_sync_id VARCHAR(36) NOT NULL DEFAULT ''",
                bind=eng,
            )
        if "ms_agent_href" not in conn_cols:
            run_migration_step(
                "ALTER TABLE connections "
                "ADD COLUMN ms_agent_href VARCHAR(512) NOT NULL DEFAULT ''",
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
                         pushed_by_base: dict[str, int], was_sent: bool) -> None:
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

    `was_sent` приходит СНАРУЖИ и читается из той же транзакции, что и запись
    ссылки (RETURNING в _commit_push_once). Брать его из order.status нельзя:
    ORM-объект заказа загружен ДО сети, а за время сетевого окна статус мог
    измениться. Раньше это спасал только побочный эффект db.rollback() в
    начале T2 (он обесценивает объект, и следующее обращение перечитывает
    строку) — то есть корректность держалась на неочевидном поведении сессии,
    а не на явном чтении.
    """
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


# ── T1: ключи идемпотентности рождаются ДО сети ──────────────────────────────

class PushKeys:
    """Что должно существовать в базе ДО единственного сетевого вызова."""

    __slots__ = ("sync_id", "lookup_mode", "agent_sync_id", "agent_href")

    def __init__(self, sync_id: str, lookup_mode: str,
                 agent_sync_id: str, agent_href: str) -> None:
        self.sync_id = sync_id
        self.lookup_mode = lookup_mode
        self.agent_sync_id = agent_sync_id
        self.agent_href = agent_href


def load_push_keys(db: Session, org_id: int, order_id: int) -> PushKeys:
    """Читает ключи из БАЗЫ, а не из ORM-объекта в памяти.

    Сессия проекта живёт с expire_on_commit=False: после коммита T1 объект
    заказа в памяти всё ещё помнит СТАРЫЕ значения. Читать ключ оттуда значит
    отправить документ со старым (пустым) ключом — то есть потерять всю
    идемпотентность на ровном месте.
    """
    row = db.execute(
        select(ProductionOrder.ms_sync_id, ProductionOrder.ms_lookup_mode)
        .where(ProductionOrder.id == order_id, ProductionOrder.org_id == org_id)
    ).first()
    conn = db.execute(
        select(Connection.ms_agent_sync_id, Connection.ms_agent_href)
        .where(Connection.org_id == org_id, Connection.kind == "moysklad")
    ).first()
    return PushKeys(
        sync_id=str((row[0] if row else "") or ""),
        lookup_mode=str((row[1] if row else "") or ""),
        agent_sync_id=str((conn[0] if conn else "") or ""),
        agent_href=str((conn[1] if conn else "") or ""),
    )


def begin_push(db: Session, org_id: int, order_id: int,
               expected_href: str, pending_href: str) -> bool:
    """T1: захват лока и рождение ключей — ОДНОЙ транзакцией, до сети.

    Три вещи обязаны стать фактом в базе раньше, чем мы тронем сеть:
      • пометка «идёт отправка» (CAS по прежнему значению ms_doc_href —
        второй одновременный клик не обновит ни строки и получит 409);
      • ms_sync_id заказа — ключ идемпотентности документа;
      • ms_agent_sync_id организации — ключ идемпотентности контрагента.

    Ключи выставляются УСЛОВНО в самом SQL (`CASE WHEN ... = ''`), а не
    сравнением в Python: повтор отправки обязан идти с ТЕМ ЖЕ ключом, иначе
    вторая попытка создаст второй документ. Условие в SQL делает это правдой
    и при гонке двух процессов.

    Возвращает True, если лок захвачен именно нами.
    """
    fresh_doc_key = str(uuid.uuid4())
    locked = db.execute(
        sa_update(ProductionOrder)
        .where(
            ProductionOrder.id == order_id,
            ProductionOrder.org_id == org_id,
            ProductionOrder.ms_doc_href == expected_href,  # CAS
        )
        .values(
            ms_doc_href=pending_href,
            ms_sync_id=case(
                (func.coalesce(ProductionOrder.ms_sync_id, "") == "", fresh_doc_key),
                else_=ProductionOrder.ms_sync_id,
            ),
        )
    ).rowcount > 0
    if locked:
        # Ключ контрагента — на организацию, а не на заказ: агент один, и
        # два одновременных push обязаны создать ОДНОГО.
        db.execute(
            sa_update(Connection)
            .where(
                Connection.org_id == org_id,
                Connection.kind == "moysklad",
                func.coalesce(Connection.ms_agent_sync_id, "") == "",
            )
            .values(ms_agent_sync_id=str(uuid.uuid4()))
        )
    db.commit()
    return locked


# ── Контрагент ───────────────────────────────────────────────────────────────

def _agent_meta_of(href: str) -> dict:
    return {"meta": {"href": href, "type": "counterparty",
                     "mediaType": "application/json"}}


def _remember_agent(db: Session, org_id: int, href: str) -> None:
    """Закрепляет выбранного контрагента за организацией (отдельной транзакцией).

    Отдельная короткая транзакция намеренно: к моменту вызова заказ держит
    пометку pending, и подмешивать привязку агента в будущий T2 значило бы
    терять её при каждом откате T2. Агент — факт про организацию, а не про
    конкретную отправку.
    """
    if not href:
        return
    db.rollback()
    db.execute(
        sa_update(Connection)
        .where(Connection.org_id == org_id, Connection.kind == "moysklad")
        .values(ms_agent_href=href)
    )
    db.commit()


def _forget_agent(db: Session, org_id: int, stale_href: str) -> None:
    """Снимает закрепление контрагента, которого в МойСкладе больше нет.

    Условием в самом UPDATE стоит ТОТ href, который мы только что проверили и
    признали мёртвым. Между проверкой и этой записью помещается чужая
    отправка, успевшая закрепить нового живого контрагента, — и затирать её
    работу мы не вправе. Та же логика, что у CAS-пометки отправки: не наше
    значение — не наша строка.
    """
    if not stale_href:
        return
    db.rollback()
    db.execute(
        sa_update(Connection)
        .where(Connection.org_id == org_id, Connection.kind == "moysklad",
               func.coalesce(Connection.ms_agent_href, "") == stale_href)
        .values(ms_agent_href="")
    )
    db.commit()


async def resolve_agent(db: Session, org_id: int, client, keys: PushKeys) -> dict:
    """Контрагент «Производство»: стабильная привязка, а не «найти или создать».

    Порядок ровно такой и по одной причине на шаг:
      1) уже закреплённая ссылка — используем её, если сущность ещё
         существует: решение про то, КОМУ уходит финансовый документ,
         принимается один раз и не пересматривается при каждой отправке;
      2) поиск по НАШЕМУ syncId — закрывает случай «создали, ответ потеряли»:
         по имени такого агента не отличить от одноимённого чужого;
      3) поиск по имени: ноль — создаём идемпотентно (тот же syncId, поэтому
         два одновременных клика дают ОДНОГО агента); ровно один — закрепляем;
         больше одного — отказ с перечислением.

    Автоматический выбор при нескольких совпадениях (первый, старейший)
    отклонён владельцем решения: см. AmbiguousCounterparty.

    Почему шаг 1 всё-таки ходит в сеть — ревью Codex, P2. Закреплённая ссылка
    возвращалась вслепую, а контрагента в МойСкладе могли удалить. С этого
    момента КАЖДЫЙ POST заказа падал валидацией «контрагент не найден», ссылка
    у нас оставалась прежней, и повтор не лечился никогда — даже когда в
    аккаунте есть подходящий контрагент и достаточно было бы его найти. Один
    дешёвый GET на отправку (а отправка — ручное действие человека, не горячий
    путь) превращает вечный отказ в самовосстановление.

    Пересмотром решения это не является: проверяется существование ИМЕННО той
    сущности, которую выбрали, а не «не появился ли кто-то лучше». И забываем
    привязку только по ответу «её нет» (404/410) — граница проведена в
    MoySkladClient.entity_exists, и она односторонняя намеренно: транзиентный
    сбой, сброшенный как «удалено», завёл бы клиенту второго подрядчика.
    """
    if keys.agent_href:
        if await client.entity_exists(keys.agent_href):
            return _agent_meta_of(keys.agent_href)
        _forget_agent(db, org_id, keys.agent_href)
    if not keys.agent_sync_id:
        raise WritebackError(
            500, "Внутренняя ошибка: ключ контрагента не создан до отправки.",
        )
    try:
        found = await client.find_counterparty_by_sync_id(keys.agent_sync_id)
    except SyncIdLookupUnavailable as exc:
        # «Не знаю, есть ли уже наш контрагент» — не повод создавать ещё
        # одного: у клиента появился бы второй «Производство», и половина
        # заказов уехала бы не на того.
        raise WritebackError(
            502,
            "Не удалось достоверно проверить, заведён ли уже контрагент "
            f"«{AGENT_NAME}» в МойСкладе ({exc}). Отправка остановлена, "
            "документ не создан — повторите позже.",
        ) from exc
    except SyncIdNotUnique as exc:
        raise WritebackError(
            409,
            f"В МойСкладе несколько контрагентов с нашим служебным ключом "
            f"({exc}). Это нарушение уникальности на стороне МойСклада: "
            "выбрать за вас, кому уходит заказ, мы не вправе. Документ не "
            "создан — обратитесь в поддержку.",
        ) from exc
    if found is None:
        rows = await client.find_counterparties_by_name(AGENT_NAME)
        if len(rows) > 1:
            raise AmbiguousCounterparty(rows)
        found = rows[0] if rows else await client.create_counterparty(
            AGENT_NAME, keys.agent_sync_id)
    href = ((found.get("meta") or {}).get("href")) or ""
    _remember_agent(db, org_id, href)
    return {"meta": found.get("meta") or {}}


# ── Поиск «своего» документа ─────────────────────────────────────────────────

async def find_own_document(client, keys: PushKeys, marker: str, *,
                            after_create: bool = False) -> dict | None:
    """Уже созданный нами документ этого заказа — или None.

    Единственный признак для НОВЫХ заказов — ms_sync_id: он наш, машинный,
    уникален в аккаунте МойСклад и не живёт в тексте, который правит человек.
    Поиск по метке `[oborot#N]` в описании остаётся ТОЛЬКО у явно помеченных
    legacy-строк, и вот почему это не «перестраховка»:

      • `N` — это rowid SQLite, он переиспользуется после удаления строки.
        Новый заказ на освободившемся rowid находил по метке документ
        УДАЛЁННОГО заказа и «усыновлял» его: своего документа не создавалось,
        а «едет к нам» считалось по чужой бумаге;
      • попытка, умершая после T1, но до POST, при повторе идёт этим же
        путём — то есть тоже могла усыновить чужое.

    Поэтому признак legacy — ЯВНЫЙ и записанный миграцией, а не выведенный из
    «какое-то поле непусто»: после T1 непустое поле есть и у нового заказа.
    """
    try:
        docs = await client.find_purchase_orders_by_sync_id(keys.sync_id)
    except SyncIdLookupUnavailable as exc:
        # Самое опасное место всего механизма. Пустой ответ здесь означает
        # «нашего документа нет» и разрешает создать его заново. Недосмотренный
        # перебор выдать за пустой ответ нельзя: это и есть тот второй заказ
        # поставщику, ради недопущения которого написан весь syncId.
        raise WritebackError(
            502,
            f"Не удалось достоверно проверить, создан ли уже этот заказ в "
            f"МойСкладе ({exc}). Отправка остановлена, чтобы не создать "
            "второй документ. Повторите позже — повтор пойдёт с тем же "
            "ключом.",
        ) from exc
    if len(docs) > 1:
        # Контракт JSON API 1.2 обещает уникальность syncId. Если обещание
        # нарушено, выбирать «какой-нибудь» нельзя тем более.
        raise AmbiguousExistingOrder(docs, after_create=after_create)
    if docs:
        return docs[0]
    if is_legacy_lookup(keys.lookup_mode):
        return await find_existing_order(client, marker, after_create=after_create)
    return None


# ── T2: ссылка и перенос вклада — одной транзакцией ──────────────────────────

def _commit_push_once(db: Session, org_id: int, order: ProductionOrder,
                      href: str, name: str,
                      pushed_by_base: dict[str, int],
                      pending_href: str) -> bool | None:
    """Одна попытка T2. True — записано, None — лок уже не наш, иначе исключение."""
    db.rollback()
    # RETURNING отдаёт статус ровно той строки, которую мы сейчас изменили, и
    # ровно в момент изменения: решение «снимать ли локальный вклад заказа»
    # обязано опираться на состояние внутри этой транзакции, а не на
    # ORM-объект, прочитанный до сетевого окна.
    saved = db.execute(
        sa_update(ProductionOrder)
        .where(
            ProductionOrder.id == order.id,
            ProductionOrder.org_id == org_id,
            # CAS: пишем ссылку только поверх СВОЕЙ пометки «идёт отправка» —
            # ровно того токена, который записал НАШ T1.
            #
            # Здесь стоял `LIKE pending:%`, и комментарий обещал «своей», а SQL
            # обеспечивал «любой». Разница не косметическая: пометка живёт TTL,
            # и по его истечении её законно перехватывает соседняя попытка.
            # Попытка, вернувшаяся из сети позже своего TTL, проходила этот CAS
            # поверх ЧУЖОЙ пометки и записывала свой href — то есть привязывала
            # заказ к своему документу и снимала локальный вклад, пока законный
            # владелец ещё был в сети. Равенство делает владение проверяемым
            # (ревью Codex, раунд 3; воспроизведено на exact HEAD d7792fe0).
            ProductionOrder.ms_doc_href == pending_href,
        )
        .values(ms_doc_href=href, ms_doc_name=name, ms_lookup_mode=LOOKUP_SYNC)
        .returning(ProductionOrder.status)
        .execution_options(synchronize_session=False)
    ).fetchall()
    if not saved:
        db.rollback()
        return None
    _move_incoming_to_ms(db, org_id, order, pushed_by_base,
                         was_sent=str(saved[0][0] or "") == "sent")
    db.commit()
    return True


def commit_push(db: Session, org_id: int, order: ProductionOrder, doc: dict,
                pushed_by_base: dict[str, int], pending_href: str) -> str:
    """T2: ссылка на документ и перенос вклада «едет к нам» — либо оба, либо ни один.

    Раньше здесь был фолбэк «не вышло целиком — сохраним хотя бы ссылку».
    Он превращал сбой в ТИХУЮ порчу данных: ссылка есть, значит заказ считается
    отправленным и его локальный вклад в «едет к нам» больше никто не снимет,
    а документ МойСклада прибавит свой — двойной счёт навсегда, без единого
    следа в логе. Половина правды здесь хуже честного отказа.

    Поэтому: обе записи в ОДНОЙ транзакции; сорвалось — повторяем T2 ЦЕЛИКОМ;
    сорвалось снова — WritebackUnknown. Ключ идемпотентности при этом остаётся
    в строке, повтор отправки безопасен, а ближайший синк свяжет документ с
    заказом по syncId сам (см. app/ms_sync._backmatch_by_sync_id).

    `pending_href` — точный токен, записанный НАШИМ T1. Он приходит снаружи, а
    не выводится здесь из текущего значения строки: смысл проверки в том, чтобы
    отличить свою пометку от чужой, а значение, прочитанное сейчас, чужим быть
    как раз и может.
    """
    href = ((doc.get("meta") or {}).get("href")) or ""
    name = str(doc.get("name") or "")
    for attempt in range(2):
        try:
            if _commit_push_once(db, org_id, order, href, name, pushed_by_base,
                                 pending_href):
                return href
            # Пометка «идёт отправка» уже не наша: пока мы ходили в сеть, лок
            # протух и его перехватила соседняя попытка. Она шла с ТЕМ ЖЕ
            # syncId, значит документ один и тот же.
            current = db.execute(
                select(ProductionOrder.ms_doc_href)
                .where(ProductionOrder.id == order.id,
                       ProductionOrder.org_id == org_id)
            ).scalar()
            if is_pushed(current):
                return str(current)
            raise WritebackUnknown(name, href)
        except WritebackUnknown:
            raise
        except Exception:  # noqa: BLE001 — второй шанс дороже точного типа сбоя
            db.rollback()
            if attempt == 0:
                continue
            raise WritebackUnknown(name, href)
    raise WritebackUnknown(name, href)


async def push_order(db: Session, org_id: int, order: ProductionOrder,
                     pending_href: str) -> dict:
    """Создаёт «Заказ поставщику» в МойСклад из позиций заказа.

    Возвращает {ok, ms_doc_name, ms_doc_href, ms_doc_ui_url,
    positions_pushed, unmatched:[...]}. T1 (ключи + лок) обязан быть выполнен
    вызывающим ДО входа сюда; T2 (ссылка + перенос вклада) выполняется здесь
    одной транзакцией — см. commit_push.

    `pending_href` — точный токен пометки, который вызывающий записал в T1.
    Он проносится через всё сетевое окно и служит доказательством владения
    локом в T2: за время окна лок мог протухнуть и достаться соседней попытке.
    """
    token = _get_ms_token(db, org_id)
    keys = load_push_keys(db, org_id, order.id)
    if not keys.sync_id:
        # Ни одной ветки «отправим без ключа» здесь нет и быть не должно:
        # динамический POST без syncId — это ровно тот дубль финансового
        # документа, ради которого весь механизм и написан. Пустой ключ
        # означает, что T1 не отработал, и это наша ошибка, а не ситуация,
        # из которой надо выкручиваться в момент отправки.
        raise WritebackError(
            500,
            "Внутренняя ошибка: ключ идемпотентности заказа не создан до "
            "отправки. Документ не отправлен — сообщите в поддержку.",
        )
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

        # 4) Контрагент «Производство»: стабильная привязка (см. resolve_agent).
        agent_meta = (await resolve_agent(db, org_id, client, keys)).get("meta") or {}

        # 5) Сам документ — но сначала проверяем, не создан ли он уже.
        #
        # Сеть даёт три исхода, а не два: «создан», «не создан» и «неизвестно»
        # (таймаут, 502, обрыв). В третьем случае документ у клиента может уже
        # существовать, и вторая попытка сделала бы ДУБЛЬ заказа поставщику —
        # с деньгами и с обещанием подрядчику.
        #
        # Защита — пользовательский идентификатор syncId (JSON API 1.2):
        # повторный POST с занятым ключом обновляет уже созданный документ,
        # а не заводит второй. Метка `[oborot#N]` в описании остаётся, но её
        # работа теперь другая: она читаема человеком, нужна диагностике и
        # правилу принадлежности D-28 в синке — а идемпотентность держит ключ.
        marker = order_marker(order.id)
        existing = await find_own_document(client, keys, marker)
        if existing is not None:
            doc, recovered = existing, True
        else:
            payload: dict = {
                "organization": {"meta": org_meta},
                "agent": {"meta": agent_meta},
                "positions": positions,
                "description": f"Создано в «Обороте»: заказ «{order.name}» {marker}",
                "syncId": keys.sync_id,
            }
            if order.eta_date:
                # Планируемая дата приёмки — из ETA заказа.
                payload["deliveryPlannedMoment"] = f"{order.eta_date} 00:00:00"
            recovered = False
            try:
                doc = await client.create_purchase_order(payload)
            except (httpx.HTTPError, httpx.HTTPStatusError):
                # Ответ не дошёл — «создан или нет» отсюда не видно.
                # Единственный честный способ узнать: спросить у МойСклада
                # по ключу, который мы записали ДО отправки.
                found = await find_own_document(client, keys, marker,
                                                after_create=True)
                if found is None:
                    raise
                # Документ всё-таки создан — потерялся только ответ.
                doc, recovered = found, True

    href = commit_push(db, org_id, order, doc, pushed_by_base, pending_href)
    return {
        "ok": True,
        "ms_doc_name": str(doc.get("name") or ""),
        "ms_doc_href": href,
        "ms_doc_ui_url": ui_url(doc),
        "positions_pushed": len(positions),
        "unmatched": unmatched,
        # True — документ уже существовал в МойСкладе и был подобран по ключу
        # (или, у legacy-строки, по маркеру), а не создан заново. Значит,
        # прошлая попытка на самом деле удалась, просто ответ до нас не дошёл.
        "recovered": recovered,
    }
