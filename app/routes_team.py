# -*- coding: utf-8 -*-
"""«Сотрудники и роли»: роли организации, назначение ролей, приглашения.

Решение владельца 15.11.2026: «нужно, чтобы были роли и владелец мог их
раздавать. Условно как в МойСкладе». Модель прав и встроенные роли описаны в
app/roles.py, здесь только ручки и страницы.

Правила доступа:
  * весь раздел (чтение и запись) под правом users_manage;
  * чужая организация: 404 без данных (роль, сотрудник, приглашение ищутся
    только в организации сессии, чужой id неотличим от несуществующего);
  * роль «Владелец» назначает, снимает и выдаёт приглашением только владелец;
  * не-владелец с users_manage не может выдать права, которых нет у него
    самого, и не может менять роль или убирать того, у кого прав больше;
  * в организации всегда остаётся хотя бы один владелец.

Приглашение по ссылке: одноразовое, 7 дней, в базе только sha256 токена (тот
же подход, что у сброса пароля D-63). Токен живёт во ФРАГМЕНТЕ ссылки
(`/invite#t=...`): на сервер он уходит только в теле POST, в журнал доступа,
прокси и Referer не попадает.
"""
from __future__ import annotations

import secrets
import threading
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import auth, roles
from app.account_recovery import token_hash
from app.auth import AuthContext, require_perm
from app.db import get_db
from app.models import Membership, Org, OrgInvite, OrgRole, User

# Прод работает одним процессом (run.py), поэтому замка процесса достаточно.
_OWNER_LOCK = threading.Lock()

router = APIRouter()

INVITE_TTL = timedelta(days=7)
MAX_ACTIVE_INVITES = 50
MAX_ROLES = 50
INVITE_INVALID = ("Приглашение недействительно: ссылка уже использована, отозвана "
                  "или устарела. Попросите владельца организации прислать новую.")

# Подбор токена приглашения бессмыслен (32 случайных байта), лимит здесь против
# шума с одного адреса. Жёсткий и только по надёжному адресу, как у сброса
# пароля (auth.reset_ip_limiter).
invite_ip_limiter = auth.LoginLimiter(max_attempts=10, window_sec=auth.LOGIN_WINDOW_SEC)

_TEAM = require_perm("users_manage")


def _id_path():
    return Path(ge=1, le=2_147_483_647)


# ── Внутреннее ───────────────────────────────────────────────────────────────

def _org_role(db: Session, org_id: int, role_id: int) -> OrgRole:
    role = db.get(OrgRole, role_id)
    if role is None or role.org_id != org_id:
        raise HTTPException(status_code=404, detail="Роль не найдена")
    return role


def _org_member(db: Session, org_id: int, user_id: int) -> Membership:
    member = db.get(Membership, (user_id, org_id))
    if member is None:
        raise HTTPException(status_code=404, detail="Сотрудник не найден")
    return member


def _effective_role_id(member: Membership, builtin: dict) -> int | None:
    if member.role == "owner":
        return builtin[roles.OWNER].id
    if member.role_id is None:
        return builtin[roles.EMPLOYEE].id
    return member.role_id


def _forbid_unless_subset(ctx: AuthContext, perms, detail: str) -> None:
    """Не-владелец не выдаёт и не трогает права, которых у него нет."""
    if ctx.role == "owner":
        return
    if not set(perms) <= set(ctx.perms):
        raise HTTPException(status_code=403, detail=detail)


def _require_owner_for_owner_role(ctx: AuthContext) -> None:
    if ctx.role != "owner":
        raise HTTPException(
            status_code=403,
            detail="Роль «Владелец» назначает и снимает только владелец организации",
        )


def _clean_perms(raw) -> frozenset[str]:
    if raw is None:
        return frozenset()
    unknown = [k for k in raw if k not in roles.PERM_KEYS]
    if unknown:
        raise HTTPException(status_code=422, detail=f"Неизвестное право: {unknown[0]}")
    return frozenset(raw)


