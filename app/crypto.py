"""Секреты приложения и шифрование токенов интеграций.

Правила безопасности (по итогам security-ревью):
- OBOROT_SECRET обязателен в проде (OBOROT_ENV=prod): при отсутствии или
  совпадении с дев-дефолтом приложение падает на старте (fail-fast).
- Из одного секрета выводятся ДВА независимых ключа (domain separation):
  ключ подписи сессий и ключ Fernet для токенов — компрометация одного
  использования не раскрывает другое напрямую.
"""
import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken

_DEV_DEFAULT_SECRET = "oborot-dev-secret-change-me"


def is_prod() -> bool:
    return os.environ.get("OBOROT_ENV", "dev").lower() in ("prod", "production")


def _base_secret() -> str:
    secret = os.environ.get("OBOROT_SECRET", _DEV_DEFAULT_SECRET)
    if is_prod() and (not secret or secret == _DEV_DEFAULT_SECRET):
        raise RuntimeError(
            "OBOROT_SECRET не задан (или равен дев-дефолту) при OBOROT_ENV=prod — "
            "запуск запрещён. Задайте длинный случайный секрет в окружении."
        )
    return secret


def _derive(purpose: str) -> bytes:
    """32 байта, детерминированно выведенные из секрета для конкретной цели."""
    return hashlib.sha256(f"{_base_secret()}|{purpose}".encode()).digest()


def get_signing_secret() -> str:
    """Ключ подписи сессионных кук (itsdangerous)."""
    return _derive("session-signing").hex()


def get_csrf_signing_secret() -> str:
    """Ключ подписи double-submit CSRF-токена форм /login, /register, /logout.

    Отдельный от сессионного: подпись CSRF-куки не должна быть переносима на
    сессионную куку и обратно (тот же принцип domain separation, что и у
    Fernet-ключа интеграций выше).
    """
    return _derive("csrf-signing").hex()


def _fernet() -> Fernet:
    key = base64.urlsafe_b64encode(_derive("integration-tokens"))
    return Fernet(key)


def encrypt_token(token: str) -> str:
    """Шифрует токен интеграции для хранения в БД."""
    return _fernet().encrypt(token.encode()).decode()


def decrypt_token(token_enc: str) -> str | None:
    """Расшифровывает токен; None, если шифртекст повреждён или ключ сменился."""
    try:
        return _fernet().decrypt(token_enc.encode()).decode()
    except (InvalidToken, ValueError):
        return None
