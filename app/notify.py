"""Telegram-уведомления: ежедневный дайджест и алерты по данным аналитики.

Бот один на весь сервис: токен — env OBOROT_TG_BOT_TOKEN, публичное имя для
инструкции в настройках — env OBOROT_TG_BOT_NAME. Организация хранит свой
chat_id и флаги в таблице notify_settings (см. models.NotifySettings).

Отправка — прямыми вызовами Telegram Bot API через httpx (без SDK, как в
legacy/sync.py _notify). База API переопределяется env TG_API_BASE — так
тесты подменяют Telegram локальным mock-сервером.

send_daily_digest(org_id) молча пропускает организацию, если токен бота не
задан, chat_id пуст или уведомления выключены — планировщик зовёт её для
всех org без предварительных проверок.
"""
import html
import logging
import os
from datetime import date, timedelta

import httpx
from sqlalchemy import case, func, select

from app import analytics
from app.db import SessionLocal
from app.models import NotifySettings, Org, Sale

log = logging.getLogger("oborot.notify")

TG_LIMIT = 4096  # жёсткий лимит Telegram на длину text в sendMessage
MAX_ALERTS_PER_KIND = 8  # больше в дайджест не кладём — остальное «и ещё N»


def bot_token() -> str:
    return os.environ.get("OBOROT_TG_BOT_TOKEN", "").strip()


def bot_name() -> str:
    """Публичное имя бота для инструкции (без @)."""
    return os.environ.get("OBOROT_TG_BOT_NAME", "").strip().lstrip("@")


def _api_base() -> str:
    return os.environ.get("TG_API_BASE", "https://api.telegram.org").rstrip("/")


# ── Низкоуровневая отправка ──────────────────────────────────────────────────

def send_message(chat_id: str, text: str) -> tuple[bool, str]:
    """Шлёт одно сообщение в Telegram. Возвращает (ok, человекочитаемая ошибка)."""
    token = bot_token()
    if not token:
        return False, "Telegram-бот не настроен на сервере (нет OBOROT_TG_BOT_TOKEN)."
    if not chat_id:
        return False, "Не указан chat_id."
    try:
        resp = httpx.post(
            f"{_api_base()}/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text[:TG_LIMIT],
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15.0,
        )
    except httpx.HTTPError as exc:
        return False, f"Не удалось связаться с Telegram: {exc.__class__.__name__}."
    if resp.status_code == 200:
        return True, ""
    return False, _human_tg_error(resp)


def _human_tg_error(resp: httpx.Response) -> str:
    """Ответ Telegram об ошибке → текст, понятный владельцу организации."""
    description = ""
    try:
        description = str(resp.json().get("description") or "")
    except ValueError:
        pass
    low = description.lower()
    if "chat not found" in low:
        return ("Telegram: чат не найден. Проверьте chat_id и убедитесь, "
                "что вы отправили боту /start.")
    if "bot was blocked" in low:
        return "Telegram: бот заблокирован в этом чате. Разблокируйте его и повторите."
    if resp.status_code == 401:
        return "Telegram не принял токен бота — проверьте OBOROT_TG_BOT_TOKEN на сервере."
    if resp.status_code == 429:
        return "Telegram ограничил частоту отправки. Попробуйте через минуту."
    return f"Telegram ответил ошибкой {resp.status_code}: {description or 'без описания'}."


# ── Настройки ────────────────────────────────────────────────────────────────

def get_settings(db, org_id: int) -> NotifySettings:
    """Строка notify_settings организации; создаёт дефолтную, если её ещё нет."""
    row = db.get(NotifySettings, org_id)
    if row is None:
        row = NotifySettings(org_id=org_id)
        db.add(row)
        db.commit()
    return row


# ── Дайджест ─────────────────────────────────────────────────────────────────

def _fmt_money(value: float) -> str:
    """1234567 → '1 234 567 ₽' (неразрывные пробелы тысяч)."""
    return f"{round(value):,}".replace(",", " ") + " ₽"


