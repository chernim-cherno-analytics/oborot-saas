# -*- coding: utf-8 -*-
"""Оплата клиента без ручного SQL и полоса подписки (BILLING-OPS-1, 14.11.2026).

Что доказывается:
  1) tools/billing.py: invoiced начинает грейс; paid ставит paid_until и
     тариф заявки и закрывает заявку; trial продлевает только вперёд;
     даты в прошлом, дальше 400 дней, сокращение срока, чужая заявка,
     организация из каталога МойСклада и несуществующие id получают отказ
     (код 2) и НИЧЕГО не меняют;
  2) gate-preview и list работают и ничего не пишут;
  3) полоса подписки: живой триал говорит «Триал до» и только его; истёкший
     триал при выключенном гейте мягко зовёт к тарифу на страницах общего
     каркаса и молчит на девяти самостоятельных; при включённом гейте
     закрытая запись названа везде, включая самостоятельные страницы;
     грейс назван с датой; организация из каталога МойСклада полосы не
     видит.

Синтетическая база, наружу набор не ходит. Запуск: python tests/test_billing_ops.py
"""
import io
import os
import sqlite3
import sys
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

DB_PATH = ROOT / "test_billing_ops.db"
for suffix in ("", "-wal", "-shm"):
    p = Path(str(DB_PATH) + suffix)
    if p.exists():
        p.unlink()
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["SCHEDULER_ENABLED"] = "0"
os.environ.pop("OBOROT_SUBSCRIPTION_GATE", None)

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
import billing  # noqa: E402

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


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = billing.main(["--db", str(DB_PATH), *argv])
    return code, out.getvalue(), err.getvalue()


TODAY = datetime.utcnow().date()


def D(n):
    return (TODAY + timedelta(days=n)).isoformat()


