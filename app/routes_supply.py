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

import json

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app import supply_sheets
from app.auth import AuthContext, require_auth_api, require_owner_api
from app.db import get_db

router = APIRouter(prefix="/api/supply", tags=["supply"])


#: Ключ флага в существующем `orgs.settings_json`. Колонки под него нет и не
#: будет: мера временная — до развилки владельца Р-1 о судьбе парсера.
PREVIEW_FLAG = "supply_sheets_preview"


def preview_enabled(org) -> bool:
    """Показывать ли этой организации предпросмотр (SUPPLY-FIX-1, F-08).

    ЖИВЁТ ЗДЕСЬ, А НЕ В `Org`, И ЭТО НЕ ВКУСОВЩИНА. Структурный сторож набора
    `tests/test_supply_sheets.py` требует, чтобы `app/models.py`, `app/db.py` и
    `app/tenancy.py` не знали про слой предпросмотра вовсе: снимок живёт в
    `connections.config_json`, своих таблиц и своих полей у него нет. Свойство
    с именем слоя на модели `Org` эту границу нарушило бы — знание о временной
    мере протекло бы в общую модель организации и пережило бы саму меру.

    FAIL-CLOSED. Флагом считается только настоящий `True`. Строка «true»,
    единица и «yes» им не являются намеренно: включение — операторское действие
    с известным способом (`tools/supply_sheets_preview.py`), и угадывать за
    оператора, что он имел в виду, здесь не из чего.

    Читается СЫРОЙ `settings_json`, а не `org.settings`: тот дозаполняет три
    ключа `DEFAULT_SETTINGS` и всё остальное из ответа выбрасывает.
    """
    try:
        data = json.loads(getattr(org, "settings_json", "") or "{}")
    except ValueError:
        return False
    return isinstance(data, dict) and data.get(PREVIEW_FLAG) is True


def _require_preview(ctx: AuthContext) -> None:
    """Обе ручки — за флагом организации (SUPPLY-FIX-1, F-08).

    ПОЧЕМУ 404, А НЕ 403. Парсер разбирает одну конкретную производственную
    таблицу: точные заголовки на фиксированных колонках, ровно два листа,
    fail-closed. Для организации без флага этого маршрута не существует — и
    отвечать надо ровно так же, как на любой несуществующий адрес, тем же
    текстом. 403 сообщил бы, что функция есть, но ей отказано, и человек пошёл
    бы искать, где её включить; 404 — правда о его аккаунте.

    Текст берётся тот же, каким приложение отвечает на несуществующий маршрут:
    иначе отличие ответа само становится признаком «здесь что-то есть».
    """
    if not preview_enabled(ctx.org):
        raise HTTPException(status_code=404, detail="Not Found")


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
    _require_preview(ctx)
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
    _require_preview(ctx)
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
