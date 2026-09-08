# -*- coding: utf-8 -*-
"""SUPPLY-3: API планирования — материал, вещь, плановая партия, назначения.

ГРАНИЦЫ, КОТОРЫЕ ЭТОТ ФАЙЛ ДЕРЖИТ.

Арендатор берётся ТОЛЬКО из сессии (`ctx.org.id`). Ни `org_id`, ни чужих
идентификаторов владельца контракт не принимает вовсе — не «игнорирует», а не
имеет: параметр, который можно прислать, рано или поздно кто-нибудь прочитает.

Чтение — владелец и участник. Запись — только владелец (`require_owner_api`),
поверх этого работает общий гейт подписки (`subscription.gate_dependency`,
повешен на приложение) и общий CSRF на изменяющие `/api/*`
(`main._security_headers_and_csrf`). Исключений из readonly здесь нет ни одного:
ни одна ручка не объявлена «безопасной» и не добавлена в `ALWAYS_OPEN_PATHS`.

Чужой идентификатор даёт 404 с одним и тем же текстом независимо от того,
существует строка у другой организации или не существует вовсе: разные ответы
рассказали бы о существовании чужих данных перебором номеров.

Транзакция на запрос — одна. Ручка либо коммитит целиком, либо откатывает
целиком: перенос метража между партиями обязан быть неделим, иначе он теряет
или удваивает метры (см. `supply_planning.move_assignment`).

ЗАМОК СХЕМЫ ЖИВЁТ ДОЛЬШЕ КОДА, и поэтому ни одна пишущая ручка здесь не
отвечает 500 на нарушение уникальности. Причина не гипотетическая: индекс,
поставленный новой версией, переживает штатный откат на предыдущую — база
остаётся мигрированной, а исполняется прежний код, для которого этого
ограничения не существует. Он делает `INSERT`, получает `IntegrityError` и,
если её никто не ловит, отдаёт пустой отказ сервера на ОСНОВНОМ пути записи.
Тогда цену отката платит пользователь, а дежурный видит 500 без причины.

Отсюда правило файла: `IntegrityError` разбирается наравне с доменной ошибкой
(`_fail`), транзакция откатывается целиком, наружу уходит управляемый 409 без
единого слова про устройство хранилища, а настоящая причина остаётся в журнале
сервера. На схеме, где такого замка ещё нет, ветка не исполняется вовсе и
поведение не меняется ни на байт.
"""
from __future__ import annotations

import logging

from fastapi import (APIRouter, Body, Depends, File, HTTPException, Query,
                     Request, UploadFile)
from fastapi.responses import Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import supply_planning as sp
from app.auth import AuthContext, require_auth_api, require_owner_api
from app.db import get_db

router = APIRouter(prefix="/api/supply/planning", tags=["supply-planning"])

log = logging.getLogger("oborot.supply_planning")

#: Ответ на нарушение уникальности. Текст ОДИН на все места, где этот отказ
#: выдаётся, и живёт константой именно поэтому: раньше он стоял литералом
#: внутри `_commit()`, и второе такое же место написало бы свой вариант.
#: Новых слов пакет не сочиняет — это уже существующая формулировка проекта
#: для этого же класса отказа.
_CONFLICT_DETAIL = "Это действие уже выполнено. Обновите страницу."

#: Потолок тела запроса на эскиз читается по факту: `UploadFile` даёт поток, и
#: доверять заголовку `Content-Length` нельзя — он приходит от клиента.
_SKETCH_READ_CHUNK = 64 * 1024


def _author(ctx: AuthContext) -> str:
    """Кто сделал правку. Имя пользователя, а не идентификатор.

    В журнале должно остаться то, что человек узнает через полгода. Почта —
    личные данные, поэтому в журнал идёт имя, а если его нет — роль.
    """
    user = getattr(ctx, "user", None)
    name = (getattr(user, "name", "") or "").strip()
    return name or (getattr(ctx, "role", "") or "")


