# -*- coding: utf-8 -*-
"""
rebuild_history.py — пересборка истории остатков из МойСклад API (3 торговых склада).

Точный остаток на каждую дату из отчёта /report/stock/all с параметром moment —
без реконструкции по документам: каждая дата = независимый факт из МойСклада.

  POST /api/rebuild-history?dry=1   — проверка: 2 пробные даты, сетка, без записи
  POST /api/rebuild-history         — фоновый прогон: staging → атомарная замена stock_snapshots
  GET  /api/rebuild-history-status  — прогресс
  POST /api/rebuild-history-restore — откат из бэкапа (stock_snapshots_backup)

Требует MS_TOKEN в переменных окружения (токен API МойСклада).
Если задан DB_QUERY_KEY — POST-эндпоинты требуют заголовок X-DB-Key.

Подключение в конце main.py (порядок роутов не важен — вставляемся в начало):
    try:
        import rebuild_history as _rbh
        _rbh.attach(app)
    except Exception as _e:
        print("rebuild_history attach failed:", _e)
"""

import os
import threading
import time
from datetime import datetime, date, timedelta

from fastapi import HTTPException
from starlette.requests import Request

MS_API_BASE = "https://api.moysklad.ru/api/remap/1.2"
TRADE_STORES = {
    "8b9e4ea2-aed7-11ed-0a80-02dc00170bf2": "Гороховая",
    "5b59fdb9-89b0-11ec-0a80-05d4000f9f5b": "Интернет-магазин",
    "6503e590-89b0-11ec-0a80-032b000e92b8": "Мясницкая",
}

_rb_state = {"running": False, "phase": "idle", "done": 0, "total": 0,
             "current_date": None, "written_rows": 0, "errors": [],
             "started": None, "finished": None}


def _check_key(request: Request):
    key = os.environ.get("DB_QUERY_KEY")
    if key and request.headers.get("X-DB-Key") != key:
        raise HTTPException(status_code=401, detail="bad or missing X-DB-Key")


def _ms_headers():
    tok = os.environ.get("MS_TOKEN", "")
    if not tok:
        raise RuntimeError("MS_TOKEN не задан в переменных окружения Render")
    return {"Authorization": f"Bearer {tok}", "Accept-Encoding": "gzip"}


def _ms_stock_on(day_iso: str):
    """Остаток по каждому SKU суммарно по 3 торговым складам на конец дня day_iso."""
    import httpx
    # moment — поле фильтра (как отдельный query-параметр игнорируется!)
    flt = f"moment={day_iso} 23:59:00;" + ";".join(
        f"store={MS_API_BASE}/entity/store/{sid}" for sid in TRADE_STORES)
    totals, offset = {}, 0
    while True:
        r = None
        for _attempt in range(6):
            r = httpx.get(f"{MS_API_BASE}/report/stock/all",
                          params={"filter": flt, "groupBy": "variant",
                                  "limit": 1000, "offset": offset},
                          headers=_ms_headers(), timeout=90)
            if r.status_code == 429:
                time.sleep(3.5)
                continue
            r.raise_for_status()
            break
        else:
            raise RuntimeError(f"МойСклад: 429 не ушёл после 6 попыток ({day_iso})")
        rows = r.json().get("rows", [])
        for row in rows:
            n = row.get("name")
            q = float(row.get("stock") or 0)
            if n and q:
                totals[n] = totals.get(n, 0.0) + q
        if len(rows) < 1000:
            return totals
        offset += 1000


