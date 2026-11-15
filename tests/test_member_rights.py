# -*- coding: utf-8 -*-
"""Права участника организации (SEC-2, предложение 14.11.2026).

Что доказывается:
  1) участник получает 403 на действиях, меняющих всю организацию и не
     возвращаемых одним кликом: удаление заказа, ручная перезапись «Едет»,
     архив позиции, переименование и слияние категорий, ручная скидка;
  2) владелец на тех же действиях проходит;
  3) отказ ничего не меняет;
  4) участник по-прежнему работает: читает, выгружает, создаёт заказ,
     переводит его по статусам.

Синтетическая база, демо-данные. Запуск: python tests/test_member_rights.py
"""
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "test_member_rights.db"
for suffix in ("", "-wal", "-shm"):
    p = Path(str(DB_PATH) + suffix)
    if p.exists():
        p.unlink()
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["SCHEDULER_ENABLED"] = "0"

from fastapi.testclient import TestClient  # noqa: E402

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


with TestClient(app, headers={"X-Oborot-CSRF": "1"}) as owner, \
        TestClient(app, headers={"X-Oborot-CSRF": "1"}) as member:
    owner.post("/register", data={"name": "Владелец", "email": "owner-r@test.io",
                                  "password": "secret123", "org_name": "Права"})
    owner.post("/api/connect/demo")
    org_id = sql("SELECT id FROM orgs WHERE name='Права'")[0][0]
    member.post("/register", data={"name": "Участник", "email": "member-r@test.io",
                                   "password": "secret123", "org_name": "Временная"})
    uid = sql("SELECT id FROM users WHERE email='member-r@test.io'")[0][0]
    tmp_org = sql("SELECT id FROM orgs WHERE name='Временная'")[0][0]
    sql("DELETE FROM memberships WHERE user_id=?", uid)
    sql("INSERT INTO memberships (user_id, org_id, role) VALUES (?,?,'member')", uid, org_id)
    member.post("/logout")
    r = member.post("/login", data={"email": "member-r@test.io", "password": "secret123"})
    check("участник вошёл в организацию владельца",
          member.get("/api/settings").json().get("role") == "member", r.status_code)

    rep = owner.get("/api/replenish").json()
    item = next(i for i in rep["items"] if i.get("sizes"))
    base, cat = item["base_name"], item.get("category") or "Без категории"
    created = member.post("/api/orders", json={"name": "Заказ участника", "items": [
        {"base_name": base, "qty": 2, "sizes": {list(item["sizes"])[0]: 2}}]})
    print("\n4. Участник работает")
    check("участник создаёт заказ", created.status_code == 200, created.text[:200])
    order_id = created.json().get("id")
    st = member.post(f"/api/orders/{order_id}/status", json={"status": "sent"})
    check("участник переводит заказ в производство", st.status_code == 200, st.text[:200])
    for path in ("/api/turnover", "/api/replenish", "/api/budget", "/api/export/turnover.xlsx"):
        check(f"участник читает {path}", member.get(path).status_code == 200, path)

    print("\n0. Интерфейс знает роль и права (SEC-2, D-69; роли 15.11.2026)")
    # После ролей скрытие решают права (data-perms на body, _perms_css.html),
    # а не data-role: своя роль с правом удаления заказов тоже «member».
    import re as _re
    for path in ("/stocks", "/turnover", "/sizes", "/replenish"):
        h = member.get(path).text
        attr = _re.search(r'data-perms="([^"]*)"', h)
        perms_on_page = set(attr.group(1).split()) if attr else None
        check(f"участник: {path} помечен data-role=member и прячет действия владельца",
              'data-role="member"' in h
              and 'body:not([data-perms~="orders_delete"]) .ord-del' in h
              and perms_on_page is not None and "orders_delete" not in perms_on_page
              and "ordered_edit" not in perms_on_page and "catalog_edit" not in perms_on_page,
              f"{path} {perms_on_page}")
        oh = owner.get(path).text
        check(f"владелец: {path} помечен data-role=owner", 'data-role="owner"' in oh
              and 'orders_delete' in (_re.search(r'data-perms="([^"]*)"', oh) or [None, ""])[1], path)

    print("\n1-3. Действия на всю организацию")
    ordered_before = sql("SELECT qty FROM ordered_qty WHERE org_id=? AND base_name=?", org_id, base)
    cases = [
        ("DELETE", f"/api/orders/{order_id}", None),
        ("POST", "/api/ordered", {"base_name": base, "qty": 999}),
        ("POST", "/api/ordered/add", {"base_name": base, "qty": 500}),
        ("POST", "/api/hidden", {"base_name": base, "hidden": True}),
        ("POST", "/api/categories/override", {"base_name": base, "category": "Захвачено"}),
        ("POST", "/api/categories/merge", {"from_category": cat, "to_category": "Захвачено"}),
        ("POST", "/api/discount-overrides", {"base_name": base, "discount": 50}),
    ]
    for method, path, body in cases:
        r = member.request(method, path, json=body) if body is not None else member.request(method, path)
        # Текст отказа после ролей: какого права не хватает и к кому идти.
        check(f"участник: {method} {path} → 403 с объяснением",
              r.status_code == 403 and "Недостаточно прав" in r.text and "владельца" in r.text,
              f"{r.status_code} {r.text[:120]}")
    check("отказы ничего не изменили: заказ на месте",
          sql("SELECT COUNT(*) FROM production_orders WHERE id=?", order_id)[0][0] == 1)
    check("отказы ничего не изменили: «Едет» ни перезаписано, ни прибавлено",
          sql("SELECT qty FROM ordered_qty WHERE org_id=? AND base_name=?", org_id, base) == ordered_before,
          sql("SELECT qty FROM ordered_qty WHERE org_id=? AND base_name=?", org_id, base))
    after_rep = owner.get("/api/replenish").json()
    check("отказы ничего не изменили: категория позиции прежняя",
          next(i for i in after_rep["items"] if i["base_name"] == base).get("category") == item.get("category"))
    for method, path, body in cases[1:]:
        r = owner.request(method, path, json=body)
        check(f"владелец: {method} {path} проходит", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
    r = owner.delete(f"/api/orders/{order_id}")
    check("владелец удаляет заказ (отправленный, не принятый)", r.status_code == 200, r.text[:200])

for suffix in ("", "-wal", "-shm"):
    p = Path(str(DB_PATH) + suffix)
    if p.exists():
        p.unlink()
print(f"\nИТОГО: {OK} OK, {FAIL} FAIL")
sys.exit(1 if FAIL else 0)
