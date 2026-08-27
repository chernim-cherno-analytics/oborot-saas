"""Клиент API МойСклад (remap 1.2) с соблюдением лимитов.

Лимиты МойСклад: не более 45 запросов за 3 секунды на пользователя и не более
5 параллельных запросов. При 429 сервер присылает X-Lognex-Retry-TimeInterval
(миллисекунды до снятия ограничения) — ждём и повторяем (до MAX_RETRIES раз,
с экспоненциальным backoff и общим cool-down; также повторяются 5xx и
транспортные сбои — инцидент 21.08).

Базовый URL берётся из env MS_BASE_URL (для тестов подменяется на mock-сервер).

Важные особенности API, учтённые здесь (проверено на реальном бренде, legacy):
- в отчёте /report/stock/all параметр moment работает ТОЛЬКО внутри filter
  (`filter=moment=YYYY-MM-DD 23:59:00;store=<href>`); как отдельный
  query-параметр МойСклад его игнорирует;
- при expand=positions максимальный limit страницы — 100 (иначе positions
  приходят href-ами без строк);
- цены (salePrices[].value, buyPrice.value, price позиций) — в КОПЕЙКАХ.
"""
import asyncio
import os
import random
import time
from collections import deque
from typing import Any, AsyncIterator

import httpx

DEFAULT_BASE_URL = "https://api.moysklad.ru/api/remap/1.2"
WINDOW_SECONDS = 3.0
WINDOW_LIMIT = 45
MAX_PARALLEL = 5
# Инцидент 21.08: первичный синк (365 дат × 3 склада /report/stock/all) трижды
# падал на ~110-й дате с 429 — 5 ретраев с паузой из X-Lognex-Retry-TimeInterval
# не спасали, пять параллельных тяжёлых отчётов выедали лимит быстрее, чем он
# восстанавливался. Поэтому: больше ретраев, экспоненциальный backoff с джиттером,
# общий «cool-down» для всех задач после 429 и отдельный, более узкий семафор
# для тяжёлых отчётов (REPORT_PARALLEL) при общем скользящем окне 45/3 с.


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    """Целое из env с терпимостью к мусору и нижней границей (ревью 21.08)."""
    try:
        value = int(str(os.environ.get(name, default)).strip())
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


MAX_RETRIES = _env_int("MS_MAX_RETRIES", 10, minimum=0)
REPORT_PARALLEL = _env_int("MS_REPORT_PARALLEL", 3, minimum=1)
MAX_BACKOFF_SECONDS = 60.0
COOLDOWN_MAX_SECONDS = 10.0  # cool-down остальных задач после 429 — не дольше этого
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
TRANSPORT_ERRORS = (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError)
PAGE_LIMIT = 1000
EXPAND_PAGE_LIMIT = 100  # с expand МойСклад отдаёт максимум 100 строк на страницу

# Потолок точного перебора при поиске по syncId (см. find_by_sync_id).
# Существует не ради экономии, а ради честности: перебор без границы либо
# когда-нибудь встанет навсегда, либо будет прерван по таймауту где-то в
# середине — и тогда «не нашли» окажется неправдой в самую опасную сторону.
# Исчерпанная граница — это «не знаю», и она поднимает исключение, а не
# возвращает пустой список.
SYNC_ID_SCAN_LIMIT = _env_int("MS_SYNCID_SCAN_LIMIT", 20000, minimum=1)

# Коды, после которых точечная ПОДСКАЗКА считается непригодной, а поиск
# продолжается полным перебором.
#
#   404/405/410 — «нет такого объекта или маршрута»;
#   412         — маршрут недокументирован, и на валидном UUID такой ответ
#                 означает «этот маршрут так не умеет». Ревью Codex, P2
#                 (discussion_r3856243671): раньше 412 пробрасывался наружу, и
#                 необязательная оптимизация становилась ОБЯЗАТЕЛЬНОЙ — при
#                 таком ответе живого аккаунта отправка падала бы целиком.
#                 Ровно это уже случилось в раунде 7 с `filter=syncId`, и
#                 повторять ту же ошибку вторым способом незачем.
#
# Глотать эти коды безопасно ровно потому, что подсказка после раунда 8 ничего
# не решает: вердикт выносит полный ограниченный перебор, и он выполняется в
# любом случае.
#
# Всё остальное остаётся fail-closed и летит наружу: 401/403 — «нет доступа»,
# 429 — «слишком часто», 5xx и транспорт — «не дошло». Ни одно из них не
# говорит «маршрут так не умеет», и превращать их в молчаливое «подсказки нет»
# значило бы прятать настоящий сбой доступа.
POINT_LOOKUP_UNUSABLE = frozenset({404, 405, 410, 412})


