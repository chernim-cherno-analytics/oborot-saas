"""Вход в «Оборот» из iframe МойСклад (SSO без пароля).

МС открывает наш sourceUrl полностраничным iframe в разделе «Приложения»:

  GET /ms/app?contextKey=XXX

Мы меняем contextKey на контекст пользователя (POST Vendor API
/context/{contextKey} с нашим JWT — app.ms_vendor.get_context), получаем
{accountId, uid, ...} и:

  1) находим Org по accountId (нет — 404 с человеческим текстом:
     lifecycle-PUT ещё не долетел или приложение не установлено);
  2) находим/создаём User по ms_uid (email = <uid>@ms.local — технический,
     писем на него не шлём; пароль случайный: вход только через МС);
     членство: первый пользователь организации — owner, остальные — member;
  3) ставим обычную сессионную куку и редиректим на /.

Кука в iframe: на проде (OBOROT_ENV=prod, https) — SameSite=None + Secure,
иначе браузер не пришлёт её в третьестороннем фрейме; в dev (http) None+Secure
невозможна — остаётся Lax, как у обычного входа (задокументировано в
auth.set_session). CSP frame-ancestors для online.moysklad.ru ставит
middleware в main.py.
"""
import html
import secrets

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import auth, ms_vendor
from app.crypto import is_prod
from app.db import get_db
from app.models import Membership, Org, User

router = APIRouter()


def _error_page(status_code: int, title: str, text: str) -> HTMLResponse:
    """Человеческая страница ошибки: /ms/app открывается в iframe, JSON там нечитаем."""
    body = (
        "<!doctype html><html lang='ru'><head><meta charset='utf-8'>"
        "<title>Оборот</title></head>"
        "<body style=\"font-family:-apple-system,'Segoe UI',Roboto,sans-serif;"
        "background:#f6f7f9;display:flex;justify-content:center;padding-top:80px\">"
        "<div style='background:#fff;border:1px solid #e5e7eb;border-radius:8px;"
        "padding:32px 40px;max-width:520px'>"
        f"<h1 style='font-size:20px;margin:0 0 12px'>{html.escape(title)}</h1>"
        f"<p style='color:#374151;margin:0'>{html.escape(text)}</p>"
        "</div></body></html>"
    )
    return HTMLResponse(body, status_code=status_code)


@router.get("/ms/app")
async def ms_app_entry(
    request: Request,
    context_key: str = Query(default="", alias="contextKey"),
    db: Session = Depends(get_db),
):
    if not context_key:
        return _error_page(
            400, "Не хватает ключа входа",
            "Откройте «Оборот» из раздела «Приложения» вашего аккаунта МойСклад.",
        )
    try:
        ctx = await ms_vendor.get_context(context_key)
    except HTTPException as exc:
        return _error_page(exc.status_code, "Не получилось войти", str(exc.detail))

    account_id = str(ctx.get("accountId") or "")
    uid = str(ctx.get("uid") or "")
    if not account_id or not uid:
        return _error_page(
            502, "Не получилось войти",
            "МойСклад вернул неполный контекст пользователя. Обновите страницу.",
        )

    org = db.execute(
        select(Org).where(Org.ms_account_id == account_id)
    ).scalars().first()
    if org is None:
        return _error_page(
            404, "Приложение не установлено",
            "Для этого аккаунта МойСклад «Оборот» ещё не активирован. "
            "Установите приложение из каталога МойСклад или подождите минуту "
            "после установки и обновите страницу.",
        )

    # ── Пользователь по ms_uid (создаётся один раз) ──────────────────────────
    user = db.execute(select(User).where(User.ms_uid == uid)).scalars().first()
    if user is None:
        email = f"{uid}@ms.local".lower()
        # Коллизия по техническому email (теоретическая) не должна ломать вход.
        if db.execute(select(User.id).where(User.email == email)).first():
            email = f"{uid}+{secrets.token_hex(4)}@ms.local".lower()
        user = User(
            email=email,
            # Пароль случайный и никому не сообщается: вход — только через МС.
            pw_hash=auth.hash_password(secrets.token_urlsafe(24)),
            name=str(ctx.get("fullName") or ctx.get("name") or uid),
            ms_uid=uid,
        )
        db.add(user)
        db.flush()

    member = db.get(Membership, (user.id, org.id))
    if member is None:
        has_owner = db.execute(
            select(Membership.user_id).where(
                Membership.org_id == org.id, Membership.role == "owner"
            )
        ).first()
        db.add(Membership(
            user_id=user.id, org_id=org.id,
            role="member" if has_owner else "owner",
        ))
    db.commit()

    response = RedirectResponse("/", status_code=302)
    # В iframe на проде кука должна быть SameSite=None+Secure (см. докстринг).
    samesite = "none" if is_prod() else "lax"
    auth.set_session(response, user.id, org.id, samesite=samesite)
    # Помечаем сессию встроенной: base.html отрендерит компактную оболочку без
    # нашего сайдбара/топбара (их даёт МойСклад снаружи).
    auth.set_embed(response, samesite=samesite)
    return response
