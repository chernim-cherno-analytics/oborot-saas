# -*- coding: utf-8 -*-
"""DATA-6: гонка между отправкой заказа (push-to-ms) и импортом «едет к нам»
(issuecomment-5431278120; corrective round after BLOCKED issuecomment-5432267185).

Первый заход закрыл гонку одним ОБЩИМ `threading.Lock` на организацию,
разделяемым между синком и КАЖДЫМ push. Это перезакрыло дефект (синк больше не
стирает свежий вклад push-а), но и пережало: два РАЗНЫХ push'а одной
организации, отправленные параллельно, стали конкурировать за один и тот же
мьютекс, и один получал ложную 409 «Идёт синхронизация», хотя никакого синка
не было (обнаружено независимым ревью).

Правка меняет мьютекс на writer-preferring read/write гейт на организацию
(`app/ms_sync._IncomingGate`, `app/ms_sync._incoming_gate`):

  * push — «читатель»: несколько РАЗНЫХ push'ов держат гейт одновременно
    (`try_acquire_incoming_lock` / `release_incoming_lock`, неблокирующие);
  * синк — «писатель»: `_acquire_incoming_write_lock` немедленно помечает
    гейт «ожидает записи» (это одно уже отклоняет НОВЫЕ push той же 409, даже
    если старые push ещё активны — writer preference), затем блокирующе ждёт,
    пока все уже активные push завершатся, и только тогда становится
    единоличным владельцем и читает purchaseorder — теперь гарантированно
    видя всё, что push успел создать и закоммитить.

Четыре сценария этого файла, каждый детерминирован ЛОКАМИ/EVENT'АМИ/
Condition, а не сном:

  1. Два РАЗНЫХ push'а одновременно — оба обязаны дойти до 200 и создать два
     разных документа (и один общий контрагент «Производство»). Одновременность
     доказывается не временем, а тем, что гейт (`_active_pushes`) реально
     показывает ДВА активных push-слота в один момент — под управляемой паузой
     внутри `ms_writeback.push_order` (`_ConcurrentPause` ниже).

  2. Синк, стартовавший при ДВУХ активных push, обязан: дождаться завершения
     ОБОИХ (не одного), не пропустить НИ ОДНОГО нового push, поданного, пока он
     ждёт, а затем своим собственным свежим чтением purchaseorder увидеть ОБА
     только что созданных документа.

  3. Синк владеет гейтом (writer) → push отклоняется 409 ДО CAS (T1), ДО POST
     в МойСклад и ДО изменения локального вклада (T2). Точка паузы синка —
     хук `mock_ms._race_hook("po_positions")` (документ po-seed-5, 150 позиций,
     не помещается в expand=positions и требует дочитки хвоста строго под
     захваченным гейтом, см. `ms_sync._sync_incoming`).

  4. Необработанное исключение (не сетевая ошибка) внутри захваченной секции
     обязано пройти через `finally` и освободить ресурс — проверено ОТДЕЛЬНО
     для push-слота (исключение в `ms_writeback.push_order`) и для владения
     синка (исключение в `ms_sync._sync_incoming_locked`, без единой сетевой
     подмены — монки-патч самой функции).

Свой мок на отдельном порту — можно гонять параллельно с соседними
writeback-наборами (tests/test_writeback_dup.py: 9812, .../idempotency: 9813).

Запуск из корня репозитория:  python tests/test_writeback_race.py
"""
import asyncio
import os
import sqlite3
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

DB_PATH = ROOT / "test_wb_race.db"
APP_PORT = int(os.environ.get("OBOROT_TEST_PORT", "8809"))
MOCK_PORT = int(os.environ.get("OBOROT_MOCK_PORT", "9816"))

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
from app import ms_sync  # noqa: E402
from app import ms_writeback  # noqa: E402
from app.main import app as oborot_app  # noqa: E402

mock_ms.PORT = MOCK_PORT

ORIG_RACE_HOOK = mock_ms._race_hook
ORIG_RACE_HOOK_ASYNC = mock_ms._race_hook_async
ORIG_PUSH_ORDER = ms_writeback.push_order
ORIG_SYNC_INCOMING_LOCKED = ms_sync._sync_incoming_locked


class ServerThread:
    def __init__(self, asgi_app, port: int):
        self.config = uvicorn.Config(asgi_app, host="127.0.0.1", port=port,
                                     log_level="warning")
        self.server = uvicorn.Server(self.config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self):
        self.thread.start()
        import time
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


