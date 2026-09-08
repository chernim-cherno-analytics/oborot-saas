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
import base64
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

#: НАСТОЯЩИЙ PNG 4×3 строкой base64: сигнатура, IHDR, IDAT и IEND с ВЕРНЫМИ
#: контрольными суммами. Бинарника в репозитории по-прежнему нет, а картинка
#: теперь действительно картинка — её открывает браузер, а не только принимает
#: наш разбор (он контрольных сумм не проверяет, и подделка проходила мимо).
VALID_PNG_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAQAAAADCAIAAAA7ljmRAAAAEElEQVR4nGNo"
                 "cFCAIwacHADRZwqBZaYHGAAAAABJRU5ErkJggg==")

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


def set_preview_flag(on: bool) -> None:
    """Флаг `settings.supply_sheets_preview` у единственной организации набора."""
    con = sqlite3.connect(DB_PATH)
    try:
        row = con.execute("SELECT id, settings_json FROM orgs"
                          " ORDER BY id LIMIT 1").fetchone()
        if row is None:
            return
        try:
            data = json.loads(row[1] or "{}")
        except ValueError:
            data = {}
        if on:
            data["supply_sheets_preview"] = True
        else:
            data.pop("supply_sheets_preview", None)
        con.execute("UPDATE orgs SET settings_json = ? WHERE id = ?",
                    (json.dumps(data, ensure_ascii=False), row[0]))
        con.commit()
    finally:
        con.close()


def seed_catalog(count: int) -> None:
    """Каталог организации: `count` моделей, среди них «Тренч «Классика»».

    Нужен F-04: поиск имеет смысл проверять только там, где найти можно то,
    до чего в списке из двадцати не дойти. Пишется строками в `products` —
    тем же ключом `base_name`, каким каталог ключуется во всём проекте.
    """
    con = sqlite3.connect(DB_PATH)
    try:
        org_id = con.execute("SELECT id FROM orgs ORDER BY id LIMIT 1").fetchone()[0]
        names = ["Тренч «Классика»"] + [f"Модель {i:03d}" for i in range(count - 1)]
        for i, base in enumerate(names):
            con.execute(
                "INSERT INTO products (org_id, ext_id, base_name, size, category,"
                " sale_price, cost_price, cost_full, supplier, archived, excluded)"
                " VALUES (?,?,?,?,'',0,0,0,'',0,0)",
                (org_id, f"fix1-cat-{i}", base, "44"))
        con.commit()
    finally:
        con.close()


#: Таблицы плана в порядке удаления: от ссылающихся к тем, на кого ссылаются.
_PLAN_TABLES = ("supply_events", "supply_assignments", "supply_batches",
                "supply_items", "supply_materials", "supply_sketches")