def _rb_grid(start: str, daily_from: str, end: str):
    """Сетка дат: понедельники от start до daily_from, дальше ежедневно до end."""
    s = date.fromisoformat(start)
    df = date.fromisoformat(daily_from)
    e = date.fromisoformat(end)
    out = []
    cur = s + timedelta(days=(7 - s.weekday()) % 7)  # ближайший понедельник от start
    while cur < df:
        out.append(cur.isoformat())
        cur += timedelta(days=7)
    cur = df
    while cur <= e:
        out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def _rb_run(grid):
    import main as site
    _rb_state.update(running=True, phase="fetch", done=0, total=len(grid),
                     written_rows=0, errors=[],
                     started=datetime.now().isoformat(timespec="seconds"), finished=None)
    try:
        conn = site.get_db()
        conn.execute("DROP TABLE IF EXISTS stock_snapshots_new")
        conn.execute("""CREATE TABLE stock_snapshots_new (
            date TEXT NOT NULL, sku_name TEXT NOT NULL,
            stock_qty REAL NOT NULL DEFAULT 0, uploaded_at TEXT NOT NULL,
            UNIQUE(date, sku_name))""")
        conn.commit()
        prev_skus = set()
        now = datetime.now().isoformat()
        for d in grid:
            _rb_state["current_date"] = d
            try:
                totals = _ms_stock_on(d)
            except Exception as e:
                _rb_state["errors"].append(f"{d}: {e}")
                if len(_rb_state["errors"]) > 30:
                    raise RuntimeError("слишком много ошибок — прерываю")
                _rb_state["done"] += 1
                continue
            rows = [(d, n, q, now) for n, q in totals.items()]
            # явный ноль: позиция была >0 на прошлой дате сетки, теперь исчезла из отчёта
            for gone in prev_skus - set(totals):
                rows.append((d, gone, 0.0, now))
            conn.executemany(
                "INSERT OR REPLACE INTO stock_snapshots_new "
                "(date, sku_name, stock_qty, uploaded_at) VALUES (?,?,?,?)", rows)
            conn.commit()
            prev_skus = {n for n, q in totals.items() if q > 0}
            _rb_state["written_rows"] += len(rows)
            _rb_state["done"] += 1
            time.sleep(0.3)  # щадим лимиты МойСклада
        # атомарная замена + бэкап старых данных
        _rb_state["phase"] = "swap"
        conn.execute("DROP TABLE IF EXISTS stock_snapshots_backup")
        conn.execute("CREATE TABLE stock_snapshots_backup AS SELECT * FROM stock_snapshots")
        conn.execute("DELETE FROM stock_snapshots")
        conn.execute("""INSERT INTO stock_snapshots (date, sku_name, stock_qty, uploaded_at)
                        SELECT date, sku_name, stock_qty, uploaded_at FROM stock_snapshots_new""")
        conn.execute("DROP TABLE stock_snapshots_new")
        conn.commit()
        _rb_state["phase"] = "caches"
        site.rebuild_analytics_json(conn)
        conn.close()
        site._analytics_cache = None
        site._analytics_cache_key = None
        _rb_state["phase"] = "done"
        try:
            import sync
            sync._notify(
                f"✅ История остатков пересобрана из МойСклада: "
                f"{_rb_state['done']} дат, {_rb_state['written_rows']} строк, "
                f"ошибок: {len(_rb_state['errors'])}", False)
        except Exception:
            pass
    except Exception as e:
        _rb_state["errors"].append(f"FATAL: {e}")
        _rb_state["phase"] = "failed"
    finally:
        _rb_state["running"] = False
        _rb_state["finished"] = datetime.now().isoformat(timespec="seconds")


def rebuild_history(request: Request, dry: int = 0,
                    start: str = "2022-04-01",
                    daily_from: str = "", end: str = ""):
    """dry=1 — пробные запросы к МойСкладу без записи. Иначе фоновый прогон.
    Сетка: понедельники от start, ежедневно за последние 365 дней (daily_from)."""
    _check_key(request)
    end = end or date.today().isoformat()
    daily_from = daily_from or (date.today() - timedelta(days=365)).isoformat()
    if dry:
        probe = {}
        for d in (end, daily_from):
            t = _ms_stock_on(d)
            probe[d] = {"skus": len(t), "total_qty": round(sum(t.values()), 1),
                        "sample": dict(sorted(t.items())[:5])}
        grid = _rb_grid(start, daily_from, end)
        return {"dry": True, "grid_dates": len(grid),
                "grid_first": grid[:3], "grid_last": grid[-3:], "probe": probe}
    if _rb_state["running"]:
        return {"error": "пересборка уже идёт", "state": _rb_state}
    grid = _rb_grid(start, daily_from, end)
    threading.Thread(target=_rb_run, args=(grid,), daemon=True).start()
    return {"started": True, "grid_dates": len(grid),
            "estimate_minutes": round(len(grid) * 0.9 / 60, 1)}


