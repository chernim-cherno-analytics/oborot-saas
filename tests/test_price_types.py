# -*- coding: utf-8 -*-
"""DATA-10: исчезнувший тип цены не должен молча портить деньги.

Дефект, который закрывает этот набор. Организация выбирает в настройках, какой
тип цены МойСклада считать ценой продажи и какой — полной себестоимостью. Поиск
по имени точный и без отката (app/ms_sync.py, _price_by): стоит переименовать
тип в МойСкладе или опечататься при выборе — и совпадения больше нет. Дальше
две разные беды, обе тихие:

  * полная себестоимость становится 0.0, и _upsert_products записывает этот ноль
    поверх прежнего значения по ВСЕМУ ассортименту. Деньги начинают считаться по
    закупочной цене; у бренда со своим пошивом это стоимость пошива без ткани;
  * цена продажи откатывается на «первую цену в списке» — то есть на ЧУЖОЙ тип.
    Это хуже нуля: ноль видно, подменённую цену — нет.

Синк при этом заканчивается словом «Синхронизация завершена». Продукт не падает,
а уверенно врёт — и заметить это можно только сверкой с источником.

Правило, которое проверяется здесь: непустой ЯВНО выбранный тип цены, которого
нет во всём ассортименте, останавливает синхронизацию человеческой ошибкой ДО
записи товаров. Прежние sale_price и cost_full остаются в базе нетронутыми,
а актуальный список типов доезжает в настройки — чтобы выбор можно было
исправить, не отгадывая новое название.

Сценарий:
  1) именованные типы цен включены в моке, выбор сохранён в настройках —
     синк проходит, и ОБЕ цены ненулевые и взяты именно из выбранных типов;
  2) тип себестоимости переименован → state=error, обе прежние цены целы,
     текст называет пропавший тип, список типов в настройках уже новый;
  3) тип цены продажи исчез совсем → state=error, обе прежние цены целы
     (без этого правила sale_price уехал бы на цену другого типа);
  4) пропали ОБА выбранных типа сразу → в тексте названы оба;
  5) тип есть в ассортименте, но не проставлен на одной карточке → синк
     проходит: это нормальное состояние каталога, а не авария;
  6) настройки пусты → синк проходит, эвристики работают как раньше;
  7) тип вернулся → синк снова done, и цены ОБНОВЛЯЮТСЯ;
  8) выбранные типы остались ТОЛЬКО у услуги → синк остановлен: строка услуги
     в products не попадает, и её типы цен товарами не подтверждены;
  9) выбранный тип проставлен ТОЛЬКО на модификациях → синк проходит:
     модификации импортируются, и это положительный контроль против
     пере-сужения критерия до одних `product`.

Пункт 8 закрывает обход, найденный внешним ревью 25.08.2026: `price_type_names`
собирала типы со ВСЕХ строк ответа МойСклада, а `_parse_assortment` импортирует
только `product`/`variant`. Тип, оставшийся у услуги, засчитывался как
присутствующий, замок молчал — и у реально импортируемого товара цена продажи
откатывалась на чужой тип, а `cost_full` записывался нулём. То есть ровно та
порча денег, ради которой замок и написан, проходила мимо него.

Отдельный файл, а не проверки внутри tests/test_sync.py: тот держит эталонные
числа одного мок-мира на трёх тысячах строк, и включение именованных типов
внутри него — риск для чужих проверок ради удобства. Переключатель мока
(mock_ms.PRICE_TYPES) по умолчанию выключен, поэтому остальные наборы видят
ровно те же цены, что и раньше.

Запуск из корня репозитория:  python tests/test_price_types.py
"""
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

DB_PATH = ROOT / "test_price_types.db"
# Порты берутся из окружения: так tests/run_all.py разводит наборы и
# может гонять их параллельно. Значения по умолчанию — свои, ничем не занятые.
APP_PORT = int(os.environ.get("OBOROT_TEST_PORT", "8809"))
MOCK_PORT = int(os.environ.get("OBOROT_MOCK_PORT", "9811"))