def _clean_name(db: Session, org_id: int, raw: str, skip_id: int | None = None) -> str:
    name = " ".join((raw or "").split())
    if not name:
        raise HTTPException(status_code=422, detail="Укажите название роли")
    if len(name) > roles.ROLE_NAME_MAX:
        raise HTTPException(status_code=422,
                            detail=f"Название роли не длиннее {roles.ROLE_NAME_MAX} символов")
    # Сравнение в Python, а не lower() в SQL: lower() SQLite не знает
    # кириллицы, и «Склад» со «склад» считались бы разными ролями.
    rows = db.execute(
        select(OrgRole.id, OrgRole.name).where(OrgRole.org_id == org_id)
    ).all()
    if any(rid != skip_id and (n or "").casefold() == name.casefold() for rid, n in rows):
        raise HTTPException(status_code=422, detail="Роль с таким названием уже есть")
    return name


def _active_invites(db: Session, org_id: int, now: datetime):
    return db.execute(
        select(OrgInvite).where(
            OrgInvite.org_id == org_id, OrgInvite.used_at.is_(None),
            OrgInvite.revoked_at.is_(None), OrgInvite.expires_at > now,
        ).order_by(OrgInvite.id.desc())
    ).scalars().all()


def team_state(db: Session, ctx: AuthContext) -> dict:
    """Всё, что показывает страница «Сотрудники и роли»."""
    org_id = ctx.org.id
    builtin = roles.ensure_builtin_roles(db, org_id)
    org_roles = db.execute(
        select(OrgRole).where(OrgRole.org_id == org_id).order_by(OrgRole.id)
    ).scalars().all()
    by_id = {r.id: r for r in org_roles}
    order = {code: i for i, code in enumerate(roles.BUILTIN_ORDER)}
    org_roles = sorted(org_roles, key=lambda r: (order.get(r.builtin, 99), r.id))

    rows = db.execute(
        select(User, Membership).join(Membership, Membership.user_id == User.id)
        .where(Membership.org_id == org_id).order_by(User.id)
    ).all()
    counts: dict[int, int] = {}
    members = []
    for user, m in rows:
        rid = _effective_role_id(m, builtin)
        role = by_id.get(rid)
        if role is None or (role.builtin == roles.OWNER and m.role != "owner"):
            rid, name = None, "Без роли"
        else:
            name = role.name
        if rid is not None:
            counts[rid] = counts.get(rid, 0) + 1
        members.append({
            "user_id": user.id, "name": user.name, "email": user.email,
            "role_id": rid, "role_name": name,
            "is_owner": m.role == "owner", "is_self": user.id == ctx.user.id,
        })

    now = datetime.utcnow()
    creators = {}
    invites = []
    for inv in _active_invites(db, org_id, now):
        role = by_id.get(inv.role_id)
        if inv.created_by and inv.created_by not in creators:
            # Автора показываем, только если он сейчас в ЭТОЙ организации:
            # id удалённого пользователя SQLite может отдать новому человеку
            # из чужой организации (ревью Codex #110, P1).
            u = db.get(User, inv.created_by)
            in_org = u is not None and db.get(Membership, (u.id, org_id)) is not None
            creators[inv.created_by] = (u.name or u.email) if in_org else ""
        invites.append({
            "id": inv.id,
            "role_id": inv.role_id,
            "role_name": role.name if role else "Без роли",
            "created_at": inv.created_at.isoformat(),
            "expires_at": inv.expires_at.isoformat(),
            "created_by": creators.get(inv.created_by, ""),
        })

    return {
        "org": {"name": ctx.org.name},
        "me": {"user_id": ctx.user.id, "is_owner": ctx.role == "owner",
               "perms": sorted(ctx.perms)},
        "perms_catalog": [{"key": k, "title": t, "group": g} for k, t, g in roles.PERMS],
        "roles": [{
            "id": r.id, "name": r.name, "builtin": r.builtin,
            "perms": [k for k in roles.PERM_KEYS if k in roles.role_perms(r)],
            "editable": not r.builtin,
            "members_count": counts.get(r.id, 0),
        } for r in org_roles],
        "members": members,
        "invites": invites,
    }


# ── Страницы ─────────────────────────────────────────────────────────────────

@router.get("/team", response_class=HTMLResponse)
def team_page(request: Request, db: Session = Depends(get_db)):
    from app.main import _page

    ctx = auth.resolve_auth(request, db)
    if ctx is None:
        return RedirectResponse("/login", status_code=302)
    if not ctx.can("users_manage"):
        # Страница целиком про управление людьми: без права показывать нечего.
        return RedirectResponse("/account", status_code=302)
    # active="team": своей подсказки у раздела нет, а подсказка «Настроек»
    # здесь говорила бы о складах и порогах.
    return _page(request, ctx, "team.html", "team", "Сотрудники и роли", db=db)


