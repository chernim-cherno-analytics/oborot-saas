# -*- coding: utf-8 -*-
"""UI2 (D-84): «Оборачиваемость» в новом виде и подсказки по клику (в браузере).

Что здесь проверяется и почему не разметкой, а браузером:

1) Первый экран чистый: над таблицей только категории, поиск и действия.
   Справка, деньги в товаре и «Торгуете в минус» лежат НИЖЕ таблицы, раздел
   денег свёрнут, колонка маржи выключена. «Строка есть в шаблоне» этого не
   ловит: блок может быть в шаблоне и стоять не там.
2) Порядок колонок и ячеек строки совпадает, а якоря подсказок (data-k)
   существуют: подсказка, не нашедшая ячейку, светит в пустоту.
3) Цвет строки по-прежнему класс позиции из /api/turnover: вид поменялся,
   смысл нет.
4) Подсказки: вступительная карточка первого визита, одиннадцать шагов по
   клику, контур лежит на своей цели, карточка в окне, Esc закрывает, меню
   «?» возвращает подсказки, «просмотрено» уходит на сервер один раз.
5) «Только с галочкой», галочка маржи и старый сохранённый вид.
6) Архив иконкой, скидка, встроенный режим МойСклада, роль без права на
   каталог, телефон 390.

Запуск: PLAYWRIGHT_BROWSERS_PATH=/tmp/pwb python tests/test_ui2_turnover.py
"""
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DB_PATH = ROOT / "test_ui2_turnover.db"
APP_PORT = int(os.environ.get("OBOROT_TEST_PORT", "8853"))
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
MOBILE = 390

# Порядок колонок = порядок чтения (D-84). Колонка маржи выключена по умолчанию.
COLS = ["name", "turn", "price", "winter", "spring", "summer", "autumn", "dis",
        "stock", "supply", "zat", "defq", "fdisc", "disc"]
CELLS = COLS + ["arch"]
ROW_CLS = {"best": "rg2", "good": "rg1", "dull": "ro", "weak": "rr"}
# Какая ячейка обязана оказаться под контуром на каждом шаге.
STEP_TARGET = {"turn": '[data-k="turn"]', "price": '[data-k="price"]', "seasons": '[data-k="spring"]',
               "dis": '[data-k="dis"]', "stock": '[data-k="stock"]', "norm": '[data-k="zat"]',
               "fdisc": '[data-k="fdisc"]', "disc": '[data-k="disc"]', "arch": '[data-k="arch"]'}
ALL_STEPS = ["turn", "row", "price", "seasons", "dis", "stock", "norm", "fdisc", "disc", "auto", "arch"]


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


def wait_rows(page):
    page.wait_for_function("() => document.querySelectorAll('#tb tr[data-base]').length > 0", timeout=60000)


def overlay_open(page):
    return page.evaluate("() => document.getElementById('hint-overlay').classList.contains('open')")


def wait_overlay(page, want=True, timeout=15000):
    page.wait_for_function(
        "(w) => document.getElementById('hint-overlay').classList.contains('open') === w", arg=want,
        timeout=timeout)


def tour(page):
    return page.evaluate("() => window.OborotTour.state()")


# Геометрия шага: контур, карточка и ячейка-цель строки-примера.
_STEP_JS = """(sel) => {
  const box = e => { const r = e.getBoundingClientRect(); return {l: r.left, t: r.top, r: r.right, b: r.bottom}; };
  const g = document.getElementById('u-glow'), tip = document.getElementById('u-tip'), veil = document.getElementById('u-veil');
  const base = window.OborotTour.state().base;
  let target = null, rowIdx = -1, hasCk = false;
  const rows = [...document.querySelectorAll('#tb tr[data-base]')];
  const row = rows.find(r => r.getAttribute('data-base') === base);
  if (row) { rowIdx = rows.indexOf(row); hasCk = !!row.querySelector('.ck');
    if (sel) { const c = sel === 'row' ? row : row.querySelector(sel); if (c) target = box(c); } }
  return {glowOn: g.classList.contains('on'), glow: box(g), tip: box(tip), tipOn: tip.classList.contains('on'),
          veil: !veil.hidden && veil.classList.contains('on'), clip: veil.style.clipPath || veil.style.webkitClipPath || '',
          target, rowIdx, hasCk, cnt: (tip.querySelector('.u-cnt') || {}).textContent || '',
          title: (tip.querySelector('h3') || {}).textContent || '', vw: innerWidth, vh: innerHeight};
}"""


