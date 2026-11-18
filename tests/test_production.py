# -*- coding: utf-8 -*-
"""PROD-1 «Производство» (решение Влада 17.11.2026): API доски запусков.

Проверяется:
  1) отправленный заказ сам появляется на доске в «Заказано», черновик нет;
  2) отметка этапа: одним вызовом с сегодняшней датой, повтор не пишет
     дубль, второй раз тот же этап даёт 409, будущая дата и чужой этап 422;
  3) сдача частями на последнем этапе: количества складываются;
  4) приёмка частями и целиком меняет колонку, «Едет» и заказ слой не трогает;
  5) дата «на складе»: по сроку цеха, по плану, по истории цеха, опоздание;
  6) этапы цеха: шаблон, переименование не теряет отметки, удалить этап с
     отметками нельзя;
  7) чужая организация не видит и не меняет ничего; наблюдатель без права
     «Вести заказы» отметить не может;
  8) удалённый заказ не передаёт свои отметки новому заказу с тем же id;
  9) /supply ведёт на /production, в меню «Производство», «Поставок» нет;
 10) удаление организации стирает и отметки, и этапы.

Запуск из корня репозитория:  python tests/test_production.py
"""
import os
import sqlite3
import sys
import threading
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "test_production.db"
APP_PORT = int(os.environ.get("OBOROT_TEST_PORT", "8841"))

os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["SCHEDULER_ENABLED"] = "0"

for suffix in ("", "-wal", "-shm"):
    p = Path(str(DB_PATH) + suffix)
    if p.exists():
        p.unlink()

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from app.main import app as oborot_app  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        PASS.append(name)
        print(f"  OK   {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL {name}  {detail}")


def sql(query: str, *args):
    con = sqlite3.connect(DB_PATH)
    try:
        cur = con.execute(query, args)
        con.commit()
        return cur.fetchall()
    finally:
        con.close()


def client() -> httpx.Client:
    return httpx.Client(headers={"X-Oborot-CSRF": "1"},
                        base_url=f"http://127.0.0.1:{APP_PORT}", timeout=120.0)


class ServerThread:
    def __init__(self, asgi_app, port: int):
        self.config = uvicorn.Config(asgi_app, host="127.0.0.1", port=port, log_level="warning")
        self.server = uvicorn.Server(self.config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self):
        self.thread.start()
        deadline = time.time() + 20
        while time.time() < deadline:
            if self.server.started:
                return
            time.sleep(0.05)
        raise RuntimeError("сервер не поднялся")

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=10)


TODAY = date.today()


def iso(d: date) -> str:
    return d.isoformat()


def register(c: httpx.Client, email: str, org: str) -> None:
    r = c.post("/register", data={"name": "Владелец", "email": email,
                                  "password": "secret123", "org_name": org})
    assert r.status_code in (200, 302, 303), r.text[:200]
    assert c.post("/api/connect/demo").status_code == 200


def org_id_of(email: str) -> int:
    return sql("SELECT m.org_id FROM memberships m JOIN users u ON u.id=m.user_id "
               "WHERE u.email=?", email)[0][0]


def catalog(org_id: int) -> list[str]:
    return [r[0] for r in sql("SELECT DISTINCT base_name FROM products WHERE org_id=? "
                              "AND excluded=0 ORDER BY base_name LIMIT 24", org_id)]


def make_order(c, name, base, qty, production_id=None, eta=None, send=True):
    body = {"name": name, "items": [{"base_name": base, "qty": qty, "sizes": {}}],
            "allow_duplicate": True}
    if production_id:
        body["production_id"] = production_id
    if eta:
        body["eta_date"] = eta
    r = c.post("/api/orders", json=body)
    assert r.status_code == 200, r.text[:300]
    oid = r.json()["id"]
    if send:
        r = c.post(f"/api/orders/{oid}/status", json={"status": "sent"})
        assert r.status_code == 200, r.text[:300]
    return oid


def card(c, oid):
    b = c.get("/api/production/board").json()
    return next((o for o in b["orders"] if o["id"] == oid), None), b


