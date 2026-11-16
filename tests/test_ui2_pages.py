# -*- coding: utf-8 -*-
"""UI2-3 (D-84): остальные страницы приложения в новом виде (в браузере).

Пакет меняет только вид, поэтому проверяется то, что вид может сломать, а
не цвета сами по себе:

1) Каждая страница получила класс `ui2` и оба файла стилей, не шире окна,
   без ошибок скрипта; верхняя панель одна, в одну строку, со всеми
   одиннадцатью разделами и отмеченным текущим.
2) У страниц на base.html боковое меню стало верхней панелью средствами
   CSS: разметка прежняя (`#side-nav`, `#side-sync`, `#user-chip`), меню
   пользователя стоит в панели, не лежит на ссылках и открывается.
3) Телефон 390: страницы не шире окна, у страниц на base.html меню
   раскрывается кнопкой и показывает все разделы.
4) Подписи капсом на самостоятельных страницах сняты.
5) Вход и встроенный режим МойСклада не сломаны.
6) Остаток ревью PR #134: подсказки не запускаются поверх урока; при
   ошибке загрузки таблицы тост говорит об ошибке, а не о пустой таблице.

Запуск: PLAYWRIGHT_BROWSERS_PATH=/tmp/pwb python tests/test_ui2_pages.py
"""
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DB_PATH = ROOT / "test_ui2_pages.db"
APP_PORT = int(os.environ.get("OBOROT_TEST_PORT", "8854"))
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["SCHEDULER_ENABLED"] = "0"
for suffix in ("", "-wal", "-shm"):
    p = Path(str(DB_PATH) + suffix)
    if p.exists():
        p.unlink()

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from app.main import app as oborot_app  # noqa: E402

BASE = f"http://127.0.0.1:{APP_PORT}"
OK = FAIL = 0
PW = "secret123"

# Самостоятельные страницы (своя шапка _app_shell.html) и страницы base.html.
STANDALONE = ["/stocks", "/assistant", "/replenish", "/sizes", "/budget", "/forecast", "/revenue", "/lessons"]
BASED = ["/settings", "/supply", "/account", "/team", "/plans"]
NAV_COUNT = 11


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


def settle(page):
    try:
        page.wait_for_load_state("networkidle", timeout=25000)
    except Exception:  # noqa: BLE001 - страница с фоновыми запросами: ждём сколько можем
        pass
    page.wait_for_timeout(500)


# Общий замер страницы: класс, стили, ширина, верхняя панель.
_PAGE_JS = """() => {
  const box = e => { const r = e.getBoundingClientRect(); return {l: r.left, t: r.top, r: r.right, b: r.bottom, w: r.width, h: r.height}; };
  const shown = e => !!e && e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden';
  const bar = document.querySelector('.header.app-top') || document.querySelector('.sidebar');
  const nav = document.getElementById('app-nav') || document.getElementById('side-nav');
  const links = nav ? [...nav.querySelectorAll('a')].filter(shown) : [];
  // Ссылка считается нажимаемой, если в её центре лежит она сама, а не что-то поверх.
  const hitOk = a => { const r = a.getBoundingClientRect(); const t = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    return !!t && (t === a || a.contains(t)); };
  const cs = bar ? getComputedStyle(bar) : null;
  const sheets = [...document.styleSheets].map(s => s.href || '');
  const main = document.querySelector('.gradient-bg ~ .main');
  let upper = 0;
  if (main) main.querySelectorAll('*').forEach(e => { if (getComputedStyle(e).textTransform === 'uppercase') upper++; });
  const chip = document.getElementById('user-chip') || document.querySelector('.app-user-btn');
  return {ui2: document.documentElement.classList.contains('ui2'),
          css: sheets.some(h => h.includes('/static/ui2.css')), cssPages: sheets.some(h => h.includes('/static/ui2-pages.css')),
          sw: document.documentElement.scrollWidth, iw: innerWidth,
          bar: bar ? box(bar) : null, glass: cs ? (cs.backdropFilter || cs.webkitBackdropFilter || '') : '',
          barBg: cs ? cs.backgroundColor : '',
          links: links.map(a => ({t: Math.round(box(a).t), l: box(a).l, r: box(a).r, b: box(a).b, active: a.classList.contains('active'), hit: hitOk(a)})),
          chip: shown(chip) ? box(chip) : null, upper, bodyBg: getComputedStyle(document.body).backgroundColor,
          sideSync: !!document.getElementById('side-sync'), sideNav: !!document.getElementById('side-nav')};
}"""


def measure(page):
    return page.evaluate(_PAGE_JS)


