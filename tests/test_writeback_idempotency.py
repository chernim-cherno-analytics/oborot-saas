# -*- coding: utf-8 -*-
"""DATA-1/DATA-2: идемпотентность отправки заказа поставщику и атомарный T2.

Что защищает этот набор. Отправка «Заказа поставщику» в МойСклад — создание
ФИНАНСОВОГО документа: за ним стоят деньги и обещание подрядчику. У сети три
исхода, а не два — «создан», «не создан» и «НЕИЗВЕСТНО». Прежняя защита от
дубля держалась на одном признаке: машинной метке `[oborot#<id>]` в описании
документа. Метка живёт в тексте, который правит человек, а `id` заказа —
rowid SQLite, который переиспользуется после удаления строки. Отсюда два
дефекта, которые здесь и проверяются:

  • НОВЫЙ заказ с переиспользованным rowid находил по метке ЧУЖОЙ (старый)
    документ и «усыновлял» его: заказ считался отправленным, документа под
    него не существовало, «едет к нам» считалось по чужой бумаге;
  • попытка, умершая ПОСЛЕ захвата лока, но ДО POST, при повторе шла тем же
    путём — то есть снова через поиск по метке.

Решение (ACK Codex по DATA-1/DATA-2):
  T1 — стабильный uuid4 `ms_sync_id` рождается и коммитится ВМЕСТЕ с
       CAS-пометкой pending, ДО единственного сетевого вызова; он же уходит
       в МойСклад полем `syncId` (пользовательский идентификатор JSON API
       1.2: повторный POST с занятым syncId обновляет существующий документ,
       а не создаёт второй);
  явный дискриминатор `ms_lookup_mode` — существующие на момент миграции
       строки помечены `legacy` (им поиск по метке ещё разрешён), новые
       рождаются `sync` и по метке НЕ ищутся НИКОГДА;
  T2 — CAS pending→href и перенос вклада «едет к нам» одной транзакцией:
       либо оба, либо ни один; фолбэка «сохранить только href» нет;
  back-match — синк заказов поставщику связывает документ с заказом по
       syncId, если предыдущая отправка закончилась честным unknown.

Раунд 1 ревью Codex добавил сюда две гонки внутри сетевого окна между T1 и
T2 — окна, в котором заказ ещё выглядит обычным и доступен для правки:

  • draft → sent во время отправки (блок 10). Черновик локально не считался,
    переход добавляет полный локальный вклад, а T2 идёт со СТАРЫМ status в
    памяти и вклад не снимает — одно и то же едет к нам дважды, навсегда;
  • DELETE во время отправки (блок 11). Строка исчезает между T1 и созданием
    документа, T2 получает rowcount=0 — в МойСкладе остаётся финансовый
    документ, у которого нет заказа, а ключ для back-match удалён вместе со
    строкой.

Обе обязаны иметь ОДИН победивший исход и честный 409 проигравшему; простая
проверка «нет ли pending» перед изменением от этого не спасает — между
проверкой и изменением T1 успевает встать (TOCTOU), поэтому условие живёт
в самой изменяющей SQL-операции.

Раунд 2 ревью Codex довёл это правило до конца — блоки 11б и 12:

  • обратный порядок удаления (11б): DELETE прочитал допустимый заказ, и
    только ПОСЛЕ этого T1 поставил pending. Блок 11 такой формы не покрывал:
    он открывал окно первым, то есть проверял уже готовое состояние;
  • приёмка против удаления (12): запрет «принятый заказ удалить нельзя»
    жил проверкой ПЕРЕД удалением и держался ровно до первой гонки —
    sent → received успевает закоммититься между чтением и DELETE, и
    удаление сносит принятый заказ вместе с фактами приёмки. Зеркальный
    случай: победило удаление — проигравший переход отвечал «ok, unchanged»,
    успехом, которого не было.

Отсюда требование к нулевому результату изменяющей операции: он обязан
РАЗЛИЧАТЬ исходы, а не сводиться к одному коду. Строки нет — 404; стала
received — 422; идёт отправка — 409.

Мок закрепляет ожидаемое поведение чужого API, но доказательством живого
контракта НЕ является: живой тест `syncId` на боевом аккаунте — отдельный
merge gate (см. TECH_DEBT, DATA-1/DATA-2).

Запуск из корня репозитория:  python tests/test_writeback_idempotency.py
"""
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

DB_PATH = ROOT / "test_wb_idem.db"
APP_PORT = int(os.environ.get("OBOROT_TEST_PORT", "8810"))
MOCK_PORT = int(os.environ.get("OBOROT_MOCK_PORT", "9813"))

os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["MS_BASE_URL"] = f"http://127.0.0.1:{MOCK_PORT}"
os.environ["SCHEDULER_ENABLED"] = "0"
os.environ["HISTORY_DAYS"] = "30"
os.environ["INITIAL_WINDOW_DAYS"] = "10"
os.environ["STOCK_CHUNK_DATES"] = "10"
os.environ["MS_CHUNK_PAUSE"] = "0"

if DB_PATH.exists():
    DB_PATH.unlink()

import httpx  # noqa: E402
import uvicorn  # noqa: E402

import mock_ms  # noqa: E402
from app import ms_writeback  # noqa: E402
from app.main import app as oborot_app  # noqa: E402
from app.ms_writeback import order_marker  # noqa: E402

mock_ms.PORT = MOCK_PORT


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


# ── Доступ к базе напрямую ───────────────────────────────────────────────────
#
# Часть проверок смотрит на КОЛОНКИ, которых до этой правки не существует.
# Отсутствие колонки — это и есть «красный» результат, а не авария набора:
# поэтому запросы возвращают маркер MISSING, а не роняют прогон.

MISSING = "<нет колонки>"


def exec_sql(query: str, *args) -> str:
    con = sqlite3.connect(DB_PATH)
    try:
        con.execute(query, args)
        con.commit()
        return ""
    except sqlite3.OperationalError as exc:
        return str(exc)
    finally:
        con.close()


def col_of(order_id: int, column: str):
    con = sqlite3.connect(DB_PATH)
    try:
        row = con.execute(
            f"SELECT {column} FROM production_orders WHERE id=?", (order_id,)
        ).fetchone()
        return row[0] if row else None
    except sqlite3.OperationalError:
        return MISSING
    finally:
        con.close()


def conn_col(column: str):
    con = sqlite3.connect(DB_PATH)
    try:
        row = con.execute(
            f"SELECT {column} FROM connections WHERE kind='moysklad'"
        ).fetchone()
        return row[0] if row else None
    except sqlite3.OperationalError:
        return MISSING
    finally:
        con.close()


def qty_map() -> dict:
    """{base_name: (qty, ms_qty, ms_qty_tracked)} — три величины «едет к нам»."""
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        return {r["base_name"]: (r["qty"], r["ms_qty"], r["ms_qty_tracked"])
                for r in con.execute("SELECT base_name, qty, ms_qty, "
                                     "ms_qty_tracked FROM ordered_qty")}
    finally:
        con.close()


def push(c: httpx.Client, order_id: int) -> httpx.Response:
    """POST push-to-ms с одним повтором на разрыв keep-alive.

    После ответа 500 uvicorn закрывает соединение, а httpx успевает взять его
    из пула и получить ECONNRESET. Это шум транспорта на стороне теста, а не
    поведение приложения: повторяем ровно один раз.
    """
    try:
        return c.post(f"/api/orders/{order_id}/push-to-ms")
    except httpx.TransportError:
        return c.post(f"/api/orders/{order_id}/push-to-ms")


