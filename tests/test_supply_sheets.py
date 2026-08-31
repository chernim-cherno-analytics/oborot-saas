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
    check("штуки суммируются только по прочитанным числам",
          autumn["quantity"] == 14 + 6 + 25 + 10 + 1 + 1, str(autumn["quantity"]))
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


# ── Прогон ──────────────────────────────────────────────────────────────────

def run() -> int:
    input_checks()
    parse_checks()
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
    envelope_version_checks()
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
