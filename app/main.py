"""Приложение FastAPI: страницы (Jinja2), маршруты аутентификации, подключение API.

Шаблоны ищутся только в templates/. Отсутствующий шаблон — ошибка
(jinja2.TemplateNotFound), а не молчаливый фолбэк на заглушку (MAINT-4).
"""
import os
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app import auth
from app import logging_conf
from app import subscription

# Настраиваем логирование ДО создания приложения и до первого вызова
# любого log.* — uvicorn к этому моменту уже применил свою конфигурацию
# (только для своих логгеров), корневой остаётся за нами.
logging_conf.setup_logging()

from app.api import router as api_router  # noqa: E402
from app.crypto import is_prod
from app.routes_connect import router as connect_router
from app.routes_extra import router as extra_router
from app.routes_ms_app import router as ms_app_router
from app.routes_ms_vendor import router as ms_vendor_router
from app.db import get_db, init_db, record_migration_step, validate_migration_step
from app.models import Connection, Membership, Org, Product, ProductionOrder, Sale, User

BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATE_DIRS = [BASE_DIR / "templates"]
STATIC_DIR = BASE_DIR / "static"
TRIAL_DAYS = 14

# Гейт подписки (D-24) — ОДНА зависимость на всё приложение, а не декоратор на
# каждой пишущей ручке. Запрещено по умолчанию: читающие запросы и явный
# список subscription.ALWAYS_OPEN_PATHS проходят, остальное упирается в
# состояние подписки. Первая версия перечисляла ЗАКРЫТЫЕ ручки, и ревью нашло
# ровно то, чего такой список не мог не пропустить. Выключен, пока нет
# OBOROT_SUBSCRIPTION_GATE=1 — тогда зависимость не делает ни одного запроса.
app = FastAPI(title="Оборот", docs_url=None, redoc_url=None,
              dependencies=[subscription.gate_dependency()])
app.include_router(api_router)
app.include_router(connect_router)
app.include_router(extra_router)
app.include_router(ms_vendor_router)
app.include_router(ms_app_router)


def _csrf_reject():
    from fastapi.responses import JSONResponse as _JR
    resp = _JR(status_code=403, content={"detail": "CSRF: запрос отклонён"})
    resp.headers["Content-Security-Policy"] = "frame-ancestors 'self' https://online.moysklad.ru"
    return resp


@app.middleware("http")
async def _security_headers_and_csrf(request: Request, call_next):
    """CSP frame-ancestors + CSRF-защита изменяющих запросов.

    CSP: приложение маркетплейса МС работает полностраничным iframe — вместо
    полного запрета фреймов ограничиваем предков (современная замена
    X-Frame-Options). Ставим и на 500 (обработка исключения ниже).

    CSRF /api: в embedded-режиме сессионная кука на проде SameSite=None (иначе
    не долетит в iframe МС), поэтому Lax-защита не работает. Для изменяющих
    методов на /api/* требуем кастомный заголовок X-Oborot-CSRF: браузер не
    даст поставить его в кросс-доменном запросе без CORS-preflight, а CORS мы
    не разрешаем — значит сторонний сайт не сможет дёрнуть наши ручки с
    кукой пользователя. Свой фронт заголовок шлёт всегда (см. app.js api()).
    Vendor-lifecycle (/ms/...) сюда не попадает: это server-to-server c JWT,
    без куки — CSRF там неприменим.

    CSRF /login /register /logout (SEC-5): это обычные HTML-формы, браузер
    шлёт их сам — заголовок на них не поставить. Проверяем double-submit
    токен (auth.verify_csrf_form): скрытое поле формы должно совпасть с
    подписанной кукой сессии. X-Oborot-CSRF остаётся допустимой
    альтернативой и здесь — ровно то же свойство (сторонняя форма не может
    поставить кастомный заголовок), которым уже держится защита /api; это
    и сохраняет совместимость с машинными клиентами тестов, которые шлют
    этот заголовок глобально, не заходя на GET-страницу формы за токеном.

    Тело читаем через request.body(), а не request.form(): BaseHTTPMiddleware
    реплеит вниз по стеку то, что закешировал сам объект Request (в
    self._body — только после body()); form() читает через stream() и вниз
    уходит уже пустое тело, и Form(...) в самой ручке получил бы None вместо
    email/password. Формы без файлов — простого urlencoded-парсинга хватает.
    """
    path = request.url.path
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        if path.startswith("/api/"):
            if request.headers.get("X-Oborot-CSRF") is None:
                return _csrf_reject()
        elif path in auth.CSRF_FORM_PATHS:
            if request.headers.get("X-Oborot-CSRF") is None:
                from urllib.parse import parse_qsl
                raw = await request.body()
                fields = dict(parse_qsl(raw.decode("utf-8", errors="replace"), keep_blank_values=True))
                if not auth.verify_csrf_form(request, fields.get(auth.CSRF_FORM_FIELD)):
                    return _csrf_reject()
    response = await call_next(request)
    response.headers.setdefault(
        "Content-Security-Policy",
        "frame-ancestors 'self' https://online.moysklad.ru",
    )
    return response

@app.middleware("http")
async def _log_org_context(request: Request, call_next):
    """Помечает все записи лога этого запроса идентификатором организации.

    Добавлено последним, поэтому оборачивает остальные middleware — метка
    стоит и на записях о CSRF-отказе, и на необработанном исключении.

    Организацию берём из ПОДПИСАННОЙ сессионной куки (`auth.read_session`),
    без обращения к базе: логирование не должно добавлять запрос к БД на
    каждый чих, а подделать значение нельзя — подпись проверяется. Здесь не
    проверяется членство пользователя в организации: для отбора строк в логе
    этого достаточно, а решения о доступе принимает `resolve_auth`.

    `/static` пропускаем: снимать подпись с куки на каждую картинку — трата
    без пользы, у статики нет своих записей в логе.
    """
    if not request.url.path.startswith("/static"):
        try:
            sess = auth.read_session(request)
        except Exception:  # noqa: BLE001 — логирование не имеет права ронять запрос
            sess = None
        logging_conf.set_org(sess.get("org_id") if sess else None)
    return await call_next(request)


