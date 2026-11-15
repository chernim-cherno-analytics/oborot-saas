# -*- coding: utf-8 -*-
"""Роли и права сотрудников (решение владельца 15.11.2026).

Что доказывается:
  1) встроенные роли «Владелец», «Сотрудник», «Наблюдатель» создаются лениво,
     один раз, с правами из кода;
  2) старый участник (role="member", role_id пустой) получает права
     «Сотрудника», то есть ровно прежние права участника (test_member_rights);
     аддитивная миграция на базе старой схемы добавляет колонку и таблицы;
  3) сторож: каждая пишущая ручка /api под сессией закрыта правом, кроме
     явного списка личных ручек;
  4) «Наблюдатель» получает 403 на каждой ручке с правом и 200 на просмотре
     и выгрузках;
  5) своя роль с одним правом даёт ровно его: на всех ручках других прав 403;
  6) последнего владельца снять и убрать нельзя; роль «Владелец» раздаёт
     только владелец; не-владелец с users_manage не выдаёт чужих прав;
  7) изоляция организаций на всех ручках /api/team (404 без данных);
  8) приглашение: создание, вход по ссылке (регистрация, вход, уже вошёл),
     повтор, просрочка и отзыв отклоняются, токен в базе не хранится;
  9) страница /team открывается владельцу и не открывается без users_manage;
 10) интерфейс получает права: data-perms на body.

Синтетическая база, демо-данные. Запуск: TZ=UTC python tests/test_roles.py
"""
import hashlib
import os
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "test_roles.db"
OLD_DB_PATH = ROOT / "test_roles_old.db"
for base in (DB_PATH, OLD_DB_PATH):
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(base) + suffix)
        if p.exists():
            p.unlink()
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["SCHEDULER_ENABLED"] = "0"

from fastapi.routing import APIRoute  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import auth, roles  # noqa: E402
from app.main import app  # noqa: E402

OK = FAIL = 0


