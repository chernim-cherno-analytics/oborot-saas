# -*- coding: utf-8 -*-
"""DATA-3. Остатки и продажи одного куска истории публикуются вместе.

Дефект. Оборачиваемость — «нетто-выручка за год / дни в стоке», и эти две
величины лежат в РАЗНЫХ таблицах. Первичная загрузка идёт кусками назад от
сегодня, и каждый кусок публиковался в ДВА приёма:

    1) _write_stock_rows(chunk)        — коммит остатков куска;
    2) await _sync_sales(chunk)        — сеть, десятки секунд, коммит в конце.

Граница загруженной истории берётся аналитикой прямо из таблицы остатков
(app/analytics.py: coverage_start = min(StockDay.date)), поэтому сразу после
шага 1 знаменатель «дней в стоке» получает дни, продаж за которые в базе ещё
нет. Темп и оборачиваемость занижаются: клиент видит завышенное «хватит на N
дней» и недозаказывает. Штатно это длится всю пересборку (для уже активной
организации — прямо на глазах у человека), а падение между шагами (429, OOM,
рестарт, деплой) оставляет такую базу до следующего успешного прогона.

Инвариант, который проверяется здесь:
    В БАЗЕ НЕ СУЩЕСТВУЕТ ДНЯ ОСТАТКОВ, ПРОДАЖИ ЗА КОТОРЫЙ НЕ ЗАГРУЖЕНЫ.

Сценарии:
  A. Продажи СТАРОГО куска не доезжают (мок отвечает 429 на документы за
     периоды раньше заданной даты). Проверяем, что история остатков не ушла
     глубже последнего куска, чьи продажи доехали, и что глубина, показанная
     пользователю (coverage_days), совпадает с тем, что видит аналитика.
  B. То же на шве фазы «окно месяца»: продажи окна не доезжают вовсе —
     в базе обязан остаться ровно один день остатков (сегодня), а не тридцать
     дней остатков с нулём продаж («товар не продаётся, заказывать не нужно»).
  C. Продолжение после обоих падений добирает всё до конца — атомарность
     не стоит потерянных данных.
  D. Тот же шов в инкременте: остатки за сегодня не обновляются в одиночку,
     если продажи за тот же день не доехали.

Свой мок на отдельном порту (9811): файл можно запускать параллельно с
tests/test_sync.py (9800) и tests/test_sync_wipe.py (9810).

Запуск из корня репозитория:  python tests/test_sync_atomic.py
"""
import os
import sys
import threading
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

DB_PATH = ROOT / "test_sync_atomic.db"
APP_PORT = int(os.environ.get("OBOROT_TEST_PORT", "8809"))
MOCK_PORT = int(os.environ.get("OBOROT_MOCK_PORT", "9811"))

os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["MS_BASE_URL"] = f"http://127.0.0.1:{MOCK_PORT}"
os.environ["SCHEDULER_ENABLED"] = "0"
# Мир: 60 дат, окно быстрого старта 10 дней, куски истории по 10 дат.
# Куски от свежих к старым: [-19…-10], [-29…-20], [-39…-30], [-49…-40], [-59…-50].
os.environ["HISTORY_DAYS"] = "60"
os.environ["INITIAL_WINDOW_DAYS"] = "10"
os.environ["STOCK_CHUNK_DATES"] = "10"
os.environ["MS_CHUNK_PAUSE"] = "0"
os.environ["MS_MAX_RETRIES"] = "2"

if DB_PATH.exists():
    DB_PATH.unlink()

import httpx  # noqa: E402
import uvicorn  # noqa: E402

import mock_ms  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.main import app as oborot_app  # noqa: E402
from app.models import Sale, StockDay  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

mock_ms.PORT = MOCK_PORT

TODAY = date.today()


def d(offset: int) -> str:
    """Дата «сегодня минус offset» в ISO."""
    return (TODAY - timedelta(days=offset)).isoformat()


# Документы за периоды, начинающиеся раньше этой даты, будут падать с 429.
# Кусок [-39…-30] запрашивает продажи с -39 → падает; кусок [-29…-20]
# запрашивает с -29 → проходит. Значит последний целиком опубликованный
# кусок кончается на -29, и глубже остатков быть не должно.
FAULT_BEFORE = d(30)
EXPECTED_DEEPEST = d(29)
EXPECTED_DATES = 30  # -29 … сегодня включительно


class ServerThread:
    def __init__(self, asgi_app, port: int):
        self.config = uvicorn.Config(asgi_app, host="127.0.0.1", port=port,
                                     log_level="warning")
        self.server = uvicorn.Server(self.config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self):
        self.thread.start()
        deadline = time.time() + 15
        while time.time() < deadline:
            if self.server.started:
                return
            time.sleep(0.05)
        raise RuntimeError(f"сервер на порту {self.config.port} не поднялся")

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=10)


PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        PASS.append(name)
        print(f"  OK   {name}" + (f"  [{detail}]" if detail else ""))
    else:
        FAIL.append(name)
        print(f"  FAIL {name}  {detail}")