def _fail(exc: Exception) -> HTTPException:
    """Один разбор доменных ошибок на все ручки — чтобы коды не разъезжались.

    Здесь же разбирается `IntegrityError` — нарушение замка САМОЙ схемы, а не
    доменного правила. Зачем это нужно ручкам, которые сегодня такого отказа не
    видят: замок в базе живёт дольше кода. Индекс, поставленный новой версией,
    переживает штатный откат на предыдущую, и тогда прежний код встречает
    ограничение, о котором ничего не знает. Без этой ветки он отвечает 500 на
    основном пути записи — то есть цена отката ложится на пользователя.

    Наружу уходит ТОЛЬКО обобщённый текст: сообщение драйвера называет таблицу
    и колонки, а это устройство хранилища, а не дело клиента. Подробность при
    этом не теряется — она идёт в журнал сервера строкой ниже. Разделение
    осознанное: широкий `except` мог бы превратить чужой дефект (NOT NULL, FK) в
    тихое «уже выполнено», и единственное, что этому мешает, — то, что настоящая
    причина ВСЕГДА остаётся видимой дежурному.

    Логируется `exc.orig` — сообщение самой БД («UNIQUE constraint failed: …»),
    а не `str(exc)`: полный текст SQLAlchemy тащит за собой SQL с параметрами,
    то есть заметки и названия, введённые человеком. В журнал они не нужны.
    """
    if isinstance(exc, IntegrityError):
        log.warning("планирование: запись отвергнута замком схемы: %s",
                    getattr(exc, "orig", None) or exc.__class__.__name__)
        return HTTPException(status_code=409, detail=_CONFLICT_DETAIL)
    if isinstance(exc, sp.ValidationError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, sp.NotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, sp.StaleWrite):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, sp.ArchivedCatalogItem):
        # 409, а не 400: ввод человека верен, модель существует — она убрана.
        # Отдельный от `InUse` тип, потому что это вопрос, а не тупик: ответ на
        # него — подтверждение `confirm_restore`, и текст его прямо предлагает.
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, sp.InUse):
        # 409, а не 400: ввод человека верен, отказ вызван состоянием соседних
        # строк. Текст приходит из слоя уже с числом и с тем, что надо сделать.
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=400, detail="Не удалось выполнить действие.")


def _commit(db: Session, org_id: int, role: str) -> dict:
    """Коммит и свежая доска одним ответом.

    Экран всегда получает ПОЛНОЕ состояние после записи, а не «ок». Так у
    страницы нет собственной версии правды, которую надо было бы догонять
    отдельным GET, и повторный клик не рисует разное в двух вкладках.
    """
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise _fail(exc) from None
    return sp.board(db, org_id, role)


@router.get("")
def api_planning_board(
    ctx: AuthContext = Depends(require_auth_api),
    db: Session = Depends(get_db),
):
    """Всё состояние планирования своей организации. Только чтение."""
    return sp.board(db, ctx.org.id, ctx.role)


@router.get("/catalog")
def api_planning_catalog(
    q: str = Query("", max_length=sp.MAX_TITLE_CHARS),
    ctx: AuthContext = Depends(require_auth_api),
    db: Session = Depends(get_db),
):
    """Кандидаты каталога по каноническому имени. Подсказка, а не привязка.

    В ответе рядом со списком идут `total` (сколько моделей совпало с запросом
    целиком) и `catalog_size` (сколько их в каталоге вообще). Первое нужно,
    чтобы честно сказать «показаны 20 из 143 — уточните», второе — чтобы не
    выдать пустой каталог за отсутствие совпадений.
    """
    return sp.catalog_options(db, ctx.org.id, q)


@router.get("/events")
def api_planning_events(
    entity: str = Query(..., max_length=16),
    id: int = Query(...),
    ctx: AuthContext = Depends(require_auth_api),
    db: Session = Depends(get_db),
):
    """История одной строки: последние 50 правок (ТЗ F-24).

    ЧИТАЮТ ВЛАДЕЛЕЦ И УЧАСТНИК — та же зависимость, что у доски
    (`require_auth_api`). Ничего нового о чужой организации ручка не
    рассказывает: строка сначала ищется обычным способом в СВОЕЙ организации, и
    чужой идентификатор даёт тот же 404 и тот же текст, что и несуществующий.
    Записи здесь нет вовсе, поэтому и `require_owner_api` тут был бы не защитой,
    а запретом участнику видеть то, что он и так видит на карточке.

    Имя параметра `id` совпадает со встроенным именем Python и выбрано не по
    небрежности: так его называет ТЗ (`?entity=…&id=`), и переименовать
    параметр запроса ради красоты кода значило бы разойтись с контрактом,
    который уже описан.
    """
    try:
        return sp.read_events(db, ctx.org.id, entity, id)
    except sp.PlanningError as exc:
        raise _fail(exc) from None


