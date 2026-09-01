# -*- coding: utf-8 -*-
"""SUPPLY-2: поведение страницы «Поставки» в НАСТОЯЩЕМ браузере.

Зачем отдельный набор. Три из восьми замечаний ревью PR #47
(REVIEW_REJECT на HEAD `08142a5`) живут целиком в браузере, и проверкой
«в шаблоне есть такая строка» ни одно из них не ловится:

  1) при `writes_blocked` (гейт подписки включён, организация в readonly)
     владелец не должен видеть рабочую форму и кнопку обновления: сервер
     ГАРАНТИРОВАННО отвечает на этот POST кодом 402, и предлагать действие,
     в котором приложение уже отказало, — обман. Снимок при этом остаётся на
     экране: readonly закрывает запись, а не чтение своих данных;
  2) «Показать ещё» обязано быть сериализовано: один запрос в полёте, кнопка
     выключена на время запроса, позиция фиксируется ТОЛЬКО после успеха,
     отказ повторяет ту же страницу, а поздний ответ прежнего фильтра в новое
     состояние не дописывается;
  3) после УСПЕШНОГО обновления фильтр по листу сбрасывается: имена листов
     могли смениться, и старый фильтр даёт настоящий 400. На неудаче фильтр и
     строки остаются, а кнопка восстанавливается даже если следующий GET упал.

ЧЕМ ЗДЕСЬ ОТВЕЧАЕТ СЕРВЕР. Настоящим сервером, а не заглушкой: снимок
кладётся в носителя тем же кодом, который его читает (`supply_sheets`
собирает `counts`), и `GET /api/supply/sheets` отвечает по-настоящему —
включая честный 400 на лист, которого в снимке больше нет. Подменяются ровно
две вещи и обе названы: POST обновления (иначе понадобился бы живой Google) и
задержка ответа (иначе гонку двойного клика воспроизвести нечем — ответ
успевает прийти раньше второго нажатия, и проверка доказывала бы отсутствие
гонки, у которой не было возможности случиться).

Запуск из корня репозитория:  python tests/test_supply_ui.py

Нужен Chromium под playwright: `pip install -r requirements-dev.lock` и
`python -m playwright install chromium`.
"""
import json
import os
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "test_supply_ui.db"
APP_PORT = int(os.environ.get("OBOROT_TEST_PORT", "8816"))

os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["SCHEDULER_ENABLED"] = "0"
os.environ["OBOROT_SUBSCRIPTION_GATE"] = "0"

if DB_PATH.exists():
    DB_PATH.unlink()

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from app import supply_sheets as ss  # noqa: E402
from app.main import app as oborot_app  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
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
        deadline = time.time() + 20
        while time.time() < deadline:
            if self.server.started:
                return
            time.sleep(0.05)
        raise RuntimeError(f"сервер на порту {self.config.port} не поднялся")

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=10)


SHEET_A, SHEET_B = "Осень 26", "НГ 26/27"
#: Имя листа для проверок приватности. Уникальное намеренно: «Осень 26» стоит
#: в исходнике страницы как placeholder поля формы, а `text_content("body")`
#: захватывает и текст блока `<script>` — на таком имени проверка «его нет на
#: экране» краснела бы по чужой причине и ничего не доказывала.
SENTINEL_SHEET = "Щ-сентинель-Ui7Kq9Zt"
SHEET_C, SHEET_D = "Весна 27", "Лето 27"
SPREADSHEET_ID = "1AbCdEf_ghijklmnop-QRSTUV0123456789wxyz"
#: Таблица НЕУДАВШЕЙСЯ попытки. Отличается от успешной намеренно: иначе
#: «в поле стоит адрес попытки» и «в поле стоит адрес снимка» неразличимы.
ATTEMPT_SPREADSHEET_ID = "2ZyXwVu_ponmlkjihg-FEDCBA9876543210zyxw"

#: Запросы страницы к API. `?` в glob playwright значим, поэтому адреса
#: сопоставляются регулярным выражением, а не шаблоном со звёздочками.
GET_RE = re.compile(r"/api/supply/sheets\?")
REFRESH_RE = re.compile(r"/api/supply/sheets/refresh")

#: Задержка ответа задаётся В СТРАНИЦЕ, а не в обработчике playwright:
#: `time.sleep` внутри route-обработчика синхронного API останавливает весь
#: цикл событий, и второй клик просто не был бы обработан — гонка исчезла бы
#: вместе с возможностью её увидеть. Здесь же тормозится ровно тот запрос,
#: который назван в `__supDelayMatch`, а журнал вызовов ведётся честно по
#: каждой попытке — включая ту, которую страница обязана НЕ сделать.
#:
#: `__supDelayLimit` тормозит только ПЕРВЫЕ N совпавших запросов (`-1` — все).
#: Без него нельзя воспроизвести гонку, где медленный и быстрый запросы идут
#: по ОДНОМУ адресу: догрузка снятого вида и догрузка нового вида отличаются
#: не URL, а тем, кто их начал, — и «тормозим всё, что совпало» затормозило бы
#: обоих, спрятав ровно ту разницу, которую проверка обязана увидеть.
DELAY_SCRIPT = """
(() => {
  window.__supCalls = [];
  window.__supDelayMs = 0;
  window.__supDelayMatch = "";
  window.__supDelayLimit = -1;
  const real = window.fetch;
  window.fetch = function (url, init) {
    const u = String((url && url.url) || url);
    const self = this, args = arguments;
    if (u.indexOf("/api/supply/sheets") !== -1) window.__supCalls.push(u);
    const wanted = window.__supDelayMatch;
    let delay = (wanted && u.indexOf(wanted) !== -1) ? (window.__supDelayMs || 0) : 0;
    if (delay && window.__supDelayLimit >= 0) {
      if (window.__supDelayLimit > 0) { window.__supDelayLimit -= 1; }
      else { delay = 0; }
    }
    if (!delay) return real.apply(self, args);
    return new Promise((resolve, reject) => {
      setTimeout(() => { real.apply(self, args).then(resolve, reject); }, delay);
    });
  };
})();
"""


def make_row(index: int, sheet: str, *, invalid: bool = False) -> dict:
    """Строка снимка ровно в той форме, которую выпускает парсер."""
    sizes = {"XS": 1, "S": 1, "M": 1, "L": 1, "XL": None if invalid else 1}
    sizes_raw = {"XS": "1", "S": "1", "M": "1", "L": "1",
                 "XL": "Кроим по заданию" if invalid else "1"}
    known = sum(v for v in sizes.values() if v is not None)
    return {
        "sheet_name": sheet, "source_row": index, "anchor_row": index,
        "is_blank": False,
        "article_raw": f"A{index}", "name_raw": f"Позиция {index}",
        "article": f"A{index}", "name": f"Позиция {index}",
        "color_raw": "Чёрный", "qty_meters_raw": "", "sketch_raw": "",
        "sizes": sizes, "sizes_raw": sizes_raw, "size_sum": known,
        "source_total_raw": str(known), "source_total": known,
        "comments_raw": ["", "", ""], "source_status_raw": "",
        "price_raw": "", "components_raw": "", "production_raw": "",
        "unknown_raw": {}, "issues": ["invalid_quantity"] if invalid else [],
    }


def write_snapshot(sheets, rows) -> None:
    """Положить снимок в носителя организации.

    Счётчики считает САМ слой (`ss.build_counts`), а не тест: иначе проверка
    экрана опиралась бы на числа, выдуманные рядом с проверкой, и доказывала
    бы согласие теста с самим собой.
    """
    envelope = {
        "schema_version": ss.ENVELOPE_SCHEMA_VERSION,
        "parser_version": ss.PARSER_VERSION,
        "spreadsheet_id": SPREADSHEET_ID,
        "sheet_names": list(sheets),
        "content_sha256": "0" * 64,
        "last_attempt_at": "2026-08-31T12:00:00+00:00",
        "last_success_at": "2026-08-31T12:00:00+00:00",
        "fetched_at": "2026-08-31T12:00:00+00:00",
        "last_error": "",
        "last_attempt_source": {"spreadsheet_id": SPREADSHEET_ID,
                                "sheet_names": list(sheets)},
        "schema": {}, "counts": ss.build_counts(rows, list(sheets)), "rows": rows,
    }
    con = sqlite3.connect(DB_PATH)
    try:
        row = con.execute("SELECT id, config_json FROM connections"
                          " ORDER BY id LIMIT 1").fetchone()
        cfg = json.loads(row[1] or "{}")
        cfg[ss.ENVELOPE_KEY] = envelope
        con.execute("UPDATE connections SET config_json = ? WHERE id = ?",
                    (json.dumps(cfg, ensure_ascii=False), row[0]))
        con.commit()
    finally:
        con.close()


