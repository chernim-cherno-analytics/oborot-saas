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
  E. Тот же шов в ПРОДОЛЖЕНИИ прерванной первичной загрузки с многодневным
     разрывом: отказ на продажах разрыва не публикует остатки этих дней и не
     двигает опубликованную границу истории. Сдвинутая граница здесь хуже
     самой дыры: следующий запуск перестаёт считать эти дни пропущенными.
  F. Однодневный разрыв при уже закрытом окне (window_done=true). Этот путь
     не грузил продажи нового дня ВООБЩЕ и не падал при этом: остатки дня
     публиковались с пустым числителем штатно. Проверяются обе половины —
     на отказе не публикуется ни одна, на успехе публикуются обе.

Сценарии E и F пришли из независимого ревью Codex на точном HEAD
(Issue #2, issuecomment-5406490164) — оба контрпримера воспроизведены здесь
детерминированно, а не приняты на слово.

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
from app import ms_sync as _mss  # noqa: E402  (подмена _today — см. interrupt_initial)
from app.db import SessionLocal  # noqa: E402
from app.main import app as oborot_app  # noqa: E402
from app.models import Product, Sale, StockDay  # noqa: E402
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


def newest_stock_date() -> str:
    """Самая свежая опубликованная дата остатков — та самая «граница» (E, F)."""
    db = SessionLocal()
    try:
        return str(db.execute(select(func.max(StockDay.date))).scalar() or "")
    finally:
        db.close()


def sale_qty_on(day: str, ext_id: str) -> float:
    """Сколько штук по этому SKU за этот день лежит в таблице sales."""
    db = SessionLocal()
    try:
        return float(db.execute(
            select(func.coalesce(func.sum(Sale.qty), 0.0))
            .select_from(Sale).join(Product, Product.id == Sale.product_id)
            .where(Sale.date == day, Product.ext_id == ext_id,
                   Sale.is_return.is_(False))
        ).scalar() or 0.0)
    finally:
        db.close()


def mock_sold_on(day: str, ext_id: str) -> float:
    """Столько же, но ПО МОКУ — эталон, с которым сверяется загруженное.

    Считать эталон по данным мока, а не по константе, обязательно: мир мока
    случайный (но с фиксированным seed), и «в базе 7 штук» ничего не доказало
    бы, если в этот день у SKU есть ещё и органическая продажа.
    """
    active = {"st-flag", "st-web"}
    total = 0.0
    for entity in ("retaildemand", "demand"):
        for doc in mock_ms.DOCS[entity]:
            if doc["moment"][:10] != day or doc.get("applicable") is False:
                continue
            if doc["store"]["meta"]["href"].rsplit("/", 1)[-1] not in active:
                continue
            for pos in doc["positions"]["rows"]:
                ext = pos["assortment"]["meta"]["href"].rsplit("/", 1)[-1].split("?")[0]
                if ext == ext_id:
                    total += float(pos["quantity"])
    return total


def inject_sale(day: str, ext_id: str, qty: float, price_rub: float) -> dict:
    """Дописать в мок продажу за конкретный день; вернуть документ для отката.

    Нужна затем, чтобы проверка «продажи дня опубликованы» не зависела от
    того, выпал ли в случайном мире мока документ именно на этот день: пустая
    таблица за день неотличима от «в этот день просто не продавали».
    """
    doc = {
        "id": f"probe-{day}",
        "meta": {"href": f"{mock_ms.BASE}/entity/retaildemand/probe-{day}",
                 "type": "retaildemand"},
        "moment": f"{day} 09:00:00",
        "store": {"meta": {"href": f"{mock_ms.BASE}/entity/store/st-flag",
                           "type": "store"}},
        "positions": {"rows": [{"assortment": {"meta": mock_ms._asm_meta(ext_id)},
                                "quantity": qty, "price": int(price_rub * 100),
                                "discount": 0}],
                      "meta": {"size": 1}},
    }
    mock_ms.DOCS["retaildemand"].append(doc)
    return doc


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


def interrupt_initial(c: httpx.Client, mock_api: httpx.Client, *,
                      shift_days: int, ok_before: int = 20) -> dict:
    """Прерванная первичная загрузка, «сегодня» которой было shift_days назад.

    ok_before=20 — ровно столько удачных ответов отчёта нужно, чтобы прошли
    «сегодня» (1 дата × 2 склада) и окно быстрого старта (9 дат × 2 склада).
    Окно закрывается ЦЕЛИКОМ, window_done становится true, а первый кусок
    истории ловит стойкий 429. Подмена ms_sync._today отодвигает «сегодня»
    прерванного запуска назад — так и получается resumed gap ровно в
    shift_days дней, ради которого написаны E и F.
    """
    _mss._today = lambda: TODAY - timedelta(days=shift_days)
    try:
        mock_api.post("/__test/faults",
                      json={"stock_ok_before": ok_before, "stock_429_burst": 100000})
        r = c.post("/api/sync/initial")
        check(f"подготовка: прерванная загрузка «{shift_days} дн. назад» запущена",
              r.status_code == 200, f"status={r.status_code}")
        st = wait_sync_done(c)
    finally:
        _mss._today = date.today
        mock_api.post("/__test/faults", json={})
    return st


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

    print("\n== E. Продолжение с многодневным разрывом ==")
    # Ревью Codex 25.08 (Issue #2, issuecomment-5406490164). Ветка
    # «продолжение прерванной первичной загрузки» осталась неатомарной, когда
    # окно, куски истории и инкремент уже починили: остатки пропущенных дней
    # коммитились, граница history_loaded_to уезжала на сегодня, и только
    # ПОТОМ отдельным вызовом ехали продажи. Отказ между этими точками
    # оставлял опубликованные дни в знаменателе «дней в стоке» без своих
    # продаж — и вылечить это было уже нечем: следующий запуск видел границу
    # продвинутой и пропущенными эти дни больше не считал.
    st = interrupt_initial(c, mock_api, shift_days=3)
    stats_e = st.get("stats") or {}
    check("(E) подготовка: загрузка прервана «три дня назад», окно закрыто",
          st.get("state") == "error" and stats_e.get("window_done") is True
          and stats_e.get("history_loaded_to") == d(3),
          f"state={st.get('state')} window_done={stats_e.get('window_done')} "
          f"history_loaded_to={stats_e.get('history_loaded_to')} ожидалось {d(3)}")
    check("(E) подготовка: опубликованная граница остатков — день падения",
          newest_stock_date() == d(3),
          f"max(StockDay.date)={newest_stock_date()}, ожидалось {d(3)}")

    mock_api.post("/__test/faults", json={"docs_429_burst": 100000})
    r = c.post("/api/sync/run")
    check("(E) продолжение запущено", r.status_code == 200, f"status={r.status_code}")
    st = wait_sync_done(c)
    mock_api.post("/__test/faults", json={})
    check("(E) продолжение упало на продажах разрыва", st.get("state") == "error",
          f"state={st.get('state')} error={str(st.get('error'))[:120]}")
    stats_e2 = st.get("stats") or {}
    check("(E) ОСТАТКИ ЗА ДНИ РАЗРЫВА НЕ ОПУБЛИКОВАНЫ БЕЗ СВОИХ ПРОДАЖ",
          newest_stock_date() == d(3),
          f"самая свежая дата остатков={newest_stock_date()}, ожидалась {d(3)}: "
          f"продажи за {d(2)}…{d(0)} не доехали, значит и остатков за эти дни "
          f"в базе быть не должно")
    check("(E) ОПУБЛИКОВАННАЯ ГРАНИЦА ИСТОРИИ НЕ СДВИНУЛАСЬ",
          stats_e2.get("history_loaded_to") == d(3),
          f"history_loaded_to={stats_e2.get('history_loaded_to')}, ожидалось {d(3)}: "
          f"сдвинутая граница означает, что следующий запуск не сочтёт эти дни "
          f"пропущенными и продажи за них не догрузит уже никогда")

    r = c.post("/api/sync/run")
    st = wait_sync_done(c)
    check("(E) контроль: продолжение без сбоя добирает разрыв до конца",
          st.get("state") == "done" and newest_stock_date() == d(0),
          f"state={st.get('state')} max(StockDay.date)={newest_stock_date()} "
          f"error={str(st.get('error'))[:120]}")

    print("\n== F. Однодневный разрыв при уже закрытом окне ==")
    # Второй контрпример того же ревью, и он опаснее первого: при разрыве
    # ровно в один день и выставленном window_done продажи нового дня не
    # грузились этим путём ВООБЩЕ. Никакого сбоя при этом не происходило —
    # день остатков штатно ложился в базу с пустым числителем.
    st = interrupt_initial(c, mock_api, shift_days=1)
    stats_f = st.get("stats") or {}
    check("(F) подготовка: загрузка прервана «вчера», окно закрыто",
          st.get("state") == "error" and stats_f.get("window_done") is True
          and stats_f.get("history_loaded_to") == d(1),
          f"state={st.get('state')} window_done={stats_f.get('window_done')} "
          f"history_loaded_to={stats_f.get('history_loaded_to')} ожидалось {d(1)}")
    check("(F) подготовка: сегодняшних остатков в базе ещё нет",
          newest_stock_date() == d(1),
          f"max(StockDay.date)={newest_stock_date()}, ожидалось {d(1)}")

    mock_api.post("/__test/faults", json={"docs_429_burst": 100000})
    r = c.post("/api/sync/run")
    check("(F) продолжение с одним пропущенным днём запущено", r.status_code == 200,
          f"status={r.status_code}")
    st = wait_sync_done(c)
    mock_api.post("/__test/faults", json={})
    check("(F) продолжение упало на продажах", st.get("state") == "error",
          f"state={st.get('state')} error={str(st.get('error'))[:120]}")
    check("(F) ПРИ ОТКАЗЕ ОСТАТКИ ЗА ДЕНЬ НЕ ОПУБЛИКОВАНЫ В ОДИНОЧКУ",
          newest_stock_date() == d(1),
          f"самая свежая дата остатков={newest_stock_date()}, ожидалась {d(1)}: "
          f"продажи за {d(0)} не доехали, значит и остатков за {d(0)} быть не должно")

    probe = next(s for s in mock_ms.SKUS
                 if s["kind"] == "variant" and "service" not in s["flags"]
                 and "archived" not in s["flags"])
    doc = inject_sale(d(0), probe["ext"], 7.0, probe["price"])
    try:
        expected = mock_sold_on(d(0), probe["ext"])
        check("(F) подготовка: продажа за этот день в моке есть, в базе — нет",
              expected >= 7.0 and sale_qty_on(d(0), probe["ext"]) == 0.0,
              f"в моке {expected} шт, в базе {sale_qty_on(d(0), probe['ext'])} шт")
        r = c.post("/api/sync/run")
        check("(F) продолжение без сбоя запущено", r.status_code == 200,
              f"status={r.status_code}")
        st = wait_sync_done(c)
        check("(F) продолжение без сбоя дошло до done", st.get("state") == "done",
              f"state={st.get('state')} error={str(st.get('error'))[:120]}")
        check("(F) остатки за день опубликованы", newest_stock_date() == d(0),
              f"max(StockDay.date)={newest_stock_date()}, ожидалось {d(0)}")
        check("(F) ВМЕСТЕ С НИМИ ОПУБЛИКОВАНЫ ПРОДАЖИ ЗА ТОТ ЖЕ ДЕНЬ",
              sale_qty_on(d(0), probe["ext"]) == expected,
              f"по {probe['ext']} за {d(0)} в базе {sale_qty_on(d(0), probe['ext'])} шт "
              f"против {expected} шт в моке: день остатков опубликован с пустым "
              f"числителем — «дней в стоке» на день больше, чем дней с продажами, "
              f"темп занижен, «хватит на N дней» завышено")
    finally:
        mock_ms.DOCS["retaildemand"].remove(doc)

    c.close()
    mock_api.close()
    print(f"\nИТОГО: {len(PASS)} OK, {len(FAIL)} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
