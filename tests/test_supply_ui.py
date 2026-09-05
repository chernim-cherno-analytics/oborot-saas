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


def make_row(index: int, sheet: str, *, invalid: bool = False,
             price: str = "", name: str | None = None) -> dict:
    """Строка снимка ровно в той форме, которую выпускает парсер.

    `price` кладётся в `price_raw` — колонку 16 источника, которую парсер
    признаёт и потому ИСКЛЮЧАЕТ из `unknown_raw`. Значение остаётся сырым
    текстом: ни числом, ни валютой оно в предпросмотре не становится.
    """
    sizes = {"XS": 1, "S": 1, "M": 1, "L": 1, "XL": None if invalid else 1}
    sizes_raw = {"XS": "1", "S": "1", "M": "1", "L": "1",
                 "XL": "Кроим по заданию" if invalid else "1"}
    known = sum(v for v in sizes.values() if v is not None)
    return {
        "sheet_name": sheet, "source_row": index, "anchor_row": index,
        "is_blank": False,
        "article_raw": f"A{index}", "name_raw": name or f"Позиция {index}",
        "article": f"A{index}", "name": name or f"Позиция {index}",
        "color_raw": "Чёрный", "qty_meters_raw": "", "sketch_raw": "",
        "sizes": sizes, "sizes_raw": sizes_raw, "size_sum": known,
        "source_total_raw": str(known), "source_total": known,
        "comments_raw": ["", "", ""], "source_status_raw": "",
        "price_raw": price, "components_raw": "", "production_raw": "",
        "unknown_raw": {}, "issues": ["invalid_quantity"] if invalid else [],
    }


#: Длинные значения — по ФОРМЕ такие же, как в живой производственной таблице:
#: длинное имя модели, составной цвет, свободный комментарий со статусом,
#: адрес самовывоза, перечень комплектующих и текст прямо в колонке размера.
#: Содержимое выдумано целиком: живых данных, имён и контактов здесь нет.
LONG_NAME = ("Платье миди из плотного трикотажа с потайной молнией и разрезом "
             "сзади, лимитированная капсула сезона")
LONG_COLOR = "Тёмно-изумрудный с переходом в бутылочный (партия 2)"
LONG_STATUS = ("Отгружено 12 шт на склад, остаток в раскрое; ждём подтверждения "
               "подрядчика по срокам и по цвету подкладки")
LONG_ADDRESS = ("Забрать: г. Вымышленск, ул. Придуманная, д. 1, стр. 4, вход со "
                "двора, оф. 512, с 10 до 18")
LONG_COMPONENTS = ("Молния потайная 60 см — 40 шт, бирка тканая — 40 шт, "
                   "пуговица 18 мм — 120 шт, лента репсовая 15 мм — 25 м")
LONG_UNKNOWN = "прочее: согласовано устно, счёт выставят позже, номер уточнить"
RAW_SIZE = "Кроим по заданию"


def make_long_row(index: int, sheet: str) -> dict:
    """Строка, на которой прежний экран разваливался: 323 px высоты.

    Все длинные значения сразу — и в размерной колонке текст вместо числа.
    Ровно эта строка проверяет и уплотнение, и сохранность исходного текста:
    высота обязана остаться строкой, а весь текст — остаться доступным.
    """
    row = make_row(index, sheet, name=LONG_NAME)
    row.update({
        "color_raw": LONG_COLOR,
        "comments_raw": [LONG_STATUS, LONG_ADDRESS, ""],
        "source_status_raw": LONG_STATUS,
        "components_raw": LONG_COMPONENTS,
        "production_raw": "Цех №3, Вымышленск",
        "qty_meters_raw": "3,2",
        "price_raw": "12 900",
        "unknown_raw": {"21": LONG_UNKNOWN},
        "sizes": {"XS": 1, "S": 1, "M": 1, "L": 1, "XL": None},
        "sizes_raw": {"XS": "1", "S": "1", "M": "1", "L": "1", "XL": RAW_SIZE},
        "size_sum": 4,
        "source_total_raw": "5", "source_total": 5,
        "issues": ["invalid_quantity", "total_mismatch", "unknown_column"],
    })
    return row


def write_snapshot(sheets, rows, content_sha256: str = "0" * 64) -> None:
    """Положить снимок в носителя организации.

    Счётчики считает САМ слой (`ss.build_counts`), а не тест: иначе проверка
    экрана опиралась бы на числа, выдуманные рядом с проверкой, и доказывала
    бы согласие теста с самим собой.

    `content_sha256` задаётся снаружи там, где проверяется ГОНКА ВЕРСИЙ: две
    версии снимка обязаны отличаться именно хешем содержимого, потому что
    страница различает их по нему, а не по числу строк.
    """
    envelope = {
        "schema_version": ss.ENVELOPE_SCHEMA_VERSION,
        "parser_version": ss.PARSER_VERSION,
        "spreadsheet_id": SPREADSHEET_ID,
        "sheet_names": list(sheets),
        "content_sha256": content_sha256,
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
                                       source_ok: bool = True,
                                       source_override=None) -> None:
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
    # ЧАСТИЧНАЯ порча — отдельно от полной. Полностью нечитаемый источник
    # попытки страница и раньше откатывала целиком; опасен именно тот случай,
    # когда ОДНА из двух половин тройки цела: из неё собирается попытка,
    # которой никто никогда не делал.
    if source_override is not None:
        source = source_override
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


