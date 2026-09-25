# -*- coding: utf-8 -*-
"""Гейт подписки (D-24): одно состояние active | grace | readonly.

Почему это отдельный набор. Гейт — единственная фича, которая умеет ЗАКРЫТЬ
работающему клиенту доступ. Цена ошибки несимметрична: пропустить неплательщика
— потерять деньги за месяц, закрыть плательщика — потерять клиента. Поэтому
здесь проверяется не только «блокирует», но и, прежде всего, «НЕ блокирует»:
чтение, экспорт, страницу тарифов и саму заявку на счёт.

Проверяется:
  1) выключенный по умолчанию флаг: без OBOROT_SUBSCRIPTION_GATE не блокируется
     ничего, даже у организации с истёкшим триалом;
  2) машина состояний: триал, paid_until, грейс от отметки «счёт выставлен»,
     организации из каталога МойСклад, suspended;
  3) грейс ровно 5 календарных дней и ни днём больше;
  4) с включённым флагом readonly закрывает три группы (синк, запись в МС,
     расчёт и сохранение планов) и НИ ОДНОГО чтения;
  5) сторож по всем POST-роутам приложения: множество закрытых гейтом ручек
     совпадает с ожидаемым списком — новая пишущая ручка не проскочит молча
     ни внутрь гейта, ни мимо него;
  6) аддитивная миграция: paid_until/invoiced_at появляются на старой базе.

Запуск из корня репозитория:  python tests/test_subscription.py
"""
import ast
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "test_subscription.db"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["SCHEDULER_ENABLED"] = "0"
os.environ.pop("OBOROT_SUBSCRIPTION_GATE", None)

if DB_PATH.exists():
    DB_PATH.unlink()

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

from app import subscription  # noqa: E402
from app.main import app as oborot_app  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        PASS.append(name)
        print(f"  OK   {name}" + (f"  [{detail}]" if detail else ""))
    else:
        FAIL.append(name)
        print(f"  FAIL {name}  {detail}")


def exec_sql(query: str, *args) -> None:
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


def gate(on: bool) -> None:
    if on:
        os.environ["OBOROT_SUBSCRIPTION_GATE"] = "1"
    else:
        os.environ.pop("OBOROT_SUBSCRIPTION_GATE", None)


def set_org(org_id: int, **fields) -> None:
    for key, value in fields.items():
        exec_sql(f"UPDATE orgs SET {key} = ? WHERE id = ?", value, org_id)


def state_of(org_id: int) -> str:
    """Состояние через ту же функцию, что и приложение (без HTTP)."""
    from app.db import SessionLocal
    from app.models import Org

    db = SessionLocal()
    try:
        db.expire_all()
        return subscription.subscription_state(db.get(Org, org_id), db)
    finally:
        db.close()


D = lambda n: (date.today() + timedelta(days=n)).isoformat()  # noqa: E731


# ── Сторож по роутам ─────────────────────────────────────────────────────────

# Гейт теперь запрещает по умолчанию, поэтому списка ЗАКРЫТЫХ ручек больше
# нет — есть список открытых, и он живёт в коде (subscription.ALWAYS_OPEN_PATHS),
# а не в тесте. Сторож проверяет три вещи:
#   1) каждый открытый путь действительно существует в приложении;
#   2) в открытых нет ничего, чему там не место (список сверяется дословно);
#   3) представительный набор пишущих ручек в readonly действительно закрыт,
#      а открытые — действительно открыты. Это проверяется запросами, а не
#      чтением: первая версия сторожа сверяла только множество закрытых, и
#      незакрытая ручка была для неё невидима в принципе.
EXPECTED_OPEN = {
    "/api/plans/request": "заявка на счёт — единственный путь к оплате",
    "/api/export/replenish.xlsx": "выгрузка своих же данных",
    "/login": "вход",
    "/register": "регистрация",
    "/logout": "выход",
    "/api/account/password": "смена пароля",
    "/api/account/delete": "удаление аккаунта",
    # Подсказки и уроки ОТСЮДА УБРАНЫ (решение владельца 23.08.2026, строгий
    # readonly). Довод «это состояние экрана одного человека, а не данные
    # организации» остаётся верным, но перечень владельца их не содержит, а
    # строгий режим на то и строгий: список исключений не растёт от здравых
    # доводов исполнителя. Ниже они проверяются как ЗАКРЫТЫЕ.
    "/ms/vendor/api/moysklad/vendor/1.0/apps/{path_app_id}/{account_id}":
        "lifecycle МойСклада — вызывает сам МС своим JWT",
}


def _has_gate(dependant) -> bool:
    if getattr(dependant, "call", None) is subscription.require_write_access:
        return True
    return any(_has_gate(d) for d in getattr(dependant, "dependencies", []))


def all_routes(node=None) -> list:
    """Все маршруты приложения, включая вложенные роутеры.

    Плоского списка мало — см. комментарий в теле функции.
    """
    node = oborot_app if node is None else node
    out = []
    for route in getattr(node, "routes", []) or []:
        if getattr(route, "methods", None) and hasattr(route, "path"):
            out.append(route)
        # include_router в этой версии FastAPI оставляет в app.routes
        # обёртку-роутер (_IncludedRouter) без .routes — настоящие маршруты
        # лежат в original_router. Наивный обход видел бы только пять ручек
        # из app/main.py и молча проверял бы пустоту.
        nested = getattr(route, "original_router", None) or route
        if nested is not route or hasattr(nested, "routes"):
            if getattr(nested, "routes", None):
                out.extend(all_routes(nested))
    return out


def gated_routes() -> set:
    found = set()
    for route in all_routes():
        dep = getattr(route, "dependant", None)
        if dep is None or not _has_gate(dep):
            continue
        for method in sorted(route.methods or []):
            if method in ("HEAD", "OPTIONS"):
                continue
            found.add((method, route.path))
    return found