from app import scheduler as _scheduler  # noqa: E402
_scheduler.attach(app)
templates = Jinja2Templates(directory=[str(d) for d in TEMPLATE_DIRS])

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _check_single_process() -> None:
    """Fail-fast при попытке поднять прод в несколько воркеров.

    Кэш аналитики и его сброс, реестр потоков синхронизации, флаг планировщика,
    лимит попыток входа и защита от повтора внешних JWT живут В ПАМЯТИ процесса.
    При нескольких воркерах это даёт не «чуть медленнее», а неправильно:
    пользователь видит разные числа на одной странице (кэш сбрасывается только
    у того воркера, который обработал запись), ночной синк и дайджест уходят
    столько раз, сколько воркеров, а синк одной организации может пойти
    параллельно сам с собой.

    Требование «один воркер» до сих пор жило только в комментариях и README —
    то есть не существовало. Здесь оно становится проверяемым.

    Число воркеров надёжно из процесса не видно, поэтому смотрим два признака:
    переменную WEB_CONCURRENCY (её ставят PaaS и gunicorn) и аргументы запуска
    (`--workers N` / `-w N` у uvicorn). Осознанный многопроцессный запуск можно
    разрешить, выставив OBOROT_ALLOW_MULTIPROC=1 — но тогда кэш, лимитер и
    планировщик нужно выносить наружу, а планировщик оставлять на ОДНОМ
    процессе (SCHEDULER_ENABLED=0 на остальных).
    """
    import sys as _sys
    if os.environ.get("OBOROT_ALLOW_MULTIPROC", "").strip() in ("1", "true", "yes"):
        return
    workers = 0
    raw = (os.environ.get("WEB_CONCURRENCY") or "").strip()
    if raw.isdigit():
        workers = int(raw)
    argv = _sys.argv
    for i, a in enumerate(argv):
        if a in ("--workers", "-w") and i + 1 < len(argv) and argv[i + 1].isdigit():
            workers = max(workers, int(argv[i + 1]))
        elif a.startswith("--workers=") and a.split("=", 1)[1].isdigit():
            workers = max(workers, int(a.split("=", 1)[1]))
    if workers > 1:
        raise RuntimeError(
            f"«Оборот» запущен в {workers} воркеров, а рассчитан на один: кэш "
            "аналитики, лимит входа и планировщик живут в памяти процесса, и "
            "числа на страницах разъедутся, а ночной синк выполнится несколько "
            "раз. Уберите --workers/WEB_CONCURRENCY либо, если это осознанно, "
            "выставьте OBOROT_ALLOW_MULTIPROC=1 и SCHEDULER_ENABLED=1 ровно на "
            "одном процессе."
        )


# ── OPS-5: объявленный порядок шагов старта ──────────────────────────────────
#
# Единственное место, где порядок шагов старта записан как данные, а не как
# порядок строк кода. Таблица не исполняет шаги, а даёт им стабильный
# идентификатор и позицию для журнала (app/db.record_migration_step): вызовы
# ниже идут ровно в этом порядке, с теми же ленивыми импортами, что и до OPS-5.
#
# Список APPEND-ONLY. Новая миграция дописывается В КОНЕЦ с новым id и новой
# позицией; менять id или позицию уже выпущенного шага нельзя — на базах, где
# он записан, старт после такой правки упадёт с MigrationLedgerConflict, и это
# не дефект, а тот самый замок (AGENTS.md §1: «только новая миграция сверху»).
# Если смысл шага изменился настолько, что прежнее свидетельство больше не
# годится, заводится НОВЫЙ шаг с новым id, а старая строка остаётся как есть.
STARTUP_SCHEMA_STEPS: tuple[tuple[str, int], ...] = (
    ("init_db", 1),
    ("lessons.ensure_schema", 2),
    ("exclusions.ensure_schema", 3),
    ("ms_sync.ensure_schema", 4),
    ("ms_sync.reset_stale_running", 5),
    ("ms_writeback.ensure_schema", 6),
    ("ms_vendor.ensure_schema", 7),
    ("subscription.ensure_schema", 8),
    ("subscription.log_preview", 9),
    # SUPPLY-1 (D-49/D-50). Дописан В КОНЕЦ новым id и позицией 10 — а не
    # добавлен внутрь `init_db`/`models.ensure_schema`, откуда он и переехал
    # по ревью PR #46 (discussion_r3894000377). Под идентичностью выпущенного
    # шага у этой схемной работы не было бы собственного свидетельства в
    # журнале, и порядок относительно будущих миграций журнал бы не удержал.
    # Девять строк выше при этом не тронуты — ни id, ни позиция.
    ("models.ensure_supply_schema", 10),
)
_STARTUP_STEP_ORDER = dict(STARTUP_SCHEMA_STEPS)

# Курсор фактически выполненных шагов текущего старта. Обнуляется в
# _validate_startup_order() — то есть в начале каждого прохода `_startup()`.
_STARTUP_CURSOR = 0


class StartupOrderViolation(RuntimeError):
    """Фактический порядок шагов старта разошёлся с объявленным.

    Отдельный тип, а не голый RuntimeError: это стоп-условие того же рода, что
    MigrationLedgerConflict, и вызывающая сторона обязана уметь отличить его от
    сбоя самого шага, не разбирая текст сообщения.
    """


def _validate_startup_order() -> None:
    """Сверяет ВЕСЬ объявленный порядок с журналом до первого шага.

    Сначала проверяется сам список: два одинаковых id или две одинаковые
    позиции в `STARTUP_SCHEMA_STEPS` — ошибка объявления, и ловить её на
    середине старта поздно. Затем каждая пара сверяется с журналом базы.

    Проверка всего списка целиком, а не только очередного шага, нужна ровно
    затем, зачем заведён замок: перестановка почти никогда не задевает один
    шаг. Если конфликт объявлен на позиции 6, то на этой базе не должен
    выполниться и первый шаг — иначе процесс успевает поработать по порядку,
    который уже признан противоречивым.
    """
    global _STARTUP_CURSOR
    _STARTUP_CURSOR = 0
    seen_ids: set[str] = set()
    seen_orders: set[int] = set()
    for step_id, step_order in STARTUP_SCHEMA_STEPS:
        if step_id in seen_ids or step_order in seen_orders:
            raise RuntimeError(
                f"объявление шагов старта противоречиво: {step_id!r}/{step_order} "
                "встречается дважды в STARTUP_SCHEMA_STEPS"
            )
        seen_ids.add(step_id)
        seen_orders.add(step_order)
        validate_migration_step(step_id, step_order)