class SyncIdLookupUnavailable(Exception):
    """Достоверно ответить «есть или нет такой syncId» не удалось.

    Отдельный тип, а не пустой результат, ровно потому, что пустой результат
    здесь читается как разрешение создать документ заново. «Не знаю» и «нет» —
    разные ответы, и цена их смешения — второй заказ поставщику у клиента.
    """

    def __init__(self, entity: str, sync_id: str, limit: int,
                 reason: str = "") -> None:
        super().__init__(
            f"{entity}: {reason}" if reason else
            f"{entity}: просмотрено больше {limit} записей, а ключ {sync_id} "
            f"так и не найден — ответ недостоверен"
        )
        self.entity, self.sync_id, self.limit = entity, sync_id, limit
        self.reason = reason


class SyncIdNotUnique(Exception):
    """Один `syncId` — несколько сущностей: контракт уникальности нарушен."""

    def __init__(self, entity: str, sync_id: str, count: int) -> None:
        super().__init__(f"{entity}: {count} сущностей с одним syncId {sync_id}")
        self.entity, self.sync_id, self.count = entity, sync_id, count


def base_url() -> str:
    """Базовый URL API: env MS_BASE_URL (тесты) или боевой адрес МойСклад."""
    return os.environ.get("MS_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


# Обратная совместимость со старым импортом (значение вычислено на момент импорта).
BASE_URL = base_url()


class RateLimiter:
    """Скользящее окно (45 запросов / 3 с) + два семафора параллельности.

    Окно ОДНО на клиент; семафоров два: обычный (MAX_PARALLEL) и «тяжёлый»
    (REPORT_PARALLEL) для /report/stock/all — инцидент 21.08. После 429 любой
    задачи выставляется pause_until: до этого момента новые запросы из окна
    не выпускаются, чтобы соседние задачи не добивали и без того закрытый лимит.

    У потолков общий бюджет (OPS-6): heavy-слот держит ОБА разрешения сразу —
    общее и узкое, — а не только узкое. Иначе normal (до MAX_PARALLEL) и heavy
    (до REPORT_PARALLEL) независимы и суммарно могут идти одновременно до
    MAX_PARALLEL + REPORT_PARALLEL запросов, превышая лимит МойСклад на
    параллельность.

    Порядок захвата в `_Slot` — узкое, потом общее (round 2, OPS-6 corrective,
    discussion_r3868519391). Первая редакция брала их в обратном порядке
    (общее, потом узкое) и из-за этого резервировала общий permit ещё ДО того,
    как задача могла пройти узкие отчётные ворота: насыщенная очередь heavy
    (например 5 heavy при parallel=5/report=3) занимала ВСЕ общие permit'ы, а
    реально проходили узкие ворота только 3 — normal при этом не получал ни
    одного места, хотя фактическая параллельность была равна REPORT_PARALLEL.
    Узкое разрешение само по себе точный потолок: держать его может не больше
    REPORT_PARALLEL задач одновременно, поэтому запрашивать общий permit имеет
    смысл только ПОСЛЕ того, как узкие ворота гарантированно пройдены — тогда
    normal всегда может использовать оставшиеся MAX_PARALLEL-REPORT_PARALLEL
    общих permit'ов. Normal узкое разрешение никогда не держит, поэтому во всём
    коде остаётся ровно один порядок захвата и циклического ожидания не
    возникает.
    """

    def __init__(self, limit: int = WINDOW_LIMIT, window: float = WINDOW_SECONDS,
                 parallel: int = MAX_PARALLEL,
                 report_parallel: int | None = None) -> None:
        self._limit = limit
        self._window = window
        self._stamps: deque[float] = deque()
        self._lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(parallel)
        self._report_semaphore = asyncio.Semaphore(
            min(parallel, report_parallel or REPORT_PARALLEL))
        self.pause_until = 0.0  # monotonic; общий cool-down после 429

    def slot(self, heavy: bool = False) -> "_Slot":
        """Контекст «один запрос».

        heavy=True держит ОБА разрешения: общее (доля в MAX_PARALLEL) и узкое
        отчётное (доля в REPORT_PARALLEL) — см. докстринг класса (OPS-6).
        """
        if heavy:
            return _Slot(self, self._semaphore, self._report_semaphore)
        return _Slot(self, self._semaphore)

    def cool_down(self, seconds: float) -> None:
        """Притормозить ВСЕ задачи клиента (зовётся при 429)."""
        until = time.monotonic() + min(seconds, COOLDOWN_MAX_SECONDS)
        if until > self.pause_until:
            self.pause_until = until

    async def _wait_window(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                if self.pause_until > now:
                    await asyncio.sleep(self.pause_until - now)
                    continue
                while self._stamps and now - self._stamps[0] > self._window:
                    self._stamps.popleft()
                if len(self._stamps) < self._limit:
                    self._stamps.append(now)
                    return
                await asyncio.sleep(self._window - (now - self._stamps[0]) + 0.01)

    # Обратная совместимость: `async with limiter:` — обычный слот.
    async def __aenter__(self) -> "RateLimiter":
        await self._semaphore.acquire()
        try:
            await self._wait_window()
        except BaseException:
            self._semaphore.release()
            raise
        return self

    async def __aexit__(self, *exc) -> None:
        self._semaphore.release()


class _Slot:
    """Один слот запроса: 1-2 разрешения (общее и, для heavy, ещё узкое).

    Порядок захвата для heavy — ВСЕГДА узкое, потом общее (round 2, OPS-6
    corrective, discussion_r3868519391); освобождение — в обратном порядке.
    Normal берёт только общее и узкое не держит никогда, поэтому во всём коде
    остаётся ровно один порядок захвата и цикла ожидания между normal и heavy
    не возникает. Захват общего РАНЬШЕ узкого (round 1) резервировал общий
    permit ещё до того, как задача могла пройти узкие ворота, и насыщенная
    heavy-очередь полностью останавливала normal — см. докстринг
    `RateLimiter`.

    Владение каждым разрешением отслеживается отдельным флагом
    (`_acquired`/`_extra_acquired`) и сбрасывается СРАЗУ после каждого
    release — как в штатном `__aexit__`, так и на любой ветке отмены/ошибки
    (round 2, discussion_r3868519606). Иначе повторное использование одного
    и того же объекта `_Slot` (второй `async with` на нём же) после отмены на
    следующем acquire освободило бы permit, которым эта попытка не владела,
    и раздуло бы семафор выше настоящей ёмкости.
    """

    def __init__(self, limiter: RateLimiter, semaphore: asyncio.Semaphore,
                 extra_semaphore: asyncio.Semaphore | None = None) -> None:
        self._limiter = limiter
        self._semaphore = semaphore
        self._extra_semaphore = extra_semaphore
        self._acquired = False
        self._extra_acquired = False

    async def __aenter__(self) -> "_Slot":
        if self._extra_semaphore is not None:
            await self._extra_semaphore.acquire()
            self._extra_acquired = True
        try:
            await self._semaphore.acquire()
            self._acquired = True
            try:
                await self._limiter._wait_window()
            except BaseException:
                self._semaphore.release()
                self._acquired = False
                raise
        except BaseException:
            # Отмена или падение между захватом узкого и общего разрешения
            # (или на ожидании окна) не должны оставлять узкое захваченным
            # навсегда — иначе это утечка permit и постепенный deadlock для
            # других heavy-запросов.
            if self._extra_acquired:
                self._extra_semaphore.release()
                self._extra_acquired = False
            raise
        return self

    async def __aexit__(self, *exc) -> None:
        if self._acquired:
            self._semaphore.release()
            self._acquired = False
        if self._extra_acquired:
            self._extra_semaphore.release()
            self._extra_acquired = False


class MoySkladClient:
    """Асинхронный клиент МойСклад: пагинация, ретраи по 429, Bearer-токен."""

    def __init__(self, token: str, base: str | None = None) -> None:
        self._base_url = (base or base_url()).rstrip("/")
        self._limiter = RateLimiter()
        # Счётчики для диагностики и для синка (пауза между чанками после 429).
        self.stats: dict[str, int] = {"429": 0, "5xx": 0, "transport": 0, "retries": 0}
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept-Encoding": "gzip",
                "Accept": "application/json;charset=utf-8",
            },
            timeout=90.0,
        )

    @property
    def api_base(self) -> str:
        return self._base_url

    def store_href(self, store_ext_id: str) -> str:
        """href склада для фильтров отчётов и сопоставления документов."""
        return f"{self._base_url}/entity/store/{store_ext_id}"

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "MoySkladClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    @staticmethod
    def _retry_delay(attempt: int, resp: httpx.Response | None) -> float:
        """Пауза перед повтором (инцидент 21.08, ревью 21.08).

        Первый повтор — строго по X-Lognex-Retry-TimeInterval (мс), если он есть
        и вменяем. Дальше пауза не меньше экспоненты 1,2,4,… — иначе при
        постоянном «подождите 200 мс» десять ретраев сгорают за 2 секунды и
        ничего не лечат. Джиттер ±20 % накладывается ДО потолка MAX_BACKOFF.
        """
        header: float | None = None
        if resp is not None:
            hdr = resp.headers.get("X-Lognex-Retry-TimeInterval", "")
            if hdr.isdigit() and 0 < int(hdr) <= MAX_BACKOFF_SECONDS * 1000:
                header = int(hdr) / 1000.0
        expo = float(2 ** min(attempt, 10))
        if header is None:
            base = expo
        elif attempt == 0:
            base = header
        else:
            base = max(header, expo)
        return min(MAX_BACKOFF_SECONDS, base * random.uniform(0.8, 1.2))

    async def _request(self, method: str, path: str, params: dict | None = None,
                       json: dict | None = None, heavy: bool = False,
                       retry_unsafe: bool = True) -> dict:
        """Запрос с rate-limit'ом и ретраями.

        Повторяем 429, 5xx (500/502/503/504) и транспортные сбои httpx
        (ConnectError, ReadTimeout, RemoteProtocolError); прочие 4xx — сразу
        наружу (401/403 — токен, 412 — валидация: повтор бессмыслен).
        При 429 дополнительно тормозим весь клиент (limiter.cool_down).

        `retry_unsafe=False` — повторяем ТОЛЬКО 429. Это режим для запросов,
        которые СОЗДАЮТ документы. Разница принципиальная: 429 означает «мы
        даже не начали, приходите позже» — повтор безопасен. А таймаут чтения
        или 502 означают «ответ не дошёл», и создан документ или нет — из
        нашей позиции неизвестно. Автоматический повтор в такой ситуации
        создаёт ВТОРОЙ заказ поставщику у клиента в МойСкладе, и заметить это
        может только он сам. Ключа идемпотентности у JSON API 1.2 нет,
        поэтому единственная защита — не повторять вслепую: решение
        принимает вызывающий код, который может сначала поискать документ
        (см. ms_writeback.find_existing_order).
        """
        for attempt in range(MAX_RETRIES + 1):
            resp: httpx.Response | None = None
            try:
                async with self._limiter.slot(heavy=heavy):
                    resp = await self._client.request(method, path, params=params, json=json)
            except TRANSPORT_ERRORS:
                self.stats["transport"] += 1
                if attempt >= MAX_RETRIES or not retry_unsafe:
                    raise
            else:
                if resp.status_code not in RETRY_STATUSES:
                    resp.raise_for_status()
                    return resp.json()
                if resp.status_code == 429:
                    self.stats["429"] += 1
                else:
                    self.stats["5xx"] += 1
                    if not retry_unsafe:
                        resp.raise_for_status()
                if attempt >= MAX_RETRIES:
                    resp.raise_for_status()
            delay = self._retry_delay(attempt, resp)
            if resp is not None and resp.status_code == 429:
                self._limiter.cool_down(delay)
            self.stats["retries"] += 1
            await asyncio.sleep(delay)
        raise RuntimeError("unreachable")

    async def get(self, path: str, params: dict | None = None, *,
                  heavy: bool = False) -> dict:
        """GET с rate-limit'ом и ретраями (heavy — через узкий семафор отчётов)."""
        return await self._request("GET", path, params=params, heavy=heavy)

    async def post(self, path: str, json: dict, *, retry_unsafe: bool = False) -> dict:
        """POST (создание сущностей).

        По умолчанию повторяем ТОЛЬКО 429 — см. докстринг `_request`: слепой
        повтор после таймаута создаёт второй документ в учёте клиента.
        `retry_unsafe=True` оставлен для случаев, где дубль безвреден.
        """
        return await self._request("POST", path, json=json, retry_unsafe=retry_unsafe)

    async def paginate(self, path: str, params: dict | None = None,
                       page_limit: int = PAGE_LIMIT, *,
                       heavy: bool = False) -> AsyncIterator[dict]:
        """Итерация по всем строкам списочного эндпоинта (limit/offset)."""
        offset = 0
        while True:
            page_params = dict(params or {})
            page_params.update({"limit": page_limit, "offset": offset})
            data = await self.get(path, page_params, heavy=heavy)
            rows: list[dict] = data.get("rows", [])
            for row in rows:
                yield row
            offset += len(rows)
            meta_size = data.get("meta", {}).get("size", 0)
            if not rows or (offset >= meta_size and len(rows) < page_limit):
                return

    # ── Типовые выборки для синка ────────────────────────────────────────────

    async def check_token(self) -> bool:
        """Проверка валидности токена лёгким запросом контекста сотрудника."""
        try:
            await self.get("/context/employee")
            return True
        except httpx.HTTPStatusError:
            return False

    async def fetch_stores(self) -> list[dict]:
        """Справочник складов."""
        return [row async for row in self.paginate("/entity/store")]

    async def fetch_assortment(self) -> list[dict]:
        """Ассортимент (товары/модификации) с ценами."""
        return [row async for row in self.paginate("/entity/assortment")]

    async def fetch_stock_on(self, day_iso: str, store_ext_id: str) -> list[dict]:
        """Остатки по складу на КОНЕЦ дня day_iso из /report/stock/all.

        КРИТИЧНО (из legacy rebuild_history): moment передаётся внутри filter —
        как отдельный query-параметр МойСклад его молча игнорирует и отдаёт
        остатки на сейчас.
        """
        flt = f"moment={day_iso} 23:59:00;store={self.store_href(store_ext_id)}"
        params = {"filter": flt, "groupBy": "variant"}
        # heavy: тяжёлый отчёт идёт через узкий семафор (инцидент 21.08).
        return [row async for row in self.paginate("/report/stock/all", params, heavy=True)]

    async def fetch_documents(self, entity: str, moment_from: str,
                              moment_to: str | None = None) -> list[dict]:
        """Документы (demand | retaildemand | salesreturn) с позициями.

        filter по moment; expand=positions ⇒ страница не больше 100 строк.
        """
        flt = f"moment>={moment_from} 00:00:00"
        if moment_to:
            flt += f";moment<={moment_to} 23:59:59"
        params: dict[str, Any] = {"filter": flt, "expand": "positions"}
        return [
            row
            async for row in self.paginate(
                f"/entity/{entity}", params, page_limit=EXPAND_PAGE_LIMIT
            )
        ]

    async def fetch_positions(self, entity: str, doc_id: str) -> list[dict]:
        """Все позиции ОДНОГО документа постранично (/entity/{e}/{id}/positions).

        Аудит 18.08: expand=positions вкладывает в документ не более ~100
        строк — хвост длинных документов (заказ на производство 30 моделей ×
        5 размеров) молча терялся. Позиции здесь читаются без expand: код
        синка использует только meta.href ассортимента, quantity/shipped/
        price/discount — всё это есть в дефолтном ответе.
        """
        return [row async for row in self.paginate(f"/entity/{entity}/{doc_id}/positions")]

    async def search_purchase_orders(self, moment_from: str) -> list[dict]:
        """«Заказы поставщику» с даты, БЕЗ позиций — для поиска своего документа.

        Отдельный метод, потому что задача другая: не «сколько чего едет», а
        «не создали ли мы уже этот документ». Без expand=positions страница
        вмещает 1000 строк вместо 100, и запрос дешевле в десять раз.
        """
        params: dict[str, Any] = {"filter": f"moment>={moment_from} 00:00:00"}
        return [row async for row in self.paginate("/entity/purchaseorder", params)]

    async def fetch_purchase_orders(self, moment_from: str) -> list[dict]:
        """«Заказы поставщику» (entity/purchaseorder) с позициями с даты moment_from.

        Для расчёта «едет к нам»: у позиций purchaseorder МойСклад отдаёт
        quantity (заказано) и shipped (принято по привязанным приёмкам) —
        остаток «в пути» = quantity − shipped. expand=positions ⇒ страница
        не больше 100 строк (как у документов продаж).
        """
        params: dict[str, Any] = {
            "filter": f"moment>={moment_from} 00:00:00",
            "expand": "positions",
        }
        return [
            row
            async for row in self.paginate(
                "/entity/purchaseorder", params, page_limit=EXPAND_PAGE_LIMIT
            )
        ]

    async def fetch_purchase_order_by_id(self, doc_id: str) -> dict | None:
        """Точечный GET ОДНОГО «Заказа поставщику» по id (DATA-6, round 2).

        Не список и не поиск — прямое чтение по первичному ключу. Нужен
        ровно там, где список ещё не увидел документ, но сам документ уже
        существует: `entity/purchaseorder` (список) может отставать от
        только что созданного документа (см. `po_hide_created` в
        tests/mock_ms.py — задержка видимости индекса, а не выдумка), а
        точечный GET по id такому отставанию в реальном МойСкладе не
        подвержен — это не поиск по индексу, а чтение по ключу.

        Контракт ответов скопирован с уже существующего `entity_exists()`
        (тот же файл) — тот же класс задачи: «сущность закреплена ссылкой,
        удалили ли её на самом деле»:

          • 404/410 — документа действительно нет. Это единственный ответ,
            по которому можно честно исключить его вклад;
          • всё остальное (429, 5xx, транспорт, 401/403) — наружу
            исключением. Транзиентный сбой ТОЧЕЧНОГО чтения не означает
            «документа нет» — обнулять уже подтверждённый локальный вклад
            push'а по «не знаю» нельзя (см. entity_exists для того же
            рассуждения на примере контрагента).
        """
        try:
            return await self.get(f"/entity/purchaseorder/{doc_id}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (404, 410):
                return None
            raise

    # ── Обратная запись (writeback): «Заказ поставщику» ─────────────────────

    async def fetch_counterparties(self) -> list[dict]:
        """Справочник контрагентов — нужен, чтобы у товара знать ИМЯ поставщика
        (в ассортименте приходит только ссылка)."""
        return [row async for row in self.paginate("/entity/counterparty")]

    async def fetch_organizations(self) -> list[dict]:
        """Юрлица аккаунта (entity/organization) — нужны для документов."""
        return [row async for row in self.paginate("/entity/organization")]

    async def find_counterparties_by_name(self, name: str) -> list[dict]:
        """ВСЕ контрагенты с точным именем (filter=name=...).

        Возвращается список, а не первый попавшийся, намеренно: заказ
        поставщику — финансовый документ, и при двух одноимённых агентах
        выбор «любого» означает отправить его наугад. Решение (связать,
        создать или отказаться) принимает вызывающий код.

        В фильтрах МойСклад точное совпадение по name — оператор `=`;
        спецсимволы `;` и `=` в значении экранировать не нужно для наших имён.
        """
        params = {"filter": f"name={name}"}
        return [row async for row in self.paginate("/entity/counterparty", params)
                if (row.get("name") or "").strip() == name]

    async def sync_id_point_lookup(self, entity: str, sync_id: str) -> dict | None:
        """Точечный запрос `/entity/{type}/syncid/{id}` — НЕОБЯЗАТЕЛЬНАЯ подсказка.

        Не доказательство. Ни отсутствия, ни — что важнее — единственности.

        Официальная документация (`md/_general.md`, «Назначение поля syncId»)
        описывает URL такого вида для УДАЛЕНИЯ сущности; поддержка `GET` по
        нему нигде не описана. Значит, 404 отсюда может означать и «сущности
        нет», и «такого маршрута нет вовсе».

        Но и положительный ответ доказывает меньше, чем кажется, и на этом
        ревью поймало ошибку в прежней редакции. Если ключ по какой-то причине
        занят ДВУМЯ объектами, точечный маршрут вернёт один из них — и, будь
        его ответ терминальным, второй не увидел бы никто: ни проверка дублей
        в клиенте, ни `AmbiguousExistingOrder`, ни живой гейт. Поэтому
        результат отсюда лишь ДОПОЛНЯЕТ полный перебор, а вердикт выносит
        перебор (см. find_by_sync_id).

        Положительный ответ проверяется строго — дословный `syncId`, ожидаемый
        `meta.type` и непустой `id`. Не сошлось — подсказка не используется
        вовсе; отбросить её безопасно ровно потому, что вердикт даёт не она.

        Непригодной подсказка считается при 404/405/410/412 — «нет такого
        объекта или маршрута» и «маршрут так не умеет». В этих случаях
        возвращается None, и поиск продолжается полным перебором: подсказка
        необязательна по построению, и её отсутствие ничего не решает.

        Всё остальное (401/403/429/5xx, транспорт) летит наружу и остаётся
        fail-closed: «нет доступа» и «слишком часто» — это не «маршрут так не
        умеет», и прятать такой сбой за молчаливым «подсказки нет» нельзя.
        """
        if not sync_id:
            return None
        try:
            row = await self.get(f"/entity/{entity}/syncid/{sync_id}")
        except httpx.HTTPStatusError as exc:
            resp = getattr(exc, "response", None)
            if resp is not None and resp.status_code in POINT_LOOKUP_UNUSABLE:
                return None
            raise
        if not isinstance(row, dict):
            return None
        if str(row.get("syncId") or "") != sync_id:
            return None
        if str(((row.get("meta") or {}).get("type")) or "") != entity:
            return None
        if not str(row.get("id") or ""):
            return None
        return row

    async def find_by_sync_id(self, entity: str, sync_id: str) -> list[dict]:
        """ВСЕ сущности с нашим ключом идемпотентности — без `filter=syncId`.

        Почему не фильтром. Документация обещает фильтрацию по `syncId`
        (в таблицах полей контрагента и заказа поставщику у него стоят
        операторы `=` и `!=`), но живой аккаунт 25.08.2026 ответил
        **HTTP 412, code 1034 «неизвестное поле фильтрации syncId»**. Между
        обещанием документа и наблюдаемым фактом побеждает факт: раньше на
        этом фильтре держались и поиск своего документа, и поиск контрагента,
        то есть отправка заказа на боевом API падала целиком.

        Что осталось достоверного и на чём стоит этот метод: `syncId`
        присутствует в полях самой сущности и приходит в выдаче списка. Значит
        точное сравнение можно сделать у себя — это не зависит ни от какого
        необязательного умения чужого API.

        Порядок, и он важнее, чем кажется:

          1) точечный запрос — НЕОБЯЗАТЕЛЬНАЯ подсказка. Он НЕ прекращает
             работу метода;
          2) полный ограниченный перебор страниц со сравнением в Python —
             выполняется ВСЕГДА и именно он выносит вердикт;
          3) подсказка и находки перебора склеиваются по `id` сущности.

        Почему шага «нашли точечно — на этом всё» здесь больше нет (ревью
        Codex, P1, discussion_r3855902789). Метод обещает ВСЕ совпадения, и на
        этом обещании стоит вся проверка дублей: `find_counterparty_by_sync_id`
        смотрит `len(rows) > 1`, `find_own_document` — `len(docs) > 1`, живой
        гейт после повторного POST — «ровно один». Терминальный ответ из
        точечного запроса делал все три недостижимыми ровно тогда, когда
        недокументированный маршрут отвечает 200: два объекта с одним ключом
        он вернул бы как один, и живой сценарий объявил бы «второго документа
        нет», не просмотрев коллекцию вообще. То есть ровно то свойство,
        которое живой тест и обязан доказывать, доказывалось бы само собой.

        Почему подсказку всё же не выбросили. Перебор — единственный
        достоверный источник, но его полнота опирается на предположение, что
        нужная сущность вообще попадает в выдачу списка (архив, корзина и иные
        режимы живьём не проверялись). Лишняя находка ведёт к отказу по дублю,
        пропущенная — к созданию второго финансового документа; из двух ошибок
        выбрана та, что останавливает. Ложного дубля подсказка дать не может:
        склейка идёт по `id`.

        Перебор идёт ДО КОНЦА, а не до первого совпадения: контракт обещает
        уникальность ключа, и два объекта с одним `syncId` означают, что
        обещание нарушено. Узнать об этом обязаны мы, а не выбрать один из
        двух молча.

        Граница перебора явная. Исчерпали её — мы НЕ знаем ответа, и метод
        поднимает `SyncIdLookupUnavailable` вместо пустого списка. Пустой
        список здесь означал бы «в аккаунте такого нет», то есть разрешение
        создать документ заново; вернуть его, не досмотрев, — это ровно тот
        дубль, ради недопущения которого весь механизм и написан.
        """
        if not sync_id:
            return []

        matches: list[dict] = []
        seen_ids: set[str] = set()

        hint = await self.sync_id_point_lookup(entity, sync_id)
        if hint is not None:
            matches.append(hint)
            seen_ids.add(str(hint.get("id") or ""))

        scanned = 0
        async for row in self.paginate(f"/entity/{entity}"):
            scanned += 1
            if scanned > SYNC_ID_SCAN_LIMIT:
                raise SyncIdLookupUnavailable(entity, sync_id, SYNC_ID_SCAN_LIMIT)
            if str(row.get("syncId") or "") != sync_id:
                continue
            row_id = str(row.get("id") or "")
            if not row_id:
                # Совпало по ключу, но склеить не с чем: без `id` мы не отличим
                # «тот же объект» от «второго такого же». Это «не знаю», а не
                # находка, и молча включать её в результат нельзя.
                raise SyncIdLookupUnavailable(
                    entity, sync_id, SYNC_ID_SCAN_LIMIT,
                    reason="в выдаче есть совпадение по ключу без id — "
                           "отличить дубль от того же объекта нечем")
            if row_id in seen_ids:
                continue
            seen_ids.add(row_id)
            matches.append(row)
        return matches

    async def find_counterparty_by_sync_id(self, sync_id: str) -> dict | None:
        """Контрагент по НАШЕМУ ключу идемпотентности.

        Нужен ровно для одного случая: контрагента мы создали, а ответ до нас
        не дошёл. Искать его по имени нельзя — одноимённых может быть много;
        `syncId` же уникален в аккаунте, поэтому находка здесь однозначна.

        Больше одного совпадения — нарушенный контракт уникальности, и тихо
        брать первый нельзя: финансовый документ уйдёт произвольному
        подрядчику. Здесь это ошибка, а не выбор.
        """
        rows = await self.find_by_sync_id("counterparty", sync_id)
        if len(rows) > 1:
            raise SyncIdNotUnique("counterparty", sync_id, len(rows))
        return rows[0] if rows else None

    async def entity_exists(self, href: str) -> bool:
        """Жива ли сущность по её href: True — есть, False — её больше нет.

        Нужно ровно для закреплённых ссылок: контрагента, выбранного один раз
        и записанного к нам в базу, в МойСкладе могли удалить. Тогда каждый
        следующий POST документа падает валидацией «контрагент не найден», а
        ссылка у нас остаётся прежней — и повторы не лечатся НИКОГДА, даже
        если подходящий контрагент в аккаунте есть.

        Граница ответов проведена намеренно узко:

          • 404/410 — сущности нет. Это единственный ответ, по которому мы
            вправе забыть закреплённую ссылку;
          • всё остальное (429, 5xx, таймаут, обрыв, 401/403) — наружу
            исключением. Транзиентный сбой НЕ означает «удалено»: если по
            каждому такому ответу сбрасывать привязку, выпадение сети заводило
            бы клиенту второго контрагента «Производство» — то есть чинили бы
            мы дубликат документа, а создавали дубликат подрядчика.

        Чужой хост не запрашивается: href обязан начинаться с базового адреса
        нашего же API. Заголовок с токеном уходит только туда, куда мы сами
        ходим, а неизвестный адрес считается несуществующей ссылкой — по нему
        всё равно нельзя оформить документ.
        """
        if not href or not href.startswith(f"{self._base_url}/"):
            return False
        try:
            await self.get(href[len(self._base_url):])
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (404, 410):
                return False
            raise
        return True

    async def create_counterparty(self, name: str, sync_id: str) -> dict:
        """Создаёт контрагента идемпотентно: name + наш syncId.

        syncId обязателен и здесь: без него два одновременных клика по
        «Отправить в МойСклад» заводят клиенту ДВУХ агентов «Производство»,
        и половина заказов уезжает не на того.
        """
        if not sync_id:
            raise ValueError("create_counterparty без syncId запрещён")
        return await self.post("/entity/counterparty",
                               {"name": name, "syncId": sync_id})

    async def find_purchase_orders_by_sync_id(self, sync_id: str) -> list[dict]:
        """«Заказы поставщику» с нашим ключом идемпотентности.

        Точный и НЕ зависящий от человека способ узнать, создан ли уже наш
        документ: `syncId` живёт в служебном поле, а не в описании, которое
        владелец может переписать или скопировать в другой документ.

        Список, а не одна строка: по контракту JSON API 1.2 `syncId` уникален,
        и два ответа означали бы, что реальность разошлась с контрактом.
        Разбираться в этом молча (взяв первый) здесь нельзя.

        Как именно ищем — см. find_by_sync_id: `filter=syncId` живой API не
        принимает, поэтому точность обеспечивается сравнением у себя, а не
        обещанием чужого фильтра.
        """
        return await self.find_by_sync_id("purchaseorder", sync_id)

    async def create_purchase_order(self, payload: dict) -> dict:
        """Создаёт документ «Заказ поставщику» (entity/purchaseorder).

        Обязательные поля payload: organization.meta, agent.meta, syncId;
        positions: [{assortment: {meta}, quantity, price(копейки)}].
        Номер (name) МойСклад присвоит сам, если не передан.

        syncId проверяется ЗДЕСЬ и без «мягкой» ветки. Это единственный
        механизм, который делает создание финансового документа безопасным
        при потерянном ответе: повторный POST с занятым syncId обновляет уже
        созданный документ, а не заводит второй. Молчаливый фолбэк «нет
        ключа — отправим динамически» вернул бы ровно тот дубль, ради
        которого всё и делается, причём незаметно для нас и для клиента.
        """
        if not str(payload.get("syncId") or ""):
            raise ValueError(
                "create_purchase_order без syncId запрещён: ключ идемпотентности "
                "обязан быть создан и закоммичен ДО сетевого вызова"
            )
        return await self.post("/entity/purchaseorder", payload)

    # Старые имена (каркас демо-скоупа) — оставлены для совместимости.

    async def fetch_stock_by_store(self) -> list[dict]:
        """Отчёт «Остатки по складам» (текущий момент)."""
        return [row async for row in self.paginate("/report/stock/bystore")]

    async def fetch_demand_positions(self, updated_from: str | None = None) -> list[dict]:
        """Отгрузки (продажи); фильтр по moment для инкрементального синка."""
        params: dict[str, Any] = {"expand": "positions"}
        if updated_from:
            params["filter"] = f"moment>={updated_from}"
        return [row async for row in self.paginate("/entity/demand", params,
                                                   page_limit=EXPAND_PAGE_LIMIT)]

    async def fetch_sales_returns(self, updated_from: str | None = None) -> list[dict]:
        """Возвраты покупателей."""
        params: dict[str, Any] = {"expand": "positions"}
        if updated_from:
            params["filter"] = f"moment>={updated_from}"
        return [row async for row in self.paginate("/entity/salesreturn", params,
                                                   page_limit=EXPAND_PAGE_LIMIT)]