def mutating_routes() -> set:
    out = set()
    for route in all_routes():
        for method in route.methods or []:
            if method in ("POST", "PUT", "PATCH", "DELETE"):
                out.add((method, route.path))
    return out


def main() -> int:
    print("\n== Машина состояний ==")
    gate(False)
    with TestClient(oborot_app, headers={"X-Oborot-CSRF": "1"}) as c:
        r = c.post("/register", data={
            "name": "vlad", "email": "gate@test.io",
            "password": "secret123", "org_name": "Гейт-бренд",
        })
        check("регистрация прошла", r.status_code in (200, 303), f"status={r.status_code}")
        org_id = sql("SELECT id FROM orgs WHERE name = ?", "Гейт-бренд")[0][0]

        set_org(org_id, plan="trial", trial_ends_at=f"{D(7)} 00:00:00", paid_until=None)
        check("живой триал — active", state_of(org_id) == subscription.ACTIVE, state_of(org_id))

        set_org(org_id, trial_ends_at=f"{D(0)} 00:00:00")
        check("последний день триала ещё active",
              state_of(org_id) == subscription.ACTIVE, state_of(org_id))

        set_org(org_id, trial_ends_at=f"{D(-1)} 00:00:00")
        check("триал кончился вчера — readonly",
              state_of(org_id) == subscription.READONLY, state_of(org_id))

        set_org(org_id, plan="start", trial_ends_at=f"{D(3)} 00:00:00", paid_until=None)
        check("тариф переключён, но триал ещё идёт — active (дата триала наша)",
              state_of(org_id) == subscription.ACTIVE, state_of(org_id))
        set_org(org_id, trial_ends_at=f"{D(-1)} 00:00:00")

        set_org(org_id, plan="start", paid_until=D(30))
        check("оплачено вперёд — active", state_of(org_id) == subscription.ACTIVE, state_of(org_id))

        set_org(org_id, paid_until=D(0))
        check("последний оплаченный день ещё active",
              state_of(org_id) == subscription.ACTIVE, state_of(org_id))

        set_org(org_id, paid_until=D(-1))
        check("оплата кончилась вчера — readonly",
              state_of(org_id) == subscription.READONLY, state_of(org_id))

        print("\n== Грейс: 5 календарных дней от отметки «счёт выставлен» ==")
        exec_sql(
            "INSERT INTO billing_requests (org_id, user_id, plan, period, amount, "
            "company, inn, email, phone, comment, status, created_at) "
            "VALUES (?, 1, 'start', 'month', 3900, '', '', '', '', '', 'new', ?)",
            org_id, datetime.utcnow().isoformat(sep=" ", timespec="seconds"),
        )
        req_id = sql("SELECT id FROM billing_requests ORDER BY id DESC LIMIT 1")[0][0]
        check("заявка со статусом new грейса не даёт",
              state_of(org_id) == subscription.READONLY, state_of(org_id))

        exec_sql("UPDATE billing_requests SET status = 'invoiced', invoiced_at = ? WHERE id = ?",
                 f"{D(-2)} 10:00:00", req_id)
        check("счёт выставлен 2 дня назад — grace",
              state_of(org_id) == subscription.GRACE, state_of(org_id))

        exec_sql("UPDATE billing_requests SET invoiced_at = ? WHERE id = ?",
                 f"{D(-5)} 10:00:00", req_id)
        check("пятый день грейса ещё grace",
              state_of(org_id) == subscription.GRACE, state_of(org_id))

        exec_sql("UPDATE billing_requests SET invoiced_at = ? WHERE id = ?",
                 f"{D(-6)} 10:00:00", req_id)
        check("шестой день — грейс кончился, readonly",
              state_of(org_id) == subscription.READONLY, state_of(org_id))

        exec_sql("UPDATE billing_requests SET invoiced_at = NULL WHERE id = ?", req_id)
        st = state_of(org_id)
        stamped = sql("SELECT invoiced_at FROM billing_requests WHERE id = ?", req_id)[0][0]
        check("отметка invoiced без времени — это грейс, а не отказ",
              st == subscription.GRACE, f"{st}")
        check("но чтение состояния при этом НИЧЕГО не пишет в базу",
              stamped is None, f"stamp={stamped}")

        print("\n== Источник истины об оплате — только orgs.paid_until ==")
        # Решение владельца 23.08.2026. Раньше здесь проверялось обратное: что
        # отметка `paid` сама по себе даёт «купленный срок» (месяц или год от
        # даты счёта). Это была моя конструкция, и она неверна: система
        # назначала доступ по числу, которого никто не вводил. Ошибку оператора
        # надо показывать оператору, а не превращать в тихое право доступа.
        # Счёт выставлен ДАВНО: грейс (5 дней от счёта) уже истёк, поэтому
        # проверяется именно отметка «оплачено», а не соседний механизм.
        exec_sql("UPDATE billing_requests SET status = 'paid', period = 'year', "
                 "invoiced_at = ? WHERE id = ?", f"{D(-30)} 10:00:00", req_id)
        set_org(org_id, paid_until=None)
        check("отметка «оплачено» БЕЗ paid_until доступа не даёт",
              state_of(org_id) == subscription.READONLY, state_of(org_id))
        # Тот же факт через HTTP: этот раздел проверяет машину состояний при
        # выключенном флаге, поэтому гейт включаем на один запрос и гасим.
        gate(True)
        r = c.post("/api/sync/run")
        gate(False)
        check("и на запись отвечает отказом", r.status_code == 402,
              f"status={r.status_code}")

        # Оператор проставил срок — вот теперь доступ есть, и именно из-за срока.
        set_org(org_id, paid_until=D(30))
        check("проставленный paid_until открывает доступ",
              state_of(org_id) == subscription.ACTIVE, state_of(org_id))
        gate(True)
        r = c.post("/api/sync/run")
        gate(False)
        check("и запись проходит", r.status_code != 402, f"status={r.status_code}")

        # Срок истёк — отметка `paid` его не продлевает.
        set_org(org_id, paid_until=D(-1))
        check("истёкший paid_until закрывает, даже если заявка «оплачено»",
              state_of(org_id) == subscription.READONLY, state_of(org_id))

        # Диагностика обязана быть: молча закрыть плательщика — худший исход.
        import io as _io, logging as _logging
        buf = _io.StringIO()
        h = _logging.StreamHandler(buf)
        lg = _logging.getLogger("oborot.subscription")
        lg.addHandler(h)
        try:
            state_of(org_id)
        finally:
            lg.removeHandler(h)
        check("оператор получает предупреждение про непроставленный срок",
              "paid_until" in buf.getvalue() and "ЗАКРЫТ" in buf.getvalue(),
              buf.getvalue()[:160] or "(лог пуст)")

        set_org(org_id, paid_until=None)
        exec_sql("UPDATE billing_requests SET status = 'new', invoiced_at = NULL WHERE id = ?",
                 req_id)

        print("\n== Организации из каталога МойСклад и suspended ==")
        set_org(org_id, source="ms_app", status="active", paid_until=None,
                trial_ends_at=f"{D(-30)} 00:00:00")
        check("ms_app + active — active (платит внутри МС)",
              state_of(org_id) == subscription.ACTIVE, state_of(org_id))
        set_org(org_id, status="suspended")
        check("ms_app + suspended — readonly",
              state_of(org_id) == subscription.READONLY, state_of(org_id))
        set_org(org_id, source="saas", status="suspended", paid_until=D(30))
        check("suspended важнее оплаты — readonly",
              state_of(org_id) == subscription.READONLY, state_of(org_id))
        set_org(org_id, status="active")

        print("\n== Флаг выключен: не блокируется ничего ==")
        gate(False)
        set_org(org_id, source="saas", plan="start", paid_until=D(-30),
                trial_ends_at=f"{D(-60)} 00:00:00")
        check("состояние readonly, но флаг выключен",
              state_of(org_id) == subscription.READONLY, state_of(org_id))
        r = c.post("/api/sync/run")
        check("синк не отдаёт 402 при выключенном флаге",
              r.status_code != 402, f"status={r.status_code}")
        r = c.post("/api/order-plan/preview", json={})
        check("расчёт плана не отдаёт 402 при выключенном флаге",
              r.status_code != 402, f"status={r.status_code}")
        body = c.get("/api/subscription").json()
        check("/api/subscription честно говорит, что гейт выключен",
              body.get("gate_enabled") is False and body.get("writes_blocked") is False,
              str(body)[:160])

        print("\n== Флаг включён: readonly закрывает ЛЮБОЕ изменение данных ==")
        # Проверяем поведение, а не «согласованный список». Первая версия
        # закрывала три группы и оставляла открытыми заказы, приёмки,
        # настройки, скидки, категории и производства — это модель «не даём
        # новую вычислительную ценность, но разрешаем менять данные», а не
        # read-only. Утверждённое решение D-24 говорит про второе.
        gate(True)
        blocked = {
            "синхронизация (инкрементальная)": c.post("/api/sync/run"),
            "синхронизация (первичная)": c.post("/api/sync/initial"),
            "запись в МойСклад": c.post("/api/orders/1/push-to-ms"),
            "расчёт плана": c.post("/api/order-plan/preview", json={}),
            "сохранение плана": c.post("/api/order-plan", json={}),
            "применение плана": c.post("/api/order-plan/1/apply", json={}),
            "демо-данные (стирают всё)": c.post("/api/connect/demo"),
            "создание заказа": c.post("/api/orders", json={"name": "x", "items": []}),
            "смена статуса заказа": c.post("/api/orders/1/status",
                                           json={"status": "sent"}),
            "удаление заказа": c.delete("/api/orders/1"),
            "запись приёмки": c.post("/api/orders/1/receipts", json={"lines": []}),
            "ручное «едет к нам»": c.post("/api/ordered",
                                          json={"base_name": "x", "qty": 1}),
            "настройки организации": c.post("/api/settings", json={}),
            "исключения из аналитики": c.post(
                "/api/exclusions", json={"base_name": "x", "excluded": True}),
            "архив позиции": c.post("/api/hidden",
                                    json={"base_name": "x", "hidden": True}),
            "перенос категории": c.post(
                "/api/categories/override", json={"base_name": "x", "category": "y"}),
            "слияние категорий": c.post(
                "/api/categories/merge", json={"from_category": "a", "to_category": "b"}),
            "ручная скидка": c.post("/api/discount-overrides",
                                    json={"base_name": "x", "discount": 10}),
            "дефолтные скидки": c.post("/api/discount-overrides/defaults"),
            "черновик ростовки": c.post("/api/replenish-draft", json={}),
            "канал производства": c.post("/api/productions", json={"name": "Цех"}),
            "правило распределения": c.post(
                "/api/productions/assign-rule",
                json={"assign_source": "supplier", "assign_map": {}}),
            "ввод токена МойСклада": c.post("/api/connect/moysklad",
                                            json={"token": "x"}),
            "настройки уведомлений": c.post("/api/notify/settings", json={}),
            # SUPPLY-FIX-2: новые пишущие ручки плана. Гейт стоит на приложении
            # и запрещает по умолчанию, поэтому сюда они попадают не «на всякий
            # случай»: сторож проверяет запросом, а не чтением списка, — и
            # незакрытая ручка иначе была бы для него невидима (см. шапку).
            # Идентификатор заведомо чужой: до тела запрос не доходит, отказ
            # обязан прийти от гейта, а не от поиска строки.
            "план: убрать материал": c.post(
                "/api/supply/planning/materials/999999/archive", json={}),
            "план: вернуть материал": c.post(
                "/api/supply/planning/materials/999999/restore", json={}),
            "план: правка вещи": c.post(
                "/api/supply/planning/items/999999/update", json={}),
            "план: убрать вещь": c.post(
                "/api/supply/planning/items/999999/archive", json={}),
            "план: вернуть вещь": c.post(
                "/api/supply/planning/items/999999/restore", json={}),
            "план: убрать партию": c.post(
                "/api/supply/planning/batches/999999/archive", json={}),
            "план: вернуть партию": c.post(
                "/api/supply/planning/batches/999999/restore", json={}),
        }
        for label, resp in blocked.items():
            check(f"readonly закрывает: {label}", resp.status_code == 402,
                  f"status={resp.status_code}")
        check("402 объясняет причину человеку",
              "подписк" in blocked["синхронизация (инкрементальная)"].text.lower(),
              blocked["синхронизация (инкрементальная)"].text[:120])

        print("\n== Интерфейс не бьётся о собственный отказ ==")
        # Строгий режим закрыл отметки подсказок и прогресса обучения. Если
        # страница продолжит их слать, человек получит 402 при обычном
        # листании СВОИХ ЖЕ данных — отказ, который ничего не защищает.
        # Поэтому страница узнаёт о запрете с сервера и просто не шлёт запрос.
        gate(True)
        html = c.get("/").text
        check("при включённом гейте страница знает, что запись закрыта",
              "window.OBOROT_READONLY = true" in html,
              [l.strip() for l in html.splitlines() if "OBOROT_READONLY" in l][:1])
        gate(False)
        html = c.get("/").text
        check("при выключенном гейте ничего не гасим — запись-то проходит",
              "window.OBOROT_READONLY = false" in html,
              [l.strip() for l in html.splitlines() if "OBOROT_READONLY" in l][:1])
        gate(True)

        print("\n== Флаг включён: чтение остаётся открытым ==")
        # Читающие ручки обязаны не просто «не отдавать 402», а РАБОТАТЬ:
        # проверка «status != 402» проходила бы и на 404 несуществующего
        # маршрута, и на 500. Поэтому здесь ждём именно 200.
        reads = {
            "/api/subscription": c.get("/api/subscription"),
            "/api/plans": c.get("/api/plans"),
            "/plans": c.get("/plans"),
            "/api/settings": c.get("/api/settings"),
            "/api/turnover": c.get("/api/turnover"),
            "/api/orders": c.get("/api/orders"),
            "/api/order-plan/last": c.get("/api/order-plan/last"),
            "/api/sync/status": c.get("/api/sync/status"),
            "/api/replenish": c.get("/api/replenish"),
            "/": c.get("/"),
        }
        for path, resp in reads.items():
            check(f"чтение работает, а не просто «не 402»: {path}",
                  resp.status_code == 200, f"status={resp.status_code}")
        r = c.post("/api/plans/request", json={
            "plan": "start", "period": "month", "company": "ООО Тест",
            "inn": "7700000000", "email": "a@b.io", "phone": "+70000000000",
        })
        check("заявка на счёт проходит (иначе платить нечем)",
              r.status_code == 200, f"status={r.status_code} {r.text[:120]}")
        r = c.post("/api/export/replenish.xlsx", json={"rows": []})
        check("экспорт не блокируется", r.status_code != 402,
              f"status={r.status_code} {r.text[:120]}")
        # Состояние интерфейса конкретного человека — не данные организации.
        # Строгий readonly: состояние интерфейса — тоже изменение данных.
        for path, body in (("/api/hints/seen", {"key": "turnover"}),
                           ("/api/prefs/hints", {"enabled": True}),
                           ("/api/lessons/reset", None)):
            rr = c.post(path, json=body) if body is not None else c.post(path)
            check(f"строгий режим закрывает и это: {path}", rr.status_code == 402,
                  f"status={rr.status_code}")

        body = c.get("/api/subscription").json()
        check("/api/subscription сообщает readonly и блокировку записи",
              body.get("state") == "readonly" and body.get("writes_blocked") is True,
              str(body)[:160])

        print("\n== Грейс пишет как обычно ==")
        exec_sql("UPDATE billing_requests SET status = 'invoiced', invoiced_at = ? WHERE id = ?",
                 f"{D(-1)} 10:00:00", req_id)
        check("состояние grace", state_of(org_id) == subscription.GRACE, state_of(org_id))
        r = c.post("/api/sync/run")
        check("в грейсе синк не отдаёт 402", r.status_code != 402, f"status={r.status_code}")
        r = c.post("/api/order-plan/preview", json={})
        check("в грейсе расчёт не отдаёт 402", r.status_code != 402, f"status={r.status_code}")

        print("\n== Планировщик ==")
        from app import scheduler
        from app.db import SessionLocal
        from app.models import Org

        exec_sql("UPDATE billing_requests SET status = 'new', invoiced_at = NULL WHERE id = ?",
                 req_id)
        db = SessionLocal()
        try:
            db.expire_all()
            org = db.get(Org, org_id)
            check("readonly не пускают в плановый синк",
                  subscription.can_sync(org, db) is False)
            gate(False)
            check("при выключенном флаге в синк пускают всех",
                  subscription.can_sync(org, db) is True)
        finally:
            db.close()
        gate(True)
        check("_paid_only отсекает readonly", scheduler._paid_only([org_id]) == [])
        gate(False)
        check("_paid_only при выключенном флаге ничего не трогает",
              scheduler._paid_only([org_id]) == [org_id])

    print("\n== Предпросмотр перед включением флага ==")
    from app.db import SessionLocal
    from app.models import Org as OrgModel

    gate(False)
    db = SessionLocal()
    try:
        info = subscription.preview(db)
    finally:
        db.close()
    check("предпросмотр посчитал ровно одну организацию",
          sum(info["counts"].values()) == 1, str(info)[:160])
    check("предпросмотр называет тех, кого закроет",
          org_id in info["readonly_org_ids"], str(info)[:160])
    check("предпросмотр честно говорит, что флаг сейчас выключен",
          info["gate_enabled"] is False, str(info)[:160])
    # Главное свойство предпросмотра: он НИЧЕГО не пишет. Проверять это надо
    # там, где писать есть что: заявка со статусом invoiced и пустым временем —
    # ровно тот случай, в котором прежняя версия делала UPDATE прямо из
    # диагностики на старте и проставляла отметку временем деплоя.
    exec_sql("UPDATE billing_requests SET status='invoiced', invoiced_at=NULL "
             "WHERE org_id=?", org_id)
    db = SessionLocal()
    try:
        subscription.preview(db)
    finally:
        db.close()
    subscription.log_preview()
    stamp_after = sql("SELECT invoiced_at FROM billing_requests WHERE org_id=?",
                      org_id)[0][0]
    check("предпросмотр не проставил отметку «счёт выставлен»",
          stamp_after is None, str(stamp_after))
    check("а состояние при этом показывает грейс, а не отказ",
          state_of(org_id) == subscription.GRACE, state_of(org_id))
    # И наоборот: попытка ЗАПИСИ отметку ставит — один раз и в одном месте.
    gate(True)
    db = SessionLocal()
    try:
        db.expire_all()
        org = db.get(OrgModel, org_id)
        state = subscription.subscription_state(org, db, stamp=True)
    finally:
        db.close()
    check("при попытке записи состояние по-прежнему грейс",
          state == subscription.GRACE, state)
    stamp_after = sql("SELECT invoiced_at FROM billing_requests WHERE org_id=?",
                      org_id)[0][0]
    check("отметку ставит именно попытка записи", stamp_after is not None,
          str(stamp_after))
    gate(False)
    exec_sql("UPDATE billing_requests SET status='new', invoiced_at=NULL WHERE org_id=?",
             org_id)

    print("\n== Сторож: запрещено по умолчанию ==")
    from app import subscription as _sub

    mutating = mutating_routes()
    mutating_paths = {path for _m, path in mutating}
    open_paths = set(_sub.ALWAYS_OPEN_PATHS)

    check("список открытых в коде совпадает со списком в тесте",
          open_paths == set(EXPECTED_OPEN),
          f"в коде лишние: {sorted(open_paths - set(EXPECTED_OPEN))}; "
          f"в тесте лишние: {sorted(set(EXPECTED_OPEN) - open_paths)}")
    check("у каждого открытого пути записана причина",
          all(str(v).strip() for v in EXPECTED_OPEN.values()))
    check("все открытые пути существуют в приложении",
          open_paths <= mutating_paths,
          f"нет таких маршрутов: {sorted(open_paths - mutating_paths)}")
    check("открыта меньшая часть пишущих ручек",
          len(open_paths) * 2 < len(mutating_paths),
          f"{len(open_paths)} из {len(mutating_paths)}")
    check("гейт не висит поштучно на роутах (он один на приложении)",
          not gated_routes(), str(sorted(gated_routes()))[:160])

    # Синк закрыт не роутом, а единой точкой запуска: роутов, ведущих к нему,
    # больше одного (токен, склады, планировщик, догон), и по-ручечная защита
    # уже один раз оказалась дырявой.
    from app import ms_sync
    src = (ROOT / "app" / "ms_sync.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "start_sync"), None)
    calls = {n.func.id for n in ast.walk(fn) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name)} if fn else set()
    check("запуск синка проверяет подписку в одной точке",
          "_subscription_allows_sync" in calls, str(sorted(calls))[:200])
    gate(True)
    set_org(org_id, source="saas", plan="start", paid_until=D(-30),
            trial_ends_at=f"{D(-60)} 00:00:00", status="active")
    exec_sql("UPDATE billing_requests SET status='new', invoiced_at=NULL WHERE org_id=?",
             org_id)
    check("readonly не может запустить синк вообще",
          ms_sync.start_sync(org_id, mode="incremental") is False)
    gate(False)

    print("\n== «Оплачено до», введённое руками в чужом формате ==")
    # Колонку заполняет человек командой UPDATE на боевом сервере, а соседняя
    # trial_ends_at — DATETIME. Оператор, скопировавший её формат, со строгим
    # типом ронял ЗАГРУЗКУ строки orgs — то есть 500 на каждой странице,
    # включая «Тарифы»: заплативший клиент оставался с мёртвым аккаунтом.
    # Проверяем через HTTP, а не через ORM: первая версия защиты работала
    # только на записи, а падение было на чтении, и тест этого не видел.
    gate(False)
    with TestClient(oborot_app, headers={"X-Oborot-CSRF": "1"}) as c:
        c.post("/login", data={"email": "gate@test.io", "password": "secret123"})
        for raw, expect_state in (
            (f"{D(30)} 00:00:00", subscription.ACTIVE),
            ("31.12.2099", subscription.ACTIVE),
            (f"{D(30)}T00:00:00", subscription.ACTIVE),
            ("мусор", subscription.READONLY),
            ("", subscription.READONLY),
        ):
            set_org(org_id, paid_until=raw, plan="start",
                    trial_ends_at=f"{D(-60)} 00:00:00")
            pages = {
                "/": c.get("/"),
                "/plans": c.get("/plans"),
                "/api/subscription": c.get("/api/subscription"),
                "/api/settings": c.get("/api/settings"),
                "/api/orders": c.get("/api/orders"),
            }
            bad = {k: r.status_code for k, r in pages.items() if r.status_code != 200}
            check(f"«{raw or 'пусто'}» не роняет ни одной страницы", not bad, str(bad))
            check(f"«{raw or 'пусто'}» даёт состояние {expect_state}",
                  state_of(org_id) == expect_state, state_of(org_id))
        # Непонятная дата = «не оплачено», но она НЕ должна ещё и открывать
        # доступ: fail-open внутри проверки синка превратил бы опечатку
        # в бесплатную работу.
        set_org(org_id, paid_until="мусор")
        gate(True)
        from app import ms_sync as _msync
        check("организация с непонятной датой в синк не пускается",
              _msync.start_sync(org_id, mode="incremental") is False)
        gate(False)

    print("\n== Одна битая строка не валит обход организаций ==")
    # Снисходительный тип съедает любой человеческий ввод, поэтому «битую»
    # организацию делаем честно: подменяем вычисление состояния так, чтобы на
    # одной из них оно падало. Проверяем ровно то, ради чего стоит перехват:
    # одна проблемная организация не должна лишать синка ВСЕХ остальных —
    # именно так ночной синк вставал бы молча, а страницы у всех были живыми.
    # Заодно: trial_ends_at теперь такой же снисходительный, как paid_until —
    # его правят руками ровно так же («продлить пилоту триал»), и строгий тип
    # ронял загрузку строки, то есть все страницы этой организации.
    exec_sql("INSERT INTO orgs (id, name, plan, settings_json, created_at, "
             "trial_ends_at, paid_until) VALUES (4242, 'Битая', 'trial', '{}', "
             "'2026-01-01 00:00:00', '31.12.2099', ?)", D(365))
    check("триал, введённый руками в формате ДД.ММ.ГГГГ, читается",
          sql("SELECT trial_ends_at FROM orgs WHERE id=4242")[0][0] == "31.12.2099")
    db = SessionLocal()
    try:
        broken_org = db.get(OrgModel, 4242)
        check("и не роняет загрузку строки организации",
              broken_org is not None and broken_org.trial_ends_at is not None,
              str(getattr(broken_org, "trial_ends_at", "нет строки")))
    finally:
        db.close()
    set_org(org_id, paid_until=D(365), plan="start")
    gate(True)
    real_state = subscription.subscription_state

    def _boom(org, db, **kw):
        if org.id == 4242:
            raise ValueError("состояние вычислить не удалось")
        return real_state(org, db, **kw)

    subscription.subscription_state = _boom
    try:
        ok_ids = scheduler._paid_only([org_id, 4242])
        check("обход пережил проблемную организацию", isinstance(ok_ids, list), str(ok_ids))
        check("здоровая организация из обхода НЕ выпала", org_id in ok_ids, str(ok_ids))
        check("сомнение толкуется в пользу синка", 4242 in ok_ids, str(ok_ids))
        db = SessionLocal()
        try:
            info = subscription.preview(db)
        finally:
            db.close()
        check("предпросмотр посчитал остальных", info["counts"][subscription.ACTIVE] == 1,
              str(info)[:200])
        check("и назвал ту, которую не смог посчитать",
              info["broken_org_ids"] == [4242], str(info)[:200])
        subscription.log_preview()  # не должен падать
    finally:
        subscription.subscription_state = real_state
    gate(False)
    exec_sql("DELETE FROM orgs WHERE id = 4242")

    print("\n== Миграция на старой базе ==")
    old_db = ROOT / "test_subscription_old.db"
    if old_db.exists():
        old_db.unlink()
    eng = create_engine(f"sqlite:///{old_db}")
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE orgs (id INTEGER PRIMARY KEY, name TEXT)"))
        conn.execute(text(
            "CREATE TABLE billing_requests (id INTEGER PRIMARY KEY, org_id INTEGER, "
            "status TEXT)"
        ))
        conn.execute(text("INSERT INTO orgs (id, name) VALUES (1, 'Старый бренд')"))
    subscription.ensure_schema(bind=eng)
    subscription.ensure_schema(bind=eng)  # идемпотентность
    with eng.connect() as conn:
        org_cols = {r[1] for r in conn.execute(text("PRAGMA table_info(orgs)"))}
        br_cols = {r[1] for r in conn.execute(text("PRAGMA table_info(billing_requests)"))}
        rows = conn.execute(text("SELECT paid_until FROM orgs WHERE id = 1")).fetchall()
    check("миграция добавила orgs.paid_until", "paid_until" in org_cols, str(org_cols))
    check("миграция добавила billing_requests.invoiced_at",
          "invoiced_at" in br_cols, str(br_cols))
    check("старая запись выжила, paid_until пустой",
          rows == [(None,)], str(rows))
    eng.dispose()
    old_db.unlink(missing_ok=True)

    operator_journey()

    print(f"\nИтого: {len(PASS)} OK, {len(FAIL)} FAIL")
    for name in FAIL:
        print(f"  FAIL {name}")
    return 1 if FAIL else 0


