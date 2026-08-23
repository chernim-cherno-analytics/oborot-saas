# -*- coding: utf-8 -*-
"""Исполнение заказа: рекомендация → решение человека → факт (D-25).

Зачем этот набор. Из трёх величин, которые владелец потребовал сделать
различимыми, третьей не существовало вовсе: у заказа были статусы `draft →
sent → received`, но ни дат переходов, ни принятого количества. «Сколько
реально пришло» в системе не было как понятия — значит, качество собственных
рекомендаций «Оборот» не мог оценить даже задним числом.

Отдельно проверяется то, ради чего заведена колонка `source`. Диагностика
ночного синка 23.08 на боевых данных дала решающий факт: из 69 позиций в трёх
открытых «Заказах поставщику» поле «отгружено» заполнено у НУЛЯ — приёмки
заводят отдельными документами, а не «на основании» заказа. Значит,
автоматический источник у этого клиента покрывает 0 %, и ручной путь — не
запасной, а основной. «Пришло 80» и «человек сказал, что пришло 80» обязаны
быть различимы, иначе статистика качества рекомендаций будет считать
допущения фактами.

Проверяется:
  1) даты переходов пишутся, фактический срок производства считается;
  2) обратная ссылка заказ → план (у плана ссылка на заказ уже была);
  3) отметка «принят» без деталей = допущение (precision=whole_order),
     с деталями = подтверждение (by_position) — и они различимы в выдаче;
  4) таблица приёмок ТОЛЬКО пополняется: исправление — компенсирующая строка
     с минусом, обе остаются в истории;
  5) частичный приход и довоз складываются, а не перезаписываются;
  6) позиция, которой в заказе не было, из приёмки не теряется;
  7) `/api/order-plan/{id}/outcome` отдаёт три величины, включая обнулённые
     человеком позиции, и `executed = null`, пока заказ не принят;
  8) приёмки чужой организации недоступны, приёмка по чужому заказу не пишется;
  9) при удалении организации приёмки не остаются осиротевшими;
 10) приёмку нельзя завести по заказу, который ещё не отправлен.

Запуск из корня репозитория:  python tests/test_execution.py
"""
import json
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "test_execution.db"
APP_PORT = int(os.environ.get("OBOROT_TEST_PORT", "8812"))

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


PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        PASS.append(name)
        print(f"  OK   {name}" + (f"  [{detail}]" if detail else ""))
    else:
        FAIL.append(name)
        print(f"  FAIL {name}  {detail}")


def _raw_sql(query: str, *args):
    """Пишет в базу напрямую — нужно, чтобы воспроизвести строку в том виде,
    в каком её создавали ПРЕЖНИЕ версии кода: через API такую уже не записать."""
    con = sqlite3.connect(DB_PATH)
    try:
        con.execute(query, args)
        con.commit()
    finally:
        con.close()


def sql(query: str, *args):
    con = sqlite3.connect(DB_PATH)
    try:
        return con.execute(query, args).fetchall()
    finally:
        con.close()


def client() -> httpx.Client:
    return httpx.Client(headers={"X-Oborot-CSRF": "1"},
                        base_url=f"http://127.0.0.1:{APP_PORT}", timeout=120.0)


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


