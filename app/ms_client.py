"""Клиент API МойСклад (remap 1.2) с соблюдением лимитов.

Лимиты МойСклад: не более 45 запросов за 3 секунды на пользователя и не более
5 параллельных запросов. При 429 сервер присылает X-Lognex-Retry-TimeInterval
(миллисекунды до снятия ограничения) — ждём и повторяем.

В демо-скоупе реальная синхронизация не запускается; клиент — рабочий каркас
для боевого синка (ассортимент, остатки по складам, отгрузки/возвраты).
"""
import asyncio
import time
from collections import deque
from typing import Any, AsyncIterator

import httpx

BASE_URL = "https://api.moysklad.ru/api/remap/1.2"
WINDOW_SECONDS = 3.0
WINDOW_LIMIT = 45
MAX_PARALLEL = 5
MAX_RETRIES = 5
PAGE_LIMIT = 1000


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

    def __init__(self, token: str, base_url: str = BASE_URL) -> None:
        self._base_url = base_url
        self._limiter = RateLimiter()
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept-Encoding": "gzip",
                "Accept": "application/json;charset=utf-8",
            },
            timeout=60.0,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "MoySkladClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def get(self, path: str, params: dict | None = None) -> dict:
        """GET с rate-limit'ом и ретраями по 429 (X-Lognex-Retry-TimeInterval, мс)."""
        for attempt in range(MAX_RETRIES + 1):
            async with self._limiter:
                resp = await self._client.get(path, params=params)
            if resp.status_code != 429:
                resp.raise_for_status()
                return resp.json()
            if attempt >= MAX_RETRIES:
                resp.raise_for_status()
            retry_ms = resp.headers.get("X-Lognex-Retry-TimeInterval")
            delay = int(retry_ms) / 1000.0 if retry_ms and retry_ms.isdigit() else 1.0
            await asyncio.sleep(delay)
        raise RuntimeError("unreachable")

    async def paginate(self, path: str, params: dict | None = None) -> AsyncIterator[dict]:
        """Итерация по всем строкам списочного эндпоинта (limit/offset)."""
        offset = 0
        while True:
            page_params = dict(params or {})
            page_params.update({"limit": PAGE_LIMIT, "offset": offset})
            data = await self.get(path, page_params)
            rows: list[dict] = data.get("rows", [])
            for row in rows:
                yield row
            offset += len(rows)
            meta_size = data.get("meta", {}).get("size", 0)
            if not rows or offset >= meta_size:
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
        """Ассортимент (товары/модификации) с ценами и остатками."""
        return [row async for row in self.paginate("/entity/assortment")]

    async def fetch_stock_by_store(self) -> list[dict]:
        """Отчёт «Остатки по складам»."""
        return [row async for row in self.paginate("/report/stock/bystore")]

    async def fetch_demand_positions(self, updated_from: str | None = None) -> list[dict]:
        """Отгрузки (продажи); фильтр по moment для инкрементального синка."""
        params: dict[str, Any] = {"expand": "positions"}
        if updated_from:
            params["filter"] = f"moment>={updated_from}"
        return [row async for row in self.paginate("/entity/demand", params)]

    async def fetch_sales_returns(self, updated_from: str | None = None) -> list[dict]:
        """Возвраты покупателей."""
        params: dict[str, Any] = {"expand": "positions"}
        if updated_from:
            params["filter"] = f"moment>={updated_from}"
        return [row async for row in self.paginate("/entity/salesreturn", params)]
