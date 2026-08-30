# -*- coding: utf-8 -*-
"""Owner-only предпросмотр онбординга: доступ, ноль записей, путь, адаптив.

Зачем этот набор существует. Страница `/onboarding/preview` даёт владельцу
визуальную приёмку будущего онбординга и при этом обещает три вещи, каждая из
которых легко ломается молча:

  1) ДОСТУП. Обещание «только владелец» держится на сервере, а не на спрятанной
     кнопке. Проверяется тремя ролями сразу: аноним, участник организации и
     владелец, — и отдельно тем, что существующие `/onboarding` и `/turnover`
     от появления предпросмотра не изменились.
  2) НОЛЬ ЗАПИСЕЙ. Обещание «настройки не изменены» проверяется не чтением
     кода, а снимком ВСЕЙ базы до и после полного прохода предпросмотра в
     настоящем браузере: побайтовый отпечаток каждой таблицы обязан совпасть.
     Перед этим инструмент проверяется на себе (§3): синтетическая запись
     обязана отпечаток СДВИНУТЬ — иначе «отпечаток совпал» доказывало бы
     только то, что мы ничего не измеряем.
  3) ЧЕСТНОСТЬ ЭКРАНА. Предпросмотр не пересчитывает канон и не выдумывает
     чисел: значения таблицы сверяются с ответом `GET /api/turnover` поле в
     поле, ноль сезона отличается от «нет данных», спорная математика Б/Т
     закрыта объяснением, а демонстрация хода загрузки отделена от живых
     данных так, что перепутать их нельзя.

Плюс адаптив и клавиатура: 1440x900, 1024x768 и 390x844 (без горизонтальной
прокрутки страницы — широкая таблица едет только внутри своего контейнера),
цели нажатия не меньше 44px, Escape, ловушка фокуса в диалоге экскурсии,
prefers-reduced-motion.

Данные — только синтетический демо-бренд самого проекта (`app/demo_seed.py`):
ни одного настоящего SKU и ни одной коммерческой суммы клиента.

Запуск из корня репозитория:  python tests/test_onboarding_preview.py

Нужен Chromium под playwright: `pip install -r requirements-dev.lock` и
`python -m playwright install chromium`.
"""
import copy
import hashlib
import json
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "test_onboarding_preview.db"
SHOTS_DIR = ROOT / "_preview_shots"
APP_PORT = int(os.environ.get("OBOROT_TEST_PORT", "8819"))

os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["SCHEDULER_ENABLED"] = "0"

if DB_PATH.exists():
    DB_PATH.unlink()

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from app.main import app as oborot_app  # noqa: E402

PASS, FAIL = [], []

PREVIEW_URL = "/onboarding/preview"
WELCOME_TITLE = "Оборот помогает выстроить торговую матрицу"
# Белый список ручек страницы. Продублирован здесь намеренно: тест обязан знать
# ожидаемый список независимо от шаблона, иначе расширение списка в шаблоне
# «проверялось» бы само собой.
ALLOWED_PATHS = {
    "/api/settings", "/api/sync/progress", "/api/turnover",
    "/api/discount-rule", "/api/discount-overrides",
}
# Пять спокойных этапов доски загрузки в объявленном порядке (было восемь
# технических — «Склады»/«Товары и цены»/«Остатки» слиты в «Товары и остатки»,
# «Расчёт оборачиваемости»/«Подготовка матрицы» слиты в «Матрица»).
SEMANTIC_STAGES = ["connection", "catalog", "month", "history", "matrix"]
# Шаги пути предпросмотра в объявленном порядке.
STEP_KEYS = ["welcome", "warehouses", "horizon", "discounts", "bt", "groups",
             "loader", "turnover"]


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        PASS.append(name)
        print(f"  OK   {name}" + (f"  [{detail}]" if detail else ""))
    else:
        FAIL.append(name)
        print(f"  FAIL {name}  {detail}")


BASE = f"http://127.0.0.1:{APP_PORT}"


class ServerThread:
    def __init__(self, asgi_app, port: int):
        self.config = uvicorn.Config(asgi_app, host="127.0.0.1", port=port,
                                     log_level="warning")
        self.server = uvicorn.Server(self.config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self):
        self.thread.start()
        deadline = time.time() + 20
        while time.time() < deadline:
            if self.server.started:
                return
            time.sleep(0.05)
        raise RuntimeError(f"сервер на порту {self.config.port} не поднялся")

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=10)


def client(follow: bool = False) -> httpx.Client:
    return httpx.Client(headers={"X-Oborot-CSRF": "1"}, base_url=BASE,
                        timeout=120.0, follow_redirects=follow)


def exec_sql(query: str, *args) -> int:
    con = sqlite3.connect(DB_PATH)
    try:
        cur = con.execute(query, args)
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def add_member(org_id: int, email: str) -> int:
    """Участник организации: приглашений в UI нет, заводим строкой в БД."""
    import bcrypt
    pw = bcrypt.hashpw(b"secret123", bcrypt.gensalt()).decode()
    uid = exec_sql(
        "INSERT INTO users (email, pw_hash, name, created_at)"
        " VALUES (?,?,?,datetime('now'))",
        email, pw, email.split("@")[0])
    exec_sql("INSERT INTO memberships (user_id, org_id, role) VALUES (?,?,'member')",
             uid, org_id)
    return uid


# ── Отпечаток базы ───────────────────────────────────────────────────────────
#
# Снимок берётся со ВСЕХ пользовательских таблиц, а не с заранее выбранного
# списка «важных». Список важных таблиц — это утверждение о том, куда страница
# теоретически может написать; проверять надо ровно наоборот: она не пишет
# НИКУДА, включая таблицы, о которых автор проверки не подумал.

def _tables(con) -> list:
    rows = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
        " AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
    return [r[0] for r in rows]


def db_snapshot() -> dict:
    """{'digest': sha256 всей базы, 'tables': {имя: (строк, sha256 таблицы)}}."""
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        whole = hashlib.sha256()
        per = {}
        for t in _tables(con):
            h = hashlib.sha256()
            n = 0
            # Порядок строк фиксируется rowid: без ORDER BY SQLite вправе
            # отдать их в любом порядке, и «отпечаток разошёлся» означало бы
            # порядок выдачи, а не изменение данных.
            for row in con.execute(f'SELECT * FROM "{t}" ORDER BY rowid'):
                n += 1
                h.update(repr(row).encode("utf-8", "replace"))
                h.update(b"\x1e")
            per[t] = (n, h.hexdigest())
            whole.update(t.encode("utf-8"))
            whole.update(str(n).encode())
            whole.update(h.digest())
        return {"digest": whole.hexdigest(), "tables": per}
    finally:
        con.close()


def snapshot_diff(a: dict, b: dict) -> list:
    """Человекочитаемое различие двух снимков (пусто — байт в байт одно и то же)."""
    out = []
    names = sorted(set(a["tables"]) | set(b["tables"]))
    for t in names:
        ra, rb = a["tables"].get(t), b["tables"].get(t)
        if ra is None:
            out.append(f"{t}: таблица появилась ({rb[0]} строк)")
        elif rb is None:
            out.append(f"{t}: таблица исчезла")
        elif ra != rb:
            out.append(f"{t}: было {ra[0]} строк/{ra[1][:8]}, стало {rb[0]}/{rb[1][:8]}")
    return out


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        # Код 77 И причина одновременно — иначе раннер засчитает это падением
        # (D-42): набор, не открывший ни одной страницы, не должен выглядеть
        # зелёным.
        print("ПРОПУЩЕНО: playwright не установлен — поставьте "
              "requirements-dev.lock и выполните `python -m playwright "
              "install chromium`")
        return 77
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


# ── §1 Доступ и совместимость существующих контрактов ────────────────────────

def part_access(owner: httpx.Client, member: httpx.Client) -> None:
    print("\n== §1 Доступ: аноним, участник, владелец ==")
    anon = client()
    r = anon.get(PREVIEW_URL)
    check("аноним не видит предпросмотр (302)", r.status_code == 302, str(r.status_code))
    check("аноним уходит на /login",
          r.headers.get("location") == "/login", str(r.headers.get("location")))

    r = member.get(PREVIEW_URL)
    check("участник организации не видит предпросмотр (302)",
          r.status_code == 302, str(r.status_code))
    check("участник получает безопасный отказ — редирект на /turnover",
          r.headers.get("location") == "/turnover", str(r.headers.get("location")))

    r = owner.get(PREVIEW_URL)
    check("владелец открывает предпросмотр (200)", r.status_code == 200, str(r.status_code))
    html = r.text
    check("на первом экране обещание продукта дословно",
          WELCOME_TITLE in html, WELCOME_TITLE)
    check("бейдж «настройки не изменены» есть в разметке",
          "настройки не изменены" in html)

    print("\n== §1 Изоляция шаблона: ни одного унаследованного писателя ==")
    # base.html тянет /static/app.js и _hints.html, а те сами шлют POST
    # (/api/hints/seen, /api/prefs/hints, /api/lessons/{key}/done,
    # /api/sync/run). Предпросмотр обязан не наследовать их вовсе.
    check("шаблон не подключает /static/app.js", "/static/app.js" not in html)
    # Скрипты страницы — единственное место, откуда может уйти запрос. Проверяем
    # именно их, а не всю разметку: `/api/productions` НАЗВАН в видимом тексте
    # шага «Производство» сознательно — там объяснено, почему эта ручка не
    # используется (она делает commit). Запрещено обращение, а не упоминание.
    scripts = "\n".join(
        html[m.start():html.index("</script>", m.start())]
        for m in __import__("re").finditer(r"<script[^>]*>", html))
    for marker, why in (("/api/hints/seen", "отметки подсказок"),
                        ("/api/prefs/hints", "тумблер подсказок"),
                        ("/api/lessons", "прогресс обучения"),
                        ("/api/sync/run", "запуск синхронизации"),
                        ("/api/productions", "ручка, создающая производство")):
        check(f"в скриптах страницы нет обращения к {marker} ({why})",
              marker not in scripts)
    check("объяснение про /api/productions при этом человеку показано",
          "/api/productions" in html)
    check("в разметке нет ни одной формы", "<form" not in html.lower())
    for m in ("method=\"post\"", "method='post'"):
        check(f"в разметке нет {m}", m not in html.lower())

    print("\n== §1 Маршрут отвечает только на GET ==")
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        rr = owner.request(method, PREVIEW_URL)
        check(f"{method} {PREVIEW_URL} отклонён", rr.status_code in (403, 405),
              str(rr.status_code))

    print("\n== §1 Существующие контракты не изменились ==")
    r_onb = owner.get("/onboarding")
    check("/onboarding у подключённой организации по-прежнему уводит на /",
          r_onb.status_code == 302 and r_onb.headers.get("location") == "/",
          f"{r_onb.status_code} {r_onb.headers.get('location')}")
    r_anon_onb = anon.get("/onboarding")
    check("/onboarding анониму по-прежнему отдаёт /login",
          r_anon_onb.status_code == 302 and r_anon_onb.headers.get("location") == "/login",
          f"{r_anon_onb.status_code} {r_anon_onb.headers.get('location')}")
    check("/turnover владельцу по-прежнему 200", owner.get("/turnover").status_code == 200)
    check("/turnover участнику по-прежнему 200", member.get("/turnover").status_code == 200)
    check("/turnover анониму по-прежнему уводит на /login",
          anon.get("/turnover").headers.get("location") == "/login")
    check("предпросмотр в разметке /turnover не появился",
          PREVIEW_URL not in owner.get("/turnover").text)
    anon.close()


# ── §2 Инструмент отпечатка проверяется на себе ──────────────────────────────

def part_instrument(owner: httpx.Client, turnover_before: dict) -> dict:
    print("\n== §2 Красная проверка: отпечаток базы действительно ловит запись ==")
    before = db_snapshot()
    item = None
    for it in turnover_before["items"]:
        if not it["archived"] and not it.get("hidden"):
            item = it
            break
    assert item is not None, "в демо-данных нет ни одной живой позиции"
    r = owner.post("/api/discount-overrides",
                   json={"base_name": item["base_name"], "discount": 7})
    check("контрольная запись выполнена (POST /api/discount-overrides)",
          r.status_code == 200, str(r.status_code))
    after = db_snapshot()
    diff = snapshot_diff(before, after)
    check("настоящая запись СДВИГАЕТ отпечаток — инструмент рабочий",
          before["digest"] != after["digest"] and bool(diff), "; ".join(diff)[:160])
    check("сдвиг виден именно в таблице ручных скидок",
          any(d.startswith("sku_discounts") for d in diff), "; ".join(diff)[:160])
    return after


# ── §3 Полный проход в браузере + доказательство нуля записей ────────────────

def wait_ready(page) -> None:
    page.wait_for_selector("body[data-pv-ready='1']", timeout=30000)


def pv(page, expr: str):
    return page.evaluate("() => " + expr)


def wait_scroll_stable(page, checks: int = 3, interval_ms: int = 80, rounds: int = 30) -> None:
    """Ждёт, пока плавный scrollIntoView() экскурсии действительно завершится.

    renderTour() вызывает cell.scrollIntoView({behavior:"smooth"}) на
    подсвеченной ячейке; съёмка скриншота сразу после появления диалога (без
    этого ожидания) ловила промежуточный, ещё едущий кадр — рваная/обрезанная
    рамка на PNG, не связанная ни с одной логической проверкой DOM. Опрашиваем
    и вертикальную прокрутку страницы, и горизонтальную прокрутку таблицы
    (scrollIntoView может двигать обе), пока оба значения не перестанут
    меняться `checks` опросов подряд — это дисциплина съёмки скриншота, а не
    изменение анимации для настоящих пользователей.
    """
    last = None
    stable = 0
    for _ in range(rounds):
        cur = page.evaluate(
            "() => { const w = document.getElementById('pv-tablewrap');"
            " return [Math.round(window.scrollY), w ? Math.round(w.scrollLeft) : 0]; }")
        if cur == last:
            stable += 1
            if stable >= checks:
                return
        else:
            stable = 0
        last = cur
        page.wait_for_timeout(interval_ms)


def digits(s: str) -> str:
    """Только цифры и минус.

    Числа на экране печатает toLocaleString('ru-RU'), а он разделяет разряды
    НЕРАЗРЫВНЫМ пробелом. Сравнивать такую строку с str(int) напрямую нельзя, и
    ловить это глазами в отчёте о падении — потерянные полчаса.
    """
    return "".join(ch for ch in str(s) if ch.isdigit() or ch == "-")


def digits_in(text: str, value) -> bool:
    """Есть ли число `value` в тексте, напечатанном с разрядами.

    toLocaleString('ru-RU') рвёт разряды неразрывным пробелом, поэтому «2 000»
    ищется в проекции текста на одни цифры, а не подстрокой как есть.
    """
    return str(int(value)) in digits(text)


