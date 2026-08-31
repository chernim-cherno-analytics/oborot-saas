# -*- coding: utf-8 -*-
"""SUPPLY-1: неизменяемый CC_BATCH_ID у партии, рождённой в «Обороте».

Зачем этот набор. Решение владельца 31.08.2026 (DECISIONS D-49) делает «Оборот»
центральной системой цепочки снабжения, и первое, что для этого нужно, — свой
идентификатор партии, который не зависит ни от одной внешней системы и не
меняется никогда. До этой правки такого понятия в продукте не было вовсе: у
заказа был только `id` — переиспользуемый rowid SQLite, который после удаления
строки достаётся СЛЕДУЮЩЕЙ партии.

Что здесь доказывается — и почему именно это:

  1) миграция аддитивна: колонка появляется у базы, созданной до правки, и
     существующие заказы получают идентификаторы (backfill двух заказов);
  2) повторный прогон ничего не переписывает — идентификатор, однажды выданный,
     остаётся тем же;
  3) строка, вставленная СТАРЫМ кодом уже ПОСЛЕ миграции, лечится следующим
     стартом. Это главный сценарий, ради которого backfill условный, а не
     разовый под флагом: деплой идёт без простоя, релиз бывает откачен, и
     рядом какое-то время живёт процесс, который про колонку не знает;
  4) год в префиксе берётся из даты самой партии, а не из «сегодня»;
  5) уникальность (org_id, cc_batch_id) работает ТОЛЬКО для непустых значений:
     две невылеченные строки одной организации законны, два одинаковых непустых
     идентификатора — нет, а одинаковый идентификатор у разных организаций
     запретом не считается (замок про партию, а не про глобальный реестр);
  6) конкурентный старт нескольких воркеров миграцию не роняет;
  7) формат нового идентификатора: `CCB-<год>-<полный uuid4 hex>`;
  8) create / list / detail / open говорят об одной партии одно и то же;
  9) склейка дубля отдаёт идентификатор ИСХОДНОГО заказа (второй партии не
     создано — значит и второго идентификатора нет), а осознанный
     `allow_duplicate` — новый;
 10) присланный клиентом cc_batch_id не принимается как выбранный;
 11) организации не видят партий друг друга;
 12) страница /replenish показывает идентификатор целиком и даёт его скопировать;
 13) непустой идентификатор нельзя переписать даже изнутри кода;
 14) в этом слое идентификатор НИКУДА не уезжает во внешние системы —
     проверяется структурно, по исходникам приложения.

Чего этот набор НЕ проверяет и не должен: формулы (D-35, BUSINESS_LOGIC §0),
«Едет», приёмки и OrderedQty — они этой правкой не тронуты. DATA-9 (`base_name`
как ключ) остаётся открытым: партия получила идентификатор, позиции внутри неё
по-прежнему ключуются именем.

Запуск из корня репозитория:  python tests/test_supply.py
"""
import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "test_supply.db"
APP_PORT = int(os.environ.get("OBOROT_TEST_PORT", "8815"))

os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["SCHEDULER_ENABLED"] = "0"

if DB_PATH.exists():
    DB_PATH.unlink()

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from sqlalchemy import create_engine, inspect as sa_inspect, text  # noqa: E402

from app import models  # noqa: E402
from app.main import app as oborot_app  # noqa: E402

# Формат идентификатора: префикс, год и ПОЛНЫЙ uuid4 hex (32 знака).
BATCH_RE = re.compile(r"^CCB-(\d{4})-([0-9a-f]{32})$")


def batch_year(value: str) -> str:
    """Год из идентификатора; '' — если формат не тот (проверка, а не падение)."""
    m = BATCH_RE.match(value or "")
    return m.group(1) if m else ""


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


def sql(query: str, *args):
    con = sqlite3.connect(DB_PATH)
    try:
        return con.execute(query, args).fetchall()
    finally:
        con.close()


def client() -> httpx.Client:
    return httpx.Client(headers={"X-Oborot-CSRF": "1"},
                        base_url=f"http://127.0.0.1:{APP_PORT}", timeout=120.0)