def open_preview(p, base: str) -> None:
    """Открыть /supply и перейти на вкладку предпросмотра.

    SUPPLY-3 добавил на страницу ВТОРОЙ раздел — план производства, — и он
    открыт по умолчанию: это то, что человек ведёт сам. Предпросмотр чужой
    таблицы никуда не делся и доступен целиком, но теперь за одним явным
    нажатием. Проверки предпросмотра ходят сюда, потому что человек ходит так
    же: набор обязан повторять его путь, а не обращаться к скрытой разметке.
    """
    p.goto(f"{base}/supply")
    p.click("#sup-tab-preview")
    p.wait_for_timeout(120)


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
            # Панель подробностей — тоже `tr`, но она не строка снимка, а её
            # раскрытие. Считаем строки, а не узлы.
            return page.evaluate(
                "() => document.querySelectorAll('#sup-rows tr:not(.sup-det)').length")

        # ── 1. Гейт подписки: read-only вместо формы, но снимок на месте ────
        print("\n== Подписка readonly: владельцу не предлагают то, в чём откажут ==")
        big = [make_row(i + 1, SHEET_A) for i in range(120)]
        write_snapshot([SHEET_A, SHEET_B], big)

        set_trial(30)
        open_preview(page, base)
        page.wait_for_timeout(1200)
        # Девятый дефект прошлого корректива: страница была МЕРТВА в браузере
        # (`api is not defined` при разборе блока `scripts`, потому что
        # `static/app.js` подключён с `defer`). Проверка стоит здесь и явно:
        # ни одной ошибки в консоли и первый же элемент, который рисует JS.
        check("страница ожила: скрипт дождался DOMContentLoaded",
              not errors and page.evaluate(
                  "() => document.querySelectorAll('#sup-rows tr:not(.sup-det)')"
                  ".length") > 0,
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
        open_preview(page, base)
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
        open_preview(page, base)
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
        open_preview(page, base)
        page.wait_for_timeout(1200)
        check("исходное состояние: показана первая страница из 121",
              total_line() == "показано 50 из 121", total_line())
        page.evaluate("() => { window.__supCalls = []; window.__supDelayMs = 1500;"
                      " window.__supDelayMatch = 'offset=50'; }")
        page.click("#sup-more")               # уходит МЕДЛЕННЫЙ запрос
        page.wait_for_timeout(200)
        page.evaluate("() => { const b = [...document.querySelectorAll("
                      "'#sup-queues button, #sup-sheets button')]"
                      ".find(x => x.getAttribute('data-label') === 'Ошибки');"
                      " if (b) b.click(); }")
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
        open_preview(page, base)
        page.wait_for_timeout(1200)
        page.evaluate("(name) => { const b = [...document.querySelectorAll("
                      "'#sup-queues button, #sup-sheets button')]"
                      ".find(x => x.getAttribute('data-label') === name);"
                      " if (b) b.click(); }", SHEET_A)
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
        # Настроенный источник показан компактно, поэтому имена листов
        # правятся через явное раскрытие формы — тем же путём, каким это
        # делает человек.
        page.click("#sup-edit")
        page.wait_for_timeout(200)
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
              page.evaluate("() => { const on = document.querySelector("
                            "'#sup-queues button[aria-pressed=\"true\"]');"
                            " return on ? on.getAttribute('data-label') : ''; }")
              == "Все строки")

        print("\n== Неудачное обновление фильтр и строки оставляет как есть ==")
        page.evaluate("(name) => { const b = [...document.querySelectorAll("
                      "'#sup-queues button, #sup-sheets button')]"
                      ".find(x => x.getAttribute('data-label') === name);"
                      " if (b) b.click(); }", SHEET_C)
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
        open_preview(page, base)
        page.wait_for_timeout(1200)
        summary = page.text_content("#sup-summary") or ""
        # Неполнота названа РЯДОМ С ЧИСЛОМ и числом же: сказано, у скольких
        # строк количество прочитать не удалось, и прямым текстом — что это не
        # весь объём поставки. Прежняя редакция писала «не определено» вместо
        # суммы: прочитанные 9 шт при этом пропадали с экрана совсем, хотя они
        # прочитаны честно и человеку нужны.
        check("сумма распознанного показана числом и в штуках",
              "9 шт" in summary, summary[:260])
        check("и тут же сказано, что прочитано НЕ ВСЁ, и у скольких строк",
              "Прочитано не всё: у 1 строки количество прочитать не удалось"
              in summary, summary[:260])
        check("и что это не весь объём поставки",
              "Это не весь объём поставки." in summary, summary[:260])
        check("а само число не названо объёмом поставки",
              "объём поставки" not in summary.split("Это не весь")[0],
              summary[:260])

        write_snapshot([SHEET_A, SHEET_B], [make_row(1, SHEET_A), make_row(2, SHEET_A)])
        open_preview(page, base)
        page.wait_for_timeout(1200)
        summary = page.text_content("#sup-summary") or ""
        check("полный итог показан числом и без предупреждения о неполноте",
              "10 шт" in summary and "Прочитано не всё" not in summary,
              summary[:260])
        check("но и он не назван объёмом поставки: итог источника отдельно",
              "Сумма распознанных размеров." in summary
              and "Итог источника — отдельная колонка" in summary, summary[:260])

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
                "() => document.querySelectorAll('#sup-rows td.sup-id').length")

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
                "'#sup-queues button[aria-pressed=\"true\"][data-kind=\"' + kind"
                " + '\"], #sup-sheets button[aria-pressed=\"true\"]"
                "[data-kind=\"' + kind + '\"]');"
                " return on ? on.getAttribute('data-label') : ''; }", kind)

        def click_chip(label: str) -> None:
            page.evaluate("(name) => { const b = [...document.querySelectorAll("
                          "'#sup-queues button, #sup-sheets button')]"
                          ".find(x => x.getAttribute('data-label') === name);"
                          " if (b) b.click(); }", label)

        print("\n== Отказ при смене очереди не оставляет строк прежней ==")
        page.evaluate("() => { window.__supDelayMs = 0; window.__supDelayMatch = ''; }")
        write_snapshot([SHEET_A, SHEET_B],
                       [make_row(i + 1, SHEET_A) for i in range(120)]
                       + [make_row(121, SHEET_A, invalid=True)])
        open_preview(page, base)
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
        click_chip("Все строки")
        page.wait_for_timeout(900)
        check("вернулись к «Все строки»: строк снова много",
              data_rows() == 50 and active_chip("queue") == "Все строки",
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
        open_preview(page, base)
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
        open_preview(page, base)
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
        click_chip("Все строки")              # B — быстрый и удачный
        page.wait_for_timeout(900)
        check("B успел нарисоваться, пока A ещё в полёте",
              data_rows() == 50 and total_line() == "показано 50 из 121"
              and active_chip("queue") == "Все строки",
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
              active_chip("queue") == "Все строки", active_chip("queue"))
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
        open_preview(page, base)
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
        # «Все строки» третьим переходом: это снова вид с догрузкой, и он тоже не
        # должен быть заперт замком давно снятого A.
        click_chip("Все строки")
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

        open_preview(page, base)
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
        open_preview(page, base)
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
        open_preview(page, base)
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
        open_preview(mpage, base)
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
        open_preview(page, base)
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
        open_preview(m2page, base)
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
                              "'#sup-rows td.sup-id').length") == 3)
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
        open_preview(page, base)
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
            open_preview(page, base)
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
        open_preview(page, base)
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

        # ── 9. Частично испорченная попытка: тройка годна ЦЕЛИКОМ ─────────
        #
        # Замечание ревью PR #47 на HEAD `6a3cabd`. Ссылка попытки и пара имён
        # проверялись и откатывались НЕЗАВИСИМО, и потому из половины одной
        # попытки и половины другой собиралась третья, которой владелец никогда
        # не делал: адрес попытки с листами снимка либо адрес снимка с листами
        # попытки. Повтор отправлял эту выдуманную комбинацию на сервер — то
        # есть страница не просто показывала неправду, она её отправляла.
        print("\n== Частично испорченная попытка не собирается в гибрид ==")
        page.evaluate("() => { window.__supDelayMs = 0; window.__supDelayMatch = '';"
                      " window.__supDelayLimit = -1; }")
        good_url = f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit"
        tried_url = f"https://docs.google.com/spreadsheets/d/{ATTEMPT_SPREADSHEET_ID}/edit"

        def field_of(fid: str) -> str:
            return page.evaluate("(id) => { const n = document.getElementById(id);"
                                 " return n ? n.value : null; }", fid)

        def retry_body() -> str:
            sent = []
            page.route(REFRESH_RE, lambda route: (
                sent.append(route.request.post_data or ""),
                route.fulfill(status=502, content_type="application/json",
                              body='{"detail":"снова не отдал CSV"}')))
            page.click("#sup-refresh")
            page.wait_for_timeout(1600)
            page.unroute(REFRESH_RE)
            return sent[-1] if sent else ""

        def whole_triple_case(label: str, override) -> None:
            """Половина попытки цела — значит не годна ВСЯ тройка."""
            write_snapshot_with_failed_attempt(
                [SHEET_A, SHEET_B],
                [make_row(i + 1, SHEET_A) for i in range(3)],
                [SHEET_C, SHEET_D], source_override=override)
            open_preview(page, base)
            page.wait_for_timeout(1200)
            check(f"{label}: ссылка — от УДАЧНОГО снимка целиком",
                  field_of("sup-url") == good_url, str(field_of("sup-url")))
            check(f"{label}: и оба имени листов — тоже его",
                  field_of("sup-cur") == SHEET_A and field_of("sup-next") == SHEET_B,
                  f"{field_of('sup-cur')} | {field_of('sup-next')}")
            body = retry_body()
            check(f"{label}: повтор отправил ТОЛЬКО значения снимка",
                  SPREADSHEET_ID in body and SHEET_A in body and SHEET_B in body,
                  body[:200])
            check(f"{label}: и ни одной части попытки в теле нет",
                  ATTEMPT_SPREADSHEET_ID not in body
                  and SHEET_C not in body and SHEET_D not in body, body[:200])
            check(f"{label}: строки прежнего снимка на экране целы",
                  data_rows() == 3, str(data_rows()))

        # Направление 1: адрес попытки ЦЕЛ, а имён не пара — было бы
        # «адрес попытки + листы снимка».
        whole_triple_case("одно имя вместо двух",
                          {"spreadsheet_id": ATTEMPT_SPREADSHEET_ID,
                           "sheet_names": [SHEET_C]})
        # Направление 2: имена попытки целы, а адреса нет — было бы
        # «адрес снимка + листы попытки».
        whole_triple_case("нет идентификатора таблицы",
                          {"spreadsheet_id": "", "sheet_names": [SHEET_C, SHEET_D]})
        # Направление 3: адрес есть, но НЕ канонический (идентификатор испорчен
        # так, что сегодняшнюю проверку он бы не прошёл), имена целы.
        whole_triple_case("неканонический идентификатор",
                          {"spreadsheet_id": "не идентификатор/../a",
                           "sheet_names": [SHEET_C, SHEET_D]})
        # И контроль сверху: ЦЕЛАЯ тройка по-прежнему работает целиком.
        write_snapshot_with_failed_attempt(
            [SHEET_A, SHEET_B], [make_row(i + 1, SHEET_A) for i in range(3)],
            [SHEET_C, SHEET_D])
        open_preview(page, base)
        page.wait_for_timeout(1200)
        check("целая тройка: поля от попытки",
              field_of("sup-url") == tried_url
              and field_of("sup-cur") == SHEET_C
              and field_of("sup-next") == SHEET_D,
              f"{field_of('sup-url')} {field_of('sup-cur')} {field_of('sup-next')}")
        body = retry_body()
        check("и повтор отправляет попытку целиком, без примеси снимка",
              ATTEMPT_SPREADSHEET_ID in body and SHEET_C in body and SHEET_D in body
              and SPREADSHEET_ID not in body and SHEET_A not in body, body[:200])

        # ── 10. Цена источника видна в строке ─────────────────────────────
        #
        # Замечание ревью PR #47 на HEAD `6a3cabd` (thread r3903352252).
        # Парсер признаёт колонку 16 как `price_raw` и ИМЕННО ПОЭТОМУ исключает
        # её из `unknown_raw` — то есть страховки «покажется как неизвестная
        # колонка» у неё нет. А строка её не выводила вовсе: значение,
        # написанное человеком в его таблице, исчезало из предпросмотра
        # бесследно.
        #
        # SUPPLY-UX-1.1 перенесла цену из колонки таблицы в подробности строки:
        # свободный текст источника уплотнённую строку и разваливал. Требование
        # ревью от этого не меняется — цена обязана быть НАЗВАНА, лежать РОВНО
        # в своей строке и оставаться СЫРОЙ. Проверяется здесь именно это, а не
        # факт существования колонки.
        print("\n== Цена источника: видна, сырая и безопасная ==")
        xss = '<img src=x onerror=alert(1)>'
        write_snapshot([SHEET_A, SHEET_B], [
            make_row(1, SHEET_A, price="12 900"),
            make_row(2, SHEET_A),
            make_row(3, SHEET_A, price=xss),
        ])
        open_preview(page, base)
        page.wait_for_timeout(1200)

        def price_cells() -> list:
            """Цена каждой строки — из ЕЁ подробностей, в порядке строк.

            `null` означает «поле не названо вовсе»: именно это и было
            исходным дефектом, поэтому пропускать такие панели нельзя."""
            return page.evaluate(
                "() => [...document.querySelectorAll('#sup-rows tr.sup-det')]"
                ".map(tr => { const dt = [...tr.querySelectorAll('dt')]"
                ".find(d => d.textContent === 'Цена источника');"
                " return dt ? dt.nextElementSibling.textContent : null; })")

        cells = price_cells()

        def cell_at(i):
            """Отсутствующая ячейка обязана дать FAIL, а не уронить сценарий:
            набор без строки `ИТОГО` раннер засчитывает как «нет отчёта»."""
            return cells[i] if i < len(cells) else None

        check("цена названа у КАЖДОЙ строки данных, а не у той, где она есть",
              len(cells) == 3 and all(v is not None for v in cells), str(cells))
        check("цена стоит РОВНО в своей строке и как есть",
              (cell_at(0) or "").startswith("12 900 —"), str(cells)[:200])
        check("и не выдаётся за рубли или за цену штуки",
              "без валюты и без перевода в рубли" in (cell_at(0) or ""),
              str(cell_at(0))[:160])
        check("строка без цены показывает честный прочерк, а не пустоту",
              cell_at(1) == "—", str(cells)[:200])
        check("цена — сырой текст, разметкой она не становится",
              (cell_at(2) or "").startswith(xss)
              and page.evaluate("() => document.querySelectorAll("
                                "'#sup-rows img').length") == 0,
              str(cell_at(2))[:160])

        # Геометрия таблицы: шапка, строка данных, разделитель и заглушка —
        # одна и та же ширина. Иначе колонка «поедет» у части строк.
        cols = page.evaluate(
            "() => document.querySelectorAll('table.sup-tbl thead th').length")
        check("колонок в шапке столько, сколько их задумано",
              cols == 11, str(cols))
        check("ширина строки данных совпадает с шапкой",
              page.evaluate("() => { const tr = [...document.querySelectorAll("
                            "'#sup-rows tr:not(.sup-det)')]"
                            ".find(t => t.querySelector('td.sup-pos'));"
                            " return tr ? [...tr.children].reduce((n, td) =>"
                            " n + (td.colSpan || 1), 0) : -1; }") == cols, str(cols))
        check("и панель подробностей занимает всю ширину, а не часть",
              page.evaluate("() => { const tr = document.querySelector("
                            "'#sup-rows tr.sup-det');"
                            " return tr ? [...tr.children].reduce((n, td) =>"
                            " n + (td.colSpan || 1), 0) : -1; }") == cols, str(cols))
        write_snapshot([SHEET_A, SHEET_B], [
            make_row(1, SHEET_A, price="12 900"),
            {**make_row(2, SHEET_A), "is_blank": True},
        ])
        open_preview(page, base)
        page.wait_for_timeout(1200)
        check("и ширина строки-разделителя тоже",
              page.evaluate("() => { const tr = document.querySelector("
                            "'#sup-rows tr.r-blank');"
                            " return tr ? [...tr.children].reduce((n, td) =>"
                            " n + (td.colSpan || 1), 0) : -1; }") == cols, str(cols))
        page.route(GET_RE, lambda route: route.fulfill(
            status=502, content_type="application/json",
            body='{"detail":"чтение снимка не удалось"}'))
        click_chip("Ошибки")
        page.wait_for_timeout(1200)
        check("и ширина заглушки с причиной отказа",
              page.evaluate("() => { const tr = document.querySelector('#sup-rows tr');"
                            " return tr ? [...tr.children].reduce((n, td) =>"
                            " n + (td.colSpan || 1), 0) : -1; }") == cols, str(cols))
        page.unroute(GET_RE)
        click_chip("Все строки")
        page.wait_for_timeout(1200)

        # ── 11. Гонка версий снимка при догрузке ──────────────────────────
        #
        # Замечание ревью PR #47 на HEAD `6a3cabd` (thread r3903352240).
        # Пока человек смотрит первую страницу, снимок могли обновить в другой
        # вкладке или руками владельца. Догрузка проверяла только поколение
        # ЭКРАНА — а оно не менялось, потому что фильтр тот же. В результате
        # строки НОВОЙ версии дописывались к строкам СТАРОЙ, а сводка и счётчик
        # брались от новой: одна таблица, собранная из двух источников правды.
        print("\n== Догрузка не смешивает две версии снимка ==")
        H1, H2 = "1" * 64, "2" * 64
        write_snapshot([SHEET_A, SHEET_B],
                       [make_row(i + 1, SHEET_A) for i in range(120)], H1)
        open_preview(page, base)
        page.wait_for_timeout(1200)

        def rows_of_sheet(name: str) -> int:
            return page.evaluate(
                "(s) => [...document.querySelectorAll('#sup-rows td.sup-id')]"
                ".filter(td => td.textContent.indexOf(s) === 0).length", name)

        check("исходно на экране первая страница версии H1",
              data_rows() == 50 and total_line() == "показано 50 из 120"
              and rows_of_sheet(SHEET_A) == 50,
              f"{data_rows()} {total_line()} {rows_of_sheet(SHEET_A)}")

        # Догрузка тормозится, и РОВНО В ЭТОТ момент снимок подменяется на
        # другую версию: другой хеш, другие листы, другое число строк.
        page.evaluate("() => { window.__supCalls = []; window.__supDelayMs = 2500;"
                      " window.__supDelayMatch = 'offset=50';"
                      " window.__supDelayLimit = 1; }")
        page.click("#sup-more")
        page.wait_for_timeout(300)
        write_snapshot([SHEET_C, SHEET_D],
                       [make_row(i + 1, SHEET_C) for i in range(60)], H2)
        page.wait_for_timeout(4000)

        check("ни одной строки прежней версии на экране не осталось",
              rows_of_sheet(SHEET_A) == 0, str(rows_of_sheet(SHEET_A)))
        check("показана согласованная новая версия с начала",
              data_rows() == 50 and rows_of_sheet(SHEET_C) == 50
              and total_line() == "показано 50 из 60",
              f"{data_rows()} {rows_of_sheet(SHEET_C)} {total_line()}")
        check("сводка тоже от новой версии, а не от смеси",
              "60" in (page.text_content("#sup-summary") or ""),
              (page.text_content("#sup-summary") or "")[:200])
        after = get_calls()
        check("перезапуск сходил ровно за началом нового вида",
              after and "offset=0" in after[-1], str(after[-2:]))
        check("и цикла не случилось — запросов немного",
              len(after) <= 4, str(after))
        check("кнопка догрузки жива и включена",
              page.evaluate("() => { const b = document.getElementById('sup-more');"
                            " return b.style.display !== 'none' && !b.disabled; }")
              is True)

        # Замок догрузки не заклинил: следующая страница НОВОЙ версии грузится.
        page.evaluate("() => { window.__supCalls = []; window.__supDelayMs = 0;"
                      " window.__supDelayMatch = ''; window.__supDelayLimit = -1; }")
        page.click("#sup-more")
        page.wait_for_timeout(1500)
        check("следующая страница новой версии дозагрузилась",
              data_rows() == 60 and total_line() == "показано 60 из 60"
              and rows_of_sheet(SHEET_A) == 0,
              f"{data_rows()} {total_line()} {rows_of_sheet(SHEET_A)}")

        # ── 12. SUPPLY-UX-1.1: плотная строка и полный текст в подробностях ─
        #
        # Замер прежнего экрана на этой же фикстуре: первая строка занимала
        # 323 px на 1440×756 и 293 px на 390 px — одна позиция съедала половину
        # экрана, а колонка «Цвет» переносилась по одной букве. Проверяется
        # поэтому не «выглядит лучше», а два ЧИСЛА и одно свойство: высота
        # строки, ширина размерных колонок и сохранность исходного текста.
        print("\n== Плотная строка: высота ограничена, текст никуда не делся ==")
        page.unroute(GET_RE)
        dense = [make_long_row(3, SHEET_A)]
        dense += [make_row(i + 4, SHEET_A) for i in range(70)]
        dense.append(make_row(80, SHEET_B, name="Пуховик НГ"))
        write_snapshot([SHEET_A, SHEET_B], dense, content_sha256="d" * 64)
        page.set_viewport_size({"width": 1440, "height": 756})
        open_preview(page, base)
        page.wait_for_timeout(1500)

        heights = page.evaluate(
            "() => [...document.querySelectorAll('#sup-rows tr:not(.sup-det)')]"
            ".map(tr => Math.round(tr.getBoundingClientRect().height))")
        check("каждая строка осталась строкой: не выше 72 px",
              heights and max(heights) <= 72, f"max={max(heights or [0])} {heights[:4]}")
        check("и не схлопнулась: не ниже 36 px",
              heights and min(heights) >= 36, f"min={min(heights or [0])}")

        widths = page.evaluate(
            "() => [...document.querySelectorAll('#sup-rows tr:not(.sup-det)')]"
            ".map(tr => [...tr.querySelectorAll('td.sz')]"
            ".map(td => Math.round(td.getBoundingClientRect().width)).join(','))")
        check("размерные колонки одинаковой ширины во ВСЕХ строках",
              len(set(widths)) == 1 and widths[0].count(",") == 4,
              str(sorted(set(widths))[:3]))

        first_row_text = page.evaluate(
            "() => { const tr = document.querySelector('#sup-rows tr:not(.sup-det)');"
            " return tr ? tr.textContent : ''; }")
        check("свободного текста источника в плотной строке нет",
              LONG_STATUS not in first_row_text
              and LONG_ADDRESS not in first_row_text
              and LONG_COMPONENTS not in first_row_text
              and LONG_UNKNOWN not in first_row_text,
              first_row_text[:160])
        check("но ошибка названа СЛОВОМ, а не только цветом строки",
              "не число" in first_row_text or "итог ≠ сумма" in first_row_text,
              first_row_text[:160])

        # ── 13. Раскрытие подробностей — кнопкой и С КЛАВИАТУРЫ ────────────
        print("\n== Подробности: доступны с клавиатуры, текст полный ==")
        det_state = ("() => { const b = document.querySelector("
                     "'#sup-rows tr:not(.sup-det) .sup-act button');"
                     " const p = document.getElementById(b.getAttribute('aria-controls'));"
                     " return {expanded: b.getAttribute('aria-expanded'),"
                     " label: b.textContent, hidden: p.hidden, text: p.textContent}; }")
        before_open = page.evaluate(det_state)
        check("до раскрытия панель скрыта и кнопка говорит об этом",
              before_open["expanded"] == "false" and before_open["hidden"] is True
              and "Подробнее" in before_open["label"], str(before_open)[:140])

        # Клавиатура: фокус на кнопку и Enter. Не `click()` — проверяется
        # именно то, что человек без мыши до подробностей доберётся.
        page.evaluate("() => document.querySelector("
                      "'#sup-rows tr:not(.sup-det) .sup-act button').focus()")
        page.keyboard.press("Enter")
        page.wait_for_timeout(250)
        after_open = page.evaluate(det_state)
        check("Enter на кнопке раскрывает подробности",
              after_open["expanded"] == "true" and after_open["hidden"] is False,
              str({k: after_open[k] for k in ("expanded", "hidden")}))
        check("и подпись кнопки описывает новое состояние",
              "Свернуть" in after_open["label"], after_open["label"])

        panel = after_open["text"]
        missing = [name for name, value in (
            ("наименование", LONG_NAME), ("цвет", LONG_COLOR),
            ("статус", LONG_STATUS), ("адрес", LONG_ADDRESS),
            ("комплектующие", LONG_COMPONENTS), ("чужая колонка", LONG_UNKNOWN),
            ("текст в размере", RAW_SIZE), ("цена", "12 900"),
        ) if value not in panel]
        check("в подробностях лежит ВЕСЬ исходный текст строки, целиком",
              not missing, "нет: " + ", ".join(missing))
        check("и там же сказано, где эта строка в источнике",
              f"Лист «{SHEET_A}», строка 3" in panel, panel[:160])
        check("и объяснено, что именно не прочитано",
              "Ноль вместо него не подставляется" in panel, panel[:200])
        check("цена показана сырой и не названа рублями за штуку",
              "без валюты и без перевода в рубли" in panel, panel[:200])

        page.keyboard.press("Enter")
        page.wait_for_timeout(250)
        closed = page.evaluate(det_state)
        check("повторный Enter сворачивает панель обратно",
              closed["expanded"] == "false" and closed["hidden"] is True,
              str({k: closed[k] for k in ("expanded", "hidden")}))

        # ── 14. Поиск в браузере: по всему набору, а не по загруженным 50 ──
        print("\n== Поиск на странице: считает весь набор и догружает его же ==")
        page.evaluate("() => { window.__supCalls = []; }")
        page.fill("#sup-q", "Позиция")
        page.wait_for_timeout(1400)
        check("запрос ушёл на сервер параметром q",
              any("q=" in u for u in get_calls()), str(get_calls()[-1:]))
        check("счётчик считает найденное по ВСЕМУ снимку, а не по странице",
              total_line() == "показано 50 из 70, найденных по запросу «Позиция»",
              total_line())
        check("и показана ровно страница найденного", data_rows() == 50,
              str(data_rows()))
        check("поле поиска сохранило и текст, и фокус",
              page.evaluate("() => document.activeElement === "
                            "document.getElementById('sup-q')") is True
              and page.input_value("#sup-q") == "Позиция")

        page.click("#sup-more")
        page.wait_for_timeout(1400)
        check("догрузка идёт по результату поиска, а не по всему снимку",
              data_rows() == 70
              and total_line() == "показано 70 из 70, найденных по запросу «Позиция»",
              f"{data_rows()} {total_line()}")
        check("и ни одной чужой строки в результат не попало",
              page.evaluate(
                  "() => [...document.querySelectorAll('#sup-rows tr:not(.sup-det)"
                  " .sup-pos .nm')].every(n => n.textContent.indexOf('Позиция') === 0)")
              is True)

        page.click("#sup-q-clear")
        page.wait_for_timeout(1200)
        check("сброс поиска возвращает весь снимок",
              total_line() == "показано 50 из 72" and page.input_value("#sup-q") == "",
              f"{total_line()} / {page.input_value('#sup-q')!r}")

        # ── 15. Отказ 400 не отбирает у человека ввод и фокус ──────────────
        #
        # Настоящий серверный 400, а не подменённый ответ: лист выбран, а снимок
        # тем временем перечитан с ДРУГИМИ листами — ровно то, что случается,
        # когда владелец обновил источник в соседней вкладке.
        print("\n== 400 на запросе: ввод и фокус остаются у человека ==")
        click_chip(SHEET_A)
        page.wait_for_timeout(900)
        write_snapshot([SHEET_C, SHEET_D],
                       [make_row(i + 3, SHEET_C) for i in range(4)],
                       content_sha256="e" * 64)
        page.focus("#sup-q")
        page.type("#sup-q", "Позиция")
        page.wait_for_timeout(1400)
        check("сервер действительно отказал по устаревшему листу",
              any("sheet=" in u for u in get_calls()), str(get_calls()[-1:]))
        check("введённый запрос никуда не делся",
              page.input_value("#sup-q") == "Позиция", page.input_value("#sup-q"))
        check("и фокус остался в поле поиска",
              page.evaluate("() => document.activeElement === "
                            "document.getElementById('sup-q')") is True)
        check("а на месте строк — причина и кнопка повтора, а не пустота",
              "Не удалось загрузить строки" in (page.text_content("#sup-rows") or "")
              and page.evaluate("() => !!document.getElementById('sup-retry')") is True,
              (page.text_content("#sup-rows") or "")[:140])

        # ── 16. Настроенный источник компактен, форма — за явным действием ─
        print("\n== Источник: компактная строка, длинная форма по кнопке ==")
        write_snapshot([SHEET_A, SHEET_B], dense, content_sha256="f" * 64)
        open_preview(page, base)
        page.wait_for_timeout(1500)
        check("у настроенного источника длинной формы на экране нет",
              page.evaluate("() => document.getElementById('sup-form-wrap').hidden")
              is True)
        bar = page.text_content("#sup-srcbar") or ""
        check("но видно главное: источник, оба листа и время снимка",
              "Google Sheets" in bar and SHEET_A in bar and SHEET_B in bar
              and "Снимок таблицы от" in bar, bar[:160])
        check("и кнопка обновления на месте",
              page.evaluate("() => !!document.getElementById('sup-refresh')") is True)
        check("свежесть снимка подписана источником, а не безымянным «обновлено»",
              "Таблица Google Sheets читается только по кнопке"
              in (page.text_content("#sup-state") or ""),
              (page.text_content("#sup-state") or "")[:120])
        check("а пилюля в шапке называет СВОЙ источник — их два и они разные",
              "Демо-данные" in (page.text_content("#live-pill") or ""),
              (page.text_content("#live-pill") or "")[:80])

        check("кнопка раскрытия честно объявляет состояние",
              page.get_attribute("#sup-edit", "aria-expanded") == "false")
        page.click("#sup-edit")
        page.wait_for_timeout(250)
        check("явное действие раскрывает длинную форму",
              page.evaluate("() => document.getElementById('sup-form-wrap').hidden")
              is False
              and page.get_attribute("#sup-edit", "aria-expanded") == "true")
        check("и фокус уезжает в первое поле, а не остаётся на кнопке",
              page.evaluate("() => document.activeElement === "
                            "document.getElementById('sup-url')") is True)
        check("в полях стоит сохранённая связь, а не пустота",
              page.input_value("#sup-url").endswith(f"{SPREADSHEET_ID}/edit")
              and page.input_value("#sup-cur") == SHEET_A
              and page.input_value("#sup-next") == SHEET_B,
              f"{page.input_value('#sup-url')} {page.input_value('#sup-cur')}")

        # Открытую человеком форму не захлопывает следующий успешный GET:
        # состояние принадлежит тому, кто его выбрал.
        click_chip("Ошибки")
        page.wait_for_timeout(1200)
        check("свой выбор переживает перерисовку экрана",
              page.evaluate("() => document.getElementById('sup-form-wrap').hidden")
              is False)
        click_chip("Все строки")
        page.wait_for_timeout(1200)

        # ── 17. Две ширины: 1440×756 и 390 px ──────────────────────────────
        print("\n== Desktop 1440×756 и mobile 390: шапка и идентичность на месте ==")
        for label, size in (("desktop", {"width": 1440, "height": 756}),
                            ("mobile", {"width": 390, "height": 844})):
            page.set_viewport_size(size)
            open_preview(page, base)
            page.wait_for_timeout(1500)

            geom = page.evaluate(
                "() => { const w = document.querySelector('.sup-scroll');"
                " const t = document.querySelector('table.sup-tbl');"
                " return {wrap: Math.round(w.clientWidth),"
                " table: Math.round(t.scrollWidth)}; }")
            if label == "desktop":
                check("desktop: таблица помещается по ширине без прокрутки вбок",
                      geom["table"] <= geom["wrap"] + 1, str(geom))

            stuck = page.evaluate(
                "() => { const w = document.querySelector('.sup-scroll');"
                " w.scrollTop = 240; w.scrollLeft = 260;"
                " const th = w.querySelector('thead th');"
                " const td = w.querySelector('#sup-rows tr:not(.sup-det) td.sup-id');"
                " return {head: Math.round(th.getBoundingClientRect().top"
                "                          - w.getBoundingClientRect().top),"
                " ident: Math.round(td.getBoundingClientRect().left"
                "                   - w.getBoundingClientRect().left),"
                " identText: td.textContent}; }")
            check(f"{label}: шапка таблицы осталась наверху при прокрутке",
                  abs(stuck["head"]) <= 2, str(stuck))
            check(f"{label}: идентичность строки осталась слева",
                  abs(stuck["ident"]) <= 2 and SHEET_A in stuck["identText"],
                  str(stuck))

            visible = page.evaluate(
                "() => { const ok = id => { const e = document.getElementById(id);"
                " if (!e) return false; const r = e.getBoundingClientRect();"
                " return r.width > 0 && r.height > 0; };"
                " return {q: ok('sup-q'), refresh: ok('sup-refresh'),"
                " edit: ok('sup-edit'), more: ok('sup-more'),"
                " queues: document.querySelectorAll('#sup-queues button').length,"
                " sheets: document.querySelectorAll('#sup-sheets button').length}; }")
            check(f"{label}: поиск, обновление и обе группы фильтров на месте",
                  visible["q"] and visible["refresh"] and visible["edit"]
                  and visible["queues"] == 3 and visible["sheets"] == 3,
                  str(visible))

            groups = page.evaluate(
                "() => [...document.querySelectorAll('.sup-grp-lbl')]"
                ".map(x => x.textContent)")
            check(f"{label}: у каждой группы фильтров есть подпись",
                  any("Что показать" in g for g in groups)
                  and any("Лист источника" in g for g in groups)
                  and any("Поиск" in g for g in groups), str(groups))

            # ПУСТОТА, А НЕ ДЛИНА. Подводка к строкам — назначение, источник,
            # сводка, фильтры — это содержание, и на телефоне она занимает
            # больше экрана, чем на мониторе; требовать «всё в один экран»
            # значило бы требовать выкинуть текст. Проверяется поэтому другое:
            # между блоками нет провалов, подводка ЗАПОЛНЕНА содержанием, а её
            # общая высота имеет потолок — иначе следующая правка добавит
            # экран подводки и никто этого не заметит.
            void = page.evaluate(
                "() => { const t = document.querySelector('.sup-scroll')"
                ".getBoundingClientRect().top;"
                # Скрытая карточка соседнего раздела подводкой не является:
                # у неё нулевая геометрия и top=0, и без фильтра по высоте она
                # считалась бы «провалом» во всю высоту экрана.
                " const cards = [...document.querySelectorAll("
                "'.sup-wrap section.card')].map(c => c.getBoundingClientRect())"
                ".filter(r => r.height > 0 && r.top < t);"
                " let filled = 0, prev = null, maxGap = 0;"
                " cards.forEach(r => { filled += Math.min(r.height, t - r.top);"
                "   if (prev !== null) maxGap = Math.max(maxGap, r.top - prev);"
                "   prev = r.bottom; });"
                " const from = cards.length ? cards[0].top : t;"
                " return {top: Math.round(t), span: Math.round(t - from),"
                "  filled: Math.round(filled), maxGap: Math.round(maxGap)}; }")
            check(f"{label}: между блоками подводки нет провалов",
                  void["maxGap"] <= 24, f"наибольший зазор {void['maxGap']} px")
            check(f"{label}: подводка занята содержанием, а не пустотой",
                  void["filled"] >= 0.85 * void["span"],
                  f"{void['filled']} px содержания на {void['span']} px подводки")
            # Потолок взят от замера: 1437 px на телефоне и 700 px на мониторе
            # (замер 03.09 на этой же фикстуре, до правки было 1610 px).
            ceiling = 1600 if label == "mobile" else 900
            check(f"{label}: подводка к строкам не разрослась",
                  void["top"] <= ceiling,
                  f"верх таблицы на {void['top']} px при потолке {ceiling}")

        page.set_viewport_size({"width": 1400, "height": 900})

        # ── 18. SUPPLY-3: план производства в браузере ─────────────────────
        #
        # Проверяется путь человека целиком, а не наличие разметки: пустое
        # состояние → материал → новинка с эскизом → плановая партия →
        # назначение → сводка и следующий шаг. Отдельно — то, что ломается чаще
        # всего: отказ сохранения, двойной клик и узкий экран.
        print("\n== План производства: сквозной путь в браузере ==")
        page.set_viewport_size({"width": 1440, "height": 900})
        page.goto(f"{base}/supply")
        page.wait_for_timeout(1200)

        check("по умолчанию открыт раздел плана, а не чужая таблица",
              page.get_attribute("#sup-tab-plan", "aria-selected") == "true"
              and page.evaluate("() => document.getElementById('sup-view-plan').hidden")
              is False
              and page.evaluate("() => document.getElementById('sup-view-preview').hidden")
              is True)
        check("старый предпросмотр при этом доступен одним нажатием",
              page.evaluate("() => !!document.getElementById('sup-tab-preview')") is True)
        check("плановая партия названа плановой прямо в назначении раздела",
              "не заказ" in (page.text_content("#pl-note") or ""),
              (page.text_content("#pl-note") or "")[:90])

        check("пустое состояние предлагает начать с материала",
              page.is_visible("#pl-mat-empty")
              and "Начните с материала" in (page.text_content("#pl-next") or ""),
              (page.text_content("#pl-next") or "")[:80])

        # Материал: раскрытие формы явным действием, сохранение, результат.
        check("форма материала закрыта, пока её не открыли",
              page.evaluate("() => document.getElementById('pl-mat-form').hidden") is True
              and page.get_attribute("#pl-add-material", "aria-expanded") == "false")
        page.click("#pl-add-material")
        page.wait_for_timeout(200)
        check("явное действие раскрывает форму и уводит фокус в первое поле",
              page.evaluate("() => document.getElementById('pl-mat-form').hidden") is False
              and page.evaluate("() => document.activeElement.id") == "pl-mat-title",
              page.evaluate("() => document.activeElement.id"))
        page.fill("#pl-mat-title", "Ткань костюмная")
        page.fill("#pl-mat-qty", "100")
        page.fill("#pl-mat-note", "счёт 42")
        page.click("#pl-mat-form button[type=submit]")
        page.wait_for_timeout(1200)
        check("материал появился на экране с числами",
              "Ткань костюмная" in (page.text_content("#pl-materials") or "")
              and "100" in (page.text_content("#pl-materials") or ""),
              (page.text_content("#pl-materials") or "")[:120])
        check("и форма закрылась сама — работа сделана",
              page.evaluate("() => document.getElementById('pl-mat-form').hidden") is True)
        check("следующий шаг сменился на выбор вещи",
              "вещь" in (page.text_content("#pl-next") or "").lower(),
              (page.text_content("#pl-next") or "")[:80])

        # Новинка с эскизом: файл собирается прямо в браузере, чтобы набор не
        # тащил бинарник в репозиторий.
        page.click("#pl-add-item")
        page.wait_for_timeout(200)
        page.select_option("#pl-item-kind", "draft")
        page.fill("#pl-item-title", "Новинка Б")
        page.evaluate("""() => {
            const png = new Uint8Array([137,80,78,71,13,10,26,10,
              0,0,0,13,73,72,68,82, 0,0,0,4, 0,0,0,3, 8,2,0,0,0, 214,111,120,131,
              0,0,0,22,73,68,65,84, 120,156,99,248,207,192,240,31,4,3,3,3,0,
              47,224,5,251, 27,132,73,157,
              0,0,0,0,73,69,78,68,174,66,96,130]);
            const file = new File([png], 'sketch.png', {type: 'image/png'});
            const dt = new DataTransfer();
            dt.items.add(file);
            document.getElementById('pl-item-sketch').files = dt.files;
        }""")
        page.click("#pl-item-form button[type=submit]")
        page.wait_for_timeout(1500)
        check("новинка создана", page.evaluate(
            "() => document.getElementById('pl-item-form').hidden") is True)

        # Плановая партия и назначение метража.
        page.click("#pl-add-batch")
        page.wait_for_timeout(200)
        page.fill("#pl-batch-title", "Партия А")
        page.fill("#pl-batch-qty", "30")
        page.select_option("#pl-batch-due-kind", "approx")
        page.fill("#pl-batch-due-text", "к середине ноября")
        page.fill("#pl-batch-due-source", "цех")
        page.click("#pl-batch-form button[type=submit]")
        page.wait_for_timeout(1300)
        batches_text = page.text_content("#pl-batches") or ""
        check("партия показана и НАЗВАНА плановой",
              "Партия А" in batches_text and "плановая партия" in batches_text,
              batches_text[:120])
        check("срок показан ориентиром вместе с источником, а не датой",
              "ориентировочно к середине ноября" in batches_text
              and "цех" in batches_text, batches_text[:200])
        check("план изделий показан в штуках",
              "30 шт" in batches_text, batches_text[:160])
        # Эскиз ищется ПОСЛЕ создания партии: он показывается в карточке
        # партии, и до неё показывать его негде.
        sketch_src = page.evaluate(
            "() => { const i = document.querySelector('#pl-batches img.pl-sketch');"
            " return i ? i.getAttribute('src') : ''; }")
        check("эскиз новинки виден на экране и адресуется приватной ручкой",
              sketch_src.startswith("/api/supply/planning/sketches/"), sketch_src[:60])

        page.click("#pl-materials .pl-actions button")
        page.wait_for_timeout(300)
        inline = page.query_selector("#pl-materials .pl-form.inline")
        check("назначение раскрывается прямо в карточке материала",
              inline is not None)
        page.fill("#pl-materials .pl-form.inline input", "40")
        page.click("#pl-materials .pl-form.inline button[type=submit]")
        page.wait_for_timeout(1300)
        mats = page.text_content("#pl-materials") or ""
        check("после назначения видно назначенное и свободное",
              "назначено:" in mats and "свободно:" in mats and "60" in mats,
              mats[:160])
        check("и следующий шаг называет остаток числом",
              "60" in (page.text_content("#pl-next") or ""),
              (page.text_content("#pl-next") or "")[:90])

        # ── 19. Отказ сохранения виден, ввод не потерян ────────────────────
        print("\n== Отказ сохранения: сказано словами, ввод на месте ==")
        page.route(re.compile(r"/api/supply/planning/materials$"),
                   lambda route: route.fulfill(
                       status=400, content_type="application/json",
                       body='{"detail":"Сервер отказал: проверьте название."}'))
        page.click("#pl-add-material")
        page.wait_for_timeout(200)
        page.fill("#pl-mat-title", "Проверочная ткань")
        page.fill("#pl-mat-qty", "7")
        page.click("#pl-mat-form button[type=submit]")
        page.wait_for_timeout(900)
        check("причина отказа показана словами, а не молча проглочена",
              "Сервер отказал" in (page.text_content("#pl-mat-err") or ""),
              (page.text_content("#pl-mat-err") or "")[:90])
        check("форма осталась открытой, а введённое — на месте",
              page.evaluate("() => document.getElementById('pl-mat-form').hidden") is False
              and page.input_value("#pl-mat-title") == "Проверочная ткань"
              and page.input_value("#pl-mat-qty") == "7")
        check("и на экране не появилось строки, которой сервер не принял",
              "Проверочная ткань" not in (page.text_content("#pl-materials") or ""))
        page.unroute(re.compile(r"/api/supply/planning/materials$"))

        # Двойной клик по кнопке сохранения не создаёт двух материалов: один
        # запрос в полёте, и `op_id` закрывает случай, когда второй всё же ушёл.
        page.fill("#pl-mat-title", "Ткань один раз")
        page.fill("#pl-mat-qty", "5")
        page.evaluate("""() => {
            const b = document.querySelector('#pl-mat-form button[type=submit]');
            b.click(); b.click();
        }""")
        page.wait_for_timeout(1600)
        count_once = page.evaluate(
            "() => [...document.querySelectorAll('#pl-materials .pl-card .t')]"
            ".filter(n => n.textContent.indexOf('Ткань один раз') === 0).length")
        check("двойной клик создал ровно ОДИН материал", count_once == 1,
              str(count_once))

        # ── 20. Мобильный 390: без обязательной широкой таблицы ────────────
        print("\n== Мобильный экран плана ==")
        page.set_viewport_size({"width": 390, "height": 844})
        page.goto(f"{base}/supply")
        page.wait_for_timeout(1200)
        geom = page.evaluate(
            "() => { const w = document.getElementById('sup-view-plan');"
            " return {doc: document.documentElement.scrollWidth,"
            "         view: window.innerWidth,"
            "         wide: [...w.querySelectorAll('table')].length}; }")
        check("на 390 px раздел не требует горизонтальной прокрутки",
              geom["doc"] <= geom["view"] + 1, str(geom))
        check("и не содержит ни одной обязательной широкой таблицы",
              geom["wide"] == 0, str(geom))
        visible = page.evaluate(
            "() => { const ok = id => { const e = document.getElementById(id);"
            " if (!e) return false; const r = e.getBoundingClientRect();"
            " return r.width > 0 && r.height > 0; };"
            " return {next: ok('pl-next'), mats: ok('pl-materials'),"
            "  add: ok('pl-add-material'), batches: ok('pl-batches')}; }")
        check("следующий шаг, материалы, партии и кнопка добавления на месте",
              all(visible.values()), str(visible))
        check("переключатель разделов доступен и на телефоне",
              page.is_visible("#sup-tab-plan") and page.is_visible("#sup-tab-preview"))
        page.set_viewport_size({"width": 1400, "height": 900})

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