def drop_demo_plan(org_name: str) -> None:
    """Снимает синтетический план «Поставок», пришедший с демо-сидом (ТЗ F-26).

    ФИКСТУРА, А НЕ ПРОВЕРКА. Демо нужно этому набору каталогом и историей
    продаж; план, который с пакета 5 приходит вместе с ними, здесь только
    мешает — почти каждый сценарий ниже считает СВОИ карточки и свои строки
    сводки. Состав самого демо проверяется там, где он и живёт
    (`tests/test_supply_planning.py`, `supply_fix_5_checks`), а фикстура о нём
    ничего не утверждает — иначе она доказывала бы саму себя.
    """
    con = sqlite3.connect(DB_PATH)
    try:
        row = con.execute("SELECT id FROM orgs WHERE name = ?", (org_name,)).fetchone()
        if row is None:
            return
        for table in _PLAN_TABLES:
            con.execute(f"DELETE FROM {table} WHERE org_id = ?", (row[0],))
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
    # SUPPLY-FIX-1 (F-08): вкладка предпросмотра теперь за флагом организации.
    # Сценарии предпросмотра обязаны его включить явно — иначе они проверяли бы
    # не предпросмотр, а собственную неудачу на несуществующей кнопке.
    set_preview_flag(True)
    check("демо-данные загружены", c.post("/api/connect/demo").status_code == 200)
    # Демо нужно набору каталогом и историей; его синтетический план «Поставок»
    # (ТЗ F-26) снимается фикстурой — сценарии ниже считают свои карточки.
    drop_demo_plan("Бренд-UI")
    check("фикстура сняла демо-план целиком",
          not c.get("/api/supply/planning").json()["materials"],
          str(c.get("/api/supply/planning").json()["summary"]))

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
        # F-20: дисклеймер стал одной утверждённой фразой. Прежде здесь
        # проверялось слово «не заказ» из старого длинного текста; смысл
        # («это план, и он ничего не двигает») проверяется по новому тексту.
        check("граница раздела названа одной фразой прямо в назначении",
              "Это план" in (page.text_content("#pl-note") or "")
              and "«Едет»" in (page.text_content("#pl-note") or ""),
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
        # ЗДЕСЬ БЫЛА ПОДДЕЛЬНАЯ КАРТИНКА, И ЭТО НАШЛОСЬ ПАКЕТОМ 4. Прежняя
        # редакция собирала PNG побайтно руками, и контрольные суммы обоих
        # блоков в нём были неверны: наш разбор их не проверяет, поэтому файл
        # принимался и хранился, но НИ ОДИН браузер такую картинку не
        # показывает. Пока проверка смотрела только на адрес в `src`, разницы
        # видно не было; проверка «картинка нарисована» (F-23) её обнаружила
        # сразу. Теперь байты берутся из настоящего PNG (base64), и фикстура
        # проверяет продукт, а не саму себя.
        page.evaluate("""(b64) => {
            const raw = atob(b64);
            const png = new Uint8Array(raw.length);
            for (let i = 0; i < raw.length; i++) png[i] = raw.charCodeAt(i);
            const file = new File([png], 'sketch.png', {type: 'image/png'});
            const dt = new DataTransfer();
            dt.items.add(file);
            document.getElementById('pl-item-sketch').files = dt.files;
        }""", VALID_PNG_B64)
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
        # F-20: бейдж «плановая партия» с карточек убран — он стоял на ста
        # процентах строк. Граница раздела осталась на месте, дисклеймером;
        # проверка карточки теперь про саму карточку, а не про бейдж.
        check("партия показана своей строкой",
              "Партия А" in batches_text, batches_text[:120])
        check("а бейджа «плановая партия» на карточках больше нет",
              "плановая партия" not in batches_text, batches_text[:200])
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
        # SUPPLY-FIX-1 (F-07): «распределите остаток» из подсказки убрано —
        # свободный метраж это норма, а не задача, и правило `assign` стояло
        # первым, закрывая собой перерасход и партии без плана и срока. Число
        # остатка человек по-прежнему видит: на карточке материала (проверка
        # выше) и в сводке. Проверяем именно это, а не исчезнувшую подсказку.
        check("остаток назван числом там, где он и есть, — в сводке",
              "60" in (page.text_content("#pl-summary") or ""),
              (page.text_content("#pl-summary") or "")[:120])
        check("а подсказка не выдаёт свободный остаток за следующий шаг",
              "распределите" not in (page.text_content("#pl-next") or "").lower(),
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
        # SUPPLY-FIX-1 (F-07): блок «Следующий шаг» больше не обязан быть
        # видимым ВСЕГДА — когда делать нечего, его на экране нет. Поэтому
        # ожидание сверяется с тем, что ответил сервер, а не с константой
        # «виден». Требовать прежнее «виден всегда» значило бы требовать от
        # экрана ровно того утверждения, которое F-07 убирает.
        code = c.get("/api/supply/planning").json()["next_step"]["code"]
        check("материалы, партии и кнопка добавления на месте",
              visible["mats"] and visible["add"] and visible["batches"],
              str(visible))
        check("а блок следующего шага виден ровно тогда, когда дело есть",
              visible["next"] == (code != "ok"), f"code={code} {visible}")
        check("переключатель разделов доступен и на телефоне",
              page.is_visible("#sup-tab-plan") and page.is_visible("#sup-tab-preview"))
        page.set_viewport_size({"width": 1400, "height": 900})

        # ── 21. Потерянный ответ на УДАВШУЮСЯ запись ───────────────────────
        #
        # Воспроизведение P1 ревью PR #49 (issuecomment-5548612500). Сеть рвётся
        # ПОСЛЕ того, как сервер принял и записал: запрос доходит, ответ — нет.
        # Человек видит отказ и нажимает ещё раз, ничего не изменив. Если экран
        # выдаёт повтор за НОВЫЙ поступок, на сервере окажется две записи вместо
        # одной, и это уже испорченные данные, а не неудобство.
        #
        # `route.fetch()` выполняет настоящий запрос к настоящему серверу, и
        # только потом соединение обрывается: подменять ответ заглушкой здесь
        # нельзя — тогда сервер ничего бы не записал и проверять было бы нечего.
        print("\n== Потерянный ответ: повтор не создаёт вторую запись ==")
        page.set_viewport_size({"width": 1440, "height": 900})
        page.goto(f"{base}/supply")
        page.wait_for_timeout(1200)

        def swallow_response(pattern):
            """Пропустить запрос на сервер и потерять ответ ровно один раз."""
            state = {"done": False}

            def handler(route):
                if state["done"]:
                    route.continue_()
                    return
                state["done"] = True
                try:
                    route.fetch()          # сервер получает и записывает
                except Exception:          # noqa: BLE001 — ответ нам не нужен
                    pass
                route.abort()              # ...а ответ до страницы не доходит

            page.route(pattern, handler)
            return state

        mat_route = re.compile(r"/api/supply/planning/materials$")
        swallow_response(mat_route)
        page.click("#pl-add-material")
        page.wait_for_timeout(200)
        page.fill("#pl-mat-title", "Ткань после обрыва")
        page.fill("#pl-mat-qty", "100")
        page.click("#pl-mat-form button[type=submit]")
        page.wait_for_timeout(1500)
        check("после обрыва человек видит отказ, а не тишину",
              (page.text_content("#pl-mat-err") or "").strip() != "",
              (page.text_content("#pl-mat-err") or "")[:80])
        check("и его ввод остался в форме, чтобы было что повторить",
              page.input_value("#pl-mat-title") == "Ткань после обрыва"
              and page.input_value("#pl-mat-qty") == "100")

        page.click("#pl-mat-form button[type=submit]")
        page.wait_for_timeout(1600)
        page.unroute(mat_route)
        page.goto(f"{base}/supply")
        page.wait_for_timeout(1300)
        after = page.evaluate(
            "() => [...document.querySelectorAll('#pl-materials .pl-card .t')]"
            ".map(n => n.textContent).filter(t => t.indexOf('Ткань после обрыва') === 0)")
        check("повтор НЕ создал вторую запись: материал ровно один",
              len(after) == 1, f"найдено {len(after)}: {after}")
        totals = page.evaluate(
            "() => [...document.querySelectorAll('#pl-materials .pl-card')]"
            ".filter(c => c.textContent.indexOf('Ткань после обрыва') === 0)"
            ".map(c => c.textContent)")
        check("и количество у него одно, а не удвоенное",
              len(totals) == 1 and "100" in totals[0] and "200" not in totals[0],
              str(totals)[:160])

        # Тот же обрыв на НАЗНАЧЕНИИ: там цена ошибки выше, потому что второе
        # назначение молча съедает свободный метраж.
        page.click("#pl-add-item")
        page.wait_for_timeout(200)
        page.select_option("#pl-item-kind", "draft")
        page.fill("#pl-item-title", "Вещь для обрыва")
        page.click("#pl-item-form button[type=submit]")
        page.wait_for_timeout(1300)
        page.click("#pl-add-batch")
        page.wait_for_timeout(200)
        page.fill("#pl-batch-title", "Партия для обрыва")
        page.fill("#pl-batch-qty", "10")
        page.click("#pl-batch-form button[type=submit]")
        page.wait_for_timeout(1300)

        assign_route = re.compile(r"/api/supply/planning/assignments$")
        swallow_response(assign_route)
        target = page.evaluate(
            "() => { const cards = [...document.querySelectorAll('#pl-materials .pl-card')];"
            " const i = cards.findIndex(c => c.textContent.indexOf('Ткань после обрыва') === 0);"
            " return i; }")
        page.evaluate(
            "(i) => document.querySelectorAll('#pl-materials .pl-card')[i]"
            ".querySelector('.pl-actions button').click()", target)
        page.wait_for_timeout(400)
        page.fill("#pl-materials .pl-form.inline input", "40")
        page.click("#pl-materials .pl-form.inline button[type=submit]")
        page.wait_for_timeout(1500)
        check("назначение тоже показало отказ после обрыва",
              (page.text_content("#pl-assign-err") or "").strip() != "",
              (page.text_content("#pl-assign-err") or "")[:80])
        page.click("#pl-materials .pl-form.inline button[type=submit]")
        page.wait_for_timeout(1600)
        page.unroute(assign_route)
        page.goto(f"{base}/supply")
        page.wait_for_timeout(1300)
        card = page.evaluate(
            "() => { const c = [...document.querySelectorAll('#pl-materials .pl-card')]"
            ".find(x => x.textContent.indexOf('Ткань после обрыва') === 0);"
            " return c ? c.textContent : ''; }")
        check("повтор назначения не съел метраж дважды: назначено 40, свободно 60",
              "назначено: 40 м" in card and "свободно: 60 м" in card, card[:200])

        # ── 22. Несовместимые единицы не складываются ──────────────────────
        #
        # Второе воспроизведение того же отчёта. 100 м ткани и 10 кг фурнитуры —
        # это НЕ 110 чего-нибудь. Пересчёта между ними у нас нет и быть не может:
        # коэффициента никто не объявлял.
        print("\n== Метры и килограммы не складываются в одно число ==")
        # Метраж ДО появления килограммов берётся с экрана, а не пишется числом:
        # проверяется свойство «килограммы не меняют метры», и оно не должно
        # краснеть от того, что выше по сценарию добавился ещё один материал.
        before = page.text_content("#pl-summary") or ""
        metres = re.search(r"(\d[\d.,]*)\s*м(?![\wа-я])", before)
        metres = metres.group(1) if metres else None
        page.click("#pl-add-material")
        page.wait_for_timeout(200)
        page.fill("#pl-mat-title", "Фурнитура на вес")
        page.fill("#pl-mat-qty", "10")
        page.select_option("#pl-mat-unit", "кг")
        page.click("#pl-mat-form button[type=submit]")
        page.wait_for_timeout(1400)
        summary_text = page.text_content("#pl-summary") or ""
        # Запрещено не абстрактное «110», а конкретное число, которое появилось
        # бы именно здесь, если сложить метры с килограммами: до правки сводка
        # показывала ровно его.
        glued = float((metres or "0").replace(",", ".")) + 10.0
        glued_forms = [f"{glued:g}", f"{glued:.1f}"]
        check("в сводке нет числа, склеенного из разных единиц",
              metres is not None
              and not any(g in summary_text for g in glued_forms),
              f"склейкой было бы {glued_forms} -> {summary_text[:160]}")
        check("а свободное показано по каждой единице отдельно",
              metres is not None and f"{metres} м" in summary_text
              and "10 кг" in summary_text,
              f"было {metres} м -> стало {summary_text[:200]}")

        check("на странице не было ошибок в консоли", not errors, str(errors)[:200])
        ctx.close()
        browser.close()

        # ── 23. SUPPLY-FIX-1: видимость раздела, hidden, поиск, снятие ──────
        supply_fix_1_ui(pw, base, c)

        # ── 24. SUPPLY-FIX-2: правки, распределение, пометка, удаление ──────
        supply_fix_2_ui(pw, base, c)

        # ── 25. SUPPLY-FIX-3: единицы, формат, тексты, сохранность форм ─────
        supply_fix_3_ui(pw, base, c)

        # ── 26. SUPPLY-FIX-4: порядок отправки эскиза, кэш картинки, история ─
        supply_fix_4_ui(pw, base, c)

        # ── 27. SUPPLY-FIX-5: урок раздела, выгрузка, масштаб и адрес ───────
        supply_fix_5_ui(pw, base)

    c.close()
    print(f"\nИТОГО: {len(PASS)} OK, {len(FAIL)} FAIL")
    for name in FAIL:
        print(f"  FAIL {name}")
    return 1 if FAIL else 0


def close_hint(page) -> None:
    """Закрыть модалку подсказки, если она открылась сама.

    `_hints.html` показывает её при ПЕРВОМ заходе на страницу, и она лежит
    поверх всего (`position: fixed; inset: 0; z-index: 1000`). Для проверок,
    которые кликают по навигации или считают попадания `elementFromPoint`, это
    посторонний слой: он перехватывает нажатия и делает вид, будто кнопка
    перекрыта. Закрываем её так же, как человек, — кнопкой в самой модалке.
    """
    if page.evaluate("() => { const o = document.getElementById('hint-overlay');"
                     " return !!o && o.classList.contains('open'); }"):
        page.click("#hint-close")
        page.wait_for_timeout(200)


def supply_fix_1_ui(pw, base, c) -> None:
    """SUPPLY-FIX-1 в настоящем браузере: F-01…F-06, F-08, F-11.

    Здесь проверяется ПОВЕДЕНИЕ, а не разметка: `getComputedStyle` вместо
    «в шаблоне есть строка», `elementFromPoint` вместо «кнопка в DOM»,
    геометрия вместо «карточка отрисована», тело запроса вместо «поле есть в
    форме».

    КАЖДЫЙ ПУНКТ — ОТДЕЛЬНЫЙ ШАГ, И ЭТО НЕ СТИЛЬ. Прогон против дерева, где
    правки ещё нет, обязан сказать про КАЖДЫЙ пункт, а не умереть на первом же
    отсутствующем узле. Прежняя, линейная редакция этого не умела: на `ea1caff`
    она падала исключением внутри `page.evaluate` в F-01, и девять оставшихся
    пунктов не выполнялись вовсе — то есть их краснота ничем не была
    доказана (D-42: непроведённая проверка не бывает ни зелёной, ни красной).
    Теперь исключение внутри шага превращается в его собственную красную
    строку, а соседние шаги идут своим чередом.
    """
    browser = pw.chromium.launch()
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    ctx.add_cookies([{"name": k, "value": v, "domain": "127.0.0.1", "path": "/"}
                     for k, v in c.cookies.items()])
    errors: list[str] = []
    dialogs: list[str] = []
    page = ctx.new_page()
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("dialog", lambda d: (dialogs.append(d.type), d.dismiss()))

    steps = (
        ("F-01", lambda: _fix1_f01(page, base)),
        ("F-02", lambda: _fix1_f02(page, base)),
        ("F-03", lambda: _fix1_f03(page)),
        ("F-05", lambda: _fix1_f05(page, base, c)),
        ("F-04", lambda: _fix1_f04(page, base, c)),
        ("F-11", lambda: _fix1_f11(page, base, c, dialogs)),
        ("F-07", lambda: _fix1_f07(page, base, c)),
        ("F-08", lambda: _fix1_f08(page, base, errors)),
    )
    for label, run_step in steps:
        try:
            run_step()
        except Exception as exc:  # noqa: BLE001 — важен отчёт, а не тип
            check(f"{label}: шаг дошёл до конца без исключения", False,
                  f"{type(exc).__name__}: "
                  f"{str(exc).strip().splitlines()[0][:160]}")

    check("за весь настольный сценарий не было ошибок в консоли",
          not errors, str(errors)[:200])
    ctx.close()
    try:
        _fix1_f06(browser, base, c)
    except Exception as exc:  # noqa: BLE001
        check("F-06: шаг дошёл до конца без исключения", False,
              f"{type(exc).__name__}: {str(exc).strip().splitlines()[0][:160]}")
    browser.close()


def _fix1_f01(page, base) -> None:
    """F-01: ссылка «Поставки» есть, видима и работает на всех десяти страницах."""
    print("\n== F-01: раздел можно найти из любой страницы ==")
    pages = ["turnover", "stocks", "assistant", "sizes", "replenish", "supply",
             "budget", "forecast", "revenue", "lessons"]
    missing, invisible, misplaced = [], [], []
    for name in pages:
        # Автопоказ подсказки ждёт несколько API-ответов и может произойти
        # уже ПОСЛЕ close_hint(). Подготовим состояние через настоящий API:
        # здесь проверяется навигация пользователя, прочитавшего подсказку.
        seen = page.request.post(
            f"{base}/api/hints/seen", data={"page": name},
            headers={"X-Oborot-CSRF": "1"})
        check(f"F-01: подсказка {name} отмечена просмотренной", seen.ok,
              str(seen.status))
        page.goto(f"{base}/{name}")
        page.wait_for_timeout(250)
        close_hint(page)
        info = page.evaluate("""() => {
          const nav = document.querySelector('nav.nav');
          if (!nav) return {nav: false};
          const a = nav.querySelector('a[href="/supply"]');
          if (!a) return {nav: true, found: false};
          const cs = getComputedStyle(a);
          const box = a.getBoundingClientRect();
          const links = [...nav.querySelectorAll('a')].map(x => x.getAttribute('href'));
          const hit = document.elementFromPoint(box.left + box.width / 2,
                                               box.top + box.height / 2);
          return {nav: true, found: true, display: cs.display,
                  visibility: cs.visibility, width: box.width, height: box.height,
                  active: a.classList.contains('active'),
                  clickable: hit === a || a.contains(hit),
                  after: links[links.indexOf('/supply') - 1],
                  before: links[links.indexOf('/supply') + 1]};
        }""")
        if not info.get("found"):
            missing.append(name)
            continue
        if (info["display"] == "none" or info["visibility"] == "hidden"
                or info["width"] <= 0 or info["height"] <= 0
                or not info["clickable"]):
            invisible.append((name, info))
        if not (info.get("after") == "/sizes" and info.get("before") == "/budget"):
            misplaced.append((name, info.get("after"), info.get("before")))
    check("ссылка «Поставки» есть в навигации всех десяти страниц",
          not missing, f"нет на: {missing}")
    check("и она действительно видима и принимает нажатие мышью",
          not invisible, str(invisible)[:200])
    check("и стоит между «Заказ позиции» и «Бюджет»",
          not misplaced, str(misplaced)[:200])

    page.goto(f"{base}/supply")
    page.wait_for_timeout(250)
    close_hint(page)
    active = page.evaluate(
        "() => { const a = document.querySelector('nav.nav a[href=\"/supply\"]');"
        " return a ? a.className : null; }")
    check("на самой странице «Поставки» ссылка помечена активной",
          active is not None and "active" in active, str(active))

    # ОДИН ФРАГМЕНТ НА ДЕВЯТЬ СТРАНИЦ ДОБАВИЛ КЛАСС ТАМ, ГДЕ ЕГО НЕ БЫЛО, и это
    # проверяется, а не объявляется. `primary-page` у «Оборачиваемости» стоял
    # только в turnover.html и stocks.html — единственных, где объявлено правило
    # `.nav a.primary-page { font-weight: 700 }`. Фрагмент ставит класс на всех
    # девяти; на остальных семи правила нет, значит и вида он менять не должен.
    page.goto(f"{base}/budget")
    page.wait_for_timeout(250)
    close_hint(page)
    # `null` вместо стиля — это ответ «ссылки нет», а не исключение: шаг обязан
    # дойти до конца и на дереве, где «Поставок» в навигации ещё не завели.
    weights = page.evaluate("""() => {
      const nav = document.querySelector('nav.nav');
      const g = h => { const a = nav && nav.querySelector('a[href="' + h + '"]');
                       return a ? getComputedStyle(a).fontWeight : null; };
      return {turnover: g('/turnover'), stocks: g('/stocks'), supply: g('/supply')};
    }""")
    check("на странице без правила `primary-page` класс ничего не меняет",
          weights["supply"] is not None
          and weights["turnover"] == weights["stocks"] == weights["supply"],
          str(weights))
    page.goto(f"{base}/turnover")
    page.wait_for_timeout(250)
    close_hint(page)
    bold = page.evaluate("""() => {
      const nav = document.querySelector('nav.nav');
      const g = h => getComputedStyle(nav.querySelector('a[href="' + h + '"]')).fontWeight;
      return {turnover: g('/turnover'), stocks: g('/stocks')};
    }""")
    check("а там, где правило есть, «Оборачиваемость» осталась выделенной",
          bold["turnover"] != bold["stocks"], str(bold))
    # Нажатие делается через DOM, а не `page.click`: на дереве без ссылки
    # `click` ждал бы её тридцать секунд и уронил шаг таймаутом, а нужно
    # поведение — «переход произошёл» или «переходить не по чему».
    page.evaluate("""() => {
      const a = document.querySelector('nav.nav a[href="/supply"]');
      if (a) a.click();
    }""")
    page.wait_for_timeout(500)
    check("ссылка работает: с «Оборачиваемости» переход приводит на /supply",
          page.url.endswith("/supply"), page.url)

def _fix1_f02(page, base) -> None:
    """F-02: атрибут hidden действительно скрывает, а не оставляет полоску."""
    print("\n== F-02: [hidden] скрывает, а не оставляет полоску в 11 px ==")
    page.goto(f"{base}/supply")
    page.wait_for_timeout(700)
    close_hint(page)
    closed = page.evaluate("""() => ({
      mat: document.getElementById('pl-mat-form').getBoundingClientRect().height,
      item: document.getElementById('pl-item-form').getBoundingClientRect().height,
      batch: document.getElementById('pl-batch-form').getBoundingClientRect().height,
      display: getComputedStyle(document.getElementById('pl-mat-form')).display,
    })""")
    check("закрытые формы имеют нулевую высоту, а не рисуются полосками",
          closed["mat"] == 0 and closed["item"] == 0 and closed["batch"] == 0,
          str(closed))
    check("и это именно display:none, а не схлопнувшийся flex",
          closed["display"] == "none", closed["display"])

    page.click("#pl-add-item")
    page.wait_for_timeout(250)
    page.select_option("#pl-item-kind", "catalog")
    page.wait_for_timeout(120)
    cat = page.evaluate("""() => ({
      title: getComputedStyle(document.getElementById('pl-item-title').parentElement).display,
      sketch: getComputedStyle(document.getElementById('pl-item-sketch').parentElement).display,
      base: getComputedStyle(document.getElementById('pl-item-base').parentElement).display,
    })""")
    check("вид «вещь каталога»: название новинки и эскиз скрыты по-настоящему",
          cat["title"] == "none" and cat["sketch"] == "none",
          str(cat))
    check("а поле выбора модели показано", cat["base"] != "none", str(cat))
    page.select_option("#pl-item-kind", "draft")
    page.wait_for_timeout(120)
    draft = page.evaluate("""() => ({
      base: getComputedStyle(document.getElementById('pl-item-base').parentElement).display,
      title: getComputedStyle(document.getElementById('pl-item-title').parentElement).display,
    })""")
    check("вид «новинка»: поле каталога скрыто, а название показано",
          draft["base"] == "none" and draft["title"] != "none", str(draft))
    page.click("#pl-add-item")
    page.wait_for_timeout(150)

    page.click("#pl-add-batch")
    page.wait_for_timeout(250)
    page.select_option("#pl-batch-due-kind", "unknown")
    page.wait_for_timeout(120)
    unknown = page.evaluate("""() => ({
      text: getComputedStyle(document.getElementById('pl-batch-due-text').parentElement).display,
      date: getComputedStyle(document.getElementById('pl-batch-due-date').parentElement).display,
      source: getComputedStyle(document.getElementById('pl-batch-due-source').parentElement).display,
    })""")
    check("при «срок неизвестен» скрыты «Срок словами» и «Дата»",
          unknown["text"] == "none" and unknown["date"] == "none", str(unknown))
    check("а «Источник срока» остаётся: сервер его хранит и карточка показывает",
          unknown["source"] != "none", str(unknown))

def _fix1_f03(page) -> None:
    """F-03: скрытое на экране поле не уходит на сервер и не пропадает молча.

    Шаг продолжает состояние F-02: форма партии уже открыта. Если предыдущий
    шаг упал, этот честно упадёт своей строкой — и это лучше, чем не выполниться
    вовсе.
    """
    print("\n== F-03: набранная дата не пропадает молча ==")
    bodies: list = []
    page.on("request", lambda r: bodies.append(r.post_data)
            if r.method == "POST" and r.url.endswith("/planning/batches") else None)
    page.select_option("#pl-batch-due-kind", "exact")
    page.wait_for_timeout(120)
    page.fill("#pl-batch-due-date", "2026-10-31")
    page.select_option("#pl-batch-due-kind", "text")
    page.wait_for_timeout(120)
    page.fill("#pl-batch-due-text", "к середине ноября")
    page.fill("#pl-batch-title", "Партия со словами")
    page.click("#pl-batch-form button[type=submit]")
    page.wait_for_timeout(1400)
    sent = [b for b in bodies if b]
    check("форма партии ушла на сервер", bool(sent), str(bodies)[:120])
    check("дата, скрытая после смены вида срока, в тело запроса НЕ попала",
          sent and "due_date" not in sent[-1], (sent[-1] if sent else "")[:200])
    check("а текст срока — попал", sent and "due_text" in sent[-1],
          (sent[-1] if sent else "")[:200])
    # Успех проверяется появлением партии, а не пустотой поля ошибки: после
    # удачного сохранения форма закрывается и очищается, поэтому `#pl-batch-err`
    # в DOM больше нет — ожидание этого узла и было бы «проверкой», которая
    # висит тридцать секунд и ничего не доказывает.
    saved_batch = page.evaluate("""() => {
      const card = [...document.querySelectorAll('[data-pl="batch"]')]
        .find(x => x.textContent.indexOf('Партия со словами') >= 0);
      return {saved: !!card,
              text: card ? card.textContent.slice(0, 120) : '',
              formClosed: document.getElementById('pl-batch-form').hidden};
    }""")
    check("и партия сохранилась, а не получила отказ",
          saved_batch["saved"] and saved_batch["formClosed"], str(saved_batch)[:200])
    check("а срок на карточке — тот, что человек написал словами",
          "к середине ноября" in saved_batch["text"], saved_batch["text"])

def _fix1_f05(page, base, c) -> None:
    """F-05: сохранённая вещь видна, и результат сохранения тоже."""
    print("\n== F-05: сохранённая вещь видна, а не «0 шт» ==")
    before_count = page.text_content("#pl-batch-count") or ""
    page.click("#pl-add-item")
    page.wait_for_timeout(250)
    page.select_option("#pl-item-kind", "draft")
    page.fill("#pl-item-title", "Вещь без партий Ф5")
    page.click("#pl-item-form button[type=submit]")
    page.wait_for_timeout(1500)
    row = page.evaluate("""() => {
      const r = [...document.querySelectorAll('[data-pl="item"]')]
        .find(x => x.textContent.indexOf('Вещь без партий Ф5') >= 0);
      if (!r) return null;
      const btn = r.querySelector('button');
      const box = r.getBoundingClientRect();
      return {text: r.textContent, button: btn ? btn.textContent : null,
              top: box.top, bottom: box.bottom, height: box.height,
              inView: box.top >= 0 && box.bottom <= window.innerHeight};
    }""")
    check("после сохранения вещь ВИДНА строкой в блоке, а не только в списке формы",
          row is not None, "строки вещи нет в DOM")
    check("в строке названо, что партий нет, и есть кнопка их создать",
          row and "плановых партий нет" in row["text"]
          and row["button"] == "Запланировать партию", str(row)[:200])
    after_count = page.text_content("#pl-batch-count") or ""
    check("счётчик блока вырос: он считает вещи вместе с партиями",
          before_count != after_count,
          f"было {before_count!r} стало {after_count!r}")
    check("новая строка прокручена в область видимости",
          row and row["inView"], str(row)[:160])
    toast = page.evaluate(
        "() => { const t = document.querySelector('#toast-root .toast');"
        " return t ? t.textContent : null; }")
    check("и человеку сказано «Сохранено»", toast == "Сохранено", str(toast))

    page.evaluate("""() => {
      const r = [...document.querySelectorAll('[data-pl="item"]')]
        .find(x => x.textContent.indexOf('Вещь без партий Ф5') >= 0);
      const b = r ? r.querySelector('button') : null;
      if (b) b.click();
    }""")
    page.wait_for_timeout(400)
    picked = page.evaluate("""() => {
      const f = document.getElementById('pl-batch-form');
      const sel = document.getElementById('pl-batch-item');
      const opt = sel && sel.selectedIndex >= 0 ? sel.options[sel.selectedIndex] : null;
      return {open: !!f && !f.hidden, label: opt ? opt.textContent : null};
    }""")
    check("кнопка строки открывает форму партии С ЭТОЙ вещью выбранной",
          picked["open"] and picked["label"]
          and "Вещь без партий Ф5" in picked["label"], str(picked))
    page.click("#pl-add-batch")
    page.wait_for_timeout(200)

    # Тридцать материалов: новая карточка встаёт ВВЕРХУ, форма остаётся внизу.
    for i in range(30):
        c.post("/api/supply/planning/materials",
               json={"title": f"Массовый материал {i:02d}", "qty": "5",
                     "op_id": f"f5-mass-{i}"})
    page.goto(f"{base}/supply")
    page.wait_for_timeout(900)
    page.click("#pl-add-material")
    page.wait_for_timeout(300)
    page.fill("#pl-mat-title", "Материал после тридцати")
    page.fill("#pl-mat-qty", "12")
    page.click("#pl-mat-form button[type=submit]")
    page.wait_for_timeout(1600)
    # Карточка ищется по `#pl-materials .pl-card` — узлу, который есть и до
    # этого пакета. Через новый `data-pl` замер на старом дереве вернул бы
    # `None`, и «карточка не в окне» доказывало бы отсутствие атрибута, а не
    # неверное положение. Нужна геометрия существующей карточки.
    fresh = page.evaluate("""() => {
      const cards = [...document.querySelectorAll('#pl-materials .pl-card')];
      const card = cards.find(
        x => x.textContent.indexOf('Материал после тридцати') === 0);
      if (!card) return {found: false, total: cards.length};
      const box = card.getBoundingClientRect();
      return {found: true, total: cards.length,
              top: Math.round(box.top), bottom: Math.round(box.bottom),
              h: window.innerHeight,
              index: cards.indexOf(card),
              flashed: card.classList.contains('pl-flash'),
              focused: document.activeElement === card};
    }""")
    check("карточка тридцать первого материала вообще отрисована",
          fresh["found"], str(fresh))
    check("при тридцати материалах новая карточка оказывается в окне",
          fresh["found"] and 0 <= fresh["top"] <= fresh["h"], str(fresh))
    check("она подсвечена и получила фокус",
          fresh["found"] and fresh["flashed"] and fresh["focused"], str(fresh))

def _fix1_f04(page, base, c) -> None:
    """F-04: модель каталога ищется, а не выбирается из первых двадцати."""
    print("\n== F-04: модель ищется, а не выбирается из двадцати ==")
    seed_catalog(60)
    page.goto(f"{base}/supply")
    page.wait_for_timeout(900)
    page.click("#pl-add-item")
    page.wait_for_timeout(300)
    page.select_option("#pl-item-kind", "catalog")
    page.wait_for_timeout(120)
    kind_tag = page.evaluate(
        "() => document.getElementById('pl-item-base').tagName")
    check("выбор модели — поле ввода, а не список из двадцати",
          kind_tag == "INPUT", str(kind_tag))
    # Ввод через DOM с событием `input` — тем же, что порождает клавиатура.
    # `page.fill` на дереве, где здесь всё ещё `<select>`, бросил бы исключение
    # и унёс бы с собой остальные проверки шага; нужен ответ «подсказка не
    # появилась», а не отсутствие ответа.
    page.evaluate("""() => {
      const el = document.getElementById('pl-item-base');
      if (!el) return;
      el.focus();
      el.value = 'тренч';
      el.dispatchEvent(new Event('input', {bubbles: true}));
    }""")
    page.wait_for_timeout(900)
    listed = page.evaluate("""() => {
      const box = document.getElementById('pl-item-base-list');
      if (!box) return {hidden: true, display: 'none', opts: [], missing: true};
      const opts = [...box.querySelectorAll('.pl-combo-opt')].map(o => o.textContent);
      return {hidden: box.hidden, display: getComputedStyle(box).display, opts: opts};
    }""")
    check("по «тренч» подсказка показывает найденную модель",
          not listed["hidden"] and listed["display"] != "none"
          and any("Тренч «Классика»" in o for o in listed["opts"]),
          str(listed)[:200])
    check("и рядом с именем названо число размеров",
          any("размеров" in o for o in listed["opts"]), str(listed["opts"][:2]))
    page.focus("#pl-item-base")
    page.keyboard.press("ArrowDown")
    page.wait_for_timeout(80)
    page.keyboard.press("Enter")
    page.wait_for_timeout(150)
    chosen = page.evaluate(
        "() => { const el = document.getElementById('pl-item-base');"
        " return el ? el.value : null; }")
    check("выбор с клавиатуры (↓ Enter) подставляет каноническое имя",
          chosen == "Тренч «Классика»", repr(chosen))
    page.evaluate("""() => {
      const b = document.querySelector('#pl-item-form button[type=submit]');
      if (b) b.click();
    }""")
    page.wait_for_timeout(1600)
    board = c.get("/api/supply/planning").json()
    check("вещь создана именно с этим base_name",
          any(i["base_name"] == "Тренч «Классика»" for i in board["items"]),
          str([i["base_name"] for i in board["items"]][:5]))


def _fix1_f11(page, base, c, dialogs) -> None:
    """F-11: «Снять» спрашивает на месте кнопки и даёт вернуть назначение."""
    print("\n== F-11: снятие назначения спрашивает и обратимо ==")
    mat = c.post("/api/supply/planning/materials",
                 json={"title": "Ткань для снятия", "qty": "100",
                       "op_id": "f11-mat"}).json()
    mat_id = [m for m in mat["materials"] if m["title"] == "Ткань для снятия"][0]["id"]
    it = c.post("/api/supply/planning/items",
                json={"kind": "draft", "title": "Вещь для снятия",
                      "op_id": "f11-item"}).json()
    item_id = [i for i in it["items"] if i["title"] == "Вещь для снятия"][0]["id"]
    ba = c.post("/api/supply/planning/batches",
                json={"item_id": item_id, "title": "Партия для снятия",
                      "plan_qty": "5", "op_id": "f11-batch"}).json()
    batch_id = [b for b in ba["batches"] if b["title"] == "Партия для снятия"][0]["id"]
    c.post("/api/supply/planning/assignments",
           json={"material_id": mat_id, "batch_id": batch_id, "qty": "40",
                 "note": "на манжеты", "op_id": "f11-assign"})
    page.goto(f"{base}/supply")
    page.wait_for_timeout(1000)

    # КАРТОЧКА ИЩЕТСЯ ПО ТОМУ, ЧТО ЕСТЬ НА ОБОИХ ДЕРЕВЬЯХ. `#pl-batches
    # .pl-card` и `.pl-assign` существовали и до этого пакета, а `data-pl`
    # добавлен им. Опираться на новый атрибут значило бы получить на старом
    # дереве «элемента нет» вместо «кнопка удалила сразу» — то есть доказать
    # отсутствие разметки вместо неверного поведения.
    CARD_JS = """
      const cards = [...document.querySelectorAll('#pl-batches .pl-card')];
      const card = cards.find(x => x.textContent.indexOf('Партия для снятия') >= 0);
    """

    def assign_line() -> str:
        return page.evaluate("""() => {
          %s
          if (!card) return "";
          const line = [...card.querySelectorAll('.pl-assign')]
            .find(l => l.textContent.indexOf('Ткань для снятия') >= 0);
          return line ? line.textContent : "";
        }""" % CARD_JS)

    check("назначение видно на карточке партии", "40" in assign_line(),
          assign_line()[:120])
    page.evaluate("""() => {
      %s
      if (!card) return;
      const line = [...card.querySelectorAll('.pl-assign')]
        .find(l => l.textContent.indexOf('Ткань для снятия') >= 0);
      if (!line) return;
      const btn = [...line.querySelectorAll('button')]
        .find(b => b.textContent === 'Снять');
      if (btn) btn.click();
    }""" % CARD_JS)
    page.wait_for_timeout(600)
    asked = page.evaluate("""() => {
      %s
      const box = card ? card.querySelector('.pl-confirm') : null;
      return box ? box.textContent : null;
    }""" % CARD_JS)
    check("одно нажатие «Снять» ничего не удаляет, а спрашивает",
          asked is not None and "Снять?" in asked and "Да" in asked and "Нет" in asked,
          str(asked))
    check("и назначение всё ещё на месте", "40" in assign_line(), assign_line()[:120])
    check("системного окна confirm() при этом не было", not dialogs, str(dialogs))

    page.evaluate("""() => {
      %s
      if (!card) return;
      const no = [...card.querySelectorAll('.pl-confirm button')]
        .find(b => b.textContent === 'Нет');
      if (no) no.click();
    }""" % CARD_JS)
    page.wait_for_timeout(250)
    check("«Нет» возвращает кнопку и оставляет назначение",
          "40" in assign_line(), assign_line()[:120])

    page.evaluate("""() => {
      %s
      if (!card) return;
      const line = [...card.querySelectorAll('.pl-assign')]
        .find(l => l.textContent.indexOf('Ткань для снятия') >= 0);
      if (!line) return;
      const btn = [...line.querySelectorAll('button')]
        .find(b => b.textContent === 'Снять');
      if (btn) btn.click();
    }""" % CARD_JS)
    page.wait_for_timeout(400)
    page.evaluate("""() => {
      %s
      if (!card) return;
      const yes = [...card.querySelectorAll('.pl-confirm button')]
        .find(b => b.textContent === 'Да');
      if (yes) yes.click();
    }""" % CARD_JS)
    page.wait_for_timeout(1500)
    check("после «Да» назначение снято", assign_line() == "", assign_line()[:120])
    undo = page.evaluate("""() => {
      const t = [...document.querySelectorAll('#toast-root .toast')].pop();
      if (!t) return null;
      const b = t.querySelector('button');
      return {text: t.textContent, action: b ? b.textContent : null};
    }""")
    check("и предложено вернуть",
          undo and "Назначение снято" in undo["text"] and undo["action"] == "Вернуть",
          str(undo))
    page.evaluate("""() => {
      const t = [...document.querySelectorAll('#toast-root .toast')].pop();
      const b = t ? t.querySelector('button') : null;
      if (b) b.click();
    }""")
    page.wait_for_timeout(1600)
    back = assign_line()
    check("«Вернуть» восстанавливает назначение с тем же количеством",
          "40" in back, back[:120])
    check("и confirm() не появлялся ни разу за весь сценарий",
          not dialogs, str(dialogs))

    # ── «Вернуть» при занятом отправителе ───────────────────────────────────
    #
    # РЕГРЕССИЯ. Обработчик тоста закрывал его ПЕРВОЙ строкой, а `send()` при
    # уже идущей записи выходил, ничего не отправив. Итог: человек нажал
    # «Вернуть», восстановление не ушло, а единственная кнопка возврата исчезла
    # вместе с тостом — повторить стало нечем.
    #
    # Занятость создаётся КОНТРОЛИРУЕМОЙ ЗАДЕРЖКОЙ ИНТЕРФЕЙСА, а не гонкой:
    # `fetch` оборачивается так, что запрос к ДРУГОЙ ручке (материалы) висит
    # заданное время. Никаких конкурентных записей в одну строку нет, запрос
    # завершается сам и обычным ответом сервера. Опыт PR #49 не повторяется.
    print("\n-- «Вернуть» при занятом отправителе --")
    page.evaluate("""() => {
      window.__undoPosts = [];
      const real = window.fetch;
      window.__realFetch = real;
      window.fetch = function (url, init) {
        const u = String((url && url.url) || url);
        if (init && init.method === 'POST'
            && u.indexOf('/planning/assignments') !== -1
            && u.indexOf('/delete') === -1) {
          window.__undoPosts.push(u);
        }
        if (u.indexOf('/planning/materials') !== -1 && window.__holdMs) {
          const ms = window.__holdMs;
          return new Promise((res, rej) => setTimeout(
            () => real.apply(this, [url, init]).then(res, rej), ms));
        }
        return real.apply(this, arguments);
      };
    }""")

    def undo_button() -> int:
        return page.evaluate(
            "() => [...document.querySelectorAll('#toast-root .pl-toast-act')]"
            ".filter(b => b.textContent === 'Вернуть').length")

    page.evaluate("""() => {
      %s
      if (!card) return;
      const line = [...card.querySelectorAll('.pl-assign')]
        .find(l => l.textContent.indexOf('Ткань для снятия') >= 0);
      const btn = line ? [...line.querySelectorAll('button')]
        .find(b => b.textContent === 'Снять') : null;
      if (btn) btn.click();
    }""" % CARD_JS)
    page.wait_for_timeout(300)
    page.evaluate("""() => {
      %s
      const yes = card ? [...card.querySelectorAll('.pl-confirm button')]
        .find(b => b.textContent === 'Да') : null;
      if (yes) yes.click();
    }""" % CARD_JS)
    page.wait_for_timeout(1500)
    check("тост с «Вернуть» появился", undo_button() == 1, str(undo_button()))

    # Занимаем отправителя долгим запросом к ДРУГОЙ ручке.
    page.evaluate("() => { window.__holdMs = 2500; }")
    page.click("#pl-add-material")
    page.wait_for_timeout(200)
    page.fill("#pl-mat-title", "Материал, занявший отправителя")
    page.fill("#pl-mat-qty", "3")
    page.click("#pl-mat-form button[type=submit]")
    page.wait_for_timeout(300)
    page.evaluate("() => { window.__undoPosts = []; }")

    page.evaluate("""() => {
      const b = [...document.querySelectorAll('#toast-root .pl-toast-act')]
        .find(x => x.textContent === 'Вернуть');
      if (b) b.click();
    }""")
    page.wait_for_timeout(400)
    sent_while_busy = page.evaluate("() => (window.__undoPosts || []).length")
    check("при занятом отправителе восстановление не ушло — это и есть занятость",
          sent_while_busy == 0, f"ушло запросов: {sent_while_busy}")
    check("НО кнопка «Вернуть» осталась на экране, а не исчезла впустую",
          undo_button() == 1, f"кнопок «Вернуть»: {undo_button()}")

    # Отпускаем отправителя и повторяем — теперь восстановление обязано пройти.
    page.evaluate("() => { window.__holdMs = 0; }")
    page.wait_for_timeout(2600)
    check("кнопка дожила до момента, когда повтор возможен",
          undo_button() == 1, f"кнопок «Вернуть»: {undo_button()}")
    page.evaluate("""() => {
      const b = [...document.querySelectorAll('#toast-root .pl-toast-act')]
        .find(x => x.textContent === 'Вернуть');
      if (b) b.click();
    }""")
    page.wait_for_timeout(1800)
    check("повторное нажатие отправило восстановление",
          page.evaluate("() => (window.__undoPosts || []).length") >= 1,
          str(page.evaluate("() => (window.__undoPosts || []).length")))
    check("и назначение действительно вернулось",
          "40" in assign_line(), assign_line()[:120])
    page.evaluate("() => { if (window.__realFetch) window.fetch = window.__realFetch; }")

def _fix1_f07(page, base, c) -> None:
    """F-07: делать нечего — блока «Следующий шаг» на экране нет.

    Отказ сервера от правила `assign` проверяется юнитами в
    `test_supply_planning.py`; здесь проверяется ВТОРАЯ половина того же
    пункта — отрисовка. Она жила отдельно от серверной: `render()` красил блок
    в «ok» и всё равно показывал, поэтому человек читал «План собран:
    материалы распределены» ровно тогда, когда сто метров лежали
    нераспределёнными.

    Ответ сервера здесь подменяется намеренно. Довести общую организацию
    набора до `code=ok` живым путём нельзя надёжно: в ней к этому шагу уже
    лежат партии без плана и перерасход из соседних сценариев, и правило `ok`
    не наступит — проверка молча превратилась бы в «блок виден», то есть
    всегда зелёную на обеих ветках. Подменяется РОВНО `next_step`, остальная
    доска остаётся настоящей; браузер, DOM и стили — тоже настоящие.
    """
    print("\n== F-07: при «делать нечего» блок следующего шага скрыт ==")
    live = c.get("/api/supply/planning").json()
    route_re = re.compile(r"/api/supply/planning$")

    def serve(code: str, text: str) -> dict:
        body = dict(live)
        body["next_step"] = {"code": code, "text": text}
        page.unroute(route_re)
        page.route(route_re, lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps(body, ensure_ascii=False)))
        page.goto(f"{base}/supply")
        page.wait_for_timeout(900)
        return page.evaluate("""() => {
          const n = document.getElementById('pl-next');
          if (!n) return {missing: true};
          const cs = getComputedStyle(n);
          const box = n.getBoundingClientRect();
          return {missing: false, hidden: n.hidden, display: cs.display,
                  h: Math.round(box.height),
                  text: n.textContent.replace(/\\s+/g, ' ').trim()};
        }""")

    todo = serve("plan_qty", "У партии «Проба» нет плана — укажите количество.")
    check("когда дело есть — блок показан и называет его",
          not todo["missing"] and not todo["hidden"]
          and todo["display"] != "none" and todo["h"] > 0
          and "нет плана" in todo["text"], str(todo)[:200])

    done = serve("ok", "План собран: материалы распределены, у партий "
                       "есть план и срок.")
    check("когда делать нечего — блок скрыт атрибутом hidden",
          not done["missing"] and done["hidden"], str(done)[:200])
    check("и скрыт по-настоящему: display none и нулевая высота",
          not done["missing"] and done["display"] == "none" and done["h"] == 0,
          str(done)[:200])
    check("и с экрана ушло утверждение «материалы распределены»",
          not done["missing"] and "распределены" not in done["text"],
          str(done)[:200])
    page.unroute(route_re)


def _fix1_f08(page, base, errors) -> None:
    """F-08: вкладка предпросмотра существует только у организации с флагом."""
    print("\n== F-08: предпросмотр показан только организации с флагом ==")
    set_preview_flag(False)
    page.goto(f"{base}/supply")
    page.wait_for_timeout(700)
    gated = page.evaluate("""() => ({
      tab: !!document.getElementById('sup-tab-preview'),
      panel: !!document.getElementById('sup-view-preview'),
      plan: !!document.getElementById('sup-view-plan'),
      planVisible: document.getElementById('sup-view-plan')
        ? getComputedStyle(document.getElementById('sup-view-plan')).display !== 'none'
        : false,
    })""")
    check("без флага вкладки предпросмотра на странице нет",
          not gated["tab"] and not gated["panel"], str(gated))
    check("а план производства остаётся и виден",
          gated["plan"] and gated["planVisible"], str(gated))
    check("страница без предпросмотра работает без ошибок в консоли",
          not errors, str(errors)[:200])
    set_preview_flag(True)
    page.goto(f"{base}/supply")
    page.wait_for_timeout(700)
    check("с флагом вкладка снова на месте и переключает раздел",
          page.evaluate("() => !!document.getElementById('sup-tab-preview')"),
          "вкладки нет")
    page.click("#sup-tab-preview")
    page.wait_for_timeout(300)
    check("переключение работает: панель предпросмотра показана",
          page.evaluate("() => getComputedStyle("
                        "document.getElementById('sup-view-preview')).display")
          != "none", "панель не открылась")


def _fix1_f06(browser, base, c) -> None:
    """F-06: на телефоне ни одна кнопка не лежит под фиксированной плашкой.

    Свой контекст, а не общая страница: 390×844 — это другое устройство, и
    менять размер окна у уже открытой страницы значило бы проверять поведение
    ресайза, а не мобильную раскладку.

    ЧЕСТНАЯ ГРАНИЦА ДОКАЗАТЕЛЬСТВА. Точный hit-test из ТЗ (кнопки «Создать
    плановую партию» и «Добавить материал» на /supply при 390×844 отдают
    `A.fresh-chip`) НА `ea1caff` НЕ ВОСПРОИЗВЁЛСЯ. Замерено: после прокрутки в
    самый низ обе кнопки занимают 719..757, плашка свежести — 773..826, между
    ними 16 px. Перебраны состояния: пустая организация, демо без плана, 1/2/3/
    4/6/9/14 материалов, `is_mobile` True и False, искусственно удлинённый до
    двух строк текст плашки. Ни одно не дало перекрытия.
    Поэтому ниже РАЗДЕЛЕНО:
      • сам hit-test на /supply — GUARD: он зелёный и на старом дереве, и
        выдавать его за RED нельзя (D-42);
      • запас снизу и место тоста — настоящий RED, они и есть то, что этот
        пакет изменил.
    Статус точного КП: NOT REPRODUCED, решение за владельцем. Подменять его
    другой страницей я не стал: механизм там тот же, но это другой КП.
    """
    print("\n== F-06: 390x844 — кнопки не под чипом свежести и кнопкой «?» ==")
    mob = browser.new_context(viewport={"width": 390, "height": 844},
                              is_mobile=True, has_touch=True)
    mob.add_cookies([{"name": k, "value": v, "domain": "127.0.0.1", "path": "/"}
                     for k, v in c.cookies.items()])
    mpage = mob.new_page()
    mpage.goto(f"{base}/supply")
    mpage.wait_for_timeout(1200)
    close_hint(mpage)
    mpage.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
    mpage.wait_for_timeout(500)
    probe = mpage.evaluate("""() => {
      const bad = [], seen = [];
      for (const btn of document.querySelectorAll('button.btn')) {
        const box = btn.getBoundingClientRect();
        if (box.width === 0 || box.height === 0) continue;
        const x = box.left + box.width / 2, y = box.top + box.height / 2;
        // Точка ЗА пределами окна — не «перекрытая кнопка»: elementFromPoint
        // там законно отдаёт null. Спрашиваем только про то, что видно.
        if (y < 0 || y > window.innerHeight || x < 0 || x > window.innerWidth) continue;
        seen.push(btn.textContent.replace(/\\s+/g, ' ').trim());
        const hit = document.elementFromPoint(x, y);
        if (!hit) { bad.push([btn.textContent.slice(0, 24), 'null']); continue; }
        if (hit !== btn && !btn.contains(hit)) {
          bad.push([btn.textContent.slice(0, 24),
                    hit.tagName + '.' + (hit.className || '')]);
        }
      }
      const present = [...document.querySelectorAll('button.btn')]
        .map(b => b.textContent.replace(/\\s+/g, ' ').trim())
        .filter(t => t.indexOf('Создать плановую партию') >= 0
                  || t.indexOf('Добавить материал') >= 0);
      return {bad: bad, seen: seen, present: present};
    }""")
    # Выборка обязана быть непустой, а обе кнопки из ТЗ — существовать на
    # странице. Без этих строк «перекрытых нет» стало бы зелёным и на странице,
    # где кнопок не осталось вовсе, — проверка перестала бы что-либо значить,
    # не покраснев.
    # Существование и осмотр РАЗНЕСЕНЫ намеренно. При прокрутке в самый низ
    # «Добавить материал» уходит выше края окна, и требовать её осмотра значило
    # бы требовать hit-test точки, которой на экране нет: `elementFromPoint`
    # там законно отдаёт null, и красная строка говорила бы о положении окна,
    # а не о перекрытии.
    check("hit-test смотрел на реальные кнопки, а не на пустую выборку",
          len(probe["seen"]) >= 5, f"осмотрено {len(probe['seen'])}: "
          f"{probe['seen'][:8]}")
    check("обе кнопки, названные в ТЗ по F-06, на странице есть",
          len(probe["present"]) == 2, f"нашлось {probe['present']}")
    # GUARD, НЕ RED: на `ea1caff` эта строка тоже зелёная (замеры — в описании
    # шага). Она защищает от появления перекрытия, но не доказывает F-06.
    check("GUARD (зелено и на baseline): на /supply ни одна кнопка не "
          "перекрыта чужим элементом",
          probe["bad"] == [], str(probe["bad"])[:300])
    chip = mpage.evaluate("""() => {
      const el = document.getElementById('fresh-chip');
      const sp = document.getElementById('hint-bottom-spacer');
      return {chip: el ? getComputedStyle(el).position : null,
              spacer: sp ? getComputedStyle(sp).display : null,
              spacerH: sp ? sp.getBoundingClientRect().height : 0};
    }""")
    check("запас под фиксированными элементами на телефоне включён",
          chip["spacer"] == "block" and chip["spacerH"] >= 70, str(chip))
    toast_pos = mpage.evaluate(
        "() => getComputedStyle(document.getElementById('toast-root')).bottom")
    check("тост поднят над кнопкой «?», а не лежит на ней",
          toast_pos == "72px", str(toast_pos))
    # Настоящий RED с геометрией: живой тост и кнопка «?» не должны занимать
    # общих точек. Инструмент — пересечение прямоугольников, а не
    # elementFromPoint: тост прозрачен для указателя, поэтому hit-test прошёл бы
    # и там, где тост лежит на кнопке — и «доказал» бы отсутствие того, что
    # человек видит своими глазами.
    overlap = mpage.evaluate("""() => {
      const root = document.getElementById('toast-root');
      const fab = document.getElementById('hint-fab');
      if (!root || !fab) return {ok: false, why: 'нет toast-root или кнопки «?»'};
      const t = document.createElement('div');
      t.className = 'toast';
      t.textContent = 'Сохранено';
      root.appendChild(t);
      const a = t.getBoundingClientRect(), b = fab.getBoundingClientRect();
      const dx = Math.min(a.right, b.right) - Math.max(a.left, b.left);
      const dy = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
      const res = {ok: true, overlapX: Math.round(dx), overlapY: Math.round(dy),
                   toast: [Math.round(a.top), Math.round(a.bottom),
                           Math.round(a.left), Math.round(a.right)],
                   fab: [Math.round(b.top), Math.round(b.bottom),
                         Math.round(b.left), Math.round(b.right)]};
      t.remove();
      return res;
    }""")
    check("живой тост и кнопка «?» не имеют ни одной общей точки",
          overlap["ok"] and not (overlap["overlapX"] > 0
                                 and overlap["overlapY"] > 0),
          str(overlap))
    mob.close()



def supply_fix_2_ui(pw, base, c) -> None:
    """SUPPLY-FIX-2 в настоящем браузере: F-13, F-14, F-15 и реализованный F-12.

    ПОВЕДЕНИЕ, А НЕ РАЗМЕТКА: карточка перечитывается после действия, видимость
    берётся из `getComputedStyle`, попадание по кнопке — из `elementFromPoint`,
    а состояние сервера сверяется отдельным запросом, а не тем, что нарисовано.

    ЧЕГО ЗДЕСЬ НЕТ И ПОЧЕМУ. КП F-12 из ТЗ говорит про «удаление ПАРТИИ с
    назначением». Архивация партии в этом пакете не реализована: её семантика —
    продуктовая развилка, удержанная до решения владельца (`TECH_DEBT.md`,
    `SUPPLY-FIX-2-REG`). Проверяется то, что сделано: удаление материала и
    новинки с возвратом. Невыполненный КП назван невыполненным, а не заменён
    похожим.

    Каждый шаг отдельный по той же причине, что в SUPPLY-FIX-1: прогон против
    дерева без правки обязан сказать про КАЖДЫЙ пункт, а не умереть на первом.
    """
    browser = pw.chromium.launch()
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    ctx.add_cookies([{"name": k, "value": v, "domain": "127.0.0.1", "path": "/"}
                     for k, v in c.cookies.items()])
    errors: list[str] = []
    dialogs: list[str] = []
    page = ctx.new_page()
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("dialog", lambda d: (dialogs.append(d.type), d.dismiss()))

    steps = (
        ("F-13", lambda: _fix2_f13(page, base, c)),
        ("F-14", lambda: _fix2_f14(page, base, c)),
        ("F-15", lambda: _fix2_f15(page, base, c)),
        ("F-12", lambda: _fix2_f12(page, base, c, dialogs)),
        ("F-12 партия", lambda: _fix2_batch_delete(page, base, c, dialogs)),
        ("F-12 каталог", lambda: _fix2_catalog_confirm(page, base, c)),
    )
    for label, run_step in steps:
        try:
            run_step()
        except Exception as exc:  # noqa: BLE001 — важен отчёт, а не тип
            check(f"{label}: шаг дошёл до конца без исключения", False,
                  f"{type(exc).__name__}: "
                  f"{str(exc).strip().splitlines()[0][:160]}")

    check("за сценарий SUPPLY-FIX-2 не было ошибок в консоли",
          not errors, str(errors)[:200])
    ctx.close()
    try:
        _fix2_mobile(browser, base, c)
    except Exception as exc:  # noqa: BLE001
        check("F-12/F-13 на телефоне: шаг дошёл до конца без исключения", False,
              f"{type(exc).__name__}: {str(exc).strip().splitlines()[0][:160]}")
    browser.close()


#: Карточка материала ищется по видимому тексту — тому, что есть на ОБОИХ
#: деревьях. Опираться на `data-pl` значило бы получить на дереве без правки
#: «элемента нет» вместо «кнопки нет», то есть доказать отсутствие разметки
#: вместо отсутствия поведения.
_MAT_CARD_JS = """
  const cards = [...document.querySelectorAll('#pl-materials .pl-card')];
  const card = cards.find(x => x.textContent.indexOf(NAME) >= 0);
"""


def _mat_card_js(body: str, name: str) -> str:
    return "() => { const NAME = %r; %s %s }" % (name, _MAT_CARD_JS, body)


def _fix2_f13(page, base, c) -> None:
    """F-13: у материала правится название, а не только количество."""
    print("\n== F-13: название материала правится из карточки ==")
    made = c.post("/api/supply/planning/materials",
                  json={"title": "Шерсь костюмная", "qty": "300", "unit": "м",
                        "source_note": "счёт 7", "op_id": "f13-mat"}).json()
    mat_id = [m for m in made["materials"]
              if m["title"] == "Шерсь костюмная"][0]["id"]
    page.goto(f"{base}/supply")
    page.wait_for_timeout(1000)
    close_hint(page)

    label = page.evaluate(_mat_card_js("""
      if (!card) return null;
      const btn = [...card.querySelectorAll('.pl-actions button')]
        .find(b => b.textContent === 'Изменить'
                   || b.textContent === 'Уточнить количество');
      return btn ? btn.textContent : null;
    """, "Шерсь костюмная"))
    check("кнопка правки называется «Изменить», а не «Уточнить количество»",
          label == "Изменить", str(label))

    page.evaluate(_mat_card_js("""
      if (!card) return;
      const btn = [...card.querySelectorAll('.pl-actions button')]
        .find(b => b.textContent === 'Изменить');
      if (btn) btn.click();
    """, "Шерсь костюмная"))
    page.wait_for_timeout(400)
    labels = page.evaluate(_mat_card_js("""
      if (!card) return [];
      const form = card.querySelector('.pl-form.inline');
      if (!form) return [];
      return [...form.querySelectorAll('label')].map(l => l.textContent);
    """, "Шерсь костюмная"))
    check("в форме правки есть название, количество, единица и заметка",
          any("Название материала" in x for x in labels)
          and any("Количество" in x for x in labels)
          and any("Единица" in x for x in labels)
          and any("Источник" in x for x in labels), str(labels))

    filled = page.evaluate(_mat_card_js("""
      if (!card) return false;
      const form = card.querySelector('.pl-form.inline');
      if (!form) return false;
      const fields = [...form.querySelectorAll('.pl-field')];
      const box = fields.find(f => f.textContent.indexOf('Название материала') >= 0);
      if (!box) return false;
      const input = box.querySelector('input');
      if (!input) return false;
      input.value = 'Шерсть костюмная';
      input.dispatchEvent(new Event('input', {bubbles: true}));
      return true;
    """, "Шерсь костюмная"))
    check("поле названия в форме нашлось и заполнено", filled is True, str(filled))
    page.evaluate(_mat_card_js("""
      if (!card) return;
      const form = card.querySelector('.pl-form.inline');
      if (form) form.querySelector('button[type=submit]').click();
    """, "Шерсь костюмная"))
    page.wait_for_timeout(1200)

    on_screen = page.evaluate("""() => {
      const cards = [...document.querySelectorAll('#pl-materials .pl-card')];
      return {
        fixed: cards.some(x => x.textContent.indexOf('Шерсть костюмная') >= 0),
        typo: cards.some(x => x.textContent.indexOf('Шерсь костюмная') >= 0),
      };
    }""")
    check("карточка на экране показывает исправленное название",
          on_screen["fixed"] and not on_screen["typo"], str(on_screen))
    board = c.get("/api/supply/planning").json()
    saved = [m for m in board["materials"] if m["id"] == mat_id]
    renamed = bool(saved) and saved[0]["title"] == "Шерсть костюмная"
    check("и сервер отдаёт новое название в board.materials", renamed,
          str(saved[0]["title"]) if saved else "материала нет")
    # ПРИВЯЗАНО К САМОЙ ПРАВКЕ. Без этого проверка зеленела бы на дереве, где
    # форма правки названия отсутствует вовсе: соседние поля там не сброшены
    # ровно потому, что ничего и не правилось.
    check("остальные поля правкой названия не сброшены",
          renamed and saved[0]["qty"] == 300 and saved[0]["unit"] == "м"
          and saved[0]["source_note"] == "счёт 7",
          f"переименовано={renamed} " + (str(saved[0]) if saved else ""))


def _fix2_f14(page, base, c) -> None:
    """F-14: на карточке материала видно, куда он расписан."""
    print("\n== F-14: строки распределения видны на карточке материала ==")
    made = c.post("/api/supply/planning/materials",
                  json={"title": "Подкладка вискоза", "qty": "200",
                        "op_id": "f14-mat"}).json()
    mat_id = [m for m in made["materials"]
              if m["title"] == "Подкладка вискоза"][0]["id"]
    it = c.post("/api/supply/planning/items",
                json={"kind": "draft", "title": "Пальто-новинка",
                      "op_id": "f14-item"}).json()
    item_id = [i for i in it["items"] if i["title"] == "Пальто-новинка"][0]["id"]
    ba = c.post("/api/supply/planning/batches",
                json={"item_id": item_id, "title": "Пальто, первая закладка",
                      "plan_qty": "12", "op_id": "f14-batch"}).json()
    batch_id = [b for b in ba["batches"]
                if b["title"] == "Пальто, первая закладка"][0]["id"]
    c.post("/api/supply/planning/assignments",
           json={"material_id": mat_id, "batch_id": batch_id, "qty": "120",
                 "op_id": "f14-assign"})
    page.goto(f"{base}/supply")
    page.wait_for_timeout(1000)
    close_hint(page)

    info = page.evaluate(_mat_card_js("""
      if (!card) return null;
      const links = [...card.querySelectorAll('.pl-link')];
      if (!links.length) return {count: 0};
      const first = links[0];
      const cs = getComputedStyle(first);
      const rect = first.getBoundingClientRect();
      return {count: links.length, text: first.textContent,
              display: cs.display, visibility: cs.visibility,
              height: Math.round(rect.height)};
    """, "Подкладка вискоза"))
    check("строка распределения существует на карточке",
          info and info.get("count") == 1, str(info))
    check("она действительно видима, а не скрыта стилем",
          info and info.get("display") != "none"
          and info.get("visibility") == "visible"
          and (info.get("height") or 0) > 0, str(info))
    check("и называет количество и партию",
          info and "120" in info.get("text", "")
          and "Пальто, первая закладка" in info.get("text", ""),
          str(info.get("text"))[:120] if info else "")

    # Клик ведёт к самой партии: карточка партии должна оказаться в окне.
    page.evaluate("() => window.scrollTo(0, 0)")
    page.wait_for_timeout(200)
    clicked = page.evaluate(_mat_card_js("""
      if (!card) return false;
      const btn = card.querySelector('.pl-link .pl-linkbtn');
      if (!btn) return false;
      btn.click();
      return true;
    """, "Подкладка вискоза"))
    page.wait_for_timeout(700)
    seen = page.evaluate("""() => {
      const cards = [...document.querySelectorAll('#pl-batches .pl-card')];
      const card = cards.find(x => x.textContent.indexOf('Пальто, первая закладка') >= 0);
      if (!card) return null;
      const r = card.getBoundingClientRect();
      return {top: Math.round(r.top), bottom: Math.round(r.bottom),
              h: window.innerHeight};
    }""")
    # ФАКТ НАЖАТИЯ — ЧАСТЬ УТВЕРЖДЕНИЯ. Без него проверка зеленела бы там, где
    # строки распределения нет вовсе: карточка партии и так могла оказаться в
    # окне, и «клик привёл» доказывалось бы её случайным положением.
    check("клик по строке приводит к карточке партии в области видимости",
          clicked is True and seen and seen["bottom"] > 0
          and seen["top"] < seen["h"], f"нажатие={clicked} {seen}")


def _fix2_f15(page, base, c) -> None:
    """F-15: партия на материале без количества помечена явно."""
    print("\n== F-15: «наличие не подтверждено» видно на партии ==")
    made = c.post("/api/supply/planning/materials",
                  json={"title": "Пуговицы рогов", "unit": "компл.",
                        "op_id": "f15-mat"}).json()
    mat_id = [m for m in made["materials"]
              if m["title"] == "Пуговицы рогов"][0]["id"]
    it = c.post("/api/supply/planning/items",
                json={"kind": "draft", "title": "Жилет-новинка",
                      "op_id": "f15-item"}).json()
    item_id = [i for i in it["items"] if i["title"] == "Жилет-новинка"][0]["id"]
    ba = c.post("/api/supply/planning/batches",
                json={"item_id": item_id, "title": "Жилеты, проба",
                      "plan_qty": "8", "op_id": "f15-batch"}).json()
    batch_id = [b for b in ba["batches"]
                if b["title"] == "Жилеты, проба"][0]["id"]
    c.post("/api/supply/planning/assignments",
           json={"material_id": mat_id, "batch_id": batch_id, "qty": "8",
                 "op_id": "f15-assign"})
    page.goto(f"{base}/supply")
    page.wait_for_timeout(1000)
    close_hint(page)

    badge = page.evaluate("""() => {
      const cards = [...document.querySelectorAll('#pl-batches .pl-card')];
      const card = cards.find(x => x.textContent.indexOf('Жилеты, проба') >= 0);
      if (!card) return null;
      const line = [...card.querySelectorAll('.pl-assign')]
        .find(l => l.textContent.indexOf('Пуговицы рогов') >= 0);
      if (!line) return {line: false};
      const tag = [...line.querySelectorAll('.pl-tag')]
        .find(t => t.textContent.indexOf('наличие не подтверждено') >= 0);
      if (!tag) return {line: true, tag: false};
      const cs = getComputedStyle(tag);
      const r = tag.getBoundingClientRect();
      return {line: true, tag: true, display: cs.display,
              visibility: cs.visibility, w: Math.round(r.width)};
    }""")
    check("бейдж «наличие не подтверждено» есть у назначения",
          badge and badge.get("tag") is True, str(badge))
    check("и он видим, а не нулевой ширины",
          badge and badge.get("display") != "none"
          and badge.get("visibility") == "visible"
          and (badge.get("w") or 0) > 0, str(badge))

    known = page.evaluate("""() => {
      const cards = [...document.querySelectorAll('#pl-batches .pl-card')];
      const card = cards.find(x => x.textContent.indexOf('Пальто, первая закладка') >= 0);
      if (!card) return null;
      const line = [...card.querySelectorAll('.pl-assign')]
        .find(l => l.textContent.indexOf('Подкладка вискоза') >= 0);
      return line ? line.textContent.indexOf('наличие не подтверждено') >= 0 : null;
    }""")
    check("у материала с известным количеством бейджа нет",
          known is False, str(known))
    summary = c.get("/api/supply/planning").json()["summary"]
    check("сводка считает партии на неподтверждённом наличии",
          summary.get("batches_on_unknown") == 1,
          str(summary.get("batches_on_unknown")))


def _fix2_f12(page, base, c, dialogs) -> None:
    """F-12: лишнюю строку можно убрать и тут же вернуть."""
    print("\n== F-12: удаление спрашивает, а тост возвращает ==")
    made = c.post("/api/supply/planning/materials",
                  json={"title": "ТЕСТ лишняя ткань", "qty": "1",
                        "op_id": "f12-mat"}).json()
    mat_id = [m for m in made["materials"]
              if m["title"] == "ТЕСТ лишняя ткань"][0]["id"]
    page.goto(f"{base}/supply")
    page.wait_for_timeout(1000)
    close_hint(page)

    NAME = "ТЕСТ лишняя ткань"
    page.evaluate(_mat_card_js("""
      if (!card) return;
      const btn = [...card.querySelectorAll('.pl-actions button')]
        .find(b => b.textContent === 'Удалить');
      if (btn) btn.click();
    """, NAME))
    page.wait_for_timeout(400)
    asked = page.evaluate(_mat_card_js("""
      const box = card ? card.querySelector('.pl-confirm') : null;
      return box ? box.textContent : null;
    """, NAME))
    check("одно нажатие «Удалить» ничего не удаляет, а спрашивает",
          asked is not None and "Удалить?" in asked and "Да" in asked
          and "Нет" in asked, str(asked))
    check("системного окна confirm() при этом не было", not dialogs, str(dialogs))
    still = c.get("/api/supply/planning").json()
    check("и на сервере строка на месте",
          any(m["id"] == mat_id for m in still["materials"]))

    page.evaluate(_mat_card_js("""
      if (!card) return;
      const no = [...card.querySelectorAll('.pl-confirm button')]
        .find(b => b.textContent === 'Нет');
      if (no) no.click();
    """, NAME))
    page.wait_for_timeout(300)
    check("«Нет» возвращает кнопку и ничего не удаляет",
          page.evaluate(_mat_card_js("""
            if (!card) return false;
            return !card.querySelector('.pl-confirm')
                   && [...card.querySelectorAll('.pl-actions button')]
                        .some(b => b.textContent === 'Удалить');
          """, NAME)) is True)

    page.evaluate(_mat_card_js("""
      if (!card) return;
      const btn = [...card.querySelectorAll('.pl-actions button')]
        .find(b => b.textContent === 'Удалить');
      if (btn) btn.click();
    """, NAME))
    page.wait_for_timeout(300)
    page.evaluate(_mat_card_js("""
      if (!card) return;
      const yes = [...card.querySelectorAll('.pl-confirm button')]
        .find(b => b.textContent === 'Да');
      if (yes) yes.click();
    """, NAME))
    page.wait_for_timeout(1200)

    gone = page.evaluate("""() => {
      const cards = [...document.querySelectorAll('#pl-materials .pl-card')];
      return cards.some(x => x.textContent.indexOf('ТЕСТ лишняя ткань') >= 0);
    }""")
    check("после «Да» карточки на экране нет", gone is False, str(gone))
    after = c.get("/api/supply/planning").json()
    was_deleted = not any(m["id"] == mat_id for m in after["materials"])
    check("и сервер её больше не отдаёт", was_deleted)

    toast = page.evaluate("""() => {
      const t = [...document.querySelectorAll('#toast-root .toast')]
        .find(x => x.textContent.indexOf('Удалено') >= 0);
      if (!t) return null;
      const btn = t.querySelector('.pl-toast-act');
      return {text: t.textContent, action: btn ? btn.textContent : null};
    }""")
    check("показан тост «Удалено» с кнопкой возврата",
          toast and toast.get("action") == "Вернуть", str(toast))

    page.evaluate("""() => {
      const t = [...document.querySelectorAll('#toast-root .toast')]
        .find(x => x.textContent.indexOf('Удалено') >= 0);
      const btn = t ? t.querySelector('.pl-toast-act') : null;
      if (btn) btn.click();
    }""")
    page.wait_for_timeout(1200)
    back = c.get("/api/supply/planning").json()
    restored = [m for m in back["materials"] if m["id"] == mat_id]
    # `was_deleted` взят выше по факту ответа сервера: на дереве без этого
    # пакета строка не удалялась вовсе, и «вернулась та же» зеленело бы, ничего
    # не доказав.
    check("«Вернуть» возвращает ТУ ЖЕ строку, а не создаёт новую",
          was_deleted and restored and restored[0]["title"] == "ТЕСТ лишняя ткань"
          and restored[0]["qty"] == 1,
          f"удалялась={was_deleted} " + str(restored)[:120])
    on_screen = page.evaluate("""() => {
      const cards = [...document.querySelectorAll('#pl-materials .pl-card')];
      return cards.some(x => x.textContent.indexOf('ТЕСТ лишняя ткань') >= 0);
    }""")
    check("и карточка снова на экране", was_deleted and on_screen is True,
          f"удалялась={was_deleted} на_экране={on_screen}")

    # Новинка без партий убирается тем же приёмом; вещь каталога — нет, и
    # кнопки у неё нет вовсе (развилка удержана, см. докстринг набора).
    it = c.post("/api/supply/planning/items",
                json={"kind": "draft", "title": "ТЕСТ лишняя новинка",
                      "op_id": "f12-item"}).json()
    item_id = [i for i in it["items"]
               if i["title"] == "ТЕСТ лишняя новинка"][0]["id"]
    page.reload()
    page.wait_for_timeout(1000)
    close_hint(page)
    has_btn = page.evaluate("""() => {
      const rows = [...document.querySelectorAll('#pl-batches .pl-itemrow')];
      const row = rows.find(x => x.textContent.indexOf('ТЕСТ лишняя новинка') >= 0);
      if (!row) return null;
      return [...row.querySelectorAll('.pl-actions button')].map(b => b.textContent);
    }""")
    check("у строки новинки есть и «Изменить», и «Удалить»",
          has_btn and "Изменить" in has_btn and "Удалить" in has_btn,
          str(has_btn))
    page.evaluate("""() => {
      const rows = [...document.querySelectorAll('#pl-batches .pl-itemrow')];
      const row = rows.find(x => x.textContent.indexOf('ТЕСТ лишняя новинка') >= 0);
      if (!row) return;
      const btn = [...row.querySelectorAll('.pl-actions button')]
        .find(b => b.textContent === 'Удалить');
      if (btn) btn.click();
    }""")
    page.wait_for_timeout(300)
    page.evaluate("""() => {
      const rows = [...document.querySelectorAll('#pl-batches .pl-itemrow')];
      const row = rows.find(x => x.textContent.indexOf('ТЕСТ лишняя новинка') >= 0);
      if (!row) return;
      const yes = [...row.querySelectorAll('.pl-confirm button')]
        .find(b => b.textContent === 'Да');
      if (yes) yes.click();
    }""")
    page.wait_for_timeout(1200)
    after_item = c.get("/api/supply/planning").json()
    check("новинка убрана с доски",
          not any(i["id"] == item_id for i in after_item["items"]))
    check("а системного confirm() по-прежнему не было", not dialogs, str(dialogs))


def _fix2_batch_delete(page, base, c, dialogs) -> None:
    """F-12 (решение владельца): партия удаляется с назначениями и возвращается."""
    print("\n== F-12: удаление партии называет последствие и обратимо ==")
    P = "/api/supply/planning"
    mat = c.post(P + "/materials", json={"title": "Сукно под партию", "qty": "300",
                                         "op_id": "ui-b-m"}).json()
    mat_id = [m for m in mat["materials"]
              if m["title"] == "Сукно под партию"][0]["id"]
    it = c.post(P + "/items", json={"kind": "draft", "title": "Китель-новинка",
                                    "op_id": "ui-b-i"}).json()
    item_id = [i for i in it["items"] if i["title"] == "Китель-новинка"][0]["id"]
    ba = c.post(P + "/batches", json={"item_id": item_id, "title": "Кители, партия",
                                      "plan_qty": "20", "op_id": "ui-b-b"}).json()
    batch_id = [b for b in ba["batches"]
                if b["title"] == "Кители, партия"][0]["id"]
    c.post(P + "/assignments", json={"material_id": mat_id, "batch_id": batch_id,
                                     "qty": "120", "note": "на воротники",
                                     "op_id": "ui-b-a"})
    page.goto(f"{base}/supply")
    page.wait_for_timeout(1100)
    close_hint(page)

    CARD = """
      const cards = [...document.querySelectorAll('#pl-batches .pl-card')];
      const card = cards.find(x => x.textContent.indexOf('Кители, партия') >= 0);
    """
    page.evaluate("""() => {
      %s
      if (!card) return;
      const btn = [...card.querySelectorAll('.pl-actions button')]
        .find(b => b.textContent === 'Удалить');
      if (btn) btn.click();
    }""" % CARD)
    page.wait_for_timeout(400)
    asked = page.evaluate("""() => {
      %s
      const box = card ? card.querySelector('.pl-confirm') : null;
      return box ? box.textContent : null;
    }""" % CARD)
    check("вопрос перед удалением партии НАЗЫВАЕТ последствие, а не просто «Удалить?»",
          asked is not None and "1 назначение" in asked and "снимется" in asked,
          str(asked))
    check("системного окна confirm() не было", not dialogs, str(dialogs))
    check("до подтверждения партия на месте",
          any(b["id"] == batch_id for b in c.get(P).json()["batches"]))

    page.evaluate("""() => {
      %s
      if (!card) return;
      const yes = [...card.querySelectorAll('.pl-confirm button')]
        .find(b => b.textContent === 'Да');
      if (yes) yes.click();
    }""" % CARD)
    page.wait_for_timeout(1300)
    board = c.get(P).json()
    gone = not any(b["id"] == batch_id for b in board["batches"])
    check("после «Да» партии на сервере нет", gone)
    freed = [m for m in board["materials"] if m["id"] == mat_id]
    check("КП F-12: назначенное у материала уменьшилось на экранных данных",
          gone and freed and freed[0]["assigned"] == 0.0,
          str(freed[0]["assigned"]) if freed else "материала нет")
    on_screen = page.evaluate("""() => {
      const cards = [...document.querySelectorAll('#pl-batches .pl-card')];
      return cards.some(x => x.textContent.indexOf('Кители, партия') >= 0);
    }""")
    check("и карточки партии на экране нет", on_screen is False, str(on_screen))
    toast = page.evaluate("""() => {
      const t = [...document.querySelectorAll('#toast-root .toast')]
        .find(x => x.textContent.indexOf('Партия удалена') >= 0);
      if (!t) return null;
      const btn = t.querySelector('.pl-toast-act');
      return {text: t.textContent, action: btn ? btn.textContent : null};
    }""")
    check("тост честно называет освободившийся метраж",
          toast and "120" in toast.get("text", "")
          and "свободный остаток" in toast.get("text", ""), str(toast))
    check("и предлагает вернуть", toast and toast.get("action") == "Вернуть",
          str(toast))

    page.evaluate("""() => {
      const t = [...document.querySelectorAll('#toast-root .toast')]
        .find(x => x.textContent.indexOf('Партия удалена') >= 0);
      const btn = t ? t.querySelector('.pl-toast-act') : null;
      if (btn) btn.click();
    }""")
    page.wait_for_timeout(1400)
    back = c.get(P).json()
    row = [b for b in back["batches"] if b["id"] == batch_id]
    check("«Вернуть» возвращает партию", gone and bool(row),
          f"уходила={gone} вернулась={bool(row)}")
    # ОБЕ ПРОВЕРКИ ПРИВЯЗАНЫ К `gone`. На дереве без этого пакета партия не
    # удаляется вовсе, назначение с неё никуда не девается — и «вернулось то же
    # самое» зеленело бы, не доказав ничего.
    check("вместе с ТЕМ ЖЕ назначением и ТОЙ ЖЕ заметкой",
          gone and row and len(row[0]["assignments"]) == 1
          and row[0]["assignments"][0]["qty"] == 120.0
          and row[0]["assignments"][0]["note"] == "на воротники",
          f"уходила={gone} " + (str(row[0]["assignments"])[:130] if row else ""))
    again = [m for m in back["materials"] if m["id"] == mat_id]
    check("и метраж снова в распределении",
          gone and again and again[0]["assigned"] == 120.0,
          f"уходила={gone} " + (str(again[0]["assigned"]) if again else ""))
    said = page.evaluate("""() => [...document.querySelectorAll('#toast-root .toast')]
      .some(x => x.textContent.indexOf('снова в распределении') >= 0)""")
    check("человеку сказано, что метраж вернулся в распределение",
          said is True, str(said))


def _fix2_catalog_confirm(page, base, c) -> None:
    """Решение владельца: архивную модель каталога возвращает только подтверждение."""
    print("\n== F-12: «Модель в архиве» и отдельное подтверждение в браузере ==")
    P = "/api/supply/planning"
    board = c.post(P + "/items", json={"kind": "catalog",
                                       "base_name": "Тренч «Классика»",
                                       "op_id": "ui-c-i"}).json()
    cat = [i for i in board.get("items", [])
           if i.get("base_name") == "Тренч «Классика»"]
    check("каталожная модель для проверки заведена", bool(cat), str(board)[:120])
    if not cat:
        return
    cid = cat[0]["id"]
    r = c.post(P + f"/items/{cid}/archive",
               json={"rev": cat[0]["rev"], "op_id": "ui-c-a"})
    check("модель убрана из плана", r.status_code == 200, str(r.status_code))

    page.goto(f"{base}/supply")
    page.wait_for_timeout(1100)
    close_hint(page)
    page.click("#pl-add-item")
    page.wait_for_timeout(400)
    page.fill("#pl-item-base", "тренч")
    page.wait_for_timeout(900)
    marked = page.evaluate("""() => {
      const opts = [...document.querySelectorAll('#pl-item-base-list .pl-combo-opt')];
      const o = opts.find(x => x.textContent.indexOf('Тренч') >= 0);
      if (!o) return null;
      const tag = [...o.querySelectorAll('.pl-tag')]
        .find(t => t.textContent.indexOf('в архиве') >= 0);
      return {found: true, tagged: !!tag};
    }""")
    check("в подсказке каталога модель помечена «в архиве»",
          marked and marked.get("tagged") is True, str(marked))

    page.evaluate("""() => {
      const opts = [...document.querySelectorAll('#pl-item-base-list .pl-combo-opt')];
      const o = opts.find(x => x.textContent.indexOf('Тренч') >= 0);
      if (o) o.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
    }""")
    page.wait_for_timeout(400)
    note = page.evaluate("""() => {
      const n = document.getElementById('pl-item-base-note');
      if (!n) return null;
      const cb = document.getElementById('pl-item-restore');
      const cs = getComputedStyle(n);
      return {text: n.textContent, checkbox: !!cb, display: cs.display,
              checked: cb ? cb.checked : null};
    }""")
    check("после выбора видно «Модель в архиве» и отдельное подтверждение",
          note and "Модель в архиве" in note.get("text", "")
          and note.get("checkbox") is True, str(note)[:180])
    check("подтверждение по умолчанию НЕ проставлено",
          note and note.get("checked") is False, str(note))
    check("и сама подпись видима", note and note.get("display") != "none",
          str(note))

    page.evaluate("""() => {
      const f = document.getElementById('pl-item-form');
      if (f) f.querySelector('button[type=submit]').click();
    }""")
    page.wait_for_timeout(1300)
    check("отправка БЕЗ подтверждения модель не вернула",
          not [i for i in c.get(P).json()["items"] if i["id"] == cid])
    err = page.evaluate("""() => {
      const e = document.getElementById('pl-item-err');
      return e ? e.textContent : null;
    }""")
    check("и человек видит причину у формы",
          err and "в архиве" in err, str(err)[:160])

    page.evaluate("""() => {
      const cb = document.getElementById('pl-item-restore');
      if (cb) { cb.checked = true; cb.dispatchEvent(new Event('change', {bubbles: true})); }
    }""")
    page.wait_for_timeout(200)
    page.evaluate("""() => {
      const f = document.getElementById('pl-item-form');
      if (f) f.querySelector('button[type=submit]').click();
    }""")
    page.wait_for_timeout(1400)
    restored = [i for i in c.get(P).json()["items"] if i["id"] == cid]
    check("после подтверждения модель вернулась ТОЙ ЖЕ строкой",
          bool(restored), "" if restored else "модель не вернулась")
    check("и второй строки той же модели не появилось",
          len([i for i in c.get(P).json()["items"]
               if i.get("base_name") == "Тренч «Классика»"]) == 1)


def supply_fix_3_ui(pw, base, c) -> None:
    """SUPPLY-FIX-3 в настоящем браузере: F-16, F-18, F-20 и весь F-21.

    ПОЧЕМУ ЭТИ ЧЕТЫРЕ ЗДЕСЬ, А ДВА ДРУГИХ — НЕТ. F-17 и F-19 живут целиком на
    сервере: строгий разбор и частичная правка срока проверяются ответом ручки,
    и браузер к ним ничего не добавляет. А F-21 наоборот НЕ проверяем ничем,
    кроме браузера: «набранное не пропало» — это состояние DOM после
    перерисовки, и по HTML его не увидеть.

    ПОВЕДЕНИЕ, А НЕ РАЗМЕТКА: тип элемента берётся у самого узла,
    видимость — из `getComputedStyle`, положение — из `getBoundingClientRect`,
    фокус — из `document.activeElement`.

    Каждый шаг отдельный по той же причине, что в пакетах 1 и 2: прогон против
    дерева без правки обязан сказать про КАЖДЫЙ пункт, а не умереть на первом.
    """
    browser = pw.chromium.launch()
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    ctx.add_cookies([{"name": k, "value": v, "domain": "127.0.0.1", "path": "/"}
                     for k, v in c.cookies.items()])
    errors: list[str] = []
    page = ctx.new_page()
    page.on("pageerror", lambda e: errors.append(str(e)))

    steps = (
        ("F-16", lambda: _fix3_units(page, base)),
        ("F-18", lambda: _fix3_format_ui(page, base, c)),
        ("F-20", lambda: _fix3_texts_ui(page, base, c)),
        ("F-21", lambda: _fix3_forms(page, base)),
        # Корректив 1 по REVIEW_REJECT: восстановленная форма обязана
        # описывать своё состояние, а «Сохранить» — работать.
        ("P1 срок", lambda: _fix3_restore_case(page, base, c, "desktop")),
        ("P1 единица", lambda: _fix3_restore_unit(page, base, c, "desktop")),
        ("P1 сосед", lambda: _fix3_restore_plain(page, base, c, "desktop")),
        # Корректив 2: воспроизведённые P1 внешних тредов.
        ("P1 пустая единица", lambda: _fix3_empty_unit_ui(page, base, c, "desktop")),
        ("P1 потерянный ответ", lambda: _fix3_lost_response(page, base, c)),
        # Корректив 3: черновик обязан держаться за СВОЮ редакцию.
        ("P1 черновик материала", lambda: _fix3_stale_material(page, base, c, "desktop")),
        ("P1 черновик вещи", lambda: _fix3_stale_item(page, base, c, "desktop")),
        ("P1 черновик партии", lambda: _fix3_stale_batch(page, base, c, "desktop")),
        ("P1 черновик переноса", lambda: _fix3_stale_move(page, base, c, "desktop")),
        ("сторож своей записи", lambda: _fix3_own_save_rebase(page, base, c, "desktop")),
        # Корректив 4: повтор поступка не воскрешает старый черновик.
        ("P1 повтор и правка", lambda: _fix3_replay_then_edit(page, base, c, "desktop")),
        ("сторож чужой правки после коммита",
         lambda: _fix3_peer_after_commit(page, base, c, "desktop")),
    )
    for label, run_step in steps:
        try:
            run_step()
        except Exception as exc:  # noqa: BLE001 — важен отчёт, а не тип
            check(f"{label}: шаг дошёл до конца без исключения", False,
                  f"{type(exc).__name__}: "
                  f"{str(exc).strip().splitlines()[0][:160]}")

    check("за сценарий SUPPLY-FIX-3 не было ошибок в консоли",
          not errors, str(errors)[:200])
    ctx.close()
    try:
        _fix3_mobile(browser, base, c)
    except Exception as exc:  # noqa: BLE001
        check("F-16/F-21 на телефоне: шаг дошёл до конца без исключения", False,
              f"{type(exc).__name__}: {str(exc).strip().splitlines()[0][:160]}")
    browser.close()


def supply_fix_4_ui(pw, base, c) -> None:
    """SUPPLY-FIX-4 в настоящем браузере: F-23 и F-24 на стороне страницы.

    ЧТО СЮДА ПОПАЛО И ПОЧЕМУ ИМЕННО ЭТО. Три вещи не видны ниоткуда, кроме
    браузера: (1) порядок запросов при сохранении новинки с эскизом — по ответу
    ручки его не увидеть вовсе; (2) отсутствие повторной перекачки картинки при
    перерисовке — это счётчик сетевых запросов, а не строка HTML; (3) «История»
    как элемент карточки. Всё остальное из пакета живёт на сервере и проверено
    там (`tests/test_supply_planning.py`).

    ЧЕГО ЗДЕСЬ НЕТ: ни одной параллельной вкладки и ни одного состязательного
    сценария. F-22 в пакет не входит.
    """
    browser = pw.chromium.launch()
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    ctx.add_cookies([{"name": k, "value": v, "domain": "127.0.0.1", "path": "/"}
                     for k, v in c.cookies.items()])
    errors: list[str] = []
    page = ctx.new_page()
    page.on("pageerror", lambda e: errors.append(str(e)))

    steps = (
        ("F-23 порядок", lambda: _fix4_order_ui(page, base, c)),
        ("F-23 кэш картинки", lambda: _fix4_no_refetch_ui(page, base)),
        ("F-24 история", lambda: _fix4_history_ui(page, base, c)),
        # Корректив по независимому ревью PR #57: отказ прикрепления эскиза
        # не должен заводить вторую новинку.
        ("P1 повтор после отказа эскиза",
         lambda: _fix4_retry_after_sketch_fail(page, base, c)),
    )
    for label, run_step in steps:
        try:
            run_step()
        except Exception as exc:  # noqa: BLE001 — важен отчёт, а не тип
            check(f"{label}: шаг дошёл до конца без исключения", False,
                  f"{type(exc).__name__}: "
                  f"{str(exc).strip().splitlines()[0][:160]}")

    check("за сценарий SUPPLY-FIX-4 не было ошибок в консоли",
          not errors, str(errors)[:200])
    ctx.close()
    browser.close()


def _fix4_order_ui(page, base, c) -> None:
    """F-23а: сначала вещь, потом файл — и порядок виден по самим запросам."""
    print("\n== F-23: эскиз уходит ПОСЛЕ создания вещи, а не до ==")
    _open_plan(page, base)
    seen: list[str] = []
    page.on("request", lambda r: seen.append(r.method + " " + r.url)
            if "/api/supply/planning/" in r.url else None)

    page.click("#pl-add-item")
    page.wait_for_timeout(250)
    page.select_option("#pl-item-kind", "draft")
    page.wait_for_timeout(150)

    # ПУСТОЕ НАЗВАНИЕ: страница обязана отказать САМА и не отправить ни байта.
    # Это и есть весь пункт: раньше файл уходил первым, и отказ сервера уже
    # ничего не менял — картинка лежала в базе.
    page.set_input_files("#pl-item-sketch", {
        "name": "sketch.png", "mimeType": "image/png",
        "buffer": base64.b64decode(VALID_PNG_B64)})
    before = len([u for u in seen if "POST" in u])
    page.evaluate("""() => {
      const b = document.querySelector('#pl-item-form button[type=submit]');
      if (b) b.click();
    }""")
    page.wait_for_timeout(700)
    after = [u for u in seen if u.startswith("POST")]
    check("пустое название не отправило НИ ОДНОГО запроса",
          len(after) == before, str(after[-3:]))
    # ПРИЧИНУ НАЗЫВАЕТ БРАУЗЕР, И ЭТО НЕ ОБХОД ПРОВЕРКИ, А ЕЁ СМЫСЛ. Поле
    # объявлено обязательным (`required`), поэтому нажатие на «Сохранить» до
    # нашего обработчика вообще не доходит: форма не отправляется, и человек
    # видит родную подсказку у пустого поля. Спрашивать после этого наш
    # `#pl-item-err` значило бы требовать вторую ошибку там, где первая уже
    # остановила отправку.
    told = page.evaluate("""() => {
      const t = document.getElementById('pl-item-title');
      if (!t) return null;
      return {missing: t.validity.valueMissing, required: t.required,
              shown: getComputedStyle(t).display !== 'none'};
    }""")
    check("и человек видит причину у самого поля: оно обязательное и пустое",
          told and told["required"] and told["missing"] and told["shown"],
          str(told))

    seen.clear()
    page.fill("#pl-item-title", "Новинка-порядок")
    page.evaluate("""() => {
      const b = document.querySelector('#pl-item-form button[type=submit]');
      if (b) b.click();
    }""")
    page.wait_for_timeout(2500)
    posts = [u for u in seen if u.startswith("POST")]
    items = [i for i, u in enumerate(posts) if u.endswith("/items")]
    sketches = [i for i, u in enumerate(posts) if "/sketch" in u]
    check("оба запроса ушли", bool(items) and bool(sketches), str(posts))
    check("вещь создана ПЕРВОЙ, файл прикреплён ВТОРЫМ",
          bool(items) and bool(sketches) and items[0] < sketches[0], str(posts))
    check("к старой ручке загрузки страница больше не ходит",
          not any(u.rstrip("/").endswith("/sketches") for u in posts), str(posts))

    board = c.get("/api/supply/planning").json()
    made = [i for i in board["items"] if i["title"] == "Новинка-порядок"]
    check("вещь на месте и с эскизом",
          bool(made) and made[0]["sketch_id"] is not None,
          str(made[:1])[:160])


def _fix4_no_refetch_ui(page, base) -> None:
    """F-23б,в: карточка показывает миниатюру и не перекачивает её заново."""
    print("\n== F-23: миниатюра в карточке и ни одного повторного запроса ==")
    _open_plan(page, base)
    # Картинке дают ЗАГРУЗИТЬСЯ прежде, чем её считать: `loading="lazy"` и
    # обычная сеть означают, что сразу после `goto` она ещё в пути, и «не
    # нарисована» тогда сказало бы о моменте замера, а не о продукте.
    #
    # Сначала картинку ПОКАЗЫВАЮТ: у неё `loading="lazy"`, и пока карточка
    # партии ниже сгиба, браузер её не запрашивает вовсе. Без прокрутки
    # проверка «повторных запросов нет» была бы зелёной ни на чём — запросов не
    # было бы и в первый раз.
    page.evaluate("""() => {
      const i = document.querySelector('#pl-batches img.pl-sketch');
      if (i && i.scrollIntoView) i.scrollIntoView({block: 'center'});
    }""")
    page.wait_for_timeout(400)
    loaded = True
    try:
        page.wait_for_function(
            "() => { const i = document.querySelector('#pl-batches img.pl-sketch');"
            " return !!i && i.complete && i.naturalWidth > 0; }", timeout=8000)
    except Exception:  # noqa: BLE001 — важен отчёт, а не тип
        loaded = False
    check("миниатюра успела загрузиться", loaded)
    shown = page.evaluate("""() => {
      const a = document.querySelector('#pl-batches a.pl-sketch-link');
      const i = document.querySelector('#pl-batches img.pl-sketch');
      if (!i) return null;
      const box = i.getBoundingClientRect();
      return {src: i.getAttribute('src'), href: a ? a.getAttribute('href') : '',
              target: a ? a.getAttribute('target') : '',
              w: Math.round(box.width), h: Math.round(box.height),
              natural: i.naturalWidth};
    }""")
    check("в карточке партии показана миниатюра, а не оригинал",
          shown and shown["src"].endswith("/thumb"), str(shown))
    check("картинка действительно нарисована браузером",
          shown and shown["natural"] > 0 and shown["w"] > 0, str(shown))
    check("полный размер открывается по клику отдельной вкладкой",
          shown and shown["href"].startswith("/api/supply/planning/sketches/")
          and not shown["href"].endswith("/thumb")
          and shown["target"] == "_blank", str(shown))

    # СЧЁТЧИК СЕТЕВЫХ ЗАПРОСОВ, А НЕ РАЗМЕТКА. `render()` пересоздаёт `<img>`
    # каждый раз, и до этого пакета каждая перерисовка означала повторную
    # загрузку картинки: ответ приходил с `no-store`. Теперь ответ приватно
    # кэшируется на сутки, и повторная перерисовка сети не касается.
    #
    # Перерисовка вызывается ТАК, КАК ЕЁ ВЫЗЫВАЕТ ЧЕЛОВЕК: сохранением в другом
    # месте экрана. Дёргать `render()` напрямую было бы нечем — страница
    # наружу его не отдаёт, и выставлять его наружу ради проверки значило бы
    # менять продукт под тест.
    hits: list[str] = []
    page.on("request", lambda r: hits.append(r.url)
            if "/api/supply/planning/sketches/" in r.url else None)
    for _ in range(3):
        opened = page.evaluate("""() => {
          const card = document.querySelector('[data-pl="material"]');
          if (!card) return false;
          const b = card.querySelector('button[data-inline="edit"]');
          if (!b) return false;
          b.click();
          return true;
        }""")
        if not opened:
            break
        page.wait_for_timeout(250)
        page.evaluate("""() => {
          const f = document.querySelector('#pl-materials .pl-form.inline');
          const b = f ? f.querySelector('button[type=submit]') : null;
          if (b) b.click();
        }""")
        page.wait_for_timeout(900)
    redrawn = page.evaluate("""() => {
      const i = document.querySelector('#pl-batches img.pl-sketch');
      return i ? {src: i.getAttribute('src'), natural: i.naturalWidth} : null;
    }""")
    check("после трёх перерисовок картинка на месте и нарисована",
          redrawn and redrawn["natural"] > 0, str(redrawn))
    check("и ни одного повторного запроса за картинкой не ушло",
          not hits, str(hits[:3]))


def _fix4_retry_after_sketch_fail(page, base, c) -> None:
    """P1 ревью: отказ эскиза не заводит вторую новинку, и повтор доделывает.

    Порядок «сначала вещь, потом файл» (F-23а) закрыл сирот в базе — и открыл
    другое: первый запрос уже коммитит новинку, а отказ второго оставляет
    человека перед формой. Любая правка поля честно сбрасывает идентичность
    поступка, и повтор уходил как НОВОЕ создание.

    После десяти раундов ревью форма ведёт себя иначе, и проверяется именно
    это: как только вещь создана, поля становятся только для чтения, а повтор
    отправляет ТОЛЬКО файл. Дерева состояний «правь что угодно между попытками»
    больше нет — вместе с классом ошибок, который оно порождало.
    """
    print("\n== P1: отказ эскиза не удваивает новинку ==")
    _open_plan(page, base)
    title = "Новинка-повтор"

    failed = {"n": 0}

    def deny(route):
        failed["n"] += 1
        route.fulfill(status=400, content_type="application/json",
                      body='{"detail":"Эскиз не принят."}')

    page.route(re.compile(r"/api/supply/planning/items/\d+/sketch$"), deny)

    page.click("#pl-add-item")
    page.wait_for_timeout(250)
    page.select_option("#pl-item-kind", "draft")
    page.wait_for_timeout(150)
    page.fill("#pl-item-title", title)
    page.fill("#pl-item-note", "заметка новинки")
    page.set_input_files("#pl-item-sketch", {
        "name": "sketch.png", "mimeType": "image/png",
        "buffer": base64.b64decode(VALID_PNG_B64)})
    page.evaluate("""() => {
      const b = document.querySelector('#pl-item-form button[type=submit]');
      if (b) b.click();
    }""")
    page.wait_for_timeout(2500)
    check("прикрепление действительно отвергнуто", failed["n"] >= 1,
          str(failed["n"]))
    err = page.evaluate("""() => {
      const box = document.getElementById('pl-item-err');
      return box ? box.textContent : '';
    }""")
    check("человеку сказано, что вещь уже сохранена",
          "уже сохранена" in err, err[:160])
    board = c.get("/api/supply/planning").json()
    made = [i for i in board["items"] if i["title"] == title]
    check("после отказа новинка ровно одна", len(made) == 1,
          f"строк: {len(made)}")

    # РЕЖИМ ПРИКРЕПЛЕНИЯ ВИДЕН ГЛАЗАМИ, А НЕ ТОЛЬКО ЗАЛОЖЕН В КОДЕ. Поля
    # показывают сохранённое и не правятся, вид вещи переключить нельзя,
    # подсказка объясняет почему.
    mode = page.evaluate("""() => {
      const t = document.getElementById('pl-item-title');
      const n = document.getElementById('pl-item-note');
      const k = document.getElementById('pl-item-kind');
      const h = document.getElementById('pl-item-attach-hint');
      return {title: t ? t.value : null, titleRO: t ? t.readOnly : null,
              note: n ? n.value : null, noteRO: n ? n.readOnly : null,
              kindOff: k ? k.disabled : null,
              hint: h ? (!h.hidden && getComputedStyle(h).display !== 'none') : null,
              hintText: h ? h.textContent : ''};
    }""")
    check("поля показывают сохранённое и стали только для чтения",
          mode and mode["titleRO"] and mode["noteRO"]
          and mode["title"] == title and mode["note"] == "заметка новинки",
          str(mode)[:200])
    check("вид вещи в этом состоянии не переключается",
          bool(mode and mode["kindOff"]), str(mode and mode["kindOff"]))
    check("и подсказка объясняет, что осталось сделать",
          bool(mode and mode["hint"]) and "прикрепить эскиз" in (mode["hintText"] or ""),
          (mode["hintText"] or "")[:120] if mode else "нет подсказки")

    # Человек выбирает другой файл и сохраняет: уходит ТОЛЬКО прикрепление.
    page.unroute(re.compile(r"/api/supply/planning/items/\d+/sketch$"))
    posts = []
    page.on("request", lambda r: posts.append(r.url)
            if r.method == "POST" and "/api/supply/planning/" in r.url else None)
    page.set_input_files("#pl-item-sketch", {
        "name": "second.png", "mimeType": "image/png",
        "buffer": base64.b64decode(VALID_PNG_B64)})
    page.wait_for_timeout(200)
    page.evaluate("""() => {
      const b = document.querySelector('#pl-item-form button[type=submit]');
      if (b) b.click();
    }""")
    page.wait_for_timeout(2500)
    board = c.get("/api/supply/planning").json()
    made = [i for i in board["items"] if i["title"] == title]
    check("после удачного повтора новинка ВСЁ ЕЩЁ одна", len(made) == 1,
          f"строк: {len(made)} — {[i['id'] for i in made]}")
    check("и эскиз прикреплён именно к ней",
          len(made) == 1 and made[0]["sketch_id"] is not None,
          str(made[0]["sketch_id"]) if made else "строки нет")
    check("повтор отправил только прикрепление, без второго создания",
          all("/sketch" in u for u in posts), str(posts))
    check("заметка, набранная до создания, на месте",
          len(made) == 1 and made[0]["note"] == "заметка новинки",
          str(made[0]["note"]) if made else "строки нет")
    check("форма закрылась — работа доведена до конца",
          page.evaluate("() => document.getElementById('pl-item-form').hidden")
          is True)

    # ПАМЯТЬ О НАЧАТОМ НЕ ПЕРЕЖИВАЕТ ЗАКРЫТИЕ ФОРМЫ. Ревью нашло путь: после
    # отказа закрыть форму кнопкой и завести СЛЕДУЮЩУЮ новинку — прежде она
    # переписывала брошенную строку вместо создания своей.
    fail2 = {"n": 0}
    page.route(re.compile(r"/api/supply/planning/items/\d+/sketch$"),
               lambda route: (fail2.__setitem__("n", fail2["n"] + 1),
                              route.fulfill(status=400,
                                            content_type="application/json",
                                            body='{"detail":"Эскиз не принят."}')))
    page.click("#pl-add-item")
    page.wait_for_timeout(250)
    page.select_option("#pl-item-kind", "draft")
    page.wait_for_timeout(150)
    page.fill("#pl-item-title", "Брошенная-новинка")
    page.set_input_files("#pl-item-sketch", {
        "name": "bad.png", "mimeType": "image/png",
        "buffer": base64.b64decode(VALID_PNG_B64)})
    page.evaluate("""() => {
      const b = document.querySelector('#pl-item-form button[type=submit]');
      if (b) b.click();
    }""")
    page.wait_for_timeout(2500)
    check("вторая новинка тоже создана и её эскиз отвергнут", fail2["n"] >= 1,
          str(fail2["n"]))
    page.unroute(re.compile(r"/api/supply/planning/items/\d+/sketch$"))
    # Закрываем форму кнопкой — это отказ от начатого.
    page.click("#pl-add-item")
    page.wait_for_timeout(400)
    page.click("#pl-add-item")
    page.wait_for_timeout(300)
    page.select_option("#pl-item-kind", "draft")
    page.wait_for_timeout(150)
    page.fill("#pl-item-title", "Совсем-другая-новинка")
    page.set_input_files("#pl-item-sketch", {
        "name": "good.png", "mimeType": "image/png",
        "buffer": base64.b64decode(VALID_PNG_B64)})
    page.evaluate("""() => {
      const b = document.querySelector('#pl-item-form button[type=submit]');
      if (b) b.click();
    }""")
    page.wait_for_timeout(2500)
    board = c.get("/api/supply/planning").json()
    dropped = [i for i in board["items"] if i["title"] == "Брошенная-новинка"]
    fresh = [i for i in board["items"] if i["title"] == "Совсем-другая-новинка"]
    check("брошенная новинка осталась собой, а не переписана следующей",
          len(dropped) == 1, f"строк: {len(dropped)}")
    check("и у неё по-прежнему нет эскиза — она его не получала",
          len(dropped) == 1 and dropped[0]["sketch_id"] is None,
          str(dropped[0]["sketch_id"]) if dropped else "строки нет")
    check("новая новинка завелась своей строкой", len(fresh) == 1,
          f"строк: {len(fresh)}")
    check("и эскиз достался именно ей",
          len(fresh) == 1 and fresh[0]["sketch_id"] is not None,
          str(fresh[0]["sketch_id"]) if fresh else "строки нет")

    # ПУТЬ БЕЗ ФАЙЛА: после отказа человек убирает файл и сохраняет вещь без
    # эскиза. Это законный исход, и вторая строка здесь так же не нужна.
    fail3 = {"n": 0}
    page.route(re.compile(r"/api/supply/planning/items/\d+/sketch$"),
               lambda route: (fail3.__setitem__("n", fail3["n"] + 1),
                              route.fulfill(status=400,
                                            content_type="application/json",
                                            body='{"detail":"Эскиз не принят."}')))
    page.click("#pl-add-item")
    page.wait_for_timeout(250)
    page.select_option("#pl-item-kind", "draft")
    page.wait_for_timeout(150)
    page.fill("#pl-item-title", "Новинка-без-файла")
    page.set_input_files("#pl-item-sketch", {
        "name": "bad2.png", "mimeType": "image/png",
        "buffer": base64.b64decode(VALID_PNG_B64)})
    page.evaluate("""() => {
      const b = document.querySelector('#pl-item-form button[type=submit]');
      if (b) b.click();
    }""")
    page.wait_for_timeout(2500)
    check("третья новинка создана, её эскиз отвергнут", fail3["n"] >= 1,
          str(fail3["n"]))
    page.unroute(re.compile(r"/api/supply/planning/items/\d+/sketch$"))
    page.set_input_files("#pl-item-sketch", [])
    page.wait_for_timeout(200)
    page.evaluate("""() => {
      const b = document.querySelector('#pl-item-form button[type=submit]');
      if (b) b.click();
    }""")
    page.wait_for_timeout(2000)
    board = c.get("/api/supply/planning").json()
    plain = [i for i in board["items"] if i["title"] == "Новинка-без-файла"]
    check("сохранение без файла не завело вторую строку", len(plain) == 1,
          f"строк: {len(plain)}")
    check("форма закрыта и после пути без файла",
          page.evaluate("() => document.getElementById('pl-item-form').hidden")
          is True)

    # ПОТЕРЯННЫЙ ОТВЕТ ПРИКРЕПЛЕНИЯ: сервер записал эскиз, страница не узнала.
    # Человек убирает файл и сохраняет — писать нечего, но экран обязан
    # сойтись с данными, а не закрыться на доске без картинки (`AGENTS.md` §3).
    def lose(route):
        try:
            route.fetch()
        finally:
            route.abort()

    page.route(re.compile(r"/api/supply/planning/items/\d+/sketch$"), lose)
    page.click("#pl-add-item")
    page.wait_for_timeout(250)
    page.select_option("#pl-item-kind", "draft")
    page.wait_for_timeout(150)
    page.fill("#pl-item-title", "Новинка-расхождение")
    page.set_input_files("#pl-item-sketch", {
        "name": "lost.png", "mimeType": "image/png",
        "buffer": base64.b64decode(VALID_PNG_B64)})
    page.evaluate("""() => {
      const b = document.querySelector('#pl-item-form button[type=submit]');
      if (b) b.click();
    }""")
    page.wait_for_timeout(2500)
    page.unroute(re.compile(r"/api/supply/planning/items/\d+/sketch$"))
    board = c.get("/api/supply/planning").json()
    diverged = [i for i in board["items"] if i["title"] == "Новинка-расхождение"]
    check("сервер прикрепил эскиз, хотя ответ не дошёл",
          len(diverged) == 1 and diverged[0]["sketch_id"] is not None,
          str(diverged[:1])[:160])
    page.set_input_files("#pl-item-sketch", [])
    page.wait_for_timeout(200)
    reads = []
    page.on("request", lambda r: reads.append(r.url)
            if r.method == "GET" and r.url.endswith("/api/supply/planning")
            else None)
    page.evaluate("""() => {
      const b = document.querySelector('#pl-item-form button[type=submit]');
      if (b) b.click();
    }""")
    page.wait_for_timeout(2500)
    check("страница сверилась с сервером, а не закрылась на устаревшем",
          len(reads) >= 1, f"чтений доски: {len(reads)}")
    check("и форма всё-таки закрыта — работа доведена до конца",
          page.evaluate("() => document.getElementById('pl-item-form').hidden")
          is True)
    board = c.get("/api/supply/planning").json()
    diverged = [i for i in board["items"] if i["title"] == "Новинка-расхождение"]
    check("строка одна, и эскиз на ней остался",
          len(diverged) == 1 and diverged[0]["sketch_id"] is not None,
          str(diverged[:1])[:160])

    # ЧУЖАЯ ПРАВКА ВО ВРЕМЯ ОТКРЫТОЙ ФОРМЫ. После перестройки формы этот случай
    # закрыт по построению: форма полей больше не пишет, значит и затирать ей
    # нечем. Проверяется именно это — ни одного запроса правки и целая чужая
    # заметка.
    page.route(re.compile(r"/api/supply/planning/items/\d+/sketch$"),
               lambda route: route.fulfill(status=400,
                                           content_type="application/json",
                                           body='{"detail":"Эскиз не принят."}'))
    page.click("#pl-add-item")
    page.wait_for_timeout(250)
    page.select_option("#pl-item-kind", "draft")
    page.wait_for_timeout(150)
    page.fill("#pl-item-title", "Новинка-сосед")
    page.set_input_files("#pl-item-sketch", {
        "name": "bad3.png", "mimeType": "image/png",
        "buffer": base64.b64decode(VALID_PNG_B64)})
    page.evaluate("""() => {
      const b = document.querySelector('#pl-item-form button[type=submit]');
      if (b) b.click();
    }""")
    page.wait_for_timeout(2500)
    page.unroute(re.compile(r"/api/supply/planning/items/\d+/sketch$"))
    board = c.get("/api/supply/planning").json()
    peer = [i for i in board["items"] if i["title"] == "Новинка-сосед"]
    check("новинка для проверки соседа создана", len(peer) == 1,
          f"строк: {len(peer)}")
    if len(peer) == 1:
        c.post(f"/api/supply/planning/items/{peer[0]['id']}/update",
               json={"note": "заметка соседа", "rev": peer[0]["rev"],
                     "op_id": "ui-peer-1"})
        updates = []
        page.on("request", lambda r: updates.append(r.url)
                if r.method == "POST" and r.url.endswith("/update") else None)
        page.set_input_files("#pl-item-sketch", {
            "name": "peer.png", "mimeType": "image/png",
            "buffer": base64.b64decode(VALID_PNG_B64)})
        page.wait_for_timeout(200)
        page.evaluate("""() => {
          const b = document.querySelector('#pl-item-form button[type=submit]');
          if (b) b.click();
        }""")
        page.wait_for_timeout(2500)
        check("форма не отправила ни одной правки полей",
              not updates, str(updates))
        after = [i for i in c.get("/api/supply/planning").json()["items"]
                 if i["id"] == peer[0]["id"]]
        check("заметка соседа осталась целой",
              bool(after) and after[0]["note"] == "заметка соседа",
              str(after[0]["note"]) if after else "строки нет")
        check("а эскиз всё-таки прикреплён",
              bool(after) and after[0]["sketch_id"] is not None,
              str(after[0]["sketch_id"]) if after else "строки нет")

    # ПОТЕРЯН ОТВЕТ САМОГО ПЕРВОГО ЗАПРОСА — СОЗДАНИЯ ВЕЩИ. Вещь на сервере
    # есть, а страница о ней не знает: номера в руках нет. Повтор обязан
    # прийти под ТОЙ ЖЕ идентичностью поступка, чтобы замок его узнал, — а
    # значит выбор другого файла эту идентичность сбрасывать не должен: тело
    # создания файла не содержит вовсе.
    page.evaluate("""() => {
      const f = document.getElementById('pl-item-form');
      const b = document.getElementById('pl-add-item');
      if (f && !f.hidden && b) b.click();
    }""")
    page.wait_for_timeout(300)
    page.route(re.compile(r"/api/supply/planning/items$"), lose)
    page.click("#pl-add-item")
    page.wait_for_timeout(250)
    page.select_option("#pl-item-kind", "draft")
    page.wait_for_timeout(150)
    page.fill("#pl-item-title", "Новинка-потеря-создания")
    page.set_input_files("#pl-item-sketch", {
        "name": "first.png", "mimeType": "image/png",
        "buffer": base64.b64decode(VALID_PNG_B64)})
    page.evaluate("""() => {
      const b = document.querySelector('#pl-item-form button[type=submit]');
      if (b) b.click();
    }""")
    page.wait_for_timeout(2500)
    page.unroute(re.compile(r"/api/supply/planning/items$"))
    born = [i for i in c.get("/api/supply/planning").json()["items"]
            if i["title"] == "Новинка-потеря-создания"]
    check("сервер создал вещь, хотя ответ не дошёл", len(born) == 1,
          f"строк: {len(born)}")
    # Человек выбирает другую картинку и жмёт снова.
    page.set_input_files("#pl-item-sketch", {
        "name": "second.png", "mimeType": "image/png",
        "buffer": base64.b64decode(VALID_PNG_B64)})
    page.wait_for_timeout(200)
    page.evaluate("""() => {
      const b = document.querySelector('#pl-item-form button[type=submit]');
      if (b) b.click();
    }""")
    page.wait_for_timeout(2500)
    again = [i for i in c.get("/api/supply/planning").json()["items"]
             if i["title"] == "Новинка-потеря-создания"]
    check("второй новинки не появилось — повтор узнан по поступку",
          len(again) == 1, f"строк: {len(again)} — {[i['id'] for i in again]}")
    check("и эскиз прикреплён к той самой вещи",
          len(again) == 1 and again[0]["sketch_id"] is not None,
          str(again[0]["sketch_id"]) if again else "строки нет")


def _fix4_history_ui(page, base, c) -> None:
    """F-24: «История» на карточке показывает последнюю правку."""
    print("\n== F-24: история правок читается с карточки ==")
    mat = c.post("/api/supply/planning/materials",
                 json={"title": "Ткань-история-UI", "qty": "12", "unit": "м",
                       "op_id": "f4ui-m"}).json()
    mid = [m for m in mat["materials"] if m["title"] == "Ткань-история-UI"][0]["id"]
    rev = [m for m in mat["materials"] if m["id"] == mid][0]["rev"]
    c.post(f"/api/supply/planning/materials/{mid}/update",
           json={"title": "Ткань-история-UI-2", "rev": rev, "op_id": "f4ui-m2"})

    _open_plan(page, base)
    opened = page.evaluate("""(id) => {
      const card = document.querySelector('[data-pl="material"][data-id="' + id + '"]');
      if (!card) return false;
      const b = card.querySelector('button[data-inline="history"]');
      if (!b) return false;
      b.click();
      return true;
    }""", str(mid))
    check("кнопка «История» есть на карточке материала", opened is True)
    page.wait_for_timeout(1200)
    text = page.evaluate("""(id) => {
      const card = document.querySelector('[data-pl="material"][data-id="' + id + '"]');
      const box = card ? card.querySelector('.pl-hist') : null;
      return box ? box.textContent : '';
    }""", str(mid))
    check("история показывает поле, прежнее и новое значение",
          "название" in text and "Ткань-история-UI" in text
          and "Ткань-история-UI-2" in text, text[:200])
    check("и называет автора правки", "Владелец" in text, text[:200])

    struck = page.evaluate("""(id) => {
      const card = document.querySelector('[data-pl="material"][data-id="' + id + '"]');
      const old = card ? card.querySelector('.pl-hist-old') : null;
      return old ? getComputedStyle(old).textDecorationLine : '';
    }""", str(mid))
    check("прежнее значение зачёркнуто, а не выдано за текущее",
          "line-through" in struck, str(struck))

    # У партии кнопка та же и работает так же — иначе история жила бы у одной
    # сущности из двух названных ТЗ.
    on_batch = page.evaluate("""() => {
      const card = document.querySelector('[data-pl="batch"]');
      if (!card) return false;
      const b = card.querySelector('button[data-inline="history"]');
      if (!b) return false;
      b.click();
      return true;
    }""")
    check("кнопка «История» есть и на карточке партии", on_batch is True)
    page.wait_for_timeout(1200)
    btext = page.evaluate("""() => {
      const card = document.querySelector('[data-pl="batch"]');
      const box = card ? card.querySelector('.pl-hist') : null;
      return box ? box.textContent : '';
    }""")
    check("история партии тоже наполнилась", bool(btext.strip()), btext[:160])


def _open_plan(page, base) -> None:
    page.goto(f"{base}/supply")
    page.wait_for_timeout(1200)
    close_hint(page)


def _fix3_units(page, base) -> None:
    """F-16: единица выбирается из списка, «другое» открывает своё поле."""
    print("\n== F-16: единица — список из шести, «другое» открывает поле ==")
    _open_plan(page, base)
    page.click("#pl-add-material")
    page.wait_for_timeout(300)

    facts = page.evaluate("""() => {
      const node = document.getElementById('pl-mat-unit');
      const other = document.getElementById('pl-mat-unit-other');
      return {
        tag: node ? node.tagName : 'НЕТ',
        options: (node && node.options)
          ? [...node.options].map(o => o.value) : [],
        otherExists: !!other,
        otherShown: other
          ? getComputedStyle(other.parentNode).display !== 'none' : null
      };
    }""")
    check("единица — выпадающий список, а не свободное поле",
          facts["tag"] == "SELECT", str(facts))
    check("и в нём ровно шесть утверждённых вариантов",
          facts["options"] == ["м", "кг", "шт", "рул.", "компл.", "другое"],
          str(facts["options"]))
    check("поле своей единицы существует",
          facts["otherExists"] is True, str(facts))
    check("но по умолчанию скрыто — оно нужно только для «другое»",
          facts["otherShown"] is False, str(facts))

    shown = page.evaluate("""() => {
      const node = document.getElementById('pl-mat-unit');
      if (!node || node.tagName !== 'SELECT') return null;
      node.value = 'другое';
      node.dispatchEvent(new Event('change'));
      const other = document.getElementById('pl-mat-unit-other');
      return other ? getComputedStyle(other.parentNode).display !== 'none' : null;
    }""")
    check("выбор «другое» открывает поле своей единицы", shown is True, str(shown))

    # Возврат к единице из списка проверяется ДО отправки: удачное сохранение
    # закрывает и очищает форму, и после него спрашивать было бы уже не у чего.
    back = page.evaluate("""() => {
      const node = document.getElementById('pl-mat-unit');
      if (!node || node.tagName !== 'SELECT') return null;
      node.value = 'кг';
      node.dispatchEvent(new Event('change'));
      const other = document.getElementById('pl-mat-unit-other');
      return other ? getComputedStyle(other.parentNode).display !== 'none' : null;
    }""")
    check("возврат к единице из списка снова прячет своё поле",
          back is False, str(back))

    typed = page.evaluate("""() => {
      const sel = document.getElementById('pl-mat-unit');
      const t = document.getElementById('pl-mat-title');
      const q = document.getElementById('pl-mat-qty');
      const o = document.getElementById('pl-mat-unit-other');
      if (!sel || sel.tagName !== 'SELECT' || !t || !q || !o) return false;
      sel.value = 'другое';
      sel.dispatchEvent(new Event('change'));
      t.value = 'Тесьма Ф16';
      q.value = '7';
      o.value = 'ярд';
      return true;
    }""")
    if typed:
        page.click("#pl-mat-form button[type=submit]")
        page.wait_for_timeout(1300)
        text = page.text_content("#pl-materials") or ""
        check("своя единица сохраняется и показывается как написана",
              "Тесьма Ф16" in text and "7 ярд" in text, text[:200])
    else:
        check("своя единица сохраняется и показывается как написана", False,
              "поля своей единицы на странице нет")


#: Тот же адрес, что и у соседних блоков; локальная константа здесь затем,
#: чтобы шаги F-18 и F-20 не переписывали его строкой в каждом вызове.
P3 = "/api/supply/planning"


def _fix3_format_ui(page, base, c) -> None:
    """F-18: «1 августа 2026», «уже прошла» и запятая в дробном количестве."""
    print("\n== F-18: русская дата на карточке и запятая в числе ==")
    item = c.post(P3 + "/items", json={"kind": "draft", "title": "Плащ Ф18",
                                      "op_id": "f3ui-i"}).json()
    iid = [i for i in item["items"] if i["title"] == "Плащ Ф18"][0]["id"]
    c.post(P3 + "/batches", json={"item_id": iid, "title": "Августовская Ф18",
                                 "due_kind": "exact", "due_date": "2026-08-01",
                                 "due_source": "цех", "plan_qty": "12",
                                 "op_id": "f3ui-b"})
    c.post(P3 + "/materials", json={"title": "Фурнитура Ф18", "qty": "10.5",
                                   "unit": "кг", "op_id": "f3ui-m"})
    _open_plan(page, base)

    card = page.evaluate("""() => {
      const cards = [...document.querySelectorAll('#pl-batches .pl-card')];
      const one = cards.find(x => x.textContent.indexOf('Августовская Ф18') >= 0);
      if (!one) return null;
      const mark = one.querySelector('.pl-past');
      const title = one.querySelector('.t');
      return {
        text: one.textContent,
        markText: mark ? mark.textContent : '',
        markColor: mark ? getComputedStyle(mark).color : '',
        titleColor: title ? getComputedStyle(title).color : ''
      };
    }""")
    check("карточка партии со сроком 2026-08-01 нашлась", card is not None,
          "" if card else "карточки нет")
    if card:
        check("дата на карточке написана по-русски",
              "1 августа 2026" in card["text"], card["text"][:200])
        check("машинного вида даты на карточке нет",
              "2026-08-01" not in card["text"], card["text"][:200])
        check("прошедший срок помечен словами «уже прошла»",
              card["markText"] == "уже прошла", repr(card["markText"]))
        check("и пометка серая, а не того же цвета, что название",
              bool(card["markColor"]) and card["markColor"] != card["titleColor"],
              f"{card['markColor']} против {card['titleColor']}")

    mat = page.evaluate("""() => {
      const cards = [...document.querySelectorAll('#pl-materials .pl-card')];
      const one = cards.find(x => x.textContent.indexOf('Фурнитура Ф18') >= 0);
      return one ? one.textContent : null;
    }""")
    check("карточка дробного материала нашлась", mat is not None,
          "" if mat else "карточки нет")
    if mat:
        check("дробное количество показано с запятой",
              "10,5 кг" in mat, mat[:200])
        check("и точки как разделителя на карточке нет",
              "10.5" not in mat, mat[:200])


def _fix3_texts_ui(page, base, c) -> None:
    """F-20: один дисклеймер, утверждённые подписи, служебных слов нет."""
    print("\n== F-20: тезис про «Едет» ровно один раз, подписи по ТЗ ==")
    set_preview_flag(False)
    _open_plan(page, base)
    count = page.evaluate(
        "() => (document.body.textContent.match(/«Едет»/g) || []).length")
    check("без вкладки предпросмотра тезис про «Едет» встречается РОВНО раз",
          count == 1, f"встретился {count} раз")

    html = page.content()
    for banned in ("Метраж, ", "Уточнить количество", "конечным числом",
                   "Редакция должна"):
        check(f"строки «{banned}» на странице нет", banned not in html,
              banned)

    set_preview_flag(True)
    _open_plan(page, base)
    per_tab = page.evaluate("""() => {
      const one = document.getElementById('sup-view-plan');
      const two = document.getElementById('sup-view-preview');
      const n = el => el ? (el.textContent.match(/«Едет»/g) || []).length : -1;
      return [n(one), n(two)];
    }""")
    check("с двумя вкладками тезис стоит по одному разу на каждой",
          per_tab == [1, 1], str(per_tab))

    batches = page.text_content("#pl-batches") or ""
    check("бейджа «плановая партия» на карточках нет",
          "плановая партия" not in batches, batches[:200])
    check("а «новинка» осталась — она различает",
          "новинка" in batches, batches[:200])
    check("источник срока подписан человеческими словами",
          "кто назвал срок: цех" in batches, batches[:300])
    check("и служебного «источник:» на карточке больше нет",
          "источник: цех" not in batches, batches[:300])

    opened = page.evaluate("""() => {
      const cards = [...document.querySelectorAll('#pl-materials .pl-card')];
      const one = cards.find(x => x.textContent.indexOf('Фурнитура Ф18') >= 0);
      if (!one) return false;
      const btn = [...one.querySelectorAll('.pl-actions button')]
        .find(b => b.textContent === 'Назначить на партию');
      if (!btn) return false;
      btn.click();
      return true;
    }""")
    check("форма назначения открылась", opened is True, str(opened))
    if opened:
        page.wait_for_timeout(300)
        form = page.evaluate("""() => {
          const box = document.querySelector('#pl-materials .pl-form.inline');
          if (!box) return null;
          const submit = box.querySelector('button[type=submit]');
          return {labels: [...box.querySelectorAll('label')].map(l => l.textContent),
                  submit: submit ? submit.textContent : ''};
        }""")
        check("подпись количества названа «Сколько, <единица>»",
              bool(form) and any(l.startswith("Сколько, ") for l in form["labels"]),
              str(form))
        check("а кнопка отправки называется «Отдать»",
              bool(form) and form["submit"] == "Отдать", str(form))


def _fix3_forms(page, base) -> None:
    """F-21: набранное не пропадает, пустая соседка закрывается, фокус на месте."""
    print("\n== F-21: формы не теряют ввод, открытая — в окне и в фокусе ==")
    _open_plan(page, base)

    # 1. Пустая соседняя форма закрывается, и это видно по её `hidden`.
    page.click("#pl-add-item")
    page.wait_for_timeout(250)
    page.click("#pl-add-batch")
    page.wait_for_timeout(250)
    state = page.evaluate("""() => ({
      item: document.getElementById('pl-item-form').hidden,
      batch: document.getElementById('pl-batch-form').hidden
    })""")
    check("пустая форма вещи закрылась, когда открыли форму партии",
          state["item"] is True and state["batch"] is False, str(state))

    # 2. НЕПУСТУЮ не закрываем: там набранное человеком.
    page.fill("#pl-batch-title", "Черновик Ф21")
    page.click("#pl-add-material")
    page.wait_for_timeout(300)
    kept = page.evaluate("""() => {
      const batch = document.getElementById('pl-batch-form');
      const title = document.getElementById('pl-batch-title');
      const first = document.getElementById('pl-mat-title');
      const r = first ? first.getBoundingClientRect() : null;
      return {
        batchHidden: batch.hidden,
        title: title ? title.value : null,
        focused: document.activeElement ? document.activeElement.id : '',
        inView: r ? (r.top >= 0 && r.bottom <= window.innerHeight
                     && r.width > 0 && r.height > 0) : false
      };
    }""")
    check("форма партии с набранным текстом НЕ закрылась",
          kept["batchHidden"] is False, str(kept))
    check("и текст в ней на месте", kept["title"] == "Черновик Ф21", str(kept))
    check("первое поле открытой формы получило фокус",
          kept["focused"] == "pl-mat-title", str(kept))
    check("и оно видно на экране целиком", kept["inView"] is True, str(kept))

    # 3. Инлайн-форма карточки переживает перерисовку с набранным текстом.
    #    Кнопка ищется по видимому тексту обеих редакций: на дереве без правки
    #    она называется иначе, и опираться на новое имя значило бы доказать
    #    отсутствие КНОПКИ вместо отсутствия ПОВЕДЕНИЯ.
    started = page.evaluate("""() => {
      const cards = [...document.querySelectorAll('#pl-materials .pl-card')];
      const one = cards.find(x => x.textContent.indexOf('Фурнитура Ф18') >= 0);
      if (!one) return null;
      const btn = [...one.querySelectorAll('.pl-actions button')]
        .find(b => b.textContent === 'Изменить'
                   || b.textContent === 'Уточнить количество');
      if (!btn) return null;
      if (!one.querySelector('.pl-form.inline')) btn.click();
      const box = one.querySelector('.pl-form.inline');
      if (!box) return null;
      const text = box.querySelector('input[type=text], input:not([type])');
      if (!text) return null;
      text.value = 'НЕ ТЕРЯЙ МЕНЯ';
      return {cardId: one.dataset.id || ''};
    }""")
    check("инлайн-форма на карточке материала открылась", started is not None,
          "" if started else "формы или кнопки нет")
    if started:
        page.fill("#pl-mat-title", "Повод для перерисовки")
        page.fill("#pl-mat-qty", "1")
        page.click("#pl-mat-form button[type=submit]")
        page.wait_for_timeout(1500)
        after = page.evaluate("""() => {
          const cards = [...document.querySelectorAll('#pl-materials .pl-card')];
          const one = cards.find(x => x.textContent.indexOf('Фурнитура Ф18') >= 0);
          if (!one) return {card: false};
          const box = one.querySelector('.pl-form.inline');
          if (!box) return {card: true, open: false};
          const vals = [...box.querySelectorAll('input')].map(i => i.value);
          return {card: true, open: true, vals: vals};
        }""")
        check("после сохранения в другой форме карточка на месте",
              after.get("card") is True, str(after))
        check("инлайн-форма пережила перерисовку",
              after.get("open") is True, str(after))
        check("и набранный в ней текст не пропал",
              "НЕ ТЕРЯЙ МЕНЯ" in (after.get("vals") or []), str(after)[:200])


# ── Корректив 1: восстановленная форма описывает СВОЁ состояние (P1 ревью) ────

def _fix3_restore_case(page, base, c, tag: str) -> None:
    """P1 ревью PR #54: после перерисовки форма врала и «Сохранить» отвечало 400.

    ЧТО ИМЕННО ВОСПРОИЗВОДИТСЯ, шаг в шаг. У партии стоит точная дата. Человек
    открывает «Изменить», переключает срок на «ориентировочно» и пишет текст —
    и, не сохранив, сохраняет что-то в ДРУГОЙ форме. Это вызывает `render()`.
    До исправления восстановленный список говорил «ориентировочно», введённый
    текст лежал СКРЫТЫМ, а прежняя точная дата оставалась ВИДИМОЙ — и уходила в
    запрос, потому что тело собирается по видимости. «Сохранить» отвечало 400.

    Проверяется не разметка, а три разных факта сразу: что видно
    (`getComputedStyle`), что уйдёт на сервер (ответ ручки) и что там осталось
    (отдельный GET). Совпасть все три могут только если форма честна.
    """
    print(f"\n== Корректив: срок переживает перерисовку и сохраняется ({tag}) ==")
    item = c.post(P3 + "/items", json={"kind": "draft", "title": f"Вещь {tag}",
                                       "op_id": f"cr-i-{tag}"}).json()
    iid = [i for i in item["items"] if i["title"] == f"Вещь {tag}"][0]["id"]
    board = c.post(P3 + "/batches",
                   json={"item_id": iid, "title": f"Партия {tag}", "plan_qty": "20",
                         "due_kind": "exact", "due_date": "2026-10-31",
                         "op_id": f"cr-b-{tag}"}).json()
    bid = [b for b in board["batches"] if b["title"] == f"Партия {tag}"][0]["id"]

    page.goto(f"{base}/supply")
    page.wait_for_timeout(1200)
    close_hint(page)

    opened = page.evaluate("""(id) => {
      const card = document.querySelector('[data-pl="batch"][data-id="' + id + '"]');
      if (!card) return 'карточки нет';
      const btn = card.querySelector('button[data-inline="edit"]');
      if (!btn) return 'кнопки правки нет';
      btn.click();
      return card.querySelector('form.pl-form.inline') ? '' : 'форма не открылась';
    }""", str(bid))
    check(f"{tag}: форма правки партии открылась", opened == "", str(opened))
    if opened:
        return
    page.wait_for_timeout(250)

    # Выбор делается ровно так, как его делает человек: значение и событие.
    page.evaluate("""(id) => {
      const box = document.querySelector('[data-pl="batch"][data-id="' + id
                                         + '"] form.pl-form.inline');
      const sel = box.querySelector('select');
      sel.value = 'approx';
      sel.dispatchEvent(new Event('change'));
      box.querySelector('input[id$="due-text"]').value = 'Конец ноября';
    }""", str(bid))

    # Перерисовку вызывает сохранение в ДРУГОЙ форме — это и есть условие P1.
    page.click("#pl-add-material")
    page.wait_for_timeout(250)
    page.fill("#pl-mat-title", f"Повод {tag}")
    page.fill("#pl-mat-qty", "3")
    page.click("#pl-mat-form button[type=submit]")
    page.wait_for_timeout(1500)

    state = page.evaluate("""(id) => {
      const box = document.querySelector('[data-pl="batch"][data-id="' + id
                                         + '"] form.pl-form.inline');
      if (!box) return null;
      const vis = n => !!n && getComputedStyle(n.closest('.pl-field')).display !== 'none';
      const t = box.querySelector('input[id$="due-text"]');
      const d = box.querySelector('input[id$="due-date"]');
      return {kind: box.querySelector('select').value,
              text: t ? t.value : null, textVisible: vis(t),
              date: d ? d.value : null, dateVisible: vis(d),
              shown: box.innerText};
    }""", str(bid))
    check(f"{tag}: форма пережила перерисовку", state is not None,
          "" if state else "формы нет")
    if not state:
        return
    check(f"{tag}: вид срока остался тем, который выбрал человек",
          state["kind"] == "approx", str(state["kind"]))
    check(f"{tag}: введённый текст на месте", state["text"] == "Конец ноября",
          repr(state["text"]))
    check(f"{tag}: и он ВИДЕН, а не лежит скрытым",
          state["textVisible"] is True, str(state))
    check(f"{tag}: поле даты для этого вида срока скрыто",
          state["dateVisible"] is False, str(state))

    # Нажимаем «Сохранить» той же формы и смотрим на ТРИ вещи: ошибку у формы,
    # ответ сервера и состояние строки после него.
    page.evaluate("""(id) => {
      const box = document.querySelector('[data-pl="batch"][data-id="' + id
                                         + '"] form.pl-form.inline');
      box.querySelector('button[type=submit]').click();
    }""", str(bid))
    page.wait_for_timeout(1500)
    err = page.evaluate("""(id) => {
      const card = document.querySelector('[data-pl="batch"][data-id="' + id + '"]');
      const box = card && card.querySelector('form.pl-form.inline');
      const e = box && box.querySelector('.pl-form-err');
      return e ? e.textContent.trim() : '';
    }""", str(bid))
    check(f"{tag}: «Сохранить» не отвечает отказом", err == "", err[:160])

    row = [b for b in c.get(P3).json()["batches"] if b["id"] == bid]
    check(f"{tag}: строка партии на месте", bool(row), "" if row else "строки нет")
    if row:
        check(f"{tag}: на сервере лежит выбранный вид срока",
              row[0]["due_kind"] == "approx", str(row[0]["due_kind"]))
        check(f"{tag}: и написанный человеком текст",
              row[0]["due_text"] == "Конец ноября", repr(row[0]["due_text"]))
        check(f"{tag}: а прежняя точная дата снята, а не уехала в запрос",
              row[0]["due_date"] == "", repr(row[0]["due_date"]))


def _fix3_restore_unit(page, base, c, tag: str) -> None:
    """Тот же корень у выбора единицы: «другое» и своя строка (корректив 1).

    Здесь сохранение проходило и ДО исправления — и именно поэтому случай
    отдельный: на сервер уходило значение, которого человек на экране не видел.
    Это ровно то, что пакет 1 запретил (D-55 п. 1), и одной проверкой «ответ
    200» такое не ловится.
    """
    print(f"\n== Корректив: своя единица переживает перерисовку ({tag}) ==")
    board = c.post(P3 + "/materials",
                   json={"title": f"Материал {tag}", "qty": "50", "unit": "м",
                         "op_id": f"cr-m-{tag}"}).json()
    mid = [m for m in board["materials"] if m["title"] == f"Материал {tag}"][0]["id"]

    page.goto(f"{base}/supply")
    page.wait_for_timeout(1200)
    close_hint(page)
    opened = page.evaluate("""(id) => {
      const card = document.querySelector('[data-pl="material"][data-id="' + id + '"]');
      if (!card) return 'карточки нет';
      const btn = card.querySelector('button[data-inline="edit"]');
      if (!btn) return 'кнопки правки нет';
      btn.click();
      return card.querySelector('form.pl-form.inline') ? '' : 'форма не открылась';
    }""", str(mid))
    check(f"{tag}: форма правки материала открылась", opened == "", str(opened))
    if opened:
        return
    page.wait_for_timeout(250)
    page.evaluate("""(id) => {
      const box = document.querySelector('[data-pl="material"][data-id="' + id
                                         + '"] form.pl-form.inline');
      const sel = box.querySelector('select');
      sel.value = 'другое';
      sel.dispatchEvent(new Event('change'));
      box.querySelector('input[id$="-other"]').value = 'бобина';
    }""", str(mid))
    page.click("#pl-add-material")
    page.wait_for_timeout(250)
    page.fill("#pl-mat-title", f"Второй повод {tag}")
    page.fill("#pl-mat-qty", "4")
    page.click("#pl-mat-form button[type=submit]")
    page.wait_for_timeout(1500)

    state = page.evaluate("""(id) => {
      const box = document.querySelector('[data-pl="material"][data-id="' + id
                                         + '"] form.pl-form.inline');
      if (!box) return null;
      const o = box.querySelector('input[id$="-other"]');
      return {unit: box.querySelector('select').value,
              other: o ? o.value : null,
              otherVisible: !!o && getComputedStyle(o.closest('.pl-field')).display !== 'none'};
    }""", str(mid))
    check(f"{tag}: форма единицы пережила перерисовку", state is not None,
          "" if state else "формы нет")
    if not state:
        return
    check(f"{tag}: выбран по-прежнему «другое»", state["unit"] == "другое",
          str(state["unit"]))
    check(f"{tag}: своя единица на месте", state["other"] == "бобина",
          repr(state["other"]))
    check(f"{tag}: и поле своей единицы ВИДНО, а не отправляется втайне",
          state["otherVisible"] is True, str(state))

    page.evaluate("""(id) => {
      const box = document.querySelector('[data-pl="material"][data-id="' + id
                                         + '"] form.pl-form.inline');
      box.querySelector('button[type=submit]').click();
    }""", str(mid))
    page.wait_for_timeout(1500)
    row = [m for m in c.get(P3).json()["materials"] if m["id"] == mid]
    check(f"{tag}: единица сохранена той, что видна на экране",
          bool(row) and row[0]["unit"] == "бобина",
          row[0]["unit"] if row else "строки нет")


def _fix3_restore_plain(page, base, c, tag: str) -> None:
    """Сторож соседей: форма БЕЗ зависимой видимости от правки не изменилась.

    Перекраска после восстановления шлёт `change` списку, а списки есть и у
    переноса. Здесь у формы зависимых полей нет вовсе, и правильное поведение —
    «ничего не изменилось»: выбранная партия-приёмник остаётся выбранной, поле
    количества целым, форма живой.
    """
    print(f"\n== Корректив: форма переноса от перекраски не пострадала ({tag}) ==")
    board = c.get(P3).json()
    mats = [m for m in board["materials"] if m["title"] == f"Материал {tag}"]
    batches = [b for b in board["batches"] if b["title"] == f"Партия {tag}"]
    if not mats or not batches:
        check(f"{tag}: фикстуры переноса на месте", False, "нет материала или партии")
        return
    mid, bid = mats[0]["id"], batches[0]["id"]
    other = c.post(P3 + "/batches",
                   json={"item_id": batches[0]["item_id"], "title": f"Приёмник {tag}",
                         "op_id": f"cr-b2-{tag}"}).json()
    oid = [b for b in other["batches"] if b["title"] == f"Приёмник {tag}"][0]["id"]
    c.post(P3 + "/assignments", json={"material_id": mid, "batch_id": bid,
                                      "qty": "10", "op_id": f"cr-a-{tag}"})

    page.goto(f"{base}/supply")
    page.wait_for_timeout(1200)
    close_hint(page)
    opened = page.evaluate("""(id) => {
      const card = document.querySelector('[data-pl="batch"][data-id="' + id + '"]');
      if (!card) return 'карточки нет';
      const btn = [...card.querySelectorAll('button[data-inline]')]
        .find(b => (b.dataset.inline || '').indexOf('move-') === 0);
      if (!btn) return 'кнопки переноса нет';
      btn.click();
      return card.querySelector('form.pl-form.inline') ? '' : 'форма не открылась';
    }""", str(bid))
    check(f"{tag}: форма переноса открылась", opened == "", str(opened))
    if opened:
        return
    page.wait_for_timeout(250)
    page.evaluate("""(args) => {
      const box = document.querySelector('[data-pl="batch"][data-id="' + args[0]
                                         + '"] form.pl-form.inline');
      const sel = box.querySelector('select');
      sel.value = String(args[1]);
      sel.dispatchEvent(new Event('change'));
      box.querySelector('input').value = '4';
    }""", [str(bid), oid])
    page.click("#pl-add-material")
    page.wait_for_timeout(250)
    page.fill("#pl-mat-title", f"Третий повод {tag}")
    page.fill("#pl-mat-qty", "5")
    page.click("#pl-mat-form button[type=submit]")
    page.wait_for_timeout(1500)
    state = page.evaluate("""(id) => {
      const box = document.querySelector('[data-pl="batch"][data-id="' + id
                                         + '"] form.pl-form.inline');
      if (!box) return null;
      const inputs = [...box.querySelectorAll('input')];
      return {target: box.querySelector('select').value,
              qty: inputs.length ? inputs[0].value : null,
              fields: [...box.querySelectorAll('.pl-field')]
                .filter(f => getComputedStyle(f).display === 'none').length};
    }""", str(bid))
    check(f"{tag}: форма переноса пережила перерисовку", state is not None,
          "" if state else "формы нет")
    if state:
        check(f"{tag}: выбранная партия-приёмник осталась выбранной",
              state["target"] == str(oid), f"{state['target']} против {oid}")
        check(f"{tag}: количество не потерялось", state["qty"] == "4", str(state))
        check(f"{tag}: и перекраска ничего в ней не спрятала",
              state["fields"] == 0, str(state))


def _fix3_empty_unit_ui(page, base, c, tag: str) -> None:
    """P1 (тред r3948822957) в браузере: тот же жест, что у ревью.

    Материал в килограммах → «Изменить» → в списке «другое» → своё поле пустым →
    «Сохранить». До исправления это отвечало 200 и молча меняло величину в
    данных владельца. Проверяется и видимая ошибка, и то, что в строке ничего
    не изменилось: одно без другого доказывает половину.
    """
    print(f"\n== P1 в браузере: пустая своя единица не подменяет величину ({tag}) ==")
    board = c.post(P3 + "/materials",
                   json={"title": f"Килограммы {tag}", "qty": "10", "unit": "кг",
                         "op_id": f"c2ui-m-{tag}"}).json()
    mid = [m for m in board["materials"] if m["title"] == f"Килограммы {tag}"][0]["id"]

    page.goto(f"{base}/supply")
    page.wait_for_timeout(1200)
    close_hint(page)
    opened = page.evaluate("""(id) => {
      const card = document.querySelector('[data-pl="material"][data-id="' + id + '"]');
      if (!card) return 'карточки нет';
      const btn = card.querySelector('button[data-inline="edit"]');
      if (!btn) return 'кнопки правки нет';
      btn.click();
      return card.querySelector('form.pl-form.inline') ? '' : 'форма не открылась';
    }""", str(mid))
    check(f"{tag}: форма правки материала открылась", opened == "", str(opened))
    if opened:
        return
    page.wait_for_timeout(250)
    shown = page.evaluate("""(id) => {
      const box = document.querySelector('[data-pl="material"][data-id="' + id
                                         + '"] form.pl-form.inline');
      const sel = box.querySelector('select');
      sel.value = 'другое';
      sel.dispatchEvent(new Event('change'));
      const o = box.querySelector('input[id$="-other"]');
      o.value = '';
      return getComputedStyle(o.closest('.pl-field')).display !== 'none';
    }""", str(mid))
    check(f"{tag}: поле своей единицы открылось и оставлено пустым",
          shown is True, str(shown))

    page.evaluate("""(id) => {
      const box = document.querySelector('[data-pl="material"][data-id="' + id
                                         + '"] form.pl-form.inline');
      box.querySelector('button[type=submit]').click();
    }""", str(mid))
    page.wait_for_timeout(1500)
    err = page.evaluate("""(id) => {
      const card = document.querySelector('[data-pl="material"][data-id="' + id + '"]');
      const box = card && card.querySelector('form.pl-form.inline');
      const e = box && box.querySelector('.pl-form-err');
      return {text: e ? e.textContent.trim() : '',
              visible: !!e && getComputedStyle(e).display !== 'none'};
    }""", str(mid))
    check(f"{tag}: человек видит отказ, а не молчаливый успех",
          bool(err) and err["visible"] is True and "единиц" in err["text"],
          str(err)[:200])

    row = [m for m in c.get(P3).json()["materials"] if m["id"] == mid]
    check(f"{tag}: величина в данных не подменена",
          bool(row) and row[0]["unit"] == "кг",
          row[0]["unit"] if row else "строки нет")

    # А годная своя единица тем же путём по-прежнему сохраняется.
    page.evaluate("""(id) => {
      const box = document.querySelector('[data-pl="material"][data-id="' + id
                                         + '"] form.pl-form.inline');
      box.querySelector('input[id$="-other"]').value = 'бобина';
      box.querySelector('button[type=submit]').click();
    }""", str(mid))
    page.wait_for_timeout(1500)
    row = [m for m in c.get(P3).json()["materials"] if m["id"] == mid]
    check(f"{tag}: а написанная своя единица сохраняется как прежде",
          bool(row) and row[0]["unit"] == "бобина",
          row[0]["unit"] if row else "строки нет")


def _fix3_lost_response(page, base, c) -> None:
    """P1 (тред r3950849148): повтор после ПОТЕРЯННОГО ответа удваивал метраж.

    Потерянный ответ имитируется честно, а не подделкой: запрос ДОХОДИТ до
    сервера и там исполняется, а страница видит сетевой отказ — ровно то, что
    бывает при обрыве после коммита. Потом человек успешно сохраняет в другой
    форме (это вызывает `render()`) и повторяет то же назначение. Замок
    повторного поступка обязан узнать его по `op_id`; до исправления форма
    приходила под новой идентичностью, и те же 10 метров прибавлялись второй раз.

    Проверка не зависит от ширины экрана — она про идентичность запроса, а не
    про раскладку, — поэтому делается на одном viewport и это сказано вслух.
    """
    print("\n== P1 в браузере: повтор после потерянного ответа не двоит метраж ==")
    it = c.post(P3 + "/items", json={"kind": "draft", "title": "Вещь-повтор",
                                     "op_id": "c2ui-i"}).json()
    iid = [i for i in it["items"] if i["title"] == "Вещь-повтор"][0]["id"]
    c.post(P3 + "/batches", json={"item_id": iid, "title": "Партия-повтор",
                                  "plan_qty": "5", "op_id": "c2ui-b"})
    mb = c.post(P3 + "/materials", json={"title": "Ткань-повтор", "qty": "100",
                                         "unit": "м", "op_id": "c2ui-m"}).json()
    mid = [m for m in mb["materials"] if m["title"] == "Ткань-повтор"][0]["id"]

    page.goto(f"{base}/supply")
    page.wait_for_timeout(1200)
    close_hint(page)
    opened = page.evaluate("""(id) => {
      const card = document.querySelector('[data-pl="material"][data-id="' + id + '"]');
      if (!card) return 'карточки нет';
      const btn = card.querySelector('button[data-inline="assign"]');
      if (!btn) return 'кнопки назначения нет';
      btn.click();
      return card.querySelector('form.pl-form.inline') ? '' : 'форма не открылась';
    }""", str(mid))
    check("форма назначения открылась", opened == "", str(opened))
    if opened:
        return
    page.wait_for_timeout(250)
    page.evaluate("""(id) => {
      const box = document.querySelector('[data-pl="material"][data-id="' + id
                                         + '"] form.pl-form.inline');
      box.querySelector('input').value = '10';
    }""", str(mid))

    def lose(route):
        # Сервер запрос ИСПОЛНЯЕТ, страница ответа не получает.
        try:
            route.fetch()
        finally:
            route.abort()

    page.route("**/api/supply/planning/assignments", lose)
    page.evaluate("""(id) => {
      const box = document.querySelector('[data-pl="material"][data-id="' + id
                                         + '"] form.pl-form.inline');
      box.querySelector('button[type=submit]').click();
    }""", str(mid))
    page.wait_for_timeout(1500)
    page.unroute("**/api/supply/planning/assignments")
    first = [m for m in c.get(P3).json()["materials"] if m["id"] == mid]
    check("сервер записал назначение, хотя ответ не дошёл",
          bool(first) and first[0]["assigned"] == 10,
          str(first[0]["assigned"]) if first else "строки нет")

    page.click("#pl-add-material")
    page.wait_for_timeout(250)
    page.fill("#pl-mat-title", "Повод для повтора")
    page.fill("#pl-mat-qty", "1")
    page.click("#pl-mat-form button[type=submit]")
    page.wait_for_timeout(1500)
    token = page.evaluate("""(id) => {
      const box = document.querySelector('[data-pl="material"][data-id="' + id
                                         + '"] form.pl-form.inline');
      return box ? (box.dataset.opId || '') : 'формы нет';
    }""", str(mid))
    check("идентичность поступка пережила перерисовку",
          isinstance(token, str) and token.startswith("op-"), repr(token))

    page.evaluate("""(id) => {
      const box = document.querySelector('[data-pl="material"][data-id="' + id
                                         + '"] form.pl-form.inline');
      box.querySelector('button[type=submit]').click();
    }""", str(mid))
    page.wait_for_timeout(1500)
    second = [m for m in c.get(P3).json()["materials"] if m["id"] == mid]
    check("повтор того же назначения НЕ прибавил метраж второй раз",
          bool(second) and second[0]["assigned"] == 10,
          str(second[0]["assigned"]) if second else "строки нет")

    # А изменённая форма — это уже другой поступок, и он обязан пройти.
    page.evaluate("""(id) => {
      const box = document.querySelector('[data-pl="material"][data-id="' + id
                                         + '"] form.pl-form.inline');
      const q = box.querySelector('input');
      q.value = '5';
      q.dispatchEvent(new Event('input', {bubbles: true}));
      box.querySelector('button[type=submit]').click();
    }""", str(mid))
    page.wait_for_timeout(1500)
    third = [m for m in c.get(P3).json()["materials"] if m["id"] == mid]
    check("а изменённое назначение по-прежнему записывается",
          bool(third) and third[0]["assigned"] == 15,
          str(third[0]["assigned"]) if third else "строки нет")


# ── Корректив 3: черновик держится за СВОЮ редакцию (тред r3951150422) ────────

def _stale_draft_replay(page, base, c, kind, card_id, inline_name, mutate, tag):
    """Разыграть два окна и вернуть текст ошибки у формы после «Сохранить».

    Хореография ровно та, что в отчёте: черновик открыт на редакции N; ВТОРОЙ
    аутентифицированный клиент правит ту же строку до N+1; в первом окне
    сохраняется ПОСТОРОННЯЯ форма, из-за чего доска перерисовывается и форма
    пересобирается; человек жмёт «Сохранить» в своём черновике.

    Возвращается именно текст ошибки, а не код ответа: человек видит текст, и
    проверять надо то, что видит он. Состояние строки набор сверяет отдельно —
    одно без другого доказывает половину.
    """
    page.goto(f"{base}/supply")
    page.wait_for_timeout(1200)
    close_hint(page)
    opened = page.evaluate("""(a) => {
      const card = document.querySelector('[data-pl="' + a[0] + '"][data-id="' + a[1] + '"]');
      if (!card) return 'карточки нет';
      const btn = card.querySelector('button[data-inline="' + a[2] + '"]');
      if (!btn) return 'кнопки нет';
      btn.click();
      return card.querySelector('form.pl-form.inline') ? '' : 'форма не открылась';
    }""", [kind, str(card_id), inline_name])
    check(f"{tag}: черновик открыт", opened == "", str(opened))
    if opened:
        return None
    page.wait_for_timeout(250)

    mutate()                       # второе окно: та же строка уходит на N+1

    page.click("#pl-add-material")
    page.wait_for_timeout(250)
    page.fill("#pl-mat-title", f"Посторонний {tag}")
    page.fill("#pl-mat-qty", "1")
    page.click("#pl-mat-form button[type=submit]")
    page.wait_for_timeout(1500)

    page.evaluate("""(a) => {
      const box = document.querySelector('[data-pl="' + a[0] + '"][data-id="' + a[1]
                                         + '"] form.pl-form.inline');
      if (box) box.querySelector('button[type=submit]').click();
    }""", [kind, str(card_id)])
    page.wait_for_timeout(1500)
    return page.evaluate("""(a) => {
      const card = document.querySelector('[data-pl="' + a[0] + '"][data-id="' + a[1] + '"]');
      const box = card && card.querySelector('form.pl-form.inline');
      const e = box && box.querySelector('.pl-form-err');
      return e ? e.textContent.trim() : '';
    }""", [kind, str(card_id)])


def _fix3_stale_material(page, base, c, tag: str) -> None:
    """P1: черновик правки материала не имеет права затирать чужую правку."""
    print(f"\n== P1: черновик материала держится за свою редакцию ({tag}) ==")
    board = c.post(P3 + "/materials",
                   json={"title": f"Ткань-редакция {tag}", "qty": "10", "unit": "м",
                         "op_id": f"c3-m-{tag}"}).json()
    mid = [m for m in board["materials"]
           if m["title"] == f"Ткань-редакция {tag}"][0]["id"]

    def other_window():
        cur = [m for m in c.get(P3).json()["materials"] if m["id"] == mid][0]
        r = c.post(P3 + f"/materials/{mid}/update",
                   json={"qty": "25", "rev": cur["rev"], "op_id": f"c3-o-{tag}"})
        check(f"{tag}: второе окно записало 25", r.status_code == 200,
              f"{r.status_code}: {r.text[:120]}")

    err = _stale_draft_replay(page, base, c, "material", mid, "edit",
                              other_window, tag)
    if err is None:
        return
    check(f"{tag}: человек видит отказ, а не молчаливый успех",
          "уже изменили" in err, err[:160] or "ошибки нет")
    row = [m for m in c.get(P3).json()["materials"] if m["id"] == mid]
    check(f"{tag}: чужая правка цела — в данных 25, а не 10",
          bool(row) and row[0]["qty"] == 25, str(row[0]["qty"]) if row else "нет строки")


def _fix3_stale_item(page, base, c, tag: str) -> None:
    """Тот же корень у формы правки вещи."""
    print(f"\n== P1: черновик вещи держится за свою редакцию ({tag}) ==")
    board = c.post(P3 + "/items",
                   json={"kind": "draft", "title": f"Новинка-редакция {tag}",
                         "note": "первая", "op_id": f"c3-i-{tag}"}).json()
    iid = [i for i in board["items"]
           if i["title"] == f"Новинка-редакция {tag}"][0]["id"]

    def other_window():
        cur = [i for i in c.get(P3).json()["items"] if i["id"] == iid][0]
        r = c.post(P3 + f"/items/{iid}/update",
                   json={"note": "правка из другого окна", "rev": cur["rev"],
                         "op_id": f"c3-oi-{tag}"})
        check(f"{tag}: второе окно правит заметку вещи", r.status_code == 200,
              f"{r.status_code}: {r.text[:120]}")

    err = _stale_draft_replay(page, base, c, "item", iid, "edit", other_window, tag)
    if err is None:
        return
    check(f"{tag}: черновик вещи отвергнут отказом",
          "уже изменили" in err, err[:160] or "ошибки нет")
    row = [i for i in c.get(P3).json()["items"] if i["id"] == iid]
    check(f"{tag}: заметка из другого окна цела",
          bool(row) and row[0]["note"] == "правка из другого окна",
          repr(row[0]["note"]) if row else "нет строки")


def _fix3_stale_batch(page, base, c, tag: str) -> None:
    """Тот же корень у формы правки плановой партии."""
    print(f"\n== P1: черновик партии держится за свою редакцию ({tag}) ==")
    it = c.post(P3 + "/items", json={"kind": "draft", "title": f"Вещь-партия {tag}",
                                     "op_id": f"c3-bi-{tag}"}).json()
    iid = [i for i in it["items"] if i["title"] == f"Вещь-партия {tag}"][0]["id"]
    board = c.post(P3 + "/batches",
                   json={"item_id": iid, "title": f"Партия-редакция {tag}",
                         "plan_qty": "10", "op_id": f"c3-b-{tag}"}).json()
    bid = [b for b in board["batches"]
           if b["title"] == f"Партия-редакция {tag}"][0]["id"]

    def other_window():
        cur = [b for b in c.get(P3).json()["batches"] if b["id"] == bid][0]
        r = c.post(P3 + f"/batches/{bid}/update",
                   json={"plan_qty": "60", "rev": cur["rev"], "op_id": f"c3-ob-{tag}"})
        check(f"{tag}: второе окно ставит план 60", r.status_code == 200,
              f"{r.status_code}: {r.text[:120]}")

    err = _stale_draft_replay(page, base, c, "batch", bid, "edit", other_window, tag)
    if err is None:
        return
    check(f"{tag}: черновик партии отвергнут отказом",
          "уже изменили" in err, err[:160] or "ошибки нет")
    row = [b for b in c.get(P3).json()["batches"] if b["id"] == bid]
    check(f"{tag}: план из другого окна цел — 60, а не 10",
          bool(row) and row[0]["plan_qty"] == 60,
          str(row[0]["plan_qty"]) if row else "нет строки")


def _fix3_stale_move(page, base, c, tag: str) -> None:
    """И у переноса: он тоже несёт редакцию и тоже восстанавливается."""
    print(f"\n== P1: черновик переноса держится за свою редакцию ({tag}) ==")
    it = c.post(P3 + "/items", json={"kind": "draft", "title": f"Вещь-перенос {tag}",
                                     "op_id": f"c3-mi-{tag}"}).json()
    iid = [i for i in it["items"] if i["title"] == f"Вещь-перенос {tag}"][0]["id"]
    b1 = c.post(P3 + "/batches", json={"item_id": iid, "title": f"Откуда {tag}",
                                       "op_id": f"c3-mb1-{tag}"}).json()
    src = [b for b in b1["batches"] if b["title"] == f"Откуда {tag}"][0]["id"]
    b2 = c.post(P3 + "/batches", json={"item_id": iid, "title": f"Куда {tag}",
                                       "op_id": f"c3-mb2-{tag}"}).json()
    dst = [b for b in b2["batches"] if b["title"] == f"Куда {tag}"][0]["id"]
    mb = c.post(P3 + "/materials", json={"title": f"Ткань-перенос {tag}", "qty": "100",
                                         "unit": "м", "op_id": f"c3-mm-{tag}"}).json()
    mid = [m for m in mb["materials"] if m["title"] == f"Ткань-перенос {tag}"][0]["id"]
    c.post(P3 + "/assignments", json={"material_id": mid, "batch_id": src,
                                      "qty": "50", "op_id": f"c3-ma-{tag}"})
    aid = None
    for b in c.get(P3).json()["batches"]:
        if b["id"] != src:
            continue
        for a in b["assignments"]:
            if a["material_id"] == mid:
                aid = a["id"]
    check(f"{tag}: назначение для переноса заведено", aid is not None,
          "" if aid else "назначения нет")
    if aid is None:
        return

    def other_window():
        cur = None
        for b in c.get(P3).json()["batches"]:
            for a in b["assignments"]:
                if a["id"] == aid:
                    cur = a
        r = c.post(P3 + f"/assignments/{aid}/update",
                   json={"qty": "80", "rev": cur["rev"], "op_id": f"c3-oa-{tag}"})
        check(f"{tag}: второе окно меняет назначение на 80", r.status_code == 200,
              f"{r.status_code}: {r.text[:120]}")

    err = _stale_draft_replay(page, base, c, "batch", src, f"move-{aid}",
                              other_window, tag)
    if err is None:
        return
    check(f"{tag}: черновик переноса отвергнут отказом",
          "уже изменили" in err, err[:160] or "ошибки нет")
    now = None
    for b in c.get(P3).json()["batches"]:
        for a in b["assignments"]:
            if a["id"] == aid:
                now = a
    check(f"{tag}: назначение из другого окна цело — 80 и на своей партии",
          now is not None and now["qty"] == 80, str(now["qty"]) if now else "нет строки")


def _fix3_own_save_rebase(page, base, c, tag: str) -> None:
    """Обратная сторона: СВОЯ удачная запись не превращается в ложный 409.

    Правило «черновик держится за свою редакцию» обязано кончаться там, где
    черновик применён. После собственной удачной записи строка — та же самая,
    что в форме, и держаться за прежний номер значило бы ответить человеку
    «кто-то изменил» на его собственную правку.

    Проверяется ВТОРОЙ правкой, а не повтором: неизменный повтор опознаётся
    замком поступка (`op_id`) и до сверки редакций не доходит вовсе.
    """
    print(f"\n== Сторож: своя удачная запись не даёт ложного отказа ({tag}) ==")
    board = c.post(P3 + "/materials",
                   json={"title": f"Своя правка {tag}", "qty": "10", "unit": "м",
                         "op_id": f"c3-s-{tag}"}).json()
    mid = [m for m in board["materials"] if m["title"] == f"Своя правка {tag}"][0]["id"]

    page.goto(f"{base}/supply")
    page.wait_for_timeout(1200)
    close_hint(page)
    opened = page.evaluate("""(id) => {
      const card = document.querySelector('[data-pl="material"][data-id="' + id + '"]');
      if (!card) return 'карточки нет';
      card.querySelector('button[data-inline="edit"]').click();
      return card.querySelector('form.pl-form.inline') ? '' : 'форма не открылась';
    }""", str(mid))
    check(f"{tag}: форма правки открыта", opened == "", str(opened))
    if opened:
        return
    page.wait_for_timeout(250)

    def save_with_title(new_title):
        page.evaluate("""(a) => {
          const box = document.querySelector('[data-pl="material"][data-id="' + a[0]
                                             + '"] form.pl-form.inline');
          const t = box.querySelector('input[type=text], input:not([type])');
          t.value = a[1];
          // Событие ввода — как у человека: оно и снимает прежнюю идентичность
          // поступка, потому что это уже другая правка.
          t.dispatchEvent(new Event('input', {bubbles: true}));
          box.querySelector('button[type=submit]').click();
        }""", [str(mid), new_title])
        page.wait_for_timeout(1500)
        return page.evaluate("""(id) => {
          const card = document.querySelector('[data-pl="material"][data-id="' + id + '"]');
          const box = card && card.querySelector('form.pl-form.inline');
          const e = box && box.querySelector('.pl-form-err');
          return e ? e.textContent.trim() : '';
        }""", str(mid))

    first = save_with_title(f"Первая правка {tag}")
    check(f"{tag}: первая правка прошла без отказа", first == "", first[:160])
    second = save_with_title(f"Вторая правка {tag}")
    check(f"{tag}: и ВТОРАЯ правка в той же форме тоже прошла",
          second == "", second[:160])
    row = [m for m in c.get(P3).json()["materials"] if m["id"] == mid]
    check(f"{tag}: в данных лежит последняя правка человека",
          bool(row) and row[0]["title"] == f"Вторая правка {tag}",
          row[0]["title"] if row else "нет строки")


def _fix3_replay_then_edit(page, base, c, tag: str) -> None:
    """Корректив 4: повтор поступка не имеет права оставить чужие данные под
    старым черновиком.

    ЦЕПОЧКА, И КАЖДОЕ ЕЁ ЗВЕНО ОБЯЗАТЕЛЬНО. Человек правит название материала;
    запрос ДОХОДИТ до сервера и там применяется, а ответ теряется (обрыв после
    коммита — имитируем честно, а не подделкой ответа). Сосед в это время ставит
    количество 40 на свежей редакции. Человек повторяет то же нажатие — замок
    поступка (`op_id`) узнаёт повтор и отвечает 200, ничего не записывая. И вот
    здесь начинается предмет проверки: форма пересобирается, и если ей вернуть
    ПРЕЖНИЕ значения черновика поверх свежей редакции, то следующая правка
    молча вернёт соседские 40 к своим 25.

    Проверяется не только итог, но и то, что человек ВИДИТ между шагами:
    количество в форме после повтора обязано быть текущим, а не прежним. Итог
    без этого доказывал бы меньше: правильное число могло бы совпасть случайно.
    """
    print(f"\n== Корректив 4: повтор поступка не воскрешает старый черновик ({tag}) ==")
    board = c.post(P3 + "/materials",
                   json={"title": f"Ткань-повтор {tag}", "qty": "25", "unit": "м",
                         "op_id": f"c4-m-{tag}"}).json()
    mid = [m for m in board["materials"]
           if m["title"] == f"Ткань-повтор {tag}"][0]["id"]

    page.goto(f"{base}/supply")
    page.wait_for_timeout(1200)
    close_hint(page)
    opened = page.evaluate("""(id) => {
      const card = document.querySelector('[data-pl="material"][data-id="' + id + '"]');
      if (!card) return 'карточки нет';
      const btn = card.querySelector('button[data-inline="edit"]');
      if (!btn) return 'кнопки правки нет';
      btn.click();
      return card.querySelector('form.pl-form.inline') ? '' : 'форма не открылась';
    }""", str(mid))
    check(f"{tag}: форма правки открыта", opened == "", str(opened))
    if opened:
        return
    page.wait_for_timeout(250)

    def type_title_and_save(value):
        page.evaluate("""(a) => {
          const box = document.querySelector('[data-pl="material"][data-id="' + a[0]
                                             + '"] form.pl-form.inline');
          const t = box.querySelector('input[type=text], input:not([type])');
          t.value = a[1];
          // Событие ввода — как у человека: оно снимает прежнюю идентичность
          // поступка, потому что это уже другая правка.
          t.dispatchEvent(new Event('input', {bubbles: true}));
          box.querySelector('button[type=submit]').click();
        }""", [str(mid), value])
        page.wait_for_timeout(1500)

    def lose(route):
        # Сервер запрос ИСПОЛНЯЕТ, страница ответа не получает.
        try:
            route.fetch()
        finally:
            route.abort()

    page.route(f"**/api/supply/planning/materials/{mid}/update", lose)
    type_title_and_save(f"Правка один {tag}")
    page.unroute(f"**/api/supply/planning/materials/{mid}/update")
    lost = [m for m in c.get(P3).json()["materials"] if m["id"] == mid]
    check(f"{tag}: сервер применил правку, хотя ответ не дошёл",
          bool(lost) and lost[0]["title"] == f"Правка один {tag}",
          lost[0]["title"] if lost else "нет строки")

    peer = c.post(P3 + f"/materials/{mid}/update",
                  json={"qty": "40", "rev": lost[0]["rev"], "op_id": f"c4-peer-{tag}"})
    check(f"{tag}: сосед поставил количество 40", peer.status_code == 200,
          f"{peer.status_code}: {peer.text[:120]}")

    # Повтор ТОГО ЖЕ нажатия: форма не тронута, идентичность поступка прежняя.
    page.evaluate("""(id) => {
      const box = document.querySelector('[data-pl="material"][data-id="' + id
                                         + '"] form.pl-form.inline');
      box.querySelector('button[type=submit]').click();
    }""", str(mid))
    page.wait_for_timeout(1500)
    after_retry = [m for m in c.get(P3).json()["materials"] if m["id"] == mid]
    check(f"{tag}: повтор поступка чужую правку не тронул — 40 на месте",
          bool(after_retry) and after_retry[0]["qty"] == 40,
          str(after_retry[0]["qty"]) if after_retry else "нет строки")

    shown = page.evaluate("""(id) => {
      const box = document.querySelector('[data-pl="material"][data-id="' + id
                                         + '"] form.pl-form.inline');
      if (!box) return null;
      const fields = [...box.querySelectorAll('input')];
      return {rev: box.dataset.rev || '',
              qty: fields.length > 1 ? fields[1].value : null};
    }""", str(mid))
    check(f"{tag}: форма после повтора показывает ТЕКУЩЕЕ количество, а не прежнее",
          bool(shown) and shown["qty"] == "40", str(shown))
    check(f"{tag}: и редакция в форме та же, из которой взяты эти значения",
          bool(shown) and shown["rev"] == str(after_retry[0]["rev"]),
          f"{shown['rev'] if shown else '—'} против {after_retry[0]['rev'] if after_retry else '—'}")

    # Следующая правка человека: она обязана лечь ПОВЕРХ правды, а не поверх
    # своего прежнего черновика.
    type_title_and_save(f"Правка два {tag}")
    final = [m for m in c.get(P3).json()["materials"] if m["id"] == mid]
    err = page.evaluate("""(id) => {
      const card = document.querySelector('[data-pl="material"][data-id="' + id + '"]');
      const box = card && card.querySelector('form.pl-form.inline');
      const e = box && box.querySelector('.pl-form-err');
      return e ? e.textContent.trim() : '';
    }""", str(mid))
    check(f"{tag}: следующая правка прошла без отказа", err == "", err[:160])
    check(f"{tag}: и чужие 40 НЕ вернулись к 25",
          bool(final) and final[0]["qty"] == 40,
          str(final[0]["qty"]) if final else "нет строки")
    check(f"{tag}: а собственная правка названия применена",
          bool(final) and final[0]["title"] == f"Правка два {tag}",
          final[0]["title"] if final else "нет строки")


def _fix3_peer_after_commit(page, base, c, tag: str) -> None:
    """Сосед пишет ПОСЛЕ нашего коммита — молчаливой подмены быть не должно.

    Это форма, о которой говорит тред r3952047085: наша запись прошла, а строку
    успели изменить прежде, чем ответ добрался до страницы. Точное серверное
    чередование «коммит → чужая запись → чтение доски» из браузера не
    закрепляется, поэтому здесь берётся достижимая и полностью детерминированная
    его половина: ответ на нашу запись перехватывается, чужая правка делается
    ПОКА он не отдан странице, и только потом он доставляется. Дальше человек
    правит ещё раз и сохраняет.

    Что обязано быть верным в любом исходе: чужие данные не подменяются молча.
    Либо человек получает отказ, либо его правка ложится поверх чужих значений —
    но «200 и чужого числа больше нет» не бывает никогда.

    Это СТОРОЖ, а не воспроизведение: он зелёный и до корректива 4. Красным
    корректив 4 доказан цепочкой повтора (`_fix3_replay_then_edit`); здесь
    проверяется, что соседняя форма той же семьи не осталась дырой.
    """
    print(f"\n== Сторож: чужая правка после нашего коммита не исчезает ({tag}) ==")
    board = c.post(P3 + "/materials",
                   json={"title": f"Ткань-гонка {tag}", "qty": "25", "unit": "м",
                         "op_id": f"c4r-m-{tag}"}).json()
    mid = [m for m in board["materials"]
           if m["title"] == f"Ткань-гонка {tag}"][0]["id"]

    page.goto(f"{base}/supply")
    page.wait_for_timeout(1200)
    close_hint(page)
    opened = page.evaluate("""(id) => {
      const card = document.querySelector('[data-pl="material"][data-id="' + id + '"]');
      if (!card) return 'карточки нет';
      const btn = card.querySelector('button[data-inline="edit"]');
      if (!btn) return 'кнопки правки нет';
      btn.click();
      return card.querySelector('form.pl-form.inline') ? '' : 'форма не открылась';
    }""", str(mid))
    check(f"{tag}: форма правки открыта", opened == "", str(opened))
    if opened:
        return
    page.wait_for_timeout(250)

    def peer_between(route):
        # Наш запрос сервер исполняет целиком; ответ придерживается.
        resp = route.fetch()
        cur = [m for m in c.get(P3).json()["materials"] if m["id"] == mid]
        if cur:
            c.post(P3 + f"/materials/{mid}/update",
                   json={"qty": "40", "rev": cur[0]["rev"], "op_id": f"c4r-peer-{tag}"})
        route.fulfill(response=resp)

    page.route(f"**/api/supply/planning/materials/{mid}/update", peer_between)
    page.evaluate("""(a) => {
      const box = document.querySelector('[data-pl="material"][data-id="' + a[0]
                                         + '"] form.pl-form.inline');
      const t = box.querySelector('input[type=text], input:not([type])');
      t.value = a[1];
      t.dispatchEvent(new Event('input', {bubbles: true}));
      box.querySelector('button[type=submit]').click();
    }""", [str(mid), f"Наша правка {tag}"])
    page.wait_for_timeout(1800)
    page.unroute(f"**/api/supply/planning/materials/{mid}/update")

    mid_state = [m for m in c.get(P3).json()["materials"] if m["id"] == mid]
    check(f"{tag}: чужая правка легла на сервер",
          bool(mid_state) and mid_state[0]["qty"] == 40,
          str(mid_state[0]["qty"]) if mid_state else "нет строки")

    page.evaluate("""(a) => {
      const box = document.querySelector('[data-pl="material"][data-id="' + a[0]
                                         + '"] form.pl-form.inline');
      if (!box) return;
      const t = box.querySelector('input[type=text], input:not([type])');
      t.value = a[1];
      t.dispatchEvent(new Event('input', {bubbles: true}));
      box.querySelector('button[type=submit]').click();
    }""", [str(mid), f"Вторая наша правка {tag}"])
    page.wait_for_timeout(1800)
    final = [m for m in c.get(P3).json()["materials"] if m["id"] == mid]
    err = page.evaluate("""(id) => {
      const card = document.querySelector('[data-pl="material"][data-id="' + id + '"]');
      const box = card && card.querySelector('form.pl-form.inline');
      const e = box && box.querySelector('.pl-form-err');
      return e ? e.textContent.trim() : '';
    }""", str(mid))
    # Годных исходов ровно два, и оба честные: отказ либо правка поверх чужих
    # значений. Негодный один — тихо вернувшиеся 25.
    check(f"{tag}: чужие 40 не исчезли ни при каком исходе",
          bool(final) and final[0]["qty"] == 40,
          f"qty={final[0]['qty'] if final else '—'}, ошибка: {err[:90]}")
    check(f"{tag}: и человек либо получил отказ, либо его правка применена",
          bool(final) and (err != "" or final[0]["title"] == f"Вторая наша правка {tag}"),
          f"title={final[0]['title'] if final else '—'}, ошибка: {err[:90]}")


def _fix3_mobile(browser, base, c) -> None:
    """Те же два свойства на телефоне: список единиц и фокус в окне 390x844."""
    print("\n== F-16/F-21 на телефоне 390x844 ==")
    ctx = browser.new_context(viewport={"width": 390, "height": 844})
    ctx.add_cookies([{"name": k, "value": v, "domain": "127.0.0.1", "path": "/"}
                     for k, v in c.cookies.items()])
    errors: list[str] = []
    page = ctx.new_page()
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{base}/supply")
    page.wait_for_timeout(1200)
    close_hint(page)
    page.click("#pl-add-material")
    page.wait_for_timeout(400)

    facts = page.evaluate("""() => {
      const sel = document.getElementById('pl-mat-unit');
      const first = document.getElementById('pl-mat-title');
      const r = first ? first.getBoundingClientRect() : null;
      return {
        tag: sel ? sel.tagName : 'НЕТ',
        focused: document.activeElement ? document.activeElement.id : '',
        inView: r ? (r.top >= 0 && r.bottom <= window.innerHeight
                     && r.width > 0 && r.height > 0) : false
      };
    }""")
    check("на телефоне единица тоже выбирается списком",
          facts["tag"] == "SELECT", str(facts))
    check("первое поле в фокусе", facts["focused"] == "pl-mat-title", str(facts))
    check("и видно целиком на узком экране", facts["inView"] is True, str(facts))
    # Тот же P1 на телефоне: ревью воспроизвело его на ОБОИХ viewport, значит и
    # доказательство исправления обязано быть на обоих.
    for label, run_step in (("P1 срок", lambda: _fix3_restore_case(page, base, c, "mobile")),
                            ("P1 единица", lambda: _fix3_restore_unit(page, base, c, "mobile")),
                            ("P1 сосед", lambda: _fix3_restore_plain(page, base, c, "mobile")),
                            ("P1 пустая единица",
                             lambda: _fix3_empty_unit_ui(page, base, c, "mobile")),
                            ("P1 черновик материала",
                             lambda: _fix3_stale_material(page, base, c, "mobile")),
                            ("P1 повтор и правка",
                             lambda: _fix3_replay_then_edit(page, base, c, "mobile"))):
        try:
            run_step()
        except Exception as exc:  # noqa: BLE001 — важен отчёт, а не тип
            check(f"{label} на телефоне: шаг дошёл до конца без исключения", False,
                  f"{type(exc).__name__}: {str(exc).strip().splitlines()[0][:160]}")

    check("на телефоне не было ошибок в консоли", not errors, str(errors)[:200])
    ctx.close()


def _fix2_mobile(browser, base, c) -> None:
    """Новые кнопки на телефоне: их видно и по ним попадаешь."""
    print("\n== F-12/F-13 на телефоне 390x844: кнопки не перекрыты ==")
    ctx = browser.new_context(viewport={"width": 390, "height": 844})
    ctx.add_cookies([{"name": k, "value": v, "domain": "127.0.0.1", "path": "/"}
                     for k, v in c.cookies.items()])
    page = ctx.new_page()
    page.goto(f"{base}/supply")
    page.wait_for_timeout(1200)
    close_hint(page)
    page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(500)

    covered = page.evaluate("""() => {
      const out = [];
      const wanted = ['Изменить', 'Удалить'];
      const btns = [...document.querySelectorAll(
        '#pl-materials .pl-actions button, #pl-batches .pl-actions button')]
        .filter(b => wanted.indexOf(b.textContent) >= 0);
      for (const b of btns) {
        const r = b.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) { out.push([b.textContent, 'нулевой размер']); continue; }
        if (r.top < 0 || r.bottom > window.innerHeight) continue;  // вне окна — не про перекрытие
        const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
        if (!hit || (hit !== b && !b.contains(hit))) {
          out.push([b.textContent, hit ? (hit.tagName + '.' + hit.className) : 'null']);
        }
      }
      return {checked: btns.length, covered: out};
    }""")
    found = bool(covered) and covered["checked"] > 0
    check("на телефоне кнопки «Изменить»/«Удалить» вообще нашлись", found,
          str(covered))
    # Пустая выборка — не «ничего не перекрыто», а «нечего было проверять».
    check("и ни одна из них не перекрыта фиксированным элементом",
          found and not covered["covered"], str(covered)[:220])
    ctx.close()


# ── SUPPLY-FIX-5 (F-27, F-28, F-29) в настоящем браузере ────────────────────
#
# СВОЯ ОРГАНИЗАЦИЯ НА ВЕСЬ БЛОК. Порог F-29 — «строк больше десяти», и
# проверять его на организации, у которой строки накопили предыдущие двадцать
# шесть шагов, нельзя: «полосы поиска нет» стало бы утверждением про чужую
# историю, а не про порог. Здесь список растёт с нуля и на глазах.

def _fix5_ui_client(base):
    c5 = httpx.Client(headers={"X-Oborot-CSRF": "1"}, base_url=base, timeout=120.0)
    r = c5.post("/register", data={"name": "Владелец Пять",
                                   "email": "supply-ui5@test.io",
                                   "password": "secret123",
                                   "org_name": "Бренд-UI-Пять"})
    check("организация пакета 5 зарегистрирована", r.status_code in (200, 302, 303),
          str(r.status_code))
    return c5


def _fix5_ui_preview_flag(org_name: str) -> None:
    """Флаг предпросмотра ИМЕННО этой организации (общий помощник берёт первую)."""
    con = sqlite3.connect(DB_PATH)
    try:
        row = con.execute("SELECT id, settings_json FROM orgs WHERE name = ?",
                          (org_name,)).fetchone()
        if row is None:
            return
        try:
            data = json.loads(row[1] or "{}")
        except ValueError:
            data = {}
        data["supply_sheets_preview"] = True
        con.execute("UPDATE orgs SET settings_json = ? WHERE id = ?",
                    (json.dumps(data, ensure_ascii=False), row[0]))
        con.commit()
    finally:
        con.close()


#: Ответ доски задерживается НАМЕРЕННО — иначе проверка обводки урока ничего не
#: проверяет. Тур и доска грузятся двумя независимыми запросами, и кто из них
#: успеет первым, решает случай. Когда первой приходит доска, кнопка стоит на
#: своём окончательном месте ещё до появления обводки, и дефект «обводка не
#: пошла за уехавшей целью» не может проявиться вовсе: такой прогон зеленеет и
#: на сломанном коде — проверено запуском на дереве с прежним `_hints.html`
#: (636 OK / 0 FAIL). Задержка закрепляет ТОТ порядок, в котором дефект живёт:
#: обводка встаёт по пустой странице, и только потом приходит список карточек.
LESSON_DELAY_SCRIPT = DELAY_SCRIPT + """
(() => { window.__supDelayMatch = "/api/supply/planning";
         window.__supDelayMs = 1500; })();
"""


def supply_fix_5_ui(pw, base) -> None:
    """SUPPLY-FIX-5 в браузере: F-27 (урок), F-28 (кнопка), F-29 (масштаб).

    ЧТО СЮДА ПОПАЛО И ПОЧЕМУ ИМЕННО ЭТО. Ни один из трёх пунктов не виден из
    ответа сервера: урок — это подсветка живого элемента страницы, выгрузка —
    работающая кнопка, а поиск и сворачивание существуют только в браузере.
    Всё, что живёт на сервере (состав демо, содержимое книги, подпись партии),
    проверено там (`tests/test_supply_planning.py`), и здесь не повторяется.
    """
    c5 = _fix5_ui_client(base)
    _fix5_ui_preview_flag("Бренд-UI-Пять")
    browser = pw.chromium.launch()
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    ctx.add_cookies([{"name": k, "value": v, "domain": "127.0.0.1", "path": "/"}
                     for k, v in c5.cookies.items()])
    errors: list[str] = []
    page = ctx.new_page()
    page.on("pageerror", lambda e: errors.append(str(e)))

    steps = (
        ("F-29 порог до одиннадцатой строки", lambda: _fix5_below_threshold(page, base, c5)),
        ("F-27 урок раздела", lambda: _fix5_lesson_ui(ctx, base, errors)),
        ("F-28 кнопка выгрузки", lambda: _fix5_export_ui(page, base)),
        ("F-29 поиск и сворачивание", lambda: _fix5_scale_ui(page, base, c5)),
        ("F-29 состояние в адресе", lambda: _fix5_hash_ui(page, base)),
    )
    for label, run_step in steps:
        try:
            run_step()
        except Exception as exc:  # noqa: BLE001 — важен отчёт, а не тип
            check(f"{label}: шаг дошёл до конца без исключения", False,
                  f"{type(exc).__name__}: "
                  f"{str(exc).strip().splitlines()[0][:160]}")

    check("за сценарий SUPPLY-FIX-5 не было ошибок в консоли",
          not errors, str(errors)[:200])
    ctx.close()
    browser.close()
    c5.close()


def _fix5_cards(page, kind: str) -> int:
    return page.evaluate(
        "(k) => document.querySelectorAll('.pl-card[data-pl=\"' + k + '\"]').length",
        kind)


def _fix5_shown(page, sel: str) -> bool:
    """Виден ли элемент по-настоящему: `getComputedStyle`, а не наличие в HTML."""
    return page.evaluate(
        "(s) => { const n = document.querySelector(s); if (!n) return false;"
        " const st = getComputedStyle(n);"
        " return st.display !== 'none' && st.visibility !== 'hidden'"
        " && n.getBoundingClientRect().height > 0; }", sel)


def _fix5_below_threshold(page, base, c5) -> None:
    """До одиннадцатой строки поиска и сворачивания на экране нет вовсе."""
    print("\n== F-29: маленький список обходится без поиска ==")
    c5.post("/api/supply/planning/materials",
            json={"title": "Шерсть-пять", "qty": "100", "unit": "м", "op_id": "u5-m1"})
    it = c5.post("/api/supply/planning/items",
                 json={"kind": "draft", "title": "Пальто-пять", "op_id": "u5-i1"}
                 ).json()["items"][0]
    board = c5.post("/api/supply/planning/batches",
                    json={"item_id": it["id"], "title": "Запуск-пять",
                          "plan_qty": "10", "op_id": "u5-b1"}).json()
    batch = board["batches"][0]
    mat = board["materials"][0]
    c5.post("/api/supply/planning/assignments",
            json={"material_id": mat["id"], "batch_id": batch["id"], "qty": "30",
                  "op_id": "u5-a1"})
    page.goto(f"{base}/supply")
    page.wait_for_timeout(1200)
    close_hint(page)
    check("карточки на месте", _fix5_cards(page, "material") == 1
          and _fix5_cards(page, "batch") == 1,
          f"{_fix5_cards(page, 'material')}/{_fix5_cards(page, 'batch')}")
    check("полосы поиска материалов нет на экране",
          not _fix5_shown(page, "#pl-mat-find"), "видна")
    check("кнопки «Свернуть» у материалов тоже нет",
          not _fix5_shown(page, "#pl-mat-toggle"), "видна")
    check("и у блока партий их тоже нет",
          not _fix5_shown(page, "#pl-batch-find")
          and not _fix5_shown(page, "#pl-batch-toggle"), "видны")


def _fix5_lesson_ui(ctx, base, errors) -> None:
    """F-27: урок раздела есть в меню «?» и подсвечивает «Добавить материал».

    Своя вкладка на весь шаг: на ней стоит задержка ответа доски
    (`LESSON_DELAY_SCRIPT`), и тащить её в соседние проверки, которым она не
    нужна, значило бы замедлять их ради чужого условия.
    """
    print("\n== F-27: седьмой урок живёт на своей странице ==")
    page = ctx.new_page()
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script(LESSON_DELAY_SCRIPT)
    try:
        _fix5_lesson_steps(page, base)
    finally:
        page.close()


def _fix5_lesson_steps(page, base) -> None:
    page.goto(f"{base}/supply")
    page.wait_for_timeout(1400)
    close_hint(page)
    page.click("#hint-fab")
    page.wait_for_timeout(400)
    check("в меню «?» появился урок этой страницы",
          _fix5_shown(page, "#hm-lesson"), "пункта нет")
    check("счётчик уроков считает семь",
          (page.text_content("#hm-cnt") or "").strip().endswith("/7"),
          str(page.text_content("#hm-cnt")))
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)

    page.goto(f"{base}/supply?lesson=supply")
    # ЖДЁМ, ПОКА ДОСКА ДОРИСУЕТСЯ, И ТОЛЬКО ПОТОМ МЕРЯЕМ. Это не «дать
    # странице время»: цель первого шага лежит В РАЗМЕТКЕ, поэтому обводка
    # встаёт по ещё пустой странице, а список карточек приходит ответом API и
    # сдвигает кнопку вниз на свою высоту. Замер без этого ожидания проверял бы
    # ту половину случаев, где ответ успел прийти раньше, — и молчал бы ровно
    # про ту, где урок подсвечивает пустое место.
    page.wait_for_selector(".pl-card", timeout=10000)
    page.wait_for_timeout(900)
    check("бегунок урока показан", _fix5_shown(page, "#tour-bar"), "бегунка нет")
    check("карточка первого шага — про материал",
          "материал" in (page.text_content(".tour-card .tc-title") or "").lower(),
          str(page.text_content(".tour-card .tc-title")))
    layout = page.evaluate(
        "() => { const b = document.getElementById('pl-add-material');"
        " const l = document.getElementById('pl-materials');"
        " if (!b || !l) return null;"
        " const br = b.getBoundingClientRect(), lr = l.getBoundingClientRect();"
        " return {listHeight: Math.round(lr.height),"
        "         buttonBelow: br.top >= lr.bottom - 1}; }")
    check("список карточек занял место НАД кнопкой — значит кнопка уехала вниз",
          layout and layout["listHeight"] > 0 and layout["buttonBelow"],
          str(layout))
    # КП ТЗ: подсветка стоит именно на «Добавить материал». Сверяются координаты,
    # а не «на странице есть кольцо»: кольцо без цели выглядело бы так же.
    same = page.evaluate(
        "() => { const r = document.querySelector('.tour-ring');"
        " const b = document.getElementById('pl-add-material');"
        " if (!r || !b) return null;"
        " const a = r.getBoundingClientRect(), c = b.getBoundingClientRect();"
        " return Math.abs(a.left + 3 - c.left) < 3 && Math.abs(a.top + 3 - c.top) < 3"
        "     && Math.abs(a.width - 6 - c.width) < 3; }")
    check("кольцо урока стоит на кнопке «Добавить материал»", same is True, str(same))

    # Шаги, которые указывают на строки списка, обязаны иметь на что указать:
    # урок, подсвечивающий пустоту, честнее не показывать вовсе.
    from app import lessons as _lessons
    steps = [l for l in _lessons.CATALOGUE if l["key"] == "supply"][0]["steps"]
    check("в уроке пять шагов пути", len(steps) == 5, str(len(steps)))
    missing = [s["sel"] for s in steps
               if not page.evaluate("(s) => !!document.querySelector(s)", s["sel"])]
    check("каждый селектор урока находит живой элемент раздела",
          not missing, str(missing))