_INVITE_PAGE_HEADERS = {"Referrer-Policy": "no-referrer", "Cache-Control": "no-store"}


@router.get("/invite", response_class=HTMLResponse)
def invite_page(request: Request, db: Session = Depends(get_db)):
    """Страница входа по приглашению. Токен во фрагменте, сервер его не видит."""
    from app.main import _render_auth

    ctx = auth.resolve_auth(request, db)
    response = _render_auth(
        request, "invite.html",
        signed_in=({"name": ctx.user.name, "email": ctx.user.email} if ctx else None),
    )
    response.headers.update(_INVITE_PAGE_HEADERS)
    return response


# ── API раздела ──────────────────────────────────────────────────────────────

@router.get("/api/team")
def api_team(ctx: AuthContext = Depends(_TEAM), db: Session = Depends(get_db)):
    return team_state(db, ctx)


@router.get("/api/team/roles")
def api_team_roles(ctx: AuthContext = Depends(_TEAM), db: Session = Depends(get_db)):
    state = team_state(db, ctx)
    return {"roles": state["roles"], "perms_catalog": state["perms_catalog"]}


class MemberRoleIn(BaseModel):
    role_id: int = Field(ge=1, le=2_147_483_647)


@router.post("/api/team/members/{user_id}/role")
def api_team_member_role(
    body: MemberRoleIn, user_id: int = _id_path(),
    ctx: AuthContext = Depends(_TEAM), db: Session = Depends(get_db),
):
    # Ревью Codex #110 (P1): проверка «последний владелец» и изменение
    # выполняются под одним замком, иначе два одновременных запроса оба
    # видят двух владельцев и оставляют организацию без хозяина.
    with _OWNER_LOCK:
        roles.ensure_builtin_roles(db, ctx.org.id)
        member = _org_member(db, ctx.org.id, user_id)
        role = _org_role(db, ctx.org.id, body.role_id)
        if role.builtin == roles.OWNER or member.role == "owner":
            _require_owner_for_owner_role(ctx)
        current, _name = roles.member_perms(db, member)
        _forbid_unless_subset(ctx, current, "Нельзя менять роль сотрудника, у которого прав больше, чем у вас")
        _forbid_unless_subset(ctx, roles.role_perms(role), "Нельзя выдать права, которых нет у вас самих")
        if (member.role == "owner" and role.builtin != roles.OWNER
                and roles.owner_count(db, ctx.org.id) <= 1):
            raise HTTPException(
                status_code=422,
                detail="В организации должен остаться хотя бы один владелец. "
                       "Сначала назначьте владельцем другого сотрудника.",
            )
        roles.apply_role(member, role)
        db.commit()
        return team_state(db, ctx)


@router.post("/api/team/members/{user_id}/remove")
def api_team_member_remove(
    user_id: int = _id_path(),
    ctx: AuthContext = Depends(_TEAM), db: Session = Depends(get_db),
):
    # Ревью Codex #110 (P1): проверка «последний владелец» и изменение
    # выполняются под одним замком, иначе два одновременных запроса оба
    # видят двух владельцев и оставляют организацию без хозяина.
    with _OWNER_LOCK:
        member = _org_member(db, ctx.org.id, user_id)
        if user_id == ctx.user.id:
            raise HTTPException(
                status_code=422,
                detail="Себя убрать нельзя. Уйти из организации можно на странице «Аккаунт».",
            )
        if member.role == "owner":
            _require_owner_for_owner_role(ctx)
            if roles.owner_count(db, ctx.org.id) <= 1:
                raise HTTPException(status_code=422,
                                    detail="Нельзя убрать последнего владельца организации")
        current, _name = roles.member_perms(db, member)
        _forbid_unless_subset(ctx, current, "Нельзя убрать сотрудника, у которого прав больше, чем у вас")
        db.delete(member)
        db.commit()
        return team_state(db, ctx)


class RoleIn(BaseModel):
    name: str = Field(default="", max_length=200)
    perms: list[str] = Field(default_factory=list, max_length=100)