def walk_tour(page) -> list:
    """Проходит экскурсию с первого шага до последнего, собирая заголовки и текст.

    Проход именно кнопкой «Дальше», а не присваиванием индекса: проверяется то,
    чем пользуется человек, а не внутреннее состояние.
    """
    out = []
    for _ in range(30):
        out.append({
            "title": (page.text_content("#pv-tour-title") or "").strip(),
            "text": (page.text_content("#pv-tour-text") or "").strip(),
        })
        if pv(page, "document.getElementById('pv-tour-next').disabled"):
            break
        page.click("#pv-tour-next")
        page.wait_for_timeout(90)
    return out


def part_browser(pw, cookies, owner: httpx.Client, snap_before: dict) -> dict:
    from playwright.sync_api import TimeoutError as PWTimeoutError  # noqa: F401

    browser = pw.chromium.launch()
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    ctx.add_cookies([{"name": k, "value": v, "domain": "127.0.0.1", "path": "/"}
                     for k, v in cookies.items()])
    errors = []
    page = ctx.new_page()
    page.on("pageerror", lambda e: errors.append(str(e)))
    # Независимое от собственного сторожа страницы доказательство метода:
    # список наблюдаемых Playwright запросов — то, что реально ушло по сети,
    # а не то, что страница сама о себе рассказывает через __PV_GUARD__.
    # Раньше «доказательство только GET/HEAD» опиралось исключительно на
    # список ПУТЕЙ из window.__PV_GUARD__.passed — тот же источник, который
    # сторож заполняет сам себе, так что баг в самом стороже остался бы
    # незамеченным. real_requests — сторонний наблюдатель этого не повторяет.
    real_requests = []
    page.on("request", lambda req: real_requests.append((req.method, req.url)))
    SHOTS_DIR.mkdir(exist_ok=True)

    turnover = owner.get("/api/turnover").json()
    settings = owner.get("/api/settings").json()
    rule = owner.get("/api/discount-rule").json()
    overrides = owner.get("/api/discount-overrides").json()

    page.goto(f"{BASE}{PREVIEW_URL}")
    wait_ready(page)

    print("\n== §3 Связный путь: восемь экранов по одному вопросу ==")
    steps = pv(page, "Array.from(document.querySelectorAll('.pv-step'))"
                     ".map(s => s.getAttribute('data-step'))")
    check("шаги объявлены в заявленном порядке", steps == STEP_KEYS, str(steps))
    shown = pv(page, "Array.from(document.querySelectorAll('.pv-step.is-on'))"
                     ".map(s => s.getAttribute('data-step'))")
    check("на экране ровно один шаг, и это приветствие", shown == ["welcome"], str(shown))
    check("заголовок приветствия дословный",
          (page.text_content("#pv-h-welcome") or "").strip() == WELCOME_TITLE)

    print("\n== §3 Приветствие за 5–10 секунд: коротко, по-человечески ==")
    welcome_lead = (page.inner_text("[data-step='welcome'] .pv-lead") or "").strip()
    check("вводный текст короткий (меньше ~40 слов — читается за 5-10 секунд)",
          len(welcome_lead.split()) <= 40, f"{len(welcome_lead.split())} слов: {welcome_lead}")
    check("видимый текст приветствия не содержит сырых путей ручек",
          "GET /api" not in (page.inner_text("[data-step='welcome']") or ""))
    # «Минута» разрешена ТОЛЬКО применительно к вопросам (реальный факт: 5
    # коротких вопросов). Полная история за 730 дней в проде занимает ~22
    # минуты, поэтому «минута» рядом со словами «матрица»/«данные»/«история»
    # была бы конкретным ложным обещанием готовности, а не честной оценкой.
    check("«минута» привязана дословно к вопросам, а не к готовности матрицы",
          "На вопросы уйдёт около минуты" in welcome_lead, welcome_lead)
    check("«минута» НЕ соседствует с обещанием готовых данных/матрицы/истории",
          not any(bad in welcome_lead for bad in
                  ("матрицу — на всё", "данные — на", "историю — на",
                   "готова через минуту", "данные появятся через минуту")),
          welcome_lead)
    check("ровно одно заметное уведомление «ответы не сохраняются» на экране",
          "Предпросмотр — ответы не сохраняются" in
          (page.inner_text("[data-step='welcome']") or ""))
    points = pv(page, "Array.from(document.querySelectorAll("
                      "'[data-step=\"welcome\"] .pv-plainlist li'))"
                      ".map(li => li.textContent.trim())")
    check("«что вы увидите» — не больше трёх понятных пунктов, без жаргона",
          0 < len(points) <= 3 and not any("GET" in p or "Б/Т" in p for p in points),
          str(points))
    check("технические детали (белый список ручек) свёрнуты по умолчанию",
          not pv(page, "document.querySelector('[data-step=\"welcome\"] details.pv-tech').open"))
    tech_txt = page.text_content("[data-step='welcome'] details.pv-tech") or ""
    check("но доказуемы: белый список из пяти GET-ручек по-прежнему там",
          tech_txt.count("/api/") >= 5, tech_txt[:200])

    focused = pv(page, "document.activeElement && document.activeElement.id")
    page.click("[data-step='welcome'] [data-go='next']")
    page.wait_for_selector("[data-step='warehouses'].is-on")
    focused = pv(page, "document.activeElement && document.activeElement.id")
    check("после перехода фокус уходит на заголовок нового шага",
          focused == "pv-h-warehouses", str(focused))

    print("\n== §3 Программный фокус заголовка: без гигантского синего контура ==")
    heading_outline = page.evaluate(
        "() => getComputedStyle(document.getElementById('pv-h-warehouses')).outlineStyle")
    check("у программно сфокусированного заголовка шага контур выключен "
          "(outline-style: none)",
          heading_outline == "none", heading_outline)
    # page.focus() — программный вызов, как и у заголовка: Chromium применяет
    # :focus-visible к нему по другой эвристике, чем к настоящему Tab с
    # клавиатуры (для «кликабельных» элементов вроде <button> программный
    # фокус его иногда не показывает вовсе). Настоящая клавиатурная
    # навигация — единственный источник истины для этой проверки.
    page.keyboard.press("Tab")
    active_tag = page.evaluate("() => document.activeElement && document.activeElement.tagName")
    btn_outline = page.evaluate(
        "() => getComputedStyle(document.activeElement).outlineStyle")
    check("а настоящий Tab с клавиатуры на интерактивный элемент кольцо не теряет",
          btn_outline == "solid", f"{active_tag}: {btn_outline}")

    print("\n== §3 Склады: только чтение текущего состояния ==")
    wh_names = pv(page, "Array.from(document.querySelectorAll('#pv-wh li'))"
                        ".map(li => li.textContent.trim())")
    check("склады показаны все, сколько их в /api/settings",
          len(wh_names) == len(settings["warehouses"]),
          f"{len(wh_names)} vs {len(settings['warehouses'])}")
    for w in settings["warehouses"]:
        found = [t for t in wh_names if w["name"] in t]
        check(f"склад «{w['name']}» показан со своим состоянием",
              bool(found) and (("продаём" in found[0]) == bool(w["active"])),
              str(found[:1]))
    inputs = pv(page, "document.querySelectorAll('#pv-wh input, #pv-wh select,"
                      " #pv-wh button').length")
    check("на экране складов нет ни одного элемента ввода", inputs == 0, str(inputs))

    print("\n== §3 Горизонт: 30/60/90/120/свой, локальный черновик ==")
    page.click("[data-step='warehouses'] [data-go='next']")
    page.wait_for_selector("[data-step='horizon'].is-on")
    opts = pv(page, "Array.from(document.querySelectorAll('#pv-horizon-opts .pv-opt'))"
                    ".map(b => b.getAttribute('data-v'))")
    check("предложены ровно объявленные варианты горизонта",
          opts == ["30", "60", "90", "120", "custom"], str(opts))
    page.click("#pv-horizon-opts [data-v='120']")
    check("выбор 120 дн. отмечен в разметке",
          pv(page, "document.querySelector(\"#pv-horizon-opts [data-v='120']\")"
                   ".getAttribute('aria-checked')") == "true")
    # Стрелка вправо внутри radiogroup: клавиатурное поведение группы выбора.
    page.focus("#pv-horizon-opts [data-v='120']")
    page.keyboard.press("ArrowRight")
    check("стрелка вправо переводит выбор на «свой»",
          pv(page, "window.__PV__.draft.horizonMode") == "custom",
          pv(page, "window.__PV__.draft.horizonMode"))
    check("слайдер своего горизонта показан",
          pv(page, "!document.getElementById('pv-horizon-custom').hidden"))
    check("текущая настройка организации названа и подписана как неизменная",
          "не меняет" in (page.text_content("#pv-horizon-now") or ""),
          (page.text_content("#pv-horizon-now") or "")[:80])
    check("черновик горизонта не тронул настройку организации",
          owner.get("/api/settings").json()["horizon_days_fixed"]
          == settings["horizon_days_fixed"])

    print("\n== §3 Минимальная и максимальная скидка: локальный черновик ==")
    page.click("[data-step='horizon'] [data-go='next']")
    page.wait_for_selector("[data-step='discounts'].is-on")
    page.evaluate("""() => {
      const el = document.getElementById('pv-disc-min');
      el.value = '60'; el.dispatchEvent(new Event('input', {bubbles:true}));
    }""")
    lo = pv(page, "window.__PV__.draft.discMin")
    hi = pv(page, "window.__PV__.draft.discMax")
    check("минимум не может перескочить максимум", lo <= hi, f"{lo} > {hi}")
    check("черновик подписан как несохраняемый",
          "не затронута" in (page.text_content("#pv-disc-hint") or ""),
          (page.text_content("#pv-disc-hint") or "")[:80])
    check("черновик скидок не тронул правило организации",
          owner.get("/api/discount-rule").json() == rule)

    print("\n== §3 Б/Т: спорная математика закрыта, чисел нет ==")
    page.click("[data-step='discounts'] [data-go='next']")
    page.wait_for_selector("[data-step='bt'].is-on")
    bt = page.text_content("#pv-bt-body") or ""
    check("замок формулы закрыт и назван",
          "формулы Б/Т в проекте нет" in bt, bt[:120])
    check("отказ опирается на решения владельца D-23 и D-35",
          "D-23" in bt and "D-35" in bt)
    check("сказано прямо, что ни одного числа не показано",
          "не показано ни одного числа" in bt)
    gate = page.text_content("#pv-bt-gate") or ""
    # Единственные цифры, которым позволено стоять в блоке отказа, — номера
    # решений владельца. Любая другая цифра здесь означала бы, что экран всё-таки
    # что-то посчитал.
    bare = (gate.replace("D-23", "").replace("D-35", "")
            .replace("BUSINESS_LOGIC.md \u00a70", "BUSINESS_LOGIC.md"))
    check("в блоке замка формулы нет ни одного числа, кроме номеров решений и ссылок",
          not any(ch.isdigit() for ch in bare),
          "".join(ch for ch in bare if ch.isdigit())[:40])
    # Замок истории проверяется на обеих ветках: живой ответ и подменённый
    # синтетический «история загружена целиком».
    hist = page.text_content("#pv-bt-history") or ""
    check("замок истории назвал покрытие числами из /api/sync/progress",
          "из" in hist and any(ch.isdigit() for ch in hist), hist[:100])

    gate_visible = page.inner_text("#pv-bt-gate") or ""
    check("видимая (не свёрнутая) часть замка формулы не тащит D-23/D-35/"
          "BUSINESS_LOGIC.md в первое прочтение — они в технических деталях",
          "D-23" not in gate_visible and "D-35" not in gate_visible
          and "BUSINESS_LOGIC" not in gate_visible, gate_visible[:200])
    check("но видимая часть всё равно объясняет решение по-человечески",
          "плохая рекомендация хуже" in gate_visible.lower(), gate_visible[:200])

    page.route("**/api/sync/progress", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body='{"state":"done","mode":"initial","phase":"","progress_pct":100,'
             '"detail":"","error":"","error_cause":"","coverage_days":730,'
             '"history_days":730,"window_days":30,'
             '"months":[{"ym":"2026-08","state":"done"},{"ym":"2026-07","state":"done"}],'
             '"stages":[{"key":"products","title":"Товары и цены","state":"done",'
             '"seconds":3,"counts":{"products_total":40}},'
             '{"key":"today","title":"Остатки на сегодня","state":"done",'
             '"seconds":2,"counts":{"warehouses":3}},'
             '{"key":"month","title":"Продажи","state":"done","seconds":4,"counts":{}},'
             '{"key":"history","title":"История","state":"done","seconds":9,"counts":{}}],'
             '"eta_sec":null,"started_at":null,"finished_at":null}'))
    page.reload()
    wait_ready(page)
    for _ in range(4):
        page.click(".pv-step.is-on [data-go='next']")
        page.wait_for_timeout(90)
    page.wait_for_selector("[data-step='bt'].is-on")
    hist_full = page.text_content("#pv-bt-history") or ""
    check("на полной истории первый замок открыт",
          "загружена полностью" in hist_full, hist_full[:100])
    gate_full = page.text_content("#pv-bt-gate") or ""
    check("но замок формулы остаётся закрытым и на полной истории",
          "формулы Б/Т в проекте нет" in gate_full, gate_full[:100])

    print("\n== §3 Доска загрузки: живое и демонстрация неперепутываемы ==")
    page.click("[data-step='bt'] [data-go='next']")
    page.wait_for_selector("[data-step='groups'].is-on")
    page.click("#pv-groups .pv-chip")
    check("черновик производственных групп остаётся в памяти вкладки",
          len(pv(page, "window.__PV__.draft.groups")) == 1,
          str(pv(page, "window.__PV__.draft.groups")))
    groups_visible = page.inner_text("[data-step='groups']") or ""
    check("на шаге групп не видно сырого GET /api/productions/commit объяснения "
          "в первом прочтении",
          "GET /api/productions" not in groups_visible, groups_visible[:200])
    groups_tech = page.text_content("[data-step='groups'] details.pv-tech") or ""
    check("но оно доказуемо в свёрнутых технических деталях",
          "GET /api/productions" in groups_tech and "commit" in groups_tech,
          groups_tech[:200])
    page.click("[data-step='groups'] [data-go='next']")
    page.wait_for_selector("[data-step='loader'].is-on")

    print("\n== §3 Загрузка: правда о времени, не «меньше минуты» ==")
    loader_lead = (page.inner_text("[data-step='loader'] .pv-lead") or "").strip()
    check("вступление не обещает конкретный (и заведомо неверный) срок",
          "меньше минуты" not in loader_lead and "минуту" not in loader_lead,
          loader_lead)
    check("демо-кнопка называется по-человечески «Посмотреть, как проходит "
          "загрузка», а не «демонстрацию хода»",
          "Посмотреть, как проходит загрузка" in
          (page.inner_text("#pv-demo-btn") or ""))

    keys = pv(page, "Array.from(document.querySelectorAll('#pv-stages .pv-stage'))"
                    ".map(s => s.getAttribute('data-key'))")
    check("пять спокойных смысловых этапов в объявленном порядке (было восемь)",
          keys == SEMANTIC_STAGES, str(keys))

    # Главный статус — одна спокойная строка над списком этапов, не пусто и
    # не тире: на мобильном именно её видно над сгибом экрана, список этапов
    # может быть уже ниже.
    mainstatus = page.inner_text("#pv-mainstatus") or ""
    check("главный статус непустой и не тире/idle",
          bool(mainstatus.strip()) and mainstatus.strip() not in ("—", ""), mainstatus)
    check("на полностью загруженных (в этом сценарии) данных статус говорит "
          "«матрица готова»", "Матрица готова" in mainstatus, mainstatus)

    # inner_text, а не text_content: лента «ДЕМОНСТРАЦИЯ» в живом режиме
    # спрятана стилем, и text_content вернул бы её как показанную.
    board = page.inner_text("#pv-board") or ""
    check("в живом режиме на доске нет слова ДЕМО",
          "ДЕМО" not in board and "ДЕМОНСТРАЦИЯ" not in board)
    check("сырые имена полей контракта не видны в первом прочтении доски "
          "(свёрнуты в технические детали)",
          "coverage_days" not in board and "progress_pct" not in board, board[:200])
    check("сырое состояние contract (idle и т.п.) не показано как есть — "
          "только человеческое слово",
          "idle" not in board, board[:300])
    check("жаргон «нет поля» не виден в первом прочтении",
          "нет поля" not in board, board[:300])
    check("помесячная диагностическая сетка (2026-08 и т.п.) не в первом "
          "прочтении доски",
          not pv(page, "!!document.querySelector('#pv-stages .pv-month')"))
    right_texts = pv(page, "Array.from(document.querySelectorAll("
                          "'#pv-stages .pv-stage-right')).map(el => el.textContent.trim())")
    # Явный запрет по каждой карточке отдельно (не только «нет ни одного» —
    # какая именно карточка нарушает, видно сразу): ни тире, ни idle, ни «нет
    # поля» не разрешены нигде в живом режиме. Пустая строка (сознательно
    # опущенный статус у этапа без поля контракта) — единственное разрешённое
    # «ничего», и то только потому, что это явный выбор, а не суррогат.
    FORBIDDEN_RIGHT = ("—", "-", "idle", "нет поля")
    bad_rights = [t for t in right_texts if t in FORBIDDEN_RIGHT]
    check("ни одна правая подпись карточки не равна —/idle/«нет поля» "
          "в живом режиме",
          not bad_rights, f"{bad_rights} из {right_texts}")
    check("правые подписи карточек — человеческие слова, не сырые токены контракта",
          all(t in ("", "ожидает", "идёт", "готово", "ошибка",
                    "получены", "получены ранее", "получаем", "недоступна")
              or t.endswith("складов") or t.endswith("склада") or t.endswith("склад")
              or t.endswith(" с") or "готово" in t or "дн." in t
              for t in right_texts),
          str(right_texts))

    print("\n== §3 Доска загрузки: технические детали свёрнуты, но доказательство цело ==")
    page.click("#pv-board details.pv-tech summary")
    page.wait_for_timeout(120)
    tech_foot = page.text_content("#pv-board-foot") or ""
    for field in ("coverage_days", "history_days", "progress_pct", "state", "eta_sec"):
        check(f"технические детали доски называют поле контракта {field}", field in tech_foot)
    check("помесячная сетка доказуема в свёрнутых технических деталях "
          "(просто переехала, не потерялась)",
          pv(page, "document.querySelectorAll('#pv-board-foot .pv-month').length") > 0)
    # У «Матрицы» нет отдельного ПОЛЯ КОНТРАКТА (state=nofield по-прежнему
    # означало бы «это не из stages[]») — но её видимое состояние честно
    # берётся из реального PV.data.turnover, а не молчит: в этом сценарии
    # /api/turnover читается нормально, поэтому карточка говорит «готово»,
    # а не «nofield»/тире.
    matrix_key_state = pv(page, "(() => { const li = document.querySelector("
                                "'#pv-stages .pv-stage[data-key=\"matrix\"]');"
                                " return li ? { state: li.getAttribute('data-state'),"
                                " right: li.querySelector('.pv-stage-right').textContent.trim() }"
                                " : null; })()")
    check("«Матрица» без своего поля контракта, но с прочитанным turnover — "
          "состояние «done», подпись «готово» (согласовано с турновером, не пусто)",
          matrix_key_state == {"state": "done", "right": "готово"}, str(matrix_key_state))
    check("для неё прямо сказано в технических деталях, что отдельного поля в "
          "контракте нет (само состояние карточки при этом берётся из турновера)",
          "отдельного поля" in tech_foot, tech_foot[:200])
    page.click("#pv-board details.pv-tech summary")
    page.wait_for_timeout(80)
    check("технические детали доски снова свёрнуты",
          not pv(page, "document.querySelector('#pv-board details.pv-tech').open"))

    page.click("#pv-demo-btn")
    page.wait_for_timeout(400)
    check("демонстрация переключает режим доски",
          pv(page, "document.getElementById('pv-board').getAttribute('data-mode')") == "demo")
    demo_txt = page.inner_text("#pv-board") or ""
    check("демонстрация подписана словом ДЕМОНСТРАЦИЯ", "ДЕМОНСТРАЦИЯ" in demo_txt)
    check("сказано, что числа синтетические", "синтетические" in demo_txt)
    demo_status = page.inner_text("#pv-mainstatus") or ""
    check("главный статус тоже подписан демонстрацией явно",
          "демонстрация" in demo_status.lower(), demo_status)
    tags = pv(page, "document.querySelectorAll('#pv-stages .pv-demotag').length")
    check("каждый этап демонстрации помечен отдельно",
          tags == len(SEMANTIC_STAGES), str(tags))
    check("живые поля контракта на время демонстрации в первом прочтении по-прежнему не видны",
          "coverage_days" not in demo_txt and "progress_pct" not in demo_txt)
    moved = set()
    for _ in range(10):
        page.wait_for_timeout(320)
        moved.add(tuple(pv(page, "Array.from(document.querySelectorAll("
                                 "'#pv-stages .pv-stage')).map(s => s.getAttribute("
                                 "'data-state'))")))
    check("демонстрация действительно показывает ход, а не статичную картинку",
          len(moved) > 1, f"различных состояний: {len(moved)}")

    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    check("Escape гасит демонстрацию и возвращает живой режим",
          pv(page, "document.getElementById('pv-board').getAttribute('data-mode')") == "live")

    print("\n== §3 Предпросмотр «Оборачиваемости»: числа канона без пересчёта ==")
    page.unroute("**/api/sync/progress")
    page.click("[data-step='loader'] [data-go='next']")
    page.wait_for_selector("[data-step='turnover'].is-on")

    turnover_step_html = page.inner_text("[data-step='turnover']") or ""
    check("главный показатель назван коротко: «Денежная отдача»",
          "Денежная отдача" in turnover_step_html, turnover_step_html[:400])
    check("рядом — короткая подпись «выручка в день наличия», без полного "
          "определения на первом экране",
          "выручка в день наличия" in turnover_step_html, turnover_step_html[:400])
    check("полное определение (с «не прибыль») в первом прочтении НЕ повторено — "
          "оно живёт в попапе «Объяснения», см. проверку ниже",
          "не прибыль" not in turnover_step_html, turnover_step_html[:400])

    # Сверяем бейдж с тем, что реально загрузила сама страница (PV.data.progress),
    # а не с независимым свежим запросом к ручке: несколькими строками выше тест
    # нарочно подменял /api/sync/progress через page.route для сценария «история
    # полная», и после page.unroute данные страницы в памяти не обновляются сами
    # без перезагрузки — свежий owner.get() и то, что видит уже отрисованная
    # страница, тут два разных числа, и бейдж обязан следовать за вторым.
    page_progress = pv(page, "window.__PV__.data.progress")
    prelim_hidden = pv(page, "document.getElementById('pv-prelim').hidden")
    cov = (page_progress or {}).get("coverage_days") or 0
    if cov >= 730:
        check("история полная (>=730 дней) — бейдж «предварительно» скрыт",
              prelim_hidden, f"coverage_days={cov}")
    else:
        check("история неполная (<730 дней) — бейдж «предварительно» показан",
              not prelim_hidden, f"coverage_days={cov}")
        prelim_txt = page.text_content("#pv-prelim") or ""
        check("бейдж называет число дней покрытия и канон 730",
              str(cov) in prelim_txt and "730" in prelim_txt, prelim_txt[:160])

    print("\n== §3 «На что обратить внимание сегодня»: подсчёт существующих полей ==")
    attn_txt = page.inner_text("#pv-attn") or ""
    check("блок явно подписан как предпросмотр, а не рекомендация действия",
          "Только предпросмотр" in attn_txt, attn_txt[:300])
    live_all = [it for it in turnover["items"] if not it.get("hidden") and not it["archived"]]
    exp_weak = sum(1 for it in live_all if it["cls"] == "weak")
    exp_below = sum(1 for it in live_all if it.get("below_cost"))
    exp_lowdata = sum(1 for it in live_all if it.get("low_data"))
    attn_numbers = pv(page, "Array.from(document.querySelectorAll('#pv-attn .pv-attn-item b'))"
                            ".map(el => el.textContent.trim())")
    check("сводка называет число слабых, ниже себестоимости и «мало данных» — "
          "ровно как в подсчёте по полям ответа ручки (без новой формулы)",
          attn_numbers[:3] == [str(exp_weak), str(exp_below), str(exp_lowdata)],
          f"{attn_numbers[:3]} vs {[exp_weak, exp_below, exp_lowdata]}")

    print("\n== §3 Поиск по таблице: подписанное поле, только визуальный фильтр ==")
    label_for = pv(page, "document.querySelector('label[for=\"pv-search\"]') ? "
                        "document.querySelector('label[for=\"pv-search\"]').getAttribute('for') : null")
    check("поле поиска связано с <label> через for/id", label_for == "pv-search", str(label_for))
    sample_item = next((it for it in live_all if it["base_name"]), None)
    assert sample_item is not None
    query = sample_item["base_name"][:4]
    page.fill("#pv-search", query)
    page.wait_for_timeout(150)
    rows_after_search = pv(page, "document.querySelectorAll('#pv-tbody tr:not(.pv-grouprow)').length")
    expected_after = sum(1 for it in live_all if query.lower() in it["base_name"].lower())
    check("поиск фильтрует строки визуально, без обращения к сети",
          rows_after_search == expected_after, f"{rows_after_search} vs {expected_after}")
    check("поиск не тронул сами данные /api/turnover",
          owner.get("/api/turnover").json() == turnover)
    page.fill("#pv-search", "")
    page.wait_for_timeout(150)

    print("\n== §3 Переключатель объяснений: выключен по умолчанию, focus-managed ==")
    check("глобальный переключатель объяснений по умолчанию выключен",
          pv(page, "document.getElementById('pv-help-toggle').getAttribute('aria-pressed')") == "false")
    check("попап объяснений изначально скрыт",
          pv(page, "document.getElementById('pv-help-pop').hidden"))
    btn_box = pv(page, "(() => { const r = document.getElementById('pv-help-toggle')"
                       ".getBoundingClientRect(); return [r.width, r.height]; })()")
    check("кнопка объяснений — полноценная цель нажатия (>=44px)",
          btn_box[0] >= 44 and btn_box[1] >= 44, str(btn_box))
    page.click("#pv-help-toggle")
    page.wait_for_timeout(150)
    check("клик открывает попап и включает состояние",
          not pv(page, "document.getElementById('pv-help-pop').hidden")
          and pv(page, "document.getElementById('pv-help-toggle').getAttribute('aria-pressed')") == "true")
    check("фокус уходит в попап (на кнопку закрытия)",
          pv(page, "document.activeElement && document.activeElement.id") == "pv-help-close")
    page.keyboard.press("Escape")
    page.wait_for_timeout(150)
    check("Escape закрывает попап объяснений",
          pv(page, "document.getElementById('pv-help-pop').hidden"))
    check("фокус возвращается на кнопку, открывшую попап",
          pv(page, "document.activeElement && document.activeElement.id") == "pv-help-toggle")
    check("после закрытия переключатель снова выключен",
          pv(page, "document.getElementById('pv-help-toggle').getAttribute('aria-pressed')") == "false")

    # Второй триггер — на уровне заголовка метрики (шаг «Оборачиваемость»), а не
    # точка в ячейке таблицы: искать точку в ячейках заведомо нечего.
    check("в ячейках таблицы нет отдельных кнопок-точек объяснения",
          pv(page, "document.querySelectorAll('#pv-tbody .pv-help, #pv-tbody [data-help]').length") == 0)
    page.click("#pv-help-turnover")
    page.wait_for_timeout(150)
    help_title = page.text_content("#pv-help-title") or ""
    check("метричный триггер открывает объяснение именно денежной отдачи",
          "Денежная отдача" in help_title, help_title[:120])
    help_text = page.text_content("#pv-help-text") or ""
    check("полное определение и «не прибыль» — здесь, в объяснении, а не на "
          "каждом экране подряд",
          "не прибыл" in help_text and "730" in help_text, help_text[:300])
    page.click("#pv-help-close")
    page.wait_for_timeout(120)
    check("кнопка закрытия тоже закрывает попап и возвращает фокус",
          pv(page, "document.getElementById('pv-help-pop').hidden")
          and pv(page, "document.activeElement && document.activeElement.id") == "pv-help-turnover")

    live_items = [it for it in turnover["items"] if not it.get("hidden") and not it["archived"]]
    rank = [it for it in live_items if it["group"] == "rank" and not it["low_data"]]
    expected_hero = rank[len(rank) // 2]["base_name"] if rank else None
    hero = pv(page, "window.__PV__.hero && window.__PV__.hero.base_name")
    check("строка экскурсии — настоящая строка из середины рейтинговой группы",
          hero == expected_hero, f"{hero} vs {expected_hero}")
    check("строка экскурсии не крайняя",
          bool(rank) and hero != rank[0]["base_name"] and hero != rank[-1]["base_name"],
          str(hero))

    rows = pv(page, "document.querySelectorAll('#pv-tbody tr:not(.pv-grouprow)').length")
    check("показаны все строки ответа ручки",
          rows == len([it for it in turnover["items"] if not it.get("hidden")]),
          str(rows))

    cells = pv(page, """(() => {
      const tr = document.querySelector('#pv-tbody tr.pv-hero');
      if (!tr) return null;
      const out = {};
      tr.querySelectorAll('[data-tour]').forEach(td => {
        const k = td.getAttribute('data-tour');
        if (!(k in out)) out[k] = td.textContent.trim();
      });
      out.cls = tr.className;
      return out;
    })()""")
    check("строка экскурсии найдена в таблице", cells is not None)
    if cells and expected_hero:
        h = [it for it in turnover["items"] if it["base_name"] == expected_hero][0]
        check("оборачиваемость показана ровно как в /api/turnover",
              digits(cells["turn"]) == str(round(h["turnover"])),
              f"{cells['turn']!r} vs {round(h['turnover'])}")
        check("дни в стоке показаны ровно как в /api/turnover",
              digits(cells["dis"]) == str(round(h["dis"])),
              f"{cells['dis']!r} vs {round(h['dis'])}")
        check("остаток показан ровно как в /api/turnover",
              digits(cells["cs"]) == str(round(h["cs"])),
              f"{cells['cs']!r} vs {round(h['cs'])}")
        check("класс строки взят из поля cls, а не пересчитан",
              {"best": "rg2", "good": "rg1", "dull": "ro", "weak": "rr"}[h["cls"]]
              in cells["cls"], f"{cells['cls']} vs {h['cls']}")
        check("средняя фактическая цена показана как в ответе ручки",
              str(round(h["avg_price"])) in digits(cells["price"]), cells["price"][:60])
        check("номинальная цена показана рядом с фактической",
              str(round(h["sale_price"])) in digits(cells["price"]), cells["price"][:60])
        check("«Не хватает до нормы» не пересчитано, а честно пусто",
              cells["need"].strip() == "\u2014", repr(cells["need"])[:40])

    print("\n== §3 Сезон: ноль и «нет данных» — разные ячейки ==")
    sea_seen = pv(page, """(() => {
      const out = {zero: 0, dash: 0};
      document.querySelectorAll('#pv-tbody tr').forEach(tr => {
        const tds = tr.querySelectorAll('td');
        for (let i = 5; i <= 8; i++) {
          if (!tds[i]) continue;
          const t = tds[i].textContent.trim();
          if (t === '0') out.zero++;
          else if (t === '—') out.dash++;
        }
      });
      return out;
    })()""")
    api_zero = sum(1 for it in turnover["items"] if not it.get("hidden")
                   for s in ("winter", "spring", "summer", "autumn")
                   if (it.get("sea") or {}).get(s) == 0)
    api_null = sum(1 for it in turnover["items"] if not it.get("hidden")
                   for s in ("winter", "spring", "summer", "autumn")
                   if (it.get("sea") or {}).get(s) is None)
    check("нулей сезона на экране столько же, сколько нулей в ответе ручки",
          sea_seen["zero"] == api_zero, f"{sea_seen['zero']} vs {api_zero}")
    check("прочерков сезона столько же, сколько null в ответе ручки",
          sea_seen["dash"] == api_null, f"{sea_seen['dash']} vs {api_null}")
    sea_title = pv(page, """(() => {
      const el = document.querySelector('#pv-tbody td .pv-na[title]');
      return el ? el.getAttribute('title') : null;
    })()""")
    if api_null:
        check("прочерк сезона подписан как «данных нет», а не как ноль",
              bool(sea_title) and "Данных нет" in sea_title
              and "Ноль вместо них не рисуется" in sea_title, str(sea_title)[:140])
    else:
        # На этих демо-данных все сезоны покрыты. Молча пропускать проверку
        # нельзя: «не проверяли» и «прошло» — разные вещи, поэтому ветка
        # утверждает ровно то, что можно утверждать.
        check("непокрытых сезонов в данных нет — прочеркам взяться неоткуда",
              sea_seen["dash"] == 0, str(sea_seen))

    print("\n== §3 Денежный слой скрыт по умолчанию и только в предпросмотре ==")
    check("полоса сумм по умолчанию скрыта",
          not pv(page, "document.getElementById('pv-money').classList.contains('is-on')"))
    check("столбец маржи по умолчанию скрыт",
          pv(page, "getComputedStyle(document.querySelector('#pv-thead th.pv-hidecol'))"
                   ".display") == "none")
    # Себестоимость под ценой гасится АТРИБУТОМ hidden, а не классом, поэтому
    # проверяется отдельно: ровно здесь `.pv-sub{display:block}` однажды уже
    # перебил UA-правило `[hidden]`, и «с/с 4 029» стояло на экране приёмки при
    # выключенном денежном слое.
    check("себестоимость под ценой по умолчанию скрыта",
          pv(page, "getComputedStyle(document.querySelector"
                   "('#pv-tbody [data-money=\"1\"]')).display") == "none",
          str(pv(page, "getComputedStyle(document.querySelector"
                       "('#pv-tbody [data-money=\"1\"]')).display")))
    check("и её текста нет в видимом тексте таблицы",
          "с/с" not in (page.inner_text("#pv-tbody") or ""),
          (page.inner_text("#pv-tbody") or "")[:120])
    page.click("#pv-money-btn")
    page.wait_for_timeout(200)
    check("тумблер открывает денежный слой",
          pv(page, "document.getElementById('pv-money').classList.contains('is-on')"))
    check("и столбец маржи вместе с ним",
          pv(page, "getComputedStyle(document.querySelector('#pv-thead th.pv-hidecol'))"
                   ".display") != "none")
    check("и себестоимость под ценой вместе с ним",
          "с/с" in (page.inner_text("#pv-tbody") or ""))
    page.click("#pv-money-btn")
    page.wait_for_timeout(200)
    check("повторное нажатие снова прячет себестоимость",
          "с/с" not in (page.inner_text("#pv-tbody") or ""))
    check("настоящая «Оборачиваемость» денежный слой по-прежнему показывает",
          "money-bar" in owner.get("/turnover").text)

    print("\n== §3 Экскурсия по строке: ровно 5 шагов, 1 мысль + короткие строки, "
          "технические детали свёрнуты ==")
    page.click("#pv-tour-start")
    page.wait_for_selector("#pv-tour:not([hidden])")

    # Первый шаг ДО прохода кнопкой «Дальше»: видимый (inner_text) текст короткий
    # и без канона/полей, а полный текст (text_content, ниже через walk_tour)
    # всё равно доказуем — потому что лежит в свёрнутых технических деталях,
    # а не выброшен совсем.
    visible0 = page.inner_text("#pv-tour-text") or ""
    check("видимый текст первого шага короткий: без канона D-35 и без сырых полей",
          "D-35" not in visible0 and "GET /api/turnover" not in visible0, visible0[:220])
    check("но «не прибыль» — базовая честность про деньги — видна сразу, без раскрытия",
          "не прибыль" in visible0, visible0[:220])
    check("на первом шаге есть свёрнутый разворот «Технические детали»",
          pv(page, "!!document.querySelector('#pv-tour-text details.pv-tech')"))
    check("и он свёрнут по умолчанию",
          not pv(page, "document.querySelector('#pv-tour-text details.pv-tech').open"))

    steps_tour = walk_tour(page)
    check("экскурсия сведена ровно к пяти шагам (было десять)",
          len(steps_tour) == 5, str(len(steps_tour)))
    joined = " | ".join(s["title"] for s in steps_tour)
    for need in ("Денежная отдача", "Цена", "Сезон", "Дни в стоке", "Скидки и архив"):
        check(f"экскурсия проходит пункт «{need}»", need in joined, joined[:260])
    for title in (s["title"] for s in steps_tour):
        check(f"заголовок шага короткий (1 мысль, не длинное предложение): «{title}»",
              len(title) <= 40, f"{len(title)} символов")

    def tour_text(i):
        return steps_tour[i]["text"] if i < len(steps_tour) else ""

    turn_txt = tour_text(0)
    check("прямо сказано, что это не прибыль",
          "не прибыль" in turn_txt, turn_txt[:220])
    check("канон истории (730) доказуем — он в свёрнутых технических деталях",
          "730" in turn_txt, turn_txt[:220])

    sea_txt = tour_text(2)
    check("ноль объяснён как «продаж не было»",
          "продаж не было" in sea_txt, sea_txt[:200])
    check("прочерк объяснён как «данных ещё нет»",
          "данных ещё нет" in sea_txt, sea_txt[:200])
    check("значок «±» (возвраты превысили продажи) объяснён отдельно от обоих",
          "возвраты" in sea_txt and "перевесили" in sea_txt, sea_txt[:200])
    check("решение владельца D-34 доказуемо — в свёрнутых технических деталях",
          "D-34" in sea_txt, sea_txt[:200])

    stock_txt = tour_text(3)
    check("шаг про запас честно объясняет пустую колонку «Не хватает»",
          "пуст сознательно" in stock_txt, stock_txt[:220])
    check("тот же шаг называет канон 730 (в технических деталях)",
          "730" in stock_txt, stock_txt[:220])

    disc_txt = tour_text(4)
    check("скидки описаны числами существующего правила",
          digits_in(disc_txt, rule["rule"]["top_turnover"])
          and digits_in(disc_txt, rule["rule"]["weak_pct"]), disc_txt[:260])
    check("и прямо сказано, что предпросмотр правило не применяет",
          "не применяется" in disc_txt, disc_txt[:260])
    check("тот же шаг честно называет архив только чтением",
          "только" in disc_txt and "информация" in disc_txt, disc_txt[:260])
    check("экскурсия ничего не применила: ручные скидки те же",
          owner.get("/api/discount-overrides").json() == overrides)

    print("\n== §3 Подсветка экскурсии — стабильные атрибуты, без nth-child ==")
    check("шаблон не использует позиционный DOM-таргетинг (:nth-child) для тура",
          ":nth-child" not in page.content())
    # Шаг «Дни в стоке, остаток и запас» подсвечивает СРАЗУ обе ячейки dis и wos —
    # проверка, что подсветка не позиционная, а по нескольким стабильным ключам.
    # walk_tour дошёл до последнего (пятого) шага кнопкой «Дальше»; возвращаемся
    # на четвёртый (индекс 3) той же кнопкой «Назад», которой пользуется человек.
    page.click("#pv-tour-prev")
    page.wait_for_timeout(80)
    check("экскурсия действительно на шаге «Дни в стоке, остаток и запас»",
          pv(page, "window.__PV__.tour.i") == 3, str(pv(page, "window.__PV__.tour.i")))
    hl = pv(page, "Array.from(document.querySelectorAll('#pv-tbody .pv-hl'))"
                  ".map(el => el.getAttribute('data-tour'))")
    check("шаг тура «запас» подсвечивает и dis, и wos одновременно",
          set(hl) == {"dis", "wos"}, str(hl))
    page.click("#pv-tour-next")
    page.wait_for_timeout(80)

    # Явное доказательство (не только через общий список ALLOWED_PATHS выше по
    # файлу): полный проход экскурсии не отправил и не мог отправить POST
    # прогресса обучения — ни через сторожа (он физически бросает исключение
    # раньше, чем запрос уйдёт), ни как-то мимо него.
    lesson_guard = page.evaluate("""() => {
      const passed = Array.from(window.__PV_GUARD__.passed);
      const blocked = window.__PV_GUARD__.blocked.slice();
      return { passed, blocked,
        anyLessonPassed: passed.some(p => p.indexOf('/api/lessons') !== -1
          || p.indexOf('/api/hints') !== -1 || p.indexOf('/api/prefs') !== -1) };
    }""")
    check("экскурсия не пропустила сторожа ни одним запросом к урокам/подсказкам/prefs",
          not lesson_guard["anyLessonPassed"], str(lesson_guard["passed"]))

    page.keyboard.press("Escape")
    page.wait_for_timeout(150)
    check("Escape закрывает экскурсию",
          pv(page, "document.getElementById('pv-tour').hidden"))
    check("фокус возвращается на кнопку, открывшую экскурсию",
          pv(page, "document.activeElement && document.activeElement.id")
          in ("pv-tour-start", "pv-tour-replay"),
          str(pv(page, "document.activeElement && document.activeElement.id")))

    page.click("#pv-tour-replay")
    page.wait_for_timeout(200)
    check("кнопка «?» повторяет экскурсию с первого шага",
          not pv(page, "document.getElementById('pv-tour').hidden")
          and pv(page, "window.__PV__.tour.i") == 0)

    # Детерминированное воспроизведение конкретного бага: openTour() ставит
    # начальный фокус на #pv-tour-title (не входит в список кнопок ловушки),
    # поэтому именно ПЕРВЫЙ Shift+Tab сразу после открытия — а не Shift+Tab
    # после ручного page.focus() на другую кнопку — был тем самым нажатием,
    # которое выпускало фокус из aria-modal диалога наружу.
    check("сразу после открытия фокус стоит на заголовке экскурсии",
          pv(page, "document.activeElement && document.activeElement.id") == "pv-tour-title",
          str(pv(page, "document.activeElement && document.activeElement.id")))
    page.keyboard.press("Shift+Tab")
    page.wait_for_timeout(120)
    after_shift_tab = pv(page, "document.activeElement && document.activeElement.id")
    check("первый же Shift+Tab с заголовка не выпускает фокус из диалога экскурсии",
          str(after_shift_tab).startswith("pv-tour"), str(after_shift_tab))
    last_btn = pv(page, "(() => { const f = document.getElementById('pv-tour')"
                        ".querySelectorAll('button:not([disabled])');"
                        " return f.length ? f[f.length - 1].id : null; })()")
    check("и заворачивает ровно на последнюю кнопку диалога (не куда попало)",
          after_shift_tab == last_btn, f"{after_shift_tab} vs {last_btn}")

    page.keyboard.press("Escape")
    page.wait_for_timeout(150)
    page.keyboard.press("?")
    page.wait_for_timeout(250)
    check("клавиша «?» тоже повторяет экскурсию",
          not pv(page, "document.getElementById('pv-tour').hidden"))
    # Ловушка фокуса: Tab с последней кнопки диалога возвращается на первую.
    page.focus("#pv-tour-close")
    page.keyboard.press("Tab")
    page.wait_for_timeout(120)
    check("Tab не выпускает фокус из диалога экскурсии",
          str(pv(page, "document.activeElement && document.activeElement.id"))
          .startswith("pv-tour"),
          str(pv(page, "document.activeElement && document.activeElement.id")))
    page.keyboard.press("Escape")
    page.wait_for_timeout(150)

    print("\n== §3 Сторож записи: страница физически не может писать ==")
    guard = page.evaluate("""() => {
      const res = {};
      try { window.fetch('/api/settings', {method:'POST', body:'{}'}); res.post = 'НЕ ОТКЛОНЁН'; }
      catch (e) { res.post = 'отклонён'; }
      try { window.fetch('/api/orders'); res.foreign = 'НЕ ОТКЛОНЁН'; }
      catch (e) { res.foreign = 'отклонён'; }
      try { const x = new XMLHttpRequest(); x.open('POST', '/api/discount-overrides');
            res.xhr = 'НЕ ОТКЛОНЁН'; }
      catch (e) { res.xhr = 'отклонён'; }
      try { navigator.sendBeacon('/api/hints/seen', 'x'); res.beacon = 'НЕ ОТКЛОНЁН'; }
      catch (e) { res.beacon = 'отклонён'; }
      try { document.createElement('form').submit(); res.form = 'НЕ ОТКЛОНЁН'; }
      catch (e) { res.form = 'отклонён'; }
      res.blocked = window.__PV_GUARD__.blocked.length;
      res.passed = Array.from(new Set(window.__PV_GUARD__.passed));
      return res;
    }""")
    for k, human in (("post", "POST через fetch"),
                     ("foreign", "GET вне белого списка"),
                     ("xhr", "POST через XMLHttpRequest"),
                     ("beacon", "navigator.sendBeacon"),
                     ("form", "отправка формы")):
        check(f"сторож отклоняет: {human}", guard[k] == "отклонён", guard[k])
    check("попытки записи не потеряны, а записаны сторожем",
          guard["blocked"] >= 5, str(guard["blocked"]))
    check("страница обращалась только к ручкам белого списка",
          set(guard["passed"]) <= ALLOWED_PATHS, str(sorted(guard["passed"])))
    check("и обратилась ко всем пяти", set(guard["passed"]) == ALLOWED_PATHS,
          str(sorted(guard["passed"])))

    print("\n== §3 Реальные сетевые методы: сторонний наблюдатель, не сам сторож ==")
    bad_methods = sorted(set(m for m, _ in real_requests if m not in ("GET", "HEAD")))
    check("за весь проход не зафиксировано ни одного метода, кроме GET/HEAD",
          not bad_methods, str(bad_methods))
    api_methods = sorted(set(m for m, u in real_requests if "/api/" in u))
    check("запросы к /api/ реально уходили методом GET (не только по заявлению сторожа)",
          api_methods == ["GET"], str(api_methods))
    check("Playwright зафиксировал непустой список запросов — наблюдатель не молчал впустую",
          len(real_requests) > 10, str(len(real_requests)))

    check("в консоли браузера нет ошибок страницы", not errors, "; ".join(errors)[:200])

    print("\n== §3 Отпечаток базы после полного прохода ==")
    snap_after = db_snapshot()
    diff = snapshot_diff(snap_before, snap_after)
    check("после полного прохода предпросмотра база байт в байт та же",
          snap_before["digest"] == snap_after["digest"], "; ".join(diff)[:300])
    for t in ("orgs", "warehouses", "connections", "sync_state",
              "sku_discounts", "products", "productions"):
        if t in snap_before["tables"]:
            check(f"таблица {t} не изменилась",
                  snap_before["tables"][t] == snap_after["tables"].get(t),
                  f"{snap_before['tables'][t]} → {snap_after['tables'].get(t)}")
    check("значения /api/turnover после предпросмотра не изменились",
          owner.get("/api/turnover").json() == turnover)
    check("ручные скидки после предпросмотра не изменились",
          owner.get("/api/discount-overrides").json() == overrides)
    check("настройки после предпросмотра не изменились",
          owner.get("/api/settings").json() == settings)

    # Отдельно — перезагрузка и уход со страницы: «закрытие предпросмотра не
    # влияет на настоящую синхронизацию» проверяется строкой sync_state.
    page.reload()
    wait_ready(page)
    page.goto(f"{BASE}/turnover")
    page.wait_for_timeout(800)
    snap_close = db_snapshot()
    check("перезагрузка и закрытие предпросмотра не тронули состояние синка",
          snap_before["tables"].get("sync_state") == snap_close["tables"].get("sync_state"),
          f"{snap_before['tables'].get('sync_state')} → "
          f"{snap_close['tables'].get('sync_state')}")

    ctx.close()
    return {"browser": browser, "turnover": turnover, "snapshot": snap_after}


def part_synthetic_contracts(browser, cookies, template_turnover: dict) -> None:
    """Синтетические контракты corrective-раунда: масштаб процентов,
    возвраты-превысили-продажи, партиция архива по hidden, below_cost.positions.

    Демо-данные проекта (app/demo_seed.py) не гарантируют ни одного сезона с
    sea_returns и ни одного товара с расхождением hidden/archived — искать
    подходящую строку среди шестидесяти демо-товаров ненадёжно (появится
    новый сид — проверка немо тихо перестанет что-либо проверять). Схема
    ответа при этом не выдумывается с нуля: каждая синтетическая строка —
    глубокая копия настоящего товара с точечно переопределёнными полями,
    поэтому все остальные поля остаются валидными как в проде.
    """
    print("\n== §5 Синтетические контракты: проценты, возвраты, архив, below_cost ==")
    real_items = template_turnover.get("items") or []
    assert real_items, "нет ни одной строки в /api/turnover, синтетику не с чего копировать"
    template_item = real_items[0]

    def mk(name, **overrides):
        it = copy.deepcopy(template_item)
        it.update(overrides)
        it["base_name"] = name
        return it

    item_pct = mk("§5 Проценты", discount_fact=0.257, margin_pct=0.4, no_cost=False,
                  hidden=False, archived=False, group="rank", cls="best", low_data=False)
    item_returns = mk("§5 Возвраты", hidden=False, archived=False, group="rank",
                       low_data=False,
                       sea={"winter": 0, "spring": 5000, "summer": None, "autumn": 1000},
                       sea_returns=["winter"])
    item_zero = mk("§5 Честный ноль", hidden=False, archived=False, group="rank",
                    low_data=False,
                    sea={"winter": 0, "spring": 5000, "summer": None, "autumn": 1000},
                    sea_returns=[])
    item_hidden = mk("§5 Архив владельца", hidden=True, archived=False, group="rank",
                      low_data=False)
    item_upstream_archived = mk("§5 Архив МойСклад", hidden=False, archived=True,
                                 group="rank", low_data=False)
    # Канонический _live_items() (app/analytics.py) исключает и hidden, и
    # archived разом; сводка «на что обратить внимание» — агрегат того же
    # рода, что и серверный дашборд, и обязана исключать archived точно так
    # же, а не только hidden (как это осознанно делает разбивка таблицы).
    # Все три поля-триггера сводки выставлены явно, чтобы позиция считалась
    # бы во ВСЕХ трёх корзинах сразу, если бы фильтр молча пропустил её.
    item_archived_weak = mk("§5 Архив слабый", hidden=False, archived=True,
                             group="rank", low_data=True, cls="weak", below_cost=True)

    payload = copy.deepcopy(template_turnover)
    # Порядок здесь не косметика: без исключения archived кандидаты героя
    # (rank && !low_data && !hidden, старая логика) — [pct, returns,
    # archived, zero], их 4, «середина» floor(4/2)=2 — ИМЕННО archived. С
    # исключением archived кандидаты — [pct, returns, zero], их 3, середина
    # — returns. Расположение специально подобрано так, чтобы тест ловил
    # регрессию, а не совпадал по случайному индексу.
    payload["items"] = [item_pct, item_returns, item_upstream_archived, item_zero,
                         item_hidden, item_archived_weak]
    payload["below_cost"] = dict(payload.get("below_cost") or {})
    payload["below_cost"]["positions"] = 3
    # app/analytics.money_totals() исключает no_cost-позиции из gross_margin
    # целиком (показать по ним ноль значило бы соврать про заработанное) и
    # отдаёт no_cost_positions/positions, чтобы экран мог честно сказать,
    # какая часть каталога осталась за пределами суммы — ровно как это уже
    # делает боевая «Оборачиваемость» (templates/turnover.html, moneyNote()).
    payload["money"] = dict(payload.get("money") or {})
    payload["money"]["positions"] = 5
    payload["money"]["no_cost_positions"] = 2
    payload["money"]["gross_margin"] = 123456

    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    ctx.add_cookies([{"name": k, "value": v, "domain": "127.0.0.1", "path": "/"}
                     for k, v in cookies.items()])
    page = ctx.new_page()
    page.route("**/api/turnover", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps(payload)))
    page.goto(f"{BASE}{PREVIEW_URL}")
    wait_ready(page)
    for _ in range(7):
        page.click(".pv-step.is-on [data-go='next']")
    page.wait_for_selector("[data-step='turnover'].is-on")

    print("== §5 Масштаб процентов: доли API умножены на 100, а не показаны как есть ==")
    row_text = page.inner_text("#pv-tbody") or ""
    check("фактическая скидка 0.257 показана как 26 % (округлено), а не 0 %",
          "26 %" in row_text, row_text[:400])

    page.click("#pv-money-btn")
    page.wait_for_timeout(150)
    row_text_money = page.inner_text("#pv-tbody") or ""
    check("маржа 0.4 показана как 40 %, а не 0 %",
          "40 %" in row_text_money, row_text_money[:400])

    print("== §5 Сводка «на что обратить внимание»: исключает archived, как _live_items ==")
    live_no_archived = [it for it in payload["items"] if not it["hidden"] and not it["archived"]]
    exp_weak_s = sum(1 for it in live_no_archived if it["cls"] == "weak")
    exp_below_s = sum(1 for it in live_no_archived if it.get("below_cost"))
    exp_lowdata_s = sum(1 for it in live_no_archived if it.get("low_data"))
    attn_numbers_s = pv(page, "Array.from(document.querySelectorAll('#pv-attn .pv-attn-item b'))"
                              ".map(el => el.textContent.trim())")
    check("сводка исключает archived=true позицию из подсчёта (не только hidden) — "
          "числа сходятся с фильтром !hidden && !archived, а не только !hidden",
          attn_numbers_s[:3] == [str(exp_weak_s), str(exp_below_s), str(exp_lowdata_s)],
          f"{attn_numbers_s[:3]} vs {[exp_weak_s, exp_below_s, exp_lowdata_s]} "
          f"(с archived числа были бы больше)")

    print("== §5 Герой экскурсии не выбирается из upstream archived ==")
    # Порядок массива подобран так (см. комментарий у payload["items"] выше),
    # что «средний» кандидат ПО СТАРОЙ логике (без исключения archived) —
    # именно archived-позиция. Прямая проверка не «не archived» (это прошло
    # бы и по счастливой случайности), а конкретное ожидаемое/неожидаемое
    # имя — тест ловит регрессию, а не просто «что-то другое».
    hero_name = pv(page, "window.__PV__.hero && window.__PV__.hero.base_name")
    check("герой экскурсии — «§5 Возвраты» (середина кандидатов БЕЗ archived), "
          "не «§5 Архив МойСклад» (была бы середина, если бы archived не "
          "исключался)",
          hero_name == "§5 Возвраты", str(hero_name))

    print("== §5 Поиск скрывает героя — тур описывает видимую строку, не скрытую ==")
    # Прежний герой «§5 Возвраты» не подходит под запрос «Честный» — он
    # исчезает из видимой таблицы, но «§5 Честный ноль» (тоже rank,
    # !low_data, !hidden, !archived) остаётся. Тур обязан переключиться на
    # видимую строку: до правки PV.hero оставался «§5 Возвраты», текст тура
    # описывал скрытую строку, а подсветка не находила её в DOM (строка не
    # отрисована) и не подсвечивала ничего.
    page.fill("#pv-search", "Честный")
    page.wait_for_timeout(150)
    check("поиск действительно оставил видимой ровно «§5 Честный ноль»",
          pv(page, "Array.from(document.querySelectorAll("
                   "'#pv-tbody tr:not(.pv-grouprow)')).map(tr => tr.textContent)")
          .__len__() == 1,
          str(pv(page, "document.querySelectorAll('#pv-tbody tr:not(.pv-grouprow)').length")))
    page.click("#pv-tour-start")
    page.wait_for_selector("#pv-tour:not([hidden])")
    hero_after_search = pv(page, "window.__PV__.hero && window.__PV__.hero.base_name")
    check("герой экскурсии переключился на видимую после поиска строку "
          "(«§5 Честный ноль»), не остался на скрытой «§5 Возвраты»",
          hero_after_search == "§5 Честный ноль", str(hero_after_search))
    highlighted = pv(page, "Array.from(document.querySelectorAll("
                          "'#pv-tbody .pv-hl')).map(el => el.getAttribute('data-tour'))")
    check("хотя бы одна ячейка данных подсвечена в диалоге тура (не «ничего»)",
          len(highlighted) > 0, str(highlighted))
    highlighted_row_text = pv(page, "(() => { const hl = document.querySelector"
                                    "('#pv-tbody .pv-hl'); if (!hl) return null;"
                                    " const tr = hl.closest('tr'); return tr ? tr.textContent : null; })()")
    check("подсвеченная ячейка принадлежит именно видимой (отрисованной) "
          "строке-герою, а не скрытой строке вне DOM",
          highlighted_row_text and "§5 Честный ноль" in highlighted_row_text,
          str(highlighted_row_text)[:200])
    page.keyboard.press("Escape")
    page.wait_for_timeout(120)

    print("== §5 Поиск без единой rank-строки — честное «показать не на чем», не старая копия ==")
    page.fill("#pv-search", "zzz-нет-такого-товара-zzz")
    page.wait_for_timeout(150)
    check("поиск без совпадений оставил таблицу пустой",
          pv(page, "document.querySelectorAll('#pv-tbody tr:not(.pv-grouprow)').length") == 0
          or pv(page, "(document.querySelector('#pv-tbody') || {}).textContent || ''")
          .find("§5") == -1)
    page.click("#pv-tour-start")
    page.wait_for_selector("#pv-tour:not([hidden])")
    tour_title_empty = page.text_content("#pv-tour-title") or ""
    check("тур честно говорит «показать не на чем», а не описывает старую "
          "(теперь скрытую) строку",
          "показать не на чем" in tour_title_empty.lower(), tour_title_empty)
    check("PV.hero сброшен в null — не осталась ссылка на скрытую строку",
          pv(page, "window.__PV__.hero") is None)
    page.keyboard.press("Escape")
    page.wait_for_timeout(120)
    page.fill("#pv-search", "")
    page.wait_for_timeout(150)

    print("== §5 Ниже себестоимости: below_cost.positions, а не отсутствующее .count ==")
    loss_text = page.inner_text("#pv-loss") or ""
    check("алерт называет 3 позиции — ровно below_cost.positions из ответа ручки",
          "3" in loss_text and "позици" in loss_text, loss_text[:200])

    print("== §5 Валовая маржа и заморожено: qualification при no_cost_positions>0 ==")
    money_text = page.inner_text("#pv-money") or ""
    check("при no_cost_positions>0 сумма валовой маржи явно оговорена "
          "(только позиции с себестоимостью — 3 из 5, ровно positions-no_cost_positions/positions)",
          "только позиции с себестоимостью" in money_text
          and "3 из 5" in money_text, money_text[:300])
    # app/analytics.money_totals(): stock_cost считается только по with_cost
    # (без no_cost-позиций) — той же оговорки требует и «Заморожено по
    # себестоимости», а не только «Валовая маржа за год». Раздельные
    # проверки по каждой плитке — общий текст #pv-money мог совпасть просто
    # потому, что нужная фраза стоит где-то ещё на экране.
    tiles = pv(page, "Array.from(document.querySelectorAll('#pv-money .pv-tile'))"
                     ".map(t => t.textContent)")
    frozen_tile = next((t for t in tiles if "Заморожено по себестоимости" in t), "")
    sale_tile = next((t for t in tiles if "Если продать" in t), "")
    check("«Заморожено по себестоимости» тоже явно оговорена (та же qualification, "
          "не только у валовой маржи)",
          "только позиции с себестоимостью" in frozen_tile and "3 из 5" in frozen_tile,
          frozen_tile[:200])
    check("«Если продать» (stock_sale, считается по ВСЕМ позициям) НЕ несёт "
          "эту оговорку — она была бы неверна для этой суммы",
          "только позиции с себестоимостью" not in sale_tile, sale_tile[:200])

    page.click("#pv-money-btn")
    page.wait_for_timeout(150)

    print("== §5 Сезон, зажатый возвратами: не тот же ноль, что «продаж не было» ==")
    check("строка с sea_returns=['winter'] помечена особо, не как обычный ноль",
          page.evaluate("() => !!document.querySelector('#pv-tbody .pv-seareturns')"))
    seareturns_title = page.evaluate(
        "() => { const el = document.querySelector('#pv-tbody .pv-seareturns');"
        " return el ? el.getAttribute('title') : null; }")
    check("подсказка объясняет: возвраты превысили продажи, это не «продаж не было»",
          bool(seareturns_title) and "озвраты" in seareturns_title, str(seareturns_title))
    zero_has_mark = page.evaluate(
        "() => { const rows = Array.from(document.querySelectorAll('#pv-tbody tr'));"
        " const row = rows.find(tr => tr.textContent.includes('§5 Честный ноль'));"
        " return row ? !!row.querySelector('.pv-seareturns') : null; }")
    check("а соседняя строка с честным нулём (sea_returns не содержит сезон) "
          "пометки не получает — иначе метка обесценилась бы, отмечая всё подряд",
          zero_has_mark is False, str(zero_has_mark))

    print("== §5 Архив партиционируется по hidden, а не по служебному archived ==")
    group_of = ("() => { const rows = Array.from(document.querySelectorAll('#pv-tbody tr'));"
                " const i = rows.findIndex(tr => tr.textContent.includes('%s'));"
                " if (i < 0) return null;"
                " for (let j = i; j >= 0; j--) {"
                "   if (rows[j].classList.contains('pv-grouprow')) return rows[j].textContent; }"
                " return null; }")
    g_upstream = page.evaluate(group_of % "§5 Архив МойСклад")
    check("archived=true, hidden=false остаётся в живом списке (не в «Архиве»)",
          g_upstream != "Архив", str(g_upstream))
    g_hidden = page.evaluate(group_of % "§5 Архив владельца")
    check("hidden=true, archived=false уходит в «Архив» независимо от archived",
          g_hidden == "Архив", str(g_hidden))

    ctx.close()


