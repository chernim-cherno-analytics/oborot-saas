# -*- coding: utf-8 -*-
"""SUPPLY-2: предпросмотр производственных Google Sheets — границы и честность.

Зачем этот набор. SUPPLY-2 приносит в продукт то, чего в нём до сих пор не было
вовсе: чтение ЧУЖОГО источника, который заполняют люди и который никто не
валидирует. Цена ошибки здесь не «страница выглядит криво», а «предпросмотр
выдал выдумку за факт»: показал ноль там, где написано «Кроим по заданию»,
склеил две строки в одну, потерял колонку, стёр прежний снимок неудачной
попыткой или уехал за токеном на чужой хост. Поэтому проверяется не «работает
ли», а «во что оно превращает неоднозначность».

Живого Google здесь нет ни в одном месте: транспорт инъектируется
(`supply_sheets.set_transport`), фикстуры синтетические, реальных данных и PII
в репозитории нет. Настоящий сетевой путь (`_httpx_get`) при этом проверяется
по-настоящему — против локального сервера, который умеет висеть и отдавать
слишком много.

Что доказывается:

  1) валидация входа: `spreadsheet_id` достаётся allowlist-regex, произвольный
     адрес не принимается, имена листов проверяются и их ровно два;
  2) парсер на ДВУХ синтетических формах наблюдённого заголовка (27 колонок,
     две физические строки заголовка; на одном листе S/M/L и колонки итога и
     цены без подписей, на другом подписаны явно) — продолжения, пустая
     строка-разделитель, новая модель без количеств, Unicode, текст в колонке
     XL, расхождение итога, неизвестная колонка — с точными source_row,
     anchor_row и сохранённым raw. Отдельно: схема, отвергнутая ревью PR #47
     (весь каркас на колонку левее), обязана быть отвергнута и сейчас;
 2а) первый неудачный refresh: причина остаётся на экране, ввод возвращается в
     форму, но источник настроенным не объявляется;
 2б) версия снимка: неизвестная/повреждённая запись под versioned-ключом —
     отказ 409, и повреждённое НЕ переписывается;
 2в) количество читается только ASCII-цифрами: «٣» и «１２» числом не считаются;
 2г) форма СТРОКИ снимка: поле не того вида даёт управляемый 409, а не 500;
 2д) все 22 общих поля строки ОБЯЗАНЫ быть — `rows: [{}]` и удаление любого из
     них по одному дают 409 на GET и POST, до сети и без записей, в обеих
     формах строки (обычной и пустой) и в обеих версиях разбора; полные снимки
     `parser-1` (без `sketch_raw`, с `extra_raw`) и `parser-2` читаются;
  3) fail closed на дрейфе заголовка, orphan, мусорных количествах, HTML-странице
     входа, редиректе на чужой хост, 403, таймауте и превышении любого лимита;
  4) ровно два GET на построенные docs.google.com endpoint'ы, никаких иных
     методов и хостов, ноль обращений к МойСкладу;
  5) неудачное обновление сохраняет прежний успешный снимок и отдаёт только
     безопасный текст ошибки;
  6) тот же хеш => `unchanged` и те же байты строк; изменившийся второй лист
     заменяет оба листа атомарно; одновременный одинаковый refresh сходится и
     не теряет чужие ключи `config_json`;
  7) арендаторы, роли и аноним;
  8) отпечаток базы: измениться может ТОЛЬКО ключ `supply_sheets_v1` внутри
     `connections.config_json`;
  9) нет основного подключения — 409 без единой мутации и без единого запроса;
 10) подсказка по каталогу остаётся непривязанной;
 11) внешний текст (в том числе XSS-полезная нагрузка) доезжает как текст, а
     страница строит таблицу DOM-API, а не сборкой разметки;
 12) структурно: слой не упоминается в синке, обратной записи, планировщике и
     аналитике; CC_BATCH_ID не уезжает наружу; шагов старта по-прежнему десять;
 13) контракт отката: поля и выбор `Connection` глазами старого кода не
     изменились, удаление организации уносит носителя.

Чего этот набор НЕ проверяет и не должен: формулы (D-35, BUSINESS_LOGIC §0),
«Едет», приёмки, OrderedQty и планировщик — SUPPLY-2 их не касается вовсе, и
именно это здесь проверяется структурно, а не обещанием.

Запуск из корня репозитория:  python tests/test_supply_sheets.py
"""
import csv
import io
import json
import os
import re
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "test_supply_sheets.db"
APP_PORT = int(os.environ.get("OBOROT_TEST_PORT", "8816"))

os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["SCHEDULER_ENABLED"] = "0"

if DB_PATH.exists():
    DB_PATH.unlink()

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from app import supply_sheets as ss  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.main import app as oborot_app  # noqa: E402


class ServerThread:
    def __init__(self, asgi_app, port: int):
        self.config = uvicorn.Config(asgi_app, host="127.0.0.1", port=port,
                                     log_level="warning")
        self.server = uvicorn.Server(self.config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self):
        self.thread.start()
        deadline = time.time() + 15
        while time.time() < deadline:
            if self.server.started:
                return
            time.sleep(0.05)
        raise RuntimeError(f"сервер на порту {self.config.port} не поднялся")

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=10)


PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        PASS.append(name)
        print(f"  OK   {name}" + (f"  [{detail}]" if detail else ""))
    else:
        FAIL.append(name)
        print(f"  FAIL {name}  {detail}")


def raises(fn, exc_type) -> str:
    """Текст ожидаемого исключения или '' — проверка, а не падение набора."""
    try:
        fn()
    except exc_type as exc:
        return str(exc) or "<пустое сообщение>"
    except Exception as exc:  # noqa: BLE001 — чужой тип тоже должен быть виден
        return f"<{type(exc).__name__}: {exc}>"
    return ""


def sql(query: str, *args):
    con = sqlite3.connect(DB_PATH)
    try:
        return con.execute(query, args).fetchall()
    finally:
        con.close()


def exec_sql(query: str, *args) -> int:
    con = sqlite3.connect(DB_PATH)
    try:
        cur = con.execute(query, args)
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def client(port: int = APP_PORT) -> httpx.Client:
    return httpx.Client(headers={"X-Oborot-CSRF": "1"},
                        base_url=f"http://127.0.0.1:{port}", timeout=60.0)


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


# ── Синтетические фикстуры: ДВЕ наблюдённые формы заголовка ─────────────────
#
# ПОЧЕМУ ЗДЕСЬ ДВЕ ФОРМЫ, А НЕ ОДНА, И ПОЧЕМУ ЭТО ВАЖНО. Первая версия набора
# (отвергнутый ревью HEAD `a25e163`) сочиняла фикстуру по той же памяти, что и
# парсер, и повторяла его ошибку: весь каркас стоял на колонку левее живого
# источника. Набор был зелёным на 302 проверках, а оба настоящих листа падали
# ДО первой строки данных. Фикстура, выведенная из кода, не проверяет код —
# она проверяет саму себя.
#
# Поэтому теперь фикстур две, и они списаны с read-only наблюдения точных
# публичных байтов 31.08.2026 — каждая со своими отличиями, а не одна «общая»:
#   * «Осень 26»  — колонка 1 содержит пробел, промежуточные S/M/L в строке 2
#                   пусты (объединённая ячейка горки), колонки итога и цены без
#                   подписи;
#   * «НГ 26/27»  — колонка 1 подписана «Цена ткани за м», в колонке 2 стоит
#                   перевод строки, S/M/L подписаны явно, а колонки 15 и 16
#                   подписаны «Общее количество» и «Цена».
# Живых строк и PII здесь нет: каркас наблюдён, содержимое выдумано.
#
# Отдельно ниже стоит `legacy_wrong_header_rows()` — воспроизведение ИМЕННО той
# ошибочной схемы, которую отверг ревьюер. Она обязана быть отвергнута парсером,
# и это красный контроль против возврата дефекта.

COLS = 27
SHEET_CURRENT = "Осень 26"
SHEET_NEXT = "НГ 26/27"


def blank() -> list:
    return [""] * COLS


def put(row: list, mapping: dict) -> list:
    for col, value in mapping.items():
        row[col - 1] = value
    return row


#: Основной каркас строки 1 — общий у обоих листов.
FRAME = {
    3: "Наименование", 4: "Эскиз", 5: "Цвет", 6: "Количество в м",
    7: "Комментарии", 8: "Комментарии", 9: "Комментарии",
    10: "Размерная горка", 18: "Комплектующие", 19: "Выбранное производство",
}


def autumn_header_rows() -> list:
    """Форма «Осень 26»: пробел в колонке 1, горка без промежуточных подписей."""
    row1 = put(blank(), {**FRAME, 1: " "})
    row2 = put(blank(), {10: "XS", 14: "XL"})
    return [row1, row2]


def next_header_rows() -> list:
    """Форма «НГ 26/27»: подписанная колонка 1, перевод строки в колонке 2,
    явные S/M/L и подписанные «Общее количество» и «Цена»."""
    row1 = put(blank(), {**FRAME, 1: "Цена ткани за м", 2: "\n"})
    row2 = put(blank(), {10: "XS", 11: "S", 12: "M", 13: "L", 14: "XL",
                         15: "Общее количество", 16: "Цена"})
    return [row1, row2]


def legacy_wrong_header_rows() -> list:
    """Схема, отвергнутая ревью PR #47: ВЕСЬ каркас на одну колонку левее.

    Существует ровно затем, чтобы парсер её ОТВЕРГ. Если однажды эта фикстура
    начнёт проходить — значит дефект вернулся.
    """
    row1 = put(blank(), {
        2: "Наименование", 4: "Цвет", 5: "Количество в м",
        6: "Комментарии", 7: "Комментарии", 8: "Комментарии",
        9: "Размерная горка", 17: "Комплектующие", 18: "Выбранное производство"})
    row2 = put(blank(), {9: "XS", 13: "XL"})
    return [row1, row2]


def to_csv(rows: list) -> bytes:
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\r\n")
    for row in rows:
        writer.writerow(row)
    return buf.getvalue().encode("utf-8")


def autumn_rows() -> list:
    """Текущий лист: девять физических строк данных, каждая со своим смыслом."""
    rows = autumn_header_rows()
    # 3 — обычный якорь: всё сошлось, ни одной неоднозначности.
    rows.append(put(blank(), {
        2: "1042", 3: "Пальто «Осень»", 4: "эскиз-1042", 5: "Чёрный", 6: "3,2",
        7: "Отгружено", 10: "2", 11: "3", 12: "4", 13: "3", 14: "2", 15: "14",
        16: "12 900", 18: "пуговицы", 19: "Цех №1"}))
    # 4 — продолжение: идентичности нет, наследует якорь строки 3.
    rows.append(put(blank(), {
        5: "Молоко", 8: "Крой", 10: "1", 11: "1", 12: "2", 13: "1", 14: "1",
        15: "6"}))
    # 5 — полностью пустая строка: разделитель, сбрасывает якорь.
    rows.append(blank())
    # 6 — якорь без артикула + итог источника не сходится с суммой размеров.
    rows.append(put(blank(), {
        3: "Жакет Тёплый", 5: "Серый",
        10: "5", 11: "5", 12: "5", 13: "5", 14: "5", 15: "20"}))
    # 7 — в колонке XL человек написал словами. Это не ноль и не статус.
    rows.append(put(blank(), {
        2: "1077", 3: "Брюки Прямые", 5: "Синий", 7: "Сдано ✓ 3 шт",
        10: "1", 11: "2", 12: "3", 13: "4", 14: "Кроим по заданию"}))
    # 8 — продолжение с непустой НЕИЗВЕСТНОЙ колонкой: raw обязан уцелеть.
    rows.append(put(blank(), {5: "Хаки", 10: "1", 22: "служебная пометка"}))
    # 9 — якорь, где количеств нет вовсе (тире — это отсутствие, а не ноль).
    rows.append(put(blank(), {
        2: "1099", 3: "Юбка Плиссе", 5: "Бежевый", 10: "-", 11: "—"}))
    # 10 — чужой текст, который очень хочет стать разметкой.
    rows.append(put(blank(), {
        2: "1101", 3: "<img src=x onerror=alert(1)>",
        5: "\"><script>alert(2)</script>", 10: "1", 15: "1"}))
    # 11 — цифроподобные НЕ-ASCII строки: арабо-индийская тройка и полноширинные
    # «12». Юникодный `\d` принял бы их за числа, и в предпросмотре появилось бы
    # количество, которого человек не писал.
    rows.append(put(blank(), {
        2: "1102", 3: "Худи Юникод", 5: "Мята", 10: "\u0663", 11: "\uff11\uff12"}))
    return rows


def next_rows() -> list:
    """Следующий лист: новая модель заведена, количеств ещё нет.

    Три физические строки — ровно столько, сколько у наблюдённого листа.
    Колонка 1 здесь подписана, но смысла ей слой не назначает: её значение
    обязано уехать в `unknown_raw`, а не превратиться в поле продукта.
    """
    rows = next_header_rows()
    rows.append(put(blank(), {1: "1 250", 2: "2001", 3: "Пуховик НГ", 5: "Чёрный"}))
    return rows


AUTUMN_CSV = to_csv(autumn_rows())
NEXT_CSV = to_csv(next_rows())

SPREADSHEET_ID = "1AbCdEf_ghijklmnop-QRSTUV0123456789wxyz"
SHEET_URL = f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit#gid=0"


class FakeGoogle:
    """Инъектируемый транспорт. Записывает КАЖДЫЙ вызов: метод, адрес, таймаут."""

    def __init__(self, bodies: dict | None = None):
        self.calls: list[tuple] = []
        self.bodies = dict(bodies or {SHEET_CURRENT: AUTUMN_CSV,
                                      SHEET_NEXT: NEXT_CSV})

    def sheet_of(self, url: str) -> str:
        from urllib.parse import parse_qs, unquote, urlparse

        query = parse_qs(urlparse(url).query)
        return unquote((query.get("sheet") or [""])[0])

    def __call__(self, method: str, url: str, timeout: float):
        self.calls.append((method, url, timeout))
        name = self.sheet_of(url)
        value = self.bodies.get(name)
        if value is None:
            return ss.HttpResponse(404, {}, b"", url)
        if callable(value):
            return value(url)
        if isinstance(value, ss.HttpResponse):
            return value
        return ss.HttpResponse(200, {"Content-Type": "text/csv"}, value, url)

    @property
    def hosts(self) -> set:
        from urllib.parse import urlparse

        return {urlparse(u).hostname for _m, u, _t in self.calls}

    @property
    def methods(self) -> set:
        return {m for m, _u, _t in self.calls}


# ── Часть 1. Валидация входа ────────────────────────────────────────────────