def check(name, cond, detail=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  OK   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


def sql(q, *args):
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute(q, args)
        con.commit()
        return cur.fetchall()


def client():
    return TestClient(app, headers={"X-Oborot-CSRF": "1"})


PW = "secret123"


def add_user(email, org_id, role="member", role_id=None, name="Сотрудник"):
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute(
            "INSERT INTO users (email, pw_hash, name, created_at, session_version) "
            "VALUES (?,?,?,datetime('now'),1)", (email, auth.hash_password(PW), name))
        uid = cur.lastrowid
        con.execute("INSERT INTO memberships (user_id, org_id, role, role_id) VALUES (?,?,?,?)",
                    (uid, org_id, role, role_id))
        con.commit()
    return uid


def login(c, email):
    r = c.post("/login", data={"email": email, "password": PW}, follow_redirects=False)
    assert r.status_code == 303, (email, r.status_code, r.text[:200])
    return c


def perms_of(c):
    j = c.get("/api/settings").json()
    return set(j.get("perms") or []), j.get("role_name")


# ── Сторож: какое право у какой ручки ─────────────────────────────────────────

def _calls(dep):
    for sub in dep.dependencies:
        yield sub.call
        yield from _calls(sub)


def route_perm(route):
    for call in _calls(route.dependant):
        perm = getattr(call, "oborot_perm", None)
        if perm:
            return perm
    return None


def route_needs_auth(route):
    return any(call is auth.require_auth_api for call in _calls(route.dependant))


MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
# Личные ручки и выгрузки: права не требуют (любая роль).
PERSONAL = {
    "/api/hints/seen", "/api/lessons/{key}/done", "/api/lessons/reset",
    "/api/lessons/{key}/reset", "/api/prefs/hints", "/api/account/password",
    "/api/account/delete", "/api/export/replenish.xlsx", "/api/account/switch-org",
}

def all_routes(node=None) -> list:
    """Все маршруты, включая вложенные роутеры (см. tests/test_subscription.py:
    include_router в этой версии FastAPI оставляет обёртку _IncludedRouter,
    настоящие маршруты лежат в original_router)."""
    node = app if node is None else node
    out = []
    for route in getattr(node, "routes", []) or []:
        if isinstance(route, APIRoute):
            out.append(route)
        nested = getattr(route, "original_router", None) or route
        if nested is not route or hasattr(nested, "routes"):
            if getattr(nested, "routes", None):
                out.extend(all_routes(nested))
    return out


PERM_ROUTES = []  # (method, path, perm)
UNGUARDED = []
for r in all_routes():
    if not r.path.startswith("/api/"):
        continue
    for m in sorted(r.methods & MUTATING):
        perm = route_perm(r)
        if perm:
            PERM_ROUTES.append((m, r.path, perm))
        elif route_needs_auth(r) and r.path not in PERSONAL:
            UNGUARDED.append((m, r.path))
    # GET-ручки с правом (настройки МС, статус синка, раздел «Сотрудники»).
    if "GET" in r.methods and route_perm(r):
        PERM_ROUTES.append(("GET", r.path, route_perm(r)))


def concrete(path):
    return re.sub(r"\{[^}]+\}", "1", path)


def call(c, method, path, body=None):
    if method in ("GET", "DELETE"):
        return c.request(method, concrete(path))
    return c.request(method, concrete(path), json=body if body is not None else {})


with client() as owner, client() as ownerB:
    owner.post("/register", data={"name": "Владелец", "email": "own@roles.io",
                                  "password": PW, "org_name": "Роли"})
    owner.post("/api/connect/demo")
    org_id = sql("SELECT id FROM orgs WHERE name='Роли'")[0][0]
    owner_uid = sql("SELECT id FROM users WHERE email='own@roles.io'")[0][0]
    ownerB.post("/register", data={"name": "Чужой", "email": "own@b.io",
                                   "password": PW, "org_name": "Чужая"})
    org_b = sql("SELECT id FROM orgs WHERE name='Чужая'")[0][0]

    print("\n0. Сторож: пишущие ручки закрыты правами")
    check("ни одной пишущей ручки /api под сессией без права (кроме личных)",
          not UNGUARDED, str(UNGUARDED))
    check("ручек с правами больше пятидесяти", len(PERM_ROUTES) > 50, str(len(PERM_ROUTES)))
    used = {p for _m, _p, p in PERM_ROUTES}
    # drafts_reset_all проверяется внутри ручки сброса ростовки (право зависит
    # от тела запроса: пустой base_name = вся таблица), см. раздел 4.
    check("каждое право закрывает хотя бы одну ручку",
          used | {"drafts_reset_all"} == set(roles.PERM_KEYS), str(set(roles.PERM_KEYS) - used))
    expect = {
        ("DELETE", "/api/orders/{order_id}"): "orders_delete",
        ("POST", "/api/ordered"): "ordered_edit",
        ("POST", "/api/ordered/add"): "ordered_edit",
        ("POST", "/api/orders"): "orders_create",
        ("POST", "/api/order-plan/{plan_id}/apply"): "orders_create",
        ("POST", "/api/orders/{order_id}/status"): "orders_manage",
        ("POST", "/api/orders/{order_id}/receipts"): "orders_manage",
        ("POST", "/api/order-plan"): "plan",
        ("POST", "/api/order-plan/preview"): "plan",
        ("POST", "/api/orders/{order_id}/push-to-ms"): "orders_push_ms",
        ("POST", "/api/settings"): "settings",
        ("POST", "/api/sync/run"): "settings",
        ("GET", "/api/sync/status"): "settings",
        ("POST", "/api/productions"): "settings",
        ("POST", "/api/hidden"): "catalog_edit",
        ("POST", "/api/categories/merge"): "catalog_edit",
        ("POST", "/api/exclusions"): "catalog_edit",
        ("POST", "/api/discount-rule"): "catalog_edit",
        ("POST", "/api/replenish-draft"): "drafts_edit",
        ("POST", "/api/replenish-draft/reset"): "drafts_edit",
        ("POST", "/api/supply/sheets/refresh"): "supply_edit",
        ("POST", "/api/supply/planning/materials"): "supply_edit",
        ("POST", "/api/plans/request"): "billing",
        ("POST", "/api/team/roles"): "users_manage",
        ("GET", "/api/team"): "users_manage",
    }
    have = {(m, p): perm for m, p, perm in PERM_ROUTES}
    for key, perm in expect.items():
        check(f"{key[0]} {key[1]} под правом {perm}", have.get(key) == perm, str(have.get(key)))

    print("\n1. Встроенные роли")
    t = owner.get("/api/team")
    check("владелец открывает /api/team", t.status_code == 200, t.text[:200])
    team = t.json()
    by_code = {r["builtin"]: r for r in team["roles"]}
    check("три встроенные роли с русскими названиями",
          {c: by_code.get(c, {}).get("name") for c in ("owner", "employee", "viewer")}
          == {"owner": "Владелец", "employee": "Сотрудник", "viewer": "Наблюдатель"}, str(by_code))
    check("«Владелец»: все права", set(by_code["owner"]["perms"]) == set(roles.PERM_KEYS))
    check("«Сотрудник»: права прежнего участника",
          set(by_code["employee"]["perms"]) == {"plan", "orders_create", "orders_manage",
                                               "drafts_edit", "drafts_reset_all"},
          str(by_code["employee"]["perms"]))
    check("«Наблюдатель»: без прав", by_code["viewer"]["perms"] == [])
    check("встроенные не редактируются", not any(r["editable"] for r in by_code.values()))
    owner.get("/api/team")
    check("повторное обращение не создаёт дублей",
          sql("SELECT COUNT(*) FROM org_roles WHERE org_id=?", org_id)[0][0] == 3)
    me = next(m for m in team["members"] if m["is_self"])
    check("владелец в списке с ролью «Владелец»",
          me["role_name"] == "Владелец" and me["role_id"] == by_code["owner"]["id"], str(me))
    perms, rname = perms_of(owner)
    check("права владельца в /api/settings: все", perms == set(roles.PERM_KEYS) and rname == "Владелец")
    owner_id, emp_id, view_id = (by_code[c]["id"] for c in ("owner", "employee", "viewer"))
    r = owner.post(f"/api/team/roles/{emp_id}", json={"perms": ["settings"]})
    check("встроенную роль изменить нельзя (422)", r.status_code == 422, r.text[:200])
    r = owner.post(f"/api/team/roles/{owner_id}/delete")
    check("встроенную роль удалить нельзя (422)", r.status_code == 422, r.text[:200])

    print("\n2. Старый участник = «Сотрудник»")
    old_uid = add_user("old@roles.io", org_id)  # role_id пустой, как до ролей
    with client() as old:
        login(old, "old@roles.io")
        perms, rname = perms_of(old)
        check("права старого участника = «Сотрудник»",
              perms == set(roles.EMPLOYEE_PERMS) and rname == "Сотрудник", f"{perms} {rname}")
        listed = next(m for m in owner.get("/api/team").json()["members"] if m["user_id"] == old_uid)
        check("в списке сотрудников он «Сотрудник»",
              listed["role_id"] == emp_id and listed["role_name"] == "Сотрудник", str(listed))
        rep = owner.get("/api/replenish").json()
        item = next(i for i in rep["items"] if i.get("sizes"))
        base = item["base_name"]
        size = list(item["sizes"])[0]
        created = old.post("/api/orders", json={"name": "Заказ старого", "items": [
            {"base_name": base, "qty": 2, "sizes": {size: 2}}]})
        check("создаёт заказ", created.status_code == 200, created.text[:200])
        order_id = created.json().get("id")
        st = old.post(f"/api/orders/{order_id}/status", json={"status": "sent"})
        check("переводит заказ в производство", st.status_code == 200, st.text[:200])
        pv = old.post("/api/order-plan/preview", json={})
        check("считает план (не 403)", pv.status_code != 403, f"{pv.status_code} {pv.text[:120]}")
        d = old.post("/api/replenish-draft", json={"base_name": base, "sizes": {size: 3}})
        check("правит черновик ростовки", d.status_code == 200, d.text[:200])
        d = old.post("/api/replenish-draft/reset", json={"base_name": ""})
        check("сбрасывает всю ростовку (как раньше)", d.status_code == 200, d.text[:200])
        for path in ("/api/turnover", "/api/replenish", "/api/budget", "/api/export/turnover.xlsx"):
            check(f"читает {path}", old.get(path).status_code == 200)
        for method, path, body in (
            ("DELETE", f"/api/orders/{order_id}", None),
            ("POST", "/api/ordered", {"base_name": base, "qty": 9}),
            ("POST", "/api/hidden", {"base_name": base, "hidden": True}),
            ("POST", "/api/settings", {}),
            ("POST", "/api/plans/request", {"plan": "start"}),
            ("GET", "/api/team", None),
        ):
            r = old.request(method, path, json=body) if body is not None else old.request(method, path)
            check(f"{method} {path}: 403 с названием права",
                  r.status_code == 403 and "Недостаточно прав:" in r.json().get("detail", ""),
                  f"{r.status_code} {r.text[:120]}")

    print("\n2а. Миграция на базе старой схемы")
    from sqlalchemy import create_engine, inspect as sa_inspect
    from app.models import ensure_roles_schema
    with sqlite3.connect(OLD_DB_PATH) as con:
        con.execute("CREATE TABLE orgs (id INTEGER PRIMARY KEY, name TEXT)")
        con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT)")
        con.execute("CREATE TABLE memberships (user_id INTEGER, org_id INTEGER, role VARCHAR(16), "
                    "PRIMARY KEY (user_id, org_id))")
        con.execute("INSERT INTO memberships VALUES (1, 1, 'owner'), (2, 1, 'member')")
        con.commit()
    eng = create_engine(f"sqlite:///{OLD_DB_PATH}")
    ensure_roles_schema(bind=eng)
    ensure_roles_schema(bind=eng)  # идемпотентно
    insp = sa_inspect(eng)
    cols = {c["name"] for c in insp.get_columns("memberships")}
    check("колонка memberships.role_id добавлена", "role_id" in cols, str(cols))
    check("таблицы ролей и приглашений созданы",
          insp.has_table("org_roles") and insp.has_table("org_invites"))
    with sqlite3.connect(OLD_DB_PATH) as con:
        rows = con.execute("SELECT user_id, role, role_id FROM memberships ORDER BY user_id").fetchall()
    check("старые строки не переписаны: role прежний, role_id пустой",
          rows == [(1, "owner", None), (2, "member", None)], str(rows))
    eng.dispose()

    print("\n3. «Наблюдатель»: 403 на каждой ручке с правом, 200 на просмотре")
    view_uid = add_user("viewer@roles.io", org_id)
    r = owner.post(f"/api/team/members/{view_uid}/role", json={"role_id": view_id})
    check("владелец назначает «Наблюдателя»", r.status_code == 200, r.text[:200])
    with client() as viewer:
        login(viewer, "viewer@roles.io")
        perms, rname = perms_of(viewer)
        check("у наблюдателя нет прав", perms == set() and rname == "Наблюдатель", f"{perms} {rname}")
        mem = viewer.get("/api/settings").json()["members"]
        check("наблюдатель не видит e-mail коллег в «Настройках» (QA 15.11)",
              all(m["email"] is None or m["email"] == "viewer@roles.io" or m["role"] == "owner" for m in mem)
              and any(m["email"] is None for m in mem)
              and any(m["email"] == "viewer@roles.io" for m in mem), str(mem)[:300])
        oth = viewer.get("/api/account").json()["others"]
        check("и в «Аккаунте» (кроме владельца)", oth and all((o["email"] is None) != (o["role"] == "owner") for o in oth),
              str(oth)[:300])
        omem = owner.get("/api/settings").json()["members"]
        check("владелец e-mail сотрудников видит", all(m["email"] for m in omem), str(omem)[:300])
        bad = []
        for method, path, perm in PERM_ROUTES:
            rr = call(viewer, method, path)
            if rr.status_code != 403:
                bad.append((method, path, rr.status_code))
        check(f"403 на всех {len(PERM_ROUTES)} ручках с правом", not bad, str(bad[:10]))
        for path in ("/api/turnover", "/api/replenish", "/api/stocks", "/api/budget",
                     "/api/orders", "/api/settings", "/api/forecast", "/api/pulse",
                     "/api/export/turnover.xlsx", "/api/export/replenish.xlsx",
                     "/api/export/budget.xlsx", "/api/supply/planning",
                     "/api/supply/planning/export.xlsx", "/api/productions"):
            st = viewer.get(path).status_code
            check(f"просмотр {path}: 200", st == 200 and owner.get(path).status_code == 200,
                  f"{path} {st}")
        rr = viewer.post("/api/export/replenish.xlsx", json={"base_names": [base]})
        check("выгрузка выбранных позиций (POST xlsx): не 403", rr.status_code != 403, str(rr.status_code))
        for path in ("/turnover", "/stocks", "/replenish", "/sizes", "/settings", "/account"):
            h = viewer.get(path)
            check(f"страница {path} открывается", h.status_code == 200, str(h.status_code))
        h = viewer.get("/replenish").text
        check("на странице data-perms пустой и правило скрытия на месте",
              'data-perms=""' in h and 'body:not([data-perms~="orders_delete"]) .ord-del' in h)
        hs = viewer.get("/settings").text
        check("в настройках кнопка «Пригласить» помечена правом users_manage",
              'need-users_manage' in hs and 'body:not([data-perms~="users_manage"]) .need-users_manage' in hs)
        check("в меню нет ссылки на /team", 'class="up-item" href="/team"' not in hs)
        r = viewer.get("/team", follow_redirects=False)
        check("/team без users_manage: редирект на /account",
              r.status_code == 302 and r.headers["location"] == "/account", str(r.status_code))
    ho = owner.get("/settings").text
    check("у владельца в меню есть «Сотрудники и роли»", 'class="up-item" href="/team"' in ho)
    r = owner.get("/team")
    check("/team открывается владельцу", r.status_code == 200 and "Роли и права" in r.text, str(r.status_code))
    with client() as anon:
        r = anon.get("/team", follow_redirects=False)
        check("/team без входа: на /login", r.status_code == 302 and r.headers["location"] == "/login")

    print("\n4. Своя роль с одним правом даёт ровно его")
    one_uid = add_user("one@roles.io", org_id)
    with client() as one:
        login(one, "one@roles.io")
        for perm in roles.PERM_KEYS:
            r = owner.post("/api/team/roles", json={"name": f"Только {perm}", "perms": [perm]})
            if r.status_code != 200:
                check(f"роль {perm} создана", False, r.text[:200])
                continue
            rid = r.json()["created_role_id"]
            a = owner.post(f"/api/team/members/{one_uid}/role", json={"role_id": rid})
            got, _n = perms_of(one)
            leaked = []
            for method, path, p in PERM_ROUTES:
                if p == perm:
                    continue
                # Сброс ростовки обслуживает два права: позиция (drafts_edit,
                # метка ручки) и вся таблица (drafts_reset_all, пустое тело).
                if path == "/api/replenish-draft/reset" and perm == "drafts_reset_all":
                    continue
                rr = call(one, method, path)
                if rr.status_code != 403:
                    leaked.append((method, path, rr.status_code))
            check(f"«{perm}»: права ровно {{{perm}}}, на ручках других прав 403",
                  a.status_code == 200 and got == {perm} and not leaked, f"{got} {leaked[:5]}")
            if perm == "drafts_edit":
                rr = one.post("/api/replenish-draft/reset", json={"base_name": ""})
                check("drafts_edit без drafts_reset_all: сброс всей таблицы 403",
                      rr.status_code == 403 and "Сбрасывать всю таблицу" not in rr.text
                      and "сбрасывать всю таблицу" in rr.json()["detail"], rr.text[:200])
                rr = one.post("/api/replenish-draft/reset", json={"base_name": base})
                check("drafts_edit: сброс своей позиции проходит", rr.status_code == 200, rr.text[:200])
            if perm == "orders_delete":
                cr = owner.post("/api/orders", json={"name": "На удаление", "items": [
                    {"base_name": base, "qty": 1, "sizes": {size: 1}}]})
                rr = one.delete(f"/api/orders/{cr.json()['id']}")
                check("orders_delete: удаляет заказ", rr.status_code == 200, rr.text[:200])
                rr = one.post("/api/orders", json={"name": "x", "items": [
                    {"base_name": base, "qty": 1, "sizes": {size: 1}}]})
                check("orders_delete: создать заказ нельзя (403)", rr.status_code == 403)
            if perm == "users_manage":
                check("users_manage: /api/team открывается", one.get("/api/team").status_code == 200)
                check("users_manage: страница /team открывается", one.get("/team").status_code == 200)
        # Роль с правом удаления заказов: на странице кнопка не прячется.
        rid = next(r["id"] for r in owner.get("/api/team").json()["roles"] if r["name"] == "Только orders_delete")
        owner.post(f"/api/team/members/{one_uid}/role", json={"role_id": rid})
        h = one.get("/replenish").text
        check("интерфейс: data-perms своей роли = orders_delete",
              'data-perms="orders_delete"' in h and 'data-role="member"' in h)

    print("\n5. Свои роли: имя, удаление, проверки")
    r = owner.post("/api/team/roles", json={"name": "только orders_delete", "perms": []})
    check("имя роли уникально без учёта регистра (422)", r.status_code == 422, r.text[:200])
    r = owner.post("/api/team/roles", json={"name": "Кривая", "perms": ["hack"]})
    check("неизвестное право (422)", r.status_code == 422, r.text[:200])
    r = owner.post("/api/team/roles", json={"name": "  ", "perms": []})
    check("пустое имя (422)", r.status_code == 422)
    r = owner.post(f"/api/team/roles/{rid}/delete")
    check("роль, на которой сидит сотрудник, не удаляется (422 с текстом)",
          r.status_code == 422 and "назначьте им другую роль" in r.text, r.text[:200])
    owner.post(f"/api/team/members/{one_uid}/role", json={"role_id": emp_id})
    r = owner.post(f"/api/team/roles/{rid}/delete")
    check("пустая роль удаляется", r.status_code == 200 and not any(
        x["id"] == rid for x in r.json()["roles"]), r.text[:200])
    r = owner.post("/api/team/roles", json={"name": "Склад", "perms": ["orders_manage"]})
    sid = r.json()["created_role_id"]
    r = owner.post(f"/api/team/roles/{sid}", json={"name": "Склад и приёмка",
                                                    "perms": ["orders_manage", "plan"]})
    sr = next(x for x in r.json()["roles"] if x["id"] == sid)
    check("свою роль можно переименовать и поменять права",
          sr["name"] == "Склад и приёмка" and sr["perms"] == ["plan", "orders_manage"], str(sr))

    print("\n6. Владельцы")
    r = owner.post(f"/api/team/members/{owner_uid}/role", json={"role_id": emp_id})
    check("последний владелец не снимает с себя роль (422)",
          r.status_code == 422 and "хотя бы один владелец" in r.text, r.text[:200])
    r = owner.post(f"/api/team/members/{owner_uid}/remove")
    check("себя убрать нельзя (422)", r.status_code == 422, r.text[:200])
    check("владелец по-прежнему владелец",
          sql("SELECT role FROM memberships WHERE user_id=? AND org_id=?", owner_uid, org_id) == [("owner",)])
    hr_uid = add_user("hr@roles.io", org_id)
    r = owner.post("/api/team/roles", json={"name": "Кадры", "perms": ["users_manage", "plan"]})
    hr_role = r.json()["created_role_id"]
    owner.post(f"/api/team/members/{hr_uid}/role", json={"role_id": hr_role})
    with client() as hr:
        login(hr, "hr@roles.io")
        r = hr.post(f"/api/team/members/{old_uid}/role", json={"role_id": owner_id})
        check("не-владелец не назначает «Владельца» (403)", r.status_code == 403, r.text[:200])
        r = hr.post(f"/api/team/members/{owner_uid}/role", json={"role_id": view_id})
        check("не-владелец не трогает владельца (403)", r.status_code == 403, r.text[:200])
        r = hr.post(f"/api/team/members/{owner_uid}/remove")
        check("не-владелец не убирает владельца (403)", r.status_code == 403, r.text[:200])
        r = hr.post("/api/team/roles", json={"name": "Хочу настройки", "perms": ["settings"]})
        check("не-владелец не выдаёт права, которых нет у него (403)", r.status_code == 403, r.text[:200])
        r = hr.post("/api/team/roles", json={"name": "Помощник кадров", "perms": ["plan"]})
        check("не-владелец создаёт роль в пределах своих прав", r.status_code == 200, r.text[:200])
        r = hr.post(f"/api/team/members/{old_uid}/role", json={"role_id": sid})
        check("не-владелец не выдаёт роль шире своих прав (403)", r.status_code == 403, r.text[:200])
        r = hr.post("/api/team/invites", json={"role_id": owner_id})
        check("не-владелец не приглашает «Владельцем» (403)", r.status_code == 403, r.text[:200])
        r = hr.post(f"/api/team/members/{old_uid}/role", json={"role_id": view_id})
        check("не-владелец не понижает того, у кого прав больше (403)", r.status_code == 403, r.text[:200])
        owner.post(f"/api/team/members/{one_uid}/role", json={"role_id": view_id})
        helper = next(x["id"] for x in hr.get("/api/team").json()["roles"] if x["name"] == "Помощник кадров")
        r = hr.post(f"/api/team/members/{one_uid}/role", json={"role_id": helper})
        check("не-владелец назначает роль в пределах своих прав", r.status_code == 200, r.text[:200])
    owner.post(f"/api/team/members/{old_uid}/role", json={"role_id": emp_id})
    r = owner.post(f"/api/team/members/{old_uid}/role", json={"role_id": owner_id})
    check("владелец назначает второго владельца",
          r.status_code == 200 and sql("SELECT role FROM memberships WHERE user_id=? AND org_id=?",
                                       old_uid, org_id) == [("owner",)], r.text[:200])
    r = owner.post(f"/api/team/members/{owner_uid}/role", json={"role_id": emp_id})
    check("при втором владельце можно снять роль с себя", r.status_code == 200, r.text[:200])
    perms, rname = perms_of(owner)
    check("права меняются сразу, без перевхода", rname == "Сотрудник"
          and perms == set(roles.EMPLOYEE_PERMS), f"{perms} {rname}")
    with client() as old:
        login(old, "old@roles.io")
        r = old.post(f"/api/team/members/{old_uid}/role", json={"role_id": emp_id})
        check("теперь единственный владелец не снимает с себя роль (422)", r.status_code == 422)
        r = old.post(f"/api/team/members/{owner_uid}/role", json={"role_id": owner_id})
        check("и возвращает роль первому", r.status_code == 200, r.text[:200])

    print("\n7. Изоляция организаций на /api/team")
    roleB = ownerB.get("/api/team").json()
    b_emp = next(r["id"] for r in roleB["roles"] if r["builtin"] == "employee")
    a_roles = {r["id"] for r in owner.get("/api/team").json()["roles"]}
    inv_a = owner.post("/api/team/invites", json={"role_id": emp_id}).json()["id"]
    cases = [
        ("POST", f"/api/team/members/{hr_uid}/role", {"role_id": b_emp}),
        ("POST", f"/api/team/members/{hr_uid}/remove", {}),
        ("POST", f"/api/team/roles/{sid}", {"name": "Захват"}),
        ("POST", f"/api/team/roles/{sid}/delete", {}),
        ("POST", "/api/team/invites", {"role_id": emp_id}),
        ("POST", f"/api/team/invites/{inv_a}/revoke", {}),
    ]
    for method, path, body in cases:
        r = ownerB.request(method, path, json=body)
        check(f"чужая организация: {path} → 404 без данных",
              r.status_code == 404 and "Роли" not in r.text and "roles.io" not in r.text,
              f"{r.status_code} {r.text[:120]}")
    b_uid = sql("SELECT id FROM users WHERE email='own@b.io'")[0][0]
    r = owner.post(f"/api/team/members/{b_uid}/role", json={"role_id": emp_id})
    check("свою роль чужому человеку не назначить (404)", r.status_code == 404)
    r = ownerB.post(f"/api/team/members/{b_uid}/role", json={"role_id": emp_id})
    check("себе роль чужой организации не назначить (404)", r.status_code == 404)
    tb = ownerB.get("/api/team").json()
    check("в /api/team чужой организации нет наших людей, ролей и приглашений",
          "roles.io" not in str(tb) and not ({r["id"] for r in tb["roles"]} & a_roles)
          and not tb["invites"], str(tb)[:200])
    check("наше приглашение не отозвано чужим",
          sql("SELECT revoked_at FROM org_invites WHERE id=?", inv_a) == [(None,)])
    for bad_id in ("0", "2147483648", "-1"):
        r = owner.post(f"/api/team/roles/{bad_id}", json={"name": "x"})
        check(f"id за границей ({bad_id}): 422", r.status_code == 422, str(r.status_code))

    print("\n8. Приглашения")
    r = owner.post("/api/team/invites", json={"role_id": view_id})
    check("владелец создаёт приглашение", r.status_code == 200 and r.json()["link_path"].startswith("/invite#t="),
          r.text[:200])
    token = r.json()["token"]
    inv_id = r.json()["id"]
    row = sql("SELECT * FROM org_invites WHERE id=?", inv_id)[0]
    check("токен в базе не хранится, только sha256",
          token not in [str(v) for v in row]
          and hashlib.sha256(token.encode()).hexdigest() in row, str(row))
    check("приглашение видно в списке активных",
          any(i["id"] == inv_id for i in owner.get("/api/team").json()["invites"]))
    with client() as anon:
        page = anon.get("/invite")
        check("страница /invite открывается и не отдаёт Referer",
              page.status_code == 200 and "Приглашение в команду" in page.text
              and page.headers.get("referrer-policy") == "no-referrer")
        info = anon.post("/api/invite/info", json={"token": token})
        check("по ссылке видно организацию и роль",
              info.status_code == 200 and info.json()["org_name"] == "Роли"
              and info.json()["role_name"] == "Наблюдатель" and info.json()["signed_in"] is None,
              info.text[:200])
        r = anon.post("/api/invite/accept", json={"token": token, "mode": "join"})
        check("«присоединиться» без входа: 401, ссылка не потрачена",
              r.status_code == 401 and sql("SELECT used_at FROM org_invites WHERE id=?", inv_id) == [(None,)])
        r = anon.post("/api/invite/accept", json={"token": token, "mode": "register",
                                                  "name": "Новенький", "email": "new@roles.io",
                                                  "password": "short"})
        check("короткий пароль: 422, ссылка не потрачена",
              r.status_code == 422 and sql("SELECT used_at FROM org_invites WHERE id=?", inv_id) == [(None,)])
        r = anon.post("/api/invite/accept", json={"token": token, "mode": "register",
                                                  "name": "Новенький", "email": "New@Roles.io",
                                                  "password": PW})
        check("регистрация по ссылке", r.status_code == 200, r.text[:200])
        perms, rname = perms_of(anon)
        check("новый человек вошёл в организацию с ролью из приглашения",
              rname == "Наблюдатель" and perms == set(), f"{perms} {rname}")
        new_uid = sql("SELECT id FROM users WHERE email='new@roles.io'")
        check("новая организация не создана, членство одно",
              bool(new_uid) and sql("SELECT org_id FROM memberships WHERE user_id=?", new_uid[0][0])
              == [(org_id,)] and sql("SELECT COUNT(*) FROM orgs")[0][0] == 2)
    with client() as anon2:
        r = anon2.post("/api/invite/accept", json={"token": token, "mode": "register",
                                                   "email": "again@roles.io", "password": PW})
        check("повторное использование отклонено (400)",
              r.status_code == 400 and not sql("SELECT id FROM users WHERE email='again@roles.io'"),
              r.text[:200])
        r = anon2.post("/api/invite/info", json={"token": token})
        check("повторный просмотр использованной ссылки: 400", r.status_code == 400)
        r = anon2.post("/api/invite/info", json={"token": "мусор"})
        check("неизвестный токен: 400", r.status_code == 400)
    check("использованное приглашение ушло из активных",
          not any(i["id"] == inv_id for i in owner.get("/api/team").json()["invites"]))

    # Просрочено.
    r = owner.post("/api/team/invites", json={"role_id": emp_id})
    tok_exp, id_exp = r.json()["token"], r.json()["id"]
    sql("UPDATE org_invites SET expires_at='2000-01-01 00:00:00' WHERE id=?", id_exp)
    with client() as anon3:
        r = anon3.post("/api/invite/accept", json={"token": tok_exp, "mode": "register",
                                                   "email": "late@roles.io", "password": PW})
        check("просроченное отклонено (400)", r.status_code == 400
              and not sql("SELECT id FROM users WHERE email='late@roles.io'"), r.text[:200])
    # Отозвано.
    r = owner.post("/api/team/invites", json={"role_id": emp_id})
    tok_rev, id_rev = r.json()["token"], r.json()["id"]
    r = owner.post(f"/api/team/invites/{id_rev}/revoke")
    check("владелец отзывает приглашение", r.status_code == 200
          and not any(i["id"] == id_rev for i in r.json()["invites"]))
    with client() as anon4:
        r = anon4.post("/api/invite/accept", json={"token": tok_rev, "mode": "register",
                                                   "email": "rev@roles.io", "password": PW})
        check("отозванное отклонено (400)", r.status_code == 400
              and not sql("SELECT id FROM users WHERE email='rev@roles.io'"), r.text[:200])
    # Вход существующим аккаунтом (человек из другой организации).
    r = owner.post("/api/team/invites", json={"role_id": sid})
    tok_l, id_l = r.json()["token"], r.json()["id"]
    with client() as anon5:
        r = anon5.post("/api/invite/accept", json={"token": tok_l, "mode": "login",
                                                   "email": "own@b.io", "password": "wrong-pass"})
        check("неверный пароль: 403, ссылка не потрачена",
              r.status_code == 403 and sql("SELECT used_at FROM org_invites WHERE id=?", id_l) == [(None,)])
        r = anon5.post("/api/invite/accept", json={"token": tok_l, "mode": "login",
                                                   "email": "own@b.io", "password": PW})
        check("вход существующим аккаунтом по ссылке", r.status_code == 200, r.text[:200])
        perms, rname = perms_of(anon5)
        check("сессия в новой организации с ролью из ссылки",
              rname == "Склад и приёмка" and perms == {"plan", "orders_manage"}, f"{perms} {rname}")
        check("в своей организации он по-прежнему владелец",
              sql("SELECT org_id, role FROM memberships WHERE user_id=? ORDER BY org_id", b_uid)
              == [(org_id, "member"), (org_b, "owner")])
    # Уже участник.
    r = owner.post("/api/team/invites", json={"role_id": emp_id})
    tok_m, id_m = r.json()["token"], r.json()["id"]
    r = owner.post("/api/invite/accept", json={"token": tok_m, "mode": "join"})
    check("уже участник: 409, ссылка не потрачена",
          r.status_code == 409 and sql("SELECT used_at FROM org_invites WHERE id=?", id_m) == [(None,)],
          r.text[:200])
    # Уже вошёл (join) другим аккаунтом: hr из этой же организации не подходит,
    # берём наблюдателя из второй организации.
    view_b = add_user("viewer@b.io", org_b)
    with client() as vb:
        login(vb, "viewer@b.io")
        info = vb.post("/api/invite/info", json={"token": tok_m}).json()
        check("info видит, кто вошёл", (info.get("signed_in") or {}).get("email") == "viewer@b.io")
        r = vb.post("/api/invite/accept", json={"token": tok_m, "mode": "join"})
        check("вошедший присоединяется одной кнопкой", r.status_code == 200, r.text[:200])
        perms, rname = perms_of(vb)
        check("и работает в новой организации «Сотрудником»", rname == "Сотрудник")
    # Удаление роли гасит её приглашения.
    r = owner.post("/api/team/roles", json={"name": "Временная роль", "perms": []})
    tmp_role = r.json()["created_role_id"]
    r = owner.post("/api/team/invites", json={"role_id": tmp_role})
    tok_t = r.json()["token"]
    owner.post(f"/api/team/roles/{tmp_role}/delete")
    with client() as anon6:
        r = anon6.post("/api/invite/info", json={"token": tok_t})
        check("приглашение удалённой роли недействительно", r.status_code == 400)
    # Убрать из организации.
    r = owner.post(f"/api/team/members/{view_b}/remove")
    check("владелец убирает сотрудника", r.status_code == 200
          and not sql("SELECT 1 FROM memberships WHERE user_id=? AND org_id=?", view_b, org_id))
    with client() as vb2:
        login(vb2, "viewer@b.io")
        check("убранный больше не видит организацию",
              vb2.get("/api/settings").json()["org"]["name"] == "Чужая")

    print("\n9. МойСклад: участник из каталога получает «Сотрудника»")
    from app import routes_ms_app
    src = Path(routes_ms_app.__file__).read_text(encoding="utf-8")
    check("routes_ms_app назначает встроенную роль «Сотрудник»",
          "ensure_builtin_roles" in src and "_roles.EMPLOYEE" in src)

    print("\n10. Удаление организации уносит роли и приглашения")
    with client() as solo:
        solo.post("/register", data={"name": "Соло", "email": "solo@roles.io",
                                     "password": PW, "org_name": "Соло"})
        solo_org = sql("SELECT id FROM orgs WHERE name='Соло'")[0][0]
        solo.get("/api/team")
        sv = next(r["id"] for r in solo.get("/api/team").json()["roles"] if r["builtin"] == "viewer")
        solo.post("/api/team/invites", json={"role_id": sv})
        r = solo.post("/api/account/delete", json={"password": PW, "confirm": "УДАЛИТЬ"})
        check("организация удалена", r.status_code == 200, r.text[:200])
        check("ролей и приглашений организации не осталось",
              sql("SELECT COUNT(*) FROM org_roles WHERE org_id=?", solo_org)[0][0] == 0
              and sql("SELECT COUNT(*) FROM org_invites WHERE org_id=?", solo_org)[0][0] == 0)

