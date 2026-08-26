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
  GET  /entity/counterparty/{id}   — жива ли ЗАКРЕПЛЁННАЯ ссылка на агента;
                                     404/410 — забыть привязку и переразрешить;
  GET  /entity/counterparty        — постраничный перебор для поиска агента по
                                     нашему syncId (см. «Как ищется ключ»);
  GET  /entity/counterparty?filter=name=… — одноимённые агенты «Производство»:
                                     0 — создаём, 1 — закрепляем, >1 — 409;
  POST /entity/counterparty        — создание агента (идемпотентно, syncId);
  GET  /entity/purchaseorder       — постраничный перебор для поиска НАШЕГО
                                     документа по syncId (там же);
  GET  /entity/purchaseorder?filter=moment>=… — ТОЛЬКО legacy-путь: список без
                                     позиций за последние LOOKBACK_DAYS, метка
                                     `[oborot#N]` сверяется у нас (по подстроке
                                     описания МойСклад фильтровать не умеет);
  POST /entity/purchaseorder       — сам документ (organization, agent, syncId,
                                     positions[{assortment.meta, quantity,
                                     price-в-копейках}], deliveryPlannedMoment).

Как ищется ключ — и почему не фильтром. Раньше в этом перечне стояли
`GET /entity/counterparty?filter=syncId=…` и
`GET /entity/purchaseorder?filter=syncId=…`, и это было неправдой: живой
аккаунт отвечает на такой фильтр **HTTP 412, code 1034, «неизвестное поле
фильтрации syncId»** (Issue координации, issuecomment-5414290329), а
документация МойСклада обещает обратное. Между обещанием документа и
наблюдаемым ответом выбран ответ.

Поиск по `syncId` целиком живёт в `MoySkladClient.find_by_sync_id`, и он —
единственный источник правды о способе:
  • необязательная точечная подсказка `GET /entity/{type}/syncid/{id}`
    (поддержка `GET` по этому URL НЕ документирована, поэтому подсказка ничего
    не решает и ничем не заменяет перебор);
  • авторитетный ограниченный постраничный перебор коллекции с ТОЧНЫМ
    сравнением `syncId` у себя; исчерпанная граница — типизированный отказ, а
    не пустой ответ.

Этот перечень описывает фактические вызовы. Если он разойдётся с кодом — верить
коду: ложный обзор модуля опаснее отсутствующего, потому что уводит
сопровождающего обратно на отвергнутый путь.

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
from app.models import (
    Connection,
    OrderedQty,
    Product,
    ProductionOrder,
    encode_items_payload,
    parse_items_payload,
)
from app.ms_client import (
    MoySkladClient,
    SyncIdLookupUnavailable,
    SyncIdNotUnique,
)

# Имя контрагента-поставщика, на которого оформляется заказ.
AGENT_NAME = "Производство"

# Пометка «идёт отправка» в ms_doc_href (лок в routes_connect): pending:<epoch>.
PENDING_PREFIX = "pending:"

# Пометка «исход неизвестен»: unknown:<epoch>. Документ в МойСкладе, скорее
# всего, СОЗДАН, а записать это у себя не удалось (D-37, WritebackUnknown).
#
# Ревью Codex, P1 (discussion_r3856243666). Раньше на этом месте оставалась
# ПУСТАЯ строка: лок снимался, и заказ снова выглядел неотправленным. Дальше
# цепочка складывалась так: `api_order_delete` пускает удаление по
# `not_pushing_clause()`, а тот смотрит только на префикс `pending:` — значит
# заказ можно удалить, и вместе со строкой уходит `ms_sync_id`. А
# `ms_sync._backmatch_by_sync_id` ищет заказы именно по этому ключу в нашей
# базе: нет строки — нечем и некого связывать. Финансовый документ в чужом
# аккаунте оставался бы без владельца НАВСЕГДА, и это ровно та потеря, которую
# back-match и был обязан предотвращать.
#
# Поэтому состояние стало ЯВНЫМ и живёт в том же поле — новой колонки и
# миграции не нужно. Свойства пометки:
#   • `is_pushed` для неё ЛОЖНО — это не ссылка на документ. Отсюда и то, что
#     локальный вклад заказа продолжает считаться в qty (двойного счёта нет),
#     и то, что back-match такой заказ по-прежнему видит и лечит;
#   • удаление по ней запрещено (см. not_orphaning_clause) — до связывания;
#   • ПОВТОР отправки поверх неё разрешён и безопасен: он идёт с тем же
#     syncId, и find_own_document подберёт уже созданный документ, а не заведёт
#     второй. Запирать заказ навсегда здесь было бы лечением хуже болезни.
UNKNOWN_PREFIX = "unknown:"

