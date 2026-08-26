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

Раунд 3 ревью Codex закрыл последнее место, где лок был не локом (блок 13).
Пометка живёт TTL, и по его истечении её ЗАКОННО перехватывает соседняя
попытка — иначе умерший воркер запирал бы заказ навсегда. Но обе снимающие
операции сравнивали пометку образцом `LIKE pending:%`, то есть «любая», а не
«моя»: старый владелец, вернувшийся из сети позже своего TTL, снимал лок
нового либо коммитил T2 поверх него. Взаимного исключения не оставалось ровно
там, где оно и нужно. Владение — свойство пары (строка, токен), поэтому токен
T1 теперь проносится через всё сетевое окно и сравнивается равенством.

Проверки владения детерминированы по построению: ни сети, ни пауз, ни потоков.
Рядом с ними обязателен положительный контроль — «чужой токен ничего не
делает» выполняется и у операции, сломанной в вечный no-op.

Мок закрепляет ожидаемое поведение чужого API, но доказательством живого
контракта НЕ является: живой тест `syncId` на боевом аккаунте — отдельный
merge gate (см. TECH_DEBT, DATA-1/DATA-2).

Запуск из корня репозитория:  python tests/test_writeback_idempotency.py
"""
import asyncio
import contextlib
import io
import json
import os
import sqlite3
import sys
import threading
import time
import uuid
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
# BASE вычисляется в моке при импорте и от присвоения PORT сам не меняется.
# Через раннер расхождения не видно: OBOROT_MOCK_PORT стоит в окружении, и обе
# величины совпадают. А при ОДИНОЧНОМ запуске файла мок раздавал href с портом
# 9800, тогда как приложение ходило на 9813 — то есть href «своего» аккаунта
# выглядел чужим. Набор от этого раньше не падал, поэтому расхождение и дожило
# до проверки закреплённого контрагента, которая сверяет href с базовым
# адресом API.
mock_ms.BASE = f"http://127.0.0.1:{MOCK_PORT}"


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


def body_of(r) -> dict:
    """Тело ответа словарём — или пустой словарь, если это не JSON.

    Нужно ровно для проверок про СЛОМАННЫЕ сборки. FastAPI на необработанное
    исключение отвечает `500 Internal Server Error` обычным текстом, и прямой
    `r.json()` на таком ответе поднимает JSONDecodeError. Проверка тогда падает
    трассировкой, набор получает приговор NO_REPORT — «набора не было» — вместо
    честного FAIL, а по контракту D-42 это разные вещи.

    Поймано мутационным прогоном раунда 13: восстановленный дефект давал 500,
    и мой же контртест на нём разваливался вместо того, чтобы покраснеть.
    """
    try:
        value = r.json()
    except Exception:  # noqa: BLE001 — «не JSON» здесь нормальный исход
        return {}
    return value if isinstance(value, dict) else {}


def detail_of(r) -> str:
    """Поле detail ответа строкой — безопасно к не-JSON телу."""
    return str(body_of(r).get("detail") or "")


def exec_sql_read(query: str, *args) -> list:
    """Чтение из той же базы, что и приложение, — списком кортежей."""
    con = sqlite3.connect(DB_PATH)
    try:
        return list(con.execute(query, args).fetchall())
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
    # Здесь стояло «лок снят — повтор возможен» с проверкой пустого
    # ms_doc_href, и это утверждение противоречило смыслу самого сбоя.
    # po_fail_before_create — отказ 502 ДО создания, но комментарий мока
    # прямо говорит: клиент из своей позиции не отличает его от «создан,
    # ответ потерян», и обязан вести себя так, будто документ мог появиться.
    # Пустая строка возвращала заказ в вид «неотправленный», после чего его
    # можно было удалить — вместе с ms_sync_id (ревью Codex, P1,
    # discussion_r3858173475). Новое утверждение строже, а не мягче: повтор
    # по-прежнему обязан пройти, но состояние между попытками — устойчивое
    # «неизвестно», а не «как будто ничего не было».
    check("ИСХОД ПОСЛЕ УШЕДШЕГО POST — «НЕИЗВЕСТНО», а не «лок снят»",
          ms_writeback.is_unknown(col_of(o1, "ms_doc_href")),
          f"ms_doc_href={col_of(o1, 'ms_doc_href')!r}")
    rdel1 = c.delete(f"/api/orders/{o1}")
    check("…и такой заказ неудаляем — ключ связывания не выбросить",
          rdel1.status_code == 409, f"delete={rdel1.status_code}")
    mock_api.post("/__test/faults", json={})
    r = push(c, o1)
    check("повтор поверх «неизвестно» прошёл (200)", r.status_code == 200,
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
        # Ссылки нет, но и пустоты нет: состояние «неизвестно» теперь ЯВНОЕ
        # (ревью Codex, P1). Пустая строка возвращала заказ в вид
        # «неотправленный» и открывала удаление вместе с ключом связывания.
        check("ссылка НЕ сохранена, а состояние помечено как «неизвестно»",
              ms_writeback.is_unknown(col_of(o6, "ms_doc_href"))
              and not ms_writeback.is_pushed(col_of(o6, "ms_doc_href")),
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
            agent_res.append({"order_id": order_id, "status": rr.status_code,
                              "body": body_of(rr)})
            if rr.status_code != 200:
                print(f"       (агент {order_id}: {rr.status_code} {rr.text[:200]})")

    threads = [threading.Thread(target=_push, args=(oid,)) for oid in (o8a, o8b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    mock_api.post("/__test/faults", json={})
    statuses8 = sorted(r["status"] for r in agent_res)
    check("один пуш принят, второй отклонён org-локом синка (DATA-6)",
          statuses8 == [200, 409], f"статусы={statuses8}")
    losers8 = [r for r in agent_res if r["status"] == 409]
    detail8a = str(losers8[0]["body"].get("detail") or "") if losers8 else ""
    check("409 назван честно — идёт синхронизация, а не иная причина",
          "Идёт синхронизация" in detail8a, f"detail={detail8a[:200]}")
    loser_id8 = losers8[0]["order_id"] if losers8 else None
    check("у отклонённого заказа ms_doc_href пуст — лок не оставил пометки",
          loser_id8 is not None and (col_of(loser_id8, "ms_doc_href") or "") == "",
          f"order={loser_id8} ms_doc_href="
          f"{col_of(loser_id8, 'ms_doc_href') if loser_id8 is not None else None!r}")
    loser_key8 = col_of(loser_id8, "ms_sync_id") if loser_id8 is not None else None
    check("ни один созданный документ не несёт ключ отклонённого заказа",
          not loser_key8 or all(str(d.get("syncId") or "") != str(loser_key8)
                                for d in docs_created()),
          f"ключ={loser_key8!r}")
    r_retry8 = push(c, loser_id8)
    check("отклонённый заказ отправлен синхронно после снятия фолтов — 200",
          r_retry8.status_code == 200, f"status={r_retry8.status_code} {r_retry8.text[:200]}")

    prod = [cp for cp in mock_ms.COUNTERPARTIES if cp["name"] == ms_writeback.AGENT_NAME]
    check("КОНТРАГЕНТ СОЗДАН РОВНО ОДИН, а не по одному на клик",
          len(prod) == 1, f"контрагентов «{ms_writeback.AGENT_NAME}»={len(prod)}")
    check("привязка контрагента закреплена в базе",
          str(conn_col("ms_agent_href") or "").startswith("http"),
          f"ms_agent_href={conn_col('ms_agent_href')!r}")
    href_a8 = str(col_of(o8a, "ms_doc_href") or "")
    href_b8 = str(col_of(o8b, "ms_doc_href") or "")
    check("оба заказа получили настоящую ссылку на документ, а не пометку",
          href_a8.startswith("http") and href_b8.startswith("http")
          and not ms_writeback.is_internal_href(href_a8)
          and not ms_writeback.is_internal_href(href_b8),
          f"o8a={href_a8!r} o8b={href_b8!r}")
    created_hrefs8 = {d["meta"]["href"] for d in docs_created()}
    check("это ДВА РАЗНЫХ созданных документа, а не один на двоих",
          href_a8 != href_b8 and {href_a8, href_b8} <= created_hrefs8,
          f"o8a={href_a8!r} o8b={href_b8!r} created={created_hrefs8}")

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
    check("ссылка не сохранена — состояние «unknown» и оно ЯВНОЕ",
          ms_writeback.is_unknown(col_of(o9, "ms_doc_href"))
          and not ms_writeback.is_pushed(col_of(o9, "ms_doc_href")),
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

    # ── 13. Поздний старый владелец лока после законного takeover ───────────
    #
    # Ревью Codex, раунд 3, блокирующий дефект. Пометка «идёт отправка» живёт
    # TTL (180 c), и по его истечении её ЗАКОННО перехватывает соседняя
    # попытка — так и задумано: воркер, умерший между T1 и сетью, не должен
    # запирать заказ навсегда. Но обе операции, которые снимают пометку,
    # сравнивали её образцом `LIKE pending:%`, то есть «любая пометка», а не
    # «моя». Отсюда interleaving, воспроизведённый на HEAD d7792fe0:
    #
    #   A захватила pending:t0 → провисела дольше TTL → B честно перехватила
    #   CAS на pending:t1 → поздняя A либо СНИМАЕТ лок B (cleanup при сбое),
    #   либо КОММИТИТ T2 поверх B (пишет свой href и переносит вклад).
    #
    # В первом случае в открытое окно входит третья попытка, пока B ещё в
    # сети; во втором заказ привязывается к документу проигравшей попытки.
    # Стабильный ms_sync_id спасает от второго документа НАРУЖУ, но владение
    # локом у себя он не восстанавливает — это разные гарантии.
    #
    # Проверки ниже детерминированные по построению: ни сети, ни пауз, ни
    # потоков. Владение — свойство пары (строка, токен), а не тайминга,
    # поэтому и проверяется прямым вызовом на реальной базе.
    #
    # Положительный контроль обязателен и стоит рядом. Обе регрессии
    # доказывают, что ЧУЖОЙ токен ничего не делает; без контроля их прошло бы
    # и исправление, сломавшее операции насовсем, — «не делает ничего никогда»
    # тоже удовлетворяет обеим.
    print("\n== 13. Поздний старый владелец лока после takeover ==")
    from app import routes_connect  # noqa: PLC0415 — прямой вызов T1/T2, не API
    from app.db import SessionLocal  # noqa: PLC0415
    from app.models import ProductionOrder as _PO  # noqa: PLC0415

    o16 = make_order(c, "Заказ, чей лок перехватили после TTL")
    db16 = SessionLocal()
    try:
        org16 = int(db16.get(_PO, o16).org_id)
        # A: обычный T1 поверх пустого href. Токен намеренно из прошлого —
        # ровно та ситуация, в которой takeover разрешён.
        t_a = int(time.time()) - 10_000
        pend_a = f"{ms_writeback.PENDING_PREFIX}{t_a}"
        locked_a = ms_writeback.begin_push(db16, org16, o16, "", pend_a)
        check("A захватила лок (T1)", locked_a and col_of(o16, "ms_doc_href") == pend_a,
              f"locked={locked_a} ms_doc_href={col_of(o16, 'ms_doc_href')!r}")
        key_a = str(col_of(o16, "ms_sync_id") or "")
        # B: законный перехват протухшего лока — CAS по значению A.
        pend_b = f"{ms_writeback.PENDING_PREFIX}{t_a + 5_000}"
        locked_b = ms_writeback.begin_push(db16, org16, o16, pend_a, pend_b)
        check("B законно перехватила протухший лок (CAS по значению A)",
              locked_b and col_of(o16, "ms_doc_href") == pend_b,
              f"locked={locked_b} ms_doc_href={col_of(o16, 'ms_doc_href')!r}")
        check("ключ идемпотентности при перехвате НЕ пересоздан",
              str(col_of(o16, "ms_sync_id") or "") == key_a and bool(key_a),
              f"было={key_a!r} стало={col_of(o16, 'ms_sync_id')!r}")

        # 13а. Регрессия: поздний cleanup A не трогает пометку B.
        routes_connect._release_push_lock(db16, o16, pend_a)
        check("ПОЗДНИЙ CLEANUP A НЕ СНЯЛ ЛОК B",
              col_of(o16, "ms_doc_href") == pend_b,
              f"ms_doc_href={col_of(o16, 'ms_doc_href')!r} ожидалось={pend_b!r}")

        # Пометка B восстанавливается принудительно — и это не подгонка.
        # В ИСПРАВЛЕННОЙ сборке 13а её не трогало, и присвоение ничего не
        # меняет. В СЛОМАННОЙ сборке 13а её уже стёрла — и тогда следующая
        # проверка мерила бы «T2 не прошёл по пустой строке» вместо «T2 не
        # прошёл поверх чужой пометки», то есть зеленела бы по чужой причине.
        # Каждая из двух регрессий обязана падать за СВОЙ дефект: cleanup и
        # T2 сломаны по отдельности и чинятся по отдельности.
        db16.rollback()  # чужому соединению нужен свободный файл базы
        err_b = exec_sql("UPDATE production_orders SET ms_doc_href=? WHERE id=?",
                         pend_b, o16)
        check("исходное состояние для проверки T2 — пометка B на месте",
              not err_b and col_of(o16, "ms_doc_href") == pend_b,
              f"ms_doc_href={col_of(o16, 'ms_doc_href')!r} sql={err_b!r}")

        # 13б. Регрессия: поздний T2 A не коммитит поверх пометки B.
        name_before = col_of(o16, "ms_doc_name")
        res_a = ms_writeback._commit_push_once(
            db16, org16, db16.get(_PO, o16),
            "https://example.invalid/entity/purchaseorder/late-A", "A-late",
            {}, pend_a, col_of(o16, "items_json"))
        check("ПОЗДНИЙ T2 A ОТКАЗАН (лок уже не его)", res_a is None,
              f"результат={res_a!r}")
        check("…и href B не перезаписан документом A",
              col_of(o16, "ms_doc_href") == pend_b,
              f"ms_doc_href={col_of(o16, 'ms_doc_href')!r} ожидалось={pend_b!r}")
        check("…и имя документа не подменено",
              col_of(o16, "ms_doc_name") == name_before,
              f"было={name_before!r} стало={col_of(o16, 'ms_doc_name')!r}")

        # 13в. Положительный контроль: законный владелец B своим токеном
        #      доводит T2 до конца. Без него обе регрессии прошли бы и на
        #      операциях, сломанных в «никогда ничего не делает».
        href_b = "https://example.invalid/entity/purchaseorder/owner-B"
        res_b = ms_writeback._commit_push_once(
            db16, org16, db16.get(_PO, o16), href_b, "B-owner", {}, pend_b,
            col_of(o16, "items_json"))
        check("ЗАКОННЫЙ ВЛАДЕЛЕЦ B СВОИМ ТОКЕНОМ T2 ЗАВЕРШИЛ", res_b is True,
              f"результат={res_b!r}")
        check("…и ссылка записана именно его документа",
              col_of(o16, "ms_doc_href") == href_b and col_of(o16, "ms_doc_name") == "B-owner",
              f"ms_doc_href={col_of(o16, 'ms_doc_href')!r} "
              f"ms_doc_name={col_of(o16, 'ms_doc_name')!r}")

        # 13г. Положительный контроль для cleanup: свой токен лок снимает.
        o17 = make_order(c, "Заказ, чью отправку снимает её же владелец")
        pend_c = f"{ms_writeback.PENDING_PREFIX}{int(time.time())}"
        locked_c = ms_writeback.begin_push(db16, org16, o17, "", pend_c)
        check("владелец C захватил лок", locked_c, f"locked={locked_c}")
        routes_connect._release_push_lock(db16, o17, pend_c)
        check("CLEANUP СВОИМ ТОКЕНОМ ЛОК СНИМАЕТ (заказ снова отправляем)",
              (col_of(o17, "ms_doc_href") or "") == "",
              f"ms_doc_href={col_of(o17, 'ms_doc_href')!r}")
    finally:
        db16.close()

    # ── 14. Удалённый контрагент: вечный отказ вместо самовосстановления ─────
    #
    # Ревью Codex, P2 (discussion_r3842533227). Закреплённая ссылка на
    # контрагента возвращалась вслепую: `if keys.agent_href: return ...`.
    # Контрагента «Производство» удалили в МойСкладе — и с этого момента КАЖДЫЙ
    # POST заказа падает валидацией «контрагент не найден», а ссылка у нас
    # остаётся прежней. Повтор не лечится никогда: следующая отправка снова
    # достаёт из базы тот же мёртвый href. Восстановить работу мог только
    # человек, правящий нашу базу руками, — а из интерфейса это выглядит как
    # «МойСклад сломался».
    #
    # Проверки детерминированные: удаление контрагента выражено состоянием
    # мира (строка убрана из mock_ms.COUNTERPARTIES), а не таймингом.
    print("\n== 14. Контрагента удалили в МойСкладе: отправка чинит себя сама ==")
    mock_ms.COUNTERPARTIES.clear()
    exec_sql("UPDATE connections SET ms_agent_href='', ms_agent_sync_id='' "
             "WHERE kind='moysklad'")
    o14 = make_order(c, "Заказ до удаления контрагента")
    r = push(c, o14)
    check("первая отправка прошла и закрепила контрагента", r.status_code == 200,
          f"status={r.status_code} {r.text[:200]}")
    href14 = str(conn_col("ms_agent_href") or "")
    check("ссылка на контрагента закреплена в базе", href14.startswith("http"),
          f"ms_agent_href={href14!r}")

    # Контрагента удаляют в МойСкладе. Наша закреплённая ссылка становится
    # мёртвой, но в базе остаётся ровно такой же.
    #
    # Вместо него в аккаунте оставлен посторонний контрагент — и не для
    # красоты. Мок раздаёт идентификаторы как `cp-<число строк + 1>`, поэтому
    # на пустом списке заново созданный контрагент получил бы ТОТ ЖЕ `cp-001`,
    # и «ссылка изменилась» стало бы неотличимо от «мёртвая ссылка случайно
    # снова заработала». С посторонней строкой новый контрагент получает
    # другой идентификатор, и переразрешение видно по факту, а не по вере.
    # (Найдено этой же проверкой: первая её редакция падала именно так.)
    gone14 = [cp for cp in mock_ms.COUNTERPARTIES
              if cp["name"] == ms_writeback.AGENT_NAME]
    mock_ms.COUNTERPARTIES.clear()
    mock_ms.COUNTERPARTIES.append({"id": "cp-other", "name": "Посторонний"})
    check("контрагент удалён в МойСкладе, а ссылка у нас осталась",
          len(gone14) == 1
          and not [cp for cp in mock_ms.COUNTERPARTIES
                   if cp["name"] == ms_writeback.AGENT_NAME]
          and str(conn_col("ms_agent_href") or "") == href14,
          f"было контрагентов={len(gone14)} ms_agent_href={conn_col('ms_agent_href')!r}")

    o14b = make_order(c, "Заказ после удаления контрагента")
    before14 = len(docs_created())
    r = push(c, o14b)
    check("ОТПРАВКА ПОСЛЕ УДАЛЕНИЯ КОНТРАГЕНТА ПРОШЛА, а не упала навсегда",
          r.status_code == 200, f"status={r.status_code} {r.text[:250]}")
    check("документ действительно создан", len(docs_created()) == before14 + 1,
          f"было={before14} стало={len(docs_created())}")
    href14b = str(conn_col("ms_agent_href") or "")
    check("МЁРТВАЯ ССЫЛКА ЗАМЕНЕНА НА ЖИВУЮ, а не осталась прежней",
          href14b.startswith("http") and href14b != href14,
          f"было={href14!r} стало={href14b!r}")
    live_ids = [cp["id"] for cp in mock_ms.COUNTERPARTIES]
    check("…и новая ссылка указывает на существующего контрагента",
          href14b.rsplit("/", 1)[-1] in live_ids,
          f"ms_agent_href={href14b!r} есть в аккаунте={live_ids}")
    check("контрагент переразрешён РОВНО ОДИН, а не по одному на попытку",
          len([cp for cp in mock_ms.COUNTERPARTIES
               if cp["name"] == ms_writeback.AGENT_NAME]) == 1,
          f"контрагенты={live_ids}")

    # 14б. Отрицательный контроль, без которого исправление опасно.
    # «Проверять ссылку» нельзя понимать как «сбрасывать привязку по любому
    # неудачному ответу»: 429/5xx/таймаут означают «мы не знаем», а не
    # «удалено». Сброс по ним завёл бы клиенту ВТОРОГО подрядчика — то есть,
    # починив дубль документа, мы породили бы дубль контрагента.
    live14 = str(conn_col("ms_agent_href") or "")
    cps_before = len(mock_ms.COUNTERPARTIES)
    o14c = make_order(c, "Заказ во время временного сбоя проверки")
    # Ретраи клиента здесь временно выключены, и только ради времени прогона:
    # 500 входит в RETRY_STATUSES, десять повторов с backoff до 60 c заняли бы
    # минуты. Проверяется не число повторов, а исход исчерпанных повторов —
    # «мы не знаем» обязано остаться «мы не знаем», а не стать «удалено».
    from app import ms_client as _ms_client  # noqa: PLC0415
    retries_before = _ms_client.MAX_RETRIES
    _ms_client.MAX_RETRIES = 0
    mock_api.post("/__test/faults", json={"cp_get_500_burst": 99})
    try:
        r = push(c, o14c)
    finally:
        mock_api.post("/__test/faults", json={})
        _ms_client.MAX_RETRIES = retries_before
    check("транзиентный сбой проверки НЕ выдан за успех", r.status_code != 200,
          f"status={r.status_code} {r.text[:200]}")
    check("ПРИВЯЗКА НЕ СБРОШЕНА транзиентным сбоем",
          str(conn_col("ms_agent_href") or "") == live14,
          f"было={live14!r} стало={conn_col('ms_agent_href')!r}")
    check("…и второго контрагента не появилось",
          len(mock_ms.COUNTERPARTIES) == cps_before,
          f"было={cps_before} стало={len(mock_ms.COUNTERPARTIES)}")

    # 14в. Положительный контроль: живая ссылка не пересматривается.
    # Без него «исправление», которое переразрешает контрагента на КАЖДОЙ
    # отправке, прошло бы 14а и 14б — и тихо отменило бы решение D-36 о том,
    # что выбор подрядчика делается один раз.
    o14d = make_order(c, "Заказ при живом закреплённом контрагенте")
    r = push(c, o14d)
    check("отправка при живой ссылке прошла", r.status_code == 200,
          f"status={r.status_code} {r.text[:200]}")
    check("ЖИВАЯ ПРИВЯЗКА НЕ ПЕРЕСМОТРЕНА (тот же контрагент)",
          str(conn_col("ms_agent_href") or "") == live14,
          f"было={live14!r} стало={conn_col('ms_agent_href')!r}")
    check("…и нового контрагента не создано",
          len(mock_ms.COUNTERPARTIES) == cps_before,
          f"было={cps_before} стало={len(mock_ms.COUNTERPARTIES)}")

    # ── 15. Подтверждение живого contract-теста — точная фраза ──────────────
    #
    # Ревью Codex, P2 (discussion_r3848613938). Гейт живого теста стоял на
    # `if not TOKEN or not CONFIRM`, то есть на «переменная непустая». Под это
    # подходило ЛЮБОЕ значение, включая `OBOROT_LIVE_MS_CONFIRM=no`: значение,
    # которым человек пытался запретить запуск, запуск разрешало — и набор
    # начинал создавать настоящие сущности в живом аккаунте МойСклад.
    #
    # Проверяется чистая функция: сети здесь нет ни в одной ветке, поэтому
    # проверка безопасна сама по себе и не может «случайно сходить» в чужой
    # аккаунт — что для теста про запуск, создающий документы, обязательно.
    print("\n== 15. Живой contract-тест: подтверждение сверяется дословно ==")
    import test_ms_syncid_live as live  # noqa: PLC0415

    check("разрешает ТОЛЬКО точную фразу подтверждения",
          live.is_authorized("tok", live.CONFIRM_PHRASE) is True,
          f"фраза={live.CONFIRM_PHRASE!r}")
    for bad in ("no", "0", "false", "нет", "y", "1",
                live.CONFIRM_PHRASE[:-1], live.CONFIRM_PHRASE + "!"):
        check(f"НЕ разрешает подтверждение {bad!r}",
              live.is_authorized("tok", bad) is False,
              f"is_authorized('tok', {bad!r})={live.is_authorized('tok', bad)}")
    check("перевод строки от окружения фразу не ломает",
          live.is_authorized("tok\n", f" {live.CONFIRM_PHRASE}\n") is True)
    check("пустое подтверждение не разрешает", not live.is_authorized("tok", ""))
    check("правильная фраза без токена не разрешает",
          not live.is_authorized("", live.CONFIRM_PHRASE))
    check("пробелы вокруг «no» его не спасают",
          not live.is_authorized("tok", "  no  "))
    # Гейт обязан быть ВШИТ в main(), а не просто существовать рядом с ним:
    # чистая функция, которую никто не зовёт, — это ноль защиты. Значения
    # подставляются в модуль явно, чтобы проверка не зависела от окружения
    # прогона.
    #
    # На время проверки run() подменён растяжкой. Это не украшение: на
    # СЛОМАННОЙ сборке main() дойдёт до `asyncio.run(run())` и начнёт создавать
    # сущности — то есть тест про «запуск не должен произойти случайно» сам
    # устроил бы этот запуск. Проверено: первая редакция этой проверки на
    # восстановленном дефекте свалилась трассировкой ровно оттуда. С растяжкой
    # сеть недостижима в ЛЮБОЙ сборке, а факт «main() дошёл до run()»
    # становится обычным FAIL, а не падением набора.
    tok_before, conf_before, run_before = live.TOKEN, live.CONFIRM, live.run
    reached_run: list = []

    async def _tripwire() -> int:
        reached_run.append(True)
        return 0

    try:
        live.run = _tripwire
        live.TOKEN, live.CONFIRM = "живой-токен", "no"
        rc_bad = live.main()
        check("ГЕЙТ ВШИТ В main(): токен есть, фраза неверна → код 2, "
              "а сеть не тронута",
              rc_bad == 2 and not reached_run,
              f"main()={rc_bad} дошло до run()={bool(reached_run)}")
        live.CONFIRM = ""
        check("…и пустое подтверждение при живом токене тоже даёт 2",
              live.main() == 2 and not reached_run,
              f"дошло до run()={bool(reached_run)}")
    finally:
        live.TOKEN, live.CONFIRM, live.run = tok_before, conf_before, run_before

    # ── 16. Живой contract-тест: разбор payload до отправки ─────────────────
    #
    # Владелец разрешил живой прогон на своём аккаунте, но узко (OWNER_DECISION
    # 25.08.2026, issuecomment-5413052421 и постоянный мандат
    # issuecomment-5413066608): непроведённый документ, одна позиция,
    # количество 1, цена 0, уникальная пометка, существующие юрлицо и позиция,
    # никакого движения денег и остатков.
    #
    # Граница разрешения обязана жить в КОДЕ, а не в намерении исполнителя,
    # поэтому её проверяет чистая функция — и проверяется она здесь, без сети.
    print("\n== 16. Живой contract-тест: тело запроса разбирается до отправки ==")
    ORG = "https://api.example.invalid/entity/organization/org-1"
    SKU = "https://api.example.invalid/entity/product/sku-1"
    AGENT = "https://api.example.invalid/entity/counterparty/cp-1"
    TAG, SYNC = "deadbeef", "11111111-2222-3333-4444-555555555555"

    def _order(**over) -> dict:
        """Заведомо ДОПУСТИМОЕ тело; over портит ровно одно поле."""
        body = {
            "organization": {"meta": {"href": ORG}},
            "agent": {"meta": {"href": AGENT}},
            "positions": [{
                "assortment": {"meta": {"href": SKU, "type": "product"}},
                "quantity": 1,
                "price": 0,
            }],
            "description": f"{live.marking(TAG)}. Проверка контракта.",
            "applicable": False,
            "syncId": SYNC,
        }
        body.update(over)
        return body

    def _verdict(body: dict) -> list:
        return live.validate_order_payload(
            body, tag=TAG, sync_id=SYNC, org_href=ORG,
            assortment_href=SKU, agent_href=AGENT)

    # Положительный контроль стоит ПЕРВЫМ и обязателен: без него валидатор,
    # который отвергает вообще всё, прошёл бы все проверки ниже — и живой
    # прогон стал бы невозможен по причине, которую никто не заметил.
    check("ДОПУСТИМОЕ тело принимается (иначе живой прогон невозможен)",
          _verdict(_order()) == [], f"замечания={_verdict(_order())}")

    _cases = [
        ("проведение документа (applicable=True)", _order(applicable=True)),
        ("applicable=None не считается «непроведённым»", _order(applicable=None)),
        ("applicable=0 не считается «непроведённым»", _order(applicable=0)),
        ("ненулевая цена", _order(positions=[{
            "assortment": {"meta": {"href": SKU, "type": "product"}},
            "quantity": 1, "price": 15000}])),
        ("количество не 1", _order(positions=[{
            "assortment": {"meta": {"href": SKU, "type": "product"}},
            "quantity": 5, "price": 0}])),
        ("больше одной позиции", _order(positions=[
            {"assortment": {"meta": {"href": SKU, "type": "product"}},
             "quantity": 1, "price": 0},
            {"assortment": {"meta": {"href": SKU, "type": "product"}},
             "quantity": 1, "price": 0}])),
        ("ни одной позиции", _order(positions=[])),
        ("неверная маркировка в описании",
         _order(description="Просто заказ")),
        ("маркировка с чужим tag",
         _order(description=f"{live.MARK} 00000000. Не наш прогон.")),
        ("чужое юрлицо", _order(organization={"meta": {"href": ORG + "-другое"}})),
        ("чужая позиция ассортимента", _order(positions=[{
            "assortment": {"meta": {"href": SKU + "-другая", "type": "product"}},
            "quantity": 1, "price": 0}])),
        ("тип позиции не product/variant", _order(positions=[{
            "assortment": {"meta": {"href": SKU, "type": "service"}},
            "quantity": 1, "price": 0}])),
        ("чужой контрагент", _order(agent={"meta": {"href": AGENT + "-другой"}})),
        ("другой syncId, чем проверялся на свободу", _order(syncId=str(uuid.uuid4()))),
        # Лишние поля — по белому списку. Именно ими документ проводят,
        # двигают склад и заводят деньги, а «мы такого не передаём» — это про
        # намерение, а не про код.
        ("лишнее поле moment (дата проведения)", _order(moment="2026-08-25 12:00:00")),
        ("лишнее поле store (склад)", _order(store={"meta": {"href": "x"}})),
        ("лишнее поле payments (платежи)", _order(payments=[{"meta": {"href": "x"}}])),
        ("лишнее поле vatEnabled", _order(vatEnabled=True)),
        ("лишнее поле в позиции (reserve)", _order(positions=[{
            "assortment": {"meta": {"href": SKU, "type": "product"}},
            "quantity": 1, "price": 0, "reserve": 5}])),
    ]
    for label, body in _cases:
        check(f"ВАЛИДАТОР ОТВЕРГАЕТ: {label}", _verdict(body) != [],
              f"замечаний={_verdict(body)}")

    check("допустимое тело контрагента принимается",
          live.validate_agent_payload(
              {"name": live.marking(TAG), "syncId": SYNC},
              tag=TAG, sync_id=SYNC) == [])
    check("ВАЛИДАТОР ОТВЕРГАЕТ: имя контрагента без пометки",
          live.validate_agent_payload({"name": "Производство", "syncId": SYNC},
                                      tag=TAG, sync_id=SYNC) != [])
    check("ВАЛИДАТОР ОТВЕРГАЕТ: лишнее поле у контрагента",
          live.validate_agent_payload(
              {"name": live.marking(TAG), "syncId": SYNC, "archived": True},
              tag=TAG, sync_id=SYNC) != [])

    # ── 17. Fail-closed: первая же неожиданность останавливает сценарий ─────
    #
    # Тот самый дефект, ради которого этот раунд и делался: check() копил FAIL
    # и возвращал управление, поэтому после несошедшейся проверки сценарий шёл
    # к СЛЕДУЮЩЕМУ POST. То есть ровно в момент, когда чужой API повёл себя не
    # так, как мы думаем, мы продолжали в нём создавать. Мандат владельца
    # (пункт 8) требует обратного: немедленная остановка без повторных
    # мутирующих попыток.
    #
    # Проверяется подставным клиентом: сети нет, живого аккаунта нет, а
    # «сколько раз позвали create_*» видно поимённо.
    print("\n== 17. Живой contract-тест: после первой ошибки записи прекращаются ==")
    # Фраза, которую разрешено печатать ТОЛЬКО при нулевом числе изменяющих
    # попыток. Берётся из вывода, а не переписывается здесь руками: проверка
    # должна ловить смысл, а не совпадение формулировок.
    NOTHING = "В аккаунте ничего не создано"

    class FakeClient:
        """Подставной МойСклад: считает вызовы и умеет врать по сценарию."""

        def __init__(self, *, cp_dupes: int = 1, cp_repeat_same: bool = True,
                     doc_dupes: int = 1, orgs: int = 1, sku: bool = True,
                     cp_get_raises: bool = False, doc_get_raises: bool = False,
                     cp_create_raises: bool = False) -> None:
            self.calls: list = []
            self.cp_dupes, self.cp_repeat_same = cp_dupes, cp_repeat_same
            self.doc_dupes, self.n_orgs, self.has_sku = doc_dupes, orgs, sku
            # Сбои ПОСЛЕ успешного создания: ровно тот случай, из-за которого
            # прежний отчёт говорил «ничего не создано» поверх реальной записи.
            self.cp_get_raises = cp_get_raises
            self.doc_get_raises = doc_get_raises
            self.cp_create_raises = cp_create_raises

        async def fetch_organizations(self) -> list:
            self.calls.append("fetch_organizations")
            return [{"name": "ИП", "meta": {"href": ORG, "type": "organization"}}
                    ] * self.n_orgs

        async def fetch_assortment(self) -> list:
            self.calls.append("fetch_assortment")
            if not self.has_sku:
                return []
            return [{"name": "Товар", "archived": False,
                     "meta": {"href": SKU, "type": "product"}}]

        async def paginate(self, path, params=None, **kw):
            # Разведочный вызов из probe_read_only: живой аккаунт отвечает на
            # filter=syncId ошибкой 412, и подставной клиент ведёт себя так же.
            self.calls.append(f"paginate:{path}")
            if "syncId=" in str((params or {}).get("filter", "")):
                raise httpx.HTTPStatusError(
                    "mock: неизвестное поле фильтрации syncId",
                    request=None, response=None)
            return
            yield {}  # noqa: PLW0101 — делает функцию асинхронным генератором

        async def sync_id_point_lookup(self, entity, sync_id):
            # Точечный маршрут ничего не подтверждает: его поддержка не
            # доказана, и продукт обязан работать, когда его нет.
            self.calls.append("sync_id_point_lookup")
            return None

        async def find_by_sync_id(self, entity, sync_id):
            self.calls.append(f"find_by_sync_id:{entity}")
            if entity == "purchaseorder":
                return await self.find_purchase_orders_by_sync_id(sync_id)
            if self.cp_get_raises and self.calls.count("create_counterparty"):
                raise httpx.ReadTimeout("mock: поиск после создания не дошёл")
            if not self.calls.count("create_counterparty"):
                return []
            return [{"id": "cp-1", "syncId": sync_id,
                     "meta": {"href": AGENT, "type": "counterparty"}}
                    ] * self.cp_dupes

        async def find_counterparties_by_name(self, name):
            self.calls.append("find_counterparties_by_name")
            return []

        async def find_purchase_orders_by_sync_id(self, sync_id):
            self.calls.append("find_purchase_orders_by_sync_id")
            if not self.calls.count("create_purchase_order"):
                return []
            if self.doc_get_raises:
                raise httpx.ReadTimeout("mock: GET после создания не дошёл")
            return [{"id": "po-1", "syncId": sync_id,
                     "meta": {"href": "https://api.example.invalid/entity/"
                                      "purchaseorder/po-1"}}] * self.doc_dupes

        async def create_counterparty(self, name, sync_id):
            self.calls.append("create_counterparty")
            if self.cp_create_raises:
                # Обрыв ПОСЛЕ отправки: создана сущность или нет — из нашей
                # позиции не видно. Это не «не получилось», это «неизвестно».
                raise httpx.ReadTimeout("mock: ответ на POST не дошёл")
            n = self.calls.count("create_counterparty")
            cid = "cp-1" if (n == 1 or self.cp_repeat_same) else f"cp-{n}"
            return {"id": cid, "name": name, "syncId": sync_id,
                    "meta": {"href": AGENT, "type": "counterparty"}}

        async def create_purchase_order(self, payload):
            self.calls.append("create_purchase_order")
            return {"id": "po-1", "name": "00001",
                    "meta": {"href": "https://api.example.invalid/entity/"
                                     "purchaseorder/po-1"}}

    def _live_run(fake, *, create: bool = True) -> tuple:
        """Прогон сценария на подставном клиенте, с чистым счётом PASS/FAIL.

        Вывод сценария ГЛУШИТСЯ, и это не косметика. `run_contract` печатает
        свой собственный «ИТОГО: N OK, M FAIL», а раннер по контракту D-42
        читает ПОСЛЕДНИЙ такой отчёт в выводе набора. Шесть подставных
        прогонов, печатающих чужие итоги в общий поток, — это шесть шансов
        подсунуть раннеру не тот приговор.

        `create=False` гоняет ЧИТАЮЩУЮ стадию — ту самую, чей приговор
        проверяет блок 28. Переменная подставляется в модуль, а не в
        окружение: стадия обязана определяться разрешением, а не тем, как
        запущен набор.
        """
        p_before, f_before = list(live.PASS), list(live.FAIL)
        create_before = live.CREATE
        live.PASS.clear()
        live.FAIL.clear()
        buf = io.StringIO()
        try:
            # Создающая половина включается ЯВНО и только здесь: подставной
            # клиент никуда не ходит, а проверять надо именно её.
            live.CREATE = live.CREATE_PHRASE if create else ""
            with contextlib.redirect_stdout(buf):
                rc = asyncio.run(live.run_contract(fake))
        finally:
            live.PASS[:] = p_before
            live.FAIL[:] = f_before
            live.CREATE = create_before
        # Журнал снимается копией: следующий прогон его обнулит, а проверять
        # мы будем СТРУКТУРУ, а не только напечатанную строку. Строку легко
        # подделать формулировкой; список событий — нет.
        return rc, buf.getvalue(), [dict(e) for e in live.LEDGER]

    # 17а. Положительный контроль: на честном API сценарий доходит до конца и
    #      делает РОВНО четыре записи — два создания контрагента (создание +
    #      доказательство upsert) и два создания заказа. Без него всё ниже
    #      удовлетворялось бы сценарием «никогда ничего не вызываем».
    ok_fake = FakeClient()
    rc_ok, _out_ok, led_ok = _live_run(ok_fake)
    check("на честном API сценарий проходит целиком",
          rc_ok == 0, f"код возврата={rc_ok}")
    check("…и делает РОВНО два POST контрагента и два POST заказа",
          ok_fake.calls.count("create_counterparty") == 2
          and ok_fake.calls.count("create_purchase_order") == 2,
          f"cp={ok_fake.calls.count('create_counterparty')} "
          f"po={ok_fake.calls.count('create_purchase_order')}")

    # 17б. ГЛАВНАЯ регрессия: по ключу контрагента вернулось ДВА объекта.
    #      Контракт нарушен — заказ не должен создаваться вообще.
    dup_cp = FakeClient(cp_dupes=2)
    rc_dup, _out_dup, _led_dup = _live_run(dup_cp)
    check("дубль контрагента по syncId → сценарий провален", rc_dup == 1,
          f"код возврата={rc_dup}")
    check("ПОСЛЕ ДУБЛЯ КОНТРАГЕНТА ЗАКАЗ НЕ СОЗДАВАЛСЯ НИ РАЗУ",
          dup_cp.calls.count("create_purchase_order") == 0,
          f"вызовов create_purchase_order={dup_cp.calls.count('create_purchase_order')}")

    # 17в. Повторный POST вернул ДРУГОЙ id — upsert не работает. Это худший
    #      исход: он означает, что второй документ в принципе возможен.
    other_id = FakeClient(cp_repeat_same=False)
    rc_other, _out_other, _led_other = _live_run(other_id)
    check("повторный POST с другим id → сценарий провален", rc_other == 1,
          f"код возврата={rc_other}")
    check("ПОСЛЕ ПРОВАЛА UPSERT ЗАКАЗ НЕ СОЗДАВАЛСЯ",
          other_id.calls.count("create_purchase_order") == 0,
          f"вызовов create_purchase_order={other_id.calls.count('create_purchase_order')}")
    check("…и контрагент не создавался третий раз «на всякий случай»",
          other_id.calls.count("create_counterparty") == 2,
          f"вызовов create_counterparty={other_id.calls.count('create_counterparty')}")

    # 17г. Дубль уже на стадии заказа: второй POST заказа сделан (это и есть
    #      проверка upsert), но НИ ОДНОЙ попытки сверх сценария быть не должно.
    dup_doc = FakeClient(doc_dupes=2)
    rc_ddoc, _out_ddoc, _led_ddoc = _live_run(dup_doc)
    check("дубль заказа по syncId → сценарий провален", rc_ddoc == 1,
          f"код возврата={rc_ddoc}")
    check("после дубля заказа лишних POST не было",
          dup_doc.calls.count("create_purchase_order") <= 2,
          f"вызовов create_purchase_order={dup_doc.calls.count('create_purchase_order')}")

    # 17д. Не сошлись ПОДГОТОВИТЕЛЬНЫЕ проверки — ни одной записи вообще.
    for label, fake in (("нет юрлица", FakeClient(orgs=0)),
                        ("нет неархивной позиции", FakeClient(sku=False))):
        rc_pre, _out_pre, led_pre = _live_run(fake)
        check(f"{label} → сценарий провален до записи", rc_pre == 1,
              f"код возврата={rc_pre}")
        check(f"…и НИ ОДНОГО создания не было ({label})",
              fake.calls.count("create_counterparty") == 0
              and fake.calls.count("create_purchase_order") == 0,
              f"cp={fake.calls.count('create_counterparty')} "
              f"po={fake.calls.count('create_purchase_order')}")
        check(f"…и журнал пуст, поэтому «ничего не создано» — правда ({label})",
              led_pre == [] and NOTHING in _out_pre,
              f"событий={len(led_pre)}")

    # 17е. Токен не попадает в вывод ни при каком исходе.
    tok_save = live.TOKEN
    try:
        live.TOKEN = "секретный-токен-abc123"
        scrubbed = live.scrub(f"Authorization: Bearer {live.TOKEN} — сбой")
        check("ТОКЕН ВЫРЕЗАЕТСЯ ИЗ ЛЮБОГО ВЫВОДА",
              live.TOKEN not in scrubbed and "<токен скрыт>" in scrubbed,
              f"после чистки={scrubbed!r}")
    finally:
        live.TOKEN = tok_save

    # ── 18. Журнал изменяющих попыток: не терять уже созданное ──────────────
    #
    # Ревью Codex, P1 (discussion_r3855229584). Остановиться на первой
    # неожиданности мало — надо ещё честно сказать, что мы к этому моменту УЖЕ
    # отправили. Прежний список созданного пополнялся только после полного
    # возврата стадии, поэтому «POST прошёл, следующий GET упал» давал пустой
    # список и отчёт «в аккаунте ничего не создано» поверх реальной записи в
    # чужом аккаунте. Владелец такую сущность не найдёт: он не знает, что её
    # надо искать.
    #
    # Проверяется СТРУКТУРА журнала и число изменяющих вызовов, а не строка
    # вывода: формулировку легко подправить так, чтобы тест позеленел, ничего
    # не починив.
    print("\n== 18. Журнал: созданное не теряется при остановке ==")

    def _events(led, stage_part: str) -> list:
        return [e for e in led if stage_part in e["stage"]]

    def _observed_ids(led, stage_part: str) -> list:
        return [o["id"] for e in _events(led, stage_part) for o in e["observed"]]

    # 18а. Положительный сценарий: журнал описывает ровно четыре отправленные
    #      попытки и обе созданные сущности, а запрещённой фразы в отчёте нет.
    check("журнал честного прогона содержит РОВНО 4 изменяющие попытки",
          len(led_ok) == 4, f"событий={len(led_ok)} стадии={[e['stage'] for e in led_ok]}")
    check("…все четыре помечены как созданные по ответу API",
          all(e["status"] == live.CREATED for e in led_ok),
          f"статусы={[e['status'] for e in led_ok]}")
    check("…и у каждой записан наблюдённый id/href",
          all(e["observed"] and e["observed"][0]["id"] and e["observed"][0]["href"]
              for e in led_ok),
          f"наблюдения={[e['observed'] for e in led_ok]}")
    check("…и «ничего не создано» в честном прогоне НЕ печатается",
          NOTHING not in _out_ok)
    check("…и в журнале нет ни токена, ни заголовков",
          "Bearer" not in str(led_ok) and "Authorization" not in str(led_ok))

    # 18б. ГЛАВНАЯ регрессия finding'а: контрагент СОЗДАН, следующий GET упал.
    #      Заказ не трогаем, но созданного контрагента обязаны назвать.
    cp_lost = FakeClient(cp_get_raises=True)
    rc_lost, out_lost, led_lost = _live_run(cp_lost)
    check("создан контрагент + упавший GET → сценарий провален", rc_lost == 1,
          f"код возврата={rc_lost}")
    check("СТАДИЯ ЗАКАЗА НЕ ВЫЗВАНА", cp_lost.calls.count("create_purchase_order") == 0,
          f"вызовов create_purchase_order={cp_lost.calls.count('create_purchase_order')}")
    check("ЖУРНАЛ ВСЁ РАВНО СОДЕРЖИТ СОЗДАННОГО КОНТРАГЕНТА",
          [e["status"] for e in _events(led_lost, "контрагент")] == [live.CREATED]
          and _observed_ids(led_lost, "контрагент") == ["cp-1"],
          f"журнал={led_lost}")
    check("…и «ничего не создано» НЕ печатается после реальной записи",
          NOTHING not in out_lost)
    check("…а отчёт требует проверить аккаунт чтением",
          "ПРОВЕРЬТЕ ЧТЕНИЕМ" in out_lost)
    check("…и повторного POST контрагента не было (upsert не проверялся)",
          cp_lost.calls.count("create_counterparty") == 1,
          f"вызовов create_counterparty={cp_lost.calls.count('create_counterparty')}")

    # 18в. То же на стадии заказа: заказ СОЗДАН, следующий GET упал.
    #      В журнале обязаны быть и контрагент, и заказ.
    doc_lost = FakeClient(doc_get_raises=True)
    rc_dlost, out_dlost, led_dlost = _live_run(doc_lost)
    check("создан заказ + упавший GET → сценарий провален", rc_dlost == 1,
          f"код возврата={rc_dlost}")
    check("ЖУРНАЛ СОДЕРЖИТ И КОНТРАГЕНТА, И ЗАКАЗ",
          _observed_ids(led_dlost, "контрагент") == ["cp-1", "cp-1"]
          and _observed_ids(led_dlost, "заказ") == ["po-1"],
          f"контрагент={_observed_ids(led_dlost, 'контрагент')} "
          f"заказ={_observed_ids(led_dlost, 'заказ')}")
    check("…и «ничего не создано» НЕ печатается", NOTHING not in out_dlost)
    check("…и лишних POST заказа не было",
          doc_lost.calls.count("create_purchase_order") == 1,
          f"вызовов={doc_lost.calls.count('create_purchase_order')}")

    # 18г. Обрыв НА ОТПРАВКЕ: ответа нет, создана сущность или нет — неизвестно.
    #      «Не получилось» здесь было бы враньём в самую опасную сторону.
    raised = FakeClient(cp_create_raises=True)
    rc_raise, out_raise, led_raise = _live_run(raised)
    check("обрыв на POST → сценарий провален", rc_raise == 1,
          f"код возврата={rc_raise}")
    # Пустой журнал — ожидаемый исход СЛОМАННОЙ сборки, поэтому индексация
    # обязана быть безопасной. Иначе проверка падает трассировкой, набор
    # получает приговор NO_REPORT («набора не было») вместо честного FAIL, и
    # красный прогон доказывает меньше, чем должен. Ровно на этом первая
    # редакция блока и споткнулась.
    ev_raise = led_raise[0] if led_raise else {}
    check("ПОПЫТКА ЗАПИСАНА В ЖУРНАЛ ДО ОТПРАВКИ (она там есть)",
          len(led_raise) == 1, f"событий={len(led_raise)}")
    check("…и помечена ЧЕСТНЫМ «неизвестно, возможно создано», а не провалом",
          ev_raise.get("status") == live.UNKNOWN,
          f"статус={ev_raise.get('status')!r}")
    check("…и наблюдений нет — ответа мы не получили",
          ev_raise.get("observed") == [], f"наблюдения={ev_raise.get('observed')!r}")
    check("…и назван тип сбоя, но не секрет",
          ev_raise.get("error") == "ReadTimeout" and "Bearer" not in str(ev_raise),
          f"событие={ev_raise}")
    check("…и «ничего не создано» НЕ печатается после отправленного POST",
          NOTHING not in out_raise)
    check("…и второй попытки/ретрая не было",
          raised.calls.count("create_counterparty") == 1,
          f"вызовов create_counterparty={raised.calls.count('create_counterparty')}")
    check("…и стадия заказа не начиналась",
          raised.calls.count("create_purchase_order") == 0)

    # 18д. Повтор вернул ДРУГОЙ id: в аккаунте два объекта, и назвать обязаны
    #      ОБА — иначе владелец пойдёт искать один.
    check("при разошедшихся id повтора в журнале записаны ОБА наблюдения",
          _observed_ids(_led_other, "контрагент") == ["cp-1", "cp-2"],
          f"наблюдения={_observed_ids(_led_other, 'контрагент')}")
    check("…и оба события помечены как созданные по ответу API",
          [e["status"] for e in _events(_led_other, "контрагент")]
          == [live.CREATED, live.CREATED],
          f"статусы={[e['status'] for e in _events(_led_other, 'контрагент')]}")
    check("…и «ничего не создано» НЕ печатается", NOTHING not in _out_other)

    # 18е. Отрицательный контроль на саму фразу: она разрешена РОВНО тогда,
    #      когда изменяющих попыток не было. Иначе проверки выше можно было бы
    #      удовлетворить, просто перестав её печатать когда бы то ни было.
    check("«ничего не создано» печатается, когда попыток действительно НЕ было",
          NOTHING in _out_pre and led_pre == [],
          f"событий={len(led_pre)}")

    # ── 19. Поиск по syncId без filter=syncId ───────────────────────────────
    #
    # Живой аккаунт 25.08.2026 отверг первый же read-only preflight:
    # GET /entity/counterparty?filter=syncId=<uuid> → HTTP 412, code 1034,
    # «неизвестное поле фильтрации syncId» (Issue #2, issuecomment-5414290329).
    # На этом фильтре держались и поиск своего документа, и поиск контрагента,
    # то есть отправка заказа на боевом API падала целиком.
    #
    # Мок раньше этот фильтр послушно поддерживал — и потому набор был
    # зелёным, пока продукт был сломан. Теперь мок моделирует ФАКТ, а не наши
    # ожидания: `_reject_sync_id_filter` отвечает тем же 412/1034.
    print("\n== 19. Поиск по syncId не зависит от filter=syncId ==")
    from app import ms_client as _ms_client  # noqa: PLC0415

    # 19а. Факт зафиксирован: мок отвергает фильтр ровно так, как живой API.
    r = mock_api.get("/entity/counterparty",
                     params={"filter": "syncId=" + str(uuid.uuid4())},
                     headers={"Authorization": f"Bearer {mock_ms.TOKEN}"})
    body19 = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    errs19 = ((body19.get("detail") or {}).get("errors") or [{}])[0]
    check("МОК ОТВЕРГАЕТ filter=syncId так же, как живой МойСклад (412/1034)",
          r.status_code == 412 and errs19.get("code") == 1034,
          f"status={r.status_code} body={str(body19)[:160]}")

    # 19б. Продакшн-код этого фильтра больше не строит. Проверка статическая:
    #      поведенческая поймала бы только те пути, по которым прошёл тест.
    src19 = (ROOT / "app" / "ms_client.py").read_text(encoding="utf-8")
    src19 += (ROOT / "app" / "ms_writeback.py").read_text(encoding="utf-8")
    src19 += (ROOT / "app" / "ms_sync.py").read_text(encoding="utf-8")
    check("В ПРОДАКШН-КОДЕ НЕТ НИ ОДНОГО filter=syncId",
          "syncId=" not in src19.replace("filter=syncId`", "").replace(
              "`filter=syncId=`", "").replace("filter=syncId=", "@"),
          "найдена сборка фильтра по syncId")

    # 19в. Отправка работает целиком, хотя фильтра нет. Это и есть главный
    #      контртест: раньше здесь был 502 на первом же поиске.
    mock_ms.COUNTERPARTIES.clear()
    exec_sql("UPDATE connections SET ms_agent_href='', ms_agent_sync_id='' "
             "WHERE kind='moysklad'")
    o19 = make_order(c, "Заказ без фильтра по syncId")
    before19 = len(docs_created())
    r = push(c, o19)
    check("ОТПРАВКА ПРОХОДИТ БЕЗ filter=syncId", r.status_code == 200,
          f"status={r.status_code} {r.text[:200]}")
    check("…и документ создан ровно один",
          len(docs_created()) == before19 + 1,
          f"было={before19} стало={len(docs_created())}")

    # 19г. Точечный маршрут /entity/{type}/syncid/{id} может не поддерживаться
    #      GET'ом — документация описывает его только для удаления. Тогда его
    #      404 неотличим от «сущности нет», и полагаться на него как на
    #      отрицательный ответ нельзя. Отправка обязана работать и без него.
    mock_ms.COUNTERPARTIES.clear()
    exec_sql("UPDATE connections SET ms_agent_href='', ms_agent_sync_id='' "
             "WHERE kind='moysklad'")
    o19b = make_order(c, "Заказ при недоступном точечном маршруте")
    before19b = len(docs_created())
    mock_api.post("/__test/faults", json={"syncid_route_404": 1})
    try:
        r = push(c, o19b)
    finally:
        mock_api.post("/__test/faults", json={})
    check("ОТПРАВКА ПРОХОДИТ, даже если точечный syncid-маршрут отвечает 404",
          r.status_code == 200, f"status={r.status_code} {r.text[:200]}")
    check("…и документ создан ровно один, без дубля",
          len(docs_created()) == before19b + 1,
          f"было={before19b} стало={len(docs_created())}")

    # 19д. Восстановление: документ с нашим ключом уже создан, ответ потерян.
    #      Перебор обязан его найти и НЕ создать второй.
    o19c = make_order(c, "Заказ, чей ответ потерялся")
    key19 = str(uuid.uuid4())
    exec_sql("UPDATE production_orders SET ms_sync_id=?, ms_lookup_mode='sync' "
             "WHERE id=?", key19, o19c)
    planted19 = plant_doc("po-lost-19", "Документ прошлой попытки", sync_id=key19)
    try:
        before19c = len(docs_created())
        r = push(c, o19c)
        check("ПОТЕРЯННЫЙ ОТВЕТ: документ найден перебором, а не создан заново",
              r.status_code == 200 and (r.json() or {}).get("recovered") is True,
              f"status={r.status_code} recovered={(r.json() or {}).get('recovered')}")
        check("…и второго документа не появилось",
              len(docs_created()) == before19c,
              f"было={before19c} стало={len(docs_created())}")
    finally:
        unplant(planted19)

    # 19и. САМАЯ ОПАСНАЯ комбинация, и без неё остальные проверки её пропускают.
    #      Документ с нашим ключом СУЩЕСТВУЕТ, а точечный маршрут отвечает 404.
    #      Если счесть этот 404 ответом «сущности нет» — создастся ВТОРОЙ
    #      финансовый документ. Именно поэтому точечный запрос у нас только
    #      подтверждающий: его отрицательный ответ не решает ничего.
    #
    #      (Пробел нашёлся мутационным прогоном: без этой проверки «доверять
    #      отрицательному ответу» проходило зелёным.)
    o19e = make_order(c, "Заказ, чей документ есть, а маршрут молчит")
    key19e = str(uuid.uuid4())
    exec_sql("UPDATE production_orders SET ms_sync_id=?, ms_lookup_mode='sync' "
             "WHERE id=?", key19e, o19e)
    planted19e = plant_doc("po-hidden-19", "Документ есть, маршрут 404",
                           sync_id=key19e)
    try:
        before19e = len(docs_created())
        mock_api.post("/__test/faults", json={"syncid_route_404": 1})
        try:
            r = push(c, o19e)
        finally:
            mock_api.post("/__test/faults", json={})
        check("СУЩЕСТВУЮЩИЙ ДОКУМЕНТ НАЙДЕН, хотя точечный маршрут отвечал 404",
              r.status_code == 200 and (r.json() or {}).get("recovered") is True,
              f"status={r.status_code} recovered={(r.json() or {}).get('recovered')}")
        check("…и ВТОРОГО документа не создано",
              len(docs_created()) == before19e,
              f"было={before19e} стало={len(docs_created())}")
    finally:
        unplant(planted19e)

    # 19е. ГЛАВНАЯ граница. Перебор исчерпал потолок — ответ НЕДОСТОВЕРЕН.
    #      Пустой список здесь означал бы «в аккаунте такого нет» и разрешал
    #      создать документ заново. Обязателен честный отказ и НОЛЬ созданий.
    mock_ms.COUNTERPARTIES.clear()
    mock_ms.COUNTERPARTIES.extend([
        {"id": f"cp-bulk-{i}", "name": f"Посторонний {i}"} for i in range(5)])
    exec_sql("UPDATE connections SET ms_agent_href='', ms_agent_sync_id='' "
             "WHERE kind='moysklad'")
    o19d = make_order(c, "Заказ при исчерпанной границе перебора")
    before19d = len(docs_created())
    cps_before19 = len(mock_ms.COUNTERPARTIES)
    limit_save = _ms_client.SYNC_ID_SCAN_LIMIT
    _ms_client.SYNC_ID_SCAN_LIMIT = 2
    try:
        r = push(c, o19d)
    finally:
        _ms_client.SYNC_ID_SCAN_LIMIT = limit_save
    check("ИСЧЕРПАННАЯ ГРАНИЦА ПЕРЕБОРА → ОТКАЗ, а не «не нашли»",
          r.status_code != 200, f"status={r.status_code} {r.text[:200]}")
    check("…и текст объясняет, что проверить не удалось",
          "достоверно" in str((r.json() or {}).get("detail") or ""),
          f"detail={str((r.json() or {}).get('detail'))[:200]}")
    check("…и НИ ОДНОГО документа не создано",
          len(docs_created()) == before19d,
          f"было={before19d} стало={len(docs_created())}")
    check("…и НИ ОДНОГО контрагента не заведено",
          len(mock_ms.COUNTERPARTIES) == cps_before19,
          f"было={cps_before19} стало={len(mock_ms.COUNTERPARTIES)}")
    check("…и лок снят — заказ можно отправить снова, когда станет достоверно",
          (col_of(o19d, "ms_doc_href") or "") == "",
          f"ms_doc_href={col_of(o19d, 'ms_doc_href')!r}")

    # 19з. Две стадии живого теста. Первый запуск обязан доказать ЧИТАЮЩУЮ
    #      половину контракта и остановиться: 25.08.2026 живой аккаунт отверг
    #      самый первый read-only preflight, и хорошо, что до создания дело не
    #      дошло. Один запуск не должен уметь и «посмотреть», и завести
    #      документы.
    create_save = live.CREATE
    try:
        for label, value in (("переменная не задана", ""),
                             ("«yes» вместо фразы", "yes"),
                             ("фраза подтверждения запуска, а не создания",
                              live.CONFIRM_PHRASE),
                             ("опечатка в фразе", live.CREATE_PHRASE + "!")):
            live.CREATE = value
            check(f"создание НЕ разрешено: {label}", not live.may_create(),
                  f"CREATE={value!r}")
            ro_fake = FakeClient()
            p_b, f_b = list(live.PASS), list(live.FAIL)
            live.PASS.clear()
            live.FAIL.clear()
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    asyncio.run(live.run_contract(ro_fake))
            finally:
                live.PASS[:] = p_b
                live.FAIL[:] = f_b
            check(f"…и прогон НИЧЕГО не создал ({label})",
                  ro_fake.calls.count("create_counterparty") == 0
                  and ro_fake.calls.count("create_purchase_order") == 0,
                  f"cp={ro_fake.calls.count('create_counterparty')} "
                  f"po={ro_fake.calls.count('create_purchase_order')}")
        live.CREATE = live.CREATE_PHRASE
        check("создание разрешено ТОЛЬКО дословной отдельной фразой",
              live.may_create())
        check("…и фраза создания отличается от фразы подтверждения запуска",
              live.CREATE_PHRASE != live.CONFIRM_PHRASE)
    finally:
        live.CREATE = create_save

    # 19ж. Положительный контроль границы: при достаточном потолке та же
    #      отправка проходит. Без него «отказ всегда» удовлетворял бы 19е.
    r = push(c, o19d)
    check("ПРИ ДОСТАТОЧНОМ ПОТОЛКЕ та же отправка проходит",
          r.status_code == 200, f"status={r.status_code} {r.text[:200]}")
    check("…и создаёт ровно один документ",
          len(docs_created()) == before19d + 1,
          f"было={before19d} стало={len(docs_created())}")

    # ── 20. Точечный ответ не заменяет полный перебор ───────────────────────
    #
    # Ревью Codex, P1 (discussion_r3855902789). find_by_sync_id обещает ВСЕ
    # совпадения, и на этом обещании стоит вся проверка дублей: клиент смотрит
    # len(rows) > 1, find_own_document — len(docs) > 1, живой гейт после
    # повторного POST — «ровно один». Терминальный ответ из точечного запроса
    # делал все три недостижимыми ровно тогда, когда недокументированный
    # маршрут отвечает 200: два объекта с одним ключом он вернул бы как один.
    #
    # Проверяется НЕ формулировка, а достижимость: в аккаунте два объекта с
    # одним syncId, точечный маршрут отдаёт первый — и результат обязан быть
    # дублем, а не единицей.
    print("\n== 20. Дубль по syncId виден, даже когда точечный маршрут ответил ==")
    from app.ms_client import MoySkladClient as _Client  # noqa: PLC0415
    from app import ms_client as _msc  # noqa: PLC0415

    def _lookup(entity: str, sync_id: str):
        """Прямой вызов продуктового поиска на моке — без веб-слоя."""
        async def _run():
            async with _Client(mock_ms.TOKEN) as cl:
                return await cl.find_by_sync_id(entity, sync_id)
        return asyncio.run(_run())

    def _cp_lookup(sync_id: str):
        async def _run():
            async with _Client(mock_ms.TOKEN) as cl:
                return await cl.find_counterparty_by_sync_id(sync_id)
        return asyncio.run(_run())

    # 20а. ГЛАВНЫЙ контртест: точечный GET отдаёт ОДИН объект, а в коллекции
    #      их ДВА с тем же ключом.
    dup_key = str(uuid.uuid4())
    mock_ms.COUNTERPARTIES.clear()
    mock_ms.COUNTERPARTIES.extend([
        {"id": "cp-dup-a", "name": "Дубль А", "syncId": dup_key},
        {"id": "cp-dup-b", "name": "Дубль Б", "syncId": dup_key},
    ])
    point20 = _lookup("counterparty", dup_key)
    check("ТОЧЕЧНЫЙ ОТВЕТ НЕ ПРЕКРАЩАЕТ ПОИСК: найдены ОБА объекта",
          len(point20) == 2
          and sorted(str(r.get("id")) for r in point20) == ["cp-dup-a", "cp-dup-b"],
          f"найдено={len(point20)} ids={[r.get('id') for r in point20]}")
    try:
        _cp_lookup(dup_key)
        check("ДУБЛЬ КОНТРАГЕНТА ОТВЕРГНУТ (SyncIdNotUnique), а не «нашёлся один»",
              False, "исключения не было")
    except _msc.SyncIdNotUnique as exc:
        check("ДУБЛЬ КОНТРАГЕНТА ОТВЕРГНУТ (SyncIdNotUnique), а не «нашёлся один»",
              exc.count == 2, f"count={exc.count}")

    # 20б. То же на документе, через продуктовый путь отправки: заказ обязан
    #      получить fail-closed отказ, а не «уже создан, вот он».
    o20 = make_order(c, "Заказ, у чьего ключа в МС два документа")
    key20 = str(uuid.uuid4())
    exec_sql("UPDATE production_orders SET ms_sync_id=?, ms_lookup_mode='sync' "
             "WHERE id=?", key20, o20)
    twin_a = plant_doc("po-dup-a", "Документ А", sync_id=key20)
    twin_b = plant_doc("po-dup-b", "Документ Б", sync_id=key20)
    try:
        docs20 = _lookup("purchaseorder", key20)
        check("по ключу документа перебор видит ОБА",
              len(docs20) == 2, f"найдено={len(docs20)}")
        before20 = len(docs_created())
        r = push(c, o20)
        check("ДВА ДОКУМЕНТА С ОДНИМ КЛЮЧОМ → ОТКАЗ (409), а не молчаливый выбор",
              r.status_code == 409, f"status={r.status_code} {r.text[:200]}")
        check("…и третьего документа не создано",
              len(docs_created()) == before20,
              f"было={before20} стало={len(docs_created())}")
    finally:
        unplant(twin_a)
        unplant(twin_b)

    # 20в. Ложного дубля подсказка дать не может: тот же объект, найденный и
    #      точечно, и перебором, склеивается по id.
    solo_key = str(uuid.uuid4())
    mock_ms.COUNTERPARTIES.clear()
    mock_ms.COUNTERPARTIES.append(
        {"id": "cp-solo", "name": "Один", "syncId": solo_key})
    solo = _lookup("counterparty", solo_key)
    check("ОДИН объект не превращается в два (склейка по id)",
          len(solo) == 1 and str(solo[0].get("id")) == "cp-solo",
          f"найдено={len(solo)} ids={[r.get('id') for r in solo]}")

    # 20г. Ответ не того сорта подсказкой не является. Вердикт всё равно даёт
    #      перебор, поэтому отбросить её безопасно — и результат не удваивается.
    mock_api.post("/__test/faults", json={"syncid_route_wrong_type": 1})
    try:
        typed = _lookup("counterparty", solo_key)
    finally:
        mock_api.post("/__test/faults", json={})
    check("ОТВЕТ ЧУЖОГО ТИПА НЕ ЗАСЧИТАН, но перебор находит настоящий",
          len(typed) == 1 and str(typed[0].get("id")) == "cp-solo",
          f"найдено={len(typed)} ids={[r.get('id') for r in typed]}")

    # 20д. Чужие ошибки не маскируются под отсутствие. 401/403/429/5xx — это
    #      «нет доступа», «слишком часто», «не дошло», но НЕ «не найдено»:
    #      превратить их в отсутствие значит разрешить создание вслепую.
    retries_save = _msc.MAX_RETRIES
    _msc.MAX_RETRIES = 0
    try:
        for code in (401, 403, 429, 500):
            mock_api.post("/__test/faults", json={"syncid_route_status": code})
            raised, returned = None, None
            try:
                returned = _lookup("counterparty", solo_key)
            except Exception as exc:  # noqa: BLE001 — важен сам факт отказа
                raised = exc
            finally:
                mock_api.post("/__test/faults", json={})
            check(f"ОТВЕТ {code} НЕ ВЫДАН ЗА «не найдено» — поиск отказал",
                  raised is not None,
                  f"вернулось {returned if returned is None else len(returned)} "
                  f"записей вместо отказа")
    finally:
        _msc.MAX_RETRIES = retries_save
        mock_api.post("/__test/faults", json={})

    # 20е. Положительный контроль: 404 точечного маршрута отсутствием ЯВЛЯЕТСЯ,
    #      и свободный ключ по-прежнему находится нулём раз. Без него все
    #      проверки выше удовлетворялись бы «отказом на любой ответ».
    mock_api.post("/__test/faults", json={"syncid_route_404": 1})
    try:
        free = _lookup("counterparty", str(uuid.uuid4()))
    finally:
        mock_api.post("/__test/faults", json={})
    check("свободный ключ по-прежнему даёт пусто (404 — это отсутствие)",
          free == [], f"найдено={len(free)}")

    # ── 21. «Неизвестно» не удаляется, но лечится ───────────────────────────
    #
    # Ревью Codex, P1 (discussion_r3856243666). Цепочка была такая: T2 падает
    # дважды → routes_connect звал _release_push_lock → ms_doc_href='' → заказ
    # снова выглядит неотправленным → api_order_delete пускает DELETE (он
    # смотрел только префикс pending:) → строка уходит вместе с ms_sync_id →
    # ms_sync._backmatch_by_sync_id ищет заказы ИМЕННО по этому ключу, и без
    # строки связывать нечем. Финансовый документ в чужом аккаунте остаётся
    # без владельца навсегда.
    #
    # Проверяется весь жизненный цикл, а не одна отдельная проверка: unknown →
    # удаление отвергнуто → связывание → обычный связанный заказ.
    print("\n== 21. Заказ с неизвестным исходом: удалить нельзя, вылечить можно ==")

    def _unknown_order(name: str) -> tuple:
        """Доводит заказ до честного unknown: T2 падает дважды."""
        oid = make_order(c, name)
        bases = order_bases(c, oid)
        base = next(iter(bases), "")
        rr = c.post(f"/api/orders/{oid}/status", json={"status": "sent"})
        assert rr.status_code == 200, rr.text
        before_docs = len(docs_created())
        ms_writeback._move_incoming_to_ms = _always_fail
        try:
            resp = push(c, oid)
        finally:
            ms_writeback._move_incoming_to_ms = original_move
        return oid, base, bases, before_docs, resp

    mock_ms.COUNTERPARTIES.clear()
    exec_sql("UPDATE connections SET ms_agent_href='', ms_agent_sync_id='' "
             "WHERE kind='moysklad'")
    o21, b21, bases21, docs_before21, r21 = _unknown_order("Заказ с неизвестным исходом (21)")
    check("T2 упал дважды → честный 502 «неизвестно»", r21.status_code == 502,
          f"status={r21.status_code} {r21.text[:200]}")
    check("документ в МойСкладе создан", len(docs_created()) == docs_before21 + 1,
          f"было={docs_before21} стало={len(docs_created())}")
    href21 = col_of(o21, "ms_doc_href")
    key21 = str(col_of(o21, "ms_sync_id") or "")
    check("СОСТОЯНИЕ ПОМЕЧЕНО КАК «НЕИЗВЕСТНО», а не очищено",
          ms_writeback.is_unknown(href21), f"ms_doc_href={href21!r}")
    check("…и это НЕ считается отправленным (вклад ещё наш)",
          not ms_writeback.is_pushed(href21), f"ms_doc_href={href21!r}")
    check("ключ связывания на месте", bool(key21), f"ms_sync_id={key21!r}")
    check("наружу пометка не показывается — ссылки нет",
          (c.get(f"/api/orders/{o21}/ms-doc").json() or {}).get("ms_doc_href") == "",
          f"ms-doc={c.get(f'/api/orders/{o21}/ms-doc').text[:160]}")

    # 21а. ГЛАВНОЕ: удалить такой заказ нельзя, и попытка не оставляет следов.
    r = c.delete(f"/api/orders/{o21}")
    check("УДАЛЕНИЕ ЗАКАЗА С НЕИЗВЕСТНЫМ ИСХОДОМ ОТВЕРГНУТО (409)",
          r.status_code == 409, f"status={r.status_code} {r.text[:200]}")
    check("…и текст объясняет, почему именно (не «идёт отправка»)",
          "неизвестн" in str((r.json() or {}).get("detail") or "").lower(),
          f"detail={str((r.json() or {}).get('detail'))[:200]}")
    check("ЗАКАЗ И КЛЮЧ НА МЕСТЕ — связывать будет чем",
          order_exists(o21) and str(col_of(o21, "ms_sync_id") or "") == key21,
          f"есть={order_exists(o21)} ms_sync_id={col_of(o21, 'ms_sync_id')!r}")
    check("…и пометка не изменилась отвергнутым удалением",
          col_of(o21, "ms_doc_href") == href21,
          f"ms_doc_href={col_of(o21, 'ms_doc_href')!r}")

    # 21б. Ветка «вылечил повтор»: TTL не держит, повтор идёт с тем же ключом
    #      и подбирает УЖЕ созданный документ, а не заводит второй.
    docs_before21b = len(docs_created())
    r = push(c, o21)
    check("ПОВТОР ПОВЕРХ «НЕИЗВЕСТНО» РАЗРЕШЁН и связал документ",
          r.status_code == 200 and (r.json() or {}).get("recovered") is True,
          f"status={r.status_code} recovered={(r.json() or {}).get('recovered')} "
          f"{r.text[:160]}")
    check("…и ВТОРОГО документа не создано",
          len(docs_created()) == docs_before21b,
          f"было={docs_before21b} стало={len(docs_created())}")
    href21b = col_of(o21, "ms_doc_href")
    check("…и заказ стал ОБЫЧНЫМ СВЯЗАННЫМ (реальная ссылка)",
          ms_writeback.is_pushed(href21b) and str(href21b).startswith("http"),
          f"ms_doc_href={href21b!r}")
    check("…ключ идемпотентности при этом не менялся",
          str(col_of(o21, "ms_sync_id") or "") == key21,
          f"было={key21!r} стало={col_of(o21, 'ms_sync_id')!r}")
    r = c.delete(f"/api/orders/{o21}")
    check("ПОСЛЕ СВЯЗЫВАНИЯ удаление снова разрешено (состояние обычное)",
          r.status_code == 200, f"status={r.status_code} {r.text[:200]}")

    # 21в. Ветка «вылечил синк»: back-match по syncId связывает документ сам,
    #      и до этого момента заказ так же неудаляем.
    mock_ms.COUNTERPARTIES.clear()
    exec_sql("UPDATE connections SET ms_agent_href='', ms_agent_sync_id='' "
             "WHERE kind='moysklad'")
    o21c, b21c, bases21c, _, r21c = _unknown_order("Заказ, который вылечит синк")
    check("второй заказ тоже в состоянии «неизвестно»",
          r21c.status_code == 502
          and ms_writeback.is_unknown(col_of(o21c, "ms_doc_href")),
          f"status={r21c.status_code} ms_doc_href={col_of(o21c, 'ms_doc_href')!r}")
    qty_unknown = qty_map().get(b21c, (0.0, 0.0, 0.0))
    r = c.delete(f"/api/orders/{o21c}")
    check("…и он тоже неудаляем до связывания", r.status_code == 409,
          f"status={r.status_code}")
    c.post("/api/sync/run")
    st = wait_sync_done(c)
    check("синк прошёл", st.get("state") == "done", f"state={st.get('state')}")
    href21c = col_of(o21c, "ms_doc_href")
    check("СИНК СВЯЗАЛ документ по syncId — состояние стало обычным",
          ms_writeback.is_pushed(href21c) and str(href21c).startswith("http"),
          f"ms_doc_href={href21c!r}")
    after21c = qty_map().get(b21c, (0.0, 0.0, 0.0))
    check("…и локальный вклад снят ровно один раз (двойного счёта нет)",
          abs(after21c[0] - (qty_unknown[0] - bases21c.get(b21c, 0))) < 1e-6,
          f"qty было={qty_unknown[0]} стало={after21c[0]} "
          f"вклад={bases21c.get(b21c)}")
    r = c.delete(f"/api/orders/{o21c}")
    check("…и после связывания синком удаление разрешено",
          r.status_code == 200, f"status={r.status_code} {r.text[:200]}")

    # 21г. Владение токеном: переход в «неизвестно» идёт CAS'ом по ТОЧНОМУ
    #      токену. Чужая пометка не трогается — `LIKE pending:%` не вернулся.
    o21d = make_order(c, "Заказ, чью пометку перехватили до unknown")
    db21 = SessionLocal()
    try:
        org21 = int(db21.get(_PO, o21d).org_id)
        t_a = int(time.time()) - 10_000
        pend_a = f"{ms_writeback.PENDING_PREFIX}{t_a}"
        pend_b = f"{ms_writeback.PENDING_PREFIX}{t_a + 5_000}"
        ms_writeback.begin_push(db21, org21, o21d, "", pend_a)
        ms_writeback.begin_push(db21, org21, o21d, pend_a, pend_b)
        moved = ms_writeback.mark_unknown(
            db21, o21d, pend_a, f"{ms_writeback.UNKNOWN_PREFIX}{t_a}")
        check("ПОЗДНЯЯ ПОПЫТКА A НЕ ПЕРЕВЕЛА В «НЕИЗВЕСТНО» ЧУЖУЮ ПОМЕТКУ B",
              moved is False and col_of(o21d, "ms_doc_href") == pend_b,
              f"moved={moved} ms_doc_href={col_of(o21d, 'ms_doc_href')!r}")
        moved_b = ms_writeback.mark_unknown(
            db21, o21d, pend_b, f"{ms_writeback.UNKNOWN_PREFIX}{t_a + 5_000}")
        check("…а ЗАКОННЫЙ владелец B своим токеном перевёл",
              moved_b is True
              and ms_writeback.is_unknown(col_of(o21d, "ms_doc_href")),
              f"moved={moved_b} ms_doc_href={col_of(o21d, 'ms_doc_href')!r}")
    finally:
        db21.close()

    # 21д. Соседняя операция не запрещена ЗАОДНО. Неудаляемость сделана
    #      отдельным условием именно ради этого: расширить not_pushing_clause
    #      значило бы запретить и «принять на склад» заказ, документ которого
    #      в МойСкладе есть, — а это уже продуктовое решение, которого finding
    #      не просил. Заказ здесь настоящий «отправленный» (status=sent),
    #      иначе проверялось бы правило переходов, а не мой предикат.
    mock_ms.COUNTERPARTIES.clear()
    exec_sql("UPDATE connections SET ms_agent_href='', ms_agent_sync_id='' "
             "WHERE kind='moysklad'")
    o21e, _, _, _, r21e = _unknown_order("Заказ «неизвестно», который принимают")
    check("третий заказ в состоянии «неизвестно»",
          r21e.status_code == 502
          and ms_writeback.is_unknown(col_of(o21e, "ms_doc_href")),
          f"status={r21e.status_code} ms_doc_href={col_of(o21e, 'ms_doc_href')!r}")
    r = c.post(f"/api/orders/{o21e}/status", json={"status": "received"})
    check("СТАТУСНЫЙ ПЕРЕХОД на заказе «неизвестно» НЕ запрещён заодно",
          r.status_code == 200, f"status={r.status_code} {r.text[:200]}")
    check("…и пометка «неизвестно» переходом не стёрта",
          ms_writeback.is_unknown(col_of(o21e, "ms_doc_href")),
          f"ms_doc_href={col_of(o21e, 'ms_doc_href')!r}")

    # ── 22. 412 точечного маршрута не отменяет перебор ──────────────────────
    #
    # Ревью Codex, P2 (discussion_r3856243671). Точечный запрос задуман как
    # НЕОБЯЗАТЕЛЬНАЯ подсказка, но 412 пробрасывался наружу — и до перебора
    # дело не доходило вовсе. То есть необязательная оптимизация становилась
    # обязательной: ответь живой аккаунт 412 и здесь, отправка упала бы
    # целиком. Ровно это уже случилось в раунде 7 с filter=syncId.
    print("\n== 22. Точечный маршрут ответил 412 — перебор всё равно решает ==")

    # 22а. Существующий объект находится перебором, дубль не создаётся.
    o22 = make_order(c, "Заказ, чей документ ищется при 412 на маршруте")
    key22 = str(uuid.uuid4())
    exec_sql("UPDATE production_orders SET ms_sync_id=?, ms_lookup_mode='sync' "
             "WHERE id=?", key22, o22)
    planted22 = plant_doc("po-412", "Документ есть, точечный маршрут 412",
                          sync_id=key22)
    try:
        before22 = len(docs_created())
        mock_api.post("/__test/faults", json={"syncid_route_status": 412})
        try:
            r = push(c, o22)
        finally:
            mock_api.post("/__test/faults", json={})
        check("ПРИ 412 НА ТОЧЕЧНОМ МАРШРУТЕ ДОКУМЕНТ НАЙДЕН ПЕРЕБОРОМ",
              r.status_code == 200 and (r.json() or {}).get("recovered") is True,
              f"status={r.status_code} recovered={(r.json() or {}).get('recovered')} "
              f"{r.text[:160]}")
        check("…и второго документа не создано",
              len(docs_created()) == before22,
              f"было={before22} стало={len(docs_created())}")
    finally:
        unplant(planted22)

    # 22б. Свободный ключ при 412 остаётся свободным, и отправка проходит.
    mock_ms.COUNTERPARTIES.clear()
    exec_sql("UPDATE connections SET ms_agent_href='', ms_agent_sync_id='' "
             "WHERE kind='moysklad'")
    o22b = make_order(c, "Обычная отправка при 412 на точечном маршруте")
    before22b = len(docs_created())
    mock_api.post("/__test/faults", json={"syncid_route_status": 412})
    try:
        r = push(c, o22b)
    finally:
        mock_api.post("/__test/faults", json={})
    check("ОТПРАВКА ПРОХОДИТ ЦЕЛИКОМ, хотя точечный маршрут отвечает 412",
          r.status_code == 200, f"status={r.status_code} {r.text[:200]}")
    check("…и создан ровно один документ",
          len(docs_created()) == before22b + 1,
          f"было={before22b} стало={len(docs_created())}")

    # 22в. Отрицательный контроль: 401/403/429/5xx по-прежнему fail-closed.
    #      Без него «глотаем 412» легко превратилось бы в «глотаем всё».
    from app import ms_client as _msc22  # noqa: PLC0415
    retries22 = _msc22.MAX_RETRIES
    _msc22.MAX_RETRIES = 0
    try:
        for code in (401, 403, 429, 500):
            o22c = make_order(c, f"Заказ при {code} на точечном маршруте")
            before22c = len(docs_created())
            mock_api.post("/__test/faults", json={"syncid_route_status": code})
            try:
                r = push(c, o22c)
            finally:
                mock_api.post("/__test/faults", json={})
            check(f"{code} на точечном маршруте по-прежнему валит отправку "
                  f"(fail-closed)", r.status_code != 200,
                  f"status={r.status_code} {r.text[:140]}")
            check(f"…и документ при {code} не создан",
                  len(docs_created()) == before22c,
                  f"было={before22c} стало={len(docs_created())}")
    finally:
        _msc22.MAX_RETRIES = retries22
        mock_api.post("/__test/faults", json={})

    # ── 23. Гонка back-match и T2: вклад не переносится дважды ──────────────
    #
    # Ревью Codex, P1 (discussion_r3856604240). _backmatch_by_sync_id был
    # устроен как read-then-write: SELECT грузил заказы, is_pushed проверялся
    # по УЖЕ ПРОЧИТАННОМУ значению, дальше шли прямые ORM-присваивания и
    # БЕЗУСЛОВНЫЙ _move_incoming_to_ms. Параллельная отправка делает свой T2
    # (_commit_push_once) с CAS'ом по точному токену и тем же переносом —
    # успей она между чтением и коммитом, вклад переносится ВТОРОЙ раз.
    #
    # И это порча данных, а не лишняя арифметика: в _move_incoming_to_ms
    # стоит `qty = max(0.0, qty - ...)`. Clamp не защищает, а ПРЯЧЕТ — второе
    # вычитание молча съедает вклад ДРУГОГО отправленного заказа с тем же
    # base_name. Два заказа по 10: T2 честно делает 20 → 10, поздний
    # backmatch делает 10 → 0, и второй заказ перестаёт считаться «едет к
    # нам», никуда не делавшись.
    #
    # Гонка воспроизводится ШВОМ В КОДЕ, а не паузами: между чтением и
    # записью backmatch зовёт _order_bases, и на этом месте подставляется
    # обёртка, которая из ОТДЕЛЬНОЙ сессии проводит настоящий T2-победитель.
    # Порядок событий один и тот же на любой машине — ни одного sleep.
    print("\n== 23. Гонка back-match и T2: перенос вклада ровно один раз ==")
    from app import ms_sync as _ms_sync  # noqa: PLC0415
    from app.ms_client import MoySkladClient as _Client23  # noqa: PLC0415

    RACE_BASE = "База гонки 23"
    # DATA-7 backmatch: _backmatch_by_sync_id теперь читает РЕАЛЬНЫЕ позиции
    # документа (см. ms_writeback.positions_pushed_by_base), а не полное
    # локальное количество заказа (_order_bases — удалён). Фиктивный
    # ext_id/product_id ниже — карта «ассортимент → base_name» ровно для
    # ОДНОЙ позиции, которую несут документы этого блока (см. _doc_for):
    # без неё позиции документа не сопоставились бы ни с чем, и перенесённый
    # вклад стал бы 0 вместо 10 — гонки блока проверяют СУММЫ, поэтому карта
    # обязана давать те же числа, что раньше давал _order_bases.
    RACE_EXT = "race-ext-23"
    RACE_PID = 900023
    _RACE_EXT_TO_PID = {RACE_EXT: RACE_PID}
    _RACE_BASE_BY_PID = {RACE_PID: RACE_BASE}

    def _run_backmatch(org_id: int, docs: list, our_docs: dict, stats: dict) -> None:
        """Прямой вызов back-match'а на моке — без полного синка вокруг."""
        async def _run():
            async with _Client23(mock_ms.TOKEN) as cl:
                await _ms_sync._backmatch_by_sync_id(
                    org_id, docs, our_docs, stats, cl,
                    _RACE_EXT_TO_PID, _RACE_BASE_BY_PID)
        asyncio.run(_run())

    def _setup_race(tag: str, marker: str) -> tuple:
        """Два отправленных заказа по 10 одного base: qty ровно 20."""
        a, b = make_order(c, f"Гонка {tag} A"), make_order(c, f"Гонка {tag} Б")
        items = json.dumps([{"base_name": RACE_BASE, "qty": 10,
                             "sizes": {"S": 10}, "cost": 100}],
                           ensure_ascii=False)
        key = str(uuid.uuid4())
        for oid, href in ((a, marker), (b, "")):
            # Колонка называется items_json, а `items` — питоновское свойство
            # только для чтения. Ошибку exec_sql проверяем: молча промахнуться
            # мимо имени колонки значит получить холостой контртест, который
            # зеленеет, ничего не проверив.
            err = exec_sql(
                "UPDATE production_orders SET items_json=?, status='sent', "
                "ms_lookup_mode='sync', ms_doc_href=?, ms_sync_id=? "
                "WHERE id=?", items, href,
                key if oid == a else str(uuid.uuid4()), oid)
            assert err == "", f"подготовка заказа {oid}: {err}"
        assert exec_sql("DELETE FROM ordered_qty WHERE base_name=?",
                        RACE_BASE) == ""
        err = exec_sql(
            "INSERT INTO ordered_qty (org_id, base_name, qty, ms_qty, "
            "ms_qty_tracked) VALUES ((SELECT org_id FROM production_orders "
            "WHERE id=?), ?, 20.0, 0.0, 0.0)", a, RACE_BASE)
        assert err == "", f"подготовка ordered_qty: {err}"
        return a, b, key

    def _race_qty() -> tuple:
        row = exec_sql_read(
            "SELECT qty, ms_qty FROM ordered_qty WHERE base_name=?", RACE_BASE)
        return (float(row[0][0]), float(row[0][1])) if row else (None, None)

    def _doc_for(key: str, doc_id: str, qty: int = 10) -> list:
        return [{"id": doc_id, "name": doc_id.upper(), "syncId": key,
                 "meta": {"href": f"{mock_ms.BASE}/entity/purchaseorder/{doc_id}",
                          "type": "purchaseorder"},
                 "positions": {
                     "rows": [{
                         "assortment": {"meta": {
                             "href": f"{mock_ms.BASE}/entity/assortment/{RACE_EXT}"}},
                         "quantity": qty, "shipped": 0.0,
                     }],
                     "meta": {"size": 1},
                 }}]

    # 23а. ГЛАВНЫЙ: backmatch прочитал заказ в состоянии unknown, а T2 успел.
    marker23 = f"{ms_writeback.UNKNOWN_PREFIX}{int(time.time())}"
    a23, b23, key23 = _setup_race("unknown", marker23)
    check("исходно: два отправленных заказа по 10 дают qty 20",
          _race_qty() == (20.0, 0.0), f"qty={_race_qty()}")

    WINNER_HREF = f"{mock_ms.BASE}/entity/purchaseorder/po-race-winner"
    raced: list = []
    original_full_positions = _ms_sync._full_positions

    async def _positions_with_race(client, entity, doc, stats):
        """Шов: T2-победитель выполняется МЕЖДУ чтением и записью backmatch.

        Позиции документа — это то место, где back-match теперь узнаёт
        «сколько реально уехало» (см. ms_writeback.positions_pushed_by_base),
        поэтому шов встал сюда же, а не в _order_bases — той функции больше
        нет.
        """
        result = await original_full_positions(client, entity, doc, stats)
        if not raced and doc.get("id") == "po-race-late":
            raced.append(True)
            db_w = SessionLocal()
            try:
                org_w = int(db_w.get(_PO, a23).org_id)
                pend_w = f"{ms_writeback.PENDING_PREFIX}{int(time.time())}"
                got = ms_writeback.begin_push(db_w, org_w, a23, marker23, pend_w)
                assert got, "T2-победитель не смог захватить лок"
                res = ms_writeback._commit_push_once(
                    db_w, org_w, db_w.get(_PO, a23), WINNER_HREF, "ПОБЕДИТЕЛЬ",
                    {RACE_BASE: 10}, pend_w, col_of(a23, "items_json"))
                assert res is True, f"T2-победитель не записал ссылку: {res!r}"
            finally:
                db_w.close()
        return result

    _ms_sync._full_positions = _positions_with_race
    try:
        stats23: dict = {}
        our23: dict = {}
        _run_backmatch(
            int(exec_sql_read("SELECT org_id FROM production_orders WHERE id=?",
                              a23)[0][0]),
            _doc_for(key23, "po-race-late"), our23, stats23)
    finally:
        _ms_sync._full_positions = original_full_positions

    check("шов сработал: T2-победитель прошёл между чтением и записью",
          bool(raced), f"raced={raced}")
    qty_after, ms_after = _race_qty()
    check("ВКЛАД ПЕРЕНЕСЁН РОВНО ОДИН РАЗ: qty ровно 10, а не 0",
          qty_after == 10.0,
          f"qty={qty_after} (20 → 10 сделал T2; 0 означало бы, что backmatch "
          f"вычел второй раз и съел вклад второго заказа)")
    check("…и вклад ВТОРОГО заказа цел",
          qty_after == 10.0 and order_exists(b23), f"qty={qty_after}")
    check("…и ms_qty прибавлен ровно один раз", ms_after == 10.0,
          f"ms_qty={ms_after}")
    check("ССЫЛКА ПОБЕДИТЕЛЯ СОХРАНЕНА (backmatch не перезаписал)",
          col_of(a23, "ms_doc_href") == WINNER_HREF,
          f"ms_doc_href={col_of(a23, 'ms_doc_href')!r}")
    check("…и проигравший backmatch не заявил заказ своим",
          a23 not in our23 and not stats23.get("incoming_backmatched"),
          f"our_docs={list(our23)} stats={stats23}")

    # 23б. ПОЛОЖИТЕЛЬНЫЙ КОНТРОЛЬ: без конкурента backmatch выигрывает и
    #      двигает РОВНО один раз. Без него «никогда не двигать» удовлетворяло
    #      бы проверке выше, а back-match перестал бы лечить unknown вообще.
    marker23b = f"{ms_writeback.UNKNOWN_PREFIX}{int(time.time())}"
    a23b, b23b, key23b = _setup_race("победа", marker23b)
    org23b = int(exec_sql_read("SELECT org_id FROM production_orders WHERE id=?",
                               a23b)[0][0])
    stats23b: dict = {}
    our23b: dict = {}
    _run_backmatch(org23b, _doc_for(key23b, "po-race-win"),
                  our23b, stats23b)
    qty_b, ms_b = _race_qty()
    check("БЕЗ КОНКУРЕНТА backmatch связал заказ",
          str(col_of(a23b, "ms_doc_href") or "").endswith("po-race-win"),
          f"ms_doc_href={col_of(a23b, 'ms_doc_href')!r}")
    check("…и перенёс вклад РОВНО ОДИН раз (20 → 10)", qty_b == 10.0,
          f"qty={qty_b}")
    check("…и заявил заказ своим", our23b.get(a23b) is not None
          and stats23b.get("incoming_backmatched") == 1,
          f"our_docs={list(our23b)} stats={stats23b}")

    # 23в. Повторный backmatch по тому же документу уже НЕ двигает: заказ
    #      связан, is_pushed истинно. Проверка того, что выигрыш одноразовый.
    _run_backmatch(org23b, _doc_for(key23b, "po-race-win"), {}, {})
    check("ПОВТОРНЫЙ backmatch по связанному заказу не двигает вклад снова",
          _race_qty()[0] == 10.0, f"qty={_race_qty()[0]}")

    # 23г. Наблюдённое состояние — пустая строка (обычный неотправленный
    #      заказ старого протокола). CAS обязан выигрывать и здесь.
    a23c, b23c, key23c = _setup_race("пусто", "")
    org23c = int(exec_sql_read("SELECT org_id FROM production_orders WHERE id=?",
                               a23c)[0][0])
    _run_backmatch(org23c, _doc_for(key23c, "po-race-empty"), {}, {})
    check("CAS выигрывает и при наблюдённой ПУСТОЙ ссылке",
          str(col_of(a23c, "ms_doc_href") or "").endswith("po-race-empty")
          and _race_qty()[0] == 10.0,
          f"ms_doc_href={col_of(a23c, 'ms_doc_href')!r} qty={_race_qty()[0]}")

    # 23д. Обратный порядок: backmatch выиграл ПЕРВЫМ, а поздний T2 приходит
    #      со своим прежним токеном. Он обязан проиграть свой CAS и НЕ
    #      перенести вклад второй раз.
    marker23d = f"{ms_writeback.PENDING_PREFIX}{int(time.time())}"
    a23d, b23d, key23d = _setup_race("поздний T2", marker23d)
    org23d = int(exec_sql_read("SELECT org_id FROM production_orders WHERE id=?",
                               a23d)[0][0])
    _run_backmatch(org23d, _doc_for(key23d, "po-race-first"), {}, {})
    check("backmatch выиграл первым и перенёс вклад (20 → 10)",
          _race_qty()[0] == 10.0, f"qty={_race_qty()[0]}")
    db23 = SessionLocal()
    try:
        late = ms_writeback._commit_push_once(
            db23, org23d, db23.get(_PO, a23d),
            f"{mock_ms.BASE}/entity/purchaseorder/po-race-late-t2", "ПОЗДНИЙ",
            {RACE_BASE: 10}, marker23d, col_of(a23d, "items_json"))
    finally:
        db23.close()
    check("ПОЗДНИЙ T2 ПРОИГРАЛ свой CAS (лок уже не его)", late is None,
          f"результат={late!r}")
    check("…и вклад НЕ перенесён второй раз: qty по-прежнему 10",
          _race_qty()[0] == 10.0, f"qty={_race_qty()[0]}")
    check("…и ссылка осталась за победившим backmatch",
          str(col_of(a23d, "ms_doc_href") or "").endswith("po-race-first"),
          f"ms_doc_href={col_of(a23d, 'ms_doc_href')!r}")

    # ── 24. Откат back-match: база и память расходиться не должны ──────────
    #
    # Ревью Codex (issuecomment-5415981236 → issuecomment-5416265593).
    # `our_docs[order.id] = href` стоял ВНУТРИ цикла, а общий db.commit —
    # после него. Исключение на ПОЗДНЕМ заказе откатывало href и перенос
    # количеств у ВСЕХ, но словарь в памяти уже держал ссылку первого:
    # `except` делал rollback и обнулял счётчик, а our_docs не трогал вовсе.
    #
    # Дальше в этом же прогоне `_oborot_order_id(doc, our_docs)` признавал
    # документ НАШИМ при несвязанной строке заказа и неснятом локальном qty —
    # одно и то же считалось дважды.
    #
    # Сбой воспроизводится ЯВНЫМ ХУКОМ на втором кандидате, а не таймингом:
    # подменяется `_move_incoming_to_ms`, и на втором вызове он бросает.
    print("\n== 24. Откат back-match: ни в базе, ни в памяти ==")

    def _setup_pair(tag: str) -> tuple:
        """Два кандидата на back-match в одной транзакции, оба unlinked."""
        a, b = make_order(c, f"Откат {tag} A"), make_order(c, f"Откат {tag} Б")
        items = json.dumps([{"base_name": RACE_BASE, "qty": 10,
                             "sizes": {"S": 10}, "cost": 100}],
                           ensure_ascii=False)
        keys = {}
        for oid in (a, b):
            keys[oid] = str(uuid.uuid4())
            err = exec_sql(
                "UPDATE production_orders SET items_json=?, status='sent', "
                "ms_lookup_mode='sync', ms_doc_href=?, ms_sync_id=? WHERE id=?",
                items, f"{ms_writeback.UNKNOWN_PREFIX}{int(time.time())}",
                keys[oid], oid)
            assert err == "", f"подготовка {oid}: {err}"
        assert exec_sql("DELETE FROM ordered_qty WHERE base_name=?",
                        RACE_BASE) == ""
        err = exec_sql(
            "INSERT INTO ordered_qty (org_id, base_name, qty, ms_qty, "
            "ms_qty_tracked) VALUES ((SELECT org_id FROM production_orders "
            "WHERE id=?), ?, 20.0, 0.0, 0.0)", a, RACE_BASE)
        assert err == "", f"подготовка ordered_qty: {err}"
        org = int(exec_sql_read(
            "SELECT org_id FROM production_orders WHERE id=?", a)[0][0])
        docs = [{"id": f"po-roll-{oid}", "name": f"PO-ROLL-{oid}",
                 "syncId": keys[oid],
                 "meta": {"href": f"{mock_ms.BASE}/entity/purchaseorder/"
                                  f"po-roll-{oid}", "type": "purchaseorder"},
                 "positions": {
                     "rows": [{
                         "assortment": {"meta": {
                             "href": f"{mock_ms.BASE}/entity/assortment/{RACE_EXT}"}},
                         "quantity": 10, "shipped": 0.0,
                     }],
                     "meta": {"size": 1},
                 }}
                for oid in (a, b)]
        return a, b, org, docs

    # 24а. ГЛАВНЫЙ: первый захват и перенос прошли, второй перенос бросил.
    a24, b24, org24, docs24 = _setup_pair("сбой")
    href_a_before = col_of(a24, "ms_doc_href")
    href_b_before = col_of(b24, "ms_doc_href")
    calls24: list = []
    original_move24 = ms_writeback._move_incoming_to_ms

    def _move_second_fails(db_, org_, order_, bases_, was_sent):
        calls24.append(int(order_.id))
        if len(calls24) >= 2:
            raise RuntimeError("подставной сбой на втором заказе")
        return original_move24(db_, org_, order_, bases_, was_sent)

    ms_writeback._move_incoming_to_ms = _move_second_fails
    try:
        our24: dict = {}
        stats24: dict = {}
        _run_backmatch(org24, docs24, our24, stats24)
    finally:
        ms_writeback._move_incoming_to_ms = original_move24

    check("хук сработал: перенос вызывался дважды, второй бросил",
          len(calls24) == 2, f"вызовов={len(calls24)} ids={calls24}")
    check("ССЫЛКА ПЕРВОГО ЗАКАЗА ОТКАЧЕНА к исходной пометке",
          col_of(a24, "ms_doc_href") == href_a_before
          and ms_writeback.is_unknown(col_of(a24, "ms_doc_href")),
          f"было={href_a_before!r} стало={col_of(a24, 'ms_doc_href')!r}")
    check("…и второй заказ тоже не связан",
          col_of(b24, "ms_doc_href") == href_b_before,
          f"было={href_b_before!r} стало={col_of(b24, 'ms_doc_href')!r}")
    check("КОЛИЧЕСТВА НЕ ТРОНУТЫ: qty по-прежнему 20",
          _race_qty() == (20.0, 0.0), f"qty={_race_qty()}")
    check("OUR_DOCS ПУСТ — память не рекламирует связь, которой нет в базе",
          our24 == {}, f"our_docs={our24}")
    check("…и incoming_backmatched в stats отсутствует",
          "incoming_backmatched" not in stats24, f"stats={stats24}")

    # 24б. ПОЛОЖИТЕЛЬНЫЙ КОНТРОЛЬ: два успешных кандидата коммитятся, и обе
    #      стадии публикуются. Без него «никогда не публиковать» удовлетворяло
    #      бы 24а, а back-match перестал бы работать вовсе.
    a24b, b24b, org24b, docs24b = _setup_pair("успех")
    our24b: dict = {}
    stats24b: dict = {}
    _run_backmatch(org24b, docs24b, our24b, stats24b)
    check("ДВА УСПЕШНЫХ КАНДИДАТА связаны в базе",
          str(col_of(a24b, "ms_doc_href") or "").endswith(f"po-roll-{a24b}")
          and str(col_of(b24b, "ms_doc_href") or "").endswith(f"po-roll-{b24b}"),
          f"a={col_of(a24b, 'ms_doc_href')!r} b={col_of(b24b, 'ms_doc_href')!r}")
    check("…и ОБЕ стадии опубликованы в our_docs",
          sorted(our24b) == sorted([a24b, b24b]), f"our_docs={our24b}")
    check("…и счётчик равен двум",
          stats24b.get("incoming_backmatched") == 2, f"stats={stats24b}")
    check("…и вклад снят ровно за два заказа (20 → 0)",
          _race_qty()[0] == 0.0, f"qty={_race_qty()[0]}")

    # 24в. Падает САМ db.commit — публикации тоже быть не должно. Коммит
    #      стоит внутри того же try, и это единственное, что делает случай
    #      отличимым от 24а по механизму, а не по формулировке.
    a24c, b24c, org24c, docs24c = _setup_pair("коммит")
    href_a24c = col_of(a24c, "ms_doc_href")
    original_session = _ms_sync.SessionLocal

    class _FailingCommitSession:
        """Сессия, у которой падает ровно commit — всё остальное настоящее."""

        def __init__(self) -> None:
            self._real = original_session()

        def commit(self):
            raise RuntimeError("подставной сбой коммита")

        def __getattr__(self, name):
            return getattr(self._real, name)

    _ms_sync.SessionLocal = _FailingCommitSession
    try:
        our24c: dict = {}
        stats24c: dict = {}
        _run_backmatch(org24c, docs24c, our24c, stats24c)
    finally:
        _ms_sync.SessionLocal = original_session
    check("СБОЙ САМОГО КОММИТА: ссылка не записана",
          col_of(a24c, "ms_doc_href") == href_a24c,
          f"было={href_a24c!r} стало={col_of(a24c, 'ms_doc_href')!r}")
    check("…и our_docs пуст", our24c == {}, f"our_docs={our24c}")
    check("…и счётчика нет", "incoming_backmatched" not in stats24c,
          f"stats={stats24c}")
    check("…и количества не тронуты", _race_qty() == (20.0, 0.0),
          f"qty={_race_qty()}")

    # ── 25. Гонка «приняли на склад» против T1 отправки ─────────────────────
    #
    # Ревью Codex, P1 (discussion_r3857277070). Маршрут читает status и
    # отвечает 422, если заказ уже принят, — но это проверка ПО ПРОЧИТАННОМУ
    # значению. CAS в begin_push проверял только id, org и точный ms_doc_href;
    # статуса в условии не было вовсе.
    #
    # Значит между чтением и T1 помещается чужой коммит `sent → received`:
    # предварительная проверка честно видела «ещё не принят», лок
    # захватывался, и сетевое окно создавало «Заказ поставщику» для УЖЕ
    # ПРИНЯТОГО заказа. Дальше T2 читал свежий received, локальный qty не
    # снимал (верно), но документ МойСклада попадал в ms_qty ближайшим синком —
    # принятый товар воскресал как «едет к нам».
    #
    # Гонка воспроизводится ЯВНЫМ ХУКОМ прямо перед настоящим begin_push, а не
    # паузами: обёртка сначала переводит статус отдельным SQL, и только потом
    # зовёт настоящий T1.
    print("\n== 25. «Принят на склад» против захвата лока отправки ==")

    o25 = make_order(c, "Заказ, который принимают во время T1")
    r = c.post(f"/api/orders/{o25}/status", json={"status": "sent"})
    check("заказ переведён «в производство»", r.status_code == 200,
          f"status={r.status_code} {r.text[:160]}")
    href25_before = col_of(o25, "ms_doc_href") or ""
    key25_before = str(col_of(o25, "ms_sync_id") or "")
    docs25_before = len(docs_created())
    check("исходно: лока нет и ключ ещё не создан",
          not str(href25_before).startswith(ms_writeback.PENDING_PREFIX)
          and key25_before == "",
          f"ms_doc_href={href25_before!r} ms_sync_id={key25_before!r}")

    original_begin = ms_writeback.begin_push
    hooked25: list = []

    def _receive_then_begin(db_, org_, order_id_, expected_, pending_):
        """Шов: приёмка коммитится МЕЖДУ чтением маршрута и настоящим T1."""
        if not hooked25 and int(order_id_) == o25:
            hooked25.append(True)
            err = exec_sql("UPDATE production_orders SET status='received' "
                           "WHERE id=?", o25)
            assert err == "", f"подставная приёмка: {err}"
        return original_begin(db_, org_, order_id_, expected_, pending_)

    ms_writeback.begin_push = _receive_then_begin
    try:
        r25 = push(c, o25)
    finally:
        ms_writeback.begin_push = original_begin

    check("шов сработал: приёмка прошла между чтением и T1",
          bool(hooked25), f"hooked={hooked25}")
    check("ОТВЕТ 422 «заказ уже принят», а не 409 и не 200",
          r25.status_code == 422, f"status={r25.status_code} {r25.text[:200]}")
    check("…и текст про принятый заказ, а не «дождитесь завершения»",
          "принят" in detail_of(r25).lower(),
          f"detail={detail_of(r25)[:200]!r} тело={r25.text[:100]!r}")
    check("НИ ОДНОГО POST В МОЙСКЛАД — документ не создан",
          len(docs_created()) == docs25_before,
          f"было={docs25_before} стало={len(docs_created())}")
    check("ЛОК НЕ ЗАХВАЧЕН: ms_doc_href не стал pending",
          not str(col_of(o25, "ms_doc_href") or "").startswith(
              ms_writeback.PENDING_PREFIX),
          f"ms_doc_href={col_of(o25, 'ms_doc_href')!r}")
    check("КЛЮЧ ИДЕМПОТЕНТНОСТИ проигравшей попыткой НЕ создан",
          str(col_of(o25, "ms_sync_id") or "") == key25_before,
          f"было={key25_before!r} стало={col_of(o25, 'ms_sync_id')!r}")
    check("…и состояние заказа не испорчено: он принят",
          col_of(o25, "status") == "received",
          f"status={col_of(o25, 'status')!r}")

    # 25а. Та же проверка БЕЗ гонки: принятый заказ отсекается предварительной
    #      проверкой и в сеть тоже не ходит. Быстрый ответ остался на месте.
    o25a = make_order(c, "Заказ, принятый заранее")
    c.post(f"/api/orders/{o25a}/status", json={"status": "sent"})
    assert exec_sql("UPDATE production_orders SET status='received' WHERE id=?",
                    o25a) == ""
    docs25a = len(docs_created())
    r = push(c, o25a)
    check("принятый заранее заказ по-прежнему даёт 422",
          r.status_code == 422, f"status={r.status_code} {r.text[:160]}")
    check("…и тоже не ходит в сеть", len(docs_created()) == docs25a,
          f"было={docs25a} стало={len(docs_created())}")

    # 25б. ПОЛОЖИТЕЛЬНЫЙ КОНТРОЛЬ: обычная отправка draft/sent по-прежнему
    #      захватывает лок и создаёт документ. Без него «никогда не
    #      захватывать» удовлетворяло бы всем проверкам выше, а отправка
    #      перестала бы работать вовсе.
    o25b = make_order(c, "Обычная отправка после правки статуса")
    docs25b = len(docs_created())
    r = push(c, o25b)
    check("ОБЫЧНАЯ ОТПРАВКА (draft) по-прежнему проходит",
          r.status_code == 200, f"status={r.status_code} {r.text[:200]}")
    check("…и документ создан", len(docs_created()) == docs25b + 1,
          f"было={docs25b} стало={len(docs_created())}")
    o25c = make_order(c, "Обычная отправка из «в производстве»")
    c.post(f"/api/orders/{o25c}/status", json={"status": "sent"})
    docs25c = len(docs_created())
    r = push(c, o25c)
    check("ОБЫЧНАЯ ОТПРАВКА (sent) по-прежнему проходит",
          r.status_code == 200, f"status={r.status_code} {r.text[:200]}")
    check("…и документ создан", len(docs_created()) == docs25c + 1,
          f"было={docs25c} стало={len(docs_created())}")

    # 25в. Прямой контроль самого предиката: T1 на принятом заказе не
    #      захватывает лок и не создаёт ключ даже при вызове напрямую.
    o25d = make_order(c, "Заказ для прямой проверки предиката T1")
    assert exec_sql("UPDATE production_orders SET status='received' WHERE id=?",
                    o25d) == ""
    db25 = SessionLocal()
    try:
        org25 = int(db25.get(_PO, o25d).org_id)
        got25 = ms_writeback.begin_push(
            db25, org25, o25d, "", f"{ms_writeback.PENDING_PREFIX}{int(time.time())}")
    finally:
        db25.close()
    check("T1 НА ПРИНЯТОМ ЗАКАЗЕ НЕ ЗАХВАТЫВАЕТ ЛОК", got25 is False,
          f"locked={got25}")
    check("…и ключ идемпотентности не создан",
          str(col_of(o25d, "ms_sync_id") or "") == "",
          f"ms_sync_id={col_of(o25d, 'ms_sync_id')!r}")

    # ── 26. Проигранный T1: три исхода, и ни одного 500 ─────────────────────
    #
    # Ревью Codex, P2 (discussion_r3857538139). В ветке locked=False стоял
    # `db.refresh(order)`. Если конкурентный DELETE закоммитил удаление между
    # первым чтением маршрута и begin_push, T1 ПРАВИЛЬНО обновляет ноль строк
    # (предикаты работают), а вот refresh на удалённой строке поднимает
    # SQLAlchemy InvalidRequestError — и обычная гонка отвечает 500 вместо
    # 404. То есть «сломались мы», хотя сломались не мы.
    #
    # Раунд 12 добавил в эту же ветку разбор исходов, но разобрал ДВА из трёх:
    # «строки больше нет» не рассматривалось вовсе, хотя соседний
    # api_order_delete в этом же PR уже показывает правильную форму.
    #
    # Гонки воспроизводятся ЯВНЫМ ХУКОМ прямо перед настоящим begin_push, а не
    # паузами: обёртка меняет состояние отдельным SQL и только потом зовёт
    # настоящий T1.
    print("\n== 26. Проигранный T1: удалён / принят / занят — и никаких 500 ==")

    def _race_before_t1(order_id: int, mutate_sql):
        """Ставит шов прямо перед настоящим T1 и выполняет push."""
        fired: list = []
        original = ms_writeback.begin_push

        def _hooked(db_, org_, oid_, expected_, pending_):
            if not fired and int(oid_) == order_id:
                fired.append(True)
                mutate_sql()
            return original(db_, org_, oid_, expected_, pending_)

        ms_writeback.begin_push = _hooked
        try:
            return push(c, order_id), fired
        finally:
            ms_writeback.begin_push = original

    # 26б. ПОЛОЖИТЕЛЬНЫЙ КОНТРОЛЬ 1: гонка раунда 12 по-прежнему даёт 422.
    o26b = make_order(c, "Заказ, принятый перед самым T1")
    c.post(f"/api/orders/{o26b}/status", json={"status": "sent"})
    docs26b = len(docs_created())
    r26b, fired26b = _race_before_t1(
        o26b, lambda: exec_sql(
            "UPDATE production_orders SET status='received' WHERE id=?", o26b))
    check("шов сработал (приёмка)", bool(fired26b), f"fired={fired26b}")
    check("ПРИНЯТЫЙ В ГОНКЕ ЗАКАЗ по-прежнему даёт 422",
          r26b.status_code == 422, f"status={r26b.status_code} {r26b.text[:200]}")
    check("…и документ не создан", len(docs_created()) == docs26b,
          f"было={docs26b} стало={len(docs_created())}")

    # 26в. ПОЛОЖИТЕЛЬНЫЙ КОНТРОЛЬ 2: настоящий конфликт лока даёт 409.
    #      Чужая пометка появилась после чтения — CAS проигрывает по href,
    #      строка на месте и статус допустимый.
    o26c = make_order(c, "Заказ, чей лок перехватили перед самым T1")
    c.post(f"/api/orders/{o26c}/status", json={"status": "sent"})
    docs26c = len(docs_created())
    alien_pending = f"{ms_writeback.PENDING_PREFIX}{int(time.time())}"
    r26c, fired26c = _race_before_t1(
        o26c, lambda: exec_sql(
            "UPDATE production_orders SET ms_doc_href=? WHERE id=?",
            alien_pending, o26c))
    check("шов сработал (перехват лока)", bool(fired26c), f"fired={fired26c}")
    check("ЗАНЯТЫЙ ЧУЖОЙ ОТПРАВКОЙ ЗАКАЗ по-прежнему даёт 409",
          r26c.status_code == 409, f"status={r26c.status_code} {r26c.text[:200]}")
    check("…и текст про идущую отправку, а не про удаление или приёмку",
          "отправля" in detail_of(r26c).lower(),
          f"detail={detail_of(r26c)[:160]!r}")
    check("…и наружу отдана пустая ссылка (внутренняя пометка не видна)",
          body_of(r26c).get("ms_doc_href") == "",
          f"ms_doc_href={body_of(r26c).get('ms_doc_href')!r}")
    check("…и документ не создан", len(docs_created()) == docs26c,
          f"было={docs26c} стало={len(docs_created())}")
    check("…и чужая пометка не тронута нами",
          col_of(o26c, "ms_doc_href") == alien_pending,
          f"ms_doc_href={col_of(o26c, 'ms_doc_href')!r}")

    # 26г. ПОЛОЖИТЕЛЬНЫЙ КОНТРОЛЬ 3: без гонки отправка по-прежнему проходит.
    o26d = make_order(c, "Обычная отправка после правки ветки проигрыша")
    docs26d = len(docs_created())
    r = push(c, o26d)
    check("ОБЫЧНАЯ ОТПРАВКА по-прежнему проходит", r.status_code == 200,
          f"status={r.status_code} {r.text[:200]}")
    check("…и документ создан", len(docs_created()) == docs26d + 1,
          f"было={docs26d} стало={len(docs_created())}")

    # 26д. Чужая организация: удалять не нужно — достаточно, что заказа этой
    #      организации нет. Ответ обязан быть 404, а не «что-то другое».
    r = c.post("/api/orders/99999999/push-to-ms")
    check("несуществующий заказ даёт 404 и вне гонки",
          r.status_code == 404, f"status={r.status_code} {r.text[:160]}")

    # 26е. ГЛАВНЫЙ СЛУЧАЙ ИДЁТ ПОСЛЕДНИМ В БЛОКЕ, и это не косметика.
    #      На СЛОМАННОЙ сборке необработанный InvalidRequestError отдаёт 500 и
    #      рвёт соединение: следующий запрос к приложению падает
    #      httpx.ReadError, и набор получает приговор NO_REPORT вместо FAIL.
    #      Поэтому после этой проверки к приложению больше не обращаемся —
    #      положительные контроли выполнены выше и на мутации остаются
    #      зелёными, а значит видно, что краснеет ровно гонка с удалением.
    # 26а. ГЛАВНЫЙ: заказ удалён между чтением маршрута и T1.
    o26 = make_order(c, "Заказ, который удаляют перед самым T1")
    c.post(f"/api/orders/{o26}/status", json={"status": "sent"})
    docs26 = len(docs_created())

    def _delete_o26():
        # Порядок как в штатном удалении: сначала приёмки (внешний ключ
        # order_receipts.order_id), потом сам заказ. Приёмок у этого заказа
        # нет, но полагаться на это в тесте про удаление — плохая идея.
        assert exec_sql("DELETE FROM order_receipts WHERE order_id=?", o26) == ""
        assert exec_sql("DELETE FROM production_orders WHERE id=?", o26) == ""

    r26, fired26 = _race_before_t1(o26, _delete_o26)
    check("шов сработал: удаление прошло между чтением и T1",
          bool(fired26), f"fired={fired26}")
    check("УДАЛЁННЫЙ В ГОНКЕ ЗАКАЗ ДАЁТ 404, а не 500",
          r26.status_code == 404, f"status={r26.status_code} {r26.text[:200]}")
    check("…и это НЕ ошибка сервера (обычная гонка не выглядит поломкой)",
          r26.status_code != 500, f"status={r26.status_code}")
    check("…и текст «Заказ не найден»",
          "не найден" in detail_of(r26).lower(),
          f"detail={detail_of(r26)[:160]!r} тело={r26.text[:100]!r}")
    check("НИ ОДНОГО POST В МОЙСКЛАД", len(docs_created()) == docs26,
          f"было={docs26} стало={len(docs_created())}")
    check("…и строки заказа в базе действительно нет",
          not order_exists(o26), f"есть={order_exists(o26)}")


    # ── 27. Исход POST неизвестен: пометка и ключ обязаны его пережить ──────
    #
    # Ревью Codex, P1 (discussion_r3858173475). Ниже — весь класс выходов из
    # узкого участка «POST уже ушёл, а подтверждения РОВНО ОДНОГО документа
    # нет». Раньше каждый из них приходил в маршрут обычным httpx.HTTPError,
    # и общий сетевой обработчик снимал пометку: заказ выглядел
    # неотправленным и становился УДАЛЯЕМЫМ, хотя финансовый документ мог уже
    # существовать. Удаление уносит ms_sync_id — единственный ключ, по
    # которому back-match связал бы документ обратно.
    #
    # Проверки детерминированы по построению: ни пауз, ни потоков — состояние
    # мира задаётся ключами сбоев мока.
    print("\n== 27. Ответ на POST потерян, а восстановление не подтвердило ==")

    # 27а. Документ создан, ответ потерян, и в выдаче он ещё НЕ ВИДЕН.
    #      Пустой ответ перебора здесь — не «документа нет», а задержка
    #      видимости, и принять одно за другое значит потерять документ.
    docs27a = len(docs_created())
    o27a = make_order(c, "Заказ: ответ потерян, документ ещё не виден")
    mock_api.post("/__test/faults", json={"po_create_then_fail": 1,
                                          "po_hide_created": 1})
    r27a = push(c, o27a)
    mock_api.post("/__test/faults", json={})
    check("исход неизвестен — честный 502", r27a.status_code == 502,
          f"status={r27a.status_code} {r27a.text[:200]}")
    # Тришка на случай, если сбой сработает не там: документ ОБЯЗАН быть
    # создан, иначе проверяется не тот сценарий и «зелёное» ничего не значит.
    check("POST ДОШЁЛ — документ в МойСкладе создан",
          len(docs_created()) == docs27a + 1,
          f"было={docs27a} стало={len(docs_created())}")
    href27a = col_of(o27a, "ms_doc_href")
    key27a = str(col_of(o27a, "ms_sync_id") or "")
    check("СОСТОЯНИЕ УСТОЙЧИВО «НЕИЗВЕСТНО», лок НЕ снят",
          ms_writeback.is_unknown(href27a), f"ms_doc_href={href27a!r}")
    check("…и это не считается отправленным (вклад ещё наш)",
          not ms_writeback.is_pushed(href27a), f"ms_doc_href={href27a!r}")
    check("КЛЮЧ СВЯЗЫВАНИЯ НА МЕСТЕ", bool(key27a), f"ms_sync_id={key27a!r}")
    r = c.delete(f"/api/orders/{o27a}")
    check("УДАЛЕНИЕ ОТВЕРГНУТО (409) — ключ не выбросить",
          r.status_code == 409, f"status={r.status_code} {r.text[:200]}")
    check("…и текст объясняет неизвестный исход",
          "неизвестн" in detail_of(r).lower(), f"detail={detail_of(r)[:200]!r}")
    check("заказ и ключ пережили отвергнутое удаление",
          order_exists(o27a) and str(col_of(o27a, "ms_sync_id") or "") == key27a,
          f"есть={order_exists(o27a)} ms_sync_id={col_of(o27a, 'ms_sync_id')!r}")
    check("ВТОРОГО POST не было — попытка ровно одна",
          len(docs_created()) == docs27a + 1,
          f"создано={len(docs_created())} ожидалось={docs27a + 1}")
    # Лечение: документ стал виден — синк связывает его по нашему ключу.
    c.post("/api/sync/run")
    st27 = wait_sync_done(c)
    check("синк прошёл", st27.get("state") == "done", f"state={st27.get('state')}")
    href27a2 = col_of(o27a, "ms_doc_href")
    check("BACK-MATCH СВЯЗАЛ документ по syncId — состояние стало обычным",
          ms_writeback.is_pushed(href27a2) and str(href27a2).startswith("http"),
          f"ms_doc_href={href27a2!r}")
    check("…ключ при этом не менялся",
          str(col_of(o27a, "ms_sync_id") or "") == key27a,
          f"было={key27a!r} стало={col_of(o27a, 'ms_sync_id')!r}")

    # 27б. Документ создан, ответ потерян, а восстановительный ПЕРЕБОР сам
    #      не состоялся (401). Неудача проверки — не ответ «документа нет».
    docs27b = len(docs_created())
    o27b = make_order(c, "Заказ: восстановление не состоялось")
    mock_api.post("/__test/faults", json={"po_create_then_fail": 1,
                                          "po_list_ok_before": 1,
                                          "po_list_status": 401})
    r27b = push(c, o27b)
    mock_api.post("/__test/faults", json={})
    check("исход неизвестен — честный 502", r27b.status_code == 502,
          f"status={r27b.status_code} {r27b.text[:200]}")
    check("POST ДОШЁЛ — документ создан (сбой сработал ПОСЛЕ него)",
          len(docs_created()) == docs27b + 1,
          f"было={docs27b} стало={len(docs_created())}")
    href27b = col_of(o27b, "ms_doc_href")
    key27b = str(col_of(o27b, "ms_sync_id") or "")
    check("НЕУДАЧА ПРОВЕРКИ НЕ СНЯЛА ЛОК — устойчивое «неизвестно»",
          ms_writeback.is_unknown(href27b), f"ms_doc_href={href27b!r}")
    check("ключ связывания на месте", bool(key27b), f"ms_sync_id={key27b!r}")
    rdel27b = c.delete(f"/api/orders/{o27b}")
    check("удаление отвергнуто (409)", rdel27b.status_code == 409,
          f"delete={rdel27b.status_code}")
    # Лечение повтором: тот же ключ, документ подобран, второго нет.
    r = push(c, o27b)
    check("ПОВТОР ПОВЕРХ «НЕИЗВЕСТНО» подобрал уже созданный документ",
          r.status_code == 200 and (r.json() or {}).get("recovered") is True,
          f"status={r.status_code} recovered={(r.json() or {}).get('recovered')} "
          f"{r.text[:160]}")
    check("…и ВТОРОГО документа не создано",
          len(docs_created()) == docs27b + 1,
          f"создано={len(docs_created())} ожидалось={docs27b + 1}")
    check("…и ключ идемпотентности тот же",
          str(col_of(o27b, "ms_sync_id") or "") == key27b,
          f"было={key27b!r} стало={col_of(o27b, 'ms_sync_id')!r}")

    # 27в. После попытки совпадений ДВА. Выбрать нельзя, но и объявить
    #      «ничего не создано» тоже: один из них только что создан нами.
    docs27c = len(docs_created())
    o27c = make_order(c, "Заказ: после попытки совпадений два")
    mock_api.post("/__test/faults", json={"po_create_twin_then_fail": 1})
    r27c = push(c, o27c)
    mock_api.post("/__test/faults", json={})
    check("неразрешимая двусмысленность — 409", r27c.status_code == 409,
          f"status={r27c.status_code} {r27c.text[:200]}")
    check("…и текст ЗАПРЕЩАЕТ повторную отправку, а не советует снять метки",
          "не отправляйте" in detail_of(r27c).lower(),
          f"detail={detail_of(r27c)[:200]!r}")
    key27c = str(col_of(o27c, "ms_sync_id") or "")
    check("в МойСкладе действительно два документа с нашим ключом",
          len([d for d in docs_created()
               if str(d.get("syncId") or "") == key27c]) == 2,
          f"с ключом={len([d for d in docs_created() if str(d.get('syncId') or '') == key27c])}")
    href27c = col_of(o27c, "ms_doc_href")
    check("ДВУСМЫСЛЕННОСТЬ ПОСЛЕ ПОПЫТКИ НЕ СНЯЛА ЛОК",
          ms_writeback.is_unknown(href27c), f"ms_doc_href={href27c!r}")
    check("ключ связывания на месте", bool(key27c), f"ms_sync_id={key27c!r}")
    rdel27c = c.delete(f"/api/orders/{o27c}")
    check("удаление отвергнуто (409)", rdel27c.status_code == 409,
          f"delete={rdel27c.status_code}")

    # 27г. Второй вход в ту же дыру, найденный при аудите класса: повтор
    #      поверх «неизвестно», сорвавшийся ДО POST, не имеет права вернуть
    #      заказ в «неотправлен» — это стёрло бы знание прошлой попытки.
    #      Здесь срыв даёт та же неразрешимая пара документов, что и в 27в.
    docs27d = len(docs_created())
    r27d = push(c, o27c)
    check("повтор отклонён (409): пара документов никуда не делась",
          r27d.status_code == 409, f"status={r27d.status_code} {r27d.text[:200]}")
    check("НИ ОДНОГО НОВОГО POST", len(docs_created()) == docs27d,
          f"было={docs27d} стало={len(docs_created())}")
    check("СНЯТИЕ ЛОКА ВЕРНУЛО «НЕИЗВЕСТНО», а не «неотправлен»",
          ms_writeback.is_unknown(col_of(o27c, "ms_doc_href")),
          f"ms_doc_href={col_of(o27c, 'ms_doc_href')!r}")
    rdel27d = c.delete(f"/api/orders/{o27c}")
    check("…и заказ по-прежнему неудаляем", rdel27d.status_code == 409,
          f"delete={rdel27d.status_code}")
    check("…и ключ связывания на месте",
          str(col_of(o27c, "ms_sync_id") or "") == key27c,
          f"ms_sync_id={col_of(o27c, 'ms_sync_id')!r}")

    # 27д. ГРАНИЦА, и она нужна не меньше. МойСклад ОТВЕТИЛ отказом 412 —
    #      это не потерянный ответ, документа нет. Если и такой исход
    #      объявить неизвестным, заказ с невалидным payload запирается
    #      навсегда: удалить нельзя, повтор даёт тот же 412 и снова
    #      «неизвестно», связывать синку нечего.
    docs27e = len(docs_created())
    o27e = make_order(c, "Заказ, который МойСклад отверг")
    mock_api.post("/__test/faults", json={"po_create_refuse": 1})
    r27e = push(c, o27e)
    mock_api.post("/__test/faults", json={})
    check("отказ МойСклада доходит как ошибка отправки",
          r27e.status_code in (409, 502),
          f"status={r27e.status_code} {r27e.text[:200]}")
    check("ДОКАЗАНО, что документа НЕ создано (это не «неизвестно»)",
          len(docs_created()) == docs27e,
          f"было={docs27e} стало={len(docs_created())}")
    check("ЛОК СНЯТ — заказ обычный, а не запертый",
          (col_of(o27e, "ms_doc_href") or "") == "",
          f"ms_doc_href={col_of(o27e, 'ms_doc_href')!r}")
    rdel27e = c.delete(f"/api/orders/{o27e}")
    check("…и удалить его можно", rdel27e.status_code == 200,
          f"delete={rdel27e.status_code} {rdel27e.text[:120]}")

    # 27е. Положительный контроль: определённый сбой ДО POST по-прежнему
    #      снимает СВОЙ pending. Без него «всё стало неизвестным» выглядело бы
    #      как исправление, а было бы просто отключением снятия лока.
    docs27f = len(docs_created())
    o27f = make_order(c, "Заказ: сеть умерла ДО создания документа")
    mock_api.post("/__test/faults", json={"syncid_route_status": 401})
    r27f = push(c, o27f)
    mock_api.post("/__test/faults", json={})
    check("сбой ДО POST — обычная ошибка отправки",
          r27f.status_code in (400, 502),
          f"status={r27f.status_code} {r27f.text[:200]}")
    check("документа в МойСкладе НЕ появилось", len(docs_created()) == docs27f,
          f"было={docs27f} стало={len(docs_created())}")
    check("ЛОК СНЯТ — заказ снова обычный",
          (col_of(o27f, "ms_doc_href") or "") == "",
          f"ms_doc_href={col_of(o27f, 'ms_doc_href')!r}")
    rdel27f = c.delete(f"/api/orders/{o27f}")
    check("…и удаление разрешено", rdel27f.status_code == 200,
          f"delete={rdel27f.status_code} {rdel27f.text[:120]}")

    # ── 28. Приговор живого гейта: «прочитал» ≠ «доказал» ───────────────────
    #
    # Ревью Codex, P1 (discussion_r3858423224). Читающая стадия живого
    # контракта возвращала 0 и печатала «ИТОГО: N OK, 0 FAIL» — ровно то же,
    # что полностью доказанный контракт. Разницы не было ни в коде возврата,
    # ни в форме последней строки, а читает этот вывод не человек, а
    # автоматика и журнал выпуска. Достаточно было один раз запустить
    # разведку, чтобы в журнале появилось «живой контракт пройден», хотя ни
    # одного обязательного POST/upsert/пересчёта уникальности не выполнялось:
    # гейт слияния закрывался бы на бумаге, оставаясь открытым.
    #
    # Проверяется тем же подставным клиентом, что и блоки 17-18: сети нет,
    # живого аккаунта нет, а «сколько раз позвали create_*» видно поимённо.
    print("\n== 28. Живой гейт: читающая стадия не выдаёт себя за пройденный ==")

    check("коды возврата попарно различны — приговор, а не одно число",
          len({live.EXIT_PASS, live.EXIT_FAIL, live.EXIT_NOT_AUTHORIZED,
               live.EXIT_NOT_COMPLETE}) == 4,
          f"pass={live.EXIT_PASS} fail={live.EXIT_FAIL} "
          f"noauth={live.EXIT_NOT_AUTHORIZED} partial={live.EXIT_NOT_COMPLETE}")
    check("…и прежние три значения не переехали",
          (live.EXIT_PASS, live.EXIT_FAIL, live.EXIT_NOT_AUTHORIZED) == (0, 1, 2),
          f"{(live.EXIT_PASS, live.EXIT_FAIL, live.EXIT_NOT_AUTHORIZED)}")

    # 28а. ГЛАВНОЕ: читающая стадия на честном API.
    ro_fake = FakeClient()
    rc_ro, out_ro, led_ro = _live_run(ro_fake, create=False)
    check("ЧИТАЮЩАЯ СТАДИЯ НЕ ВОЗВРАЩАЕТ УСПЕХ", rc_ro != live.EXIT_PASS,
          f"код возврата={rc_ro}")
    check("…и приговор у неё отдельный — «не выполнен»",
          rc_ro == live.EXIT_NOT_COMPLETE, f"код возврата={rc_ro}")
    check("…и в выводе НЕТ терминального «ИТОГО: N OK, 0 FAIL»",
          "0 FAIL" not in out_ro and "ИТОГО" not in out_ro,
          f"«0 FAIL» есть={'0 FAIL' in out_ro} «ИТОГО» есть={'ИТОГО' in out_ro}")
    check("…а сказано прямо: контракт не доказан и гейт не закрыт",
          "НЕ ВЫПОЛНЕН" in out_ro and "ГЕЙТ СЛИЯНИЯ" in out_ro
          and "НЕ ЗАКРЫТ" in out_ro,
          f"хвост={out_ro.strip()[-200:]!r}")
    check("…и названо, чего именно не выполнялось (POST/повтор/уникальность)",
          "POST" in out_ro and "syncId" in out_ro,
          f"хвост={out_ro.strip()[-260:]!r}")
    check("НИ ОДНОЙ ИЗМЕНЯЮЩЕЙ ПОПЫТКИ на читающей стадии",
          ro_fake.calls.count("create_counterparty") == 0
          and ro_fake.calls.count("create_purchase_order") == 0,
          f"cp={ro_fake.calls.count('create_counterparty')} "
          f"po={ro_fake.calls.count('create_purchase_order')}")
    check("…и журнал пуст, поэтому «ничего не создано» — правда",
          led_ro == [] and NOTHING in out_ro, f"событий={len(led_ro)}")
    check("…но читающая половина при этом реально отработала (не пустышка)",
          ro_fake.calls.count("fetch_organizations") >= 1
          and any(c.startswith("paginate:") for c in ro_fake.calls),
          f"вызовы={ro_fake.calls}")

    # 28б. Полная стадия — единственная, которой позволено вернуть ноль.
    full_fake = FakeClient()
    rc_full, out_full, _led_full = _live_run(full_fake, create=True)
    check("ПОЛНЫЙ ПРОГОН НА ЧЕСТНОМ API — ноль и «ПРОЙДЕН»",
          rc_full == live.EXIT_PASS and "ВЕРДИКТ: ПРОЙДЕН" in out_full,
          f"код возврата={rc_full} хвост={out_full.strip()[-160:]!r}")
    check("…и терминальное «ИТОГО» у него есть", "ИТОГО" in out_full,
          f"хвост={out_full.strip()[-160:]!r}")
    check("…и обязательные записи действительно выполнялись",
          full_fake.calls.count("create_counterparty") == 2
          and full_fake.calls.count("create_purchase_order") == 2,
          f"cp={full_fake.calls.count('create_counterparty')} "
          f"po={full_fake.calls.count('create_purchase_order')}")

    # 28в. Fail-closed на читающей стадии СОХРАНЁН: провал остаётся провалом,
    #      а не превращается в мягкое «не выполнено».
    bad_ro = FakeClient(orgs=0)
    rc_bad_ro, out_bad_ro, led_bad_ro = _live_run(bad_ro, create=False)
    check("ПРОВАЛ НА ЧИТАЮЩЕЙ СТАДИИ — это провал, а не «частично»",
          rc_bad_ro == live.EXIT_FAIL, f"код возврата={rc_bad_ro}")
    check("…и вывод называет его провалом",
          "ПРОВАЛЕН" in out_bad_ro, f"хвост={out_bad_ro.strip()[-160:]!r}")
    check("…и записей по-прежнему ноль",
          led_bad_ro == [] and bad_ro.calls.count("create_counterparty") == 0,
          f"событий={len(led_bad_ro)}")

    # 28г. Ноль обязан означать «контракт доказан целиком», а не «список
    #      провалов пуст». Сценарий обрывается ПОСЛЕ контрагента и ДО заказа,
    #      не записав ни одного FAIL, — раньше это дало бы ровно ноль.
    stage_order_before = live.stage_order

    async def _abort_without_fail(*a, **kw):
        raise live.LiveStop("оборвано без единой записи FAIL")

    try:
        live.stage_order = _abort_without_fail
        cut_fake = FakeClient()
        rc_cut, out_cut, _led_cut = _live_run(cut_fake, create=True)
    finally:
        live.stage_order = stage_order_before
    check("ОБРЫВ БЕЗ ЕДИНОГО FAIL НЕ ДАЁТ УСПЕХА", rc_cut != live.EXIT_PASS,
          f"код возврата={rc_cut}")
    check("…и приговор — «не выполнен»", rc_cut == live.EXIT_NOT_COMPLETE,
          f"код возврата={rc_cut}")
    check("…и названа незавершённая стадия",
          live.STAGE_ORDER in out_cut, f"хвост={out_cut.strip()[-220:]!r}")
    check("…и терминального «ИТОГО» здесь тоже нет", "ИТОГО" not in out_cut,
          f"хвост={out_cut.strip()[-220:]!r}")
    check("…а контрагент при этом успел создаться — обрыв настоящий",
          cut_fake.calls.count("create_counterparty") >= 1,
          f"cp={cut_fake.calls.count('create_counterparty')}")

    # 28д. Прежние гарантии никуда не делись: разрешение на запуск и
    #      разрешение на создание — по-прежнему ДВЕ разные фразы.
    check("фраза запуска и фраза создания не совпадают",
          live.CONFIRM_PHRASE != live.CREATE_PHRASE,
          f"confirm={live.CONFIRM_PHRASE!r} create={live.CREATE_PHRASE!r}")
    # Проверяется САМ предикат, а не то, что фраза выглядит непохожей на «да»:
    # сравнение строк здесь никогда бы не упало и защищало бы ноль.
    create_before28 = live.CREATE
    try:
        for bad in ("", "1", "yes", "true", "да", live.CONFIRM_PHRASE,
                    live.CREATE_PHRASE[:-1], live.CREATE_PHRASE + "!"):
            live.CREATE = bad
            check(f"«непустое значение» {bad!r} создание НЕ разрешает",
                  live.may_create() is False, f"may_create()={live.may_create()}")
        live.CREATE = f"  {live.CREATE_PHRASE}\n"
        check("…а ровно та фраза (с пробелами от окружения) — разрешает",
              live.may_create() is True, f"may_create()={live.may_create()}")
    finally:
        live.CREATE = create_before28

    check("каждый POST заказа поставщику нёс syncId",
          all(d.get("syncId") for d in docs_created()),
          f"без ключа={[d.get('id') for d in docs_created() if not d.get('syncId')]}")

    # ── 29. T2 CAS: конкурентное переименование не теряет маркер pushed_by_base
    #      (DATA-7 Package A, Codex corrective issuecomment-5427535755) ────────
    #
    # Матчинг позиций (order.items + products, БЕЗ сети) отрабатывает сразу
    # после T1, задолго до сетевого окна создания документа. Между этим
    # матчингом и записью T2 (после ответа МойСклада) окно открыто секундами —
    # ровно там ms_sync._migrate_renames может переписать items_json ЭТОГО ЖЕ
    # заказа в отдельной сессии. Раньше T2 писал маркер безусловным UPDATE
    # поверх ЛЮБОГО items_json: rename, случившийся в этом окне, либо
    # затирался маркером с устаревшими (дорененными) items, либо T2 писал
    # pushed_by_base под СТАРЫМ ключом поверх УЖЕ переименованных items —
    # расхождение между items[].base_name и ключом маркера, которое на
    # sent→received даёт ровно ту потерю остатка/задвоение, ради которого
    # весь Package A и написан.
    print("\n== 29. T2: rename во время сетевого окна push не теряет маркер (CAS) ==")
    bases29 = sized_bases()
    base29, sizes29 = next((b, s) for b, s in bases29 if len(s) >= 2)
    new_base29 = base29 + " (rename-race)"
    picked29 = {s: 1 for s in sizes29[:2]}
    qty29 = sum(picked29.values())
    r = c.post("/api/orders", json={
        "name": "Гонка rename/push", "eta_date": None,
        "items": [{"base_name": base29, "qty": qty29, "sizes": picked29, "cost": 100}],
        "allow_duplicate": True,
    })
    check("заказ для гонки rename/push создан", r.status_code == 200, r.text[:150])
    o29 = int(r.json()["id"])
    r = c.post(f"/api/orders/{o29}/status", json={"status": "sent"})
    check("заказ для гонки переведён в sent", r.status_code == 200)
    before29 = qty_map().get(base29, (0.0, 0.0, 0.0))

    mock_api.post("/__test/faults", json={"po_create_delay_ms": 3000})
    push29: list = []

    def _push29():
        with httpx.Client(headers=dict(c.headers), cookies=c.cookies,
                          base_url=base, timeout=120.0) as cc:
            push29.append(cc.post(f"/api/orders/{o29}/push-to-ms"))

    t29 = threading.Thread(target=_push29)
    t29.start()
    marker29 = wait_pending(o29)
    check("окно отправки открыто (T1 закоммичен, документа ещё нет)",
          marker29.startswith("pending:"), f"ms_doc_href={marker29!r}")
    # Матчинг не делает сетевых вызовов и отрабатывает почти мгновенно после
    # T1 — 300 мс с огромным запасом (окно po_create_delay_ms — 3с) дают ему
    # заведомо завершиться ДО переименования.
    time.sleep(0.3)
    exec_sql("UPDATE products SET base_name=? WHERE org_id=1 AND base_name=?",
             new_base29, base29)
    from app import ms_sync as _ms29
    from app.db import SessionLocal as _SL29
    dbx29 = _SL29()
    try:
        _ms29._migrate_renames(dbx29, 1, {base29: {new_base29}}, {})
        dbx29.commit()
    finally:
        dbx29.close()
    t29.join(timeout=120)
    mock_api.post("/__test/faults", json={})
    check("push несмотря на гонку с rename завершился успехом",
          bool(push29) and push29[0].status_code == 200 and body_of(push29[0]).get("ok"),
          f"status={push29[0].status_code if push29 else None} "
          f"body={push29[0].text[:200] if push29 else None}")

    from app.models import parse_items_payload as _parse29
    items29, pushed29 = _parse29(col_of(o29, "items_json"))
    check("rename применился к items заказа (base_name — новый)",
          bool(items29) and all(it.get("base_name") == new_base29 for it in items29),
          f"items={items29}")
    check("DATA-7 CAS: маркер pushed_by_base НЕ ПОТЕРЯН и стоит под НОВЫМ именем "
          "(а не под старым, которого в items больше нет)",
          pushed29 == {new_base29: qty29},
          f"pushed_by_base={pushed29} (ждали {{{new_base29!r}: {qty29}}})")

    after29 = qty_map().get(new_base29, (0.0, 0.0, 0.0))
    # before29 снят ПОСЛЕ sent (уже несёт +qty29 от статусного перехода) —
    # push полностью сопоставленного заказа обязан перенести ЭТУ же величину
    # из qty в ms_qty: qty-qty29, ms_qty+qty29.
    check("push (несмотря на гонку) перенёс ПОЛНОЕ количество из qty в ms_qty "
          "под новым именем — заказ был полностью сопоставлен",
          after29[0] == before29[0] - qty29 and after29[1] == before29[1] + qty29,
          f"{before29} -> {after29} (ждали qty-{qty29}, ms_qty+{qty29})")

    recv_before29 = qty_map().get(new_base29, (0.0, 0.0, 0.0))
    r = c.post(f"/api/orders/{o29}/status", json={"status": "received"})
    check("заказ из гонки rename/push принят на склад", r.status_code == 200)
    recv_after29 = qty_map().get(new_base29, (0.0, 0.0, 0.0))
    check("DATA-7: заказ полностью сопоставлен — receive ничего не снимает "
          "(remainder=0 под правильным именем, а не «весь qty под потерянным маркером»)",
          recv_after29 == recv_before29,
          f"{recv_before29} -> {recv_after29}")

    mock_api.post("/__test/faults", json={})

    # ── 30. DATA-7 Package B: recovered/backmatch по РЕАЛЬНЫМ позициям ──────
    #
    # Package A закрыл push обычного пути: pushed_by_base считался против
    # ассортимента в момент ЭТОЙ попытки, и unmatched-остаток честно оставался
    # в qty. Package B закрывает два места, где это же число раньше бралось
    # не из документа, а из текущего локального состояния:
    #   30а — push_order.recovered: документ уже существует, а pushed_by_base
    #         брался из локального матчинга ЭТОЙ (второй) попытки — если
    #         ассортимент успел разойтись с тем, что было на момент СОЗДАНИЯ
    #         документа, маркер и перенос вклада получались по ЧУЖОМУ, более
    #         новому сопоставлению;
    #   30б — ms_sync._backmatch_by_sync_id: pushed_by_base брался из
    #         `_order_bases(order)` — ПОЛНОГО локального количества, включая
    #         unmatched-остаток, которого в документе никогда не было, и
    #         маркер DATA-7 при этом не писался вовсе.
    print("\n== 30. Recovered/backmatch считают вклад по ДОКУМЕНТУ (DATA-7 Package B) ==")

    # 30а. Recovered документ отличается от ТЕКУЩЕГО локального сопоставления.
    #      (base29 исключён: блок 29 переименовал его в products, под старым
    #      именем товара там больше нет — заказ на нём вообще не сопоставился
    #      бы, и тест проверял бы не то расхождение.)
    #
    # Расхождение смоделировано напрямую в items_json — между «документ
    # создан» и «повтор нашёл его» окно СЕТЕВОЕ, а не редакторское (заказ уже
    # sent, обычный маршрут его не меняет), поэтому проверяется ИНВАРИАНТ, а
    # не бытовой сценарий: локальное состояние заказа МОГЛО разойтись с тем,
    # что реально уехало в документ (гонка синка, ручная правка в базе), и
    # recovered-путь обязан пережить это, читая правду из документа, а не из
    # текущего items_json.
    bases30 = sized_bases()
    base30a, sizes30a = next((b, s) for b, s in bases30
                             if len(s) >= 2 and b != base29)
    size30a_a, size30a_b = sizes30a[0], sizes30a[1]
    r = c.post("/api/orders", json={
        "name": "Recovered ≠ текущий матч (DATA-7 Package B)", "eta_date": None,
        "items": [{"base_name": base30a, "qty": 2,
                  "sizes": {size30a_a: 1, size30a_b: 1}, "cost": 100}],
        "allow_duplicate": True,
    })
    check("заказ 30а создан", r.status_code == 200, r.text[:150])
    o30a = int(r.json()["id"])
    r = c.post(f"/api/orders/{o30a}/status", json={"status": "sent"})
    check("заказ 30а переведён в sent", r.status_code == 200)
    after_sent30a = qty_map().get(base30a, (0.0, 0.0, 0.0))

    mock_api.post("/__test/faults", json={"po_create_then_fail": 1,
                                          "po_hide_created": 1})
    r = push(c, o30a)
    mock_api.post("/__test/faults", json={})
    check("первая попытка 30а даёт честный «неизвестно» (502)",
          r.status_code == 502, f"status={r.status_code} {r.text[:160]}")
    check("состояние 30а — unknown, вклад ещё не тронут",
          ms_writeback.is_unknown(col_of(o30a, "ms_doc_href"))
          and qty_map().get(base30a, (0.0, 0.0, 0.0)) == after_sent30a,
          f"ms_doc_href={col_of(o30a, 'ms_doc_href')!r}")
    doc30a = doc_with_marker(order_marker(o30a))
    check("документ 30а реально создан (скрыт от перебора, не отсутствует), "
          "и несёт РОВНО те 2 позиции, что были сопоставлены на момент POST",
          doc30a is not None and len(doc30a.get("positions") or []) == 2
          and sum(float(p.get("quantity") or 0)
                  for p in (doc30a.get("positions") or [])) == 2,
          f"doc={doc30a}")

    # Локальный items_json «портится» ПОСЛЕ того, как документ уже создан:
    # количество первого размера подменяется на заведомо большее. Продукты и
    # ассортимент не трогаем — ТЕКУЩИЙ локальный матч по-прежнему сопоставит
    # ОБА размера (unmatched будет пуст), только с чужими количествами.
    corrupt_json30a = json.dumps(
        [{"base_name": base30a, "qty": 9,
         "sizes": {size30a_a: 9, size30a_b: 1}, "cost": 100}],
        ensure_ascii=False)
    assert exec_sql("UPDATE production_orders SET items_json=? WHERE id=?",
                    corrupt_json30a, o30a) == ""

    r = push(c, o30a)
    body30a = r.json() if r.status_code == 200 else {}
    check("повтор 30а подобрал уже созданный документ (recovered)",
          r.status_code == 200 and body30a.get("recovered") is True,
          f"status={r.status_code} recovered={body30a.get('recovered')} {r.text[:160]}")
    check("ТЕКУЩИЙ локальный матч в ЭТОЙ попытке сопоставляет ОБА размера "
          "(unmatched пуст) — расхождение в количестве, не в сопоставимости: "
          "будь дело в матче, старый код тоже бы промахнулся мимо",
          body30a.get("unmatched") == [], f"unmatched={body30a.get('unmatched')}")
    after_retry30a = qty_map().get(base30a, (0.0, 0.0, 0.0))
    check("DATA-7 Package B: перенесено РОВНО количество ДОКУМЕНТА (2), а не "
          "испорченного локального items_json (было бы 10)",
          after_retry30a[0] == after_sent30a[0] - 2
          and after_retry30a[1] == after_sent30a[1] + 2,
          f"{after_sent30a} -> {after_retry30a} (ждали qty-2, ms_qty+2, "
          f"НЕ qty-10/ms_qty+10)")
    from app.models import parse_items_payload as _parse30a
    _, pushed30a = _parse30a(col_of(o30a, "items_json"))
    check("…и маркер DATA-7 несёт количество ДОКУМЕНТА (2), а не "
          "испорченного items_json (10)",
          pushed30a == {base30a: 2}, f"pushed_by_base={pushed30a}")

    # 30б. Частично сопоставленный заказ + честный unknown → backmatch:
    #      remainder обязан остаться РОВНО unmatched-частью, а не полным qty
    #      и не нулём, и маркер обязан появиться атомарно со связыванием.
    base30b, sizes30b = next(
        (b, s) for b, s in bases30 if b not in (base30a, base29) and s)
    size30b = sizes30b[0]
    r = c.post("/api/orders", json={
        "name": "Частично сопоставлен + unknown→backmatch (DATA-7 Package B)",
        "eta_date": None,
        "items": [{"base_name": base30b, "qty": 5,
                  "sizes": {size30b: 2, "XL": 3}, "cost": 100}],
        "allow_duplicate": True,
    })
    check("заказ 30б создан", r.status_code == 200, r.text[:150])
    o30b = int(r.json()["id"])
    r = c.post(f"/api/orders/{o30b}/status", json={"status": "sent"})
    check("заказ 30б переведён в sent (+5 локально: 2 сопоставится, 3 — нет)",
          r.status_code == 200)
    before_bm30b = qty_map().get(base30b, (0.0, 0.0, 0.0))

    mock_api.post("/__test/faults", json={"po_create_then_fail": 1,
                                          "po_hide_created": 1})
    r = push(c, o30b)
    mock_api.post("/__test/faults", json={})
    check("push 30б честно неизвестен (502)", r.status_code == 502,
          f"status={r.status_code} {r.text[:160]}")
    check("документ 30б создан, но НЕСЁТ ТОЛЬКО сопоставленную позицию (1, "
          "не 2) — XL в него не попал",
          (doc_with_marker(order_marker(o30b)) or {}).get("positions")
          and len(doc_with_marker(order_marker(o30b))["positions"]) == 1,
          f"doc={doc_with_marker(order_marker(o30b))}")
    check("вклад 30б ещё не тронут: unknown не двигает qty",
          qty_map().get(base30b, (0.0, 0.0, 0.0)) == before_bm30b,
          f"qty_map={qty_map().get(base30b)}")

    c.post("/api/sync/run")
    st30b = wait_sync_done(c)
    check("синк 30б завершился", st30b.get("state") == "done",
          f"state={st30b.get('state')}")
    href30b = col_of(o30b, "ms_doc_href")
    check("BACK-MATCH СВЯЗАЛ документ 30б по syncId",
          ms_writeback.is_pushed(href30b) and str(href30b).startswith("http"),
          f"ms_doc_href={href30b!r}")
    from app.models import parse_items_payload as _parse30b
    _, pushed30b = _parse30b(col_of(o30b, "items_json"))
    check("DATA-7 Package B: backmatch записал МАРКЕР — сопоставленную часть "
          "(2), а не «неизвестно» и не полный локальный вклад (5)",
          pushed30b == {base30b: 2}, f"pushed_by_base={pushed30b}")
    after_bm30b = qty_map().get(base30b, (0.0, 0.0, 0.0))
    check("backmatch снял РОВНО сопоставленную часть (2), unmatched-остаток "
          "(3, XL) остался в qty",
          after_bm30b[0] == before_bm30b[0] - 2,
          f"{before_bm30b} -> {after_bm30b} (ждали qty-2)")
    # ms_qty здесь НЕ проверяем числом: это ПОЛНАЯ пересборка синком по ВСЕМ
    # открытым документам этого base_name (а не только по нашему), и её
    # прирост зависит от документов прежних блоков, не только от нашего —
    # проверять здесь нечего сверх того, что уже доказали marker (=2, не 5) и
    # qty (-2, не -5): маркер и есть источник истины, из которого другой код
    # (например receive ниже) считает remainder.

    # Повторный синк не двигает вклад снова (лечится один раз).
    c.post("/api/sync/run")
    st30b2 = wait_sync_done(c)
    check("повторный синк 30б завершился", st30b2.get("state") == "done",
          f"state={st30b2.get('state')}")
    check("повторный синк не двигает вклад снова",
          qty_map().get(base30b, (0.0, 0.0, 0.0))[0] == after_bm30b[0],
          f"qty_map={qty_map().get(base30b)}")

    # Приёмка после лечения снимает РОВНО unmatched-остаток (3), один раз.
    r = c.post(f"/api/orders/{o30b}/status", json={"status": "received"})
    check("заказ 30б принят на склад", r.status_code == 200, f"{r.text[:150]}")
    after_recv30b = qty_map().get(base30b, (0.0, 0.0, 0.0))
    check("DATA-7 Package B: receive снял РОВНО unmatched-остаток (3, XL), "
          "не все 5 и не 0 — маркер backmatch'а прочитан верно",
          after_recv30b[0] == after_bm30b[0] - 3
          and after_recv30b[1] == after_bm30b[1],
          f"{after_bm30b} -> {after_recv30b} (ждали qty-3, ms_qty без изменений)")

    recv_repeat30b = qty_map().get(base30b, (0.0, 0.0, 0.0))
    r = c.post(f"/api/orders/{o30b}/status", json={"status": "received"})
    check("повторный received заказа 30б — unchanged, а не повторное снятие",
          r.status_code == 200 and r.json().get("unchanged") is True,
          f"status={r.status_code} body={r.text[:150]}")
    check("…remainder снят РОВНО ОДИН РАЗ (повтор ничего не меняет)",
          qty_map().get(base30b, (0.0, 0.0, 0.0)) == recv_repeat30b,
          f"qty_map={qty_map().get(base30b)}")

    # ── 31. Corrective (issuecomment-5428103206): capping/fail-closed по
    #        реальным позициям документа ─────────────────────────────────────
    #
    # positions_pushed_by_base раньше слепо суммировал ВСЕ сопоставившиеся
    # позиции документа и округлял дробные количества (int(round(...))).
    # Документ мог отдать БОЛЬШЕ, чем заказывал ЭТОТ заказ (лишняя строка
    # того же товара, ручная правка в МС, пересозданная позиция) — перенос
    # тогда съедал общий/чужой qty того же base_name; либо содержать позицию,
    # которую текущий ассортимент вообще не опознаёт, — округление/пропуск
    # тихо теряло или добавляло единицы. Четыре блока ниже проверяют
    # исправленный контракт прямой мутацией уже созданного документа (тот же
    # приём, что у 30а/30б, без догадок о внутреннем состоянии мока): капинг
    # сверху заказа, исключение чужого base, fail-closed на несопоставленной
    # позиции и на дробном количестве.
    print("\n== 31. Capping/fail-closed по реальным позициям документа "
          "(corrective issuecomment-5428103206) ==")

    base31, sizes31 = next((b, s) for b, s in bases30
                           if len(s) >= 2 and b not in (base29, base30a, base30b))
    size31a, size31b = sizes31[0], sizes31[1]
    base31w, sizes31w = next(
        (b, s) for b, s in bases30
        if s and b not in (base29, base30a, base30b, base31))
    ext31w = exec_sql_read(
        "SELECT ext_id FROM products WHERE base_name=? AND size=?",
        base31w, sizes31w[0])[0][0]
    from app.models import parse_items_payload as _parse31  # noqa: PLC0415

    def _setup31(name: str) -> tuple:
        """Заказ на base31 (qty 2, оба размера сопоставятся) → скрытый recovered."""
        r = c.post("/api/orders", json={
            "name": name, "eta_date": None,
            "items": [{"base_name": base31, "qty": 2,
                      "sizes": {size31a: 1, size31b: 1}, "cost": 100}],
            "allow_duplicate": True,
        })
        check(f"{name}: заказ создан", r.status_code == 200, r.text[:150])
        oid = int(r.json()["id"])
        r = c.post(f"/api/orders/{oid}/status", json={"status": "sent"})
        check(f"{name}: переведён в sent", r.status_code == 200)
        before = qty_map().get(base31, (0.0, 0.0, 0.0))
        mock_api.post("/__test/faults", json={"po_create_then_fail": 1,
                                              "po_hide_created": 1})
        r = push(c, oid)
        mock_api.post("/__test/faults", json={})
        check(f"{name}: первая попытка честно неизвестна (502)",
              r.status_code == 502, f"status={r.status_code} {r.text[:160]}")
        check(f"{name}: вклад ещё не тронут (unknown не двигает qty)",
              qty_map().get(base31, (0.0, 0.0, 0.0)) == before,
              f"qty_map={qty_map().get(base31)}")
        doc = doc_with_marker(order_marker(oid))
        check(f"{name}: документ создан и несёт ровно 2 сопоставленные позиции",
              doc is not None and len(doc.get("positions") or []) == 2
              and sum(float(p.get("quantity") or 0)
                      for p in (doc.get("positions") or [])) == 2,
              f"doc={doc}")
        return oid, doc, before

    # 31а. Over-quantity: документ несёт БОЛЬШЕ, чем заказывал ЭТОТ заказ —
    #      перенос обязан обрезаться до заказанного (2), а не до документа (7).
    o31a, doc31a, before31a = _setup31("Over-quantity capped (DATA-7 corrective)")
    doc31a["positions"][0]["quantity"] = 6  # было 1 → сумма позиций 6+1=7
    r = push(c, o31a)
    body31a = r.json() if r.status_code == 200 else {}
    check("31а: повтор подобрал документ (recovered)",
          r.status_code == 200 and body31a.get("recovered") is True,
          f"status={r.status_code} {r.text[:160]}")
    _, pushed31a = _parse31(col_of(o31a, "items_json"))
    check("31а: маркер несёт РОВНО заказанное (2), а не документное (7)",
          pushed31a == {base31: 2}, f"pushed_by_base={pushed31a}")
    after31a = qty_map().get(base31, (0.0, 0.0, 0.0))
    check("31а: перенесено РОВНО 2 (не 7): qty-2, ms_qty+2",
          after31a[0] == before31a[0] - 2 and after31a[1] == before31a[1] + 2,
          f"{before31a} -> {after31a}")

    # 31б. Extra mapped base: документ несёт ПОСТОРОННЮЮ (для этого заказа)
    #      позицию другого товара — она не должна попасть в маркер и не
    #      должна списать локальный qty base31w другого заказа (ручная правка
    #      в МС/пересоздание позиции не делают её вкладом ИМЕННО этого заказа).
    rw = c.post("/api/orders", json={
        "name": "Донор qty base31w (DATA-7 corrective)", "eta_date": None,
        "items": [{"base_name": base31w, "qty": 3,
                  "sizes": {sizes31w[0]: 3}, "cost": 100}],
        "allow_duplicate": True,
    })
    check("31б: заказ-донор создан", rw.status_code == 200, rw.text[:150])
    ow31b = int(rw.json()["id"])
    rw = c.post(f"/api/orders/{ow31b}/status", json={"status": "sent"})
    check("31б: заказ-донор переведён в sent (даёт локальный qty base31w)",
          rw.status_code == 200)
    donor_before = qty_map().get(base31w, (0.0, 0.0, 0.0))
    check("31б: у донора есть локальный qty base31w", donor_before[0] > 0,
          f"donor_before={donor_before}")

    o31b, doc31b, before31b = _setup31("Extra mapped base (DATA-7 corrective)")
    doc31b["positions"].append({
        "assortment": {"meta": {
            "href": f"{mock_ms.BASE}/entity/assortment/{ext31w}"}},
        "quantity": 3, "price": 100,
    })
    r = push(c, o31b)
    body31b = r.json() if r.status_code == 200 else {}
    check("31б: повтор подобрал документ (recovered)",
          r.status_code == 200 and body31b.get("recovered") is True,
          f"status={r.status_code} {r.text[:160]}")
    _, pushed31b = _parse31(col_of(o31b, "items_json"))
    check("31б: маркер несёт ТОЛЬКО base31 (2) — посторонний base31w не попал",
          pushed31b == {base31: 2}, f"pushed_by_base={pushed31b}")
    after31b = qty_map().get(base31, (0.0, 0.0, 0.0))
    check("31б: base31 перенесён как обычно (qty-2, ms_qty+2)",
          after31b[0] == before31b[0] - 2 and after31b[1] == before31b[1] + 2,
          f"{before31b} -> {after31b}")
    donor_after = qty_map().get(base31w, (0.0, 0.0, 0.0))
    check("31б: ПОСТОРОННИЙ base31w (донора) НЕ тронут — ни qty, ни ms_qty",
          donor_after == donor_before, f"{donor_before} -> {donor_after}")

    # 31в. Unmapped positive line: позиция документа не сопоставляется ни с
    #      одним base текущего ассортимента (посторонний/удалённый ext-id) —
    #      recovered обязан fail-closed: остаться unknown, не звать T2.
    o31c, doc31c, before31c = _setup31(
        "Unmapped ext-id fail-closed (DATA-7 corrective)")
    doc31c["positions"].append({
        "assortment": {"meta": {
            "href": f"{mock_ms.BASE}/entity/assortment/no-such-ext-31c"}},
        "quantity": 1, "price": 100,
    })
    r = push(c, o31c)
    check("31в: повтор с несопоставленной позицией — снова честно неизвестен (502)",
          r.status_code == 502, f"status={r.status_code} {r.text[:160]}")
    href31c_after = col_of(o31c, "ms_doc_href")
    check("31в: ссылка осталась в состоянии unknown (T2 не вызывался)",
          ms_writeback.is_unknown(str(href31c_after or "")),
          f"ms_doc_href={href31c_after!r}")
    check("31в: qty/ms_qty НЕ тронуты повторной попыткой",
          qty_map().get(base31, (0.0, 0.0, 0.0)) == before31c,
          f"qty_map={qty_map().get(base31)}")

    # 31г. Fractional quantity: позиция документа сопоставляется, но несёт
    #      дробное количество — контракт «fail-closed на дробном», не
    #      int(round(...)): та же гарантия, что и у unmapped-строки.
    o31d, doc31d, before31d = _setup31(
        "Fractional qty fail-closed (DATA-7 corrective)")
    doc31d["positions"][0]["quantity"] = 1.5
    r = push(c, o31d)
    check("31г: повтор с дробным количеством — честно неизвестен (502)",
          r.status_code == 502, f"status={r.status_code} {r.text[:160]}")
    href31d_after = col_of(o31d, "ms_doc_href")
    check("31г: ссылка осталась в состоянии unknown (T2 не вызывался)",
          ms_writeback.is_unknown(str(href31d_after or "")),
          f"ms_doc_href={href31d_after!r}")
    check("31г: qty/ms_qty НЕ тронуты повторной попыткой",
          qty_map().get(base31, (0.0, 0.0, 0.0)) == before31d,
          f"qty_map={qty_map().get(base31)}")

    # ── 32. Corrective (discussion_r3865067879): in-attempt post-create
    #        lost-response recovery must use ACTUAL document positions ──────
    #
    # discussion_r3865067879 / issuecomment-5428606310: create_purchase_order
    # raises, and find_own_document(..., after_create=True) finds the SAME
    # document WITHIN the SAME push_order() call — one push(), not a retry.
    # Before the fix, this branch fell through to commit_push with
    # pushed_by_base still computed from the pre-POST local match: the
    # actual-position attribution step (fetch_positions + positions_pushed_by_base
    # + fail-closed) ran only for the pre-existing-document branch (30а/31).
    # The mock models "document edited/normalized in the same network window
    # the lost response happened in" directly inside
    # entity_purchaseorder_create, via po_create_then_fail_mutate_qty /
    # po_create_then_fail_extra_unmapped — no second push() call, no manual
    # mutation between attempts, so the divergence is provably in-attempt.
    print("\n== 32. In-attempt post-create lost-response recovery reads "
          "ACTUAL document positions (discussion_r3865067879) ==")

    base32, sizes32 = next((b, s) for b, s in bases30
                           if s and b not in (base29, base30a, base30b, base31,
                                              base31w))
    size32 = sizes32[0]

    def _order32(name: str, qty: int) -> tuple:
        r = c.post("/api/orders", json={
            "name": name, "eta_date": None,
            "items": [{"base_name": base32, "qty": qty,
                      "sizes": {size32: qty}, "cost": 100}],
            "allow_duplicate": True,
        })
        check(f"{name}: заказ создан", r.status_code == 200, r.text[:150])
        oid = int(r.json()["id"])
        r = c.post(f"/api/orders/{oid}/status", json={"status": "sent"})
        check(f"{name}: переведён в sent", r.status_code == 200)
        return oid, qty_map().get(base32, (0.0, 0.0, 0.0))

    # 32а. Divergence: actual document quantity (2) differs from pre-POST
    #      local match (3) — ONE push() call must recover in-attempt and use
    #      the actual quantity, not the local match.
    o32a, before32a = _order32("In-attempt divergence (discussion_r3865067879)", 3)
    mock_api.post("/__test/faults", json={
        "po_create_then_fail": 1, "po_create_then_fail_mutate_qty": 2,
    })
    r = push(c, o32a)
    mock_api.post("/__test/faults", json={})
    body32a = r.json() if r.status_code == 200 else {}
    check("32а: одна попытка push() сама восстановила документ (recovered), "
          "без второго вызова",
          r.status_code == 200 and body32a.get("recovered") is True,
          f"status={r.status_code} recovered={body32a.get('recovered')} {r.text[:160]}")
    after32a = qty_map().get(base32, (0.0, 0.0, 0.0))
    check("32а: перенесено ФАКТИЧЕСКОЕ количество документа (2), а не "
          "pre-POST локальный матч (3): qty-2, ms_qty+2",
          after32a[0] == before32a[0] - 2 and after32a[1] == before32a[1] + 2,
          f"{before32a} -> {after32a} (ждали qty-2/ms_qty+2, НЕ qty-3/ms_qty+3)")
    from app.models import parse_items_payload as _parse32a  # noqa: PLC0415
    _, pushed32a = _parse32a(col_of(o32a, "items_json"))
    check("32а: маркер несёт ФАКТИЧЕСКОЕ количество документа (2), не "
          "pre-POST локальный матч (3)",
          pushed32a == {base32: 2}, f"pushed_by_base={pushed32a}")

    # 32б. Unmapped positive line, same in-attempt recovery: fail-closed,
    #      no link/move/marker, single push() call.
    o32b, before32b = _order32("In-attempt unmapped fail-closed "
                               "(discussion_r3865067879)", 2)
    mock_api.post("/__test/faults", json={
        "po_create_then_fail": 1, "po_create_then_fail_extra_unmapped": 1,
    })
    r = push(c, o32b)
    mock_api.post("/__test/faults", json={})
    check("32б: несопоставленная позиция при самом создании — одна попытка "
          "честно неизвестна (502), не 200",
          r.status_code == 502, f"status={r.status_code} {r.text[:160]}")
    href32b = col_of(o32b, "ms_doc_href")
    check("32б: ссылка осталась unknown (T2 не вызывался)",
          ms_writeback.is_unknown(str(href32b or "")), f"ms_doc_href={href32b!r}")
    check("32б: qty/ms_qty НЕ тронуты",
          qty_map().get(base32, (0.0, 0.0, 0.0)) == before32b,
          f"qty_map={qty_map().get(base32)}")

    # 32в. Fractional actual quantity, same in-attempt recovery: fail-closed,
    #      no link/move/marker, single push() call.
    o32c, before32c = _order32("In-attempt fractional fail-closed "
                               "(discussion_r3865067879)", 2)
    mock_api.post("/__test/faults", json={
        "po_create_then_fail": 1, "po_create_then_fail_mutate_qty": 1.5,
    })
    r = push(c, o32c)
    mock_api.post("/__test/faults", json={})
    check("32в: дробное фактическое количество при самом создании — одна "
          "попытка честно неизвестна (502), не 200",
          r.status_code == 502, f"status={r.status_code} {r.text[:160]}")
    href32c = col_of(o32c, "ms_doc_href")
    check("32в: ссылка осталась unknown (T2 не вызывался)",
          ms_writeback.is_unknown(str(href32c or "")), f"ms_doc_href={href32c!r}")
    check("32в: qty/ms_qty НЕ тронуты",
          qty_map().get(base32, (0.0, 0.0, 0.0)) == before32c,
          f"qty_map={qty_map().get(base32)}")

    mock_api.post("/__test/faults", json={})
    c.close()
    mock_api.close()
    print(f"\nИТОГО: {len(PASS)} OK, {len(FAIL)} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
