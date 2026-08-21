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
        """Контекст «один запрос»: heavy=True — через узкий семафор отчётов."""
        return _Slot(self, self._report_semaphore if heavy else self._semaphore)

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
    def __init__(self, limiter: RateLimiter, semaphore: asyncio.Semaphore) -> None:
        self._limiter = limiter
        self._semaphore = semaphore

    async def __aenter__(self) -> "_Slot":
        await self._semaphore.acquire()
        try:
            await self._limiter._wait_window()
        except BaseException:
            self._semaphore.release()
            raise
        return self

    async def __aexit__(self, *exc) -> None:
        self._semaphore.release()


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
                       json: dict | None = None, heavy: bool = False) -> dict:
        """Запрос с rate-limit'ом и ретраями.

        Повторяем 429, 5xx (500/502/503/504) и транспортные сбои httpx
        (ConnectError, ReadTimeout, RemoteProtocolError); прочие 4xx — сразу
        наружу (401/403 — токен, 412 — валидация: повтор бессмыслен).
        При 429 дополнительно тормозим весь клиент (limiter.cool_down).
        """
        for attempt in range(MAX_RETRIES + 1):
            resp: httpx.Response | None = None
            try:
                async with self._limiter.slot(heavy=heavy):
                    resp = await self._client.request(method, path, params=params, json=json)
            except TRANSPORT_ERRORS:
                self.stats["transport"] += 1
                if attempt >= MAX_RETRIES:
                    raise
            else:
                if resp.status_code not in RETRY_STATUSES:
                    resp.raise_for_status()
                    return resp.json()
                if resp.status_code == 429:
                    self.stats["429"] += 1
                else:
                    self.stats["5xx"] += 1
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

    async def post(self, path: str, json: dict) -> dict:
        """POST (создание сущностей) с тем же rate-limit'ом и ретраями."""
        return await self._request("POST", path, json=json)

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

    # ── Обратная запись (writeback): «Заказ поставщику» ─────────────────────

    async def fetch_counterparties(self) -> list[dict]:
        """Справочник контрагентов — нужен, чтобы у товара знать ИМЯ поставщика
        (в ассортименте приходит только ссылка)."""
        return [row async for row in self.paginate("/entity/counterparty")]

    async def fetch_organizations(self) -> list[dict]:
        """Юрлица аккаунта (entity/organization) — нужны для документов."""
        return [row async for row in self.paginate("/entity/organization")]

    async def find_counterparty_by_name(self, name: str) -> dict | None:
        """Контрагент по точному имени (filter=name=...); None, если не найден.

        В фильтрах МойСклад точное совпадение по name — оператор `=`;
        спецсимволы `;` и `=` в значении экранируются не нужны для наших имён.
        """
        params = {"filter": f"name={name}"}
        async for row in self.paginate("/entity/counterparty", params):
            if (row.get("name") or "").strip() == name:
                return row
        return None

    async def create_counterparty(self, name: str) -> dict:
        """Создаёт контрагента (обязательное поле — только name)."""
        return await self.post("/entity/counterparty", {"name": name})

    async def create_purchase_order(self, payload: dict) -> dict:
        """Создаёт документ «Заказ поставщику» (entity/purchaseorder).

        Обязательные поля payload: organization.meta, agent.meta;
        positions: [{assortment: {meta}, quantity, price(копейки)}].
        Номер (name) МойСклад присвоит сам, если не передан.
        """
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
