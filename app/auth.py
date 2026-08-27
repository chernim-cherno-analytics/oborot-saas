"""Аутентификация: bcrypt-пароли, подписанные cookie-сессии, FastAPI-зависимости.

Кука: HttpOnly, SameSite=Lax, подписана itsdangerous (URLSafeTimedSerializer).
Внутри — {user_id, org_id}; org_id определяет тенант для всех запросов.
"""
import hmac
import logging
import os
import secrets
import time
from collections import deque
from dataclasses import dataclass

import bcrypt
from fastapi import Depends, HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from app.crypto import get_csrf_signing_secret, get_signing_secret, is_prod
from app.db import get_db
from app.models import Membership, Org, User

log = logging.getLogger("oborot.auth")

SESSION_COOKIE = "oborot_session"
SESSION_MAX_AGE = 7 * 24 * 3600  # 7 дней

# Лёгкий признак «работаем внутри iframe МойСклад»: ставится вместе с сессией
# при входе через /ms/app. Не несёт секретов (значение "1") — только переключает
# оболочку шаблона на встроенную. HttpOnly и SameSite согласованы с основной
# кукой (в iframe на проде — None+Secure, см. set_session).
EMBED_COOKIE = "oborot_embed"


# ── Адрес клиента (за прокси и без него) ─────────────────────────────────────
#
# ВНИМАНИЕ ТОМУ, КТО РАЗВОРАЧИВАЕТ СЕРВИС.
# Лимит по IP имеет смысл, только если IP настоящий. `request.client.host`
# доверять нельзя: uvicorn по умолчанию (`--proxy-headers`) подменяет его
# значением из заголовка X-Forwarded-For, когда запрос пришёл с доверенного
# адреса (по умолчанию 127.0.0.1 — то есть с локального nginx). Отсюда две
# противоположные беды:
#   • приложение открыто/проксируется с localhost → любой клиент подставляет
#     свой X-Forwarded-For и получает новый ключ лимита на каждую попытку;
#   • заголовок никто не разбирает → у всех клиентов один и тот же адрес
#     (адрес прокси), и лимит по IP закрывает вход сразу всему сервису.
# Поэтому адрес мы считаем сами, а конфигурация задаётся явно:
#
#   OBOROT_TRUSTED_PROXY_HOPS  — сколько СВОИХ прокси стоит перед приложением.
#     не задан / 0 — прокси нет: адрес берём как есть, X-Forwarded-For не
#                    доверяем (и, если он всё же пришёл, лимит по IP
#                    выключается — см. client_ip);
#     1            — один свой обратный прокси (обычный случай: nginx на том же
#                    сервере, `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;`);
#     2            — прокси + CDN/балансировщик перед ним. И так далее.
#
# Команда запуска на проде при OBOROT_TRUSTED_PROXY_HOPS=1:
#   uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-proxy-headers
# (`--no-proxy-headers` — чтобы uvicorn не подменял client.host; разбор
# заголовка целиком наш, см. client_ip). Если прокси нет вообще, ключ можно не
# задавать, но `--no-proxy-headers` всё равно поставьте.

TRUSTED_PROXY_HOPS_ENV = "OBOROT_TRUSTED_PROXY_HOPS"
_proxy_warned = False

# Разумный потолок числа хопов: свой прокси + CDN/балансировщик перед ним —
# редко больше двух-трёх звеньев. Больше этого — почти наверняка не топология
# сети, а опечатка (перепутали с портом, ID и т.п.), и её тоже надо ловить
# fail-fast, а не тихо получить лимит по IP, который никогда не сработает.
_MAX_TRUSTED_PROXY_HOPS = 10


def _parse_trusted_proxy_hops(raw: str) -> int | None:
    """Строгий разбор значения переменной: целое число в [0, потолок] или None.

    В отличие от `trusted_proxy_hops()` (та для рантайма — на любой мусор
    молча отвечает 0, безопасным выключением лимита), здесь мусор ДОЛЖЕН
    быть отличим от валидного нуля, иначе `check_proxy_config` не сможет
    отказать в старте на пустое значение или "abc" — вернётся то же самое,
    что и на "0".
    """
    try:
        value = int(raw.strip())
    except ValueError:
        return None
    if value < 0 or value > _MAX_TRUSTED_PROXY_HOPS:
        return None
    return value


