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

Corrective #2 (rollout, BLOCKED issuecomment-5438529547) добавил проверку
`_carry_stats`/`_resolve_skip_diagnostic`: если у организации уже был
положительный `sales_docs_skipped_store` ДО появления sticky-ключа (прод
до выпуска), owner должен увидеть его немедленно, а он обязан пережить
carry в первый прогон новой версии.

Corrective #3 (P2, discussion_r3871610382) закрывает более узкий случай
той же природы: прерванный прогон, у которого УЖЕ был sticky (например 5),
находит НОВОЕ положительное raw (например 8) до собственного падения —
это evidence обязано материализоваться в sticky на carry СЛЕДУЮЩЕГО
прогона, даже когда старый sticky уже существовал, иначе resume (не
перечитывающий уже обработанные чанки) навсегда теряет находку упавшего
прогона.

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


def stats_legacy_only(value_json: str | None) -> str:
    """Валидный по форме JSON stats БЕЗ sticky-ключа, с ОДНИМ legacy полем
    sales_docs_skipped_store=<value_json> — симулирует прод-строку ДО
    появления sticky (rollout bootstrap, corrective #2, BLOCKED
    issuecomment-5438529547)."""
    if value_json is None:
        return json.dumps({"sales_docs": 10, "sales_rows": 8})
    return ('{"sales_docs": 10, "sales_rows": 8, "sales_docs_skipped_store": '
            + value_json + "}")


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

    print("\n== [часть 1, rollout] Sticky отсутствует — валидный ПОЛОЖИТЕЛЬНЫЙ "
          "legacy бутстрапится немедленно ==")
    set_stats(org_a, stats_legacy_only("9"))
    st_legacy = a.get("/api/sync/status").json()
    check("bootstrap: legacy positive (9) виден владельцу, пока sticky не записан",
          diag(st_legacy) == 9, f"diagnostics={st_legacy.get('diagnostics')}")

    print("\n== [часть 1, rollout] Fail-closed таблица: legacy сам по себе "
          "НИЧЕГО не авторизует ==")
    legacy_cases = [
        ("legacy отсутствует вовсе", None),
        ("legacy == 0", "0"),
        ("legacy отрицательный", "-1"),
        ("legacy bool true", "true"),
        ("legacy bool false", "false"),
        ("legacy строка", '"3"'),
        ("legacy float", "3.5"),
        ("legacy null", "null"),
    ]
    for title, raw in legacy_cases:
        set_stats(org_a, stats_legacy_only(raw))
        st_lbad = a.get("/api/sync/status").json()
        check(f"rollout fail-closed ({title}) -> diagnostics is None, не 0/подделка",
              diag(st_lbad) is None, f"diagnostics={st_lbad.get('diagnostics')}")

    print("\n== [часть 1, rollout] Присутствующий sticky приоритетнее legacy, "
          "даже если сам sticky некорректен ==")
    set_stats(org_a, '{"sales_docs_skipped_store": 9, '
                     '"sales_docs_skipped_store_unresolved": "bad"}')
    st_priority = a.get("/api/sync/status").json()
    check("malformed sticky НЕ подменяется валидным положительным legacy",
          diag(st_priority) is None, f"diagnostics={st_priority.get('diagnostics')}")

    set_stats(org_a, '{"sales_docs_skipped_store": 9, '
                     '"sales_docs_skipped_store_unresolved": 0}')
    st_priority0 = a.get("/api/sync/status").json()
    check("валидный sticky==0 (авторитетная очистка) НЕ подменяется "
          "положительным legacy",
          diag(st_priority0) == 0, f"diagnostics={st_priority0.get('diagnostics')}")

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


