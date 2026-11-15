# -*- coding: utf-8 -*-
"""Плашка свежести: перезапуск сервера при выкладке не «падение» (прод 15.11).

Что доказывается:
  1) /api/freshness отдаёт класс причины ошибки синка (sync_error_cause);
  2) ms_sync.reset_stale_running помечает прерванный синк как transient;
  3) шаблон плашки показывает transient при свежих данных жёлтым, без «падает».

Запуск: python tests/test_sync_chip.py
"""
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DB_PATH = ROOT / "test_sync_chip.db"
for suffix in ("", "-wal", "-shm"):
    p = Path(str(DB_PATH) + suffix)
    if p.exists():
        p.unlink()
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["SCHEDULER_ENABLED"] = "0"

from fastapi.testclient import TestClient  # noqa: E402

from app import ms_sync  # noqa: E402
from app.main import app  # noqa: E402

OK = FAIL = 0


def check(name, cond, detail=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  OK   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


with TestClient(app, headers={"X-Oborot-CSRF": "1"}) as a:
    a.post("/register", data={"name": "A", "email": "chip@test.io",
                              "password": "secret123", "org_name": "Чип"})
    check("демо", a.post("/api/connect/demo").status_code == 200)
    with sqlite3.connect(DB_PATH) as con:
        org_id = con.execute("SELECT id FROM orgs WHERE name='Чип'").fetchone()[0]
        con.execute("DELETE FROM sync_state WHERE org_id=?", (org_id,))
        con.commit()
    from app.db import SessionLocal
    from app.models import SyncState
    s = SessionLocal()
    s.add(SyncState(org_id=org_id, state="running", mode="incremental"))
    s.commit()
    s.close()
    ms_sync.reset_stale_running()
    d = a.get("/api/freshness").json()
    check("прерванный перезапуском синк: state error, причина transient",
          d.get("sync_state") == "error" and d.get("sync_error_cause") == "transient", json.dumps(d)[:300])

html = (ROOT / "templates" / "_hints.html").read_text(encoding="utf-8")
check("плашка: transient при свежих данных жёлтая, без «падает»",
      'd.sync_error_cause === "transient" && stale < 2' in html
      and "продолжится при следующем плановом запуске" in html)

print(f"\nИТОГО: {OK} OK, {FAIL} FAIL")
for suffix in ("", "-wal", "-shm"):
    p = Path(str(DB_PATH) + suffix)
    if p.exists():
        p.unlink()
sys.exit(1 if FAIL else 0)