def _fix5_export_ui(page, base) -> None:
    """F-28: кнопка «Скачать xlsx» стоит в шапке раздела и действительно качает."""
    print("\n== F-28: кнопка выгрузки не обещает того, чего не делает ==")
    page.goto(f"{base}/supply")
    page.wait_for_timeout(1200)
    close_hint(page)
    check("кнопка «Скачать xlsx» видна на вкладке плана",
          _fix5_shown(page, "#pl-export"), "не видна")
    check("и ведёт на ручку выгрузки, а не в никуда",
          (page.get_attribute("#pl-export", "href") or "")
          .endswith("/api/supply/planning/export.xlsx"),
          str(page.get_attribute("#pl-export", "href")))
    with page.expect_download(timeout=15000) as info:
        page.click("#pl-export")
    dl = info.value
    path = dl.path()
    head = open(path, "rb").read(4) if path else b""
    check("нажатие действительно скачивает файл",
          str(dl.suggested_filename).endswith(".xlsx"), str(dl.suggested_filename))
    check("и это настоящий xlsx (ZIP-контейнер), а не страница с ошибкой",
          head == b"PK\x03\x04", repr(head))


def _fix5_scale_ui(page, base, c5) -> None:
    """F-29: тридцать материалов и двадцать партий — поиск и сворачивание."""
    print("\n== F-29: тридцать материалов и двадцать партий ==")
    it = c5.post("/api/supply/planning/items",
                 json={"kind": "draft", "title": "Жакет-масштаб", "op_id": "u5-i2"}
                 ).json()["items"][0]
    for i in range(29):
        c5.post("/api/supply/planning/materials",
                json={"title": f"Ткань-масштаб-{i:02d}", "qty": "5", "unit": "м",
                      "op_id": f"u5-sm-{i}"})
    for i in range(18):
        c5.post("/api/supply/planning/batches",
                json={"item_id": it["id"], "title": f"Запуск-масштаб-{i:02d}",
                      "plan_qty": "3", "op_id": f"u5-sb-{i}"})
    # Одна партия НАМЕРЕННО без названия: её подпись — предмет отдельной
    # проверки ниже, и собирать её задним числом было бы поздно.
    c5.post("/api/supply/planning/batches",
            json={"item_id": it["id"], "op_id": "u5-sb-none"})
    board = c5.get("/api/supply/planning").json()
    check("на доске тридцать материалов и двадцать партий",
          len(board["materials"]) == 30 and len(board["batches"]) == 20,
          f"{len(board['materials'])}/{len(board['batches'])}")

    page.goto(f"{base}/supply")
    page.wait_for_timeout(1600)
    close_hint(page)
    check("за порогом полоса поиска появилась",
          _fix5_shown(page, "#pl-mat-find") and _fix5_shown(page, "#pl-batch-find"),
          "не появилась")
    check("и кнопки сворачивания тоже",
          _fix5_shown(page, "#pl-mat-toggle") and _fix5_shown(page, "#pl-batch-toggle"),
          "не появились")
    check("нарисованы все тридцать карточек материалов",
          _fix5_cards(page, "material") == 30, str(_fix5_cards(page, "material")))

    page.fill("#pl-mat-q", "масштаб-07")
    page.wait_for_timeout(400)
    check("поиск сузил список материалов до одного",
          _fix5_cards(page, "material") == 1, str(_fix5_cards(page, "material")))
    check("и сказал, сколько нашлось",
          "найдено 1 из 30" in (page.text_content("#pl-mat-found") or ""),
          str(page.text_content("#pl-mat-found")))
    check("уцелевшая карточка — именно искомая",
          "Ткань-масштаб-07" in (page.text_content("#pl-materials") or ""),
          (page.text_content("#pl-materials") or "")[:80])
    page.fill("#pl-mat-q", "ТКАНЬ-МАСШТАБ-07")
    page.wait_for_timeout(400)
    check("верхний регистр находит ту же строку",
          _fix5_cards(page, "material") == 1, str(_fix5_cards(page, "material")))
    page.fill("#pl-mat-q", "такого нет")
    page.wait_for_timeout(400)
    check("несовпадение сказано словами, а не пустым экраном",
          _fix5_cards(page, "material") == 0
          and "ничего не найдено" in (page.text_content("#pl-mat-found") or ""),
          str(page.text_content("#pl-mat-found")))
    page.click("#pl-mat-q-clear")
    page.wait_for_timeout(400)
    check("«Сбросить» возвращает весь список",
          _fix5_cards(page, "material") == 30 and not page.input_value("#pl-mat-q"),
          str(_fix5_cards(page, "material")))

    page.fill("#pl-batch-q", "масштаб-11")
    page.wait_for_timeout(400)
    check("поиск по партиям сужает свой блок и не трогает соседний",
          _fix5_cards(page, "batch") == 1 and _fix5_cards(page, "material") == 30,
          f"{_fix5_cards(page, 'batch')}/{_fix5_cards(page, 'material')}")
    page.click("#pl-batch-q-clear")
    page.wait_for_timeout(400)

    # Безымянная партия: подпись видна на самой карточке, а не только в ответе.
    check("безымянная партия подписана вещью и номером",
          " · партия №" in (page.text_content("#pl-batches") or ""),
          (page.text_content("#pl-batches") or "")[:120])
    # И имя вещи на такой карточке стоит ОДИН раз: подпись уже начинается с
    # него, поэтому строки «вещь: …» под ней быть не должно.
    twice = page.evaluate("""() => {
      const card = [...document.querySelectorAll('.pl-card[data-pl="batch"]')]
        .find(c => (c.querySelector('.t') || {}).textContent
                   && c.querySelector('.t').textContent.indexOf(' · партия №') >= 0);
      if (!card) return null;
      const head = card.querySelector('.t').textContent.trim();
      const subs = [...card.querySelectorAll('.sub')].map(s => s.textContent.trim());
      return {head: head, subs: subs,
              repeats: subs.some(s => s.indexOf('вещь: ') === 0)};
    }""")
    check("на безымянной карточке имя вещи стоит один раз, а не дважды",
          twice and twice["repeats"] is False, str(twice)[:200])

    # «X · X» ЖИЛО НЕ ТОЛЬКО НА КАРТОЧКЕ. Выпадающие списки «Назначить» и
    # «Перенести» собирали подпись как `(название или вещь) · вещь`, и у
    # безымянной партии обе половины были одним и тем же словом: человек
    # выбирал «куда перенести» из нескольких одинаковых строк. Проверяется
    # именно повтор, а не наличие точки: у названной партии точка законна.
    if page.is_visible("#pl-mat-q-clear"):
        page.click("#pl-mat-q-clear")
        page.wait_for_timeout(300)
    opened = page.evaluate("""() => {
      const card = document.querySelector('.pl-card[data-pl="material"]');
      if (!card) return null;
      const b = [...card.querySelectorAll('button')]
        .find(x => x.textContent.trim() === 'Назначить на партию');
      if (!b) return null;
      b.click();
      return true;
    }""")
    page.wait_for_timeout(600)
    opts = page.evaluate("""() => {
      const sel = document.querySelector('#pl-materials .pl-form.inline select');
      return sel ? [...sel.options].map(o => o.textContent.trim()) : null;
    }""")
    check("форма назначения открылась и в ней есть список партий",
          opened is True and isinstance(opts, list) and len(opts) >= 2,
          str(opts)[:160])
    doubled = [o for o in (opts or []) if " · " in o
               and o.split(" · ")[0].strip() == o.split(" · ")[1].strip()]
    check("ни одна строка списка не повторяет одно и то же слово дважды",
          not doubled, str(doubled)[:160])
    check("а безымянная партия и там названа вещью с номером",
          any(" · партия №" in o for o in (opts or [])), str(opts)[:200])

    page.click("#pl-mat-toggle")
    page.wait_for_timeout(300)
    check("«Свернуть» прячет список по-настоящему",
          not _fix5_shown(page, "#pl-materials"), "список виден")
    check("кнопка называет обратное действие",
          (page.text_content("#pl-mat-toggle") or "").strip() == "Развернуть",
          str(page.text_content("#pl-mat-toggle")))
    check("а соседний блок остался развёрнутым",
          _fix5_shown(page, "#pl-batches"), "свёрнут вместе с чужим")
    page.click("#pl-mat-toggle")
    page.wait_for_timeout(300)
    check("«Развернуть» возвращает список", _fix5_shown(page, "#pl-materials"),
          "список не вернулся")


