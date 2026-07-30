"""Синхронизация данных МойСклад → БД «Оборота».

Первичный синк (mode='initial'), этапы и доли прогресса:
  products      (0–8%)   — entity/assortment: товары и модификации, цены;
  stock_history (8–70%)  — report/stock/all с moment ВНУТРИ filter, по каждой
                           дате за HISTORY_DAYS × каждому активному складу;
                           суммарно → stock_days, последняя дата → warehouse_stock;
  sales         (70–95%) — retaildemand + demand + salesreturn c expand=positions
                           за HISTORY_DAYS, фильтр по выбранным складам → sales;
  finalize      (95–100%)— connection.status='active', сброс кэша аналитики.

Инкрементальный синк (mode='incremental'): обновление цен из ассортимента,
живые остатки на сегодня (+ явные нули), перезапись продаж за последние
SYNC_DAYS_BACK дней (окно — так legacy чинил дыры от опоздавших документов).

Приёмы, портированные из legacy (проверены на реальном бренде):
- «явный ноль»: позиция с прошлым остатком >0, исчезнувшая из отчёта, получает
  qty=0 — иначе фронт вечно тянет последний положительный остаток, а dis тикает
  (правило самоизлечивающееся: после записи нуля позиция выпадает из prev);
- цены МойСклад — в копейках (везде /100);
- base_name/size: характеристика «Размер» модификации, иначе финальные скобки
  имени как в legacy _canon_name.

Прогресс пишется в таблицу sync_state; запуск — фоновым потоком со своим
event loop (start_sync). APScheduler намеренно не подключён — планировщик
прода отдельная задача, есть только ручка POST /api/sync/run.
"""
import asyncio
import json
import os
import re
import threading
from datetime import date, datetime, timedelta

from sqlalchemy import delete, insert, select

from app import analytics
from app.crypto import decrypt_token
from app.db import SessionLocal
from app.models import (
    Connection,
    Product,
    Sale,
    StockDay,
    SyncState,
    Warehouse,
    WarehouseStock,
)
from app.ms_client import MoySkladClient

HISTORY_DAYS = int(os.environ.get("HISTORY_DAYS", "365"))
SALES_RESYNC_DAYS = int(os.environ.get("SYNC_DAYS_BACK", "3"))

# Ожидаемый темп с учётом лимитов МойСклад (45 req / 3 c ≈ 15 rps, берём с запасом).
EFFECTIVE_RPS = 12.0

_SIZE_SUFFIX_RE = re.compile(r"\s*\(([^)]*)\)\s*$")

_threads: dict[int, threading.Thread] = {}
_threads_lock = threading.Lock()


# ── Имена и размеры (как в legacy _canon_name) ───────────────────────────────

def strip_size(name: str) -> str:
    """Каноническое имя без финальных скобок-размера: 'Худи (S)' → 'Худи'."""
    return _SIZE_SUFFIX_RE.sub("", str(name or "")).strip()


def parse_size_suffix(name: str) -> str:
    """Размер из финальных скобок имени: 'Худи (S)' → 'S'; нет скобок — ''."""
    m = _SIZE_SUFFIX_RE.search(str(name or ""))
    return m.group(1).strip() if m else ""


def _href_id(href: str | None) -> str:
    """UUID сущности из meta.href (query-параметры отбрасываются)."""
    if not href:
        return ""
    return href.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]


def _kopecks(value) -> float:
    """Копейки МойСклад → рубли."""
    try:
        return float(value or 0) / 100.0
    except (TypeError, ValueError):
        return 0.0


# ── Состояние синка (sync_state) ─────────────────────────────────────────────

def _set_state(org_id: int, **fields) -> None:
    db = SessionLocal()
    try:
        row = db.get(SyncState, org_id)
        if row is None:
            row = SyncState(org_id=org_id)
            db.add(row)
        for key, value in fields.items():
            setattr(row, key, value)
        db.commit()
    finally:
        db.close()


def get_status(org_id: int) -> dict:
    """GET /api/sync/status: текущее состояние синхронизации организации."""
    db = SessionLocal()
    try:
        row = db.get(SyncState, org_id)
    finally:
        db.close()
    if row is None:
        return {"state": "idle", "stage": "", "progress_pct": 0, "detail": "",
                "started_at": None, "finished_at": None, "stats": {}, "error": ""}
    return {
        "state": row.state,
        "mode": row.mode,
        "stage": row.stage,
        "progress_pct": round(row.progress, 1),
        "detail": row.detail,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "stats": row.stats,
        "error": row.error,
    }


