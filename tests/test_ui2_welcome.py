# -*- coding: utf-8 -*-
"""UI2-4 (D-90): первый запуск, четыре вопроса по одному на окно (в браузере).

Страница первого запуска ничего не считает для расчётов: каждый ответ
уходит в уже существующую настройку. Поэтому проверяется не вёрстка, а то,
что ответ действительно доходит до своей настройки и ничего лишнего не
трогает:

1) Доступ: аноним уходит на вход, организация без подключения на
   подключение, участник без права на настройки в таблицу.
2) Путь нового человека: приветствие без экрана «Привет», подключение
   демо, четыре вопроса, таблица.
3) Срок попадает в overstock_days правила скидок, скидки в его проценты
   (равный шаг, округление до 5 %, пороги ₽/день правила не меняются),
   производство создаётся, границы классов уходят в thresholds.
4) Справка «в среднем» и «в полтора раза выше» сходится с данными
   /api/turnover, а в настройки попадает только подтверждённое.
5) Повторный проход без правок не отправляет ни одной записи.
6) Неверные границы не сохраняются и объясняются; телефон 390.

Запуск: PLAYWRIGHT_BROWSERS_PATH=/tmp/pwb python tests/test_ui2_welcome.py
"""
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DB_PATH = ROOT / "test_ui2_welcome.db"
APP_PORT = int(os.environ.get("OBOROT_TEST_PORT", "8855"))
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
CELLS = ["new_pct", "top_pct", "top_over_pct", "mid_pct", "mid_over_pct", "weak_pct", "weak_over_pct"]


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


def client():
    return httpx.Client(base_url=BASE, headers={"X-Oborot-CSRF": "1"}, timeout=120, follow_redirects=False)


def step(page):
    return page.evaluate("() => (location.hash || '').slice(1)")


def wait_step(page, key, timeout=15000):
    page.wait_for_function("(k) => (location.hash || '').slice(1) === k && !document.getElementById('pane').classList.contains('out')",
                           arg=key, timeout=timeout)
    page.wait_for_timeout(150)


def next_ready(page, timeout=60000):
    page.wait_for_function("() => { const n = document.getElementById('w-next'); return n && !n.disabled; }", timeout=timeout)


def ladder(lo, hi):
    out = {k: int(round((lo + (hi - lo) * i / (len(CELLS) - 1)) / 5.0 + 1e-9) * 5) for i, k in enumerate(CELLS)}
    out["new_pct"], out["weak_over_pct"] = lo, hi
    return out