def trusted_proxy_hops() -> int:
    """Сколько последних адресов в X-Forwarded-For проставили НАШИ прокси."""
    raw = os.getenv(TRUSTED_PROXY_HOPS_ENV, "0")
    parsed = _parse_trusted_proxy_hops(raw)
    return parsed if parsed is not None else 0


def check_proxy_config() -> None:
    """Fail-fast на старте: в проде переменная должна быть задана ЯВНО и по существу.

    Если её не задать, `client_ip` (правильно и безопасно) считает адрес
    ненадёжным и тихо выключает лимит по IP — а на проде за nginx на этом же
    хосте заголовок X-Forwarded-For приходит всегда, то есть эта защита
    молча отключается именно тогда, когда должна работать. Раньше (до этого
    разбора заголовка) uvicorn сам подставлял настоящий IP — значит
    "забыли настроить" здесь не «как было», а регресс.
    Тихого дефолта в любую сторону нет и не будет: доверять непроверенному
    прокси — открыть подмену адреса, не доверять никому — при реальном
    прокси свалить всех клиентов в один адрес (см. блок выше). Поэтому
    выбор явный: либо поставить хопы (1 — прокси на этом же сервере,
    2 — прокси + CDN/балансировщик и т.д.), либо явно подтвердить, что
    прокси нет вообще (=0). "Забыли" здесь не проходит — сервис просто не
    стартует, а не тихо теряет часть защиты.

    Важно: пустая строка и любой нечисловой мусор ("", "abc", "-1", "99")
    ловятся так же, как отсутствие переменной вовсе. Раньше проверялся только
    факт наличия (`is None`) — деплойщик мог выставить переменную с пустым
    или кривым значением на панели хостинга (частый случай на Timeweb) и
    получить тот же молчаливый провал лимита по IP, от которого fail-fast
    должен защищать. Значение должно быть проверено по существу.
    """
    if not is_prod():
        return
    raw = os.environ.get(TRUSTED_PROXY_HOPS_ENV)
    if raw is not None and _parse_trusted_proxy_hops(raw) is not None:
        return
    what = "не задан" if raw is None else f"задан некорректно ({raw!r})"
    raise RuntimeError(
        f"{TRUSTED_PROXY_HOPS_ENV} {what} при OBOROT_ENV=prod — запуск запрещён. "
        f"Нужно целое число от 0 до {_MAX_TRUSTED_PROXY_HOPS} без пробелов вокруг: "
        f"0 — приложение доступно напрямую, без прокси; "
        f"1 — один обратный прокси на этом же сервере (типичный случай — nginx); "
        f"2 — прокси + CDN/балансировщик перед ним; и так далее. "
        f"Поставьте {TRUSTED_PROXY_HOPS_ENV}=1 (или другое подходящее число) "
        f"на панели хостинга — пустое значение не считается настройкой. "
        f"Подробности — в комментарии над этой функцией и в app/auth.py:33-59."
    )


