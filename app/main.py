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
templates = Jinja2Templates(directory=[str(d) for d in TEMPLATE_DIRS])

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.on_event("startup")
def _startup() -> None:
    init_db()


# ── Помощники ────────────────────────────────────────────────────────────────

def _page(request: Request, ctx: auth.AuthContext, template: str, active: str, page_title: str):
    """Рендер страницы с обязательным контекстом {user, org, active, page_title}."""
    org = ctx.org
    return templates.TemplateResponse(
        request,
        template,
        {
            "user": {"name": ctx.user.name, "email": ctx.user.email},
            "org": {
                "name": org.name,
                "plan": org.plan,
                "trial_ends_at": org.trial_ends_at.date().isoformat() if org.trial_ends_at else None,
            },
            "active": active,
            "page_title": page_title,
        },
    )


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
    return _page(request, ctx, template, active, page_title)


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
            request, "login.html",
            error="Слишком много попыток входа. Подождите несколько минут и попробуйте снова.",
        )
    user = db.execute(
        select(User).where(User.email == email_norm)
    ).scalars().first()
    if user is None or not auth.verify_password(password, user.pw_hash):
        for k in limiter_keys:
            auth.login_limiter.hit(k)
        return _render_auth(request, "login.html", error="Неверный e-mail или пароль")
    for k in limiter_keys:
        auth.login_limiter.reset(k)
    member = db.execute(
        select(Membership).where(Membership.user_id == user.id)
    ).scalars().first()
    if member is None:
        return _render_auth(request, "login.html", error="У пользователя нет организации")
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
        return _render_auth(request, "register.html", error="Укажите корректный e-mail")
    if len(password) < 6:
        return _render_auth(request, "register.html", error="Пароль — минимум 6 символов")
    exists = db.execute(select(User.id).where(User.email == email_norm)).first()
    if exists:
        return _render_auth(request, "register.html", error="Такой e-mail уже зарегистрирован")

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
        return RedirectResponse("/login", status_code=302)
    if not _has_active_connection(db, ctx.org.id):
        return RedirectResponse("/onboarding", status_code=302)
    return _page(request, ctx, "dashboard.html", "dashboard", "Показатели")


@app.get("/onboarding", response_class=HTMLResponse)
def onboarding_page(request: Request, db: Session = Depends(get_db)):
    ctx = auth.resolve_auth(request, db)
    if ctx is None:
        return RedirectResponse("/login", status_code=302)
    if _has_active_connection(db, ctx.org.id):
        return RedirectResponse("/", status_code=302)
    return _page(request, ctx, "onboarding.html", "onboarding", "Подключение данных")


@app.get("/replenish", response_class=HTMLResponse)
def replenish_page(request: Request, db: Session = Depends(get_db)):
    return _authed_page(request, db, "replenish.html", "replenish", "Что заказать")


@app.get("/turnover", response_class=HTMLResponse)
def turnover_page(request: Request, db: Session = Depends(get_db)):
    return _authed_page(request, db, "turnover.html", "turnover", "Оборачиваемость")


@app.get("/stocks", response_class=HTMLResponse)
def stocks_page(request: Request, db: Session = Depends(get_db)):
    return _authed_page(request, db, "stocks.html", "stocks", "Остатки")


@app.get("/orders", response_class=HTMLResponse)
def orders_page(request: Request, db: Session = Depends(get_db)):
    return _authed_page(request, db, "orders.html", "orders", "Заказы на производство")


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)):
    return _authed_page(request, db, "settings.html", "settings", "Настройки")
