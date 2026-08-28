# -*- coding: utf-8 -*-
"""Сверка месяца продаж с эталоном первой таблицы — DATA-4/DATA-5.

ЧТО ЭТО. Набор на `tools/reconcile_sales.py`: операторский инструмент,
который читает месяц продаж из базы «Оборота» и сравнивает его с эталоном
первой таблицы. Проверяется ровно то, ради чего инструмент существует:
арифметика, разделение «сырое нетто» и «нетто интерфейса», знак возврата,
каноническая свёртка по базовому имени, отказ закрытым на битом и
недоступном эталоне, отказ закрытым на неизвестной организации и месяце,
доказанный режим «только чтение» и семантика кода возврата по расхождению.

ДАННЫЕ ТОЛЬКО СИНТЕТИЧЕСКИЕ. Репозиторий и Issue координации публичные,
поэтому здесь нет и не может быть ни одной настоящей суммы, ни одного
настоящего имени позиции и ни одной строки боевой базы. Все числа ниже
выдуманы и подобраны так, чтобы каждая проверка считалась в уме.

ЧЕСТНАЯ РАМКА RED/GREEN. Файла `tools/reconcile_sales.py` на BASE нет вовсе,
поэтому «набор красный до правки» здесь означает лишь «модуля не было» — это
слабая форма RED и выдавать её за доказательство нельзя. Способность ловить
доказывается отдельно и прямо внутри набора: блок «мутации» по одной вносит
в КОПИЮ исходника инструмента ровно те правки, ради которых написана каждая
группа проверок, и каждая мутация обязана быть поймана. Копия живёт в памяти
(модуль собирается `exec` из строки), продуктовый файл на диске при этом не
меняется; мутация, которой нужна база, работает на ВРЕМЕННОЙ копии базы.
Отдельно проверяется, что сама мутация не пустая: искомый фрагмент есть в
исходнике и ровно в ожидаемом количестве. Мутация, которая ничего не
поменяла, «поймала» бы что угодно.

ПОЧЕМУ БАЗА СТРОИТСЯ НАСТОЯЩИМИ МОДЕЛЯМИ. Фикстура создаётся через
`app.models`, а не рукописным `CREATE TABLE`: инструмент ходит в живую схему
`sales`/`products`/`sku_hidden`, и рукописная копия схемы разошлась бы с
продуктом молча. По той же причине «нетто интерфейса» сверяется не с
константой, а с настоящим `analytics_extra.build_revenue` — тем самым
расчётом, который видит пользователь на странице «Оборот».

Запуск из корня репозитория:  python tests/test_reconcile_sales.py
"""
import hashlib
import importlib.util  # noqa: F401 — оставлено намеренно: см. load_tool ниже
import io
import json
import math
import os
import shutil
import socket
import sqlite3
import sys
import tempfile
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "test_reconcile_sales.db"
SIDECARS = (Path(str(DB_PATH) + "-wal"), Path(str(DB_PATH) + "-shm"))

os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["SCHEDULER_ENABLED"] = "0"

for _p in (DB_PATH, *SIDECARS):
    if _p.exists():
        _p.unlink()

from app import analytics_extra  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.models import Org, Product, Sale, SkuHidden  # noqa: E402
from app.ms_sync import strip_size  # noqa: E402

