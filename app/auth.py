"""Аутентификация: bcrypt-пароли, подписанные cookie-сессии, FastAPI-зависимости.

Кука: HttpOnly, SameSite=Lax, подписана itsdangerous (URLSafeTimedSerializer).
Внутри — {user_id, org_id}; org_id определяет тенант для всех запросов.
"""
import time
from collections import defaultdict, deque
from dataclasses import dataclass

import bcrypt
from fastapi import Depends, HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from app.crypto import get_signing_secret, is_prod
from app.db import get_db
from app.models import Membership, Org, User

SESSION_COOKIE = "oborot_session"
SESSION_MAX_AGE = 7 * 24 * 3600  # 7 дней

# Лёгкий признак «работаем внутри iframe МойСклад»: ставится вместе с сессией
# при входе через /ms/app. Не несёт секретов (значение "1") — только переключает
# оболочку шаблона на встроенную. HttpOnly и SameSite согласованы с основной
# кукой (в iframe на проде — None+Secure, см. set_session).
EMBED_COOKIE = "oborot_embed"


# ── Rate-limit логина (защита от перебора паролей) ────────────────────────────

class LoginLimiter:
    """Скользящее окно попыток входа по ключу (ip и ip+email).

    In-memory: при нескольких воркерах лимит действует per-process — для прода
    с >1 воркером вынести в Redis (см. SECURITY-бэклог).
    """

    def __init__(self, max_attempts: int = 10, window_sec: int = 300):
        self.max_attempts = max_attempts
        self.window_sec = window_sec
        self._hits: dict[str, deque] = defaultdict(deque)

    def check(self, key: str) -> bool:
        """True — можно пробовать; False — лимит исчерпан."""
        now = time.monotonic()
        q = self._hits[key]
        while q and now - q[0] > self.window_sec:
            q.popleft()
        return len(q) < self.max_attempts

    def hit(self, key: str) -> None:
        self._hits[key].append(time.monotonic())

    def reset(self, key: str) -> None:
        self._hits.pop(key, None)


login_limiter = LoginLimiter()


# ── Пароли ────────────────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    """bcrypt-хеш пароля (соль внутри хеша)."""
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, pw_hash: str) -> bool:
    """Проверка пароля против bcrypt-хеша."""
    try:
        return bcrypt.checkpw(password.encode(), pw_hash.encode())
    except ValueError:
        return False


# ── Сессии ────────────────────────────────────────────────────────────────────

def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_signing_secret(), salt="oborot-session")


def set_session(response, user_id: int, org_id: int, samesite: str = "lax") -> None:
    """Ставит подписанную сессионную куку на ответ.

    samesite: обычный вход — "lax" (дефолт). Вход из iframe МойСклад
    (routes_ms_app) на проде передаёт "none": третьесторонняя кука во фрейме
    требует SameSite=None + Secure. В dev (http) None+Secure браузер отбросил
    бы — остаётся "lax" (iframe-вход в dev работает только same-site).
    """
    value = _serializer().dumps({"user_id": user_id, "org_id": org_id})
    response.set_cookie(
        SESSION_COOKIE,
        value,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite=samesite,
        # на проде кука только по https; SameSite=None без Secure невалидна
        secure=is_prod() or samesite == "none",
        path="/",
    )


def clear_session(response) -> None:
    """Снимает сессионную куку."""
    response.delete_cookie(SESSION_COOKIE, path="/")


def set_embed(response, samesite: str = "lax") -> None:
    """Ставит признак встроенного режима (iframe МойСклад) на ответ.

    Аддитивно к set_session: /ms/app зовёт обе. samesite согласуется с сессией
    (в iframe на проде — "none" + Secure, чтобы кука долетела в третьесторонний
    фрейм; в dev — "lax"). read_embed читает флаг обратно.
    """
    response.set_cookie(
        EMBED_COOKIE,
        "1",
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite=samesite,
        secure=is_prod() or samesite == "none",
        path="/",
    )


def clear_embed(response) -> None:
    """Снимает признак встроенного режима (возврат к полной оболочке)."""
    response.delete_cookie(EMBED_COOKIE, path="/")


def read_embed(request: Request) -> bool:
    """True, если пользователь работает внутри iframe МойСклад (кука oborot_embed)."""
    return request.cookies.get(EMBED_COOKIE) == "1"


def read_session(request: Request) -> dict | None:
    """Читает и валидирует сессию из куки; None, если её нет или подпись битая."""
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    try:
        return _serializer().loads(raw, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None


# ── Зависимости ───────────────────────────────────────────────────────────────

@dataclass
class AuthContext:
    """Аутентифицированный пользователь, его организация и роль в ней."""

    user: User
    org: Org
    role: str = "member"


def resolve_auth(request: Request, db: Session) -> AuthContext | None:
    """Восстанавливает пользователя и организацию из сессии (с проверкой членства)."""
    sess = read_session(request)
    if not sess:
        return None
    user = db.get(User, sess.get("user_id"))
    if not user:
        return None
    org_id = sess.get("org_id")
    member = db.get(Membership, (user.id, org_id)) if org_id else None
    if not member:
        return None
    org = db.get(Org, org_id)
    if not org:
        return None
    return AuthContext(user=user, org=org, role=member.role or "member")


def require_auth_api(request: Request, db: Session = Depends(get_db)) -> AuthContext:
    """Зависимость для JSON API: 401 без валидной сессии."""
    ctx = resolve_auth(request, db)
    if ctx is None:
        raise HTTPException(status_code=401, detail="Не авторизован")
    return ctx


def require_owner_api(ctx: AuthContext = Depends(require_auth_api)) -> AuthContext:
    """Зависимость для чувствительных ручек (настройки, подключения): только owner."""
    if ctx.role != "owner":
        raise HTTPException(status_code=403, detail="Доступно только владельцу организации")
    return ctx