def stock_facts() -> tuple[int, str, int]:
    """(дат остатков, самая старая дата остатков, строк продаж)."""
    db = SessionLocal()
    try:
        dates = db.execute(select(func.count(func.distinct(StockDay.date)))).scalar() or 0
        oldest = db.execute(select(func.min(StockDay.date))).scalar() or ""
        sales = db.execute(select(func.count()).select_from(Sale)).scalar() or 0
        return int(dates), str(oldest), int(sales)
    finally:
        db.close()


def today_qty() -> float:
    """Сумма остатков за сегодня — по ней видно, обновились ли остатки."""
    db = SessionLocal()
    try:
        return float(db.execute(
            select(func.coalesce(func.sum(StockDay.qty), 0.0))
            .where(StockDay.date == d(0))).scalar() or 0.0)
    finally:
        db.close()


def sales_days() -> int:
    db = SessionLocal()
    try:
        return int(db.execute(select(func.count(func.distinct(Sale.date)))).scalar() or 0)
    finally:
        db.close()


def wait_sync_done(client: httpx.Client, timeout: float = 300.0) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        last = client.get("/api/sync/status").json()
        if last.get("state") in ("done", "error"):
            return last
        time.sleep(1.0)
    return last


def main() -> int:
    mock_srv = ServerThread(mock_ms.app, MOCK_PORT)
    app_srv = ServerThread(oborot_app, APP_PORT)
    mock_srv.start()
    app_srv.start()
    try:
        return run()
    finally:
        app_srv.stop()
        mock_srv.stop()
        if DB_PATH.exists():
            DB_PATH.unlink()


