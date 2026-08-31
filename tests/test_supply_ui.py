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
SHEET_C, SHEET_D = "Весна 27", "Лето 27"
SPREADSHEET_ID = "1AbCdEf_ghijklmnop-QRSTUV0123456789wxyz"

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
DELAY_SCRIPT = """
(() => {
  window.__supCalls = [];
  window.__supDelayMs = 0;
  window.__supDelayMatch = "";
  const real = window.fetch;
  window.fetch = function (url, init) {
    const u = String((url && url.url) || url);
    const self = this, args = arguments;
    if (u.indexOf("/api/supply/sheets") !== -1) window.__supCalls.push(u);
    const wanted = window.__supDelayMatch;
    const delay = (wanted && u.indexOf(wanted) !== -1) ? (window.__supDelayMs || 0) : 0;
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