def client_ip(request: Request) -> tuple[str, bool]:
    """Адрес клиента и признак «этому адресу можно верить».

    Считаем сами, а не полагаемся на request.client.host: за прокси он либо
    подменяем заголовком, либо одинаков у всех (см. блок выше).

    Правило: цепочку X-Forwarded-For пишут слева направо, каждый прокси
    дописывает адрес СВОЕГО клиента в конец. Значит настоящий адрес — N-й
    справа, где N = число наших прокси; всё, что левее, прислал сам клиент и
    подделать может как угодно.

    Второй элемент — False, если адрес ненадёжен (за прокси, но
    OBOROT_TRUSTED_PROXY_HOPS не задан или цепочка короче, чем ожидалось).
    По ненадёжному адресу лимит не считаем вообще: считать по нему — значит
    либо ловить подделку, либо блокировать всех клиентов разом. Защиту от
    подбора пароля это не ослабляет: она держится на ключе по аккаунту,
    который от адреса не зависит.
    """
    global _proxy_warned
    peer = (request.client.host if request.client else "") or "?"
    raw = request.headers.get("x-forwarded-for") or ""
    chain = [p.strip() for p in raw.split(",") if p.strip()]
    hops = trusted_proxy_hops()
    if hops:
        if len(chain) >= hops:
            return chain[-hops], True
        # Цепочка короче настроенной: запрос пришёл мимо прокси или конфигурация
        # разъехалась. Гадать нельзя — считаем адрес ненадёжным.
        return peer, not chain
    if chain:
        if not _proxy_warned:
            _proxy_warned = True
            log.warning(
                "Запрос с заголовком X-Forwarded-For, но %s не задан: лимит входа "
                "по IP отключён (адресу клиента верить нельзя). Задайте "
                "%s=1 и запускайте uvicorn с --no-proxy-headers.",
                TRUSTED_PROXY_HOPS_ENV, TRUSTED_PROXY_HOPS_ENV,
            )
        return peer, False
    return peer, True


# ── Rate-limit логина (защита от перебора паролей) ────────────────────────────