def run_part2_saved_resume_point() -> None:
    """Corrective #2 (BLOCKED issuecomment-5438529547): настоящая ЧАСТИЧНАЯ
    первичная загрузка, реально сохранившая resume point (history_loaded_from
    записан фазой month/finalize-lite — см. app/ms_sync.py:_run_initial),
    падает ВНУТРИ фазы history — а не на ассортименте, как в первой версии
    corrective. Затем обычная кнопка «Синхронизировать сейчас» резюмирует
    прерванную загрузку, и её собственный ЧЕСТНЫЙ ноль истории (в организации
    нет ни одного skip-документа) не авторитетен — sticky остаётся нетронутым.
    """
    c = client()
    mock_api = httpx.Client(base_url=f"http://127.0.0.1:{MOCK_PORT}", timeout=30.0)
    all_stores = [sid for sid, _ in mock_ms.STORES]
    org_id = register_and_connect_ms(c, "owner-d@diag.test", "Организация D (resume)",
                                     all_stores)

    # Источник sticky тут не важен — важно, что RESUMED-прогон со своим
    # честным нулём его не почеркнёт.
    set_stats(org_id, stats_with("5"), state="done")

    window_days = int(os.environ["INITIAL_WINDOW_DAYS"])
    w_start = (date.today() - timedelta(days=window_days - 1)).isoformat()
    # docs_429_before блокирует ТОЛЬКО документы старше начала окна
    # (m_from < w_start) — ровно фаза history, идущая ПОСЛЕ month/
    # finalize-lite, где history_loaded_from уже сохранён и персистирован.
    mock_api.post("/__test/faults", json={"docs_429_before": w_start})

    print("\n== [часть 2, resume] Первичная загрузка реально сохраняет resume "
          "point и падает ВНУТРИ фазы history ==")
    r = c.post("/api/sync/initial")
    check("первичная загрузка (будущий partial) запущена", r.status_code == 200,
          f"status={r.status_code}")
    st_partial = wait_sync_done(c)
    check("прогон честно упал (error), не завис и не выдумал done",
          st_partial.get("state") == "error", f"state={st_partial.get('state')}")
    saved_from = (st_partial.get("stats") or {}).get("history_loaded_from")
    check("resume point РЕАЛЬНО сохранён (history_loaded_from == начало окна)",
          saved_from == w_start, f"history_loaded_from={saved_from} ожидалось {w_start}")
    check("sticky факт после partial-провала остался 5 (carried, не потерян)",
          diag(st_partial) == 5, f"diagnostics={st_partial.get('diagnostics')}")

    print("\n== [часть 2, resume] Обычная кнопка «Синхронизировать сейчас» "
          "резюмирует прерванную загрузку (веткой initial, а не с нуля) ==")
    mock_api.post("/__test/faults", json={})  # снять фолт — резюме обязано дойти до done
    r = c.post("/api/sync/run")  # обычный инкремент-эндпоинт — см. _pending_resume
    check("резюме запущено", r.status_code == 200, f"status={r.status_code}")
    st_resumed = wait_sync_done(c)
    check("резюме дошло до done", st_resumed.get("state") == "done",
          f"state={st_resumed.get('state')} error={st_resumed.get('error')}")
    check("резюме реально пошло веткой initial (resume_from), не голым инкрементом",
          st_resumed.get("mode") == "initial", f"mode={st_resumed.get('mode')}")
    raw_resumed = (st_resumed.get("stats") or {}).get("sales_docs_skipped_store")
    check("СВОЙ счётчик резюмированного прогона честно 0 (в org D нет skip-документов)",
          raw_resumed == 0, f"raw={raw_resumed}")
    check("sticky факт == 5 — RESUMED-прогон со своим честным нулём его НЕ очистил",
          diag(st_resumed) == 5, f"diagnostics={st_resumed.get('diagnostics')}")

    c.close()
    mock_api.close()


