# -*- coding: utf-8 -*-
"""Регрессия DATA-8 (третий сценарий + corrective): sales_docs_skipped_store.

`_collect_sales` (`app/ms_sync.py`) считает `stats["sales_docs_skipped_store"]`
— документы продаж, чей склад (`store` документа) не распознан вообще ИЛИ не
входит в `active_store_ids` организации (выбранные склады). Это НЕ трогается
этим тестом: отбор продаж и сам ephemeral-счётчик каждого прогона — канон.

ДВЕ части теста.

Часть 1 (СХЕМА, как было изначально) — `sync_state.stats_json` организации
пишется напрямую, без прогона синка: точный API-контракт `diagnostics.
sales_docs_skipped_store_unresolved` (owner-only, fail-closed на некорректных
значениях, изоляция между организациями, 403 участнику, отсутствие утечки в
member-visible /api/sync/progress).

Часть 2 (ЖИЗНЕННЫЙ ЦИКЛ, corrective — BLOCKED issuecomment-5438193835) —
доказывает то, что часть 1 доказать не может: прогоняет НАСТОЯЩИЙ синк через
мок МойСклад. Первая версия правки читала diagnostics ПРЯМО из ephemeral
`sales_docs_skipped_store`, который `_collect_sales` обнуляет на каждом
прогоне (`if replace_all or not initial: ... = 0`) — обычный успешный
инкремент, перечитывающий только свой узкий хвост (DATA-4: старые документы
никогда не перечитываются), тем самым тихо гасил предупреждение о прежних
пропусках, ни капли их не почеркив. Часть 2 проверяет sticky-факт
`sales_docs_skipped_store_unresolved` (отдельно именованный ключ stats,
переживающий перезапуск через `_CARRIED_STATS`, обновляется только в
`_apply_skip_diagnostic_lifecycle` из финализации `_run_sync`):
  * первичный прогон с пропусками -> факт положительный (авторитетно, полный
    несрезюмированный проход по истории);
  * следующий инкремент — СТАРТ (ещё running) и ЗАВЕРШЕНИЕ с честным нулём
    (пропущенный документ вне узкого окна инкремента) -> факт НЕ гаснет;
  * первичная пересборка, упавшая до финализации (429 без ретраев) -> факт
    НЕ трогается (упавший прогон до `_apply_skip_diagnostic_lifecycle` не
    доходит);
  * успешная НЕрезюмированная пересборка (`/api/sync/initial`, force_full)
    после устранения проблемных документов, с авторитетным нулём -> факт
    гасится.

Мок МойСклад дополнен двумя test-only ручками (`/__test/skip_store_doc`,
`/__test/reset_skip_store_docs`) — без них нельзя детерминированно посадить
документ на склад, которого нет в активном наборе организации, не трогая
сид сценария остальных наборов синка.

Запуск из корня репозитория:  python tests/test_sync_diag_store.py
"""
import json
import os
import sqlite3
import sys
import threading
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

DB_PATH = ROOT / "test_sync_diag_store.db"
APP_PORT = int(os.environ.get("OBOROT_TEST_PORT", "8818"))
MOCK_PORT = int(os.environ.get("OBOROT_MOCK_PORT", "9817"))

os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["MS_BASE_URL"] = f"http://127.0.0.1:{MOCK_PORT}"
os.environ["SCHEDULER_ENABLED"] = "0"
# Малые окна — быстрый прогон; SYNC_DAYS_BACK держит инкремент строго уже
# HISTORY_DAYS, чтобы можно было детерминированно посадить документ ВНЕ
# окна инкремента, но ВНУТРИ окна первичной загрузки (см. SKIP_DATE ниже).
os.environ["HISTORY_DAYS"] = "10"
os.environ["INITIAL_WINDOW_DAYS"] = "5"
os.environ["STOCK_CHUNK_DATES"] = "5"
os.environ["SYNC_DAYS_BACK"] = "2"
os.environ["MS_CHUNK_PAUSE"] = "0"
os.environ["MS_MAX_RETRIES"] = "1"  # быстрый детерминированный fail на 429-фолте

if DB_PATH.exists():
    DB_PATH.unlink()

import bcrypt  # noqa: E402
import httpx  # noqa: E402
import uvicorn  # noqa: E402

import mock_ms  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.main import app as oborot_app  # noqa: E402
from app.models import Org  # noqa: E402
from sqlalchemy import select  # noqa: E402

mock_ms.PORT = MOCK_PORT
mock_ms.BASE = f"http://127.0.0.1:{MOCK_PORT}"

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
        print(f"  OK   {name}" + (f"  [{detail}]" if detail else ""))
    else:
        FAIL.append(name)
        print(f"  FAIL {name}  {detail}")


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