class LoginLimiter:
    """Окно попыток входа по ключу (аккаунт и IP) с ФИКСИРОВАННОЙ блокировкой.

    In-memory: при нескольких воркерах лимит действует per-process — для прода
    с >1 воркером вынести в Redis (см. SECURITY-бэклог).

    До порога (`max_attempts`) попытки копятся в скользящем окне, как обычно.
    Но как только счётчик достигает порога, ключ ЗАПИРАЕТСЯ на `window_sec` от
    момента блокировки, а счётчик сбрасывается. Пока блокировка активна,
    дальнейшие попытки (`hit`) её НЕ продлевают и не копятся — они просто
    игнорируются до истечения срока. Это принципиально: со скользящим окном
    без фиксации подбиратель одной попыткой раз в несколько минут постоянно
    подсовывал бы новый "самый свежий" элемент и отодвигал разблокировку —
    блокировку можно было держать вечно ценой одного запроса. С фиксацией
    срок известен заранее и не зависит от того, сколько ещё попыток придёт
    следом: человек видит именно его и может просто дождаться (см.
    main._too_many_attempts_error). После истечения блокировки счётчик пуст:
    чтобы запереть ключ снова, нужны ещё `max_attempts` новых неудач с нуля.

    Память ограничена по построению. Запись (и в скользящем окне, и в
    блокировке) живёт не дольше окна: `_trim`/`_is_locked` выбрасывают ключ,
    как только он протух, а `_maybe_sweep` (по росту словарей вдвое и не реже
    раза в окно) проходит по обоим словарям и убирает протухшее, до чего
    никто больше не постучится. Верхняя граница — «попыток в секунду × окно»,
    а не «попыток за всё время»: при 100 попытках/с и окне 300 с это ~30 000
    ключей (несколько МБ), и после окна память освобождается.

    Живые записи не вытесняются НИКОГДА — ни по размеру, ни по LRU. Это
    принципиально: если бы переполнение словаря выбрасывало самые старые
    записи, подбиратель одним потоком мусорных ключей вымывал бы собственный
    счётчик и обнулял лимит. Поэтому чистка удаляет только то, что уже никого
    не ограничивает (окно истекло / блокировка истекла).
    """

    _SWEEP_MIN = 1024  # реже этого числа ключей (суммарно по обоим словарям) уборку не запускаем

    def __init__(self, max_attempts: int = 5, window_sec: int = 300):
        self.max_attempts = max_attempts
        self.window_sec = window_sec
        self._hits: dict[str, deque] = {}
        self._locked_until: dict[str, float] = {}
        self._sweep_at = self._SWEEP_MIN
        self._last_sweep = time.monotonic()

    def _trim(self, key: str) -> deque:
        """Отбрасывает попытки старше окна; пустой ключ удаляет из словаря."""
        q = self._hits.get(key)
        if q is None:
            return deque()
        now = time.monotonic()
        while q and now - q[0] > self.window_sec:
            q.popleft()
        if not q:
            del self._hits[key]
        return q

    def _is_locked(self, key: str) -> int:
        """0, если ключ не заперт; иначе — сколько секунд ждать (не меньше 1)."""
        until = self._locked_until.get(key)
        if until is None:
            return 0
        left = until - time.monotonic()
        if left <= 0:
            del self._locked_until[key]
            return 0
        return max(1, int(left) + 1)

    def _maybe_sweep(self) -> None:
        """Уборка протухшего — по суммарному размеру словарей или раз в окно."""
        now = time.monotonic()
        total = len(self._hits) + len(self._locked_until)
        if total < self._sweep_at and now - self._last_sweep <= self.window_sec:
            return
        for key, q in list(self._hits.items()):
            if not q or now - q[-1] > self.window_sec:
                self._hits.pop(key, None)
        for key, until in list(self._locked_until.items()):
            if until <= now:
                self._locked_until.pop(key, None)
        self._last_sweep = now
        # Следующая уборка по размеру — когда суммарный размер снова вырастет
        # вдвое: расход амортизированно постоянный, а живые записи не трогаются.
        self._sweep_at = max(self._SWEEP_MIN, (len(self._hits) + len(self._locked_until)) * 2)

    def check(self, key: str) -> bool:
        """True — можно пробовать; False — лимит исчерпан (заперт)."""
        if self._is_locked(key):
            return False
        return len(self._trim(key)) < self.max_attempts

    def hit(self, key: str) -> None:
        """Считает неудачную попытку.

        Пока ключ заперт — попытка не учитывается вовсе: не продлевает и не
        обновляет блокировку (см. докстринг класса). Как только счётчик
        внутри окна достигает порога — запирает ключ на `window_sec` от
        текущего момента и сбрасывает скользящий счётчик.
        """
        if self._is_locked(key):
            return
        q = self._trim(key)
        if not q:
            self._maybe_sweep()
            q = self._hits[key] = deque()
        q.append(time.monotonic())
        if len(q) >= self.max_attempts:
            self._locked_until[key] = time.monotonic() + self.window_sec
            self._hits.pop(key, None)

    def count(self, key: str) -> int:
        """Число попыток внутри текущего скользящего окна (0, если заперт)."""
        if self._is_locked(key):
            return 0
        return len(self._trim(key))

    def retry_after(self, key: str) -> int:
        """Через сколько секунд снова можно пробовать (0 — можно прямо сейчас).

        Срок ФИКСИРОВАН в момент блокировки (см. докстринг класса) — не
        пересчитывается заново на каждый вызов и не сдвигается чужими
        попытками, только убывает по факту прошедшего времени.
        """
        self._maybe_sweep()
        return self._is_locked(key)

    def reset(self, key: str) -> None:
        self._hits.pop(key, None)
        self._locked_until.pop(key, None)


# Окно короткое специально. Длинное окно (15 минут) мучает человека, который
# забыл пароль — тем более что восстановления пароля в продукте пока нет, — а
# подбору мешает ровно так же: важна не длина окна, а разрешённый темп. 5
# попыток за 5 минут — это 60 попыток в час на аккаунт, для перебора пароля
# бессмысленно, а честному человеку ждать 5 минут, а не 15.
LOGIN_WINDOW_SEC = 300