def run_part2_rollout_carry() -> None:
    """Corrective #2: legacy-only positive (rollout-состояние строки) видна
    владельцу немедленно и переживает первый ОБЫЧНЫЙ инкремент — и на
    queued/running снимке, и на честном нуле завершения (org E не имеет
    skip-документов вовсе).
    """
    c = client()
    mock_api = httpx.Client(base_url=f"http://127.0.0.1:{MOCK_PORT}", timeout=30.0)
    all_stores = [sid for sid, _ in mock_ms.STORES]
    org_id = register_and_connect_ms(c, "owner-e@diag.test", "Организация E (rollout)",
                                     all_stores)

    # Прод-строка ДО появления sticky-ключа: только legacy, state=done.
    set_stats(org_id, stats_legacy_only("6"), state="done")

    print("\n== [часть 2, rollout] Немедленная видимость legacy-факта до "
          "любого нового прогона ==")
    st_before = c.get("/api/sync/status").json()
    check("owner видит bootstrap-факт (6) ДО нового прогона",
          diag(st_before) == 6, f"diagnostics={st_before.get('diagnostics')}")

    print("\n== [часть 2, rollout] Обычный инкремент: bootstrap переносится "
          "в queued/running снимок и переживает честный ноль ==")
    mock_api.post("/__test/faults", json={"stock_delay_ms": 300})
    r = c.post("/api/sync/run")
    check("инкремент запущен", r.status_code == 200, f"status={r.status_code}")
    st_start = c.get("/api/sync/status").json()
    check("сразу после старта факт всё ещё == 6 (bootstrap перенесён в carried)",
          diag(st_start) == 6,
          f"state={st_start.get('state')} diagnostics={st_start.get('diagnostics')}")

    st_done = wait_sync_done(c)
    mock_api.post("/__test/faults", json={})
    check("инкремент дошёл до done", st_done.get("state") == "done",
          f"state={st_done.get('state')} error={st_done.get('error')}")
    raw_done = (st_done.get("stats") or {}).get("sales_docs_skipped_store")
    check("СВОЙ счётчик инкремента честно 0 (в org E нет skip-документов)",
          raw_done == 0, f"raw={raw_done}")
    check("факт остался 6 после честного нуля обычного инкремента",
          diag(st_done) == 6, f"diagnostics={st_done.get('diagnostics')}")

    c.close()
    mock_api.close()