def overlap(a, b):
    return a and b and min(a["r"], b["r"]) - max(a["l"], b["l"]) > 2 and min(a["b"], b["b"]) - max(a["t"], b["t"]) > 2


def walk_tour(page, label, expect_keys, tap=False):
    """Пройти подсказки до конца, проверяя каждый шаг."""
    page.wait_for_function("() => window.OborotTour.state().step === 0", timeout=10000)
    page.wait_for_timeout(700)
    st = tour(page)
    check(f"{label}: шагов {len(expect_keys)}", st["total"] == len(expect_keys), str(st))
    keys, bad_geo, bad_tip, bad_cnt, bad_view = [], [], [], [], []
    ex = None
    for i in range(st["total"]):
        cur = tour(page)
        keys.append(cur["key"])
        sel = "row" if cur["key"] == "row" else STEP_TARGET.get(cur["key"])
        g = page.evaluate(_STEP_JS, sel)
        if i == 0:
            ex = g
        if cur["key"] == "row":
            # Ревью Codex PR #134: цвет строки это класс по ВЫРУЧКЕ в день, а не
            # по прибыли (убыточная вещь может быть бестселлером). Текст обязан
            # говорить именно это.
            txt = page.evaluate("() => document.querySelector('#u-tip p').textContent")
            check(f"{label}: подсказка про цвет говорит о выручке и не обещает прибыльность",
                  "выручк" in txt and "не прибыль" in txt and "прибыльн" not in txt, txt)
        if cur["key"] == "auto":
            btn = page.evaluate("() => { const r = document.getElementById('defDiscBtn').getBoundingClientRect();"
                                " return {l: r.left, t: r.top, r: r.right, b: r.bottom}; }")
            if not (g["glowOn"] and overlap(g["glow"], btn)):
                bad_geo.append(cur["key"])
        elif not (g["glowOn"] and g["veil"] and overlap(g["glow"], g["target"])):
            bad_geo.append(cur["key"])
        # Сама цель обязана быть на экране, а не только карточка со стрелкой
        # (ревью PR #134: телефон боком, строка уезжала под нижний край).
        gl = g["glow"]
        if not (gl["t"] >= 0 and gl["b"] <= g["vh"] + 0.5 and gl["r"] > 0 and gl["l"] < g["vw"]):
            bad_view.append((cur["key"], gl))
        t = g["tip"]
        if not (g["tipOn"] and t["l"] >= 0 and t["r"] <= g["vw"] + 0.5 and t["t"] >= 0 and t["b"] <= g["vh"] + 0.5):
            bad_tip.append((cur["key"], t))
        if g["cnt"] != f"{i + 1} из {st['total']}":
            bad_cnt.append((cur["key"], g["cnt"]))
        nxt = page.locator("#u-next")
        (nxt.tap if tap else nxt.click)()
        page.wait_for_timeout(650)
    check(f"{label}: порядок шагов", keys == expect_keys, str(keys))
    check(f"{label}: на каждом шаге контур лежит на своей цели, фон затемнён", not bad_geo, str(bad_geo))
    check(f"{label}: цель подсказки на экране на каждом шаге", not bad_view, str(bad_view)[:200])
    check(f"{label}: карточка подсказки целиком в окне на каждом шаге", not bad_tip, str(bad_tip)[:200])
    check(f"{label}: счётчик «N из M» верный", not bad_cnt, str(bad_cnt)[:160])
    page.wait_for_timeout(600)
    end = page.evaluate("() => ({a: window.OborotTour.isActive(), veil: document.getElementById('u-veil').hidden,"
                        " ping: document.getElementById('hint-fab').classList.contains('ping')})")
    check(f"{label}: после «Готово» подсказки закрыты, фон открыт, «?» отмечен", end == {"a": False, "veil": True, "ping": True},
          str(end))
    return ex