def query_one(sql: str, *args):
    con = sqlite3.connect(DB_PATH)
    try:
        return con.execute(sql, args).fetchone()
    finally:
        con.close()


def get_org_id() -> int:
    row = query_one("SELECT id FROM orgs LIMIT 1")
    assert row is not None, "организация не создана"
    return int(row[0])


def order_href(order_id: int) -> str:
    row = query_one("SELECT ms_doc_href FROM production_orders WHERE id=?", order_id)
    return str(row[0]) if row else ""


def local_qty(org_id: int, base_name: str) -> float:
    row = query_one(
        "SELECT qty FROM ordered_qty WHERE org_id=? AND base_name=?",
        org_id, base_name,
    )
    return float(row[0]) if row else 0.0


def ms_qty_pair(org_id: int, base_name: str) -> tuple:
    row = query_one(
        "SELECT ms_qty, ms_qty_tracked FROM ordered_qty WHERE org_id=? AND base_name=?",
        org_id, base_name,
    )
    return (float(row[0]), float(row[1])) if row else (0.0, 0.0)


def wait_sync_done(c: httpx.Client, timeout: float = 240.0) -> dict:
    import time
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        last = c.get("/api/sync/status").json()
        if last.get("state") in ("done", "error"):
            return last
        time.sleep(1.0)
    return last


def make_order(c: httpx.Client, name: str) -> tuple:
    """Заказ из первой рекомендации /replenish. Возвращает (id, item)."""
    items = (c.get("/api/replenish").json() or {}).get("items") or []
    for it in items:
        sizes = {s: v["rec"] for s, v in (it.get("sizes") or {}).items() if v["rec"] > 0}
        if not sizes:
            continue
        item = {"base_name": it["base_name"], "qty": it["need"],
                "sizes": sizes, "cost": it.get("cost_price") or 0}
        r = c.post("/api/orders", json={"name": name, "eta_date": None,
                                        "items": [item], "allow_duplicate": True})
        assert r.status_code == 200, (name, r.status_code, r.text[:200])
        return int(r.json()["id"]), item
    raise RuntimeError("нет позиции с рекомендацией для тестового заказа")


def make_two_distinct_orders(c: httpx.Client, name1: str, name2: str) -> tuple:
    """Два заказа на ДВЕ РАЗНЫЕ базовые позиции одним снимком /api/replenish.

    Два последовательных вызова make_order() подряд рискуют получить ОДНУ и
    ту же «первую» рекомендацию: кэш аналитики (D-17, живёт в памяти
    процесса) не обязан успеть инвалидироваться и пересчитать «нужно» между
    первым POST /api/orders и вторым чтением /api/replenish. Здесь список
    читается ОДИН раз, и берутся первые две РАЗНЫЕ позиции — без гадания по
    времени. Возвращает ((id1, item1), (id2, item2)).
    """
    items = (c.get("/api/replenish").json() or {}).get("items") or []
    picked = []
    for it in items:
        sizes = {s: v["rec"] for s, v in (it.get("sizes") or {}).items() if v["rec"] > 0}
        if not sizes:
            continue
        picked.append((it, sizes))
        if len(picked) == 2:
            break
    if len(picked) < 2:
        raise RuntimeError("нет двух разных позиций с рекомендацией для теста")
    out = []
    for name, (it, sizes) in zip((name1, name2), picked):
        item = {"base_name": it["base_name"], "qty": it["need"],
                "sizes": sizes, "cost": it.get("cost_price") or 0}
        r = c.post("/api/orders", json={"name": name, "eta_date": None,
                                        "items": [item], "allow_duplicate": True})
        assert r.status_code == 200, (name, r.status_code, r.text[:200])
        out.append((int(r.json()["id"]), item))
    assert out[0][1]["base_name"] != out[1][1]["base_name"], \
        f"позиции обязаны быть разными: {out[0][1]['base_name']!r}"
    return tuple(out)


