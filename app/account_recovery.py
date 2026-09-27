"""Восстановление доступа по заранее подтверждённым контактам (D-63).

Политика владельца, а не выбор кода: при сопровождаемом подключении оператор
подтверждает и записывает телефон и Telegram человека. При восстановлении он
перезванивает ТОЛЬКО на записанный телефон, а одноразовую ссылку отправляет
ТОЛЬКО в записанный Telegram. Нет обоих подтверждённых контактов — сброса нет.
Имени пользователя, e-mail или знания счёта для этого недостаточно никогда.

Что здесь сделано ради безопасности, и почему:

* **Токен не хранится.** В базе только SHA-256 от 32 случайных байт; утечка
  базы не даёт ни одной рабочей ссылки.
* **Токен не ходит в адресе запроса.** Ссылка — `/reset#t=…`: фрагмент браузер
  серверу не отправляет вовсе, поэтому его нет ни в журнале доступа, ни в
  прокси, ни в `Referer`. Страница забирает его из фрагмента, сразу стирает из
  адресной строки и отправляет только в теле POST.
* **Одноразовость атомарна.** Условный UPDATE «ещё не использован, не отозван,
  не истёк» и смена пароля — одна транзакция; из двух одновременных попыток
  проходит ровно одна, а сбой на смене пароля откатывает и отметку об
  использовании.
* **Все сессии отзываются.** Та же атомарная прибавка `session_version`, что и
  при смене пароля (SEC-3). Новую сессию сброс не выдаёт — входит человек сам.
* **Ответ на плохую ссылку один.** Неизвестная, истёкшая, использованная и
  отозванная ссылки неразличимы снаружи.
* **Журнал без секретов.** `account_events` пишет, кто, когда и с каким
  исходом; контакты — только в маске, токена и хеша там нет.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import AccountEvent, PasswordReset, RecoveryContact, User

log = logging.getLogger("oborot.recovery")

RESET_TTL = timedelta(minutes=30)
CONTACT_KINDS = ("phone", "telegram")
MIN_NOTE_LEN = 10

GENERIC_INVALID = (
    "Ссылка недействительна или устарела. Попросите новую у поддержки — "
    "её пришлют в ваш подтверждённый Telegram после звонка на ваш номер."
)


class RecoveryRefused(ValueError):
    """Отказ оператору. Текст — для терминала оператора, не для публичной страницы."""


# ── Контакты ─────────────────────────────────────────────────────────────────

_TG_RE = re.compile(r"^@[a-z0-9_]{5,32}$")


def normalize_contact(kind: str, value: str) -> str:
    """Приводит контакт к одному виду; непохожее на контакт — отказ."""
    raw = (value or "").strip()
    if kind == "phone":
        digits = re.sub(r"\D", "", raw)
        if not 10 <= len(digits) <= 15:
            raise RecoveryRefused("Телефон: нужно от 10 до 15 цифр")
        return "+" + digits
    if kind == "telegram":
        handle = raw.lower()
        if not handle.startswith("@"):
            handle = "@" + handle
        if not _TG_RE.match(handle):
            raise RecoveryRefused("Telegram: имя вида @name, 5–32 символа a-z, 0-9, _")
        return handle
    raise RecoveryRefused(f"Неизвестный вид контакта: {kind!r} (нужен phone или telegram)")


def mask(kind: str, value: str) -> str:
    """Маска для журнала и списков: узнать можно, восстановить нельзя."""
    if kind == "phone":
        return value[:2] + "*" * max(0, len(value) - 6) + value[-4:]
    if kind == "telegram":
        return value[:3] + "***"
    return "***"


def _audit(db: Session, user_id: int | None, event: str, actor: str, **detail) -> None:
    db.add(AccountEvent(user_id=user_id, kind=event, at=datetime.utcnow(),
                        actor=actor[:64], detail_json=json.dumps(detail, ensure_ascii=False)))


def _require_note(label: str, text: str) -> str:
    note = (text or "").strip()
    if len(note) < MIN_NOTE_LEN:
        raise RecoveryRefused(f"{label}: опишите, как именно проверено (от {MIN_NOTE_LEN} символов)")
    return note


def _user_by_email(db: Session, email: str) -> User:
    user = db.execute(
        select(User).where(User.email == (email or "").strip().lower())
    ).scalars().first()
    if user is None:
        raise RecoveryRefused("Пользователя с таким e-mail нет")
    if user.ms_uid:
        # Вход через МойСклад без пароля: сбрасывать здесь нечего.
        raise RecoveryRefused("Пользователь входит через МойСклад — пароль у него не используется")
    return user


def set_contact(db: Session, *, email: str, kind: str, value: str, operator: str,
                method: str, reverify: bool = False) -> RecoveryContact:
    """Записывает подтверждённый контакт. Замена — только с явной перепроверкой.

    Замену нельзя делать по деталям из запроса на восстановление: человек,
    потерявший доступ, и тот, кто выдаёт себя за него, пишут одинаково. Поэтому
    уже записанный контакт меняется только флагом `reverify` и с описанием
    новой проверки, а журнал хранит маски старого и нового значения.
    """
    if kind not in CONTACT_KINDS:
        raise RecoveryRefused(f"Неизвестный вид контакта: {kind!r}")
    operator = (operator or "").strip()
    if not operator:
        raise RecoveryRefused("Нужно имя оператора (--operator)")
    note = _require_note("Способ подтверждения", method)
    user = _user_by_email(db, email)
    norm = normalize_contact(kind, value)
    now = datetime.utcnow()
    row = db.execute(
        select(RecoveryContact).where(RecoveryContact.user_id == user.id,
                                      RecoveryContact.kind == kind)
    ).scalars().first()
    if row is not None and not reverify:
        raise RecoveryRefused(
            f"Контакт {kind} уже подтверждён ({mask(kind, row.value)}). Замена — "
            "только после новой проверки, флагом --reverify")
    if row is None:
        row = RecoveryContact(user_id=user.id, kind=kind, value=norm, verified_at=now,
                              verified_by=operator, verified_method=note)
        db.add(row)
        _audit(db, user.id, "contact_set", operator, kind=kind, contact=mask(kind, norm),
               method=note)
    else:
        old = row.value
        row.value, row.verified_at, row.verified_by, row.verified_method = norm, now, operator, note
        _audit(db, user.id, "contact_reverified", operator, kind=kind,
               old=mask(kind, old), new=mask(kind, norm), method=note)
    db.commit()
    return row


def callback_phone(db: Session, *, email: str, operator: str, reason: str) -> RecoveryContact:
    """Полный записанный телефон — чтобы оператор мог ПЕРЕЗВОНИТЬ по нему.

    Маска в списках защищает номер от случайного взгляда, но правило D-63
    требует звонка именно на записанный номер, а не на тот, с которого пришла
    просьба. Без этой команды оператор без собственной записной книжки шаг
    «перезвонить» выполнить не мог (ревью PR #66, issuecomment-5855362557).

    Только для оператора на сервере; публичной ручки нет. Каждый просмотр
    пишется в журнал — кто, когда и зачем, — но САМ номер в журнал не попадает
    даже в маске: журнал отвечает на вопрос «кто смотрел», а не хранит копию.
    """
    operator = (operator or "").strip()
    if not operator:
        raise RecoveryRefused("Нужно имя оператора (--operator)")
    note = _require_note("Причина просмотра", reason)
    user = _user_by_email(db, email)
    row = db.execute(
        select(RecoveryContact).where(RecoveryContact.user_id == user.id,
                                      RecoveryContact.kind == "phone")
    ).scalars().first()
    if row is None:
        raise RecoveryRefused("Подтверждённого телефона нет — перезванивать некуда, "
                              "сброс не выдаётся (D-63)")
    _audit(db, user.id, "callback_phone_viewed", operator, reason=note)
    db.commit()
    return row


def contacts(db: Session, email: str) -> list[RecoveryContact]:
    user = _user_by_email(db, email)
    return list(db.execute(
        select(RecoveryContact).where(RecoveryContact.user_id == user.id)
        .order_by(RecoveryContact.kind)
    ).scalars())


# ── Выпуск ссылки ────────────────────────────────────────────────────────────

def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def reset_link(base_url: str, token: str) -> str:
    """Ссылка с токеном во ФРАГМЕНТЕ — серверу он не отправляется никогда."""
    return f"{base_url.rstrip('/')}/reset#t={token}"


def issue_reset(db: Session, *, email: str, operator: str, callback_note: str,
                now: datetime | None = None) -> tuple[str, str]:
    """Выпускает одноразовую ссылку. Возвращает (токен, записанный Telegram).

    Требует ОБА подтверждённых контакта и явное подтверждение обратного звонка
    на записанный телефон. Прежние открытые ссылки этого человека отзываются.
    Токен возвращается один раз — ни в базе, ни в журнале его нет.
    """
    operator = (operator or "").strip()
    if not operator:
        raise RecoveryRefused("Нужно имя оператора (--operator)")
    note = _require_note("Обратный звонок", callback_note)
    user = _user_by_email(db, email)
    have = {c.kind: c for c in db.execute(
        select(RecoveryContact).where(RecoveryContact.user_id == user.id)
    ).scalars()}
    missing = [k for k in CONTACT_KINDS if k not in have]
    if missing:
        raise RecoveryRefused(
            "Нет подтверждённых контактов: " + ", ".join(missing)
            + ". Без обоих сброс не выдаётся (D-63) — сначала сопровождаемая проверка")
    now = now or datetime.utcnow()
    revoked = db.execute(
        update(PasswordReset)
        .where(PasswordReset.user_id == user.id, PasswordReset.used_at.is_(None),
               PasswordReset.revoked_at.is_(None))
        .values(revoked_at=now)
        .execution_options(synchronize_session=False)
    ).rowcount
    token = secrets.token_urlsafe(32)
    db.add(PasswordReset(user_id=user.id, token_hash=token_hash(token), created_at=now,
                         expires_at=now + RESET_TTL, issued_by=operator))
    tg = have["telegram"].value
    _audit(db, user.id, "reset_issued", operator,
           phone=mask("phone", have["phone"].value), telegram=mask("telegram", tg),
           callback=note, revoked_previous=revoked, ttl_min=int(RESET_TTL.total_seconds() // 60))
    db.commit()
    log.info("выпущена ссылка сброса: user=%s оператор=%s", user.id, operator)
    return token, tg


# ── Использование ссылки ─────────────────────────────────────────────────────

def password_problem(new_password: str, confirm: str) -> str | None:
    """Те же правила, что при регистрации и смене пароля."""
    if new_password != confirm:
        return "Пароль и подтверждение не совпадают — введите их заново"
    if len(new_password) < 8:
        return "Пароль — минимум 8 символов"
    if len(new_password.encode("utf-8")) > 72:
        return ("Пароль слишком длинный. Лимит — 72 байта (это примерно 36 русских букв "
                "или 72 латинских) — сократите фразу.")
    return None


def _apply_password(db: Session, user_id: int, pw_hash: str) -> None:
    """Новый пароль и отзыв всех сессий одним UPDATE (SEC-3). Внутри транзакции."""
    db.execute(
        update(User).where(User.id == user_id)
        .values(pw_hash=pw_hash, session_version=User.session_version + 1)
        .execution_options(synchronize_session=False)
    )


def consume_reset(db: Session, token: str, pw_hash: str,
                  now: datetime | None = None) -> bool:
    """Гасит ссылку и меняет пароль атомарно. False — ссылка не годится.

    Хеш пароля считается ДО вызова (bcrypt медленный): транзакция держит
    блокировку записи только на два UPDATE. Любое исключение внутри
    откатывает всё — и отметку об использовании, и пароль.
    """
    now = now or datetime.utcnow()
    th = token_hash(token or "")
    try:
        # Условный UPDATE — ПЕРВЫЙ оператор транзакции, без предварительного
        # SELECT. В WAL-режиме SQLite транзакция, начавшаяся с чтения, при
        # попытке записи после чужого коммита падает сразу («database is
        # locked»), минуя busy_timeout: проигравший из двух одновременных
        # запросов получал бы 500 вместо честного «ссылка недействительна».
        # Начав с записи, транзакция штатно ждёт соседа и затем видит used_at.
        taken = db.execute(
            update(PasswordReset)
            .where(PasswordReset.token_hash == th, PasswordReset.used_at.is_(None),
                   PasswordReset.revoked_at.is_(None), PasswordReset.expires_at > now)
            .values(used_at=now)
            .execution_options(synchronize_session=False)
        ).rowcount
        if taken != 1:
            db.rollback()
            uid = db.execute(
                select(PasswordReset.user_id).where(PasswordReset.token_hash == th)
            ).scalar()
            _audit(db, uid, "reset_rejected", "public",
                   reason="unknown" if uid is None else "used_expired_or_revoked")
            db.commit()
            return False
        uid = db.execute(
            select(PasswordReset.user_id).where(PasswordReset.token_hash == th)
        ).scalar_one()
        _apply_password(db, uid, pw_hash)
        _audit(db, uid, "reset_consumed", "public")
        db.commit()
    except Exception:
        db.rollback()
        raise
    log.info("пароль сброшен по ссылке: user=%s, все сессии отозваны", uid)
    return True


def events(db: Session, email: str) -> list[AccountEvent]:
    user = _user_by_email(db, email)
    return list(db.execute(
        select(AccountEvent).where(AccountEvent.user_id == user.id).order_by(AccountEvent.id)
    ).scalars())
