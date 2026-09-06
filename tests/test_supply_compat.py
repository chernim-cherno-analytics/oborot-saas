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

    r = post(owner, "/api/supply/planning/items",
             {"kind": "catalog", "base_name": base_name, "op_id": "c-i1-dup"})
    dup_items = [i for i in json.loads(r.text).get("items", [])
                 if i.get("base_name") == base_name] if r.status == 200 else []
    check("без замка ВТОРАЯ такая же каталожная вещь принимается — как и было",
          r.status == 200 and len(dup_items) == 2, f"{r.where} вещей: {len(dup_items)}")

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

    r = post(owner, "/api/supply/planning/assignments",
             {"material_id": mat_id, "batch_id": bids["Партия А"], "qty": "40",
              "op_id": "c-a1-dup"})
    pair_rows = [a for a in json.loads(r.text).get("materials", [{}])[0].get("links", [])] \
        if r.status == 200 else []
    con = db_conn()
    try:
        pair_count = con.execute(
            "SELECT COUNT(*) FROM supply_assignments"
            " WHERE org_id=? AND material_id=? AND batch_id=?",
            (org_id, mat_id, bids["Партия А"])).fetchone()[0]
    finally:
        con.close()
    check("без замка ВТОРАЯ строка на ту же пару принимается — как и было",
          r.status == 200 and pair_count == 2,
          f"{r.where} строк на пару: {pair_count}; связей: {len(pair_rows)}")

    # ── 2. Приезжает будущая схема ────────────────────────────────────────────
    #
    # Дубли, законно созданные выше, сначала СХЛОПЫВАЮТСЯ — иначе уникальный
    # индекс просто не встанет. Это не удобство набора, а свойство самой
    # миграции: по этой же причине шаг 12 в PR #51 сначала сливает существующие
    # строки и только потом ставит замок, и обратный порядок невозможен.
    print("\n== Приехала будущая схема: два замка ==")
    con = db_conn()
    try:
        con.execute(
            "DELETE FROM supply_assignments WHERE id NOT IN "
            "(SELECT MIN(id) FROM supply_assignments GROUP BY org_id, material_id, batch_id)")
        con.execute(
            "DELETE FROM supply_items WHERE kind='catalog' AND id NOT IN "
            "(SELECT MIN(id) FROM supply_items WHERE kind='catalog'"
            " GROUP BY org_id, base_name)")
        for ddl in FUTURE_INDEXES:
            con.execute(ddl)
        con.commit()
        made = {row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'ux_supply_%'")}
    finally:
        con.close()
    check("оба будущих замка стоят в базе",
          {"ux_supply_assignments_pair", "ux_supply_items_catalog"} <= made,
          ", ".join(sorted(made)))

    board = owner.get("/api/supply/planning")
    check("прежний код на мигрированной базе читает доску", board.status_code == 200,
          str(board.status_code))

    # ── 3. Конфликт пары: управляемый отказ вместо 500 ───────────────────────
    #
    # ЭТО КЛЮЧЕВАЯ ПРОВЕРКА ПАКЕТА. На BASE здесь HTTP 500 и
    # `UNIQUE constraint failed` — тот самый открытый gate выпуска.
    print("\n== Повторное назначение той же пары ==")
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
    check("отказ управляемый: 409 Conflict", conflict.status == 409, conflict.where)
    check("в теле ответа нет ни слова про устройство хранилища",
          not leaks(conflict.text), ", ".join(leaks(conflict.text)) or conflict.text[:90])
    check("клиенту сказано человеческими словами",
          "Обновите страницу" in conflict.text, conflict.text[:120])

    check("частичной записи не осталось: отпечаток строк совпал целиком",
          before == after, f"до {before} / после {after}")
    check("и поступок не отмечен как выполненный",
          not op_recorded(org_id, "c-a1-conflict"))

    # 8-й и 9-й пункты набора: причина видна дежурному, но человек в лог не уехал.
    logged = " | ".join(cap.records)
    check("настоящая причина ушла в журнал сервера, а не потерялась",
          "UNIQUE constraint failed" in logged, logged[:160] or "<журнал пуст>")
    check("а введённый человеком текст в журнал НЕ уехал",
          HUMAN_NOTE not in logged, logged[:160])

    # ── 4. Отказ точечный: следующий запрос проходит ─────────────────────────
    print("\n== После отказа приложение продолжает работать ==")
    ok_next = post(owner, "/api/supply/planning/assignments",
                   {"material_id": mat_id, "batch_id": bids["Партия Б"], "qty": "7",
                    "op_id": "c-a2"})
    check("назначение на ДРУГУЮ партию проходит сразу после отказа",
          ok_next.status == 200, ok_next.where)
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
    check("уже назначенный метраж отказом не тронут",
          pair_qty is not None and abs(float(pair_qty[0]) - 30.0) < 0.001,
          str(pair_qty))

    # ── 5. Второй замок — частичный, по каталожной вещи ──────────────────────
    print("\n== Повторная каталожная вещь ==")
    before = fingerprint(org_id)
    dup = post(owner, "/api/supply/planning/items",
               {"kind": "catalog", "base_name": base_name, "note": HUMAN_NOTE,
                "op_id": "c-i-conflict"})
    after = fingerprint(org_id)
    check("повтор каталожной вещи НЕ отвечает 500", dup.status != 500, dup.where)
    check("отказ управляемый: 409 Conflict", dup.status == 409, dup.where)
    check("и без деталей хранилища в теле",
          not leaks(dup.text), ", ".join(leaks(dup.text)) or dup.text[:90])
    check("вещь, событие и отметка поступка не появились",
          before == after and not op_recorded(org_id, "c-i-conflict"),
          f"до {before} / после {after}")

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

    # ── 7. Замки не сняты и данные целы ──────────────────────────────────────
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
