"""Гейт подписки: одно состояние организации — active | grace | readonly.

Зачем отдельный модуль. До сих пор оплата в «Обороте» существовала только как
заявка на счёт (billing_requests) и текст на странице тарифов: истёкший триал
ничего не закрывал, платить было не обязательно. Этот модуль вводит ровно одно
доменное состояние подписки и одно место, где оно вычисляется, — чтобы «кому
можно писать» не расползлось по роутам разными формулами (ровно так уже
разъехались need/lead time/MOQ, см. BUSINESS_LOGIC §9).

Состояния (D-24):
  active   — всё работает;
  grace    — счёт выставлен, деньги ещё не пришли: пишем как обычно, но
             интерфейс вправе показать предупреждение;
  readonly — писать нельзя, читать можно.

Что закрывает readonly (в порядке ценности, а не «всё подряд»):
  1) синхронизация с МойСклад,
  2) запись в МойСклад (заказ поставщику),
  3) расчёт и сохранение планов заказа.
Чтение, экспорт, страница тарифов и заявка на счёт НЕ закрываются никогда:
клиент с истёкшей подпиской должен видеть свои данные и иметь возможность
заплатить. Это правило проверяется тестом (tests/test_subscription.py).

Выключен по умолчанию. Включается переменной окружения
OBOROT_SUBSCRIPTION_GATE=1. Причина осторожности прозаическая: у собственной
организации владельца на проде триал истёк 13 августа, и включённый гейт
закрыл бы доступ ему же. Порядок ввода: сначала выставить paid_until живым
организациям, потом включать флаг.
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta

from fastapi import Depends, HTTPException, Request
from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session

from app.auth import AuthContext, require_auth_api, resolve_auth
from app.db import engine, get_db, run_migration_step

log = logging.getLogger("oborot.subscription")

# Сколько календарных дней клиент работает после отметки «счёт выставлен».
# Календарных, а не рабочих: решение владельца, 5 дней (D-24).
GRACE_DAYS = 5

GATE_ENV = "OBOROT_SUBSCRIPTION_GATE"

# Сколько id показывать в строке предпросмотра (см. log_preview).
LOG_IDS_LIMIT = 20

ACTIVE = "active"
GRACE = "grace"
READONLY = "readonly"

_TRUE = {"1", "true", "yes", "on"}


def gate_enabled() -> bool:
    """Включён ли гейт. Читается на каждом вызове — тесты меняют окружение."""
    return (os.getenv(GATE_ENV) or "").strip().lower() in _TRUE


# ── Аддитивная мини-миграция ─────────────────────────────────────────────────

def ensure_schema(bind=None) -> None:
    """orgs.paid_until и billing_requests.invoiced_at.

    Обе колонки аддитивные и nullable — старые записи остаются валидными,
    откат кода не ломает базу. Вызывается из app.main._startup вместе с
    остальными миграциями (не на импорте: несколько воркеров стартуют разом).
    """
    eng = bind or engine
    insp = inspect(eng)
    if insp.has_table("orgs"):
        cols = {c["name"] for c in insp.get_columns("orgs")}
        if "paid_until" not in cols:
            run_migration_step("ALTER TABLE orgs ADD COLUMN paid_until DATE", bind=eng)
    if insp.has_table("billing_requests"):
        cols = {c["name"] for c in insp.get_columns("billing_requests")}
        if "invoiced_at" not in cols:
            run_migration_step(
                "ALTER TABLE billing_requests ADD COLUMN invoiced_at DATETIME", bind=eng,
            )


# ── Вычисление состояния ─────────────────────────────────────────────────────

def _as_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:  # SQLite отдаёт строку, если колонка добавлена ALTER-ом
        return datetime.fromisoformat(str(value)[:19]).date()
    except ValueError:
        return None


def _grace_until(
    db: Session, org_id: int, today: date, *, stamp: bool = False
) -> date | None:
    """Дата конца грейса по последней отметке «счёт выставлен».

    Грейс отсчитывается от момента, когда счёт выставили МЫ, а не от даты
    заявки клиента: заявка — это намерение, а обязательство возникает после
    выставленного счёта.

    Тонкость эксплуатации: статус заявки сейчас меняется вручную (UPDATE в
    базе), и такой UPDATE не проставит invoiced_at. Поэтому при первой ПОПЫТКЕ
    ЗАПИСИ мы ставим отметку сами — «первое наблюдение». Ошибка тут возможна
    только в сторону клиента (грейс начнётся позже фактического счёта, то есть
    дольше), и это сознательно: гейт не должен закрывать доступ из-за нашей же
    забывчивости.

    `stamp` по умолчанию ВЫКЛЮЧЕН, и это важно. Раньше отметка ставилась при
    любом чтении, и функция, объявленная диагностической, писала в боевую базу:
    предпросмотр на старте проставлял invoiced_at всем таким организациям
    временем деплоя, а GET /api/subscription делал UPDATE+COMMIT — то есть
    обычный просмотр страницы запускал отсчёт пяти дней, и он же падал 500,
    если база в этот момент занята синком или открыта только на чтение.
    Теперь пишет ровно одно место — проверка права на запись, где транзакция
    и так ожидается. Читающие пути при NULL считают грейс от сегодняшнего дня
    БЕЗ записи: они показывают то же состояние, которое клиент получит при
    первой же попытке что-то сделать.
    """
    insp = inspect(db.get_bind())
    if not insp.has_table("billing_requests"):
        return None
    cols = {c["name"] for c in insp.get_columns("billing_requests")}
    if "invoiced_at" not in cols:
        return None
    rows = db.execute(
        text(
            "SELECT id, invoiced_at FROM billing_requests "
            # lower(trim(...)): статус правит оператор руками, и «Paid» или
            # « paid » не должны означать «нет отметки».
            "WHERE org_id = :org AND lower(trim(status)) IN ('invoiced', 'paid') "
            "ORDER BY id DESC LIMIT 1"
        ),
        {"org": org_id},
    ).fetchall()
    if not rows:
        return None
    row_id, marked_raw = rows[0]
    marked = _as_date(marked_raw)
    if marked is None:
        if stamp:
            db.execute(
                text("UPDATE billing_requests SET invoiced_at = :ts WHERE id = :id"),
                {"ts": datetime.utcnow(), "id": row_id},
            )
            db.commit()
        marked = today
    return marked + timedelta(days=GRACE_DAYS)


def _marked_paid_without_date(db: Session, org_id: int) -> bool:
    """У организации есть отметка «деньги пришли», но срок не проставлен.

    Это ДИАГНОСТИКА, а не право доступа. Решение владельца от 23.08.2026:
    оплаченный срок определяется ТОЛЬКО `orgs.paid_until`; статус заявки
    `paid` сам по себе не выдаёт ни дня.

    Почему это правильнее моей прежней версии. Я делал так: отметка `paid`
    давала «купленный срок» — месяц или год от даты счёта. Выглядело как
    страховка от закрытия плательщика, а на деле это выдуманный период:
    система назначала доступ по числу, которого никто не вводил. Ошибку
    оператора надо показывать оператору, а не превращать в тихое право
    доступа — иначе «сколько оплачено» перестаёт быть фактом и становится
    следствием нашей догадки.

    Поэтому здесь остаётся ровно одно: сказать вслух, что отметка есть, а
    срока нет. Дальше это идёт в лог и в предпросмотр гейта, а доступ
    считается по `paid_until`.
    """
    insp = inspect(db.get_bind())
    if not insp.has_table("billing_requests"):
        return False
    row = db.execute(
        text(
            "SELECT 1 FROM billing_requests "
            "WHERE org_id = :org AND lower(trim(status)) = 'paid' LIMIT 1"
        ),
        {"org": org_id},
    ).fetchone()
    return bool(row)


def _today() -> date:
    """«Сегодня» в UTC.

    Отметка о счёте пишется datetime.utcnow(), и инструкция в deploy/README
    предлагает sqlite3 datetime('now') — тоже UTC. Сравнивать это с локальным
    date.today() значило бы, что счёт, выставленный ночью, штампуется вчерашним
    числом и грейс выходит на день короче, а состояние организации меняется
    без единого внешнего события. Одна шкала на запись и на сравнение.
    """
    return datetime.utcnow().date()


def subscription_state(org, db: Session, *, stamp: bool = False) -> str:
    """active | grace | readonly. Единственное место, где это решается."""
    today = _today()

    # Организации из каталога МойСклад платят внутри МС: их состояние —
    # то, что прислал МС (Activate/Suspend/Uninstall кладут status).
    # Своих счетов мы им не выставляем, поэтому грейса у них нет.
    if (getattr(org, "source", "saas") or "saas") == "ms_app":
        return ACTIVE if (getattr(org, "status", "active") or "active") == "active" else READONLY

    if (getattr(org, "status", "active") or "active") != "active":
        return READONLY

    paid_until = _as_date(getattr(org, "paid_until", None))
    if paid_until is not None and paid_until >= today:
        return ACTIVE

    # Дата конца триала — НАША запись, клиент её не задаёт, подделать не может.
    # Поэтому она уважается независимо от названия тарифа: организация, успевшая
    # переключить `plan` на платный, но ещё не оплатившая, доживает свой триал,
    # а не выключается на день раньше. Асимметрия ошибок здесь однозначна:
    # пропустить неплательщика — потерять месяц денег, закрыть плательщика —
    # потерять клиента (D-24, «название тарифа доказательством не является» —
    # доказательство оплаты это paid_until, а не plan).
    trial_ends = _as_date(getattr(org, "trial_ends_at", None))
    if trial_ends is not None and trial_ends >= today:
        return ACTIVE

    grace_until = _grace_until(db, org.id, today, stamp=stamp)
    if grace_until is not None and grace_until >= today:
        return GRACE

    # Отметка «деньги пришли» без проставленного срока — это НЕ доступ, а
    # незакрытая работа оператора. Говорим об этом громко и закрываем: право
    # доступа даёт только `paid_until` (решение владельца 23.08.2026).
    if _marked_paid_without_date(db, org.id):
        log.warning(
            "организация %s помечена оплаченной (billing_requests.status='paid'), "
            "но orgs.paid_until не проставлен — доступ ЗАКРЫТ, потому что срок "
            "оплаты определяется только paid_until. Проставьте: "
            "UPDATE orgs SET paid_until='ГГГГ-ММ-ДД' WHERE id=%s", org.id, org.id,
        )

    return READONLY


def state_info(org, db: Session) -> dict:
    """Состояние + даты для интерфейса и /api/subscription."""
    state = subscription_state(org, db)
    paid_until = _as_date(getattr(org, "paid_until", None))
    trial_ends = _as_date(getattr(org, "trial_ends_at", None))
    grace_until = _grace_until(db, org.id, _today()) if state == GRACE else None
    return {
        "state": state,
        "gate_enabled": gate_enabled(),
        "writes_blocked": gate_enabled() and state == READONLY,
        "paid_until": paid_until.isoformat() if paid_until else None,
        "trial_ends_at": trial_ends.isoformat() if trial_ends else None,
        "grace_until": grace_until.isoformat() if grace_until else None,
        "source": getattr(org, "source", "saas"),
    }


def can_sync(org, db: Session) -> bool:
    """Пускать ли организацию в плановую (фоновую) синхронизацию."""
    if not gate_enabled():
        return True
    return subscription_state(org, db) != READONLY


BLOCK_MESSAGE = (
    "Доступ к записи приостановлен: подписка не оплачена. "
    "Данные и отчёты открыты, синхронизация и заказы возобновятся после оплаты. "
    "Выставить счёт — на странице «Тарифы»."
)


# ── Зависимость FastAPI ──────────────────────────────────────────────────────

# ── Что остаётся открытым в readonly ─────────────────────────────────────────
#
# Список ЗАКРЫТЫХ ручек не ведётся: закрыто всё, что меняет данные. Открыт
# только этот перечень, и у каждого пути назван смысл. Так новая пишущая ручка
# по умолчанию оказывается ЗАКРЫТОЙ — а не открытой, как было в первой версии,
# где перечислялись закрытые и любая забытая проскакивала мимо гейта молча.
#
# Смысл списка ровно один: человек, которому приостановили запись, обязан
# уметь (а) видеть свои данные и выгружать их, (б) заплатить, (в) войти,
# выйти и распорядиться аккаунтом. Всё остальное — «активные действия»,
# которые решение D-24 останавливает до оплаты.
# Открытый список. СТРОГИЙ: решение владельца 23.08.2026 перечисляет, что
# именно остаётся доступным в readonly, и всё остальное закрыто. Здесь ровно
# этот перечень, и каждый пункт объяснён — «любое исключение должно иметь явную
# причину и тест».
#
# Что отсюда УБРАНО по этому решению: отметки подсказок и прогресса обучения
# (`/api/hints/seen`, `/api/prefs/hints`, `/api/lessons/*`). Я оставлял их,
# рассуждая, что это состояние экрана одного человека, а не данные
# организации. Довод остаётся верным, но перечень владельца их не содержит, а
# «строгий режим» на то и строгий: список исключений не растёт от здравых
# доводов исполнителя. Последствие названо и лечится на фронте — страница
# просто не шлёт эти запросы, когда состояние readonly (см. _mobile/_hints).
ALWAYS_OPEN_PATHS = frozenset({
    # Путь оплаты: единственный способ выйти из readonly.
    "/api/plans/request",
    # Вход, выход, управление собственным аккаунтом.
    "/login", "/register", "/logout",
    "/api/account/password", "/api/account/delete",
    # Выгрузка своих данных. По природе это ЧТЕНИЕ; POST здесь только потому,
    # что список позиций не помещается в строку запроса (см. ReplenishExportIn).
    # Отношу к «чтению» из перечня владельца — и помечаю как решение, а не как
    # самоочевидность, чтобы при следующем пересмотре его было видно.
    "/api/export/replenish.xlsx",
    # Подписанный callback МойСклада: этим каналом магазин сообщает нам об
    # активации и снятии подписки. Закрыть его значило бы не пустить внешнюю
    # систему сказать, что организация ЗАПЛАТИЛА.
    "/ms/vendor/api/moysklad/vendor/1.0/apps/{path_app_id}/{account_id}",
})

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def require_write_access(
    request: Request,
    ctx: AuthContext = Depends(require_auth_api),
    db: Session = Depends(get_db),
) -> AuthContext:
    """402, если организация в readonly и гейт включён.

    Вешается ОДИН раз на приложение (`FastAPI(dependencies=[...])`), а не на
    каждый роут. Причина — в первой версии перечислялись закрытые ручки, и
    ревью нашло ровно то, чего такой список не мог не пропустить: сохранение
    того же токена МойСклада запускало полный синк организации, которой мы
    только что отказали в записи. Теперь запрещено по умолчанию: чтение и
    список ALWAYS_OPEN_PATHS проходят, остальное упирается в подписку.

    Роль (require_owner_api) проверяется отдельно и по-прежнему. Состояние
    grace пропускает запись: счёт выставлен, закрывать доступ нельзя.
    """
    if not gate_enabled():
        return ctx
    if request.method in SAFE_METHODS:
        return ctx
    route = request.scope.get("route")
    path = getattr(route, "path", request.url.path)
    if path in ALWAYS_OPEN_PATHS:
        return ctx
    # stamp=True: это попытка ЗАПИСИ, транзакция здесь ожидается. Отметка
    # «счёт выставлен», забытая оператором, ставится ровно тут и один раз.
    state = subscription_state(ctx.org, db, stamp=True)
    if state == READONLY:
        raise HTTPException(status_code=402, detail=BLOCK_MESSAGE)
    return ctx


def gate_dependency():
    """Зависимость приложения: пускает читающие запросы без сессии.

    Глобальная зависимость выполняется и на /login, и на статике, и на
    health-ручках, где сессии нет и быть не должно. Поэтому проверка
    авторизации здесь мягкая: нет сессии — нет и организации, которой можно
    что-то запретить; такой запрос отдадут 401/403 те, кому положено.
    """

    def _gate(request: Request, db: Session = Depends(get_db)) -> None:
        if not gate_enabled():
            return
        if request.method in SAFE_METHODS:
            return
        route = request.scope.get("route")
        path = getattr(route, "path", request.url.path)
        if path in ALWAYS_OPEN_PATHS:
            return
        ctx = resolve_auth(request, db)
        if ctx is None:
            return  # неавторизованный запрос закроет обычная защита роута
        if subscription_state(ctx.org, db, stamp=True) == READONLY:
            raise HTTPException(status_code=402, detail=BLOCK_MESSAGE)

    return Depends(_gate)


# ── Предпросмотр перед включением флага ──────────────────────────────────────

def preview(db: Session) -> dict:
    """Кого закроет гейт, если его включить. Ничего не меняет.

    Существует ради одного сценария: флаг включают, и сервис молча закрывается
    тем, кого закрывать не собирались. Считается на старте приложения и пишется
    в лог — так «посмотреть перед тем, как щёлкнуть» не требует ни доступа
    к базе, ни отдельной ручки.
    """
    from app.models import Org

    counts = {ACTIVE: 0, GRACE: 0, READONLY: 0}
    readonly_ids: list[int] = []
    broken: list[int] = []
    org_ids = [row[0] for row in db.execute(select(Org.id)).all()]
    for org_id in org_ids:
        # По одной, с перехватом: предпросмотр обязан пережить битую строку,
        # иначе именно в тот момент, когда оператор готовится включить гейт,
        # он получает пустой отчёт вместо предупреждения.
        try:
            org = db.get(Org, org_id)
            if org is None:
                continue
            state = subscription_state(org, db)
        except Exception:  # noqa: BLE001
            broken.append(org_id)
            continue
        counts[state] = counts.get(state, 0) + 1
        if state == READONLY:
            readonly_ids.append(org.id)
    return {"counts": counts, "readonly_org_ids": readonly_ids,
            "broken_org_ids": broken, "gate_enabled": gate_enabled()}


def log_preview() -> None:
    """Пишет предпросмотр в лог на старте. Ошибки глушит: это диагностика."""
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        info = preview(db)
        counts = info["counts"]
        # Список id обрезаем: у сотни организаций строка лога превращалась бы
        # в килобайты, а читают её глазами перед включением флага.
        ids = info["readonly_org_ids"]
        shown = ", ".join(str(x) for x in ids[:LOG_IDS_LIMIT]) or "—"
        if len(ids) > LOG_IDS_LIMIT:
            shown += f" и ещё {len(ids) - LOG_IDS_LIMIT}"
        if info["gate_enabled"]:
            log.warning(
                "гейт подписки ВКЛЮЧЁН: active=%d grace=%d readonly=%d; "
                "закрыты организации %s", counts[ACTIVE], counts[GRACE],
                counts[READONLY], shown,
            )
        else:
            log.info(
                "гейт подписки выключен; если включить: active=%d grace=%d "
                "readonly=%d (закрылись бы %s)", counts[ACTIVE], counts[GRACE],
                counts[READONLY], shown,
            )
        if info["broken_org_ids"]:
            # Битая строка = организация, состояние которой мы не смогли
            # вычислить. Молчать нельзя: при включённом гейте синк ей
            # разрешается «в пользу клиента», то есть неплательщик с опечаткой
            # в дате работал бы бесплатно и незаметно.
            log.warning("состояние подписки не удалось вычислить: организации %s "
                        "(проверьте формат «оплачено до»)", info["broken_org_ids"])
    except Exception:  # noqa: BLE001 — диагностика не имеет права валить старт
        log.exception("предпросмотр гейта подписки не удался")
    finally:
        db.close()