def _record_startup_step(step_id: str) -> None:
    """Отмечает в журнале шаг старта, который ТОЛЬКО ЧТО успешно завершился.

    Вызывается строго после соответствующего шага: упавший шаг записи не
    получает.
    """
    record_migration_step(step_id, _STARTUP_STEP_ORDER[step_id])


def _startup_step(step_id: str, run) -> None:
    """Один шаг старта: preflight → сам шаг → запись успеха.

    Порядок этих трёх действий — предмет ревью жизненного цикла 28.08.2026
    (PR #44, discussion_r3884250490). Пока конфликт ловился только на записи,
    fail-closed срабатывал ПОСЛЕ того, как шаг отработал: с чужим шагом на
    позиции 1 старт падал, но `init_db()` уже успевал создать схему. Для
    сегодняшних аддитивных шагов это безобидно, но замок заводился как раз
    на случай переставленного НЕАДДИТИВНОГО шага — а для него «сначала
    выполнили, потом заметили» означает, что замка нет.

    Поэтому preflight стоит ПЕРЕД `run()`. Записать успех до шага нельзя по
    той же логике с другого конца: журнал обязан говорить о том, что
    действительно случилось.

    `run` передаётся уже разрешённым значением, а не именем: тесты подменяют
    шаги атрибутами модулей, и подмена обязана долетать.

    Первым делом проверяется, что это ДЕЙСТВИТЕЛЬНО очередной шаг объявленного
    списка. Это вторая претензия того же ревью (discussion_r3884257316), и она
    про другое, чем первая: пара (id, позиция) статична и не меняется, если
    будущая правка переставит два вызова ВМЕСТЕ с их идентификаторами. Журнал
    в таком случае принимает все строки как уже знакомые, старт проходит
    целиком — а миграции выполнились в другом порядке. Курсор привязывает
    фактическую последовательность вызовов к единственному объявленному
    порядку: шаг не на своём месте не выполняется вовсе.
    """
    global _STARTUP_CURSOR
    if _STARTUP_CURSOR >= len(STARTUP_SCHEMA_STEPS):
        raise StartupOrderViolation(
            f"шаг старта {step_id!r} выполняется после того, как объявленный "
            f"список из {len(STARTUP_SCHEMA_STEPS)} шагов уже исчерпан"
        )
    expected_id, expected_order = STARTUP_SCHEMA_STEPS[_STARTUP_CURSOR]
    if step_id != expected_id:
        raise StartupOrderViolation(
            f"фактический порядок шагов старта разошёлся с объявленным: "
            f"на позиции {expected_order} ожидался {expected_id!r}, "
            f"а выполняется {step_id!r}. Порядок задаётся одним списком "
            "STARTUP_SCHEMA_STEPS; переставлять вызовы в _startup() нельзя"
        )
    validate_migration_step(step_id, expected_order)
    run()
    _record_startup_step(step_id)
    _STARTUP_CURSOR += 1


def _finish_startup_steps() -> None:
    """Убеждается, что выполнены ВСЕ объявленные шаги, и ни одного сверх.

    Курсор ловит перестановку и лишний вызов, но сам по себе не заметил бы
    пропуска: список, оборванный на восьмом шаге, монотонен. Эта проверка
    закрывает разницу — и стоит она перед `scheduler.start()`, чтобы
    планировщик не поднялся над недоделанным стартом.
    """
    done = _STARTUP_CURSOR
    total = len(STARTUP_SCHEMA_STEPS)
    if done != total:
        missing = [sid for sid, _pos in STARTUP_SCHEMA_STEPS[done:]]
        raise StartupOrderViolation(
            f"выполнено {done} шагов старта из объявленных {total}; "
            f"не выполнены: {', '.join(missing)}"
        )


@app.on_event("startup")
def _startup() -> None:
    # Fail-fast, пока сервис ещё не принял ни одного запроса: в проде
    # OBOROT_TRUSTED_PROXY_HOPS обязан быть задан явно (0 или больше) — иначе
    # лимит входа по IP молча выключается ровно тогда, когда нужен (см.
    # auth.check_proxy_config).
    auth.check_proxy_config()
    _check_single_process()
    # Замок порядка проверяется ДО первого шага и повторно перед каждым (см.
    # _startup_step): единственное, что при этом заводится на базе без
    # журнала, — сама таблица журнала и её индекс.
    _validate_startup_order()
    _startup_step("init_db", init_db)
    # OPS-6: последняя migration-on-import. Раньше вызывалась на импорте
    # app/api.py — до старта приложения и вне защиты от гонки нескольких
    # воркеров, тем же классом дефекта, что и ms_writeback/ms_vendor ниже.
    from app import lessons as _lessons
    _startup_step("lessons.ensure_schema", _lessons.ensure_schema)
    from app import exclusions as _exclusions
    _startup_step("exclusions.ensure_schema", _exclusions.ensure_schema)
    # Аудит 18.08: убитый процессом синк оставался state='running' навсегда
    # и блокировал все будущие запуски организации.
    from app import ms_sync as _ms_sync
    _startup_step("ms_sync.ensure_schema", _ms_sync.ensure_schema)
    _startup_step("ms_sync.reset_stale_running", _ms_sync.reset_stale_running)
    # Д4 (ревью 22.08): эти две миграции раньше запускались на импорте
    # routes_connect.py / routes_ms_vendor.py — до старта приложения и вне
    # защиты от гонки нескольких воркеров (обращение к базе на импорте
    # модуля само по себе было опасно). Место — здесь, вместе с остальными
    # аддитивными миграциями.
    from app import ms_writeback as _ms_writeback
    _startup_step("ms_writeback.ensure_schema", _ms_writeback.ensure_schema)
    from app import ms_vendor as _ms_vendor
    _startup_step("ms_vendor.ensure_schema", _ms_vendor.ensure_schema)
    # D-24: orgs.paid_until + billing_requests.invoiced_at. Колонки заводим
    # всегда, сам гейт включается флагом OBOROT_SUBSCRIPTION_GATE — схема
    # должна быть готова заранее, иначе включение флага потребует деплоя.
    from app import subscription as _subscription
    _startup_step("subscription.ensure_schema", _subscription.ensure_schema)
    # Предпросмотр в лог: кого закроет гейт, если его включить. Читает базу,
    # ничего не меняет. Нужен ровно затем, чтобы включение флага не оказалось
    # сюрпризом — «посмотреть перед тем, как щёлкнуть».
    _startup_step("subscription.log_preview", _subscription.log_preview)
    # SUPPLY-1 (D-49/D-50): аддитивная колонка идентификатора партии в
    # production_orders, ЧАСТИЧНЫЙ уникальный индекс по её непустым значениям
    # и условный backfill пустых (подробности — в докстринге самой функции).
    # Терминальный шаг позиции 10 со своим id — см. STARTUP_SCHEMA_STEPS.
    #
    # Имя поля здесь намеренно не пишется: tests/test_supply.py структурно
    # проверяет, что вне models.py/api.py его не упоминает ни один модуль
    # приложения — так «идентификатор никуда не уезжает» остаётся проверяемым
    # фактом, а не обещанием в комментарии.
    #
    # Шаг идемпотентен и выполняется НА КАЖДОМ старте: строка журнала здесь —
    # свидетельство, а не основание пропустить (`_startup_step` зовёт run()
    # ДО записи, а `validate_migration_step` отметку «уже был» намеренно не
    # использует как решение пропустить). Для backfill это существенно:
    # откатившийся старый код создаёт новую пустую строку уже ПОСЛЕ того, как
    # шаг записан, и вылечить её обязан следующий старт.
    from app import models as _models
    _startup_step("models.ensure_supply_schema", _models.ensure_supply_schema)
    # Замок на пропуск: все объявленные шаги выполнены, и ровно они.
    _finish_startup_steps()
    global _STARTUP_DONE
    _STARTUP_DONE = True
    # OPS-6: планировщик стартует последним статементом, ПОСЛЕ того как все
    # миграции/ensure_schema выше завершились успешно — см. scheduler.attach.
    _scheduler.start()