def _sold_yesterday(db, org_id: int) -> tuple[int, float] | None:
    """Нетто-продажи за вчера (шт, ₽); None — строк за вчера в БД нет."""
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    sign_qty = case((Sale.is_return, -Sale.qty), else_=Sale.qty)
    sign_rev = case((Sale.is_return, -Sale.revenue), else_=Sale.revenue)
    qty, rev, n_rows = db.execute(
        select(
            func.coalesce(func.sum(sign_qty), 0),
            func.coalesce(func.sum(sign_rev), 0),
            func.count(),
        ).where(Sale.org_id == org_id, Sale.date == yesterday)
    ).one()
    if not n_rows:
        return None
    return round(float(qty)), float(rev)


def _alert_block(title: str, alerts: list[dict]) -> list[str]:
    if not alerts:
        return []
    lines = [f"<b>{title}</b>"]
    for a in alerts[:MAX_ALERTS_PER_KIND]:
        lines.append("• " + html.escape(a["text"]))
    hidden = len(alerts) - MAX_ALERTS_PER_KIND
    if hidden > 0:
        lines.append(f"…и ещё {hidden} — см. «Показатели» в Обороте")
    lines.append("")
    return lines


def build_digest_text(org: Org, summary: dict, sold_yesterday: tuple[int, float] | None,
                      settings: NotifySettings) -> str:
    """Текст ежедневного дайджеста (HTML parse_mode, ≤4096 символов)."""
    red = [a for a in summary["alerts"] if a["severity"] == "red"]
    yellow = [a for a in summary["alerts"] if a["severity"] == "yellow"]

    lines: list[str] = [f"<b>Оборот · {html.escape(org.name)}</b>",
                        f"Дайджест за {date.today().isoformat()}", ""]
    if sold_yesterday is not None:
        qty, rev = sold_yesterday
        lines.append(f"Продано вчера: <b>{_fmt_money(rev)}</b> · {qty} шт")
    lines.append(
        f"Товара на складе: <b>{_fmt_money(summary['stock_value_retail'])}</b> "
        f"по рознице · {summary['stock_units']} шт"
    )
    lines.append(f"Продано за 30 дней: {_fmt_money(summary['sold_30d_rev'])} "
                 f"· {summary['sold_30d_qty']} шт")
    lines.append("")

    if settings.alerts_stockout:
        lines += _alert_block("🔴 Скоро закончатся (пора заказывать)", red)
    if settings.alerts_overstock:
        lines += _alert_block("🟡 Неликвид и затоварка", yellow)
    if not red and not yellow:
        lines += ["✅ Критичных алертов нет.", ""]

    lines.append("Подробности: раздел «Показатели» в Обороте.")
    text = "\n".join(lines).strip()
    if len(text) > TG_LIMIT:
        text = text[: TG_LIMIT - 1] + "…"
    return text


def send_daily_digest(org_id: int) -> bool:
    """Собирает и шлёт дайджест организации. False — пропущено или ошибка.

    Молча скипает, если бот-токен не задан, уведомления выключены или chat_id
    пуст (так планировщик может звать её для каждой организации подряд).
    """
    if not bot_token():
        return False
    db = SessionLocal()
    try:
        org = db.get(Org, org_id)
        if org is None:
            return False
        settings = db.get(NotifySettings, org_id)
        if (settings is None or not settings.tg_enabled
                or not settings.digest_enabled or not settings.tg_chat_id.strip()):
            return False
        snap = analytics.get_snapshot(db, org)
        summary = analytics.build_summary(snap)
        sold = _sold_yesterday(db, org_id)
        text = build_digest_text(org, summary, sold, settings)
        chat_id = settings.tg_chat_id.strip()
    finally:
        db.close()

    ok, err = send_message(chat_id, text)
    if not ok:
        log.warning("digest org=%s не отправлен: %s", org_id, err)
    return ok