def run_part2_fresh_evidence_overrides_sticky() -> None:
    """Corrective #3 (P2, discussion_r3871610382): прерванный прогон находит
    НОВОЕ положительное evidence (raw=8) ПОСЛЕ того, как sticky уже был 5.
    Это evidence обязано материализоваться в sticky на СЛЕДУЮЩЕМ прогоне (при
    carry), даже когда старый sticky уже существовал — иначе находка
    упавшего прогона теряется навсегда: resume не перечитывает уже
    обработанные чанки (тот же принцип DATA-4, что и для инкремента).
    """
    c = client()
    mock_api = httpx.Client(base_url=f"http://127.0.0.1:{MOCK_PORT}", timeout=30.0)
    all_stores = [sid for sid, _ in mock_ms.STORES]
    org_id = register_and_connect_ms(c, "owner-f@diag.test",
                                     "Организация F (fresh-evidence)", all_stores)

    set_stats(org_id, stats_with("5"), state="done")

    window_days = int(os.environ["INITIAL_WINDOW_DAYS"])
    w_start = (date.today() - timedelta(days=window_days - 1)).isoformat()
    SKIP_STORE = "st-ghost-test-f"
    # Документы кладём В ОКНО (date == w_start, самый старый день окна): их
    # считает фаза month, которая идёт ДО фазы history — раньше, чем
    # сработает fault ниже, и раньше, чем прогон вообще может упасть.
    mock_api.post("/__test/skip_store_doc", json={
        "entity": "demand", "store": SKIP_STORE, "date": w_start, "count": 8,
    })
    mock_api.post("/__test/faults", json={"docs_429_before": w_start})

    print("\n== [часть 2, fresh-evidence] Прерванный прогон находит НОВОЕ "
          "raw=8 поверх старого sticky=5, падает ВНУТРИ фазы history ==")
    r = c.post("/api/sync/initial")
    check("первичная загрузка (fresh evidence) запущена", r.status_code == 200,
          f"status={r.status_code}")
    st_partial = wait_sync_done(c)
    check("прогон честно упал (error)", st_partial.get("state") == "error",
          f"state={st_partial.get('state')}")
    saved_from = (st_partial.get("stats") or {}).get("history_loaded_from")
    check("resume point реально сохранён (месяц/finalize-lite отработали)",
          saved_from == w_start, f"history_loaded_from={saved_from} ожидалось {w_start}")
    raw_partial = (st_partial.get("stats") or {}).get("sales_docs_skipped_store")
    check("error snapshot: СВОЙ raw счётчик прерванного прогона == 8 "
          "(найдено фазой month до падения в фазе history)",
          raw_partial == 8, f"raw={raw_partial}")
    check("старый sticky (5) в error snapshot ещё не тронут "
          "(перенос происходит на carry СЛЕДУЮЩЕГО прогона)",
          diag(st_partial) == 5, f"diagnostics={st_partial.get('diagnostics')}")

    print("\n== [часть 2, fresh-evidence] Resume материализует свежие 8 "
          "поверх старого sticky=5, не теряет находку ==")
    mock_api.post("/__test/faults", json={"stock_delay_ms": 300})
    mock_api.post("/__test/reset_skip_store_docs")  # чтобы resume нашёл СВОЙ raw=0
    r = c.post("/api/sync/run")  # обычная кнопка — резюмирует через _pending_resume
    check("резюме запущено", r.status_code == 200, f"status={r.status_code}")
    st_start = c.get("/api/sync/status").json()
    check("сразу после старта резюме sticky УЖЕ == 8 (carry сработал до "
          "первого _collect_sales этого прогона)",
          diag(st_start) == 8,
          f"state={st_start.get('state')} diagnostics={st_start.get('diagnostics')}")

    st_resumed = wait_sync_done(c)
    mock_api.post("/__test/faults", json={})
    check("резюме дошло до done", st_resumed.get("state") == "done",
          f"state={st_resumed.get('state')} error={st_resumed.get('error')}")
    check("резюме реально пошло веткой initial (resume_from)",
          st_resumed.get("mode") == "initial", f"mode={st_resumed.get('mode')}")
    raw_resumed = (st_resumed.get("stats") or {}).get("sales_docs_skipped_store")
    check("СВОЙ счётчик резюмированного прогона честно 0 (документы убраны, "
          "уже обработанные чанки НЕ перечитываются)",
          raw_resumed == 0, f"raw={raw_resumed}")
    check("sticky факт == 8 — свежее evidence прерванного прогона победило "
          "старый sticky=5, а не потерялось",
          diag(st_resumed) == 8, f"diagnostics={st_resumed.get('diagnostics')}")

    c.close()
    mock_api.close()