# ── §4 Адаптив, цели нажатия, клавиатура, prefers-reduced-motion ─────────────

TOUCH_JS = """() => {
  const bad = [];
  const sel = 'button, a[href], input[type=range], [role="radio"]';
  document.querySelectorAll(sel).forEach(el => {
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return;      // элемент скрытого шага
    if (r.height < 44 || r.width < 44) {
      bad.push((el.id || el.className || el.tagName) + ' ' +
               Math.round(r.width) + 'x' + Math.round(r.height));
    }
  });
  return bad;
}"""

OVERFLOW_JS = ("() => document.documentElement.scrollWidth - "
               "document.documentElement.clientWidth")


def part_loader_idle_partial(browser, cookies) -> None:
    """idle + частично загруженная история (400/730) — идле это не «нет связи».

    Изолированный сценарий в своём контексте (не встроен в общий проход
    part_browser): реалистичная синтетика — синхронизация прямо сейчас не
    идёт (state=idle), но 400 из 730 дней истории уже загружены раньше и
    массив stages[] пуст (сервер не отдаёт по нему детали, когда он не
    активен). До правки idle трактовался как «подключения нет» и владелец с
    реальными частично загруженными данными видел лживое «Подключение · 0 из
    4 готово».
    """
    print("\n== §5 idle + частичная история (400/730): idle — это НЕ «нет связи» ==")
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    ctx.add_cookies([{"name": k, "value": v, "domain": "127.0.0.1", "path": "/"}
                     for k, v in cookies.items()])
    page = ctx.new_page()
    page.route("**/api/sync/progress", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body='{"state":"idle","mode":"initial","phase":"","progress_pct":0,'
             '"detail":"","error":"","error_cause":"","coverage_days":400,'
             '"history_days":730,"window_days":30,"months":[],"stages":[],'
             '"eta_sec":null,"started_at":null,"finished_at":null}'))
    page.goto(f"{BASE}{PREVIEW_URL}")
    wait_ready(page)
    for _ in range(6):
        page.click(".pv-step.is-on [data-go='next']")
        page.wait_for_timeout(80)
    page.wait_for_selector("[data-step='loader'].is-on")

    # idle+данные доказывают только ФАКТ получения — не «связь жива сейчас».
    # «получены ранее» — честный смысл: что-то есть, но синк сейчас не идёт.
    conn_right = pv(page, "(() => { const li = document.querySelector("
                          "'#pv-stages .pv-stage[data-key=\"connection\"]');"
                          " return li ? li.querySelector('.pv-stage-right').textContent.trim()"
                          " : null; })()")
    check("idle + покрытие 400/730: «Данные источника» читается как "
          "«получены ранее» (факт получения, не заявление о живом соединении)",
          conn_right == "получены ранее", str(conn_right))
    board_idle_txt = page.inner_text("#pv-board") or ""
    check("сырое слово idle нигде не показано на доске",
          "idle" not in board_idle_txt, board_idle_txt[:300])

    # idle — это «синк сейчас не идёт»: заголовок обязан говорить именно так,
    # без «Загружаем»/спиннера/ETA — это были бы придуманное движение там,
    # где на деле пауза.
    mainstatus_idle = page.inner_text("#pv-mainstatus") or ""
    check("idle + частичная история: заголовок статуса без слова «Загружаем» "
          "и явно говорит, что загрузка сейчас не идёт",
          "Загружаем" not in mainstatus_idle
          and "загрузка сейчас не идёт" in mainstatus_idle.lower(), mainstatus_idle)
    check("и всё равно называет числа покрытия (400 из 730)",
          "400" in mainstatus_idle and "730" in mainstatus_idle, mainstatus_idle)
    check("и явно не говорит про подключение/данные источника как про текущий этап",
          "данные источника" not in mainstatus_idle.lower(), mainstatus_idle)
    spin_visible = pv(page, "!!document.querySelector('#pv-mainstatus .pv-spin')")
    check("idle: спиннер выключен (нет активного процесса, который он бы изображал)",
          not spin_visible)
    close_note_idle = page.inner_text("#pv-close-note") or ""
    check("idle: нижняя строка НЕ обещает, что загрузка продолжится сама "
          "(движения, которое можно было бы продолжить, сейчас нет)",
          "продолжится сама" not in close_note_idle, close_note_idle)

    # Статический раньше текст-заголовок #pv-loader-lead обещал «появятся
    # скоро» и «продолжит грузиться в фоне» БЕЗУСЛОВНО — на этом самом экране
    # одновременно с #pv-mainstatus, честно говорящим «Загрузка сейчас не
    # идёт». idle обязан видеть нейтральный заголовок без обещания
    # продолжения/скорого появления.
    loader_lead_idle = page.inner_text("#pv-loader-lead") or ""
    check("idle: вводный текст доски НЕ обещает «появятся скоро»/продолжение "
          "в фоне — на этом же экране правда в том, что загрузка не идёт",
          "скоро" not in loader_lead_idle and "фоне" not in loader_lead_idle
          and "продолжит" not in loader_lead_idle, loader_lead_idle)

    # Матрица построена из /api/turnover, который в этом сценарии реально
    # прочитался (замокан только /api/sync/progress) — карточка обязана
    # согласованно сказать «готово», а не молчать пустым nofield-плейсхолдером.
    matrix_right_idle = pv(page, "(() => { const li = document.querySelector("
                                "'#pv-stages .pv-stage[data-key=\"matrix\"]');"
                                " return li ? li.querySelector('.pv-stage-right').textContent.trim()"
                                " : null; })()")
    check("турновер реально прочитался в этом сценарии — карточка «Матрица» "
          "согласованно говорит «готово», не молчит",
          matrix_right_idle == "готово", str(matrix_right_idle))

    # Все пять карточек разом: даже когда stages[] пуст (реалистичный idle-
    # ответ без детализации по этапам), НИ ОДНА карточка не рисует голое
    # тире/idle/«нет поля» — «Товары и остатки», «Продажи и возвраты» и
    # «История продаж» тоже обязаны говорить «ожидает», а не «—».
    all_rights = pv(page, "Array.from(document.querySelectorAll("
                          "'#pv-stages .pv-stage')).map(li => ({"
                          " key: li.getAttribute('data-key'),"
                          " right: li.querySelector('.pv-stage-right').textContent.trim() }))")
    bad = [r for r in all_rights if r["right"] in ("—", "-", "idle", "нет поля")]
    check("ни одна из пяти карточек не показывает —/idle/«нет поля», "
          "включая «Товары и остатки»/«Продажи и возвраты»/«История продаж»",
          not bad, str(all_rights))
    todo_human = [r for r in all_rights if r["key"] in ("catalog", "month", "history")]
    check("«Товары и остатки», «Продажи и возвраты», «История продаж» без "
          "детализации stages[] говорят человеческое «ожидает»",
          all(r["right"] == "ожидает" for r in todo_human), str(todo_human))

    ctx.close()

    # Контрольная ветка: то же самое частичное покрытие (400/730), но синк
    # РЕАЛЬНО идёт (state=running). Здесь и «Загружаем»/спиннер/ETA, и
    # «появятся скоро»/«продолжит в фоне», и «продолжится сама» — обязаны
    # вернуться, потому что теперь они правда. Без этой ветки правка пункта 1
    # могла бы тайно превратить заголовки в вечно нейтральные и потерять
    # честное «идёт прямо сейчас».
    ctx3 = browser.new_context(viewport={"width": 1440, "height": 900})
    ctx3.add_cookies([{"name": k, "value": v, "domain": "127.0.0.1", "path": "/"}
                      for k, v in cookies.items()])
    page3 = ctx3.new_page()
    page3.route("**/api/sync/progress", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body='{"state":"running","mode":"initial","phase":"","progress_pct":55,'
             '"detail":"","error":"","error_cause":"","coverage_days":400,'
             '"history_days":730,"window_days":30,"months":[],'
             '"stages":[{"key":"products","title":"Товары и цены","state":"done",'
             '"seconds":3,"counts":{"products_total":40}},'
             '{"key":"today","title":"Остатки на сегодня","state":"done",'
             '"seconds":2,"counts":{"warehouses":3}},'
             '{"key":"month","title":"Продажи","state":"done","seconds":4,"counts":{}},'
             '{"key":"history","title":"История","state":"running","seconds":null}],'
             '"eta_sec":120,"started_at":null,"finished_at":null}'))
    page3.goto(f"{BASE}{PREVIEW_URL}")
    wait_ready(page3)
    for _ in range(6):
        page3.click(".pv-step.is-on [data-go='next']")
        page3.wait_for_timeout(80)
    page3.wait_for_selector("[data-step='loader'].is-on")

    mainstatus_running = page3.inner_text("#pv-mainstatus") or ""
    check("контроль (running): заголовок статуса честно говорит «Загружаем» "
          "с числами покрытия",
          "Загружаем" in mainstatus_running and "400" in mainstatus_running
          and "730" in mainstatus_running, mainstatus_running)
    spin_running = pv(page3, "!!document.querySelector('#pv-mainstatus .pv-spin')")
    check("контроль (running): спиннер включён — процесс правда идёт",
          spin_running)
    close_note_running = page3.inner_text("#pv-close-note") or ""
    check("контроль (running): нижняя строка обещает продолжение — это правда",
          "продолжится сама" in close_note_running, close_note_running)
    loader_lead_running = page3.inner_text("#pv-loader-lead") or ""
    check("контроль (running): вводный текст доски обещает «появятся скоро» "
          "и продолжение в фоне — на этом экране это правда",
          "скоро" in loader_lead_running and "фоне" in loader_lead_running,
          loader_lead_running)
    conn_right_running = pv(page3, "(() => { const li = document.querySelector("
                                   "'#pv-stages .pv-stage[data-key=\"connection\"]');"
                                   " return li ? li.querySelector('.pv-stage-right')"
                                   ".textContent.trim() : null; })()")
    check("контроль (running): «Данные источника» говорит «получаем» "
          "(процесс правда идёт сейчас)",
          conn_right_running == "получаем", str(conn_right_running))
    ctx3.close()