os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["MS_BASE_URL"] = f"http://127.0.0.1:{MOCK_PORT}"
os.environ["SCHEDULER_ENABLED"] = "0"
# История здесь ни при чём — проверяются товары и цены. Берём короткое окно,
# чтобы набор не гонял минуту остатков ради шести синков подряд.
os.environ["HISTORY_DAYS"] = "20"
os.environ["INITIAL_WINDOW_DAYS"] = "10"
os.environ["STOCK_CHUNK_DATES"] = "10"
os.environ["SYNC_DAYS_BACK"] = "3"
os.environ["MS_CHUNK_PAUSE"] = "0"
os.environ["MS_MAX_RETRIES"] = "2"

if DB_PATH.exists():
    DB_PATH.unlink()

import httpx  # noqa: E402
import uvicorn  # noqa: E402

import mock_ms  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.main import app as oborot_app  # noqa: E402
from app.models import Product  # noqa: E402
from sqlalchemy import select  # noqa: E402

mock_ms.PORT = MOCK_PORT

# Безразмерный товар: у него нет родителя, поэтому cost_full не подхватывается
# по наследству и видно РОВНО то, что посчитал выбранный тип цены.
SUBJECT = "p-bag1"          # Сумка «Тоут»: цена 5900 ₽, закупка 2400 ₽
NEIGHBOUR = "p-ear1"        # Серьга 12 мм: цена 2400 ₽, закупка 900 ₽
# Модификация: своя строка ассортимента с meta.type = "variant". Нужна для
# положительного контроля — синк её импортирует, значит её типы цен считаются.
VARIANT_SUBJECT = "v-hoodie1-M"   # Худи «Скетч» (M): цена 9800 ₽, закупка 3900 ₽
SALE_TYPE = "Цена продажи"
COST_TYPE = "Полная себестоимость"
COST_TYPE_RENAMED = "Полная себестоимость 2026"
# Типы, которые в сценарии 8 встречаются ТОЛЬКО у услуги.
SALE_TYPE_SERVICE = "Продажа выбранная"
COST_TYPE_SERVICE = "Себестоимость выбранная"
# Единственный тип, оставшийся у товаров: чужой по отношению к выбору.
FOREIGN_TYPE = "Другая цена"
# ext_id всех строк ассортимента с meta.type = "product" — берутся из самого
# мока, а не переписываются сюда руками: список мок-мира меняется, а
# положительный контроль обязан остаться про «только модификации».
PRODUCT_EXTS = ([p[0] for p in mock_ms.SIZED]
                + [p[0] for p in mock_ms.SIMPLE]
                + [p[0] for p in mock_ms.ARCHIVED_SIMPLE])


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


def prices(ext_id: str) -> tuple[float, float, float]:
    """(sale_price, cost_full, cost_price) товара — как они лежат в базе."""
    db = SessionLocal()
    try:
        row = db.execute(select(Product).where(Product.ext_id == ext_id)).scalars().first()
        if row is None:
            return (-1.0, -1.0, -1.0)
        return (float(row.sale_price or 0), float(row.cost_full or 0),
                float(row.cost_price or 0))
    finally:
        db.close()


def wait_sync_done(client: httpx.Client, timeout: float = 240.0) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        last = client.get("/api/sync/status").json()
        if last.get("state") in ("done", "error"):
            return last
        time.sleep(0.5)
    return last


def main() -> int:
    mock_srv = ServerThread(mock_ms.app, MOCK_PORT)
    app_srv = ServerThread(oborot_app, APP_PORT)
    mock_srv.start()
    app_srv.start()
    try:
        return run()
    finally:
        mock_ms.reset_price_types()
        app_srv.stop()
        mock_srv.stop()
        if DB_PATH.exists():
            DB_PATH.unlink()