def backdate_sent(oid: int, days: int) -> None:
    d = TODAY - timedelta(days=days)
    sql("UPDATE production_orders SET sent_at=?, created_at=? WHERE id=?",
        f"{iso(d)} 10:00:00", f"{iso(d)} 09:00:00", oid)


def main() -> int:
    srv = ServerThread(oborot_app, APP_PORT)
    srv.start()
    try:
        return run()
    finally:
        srv.stop()
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(DB_PATH) + suffix)
            if p.exists():
                p.unlink()


def run() -> int:  # noqa: C901 — сценарный набор
    c = client()
    register(c, "prod@test.io", "Бренд-П")
    org = org_id_of("prod@test.io")
    bases = catalog(org)
    check("в демо-каталоге есть позиции", len(bases) >= 14, str(bases))

    print("\n== Страница и меню ==")
    r = c.get("/production")
    check("/production открывается", r.status_code == 200, str(r.status_code))
    check("в меню «Производство», «Поставок» нет",
          'href="/production"' in r.text and "Поставки" not in r.text
          and 'href="/supply"' not in r.text)
    r = c.get("/supply", follow_redirects=False)
    check("/supply ведёт на /production", r.status_code in (301, 302, 307, 308)
          and r.headers.get("location") == "/production", f"{r.status_code} {r.headers.get('location')}")
    for page in ("/turnover", "/replenish", "/settings"):
        html = c.get(page).text
        check(f"на {page} пункт «Производство» вместо «Поставок»",
              'href="/production"' in html and 'href="/supply"' not in html)

    print("\n== Пустая доска и черновик ==")
    b = c.get("/api/production/board").json()
    check("пустая доска отдаётся", b["orders"] == [] and b["summary"]["in_work"] == 0, str(b)[:200])
    check("право отмечать у владельца", b.get("can_mark") is True)
    draft = make_order(c, "Черновик", bases[0], 5, send=False)
    b = c.get("/api/production/board").json()
    check("черновика на доске нет, но он посчитан", b["orders"] == [] and b["drafts"] == 1, str(b)[:200])

    print("\n== Цеха ==")
    r = c.post("/api/productions", json={"name": "Цех Москва", "preset": "fabric_sewing"})
    moscow = r.json()["id"]
    r = c.post("/api/productions", json={"name": "Китай", "preset": "turnkey"})
    china = r.json()["id"]
    b = c.get("/api/production/board").json()
    chs = {ch["id"]: ch for ch in b["channels"]}
    check("свой пошив по умолчанию: ткань, крой, пошив, сдано",
          [s["name"] for s in chs[moscow]["stages"]] == ["Ткань у цеха", "Крой", "Пошив", "Сдано цехом"],
          str(chs.get(moscow)))
    check("канал с одним этапом в условиях получает шаблон «под ключ»",
          chs[china]["preset"] == "turnkey"
          and [s["name"] for s in chs[china]["stages"]] == ["Производство", "Проверка", "Отправлено"],
          str(chs.get(china)))

    print("\n== Отправленный заказ сам на доске ==")
    o1 = make_order(c, "Брюки Джаз", bases[1], 58, production_id=moscow)
    x, b = card(c, o1)
    check("отправленный заказ в «Заказано»", x is not None and x["phase"] == "ordered", str(x)[:200])
    check("следующий этап первый по порядку", x and x["next"]["name"] == "Ткань у цеха", str(x and x["next"]))
    check("цех подписан", x and x["channel_name"] == "Цех Москва")

    print("\n== Отметки этапов ==")
    r = c.post(f"/api/production/orders/{o1}/marks", json={"stage_key": x["next"]["key"]})
    check("отметка одним вызовом", r.status_code == 200, r.text[:200])
    first_mark = r.json().get("id")
    r = c.post(f"/api/production/orders/{o1}/marks", json={"stage_key": x["next"]["key"]})
    check("повтор того же запроса не пишет дубль", r.status_code == 200 and r.json().get("repeat") is True,
          r.text[:200])
    n = sql("SELECT COUNT(*) FROM order_stage_marks WHERE order_id=?", o1)[0][0]
    check("в базе одна отметка", n == 1, str(n))
    r = c.post(f"/api/production/orders/{o1}/marks",
               json={"stage_key": x["next"]["key"], "at": iso(TODAY - timedelta(days=3))})
    check("тот же этап другой датой: 409 с подсказкой", r.status_code == 409 and "уже отмечен" in r.text,
          f"{r.status_code} {r.text[:200]}")
    x, _ = card(c, o1)
    check("запуск перешёл в «В работе»", x["phase"] == "work" and x["reached"] == 0, str(x)[:200])
    r = c.post(f"/api/production/orders/{o1}/marks",
               json={"stage_key": x["next"]["key"], "at": iso(TODAY + timedelta(days=5))})
    check("будущая дата: 422", r.status_code == 422, f"{r.status_code} {r.text[:200]}")
    r = c.post(f"/api/production/orders/{o1}/marks", json={"stage_key": "s99"})
    check("чужой этап: 422", r.status_code == 422, f"{r.status_code}")
    r = c.post(f"/api/production/orders/{o1}/marks", json={"stage_key": x["next"]["key"], "qty": 10})
    check("количество не на последнем этапе: 422", r.status_code == 422, f"{r.status_code} {r.text[:200]}")
    r = c.post(f"/api/production/orders/{o1}/marks",
               json={"stage_key": x["next"]["key"], "at": iso(TODAY - timedelta(days=1)), "note": "  крой  начали  "})
    check("крой вчерашним числом", r.status_code == 200, r.text[:200])
    r = c.post(f"/api/production/orders/{draft}/marks", json={"stage_key": "s1"})
    check("по черновику отмечать нельзя: 422", r.status_code == 422, f"{r.status_code}")

    print("\n== Сдача частями ==")
    keys = [s["key"] for s in chs[moscow]["stages"]]
    c.post(f"/api/production/orders/{o1}/marks", json={"stage_key": keys[2]})
    r = c.post(f"/api/production/orders/{o1}/marks", json={"stage_key": keys[3], "qty": 30})
    check("первая сдача 30 шт", r.status_code == 200, r.text[:200])
    r = c.post(f"/api/production/orders/{o1}/marks", json={"stage_key": keys[3], "qty": 20,
                                                          "note": "вторая сдача"})
    check("вторая сдача 20 шт", r.status_code == 200, r.text[:200])
    r = c.post(f"/api/production/orders/{o1}/marks", json={"stage_key": keys[3]})
    check("следующая сдача без количества: 422", r.status_code == 422, f"{r.status_code} {r.text[:200]}")
    r = c.post(f"/api/production/orders/{o1}/marks", json={"stage_key": keys[3], "qty": 2.5})
    check("дробные штуки: 422", r.status_code == 422, f"{r.status_code}")
    r = c.post(f"/api/production/orders/{o1}/marks", json={"stage_key": keys[3], "qty": True})
    check("логическое значение вместо штук: 422", r.status_code == 422, f"{r.status_code}")
    r = c.post(f"/api/production/orders/{o1}/marks", json={"stage_key": keys[3], "qty": 5, "at": "20261117"})
    check("дата не в формате ГГГГ-ММ-ДД: 422", r.status_code == 422, f"{r.status_code}")
    x, _ = card(c, o1)
    check("сдано 50, колонка «Сдано, едет»", x["handed_qty"] == 50 and x["phase"] == "shipped",
          f"{x['handed_qty']} {x['phase']}")
    check("можно отметить ещё сдачу", x["next"] and x["next"]["key"] == keys[3], str(x["next"]))

    print("\n== Приёмка ==")
    r = c.post(f"/api/orders/{o1}/receipts", json={"lines": [{"base_name": bases[1], "qty": 20}]})
    check("частичная приёмка существующей ручкой", r.status_code == 200, r.text[:200])
    x, _ = card(c, o1)
    check("пришло 20 из 58, запуск ещё не принят", x["received_qty"] == 20 and x["phase"] == "shipped"
          # Приёмка пишется по UTC сервера: около полуночи это может быть вчерашний день.
          and x["arrived_at"] in (iso(TODAY), iso(TODAY - timedelta(days=1))), str(x)[:300])
    r = c.post(f"/api/orders/{o1}/status", json={"status": "received",
                                                 "received": [{"base_name": bases[1], "qty": 38}]})
    check("приняли остаток и закрыли", r.status_code == 200, r.text[:200])
    x, b = card(c, o1)
    check("запуск в «Принято»", x and x["phase"] == "done" and x["received_qty"] == 58, str(x)[:300])
    r = c.post(f"/api/production/orders/{o1}/marks", json={"stage_key": keys[3], "qty": 8})
    check("по принятому можно дописать историю", r.status_code == 200, r.text[:200])

    print("\n== Отметки и «Едет» ==")
    o_e = make_order(c, "Проверка едет", bases[2], 9, production_id=moscow)
    before = sql("SELECT base_name, qty, ms_qty FROM ordered_qty WHERE org_id=? ORDER BY base_name", org)
    st = card(c, o_e)[0]["next"]["key"]
    c.post(f"/api/production/orders/{o_e}/marks", json={"stage_key": st})
    after = sql("SELECT base_name, qty, ms_qty FROM ordered_qty WHERE org_id=? ORDER BY base_name", org)
    check("отметка этапа не меняет «Едет»", before == after, f"{before[:3]} {after[:3]}")
    status = sql("SELECT status, eta_date FROM production_orders WHERE id=?", o_e)[0]
    check("и не меняет статус и плановую дату заказа", status == ("sent", None), str(status))

    print("\n== Срок и опоздание ==")
    sql("UPDATE productions SET lead_time_days=30, stages_json='' WHERE id=?", moscow)
    o2 = make_order(c, "По сроку цеха", bases[3], 10, production_id=moscow)
    backdate_sent(o2, 5)
    x, _ = card(c, o2)
    check("без плана и срока от цеха: отправка + срок цеха",
          x["forecast"]["basis"] == "lead" and x["forecast"]["date"] == iso(TODAY + timedelta(days=25))
          and not x["forecast"]["late_days"], str(x["forecast"]))
    plan = iso(TODAY + timedelta(days=12))
    o3 = make_order(c, "По плану", bases[4], 10, production_id=china, eta=plan)
    x, _ = card(c, o3)
    check("с плановой датой: план", x["forecast"]["basis"] == "plan" and x["forecast"]["date"] == plan,
          str(x["forecast"]))
    late_plan = iso(TODAY - timedelta(days=4))
    o4 = make_order(c, "Опаздывает", bases[5], 10, production_id=china, eta=late_plan)
    x, b = card(c, o4)
    check("план прошёл, прихода нет: опаздывает на 4 дн", x["forecast"]["late_days"] == 4, str(x["forecast"]))
    check("опоздание в сводке", b["summary"]["late"] >= 1, str(b["summary"]))
    check("опоздавший не входит в «ждём за 14 дней»",
          b["summary"]["waiting_qty"] == sum(max(0, o["total_qty"] - o["received_qty"]) for o in b["orders"]
                                             if o["phase"] != "done" and not o["forecast"]["late_days"]
                                             and o["forecast"]["date"]
                                             and iso(TODAY) <= o["forecast"]["date"] <= iso(TODAY + timedelta(days=14))),
          str(b["summary"]))

    print("\n== Срок от цеха ==")
    new_due = iso(TODAY + timedelta(days=6))
    r = c.post(f"/api/production/orders/{o4}/due", json={"due": new_due, "note": "ждут фурнитуру"})
    check("цех назвал новый срок", r.status_code == 200, r.text[:200])
    x, _ = card(c, o4)
    check("опоздания больше нет, действует срок от цеха",
          not x["forecast"]["late_days"] and x["forecast"]["basis"] == "due"
          and x["forecast"]["date"] == new_due and x["forecast"]["plan_date"] == late_plan, str(x["forecast"]))
    r = c.post(f"/api/production/orders/{o4}/due", json={"due": new_due, "note": "ждут фурнитуру"})
    check("повтор того же срока не пишет второй строки", r.json().get("repeat") is True, r.text[:200])
    r2 = c.post(f"/api/production/orders/{o4}/due", json={"due": iso(TODAY - timedelta(days=1))})
    x, _ = card(c, o4)
    check("перенос: последний срок действует, переносов 1, снова опаздывает",
          x["forecast"]["due_moves"] == 1 and x["forecast"]["late_days"] == 1, str(x["forecast"]))
    d = c.get(f"/api/production/orders/{o4}").json()
    check("история сроков в окне", [z["due"] for z in d["dues"]] == [new_due, iso(TODAY - timedelta(days=1))]
          and d["dues"][0]["note"] == "ждут фурнитуру", str(d.get("dues")))
    r = c.delete(f"/api/production/due/{r2.json()['id']}")
    x, _ = card(c, o4)
    check("удалённый срок: снова действует предыдущий", r.status_code == 200
          and x["forecast"]["date"] == new_due, str(x["forecast"]))
    r = c.post(f"/api/production/orders/{o4}/due", json={"due": iso(TODAY - timedelta(days=400))})
    check("срок раньше отправки: 422", r.status_code == 422, str(r.status_code))
    r = c.post(f"/api/production/orders/{o4}/due", json={"due": "20261201"})
    check("срок не в формате ГГГГ-ММ-ДД: 422", r.status_code == 422, str(r.status_code))
    status = sql("SELECT eta_date FROM production_orders WHERE id=?", o4)[0][0]
    check("срок от цеха не трогает плановую дату заказа", status == late_plan, str(status))

    print("\n== Оценка по истории цеха ==")
    # История Китая: два закрытых заказа, «Отправлено» за 10 и 14 дней до прихода,
    # оба пришли больше 30 дней назад (на доске их уже нет, в истории есть).
    ch_keys = [s["key"] for s in chs[china]["stages"]]
    for i, (sent_ago, shipped_ago, arrived_ago) in enumerate(((90, 50, 40), (80, 49, 35))):
        oid = make_order(c, f"Китай история {i}", bases[6 + i], 5, production_id=china)
        backdate_sent(oid, sent_ago)
        r = c.post(f"/api/production/orders/{oid}/marks",
                   json={"stage_key": ch_keys[2], "at": iso(TODAY - timedelta(days=shipped_ago))})
        assert r.status_code == 200, r.text
        c.post(f"/api/orders/{oid}/status", json={"status": "received",
                                                  "received": [{"base_name": bases[6 + i], "qty": 5}]})
        arrived = TODAY - timedelta(days=arrived_ago)
        sql("UPDATE order_receipts SET at=? WHERE order_id=?", f"{iso(arrived)} 12:00:00", oid)
        sql("UPDATE production_orders SET received_at=? WHERE id=?", f"{iso(arrived)} 12:00:00", oid)
    o5 = make_order(c, "Китай едет", bases[8], 7, production_id=china, eta=iso(TODAY + timedelta(days=3)))
    c.post(f"/api/production/orders/{o5}/marks",
           json={"stage_key": ch_keys[2], "at": iso(TODAY - timedelta(days=2))})
    x, b = card(c, o5)
    # медиана (10, 14) = 12; отправлено 2 дня назад, значит через 10 дней.
    est = x["forecast"]["estimate"]
    check("оценка по истории: точка + медиана",
          est and est["n"] == 2 and est["date"] == iso(TODAY + timedelta(days=10)), str(x["forecast"]))
    check("срок остаётся плановым, оценка позже срока на 7 дн, но это не опоздание",
          x["forecast"]["basis"] == "plan" and x["forecast"]["risk_days"] == 7
          and not x["forecast"]["late_days"], str(x["forecast"]))
    o5b = make_order(c, "Китай без отметок", bases[11], 3, production_id=china)
    x, _ = card(c, o5b)
    c.post(f"/api/production/orders/{o5b}/marks", json={"stage_key": ch_keys[0]})
    y, _ = card(c, o5b)
    check("отметка этапа без истории не теряет оценку по прошлой точке",
          x["forecast"]["estimate"] and y["forecast"]["estimate"]
          and y["forecast"]["estimate"]["date"] == x["forecast"]["estimate"]["date"],
          f"{x['forecast'].get('estimate')} {y['forecast'].get('estimate')}")
    sp = next(s for s in b["speed"] if s["channel_id"] == china)
    check("скорость цеха: два пришедших, медиана от «Отправлено» 12 дн",
          sp["done"] == 2 and next(s for s in sp["stages"] if s["key"] == ch_keys[2])["median_to_arrival"] == 12,
          str(sp))
    check("старые принятые не на доске, но в истории",
          not any(o["name"].startswith("Китай история") for o in b["orders"]), "")

    print("\n== Приход построчно ==")
    two = c.post("/api/orders", json={"name": "Две позиции", "production_id": moscow, "allow_duplicate": True,
                                      "items": [{"base_name": bases[12], "qty": 10, "sizes": {}},
                                                {"base_name": bases[13], "qty": 10, "sizes": {}}]}).json()["id"]
    c.post(f"/api/orders/{two}/status", json={"status": "sent"})
    c.post(f"/api/orders/{two}/receipts", json={"lines": [{"base_name": bases[12], "qty": 20},
                                                         {"base_name": "Не из заказа", "qty": 10}]})
    x, _ = card(c, two)
    check("перебор по одной позиции и чужая позиция не закрывают недостачу по другой",
          x["phase"] == "shipped" and x["received_qty"] == 10, f"{x['phase']} {x['received_qty']}")

    print("\n== Закрыт без записанного прихода ==")
    ivan = c.post("/api/productions", json={"name": "Цех без истории"}).json()["id"]
    o9 = make_order(c, "Закрыт без количеств", bases[15], 21, production_id=ivan,
                    eta=iso(TODAY + timedelta(days=5)))
    r = c.post(f"/api/orders/{o9}/status", json={"status": "received"})
    x, b = card(c, o9)
    check("закрытый без количеств: принят, пришло 0, дата закрытия есть",
          r.status_code == 200 and x and x["phase"] == "done" and x["received_qty"] == 0
          and x["closed"] is True and x["closed_at"] and x["arrived_at"] is None, str(x)[:300])
    sp = next((s_ for s_ in b["speed"] if s_["channel_id"] == ivan), None)
    check("в историю и «вовремя» такой заказ не идёт (даты прихода нет)",
          sp is not None and sp["done"] == 0 and sp["on_time_share"] is None, str(sp))
    o10 = make_order(c, "Срок записан после прихода", bases[16], 2, production_id=ivan)
    backdate_sent(o10, 10)
    c.post(f"/api/orders/{o10}/receipts", json={"lines": [{"base_name": bases[16], "qty": 2}]})
    sql("UPDATE order_receipts SET at=? WHERE order_id=?", f"{iso(TODAY - timedelta(days=3))} 12:00:00", o10)
    r = c.post(f"/api/production/orders/{o10}/due", json={"due": iso(TODAY - timedelta(days=2))})
    assert r.status_code == 200, r.text
    _, b = card(c, o10)
    sp = next((s_ for s_ in b["speed"] if s_["channel_id"] == ivan), None)
    check("срок, записанный после прихода, долю «вовремя» не делает",
          sp is not None and sp["done"] == 1 and sp["on_time_share"] is None, str(sp))

    print("\n== Этапы цеха ==")
    stages = [{"key": s["key"], "name": s["name"]} for s in chs[moscow]["stages"]]
    stages[1]["name"] = "Раскрой"
    r = c.put("/api/production/stages", json={"production_id": moscow, "preset": "custom", "stages": stages})
    check("переименование этапа", r.status_code == 200, r.text[:200])
    x, _ = card(c, o_e)
    check("переименование не потеряло отметку", x["reached"] == 0, str(x["stages"]))
    r = c.put("/api/production/stages", json={"production_id": moscow, "preset": "custom",
                                              "stages": stages[1:]})
    check("убрать этап с отметками нельзя: 409", r.status_code == 409 and "Ткань у цеха" in r.text,
          f"{r.status_code} {r.text[:200]}")
    r = c.put("/api/production/stages", json={"production_id": moscow, "preset": "custom",
                                              "stages": stages + [{"name": "ОТК"}]})
    check("добавить этап в конец", r.status_code == 200 and r.json()["stages"][-1]["name"] == "ОТК",
          r.text[:200])
    r = c.put("/api/production/stages", json={"production_id": moscow, "preset": "custom",
                                              "stages": [{"name": "А"}, {"name": "а"}]})
    check("повтор названия: 422", r.status_code == 422, str(r.status_code))
    r = c.put("/api/production/stages", json={"production_id": moscow, "preset": "custom",
                                              "stages": [{"name": f"Э{i}"} for i in range(9)]})
    check("больше 8 этапов: 422", r.status_code == 422, str(r.status_code))
    r = c.put("/api/production/stages", json={"production_id": moscow, "preset": "custom", "stages": []})
    check("без этапов: 422", r.status_code == 422, str(r.status_code))
    r = c.put("/api/production/stages", json={"production_id": 0, "preset": "turnkey", "stages": []})
    check("шаблон для заказов без цеха", r.status_code == 200
          and [s["name"] for s in r.json()["stages"]] == ["Производство", "Проверка", "Отправлено"], r.text[:200])

    print("\n== Отмена отметки ==")
    r = c.delete(f"/api/production/marks/{first_mark}")
    check("отметку можно отменить", r.status_code == 200, r.text[:200])
    r = c.delete(f"/api/production/marks/{first_mark}")
    check("повторная отмена: 404", r.status_code == 404, str(r.status_code))

    print("\n== Чужая организация ==")
    o = client()
    register(o, "other@test.io", "Чужой бренд")
    ob = o.get("/api/production/board").json()
    check("чужая доска пуста", ob["orders"] == [], str(ob["orders"])[:100])
    r = o.get(f"/api/production/orders/{o_e}")
    check("чужой заказ не читается: 404", r.status_code == 404, str(r.status_code))
    r = o.post(f"/api/production/orders/{o_e}/marks", json={"stage_key": "s2"})
    check("по чужому заказу отметить нельзя: 404", r.status_code == 404, str(r.status_code))
    mid = sql("SELECT id FROM order_stage_marks WHERE order_id=? LIMIT 1", o_e)[0][0]
    r = o.delete(f"/api/production/marks/{mid}")
    check("чужую отметку отменить нельзя: 404", r.status_code == 404, str(r.status_code))
    r = o.put("/api/production/stages", json={"production_id": moscow, "preset": "turnkey", "stages": []})
    check("чужой цех: 404", r.status_code == 404, str(r.status_code))
    check("чужая отметка на месте", sql("SELECT COUNT(*) FROM order_stage_marks WHERE id=?", mid)[0][0] == 1)

    print("\n== Наблюдатель ==")
    viewer_role = sql("SELECT id FROM org_roles WHERE org_id=? AND builtin='viewer'", org)
    if not viewer_role:
        c.get("/api/team")  # роли создаются лениво
        viewer_role = sql("SELECT id FROM org_roles WHERE org_id=? AND builtin='viewer'", org)
    if viewer_role:
        from app import auth as _auth
        con = sqlite3.connect(DB_PATH)
        cur = con.execute("INSERT INTO users (email, pw_hash, name, created_at, session_version) "
                          "VALUES (?,?,?,datetime('now'),1)",
                          ("viewer@test.io", _auth.hash_password("secret123"), "Наблюдатель"))
        con.execute("INSERT INTO memberships (user_id, org_id, role, role_id) VALUES (?,?,?,?)",
                    (cur.lastrowid, org, "member", viewer_role[0][0]))
        con.commit()
        con.close()
        v = client()
        r = v.post("/login", data={"email": "viewer@test.io", "password": "secret123"},
                   follow_redirects=False)
        check("наблюдатель вошёл", r.status_code == 303, str(r.status_code))
        vb = v.get("/api/production/board").json()
        check("наблюдатель видит доску без права отмечать",
              any(x["id"] == o_e for x in vb.get("orders", [])) and vb.get("can_mark") is False,
              str(vb)[:200])
        r = v.post(f"/api/production/orders/{o_e}/marks", json={"stage_key": "s2"})
        check("наблюдатель отметить не может: 403", r.status_code == 403, str(r.status_code))
        r = v.put("/api/production/stages", json={"production_id": moscow, "preset": "turnkey", "stages": []})
        check("наблюдатель этапы цеха не меняет: 403", r.status_code == 403, str(r.status_code))
        html = v.get("/production").text
        check("у наблюдателя нет права на странице", 'data-can-mark="0"' in html)
    else:
        check("роль «Наблюдатель» найдена", False, "нет builtin viewer")

    print("\n== Переиспользованный id заказа ==")
    o6 = make_order(c, "Будет удалён", bases[9], 4, production_id=moscow)
    c.post(f"/api/production/orders/{o6}/marks", json={"stage_key": keys[0]})
    r = c.delete(f"/api/orders/{o6}")
    check("заказ удалён", r.status_code == 200, r.text[:200])
    o7 = make_order(c, "Новый", bases[10], 4, production_id=moscow)
    x, _ = card(c, o7)
    check("id действительно переиспользован (иначе проверка ниже ничего не доказывает)", o7 == o6,
          f"{o6} -> {o7}")
    check("новый заказ с тем же id не унаследовал отметок", x and x["reached"] == -1
          and x["phase"] == "ordered", str(x and x["stages"]))

    print("\n== Замок от одновременных отметок ==")
    row = sql("SELECT org_id, order_id, order_batch, stage_key FROM order_stage_marks "
              "WHERE qty IS NULL LIMIT 1")[0]
    dup_err = ""
    try:
        sql("INSERT INTO order_stage_marks (org_id, order_id, order_batch, stage_key, stage_name, at, "
            "note, created_at) VALUES (?,?,?,?,'x','2026-01-01','',datetime('now'))", *row)
    except sqlite3.IntegrityError as exc:
        dup_err = str(exc)
    check("второй такой же этап без количества база не принимает", "UNIQUE" in dup_err, dup_err)

    print("\n== Удалённый цех ==")
    tmp = c.post("/api/productions", json={"name": "Временный цех"}).json()["id"]
    o8 = make_order(c, "У временного цеха", bases[14], 3, production_id=tmp)
    k0 = card(c, o8)[0]["next"]["key"]
    c.post(f"/api/production/orders/{o8}/marks", json={"stage_key": k0})
    r = c.delete(f"/api/productions/{tmp}")
    x, _ = card(c, o8)
    check("заказ удалённого цеха относится к «Без цеха»", r.status_code == 200 and x["channel_id"] == 0
          and x["reached"] == 0, f"{r.status_code} {x['channel_id']} {x['reached']}")
    zero = c.get("/api/production/board").json()
    st0 = next(ch for ch in zero["channels"] if ch["id"] == 0)["stages"]
    r = c.put("/api/production/stages", json={"production_id": 0, "preset": "custom",
                                              "stages": [s_ for s_ in st0 if s_["key"] != k0]})
    check("убрать этап с отметками у «Без цеха» нельзя и после удаления цеха: 409",
          r.status_code == 409, f"{r.status_code} {r.text[:150]}")

    print("\n== Окно запуска ==")
    d = c.get(f"/api/production/orders/{o1}").json()
    check("в окне история отметок с автором и заметкой",
          len(d["marks"]) >= 4 and any(m["note"] == "вторая сдача" for m in d["marks"])
          and all(m["by"] for m in d["marks"]), str(d.get("marks"))[:300])
    check("заметка очищена от лишних пробелов",
          any(m["note"] == "крой начали" for m in d["marks"]), str([m["note"] for m in d["marks"]]))

    print("\n== Удаление организации ==")
    from app import tenancy
    names = {m.__tablename__ for m in tenancy.org_purge_models()} if hasattr(tenancy, "org_purge_models") else set()
    if not names:
        names = {m.__tablename__ for m in tenancy.purge_models()} if hasattr(tenancy, "purge_models") else set()
    check("отметки и этапы входят в удаление организации",
          {"order_stage_marks", "production_tracks", "order_due_dates"} <= names, str(sorted(names))[:300])

    print(f"\nИТОГО: {len(PASS)} OK, {len(FAIL)} FAIL")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