def part_loader_matrix_truth(browser, cookies) -> None:
    """«Матрица готова» — обещание про экран «Оборачиваемость», не только про синк.

    Синтетика: все четыре реальных этапа синхронизации помечены завершёнными
    (state=done), но GET /api/turnover отдаёт 409 — сценарий, который
    реально случается (конфликт состояния синка, сеть, любая другая ошибка
    из errMessage()). До правки mainStatusText() смотрел только на
    SEM_STAGES и говорил «Матрица готова», хотя следующий экран честно
    писал «не удалось прочитать данные» — прямое противоречие в один шаг.
    """
    print("\n== §5 «Матрица готова» — только когда матрица реально прочиталась ==")
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    ctx.add_cookies([{"name": k, "value": v, "domain": "127.0.0.1", "path": "/"}
                     for k, v in cookies.items()])
    page = ctx.new_page()
    page.route("**/api/sync/progress", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body='{"state":"done","mode":"initial","phase":"","progress_pct":100,'
             '"detail":"","error":"","error_cause":"","coverage_days":730,'
             '"history_days":730,"window_days":30,"months":[],'
             '"stages":[{"key":"products","title":"Товары и цены","state":"done",'
             '"seconds":3,"counts":{"products_total":40}},'
             '{"key":"today","title":"Остатки на сегодня","state":"done",'
             '"seconds":2,"counts":{"warehouses":3}},'
             '{"key":"month","title":"Продажи","state":"done","seconds":4,"counts":{}},'
             '{"key":"history","title":"История","state":"done","seconds":9,"counts":{}}],'
             '"eta_sec":null,"started_at":null,"finished_at":null}'))
    page.route("**/api/turnover", lambda route: route.fulfill(
        status=409, content_type="application/json", body='{"detail":"conflict"}'))
    page.goto(f"{BASE}{PREVIEW_URL}")
    wait_ready(page)
    for _ in range(6):
        page.click(".pv-step.is-on [data-go='next']")
        page.wait_for_timeout(80)
    page.wait_for_selector("[data-step='loader'].is-on")

    mainstatus = page.inner_text("#pv-mainstatus") or ""
    check("синк завершён, но турновер не прочитался: НЕ говорим «Матрица готова»",
          "Матрица готова" not in mainstatus, mainstatus)
    check("вместо этого честно — синхронизация завершена, матрица недоступна",
          "инхронизация завершена" in mainstatus and "недоступна" in mainstatus,
          mainstatus)

    ctx.close()

    # Контрольная ветка: то же самое (все стадии done), но турновер читается
    # нормально — «Матрица готова» обязана вернуться. Без этой ветки правка
    # пункта 2 могла бы тайно сломать нормальный путь «всё хорошо».
    ctx2 = browser.new_context(viewport={"width": 1440, "height": 900})
    ctx2.add_cookies([{"name": k, "value": v, "domain": "127.0.0.1", "path": "/"}
                      for k, v in cookies.items()])
    page2 = ctx2.new_page()
    page2.route("**/api/sync/progress", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body='{"state":"done","mode":"initial","phase":"","progress_pct":100,'
             '"detail":"","error":"","error_cause":"","coverage_days":730,'
             '"history_days":730,"window_days":30,"months":[],'
             '"stages":[{"key":"products","title":"Товары и цены","state":"done",'
             '"seconds":3,"counts":{"products_total":40}},'
             '{"key":"today","title":"Остатки на сегодня","state":"done",'
             '"seconds":2,"counts":{"warehouses":3}},'
             '{"key":"month","title":"Продажи","state":"done","seconds":4,"counts":{}},'
             '{"key":"history","title":"История","state":"done","seconds":9,"counts":{}}],'
             '"eta_sec":null,"started_at":null,"finished_at":null}'))
    page2.goto(f"{BASE}{PREVIEW_URL}")
    wait_ready(page2)
    for _ in range(6):
        page2.click(".pv-step.is-on [data-go='next']")
        page2.wait_for_timeout(80)
    page2.wait_for_selector("[data-step='loader'].is-on")
    mainstatus2 = page2.inner_text("#pv-mainstatus") or ""
    check("контроль: синк завершён И турновер читается нормально — «Матрица готова» на месте",
          "Матрица готова" in mainstatus2, mainstatus2)
    ctx2.close()