def is_running(org_id: int) -> bool:
    with _threads_lock:
        thread = _threads.get(org_id)
        if thread is not None and thread.is_alive():
            return True
    return get_status(org_id)["state"] == "running"


def estimate_minutes(n_dates: int, n_stores: int) -> float:
    """Честная оценка длительности: даты×склады + ассортимент + документы."""
    requests = n_dates * max(1, n_stores) + 30
    return round(requests / EFFECTIVE_RPS / 60.0, 1)


def start_sync(org_id: int, mode: str) -> bool:
    """Запускает фоновый синк (initial | incremental). False — уже идёт."""
    with _threads_lock:
        thread = _threads.get(org_id)
        if thread is not None and thread.is_alive():
            return False
        _set_state(
            org_id,
            state="running", mode=mode, stage="queued", progress=0.0,
            detail="Синхронизация поставлена в очередь", error="",
            stats_json="{}", started_at=datetime.utcnow(), finished_at=None,
        )
        thread = threading.Thread(
            target=_thread_main, args=(org_id, mode),
            name=f"ms-sync-{mode}-{org_id}", daemon=True,
        )
        _threads[org_id] = thread
        thread.start()
        return True


def _thread_main(org_id: int, mode: str) -> None:
    try:
        asyncio.run(_run_sync(org_id, mode))
    except Exception as exc:  # noqa: BLE001 — любой сбой фиксируем в состоянии
        _set_state(
            org_id,
            state="error", error=_human_error(exc),
            detail=str(exc)[:500], finished_at=datetime.utcnow(),
        )


def _human_error(exc: Exception) -> str:
    import httpx

    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 403):
            return ("МойСклад не принял токен доступа. Проверьте токен в настройках "
                    "и запустите синхронизацию заново.")
        return f"МойСклад ответил ошибкой {code}. Попробуйте повторить синхронизацию позже."
    if isinstance(exc, httpx.HTTPError):
        return "Не удалось связаться с МойСклад: проблема с сетью. Попробуйте позже."
    return f"Синхронизация прервана: {exc}"


# ── Основной прогон ──────────────────────────────────────────────────────────

async def _run_sync(org_id: int, mode: str) -> None:
    db = SessionLocal()
    try:
        conn = db.execute(
            select(Connection).where(
                Connection.org_id == org_id, Connection.kind == "moysklad"
            )
        ).scalars().first()
        if conn is None:
            raise RuntimeError("Подключение МойСклад не настроено")
        token = decrypt_token(conn.token_enc)
        if not token:
            raise RuntimeError("Токен МойСклад не читается — подключите заново")
        warehouses = db.execute(
            select(Warehouse).where(Warehouse.org_id == org_id, Warehouse.ext_id != "")
        ).scalars().all()
        active_wh = [w for w in warehouses if w.active]
        if not active_wh:
            raise RuntimeError("Не выбран ни один склад для синхронизации")
    finally:
        db.close()

    stats: dict = {"mode": mode}
    history_days = HISTORY_DAYS if mode == "initial" else 1
    sales_days = HISTORY_DAYS if mode == "initial" else SALES_RESYNC_DAYS

    async with MoySkladClient(token) as client:
        # ── Этап 1: товары ──────────────────────────────────────────────────
        _set_state(org_id, stage="products", progress=1.0,
                   detail="Загружаем ассортимент (товары и размеры)…")
        assortment = await client.fetch_assortment()
        ext_to_pid = _upsert_products(org_id, assortment, stats)
        _set_state(org_id, stage="products", progress=8.0,
                   detail=f"Товары обновлены: {stats['products_total']} позиций",
                   stats_json=json.dumps(stats, ensure_ascii=False))

        # ── Этап 2: история остатков ────────────────────────────────────────
        today = date.today()
        dates = [
            (today - timedelta(days=offset)).isoformat()
            for offset in range(history_days - 1, -1, -1)
        ]
        await _sync_stock_history(org_id, client, active_wh, dates, ext_to_pid,
                                  stats, initial=(mode == "initial"))

        # ── Этап 3: продажи ─────────────────────────────────────────────────
        await _sync_sales(org_id, client, active_wh, sales_days, ext_to_pid,
                          stats, initial=(mode == "initial"))

    # ── Финализация ─────────────────────────────────────────────────────────
    _set_state(org_id, stage="finalize", progress=97.0,
               detail="Пересчитываем аналитику…")
    db = SessionLocal()
    try:
        conn = db.execute(
            select(Connection).where(
                Connection.org_id == org_id, Connection.kind == "moysklad"
            )
        ).scalars().first()
        if conn is not None:
            conn.status = "active"
            conn.last_sync_at = datetime.utcnow()
        db.commit()
    finally:
        db.close()
    analytics.invalidate(org_id)
    _set_state(
        org_id,
        state="done", stage="done", progress=100.0,
        detail="Синхронизация завершена",
        stats_json=json.dumps(stats, ensure_ascii=False),
        finished_at=datetime.utcnow(),
    )