def run() -> int:  # noqa: C901 — линейный сценарий, дробить его вредно
    base = f"http://127.0.0.1:{APP_PORT}"
    c = httpx.Client(headers={"X-Oborot-CSRF": "1"}, base_url=base, timeout=120.0)
    mock_api = httpx.Client(base_url=f"http://127.0.0.1:{MOCK_PORT}", timeout=30.0)

    def set_mock_types(**kw) -> None:
        r = mock_api.post("/__test/price_types", json=kw)
        assert r.status_code == 200, r.text

    def set_settings(sale: str, cost: str) -> None:
        r = c.post("/api/settings", json={"price_type_sale": sale,
                                          "price_type_cost": cost})
        assert r.status_code == 200, r.text

    def sync() -> dict:
        r = c.post("/api/sync/run")
        assert r.status_code == 200, r.text
        return wait_sync_done(c)

    def types_offered() -> list[str]:
        return list(c.get("/api/settings").json().get("price_types") or [])

    print("\n== Подготовка: именованные типы цен и явный выбор ==")
    mock_ms.reset_price_types()
    set_mock_types(enabled=True, sale=SALE_TYPE, cost=COST_TYPE, cost_ratio=1.5)

    r = c.post("/register", data={"name": "Владелец", "email": "owner@test.io",
                                  "password": "secret123", "org_name": "Бренд"})
    check("регистрация", r.status_code in (200, 302, 303), f"status={r.status_code}")
    r = c.post("/api/connect/moysklad", json={"token": mock_ms.TOKEN})
    check("токен принят", r.status_code == 200, f"status={r.status_code}")
    c.post("/api/connect/moysklad/stores", json={"ext_ids": ["st-flag", "st-web"]})
    r = c.post("/api/sync/initial")
    check("первичный синк запущен", r.status_code == 200, f"status={r.status_code}")
    st = wait_sync_done(c)
    check("первичный синк дошёл до done", st.get("state") == "done",
          f"state={st.get('state')} error={str(st.get('error'))[:120]}")

    check("оба типа цен предложены в настройках",
          SALE_TYPE in types_offered() and COST_TYPE in types_offered(),
          f"price_types={types_offered()}")

    set_settings(SALE_TYPE, COST_TYPE)
    st = sync()
    check("синк с явно выбранными типами доходит до done", st.get("state") == "done",
          f"state={st.get('state')} error={str(st.get('error'))[:120]}")

    sale0, cost0, buy0 = prices(SUBJECT)
    check("цена продажи взята из выбранного типа и не равна нулю", sale0 == 5900.0,
          f"sale_price={sale0}")
    check("полная себестоимость взята из выбранного типа и не равна нулю",
          cost0 == 3600.0, f"cost_full={cost0} (закупка {buy0}, ×1.5)")
    check("полная себестоимость — это НЕ закупочная цена и НЕ цена продажи",
          cost0 != buy0 and cost0 != sale0, f"cost_full={cost0} buy={buy0} sale={sale0}")

    print("\n== Тип себестоимости переименован в МойСкладе ==")
    set_mock_types(cost=COST_TYPE_RENAMED)
    st = sync()
    check("СИНК ОСТАНОВЛЕН: выбранный тип себестоимости исчез",
          st.get("state") == "error",
          f"state={st.get('state')} (до исправления здесь было done, "
          f"а cost_full молча уходил в 0)")
    err = str(st.get("error") or "")
    check("в тексте ошибки назван пропавший тип цены", COST_TYPE in err, f"error={err[:200]}")
    check("текст ошибки объясняет, что делать", "астройк" in err, f"error={err[:200]}")
    check("текст ошибки — человеческий, без внутренностей",
          "Traceback" not in err and "Error" not in err and len(err) > 40,
          f"error={err[:200]}")

    sale1, cost1, _ = prices(SUBJECT)
    check("ПРЕЖНЯЯ полная себестоимость сохранена", cost1 == cost0,
          f"было={cost0} стало={cost1} (до исправления здесь был 0.0)")
    check("прежняя цена продажи тоже сохранена", sale1 == sale0,
          f"было={sale0} стало={sale1}")
    sale1n, cost1n, _ = prices(NEIGHBOUR)
    check("цены соседнего товара тоже не тронуты",
          sale1n == 2400.0 and cost1n == 1350.0, f"sale={sale1n} cost_full={cost1n}")

    offered = types_offered()
    check("актуальный список типов доехал в настройки — выбор исправим",
          COST_TYPE_RENAMED in offered, f"price_types={offered}")
    check("в тексте ошибки видно, из чего теперь выбирать",
          COST_TYPE_RENAMED in err, f"error={err[:250]}")

    print("\n== Тип цены продажи исчез совсем ==")
    set_mock_types(sale="", cost=COST_TYPE)
    set_settings(SALE_TYPE, COST_TYPE)
    st = sync()
    check("СИНК ОСТАНОВЛЕН: выбранный тип цены продажи исчез",
          st.get("state") == "error",
          f"state={st.get('state')} error={str(st.get('error'))[:120]}")
    err = str(st.get("error") or "")
    check("в тексте ошибки назван пропавший тип цены продажи", SALE_TYPE in err,
          f"error={err[:200]}")
    sale2, cost2, _ = prices(SUBJECT)
    check("ЦЕНА ПРОДАЖИ НЕ ПОДМЕНЕНА чужим типом цены", sale2 == sale0,
          f"было={sale0} стало={sale2} (до исправления сюда приезжала "
          f"первая цена в списке — {cost0} ₽ себестоимости)")
    check("полная себестоимость при этом тоже цела", cost2 == cost0,
          f"было={cost0} стало={cost2}")

    print("\n== Пропали оба выбранных типа сразу ==")
    set_mock_types(sale="", cost=COST_TYPE_RENAMED)
    st = sync()
    check("СИНК ОСТАНОВЛЕН: пропали оба типа", st.get("state") == "error",
          f"state={st.get('state')} error={str(st.get('error'))[:120]}")
    err = str(st.get("error") or "")
    check("названы ОБА пропавших типа, а не первый попавшийся",
          SALE_TYPE in err and COST_TYPE in err, f"error={err[:250]}")
    sale2b, cost2b, _ = prices(SUBJECT)
    check("обе прежние цены целы и при двойной пропаже",
          sale2b == sale0 and cost2b == cost0,
          f"sale={sale2b} cost_full={cost2b}")

    print("\n== Тип есть в ассортименте, но не проставлен на одной карточке ==")
    set_mock_types(sale=SALE_TYPE, cost=COST_TYPE, cost_skip=[SUBJECT])
    st = sync()
    check("частичное отсутствие типа синк НЕ останавливает",
          st.get("state") == "done",
          f"state={st.get('state')} error={str(st.get('error'))[:120]}")
    sale3, cost3, _ = prices(SUBJECT)
    check("у карточки без цены этого типа себестоимость пуста", cost3 == 0.0,
          f"cost_full={cost3}")
    check("цена продажи этой карточки на месте", sale3 == 5900.0, f"sale_price={sale3}")
    _, cost3n, _ = prices(NEIGHBOUR)
    check("у остальных карточек себестоимость посчитана", cost3n == 1350.0,
          f"cost_full={cost3n}")

    print("\n== Пустые настройки: эвристики работают как раньше ==")
    set_mock_types(cost_skip=[], cost=COST_TYPE_RENAMED)
    set_settings("", "")
    st = sync()
    check("без явного выбора переименование типа синк НЕ останавливает",
          st.get("state") == "done",
          f"state={st.get('state')} error={str(st.get('error'))[:120]}")
    sale4, cost4, _ = prices(SUBJECT)
    check("эвристика нашла цену продажи по названию типа", sale4 == 5900.0,
          f"sale_price={sale4}")
    check("эвристика нашла полную себестоимость по названию типа", cost4 == 3600.0,
          f"cost_full={cost4}")

    print("\n== Тип вернулся: следующий синк проходит и обновляет цены ==")
    set_mock_types(cost=COST_TYPE, cost_ratio=2.0)
    set_settings(SALE_TYPE, COST_TYPE)
    st = sync()
    check("после возврата типа синк снова доходит до done", st.get("state") == "done",
          f"state={st.get('state')} error={str(st.get('error'))[:120]}")
    sale5, cost5, buy5 = prices(SUBJECT)
    check("полная себестоимость ОБНОВИЛАСЬ из вернувшегося типа", cost5 == 4800.0,
          f"cost_full={cost5} (закупка {buy5}, ×2.0)")
    check("цена продажи на месте", sale5 == 5900.0, f"sale_price={sale5}")

    print("\n== Выбранные типы остались ТОЛЬКО у услуги (её синк не импортирует) ==")
    # У ТОВАРОВ остаётся единственный, чужой по отношению к выбору тип цены;
    # оба выбранных типа есть только в карточке услуги. До исправления замок
    # засчитывал их как присутствующие: услуга попадала в price_type_names,
    # хотя в products не попадает никогда.
    set_mock_types(sale="", cost=FOREIGN_TYPE, cost_ratio=1.5, cost_skip=[],
                   service=[[SALE_TYPE_SERVICE, 1110], [COST_TYPE_SERVICE, 700]])
    set_settings(SALE_TYPE_SERVICE, COST_TYPE_SERVICE)
    st = sync()
    check("СИНК ОСТАНОВЛЕН: выбранных типов нет ни у одного ТОВАРА",
          st.get("state") == "error",
          f"state={st.get('state')} (до исправления здесь было done: типы "
          f"засчитывались по строке услуги)")
    err = str(st.get("error") or "")
    check("названы оба типа, которых нет у товаров",
          SALE_TYPE_SERVICE in err and COST_TYPE_SERVICE in err, f"error={err[:250]}")
    check("товары не записаны: до _upsert_products синк не дошёл",
          "products_total" not in (st.get("stats") or {}),
          f"stats.products_total={(st.get('stats') or {}).get('products_total')}")
    sale6, cost6, _ = prices(SUBJECT)
    check("прежняя цена продажи цела до копейки", sale6 == sale5,
          f"было={sale5} стало={sale6} (до исправления сюда приезжала чужая "
          f"цена — 3600.0 ₽)")
    check("прежняя полная себестоимость цела до копейки", cost6 == cost5,
          f"было={cost5} стало={cost6} (до исправления здесь был 0.0)")
    sale6n, cost6n, _ = prices(NEIGHBOUR)
    check("цены соседнего товара тоже целы до копейки",
          sale6n == 2400.0 and cost6n == 1800.0,
          f"sale={sale6n} cost_full={cost6n}")
    offered = types_offered()
    check("настройки не предлагают тип, которого нет ни у одного товара",
          FOREIGN_TYPE in offered
          and SALE_TYPE_SERVICE not in offered and COST_TYPE_SERVICE not in offered,
          f"price_types={offered}")
    tail = err.split("Сейчас в ассортименте есть:", 1)
    check("в тексте ошибки предложены только типы товаров",
          len(tail) == 2 and FOREIGN_TYPE in tail[1]
          and SALE_TYPE_SERVICE not in tail[1] and COST_TYPE_SERVICE not in tail[1],
          f"error={err[:250]}")

    print("\n== Выбранный тип проставлен ТОЛЬКО на модификациях ==")
    # Положительный контроль: модификации синк импортирует, значит их типы цен
    # засчитываются. Без этой проверки «фильтр импортируемых строк» мог бы
    # незаметно сузиться до одних product — и сломать аккаунты, где цены
    # проставлены на размерах, а не на модели.
    set_mock_types(sale=SALE_TYPE, cost=COST_TYPE, cost_ratio=2.0,
                   cost_skip=PRODUCT_EXTS, service=[])
    set_settings(SALE_TYPE, COST_TYPE)
    st = sync()
    check("тип, найденный только на модификациях, синк НЕ останавливает",
          st.get("state") == "done",
          f"state={st.get('state')} error={str(st.get('error'))[:150]}")
    saleV, costV, buyV = prices(VARIANT_SUBJECT)
    check("полная себестоимость модификации посчитана из выбранного типа",
          costV == 7800.0, f"cost_full={costV} (закупка {buyV}, ×2.0)")
    check("цена продажи модификации на месте", saleV == 9800.0, f"sale_price={saleV}")
    sale7, cost7, _ = prices(SUBJECT)
    check("у безразмерного товара без этого типа себестоимость пуста и это не авария",
          cost7 == 0.0 and sale7 == 5900.0, f"sale={sale7} cost_full={cost7}")

    c.close()
    mock_api.close()
    print(f"\nИТОГО: {len(PASS)} OK, {len(FAIL)} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
