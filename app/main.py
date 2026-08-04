"""Приложение FastAPI: страницы (Jinja2), маршруты аутентификации, подключение API.

Шаблоны ищутся сначала в templates/ (зона frontend-агента), затем в
_stub_templates/ (временные заглушки backend'а) — когда появляются настоящие
шаблоны, они автоматически перекрывают заглушки.
"""
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import auth
from app.api import router as api_router
from app.crypto import is_prod
from app.routes_connect import router as connect_router
from app.routes_extra import router as extra_router
from app.routes_ms_app import router as ms_app_router
from app.routes_ms_vendor import router as ms_vendor_router
from app.db import get_db, init_db
from app.models import Connection, Membership, Org, User

BASE_DIR = Path(__file__).resolve().parent.parent
# Порядок важен: настоящие шаблоны перекрывают заглушки; несуществующая
# директория для FileSystemLoader не ошибка — templates/ подхватится, как появится.
TEMPLATE_DIRS = [BASE_DIR / "templates", BASE_DIR / "_stub_templates"]
STATIC_DIR = BASE_DIR / "static"
TRIAL_DAYS = 14

app = FastAPI(title="Оборот", docs_url=None, redoc_url=None)
app.include_router(api_router)
app.include_router(connect_router)
app.include_router(extra_router)
app.include_router(ms_vendor_router)
app.include_router(ms_app_router)


@app.middleware("http")
async def _security_headers_and_csrf(request: Request, call_next):
    """CSP frame-ancestors + CSRF-защита изменяющих запросов к /api.

    CSP: приложение маркетплейса МС работает полностраничным iframe — вместо
    полного запрета фреймов ограничиваем предков (современная замена
    X-Frame-Options). Ставим и на 500 (обработка исключения ниже).

    CSRF: в embedded-режиме сессионная кука на проде SameSite=None (иначе не
    долетит в iframe МС), поэтому Lax-защита не работает. Для изменяющих
    методов на /api/* требуем кастомный заголовок X-Oborot-CSRF: браузер не
    даст поставить его в кросс-доменном запросе без CORS-preflight, а CORS мы
    не разрешаем — значит сторонний сайт не сможет дёрнуть наши ручки с
    кукой пользователя. Свой фронт заголовок шлёт всегда (см. app.js api()).
    Vendor-lifecycle (/ms/...) сюда не попадает: это server-to-server c JWT,
    без куки — CSRF там неприменим.
    """
    if (
        request.method in ("POST", "PUT", "PATCH", "DELETE")
        and request.url.path.startswith("/api/")
        and request.headers.get("X-Oborot-CSRF") is None
    ):
        from fastapi.responses import JSONResponse as _JR
        resp = _JR(status_code=403, content={"detail": "CSRF: запрос отклонён"})
        resp.headers["Content-Security-Policy"] = "frame-ancestors 'self' https://online.moysklad.ru"
        return resp
    response = await call_next(request)
    response.headers.setdefault(
        "Content-Security-Policy",
        "frame-ancestors 'self' https://online.moysklad.ru",
    )
    return response

from app import scheduler as _scheduler  # noqa: E402
_scheduler.attach(app)
templates = Jinja2Templates(directory=[str(d) for d in TEMPLATE_DIRS])

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.on_event("startup")
def _startup() -> None:
    init_db()
    from app import exclusions as _exclusions
    _exclusions.ensure_schema()


# ── Помощники ────────────────────────────────────────────────────────────────

def _resolve_embedded(request: Request) -> bool | None:
    """Встроенный режим (iframe МойСклад): кука oborot_embed, ставится в /ms/app.

    В dev (OBOROT_ENV!=prod) допускаем query-переключатель ?embed=1 / ?embed=0 —
    чтобы открыть встроенный вид локально без реального МС; при этом он ещё и
    залипает кукой (возврат True/False), чтобы переходы по табам сохраняли режим.
    На проде query игнорируется — только кука. None = «переключать куку не надо».
    """
    embedded = auth.read_embed(request)
    override = None
    if not is_prod():
        q = request.query_params.get("embed")
        if q == "1":
            embedded, override = True, True
        elif q == "0":
            embedded, override = False, False
    return embedded, override


def _page(request: Request, ctx: auth.AuthContext, template: str, active: str, page_title: str,
          db: Session | None = None):
    """Рендер страницы с обязательным контекстом {user, org, active, page_title}."""
    org = ctx.org
    embedded, override = _resolve_embedded(request)
    # Флаг «данные демо»: слова про синтетические данные показываем только когда
    # активное подключение — demo. При реальном МойСкладе полоска говорит лишь о триале.
    is_demo = False
    if db is not None:
        is_demo = (
            db.execute(
                select(Connection.id).where(
                    Connection.org_id == org.id,
                    Connection.status == "active",
                    Connection.kind == "demo",
                )
            ).first()
            is not None
        )
    response = templates.TemplateResponse(
        request,
        template,
        {
            "user": {"name": ctx.user.name, "email": ctx.user.email},
            "org": {
                "name": org.name,
                "plan": org.plan,
                # source нужен шаблону: не-МС-триалу показываем демо-полоску,
                # МС-аккаунту (source='ms_app') подписку показывает сам МойСклад.
                "source": getattr(org, "source", "saas"),
                "demo": is_demo,
                "trial_ends_at": org.trial_ends_at.date().isoformat() if org.trial_ends_at else None,
            },
            "active": active,
            "page_title": page_title,
            "embedded": embedded,
        },
    )
    # dev-only: ?embed=1/0 залипает кукой, чтобы навигация сохраняла режим.
    if override is True:
        auth.set_embed(response)
    elif override is False:
        auth.clear_embed(response)
    return response