# ── Часть 1. Миграция и backfill на «старой» схеме (без приложения) ──────────
#
# Схема здесь пишется руками ровно в том виде, в каком её создавал код ДО этой
# правки: create_all свежей базы дал бы колонку сразу, и проверять было бы
# нечего.
_OLD_ORDERS_DDL = (
    "CREATE TABLE production_orders ("
    "id INTEGER PRIMARY KEY, org_id INTEGER NOT NULL, name VARCHAR(255) NOT NULL, "
    "created_at DATETIME, eta_date VARCHAR(10), status VARCHAR(16) NOT NULL DEFAULT 'draft', "
    "items_json TEXT NOT NULL DEFAULT '[]')"
)


def _old_schema_engine(filename: str):
    path = Path(tempfile.mkdtemp()) / filename
    eng = create_engine(f"sqlite:///{path}", future=True,
                        connect_args={"check_same_thread": False})
    with eng.begin() as conn:
        conn.execute(text(_OLD_ORDERS_DDL))
    return eng


def _batch_ids(eng) -> dict:
    with eng.connect() as conn:
        return {r[0]: r[1] for r in conn.execute(
            text("SELECT id, cc_batch_id FROM production_orders ORDER BY id")).all()}


def migration_checks() -> None:
    print("\n== Аддитивная миграция и backfill на старой схеме ==")
    eng = _old_schema_engine("old_orders_schema.db")
    with eng.begin() as conn:
        conn.execute(text(
            "INSERT INTO production_orders (id, org_id, name, created_at) "
            "VALUES (1, 1, 'Партия до Оборота', '2025-04-17 10:00:00')"))
        conn.execute(text(
            "INSERT INTO production_orders (id, org_id, name, created_at) "
            "VALUES (2, 1, 'Вторая партия', '2026-01-09 08:30:00')"))

    models.ensure_schema(bind=eng)
    cols = {c["name"] for c in sa_inspect(eng).get_columns("production_orders")}
    check("миграция добавила cc_batch_id в существующую таблицу",
          "cc_batch_id" in cols, f"cols={sorted(cols)}")

    ids = _batch_ids(eng)
    check("оба старых заказа получили идентификатор",
          all(bool(v) for v in ids.values()) and len(ids) == 2, str(ids))
    check("идентификаторы разные, а не один на всех",
          len(set(ids.values())) == 2, str(ids))
    check("оба идентификатора в каноническом формате",
          all(BATCH_RE.match(v or "") for v in ids.values()), str(ids))
    check("год в префиксе взят из даты партии, а не из «сегодня»",
          batch_year(ids[1]) == "2025" and batch_year(ids[2]) == "2026", str(ids))

    models.ensure_schema(bind=eng)          # повторный старт — идемпотентно
    again = _batch_ids(eng)
    check("повторный прогон миграции НЕ переписывает выданные идентификаторы",
          again == ids, f"было={ids} стало={again}")

    print("\n== Строка, вставленная старым кодом ПОСЛЕ миграции ==")
    # Ровно то, что делает откатившийся релиз: INSERT без колонки, значение
    # приходит из server_default=''.
    with eng.begin() as conn:
        conn.execute(text(
            "INSERT INTO production_orders (id, org_id, name, created_at) "
            "VALUES (3, 1, 'Заказ откатившегося кода', '2026-08-31 12:00:00')"))
    with eng.connect() as conn:
        raw = conn.execute(text(
            "SELECT cc_batch_id FROM production_orders WHERE id = 3")).scalar()
    check("старый код создал строку с пустым идентификатором (откат не сломан)",
          raw == "", repr(raw))

    models.ensure_schema(bind=eng)
    healed = _batch_ids(eng)
    check("следующий старт вылечил новую пустую строку",
          bool(healed.get(3)) and BATCH_RE.match(healed[3] or ""), str(healed.get(3)))
    check("и не тронул уже выданные идентификаторы соседей",
          {k: healed[k] for k in (1, 2)} == ids, f"было={ids} стало={healed}")

    print("\n== Уникальность действует только для непустых идентификаторов ==")
    with eng.begin() as conn:
        conn.execute(text(
            "INSERT INTO production_orders (id, org_id, name, created_at) "
            "VALUES (4, 1, 'Пустая А', '2026-08-31 12:00:00')"))
        conn.execute(text(
            "INSERT INTO production_orders (id, org_id, name, created_at) "
            "VALUES (5, 1, 'Пустая Б', '2026-08-31 12:00:00')"))
    with eng.connect() as conn:
        empties = conn.execute(text(
            "SELECT COUNT(*) FROM production_orders "
            "WHERE org_id = 1 AND cc_batch_id = ''")).scalar()
    check("две невылеченные строки одной организации законны (частичный индекс)",
          empties == 2, str(empties))

    taken = healed[1]
    dup_err = ""
    try:
        with eng.begin() as conn:
            conn.execute(text(
                "INSERT INTO production_orders (id, org_id, name, cc_batch_id) "
                "VALUES (6, 1, 'Дубль', :v)"), {"v": taken})
    except Exception as exc:  # noqa: BLE001 — ждём именно нарушение уникальности
        dup_err = str(exc)
    check("тот же непустой идентификатор в той же организации отвергнут базой",
          "unique" in dup_err.lower(), dup_err[:140] or "вставка прошла")

    other_org_err = ""
    try:
        with eng.begin() as conn:
            conn.execute(text(
                "INSERT INTO production_orders (id, org_id, name, cc_batch_id) "
                "VALUES (7, 2, 'Другая организация', :v)"), {"v": taken})
    except Exception as exc:  # noqa: BLE001
        other_org_err = str(exc)
    check("замок ограничен организацией и чужую строку не запрещает",
          other_org_err == "", other_org_err[:140])

    check("индекс частичный, а не обычный UNIQUE",
          "WHERE cc_batch_id <> ''" in models._CC_BATCH_ID_INDEX_DDL,
          models._CC_BATCH_ID_INDEX_DDL)
    models.ensure_schema(bind=eng)
    after = _batch_ids(eng)
    check("последний прогон вылечил и обе пустые строки, не задев прежние",
          all(after[k] for k in after) and {k: after[k] for k in (1, 2, 3)} == {
              1: healed[1], 2: healed[2], 3: healed[3]}, str(after))
    eng.dispose()

    print("\n== Таблица без created_at: миграция не имеет права падать ==")
    # Базы бывают старше любого нашего представления о «нормальной» таблице
    # заказов. Ровно такую — `(id, org_id)` и больше ничего — поднимает тест
    # гонки миграций в tests/test_sync.py, и первая версия backfill валила на
    # ней старт: она спрашивала `created_at` безусловно. Шаг старта не должен
    # опираться на колонки, которыми не управляет.
    bare_path = Path(tempfile.mkdtemp()) / "bare_orders.db"
    bare = create_engine(f"sqlite:///{bare_path}", future=True,
                         connect_args={"check_same_thread": False})
    with bare.begin() as conn:
        conn.execute(text(
            "CREATE TABLE production_orders (id INTEGER PRIMARY KEY, org_id INTEGER NOT NULL)"))
        conn.execute(text(
            "INSERT INTO production_orders (id, org_id) VALUES (1, 1), (2, 1)"))
    bare_err = ""
    try:
        models.ensure_schema(bind=bare)
    except Exception as exc:  # noqa: BLE001 — падение здесь и есть проверяемый дефект
        bare_err = f"{type(exc).__name__}: {exc}"
    check("миграция на таблице без created_at не падает", bare_err == "", bare_err[:160])
    bare_ids = _batch_ids(bare) if not bare_err else {}
    check("и партии всё равно получили имена",
          len(bare_ids) == 2 and all(BATCH_RE.match(v or "") for v in bare_ids.values()),
          str(bare_ids))
    bare.dispose()

    print("\n== Конкурентный старт нескольких воркеров ==")
    eng2 = _old_schema_engine("old_orders_concurrent.db")
    with eng2.begin() as conn:
        for i in (1, 2, 3):
            conn.execute(text(
                "INSERT INTO production_orders (id, org_id, name, created_at) "
                f"VALUES ({i}, 1, 'Партия {i}', '2026-02-02 10:00:00')"))
    errors = []

    def _run():
        try:
            models.ensure_schema(bind=eng2)
        except Exception as exc:  # noqa: BLE001 — гонка не должна ронять воркер
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda _: _run(), range(6)))
    check("конкурентная миграция не падает", not errors, str(errors)[:200])
    conc = _batch_ids(eng2)
    check("каждая строка получила ровно один идентификатор",
          len(conc) == 3 and all(BATCH_RE.match(v or "") for v in conc.values()),
          str(conc))
    check("и все три различны", len(set(conc.values())) == 3, str(conc))
    names = [c["name"] for c in sa_inspect(eng2).get_columns("production_orders")]
    check("колонка добавлена ровно один раз, без дублей",
          names.count("cc_batch_id") == 1, str(names))
    eng2.dispose()