# ── Товары ───────────────────────────────────────────────────────────────────

def _parse_assortment(rows: list[dict]) -> list[dict]:
    """Строки ассортимента → атрибуты наших products.

    product: base_name = имя, size=''; variant: base_name = имя родительского
    product (или имя без финальных скобок), size = характеристика «Размер»
    либо финальные скобки имени. Цены в копейках → рубли.
    """
    parent_meta: dict[str, dict] = {}
    for row in rows:
        if (row.get("meta") or {}).get("type") == "product":
            pid = row.get("id") or _href_id((row.get("meta") or {}).get("href"))
            parent_meta[pid] = {
                "name": row.get("name") or "",
                "category": _category_of(row),
                "sale_price": _sale_price_of(row),
                "cost_price": _kopecks((row.get("buyPrice") or {}).get("value")),
                "archived": bool(row.get("archived")),
            }

    out: list[dict] = []
    for row in rows:
        meta_type = (row.get("meta") or {}).get("type")
        if meta_type not in ("product", "variant"):
            continue  # услуги, комплекты и серии в аналитике не участвуют
        ext_id = row.get("id") or _href_id((row.get("meta") or {}).get("href"))
        if not ext_id:
            continue
        name = row.get("name") or ""
        if meta_type == "product":
            parent = parent_meta[ext_id]
            out.append({
                "ext_id": ext_id,
                "base_name": name,
                "size": "",
                "category": parent["category"],
                "sale_price": parent["sale_price"],
                "cost_price": parent["cost_price"],
                "archived": parent["archived"],
            })
            continue
        # variant
        parent_id = _href_id(((row.get("product") or {}).get("meta") or {}).get("href"))
        parent = parent_meta.get(parent_id)
        size = ""
        for ch in row.get("characteristics") or []:
            if str(ch.get("name") or "").strip().lower() in ("размер", "size"):
                size = str(ch.get("value") or "").strip()
                break
        if not size:
            size = parse_size_suffix(name)
        base_name = parent["name"] if parent else strip_size(name)
        sale_price = _sale_price_of(row) or (parent["sale_price"] if parent else 0.0)
        cost_price = _kopecks((row.get("buyPrice") or {}).get("value")) or (
            parent["cost_price"] if parent else 0.0
        )
        archived = bool(row.get("archived")) or bool(parent and parent["archived"])
        out.append({
            "ext_id": ext_id,
            "base_name": base_name,
            "size": size,
            "category": parent["category"] if parent else _category_of(row),
            "sale_price": sale_price,
            "cost_price": cost_price,
            "archived": archived,
        })
    return out


def _sale_price_of(row: dict) -> float:
    prices = row.get("salePrices") or []
    if not prices:
        return 0.0
    return _kopecks((prices[0] or {}).get("value"))


def _category_of(row: dict) -> str:
    """Категория = последний сегмент pathName группы товаров МойСклад."""
    path = row.get("pathName") or ""
    if path:
        return path.split("/")[-1].strip()
    folder = (row.get("productFolder") or {}).get("name")
    return str(folder or "").strip()


def _upsert_products(org_id: int, assortment: list[dict], stats: dict) -> dict[str, int]:
    """Создаёт/обновляет products; возвращает карту ext_id → наш product.id."""
    parsed = _parse_assortment(assortment)
    db = SessionLocal()
    try:
        existing = {
            p.ext_id: p
            for p in db.execute(
                select(Product).where(Product.org_id == org_id, Product.ext_id != "")
            ).scalars()
        }
        created = updated = 0
        for item in parsed:
            row = existing.get(item["ext_id"])
            if row is None:
                row = Product(org_id=org_id, ext_id=item["ext_id"])
                db.add(row)
                created += 1
            else:
                updated += 1
            row.base_name = item["base_name"]
            row.size = item["size"]
            row.category = item["category"]
            row.sale_price = item["sale_price"]
            row.cost_price = item["cost_price"]
            row.archived = item["archived"]
        db.commit()
        ext_to_pid = {
            ext: pid
            for ext, pid in db.execute(
                select(Product.ext_id, Product.id).where(
                    Product.org_id == org_id, Product.ext_id != ""
                )
            )
        }
    finally:
        db.close()
    stats["products_total"] = len(parsed)
    stats["products_created"] = created
    stats["products_updated"] = updated
    return ext_to_pid