# Ключ — только нормализованный e-mail, БЕЗ адреса: подбор пароля к аккаунту
# должен блокироваться независимо от того, с какого адреса (или скольких)
# он идёт. Ключ вида "ip+email" защищал пару, а не аккаунт: распределённый
# перебор одного аккаунта не ограничивал вообще.
#
# Блокировка ограничивает СКОРОСТЬ перебора, а не запирает владельца
# аккаунта: верный пароль пропускает всегда, даже пока лимит исчерпан (см.
# main.login_submit) — иначе один вредный запрос раз в несколько минут на
# ЧУЖОЙ известный e-mail держал бы платящего клиента заблокированным
# бессрочно, а восстановления пароля в продукте нет. Это не ослабляет защиту
# от подбора: угадать пароль отсюда не легче, лимит по-прежнему не даёт
# перебирать чаще одного захода в окно, а «мягкость» никак не подсказывает
# подбирающему, близко ли он подошёл к правильному паролю — единственный
# видимый ему сигнал успеха — собственно вход.
login_limiter = LoginLimiter(max_attempts=5, window_sec=LOGIN_WINDOW_SEC)

# Отдельный, более широкий лимит по IP (защита от перебора e-mail'ов и чистого
# флуда с одного адреса). Специально НЕ равен login_limiter: если бы порог
# совпадал, 5 неудач по ОДНОМУ аккаунту с общего IP (офис, NAT) заодно
# блокировали бы вход всем остальным пользователям за тем же адресом. Считается
# только по адресу, которому можно верить (см. client_ip), и применяется мягко:
# останавливает неудачные попытки, но человека с верным паролем пропускает
# всегда (см. main.login_submit) — лимит по IP не должен запирать сервис.
ip_login_limiter = LoginLimiter(max_attempts=20, window_sec=LOGIN_WINDOW_SEC)


# ── Пароли ────────────────────────────────────────────────────────────────────

class PasswordTooLongError(ValueError):
    """Пароль длиннее 72 байт — физический предел bcrypt."""


def hash_password(password: str) -> str:
    """bcrypt-хеш пароля (соль внутри хеша).

    bcrypt не может захешировать пароль длиннее 72 байт. Специально НЕ
    обрезаем его молча до 72 байт — это тихо ослабило бы пароль (два разных
    пароля с одинаковым 72-байтовым началом стали бы неразличимы). Вместо
    этого поднимаем понятную ошибку — вызывающий код обязан проверить длину
    заранее и показать пользователю человеческое сообщение (см.
    app/main.py:register_submit).
    """
    raw = password.encode("utf-8")
    if len(raw) > 72:
        raise PasswordTooLongError("Пароль длиннее 72 байт — bcrypt не может его захешировать")
    return bcrypt.hashpw(raw, bcrypt.gensalt()).decode()


def verify_password(password: str, pw_hash: str) -> bool:
    """Проверка пароля против bcrypt-хеша."""
    try:
        return bcrypt.checkpw(password.encode(), pw_hash.encode())
    except ValueError:
        return False


# ── Сессии ────────────────────────────────────────────────────────────────────

def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_signing_secret(), salt="oborot-session")


def set_session(response, user_id: int, org_id: int, session_version: int, samesite: str = "lax") -> None:
    """Ставит подписанную сессионную куку на ответ.

    session_version — версия сессии пользователя НА МОМЕНТ ВЫДАЧИ (SEC-3),
    обязательный параметр без дефолта: молчаливый дефолт здесь — источник
    самого дефекта, который эта версия закрывает (перевыпуск куки со старой
    версией после смены пароля отозвал бы собственную свежую сессию).
    Все call site'ы обязаны передавать актуальное `user.session_version`.
    resolve_auth сравнивает её с текущей записью пользователя и отзывает
    куку, чья версия отстала (смена пароля увеличивает её на 1).

    samesite: обычный вход — "lax" (дефолт). Вход из iframe МойСклад
    (routes_ms_app) на проде передаёт "none": третьесторонняя кука во фрейме
    требует SameSite=None + Secure. В dev (http) None+Secure браузер отбросил
    бы — остаётся "lax" (iframe-вход в dev работает только same-site).
    """
    value = _serializer().dumps({"user_id": user_id, "org_id": org_id, "v": session_version})
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


