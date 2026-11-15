"""Кэш снапшота: один расчёт на организацию, фоновое обновление, честный сброс.

Прод 15.11: холодный снапшот живой организации считается до 15 секунд, а TTL
10 минут. Проверяем, что ускорение не отдаёт устаревшие числа там, где данные
менялись: после invalidate() старый снапшот не отдаётся никогда.
"""
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DB_PATH = ROOT / "test_snapshot_cache.db"
for suffix in ("", "-wal", "-shm"):
    p = Path(str(DB_PATH) + suffix)
    if p.exists():
        p.unlink()
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["SCHEDULER_ENABLED"] = "0"

from fastapi.testclient import TestClient  # noqa: E402

from app import analytics, scheduler  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Org  # noqa: E402

OK = FAIL = 0


def check(name, cond, detail=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  OK   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


calls = []
delay = {"s": 0.0}
real_compute = analytics._compute_snapshot


def fake_compute(db, org):
    calls.append(org.id)
    if delay["s"]:
        time.sleep(delay["s"])
    return {"n": len(calls), "org": org.id}


def expire(org_id, seconds_ago=1):
    with analytics._cache_lock:
        exp, snap, day = analytics._cache[org_id]
        analytics._cache[org_id] = (time.monotonic() - seconds_ago, snap, day)


def wait_idle(org_id, timeout=5):
    t = time.time()
    while time.time() - t < timeout:
        with analytics._cache_lock:
            if org_id not in analytics._inflight:
                return True
        time.sleep(0.01)
    return False


with TestClient(app, headers={"X-Oborot-CSRF": "1"}) as c:
    c.post("/register", data={"name": "T", "email": "cache@test.io",
                              "password": "secret123", "org_name": "Кэш"})
    db = SessionLocal()
    org = db.query(Org).first()
    oid = org.id
    analytics._compute_snapshot = fake_compute
    try:
        print("\n1. Обычный кэш")
        analytics.invalidate(oid)
        calls.clear()
        a = analytics.get_snapshot(db, org)
        b = analytics.get_snapshot(db, org)
        check("второй запрос из кэша", len(calls) == 1 and a is b, str(calls))

        print("\n2. Холодный старт: параллельные запросы считают один раз")
        analytics.invalidate(oid)
        calls.clear()
        delay["s"] = 0.3
        res = []

        def worker():
            s = SessionLocal()
            try:
                res.append(analytics.get_snapshot(s, s.get(Org, oid)))
            finally:
                s.close()

        ts = [threading.Thread(target=worker) for _ in range(5)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        delay["s"] = 0.0
        check("5 параллельных запросов, 1 расчёт", len(calls) == 1, str(calls))
        check("все получили один снапшот", len({id(r) for r in res}) == 1 and len(res) == 5)

        print("\n3. Истёк TTL, данные не менялись: старый сразу, свежий в фоне")
        calls.clear()
        old = analytics.get_snapshot(db, org)
        expire(oid)
        delay["s"] = 0.3
        t0 = time.time()
        got = analytics.get_snapshot(db, org)
        fast = time.time() - t0
        check("отдан прежний снапшот без ожидания", got is old and fast < 0.2, f"{fast:.2f}s")
        check("фоновый пересчёт завершился", wait_idle(oid))
        delay["s"] = 0.0
        fresh = analytics.get_snapshot(db, org)
        check("следующий запрос получает свежий", fresh is not old and len(calls) == 1, str(calls))

        print("\n4. После записи старый снапшот не отдаётся")
        calls.clear()
        before = analytics.get_snapshot(db, org)
        expire(oid)
        analytics.invalidate(oid)
        after = analytics.get_snapshot(db, org)
        check("invalidate: расчёт синхронный, новый снапшот", after is not before and len(calls) == 1)

        print("\n5. Запись во время расчёта: результат не кладётся в кэш")
        analytics.invalidate(oid)
        calls.clear()
        delay["s"] = 0.3
        out = {}
        t = threading.Thread(target=lambda: out.setdefault("s", worker()))
        t.start()
        time.sleep(0.1)
        analytics.invalidate(oid)
        t.join()
        delay["s"] = 0.0
        with analytics._cache_lock:
            cached = oid in analytics._cache
        check("снапшот по старым данным не сохранён", not cached)
        analytics.get_snapshot(db, org)
        check("следующий запрос пересчитал", len(calls) == 2, str(calls))

        print("\n6. Смена дня: старый снапшот не отдаётся")
        calls.clear()
        from datetime import date, timedelta
        with analytics._cache_lock:
            exp, snap, day = analytics._cache[oid]
            analytics._cache[oid] = (time.monotonic() - 1, snap, date.today() - timedelta(days=1))
        got = analytics.get_snapshot(db, org)
        check("вчерашний снапшот не отдан, расчёт синхронный", got is not snap and len(calls) == 1)
        with analytics._cache_lock:
            exp, snap, day = analytics._cache[oid]
            analytics._cache[oid] = (time.monotonic() + 600, snap, date.today() - timedelta(days=1))
        got = analytics.get_snapshot(db, org)
        check("вчерашний не отдаётся даже до истечения TTL", got is not snap and len(calls) == 2)

        print("\n7. Ошибка расчёта не вешает следующих")
        analytics.invalidate(oid)

        def boom(db_, org_):
            raise RuntimeError("boom")

        analytics._compute_snapshot = boom
        try:
            analytics.get_snapshot(db, org)
            raised = False
        except RuntimeError:
            raised = True
        check("ошибка дошла до вызывающего", raised)
        check("отметка расчёта снята", wait_idle(oid, 0.5))
        analytics._compute_snapshot = fake_compute
        calls.clear()
        analytics.get_snapshot(db, org)
        check("после ошибки считается заново", len(calls) == 1)

        print("\n8. Прогрев")
        analytics.invalidate(oid)
        calls.clear()
        check("warm считает переданные организации", analytics.warm([oid, 999999]) == 1 and calls == [oid])
    finally:
        analytics._compute_snapshot = real_compute
        db.close()

    print("\n9. Прогрев по настоящему расчёту и список организаций")
    c.post("/api/connect/demo")
    analytics.invalidate(oid)
    ids = scheduler._warm_org_ids()
    check("список прогрева выполняется", isinstance(ids, list), str(ids))
    check("warm с настоящим расчётом", analytics.warm([oid]) == 1)
    with analytics._cache_lock:
        check("снапшот в кэше после прогрева", oid in analytics._cache)
    r = c.get("/api/replenish")
    check("«Заказ» открывается из прогретого кэша", r.status_code == 200, str(r.status_code))

for suffix in ("", "-wal", "-shm"):
    p = Path(str(DB_PATH) + suffix)
    if p.exists():
        p.unlink()
print(f"\nИТОГО: {OK} OK, {FAIL} FAIL")
sys.exit(1 if FAIL else 0)