# ── История остатков ─────────────────────────────────────────────────────────

async def _fetch_day_stock(client: MoySkladClient, active_wh: list[Warehouse],
                           day_iso: str, ext_to_pid: dict[str, int],
                           unmatched: set[str]) -> tuple[dict[int, float], dict[int, dict[int, float]]]:
    """Остатки на конец дня: (суммарно по товару, по складам).

    Возвращает ({product_id: qty>0}, {warehouse.id: {product_id: qty}}).
    """
    per_wh_rows = await asyncio.gather(
        *[client.fetch_stock_on(day_iso, w.ext_id) for w in active_wh]
    )
    totals: dict[int, float] = {}
    by_wh: dict[int, dict[int, float]] = {}
    for warehouse, rows in zip(active_wh, per_wh_rows):
        wh_map = by_wh.setdefault(warehouse.id, {})
        for row in rows:
            ext = _href_id((row.get("meta") or {}).get("href"))
            pid = ext_to_pid.get(ext)
            if pid is None:
                if ext:
                    unmatched.add(ext)
                continue
            qty = float(row.get("stock") or 0)
            if not qty:
                continue
            totals[pid] = totals.get(pid, 0.0) + qty
            wh_map[pid] = wh_map.get(pid, 0.0) + qty
    return totals, by_wh


async def _sync_stock_history(org_id: int, client: MoySkladClient,
                              active_wh: list[Warehouse], dates: list[str],
                              ext_to_pid: dict[str, int], stats: dict,
                              initial: bool) -> None:
    """stock_days по датам + warehouse_stock на последнюю дату.

    Явные нули — правило из legacy sync.py: позиция с прошлым остатком >0,
    отсутствующая в отчёте текущей даты, получает qty=0.
    """
    unmatched: set[str] = set()
    written = zeroed = 0

    db = SessionLocal()
    try:
        if initial:
            # Полная пересборка истории: чистим и пишем заново, prev с нуля.
            db.execute(delete(StockDay).where(StockDay.org_id == org_id))
            db.commit()
            prev_positive: set[int] = set()
        else:
            # Инкремент: prev = последняя дата ДО сегодняшней.
            first_day = dates[0]
            prev_date = db.execute(
                select(StockDay.date)
                .where(StockDay.org_id == org_id, StockDay.date < first_day)
                .order_by(StockDay.date.desc())
                .limit(1)
            ).scalar()
            prev_positive = set()
            if prev_date:
                prev_positive = {
                    pid
                    for pid, in db.execute(
                        select(StockDay.product_id).where(
                            StockDay.org_id == org_id,
                            StockDay.date == prev_date,
                            StockDay.qty > 0,
                        )
                    )
                }
    finally:
        db.close()

    total_dates = len(dates)
    last_by_wh: dict[int, dict[int, float]] = {}
    for idx, day_iso in enumerate(dates):
        totals, by_wh = await _fetch_day_stock(client, active_wh, day_iso,
                                               ext_to_pid, unmatched)
        rows = [
            {"org_id": org_id, "product_id": pid, "date": day_iso, "qty": qty}
            for pid, qty in totals.items()
        ]
        # Явный ноль для распроданных: были >0, из отчёта исчезли.
        for gone in prev_positive - set(totals):
            rows.append({"org_id": org_id, "product_id": gone, "date": day_iso, "qty": 0.0})
            zeroed += 1
        db = SessionLocal()
        try:
            if not initial:
                db.execute(delete(StockDay).where(
                    StockDay.org_id == org_id, StockDay.date == day_iso
                ))
            if rows:
                db.execute(insert(StockDay), rows)
            db.commit()
        finally:
            db.close()
        written += len(rows)
        prev_positive = {pid for pid, qty in totals.items() if qty > 0}
        last_by_wh = by_wh

        if initial:
            progress = 8.0 + 62.0 * (idx + 1) / total_dates
            _set_state(org_id, stage="stock_history", progress=progress,
                       detail=f"История остатков: {day_iso} ({idx + 1}/{total_dates})")
        else:
            _set_state(org_id, stage="stock_today", progress=40.0,
                       detail="Остатки на сегодня обновлены")

    # Текущие остатки по складам — из последней даты (сегодня).
    wh_rows = [
        {"org_id": org_id, "product_id": pid, "warehouse_id": wh_id, "qty": qty}
        for wh_id, wh_map in last_by_wh.items()
        for pid, qty in wh_map.items()
        if qty
    ]
    db = SessionLocal()
    try:
        db.execute(delete(WarehouseStock).where(WarehouseStock.org_id == org_id))
        if wh_rows:
            db.execute(insert(WarehouseStock), wh_rows)
        db.commit()
    finally:
        db.close()

    stats["stock_dates"] = total_dates
    stats["stock_rows"] = written
    stats["stock_zeroed"] = zeroed
    stats["warehouse_stock_rows"] = len(wh_rows)
    if unmatched:
        stats["stock_unmatched_skus"] = len(unmatched)