try:
    own = httpx.Client(base_url=BASE, headers={"X-Oborot-CSRF": "1"}, timeout=120)
    own.post("/register", data={"name": "Владелец", "email": "own@ui2p.io", "password": PW, "org_name": "Вид"})
    assert own.post("/api/connect/demo").status_code == 200
    # Карточка первого визита здесь не предмет проверки и перехватывала бы клики.
    own.post("/api/prefs/hints", json={"enabled": False})

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        b = pw.chromium.launch()

        print("\n1. Компьютер 1440: каждая страница в новом виде")
        ctx = b.new_context(viewport={"width": 1440, "height": 900})
        ctx.add_cookies(cookies(own))
        page = ctx.new_page()
        errs: list[str] = []
        cur = [""]
        page.on("pageerror", lambda e: errs.append(f"{cur[0]}: {e}"))
        for path in STANDALONE + BASED:
            cur[0] = path
            page.goto(f"{BASE}{path}")
            settle(page)
            m = measure(page)
            check(f"{path}: класс ui2 и оба файла стилей", m["ui2"] and m["css"] and m["cssPages"], str(m)[:160])
            check(f"{path}: страница не шире окна", m["sw"] <= m["iw"], f"{m['sw']} > {m['iw']}")
            bar = m["bar"]
            check(f"{path}: верхняя панель одна строка у верха страницы, стеклянная",
                  bool(bar) and bar["t"] < 30 and bar["h"] < 80 and bar["w"] > 1200 and "blur" in m["glass"],
                  f"{bar} {m['glass']}")
            tops = {ln["t"] for ln in m["links"]}
            check(f"{path}: все {NAV_COUNT} разделов видны в одну строку и нажимаются",
                  len(m["links"]) == NAV_COUNT and max(tops) - min(tops) <= 6 and all(ln["hit"] for ln in m["links"]),
                  f"{len(m['links'])} {sorted(tops)} hit={sum(1 for ln in m['links'] if ln['hit'])}")
            if path not in ("/account", "/team", "/plans"):
                check(f"{path}: текущий раздел отмечен один", sum(1 for ln in m["links"] if ln["active"]) == 1)
            chip = m["chip"]
            hit = [ln for ln in m["links"] if chip and min(ln["r"], chip["r"]) - max(ln["l"], chip["l"]) > 1
                   and min(ln["b"], chip["b"]) - max(ln["t"], chip["t"]) > 1]
            check(f"{path}: меню пользователя стоит в панели и не лежит на разделах",
                  bool(chip) and bool(bar) and chip["t"] >= bar["t"] - 1 and chip["b"] <= bar["b"] + 1 and not hit,
                  f"chip={chip} bar={bar} hit={len(hit)}")
            if path in STANDALONE:
                check(f"{path}: подписей капсом не осталось", m["upper"] == 0, f"{m['upper']} элементов")
        check("1440: ошибок скрипта нет ни на одной странице", not errs, str(errs[:3]))

        print("\n2. Страницы base.html: разметка каркаса прежняя, меню пользователя работает")
        page.goto(f"{BASE}/settings")
        settle(page)
        m = measure(page)
        check("разметка сайдбара на месте (#side-nav, #side-sync)", m["sideNav"] and m["sideSync"])
        check("панель больше не тёмная", m["barBg"] not in ("rgb(16, 22, 31)", "rgb(22, 30, 41)"), m["barBg"])
        page.click("#user-chip")
        page.wait_for_timeout(300)
        pop = page.evaluate("""() => { const p = document.getElementById('user-pop'); const r = p.getBoundingClientRect();
          return {open: p.classList.contains('open') && r.width > 0, r: r.right, b: r.bottom, l: r.left,
                  out: !!p.querySelector('form[action="/logout"] button[type="submit"]')}; }""")
        check("меню пользователя открывается, целиком в окне, «Выйти» на месте",
              pop["open"] and pop["out"] and pop["l"] >= 0 and pop["r"] <= 1440 and pop["b"] <= 900, str(pop))
        page.keyboard.press("Escape")
        page.evaluate("() => window.scrollTo(0, 600)")
        page.wait_for_timeout(300)
        m2 = measure(page)
        check("при прокрутке панель и меню пользователя остаются вместе у верха",
              m2["bar"]["t"] < 30 and m2["chip"]["t"] >= m2["bar"]["t"] - 1 and m2["chip"]["b"] <= m2["bar"]["b"] + 1,
              f"bar={m2['bar']} chip={m2['chip']}")
        ctx.close()

        print("\n3. Телефон 390")
        ctx = b.new_context(viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True)
        ctx.add_cookies(cookies(own))
        page = ctx.new_page()
        merr: list[str] = []
        page.on("pageerror", lambda e: merr.append(str(e)))
        for path in STANDALONE + BASED:
            page.goto(f"{BASE}{path}")
            settle(page)
            m = measure(page)
            check(f"390 {path}: страница не шире окна", m["sw"] <= m["iw"], f"{m['sw']} > {m['iw']}")
        page.goto(f"{BASE}/settings")
        settle(page)
        btn = page.locator('[data-shell-toggle][aria-controls="side-nav"]')
        check("390 /settings: кнопка меню видна, список свёрнут",
              btn.is_visible() and not page.locator("#side-nav a").first.is_visible())
        btn.click()
        page.wait_for_timeout(300)
        vis = page.evaluate("() => [...document.querySelectorAll('#side-nav a')].filter(a => a.getClientRects().length).length")
        check(f"390 /settings: по кнопке раскрываются все {NAV_COUNT} разделов", vis == NAV_COUNT, str(vis))
        check("390 /settings: меню пользователя видно", page.locator("#user-chip").is_visible())
        check("390: ошибок скрипта нет", not merr, str(merr[:3]))
        ctx.close()

        print("\n4. Вход и встроенный режим")
        ctx = b.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        page.goto(f"{BASE}/login")
        settle(page)
        lg = page.evaluate("""() => { const c = document.querySelector('.auth-card'); const r = c ? c.getBoundingClientRect() : null;
          return {ui2: document.documentElement.classList.contains('ui2'), card: !!r && r.width > 200,
                  form: !!document.querySelector('form input[type="password"]'), sw: document.documentElement.scrollWidth}; }""")
        check("вход: карточка и форма на месте, страница в новом виде", lg["ui2"] and lg["card"] and lg["form"] and lg["sw"] <= 1440, str(lg))
        # Ревью #137: содержимое вне карточки (логотип, строка под ней) лежало под обоями.
        top = page.evaluate("""() => { const hit = sel => { const e = document.querySelector(sel); if (!e) return null;
            const r = e.getBoundingClientRect(); const t = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
            return !!t && (t === e || e.contains(t) || t.contains(e)) && !t.classList.contains('gradient-bg'); };
          return {logo: hit('.auth-logo .logo-text'), card: hit('.auth-card h1'), link: hit('.auth-card a, .auth-note a')}; }""")
        check("вход: логотип, карточка и ссылки лежат поверх обоев и нажимаются",
              top["logo"] is True and top["card"] is True and top["link"] is not False, str(top))
        ctx.close()
        ctx = b.new_context(viewport={"width": 1200, "height": 800})
        ctx.add_cookies(cookies(own))
        page = ctx.new_page()
        for path in ("/settings?embed=1", "/stocks?embed=1"):
            page.goto(f"{BASE}{path}")
            settle(page)
            em = page.evaluate("""() => { const g = document.querySelector('.gradient-bg');
              const bar = document.querySelector('.sidebar') || document.querySelector('.header.app-top');
              return {bg: !!g && g.getClientRects().length > 0, bar: !!bar && bar.getClientRects().length > 0,
                      sw: document.documentElement.scrollWidth, iw: innerWidth}; }""")
            check(f"встроенный режим {path}: своей панели и обоев нет, страница не шире окна",
                  not em["bg"] and not em["bar"] and em["sw"] <= em["iw"], str(em))
        ctx.close()

        print("\n5. Остаток ревью PR #134: подсказки к таблице")
        own.post("/api/prefs/hints", json={"enabled": True})
        ctx = b.new_context(viewport={"width": 1440, "height": 900})
        ctx.add_cookies(cookies(own))
        page = ctx.new_page()
        page.goto(f"{BASE}/turnover?lesson=turnover&step=2")
        page.wait_for_function("() => document.querySelectorAll('#tb tr[data-base]').length > 0", timeout=60000)
        page.wait_for_selector(".tour-card", timeout=20000)
        page.evaluate("() => window.OborotTour.start()")
        page.wait_for_timeout(600)
        st = page.evaluate("() => ({s: window.OborotTour.state(), t: document.getElementById('u-toast').textContent,"
                           " veil: !document.getElementById('u-veil').hidden})")
        check("поверх идущего урока подсказки не запускаются и объясняют почему",
              st["s"]["active"] is False and st["s"]["pending"] is False and not st["veil"] and "урок" in st["t"], str(st))
        page.route("**/api/turnover", lambda route: route.fulfill(status=500, body="{}", content_type="application/json"))
        page.goto(f"{BASE}/turnover")
        page.wait_for_timeout(1500)
        st = page.evaluate("""async () => { window.OborotTour.start(); await new Promise(r => setTimeout(r, 900));
          return {s: window.OborotTour.state(), t: document.getElementById('u-toast').textContent,
                  shield: document.getElementById('u-shield').hidden}; }""")
        check("таблица не загрузилась: тост говорит об ошибке, подсказки не зависли",
              st["s"]["active"] is False and st["s"]["pending"] is False and st["shield"]
              and "не загрузилась" in st["t"] and "нет вещей" not in st["t"], str(st))
        ctx.close()
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