class RoleUpdateIn(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    perms: list[str] | None = Field(default=None, max_length=100)


@router.post("/api/team/roles")
def api_team_role_create(body: RoleIn, ctx: AuthContext = Depends(_TEAM),
                         db: Session = Depends(get_db)):
    roles.ensure_builtin_roles(db, ctx.org.id)
    perms = _clean_perms(body.perms)
    _forbid_unless_subset(ctx, perms, "Нельзя выдать права, которых нет у вас самих")
    total = db.execute(select(func.count()).select_from(OrgRole)
                       .where(OrgRole.org_id == ctx.org.id)).scalar() or 0
    if total >= MAX_ROLES:
        raise HTTPException(status_code=422, detail=f"Ролей не больше {MAX_ROLES}")
    name = _clean_name(db, ctx.org.id, body.name)
    role = OrgRole(org_id=ctx.org.id, name=name, builtin="",
                   perms_json=roles.dump_perms(perms), created_at=datetime.utcnow())
    db.add(role)
    db.commit()
    state = team_state(db, ctx)
    state["created_role_id"] = role.id
    return state


@router.post("/api/team/roles/{role_id}")
def api_team_role_update(body: RoleUpdateIn, role_id: int = _id_path(),
                         ctx: AuthContext = Depends(_TEAM), db: Session = Depends(get_db)):
    role = _org_role(db, ctx.org.id, role_id)
    if role.builtin:
        raise HTTPException(
            status_code=422,
            detail="Встроенную роль изменить нельзя. Создайте свою роль с нужными правами.",
        )
    _forbid_unless_subset(ctx, roles.role_perms(role),
                          "Нельзя менять роль, у которой прав больше, чем у вас")
    if body.perms is not None:
        perms = _clean_perms(body.perms)
        _forbid_unless_subset(ctx, perms, "Нельзя выдать права, которых нет у вас самих")
        role.perms_json = roles.dump_perms(perms)
    if body.name is not None:
        role.name = _clean_name(db, ctx.org.id, body.name, skip_id=role.id)
    db.commit()
    return team_state(db, ctx)


@router.post("/api/team/roles/{role_id}/delete")
def api_team_role_delete(role_id: int = _id_path(),
                         ctx: AuthContext = Depends(_TEAM), db: Session = Depends(get_db)):
    role = _org_role(db, ctx.org.id, role_id)
    if role.builtin:
        raise HTTPException(status_code=422, detail="Встроенную роль удалить нельзя")
    _forbid_unless_subset(ctx, roles.role_perms(role),
                          "Нельзя удалить роль, у которой прав больше, чем у вас")
    on_role = db.execute(
        select(func.count()).select_from(Membership).where(
            Membership.org_id == ctx.org.id, Membership.role_id == role.id,
            Membership.role != "owner")
    ).scalar() or 0
    if on_role:
        raise HTTPException(
            status_code=422,
            detail=f"Роль назначена сотрудникам ({on_role}). "
                   "Сначала назначьте им другую роль, потом удалите эту.",
        )
    now = datetime.utcnow()
    db.execute(
        update(OrgInvite).where(
            OrgInvite.org_id == ctx.org.id, OrgInvite.role_id == role.id,
            OrgInvite.used_at.is_(None), OrgInvite.revoked_at.is_(None),
        ).values(revoked_at=now).execution_options(synchronize_session=False)
    )
    db.delete(role)
    db.commit()
    return team_state(db, ctx)


class InviteIn(BaseModel):
    role_id: int = Field(ge=1, le=2_147_483_647)


@router.post("/api/team/invites")
def api_team_invite_create(body: InviteIn, ctx: AuthContext = Depends(_TEAM),
                           db: Session = Depends(get_db)):
    """Новая ссылка-приглашение. Токен в ответе один раз, в базе только хеш."""
    roles.ensure_builtin_roles(db, ctx.org.id)
    role = _org_role(db, ctx.org.id, body.role_id)
    if role.builtin == roles.OWNER:
        _require_owner_for_owner_role(ctx)
    _forbid_unless_subset(ctx, roles.role_perms(role), "Нельзя выдать права, которых нет у вас самих")
    now = datetime.utcnow()
    if len(_active_invites(db, ctx.org.id, now)) >= MAX_ACTIVE_INVITES:
        raise HTTPException(
            status_code=422,
            detail=f"Активных приглашений не больше {MAX_ACTIVE_INVITES}. Отзовите ненужные.",
        )
    token = secrets.token_urlsafe(32)
    inv = OrgInvite(org_id=ctx.org.id, role_id=role.id, token_hash=token_hash(token),
                    created_by=ctx.user.id, created_at=now, expires_at=now + INVITE_TTL)
    db.add(inv)
    db.commit()
    response = JSONResponse({
        "ok": True, "id": inv.id, "role_name": role.name,
        "expires_at": inv.expires_at.isoformat(),
        # Показывается один раз: в базе токена нет, повторно его не получить.
        "token": token,
        "link_path": f"/invite#t={token}",
    })
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/api/team/invites/{invite_id}/revoke")
def api_team_invite_revoke(invite_id: int = _id_path(),
                           ctx: AuthContext = Depends(_TEAM), db: Session = Depends(get_db)):
    inv = db.get(OrgInvite, invite_id)
    if inv is None or inv.org_id != ctx.org.id:
        raise HTTPException(status_code=404, detail="Приглашение не найдено")
    role = db.get(OrgRole, inv.role_id)
    if role is not None and role.builtin == roles.OWNER:
        _require_owner_for_owner_role(ctx)
    if inv.used_at is not None:
        raise HTTPException(status_code=409, detail="Приглашение уже использовано")
    if inv.revoked_at is None:
        inv.revoked_at = datetime.utcnow()
        db.commit()
    return team_state(db, ctx)


# ── Вход по приглашению (без сессии или с ней) ───────────────────────────────

class InviteTokenIn(BaseModel):
    token: str = Field(default="", max_length=128)


class InviteAcceptIn(BaseModel):
    token: str = Field(default="", max_length=128)
    # join: уже вошёл и присоединяюсь; login: есть аккаунт; register: новый.
    mode: str = Field(default="", max_length=16)
    name: str = Field(default="", max_length=255)
    email: str = Field(default="", max_length=255)
    password: str = Field(default="", max_length=512)


def _limit_ip(request: Request) -> tuple[str, bool]:
    ip, trusted = auth.client_ip(request)
    key = f"ip:{ip}"
    if trusted and invite_ip_limiter.retry_after(key):
        raise HTTPException(
            status_code=429,
            detail="Слишком много попыток с этого адреса. Попробуйте через несколько минут.",
        )
    return key, trusted


def _valid_invite(db: Session, token: str, ip_key: str, trusted: bool,
                  now: datetime) -> tuple[OrgInvite, OrgRole, Org]:
    inv = None
    if token:
        inv = db.execute(
            select(OrgInvite).where(OrgInvite.token_hash == token_hash(token))
        ).scalars().first()
    role = db.get(OrgRole, inv.role_id) if inv else None
    org = db.get(Org, inv.org_id) if inv else None
    if (inv is None or inv.used_at is not None or inv.revoked_at is not None
            or inv.expires_at <= now or role is None or role.org_id != inv.org_id
            or org is None):
        if trusted:
            invite_ip_limiter.hit(ip_key)
        raise HTTPException(status_code=400, detail=INVITE_INVALID)
    return inv, role, org


@router.post("/api/invite/info")
def api_invite_info(body: InviteTokenIn, request: Request, db: Session = Depends(get_db)):
    """Куда зовут: название организации и роль. Ничего не меняет."""
    ip_key, trusted = _limit_ip(request)
    inv, role, org = _valid_invite(db, body.token, ip_key, trusted, datetime.utcnow())
    ctx = auth.resolve_auth(request, db)
    already = bool(ctx and db.get(Membership, (ctx.user.id, org.id)))
    response = JSONResponse({
        "org_name": org.name, "role_name": role.name,
        "expires_at": inv.expires_at.isoformat(),
        "signed_in": ({"name": ctx.user.name, "email": ctx.user.email} if ctx else None),
        "already_member": already,
    })
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/api/invite/accept")
def api_invite_accept(body: InviteAcceptIn, request: Request, db: Session = Depends(get_db)):
    """Принять приглашение: стать участником организации с ролью из ссылки.

    Порядок: сначала все проверки (ссылка, пароль, e-mail), и только потом
    ссылка гасится условным UPDATE, первым пишущим оператором транзакции
    (как consume_reset в D-63): из двух одновременных запросов проходит один,
    второй получает «недействительно». Отказ на любой проверке ссылку не
    тратит: человек, ошибившийся в пароле, может попробовать снова.
    """
    ip_key, trusted = _limit_ip(request)
    now = datetime.utcnow()
    inv, role, org = _valid_invite(db, body.token, ip_key, trusted, now)

    user: User | None = None
    pw_hash = ""
    email_norm = (body.email or "").strip().lower()
    if body.mode == "join":
        ctx = auth.resolve_auth(request, db)
        if ctx is None:
            raise HTTPException(status_code=401,
                                detail="Войдите в свой аккаунт или зарегистрируйтесь")
        user = ctx.user
    elif body.mode == "login":
        acc_key = f"acc:{email_norm}"
        acc_retry = auth.login_limiter.retry_after(acc_key)
        user = db.execute(select(User).where(User.email == email_norm)).scalars().first()
        if user is None or not auth.verify_password(body.password, user.pw_hash):
            auth.login_limiter.hit(acc_key)
            if acc_retry:
                raise HTTPException(
                    status_code=429,
                    detail="Слишком много неудачных попыток входа в этот аккаунт. "
                           "Попробуйте через несколько минут.",
                )
            raise HTTPException(status_code=403, detail="Неверный e-mail или пароль")
        auth.login_limiter.reset(acc_key)
    elif body.mode == "register":
        if not email_norm or "@" not in email_norm:
            raise HTTPException(status_code=422, detail="Укажите корректный e-mail")
        if len(body.password) < 8:
            raise HTTPException(status_code=422, detail="Пароль: минимум 8 символов")
        if len(body.password.encode("utf-8")) > 72:
            raise HTTPException(
                status_code=422,
                detail="Пароль слишком длинный. Лимит 72 байта (это примерно 36 русских "
                       "букв или 72 латинских), сократите фразу.",
            )
        if db.execute(select(User.id).where(User.email == email_norm)).first():
            raise HTTPException(
                status_code=422,
                detail="Такой e-mail уже зарегистрирован. Выберите «У меня уже есть аккаунт».",
            )
        pw_hash = auth.hash_password(body.password)
    else:
        raise HTTPException(status_code=422, detail="Неизвестный способ входа")

    if user is not None and db.get(Membership, (user.id, org.id)) is not None:
        raise HTTPException(status_code=409,
                            detail=f"Вы уже участник организации «{org.name}»")

    # Состояние прочитано: закрываем транзакцию чтения, чтобы условный UPDATE
    # стал первым оператором новой (WAL SQLite, см. consume_reset).
    db.rollback()
    try:
        taken = db.execute(
            update(OrgInvite).where(
                OrgInvite.id == inv.id, OrgInvite.used_at.is_(None),
                OrgInvite.revoked_at.is_(None), OrgInvite.expires_at > now,
            ).values(used_at=now).execution_options(synchronize_session=False)
        ).rowcount
        if taken != 1:
            db.rollback()
            raise HTTPException(status_code=400, detail=INVITE_INVALID)
        if body.mode == "register":
            user = User(
                email=email_norm, pw_hash=pw_hash, name=(body.name or "").strip(),
                session_version=auth.new_session_version_seed(),
            )
            db.add(user)
            db.flush()
        else:
            user = db.get(User, user.id)
        member = Membership(user_id=user.id, org_id=inv.org_id, role="member")
        roles.apply_role(member, db.get(OrgRole, inv.role_id))
        db.add(member)
        db.commit()
    except HTTPException:
        raise
    except IntegrityError:
        # Тот же e-mail зарегистрировали параллельно: ссылка не потрачена.
        db.rollback()
        raise HTTPException(status_code=422, detail="Такой e-mail уже зарегистрирован") from None
    except Exception:
        db.rollback()
        raise
    response = JSONResponse({"ok": True, "redirect": "/", "org_name": org.name,
                             "role_name": role.name})
    response.headers["Cache-Control"] = "no-store"
    auth.set_session(response, user.id, inv.org_id, user.session_version)
    return response
