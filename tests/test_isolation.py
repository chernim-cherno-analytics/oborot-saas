# -*- coding: utf-8 -*-
"""Тест изоляции организаций: данные одного клиента не должны утекать другому.

Зачем отдельный файл. В «Обороте» изоляция арендаторов держится не на механизме,
а на дисциплине: `org_id` подставляется в каждый запрос руками. Один забытый
фильтр — и клиент видит чужие остатки. Это единственный класс дефектов, который
убивает продукт целиком, поэтому проверяется не выборочно, а обходом:

  1) ПРЯМОЙ ДОСТУП ПО ИДЕНТИФИКАТОРУ — все маршруты, принимающие id в пути,
     вызываются сессией организации A с идентификаторами организации B.
     Допустимый ответ: 403 или 404. Ответ 200 — утечка; ответ 5xx — тоже дефект
     (значит, чужой объект долетел до кода и упал уже внутри).
  2) ЧУЖОЕ ИМЯ ПОЗИЦИИ — ручки, принимающие base_name, вызываются с именем
     товара, которого у организации нет.
  3) УТЕЧКА В ЧТЕНИИ — в организацию B кладётся позиция с уникальным именем;
     ни один читающий отчёт организации A не должен её содержать.
  4) ЧУЖОЕ ПРОИЗВОДСТВО В БРИФЕ (утечка условий подрядчика) — организация A
     сохраняет план заказа, указав production_id чужого канала, и оформляет по
     нему заказ. В заказе не должно остаться чужого идентификатора: иначе
     календарь платежей покажет сроки, доли себестоимости и предоплаты
     подрядчика чужой организации.
  5) СТОРОЖ CSRF — защита изменяющих запросов держится на том, что кастомный
     заголовок нельзя поставить кросс-доменно, а это верно ровно до тех пор,
     пока в приложении нет CORS с поддержкой учётных данных. Тест падает, если
     такой middleware появится: это молча отключает защиту.
  6) РОЛИ — owner-only ручки должны отклонять участника. Ручки, меняющие данные
     всей организации и при этом доступные участнику, тест не роняет, но
     печатает списком: это вопрос к владельцу продукта, а не дефект изоляции.

Запуск из корня репозитория:  python tests/test_isolation.py
"""
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "test_isolation.db"
APP_PORT = 8806

# Окружение — ДО импорта приложения (db.py читает DATABASE_URL при импорте).
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["SCHEDULER_ENABLED"] = "0"

if DB_PATH.exists():
    DB_PATH.unlink()

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from app.main import app as oborot_app  # noqa: E402


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


PASS, FAIL, NOTES = [], [], []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        PASS.append(name)
        print(f"  OK   {name}" + (f"  [{detail}]" if detail else ""))
    else:
        FAIL.append(name)
        print(f"  FAIL {name}  {detail}")


BASE = f"http://127.0.0.1:{APP_PORT}"


def client() -> httpx.Client:
    return httpx.Client(headers={"X-Oborot-CSRF": "1"}, base_url=BASE, timeout=120.0)


def sql(query: str, *args):
    con = sqlite3.connect(DB_PATH)
    try:
        return con.execute(query, args).fetchall()
    finally:
        con.close()


def exec_sql(query: str, *args) -> int:
    con = sqlite3.connect(DB_PATH)
    try:
        cur = con.execute(query, args)
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def register(c: httpx.Client, email: str, org_name: str, password: str = "secret123"):
    return c.post("/register", data={
        "name": email.split("@")[0], "email": email,
        "password": password, "org_name": org_name,
    })


def login(c: httpx.Client, email: str, password: str = "secret123"):
    return c.post("/login", data={"email": email, "password": password})


def add_member(org_id: int, email: str) -> int:
    """Сотрудник организации: приглашений в UI ещё нет, заводим строкой в БД."""
    import bcrypt
    pw = bcrypt.hashpw(b"secret123", bcrypt.gensalt()).decode()
    uid = exec_sql(
        "INSERT INTO users (email, pw_hash, name, created_at) VALUES (?,?,?,datetime('now'))",
        email, pw, email.split("@")[0])
    exec_sql("INSERT INTO memberships (user_id, org_id, role) VALUES (?,?,'member')",
             uid, org_id)
    return uid


# ─────────────────────────────────────────────────────────────────────────────

def setup_org(c: httpx.Client, email: str, org_name: str) -> int:
    """Регистрация + демо-данные. Возвращает org_id."""
    r = register(c, email, org_name)
    assert r.status_code in (200, 302, 303), (email, r.status_code)
    r = c.post("/api/connect/demo")
    assert r.status_code == 200, (email, r.status_code, r.text[:200])
    row = sql("SELECT id FROM orgs WHERE name=?", org_name)
    return row[0][0]


