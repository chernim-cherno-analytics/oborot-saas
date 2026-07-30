# -*- coding: utf-8 -*-
"""
sync.py — автосинк данных МойСклад → SQLite сайта.

Остатки: НАПРЯМУЮ из МойСклад API (через rebuild_history.stock_on_date) —
отчёт остатков зеркала LensSklad может отставать от реальности.
Продажи/возвраты: из зеркала Postgres (LensSklad) — документы там свежие.

Пишет в те же таблицы и в том же формате, что ручные загрузчики:
  - stock_snapshots (date, sku_name, stock_qty)  — снапшот остатков на сегодня
  - stock_bystore (date, store, sku_name, qty)   — по складам (хронология)
  - sales_data (date, sku_name, qty, revenue, doc_type)  — продажи/возвраты по дням
"""

import os
import sqlite3
from datetime import date, datetime, timedelta

PG_URL = os.environ.get("PG_URL", "")
DB_PATH = "/data/stocks.db"

SCHEMA = {
    "demand":        "lendemand",
    "retaildemand":  "lenretaildemand",
    "salesreturn":   "lensalesreturn",
}

SALES_DAYS_BACK = int(os.environ.get("SYNC_DAYS_BACK", "3"))
MIN_STOCK_ROWS = 10   # защита: не пишем снапшот, если данных подозрительно мало


# ── подключения ───────────────────────────────────────────────────────────────

def get_pg():
    import psycopg2
    import psycopg2.extras
    conn = psycopg2.connect(PG_URL, sslmode="prefer", connect_timeout=15)
    conn.set_session(readonly=True)
    return conn


def get_sqlite():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ── инспектор схемы PG (для отладки) ─────────────────────────────────────────

def inspect_schema(prefix: str = "len"):
    pg = get_pg()
    cur = pg.cursor()
    cur.execute("""
        SELECT table_name, column_name, data_type
        FROM information_schema.columns
        WHERE table_schema='public' AND table_name LIKE %s
        ORDER BY table_name, ordinal_position
    """, (prefix + "%",))
    out = {}
    for t, c, dt in cur.fetchall():
        out.setdefault(t, []).append(f"{c} ({dt})")
    cur.execute("""
        SELECT relname, n_live_tup FROM pg_stat_user_tables
        WHERE relname LIKE %s ORDER BY n_live_tup DESC
    """, (prefix + "%",))
    counts = {r[0]: r[1] for r in cur.fetchall()}
    pg.close()
    return {"tables": out, "row_counts": counts}


def _init_bystore_table(lite):
    lite.execute("""CREATE TABLE IF NOT EXISTS stock_bystore (
        date TEXT NOT NULL, store TEXT NOT NULL, sku_name TEXT NOT NULL,
        qty REAL NOT NULL DEFAULT 0,
        PRIMARY KEY (date, store, sku_name))""")
    lite.execute("CREATE INDEX IF NOT EXISTS idx_bs_sku ON stock_bystore(sku_name)")


# ── остатки: снапшот на сегодня — НАПРЯМУЮ ИЗ МОЙСКЛАДА ─────────────────────