print("\n== Ревью Codex #110: владелец другой организации и гонка владельцев ==")
with client() as ha, client() as hb:
    ha.post("/register", data={"name": "Альфа", "email": "alpha@roles.io",
                               "password": PW, "org_name": "Альфа"})
    hb.post("/register", data={"name": "Бета", "email": "beta@roles.io",
                               "password": PW, "org_name": "Бета"})
    a_owner = next(r["id"] for r in ha.get("/api/team").json()["roles"] if r["builtin"] == "owner")
    tok = ha.post("/api/team/invites", json={"role_id": a_owner}).json()["link_path"].split("t=", 1)[1]
    r = hb.post("/api/invite/accept", json={"token": tok, "mode": "join"})
    check("владелец Беты принял приглашение владельцем в Альфу", r.status_code == 200, r.text[:200])
    in_alpha = hb.get("/api/team").json()
    r = hb.post("/api/account/delete", json={"password": PW, "confirm": "УДАЛИТЬ"})
    check("удаление аккаунта отказано: он единственный владелец Беты",
          r.status_code == 422 and "Бета" in r.text, f"{r.status_code} {r.text[:200]}")
    beta_id = sql("SELECT id FROM orgs WHERE name='Бета'")[0][0]
    check("Бета осталась с владельцем",
          sql("SELECT COUNT(*) FROM memberships WHERE org_id=? AND role='owner'", beta_id)[0][0] == 1)

    import threading as _th
    alpha_id = sql("SELECT id FROM orgs WHERE name='Альфа'")[0][0]
    beta_uid = sql("SELECT id FROM users WHERE email='beta@roles.io'")[0][0]
    alpha_uid = sql("SELECT id FROM users WHERE email='alpha@roles.io'")[0][0]
    emp = next(r["id"] for r in ha.get("/api/team").json()["roles"] if r["builtin"] == "employee")
    results = []

    def demote(c, uid):
        results.append(c.post(f"/api/team/members/{uid}/role", json={"role_id": emp}).status_code)

    t1 = _th.Thread(target=demote, args=(ha, alpha_uid))
    t2 = _th.Thread(target=demote, args=(hb, beta_uid))
    t1.start(); t2.start(); t1.join(); t2.join()
    owners_left = sql("SELECT COUNT(*) FROM memberships WHERE org_id=? AND role='owner'", alpha_id)[0][0]
    check("два владельца снимают себя одновременно: владелец остаётся",
          owners_left >= 1 and sorted(results).count(422) >= 1, f"{results} владельцев={owners_left}")