# ── PILOT-OPERATOR-JOURNEY-1: один сквозной путь оператора ──────────────────
#
# Пункт 6 отчёта приёмки (issuecomment-5838277250): по отдельности были
# доказаны поведение приложения и атомарность буквальной команды из
# `deploy/README.md`, но НИ ОДНОГО прохода «заявка → счёт → оплата → продление
# → истечение» на ОДНОЙ организации и ОДНОЙ заявке не существовало. Проверки
# выше собирают состояния фикстурами — каждая правдива про свой кусок и ни
# одна не доказывает, что куски стыкуются.
#
# ЧТО ЭТОТ ПРОХОД НЕ ДОКАЗЫВАЕТ, и это сказано здесь, а не в примечании мелким
# шрифтом: отметка `status='paid'` — ИМИТАЦИЯ поступления денег, а не
# банковская проверка. Проход доказывает связность приложения и
# документированной процедуры, и ничего про фактический платёж, личность
# оператора и восстановление доступа.


def documented_atomic(*statements: str) -> None:
    """Выполнить пару UPDATE ОДНОЙ транзакцией — как написано в README.

    Форма совпадает с документом: `BEGIN IMMEDIATE … COMMIT`. Здесь она
    прогоняется через ту же базу, с которой работает приложение, поэтому
    проверяется стыковка процедуры и продукта, а не пересказ процедуры.

    `isolation_level=None` снимает неявное управление транзакцией у драйвера:
    иначе BEGIN/COMMIT в скрипте спорили бы с его собственным.
    """
    con = sqlite3.connect(DB_PATH, isolation_level=None)
    try:
        con.executescript("BEGIN IMMEDIATE;\n" + "\n".join(statements) + "\nCOMMIT;")
    finally:
        con.close()


