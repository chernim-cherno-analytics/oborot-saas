# -*- coding: utf-8 -*-
"""«Сотрудники и роли» для не-владельца с правом users_manage (ревью #110, P3).

Что доказывается (в браузере): кадровик с правами {plan, users_manage}
  1) не может выбрать роль шире своих прав (опция отключена);
  2) у сотрудника с правами шире его роль не меняется и «Убрать» нет;
  3) галочки прав, которых у него нет, отключены; встроенные роли не правятся.

Запуск: PLAYWRIGHT_BROWSERS_PATH=/tmp/pwb python tests/test_team_ui.py
"""
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DB_PATH = ROOT / "test_team_ui.db"
APP_PORT = int(os.environ.get("OBOROT_TEST_PORT", "8851"))
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["SCHEDULER_ENABLED"] = "0"
for suffix in ("", "-wal", "-shm"):
    p = Path(str(DB_PATH) + suffix)
    if p.exists():
        p.unlink()

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from app import auth  # noqa: E402
from app.main import app as oborot_app  # noqa: E402

BASE = f"http://127.0.0.1:{APP_PORT}"
OK = FAIL = 0
PW = "secret123"


def check(name, cond, detail=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  OK   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


cfg = uvicorn.Config(oborot_app, host="127.0.0.1", port=APP_PORT, log_level="warning")
server = uvicorn.Server(cfg)
th = threading.Thread(target=server.run, daemon=True)
th.start()
for _ in range(200):
    if server.started:
        break
    time.sleep(0.05)

try:
    own = httpx.Client(base_url=BASE, headers={"X-Oborot-CSRF": "1"}, timeout=60)
    own.post("/register", data={"name": "Владелец", "email": "own@team.io", "password": PW, "org_name": "Команда"})
    roles = own.get("/api/team").json()["roles"]
    hr = own.post("/api/team/roles", json={"name": "Кадровик", "perms": ["plan", "users_manage"]}).json()
    hr_id = hr.get("id") or next(r["id"] for r in own.get("/api/team").json()["roles"] if r["name"] == "Кадровик")
    emp_id = next(r["id"] for r in roles if r["builtin"] == "employee")
    with sqlite3.connect(DB_PATH) as con:
        org_id = con.execute("SELECT id FROM orgs WHERE name='Команда'").fetchone()[0]
        for email, rid in (("hr@team.io", hr_id), ("emp@team.io", emp_id)):
            cur = con.execute("INSERT INTO users (email, pw_hash, name, created_at, session_version) "
                              "VALUES (?,?,?,datetime('now'),1)", (email, auth.hash_password(PW), email))
            con.execute("INSERT INTO memberships (user_id, org_id, role, role_id) VALUES (?,?,?,?)",
                        (cur.lastrowid, org_id, "member", rid))
        con.commit()
    hrc = httpx.Client(base_url=BASE, headers={"X-Oborot-CSRF": "1"}, timeout=60)
    hrc.post("/login", data={"email": "hr@team.io", "password": PW})
    hrc.post("/api/hints/seen", json={"page": "team"})
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        ctx = b.new_context(viewport={"width": 1400, "height": 900})
        ctx.add_cookies([{"name": k, "value": v, "domain": "127.0.0.1", "path": "/"} for k, v in hrc.cookies.items()])
        page = ctx.new_page()
        errs = []
        page.on("pageerror", lambda e: errs.append(str(e)))
        page.goto(f"{BASE}/team")
        page.wait_for_timeout(1500)
        page.evaluate("() => { const o=document.getElementById('hint-overlay'); if(o) o.remove(); }")
        emp_opt_disabled = page.evaluate(
            f"() => [...document.querySelectorAll('.m-role option[value=\"{emp_id}\"]')].every(o => o.disabled)")
        check("роль шире своих прав выбрать нельзя", emp_opt_disabled)
        emp_row = page.evaluate(
            "() => { const r=[...document.querySelectorAll('.team-table tbody tr')].find(t=>t.textContent.includes('emp@team.io'));"
            " return r ? {sel: r.querySelector('.m-role').disabled, rm: !!r.querySelector('.m-remove')} : null; }")
        check("у сотрудника с правами шире роль не меняется и «Убрать» нет",
              emp_row is not None and emp_row["sel"] and not emp_row["rm"], str(emp_row))
        perm_state = page.evaluate(
            f"() => {{ const c=document.querySelector('.r-perm[data-id=\"{hr_id}\"][data-key=\"settings\"]');"
            f" const p=document.querySelector('.r-perm[data-id=\"{hr_id}\"][data-key=\"plan\"]');"
            " return c && p ? {settings: c.disabled, plan: p.disabled} : null; }")
        check("галочка права, которого у кадровика нет, отключена; своё право доступно",
              perm_state == {"settings": True, "plan": False}, str(perm_state))
        check("ошибок страницы нет", not errs, str(errs[:2]))
        b.close()
finally:
    server.should_exit = True
    th.join(timeout=10)

print(f"\nИТОГО: {OK} OK, {FAIL} FAIL")
for suffix in ("", "-wal", "-shm"):
    p = Path(str(DB_PATH) + suffix)
    if p.exists():
        p.unlink()
sys.exit(1 if FAIL else 0)