# ── Часть 2. Идентификатор снаружи: API, страница, границы ───────────────────

def api_checks() -> None:  # noqa: C901 — сценарный тест: шагов много, ветвлений мало
    c = client()

    print("\n== Подготовка ==")
    r = c.post("/register", data={"name": "Владелец", "email": "supply@test.io",
                                  "password": "secret123", "org_name": "Бренд-П"})
    check("регистрация", r.status_code in (200, 302, 303), f"status={r.status_code}")
    check("демо-данные", c.post("/api/connect/demo").status_code == 200)

    rows = c.get("/api/turnover").json().get("items") or []
    base = str((rows[0] if rows else {}).get("base_name") or "")
    check("имя позиции из каталога получено", bool(base), str(rows)[:120])

    print("\n== Новая партия получает серверный идентификатор ==")
    r = c.post("/api/orders", json={"name": "Партия №1", "items": [
        {"base_name": base, "qty": 10, "sizes": {}, "cost": 100}]})
    check("заказ создан", r.status_code == 200, r.text[:150])
    created = r.json()
    order_id, batch = created.get("id"), created.get("cc_batch_id") or ""
    check("POST /api/orders вернул cc_batch_id", bool(batch), json.dumps(created)[:160])
    m = BATCH_RE.match(batch)
    check("формат идентификатора: CCB-<год>-<полный uuid hex>", bool(m), batch)
    check("uuid не обрезан — ровно 32 знака", bool(m) and len(m.group(2)) == 32, batch)
    check("длина укладывается в объявленные 48 символов",
          len(batch) <= models.CC_BATCH_ID_MAX_LEN, f"len={len(batch)}")

    print("\n== Одна партия — один идентификатор во всех ручках ==")
    detail = c.get(f"/api/orders/{order_id}").json()
    check("GET /api/orders/{id} отдаёт тот же идентификатор",
          detail.get("cc_batch_id") == batch, str(detail.get("cc_batch_id")))
    listed = c.get("/api/orders").json().get("orders") or []
    mine = next((o for o in listed if o.get("id") == order_id), None)
    check("GET /api/orders отдаёт тот же идентификатор",
          mine is not None and mine.get("cc_batch_id") == batch,
          str(mine and mine.get("cc_batch_id")))
    opened = c.get("/api/orders/open").json().get("orders") or []
    mine_open = next((o for o in opened if o.get("id") == order_id), None)
    check("GET /api/orders/open отдаёт тот же идентификатор",
          mine_open is not None and mine_open.get("cc_batch_id") == batch,
          str(mine_open and mine_open.get("cc_batch_id")))
    stored = sql("SELECT cc_batch_id FROM production_orders WHERE id = ?", order_id)
    check("в базе лежит ровно он же",
          stored and stored[0][0] == batch, str(stored))

    print("\n== Склейка дубля не создаёт второй партии ==")
    r = c.post("/api/orders", json={"name": "Партия №1", "items": [
        {"base_name": base, "qty": 10, "sizes": {}, "cost": 100}]})
    dup = r.json()
    check("повтор распознан как дубль", dup.get("duplicate") is True, r.text[:150])
    check("дубль вернул идентификатор ИСХОДНОЙ партии",
          dup.get("cc_batch_id") == batch and dup.get("id") == order_id,
          json.dumps(dup, ensure_ascii=False)[:180])
    total = sql("SELECT COUNT(*) FROM production_orders WHERE org_id = 1")[0][0]
    check("второй строки заказа не появилось", total == 1, str(total))

    print("\n== Осознанный повтор — это НОВАЯ партия ==")
    r = c.post("/api/orders", json={"name": "Партия №1", "allow_duplicate": True,
                                    "items": [
        {"base_name": base, "qty": 10, "sizes": {}, "cost": 100}]})
    second = r.json()
    check("allow_duplicate создал новый заказ",
          r.status_code == 200 and second.get("id") != order_id, r.text[:150])
    check("и у него ДРУГОЙ идентификатор партии",
          bool(second.get("cc_batch_id")) and second["cc_batch_id"] != batch,
          str(second.get("cc_batch_id")))
    check("новый идентификатор тоже канонического формата",
          bool(BATCH_RE.match(second.get("cc_batch_id") or "")),
          str(second.get("cc_batch_id")))

    print("\n== Идентификатор выбирает сервер, а не клиент ==")
    forged = "CCB-1999-" + "de" * 16
    r = c.post("/api/orders", json={"name": "Партия с чужим id",
                                    "cc_batch_id": forged, "items": [
        {"base_name": base, "qty": 7, "sizes": {}, "cost": 100}]})
    check("заказ создан", r.status_code == 200, r.text[:150])
    forced = r.json()
    check("присланный клиентом идентификатор не принят",
          forced.get("cc_batch_id") != forged, str(forced.get("cc_batch_id")))
    check("выдан серверный идентификатор канонического формата",
          bool(BATCH_RE.match(forced.get("cc_batch_id") or "")),
          str(forced.get("cc_batch_id")))
    check("и в базе лежит серверный, а не присланный",
          not sql("SELECT id FROM production_orders WHERE cc_batch_id = ?", forged),
          forged)

    print("\n== Партия, выросшая из плана, тоже названа ==")
    r = c.post("/api/order-plan", json={"budget": 150000, "budget_scope": "now",
                                        "cadence_days": 30, "safety_days": 14})
    check("план посчитан", r.status_code == 200, r.text[:150])
    plan_id = r.json().get("id")
    # force=True: этот набор проверяет идентификатор партии, а не защиту от
    # повторного заказа — а у организации выше уже созданы заказы, и совпадение
    # состава сделало бы шаг то зелёным, то 409-м в зависимости от каталога.
    r = c.post(f"/api/order-plan/{plan_id}/apply",
               json={"name": "Партия из плана", "force": True})
    check("заказ из плана создан", r.status_code == 200, r.text[:150])
    from_plan = r.json()
    check("apply вернул идентификатор партии",
          bool(BATCH_RE.match(from_plan.get("cc_batch_id") or "")),
          str(from_plan.get("cc_batch_id")))
    plan_detail = c.get(f"/api/orders/{from_plan.get('order_id')}").json()
    check("и карточка заказа отдаёт ровно его",
          plan_detail.get("cc_batch_id") == from_plan.get("cc_batch_id"),
          str(plan_detail.get("cc_batch_id")))

    print("\n== Прежнее поведение заказов не тронуто ==")
    r = c.post(f"/api/orders/{order_id}/status", json={"status": "sent"})
    check("перевод в производство работает как раньше", r.status_code == 200, r.text[:150])
    sent = c.get(f"/api/orders/{order_id}").json()
    check("статус сменился, идентификатор партии остался прежним",
          sent.get("status") == "sent" and sent.get("cc_batch_id") == batch,
          f"{sent.get('status')} / {sent.get('cc_batch_id')}")
    r = c.post(f"/api/orders/{order_id}/status", json={"status": "received"})
    check("приёмка одним кликом работает как раньше", r.status_code == 200, r.text[:150])
    received = c.get(f"/api/orders/{order_id}").json()
    check("и после приёмки идентификатор тот же",
          received.get("cc_batch_id") == batch, str(received.get("cc_batch_id")))

    print("\n== Страница /replenish показывает идентификатор ==")
    page = c.get("/replenish")
    check("страница отдаётся", page.status_code == 200, f"status={page.status_code}")
    html = page.text
    check("реестр заказов выводит cc_batch_id", "o.cc_batch_id" in html,
          "в разметке страницы нет ссылки на поле партии")
    check("идентификатор подписан словом «Партия»", ">Партия<" in html,
          "подписи нет — число без имени читателю ничего не говорит")
    check("рядом есть кнопка копирования", "ord-copy" in html and "copyBatchId" in html,
          "копировать идентификатор нечем")
    check("значение выводится целиком, без обрезки",
          "esc(bid)" in html and "word-break: break-all" in html,
          "идентификатор показан не полностью")
    check("прежние кнопки статусов на месте",
          "ord-send" in html and "ord-recv" in html and "ord-del" in html,
          "управление заказами пропало со страницы")

    print("\n== Организации не видят партий друг друга ==")
    c2 = client()
    c2.post("/register", data={"name": "Второй", "email": "supply2@test.io",
                               "password": "secret123", "org_name": "Бренд-Р"})
    c2.post("/api/connect/demo")
    rows2 = c2.get("/api/turnover").json().get("items") or []
    base2 = str((rows2[0] if rows2 else {}).get("base_name") or "")
    r = c2.post("/api/orders", json={"name": "Чужая партия", "items": [
        {"base_name": base2, "qty": 5, "sizes": {}, "cost": 100}]})
    check("вторая организация создала свою партию", r.status_code == 200, r.text[:150])
    foreign = r.json()
    check("её идентификатор отличается от нашего",
          foreign.get("cc_batch_id") != batch, str(foreign.get("cc_batch_id")))
    theirs = {o.get("cc_batch_id") for o in (c2.get("/api/orders").json().get("orders") or [])}
    ours = {o.get("cc_batch_id") for o in (c.get("/api/orders").json().get("orders") or [])}
    check("в списке второй организации нет наших партий", not (theirs & ours),
          str(sorted(theirs & ours))[:160])
    check("и наша карточка чужого заказа не отдаётся",
          c.get(f"/api/orders/{foreign.get('id')}").status_code == 404,
          str(c.get(f"/api/orders/{foreign.get('id')}").status_code))

    print("\n== Непустой идентификатор неизменяем изнутри кода ==")
    order = models.ProductionOrder(org_id=1, name="Проба")
    order.cc_batch_id = models.new_cc_batch_id()
    err = ""
    try:
        order.cc_batch_id = models.new_cc_batch_id()
    except ValueError as exc:
        err = str(exc)
    check("попытка заменить выданный идентификатор — ошибка",
          "неизменяем" in err, err[:140] or "замена прошла молча")

    print("\n== Первый слой никуда идентификатор не отправляет ==")
    # Граница пакета структурная: модули внешних интеграций про поле не знают
    # вовсе, поэтому «мы его не отправляем» — не обещание, а проверяемый факт.
    external = sorted(
        p.name for p in (ROOT / "app").glob("*.py")
        if "cc_batch_id" in p.read_text(encoding="utf-8")
        and p.name not in ("models.py", "api.py")
    )
    check("ни один модуль интеграций не упоминает cc_batch_id",
          not external, f"упоминают: {external}")
    check("модуль обратной записи в МойСклад про поле не знает",
          "cc_batch_id" not in (ROOT / "app" / "ms_writeback.py").read_text(encoding="utf-8"))
    check("модуль синхронизации с МойСкладом — тоже",
          "cc_batch_id" not in (ROOT / "app" / "ms_sync.py").read_text(encoding="utf-8"))


def run() -> int:
    migration_checks()
    api_checks()
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
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(DB_PATH) + suffix)
            if p.exists():
                p.unlink()


if __name__ == "__main__":
    sys.exit(main())