def _fix5_hash_ui(page, base) -> None:
    """F-29: вкладка и свёрнутые блоки живут в адресе и переживают перезагрузку."""
    print("\n== F-29: состояние экрана лежит в адресе ==")
    page.goto(f"{base}/supply#preview")
    page.wait_for_timeout(1600)
    close_hint(page)
    check("адрес с #preview открывает вторую вкладку",
          _fix5_shown(page, "#sup-view-preview")
          and not _fix5_shown(page, "#sup-view-plan"),
          "открыта не та вкладка")
    check("и переключатель это показывает",
          page.get_attribute("#sup-tab-preview", "aria-selected") == "true",
          str(page.get_attribute("#sup-tab-preview", "aria-selected")))

    page.click("#sup-tab-plan")
    page.wait_for_timeout(300)
    check("возврат на план убирает #preview из адреса",
          "preview" not in (page.evaluate("() => location.hash") or ""),
          str(page.evaluate("() => location.hash")))

    page.click("#pl-mat-toggle")
    page.wait_for_timeout(300)
    check("свёрнутый блок записан в адрес",
          "mat-off" in (page.evaluate("() => location.hash") or ""),
          str(page.evaluate("() => location.hash")))

    page.goto(f"{base}/supply#preview,mat-off")
    page.wait_for_timeout(1600)
    close_hint(page)
    check("после перезагрузки вкладка и свёрнутый блок восстановлены",
          _fix5_shown(page, "#sup-view-preview"), "вкладка не та")
    page.click("#sup-tab-plan")
    page.wait_for_timeout(400)
    check("и блок материалов действительно свёрнут",
          not _fix5_shown(page, "#pl-materials")
          and (page.text_content("#pl-mat-toggle") or "").strip() == "Развернуть",
          str(page.text_content("#pl-mat-toggle")))


if __name__ == "__main__":
    sys.exit(main())