BASE = f"http://127.0.0.1:{APP_PORT}"


def client() -> httpx.Client:
    return httpx.Client(headers={"X-Oborot-CSRF": "1"}, base_url=BASE, timeout=60.0)


def sql(query: str, *args):
    con = sqlite3.connect(DB_PATH)
    try:
        return con.execute(query, args).fetchall()
    finally:
        con.close()


def exec_sql(query: str, *args) -> None:
    con = sqlite3.connect(DB_PATH)
    try:
        con.execute(query, args)
        con.commit()
    finally:
        con.close()


def register(c: httpx.Client, email: str, org_name: str, password: str = "secret123"):
    return c.post("/register", data={
        "name": email.split("@")[0], "email": email,
        "password": password, "org_name": org_name,
    })


def login(c: httpx.Client, email: str, password: str = "secret123"):
    return c.post("/login", data={"email": email, "password": password})


def add_member(org_id: int, email: str) -> None:
    pw = bcrypt.hashpw(b"secret123", bcrypt.gensalt()).decode()
    con = sqlite3.connect(DB_PATH)
    try:
        cur = con.execute(
            "INSERT INTO users (email, pw_hash, name, created_at) VALUES (?,?,?,datetime('now'))",
            (email, pw, email.split("@")[0]))
        uid = cur.lastrowid
        con.execute("INSERT INTO memberships (user_id, org_id, role) VALUES (?,?,'member')",
                     (uid, org_id))
        con.commit()
    finally:
        con.close()


def setup_org(c: httpx.Client, email: str, org_name: str) -> int:
    r = register(c, email, org_name)
    assert r.status_code in (200, 302, 303), (email, r.status_code)
    r = c.post("/api/connect/demo")
    assert r.status_code == 200, (email, r.status_code, r.text[:200])
    row = sql("SELECT id FROM orgs WHERE name=?", org_name)
    return row[0][0]


def set_stats(org_id: int, stats_raw: str, *, state: str = "done") -> None:
    """Пишет sync_state организации напрямую — без прогона синка (часть 1)."""
    exec_sql("DELETE FROM sync_state WHERE org_id=?", org_id)
    exec_sql(
        "INSERT INTO sync_state (org_id, state, mode, stage, progress, detail, "
        "stats_json, error, fail_streak, alerted_streak) "
        "VALUES (?, ?, 'incremental', '', 100.0, '', ?, '', 0, 0)",
        org_id, state, stats_raw,
    )


def stats_with(value_json: str) -> str:
    """Валидный по форме JSON stats с ОДНИМ полем
    sales_docs_skipped_store_unresolved=<value_json> подставленным как сырой
    JSON-литерал (позволяет засеять некорректные типы — отрицательные числа,
    bool, строку, отсутствие поля вовсе)."""
    if value_json is None:
        return json.dumps({"sales_docs": 10, "sales_rows": 8})
    return ('{"sales_docs": 10, "sales_rows": 8, '
            '"sales_docs_skipped_store_unresolved": ' + value_json + "}")


def register_and_connect_ms(c: httpx.Client, email: str, org_name: str,
                            store_ids: list[str]) -> int:
    """Регистрация + подключение МС мока + выбор складов (часть 2, реальный синк)."""
    r = register(c, email, org_name)
    assert r.status_code in (200, 302, 303), (email, r.status_code)
    r = c.post("/api/connect/moysklad", json={"token": mock_ms.TOKEN})
    assert r.status_code == 200, (email, r.status_code, r.text[:200])
    r = c.post("/api/connect/moysklad/stores", json={"ext_ids": store_ids})
    assert r.status_code == 200, (email, r.status_code, r.text[:200])
    db = SessionLocal()
    try:
        org = db.execute(select(Org).where(Org.name == org_name)).scalar_one()
        return org.id
    finally:
        db.close()


def wait_sync_done(c: httpx.Client, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        last = c.get("/api/sync/status").json()
        if last.get("state") in ("done", "error"):
            return last
        time.sleep(0.2)
    return last


def diag(resp_json: dict):
    return (resp_json.get("diagnostics") or {}).get("sales_docs_skipped_store_unresolved")


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
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(DB_PATH) + suffix)
            if p.exists():
                p.unlink()


