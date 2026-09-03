# -*- coding: utf-8 -*-
"""SUPPLY-2: API предпросмотра производственных Google Sheets.

Две ручки и ничего сверх них:

  * `POST /api/supply/sheets/refresh` — владелец нажал «Обновить предпросмотр».
    Это ОДИН пользовательский поступок, поэтому настройка источника и его
    чтение живут в одном запросе: разводить их на «сохранить» и «обновить»
    значило бы завести состояние «сохранили, но не проверили», в котором
    страница показывает ссылку, за которой ничего нет.
  * `GET /api/supply/sheets` — чтение снимка. Владелец и участник видят одно и
    то же; отличается только право обновлять.

Арендатор берётся ТОЛЬКО из сессии (`ctx.org.id`). Ни `org_id`, ни
`connection_id` от клиента здесь не принимаются вовсе — не «игнорируются», а
не существуют в контракте: параметр, который можно прислать, рано или поздно
кто-нибудь начнёт читать.

CSRF на POST — штатный: заголовок `X-Oborot-CSRF` требует общий middleware
`app.main._security_headers_and_csrf` для всех изменяющих `/api/*`, и эта
ручка ничем от соседей не отличается.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app import supply_sheets
from app.auth import AuthContext, require_auth_api, require_owner_api
from app.db import get_db

router = APIRouter(prefix="/api/supply", tags=["supply"])


@router.get("/sheets")
def api_supply_sheets(
    sheet: str = Query("", max_length=supply_sheets.MAX_SHEET_NAME_CHARS),
    queue: str = Query("all"),
    q: str = Query("", max_length=supply_sheets.MAX_SEARCH_CHARS),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    ctx: AuthContext = Depends(require_auth_api),
    db: Session = Depends(get_db),
):
    """Снимок предпросмотра своей организации. Только чтение.

    `q` — поиск по наименованию, артикулу и цвету. Он сужает ВЕСЬ применимый
    набор строк снимка до нарезки на страницы, поэтому `total` и догрузка
    относятся к результату поиска, а не к тем строкам, которые страница успела
    загрузить. Ни одной записи поиск не делает и ни одного сетевого вызова не
    порождает: снимок уже лежит в носителе, читается он целиком и в памяти.
    """
    try:
        return supply_sheets.preview(
            db, ctx.org.id, role=ctx.role, sheet=sheet or None,
            queue=queue, offset=offset, limit=limit, q=q,
        )
    except supply_sheets.ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except supply_sheets.CarrierConfigError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.post("/sheets/refresh")
def api_supply_sheets_refresh(
    payload: dict = Body(...),
    ctx: AuthContext = Depends(require_owner_api),
    db: Session = Depends(get_db),
):
    """Прочитать оба листа заново и записать один последний снимок.

    Коды ответа разведены намеренно, потому что человеку нужно разное:

      400 — поправьте ссылку или имена листов (это ваша строка);
      409 — предпросмотру негде жить: у организации нет основного подключения
            (и тогда ни одного сетевого вызова и ни одной записи не было);
      502 — источник не отдал того, что мы умеем читать. Прежний снимок цел.
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Ожидался объект JSON.")
    try:
        result = supply_sheets.refresh(
            db, ctx.org.id,
            payload.get("spreadsheet_url"),
            payload.get("sheet_names"),
        )
    except supply_sheets.ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except supply_sheets.NoCarrierError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except supply_sheets.CarrierConfigError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except supply_sheets.SourceError as exc:
        # Наружу уходит только наше собственное сообщение: тела чужого ответа,
        # заголовков и адреса в нём нет по построению (app/supply_sheets.py).
        raise HTTPException(status_code=502, detail=str(exc)) from None
    return {"ok": True, **result}