def _has_active_connection(db: Session, org_id: int) -> bool:
    return (
        db.execute(
            select(Connection.id).where(
                Connection.org_id == org_id, Connection.status == "active"
            )
        ).first()
        is not None
    )


def _authed_page(request: Request, db: Session, template: str, active: str, page_title: str):
    """Страница под сессией: без авторизации — redirect /login."""
    ctx = auth.resolve_auth(request, db)
    if ctx is None:
        return RedirectResponse("/login", status_code=302)
    return _page(request, ctx, template, active, page_title, db=db)


# ── Аутентификация ───────────────────────────────────────────────────────────

def _render_auth(request: Request, template: str, **extra):
    return templates.TemplateResponse(
        request,
        template,
        {"user": None, "org": None, "active": "", "page_title": "Оборот", **extra},
    )


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return _render_auth(request, "login.html")


@app.post("/login")
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    ip = request.client.host if request.client else "?"
    email_norm = email.strip().lower()
    limiter_keys = (f"ip:{ip}", f"acc:{ip}:{email_norm}")
    if not all(auth.login_limiter.check(k) for k in limiter_keys):
        return _render_auth(
            request, "login.html", email=email_norm,
            error="Слишком много попыток входа. Подождите несколько минут и попробуйте снова.",
        )
    user = db.execute(
        select(User).where(User.email == email_norm)
    ).scalars().first()
    if user is None or not auth.verify_password(password, user.pw_hash):
        for k in limiter_keys:
            auth.login_limiter.hit(k)
        return _render_auth(request, "login.html", email=email_norm, error="Неверный e-mail или пароль")
    for k in limiter_keys:
        auth.login_limiter.reset(k)
    member = db.execute(
        select(Membership).where(Membership.user_id == user.id)
    ).scalars().first()
    if member is None:
        return _render_auth(request, "login.html", email=email_norm, error="У пользователя нет организации")
    response = RedirectResponse("/", status_code=303)
    auth.set_session(response, user.id, member.org_id)
    return response


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    return _render_auth(request, "register.html")


@app.post("/register")
def register_submit(
    request: Request,
    name: str = Form(""),
    email: str = Form(...),
    password: str = Form(...),
    org_name: str = Form(""),
    db: Session = Depends(get_db),
):
    email_norm = email.strip().lower()
    if not email_norm or "@" not in email_norm:
        return _render_auth(request, "register.html", name=name, org_name=org_name, email=email, error="Укажите корректный e-mail")
    if len(password) < 8:
        return _render_auth(request, "register.html", name=name, org_name=org_name, email=email, error="Пароль — минимум 8 символов")
    exists = db.execute(select(User.id).where(User.email == email_norm)).first()
    if exists:
        return _render_auth(request, "register.html", name=name, org_name=org_name, email=email, error="Такой e-mail уже зарегистрирован")

    user = User(email=email_norm, pw_hash=auth.hash_password(password), name=name.strip())
    org = Org(
        name=org_name.strip() or "Моя компания",
        plan="trial",
        trial_ends_at=datetime.utcnow() + timedelta(days=TRIAL_DAYS),
    )
    db.add_all([user, org])
    db.flush()
    db.add(Membership(user_id=user.id, org_id=org.id, role="owner"))
    db.commit()

    response = RedirectResponse("/", status_code=303)
    auth.set_session(response, user.id, org.id)
    return response


@app.post("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    auth.clear_session(response)
    return response


# ── Страницы ─────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def dashboard_page(request: Request, db: Session = Depends(get_db)):
    ctx = auth.resolve_auth(request, db)
    if ctx is None:
        # Неавторизованным показываем публичный лендинг (самодостаточный шаблон).
        return templates.TemplateResponse(request, "landing.html", {})
    if not _has_active_connection(db, ctx.org.id):
        return RedirectResponse("/onboarding", status_code=302)
    # Дашборд «Показатели» скрыт (продукт сфокусирован на Оборачиваемости и
    # Активном стоке) — главная ведёт сразу в Оборачиваемость.
    return RedirectResponse("/turnover", status_code=302)


@app.get("/onboarding", response_class=HTMLResponse)
def onboarding_page(request: Request, db: Session = Depends(get_db)):
    ctx = auth.resolve_auth(request, db)
    if ctx is None:
        return RedirectResponse("/login", status_code=302)
    if _has_active_connection(db, ctx.org.id):
        return RedirectResponse("/", status_code=302)
    return _page(request, ctx, "onboarding.html", "onboarding", "Подключение данных", db=db)


@app.get("/replenish", response_class=HTMLResponse)
def replenish_page(request: Request, db: Session = Depends(get_db)):
    return _authed_page(request, db, "replenish.html", "replenish", "Что заказать")


@app.get("/turnover", response_class=HTMLResponse)
def turnover_page(request: Request, db: Session = Depends(get_db)):
    return _authed_page(request, db, "turnover.html", "turnover", "Оборачиваемость")


@app.get("/stocks", response_class=HTMLResponse)
def stocks_page(request: Request, db: Session = Depends(get_db)):
    return _authed_page(request, db, "stocks.html", "stocks", "Активный сток")


@app.get("/orders", response_class=HTMLResponse)
def orders_page(request: Request, db: Session = Depends(get_db)):
    return _authed_page(request, db, "orders.html", "orders", "Заказы на производство")


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)):
    return _authed_page(request, db, "settings.html", "settings", "Настройки")