print("\n== Ревью Codex #110: сброс всей ростовки отдельным правом, автор приглашения ==")
with client() as hc:
    hc.post("/register", data={"name": "Гамма", "email": "gamma@roles.io",
                               "password": PW, "org_name": "Гамма"})
    hc.post("/api/connect/demo")
    g_org = sql("SELECT id FROM orgs WHERE name='Гамма'")[0][0]
    rr = hc.post("/api/team/roles", json={"name": "Только сброс", "perms": ["drafts_reset_all"]})
    rid = rr.json().get("id") or next(r["id"] for r in hc.get("/api/team").json()["roles"]
                                      if r["name"] == "Только сброс")
    uid = add_user("reset@roles.io", g_org, role_id=rid)
    with client() as hr:
        login(hr, "reset@roles.io")
        r_all = hr.post("/api/replenish-draft/reset", json={"base_name": ""})
        r_one = hr.post("/api/replenish-draft/reset", json={"base_name": "Худи «Скетч»"})
        check("роль только с drafts_reset_all сбрасывает всю таблицу, но не позицию",
              r_all.status_code == 200 and r_one.status_code == 403, f"{r_all.status_code} {r_one.status_code}")
    mgr = next(r["id"] for r in hc.get("/api/team").json()["roles"] if r["builtin"] == "owner")
    uid2 = add_user("creator@roles.io", g_org, role="owner", role_id=mgr, name="Автор Приглашения")
    with client() as hm:
        login(hm, "creator@roles.io")
        emp = next(r["id"] for r in hm.get("/api/team").json()["roles"] if r["builtin"] == "employee")
        hm.post("/api/team/invites", json={"role_id": emp})
    sql("DELETE FROM memberships WHERE user_id=? AND org_id=?", uid2, g_org)
    invs = hc.get("/api/team").json().get("invites") or []
    check("автор не в организации: имя автора приглашения не показывается",
          invs and all(i.get("created_by") == "" for i in invs if i.get("created_by") != "Гамма"),
          str(invs)[:300])