def wait_pending(order_id: int, timeout: float = 20.0) -> str:
    """Ждёт, пока T1 закоммитит пометку «идёт отправка», и возвращает её.

    Это точка синхронизации теста, а не sleep наугад: пометка появляется в
    базе ровно между T1 и сетью, и увидев её, тест знает, что окно открыто.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        href = str(col_of(order_id, "ms_doc_href") or "")
        if href.startswith("pending:"):
            return href
        time.sleep(0.02)
    return str(col_of(order_id, "ms_doc_href") or "")


def order_exists(order_id: int) -> bool:
    con = sqlite3.connect(DB_PATH)
    try:
        return con.execute("SELECT 1 FROM production_orders WHERE id=?",
                           (order_id,)).fetchone() is not None
    finally:
        con.close()


def status_of(order_id: int) -> str:
    return str(col_of(order_id, "status") or "")


def receipts_count(order_id: int) -> int:
    """Сколько строк приёмки лежит по заказу (факты исполнения, D-25).

    Приёмка — факт, а не мнение: она обязана пережить проигранную гонку
    удаления и обязана НЕ появиться, если переход в «принят» не состоялся.
    """
    con = sqlite3.connect(DB_PATH)
    try:
        row = con.execute("SELECT COUNT(*) FROM order_receipts WHERE order_id=?",
                          (order_id,)).fetchone()
        return int(row[0]) if row else 0
    except sqlite3.OperationalError:
        return -1
    finally:
        con.close()


def wait_sync_done(c: httpx.Client, timeout: float = 240.0) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        last = c.get("/api/sync/status").json()
        if last.get("state") in ("done", "error"):
            return last
        time.sleep(0.5)
    return last


# ── Работа с моком ───────────────────────────────────────────────────────────

def docs_created() -> list:
    return mock_ms.CREATED_PURCHASE_ORDERS


def doc_with_marker(marker: str):
    for d in docs_created():
        if marker in str(d.get("description") or ""):
            return d
    return None


def plant_doc(doc_id: str, description: str, *, sync_id: str = "",
              positions=(), applicable: bool = True) -> dict:
    """Кладёт в мок «чужой» заказ поставщику (как будто его завёл человек)."""
    doc = {
        "id": doc_id, "name": doc_id.upper(), "applicable": applicable,
        "moment": f"{mock_ms.TODAY.isoformat()} 09:00:00",
        "description": description,
        "meta": {"href": f"{mock_ms.BASE}/entity/purchaseorder/{doc_id}",
                 "type": "purchaseorder"},
        "positions": {"rows": list(positions),
                      "meta": {"size": len(positions)}},
    }
    if sync_id:
        doc["syncId"] = sync_id
    mock_ms.PURCHASE_ORDERS.append(doc)
    return doc


def unplant(doc: dict) -> None:
    if doc in mock_ms.PURCHASE_ORDERS:
        mock_ms.PURCHASE_ORDERS.remove(doc)


# ── Заказы ───────────────────────────────────────────────────────────────────

_BASES: list = []
_NEXT_BASE = {"i": 0}


def sized_bases() -> list:
    """[(base_name, [size, ...])] по товарам с ext_id — из чего собирать заказ.

    Набор делает больше десятка отправок, а /replenish после первых же push
    перестаёт рекомендовать (вклад уехал в «едет к нам») и отдаёт пустой
    список. Позиции берём прямо из синхронизированных товаров: тесту нужен
    заказ, который сопоставится с ассортиментом МС, а не «правильная»
    рекомендация.
    """
    if _BASES:
        return _BASES
    con = sqlite3.connect(DB_PATH)
    try:
        rows = con.execute("SELECT base_name, size, ext_id FROM products "
                           "WHERE ext_id != '' ORDER BY base_name, size").fetchall()
    finally:
        con.close()
    grouped: dict = {}
    for base, size, ext in rows:
        # Родительский product вариантной модели тоже приезжает в ассортименте,
        # но заказать его нельзя (МойСклад принимает только вариант) — мок
        # отвечает на такую позицию 412. В заказ берём только то, что реально
        # существует как складская позиция.
        if ext not in mock_ms.SKU_BY_EXT:
            continue
        grouped.setdefault(base, []).append(size)
    _BASES.extend(sorted(grouped.items()))
    return _BASES


def make_order(c: httpx.Client, name: str, base: str = "") -> int:
    """Заказ на один base_name (по кругу, чтобы блоки не мешали друг другу)."""
    bases = sized_bases()
    assert bases, "в базе нет товаров с ext_id — синк не отработал"
    if base:
        sizes = dict(bases)[base]
    else:
        base, sizes = bases[_NEXT_BASE["i"] % len(bases)]
        _NEXT_BASE["i"] += 1
    picked = {s: 1 for s in sizes[:2]}
    payload = [{"base_name": base, "qty": sum(picked.values()),
                "sizes": picked, "cost": 100}]
    r = c.post("/api/orders", json={"name": name, "eta_date": None,
                                   "items": payload, "allow_duplicate": True})
    assert r.status_code == 200, (name, r.status_code, r.text[:200])
    return int(r.json()["id"])


def order_bases(c: httpx.Client, order_id: int) -> dict:
    """{base_name: qty} позиций заказа — сколько уедет в «едет к нам»."""
    con = sqlite3.connect(DB_PATH)
    try:
        row = con.execute("SELECT items_json FROM production_orders WHERE id=?",
                          (order_id,)).fetchone()
    finally:
        con.close()
    import json as _json
    out: dict = {}
    for item in _json.loads(row[0] or "[]"):
        base = str(item.get("base_name") or "")
        sizes = item.get("sizes") or {}
        qty = sum(int(v or 0) for v in sizes.values()) or int(item.get("qty") or 0)
        if base and qty:
            out[base] = out.get(base, 0) + qty
    return out


def main() -> int:
    mock_srv = ServerThread(mock_ms.app, MOCK_PORT)
    app_srv = ServerThread(oborot_app, APP_PORT)
    mock_srv.start()
    app_srv.start()
    try:
        return run()
    finally:
        app_srv.stop()
        mock_srv.stop()
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(DB_PATH) + suffix)
            if p.exists():
                p.unlink()


def run() -> int:  # noqa: C901 — сценарный набор, читается сверху вниз
    mock_ms.reset_writeback_state()
    mock_ms.reset_faults()
    base = f"http://127.0.0.1:{APP_PORT}"
    c = httpx.Client(headers={"X-Oborot-CSRF": "1"}, base_url=base, timeout=120.0)
    mock_api = httpx.Client(base_url=f"http://127.0.0.1:{MOCK_PORT}", timeout=30.0)

    print("\n== Подготовка ==")
    r = c.post("/register", data={"name": "Владелец", "email": "owner@idem.io",
                                  "password": "secret123", "org_name": "Идем-бренд"})
    check("регистрация", r.status_code in (200, 302, 303), f"status={r.status_code}")
    r = c.post("/api/connect/moysklad", json={"token": mock_ms.TOKEN})
    check("токен принят", r.status_code == 200, f"status={r.status_code}")
    c.post("/api/connect/moysklad/stores", json={"ext_ids": ["st-flag", "st-web"]})
    c.post("/api/sync/initial")
    st = wait_sync_done(c)
    check("первичный синк завершился", st.get("state") == "done",
          f"state={st.get('state')}")

    # ── 1. T1: ключ идемпотентности рождается ДО сети и переживает смерть ────
    #
    # «Смерть после T1» смоделирована честно: POST отвечает 502, НЕ создав
    # документа. Из позиции клиента этот исход неотличим от «создан, ответ
    # потерян», и именно поэтому повтор обязан идти с ТЕМ ЖЕ ключом.
    print("\n== 1. Ключ идемпотентности создаётся до сети и не меняется ==")
    o1 = make_order(c, "Заказ с ключом")
    check("до отправки ключ пуст", col_of(o1, "ms_sync_id") in ("", None),
          f"ms_sync_id={col_of(o1, 'ms_sync_id')!r}")
    check("новый заказ помечен как НЕ-legacy (поиск по метке ему запрещён)",
          col_of(o1, "ms_lookup_mode") == "sync",
          f"ms_lookup_mode={col_of(o1, 'ms_lookup_mode')!r}")
    mock_api.post("/__test/faults", json={"po_fail_before_create": 1})
    r = push(c, o1)
    check("отправка сорвалась (502), документа нет", r.status_code == 502,
          f"status={r.status_code} {r.text[:160]}")
    check("документ в МойСкладе не появился", len(docs_created()) == 0,
          f"создано={len(docs_created())}")
    key1 = col_of(o1, "ms_sync_id")
    check("ключ уцелел после сорванной попытки (T1 закоммичен до сети)",
          isinstance(key1, str) and key1 not in ("", MISSING, None),
          f"ms_sync_id={key1!r}")
    check("лок снят — повтор возможен",
          (col_of(o1, "ms_doc_href") or "") == "",
          f"ms_doc_href={col_of(o1, 'ms_doc_href')!r}")
    mock_api.post("/__test/faults", json={})
    r = push(c, o1)
    check("повтор прошёл (200)", r.status_code == 200,
          f"status={r.status_code} {r.text[:160]}")
    check("ключ ТОТ ЖЕ, что до смерти попытки", col_of(o1, "ms_sync_id") == key1,
          f"было={key1!r} стало={col_of(o1, 'ms_sync_id')!r}")
    check("создан ровно один документ", len(docs_created()) == 1,
          f"создано={len(docs_created())}")
    check("документ несёт наш ключ идемпотентности",
          docs_created() and docs_created()[0].get("syncId") == key1,
          f"syncId={docs_created()[0].get('syncId') if docs_created() else None!r}")

    # ── 2. Потерянный ответ + вычищенное описание ────────────────────────────
    print("\n== 2. Ответ потерян, описание документа вычищено человеком ==")
    o2 = make_order(c, "Заказ с потерянным ответом")
    mock_api.post("/__test/faults", json={"po_create_then_fail": 1})
    r = push(c, o2)
    check("push успешен несмотря на потерянный ответ", r.status_code == 200,
          f"status={r.status_code} {r.text[:160]}")
    check("документов стало два (по одному на заказ)", len(docs_created()) == 2,
          f"создано={len(docs_created())}")
    mock_api.post("/__test/faults", json={})
    d2 = doc_with_marker(order_marker(o2))
    check("документ второго заказа найден в моке", d2 is not None)
    if d2 is not None:
        # Человек переписал описание — единственный прежний признак исчез.
        d2["description"] = "Пошив, партия к осени"
    exec_sql("UPDATE production_orders SET ms_doc_href='pending:1' WHERE id=?", o2)
    r = push(c, o2)
    check("повтор после чистки описания прошёл", r.status_code == 200,
          f"status={r.status_code} {r.text[:160]}")
    check("ВТОРОГО ДОКУМЕНТА НЕ ПОЯВИЛОСЬ (ключ пережил чистку описания)",
          len(docs_created()) == 2, f"создано={len(docs_created())}")
    check("заказ привязан к тому же документу",
          d2 is not None
          and (col_of(o2, "ms_doc_href") or "").endswith(str(d2.get("id"))),
          f"href={col_of(o2, 'ms_doc_href')!r} doc={d2 and d2.get('id')}")

    # ── 3. Переиспользованный rowid ──────────────────────────────────────────
    #
    # SQLite выдаёт удалённый rowid следующему INSERT. Заказ удалили, новый
    # получил тот же id — и метка `[oborot#<id>]` старого документа стала
    # «его» меткой. Поиск по метке усыновлял чужую бумагу.
    print("\n== 3. Новый заказ с переиспользованным rowid ==")
    o3 = make_order(c, "Заказ, который потом удалят")
    r = push(c, o3)
    check("заказ отправлен", r.status_code == 200,
          f"status={r.status_code} {r.text[:200]}")
    old_href = col_of(o3, "ms_doc_href")
    before = len(docs_created())
    err = exec_sql("DELETE FROM production_orders WHERE id=?", o3)
    check("строка заказа удалена", err == "", err)
    o3b = make_order(c, "Новый заказ на том же rowid")
    check("SQLite переиспользовал rowid удалённого заказа", o3b == o3,
          f"старый={o3} новый={o3b}")
    r = push(c, o3b)
    check("отправка нового заказа прошла", r.status_code == 200,
          f"status={r.status_code} {r.text[:160]}")
    body3 = r.json() if r.status_code == 200 else {}
    check("НОВЫЙ ЗАКАЗ НЕ УСЫНОВИЛ СТАРЫЙ ДОКУМЕНТ",
          str(body3.get("ms_doc_href") or "") != str(old_href or "_"),
          f"старый={old_href} привязан={body3.get('ms_doc_href')}")
    check("…и он именно СОЗДАН, а не «подобран по метке»",
          body3.get("recovered") is False, f"recovered={body3.get('recovered')}")
    check("в МойСкладе появился новый документ",
          len(docs_created()) == before + 1,
          f"было={before} стало={len(docs_created())}")

    # ── 4. Смерть после T1 при живом «двойнике» с той же меткой ──────────────
    print("\n== 4. Смерть после T1 + чужой документ с той же меткой ==")
    o4 = make_order(c, "Заказ рядом с двойником")
    twin = plant_doc("po-alien-4", f"Чужая бумага {order_marker(o4)}")
    try:
        mock_api.post("/__test/faults", json={"po_fail_before_create": 1})
        r = push(c, o4)
        check("первая попытка сорвалась", r.status_code == 502,
              f"status={r.status_code}")
        check("сорванная попытка НЕ привязала заказ к чужому документу",
              "po-alien-4" not in str(col_of(o4, "ms_doc_href") or ""),
              f"ms_doc_href={col_of(o4, 'ms_doc_href')!r}")
        key4 = col_of(o4, "ms_sync_id")
        check("ключ записан", isinstance(key4, str) and key4 not in ("", MISSING),
              f"ms_sync_id={key4!r}")
        mock_api.post("/__test/faults", json={})
        before4 = len(docs_created())
        r = push(c, o4)
        check("повтор прошёл", r.status_code == 200,
              f"status={r.status_code} {r.text[:160]}")
        body4 = r.json() if r.status_code == 200 else {}
        check("ключ не менялся между попытками", col_of(o4, "ms_sync_id") == key4,
              f"было={key4!r} стало={col_of(o4, 'ms_sync_id')!r}")
        check("ЗАКАЗ ПОСЛЕ СМЕРТИ T1 НЕ УШЁЛ В ПОИСК ПО МЕТКЕ",
              "po-alien-4" not in str(col_of(o4, "ms_doc_href") or ""),
              f"привязан={col_of(o4, 'ms_doc_href')!r} ответ={body4.get('ms_doc_href')}")
        check("создан свой документ", len(docs_created()) == before4 + 1,
              f"было={before4} стало={len(docs_created())}")
    finally:
        unplant(twin)

    # ── 5. Legacy-строка: старый документ по-прежнему подбирается ────────────
    #
    # Обратная сторона правила. Заказы, существовавшие ДО этой правки, могли
    # уже создать документ, у которого нет ни syncId, ни сохранённой у нас
    # ссылки. Единственный след — метка в описании, и отнимать у таких строк
    # поиск по метке нельзя: иначе повтор создаст дубль. Поэтому дискриминатор
    # ЯВНЫЙ и выставляется миграцией, а не выводится из «непустого поля».
    print("\n== 5. Явная legacy-строка: восстановление по метке разрешено ==")
    o5 = make_order(c, "Заказ времён до правки")
    legacy_doc = plant_doc("po-legacy-5",
                           f"Создано в «Обороте»: заказ «Старый» {order_marker(o5)}")
    try:
        err = exec_sql("UPDATE production_orders SET ms_lookup_mode='legacy' "
                       "WHERE id=?", o5)
        check("строку можно явно пометить как legacy", err == "", err)
        before5 = len(docs_created())
        r = push(c, o5)
        body5 = r.json() if r.status_code == 200 else {}
        check("legacy-заказ подобрал свой старый документ",
              r.status_code == 200 and body5.get("recovered") is True,
              f"status={r.status_code} recovered={body5.get('recovered')}")
        check("…именно тот, что лежал в МойСкладе",
              "po-legacy-5" in str(body5.get("ms_doc_href") or ""),
              f"href={body5.get('ms_doc_href')}")
        check("дубля не создано", len(docs_created()) == before5,
              f"было={before5} стало={len(docs_created())}")
        check("после безопасной привязки legacy снят",
              col_of(o5, "ms_lookup_mode") == "sync",
              f"ms_lookup_mode={col_of(o5, 'ms_lookup_mode')!r}")
    finally:
        unplant(legacy_doc)

    # ── 6. T2 атомарен: ссылка и перенос вклада — вместе или никак ───────────
    print("\n== 6. Атомарность T2 (ссылка + перенос «едет к нам») ==")
    o6 = make_order(c, "Заказ со сбоем записи")
    bases6 = order_bases(c, o6)
    b6 = next(iter(bases6), "")
    before_qty = qty_map().get(b6, (0.0, 0.0, 0.0))
    original_move = ms_writeback._move_incoming_to_ms
    calls = {"n": 0}

    def _always_fail(*a, **kw):
        calls["n"] += 1
        raise RuntimeError("смоделированный сбой записи в базу")

    ms_writeback._move_incoming_to_ms = _always_fail
    try:
        before6 = len(docs_created())
        r = push(c, o6)
        check("сбой T2 отдаёт 502, а не 500", r.status_code == 502,
              f"status={r.status_code} {r.text[:200]}")
        detail6 = str((r.json() or {}).get("detail") or "") if r.status_code < 500 \
            else r.text
        check("исход назван ЧЕСТНО: «не создан» не утверждается",
              "не создан" not in detail6, f"detail={detail6[:220]}")
        check("документ в МойСкладе всё же создан", len(docs_created()) == before6 + 1,
              f"было={before6} стало={len(docs_created())}")
        check("ссылка НЕ сохранена (T2 откатился целиком)",
              (col_of(o6, "ms_doc_href") or "") in ("", MISSING)
              or str(col_of(o6, "ms_doc_href")).startswith("pending:"),
              f"ms_doc_href={col_of(o6, 'ms_doc_href')!r}")
        after_fail = qty_map().get(b6, (0.0, 0.0, 0.0))
        check("и перенос вклада тоже НЕ произошёл",
              abs(after_fail[1] - before_qty[1]) < 1e-6,
              f"ms_qty было={before_qty[1]} стало={after_fail[1]}")
        check("ключ идемпотентности сохранён для повтора",
              col_of(o6, "ms_sync_id") not in ("", None, MISSING),
              f"ms_sync_id={col_of(o6, 'ms_sync_id')!r}")
        check("T2 повторён целиком, а не по частям", calls["n"] >= 2,
              f"вызовов переноса={calls['n']}")
    finally:
        ms_writeback._move_incoming_to_ms = original_move

    exec_sql("UPDATE production_orders SET ms_doc_href='' WHERE id=? "
             "AND ms_doc_href LIKE 'pending:%'", o6)
    before6b = len(docs_created())
    r = push(c, o6)
    check("повтор после сбоя T2 прошёл", r.status_code == 200,
          f"status={r.status_code} {r.text[:160]}")
    check("ВТОРОГО ДОКУМЕНТА НЕ СОЗДАНО (upsert по тому же ключу)",
          len(docs_created()) == before6b,
          f"было={before6b} стало={len(docs_created())}")
    after_ok = qty_map().get(b6, (0.0, 0.0, 0.0))
    check("вклад учтён РОВНО ОДИН раз",
          abs(after_ok[1] - before_qty[1] - bases6.get(b6, 0)) < 1e-6,
          f"ms_qty было={before_qty[1]} стало={after_ok[1]} "
          f"отправлено={bases6.get(b6)}")

    # ── 7. Два одновременных клика ───────────────────────────────────────────
    print("\n== 7. Два одновременных клика по одному заказу ==")
    o7 = make_order(c, "Заказ на два клика")
    before7 = len(docs_created())
    results: list = []

    def _click():
        with httpx.Client(headers=dict(c.headers), cookies=c.cookies,
                          base_url=base, timeout=120.0) as cc:
            results.append(cc.post(f"/api/orders/{o7}/push-to-ms").status_code)

    threads = [threading.Thread(target=_click) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    check("один клик принят, второй отклонён",
          sorted(results) == [200, 409], f"статусы={sorted(results)}")
    check("создан ровно один документ", len(docs_created()) == before7 + 1,
          f"было={before7} стало={len(docs_created())}")

    # ── 8. Контрагент ────────────────────────────────────────────────────────
    print("\n== 8. Контрагент «Производство»: одна привязка, дубли → 409 ==")
    mock_ms.COUNTERPARTIES.clear()
    err = exec_sql("UPDATE connections SET ms_agent_href='', ms_agent_sync_id='' "
                   "WHERE kind='moysklad'")
    check("привязка контрагента хранится в базе и обнуляема", err == "", err)
    o8a, o8b = make_order(c, "Агент А"), make_order(c, "Агент Б")
    mock_api.post("/__test/faults", json={"cp_search_delay_ms": 400})
    agent_res: list = []

    def _push(order_id: int):
        with httpx.Client(headers=dict(c.headers), cookies=c.cookies,
                          base_url=base, timeout=120.0) as cc:
            rr = cc.post(f"/api/orders/{order_id}/push-to-ms")
            agent_res.append(rr.status_code)
            if rr.status_code != 200:
                print(f"       (агент {order_id}: {rr.status_code} {rr.text[:200]})")

    threads = [threading.Thread(target=_push, args=(oid,)) for oid in (o8a, o8b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    mock_api.post("/__test/faults", json={})
    prod = [cp for cp in mock_ms.COUNTERPARTIES if cp["name"] == ms_writeback.AGENT_NAME]
    check("обе отправки прошли", sorted(agent_res) == [200, 200],
          f"статусы={sorted(agent_res)}")
    check("КОНТРАГЕНТ СОЗДАН РОВНО ОДИН, а не по одному на клик",
          len(prod) == 1, f"контрагентов «{ms_writeback.AGENT_NAME}»={len(prod)}")
    check("привязка контрагента закреплена в базе",
          str(conn_col("ms_agent_href") or "").startswith("http"),
          f"ms_agent_href={conn_col('ms_agent_href')!r}")

    mock_ms.COUNTERPARTIES.clear()
    mock_ms.COUNTERPARTIES.extend([
        {"id": "cp-dup-1", "name": ms_writeback.AGENT_NAME},
        {"id": "cp-dup-2", "name": ms_writeback.AGENT_NAME},
    ])
    exec_sql("UPDATE connections SET ms_agent_href='', ms_agent_sync_id='' "
             "WHERE kind='moysklad'")
    o8c = make_order(c, "Агент-двойник")
    before8 = len(docs_created())
    r = push(c, o8c)
    check("ДВА контрагента с одним именем → 409, а не «взять первого»",
          r.status_code == 409, f"status={r.status_code} {r.text[:200]}")
    detail8 = str((r.json() or {}).get("detail") or "")
    check("в тексте перечислены оба контрагента",
          "cp-dup-1" in detail8 or detail8.count(ms_writeback.AGENT_NAME) >= 1,
          f"detail={detail8[:200]}")
    check("документ не создан", len(docs_created()) == before8,
          f"было={before8} стало={len(docs_created())}")
    check("лок снят — можно повторить после разбора",
          (col_of(o8c, "ms_doc_href") or "") == "",
          f"ms_doc_href={col_of(o8c, 'ms_doc_href')!r}")

    # ── 9. Back-match: честный unknown лечится ближайшим синком ──────────────
    #
    # Состояние после честного unknown: документ в МойСкладе есть, ссылки у
    # нас нет, локальный вклад заказа из qty не снят. Без back-match это
    # ВЕЧНЫЙ двойной счёт: одно и то же едет и в qty (наш заказ), и в ms_qty
    # (документ МС). Синк обязан связать их по syncId и снять локальный вклад.
    print("\n== 9. Back-match по syncId убирает вечный двойной счёт ==")
    mock_ms.COUNTERPARTIES.clear()
    exec_sql("UPDATE connections SET ms_agent_href='', ms_agent_sync_id='' "
             "WHERE kind='moysklad'")
    o9 = make_order(c, "Заказ с неизвестным исходом")
    bases9 = order_bases(c, o9)
    b9 = next(iter(bases9), "")
    r = c.post(f"/api/orders/{o9}/status", json={"status": "sent"})
    check("заказ переведён «в производство» (его вклад считается локально)",
          r.status_code == 200, f"status={r.status_code} {r.text[:160]}")
    qty_sent = qty_map().get(b9, (0.0, 0.0, 0.0))
    ms_writeback._move_incoming_to_ms = _always_fail
    try:
        r = push(c, o9)
        check("исход отправки честно неизвестен (502)", r.status_code == 502,
              f"status={r.status_code}")
    finally:
        ms_writeback._move_incoming_to_ms = original_move
    key9 = col_of(o9, "ms_sync_id")
    check("ключ отправленного документа известен",
          key9 not in ("", None, MISSING), f"ms_sync_id={key9!r}")
    check("ссылка не сохранена — состояние «unknown»",
          (col_of(o9, "ms_doc_href") or "") in ("", MISSING)
          or str(col_of(o9, "ms_doc_href")).startswith("pending:"),
          f"ms_doc_href={col_of(o9, 'ms_doc_href')!r}")

    # Двойник с ТЕМ ЖЕ ключом: связывать вслепую нельзя даже по своему ключу.
    twin9 = plant_doc("po-twin-sync", "Копия", sync_id=str(key9))
    c.post("/api/sync/run")
    st = wait_sync_done(c)
    check("синк с двойником по ключу завершился", st.get("state") == "done",
          f"state={st.get('state')}")
    check("при ДВУХ документах с одним ключом связывание не выполнено",
          not str(col_of(o9, "ms_doc_href") or "").startswith("http"),
          f"ms_doc_href={col_of(o9, 'ms_doc_href')!r}")
    unplant(twin9)

    # Чужой документ с меткой нашего заказа, но чужой ссылкой и без ключа:
    # D-28 не должен ослабнуть — он остаётся «внешним».
    alien_sku = next(s for s in mock_ms.SKUS
                     if s["base"] != b9 and "service" not in s["flags"])
    alien_base = alien_sku["base"]
    alien9 = plant_doc(
        "po-alien-9", f"Скопированное описание {order_marker(o9)}",
        positions=[{"quantity": 7.0, "shipped": 0.0,
                    "assortment": {"meta": mock_ms._asm_meta(alien_sku["ext"])}}])
    try:
        c.post("/api/sync/run")
        st = wait_sync_done(c)
        check("синк завершился", st.get("state") == "done", f"state={st.get('state')}")
        href9 = col_of(o9, "ms_doc_href") or ""
        check("СИНК СВЯЗАЛ документ с заказом по ключу",
              href9.startswith("http"), f"ms_doc_href={href9!r}")
        after9 = qty_map().get(b9, (0.0, 0.0, 0.0))
        check("ЛОКАЛЬНЫЙ ВКЛАД СНЯТ — двойного счёта больше нет",
              abs(after9[0] - (qty_sent[0] - bases9.get(b9, 0))) < 1e-6,
              f"qty было={qty_sent[0]} стало={after9[0]} "
              f"вклад заказа={bases9.get(b9)}")
        check("после связывания заказ считается нашим (D-28 выполняется)",
              after9[2] > 0, f"ms_qty_tracked={after9[2]}")
        ext9 = qty_map().get(alien_base, (0.0, 0.0, 0.0))
        check("D-28 НЕ ОСЛАБЛЕН: чужой документ со скопированной меткой — внешний",
              ext9[1] - ext9[2] >= 7 - 1e-6,
              f"ms_qty={ext9[1]} tracked={ext9[2]} внешних={ext9[1] - ext9[2]}")
    finally:
        unplant(alien9)

    # ── 10. Статусный переход ВО ВРЕМЯ отправки ─────────────────────────────
    #
    # Ревью Codex, раунд 1, блокер 1. Заказ был черновиком: его количества в
    # «едет к нам» локально НЕ считаются. Push прошёл T1 и ждёт ответа сети.
    # В это окно приходит перевод draft → sent — и добавляет полный локальный
    # вклад. Push об этом не знает (его ORM-объект помнит status='draft'),
    # поэтому T2 локальный вклад не снимает, а ms_qty прибавляет. Одно и то же
    # едет к нам ДВАЖДЫ, и никакой синк это не разведёт: обе величины «свои».
    print("\n== 10. Гонка: смена статуса во время сетевого окна отправки ==")
    o10 = make_order(c, "Заказ, которому меняют статус на лету")
    bases10 = order_bases(c, o10)
    b10 = next(iter(bases10), "")
    n10 = bases10.get(b10, 0)
    before10 = qty_map().get(b10, (0.0, 0.0, 0.0))
    docs_before10 = len(docs_created())
    mock_api.post("/__test/faults", json={"po_create_delay_ms": 2500})
    push10: list = []

    def _push10():
        with httpx.Client(headers=dict(c.headers), cookies=c.cookies,
                          base_url=base, timeout=120.0) as cc:
            push10.append(cc.post(f"/api/orders/{o10}/push-to-ms"))

    t10 = threading.Thread(target=_push10)
    t10.start()
    marker10 = wait_pending(o10)
    check("окно отправки открыто (T1 закоммичен, документа ещё нет)",
          marker10.startswith("pending:") and len(docs_created()) == docs_before10,
          f"ms_doc_href={marker10!r} документов={len(docs_created())}")
    r10 = c.post(f"/api/orders/{o10}/status", json={"status": "sent"})
    t10.join(timeout=120)
    mock_api.post("/__test/faults", json={})
    check("СМЕНА СТАТУСА ВО ВРЕМЯ ОТПРАВКИ ОТКЛОНЕНА (409)",
          r10.status_code == 409, f"status={r10.status_code} {r10.text[:200]}")
    detail10 = ""
    try:
        detail10 = str((r10.json() or {}).get("detail") or "")
    except ValueError:
        detail10 = r10.text
    check("отказ объясняет причину — идёт отправка в МойСклад",
          "отправ" in detail10.lower(), f"detail={detail10[:200]}")
    check("статус заказа не изменился", status_of(o10) == "draft",
          f"status={status_of(o10)!r}")
    check("сама отправка при этом прошла",
          bool(push10) and push10[0].status_code == 200,
          f"status={push10[0].status_code if push10 else None} "
          f"{push10[0].text[:200] if push10 else ''}")
    check("создан ровно один документ", len(docs_created()) == docs_before10 + 1,
          f"было={docs_before10} стало={len(docs_created())}")
    after10 = qty_map().get(b10, (0.0, 0.0, 0.0))
    check("ЛОКАЛЬНЫЙ ВКЛАД НЕ ПОЯВИЛСЯ (нет двойного счёта)",
          abs(after10[0] - before10[0]) < 1e-6,
          f"qty было={before10[0]} стало={after10[0]} вклад заказа={n10}")
    check("вклад «едет к нам» учтён РОВНО ОДИН РАЗ — по документу МС",
          abs(after10[1] - before10[1] - n10) < 1e-6,
          f"ms_qty было={before10[1]} стало={after10[1]} отправлено={n10}")
    # Отправка кончилась — обычный переход обязан работать как прежде.
    r = c.post(f"/api/orders/{o10}/status", json={"status": "sent"})
    check("после отправки статусный переход снова разрешён",
          r.status_code == 200, f"status={r.status_code} {r.text[:200]}")
    after10b = qty_map().get(b10, (0.0, 0.0, 0.0))
    check("…и отправленный заказ по-прежнему не двигает локальный qty",
          abs(after10b[0] - before10[0]) < 1e-6 and abs(after10b[1] - after10[1]) < 1e-6,
          f"qty={after10b[0]} ms_qty={after10b[1]}")

    # ── 10б. То же окно, обратный порядок: изменение ПОСЛЕ T2 ───────────────
    #
    # Здесь двойной счёт возникает по-настоящему и остаётся навсегда.
    # Статусный запрос успевает ПРОЧИТАТЬ заказ, пока идёт отправка (в памяти
    # у него ms_doc_href='pending:…'), а свой UPDATE выполняет уже ПОСЛЕ того,
    # как T2 закоммитил ссылку и перенёс вклад в ms_qty. Решение «двигать ли
    # локальный qty» принимается по УСТАРЕВШЕМУ значению — и полный вклад
    # заказа ложится поверх уже учтённого ms_qty. Снять его потом нечем:
    # обе величины «свои», back-match тут ни при чём.
    #
    # Одной пометки pending этот случай не ловит: к моменту UPDATE пометки
    # уже нет, и запрет по ней пропустил бы запрос. Лечится только тем, что
    # признак «отправлен» берётся из той же транзакции, что и изменение.
    #
    # Задержка честная: пауза стоит ровно между чтением состояния и
    # изменяющим SQL — в том самом промежутке TOCTOU, который и обсуждается.
    print("\n== 10б. Гонка: изменение статуса решается по устаревшему признаку ==")
    import app.api as _api  # noqa: PLC0415 — точка инструментирования, не импорт API
    o10c = make_order(c, "Заказ, чей статус меняют сразу после T2")
    bases10c = order_bases(c, o10c)
    b10c = next(iter(bases10c), "")
    n10c = bases10c.get(b10c, 0)
    before10c = qty_map().get(b10c, (0.0, 0.0, 0.0))
    docs_before10c = len(docs_created())
    mock_api.post("/__test/faults", json={"po_create_delay_ms": 1200})
    push10c: list = []
    t2_done = threading.Event()

    def _push10c():
        try:
            with httpx.Client(headers=dict(c.headers), cookies=c.cookies,
                              base_url=base, timeout=120.0) as cc:
                push10c.append(cc.post(f"/api/orders/{o10c}/push-to-ms"))
        finally:
            t2_done.set()

    _orig_update = _api.update
    _armed = {"on": False}

    def _gated_update(*a, **kw):
        """Первый изменяющий SQL после взвода ждёт, пока отправка закончится."""
        if _armed["on"]:
            _armed["on"] = False
            t2_done.wait(timeout=90)
        return _orig_update(*a, **kw)

    t10c = threading.Thread(target=_push10c)
    t10c.start()
    marker10c = wait_pending(o10c)
    check("окно отправки открыто", marker10c.startswith("pending:"),
          f"ms_doc_href={marker10c!r}")
    _api.update = _gated_update
    _armed["on"] = True
    try:
        r10c = c.post(f"/api/orders/{o10c}/status", json={"status": "sent"})
    finally:
        _api.update = _orig_update
        _armed["on"] = False
    t10c.join(timeout=120)
    mock_api.post("/__test/faults", json={})
    check("отправка прошла", bool(push10c) and push10c[0].status_code == 200,
          f"status={push10c[0].status_code if push10c else None} "
          f"{push10c[0].text[:200] if push10c else ''}")
    check("создан ровно один документ", len(docs_created()) == docs_before10c + 1,
          f"было={docs_before10c} стало={len(docs_created())}")
    check("статусный переход после завершения отправки разрешён (200)",
          r10c.status_code == 200, f"status={r10c.status_code} {r10c.text[:200]}")
    after10c = qty_map().get(b10c, (0.0, 0.0, 0.0))
    check("ДВОЙНОГО QTY НЕТ: локальный вклад не лёг поверх ms_qty",
          abs(after10c[0] - before10c[0]) < 1e-6,
          f"qty было={before10c[0]} стало={after10c[0]} вклад заказа={n10c}")
    check("вклад «едет к нам» учтён РОВНО ОДИН РАЗ",
          abs(after10c[1] - before10c[1] - n10c) < 1e-6,
          f"ms_qty было={before10c[1]} стало={after10c[1]} отправлено={n10c}")

    # ── 11. Удаление ВО ВРЕМЯ отправки ──────────────────────────────────────
    #
    # Ревью Codex, раунд 1, блокер 2. Удаление проходит между T1 и T2: строка
    # исчезает, документ в МойСкладе создаётся уже после этого, а T2 получает
    # rowcount=0. Остаётся финансовый документ, к которому у нас нет заказа —
    # и связать его обратно нечем: ключ идемпотентности удалён вместе со строкой.
    print("\n== 11. Гонка: удаление заказа во время сетевого окна отправки ==")
    o11 = make_order(c, "Заказ, который удаляют на лету")
    docs_before11 = len(docs_created())
    mock_api.post("/__test/faults", json={"po_create_delay_ms": 2500})
    push11: list = []

    def _push11():
        with httpx.Client(headers=dict(c.headers), cookies=c.cookies,
                          base_url=base, timeout=120.0) as cc:
            push11.append(cc.post(f"/api/orders/{o11}/push-to-ms"))

    t11 = threading.Thread(target=_push11)
    t11.start()
    marker11 = wait_pending(o11)
    check("окно отправки открыто", marker11.startswith("pending:"),
          f"ms_doc_href={marker11!r}")
    r11 = c.request("DELETE", f"/api/orders/{o11}")
    t11.join(timeout=120)
    mock_api.post("/__test/faults", json={})
    check("УДАЛЕНИЕ ВО ВРЕМЯ ОТПРАВКИ ОТКЛОНЕНО (409)",
          r11.status_code == 409, f"status={r11.status_code} {r11.text[:200]}")
    detail11 = ""
    try:
        detail11 = str((r11.json() or {}).get("detail") or "")
    except ValueError:
        detail11 = r11.text
    check("отказ объясняет причину — идёт отправка в МойСклад",
          "отправ" in detail11.lower(), f"detail={detail11[:200]}")
    check("ЗАКАЗ НЕ УДАЛЁН", order_exists(o11),
          f"строка заказа в базе={order_exists(o11)}")
    check("создан ровно один документ", len(docs_created()) == docs_before11 + 1,
          f"было={docs_before11} стало={len(docs_created())}")
    check("отправка завершилась успехом, а не «неизвестно»",
          bool(push11) and push11[0].status_code == 200,
          f"status={push11[0].status_code if push11 else None} "
          f"{push11[0].text[:200] if push11 else ''}")
    href11 = str(col_of(o11, "ms_doc_href") or "")
    check("ДОКУМЕНТ НЕ ОСИРОТЕЛ — ссылка сохранена у живого заказа",
          href11.startswith("http"), f"ms_doc_href={href11!r}")
    check("документ несёт ключ этого заказа",
          any(str(d.get("syncId") or "") == str(col_of(o11, "ms_sync_id") or "_")
              for d in docs_created()),
          f"ms_sync_id={col_of(o11, 'ms_sync_id')!r}")
    # Отправка кончилась — удаление снова разрешено (поведение не сломано).
    r = c.request("DELETE", f"/api/orders/{o11}")
    check("после отправки удаление снова разрешено", r.status_code == 200,
          f"status={r.status_code} {r.text[:200]}")
    check("…и заказ действительно удалён", not order_exists(o11),
          f"строка заказа в базе={order_exists(o11)}")
    o11b = make_order(c, "Обычный черновик под удаление")
    r = c.request("DELETE", f"/api/orders/{o11b}")
    check("удаление обычного черновика не сломано", r.status_code == 200,
          f"status={r.status_code} {r.text[:200]}")

    # ── 11б. Обратный порядок: удаление решается по состоянию, прочитанному
    #         ДО T1 ──────────────────────────────────────────────────────────
    #
    # Ревью Codex, раунд 2, пункт 3. Блок 11 открывает окно отправки ПЕРВЫМ и
    # только потом зовёт DELETE: он доказывает, что пометка pending учтена в
    # самом удалении, но не исходную форму TOCTOU — «DELETE прочитал допустимый
    # заказ → T1 поставил pending → DELETE выполнил изменение». Порядок здесь
    # обратный и закреплён детерминированно, ровно как блок 10б для статуса:
    # пауза стоит между чтением заказа и первым изменяющим SQL удаления — в том
    # промежутке, где предварительная проверка «отправки нет» уже устарела.
    #
    # Исход обязан быть один: отправка доводится до конца, удаление получает
    # честный 409, заказ жив, документ ровно один и связан с заказом ссылкой.
    print("\n== 11б. Гонка: удаление начато до T1, изменение — после ==")
    o11c = make_order(c, "Заказ, чьё удаление началось до отправки")
    docs_before11c = len(docs_created())
    mock_api.post("/__test/faults", json={"po_create_delay_ms": 1500})
    _orig_delete = _api.delete
    armed11c = {"on": False}
    at_border11c = threading.Event()
    go11c = threading.Event()

    def _gated_delete11c(*a, **kw):
        """Первый изменяющий SQL удаления ждёт на границе, пока пройдёт T1."""
        if armed11c["on"]:
            armed11c["on"] = False
            at_border11c.set()
            go11c.wait(timeout=90)
        return _orig_delete(*a, **kw)

    del11c: list = []

    def _del11c():
        with httpx.Client(headers=dict(c.headers), cookies=c.cookies,
                          base_url=base, timeout=120.0) as cc:
            del11c.append(cc.request("DELETE", f"/api/orders/{o11c}"))

    push11c: list = []

    def _push11c():
        with httpx.Client(headers=dict(c.headers), cookies=c.cookies,
                          base_url=base, timeout=120.0) as cc:
            push11c.append(cc.post(f"/api/orders/{o11c}/push-to-ms"))

    _api.delete = _gated_delete11c
    armed11c["on"] = True
    td11c = threading.Thread(target=_del11c)
    td11c.start()
    border11c = at_border11c.wait(timeout=30)
    try:
        check("удаление прочитало живой заказ и стоит перед изменением",
              border11c, f"дошли до границы={border11c}")
        tp11c = threading.Thread(target=_push11c)
        tp11c.start()
        marker11c = wait_pending(o11c)
        check("T1 встал между чтением и изменением",
              marker11c.startswith("pending:"), f"ms_doc_href={marker11c!r}")
    finally:
        go11c.set()
        td11c.join(timeout=120)
        _api.delete = _orig_delete
        armed11c["on"] = False
    tp11c.join(timeout=120)
    mock_api.post("/__test/faults", json={})
    check("отправка доведена до конца (200)",
          bool(push11c) and push11c[0].status_code == 200,
          f"status={push11c[0].status_code if push11c else None} "
          f"{push11c[0].text[:200] if push11c else ''}")
    check("УДАЛЕНИЕ, НАЧАТОЕ ДО T1, ОТКЛОНЕНО (409)",
          bool(del11c) and del11c[0].status_code == 409,
          f"status={del11c[0].status_code if del11c else None} "
          f"{del11c[0].text[:200] if del11c else ''}")
    detail11c = ""
    if del11c:
        try:
            detail11c = str((del11c[0].json() or {}).get("detail") or "")
        except ValueError:
            detail11c = del11c[0].text
    check("отказ объясняет причину — идёт отправка в МойСклад",
          "отправ" in detail11c.lower(), f"detail={detail11c[:200]}")
    check("ЗАКАЗ НЕ УДАЛЁН", order_exists(o11c),
          f"строка заказа в базе={order_exists(o11c)}")
    check("создан ровно один документ", len(docs_created()) == docs_before11c + 1,
          f"было={docs_before11c} стало={len(docs_created())}")
    href11c = str(col_of(o11c, "ms_doc_href") or "")
    check("ССЫЛКА НА ДОКУМЕНТ СОХРАНЕНА у живого заказа",
          href11c.startswith("http"), f"ms_doc_href={href11c!r}")

    # ── 12. Приёмка против удаления одного и того же заказа ─────────────────
    #
    # Ревью Codex, раунд 2, пункты 1 и 2. Запрет «принятый заказ удалить
    # нельзя» стоит проверкой ПЕРЕД удалением, а значит держится ровно до
    # первой гонки: sent → received успевает закоммититься между чтением и
    # DELETE, и удаление сносит уже принятый заказ вместе с фактами приёмки.
    # Зеркальный случай не лучше: если победило удаление, проигравший переход
    # отвечает «ok, unchanged» — успех, которого не было.
    #
    # Оба порядка ниже закреплены детерминированно; SQL обязан обеспечивать
    # оба, а нулевой результат изменения — различать три исхода: строки нет
    # (404), стала received (422), идёт отправка (409).
    print("\n== 12. Гонка: «принят на склад» против удаления ==")

    # 12а. Приёмка победила: удаление обязано получить 422, а не снести заказ.
    o12 = make_order(c, "Заказ, который принимают во время удаления")
    bases12 = order_bases(c, o12)
    b12 = next(iter(bases12), "")
    n12 = bases12.get(b12, 0)
    before12 = qty_map().get(b12, (0.0, 0.0, 0.0))
    r = c.post(f"/api/orders/{o12}/status", json={"status": "sent"})
    check("заказ переведён в «в производстве»", r.status_code == 200,
          f"status={r.status_code} {r.text[:200]}")
    armed12 = {"on": False}
    at_border12 = threading.Event()
    recv12_done = threading.Event()

    def _gated_delete12(*a, **kw):
        """Удаление ждёт на границе, пока приёмка не закоммитится."""
        if armed12["on"]:
            armed12["on"] = False
            at_border12.set()
            recv12_done.wait(timeout=90)
        return _orig_delete(*a, **kw)

    del12: list = []

    def _del12():
        with httpx.Client(headers=dict(c.headers), cookies=c.cookies,
                          base_url=base, timeout=120.0) as cc:
            del12.append(cc.request("DELETE", f"/api/orders/{o12}"))

    _api.delete = _gated_delete12
    armed12["on"] = True
    td12 = threading.Thread(target=_del12)
    td12.start()
    border12 = at_border12.wait(timeout=30)
    r12 = None
    try:
        check("удаление прочитало заказ ещё в статусе «в производстве»",
              border12, f"дошли до границы={border12}")
        r12 = c.post(f"/api/orders/{o12}/status",
                     json={"status": "received",
                           "received": [{"base_name": b12, "qty": n12}]})
    finally:
        recv12_done.set()
        td12.join(timeout=120)
        _api.delete = _orig_delete
        armed12["on"] = False
    check("приёмка прошла (200)", r12 is not None and r12.status_code == 200,
          f"status={r12.status_code if r12 is not None else None} "
          f"{r12.text[:200] if r12 is not None else ''}")
    check("УДАЛЕНИЕ, ПРОИГРАВШЕЕ ПРИЁМКЕ, ОТКЛОНЕНО (422)",
          bool(del12) and del12[0].status_code == 422,
          f"status={del12[0].status_code if del12 else None} "
          f"{del12[0].text[:200] if del12 else ''}")
    detail12 = ""
    if del12:
        try:
            detail12 = str((del12[0].json() or {}).get("detail") or "")
        except ValueError:
            detail12 = del12[0].text
    check("отказ объясняет причину — заказ принят на склад",
          "принят" in detail12.lower(), f"detail={detail12[:200]}")
    check("ПРИНЯТЫЙ ЗАКАЗ НЕ УДАЛЁН", order_exists(o12),
          f"строка заказа в базе={order_exists(o12)}")
    check("статус остался «принят на склад»", status_of(o12) == "received",
          f"status={status_of(o12)!r}")
    check("ФАКТ ПРИЁМКИ СОХРАНЁН", receipts_count(o12) > 0,
          f"строк приёмки={receipts_count(o12)}")
    after12 = qty_map().get(b12, (0.0, 0.0, 0.0))
    check("«едет к нам» вернулось к исходному: заказано и принято",
          abs(after12[0] - before12[0]) < 1e-6,
          f"qty было={before12[0]} стало={after12[0]} вклад заказа={n12}")
    r = c.post(f"/api/orders/{o12}/status", json={"status": "received"})
    check("повтор того же статуса по-прежнему идемпотентен (200 unchanged)",
          r.status_code == 200 and bool((r.json() or {}).get("unchanged")),
          f"status={r.status_code} {r.text[:200]}")

    # 12б. Удаление победило: проигравший переход обязан ответить 404 и не
    #      записать приёмку по заказу, которого больше нет.
    o13 = make_order(c, "Заказ, который удаляют во время приёмки")
    bases13 = order_bases(c, o13)
    b13 = next(iter(bases13), "")
    n13 = bases13.get(b13, 0)
    before13 = qty_map().get(b13, (0.0, 0.0, 0.0))
    r = c.post(f"/api/orders/{o13}/status", json={"status": "sent"})
    check("заказ переведён в «в производстве»", r.status_code == 200,
          f"status={r.status_code} {r.text[:200]}")
    sent13 = qty_map().get(b13, (0.0, 0.0, 0.0))
    check("вклад «едет к нам» учтён локально",
          abs(sent13[0] - before13[0] - n13) < 1e-6,
          f"qty было={before13[0]} стало={sent13[0]} вклад={n13}")
    _orig_update13 = _api.update
    armed13 = {"on": False}
    at_border13 = threading.Event()
    del13_done = threading.Event()

    def _gated_update13(*a, **kw):
        """Приёмка ждёт на границе, пока удаление не закоммитится."""
        if armed13["on"]:
            armed13["on"] = False
            at_border13.set()
            del13_done.wait(timeout=90)
        return _orig_update13(*a, **kw)

    recv13: list = []

    def _recv13():
        with httpx.Client(headers=dict(c.headers), cookies=c.cookies,
                          base_url=base, timeout=120.0) as cc:
            recv13.append(cc.post(
                f"/api/orders/{o13}/status",
                json={"status": "received",
                      "received": [{"base_name": b13, "qty": n13}]}))

    _api.update = _gated_update13
    armed13["on"] = True
    tr13 = threading.Thread(target=_recv13)
    tr13.start()
    border13 = at_border13.wait(timeout=30)
    r13d = None
    try:
        check("приёмка прочитала живой заказ и стоит перед изменением",
              border13, f"дошли до границы={border13}")
        r13d = c.request("DELETE", f"/api/orders/{o13}")
    finally:
        del13_done.set()
        tr13.join(timeout=120)
        _api.update = _orig_update13
        armed13["on"] = False
    check("удаление прошло (200)", r13d is not None and r13d.status_code == 200,
          f"status={r13d.status_code if r13d is not None else None} "
          f"{r13d.text[:200] if r13d is not None else ''}")
    check("заказ действительно удалён", not order_exists(o13),
          f"строка заказа в базе={order_exists(o13)}")
    check("ПРОИГРАВШАЯ ПРИЁМКА ОТВЕЧАЕТ 404, а не «ok, unchanged»",
          bool(recv13) and recv13[0].status_code == 404,
          f"status={recv13[0].status_code if recv13 else None} "
          f"{recv13[0].text[:200] if recv13 else ''}")
    check("ПРИЁМКА НЕ ЗАПИСАНА по удалённому заказу",
          receipts_count(o13) == 0, f"строк приёмки={receipts_count(o13)}")
    after13 = qty_map().get(b13, (0.0, 0.0, 0.0))
    check("вклад «едет к нам» снят удалением ровно один раз",
          abs(after13[0] - before13[0]) < 1e-6,
          f"qty было={before13[0]} стало={after13[0]} вклад={n13}")

    # 12в. Те же правила без всякой гонки — обычные пути не сломаны.
    o14 = make_order(c, "Обычный принятый заказ")
    c.post(f"/api/orders/{o14}/status", json={"status": "sent"})
    c.post(f"/api/orders/{o14}/status", json={"status": "received"})
    r = c.request("DELETE", f"/api/orders/{o14}")
    check("обычное удаление принятого заказа по-прежнему 422",
          r.status_code == 422, f"status={r.status_code} {r.text[:200]}")
    check("…и заказ на месте", order_exists(o14),
          f"строка заказа в базе={order_exists(o14)}")
    o15 = make_order(c, "Черновик под обычное удаление")
    r = c.request("DELETE", f"/api/orders/{o15}")
    check("обычное удаление черновика не сломано", r.status_code == 200,
          f"status={r.status_code} {r.text[:200]}")
    r = c.post(f"/api/orders/{o15}/status", json={"status": "sent"})
    check("статусный переход по удалённому заказу — 404", r.status_code == 404,
          f"status={r.status_code} {r.text[:200]}")

    check("каждый POST заказа поставщику нёс syncId",
          all(d.get("syncId") for d in docs_created()),
          f"без ключа={[d.get('id') for d in docs_created() if not d.get('syncId')]}")

    mock_api.post("/__test/faults", json={})
    c.close()
    mock_api.close()
    print(f"\nИТОГО: {len(PASS)} OK, {len(FAIL)} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