def rebuild_history_status():
    return _rb_state


def rebuild_history_restore(request: Request):
    """Откат: вернуть stock_snapshots из бэкапа, пересобрать кэши."""
    _check_key(request)
    import main as site
    conn = site.get_db()
    try:
        n = conn.execute("SELECT COUNT(*) FROM stock_snapshots_backup").fetchone()[0]
    except Exception:
        conn.close()
        return {"error": "бэкапа нет (stock_snapshots_backup отсутствует)"}
    conn.execute("DELETE FROM stock_snapshots")
    conn.execute("INSERT INTO stock_snapshots SELECT * FROM stock_snapshots_backup")
    conn.commit()
    site.rebuild_analytics_json(conn)
    conn.close()
    site._analytics_cache = None
    site._analytics_cache_key = None
    return {"restored_rows": n}


def _migrate_project_tables():
    """Старые SQLite-таблицы без колонки project_id ломают /api/adjustments,
    /api/excluded, /api/order-added (500). Пересоздаём с новой схемой,
    старые данные переносим с project_id=''."""
    import sqlite3
    conn = sqlite3.connect("/data/stocks.db")
    specs = {
        "sku_adjustments": (
            "CREATE TABLE {t} (project_id TEXT NOT NULL DEFAULT '', sku_base TEXT NOT NULL, "
            "qty_adj INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL DEFAULT '', "
            "PRIMARY KEY (project_id, sku_base))",
            ["sku_base", "qty_adj", "updated_at"]),
        "order_excluded": (
            "CREATE TABLE {t} (project_id TEXT NOT NULL DEFAULT '', sku_base TEXT NOT NULL, "
            "excluded_at TEXT NOT NULL DEFAULT '', PRIMARY KEY (project_id, sku_base))",
            ["sku_base", "excluded_at"]),
        "order_added": (
            "CREATE TABLE {t} (project_id TEXT NOT NULL DEFAULT '', sku_base TEXT NOT NULL, "
            "added_at TEXT NOT NULL DEFAULT '', PRIMARY KEY (project_id, sku_base))",
            ["sku_base", "added_at"]),
    }
    for t, (ddl, cols) in specs.items():
        try:
            info = conn.execute("PRAGMA table_info(%s)" % t).fetchall()
            names = [r[1] for r in info]
            if not info or "project_id" in names:
                continue
            avail = [c for c in cols if c in names]
            conn.execute("ALTER TABLE %s RENAME TO %s_legacy" % (t, t))
            conn.execute(ddl.format(t=t))
            if avail:
                collist = ",".join(avail)
                conn.execute("INSERT OR IGNORE INTO %s (project_id,%s) SELECT '',%s FROM %s_legacy"
                             % (t, collist, collist, t))
            conn.commit()
            print("migrated table:", t)
        except Exception as e:
            print("migrate", t, "failed:", e)
    conn.close()


_retail_cache = {"t": 0.0, "data": None}


def retail_prices():
    """Розничные цены («Цена продажи») из зеркала МойСклада, по имени товара. Кэш 1 час."""
    if _retail_cache["data"] is not None and time.time() - _retail_cache["t"] < 3600:
        return _retail_cache["data"]
    import sync
    pg = sync.get_pg()
    try:
        cur = pg.cursor()
        cur.execute("""SELECT p.name, MAX(sp.value)/100.0
                       FROM lenproduct_saleprice sp
                       JOIN lenproduct p ON p.id = sp.id
                       WHERE sp.pricetype_name = 'Цена продажи' AND sp.value > 0
                       GROUP BY p.name""")
        data = {str(n): float(v) for n, v in cur.fetchall() if n}
    finally:
        pg.close()
    _retail_cache["t"] = time.time()
    _retail_cache["data"] = data
    return data


_costprice_cache = {"t": 0.0, "data": None}