print("\n== Ревью Codex #110: убранный из МойСклада не возвращается сам; переключение организаций ==")
from app import ms_vendor as _msv_mod
_orig_ctx = _msv_mod.get_context


async def _fake_ctx(key):
    return {"accountId": "acc-roles-ms", "uid": "emp@roles-ms", "fullName": "Сотрудник МС"}

_msv_mod.get_context = _fake_ctx
try:
    with client() as hd:
        hd.post("/register", data={"name": "Дельта", "email": "delta@roles.io",
                                   "password": PW, "org_name": "Дельта"})
        d_org = sql("SELECT id FROM orgs WHERE name='Дельта'")[0][0]
        sql("UPDATE orgs SET ms_account_id='acc-roles-ms' WHERE id=?", d_org)
        with client() as hm:
            r = hm.get("/ms/app", params={"contextKey": "k"}, follow_redirects=False)
            check("сотрудник МС входит и становится участником", r.status_code == 302, str(r.status_code))
        ms_uid = sql("SELECT id FROM users WHERE ms_uid='emp@roles-ms'")[0][0]
        r = hd.post(f"/api/team/members/{ms_uid}/remove")
        check("владелец убирает сотрудника МС", r.status_code == 200, r.text[:200])
        with client() as hm2:
            r = hm2.get("/ms/app", params={"contextKey": "k"}, follow_redirects=False)
            check("повторный вход из МС не возвращает убранного", r.status_code == 403
                  and not sql("SELECT 1 FROM memberships WHERE user_id=? AND org_id=?", ms_uid, d_org),
                  f"{r.status_code}")
