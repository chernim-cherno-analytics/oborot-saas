# -*- coding: utf-8 -*-
"""Гейт подписки (D-24): одно состояние active | grace | readonly.

Почему это отдельный набор. Гейт — единственная фича, которая умеет ЗАКРЫТЬ
работающему клиенту доступ. Цена ошибки несимметрична: пропустить неплательщика
— потерять деньги за месяц, закрыть плательщика — потерять клиента. Поэтому
здесь проверяется не только «блокирует», но и, прежде всего, «НЕ блокирует»:
чтение, экспорт, вход/аккаунт, страницу тарифов и саму заявку на счёт.

Проверяется:
  1) выключенный по умолчанию флаг: без OBOROT_SUBSCRIPTION_GATE не блокируется
     ничего, даже у организации с истёкшим триалом;
  2) машина состояний: триал, paid_until, грейс от отметки «счёт выставлен»,
     организации из каталога МойСклад, suspended;
  3) грейс ровно 5 календарных дней и ни днём больше;
  4) с включённым флагом readonly закрывает все изменения бизнес-данных,
     синк, запись в МС, расчёт и сохранение планов — и НИ ОДНОГО чтения;
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

# Все изменения бизнес-данных организации. Это и есть буквальный смысл
# readonly по D-24: прежние данные видны, новые действия недоступны. Правка
# множества обязана сопровождаться правкой BUSINESS_LOGIC §11.
EXPECTED_GATED = {
    # Подключение, синк и внешняя запись.
    ("POST", "/api/connect/moysklad"),
    ("POST", "/api/connect/moysklad/stores"),
    ("POST", "/api/warehouses/{warehouse_id}/toggle"),
    ("POST", "/api/sync/initial"),
    ("POST", "/api/sync/run"),
    ("POST", "/api/orders/{order_id}/push-to-ms"),
    # Заказы, исполнение и фактическое «едет к нам».
    ("POST", "/api/orders"),
    ("POST", "/api/orders/{order_id}/status"),
    ("POST", "/api/orders/{order_id}/receipts"),
    ("DELETE", "/api/orders/{order_id}"),
    ("POST", "/api/ordered"),
    ("POST", "/api/ordered/add"),
    # Настройки расчётов и пользовательские бизнес-справочники.
    ("POST", "/api/settings"),
    ("POST", "/api/exclusions"),
    ("POST", "/api/hidden"),
    ("POST", "/api/categories/override"),
    ("POST", "/api/categories/merge"),
    ("POST", "/api/discount-rule"),
    ("POST", "/api/discount-overrides"),
    ("POST", "/api/discount-overrides/defaults"),
    ("POST", "/api/replenish-draft"),
    ("POST", "/api/replenish-draft/reset"),
    ("POST", "/api/productions"),
    ("POST", "/api/productions/{pid}"),
    ("POST", "/api/productions/{pid}/setup"),
    ("POST", "/api/productions/assign-rule"),
    ("POST", "/api/productions/assign"),
    ("DELETE", "/api/productions/{pid}"),
    # Новые рекомендации и превращение их в заказ.
    ("POST", "/api/order-plan/preview"),
    ("POST", "/api/order-plan"),
    ("POST", "/api/order-plan/{plan_id}/apply"),
    # Демо стирает данные организации перед засевом — для readonly это
    # невосстановимая потеря там, где мы уже ничего не продаём.
    ("POST", "/api/connect/demo"),
}

# Пишущие ручки, которые гейт закрывать НЕ должен, с причиной. Список
# обязателен: первая версия сторожа сверяла только множество ЗАКРЫТЫХ, и
# новая незакрытая ручка была для неё невидима. Ровно так и проскочило
# сохранение токена МойСклада, запускавшее полный синк организации, которой
# мы отказали в записи. Теперь КАЖДАЯ пишущая ручка обязана быть в одном из
# двух списков — иначе тест падает и требует решения.
EXPECTED_OPEN = {
    # Деньги: клиент обязан иметь возможность заплатить.
    ("POST", "/api/plans/request"): "заявка на счёт — единственный путь к оплате",
    # Экспорт и чтение своих данных.
    ("POST", "/api/export/replenish.xlsx"): "экспорт своих же данных",
    # Вход, выход, аккаунт.
    ("POST", "/login"): "вход",
    ("POST", "/register"): "регистрация",
    ("POST", "/logout"): "выход",
    ("POST", "/api/account/password"): "смена пароля",
    ("POST", "/api/account/delete"): "удаление аккаунта",
    # Коммуникационные и интерфейсные предпочтения не меняют бизнес-данные.
    ("POST", "/api/notify/settings"): "настройки уведомлений",
    ("POST", "/api/notify/test"): "проверка своего же телеграм-канала",
    ("POST", "/api/hints/seen"): "подсказки",
    ("POST", "/api/prefs/hints"): "подсказки",
    ("POST", "/api/lessons/{key}/done"): "обучение",
    ("POST", "/api/lessons/reset"): "обучение",
    ("POST", "/api/lessons/{key}/reset"): "обучение",
    # Ручки жизненного цикла приложения МойСклада: их вызывает САМ МойСклад
    # по своему JWT, а не пользователь. Закрыть их гейтом значило бы не пустить
    # МС сообщить нам, что подписку активировали или сняли, — то есть закрыть
    # ровно тот канал, которым состояние такой организации и управляется.
    ("PUT", "/ms/vendor/api/moysklad/vendor/1.0/apps/{path_app_id}/{account_id}"):
        "lifecycle МойСклада (Activate), вызывает МС по своему JWT",
    ("DELETE", "/ms/vendor/api/moysklad/vendor/1.0/apps/{path_app_id}/{account_id}"):
        "lifecycle МойСклада (Suspend/Uninstall), вызывает МС по своему JWT",
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

        print("\n== Флаг включён: readonly закрывает запись ==")
        gate(True)
        blocked = {
            "подключение МойСклада": c.post("/api/connect/moysklad", json={}),
            "синхронизация (инкрементальная)": c.post("/api/sync/run"),
            "синхронизация (первичная)": c.post("/api/sync/initial"),
            "запись в МойСклад": c.post("/api/orders/1/push-to-ms"),
            "создание ручного заказа": c.post("/api/orders", json={}),
            "запись факта приёмки": c.post("/api/orders/1/receipts", json={}),
            "изменение настроек расчёта": c.post("/api/settings", json={}),
            "изменение справочника": c.post("/api/productions", json={}),
            "ручная правка рекомендации": c.post("/api/replenish-draft", json={}),
            "расчёт плана": c.post("/api/order-plan/preview", json={}),
            "сохранение плана": c.post("/api/order-plan", json={}),
            "применение плана": c.post("/api/order-plan/1/apply", json={}),
        }
        for label, resp in blocked.items():
            check(f"readonly закрывает: {label}", resp.status_code == 402,
                  f"status={resp.status_code}")
        check("402 объясняет причину человеку",
              "подписк" in blocked["синхронизация (инкрементальная)"].text.lower(),
              blocked["синхронизация (инкрементальная)"].text[:120])

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

    print("\n== Сторож по всем пишущим ручкам ==")
    found = gated_routes()
    mutating = mutating_routes()
    check("гейт стоит ровно на согласованных ручках", found == EXPECTED_GATED,
          f"лишние={sorted(found - EXPECTED_GATED)} потерянные={sorted(EXPECTED_GATED - found)}")
    check("все закрытые ручки действительно пишущие",
          found <= mutating, f"не-POST={sorted(found - mutating)}")
    check("гейт не стоит ни на одном GET",
          not any(m == "GET" for m, _ in found), str(sorted(found))[:200])
    check("открыты только явно разрешённые служебные и account-ручки",
          found == mutating - set(EXPECTED_OPEN),
          f"закрыто {len(found)} из {len(mutating)}")

    # Вторая половина сторожа — та, которой не было и из-за которой мимо гейта
    # проскочило сохранение токена, запускавшее полный синк. Каждая пишущая
    # ручка приложения обязана быть либо в EXPECTED_GATED, либо в EXPECTED_OPEN
    # с причиной. Новая ручка роняет тест и требует осознанного решения.
    classified = set(EXPECTED_GATED) | set(EXPECTED_OPEN)
    unclassified = mutating - classified
    check("каждая пишущая ручка осознанно отнесена к закрытым или открытым",
          not unclassified, f"не классифицированы: {sorted(unclassified)}")
    check("в списке открытых нет ручек, которых больше нет в приложении",
          not (set(EXPECTED_OPEN) - mutating),
          f"лишние: {sorted(set(EXPECTED_OPEN) - mutating)}")
    check("списки закрытых и открытых не пересекаются",
          not (set(EXPECTED_GATED) & set(EXPECTED_OPEN)),
          str(sorted(set(EXPECTED_GATED) & set(EXPECTED_OPEN))))
    check("у каждой открытой пишущей ручки записана причина",
          all(str(v).strip() for v in EXPECTED_OPEN.values()))

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

    print(f"\nИтого: {len(PASS)} OK, {len(FAIL)} FAIL")
    for name in FAIL:
        print(f"  FAIL {name}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