with TestClient(app, headers={"X-Oborot-CSRF": "1"}) as c:
    c.post("/register", data={"name": "Оператор", "email": "bill@test.io",
                              "password": "secret123", "org_name": "Платящий Бренд"})
    org_id = sql("SELECT id FROM orgs WHERE name=?", "Платящий Бренд")[0][0]
    c2 = TestClient(app, headers={"X-Oborot-CSRF": "1"})
    c2.post("/register", data={"name": "Сосед", "email": "neighbour-bill@test.io",
                               "password": "secret123", "org_name": "Сосед"})
    other_id = sql("SELECT id FROM orgs WHERE name=?", "Сосед")[0][0]
    sql("INSERT INTO billing_requests (org_id,user_id,plan,period,amount,company,inn,email,phone,comment,status,created_at)"
        " VALUES (?,1,'brand','month',9900,'ИП Тест','123456789012','bill@test.io','','','new',?)",
        org_id, datetime.utcnow().isoformat(sep=" "))
    req_id = sql("SELECT id FROM billing_requests WHERE org_id=?", org_id)[0][0]

    print("\n1. Полоса подписки")
    sql("UPDATE orgs SET trial_ends_at=? WHERE id=?", f"{D(7)} 12:00:00", org_id)
    s = c.get("/settings").text
    check("живой триал: «Триал до» и без полосы подписки",
          "Триал до" in s and "sub-strip" not in s)
    sql("UPDATE orgs SET trial_ends_at=? WHERE id=?", f"{D(-30)} 12:00:00", org_id)
    s = c.get("/settings").text
    check("истёкший триал, гейт выключен: «Триал до» больше не пишется", "Триал до" not in s)
    check("истёкший триал, гейт выключен: мягкая подсказка на странице каркаса",
          "Пробный период закончился" in s and 'href="/plans"' in s and "sub-blocked" not in s)
    t = c.get("/turnover").text
    check("истёкший триал, гейт выключен: самостоятельная страница про тариф молчит",
          "sub-strip" not in t and "Пробный период" not in t)
    os.environ["OBOROT_SUBSCRIPTION_GATE"] = "1"
    for path in ("/turnover", "/replenish", "/assistant", "/settings"):
        h = c.get(path).text
        check(f"гейт включён, readonly: {path} называет закрытую запись",
              "Подписка не активна" in h and "sub-blocked" in h, path)
    os.environ.pop("OBOROT_SUBSCRIPTION_GATE")

    print("\n2. Инструмент оплаты: отказы ничего не меняют")
    before = sql("SELECT paid_until, plan, trial_ends_at FROM orgs WHERE id=?", org_id)
    for label, argv in (
        ("дата в прошлом", ("paid", "--org", str(org_id), "--until", D(-1), "--operator", "t")),
        ("опечатка в годе", ("paid", "--org", str(org_id), "--until", D(4000), "--operator", "t")),
        ("не дата", ("paid", "--org", str(org_id), "--until", "31.12.2026", "--operator", "t")),
        ("нет организации", ("paid", "--org", "99999", "--until", D(30), "--operator", "t")),
        ("чужая заявка", ("paid", "--org", str(other_id), "--until", D(30), "--operator", "t",
                          "--request", str(req_id))),
        ("нет заявки", ("invoiced", "--request", "99999", "--operator", "t")),
        ("триал назад", ("trial", "--org", str(org_id), "--until", D(-5), "--operator", "t")),
    ):
        code, out, err = run(*argv)
        check(f"отказ: {label} (код 2, причина названа)", code == 2 and "Отказ" in err, err + out)
    after = sql("SELECT paid_until, plan, trial_ends_at FROM orgs WHERE id=?", org_id)
    check("после отказов организация не изменилась", before == after, f"{before} {after}")
    check("после отказов заявка не изменилась",
          sql("SELECT status FROM billing_requests WHERE id=?", req_id)[0][0] == "new")

    print("\n3. Инструмент оплаты: счёт, оплата, продление")
    code, out, _ = run("invoiced", "--request", str(req_id), "--operator", "vlad")
    st, inv = sql("SELECT status, invoiced_at FROM billing_requests WHERE id=?", req_id)[0]
    check("invoiced: заявка выставлена, отметка счёта стоит", code == 0 and st == "invoiced" and inv, out)
    check("invoiced: состояние организации grace", "grace" in out, out)
    s = c.get("/settings").text
    check("грейс: полоса «Счёт выставлен» с датой", "Счёт выставлен" in s and "Всё работает до" in s)
    code, out, _ = run("paid", "--org", str(org_id), "--until", D(30), "--operator", "vlad",
                       "--request", str(req_id))
    pu, plan = sql("SELECT paid_until, plan FROM orgs WHERE id=?", org_id)[0]
    check("paid: оплачено до даты, тариф из заявки, состояние active",
          code == 0 and str(pu).startswith(D(30)) and plan == "brand" and "active" in out, out)
    check("paid: заявка закрыта", sql("SELECT status FROM billing_requests WHERE id=?", req_id)[0][0] == "paid")
    code, out, err = run("paid", "--org", str(org_id), "--until", D(10), "--operator", "vlad")
    check("paid: сократить срок нельзя", code == 2 and "сокращать" in err, err)
    code, out, _ = run("paid", "--org", str(org_id), "--until", D(60), "--operator", "vlad")
    check("paid: продление вперёд без заявки", code == 0
          and str(sql("SELECT paid_until FROM orgs WHERE id=?", org_id)[0][0]).startswith(D(60)), out)
    s = c.get("/settings").text
    check("оплаченный клиент: ни полосы подписки, ни «Триал до»",
          "sub-strip" not in s and "Триал до" not in s)
    code, out, _ = run("trial", "--org", str(other_id), "--until", D(20), "--operator", "vlad")
    check("trial: пробный период продлён вперёд", code == 0
          and str(sql("SELECT trial_ends_at FROM orgs WHERE id=?", other_id)[0][0]).startswith(D(20)), out)

    print("\n4. Каталог МойСклада и только чтение")
    sql("UPDATE orgs SET source='ms_app', trial_ends_at=? WHERE id=?", f"{D(-30)} 12:00:00", other_id)
    code, out, err = run("paid", "--org", str(other_id), "--until", D(30), "--operator", "t")
    check("каталог МойСклада: оплата здесь отказывает", code == 2 and "каталога" in err, err)
    os.environ["OBOROT_SUBSCRIPTION_GATE"] = "1"
    h = c2.get("/settings").text
    check("каталог МойСклада: своей полосы подписки нет", "sub-strip" not in h)
    os.environ.pop("OBOROT_SUBSCRIPTION_GATE")
    snapshot = sql("SELECT * FROM orgs ORDER BY id"), sql("SELECT * FROM billing_requests ORDER BY id")
    code, out, _ = run("list")
    check("list показывает организацию и заявку", code == 0 and "Платящий Бренд" in out
          and f"#{req_id}" in out, out)
    code, out, _ = run("gate-preview")
    check("gate-preview отвечает числами", code == 0 and "Если включить" in out, out)
    check("list и gate-preview ничего не пишут",
          snapshot == (sql("SELECT * FROM orgs ORDER BY id"), sql("SELECT * FROM billing_requests ORDER BY id")))

for suffix in ("", "-wal", "-shm"):
    p = Path(str(DB_PATH) + suffix)
    if p.exists():
        p.unlink()
print(f"\nИТОГО: {OK} OK, {FAIL} FAIL")
sys.exit(1 if FAIL else 0)