def run_part1_schema() -> None:
    a = client()
    org_a = setup_org(a, "owner-a@diag.test", "Организация A (diag)")
    b = client()
    org_b = setup_org(b, "owner-b@diag.test", "Организация B (diag)")
    add_member(org_a, "member-a@diag.test")
    m = client()
    login(m, "member-a@diag.test")

    print("\n== [часть 1] Точный положительный счётчик виден владельцу ==")
    set_stats(org_a, stats_with("7"))
    st = a.get("/api/sync/status").json()
    check("owner-only /api/sync/status: diagnostics.sales_docs_skipped_store_unresolved == 7",
          diag(st) == 7, f"diagnostics={st.get('diagnostics')}")
    check("тип значения — именно int, не bool/str",
          type(diag(st)) is int, f"type={type(diag(st))}")

    print("\n== [часть 1] Ноль — валидное значение, но не тревога ==")
    set_stats(org_a, stats_with("0"))
    st0 = a.get("/api/sync/status").json()
    check("diagnostics.sales_docs_skipped_store_unresolved == 0 (точно, не подделка)",
          diag(st0) == 0, f"diagnostics={st0.get('diagnostics')}")

    print("\n== [часть 1] Отсутствующее/некорректное значение — fail-closed None, НЕ 0 ==")
    cases = [
        ("поле отсутствует вовсе", None),
        ("отрицательное число", "-1"),
        ("bool true", "true"),
        ("bool false", "false"),
        ("строка", '"3"'),
        ("float", "3.5"),
        ("null", "null"),
    ]
    for title, raw in cases:
        set_stats(org_a, stats_with(raw))
        st_bad = a.get("/api/sync/status").json()
        check(f"malformed ({title}) -> diagnostics is None, не 0",
              diag(st_bad) is None, f"diagnostics={st_bad.get('diagnostics')}")

    print("\n== [часть 1] Схема диагностики ограничена одним именованным полем ==")
    set_stats(org_a, stats_with("4"))
    st_schema = a.get("/api/sync/status").json()
    d = st_schema.get("diagnostics")
    check("diagnostics — словарь ровно с одним ключом sales_docs_skipped_store_unresolved",
          isinstance(d, dict) and set(d.keys()) == {"sales_docs_skipped_store_unresolved"},
          f"diagnostics={d}")

    print("\n== [часть 1] Организация A не видит значение организации B ==")
    set_stats(org_a, stats_with("7"))
    set_stats(org_b, stats_with("13"))
    st_a = a.get("/api/sync/status").json()
    st_b = b.get("/api/sync/status").json()
    check("owner A видит СВОЁ значение (7), не значение B",
          diag(st_a) == 7, f"diagnostics_a={st_a.get('diagnostics')}")
    check("owner B видит СВОЁ значение (13), не значение A",
          diag(st_b) == 13, f"diagnostics_b={st_b.get('diagnostics')}")

    print("\n== [часть 1] Участник: /api/sync/status по-прежнему 403 ==")
    r_member_status = m.get("/api/sync/status")
    check("участник получает 403 на owner-only /api/sync/status",
          r_member_status.status_code == 403, f"status={r_member_status.status_code}")

    print("\n== [часть 1] Публичный /api/sync/progress не раскрывает диагностику и stats ==")
    r_prog_member = m.get("/api/sync/progress")
    check("участник получает 200 на /api/sync/progress",
          r_prog_member.status_code == 200, f"status={r_prog_member.status_code}")
    prog_member = r_prog_member.json()
    check("в ответе участнику НЕТ ключа diagnostics",
          "diagnostics" not in prog_member, f"keys={sorted(prog_member.keys())}")
    check("в ответе участнику НЕТ ключа stats",
          "stats" not in prog_member, f"keys={sorted(prog_member.keys())}")

    r_prog_owner = a.get("/api/sync/progress")
    prog_owner = r_prog_owner.json()
    check("в ответе владельцу на /api/sync/progress тоже НЕТ diagnostics",
          "diagnostics" not in prog_owner, f"keys={sorted(prog_owner.keys())}")
    check("в ответе владельцу на /api/sync/progress тоже НЕТ stats",
          "stats" not in prog_owner, f"keys={sorted(prog_owner.keys())}")

    a.close()
    b.close()
    m.close()