def cost_prices():
    """Цены из зеркала МойСклада по имени товара:
    price = «Цена продажи», cost = «Себестоимость», purchase = закупочная (пошив).
    Ткань = cost - purchase (считается на клиенте). Кэш 1 час."""
    if _costprice_cache["data"] is not None and time.time() - _costprice_cache["t"] < 3600:
        return _costprice_cache["data"]
    import sync
    pg = sync.get_pg()
    try:
        cur = pg.cursor()
        cur.execute("""
            SELECT p.name,
                   MAX(p.buyprice_value)/100.0 AS purchase,
                   MAX(CASE WHEN sp.pricetype_name = 'Себестоимость' THEN sp.value END)/100.0 AS cost,
                   MAX(CASE WHEN sp.pricetype_name = 'Цена продажи' THEN sp.value END)/100.0 AS price
            FROM lenproduct p
            LEFT JOIN lenproduct_saleprice sp ON sp.id = p.id
            GROUP BY p.name""")
        data = {}
        for n, purchase, cost, price in cur.fetchall():
            if not n:
                continue
            data[str(n)] = {"purchase": float(purchase or 0), "cost": float(cost or 0),
                            "price": float(price or 0)}
    finally:
        pg.close()
    _costprice_cache["t"] = time.time()
    _costprice_cache["data"] = data
    return data


_sbm_cache = {"t": 0.0, "data": None}


def sales_by_month():
    """Нетто-продажи по месяцам и позициям: {base: {"2026-07": [qty, rev]}}. Кэш 1 час."""
    if _sbm_cache["data"] is not None and time.time() - _sbm_cache["t"] < 3600:
        return _sbm_cache["data"]
    import main as site
    conn = site.get_db()
    rows = conn.execute("""
        SELECT substr(date,1,7) AS m, sku_name,
               SUM(CASE WHEN doc_type='sale' THEN qty ELSE -qty END) AS q,
               SUM(CASE WHEN doc_type='sale' THEN revenue ELSE -revenue END) AS r
        FROM sales_data GROUP BY 1, 2""").fetchall()
    conn.close()
    out = {}
    for row in rows:
        base = site._canon_name(row["sku_name"])
        d = out.setdefault(base, {})
        cur = d.get(row["m"]) or [0.0, 0.0]
        cur[0] += row["q"] or 0
        cur[1] += row["r"] or 0
        d[row["m"]] = cur
    for base, d in out.items():
        for m, v in d.items():
            d[m] = [round(v[0], 1), round(v[1])]
    _sbm_cache["t"] = time.time()
    _sbm_cache["data"] = out
    return out


def sales_range(date_from: str = "", date_to: str = ""):
    """Нетто-продажи по позициям за период: {base: [qty, rev]}."""
    import main as site
    conn = site.get_db()
    q = """SELECT sku_name,
               SUM(CASE WHEN doc_type='sale' THEN qty ELSE -qty END) AS q,
               SUM(CASE WHEN doc_type='sale' THEN revenue ELSE -revenue END) AS r
           FROM sales_data WHERE 1=1"""
    args = []
    if date_from:
        q += " AND date >= ?"; args.append(date_from)
    if date_to:
        q += " AND date <= ?"; args.append(date_to)
    q += " GROUP BY sku_name"
    rows = conn.execute(q, args).fetchall()
    conn.close()
    out = {}
    for row in rows:
        base = site._canon_name(row["sku_name"])
        cur = out.get(base) or [0.0, 0.0]
        cur[0] += row["q"] or 0
        cur[1] += row["r"] or 0
        out[base] = cur
    return {b: [round(v[0], 1), round(v[1])] for b, v in out.items()}


_sod_cache = {}


def stock_on_date(date: str = ""):
    """Остатки по 3 торговым складам на конец дня date — живой запрос к МойСкладу.
    {date, stores:[имена], skus:{name:[q1,q2,q3]}}"""
    import httpx
    from datetime import date as _d
    if not date:
        date = _d.today().isoformat()
    today = _d.today().isoformat()
    ttl = 600 if date == today else 86400
    hit = _sod_cache.get(date)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    stores = list(TRADE_STORES.items())
    skus = {}
    for idx, (sid, sname) in enumerate(stores):
        flt = f"moment={date} 23:59:00;store={MS_API_BASE}/entity/store/{sid}"
        offset = 0
        while True:
            r = None
            for _attempt in range(6):
                r = httpx.get(f"{MS_API_BASE}/report/stock/all",
                              params={"filter": flt, "groupBy": "variant",
                                      "limit": 1000, "offset": offset},
                              headers=_ms_headers(), timeout=90)
                if r.status_code == 429:
                    time.sleep(3.5)
                    continue
                r.raise_for_status()
                break
            else:
                raise RuntimeError("МойСклад: слишком много 429")
            rows = r.json().get("rows", [])
            for row in rows:
                n = row.get("name")
                q = float(row.get("stock") or 0)
                if n and q:
                    skus.setdefault(n, [0.0] * len(stores))[idx] += q
            if len(rows) < 1000:
                break
            offset += 1000
    data = {"date": date, "stores": [s for _, s in stores], "skus": skus}
    if len(_sod_cache) > 120:
        _sod_cache.clear()
    _sod_cache[date] = (time.time(), data)
    return data