def sync_stock(dry: bool = False):
    """Снапшот остатков по 3 торговым складам из живого МойСклад API:
    суммарно — в stock_snapshots, по складам — в stock_bystore."""
    import rebuild_history as rh
    data = rh.stock_on_date()          # сегодня, из МойСклада (кэш 10 мин)
    stores = data["stores"]
    rows = []
    for name, per in data["skus"].items():
        for st, q in zip(stores, per):
            if q:
                rows.append((str(name), str(st), float(q)))

    if len(rows) < MIN_STOCK_ROWS:
        raise RuntimeError(f"Остатки из МойСклада подозрительно пусты ({len(rows)} строк) — снапшот не записан")

    totals = {}
    for n, st, q in rows:
        totals[n] = totals.get(n, 0.0) + q

    today = date.today().isoformat()
    now = datetime.now().isoformat()
    if dry:
        return {"stock_skus": len(totals), "stock_total_qty": sum(totals.values()),
                "stores": stores, "date": today, "source": "moysklad-live", "dry": True}

    lite = get_sqlite()
    _init_bystore_table(lite)
    # Явные нули: позиция, у которой последний записанный остаток был >0,
    # а сегодня её нет в отчёте МойСклада, — распродана. Пишем 0 на сегодня,
    # иначе фронты (turnover/analytics/forecast/order) вечно тянут последний
    # положительный остаток («фантомный сток») и dis продолжает тикать.
    # Правило самоизлечивающееся: после записи нуля последняя строка = 0,
    # и позиция больше не попадает в gone.
    gone_rows = []
    try:
        cur = lite.execute(
            "SELECT s.sku_name, s.stock_qty FROM stock_snapshots s "
            "JOIN (SELECT sku_name, MAX(date) md FROM stock_snapshots "
            "      WHERE date < ? GROUP BY sku_name) m "
            "  ON m.sku_name = s.sku_name AND m.md = s.date "
            "WHERE s.stock_qty > 0", (today,))
        for n, q in cur.fetchall():
            if n not in totals:
                gone_rows.append((today, str(n), 0.0, now))
    except Exception:
        gone_rows = []
    lite.executemany(
        "INSERT OR REPLACE INTO stock_snapshots (date, sku_name, stock_qty, uploaded_at) "
        "VALUES (?, ?, ?, ?)",
        [(today, n, q, now) for n, q in totals.items()] + gone_rows
    )
    lite.executemany(
        "INSERT OR REPLACE INTO stock_bystore (date, store, sku_name, qty) "
        "VALUES (?, ?, ?, ?)",
        [(today, st, n, q) for n, st, q in rows]
    )
    lite.commit(); lite.close()
    return {"stock_skus": len(totals), "bystore_rows": len(rows), "date": today,
            "zeroed_gone": len(gone_rows), "source": "moysklad-live"}


# ── продажи/возвраты за последние N дней (из зеркала — документы свежие) ─────

def _fetch_docs(pg, head_table: str, days_back: int):
    pos_table = head_table + "_position"
    cutoff = (date.today() - timedelta(days=days_back)).isoformat()
    cur = pg.cursor()
    cur.execute(f"""
        SELECT (h.moment)::date AS d,
               COALESCE(v.name, pr.name) AS sku,
               SUM(p.quantity) AS qty,
               SUM(p.price * p.quantity * (1 - COALESCE(p.discount,0)/100.0)) / 100.0 AS rev
        FROM {pos_table} p
        JOIN {head_table} h ON h.id = p.id
        LEFT JOIN lenvariant v ON v.id = RIGHT(p.assortment_id, 36)
        LEFT JOIN lenproduct pr ON pr.id = RIGHT(p.assortment_id, 36)
        WHERE (h.moment)::date >= %s
          AND COALESCE(v.name, pr.name) IS NOT NULL
        GROUP BY 1, 2
    """, (cutoff,))
    rows = cur.fetchall()
    return [(r[0].isoformat(), str(r[1]), float(r[2] or 0), float(r[3] or 0)) for r in rows if r[1]]