def run() -> int:  # noqa: C901 — сценарный тест, ветвлений мало, шагов много
    c = client()

    print("\n== Подготовка ==")
    r = c.post("/register", data={"name": "Владелец", "email": "exec@test.io",
                                  "password": "secret123", "org_name": "Бренд-Ф"})
    check("регистрация", r.status_code in (200, 302, 303), f"status={r.status_code}")
    check("демо-данные", c.post("/api/connect/demo").status_code == 200)

    r = c.post("/api/order-plan", json={"budget": 150000, "budget_scope": "now",
                                        "cadence_days": 30, "safety_days": 14})
    check("план посчитан и сохранён", r.status_code == 200, r.text[:120])
    plan_id = r.json().get("id")

    r = c.post(f"/api/order-plan/{plan_id}/apply", json={"name": "Заказ №1"})
    check("заказ создан из плана", r.status_code == 200, r.text[:150])
    order_id = r.json().get("order_id")

    print("\n== Три величины: пока не приняли — исполнения нет ==")
    out0 = c.get(f"/api/order-plan/{plan_id}/outcome").json()
    check("исполнение не подтверждено", out0.get("execution_confirmed") is False,
          str(out0.get("execution_confirmed")))
    check("исполнение = null, а не ноль (это разные утверждения)",
          all(x["executed"] is None for x in out0["lines"]),
          str(out0["lines"][:1]))
    check("итог исполнения тоже null", out0["totals"]["executed"] is None,
          str(out0["totals"]))
    check("рекомендация есть у КАЖДОЙ строки, а не только у правленых",
          all(x["recommended"] is not None for x in out0["lines"]),
          str([x for x in out0["lines"] if x["recommended"] is None][:2]))
    check("до правок рекомендация и решение совпадают",
          out0.get("edited_by_human") == 0, str(out0.get("edited_by_human")))

    print("\n== Обратная ссылка и даты переходов ==")
    r = c.get(f"/api/orders/{order_id}")
    body = r.json()
    check("заказ помнит план, из которого вырос", body.get("order_plan_id") == plan_id,
          str(body.get("order_plan_id")))
    check("у черновика дат переходов нет",
          body.get("sent_at") is None and body.get("received_at") is None, str(body)[:120])
    check("фактический срок производства пока неизвестен",
          body.get("lead_time_fact_days") is None)

    ordered = {i["base_name"]: float(i["qty"]) for i in body.get("items") or []}
    check("в заказе есть позиции", len(ordered) > 0, f"{len(ordered)}")
    first = sorted(ordered)[0]

    print("\n== Приёмка по неотправленному заказу невозможна ==")
    r = c.post(f"/api/orders/{order_id}/receipts",
               json={"lines": [{"base_name": first, "qty": 1}]})
    check("по черновику принимать нечего — 422", r.status_code == 422,
          f"status={r.status_code}")

    r = c.post(f"/api/orders/{order_id}/status", json={"status": "sent"})
    check("заказ отправлен в производство", r.status_code == 200, r.text[:120])
    body = c.get(f"/api/orders/{order_id}").json()
    check("дата отправки записана", bool(body.get("sent_at")), str(body.get("sent_at")))
    check("дата приёмки пока пуста", body.get("received_at") is None)

    print("\n== Частичный приход: строки складываются, а не перезаписываются ==")
    r = c.post(f"/api/orders/{order_id}/receipts",
               json={"lines": [{"base_name": first, "qty": 3}]})
    check("первая приёмка записана", r.status_code == 200, r.text[:150])
    r = c.post(f"/api/orders/{order_id}/receipts",
               json={"lines": [{"base_name": first, "qty": 2}]})
    check("довоз записан отдельной строкой", r.status_code == 200, r.text[:150])
    got = c.get(f"/api/orders/{order_id}/receipts").json()
    line = next((x for x in got["lines"] if x["base_name"] == first), None)
    check("принятое сложилось (3 + 2 = 5)", line and line["received_qty"] == 5,
          str(line))
    check("в истории две строки, а не одна",
          len([x for x in got["receipts"] if x["base_name"] == first]) == 2,
          str(len(got["receipts"])))
    check("источник ручной", got["sources"] == ["manual"], str(got["sources"]))
    check("точность — по строкам", got["precisions"] == ["by_position"],
          str(got["precisions"]))

    print("\n== Исправление ошибки — компенсирующей строкой, а не правкой ==")
    r = c.post(f"/api/orders/{order_id}/receipts",
               json={"lines": [{"base_name": first, "qty": -2}]})
    check("минусовая строка принята", r.status_code == 200, r.text[:150])
    got = c.get(f"/api/orders/{order_id}/receipts").json()
    line = next((x for x in got["lines"] if x["base_name"] == first), None)
    check("итог уменьшился до 3", line and line["received_qty"] == 3, str(line))
    check("все три строки остались видны в истории",
          len([x for x in got["receipts"] if x["base_name"] == first]) == 3,
          str(len(got["receipts"])))

    print("\n== Позиция, которой в заказе не было ==")
    r = c.post(f"/api/orders/{order_id}/receipts",
               json={"lines": [{"base_name": "Подрядчик прислал не то", "qty": 7}]})
    check("приёмка чужой позиции записана", r.status_code == 200, r.text[:150])
    got = c.get(f"/api/orders/{order_id}/receipts").json()
    extra = next((x for x in got["lines"] if x["base_name"] == "Подрядчик прислал не то"), None)
    check("она видна в сверке отдельной строкой", extra is not None, str(extra))
    check("и помечена как не заказанная",
          extra and extra["ordered_qty"] == 0 and extra["received_qty"] == 7, str(extra))

    print("\n== Три величины до приёмки заказа ==")
    out = c.get(f"/api/order-plan/{plan_id}/outcome").json()
    check("outcome отдаёт строки", out.get("positions", 0) > 0, str(out)[:160])
    check("outcome знает заказ", out.get("order_id") == order_id)
    check("строки плана есть", len(out["lines"]) > 0, str(len(out["lines"])))
    check("исполнение подтверждено — приёмки записаны вручную",
          out.get("execution_confirmed") is True, str(out.get("execution_confirmed")))
    # Приёмка была записана по ОДНОЙ позиции (и по одной, которой в заказе нет).
    # Остальные позиции ещё едут: у них исполнение обязано быть неизвестно,
    # а не нулём. Раньше одна частичная приёмка обнуляла ВСЕ строки, и цифры
    # на середине пути выглядели итоговыми.
    known = [x for x in out["lines"] if x["executed"] is not None]
    unknown = [x for x in out["lines"] if x["executed"] is None]
    check("по принятой позиции исполнение известно", len(known) >= 1, str(known[:1]))
    check("по остальным — неизвестно, а не ноль", len(unknown) >= 1,
          str([x["base_name"] for x in out["lines"]][:3]))
    check("итог исполнения не показывается, пока известны не все строки",
          out["totals"]["executed"] is None, str(out["totals"]))

    print("\n== Обнулённая человеком позиция остаётся в трёх величинах ==")
    prev = c.post("/api/order-plan/preview", json={
        "budget": 150000, "budget_scope": "now",
        "cadence_days": 30, "safety_days": 14}).json()
    plan_items = (prev.get("plan") or prev).get("items") or []
    victim = str((plan_items[0] if plan_items else {}).get("base_name") or "")
    check("позиция для обнуления найдена", bool(victim), str(len(plan_items)))
    r = c.post("/api/order-plan", json={
        "budget": 150000, "budget_scope": "now", "cadence_days": 30,
        "safety_days": 14, "overrides": {victim: 0}})
    check("план с обнулённой позицией сохранён", r.status_code == 200, r.text[:150])
    plan2 = r.json().get("id")
    out2 = c.get(f"/api/order-plan/{plan2}/outcome").json()
    zeroed = next((x for x in out2["lines"] if x["base_name"] == victim), None)
    check("обнулённая позиция не исчезла из истории", zeroed is not None, str(victim))
    check("видно, что система советовала не ноль",
          zeroed and (zeroed["recommended"] or 0) > 0, str(zeroed))
    check("а человек решил ноль", zeroed and zeroed["decided"] == 0, str(zeroed))
    check("правка человека посчитана", out2.get("edited_by_human", 0) >= 1,
          str(out2.get("edited_by_human")))
    check("у плана без заказа исполнения нет",
          out2.get("order_id") is None and out2["totals"]["executed"] is None,
          str(out2.get("order_id")))

    print("\n== Отметка «принят» без деталей — это допущение, а не подтверждение ==")
    c2 = client()
    c2.post("/register", data={"name": "Второй", "email": "exec2@test.io",
                               "password": "secret123", "org_name": "Бренд-Ц"})
    c2.post("/api/connect/demo")
    # Имя берём из собственного каталога организации: заказ на позицию,
    # которой в каталоге нет, система намеренно не принимает.
    rows = c2.get("/api/turnover").json().get("items") or []
    name2 = str((rows[0] if rows else {}).get("base_name") or "")
    check("имя позиции из каталога получено", bool(name2), str(rows)[:120])
    r = c2.post("/api/orders", json={"name": "Ручной заказ", "items": [
        {"base_name": name2, "qty": 10, "sizes": {}, "cost": 100},
    ]})
    check("заказ второй организации создан", r.status_code == 200, r.text[:150])
    order2 = r.json().get("id")
    c2.post(f"/api/orders/{order2}/status", json={"status": "sent"})
    r = c2.post(f"/api/orders/{order2}/status", json={"status": "received"})
    check("заказ принят одним кликом", r.status_code == 200, r.text[:150])
    got2 = c2.get(f"/api/orders/{order2}/receipts").json()
    check("никакого количества не выдумано", got2["received_total"] is None,
          str(got2["received_total"]))
    check("и выдача говорит, что принятое неизвестно",
          got2.get("execution_unknown") is True, str(got2.get("execution_unknown")))
    check("исполнение НЕ считается подтверждённым",
          got2.get("confirmed") is False, str(got2.get("confirmed")))
    body2 = c2.get(f"/api/orders/{order2}").json()
    check("дата приёмки записана", bool(body2.get("received_at")))
    check("фактический срок производства посчитан",
          body2.get("lead_time_fact_days") == 0, str(body2.get("lead_time_fact_days")))

    print("\n== Отметка «принят» С деталями — подтверждение ==")
    r = c2.post("/api/orders", json={"name": "Второй заказ", "items": [
        {"base_name": name2, "qty": 10, "sizes": {}, "cost": 100},
    ]})
    order3 = r.json().get("id")
    c2.post(f"/api/orders/{order3}/status", json={"status": "sent"})
    r = c2.post(f"/api/orders/{order3}/status", json={
        "status": "received", "received": [{"base_name": name2, "qty": 7}]})
    check("принят с указанием фактического количества", r.status_code == 200, r.text[:150])
    got3 = c2.get(f"/api/orders/{order3}/receipts").json()
    check("записано 7, а не 10", got3["received_total"] == 7, str(got3["received_total"]))
    check("это подтверждение по строкам", got3["precisions"] == ["by_position"],
          str(got3["precisions"]))
    line3 = got3["lines"][0]
    check("недостача видна в сверке", line3["diff"] == -3, str(line3))

    print("\n== Приёмки не протекают между организациями ==")
    r = c2.get(f"/api/orders/{order_id}/receipts")
    check("чужой заказ не виден — 404", r.status_code == 404, f"status={r.status_code}")
    r = c2.post(f"/api/orders/{order_id}/receipts",
                json={"lines": [{"base_name": first, "qty": 100}]})
    check("в чужой заказ нельзя записать приёмку — 404", r.status_code == 404,
          f"status={r.status_code}")
    check("чужие строки не появились",
          len(c.get(f"/api/orders/{order_id}/receipts").json()["receipts"]) == 4,
          str(len(c.get(f"/api/orders/{order_id}/receipts").json()["receipts"])))
    r = c2.get(f"/api/order-plan/{plan_id}/outcome")
    check("чужой план в outcome не отдаётся — 404", r.status_code == 404,
          f"status={r.status_code}")

    print("\n== Машинный источник: дельта, а не дубль ==")
    from app.ms_sync import _write_shipped_receipts

    added = _write_shipped_receipts(1, {(order_id, "https://ms/doc/1"): {first: 4.0}})
    check("первый прогон записал строку", added == 1, str(added))
    added = _write_shipped_receipts(1, {(order_id, "https://ms/doc/1"): {first: 4.0}})
    check("повторный синк с тем же «отгружено» ничего не пишет", added == 0, str(added))
    added = _write_shipped_receipts(1, {(order_id, "https://ms/doc/1"): {first: 6.0}})
    check("выросло «отгружено» — записана только разница", added == 1, str(added))
    rows = sql("SELECT qty FROM order_receipts WHERE order_id=? AND source='ms_order_shipped'"
               " ORDER BY id", order_id)
    check("в истории две строки: 4 и 2", [float(x[0]) for x in rows] == [4.0, 2.0], str(rows))
    added = _write_shipped_receipts(1, {(order_id, "https://ms/doc/1"): {first: 1.0}})
    check("уменьшение записано компенсирующей строкой", added == 1, str(added))
    rows = sql("SELECT qty FROM order_receipts WHERE order_id=? AND source='ms_order_shipped'"
               " ORDER BY id", order_id)
    check("минус виден в истории", [float(x[0]) for x in rows] == [4.0, 2.0, -5.0], str(rows))
    got = c.get(f"/api/orders/{order_id}/receipts").json()
    check("оба источника различимы в выдаче",
          got["sources"] == ["manual", "ms_order_shipped"], str(got["sources"]))

    added = _write_shipped_receipts(999, {(order_id, "https://ms/doc/1"): {first: 50.0}})
    check("чужая организация не может дописать приёмку по нашему заказу",
          added == 0, str(added))
    added = _write_shipped_receipts(1, {(7654321, "https://ms/doc/9"): {first: 5.0}})
    check("несуществующий заказ пропускается молча", added == 0, str(added))

    print("\n== Дефекты, найденные ревью: двойной счёт и потеря фактов ==")
    c3 = client()
    c3.post("/register", data={"name": "Третий", "email": "exec3@test.io",
                              "password": "secret123", "org_name": "Бренд-Э"})
    c3.post("/api/connect/demo")
    rows3 = c3.get("/api/turnover").json().get("items") or []
    name3 = str((rows3[0] if rows3 else {}).get("base_name") or "")

    def make_order(cl, qty=10, name="Заказ", pushed=False):
        r = cl.post("/api/orders", json={"name": name, "items": [
            {"base_name": name3, "qty": qty, "sizes": {}, "cost": 100}]})
        oid = r.json().get("id")
        if pushed:
            exec_sql_local(oid)
        cl.post(f"/api/orders/{oid}/status", json={"status": "sent"})
        return oid

    def exec_sql_local(oid):
        con = sqlite3.connect(DB_PATH)
        try:
            con.execute("UPDATE production_orders SET ms_doc_href=? WHERE id=?",
                        (f"https://ms/entity/purchaseorder/doc-{oid}", oid))
            con.commit()
        finally:
            con.close()

    # (а) Заказ, отправленный в МойСклад: допущение не пишем — исполнение по
    # нему приходит машинным источником, и допущение сложилось бы с ним вдвое.
    oid_pushed = make_order(c3, 10, "Отправлен в МС", pushed=True)
    c3.post(f"/api/orders/{oid_pushed}/status", json={"status": "received"})
    got = c3.get(f"/api/orders/{oid_pushed}/receipts").json()
    check("по заказу, ушедшему в МойСклад, допущение не пишется",
          got["received_total"] is None, str(got["received_total"]))
    from app.ms_sync import _write_shipped_receipts
    _write_shipped_receipts(3, {(oid_pushed, f"doc-{oid_pushed}"): {name3: 10.0}})
    got = c3.get(f"/api/orders/{oid_pushed}/receipts").json()
    check("машинный источник даёт ровно заказанное, без удвоения",
          got["received_total"] == 10, str(got["received_total"]))
    check("источники видны раздельно", got["by_source"] == {"ms_order_shipped": 10.0},
          str(got.get("by_source")))

    # (б) Частичный приход, потом «Принят» одним кликом: подтверждённым
    # остаётся ровно то, что человек назвал. Отметка «принят» закрывает заказ,
    # но не добавляет к 3 недостающие 7 — это было бы выдуманное число.
    oid_part = make_order(c3, 10, "Частичный")
    c3.post(f"/api/orders/{oid_part}/receipts",
            json={"lines": [{"base_name": name3, "qty": 3}]})
    c3.post(f"/api/orders/{oid_part}/status", json={"status": "received"})
    got = c3.get(f"/api/orders/{oid_part}/receipts").json()
    check("подтверждено ровно названное человеком", got["received_total"] == 3,
          str(got["received_total"]))
    check("в истории только подтверждение по строкам",
          got["precisions"] == ["by_position"], str(got["precisions"]))
    check("недостача видна в сверке, а не спрятана",
          got["lines"][0]["diff"] == -7, str(got["lines"][0]))

    # (в) Пустой список received — это «количества не назвали», а не «принято 0».
    oid_empty = make_order(c3, 10, "Пустой список")
    r = c3.post(f"/api/orders/{oid_empty}/status",
                json={"status": "received", "received": []})
    check("пустой список принят", r.status_code == 200, r.text[:120])
    got = c3.get(f"/api/orders/{oid_empty}/receipts").json()
    check("пустой список ничего не выдумал", got["received_total"] is None,
          str(got["received_total"]))
    body_empty = c3.get(f"/api/orders/{oid_empty}").json()
    check("но заказ закрыт: дата приёмки стоит", bool(body_empty.get("received_at")),
          str(body_empty.get("received_at")))
    r = c3.post(f"/api/orders/{oid_empty}/receipts",
                json={"lines": [{"base_name": "   ", "qty": 5}]})
    check("имя из одних пробелов отклоняется, а не глотается молча",
          r.status_code == 422, f"status={r.status_code}")

    # (г) Двойной клик по «Принят»: переход выигрывает ровно один запрос.
    oid_race = make_order(c3, 10, "Двойной клик")
    import concurrent.futures as _f
    with _f.ThreadPoolExecutor(max_workers=6) as pool:
        codes = [r.status_code for r in pool.map(lambda _: c3.post(
            f"/api/orders/{oid_race}/status", json={"status": "received"}), range(6))]
    got = c3.get(f"/api/orders/{oid_race}/receipts").json()
    body_race = c3.get(f"/api/orders/{oid_race}").json()
    check("шесть одновременных «Принят» приняты сервером",
          all(x == 200 for x in codes), str(codes))
    check("переход выполнился ровно один раз", body_race.get("status") == "received",
          str(body_race.get("status")))
    check("и не наплодил строк приёмки", len(got["receipts"]) == 0,
          str(len(got["receipts"])))

    # (д) Удаление заказа уносит его приёмки: id в SQLite переиспользуется,
    # и осиротевшие строки достались бы следующему заказу.
    oid_del = make_order(c3, 10, "На удаление")
    c3.post(f"/api/orders/{oid_del}/receipts",
            json={"lines": [{"base_name": name3, "qty": 4}]})
    left_before = sql("SELECT COUNT(*) FROM order_receipts WHERE order_id=?", oid_del)[0][0]
    check("приёмка записана", left_before == 1, str(left_before))
    r = c3.delete(f"/api/orders/{oid_del}")
    check("заказ удалён", r.status_code == 200, r.text[:120])
    left_after = sql("SELECT COUNT(*) FROM order_receipts WHERE order_id=?", oid_del)[0][0]
    check("вместе с ним удалены и его приёмки", left_after == 0, str(left_after))

    # (е) Две строки заказа с одним именем: «заказано» складывается.
    r = c3.post("/api/orders", json={"name": "Дубль имени", "items": [
        {"base_name": name3, "qty": 5, "sizes": {}, "cost": 100},
        {"base_name": name3, "qty": 7, "sizes": {}, "cost": 100}]})
    oid_dup = r.json().get("id")
    got = c3.get(f"/api/orders/{oid_dup}/receipts").json()
    check("две строки одного имени складываются, а не затирают друг друга",
          got["ordered_total"] == 12, str(got["ordered_total"]))

    # (ж) Переименование позиции переносит и приёмки.
    from app import ms_sync as _ms
    from app.db import SessionLocal as _SL
    oid_ren = make_order(c3, 10, "Переименование")
    c3.post(f"/api/orders/{oid_ren}/receipts",
            json={"lines": [{"base_name": name3, "qty": 4}]})
    con = sqlite3.connect(DB_PATH)
    try:
        con.execute("UPDATE products SET base_name=? WHERE org_id=3 AND base_name=?",
                    (name3 + " v2", name3))
        con.commit()
    finally:
        con.close()
    dbx = _SL()
    try:
        _ms._migrate_renames(dbx, 3, {name3: {name3 + " v2"}}, {})
        dbx.commit()
    finally:
        dbx.close()
    moved = sql("SELECT base_name FROM order_receipts WHERE order_id=?", oid_ren)
    check("приёмка переехала на новое имя вместе с товаром",
          moved and moved[0][0] == name3 + " v2", str(moved))

    # (з) Заказ ушёл в МойСклад, «отгружено» пустое, человек жмёт «Принят».
    # На боевых данных это САМЫЙ частый случай: shipped заполнен у нуля
    # позиций из 69. Показать здесь ноль значило бы утверждать «заказали 65,
    # приехало 0» — подтверждённую недостачу, которой не было.
    r = c3.post("/api/order-plan", json={"budget": 150000, "budget_scope": "now",
                                         "cadence_days": 30, "safety_days": 14})
    plan_ms = r.json().get("id")
    r = c3.post(f"/api/order-plan/{plan_ms}/apply", json={"name": "Уехал в МС"})
    oid_ms = r.json().get("order_id")
    exec_sql_local(oid_ms)
    c3.post(f"/api/orders/{oid_ms}/status", json={"status": "sent"})
    c3.post(f"/api/orders/{oid_ms}/status", json={"status": "received"})
    got = c3.get(f"/api/orders/{oid_ms}/receipts").json()
    check("по такому заказу приёмок нет", got["received_total"] is None,
          str(got["received_total"]))
    check("и выдача честно говорит, что принятое НЕИЗВЕСТНО",
          got.get("execution_unknown") is True, str(got.get("execution_unknown")))
    out_ms = c3.get(f"/api/order-plan/{plan_ms}/outcome").json()
    check("исполнение не выдаётся за подтверждённое",
          out_ms.get("execution_confirmed") is False,
          str(out_ms.get("execution_confirmed")))
    check("итог исполнения — не ноль, а «неизвестно»",
          out_ms["totals"]["executed"] is None, str(out_ms["totals"]))
    check("и построчно тоже неизвестно, а не ноль",
          all(x["executed"] is None for x in out_ms["lines"]),
          str([x for x in out_ms["lines"] if x["executed"] is not None][:2]))
    check("а признак «не знаем» назван прямо",
          out_ms.get("execution_unknown") is True, str(out_ms)[:160])
    # Как только МойСклад пришлёт «отгружено» — цифры появятся.
    first_ms = sorted({i["base_name"] for i in c3.get(f"/api/orders/{oid_ms}").json()["items"]})[0]
    _write_shipped_receipts(3, {(oid_ms, f"doc-{oid_ms}"): {first_ms: 5.0}})
    out_ms = c3.get(f"/api/order-plan/{plan_ms}/outcome").json()
    line_ms = next(x for x in out_ms["lines"] if x["base_name"] == first_ms)
    check("после прихода «отгружено» исполнение по позиции известно",
          line_ms["executed"] == 5.0, str(line_ms))
    # …а по остальным позициям того же заказа — по-прежнему НЕИЗВЕСТНО.
    # МойСклад заполняет «отгружено» по частям, и одна пришедшая позиция
    # не имеет права утверждать «по остальным 28 не приехало ничего».
    others = [x for x in out_ms["lines"] if x["base_name"] != first_ms]
    check("одна машинная строка не обнуляет остальные позиции",
          all(x["executed"] is None for x in others),
          str([x for x in others if x["executed"] is not None][:2]))
    check("итог не показывается, пока известны не все строки",
          out_ms["totals"]["executed"] is None, str(out_ms["totals"]))

    # (и) Переименование у ПРИНЯТОГО заказа: обе половины сверки обязаны
    # жить под одним именем. Раньше items_json принятых заказов не
    # переносился, и сверка распадалась надвое.
    r = c3.post("/api/orders", json={"name": "Принят и переименован", "items": [
        {"base_name": name3 + " v2", "qty": 10, "sizes": {}, "cost": 100}]})
    check("заказ на переименованную позицию создан", r.status_code == 200, r.text[:150])
    oid_rec = r.json().get("id")
    c3.post(f"/api/orders/{oid_rec}/status", json={"status": "sent"})
    c3.post(f"/api/orders/{oid_rec}/receipts",
            json={"lines": [{"base_name": name3 + " v2", "qty": 6}]})
    c3.post(f"/api/orders/{oid_rec}/status", json={"status": "received"})
    con = sqlite3.connect(DB_PATH)
    try:
        con.execute("UPDATE products SET base_name=? WHERE org_id=3 AND base_name=?",
                    (name3 + " v3", name3 + " v2"))
        con.commit()
    finally:
        con.close()
    dbx = _SL()
    try:
        _ms._migrate_renames(dbx, 3, {name3 + " v2": {name3 + " v3"}}, {})
        dbx.commit()
    finally:
        dbx.close()
    got = c3.get(f"/api/orders/{oid_rec}/receipts").json()
    names = sorted({x["base_name"] for x in got["lines"]})
    check("сверка принятого заказа не распалась надвое", len(names) == 1, str(names))
    check("и живёт под новым именем", names == [name3 + " v3"], str(names))

    # (к) Старый формат ключа машинного источника не даёт удвоения.
    r = c3.post("/api/orders", json={"name": "Старый ключ", "items": [
        {"base_name": name3 + " v3", "qty": 10, "sizes": {}, "cost": 100}]})
    check("заказ для проверки старого ключа создан", r.status_code == 200, r.text[:150])
    oid_legacy = r.json().get("id")
    c3.post(f"/api/orders/{oid_legacy}/status", json={"status": "sent"})
    exec_sql_local(oid_legacy)
    con = sqlite3.connect(DB_PATH)
    try:
        con.execute(
            "INSERT INTO order_receipts (org_id, order_id, base_name, qty, at, source,"
            " precision, source_ref, created_at) VALUES (3,?,?,4,'2026-08-22 00:00:00',"
            " 'ms_order_shipped','by_position',?, '2026-08-22 00:00:00')",
            (oid_legacy, name3 + " v3",
             f"https://api.moysklad.ru/api/remap/1.2/entity/purchaseorder/doc-{oid_legacy}"))
        con.commit()
    finally:
        con.close()
    added = _write_shipped_receipts(3, {(oid_legacy, f"doc-{oid_legacy}"): {name3 + " v3": 4.0}})
    check("строку, записанную прежним ключом, находим и не дублируем",
          added == 0, str(added))
    got = c3.get(f"/api/orders/{oid_legacy}/receipts").json()
    check("принятое осталось прежним", got["received_total"] == 4,
          str(got["received_total"]))

    print("\n== Источники не складываются: приоритет и конфликт ==")
    # Инвариант заводится ДО подключения ms_supply намеренно: чинить это после
    # первой автоматической записи пришлось бы уже на испорченных данных.
    # Человек подтвердил 80, потом МойСклад прислал доказуемую приёмку на те
    # же 80 — это не 160, это два свидетельства об одном факте.
    r = c3.post("/api/orders", json={"name": "Два источника", "items": [
        {"base_name": name3 + " v3", "qty": 80, "sizes": {}, "cost": 100}]})
    oid_two = r.json().get("id")
    c3.post(f"/api/orders/{oid_two}/status", json={"status": "sent"})
    exec_sql_local(oid_two)
    c3.post(f"/api/orders/{oid_two}/receipts",
            json={"lines": [{"base_name": name3 + " v3", "qty": 80}]})
    got = c3.get(f"/api/orders/{oid_two}/receipts").json()
    check("сначала подтвердил человек — 80", got["received_total"] == 80,
          str(got["received_total"]))
    _write_shipped_receipts(3, {(oid_two, f"doc-{oid_two}"): {name3 + " v3": 80.0}})
    got = c3.get(f"/api/orders/{oid_two}/receipts").json()
    check("МойСклад прислал те же 80 — итог остался 80, а не стал 160",
          got["received_total"] == 80, str(got["received_total"]))
    check("оба источника видны раздельно",
          got["by_source"] == {"manual": 80.0, "ms_order_shipped": 80.0},
          str(got.get("by_source")))
    check("одинаковые числа конфликтом не считаются",
          got.get("source_conflicts") == [], str(got.get("source_conflicts")))

    # А вот расхождение прятать нельзя: его выносят на разбор человеку.
    _write_shipped_receipts(3, {(oid_two, f"doc-{oid_two}"): {name3 + " v3": 74.0}})
    got = c3.get(f"/api/orders/{oid_two}/receipts").json()
    # Раньше здесь ждали 74 — победителя приоритета. По решению владельца
    # 23.08.2026 спор источников не даёт числа вовсе: подтверждённый итог null,
    # а сырые показания обоих источников остаются ниже, в by_source.
    check("при расхождении подтверждённого числа нет",
          got["received_total"] is None, str(got["received_total"]))
    check("но сырые показания источников сохранены",
          (got.get("by_source") or {}).get("ms_order_shipped") == 74.0
          and (got.get("by_source") or {}).get("manual") == 80.0,
          str(got.get("by_source")))
    conf = got.get("source_conflicts") or []
    check("расхождение названо прямо, а не сложено", len(conf) == 1, str(conf)[:160])
    check("и в нём видно, что сказал каждый источник",
          conf and conf[0]["by_source"] == {"manual": 80.0, "ms_order_shipped": 74.0},
          str(conf)[:200])
    # Спор источников — это не факт, это спор. Пока его не разобрал человек,
    # заказ не имеет права называться подтверждённым: раньше приоритет молча
    # выбирал победителя, и «80 против 74» выезжало наружу как доказанная
    # недостача.
    check("пока источники спорят, заказ НЕ подтверждён",
          got.get("confirmed") is False, str(got.get("confirmed")))
    check("и это названо неизвестностью, а не нулём",
          got.get("execution_unknown") is True, str(got.get("execution_unknown")))

    # Машинный ноль — не свидетельство прихода, а его отсутствие. Так бывает
    # штатно: в МойСкладе приёмку распровели, и компенсирующая строка схлопнула
    # машинную сумму в ноль. Раньше приоритет выбирал источник ПО НАЛИЧИЮ строк,
    # и этот ноль побеждал подтверждённые человеком 80 штук — система переходила
    # от «не знаем» к уверенному «приехало ноль».
    _write_shipped_receipts(3, {(oid_two, f"doc-{oid_two}"): {name3 + " v3": 0.0}})
    got = c3.get(f"/api/orders/{oid_two}/receipts").json()
    check("машинный ноль НЕ стирает подтверждение человека",
          got["received_total"] == 80, str(got["received_total"]))
    check("машинная сумма при этом действительно ноль",
          (got.get("by_source") or {}).get("ms_order_shipped") == 0.0,
          str(got.get("by_source")))
    # И это НЕ спор источников. Первая версия правки считала машинный ноль
    # полноправным свидетельством в конфликте — заказ уходил в «неизвестно»
    # навсегда, потому что единственным способом снять расхождение было
    # согласиться с нулём, то есть испортить данные. «В документах ничего нет»
    # не спорит с «человек подтвердил 80»: это утверждение и пустота.
    check("машинный ноль не считается спором с подтверждением человека",
          got.get("source_conflicts") == [], str(got.get("source_conflicts")))
    check("и заказ остаётся подтверждённым на 80",
          got.get("confirmed") is True and got["received_total"] == 80,
          f"confirmed={got.get('confirmed')} total={got.get('received_total')}")

    # А вот машинный ноль БЕЗ подтверждения человека — это «не знаем», а не
    # «приехало ноль». Позиции просто нет в сверке.
    from app.api import _received_by_base as _rbb0

    class _R0:
        def __init__(self, base, qty, source):
            self.base_name, self.qty, self.source = base, qty, source

    only_machine = [_R0("Кепка", 80, "ms_order_shipped"),
                    _R0("Кепка", -80, "ms_order_shipped")]
    check("схлопнувшийся машинный источник не даёт «приехало ноль»",
          _rbb0(only_machine) == {}, str(_rbb0(only_machine)))
    # Но осознанный ноль человека — утверждение, и он остаётся.
    human_zero = [_R0("Кепка", 0, "manual")]
    check("ноль, записанный человеком, остаётся фактом",
          _rbb0(human_zero) == {"Кепка": 0.0}, str(_rbb0(human_zero)))

    # Склейка переименованных позиций: МойСклад объединил два товара в один,
    # приёмки разных источников съехались под одно имя и выглядят как два
    # свидетельства об одном приходе. Честный ответ — «не знаем», а не число,
    # получившееся из склейки.
    from app.api import _received_by_base as _rbb, _source_conflicts as _sc

    class _Row:
        def __init__(self, base, qty, source):
            self.base_name, self.qty, self.source = base, qty, source

    merged = [_Row("NEW", 6, "ms_order_shipped"), _Row("NEW", 4, "manual")]
    check("склейка объявлена спором, а не недостачей",
          len(_sc(merged)) == 1, str(_sc(merged))[:160])
    part = [_Row("Худи", 30, "manual"), _Row("Худи", 50, "manual")]
    check("частичный приход и довоз одного источника по-прежнему складываются",
          _rbb(part) == {"Худи": 80.0}, str(_rbb(part)))

    print("\n== Повтор запроса приёмки не удваивает принятое ==")
    # Таблица приёмок только пополняется, поэтому двойной клик или ретрай
    # после таймаута дописывал вторую такую же строку. Машинный источник от
    # этого защищён source_ref; ручному дан тот же механизм — ключ повтора.
    r = c3.post("/api/orders", json={"name": "Повтор", "items": [
        {"base_name": name3 + " v3", "qty": 20, "sizes": {}, "cost": 100}]})
    oid_rep = r.json().get("id")
    c3.post(f"/api/orders/{oid_rep}/status", json={"status": "sent"})
    body = {"lines": [{"base_name": name3 + " v3", "qty": 10}],
            "idempotency_key": "klik-1"}
    first = c3.post(f"/api/orders/{oid_rep}/receipts", json=body).json()
    second = c3.post(f"/api/orders/{oid_rep}/receipts", json=body).json()
    check("первый запрос записал строку", first.get("added") == 1, str(first.get("added")))
    check("повтор с тем же ключом не записал ничего",
          second.get("added") == 0 and second.get("repeat") is True,
          f"added={second.get('added')} repeat={second.get('repeat')}")
    check("и принятое не удвоилось", second["received_total"] == 10,
          str(second["received_total"]))
    # А другой ключ — это другой факт: довоз должен пройти.
    third = c3.post(f"/api/orders/{oid_rep}/receipts", json={
        "lines": [{"base_name": name3 + " v3", "qty": 10}],
        "idempotency_key": "klik-2"}).json()
    check("довоз с другим ключом записывается", third["received_total"] == 20,
          str(third["received_total"]))
    # Без ключа поведение прежнее: ручка остаётся совместимой.
    fourth = c3.post(f"/api/orders/{oid_rep}/receipts", json={
        "lines": [{"base_name": name3 + " v3", "qty": 5}]}).json()
    check("запрос без ключа работает как раньше", fourth["received_total"] == 25,
          str(fourth["received_total"]))
    # Ключ привязан к телу запроса. Клиент, который генерирует ключ на сессию
    # или на заказ, а не на запрос, иначе молча терял бы довоз: тот же ключ с
    # другими позициями считался бы повтором.
    fifth = c3.post(f"/api/orders/{oid_rep}/receipts", json={
        "lines": [{"base_name": name3 + " v3", "qty": 3}],
        "idempotency_key": "klik-1"}).json()
    check("тот же ключ с ДРУГИМ телом — новый факт, а не повтор",
          fifth.get("added") == 1 and fifth["received_total"] == 28,
          f"added={fifth.get('added')} total={fifth.get('received_total')}")

    print("\n== Две выдачи об одном заказе отвечают одинаково ==")
    # Сверка приёмок говорила «не знаем» из-за спора источников, а outcome —
    # тот самый экран, по которому меряют качество рекомендаций, — выдавал
    # победителя приоритета как факт. Расхождение двух ответов об одном заказе
    # хуже любого из них по отдельности.
    # Спор заводим на заказе, ВЫРОСШЕМ ИЗ ПЛАНА: только у такого есть outcome,
    # и только там расхождение двух ответов имеет цену — по этому экрану потом
    # меряют качество рекомендаций.
    rc0 = c.get(f"/api/orders/{order_id}/receipts").json()
    already = {x["base_name"] for x in rc0.get("source_conflicts") or []}
    oc0 = c.get(f"/api/order-plan/{plan_id}/outcome").json()
    in_plan = [x["base_name"] for x in oc0.get("lines", [])]
    # Позиция должна быть и в приёмке, и в строках плана: спор нужен там, где
    # его увидят ОБЕ выдачи, иначе проверка сравнивала бы разные вещи.
    free = [b for b in in_plan if b not in already]
    check("в плане есть позиция без спора", bool(free), f"уже спорят: {sorted(already)}")
    spor_base, spor_qty = free[0], 4.0
    r = c.post(f"/api/orders/{order_id}/receipts",
               json={"lines": [{"base_name": spor_base, "qty": spor_qty}]})
    check("человек подтвердил приход по ней", r.status_code == 200, r.text[:120])
    _write_shipped_receipts(1, {(order_id, f"doc-spor-{order_id}"):
                                {spor_base: spor_qty + 5}})
    oc = c.get(f"/api/order-plan/{plan_id}/outcome").json()
    rc = c.get(f"/api/orders/{order_id}/receipts").json()
    check("пока источники спорят, обе выдачи говорят «не подтверждено»",
          oc.get("execution_confirmed") is False and rc.get("confirmed") is False,
          f"outcome={oc.get('execution_confirmed')} receipts={rc.get('confirmed')}")
    check("и обе называют это неизвестностью",
          oc.get("execution_unknown") is True and rc.get("execution_unknown") is True,
          f"outcome={oc.get('execution_unknown')} receipts={rc.get('execution_unknown')}")
    check("спорная позиция названа поимённо",
          spor_base in (oc.get("disputed_bases") or []), str(oc.get("disputed_bases")))
    disputed_lines = [x for x in oc.get("lines", []) if x["base_name"] == spor_base]
    check("и по ней executed = null, а не число из спора",
          disputed_lines and all(x["executed"] is None for x in disputed_lines),
          str(disputed_lines)[:160])

    print("\n== Контракт null: числа нет — значит null, а не ноль ==")
    # Решение владельца 23.08.2026. Ноль — это утверждение «не приехало»;
    # null — честное «не знаем». Раньше оба случая выглядели как ноль, и
    # статистика качества рекомендаций считала нашу неизвестность измерением.
    r = c3.post("/api/orders", json={"name": "Контракт null", "items": [
        {"base_name": name3 + " v3", "qty": 9, "sizes": {}, "cost": 100}]})
    oid_null = r.json().get("id")
    c3.post(f"/api/orders/{oid_null}/status", json={"status": "sent"})
    got = c3.get(f"/api/orders/{oid_null}/receipts").json()
    line = next((x for x in got["lines"] if x["base_name"] == name3 + " v3"), None)
    check("построчно: received_qty = null, когда факта нет",
          line is not None and line["received_qty"] is None, str(line))
    check("построчно: diff тоже null, а не «минус всё заказанное»",
          line is not None and line["diff"] is None, str(line))
    check("итог по заказу = null", got["received_total"] is None,
          str(got["received_total"]))
    check("заказанное при этом известно и остаётся числом",
          got["ordered_total"] == 9, str(got["ordered_total"]))

    # Записали факт — числа появляются.
    c3.post(f"/api/orders/{oid_null}/receipts",
            json={"lines": [{"base_name": name3 + " v3", "qty": 9}]})
    got = c3.get(f"/api/orders/{oid_null}/receipts").json()
    line = next((x for x in got["lines"] if x["base_name"] == name3 + " v3"), None)
    check("после записи факта число появляется",
          line["received_qty"] == 9 and line["diff"] == 0, str(line))
    check("и итог перестаёт быть null", got["received_total"] == 9,
          str(got["received_total"]))
    check("заказ подтверждён", got.get("confirmed") is True, str(got.get("confirmed")))

    print("\n== Старое допущение whole_order не выдаётся за подтверждение ==")
    # Код перестал ПИСАТЬ такие строки в П20, но в боевой базе они уже лежат с
    # прежних версий и до этой отсечки считались подтверждением количества
    # наравне с названными цифрами. Чинить только новые данные — половина
    # работы: статистика качества рекомендаций считается по всей истории.
    r = c3.post("/api/orders", json={"name": "Допущение", "items": [
        {"base_name": name3 + " v3", "qty": 15, "sizes": {}, "cost": 100}]})
    oid_as = r.json().get("id")
    c3.post(f"/api/orders/{oid_as}/status", json={"status": "sent"})
    # Пишем строку ровно так, как её писали прежние версии кода.
    _raw_sql(
        "INSERT INTO order_receipts (org_id, order_id, base_name, qty, at, "
        "source, precision, source_ref, created_by, created_at) VALUES "
        "(3, ?, ?, 15, ?, 'manual', 'whole_order', '', 1, ?)",
        oid_as, name3 + " v3",
        datetime.utcnow().isoformat(sep=" ", timespec="seconds"),
        datetime.utcnow().isoformat(sep=" ", timespec="seconds"),
    )
    got = c3.get(f"/api/orders/{oid_as}/receipts").json()
    check("старое допущение не считается принятым количеством",
          got["received_total"] is None, str(got["received_total"]))
    check("и заказ честно говорит «не знаем», а не «принято 15»",
          got.get("execution_unknown") is True and got.get("confirmed") is False,
          f"unknown={got.get('execution_unknown')} confirmed={got.get('confirmed')}")
    check("но сама строка из истории не пропала",
          "whole_order" in (got.get("precisions") or []), str(got.get("precisions")))
    # А названное человеком количество по тому же заказу считается как обычно.
    c3.post(f"/api/orders/{oid_as}/receipts",
            json={"lines": [{"base_name": name3 + " v3", "qty": 15}]})
    got = c3.get(f"/api/orders/{oid_as}/receipts").json()
    check("названное количество поверх допущения считается",
          got["received_total"] == 15, str(got["received_total"]))

    print("\n== Ноль, записанный человеком, — это факт, а не пустота ==")
    # Ручка отвечала `ok: true, added: 0`, строку не писала, и утверждение
    # пользователя молча исчезало. Соседний путь (перевод в «на складе» с
    # количествами) нули писал — две ручки отвечали об одном по-разному.
    r = c3.post("/api/orders", json={"name": "Ноль", "items": [
        {"base_name": name3 + " v3", "qty": 12, "sizes": {}, "cost": 100}]})
    oid_zero = r.json().get("id")
    c3.post(f"/api/orders/{oid_zero}/status", json={"status": "sent"})
    z = c3.post(f"/api/orders/{oid_zero}/receipts", json={
        "lines": [{"base_name": name3 + " v3", "qty": 0}]}).json()
    check("ноль записан строкой, а не проглочен", z.get("added") == 1,
          str(z.get("added")))
    check("и это подтверждённый ноль, а не «неизвестно»",
          z.get("confirmed") is True and z.get("execution_unknown") is False,
          f"confirmed={z.get('confirmed')} unknown={z.get('execution_unknown')}")

    print("\n== Огромный id не роняет outcome ==")
    r = c.get("/api/order-plan/99999999999999999999999999/outcome")
    check("вместо 500 — аккуратный отказ", r.status_code == 422, f"status={r.status_code}")

    print("\n== Проводка машинного источника внутрь синка ==")
    # Дельту проверяем прямыми вызовами (выше), но остаётся вопрос «а вызывают
    # ли её вообще». Сценарий с настоящим маркером в моке завести нельзя: тем
    # же маркером ищет дедупликация отправки, и seed сломал бы её (см.
    # комментарий у po-seed-6). Поэтому связь проверяется разбором дерева —
    # так же, как сторож на it["rate"] в П13: пропажу вызова тест поймает,
    # а комментарий с этим именем нарушением не считается.
    import ast as _ast

    tree = _ast.parse((ROOT / "app" / "ms_sync.py").read_text(encoding="utf-8"))
    fn = next((n for n in _ast.walk(tree)
               if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))
               and n.name == "_sync_incoming"), None)
    calls = {n.func.id for n in _ast.walk(fn) if isinstance(n, _ast.Call)
             and isinstance(n.func, _ast.Name)} if fn else set()
    check("синк заказов поставщику пишет приёмки по «отгружено»",
          "_write_shipped_receipts" in calls, str(sorted(calls))[:200])
    check("и берёт id заказа доказуемой связью, а не догадкой",
          "_oborot_order_id" in calls, str(sorted(calls))[:200])

    print("\n== Удаление организации не оставляет приёмок ==")
    before = sql("SELECT COUNT(*) FROM order_receipts")[0][0]
    check("приёмки в базе есть", before > 0, str(before))
    r = c2.post("/api/account/delete", json={"password": "secret123", "confirm": "УДАЛИТЬ", "mode": "org"})
    check("организация удалена", r.status_code in (200, 204), f"{r.status_code} {r.text[:120]}")
    left = sql("SELECT COUNT(*) FROM order_receipts WHERE org_id=2")[0][0]
    check("её приёмок не осталось", left == 0, str(left))
    mine = sql("SELECT COUNT(*) FROM order_receipts WHERE org_id=1")[0][0]
    check("наши приёмки не пострадали", mine > 0, str(mine))

    print(f"\nИтого: {len(PASS)} OK, {len(FAIL)} FAIL")
    for name in FAIL:
        print(f"  FAIL {name}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