# ── CSRF форм вне /api (SEC-5) ────────────────────────────────────────────────
#
# /login, /register, /logout — обычные HTML-формы, а не fetch() из app.js:
# браузер отправляет их сам, без JS и без кастомных заголовков, поэтому
# заголовок X-Oborot-CSRF (см. main._security_headers_and_csrf) им не защита.
# Схема — подписанный double-submit: сервер выдаёт куку со случайным
# значением и то же значение кладёт в скрытое поле формы. Кука HttpOnly
# (сравнение целиком серверное, JS её никогда не читает), сравнение —
# hmac.compare_digest, подпись — отдельный от сессии ключ (см.
# get_csrf_signing_secret). Сторонняя форма не может ни прочитать куку
# (чужой origin), ни поставить её сама с валидной подписью (не знает ключ) —
# значит не может подобрать значение, совпадающее со скрытым полем.
CSRF_COOKIE = "oborot_csrf"
CSRF_FORM_FIELD = "csrf_token"
CSRF_MAX_AGE = 6 * 3600  # 6 часов
# Пути, где сессия ставится/снимается формой, а не /api — им нужна эта, а не
# заголовочная защита.
CSRF_FORM_PATHS = frozenset({"/login", "/register", "/logout"})


def _csrf_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_csrf_signing_secret(), salt="oborot-csrf")


def get_csrf_token(request: Request) -> str:
    """Значение для скрытого поля формы.

    Переиспользует уже выставленную куку, если она ещё валидна — токен НЕ
    перевыпускается на каждый рендер. Иначе несколько открытых вкладок и
    кнопка «назад» держали бы в разметке устаревшее скрытое поле, пока кука
    (общая на браузер) уже сменилась на новую, и легитимная отправка стала
    бы падать наравне с поддельной.
    """
    raw = request.cookies.get(CSRF_COOKIE)
    if raw:
        try:
            _csrf_serializer().loads(raw, max_age=CSRF_MAX_AGE)
            return raw
        except (BadSignature, SignatureExpired):
            pass
    return _csrf_serializer().dumps(secrets.token_urlsafe(32))


def set_csrf_cookie(response, token: str, samesite: str = "lax") -> None:
    """Ставит/продлевает CSRF-куку на ответ, где показана форма.

    samesite согласован с сессионной кукой (см. set_session): встроенный
    режим на проде — "none" + Secure, иначе кука не долетит до формы logout
    внутри iframe МойСклад и double-submit станет невыполним для настоящего
    пользователя, а не только для атакующего.
    """
    response.set_cookie(
        CSRF_COOKIE,
        token,
        max_age=CSRF_MAX_AGE,
        httponly=True,
        samesite=samesite,
        secure=is_prod() or samesite == "none",
        path="/",
    )


def verify_csrf_form(request: Request, form_token) -> bool:
    """True, если скрытое поле формы совпадает с подписанной CSRF-кукой.

    Дважды: значения должны совпасть БУКВАЛЬНО (constant-time compare — не
    течёт тайминг совпадения символов) И кука должна быть подписана нашим
    ключом и не просрочена. Только совпадения недостаточно: без подписи
    сторонний, кто как-то смог поставить куку с тем же значением, что и
    угаданное/подсмотренное поле, тоже прошёл бы.
    """
    if not isinstance(form_token, str) or not form_token:
        return False
    cookie_val = request.cookies.get(CSRF_COOKIE)
    if not cookie_val:
        return False
    if not hmac.compare_digest(cookie_val, form_token):
        return False
    try:
        _csrf_serializer().loads(cookie_val, max_age=CSRF_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return False
    return True


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
    # SEC-3: гашение сессий после смены пароля. Кука без поля версии — это
    # довыпущенный формат (до этой правки): трактуем как версию 0, чтобы сам
    # деплой миграции никого не разлогинил (у старых строк users тоже 0 — см.
    # models._ensure_users_session_version). Любое НЕЦЕЛОЕ значение
    # (строка, дробь, bool — bool в Python является подклассом int, поэтому
    # исключён явно через type()) — fail-closed отказ, а не «как ноль»: это
    # не легитимный формат ни старой, ни новой куки.
    cookie_version = sess.get("v", 0)
    if type(cookie_version) is not int or cookie_version != user.session_version:
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
