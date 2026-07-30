"""Клиент API МойСклад (remap 1.2) с соблюдением лимитов.

Лимиты МойСклад: не более 45 запросов за 3 секунды на пользователя и не более
5 параллельных запросов. При 429 сервер присылает X-Lognex-Retry-TimeInterval
(миллисекунды до снятия ограничения) — ждём и повторяем.

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
import time
from collections import deque
from typing import Any, AsyncIterator

import httpx

DEFAULT_BASE_URL = "https://api.moysklad.ru/api/remap/1.2"
WINDOW_SECONDS = 3.0
WINDOW_LIMIT = 45
MAX_PARALLEL = 5
MAX_RETRIES = 5
PAGE_LIMIT = 1000
EXPAND_PAGE_LIMIT = 100  # с expand МойСклад отдаёт максимум 100 строк на страницу


def base_url() -> str:
    """Базовый URL API: env MS_BASE_URL (тесты) или боевой адрес МойСклад."""
    return os.environ.get("MS_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


# Обратная совместимость со старым импортом (значение вычислено на момент импорта).
BASE_URL = base_url()


class RateLimiter:
    """Скользящее окно (45 запросов / 3 с) + ограничение параллельности (5)."""

    def __init__(self, limit: int = WINDOW_LIMIT, window: float = WINDOW_SECONDS,
                 parallel: int = MAX_PARALLEL) -> None:
        self._limit = limit
        self._window = window
        self._stamps: deque[float] = deque()
        self._lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(parallel)

    async def __aenter__(self) -> "RateLimiter":
        await self._semaphore.acquire()
        async with self._lock:
            while True:
                now = time.monotonic()
                while self._stamps and now - self._stamps[0] > self._window:
                    self._stamps.popleft()
                if len(self._stamps) < self._limit:
                    self._stamps.append(now)
                    return self
                await asyncio.sleep(self._window - (now - self._stamps[0]) + 0.01)

    async def __aexit__(self, *exc) -> None:
        self._semaphore.release()


class MoySkladClient:
    """Асинхронный клиент МойСклад: пагинация, ретраи по 429, Bearer-токен."""

    def __init__(self, token: str, base: str | None = None) -> None:
        self._base_url = (base or base_url()).rstrip("/")
        self._limiter = RateLimiter()
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

    async def _request(self, method: str, path: str, params: dict | None = None,
                       json: dict | None = None) -> dict:
        """Запрос с rate-limit'ом и ретраями по 429 (X-Lognex-Retry-TimeInterval, мс)."""
        for attempt in range(MAX_RETRIES + 1):
            async with self._limiter:
                resp = await self._client.request(method, path, params=params, json=json)
            if resp.status_code != 429:
                resp.raise_for_status()
                return resp.json()
            if attempt >= MAX_RETRIES:
                resp.raise_for_status()
            retry_ms = resp.headers.get("X-Lognex-Retry-TimeInterval")
            delay = int(retry_ms) / 1000.0 if retry_ms and retry_ms.isdigit() else 1.0
            await asyncio.sleep(delay)
        raise RuntimeError("unreachable")

    async def get(self, path: str, params: dict | None = None) -> dict:
        """GET с rate-limit'ом и ретраями по 429."""
        return await self._request("GET", path, params=params)

    async def post(self, path: str, json: dict) -> dict:
        """POST (создание сущностей) с тем же rate-limit'ом и ретраями по 429."""
        return await self._request("POST", path, json=json)

    async def paginate(self, path: str, params: dict | None = None,
                       page_limit: int = PAGE_LIMIT) -> AsyncIterator[dict]:
        """Итерация по всем строкам списочного эндпоинта (limit/offset)."""
        offset = 0
        while True:
            page_params = dict(params or {})
            page_params.update({"limit": page_limit, "offset": offset})
            data = await self.get(path, page_params)
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
        return [row async for row in self.paginate("/report/stock/all", params)]

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