@router.post("/materials")
def api_planning_material_create(
    payload: dict = Body(...),
    ctx: AuthContext = Depends(require_owner_api),
    db: Session = Depends(get_db),
):
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Ожидался объект JSON.")
    try:
        if sp.check_op(db, ctx.org.id, sp.parse_op_id(payload)):
            return sp.board(db, ctx.org.id, ctx.role)
        sp.create_material(db, ctx.org.id, payload, _author(ctx))
    except (sp.PlanningError, IntegrityError) as exc:
        db.rollback()
        raise _fail(exc) from None
    return _commit(db, ctx.org.id, ctx.role)


@router.post("/materials/{material_id}/update")
def api_planning_material_update(
    material_id: int,
    payload: dict = Body(...),
    ctx: AuthContext = Depends(require_owner_api),
    db: Session = Depends(get_db),
):
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Ожидался объект JSON.")
    try:
        if sp.check_op(db, ctx.org.id, sp.parse_op_id(payload)):
            return sp.board(db, ctx.org.id, ctx.role)
        sp.update_material(db, ctx.org.id, material_id, payload, _author(ctx))
    except (sp.PlanningError, IntegrityError) as exc:
        db.rollback()
        raise _fail(exc) from None
    return _commit(db, ctx.org.id, ctx.role)


@router.post("/materials/{material_id}/archive")
def api_planning_material_archive(
    material_id: int,
    payload: dict = Body(default={}),
    ctx: AuthContext = Depends(require_owner_api),
    db: Session = Depends(get_db),
):
    """Убрать материал с доски (F-12). Строка остаётся, отметка проставляется.

    Права те же, что у любой записи слоя: только владелец, поверх — общий гейт
    подписки и общий CSRF. Исключений «удаление же не запись» здесь нет:
    убрать строку — это изменить данные организации.
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Ожидался объект JSON.")
    try:
        if sp.check_op(db, ctx.org.id, sp.parse_op_id(payload)):
            return sp.board(db, ctx.org.id, ctx.role)
        sp.archive_material(db, ctx.org.id, material_id, payload, _author(ctx))
    except (sp.PlanningError, IntegrityError) as exc:
        db.rollback()
        raise _fail(exc) from None
    return _commit(db, ctx.org.id, ctx.role)


@router.post("/materials/{material_id}/restore")
def api_planning_material_restore(
    material_id: int,
    payload: dict = Body(default={}),
    ctx: AuthContext = Depends(require_owner_api),
    db: Session = Depends(get_db),
):
    """Вернуть материал на доску. Возвращается ровно то, что убрали."""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Ожидался объект JSON.")
    try:
        if sp.check_op(db, ctx.org.id, sp.parse_op_id(payload)):
            return sp.board(db, ctx.org.id, ctx.role)
        sp.restore_material(db, ctx.org.id, material_id, payload, _author(ctx))
    except (sp.PlanningError, IntegrityError) as exc:
        db.rollback()
        raise _fail(exc) from None
    return _commit(db, ctx.org.id, ctx.role)


@router.post("/items/{item_id}/update")
def api_planning_item_update(
    item_id: int,
    payload: dict = Body(...),
    ctx: AuthContext = Depends(require_owner_api),
    db: Session = Depends(get_db),
):
    """Правка вещи: имя новинки, заметка, замена эскиза (F-13в).

    `base_name` и `kind` не меняются — слой отвечает 400 и называет, что убрать,
    вместо того чтобы принять поле и молча его выбросить (D-55, п. 1).
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Ожидался объект JSON.")
    try:
        if sp.check_op(db, ctx.org.id, sp.parse_op_id(payload)):
            return sp.board(db, ctx.org.id, ctx.role)
        sp.update_item(db, ctx.org.id, item_id, payload, _author(ctx))
    except (sp.PlanningError, IntegrityError) as exc:
        db.rollback()
        raise _fail(exc) from None
    return _commit(db, ctx.org.id, ctx.role)