def write_failed_attempt(sheets, detailed: str, public: str,
                         code: str = "access") -> None:
    """Состояние «удачного чтения ещё не было, последняя попытка отказала».

    `configured` в нём false: `spreadsheet_id` пуст, строк нет, успеха не было
    ни одного. Ровно на этом состоянии страница и советует человеку, что делать
    дальше, — и совет обязан подходить его роли.

    Код причины и его отпечаток считает САМ слой (`ss._public_binding`), а не
    тест: иначе проверка экрана опиралась бы на связывание, выдуманное рядом с
    проверкой, и доказывала бы согласие теста с самим собой.
    """
    attempt_at = "2026-08-31T12:00:00+00:00"
    source = {"spreadsheet_id": SPREADSHEET_ID, "sheet_names": list(sheets)}
    envelope = {
        "schema_version": ss.ENVELOPE_SCHEMA_VERSION,
        "parser_version": ss.PARSER_VERSION,
        "spreadsheet_id": "", "sheet_names": [],
        "content_sha256": "",
        "last_attempt_at": attempt_at,
        "last_success_at": None, "fetched_at": None,
        "last_error": detailed, "last_error_public": public,
        "last_error_public_code": code,
        "last_error_public_binding": ss._public_binding(
            code, detailed, attempt_at, source),
        "last_attempt_source": source,
        "schema": {}, "counts": ss.build_counts([], []), "rows": [],
    }
    con = sqlite3.connect(DB_PATH)
    try:
        row = con.execute("SELECT id, config_json FROM connections"
                          " ORDER BY id LIMIT 1").fetchone()
        cfg = json.loads(row[1] or "{}")
        cfg[ss.ENVELOPE_KEY] = envelope
        con.execute("UPDATE connections SET config_json = ? WHERE id = ?",
                    (json.dumps(cfg, ensure_ascii=False), row[0]))
        con.commit()
    finally:
        con.close()


def write_snapshot_with_failed_attempt(sheets, rows, attempt_sheets,
                                       attempt_id: str = ATTEMPT_SPREADSHEET_ID,
                                       source_ok: bool = True) -> None:
    """Самое живое состояние владельца: снимок ЕСТЬ, а последняя попытка упала.

    Это состояние, в котором владелец меняет ссылку или имена листов у уже
    настроенного источника и промахивается. На сервере оно выглядит так:
    успешные поля снимка (`spreadsheet_id`, `sheet_names`, `rows`, `counts`,
    `last_success_at`) остаются прежними, а `last_error` и
    `last_attempt_source` описывают НОВУЮ, неудачную попытку — ровно то, что
    делает `_record_failure()`.

    `source_ok=False` даёт испорченный источник попытки: читатель обязан
    безопасно откатиться к успешным значениям, а не показать мусор.
    """
    attempt_at = "2026-08-31T15:00:00+00:00"
    detailed = f"лист «{attempt_sheets[0]}»: источник ответил 500"
    source = ({"spreadsheet_id": attempt_id, "sheet_names": list(attempt_sheets)}
              if source_ok else "испорчено рукой")
    envelope = {
        "schema_version": ss.ENVELOPE_SCHEMA_VERSION,
        "parser_version": ss.PARSER_VERSION,
        # Успешный снимок — прежний, его неудача не трогает.
        "spreadsheet_id": SPREADSHEET_ID,
        "sheet_names": list(sheets),
        "content_sha256": "0" * 64,
        "last_success_at": "2026-08-31T12:00:00+00:00",
        "fetched_at": "2026-08-31T12:00:00+00:00",
        # Поля ПОПЫТКИ — новые.
        "last_attempt_at": attempt_at,
        "last_error": detailed,
        "last_error_public": "лист источника: источник ответил 500",
        "last_error_public_code": "unavailable",
        "last_error_public_binding": ss._public_binding(
            "unavailable", detailed, attempt_at, source),
        "last_attempt_source": source,
        "schema": {}, "counts": ss.build_counts(rows, list(sheets)), "rows": rows,
    }
    con = sqlite3.connect(DB_PATH)
    try:
        row = con.execute("SELECT id, config_json FROM connections"
                          " ORDER BY id LIMIT 1").fetchone()
        cfg = json.loads(row[1] or "{}")
        cfg[ss.ENVELOPE_KEY] = envelope
        con.execute("UPDATE connections SET config_json = ? WHERE id = ?",
                    (json.dumps(cfg, ensure_ascii=False), row[0]))
        con.commit()
    finally:
        con.close()


def drop_carriers():
    """Убрать ВСЕ основные связи организации: `carrier_present` станет false.

    Возвращает (колонки, строки), чтобы состояние можно было вернуть на место
    целиком: следующие сценарии набора рассчитывают на живого носителя.
    Колонки читаются из схемы, а не выписываются здесь — иначе набор ломался бы
    от любой будущей колонки, к нему отношения не имеющей.
    """
    con = sqlite3.connect(DB_PATH)
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info(connections)")]
        rows = con.execute(
            f"SELECT {', '.join(cols)} FROM connections ORDER BY id").fetchall()
        con.execute("DELETE FROM connections")
        con.commit()
        return cols, rows
    finally:
        con.close()


def restore_carriers(saved) -> None:
    cols, rows = saved
    placeholders = ", ".join("?" * len(cols))
    con = sqlite3.connect(DB_PATH)
    try:
        for r in rows:
            con.execute(f"INSERT INTO connections ({', '.join(cols)})"
                        f" VALUES ({placeholders})", r)
        con.commit()
    finally:
        con.close()


def add_member(email: str) -> None:
    """Участник организации: приглашений в UI нет, заводим строкой в БД."""
    import bcrypt

    con = sqlite3.connect(DB_PATH)
    try:
        org_id = con.execute("SELECT id FROM orgs ORDER BY id LIMIT 1").fetchone()[0]
        pw = bcrypt.hashpw(b"secret123", bcrypt.gensalt()).decode()
        cur = con.execute(
            "INSERT INTO users (email, pw_hash, name, created_at)"
            " VALUES (?,?,?,datetime('now'))", (email, pw, email.split("@")[0]))
        con.execute("INSERT INTO memberships (user_id, org_id, role)"
                    " VALUES (?,?,'member')", (cur.lastrowid, org_id))
        con.commit()
    finally:
        con.close()


def set_trial(days: int) -> None:
    con = sqlite3.connect(DB_PATH)
    try:
        con.execute("UPDATE orgs SET trial_ends_at = datetime('now', ?),"
                    " paid_until = NULL", (f"{days} day",))
        con.commit()
    finally:
        con.close()


def carrier_blob() -> str:
    con = sqlite3.connect(DB_PATH)
    try:
        return con.execute("SELECT COALESCE(GROUP_CONCAT(config_json), '')"
                           " FROM connections").fetchone()[0] or ""
    finally:
        con.close()


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        # Код 77 И причина — оба сигнала сразу (D-42): набор, не открывший ни
        # одной страницы, не должен выглядеть в CI зелёным.
        print("ПРОПУЩЕНО: playwright не установлен — поставьте "
              "requirements-dev.lock и выполните `python -m playwright "
              "install chromium`")
        return 77
    srv = ServerThread(oborot_app, APP_PORT)
    srv.start()
    try:
        return run()
    except Exception as exc:  # noqa: BLE001 — важен отчёт, а не тип
        # Набор обязан отчитаться, а не умереть трассировкой. Мёртвая страница
        # роняет сценарий на первом же клике по невидимой кнопке, и без этой
        # ветки прогон не печатал бы ни `ИТОГО`, ни причины: раннер засчитал бы
        # его как «нет отчёта» (D-42) — верно по итогу, но нечитаемо человеком.
        check("сценарий страницы дошёл до конца без исключения", False,
              f"{type(exc).__name__}: {str(exc).strip().splitlines()[0][:200]}")
        print(f"\nИТОГО: {len(PASS)} OK, {len(FAIL)} FAIL")
        for name in FAIL:
            print(f"  FAIL {name}")
        return 1
    finally:
        srv.stop()
        os.environ["OBOROT_SUBSCRIPTION_GATE"] = "0"
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(DB_PATH) + suffix)
            if p.exists():
                p.unlink()