# Оба служебных значения поля `ms_doc_href`. Всё, что не начинается ни с
# одного из них и непусто, — настоящая ссылка на документ.
INTERNAL_HREF_PREFIXES = (PENDING_PREFIX, UNKNOWN_PREFIX)


def is_internal_href(href: str | None) -> bool:
    """Служебная пометка (`pending:` / `unknown:`), а не ссылка на документ."""
    return str(href or "").startswith(INTERNAL_HREF_PREFIXES)


def is_unknown(href: str | None) -> bool:
    """Заказ в состоянии «документ создан, а записать не удалось»."""
    return str(href or "").startswith(UNKNOWN_PREFIX)

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
    """Заказ реально отправлен в МойСклад (есть ссылка на документ, не пометка).

    Такой заказ учитывается в «едет к нам» ТОЛЬКО через ordered_qty.ms_qty
    (импорт purchaseorder синком) — статусные переходы в api.py не должны
    двигать локальный qty, иначе двойной счёт.

    Пометка «неизвестно» ссылкой НЕ является, и это не формальность: пока
    документ не связан, его вклад считает наш локальный qty, а не ms_qty.
    Признать такой заказ отправленным значило бы потерять вклад целиком —
    товар исчез бы из «едет к нам» у обеих сторон сразу.
    """
    h = href or ""
    return bool(h) and not is_internal_href(h)


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


# Текст отказа удалить заказ, исход отправки которого неизвестен.
ORDER_UNKNOWN_OUTCOME = (
    "По этому заказу отправка в МойСклад закончилась неизвестным исходом: "
    "документ там, скорее всего, создан, а сохранить ссылку у нас не вышло. "
    "Удалить заказ сейчас нельзя — вместе с ним пропадёт ключ, по которому "
    "ближайшая синхронизация свяжет документ обратно. Повторите отправку: "
    "она пойдёт с тем же ключом и второго документа не создаст."
)


# Статусы, из которых заказ вообще можно отправлять в МойСклад.
#
# ОДНА константа на маршрут и на SQL-условие T1 намеренно. Два независимых
# списка разъехались бы при первой же правке — и разъехались бы молча: маршрут
# отказывал бы там, где CAS пропускает, или наоборот. Здесь цена расхождения —
# финансовый документ на принятом заказе.
PUSHABLE_STATUSES = ("draft", "sent")

# Текст отказа отправить уже принятый заказ. Один и тот же и для быстрой
# проверки в маршруте, и для проигранной гонки: с точки зрения человека это
# одно и то же событие, и звучать оно обязано одинаково.
ORDER_ALREADY_RECEIVED = (
    "Заказ уже принят на склад — отправлять его в МойСклад поздно."
)


def pushable_status_clause():
    """SQL-условие «заказ ещё можно отправлять» — для WHERE самого T1.

    Существует по той же причине, что и not_pushing_clause: проверка ПЕРЕД
    операцией отвечает на вопрос о прошлом. Между ней и захватом лока
    помещается чужой коммит `sent → received`, и тогда сеть создаёт
    финансовый документ на заказ, который уже приняли на склад.

    coalesce — на случай NULL: `NULL IN (...)` даёт NULL, то есть строка молча
    выпала бы из-под захвата, и отправка сломалась бы на ровном месте.
    """
    return func.coalesce(ProductionOrder.status, "").in_(PUSHABLE_STATUSES)