def run() -> int:
    base = f"http://127.0.0.1:{APP_PORT}"
    c = httpx.Client(headers={"X-Oborot-CSRF": "1"}, base_url=base, timeout=120.0)
    mock_api = httpx.Client(base_url=f"http://127.0.0.1:{MOCK_PORT}", timeout=30.0)

    print("\n== Подготовка: обычный первичный синк ==")
    r = c.post("/register", data={"name": "Владелец", "email": "owner@test.io",
                                  "password": "secret123", "org_name": "Бренд"})
    check("регистрация", r.status_code in (200, 302, 303), f"status={r.status_code}")
    r = c.post("/api/connect/moysklad", json={"token": mock_ms.TOKEN})
    check("токен принят", r.status_code == 200, f"status={r.status_code}")
    c.post("/api/connect/moysklad/stores", json={"ext_ids": ["st-flag", "st-web"]})
    r = c.post("/api/sync/initial")
    check("первичный синк запущен", r.status_code == 200, f"status={r.status_code}")
    st = wait_sync_done(c)
    check("первичный синк дошёл до done", st.get("state") == "done",
          f"state={st.get('state')} error={str(st.get('error'))[:120]}")
    dates0, oldest0, sales0 = stock_facts()
    check("в базе полная история остатков", dates0 >= 60, f"дат={dates0} с {oldest0}")
    check("в базе есть продажи", sales0 > 0, f"строк={sales0}")

    print("\n== A. Продажи старого куска не доезжают ==")
    mock_api.post("/__test/faults", json={"docs_429_before": FAULT_BEFORE})
    r = c.post("/api/sync/initial")
    check("полная пересборка запущена", r.status_code == 200, f"status={r.status_code}")
    st = wait_sync_done(c)
    check("пересборка упала на продажах старого куска", st.get("state") == "error",
          f"state={st.get('state')} error={str(st.get('error'))[:120]}")

    dates1, oldest1, sales1 = stock_facts()
    check("ОСТАТКИ НЕ УШЛИ ГЛУБЖЕ КУСКА, ЧЬИ ПРОДАЖИ ДОЕХАЛИ",
          oldest1 == EXPECTED_DEEPEST,
          f"самая старая дата остатков={oldest1}, ожидалась {EXPECTED_DEEPEST}; "
          f"продажи за {d(39)}…{d(30)} не загружены, значит и остатков "
          f"за эти дни в базе быть не должно")
    check("дней остатков ровно столько, сколько опубликовано кусками",
          dates1 == EXPECTED_DATES, f"дат={dates1}, ожидалось {EXPECTED_DATES}")
    check("продажи за загруженные дни на месте", sales1 > 0, f"строк={sales1}")

    st = c.get("/api/sync/status").json()
    stats = st.get("stats") or {}
    cov = int(stats.get("coverage_days") or 0)
    check("ГЛУБИНА, ПОКАЗАННАЯ ЧЕЛОВЕКУ, СОВПАДАЕТ С ТЕМ, ЧТО СЧИТАЕТ АНАЛИТИКА",
          cov == dates1,
          f"в статусе синка coverage_days={cov}, в таблице остатков {dates1} дат "
          f"(аналитика берёт границу истории из min(StockDay.date) — расхождение "
          f"означает, что знаменатель «дней в стоке» шире загруженных продаж)")

    # Продуктовая проверка: числа на странице оборачиваемости не могут
    # опираться на большее число дней, чем реально загружено.
    tt = c.get("/api/turnover")
    check("страница оборачиваемости отвечает и после падения", tt.status_code == 200,
          f"status={tt.status_code}")
    items = tt.json().get("items", []) if tt.status_code == 200 else []
    worst = max((int(it.get("dis") or 0) for it in items), default=0)
    check("«ДНЕЙ В СТОКЕ» НЕ БОЛЬШЕ, ЧЕМ ДНЕЙ С ЗАГРУЖЕННЫМИ ПРОДАЖАМИ",
          worst <= cov,
          f"максимум dis={worst} при {cov} днях загруженных продаж: знаменатель "
          f"шире числителя ровно на непривезённый кусок, темп занижен, "
          f"«хватит на N дней» завышено")

    print("\n== B. Продажи окна месяца не доезжают вовсе ==")
    mock_api.post("/__test/faults", json={"docs_429_burst": 100000})
    r = c.post("/api/sync/initial")
    check("вторая пересборка запущена", r.status_code == 200, f"status={r.status_code}")
    st = wait_sync_done(c)
    check("пересборка упала на продажах окна", st.get("state") == "error",
          f"state={st.get('state')} error={str(st.get('error'))[:120]}")
    dates2, oldest2, sales2 = stock_facts()
    check("В БАЗЕ ОСТАЛСЯ РОВНО ОДИН ДЕНЬ ОСТАТКОВ, А НЕ МЕСЯЦ БЕЗ ПРОДАЖ",
          dates2 == 1 and oldest2 == d(0),
          f"дат={dates2} самая старая={oldest2}; месяц остатков при нуле продаж "
          f"читается как «товар не продаётся» и обнуляет заказ")
    check("продаж нет — они стёрты вместе с историей", sales2 == 0, f"строк={sales2}")

    print("\n== C. Продолжение добирает всё ==")
    mock_api.post("/__test/faults", json={})
    for attempt in (1, 2):
        r = c.post("/api/sync/run")
        check(f"продолжение {attempt} запущено", r.status_code == 200,
              f"status={r.status_code}")
        st = wait_sync_done(c)
        if st.get("state") == "done":
            break
    check("продолжение дошло до done", st.get("state") == "done",
          f"state={st.get('state')} error={str(st.get('error'))[:120]}")
    dates3, oldest3, sales3 = stock_facts()
    check("история остатков восстановлена целиком", dates3 >= dates0,
          f"было={dates0} стало={dates3} с {oldest3}")
    check("продажи восстановлены", sales3 > 0, f"было={sales0} стало={sales3}")
    check("атомарность не съела ни одного дня продаж", sales_days() > 1,
          f"дней с продажами={sales_days()}")

    print("\n== D. Инкремент тоже публикует обе половины вместе ==")
    # Инкремент каждый час дописывает новый день остатков и переписывает окно
    # продаж. Раньше это были два коммита: между ними «дней в стоке» на день
    # больше, чем дней с загруженными продажами. Меняем остаток за сегодня в
    # моке и роняем документы: обновиться в одиночку остатки не имеют права.
    qty_before = today_qty()
    key = (d(0), "st-flag")
    snapshot = dict(mock_ms.STOCK_BY_DAY.get(key) or {})
    victim = next(iter(snapshot), "")
    check("подготовка: в моке есть остаток за сегодня", bool(victim),
          f"позиций в срезе={len(snapshot)}")
    if victim:
        mock_ms.STOCK_BY_DAY[key][victim] = snapshot[victim] + 500
    try:
        mock_api.post("/__test/faults", json={"docs_429_burst": 100000})
        r = c.post("/api/sync/run")
        check("инкремент запущен", r.status_code == 200, f"status={r.status_code}")
        st = wait_sync_done(c)
        check("инкремент упал на продажах", st.get("state") == "error",
              f"state={st.get('state')} error={str(st.get('error'))[:120]}")
        check("ОСТАТКИ НЕ ОБНОВИЛИСЬ В ОДИНОЧКУ, БЕЗ ПРОДАЖ ЗА ТОТ ЖЕ ДЕНЬ",
              today_qty() == qty_before,
              f"сумма остатков за сегодня была {qty_before}, стала {today_qty()} "
              f"(в моке остаток вырос на 500): новый остаток записан, а продажи "
              f"за тот же день не доехали")
    finally:
        if victim:
            mock_ms.STOCK_BY_DAY[key] = snapshot
        mock_api.post("/__test/faults", json={})

    r = c.post("/api/sync/run")
    st = wait_sync_done(c)
    check("следующий инкремент проходит нормально", st.get("state") == "done",
          f"state={st.get('state')} error={str(st.get('error'))[:120]}")
    check("данные сошлись обратно", today_qty() == qty_before,
          f"было={qty_before} стало={today_qty()}")

    c.close()
    mock_api.close()
    print(f"\nИТОГО: {len(PASS)} OK, {len(FAIL)} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