def run() -> int:  # noqa: C901 — сценарный набор: шагов много, ветвлений мало
    from playwright.sync_api import sync_playwright

    base = f"http://127.0.0.1:{APP_PORT}"
    c = httpx.Client(headers={"X-Oborot-CSRF": "1"}, base_url=base, timeout=120.0)
    reg = c.post("/register", data={"name": "Владелец", "email": "supply-ui@test.io",
                                    "password": "secret123", "org_name": "Бренд-UI"})
    check("владелец зарегистрирован", reg.status_code in (200, 302, 303),
          str(reg.status_code))
    check("демо-данные загружены", c.post("/api/connect/demo").status_code == 200)

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as exc:  # noqa: BLE001 — важно имя причины, а не тип
            check("Chromium запускается", False,
                  str(exc).strip().splitlines()[0][:200])
            print(f"\nИТОГО: {len(PASS)} OK, {len(FAIL)} FAIL")
            return 1
        ctx = browser.new_context(viewport={"width": 1400, "height": 900})
        ctx.add_cookies([{"name": k, "value": v, "domain": "127.0.0.1", "path": "/"}
                         for k, v in c.cookies.items()])
        errors: list[str] = []
        page = ctx.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.add_init_script(DELAY_SCRIPT)

        def calls() -> list:
            return page.evaluate("() => window.__supCalls || []")

        def get_calls() -> list:
            return [u for u in calls() if "/refresh" not in u]

        def total_line() -> str:
            return page.text_content("#sup-total") or ""

        def row_count() -> int:
            return page.evaluate(
                "() => document.querySelectorAll('#sup-rows tr').length")

        # ── 1. Гейт подписки: read-only вместо формы, но снимок на месте ────
        print("\n== Подписка readonly: владельцу не предлагают то, в чём откажут ==")
        big = [make_row(i + 1, SHEET_A) for i in range(120)]
        write_snapshot([SHEET_A, SHEET_B], big)

        set_trial(30)
        page.goto(f"{base}/supply")
        page.wait_for_timeout(1200)
        # Девятый дефект прошлого корректива: страница была МЕРТВА в браузере
        # (`api is not defined` при разборе блока `scripts`, потому что
        # `static/app.js` подключён с `defer`). Проверка стоит здесь и явно:
        # ни одной ошибки в консоли и первый же элемент, который рисует JS.
        check("страница ожила: скрипт дождался DOMContentLoaded",
              not errors and page.evaluate(
                  "() => document.querySelectorAll('#sup-rows tr').length") > 0,
              str(errors)[:200])
        check("гейт выключен: кнопка обновления на месте",
              page.evaluate("() => !!document.getElementById('sup-refresh')") is True)
        check("и поле ссылки тоже",
              page.evaluate("() => !!document.getElementById('sup-url')") is True)
        check("снимок показан настоящим сервером",
              total_line() == "показано 50 из 120", total_line())

        # Организация уводится в readonly ровно так, как это происходит в
        # жизни: триал истёк, счёт не оплачен. Отдельной ручки «сделай
        # readonly» нет и не должно быть — состояние считает
        # `subscription.subscription_state`.
        set_trial(-40)
        os.environ["OBOROT_SUBSCRIPTION_GATE"] = "1"
        page.goto(f"{base}/supply")
        page.wait_for_timeout(1200)
        check("в readonly кнопки обновления нет вовсе",
              page.evaluate("() => !!document.getElementById('sup-refresh')") is False)
        check("и поля ссылки тоже нет — нечего заполнять впустую",
              page.evaluate("() => !!document.getElementById('sup-url')") is False)
        form_text = page.text_content("#sup-form-wrap") or ""
        check("сказано словами, ПОЧЕМУ обновление недоступно",
              "приостановлено" in form_text and "подписк" in form_text.lower(),
              form_text[:140])
        check("и названа дорога назад — страница «Тарифы»",
              page.evaluate(
                  "() => [...document.querySelectorAll('#sup-form-wrap a')]"
                  ".some(a => a.getAttribute('href') === '/plans')") is True,
              form_text[:140])
        check("просмотр снимка при этом НЕ закрыт",
              total_line() == "показано 50 из 120", total_line())
        check("и строки на экране остались", row_count() == 50, str(row_count()))

        before_blob = carrier_blob()
        direct = c.post("/api/supply/sheets/refresh",
                        json={"spreadsheet_url":
                              f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit",
                              "sheet_names": [SHEET_C, SHEET_D]})
        check("прямой POST в обход страницы — 402, а не 200",
              direct.status_code == 402, f"{direct.status_code} {direct.text[:110]}")
        check("и отказ случился ДО любой записи: носитель не тронут",
              carrier_blob() == before_blob)
        check("чтение при этом осталось открытым",
              c.get("/api/supply/sheets").status_code == 200)

        os.environ["OBOROT_SUBSCRIPTION_GATE"] = "0"
        set_trial(30)

        # ── 2. Сериализованная догрузка ────────────────────────────────────
        print("\n== «Показать ещё»: один запрос в полёте, offset после успеха ==")
        page.goto(f"{base}/supply")
        page.wait_for_timeout(1200)
        check("гейт снят — форма владельца вернулась",
              page.evaluate("() => !!document.getElementById('sup-refresh')") is True)
        check("первая страница показана целиком",
              total_line() == "показано 50 из 120", total_line())

        page.evaluate("() => { window.__supCalls = []; window.__supDelayMs = 700;"
                      " window.__supDelayMatch = 'offset=50'; }")
        page.click("#sup-more")
        page.wait_for_timeout(150)
        check("кнопка выключена на время запроса",
              page.evaluate("() => document.getElementById('sup-more').disabled") is True)
        # Второй клик — программный: настоящая кнопка уже выключена, и обычный
        # click просто не дошёл бы до обработчика. Здесь проверяется именно
        # замок в коде страницы, а не то, что браузер не пускает клик по
        # disabled-кнопке: без замка второй вызов ушёл бы вторым запросом.
        page.evaluate("() => document.getElementById('sup-more')"
                      ".dispatchEvent(new MouseEvent('click', {bubbles: true}))")
        page.wait_for_timeout(1600)
        check("двойной клик дал ровно ОДИН запрос догрузки",
              len(get_calls()) == 1, str(get_calls()))
        check("и ровно одну дописанную страницу",
              total_line() == "показано 100 из 120", total_line())
        check("строк на экране столько же, сколько показано",
              row_count() == 100, str(row_count()))
        check("кнопка снова включена",
              page.evaluate("() => document.getElementById('sup-more').disabled") is False)

        print("\n== Отказ догрузки повторяет ТУ ЖЕ страницу, а не пропускает её ==")
        page.evaluate("() => { window.__supCalls = []; window.__supDelayMs = 0;"
                      " window.__supDelayMatch = ''; }")
        page.route(GET_RE, lambda route: route.fulfill(
            status=502, content_type="application/json",
            body='{"detail":"чтение снимка не удалось"}'))
        page.click("#sup-more")
        page.wait_for_timeout(900)
        failed = get_calls()
        check("запрошена была страница со смещением 100",
              failed and "offset=100" in failed[-1], str(failed))
        check("неудачная догрузка не сдвинула счётчик показанного",
              total_line() == "показано 100 из 120", total_line())
        check("и строк на экране не убавилось и не прибавилось",
              row_count() == 100, str(row_count()))
        check("кнопка вернулась в рабочее состояние",
              page.evaluate("() => document.getElementById('sup-more').disabled") is False)

        page.unroute(GET_RE)
        page.evaluate("() => { window.__supCalls = []; }")
        page.click("#sup-more")
        page.wait_for_timeout(1200)
        retried = get_calls()
        check("повтор запросил ТУ ЖЕ страницу, а не следующую",
              retried and "offset=100" in retried[-1], str(retried))
        check("и после успеха показано ровно 120 из 120",
              total_line() == "показано 120 из 120", total_line())
        check("а строк на экране ровно 120 — без дублей и без пропусков",
              row_count() == 120, str(row_count()))

        print("\n== Поздний ответ прежнего фильтра не дописывается в новое ==")
        # Снимку добавляется одна строка с ошибкой: очередь «Ошибки» должна
        # быть НЕПУСТОЙ и заведомо другой длины, иначе подмену не отличить.
        write_snapshot([SHEET_A, SHEET_B],
                       [make_row(i + 1, SHEET_A) for i in range(120)]
                       + [make_row(121, SHEET_A, invalid=True)])
        page.goto(f"{base}/supply")
        page.wait_for_timeout(1200)
        check("исходное состояние: показана первая страница из 121",
              total_line() == "показано 50 из 121", total_line())
        page.evaluate("() => { window.__supCalls = []; window.__supDelayMs = 1500;"
                      " window.__supDelayMatch = 'offset=50'; }")
        page.click("#sup-more")               # уходит МЕДЛЕННЫЙ запрос
        page.wait_for_timeout(200)
        page.evaluate("() => { const b = [...document.querySelectorAll('#sup-filters .sup-chip')]"
                      ".find(x => x.textContent === 'Ошибки'); if (b) b.click(); }")
        page.wait_for_timeout(2500)
        check("новый фильтр показал ровно свои строки",
              total_line() == "показано 1 из 1", total_line())
        check("и поздний ответ прежнего фильтра в них не дописался",
              row_count() == 1, str(row_count()))
        check("оба запроса при этом действительно были сделаны",
              len(get_calls()) >= 2, str(get_calls()))

        # ── 3. Устаревший фильтр листа после обновления ────────────────────
        print("\n== После успешного обновления фильтр листа сбрасывается ==")
        page.evaluate("() => { window.__supDelayMs = 0; window.__supDelayMatch = ''; }")
        write_snapshot([SHEET_A, SHEET_B], [make_row(i + 1, SHEET_A) for i in range(3)])
        page.route(REFRESH_RE, lambda route: route.fulfill(
            status=200, content_type="application/json",
            body='{"ok":true,"unchanged":false}'))
        page.goto(f"{base}/supply")
        page.wait_for_timeout(1200)
        page.evaluate("(name) => { const b = [...document.querySelectorAll('#sup-filters .sup-chip')]"
                      ".find(x => x.textContent === name); if (b) b.click(); }", SHEET_A)
        page.wait_for_timeout(900)
        check("лист выбран и запрошен у сервера",
              any("sheet=" in u for u in get_calls()), str(get_calls()[-1:]))
        check("и сервер отдал только его строки",
              total_line() == "показано 3 из 3", total_line())

        # Имена листов в снимке меняются — ровно то, что делает удачное
        # обновление с новыми именами. Сервер после этого честно отвечает 400
        # на прежний лист, и это не заглушка, а его настоящее поведение.
        write_snapshot([SHEET_C, SHEET_D], [make_row(i + 1, SHEET_C) for i in range(3)])
        stale = c.get(f"/api/supply/sheets?sheet={SHEET_A}")
        check("прежний лист теперь действительно даёт 400 у сервера",
              stale.status_code == 400, f"{stale.status_code} {stale.text[:90]}")

        page.evaluate("() => { window.__supCalls = []; }")
        page.fill("#sup-cur", SHEET_C)
        page.fill("#sup-next", SHEET_D)
        page.click("#sup-refresh")
        page.wait_for_timeout(1600)
        after = get_calls()
        check("следующий GET уже НЕ просит прежний лист",
              after and all("sheet=" not in u for u in after), str(after))
        check("и страница показывает новый снимок, а не отказ",
              total_line() == "показано 3 из 3", total_line())
        check("кнопка обновления вернулась в рабочее состояние",
              page.evaluate("() => document.getElementById('sup-refresh').disabled") is False)
        check("и её подпись снова обычная",
              (page.text_content("#sup-refresh") or "") == "Обновить предпросмотр",
              page.text_content("#sup-refresh") or "")
        check("очередь при этом сохранена — она к именам листов не относится",
              page.evaluate("() => { const on = document.querySelector('#sup-filters .sup-chip.on');"
                            " return on ? on.textContent : ''; }") == "Все")

        print("\n== Неудачное обновление фильтр и строки оставляет как есть ==")
        page.evaluate("(name) => { const b = [...document.querySelectorAll('#sup-filters .sup-chip')]"
                      ".find(x => x.textContent === name); if (b) b.click(); }", SHEET_C)
        page.wait_for_timeout(900)
        page.unroute(REFRESH_RE)
        page.route(REFRESH_RE, lambda route: route.fulfill(
            status=502, content_type="application/json",
            body='{"detail":"источник не отдал CSV"}'))
        page.evaluate("() => { window.__supCalls = []; }")
        page.click("#sup-refresh")
        page.wait_for_timeout(1600)
        after_fail = get_calls()
        check("на неудаче прежний фильтр листа сохранён",
              after_fail and all("sheet=" in u for u in after_fail), str(after_fail))
        check("строки прежнего снимка остались на экране",
              row_count() == 3, str(row_count()))
        check("и кнопка обновления снова доступна",
              page.evaluate("() => document.getElementById('sup-refresh').disabled") is False)

        print("\n== Кнопка восстанавливается даже если следующий GET упал ==")
        page.unroute(REFRESH_RE)
        page.route(REFRESH_RE, lambda route: route.fulfill(
            status=200, content_type="application/json",
            body='{"ok":true,"unchanged":false}'))
        page.route(GET_RE, lambda route: route.fulfill(
            status=502, content_type="application/json",
            body='{"detail":"чтение снимка не удалось"}'))
        page.click("#sup-refresh")
        page.wait_for_timeout(1600)
        check("кнопка не осталась выключенной навсегда",
              page.evaluate("() => document.getElementById('sup-refresh').disabled") is False)
        check("и её подпись вернулась, а не застыла на «Читаем таблицу…»",
              (page.text_content("#sup-refresh") or "") == "Обновить предпросмотр",
              page.text_content("#sup-refresh") or "")
        page.unroute(GET_RE)
        page.unroute(REFRESH_RE)

        # ── 4. Неполный итог называется словами, а не числом ───────────────
        print("\n== Неполный итог штук показан как «не определено» ==")
        write_snapshot([SHEET_A, SHEET_B],
                       [make_row(1, SHEET_A), make_row(2, SHEET_A, invalid=True)])
        page.goto(f"{base}/supply")
        page.wait_for_timeout(1200)
        summary = page.text_content("#sup-summary") or ""
        check("вместо числа сказано «не определено» и названо прочитанное",
              "не определено" in summary and "прочитано 9" in summary, summary[:220])
        check("и голого числа итога на плитке штук нет",
              "штук9" not in summary.replace(" ", ""), summary[:220])

        write_snapshot([SHEET_A, SHEET_B], [make_row(1, SHEET_A), make_row(2, SHEET_A)])
        page.goto(f"{base}/supply")
        page.wait_for_timeout(1200)
        summary = page.text_content("#sup-summary") or ""
        check("полный итог по-прежнему показывается числом",
              "не определено" not in summary and "10" in summary, summary[:220])

        # ── 5. Переход между фильтрами атомарен ────────────────────────────
        #
        # Замечание ревью PR #47 на HEAD `590b5c6`. Прежняя редакция меняла
        # `state.queue`/`state.sheet` и уходила за данными, а старые строки,
        # старый `last` и старая кнопка догрузки оставались на экране. Отказ
        # нового GET оставлял человека перед строками ОДНОГО фильтра под
        # подписью ДРУГОГО, а следующий клик по «Показать ещё» уходил за
        # `offset=50` НОВОГО фильтра и дописывал его к строкам СТАРОГО.
        def data_rows() -> int:
            """Строки данных, а не любые `tr`: заглушка — это тоже строка."""
            return page.evaluate(
                "() => document.querySelectorAll('#sup-rows td.sup-src').length")

        def more_hidden() -> bool:
            return page.evaluate(
                "() => { const b = document.getElementById('sup-more');"
                " return b.style.display === 'none' && b.disabled === true; }")

        def active_chip(kind: str) -> str:
            """Активный чип НУЖНОГО рода.

            Чипов два ряда — очередь и лист, — и активен всегда один в каждом:
            «первый .on в документе» отвечал бы на другой вопрос.
            """
            return page.evaluate(
                "(kind) => { const on = document.querySelector("
                "'#sup-filters .sup-chip.on[data-kind=\"' + kind + '\"]');"
                " return on ? on.textContent : ''; }", kind)

        def click_chip(label: str) -> None:
            page.evaluate("(name) => { const b = [...document.querySelectorAll("
                          "'#sup-filters .sup-chip')].find(x => x.textContent === name);"
                          " if (b) b.click(); }", label)

        print("\n== Отказ при смене очереди не оставляет строк прежней ==")
        page.evaluate("() => { window.__supDelayMs = 0; window.__supDelayMatch = ''; }")
        write_snapshot([SHEET_A, SHEET_B],
                       [make_row(i + 1, SHEET_A) for i in range(120)]
                       + [make_row(121, SHEET_A, invalid=True)])
        page.goto(f"{base}/supply")
        page.wait_for_timeout(1200)
        check("исходно на экране первая страница прежнего фильтра",
              data_rows() == 50 and total_line() == "показано 50 из 121",
              f"{data_rows()} {total_line()}")

        page.route(GET_RE, lambda route: route.fulfill(
            status=502, content_type="application/json",
            body='{"detail":"чтение снимка не удалось"}'))
        page.evaluate("() => { window.__supCalls = []; }")
        click_chip("Ошибки")
        page.wait_for_timeout(900)
        check("строк прежнего фильтра на экране не осталось",
              data_rows() == 0, str(data_rows()))
        check("счётчик показанного тоже не врёт о прежнем виде",
              total_line() == "", total_line())
        check("кнопка догрузки убрана и выключена", more_hidden() is True)
        check("активный чип описывает НОВЫЙ (пустой) вид, а не старые строки",
              active_chip("queue") == "Ошибки", active_chip("queue"))
        check("человеку названа причина и дана кнопка повтора",
              page.evaluate("() => !!document.getElementById('sup-retry')") is True,
              page.text_content("#sup-rows") or "")

        page.evaluate("() => { window.__supCalls = []; }")
        page.evaluate("() => document.getElementById('sup-more')"
                      ".dispatchEvent(new MouseEvent('click', {bubbles: true}))")
        page.wait_for_timeout(600)
        check("попытка догрузки после отказа не делает запроса вовсе",
              get_calls() == [], str(get_calls()))
        check("и уж точно не просит offset=50 для нового фильтра",
              all("offset=50" not in u for u in get_calls()), str(get_calls()))
        check("и дописывать в DOM ей тоже нечего", data_rows() == 0, str(data_rows()))

        page.unroute(GET_RE)
        page.evaluate("() => { window.__supCalls = []; }")
        page.click("#sup-retry")
        page.wait_for_timeout(1200)
        retry_calls = get_calls()
        check("повтор начинает НОВЫЙ фильтр с начала — offset=0",
              retry_calls and "offset=0" in retry_calls[-1]
              and "queue=invalid" in retry_calls[-1], str(retry_calls))
        check("и показывает ровно строки нового фильтра, без примеси старых",
              data_rows() == 1 and total_line() == "показано 1 из 1",
              f"{data_rows()} {total_line()}")

        print("\n== То же самое при смене ЛИСТА ==")
        click_chip("Все")
        page.wait_for_timeout(900)
        check("вернулись к «Все»: строк снова много",
              data_rows() == 50 and active_chip("queue") == "Все",
              f"{data_rows()} {active_chip('queue')}")
        page.route(GET_RE, lambda route: route.fulfill(
            status=502, content_type="application/json",
            body='{"detail":"чтение снимка не удалось"}'))
        page.evaluate("() => { window.__supCalls = []; }")
        click_chip(SHEET_A)
        page.wait_for_timeout(900)
        check("строк «обоих листов» под фильтром одного листа не осталось",
              data_rows() == 0 and total_line() == "", str(data_rows()))
        check("кнопка догрузки и здесь убрана и выключена", more_hidden() is True)
        check("активный чип — выбранный лист, а не прежний вид",
              active_chip("sheet") == SHEET_A, active_chip("sheet"))
        page.evaluate("() => { window.__supCalls = []; }")
        page.evaluate("() => document.getElementById('sup-more')"
                      ".dispatchEvent(new MouseEvent('click', {bubbles: true}))")
        page.wait_for_timeout(600)
        check("догрузка по несуществующему виду запроса не делает",
              get_calls() == [], str(get_calls()))
        page.unroute(GET_RE)
        page.evaluate("() => { window.__supCalls = []; }")
        page.click("#sup-retry")
        page.wait_for_timeout(1200)
        retry_calls = get_calls()
        check("повтор просит именно этот лист и с начала",
              retry_calls and "offset=0" in retry_calls[-1]
              and "sheet=" in retry_calls[-1], str(retry_calls))
        check("и на экране строки только этого листа",
              data_rows() == 50 and total_line() == "показано 50 из 121",
              f"{data_rows()} {total_line()}")

        print("\n== Удачное обновление + упавший следующий GET ==")
        # Отдельный случай, и он НЕ сводится к предыдущим: снимок на сервере
        # уже ДРУГОЙ, а на экране — интерактивные строки прежнего. Показывать
        # их дальше значит показывать данные, которых на сервере больше нет.
        write_snapshot([SHEET_A, SHEET_B], [make_row(i + 1, SHEET_A) for i in range(3)])
        page.route(REFRESH_RE, lambda route: route.fulfill(
            status=200, content_type="application/json",
            body='{"ok":true,"unchanged":false}'))
        page.goto(f"{base}/supply")
        page.wait_for_timeout(1200)
        check("на экране прежний снимок целиком",
              data_rows() == 3 and total_line() == "показано 3 из 3",
              f"{data_rows()} {total_line()}")
        # Сервер меняет снимок ровно так, как это делает удачное обновление.
        write_snapshot([SHEET_C, SHEET_D],
                       [make_row(i + 1, SHEET_C) for i in range(5)])
        page.route(GET_RE, lambda route: route.fulfill(
            status=502, content_type="application/json",
            body='{"detail":"чтение снимка не удалось"}'))
        page.evaluate("() => { window.__supCalls = []; }")
        page.click("#sup-refresh")
        page.wait_for_timeout(1600)
        check("строки ПРЕЖНЕГО снимка с экрана убраны",
              data_rows() == 0 and total_line() == "", str(data_rows()))
        check("и догрузка по ним невозможна", more_hidden() is True)
        page.evaluate("() => { window.__supCalls = []; }")
        page.evaluate("() => document.getElementById('sup-more')"
                      ".dispatchEvent(new MouseEvent('click', {bubbles: true}))")
        page.wait_for_timeout(600)
        check("смешать два снимка догрузкой нечем",
              get_calls() == [], str(get_calls()))
        check("кнопка обновления при этом снова рабочая",
              page.evaluate("() => document.getElementById('sup-refresh').disabled")
              is False)
        page.unroute(GET_RE)
        page.evaluate("() => { window.__supCalls = []; }")
        page.click("#sup-retry")
        page.wait_for_timeout(1200)
        retry_calls = get_calls()
        check("повтор просит новый снимок с начала и без устаревшего листа",
              retry_calls and "offset=0" in retry_calls[-1]
              and "sheet=" not in retry_calls[-1], str(retry_calls))
        check("и на экране НОВЫЙ снимок целиком, без строк прежнего",
              data_rows() == 5 and total_line() == "показано 5 из 5",
              f"{data_rows()} {total_line()}")
        page.unroute(REFRESH_RE)

        # ── 5а. Поздний ОТКАЗ снятого вида не стирает новый ───────────────
        #
        # Замечание ревью PR #47 на HEAD `459f170`. Поколение проверялось только
        # в ветке успеха: `load()` молча бросал поздний УСПЕХ, но поздний ОТКАЗ
        # летел дальше безусловно и попадал в `.catch()` того `switchView()`,
        # который его начинал. Медленный переход A, отказавший ПОСЛЕ того, как
        # быстрый переход B уже нарисовал свои строки, стирал строки B и рисовал
        # поверх них ошибку A вместе с кнопкой «Повторить» — привязанной к
        # текущему состоянию, то есть к чужому фильтру.
        #
        # Проверкой разметки это не ловится: разметка правильная, неверен
        # порядок во времени. Поэтому здесь настоящий браузер, настоящая
        # задержка ровно одного URL и настоящий 502 ровно на нём.
        print("\n== Поздний 502 снятого фильтра не трогает уже показанный ==")
        page.evaluate("() => { window.__supDelayMs = 0; window.__supDelayMatch = ''; }")
        write_snapshot([SHEET_A, SHEET_B],
                       [make_row(i + 1, SHEET_A) for i in range(120)]
                       + [make_row(121, SHEET_A, invalid=True)])
        page.goto(f"{base}/supply")
        page.wait_for_timeout(1200)
        check("исходно показан первый фильтр целиком",
              data_rows() == 50 and total_line() == "показано 50 из 121",
              f"{data_rows()} {total_line()}")

        # Падает и тормозит РОВНО фильтр A (`queue=invalid`). Остальные запросы
        # идут на настоящий сервер и отвечают быстро — иначе «B успел» было бы
        # не свойством страницы, а свойством заглушки.
        def only_invalid_fails(route):
            if "queue=invalid" in route.request.url:
                route.fulfill(status=502, content_type="application/json",
                              body='{"detail":"фильтр A не прочитался"}')
            else:
                route.continue_()

        page.route(GET_RE, only_invalid_fails)
        page.evaluate("() => { window.__supCalls = []; window.__supDelayMs = 1800;"
                      " window.__supDelayMatch = 'queue=invalid'; }")
        click_chip("Ошибки")                  # A — медленный, обречённый
        page.wait_for_timeout(250)
        click_chip("Все")                     # B — быстрый и удачный
        page.wait_for_timeout(900)
        check("B успел нарисоваться, пока A ещё в полёте",
              data_rows() == 50 and total_line() == "показано 50 из 121"
              and active_chip("queue") == "Все",
              f"{data_rows()} {total_line()} {active_chip('queue')}")

        page.wait_for_timeout(2200)           # сюда приходит 502 фильтра A
        after_calls = get_calls()
        check("оба запроса действительно были сделаны",
              any("queue=invalid" in u for u in after_calls)
              and any("queue=all" in u for u in after_calls), str(after_calls))
        check("строки B на месте — поздний отказ A их не стёр",
              data_rows() == 50, str(data_rows()))
        check("и счётчик показанного остался счётчиком B",
              total_line() == "показано 50 из 121", total_line())
        check("и подсветка чипа тоже описывает B, а не A",
              active_chip("queue") == "Все", active_chip("queue"))
        check("заглушки с причиной отказа A на экране нет вовсе",
              "Не удалось загрузить строки" not in (page.text_content("#sup-rows") or ""),
              (page.text_content("#sup-rows") or "")[:160])
        check("и устаревшей кнопки «Повторить» тоже нет — перепривязывать нечего",
              page.evaluate("() => !!document.getElementById('sup-retry')") is False)
        check("кнопка догрузки осталась рабочей кнопкой вида B",
              page.evaluate("() => { const b = document.getElementById('sup-more');"
                            " return b.style.display !== 'none' && !b.disabled; }")
              is True)

        # И главное — вид B остался ЖИВЫМ, а не просто нарисованным: догрузка
        # продолжает его же, с той позиции, на которой он остановился.
        page.evaluate("() => { window.__supCalls = []; window.__supDelayMs = 0;"
                      " window.__supDelayMatch = ''; }")
        page.click("#sup-more")
        page.wait_for_timeout(1200)
        more_calls = get_calls()
        check("догрузка продолжает именно B — offset=50 и queue=all",
              more_calls and "offset=50" in more_calls[-1]
              and "queue=all" in more_calls[-1], str(more_calls))
        check("и дописала страницу к строкам B, а не к чему-то ещё",
              data_rows() == 100 and total_line() == "показано 100 из 121",
              f"{data_rows()} {total_line()}")
        page.unroute(GET_RE)

        # Контроль сверху: отказ СВОЕГО, не снятого вида по-прежнему виден.
        # Иначе «поздний отказ не трогает экран» можно было бы выполнить,
        # перестав показывать отказы вовсе.
        page.route(GET_RE, lambda route: route.fulfill(
            status=502, content_type="application/json",
            body='{"detail":"чтение снимка не удалось"}'))
        click_chip("Ошибки")
        page.wait_for_timeout(1200)
        check("отказ ТЕКУЩЕГО фильтра по-прежнему очищает экран и объясняет себя",
              data_rows() == 0 and total_line() == ""
              and page.evaluate("() => !!document.getElementById('sup-retry')") is True,
              f"{data_rows()} {total_line()}")
        page.unroute(GET_RE)

        # ── 5б. Замок догрузки принадлежит ВИДУ, а не странице ────────────
        #
        # Замечание UX-аудита. `inflight` был один на всю страницу и жил дольше
        # вида, который его взял. Отсюда два разных вреда из одной причины:
        #
        #   * медленная догрузка вида A не отпускала замок, человек переключался
        #     на новый вид, тот рисовал ВКЛЮЧЁННУЮ кнопку «Показать ещё» — и
        #     клик по ней молча не делал ничего: обработчик выходил по
        #     `inflight` чужого вида. Кнопка выглядит рабочей и не работает —
        #     худший вид отказа, потому что человеку нечего понять;
        #   * финализатор A, добежав позже, трогал кнопку ЖИВОГО вида и
        #     сбрасывал его замок — то есть снимал защиту от двойного запроса
        #     ровно там, где она нужна.
        #
        # Тормозится РОВНО ОДИН первый совпавший запрос (`__supDelayLimit = 1`):
        # догрузка снятого вида и догрузка живого идут по ОДНОМУ адресу, и
        # «тормозим всё, что совпало» затормозило бы обоих, спрятав ровно ту
        # разницу, которую проверка обязана увидеть.
        print("\n== Медленная догрузка A не запирает догрузку нового вида ==")
        page.evaluate("() => { window.__supDelayMs = 0; window.__supDelayMatch = '';"
                      " window.__supDelayLimit = -1; }")
        write_snapshot([SHEET_A, SHEET_B],
                       [make_row(i + 1, SHEET_A) for i in range(120)]
                       + [make_row(121, SHEET_A, invalid=True)])
        page.goto(f"{base}/supply")
        page.wait_for_timeout(1200)
        check("исходно на экране первая страница вида A",
              data_rows() == 50 and total_line() == "показано 50 из 121",
              f"{data_rows()} {total_line()}")

        page.evaluate("() => { window.__supCalls = []; window.__supDelayMs = 3000;"
                      " window.__supDelayMatch = 'queue=all&offset=50';"
                      " window.__supDelayLimit = 1; }")
        page.click("#sup-more")               # A: медленная догрузка, замок взят
        page.wait_for_timeout(200)
        check("замок вида A взят: его кнопка выключена",
              page.evaluate("() => document.getElementById('sup-more').disabled")
              is True)

        click_chip("Ошибки")                  # переход, вид A снят
        page.wait_for_timeout(900)
        check("нарисован уже другой вид",
              active_chip("queue") == "Ошибки" and total_line() == "показано 1 из 1",
              f"{active_chip('queue')} {total_line()}")

        # Вид «Ошибки» короткий, догружать в нём нечего — поэтому возвращаемся в
        # «Все» третьим переходом: это снова вид с догрузкой, и он тоже не
        # должен быть заперт замком давно снятого A.
        click_chip("Все")
        page.wait_for_timeout(900)
        check("вернулись в вид с догрузкой, кнопка включена",
              page.evaluate("() => { const b = document.getElementById('sup-more');"
                            " return b.style.display !== 'none' && !b.disabled; }")
              is True)

        page.evaluate("() => { window.__supCalls = []; }")
        page.click("#sup-more")               # догрузка ЖИВОГО вида
        page.wait_for_timeout(1200)
        live_calls = get_calls()
        check("догрузка живого вида действительно ушла на сервер",
              any("offset=50" in u for u in live_calls), str(live_calls))
        check("и дописала свою страницу, а не осталась немой кнопкой",
              data_rows() == 100 and total_line() == "показано 100 из 121",
              f"{data_rows()} {total_line()}")

        # Теперь ждём, пока добежит медленная догрузка A, и смотрим, что её
        # финализатор сделает с живым видом.
        page.wait_for_timeout(2600)
        check("поздний ответ A строк живого вида не дописал",
              data_rows() == 100 and total_line() == "показано 100 из 121",
              f"{data_rows()} {total_line()}")
        check("и кнопка живого вида осталась в своём состоянии",
              page.evaluate("() => { const b = document.getElementById('sup-more');"
                            " return b.style.display !== 'none' && !b.disabled; }")
              is True)

        # И самое главное: замок живого вида цел. Двойной клик по-прежнему даёт
        # ровно ОДИН запрос — финализатор A не снял чужую защиту.
        page.evaluate("() => { window.__supCalls = []; window.__supDelayMs = 700;"
                      " window.__supDelayMatch = 'offset=100';"
                      " window.__supDelayLimit = -1; }")
        page.click("#sup-more")
        page.wait_for_timeout(150)
        page.evaluate("() => document.getElementById('sup-more')"
                      ".dispatchEvent(new MouseEvent('click', {bubbles: true}))")
        page.wait_for_timeout(1600)
        check("замок живого вида цел: двойной клик дал ровно один запрос",
              len(get_calls()) == 1, str(get_calls()))
        # 50 + 50 + 21 = 121: последняя страница короткая, и это НЕ дубль.
        # Дубль виден иначе — числом строк в DOM, поэтому сверяются оба.
        check("и показано ровно 121 из 121, без дублей и без пропусков",
              total_line() == "показано 121 из 121" and data_rows() == 121,
              f"{total_line()} {data_rows()}")
        page.evaluate("() => { window.__supDelayMs = 0; window.__supDelayMatch = '';"
                      " window.__supDelayLimit = -1; }")

        # ── 5в. Замок обновления переживает пересборку формы ───────────────
        #
        # Замечание UX-аудита. Замок «идёт обновление» жил в САМОМ элементе
        # кнопки (`btn.disabled`), а кнопку пересоздаёт `renderForm()` на каждом
        # успешном GET. Значит любой переход фильтра во время медленного POST
        # рисовал НОВУЮ включённую кнопку — и второй клик отправлял второй
        # POST теми же (или уже другими) полями формы. Два обновления одного
        # снимка наперегонки: чей ответ придёт вторым, тот и определит, что
        # человек увидит.
        #
        # Попытки считаются НА СТОРОНЕ СТРАНИЦЫ (`__supCalls`), а не по приходу
        # в обработчик playwright: запрос заторможен до отправки, и обработчик
        # узнал бы о нём только через три секунды — проверка «второго POST нет»
        # была бы зелёной просто потому, что первый ещё не долетел.
        print("\n== Обновление идёт: пересборка формы кнопку не отпирает ==")
        write_snapshot([SHEET_A, SHEET_B],
                       [make_row(i + 1, SHEET_A) for i in range(120)]
                       + [make_row(121, SHEET_A, invalid=True)])
        arrived = []

        def count_refresh(route):
            arrived.append(route.request.url)
            route.fulfill(status=200, content_type="application/json",
                          body='{"ok":true,"unchanged":false}')

        def refresh_attempts() -> int:
            return len([u for u in calls() if "/refresh" in u])

        page.goto(f"{base}/supply")
        page.wait_for_timeout(1200)
        page.route(REFRESH_RE, count_refresh)
        page.evaluate("() => { window.__supCalls = []; window.__supDelayMs = 3000;"
                      " window.__supDelayMatch = '/refresh';"
                      " window.__supDelayLimit = -1; }")
        page.click("#sup-refresh")
        page.wait_for_timeout(200)
        check("кнопка обновления выключена на время запроса",
              page.evaluate("() => document.getElementById('sup-refresh').disabled")
              is True)
        check("и попытка обновления пока ровно одна",
              refresh_attempts() == 1, str(refresh_attempts()))

        # Переход фильтра во время POST: успешный GET пересобирает форму.
        click_chip("Ошибки")
        page.wait_for_timeout(900)
        check("форма действительно пересобрана — кнопка на месте",
              page.evaluate("() => !!document.getElementById('sup-refresh')") is True)
        check("и ПЕРЕСОБРАННАЯ кнопка всё ещё показывает, что идёт чтение",
              page.evaluate("() => document.getElementById('sup-refresh').disabled")
              is True
              and (page.text_content("#sup-refresh") or "") == "Читаем таблицу…",
              page.text_content("#sup-refresh") or "")

        page.evaluate("() => document.getElementById('sup-refresh')"
                      ".dispatchEvent(new MouseEvent('click', {bubbles: true}))")
        page.wait_for_timeout(300)
        check("второй клик по пересобранной кнопке второго POST не отправил",
              refresh_attempts() == 1, str(refresh_attempts()))

        page.wait_for_timeout(3500)           # медленный POST добегает и оседает
        check("за весь сценарий на сервер ушёл ровно один POST",
              len(arrived) == 1, str(len(arrived)))
        check("кнопка обновления вернулась в рабочее состояние",
              page.evaluate("() => document.getElementById('sup-refresh').disabled")
              is False)
        check("и её подпись снова обычная",
              (page.text_content("#sup-refresh") or "") == "Обновить предпросмотр",
              page.text_content("#sup-refresh") or "")

        # Контроль сверху: замок не «залип». Следующее обновление возможно —
        # иначе требование можно было бы выполнить, запретив обновление совсем.
        page.evaluate("() => { window.__supDelayMs = 0; window.__supDelayMatch = '';"
                      " window.__supDelayLimit = -1; }")
        page.click("#sup-refresh")
        page.wait_for_timeout(1500)
        check("следующее обновление после этого снова возможно",
              len(arrived) == 2, str(len(arrived)))
        check("и кнопка снова свободна",
              page.evaluate("() => document.getElementById('sup-refresh').disabled")
              is False)
        page.unroute(REFRESH_RE)

        # ── 6. Совет после отказа подходит роли и состоянию подписки ───────
        #
        # Замечание ревью PR #47 (P3): прежний текст обещал ВСЕМ, что ссылка и
        # имена листов «сохранены в форме выше — поправьте и попробуйте снова».
        # Формы выше нет ни у участника, ни у владельца в readonly.
        print("\n== Совет после первого отказа зависит от роли ==")
        write_failed_attempt([SENTINEL_SHEET, SHEET_B],
                             f"лист «{SENTINEL_SHEET}»: источник ответил 403",
                             "лист источника: источник ответил 403")
        page.goto(f"{base}/supply")
        page.wait_for_timeout(1200)
        err_text = page.text_content("#sup-error") or ""
        check("владелец с открытой записью: причина показана целиком",
              "403" in err_text and SENTINEL_SHEET in err_text, err_text[:200])
        check("и совет ведёт в форму, которая у него на экране есть",
              "в форме выше" in err_text
              and page.evaluate("() => !!document.getElementById('sup-url')") is True,
              err_text[:200])

        set_trial(-40)
        os.environ["OBOROT_SUBSCRIPTION_GATE"] = "1"
        page.goto(f"{base}/supply")
        page.wait_for_timeout(1200)
        err_text = page.text_content("#sup-error") or ""
        check("владелец в readonly формы не видит",
              page.evaluate("() => !!document.getElementById('sup-url')") is False)
        check("и совета «поправьте в форме выше» ему больше не дают",
              "в форме выше" not in err_text, err_text[:220])
        check("вместо этого сказано, что настройки целы и когда вернётся правка",
              "сохранены" in err_text and "подписк" in err_text.lower(),
              err_text[:220])
        os.environ["OBOROT_SUBSCRIPTION_GATE"] = "0"
        set_trial(30)

        add_member("supply-ui-member@test.io")
        mc = httpx.Client(headers={"X-Oborot-CSRF": "1"}, base_url=base, timeout=60.0)
        mc.post("/login", data={"email": "supply-ui-member@test.io",
                                "password": "secret123"})
        mctx = browser.new_context(viewport={"width": 1400, "height": 900})
        mctx.add_cookies([{"name": k, "value": v, "domain": "127.0.0.1", "path": "/"}
                          for k, v in mc.cookies.items()])
        mpage = mctx.new_page()
        merrors: list[str] = []
        mpage.on("pageerror", lambda e: merrors.append(str(e)))
        mpage.goto(f"{base}/supply")
        mpage.wait_for_timeout(1200)
        m_err = mpage.text_content("#sup-error") or ""
        check("участник формы не видит вовсе",
              mpage.evaluate("() => !!document.getElementById('sup-url')") is False)
        check("и ему не советуют править несуществующую форму",
              "в форме выше" not in m_err, m_err[:220])
        check("сказано, что поправить связь может владелец организации",
              "владелец организации" in m_err, m_err[:220])
        check("причина отказа участнику при этом видна и непуста",
              ss.PUBLIC_FAILURE_REASONS["access"] in m_err, m_err[:220])
        check("а свободного текста из носителя на его экране нет",
              "источник ответил 403" not in (mpage.text_content("body") or ""),
              m_err[:220])
        check("а имени листа неудачной попытки на его экране нет нигде",
              SENTINEL_SHEET not in (mpage.text_content("body") or "")
              and SENTINEL_SHEET not in mpage.content(),
              (mpage.text_content("#sup-error") or "")[:220])
        check("страница участника тоже ожила, без ошибок в консоли",
              not merrors, str(merrors)[:200])
        mctx.close()
        mc.close()

        # ── 7. Настроенный источник + упавшая НОВАЯ попытка ────────────────
        #
        # Замечание ревью PR #47 на HEAD `5e21ba1` (thread r3898968262).
        # Владелец у уже настроенного источника меняет ссылку или имена листов,
        # и обновление падает. Сервер это состояние хранит правильно: успешный
        # снимок A/B остаётся снимком, а НОВАЯ попытка C/D лежит в `attempt`.
        # Форма же предпочитала `data.spreadsheet_url`/`data.sheet_names` и
        # подставляла обратно A/B — при том что текст под ошибкой обещает
        # «ссылка и имена листов сохранены в форме выше». Владелец, поправив
        # одну букву в имени листа, терял свой ввод и восстанавливал его по
        # памяти; а повторный клик уходил со СТАРЫМИ значениями, то есть
        # «Повторить» повторял не ту попытку, которая упала.
        print("\n== Настроен + новая попытка упала: в форме ввод ПОПЫТКИ ==")
        page.evaluate("() => { window.__supDelayMs = 0; window.__supDelayMatch = '';"
                      " window.__supDelayLimit = -1; }")
        write_snapshot_with_failed_attempt(
            [SHEET_A, SHEET_B], [make_row(i + 1, SHEET_A) for i in range(3)],
            [SHEET_C, SHEET_D])
        page.goto(f"{base}/supply")
        page.wait_for_timeout(1200)

        def field_value(fid: str) -> str:
            return page.evaluate("(id) => { const n = document.getElementById(id);"
                                 " return n ? n.value : null; }", fid)

        # Снимок на экране — ПРЕЖНИЙ. Его неудача не трогает, и это прежнее
        # решение D-51, а не побочный эффект правки.
        check("строки прежнего успешного снимка остались на экране",
              data_rows() == 3 and total_line() == "показано 3 из 3",
              f"{data_rows()} {total_line()}")
        check("и сводка тоже от прежнего снимка",
              (page.text_content("#sup-summary") or "").strip() != "")
        check("ссылка «открыть исходник» ведёт на УСПЕШНУЮ таблицу, не на попытку",
              page.evaluate("() => { const a = document.querySelector('#sup-src-link a');"
                            " return a ? a.getAttribute('href') : ''; }")
              == f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit",
              page.evaluate("() => { const a = document.querySelector('#sup-src-link a');"
                            " return a ? a.getAttribute('href') : ''; }"))

        # А поля формы — от ПОПЫТКИ: чинить владелец идёт именно её.
        check("в поле ссылки стоит адрес НЕУДАВШЕЙСЯ попытки",
              field_value("sup-url")
              == f"https://docs.google.com/spreadsheets/d/{ATTEMPT_SPREADSHEET_ID}/edit",
              str(field_value("sup-url")))
        check("и имена листов — тоже её",
              field_value("sup-cur") == SHEET_C
              and field_value("sup-next") == SHEET_D,
              f"{field_value('sup-cur')} | {field_value('sup-next')}")
        check("совет при этом обещает ровно то, что на экране есть",
              "в форме выше" in (page.text_content("#sup-error") or ""),
              (page.text_content("#sup-error") or "")[:160])

        # Повторный клик обязан повторить ТУ попытку, которая упала.
        posted = []

        def capture_refresh(route):
            posted.append(route.request.post_data or "")
            route.fulfill(status=502, content_type="application/json",
                          body='{"detail":"источник снова не отдал CSV"}')

        page.route(REFRESH_RE, capture_refresh)
        page.click("#sup-refresh")
        page.wait_for_timeout(1600)
        body = posted[-1] if posted else ""
        check("повторный клик отправил адрес ПОПЫТКИ, а не прежнего снимка",
              ATTEMPT_SPREADSHEET_ID in body and SPREADSHEET_ID not in body,
              body[:200])
        check("и её имена листов, без повторного ввода руками",
              SHEET_C in body and SHEET_D in body
              and SHEET_A not in body and SHEET_B not in body, body[:200])
        page.unroute(REFRESH_RE)

        # Участнику неудачная попытка не адресована вовсе — прежнее решение
        # D-51, и правка формы его не ослабляет.
        m2 = httpx.Client(headers={"X-Oborot-CSRF": "1"}, base_url=base, timeout=60.0)
        m2.post("/login", data={"email": "supply-ui-member@test.io",
                                "password": "secret123"})
        m2ctx = browser.new_context(viewport={"width": 1400, "height": 900})
        m2ctx.add_cookies([{"name": k, "value": v, "domain": "127.0.0.1", "path": "/"}
                           for k, v in m2.cookies.items()])
        m2page = m2ctx.new_page()
        m2errors: list[str] = []
        m2page.on("pageerror", lambda e: m2errors.append(str(e)))
        m2page.goto(f"{base}/supply")
        m2page.wait_for_timeout(1200)
        member_api = m2.get("/api/supply/sheets?limit=200").text
        check("участнику адрес и листы попытки не отдаются вовсе",
              ATTEMPT_SPREADSHEET_ID not in member_api
              and SHEET_C not in member_api and SHEET_D not in member_api,
              member_api[:200])
        check("и на его экране их тоже нет",
              ATTEMPT_SPREADSHEET_ID not in (m2page.content() or "")
              and SHEET_C not in (m2page.text_content("body") or ""),
              (m2page.text_content("#sup-error") or "")[:160])
        check("прежний снимок участник по-прежнему видит целиком",
              m2page.evaluate("() => document.querySelectorAll("
                              "'#sup-rows td.sup-src').length") == 3)
        check("страница участника без ошибок в консоли", not m2errors,
              str(m2errors)[:200])
        m2ctx.close()
        m2.close()

        # Испорченный источник попытки — безопасный откат к успешным значениям,
        # а не мусор в полях и не пустая форма.
        print("\n== Испорченная попытка: откат к успешным значениям ==")
        write_snapshot_with_failed_attempt(
            [SHEET_A, SHEET_B], [make_row(i + 1, SHEET_A) for i in range(3)],
            [SHEET_C, SHEET_D], source_ok=False)
        page.goto(f"{base}/supply")
        page.wait_for_timeout(1200)
        check("в поле ссылки — адрес удачного снимка",
              field_value("sup-url")
              == f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit",
              str(field_value("sup-url")))
        check("и его же имена листов",
              field_value("sup-cur") == SHEET_A
              and field_value("sup-next") == SHEET_B,
              f"{field_value('sup-cur')} | {field_value('sup-next')}")
        check("строки при этом на месте, экран не сломался",
              data_rows() == 3, str(data_rows()))

        # ── 8. Владелец без носителя: формы нет, есть причина и дорога ─────
        #
        # Замечание ревью PR #47 на HEAD `5e21ba1` (thread r3898968267).
        # У организации нет ни МойСклад, ни демо-подключения — снимку негде
        # жить. Сервер отвечает на POST кодом 409 ГАРАНТИРОВАННО и ДО единого
        # сетевого вызова, а страница всё равно строила рабочую форму и
        # включённую кнопку: человека просили ввести три значения ради
        # действия, которое не может получиться. Это тот же класс ошибки, что
        # уже закрыт для приостановленной подписки, — и закрывается он так же.
        print("\n== Нет носителя: формы и кнопки нет, есть причина и /settings ==")
        saved_carriers = drop_carriers()
        try:
            page.evaluate("() => { window.__supCalls = []; }")
            page.goto(f"{base}/supply")
            page.wait_for_timeout(1200)
            check("сервер действительно сообщает, что носителя нет",
                  c.get("/api/supply/sheets").json().get("carrier_present") is False)
            check("поля ссылки на экране нет вовсе",
                  page.evaluate("() => !!document.getElementById('sup-url')") is False)
            check("и полей листов тоже нет",
                  page.evaluate("() => !!document.getElementById('sup-cur')") is False
                  and page.evaluate("() => !!document.getElementById('sup-next')")
                  is False)
            check("и кнопки обновления нет",
                  page.evaluate("() => !!document.getElementById('sup-refresh')")
                  is False)
            form_text = page.text_content("#sup-form-wrap") or ""
            check("названа причина: хранить предпросмотр негде",
                  "подключени" in form_text.lower() and "негде" in form_text.lower(),
                  form_text[:200])
            settings_href = page.evaluate(
                "() => { const a = [...document.querySelectorAll('#sup-form-wrap a')]"
                ".find(x => x.getAttribute('href') === '/settings');"
                " return a ? a.getAttribute('href') : ''; }")
            check("и дана дорога — ссылка на «Настройки»",
                  settings_href == "/settings", str(settings_href))
            check("ссылка рабочая, а не украшение",
                  c.get("/settings").status_code == 200,
                  str(c.get("/settings").status_code))
            check("страница при этом ни одного POST обновления не отправила",
                  all("/refresh" not in u for u in calls()), str(calls()))
            # Серверная граница не ослаблена: прямой POST по-прежнему 409 и
            # по-прежнему ДО сети и без единой записи.
            before_blob = carrier_blob()
            direct = c.post("/api/supply/sheets/refresh",
                            json={"spreadsheet_url":
                                  f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit",
                                  "sheet_names": [SHEET_A, SHEET_B]})
            check("прямой POST в обход страницы — 409, а не 200",
                  direct.status_code == 409, f"{direct.status_code} {direct.text[:110]}")
            check("и он ничего не записал", carrier_blob() == before_blob)
        finally:
            restore_carriers(saved_carriers)

        # Носитель вернулся — форма и кнопка обязаны вернуться вместе с ним.
        write_snapshot([SHEET_A, SHEET_B], [make_row(i + 1, SHEET_A) for i in range(3)])
        page.goto(f"{base}/supply")
        page.wait_for_timeout(1200)
        check("носитель вернулся — вернулась и форма",
              page.evaluate("() => !!document.getElementById('sup-url')") is True
              and page.evaluate("() => !!document.getElementById('sup-cur')") is True)
        check("и рабочая кнопка обновления",
              page.evaluate("() => { const b = document.getElementById('sup-refresh');"
                            " return !!b && !b.disabled; }") is True)
        check("и снимок снова показан",
              data_rows() == 3 and total_line() == "показано 3 из 3",
              f"{data_rows()} {total_line()}")

        check("на странице не было ошибок в консоли", not errors, str(errors)[:200])
        ctx.close()
        browser.close()

    c.close()
    print(f"\nИТОГО: {len(PASS)} OK, {len(FAIL)} FAIL")
    for name in FAIL:
        print(f"  FAIL {name}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