def not_orphaning_clause():
    """SQL-условие «удаление не осиротит финансовый документ» — только для DELETE.

    Ревью Codex, P1 (discussion_r3856243666). Удаление заказа в состоянии
    «неизвестно» уносит `ms_sync_id`, а вместе с ним — единственную возможность
    связать уже созданный документ обратно (`ms_sync._backmatch_by_sync_id`
    ищет заказы именно по этому ключу). Документ остаётся в чужом аккаунте
    навсегда без владельца.

    Условие отдельное, а не расширение `not_pushing_clause()`, и это осознанно.
    Тот же предикат стоит в СТАТУСНОМ переходе, и расширить его значило бы
    заодно запретить «принять на склад» заказ, документ которого в МойСкладе
    есть. Это уже продуктовое решение, а finding просит другого: запрещается
    ровно удаление и ровно до связывания.

    Как и `not_pushing_clause`, живёт внутри самой изменяющей операции: между
    чтением и DELETE помещается и T2 отправки, и back-match синка, поэтому
    предварительная проверка ловила бы состояние, которого уже нет (TOCTOU).
    """
    return func.coalesce(ProductionOrder.ms_doc_href, "").notlike(
        f"{UNKNOWN_PREFIX}%")


def mark_unknown(db: Session, order_id: int, pending_href: str,
                 unknown_href: str) -> bool:
    """Переводит СВОЮ пометку отправки в состояние «исход неизвестен».

    CAS по ТОЧНОМУ токену нашей попытки — тому самому, который записал наш T1.
    `LIKE pending:%` сюда не возвращается ни в каком виде: пометка живёт TTL,
    по его истечении её законно перехватывает соседняя попытка, и трогать
    чужое владение мы не вправе (ревью Codex, раунд 3). Не наша пометка —
    rowcount=0 и молчание.

    Возвращает True, если перевели именно мы.
    """
    if not pending_href or not unknown_href:
        return False
    db.rollback()
    changed = db.execute(
        sa_update(ProductionOrder)
        .where(
            ProductionOrder.id == order_id,
            ProductionOrder.ms_doc_href == pending_href,  # ТОЧНЫЙ токен T1
        )
        .values(ms_doc_href=unknown_href)
    ).rowcount > 0
    db.commit()
    return changed


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