# ── Health-эндпоинты ─────────────────────────────────────────────────────────
#
# До сих пор единственным способом узнать, что сервису плохо, был Telegram-алерт
# о втором подряд упавшем синке — то есть о частном случае, и постфактум.
# Инцидент 03–21.08 (синк молча падал на протухшем токене, данные протухали
# восемнадцать дней) показал цену этого. Две ручки ниже — минимум, который
# позволяет хостингу и внешнему мониторингу отличать «процесс жив» от
# «сервис готов работать»; авторизации не требуют и данных не раскрывают.

_STARTUP_DONE = False


@app.get("/health/live", include_in_schema=False)
def health_live():
    """Процесс жив и отвечает. Намеренно НЕ трогает базу.

    Liveness обязан отвечать быстро и не зависеть от внешних систем: иначе
    временная недоступность БД приводит к перезапуску процесса, а перезапуск
    рвёт фоновую догрузку истории и обнуляет прогресс синхронизации.
    """
    return {"status": "ok"}


@app.get("/health/ready", include_in_schema=False)
def health_ready(db: Session = Depends(get_db)):
    """Готов обслуживать запросы: старт завершён (миграции прошли), БД отвечает.

    Планировщик в готовность НЕ входит: он может быть намеренно выключен
    (SCHEDULER_ENABLED=0 в dev и на втором процессе), и это не мешает отдавать
    страницы. Его состояние отдаётся справочно.
    """
    from sqlalchemy import text as _text
    checks: dict = {"startup": _STARTUP_DONE}
    try:
        db.execute(_text("SELECT 1"))
        checks["db"] = True
    except Exception as exc:  # noqa: BLE001 — наружу отдаём только тип ошибки
        checks["db"] = False
        checks["db_error"] = type(exc).__name__
    checks["scheduler"] = bool(getattr(_scheduler, "_started", False))
    # Версия сборки наружу. Раньше «что именно сейчас в бою» нельзя было
    # спросить у самого прода: OBOROT_COMMIT писался деплоем, но не отдавался
    # ни одной ручкой, и выкладку приходилось подтверждать косвенно — по тому,
    # что появился новый эндпоинт. Это доказательство «от отсутствия», и оно
    # молчит ровно тогда, когда пакет не содержит новых ручек.
    #
    # Секрета здесь нет: это хеш публичного коммита публичного репозитория.
    from app.version import BUILD_COMMIT, DOMAIN_VERSION
    checks["commit"] = BUILD_COMMIT
    checks["domain_version"] = DOMAIN_VERSION
    ok = bool(checks["startup"] and checks["db"])
    return JSONResponse(status_code=200 if ok else 503,
                        content={"status": "ok" if ok else "not ready", **checks})


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
          db: Session | None = None, extra: dict | None = None):
    """Рендер страницы с обязательным контекстом {user, org, active, page_title}.

    extra — дополнительные переменные шаблона (нужны «Обучению»: контакт
    поддержки берётся из env и в общий контекст страниц не входит).

    Единственная точка рендера страниц под сессией. До 22.08 таких точек было
    ДВЕ: в routes_extra лежала независимая копия, которая не клала в контекст
    ни `embedded`, ни признак демо-данных. Следствие: шесть страниц (Мастер
    заказа, Заказ позиции, Бюджет, Прогноз, Оборот, Обучение) не умели
    встроенный режим МойСклада вообще — сколько бы его ни поддерживали шаблоны.
    Копию убрали, routes_extra зовёт эту функцию.
    """
    from app import subscription as _subscription

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
    csrf_token = auth.get_csrf_token(request)
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
            # Состояние подписки в контекст страницы. Нужно ровно для одного:
            # в readonly интерфейс не должен слать служебные POST (отметки
            # подсказок и прогресса обучения) — по строгому режиму они закрыты,
            # и человек получал бы 402 при обычном листании СВОИХ ЖЕ страниц.
            # Считаем здесь, а не отдельным запросом с фронта: страница и так
            # рендерится под сессией, лишний round-trip на каждую загрузку —
            # плата за то, что и так известно на сервере.
            # Именно «запись СЕЙЧАС запрещена», а не просто «состояние
            # readonly»: при выключенном флаге гейта запись проходит, и гасить
            # отметки подсказок было бы вредом без причины — человек с
            # истёкшим триалом перестал бы запоминать закрытые подсказки на
            # ровном месте.
            "writes_blocked": (
                _subscription.gate_enabled()
                and db is not None
                and _subscription.subscription_state(org, db) == _subscription.READONLY
            ),
            **(extra or {}),
            "csrf_token": csrf_token,
        },
    )
    # CSRF-кука формы logout (SEC-5): в iframe МойСклад на проде нужна
    # SameSite=None+Secure — иначе браузер отбросит куку в третьесторонней
    # рамке, и double-submit станет невозможен для легитимного пользователя.
    auth.set_csrf_cookie(response, csrf_token, samesite=("none" if (embedded and is_prod()) else "lax"))
    # dev-only: ?embed=1/0 залипает кукой, чтобы навигация сохраняла режим.
    if override is True:
        auth.set_embed(response)
    elif override is False:
        auth.clear_embed(response)
    return response