def part_status_empty_stages_and_modal(browser, cookies) -> None:
    """Пять P1/P2 из независимого exact-HEAD ревью: пустой stages[] и два модальных слоя разом.

    1) state=done + пустой stages[] (например, инкрементальный синк не
       отдаёт детализацию по этапам) не должен выглядеть как «1 из 4
       готово» со спиннером — верхнеуровневый done сам по себе достаточное
       доказательство завершения.
    2) state=idle с coverage_days=0 и с coverage_days>=history_days (оба —
       при пустом stages[]) не должны падать в «Собираем данные» со
       спиннером — idle никогда не обещает активную загрузку, ни при каком
       покрытии.
    5) Попап объяснений и диалог экскурсии — оба aria-modal="true": открытие
       тура при открытом попапе обязано сначала закрыть попап.
    """
    def mock_progress(page, state, coverage_days, history_days, extra=""):
        page.route("**/api/sync/progress", lambda route: route.fulfill(
            status=200, content_type="application/json",
            body='{"state":"' + state + '","mode":"incremental","phase":"",'
                 '"progress_pct":0,"detail":"","error":"","error_cause":"",'
                 '"coverage_days":' + str(coverage_days) + ',' +
                 '"history_days":' + str(history_days) + ',"window_days":30,'
                 '"months":[],"stages":[],"eta_sec":null,"started_at":null,'
                 '"finished_at":null}'))

    def open_loader(pw_browser):
        ctx = pw_browser.new_context(viewport={"width": 1440, "height": 900})
        ctx.add_cookies([{"name": k, "value": v, "domain": "127.0.0.1", "path": "/"}
                         for k, v in cookies.items()])
        page = ctx.new_page()
        return ctx, page

    def goto_loader(page):
        page.goto(f"{BASE}{PREVIEW_URL}")
        wait_ready(page)
        for _ in range(6):
            page.click(".pv-step.is-on [data-go='next']")
            page.wait_for_timeout(80)
        page.wait_for_selector("[data-step='loader'].is-on")

    print("\n== §5 state=done + пустой stages[]: не «1 из 4», без спиннера ==")
    ctx1, page1 = open_loader(browser)
    mock_progress(page1, "done", 730, 730)
    goto_loader(page1)
    mainstatus_done_empty = page1.inner_text("#pv-mainstatus") or ""
    check("done + пустой stages[]: заголовок НЕ говорит «Собираем данные»",
          "Собираем данные" not in mainstatus_done_empty, mainstatus_done_empty)
    check("done + пустой stages[]: заголовок НЕ говорит «N из 4 готово» "
          "(псевдо-pending при на деле завершённом синке)",
          "из 4 готово" not in mainstatus_done_empty, mainstatus_done_empty)
    spin_done_empty = pv(page1, "!!document.querySelector('#pv-mainstatus .pv-spin')")
    check("done + пустой stages[]: спиннер выключен — верхнеуровневый done "
          "уже достаточное доказательство завершения",
          not spin_done_empty)
    check("done + пустой stages[]: вместо этого — правда про матрицу "
          "(готова или недоступна, но не «в процессе»)",
          "Матрица готова" in mainstatus_done_empty
          or "Матрица недоступна" in mainstatus_done_empty,
          mainstatus_done_empty)
    # Экран не может противоречить сам себе: если главный статус уже сказал
    # «готово», ни одна карточка этапа НИЖЕ не имеет права молча стоять на
    # «ожидает» — до этой правки liveStageState() не знал про верхнеуровневый
    # p.state==="done" и рисовал catalog/month/history как todo, хотя
    # заголовок над ними уже был «Матрица готова».
    rows_done_empty = pv(page1, "Array.from(document.querySelectorAll("
                                "'#pv-stages .pv-stage')).map(li => ({"
                                " key: li.getAttribute('data-key'),"
                                " state: li.getAttribute('data-state'),"
                                " right: li.querySelector('.pv-stage-right').textContent.trim() }))")
    bound_rows = [r for r in rows_done_empty if r["key"] != "matrix"]
    todo_rows = [r for r in bound_rows if r["state"] == "todo" or r["right"] == "ожидает"]
    check("done + пустой stages[]: ни одна из связанных карточек "
          "(данные источника/товары/продажи/история) не осталась «ожидает» "
          "— один и тот же экран не может звучать разными правдами",
          not todo_rows, str(rows_done_empty))
    hist_row_full = next((r for r in rows_done_empty if r["key"] == "history"), None)
    check("история при полном покрытии (730 из 730) в этом сценарии честно "
          "называет оба числа, а не абстрактное «готово»",
          hist_row_full and hist_row_full["right"] == "730 из 730 дн.",
          str(hist_row_full))
    ctx1.close()

    print("\n== §5 state=done + пустой stages[] + ЧАСТИЧНОЕ покрытие: карточки не лгут про полноту ==")
    ctx1b, page1b = open_loader(browser)
    mock_progress(page1b, "done", 400, 730)
    goto_loader(page1b)
    hist_row_partial = pv(page1b, "(() => { const li = document.querySelector("
                                  "'#pv-stages .pv-stage[data-key=\"history\"]');"
                                  " return li ? { state: li.getAttribute('data-state'),"
                                  " right: li.querySelector('.pv-stage-right').textContent.trim() }"
                                  " : null; })()")
    check("частичное покрытие (400 из 730) при done+пустых stages[]: карточка "
          "истории называет РЕАЛЬНЫЕ числа (400 из 730), не выдуманную "
          "полноту 730/730 и не голое «готово»",
          hist_row_partial == {"state": "done", "right": "400 из 730 дн."},
          str(hist_row_partial))
    other_rows_partial = pv(page1b, "Array.from(document.querySelectorAll("
                                    "'#pv-stages .pv-stage')).map(li => ({"
                                    " key: li.getAttribute('data-key'),"
                                    " state: li.getAttribute('data-state'),"
                                    " right: li.querySelector('.pv-stage-right').textContent.trim() }))")
    todo_rows_partial = [r for r in other_rows_partial
                          if r["key"] != "matrix" and (r["state"] == "todo" or r["right"] == "ожидает")]
    check("частичное покрытие: ни одна связанная карточка всё равно не "
          "осталась «ожидает»",
          not todo_rows_partial, str(other_rows_partial))
    ctx1b.close()

    print("\n== §5 state=idle + coverage=0 + пустой stages[]: без спиннера/«Собираем» ==")
    ctx2, page2 = open_loader(browser)
    mock_progress(page2, "idle", 0, 730)
    goto_loader(page2)
    mainstatus_idle_zero = page2.inner_text("#pv-mainstatus") or ""
    check("idle + coverage=0: заголовок НЕ говорит «Собираем данные»/«Загружаем»",
          "Собираем данные" not in mainstatus_idle_zero
          and "Загружаем" not in mainstatus_idle_zero, mainstatus_idle_zero)
    check("idle + coverage=0: явно сказано, что загрузка сейчас не идёт",
          "не идёт" in mainstatus_idle_zero, mainstatus_idle_zero)
    spin_idle_zero = pv(page2, "!!document.querySelector('#pv-mainstatus .pv-spin')")
    check("idle + coverage=0: спиннер выключен", not spin_idle_zero)
    ctx2.close()

    print("\n== §5 state=idle + coverage=history_days (полная) + пустой stages[]: без спиннера/«Собираем» ==")
    ctx3, page3 = open_loader(browser)
    mock_progress(page3, "idle", 730, 730)
    goto_loader(page3)
    mainstatus_idle_full = page3.inner_text("#pv-mainstatus") or ""
    check("idle + полное покрытие: заголовок НЕ говорит «Собираем данные»/«Загружаем»",
          "Собираем данные" not in mainstatus_idle_full
          and "Загружаем" not in mainstatus_idle_full, mainstatus_idle_full)
    check("idle + полное покрытие: явно сказано, что загрузка сейчас не идёт",
          "не идёт" in mainstatus_idle_full, mainstatus_idle_full)
    check("idle + полное покрытие: числа покрытия названы (730)",
          "730" in mainstatus_idle_full, mainstatus_idle_full)
    spin_idle_full = pv(page3, "!!document.querySelector('#pv-mainstatus .pv-spin')")
    check("idle + полное покрытие: спиннер выключен", not spin_idle_full)
    ctx3.close()

    print("\n== §5 Тур закрывает попап объяснений — не два aria-modal разом ==")
    ctx5, page5 = open_loader(browser)
    goto_loader(page5)
    page5.click(".pv-step.is-on [data-go='next']")
    page5.wait_for_selector("[data-step='turnover'].is-on")
    page5.click("#pv-help-turnover")
    page5.wait_for_timeout(150)
    check("попап объяснений открыт (подготовка сценария)",
          not pv(page5, "document.getElementById('pv-help-pop').hidden"))
    page5.click("#pv-tour-start")
    page5.wait_for_selector("#pv-tour:not([hidden])")
    check("открытие тура при открытом попапе объяснений сначала закрывает попап "
          "(не два aria-modal одновременно)",
          pv(page5, "document.getElementById('pv-help-pop').hidden"))
    check("попап объяснений больше не в состоянии «открыт» (PV.help.open=false)",
          not pv(page5, "window.__PV__.help.open"))
    check("фокус — в диалоге экскурсии (заголовок тура), не в попапе объяснений",
          pv(page5, "document.activeElement && document.activeElement.id") == "pv-tour-title")
    page5.keyboard.press("Escape")
    page5.wait_for_timeout(150)
    check("Escape после закрытия тура возвращает фокус на opener ТУРА "
          "(#pv-tour-start), а не на кнопку объяснений",
          pv(page5, "document.activeElement && document.activeElement.id") == "pv-tour-start")
    ctx5.close()