class PushOutcomeUnknown(Exception):
    """Запрос на создание документа УШЁЛ, а исход установить не удалось.

    Родственник `WritebackUnknown`, но из другой точки и с другим знанием.
    Там документ точно создан и у нас на руках его номер — не записалась
    только наша сторона. Здесь мы не знаем даже этого: POST мог дойти и
    создать «Заказ поставщику», а мог не дойти вовсе.

    Почему у этого исхода отдельное имя, а не «сетевая ошибка» (ревью Codex,
    P1, discussion_r3858173475). Сорванный запрос ДО попытки создания —
    честный локальный отказ: документа нет, лок надо снять, заказ обычный.
    Сорванная попытка ПОСЛЕ POST — совсем другое событие с тем же текстом
    исключения. Раньше оба уходили в маршрут одним `httpx.HTTPError`, и общий
    обработчик снимал пометку: заказ выглядел неотправленным и становился
    УДАЛЯЕМЫМ. Удаление уносит `ms_sync_id` — единственный ключ, по которому
    `ms_sync._backmatch_by_sync_id` связал бы документ обратно, — и
    финансовый документ оставался в чужом аккаунте без владельца навсегда.

    Поэтому исход называется своим именем и ведёт туда же, куда ведёт
    `WritebackUnknown`: в устойчивое `unknown:` (см. `mark_unknown`), где
    ключ сохранён, удаление запрещено (`not_orphaning_clause`), а повтор с
    тем же `syncId` разрешён и второго документа не создаёт.

    `reason` — короткая причина для человека: почему исход остался неизвестным.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def post_refused_by_ms(exc: BaseException) -> bool:
    """Ответил ли сам МойСклад окончательным отказом на наш POST (4xx).

    Граница между «неизвестно» и «точно не создан», и она проведена по
    единственному достоверному признаку: сервер ОТВЕТИЛ про этот запрос.
    412 «поле не задано», 401/403 «нет доступа», 429 «мы даже не начали» —
    это не потерянный ответ, а отказ, и документа за ним нет.

    Почему не «всё после POST считаем неизвестным». Такой заказ уходил бы в
    `unknown:` навсегда: удалить нельзя, повтор даёт тот же 4xx и снова
    `unknown:`, связывать синку нечего. Владелец остаётся с заказом, который
    нельзя ни отправить, ни удалить, — лечение хуже болезни.

    Асимметрия рисков сохранена в нужную сторону: неизвестным считается ВСЁ,
    кроме прямого отказа сервера — 5xx, таймаут, обрыв соединения и любая
    неудача восстановительного поиска.
    """
    resp = getattr(exc, "response", None)
    code = getattr(resp, "status_code", None)
    return isinstance(code, int) and 400 <= code < 500


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


def _order_base_totals(items: list[dict]) -> dict[str, int]:
    """Локальный total qty по base_name из ТОЧНОГО снимка items заказа.

    Тот же способ счёта, что у `app.api._apply_remainder_to_incoming`: сумма
    по base_name из ВСЕХ строк заказа (сопоставленных и нет), а не то, что
    сопоставилось при матчинге. Общий для push_order (recovered-путь) и
    `ms_sync._backmatch_by_sync_id` — верхняя граница, которую перенос вклада
    «едет к нам» не имеет права превышать, даже если сам документ содержит
    больше (см. `positions_pushed_by_base`).
    """
    totals: dict[str, int] = {}
    for item in items:
        base, qty = item.get("base_name"), int(item.get("qty") or 0)
        if not base or qty <= 0:
            continue
        totals[base] = totals.get(base, 0) + qty
    return totals


def positions_pushed_by_base(positions: list[dict],
                             ext_id_to_base: dict[str, str],
                             order_totals: dict[str, int]) -> dict[str, float] | None:
    """Фактические позиции ДОКУМЕНТА МойСклад → {base_name: qty}, обрезано заказом.

    Источник истины для recovered-документа (см. push_order) и для
    back-match'а (см. ms_sync._backmatch_by_sync_id): что РЕАЛЬНО лежит в
    документе, а не что СЕЙЧАС сопоставляют локальные items с ассортиментом.
    Сопоставление между попытками могло измениться (переименование, замена
    SKU, удалённый вариант) — локальный матч в момент recovery доказывает
    только «что мы сопоставили бы, если бы создавали заново», а не то, что
    реально уехало в уже существующий документ.

    `ext_id_to_base` — обратная карта Product.ext_id → base_name, построенная
    вызывающим по ТЕКУЩЕМУ ассортименту организации (стабильный признак:
    ext_id живёт в самой позиции документа, а не в её порядке или тексте).

    `order_totals` (см. `_order_base_totals`) — ВЕРХНЯЯ ГРАНИЦА переноса на
    base: документ мог получить лишнее относительно ЭТОГО заказа (посторонняя
    строка того же товара, ручная правка в МС, дубль позиции) — списывать
    больше, чем сам заказ когда-либо заявлял по base, нельзя, иначе перенос
    вклада (`_move_incoming_to_ms`) съедает общий/чужой qty того же base_name
    (Codex corrective, issuecomment-5428103206). База документа, которой нет
    в `order_totals` вовсе — не наш вклад, и исключается целиком, а не
    обрезается до нуля молча где-то ниже по цепочке.

    Возвращает None, если хотя бы одна ПОЛОЖИТЕЛЬНАЯ позиция документа не
    сопоставилась ни с одним base текущего ассортимента (ext-id неизвестен),
    либо её количество дробное. Оба случая — fail-closed: подтвердить, что
    РЕАЛЬНО относится к этому заказу, нельзя, а угадывать (округлять,
    пропускать) запрещено — тем же контрактом, что и у `int(round(...))`,
    который здесь раньше стоял и тихо терял/добавлял единицы. Вызывающий
    обязан считать это неизвестным исходом, а не частичным успехом (см.
    push_order.recovered и ms_sync._backmatch_by_sync_id).
    """
    raw: dict[str, float] = {}
    for pos in positions:
        try:
            qty = float(pos.get("quantity") or 0)
        except (TypeError, ValueError):
            return None
        if qty <= 0:
            continue
        if qty != int(qty):
            # Локальная модель заказа — целые единицы (_order_base_totals);
            # дробную позицию документа обрезать/перенести без округления
            # нельзя — fail-closed, не int(round(...)).
            return None
        href = (((pos.get("assortment") or {}).get("meta")) or {}).get("href") or ""
        ext = _href_uuid(href)
        base = ext_id_to_base.get(ext)
        if not base:
            return None
        raw[base] = raw.get(base, 0.0) + qty
    out: dict[str, float] = {}
    for base, qty in raw.items():
        cap = order_totals.get(base, 0)
        if cap <= 0:
            continue
        out[base] = min(qty, float(cap))
    return out


def _move_incoming_to_ms(db: Session, org_id: int, order: ProductionOrder,
                         pushed_by_base: dict[str, float], was_sent: bool) -> None:
    """Перенос вклада заказа в «едет к нам» с локального qty на ms_qty.

    С момента отправки источник истины по этому заказу — документ в МойСклад
    (следующий синк посчитает его из purchaseorder, приёмки снимут принятое).
    Здесь: (а) если заказ уже был «В производстве» — снимаем из qty РОВНО ту
    часть его прежнего локального вклада, которая реально попала в документ
    (`pushed_by_base`), а не полное количество позиции. Позиция сопоставляется
    по base_name+size (см. push_order): если сопоставились не все размеры,
    несопоставленный остаток обязан остаться в qty — в МойСкладе на него
    документа нет, снимать его неоткуда; (б) отправленные позиции сразу
    прибавляем к ms_qty, чтобы «едет» не мигал до ближайшего синка.

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
        # Снимаем ровно matched-часть, а не полное item["qty"]: unmatched
        # размеры/позиции документа в МойСкладе не имеют, и их вклад обязан
        # остаться в qty (DATA-7) — иначе он пропадает из «едет к нам» молча.
        for base, qty in pushed_by_base.items():
            if qty > 0:
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

    Допустимый статус — тоже УСЛОВИЕ ЭТОГО ЖЕ UPDATE, а не проверка перед ним
    (ревью Codex, P1, discussion_r3857277070). Маршрут читает статус до
    сетевого окна, и между его чтением и этим T1 помещается чужой коммит
    `sent → received`. Предварительная проверка честно видела «ещё не принят»,
    CAS отрабатывал по одному лишь href — и отправка создавала «Заказ
    поставщику» для УЖЕ ПРИНЯТОГО заказа. Дальше T2 читал свежий `received`,
    локальный qty не снимал (верно), но документ МойСклада попадал в ms_qty
    ближайшим синком: принятый товар воскресал как «едет к нам». Интерфейс
    показывал в пути то, что уже лежит на складе.

    Это ровно тот же приём, что в `not_pushing_clause` и `not_orphaning_clause`:
    условие живёт внутри самой изменяющей операции, и третьего состояния между
    «проверили» и «записали» не существует.

    Возвращает True, если лок захвачен именно нами.
    """
    fresh_doc_key = str(uuid.uuid4())
    locked = db.execute(
        sa_update(ProductionOrder)
        .where(
            ProductionOrder.id == order_id,
            ProductionOrder.org_id == org_id,
            ProductionOrder.ms_doc_href == expected_href,  # CAS
            pushable_status_clause(),
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

# Сколько раз T2 переснимает items_json при гонке с переименованием, прежде
# чем сдаться и отдать исключение обычному пути повтора (commit_push сам
# перезапускает T2 целиком). Ренейм — редкое фоновое событие синка, не
# состязание в горячем цикле, поэтому пары попыток с запасом достаточно —
# граница нужна только чтобы не крутиться бесконечно при патологической
# гонке.
_T2_ITEMS_CAS_ATTEMPTS = 4


def _remap_pushed_by_base(old_items: list[dict], new_items: list[dict],
                          pushed_by_base: dict[str, float]) -> dict[str, float]:
    """Переносит ключи pushed_by_base через конкурентное переименование.

    Между чтением items (для сопоставления с ассортиментом МС) и записью T2
    конкурентный ms_sync._migrate_renames мог переписать items_json —
    ренейм меняет `it["base_name"]` НА МЕСТЕ, не трогая состав и порядок
    строк, поэтому старое и новое имя одной и той же позиции находятся по
    индексу. Если длина/состав разошлись сильнее простого rename (что не
    должно происходить конкурентно с этим же T2 — заказ ещё не помечен
    отправленным, ms_sync его строки не трогает), маркер безопаснее оставить
    как есть, чем гадать: несовпавшие ключи просто не найдут пары в новых
    items и останутся в remainder нетронутыми в ту же сторону, что и
    отсутствующий маркер (см. app.api._apply_remainder_to_incoming).
    """
    if len(old_items) != len(new_items):
        return pushed_by_base
    mapping: dict[str, str] = {}
    for old_it, new_it in zip(old_items, new_items):
        old_b, new_b = old_it.get("base_name"), new_it.get("base_name")
        if old_b and new_b and old_b != new_b:
            mapping[old_b] = new_b
    if not mapping:
        return pushed_by_base
    remapped: dict[str, int] = {}
    for base, qty in pushed_by_base.items():
        key = mapping.get(base, base)
        remapped[key] = remapped.get(key, 0) + qty
    return remapped


def _commit_push_once(db: Session, org_id: int, order: ProductionOrder,
                      href: str, name: str,
                      pushed_by_base: dict[str, float],
                      pending_href: str,
                      matched_items_json: str) -> bool | None:
    """Одна попытка T2. True — записано, None — лок уже не наш, иначе исключение."""
    db.rollback()
    # `matched_items_json` — снимок items_json РОВНО в момент матчинга (см.
    # push_order), а не текущее состояние строки. pushed_by_base посчитан
    # именно против этого снимка, и CAS обязан ловить ЛЮБОЙ rename между
    # матчингом и этой записью — включая тот, что успел закоммититься ЗАДОЛГО
    # до входа сюда, за время сетевого окна (rename — фоновая операция синка,
    # а не гонка в доли миллисекунды: типичный случай — именно этот). Более
    # ранняя версия читала `order.items_json` заново прямо здесь, СРАЗУ после
    # `db.rollback()`, которое экспирит ORM-объект, — а если rename уже
    # закоммитился к этому моменту (обычное дело после многосекундного
    # сетевого ожидания), такое чтение как раз и ВИДЕЛО его: локальный CAS
    # сверял «текущее» само с собой, находил их равными и писал
    # pushed_by_base под СТАРЫМИ ключами поверх УЖЕ переименованных items —
    # маркер и items расходились молча (Codex corrective,
    # issuecomment-5427535755; воспроизведено гонкой в тесте «29»
    # test_writeback_idempotency.py). Ремап должен сравнивать с тем, ПРОТИВ
    # ЧЕГО реально считали pushed_by_base, а не с тем, что случайно лежит в
    # строке в момент вызова.
    items_json = matched_items_json
    items, _matched_marker = parse_items_payload(items_json)
    for _ in range(_T2_ITEMS_CAS_ATTEMPTS):
        # RETURNING отдаёт статус ровно той строки, которую мы сейчас изменили,
        # и ровно в момент изменения: решение «снимать ли локальный вклад
        # заказа» обязано опираться на состояние внутри этой транзакции, а не
        # на ORM-объект, прочитанный до сетевого окна.
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
                # Второй CAS — на items_json: пишем маркер только поверх ТОГО
                # снимка позиций, по которому pushed_by_base посчитан. Без
                # него T2 клобберил бы конкурентную запись переименования
                # (ms_sync._migrate_renames переписывает items_json отдельной
                # транзакцией) своим старым снимком — маркер остался бы под
                # именами, которых для этого заказа уже нет, и remainder на
                # sent→received/удалении считался бы по позициям, которых
                # больше не существует (Codex corrective, DATA-7 PR #25,
                # issuecomment-5427535755).
                ProductionOrder.items_json == items_json,
            )
            .values(
                ms_doc_href=href, ms_doc_name=name, ms_lookup_mode=LOOKUP_SYNC,
                # Маркер DATA-7 (см. models.encode_items_payload) уходит в той же
                # строке того же UPDATE, что и href: sent→received и удаление
                # обязаны увидеть его РОВНО тогда же, когда заказ становится
                # is_pushed, иначе есть окно, где заказ уже "отправлен", а какая
                # часть — неизвестно (см. app.api._apply_remainder_to_incoming).
                items_json=encode_items_payload(items, pushed_by_base),
            )
            .returning(ProductionOrder.status)
            .execution_options(synchronize_session=False)
        ).fetchall()
        if saved:
            _move_incoming_to_ms(db, org_id, order, pushed_by_base,
                                 was_sent=str(saved[0][0] or "") == "sent")
            db.commit()
            return True
        db.rollback()
        current = db.execute(
            select(ProductionOrder.ms_doc_href, ProductionOrder.items_json)
            .where(ProductionOrder.id == order.id, ProductionOrder.org_id == org_id)
        ).one_or_none()
        if current is None or str(current[0] or "") != pending_href:
            # Лок уже не наш: пометка протухла, или заказа больше нет.
            return None
        current_items_json = current[1]
        if current_items_json == items_json:
            # href совпал, items_json не изменился — но UPDATE всё равно
            # ничего не задел. При SQLite/Postgres READ COMMITTED такое не
            # должно происходить без внешнего изменения строки; повторяем тем
            # же снимком, не тратя это на remap.
            continue
        # items_json разошёлся при живом pending_href — переименование
        # опередило нас. Переносим маркер на новые имена той же строки и
        # повторяем CAS уже с актуальным снимком, вместо того чтобы затереть
        # rename своим старым.
        fresh_items, _marker = parse_items_payload(current_items_json)
        pushed_by_base = _remap_pushed_by_base(items, fresh_items, pushed_by_base)
        items = fresh_items
        items_json = current_items_json
    # Границы гонки исчерпаны — не гадаем и не клобберим: отдаём исключение
    # обычному пути повтора (commit_push перезапускает T2 целиком; исчерпал —
    # WritebackUnknown, честный отказ вместо тихой порчи).
    raise RuntimeError(
        "T2: items_json меняется быстрее, чем CAS успевает записать маркер DATA-7")


def commit_push(db: Session, org_id: int, order: ProductionOrder, doc: dict,
                pushed_by_base: dict[str, float], pending_href: str,
                matched_items_json: str) -> str:
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

    `matched_items_json` — снимок items_json РОВНО в момент, когда push_order
    матчил позиции и считал pushed_by_base (см. _commit_push_once). Пробрасывается
    насквозь, а не перечитывается здесь: значение нужно ИМЕННО с той стороны
    сети, до которой могло случиться переименование.
    """
    href = ((doc.get("meta") or {}).get("href")) or ""
    name = str(doc.get("name") or "")
    for attempt in range(2):
        try:
            if _commit_push_once(db, org_id, order, href, name, pushed_by_base,
                                 pending_href, matched_items_json):
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
    # Обратная карта для recovered-документов (см. positions_pushed_by_base):
    # ext_id → base_name, по ТЕКУЩЕМУ ассортименту организации.
    ext_id_to_base: dict[str, str] = {
        p.ext_id: p.base_name for p in products.values() if p.ext_id
    }

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
        #
        # Снимок items_json ИМЕННО отсюда (а не перечитанный позже, в T2,
        # после сетевого окна) — база для CAS в commit_push/_commit_push_once:
        # pushed_by_base ниже считается против ЭТОГО состояния строки, и
        # ремап конкурентного переименования обязан сравнивать с ним, а не с
        # тем, что окажется в строке к моменту записи T2.
        matched_items_json = order.items_json
        # Верхняя граница переноса на recovered-пути (см. positions_pushed_by_base):
        # ТОТ ЖЕ снимок items, что и matched_items_json выше.
        order_totals = _order_base_totals(order.items)
        positions: list[dict] = []
        unmatched: list[str] = []
        pushed_by_base: dict[str, float] = {}  # для переноса вклада в ms_qty
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
            # Документ уже существует — прошлая попытка (или legacy-документ)
            # могла сопоставить позиции ДРУГИМ ассортиментом, чем сопоставляет
            # СЕЙЧАС локальный матч выше (шаг 2): вариант могли пересоздать,
            # переименовать в МС, у нас — заменить ext_id. Источник истины про
            # то, что РЕАЛЬНО уехало, — сам документ, а не свежий локальный
            # матч, который вообще не создавал ни одной сетевой сущности этой
            # попытки. pushed_by_base отсюда идёт напрямую в commit_push и в
            # маркер DATA-7 — подменяем его на фактические позиции документа.
            doc_id = str(doc.get("id")
                        or _href_uuid(((doc.get("meta") or {}).get("href")) or ""))
            real_positions = await client.fetch_positions("purchaseorder", doc_id) \
                if doc_id else []
            resolved = positions_pushed_by_base(real_positions, ext_id_to_base,
                                                order_totals)
            if resolved is None:
                # Fail-closed (см. docstring positions_pushed_by_base): хотя бы
                # одна положительная позиция документа не сопоставилась ни с
                # одним base текущего ассортимента, либо несёт дробное
                # количество. Подтвердить, какая часть реально относится к
                # ЭТОМУ заказу, нельзя — гадать (списывать по неполной карте)
                # запрещено так же, как и раньше округлять. T2 не вызываем:
                # заказ остаётся в устойчивом «неизвестно», ключ и документ
                # целы, следующая отправка (или backmatch синка) повторит
                # попытку сама.
                doc_href = ((doc.get("meta") or {}).get("href")) or ""
                doc_name = str(doc.get("name") or "")
                raise WritebackUnknown(doc_name, doc_href)
            pushed_by_base = resolved
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
            except (httpx.HTTPError, httpx.HTTPStatusError) as exc:
                # ── Граница «до попытки» / «после попытки» ────────────────
                #
                # Ревью Codex, P1 (discussion_r3858173475). Ниже начинается
                # участок, где запрос на создание финансового документа УЖЕ
                # ушёл. Всё, что здесь не кончилось подтверждённым РОВНО
                # ОДНИМ документом, — исход НЕИЗВЕСТНЫЙ, а не «безопасный
                # сетевой сбой». Разница не в словах: неизвестный исход
                # обязан сохранить пометку и ключ, а «сбой» снимал пометку,
                # после чего заказ становился удаляемым — вместе с
                # `ms_sync_id`, то есть с единственным ключом back-match'а.
                #
                # Исключение ровно одно и оно проверяемое: МойСклад ОТВЕТИЛ
                # отказом на этот запрос (4xx) — см. post_refused_by_ms.
                if post_refused_by_ms(exc):
                    raise
                # Ответ не дошёл — «создан или нет» отсюда не видно.
                # Единственный честный способ узнать: спросить у МойСклада
                # по ключу, который мы записали ДО отправки.
                try:
                    found = await find_own_document(client, keys, marker,
                                                    after_create=True)
                except AmbiguousExistingOrder:
                    # Совпадений несколько. Исключение само несёт
                    # after_create=True, и маршрут обязан обойтись с ним как с
                    # неизвестным исходом — но текст у него свой, поэтому
                    # подменять его здесь нельзя.
                    raise
                except Exception as probe_exc:
                    # Восстановление не состоялось: перебор упал транспортом
                    # или статусом, либо исчерпал границу (SyncIdLookupUnavailable
                    # приходит сюда уже как WritebackError). Ни одна из этих
                    # неудач НЕ является ответом «документа нет».
                    raise PushOutcomeUnknown(
                        f"проверить исход не удалось: {probe_exc}"
                    ) from probe_exc
                if found is None:
                    # Перебор отработал и не нашёл — но это тоже не «нет».
                    # Свежесозданный документ может быть ещё не виден в
                    # выдаче списка, и принять задержку видимости за
                    # отсутствие значит потерять документ насовсем.
                    raise PushOutcomeUnknown(
                        f"ответ на создание документа не получен ({exc}), "
                        "а поиск по ключу его пока не видит"
                    ) from exc
                # Документ всё-таки создан — потерялся только ответ.
                doc, recovered = found, True

    href = commit_push(db, org_id, order, doc, pushed_by_base, pending_href,
                       matched_items_json)
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