@router.post("/items/{item_id}/archive")
def api_planning_item_archive(
    item_id: int,
    payload: dict = Body(default={}),
    ctx: AuthContext = Depends(require_owner_api),
    db: Session = Depends(get_db),
):
    """Убрать вещь с доски (F-12). В этом пакете — только новинку.

    Вещь каталога отвечает 409: её взаимодействие с выпущенным замком
    `ux_supply_items_catalog` — продуктовая развилка, удержанная до решения
    владельца (`TECH_DEBT.md`, `SUPPLY-FIX-2-REG`).
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Ожидался объект JSON.")
    try:
        if sp.check_op(db, ctx.org.id, sp.parse_op_id(payload)):
            return sp.board(db, ctx.org.id, ctx.role)
        sp.archive_item(db, ctx.org.id, item_id, payload, _author(ctx))
    except (sp.PlanningError, IntegrityError) as exc:
        db.rollback()
        raise _fail(exc) from None
    return _commit(db, ctx.org.id, ctx.role)


@router.post("/items/{item_id}/restore")
def api_planning_item_restore(
    item_id: int,
    payload: dict = Body(default={}),
    ctx: AuthContext = Depends(require_owner_api),
    db: Session = Depends(get_db),
):
    """Вернуть вещь на доску — ровно ту же, ничего не воссоздавая."""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Ожидался объект JSON.")
    try:
        if sp.check_op(db, ctx.org.id, sp.parse_op_id(payload)):
            return sp.board(db, ctx.org.id, ctx.role)
        sp.restore_item(db, ctx.org.id, item_id, payload, _author(ctx))
    except (sp.PlanningError, IntegrityError) as exc:
        db.rollback()
        raise _fail(exc) from None
    return _commit(db, ctx.org.id, ctx.role)


@router.post("/items")
def api_planning_item_create(
    payload: dict = Body(...),
    ctx: AuthContext = Depends(require_owner_api),
    db: Session = Depends(get_db),
):
    """Вещь каталога или полноценная новинка с эскизом.

    Повторный выбор ТОЙ ЖЕ модели каталога возвращает существующую вещь, а не
    заводит вторую (F-10), и это не молчаливое «ничего не произошло»: в ответ
    кладётся `notice`, из которого экран делает тост «Эта модель уже есть в
    плане». Без него человек, нажавший «Сохранить вещь» и не увидевший новой
    строки, нажимал бы ещё раз.
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Ожидался объект JSON.")
    try:
        op_id = sp.parse_op_id(payload)
        if sp.check_op(db, ctx.org.id, op_id):
            # ПОВТОР ПОСЛЕ ПОТЕРЯННОГО ОТВЕТА ОТВЕЧАЕТ ТЕМ ЖЕ, ЧЕМ И ПЕРВЫЙ РАЗ.
            # Номер созданной вещи здесь берётся из журнала: без него страница,
            # держащая в руках выбранный файл, не смогла бы прикрепить эскиз к
            # уже созданной новинке — то есть идемпотентность первого запроса
            # ломала бы второй (ТЗ F-23а).
            replay = sp.board(db, ctx.org.id, ctx.role)
            known = sp.find_op_entity(db, ctx.org.id, op_id, "item")
            if known is not None:
                replay["item_id"] = known
            return replay
        item = sp.create_item(db, ctx.org.id, payload, _author(ctx))
        reused = bool(getattr(item, "reused", False))
        restored = bool(getattr(item, "restored", False))
        title = item.title
        item_id = item.id
    except (sp.PlanningError, IntegrityError) as exc:
        db.rollback()
        raise _fail(exc) from None
    board = _commit(db, ctx.org.id, ctx.role)
    # НОМЕР СОХРАНЁННОЙ ВЕЩИ В ОТВЕТЕ. Ответ этой ручки — вся доска, и до сих
    # пор из неё нельзя было понять, какая строка только что появилась: экран
    # вычислял это разностью списков. Для эскиза такой догадки мало — файл
    # прикрепляется по точному номеру, и ошибиться в нём значит прикрепить
    # картинку не к той вещи.
    board["item_id"] = item_id
    if restored:
        # Возврат из архива и «эта модель уже есть» — разные события, и один
        # текст на оба сказал бы человеку неправду о том, что он сделал.
        board["notice"] = f"Модель «{title}» вернулась в план."
    elif reused:
        board["notice"] = "Эта модель уже есть в плане."
    return board