TOOL_PATH = ROOT / "tools" / "reconcile_sales.py"

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, cond, detail: str = "") -> bool:
    """Проверка. cond — значение ИЛИ функция без аргументов.

    Исключение внутри — провал проверки, а не крах набора: набор обязан
    дописать канонический отчёт до конца, иначе раннер по D-42 не отличит
    его от оборвавшегося.
    """
    try:
        ok = bool(cond() if callable(cond) else cond)
    except Exception as exc:  # noqa: BLE001 — исключение и есть результат
        ok, detail = False, f"{detail} исключение: {exc!r}".strip()
    print(("  OK   " if ok else "  FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    (PASSED if ok else FAILED).append(name)
    return ok


def block(title: str, fn) -> None:
    print(f"\n{title}")
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        check(f"{title} — блок дошёл до конца", False, f"исключение: {exc!r}")


def raises(fn, exc_type) -> bool:
    """Вызов обязан бросить именно этот класс исключения."""
    try:
        fn()
    except exc_type:
        return True
    except Exception:  # noqa: BLE001 — другой класс исключения тоже провал
        return False
    return False


# ── загрузка инструмента (в том числе мутированного) ─────────────────────────

TOOL_SOURCE = TOOL_PATH.read_text(encoding="utf-8")


def load_tool(source: str | None = None, name: str = "reconcile_sales_under_test"):
    """Собрать модуль инструмента из исходника БЕЗ записи на диск.

    Мутации применяются к строке, а не к файлу: продуктовый файл в рабочем
    дереве обязан остаться нетронутым, и никакой `__pycache__` подмену не
    съедает, потому что импорта из файла здесь нет вовсе.
    """
    mod = types.ModuleType(name)
    mod.__file__ = str(TOOL_PATH)
    exec(compile(source if source is not None else TOOL_SOURCE,  # noqa: S102 — ровно ради мутаций
                 str(TOOL_PATH), "exec"), mod.__dict__)
    return mod


rc = load_tool()


# ── фикстура: синтетическая организация ──────────────────────────────────────
#
# Организация 1, май 2026. Все суммы выдуманы и круглые.
#
#   Худи (S), Худи (M)  — обычные позиции, две карточки одного базового имени;
#   Пробник             — products.excluded = 1 (исключена из аналитики);
#   Свеча               — в ручном архиве (sku_hidden);
#   Футболка            — обычная позиция с возвратом.
#
#   продажи:  Худи(S) 3 шт / 9 000 ₽, Худи(M) 2 шт / 6 000 ₽,
#             Пробник 5 шт / 1 000 ₽, Свеча 2 шт / 4 000 ₽,
#             Футболка 4 шт / 8 000 ₽
#   возвраты: Худи(S) 1 шт / 3 000 ₽, Футболка 1 шт / 2 000 ₽
#
#   валовые 28 000, возвраты 5 000, сырое нетто 23 000;
#   нетто интерфейса = 23 000 − 1 000 (исключённая) − 4 000 (архив) = 18 000.
#
# Дополнительно в базе лежат: строки за апрель и июнь той же позиции (границы
# месяца) и чужая организация 2 со своими продажами (изоляция).

MONTH = "2026-05"
EXPECT = {
    "gross_rev": 28000.0, "return_rev": 5000.0, "net_rev": 23000.0,
    "included_net_rev": 18000.0,
    "gross_qty": 16.0, "return_qty": 2.0, "net_qty": 14.0, "included_net_qty": 7.0,
}

ORG_ID = 1
OTHER_ORG_ID = 2


def build_fixture() -> int:
    """Создать базу настоящими моделями и вернуть выручку страницы «Оборот»."""
    Base.metadata.create_all(engine)
    db = SessionLocal()

    org = Org(id=ORG_ID, name="Синтетическая организация")
    other = Org(id=OTHER_ORG_ID, name="Чужая организация")
    db.add_all([org, other])
    db.flush()

    hoodie_s = Product(org_id=ORG_ID, ext_id="p-1", base_name="Худи", size="S")
    hoodie_m = Product(org_id=ORG_ID, ext_id="p-2", base_name="Худи", size="M")
    tester = Product(org_id=ORG_ID, ext_id="p-3", base_name="Пробник", size="",
                     excluded=True)
    candle = Product(org_id=ORG_ID, ext_id="p-4", base_name="Свеча", size="")
    tee = Product(org_id=ORG_ID, ext_id="p-5", base_name="Футболка", size="")
    alien = Product(org_id=OTHER_ORG_ID, ext_id="p-6", base_name="Худи", size="S")
    db.add_all([hoodie_s, hoodie_m, tester, candle, tee, alien])
    db.flush()

    db.add(SkuHidden(org_id=ORG_ID, base_name="Свеча"))

    rows = [
        (hoodie_s, "2026-05-02", 3, 9000.0, False),
        (hoodie_m, "2026-05-03", 2, 6000.0, False),
        (hoodie_s, "2026-05-10", 1, 3000.0, True),
        (tester,   "2026-05-05", 5, 1000.0, False),
        (candle,   "2026-05-06", 2, 4000.0, False),
        (tee,      "2026-05-07", 4, 8000.0, False),
        (tee,      "2026-05-08", 1, 2000.0, True),
        # Границы месяца: не должны попасть в май ни одной копейкой.
        (hoodie_s, "2026-04-30", 10, 30000.0, False),
        (hoodie_s, "2026-06-01", 10, 30000.0, False),
    ]
    for product, day, qty, revenue, is_return in rows:
        db.add(Sale(org_id=ORG_ID, product_id=product.id, date=day,
                    qty=float(qty), revenue=revenue, is_return=is_return))
    # Чужая организация: её майские продажи не имеют права попасть в отчёт.
    db.add(Sale(org_id=OTHER_ORG_ID, product_id=alien.id, date="2026-05-04",
                qty=7.0, revenue=77000.0, is_return=False))
    db.commit()

    ui = analytics_extra.build_revenue(db, ORG_ID, "2026-05-01", "2026-05-31")
    db.close()
    return int(ui["total_rev"])


def settle_fixture() -> None:
    """Закрыть движок и вернуть журнал в обычный режим.

    Приложение ставит WAL на каждое соединение; для набора это лишний
    источник недетерминизма (файлы-спутники рядом с базой). Тесты режима
    «только чтение» ниже работают на предсказуемом файле, а поведение самого
    WAL проверяется отдельной проверкой на отдельной копии.
    """
    engine.dispose()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.commit()
    conn.close()
    for path in SIDECARS:
        if path.exists():
            path.unlink()


UI_TOTAL_REV = build_fixture()
settle_fixture()


def sha256(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


DB_DIGEST = sha256(DB_PATH)


# ── синтетические эталоны ────────────────────────────────────────────────────
#
# Имена намеренно с суффиксом размера: инструмент обязан свернуть их к
# базовому имени сам, как это делает первая таблица своим _canon_name.

REFERENCE_BY_MONTH = {
    "Худи (S)": {"2026-05": [3, 9000], "2026-04": [10, 30000]},
    "Худи (M)": {"2026-05": [1, 3000]},
    "Футболка": {"2026-05": [3, 6000]},
    "Пробник": {"2026-05": [5, 1000]},
    "Свеча": {"2026-05": [2, 4000]},
    # Позиция, которой у «Оборота» за этот месяц нет вовсе.
    "Ремень": {"2026-05": [1, 500]},
}
# Свёртка эталона: Худи 4 шт / 12 000, Футболка 3 / 6 000, Пробник 5 / 1 000,
# Свеча 2 / 4 000, Ремень 1 / 500 → нетто 23 500 ₽ при 15 шт.
REFERENCE_NET_REV = 23500.0
REFERENCE_NET_QTY = 15.0

# Явный формат: тот же месяц, но с разрезом на валовые и возвраты. Возвраты
# сходятся до копейки, а всё расхождение сидит в валовых — ровно тот рисунок,
# который классифицирует DATA-5. Числа синтетические.
REFERENCE_EXPLICIT = {
    "month": "2026-05",
    "bases": {
        "Худи (S)": {"net_qty": 3, "net_rev": 9000, "gross_rev": 9000, "return_rev": 0},
        "Худи (M)": {"net_qty": 1, "net_rev": 3000, "gross_rev": 6000, "return_rev": 3000},
        "Пробник": {"net_qty": 5, "net_rev": 1000, "gross_rev": 1000, "return_rev": 0},
        "Свеча": {"net_qty": 2, "net_rev": 4000, "gross_rev": 4000, "return_rev": 0},
        "Футболка": {"net_qty": 3, "net_rev": 6000, "gross_rev": 8000, "return_rev": 2000},
        "Ремень": {"net_qty": 1, "net_rev": 500, "gross_rev": 500, "return_rev": 0},
    },
}
# Тот же явный формат, но ровно совпадающий с базой: нетто 23 000, валовые
# 28 000, возвраты 5 000. Нужен, чтобы проверить нулевое расхождение и код
# возврата при нулевом пороге.
REFERENCE_MATCHING = {
    "month": "2026-05",
    "bases": {k: v for k, v in REFERENCE_EXPLICIT["bases"].items() if k != "Ремень"},
}

# Штатный ответ публичной ручки первой таблицы
# `GET /api/sales-monthly?month=YYYY-MM` (legacy/main.py::get_sales_monthly).
# Числа синтетические и подобраны так, чтобы воспроизвести рисунок DATA-5:
# возвраты сходятся с базой до копейки (5 000), а всё расхождение сидит в
# валовых (28 000 у «Оборота» против 28 500 здесь).
SALES_MONTHLY_ITEMS = [
    {"base": "Худи", "sale_rev": 15000, "sale_qty": 5, "ret_rev": 3000,
     "ret_qty": 1, "net": 12000, "sizes": []},
    {"base": "Футболка", "sale_rev": 8000, "sale_qty": 4, "ret_rev": 2000,
     "ret_qty": 1, "net": 6000, "sizes": []},
    {"base": "Свеча", "sale_rev": 4000, "sale_qty": 2, "ret_rev": 0,
     "ret_qty": 0, "net": 4000, "sizes": []},
    {"base": "Пробник", "sale_rev": 1000, "sale_qty": 5, "ret_rev": 0,
     "ret_qty": 0, "net": 1000, "sizes": []},
    {"base": "Ремень", "sale_rev": 500, "sale_qty": 1, "ret_rev": 0,
     "ret_qty": 0, "net": 500, "sizes": []},
]
SALES_MONTHLY_SUMMARY = {"sales": 28500, "returns": 5000, "net": 23500,
                         "sales_qty": 17, "returns_qty": 2}
SALES_MONTHLY = {
    "months": ["2026-07", "2026-05", "2026-04"],
    "month": "2026-05",
    "summary": dict(SALES_MONTHLY_SUMMARY),
    "items": [dict(item) for item in SALES_MONTHLY_ITEMS],
}

# Тот же месяц, но «Худи» приехало двумя позициями, схлопывающимися в одно
# каноническое имя. Итоги обязаны совпасть с предыдущим payload'ом до цифры:
# свёртка не имеет права ни потерять позицию, ни посчитать её дважды.
SALES_MONTHLY_FOLDED = {
    "months": ["2026-05"],
    "month": "2026-05",
    "summary": dict(SALES_MONTHLY_SUMMARY),
    "items": [
        {"base": "Худи (S)", "sale_rev": 9000, "sale_qty": 3, "ret_rev": 3000,
         "ret_qty": 1, "net": 6000, "sizes": []},
        {"base": "Худи (M)", "sale_rev": 6000, "sale_qty": 2, "ret_rev": 0,
         "ret_qty": 0, "net": 6000, "sizes": []},
    ] + [dict(item) for item in SALES_MONTHLY_ITEMS if item["base"] != "Худи"],
}


def sales_monthly(*, summary=None, items=None, **top) -> dict:
    """Копия штатного payload'а с точечной подменой — фикстура не мутируется."""
    payload = {
        "months": list(SALES_MONTHLY["months"]),
        "month": SALES_MONTHLY["month"],
        "summary": dict(SALES_MONTHLY_SUMMARY) if summary is None else summary,
        "items": [dict(item) for item in SALES_MONTHLY_ITEMS] if items is None else items,
    }
    payload.update(top)
    return payload


def sales_monthly_item(index: int = 0, **fields) -> list:
    """Список позиций с изменённой одной позицией."""
    items = [dict(item) for item in SALES_MONTHLY_ITEMS]
    items[index] = {**items[index], **fields}
    return items


def write_json(tmpdir: Path, name: str, payload) -> str:
    path = tmpdir / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(path)


def saas_month(month: str = MONTH, org_id: int = ORG_ID) -> dict:
    conn = rc.open_readonly(str(DB_PATH))
    try:
        return rc.load_saas_month(conn, org_id, month)
    finally:
        conn.close()


def report(reference=None, basis: str = "raw", top: int = 10) -> dict:
    ref = rc.parse_reference(REFERENCE_BY_MONTH if reference is None else reference, MONTH)
    return rc.compare(saas_month(), ref, basis=basis, top=top)


# ── локальный сервер эталона ─────────────────────────────────────────────────

class _RefHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def do_GET(self):  # noqa: N802 — имя задано базовым классом
        if self.path == "/ok":
            body = json.dumps(REFERENCE_BY_MONTH, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/notjson":
            body = b"<html>not json</html>"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def log_message(self, *args):  # noqa: A003 — глушим шум в отчёте
        pass


def free_port() -> int:
    """Порт, который точно никто не слушает — для проверки отказа сети."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


# ══ 1. Канонические имена ════════════════════════════════════════════════════

def test_canon():
    check("суффикс размера снимается", rc.canon_base("Худи (S)") == "Худи")
    check("без суффикса имя не меняется", rc.canon_base("Свеча") == "Свеча")
    check("пробелы по краям снимаются", rc.canon_base("  Худи (XL)  ") == "Худи")
    check("None не роняет", rc.canon_base(None) == "")
    check("скобки в середине не трогаются",
          rc.canon_base("Худи (лимит) синее") == "Худи (лимит) синее")

    # Копия регулярки в инструменте обязана вести себя как оригинал продукта.
    names = ["Худи (S)", "Худи (M)", "Свеча", "Футболка (XS)", "Носки (42-44)",
             "Платье (one size)", "Ремень", "Худи (лимит) синее", "  Юбка (L) "]
    check("канон инструмента совпадает с app.ms_sync.strip_size",
          all(rc.canon_base(n) == strip_size(n) for n in names),
          str([n for n in names if rc.canon_base(n) != strip_size(n)]))


# ══ 2. Границы месяца ════════════════════════════════════════════════════════

def test_month_bounds():
    check("май 2026", rc.month_bounds("2026-05") == ("2026-05-01", "2026-05-31"))
    check("февраль невисокосного", rc.month_bounds("2026-02") == ("2026-02-01", "2026-02-28"))
    check("февраль високосного", rc.month_bounds("2024-02") == ("2024-02-01", "2024-02-29"))
    for bad in ("2026-5", "2026-13", "2026-00", "", "май", "2026-05-01", "26-05", None):
        check(f"мусор отклонён: {bad!r}",
              raises(lambda b=bad: rc.month_bounds(b), rc.ReconcileError))


# ══ 3. Только чтение ═════════════════════════════════════════════════════════

def test_readonly():
    conn = rc.open_readonly(str(DB_PATH))
    try:
        check("PRAGMA query_only = 1",
              int(conn.execute("PRAGMA query_only").fetchone()[0]) == 1)
        check("INSERT отклонён",
              raises(lambda: conn.execute(
                  "INSERT INTO sku_hidden (org_id, base_name) VALUES (1, 'x')"),
                  sqlite3.Error))
        check("UPDATE отклонён",
              raises(lambda: conn.execute("UPDATE sales SET qty = 0"), sqlite3.Error))
        check("DELETE отклонён",
              raises(lambda: conn.execute("DELETE FROM sales"), sqlite3.Error))
        check("CREATE TABLE отклонён",
              raises(lambda: conn.execute("CREATE TABLE t (x)"), sqlite3.Error))
        check("SELECT работает",
              conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0] > 0)
    finally:
        conn.close()

    check("база не изменилась после чтения", sha256(DB_PATH) == DB_DIGEST)

    with tempfile.TemporaryDirectory() as tmp:
        missing = Path(tmp) / "нет-такой.db"
        check("несуществующая база — отказ закрытым",
              raises(lambda: rc.open_readonly(str(missing)), rc.ReconcileError))
        check("несуществующая база НЕ создана", not missing.exists())

        broken = Path(tmp) / "broken.db"
        broken.write_bytes("это не sqlite".encode("utf-8"))
        check("битый файл — отказ закрытым (а не пустой отчёт)",
              raises(lambda: rc.load_saas_month(rc.open_readonly(str(broken)), 1, MONTH),
                     rc.ReconcileError))

        # Третий слой защиты проверяется отдельно и на ЗАВЕДОМО пишущем
        # соединении: иначе о нём известно только то, что он не мешает.
        writable_path = Path(tmp) / "writable.db"
        shutil.copyfile(DB_PATH, writable_path)
        digest = sha256(writable_path)
        files_before = {p.name for p in Path(tmp).iterdir()}
        writable = sqlite3.connect(writable_path)
        try:
            check("пишущее соединение отвергается пробой",
                  raises(lambda: rc._assert_cannot_write(writable), rc.ReconcileError))
        finally:
            writable.close()
        check("проба ничего не записала", sha256(writable_path) == digest)
        check("проба не оставила файлов рядом",
              {p.name for p in Path(tmp).iterdir()} == files_before,
              str(sorted({p.name for p in Path(tmp).iterdir()} - files_before)))


def test_readonly_wal():
    """База в WAL: содержимое не меняется, а рядом появляются только спутники.

    Это не придирка: прод «Оборота» живёт в WAL, и SQLite при чтении такой
    базы создаёт рядом `-wal`/`-shm`. Умалчивать об этом нельзя — в этом
    случае «только чтение» означает «не меняет данные», а не «не касается
    каталога». Проверка фиксирует ровно эту границу.
    """
    with tempfile.TemporaryDirectory() as tmp:
        wal_db = Path(tmp) / "wal.db"
        shutil.copyfile(DB_PATH, wal_db)
        conn = sqlite3.connect(wal_db)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.commit()
        conn.close()
        for suffix in ("-wal", "-shm"):
            side = Path(str(wal_db) + suffix)
            if side.exists():
                side.unlink()
        digest = sha256(wal_db)

        ro = rc.open_readonly(str(wal_db))
        rows = ro.execute("SELECT COUNT(*) FROM sales").fetchone()[0]
        ro.close()

        check("WAL-база читается", rows > 0)
        check("содержимое WAL-базы не изменилось", sha256(wal_db) == digest)
        appeared = {p.name for p in Path(tmp).iterdir()} - {"wal.db"}
        check("рядом появились только служебные спутники SQLite",
              appeared <= {"wal.db-wal", "wal.db-shm"}, str(sorted(appeared)))


# ══ 4. Арифметика месяца ═════════════════════════════════════════════════════

def test_totals():
    data = saas_month()
    totals = data["totals"]
    for key, expected in EXPECT.items():
        check(f"итог {key} = {expected:g}", totals[key] == expected,
              f"получено {totals[key]!r}")
    check("сырое нетто = валовые − возвраты",
          totals["net_rev"] == totals["gross_rev"] - totals["return_rev"])
    check("нетто интерфейса = включённые валовые − включённые возвраты",
          totals["included_net_rev"]
          == totals["included_gross_rev"] - totals["included_return_rev"])

    bases = data["bases"]
    check("две карточки одного имени свёрнуты в одну позицию",
          set(bases) == {"Худи", "Пробник", "Свеча", "Футболка"}, str(sorted(bases)))
    check("Худи: сырое нетто 12 000", bases["Худи"]["net_rev"] == 12000.0)
    check("Худи: нетто интерфейса тоже 12 000",
          bases["Худи"]["included_net_rev"] == 12000.0)
    check("Пробник: сырое 1 000, интерфейс 0 (позиция исключена)",
          bases["Пробник"]["net_rev"] == 1000.0
          and bases["Пробник"]["included_net_rev"] == 0.0)
    check("Свеча: сырое 4 000, интерфейс 0 (ручной архив)",
          bases["Свеча"]["net_rev"] == 4000.0
          and bases["Свеча"]["included_net_rev"] == 0.0)
    check("Футболка: возврат вычтен, нетто 6 000", bases["Футболка"]["net_rev"] == 6000.0)
    check("ручной архив назван в отчёте", data["archived_bases"] == ["Свеча"])
    check("строк продаж за месяц ровно 7", data["rows"] == 7, str(data["rows"]))
    check("строк без своего товара нет", data["orphan_rows"] == 0)

    april = saas_month("2026-04")
    check("апрель считается отдельно", april["totals"]["gross_rev"] == 30000.0)
    check("границы месяца: май не забрал апрель и июнь",
          EXPECT["gross_rev"] == 28000.0 and april["totals"]["gross_rev"] == 30000.0)

    alien = saas_month(MONTH, OTHER_ORG_ID)
    check("чужая организация считается своими деньгами",
          alien["totals"]["gross_rev"] == 77000.0)
    check("чужие деньги не попали в отчёт организации 1",
          saas_month()["totals"]["gross_rev"] == 28000.0)


def test_ui_parity():
    """«Нетто интерфейса» обязано совпасть с тем, что считает страница «Оборот».

    Сверка идёт с настоящим `analytics_extra.build_revenue`, а не с числом в
    этом файле: инструмент должен повторять правило продукта, а не своё.
    """
    totals = saas_month()["totals"]
    check("нетто интерфейса = total_rev страницы «Оборот»",
          round(totals["included_net_rev"]) == UI_TOTAL_REV,
          f"инструмент {totals['included_net_rev']!r}, страница {UI_TOTAL_REV!r}")
    check("сырое нетто со страницей НЕ совпадает — и это не дефект",
          round(totals["net_rev"]) != UI_TOTAL_REV)


def test_fail_closed_inputs():
    check("неизвестная организация — отказ закрытым",
          raises(lambda: saas_month(MONTH, 999), rc.ReconcileError))
    check("месяц без единой строки продаж — отказ закрытым, а не нули",
          raises(lambda: saas_month("2026-07"), rc.ReconcileError))
    check("мусор вместо месяца — отказ закрытым",
          raises(lambda: saas_month("май"), rc.ReconcileError))


# ══ 5. Разбор эталона ════════════════════════════════════════════════════════

def test_reference_by_month():
    ref = rc.parse_reference(REFERENCE_BY_MONTH, MONTH)
    check("формат опознан как sales_by_month", ref["shape"] == "sales_by_month")
    check("Худи (S) и Худи (M) свёрнуты в Худи",
          ref["bases"]["Худи"]["net_rev"] == 12000.0
          and ref["bases"]["Худи"]["net_qty"] == 4.0)
    check("чужой месяц эталона не подмешан",
          ref["totals"]["net_rev"] == REFERENCE_NET_REV, str(ref["totals"]["net_rev"]))
    check("штуки эталона свёрнуты так же",
          ref["totals"]["net_qty"] == REFERENCE_NET_QTY)
    check("валовые и возвраты этому формату неизвестны — null, а не ноль",
          ref["totals"]["gross_rev"] is None and ref["totals"]["return_rev"] is None)
    check("месяца нет в эталоне — отказ закрытым",
          raises(lambda: rc.parse_reference(REFERENCE_BY_MONTH, "2026-09"), rc.ReconcileError))


def test_reference_malformed():
    bad_payloads = {
        "верхний уровень — список": [1, 2, 3],
        "верхний уровень — строка": "нет",
        "позиция не объект": {"Худи": [1, 2]},
        "месяц не месяц": {"Худи": {"май": [1, 2]}},
        "пара не пара": {"Худи": {"2026-05": [1]}},
        "пара не список": {"Худи": {"2026-05": {"qty": 1}}},
        "выручка строкой": {"Худи": {"2026-05": [1, "9000"]}},
        "количество строкой": {"Худи": {"2026-05": ["1", 9000]}},
        "булево вместо числа": {"Худи": {"2026-05": [True, 9000]}},
        "null вместо числа": {"Худи": {"2026-05": [1, None]}},
    }
    for title, payload in bad_payloads.items():
        check(f"битый эталон отклонён: {title}",
              raises(lambda p=payload: rc.parse_reference(p, MONTH), rc.ReconcileError))


def test_reference_explicit():
    ref = rc.parse_reference(REFERENCE_EXPLICIT, MONTH)
    check("формат опознан как explicit", ref["shape"] == "explicit")
    check("нетто эталона свёрнуто", ref["totals"]["net_rev"] == REFERENCE_NET_REV)
    check("валовые эталона известны", ref["totals"]["gross_rev"] == 28500.0,
          str(ref["totals"]["gross_rev"]))
    check("возвраты эталона известны", ref["totals"]["return_rev"] == 5000.0)

    other_month = dict(REFERENCE_EXPLICIT, month="2026-06")
    check("месяц эталона не тот — отказ закрытым",
          raises(lambda: rc.parse_reference(other_month, MONTH), rc.ReconcileError))
    check("пустой список позиций — отказ закрытым",
          raises(lambda: rc.parse_reference({"month": MONTH, "bases": {}}, MONTH),
                 rc.ReconcileError))
    check("bases не объект — отказ закрытым",
          raises(lambda: rc.parse_reference({"month": MONTH, "bases": []}, MONTH),
                 rc.ReconcileError))
    check("нет обязательного net_rev — отказ закрытым",
          raises(lambda: rc.parse_reference(
              {"month": MONTH, "bases": {"Худи": {"net_qty": 1}}}, MONTH), rc.ReconcileError))

    # Разрез, известный не у всех позиций, — это не разрез.
    partial = {"month": MONTH, "bases": {
        "Худи": {"net_qty": 1, "net_rev": 100, "gross_rev": 100, "return_rev": 0},
        "Свеча": {"net_qty": 1, "net_rev": 50},
    }}
    part = rc.parse_reference(partial, MONTH)
    check("частично известные валовые не суммируются в число",
          part["totals"]["gross_rev"] is None and part["totals"]["return_rev"] is None)
    check("нетто при этом остаётся известным", part["totals"]["net_rev"] == 150.0)


def test_reference_sales_monthly():
    """Штатный публичный payload первой таблицы разбирается напрямую."""
    ref = rc.parse_reference(SALES_MONTHLY, MONTH)
    check("формат опознан как sales_monthly", ref["shape"] == "sales_monthly")
    check("валовые эталона известны", ref["totals"]["gross_rev"] == 28500.0,
          str(ref["totals"]["gross_rev"]))
    check("возвраты эталона известны", ref["totals"]["return_rev"] == 5000.0)
    check("нетто эталона = валовые − возвраты", ref["totals"]["net_rev"] == 23500.0)
    check("штуки эталона нетто = 17 − 2", ref["totals"]["net_qty"] == 15.0)
    check("позиции разобраны все", set(ref["bases"]) ==
          {"Худи", "Футболка", "Свеча", "Пробник", "Ремень"}, str(sorted(ref["bases"])))
    check("у позиции известен весь разрез",
          ref["bases"]["Худи"] == {"net_qty": 4.0, "net_rev": 12000.0,
                                   "gross_rev": 15000.0, "return_rev": 3000.0},
          str(ref["bases"]["Худи"]))

    folded = rc.parse_reference(SALES_MONTHLY_FOLDED, MONTH)
    check("две позиции одного канонического имени свёрнуты в одну",
          set(folded["bases"]) == set(ref["bases"]), str(sorted(folded["bases"])))
    check("свёртка ничего не посчитала дважды и ничего не потеряла",
          folded["bases"] == ref["bases"] and folded["totals"] == ref["totals"])


def test_sales_monthly_month_guard():
    """Ручка молча подменяет месяц — это обязано быть отказом, а не сверкой."""
    check("ответ за другой месяц — отказ закрытым",
          raises(lambda: rc.parse_reference(sales_monthly(month="2026-07"), MONTH),
                 rc.ReconcileError))
    check("пустая база эталона (months=[], month=null) — отказ закрытым",
          raises(lambda: rc.parse_reference(
              {"months": [], "month": None,
               "summary": {"sales": 0, "returns": 0, "net": 0,
                           "sales_qty": 0, "returns_qty": 0},
               "items": []}, MONTH), rc.ReconcileError))
    check("month не месяц — отказ закрытым",
          raises(lambda: rc.parse_reference(sales_monthly(month="май"), MONTH),
                 rc.ReconcileError))
    check("запрошенного месяца нет в списке months — отказ закрытым",
          raises(lambda: rc.parse_reference(sales_monthly(months=["2026-07"]), MONTH),
                 rc.ReconcileError))
    check("months не список — отказ закрытым",
          raises(lambda: rc.parse_reference(sales_monthly(months="2026-05"), MONTH),
                 rc.ReconcileError))
    check("months с мусором — отказ закрытым",
          raises(lambda: rc.parse_reference(sales_monthly(months=["2026-05", 7]), MONTH),
                 rc.ReconcileError))
    payload = sales_monthly()
    payload.pop("months")
    check("без необязательного months разбор проходит",
          rc.parse_reference(payload, MONTH)["totals"]["net_rev"] == 23500.0)


def test_sales_monthly_items_guard():
    bad_items = {
        "items не список": sales_monthly(items={"Худи": 1}),
        "items пуст": sales_monthly(items=[]),
        "позиция не объект": sales_monthly(items=["Худи"]),
        "нет base": sales_monthly(items=[{"sale_rev": 1, "sale_qty": 1,
                                          "ret_rev": 0, "ret_qty": 0, "net": 1}]),
        "base не строка": sales_monthly(items=sales_monthly_item(0, base=42)),
        "base пуст": sales_monthly(items=sales_monthly_item(0, base="   ")),
        "выручка строкой": sales_monthly(items=sales_monthly_item(0, sale_rev="15000")),
        "количество булевым": sales_monthly(items=sales_monthly_item(0, sale_qty=True)),
        "возврат null": sales_monthly(items=sales_monthly_item(0, ret_rev=None)),
        "net противоречит sale_rev − ret_rev":
            sales_monthly(items=sales_monthly_item(0, net=999999)),
    }
    for title, payload in bad_items.items():
        check(f"битая позиция отклонена: {title}",
              raises(lambda p=payload: rc.parse_reference(p, MONTH), rc.ReconcileError))

    for field in ("sale_rev", "sale_qty", "ret_rev", "ret_qty", "net"):
        items = [dict(item) for item in SALES_MONTHLY_ITEMS]
        items[0].pop(field)
        check(f"позиция без поля {field!r} отклонена",
              raises(lambda p=sales_monthly(items=items): rc.parse_reference(p, MONTH),
                     rc.ReconcileError))


def test_sales_monthly_summary_guard():
    """summary считается отдельно от items и потому проверяется ими."""
    check("summary не объект — отказ закрытым",
          raises(lambda: rc.parse_reference(sales_monthly(summary=[]), MONTH),
                 rc.ReconcileError))
    for field in ("sales", "returns", "net", "sales_qty", "returns_qty"):
        summary = dict(SALES_MONTHLY_SUMMARY)
        summary.pop(field)
        check(f"summary без поля {field!r} — отказ закрытым",
              raises(lambda p=sales_monthly(summary=summary): rc.parse_reference(p, MONTH),
                     rc.ReconcileError))
        contradiction = dict(SALES_MONTHLY_SUMMARY)
        contradiction[field] = contradiction[field] + 500
        check(f"summary.{field} расходится с items на 500 — отказ закрытым",
              raises(lambda p=sales_monthly(summary=contradiction): rc.parse_reference(p, MONTH),
                     rc.ReconcileError))

    money_off = dict(SALES_MONTHLY_SUMMARY, sales=28500.02)
    check("расхождение в две копейки уже не прощается",
          raises(lambda: rc.parse_reference(sales_monthly(summary=money_off), MONTH),
                 rc.ReconcileError))
    qty_off = dict(SALES_MONTHLY_SUMMARY, sales_qty=17.001)
    check("расхождение по штукам не прощается",
          raises(lambda: rc.parse_reference(sales_monthly(summary=qty_off), MONTH),
                 rc.ReconcileError))
    check("summary строкой — отказ закрытым",
          raises(lambda: rc.parse_reference(
              sales_monthly(summary=dict(SALES_MONTHLY_SUMMARY, sales="28500")), MONTH),
              rc.ReconcileError))

    noise = dict(SALES_MONTHLY_SUMMARY, sales=28500.0 + 1e-9, net=23500.0 - 1e-9)
    check("шум сложения float внутри допуска не роняет разбор",
          rc.parse_reference(sales_monthly(summary=noise), MONTH)["totals"]["net_rev"]
          == 23500.0)


def test_tolerance_cannot_hide():
    """Допуск обязан оставаться узким: это сторож, а не лазейка.

    Регрессия на найденный ревью дефект (discussion_r3877349893 и
    r3877357949): прежняя ОТНОСИТЕЛЬНАЯ добавка росла вместе с оборотом и на
    25 млн прощала две копейки, на миллиарде — полтинник, хотя документация
    обещала «не больше копейки». Теперь допуск абсолютный и ограничен сверху.
    """
    check("денежный потолок не шире копейки", rc.MONEY_TOLERANCE <= 0.01,
          str(rc.MONEY_TOLERANCE))
    check("потолок по штукам не шире 1e-6", rc.QTY_TOLERANCE <= 1e-6, str(rc.QTY_TOLERANCE))
    check("относительного допуска больше нет вовсе",
          not hasattr(rc, "RELATIVE_TOLERANCE"))

    # Именно тот масштаб и то расхождение, которые ревью назвало поимённо.
    check("25 000 000 против 25 000 000.02 — расхождение, а не совпадение",
          rc._agrees(25_000_000.02, 25_000_000.0, rc.MONEY_TOLERANCE) is False)
    check("на 25 млн допуск меньше двух копеек",
          rc._tolerance(25_000_000.0, 25_000_000.02, rc.MONEY_TOLERANCE) < 0.02,
          str(rc._tolerance(25_000_000.0, 25_000_000.02, rc.MONEY_TOLERANCE)))
    check("на миллиарде полтинник не прощается",
          rc._agrees(1_000_000_000.5, 1_000_000_000.0, rc.MONEY_TOLERANCE) is False)

    magnitudes = (0.0, 100.0, 1e5, 1e7, 25_000_000.0, 1e9, 1e12)
    check("допуск НИКОГДА не превышает документированный потолок",
          all(rc._tolerance(value, value, rc.MONEY_TOLERANCE) <= rc.MONEY_TOLERANCE
              for value in magnitudes),
          str([(v, rc._tolerance(v, v, rc.MONEY_TOLERANCE)) for v in magnitudes]))
    check("две копейки не прощаются ни на одном из масштабов",
          all(not rc._agrees(value + 0.02, value, rc.MONEY_TOLERANCE)
              for value in magnitudes),
          str([v for v in magnitudes if rc._agrees(v + 0.02, v, rc.MONEY_TOLERANCE)]))
    check("материальная разница не прощается ни на каком масштабе",
          all(rc._agrees(value * 1.0001, value, rc.MONEY_TOLERANCE) is False
              for value in (1e3, 1e5, 1e7, 1e9)))

    # Ради чего допуск вообще существует: два ПРАВИЛЬНЫХ сложения одних и тех
    # же слагаемых в разном порядке. Проверяется не рассуждением, а сложением.
    parts = [round(7.77 + i * 0.13, 2) for i in range(5000)]
    forward = 0.0
    for part in parts:
        forward += part
    backward = 0.0
    for part in reversed(parts):
        backward += part
    check("реальный шум сложения 5000 слагаемых прощается",
          rc._agrees(forward, backward, rc.MONEY_TOLERANCE),
          f"разница {abs(forward - backward)!r}")
    check("и этот шум на порядки меньше копейки",
          abs(forward - backward) < 0.001, str(abs(forward - backward)))
    check("шум в несколько шагов float прощается на любом масштабе",
          all(rc._agrees(value + math.ulp(value) * 10, value, rc.MONEY_TOLERANCE)
              for value in (100.0, 1e5, 1e7, 25_000_000.0, 1e9)))


def test_non_finite_reference():
    """NaN, Infinity и переполнение — эталон непригоден, а не «ноль».

    Регрессия на discussion_r3877212862. `json.loads` принимает литералы
    `NaN`/`Infinity`, а `1e309` превращает в бесконечность; огромное целое
    роняло `float()` с `OverflowError` мимо обработчика — трассировкой вместо
    обещанного кода 1.
    """
    for label, value in (("NaN", float("nan")), ("Infinity", float("inf")),
                         ("-Infinity", float("-inf")), ("1e309", float("1e309")),
                         ("огромное целое", 10 ** 400)):
        check(f"эталон с {label} отклонён",
              raises(lambda v=value: rc._number(v, "проба"), rc.ReconcileError))

    check("NaN в позиции sales-monthly — отказ закрытым",
          raises(lambda: rc.parse_reference(
              sales_monthly(items=sales_monthly_item(0, sale_rev=float("nan"))), MONTH),
              rc.ReconcileError))
    check("Infinity в summary — отказ закрытым",
          raises(lambda: rc.parse_reference(
              sales_monthly(summary=dict(SALES_MONTHLY_SUMMARY, sales=float("inf"))), MONTH),
              rc.ReconcileError))
    check("огромное целое в формате A — отказ закрытым",
          raises(lambda: rc.parse_reference({"Худи": {"2026-05": [1, 10 ** 400]}}, MONTH),
                 rc.ReconcileError))
    check("Infinity в формате B — отказ закрытым",
          raises(lambda: rc.parse_reference(
              {"month": MONTH, "bases": {"Худи": {"net_qty": 1, "net_rev": float("inf")}}},
              MONTH), rc.ReconcileError))

    # Разбор идёт через json.loads, который эти литералы принимает молча.
    check("литералы NaN/Infinity из настоящего JSON тоже отклоняются",
          all(raises(lambda t=text: rc.parse_reference(json.loads(t), MONTH),
                     rc.ReconcileError)
              for text in ('{"Худи": {"2026-05": [1, NaN]}}',
                           '{"Худи": {"2026-05": [1, Infinity]}}',
                           '{"Худи": {"2026-05": [1, 1e309]}}')))

    check("_agrees не считает не-числа совпадающими",
          not rc._agrees(float("nan"), 0.0, rc.MONEY_TOLERANCE)
          and not rc._agrees(float("inf"), float("inf"), rc.MONEY_TOLERANCE))


def test_threshold_validation():
    """Порог, который сам себя выключает или выворачивает, — отказ закрытым.

    Регрессия на discussion_r3877212859: `nan` делал `exceeds` ложным для
    любого расхождения, отрицательный порог — истинным даже для нулевого.
    """
    rep = report()
    for bad in (float("nan"), float("inf"), float("-inf"), -0.01, -1, -1e9):
        check(f"порог {bad!r} отклонён",
              raises(lambda b=bad: rc.check_threshold(b), rc.ReconcileError))
        check(f"exceeds с порогом {bad!r} не отвечает молча",
              raises(lambda b=bad: rc.exceeds(rep, b), rc.ReconcileError))
    check("нулевой порог по-прежнему допустим", rc.check_threshold(0) == 0.0)
    check("положительный порог по-прежнему допустим", rc.check_threshold(500) == 500.0)
    check("отсутствие порога по-прежнему допустимо", rc.check_threshold(None) is None)


def test_shapes_preserved():
    """Три формата различаются, и новый не перехватил старые."""
    check("формат A остаётся A",
          rc.parse_reference(REFERENCE_BY_MONTH, MONTH)["shape"] == "sales_by_month")
    check("формат B остаётся B",
          rc.parse_reference(REFERENCE_EXPLICIT, MONTH)["shape"] == "explicit")
    check("формат C опознан", rc.parse_reference(SALES_MONTHLY, MONTH)["shape"] == "sales_monthly")
    # Позиция с именем «items» одна, без «summary», форматом C не является.
    quirky = dict(REFERENCE_BY_MONTH, items={"2026-05": [1, 100]})
    parsed = rc.parse_reference(quirky, MONTH)
    check("одного поля 'items' для опознания формата C мало",
          parsed["shape"] == "sales_by_month"
          and parsed["totals"]["net_rev"] == REFERENCE_NET_REV + 100)


def test_sales_monthly_end_to_end():
    """Классификация DATA-5 воспроизводится прямо из штатного payload'а."""
    rep = rc.compare(saas_month(), rc.parse_reference(SALES_MONTHLY, MONTH))
    d = rep["deltas"]
    check("формат эталона попал в отчёт", rep["reference_shape"] == "sales_monthly")
    check("возвраты сошлись в ноль", d["return_rev_raw"] == 0.0, str(d["return_rev_raw"]))
    check("валовые разошлись на −500", d["gross_rev_raw"] == -500.0, str(d["gross_rev_raw"]))
    check("всё расхождение нетто объясняется валовыми",
          d["net_rev_raw"] == d["gross_rev_raw"] - d["return_rev_raw"])
    check("расхождение по штукам = −1", d["net_qty_raw"] == -1.0)
    check("позиция только у эталона названа", rep["only_in_reference"] == ["Ремень"])
    text = rc.render_text(rep)
    check("в тексте отчёта валовые и возвраты — числа, а не прочерк",
          "28 500.00" in text and "5 000.00" in text)


def test_reference_transport():
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        good = write_json(tmpdir, "ref.json", REFERENCE_BY_MONTH)
        ref = rc.load_reference_file(good, MONTH)
        check("эталон читается из файла", ref["totals"]["net_rev"] == REFERENCE_NET_REV)

        broken = tmpdir / "broken.json"
        broken.write_text("{не json", encoding="utf-8")
        check("файл не JSON — отказ закрытым",
              raises(lambda: rc.load_reference_file(str(broken), MONTH), rc.ReconcileError))
        check("файла нет — отказ закрытым",
              raises(lambda: rc.load_reference_file(str(tmpdir / "нет.json"), MONTH),
                     rc.ReconcileError))

    for bad_url in ("file:///etc/passwd", "ftp://example.invalid/x",
                    "https://user:secret@example.invalid/x", "https://", "не адрес"):
        check(f"адрес отклонён до сети: {bad_url!r}",
              raises(lambda u=bad_url: rc.load_reference_url(u, MONTH, timeout=1),
                     rc.ReconcileError))

    server = ThreadingHTTPServer(("127.0.0.1", 0), _RefHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        ref = rc.load_reference_url(base + "/ok", MONTH, timeout=10)
        check("эталон читается по http", ref["totals"]["net_rev"] == REFERENCE_NET_REV)
        check("HTTP 500 — отказ закрытым",
              raises(lambda: rc.load_reference_url(base + "/boom", MONTH, timeout=10),
                     rc.ReconcileError))
        check("ответ не JSON — отказ закрытым",
              raises(lambda: rc.load_reference_url(base + "/notjson", MONTH, timeout=10),
                     rc.ReconcileError))
    finally:
        server.shutdown()
        server.server_close()

    dead = f"http://127.0.0.1:{free_port()}/ok"
    check("сеть недоступна — отказ закрытым",
          raises(lambda: rc.load_reference_url(dead, MONTH, timeout=2), rc.ReconcileError))


# ══ 6. Сравнение и классификация ═════════════════════════════════════════════

def test_compare():
    rep = report()
    d = rep["deltas"]
    check("расхождение сырого нетто = 23 000 − 23 500 = −500",
          d["net_rev_raw"] == -500.0, str(d["net_rev_raw"]))
    check("расхождение нетто интерфейса = 18 000 − 23 500 = −5 500",
          d["net_rev_included"] == -5500.0, str(d["net_rev_included"]))
    check("по умолчанию база сравнения — сырое нетто",
          rep["basis"] == "raw" and d["net_rev"] == d["net_rev_raw"])
    check("расхождение по штукам = 14 − 15 = −1", d["net_qty_raw"] == -1.0)
    check("валовые и возвраты этому эталону неизвестны",
          d["gross_rev_raw"] is None and d["return_rev_raw"] is None)

    included = report(basis="included")
    check("база сравнения included меняет расхождение",
          included["deltas"]["net_rev"] == -5500.0)

    counts = rep["base_counts"]
    check("позиций у «Оборота» 4, у эталона 5",
          counts["saas"] == 4 and counts["reference"] == 5)
    check("общих позиций 4, только у эталона 1",
          counts["matched"] == 4 and counts["only_in_reference"] == 1
          and counts["only_in_saas"] == 0)
    check("позиция, которой нет у «Оборота», названа",
          rep["only_in_reference"] == ["Ремень"])

    top = rep["top_base_deltas"]
    check("первой идёт позиция с наибольшим модулем расхождения",
          top[0]["base_name"] == "Ремень" and top[0]["delta_rev"] == -500.0)
    check("нули упорядочены по имени — порядок детерминирован",
          [it["base_name"] for it in top[1:]] == ["Пробник", "Свеча", "Футболка", "Худи"],
          str([it["base_name"] for it in top[1:]]))
    check("--top 0 отдаёт пустой список", report(top=0)["top_base_deltas"] == [])
    check("--top больше числа позиций отдаёт все",
          len(report(top=99)["top_base_deltas"]) == 5)
    check("повторный вызов даёт тот же отчёт", rc.render_json(report()) == rc.render_json(report()))

    check("предупреждение об охвате складов всегда в отчёте",
          any("склад" in note for note in rep["scope_notes"]))
    check("предупреждение о разнице сырого и интерфейсного нетто на месте",
          any("excluded" in note for note in rep["scope_notes"]))

    check("месяцы сторон обязаны совпадать",
          raises(lambda: rc.compare(saas_month("2026-04"),
                                    rc.parse_reference(REFERENCE_BY_MONTH, MONTH)),
                 rc.ReconcileError))
    check("неизвестная база сравнения отклонена",
          raises(lambda: report(basis="что-нибудь"), rc.ReconcileError))


def test_returns_reconcile_gross_diverges():
    """Классификация DATA-5 на синтетике: возвраты сходятся, разница в валовых.

    Это не утверждение о боевых числах — это проверка того, что инструмент
    способен такой рисунок ПОКАЗАТЬ и не размазать его по одному итогу.
    """
    rep = report(REFERENCE_EXPLICIT)
    d = rep["deltas"]
    check("возвраты сошлись в ноль", d["return_rev_raw"] == 0.0, str(d["return_rev_raw"]))
    check("валовые разошлись на −500", d["gross_rev_raw"] == -500.0, str(d["gross_rev_raw"]))
    check("всё расхождение нетто объясняется валовыми",
          d["net_rev_raw"] == d["gross_rev_raw"] - d["return_rev_raw"])


# ══ 7. Коды возврата и печать ════════════════════════════════════════════════

def test_exit_policy():
    rep = report()
    check("порога нет — расхождение не наказывается", rc.exceeds(rep, None) is False)
    check("порог 0 при расхождении −500 — превышен", rc.exceeds(rep, 0) is True)
    check("порог 500 при расхождении −500 — НЕ превышен (строго больше)",
          rc.exceeds(rep, 500) is False)
    check("порог 499.99 при расхождении −500 — превышен", rc.exceeds(rep, 499.99) is True)

    zero = report(REFERENCE_MATCHING)
    check("совпавший эталон даёт нулевое расхождение",
          zero["deltas"]["net_rev"] == 0.0, str(zero["deltas"]["net_rev"]))
    check("нулевое расхождение не превышает нулевой порог", rc.exceeds(zero, 0) is False)


def run_cli(args: list[str]) -> tuple[int, str]:
    out = io.StringIO()
    code = rc.run(args, stdout=out)
    return code, out.getvalue()


def test_cli():
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        ref_file = write_json(tmpdir, "ref.json", REFERENCE_BY_MONTH)
        base_args = ["--db", str(DB_PATH), "--org", str(ORG_ID), "--month", MONTH,
                     "--reference-file", ref_file]

        code, text = run_cli(base_args)
        check("обычный запуск — код 0", code == rc.EXIT_OK, f"код {code}")
        check("в тексте есть строка сырого нетто", "сырое нетто" in text)
        check("в тексте есть предупреждение об охвате", "склад" in text)

        code, payload = run_cli(base_args + ["--json"])
        parsed = json.loads(payload)
        check("--json печатает разбираемый JSON и код 0",
              code == rc.EXIT_OK and parsed["deltas"]["net_rev_raw"] == -500.0)
        check("JSON детерминирован", run_cli(base_args + ["--json"])[1] == payload)

        code, _ = run_cli(base_args + ["--fail-on-delta", "0"])
        check("порог 0 при расхождении — код 2", code == rc.EXIT_DELTA, f"код {code}")
        code, _ = run_cli(base_args + ["--fail-on-delta", "500"])
        check("порог 500 при расхождении 500 — код 0", code == rc.EXIT_OK, f"код {code}")

        monthly_file = write_json(tmpdir, "sales_monthly.json", SALES_MONTHLY)
        code, payload = run_cli(["--db", str(DB_PATH), "--org", str(ORG_ID), "--month", MONTH,
                                 "--reference-file", monthly_file, "--json"])
        parsed = json.loads(payload)
        check("штатный payload ручки проходит весь путь CLI",
              code == rc.EXIT_OK and parsed["reference_shape"] == "sales_monthly"
              and parsed["deltas"]["return_rev_raw"] == 0.0
              and parsed["deltas"]["gross_rev_raw"] == -500.0, f"код {code}")

        matching_file = write_json(tmpdir, "matching.json", REFERENCE_MATCHING)
        code, _ = run_cli(["--db", str(DB_PATH), "--org", str(ORG_ID), "--month", MONTH,
                           "--reference-file", matching_file, "--fail-on-delta", "0"])
        check("нулевое расхождение при пороге 0 — код 0", code == rc.EXIT_OK, f"код {code}")

        bad_file = tmpdir / "bad.json"
        bad_file.write_text("{", encoding="utf-8")
        failures = {
            "неизвестная организация": ["--db", str(DB_PATH), "--org", "999",
                                        "--month", MONTH, "--reference-file", ref_file],
            "месяц без продаж": ["--db", str(DB_PATH), "--org", str(ORG_ID),
                                 "--month", "2026-07", "--reference-file", ref_file],
            "мусор в месяце": ["--db", str(DB_PATH), "--org", str(ORG_ID),
                               "--month", "май", "--reference-file", ref_file],
            "битый эталон": ["--db", str(DB_PATH), "--org", str(ORG_ID),
                             "--month", MONTH, "--reference-file", str(bad_file)],
            "базы нет": ["--db", str(tmpdir / "нет.db"), "--org", str(ORG_ID),
                         "--month", MONTH, "--reference-file", ref_file],
            "адрес с секретом": ["--db", str(DB_PATH), "--org", str(ORG_ID),
                                 "--month", MONTH, "--reference-url",
                                 "https://user:secret@example.invalid/x"],
        }
        for title, args in failures.items():
            code, text = run_cli(args)
            check(f"отказ закрытым, код 1: {title}", code == rc.EXIT_ERROR, f"код {code}")
            check(f"при отказе отчёт не печатается: {title}", text == "", repr(text[:80]))


def test_cli_exit_contract():
    """Код 2 значит РОВНО одно — подтверждённое расхождение.

    Регрессия на discussion_r3877212853 и r3877212859. Штатный `argparse`
    выходит на ошибке вызова кодом 2, а этот код здесь занят: автоматика,
    которая по нему заводит расследование, на опечатке заводила бы его на
    пустом месте. Негодный порог отдельной строкой: он ломает гейт в обе
    стороны сразу.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        ref_file = write_json(tmpdir, "ref.json", REFERENCE_BY_MONTH)
        base = ["--db", str(DB_PATH), "--org", str(ORG_ID), "--month", MONTH,
                "--reference-file", ref_file]

        misuse = {
            "нечисловой --org": ["--db", str(DB_PATH), "--org", "не-число",
                                 "--month", MONTH, "--reference-file", ref_file],
            "нечисловой --top": base + ["--top", "много"],
            "нечисловой --fail-on-delta": base + ["--fail-on-delta", "порог"],
            "неизвестный ключ": base + ["--выдумка", "1"],
            "нет обязательного --org": ["--db", str(DB_PATH), "--month", MONTH,
                                        "--reference-file", ref_file],
            "нет источника эталона": ["--db", str(DB_PATH), "--org", str(ORG_ID),
                                      "--month", MONTH],
            "два источника эталона": base + ["--reference-url", "https://example.invalid/x"],
            "неизвестная база сравнения": base + ["--basis", "выдумка"],
        }
        for title, args in misuse.items():
            code, text = run_cli(args)
            check(f"ошибка вызова — код 1, а не 2: {title}",
                  code == rc.EXIT_ERROR, f"код {code}")
            check(f"и отчёта при этом нет: {title}", text == "", repr(text[:80]))

        for bad in ("nan", "-1", "inf", "-inf"):
            code, text = run_cli(base + ["--fail-on-delta", bad])
            check(f"негодный порог {bad!r} — код 1, а не молчаливый 0 или 2",
                  code == rc.EXIT_ERROR, f"код {code}")
            check(f"и отчёта при негодном пороге нет: {bad!r}", text == "", repr(text[:80]))

        code, _ = run_cli(base + ["--top", "-1"])
        check("отрицательный --top — код 1", code == rc.EXIT_ERROR, f"код {code}")


def test_no_persistence():
    """Инструмент ничего не сохраняет: ни рядом с собой, ни рядом с базой."""
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        db_copy = tmpdir / "copy.db"
        shutil.copyfile(DB_PATH, db_copy)
        ref_file = write_json(tmpdir, "ref.json", REFERENCE_BY_MONTH)
        digest = sha256(db_copy)
        before = {p.name for p in tmpdir.iterdir()}

        cwd = os.getcwd()
        os.chdir(tmpdir)
        try:
            code, text = run_cli(["--db", str(db_copy), "--org", str(ORG_ID),
                                  "--month", MONTH, "--reference-file", ref_file, "--json"])
        finally:
            os.chdir(cwd)

        check("прогон состоялся", code == rc.EXIT_OK and text.strip().startswith("{"))
        check("база не изменилась ни на байт", sha256(db_copy) == digest)
        check("ни одного нового файла не появилось",
              {p.name for p in tmpdir.iterdir()} == before,
              str(sorted({p.name for p in tmpdir.iterdir()} - before)))


# ══ 8. Мутации: доказательство, что проверки ловят ═══════════════════════════
#
# Каждая мутация — это ровно тот дефект, ради которого написана своя группа
# проверок. Проба возвращает True, когда инвариант ЦЕЛ; мутация считается
# пойманной, если после неё проба перестала выполняться (вернула False или
# бросила исключение). Отдельно проверяется, что фрагмент вообще нашёлся в
# нужном количестве: пустая мутация «ловится» всегда и не значит ничего.

def _probe_readonly(mod) -> bool:
    """База, открытая инструментом, не даёт записать."""
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "probe.db"
        shutil.copyfile(DB_PATH, copy)
        try:
            conn = mod.open_readonly(str(copy))
        except mod.ReconcileError:
            return True  # отказался открывать — тоже честный исход
        try:
            conn.execute("INSERT INTO sku_hidden (org_id, base_name) VALUES (1, 'мут')")
            conn.commit()
            return False
        except sqlite3.Error:
            return True
        finally:
            conn.close()


def _probe_query_only(mod) -> bool:
    """Соединение инструмента объявляет себя query_only."""
    conn = mod.open_readonly(str(DB_PATH))
    try:
        return int(conn.execute("PRAGMA query_only").fetchone()[0]) == 1
    finally:
        conn.close()


def _probe_write_guard(mod) -> bool:
    """Проба третьего слоя отвергает заведомо пишущее соединение."""
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "guard.db"
        shutil.copyfile(DB_PATH, copy)
        conn = sqlite3.connect(copy)
        try:
            return raises(lambda: mod._assert_cannot_write(conn), mod.ReconcileError)
        finally:
            conn.close()


def _probe_return_sign(mod) -> bool:
    conn = mod.open_readonly(str(DB_PATH))
    try:
        return mod.load_saas_month(conn, ORG_ID, MONTH)["totals"]["net_rev"] == 23000.0
    finally:
        conn.close()


def _probe_included(mod) -> bool:
    conn = mod.open_readonly(str(DB_PATH))
    try:
        return mod.load_saas_month(conn, ORG_ID, MONTH)["totals"]["included_net_rev"] == 18000.0
    finally:
        conn.close()


def _probe_canon(mod) -> bool:
    ref = mod.parse_reference(REFERENCE_BY_MONTH, MONTH)
    return ref["bases"].get("Худи", {}).get("net_rev") == 12000.0


def _probe_number(mod) -> bool:
    return raises(lambda: mod.parse_reference({"Худи": {"2026-05": [1, "9000"]}}, MONTH),
                  mod.ReconcileError)


def _probe_strict_threshold(mod) -> bool:
    """Порог строгий: расхождение, равное порогу, кодом возврата не наказывается."""
    conn = mod.open_readonly(str(DB_PATH))
    try:
        rep = mod.compare(mod.load_saas_month(conn, ORG_ID, MONTH),
                          mod.parse_reference(REFERENCE_BY_MONTH, MONTH))
    finally:
        conn.close()
    return mod.exceeds(rep, 500) is False


def _probe_month_guard(mod) -> bool:
    """Ответ ручки за ДРУГОЙ месяц отвергается."""
    return raises(lambda: mod.parse_reference(sales_monthly(month="2026-07"), MONTH),
                  mod.ReconcileError)


def _probe_summary_guard(mod) -> bool:
    """summary, противоречащий сумме items, отвергается."""
    contradiction = dict(SALES_MONTHLY_SUMMARY, sales=29000)
    return raises(lambda: mod.parse_reference(sales_monthly(summary=contradiction), MONTH),
                  mod.ReconcileError)


def _probe_item_net_guard(mod) -> bool:
    """Позиция, у которой net не равен sale_rev − ret_rev, отвергается."""
    return raises(lambda: mod.parse_reference(
        sales_monthly(items=sales_monthly_item(0, net=999999)), MONTH), mod.ReconcileError)


def _probe_fold(mod) -> bool:
    """Две позиции одного канонического имени складываются, а не затирают друг друга."""
    folded = mod.parse_reference(SALES_MONTHLY_FOLDED, MONTH)
    return folded["bases"]["Худи"]["gross_rev"] == 15000.0


def _probe_absolute_tolerance(mod) -> bool:
    """Две копейки на 25 млн — расхождение, а не совпадение."""
    return mod._agrees(25_000_000.02, 25_000_000.0, mod.MONEY_TOLERANCE) is False


def _probe_finite_numbers(mod) -> bool:
    """NaN, Infinity и переполнение отвергаются как непригодный эталон."""
    return all(raises(lambda v=value: mod._number(v, "проба"), mod.ReconcileError)
               for value in (float("nan"), float("inf"), 10 ** 400))


def _probe_threshold(mod) -> bool:
    """Негодный порог отвергается, а не выключает и не выворачивает гейт."""
    return all(raises(lambda b=bad: mod.check_threshold(b), mod.ReconcileError)
               for bad in (float("nan"), -1))


def _probe_argparse_exit(mod) -> bool:
    """Ошибка вызова даёт код 1, а не занятый код 2."""
    out = io.StringIO()
    try:
        code = mod.run(["--db", str(DB_PATH), "--org", "не-число", "--month", MONTH,
                        "--reference-file", "/несуществующий.json"], stdout=out)
    except SystemExit as exc:
        return exc.code == mod.EXIT_ERROR
    return code == mod.EXIT_ERROR


def _probe_empty_month(mod) -> bool:
    conn = mod.open_readonly(str(DB_PATH))
    try:
        return raises(lambda: mod.load_saas_month(conn, ORG_ID, "2026-07"), mod.ReconcileError)
    finally:
        conn.close()


MUTATIONS = [
    # Слои защиты страхуют друг друга, поэтому снятие ОДНОГО из них наружу не
    # видно — это и есть смысл трёх слоёв. Мутация снимает защиту целиком:
    # именно этот случай проверка «INSERT отклонён» и обязана поймать.
    ("защита «только чтение» снята целиком",
     [('"?mode=ro"', '"?mode=rw"', 1),
      ('conn.execute("PRAGMA query_only = 1")', 'conn.execute("PRAGMA query_only = 0")', 1),
      ('if not row or int(row[0]) != 1:', 'if False:', 1),
      ('        _assert_cannot_write(conn)', '        pass', 1)],
     _probe_readonly),
    ("соединение перестаёт объявлять query_only",
     [('conn.execute("PRAGMA query_only = 1")', 'conn.execute("PRAGMA query_only = 0")', 1),
      ('if not row or int(row[0]) != 1:', 'if False:', 1)],
     _probe_query_only),
    ("проба записи перестаёт отказывать",
     [("raise ReconcileError(_WRITABLE_MSG)", "return None", 1)],
     _probe_write_guard),
    ("возврат перестаёт вычитаться",
     [("WHEN s.is_return <> 0 THEN s.revenue ELSE 0 END) AS return_rev",
       "WHEN s.is_return <> 0 THEN 0 ELSE 0 END) AS return_rev", 1)],
     _probe_return_sign),
    ("исключённые позиции попадают в нетто интерфейса",
     [("AND p.excluded = 0", "", 4)],
     _probe_included),
    ("каноническое имя перестаёт снимать размер",
     [('SIZE_SUFFIX_RE.sub("", str(name if name is not None else "")).strip()',
       'str(name if name is not None else "").strip()', 1)],
     _probe_canon),
    ("эталон принимает не-числа",
     [("if isinstance(value, bool) or not isinstance(value, (int, float)):",
       "if False:", 1)],
     _probe_number),
    ("порог расхождения становится нестрогим",
     [("    return abs(delta) > value", "    return abs(delta) >= value", 1)],
     _probe_strict_threshold),
    ("месяц без продаж отдаёт нули вместо отказа",
     [("if not total_rows:", "if False:", 1)],
     _probe_empty_month),
    ("подмена месяца ручкой перестаёт быть отказом",
     [('    if ref_month != month:\n        raise ReconcileError(\n'
       '            f"эталон ответил за',
       '    if False:\n        raise ReconcileError(\n'
       '            f"эталон ответил за', 1)],
     _probe_month_guard),
    ("summary перестаёт сверяться с items",
     [("if not _agrees(got, value, absolute):", "if False:", 1)],
     _probe_summary_guard),
    ("net позиции перестаёт сверяться с sale_rev − ret_rev",
     [('if not _agrees(values["net"], values["sale_rev"] - values["ret_rev"], MONEY_TOLERANCE):',
       "if False:", 1)],
     _probe_item_net_guard),
    ("денежный допуск расширен до неразличимости",
     [("MONEY_TOLERANCE = 0.01      # ₽, копейка", "MONEY_TOLERANCE = 1000.0", 1)],
     _probe_summary_guard),
    ("свёртка по каноническому имени затирает вместо сложения",
     [('cur["gross_rev"] += values["sale_rev"]', 'cur["gross_rev"] = values["sale_rev"]', 1)],
     _probe_fold),
    # Возврат к прежней относительной добавке — ровно тот дефект, который
    # нашло ревью: на 25 млн она прощает две копейки.
    ("допуск снова становится относительным",
     [("    return min(absolute, SUMMATION_STEPS * math.ulp(magnitude))",
       "    return max(absolute, magnitude * 1e-9)", 1)],
     _probe_absolute_tolerance),
    ("эталон снова принимает не конечные числа",
     [("    if not math.isfinite(number):", "    if False:", 1)],
     _probe_finite_numbers),
    ("порог перестаёт проверяться",
     [("    if not math.isfinite(value):", "    if False:", 1),
      ("    if value < 0:", "    if False:", 1)],
     _probe_threshold),
    ("ошибка вызова снова уходит в занятый код 2",
     [("        raise ReconcileError(f\"неверный вызов: {message}\")",
       "        super().error(message)", 1)],
     _probe_argparse_exit),
]


def test_mutations():
    check("на неизменённом исходнике все пробы зелёные",
          all(probe(rc) for _, _, probe in MUTATIONS))
    for title, edits, probe in MUTATIONS:
        source = TOOL_SOURCE
        counts_ok = True
        for old, new, expected in edits:
            found = source.count(old)
            if found != expected:
                counts_ok = False
                check(f"мутация «{title}»: фрагмент найден {expected} раз",
                      False, f"найдено {found}: {old!r}")
                break
            source = source.replace(old, new)
        if not counts_ok:
            continue
        check(f"мутация «{title}»: исходник действительно изменён", source != TOOL_SOURCE)
        try:
            mutated = load_tool(source, name=f"reconcile_mut_{abs(hash(title))}")
            caught = not probe(mutated)
        except Exception:  # noqa: BLE001 — упавшая проба это тоже «поймано»
            caught = True
        check(f"мутация поймана: {title}", caught)

    check("продуктовый файл на диске не изменён мутациями",
          TOOL_PATH.read_text(encoding="utf-8") == TOOL_SOURCE)


def main() -> int:
    block("== 1. Канонические имена ==", test_canon)
    block("== 2. Границы месяца ==", test_month_bounds)
    block("== 3. Только чтение ==", test_readonly)
    block("== 3а. Только чтение: база в WAL ==", test_readonly_wal)
    block("== 4. Арифметика месяца ==", test_totals)
    block("== 4а. Совпадение с расчётом страницы «Оборот» ==", test_ui_parity)
    block("== 4б. Отказ закрытым на входе ==", test_fail_closed_inputs)
    block("== 5. Эталон: формат первой таблицы ==", test_reference_by_month)
    block("== 5а. Эталон: битые данные ==", test_reference_malformed)
    block("== 5б. Эталон: явный формат ==", test_reference_explicit)
    block("== 5в. Эталон: штатный payload /api/sales-monthly ==", test_reference_sales_monthly)
    block("== 5г. sales-monthly: подмена месяца ==", test_sales_monthly_month_guard)
    block("== 5д. sales-monthly: битые позиции ==", test_sales_monthly_items_guard)
    block("== 5е. sales-monthly: summary против items ==", test_sales_monthly_summary_guard)
    block("== 5ж. Допуск не прячет материальную разницу ==", test_tolerance_cannot_hide)
    block("== 5ж-1. Не конечные числа эталона ==", test_non_finite_reference)
    block("== 5ж-2. Порог --fail-on-delta ==", test_threshold_validation)
    block("== 5з. Три формата не перехватывают друг друга ==", test_shapes_preserved)
    block("== 5и. sales-monthly целиком: классификация DATA-5 ==", test_sales_monthly_end_to_end)
    block("== 5к. Эталон: файл и сеть ==", test_reference_transport)
    block("== 6. Сравнение ==", test_compare)
    block("== 6а. Возвраты сходятся, валовые нет ==", test_returns_reconcile_gross_diverges)
    block("== 7. Порог расхождения ==", test_exit_policy)
    block("== 7а. Командная строка ==", test_cli)
    block("== 7а-1. Контракт кодов возврата ==", test_cli_exit_contract)
    block("== 7б. Ничего не сохраняется ==", test_no_persistence)
    block("== 8. Мутации ==", test_mutations)

    print(f"\nИТОГО: {len(PASSED)} OK, {len(FAILED)} FAIL")
    for name in FAILED:
        print(f"  FAIL {name}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    code = main()
    for _p in (DB_PATH, *SIDECARS):
        if _p.exists():
            _p.unlink()
    sys.exit(code)