finally:
    _msv_mod.get_context = _orig_ctx

with client() as h1, client() as h2:
    h1.post("/register", data={"name": "Эпсилон", "email": "eps@roles.io",
                               "password": PW, "org_name": "Эпсилон"})
    h2.post("/register", data={"name": "Зета", "email": "zeta@roles.io",
                               "password": PW, "org_name": "Зета"})
    viewer = next(r["id"] for r in h2.get("/api/team").json()["roles"] if r["builtin"] == "viewer")
    tok = h2.post("/api/team/invites", json={"role_id": viewer}).json()["link_path"].split("t=", 1)[1]
    r = h1.post("/api/invite/accept", json={"token": tok, "mode": "join"})
    check("Эпсилон принял приглашение в Зету", r.status_code == 200, r.text[:200])
with client() as h3:
    login(h3, "eps@roles.io")
    orgs = h3.get("/api/account/orgs").json()["orgs"]
    check("в списке две организации", sorted(o["name"] for o in orgs) == ["Зета", "Эпсилон"], str(orgs))
    other = next(o for o in orgs if not o["current"])
    r = h3.post("/api/account/switch-org", json={"org_id": other["org_id"]})
    now = next(o for o in h3.get("/api/account/orgs").json()["orgs"] if o["current"])
    check("переключение в другую свою организацию", r.status_code == 200 and now["org_id"] == other["org_id"],
          f"{r.status_code} {now}")
    foreign = sql("SELECT id FROM orgs WHERE name='Дельта'")[0][0]
    r = h3.post("/api/account/switch-org", json={"org_id": foreign})
    check("в чужую организацию переключиться нельзя (404)", r.status_code == 404, str(r.status_code))