@router.post("/batches")
def api_planning_batch_create(
    payload: dict = Body(...),
    ctx: AuthContext = Depends(require_owner_api),
    db: Session = Depends(get_db),
):
    """Плановая партия. Партией «Оборота» она не становится и номера не получает."""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Ожидался объект JSON.")
    try:
        if sp.check_op(db, ctx.org.id, sp.parse_op_id(payload)):
            return sp.board(db, ctx.org.id, ctx.role)
        sp.create_batch(db, ctx.org.id, payload, _author(ctx))
    except (sp.PlanningError, IntegrityError) as exc:
        db.rollback()
        raise _fail(exc) from None
    return _commit(db, ctx.org.id, ctx.role)


@router.post("/batches/{batch_id}/update")
def api_planning_batch_update(
    batch_id: int,
    payload: dict = Body(...),
    ctx: AuthContext = Depends(require_owner_api),
    db: Session = Depends(get_db),
):
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Ожидался объект JSON.")
    try:
        if sp.check_op(db, ctx.org.id, sp.parse_op_id(payload)):
            return sp.board(db, ctx.org.id, ctx.role)
        sp.update_batch(db, ctx.org.id, batch_id, payload, _author(ctx))
    except (sp.PlanningError, IntegrityError) as exc:
        db.rollback()
        raise _fail(exc) from None
    return _commit(db, ctx.org.id, ctx.role)


@router.post("/batches/{batch_id}/archive")
def api_planning_batch_archive(
    batch_id: int,
    payload: dict = Body(default={}),
    ctx: AuthContext = Depends(require_owner_api),
    db: Session = Depends(get_db),
):
    """Убрать партию вместе с её назначениями (F-12, решение владельца).

    В ответ кладётся, СКОЛЬКО назначений снято и сколько метража вернулось в
    свободный остаток: без этих чисел интерфейс не может сказать человеку
    правду о том, что сейчас произошло, — а «Удалено» без последствий было бы
    половиной правды.
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Ожидался объект JSON.")
    try:
        if sp.check_op(db, ctx.org.id, sp.parse_op_id(payload)):
            return sp.board(db, ctx.org.id, ctx.role)
        done = sp.archive_batch(db, ctx.org.id, batch_id, payload, _author(ctx))
    except (sp.PlanningError, IntegrityError) as exc:
        db.rollback()
        raise _fail(exc) from None
    board = _commit(db, ctx.org.id, ctx.role)
    board["archived"] = done
    return board


@router.post("/batches/{batch_id}/restore")
def api_planning_batch_restore(
    batch_id: int,
    payload: dict = Body(default={}),
    ctx: AuthContext = Depends(require_owner_api),
    db: Session = Depends(get_db),
):
    """Вернуть партию ВМЕСТЕ с прежними назначениями и заметками.

    Число возвращённого метража уходит наружу по той же причине, что и при
    архивации: метраж возвращается В РАСПРЕДЕЛЕНИЕ, у материала снова растёт
    «назначено», и человек обязан увидеть это числом, а не обнаружить потом.
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Ожидался объект JSON.")
    try:
        if sp.check_op(db, ctx.org.id, sp.parse_op_id(payload)):
            return sp.board(db, ctx.org.id, ctx.role)
        done = sp.restore_batch(db, ctx.org.id, batch_id, payload, _author(ctx))
    except (sp.PlanningError, IntegrityError) as exc:
        db.rollback()
        raise _fail(exc) from None
    board = _commit(db, ctx.org.id, ctx.role)
    board["restored"] = done
    return board


@router.post("/assignments")
def api_planning_assignment_create(
    payload: dict = Body(...),
    ctx: AuthContext = Depends(require_owner_api),
    db: Session = Depends(get_db),
):
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Ожидался объект JSON.")
    try:
        if sp.check_op(db, ctx.org.id, sp.parse_op_id(payload)):
            return sp.board(db, ctx.org.id, ctx.role)
        sp.create_assignment(db, ctx.org.id, payload, _author(ctx))
    except (sp.PlanningError, IntegrityError) as exc:
        db.rollback()
        raise _fail(exc) from None
    return _commit(db, ctx.org.id, ctx.role)