try:
    print("\n1. Доступ")
    anon = client()
    r = anon.get("/welcome")
    check("аноним уходит на вход", r.status_code == 302 and r.headers["location"] == "/login", f"{r.status_code} {r.headers.get('location')}")
    own = client()
    own.post("/register", data={"name": "Владелец", "email": "own@w.io", "password": PW, "org_name": "Первый"})
    r = own.get("/welcome")
    check("без подключения первый запуск уводит на подключение данных",
          r.status_code == 302 and r.headers["location"] == "/onboarding", f"{r.status_code} {r.headers.get('location')}")
    rule_default = None

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        b = pw.chromium.launch()

        print("\n2. Путь нового человека: приветствие, подключение, вопросы")
        ctx = b.new_context(viewport={"width": 1280, "height": 800})
        ctx.add_cookies(cookies(own))
        page = ctx.new_page()
        errs: list[str] = []
        page.on("pageerror", lambda e: errs.append(str(e)))
        page.goto(f"{BASE}/")
        page.wait_for_selector("#btn-hello", timeout=20000)
        hello = page.evaluate("""() => { const vis = e => !!e && e.getClientRects().length > 0;
          const h = [...document.querySelectorAll('h1')].filter(vis).map(e => e.textContent.trim());
          let big = 0; document.querySelectorAll('body *').forEach(e => { if (vis(e) && parseFloat(getComputedStyle(e).fontSize) > 40) big++; });
          return {url: location.pathname, h, big, src: vis(document.getElementById('step-1')),
                  text: document.getElementById('step-0').textContent}; }""")
        check("первым открывается окно-приветствие на странице подключения",
              hello["url"] == "/onboarding" and hello["h"] == ["Добро пожаловать в Оборот"] and not hello["src"], str(hello)[:200])
        check("экрана с огромной надписью нет, аннотация про товарную матрицу на месте",
              hello["big"] == 0 and "товарную матрицу" in hello["text"] and "Привет" not in hello["text"], str(hello)[:200])
        page.click("#btn-hello")
        page.wait_for_selector("#btn-connect-demo", state="visible", timeout=10000)
        check("«Начать» открывает выбор источника данных",
              page.evaluate("() => document.getElementById('step-0').getClientRects().length === 0"))
        with page.expect_navigation(url=lambda u: "/onboarding" not in u, timeout=60000):
            page.click("#btn-connect-demo")
        check("после подключения открывается первый запуск", page.url.split("#")[0].endswith("/welcome"), page.url)
        wait_step(page, "days")
        rule_default = own.get("/api/discount-rule").json()["rule"]
        thr_default = own.get("/api/settings").json()["thresholds"]
        ui = page.evaluate("""() => ({title: document.getElementById('w-title').textContent,
          dots: document.querySelectorAll('#w-dots i').length, on: document.querySelector('#d-tiles .tile.on')?.getAttribute('data-d'),
          back: getComputedStyle(document.getElementById('w-back')).visibility,
          header: !!document.querySelector('.header.app-top, .sidebar'), fab: !!document.getElementById('hint-fab')})""")
        check("первый вопрос про срок, четыре шага, отмечен действующий срок, «Назад» скрыт",
              "дней" in ui["title"] and ui["dots"] == 4 and ui["on"] == str(rule_default["overstock_days"]) and ui["back"] == "hidden", str(ui))
        check("на первом запуске нет ни меню разделов, ни кнопки «?»", not ui["header"] and not ui["fab"], str(ui))

        print("\n3. Ответы доходят до своих настроек")
        page.click('#d-tiles .tile[data-d="60"]')
        page.click("#w-next")
        wait_step(page, "disc")
        rule = own.get("/api/discount-rule").json()["rule"]
        check("срок 60 дней сохранён в норму запаса, остальное правило не тронуто",
              rule["overstock_days"] == 60 and all(rule[k] == rule_default[k] for k in rule if k != "overstock_days"), str(rule))
        page.fill("#k-lo", "12")
        page.dispatch_event("#k-lo", "change")
        page.fill("#k-hi", "50")
        page.dispatch_event("#k-hi", "change")
        shown = page.evaluate("() => ({lo: document.getElementById('k-lo').value, hi: document.getElementById('k-hi').value,"
                              " scale: document.getElementById('k-scale').textContent})")
        check("границы скидок округляются до 5 %, шкала показана до сохранения",
              shown["lo"] == "10" and shown["hi"] == "50" and "без скидки" in shown["scale"] and "50 %" in shown["scale"]
              and "60 дн" in shown["scale"], str(shown))
        page.click("#w-next")
        wait_step(page, "prod")
        rule = own.get("/api/discount-rule").json()["rule"]
        want = ladder(10, 50)
        check("ступени скидок: равный шаг от 10 до 50, округление до 5 %",
              all(rule[k] == want[k] for k in CELLS) and [rule[k] for k in CELLS] == sorted(rule[k] for k in CELLS),
              f"{[rule[k] for k in CELLS]} != {[want[k] for k in CELLS]}")
        check("пороги ₽/день и «новинка» правила скидок вопросом не меняются",
              all(rule[k] == rule_default[k] for k in ("top_turnover", "mid_turnover", "new_days")) and rule["overstock_days"] == 60, str(rule))
        prods0 = own.get("/api/productions").json()["productions"]
        page.click("#p-add")
        page.keyboard.type("Китай")
        page.locator("#p-list .prow").last.locator(".num").fill("60")
        page.dispatch_event("#p-list .prow:last-child .num", "change")
        page.click("#w-next")
        wait_step(page, "bar")
        prods = own.get("/api/productions").json()["productions"]
        new = [p for p in prods if p["name"] == "Китай"]
        check("производство «Китай» со сроком 60 дней создано, основное не тронуто",
              len(prods) == len(prods0) + 1 and len(new) == 1 and new[0]["lead_time_days"] == 60
              and prods[0]["name"] == prods0[0]["name"] and prods[0]["lead_time_days"] == prods0[0]["lead_time_days"], str(prods)[:300])

        print("\n4. Границы классов: справка сходится с данными, сохраняется подтверждённое")
        next_ready(page)
        items = own.get("/api/turnover").json()["items"]
        rev = sum(it["turnover"] * it["dis"] for it in items if not it.get("hidden") and it["group"] == "rank" and it["dis"] > 0)
        days = sum(it["dis"] for it in items if not it.get("hidden") and it["group"] == "rank" and it["dis"] > 0)
        b_avg = rev / days
        t_bar = b_avg * 1.5
        calc = {"weak": round(t_bar / 3), "dull": round(2 * t_bar / 3), "good": round(5 * t_bar / 3)}
        seen = page.evaluate("""() => ({fact: document.getElementById('b-fact').textContent.replace(/\\s/g, ''),
          weak: +document.getElementById('b-weak').value, dull: +document.getElementById('b-dull').value,
          good: +document.getElementById('b-good').value, best: document.getElementById('b-best').textContent.replace(/\\s/g, ''),
          use: !!document.getElementById('b-use')})""")
        check("в полях действующие границы организации, а не расчёт",
              {k: seen[k] for k in ("weak", "dull", "good")} == thr_default and seen["best"] == str(thr_default["good"]), str(seen))
        check("справка: среднее по каталогу и ориентир ×1,5 совпадают с /api/turnover",
              f"{round(b_avg)}₽" in seen["fact"] and f"{round(t_bar)}₽" in seen["fact"], f"{round(b_avg)} {round(t_bar)} {seen['fact'][:120]}")
        check("до нажатия «Подставить расчёт» в настройках прежние границы",
              own.get("/api/settings").json()["thresholds"] == thr_default)
        page.fill("#b-weak", "3000")
        page.dispatch_event("#b-weak", "change")
        page.click("#w-next")
        page.wait_for_timeout(700)
        bad = page.evaluate("() => ({err: document.getElementById('w-err').textContent, hash: location.hash, path: location.pathname})")
        check("границы не по возрастанию не сохраняются и объясняются",
              "слева направо" in bad["err"] and bad["path"] == "/welcome" and own.get("/api/settings").json()["thresholds"] == thr_default, str(bad))
        if seen["use"]:
            page.click("#b-use")
        else:
            for k in ("weak", "dull", "good"):
                page.fill(f"#b-{k}", str(calc[k]))
                page.dispatch_event(f"#b-{k}", "change")
        got = page.evaluate("() => ({weak: +document.getElementById('b-weak').value, dull: +document.getElementById('b-dull').value,"
                            " good: +document.getElementById('b-good').value})")
        check("«Подставить расчёт» ставит треть, две трети и пять третей ориентира", got == calc, f"{got} != {calc}")
        with page.expect_navigation(url=lambda u: "/welcome" not in u, timeout=60000):
            page.click("#w-next")
        check("последний шаг открывает «Оборачиваемость»", page.url.rstrip("/").endswith("/turnover"), page.url)
        check("границы классов сохранены в настройки организации", own.get("/api/settings").json()["thresholds"] == calc,
              str(own.get("/api/settings").json()["thresholds"]))
        page.wait_for_function("() => document.querySelectorAll('#tb tr[data-base]').length > 0", timeout=60000)
        norm = page.evaluate("() => window.OborotTurnover.norm()")
        check("таблица считает запас от срока из первого запуска", norm == 60, str(norm))
        check("на пути нового человека ошибок скрипта нет", not errs, str(errs[:3]))

        print("\n5. Повторный проход без правок ничего не записывает")
        page.wait_for_timeout(1500)
        has_item = page.evaluate("() => !!document.getElementById('hm-first')")
        check("в меню «?» у владельца есть «Первый запуск заново»", has_item)
        writes: list[str] = []
        page.on("request", lambda rq: writes.append(rq.method + " " + rq.url.replace(BASE, "")) if rq.method != "GET" else None)
        before = (own.get("/api/discount-rule").json()["rule"], own.get("/api/settings").json()["thresholds"],
                  own.get("/api/productions").json()["productions"])
        page.goto(f"{BASE}/welcome")
        wait_step(page, "days")
        for key in ("disc", "prod", "bar"):
            page.click("#w-next")
            wait_step(page, key)
        next_ready(page)
        page.click("#w-back")
        wait_step(page, "prod")
        page.click("#w-next")
        wait_step(page, "bar")
        next_ready(page)
        with page.expect_navigation(url=lambda u: "/welcome" not in u, timeout=60000):
            page.click("#w-next")
        after = (own.get("/api/discount-rule").json()["rule"], own.get("/api/settings").json()["thresholds"],
                 own.get("/api/productions").json()["productions"])
        own_writes = [w for w in writes if "/api/hints" not in w and "/api/lessons" not in w and "/api/sync" not in w and "/api/prefs" not in w]
        check("пролистать первый запуск без правок: ни одной записи настроек", not own_writes and before == after, str(own_writes[:4]))

        print("\n5а. «Мой бизнес» в настройках показывает те же ответы")
        own.post("/api/hints/seen", json={"page": "settings"})   # карточка первого визита перехватила бы клик
        page.goto(f"{BASE}/settings")
        page.wait_for_function("() => { const e = document.getElementById('biz-thr'); return e && /\\d/.test(e.textContent); }", timeout=20000)
        page.wait_for_timeout(600)
        biz = page.evaluate("""() => { const t = id => document.getElementById(id).textContent.replace(/\\s+/g, ' ').trim();
          return {days: t('biz-days'), disc: t('biz-disc'), prod: t('biz-prod'), thr: t('biz-thr').replace(/ /g, ''),
                  best: t('biz-g-b').replace(/ /g, ''),
                  links: [...document.querySelectorAll('#biz-rows a')].map(a => a.getAttribute('href')), rerun: !!document.getElementById('biz-rerun')}; }""")
        check("срок, скидки, производства и границы совпадают с настройками организации",
              biz["days"] == "60 дн." and biz["disc"] == "от 10 до 50 %" and "Китай" in biz["prod"]
              and biz["thr"] == f"{calc['weak']}/{calc['dull']}/{calc['good']}" and biz["best"] == f"от{calc['good']}", str(biz))
        check("у владельца каждая строка ведёт на свой вопрос первого запуска",
              biz["links"] == [f"/welcome?one=1#{k}" for k in ("days", "disc", "prod", "bar")] and biz["rerun"], str(biz["links"]))
        page.click('#biz-rows a[href="/welcome?one=1#days"]')
        wait_step(page, "days")
        one = page.evaluate("""() => ({title: document.getElementById('w-title').textContent, next: document.getElementById('w-next').textContent,
          back: getComputedStyle(document.getElementById('w-back')).visibility, dots: document.querySelectorAll('#w-dots i').length,
          later: getComputedStyle(document.getElementById('w-later')).visibility})""")
        check("«Изменить» открывает один вопрос: кнопка «Сохранить», без «Назад», точек и строки про настройки",
              "дней" in one["title"] and one["next"] == "Сохранить" and one["back"] == "hidden" and one["dots"] == 0
              and one["later"] == "hidden", str(one))
        page.click('#d-tiles .tile[data-d="90"]')
        with page.expect_navigation(url=lambda u: "/welcome" not in u, timeout=30000):
            page.click("#w-next")
        page.wait_for_function("() => { const e = document.getElementById('biz-days'); return e && /\\d/.test(e.textContent); }", timeout=20000)
        check("после «Сохранить» человек снова в настройках и видит новое значение",
              page.url.rstrip("/").endswith("/settings")
              and page.evaluate("() => document.getElementById('biz-days').textContent.trim()") == "90 дн."
              and own.get("/api/discount-rule").json()["rule"]["overstock_days"] == 90, page.url)
        ctx.close()

        print("\n6. Участник без права на настройки и телефон")
        view_id = next(r["id"] for r in own.get("/api/team").json()["roles"] if r["builtin"] == "viewer")
        with sqlite3.connect(DB_PATH) as con:
            org_id = con.execute("SELECT m.org_id FROM memberships m JOIN users u ON u.id=m.user_id WHERE u.email=?",
                                 ("own@w.io",)).fetchone()[0]
            cur = con.execute("INSERT INTO users (email, pw_hash, name, created_at, session_version) "
                              "VALUES (?,?,?,datetime('now'),1)", ("v@w.io", auth.hash_password(PW), "Наблюдатель"))
            con.execute("INSERT INTO memberships (user_id, org_id, role, role_id) VALUES (?,?,?,?)",
                        (cur.lastrowid, org_id, "member", view_id))
            con.commit()
        mem = client()
        mem.post("/login", data={"email": "v@w.io", "password": PW})
        r = mem.get("/welcome")
        role_ok = mem.get("/api/settings").status_code == 200 and mem.post("/api/settings", json={"horizon_days": 90}).status_code == 403
        check("фикстура: второй человек в той же организации без права на настройки", role_ok)
        check("участник без права на настройки уходит в таблицу",
              r.status_code == 302 and r.headers["location"] == "/turnover", f"{r.status_code} {r.headers.get('location')}")
        vctx = b.new_context(viewport={"width": 1280, "height": 800})
        vctx.add_cookies(cookies(mem))
        vpage = vctx.new_page()
        vpage.goto(f"{BASE}/turnover")
        vpage.wait_for_function("() => document.querySelectorAll('#tb tr[data-base]').length > 0", timeout=60000)
        check("у него в меню «?» пункта «Первый запуск заново» нет",
              vpage.evaluate("() => !document.getElementById('hm-first')"))
        vpage.goto(f"{BASE}/settings")
        vpage.wait_for_function("() => { const e = document.getElementById('biz-thr'); return e && /\\d/.test(e.textContent); }", timeout=20000)
        check("наблюдатель видит «Мой бизнес» без ссылок на изменение",
              vpage.evaluate("() => document.querySelectorAll('#biz-rows a').length === 0 && !document.getElementById('biz-rerun')"))
        vctx.close()

        # Ревью #137 (Codex, P1): пустой каталог после законченной загрузки не
        # запирает человека на последнем шаге; пока загрузка идёт, шаг можно не ждать.
        ectx = b.new_context(viewport={"width": 1280, "height": 800})
        ectx.add_cookies(cookies(own))
        epage = ectx.new_page()
        prog = {"state": "running"}
        epage.route("**/api/turnover", lambda r: r.fulfill(status=200, content_type="application/json", body='{"items": []}'))
        epage.route("**/api/sync/progress", lambda r: r.fulfill(status=200, content_type="application/json",
                                                               body='{"state": "%s"}' % prog["state"]))
        epage.goto(f"{BASE}/welcome#bar")
        wait_step(epage, "bar")
        epage.wait_for_selector("#b-skip", timeout=10000)
        check("пока данные грузятся, «Открыть таблицу» ждёт, но есть «Не ждать и открыть таблицу»",
              epage.evaluate("() => document.getElementById('w-next').disabled === true"))
        prog["state"] = "idle"
        epage.wait_for_function("() => !!document.getElementById('b-weak') && !document.getElementById('w-next').disabled", timeout=30000)
        fact = epage.evaluate("() => document.getElementById('b-fact').textContent")
        check("загрузка закончилась пустой: шаг открыт, честно сказано, что считать не из чего",
              "нет вещей с продажами" in fact and not epage.evaluate("() => !!document.getElementById('b-use')"), fact[:120])
        thr_before = own.get("/api/settings").json()["thresholds"]
        with epage.expect_navigation(url=lambda u: "/welcome" not in u, timeout=30000):
            epage.click("#w-next")
        prog["state"] = "error"
        epage2 = ectx.new_page()
        epage2.route("**/api/turnover", lambda r: r.fulfill(status=200, content_type="application/json", body='{"items": []}'))
        epage2.route("**/api/sync/progress", lambda r: r.fulfill(status=200, content_type="application/json", body='{"state": "error"}'))
        epage2.goto(f"{BASE}/welcome#bar")
        epage2.wait_for_function("() => !!document.getElementById('b-fact') && !document.getElementById('w-next').disabled", timeout=30000)
        check("сбой загрузки назван сбоем, шаг не заперт",
              "не загрузились" in epage2.evaluate("() => document.getElementById('b-fact').textContent"))
        epage2.close()
        check("с пустым каталогом последний шаг ведёт в таблицу и границы не трогает",
              epage.url.rstrip("/").endswith("/turnover") and own.get("/api/settings").json()["thresholds"] == thr_before, epage.url)
        ectx.close()

        mctx = b.new_context(viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True)
        mctx.add_cookies(cookies(own))
        mpage = mctx.new_page()
        merr: list[str] = []
        mpage.on("pageerror", lambda e: merr.append(str(e)))
        mpage.goto(f"{BASE}/welcome")
        wide = []
        for key in ("days", "disc", "prod", "bar"):
            wait_step(mpage, key)
            if key == "bar":
                next_ready(mpage)
            g = mpage.evaluate("""() => { const w = document.getElementById('win').getBoundingClientRect();
              const n = document.getElementById('w-next').getBoundingClientRect();
              return {sw: document.documentElement.scrollWidth, l: w.left, r: w.right, nr: n.right, nh: n.height}; }""")
            if not (g["sw"] <= 390 and g["l"] >= 0 and g["r"] <= 390 and g["nr"] <= g["r"] and g["nh"] >= 40):
                wide.append((key, g))
            if key != "bar":
                mpage.tap("#w-next")
        check("телефон 390: каждое окно в ширину экрана, кнопка «Дальше» не меньше 40 px", not wide, str(wide))
        check("телефон: ошибок скрипта нет", not merr, str(merr[:3]))
        mctx.close()
        b.close()
finally:
    server.should_exit = True
    th.join(timeout=10)
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(DB_PATH) + suffix)
        if p.exists():
            p.unlink()

print(f"\nИТОГО: {OK} OK, {FAIL} FAIL")
sys.exit(1 if FAIL else 0)
