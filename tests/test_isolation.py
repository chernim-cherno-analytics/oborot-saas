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
  7) СТОРОЖ ПОЛНОТЫ ОБХОДА — пункт 1 обещает «все маршруты, принимающие id
     в пути», но до 28.08.2026 держался на списке, набранном руками, и список
     разошёлся с кодом: `/api/orders/{order_id}/receipts` (GET и POST) и
     `/api/order-plan/{plan_id}/outcome|brief` в приложении появились, а в
     обход не попали — и уронить тест было нечему. Теперь инвентарь снимается
     с живой таблицы маршрутов FastAPI, покрытие регистрируется самим фактом
     выполнения пробы, и каждый маршрут с параметром пути обязан быть либо
     пройден пробой, либо внесён в узкий реестр исключений с причиной.
     Проба не принимает готовый URL: он собирается из зарегистрированного
     шаблона и резолвится обратно в тот же маршрут — иначе шаблон и URL были
     бы двумя источниками правды, и скопированная проба с новым шаблоном при
     старом URL записала бы покрытие на невызванную ручку.

Запуск из корня репозитория:  python tests/test_isolation.py
"""
import os
import re
import sqlite3
import sys
import threading
import time
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "test_isolation.db"
# Порты берутся из окружения: так tests/run_all.py разводит наборы и
# может гонять их параллельно. Значения по умолчанию — прежние.
APP_PORT = int(os.environ.get("OBOROT_TEST_PORT", "8806"))

# Окружение — ДО импорта приложения (db.py читает DATABASE_URL при импорте).
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["SCHEDULER_ENABLED"] = "0"

if DB_PATH.exists():
    DB_PATH.unlink()

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from starlette.routing import Match  # noqa: E402

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


def receipt_counts(org_a: int, org_b: int) -> tuple:
    """Снимок таблицы приёмок: (строк и штук у A, у B, строк всего).

    Итог по всей таблице считается отдельно от организаций намеренно: строка,
    записанная с третьим `org_id` (или с нулём вместо него), в разрезе A и B
    не видна, а таблицу уже испортила.
    """
    a_row = sql("SELECT COUNT(*), COALESCE(SUM(qty),0) FROM order_receipts"
                " WHERE org_id=?", org_a)[0]
    b_row = sql("SELECT COUNT(*), COALESCE(SUM(qty),0) FROM order_receipts"
                " WHERE org_id=?", org_b)[0]
    total = sql("SELECT COUNT(*) FROM order_receipts")[0][0]
    return (tuple(a_row), tuple(b_row), total)


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


# ── Реестр маршрутов: сторож полноты обхода (§7) ─────────────────────────────
#
# Инвентарь маршрутов снимается с живого приложения, а не набирается руками.
# Ручной список ровно один раз уже разошёлся с кодом — и разошёлся молча,
# потому что «список не полон» не является событием, на которое что-то падает.

# Маршруты с параметром пути, которые НЕ адресуют объект арендатора по
# идентификатору. Реестр намеренно короткий, и у каждой записи есть причина:
# исключение — это утверждение «здесь нечему утечь», а утверждение должно быть
# проверяемым. Сам реестр тоже под сторожем (см. run_all §7): у исключённого
# маршрута ни один параметр пути не может быть объявлен целым числом, поэтому
# спрятать сюда `/api/orders/{order_id}` и закрыть вопрос не получится.
ID_ROUTE_EXCLUSIONS = {
    ("/api/lessons/{key}/done", "POST"):
        "{key} — ключ урока из каталога app/lessons.py (строка справочника, "
        "а не строка базы); прогресс привязан к ctx.user.id, объект другой "
        "организации по этому пути не адресуется",
    ("/api/lessons/{key}/reset", "POST"):
        "то же самое: ключ урока из того же каталога, прогресс по ctx.user.id",
    ("/ms/vendor/api/moysklad/vendor/1.0/apps/{path_app_id}/{account_id}", "PUT"):
        "lifecycle-ручка вендора МойСклад: арендаторской сессии здесь нет "
        "вовсе, вход по vendor JWT (app.ms_vendor.verify_incoming_jwt), а "
        "{account_id} — внешний идентификатор аккаунта МС, не id нашей строки",
    ("/ms/vendor/api/moysklad/vendor/1.0/apps/{path_app_id}/{account_id}", "DELETE"):
        "то же самое, деактивация приложения в чужом аккаунте МС",
}

# Пары (шаблон маршрута, метод), по которым проба чужим идентификатором
# ДЕЙСТВИТЕЛЬНО выполнена. Заполняется исполнением `probe_foreign`, а не
# литералом: список покрытия, набранный рядом с проверками, — это второй
# ручной реестр, и разойдётся он так же, как разошёлся первый.
PROBED_ID_ROUTES: set = set()


def iter_routes(routes):
    """Плоский обход таблицы маршрутов приложения.

    Спуск через `original_router` обязателен. В FastAPI 0.141 подключённый
    роутер лежит в `app.routes` объектом `fastapi.routing._IncludedRouter`,
    у которого нет ни `path`, ни `routes`: наивный обход `app.routes` не
    находит НИ ОДНОГО маршрута из `app/api.py`, и сторож полноты молча стал бы
    сторожем пустого множества — то есть был бы хуже, чем его отсутствие.
    Поэтому §7 отдельно проверяет, что инвентарь непустой.
    """
    for route in routes:
        original = getattr(route, "original_router", None)
        if original is not None:
            yield from iter_routes(getattr(original, "routes", ()))
            continue
        nested = getattr(route, "routes", None)
        if nested:
            yield from iter_routes(nested)
            continue
        yield route


def id_route_inventory() -> dict:
    """{(шаблон пути, метод): маршрут} для всех маршрутов с параметром в пути.

    Из методов отбрасывается ровно один вид шума — HEAD, который Starlette
    дописывает к каждому GET сам. За таким HEAD не стоит отдельной ручки: это
    тот же обработчик, и проба GET его уже проходит.

    Отбрасывать HEAD и OPTIONS безусловно, «потому что их добавляет фреймворк»,
    нельзя. У маршрута, объявленного явно через `@router.head(...)` или
    `@router.options(...)`, других методов нет вовсе, и такой фильтр вычёркивал
    бы из инвентаря ВЕСЬ обработчик: §7 остался бы зелёным на арендаторской
    ручке, которую никто не пробовал и не исключал (ревью PR #42,
    discussion_r3879412309). Признак происхождения ровно один и он надёжен:
    автоматический HEAD всегда идёт в паре с GET на том же маршруте, а явный
    приходит один. OPTIONS Starlette не добавляет вовсе — preflight отвечал бы
    CORS-middleware, которого в приложении нет и появление которого сторожит
    §5, — поэтому любой OPTIONS в таблице объявлен руками и считается.

    Маршрут, у которого GET и HEAD объявлены руками одной строкой
    (`methods=["GET", "HEAD"]`), от автоматической пары неотличим, и HEAD у
    него тоже отбрасывается. Потери покрытия здесь нет: обработчик один и тот
    же, и проба GET доходит до него.
    """
    found = {}
    for route in iter_routes(oborot_app.routes):
        path = getattr(route, "path", "") or ""
        if "{" not in path:
            continue
        methods = set(getattr(route, "methods", None) or ())
        if "GET" in methods:
            methods.discard("HEAD")
        for method in methods:
            found[(path, method)] = route
    return found


def dispatch_route(scope: dict):
    """Какой маршрут ФАКТИЧЕСКИ обработает запрос — по порядку, как роутер.

    Starlette перебирает маршруты в порядке регистрации и отдаёт запрос
    ПЕРВОМУ, совпавшему целиком. Поэтому спросить `target.matches(scope)` мало:
    это вопрос «может ли ЭТОТ маршрут принять URL», а не «кто его примет».
    Два шаблона способны совпасть с одним concrete URL — у FastAPI аннотация
    `int` в сигнатуре в регулярное выражение пути не попадает, и
    `/api/x/{order_id}` и `/api/x/{other}` компилируются в одно и то же
    `[^/]+`; литеральный `/api/orders/open` тоже перекрывает
    `/api/orders/{order_id}`. Тогда запрос уходит в маршрут, объявленный
    раньше, а покрытие записалось бы на тот, который никто не вызывал
    (ревью PR #42, discussion_r3879544096). Возвращается ровно тот объект
    маршрута, который выберет роутер, — сравнивать с целью нужно по
    идентичности, а не по совпадению пути.
    """
    for route in iter_routes(oborot_app.routes):
        matches = getattr(route, "matches", None)
        if matches is None:
            continue
        if matches(scope)[0] is Match.FULL:
            return route
    return None


def int_path_params(route) -> list:
    """Параметры пути маршрута, объявленные целым числом.

    В этом приложении целочисленный параметр пути означает первичный ключ
    строки в базе (`_id_path()` в `app/api.py` — `Path(ge=1, le=2_147_483_647)`),
    то есть ровно тот случай, ради которого §1 и существует.

    Тип берётся из РАЗОБРАННОГО FastAPI маршрута — `route.dependant.path_params`,
    поле `field_info.annotation`, — а не из сырых `__annotations__` функции.
    Сырая аннотация зависит от того, как её написали: `order_id: int` даёт
    `int`, а равносильный `order_id: Annotated[int, Path(ge=1)]` даёт объект
    `Annotated[...]`, который на `int` не похож и мимо сравнения проходит.
    Тогда арендаторский id считался бы не-целочисленным, и сторож реестра
    исключений (§7) пропустил бы в исключения ручку с настоящим id объекта —
    то есть проверка, которая должна закрывать самый опасный вид ошибки в
    реестре, зависела бы от стиля записи аннотации. FastAPI обе формы уже
    свёл к одному разрешённому типу, и здесь берётся именно он.

    Строковые ключи справочников (`/api/lessons/{key}`) и внешние
    идентификаторы (`{account_id}` у вендора МойСклад) остаются не-целыми при
    любой форме записи — у них разрешённый тип `str`.

    Если у маршрута разобранного `dependant` нет вовсе (обычный
    starlette-`Route`, добавленный в обход FastAPI), тип берётся из
    `__annotations__` как раньше. Это не запасной путь «на всякий случай»:
    без него такой маршрут молча получал бы пустой список, и исключение для
    него прошло бы проверку просто потому, что тип не удалось прочитать.
    """
    dependant = getattr(route, "dependant", None)
    fields = getattr(dependant, "path_params", None)
    if fields:
        names = []
        for field in fields:
            annotation = getattr(getattr(field, "field_info", None),
                                 "annotation", None)
            if annotation is int:
                names.append(getattr(field, "name", ""))
        return sorted(n for n in names if n)

    endpoint = getattr(route, "endpoint", None)
    if endpoint is None:
        return []
    hints = getattr(endpoint, "__annotations__", None) or {}
    names = re.findall(r"\{([^}:]+)", getattr(route, "path", "") or "")
    return sorted(n for n in names if hints.get(n) in (int, "int"))


def concrete_url(route: str, params: dict) -> str:
    """Подставить значения в ЗАРЕГИСТРИРОВАННЫЙ шаблон маршрута.

    URL пробе отдельной строкой не передаётся, и это не удобство, а требование.
    Отдельная строка — второй источник правды рядом с шаблоном, и разойтись
    они могут молча: скопированная проба, у которой шаблон поменяли на новый
    маршрут, а URL оставили от соседнего, уходит на СТАРЫЙ endpoint, честно
    получает 403/404 и регистрирует покрытие на маршрут, который никто не
    вызывал. Тогда §7 зеленеет на непроверенной ручке — то есть сторож полноты
    даёт ровно ту ложную уверенность, ради устранения которой заведён
    (ревью PR #42, discussion_r3879252953). Здесь URL — функция от шаблона,
    и разъехаться им негде.
    """
    url = route
    for name, value in params.items():
        url = url.replace("{" + name + "}", quote(str(value), safe=""))
    return url


def probe_foreign(c: httpx.Client, route: str, method: str, params: dict,
                  body=None):
    """Дёрнуть чужой объект по идентификатору — и этим же зарегистрировать обход.

    Покрытие записывается здесь, побочным продуктом исполнения, и только после
    того, как запрос фактически ушёл: «маршрут пройден» и «маршрут числится
    пройденным» — одно событие. Приговор fail-closed: только 403 или 404.
    2xx — утечка, 5xx — тоже дефект (чужой объект долетел до кода и упал уже
    внутри).

    Перед регистрацией покрытия проверяется ещё и обратное направление:
    собранный URL прогоняется через РЕАЛЬНЫЙ порядок диспетчеризации, и
    выбранный роутером маршрут обязан быть тем же самым объектом, под которым
    покрытие записывается. Одной сборки URL из шаблона для этого мало —
    шаблон, которого в приложении нет вовсе, так бы не поймался. Спросить
    `target.matches(scope)` тоже мало: это проверяет, может ли цель принять
    URL, а не то, достанется ли он ей. Совпасть с одним concrete URL способны
    два шаблона, и тогда запрос уйдёт в объявленный раньше, а кредит достался
    бы невызванному (ревью PR #42, discussion_r3879544096).
    """
    names = set(re.findall(r"\{([^}:]+)", route))
    if set(params) != names:
        check(f"{method} {route}: параметры пробы отвечают шаблону маршрута",
              False, f"в шаблоне {sorted(names)}, в пробе {sorted(params)}")
        return None
    absent = sorted(n for n, v in params.items() if v is None)
    if absent:
        # Проба не выполнена — покрытия нет. Регистрировать его тут было бы
        # враньём, поэтому §7 назовёт маршрут непокрытым, а заметка объяснит.
        NOTES.append(f"{method} {route}: не с чем проверять (нет объекта в B: {absent})")
        return None

    url = concrete_url(route, params)
    target = id_route_inventory().get((route, method))
    scope = {"type": "http", "method": method, "path": url,
             "path_params": {}, "root_path": "", "headers": []}
    chosen = dispatch_route(scope)
    if target is None or chosen is not target:
        if target is None:
            why = "такого шаблона с таким методом нет в таблице приложения"
        elif chosen is None:
            why = "этот URL не принимает ни один маршрут приложения"
        else:
            why = (f"запрос достанется другому маршруту: "
                   f"{sorted(getattr(chosen, 'methods', None) or ())} "
                   f"{getattr(chosen, 'path', '?')} — он объявлен раньше и "
                   f"перекрывает цель на этом URL")
        check(f"{method} {route}: запрос по собранному URL достаётся именно "
              f"этому маршруту", False, f"url={url}; {why}")
        return None

    r = (c.request(method, url, json=body) if body is not None
         else c.request(method, url))
    PROBED_ID_ROUTES.add((route, method))
    check(f"{method} {route} из чужой организации отклонён",
          r.status_code in (403, 404), f"status={r.status_code} {r.text[:100]}")
    return r


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
    # Маркеры приёмок. Ручка приёмки НАМЕРЕННО не сверяет имя позиции с
    # каталогом (см. api_order_receipts_add), поэтому имя здесь — чистый
    # маркер: если он окажется не в той таблице или не в той организации,
    # спутать его не с чем.
    RECEIPT_A = "ПРИЁМКА-МАРКЕР A-111"
    RECEIPT_B = "ПРИЁМКА-МАРКЕР B-999"
    INTRUSION = "ВТОРЖЕНИЕ A В ЗАКАЗ B"
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

    # SUPPLY-3: объекты планирования организации B. Нужны настоящие, а не
    # выдуманные идентификаторы: проба чужим id доказывает что-то только тогда,
    # когда объект по этому id действительно существует — у соседа.
    png_b = (b"\x89PNG\r\n\x1a\n"
             b"\x00\x00\x00\rIHDR\x00\x00\x00\x04\x00\x00\x00\x03"
             b"\x08\x02\x00\x00\x00\xd6oX\x83"
             b"\x00\x00\x00\nIDATx\x9cc\xf8\x0f\x00\x01\x01\x01\x00"
             b"\x18\xdd\x8d\xb4"
             b"\x00\x00\x00\x00IEND\xaeB`\x82")
    rmat = b.post("/api/supply/planning/materials",
                  json={"title": SECRET, "qty": "50", "op_id": "iso-mat"})
    mat_b = (rmat.json()["materials"][0]["id"]
             if rmat.status_code == 200 and rmat.json().get("materials") else None)
    rsk = b.post("/api/supply/planning/sketches",
                 files={"file": ("b.png", png_b, "image/png")})
    sketch_b = rsk.json().get("sketch_id") if rsk.status_code == 200 else None
    ritem = b.post("/api/supply/planning/items",
                   json={"kind": "draft", "title": SECRET, "sketch_id": sketch_b,
                         "op_id": "iso-item"})
    item_b = (ritem.json()["items"][0]["id"]
              if ritem.status_code == 200 and ritem.json().get("items") else None)
    rbatch = b.post("/api/supply/planning/batches",
                    json={"item_id": item_b, "title": SECRET, "plan_qty": "9",
                          "op_id": "iso-batch"}) if item_b else None
    batch_b = (rbatch.json()["batches"][0]["id"]
               if rbatch is not None and rbatch.status_code == 200
               and rbatch.json().get("batches") else None)
    rassign = b.post("/api/supply/planning/assignments",
                     json={"material_id": mat_b, "batch_id": batch_b, "qty": "3",
                           "op_id": "iso-assign"}) if (mat_b and batch_b) else None
    assign_b = None
    if rassign is not None and rassign.status_code == 200:
        # Имена переменных цикла намеренно длинные: `a` и `b` в этом наборе —
        # клиенты двух организаций, и затенить их значило бы сломать всё ниже.
        for planned_batch in rassign.json().get("batches", []):
            for planned_assignment in planned_batch.get("assignments", []):
                assign_b = planned_assignment["id"]
    check("в организации B есть план для проверок",
          all(x is not None for x in (mat_b, sketch_b, item_b, batch_b, assign_b)),
          f"mat={mat_b} sketch={sketch_b} item={item_b} batch={batch_b} assign={assign_b}")

    # Заказ и приёмка организации A — чтобы «у A ничего не изменилось» было
    # утверждением о живых строках, а не о пустой таблице.
    ra = a.post("/api/orders", json={"name": "Заказ A", "eta_date": None, "items": [
        {"base_name": sql("SELECT base_name FROM products WHERE org_id=? LIMIT 1",
                          org_a)[0][0], "qty": 4, "sizes": {}},
    ]})
    order_a = ra.json().get("id") if ra.status_code == 200 else None
    if order_a:
        a.post(f"/api/orders/{order_a}/status", json={"status": "sent"})
        a.post(f"/api/orders/{order_a}/receipts",
               json={"lines": [{"base_name": RECEIPT_A, "qty": 2}]})

    # Приёмка организации B и её план заказа — цели проб §1.
    # Живая строка приёмки нужна по той же причине: без неё проверка
    # «отклонённый POST ничего не изменил» сравнивала бы ноль с нулём и
    # проходила бы даже на дырявом коде.
    if order_b:
        b.post(f"/api/orders/{order_b}/status", json={"status": "sent"})
        b.post(f"/api/orders/{order_b}/receipts",
               json={"lines": [{"base_name": RECEIPT_B, "qty": 4}]})
    rows_a = sql("SELECT COUNT(*) FROM order_receipts WHERE org_id=?", org_a)[0][0]
    rows_b = sql("SELECT COUNT(*) FROM order_receipts WHERE org_id=?", org_b)[0][0]
    check("у обеих организаций есть живые строки приёмки — базовая линия «не изменилось»",
          rows_a > 0 and rows_b > 0, f"A={rows_a} B={rows_b}")

    rb = b.post("/api/order-plan", json={"production_id": prod_b, "budget": 100000})
    plan_b = ((rb.json().get("plan_id") or rb.json().get("id"))
              if rb.status_code == 200 else None)
    check("в организации B есть план заказа для проверок", plan_b is not None,
          f"status={rb.status_code} {rb.text[:120]}")

    # ── 1. Прямой доступ по чужому идентификатору ────────────────────────────
    print("\n== 1. Чужой идентификатор в пути: ожидаем 403/404, никогда 200 и никогда 5xx ==")
    # Первый столбец — ЗАРЕГИСТРИРОВАННЫЙ шаблон маршрута, третий — значения
    # его параметров. Готового URL здесь нет намеренно: его собирает из шаблона
    # сам `probe_foreign` и проверяет, что собранное резолвится обратно в этот
    # же маршрут. Иначе шаблон и URL — два источника правды, и скопированная
    # проба с новым шаблоном и старым URL зарегистрировала бы покрытие на
    # ручку, которую никто не вызывал.
    receipts_before = receipt_counts(org_a, org_b)
    cases = [
        ("/api/orders/{order_id}",                "GET",    {"order_id": order_b}, None),
        ("/api/orders/{order_id}/status",         "POST",   {"order_id": order_b}, {"status": "sent"}),
        ("/api/orders/{order_id}",                "DELETE", {"order_id": order_b}, None),
        ("/api/orders/{order_id}/ms-doc",         "GET",    {"order_id": order_b}, None),
        ("/api/orders/{order_id}/push-to-ms",     "POST",   {"order_id": order_b}, {}),
        ("/api/orders/{order_id}/receipts",       "GET",    {"order_id": order_b}, None),
        ("/api/orders/{order_id}/receipts",       "POST",   {"order_id": order_b},
         {"lines": [{"base_name": INTRUSION, "qty": 7}]}),
        ("/api/order-plan/{plan_id}/outcome",     "GET",    {"plan_id": plan_b}, None),
        ("/api/order-plan/{plan_id}/brief",       "GET",    {"plan_id": plan_b}, None),
        ("/api/warehouses/{warehouse_id}/toggle", "POST",   {"warehouse_id": wh_b}, {"active": False}),
        ("/api/productions/{pid}",                "POST",   {"pid": prod_b}, {"name": "Захвачено"}),
        ("/api/productions/{pid}",                "DELETE", {"pid": prod_b}, None),
        ("/api/productions/{pid}/setup",          "POST",   {"pid": prod_b}, {"preset": "turnkey"}),
        # SUPPLY-3. Отдельного «не найдено» и «чужое» у слоя нет намеренно:
        # разные ответы рассказали бы о существовании чужих строк перебором
        # номеров. Здесь проверяется сам запрет, ниже (§1а) — что отказ не несёт
        # чужого текста и ничего не пишет.
        ("/api/supply/planning/sketches/{sketch_id}", "GET", {"sketch_id": sketch_b}, None),
        ("/api/supply/planning/materials/{material_id}/update", "POST",
         {"material_id": mat_b}, {"qty": "1", "op_id": "iso-x1"}),
        ("/api/supply/planning/batches/{batch_id}/update", "POST",
         {"batch_id": batch_b}, {"plan_qty": "1", "op_id": "iso-x2"}),
        ("/api/supply/planning/assignments/{assignment_id}/update", "POST",
         {"assignment_id": assign_b}, {"qty": "1", "op_id": "iso-x3"}),
        ("/api/supply/planning/assignments/{assignment_id}/delete", "POST",
         {"assignment_id": assign_b}, {"op_id": "iso-x4"}),
    ]
    answers = {}
    for route, method, params, body in cases:
        r = probe_foreign(a, route, method, params, body)
        if r is not None:
            answers[(route, method)] = r

    # ── 1а. Отказ не должен ни раскрывать, ни менять ─────────────────────────
    # Один только код ответа доказывает меньше, чем кажется: 404 с чужими
    # строками в теле — это утечка, а отклонённый POST, успевший дописать
    # строку, — это порча данных. Проверяются оба следствия отдельно.
    print("\n== 1а. Отказ по чужому id: без раскрытия и без записи ==")
    markers = [SECRET, "СЕКРЕТНЫЙ ЭТАП B", RECEIPT_B, "Заказ B"]
    for route, method in (("/api/orders/{order_id}/receipts", "GET"),
                          ("/api/orders/{order_id}/receipts", "POST"),
                          ("/api/order-plan/{plan_id}/outcome", "GET"),
                          ("/api/order-plan/{plan_id}/brief", "GET")):
        r = answers.get((route, method))
        if r is None:
            continue
        leaked = [m for m in markers if m in r.text]
        check(f"{method} {route}: в отказе нет данных организации B",
              not leaked, f"нашлось: {leaked}")
        try:
            payload = r.json()
            keys = set(payload) if isinstance(payload, dict) else {"<не-объект>"}
        except ValueError:
            keys = set()
        check(f"{method} {route}: тело отказа — только объяснение, без нагрузки",
              keys <= {"detail"}, f"keys={sorted(keys)}")

    receipts_after = receipt_counts(org_a, org_b)
    check("отклонённый POST приёмки не изменил строки ни одной организации",
          receipts_after == receipts_before,
          f"было {receipts_before}, стало {receipts_after}")
    intruded = sql("SELECT COUNT(*) FROM order_receipts WHERE base_name=?", INTRUSION)[0][0]
    check("строка, которую A пыталась вписать в заказ B, не появилась нигде",
          intruded == 0, f"rows={intruded}")

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

    # Ручное добавление позиции в заказ (D-23) — новая точка входа с именем
    # позиции в теле запроса. Проверка принадлежности идёт по снапшоту
    # организации, а не по присланной строке; иначе через overrides можно было
    # бы вписать в свой заказ чужой товар (и увидеть его цену и остаток).
    eta_iso = (date.today() + timedelta(days=40)).isoformat()
    r = a.post("/api/order-plan/preview", json={
        "eta_date": eta_iso, "budget": 100000, "budget_scope": "full",
        "overrides": {SECRET: 10},
    })
    check("ручное добавление чужой позиции в план не срабатывает",
          r.status_code != 200
          or not any(i.get("base_name") == SECRET for i in (r.json().get("items") or [])),
          f"status={r.status_code} {r.text[:160]}")
    check("и чужое имя не утекает в ответ целиком",
          r.status_code != 200 or SECRET not in r.text, f"status={r.status_code}")

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

    # Чужой план заказа нельзя применить. План организации B создан в
    # подготовке — он же цель проб outcome и brief в §1, и проба идёт через
    # общий helper, чтобы маршрут `apply` числился пройденным у сторожа §7.
    probe_foreign(a, "/api/order-plan/{plan_id}/apply", "POST",
                  {"plan_id": plan_b}, {"name": "Чужой план"})

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

    # ── 7. Сторож полноты обхода ─────────────────────────────────────────────
    print("\n== 7. Сторож: маршрут с id в пути либо пройден пробой, либо исключён явно ==")
    inventory = id_route_inventory()
    # Первым делом — что инвентарь вообще собрался. Пустой инвентарь превращает
    # все проверки ниже в тавтологию: пустое множество не нарушает ничего.
    check("таблица маршрутов приложения прочитана",
          len(inventory) >= 10,
          f"маршрутов с параметром пути: {len(inventory)}")

    uncovered = sorted(k for k in inventory
                       if k not in PROBED_ID_ROUTES and k not in ID_ROUTE_EXCLUSIONS)
    detail = ("не пройдено: "
              + "; ".join(f"{m} {p}" for p, m in uncovered)
              + " — добавьте пробу чужим идентификатором в §1 либо запись с "
                "причиной в ID_ROUTE_EXCLUSIONS") if uncovered else ""
    check("каждый маршрут с id в пути пройден пробой или исключён с причиной",
          not uncovered, detail)

    stale = sorted(k for k in ID_ROUTE_EXCLUSIONS if k not in inventory)
    detail = ("устарело: " + "; ".join(f"{m} {p}" for p, m in stale)) if stale else ""
    check("в реестре исключений нет записей о несуществующих маршрутах",
          not stale, detail)

    phantom = sorted(k for k in PROBED_ID_ROUTES if k not in inventory)
    detail = ("таких маршрутов нет: " + "; ".join(f"{m} {p}" for p, m in phantom)
              + " — шаблон в таблице проб разошёлся с приложением") if phantom else ""
    check("каждая проба §1 бьёт по существующему маршруту", not phantom, detail)

    # Сторож самого реестра исключений. Исключение оправдано тем, что параметр
    # пути не адресует строку базы; целочисленный параметр — это ровно
    # первичный ключ (`_id_path()`), поэтому исключить такой маршрут нельзя.
    for key in sorted(ID_ROUTE_EXCLUSIONS):
        route = inventory.get(key)
        if route is None:
            continue
        ints = int_path_params(route)
        check(f"исключение {key[1]} {key[0]} не прячет целочисленный id объекта",
              not ints, f"целочисленные параметры пути: {ints}")


if __name__ == "__main__":
    sys.exit(main())