@router.post("/assignments/move")
def api_planning_assignment_move(
    payload: dict = Body(...),
    ctx: AuthContext = Depends(require_owner_api),
    db: Session = Depends(get_db),
):
    """Перенос метража между плановыми партиями — одной транзакцией."""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Ожидался объект JSON.")
    try:
        if sp.check_op(db, ctx.org.id, sp.parse_op_id(payload)):
            return sp.board(db, ctx.org.id, ctx.role)
        sp.move_assignment(db, ctx.org.id, payload, _author(ctx))
    except (sp.PlanningError, IntegrityError) as exc:
        db.rollback()
        raise _fail(exc) from None
    return _commit(db, ctx.org.id, ctx.role)


@router.post("/assignments/{assignment_id}/update")
def api_planning_assignment_update(
    assignment_id: int,
    payload: dict = Body(...),
    ctx: AuthContext = Depends(require_owner_api),
    db: Session = Depends(get_db),
):
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Ожидался объект JSON.")
    try:
        if sp.check_op(db, ctx.org.id, sp.parse_op_id(payload)):
            return sp.board(db, ctx.org.id, ctx.role)
        sp.update_assignment(db, ctx.org.id, assignment_id, payload, _author(ctx))
    except (sp.PlanningError, IntegrityError) as exc:
        db.rollback()
        raise _fail(exc) from None
    return _commit(db, ctx.org.id, ctx.role)


@router.post("/assignments/{assignment_id}/delete")
def api_planning_assignment_delete(
    assignment_id: int,
    payload: dict = Body(default={}),
    ctx: AuthContext = Depends(require_owner_api),
    db: Session = Depends(get_db),
):
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Ожидался объект JSON.")
    try:
        if sp.check_op(db, ctx.org.id, sp.parse_op_id(payload)):
            return sp.board(db, ctx.org.id, ctx.role)
        sp.delete_assignment(db, ctx.org.id, assignment_id, payload, _author(ctx))
    except (sp.PlanningError, IntegrityError) as exc:
        db.rollback()
        raise _fail(exc) from None
    return _commit(db, ctx.org.id, ctx.role)


async def _read_upload(file: UploadFile, limit: int, too_big: str) -> bytes:
    """Читает файл с потолком и говорит человеку, что именно не влезло.

    ЧТО ЭТА ПРОВЕРКА ДЕЛАЕТ, А ЧТО НЕТ, И ЭТО ВАЖНО НЕ ПЕРЕПУТАТЬ. Она НЕ
    защищает память воркера: к моменту, когда ручка начинает исполняться,
    FastAPI уже разобрал форму и сложил файл в `UploadFile`. Оборвать входящий
    поток отсюда невозможно в принципе. Потолок самого потока стоит выше по
    стеку — в `main._supply_body_limit`, до разбора формы.

    Здесь остаётся ДОМЕННЫЙ предел: сказать человеку понятными словами, что
    картинка больше двух мегабайт или миниатюра больше шестидесяти четырёх
    килобайт, и не пустить такие байты в базу. Тексты приходят от вызывающего:
    у полного эскиза и у миниатюры пределы разные, и один текст на оба сказал
    бы неправду о том, что именно не влезло.

    `Content-Length` тут не участвует вовсе: он приходит от клиента, и файл,
    объявленный маленьким, может оказаться каким угодно.
    """
    data = bytearray()
    while True:
        chunk = await file.read(_SKETCH_READ_CHUNK)
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > limit:
            raise HTTPException(status_code=400, detail=too_big)
    return bytes(data)


def _sketch_too_big() -> str:
    """Один текст на оба места, где отвергается слишком большой эскиз.

    До слоя такой файл не доходит вовсе — он обрывается на чтении потока, — а
    человек обязан получить один и тот же ответ на одну и ту же причину
    (ТЗ F-20, Приложение А).
    """
    return (f"Файл больше {sp.SKETCH_MAX_BYTES // (1024 * 1024)} МБ — "
            "уменьшите картинку.")


def _thumb_too_big() -> str:
    return f"Миниатюра больше {sp.SKETCH_THUMB_MAX_BYTES // 1024} КБ."


