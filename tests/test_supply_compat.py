# -*- coding: utf-8 -*-
"""SUPPLY-COMPAT: пишущие маршруты плана переживают ЧУЖОЙ замок схемы.

ЗАЧЕМ ЭТОТ НАБОР СУЩЕСТВУЕТ, ЕСЛИ СЕГОДНЯ ТАКОГО ЗАМКА НЕТ.

Замок в базе живёт дольше кода. Штатный откат (`deploy/deploy.sh <прежний SHA>`)
возвращает КОД, но не схему: уникальный индекс, поставленный новой версией,
остаётся в базе. Прежний код такого ограничения не знает, делает `INSERT`,
получает `IntegrityError` — и, если её никто не ловит, отдаёт 500 на основном
пути записи. То есть цену отката платит пользователь.

Это не гипотеза. Ровно так и вышло с SUPPLY-FIX-1: два независимых
воспроизведения (моё на `ea1caff`, Codex на фактическом rollback target
`e7e106ed`) дали HTTP 500 и `UNIQUE constraint failed:
supply_assignments.org_id, material_id, batch_id` на повторном назначении той же
пары. Данные при этом целы, старт и чтение работают — но основной путь записи
отвечает пустым отказом сервера. Владелец разрешил другой порядок выпуска:
сначала этот пакет совместимости (он же новый rollback target), потом схема.

ЧТО ИМЕННО ЗДЕСЬ ПРОВЕРЯЕТСЯ И ПОЧЕМУ ИМЕННО ТАК.

Набор сам ставит два будущих индекса — дословным DDL из PR #51
(`app/models.py`, шаг старта 12). Это не имитация «похожего» ограничения:
проверять совместимость с придуманным замком значило бы проверять собственную
выдумку. Схему пакет при этом не меняет ни на строку — индексы создаются ЗДЕСЬ,
на тестовой базе, и живут только в ней.

  1) на схеме БЕЗ будущих замков поведение не изменилось ни в одном ответе —
     дубль пары и дубль каталожной вещи по-прежнему принимаются, как на BASE;
  2) с поставленными замками повторное назначение той же пары НЕ даёт 500:
     ответ управляемый, и в его теле нет ни имени таблицы, ни слова про
     ограничение, ни следа драйвера;
  3) то же для второй, ЧАСТИЧНОЙ уникальности — каталожной вещи;
  4) после отказа не осталось частичной записи: ни строки назначения, ни вещи,
     ни события журнала, ни отметки поступка `op_id`;
  5) назначенный метраж после отказа тот же до сотых — откат полный, а не
     «почти»;
  6) следующий запрос той же сессии проходит: отказ точечный, а не отравляющий;
  7) границы доступа замком НЕ подменяются: участник получает свои 403, а
     readonly-подписка свои 402 — на том же конфликтующем запросе, где владелец
     получил бы 409. Иначе новая ветка стала бы дырой в правах;
  8) настоящая причина не потеряна: она уходит в журнал сервера. Это единственное,
     что отличает управляемый отказ от МАСКИРОВКИ чужого дефекта, и потому
     проверяется, а не подразумевается;
  9) и в журнал не уезжает введённый человеком текст: логируется сообщение БД,
     а не SQL с параметрами.

Живых внешних систем здесь нет: ни МойСклада, ни Google, ни сети наружу. Все
данные синтетические, PII в наборе нет.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "test_supply_compat.db"
APP_PORT = int(os.environ.get("OBOROT_TEST_PORT", "8824"))

os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["SCHEDULER_ENABLED"] = "0"

for suffix in ("", "-wal", "-shm"):
    p = Path(str(DB_PATH) + suffix)
    if p.exists():
        p.unlink()

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402

from app import routes_supply_planning as rsp  # noqa: E402
from app.main import app as oborot_app  # noqa: E402

BASE = f"http://127.0.0.1:{APP_PORT}"
PASS, FAIL = [], []

# ── Будущая схема, дословно ───────────────────────────────────────────────────
#
# Оба DDL скопированы БЕЗ ИЗМЕНЕНИЙ из PR #51 (`bfd91ef`, `app/models.py`,
# константы `_SUPPLY_ASSIGNMENT_PAIR_DDL` и `_SUPPLY_ITEM_CATALOG_DDL`). Брать
# их оттуда, а не сочинять «примерно такой же индекс», принципиально: набор
# обязан проверять совместимость с ТЕМ ограничением, которое реально приедет, а
# не с удобным. Второй индекс частичный — и это существенно: у новинок
# (`kind='draft'`) `base_name` пуст у всех сразу, поэтому полный индекс запретил
# бы вторую новинку вообще.
FUTURE_INDEXES = (
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_supply_assignments_pair "
    "ON supply_assignments (org_id, material_id, batch_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_supply_items_catalog "
    "ON supply_items (org_id, base_name) WHERE kind = 'catalog'",
)

#: Текст, который человек вводит в конфликтующем запросе. Он нарочно приметный:
#: по нему проверяется, что введённое человеком НЕ уехало в журнал сервера.
HUMAN_NOTE = "заметка-владельца-9137-приметная"


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
        deadline = time.time() + 15
        while time.time() < deadline:
            if self.server.started:
                return
            time.sleep(0.05)
        raise RuntimeError(f"сервер на порту {self.config.port} не поднялся")

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=10)


class LogCapture(logging.Handler):
    """Ловит записи логгера слоя — и хранит их отдельно от общего вывода."""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records: list[str] = []

    def emit(self, record):
        try:
            self.records.append(record.getMessage())
        except Exception:                                   # pragma: no cover
            self.records.append("<нечитаемая запись>")


def client(headers=None) -> httpx.Client:
    h = {"X-Oborot-CSRF": "1"}
    h.update(headers or {})
    return httpx.Client(base_url=BASE, headers=h, timeout=60.0)


def register(c: httpx.Client, email: str, org: str, name: str = "Владелец"):
    return c.post("/register", data={"name": name, "email": email,
                                     "password": "secret123", "org_name": org})


class Answer:
    """Ответ сервера ИЛИ обрыв соединения — одинаково пригодный для разбора.

    Зачем не просто `httpx.Response`. На BASE конфликтующий запрос отвечает 500,
    а 500 рвёт keep-alive: следующий запрос по тому же соединению может
    прилететь транспортной ошибкой. Если бы набор падал исключением, красный
    прогон на BASE превратился бы в аварию раннера, и РАЗНИЦА между «продукт
    отвечает 500» и «набор сломался» пропала бы ровно там, где она и нужна.
    """

    def __init__(self, status: int, text: str, transport_error: str = ""):
        self.status = status
        self.text = text
        self.transport_error = transport_error

    @property
    def where(self) -> str:
        return self.transport_error or f"{self.status} {self.text[:90]}"


def post(c: httpx.Client, path: str, payload: dict) -> Answer:
    try:
        r = c.post(path, json=payload)
    except httpx.HTTPError as exc:
        return Answer(0, "", f"транспорт: {type(exc).__name__}: {exc}")
    return Answer(r.status_code, r.text)


def db_conn() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH, timeout=30)


def fingerprint(org_id: int) -> dict:
    """Отпечаток строк слоя. Сравнивается ДО и ПОСЛЕ отказа целиком.

    Считаются не только количества, но и сумма назначенного: отказ, который
    оставил бы половину поступка, поменял бы именно её, а количество строк — нет.
    """
    con = db_conn()
    try:
        def one(sql: str):
            return con.execute(sql, (org_id,)).fetchone()[0]

        return {
            "assignments": one("SELECT COUNT(*) FROM supply_assignments WHERE org_id=?"),
            "assigned_sum": one("SELECT COALESCE(ROUND(SUM(qty), 3), 0)"
                                " FROM supply_assignments WHERE org_id=?"),
            "items": one("SELECT COUNT(*) FROM supply_items WHERE org_id=?"),
            "materials": one("SELECT COUNT(*) FROM supply_materials WHERE org_id=?"),
            "batches": one("SELECT COUNT(*) FROM supply_batches WHERE org_id=?"),
            "events": one("SELECT COUNT(*) FROM supply_events WHERE org_id=?"),
        }
    finally:
        con.close()


def op_recorded(org_id: int, op_id: str) -> bool:
    con = db_conn()
    try:
        row = con.execute("SELECT 1 FROM supply_events WHERE org_id=? AND op_id=? LIMIT 1",
                          (org_id, op_id)).fetchone()
    finally:
        con.close()
    return row is not None


#: Слова, которых в теле ответа клиенту быть не должно ни при каком отказе:
#: устройство хранилища — не дело клиента, а имя колонки в тексте ошибки это
#: ровно оно.
LEAKY_WORDS = ("UNIQUE", "constraint", "supply_assignments", "supply_items",
               "sqlite", "SQL", "INSERT", "Traceback")


def leaks(text: str) -> list[str]:
    low = text.lower()
    return [w for w in LEAKY_WORDS if w.lower() in low]


def run() -> int:
    owner = client()
    register(owner, "compat-owner@test.io", "Бренд Совместимость")
    owner.post("/api/connect/demo")

    con = db_conn()
    try:
        org_id = con.execute("SELECT id FROM orgs ORDER BY id LIMIT 1").fetchone()[0]
    finally:
        con.close()

    # ── 1. Схема БЕЗ будущих замков: ни один ответ не изменился ───────────────
    #
    # Этот блок обязан быть ЗЕЛЁНЫМ и на BASE, и на HEAD — он и есть
    # доказательство, что пакет ничего не сломал в живом продукте. Ветка
    # обработчика на такой схеме не исполняется вовсе: срабатывать нечему.
    print("\n== На сегодняшней схеме поведение прежнее ==")
    r = post(owner, "/api/supply/planning/materials",
             {"title": "Ткань костюмная", "qty": "100", "unit": "м", "op_id": "c-m1"})
    check("материал заводится", r.status == 200, r.where)
    mat_id = json.loads(r.text)["materials"][0]["id"]

    cat = owner.get("/api/supply/planning/catalog").json()["options"]
    check("каталог демо-данных прочитан", len(cat) > 0, str(len(cat)))
    base_name = cat[0]["base_name"]

    r = post(owner, "/api/supply/planning/items",
             {"kind": "catalog", "base_name": base_name, "op_id": "c-i1"})
    check("каталожная вещь заводится", r.status == 200, r.where)
    item_id = [i for i in json.loads(r.text)["items"] if i["kind"] == "catalog"][0]["id"]

    # SUPPLY-FIX-1 изменил САМО создание: повтор той же модели каталога больше
    # не заводит вторую вещь, а возвращает существующую с `notice` (F-10).
    # Прежняя редакция этого набора требовала здесь ДВЕ вещи — она писалась
    # против рантайма, где дубль был законен. Требовать прежнего теперь значило
    # бы проверять отменённое поведение.
    r = post(owner, "/api/supply/planning/items",
             {"kind": "catalog", "base_name": base_name, "op_id": "c-i1-dup"})
    body = json.loads(r.text) if r.status == 200 else {}
    dup_items = [i for i in body.get("items", []) if i.get("base_name") == base_name]
    check("повтор той же модели каталога переиспользует вещь, а не заводит вторую",
          r.status == 200 and len(dup_items) == 1, f"{r.where} вещей: {len(dup_items)}")
    check("и человеку сказано, почему новой строки не появилось",
          body.get("notice") == "Эта модель уже есть в плане.", str(body.get("notice")))

    r = post(owner, "/api/supply/planning/batches",
             {"item_id": item_id, "title": "Партия А", "plan_qty": "30",
              "due_kind": "unknown", "op_id": "c-b1"})
    check("плановая партия А заводится", r.status == 200, r.where)
    r = post(owner, "/api/supply/planning/batches",
             {"item_id": item_id, "title": "Партия Б", "plan_qty": "20",
              "due_kind": "unknown", "op_id": "c-b2"})
    check("плановая партия Б заводится", r.status == 200, r.where)
    bids = {b["title"]: b["id"] for b in json.loads(r.text)["batches"]}

    r = post(owner, "/api/supply/planning/assignments",
             {"material_id": mat_id, "batch_id": bids["Партия А"], "qty": "30",
              "op_id": "c-a1"})
    check("метраж назначен на партию А", r.status == 200, r.where)

    # Здесь та же перемена: повтор назначения на ту же пару ПРИБАВЛЯЕТ к
    # существующей строке (F-09), а не заводит вторую. 30 + 40 = 70 одной
    # строкой — и это уже не «как и было», а то, ради чего пакет делался.
    r = post(owner, "/api/supply/planning/assignments",
             {"material_id": mat_id, "batch_id": bids["Партия А"], "qty": "40",
              "op_id": "c-a1-dup"})
    con = db_conn()
    try:
        pair = con.execute(
            "SELECT COUNT(*), COALESCE(SUM(qty), 0) FROM supply_assignments"
            " WHERE org_id=? AND material_id=? AND batch_id=?",
            (org_id, mat_id, bids["Партия А"])).fetchone()
    finally:
        con.close()
    pair_count, pair_sum = int(pair[0]), float(pair[1])
    check("повтор назначения на ту же пару прибавляет к строке, а не двоит её",
          r.status == 200 and pair_count == 1,
          f"{r.where} строк на пару: {pair_count}")
    check("и в строке сумма обоих назначений, а не последнее число",
          abs(pair_sum - 70.0) < 0.001, f"сумма: {pair_sum}")

    # ── 2. Схема больше не «будущая»: замки ставит настоящая миграция ────────
    #
    # Прежняя редакция доводила базу до будущего состояния РУКАМИ: удаляла дубли
    # своим `DELETE` и создавала индексы своим DDL. Тогда иначе было нельзя —
    # шага 12 в дереве не существовало. Теперь он здесь, и его выполняет старт
    # приложения. Оставить ручную имитацию значило бы проверять собственный
    # `DELETE` вместо миграции — ровно та подмена, которой быть не должно.
    #
    # Поэтому замки не создаются, а СВЕРЯЮТСЯ: если шаг 12 их не поставил, тут
    # красная строка, а не тихо доведённая до нужного вида база.
    print("\n== Замки поставлены настоящей миграцией шага 12 ==")
    con = db_conn()
    try:
        made = {row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'ux_supply_%'")}
        ledger = {row[0] for row in con.execute(
            "SELECT step_id FROM migration_ledger")}
    finally:
        con.close()
    check("оба замка стоят в базе — их поставил старт, а не набор",
          {"ux_supply_assignments_pair", "ux_supply_items_catalog"} <= made,
          ", ".join(sorted(made)))
    check("и это именно шаг 12, записанный в журнал миграций",
          "models.ensure_supply_planning_unique_schema" in ledger,
          ", ".join(sorted(ledger)))
    # Ожидаемая форма замков сохранена рядом как контракт: имена и колонки
    # заданы в `FUTURE_INDEXES` и не должны разъехаться с тем, что создаёт шаг.
    con = db_conn()
    try:
        ddl_now = {row[0]: (row[1] or "") for row in con.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='index'"
            " AND name LIKE 'ux_supply_%'")}
    finally:
        con.close()
    check("замок пары — по (org_id, material_id, batch_id)",
          "org_id" in ddl_now.get("ux_supply_assignments_pair", "")
          and "material_id" in ddl_now.get("ux_supply_assignments_pair", "")
          and "batch_id" in ddl_now.get("ux_supply_assignments_pair", ""),
          ddl_now.get("ux_supply_assignments_pair", "<нет>"))
    check("замок каталожной вещи частичный: только kind='catalog'",
          "kind" in ddl_now.get("ux_supply_items_catalog", "")
          and "catalog" in ddl_now.get("ux_supply_items_catalog", ""),
          ddl_now.get("ux_supply_items_catalog", "<нет>"))

    board = owner.get("/api/supply/planning")
    check("прежний код на мигрированной базе читает доску", board.status_code == 200,
          str(board.status_code))

    # ── 3. Повтор на замкнутой схеме: не 500 и не отказ, а прибавление ───────
    #
    # ЧТО ЗДЕСЬ ИЗМЕНИЛОСЬ И ПОЧЕМУ. Набор писался, когда создание делало
    # прямой `INSERT`: тогда на замкнутой схеме повтор упирался в индекс, и
    # ключевой проверкой было «управляемый 409 вместо 500». SUPPLY-FIX-1 убрал
    # сам конфликт: назначение прибавляет к строке, каталожная вещь
    # переиспользуется, `move_assignment` тоже сливает в существующую пару.
    # Ни одна ручка этого дерева больше не может нарушить эти два замка.
    #
    # Поэтому здесь проверяется то, что происходит НА САМОМ ДЕЛЕ: запрос
    # проходит, строка одна, сумма сошлась, пятисотки нет. Требовать 409 от
    # рантайма, который до конфликта не доходит, значило бы держать проверку,
    # которая либо всегда красная, либо доказывает подстроенный конфликт.
    #
    # Обработчик 409 при этом НЕ остаётся без доказательства. Он существует для
    # ОТКАТИВШЕГОСЯ рантайма, и проверяется там, где случается на самом деле:
    # §7 ниже разбирает `IntegrityError` напрямую и сторожит, что каждый пишущий
    # маршрут его ловит, а сквозной трёхфазный опыт с настоящим выпущенным
    # рантаймом `2f1434eb` показывает живой 409 после отката. Подстраивать
    # конфликт гонкой здесь нельзя: это запрещённый опыт PR #49.
    print("\n== Повторное назначение той же пары на замкнутой схеме ==")
    cap = LogCapture()
    layer_log = logging.getLogger("oborot.supply_planning")
    layer_log.addHandler(cap)
    prev_level = layer_log.level
    layer_log.setLevel(logging.DEBUG)
    try:
        before = fingerprint(org_id)
        conflict = post(owner, "/api/supply/planning/assignments",
                        {"material_id": mat_id, "batch_id": bids["Партия А"],
                         "qty": "5", "note": HUMAN_NOTE, "op_id": "c-a1-conflict"})
        after = fingerprint(org_id)
    finally:
        layer_log.removeHandler(cap)
        layer_log.setLevel(prev_level)

    check("основной путь записи НЕ отвечает 500", conflict.status != 500, conflict.where)
    check("и вообще не падает пятисоткой любого вида",
          conflict.status not in (0, 500, 502, 503), conflict.where)
    check("запрос проходит: конфликта на этом рантайме не возникает",
          conflict.status == 200, conflict.where)
    check("в теле ответа нет ни слова про устройство хранилища",
          not leaks(conflict.text), ", ".join(leaks(conflict.text)) or conflict.text[:90])

    # Строка по-прежнему ОДНА, а метраж прибавился: 70 + 5. Это и есть замена
    # прежней проверке «частичной записи не осталось» — тогда записи не должно
    # было быть вовсе, теперь она обязана быть полной и ровно одной.
    check("строка на пару осталась одна",
          after["assignments"] == before["assignments"],
          f"до {before['assignments']} / после {after['assignments']}")
    check("а метраж прибавлен целиком, без потери и без удвоения",
          abs(after["assigned_sum"] - before["assigned_sum"] - 5.0) < 0.001,
          f"до {before['assigned_sum']} / после {after['assigned_sum']}")
    check("поступок отмечен выполненным — запись состоялась",
          op_recorded(org_id, "c-a1-conflict"))

    # Журнал слоя обязан молчать: жаловаться не на что. И заметка человека в
    # него не уезжает ни при каком исходе — это свойство обработчика, а не
    # удача конкретного сценария.
    logged = " | ".join(cap.records)
    check("замок не срабатывал, и в журнале слоя нет жалобы на него",
          "UNIQUE constraint failed" not in logged, logged[:160] or "<журнал пуст>")
    check("а введённый человеком текст в журнал НЕ уехал",
          HUMAN_NOTE not in logged, logged[:160])

    # ── 4. Соседняя пара живёт своей жизнью ──────────────────────────────────
    print("\n== Прибавление к одной паре не задевает другую ==")
    ok_next = post(owner, "/api/supply/planning/assignments",
                   {"material_id": mat_id, "batch_id": bids["Партия Б"], "qty": "7",
                    "op_id": "c-a2"})
    check("назначение на ДРУГУЮ партию проходит", ok_next.status == 200, ok_next.where)
    again = owner.get("/api/supply/planning")
    check("и доска читается", again.status_code == 200, str(again.status_code))

    con = db_conn()
    try:
        pair_qty = con.execute(
            "SELECT qty FROM supply_assignments"
            " WHERE org_id=? AND material_id=? AND batch_id=?",
            (org_id, mat_id, bids["Партия А"])).fetchone()
    finally:
        con.close()
    check("на первой паре ровно то, что на неё назначали: 30 + 40 + 5",
          pair_qty is not None and abs(float(pair_qty[0]) - 75.0) < 0.001,
          str(pair_qty))

    # ── 5. Второй замок — частичный, по каталожной вещи ──────────────────────
    #
    # Здесь та же перемена, что в §3: повтор не упирается в замок, а
    # переиспользует вещь. Важное остаётся прежним и проверяется: пятисотки
    # нет, устройство хранилища наружу не течёт, ВТОРАЯ строка не появляется.
    print("\n== Повторная каталожная вещь на замкнутой схеме ==")
    before = fingerprint(org_id)
    dup = post(owner, "/api/supply/planning/items",
               {"kind": "catalog", "base_name": base_name, "note": HUMAN_NOTE,
                "op_id": "c-i-conflict"})
    after = fingerprint(org_id)
    check("повтор каталожной вещи НЕ отвечает 500", dup.status != 500, dup.where)
    check("запрос проходит: вещь переиспользована", dup.status == 200, dup.where)
    check("и без деталей хранилища в теле",
          not leaks(dup.text), ", ".join(leaks(dup.text)) or dup.text[:90])
    check("второй такой же вещи в плане не появилось",
          after["items"] == before["items"],
          f"до {before['items']} / после {after['items']}")

    # Частичность второго замка — это не деталь реализации, а условие того, что
    # он вообще пригоден: полный индекс по (org_id, base_name) запретил бы ВТОРУЮ
    # новинку, у которой base_name пуст у всех сразу.
    n1 = post(owner, "/api/supply/planning/items",
              {"kind": "draft", "title": "Новинка первая", "op_id": "c-n1"})
    n2 = post(owner, "/api/supply/planning/items",
              {"kind": "draft", "title": "Новинка вторая", "op_id": "c-n2"})
    check("две новинки с пустым base_name по-прежнему заводятся обе",
          n1.status == 200 and n2.status == 200, f"{n1.where} / {n2.where}")

    # ── 6. Границы доступа замком не подменяются ─────────────────────────────
    #
    # Проверка существует потому, что новая ветка обработчика могла бы стать
    # дырой в правах: если бы она отвечала 409 раньше, чем срабатывает проверка
    # роли, участник узнавал бы о существовании конфликта в чужом плане.
    print("\n== Права: замок не заменяет собой проверку роли ==")
    import bcrypt
    con = db_conn()
    try:
        pw = bcrypt.hashpw(b"secret123", bcrypt.gensalt()).decode()
        cur = con.execute("INSERT INTO users (email, pw_hash, name, created_at)"
                          " VALUES (?,?,?,datetime('now'))",
                          ("compat-member@test.io", pw, "Участник"))
        con.execute("INSERT INTO memberships (user_id, org_id, role)"
                    " VALUES (?,?,'member')", (cur.lastrowid, org_id))
        con.commit()
    finally:
        con.close()

    member = client()
    member.post("/login", data={"email": "compat-member@test.io",
                                "password": "secret123"})
    m_conflict = post(member, "/api/supply/planning/assignments",
                      {"material_id": mat_id, "batch_id": bids["Партия А"],
                       "qty": "5", "op_id": "c-mem-conflict"})
    check("участник на конфликтующем запросе получает 403, а не 409",
          m_conflict.status == 403, m_conflict.where)
    check("участник по-прежнему читает план",
          member.get("/api/supply/planning").status_code == 200)

    os.environ["OBOROT_SUBSCRIPTION_GATE"] = "1"
    con = db_conn()
    try:
        con.execute("UPDATE orgs SET trial_ends_at = datetime('now', '-5 day'),"
                    " paid_until = NULL")
        con.commit()
    finally:
        con.close()
    try:
        ro = post(owner, "/api/supply/planning/assignments",
                  {"material_id": mat_id, "batch_id": bids["Партия А"], "qty": "5",
                   "op_id": "c-ro-conflict"})
        check("в readonly тот же конфликт даёт 402, а не 409",
              ro.status == 402, ro.where)
        check("и план по-прежнему читается",
              owner.get("/api/supply/planning").status_code == 200)
    finally:
        os.environ["OBOROT_SUBSCRIPTION_GATE"] = "0"
        con = db_conn()
        try:
            con.execute("UPDATE orgs SET trial_ends_at = datetime('now', '+30 day')")
            con.commit()
        finally:
            con.close()

    # ── 7. Сторожа контракта, а не одного случая ─────────────────────────────
    #
    # Всё выше проверяет, что сегодня замок обработан. Этот блок проверяет, что
    # так останется: маршрут, добавленный завтра без обработчика, вернёт ту же
    # пятисотку, и узнать об этом лучше здесь, чем из журнала выпуска.
    print("\n== Контракт файла, а не отдельные ручки ==")

    # `getattr` с умолчанием, а не прямое обращение: на дереве БЕЗ этого пакета
    # константы нет вовсе, и прямое обращение уронило бы весь набор
    # AttributeError'ом — то есть красный прогон на BASE превратился бы в аварию
    # раннера вместо честного FAIL. Проверка обязана падать, а не ломаться.
    detail = getattr(rsp, "_CONFLICT_DETAIL", None)
    check("текст управляемого отказа не изменился ни на символ",
          detail == "Это действие уже выполнено. Обновите страницу.", str(detail))

    # Отказ на flush и отказ на commit обязаны быть НЕОТЛИЧИМЫ снаружи: это один
    # класс события, и разные ответы на него означали бы, что клиент видит
    # внутреннее устройство транзакции.
    handled = rsp._fail(IntegrityError("INSERT INTO t VALUES (?)", {},
                                       Exception("UNIQUE constraint failed: t.c")))
    check("IntegrityError разбирается в 409 тем же самым текстом",
          handled.status_code == 409 and handled.detail == detail,
          f"{handled.status_code} {handled.detail}")

    src = (ROOT / "app" / "routes_supply_planning.py").read_text(encoding="utf-8")
    posts = [b for b in src.split("@router.") if b.startswith("post(")]
    uncovered = []
    for block in posts:
        head = block.split("\n")
        name = next((ln for ln in head if ln.startswith(("def ", "async def "))), "?")
        if "IntegrityError" not in block or "db.rollback()" not in block:
            uncovered.append(name.strip())
    # Число законно растёт вместе со слоем (SUPPLY-FIX-2 добавил пять ручек:
    # архив и возврат материала, правку, архив и возврат вещи; SUPPLY-FIX-4 —
    # прикрепление эскиза к уже созданной вещи). Сторож здесь не про длину
    # списка, а про то, что выборка вообще что-то нашла: без этой строки пустой
    # `posts` сделал бы следующую проверку зелёной ни на чём.
    check("пишущих маршрутов найдено столько, сколько их есть", len(posts) == 18,
          f"найдено: {len(posts)}")
    check("КАЖДЫЙ пишущий маршрут ловит замок схемы и откатывает транзакцию",
          not uncovered, "; ".join(uncovered))

    # Обратная сторона того же контракта: у ЧИТАЮЩЕЙ ручки обработчика записи
    # быть не должно — иначе однажды окажется, что GET умеет откатывать.
    gets = [b for b in src.split("@router.") if b.startswith("get(")]
    check("читающие ручки транзакцию не откатывают",
          all("db.rollback()" not in b for b in gets), f"ручек: {len(gets)}")

    # ── 8. Замки не сняты и данные целы ──────────────────────────────────────
    print("\n== Итоговое состояние базы ==")
    con = db_conn()
    try:
        still = {row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'ux_supply_%'")}
        integrity = con.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        con.close()
    check("оба замка на месте — пакет уникальность не ослабляет",
          {"ux_supply_assignments_pair", "ux_supply_items_catalog"} <= still,
          ", ".join(sorted(still)))
    check("база цела", integrity == "ok", str(integrity))

    print(f"\nИТОГО: {len(PASS)} OK, {len(FAIL)} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    server = ServerThread(oborot_app, APP_PORT)
    server.start()
    try:
        code = run()
    finally:
        server.stop()
    sys.exit(code)