def make_production(c: httpx.Client, name: str, preset: str, moq: int,
                    stages: list | None = None) -> int:
    r = c.post("/api/productions", json={"name": name})
    assert r.status_code == 200, (name, r.status_code, r.text[:200])
    pid = r.json().get("id") or sql("SELECT id FROM productions WHERE name=?", name)[0][0]
    body: dict = {"moq_units": moq}
    if stages is not None:
        body["stages"] = stages
    else:
        body["preset"] = preset
    r = c.post(f"/api/productions/{pid}/setup", json=body)
    assert r.status_code == 200, (name, r.status_code, r.text[:200])
    return pid


def main() -> int:
    srv = ServerThread(oborot_app, APP_PORT)
    srv.start()
    try:
        run_all()
    finally:
        srv.stop()
    print(f"\nИТОГО: {len(PASS)} OK, {len(FAIL)} FAIL")
    for n in NOTES:
        print(f"  ~ {n}")
    return 1 if FAIL else 0


def run_all() -> None:
    a, b = client(), client()

    print("\n== Подготовка: две независимые организации с демо-данными ==")
    org_a = setup_org(a, "owner-a@test.io", "Организация A")
    org_b = setup_org(b, "owner-b@test.io", "Организация B")
    check("две организации созданы и наполнены демо-данными",
          org_a != org_b, f"A={org_a} B={org_b}")

    # Уникальная позиция организации B — маркер утечки в чтении.
    SECRET = "СЕКРЕТНАЯ МОДЕЛЬ B-777"
    exec_sql("INSERT INTO products (org_id, ext_id, base_name, size, category,"
             " sale_price, cost_price, cost_full, supplier, archived, excluded)"
             " VALUES (?,?,?,?,?,?,?,?,?,0,0)",
             org_b, "secret-ext-777", SECRET, "M", "Футболки", 5000.0, 1000.0, 1500.0, "")
    secret_pid = sql("SELECT id FROM products WHERE base_name=?", SECRET)[0][0]
    exec_sql("INSERT INTO stock_days (org_id, product_id, date, qty) VALUES (?,?,date('now'),?)",
             org_b, secret_pid, 10.0)
    exec_sql("INSERT INTO sales (org_id, product_id, date, qty, revenue, is_return)"
             " VALUES (?,?,date('now','-5 day'),?,?,0)", org_b, secret_pid, 3.0, 15000.0)

    # Производства с РАЗНЫМИ условиями: если чужие этапы утекут, это будет видно.
    prod_a = make_production(a, "Цех A", "turnkey", 10)
    prod_b = make_production(b, "Цех B", "fabric_sewing", 777, stages=[
        {"key": "secret", "name": "СЕКРЕТНЫЙ ЭТАП B", "lead_days": 123,
         "cost_share": 1.0, "prepay_share": 0.13, "min_units": 777},
    ])
    check("у каждой организации своё производство", prod_a != prod_b,
          f"A={prod_a} B={prod_b}")

    # Заказ в организации B — цель для прямых обращений из A.
    r = b.post("/api/orders", json={"name": "Заказ B", "eta_date": None, "items": [
        {"base_name": SECRET, "qty": 5, "sizes": {"M": 5}},
    ]})
    if r.status_code != 200:
        # у демо-набора свои имена; берём первую позицию каталога B
        base_b = sql("SELECT base_name FROM products WHERE org_id=? AND base_name!=? LIMIT 1",
                     org_b, SECRET)[0][0]
        r = b.post("/api/orders", json={"name": "Заказ B", "eta_date": None, "items": [
            {"base_name": base_b, "qty": 5, "sizes": {}},
        ]})
    check("в организации B есть заказ для проверок", r.status_code == 200,
          f"status={r.status_code} {r.text[:120]}")
    order_b = r.json().get("id") if r.status_code == 200 else None
    wh_b = sql("SELECT id FROM warehouses WHERE org_id=? LIMIT 1", org_b)
    wh_b = wh_b[0][0] if wh_b else None
    base_b = sql("SELECT base_name FROM products WHERE org_id=? LIMIT 1", org_b)[0][0]

    # ── 1. Прямой доступ по чужому идентификатору ────────────────────────────
    print("\n== 1. Чужой идентификатор в пути: ожидаем 403/404, никогда 200 и никогда 5xx ==")
    cases = [
        ("GET  /api/orders/{id}",            "GET",    f"/api/orders/{order_b}", None),
        ("POST /api/orders/{id}/status",     "POST",   f"/api/orders/{order_b}/status", {"status": "sent"}),
        ("DELETE /api/orders/{id}",          "DELETE", f"/api/orders/{order_b}", None),
        ("GET  /api/orders/{id}/ms-doc",     "GET",    f"/api/orders/{order_b}/ms-doc", None),
        ("POST /api/orders/{id}/push-to-ms", "POST",   f"/api/orders/{order_b}/push-to-ms", {}),
        ("POST /api/warehouses/{id}/toggle", "POST",   f"/api/warehouses/{wh_b}/toggle", {"active": False}),
        ("POST /api/productions/{id}",       "POST",   f"/api/productions/{prod_b}", {"name": "Захвачено"}),
        ("DELETE /api/productions/{id}",     "DELETE", f"/api/productions/{prod_b}", None),
        ("POST /api/productions/{id}/setup", "POST",   f"/api/productions/{prod_b}/setup", {"preset": "turnkey"}),
    ]
    for title, method, url, body in cases:
        if "None" in url:
            NOTES.append(f"{title}: не с чем проверять (нет объекта в B)")
            continue
        r = a.request(method, url, json=body) if body is not None else a.request(method, url)
        check(f"{title} из чужой организации отклонён",
              r.status_code in (403, 404), f"status={r.status_code} {r.text[:100]}")

    # Чужое производство нельзя назначить своей позиции.
    base_a = sql("SELECT base_name FROM products WHERE org_id=? LIMIT 1", org_a)[0][0]
    r = a.post("/api/productions/assign", json={"base_name": base_a, "production_id": prod_b})
    check("нельзя назначить свою позицию на чужое производство",
          r.status_code in (403, 404, 422), f"status={r.status_code} {r.text[:100]}")

    r = a.post("/api/productions/assign-rule",
               json={"assign_source": "supplier", "assign_map": {"Китай": prod_b}})
    check("правило распределения не принимает чужое производство",
          r.status_code in (403, 404, 422), f"status={r.status_code} {r.text[:100]}")

    # ── 2. Чужое имя позиции ─────────────────────────────────────────────────
    print("\n== 2. Чужое имя позиции: ожидаем 404 ==")
    name_cases = [
        ("POST /api/ordered",              "/api/ordered",              {"base_name": SECRET, "qty": 5}),
        ("POST /api/exclusions",           "/api/exclusions",           {"base_name": SECRET, "excluded": True}),
        ("POST /api/hidden",               "/api/hidden",               {"base_name": SECRET, "hidden": True}),
        ("POST /api/categories/override",  "/api/categories/override",  {"base_name": SECRET, "category": "Прочее"}),
        ("POST /api/discount-overrides",   "/api/discount-overrides",   {"base_name": SECRET, "discount": 50}),
        ("POST /api/replenish-draft",      "/api/replenish-draft",      {"base_name": SECRET, "sizes": {"M": 3}}),
    ]
    for title, url, body in name_cases:
        r = a.post(url, json=body)
        check(f"{title} с чужим именем позиции отклонён",
              r.status_code in (403, 404, 422), f"status={r.status_code} {r.text[:100]}")
        leaked = sql("SELECT COUNT(*) FROM products WHERE org_id=? AND base_name=?",
                     org_a, SECRET)[0][0]
        check(f"{title} не создал чужую позицию у себя", leaked == 0, f"rows={leaked}")

    # ── 3. Утечка в чтении ───────────────────────────────────────────────────
    print("\n== 3. Читающие отчёты организации A не содержат позицию организации B ==")
    for url in ("/api/turnover", "/api/replenish", "/api/active-stock", "/api/revenue",
                "/api/budget?budget=100000", "/api/summary", "/api/forecast",
                "/api/stocks", "/api/sizes/products", "/api/exclusions"):
        r = a.get(url)
        body = r.text if r.status_code == 200 else ""
        check(f"{url} не содержит чужую позицию",
              r.status_code != 200 or SECRET not in body,
              f"status={r.status_code}")

    # ── 4. Чужое производство в брифе плана заказа ───────────────────────────
    print("\n== 4. Чужое производство в брифе: условия подрядчика не должны утечь ==")
    r = a.post("/api/order-plan", json={
        "production_id": prod_b, "budget": 300000, "budget_scope": "now",
        "cadence_days": 30, "safety_days": 14, "strategy": "balance",
    })
    check("план с чужим производством сохраняется без 5xx",
          r.status_code < 500, f"status={r.status_code} {r.text[:120]}")
    if r.status_code == 200:
        data = r.json()
        plan_id = data.get("plan_id") or data.get("id")
        prod_out = (data.get("production") or {}).get("id") if isinstance(data.get("production"), dict) else data.get("production")
        check("в ответе плана нет идентификатора чужого производства",
              prod_out != prod_b, f"production={prod_out}")
        stages_txt = str(data.get("computed", {}).get("stages", "")) + str(data.get("stages", ""))
        check("в ответе плана нет чужих этапов",
              "СЕКРЕТНЫЙ ЭТАП B" not in r.text,
              f"stages={stages_txt[:80]}")
        if plan_id:
            saved = sql("SELECT brief_json FROM order_plans WHERE id=?", plan_id)
            brief = saved[0][0] if saved else ""
            check("в сохранённом брифе нет чужого производства",
                  f'"production_id": {prod_b}' not in brief and f'"production_id":{prod_b}' not in brief,
                  f"brief={brief[:120]}")
            r2 = a.post(f"/api/order-plan/{plan_id}/apply",
                        json={"name": "Из плана A", "force": True, "confirm_partial": True})
            if r2.status_code == 200:
                oid = r2.json().get("id")
                got = sql("SELECT production_id FROM production_orders WHERE id=?", oid)
                got = got[0][0] if got else None
                check("в созданном заказе нет чужого производства", got != prod_b,
                      f"production_id={got}")
                r3 = a.get("/api/orders/open")
                check("сводка открытых заказов не содержит чужих этапов",
                      r3.status_code != 200 or "СЕКРЕТНЫЙ ЭТАП B" not in r3.text,
                      f"status={r3.status_code}")
                r4 = a.get("/api/cash-calendar")
                check("календарь денег не содержит чужих этапов",
                      r4.status_code != 200 or "СЕКРЕТНЫЙ ЭТАП B" not in r4.text,
                      f"status={r4.status_code}")
            else:
                NOTES.append(f"apply вернул {r2.status_code} — заказ по плану не создан, "
                             f"проверка утечки в заказе пропущена")

    # Чужой план заказа нельзя применить.
    rb = b.post("/api/order-plan", json={"production_id": prod_b, "budget": 100000})
    if rb.status_code == 200:
        pid_b = rb.json().get("plan_id") or rb.json().get("id")
        if pid_b:
            r = a.post(f"/api/order-plan/{pid_b}/apply", json={"name": "Чужой план"})
            check("нельзя оформить заказ по чужому плану",
                  r.status_code in (403, 404), f"status={r.status_code} {r.text[:100]}")

    # ── 5. Сторож CSRF ───────────────────────────────────────────────────────
    print("\n== 5. Сторож: защита изменяющих запросов держится на отсутствии CORS ==")
    cors = [m for m in getattr(oborot_app, "user_middleware", [])
            if "cors" in str(getattr(m, "cls", m)).lower()]
    check("в приложении нет CORS-middleware (иначе проверка заголовка перестаёт защищать)",
          not cors, f"middleware={cors}")
    nc = httpx.Client(base_url=BASE, timeout=30.0)
    nc.cookies.update(a.cookies)
    r = nc.post("/api/ordered", json={"base_name": base_a, "qty": 1})
    check("изменяющий запрос без заголовка X-Oborot-CSRF отклонён",
          r.status_code == 403, f"status={r.status_code}")
    nc.close()

    # ── 6. Роли ──────────────────────────────────────────────────────────────
    print("\n== 6. Роли: owner-only ручки отклоняют участника ==")
    add_member(org_a, "member-a@test.io")
    m = client()
    login(m, "member-a@test.io")
    owner_only = [
        ("POST /api/settings",           "/api/settings",           {"horizon_days": 120}),
        ("POST /api/exclusions",         "/api/exclusions",         {"base_name": base_a, "excluded": True}),
        ("POST /api/productions",        "/api/productions",        {"name": "Цех участника"}),
        (f"POST /api/productions/{prod_a}/setup", f"/api/productions/{prod_a}/setup", {"preset": "turnkey"}),
        ("POST /api/discount-rule",      "/api/discount-rule",      {"new_pct": 10}),
        ("POST /api/plans/request",      "/api/plans/request",      {"plan": "brand", "period": "month",
                                                                     "company": "ООО Ромашка", "inn": "1234567890",
                                                                     "email": "x@y.ru"}),
    ]
    for title, url, body in owner_only:
        r = m.post(url, json=body)
        check(f"{title} недоступна участнику", r.status_code == 403,
              f"status={r.status_code}")

    # Информационно: ручки, меняющие данные всей организации и открытые участнику.
    org_wide_for_member = []
    probes = [
        ("POST /api/hidden",              "/api/hidden",              {"base_name": base_a, "hidden": True}),
        ("POST /api/categories/override", "/api/categories/override", {"base_name": base_a, "category": "Прочее"}),
        ("POST /api/replenish-draft",     "/api/replenish-draft",     {"base_name": base_a, "sizes": {"M": 1}}),
        ("POST /api/ordered",             "/api/ordered",             {"base_name": base_a, "qty": 1}),
    ]
    for title, url, body in probes:
        r = m.post(url, json=body)
        if r.status_code == 200:
            org_wide_for_member.append(title)
    if org_wide_for_member:
        NOTES.append("участник меняет данные всей организации через: "
                     + ", ".join(org_wide_for_member)
                     + " — это вопрос к владельцу продукта, не дефект изоляции")
    m.close()
    a.close()
    b.close()


if __name__ == "__main__":
    sys.exit(main())