def _sketch_response(payload: bytes, mime: str, name: str, etag: str,
                     request: Request) -> Response:
    """Отдача картинки: приватный кэш, ETag и 304 — но только СВОЕЙ организации.

    ПОРЯДОК ЗДЕСЬ ЧАСТЬ ЗАЩИТЫ, А НЕ ОФОРМЛЕНИЕ. Эта функция вызывается ПОСЛЕ
    того, как строка найдена через `sp.get_sketch`, то есть после проверки
    арендатора. Сделать наоборот — сверить `If-None-Match` раньше и ответить
    304 — значило бы рассказать чужому, что эскиз с таким номером и таким
    содержимым существует: 304 на несуществующее не приходит.

    `private` в кэше и `max-age=86400` (ТЗ F-23в) вместо прежнего `no-store`:
    картинка перестаёт перекачиваться при каждой перерисовке страницы, но
    остаётся в кэше ОДНОГО браузера и не попадает ни в один общий кэш.
    Остальная скупость отдачи прежняя: тип из нашего разбора, `nosniff`,
    имя файла без пользовательского текста, запрет на исполнение чего бы то ни
    было внутри.
    """
    headers = {
        "Cache-Control": "private, max-age=86400",
        # VARY: COOKIE — ЭТО НЕ ФОРМАЛЬНОСТЬ, А ВТОРАЯ ПОЛОВИНА `private`.
        # Кэш браузера ключуется адресом, а адрес эскиза — это номер строки, и
        # номера у разных организаций совпадают. Без этой строки один профиль
        # браузера, побывавший в двух организациях, мог показать во второй
        # картинку из первой — не спросив сервер вовсе, то есть мимо всех
        # наших проверок арендатора. Сессия живёт в куке, поэтому именно она
        # и объявляется частью ключа.
        "Vary": "Cookie",
        "ETag": etag,
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f'inline; filename="{name}"',
        "Content-Security-Policy": "default-src 'none'; sandbox",
    }
    if _etag_matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)
    return Response(content=payload, media_type=mime, headers=headers)


def _etag_matches(header: str | None, etag: str) -> bool:
    """`If-None-Match` по правилам заголовка, а не по равенству строк.

    Браузер вправе прислать список через запятую, `*` и слабую форму `W/"…"`.
    Сравнение «строка равна строке» промахнулось бы на каждом из трёх случаев,
    и картинка перекачивалась бы целиком при формально верном кэше — то есть
    пункт ТЗ был бы выполнен только на своём собственном тесте.
    """
    if not header:
        return False
    for part in header.split(","):
        candidate = part.strip()
        if candidate == "*":
            return True
        if candidate.startswith("W/"):
            candidate = candidate[2:]
        if candidate == etag:
            return True
    return False


@router.post("/sketches")
async def api_planning_sketch_upload(
    file: UploadFile = File(...),
    thumb: UploadFile | None = File(None),
    ctx: AuthContext = Depends(require_owner_api),
    db: Session = Depends(get_db),
):
    """СТАРЫЙ путь загрузки эскиза: файл отдельно, вещь потом.

    Ручка сохранена ради совместимости (ТЗ F-23а разрешает оба исхода) — но
    штатным путём страницы она больше не является: теперь вещь создаётся
    первой, а файл прикрепляется к ней (`POST /items/{id}/sketch`). Здесь
    остаётся ровно то, что нужно уже выпущенному клиенту и программному
    обращению.

    Именно этот порядок и рождал сирот: между «файл сохранён» и «вещь создана»
    есть отказ формы, и после него картинка остаётся ничьей. Поэтому уборка
    сирот старше суток идёт здесь же, при следующем сохранении (ТЗ F-23г).

    Формат определяется по САМИМ БАЙТАМ (`supply_planning.sniff_image`), имя
    файла и присланный `content_type` не участвуют в решении вовсе.
    """
    data = await _read_upload(file, sp.SKETCH_MAX_BYTES, _sketch_too_big())
    thumb_data = None
    if thumb is not None and getattr(thumb, "filename", None):
        thumb_data = await _read_upload(thumb, sp.SKETCH_THUMB_MAX_BYTES,
                                        _thumb_too_big())
    try:
        row = sp.save_sketch(db, ctx.org.id, data, _author(ctx), thumb=thumb_data)
        sp.cleanup_orphan_sketches(db, ctx.org.id)
    except (sp.PlanningError, IntegrityError) as exc:
        db.rollback()
        raise _fail(exc) from None
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise _fail(exc) from None
    return {"ok": True, "sketch_id": row.id, "width": row.width,
            "height": row.height, "mime": row.mime, "bytes": row.byte_len,
            "has_thumb": bool(row.thumb)}