_ya_cache = {"t": 0.0, "data": None}
_agent_names = {}


def _agent_name(aid):
    """Имя контрагента из МойСклад API. Кэшируем ТОЛЬКО успешный ответ:
    раньше пустое имя при разовом сбое МС (таймаут/429) кэшировалось навсегда,
    и /api/yandex-live «не находил» контрагента Яндекса до рестарта сервера."""
    if aid in _agent_names:
        return _agent_names[aid]
    import httpx
    nm = ""
    try:
        r = httpx.get(f"{MS_API_BASE}/entity/counterparty/{aid}",
                      headers=_ms_headers(), timeout=30)
        if r.status_code == 200:
            nm = r.json().get("name", "")
    except Exception:
        pass
    if nm:
        _agent_names[aid] = nm
    return nm


def yandex_live():
    """Продажи Яндекс.Маркета из зеркала МойСклада (отгрузки контрагента-маркетплейса).
    {agent, skus: {base: {"2026-07": [qty, rev]}}, returns: {"2026-07": [qty, rev]}}. Кэш 1 час."""
    if _ya_cache["data"] is not None and time.time() - _ya_cache["t"] < 3600:
        return _ya_cache["data"]
    import sync
    import main as site
    pg = sync.get_pg()
    try:
        cur = pg.cursor()
        # массовые контрагенты (маркетплейсы отгружаются на одного агента)
        cur.execute("""SELECT agent_id, COUNT(*) FROM lendemand
                       WHERE agent_id IS NOT NULL GROUP BY agent_id
                       HAVING COUNT(*) >= 200 ORDER BY COUNT(*) DESC""")
        cands = [r[0] for r in cur.fetchall()]
        ya = None
        names = {}
        for a in cands:
            nm = _agent_name(a)
            names[a] = nm
            if "яндекс" in nm.lower() or "yandex" in nm.lower():
                ya = a
                break
        if not ya:
            return {"error": "Контрагент Яндекса не найден среди массовых агентов", "agents": names}
        # продажи по месяцам и позициям
        cur.execute("""
            SELECT to_char(h.moment,'YYYY-MM') AS m, COALESCE(v.name, pr.name) AS sku,
                   SUM(p.quantity) AS q,
                   SUM(p.price*p.quantity*(1-COALESCE(p.discount,0)/100.0))/100.0 AS r
            FROM lendemand_position p
            JOIN lendemand h ON h.id = p.id
            LEFT JOIN lenvariant v ON v.id = RIGHT(p.assortment_id, 36)
            LEFT JOIN lenproduct pr ON pr.id = RIGHT(p.assortment_id, 36)
            WHERE h.agent_id = %s AND COALESCE(v.name, pr.name) IS NOT NULL
            GROUP BY 1, 2""", (ya,))
        skus = {}
        for m, sku, q, r in cur.fetchall():
            base = site._canon_name(str(sku))
            d = skus.setdefault(base, {})
            cur2 = d.get(m) or [0.0, 0.0]
            cur2[0] += float(q or 0)
            cur2[1] += float(r or 0)
            d[m] = cur2
        for base, d in skus.items():
            for m, v in d.items():
                d[m] = [round(v[0], 1), round(v[1])]
        # возвраты по месяцам
        cur.execute("""
            SELECT to_char(h.moment,'YYYY-MM') AS m,
                   SUM(p.quantity) AS q,
                   SUM(p.price*p.quantity*(1-COALESCE(p.discount,0)/100.0))/100.0 AS r
            FROM lensalesreturn_position p
            JOIN lensalesreturn h ON h.id = p.id
            WHERE h.agent_id = %s
            GROUP BY 1""", (ya,))
        rets = {m: [round(float(q or 0), 1), round(float(r or 0))] for m, q, r in cur.fetchall()}
    finally:
        pg.close()
    data = {"agent": names.get(ya, ""), "skus": skus, "returns": rets}
    _ya_cache["t"] = time.time()
    _ya_cache["data"] = data
    return data