def run_part3_unit_lifecycle() -> None:
    """Прямая проверка чистой функции `_apply_skip_diagnostic_lifecycle` —
    быстрое точное покрытие всей таблицы решений без сетевого прогона синка.
    Части 2 доказывают end-to-end поведение через настоящий синк; здесь —
    что сама функция реализует ИМЕННО эту таблицу, включая ключевой пункт
    corrective #2: RESUMED + положительный СВОЙ счётчик — это ОБНОВЛЕНИЕ
    sticky, а не «resumed ничего не трогает» (именно так и была
    сформулирована — неточно — прежняя версия TECH_DEBT.md).
    """
    from app import ms_sync as _mss

    print("\n== [часть 3] _apply_skip_diagnostic_lifecycle: таблица решений ==")

    s = {"sales_docs_skipped_store": 4}
    _mss._apply_skip_diagnostic_lifecycle(s, initial=True)
    check("non-resumed initial, raw=4>0 -> sticky обновлён на 4",
          s.get("sales_docs_skipped_store_unresolved") == 4, f"stats={s}")

    s = {"sales_docs_skipped_store": 0, "sales_docs_skipped_store_unresolved": 9}
    _mss._apply_skip_diagnostic_lifecycle(s, initial=True)
    check("non-resumed initial, raw=0 -> authoritative clear (0)",
          s.get("sales_docs_skipped_store_unresolved") == 0, f"stats={s}")

    s = {"sales_docs_skipped_store": 4, "resumed_from": "2026-01-01",
         "sales_docs_skipped_store_unresolved": 9}
    _mss._apply_skip_diagnostic_lifecycle(s, initial=True)
    check("RESUMED initial, raw=4>0 -> sticky ОБНОВЛЯЕТСЯ на 4 (не «не трогаем»)",
          s.get("sales_docs_skipped_store_unresolved") == 4, f"stats={s}")

    s = {"sales_docs_skipped_store": 0, "resumed_from": "2026-01-01",
         "sales_docs_skipped_store_unresolved": 9}
    _mss._apply_skip_diagnostic_lifecycle(s, initial=True)
    check("RESUMED initial, raw=0 -> sticky сохранён (9), НЕ авторитетный ноль",
          s.get("sales_docs_skipped_store_unresolved") == 9, f"stats={s}")

    s = {"sales_docs_skipped_store": 0, "sales_docs_skipped_store_unresolved": 9}
    _mss._apply_skip_diagnostic_lifecycle(s, initial=False)
    check("incremental, raw=0 -> sticky сохранён (9)",
          s.get("sales_docs_skipped_store_unresolved") == 9, f"stats={s}")

    s = {"sales_docs_skipped_store": 2, "sales_docs_skipped_store_unresolved": 9}
    _mss._apply_skip_diagnostic_lifecycle(s, initial=False)
    check("incremental, raw=2>0 -> sticky обновлён на 2",
          s.get("sales_docs_skipped_store_unresolved") == 2, f"stats={s}")

    for bad in (None, -1, True, "3", 3.5):
        s = {"sales_docs_skipped_store": bad, "sales_docs_skipped_store_unresolved": 9}
        _mss._apply_skip_diagnostic_lifecycle(s, initial=True)
        check(f"malformed raw ({bad!r}) -> sticky не тронут (9)",
              s.get("sales_docs_skipped_store_unresolved") == 9, f"stats={s}")

    print("\n== [часть 3] _carry_stats: свежее evidence против старого sticky ==")

    c1 = _mss._carry_stats({"sales_docs_skipped_store": 8,
                            "sales_docs_skipped_store_unresolved": 5})
    check("fresh legacy positive (8) ПОБЕЖДАЕТ существующий sticky (5)",
          c1.get("sales_docs_skipped_store_unresolved") == 8, f"carried={c1}")

    c2 = _mss._carry_stats({"sales_docs_skipped_store": 8})
    check("fresh legacy positive (8) бутстрапит отсутствующий sticky (rollout)",
          c2.get("sales_docs_skipped_store_unresolved") == 8, f"carried={c2}")

    for bad in (0, None, -1, True, "3", 3.5):
        prev = {"sales_docs_skipped_store_unresolved": 5}
        if bad is not None:
            prev["sales_docs_skipped_store"] = bad
        c3 = _mss._carry_stats(prev)
        check(f"malformed/non-positive legacy ({bad!r}) НЕ подменяет sticky (5)",
              c3.get("sales_docs_skipped_store_unresolved") == 5, f"carried={c3}")

    c4 = _mss._carry_stats({"sales_docs_skipped_store": 0})
    check("legacy==0 без sticky -> carried не содержит sticky-ключ вовсе "
          "(не подделывает 0)",
          "sales_docs_skipped_store_unresolved" not in c4, f"carried={c4}")


def run() -> int:
    run_part1_schema()
    run_part2_lifecycle()
    run_part2_saved_resume_point()
    run_part2_rollout_carry()
    run_part2_fresh_evidence_overrides_sticky()
    run_part3_unit_lifecycle()
    print(f"\nИТОГО: {len(PASS)} OK, {len(FAIL)} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