class _ConcurrentPause:
    """Синхронно ставит на паузу N параллельных вызовов одной и той же async
    функции (`ms_writeback.push_order`) и отпускает их все разом — единственный
    способ ДОКАЗАТЬ, что несколько запросов одновременно находятся внутри
    критической секции, а не просто быстро отработали друг за другом, без сна
    и без гадания по времени.

    В отличие от одного разделяемого `asyncio.Event`, каждый вызов получает
    СВОЙ Event: иначе второй параллельный вызов затирал бы Event первого
    (ровно эта ловушка — причина, по которой прежний `_AsyncPause` в этом
    файле поддерживал только одно попадание в хук за раз).
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._pending: list = []
        self._waiters: list = []
        self.loop = None

    async def wait_here(self) -> None:
        if self.loop is None:
            self.loop = asyncio.get_running_loop()
        evt = asyncio.Event()
        with self._lock:
            self._pending.append(evt)
            n = len(self._pending)
            waiters = list(self._waiters)
        for want, tevt in waiters:
            if n >= want:
                tevt.set()
        await evt.wait()

    def wait_for_n(self, n: int, timeout: float = 30.0) -> bool:
        with self._lock:
            if len(self._pending) >= n:
                return True
            tevt = threading.Event()
            self._waiters.append((n, tevt))
        return tevt.wait(timeout=timeout)

    def release_all(self) -> None:
        with self._lock:
            events = list(self._pending)
            self._pending.clear()
        assert self.loop is not None, "wait_here() ни разу не вызывался"
        for evt in events:
            self.loop.call_soon_threadsafe(evt.set)


def _pausing_push_order(pause: "_ConcurrentPause"):
    async def _wrapped(db, org_id_, order, pending_href):
        await pause.wait_here()
        return await ORIG_PUSH_ORDER(db, org_id_, order, pending_href)
    return _wrapped


def main() -> int:
    mock_srv = ServerThread(mock_ms.app, MOCK_PORT)
    app_srv = ServerThread(oborot_app, APP_PORT)
    mock_srv.start()
    app_srv.start()
    try:
        return run()
    finally:
        mock_ms._race_hook = ORIG_RACE_HOOK
        mock_ms._race_hook_async = ORIG_RACE_HOOK_ASYNC
        ms_writeback.push_order = ORIG_PUSH_ORDER
        ms_sync._sync_incoming_locked = ORIG_SYNC_INCOMING_LOCKED
        app_srv.stop()
        mock_srv.stop()
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(DB_PATH) + suffix)
            if p.exists():
                p.unlink()


def run() -> int:
    mock_ms.reset_purchase_orders()
    mock_ms.reset_writeback_state()
    mock_ms.reset_faults()
    base = f"http://127.0.0.1:{APP_PORT}"
    c = httpx.Client(headers={"X-Oborot-CSRF": "1"}, base_url=base, timeout=120.0)

    print("\n== Подготовка ==")
    r = c.post("/register", data={"name": "Владелец", "email": "owner@race.io",
                                  "password": "secret123", "org_name": "Гонка-бренд"})
    check("регистрация", r.status_code in (200, 302, 303), f"status={r.status_code}")
    r = c.post("/api/connect/moysklad", json={"token": mock_ms.TOKEN})
    check("токен принят", r.status_code == 200, f"status={r.status_code}")
    c.post("/api/connect/moysklad/stores", json={"ext_ids": ["st-flag", "st-web"]})
    c.post("/api/sync/initial")
    st = wait_sync_done(c)
    check("первичный синк завершился", st.get("state") == "done",
          f"state={st.get('state')} error={str(st.get('error'))[:120]}")

    org_id = get_org_id()
    gate = ms_sync._incoming_gate(org_id)

    # ── 1. Два разных push одновременно — оба обязаны дойти до 200 ─────────
    print("\n== 1. Два РАЗНЫХ push одновременно: оба обязаны дойти до 200 ==")
    order_a_id, _item_a = make_order(c, "Конкурентный push А")
    order_b_id, _item_b = make_order(c, "Конкурентный push Б")
    created_before_s1 = len(mock_ms.CREATED_PURCHASE_ORDERS)

    pause1 = _ConcurrentPause()
    ms_writeback.push_order = _pausing_push_order(pause1)
    results1: dict = {}

    def _push_s1(order_id: int, key: str) -> None:
        resp = c.post(f"/api/orders/{order_id}/push-to-ms")
        results1[key] = {"status": resp.status_code,
                         "body": resp.json() if resp.content else {}}

    t_a = threading.Thread(target=_push_s1, args=(order_a_id, "a"), daemon=True)
    t_b = threading.Thread(target=_push_s1, args=(order_b_id, "b"), daemon=True)
    try:
        t_a.start()
        t_b.start()
        got = pause1.wait_for_n(2, timeout=30)
        check("оба push одновременно попали в критическую секцию (по счётчику, не по сну)", got)
        check("ГЕЙТ РЕАЛЬНО ДЕРЖИТ ДВА АКТИВНЫХ PUSH-СЛОТА ОДНОВРЕМЕННО",
              gate._active_pushes == 2, f"active_pushes={gate._active_pushes}")
        pause1.release_all()
        t_a.join(timeout=30)
        t_b.join(timeout=30)
    finally:
        ms_writeback.push_order = ORIG_PUSH_ORDER

    check("push А дошёл до конца (не завис)", not t_a.is_alive())
    check("push Б дошёл до конца (не завис)", not t_b.is_alive())
    check("ОБА PUSH УСПЕШНЫ (200) — НИКАКОЙ ЛОЖНОЙ 409 «ИДЁТ СИНХРОНИЗАЦИЯ»",
          results1.get("a", {}).get("status") == 200
          and results1.get("b", {}).get("status") == 200,
          f"a={results1.get('a')} b={results1.get('b')}")
    check("создано РОВНО ДВА новых документа — по одному на заказ",
          len(mock_ms.CREATED_PURCHASE_ORDERS) == created_before_s1 + 2,
          f"было={created_before_s1} стало={len(mock_ms.CREATED_PURCHASE_ORDERS)}")
    href_a1 = order_href(order_a_id)
    href_b1 = order_href(order_b_id)
    check("это ДВА РАЗНЫХ документа, оба реальные (не pending/unknown)",
          bool(href_a1) and bool(href_b1) and href_a1 != href_b1
          and not ms_writeback.is_internal_href(href_a1)
          and not ms_writeback.is_internal_href(href_b1),
          f"a={href_a1!r} b={href_b1!r}")
    prod_agents1 = [cp for cp in mock_ms.COUNTERPARTIES if cp["name"] == ms_writeback.AGENT_NAME]
    check("контрагент «Производство» создан РОВНО ОДИН на двоих (гонка не задвоила)",
          len(prod_agents1) == 1, f"контрагентов={len(prod_agents1)}")
    check("гейт полностью свободен после обоих push",
          ms_sync.try_acquire_incoming_lock(org_id))
    ms_sync.release_incoming_lock(org_id)

    # ── 2. Синк при двух активных push: ждёт обоих, не пропускает новый ────
    print("\n== 2. Синк, стартовавший при двух активных push, ждёт обоих, "
          "блокирует новый push, затем видит оба документа ==")
    (order_c_id, item_c), (order_d_id, item_d) = make_two_distinct_orders(
        c, "Заказ C во время push", "Заказ D во время push")
    base_c, base_d = item_c["base_name"], item_d["base_name"]
    qty_c, qty_d = float(item_c["qty"]), float(item_d["qty"])
    before_ms_c = ms_qty_pair(org_id, base_c)
    before_ms_d = ms_qty_pair(org_id, base_d)
    created_before_s2 = len(mock_ms.CREATED_PURCHASE_ORDERS)

    pause2 = _ConcurrentPause()
    ms_writeback.push_order = _pausing_push_order(pause2)
    results2: dict = {}

    def _push_s2(order_id: int, key: str) -> None:
        resp = c.post(f"/api/orders/{order_id}/push-to-ms")
        results2[key] = {"status": resp.status_code,
                         "body": resp.json() if resp.content else {}}

    t_c = threading.Thread(target=_push_s2, args=(order_c_id, "c"), daemon=True)
    t_d = threading.Thread(target=_push_s2, args=(order_d_id, "d"), daemon=True)
    try:
        t_c.start()
        t_d.start()
        got = pause2.wait_for_n(2, timeout=30)
        check("оба push (C и D) держат активный слот перед стартом синка", got)
        check("гейт подтверждает два активных push-слота",
              gate._active_pushes == 2, f"active_pushes={gate._active_pushes}")

        r = c.post("/api/sync/run")
        check("инкрементальный синк запущен параллельно двум push",
              r.status_code == 200, f"status={r.status_code}")

        def _sync_pending_predicate() -> bool:
            return gate._sync_pending

        with gate._cond:
            sync_pending = gate._cond.wait_for(_sync_pending_predicate, timeout=30)
        check("СИНК ВСТАЛ В ОЧЕРЕДЬ НА ЗАПИСЬ (writer-preference), ещё НЕ владеет",
              sync_pending and not gate._sync_owned,
              f"sync_pending={gate._sync_pending} sync_owned={gate._sync_owned}")

        order_e_id, _item_e = make_order(c, "Заказ E — новый push при ожидающем синке")
        r_new_push = c.post(f"/api/orders/{order_e_id}/push-to-ms")
        check("НОВЫЙ PUSH, ПОДАННЫЙ ПОКА СИНК ЖДЁТ, ОТКЛОНЁН 409 (writer-preference)",
              r_new_push.status_code == 409, f"status={r_new_push.status_code}")
        check("оба исходных push всё ещё активны — синк их не пропустил и не забрал раньше времени",
              gate._active_pushes == 2 and not gate._sync_owned,
              f"active_pushes={gate._active_pushes} sync_owned={gate._sync_owned}")

        pause2.release_all()
        t_c.join(timeout=30)
        t_d.join(timeout=30)
    finally:
        ms_writeback.push_order = ORIG_PUSH_ORDER

    check("push C дошёл до конца (не завис)", not t_c.is_alive())
    check("push D дошёл до конца (не завис)", not t_d.is_alive())
    check("ОБА push (C и D) успешны (200)",
          results2.get("c", {}).get("status") == 200
          and results2.get("d", {}).get("status") == 200,
          f"c={results2.get('c')} d={results2.get('d')}")
    check("создано ровно два новых документа (C и D)",
          len(mock_ms.CREATED_PURCHASE_ORDERS) == created_before_s2 + 2,
          f"было={created_before_s2} стало={len(mock_ms.CREATED_PURCHASE_ORDERS)}")

    st = wait_sync_done(c)
    check("синк, разблокированный после ОБОИХ push, дошёл до done",
          st.get("state") == "done", f"state={st.get('state')}")

    after_ms_c = ms_qty_pair(org_id, base_c)
    after_ms_d = ms_qty_pair(org_id, base_d)
    check("СИНК УВИДЕЛ ДОКУМЕНТ C: ms_qty вырос ровно на его qty",
          after_ms_c[0] == before_ms_c[0] + qty_c,
          f"было={before_ms_c[0]} стало={after_ms_c[0]} ожидали +{qty_c}")
    check("СИНК УВИДЕЛ ДОКУМЕНТ D: ms_qty вырос ровно на его qty",
          after_ms_d[0] == before_ms_d[0] + qty_d,
          f"было={before_ms_d[0]} стало={after_ms_d[0]} ожидали +{qty_d}")
    check("оба учтены как oborot_tracked (свои), а не только внешние",
          after_ms_c[1] == before_ms_c[1] + qty_c
          and after_ms_d[1] == before_ms_d[1] + qty_d,
          f"c={after_ms_c} d={after_ms_d}")

    # ── 3. Синк владеет гейтом: push отклоняется ДО CAS/POST/T2 ────────────
    print("\n== 3. Синк владеет гейтом (writer): push отклоняется ДО CAS(T1)/POST/T2 ==")
    order_f_id, item_f = make_order(c, "Заказ во время синка (owner)")
    base_f = item_f["base_name"]
    check("заказ ещё не отправлен", order_href(order_f_id) == "",
          f"href={order_href(order_f_id)!r}")
    created_before_s3 = len(mock_ms.CREATED_PURCHASE_ORDERS)
    local_qty_before_f = local_qty(org_id, base_f)

    sync_holds_gate = threading.Event()
    release_sync = threading.Event()

    def _pause_sync(name: str) -> None:
        if name == "po_positions":
            sync_holds_gate.set()
            release_sync.wait(timeout=30)

    mock_ms._race_hook = _pause_sync
    try:
        r = c.post("/api/sync/run")
        check("инкрементальный синк запущен", r.status_code == 200,
              f"status={r.status_code}")
        got = sync_holds_gate.wait(timeout=30)
        check("синк дошёл до дочитывания po-seed-5 — держит гейт как writer", got)
        check("гейт действительно во владении синка (sync_owned)", gate._sync_owned)

        r2 = c.post(f"/api/orders/{order_f_id}/push-to-ms")
        check("PUSH ОТКЛОНЁН 409, ПОКА ГЕЙТ У СИНКА", r2.status_code == 409,
              f"status={r2.status_code} body={r2.text[:160]}")
        detail_f = str((r2.json() or {}).get("detail") or "")
        check("причина отказа — идущая синхронизация",
              "синхронизац" in detail_f.lower(), f"detail={detail_f[:160]}")
    finally:
        release_sync.set()
        mock_ms._race_hook = ORIG_RACE_HOOK

    st = wait_sync_done(c)
    check("синк после освобождения гейта дошёл до done", st.get("state") == "done",
          f"state={st.get('state')}")
    check("НИ ОДНОГО НОВОГО ДОКУМЕНТА В МОЙСКЛАДЕ (POST не дошёл)",
          len(mock_ms.CREATED_PURCHASE_ORDERS) == created_before_s3,
          f"было={created_before_s3} стало={len(mock_ms.CREATED_PURCHASE_ORDERS)}")
    check("CAS (T1) НЕ ВЫПОЛНЕН: ссылка заказа по-прежнему пуста",
          order_href(order_f_id) == "", f"href={order_href(order_f_id)!r}")
    check("ЛОКАЛЬНЫЙ ВКЛАД (T2) НЕ ИЗМЕНЁН",
          local_qty(org_id, base_f) == local_qty_before_f,
          f"было={local_qty_before_f} стало={local_qty(org_id, base_f)}")

    # ── 4. Исключения освобождают push-слот и владение синка ───────────────
    print("\n== 4. Исключения корректно освобождают push-слот и владение синка ==")
    order_g_id, _item_g = make_order(c, "Заказ с падением push")
    check("до сценария push-слот свободен", ms_sync.try_acquire_incoming_lock(org_id))
    ms_sync.release_incoming_lock(org_id)

    exc_reached_push = threading.Event()

    async def _boom_push(db, org_id_, order, pending_href):
        exc_reached_push.set()
        raise RuntimeError("DATA-6 test: форс-исключение под захваченным push-слотом")

    ms_writeback.push_order = _boom_push
    try:
        r4 = c.post(f"/api/orders/{order_g_id}/push-to-ms")
        check("исключение реально произошло внутри захваченного push-слота",
              exc_reached_push.is_set())
        check("необработанное исключение дошло до клиента как 5xx",
              r4.status_code >= 500, f"status={r4.status_code} body={r4.text[:160]}")
    finally:
        ms_writeback.push_order = ORIG_PUSH_ORDER

    check("PUSH-СЛОТ ОСВОБОЖДЁН ПОСЛЕ ИСКЛЮЧЕНИЯ (finally отработал)",
          ms_sync.try_acquire_incoming_lock(org_id))
    ms_sync.release_incoming_lock(org_id)

    # Необработанное исключение внутри BaseHTTPMiddleware оставляет keep-alive
    # соединение httpx использовать нельзя: сервер закрывает сокет ПОСЛЕ
    # отдачи 500 (это уже произошло выше и там доказано), и следующий запрос
    # через тот же пул соединений ловит «Connection reset by peer» — не баг
    # приложения, а такое соединение. Открываем новое соединение той же
    # сессией (те же cookies), а не рискуем пулом старого клиента.
    cookies_after_boom = dict(c.cookies)
    c.close()
    c = httpx.Client(headers={"X-Oborot-CSRF": "1"}, cookies=cookies_after_boom,
                     base_url=base, timeout=120.0)

    exc_reached_sync = threading.Event()

    async def _boom_sync(*_a, **_kw):
        exc_reached_sync.set()
        raise RuntimeError("DATA-6 test: форс-исключение под захваченным гейтом синка")

    ms_sync._sync_incoming_locked = _boom_sync
    try:
        r5 = c.post("/api/sync/run")
        check("инкрементальный синк (форс-исключение) запущен",
              r5.status_code == 200, f"status={r5.status_code}")
        st5 = wait_sync_done(c)
        check("исключение реально произошло внутри захваченного гейта синка",
              exc_reached_sync.is_set())
        check("синк честно завершился состоянием error, а не завис и не потерялся",
              st5.get("state") == "error", f"state={st5.get('state')}")
    finally:
        ms_sync._sync_incoming_locked = ORIG_SYNC_INCOMING_LOCKED

    check("ВЛАДЕНИЕ ГЕЙТОМ СИНКОМ СНЯТО ПОСЛЕ ИСКЛЮЧЕНИЯ (finally отработал)",
          not gate._sync_owned and not gate._sync_pending,
          f"sync_owned={gate._sync_owned} sync_pending={gate._sync_pending}")
    check("PUSH ПРОХОДИТ СРАЗУ ПОСЛЕ — ГЕЙТ ПОЛНОСТЬЮ СВОБОДЕН",
          ms_sync.try_acquire_incoming_lock(org_id))
    ms_sync.release_incoming_lock(org_id)

    c.close()
    print(f"\nИТОГО: {len(PASS)} OK, {len(FAIL)} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