def sync_sales(dry: bool = False, days_back: int = SALES_DAYS_BACK):
    pg = get_pg()
    sales = _fetch_docs(pg, SCHEMA["demand"], days_back)
    try:
        sales += _fetch_docs(pg, SCHEMA["retaildemand"], days_back)
    except Exception as e:
        # РАНЬШЕ сбой глотался молча: оптовые продажи перезаписывали дни БЕЗ розницы,
        # и розничные продажи за days_back дней тихо стирались из sales_data.
        # Теперь синк падает громко (Телеграм «❌ Синк УПАЛ»), старые данные остаются
        # нетронутыми, следующий успешный запуск (окно 3 дня) сам дозаполнит дни.
        pg.rollback()
        pg.close()
        raise RuntimeError(f"зеркало: lenretaildemand недоступен — продажи не перезаписаны: {e}")
    try:
        returns = _fetch_docs(pg, SCHEMA["salesreturn"], days_back)
    except Exception:
        pg.rollback()
        returns = []
    pg.close()

    agg = {}
    for d, sku, qty, rev in sales:
        k = (d, sku)
        cur_q, cur_r = agg.get(k, (0.0, 0.0))
        agg[k] = (cur_q + qty, cur_r + rev)

    dates_sales = sorted({d for d, _ in agg})
    dates_ret = sorted({d for d, _, _, _ in returns})

    if dry:
        return {
            "sales_days": dates_sales, "sales_rows": len(agg),
            "sales_revenue": round(sum(r for _, r in agg.values()), 2),
            "returns_days": dates_ret, "returns_rows": len(returns),
            "returns_revenue": round(sum(r[3] for r in returns), 2),
            "dry": True,
        }

    lite = get_sqlite()
    for d in dates_sales:
        lite.execute("DELETE FROM sales_data WHERE date=? AND doc_type='sale'", (d,))
    lite.executemany(
        "INSERT OR REPLACE INTO sales_data (date, sku_name, qty, revenue, doc_type) "
        "VALUES (?, ?, ?, ?, 'sale')",
        [(d, sku, q, r) for (d, sku), (q, r) in agg.items()]
    )
    for d in dates_ret:
        lite.execute("DELETE FROM sales_data WHERE date=? AND doc_type='return'", (d,))
    lite.executemany(
        "INSERT OR REPLACE INTO sales_data (date, sku_name, qty, revenue, doc_type) "
        "VALUES (?, ?, ?, ?, 'return')",
        [(d, sku, q, r) for d, sku, q, r in returns]
    )
    lite.commit(); lite.close()
    return {"sales_days": dates_sales, "sales_rows": len(agg),
            "returns_days": dates_ret, "returns_rows": len(returns)}


# ── сверка имён SKU ───────────────────────────────────────────────────────────

def verify_names(limit: int = 50):
    import rebuild_history as rh
    data = rh.stock_on_date()
    pg_names = set(data["skus"].keys())

    lite = get_sqlite()
    site_names = {r[0] for r in lite.execute("SELECT DISTINCT sku_name FROM stock_snapshots")}
    lite.close()

    only_pg = sorted(pg_names - site_names)[:limit]
    only_site = sorted(site_names - pg_names)[:limit]
    return {"pg_total": len(pg_names), "site_total": len(site_names),
            "matched": len(pg_names & site_names),
            "only_in_pg": only_pg, "only_on_site": only_site}


# ── общий запуск ─────────────────────────────────────────────────────────────

def sync_all(dry: bool = False):
    started = datetime.now().isoformat(timespec="seconds")
    result = {"started": started}
    try:
        result["stock"] = sync_stock(dry=dry)
        result["sales"] = sync_sales(dry=dry)
        if not dry:
            try:
                import rebuild_history as rh
                result["settle"] = rh.settle_incoming(force=1)   # приёмки → вычет из «Заказано»
            except Exception as e:
                result["settle"] = {"error": str(e)}
        if not dry:
            import main as site
            conn = site.get_db()
            site.rebuild_analytics_json(conn)
            conn.close()
            result["caches"] = "rebuilt"
        result["ok"] = True
        _notify(f"✅ Синк ОК {started}\n"
                f"Остатки (МойСклад напрямую): {result['stock'].get('stock_skus')} SKU\n"
                f"Продажи: {result['sales'].get('sales_rows')} строк за {len(result['sales'].get('sales_days', []))} дн.",
                dry)
    except Exception as e:
        result["ok"] = False
        result["error"] = str(e)
        _notify(f"❌ Синк УПАЛ {started}: {e}", dry)
    return result


def _notify(text: str, dry: bool):
    if dry:
        return
    tok = os.environ.get("TG_TOKEN", "")
    chat = os.environ.get("TG_CHAT", "")
    if not tok or not chat:
        return
    try:
        import httpx
        httpx.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                   json={"chat_id": chat, "text": text}, timeout=10)
    except Exception:
        pass
