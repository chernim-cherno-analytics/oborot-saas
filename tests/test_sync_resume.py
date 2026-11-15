# -*- coding: utf-8 -*-
"""Почасовой догон повторяет синк, прерванный перезапуском сервера (прод 15.11).

Что доказывается:
  1) синк, прерванный перезапуском (transient), при свежем last_sync_at
     попадает в почасовой догон, а не ждёт утра;
  2) ошибка токена (не transient) в догон по этой причине не попадает;
  3) старый (больше 48 часов) transient не повторяется бесконечно.

Синтетическая база, МойСклад не вызывается (start_sync подменён).
Запуск: python tests/test_sync_resume.py
"""
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DB_PATH = ROOT / "test_sync_resume.db"
for suffix in ("", "-wal", "-shm"):
    p = Path(str(DB_PATH) + suffix)
    if p.exists():
        p.unlink()
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["SCHEDULER_ENABLED"] = "0"

from app.db import Base, SessionLocal, engine  # noqa: E402
from app import models  # noqa: E402,F401
from app import ms_sync, scheduler  # noqa: E402
from app.models import Connection, Org, SyncState  # noqa: E402

OK = FAIL = 0


def check(name, cond, detail=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  OK   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


Base.metadata.create_all(engine)
now = datetime.utcnow()
s = SessionLocal()
orgs = {}
for name, cause, finished in (("restart", "transient", now - timedelta(minutes=5)),
                              ("token", "token", now - timedelta(minutes=5)),
                              ("old", "transient", now - timedelta(hours=72))):
    o = Org(name=name)
    s.add(o)
    s.flush()
    orgs[name] = o.id
    s.add(Connection(org_id=o.id, kind="moysklad", status="active", last_sync_at=now - timedelta(hours=2)))
    s.add(SyncState(org_id=o.id, state="error", mode="incremental", finished_at=finished,
                    stats_json=json.dumps({"error_cause": cause}), error="x"))
s.commit()
s.close()

started = []
ms_sync.start_sync = lambda org_id, mode="incremental": started.append(org_id) or False
res = scheduler._run_catchup_job()
check("прерванный перезапуском синк повторяется в ближайший час", orgs["restart"] in started, str(res))
check("ошибка токена этой веткой не повторяется", orgs["token"] not in started, str(res))
check("старый transient (72 ч) не крутится бесконечно", orgs["old"] not in started, str(res))

print(f"\nИТОГО: {OK} OK, {FAIL} FAIL")
for suffix in ("", "-wal", "-shm"):
    p = Path(str(DB_PATH) + suffix)
    if p.exists():
        p.unlink()
sys.exit(1 if FAIL else 0)