print("\n== Ревью #110 (Fable): ссылка убранного или пониженного кадровика гаснет ==")
with client() as ho:
    ho.post("/register", data={"name": "Тета", "email": "theta@roles.io",
                               "password": PW, "org_name": "Тета"})
    t_org = sql("SELECT id FROM orgs WHERE name='Тета'")[0][0]
    rr = ho.post("/api/team/roles", json={"name": "Кадровик", "perms": ["plan", "users_manage"]})
    hr_role = rr.json().get("id") or next(r["id"] for r in ho.get("/api/team").json()["roles"]
                                          if r["name"] == "Кадровик")
    viewer_t = next(r["id"] for r in ho.get("/api/team").json()["roles"] if r["builtin"] == "viewer")
    hr_uid = add_user("hr@theta.io", t_org, role_id=hr_role)
    with client() as hh:
        login(hh, "hr@theta.io")
        own = hh.post("/api/team/invites", json={"role_id": hr_role}).json()["link_path"].split("t=", 1)[1]
        own2 = hh.post("/api/team/invites", json={"role_id": hr_role}).json()["link_path"].split("t=", 1)[1]
    r = ho.post(f"/api/team/members/{hr_uid}/role", json={"role_id": viewer_t})
    with client() as anon:
        r = anon.post("/api/invite/accept", json={"token": own, "mode": "register", "name": "Двойник",
                                                  "email": "twin@theta.io", "password": PW})
        check("пониженный кадровик: его ссылка не действует", r.status_code == 400, f"{r.status_code} {r.text[:120]}")
    ho.post(f"/api/team/members/{hr_uid}/remove")
    sql("UPDATE org_invites SET revoked_at=NULL WHERE created_by=?", hr_uid)  # даже если отзыв пропустили
    with client() as anon2:
        r = anon2.post("/api/invite/accept", json={"token": own2, "mode": "login",
                                                   "email": "hr@theta.io", "password": PW})
        check("убранный кадровик не возвращается по своей ссылке (автор больше не участник)",
              r.status_code == 400 and not sql("SELECT 1 FROM memberships WHERE user_id=? AND org_id=?", hr_uid, t_org),
              f"{r.status_code} {r.text[:120]}")

for base in (DB_PATH, OLD_DB_PATH):
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(base) + suffix)
        if p.exists():
            p.unlink()
print(f"\nИТОГО: {OK} OK, {FAIL} FAIL")
sys.exit(1 if FAIL else 0)