def _data_is_loading(org_id: int) -> bool:
    """Идёт первичная загрузка или её часть уже на диске (деплой П1, мажор 4).

    До finalize-lite подключение ещё 'pending', и «/» уводило на онбординг,
    где по умолчанию выбраны «Демо-данные» — клик стирал таблицы организации
    прямо во время записи их синком. Пока синк идёт (или уже есть покрытие),
    показываем обычные страницы: они работают на загруженной части истории.
    """
    from app import ms_sync as _ms_sync

    if _ms_sync.is_running(org_id):
        return True
    return _ms_sync.get_status(org_id).get("coverage_days", 0) > 0


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
    token = auth.get_csrf_token(request)
    response = templates.TemplateResponse(
        request,
        template,
        {"user": None, "org": None, "active": "", "page_title": "Оборот",
         **extra, "csrf_token": token},
    )
    auth.set_csrf_cookie(response, token)
    return response


# Прощальные сообщения после удаления аккаунта (?deleted=...): человек должен
# увидеть подтверждение, что всё действительно стёрто, а не просто «вас выкинуло».
_DELETED_NOTICES = {
    "org": "Организация и все её данные удалены: товары, продажи, заказы, "
           "настройки и подключение к МойСкладу вместе с токеном. Восстановить их нельзя. "
           "Спасибо, что попробовали «Оборот» — будем рады, если вернётесь.",
    "account": "Ваш аккаунт удалён. Данные организации остались у её участников. "
               "Спасибо, что были с нами.",
}


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, deleted: str = ""):
    return _render_auth(request, "login.html", notice=_DELETED_NOTICES.get(deleted))


# Почта поддержки: пока нет восстановления пароля, это единственный способ
# вернуть доступ — значит человек должен видеть её ровно тогда, когда заперт.
SUPPORT_EMAIL = "tsitsilinvlad@gmail.com"


def _minutes_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "минуту"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "минуты"
    return "минут"