# ── Продажи ──────────────────────────────────────────────────────────────────

async def _sync_sales(org_id: int, client: MoySkladClient,
                      active_wh: list[Warehouse], days_back: int,
                      ext_to_pid: dict[str, int], stats: dict,
                      initial: bool) -> None:
    """Продажи и возвраты из документов МойСклад → таблица sales.

    Документы: retaildemand (розница) + demand (отгрузки) — продажи,
    salesreturn — возвраты. Фильтр по выбранным складам (store документа).
    Выручка позиции — после скидки: price*qty*(1-discount/100), копейки → ₽.
    Окно дат перезаписывается целиком (так legacy чинил дыры от опоздавших
    документов).
    """
    today = date.today()
    cutoff = (today - timedelta(days=days_back - 1)).isoformat()
    active_store_ids = {w.ext_id for w in active_wh}

    base_progress = 70.0 if initial else 50.0
    span = 25.0 if initial else 40.0

    agg: dict[tuple[int, str, bool], list[float]] = {}
    stats["sales_docs"] = 0
    stats["sales_docs_skipped_store"] = 0
    entities = (("retaildemand", False), ("demand", False), ("salesreturn", True))
    for step, (entity, is_return) in enumerate(entities):
        _set_state(org_id, stage="sales",
                   progress=base_progress + span * step / len(entities),
                   detail=f"Загружаем документы: {entity}…")
        docs = await client.fetch_documents(entity, cutoff)
        for doc in docs:
            store_ext = _href_id(((doc.get("store") or {}).get("meta") or {}).get("href"))
            if store_ext not in active_store_ids:
                stats["sales_docs_skipped_store"] += 1
                continue
            day = str(doc.get("moment") or "")[:10]
            if not day or day < cutoff:
                continue
            positions = ((doc.get("positions") or {}).get("rows")) or []
            for pos in positions:
                ext = _href_id(((pos.get("assortment") or {}).get("meta") or {}).get("href"))
                pid = ext_to_pid.get(ext)
                if pid is None:
                    continue
                qty = float(pos.get("quantity") or 0)
                if qty <= 0:
                    continue
                discount = float(pos.get("discount") or 0)
                revenue = _kopecks(pos.get("price")) * qty * (1 - discount / 100.0)
                key = (pid, day, is_return)
                cur = agg.setdefault(key, [0.0, 0.0])
                cur[0] += qty
                cur[1] += revenue
            stats["sales_docs"] += 1

    rows = [
        {"org_id": org_id, "product_id": pid, "date": day, "qty": qty,
         "revenue": round(revenue, 2), "is_return": is_return}
        for (pid, day, is_return), (qty, revenue) in agg.items()
    ]
    db = SessionLocal()
    try:
        if initial:
            db.execute(delete(Sale).where(Sale.org_id == org_id))
        else:
            db.execute(delete(Sale).where(Sale.org_id == org_id, Sale.date >= cutoff))
        if rows:
            db.execute(insert(Sale), rows)
        db.commit()
    finally:
        db.close()

    stats["sales_rows"] = sum(1 for r in rows if not r["is_return"])
    stats["return_rows"] = sum(1 for r in rows if r["is_return"])
    stats["sales_window_from"] = cutoff
    _set_state(org_id, stage="sales", progress=base_progress + span,
               detail=f"Продажи записаны: {len(rows)} строк с {cutoff}",
               stats_json=json.dumps(stats, ensure_ascii=False))