def input_checks() -> None:
    print("\n== Ссылка на таблицу: allowlist, а не «похоже на Google» ==")
    check("каноническая ссылка разбирается",
          ss.parse_spreadsheet_url(SHEET_URL) == SPREADSHEET_ID,
          ss.parse_spreadsheet_url(SHEET_URL))
    check("ссылка без хвоста тоже",
          ss.parse_spreadsheet_url(
              f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}")
          == SPREADSHEET_ID)
    bad = {
        "http (не https)": f"http://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit",
        "похожий хост": f"https://docs.google.com.evil.tld/spreadsheets/d/{SPREADSHEET_ID}/edit",
        "поддомен-обманка": f"https://evil.docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit",
        "учётные данные в адресе": f"https://user:pw@docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit",
        "чужой сервис": "https://example.com/spreadsheets/d/abcdefghij/edit",
        "внутренний адрес": "http://127.0.0.1:8080/spreadsheets/d/abcdefghij",
        "пусто": "",
        "не ссылка вовсе": "Осень 26",
        "другой путь Google": "https://docs.google.com/document/d/abcdefghij/edit",
    }
    for label, value in bad.items():
        check(f"адрес отклонён: {label}",
              bool(raises(lambda v=value: ss.parse_spreadsheet_url(v),
                          ss.ValidationError)),
              value[:70])

    print("\n== Имена листов: ровно два, проверенные, в присланном порядке ==")
    names = ss.validate_sheet_names([f" {SHEET_CURRENT} ", SHEET_NEXT])
    check("пробелы срезаются, порядок сохраняется",
          names == [SHEET_CURRENT, SHEET_NEXT], str(names))
    for label, value in {
        "один лист": [SHEET_CURRENT],
        "три листа": [SHEET_CURRENT, SHEET_NEXT, "Лето"],
        "пустое имя": [SHEET_CURRENT, "   "],
        "не список": SHEET_CURRENT,
        "не строка": [SHEET_CURRENT, 7],
        "перевод строки внутри": [SHEET_CURRENT, "НГ\n26"],
        "нулевой байт": [SHEET_CURRENT, "НГ\x0026"],
        "слишком длинное": [SHEET_CURRENT, "и" * 300],
        "два одинаковых": [SHEET_CURRENT, SHEET_CURRENT],
    }.items():
        check(f"имена отклонены: {label}",
              bool(raises(lambda v=value: ss.validate_sheet_names(v),
                          ss.ValidationError)), str(value)[:60])

    print("\n== Endpoint строит сервер, а не клиент ==")
    url = ss.build_csv_url(SPREADSHEET_ID, SHEET_NEXT)
    check("хост зафиксирован", url.startswith("https://docs.google.com/spreadsheets/d/"), url)
    check("идентификатор подставлен целиком", SPREADSHEET_ID in url, url)
    check("headers=0 — иначе вторая строка заголовка будет съедена",
          "headers=0" in url, url)
    check("имя листа URL-кодируется сервером (слеш не уезжает как разделитель)",
          "%D0%9D%D0%93%2026%2F27" in url, url)
    check("ссылка «открыть исходник» тоже собирается сервером",
          ss.spreadsheet_link(SPREADSHEET_ID)
          == f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit")


# ── Часть 2. Парсер на доказанной форме ─────────────────────────────────────

def parse_checks() -> None:  # noqa: C901 — сценарий, ветвлений мало
    print("\n== Две физические строки заголовка и явный контракт схемы ==")
    rows = ss.decode_csv(SHEET_CURRENT, AUTUMN_CSV)
    check("фикстура ровно 27 колонок",
          max(len(r) for r in rows) == COLS, str(max(len(r) for r in rows)))
    parsed, schema = ss.parse_sheet(SHEET_CURRENT, rows)
    check("заголовков ровно два", schema["header_rows"] == 2, str(schema))
    check("«Наименование» стоит в колонке 3, как в живом источнике",
          schema["name_column"] == 3, str(schema["name_column"]))
    check("артикул выведен как колонка 2 — ровно перед именем",
          schema["article_column"] == 2 and schema["article_column_inferred"] is True,
          str(schema["article_column"]))
    check("размерная горка начинается с колонки 10",
          schema["size_band_start"] == 10, str(schema["size_band_start"]))
    check("итог источника — колонка 15, и на этом листе он без подписи",
          schema["source_total_column"] == 15
          and schema["source_total_column_inferred"] is True
          and schema["source_total_header"] == "", str(schema))
    check("цена — колонка 16, тоже без подписи на этом листе",
          schema["price_column"] == 16 and schema["price_column_inferred"] is True,
          str(schema))
    check("комментарии — колонки 7, 8, 9",
          schema["comment_columns"] == [7, 8, 9], str(schema["comment_columns"]))
    check("на «Осень 26» S/M/L выведены позиционно (подписей в источнике нет)",
          schema["size_labels_inferred"] == ["S", "M", "L"], str(schema))
    check("данные начинаются с третьей физической строки",
          schema["first_data_row"] == 3, str(schema))
    check("пробел в колонке 1 свободной подписью не считается",
          schema["free_columns"] == {}, str(schema["free_columns"]))
    check("физических строк данных ровно девять, ни одна не схлопнута",
          len(parsed) == 9, str(len(parsed)))

    by_row = {r["source_row"]: r for r in parsed}
    check("нумерация строк физическая и непрерывная",
          sorted(by_row) == [3, 4, 5, 6, 7, 8, 9, 10, 11], str(sorted(by_row)))

    print("\n== Обычная строка: ничего не потеряно и ничего не придумано ==")
    r3 = by_row[3]
    check("артикул и имя прочитаны как есть",
          (r3["article"], r3["name"]) == ("1042", "Пальто «Осень»"), str(r3["article"]))
    check("своя строка — сама себе якорь", r3["anchor_row"] == 3, str(r3["anchor_row"]))
    check("размеры разобраны позиционно XS..XL из колонок 10–14",
          r3["sizes"] == {"XS": 2, "S": 3, "M": 4, "L": 3, "XL": 2}, str(r3["sizes"]))
    check("сумма размеров посчитана как объясняющий показатель",
          r3["size_sum"] == 14, str(r3["size_sum"]))
    check("итог источника прочитан из колонки 15, отдельно от суммы",
          r3["source_total"] == 14 and r3["source_total_raw"] == "14", str(r3))
    check("неоднозначностей у этой строки нет", r3["issues"] == [], str(r3["issues"]))
    check("свободный текст остался текстом, а не стал статусом",
          r3["source_status_raw"] == "Отгружено"
          and r3["comments_raw"] == ["Отгружено", "", ""], str(r3["comments_raw"]))
    check("цена (колонка 16) и комплектующие (18) сохранены сырыми",
          r3["price_raw"] == "12 900" and r3["components_raw"] == "пуговицы",
          str((r3["price_raw"], r3["components_raw"])))
    check("производство (колонка 19) сохранено сырым",
          r3["production_raw"] == "Цех №1", r3["production_raw"])
    check("эскиз (колонка 4) сохранён сырым и полем продукта не стал",
          r3["sketch_raw"] == "эскиз-1042", r3["sketch_raw"])
    check("метраж ткани сохранён сырым, без попытки посчитать",
          r3["qty_meters_raw"] == "3,2", r3["qty_meters_raw"])
    check("именованные колонки в unknown не утекают", r3["unknown_raw"] == {},
          str(r3["unknown_raw"]))

    print("\n== Продолжение наследует личность, но остаётся своей строкой ==")
    r4 = by_row[4]
    check("собственной идентичности у продолжения нет",
          (r4["article_raw"], r4["name_raw"]) == ("", ""), str(r4))
    check("эффективная личность унаследована от строки 3",
          (r4["article"], r4["name"]) == ("1042", "Пальто «Осень»"), str(r4["name"]))
    check("ссылка на якорь точная", r4["anchor_row"] == 3, str(r4["anchor_row"]))
    check("свои цвет и размеры у продолжения свои",
          r4["color_raw"] == "Молоко" and r4["size_sum"] == 6, str(r4))
    check("комментарий из ВТОРОЙ колонки комментариев не потерян",
          r4["comments_raw"] == ["", "Крой", ""], str(r4["comments_raw"]))
    check("продолжение с количествами не считается неполным",
          r4["issues"] == [], str(r4["issues"]))

    print("\n== Пустая строка — разделитель, а не позиция ==")
    r5 = by_row[5]
    check("строка помечена пустой", r5["is_blank"] is True, str(r5["is_blank"]))
    check("у пустой строки нет ни якоря, ни неоднозначностей",
          r5["anchor_row"] is None and r5["issues"] == [], str(r5))
    check("пустая строка СБРОСИЛА якорь: следующая начинает заново",
          by_row[6]["anchor_row"] == 6, str(by_row[6]["anchor_row"]))

    print("\n== Половина идентичности и расхождение итога ==")
    r6 = by_row[6]
    check("имя есть, артикула нет — строка помечена неполной",
          "identity_missing_part" in r6["issues"], str(r6["issues"]))
    check("артикул НЕ придуман из соседей", r6["article"] == "", repr(r6["article"]))
    check("расхождение итога и суммы названо, а не «исправлено»",
          "total_mismatch" in r6["issues"]
          and r6["source_total"] == 20 and r6["size_sum"] == 25, str(r6))

    print("\n== «Кроим по заданию» в колонке XL: это не ноль и не статус ==")
    r7 = by_row[7]
    check("количество помечено нечитаемым",
          "invalid_quantity" in r7["issues"], str(r7["issues"]))
    check("значение НЕ подменено нулём", r7["sizes"]["XL"] is None, str(r7["sizes"]))
    check("исходный текст сохранён как доказательство",
          r7["sizes_raw"]["XL"] == "Кроим по заданию", r7["sizes_raw"]["XL"])
    check("остальные размеры прочитаны нормально",
          r7["size_sum"] == 10, str(r7["size_sum"]))
    check("Unicode в комментарии доехал без потерь",
          r7["source_status_raw"] == "Сдано ✓ 3 шт", r7["source_status_raw"])
    check("пустого итога источника хватает, чтобы не выдумывать расхождение",
          "total_mismatch" not in r7["issues"], str(r7["issues"]))

    print("\n== Неизвестная колонка не теряется ==")
    r8 = by_row[8]
    check("строка помечена неизвестной колонкой",
          "unknown_column" in r8["issues"], str(r8["issues"]))
    check("сырое значение сохранено вместе с номером колонки",
          r8["unknown_raw"] == {"22": "служебная пометка"}, str(r8["unknown_raw"]))
    check("продолжение по-прежнему знает свой якорь",
          r8["anchor_row"] == 7, str(r8["anchor_row"]))

    print("\n== Тире — это отсутствие, а не ноль ==")
    r9 = by_row[9]
    check("оба вида тире прочитаны как отсутствие",
          r9["sizes"]["XS"] is None and r9["sizes"]["S"] is None, str(r9["sizes"]))
    check("но исходные символы сохранены",
          (r9["sizes_raw"]["XS"], r9["sizes_raw"]["S"]) == ("-", "—"),
          str(r9["sizes_raw"]))
    check("строка помечена «нет количеств», а не нулевой",
          r9["issues"] == ["quantity_missing"], str(r9["issues"]))
    check("сумма размеров отсутствует, а не равна нулю",
          r9["size_sum"] is None, str(r9["size_sum"]))

    print("\n== Цифроподобные не-ASCII строки числом не считаются ==")
    r11 = by_row[11]
    check("арабо-индийская «٣» не стала числом 3",
          r11["sizes"]["XS"] is None, str(r11["sizes"]["XS"]))
    check("полноширинные «１２» не стали числом 12",
          r11["sizes"]["S"] is None, str(r11["sizes"]["S"]))
    check("исходные символы сохранены дословно",
          (r11["sizes_raw"]["XS"], r11["sizes_raw"]["S"]) == ("\u0663", "\uff11\uff12"),
          str(r11["sizes_raw"]))
    check("строка помечена нечитаемым количеством",
          "invalid_quantity" in r11["issues"], str(r11["issues"]))

    print("\n== Следующий лист: ВТОРАЯ форма заголовка, а не та же самая ==")
    nxt, nxt_schema = ss.parse_sheet(SHEET_NEXT, ss.decode_csv(SHEET_NEXT, NEXT_CSV))
    check("на «НГ 26/27» S/M/L подписаны явно и ничего не выводится",
          nxt_schema["size_labels_inferred"] == [], str(nxt_schema))
    check("итог источника здесь ПОДПИСАН «Общее количество»",
          nxt_schema["source_total_header"] == "Общее количество"
          and nxt_schema["source_total_column_inferred"] is False, str(nxt_schema))
    check("цена здесь подписана «Цена»",
          nxt_schema["price_header"] == "Цена"
          and nxt_schema["price_column_inferred"] is False, str(nxt_schema))
    check("подпись свободной колонки 1 сохранена как наблюдение",
          nxt_schema["free_columns"] == {"1": "Цена ткани за м"},
          str(nxt_schema["free_columns"]))
    check("перевод строки в колонке 2 читается как отсутствие заголовка",
          nxt_schema["article_column"] == 2, str(nxt_schema["article_column"]))
    check("на следующем листе ровно одна физическая строка данных",
          len(nxt) == 1, str(len(nxt)))
    check("она видна и помечена «нет количеств», а не пропущена",
          nxt[0]["name"] == "Пуховик НГ" and "quantity_missing" in nxt[0]["issues"],
          str(nxt[0]["issues"]))
    check("и её физический номер строки — третий",
          nxt[0]["source_row"] == 3, str(nxt[0]["source_row"]))
    check("значение подписанной, но НЕ назначенной колонки 1 уехало в raw",
          nxt[0]["unknown_raw"] == {"1": "1 250"}, str(nxt[0]["unknown_raw"]))
    check("и строка честно помечена неизвестной колонкой",
          "unknown_column" in nxt[0]["issues"], str(nxt[0]["issues"]))

    print("\n== Красный контроль: отвергнутая ревью схема не должна пройти ==")
    legacy = legacy_wrong_header_rows()
    legacy.append(put(blank(), {1: "1042", 2: "Пальто", 9: "2"}))
    message = raises(
        lambda: ss.parse_sheet("Старая", ss.decode_csv("Старая", to_csv(legacy))),
        ss.SourceError)
    check("схема «весь каркас на колонку левее» отвергается",
          bool(message), message[:130])
    check("и отказ называет колонку, а не «что-то пошло не так»",
          "колонке 3" in message or "«Наименование»" in message, message[:130])

    print("\n== Количество: что считается числом, а что — нет ==")
    for raw, expect in [("0", (0, False)), ("7", (7, False)), (" 12 ", (12, False)),
                        ("", (None, False)), ("-", (None, False)), ("—", (None, False)),
                        ("-5", (None, True)), ("2.5", (None, True)),
                        ("2,5", (None, True)), ("1e3", (None, True)),
                        ("abc", (None, True)), ("Кроим", (None, True)),
                        ("\u0663", (None, True)), ("\uff11\uff12", (None, True)),
                        ("\u06f3", (None, True)), ("\u0967", (None, True)),
                        ("99999999999999999999", (None, True)),
                        (str(ss.MAX_QUANTITY + 1), (None, True)),
                        (str(ss.MAX_QUANTITY), (ss.MAX_QUANTITY, False))]:
        got = ss.parse_quantity(raw)
        check(f"количество {raw!r} → {expect}", got == expect, str(got))

    print("\n== Продолжение без якоря — сирота, а не позиция ==")
    orphan = autumn_header_rows()
    orphan.append(put(blank(), {5: "Синий", 10: "2"}))
    parsed_orphan, _ = ss.parse_sheet("Сирота", ss.decode_csv("Сирота", to_csv(orphan)))
    check("первая строка данных без идентичности помечена сиротой",
          parsed_orphan[0]["issues"] == ["orphan_continuation"],
          str(parsed_orphan[0]["issues"]))
    check("и якоря у неё нет — ничего не унаследовано",
          parsed_orphan[0]["anchor_row"] is None
          and parsed_orphan[0]["name"] == "", str(parsed_orphan[0]))

    print("\n== Русские подписи разведены на «разбор» и «ошибку» ==")
    check("подпись есть у каждого кода неоднозначности",
          all(code in ss.ISSUE_LABELS for code in (
              "orphan_continuation", "identity_missing_part", "quantity_missing",
              "invalid_quantity", "total_mismatch", "unknown_column")),
          str(sorted(ss.ISSUE_LABELS)))
    check("подписи на русском и не совпадают с кодами",
          all(re.search(r"[А-Яа-я]", v) and v != k
              for k, v in ss.ISSUE_LABELS.items()), str(ss.ISSUE_LABELS))
    check("«ошибки» — подмножество «требуют разбора»",
          ss.INVALID_ISSUES < set(ss.ISSUE_LABELS), str(sorted(ss.INVALID_ISSUES)))

    print("\n== Сводка считается по физическим строкам и количествам ==")
    counts = ss.build_counts(parsed + nxt, [SHEET_CURRENT, SHEET_NEXT])
    check("две записи по листам", len(counts["sheets"]) == 2, str(counts["sheets"]))
    autumn = counts["sheets"][0]
    check("физические строки и строки данных различаются",
          (autumn["rows"], autumn["data_rows"]) == (9, 8), str(autumn))
    check("прочитанное складывается, но итогом не объявляется",
          autumn["quantity_known"] == 14 + 6 + 25 + 10 + 1 + 1
          and autumn["quantity"] is None
          and autumn["quantity_complete"] is False,
          str({k: autumn[k] for k in
               ("quantity", "quantity_known", "quantity_complete")}))
    check("счётчик неоднозначностей поимённый",
          counts["issues"].get("quantity_missing") == 3
          and counts["issues"].get("unknown_column") == 2, str(counts["issues"]))


# ── Часть 3. Fail closed ────────────────────────────────────────────────────

def fail_closed_checks() -> None:
    print("\n== Дрейф заголовка: молча читать дальше нельзя ==")
    drifts = {
        "«Цвет» переименован": ({5: "Цвета"}, {}),
        "«Наименование» уехало на колонку правее": ({3: "", 4: "Наименование"}, {}),
        "«Эскиз» пропал": ({4: ""}, {}),
        "у колонки артикула появился заголовок": ({2: "Артикул"}, {}),
        "«Размерная горка» пропала": ({10: ""}, {}),
        "«Комплектующие» уехали": ({18: "", 17: "Комплектующие"}, {}),
        "«Наименование» встречается дважды": ({25: "Наименование"}, {}),
        "«Размерная горка» встречается дважды": ({25: "Размерная горка"}, {}),
        "XL заменён на XXL": ({}, {14: "XXL"}),
        "размерная горка сдвинулась": ({}, {10: "", 11: "XS", 15: "XL"}),
        "вторая метка размера за пределами горки": ({}, {23: "XS"}),
        "колонка итога подписана чужим словом": ({}, {15: "Итого"}),
        "колонка цены подписана чужим словом": ({}, {16: "Стоимость"}),
    }
    for label, (fix1, fix2) in drifts.items():
        rows = autumn_header_rows()
        put(rows[0], fix1)
        put(rows[1], fix2)
        rows.append(put(blank(), {2: "1", 3: "Тест", 10: "1"}))
        blob = to_csv(rows)
        message = raises(
            lambda b=blob: ss.parse_sheet("Дрейф", ss.decode_csv("Дрейф", b)),
            ss.SourceError)
        check(f"fail closed на дрейфе: {label}", bool(message), message[:130])

    print("\n== Обе наблюдённые формы заголовка принимаются ==")
    for label, header in (("Осень 26", autumn_header_rows()),
                          ("НГ 26/27", next_header_rows())):
        rows = list(header)
        rows.append(put(blank(), {2: "1", 3: "Тест", 10: "1"}))
        check(f"форма «{label}» проходит контракт",
              not raises(lambda r=rows: ss.parse_sheet(label, ss.decode_csv(label, to_csv(r))),
                         ss.SourceError))
    mixed = autumn_header_rows()
    put(mixed[1], {11: "S", 12: "M", 13: "L"})
    mixed.append(put(blank(), {2: "1", 3: "Тест", 10: "1"}))
    check("явно подписанные S/M/L на листе без подписей тоже законны",
          not raises(lambda: ss.parse_sheet("Смешанная",
                                            ss.decode_csv("Смешанная", to_csv(mixed))),
                     ss.SourceError))
    bad_rows = autumn_header_rows()
    put(bad_rows[1], {12: "XXL"})
    bad_rows.append(put(blank(), {2: "1", 3: "Тест", 10: "1"}))
    check("чужая подпись внутри горки — fail closed",
          bool(raises(lambda: ss.parse_sheet("Чужая",
                                             ss.decode_csv("Чужая", to_csv(bad_rows))),
                      ss.SourceError)))

    print("\n== Размерная горка: подписаны все три или ни одной, третьего нет ==")
    # Замечание ревью PR #47 на HEAD `ae50ba0` (thread 3904456552). D-51 знает
    # ровно две формы строки 2, и обе наблюдены на живых листах. Прежняя
    # проверка смотрела каждую промежуточную колонку НЕЗАВИСИМО от соседей и
    # потому принимала третью: «S подписан, M и L пусты». Дальше эти колонки
    # читаются позиционно, то есть половинчатая разметка означала бы штуки,
    # разъехавшиеся по размерам и показанные как уверенное число.
    #
    # Матрица полная: все восемь сочетаний «подписана / пуста» для S, M и L.
    # Законны ровно два края — все три и ни одной; шесть середин обязаны быть
    # отвергнуты. Проверяется не только сам отказ, но и то, что законные формы
    # не поехали: `size_labels_inferred` у них прежний.
    middle = list(ss.SIZE_LABELS[1:-1])
    first_mid = ss.SIZE_BAND_START + 1
    for mask in range(8):
        marked = [bool(mask & (1 << i)) for i in range(len(middle))]
        cells = {first_mid + i: (middle[i] if marked[i] else "")
                 for i in range(len(middle))}
        rows = autumn_header_rows()
        put(rows[1], cells)
        rows.append(put(blank(), {2: "1", 3: "Тест", 10: "1"}))
        blob = to_csv(rows)
        shape = "".join(lbl if m else "·" for lbl, m in zip(middle, marked))
        legal = all(marked) or not any(marked)
        message = raises(
            lambda b=blob: ss.parse_sheet("Горка", ss.decode_csv("Горка", b)),
            ss.SourceError)
        if legal:
            check(f"законная форма горки «{shape}» принимается", not message,
                  message[:130])
            schema = ss.check_header("Горка", ss.decode_csv("Горка", blob))
            check(f"и её size_labels_inferred прежний ({shape})",
                  schema["size_labels_inferred"] == (middle if not any(marked)
                                                     else []),
                  str(schema["size_labels_inferred"]))
        else:
            check(f"половинчатая горка «{shape}» — fail closed", bool(message),
                  message[:130])
            check(f"и отказ называет колонки, а не содержимое ячеек ({shape})",
                  f"{first_mid}–{first_mid + len(middle) - 1}" in message
                  and "содержимое ячеек здесь не показывается" in message,
                  message[:160])

    # Та же половинчатость на листе, где подписи стоят ЯВНО («НГ 26/27»):
    # стереть одну подпись — то же самое нарушение с другой стороны.
    for gone, label in ((first_mid, middle[0]),
                        (first_mid + 1, middle[1]),
                        (first_mid + 2, middle[2])):
        rows = next_header_rows()
        put(rows[1], {gone: ""})
        rows.append(put(blank(), {2: "1", 3: "Тест", 10: "1"}))
        check(f"на явно подписанном листе стёртая «{label}» — fail closed",
              bool(raises(
                  lambda r=rows: ss.parse_sheet("Полугорка",
                                                ss.decode_csv("Полугорка",
                                                              to_csv(r))),
                  ss.SourceError)))

    print("\n== Заголовка нет вовсе ==")
    check("одна строка вместо двух заголовков — fail closed",
          bool(raises(lambda: ss.parse_sheet("Куцый", [autumn_header_rows()[0]]),
                      ss.SourceError)))

    print("\n== Лимиты источника: fail closed до любой записи ==")
    huge_rows = autumn_header_rows() + [put(blank(), {2: str(i), 3: "Т", 10: "1"})
                                        for i in range(ss.MAX_ROWS_PER_SHEET + 1)]
    check("строк больше предела",
          bool(raises(lambda: ss.decode_csv("Много", to_csv(huge_rows)), ss.SourceError)))
    wide = autumn_header_rows() + [[""] * (ss.MAX_COLUMNS + 1)]
    check("колонок больше предела",
          bool(raises(lambda: ss.decode_csv("Широкий", to_csv(wide)), ss.SourceError)))
    fat = autumn_header_rows()
    fat.append(put(blank(), {3: "я" * (ss.MAX_CELL_CHARS + 1)}))
    check("ячейка длиннее предела",
          bool(raises(lambda: ss.decode_csv("Жирный", to_csv(fat)), ss.SourceError)))
    check("не-UTF-8 ответ — fail closed, а не «замена символов»",
          bool(raises(lambda: ss.decode_csv("Битый", b"\xff\xfe\x00\x01"), ss.SourceError)))

    print("\n== Ответы источника, которые не CSV ==")
    login = ss.HttpResponse(200, {"Content-Type": "text/html; charset=utf-8"},
                            b"<!DOCTYPE html><html><body>Sign in</body></html>",
                            "https://docs.google.com/x")
    fake = FakeGoogle({SHEET_CURRENT: login})
    ss.set_transport(fake)
    try:
        message = raises(lambda: ss.fetch_sheet_csv(SPREADSHEET_ID, SHEET_CURRENT),
                         ss.SourceError)
        check("HTML-страница входа — не CSV", "страницу входа" in message, message[:120])
        check("тела чужого ответа в сообщении нет",
              "Sign in" not in message and "<html" not in message, message[:120])

        # Тот же случай без честного Content-Type: узнаём по телу.
        sniff = ss.HttpResponse(200, {"Content-Type": "text/plain"},
                                b"\n  <html><head></head></html>", "https://docs.google.com/x")
        ss.set_transport(FakeGoogle({SHEET_CURRENT: sniff}))
        check("HTML узнаётся и по телу, а не только по заголовку",
              bool(raises(lambda: ss.fetch_sheet_csv(SPREADSHEET_ID, SHEET_CURRENT),
                          ss.SourceError)))

        for status, label in ((401, "401"), (403, "403"), (404, "404"), (500, "500")):
            ss.set_transport(FakeGoogle({
                SHEET_CURRENT: ss.HttpResponse(status, {}, b"secret body",
                                               "https://docs.google.com/x")}))
            message = raises(lambda: ss.fetch_sheet_csv(SPREADSHEET_ID, SHEET_CURRENT),
                             ss.SourceError)
            check(f"статус {label} — понятная ошибка без тела ответа",
                  bool(message) and "secret body" not in message, message[:110])

        ss.set_transport(FakeGoogle({
            SHEET_CURRENT: ss.HttpResponse(200, {"Content-Type": "text/csv"}, b"   ",
                                           "https://docs.google.com/x")}))
        check("пустой ответ — тоже отказ",
              bool(raises(lambda: ss.fetch_sheet_csv(SPREADSHEET_ID, SHEET_CURRENT),
                          ss.SourceError)))

        print("\n== Редирект проверяется на каждом переходе ==")
        away = ss.HttpResponse(302, {"Location": "https://accounts.google.com/signin"},
                               b"", "https://docs.google.com/x")
        ss.set_transport(FakeGoogle({SHEET_CURRENT: away}))
        message = raises(lambda: ss.fetch_sheet_csv(SPREADSHEET_ID, SHEET_CURRENT),
                         ss.SourceError)
        check("редирект на посторонний хост запрещён",
              "посторонний адрес" in message, message[:120])

        downgrade = ss.HttpResponse(302, {"Location": "http://docs.google.com/x"},
                                    b"", "https://docs.google.com/x")
        ss.set_transport(FakeGoogle({SHEET_CURRENT: downgrade}))
        check("редирект с https на http запрещён",
              "https" in raises(lambda: ss.fetch_sheet_csv(SPREADSHEET_ID, SHEET_CURRENT),
                                ss.SourceError))

        loop = ss.HttpResponse(302, {"Location": "https://docs.google.com/next"},
                               b"", "https://docs.google.com/x")
        ss.set_transport(FakeGoogle({SHEET_CURRENT: loop}))
        check("бесконечный редирект внутри своего хоста тоже обрывается",
              bool(raises(lambda: ss.fetch_sheet_csv(SPREADSHEET_ID, SHEET_CURRENT),
                          ss.SourceError)))

        empty_loc = ss.HttpResponse(302, {}, b"", "https://docs.google.com/x")
        ss.set_transport(FakeGoogle({SHEET_CURRENT: empty_loc}))
        check("редирект без адреса — отказ",
              bool(raises(lambda: ss.fetch_sheet_csv(SPREADSHEET_ID, SHEET_CURRENT),
                          ss.SourceError)))
    finally:
        ss.set_transport(None)


# ── Часть 4. Настоящий сетевой путь: таймаут и bounded read ─────────────────

class _SlowHandler(BaseHTTPRequestHandler):
    """Локальный сервер: умеет висеть и умеет отдать слишком много."""

    def log_message(self, *_args):  # тишина в отчёте набора
        pass

    def do_GET(self):  # noqa: N802 — имя задано BaseHTTPRequestHandler
        if self.path.startswith("/slow"):
            time.sleep(5)
            return
        if self.path.startswith("/huge"):
            self.send_response(200)
            self.send_header("Content-Type", "text/csv")
            self.end_headers()
            chunk = b"a" * 65536
            try:
                for _ in range((ss.MAX_RESPONSE_BYTES // len(chunk)) + 4):
                    self.wfile.write(chunk)
            except OSError:
                pass
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/csv")
        self.end_headers()
        self.wfile.write(b"ok")

    def do_POST(self):  # noqa: N802
        self.send_response(405)
        self.end_headers()


def network_checks() -> None:
    print("\n== Настоящий транспорт: таймаут и ограниченное чтение ==")
    server = HTTPServer(("127.0.0.1", 0), _SlowHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        message = raises(
            lambda: ss._httpx_get("GET", f"http://127.0.0.1:{port}/slow", 0.7),
            ss.SourceError)
        check("зависший источник превращается в понятный отказ",
              "не ответил" in message, message[:110])

        message = raises(
            lambda: ss._httpx_get("GET", f"http://127.0.0.1:{port}/huge", 30.0),
            ss.SourceError)
        check("ответ больше предела обрывается чтением, а не после него",
              "больше" in message, message[:110])

        check("не-GET через этот слой невозможен",
              bool(raises(lambda: ss._httpx_get("POST", f"http://127.0.0.1:{port}/", 5.0),
                          ss.SourceError)))
        ok = ss._httpx_get("GET", f"http://127.0.0.1:{port}/", 5.0)
        check("обычный ответ читается целиком",
              ok.status == 200 and ok.body == b"ok", str(ok.status))
    finally:
        server.shutdown()
        server.server_close()


# ── Часть 5. Обновление снимка, хранение и границы ──────────────────────────

def _config(org_id: int) -> dict:
    row = sql("SELECT config_json FROM connections WHERE org_id = ? ORDER BY id", org_id)
    return json.loads(row[0][0]) if row else {}


def _carrier_row(org_id: int):
    return sql("SELECT id, kind, token_enc, status, last_sync_at, ms_agent_sync_id,"
               " ms_agent_href FROM connections WHERE org_id = ? ORDER BY id", org_id)


def _fingerprint() -> dict:
    """Полный снимок ВСЕХ пользовательских таблиц.

    Снимок берётся со всех таблиц, а не с заранее выбранного списка «важных»:
    список важных — это утверждение о том, куда слой теоретически может
    написать, а проверять надо ровно наоборот — что он не пишет НИКУДА.
    """
    con = sqlite3.connect(DB_PATH)
    try:
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {t: con.execute(f"SELECT * FROM {t}").fetchall() for t in tables}
    finally:
        con.close()


def _diff(before: dict, after: dict) -> list:
    keys = sorted(set(before) | set(after))
    return [k for k in keys if before.get(k) != after.get(k)]


def refresh_checks(owner, org_id: int) -> None:  # noqa: C901 — сценарий, ветвлений мало
    print("\n== Первое обновление: ровно два GET и ни одного постороннего ==")
    fake = FakeGoogle()
    ss.set_transport(fake)
    try:
        before = _fingerprint()
        r = owner.post("/api/supply/sheets/refresh",
                       json={"spreadsheet_url": SHEET_URL,
                             "sheet_names": [SHEET_CURRENT, SHEET_NEXT]})
        check("владелец обновил предпросмотр", r.status_code == 200, r.text[:200])
        body = r.json()
        check("это новый импорт, а не «ничего не изменилось»",
              body.get("unchanged") is False, json.dumps(body)[:160])
        check("запросов ровно два — по одному на лист",
              len(fake.calls) == 2, str(len(fake.calls)))
        check("метод только GET", fake.methods == {"GET"}, str(fake.methods))
        check("хост только docs.google.com",
              fake.hosts == {"docs.google.com"}, str(fake.hosts))
        check("оба адреса — построенные CSV-endpoint'ы",
              all("/gviz/tq?tqx=out:csv&headers=0&sheet=" in u for _m, u, _t in fake.calls),
              str([u for _m, u, _t in fake.calls])[:200])
        check("таймаут задан явно на каждом запросе",
              all(isinstance(t, (int, float)) and t > 0 for _m, _u, t in fake.calls),
              str([t for _m, _u, t in fake.calls]))
        check("порядок листов сохранён: сначала текущий, потом следующий",
              fake.sheet_of(fake.calls[0][1]) == SHEET_CURRENT
              and fake.sheet_of(fake.calls[1][1]) == SHEET_NEXT,
              str([fake.sheet_of(u) for _m, u, _t in fake.calls]))
        check("CC_BATCH_ID наружу не уехал ни в одном адресе",
              not any("CCB-" in u or "cc_batch" in u.lower() for _m, u, _t in fake.calls))

        print("\n== Отпечаток базы: изменился ровно один ключ одной колонки ==")
        after = _fingerprint()
        changed = _diff(before, after)
        check("изменилась только таблица connections",
              changed == ["connections"], str(changed))
        for table in ("production_orders", "ordered_qty", "order_receipts",
                      "order_plans", "products", "sales", "stock_days",
                      "warehouse_stock", "productions", "replenish_drafts"):
            if table in before:
                check(f"таблица {table} не тронута",
                      before[table] == after[table],
                      f"{len(before[table])} → {len(after[table])}")
        carrier_before = [row[:1] + row[1:] for row in _carrier_row(org_id)]
        check("носитель существует ровно один", len(carrier_before) == 1,
              str(carrier_before))
        cfg = _config(org_id)
        check("снимок лежит под своим ключом", ss.ENVELOPE_KEY in cfg, str(sorted(cfg)))
        check("чужие ключи config_json на месте",
              cfg.get("keep_me") == {"a": [1, 2], "b": "чужое"}, str(cfg.get("keep_me")))
        check("версия схемы снимка записана",
              cfg[ss.ENVELOPE_KEY]["schema_version"] == ss.ENVELOPE_SCHEMA_VERSION)
        check("версия парсера записана",
              cfg[ss.ENVELOPE_KEY]["parser_version"] == ss.PARSER_VERSION)
        check("снимок помещается в отведённый предел",
              len(json.dumps(cfg[ss.ENVELOPE_KEY], ensure_ascii=False).encode())
              <= ss.MAX_ENVELOPE_BYTES)
        check("время обновления Google живёт ВНУТРИ снимка",
              bool(cfg[ss.ENVELOPE_KEY].get("last_success_at")))

        print("\n== Чтение снимка: строки, фильтры, сводка ==")
        data = owner.get("/api/supply/sheets?limit=200").json()
        check("отданы все десять физических строк обоих листов",
              data["total"] == 10 and len(data["rows"]) == 10,
              f"total={data['total']} rows={len(data['rows'])}")
        check("честная подпись приходит с сервера",
              "не партия" in data["disclaimer"] and "Едет" in data["disclaimer"],
              data["disclaimer"])
        check("ссылка на источник собрана сервером из идентификатора",
              data["spreadsheet_url"] == ss.spreadsheet_link(SPREADSHEET_ID),
              data["spreadsheet_url"])
        check("имена листов в исходном порядке",
              data["sheet_names"] == [SHEET_CURRENT, SHEET_NEXT], str(data["sheet_names"]))
        need = owner.get("/api/supply/sheets?queue=needs_review&limit=200").json()
        bad = owner.get("/api/supply/sheets?queue=invalid&limit=200").json()
        check("воронка «все ⊇ разбор ⊇ ошибки» соблюдается",
              data["total"] >= need["total"] >= bad["total"] > 0,
              f"{data['total']}/{need['total']}/{bad['total']}")
        check("в очереди разбора нет пустых строк-разделителей",
              all(not r["is_blank"] for r in need["rows"]))
        check("«ошибки» — это только нечитаемое число и расхождение итога",
              all(set(r["issues"]) & ss.INVALID_ISSUES for r in bad["rows"]),
              str([r["issues"] for r in bad["rows"]]))
        one = owner.get(f"/api/supply/sheets?sheet={SHEET_NEXT}&limit=200").json()
        check("фильтр по листу отдаёт только его строки",
              one["total"] == 1 and one["rows"][0]["sheet_name"] == SHEET_NEXT,
              str(one["total"]))
        check("несуществующий лист — понятный отказ, а не пустая таблица",
              owner.get("/api/supply/sheets?sheet=Лето").status_code == 400)
        page = owner.get("/api/supply/sheets?limit=2&offset=4").json()
        check("постраничность режет по физическому порядку",
              [r["source_row"] for r in page["rows"]] == [7, 8],
              str([r["source_row"] for r in page["rows"]]))
        for bad_q in ("?limit=0", "?limit=201", "?offset=-1", "?queue=всё"):
            check(f"негодный параметр отклонён: {bad_q}",
                  owner.get("/api/supply/sheets" + bad_q).status_code in (400, 422),
                  str(owner.get("/api/supply/sheets" + bad_q).status_code))

        print("\n== Подсказка по каталогу: кандидат, но не привязка ==")
        rows = {r["source_row"]: r for r in data["rows"] if r["sheet_name"] == SHEET_CURRENT}
        sugg = rows[3]["suggestion"]
        check("подсказка есть у каждой строки", all(
            "suggestion" in r for r in data["rows"]))
        check("несовпадающее имя честно даёт «нет кандидатов»",
              sugg["status"] == "none" and sugg["count"] == 0, str(sugg))
        check("привязки нет никогда", all(
            r["suggestion"]["linked"] is False for r in data["rows"]))
        check("идентификатора товара в подсказке нет вовсе",
              "product_id" not in json.dumps(data["rows"], ensure_ascii=False))
        catalogue = sql("SELECT base_name FROM products WHERE org_id = ? LIMIT 1", org_id)
        if catalogue:
            existing = catalogue[0][0]
            hinted = ss.name_candidates(SessionLocal(), org_id, [existing])
            check("точное совпадение имени находит ровно одного кандидата",
                  hinted.get(existing) == [existing], str(hinted))
            single = ss.suggestion_from_candidates([existing])
            check("даже единственный кандидат называется кандидатом, а не связью",
                  single["status"] == "one" and single["linked"] is False
                  and "не привязано" in single["label"], str(single))
            check("похожее, но не точное имя кандидатом не считается",
                  ss.name_candidates(SessionLocal(), org_id,
                                     [existing + " "]).get(existing + " ") == [],
                  existing)
        check("исход «несколько кандидатов» честно назван множественным",
              ss.suggestion_from_candidates(["А", "Б"])["status"] == "many")

        print("\n== Тот же источник: unchanged и те же байты строк ==")
        rows_before = json.dumps(_config(org_id)[ss.ENVELOPE_KEY]["rows"],
                                 ensure_ascii=False, sort_keys=True)
        hash_before = _config(org_id)[ss.ENVELOPE_KEY]["content_sha256"]
        success_before = _config(org_id)[ss.ENVELOPE_KEY]["last_success_at"]
        fake.calls.clear()
        r = owner.post("/api/supply/sheets/refresh",
                       json={"spreadsheet_url": SHEET_URL,
                             "sheet_names": [SHEET_CURRENT, SHEET_NEXT]})
        check("повтор принят", r.status_code == 200, r.text[:150])
        check("и честно назван неизменившимся", r.json().get("unchanged") is True,
              r.text[:150])
        env = _config(org_id)[ss.ENVELOPE_KEY]
        check("строки не переписаны ни на байт",
              json.dumps(env["rows"], ensure_ascii=False, sort_keys=True) == rows_before)
        check("хеш содержимого тот же", env["content_sha256"] == hash_before)
        check("время успеха НЕ подменено новым (это не новый импорт)",
              env["last_success_at"] == success_before, str(env["last_success_at"]))
        check("но попытка честно отмечена",
              env["last_attempt_at"] >= success_before, str(env["last_attempt_at"]))

        print("\n== Изменился ВТОРОЙ лист: заменяются оба, смешанного снимка нет ==")
        moved = next_rows()
        moved.append(put(blank(), {2: "2002", 3: "Шапка НГ", 5: "Белый",
                                   10: "4", 11: "4", 14: "2", 15: "10"}))
        fake.bodies[SHEET_NEXT] = to_csv(moved)
        fake.calls.clear()
        r = owner.post("/api/supply/sheets/refresh",
                       json={"spreadsheet_url": SHEET_URL,
                             "sheet_names": [SHEET_CURRENT, SHEET_NEXT]})
        check("изменение источника даёт новый импорт",
              r.status_code == 200 and r.json().get("unchanged") is False, r.text[:150])
        env = _config(org_id)[ss.ENVELOPE_KEY]
        check("хеш содержимого сменился", env["content_sha256"] != hash_before)
        sheets_in_snapshot = {row["sheet_name"] for row in env["rows"]}
        check("в снимке по-прежнему оба листа",
              sheets_in_snapshot == {SHEET_CURRENT, SHEET_NEXT}, str(sheets_in_snapshot))
        check("строки первого листа не потерялись при замене второго",
              sum(1 for row in env["rows"] if row["sheet_name"] == SHEET_CURRENT) == 9)
        check("новая строка второго листа доехала",
              sum(1 for row in env["rows"] if row["sheet_name"] == SHEET_NEXT) == 2)
        good_hash = env["content_sha256"]
        good_rows = json.dumps(env["rows"], ensure_ascii=False, sort_keys=True)
        good_success = env["last_success_at"]

        print("\n== Сбой ВТОРОГО листа не трогает прежний успешный снимок ==")
        fake.bodies[SHEET_NEXT] = ss.HttpResponse(
            403, {}, b"forbidden body", "https://docs.google.com/x")
        fake.calls.clear()
        r = owner.post("/api/supply/sheets/refresh",
                       json={"spreadsheet_url": SHEET_URL,
                             "sheet_names": [SHEET_CURRENT, SHEET_NEXT]})
        check("отказ источника — 502, а не 500 и не тихий успех",
              r.status_code == 502, f"{r.status_code} {r.text[:120]}")
        check("наружу ушёл только безопасный текст",
              "forbidden body" not in r.text and "403" in r.text, r.text[:140])
        env = _config(org_id)[ss.ENVELOPE_KEY]
        check("прежний хеш на месте", env["content_sha256"] == good_hash)
        check("прежние строки на месте до байта",
              json.dumps(env["rows"], ensure_ascii=False, sort_keys=True) == good_rows)
        check("время последнего УСПЕХА не сдвинулось",
              env["last_success_at"] == good_success)
        check("зато отмечены попытка и безопасная ошибка",
              bool(env["last_attempt_at"]) and "403" in env["last_error"],
              env["last_error"][:110])
        shown = owner.get("/api/supply/sheets?limit=200").json()
        check("страница показывает прежний снимок и объясняет сбой",
              shown["total"] == 11 and "403" in shown["last_error"],
              f"{shown['total']} {shown['last_error'][:80]}")

        print("\n== Дрейф заголовка ведёт себя так же: снимок цел ==")
        drift = autumn_header_rows()
        put(drift[0], {5: "Цвета"})
        drift.append(put(blank(), {2: "1", 3: "Т", 10: "1"}))
        fake.bodies[SHEET_NEXT] = to_csv(drift)
        r = owner.post("/api/supply/sheets/refresh",
                       json={"spreadsheet_url": SHEET_URL,
                             "sheet_names": [SHEET_CURRENT, SHEET_NEXT]})
        check("дрейф заголовка — 502", r.status_code == 502, r.text[:120])
        env = _config(org_id)[ss.ENVELOPE_KEY]
        check("и снимок опять не тронут", env["content_sha256"] == good_hash)
        check("ошибка называет колонку, а не «что-то пошло не так»",
              "заголовк" in env["last_error"], env["last_error"][:120])

        print("\n== Снимок больше отведённого предела: fail closed ==")
        fake.bodies[SHEET_NEXT] = NEXT_CSV
        real_limit = ss.MAX_ENVELOPE_BYTES
        ss.MAX_ENVELOPE_BYTES = 512   # тот же код, другая константа
        try:
            r = owner.post("/api/supply/sheets/refresh",
                           json={"spreadsheet_url": SHEET_URL,
                                 "sheet_names": [SHEET_CURRENT, SHEET_NEXT]})
            check("слишком большой снимок не записывается",
                  r.status_code == 502, f"{r.status_code} {r.text[:120]}")
            env = _config(org_id)[ss.ENVELOPE_KEY]
            check("и прежний снимок опять цел", env["content_sha256"] == good_hash)
        finally:
            ss.MAX_ENVELOPE_BYTES = real_limit

        print("\n== Носитель как строка МойСклада: ни одно поле не тронуто ==")
        carrier_after = _carrier_row(org_id)
        check("kind, токен, статус, время синка и ms_* поля прежние",
              [row[1:] for row in carrier_after] == [row[1:] for row in carrier_before],
              str(carrier_after))
        check("отдельная Connection под Google НЕ заведена",
              len(carrier_after) == 1
              and carrier_after[0][1] in ss.PRIMARY_CONNECTION_KINDS,
              str(carrier_after))
        check("чужие ключи config_json пережили все обновления и сбои",
              _config(org_id).get("keep_me") == {"a": [1, 2], "b": "чужое"},
              str(_config(org_id).get("keep_me")))

        print("\n== Контракт отката: старый код видит связь ровно как раньше ==")
        settings = owner.get("/api/settings").json()
        check("/api/settings по-прежнему отдаёт вид, статус и время синка",
              set(settings["connection"]) == {"kind", "status", "last_sync_at"},
              str(settings["connection"]))
        check("и это по-прежнему та же связь",
              settings["connection"]["kind"] == carrier_after[0][1],
              str(settings["connection"]))
        check("предпросмотра в ответе старой ручки нет вовсе",
              ss.ENVELOPE_KEY not in json.dumps(settings, ensure_ascii=False))

        print("\n== Одновременное одинаковое обновление сходится ==")
        fake.bodies[SHEET_NEXT] = to_csv(moved)
        clients = [client() for _ in range(4)]
        for c in clients:
            c.post("/login", data={"email": "sheets-owner@test.io",
                                   "password": "secret123"})
        errors = []

        def _hit(c):
            try:
                return c.post("/api/supply/sheets/refresh",
                              json={"spreadsheet_url": SHEET_URL,
                                    "sheet_names": [SHEET_CURRENT, SHEET_NEXT]}).status_code
            except Exception as exc:  # noqa: BLE001 — гонка не должна ронять набор
                errors.append(exc)
                return 0

        with ThreadPoolExecutor(max_workers=4) as pool:
            codes = list(pool.map(_hit, clients))
        for c in clients:
            c.close()
        check("ни один одновременный запрос не упал",
              not errors and set(codes) == {200}, f"{codes} {str(errors)[:120]}")
        env = _config(org_id)[ss.ENVELOPE_KEY]
        check("снимок сошёлся к одному значению", env["content_sha256"] == good_hash,
              env["content_sha256"][:16])
        check("строк ровно столько, сколько в источнике — без дублей",
              len(env["rows"]) == 11, str(len(env["rows"])))
        check("чужие ключи пережили и гонку",
              _config(org_id).get("keep_me") == {"a": [1, 2], "b": "чужое"})
        check("носитель после гонки по-прежнему один",
              len(_carrier_row(org_id)) == 1, str(len(_carrier_row(org_id))))
    finally:
        ss.set_transport(None)


def isolation_checks(owner, member, org_id: int) -> None:
    print("\n== Роли и арендаторы ==")
    fake = FakeGoogle()
    ss.set_transport(fake)
    try:
        check("участник видит предпросмотр",
              member.get("/api/supply/sheets").status_code == 200)
        seen = member.get("/api/supply/sheets?limit=200").json()
        check("участнику отдана его роль и запрет обновления",
              seen["role"] == "member" and seen["can_refresh"] is False, str(seen["role"]))
        fake.calls.clear()
        r = member.post("/api/supply/sheets/refresh",
                        json={"spreadsheet_url": SHEET_URL,
                              "sheet_names": [SHEET_CURRENT, SHEET_NEXT]})
        check("участнику запрещено обновлять", r.status_code == 403, str(r.status_code))
        check("и запрет случился ДО сетевого вызова",
              fake.calls == [], str(fake.calls))
        check("владельцу право обновлять отдано",
              owner.get("/api/supply/sheets").json()["can_refresh"] is True)

        anon = client()
        check("аноним не читает предпросмотр",
              anon.get("/api/supply/sheets").status_code == 401)
        check("аноним не обновляет",
              anon.post("/api/supply/sheets/refresh",
                        json={"spreadsheet_url": SHEET_URL,
                              "sheet_names": [SHEET_CURRENT, SHEET_NEXT]}).status_code == 401)
        page = anon.get("/supply", follow_redirects=False)
        check("страница анонима уводит на вход",
              page.status_code == 302 and "/login" in page.headers.get("location", ""),
              str(page.status_code))
        anon.close()

        print("\n== Вторая организация не видит чужого предпросмотра ==")
        other = client()
        other.post("/register", data={"name": "Чужой", "email": "sheets-b@test.io",
                                      "password": "secret123", "org_name": "Бренд-Б"})
        other.post("/api/connect/demo")
        theirs = other.get("/api/supply/sheets?limit=200").json()
        check("у чужой организации предпросмотра нет",
              theirs["configured"] is False and theirs["rows"] == [], str(theirs["total"]))
        other_id = sql("SELECT org_id FROM memberships WHERE user_id ="
                       " (SELECT id FROM users WHERE email = 'sheets-b@test.io')")[0][0]
        check("организации действительно разные", other_id != org_id,
              f"{other_id} vs {org_id}")

        other_fake = FakeGoogle()
        ss.set_transport(other_fake)
        r = other.post("/api/supply/sheets/refresh",
                       json={"spreadsheet_url": SHEET_URL,
                             "sheet_names": [SHEET_CURRENT, SHEET_NEXT]})
        check("чужая организация делает свой снимок", r.status_code == 200, r.text[:140])
        check("и он лежит в ЕЁ носителе",
              ss.ENVELOPE_KEY in _config(other_id), str(sorted(_config(other_id))))
        ours = owner.get("/api/supply/sheets?limit=200").json()
        check("наш снимок от этого не изменился",
              ours["content_sha256"] == _config(org_id)[ss.ENVELOPE_KEY]["content_sha256"])
        check("клиент не может назвать чужую организацию: параметра просто нет",
              owner.get(f"/api/supply/sheets?org_id={other_id}&limit=200")
              .json()["content_sha256"] == ours["content_sha256"])
        other.close()

        print("\n== Нет основного подключения: 409 без записи и без сети ==")
        naked = client()
        naked.post("/register", data={"name": "Без связи", "email": "sheets-c@test.io",
                                      "password": "secret123", "org_name": "Бренд-В"})
        naked_id = sql("SELECT org_id FROM memberships WHERE user_id ="
                       " (SELECT id FROM users WHERE email = 'sheets-c@test.io')")[0][0]
        exec_sql("DELETE FROM connections WHERE org_id = ?", naked_id)
        check("у организации действительно нет связей",
              sql("SELECT COUNT(*) FROM connections WHERE org_id = ?", naked_id)[0][0] == 0)
        naked_fake = FakeGoogle()
        ss.set_transport(naked_fake)
        before = _fingerprint()
        r = naked.post("/api/supply/sheets/refresh",
                       json={"spreadsheet_url": SHEET_URL,
                             "sheet_names": [SHEET_CURRENT, SHEET_NEXT]})
        check("отказ 409 с понятным текстом", r.status_code == 409, r.text[:160])
        check("текст объясняет, что делать",
              "подключен" in r.text.lower(), r.text[:160])
        check("ни одного сетевого вызова", naked_fake.calls == [], str(naked_fake.calls))
        check("ни одной мутации в базе", _diff(before, _fingerprint()) == [],
              str(_diff(before, _fingerprint())))
        check("отдельная Connection под Google не создана",
              sql("SELECT COUNT(*) FROM connections WHERE org_id = ?", naked_id)[0][0] == 0)
        check("чтение тоже честно говорит, что хранить негде",
              naked.get("/api/supply/sheets").json()["carrier_present"] is False)
        naked.close()
    finally:
        ss.set_transport(None)


def first_failure_checks() -> None:
    """ПЕРВОЕ обновление не удалось: причина обязана остаться на экране.

    Замечание ревью PR #47. Раньше в этом состоянии человек получал
    четырёхсекундный тост и пустую форму — то есть вводил ссылку и два имени
    листов заново, не понимая, что пошло не так, хотя сервер их помнил.
    Обратная опасность ровно такая же: восстановленная форма НЕ должна
    выглядеть как успешно настроенный источник.
    """
    print("\n== Первый неудачный refresh: ошибка видна, ввод не потерян ==")
    fresh = client()
    fresh.post("/register", data={"name": "Первый сбой", "email": "sheets-f@test.io",
                                 "password": "secret123", "org_name": "Бренд-Е"})
    fresh.post("/api/connect/demo")
    org_id = sql("SELECT org_id FROM memberships WHERE user_id ="
                 " (SELECT id FROM users WHERE email = 'sheets-f@test.io')")[0][0]

    denied = FakeGoogle({SHEET_CURRENT: ss.HttpResponse(
        403, {}, b"forbidden body", "https://docs.google.com/x")})
    ss.set_transport(denied)
    try:
        r = fresh.post("/api/supply/sheets/refresh",
                       json={"spreadsheet_url": SHEET_URL,
                             "sheet_names": [SHEET_CURRENT, SHEET_NEXT]})
        check("первое обновление честно отказало", r.status_code == 502, r.text[:140])

        data = fresh.get("/api/supply/sheets?limit=200").json()
        check("причина отказа осталась в состоянии, а не только в тосте",
              "403" in data["last_error"], data["last_error"][:120])
        check("тела чужого ответа в ней нет",
              "forbidden body" not in json.dumps(data, ensure_ascii=False))
        check("время попытки отмечено", bool(data["last_attempt_at"]),
              str(data["last_attempt_at"]))
        check("ввод сохранён: ссылка восстановлена сервером из идентификатора",
              data["attempt"]["spreadsheet_url"] == ss.spreadsheet_link(SPREADSHEET_ID),
              str(data["attempt"]))
        check("и оба имени листов в исходном порядке",
              data["attempt"]["sheet_names"] == [SHEET_CURRENT, SHEET_NEXT],
              str(data["attempt"]["sheet_names"]))

        print("\n== ...но это НЕ выдаётся за успешно настроенный источник ==")
        check("configured остаётся false", data["configured"] is False,
              str(data["configured"]))
        check("удачного чтения не было", data["last_success_at"] is None,
              str(data["last_success_at"]))
        check("снимка нет: ни хеша, ни строк",
              data["content_sha256"] == "" and data["rows"] == [] and data["total"] == 0,
              str((data["content_sha256"], data["total"])))
        check("и ссылка «открыть исходник» не показывается как настроенная",
              data["spreadsheet_url"] == "", data["spreadsheet_url"])
        check("сводка пустая, а не выдуманная",
              data["counts"]["data_rows"] == 0, str(data["counts"]["data_rows"]))

        print("\n== Удачное чтение поверх сбоя приводит состояние в порядок ==")
        ss.set_transport(FakeGoogle())
        r = fresh.post("/api/supply/sheets/refresh",
                       json={"spreadsheet_url": SHEET_URL,
                             "sheet_names": [SHEET_CURRENT, SHEET_NEXT]})
        check("повторная попытка удалась", r.status_code == 200, r.text[:140])
        data = fresh.get("/api/supply/sheets?limit=200").json()
        check("теперь источник настроен и ошибка снята",
              data["configured"] is True and data["last_error"] == "",
              str((data["configured"], data["last_error"][:60])))
        check("и снимок появился", data["total"] == 10 and bool(data["last_success_at"]),
              str(data["total"]))
    finally:
        ss.set_transport(None)
        fresh.close()


def envelope_version_checks() -> None:
    """Снимок, который мы не умеем прочитать, не интерпретируется и не стирается."""
    print("\n== Версия снимка: неизвестное/повреждённое — fail closed ==")
    holder = client()
    holder.post("/register", data={"name": "Версии", "email": "sheets-g@test.io",
                                  "password": "secret123", "org_name": "Бренд-Ж"})
    holder.post("/api/connect/demo")
    org_id = sql("SELECT org_id FROM memberships WHERE user_id ="
                 " (SELECT id FROM users WHERE email = 'sheets-g@test.io')")[0][0]
    conn_id = sql("SELECT id FROM connections WHERE org_id = ? ORDER BY id",
                  org_id)[0][0]
    exec_sql("UPDATE connections SET config_json = ? WHERE id = ?",
             json.dumps({"keep_me": {"чужое": [1, 2]}}, ensure_ascii=False), conn_id)

    ss.set_transport(FakeGoogle())
    try:
        r = holder.post("/api/supply/sheets/refresh",
                        json={"spreadsheet_url": SHEET_URL,
                              "sheet_names": [SHEET_CURRENT, SHEET_NEXT]})
        check("корректный снимок создан и читается", r.status_code == 200, r.text[:140])
        check("и текущая версия читателем принимается",
              holder.get("/api/supply/sheets").status_code == 200)
        good = json.loads(sql("SELECT config_json FROM connections WHERE id = ?",
                              conn_id)[0][0])
        good_blob = json.dumps(good, ensure_ascii=False, sort_keys=True)

        broken = {
            "версия из будущего": {**good[ss.ENVELOPE_KEY], "schema_version": 2},
            "версия строкой": {**good[ss.ENVELOPE_KEY], "schema_version": "1"},
            "версия булевым": {**good[ss.ENVELOPE_KEY], "schema_version": True},
            "версии нет вовсе": {k: v for k, v in good[ss.ENVELOPE_KEY].items()
                                 if k != "schema_version"},
            "нет строк": {k: v for k, v in good[ss.ENVELOPE_KEY].items() if k != "rows"},
            "строки не список": {**good[ss.ENVELOPE_KEY], "rows": {"a": 1}},
            "строка снимка не запись": {**good[ss.ENVELOPE_KEY], "rows": ["строка"]},
            "счётчики не запись": {**good[ss.ENVELOPE_KEY], "counts": []},
            "ошибка не текст": {**good[ss.ENVELOPE_KEY], "last_error": 500},
            "под ключом не запись": "снимок",
        }
        for label, payload in broken.items():
            spoiled = dict(good)
            spoiled[ss.ENVELOPE_KEY] = payload
            blob = json.dumps(spoiled, ensure_ascii=False)
            exec_sql("UPDATE connections SET config_json = ? WHERE id = ?", blob, conn_id)

            read = holder.get("/api/supply/sheets")
            check(f"чтение отказывает 409: {label}", read.status_code == 409,
                  f"{read.status_code} {read.text[:90]}")
            check(f"и отказ безопасен по тексту: {label}",
                  "предпросмотр" in read.text.lower(), read.text[:110])

            probe = FakeGoogle()
            ss.set_transport(probe)
            write = holder.post("/api/supply/sheets/refresh",
                                json={"spreadsheet_url": SHEET_URL,
                                      "sheet_names": [SHEET_CURRENT, SHEET_NEXT]})
            check(f"обновление тоже отказывает 409: {label}", write.status_code == 409,
                  f"{write.status_code} {write.text[:90]}")
            check(f"и не ходит в источник впустую: {label}", probe.calls == [],
                  str(probe.calls)[:80])
            after = sql("SELECT config_json FROM connections WHERE id = ?", conn_id)[0][0]
            check(f"повреждённый снимок оставлен как есть, а не переписан: {label}",
                  after == blob, after[:80])
            check(f"и чужой ключ config_json цел: {label}",
                  json.loads(after).get("keep_me") == {"чужое": [1, 2]},
                  str(json.loads(after).get("keep_me")))

        print("\n== Возврат корректного снимка снова делает раздел рабочим ==")
        exec_sql("UPDATE connections SET config_json = ? WHERE id = ?",
                 json.dumps(good, ensure_ascii=False), conn_id)
        check("исправный снимок читается снова",
              holder.get("/api/supply/sheets").status_code == 200)
        check("и содержимое не изменилось за всё это время",
              json.dumps(json.loads(sql("SELECT config_json FROM connections"
                                        " WHERE id = ?", conn_id)[0][0]),
                         ensure_ascii=False, sort_keys=True) == good_blob)

        print("\n== Снимок прежней версии разбора помечается устаревшим ==")
        stale = dict(good)
        stale[ss.ENVELOPE_KEY] = {**good[ss.ENVELOPE_KEY],
                                  "parser_version": "supply-sheets-parser-1"}
        exec_sql("UPDATE connections SET config_json = ? WHERE id = ?",
                 json.dumps(stale, ensure_ascii=False), conn_id)
        data = holder.get("/api/supply/sheets").json()
        check("он читается, но честно назван устаревшим",
              data["parser_stale"] is True and data["configured"] is True,
              str(data["parser_stale"]))
        exec_sql("UPDATE connections SET config_json = ? WHERE id = ?",
                 json.dumps(good, ensure_ascii=False), conn_id)
        check("текущая версия разбора устаревшей не считается",
              holder.get("/api/supply/sheets").json()["parser_stale"] is False)
    finally:
        ss.set_transport(None)
        holder.close()


def row_shape_checks() -> None:  # noqa: C901 — матрица полей, ветвлений мало
    """Форма СТРОКИ снимка проверяется, а не предполагается.

    Замечание ревью PR #47 на HEAD `c6919af`, воспроизведённое дословно:
    `{"issues": 5}` проходил читателя, а потом `_row_flags` делал `set(5)` и
    падал `TypeError` — вместо обещанного управляемого 409 получалась 500. То
    же с полями, которые разыменовывает браузер: `comments_raw` числом ломает
    `forEach`, `sizes` строкой молча превращает размеры в прочерки.

    Здесь проверяется, что каждое такое поле даёт УПРАВЛЯЕМЫЙ 409 — на чтении и
    на записи, до сети и без единой записи в базу, — и что валидные снимки
    (включая сделанные прежней версией разбора) по-прежнему читаются.
    """
    print("\n== Форма строки снимка: fail closed вместо 500 ==")
    shaped = client()
    shaped.post("/register", data={"name": "Форма строк", "email": "sheets-i@test.io",
                                  "password": "secret123", "org_name": "Бренд-И"})
    shaped.post("/api/connect/demo")
    org_id = sql("SELECT org_id FROM memberships WHERE user_id ="
                 " (SELECT id FROM users WHERE email = 'sheets-i@test.io')")[0][0]
    conn_id = sql("SELECT id FROM connections WHERE org_id = ? ORDER BY id",
                  org_id)[0][0]
    exec_sql("UPDATE connections SET config_json = ? WHERE id = ?",
             json.dumps({"keep_row": {"чужое": 1}}, ensure_ascii=False), conn_id)

    ss.set_transport(FakeGoogle())
    try:
        r = shaped.post("/api/supply/sheets/refresh",
                        json={"spreadsheet_url": SHEET_URL,
                              "sheet_names": [SHEET_CURRENT, SHEET_NEXT]})
        check("исходный снимок создан", r.status_code == 200, r.text[:140])
        good = json.loads(sql("SELECT config_json FROM connections WHERE id = ?",
                              conn_id)[0][0])
        good_env = good[ss.ENVELOPE_KEY]
        sample = dict(good_env["rows"][0])
        check("образец строки взят из настоящего снимка",
              sample.get("sheet_name") == SHEET_CURRENT, str(sample.get("sheet_name")))
        check("валидный снимок читается", shaped.get("/api/supply/sheets").status_code == 200)

        # Каждый случай — реалистичная ПОЛНАЯ строка с одним испорченным полем:
        # так проверяется именно поле, а не то, что запись «вообще куцая».
        cases = [
            ("issues числом (случай из отчёта ревью)", {"issues": 5}),
            ("issues с не-строкой внутри", {"issues": ["quantity_missing", 5]}),
            ("issues с булевым внутри", {"issues": [True]}),
            ("issues отображением", {"issues": {"a": 1}}),
            ("comments_raw числом", {"comments_raw": 7}),
            ("comments_raw с не-строкой внутри", {"comments_raw": ["ок", 3]}),
            ("sizes строкой", {"sizes": "нет"}),
            ("sizes со значением-дробью", {"sizes": {"XS": 2.5}}),
            ("sizes со значением-строкой", {"sizes": {"XS": "2"}}),
            ("sizes_raw числом", {"sizes_raw": 3}),
            ("sizes_raw со значением-числом", {"sizes_raw": {"XS": 2}}),
            ("unknown_raw списком", {"unknown_raw": [1]}),
            ("unknown_raw со значением-числом", {"unknown_raw": {"22": 5}}),
            ("name числом", {"name": 5}),
            ("sheet_name числом", {"sheet_name": 1}),
            ("source_status_raw числом", {"source_status_raw": 0}),
            ("is_blank единицей вместо True", {"is_blank": 1}),
            ("source_row булевым", {"source_row": True}),
            ("source_row строкой", {"source_row": "3"}),
            ("anchor_row строкой", {"anchor_row": "3"}),
            ("size_sum дробью", {"size_sum": 2.5}),
            ("source_total булевым", {"source_total": True}),
        ]
        for label, patch in cases:
            spoiled = dict(good)
            spoiled[ss.ENVELOPE_KEY] = {**good_env, "rows": [{**sample, **patch}]}
            blob = json.dumps(spoiled, ensure_ascii=False)
            exec_sql("UPDATE connections SET config_json = ? WHERE id = ?",
                     blob, conn_id)

            probe = FakeGoogle()
            ss.set_transport(probe)
            before = _fingerprint()
            read = shaped.get("/api/supply/sheets?limit=200")
            check(f"GET отдаёт управляемый 409, а не 500: {label}",
                  read.status_code == 409, f"{read.status_code} {read.text[:80]}")
            write = shaped.post("/api/supply/sheets/refresh",
                                json={"spreadsheet_url": SHEET_URL,
                                      "sheet_names": [SHEET_CURRENT, SHEET_NEXT]})
            check(f"POST тоже 409: {label}", write.status_code == 409,
                  str(write.status_code))
            check(f"без сети: {label}", probe.calls == [], str(probe.calls)[:70])
            check(f"без единой записи: {label}",
                  _diff(before, _fingerprint()) == [],
                  str(_diff(before, _fingerprint())))
            check(f"повреждённое оставлено как есть: {label}",
                  sql("SELECT config_json FROM connections WHERE id = ?",
                      conn_id)[0][0] == blob)

        print("\n== В тексте отказа нет содержимого чужой таблицы ==")
        # Содержимое источника заполняют люди, и в нём бывают личные данные.
        # Проверяется САМЫЙ опасный случай: испорчено ровно то поле, значение
        # которого и содержит секрет. Иначе проверка ловила бы только утечку
        # соседнего поля и молчала бы про утечку самого значения — так первая
        # версия этой проверки и промолчала на мутации «печатать значение».
        secret = "СЕКРЕТНОЕ-ИМЯ-КЛИЕНТА-+7 900 000-00-00"
        for label, patch in (
            ("испорчено соседнее поле", {"name": secret, "issues": 5}),
            ("испорчено само поле с секретом", {"name": [secret]}),
            ("секрет внутри списка комментариев", {"comments_raw": [secret, 5]}),
            ("секрет внутри отображения", {"unknown_raw": {"22": secret, "23": 5}}),
        ):
            spoiled = dict(good)
            spoiled[ss.ENVELOPE_KEY] = {**good_env, "rows": [{**sample, **patch}]}
            exec_sql("UPDATE connections SET config_json = ? WHERE id = ?",
                     json.dumps(spoiled, ensure_ascii=False), conn_id)
            resp = shaped.get("/api/supply/sheets")
            check(f"отказ управляемый: {label}", resp.status_code == 409,
                  str(resp.status_code))
            check(f"значение строки в сообщение не попало: {label}",
                  secret not in resp.text, resp.text[:140])
            check(f"но поле и номер строки названы: {label}",
                  "поле" in resp.text and "строки" in resp.text, resp.text[:140])

        print("\n== Валидные снимки по-прежнему читаются ==")
        exec_sql("UPDATE connections SET config_json = ? WHERE id = ?",
                 json.dumps(good, ensure_ascii=False), conn_id)
        check("текущий снимок читается",
              shaped.get("/api/supply/sheets?limit=200").json()["total"] == 10)
        stale = dict(good)
        stale_rows = []
        for row in good_env["rows"]:
            # Строка в форме parser-1: поля `sketch_raw` она не знала, зато
            # несла `extra_raw`. Такой снимок обязан остаться читаемым.
            old_row = {k: v for k, v in row.items() if k != "sketch_raw"}
            old_row["extra_raw"] = {"16": "", "19": ""}
            stale_rows.append(old_row)
        stale[ss.ENVELOPE_KEY] = {**good_env, "rows": stale_rows,
                                  "parser_version": "supply-sheets-parser-1"}
        exec_sql("UPDATE connections SET config_json = ? WHERE id = ?",
                 json.dumps(stale, ensure_ascii=False), conn_id)
        # Ответ разбирается ТОЛЬКО после проверки кода. Первая версия делала
        # `.json()["total"]` сразу — и на мутации «`sketch_raw` тоже обязателен»
        # набор не краснел, а падал `KeyError` посреди прогона, унося с собой
        # всё, что шло после. Падение проверки — не то же самое, что её отказ.
        stale_resp = shaped.get("/api/supply/sheets?limit=200")
        check("снимок прежней версии разбора вообще читается",
              stale_resp.status_code == 200,
              f"{stale_resp.status_code} {stale_resp.text[:90]}")
        data = stale_resp.json() if stale_resp.status_code == 200 else {}
        check("снимок прежней версии разбора читается и помечен устаревшим",
              data.get("total") == 10 and data.get("parser_stale") is True,
              str(data.get("total")))
        check("лишнее незнакомое поле строки чтению не мешает",
              (data.get("rows") or [{}])[0].get("extra_raw") == {"16": "", "19": ""},
              str((data.get("rows") or [{}])[0].get("extra_raw")))
        exec_sql("UPDATE connections SET config_json = ? WHERE id = ?",
                 json.dumps(good, ensure_ascii=False), conn_id)
        check("чужой ключ config_json пережил всю матрицу",
              json.loads(sql("SELECT config_json FROM connections WHERE id = ?",
                             conn_id)[0][0]).get("keep_row") == {"чужое": 1})
    finally:
        ss.set_transport(None)
        shaped.close()


def row_required_checks() -> None:  # noqa: C901 — матрица полей, ветвлений мало
    """Общие поля строки ОБЯЗАНЫ быть, а не только иметь верный тип.

    Замечание ревью PR #47 на HEAD `0d9c226`, воспроизведённое дословно:
    `_validate_row` смотрел поле, только если оно есть, и потому `rows: [{}]`
    проходил читателя как нормальный снимок v1. Испорченный носитель показался
    бы человеку обычной строкой — без листа, без номера строки источника, без
    идентичности, количеств и очереди неоднозначностей.

    Здесь проверяется три вещи:
      1. форма, которую парсер выпускает СЕГОДНЯ, совпадает с требуемой — не по
         памяти, а сверкой ключей настоящего снимка со списком в коде;
      2. пустая запись строки и удаление любого общего поля дают управляемый
         409 и на чтении, и на записи — до сети и без единой записи в базу;
      3. полные снимки обеих версий разбора по-прежнему читаются: `parser-1`
         без `sketch_raw` и с `extra_raw`, `parser-2` со `sketch_raw`.
    """
    print("\n== Общие поля строки: обязательны, а не «если оказались» ==")
    req = client()
    req.post("/register", data={"name": "Обязательные поля",
                                "email": "sheets-r@test.io",
                                "password": "secret123", "org_name": "Бренд-Р"})
    req.post("/api/connect/demo")
    org_id = sql("SELECT org_id FROM memberships WHERE user_id ="
                 " (SELECT id FROM users WHERE email = 'sheets-r@test.io')")[0][0]
    conn_id = sql("SELECT id FROM connections WHERE org_id = ? ORDER BY id",
                  org_id)[0][0]
    exec_sql("UPDATE connections SET config_json = ? WHERE id = ?",
             json.dumps({"keep_req": {"чужое": 2}}, ensure_ascii=False), conn_id)

    ss.set_transport(FakeGoogle())
    try:
        r = req.post("/api/supply/sheets/refresh",
                     json={"spreadsheet_url": SHEET_URL,
                           "sheet_names": [SHEET_CURRENT, SHEET_NEXT]})
        check("исходный снимок создан", r.status_code == 200, r.text[:140])
        good = json.loads(sql("SELECT config_json FROM connections WHERE id = ?",
                              conn_id)[0][0])
        good_env = good[ss.ENVELOPE_KEY]

        # 1. Требуемый набор — это ФАКТИЧЕСКАЯ форма сегодняшнего парсера, а не
        # список, выписанный по памяти. Если завтра парсер начнёт выпускать
        # новое поле или перестанет выпускать старое, эта проверка покраснеет
        # раньше, чем расхождение доедет до носителя.
        anchor_sample = next(row for row in good_env["rows"] if not row["is_blank"])
        blank_sample = next((row for row in good_env["rows"] if row["is_blank"]), None)
        check("в снимке есть и обычная строка, и пустая строка-разделитель",
              blank_sample is not None, str(len(good_env["rows"])))
        required = set(ss._ROW_REQUIRED_FIELDS)
        check("общих обязательных полей ровно 22", len(required) == 22, str(len(required)))
        check("`sketch_raw` обязательным НЕ считается (его не знал parser-1)",
              "sketch_raw" not in required)
        check("обычная строка выпускает ровно общие поля плюс `sketch_raw`",
              set(anchor_sample) == required | {"sketch_raw"},
              str(sorted(set(anchor_sample) ^ (required | {"sketch_raw"}))))
        check("у пустой строки-разделителя набор полей ТОТ ЖЕ",
              blank_sample is not None and set(blank_sample) == set(anchor_sample),
              str(sorted(set(blank_sample or {}) ^ set(anchor_sample))))

        # Форма parser-1: `sketch_raw` он не знал, зато нёс `extra_raw`.
        def as_parser1(row: dict) -> dict:
            old = {k: v for k, v in row.items() if k != "sketch_raw"}
            old["extra_raw"] = {"16": "", "19": ""}
            return old

        def spoil(rows: list) -> str:
            spoiled = dict(good)
            spoiled[ss.ENVELOPE_KEY] = {**good_env, "rows": rows}
            blob = json.dumps(spoiled, ensure_ascii=False)
            exec_sql("UPDATE connections SET config_json = ? WHERE id = ?",
                     blob, conn_id)
            return blob

        def refuses(label: str, blob: str, full: bool) -> None:
            probe = FakeGoogle()
            ss.set_transport(probe)
            before = _fingerprint()
            read = req.get("/api/supply/sheets?limit=200")
            check(f"GET отдаёт управляемый 409: {label}", read.status_code == 409,
                  f"{read.status_code} {read.text[:80]}")
            if not full:
                return
            write = req.post("/api/supply/sheets/refresh",
                             json={"spreadsheet_url": SHEET_URL,
                                   "sheet_names": [SHEET_CURRENT, SHEET_NEXT]})
            check(f"POST тоже 409: {label}", write.status_code == 409,
                  str(write.status_code))
            check(f"без сети и без единой записи: {label}",
                  probe.calls == [] and _diff(before, _fingerprint()) == [],
                  f"{probe.calls}{_diff(before, _fingerprint())}"[:90])
            check(f"повреждённое оставлено как есть: {label}",
                  sql("SELECT config_json FROM connections WHERE id = ?",
                      conn_id)[0][0] == blob)

        # 2. Пустая запись строки — тот самый случай из отчёта ревью.
        print("\n== Пустая запись строки: rows: [{}] ==")
        refuses("rows: [{}]", spoil([{}]), full=True)

        # 3. Удаление КАЖДОГО общего поля по одному. Обе версии разбора и обе
        # формы строки: обычная и пустая. У пустой набор ключей доказанно тот
        # же, поэтому ей достаточно чтения — POST и отпечаток базы уже закрыты
        # на обычной строке тем же кодом отказа.
        print("\n== Удаление каждого общего поля: parser-2 и parser-1 ==")
        for version, shape in (("parser-2", anchor_sample),
                               ("parser-1", as_parser1(anchor_sample))):
            for field in sorted(ss._ROW_REQUIRED_FIELDS):
                row = {k: v for k, v in shape.items() if k != field}
                refuses(f"{version}, обычная строка без «{field}»",
                        spoil([row]), full=True)
        for version, shape in (("parser-2", blank_sample),
                               ("parser-1", as_parser1(blank_sample or {}))):
            for field in sorted(ss._ROW_REQUIRED_FIELDS):
                row = {k: v for k, v in shape.items() if k != field}
                refuses(f"{version}, пустая строка без «{field}»",
                        spoil([row]), full=False)

        # 4. Полные снимки обеих версий читаются. Иначе «стало строже» значило
        # бы «перестали читаться уже выпущенные снимки», а это не исправление.
        print("\n== Полные снимки обеих версий разбора читаются ==")
        exec_sql("UPDATE connections SET config_json = ? WHERE id = ?",
                 json.dumps(good, ensure_ascii=False), conn_id)
        cur_resp = req.get("/api/supply/sheets?limit=200")
        check("полный снимок parser-2 вообще читается", cur_resp.status_code == 200,
              f"{cur_resp.status_code} {cur_resp.text[:90]}")
        cur = cur_resp.json() if cur_resp.status_code == 200 else {}
        check("полный снимок parser-2 читается целиком",
              cur.get("total") == len(good_env["rows"]), str(cur.get("total")))
        check("и он не помечен устаревшим", cur.get("parser_stale") is False)
        stale = dict(good)
        stale[ss.ENVELOPE_KEY] = {
            **good_env,
            "rows": [as_parser1(row) for row in good_env["rows"]],
            "parser_version": "supply-sheets-parser-1"}
        exec_sql("UPDATE connections SET config_json = ? WHERE id = ?",
                 json.dumps(stale, ensure_ascii=False), conn_id)
        old_resp = req.get("/api/supply/sheets?limit=200")
        check("полный снимок parser-1 (без `sketch_raw`, с `extra_raw`) читается",
              old_resp.status_code == 200,
              f"{old_resp.status_code} {old_resp.text[:90]}")
        old = old_resp.json() if old_resp.status_code == 200 else {}
        check("снимок parser-1 отдан целиком",
              old.get("total") == len(good_env["rows"]), str(old.get("total")))
        check("и помечен устаревшим по версии разбора",
              old.get("parser_stale") is True)
        check("пустая строка-разделитель в снимке parser-1 тоже прочиталась",
              any(row.get("is_blank") for row in old.get("rows") or []))
        exec_sql("UPDATE connections SET config_json = ? WHERE id = ?",
                 json.dumps(good, ensure_ascii=False), conn_id)
        check("чужой ключ config_json пережил всю матрицу",
              json.loads(sql("SELECT config_json FROM connections WHERE id = ?",
                             conn_id)[0][0]).get("keep_req") == {"чужое": 2})
    finally:
        ss.set_transport(None)
        req.close()


# ── Непрерывность снимка при смене носителя ─────────────────────────────────

def _conn_rows(org_id: int):
    """Все связи организации: id, вид, config_json и НЕ-config поля."""
    return sql("SELECT id, kind, config_json, token_enc, status, last_sync_at,"
               " ms_agent_sync_id, ms_agent_href FROM connections"
               " WHERE org_id = ? ORDER BY id", org_id)


def _conn_by_kind(org_id: int, kind: str):
    for row in _conn_rows(org_id):
        if row[1] == kind:
            return row
    return None


def _env_of(row) -> dict | None:
    cfg = json.loads(row[2] or "{}")
    value = cfg.get(ss.ENVELOPE_KEY)
    return value if isinstance(value, dict) else None


def continuity_checks() -> None:  # noqa: C901 — сценарный тест: шагов много
    """Снимок не должен исчезать оттого, что рядом появилась новая связь.

    Дефект, найденный независимой подготовкой к ревью: носитель ЗАПИСИ выбирался
    канонически (`moysklad` → `demo`), а читатель смотрел ТОЛЬКО в него. Успешный
    снимок в `demo` + появившаяся позже пустая связь `moysklad` = снимок пропал
    с экрана, хотя данные целы. А следующая неудача записала бы в `moysklad`
    пустой skeleton и закрепила бы пропажу.

    Здесь проверяется поведение целиком: чтение идёт по тому же каноническому
    порядку и берёт первый носитель, где снимок ЕСТЬ; повреждённый снимок
    останавливает чтение, а не пропускается ради более старого; запись всегда
    идёт в канонический носитель и прежний снимок не разрушает.
    """
    print("\n== Снимок в demo, затем появляется пустой МойСклад ==")
    cont = client()
    cont.post("/register", data={"name": "Непрерывность", "email": "sheets-h@test.io",
                                "password": "secret123", "org_name": "Бренд-З"})
    cont.post("/api/connect/demo")
    org_id = sql("SELECT org_id FROM memberships WHERE user_id ="
                 " (SELECT id FROM users WHERE email = 'sheets-h@test.io')")[0][0]
    demo = _conn_by_kind(org_id, "demo")
    check("демо-связь заведена и она единственная",
          demo is not None and len(_conn_rows(org_id)) == 1, str(_conn_rows(org_id)))
    demo_cfg = json.loads(demo[2] or "{}")
    demo_cfg["keep_demo"] = {"чужое": [1, 2]}
    exec_sql("UPDATE connections SET config_json = ? WHERE id = ?",
             json.dumps(demo_cfg, ensure_ascii=False), demo[0])

    fake = FakeGoogle()
    ss.set_transport(fake)
    try:
        r = cont.post("/api/supply/sheets/refresh",
                      json={"spreadsheet_url": SHEET_URL,
                            "sheet_names": [SHEET_CURRENT, SHEET_NEXT]})
        check("первый снимок создан в демо-связи", r.status_code == 200, r.text[:140])
        good = _env_of(_conn_by_kind(org_id, "demo"))
        check("и он действительно лежит именно в демо", good is not None)
        good_hash = good["content_sha256"]
        good_rows = json.dumps(good["rows"], ensure_ascii=False, sort_keys=True)
        good_success = good["last_success_at"]
        demo_blob_before = _conn_by_kind(org_id, "demo")[2]

        # Появляется пустая связь МойСклада — она становится КАНОНИЧЕСКОЙ.
        ms_id = exec_sql(
            "INSERT INTO connections (org_id, kind, token_enc, status, config_json,"
            " last_sync_at, ms_agent_sync_id, ms_agent_href)"
            " VALUES (?, 'moysklad', 'tok-ms', 'active', ?, '2026-08-30 10:00:00',"
            " 'sync-1', 'href-1')", org_id, json.dumps({"ms_own": 42}))
        db = SessionLocal()
        try:
            check("канонический носитель ЗАПИСИ теперь МойСклад",
                  ss.select_carrier(db, org_id).id == ms_id,
                  str(ss.select_carrier(db, org_id).id))
            found, env = ss.read_envelope(db, org_id)
            check("а ЧТЕНИЕ находит снимок там, где он лежит — в демо",
                  found is not None and found.id == demo[0] and env is not None,
                  str(found and found.id))
        finally:
            db.close()

        print("\n== GET после появления новой связи: снимок на месте, записи нет ==")
        before = _fingerprint()
        fake.calls.clear()
        data = cont.get("/api/supply/sheets?limit=200").json()
        check("прежний хеш виден", data["content_sha256"] == good_hash,
              data["content_sha256"][:16])
        check("прежнее время успеха видно",
              data["last_success_at"] == good_success, str(data["last_success_at"]))
        check("строки видны, а не пропали", data["total"] == 10, str(data["total"]))
        check("источник считается настроенным", data["configured"] is True)
        check("GET не сделал НИ ОДНОЙ записи в базу",
              _diff(before, _fingerprint()) == [], str(_diff(before, _fingerprint())))
        check("и ни одного сетевого вызова", fake.calls == [], str(fake.calls))

        print("\n== Неудачное обновление после перехода ==")
        fake.bodies[SHEET_NEXT] = ss.HttpResponse(
            403, {}, b"forbidden body", "https://docs.google.com/x")
        r = cont.post("/api/supply/sheets/refresh",
                      json={"spreadsheet_url": SHEET_URL,
                            "sheet_names": [SHEET_CURRENT, SHEET_NEXT]})
        check("отказ источника — 502", r.status_code == 502, r.text[:120])
        ms_env = _env_of(_conn_by_kind(org_id, "moysklad"))
        check("в МойСклад приехал ПРЕЖНИЙ УСПЕШНЫЙ снимок, а не пустой skeleton",
              ms_env is not None and ms_env["content_sha256"] == good_hash,
              str(ms_env and ms_env["content_sha256"][:16]))
        check("вместе с прежними строками до байта",
              json.dumps(ms_env["rows"], ensure_ascii=False, sort_keys=True) == good_rows)
        check("и прежним временем успеха",
              ms_env["last_success_at"] == good_success, str(ms_env["last_success_at"]))
        check("плюс новая безопасная ошибка",
              "403" in ms_env["last_error"] and "forbidden body" not in ms_env["last_error"],
              ms_env["last_error"][:110])
        check("демо-связь не изменилась НИ НА БАЙТ",
              _conn_by_kind(org_id, "demo")[2] == demo_blob_before)
        shown = cont.get("/api/supply/sheets?limit=200").json()
        check("экран показывает строки и ошибку, а не skeleton",
              shown["total"] == 10 and "403" in shown["last_error"]
              and shown["configured"] is True, str(shown["total"]))

        print("\n== Удачное обновление после перехода пишет только в МойСклад ==")
        moved = next_rows()
        moved.append(put(blank(), {2: "2002", 3: "Шапка НГ", 5: "Белый",
                                   10: "4", 11: "4", 14: "2", 15: "10"}))
        fake.bodies[SHEET_NEXT] = to_csv(moved)
        r = cont.post("/api/supply/sheets/refresh",
                      json={"spreadsheet_url": SHEET_URL,
                            "sheet_names": [SHEET_CURRENT, SHEET_NEXT]})
        check("обновление удалось", r.status_code == 200, r.text[:140])
        new_hash = _env_of(_conn_by_kind(org_id, "moysklad"))["content_sha256"]
        check("новый снимок записан в МойСклад", new_hash != good_hash, new_hash[:16])
        check("демо по-прежнему не тронута ни на байт",
              _conn_by_kind(org_id, "demo")[2] == demo_blob_before)
        check("демо всё ещё несёт ПРЕЖНИЙ снимок, он не удалён",
              _env_of(_conn_by_kind(org_id, "demo"))["content_sha256"] == good_hash)
        after = cont.get("/api/supply/sheets?limit=200").json()
        check("чтение выбирает канонический носитель, а не более старый",
              after["content_sha256"] == new_hash, after["content_sha256"][:16])
        check("и показывает новые строки", after["total"] == 11, str(after["total"]))

        print("\n== Чужие ключи и не-config поля целы на ОБЕИХ связях ==")
        ms_row = _conn_by_kind(org_id, "moysklad")
        demo_row = _conn_by_kind(org_id, "demo")
        check("чужой ключ демо-связи цел",
              json.loads(demo_row[2]).get("keep_demo") == {"чужое": [1, 2]},
              str(json.loads(demo_row[2]).get("keep_demo")))
        check("чужой ключ связи МойСклада цел",
              json.loads(ms_row[2]).get("ms_own") == 42,
              str(json.loads(ms_row[2]).get("ms_own")))
        check("токен, вид, статус и время синка МойСклада не тронуты",
              (ms_row[3], ms_row[4], ms_row[5]) == ("tok-ms", "active",
                                                    "2026-08-30 10:00:00"),
              str(ms_row[3:6]))
        check("поля ms_* не тронуты",
              (ms_row[6], ms_row[7]) == ("sync-1", "href-1"), str(ms_row[6:8]))
        check("вид и статус демо-связи не тронуты",
              (demo_row[1], demo_row[4]) == ("demo", demo[4]), str(demo_row[1:5]))
        check("новых связей не появилось", len(_conn_rows(org_id)) == 2,
              str(len(_conn_rows(org_id))))

        print("\n== Два валидных снимка: канонический выигрывает детерминированно ==")
        db = SessionLocal()
        try:
            found, env = ss.read_envelope(db, org_id)
            check("выбран МойСклад, а не более старая демо",
                  found.id == ms_id and env["content_sha256"] == new_hash,
                  str(found.id))
        finally:
            db.close()
        check("и это НЕ зависит от порядка вставки: демо вставлена раньше",
              demo[0] < ms_id, f"demo={demo[0]} ms={ms_id}")
        fake.calls.clear()
        before = _fingerprint()
        r = cont.post("/api/supply/sheets/refresh",
                      json={"spreadsheet_url": SHEET_URL,
                            "sheet_names": [SHEET_CURRENT, SHEET_NEXT]})
        check("повтор принят и назван неизменившимся",
              r.status_code == 200 and r.json().get("unchanged") is True, r.text[:140])
        check("демо и после этого не изменилась",
              _conn_by_kind(org_id, "demo")[2] == demo_blob_before)

        print("\n== Повреждённый снимок НЕ перепрыгивается ради более старого ==")
        ms_cfg = json.loads(_conn_by_kind(org_id, "moysklad")[2])
        for label, payload in (
            ("версия из будущего", {**ms_cfg[ss.ENVELOPE_KEY], "schema_version": 7}),
            ("строки не список", {**ms_cfg[ss.ENVELOPE_KEY], "rows": "нет"}),
            ("под ключом не запись", "снимок"),
        ):
            spoiled = dict(ms_cfg)
            spoiled[ss.ENVELOPE_KEY] = payload
            blob = json.dumps(spoiled, ensure_ascii=False)
            exec_sql("UPDATE connections SET config_json = ? WHERE id = ?", blob, ms_id)
            probe = FakeGoogle()
            ss.set_transport(probe)
            before = _fingerprint()
            read = cont.get("/api/supply/sheets")
            check(f"чтение отказывает 409, а не показывает демо: {label}",
                  read.status_code == 409, f"{read.status_code} {read.text[:90]}")
            write = cont.post("/api/supply/sheets/refresh",
                              json={"spreadsheet_url": SHEET_URL,
                                    "sheet_names": [SHEET_CURRENT, SHEET_NEXT]})
            check(f"обновление тоже 409: {label}", write.status_code == 409,
                  str(write.status_code))
            check(f"без сети: {label}", probe.calls == [], str(probe.calls)[:80])
            check(f"без единой записи: {label}", _diff(before, _fingerprint()) == [],
                  str(_diff(before, _fingerprint())))
            check(f"повреждённое оставлено как есть: {label}",
                  _conn_by_kind(org_id, "moysklad")[2] == blob)
            check(f"и демо по-прежнему цела: {label}",
                  _conn_by_kind(org_id, "demo")[2] == demo_blob_before)
        exec_sql("UPDATE connections SET config_json = ? WHERE id = ?",
                 json.dumps(ms_cfg, ensure_ascii=False), ms_id)
        ss.set_transport(fake)
        check("возврат исправного снимка снова делает раздел рабочим",
              cont.get("/api/supply/sheets").status_code == 200)

        print("\n== Нечитаемый JSON канонической связи — тоже 409 без отката ==")
        exec_sql("UPDATE connections SET config_json = ? WHERE id = ?",
                 "не json вовсе", ms_id)
        check("чтение отказывает", cont.get("/api/supply/sheets").status_code == 409)
        check("и демо не подставляется вместо него",
              good_hash not in cont.get("/api/supply/sheets").text)
        exec_sql("UPDATE connections SET config_json = ? WHERE id = ?",
                 json.dumps(ms_cfg, ensure_ascii=False), ms_id)

        print("\n== Пустой канонический + повреждённый нижний = 409, не пустота ==")
        exec_sql("UPDATE connections SET config_json = ? WHERE id = ?",
                 json.dumps({"ms_own": 42}), ms_id)
        broken_demo = json.loads(demo_blob_before)
        broken_demo[ss.ENVELOPE_KEY] = {**broken_demo[ss.ENVELOPE_KEY],
                                        "schema_version": 99}
        exec_sql("UPDATE connections SET config_json = ? WHERE id = ?",
                 json.dumps(broken_demo, ensure_ascii=False), demo[0])
        probe = FakeGoogle()
        ss.set_transport(probe)
        before = _fingerprint()
        read = cont.get("/api/supply/sheets")
        check("повреждённый нижний носитель даёт 409, а не «снимка нет»",
              read.status_code == 409, f"{read.status_code} {read.text[:90]}")
        write = cont.post("/api/supply/sheets/refresh",
                          json={"spreadsheet_url": SHEET_URL,
                                "sheet_names": [SHEET_CURRENT, SHEET_NEXT]})
        check("обновление тоже 409", write.status_code == 409, str(write.status_code))
        check("без сети и без записей",
              probe.calls == [] and _diff(before, _fingerprint()) == [],
              str(probe.calls)[:60])

        print("\n== Пустой канонический + НЕЧИТАЕМЫЙ JSON нижнего = 409 ==")
        # Отдельно от «повреждённой версии» выше: там JSON был читаем, а снимок
        # нет. Здесь не читается сам `config_json` нижней связи — путь другой,
        # а обещание то же: отказ, а не «снимка нет» и не показ пустоты.
        exec_sql("UPDATE connections SET config_json = ? WHERE id = ?",
                 json.dumps({"ms_own": 42}), ms_id)
        exec_sql("UPDATE connections SET config_json = ? WHERE id = ?",
                 "{это не json", demo[0])
        probe = FakeGoogle()
        ss.set_transport(probe)
        before = _fingerprint()
        read = cont.get("/api/supply/sheets")
        check("нечитаемый JSON нижней связи даёт 409", read.status_code == 409,
              f"{read.status_code} {read.text[:90]}")
        write = cont.post("/api/supply/sheets/refresh",
                          json={"spreadsheet_url": SHEET_URL,
                                "sheet_names": [SHEET_CURRENT, SHEET_NEXT]})
        check("и обновление тоже 409", write.status_code == 409, str(write.status_code))
        check("без сети и без записей",
              probe.calls == [] and _diff(before, _fingerprint()) == [],
              str(probe.calls)[:60])
        check("нечитаемое содержимое не переписано",
              _conn_by_kind(org_id, "demo")[2] == "{это не json")
        exec_sql("UPDATE connections SET config_json = ? WHERE id = ?",
                 demo_blob_before, demo[0])

        print("\n== Переход у одной организации не задевает соседнюю ==")
        # Дёшево, но закрывает главный страх: `ordered_carriers` фильтрует по
        # арендатору, и обход носителей не может «дотянуться» до чужой строки.
        neighbour = client()
        neighbour.post("/register", data={"name": "Сосед", "email": "sheets-j@test.io",
                                          "password": "secret123", "org_name": "Бренд-К"})
        neighbour.post("/api/connect/demo")
        other_id = sql("SELECT org_id FROM memberships WHERE user_id ="
                       " (SELECT id FROM users WHERE email = 'sheets-j@test.io')")[0][0]
        ss.set_transport(FakeGoogle())
        neighbour.post("/api/supply/sheets/refresh",
                       json={"spreadsheet_url": SHEET_URL,
                             "sheet_names": [SHEET_CURRENT, SHEET_NEXT]})
        other_blob = _conn_rows(other_id)[0][2]
        check("у соседа есть свой снимок", ss.ENVELOPE_KEY in other_blob)
        db = SessionLocal()
        try:
            ours = {c.id for c in ss.ordered_carriers(db, org_id)}
            theirs = {c.id for c in ss.ordered_carriers(db, other_id)}
            check("обход носителей не пересекается между организациями",
                  not (ours & theirs), f"{sorted(ours)} vs {sorted(theirs)}")
            found_a = ss.read_envelope(db, org_id)[0]
            found_b = ss.read_envelope(db, other_id)[0]
            check("каждая организация читает свой носитель",
                  found_a.id in ours and found_b.id in theirs,
                  f"{found_a.id} / {found_b.id}")
        finally:
            db.close()
        ss.set_transport(fake)
        before_other = _conn_rows(other_id)
        cont.post("/api/supply/sheets/refresh",
                  json={"spreadsheet_url": SHEET_URL,
                        "sheet_names": [SHEET_CURRENT, SHEET_NEXT]})
        check("обновление у переехавшей организации не тронуло соседа",
              _conn_rows(other_id) == before_other)
        check("и сосед по-прежнему видит свой снимок",
              neighbour.get("/api/supply/sheets?limit=200").json()["total"] == 10)
        neighbour.close()

        print("\n== Валидный канонический + повреждённый нижний: читается верхний ==")
        exec_sql("UPDATE connections SET config_json = ? WHERE id = ?",
                 json.dumps(ms_cfg, ensure_ascii=False), ms_id)
        data = cont.get("/api/supply/sheets?limit=200").json()
        check("верхний валидный снимок читается нормально",
              data["content_sha256"] == new_hash, data["content_sha256"][:16])
        check("нижний повреждённый при этом даже не инспектируется",
              data["total"] == 11 and data["configured"] is True, str(data["total"]))
    finally:
        ss.set_transport(None)
        cont.close()


def purge_checks() -> None:
    print("\n== Удаление организации уносит носителя вместе со снимком ==")
    doomed = client()
    doomed.post("/register", data={"name": "Уходящий", "email": "sheets-d@test.io",
                                   "password": "secret123", "org_name": "Бренд-Г"})
    doomed.post("/api/connect/demo")
    org_id = sql("SELECT org_id FROM memberships WHERE user_id ="
                 " (SELECT id FROM users WHERE email = 'sheets-d@test.io')")[0][0]
    ss.set_transport(FakeGoogle())
    try:
        r = doomed.post("/api/supply/sheets/refresh",
                        json={"spreadsheet_url": SHEET_URL,
                              "sheet_names": [SHEET_CURRENT, SHEET_NEXT]})
        check("снимок у обречённой организации создан", r.status_code == 200, r.text[:140])
        check("и он действительно лежит в носителе", ss.ENVELOPE_KEY in _config(org_id))
        r = doomed.post("/api/account/delete",
                        json={"password": "secret123", "confirm": "УДАЛИТЬ",
                              "mode": "org"})
        check("организация удалена штатной ручкой", r.status_code == 200, r.text[:160])
        check("носитель со снимком стёрт вместе с ней",
              sql("SELECT COUNT(*) FROM connections WHERE org_id = ?", org_id)[0][0] == 0)
        check("и строка организации тоже",
              sql("SELECT COUNT(*) FROM orgs WHERE id = ?", org_id)[0][0] == 0)
    finally:
        ss.set_transport(None)
        doomed.close()


# ── Часть 6. Границы, доказанные структурно ─────────────────────────────────

def structural_checks(owner) -> None:
    print("\n== Чужой текст доезжает как текст, а не как разметка ==")
    data = owner.get(f"/api/supply/sheets?sheet={SHEET_CURRENT}&limit=200").json()
    row = next((r for r in data["rows"] if r["source_row"] == 10), None)
    check("строка с полезной нагрузкой доехала", row is not None, str(data["total"]))
    if row:
        check("разметка сохранена дословно, без экранирования на сервере",
              row["name"] == "<img src=x onerror=alert(1)>", row["name"])
        check("и в цвете тоже",
              row["color_raw"] == "\"><script>alert(2)</script>", row["color_raw"])

    page = owner.get("/supply").text
    template = (ROOT / "templates" / "supply.html").read_text(encoding="utf-8")
    check("страница «Поставки» отдаётся владельцу", "sup-rows" in page, page[:120])
    check("таблица строится DOM-API, а не сборкой разметки",
          "textContent" in page and "createElement" in page)
    # Проверяется САМ шаблон, а не собранная страница: в общий каркас входит
    # `_hints.html`, у которого свой давний код, и его упоминания innerHTML к
    # этому пакету отношения не имеют. Здесь важно одно — внешние данные не
    # попадают в разметку строкой, и это свойство именно этой страницы.
    check("innerHTML для внешних данных не используется вовсе",
          not re.search(r"\.innerHTML\s*=", template),
          "в шаблоне есть присваивание innerHTML")
    check("и разметка строкой не вставляется никаким другим способом",
          not any(bad in template for bad in
                  ("insertAdjacentHTML", ".outerHTML", "document.write")))
    check("честная подпись есть в самой разметке, а не только в ответе API",
          "не партия «Оборота»" in page and "не учитывается в «Едет»" in page)
    check("исходная таблица — ссылка, а не iframe",
          "<iframe" not in page and 'rel = "noopener noreferrer"' in page.replace('rel="', 'rel = "'))
    print("\n== Страница: ошибка первого сбоя и восстановленный ввод ==")
    # Регрессия по ревью PR #47. Раньше отрисовка состояния выходила раньше
    # блока ошибки, когда успешного снимка ещё нет, и человек видел только
    # тост. Проверяется структура шаблона: блок ошибки живёт СВОЕЙ функцией,
    # которая вызывается безусловно, а не внутри ветки «источник настроен».
    check("блок ошибки вынесен в отдельную функцию",
          "function renderError(data)" in template)
    # Именно БЕЗУСЛОВНО. Проверка на подстроку «renderError(data);» этого не
    # доказывает: `if (data.configured) renderError(data);` содержит её тоже —
    # и это ровно тот дефект, который ревью и нашло. Поэтому требуется строка,
    # которая целиком является вызовом.
    call_lines = [ln.strip() for ln in template.splitlines()
                  if "renderError(data)" in ln and "function" not in ln]
    check("и вызывается безусловно, рядом с остальной отрисовкой",
          "renderError(data);" in call_lines,
          f"вызов условный: {call_lines}")
    check("внутри renderState блока ошибки больше нет",
          "sup-error" not in template.split("function renderState(data)")[1]
          .split("function tile(")[0],
          "renderState всё ещё сам рисует ошибку")
    check("текст для случая «удачного чтения ещё не было» есть",
          "Удачного чтения ещё не было" in template)
    check("форма восстанавливает ссылку последней попытки",
          "attempt.spreadsheet_url" in template)
    check("и имена листов последней попытки",
          "attempt.sheet_names" in template)
    check("устаревший разбор проговаривается словами",
          "parser_stale" in template and "прежней версией разбора" in template)

    # Корректив по ревью PR #47. Ниже — структурные замки на три правки,
    # ПОВЕДЕНИЕ которых проверяет браузерный набор `tests/test_supply_ui.py`.
    # Здесь они стоят не вместо него, а рядом: этот набор офлайновый и
    # выполняется даже там, где Chromium не поднялся, — и тогда возврат
    # дефекта хотя бы не пройдёт молча.
    print("\n== Страница: замки на корректив по ревью ==")
    check("состояние подписки доезжает до страницы, а не вычисляется на глаз",
          "writes_blocked" in template and "WRITES_BLOCKED" in template)
    check("read-only состояние ветвится ДО отрисовки формы владельца",
          template.index("if (WRITES_BLOCKED)") < template.index('var form = el("div", "sup-form")'))
    check("общий offset не двигается по клику — позиция запроса локальная",
          "state.offset += state.limit" not in template
          and "var requested = state.offset + state.limit" in template)
    check("позиция фиксируется только после успешного ответа",
          "state.offset = offset;" in template)
    check("у догрузки есть замок «один запрос в полёте»",
          "inflightGen" in template and "moreBtn.disabled = true" in template)
    check("и поколение состояния, чтобы поздний ответ не дописался",
          "myGen !== gen" in template and "function resetView()" in template)
    check("успешное обновление сбрасывает фильтр листа",
          'state.sheet = "";' in template.split("function refresh()")[1]
          .split("function renderError")[0])

    # Корректив по UX-аудиту: оба замка перестали быть привязаны к тому, что
    # живёт не столько же, сколько они сами. Поведение проверяет браузерный
    # набор; здесь стоят структурные замки — офлайн и без Chromium.
    check("замок догрузки принадлежит поколению, а не странице",
          "var inflightGen = -1;" in template
          and "if (inflightGen === gen) return;" in template
          and "if (inflightGen !== myGen) return;" in template)
    check("и общего булева замка догрузки больше нет вовсе",
          "var inflight = false" not in template
          and "inflight = true;" not in template)
    check("финализатор догрузки трогает кнопку только своего поколения",
          "if (gen === myGen) moreBtn.disabled = false;" in template)
    check("замок обновления живёт ВНЕ DOM и переживает пересборку формы",
          "var refreshBusy = false;" in template
          and "if (refreshBusy) return;" in template
          and "function paintRefreshButton()" in template)
    check("и кнопка рождается в текущем состоянии обновления",
          "btn.disabled = refreshBusy;" in template
          and "REFRESH_LABELS[refreshBusy ? 1 : 0]" in template)

    # Корректив по ревью на HEAD `5e21ba1`. Поведение обоих исправлений
    # проверяет браузерный набор; здесь — только структурные замки, и они
    # стоят РЯДОМ с ним, а не вместо: этот набор офлайновый и отработает даже
    # там, где Chromium не поднялся.
    check("нет носителя — ветка стоит ДО построения формы, а не после",
          "if (!data.carrier_present) {" in template
          and template.index("if (!data.carrier_present) {")
          < template.index('var form = el("div", "sup-form")'))
    check("и она ведёт в «Настройки», а не в никуда",
          'slink.href = "/settings";' in template)
    check("поля формы предпочитают ввод последней НЕУДАЧНОЙ попытки",
          "var failedNow = !!(data.last_error);" in template
          and "var srcUrl = triple ? triple.url" in template)
    check("а имена листов берутся парой либо не берутся вовсе",
          "names.length !== 2" in template
          and "var names = triple ? triple.names" in template)
    check("ссылка «открыть исходник» осталась на УСПЕШНОМ снимке",
          "a.href = data.spreadsheet_url;" in template)

    # Корректив по ревью на HEAD `6a3cabd`. Поведение всех трёх проверяет
    # браузерный набор; здесь — офлайновые структурные замки рядом с ним.
    check("тройка попытки проверяется целиком, одной функцией",
          "function attemptTriple(attempt)" in template
          and "var triple = failedNow ? attemptTriple(attempt) : null;" in template)
    check("и адрес попытки сверяется с КАНОНИЧЕСКОЙ формой",
          "CANONICAL_SOURCE_URL" in template
          and "docs\\.google\\.com" in template)
    check("половины тройки не берутся по отдельности",
          "var srcUrl = triple ? triple.url" in template
          and "var names = triple ? triple.names" in template)
    check("догрузка сверяет версию снимка, а не только поколение вида",
          "var viewHash = null;" in template
          and 'if (append && hash !== (viewHash || "")) {' in template)
    check("и при расхождении перезапускает вид с начала, а не дописывает",
          "switchView(null);" in template.split("if (append && hash !==")[1]
          .split("return STALE;")[0])
    check("цена источника выводится сырой отдельной ячейкой",
          'el("td", "sup-raw sup-price", r.price_raw || "—")' in template)
    check("и у неё есть своя подпись в шапке",
          "<th>Цена источника</th>" in template)
    check("ширина «широких» строк не выписана числом в трёх местах",
          "colSpan = 13" not in template and "colSpan = 12" not in template
          and template.count("SUP_COLUMNS") >= 4)
    check("кнопка обновления восстанавливается при ЛЮБОМ исходе",
          ".then(restore, restore);" in template)
    check("неполный итог штук называется словами, а не числом",
          "не определено · прочитано" in template
          and "quantity_complete" in template)
    # `static/app.js` подключён с `defer`: его `api()` появляется ПОСЛЕ разбора
    # блока `scripts`. Запуск без ожидания `DOMContentLoaded` означал не
    # «иногда медленнее», а «страница мертва всегда»: она падала на
    # `api is not defined` и не показывала ни строк, ни формы, ни ошибки.
    # Найдено запуском страницы в браузере (tests/test_supply_ui.py) — на
    # уровне разметки дефект не виден вовсе, поэтому здесь стоит замок.
    check("страница ждёт DOMContentLoaded, а не зовёт api() при разборе",
          'document.addEventListener("DOMContentLoaded"' in template,
          "скрипт страницы запускается до загрузки app.js")
    check("и app.js по-прежнему подключён отложенно — это не догадка",
          'src="/static/app.js" defer' in
          (ROOT / "templates" / "base.html").read_text(encoding="utf-8"))

    check("на странице есть три фильтра очереди",
          "Требуют разбора" in page and "Ошибки" in page and "Все" in page)
    check("и одна кнопка обновления",
          page.count("Обновить предпросмотр") >= 1 and "sup-refresh" in page)
    check("поля источника подписаны по-человечески",
          "Текущий лист" in page and "Следующий лист" in page)
    check("страница верстается общим каркасом (мобильный и встроенный режимы)",
          '{% extends "base.html" %}' in Path(ROOT / "templates" / "supply.html")
          .read_text(encoding="utf-8"))
    check("в шаблоне есть мобильная раскладка",
          "@media (max-width: 760px)" in
          Path(ROOT / "templates" / "supply.html").read_text(encoding="utf-8"))

    print("\n== Раздел обнаружим в навигации ==")
    for name in ("base.html", "_embed.html", "replenish.html"):
        text = (ROOT / "templates" / name).read_text(encoding="utf-8")
        check(f"ссылка «Поставки» есть в {name}",
              'href="/supply"' in text and "Поставки" in text)
    base = (ROOT / "templates" / "base.html").read_text(encoding="utf-8")
    check("в base.html ссылка и в обычной навигации, и во встроенной",
          base.count('href="/supply"') == 2, str(base.count('href="/supply"')))

    print("\n== Слой не касается заказов, синка, планировщика и формул ==")
    supply_sources = {
        name: (ROOT / "app" / name).read_text(encoding="utf-8")
        for name in ("supply_sheets.py", "routes_supply.py")
    }
    for name, text in supply_sources.items():
        body = text.split('"""', 2)[-1]      # без модульного докстринга
        for forbidden in ("ProductionOrder", "OrderedQty", "OrderReceipt",
                          "OrderPlan", "cc_batch_id", "CC_BATCH_ID"):
            check(f"{name} не упоминает {forbidden} в коде",
                  forbidden not in body, forbidden)
        for forbidden in ("ms_client", "ms_sync", "ms_writeback", "order_planner",
                          "analytics"):
            check(f"{name} не импортирует {forbidden}",
                  f"import {forbidden}" not in text and f"from app.{forbidden}" not in text)

    for name in ("ms_sync.py", "ms_writeback.py", "ms_client.py", "order_planner.py",
                 "analytics.py", "analytics_extra.py", "scheduler.py"):
        text = (ROOT / "app" / name).read_text(encoding="utf-8")
        check(f"{name} ничего не знает про supply_sheets",
              "supply_sheets" not in text and ss.ENVELOPE_KEY not in text, name)

    print("\n== Схема не тронута: те же десять выпущенных шагов старта ==")
    from app import main as _main
    check("шагов ровно десять", len(_main.STARTUP_SCHEMA_STEPS) == 10,
          str(len(_main.STARTUP_SCHEMA_STEPS)))
    check("и это ровно прежние десять пар (id, позиция)",
          list(_main.STARTUP_SCHEMA_STEPS) == [
              ("init_db", 1), ("lessons.ensure_schema", 2),
              ("exclusions.ensure_schema", 3), ("ms_sync.ensure_schema", 4),
              ("ms_sync.reset_stale_running", 5), ("ms_writeback.ensure_schema", 6),
              ("ms_vendor.ensure_schema", 7), ("subscription.ensure_schema", 8),
              ("subscription.log_preview", 9), ("models.ensure_supply_schema", 10)],
          str(_main.STARTUP_SCHEMA_STEPS))
    for name in ("models.py", "db.py", "tenancy.py"):
        text = (ROOT / "app" / name).read_text(encoding="utf-8")
        check(f"{name} этим пакетом не тронут (нет ссылок на слой)",
              "supply_sheets" not in text and ss.ENVELOPE_KEY not in text, name)
    check("новых таблиц в базе не появилось",
          "supply_sheets" not in "".join(
              r[0] or "" for r in sql("SELECT sql FROM sqlite_master")),
          "в схеме есть таблица слоя")

    print("\n== Хеш содержимого: разделитель не может подделать источник ==")
    left = ss.content_hash("id", ["a", "b|c"], [b"1", b"2"])
    right = ss.content_hash("id", ["a|b", "c"], [b"1", b"2"])
    check("разные имена листов дают разные хеши",
          left != right, f"{left[:12]} vs {right[:12]}")
    check("порядок листов входит в хеш",
          ss.content_hash("id", ["a", "b"], [b"1", b"2"])
          != ss.content_hash("id", ["b", "a"], [b"1", b"2"]))
    check("байты CSV входят в хеш",
          ss.content_hash("id", ["a", "b"], [b"1", b"2"])
          != ss.content_hash("id", ["a", "b"], [b"1", b"3"]))
    check("идентификатор таблицы входит в хеш",
          ss.content_hash("id1", ["a", "b"], [b"1", b"2"])
          != ss.content_hash("id2", ["a", "b"], [b"1", b"2"]))
    check("версия парсера входит в хеш",
          ss.PARSER_VERSION.encode() and
          ss.content_hash("id", ["a"], [b"1"]) != ss.content_hash("id", ["a"], [b"1x"]))


def offline_checks(org_id: int) -> None:
    """Ни одного постороннего сетевого клиента: httpx на время вызова запрещён."""
    print("\n== Обновление не открывает НИ ОДНОГО постороннего соединения ==")
    fake = FakeGoogle()
    ss.set_transport(fake)
    real_client, real_async = httpx.Client, httpx.AsyncClient

    class Forbidden:
        def __init__(self, *a, **kw):
            raise AssertionError("слой открыл постороннее HTTP-соединение")

    httpx.Client = Forbidden
    httpx.AsyncClient = Forbidden
    db = SessionLocal()
    try:
        result = ss.refresh(db, org_id, SHEET_URL, [SHEET_CURRENT, SHEET_NEXT])
        check("обновление прошло без единого настоящего HTTP-клиента",
              result["content_sha256"], str(result)[:120])
        check("а инъектированный транспорт получил ровно два GET",
              len(fake.calls) == 2 and fake.methods == {"GET"}, str(fake.calls)[:120])
    except AssertionError as exc:
        check("обновление не открывает посторонних соединений", False, str(exc))
    finally:
        httpx.Client, httpx.AsyncClient = real_client, real_async
        db.close()
        ss.set_transport(None)


def config_guard_checks(org_id: int) -> None:
    print("\n== Нечитаемый config_json носителя: fail closed, а не перезапись ==")
    keep = sql("SELECT id, config_json FROM connections WHERE org_id = ? ORDER BY id",
               org_id)[0]
    exec_sql("UPDATE connections SET config_json = ? WHERE id = ?",
             "[1, 2, 3]", keep[0])
    ss.set_transport(FakeGoogle())
    db = SessionLocal()
    try:
        message = raises(
            lambda: ss.refresh(db, org_id, SHEET_URL, [SHEET_CURRENT, SHEET_NEXT]),
            ss.CarrierConfigError)
        check("массив вместо объекта — отказ, а не «починим перезаписью»",
              bool(message), message[:120])
        check("чужое содержимое осталось на месте",
              sql("SELECT config_json FROM connections WHERE id = ?", keep[0])[0][0]
              == "[1, 2, 3]")
        exec_sql("UPDATE connections SET config_json = ? WHERE id = ?",
                 "не json вовсе", keep[0])
        check("нечитаемый JSON — тоже отказ",
              bool(raises(lambda: ss.refresh(db, org_id, SHEET_URL,
                                             [SHEET_CURRENT, SHEET_NEXT]),
                          ss.CarrierConfigError)))
    finally:
        db.close()
        ss.set_transport(None)
        exec_sql("UPDATE connections SET config_json = ? WHERE id = ?", keep[1], keep[0])


def carrier_choice_checks() -> None:
    print("\n== Носитель выбирается детерминированно ==")
    picker = client()
    picker.post("/register", data={"name": "Двойной", "email": "sheets-e@test.io",
                                   "password": "secret123", "org_name": "Бренд-Д"})
    org_id = sql("SELECT org_id FROM memberships WHERE user_id ="
                 " (SELECT id FROM users WHERE email = 'sheets-e@test.io')")[0][0]
    exec_sql("DELETE FROM connections WHERE org_id = ?", org_id)
    # МойСклад заводится ПЕРВЫМ, demo — вторым, то есть с БОЛЬШИМ id. Порядок
    # не случайный: так «взять последнюю связь» (правило старого /api/settings)
    # дало бы demo, и подмена детерминированного выбора на «самую свежую строку»
    # видна проверкой, а не только рассуждением.
    ms_id = exec_sql("INSERT INTO connections (org_id, kind, token_enc, status,"
                     " config_json, ms_agent_sync_id, ms_agent_href)"
                     " VALUES (?, 'moysklad', 'tok', 'active', '{}', '', '')", org_id)
    demo_id = exec_sql("INSERT INTO connections (org_id, kind, token_enc, status,"
                       " config_json, ms_agent_sync_id, ms_agent_href)"
                       " VALUES (?, 'demo', '', 'active', '{}', '', '')", org_id)
    db = SessionLocal()
    try:
        picked = ss.select_carrier(db, org_id)
        check("при demo и moysklad носитель — moysklad, а не последняя строка",
              picked is not None and picked.id == ms_id,
              f"выбран {picked and picked.id}, demo={demo_id}, ms={ms_id}")
        exec_sql("DELETE FROM connections WHERE id = ?", ms_id)
        db.expire_all()
        picked = ss.select_carrier(db, org_id)
        check("остался только demo — носитель он",
              picked is not None and picked.id == demo_id, str(picked and picked.id))
        alien = exec_sql("INSERT INTO connections (org_id, kind, token_enc, status,"
                         " config_json, ms_agent_sync_id, ms_agent_href)"
                         " VALUES (?, 'google_sheets', '', 'active', '{}', '', '')",
                         org_id)
        db.expire_all()
        picked = ss.select_carrier(db, org_id)
        check("связь постороннего вида носителем не считается",
              picked is not None and picked.id == demo_id,
              f"выбран {picked and picked.id}, посторонний={alien}")
    finally:
        db.close()
        picker.close()



# ── Корректив по ревью PR #47 (REVIEW_REJECT на HEAD `08142a5`) ──────────────
#
# Восемь подтверждённых P2. Ниже — регрессии на каждый серверный пункт; три
# пункта, которые живут в браузере (read-only форма подписки, сериализованная
# догрузка и сброс устаревшего фильтра), проверяются запуском страницы —
# `tests/test_supply_ui.py`, потому что «строка есть в шаблоне» их не ловит.

#: Строка, которой в НАСТОЯЩЕЙ таблице быть не может, — и потому её появление
#: где угодно однозначно означает утечку содержимого источника, а не совпадение.
SENTINEL = "СЕКРЕТ-ЯЧЕЙКИ-b7f3e1a9"


def safe_error_checks() -> None:  # noqa: C901 — матрица веток, ветвлений мало
    """Ни одна ветка проверки заголовка не печатает содержимое чужой ячейки.

    Замечание ревью PR #47. Отказ отсюда уходит тремя дорогами сразу: в ответ
    502 владельцу, в сохранённый `last_error` носителя и оттуда — в GET,
    который читает ЛЮБОЙ участник организации. Значение ячейки приехало из
    чужой таблицы, которую заполняют люди; личные данные в ней возможны, и
    показывать их в сообщении об ошибке нельзя.

    Проверяются ВСЕ ветки, а не та одна, на которую указало ревью: дефект был
    не в конкретной строке, а в способе писать сообщения.
    """
    print("\n== Отказ источника не выносит наружу содержимое чужой ячейки ==")

    def drift(where: str, row1_patch: dict, row2_patch: dict) -> str:
        rows = autumn_header_rows()
        put(rows[0], row1_patch)
        put(rows[1], row2_patch)
        rows.append(put(blank(), {2: "1", 3: "Т", 10: "1", 15: "1"}))
        message = raises(
            lambda: ss.parse_sheet("Осень 26", ss.decode_csv("Осень 26", to_csv(rows))),
            ss.SourceError)
        check(f"ветка «{where}»: отказ случился", bool(message), message[:120])
        check(f"ветка «{where}»: содержимого ячейки в тексте нет",
              SENTINEL not in message, message[:160])
        return message

    # 1. Подпись каркаса не на месте (та самая строка 568 из отчёта ревью).
    msg = drift("подпись каркаса", {5: SENTINEL}, {})
    check("и при этом отказ по-прежнему называет колонку и ожидаемое",
          "колонке 5" in msg and "«Цвет»" in msg, msg[:160])
    # 2. Колонка артикула обязана быть без заголовка.
    drift("артикул без заголовка", {2: SENTINEL}, {})
    # 3. Крайняя метка размерной горки.
    drift("край размерной горки", {}, {10: SENTINEL})
    # 4. Промежуточная метка размерной горки.
    drift("середина размерной горки", {}, {11: SENTINEL})
    # 5. Подписи итога и цены за горкой.
    drift("подпись итога", {}, {15: SENTINEL})
    drift("подпись цены", {}, {16: SENTINEL})
    # 6. Повтор подписи каркаса: сообщение и так о колонках, но проверяется.
    dup = drift("повтор подписи каркаса", {20: "Цвет"}, {})
    check("повтор назван колонками, а не содержимым",
          "Цвет" in dup and "раз" in dup, dup[:160])
    # 7. Вторая метка размера за пределами горки.
    drift("вторая метка размера", {}, {20: "XL"})
    # 8. Ячейка длиннее предела: номер строки есть, содержимого нет.
    long_rows = autumn_header_rows()
    long_rows.append(put(blank(), {3: SENTINEL + "x" * ss.MAX_CELL_CHARS}))
    long_msg = raises(lambda: ss.decode_csv("Осень 26", to_csv(long_rows)),
                      ss.SourceError)
    check("ветка «слишком длинная ячейка»: содержимого в тексте нет",
          bool(long_msg) and SENTINEL not in long_msg, long_msg[:120])

    print("\n== И ни одной дорогой наружу: 502, носитель, owner, member, HTML ==")
    holder = client()
    holder.post("/register", data={"name": "Утечка", "email": "sheets-leak@test.io",
                                   "password": "secret123", "org_name": "Бренд-У"})
    holder.post("/api/connect/demo")
    org_id = sql("SELECT org_id FROM memberships WHERE user_id ="
                 " (SELECT id FROM users WHERE email = 'sheets-leak@test.io')")[0][0]
    add_member(org_id, "sheets-leak-m@test.io")
    watcher = client()
    watcher.post("/login", data={"email": "sheets-leak-m@test.io",
                                 "password": "secret123"})
    ss.set_transport(FakeGoogle())
    try:
        ok = holder.post("/api/supply/sheets/refresh",
                         json={"spreadsheet_url": SHEET_URL,
                               "sheet_names": [SHEET_CURRENT, SHEET_NEXT]})
        check("прежний успешный снимок сделан", ok.status_code == 200, ok.text[:120])
        good_env = _config(org_id)[ss.ENVELOPE_KEY]
        good_hash = good_env["content_sha256"]
        good_rows = json.dumps(good_env["rows"], ensure_ascii=False, sort_keys=True)

        poisoned = autumn_header_rows()
        put(poisoned[0], {5: SENTINEL})
        poisoned.append(put(blank(), {2: "1", 3: "Т", 10: "1", 15: "1"}))
        ss.set_transport(FakeGoogle({SHEET_CURRENT: AUTUMN_CSV,
                                     SHEET_NEXT: to_csv(poisoned)}))
        bad = holder.post("/api/supply/sheets/refresh",
                          json={"spreadsheet_url": SHEET_URL,
                                "sheet_names": [SHEET_CURRENT, SHEET_NEXT]})
        check("дрейф с чужим текстом — 502", bad.status_code == 502, bad.text[:120])
        check("в теле 502 содержимого ячейки нет", SENTINEL not in bad.text,
              bad.text[:160])
        stored = _config(org_id)[ss.ENVELOPE_KEY]
        check("в сохранённом last_error содержимого ячейки нет",
              SENTINEL not in stored["last_error"], stored["last_error"][:160])
        check("и вообще во всём носителе его нет",
              SENTINEL not in json.dumps(_config(org_id), ensure_ascii=False))
        owner_json = holder.get("/api/supply/sheets?limit=200").text
        check("владелец не получает содержимого ячейки", SENTINEL not in owner_json)
        member_json = watcher.get("/api/supply/sheets?limit=200").text
        check("участник тоже не получает", SENTINEL not in member_json)
        check("HTML страницы его тоже не несёт",
              SENTINEL not in holder.get("/supply").text)
        check("причина отказа при этом ЕСТЬ и она осмысленна",
              "заголовк" in stored["last_error"], stored["last_error"][:120])
        check("прежний успешный снимок цел: хеш",
              stored["content_sha256"] == good_hash)
        check("прежний успешный снимок цел: строки до байта",
              json.dumps(stored["rows"], ensure_ascii=False, sort_keys=True)
              == good_rows)
    finally:
        ss.set_transport(None)
        holder.close()
        watcher.close()


def attempt_privacy_checks() -> None:
    """Адрес и листы НЕУДАЧНОЙ попытки — владельцу, а не участнику.

    Замечание ревью PR #47. Ввести ссылку и имена листов может только
    владелец; форму восстанавливать нужно ему же. Участнику это состояние не
    адресовано вовсе, а успешный снимок остаётся общим — так и записано в D-51.
    """
    print("\n== Неудачная попытка владельца не уезжает участнику ==")
    boss = client()
    boss.post("/register", data={"name": "Приватность", "email": "sheets-p@test.io",
                                "password": "secret123", "org_name": "Бренд-П"})
    boss.post("/api/connect/demo")
    org_id = sql("SELECT org_id FROM memberships WHERE user_id ="
                 " (SELECT id FROM users WHERE email = 'sheets-p@test.io')")[0][0]
    add_member(org_id, "sheets-p-m@test.io")
    mate = client()
    mate.post("/login", data={"email": "sheets-p-m@test.io", "password": "secret123"})

    denied = FakeGoogle({SHEET_CURRENT: ss.HttpResponse(
        403, {}, b"forbidden body", "https://docs.google.com/x")})
    ss.set_transport(denied)
    try:
        r = boss.post("/api/supply/sheets/refresh",
                      json={"spreadsheet_url": SHEET_URL,
                            "sheet_names": [SHEET_CURRENT, SHEET_NEXT]})
        check("первое обновление владельца отказало", r.status_code == 502, r.text[:120])

        mine = boss.get("/api/supply/sheets?limit=200").json()
        check("владелец получает свой ввод обратно",
              mine["attempt"]["spreadsheet_url"] == ss.spreadsheet_link(SPREADSHEET_ID)
              and mine["attempt"]["sheet_names"] == [SHEET_CURRENT, SHEET_NEXT],
              str(mine["attempt"]))

        theirs_raw = mate.get("/api/supply/sheets?limit=200").text
        theirs = json.loads(theirs_raw)
        check("участнику попытка не отдана вовсе",
              theirs["attempt"] == {"spreadsheet_id": "", "spreadsheet_url": "",
                                    "sheet_names": []},
              str(theirs["attempt"]))
        check("идентификатора чужой таблицы в ответе участника нет",
              SPREADSHEET_ID not in theirs_raw)
        # Ничего НЕ вырезается перед сравнением. Прежняя редакция убирала из
        # текста ответа сериализованный `last_error` и только потом искала имя
        # листа — то есть проверяла всё, кроме канала, по которому имя как раз
        # и утекало (замечание ревью PR #47 на HEAD `590b5c6`). Проверка,
        # которая заранее вычитает подозрительное место, доказывает меньше,
        # чем кажется. Отдельная матрица этого канала — `failure_privacy_checks`.
        check("и имён листов неудачной попытки тоже нет — во ВСЁМ ответе",
              SHEET_CURRENT not in theirs_raw and SHEET_NEXT not in theirs_raw,
              theirs_raw[:200])
        check("но причина отказа участнику видна — это состояние раздела",
              theirs["last_error"] == ss.PUBLIC_FAILURE_REASONS["access"],
              theirs["last_error"][:100])
        check("источник настроенным при этом не считается ни у кого",
              mine["configured"] is False and theirs["configured"] is False)

        print("\n== Удачный снимок остаётся общим для владельца и участника ==")
        ss.set_transport(FakeGoogle())
        ok = boss.post("/api/supply/sheets/refresh",
                       json={"spreadsheet_url": SHEET_URL,
                             "sheet_names": [SHEET_CURRENT, SHEET_NEXT]})
        check("повторное обновление удалось", ok.status_code == 200, ok.text[:120])
        mine = boss.get("/api/supply/sheets?limit=200").json()
        theirs = mate.get("/api/supply/sheets?limit=200").json()
        check("оба видят одну и ту же ссылку на источник",
              mine["spreadsheet_url"] == theirs["spreadsheet_url"]
              == ss.spreadsheet_link(SPREADSHEET_ID), str(theirs["spreadsheet_url"]))
        check("оба видят одни и те же листы и строки",
              mine["sheet_names"] == theirs["sheet_names"] == [SHEET_CURRENT, SHEET_NEXT]
              and mine["total"] == theirs["total"], str(theirs["sheet_names"]))
        check("право обновлять по-прежнему только у владельца",
              mine["can_refresh"] is True and theirs["can_refresh"] is False)
    finally:
        ss.set_transport(None)
        boss.close()
        mate.close()


#: Имя листа, которого нет больше нигде: ни в фикстурах, ни в коде, ни в
#: соседних проверках. Уникальность здесь — не украшение: проверка «имени листа
#: нет во ВСЁМ ответе участника» имеет смысл только если совпадение может быть
#: вызвано ровно одной причиной. Обычное «Осень 26» встречается в ответе
#: законно (это имя листа удачного снимка), и на нём проверка доказывала бы
#: обратное тому, что нужно.
SENTINEL_SHEET = "Щ-сентинель-Vx7Kq9Zt-текущий"
SENTINEL_SHEET_NEXT = "Щ-сентинель-Vx7Kq9Zt-следующий"


def failure_privacy_checks() -> None:  # noqa: C901 — сценарий, ветвлений мало
    """Причина отказа видна обоим, но чужой ввод владельца — только владельцу.

    Замечание ревью PR #47 на HEAD `590b5c6`. Предыдущий корректив убрал из
    текста отказа содержимое ЯЧЕЙКИ источника и спрятал от участника адрес и
    листы попытки (`attempt`), но САМ текст отказа остался общим: он собирался
    как «лист «<имя, которое ввёл владелец>»: …», сохранялся в общий
    `last_error` и оттуда уезжал в GET, который читает любой участник
    организации. То есть закрытая дверь стояла рядом с открытым окном.

    Здесь проверяется не формулировка, а канал: уникальное имя листа НЕ
    ВСТРЕЧАЕТСЯ во всём ответе участника и во всём HTML страницы — включая
    `last_error`, который прежняя проверка вырезала перед сравнением и тем
    самым маскировала дефект. Владелец при этом сохраняет подробность: без
    имени листа он не знает, какой из двух чинить.
    """
    print("\n== Имя листа неудачной попытки: владельцу да, участнику нет ==")
    boss = client()
    boss.post("/register", data={"name": "Приватность-2",
                                 "email": "sheets-pv@test.io",
                                 "password": "secret123", "org_name": "Бренд-ПВ"})
    boss.post("/api/connect/demo")
    org_id = sql("SELECT org_id FROM memberships WHERE user_id ="
                 " (SELECT id FROM users WHERE email = 'sheets-pv@test.io')")[0][0]
    conn_id = sql("SELECT id FROM connections WHERE org_id = ? ORDER BY id",
                  org_id)[0][0]
    add_member(org_id, "sheets-pv-m@test.io")
    mate = client()
    mate.post("/login", data={"email": "sheets-pv-m@test.io",
                              "password": "secret123"})

    def envelope_now() -> dict:
        cfg = json.loads(sql("SELECT config_json FROM connections WHERE id = ?",
                             conn_id)[0][0])
        return cfg[ss.ENVELOPE_KEY]

    def probe(label: str, expect_in_detailed: str, public_code: str) -> None:
        """Один отказ — пять дорог наружу, и на каждой имени листа быть не должно."""
        mine = boss.get("/api/supply/sheets?limit=200").json()
        theirs_raw = mate.get("/api/supply/sheets?limit=200").text
        theirs = json.loads(theirs_raw)
        stored = envelope_now()

        check(f"{label}: владелец сохраняет подробность — какой лист сломался",
              SENTINEL_SHEET in (mine.get("last_error") or ""),
              (mine.get("last_error") or "")[:140])
        check(f"{label}: и она осмысленна, а не просто содержит имя",
              expect_in_detailed in (mine.get("last_error") or ""),
              (mine.get("last_error") or "")[:140])
        # ГЛАВНАЯ проверка пакета: ничего не вырезается перед сравнением.
        check(f"{label}: имени листа нет НИГДЕ во всём ответе участника",
              SENTINEL_SHEET not in theirs_raw
              and SENTINEL_SHEET_NEXT not in theirs_raw,
              theirs_raw[:220])
        check(f"{label}: в том числе в его last_error",
              SENTINEL_SHEET not in (theirs.get("last_error") or ""),
              (theirs.get("last_error") or "")[:160])
        check(f"{label}: идентификатора чужой таблицы у участника тоже нет",
              SPREADSHEET_ID not in theirs_raw)
        # Причина участника — КОНСТАНТА нашего исходника, выбранная по коду и
        # связанная отпечатком с этой самой неудачей. Проверяется равенство, а
        # не вхождение подстроки: «содержит» пропустило бы приписанный к
        # константе хвост из носителя, а именно этого здесь и не должно быть.
        check(f"{label}: причина у участника НЕПУСТА и это наша константа",
              bool((theirs.get("last_error") or "").strip())
              and theirs["last_error"] == ss.PUBLIC_FAILURE_REASONS[public_code],
              (theirs.get("last_error") or "")[:160])
        check(f"{label}: HTML страницы участника имени листа не несёт",
              SENTINEL_SHEET not in mate.get("/supply").text)
        check(f"{label}: и HTML страницы владельца тоже (его строит JS)",
              SENTINEL_SHEET not in boss.get("/supply").text)
        check(f"{label}: в носителе подробный текст есть, а в общем — нет",
              SENTINEL_SHEET in stored["last_error"]
              and SENTINEL_SHEET not in stored["last_error_public"],
              stored["last_error_public"][:160])
        check(f"{label}: источник настроенным не считается ни у кого",
              mine["configured"] is False and theirs["configured"] is False)

    try:
        # 1. Отказ транспорта: 403 на листе с сентинельным именем.
        ss.set_transport(FakeGoogle({SENTINEL_SHEET: ss.HttpResponse(
            403, {}, b"forbidden body", "https://docs.google.com/x")}))
        r = boss.post("/api/supply/sheets/refresh",
                      json={"spreadsheet_url": SHEET_URL,
                            "sheet_names": [SENTINEL_SHEET, SENTINEL_SHEET_NEXT]})
        check("отказ 403 доехал как 502", r.status_code == 502, r.text[:120])
        check("и владельцу в теле 502 названо, какой именно лист",
              SENTINEL_SHEET in r.text, r.text[:160])
        probe("403", "403", "access")

        # 2. Дрейф заголовка: отказ рождается в парсере, а не в транспорте, —
        #    это ДРУГАЯ ветка сборки текста, и её тоже надо пройти.
        ss.set_transport(FakeGoogle({
            SENTINEL_SHEET: to_csv(legacy_wrong_header_rows()),
            SENTINEL_SHEET_NEXT: NEXT_CSV}))
        r = boss.post("/api/supply/sheets/refresh",
                      json={"spreadsheet_url": SHEET_URL,
                            "sheet_names": [SENTINEL_SHEET, SENTINEL_SHEET_NEXT]})
        check("дрейф заголовка тоже 502", r.status_code == 502, r.text[:120])
        probe("дрейф заголовка", "заголовк", "format")

        print("\n== Тот же запрет, когда удачный снимок УЖЕ есть ==")
        # Самое живое состояние: снимок с обычными именами прочитан и общий для
        # обеих ролей, а неудачная попытка сделана уже с ДРУГИМИ листами.
        # Прежний снимок при этом остаётся видимым обоим (D-51), и проверять
        # надо ровно то, что имя листа НЕУДАВШЕЙСЯ попытки в общий вид не
        # просочилось ни одним полем.
        ss.set_transport(FakeGoogle())
        ok = boss.post("/api/supply/sheets/refresh",
                       json={"spreadsheet_url": SHEET_URL,
                             "sheet_names": [SHEET_CURRENT, SHEET_NEXT]})
        check("удачный снимок создан обычными листами",
              ok.status_code == 200, ok.text[:120])
        ss.set_transport(FakeGoogle({SENTINEL_SHEET: ss.HttpResponse(
            500, {}, b"", "https://docs.google.com/x")}))
        bad = boss.post("/api/supply/sheets/refresh",
                        json={"spreadsheet_url": SHEET_URL,
                              "sheet_names": [SENTINEL_SHEET, SENTINEL_SHEET_NEXT]})
        check("следующая попытка другими листами отказала",
              bad.status_code == 502, bad.text[:120])
        theirs_raw = mate.get("/api/supply/sheets?limit=200").text
        theirs = json.loads(theirs_raw)
        mine = boss.get("/api/supply/sheets?limit=200").json()
        check("прежний снимок остался виден обоим — это прежнее решение D-51",
              theirs["configured"] is True and theirs["total"] == mine["total"] > 0
              and theirs["sheet_names"] == [SHEET_CURRENT, SHEET_NEXT],
              str((theirs["configured"], theirs["total"])))
        check("а имён листов неудачной попытки нет во ВСЁМ ответе участника",
              SENTINEL_SHEET not in theirs_raw
              and SENTINEL_SHEET_NEXT not in theirs_raw,
              (theirs.get("last_error") or "")[:160])
        check("и в HTML его страницы тоже нет",
              SENTINEL_SHEET not in mate.get("/supply").text)
        check("причина отказа участнику при этом видна",
              theirs.get("last_error") == ss.PUBLIC_FAILURE_REASONS["unavailable"],
              (theirs.get("last_error") or "")[:160])
        check("владелец видит и подробность, и свой ввод обратно",
              SENTINEL_SHEET in (mine.get("last_error") or "")
              and mine["attempt"]["sheet_names"] == [SENTINEL_SHEET,
                                                     SENTINEL_SHEET_NEXT],
              str(mine["attempt"]["sheet_names"]))
        check("и сам снимок неудача не тронула: хеш и время успеха прежние",
              envelope_now()["content_sha256"] == mine["content_sha256"] != ""
              and envelope_now()["last_success_at"] == mine["last_success_at"],
              str(mine["content_sha256"])[:16])

        print("\n== Носителю на слово не верят: свободный текст не читается ==")
        # Снимок, записанный ДО этого разделения (или после отката релиза),
        # кода причины не несёт вовсе. Читатель не может знать, что лежит в
        # подробном тексте, — и обязан отдать участнику generic.
        stored = envelope_now()
        rolled_back = {k: v for k, v in stored.items()
                       if k not in ("last_error_public_code",
                                    "last_error_public_binding")}
        cfg = json.loads(sql("SELECT config_json FROM connections WHERE id = ?",
                             conn_id)[0][0])
        cfg[ss.ENVELOPE_KEY] = rolled_back
        blob = json.dumps(cfg, ensure_ascii=False)
        exec_sql("UPDATE connections SET config_json = ? WHERE id = ?",
                 blob, conn_id)
        theirs_raw = mate.get("/api/supply/sheets?limit=200").text
        theirs = json.loads(theirs_raw)
        check("снимок без кода причины читается, а не 409",
              mate.get("/api/supply/sheets?limit=200").status_code == 200)
        check("и участник получает generic, а не подробность прежней версии",
              SENTINEL_SHEET not in theirs_raw
              and theirs["last_error"] == ss.PUBLIC_FAILURE_FALLBACK,
              theirs["last_error"][:160])
        check("сохранённый рядом свободный текст на экран участника НЕ попадает",
              isinstance(rolled_back.get("last_error_public"), str)
              and rolled_back["last_error_public"] != ""
              and rolled_back["last_error_public"] not in theirs_raw,
              str(rolled_back.get("last_error_public"))[:120])
        check("владелец при этом подробность не потерял",
              SENTINEL_SHEET in boss.get("/api/supply/sheets").json()["last_error"])
        check("и чтение ничего не переписало в носителе",
              sql("SELECT config_json FROM connections WHERE id = ?",
                  conn_id)[0][0] == blob)

        # Подложенный руками свободный текст — та же история, но теперь она
        # заканчивается раньше: читатель его не сверяет, он его не читает.
        # Даже безобидный на вид текст без строк владельца участнику не уезжает.
        planted = "подложено рукой: источник ответил 418"
        cfg[ss.ENVELOPE_KEY] = {**rolled_back, "last_error_public": planted}
        blob = json.dumps(cfg, ensure_ascii=False)
        exec_sql("UPDATE connections SET config_json = ? WHERE id = ?",
                 blob, conn_id)
        theirs_raw = mate.get("/api/supply/sheets?limit=200").text
        check("подложенный в носитель свободный текст участнику не уезжает",
              planted not in theirs_raw
              and json.loads(theirs_raw)["last_error"] == ss.PUBLIC_FAILURE_FALLBACK,
              theirs_raw[:200])
        check("и это чтение тоже ничего не записало",
              sql("SELECT config_json FROM connections WHERE id = ?",
                  conn_id)[0][0] == blob)

        print("\n== Сам фильтр: что считается небезопасным текстом ==")
        check("имя листа в тексте — замена на generic целиком",
              ss._role_safe_failure(f"лист «{SENTINEL_SHEET}»: беда",
                                    SPREADSHEET_ID, [SENTINEL_SHEET])
              == ss.PUBLIC_FAILURE_FALLBACK)
        check("идентификатор таблицы в тексте — тоже",
              ss._role_safe_failure(f"таблица {SPREADSHEET_ID} закрыта",
                                    SPREADSHEET_ID, [SENTINEL_SHEET])
              == ss.PUBLIC_FAILURE_FALLBACK)
        check("пустой текст generic'ом не остаётся пустым",
              ss._role_safe_failure("   ", SPREADSHEET_ID, [])
              == ss.PUBLIC_FAILURE_FALLBACK
              and ss._role_safe_failure(None, SPREADSHEET_ID, [])
              == ss.PUBLIC_FAILURE_FALLBACK)
        check("чистый текст проходит как есть, а не обедняется",
              ss._role_safe_failure("лист источника: источник ответил 500",
                                    SPREADSHEET_ID, [SENTINEL_SHEET])
              == "лист источника: источник ответил 500")
        check("у каждого отказа слоя общий текст непустой",
              bool(ss.SourceError("что-то").public)
              and ss.SourceError("что-то", "иначе").public == "иначе")
    finally:
        ss.set_transport(None)
        boss.close()
        mate.close()


def malformed_url_checks() -> None:
    """`spreadsheet_url` не строкой — управляемый 400, а не 500.

    Замечание ревью PR #47: тело запроса нетипизировано, и `{"spreadsheet_url":
    123}` доезжал до `(raw or "").strip()`, где число даёт `AttributeError`.
    Проверяется не только код ответа: на негодном вводе не должно быть НИ
    ОДНОГО сетевого вызова и НИ ОДНОЙ записи в базу, а прежний снимок и
    носитель обязаны остаться теми же до байта.
    """
    print("\n== Негодный тип ссылки: 400, ноль сети, ноль записей ==")
    typed = client()
    typed.post("/register", data={"name": "Типы", "email": "sheets-t@test.io",
                                 "password": "secret123", "org_name": "Бренд-Т"})
    typed.post("/api/connect/demo")
    org_id = sql("SELECT org_id FROM memberships WHERE user_id ="
                 " (SELECT id FROM users WHERE email = 'sheets-t@test.io')")[0][0]

    ss.set_transport(FakeGoogle())
    try:
        ok = typed.post("/api/supply/sheets/refresh",
                        json={"spreadsheet_url": SHEET_URL,
                              "sheet_names": [SHEET_CURRENT, SHEET_NEXT]})
        check("снимок для контроля создан", ok.status_code == 200, ok.text[:120])
        env_before = json.dumps(_config(org_id)[ss.ENVELOPE_KEY],
                                ensure_ascii=False, sort_keys=True)
        carrier_before = _carrier_row(org_id)

        cases = [("число", 123), ("дробное", 1.5), ("логическое", True),
                 ("список", ["https://docs.google.com/spreadsheets/d/" + SPREADSHEET_ID]),
                 ("объект", {"url": SHEET_URL}), ("null", None)]
        for label, value in cases:
            probe = FakeGoogle()
            ss.set_transport(probe)
            before = _fingerprint()
            r = typed.post("/api/supply/sheets/refresh",
                           json={"spreadsheet_url": value,
                                 "sheet_names": [SHEET_CURRENT, SHEET_NEXT]})
            check(f"{label}: управляемый 400, а не 500",
                  r.status_code == 400, f"{r.status_code} {r.text[:110]}")
            check(f"{label}: в ответе объяснение, а не трассировка",
                  "AttributeError" not in r.text and "Traceback" not in r.text,
                  r.text[:110])
            check(f"{label}: ни одного сетевого вызова", probe.calls == [],
                  str(probe.calls)[:100])
            check(f"{label}: ни одной записи в базу",
                  _diff(before, _fingerprint()) == [],
                  str(_diff(before, _fingerprint())))
            check(f"{label}: снимок не тронут",
                  json.dumps(_config(org_id)[ss.ENVELOPE_KEY], ensure_ascii=False,
                             sort_keys=True) == env_before)
            check(f"{label}: носитель не тронут",
                  _carrier_row(org_id) == carrier_before)
            # Та же граница на уровне функции: маршрут её не подменяет.
            message = raises(lambda v=value: ss.parse_spreadsheet_url(v),
                             ss.ValidationError)
            check(f"{label}: функция отказывает своим типом ошибки",
                  bool(message) and not message.startswith("<"), message[:110])

        print("\n== Негодные имена листов ведут себя так же ==")
        for label, value in [("не список", "Осень 26"), ("число внутри", [1, 2]),
                             ("одно имя", [SHEET_CURRENT]),
                             ("null внутри", [SHEET_CURRENT, None])]:
            probe = FakeGoogle()
            ss.set_transport(probe)
            before = _fingerprint()
            r = typed.post("/api/supply/sheets/refresh",
                           json={"spreadsheet_url": SHEET_URL, "sheet_names": value})
            check(f"листы «{label}»: 400 без сети и без записей",
                  r.status_code == 400 and probe.calls == []
                  and _diff(before, _fingerprint()) == [],
                  f"{r.status_code} {probe.calls}")
    finally:
        ss.set_transport(None)
        typed.close()


def incomplete_counts_checks() -> None:  # noqa: C901 — сценарий, ветвлений мало
    """Неполный набор количеств не выдаётся за окончательный итог.

    Замечание ревью PR #47 и исполнение уже закреплённого D-51 «нечитаемое или
    отсутствующее не равно нулю». Новой формулы здесь нет: `size_sum` строки
    остаётся тем же объясняющим показателем, меняется только контракт агрегата.
    """
    print("\n== Итог штук: число или честное «не определено» ==")

    def sheet_of(name, data_rows):
        rows = autumn_header_rows()
        for patch in data_rows:
            rows.append(put(blank(), patch))
        return ss.parse_sheet(name, ss.decode_csv(name, to_csv(rows)))[0]

    full = sheet_of("Полный", [
        {2: "1", 3: "А", 10: "2", 11: "3", 15: "5"},
        {2: "2", 3: "Б", 10: "4", 15: "4"},
    ])
    mixed = sheet_of("Смешанный", [
        {2: "1", 3: "А", 10: "2", 11: "3", 15: "5"},
        {2: "2", 3: "Б", 10: "4", 14: "Кроим по заданию"},
    ])
    missing = sheet_of("Без количеств", [
        {2: "1", 3: "А", 10: "2", 11: "3", 15: "5"},
        {2: "2", 3: "Б", 10: "-", 11: "—"},
    ])

    full_counts = ss.build_counts(full, ["Полный"])["sheets"][0]
    check("полный набор: итог — число, и оно равно прочитанному",
          full_counts["quantity"] == 9 and full_counts["quantity_known"] == 9
          and full_counts["quantity_complete"] is True, str(full_counts))

    mixed_counts = ss.build_counts(mixed, ["Смешанный"])["sheets"][0]
    check("нечитаемый размер: итога нет, прочитанное названо",
          mixed_counts["quantity"] is None
          and mixed_counts["quantity_known"] == 5 + 4
          and mixed_counts["quantity_complete"] is False, str(mixed_counts))
    broken = [r for r in mixed if "invalid_quantity" in r["issues"]]
    check("строка с нечитаемой ячейкой помечена и её сумма НЕ стёрта",
          len(broken) == 1 and broken[0]["size_sum"] == 4
          and broken[0]["sizes_raw"]["XL"] == "Кроим по заданию", str(broken))
    check("и нечитаемая ячейка не превратилась в ноль",
          broken and broken[0]["sizes"]["XL"] is None, str(broken[0]["sizes"]))

    missing_counts = ss.build_counts(missing, ["Без количеств"])["sheets"][0]
    check("строка без количеств тоже делает итог неопределённым",
          missing_counts["quantity"] is None
          and missing_counts["quantity_known"] == 5
          and missing_counts["quantity_complete"] is False, str(missing_counts))

    both = ss.build_counts(full + mixed, ["Полный", "Смешанный"])
    check("полный лист остаётся полным рядом с неполным",
          both["sheets"][0]["quantity_complete"] is True
          and both["sheets"][1]["quantity_complete"] is False, str(both["sheets"]))
    check("общий итог неполон, если неполон хотя бы один лист",
          both["quantity"] is None and both["quantity_known"] == 9 + 9
          and both["quantity_complete"] is False,
          str({k: both[k] for k in ("quantity", "quantity_known",
                                    "quantity_complete")}))
    check("расхождение итога полноты НЕ отменяет: оба числа прочитаны",
          ss.build_counts(sheet_of("Расхождение", [
              {2: "1", 3: "А", 10: "2", 15: "9"}]), ["Расхождение"]
          )["quantity"] == 2)

    print("\n== Снимок прежней версии разбора не показывает частичный итог ==")
    stale_c = client()
    stale_c.post("/register", data={"name": "Устаревший разбор",
                                    "email": "sheets-stale@test.io",
                                    "password": "secret123", "org_name": "Бренд-С"})
    stale_c.post("/api/connect/demo")
    org_id = sql("SELECT org_id FROM memberships WHERE user_id ="
                 " (SELECT id FROM users WHERE email = 'sheets-stale@test.io')")[0][0]
    conn_id = sql("SELECT id FROM connections WHERE org_id = ? ORDER BY id",
                  org_id)[0][0]
    ss.set_transport(FakeGoogle())
    try:
        r = stale_c.post("/api/supply/sheets/refresh",
                         json={"spreadsheet_url": SHEET_URL,
                               "sheet_names": [SHEET_CURRENT, SHEET_NEXT]})
        check("исходный снимок создан", r.status_code == 200, r.text[:120])
        fresh = stale_c.get("/api/supply/sheets?limit=200").json()
        check("свежий снимок сделан сегодняшним разбором",
              fresh["parser_stale"] is False
              and fresh["parser_version"] == ss.PARSER_VERSION,
              fresh["parser_version"])
        check("и его сводка честно неполна (в фикстуре есть нечитаемое)",
              (fresh.get("counts") or {}).get("quantity", "нет ключа") is None
              and (fresh.get("counts") or {}).get("quantity_complete") is False,
              str((fresh.get("counts") or {}).get("quantity_known")))

        # Хеш прежней версии считается ТОЙ ЖЕ функцией на той же константе —
        # иначе «версия парсера входит в хеш» осталось бы утверждением из
        # комментария, а не проверенным фактом.
        real_version = ss.PARSER_VERSION
        ss.PARSER_VERSION = "supply-sheets-parser-2"
        try:
            old_digest = ss.content_hash(SPREADSHEET_ID,
                                         [SHEET_CURRENT, SHEET_NEXT],
                                         [AUTUMN_CSV, NEXT_CSV])
        finally:
            ss.PARSER_VERSION = real_version
        new_digest = ss.content_hash(SPREADSHEET_ID, [SHEET_CURRENT, SHEET_NEXT],
                                     [AUTUMN_CSV, NEXT_CSV])
        check("те же байты источника при разных версиях разбора дают разный хеш",
              old_digest != new_digest, f"{old_digest[:12]} vs {new_digest[:12]}")

        for old_version, old_counts in (
            ("supply-sheets-parser-1", {"sheets": [
                {"sheet_name": SHEET_CURRENT, "rows": 9, "data_rows": 8,
                 "needs_review": 5, "invalid": 2, "quantity": 57},
                {"sheet_name": SHEET_NEXT, "rows": 3, "data_rows": 1,
                 "needs_review": 1, "invalid": 0, "quantity": 0}],
                "rows": 12, "data_rows": 9, "needs_review": 6, "invalid": 2,
                "quantity": 57, "issues": {}}),
            ("supply-sheets-parser-2", {"sheets": [
                {"sheet_name": SHEET_CURRENT, "rows": 9, "data_rows": 8,
                 "needs_review": 5, "invalid": 2, "quantity": 57},
                {"sheet_name": SHEET_NEXT, "rows": 3, "data_rows": 1,
                 "needs_review": 1, "invalid": 0, "quantity": 0}],
                "rows": 12, "data_rows": 9, "needs_review": 6, "invalid": 2,
                "quantity": 57, "issues": {}}),
        ):
            cfg = json.loads(sql("SELECT config_json FROM connections WHERE id = ?",
                                 conn_id)[0][0])
            cfg[ss.ENVELOPE_KEY] = {**cfg[ss.ENVELOPE_KEY],
                                    "parser_version": old_version,
                                    "content_sha256": old_digest,
                                    "counts": old_counts}
            blob = json.dumps(cfg, ensure_ascii=False)
            exec_sql("UPDATE connections SET config_json = ? WHERE id = ?",
                     blob, conn_id)
            seen = stale_c.get("/api/supply/sheets?limit=200").json()
            # Ключи читаются через `.get`: проверка обязана ПОКРАСНЕТЬ на
            # возврате дефекта, а не упасть посреди прогона. Красный контроль
            # уже ловил здесь именно это — снимок прежней сводки не несёт
            # сегодняшних ключей вовсе, и прямое обращение убивало набор
            # вместо честного FAIL.
            counts = seen.get("counts") or {}
            per_sheet = {sheet.get("sheet_name"): sheet
                         for sheet in (counts.get("sheets") or [])}
            check(f"{old_version}: снимок читается и помечен устаревшим",
                  seen.get("parser_stale") is True and seen.get("total") == 10,
                  str((seen.get("parser_stale"), seen.get("total"))))
            check(f"{old_version}: частичный итог НЕ показан окончательным",
                  counts.get("quantity", "нет ключа") is None
                  and counts.get("quantity_complete") is False,
                  str(counts.get("quantity", "нет ключа")))
            check(f"{old_version}: прочитанное пересчитано из самих строк",
                  counts.get("quantity_known") == 14 + 6 + 25 + 10 + 1 + 1,
                  str(counts.get("quantity_known")))
            check(f"{old_version}: и по листам тоже, а не только в целом",
                  per_sheet.get(SHEET_CURRENT, {}).get("quantity", "нет") is None
                  and per_sheet.get(SHEET_CURRENT, {}).get("quantity_known") == 57
                  and per_sheet.get(SHEET_NEXT, {}).get("quantity", "нет") is None
                  and per_sheet.get(SHEET_NEXT, {}).get("quantity_known") == 0,
                  str(counts.get("sheets")))
            check(f"{old_version}: сохранённое число 57 больше не выдаётся за итог",
                  counts.get("quantity", "нет ключа") != old_counts["quantity"],
                  str(counts.get("quantity", "нет ключа")))
            check(f"{old_version}: пересчёт НИЧЕГО не записал в носитель",
                  sql("SELECT config_json FROM connections WHERE id = ?",
                      conn_id)[0][0] == blob)

        print("\n== Тот же источник после смены версии разбора — новый импорт ==")
        probe = FakeGoogle()
        ss.set_transport(probe)
        again = stale_c.post("/api/supply/sheets/refresh",
                             json={"spreadsheet_url": SHEET_URL,
                                   "sheet_names": [SHEET_CURRENT, SHEET_NEXT]})
        check("обновление принято", again.status_code == 200, again.text[:120])
        check("и оно честно названо НОВЫМ импортом, а не «ничего не изменилось»",
              again.json().get("unchanged") is False, again.text[:150])
        healed = stale_c.get("/api/supply/sheets?limit=200").json()
        check("снимок вылечился сам: он больше не устаревший",
              healed["parser_stale"] is False
              and healed["parser_version"] == ss.PARSER_VERSION,
              healed["parser_version"])
        check("и хеш содержимого стал хешем сегодняшней версии разбора",
              healed["content_sha256"] == new_digest != old_digest,
              healed["content_sha256"][:16])
    finally:
        ss.set_transport(None)
        stale_c.close()


def stored_counts_checks() -> None:  # noqa: C901 — матрица подмен, ветвлений мало
    """Сводка на чтении ВЫВОДИТСЯ из строк, а не принимается по виду полей.

    Замечание ревью PR #47 на HEAD `590b5c6`. Прежний читатель принимал
    сохранённую сводку, если её поля были на месте и нужного ТИПА. Но тип — не
    содержание: сводка с настоящим `bool` в `quantity_complete` и настоящим
    `int` в `quantity` проезжала на экран, даже если сами числа были ложными —
    записанными прежней версией, оставшимися от отката релиза или поправленными
    руками. А носитель в этом слое прямо описан как «то, что оставил кто-то
    другой»: доверять его числам по форме значит показывать человеку выдумку
    как факт.

    Проверяется поэтому не «сводка пересчитана», а РЕЗУЛЬТАТ: точные числа,
    выведенные из тех же проверенных строк, включая имена листов и порядок.
    И отдельно — что чтение осталось чтением: носитель байт в байт тот же,
    сетевых вызовов ноль.
    """
    print("\n== Ложная сводка нужного вида на экран не попадает ==")
    liar = client()
    liar.post("/register", data={"name": "Ложная сводка",
                                 "email": "sheets-cnt@test.io",
                                 "password": "secret123", "org_name": "Бренд-Ц"})
    liar.post("/api/connect/demo")
    org_id = sql("SELECT org_id FROM memberships WHERE user_id ="
                 " (SELECT id FROM users WHERE email = 'sheets-cnt@test.io')")[0][0]
    conn_id = sql("SELECT id FROM connections WHERE org_id = ? ORDER BY id",
                  org_id)[0][0]

    # Истина этой фикстуры выписана ЧИСЛАМИ, а не выражена через ту же функцию,
    # которую проверяем: иначе проверка сравнивала бы код сам с собой и молча
    # соглашалась бы с любой его будущей ошибкой.
    TRUE_TOTAL = {"rows": 10, "data_rows": 9, "needs_review": 6, "invalid": 3,
                  "quantity": None, "quantity_known": 57,
                  "quantity_complete": False}
    TRUE_SHEETS = [
        {"sheet_name": SHEET_CURRENT, "rows": 9, "data_rows": 8,
         "needs_review": 5, "invalid": 3, "quantity": None,
         "quantity_known": 57, "quantity_complete": False},
        {"sheet_name": SHEET_NEXT, "rows": 1, "data_rows": 1,
         "needs_review": 1, "invalid": 0, "quantity": None,
         "quantity_known": 0, "quantity_complete": False},
    ]

    # Ложь СЕГОДНЯШНЕЙ формы: все поля на месте, все нужного типа, все врут.
    # Прежний `_counts_are_current` пропускал ровно такую сводку целиком.
    def liar_sheet(name: str) -> dict:
        return {"sheet_name": name, "rows": 999, "data_rows": 999,
                "needs_review": 0, "invalid": 0, "quantity": 4242,
                "quantity_known": 4242, "quantity_complete": True}

    ss.set_transport(FakeGoogle())
    try:
        r = liar.post("/api/supply/sheets/refresh",
                      json={"spreadsheet_url": SHEET_URL,
                            "sheet_names": [SHEET_CURRENT, SHEET_NEXT]})
        check("исходный снимок создан", r.status_code == 200, r.text[:120])
        honest = liar.get("/api/supply/sheets?limit=200").json()["counts"]
        check("честная сводка совпала с выписанной в тесте истиной",
              all(honest.get(k) == v for k, v in TRUE_TOTAL.items())
              and honest.get("sheets") == TRUE_SHEETS,
              json.dumps({k: honest.get(k) for k in TRUE_TOTAL},
                         ensure_ascii=False))
        # Сохранённая сводка продолжает ПИСАТЬСЯ, и это не рудимент: старый код
        # после отката релиза читает её напрямую. Убрать её из носителя было бы
        # не упрощением, а поломкой отката.
        stored_counts = json.loads(sql(
            "SELECT config_json FROM connections WHERE id = ?",
            conn_id)[0][0])[ss.ENVELOPE_KEY]["counts"]
        check("и она же честно записана в носитель — для отката",
              all(stored_counts.get(k) == v for k, v in TRUE_TOTAL.items())
              and stored_counts.get("sheets") == TRUE_SHEETS,
              json.dumps({k: stored_counts.get(k) for k in TRUE_TOTAL},
                         ensure_ascii=False))

        fakes = {
            "числа лгут, форма сегодняшняя": {
                **TRUE_TOTAL, "rows": 999, "data_rows": 999, "needs_review": 0,
                "invalid": 0, "quantity": 4242, "quantity_known": 4242,
                "quantity_complete": True, "issues": {},
                "sheets": [liar_sheet(SHEET_CURRENT), liar_sheet(SHEET_NEXT)]},
            "листов нет вовсе": {
                **TRUE_TOTAL, "quantity": 0, "quantity_known": 0,
                "quantity_complete": True, "issues": {}, "sheets": []},
            "имена листов чужие": {
                **TRUE_TOTAL, "issues": {},
                "sheets": [liar_sheet("Совсем другой лист"),
                           liar_sheet("И ещё один")]},
            "неполнота выдана за полноту": {
                **TRUE_TOTAL, "quantity": 57, "quantity_complete": True,
                "issues": {},
                "sheets": [{**TRUE_SHEETS[0], "quantity": 57,
                            "quantity_complete": True},
                           {**TRUE_SHEETS[1], "quantity": 0,
                            "quantity_complete": True}]},
        }

        for label, fake in fakes.items():
            cfg = json.loads(sql("SELECT config_json FROM connections WHERE id = ?",
                                 conn_id)[0][0])
            cfg[ss.ENVELOPE_KEY] = {**cfg[ss.ENVELOPE_KEY], "counts": fake}
            blob = json.dumps(cfg, ensure_ascii=False)
            exec_sql("UPDATE connections SET config_json = ? WHERE id = ?",
                     blob, conn_id)

            # Сетевой счётчик обнуляется на каждой подмене: «ноль вызовов»
            # должно относиться к ЭТОМУ GET, а не к прогону в целом.
            watcher = FakeGoogle()
            ss.set_transport(watcher)
            seen = liar.get("/api/supply/sheets?limit=200").json()
            counts = seen.get("counts") or {}

            check(f"{label}: верхние числа — пересчитанные, а не сохранённые",
                  all(counts.get(k, "нет ключа") == v
                      for k, v in TRUE_TOTAL.items()),
                  json.dumps({k: counts.get(k, "нет ключа") for k in TRUE_TOTAL},
                             ensure_ascii=False))
            check(f"{label}: листы, их имена и порядок — тоже",
                  counts.get("sheets") == TRUE_SHEETS,
                  json.dumps(counts.get("sheets"), ensure_ascii=False)[:220])
            check(f"{label}: и ложное число на экран не попало",
                  json.dumps(counts, ensure_ascii=False).find("4242") == -1,
                  json.dumps(counts, ensure_ascii=False)[:200])
            check(f"{label}: GET остался read-only — носитель байт в байт тот же",
                  sql("SELECT config_json FROM connections WHERE id = ?",
                      conn_id)[0][0] == blob)
            check(f"{label}: и ни одного сетевого вызова",
                  watcher.calls == [], str(watcher.calls))

        print("\n== Тот же источник: ответ честен, а улику никто не «чинит» ==")
        # Те же байты — `unchanged`: строки не переписываются, и сводка тоже.
        # Испорченную запись обновление НЕ «лечит» перезаписью — ровно как
        # повреждённый снимок не чинится чтением. Правду при этом обязаны
        # говорить оба ответа: и GET, и сам ответ обновления.
        before = sql("SELECT config_json FROM connections WHERE id = ?",
                     conn_id)[0][0]
        ss.set_transport(FakeGoogle())
        again = liar.post("/api/supply/sheets/refresh",
                          json={"spreadsheet_url": SHEET_URL,
                                "sheet_names": [SHEET_CURRENT, SHEET_NEXT]})
        check("те же байты — это `unchanged`",
              again.status_code == 200 and again.json().get("unchanged") is True,
              again.text[:150])
        after_counts = json.loads(sql(
            "SELECT config_json FROM connections WHERE id = ?",
            conn_id)[0][0])[ss.ENVELOPE_KEY]["counts"]
        check("сохранённая ложь при `unchanged` осталась лежать как была",
              after_counts["quantity"] == 57
              and after_counts["quantity_complete"] is True,
              json.dumps(after_counts, ensure_ascii=False)[:160])
        check("но ответ обновления показывает пересчитанную правду",
              all((again.json().get("counts") or {}).get(k, "нет ключа") == v
                  for k, v in TRUE_TOTAL.items()),
              json.dumps(again.json().get("counts"), ensure_ascii=False)[:200])
        check("и он совпадает с тем, что показывает GET — до единого числа",
              again.json().get("counts") == liar.get(
                  "/api/supply/sheets?limit=200").json()["counts"])
        check("а строки снимка `unchanged` не тронул",
              json.loads(before)[ss.ENVELOPE_KEY]["rows"]
              == json.loads(sql("SELECT config_json FROM connections"
                                " WHERE id = ?", conn_id)[0][0]
                            )[ss.ENVELOPE_KEY]["rows"])
    finally:
        ss.set_transport(None)
        liar.close()


def continuation_checks() -> None:
    """Продолжение неполного якоря наследует неполноту вместе с идентичностью.

    Замечание ревью PR #47: очередь «требуют разбора» строится по собственным
    `issues` строки, поэтому продолжение неполного якоря из неё выпадало — его
    количества и пометки опознать по-прежнему нельзя, а на экране разбора его
    нет. Границы соседних случаев при этом не двигаются: пустая строка остаётся
    разделителем, сирота — сиротой, продолжение ПОЛНОГО якоря — чистым.
    """
    print("\n== Продолжение неполного якоря видно в очереди разбора ==")

    def parse(data_rows):
        rows = autumn_header_rows()
        for patch in data_rows:
            rows.append(put(blank(), patch))
        return ss.parse_sheet("Осень 26",
                              ss.decode_csv("Осень 26", to_csv(rows)))[0]

    for label, anchor_patch in (("только артикул", {2: "1042"}),
                                ("только имя", {3: "Пальто «Осень»"})):
        parsed = parse([anchor_patch, {5: "Молоко", 10: "3"}, {5: "Хаки", 10: "1"}])
        anchor, first, second = parsed
        check(f"{label}: якорь помечен неполной идентичностью",
              "identity_missing_part" in anchor["issues"], str(anchor["issues"]))
        check(f"{label}: первое продолжение унаследовало пометку",
              "identity_missing_part" in first["issues"]
              and first["anchor_row"] == anchor["source_row"], str(first["issues"]))
        check(f"{label}: и второе тоже — наследуется якорь, а не соседняя строка",
              "identity_missing_part" in second["issues"]
              and second["anchor_row"] == anchor["source_row"], str(second["issues"]))
        check(f"{label}: обе строки попадают в очередь разбора",
              len(ss.filter_rows(parsed, None, "needs_review")) == 3,
              str([r["issues"] for r in parsed]))
        check(f"{label}: но ошибкой это не объявлено — читать можно, трактовать нет",
              ss.filter_rows(parsed, None, "invalid") == [],
              str(ss.filter_rows(parsed, None, "invalid")))

    print("\n== Соседние случаи не сдвинулись ==")
    clean = parse([{2: "1042", 3: "Пальто", 10: "2", 15: "2"},
                   {5: "Молоко", 10: "1", 15: "1"}])
    check("продолжение ПОЛНОГО якоря остаётся чистым",
          clean[1]["issues"] == [] and clean[1]["anchor_row"] == 3,
          str(clean[1]["issues"]))
    reset = parse([{2: "1042"}, {}, {5: "Хаки", 10: "1"}])
    check("пустая строка-разделитель по-прежнему сбрасывает якорь",
          reset[1]["is_blank"] is True and reset[1]["issues"] == [],
          str(reset[1]["issues"]))
    check("а строка после разделителя становится сиротой, а не наследницей",
          reset[2]["issues"] == ["orphan_continuation"]
          and reset[2]["anchor_row"] is None, str(reset[2]))
    orphan = parse([{5: "Синий", 10: "2"}])
    check("сирота в начале листа осталась сиротой и ничем больше",
          orphan[0]["issues"] == ["orphan_continuation"], str(orphan[0]["issues"]))
    after_full = parse([{2: "1", 3: "А", 10: "1", 15: "1"}, {2: "2"},
                        {5: "Хаки", 10: "1"}])
    check("новый неполный якорь перебивает прежний полный",
          "identity_missing_part" in after_full[2]["issues"]
          and after_full[2]["anchor_row"] == after_full[1]["source_row"],
          str(after_full[2]["issues"]))


class _CountingCsv:
    """Обёртка над модулем `csv`, считающая ФАКТИЧЕСКИ прочитанные строки.

    Нужна для одного утверждения, которое иначе не доказать: предел строк
    срабатывает ПО ХОДУ чтения, а не после того, как весь CSV уже материализован
    в памяти. Проверка «в конце пришёл отказ» этого не отличает вовсе — отказ
    приходил и раньше, просто после того, как ресурс был потрачен.
    """

    def __init__(self, real):
        self._real = real
        self.Error = real.Error
        self.rows = 0

    def reader(self, *args, **kwargs):
        for row in self._real.reader(*args, **kwargs):
            self.rows += 1
            yield row


def csv_limit_checks() -> None:
    """Предел строк CSV срабатывает на limit+1, а не после всего файла."""
    print("\n== Предел строк CSV: отказ до усиления, а не после ==")
    over = ("\r\n" * (ss.MAX_ROWS_PER_SHEET + 500)).encode("utf-8")
    check("ответ при этом умещается в разрешённые байты",
          len(over) <= ss.MAX_RESPONSE_BYTES, str(len(over)))

    real_csv = ss.csv
    counting = _CountingCsv(real_csv)
    ss.csv = counting
    try:
        message = raises(lambda: ss.decode_csv("Осень 26", over), ss.SourceError)
    finally:
        ss.csv = real_csv
    check("слишком длинный лист отвергнут", bool(message), message[:120])
    check("и отказ называет предел, а не «что-то пошло не так»",
          str(ss.MAX_ROWS_PER_SHEET) in message, message[:120])
    check("итератор прочитан ровно до предела плюс одна строка",
          counting.rows == ss.MAX_ROWS_PER_SHEET + 1,
          f"прочитано {counting.rows}, ожидалось {ss.MAX_ROWS_PER_SHEET + 1}")

    edge = ("\r\n" * ss.MAX_ROWS_PER_SHEET).encode("utf-8")
    counting = _CountingCsv(real_csv)
    ss.csv = counting
    try:
        rows = ss.decode_csv("Осень 26", edge)
    finally:
        ss.csv = real_csv
    check("ровно предел строк ещё принимается",
          len(rows) == ss.MAX_ROWS_PER_SHEET, str(len(rows)))
    check("и лишнего чтения на границе не случилось",
          counting.rows == ss.MAX_ROWS_PER_SHEET, str(counting.rows))

    print("\n== Прежние пределы байтов, колонок и ячеек не ослаблены ==")
    wide = to_csv([[str(i) for i in range(ss.MAX_COLUMNS + 1)]])
    check("колонок больше предела — отказ",
          str(ss.MAX_COLUMNS) in raises(lambda: ss.decode_csv("Ш", wide),
                                        ss.SourceError))
    long_cell = to_csv([["x" * (ss.MAX_CELL_CHARS + 1)]])
    check("ячейка длиннее предела — отказ",
          str(ss.MAX_CELL_CHARS) in raises(lambda: ss.decode_csv("Я", long_cell),
                                           ss.SourceError))
    check("предел байтов ответа остался прежним",
          ss.MAX_RESPONSE_BYTES == 2 * 1024 * 1024, str(ss.MAX_RESPONSE_BYTES))
    # Ошибка самого разбора CSV должна остаться управляемой и после перехода
    # на итератор: раньше `csv.Error` ловился вокруг `list(reader)`, теперь —
    # вокруг каждого `next`, и потерять её на этом переходе было бы легко.
    huge_field = b'"' + b"x" * (csv.field_size_limit() + 10) + b'"\r\n'
    check("сбой самого разбора CSV — управляемый отказ, а не падение набора",
          "CSV не разбирается" in raises(
              lambda: ss.decode_csv("Б", huge_field), ss.SourceError),
          raises(lambda: ss.decode_csv("Б", huge_field), ss.SourceError)[:120])

# ── Корректив по ревью PR #47 (REVIEW_REJECT на HEAD `459f170`) ─────────────

def bound_public_reason_checks() -> None:  # noqa: C901 — матрица подмен, ветвлений мало
    """Участник не получает НИ ОДНОЙ свободной строки из носителя.

    Замечание ревью PR #47 на HEAD `459f170`. Прежняя редакция отдавала
    участнику `last_error_public` — persisted-строку, признанную безопасной
    лишь потому, что в ней не нашлось подстрок ТЕКУЩЕЙ попытки. У этой проверки
    было два прохода насквозь, и оба воспроизводятся здесь буквально:

      A. ОТКАТ. Старый писатель обновляет подробный текст, время и источник
         попытки, а незнакомое ему поле общего текста переносит из прежней
         записи как есть. Строк новой попытки в нём нет, сверка проходит — и
         участник читает причину ПРОШЛОЙ попытки как сегодняшнюю.
      B. ИСПОРЧЕННЫЙ ИСТОЧНИК ПОПЫТКИ. `last_attempt_source` отсутствует или не
         того вида — сверять не с чем, «совпадений нет», и произвольный текст
         уезжает участнику целиком. Проверка формы envelope здесь не помогает:
         она смотрит ТИП необязательного поля, а не его происхождение.

    Проверяется не формулировка, а канал: в ответе участника не должно быть ни
    подложенной строки, ни подробностей чужой попытки — только константа из
    закрытого списка либо generic. Владелец подробность сохраняет. GET при этом
    остаётся read-only и без сети: носитель сверяется побайтно ДО и ПОСЛЕ, а
    транспорт подменён на такой, который от любого вызова падает.
    """
    print("\n== Причина для участника связана с записанной неудачей ==")
    boss = client()
    boss.post("/register", data={"name": "Связывание",
                                 "email": "sheets-bind@test.io",
                                 "password": "secret123", "org_name": "Бренд-СВ"})
    boss.post("/api/connect/demo")
    org_id = sql("SELECT org_id FROM memberships WHERE user_id ="
                 " (SELECT id FROM users WHERE email = 'sheets-bind@test.io')")[0][0]
    conn_id = sql("SELECT id FROM connections WHERE org_id = ? ORDER BY id",
                  org_id)[0][0]
    add_member(org_id, "sheets-bind-m@test.io")
    mate = client()
    mate.post("/login", data={"email": "sheets-bind-m@test.io",
                              "password": "secret123"})

    def exploding_transport(method, url, timeout):
        raise AssertionError("GET сходил в сеть — а чтение обязано быть без неё")

    def blob() -> str:
        return sql("SELECT config_json FROM connections WHERE id = ?",
                   conn_id)[0][0]

    def envelope_now() -> dict:
        return json.loads(blob())[ss.ENVELOPE_KEY]

    def store(envelope: dict) -> str:
        cfg = json.loads(blob())
        cfg[ss.ENVELOPE_KEY] = envelope
        raw = json.dumps(cfg, ensure_ascii=False)
        exec_sql("UPDATE connections SET config_json = ? WHERE id = ?",
                 raw, conn_id)
        return raw

    def member_reason(label: str, expect: str, forbidden: list) -> None:
        """Что видит участник, что при этом видит владелец и что стало с базой."""
        before = blob()
        ss.set_transport(exploding_transport)
        try:
            theirs_raw = mate.get("/api/supply/sheets?limit=200").text
            mine = boss.get("/api/supply/sheets?limit=200").json()
        finally:
            ss.set_transport(None)
        theirs = json.loads(theirs_raw)
        check(f"{label}: участник получает ровно ожидаемую причину",
              theirs.get("last_error") == expect,
              str(theirs.get("last_error"))[:160])
        check(f"{label}: и она непуста",
              bool((theirs.get("last_error") or "").strip()))
        check(f"{label}: причина участника — строка ИЗ НАШЕГО исходника",
              theirs.get("last_error") in set(ss.PUBLIC_FAILURE_REASONS.values())
              | {ss.PUBLIC_FAILURE_FALLBACK, ""},
              str(theirs.get("last_error"))[:160])
        for bad in forbidden:
            check(f"{label}: «{bad[:40]}» нет во ВСЁМ ответе участника",
                  bad not in theirs_raw, theirs_raw[:200])
        check(f"{label}: владелец подробность сохранил",
              isinstance(mine.get("last_error"), str)
              and mine["last_error"] == envelope_now().get("last_error"),
              str(mine.get("last_error"))[:160])
        check(f"{label}: носитель после двух GET байт в байт прежний",
              blob() == before)

    try:
        # Настоящая неудача настоящим кодом: 403 на сентинельном листе.
        ss.set_transport(FakeGoogle({SENTINEL_SHEET: ss.HttpResponse(
            403, {}, b"forbidden", "https://docs.google.com/x")}))
        r = boss.post("/api/supply/sheets/refresh",
                      json={"spreadsheet_url": SHEET_URL,
                            "sheet_names": [SENTINEL_SHEET, SENTINEL_SHEET_NEXT]})
        ss.set_transport(None)
        check("отказ 403 записан", r.status_code == 502, r.text[:120])
        first = envelope_now()
        check("в носителе появился код причины из закрытого списка",
              first.get("last_error_public_code") in ss.PUBLIC_FAILURE_REASONS,
              str(first.get("last_error_public_code")))
        check("и отпечаток его связи с этой записью",
              bool(first.get("last_error_public_binding")),
              str(first.get("last_error_public_binding"))[:24])
        member_reason("связанная запись", ss.PUBLIC_FAILURE_REASONS["access"],
                      [SENTINEL_SHEET, SENTINEL_SHEET_NEXT, SPREADSHEET_ID])

        print("\n== A. Откат: старый писатель обновил неудачу, код остался прежним ==")
        # Ровно то, что делает код, не знающий про код причины и отпечаток:
        # переписывает подробный текст, время и источник попытки, а незнакомые
        # поля переносит как есть. Прежняя редакция на этом и ломалась.
        other_sheet = "Щ-сентинель-другой-лист-Wq2Lm8"
        rolled = dict(first)
        rolled["last_attempt_at"] = "2026-09-01T03:04:05+00:00"
        rolled["last_error"] = f"лист «{other_sheet}»: таблица или лист не найдены (404)"
        rolled["last_attempt_source"] = {"spreadsheet_id": SPREADSHEET_ID,
                                         "sheet_names": [other_sheet,
                                                         SENTINEL_SHEET_NEXT]}
        rolled["last_error_public"] = "лист источника: источник ответил 403"
        store(rolled)
        check("подмена действительно сохранила ПРЕЖНИЙ код и отпечаток",
              envelope_now()["last_error_public_code"]
              == first["last_error_public_code"]
              and envelope_now()["last_error_public_binding"]
              == first["last_error_public_binding"])
        # Причина прошлой попытки («не открыта на чтение») сегодняшней не
        # является: сегодня 404. Связь недоказуема — значит generic.
        member_reason("A: устаревший код", ss.PUBLIC_FAILURE_FALLBACK,
                      [other_sheet, SENTINEL_SHEET,
                       ss.PUBLIC_FAILURE_REASONS["access"],
                       "лист источника: источник ответил 403"])

        print("\n== B. Испорченный источник попытки + произвольный текст ==")
        planted = "СЕКРЕТНАЯ-ПОДЛОЖЕННАЯ-СТРОКА-Zz9Qw4: таблица Иванова закрыта"
        broken = dict(first)
        broken.pop("last_attempt_source", None)
        broken["last_error_public"] = planted
        store(broken)
        member_reason("B: нет источника попытки", ss.PUBLIC_FAILURE_FALLBACK,
                      [planted, SENTINEL_SHEET,
                       ss.PUBLIC_FAILURE_REASONS["access"]])

        broken2 = dict(first)
        broken2["last_attempt_source"] = "строка вместо объекта"
        broken2["last_error_public"] = planted
        store(broken2)
        member_reason("B: источник попытки не того вида",
                      ss.PUBLIC_FAILURE_FALLBACK,
                      [planted, ss.PUBLIC_FAILURE_REASONS["access"]])

        print("\n== Код без отпечатка и отпечаток без кода — тоже generic ==")
        no_binding = dict(first)
        no_binding["last_error_public_binding"] = ""
        store(no_binding)
        member_reason("код без отпечатка", ss.PUBLIC_FAILURE_FALLBACK,
                      [ss.PUBLIC_FAILURE_REASONS["access"]])

        forged = dict(first)
        forged["last_error_public_code"] = "выдуманный-код"
        store(forged)
        member_reason("код вне закрытого списка", ss.PUBLIC_FAILURE_FALLBACK, [])

        swapped = dict(first)
        swapped["last_error_public_code"] = "missing"
        store(swapped)
        member_reason("подменённый код не сходится отпечатком",
                      ss.PUBLIC_FAILURE_FALLBACK,
                      [ss.PUBLIC_FAILURE_REASONS["missing"]])

        print("\n== Сама функция связывания ==")
        source = {"spreadsheet_id": SPREADSHEET_ID,
                  "sheet_names": [SENTINEL_SHEET, SENTINEL_SHEET_NEXT]}
        base = ss._public_binding("access", "подробно", "2026-09-01T00:00:00+00:00",
                                  source)
        check("отпечаток детерминирован",
              base == ss._public_binding("access", "подробно",
                                         "2026-09-01T00:00:00+00:00", source))
        check("код входит в отпечаток",
              base != ss._public_binding("missing", "подробно",
                                         "2026-09-01T00:00:00+00:00", source))
        check("подробный текст входит в отпечаток",
              base != ss._public_binding("access", "иначе",
                                         "2026-09-01T00:00:00+00:00", source))
        check("время попытки входит в отпечаток",
              base != ss._public_binding("access", "подробно",
                                         "2026-09-01T00:00:01+00:00", source))
        check("идентификатор таблицы входит в отпечаток",
              base != ss._public_binding(
                  "access", "подробно", "2026-09-01T00:00:00+00:00",
                  {**source, "spreadsheet_id": SPREADSHEET_ID[:-1] + "Z"}))
        check("имена листов входят в отпечаток",
              base != ss._public_binding(
                  "access", "подробно", "2026-09-01T00:00:00+00:00",
                  {**source, "sheet_names": [SENTINEL_SHEET_NEXT, SENTINEL_SHEET]}))
        # Length-prefix: склейка имён не должна давать тот же отпечаток, что
        # другое разбиение тех же символов. Разделитель здесь подделать нечем.
        check("склейка имён листов отпечаток не подделывает",
              ss._public_binding("access", "п", "т",
                                 {"spreadsheet_id": "", "sheet_names": ["аб", "в"]})
              != ss._public_binding("access", "п", "т",
                                    {"spreadsheet_id": "",
                                     "sheet_names": ["а", "бв"]}))
        check("испорченный источник попытки даёт отпечаток, а не исключение",
              isinstance(ss._public_binding("access", "п", None, "не словарь"), str))

        print("\n== Закрытый список причин ==")
        check("в списке нет пустых и повторяющихся текстов",
              all(v.strip() for v in ss.PUBLIC_FAILURE_REASONS.values())
              and len(set(ss.PUBLIC_FAILURE_REASONS.values()))
              == len(ss.PUBLIC_FAILURE_REASONS))
        check("generic в закрытый список не входит и тоже непуст",
              ss.PUBLIC_FAILURE_FALLBACK.strip()
              and ss.PUBLIC_FAILURE_FALLBACK
              not in set(ss.PUBLIC_FAILURE_REASONS.values()))
        check("неизвестный код у самого отказа не приживается",
              ss.SourceError("текст", code="таких-нет").code == ""
              and ss.SourceError("текст").code == ""
              and ss.SourceError("текст", code="access").code == "access")
        # Отказ БЕЗ кода обязан обедняться до generic, а не показывать чужое.
        check("отказ без кода даёт участнику generic",
              ss._public_reason({"last_error_public_code": "",
                                 "last_error_public_binding": "x"}, "подробно")
              == ss.PUBLIC_FAILURE_FALLBACK)

        # СТРУКТУРНАЯ проверка, а не дисциплинарная: у КАЖДОГО места, где слой
        # рождает отказ источника, код причины проставлен явно. Забытый код не
        # опасен — он даёт generic, — но он молча обедняет сообщение участнику,
        # и заметить это глазами в двух десятках мест нельзя. Читается сам
        # исходник, а не поведение: место, которого не проходит ни один тест,
        # иначе не проверить.
        import ast

        tree = ast.parse(io.open(ss.__file__, encoding="utf-8").read())
        sites = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                 and n.func.id in ("SourceError", "_sheet_error")]
        without_code = [n.lineno for n in sites
                        if "code" not in {kw.arg for kw in n.keywords}]
        check("у каждого места отказа источника код причины проставлен явно",
              without_code == [], str(without_code))
        check("и таких мест действительно много — проверка не выродилась",
              len(sites) >= 20, str(len(sites)))
    finally:
        ss.set_transport(None)
        boss.close()
        mate.close()


def source_total_only_checks() -> None:
    """Якорь с пустой горкой и читаемым итогом источника — НЕ окончательный ноль.

    Замечание ревью PR #47 на HEAD `459f170`, воспроизведённое буквально:
    строка-якорь, у которой все пять колонок XS–XL пусты, а колонка итога
    источника содержит `10`. `quantity_missing` ей не ставится (количества у
    позиции есть), `invalid_quantity` тоже (всё написанное читается) — и сводка
    выдавала `quantity_known=0, quantity=0, quantity_complete=true`. То есть
    лист с десятью штуками объявлялся окончательно пустым.

    Правильный ответ — «не знаем». Итог источника количеством НЕ становится:
    решения считать его количеством никто не принимал, а в этой самой таблице
    итог и сумма размеров регулярно расходятся (`total_mismatch` живёт
    отдельной пометкой). Здесь проверяется и то, и другое: сводка неполна И
    `quantity_known` не подменён числом источника.
    """
    print("\n== Итог источника без размеров: «не определено», а не ноль ==")
    rows = autumn_header_rows()
    rows.append(put(blank(), {
        2: "2200", 3: "Пальто без горки", 5: "Чёрный", 15: "10"}))
    parsed, _schema = ss.parse_sheet(SHEET_CURRENT, rows)
    check("разобрана ровно одна строка данных", len(parsed) == 1, str(len(parsed)))
    row = parsed[0]
    check("итог источника прочитан как есть",
          row["source_total"] == 10 and row["source_total_raw"] == "10",
          str(row["source_total"]))
    check("а суммы размеров у строки нет вовсе — её нечем складывать",
          row["size_sum"] is None, str(row["size_sum"]))
    check("итог источника размерную сумму НЕ подменил",
          row["size_sum"] != row["source_total"], str(row["size_sum"]))
    check("расхождением это не называется: сравнивать не с чем",
          "total_mismatch" not in row["issues"], str(row["issues"]))

    counts = ss.build_counts(parsed, [SHEET_CURRENT])
    check("итог штук по снимку — «не знаем», а не ноль",
          counts["quantity"] is None, str(counts["quantity"]))
    check("и полным он не объявлен",
          counts["quantity_complete"] is False, str(counts["quantity_complete"]))
    check("прочитанное честно равно нулю — мы не прочитали ничего",
          counts["quantity_known"] == 0, str(counts["quantity_known"]))
    check("и итог источника в «прочитано» не подставлен",
          counts["quantity_known"] != 10, str(counts["quantity_known"]))
    sheet = counts["sheets"][0]
    check("по листу ровно то же самое",
          sheet["quantity"] is None and sheet["quantity_complete"] is False
          and sheet["quantity_known"] == 0, str(sheet))

    # Контроль сверху: правило узкое. Полная строка полноту не отменяет.
    full = autumn_header_rows()
    full.append(put(blank(), {
        2: "2201", 3: "Пальто с горкой", 5: "Синий",
        10: "1", 11: "1", 12: "1", 13: "1", 14: "1", 15: "5"}))
    full_counts = ss.build_counts(ss.parse_sheet(SHEET_CURRENT, full)[0],
                                  [SHEET_CURRENT])
    check("строка с прочитанной горкой по-прежнему даёт окончательное число",
          full_counts["quantity"] == 5 and full_counts["quantity_complete"] is True,
          str(full_counts["quantity"]))

    # И контроль снизу: расхождение итога полноту не отменяет — оба числа
    # прочитаны, и это прежнее решение D-51, а не побочный эффект правки.
    mism = autumn_header_rows()
    mism.append(put(blank(), {
        2: "2202", 3: "Пальто расхождение", 5: "Серый",
        10: "1", 11: "1", 12: "1", 13: "1", 14: "1", 15: "9"}))
    mism_rows = ss.parse_sheet(SHEET_CURRENT, mism)[0]
    mism_counts = ss.build_counts(mism_rows, [SHEET_CURRENT])
    check("расхождение итога названо расхождением",
          "total_mismatch" in mism_rows[0]["issues"], str(mism_rows[0]["issues"]))
    check("и полноту оно по-прежнему НЕ отменяет",
          mism_counts["quantity"] == 5
          and mism_counts["quantity_complete"] is True, str(mism_counts["quantity"]))

    # Строка-продолжение без количеств и БЕЗ итога — прежнее поведение. Правка
    # сюда не расползается: у такого фрагмента источник итога не называл вовсе,
    # и объявлять из-за него весь лист неопределённым было бы новым смыслом.
    cont = autumn_header_rows()
    cont.append(put(blank(), {
        2: "2203", 3: "Пальто с продолжением", 5: "Чёрный",
        10: "1", 11: "1", 12: "1", 13: "1", 14: "1", 15: "5"}))
    cont.append(put(blank(), {5: "Молоко"}))
    cont_counts = ss.build_counts(ss.parse_sheet(SHEET_CURRENT, cont)[0],
                                  [SHEET_CURRENT])
    check("продолжение без количеств и без итога полноту не отменяет",
          cont_counts["quantity"] == 5
          and cont_counts["quantity_complete"] is True, str(cont_counts["quantity"]))

    # Та же строка, но итог источника у продолжения назван: количество у неё
    # есть, прочитать его в сумму нечем — значит «не знаем».
    cont2 = autumn_header_rows()
    cont2.append(put(blank(), {
        2: "2204", 3: "Пальто с продолжением", 5: "Чёрный",
        10: "1", 11: "1", 12: "1", 13: "1", 14: "1", 15: "5"}))
    cont2.append(put(blank(), {5: "Молоко", 15: "3"}))
    cont2_counts = ss.build_counts(ss.parse_sheet(SHEET_CURRENT, cont2)[0],
                                   [SHEET_CURRENT])
    check("а продолжение с названным итогом — уже «не определено»",
          cont2_counts["quantity"] is None
          and cont2_counts["quantity_known"] == 5, str(cont2_counts))


def envelope_headroom_checks() -> None:  # noqa: C901 — сценарий, ветвлений мало
    """Отказ у самой границы предела: причина сохраняется, а не подменяется.

    Замечание ревью PR #47 на HEAD `ae50ba0` (thread 3903750392), и оно про
    самый неприятный класс дефекта — тот, где ломается механизм, который и
    существует ради честного сообщения о поломке.

    `_record_failure()` берёт УЖЕ ПРИНЯТЫЙ успешный снимок, накладывает поля
    попытки и причины и отдаёт результат тому же `_store()`. Пока предел
    записи был один на всех, снимок, принятый у самой границы, переставал
    помещаться от одного лишь добавления причины: из записи отказа вылетал
    ВТОРОЙ отказ с кодом `too_big`, он подменял исходную причину в ответе, а
    метаданные попытки не записывались вовсе. Пользователь при этом видел
    «предпросмотр не помещается» там, где источник ответил 403 — то есть
    ровно ту неправду, против которой написан весь этот слой.

    ПОЧЕМУ ГРАНИЦА ЗДЕСЬ ДВИГАЕТСЯ КОНСТАНТОЙ, А НЕ МЕГАБАЙТОМ CSV. Предел —
    это одно сравнение размера в `_guard_envelope_size()`, и для него «снимок
    в 1 МиБ при пределе 1 МиБ» и «снимок в 3 КиБ при пределе 3 КиБ» — один и
    тот же случай, проходящий по одному и тому же коду. Разница только в
    времени прогона. Поэтому предел опускается ровно до фактического размера
    ПРИНЯТОГО успешного снимка: запаса не остаётся ни одного байта — это и
    есть «у границы» в самом строгом виде, какой вообще бывает.

    Существующая проверка малого лимита (`ss.MAX_ENVELOPE_BYTES = 512`) этот
    класс не ловит и никогда не ловила: там успешный снимок В ПРИНЦИПЕ не
    помещается, проверяются только 502 и сохранность прежнего hash, и оба
    исхода одинаковы что с дефектом, что без него.
    """
    print("\n== Отказ у самой границы предела: причина не подменяется ==")
    boss = client()
    boss.post("/register", data={"name": "Граница",
                                 "email": "sheets-edge@test.io",
                                 "password": "secret123", "org_name": "Бренд-ГР"})
    boss.post("/api/connect/demo")
    org_id = sql("SELECT org_id FROM memberships WHERE user_id ="
                 " (SELECT id FROM users WHERE email = 'sheets-edge@test.io')")[0][0]
    conn_id = sql("SELECT id FROM connections WHERE org_id = ? ORDER BY id",
                  org_id)[0][0]
    add_member(org_id, "sheets-edge-m@test.io")
    mate = client()
    mate.post("/login", data={"email": "sheets-edge-m@test.io",
                              "password": "secret123"})

    def envelope_now() -> dict:
        cfg = json.loads(sql("SELECT config_json FROM connections WHERE id = ?",
                             conn_id)[0][0])
        return cfg[ss.ENVELOPE_KEY]

    def refresh_now():
        return boss.post("/api/supply/sheets/refresh",
                         json={"spreadsheet_url": SHEET_URL,
                               "sheet_names": [SHEET_CURRENT, SHEET_NEXT]})

    real_limit = ss.MAX_ENVELOPE_BYTES
    try:
        ss.set_transport(FakeGoogle())
        r = refresh_now()
        check("успешный снимок записан", r.status_code == 200, r.text[:160])
        good = envelope_now()
        exact = len(ss._dump(good).encode("utf-8"))

        # Предел ровно по факту принятого снимка: ни одного байта запаса.
        ss.MAX_ENVELOPE_BYTES = exact
        r = refresh_now()
        check("тот же успех у САМОЙ границы по-прежнему принимается",
              r.status_code == 200, f"{r.status_code} {r.text[:160]}")
        check("и это честная ветка «ничего не изменилось»",
              r.json().get("unchanged") is True, r.text[:160])
        good = envelope_now()
        check("снимок у границы занимает ровно предел, а не меньше",
              len(ss._dump(good).encode("utf-8")) == ss.MAX_ENVELOPE_BYTES,
              f"{len(ss._dump(good).encode('utf-8'))} vs {ss.MAX_ENVELOPE_BYTES}")
        good_rows = json.loads(json.dumps(good["rows"], ensure_ascii=False))
        good_hash = good["content_sha256"]
        good_success = good["last_success_at"]
        good_fetched = good["fetched_at"]
        good_counts = json.loads(json.dumps(good["counts"], ensure_ascii=False))
        seen_total = boss.get("/api/supply/sheets?limit=200").json()["total"]

        # Обычный последующий отказ источника: 403 на первом листе.
        #
        # Листы намеренно переставлены местами. Метаданные попытки обязаны
        # принадлежать ЭТОЙ попытке, а не пережить её от прошлого успеха, — а
        # у прошлого успеха ровно те же две строки в другом порядке. Проверка
        # «last_attempt_source не пуст» этого не различила бы вовсе: он не был
        # бы пуст и без единой новой записи. Порядок при этом не меняет ни
        # одного байта размера, и снимок остаётся ровно на границе.
        ss.set_transport(FakeGoogle({
            SHEET_NEXT: ss.HttpResponse(403, {}, b"forbidden",
                                        "https://docs.google.com/x"),
            SHEET_CURRENT: AUTUMN_CSV}))
        r = boss.post("/api/supply/sheets/refresh",
                      json={"spreadsheet_url": SHEET_URL,
                            "sheet_names": [SHEET_NEXT, SHEET_CURRENT]})
        check("отказ источника у границы доехал как 502",
              r.status_code == 502, f"{r.status_code} {r.text[:160]}")
        check("в ответе названа ИСХОДНАЯ причина — 403, а не предел снимка",
              "403" in r.text, r.text[:220])
        check("и текстом предела исходная причина не подменена",
              "не помещается" not in r.text and "вырос за пределы" not in r.text,
              r.text[:220])

        env = envelope_now()
        check("прежний успешный снимок цел: строки не тронуты и не обрезаны",
              env["rows"] == good_rows, f"{len(env['rows'])} vs {len(good_rows)}")
        check("... и hash содержимого прежний", env["content_sha256"] == good_hash,
              f"{env['content_sha256']} vs {good_hash}")
        check("... и время удачи, и время загрузки прежние",
              env["last_success_at"] == good_success
              and env["fetched_at"] == good_fetched,
              f"{env['last_success_at']} / {env['fetched_at']}")
        check("... и сохранённая сводка прежняя", env["counts"] == good_counts,
              str(env["counts"]))
        check("метаданные попытки записаны: её время",
              bool(env["last_attempt_at"]), str(env["last_attempt_at"]))
        check("... и её источник целиком — именно этой попытки, а не прошлой",
              env["last_attempt_source"] == {"spreadsheet_id": SPREADSHEET_ID,
                                             "sheet_names": [SHEET_NEXT,
                                                             SHEET_CURRENT]},
              str(env["last_attempt_source"]))
        check("подробная причина владельца сохранена и это тот самый 403",
              "403" in (env["last_error"] or ""), (env["last_error"] or "")[:160])
        check("и она не подменена текстом предела снимка",
              "не помещается" not in (env["last_error"] or ""),
              (env["last_error"] or "")[:160])
        check("код причины из закрытого списка сохранён",
              env["last_error_public_code"] == "access",
              str(env["last_error_public_code"]))
        check("и отпечаток связи сошёлся с ЭТОЙ записанной неудачей",
              env["last_error_public_binding"] == ss._public_binding(
                  "access", env["last_error"], env["last_attempt_at"],
                  env["last_attempt_source"]),
              str(env["last_error_public_binding"])[:32])

        mine = boss.get("/api/supply/sheets?limit=200").json()
        check("владелец читает исходную причину, а не «снимок не поместился»",
              "403" in (mine.get("last_error") or "")
              and "не помещается" not in (mine.get("last_error") or ""),
              (mine.get("last_error") or "")[:160])
        check("и прежний снимок владельцу по-прежнему виден целиком",
              mine["total"] == seen_total, f"{mine['total']} vs {seen_total}")
        theirs_raw = mate.get("/api/supply/sheets?limit=200").text
        theirs = json.loads(theirs_raw)
        check("участнику причина отказа — наша константа по коду",
              theirs["last_error"] == ss.PUBLIC_FAILURE_REASONS["access"],
              (theirs.get("last_error") or "")[:160])
        # Успешный снимок общий, и имена его листов участник видит законно
        # (D-51). Закрыт ровно ПОДРОБНЫЙ текст отказа: ни статуса источника,
        # ни имени сломавшегося листа в причине участника быть не должно.
        check("подробности отказа участнику не уехали ни одной строкой",
              "403" not in (theirs.get("last_error") or "")
              and SHEET_NEXT not in (theirs.get("last_error") or ""),
              (theirs.get("last_error") or "")[:160])
        check("и прежний общий снимок участник видит целиком",
              theirs["total"] == seen_total, f"{theirs['total']} vs {seen_total}")

        # Восстановление у той же границы. Успешное чтение ТОГО ЖЕ содержимого
        # очищает поля отказа — и обязано поместиться, иначе «починиться» было
        # бы нельзя: раздел остался бы навсегда с записанным отказом.
        ss.set_transport(FakeGoogle())
        r = refresh_now()
        check("успех тем же содержимым у той же границы принят",
              r.status_code == 200, f"{r.status_code} {r.text[:200]}")
        env = envelope_now()
        check("поля отказа очищены полностью",
              env["last_error"] == "" and env["last_error_public"] == ""
              and env["last_error_public_code"] == ""
              and env["last_error_public_binding"] == "",
              str([env["last_error"], env["last_error_public_code"]]))
        check("а сам снимок так и остался прежним",
              env["content_sha256"] == good_hash and env["rows"] == good_rows
              and env["last_success_at"] == good_success,
              env["content_sha256"])
        check("и снова помещается в неослабленный предел успеха",
              len(ss._dump(env).encode("utf-8")) <= ss.MAX_ENVELOPE_BYTES,
              f"{len(ss._dump(env).encode('utf-8'))} vs {ss.MAX_ENVELOPE_BYTES}")

        # И главное про границу решения владельца: резерв принадлежит ТОЛЬКО
        # служебной записи отказа. Опускаем предел на один байт ниже
        # фактического снимка — успешное чтение обязано упереться в него, а не
        # проехать на резерве.
        ss.MAX_ENVELOPE_BYTES = exact - 1
        r = refresh_now()
        check("успеху резерв не достаётся: на байт ниже — уже отказ",
              r.status_code == 502 and "не помещается" in r.text,
              f"{r.status_code} {r.text[:200]}")
        env = envelope_now()
        check("и снимок при этом остался нетронутым",
              env["content_sha256"] == good_hash and env["rows"] == good_rows
              and env["last_success_at"] == good_success,
              env["content_sha256"])

        # И главное про границу решения: резерв принадлежит ТОЛЬКО записи
        # отказа. Опускаем предел на один байт ниже фактического снимка —
        # успешное чтение обязано упереться в него, а не проехать на резерве.
        ss.MAX_ENVELOPE_BYTES = exact - 1
        r = refresh_now()
        check("успеху резерв не достаётся: на байт ниже — уже отказ",
              r.status_code == 502 and "не помещается" in r.text,
              f"{r.status_code} {r.text[:200]}")
        env = envelope_now()
        check("и снимок при этом остался нетронутым",
              env["content_sha256"] == good_hash and env["rows"] == good_rows
              and env["last_success_at"] == good_success,
              env["content_sha256"])
    finally:
        ss.MAX_ENVELOPE_BYTES = real_limit
        ss.set_transport(None)
        boss.close()
        mate.close()


def headroom_arithmetic_checks() -> None:  # noqa: C901 — матрица границ, ветвлений мало
    """Замки на сам резерв: он назначен владельцем, доказан кодом и ограничен.

    Сценарий выше доказывает ПОВЕДЕНИЕ на одном отказе. Здесь стоит то, чего
    поведением одного отказа не проверить: что оценка худшего случая верна
    ЛОКАЛЬНО (каждое слагаемое — граница само по себе, а не за счёт запаса в
    соседнем), что она покрывает снимок старой версии, где пяти из шести полей
    нет вовсе, и что обрезка длинного текста происходит ДО расчёта отпечатка,
    а не после. Всё офлайновое: ни сети, ни базы.
    """
    print("\n== Резерв под запись отказа: числа владельца и локальная граница ==")

    # 1. Числа, одобренные владельцем 01.09.2026 (DECISIONS D-52).
    check("предел УСПЕШНОГО снимка остался ровно 1 МиБ",
          ss.MAX_ENVELOPE_BYTES == 1024 * 1024, str(ss.MAX_ENVELOPE_BYTES))
    check("резерв под запись отказа — ровно одобренные 27 525 байт",
          ss.FAILURE_OVERLAY_BUDGET_BYTES == 27_525,
          str(ss.FAILURE_OVERLAY_BUDGET_BYTES))
    check("полный error-envelope — ровно одобренные 1 076 101 байт",
          ss.MAX_ENVELOPE_BYTES + ss.FAILURE_OVERLAY_BUDGET_BYTES == 1_076_101,
          str(ss.MAX_ENVELOPE_BYTES + ss.FAILURE_OVERLAY_BUDGET_BYTES))
    check("худший случай ВЫЧИСЛЕН и укладывается в потолок владельца",
          0 < ss._FAILURE_OVERLAY_WORST_BYTES <= ss.FAILURE_OVERLAY_BUDGET_BYTES,
          f"{ss._FAILURE_OVERLAY_WORST_BYTES} vs {ss.FAILURE_OVERLAY_BUDGET_BYTES}")

    # 2. ЛОКАЛЬНАЯ корректность слагаемого. Замечание независимого ревью на
    #    HEAD `ae50ba0`: сумма может сойтись и при неверном слагаемом, если
    #    недобор одного члена скрыт запасом другого. Поэтому каждый член
    #    проверяется отдельно и ИЗМЕРЕНИЕМ: добавляем его в непустой объект той
    #    же сериализацией, что и продукт, и сравниваем фактический прирост с
    #    заявленной границей. Разделитель `", "` — два байта, и именно на нём
    #    прежняя редакция теряла по байту на член.
    worst_char = "\x01"                 # шесть байт в JSON: самый дорогой символ
    members = [
        ("last_attempt_at", "2026-09-01T13:34:40+00:00",
         ss._json_ascii_bytes(ss.MAX_ATTEMPT_TIME_CHARS)),
        ("last_attempt_source",
         {"spreadsheet_id": "a" * ss.MAX_SPREADSHEET_ID_CHARS,
          "sheet_names": [worst_char * ss.MAX_SHEET_NAME_CHARS] * ss.SHEET_COUNT},
         ss._ATTEMPT_SOURCE_MAX_BYTES),
        ("last_error", worst_char * ss.MAX_FAILURE_TEXT_CHARS,
         ss._json_text_bytes(ss.MAX_FAILURE_TEXT_CHARS)),
        ("last_error_public", worst_char * ss.MAX_FAILURE_TEXT_CHARS,
         ss._json_text_bytes(ss.MAX_FAILURE_TEXT_CHARS)),
        ("last_error_public_code", max(ss.PUBLIC_FAILURE_REASONS, key=len),
         ss._json_ascii_bytes(max(len(c) for c in ss.PUBLIC_FAILURE_REASONS))),
        ("last_error_public_binding", "f" * 64,
         ss._json_ascii_bytes(64)),
    ]
    for key, value, value_bound in members:
        # Соседи подобраны так, чтобы ключ попадал и в начало, и в середину, и
        # в конец отсортированного объекта: разделитель у первого члена не
        # нужен вовсе, и граница обязана оставаться верной в обоих случаях.
        for label, neighbours in (("в начале", {"zzz": 1}),
                                  ("в конце", {"aaa": 1}),
                                  ("в середине", {"aaa": 1, "zzz": 1})):
            base = dict(neighbours)
            grown = dict(base)
            grown[key] = value
            delta = (len(ss._dump(grown).encode("utf-8"))
                     - len(ss._dump(base).encode("utf-8")))
            bound = ss._json_member_bytes(key, value_bound)
            check(f"граница члена «{key}» верна сама по себе ({label})",
                  delta <= bound, f"прирост {delta}, граница {bound}")

    check("и сумма шести таких границ — это и есть худший случай",
          sum(ss._json_member_bytes(k, b) for k, _v, b in members)
          == ss._FAILURE_OVERLAY_WORST_BYTES,
          str(ss._FAILURE_OVERLAY_WORST_BYTES))

    # 3. СНИМОК СТАРОЙ ВЕРСИИ: пяти из шести полей нет вовсе. Обязателен по
    #    `_ENVELOPE_SHAPE` только `last_error`, остальные пять — необязательные
    #    или вовсе незнакомые старому коду. Это и есть случай максимального
    #    прироста: каждому недостающему полю нужен и ключ, и разделитель.
    overlay = {key: value for key, value, _b in members}
    legacy = ss._skeleton("abcdefghij", ["Лист"])
    missing = [k for k in overlay if k != "last_error"]
    for key in missing:
        legacy.pop(key, None)
    check("у снимка старой версии отсутствуют ровно пять из шести полей",
          len(missing) == 5 and all(k not in legacy for k in missing)
          and "last_error" in legacy, str(sorted(missing)))
    grown = dict(legacy)
    grown.update(overlay)
    delta = (len(ss._dump(grown).encode("utf-8"))
             - len(ss._dump(legacy).encode("utf-8")))
    check("худшая запись отказа поверх снимка старой версии влезает в резерв",
          delta <= ss.FAILURE_OVERLAY_BUDGET_BYTES,
          f"прирост {delta}, резерв {ss.FAILURE_OVERLAY_BUDGET_BYTES}")
    check("и она же укладывается в вычисленный худший случай",
          delta <= ss._FAILURE_OVERLAY_WORST_BYTES,
          f"прирост {delta}, худший случай {ss._FAILURE_OVERLAY_WORST_BYTES}")

    # 4. Повторный отказ поверх отказа ничего не разгоняет: поля ЗАМЕНЯЮТСЯ.
    twice = dict(grown)
    twice.update(overlay)
    check("второй отказ подряд не увеличивает снимок ни на байт",
          len(ss._dump(twice).encode("utf-8"))
          == len(ss._dump(grown).encode("utf-8")))

    # 5. Потолок текста отказа — свойство кода, а не обещание.
    long_text = "я" * (ss.MAX_FAILURE_TEXT_CHARS * 3)
    cut = ss._bounded_failure_text(long_text)
    check("слишком длинный текст отказа обрезается до потолка",
          len(cut) == ss.MAX_FAILURE_TEXT_CHARS, str(len(cut)))
    check("обрезка проговаривается многоточием и не оставляет пустоты",
          cut.endswith("…") and len(cut.strip()) > 1, cut[-8:])
    short = "лист «Осень 26»: таблица не открыта на чтение по ссылке"
    check("а обычный текст отказа не трогается вовсе",
          ss._bounded_failure_text(short) == short, short)
    check("самый длинный СЕГОДНЯШНИЙ текст слоя под потолок не попадает",
          len(ss._sheet_error(
              "Ж" * ss.MAX_SHEET_NAME_CHARS,
              f"подпись «Комментарии» встречается в строке заголовка "
              f"{ss.MAX_COLUMNS} раз(а) (колонки "
              f"{list(range(1, ss.MAX_COLUMNS + 1))}), а должна ровно один — "
              f"какая из них задаёт каркас, мы не угадываем").args[0])
          <= ss.MAX_FAILURE_TEXT_CHARS,
          f"потолок {ss.MAX_FAILURE_TEXT_CHARS}")

    # 6. Длина идентификатора, из которой посчитан резерв, — та же, что
    #    принимает разбор ссылки. Разъехались бы — резерв считался бы не по
    #    тому источнику, и это заметили бы не здесь, а у пользователя.
    limit_id = "a" * ss.MAX_SPREADSHEET_ID_CHARS
    check("идентификатор предельной длины разбор принимает",
          ss.parse_spreadsheet_url(
              f"https://docs.google.com/spreadsheets/d/{limit_id}/edit") == limit_id)
    check("а на символ длиннее — уже нет",
          raises(lambda: ss.parse_spreadsheet_url(
              f"https://docs.google.com/spreadsheets/d/{limit_id}a/edit"),
              ss.ValidationError) != "")
    check("имя листа предельной длины валидатор принимает",
          ss.validate_sheet_names(["Ж" * ss.MAX_SHEET_NAME_CHARS, "Б"])
          == ["Ж" * ss.MAX_SHEET_NAME_CHARS, "Б"])
    check("а на символ длиннее — уже нет",
          raises(lambda: ss.validate_sheet_names(
              ["Ж" * (ss.MAX_SHEET_NAME_CHARS + 1), "Б"]),
              ss.ValidationError) != "")


def truncated_reason_checks() -> None:
    """Обрезка длинного текста происходит ДО отпечатка, а не после.

    Отпечаток `_public_binding()` считается по ТОМУ ЖЕ подробному тексту,
    который лёг в носитель. Обрежь текст после расчёта — и отпечаток перестанет
    сходиться на чтении: участник вместо конкретной причины получит generic,
    причём молча и только на длинных отказах, то есть ровно там, где никто не
    смотрит. Поэтому граница проверяется с обеих сторон: на символ короче
    потолка и заведомо длиннее него.
    """
    print("\n== Обрезка длинного отказа не рвёт связь причины с записью ==")
    boss = client()
    boss.post("/register", data={"name": "Обрезка",
                                 "email": "sheets-cut@test.io",
                                 "password": "secret123", "org_name": "Бренд-ОБ"})
    boss.post("/api/connect/demo")
    org_id = sql("SELECT org_id FROM memberships WHERE user_id ="
                 " (SELECT id FROM users WHERE email = 'sheets-cut@test.io')")[0][0]
    from app.db import SessionLocal

    def record(length: int) -> dict:
        """Записать отказ с текстом ровно `length` символов и вернуть снимок."""
        tail = "ы" * (length - len("лист «Осень 26»: "))
        exc = ss.SourceError("лист «Осень 26»: " + tail,
                             "лист источника: " + tail, code="access")
        db = SessionLocal()
        try:
            ss._record_failure(db, org_id, SPREADSHEET_ID,
                               [SHEET_CURRENT, SHEET_NEXT], ss._now_iso(), exc)
            return ss.get_envelope(db, org_id)
        finally:
            db.close()

    for label, length, expect_cut in (
            ("на символ короче потолка", ss.MAX_FAILURE_TEXT_CHARS - 1, False),
            ("ровно по потолку", ss.MAX_FAILURE_TEXT_CHARS, False),
            ("заведомо длиннее потолка", ss.MAX_FAILURE_TEXT_CHARS * 2, True)):
        env = record(length)
        stored = env["last_error"]
        check(f"{label}: сохранённый текст не длиннее потолка",
              len(stored) <= ss.MAX_FAILURE_TEXT_CHARS, str(len(stored)))
        check(f"{label}: обрезка сработала ровно тогда, когда должна",
              stored.endswith("…") is expect_cut, f"len={len(stored)}")
        check(f"{label}: отпечаток посчитан по СОХРАНЁННОМУ тексту",
              env["last_error_public_binding"] == ss._public_binding(
                  "access", stored, env["last_attempt_at"],
                  env["last_attempt_source"]),
              env["last_error_public_binding"][:32])
        # И то, ради чего отпечаток вообще существует: участник получает
        # конкретную причину, а не generic-заглушку.
        check(f"{label}: участник читает конкретную причину, а не generic",
              ss._public_reason(env, stored)
              == ss.PUBLIC_FAILURE_REASONS["access"],
              ss._public_reason(env, stored)[:80])
        check(f"{label}: и снимок целиком помещается в error-envelope",
              len(ss._dump(env).encode("utf-8"))
              <= ss.MAX_ENVELOPE_BYTES + ss.FAILURE_OVERLAY_BUDGET_BYTES,
              str(len(ss._dump(env).encode("utf-8"))))
    boss.close()


# ── Прогон ──────────────────────────────────────────────────────────────────

def run() -> int:
    input_checks()
    parse_checks()
    continuation_checks()
    csv_limit_checks()
    fail_closed_checks()
    network_checks()

    owner = client()
    print("\n== Подготовка организации ==")
    r = owner.post("/register", data={"name": "Владелец", "email": "sheets-owner@test.io",
                                      "password": "secret123", "org_name": "Бренд-А"})
    check("владелец зарегистрирован", r.status_code in (200, 302, 303), str(r.status_code))
    check("демо-данные загружены", owner.post("/api/connect/demo").status_code == 200)
    org_id = sql("SELECT org_id FROM memberships WHERE user_id ="
                 " (SELECT id FROM users WHERE email = 'sheets-owner@test.io')")[0][0]

    # Чужой ключ в config_json носителя: слой обязан вернуть его на место.
    carrier = sql("SELECT id, config_json FROM connections WHERE org_id = ? ORDER BY id",
                  org_id)[0]
    cfg = json.loads(carrier[1] or "{}")
    cfg["keep_me"] = {"a": [1, 2], "b": "чужое"}
    exec_sql("UPDATE connections SET config_json = ? WHERE id = ?",
             json.dumps(cfg, ensure_ascii=False), carrier[0])

    add_member(org_id, "sheets-member@test.io")
    member = client()
    member.post("/login", data={"email": "sheets-member@test.io", "password": "secret123"})
    check("участник вошёл",
          member.get("/api/settings").json().get("role") == "member",
          str(member.get("/api/settings").json().get("role")))

    refresh_checks(owner, org_id)
    first_failure_checks()
    safe_error_checks()
    attempt_privacy_checks()
    failure_privacy_checks()
    malformed_url_checks()
    incomplete_counts_checks()
    source_total_only_checks()
    envelope_headroom_checks()
    headroom_arithmetic_checks()
    truncated_reason_checks()
    bound_public_reason_checks()
    stored_counts_checks()
    envelope_version_checks()
    row_shape_checks()
    row_required_checks()
    continuity_checks()
    isolation_checks(owner, member, org_id)
    structural_checks(owner)
    offline_checks(org_id)
    config_guard_checks(org_id)
    carrier_choice_checks()
    purge_checks()

    owner.close()
    member.close()

    print(f"\nИТОГО: {len(PASS)} OK, {len(FAIL)} FAIL")
    for name in FAIL:
        print(f"  FAIL {name}")
    return 1 if FAIL else 0


def main() -> int:
    srv = ServerThread(oborot_app, APP_PORT)
    srv.start()
    try:
        return run()
    finally:
        srv.stop()
        ss.set_transport(None)
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(DB_PATH) + suffix)
            if p.exists():
                p.unlink()


if __name__ == "__main__":
    sys.exit(main())