try:
    own = httpx.Client(base_url=BASE, headers={"X-Oborot-CSRF": "1"}, timeout=120)
    own.post("/register", data={"name": "Владелец", "email": "own@ui2.io", "password": PW, "org_name": "Вид"})
    check("демо-данные загружены", own.post("/api/connect/demo").status_code == 200)
    api_items = own.get("/api/turnover").json()["items"]
    by_name = {it["base_name"]: it for it in api_items}
    view_id = next(r["id"] for r in own.get("/api/team").json()["roles"] if r["builtin"] == "viewer")
    with sqlite3.connect(DB_PATH) as con:
        org_id = con.execute("SELECT id FROM orgs WHERE name='Вид'").fetchone()[0]
        cur = con.execute("INSERT INTO users (email, pw_hash, name, created_at, session_version) "
                          "VALUES (?,?,?,datetime('now'),1)", ("view@ui2.io", auth.hash_password(PW), "Наблюдатель"))
        con.execute("INSERT INTO memberships (user_id, org_id, role, role_id) VALUES (?,?,?,?)",
                    (cur.lastrowid, org_id, "member", view_id))
        con.commit()
    vc = httpx.Client(base_url=BASE, headers={"X-Oborot-CSRF": "1"}, timeout=60)
    vc.post("/login", data={"email": "view@ui2.io", "password": PW})

    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        ctx = b.new_context(viewport={"width": 1440, "height": 900})
        ctx.add_cookies(cookies(own))
        page = ctx.new_page()
        errs: list[str] = []
        page.on("pageerror", lambda e: errs.append(str(e)))
        page.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
        seen_posts: list[str] = []
        page.on("request", lambda r: seen_posts.append(r.url) if r.method == "POST" and "/api/hints/seen" in r.url else None)

        print("\n1. Первый визит: вступительная карточка и чистый первый экран")
        page.goto(f"{BASE}/turnover")
        wait_rows(page)
        wait_overlay(page, True)
        intro = page.evaluate("""() => ({h: document.querySelector('#hint-body h2').textContent,
          go: !!document.getElementById('hint-tour'), close: document.getElementById('hint-close').textContent,
          ui2: document.documentElement.classList.contains('ui2')})""")
        check("страница в новом виде (класс ui2 на <html>)", intro["ui2"] is True, str(intro))
        check("карточка первого визита называет страницу и предлагает подсказки",
              "оборачиваемости" in intro["h"] and intro["go"] and intro["close"] == "Посмотрю сам", str(intro))
        lay = page.evaluate("""() => { const top = e => e.getBoundingClientRect().top;
          const t = document.getElementById('table-outer').getBoundingClientRect();
          const below = sel => { const e = document.querySelector(sel); return !!e && top(e) >= t.bottom - 1; };
          return {note: below('details.note'), fold: below('#money-fold'), arch: below('#arch-title'),
                  foldOpen: document.getElementById('money-fold').open, noteOpen: document.querySelector('details.note').open,
                  margin: document.getElementById('marginToggle').checked,
                  mcol: getComputedStyle(document.querySelector('th.mcol')).display,
                  tableTop: t.top, catsAbove: top(document.getElementById('cat-bar')) < t.top,
                  toolsAbove: top(document.getElementById('search')) < t.top,
                  between: [...document.querySelector('.main').children].filter(e => { const r = e.getBoundingClientRect();
                    return r.height > 0 && r.bottom <= t.top + 1; }).map(e => e.id || e.className)}; }""")
        check("справка, деньги и архив стоят ниже таблицы", lay["note"] and lay["fold"] and lay["arch"], str(lay))
        check("раздел денег и справка свёрнуты", lay["foldOpen"] is False and lay["noteOpen"] is False, str(lay))
        check("над таблицей только категории и строка инструментов",
              lay["between"] == ["cat-bar", "tools"] and lay["catsAbove"] and lay["toolsAbove"], str(lay["between"]))
        check("маржа в таблице по умолчанию выключена", lay["margin"] is False and lay["mcol"] == "none", str(lay))

        print("\n2. Колонки, ячейки и цвет строк")
        cols = page.evaluate("""() => [...document.querySelectorAll('#mt thead th[data-sk]')]
          .filter(th => getComputedStyle(th).display !== 'none').map(th => th.getAttribute('data-sk'))""")
        check("порядок колонок: оборачиваемость, цена, сезоны, сток, скидки", cols == COLS, str(cols))
        rows = page.evaluate("""() => [...document.querySelectorAll('#tb tr[data-base]')].map(r => ({
          base: r.getAttribute('data-base'), cls: r.className, cells: [...r.children].map(c => c.getAttribute('data-k')),
          bg: getComputedStyle(r.children[1]).backgroundColor, first: getComputedStyle(r.children[0]).backgroundColor}))""")
        check("строки отрисованы", len(rows) > 3, str(len(rows)))
        check("у каждой строки ячейки идут в порядке заголовков и несут якоря подсказок",
              all(r["cells"] == CELLS for r in rows), str([r["cells"] for r in rows if r["cells"] != CELLS][:1]))
        ranked = [r for r in rows if by_name.get(r["base"], {}).get("group") == "rank"]
        wrong = [(r["base"], r["cls"]) for r in ranked if r["cls"] != ROW_CLS.get(by_name[r["base"]]["cls"])]
        check("класс строки = класс позиции из /api/turnover", ranked and not wrong, str(wrong[:3]))
        colors = {}
        for r in ranked:
            colors.setdefault(r["cls"], set()).add(r["bg"])
        check("у каждого класса свой цвет строки, одинаковый внутри класса",
              all(len(v) == 1 for v in colors.values()) and len({next(iter(v)) for v in colors.values()}) == len(colors),
              str(colors))
        check("залипшая ячейка названия того же цвета, что строка (не просвечивает)",
              all(r["bg"] == r["first"] and "rgba" not in r["first"] for r in ranked), str(ranked[:1]))
        unit = page.evaluate("() => document.querySelector('#th-turnover .th-unit').textContent.trim()")
        check("у оборачиваемости видна единица ₽/день", unit == "₽/день", unit)

        print("\n3. Подсказки: запуск из карточки, 11 шагов, отметка на сервере")
        page.click("#hint-tour")
        wait_overlay(page, False)
        ex = walk_tour(page, "1440", ALL_STEPS)
        check("строка-пример не первая в списке", ex and ex["rowIdx"] > 0, str(ex and ex["rowIdx"]))
        check("у строки-примера есть галочка надёжности", ex and ex["hasCk"] is True, str(ex and ex["hasCk"]))
        check("«просмотрено» ушло на сервер один раз", len(seen_posts) == 1, str(seen_posts))
        seen = own.get("/api/hints/seen").json().get("seen") or []
        check("и сервер это запомнил", "turnover" in seen, str(seen))

        print("\n4. Меню «?» возвращает подсказки, Esc закрывает, повторно карточка не всплывает")
        page.click("#hint-fab")
        label = page.evaluate("() => document.querySelector('#hm-cols span').textContent")
        check("пункт меню называется «Подсказки к таблице»", label == "Подсказки к таблице", label)
        page.click("#hm-cols")
        page.wait_for_function("() => window.OborotTour.state().step === 0", timeout=10000)
        check("пункт меню запускает подсказки сразу, без карточки", not overlay_open(page) and tour(page)["active"])
        page.keyboard.press("ArrowRight")
        page.wait_for_timeout(500)
        check("стрелка вправо листает", tour(page)["step"] == 1, str(tour(page)))
        page.keyboard.press("Escape")
        page.wait_for_timeout(700)
        check("Esc закрывает подсказки", tour(page)["active"] is False and page.evaluate(
            "() => document.getElementById('u-veil').hidden"))
        page.reload()
        wait_rows(page)
        page.wait_for_timeout(2500)
        check("при повторном заходе карточка первого визита не всплывает", not overlay_open(page))
        check("лишних отметок «просмотрено» нет", len(seen_posts) == 1, str(seen_posts))

        print("\n5. «Только с галочкой», маржа и старый сохранённый вид")
        page.click("#relToggle")
        page.wait_for_timeout(300)
        okv = page.evaluate("""() => ({p: document.getElementById('relToggle').getAttribute('aria-pressed'),
          rows: [...document.querySelectorAll('#tb tr[data-base]')].map(r => !!r.querySelector('[data-k="dis"] .ck')),
          reset: document.getElementById('resetView').textContent,
          shown: getComputedStyle(document.getElementById('resetView')).display})""")
        check("кнопка включена и в таблице остались только вещи с галочкой",
              okv["p"] == "true" and okv["rows"] and all(okv["rows"]), str(okv)[:160])
        check("плашка «Сбросить вид» называет фильтр", okv["shown"] != "none" and "только с галочкой" in okv["reset"],
              okv["reset"])
        page.reload()
        wait_rows(page)
        check("фильтр пережил перезагрузку",
              page.evaluate("() => document.getElementById('relToggle').getAttribute('aria-pressed')") == "true")
        page.click("#resetView")
        page.wait_for_timeout(300)
        check("«Сбросить вид» выключает фильтр",
              page.evaluate("() => document.getElementById('relToggle').getAttribute('aria-pressed')") == "false")
        page.locator("#money-fold > summary").click()
        page.check("#marginToggle")
        page.wait_for_timeout(300)
        mv = page.evaluate("""() => ({th: getComputedStyle(document.querySelector('th.mcol')).display,
          cells: [...document.querySelector('#tb tr[data-base]').children].map(c => c.getAttribute('data-k'))})""")
        check("галочка в разделе денег возвращает колонку маржи",
              mv["th"] != "none" and mv["cells"] == CELLS[:3] + ["margin"] + CELLS[3:], str(mv))
        page.reload()
        wait_rows(page)
        check("выбор маржи пережил перезагрузку",
              page.evaluate("() => document.getElementById('marginToggle').checked") is True)
        # Прежняя раскладка сохраняла `margin: true` у каждого, кто менял вид: в
        # новой это не должно включать маржу само.
        page.evaluate("""() => { const k = Object.keys(localStorage).find(x => x.indexOf('oborot.view.turnover.') === 0);
          localStorage.setItem(k, JSON.stringify({margin: true})); }""")
        page.reload()
        wait_rows(page)
        check("старый сохранённый вид (margin: true) маржу не включает",
              page.evaluate("() => document.getElementById('marginToggle').checked") is False
              and page.evaluate("() => getComputedStyle(document.querySelector('th.mcol')).display") == "none")

        print("\n6. Архив иконкой и скидка")
        victim = page.evaluate("() => document.querySelector('#tb tr[data-base]:last-of-type').getAttribute('data-base')")
        n_before = page.evaluate("() => document.querySelectorAll('#tb tr[data-base]').length")
        page.locator(f'#tb tr[data-base="{victim}"] .arch-btn').click()
        page.wait_for_function("(n) => document.querySelectorAll('#tb tr[data-base]').length === n - 1", arg=n_before,
                               timeout=10000)
        hidden = own.get("/api/turnover").json()["items"]
        check("кнопка архива убирает строку и сервер это сохранил",
              any(it["base_name"] == victim and it["hidden"] for it in hidden))
        page.click("#arch-title")
        back = page.locator(f'#atb tr[data-base="{victim}"] .unarch-btn')
        check("вещь видна в архиве под таблицей с кнопкой «Вернуть»",
              back.count() == 1 and (back.text_content() or "").strip() == "Вернуть")
        back.click()
        page.wait_for_function("(n) => document.querySelectorAll('#tb tr[data-base]').length === n", arg=n_before,
                               timeout=10000)
        check("«Вернуть» возвращает вещь в таблицу",
              not any(it["base_name"] == victim and it["hidden"] for it in own.get("/api/turnover").json()["items"]))
        inp = page.locator("#tb tr[data-base] .disc-in").first
        base_name = inp.get_attribute("data-base")
        inp.fill("15")
        inp.press("Tab")
        page.wait_for_timeout(700)
        saved = own.get("/api/discount-overrides").json()
        check("ручная скидка сохраняется и подсвечивается", saved.get(base_name) == 15
              and "has" in (inp.get_attribute("class") or ""), str(saved)[:120])
        inp.fill("")
        inp.press("Tab")
        page.wait_for_timeout(500)
        check("ошибок на странице не было", not errs, str(errs[:2])[:240])
        ctx.close()

        print("\n6а. Замечания ревью PR #134")
        ctx = b.new_context(viewport={"width": 1440, "height": 900})
        ctx.add_cookies(cookies(own))
        page = ctx.new_page()
        rerr: list[str] = []
        page.on("pageerror", lambda e: rerr.append(str(e)))
        writes: list[str] = []
        page.on("request", lambda r: writes.append(r.url.split("?")[0]) if r.method != "GET" else None)
        page.goto(f"{BASE}/turnover")
        wait_rows(page)
        page.wait_for_timeout(800)
        # Вид человека: вторая категория и сортировка по остатку.
        page.locator("#cat-bar .cat-btn[data-cat]").nth(1).click()
        page.locator('#mt thead th[data-sk="stock"]').click()
        page.wait_for_timeout(300)
        view_js = """() => ({cat: (document.querySelector('#cat-bar .cat-btn.active') || {}).getAttribute
          ? document.querySelector('#cat-bar .cat-btn.active').getAttribute('data-cat') : null,
          sort: (document.querySelector('#mt thead th.sorted') || {getAttribute: () => null}).getAttribute('data-sk')})"""
        before = page.evaluate(view_js)
        check("вид задан: категория и сортировка по остатку", before["cat"] and before["sort"] == "stock", str(before))
        n_rows = len(own.get("/api/turnover").json()["items"])
        page.evaluate("() => window.OborotTour.start()")
        page.wait_for_function("() => window.OborotTour.state().step === 0", timeout=10000)
        page.wait_for_timeout(500)
        during = page.evaluate(view_js)
        check("подсказки идут на исходном виде (первая категория, без сортировки)",
              during["sort"] is None and during["cat"] != before["cat"], str(during))
        # Enter на «Назад» нажимает «Назад», а не листает вперёд.
        page.keyboard.press("ArrowRight")
        page.wait_for_timeout(450)
        page.keyboard.press("ArrowRight")
        page.wait_for_timeout(450)
        page.locator("#u-tip .u-back").focus()
        page.keyboard.press("Enter")
        page.wait_for_timeout(450)
        check("Enter на кнопке «Назад» возвращает на шаг назад", tour(page)["step"] == 1, str(tour(page)))
        # Клик по подсвеченной кнопке архива не архивирует вещь.
        for _ in range(9):
            page.keyboard.press("ArrowRight")
            page.wait_for_timeout(380)
        last = tour(page)
        check("дошли до шага про архив", last["key"] == "arch" and last["step"] == last["total"] - 1, str(last))
        pos = page.evaluate("""() => { const b = window.OborotTour.state().base;
          const r = [...document.querySelectorAll('#tb tr[data-base]')].find(x => x.getAttribute('data-base') === b)
            .querySelector('.arch-btn').getBoundingClientRect(); return {x: r.left + r.width / 2, y: r.top + r.height / 2}; }""")
        writes.clear()
        page.mouse.click(pos["x"], pos["y"])
        page.wait_for_timeout(900)
        still = own.get("/api/turnover").json()["items"]
        check("клик по подсвеченной кнопке архива во время подсказок ничего не архивирует",
              not any(u.endswith("/api/hidden") for u in writes) and not any(it["hidden"] for it in still)
              and len(still) == n_rows, str(writes))
        check("этот клик листает подсказку (последний шаг закрывает их)", tour(page)["active"] is False)
        after = page.evaluate(view_js)
        check("после подсказок вид человека вернулся", after == before, f"{before} -> {after}")
        page.reload()
        wait_rows(page)
        check("и он же сохранён: подсказки вид не стёрли", page.evaluate(view_js) == before, str(page.evaluate(view_js)))
        page.click("#resetView")
        # Данные ещё грузятся: подсказки ждут загрузку, а не объявляют таблицу пустой.
        held = {"go": False}

        def slow(route):
            deadline = time.time() + 20
            while not held["go"] and time.time() < deadline:
                page.wait_for_timeout(100)
            route.continue_()

        page.route("**/api/turnover", slow)
        page.goto(f"{BASE}/turnover")
        page.wait_for_timeout(1200)
        page.evaluate("() => window.OborotTour.start()")
        page.wait_for_timeout(400)
        st = tour(page)
        toast = page.evaluate("() => document.getElementById('u-toast').textContent")
        check("пока таблица грузится, подсказки ждут и говорят об этом",
              st["pending"] is True and st["active"] is False and "Загружаем таблицу" in toast, f"{st} {toast}")
        held["go"] = True
        page.wait_for_function("() => window.OborotTour.state().step === 0", timeout=30000)
        check("после загрузки подсказки стартуют сами", tour(page)["active"] is True)
        page.keyboard.press("Escape")
        page.unroute("**/api/turnover")
        check("в этих сценариях ошибок страницы нет", not rerr, str(rerr[:2]))
        ctx.close()

        print("\n7. Урок находит свои цели в новой раскладке")
        ctx = b.new_context(viewport={"width": 1440, "height": 900})
        ctx.add_cookies(cookies(own))
        page = ctx.new_page()
        lerr: list[str] = []
        page.on("pageerror", lambda e: lerr.append(str(e)))
        page.goto(f"{BASE}/turnover?lesson=turnover&step=2")
        wait_rows(page)
        page.wait_for_selector(".tour-card", timeout=20000)
        page.wait_for_timeout(800)
        ring = page.evaluate("""() => { const r = document.querySelector('.tour-ring'), t = document.getElementById('th-supply');
          if (!r || r.hidden || !t) return null; const a = r.getBoundingClientRect(), b = t.getBoundingClientRect();
          return Math.min(a.right, b.right) - Math.max(a.left, b.left) > 2 && Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 2; }""")
        check("шаг про запас обводит колонку «Запас»", ring is True, str(ring))
        page.goto(f"{BASE}/turnover?lesson=turnover&step=3")
        wait_rows(page)
        page.wait_for_selector(".tour-card", timeout=20000)
        page.wait_for_timeout(800)
        note = page.evaluate("""() => { const d = document.querySelector('details.note'), i = document.getElementById('minstock-in');
          const r = i.getBoundingClientRect(); return {open: d.open, vis: r.width > 0 && r.height > 0}; }""")
        check("шаг про порог наличия сам раскрывает справку внизу страницы", note == {"open": True, "vis": True}, str(note))
        check("в уроке ошибок страницы нет", not lerr, str(lerr[:2]))
        ctx.close()

        print("\n8. Встроенный режим МойСклада")
        ctx = b.new_context(viewport={"width": 1280, "height": 800})
        ctx.add_cookies(cookies(own))
        page = ctx.new_page()
        eerr: list[str] = []
        page.on("pageerror", lambda e: eerr.append(str(e)))
        page.goto(f"{BASE}/turnover?embed=1")
        wait_rows(page)
        emb = page.evaluate("""() => ({hdr: getComputedStyle(document.querySelector('.header')).display,
          bg: getComputedStyle(document.querySelector('.gradient-bg')).display,
          tabs: !!document.querySelector('.embed-tabs'), sw: document.documentElement.scrollWidth, vw: innerWidth})""")
        check("во фрейме своя шапка и фон спрятаны, лента разделов есть",
              emb["hdr"] == "none" and emb["bg"] == "none" and emb["tabs"], str(emb))
        check("во фрейме 1280 страница не шире окна", emb["sw"] <= emb["vw"] + 1, str(emb))
        check("во фрейме ошибок страницы нет", not eerr, str(eerr[:2]))
        ctx.close()

        print("\n9. Роль без права на каталог: скрытого не подсвечиваем")
        ctx = b.new_context(viewport={"width": 1440, "height": 900})
        ctx.add_cookies(cookies(vc))
        page = ctx.new_page()
        page.goto(f"{BASE}/turnover")
        wait_rows(page)
        wait_overlay(page, True)
        page.click("#hint-tour")
        page.wait_for_function("() => window.OborotTour.state().step === 0", timeout=10000)
        vis = page.evaluate("""() => ({btn: getComputedStyle(document.getElementById('defDiscBtn')).display,
          arch: getComputedStyle(document.querySelector('.arch-btn')).display})""")
        check("у наблюдателя кнопки скидок и архива скрыты", vis == {"btn": "none", "arch": "none"}, str(vis))
        check("поле скидки наблюдатель видит, но менять не может",
              page.evaluate("() => getComputedStyle(document.querySelector('.disc-in')).pointerEvents") == "none")
        walk_tour(page, "наблюдатель", [k for k in ALL_STEPS if k not in ("disc", "auto", "arch")])
        ctx.close()

        print("\n10. Телефон 390")
        ctx = b.new_context(viewport={"width": MOBILE, "height": 844}, has_touch=True, is_mobile=True)
        ctx.add_cookies(cookies(own))
        page = ctx.new_page()
        merr: list[str] = []
        page.on("pageerror", lambda e: merr.append(str(e)))
        page.goto(f"{BASE}/turnover")
        wait_rows(page)
        page.wait_for_timeout(1500)
        sw = page.evaluate("() => document.documentElement.scrollWidth")
        check(f"390: страница не шире окна ({MOBILE})", sw <= MOBILE + 1, str(sw))
        check("390: карточка первого визита уже не всплывает (просмотрено на сервере)", not overlay_open(page))
        page.evaluate("() => window.OborotTour.start()")
        walk_tour(page, "390", ALL_STEPS, tap=True)
        check("390: ошибок страницы нет", not merr, str(merr[:2]))
        ctx.close()

        print("\n11. Телефон боком 844×390")
        ctx = b.new_context(viewport={"width": 844, "height": MOBILE}, has_touch=True, is_mobile=True)
        ctx.add_cookies(cookies(own))
        page = ctx.new_page()
        page.goto(f"{BASE}/turnover")
        wait_rows(page)
        page.wait_for_timeout(1200)
        page.evaluate("() => window.OborotTour.start()")
        walk_tour(page, "844×390", ALL_STEPS, tap=True)
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