def part_responsive(browser, cookies) -> None:
    print("\n== §4 Адаптив: 1440x900, 1024x768, 390x844 ==")
    SHOTS_DIR.mkdir(exist_ok=True)
    for w, h, tag in ((1440, 900, "1440x900"), (1024, 768, "1024x768"),
                      (390, 844, "390x844")):
        ctx = browser.new_context(viewport={"width": w, "height": h})
        ctx.add_cookies([{"name": k, "value": v, "domain": "127.0.0.1", "path": "/"}
                         for k, v in cookies.items()])
        page = ctx.new_page()
        page.goto(f"{BASE}{PREVIEW_URL}")
        wait_ready(page)
        worst, worst_step = 0, ""
        badge_fail_steps = []
        for i, key in enumerate(STEP_KEYS):
            if i:
                page.click(".pv-step.is-on [data-go='next']")
                page.wait_for_selector(f"[data-step='{key}'].is-on")
                page.wait_for_timeout(120)
            over = page.evaluate(OVERFLOW_JS)
            if over > worst:
                worst, worst_step = over, key
            if tag == "390x844":
                # На каждом шаге бейдж «ПРЕДПРОСМОТР» обязан оставаться видимым
                # и целиком читаемым текстом — не сжиматься до точки. Проверка
                # ловит и box (ширина/высота), и реальный видимый текст: узкая
                # коробка с обрезанным текстом дала бы правильную ширину и
                # неправильный (пустой) видимый текст, а ловушка должна ловить
                # оба случая.
                badge = page.evaluate(
                    "() => { const el = document.getElementById('pv-badge');"
                    " if (!el) return null; const r = el.getBoundingClientRect();"
                    " return { w: r.width, h: r.height, text: el.innerText.trim(),"
                    " display: getComputedStyle(el).display,"
                    " scrollW: el.scrollWidth, clientW: el.clientWidth }; }")
                # Явная проверка на КАЖДОМ шаге (не только welcome/turnover):
                # innerText содержит слово буквально, бокс ненулевой и достаточно
                # широкий, и scrollWidth не превышает clientWidth — то есть текст
                # внутри бейджа не обрезан переполнением контейнера. Прежняя
                # проверка ловила пустой/сжатый текст, но не внутреннее
                # переполнение конкретно на шаге «Загрузка».
                ok = (badge and badge["display"] != "none" and badge["w"] >= 100
                      and badge["h"] >= 20 and badge["text"] == "ПРЕДПРОСМОТР"
                      and badge["scrollW"] <= badge["clientW"] + 1)
                if not ok:
                    badge_fail_steps.append(f"{key}: {badge}")
        check(f"{tag}: горизонтальной прокрутки страницы нет ни на одном из "
              f"{len(STEP_KEYS)} шагов",
              worst <= 1, f"перелив {worst}px на шаге «{worst_step}»")
        if tag == "390x844":
            check("390px: бейдж «ПРЕДПРОСМОТР» виден и не сжат ни на одном из "
                  f"{len(STEP_KEYS)} шагов",
                  not badge_fail_steps, "; ".join(badge_fail_steps)[:300])
        page.evaluate("() => window.scrollTo(0, 0)")
        page.wait_for_timeout(150)
        page.screenshot(path=str(SHOTS_DIR / f"preview_{tag}_turnover.png"),
                        full_page=False)
        if tag == "390x844":
            inner = page.evaluate(
                "() => { const w = document.getElementById('pv-tablewrap');"
                " return [w.scrollWidth, w.clientWidth,"
                " document.documentElement.scrollWidth,"
                " document.documentElement.clientWidth]; }")
            check("390px: широкая таблица прокручивается внутри своего контейнера",
                  inner[0] > inner[1], f"scroll {inner[0]} > client {inner[1]}")
            check("390px: и при этом страница целиком не разъезжается",
                  inner[2] - inner[3] <= 1, f"{inner[2]} vs {inner[3]}")
            bad = page.evaluate(TOUCH_JS)
            check("390px: все цели нажатия не меньше 44px", not bad, "; ".join(bad)[:200])
            check("390px: таблица шире контейнера — видна подсказка прокрутки",
                  page.evaluate("() => document.getElementById('pv-tablewrap')"
                                ".classList.contains('is-scrollable')"))
            hint_visible = page.evaluate(
                "() => getComputedStyle(document.getElementById('pv-scrollhint')).display")
            check("390px: подсказка прокрутки реально показана (не display:none)",
                  hint_visible != "none", hint_visible)
            check("390px: у поля поиска есть подписанный <label>",
                  page.evaluate("() => !!document.querySelector('label[for=\"pv-search\"]')"))
            cls_badges = page.evaluate(
                "() => document.querySelectorAll('#pv-tbody .pv-clsbadge').length")
            check("390px: класс строки продублирован текстом, не только цветом фона",
                  cls_badges > 0, str(cls_badges))
            check("390px: короткая строка над таблицей видна сразу, без раскрытия деталей",
                  bool((page.inner_text("#pv-tfoot-short") or "").strip()))
            check("390px: полное объяснение архива (в т.ч. «только информация») "
                  "свёрнуто по умолчанию, но доказуемо",
                  "только" in (page.text_content("#pv-tfoot") or "")
                  and "информац" in (page.text_content("#pv-tfoot") or ""))
            check("390px: сводка «на что обратить внимание» стоит ДО плотной таблицы "
                  "(компактная карточка над скроллящейся таблицей)",
                  page.evaluate("() => { const a = document.getElementById('pv-attn');"
                                " const t = document.getElementById('pv-tablewrap');"
                                " return !!(a && t && (a.compareDocumentPosition(t) "
                                "& Node.DOCUMENT_POSITION_FOLLOWING)); }"))
            page.click("#pv-help-toggle")
            page.wait_for_timeout(200)
            sheet_box = page.evaluate(
                "() => { const r = document.getElementById('pv-help-pop')"
                ".getBoundingClientRect(); return [r.left, r.width]; }")
            check("390px: попап объяснений превращается в донный лист во всю ширину",
                  sheet_box[0] == 0 and sheet_box[1] >= 380, str(sheet_box))
            page.keyboard.press("Escape")
            page.wait_for_timeout(150)
        # Экскурсия на каждом размере: снимок для приёмки. Диалог открывается,
        # renderTour() запускает scrollIntoView() к подсвеченной ячейке —
        # ждём, пока прокрутка реально остановится, прежде чем снимать (см.
        # wait_scroll_stable): иначе кадр ловит середину плавной анимации.
        page.click("#pv-tour-start")
        page.wait_for_selector("#pv-tour:not([hidden])")
        wait_scroll_stable(page)
        tour_box = page.evaluate(
            "() => { const r = document.getElementById('pv-tour').getBoundingClientRect();"
            " return { top: r.top, bottom: r.bottom, h: window.innerHeight }; }")
        check(f"{tag}: диалог экскурсии кадрирован стабильно (не обрезан по высоте) "
              "в момент съёмки",
              tour_box["h"] > 0 and tour_box["bottom"] <= tour_box["h"] + 1
              and tour_box["top"] >= -1, str(tour_box))
        page.screenshot(path=str(SHOTS_DIR / f"preview_{tag}_tour.png"))
        page.keyboard.press("Escape")
        # Первый и седьмой экраны — тоже в артефакты.
        page.goto(f"{BASE}{PREVIEW_URL}")
        wait_ready(page)
        page.screenshot(path=str(SHOTS_DIR / f"preview_{tag}_welcome.png"))
        for _ in range(6):
            page.click(".pv-step.is-on [data-go='next']")
            page.wait_for_timeout(90)
        page.wait_for_selector("[data-step='loader'].is-on")
        page.evaluate("() => window.scrollTo(0, 0)")
        page.wait_for_timeout(400)
        if tag == "390x844":
            # Над сгибом экрана должно быть видно, ЧТО происходит — не пустое
            # состояние и не тире. Порог 640px взят из окна: главный статус
            # обязан попадать в первый экран без прокрутки на телефоне.
            status_box = page.evaluate(
                "() => { const el = document.getElementById('pv-mainstatus');"
                " if (!el) return null; const r = el.getBoundingClientRect();"
                " return { top: r.top, bottom: r.bottom, text: el.innerText.trim() }; }")
            check("390px: главный статус загрузки виден над сгибом экрана",
                  status_box and status_box["top"] < 640, str(status_box))
            check("390px: и это не пустая строка/тире/idle",
                  status_box and status_box["text"] and status_box["text"] not in ("—", ""),
                  str(status_box))
            header_layout = page.evaluate(
                "() => { const b = document.getElementById('pv-badge').getBoundingClientRect();"
                " const s = document.getElementById('pv-stepcount').getBoundingClientRect();"
                " const a = document.querySelector('.pv-top-actions').getBoundingClientRect();"
                " return { badgeTop: b.top, stepTop: s.top, actionsTop: a.top }; }")
            # Бейдж — пилюля с вертикальным паддингом, счётчик шага — голый текст:
            # у них разная высота строки, поэтому верхние края отличаются даже в
            # одной визуальной строке. Порог взят не «около нуля», а «явно меньше
            # высоты строки шапки» (~30px) — это отличает «в одной строке» от
            # «строкой ниже» (там разница — вся высота actions-блока, ~35px+).
            check("390px: бейдж и счётчик шага — в одной строке шапки",
                  abs(header_layout["badgeTop"] - header_layout["stepTop"]) < 20,
                  str(header_layout))
            check("390px: действия («Объяснения»/«Выйти») — отдельной строкой ниже, "
                  "не втиснуты в первую",
                  header_layout["actionsTop"] > header_layout["badgeTop"] + 20,
                  str(header_layout))

            # Проверка ИМЕННО в момент съёмки скриншота (не раньше): читаем
            # box/стиль отдельного текстового узла .pv-badge-text на шаге
            # «Загрузка» — том самом шаге, где ранее сообщался визуальный
            # дефект (голая точка вместо слова). DOM innerText сам по себе не
            # ловит «текст присутствует, но нулевой ширины/прозрачный» — здесь
            # проверяются раздельно текст, box и вычисленные визуальные
            # свойства прямо перед page.screenshot().
            loader_badge = page.evaluate(
                "() => { const el = document.querySelector('#pv-badge .pv-badge-text');"
                " if (!el) return null; const r = el.getBoundingClientRect();"
                " const cs = getComputedStyle(el);"
                " return { text: el.textContent.trim(), w: r.width, h: r.height,"
                " color: cs.color, opacity: cs.opacity, visibility: cs.visibility,"
                " display: cs.display }; }")
            check("390px, шаг «Загрузка», в момент съёмки скриншота: текст бейджа "
                  "дословно «ПРЕДПРОСМОТР»",
                  loader_badge and loader_badge["text"] == "ПРЕДПРОСМОТР", str(loader_badge))
            check("390px, шаг «Загрузка»: текстовый узел бейджа имеет ненулевой "
                  "видимый box (не 0×0)",
                  loader_badge and loader_badge["w"] > 0 and loader_badge["h"] > 0,
                  str(loader_badge))
            check("390px, шаг «Загрузка»: текст бейджа не прозрачный и не visibility:hidden",
                  loader_badge and loader_badge["opacity"] == "1"
                  and loader_badge["visibility"] == "visible"
                  and loader_badge["display"] != "none",
                  str(loader_badge))
            check("390px, шаг «Загрузка»: у текста задан непустой цвет (не "
                  "transparent/rgba(0,0,0,0))",
                  loader_badge and loader_badge["color"]
                  and "0, 0, 0, 0)" not in loader_badge["color"].replace(" ", ", ")
                  and loader_badge["color"] != "transparent",
                  str(loader_badge))
        page.screenshot(path=str(SHOTS_DIR / f"preview_{tag}_loader.png"))
        ctx.close()
    check("снимки всех трёх размеров сохранены",
          len(list(SHOTS_DIR.glob("preview_*.png"))) >= 12,
          str(len(list(SHOTS_DIR.glob("preview_*.png")))))

    print("\n== §4 prefers-reduced-motion и клавиатура ==")
    ctx = browser.new_context(viewport={"width": 1440, "height": 900},
                              reduced_motion="reduce")
    ctx.add_cookies([{"name": k, "value": v, "domain": "127.0.0.1", "path": "/"}
                     for k, v in cookies.items()])
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{BASE}{PREVIEW_URL}")
    wait_ready(page)
    check("при reduced-motion страница знает об этом",
          page.evaluate("() => window.matchMedia('(prefers-reduced-motion: reduce)')"
                        ".matches"))
    check("при reduced-motion анимация появления шага выключена",
          page.evaluate("() => getComputedStyle(document.querySelector"
                        "('.pv-step.is-on')).animationName") in ("none", ""),
          str(page.evaluate("() => getComputedStyle(document.querySelector"
                            "('.pv-step.is-on')).animationName")))
    page.click("[data-step='welcome'] [data-go='next']")
    page.wait_for_selector("[data-step='warehouses'].is-on")
    check("при reduced-motion шаги переключаются как обычно",
          page.evaluate("() => window.__PV__.step") == 1)
    # Клавиатура: с первой позиции Tab доходит до кнопки «Начать» и Enter работает.
    page.goto(f"{BASE}{PREVIEW_URL}")
    wait_ready(page)
    first_focusable = page.evaluate(
        "() => { const el = document.querySelector("
        "'a[href], button, input, [tabindex]:not([tabindex=\"-1\"])');"
        " return el ? el.className : null; }")
    check("ссылка «к содержимому» стоит первой в порядке документа",
          "pv-skip" in str(first_focusable), str(first_focusable))
    page.focus(".pv-skip")
    check("ссылка «к содержимому» получает фокус с клавиатуры",
          "pv-skip" in str(page.evaluate(
              "() => document.activeElement && document.activeElement.className")))
    page.keyboard.press("Enter")
    page.wait_for_timeout(150)
    check("и уводит к содержимому предпросмотра",
          page.evaluate("() => location.hash") == "#pv-main",
          str(page.evaluate("() => location.hash")))
    page.focus("[data-step='welcome'] [data-go='next']")
    page.keyboard.press("Enter")
    page.wait_for_selector("[data-step='warehouses'].is-on")
    check("шаг переключается с клавиатуры", page.evaluate("() => window.__PV__.step") == 1)
    page.keyboard.press("Escape")
    page.wait_for_timeout(120)
    check("Escape без открытого диалога уводит фокус на выход, а не ломает страницу",
          page.evaluate("() => document.activeElement && document.activeElement.id")
          == "pv-exit",
          str(page.evaluate("() => document.activeElement && document.activeElement.id")))

    # Кольцо фокуса заметно, но не «взрывает» макет: было 3px + отступ 2px
    # (визуально ~5px хало), стало 2px + отступ 1px. Проверяем именно то, что
    # уменьшилось, а не просто «кольцо есть» — иначе регрессия к старому
    # большому кольцу тоже прошла бы эту проверку.
    ring = page.evaluate(
        "() => { const el = document.getElementById('pv-exit'); el.focus();"
        " const cs = getComputedStyle(el);"
        " return { width: cs.outlineWidth, offset: cs.outlineOffset,"
        " style: cs.outlineStyle }; }")
    check("кольцо фокуса тоньше прежнего (<=2px, было 3px)",
          ring["width"] in ("2px",), str(ring))
    check("и не отодвинуто далеко от элемента (<=1px, был отступ 2px)",
          ring["offset"] in ("1px",), str(ring))
    check("кольцо всё ещё видимо (не none/hidden) — фокус клавиатуры не потерян",
          ring["style"] not in ("none", "hidden"), str(ring))

    check("в консоли браузера нет ошибок и при reduced-motion",
          not errors, "; ".join(errors)[:200])
    ctx.close()