def _too_many_attempts_error(retry_after_sec: int) -> str:
    """Текст блокировки: сколько именно ждать и что делать, если пароль забыт.

    Срок здесь ФИКСИРОВАННЫЙ (см. auth.LoginLimiter) — чужие попытки, пока
    блокировка активна, его не сдвигают, так что название «через N минут»
    остаётся правдой, сколько бы их ни пришло следом.
    """
    mins = max(1, -(-retry_after_sec // 60))
    return (
        f"Слишком много неудачных попыток входа в этот аккаунт. "
        f"Попробуйте снова через {mins} {_minutes_word(mins)} — счётчик обнулится сам. "
        f"Если вы забыли пароль: восстановления по почте у нас пока нет, "
        f"напишите на {SUPPORT_EMAIL} с адреса, на который заведён аккаунт, — вернём доступ."
    )


@app.post("/login")
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    email_norm = email.strip().lower()
    # Ключ аккаунта не зависит от адреса: подбор пароля к одному аккаунту
    # блокируется, с каких бы адресов он ни шёл. Ключ по IP считаем только
    # если адресу можно верить (см. auth.client_ip).
    ip, ip_trusted = auth.client_ip(request)
    acc_key, ip_key = f"acc:{email_norm}", f"ip:{ip}"
    # Оба лимита читаем ДО проверки пароля — сообщение о блокировке (если она
    # есть) должно отражать состояние на момент запроса, а не то, что успеет
    # поменяться внутри hit() ниже.
    acc_retry = auth.login_limiter.retry_after(acc_key)
    ip_retry = auth.ip_login_limiter.retry_after(ip_key) if ip_trusted else 0
    # Пароль проверяем ВСЕГДА, независимо от лимитов. Оба лимита — мягкие:
    # они ограничивают скорость подбора (сколько НЕУДАЧНЫХ попыток пройдёт в
    # окно), а не запрещают вход владельцу аккаунта. Раньше лимит по
    # аккаунту был жёстким (проверка стояла до пароля) — значит зная только
    # чужой e-mail, можно было пятью запросами раз в несколько минут держать
    # платящего клиента заблокированным бессрочно; восстановления пароля в
    # продукте нет, обращаться было бы не к кому. «Мягкость» не превращается
    # в оракул: подбирающий не может по разнице ответов понять, насколько
    # близко подошёл — сообщения различаются только по состоянию ЛИМИТА
    # (заблокирован / нет), а единственный сигнал успеха — собственно вход
    # (редирект), как и раньше.
    user = db.execute(
        select(User).where(User.email == email_norm)
    ).scalars().first()
    if user is None or not auth.verify_password(password, user.pw_hash):
        # Счётчик аккаунта растёт всегда, даже когда сработал лимит по IP:
        # иначе подбиратель, сам себя «заблокировавший» по адресу, получил бы
        # неограниченный оракул «верный пароль / неверный». Пока ключ уже
        # заперт, hit() — no-op (см. auth.LoginLimiter): чужие неудачные
        # попытки не продлевают и не переоткрывают блокировку.
        auth.login_limiter.hit(acc_key)
        if ip_trusted:
            auth.ip_login_limiter.hit(ip_key)
        if acc_retry:
            return _render_auth(
                request, "login.html", email=email_norm,
                error=_too_many_attempts_error(acc_retry),
            )
        if ip_retry:
            return _render_auth(request, "login.html", email=email_norm,
                                error=_too_many_attempts_error(ip_retry))
        # Задержки ответа здесь СПЕЦИАЛЬНО нет. Синхронный эндпоинт выполняется
        # в пуле потоков FastAPI (по умолчанию их немного), и секунда сна
        # занимает поток целиком: десяток параллельных попыток подбора тормозил
        # весь сайт для обычных посетителей, а подбирателю стоил максимум трёх
        # секунд на аккаунт (дальше всё равно срабатывает лимит попыток).
        # Асинхронный вариант тут тоже не годится: bcrypt и запросы к БД в
        # async-эндпоинте заблокировали бы уже event loop, то есть все запросы
        # разом. Защита — лимит попыток выше.
        return _render_auth(request, "login.html", email=email_norm, error="Неверный e-mail или пароль")
    # Верный пароль пропускает ВСЕГДА, даже если лимит только что был
    # исчерпан, — и снимает обе блокировки: владелец аккаунта, который их
    # заслуженно не должен видеть, не должен и ждать их истечения.
    auth.login_limiter.reset(acc_key)
    if ip_trusted:
        auth.ip_login_limiter.reset(ip_key)
    member = db.execute(
        select(Membership).where(Membership.user_id == user.id)
    ).scalars().first()
    if member is None:
        return _render_auth(request, "login.html", email=email_norm, error="У пользователя нет организации")
    response = RedirectResponse("/", status_code=303)
    auth.set_session(response, user.id, member.org_id, user.session_version)
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
    if len(password.encode("utf-8")) > 72:
        # bcrypt физически не хеширует пароль длиннее 72 байт. Длина считается
        # в байтах, а не в символах: русская буква весит два байта, латинская —
        # один, поэтому предел — это примерно 36 русских букв или 72 латинских.
        return _render_auth(
            request, "register.html", name=name, org_name=org_name, email=email,
            error="Пароль слишком длинный. Лимит — 72 байта (это примерно 36 русских букв "
                  "или 72 латинских) — сократите фразу.",
        )
    exists = db.execute(select(User.id).where(User.email == email_norm)).first()
    if exists:
        return _render_auth(request, "register.html", name=name, org_name=org_name, email=email, error="Такой e-mail уже зарегистрирован")

    user = User(
        email=email_norm, pw_hash=auth.hash_password(password), name=name.strip(),
        # SEC-3 corrective: непредсказуемый положительный старт версии сессии —
        # не 0 из server_default (см. auth.new_session_version_seed).
        session_version=auth.new_session_version_seed(),
    )
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
    auth.set_session(response, user.id, org.id, user.session_version)
    return response


@app.post("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    auth.clear_session(response)
    return response


# ── Аккаунт: пароль и удаление (страница /account) ───────────────────────────
# Сервис просит доступ к учётной системе клиента — значит обязан показывать
# «путь назад»: сменить пароль и уйти, забрав данные. Пути /profile и
# /security раньше давали 404 — теперь ведут сюда же (люди ищут их наугад).
#
# Почему удаляем физически, а не помечаем «удалён»: в интерфейсе мы обещаем
# «данные стираются» — значит они должны быть стёрты, иначе обещание хуже,
# чем его отсутствие. Плюс: токен МойСклада (даже зашифрованный) не должен
# лежать в базе после отключения, а e-mail с уникальным индексом не должен
# мешать человеку зарегистрироваться заново.

# Слово-подтверждение: печатается руками, одной кнопкой аккаунт не удалить.
DELETE_CONFIRM_WORD = "УДАЛИТЬ"


@app.get("/account", response_class=HTMLResponse)
def account_page(request: Request, db: Session = Depends(get_db)):
    return _authed_page(request, db, "account.html", "settings", "Аккаунт и безопасность")


@app.get("/profile")
@app.get("/security")
def account_aliases():
    """Люди ищут эти адреса наугад — ведём их на настоящую страницу аккаунта."""
    return RedirectResponse("/account", status_code=302)


def _org_members(db: Session, org_id: int) -> list[tuple[User, Membership]]:
    """Все участники организации: (пользователь, членство)."""
    return list(
        db.execute(
            select(User, Membership)
            .join(Membership, Membership.user_id == User.id)
            .where(Membership.org_id == org_id)
            .order_by(User.id)
        ).all()
    )


@app.get("/api/account")
def api_account(
    ctx: auth.AuthContext = Depends(auth.require_auth_api), db: Session = Depends(get_db)
):
    """Что человек увидит перед удалением: кто он, что уйдёт, кто останется."""
    members = _org_members(db, ctx.org.id)
    others = [(u, m) for u, m in members if u.id != ctx.user.id]
    conn = db.execute(
        select(Connection).where(Connection.org_id == ctx.org.id).order_by(Connection.id.desc())
    ).scalars().first()
    products = db.execute(
        select(func.count()).select_from(Product).where(Product.org_id == ctx.org.id)
    ).scalar() or 0
    sales = db.execute(
        select(func.count()).select_from(Sale).where(Sale.org_id == ctx.org.id)
    ).scalar() or 0
    orders = db.execute(
        select(func.count()).select_from(ProductionOrder).where(ProductionOrder.org_id == ctx.org.id)
    ).scalar() or 0
    return {
        "user": {"name": ctx.user.name, "email": ctx.user.email},
        "role": ctx.role,
        "org": {"name": ctx.org.name, "plan": ctx.org.plan},
        "others": [
            {"name": u.name, "email": u.email, "role": m.role} for u, m in others
        ],
        "connection": (
            {"kind": conn.kind, "status": conn.status, "has_token": bool(conn.token_enc)}
            if conn else None
        ),
        "counts": {"products": int(products), "sales": int(sales), "orders": int(orders)},
        "confirm_word": DELETE_CONFIRM_WORD,
    }


class PasswordChangeIn(BaseModel):
    current_password: str = Field(default="")
    new_password: str = Field(default="")
    confirm_password: str = Field(default="")


@app.post("/api/account/password")
def api_change_password(
    body: PasswordChangeIn,
    ctx: auth.AuthContext = Depends(auth.require_auth_api),
    db: Session = Depends(get_db),
):
    """Смена пароля: текущий + новый + подтверждение.

    Правила те же, что при регистрации (см. register_submit): минимум 8
    символов и не длиннее 72 БАЙТ — физический предел bcrypt (русская буква
    весит два байта, поэтому предел — примерно 36 русских букв).
    """
    if not auth.verify_password(body.current_password, ctx.user.pw_hash):
        raise HTTPException(
            status_code=403,
            detail="Текущий пароль не подошёл. Проверьте раскладку и регистр — "
                   "или выйдите и войдите заново, чтобы убедиться в пароле.",
        )
    if body.new_password != body.confirm_password:
        raise HTTPException(
            status_code=422, detail="Новый пароль и подтверждение не совпадают — введите их заново"
        )
    if len(body.new_password) < 8:
        raise HTTPException(status_code=422, detail="Новый пароль — минимум 8 символов")
    if len(body.new_password.encode("utf-8")) > 72:
        raise HTTPException(
            status_code=422,
            detail="Пароль слишком длинный. Лимит — 72 байта (это примерно 36 русских букв "
                   "или 72 латинских) — сократите фразу.",
        )
    if body.new_password == body.current_password:
        raise HTTPException(
            status_code=422, detail="Новый пароль совпадает со старым — придумайте другой"
        )
    new_pw_hash = auth.hash_password(body.new_password)
    # SEC-3 corrective: инкремент версии сессии — атомарное DB-side выражение
    # `session_version = session_version + 1` ОДНИМ UPDATE, а не Python-side
    # read-modify-write (было: user.session_version = user.session_version + 1
    # на объекте, загруженном в начале запроса). Под двумя одновременными
    # сменами пароля, стартовавшими от одного и того же значения, ORM-вариант
    # терял инкремент — оба запроса читали одну и ту же версию и оба писали
    # одно и то же +1. Здесь СЛОЖЕНИЕ выполняет сама БД поверх её АКТУАЛЬНОГО
    # значения на момент записи: SQLite (busy_timeout, см. app/db.py) и
    # Postgres (row lock при READ COMMITTED) обе сериализуют конкурентные
    # UPDATE той же строки — вторая транзакция ждёт коммита первой и видит уже
    # инкрементированное значение, поэтому конкурентные успешные смены пароля
    # гарантированно получают РАЗНЫЕ версии, а не теряют инкремент друг друга.
    # synchronize_session=False: мы намеренно не трогаем атрибуты ctx.user в
    # identity map — актуальная версия для куки берётся отдельным SELECT ниже,
    # читающим значение внутри той же (ещё не закоммиченной) транзакции.
    db.execute(
        update(User)
        .where(User.id == ctx.user.id)
        .values(pw_hash=new_pw_hash, session_version=User.session_version + 1)
        .execution_options(synchronize_session=False)
    )
    new_version = db.execute(
        select(User.session_version).where(User.id == ctx.user.id)
    ).scalar_one()
    db.commit()
    # Текущую сессию НЕ обрываем: человек только что доказал знание пароля,
    # выкидывать его на форму входа посреди работы незачем. Куку переставляем
    # заново — с новой версией, иначе собственная свежая сессия отозвала бы
    # сама себя следующим запросом. Сессии на других устройствах отзываются
    # немедленно (см. auth.resolve_auth), а не «протухают сами до 7 дней».
    response = JSONResponse({"ok": True, "note": "Пароль изменён"})
    auth.set_session(response, ctx.user.id, ctx.org.id, new_version)
    return response


class AccountDeleteIn(BaseModel):
    password: str = Field(default="")
    confirm: str = Field(default="")
    # Что делать с организацией, если в ней есть другие участники:
    # transfer — передать её коллеге, org — удалить вместе со всеми данными.
    mode: str = Field(default="")
    transfer_to: str = Field(default="", max_length=255)


def _purge_org(db: Session, org_id: int) -> None:
    """Физически стирает ВСЕ данные организации, включая подключение и токен.

    Порядок — от зависимых таблиц к orgs: в Postgres внешние ключи проверяются
    (в SQLite по умолчанию нет), и обратный порядок упал бы. Список таблиц
    держим полным: осиротевшая строка с org_id удалённой организации — это и
    невыполненное обещание «данные стёрты», и мина под следующий org_id.

    Сам список живёт в `app.tenancy.org_purge_models()` — там же, где сторож,
    который сверяет его с реестром моделей SQLAlchemy. Раньше список был
    литералом здесь, и связи между «что мы удаляем» и «что вообще есть в базе»
    не было никакой: новая модель с org_id, забытая в списке, молча оставляла
    бы осиротевшие строки. Порядок изменился в одном месте, и это не рефакторинг:
    `production_orders` теперь стирается ПЕРЕД `productions`, потому что прежний
    порядок нарушал внешний ключ `production_id` (SEC-9).
    """
    from app import analytics
    from app.tenancy import org_purge_models

    for model in org_purge_models():
        db.execute(delete(model).where(model.org_id == org_id))
    db.execute(delete(Org).where(Org.id == org_id))
    analytics.invalidate(org_id)


def _purge_user(db: Session, user_id: int) -> None:
    """Стирает пользователя и его личные следы (подсказки, уроки, настройки).

    Список — в `app.tenancy.user_purge_models()`, рядом со сторожем полноты.
    Прогресс уроков и личный тумблер подсказок раньше оставались после удаления
    аккаунта: осиротевшие строки с чужим user_id, которые достались бы
    следующему пользователю с тем же идентификатором. Порядок тот же, что был.
    """
    from app.tenancy import user_purge_models

    for model in user_purge_models():
        db.execute(delete(model).where(model.user_id == user_id))
    db.execute(delete(User).where(User.id == user_id))


@app.post("/api/account/delete")
def api_delete_account(
    body: AccountDeleteIn,
    ctx: auth.AuthContext = Depends(auth.require_auth_api),
    db: Session = Depends(get_db),
):
    """Удаление аккаунта. Подтверждение — пароль + слово «УДАЛИТЬ».

    Три разных случая, и они правда разные:
      • участник (не владелец) — удаляем только его: организация и данные
        коллег не трогаются;
      • владелец-единственный участник — удаляем организацию целиком вместе
        с подключением, токеном и всей аналитикой;
      • владелец, у которого есть сотрудники — сам выбирает: передать
        организацию коллеге (данные и доступ коллег остаются) либо удалить
        организацию совсем (тогда коллеги теряют доступ, и их аккаунты, если
        других организаций у них нет, удаляются вместе с ней).
    """
    from app import ms_sync

    if not auth.verify_password(body.password, ctx.user.pw_hash):
        raise HTTPException(
            status_code=403, detail="Пароль не подошёл — удаление отменено. Попробуйте ещё раз."
        )
    if body.confirm.strip().upper() != DELETE_CONFIRM_WORD:
        raise HTTPException(
            status_code=422,
            detail=f"Чтобы подтвердить удаление, введите слово {DELETE_CONFIRM_WORD} "
                   "в поле подтверждения.",
        )

    members = _org_members(db, ctx.org.id)
    others = [(u, m) for u, m in members if u.id != ctx.user.id]
    org_id, user_id = ctx.org.id, ctx.user.id

    # Участник (не владелец): уходит только он.
    if ctx.role != "owner":
        _purge_user(db, user_id)
        db.commit()
        return _deleted_response("account", 0)

    if ms_sync.get_status(org_id).get("state") == "running":
        raise HTTPException(
            status_code=409,
            detail="Сейчас идёт синхронизация с МойСкладом. Подождите пару минут "
                   "и повторите удаление — иначе часть данных успела бы записаться заново.",
        )

    if others:
        if body.mode == "transfer":
            target = None
            wanted = body.transfer_to.strip().lower()
            for u, m in others:
                if u.email == wanted:
                    target = (u, m)
                    break
            if target is None:
                raise HTTPException(
                    status_code=422,
                    detail="Выберите, кому передать организацию — из списка её участников",
                )
            target[1].role = "owner"
            _purge_user(db, user_id)
            db.commit()
            return _deleted_response("account", 0)
        if body.mode != "org":
            raise HTTPException(
                status_code=422,
                detail="В организации есть другие участники. Выберите, что с ней сделать: "
                       "передать коллеге или удалить вместе со всеми данными.",
            )

    # Полное удаление организации (владелец один либо выбрал «удалить всё»).
    orphans = 0
    for u, _m in others:
        other_orgs = db.execute(
            select(func.count()).select_from(Membership).where(
                Membership.user_id == u.id, Membership.org_id != org_id
            )
        ).scalar() or 0
        if not other_orgs:
            _purge_user(db, u.id)
            orphans += 1
    _purge_org(db, org_id)
    _purge_user(db, user_id)
    db.commit()
    return _deleted_response("org", orphans)


def _deleted_response(scope: str, removed_members: int) -> JSONResponse:
    """Ответ об удалении + гашение сессионной куки (входить больше некуда)."""
    response = JSONResponse({
        "ok": True,
        "scope": scope,  # account — ушёл только человек; org — организация целиком
        "removed_members": removed_members,
        "redirect": "/login?deleted=" + ("org" if scope == "org" else "account"),
    })
    auth.clear_session(response)
    auth.clear_embed(response)
    return response


# ── Страницы ─────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def dashboard_page(request: Request, db: Session = Depends(get_db)):
    ctx = auth.resolve_auth(request, db)
    if ctx is None:
        # Неавторизованным показываем публичный лендинг (самодостаточный шаблон).
        return templates.TemplateResponse(request, "landing.html", {})
    if not _has_active_connection(db, ctx.org.id) and not _data_is_loading(ctx.org.id):
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


# Куда уходит участник (не владелец) с owner-only предпросмотра. Безопасный
# отказ, а не 500 и не пустая страница: человек попадает на обычный рабочий
# экран своей организации. Вынесено в константу, потому что на это значение
# опирается тест доступа — иначе адрес отказа жил бы в двух местах.
ONBOARDING_PREVIEW_DENY_REDIRECT = "/turnover"


@app.get("/onboarding/preview", response_class=HTMLResponse)
def onboarding_preview_page(request: Request, db: Session = Depends(get_db)):
    """Owner-only предпросмотр онбординга по выделенному явному адресу.

    Это ВИЗУАЛЬНАЯ ПРИЁМКА, а не онбординг. Отсюда три свойства, и каждое из
    них держится не на вёрстке, а на коде:

    1. **Изолированность.** Ни один автоматический редирект приложения сюда не
       ведёт: `/` уводит на `/onboarding` или `/turnover`, сам `/onboarding`
       не изменён. Попасть на предпросмотр можно только набрав адрес руками —
       поэтому он и не становится онбордингом «для всех подряд».
    2. **Доступ проверяется на сервере.** Спрятать кнопку недостаточно: адрес
       угадывается. Аноним — редирект на `/login`; участник без роли владельца
       — редирект на обычную рабочую страницу. Роль берётся из
       `auth.resolve_auth` (членство в организации проверено там же), а не из
       заголовка и не из параметра запроса.
    3. **Ничего не пишет.** Маршрут только GET, шаблон
       `onboarding_preview.html` самодостаточен и НЕ наследует `base.html`:
       базовый шаблон подключает `_hints.html` и `/static/app.js`, а те сами
       по себе шлют служебные POST (`/api/hints/seen`, `/api/prefs/hints`,
       `/api/lessons/{key}/done`, `/api/sync/run`). На странице приёмки такие
       запросы были бы записью в чужую сессию под видом «просто посмотреть»,
       поэтому базовый шаблон здесь не используется сознательно.

    Проверка `_has_active_connection` тут намеренно ОТСУТСТВУЕТ (в отличие от
    `/onboarding`, который уводит подключённую организацию на `/`): предпросмотр
    показывают владельцу как раз на живой организации, где данные уже есть.
    """
    ctx = auth.resolve_auth(request, db)
    if ctx is None:
        return RedirectResponse("/login", status_code=302)
    if ctx.role != "owner":
        return RedirectResponse(ONBOARDING_PREVIEW_DENY_REDIRECT, status_code=302)
    return _page(request, ctx, "onboarding_preview.html", "onboarding",
                 "Предпросмотр онбординга", db=db)


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
    # Страница «Заказы» убрана: отдельный реестр со статусами путал («швейка так
    # не работает»). Заказы создаются и управляются на странице «Заказ» (блок
    # «Заказы в производстве»); API /api/orders остаётся рабочим.
    raise HTTPException(status_code=404, detail="Раздел отключён")


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)):
    return _authed_page(request, db, "settings.html", "settings", "Настройки")
