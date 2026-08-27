# -*- coding: utf-8 -*-
"""Регрессия DATA-8 (третий, последний сценарий): видимость sales_docs_skipped_store.

Дефект, который закрывает этот тест. `_collect_sales` (`app/ms_sync.py`) уже
считал `stats["sales_docs_skipped_store"]` — документы продаж, чей склад
(`store` документа) не распознан вообще ИЛИ не входит в `active_store_ids`
организации (выбранные склады). Счётчик писался в `stats_json` и был виден
владельцу только «сырым» — внутри `stats` целиком на owner-only
`/api/sync/status`, без предупреждения на экране. Публичный
`/api/sync/progress` (любой участник) стата не отдавал вовсе (уже верно) —
это НЕ то, что чинит этот тест. Этот тест доказывает контракт УЗКОЙ
owner-only диагностики поверх уже существующего счётчика: точный
неотрицательный int или fail-closed `None` (никогда не 0 по умолчанию для
отсутствующих/некорректных данных), не протекает участнику и не смешивается
с чужой организацией.

Сборка синка НЕ запускается: `sync_state.stats_json` организации пишется
напрямую (как и в проде — просто без сетевого похода в МойСклад), сценарий
детерминирован и не нуждается в моке МойСклад.

Запуск из корня репозитория:  python tests/test_sync_diag_store.py
"""
import json
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "test_sync_diag_store.db"
APP_PORT = int(os.environ.get("OBOROT_TEST_PORT", "8818"))

os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["SCHEDULER_ENABLED"] = "0"

if DB_PATH.exists():
    DB_PATH.unlink()

import bcrypt  # noqa: E402
import httpx  # noqa: E402
import uvicorn  # noqa: E402

from app.main import app as oborot_app  # noqa: E402

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
    uid = None
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
    """Пишет sync_state организации напрямую — без прогона синка (детерминизм)."""
    exec_sql("DELETE FROM sync_state WHERE org_id=?", org_id)
    exec_sql(
        "INSERT INTO sync_state (org_id, state, mode, stage, progress, detail, "
        "stats_json, error, fail_streak, alerted_streak) "
        "VALUES (?, ?, 'incremental', '', 100.0, '', ?, '', 0, 0)",
        org_id, state, stats_raw,
    )


def stats_with(value_json: str) -> str:
    """Валидный по форме JSON stats с ОДНИМ полем sales_docs_skipped_store=<value_json>
    подставленным как сырой JSON-литерал (позволяет засеять некорректные типы —
    отрицательные числа, bool, строку, отсутствие поля вовсе)."""
    if value_json is None:
        return json.dumps({"sales_docs": 10, "sales_rows": 8})
    return ('{"sales_docs": 10, "sales_rows": 8, "sales_docs_skipped_store": '
            + value_json + "}")


def main() -> int:
    srv = ServerThread(oborot_app, APP_PORT)
    srv.start()
    try:
        return run()
    finally:
        srv.stop()
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(DB_PATH) + suffix)
            if p.exists():
                p.unlink()


def diag(resp_json: dict):
    return (resp_json.get("diagnostics") or {}).get("sales_docs_skipped_store")


def run() -> int:
    a = client()
    org_a = setup_org(a, "owner-a@diag.test", "Организация A (diag)")
    b = client()
    org_b = setup_org(b, "owner-b@diag.test", "Организация B (diag)")
    add_member(org_a, "member-a@diag.test")
    m = client()
    login(m, "member-a@diag.test")

    print("\n== Точный положительный счётчик виден владельцу ==")
    set_stats(org_a, stats_with("7"))
    st = a.get("/api/sync/status").json()
    check("owner-only /api/sync/status: diagnostics.sales_docs_skipped_store == 7",
          diag(st) == 7, f"diagnostics={st.get('diagnostics')}")
    check("тип значения — именно int, не bool/str",
          type(diag(st)) is int, f"type={type(diag(st))}")

    print("\n== Ноль — валидное значение, но не тревога (сама диагностика честная) ==")
    set_stats(org_a, stats_with("0"))
    st0 = a.get("/api/sync/status").json()
    check("diagnostics.sales_docs_skipped_store == 0 (точно, не подделка)",
          diag(st0) == 0, f"diagnostics={st0.get('diagnostics')}")

    print("\n== Отсутствующее/некорректное значение — fail-closed None, НЕ 0 ==")
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

    print("\n== Схема диагностики ограничена одним именованным полем ==")
    set_stats(org_a, stats_with("4"))
    st_schema = a.get("/api/sync/status").json()
    d = st_schema.get("diagnostics")
    check("diagnostics — словарь ровно с одним ключом sales_docs_skipped_store",
          isinstance(d, dict) and set(d.keys()) == {"sales_docs_skipped_store"},
          f"diagnostics={d}")

    print("\n== Организация A не видит значение организации B ==")
    set_stats(org_a, stats_with("7"))
    set_stats(org_b, stats_with("13"))
    st_a = a.get("/api/sync/status").json()
    st_b = b.get("/api/sync/status").json()
    check("owner A видит СВОЁ значение (7), не значение B",
          diag(st_a) == 7, f"diagnostics_a={st_a.get('diagnostics')}")
    check("owner B видит СВОЁ значение (13), не значение A",
          diag(st_b) == 13, f"diagnostics_b={st_b.get('diagnostics')}")

    print("\n== Участник: /api/sync/status по-прежнему 403 ==")
    r_member_status = m.get("/api/sync/status")
    check("участник получает 403 на owner-only /api/sync/status",
          r_member_status.status_code == 403, f"status={r_member_status.status_code}")

    print("\n== Публичный /api/sync/progress не раскрывает диагностику и stats ==")
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
    print(f"\nИТОГО: {len(PASS)} OK, {len(FAIL)} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
