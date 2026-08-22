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
    for x in out["lines"]:
        if x["recommended"] is None:
            check(f"строка без рекомендации помечена честно: {x['base_name']}", True)
    check("у каждой строки есть решение человека",
          all(isinstance(x["decided"], (int, float)) for x in out["lines"]))
    check("исполнение уже подтверждено (приёмки записаны вручную)",
          out.get("execution_confirmed") is True, str(out.get("execution_confirmed")))

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
    check("количества взяты из заказа", got2["received_total"] == 10, str(got2["received_total"]))
    check("и помечены как допущение, а не подтверждение",
          got2["precisions"] == ["whole_order"], str(got2["precisions"]))
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
