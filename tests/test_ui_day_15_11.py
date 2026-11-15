# -*- coding: utf-8 -*-
"""Мелочи из прогона интерфейса 15.11 (в браузере).

1) Мастер, шаг 3: раскрытый блок «Что система считать не берётся» остаётся
   раскрытым после галочки (раньше сворачивался при каждом пересчёте).
2) «Заказ» у наблюдателя: поля ростовки не принимают клик.

Запуск: PLAYWRIGHT_BROWSERS_PATH=/tmp/pwb python tests/test_ui_day_15_11.py
"""
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DB_PATH = ROOT / "test_ui_day_15_11.db"
APP_PORT = int(os.environ.get("OBOROT_TEST_PORT", "8852"))
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


def cookies(cl):
    return [{"name": k, "value": v, "domain": "127.0.0.1", "path": "/"} for k, v in cl.cookies.items()]


try:
    own = httpx.Client(base_url=BASE, headers={"X-Oborot-CSRF": "1"}, timeout=120)
    own.post("/register", data={"name": "Владелец", "email": "own@day.io", "password": PW, "org_name": "День"})
    own.post("/api/connect/demo")
    for pg in ("assistant", "replenish"):
        own.post("/api/hints/seen", json={"page": pg})
    view_id = next(r["id"] for r in own.get("/api/team").json()["roles"] if r["builtin"] == "viewer")
    with sqlite3.connect(DB_PATH) as con:
        org_id = con.execute("SELECT id FROM orgs WHERE name='День'").fetchone()[0]
        cur = con.execute("INSERT INTO users (email, pw_hash, name, created_at, session_version) "
                          "VALUES (?,?,?,datetime('now'),1)", ("view@day.io", auth.hash_password(PW), "Наблюдатель"))
        con.execute("INSERT INTO memberships (user_id, org_id, role, role_id) VALUES (?,?,?,?)",
                    (cur.lastrowid, org_id, "member", view_id))
        con.commit()
    vc = httpx.Client(base_url=BASE, headers={"X-Oborot-CSRF": "1"}, timeout=60)
    vc.post("/login", data={"email": "view@day.io", "password": PW})
    vc.post("/api/hints/seen", json={"page": "replenish"})

    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch()

        print("\n1. Мастер: раскрытый блок не сворачивается после галочки")
        ctx = b.new_context(viewport={"width": 1400, "height": 900})
        ctx.add_cookies(cookies(own))
        page = ctx.new_page()
        errs = []
        page.on("pageerror", lambda e: errs.append(str(e)))
        page.goto(f"{BASE}/assistant")
        page.wait_for_timeout(1500)
        page.evaluate("() => { const o=document.getElementById('hint-overlay'); if(o) o.remove(); }")
        chip = page.locator('#catChips .chip[data-all="1"]')
        chip.wait_for(state="visible", timeout=15000)
        if chip.get_attribute("aria-pressed") != "true":
            chip.click()
        page.fill("#budget", "5 000 000")
        page.evaluate("() => window.go(2)")
        page.wait_for_timeout(500)
        page.evaluate("() => window.preview(3)")
        page.wait_for_function("() => [...document.querySelectorAll('#planBox summary')]"
                               ".some(s => s.textContent.includes('считать не берётся'))", timeout=60000)
        has_box = page.evaluate(
            "() => { const d=[...document.querySelectorAll('#planBox details')].find(x=>x.querySelector('summary')"
            ".textContent.includes('считать не берётся')); if(!d) return null; d.open=true;"
            " const cb=d.querySelector('.takebox'); if(!cb) return 'nobox'; cb.click(); return 'clicked'; }")
        check("в блоке есть галочка «взять»", has_box == "clicked", str(has_box))
        page.wait_for_timeout(4000)
        still_open = page.evaluate(
            "() => { const d=[...document.querySelectorAll('#planBox details')].find(x=>x.querySelector('summary')"
            ".textContent.includes('считать не берётся')); return d ? d.open : null; }")
        check("после пересчёта блок по-прежнему раскрыт", still_open is True, str(still_open))
        check("ошибок страницы нет", not errs, str(errs[:2]))
        ctx.close()

        print("\n2. «Заказ» у наблюдателя: ростовка не принимает клик")
        ctx = b.new_context(viewport={"width": 1400, "height": 900})
        ctx.add_cookies(cookies(vc))
        page = ctx.new_page()
        page.goto(f"{BASE}/replenish")
        page.wait_for_timeout(2500)
        pe = page.evaluate(
            "() => { const s=document.createElement('input'); s.className='size-rec'; document.body.appendChild(s);"
            " const v=getComputedStyle(s).pointerEvents; s.remove(); return v; }")
        check("поле ростовки у наблюдателя: pointer-events none", pe == "none", str(pe))
        ctx.close()
        ctx = b.new_context(viewport={"width": 1400, "height": 900})
        ctx.add_cookies(cookies(own))
        page = ctx.new_page()
        page.goto(f"{BASE}/replenish")
        page.wait_for_timeout(2500)
        pe = page.evaluate(
            "() => { const s=document.createElement('input'); s.className='size-rec'; document.body.appendChild(s);"
            " const v=getComputedStyle(s).pointerEvents; s.remove(); return v; }")
        check("у владельца поле ростовки доступно", pe != "none", str(pe))
        ctx.close()
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
