# -*- coding: utf-8 -*-
"""DATA-6: гонка между отправкой заказа (push-to-ms) и импортом «едет к нам»
(issuecomment-5431278120).

Дефект, который защищает организационный лок (см. app/ms_sync._incoming_lock,
app/routes_connect.api_order_push_to_ms). Синк (_sync_incoming_locked) читает
entity/purchaseorder ОДНИМ снимком, а затем ОДНОЙ транзакцией обнуляет и
переписывает ordered_qty.ms_qty/ms_qty_tracked по этому снимку. Если между
чтением и записью push успевает создать документ и зафиксировать свой
локальный вклад, перезапись синка стирает его до следующего цикла. Лок
процесс-локальный, по одному на организацию: синк держит его БЛОКИРУЮЩИМ
захватом на весь свой проход, push — НЕБЛОКИРУЮЩИМ (иначе HTTP-запрос
владельца завис бы на время фонового синка) и при занятости отвечает 409.

Три сценария этого файла, каждый детерминирован ЛОКАМИ/EVENT'АМИ, а не сном:

  1. Синк держит лок → push отклоняется 409 ДО CAS (T1), ДО POST в МойСклад и
     ДО изменения локального вклада (T2) — ни одного нового документа в
     МойСкладе, ссылка заказа и локальный `qty` не тронуты.
     Точка паузы синка — хук `mock_ms._race_hook("po_positions")`: документ
     po-seed-5 (150 позиций, уже есть в базовом сиде мока) заведомо не
     помещается в expand=positions (лимит ~100) и синк обязан дочитать хвост
     через GET /entity/purchaseorder/{id}/positions — эта точка выполняется
     СТРОГО под захваченным org-локом (см. ms_sync._sync_incoming).

  2. Push держит лок → синк, стартовавший параллельно, ДОКАЗУЕМО блокируется
     на `lock.acquire()` (не «пока просто не успел», а буквально стоит на
     захвате: см. _ProbeLock ниже) и продолжает читать purchaseorder ТОЛЬКО
     после освобождения лока push'ем. К этому моменту push уже создал СВОЙ
     документ — синк обязан подобрать его в том же проходе, а не потерять до
     следующего цикла: итоговые ms_qty/ms_qty_tracked обязаны включать ровно
     этот документ, не ноль и не дважды.
     Точка паузы push'а — хук `mock_ms._race_hook_async("po_create")`,
     await-точка внутри POST /entity/purchaseorder (мок держит свой event
     loop в отдельном потоке — снаружи Event выставляется потокобезопасно
     через call_soon_threadsafe).

  3. Исключение внутри захваченного лока (не сетевая ошибка, а необработанный
     Python-исключение — самый суровый случай) обязано пройти через `finally`
     и освободить лок: следующая попытка обязана снова видеть его свободным,
     а не залипшим навсегда за упавшим воркером.

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


class _AsyncPause:
    """Управляемая пауза внутри одной await-точки мока (см. `_race_hook_async`).

    Мок крутит свой event loop в отдельном потоке (ServerThread) — Event
    обязан быть создан ВНУТРИ этого потока (иначе он привязан не к тому
    loop'у), поэтому `hook()` создаёт его сам при первом вызове, а разбудить
    его снаружи можно только потокобезопасно, через call_soon_threadsafe.
    """

    def __init__(self, name: str):
        self.name = name
        self.reached = threading.Event()
        self._ready = threading.Event()
        self._loop = None
        self._evt = None

    async def hook(self, name: str) -> None:
        if name != self.name:
            return
        self._loop = asyncio.get_running_loop()
        self._evt = asyncio.Event()
        self._ready.set()
        self.reached.set()
        await self._evt.wait()

    def release(self, timeout: float = 15.0) -> None:
        got = self._ready.wait(timeout=timeout)
        assert got, f"хук {self.name!r} ни разу не сработал"
        self._loop.call_soon_threadsafe(self._evt.set)


class _ProbeLock:
    """Прокси над threading.Lock, доказывающий момент входа в блокирующий
    acquire() и момент реального получения — используется вместо голого
    сравнения по времени: сам факт, что `blocking_wait_started` выставлен, а
    `acquired` синка ещё не взведён, ДОКАЗЫВАЕТ (гарантией самого мьютекса,
    а не догадкой по часам), что синк стоит на захвате чужого лока."""

    def __init__(self):
        self._lock = threading.Lock()
        self.blocking_wait_started = threading.Event()
        self.acquired = threading.Event()

    def acquire(self, blocking: bool = True) -> bool:
        if blocking:
            self.blocking_wait_started.set()
        got = self._lock.acquire(blocking)
        if got:
            self.acquired.set()
        return got

    def release(self) -> None:
        self.acquired.clear()
        self._lock.release()


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

    # ── 1. Синк держит лок: push отклоняется ДО T1/POST/T2 ─────────────────
    print("\n== 1. Синк владеет локом: push обязан отказать ДО CAS/POST/qty ==")
    order1_id, order1_item = make_order(c, "Заказ во время синка")
    base1 = order1_item["base_name"]
    check("заказ ещё не отправлен", order_href(order1_id) == "",
          f"href={order_href(order1_id)!r}")
    created_before1 = len(mock_ms.CREATED_PURCHASE_ORDERS)
    local_qty_before1 = local_qty(org_id, base1)

    sync_holds_lock = threading.Event()
    release_sync = threading.Event()

    def _pause_sync(name: str) -> None:
        if name == "po_positions":
            sync_holds_lock.set()
            release_sync.wait(timeout=30)

    mock_ms._race_hook = _pause_sync
    try:
        r = c.post("/api/sync/run")
        check("инкрементальный синк запущен", r.status_code == 200,
              f"status={r.status_code}")
        got = sync_holds_lock.wait(timeout=30)
        check("синк дошёл до дочитывания po-seed-5 — держит org-лок", got)

        r2 = c.post(f"/api/orders/{order1_id}/push-to-ms")
        check("PUSH ОТКЛОНЁН 409, ПОКА ЛОК У СИНКА", r2.status_code == 409,
              f"status={r2.status_code} body={r2.text[:160]}")
        detail1 = str((r2.json() or {}).get("detail") or "")
        check("причина отказа — идущая синхронизация",
              "синхронизац" in detail1.lower(), f"detail={detail1[:160]}")
    finally:
        release_sync.set()
        mock_ms._race_hook = ORIG_RACE_HOOK

    st = wait_sync_done(c)
    check("синк после освобождения лока дошёл до done", st.get("state") == "done",
          f"state={st.get('state')}")

    check("НИ ОДНОГО НОВОГО ДОКУМЕНТА В МОЙСКЛАДЕ (POST не дошёл)",
          len(mock_ms.CREATED_PURCHASE_ORDERS) == created_before1,
          f"было={created_before1} стало={len(mock_ms.CREATED_PURCHASE_ORDERS)}")
    check("CAS (T1) НЕ ВЫПОЛНЕН: ссылка заказа по-прежнему пуста",
          order_href(order1_id) == "", f"href={order_href(order1_id)!r}")
    check("ЛОКАЛЬНЫЙ ВКЛАД (T2) НЕ ИЗМЕНЁН",
          local_qty(org_id, base1) == local_qty_before1,
          f"было={local_qty_before1} стало={local_qty(org_id, base1)}")

    # ── 2. Push держит лок: синк доказуемо блокируется, потом видит документ ─
    print("\n== 2. Push владеет локом: синк блокируется, затем подбирает документ ==")
    order2_id, order2_item = make_order(c, "Заказ во время push")
    base2 = order2_item["base_name"]
    qty2 = float(order2_item["qty"])
    before_ms2 = ms_qty_pair(org_id, base2)
    created_before2 = len(mock_ms.CREATED_PURCHASE_ORDERS)

    probe = _ProbeLock()
    ms_sync._incoming_locks[org_id] = probe
    pause = _AsyncPause("po_create")
    mock_ms._race_hook_async = pause.hook

    push_result: dict = {}

    def _do_push():
        resp = c.post(f"/api/orders/{order2_id}/push-to-ms")
        push_result["status"] = resp.status_code
        push_result["body"] = resp.json() if resp.content else {}

    push_thread = threading.Thread(target=_do_push, daemon=True)
    try:
        push_thread.start()
        got = pause.reached.wait(timeout=30)
        check("push дошёл до POST create и держит org-лок", got)
        check("org-лок реально захвачен (не просто «скоро»)", probe.acquired.is_set())

        r = c.post("/api/sync/run")
        check("инкрементальный синк запущен параллельно push", r.status_code == 200,
              f"status={r.status_code}")
        got = probe.blocking_wait_started.wait(timeout=30)
        check("СИНК ДОШЁЛ ДО lock.acquire() — вошёл в блокирующий захват", got)
        # Мьютекс гарантирует: пока push не вызвал release(), синк из acquire()
        # выйти не может — проверяем это здесь, а не полагаясь на время.
        check("СИНК ДЕМОНСТРАТИВНО ЗАБЛОКИРОВАН: захват всё ещё у push",
              probe.acquired.is_set() and not probe._lock.acquire(False),
              "acquire(False) не должен был получиться, пока push держит лок")
        st_mid = c.get("/api/sync/status").json()
        check("статус синка ещё не дошёл до стадии «едет к нам»",
              st_mid.get("stage") != "incoming" and st_mid.get("state") != "done",
              f"stage={st_mid.get('stage')} state={st_mid.get('state')}")

        pause.release()
        push_thread.join(timeout=30)
    finally:
        mock_ms._race_hook_async = ORIG_RACE_HOOK_ASYNC

    check("push дошёл до конца (не завис)", not push_thread.is_alive())
    check("push успешен, документ создан заново (не подобран)",
          push_result.get("status") == 200
          and push_result.get("body", {}).get("recovered") is False,
          f"status={push_result.get('status')} body={push_result.get('body')}")
    check("В МОЙСКЛАДЕ РОВНО ОДИН НОВЫЙ ДОКУМЕНТ",
          len(mock_ms.CREATED_PURCHASE_ORDERS) == created_before2 + 1,
          f"было={created_before2} стало={len(mock_ms.CREATED_PURCHASE_ORDERS)}")

    st = wait_sync_done(c)
    check("синк, разблокированный после push, дошёл до done",
          st.get("state") == "done", f"state={st.get('state')}")

    href2 = order_href(order2_id)
    check("ссылка заказа указывает на реальный (не pending/unknown) документ",
          bool(href2) and not ms_writeback.is_internal_href(href2),
          f"href={href2!r}")

    after_ms2 = ms_qty_pair(org_id, base2)
    check("СИНК СВЕЖЕ ПОДОБРАЛ НОВЫЙ ДОКУМЕНТ: ms_qty вырос ровно на его qty",
          after_ms2[0] == before_ms2[0] + qty2,
          f"было={before_ms2[0]} стало={after_ms2[0]} ожидали +{qty2}")
    check("ОН ЖЕ УЧТЁН КАК oborot_tracked (свой), а не только внешний",
          after_ms2[1] == before_ms2[1] + qty2,
          f"было={before_ms2[1]} стало={after_ms2[1]} ожидали +{qty2}")

    # ── 3. Исключение под локом: лок обязан освободиться ───────────────────
    print("\n== 3. Необработанное исключение под локом — лок обязан освободиться ==")
    order3_id, _order3_item = make_order(c, "Заказ с падением воркера")
    check("до сценария лок свободен", ms_sync.try_acquire_incoming_lock(org_id))
    ms_sync.release_incoming_lock(org_id)

    exc_reached = threading.Event()

    async def _boom(db, org_id_, order, pending_href):
        exc_reached.set()
        raise RuntimeError("DATA-6 test: форс-исключение под захваченным локом")

    ms_writeback.push_order = _boom
    try:
        r3 = c.post(f"/api/orders/{order3_id}/push-to-ms")
        check("исключение реально произошло внутри захваченного лока",
              exc_reached.is_set())
        check("необработанное исключение дошло до клиента как 5xx",
              r3.status_code >= 500, f"status={r3.status_code} body={r3.text[:160]}")
    finally:
        ms_writeback.push_order = ORIG_PUSH_ORDER

    check("ЛОК ОСВОБОЖДЁН ПОСЛЕ ИСКЛЮЧЕНИЯ (finally отработал)",
          ms_sync.try_acquire_incoming_lock(org_id))
    ms_sync.release_incoming_lock(org_id)

    c.close()
    print(f"\nИТОГО: {len(PASS)} OK, {len(FAIL)} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
