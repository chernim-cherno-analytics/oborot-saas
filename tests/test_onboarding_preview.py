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
import hashlib
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
# Восемь смысловых этапов доски загрузки в объявленном порядке.
SEMANTIC_STAGES = ["connection", "warehouses", "products", "today", "month",
                   "history", "turnover_calc", "matrix"]
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

    focused = pv(page, "document.activeElement && document.activeElement.id")
    page.click("[data-step='welcome'] [data-go='next']")
    page.wait_for_selector("[data-step='warehouses'].is-on")
    focused = pv(page, "document.activeElement && document.activeElement.id")
    check("после перехода фокус уходит на заголовок нового шага",
          focused == "pv-h-warehouses", str(focused))

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
    page.click("[data-step='groups'] [data-go='next']")
    page.wait_for_selector("[data-step='loader'].is-on")

    keys = pv(page, "Array.from(document.querySelectorAll('#pv-stages .pv-stage'))"
                    ".map(s => s.getAttribute('data-key'))")
    check("восемь смысловых этапов в объявленном порядке",
          keys == SEMANTIC_STAGES, str(keys))
    # inner_text, а не text_content: лента «ДЕМОНСТРАЦИЯ» в живом режиме
    # спрятана стилем, и text_content вернул бы её как показанную.
    board = page.inner_text("#pv-board") or ""
    for field in ("coverage_days", "history_days", "progress_pct", "state", "eta_sec"):
        check(f"живая доска называет поле контракта {field}", field in board)
    nofield = pv(page, "Array.from(document.querySelectorAll("
                       "'#pv-stages .pv-stage[data-state=\"nofield\"]'))"
                       ".map(s => s.getAttribute('data-key'))")
    check("этапы без поля в контракте честно помечены, а не нарисованы",
          nofield == ["turnover_calc", "matrix"], str(nofield))
    check("для них прямо сказано, что поля нет и прогресс не рисуется",
          "отдельного поля для этого этапа нет" in board)
    check("в живом режиме на доске нет слова ДЕМО",
          "ДЕМО" not in board and "ДЕМОНСТРАЦИЯ" not in board)

    page.click("#pv-demo-btn")
    page.wait_for_timeout(400)
    check("демонстрация переключает режим доски",
          pv(page, "document.getElementById('pv-board').getAttribute('data-mode')") == "demo")
    demo_txt = page.inner_text("#pv-board") or ""
    check("демонстрация подписана словом ДЕМОНСТРАЦИЯ", "ДЕМОНСТРАЦИЯ" in demo_txt)
    check("сказано, что числа синтетические", "синтетические" in demo_txt)
    tags = pv(page, "document.querySelectorAll('#pv-stages .pv-demotag').length")
    check("каждый этап демонстрации помечен отдельно",
          tags == len(SEMANTIC_STAGES), str(tags))
    check("живые поля контракта на время демонстрации скрыты целиком",
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
    page.click("#pv-money-btn")
    page.wait_for_timeout(200)
    check("тумблер открывает денежный слой",
          pv(page, "document.getElementById('pv-money').classList.contains('is-on')"))
    check("и столбец маржи вместе с ним",
          pv(page, "getComputedStyle(document.querySelector('#pv-thead th.pv-hidecol'))"
                   ".display") != "none")
    page.click("#pv-money-btn")
    page.wait_for_timeout(200)
    check("настоящая «Оборачиваемость» денежный слой по-прежнему показывает",
          "money-bar" in owner.get("/turnover").text)

    print("\n== §3 Экскурсия по строке ==")
    page.click("#pv-tour-start")
    page.wait_for_selector("#pv-tour:not([hidden])")
    steps_tour = walk_tour(page)
    check("экскурсия состоит из десяти шагов", len(steps_tour) == 10, str(len(steps_tour)))
    joined = " | ".join(s["title"] for s in steps_tour)
    for need in ("Оборачиваемость", "денежная отдача", "номинальная цена", "Сезон",
                 "Дней в стоке", "Остаток", "Фактическая скидка", "Ручная скидка",
                 "Автоматическая скидка", "Архив"):
        check(f"экскурсия проходит пункт «{need}»", need in joined, joined[:220])

    def tour_text(i):
        return steps_tour[i]["text"] if i < len(steps_tour) else ""

    color_txt = tour_text(1)
    check("цвет объяснён как денежная отдача",
          "денежную отдачу" in color_txt, color_txt[:140])
    check("и прямо сказано, что это не рентабельность и не прибыльность",
          "не рентабельность и не " in color_txt and "прибыльность" in color_txt,
          color_txt[:200])

    sea_txt = tour_text(3)
    check("сезонный ноль объяснён только для покрытого сезона без продаж",
          "покрыт загруженной историей" in sea_txt and "продаж в нём не было" in sea_txt,
          sea_txt[:200])
    check("прочерк объяснён как «данных нет», со ссылкой на D-34",
          "данных нет" in sea_txt.lower() and "D-34" in sea_txt, sea_txt[:200])

    stock_txt = tour_text(5)
    check("шаг про запас честно объясняет пустую колонку «Не хватает»",
          "в предпросмотре пуст сознательно" in stock_txt
          and "пересчитывать её здесь" in stock_txt, stock_txt[:220])

    auto_txt = tour_text(8)
    check("автоматическая скидка описана числами существующего правила",
          digits_in(auto_txt, rule["rule"]["top_turnover"])
          and digits_in(auto_txt, rule["rule"]["weak_pct"]), auto_txt[:220])
    check("и прямо сказано, что предпросмотр правило не применяет",
          "не применяет" in auto_txt, auto_txt[:220])
    check("экскурсия ничего не применила: ручные скидки те же",
          owner.get("/api/discount-overrides").json() == overrides)

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
        for i, key in enumerate(STEP_KEYS):
            if i:
                page.click(".pv-step.is-on [data-go='next']")
                page.wait_for_selector(f"[data-step='{key}'].is-on")
                page.wait_for_timeout(120)
            over = page.evaluate(OVERFLOW_JS)
            if over > worst:
                worst, worst_step = over, key
        check(f"{tag}: горизонтальной прокрутки страницы нет ни на одном из "
              f"{len(STEP_KEYS)} шагов",
              worst <= 1, f"перелив {worst}px на шаге «{worst_step}»")
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
        # Экскурсия на каждом размере: снимок для приёмки.
        page.click("#pv-tour-start")
        page.wait_for_selector("#pv-tour:not([hidden])")
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
            part_responsive(out["browser"], {k: v for k, v in owner.cookies.items()})
        finally:
            out["browser"].close()

    print(f"\nИТОГО: {len(PASS)} OK, {len(FAIL)} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
