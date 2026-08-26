# -*- coding: utf-8 -*-
"""Регрессия DATA-8 (третий сценарий): сбой справочника контрагентов не должен
затирать уже проверенных поставщиков товаров.

Дефект, который закрывает этот тест. `_supplier_of()` резолвит имя поставщика
товара по ссылке на контрагента через кэш `_SUPPLIERS[org_id]`, который
заполняется из `fetch_counterparties()` в начале каждого синка. Раньше
исключение при этом чтении (сеть, 5xx, лимит) молча ловилось, кэш подменялся
на `{}`, и `_upsert_products()` БЕЗ УСЛОВИЙ писал `row.supplier = "" ` для
КАЖДОГО товара организации — то есть один транзиентный сбой одного
справочника стирал результат всех прошлых успешных синков сразу, а сам синк
при этом завершался статусом «готово», как будто ничего не случилось. Именно
это и написано в TECH_DEBT DATA-8: «правило распределения по производствам
перестаёт работать» — оно читает как раз `products.supplier`
(`app/assign_rules.py`, `source == "supplier"`).

Правильное поведение: различать «справочник не прочитался» (ничего не узнали
— оставить как было) и «справочник прочитался и авторитетно не содержит этого
поставщика» (МойСклад ответил — значит, поставщика действительно сняли или
не назначали, писать пустую строку можно как и раньше).

Сценарий на ОДНОЙ организации (A):
  1) первичный синк с привязкой «p-hoodie1 → Поставщик А» — поставщик
     проставился (валидное соответствие применяется);
  2) справочник контрагентов отвечает 500 до исчерпания ретраев — синк всё
     равно доходит до done (исключение гасится), но поставщик НЕ стирается;
  3) справочник снова отвечает 200, но привязка снята (у товара её больше
     нет и MоySklad сейчас ничего не находит) — поставщик обнуляется, как
     и раньше: это авторитетный ответ, а не сбой;
  4) привязка на другого поставщика — следующий синк подтягивает новое имя
     (восстановление после сбоя сходится к текущей истине).

Плюс организация B, синкающаяся в тот же процесс с тем же mock (свой токен
общий, но org_id и её products — свои): её поставщик не должен шевельнуться
ни от сбоя, ни от синков организации A (изоляция).

Свой мок на отдельном порту (9814), чтобы файл можно было запускать, пока
идут другие наборы.

Запуск из корня репозитория:  python tests/test_sync_supplier.py
"""
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

DB_PATH = ROOT / "test_sync_supplier.db"
# Порты берутся из окружения: так tests/run_all.py разводит наборы и
# может гонять их параллельно. Значения по умолчанию — свои, не заняты
# другими файлами (см. комментарии портов в tests/run_all.py).
APP_PORT = int(os.environ.get("OBOROT_TEST_PORT", "8816"))
MOCK_PORT = int(os.environ.get("OBOROT_MOCK_PORT", "9814"))

os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["MS_BASE_URL"] = f"http://127.0.0.1:{MOCK_PORT}"
os.environ["SCHEDULER_ENABLED"] = "0"
os.environ["HISTORY_DAYS"] = "60"
os.environ["INITIAL_WINDOW_DAYS"] = "10"
os.environ["STOCK_CHUNK_DATES"] = "10"
os.environ["MS_CHUNK_PAUSE"] = "0"
# Мало ретраев — иначе исчерпание 500-й серии тянет тест на десятки секунд
# (экспоненциальный бэкофф между попытками), не давая новой информации.
os.environ["MS_MAX_RETRIES"] = "2"

if DB_PATH.exists():
    DB_PATH.unlink()

import httpx  # noqa: E402
import uvicorn  # noqa: E402

import mock_ms  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.main import app as oborot_app  # noqa: E402
from app.models import Org, Product  # noqa: E402
from sqlalchemy import select  # noqa: E402

mock_ms.PORT = MOCK_PORT
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


def wait_sync_done(client: httpx.Client, timeout: float = 240.0) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        last = client.get("/api/sync/status").json()
        if last.get("state") in ("done", "error"):
            return last
        time.sleep(1.0)
    return last


def supplier_of(org_name: str, ext_id: str) -> str | None:
    """products.supplier по ext_id родительского товара, None — товара нет."""
    db = SessionLocal()
    try:
        org = db.execute(select(Org).where(Org.name == org_name)).scalar_one_or_none()
        if org is None:
            return None
        row = db.execute(
            select(Product).where(Product.org_id == org.id, Product.ext_id == ext_id)
        ).scalar_one_or_none()
        return None if row is None else row.supplier
    finally:
        db.close()


def register_and_connect(c: httpx.Client, email: str, org_name: str) -> None:
    r = c.post("/register", data={"name": "Владелец", "email": email,
                                  "password": "secret123", "org_name": org_name})
    check(f"регистрация {org_name}", r.status_code in (200, 302, 303),
          f"status={r.status_code}")
    r = c.post("/api/connect/moysklad", json={"token": mock_ms.TOKEN})
    check(f"токен принят {org_name}", r.status_code == 200, f"status={r.status_code}")
    c.post("/api/connect/moysklad/stores", json={"ext_ids": ["st-flag", "st-web"]})


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
        for p in (DB_PATH,):
            if p.exists():
                p.unlink()