@router.post("/items/{item_id}/sketch")
async def api_planning_item_sketch(
    item_id: int,
    file: UploadFile = File(...),
    thumb: UploadFile | None = File(None),
    ctx: AuthContext = Depends(require_owner_api),
    db: Session = Depends(get_db),
):
    """ШТАТНЫЙ путь (ТЗ F-23а): файл прикрепляется к уже созданной новинке.

    Что этим исправлено. Прежде страница отправляла картинку ПЕРВОЙ, и любая
    ошибка валидации формы — пустое название новинки, слишком длинная
    заметка — оставляла в базе до двух мегабайт, на которых уже никогда ничего
    не повиснет. Теперь порядок обратный: сначала вещь, и только потом байты.
    Ошибка формы больше не стоит ни одной строки в `supply_sketches`.

    Миниатюра приходит вторым файлом и рисуется браузером: Pillow в замыкании
    зависимостей проекта нет, и ТЗ F-23 назвало этот путь прямо. Сервер ей не
    верит — формат и сторона проверяются по самим байтам (`sp.sniff_thumb`).
    Миниатюра НЕОБЯЗАТЕЛЬНА: без неё эскиз сохраняется как раньше, и карточка
    показывает оригинал.
    """
    data = await _read_upload(file, sp.SKETCH_MAX_BYTES, _sketch_too_big())
    thumb_data = None
    if thumb is not None and getattr(thumb, "filename", None):
        thumb_data = await _read_upload(thumb, sp.SKETCH_THUMB_MAX_BYTES,
                                        _thumb_too_big())
    try:
        row = sp.attach_sketch(db, ctx.org.id, item_id, data, thumb_data,
                               _author(ctx))
        sketch_id = row.id
        sp.cleanup_orphan_sketches(db, ctx.org.id)
    except (sp.PlanningError, IntegrityError) as exc:
        db.rollback()
        raise _fail(exc) from None
    board = _commit(db, ctx.org.id, ctx.role)
    board["sketch_id"] = sketch_id
    return board


@router.get("/sketches/{sketch_id}")
def api_planning_sketch_read(
    sketch_id: int,
    request: Request,
    ctx: AuthContext = Depends(require_auth_api),
    db: Session = Depends(get_db),
):
    """Отдаёт эскиз СВОЕЙ организации. Публичной ссылки у него нет."""
    try:
        row = sp.get_sketch(db, ctx.org.id, sketch_id)
    except sp.PlanningError as exc:
        raise _fail(exc) from None
    ext = "png" if row.mime == "image/png" else "jpg"
    return _sketch_response(row.data, row.mime, f"sketch-{row.id}.{ext}",
                            f'"{row.sha256}"', request)


@router.get("/sketches/{sketch_id}/thumb")
def api_planning_sketch_thumb(
    sketch_id: int,
    request: Request,
    ctx: AuthContext = Depends(require_auth_api),
    db: Session = Depends(get_db),
):
    """Миниатюра эскиза (ТЗ F-23б). Права и арендатор — те же, что у оригинала.

    ЧТО ПРОИСХОДИТ У ЭСКИЗА БЕЗ МИНИАТЮРЫ, И ПОЧЕМУ НЕ 404. Миниатюры нет у
    всего, что загружено до этого пакета и старым путём, а пересчитать её на
    сервере нечем — Pillow в проекте отсутствует. Ответ 404 превратил бы
    картинки на живых карточках в битые квадраты, то есть починка кэша сломала
    бы показ. Поэтому такая ручка отдаёт ОРИГИНАЛ: он тяжелее, чем нужно, но
    это ровно то поведение, которое было до пакета, а не поломка.

    ETag у миниатюры свой (`sha256` оригинала с суффиксом): содержимое здесь
    другое, и общий ETag на два разных ответа означал бы, что браузер способен
    подставить одно вместо другого.
    """
    try:
        row = sp.get_sketch(db, ctx.org.id, sketch_id)
    except sp.PlanningError as exc:
        raise _fail(exc) from None
    if row.thumb:
        return _sketch_response(row.thumb, "image/png",
                                f"sketch-{row.id}-thumb.png",
                                f'"{row.sha256}-t"', request)
    ext = "png" if row.mime == "image/png" else "jpg"
    return _sketch_response(row.data, row.mime, f"sketch-{row.id}.{ext}",
                            f'"{row.sha256}"', request)