_bys_cache = {"t": 0.0, "data": None}


def stocks_bystore_live():
    """Замена /api/stocks-bystore: остатки берём ЖИВЬЁМ из МойСклада
    (отчёт остатков зеркала LensSklad может отставать), хронологию продаж — из зеркала.
    Формат совместим со старым эндпоинтом."""
    import re as _re2
    import main as site
    if _bys_cache["data"] is not None and time.time() - _bys_cache["t"] < 300:
        return _bys_cache["data"]
    data = stock_on_date()          # сегодня, напрямую из МойСклада
    stores = data["stores"]
    skus = {}
    for full, per in data["skus"].items():
        base = site._canon_name(full)
        m = _re2.search(r"\(([^)]+)\)\s*$", full)
        size = m.group(1) if m else ""
        rec = skus.setdefault(base, {"per_store": [0.0] * len(stores), "total": 0.0, "sizes": {}})
        for i, q in enumerate(per):
            rec["per_store"][i] += q
        rec["total"] += sum(per)
        srec = rec["sizes"].setdefault(size, {"full": full, "per": [0.0] * len(stores),
                                              "last": [None] * len(stores), "seen": [None] * len(stores)})
        for i, q in enumerate(per):
            srec["per"][i] += q
    # хронология последних продаж — из зеркала (отгрузки там свежие)
    try:
        import sync
        pg = sync.get_pg()
        cur = pg.cursor()
        cur.execute("""
            SELECT sku, store, MAX(last)::date FROM (
                SELECT COALESCE(v.name, p.name) AS sku, st.name AS store, MAX(h.moment) AS last
                FROM lendemand_position dp
                JOIN lendemand h ON h.id = dp.id
                JOIN lenstore st ON st.id = h.store_id
                LEFT JOIN lenvariant v ON v.id = RIGHT(dp.assortment_id, 36)
                LEFT JOIN lenproduct p ON p.id = RIGHT(dp.assortment_id, 36)
                WHERE COALESCE(v.name, p.name) IS NOT NULL GROUP BY 1, 2
                UNION ALL
                SELECT COALESCE(v.name, p.name), st.name, MAX(h.moment)
                FROM lenretaildemand_position rp
                JOIN lenretaildemand h ON h.id = rp.id
                JOIN lenstore st ON st.id = h.store_id
                LEFT JOIN lenvariant v ON v.id = RIGHT(rp.assortment_id, 36)
                LEFT JOIN lenproduct p ON p.id = RIGHT(rp.assortment_id, 36)
                WHERE COALESCE(v.name, p.name) IS NOT NULL GROUP BY 1, 2
            ) t GROUP BY 1, 2""")
        last_map = {(str(a), str(b)): str(c) for a, b, c in cur.fetchall() if a and b and c}
        pg.close()
        seen = site._seen_map()
        for base, rec in skus.items():
            for size, srec in rec["sizes"].items():
                for i, st in enumerate(stores):
                    srec["last"][i] = last_map.get((srec["full"], st))
                    srec["seen"][i] = seen.get((srec["full"], site._store_key(st)))
    except Exception:
        pass
    out = {"stores": stores, "skus": skus}
    _bys_cache["t"] = time.time()
    _bys_cache["data"] = out
    return out


_settle_cache = {"t": 0.0}