def run() -> int:
    base = f"http://127.0.0.1:{APP_PORT}"
    a = httpx.Client(headers={"X-Oborot-CSRF": "1"}, base_url=base, timeout=120.0)
    b = httpx.Client(headers={"X-Oborot-CSRF": "1"}, base_url=base, timeout=120.0)
    mock_api = httpx.Client(base_url=f"http://127.0.0.1:{MOCK_PORT}", timeout=30.0)

    P1, P2 = "p-hoodie1", "p-hoodie2"  # родительские product двух разных моделей

    print("\n== Подготовка: обе организации, валидная привязка поставщика ==")
    mock_api.post("/__test/supplier_links", json={P1: "Поставщик А", P2: "Поставщик В"})
    register_and_connect(a, "owner-a@test.io", "Организация A")
    register_and_connect(b, "owner-b@test.io", "Организация B")

    r = a.post("/api/sync/initial")
    check("первичный синк A запущен", r.status_code == 200, f"status={r.status_code}")
    st = wait_sync_done(a)
    check("первичный синк A дошёл до done", st.get("state") == "done",
          f"state={st.get('state')} error={str(st.get('error'))[:150]}")

    r = b.post("/api/sync/initial")
    check("первичный синк B запущен", r.status_code == 200, f"status={r.status_code}")
    st = wait_sync_done(b)
    check("первичный синк B дошёл до done", st.get("state") == "done",
          f"state={st.get('state')} error={str(st.get('error'))[:150]}")

    sup_a0 = supplier_of("Организация A", P1)
    sup_b0 = supplier_of("Организация B", P2)
    check("успешное валидное соответствие применилось (A)", sup_a0 == "Поставщик А",
          f"supplier={sup_a0!r}")
    check("успешное валидное соответствие применилось (B)", sup_b0 == "Поставщик В",
          f"supplier={sup_b0!r}")

    print("\n== Справочник контрагентов падает: A синкает, B — нет (изоляция) ==")
    mock_api.post("/__test/faults", json={"cp_list_500_burst": 1000})
    r = a.post("/api/sync/run")
    check("инкремент A (сбой справочника) запущен", r.status_code == 200,
          f"status={r.status_code}")
    st = wait_sync_done(a)
    check("инкремент A всё равно доходит до done (сбой справочника не роняет синк)",
          st.get("state") == "done",
          f"state={st.get('state')} error={str(st.get('error'))[:150]}")
    stats = st.get("stats") or {}
    check("честный сигнал сбоя справочника записан в stats",
          bool(stats.get("suppliers_error")), f"suppliers_error={stats.get('suppliers_error')!r}")

    sup_a1 = supplier_of("Организация A", P1)
    check("СУЩЕСТВУЮЩЕЕ СООТВЕТСТВИЕ ПЕРЕЖИЛО СБОЙ СПРАВОЧНИКА (A)",
          sup_a1 == "Поставщик А", f"было={sup_a0!r} стало={sup_a1!r}")

    sup_b1 = supplier_of("Организация B", P2)
    check("ИЗОЛЯЦИЯ: сбой справочника у A не задел уже сохранённого поставщика B",
          sup_b1 == sup_b0, f"было={sup_b0!r} стало={sup_b1!r}")

    print("\n== Справочник снова отвечает, привязка снята: авторитетный пустой ответ ==")
    mock_api.post("/__test/faults", json={})  # сбросить сбой
    mock_api.post("/__test/supplier_links", json={P1: ""})  # снять привязку у A
    r = a.post("/api/sync/run")
    check("инкремент A (привязка снята) запущен", r.status_code == 200,
          f"status={r.status_code}")
    st = wait_sync_done(a)
    check("инкремент A дошёл до done", st.get("state") == "done",
          f"state={st.get('state')} error={str(st.get('error'))[:150]}")
    stats = st.get("stats") or {}
    check("сигнала сбоя справочника больше нет — фетч был успешным",
          "suppliers_error" not in stats, f"stats keys={sorted(stats.keys())}")

    sup_a2 = supplier_of("Организация A", P1)
    check("УСПЕШНЫЙ АВТОРИТЕТНЫЙ ПУСТОЙ ОТВЕТ ОБНУЛЯЕТ ПОСТАВЩИКА, КАК И РАНЬШЕ",
          sup_a2 == "", f"было={sup_a1!r} стало={sup_a2!r}")

    print("\n== Восстановление: новая валидная привязка сходится следующим синком ==")
    mock_api.post("/__test/supplier_links", json={P1: "Поставщик Б"})
    r = a.post("/api/sync/run")
    check("инкремент A (новая привязка) запущен", r.status_code == 200,
          f"status={r.status_code}")
    st = wait_sync_done(a)
    check("инкремент A дошёл до done", st.get("state") == "done",
          f"state={st.get('state')} error={str(st.get('error'))[:150]}")

    sup_a3 = supplier_of("Организация A", P1)
    check("ПОВТОР ПОСЛЕ ВОССТАНОВЛЕНИЯ СХОДИТСЯ К ТЕКУЩЕЙ ИСТИНЕ",
          sup_a3 == "Поставщик Б", f"было={sup_a2!r} стало={sup_a3!r}")

    print("\n== B продолжает работать штатно (не задета всей чехардой A) ==")
    r = b.post("/api/sync/run")
    check("инкремент B запущен", r.status_code == 200, f"status={r.status_code}")
    st = wait_sync_done(b)
    check("инкремент B дошёл до done", st.get("state") == "done",
          f"state={st.get('state')} error={str(st.get('error'))[:150]}")
    sup_b2 = supplier_of("Организация B", P2)
    check("поставщик B по-прежнему верный", sup_b2 == "Поставщик В", f"supplier={sup_b2!r}")

    a.close()
    b.close()
    mock_api.close()
    print(f"\nИТОГО: {len(PASS)} OK, {len(FAIL)} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
