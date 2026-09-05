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
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import supply_planning as sp
from app.auth import AuthContext, require_auth_api, require_owner_api
from app.db import get_db

router = APIRouter(prefix="/api/supply/planning", tags=["supply-planning"])

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
    """Один разбор доменных ошибок на все ручки — чтобы коды не разъезжались."""
    if isinstance(exc, sp.ValidationError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, sp.NotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, sp.StaleWrite):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, sp.DuplicateOp):
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
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Это действие уже выполнено. Обновите страницу.") from None
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
    """Кандидаты каталога по каноническому имени. Подсказка, а не привязка."""
    return {"options": sp.catalog_options(db, ctx.org.id, q)}


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
    except sp.PlanningError as exc:
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
    except sp.PlanningError as exc:
        db.rollback()
        raise _fail(exc) from None
    return _commit(db, ctx.org.id, ctx.role)


@router.post("/items")
def api_planning_item_create(
    payload: dict = Body(...),
    ctx: AuthContext = Depends(require_owner_api),
    db: Session = Depends(get_db),
):
    """Вещь каталога или полноценная новинка с эскизом."""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Ожидался объект JSON.")
    try:
        if sp.check_op(db, ctx.org.id, sp.parse_op_id(payload)):
            return sp.board(db, ctx.org.id, ctx.role)
        sp.create_item(db, ctx.org.id, payload, _author(ctx))
    except sp.PlanningError as exc:
        db.rollback()
        raise _fail(exc) from None
    return _commit(db, ctx.org.id, ctx.role)


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
    except sp.PlanningError as exc:
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
    except sp.PlanningError as exc:
        db.rollback()
        raise _fail(exc) from None
    return _commit(db, ctx.org.id, ctx.role)


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
    except sp.PlanningError as exc:
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
    except sp.PlanningError as exc:
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
    except sp.PlanningError as exc:
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
    except sp.PlanningError as exc:
        db.rollback()
        raise _fail(exc) from None
    return _commit(db, ctx.org.id, ctx.role)


@router.post("/sketches")
async def api_planning_sketch_upload(
    file: UploadFile = File(...),
    ctx: AuthContext = Depends(require_owner_api),
    db: Session = Depends(get_db),
):
    """Приватный эскиз новинки. Байты идут в базу, а не на диск.

    Читаем ПОТОКОМ с потолком: `Content-Length` приходит от клиента, и верить
    ему нельзя — файл, объявленный маленьким, может оказаться каким угодно.
    Формат определяется по самим байтам (`supply_planning.sniff_image`), имя
    файла и присланный `content_type` не участвуют в решении вовсе.
    """
    data = bytearray()
    limit = sp.SKETCH_MAX_BYTES
    while True:
        chunk = await file.read(_SKETCH_READ_CHUNK)
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > limit:
            raise HTTPException(
                status_code=400,
                detail=f"Файл больше {limit // (1024 * 1024)} МБ.")
    try:
        row = sp.save_sketch(db, ctx.org.id, bytes(data), _author(ctx))
    except sp.PlanningError as exc:
        db.rollback()
        raise _fail(exc) from None
    db.commit()
    return {"ok": True, "sketch_id": row.id, "width": row.width,
            "height": row.height, "mime": row.mime, "bytes": row.byte_len}


@router.get("/sketches/{sketch_id}")
def api_planning_sketch_read(
    sketch_id: int,
    ctx: AuthContext = Depends(require_auth_api),
    db: Session = Depends(get_db),
):
    """Отдаёт эскиз СВОЕЙ организации. Публичной ссылки у него нет.

    Отдача намеренно скупая: тип из нашего разбора (а не из присланного
    заголовка), `nosniff`, `attachment`-имя без пользовательского текста и
    `private` в кэше. Картинку видно на своей странице, но она не превращается
    в файл, который можно раздать по ссылке кому угодно.
    """
    try:
        row = sp.get_sketch(db, ctx.org.id, sketch_id)
    except sp.PlanningError as exc:
        raise _fail(exc) from None
    ext = "png" if row.mime == "image/png" else "jpg"
    return Response(
        content=row.data,
        media_type=row.mime,
        headers={
            "Cache-Control": "private, max-age=0, no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f'inline; filename="sketch-{row.id}.{ext}"',
            "Content-Security-Policy": "default-src 'none'; sandbox",
        },
    )