def run_part2_lifecycle() -> None:
    """Corrective: реальный синк через мок МойСклад, sticky-факт по стадиям."""
    c = client()
    mock_api = httpx.Client(base_url=f"http://127.0.0.1:{MOCK_PORT}", timeout=30.0)
    all_stores = [sid for sid, _ in mock_ms.STORES]  # все реальные склады активны:
    # никакой НАСТОЯЩИЙ документ мира мока тогда не считается пропущенным —
    # пропущенными окажутся ТОЛЬКО наши синтетические документы на
    # несуществующий склад SKIP_STORE, и итоговый счётчик детерминирован.
    org_id = register_and_connect_ms(c, "owner-c@diag.test", "Организация C (lifecycle)",
                                     all_stores)

    SKIP_STORE = "st-ghost-test"
    # HISTORY_DAYS=10, SYNC_DAYS_BACK=2 (окно инкремента — только today/today-1):
    # today-5 гарантированно ВНУТРИ первичного окна истории и ГАРАНТИРОВАННО
    # ВНЕ окна инкремента — на нём и строится доказательство «инкремент не
    # перечитывает старое».
    skip_date = (date.today() - timedelta(days=5)).isoformat()
    mock_api.post("/__test/skip_store_doc", json={
        "entity": "demand", "store": SKIP_STORE, "date": skip_date, "count": 3,
    })

    print("\n== [часть 2] Первичная загрузка с пропусками -> факт положительный (авторитетно) ==")
    r = c.post("/api/sync/initial")
    check("первичный синк запущен", r.status_code == 200, f"status={r.status_code}")
    st = wait_sync_done(c)
    check("первичный синк дошёл до done",
          st.get("state") == "done", f"state={st.get('state')} error={st.get('error')}")
    check("sticky факт == 3 (полный несрезюмированный прогон, авторитетно)",
          diag(st) == 3, f"diagnostics={st.get('diagnostics')}")

    print("\n== [часть 2] Инкремент: СТАРТ сохраняет факт (не гасит на queued/running) ==")
    mock_api.post("/__test/faults", json={"stock_delay_ms": 300})
    r = c.post("/api/sync/run")
    check("инкремент запущен", r.status_code == 200, f"status={r.status_code}")
    st_start = c.get("/api/sync/status").json()
    check("сразу после старта факт всё ещё == 3 (carried, не сброшен в None/0)",
          diag(st_start) == 3,
          f"state={st_start.get('state')} diagnostics={st_start.get('diagnostics')}")

    print("\n== [часть 2] Инкремент: честный ноль СВОЕГО окна НЕ гасит старый факт ==")
    st_inc = wait_sync_done(c)
    mock_api.post("/__test/faults", json={})  # снять stock_delay_ms
    check("инкремент дошёл до done",
          st_inc.get("state") == "done",
          f"state={st_inc.get('state')} error={st_inc.get('error')}")
    raw_inc = (st_inc.get("stats") or {}).get("sales_docs_skipped_store")
    check("СВОЙ счётчик этого инкремента честно 0 (окно не видит skip_date)",
          raw_inc == 0, f"raw={raw_inc}")
    check("sticky факт остался 3 — честный ноль инкремента его НЕ погасил",
          diag(st_inc) == 3, f"diagnostics={st_inc.get('diagnostics')}")

    print("\n== [часть 2] Упавшая (партиальная) пересборка НЕ трогает факт ==")
    mock_api.post("/__test/faults", json={"assortment_429_burst": 10})
    r = c.post("/api/sync/initial")
    check("пересборка запущена", r.status_code == 200, f"status={r.status_code}")
    st_failed = wait_sync_done(c)
    mock_api.post("/__test/faults", json={})  # снять фолт для следующих шагов
    check("пересборка честно упала (error), не done с выдумкой",
          st_failed.get("state") == "error", f"state={st_failed.get('state')}")
    check("факт после провала остался 3 — partial rebuild не чистит sticky",
          diag(st_failed) == 3, f"diagnostics={st_failed.get('diagnostics')}")

    print("\n== [часть 2] Успешная НЕрезюмированная пересборка с авторитетным 0 -> факт гасится ==")
    mock_api.post("/__test/reset_skip_store_docs")
    r = c.post("/api/sync/initial")
    check("вторая (успешная) пересборка запущена", r.status_code == 200,
          f"status={r.status_code}")
    st_clean = wait_sync_done(c)
    check("успешная пересборка дошла до done",
          st_clean.get("state") == "done",
          f"state={st_clean.get('state')} error={st_clean.get('error')}")
    check("сама пересборка честно нашла 0 пропусков (документы убраны)",
          (st_clean.get("stats") or {}).get("sales_docs_skipped_store") == 0,
          f"raw={(st_clean.get('stats') or {}).get('sales_docs_skipped_store')}")
    check("sticky факт ОЧИЩЕН авторитетным нулём полного прогона",
          diag(st_clean) == 0, f"diagnostics={st_clean.get('diagnostics')}")

    print("\n== [часть 2] Изоляция: org C не видит org A/B (и наоборот, проверено в части 1) ==")
    other = sql("SELECT id FROM orgs WHERE name LIKE 'Организация %(diag)'")
    check("org C — отдельная организация с собственным org_id",
          org_id not in {row[0] for row in other}, f"org_id={org_id} other={other}")

    c.close()
    mock_api.close()


def run() -> int:
    run_part1_schema()
    run_part2_lifecycle()
    print(f"\nИТОГО: {len(PASS)} OK, {len(FAIL)} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