class FakeToday:
    """Синтетические часы: сдвигаем «сегодня», НЕ трогая купленные даты.

    Истечение проверяется сменой дня, а не переписыванием `paid_until`.
    Переписать срок значило бы проверить фикстуру: купленная дата — это то,
    за что заплатили, и в проходе она обязана оставаться неизменной.
    """

    def __init__(self, offset_days: int):
        self.offset = offset_days
        self.original = subscription._today

    def __enter__(self):
        shift = timedelta(days=self.offset)
        subscription._today = lambda: datetime.utcnow().date() + shift
        return self

    def __exit__(self, *exc):
        subscription._today = self.original
        return False


def operator_journey() -> None:  # noqa: C901 — один связный путь, ветвлений нет
    print("\n== Сквозной путь оператора: заявка → счёт → оплата → продление → истечение ==")
    gate(True)
    with TestClient(oborot_app, headers={"X-Oborot-CSRF": "1"}) as c:
        # ── Шаг 0. Две организации: целевая и соседняя ────────────────────
        # ПОРЯДОК РЕГИСТРАЦИИ ЗДЕСЬ ЗНАЧИМ. `/register` логинит клиента, и
        # сессия принадлежит последней зарегистрированной организации. Сосед
        # заводится ПЕРВЫМ, целевая — последней: иначе заявка на счёт уходит
        # от чужого имени, и проход молча проверяет не ту организацию.
        #
        # Сосед заводится ТОЙ ЖЕ настоящей ручкой, а не INSERT-ом: собранная
        # руками строка расходится со схемой (и разошлась — `settings_json`
        # NOT NULL), а «сосед», которого продукт создать не умеет, ничего не
        # доказывает про невмешательство.
        c.post("/register", data={"name": "сосед", "email": "neighbour@test.io",
                                  "password": "secret123", "org_name": "Сосед-бренд"})
        neighbour = sql("SELECT id FROM orgs WHERE name = ?", "Сосед-бренд")[0][0]
        set_org(neighbour, plan="start", trial_ends_at=f"{D(-30)} 00:00:00",
                paid_until=D(400))

        c.post("/register", data={"name": "оператор", "email": "journey@test.io",
                                  "password": "secret123", "org_name": "Путь-бренд"})
        org = sql("SELECT id FROM orgs WHERE name = ?", "Путь-бренд")[0][0]
        check("0. целевая и соседняя организации — разные",
              org != neighbour, f"org={org} neighbour={neighbour}")
        neighbour_paid = sql("SELECT paid_until FROM orgs WHERE id = ?", neighbour)[0][0]

        def neighbour_untouched(where: str) -> None:
            now = sql("SELECT paid_until FROM orgs WHERE id = ?", neighbour)[0][0]
            check(f"сосед не тронут: {where}",
                  now == neighbour_paid and state_of(neighbour) == subscription.ACTIVE,
                  f"paid_until={now} state={state_of(neighbour)}")

        # Триал кончился — это честная отправная точка того, кому пора платить.
        set_org(org, plan="start", trial_ends_at=f"{D(-1)} 00:00:00", paid_until=None)
        # Сессия принадлежит ЦЕЛЕВОЙ организации, и проверяется это РАЗЛИЧАЮЩИМ
        # признаком, а не тем, что ответ непустой: у соседа оплачено до D(400),
        # у целевой срока нет вовсе.
        mine = c.get("/api/subscription").json()
        check("   и сессия принадлежит целевой, а не соседу",
              mine.get("paid_until") is None,
              f"payload.paid_until={mine.get('paid_until')}")

        check("1. до оплаты организация в readonly",
              state_of(org) == subscription.READONLY, state_of(org))
        # ТЕЛО ВЕРНОЕ (`page`, а не `key`), и это принципиально: гейт стоит
        # ПЕРЕД разбором тела, поэтому с неверным телом 402 приходил бы и от
        # опечатки. Верное тело означает, что 402 — это именно запрет записи.
        blocked = c.post("/api/hints/seen", json={"page": "turnover"})
        check("   и запись действительно закрыта гейтом", blocked.status_code == 402,
              f"status={blocked.status_code}")

        # ── Шаг 1. Заявка на счёт настоящей ручкой ───────────────────────
        r = c.post("/api/plans/request", json={
            "plan": "start", "period": "month", "company": "ООО Путь-бренд",
            "inn": "7701234567", "email": "journey@test.io",
            "phone": "+70000000001",
        })
        check("2. заявка на счёт проходит даже из readonly",
              r.status_code == 200, f"status={r.status_code} {r.text[:120]}")
        rows = sql("SELECT id, org_id, status, plan, period FROM billing_requests"
                   " WHERE org_id = ? ORDER BY id DESC LIMIT 1", org)
        check("   заявка записана и привязана к организации", bool(rows), str(rows))
        req_id, req_org, req_status, req_plan, req_period = rows[0]

        # ── Шаг 2. Оператор сверяет заявку ───────────────────────────────
        # Это тот самый шаг, который README требует делать глазами. Здесь он
        # выражен утверждением по реальным данным: та ли организация и тот ли
        # согласованный тариф с периодом.
        check("3. сверка оператора: заявка принадлежит ИМЕННО этой организации",
              req_org == org and req_org != neighbour,
              f"req.org_id={req_org} org={org} neighbour={neighbour}")
        check("   и несёт согласованные тариф и период",
              (req_plan, req_period) == ("start", "month"),
              f"{req_plan}/{req_period}")
        check("   новая заявка ещё не даёт ни дня доступа",
              req_status == "new" and state_of(org) == subscription.READONLY,
              f"status={req_status} state={state_of(org)}")
        neighbour_untouched("после заявки")

        # ── Шаг 3. Документированный шаг «счёт выставлен» ────────────────
        exec_sql("UPDATE billing_requests SET status = 'invoiced',"
                 " invoiced_at = datetime('now') WHERE id = ?", req_id)
        check("4. после выставленного счёта — грейс",
              state_of(org) == subscription.GRACE, state_of(org))
        # 200, а НЕ «не 402». Первая редакция этой проверки спрашивала
        # `!= 402` и зеленела на 422 — то есть доказывала, что тело неверное,
        # а не что запись прошла. Пустая проверка хуже отсутствующей.
        wrote = c.post("/api/hints/seen", json={"page": "turnover"})
        check("   и запись в грейсе ДЕЙСТВИТЕЛЬНО проходит (200)",
              wrote.status_code == 200, f"status={wrote.status_code}")

        # ── Шаг 4. Границы грейса синтетическими часами ──────────────────
        # invoiced_at НЕ переписывается: двигаем «сегодня».
        stamped = sql("SELECT invoiced_at FROM billing_requests WHERE id = ?", req_id)[0][0]
        with FakeToday(subscription.GRACE_DAYS):
            check("5. пятый день грейса ещё грейс",
                  state_of(org) == subscription.GRACE, state_of(org))
        with FakeToday(subscription.GRACE_DAYS + 1):
            check("   шестой день — грейс кончился",
                  state_of(org) == subscription.READONLY, state_of(org))
        check("   отметка о счёте при этом не переписывалась",
              sql("SELECT invoiced_at FROM billing_requests WHERE id = ?",
                  req_id)[0][0] == stamped, str(stamped))

        # ── Шаг 5. Документированная АТОМАРНАЯ пара «оплата + срок» ──────
        # ИМИТАЦИЯ поступления денег. Банковской проверки здесь нет и быть не
        # может: мы лишь отмечаем то, что оператор увидел в выписке.
        paid_until = D(30)
        documented_atomic(
            f"UPDATE billing_requests SET status='paid' WHERE id = {req_id};",
            f"UPDATE orgs SET paid_until='{paid_until}' WHERE id = {org};")
        after = sql("SELECT (SELECT status FROM billing_requests WHERE id = ?),"
                    " (SELECT paid_until FROM orgs WHERE id = ?)", req_id, org)[0]
        check("6. документированная пара применилась целиком",
              after == ("paid", paid_until), str(after))
        check("   и это ТА ЖЕ заявка и ТА ЖЕ организация, что в шаге 3",
              req_id == rows[0][0] and req_org == org, f"req={req_id} org={org}")

        # ── Шаг 6. Право доступа приложения ──────────────────────────────
        check("7. приложение признаёт оплату — active",
              state_of(org) == subscription.ACTIVE, state_of(org))
        w = c.post("/api/hints/seen", json={"page": "replenish"})
        check("   запись после оплаты ДЕЙСТВИТЕЛЬНО проходит (200)",
              w.status_code == 200, f"status={w.status_code}")
        rd = c.get("/api/subscription").json()
        check("   интерфейс сообщает, что запись не блокируется",
              rd.get("state") == "active" and rd.get("writes_blocked") is False,
              str(rd)[:160])
        neighbour_untouched("после оплаты")

        # ── Шаг 7. Продление тем же документированным способом ───────────
        renewed = D(60)
        documented_atomic(
            f"UPDATE orgs SET paid_until='{renewed}' WHERE id = {org};")
        check("8. продление записано и организация по-прежнему active",
              sql("SELECT paid_until FROM orgs WHERE id = ?", org)[0][0] == renewed
              and state_of(org) == subscription.ACTIVE,
              f"paid_until={sql('SELECT paid_until FROM orgs WHERE id = ?', org)[0][0]}")

        # ── Шаг 8. Включительное истечение — часами, не правкой срока ────
        with FakeToday(60):
            check("9. последний оплаченный день ещё active",
                  state_of(org) == subscription.ACTIVE, state_of(org))
        with FakeToday(61):
            check("   следующий день — readonly",
                  state_of(org) == subscription.READONLY, state_of(org))
            # Запись закрыта, а чтение и путь оплаты — нет.
            w2 = c.post("/api/hints/seen", json={"page": "turnover"})
            check("   запись после истечения закрыта гейтом (верное тело, 402)",
                  w2.status_code == 402, f"status={w2.status_code}")
            for path in ("/api/subscription", "/api/turnover", "/"):
                resp = c.get(path)
                check(f"   чтение остаётся открытым: {path}",
                      resp.status_code == 200, f"status={resp.status_code}")
            again = c.post("/api/plans/request", json={
                "plan": "start", "period": "month", "company": "ООО Путь-бренд",
                "inn": "7701234567", "email": "journey@test.io",
                "phone": "+70000000001",
            })
            check("   и путь оплаты снова открыт", again.status_code == 200,
                  f"status={again.status_code}")
        check("10. купленный срок за весь проход не переписывался часами",
              sql("SELECT paid_until FROM orgs WHERE id = ?", org)[0][0] == renewed,
              sql("SELECT paid_until FROM orgs WHERE id = ?", org)[0][0])
        neighbour_untouched("после истечения")
    gate(False)


if __name__ == "__main__":
    sys.exit(main())