def run() -> int:
    from playwright.sync_api import sync_playwright

    owner = client()
    r = owner.post("/register", data={"name": "Владелец", "email": "preview-owner@test.io",
                                      "password": "secret123", "org_name": "Бренд-Превью"})
    check("владелец зарегистрирован", r.status_code in (200, 302, 303), str(r.status_code))
    check("синтетические демо-данные загружены",
          owner.post("/api/connect/demo").status_code == 200)

    org_id = sqlite3.connect(DB_PATH).execute("SELECT id FROM orgs ORDER BY id").fetchone()[0]
    add_member(org_id, "preview-member@test.io")
    member = client()
    member.post("/login", data={"email": "preview-member@test.io", "password": "secret123"})
    check("участник вошёл", member.get("/api/settings").json().get("role") == "member",
          str(member.get("/api/settings").json().get("role")))

    part_access(owner, member)

    turnover_before = owner.get("/api/turnover").json()
    check("в демо-данных есть строки для предпросмотра",
          len(turnover_before.get("items", [])) > 0,
          str(len(turnover_before.get("items", []))))

    snap_before = part_instrument(owner, turnover_before)

    with sync_playwright() as pw:
        try:
            probe = pw.chromium.launch()
            probe.close()
        except Exception as exc:  # noqa: BLE001 — важна причина, а не тип
            # Playwright есть, браузера нет. Это НЕ пропуск: набор обязателен,
            # а окружение не готово — и сказать об этом надо отчётом, а не
            # трассировкой, которую раннер прочитает как «нет отчёта».
            check("Chromium запускается", False,
                  str(exc).strip().splitlines()[0][:200])
            print(f"\nИТОГО: {len(PASS)} OK, {len(FAIL)} FAIL")
            return 1
        out = part_browser(pw, {k: v for k, v in owner.cookies.items()}, owner, snap_before)
        try:
            part_synthetic_contracts(out["browser"], {k: v for k, v in owner.cookies.items()},
                                      out["turnover"])
            part_loader_idle_partial(out["browser"], {k: v for k, v in owner.cookies.items()})
            part_loader_matrix_truth(out["browser"], {k: v for k, v in owner.cookies.items()})
            part_status_empty_stages_and_modal(out["browser"],
                                                {k: v for k, v in owner.cookies.items()})
            part_responsive(out["browser"], {k: v for k, v in owner.cookies.items()})
        finally:
            out["browser"].close()

    print(f"\nИТОГО: {len(PASS)} OK, {len(FAIL)} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