def settle_incoming(force: int = 0):
    """Взаимозачёт: приёмки и оприходования из МойСклада (на любые склады,
    включая Съёмку/Списания/БРАК) автоматически вычитаются из графы «Заказано»
    (analytics-ordered) на странице Остатков. Каждый документ учитывается один раз."""
    import httpx
    import main as site
    if not force and time.time() - _settle_cache["t"] < 600:
        return {"ok": True, "cached": True}
    _settle_cache["t"] = time.time()
    conn = site.get_db()
    done = {r["sku_base"] for r in conn.execute(
        "SELECT sku_base FROM order_added WHERE project_id='settled-docs'").fetchall()}
    ordered = {r["sku_base"]: r["qty_adj"] for r in conn.execute(
        "SELECT sku_base, qty_adj FROM sku_adjustments "
        "WHERE project_id='analytics-ordered' AND qty_adj>0").fetchall()}
    from datetime import date as _d, timedelta as _td
    cutoff = (_d.today() - _td(days=60)).isoformat()
    new_docs = []
    deltas = {}
    for ent in ("supply", "enter"):
        offset = 0
        while True:
            r = None
            for _attempt in range(6):
                r = httpx.get(f"{MS_API_BASE}/entity/{ent}",
                              params={"filter": f"moment>={cutoff} 00:00:00",
                                      "expand": "positions.assortment",
                                      "limit": 50, "offset": offset},
                              headers=_ms_headers(), timeout=60)
                if r.status_code == 429:
                    time.sleep(3.5)
                    continue
                r.raise_for_status()
                break
            else:
                raise RuntimeError("МойСклад: слишком много 429")
            rows = r.json().get("rows", [])
            for doc in rows:
                did = ent + ":" + str(doc.get("id", ""))
                if not doc.get("id") or did in done:
                    continue
                pos = (doc.get("positions") or {}).get("rows") or []
                for p in pos:
                    nm = ((p.get("assortment") or {}).get("name")) or ""
                    if not nm:
                        continue
                    base = site._canon_name(nm)
                    deltas[base] = deltas.get(base, 0.0) + float(p.get("quantity") or 0)
                new_docs.append(did)
            if len(rows) < 50:
                break
            offset += 50
    applied = {}
    now = datetime.now().isoformat()
    for base, q in deltas.items():
        cur = ordered.get(base, 0)
        if cur <= 0:
            continue
        newv = max(0, int(round(cur - q)))
        conn.execute("INSERT OR REPLACE INTO sku_adjustments "
                     "(project_id, sku_base, qty_adj, updated_at) "
                     "VALUES ('analytics-ordered', ?, ?, ?)", (base, newv, now))
        applied[base] = {"was": cur, "received": round(q, 1), "now": newv}
    for did in new_docs:
        conn.execute("INSERT OR IGNORE INTO order_added (project_id, sku_base, added_at) "
                     "VALUES ('settled-docs', ?, ?)", (did, now))
    conn.commit()
    conn.close()
    return {"ok": True, "new_docs": len(new_docs), "applied": applied}


def attach(app):
    """Регистрирует роуты В НАЧАЛО списка — до catch-all /{full_path:path}."""
    try:
        _migrate_project_tables()
    except Exception as e:
        print("project tables migration failed:", e)
    n0 = len(app.router.routes)
    app.add_api_route("/api/rebuild-history", rebuild_history, methods=["POST"])
    app.add_api_route("/api/rebuild-history-status", rebuild_history_status, methods=["GET"])
    app.add_api_route("/api/rebuild-history-restore", rebuild_history_restore, methods=["POST"])
    app.add_api_route("/api/retail-prices", retail_prices, methods=["GET"])
    app.add_api_route("/api/cost-prices", cost_prices, methods=["GET"])
    app.add_api_route("/api/sales-by-month", sales_by_month, methods=["GET"])
    app.add_api_route("/api/sales-range", sales_range, methods=["GET"])
    app.add_api_route("/api/stock-on-date", stock_on_date, methods=["GET"])
    app.add_api_route("/api/yandex-live", yandex_live, methods=["GET"])
    # перекрывает старый /api/stocks-bystore (зеркало) — остатки теперь напрямую из МойСклада
    app.add_api_route("/api/stocks-bystore", stocks_bystore_live, methods=["GET"])
    app.add_api_route("/api/settle-incoming", settle_incoming, methods=["GET", "POST"])
    new = app.router.routes[n0:]
    del app.router.routes[n0:]
    app.router.routes[:0] = new
