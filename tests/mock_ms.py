# -*- coding: utf-8 -*-
"""Mock-сервер МойСклад JSON API 1.2 для тестов синхронизации.

Имитирует ровно те эндпоинты и особенности, которыми пользуется синк:
- context/employee (проверка токена, 401 при чужом токене);
- entity/store (3 склада: два торговых + сервисный «Съёмки»);
- entity/assortment (товары с вариантами-размерами и безразмерные, цены
  в КОПЕЙКАХ, pathName, characteristics «Размер»; у одной модели
  характеристики отсутствуют — размер парсится из скобок имени);
- report/stock/all: остатки на дату из filter=moment=...;store=... —
  отдельный query-параметр moment ИГНОРИРУЕТСЯ (как в реальном МС),
  позиции с нулевым остатком в отчёт не попадают (для явных нулей);
- entity/retaildemand | demand | salesreturn с positions: при limit > 100
  и expand positions приходят href-ами без строк (как в реальном МС);
- обратная запись (writeback): entity/organization (одно юрлицо),
  GET/POST entity/counterparty (поиск по filter=name=..., создание),
  POST entity/purchaseorder — валидирует organization/agent/positions,
  проверяет, что assortment.meta.href указывает на существующий SKU,
  сохраняет документ в CREATED_PURCHASE_ORDERS и возвращает meta с
  href и uuidHref (ссылка веб-интерфейса), name — автонумерация.

Данные детерминированные: random.Random(SEED), 60 дней истории.
Модуль экспортирует expected_*() — эталонные суммы для проверок теста.
"""
import os
import random
from datetime import date, timedelta

from fastapi import FastAPI, HTTPException, Query, Request

# Порты берутся из окружения: так tests/run_all.py разводит наборы и
# может гонять их параллельно. Значения по умолчанию — прежние.
PORT = int(os.environ.get("OBOROT_MOCK_PORT", "9800"))
BASE = f"http://127.0.0.1:{PORT}"
TOKEN = "mock-token-correct-1234"
SEED = 20260730
HISTORY_DAYS = 60

STORES = [
    ("st-flag", "Флагман"),
    ("st-web", "Интернет-магазин"),
    ("st-lab", "Съёмки (сервисный)"),  # в тесте НЕ выбирается — фильтр складов
]
TRADE_STORES = ["st-flag", "st-web"]

SIZES = ["S", "M", "L"]

# (ext-база, имя, категория(pathName), цена ₽, себес ₽, темп продаж/день, флаг)
# флаги: sellout — маленький тираж, распродаётся в ноль (явные нули);
#        nochar — у вариантов нет характеристики «Размер» (парсинг скобок);
#        dead — не продаётся вовсе (неликвид);
#        limited — крошечный тираж (2+2 шт), пара продаж по высокой цене:
#                  сырая «оборачиваемость» = nr/dis дикая (дни в стоке 1–2),
#                  но это шум → позиция обязана попадать в low_data и НЕ
#                  возглавлять рейтинг (кейс «бомбер Регби» из фидбэка).
SIZED = [
    ("p-hoodie1", "Худи «Скетч»", "Одежда/Худи", 9800, 3900, 1.6, ""),
    ("p-hoodie2", "Худи «Штрих»", "Одежда/Худи", 8900, 3600, 1.1, ""),
    ("p-tee1", "Футболка «Манифест»", "Одежда/Футболки", 3900, 1500, 2.2, ""),
    ("p-tee2", "Футболка «Курсив»", "Одежда/Футболки", 3400, 1400, 1.4, "sellout"),
    ("p-tee3", "Футболка «Полночь»", "Одежда/Футболки", 3300, 1300, 0.8, "nochar"),
    ("p-shirt1", "Рубашка «Разворот»", "Одежда/Рубашки", 6900, 2800, 0.7, ""),
    ("p-pants1", "Брюки «Чертёж»", "Одежда/Брюки", 7400, 3000, 0.9, ""),
    ("p-dress1", "Платье «Тушь»", "Одежда/Платья", 8200, 3300, 0.6, ""),
]
SIMPLE = [
    ("p-bag1", "Сумка «Тоут»", "Аксессуары/Сумки", 5900, 2400, 1.2, ""),
    ("p-ear1", "Серьга 12 мм", "Украшения", 2400, 900, 1.8, ""),
    ("p-ring1", "Кольцо «Грань»", "Украшения", 2900, 1100, 0.9, ""),
    ("p-cap1", "Кепка «Штамп»", "Аксессуары", 3200, 1300, 0.5, ""),
    ("p-belt1", "Ремень «Ось»", "Аксессуары", 4100, 1700, 0.0, "dead"),
    ("p-bomber1", "Бомбер «Камео»", "Одежда/Верхняя одежда", 24000, 9000, 2.2, "limited"),
    # service: расходники/сертификаты — авто-исключаются из аналитики (app/exclusions.py)
    ("p-pack1", "Брендированный пакет средний", "Расходный материал", 50, 20, 3.0, "service"),
    ("p-cert1", "Подарочный сертификат на десять тысяч", "Сертификаты", 10000, 0, 0.4, "service"),
]
ARCHIVED_SIMPLE = [
    ("p-old1", "Футболка «Архив 2022»", "Одежда/Футболки", 2500, 1000, 0.0, "archived"),
]

TODAY = date.today()
DATES = [(TODAY - timedelta(days=off)).isoformat()
         for off in range(HISTORY_DAYS - 1, -1, -1)]


# ── Генерация детерминированного мира ────────────────────────────────────────

def _build_world():
    rnd = random.Random(SEED)
    skus = []  # {ext, name, base, size, price, cost, path, rate, flags, kind, parent}
    for pid, name, path, price, cost, rate, flags in SIZED:
        for size in SIZES:
            skus.append({
                "ext": f"v-{pid[2:]}-{size}", "kind": "variant", "parent": pid,
                "name": f"{name} ({size})", "base": name, "size": size,
                "price": price, "cost": cost, "path": path,
                "rate": rate / len(SIZES), "flags": flags,
            })
    for pid, name, path, price, cost, rate, flags in SIMPLE + ARCHIVED_SIMPLE:
        skus.append({
            "ext": pid, "kind": "product", "parent": None,
            "name": name, "base": name, "size": "",
            "price": price, "cost": cost, "path": path,
            "rate": rate, "flags": flags,
        })

    # Начальные остатки на каждом складе (сервисный склад почти пустой).
    stock = {}  # (store, ext) -> qty
    for sku in skus:
        for sid, _ in STORES:
            if "archived" in sku["flags"]:
                init = 0
            elif sid == "st-lab":
                init = 1 if rnd.random() < 0.3 else 0
            elif "limited" in sku["flags"]:
                init = 3  # 3+3 шт на два торговых склада: дни с остатком ≥3 — единицы
            elif "sellout" in sku["flags"]:
                init = rnd.randint(4, 7)
            elif "dead" in sku["flags"]:
                init = rnd.randint(8, 14)
            else:
                init = rnd.randint(25, 60)
            stock[(sid, sku["ext"])] = init

    stock_by_day = {}   # (date, store) -> {ext: qty>0} — остаток на КОНЕЦ дня
    sale_events = []    # {date, store, ext, qty, price_kop, discount}
    return_events = []
    for day_idx, day in enumerate(DATES):
        for sid, _ in STORES:
            for sku in skus:
                if sid == "st-lab":
                    # редкие «сервисные» отгрузки — должны отфильтроваться
                    if rnd.random() < 0.01 and stock[(sid, sku["ext"])] > 0:
                        sale_events.append({
                            "date": day, "store": sid, "ext": sku["ext"], "qty": 1,
                            "price_kop": sku["price"] * 100, "discount": 0,
                        })
                        stock[(sid, sku["ext"])] -= 1
                    continue
                rate = sku["rate"] / len(TRADE_STORES)
                left = stock[(sid, sku["ext"])]
                if left <= 0 or rate <= 0:
                    continue
                qty = 0
                if rnd.random() < min(0.95, rate):
                    qty = 1 + (1 if rnd.random() < rate / 3 else 0)
                qty = min(qty, left)
                if qty > 0:
                    discount = 10 if rnd.random() < 0.25 else 0
                    sale_events.append({
                        "date": day, "store": sid, "ext": sku["ext"], "qty": qty,
                        "price_kop": sku["price"] * 100, "discount": discount,
                    })
                    stock[(sid, sku["ext"])] -= qty
            # возврат: каждые 9 дней по одной штуке последней проданной позиции
            if day_idx % 9 == 5:
                todays = [e for e in sale_events
                          if e["date"] == day and e["store"] == sid]
                if todays:
                    src = todays[-1]
                    return_events.append({
                        "date": day, "store": sid, "ext": src["ext"], "qty": 1,
                        "price_kop": src["price_kop"], "discount": src["discount"],
                    })
        for sid, _ in STORES:
            snapshot = {}
            for sku in skus:
                q = stock[(sid, sku["ext"])]
                if q > 0:
                    snapshot[sku["ext"]] = float(q)
            stock_by_day[(day, sid)] = snapshot

    # Документы: retaildemand — розница Флагмана, demand — отгрузки остальных.
    # Возвраты: розничные (Флагман) — retailsalesreturn, остальные — salesreturn
    # (как в реальном МС; аудит 18.08 — retailsalesreturn не синкался вовсе).
    docs = {"retaildemand": [], "demand": [], "salesreturn": [],
            "retailsalesreturn": []}

    def _positions(events):
        return [
            {
                "assortment": {"meta": _asm_meta(e["ext"])},
                "quantity": e["qty"],
                "price": e["price_kop"],
                "discount": e["discount"],
            }
            for e in events
        ]

    def _add_doc(entity, day, sid, events, num, applicable=None):
        doc = {
            "id": f"{entity}-{day}-{sid}-{num}",
            "meta": {"href": f"{BASE}/entity/{entity}/{entity}-{day}-{sid}-{num}",
                     "type": entity},
            "moment": f"{day} 14:30:00",
            "store": {"meta": {"href": f"{BASE}/entity/store/{sid}",
                               "type": "store"}},
            "positions": {"rows": _positions(events),
                          "meta": {"size": len(events)}},
        }
        if applicable is not None:
            doc["applicable"] = applicable
        docs[entity].append(doc)

    for day in DATES:
        for sid, _ in STORES:
            day_sales = [e for e in sale_events
                         if e["date"] == day and e["store"] == sid]
            if day_sales:
                entity = "retaildemand" if sid == "st-flag" else "demand"
                _add_doc(entity, day, sid, day_sales, 0)
            day_rets = [e for e in return_events
                        if e["date"] == day and e["store"] == sid]
            if day_rets:
                _add_doc("retailsalesreturn" if sid == "st-flag" else "salesreturn",
                         day, sid, day_rets, 0)

    # Черновик отгрузки (applicable=false) с большим количеством: синк обязан
    # его ПРОПУСТИТЬ — иначе эталон нетто-продаж не сойдётся (аудит 18.08).
    _add_doc("demand", DATES[-1], "st-2",
             [{"ext": "v-tee1-S", "qty": 999, "price_kop": 100000, "discount": 0}],
             99, applicable=False)

    _apply_stock_shapes(stock_by_day)
    return skus, stock_by_day, docs, sale_events, return_events


def _apply_stock_shapes(stock_by_day: dict) -> None:
    """Формы истории остатков, которых не даёт «монотонный» генератор (ревью 21.08).

    Базовый мир только распродаётся и никогда не пополняется, поэтому тест
    эквивалентности «прямой проход == обратная загрузка чанками» не проверял
    самые опасные для граничной заплатки случаи. Добавляем:
      - ПОЯВЛЕНИЕ ЗАНОВО после дыры ДЛИННЕЕ ЧАНКА (позиция исчезает из отчёта
        на 15 дат и возвращается);
      - ПЕРВОЕ ПОЯВЛЕНИЕ В СЕРЕДИНЕ истории (позиции нет в первые 30 дат);
      - позиция, положительная ТОЛЬКО НА САМОЙ СТАРОЙ дате;
      - ПУСТОЙ ОТЧЁТ за день (МойСклад вернул ноль строк по всем складам) —
        все явные нули за этот день и восстановление на следующий.
    Все эталоны expected_*() читают STOCK_BY_DAY, поэтому остаются верными.
    """
    def _drop(ext: str, idx_from: int, idx_to: int) -> None:
        for day in DATES[idx_from:idx_to]:
            for sid, _ in STORES:
                stock_by_day.get((day, sid), {}).pop(ext, None)

    _drop("v-dress1-S", 20, 35)          # дыра длиннее чанка → появление заново
    _drop("v-pants1-L", 0, 30)           # первое появление в середине истории
    _drop("v-shirt1-S", 1, len(DATES))   # положителен только на самой старой дате
    for sid, _ in STORES:                # пустой отчёт за день
        stock_by_day[(DATES[12], sid)] = {}


def _asm_meta(ext: str) -> dict:
    kind = "variant" if ext.startswith("v-") else "product"
    return {"href": f"{BASE}/entity/{kind}/{ext}?expand=none", "type": kind}


SKUS, STOCK_BY_DAY, DOCS, SALE_EVENTS, RETURN_EVENTS = _build_world()
SKU_BY_EXT = {s["ext"]: s for s in SKUS}


# ── Эталонные ожидания для теста ─────────────────────────────────────────────

def expected_net_sales(stores=tuple(TRADE_STORES)) -> dict:
    """{base_name: [нетто-шт, нетто-₽]} по выбранным складам за всю историю.

    service-позиции пропускаются: они исключены из аналитики продукта by design.
    """
    out = {}
    for e in SALE_EVENTS:
        if e["store"] not in stores:
            continue
        if "service" in SKU_BY_EXT[e["ext"]]["flags"]:
            continue
        base = SKU_BY_EXT[e["ext"]]["base"]
        rec = out.setdefault(base, [0.0, 0.0])
        rec[0] += e["qty"]
        rec[1] += e["price_kop"] / 100.0 * e["qty"] * (1 - e["discount"] / 100.0)
    for e in RETURN_EVENTS:
        if e["store"] not in stores:
            continue
        if "service" in SKU_BY_EXT[e["ext"]]["flags"]:
            continue
        base = SKU_BY_EXT[e["ext"]]["base"]
        rec = out.setdefault(base, [0.0, 0.0])
        rec[0] -= e["qty"]
        rec[1] -= e["price_kop"] / 100.0 * e["qty"] * (1 - e["discount"] / 100.0)
    return out


def expected_stock_today(stores=tuple(TRADE_STORES), include_service: bool = False) -> dict:
    """{base_name: остаток-шт на конец сегодняшнего дня} по выбранным складам.

    По умолчанию service-позиции пропускаются: они исключены из АНАЛИТИКИ
    продукта by design. include_service=True — сырой эталон для проверок БД
    (в warehouse_stock/stock_days исключённые позиции хранятся).
    """
    out = {}
    for sid in stores:
        for ext, qty in STOCK_BY_DAY[(DATES[-1], sid)].items():
            if not include_service and "service" in SKU_BY_EXT[ext]["flags"]:
                continue
            base = SKU_BY_EXT[ext]["base"]
            out[base] = out.get(base, 0.0) + qty
    return out


def sellout_ext_ids() -> list[str]:
    """SKU, распроданные в ноль на торговых складах (для проверки явных нулей)."""
    out = []
    for sku in SKUS:
        if "sellout" not in sku["flags"]:
            continue
        last = sum(STOCK_BY_DAY[(DATES[-1], sid)].get(sku["ext"], 0.0)
                   for sid in TRADE_STORES)
        had = any(
            STOCK_BY_DAY[(DATES[0], sid)].get(sku["ext"], 0.0) > 0
            for sid in TRADE_STORES
        )
        if had and last == 0:
            out.append(sku["ext"])
    return out


# ── FastAPI-приложение ───────────────────────────────────────────────────────

app = FastAPI(title="mock-moysklad")


def _auth(request: Request) -> None:
    if request.headers.get("Authorization") != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401, detail={"errors": [
            {"error": "Ошибка аутентификации: неправильный пароль или имя пользователя"}
        ]})


def _page(rows: list, limit: int, offset: int) -> dict:
    return {
        "meta": {"size": len(rows), "limit": limit, "offset": offset},
        "rows": rows[offset:offset + limit],
    }


@app.get("/entity/{entity}/{doc_id}/positions")
def entity_doc_positions(entity: str, doc_id: str, request: Request,
                         limit: int = 1000, offset: int = 0):
    """Позиции одного документа постранично (аудит 18.08: дочитывание
    хвоста документов >100 позиций)."""
    _auth(request)
    pool = list(DOCS.get(entity, [])) if entity in DOCS else []
    if entity == "purchaseorder":
        pool = PURCHASE_ORDERS
    for doc in pool:
        if doc.get("id") == doc_id:
            return _page(doc["positions"]["rows"], limit, offset)
    raise HTTPException(404, "document not found")


@app.get("/context/employee")
def context_employee(request: Request):
    _auth(request)
    return {"meta": {"type": "employee"}, "name": "Тестовый сотрудник"}


@app.get("/entity/store")
def entity_store(request: Request, limit: int = 1000, offset: int = 0):
    _auth(request)
    rows = [
        {"id": sid, "name": name,
         "meta": {"href": f"{BASE}/entity/store/{sid}", "type": "store"}}
        for sid, name in STORES
    ]
    return _page(rows, limit, offset)


# ── Именованные типы цен (DATA-10) ───────────────────────────────────────────
#
# По умолчанию ассортимент отдаёт РОВНО ОДНУ безымянную цену — как раньше.
# Это не осторожность ради осторожности: на числах этого мок-мира стоят
# денежные ожидания четырёх наборов, и появление второго элемента в
# salePrices сдвинуло бы _sale_price_of у всех сразу. Набор DATA-10 включает
# именованные типы явно, через POST /__test/price_types.
#
#   enabled    — выдавать ли priceType у цен вообще;
#   sale       — имя типа для цены продажи ('' — этой цены в карточке нет
#                вовсе: тип удалили в МойСкладе, и в salePrices осталась
#                только себестоимость — первой в списке);
#   cost       — имя типа для второй цены, полной себестоимости
#                ('' — второй цены в карточке нет вовсе);
#   cost_ratio — полная себестоимость = закупочная цена × ratio. Отдельное
#                число нужно, чтобы тест отличал «взяли выбранный тип» от
#                «взяли buyPrice» и от «взяли первую цену в списке»;
#   cost_skip  — ext_id, у которых второй цены нет: тип существует в
#                ассортименте глобально, но не проставлен на этой карточке.
PRICE_TYPES: dict = {
    "enabled": False,
    "sale": "Цена продажи",
    "cost": "Полная себестоимость",
    "cost_ratio": 1.5,
    "cost_skip": [],
}


def reset_price_types() -> None:
    PRICE_TYPES.update(enabled=False, sale="Цена продажи",
                       cost="Полная себестоимость", cost_ratio=1.5, cost_skip=[])


@app.post("/__test/price_types")
async def test_price_types(request: Request):
    body = await request.json()
    for key, val in (body or {}).items():
        if key in PRICE_TYPES:
            PRICE_TYPES[key] = val
    return dict(PRICE_TYPES)


def _sale_prices(ext_id: str, price_rub: int, cost_rub: int) -> list[dict]:
    """salePrices карточки: без переключателя — та же одна безымянная цена."""
    if not PRICE_TYPES.get("enabled"):
        return [{"value": price_rub * 100}]
    out: list[dict] = []
    sale_name = str(PRICE_TYPES.get("sale") or "")
    if sale_name:
        out.append({"value": price_rub * 100, "priceType": {"name": sale_name}})
    cost_name = str(PRICE_TYPES.get("cost") or "")
    if cost_name and ext_id not in (PRICE_TYPES.get("cost_skip") or []):
        out.append({
            "value": int(round(cost_rub * float(PRICE_TYPES.get("cost_ratio") or 1.0))) * 100,
            "priceType": {"name": cost_name},
        })
    return out


@app.get("/entity/assortment")
def entity_assortment(request: Request, limit: int = 1000, offset: int = 0):
    _auth(request)
    if int(FAULTS.get("assortment_429_burst") or 0) > 0:
        from fastapi.responses import JSONResponse
        FAULTS["assortment_429_burst"] -= 1
        return JSONResponse(status_code=429,
                            content={"errors": [{"error": "mock: Превышен лимит запросов"}]},
                            headers={"X-Lognex-Retry-TimeInterval": "200"})
    rows = []
    # родительские product для вариантных моделей
    for pid, name, path, price, cost, _rate, flags in SIZED:
        rows.append({
            "id": pid, "name": name, "pathName": path,
            "meta": {"href": f"{BASE}/entity/product/{pid}", "type": "product"},
            "salePrices": _sale_prices(pid, price, cost),
            "buyPrice": {"value": cost * 100},
            "archived": False,
        })
    for sku in SKUS:
        if sku["kind"] == "variant":
            chars = ([] if "nochar" in sku["flags"]
                     else [{"name": "Размер", "value": sku["size"]}])
            rows.append({
                "id": sku["ext"], "name": sku["name"],
                "meta": {"href": f"{BASE}/entity/variant/{sku['ext']}",
                         "type": "variant"},
                "characteristics": chars,
                "product": {"meta": {"href": f"{BASE}/entity/product/{sku['parent']}",
                                     "type": "product"}},
                "salePrices": _sale_prices(sku["ext"], sku["price"], sku["cost"]),
                "buyPrice": {"value": sku["cost"] * 100},
                "archived": False,
            })
        else:
            rows.append({
                "id": sku["ext"], "name": sku["name"], "pathName": sku["path"],
                "meta": {"href": f"{BASE}/entity/product/{sku['ext']}",
                         "type": "product"},
                "salePrices": _sale_prices(sku["ext"], sku["price"], sku["cost"]),
                "buyPrice": {"value": sku["cost"] * 100},
                "archived": "archived" in sku["flags"],
            })
    return _page(rows, limit, offset)


def _parse_filter(flt: str) -> dict:
    out = {}
    for part in (flt or "").split(";"):
        if not part:
            continue
        if ">=" in part:
            key, val = part.split(">=", 1)
            out[key.strip() + ">="] = val.strip()
        elif "<=" in part:
            key, val = part.split("<=", 1)
            out[key.strip() + "<="] = val.strip()
        elif "=" in part:
            key, val = part.split("=", 1)
            out[key.strip()] = val.strip()
    return out


# ── Инъекция сбоев (инцидент 21.08: 429 от /report/stock/all) ────────────────
#
# FAULTS управляются тестом напрямую (тот же процесс) или через
# POST /__test/faults (JSON с теми же ключами; счётчики FAULT_STATS сбрасываются):
#   stock_ok_before — сбои включаются только после N успешных ответов отчёта;
#   stock_429_burst — следующие N запросов отчёта → 429 с Retry-TimeInterval=200;
#   stock_429_every — каждый k-й запрос отчёта → 429 (0 — выключено);
#   stock_500_once  — ровно один ближайший запрос отчёта → 500;
#   stock_delay_ms  — задержка каждого удачного ответа отчёта (деплой П1:
#                     чтобы тест успел увидеть промежуточные состояния синка).

FAULTS: dict = {"stock_ok_before": 0, "stock_429_burst": 0,
                "stock_429_every": 0, "stock_500_once": False,
                "assortment_429_burst": 0,  # 429 на /entity/assortment (до истории)
                # 429 на документах продаж/возвратов: прерывает синк ровно
                # на продажах окна быстрого старта (ревью 21.08, мажор 2)
                "docs_429_burst": 0,
                # Заказ поставщику СОЗДАН, но ответ не дошёл (таймаут/502).
                # Ровно тот случай, ради которого писался маркер и поиск
                # документа перед созданием: слепой повтор дал бы дубль.
                "po_create_then_fail": 0,
                # POST /entity/purchaseorder отвечает 429 N раз подряд:
                # «мы даже не начали» — повтор безопасен и обязан произойти.
                "po_429_burst": 0,
                "stock_delay_ms": 0}
FAULT_STATS: dict = {"stock_requests": 0, "stock_ok": 0, "stock_429": 0, "stock_500": 0}


def reset_faults() -> None:
    FAULTS.update(stock_ok_before=0, stock_429_burst=0, stock_429_every=0,
                  stock_500_once=False, assortment_429_burst=0,
                  docs_429_burst=0, po_create_then_fail=0, po_429_burst=0,
                  stock_delay_ms=0)
    for k in FAULT_STATS:
        FAULT_STATS[k] = 0


@app.post("/__test/faults")
async def test_faults(request: Request):
    body = await request.json()
    reset_faults()
    for key, val in (body or {}).items():
        if key in FAULTS:
            FAULTS[key] = val
    return {"faults": FAULTS, "stats": FAULT_STATS}


def _maybe_fault_stock():
    """Возвращает Response-сбой для /report/stock/all или None."""
    from fastapi.responses import JSONResponse

    FAULT_STATS["stock_requests"] += 1
    if FAULTS["stock_500_once"]:
        FAULTS["stock_500_once"] = False
        FAULT_STATS["stock_500"] += 1
        return JSONResponse(status_code=500, content={"errors": [{"error": "mock: 500"}]})
    if FAULT_STATS["stock_ok"] >= int(FAULTS["stock_ok_before"] or 0):
        hit_429 = False
        if int(FAULTS["stock_429_burst"] or 0) > 0:
            FAULTS["stock_429_burst"] -= 1
            hit_429 = True
        elif int(FAULTS["stock_429_every"] or 0) > 0 and \
                FAULT_STATS["stock_requests"] % int(FAULTS["stock_429_every"]) == 0:
            hit_429 = True
        if hit_429:
            FAULT_STATS["stock_429"] += 1
            return JSONResponse(
                status_code=429,
                content={"errors": [{"error": "mock: Превышен лимит запросов"}]},
                headers={"X-Lognex-Retry-TimeInterval": "200"},
            )
    FAULT_STATS["stock_ok"] += 1
    return None


@app.get("/report/stock/all")
async def report_stock_all(request: Request, limit: int = 1000, offset: int = 0,
                           flt: str = Query(default="", alias="filter")):
    _auth(request)
    fault = _maybe_fault_stock()
    if fault is not None:
        return fault
    # Задержка — асинхронная: time.sleep сериализовал ВЕСЬ mock (соседние
    # даты переставали качаться параллельно, тест мерил не то).
    if int(FAULTS.get("stock_delay_ms") or 0) > 0:
        import asyncio as _asyncio
        await _asyncio.sleep(int(FAULTS["stock_delay_ms"]) / 1000.0)
    parsed = _parse_filter(flt)
    # КРИТИЧНО: moment учитывается ТОЛЬКО из filter; отдельный query-параметр
    # ?moment=... игнорируется — как в реальном МойСкладе.
    moment = parsed.get("moment", "")
    day = moment[:10] if moment else DATES[-1]
    store_href = parsed.get("store", "")
    if not store_href:
        raise HTTPException(status_code=412, detail="mock: нужен фильтр store")
    sid = store_href.rstrip("/").rsplit("/", 1)[-1]
    if sid not in {s for s, _ in STORES}:
        raise HTTPException(status_code=404, detail="mock: неизвестный склад")
    snapshot = STOCK_BY_DAY.get((day, sid), {})
    rows = [
        {"meta": _asm_meta(ext), "name": SKU_BY_EXT[ext]["name"], "stock": qty}
        for ext, qty in sorted(snapshot.items())
    ]
    return _page(rows, limit, offset)


def _docs_endpoint(entity: str, request: Request, limit: int, offset: int,
                   flt: str, expand: str) -> dict:
    _auth(request)
    if int(FAULTS.get("docs_429_burst") or 0) > 0:
        from fastapi.responses import JSONResponse
        FAULTS["docs_429_burst"] -= 1
        return JSONResponse(status_code=429,
                            content={"errors": [{"error": "mock: Превышен лимит запросов"}]},
                            headers={"X-Lognex-Retry-TimeInterval": "200"})
    parsed = _parse_filter(flt)
    m_from = parsed.get("moment>=", "")[:10]
    m_to = parsed.get("moment<=", "")[:10]
    rows = []
    for doc in DOCS[entity]:
        day = doc["moment"][:10]
        if m_from and day < m_from:
            continue
        if m_to and day > m_to:
            continue
        if "positions" in (expand or "") and limit <= 100:
            # как в реальном МС: expand вкладывает не более 100 строк позиций
            full = doc["positions"]["rows"]
            if len(full) > 100:
                doc = dict(doc)
                doc["positions"] = {"rows": full[:100],
                                    "meta": doc["positions"]["meta"]}
            rows.append(doc)
        else:
            # как в реальном МС: без expand (или при limit > 100)
            # positions приходят только meta-ссылкой, без rows
            stripped = dict(doc)
            stripped["positions"] = {"meta": doc["positions"]["meta"]}
            rows.append(stripped)
    return _page(rows, limit, offset)


@app.get("/entity/retaildemand")
def entity_retaildemand(request: Request, limit: int = 100, offset: int = 0,
                        flt: str = Query(default="", alias="filter"),
                        expand: str = ""):
    return _docs_endpoint("retaildemand", request, limit, offset, flt, expand)


@app.get("/entity/demand")
def entity_demand(request: Request, limit: int = 100, offset: int = 0,
                  flt: str = Query(default="", alias="filter"),
                  expand: str = ""):
    return _docs_endpoint("demand", request, limit, offset, flt, expand)


@app.get("/entity/salesreturn")
def entity_salesreturn(request: Request, limit: int = 100, offset: int = 0,
                       flt: str = Query(default="", alias="filter"),
                       expand: str = ""):
    return _docs_endpoint("salesreturn", request, limit, offset, flt, expand)


@app.get("/entity/retailsalesreturn")
def entity_retailsalesreturn(request: Request, limit: int = 100, offset: int = 0,
                             flt: str = Query(default="", alias="filter"),
                             expand: str = ""):
    return _docs_endpoint("retailsalesreturn", request, limit, offset, flt, expand)


# ── Заказы поставщику: «едет к нам» (импорт purchaseorder) ───────────────────
#
# Seeded-документы для проверки этапа incoming синка. У позиций purchaseorder
# МойСклад отдаёт quantity (заказано) и shipped (принято по привязанным
# приёмкам); «едет» = quantity − shipped по проведённым (applicable) докам
# за окно истории. Кейсы: частично принятый, непроведённый (skip),
# полностью принятый (0), старый за окном (skip), неизвестный SKU (skip).

def _po_seed():
    recent = (TODAY - timedelta(days=10)).isoformat()
    old = (TODAY - timedelta(days=HISTORY_DAYS + 30)).isoformat()

    def pos(ext, qty, shipped=0.0):
        return {"assortment": {"meta": _asm_meta(ext)},
                "quantity": qty, "shipped": shipped, "price": 100000}

    return [
        {  # частично принятый: Худи «Скетч» едет 6 + 8, сумка принята целиком
            "id": "po-seed-1", "name": "S-0001", "applicable": True,
            "moment": f"{recent} 12:00:00",
            "meta": {"href": f"{BASE}/entity/purchaseorder/po-seed-1",
                     "type": "purchaseorder"},
            "positions": {"rows": [
                pos("v-hoodie1-S", 10, 4.0),
                pos("v-hoodie1-M", 8),
                pos("p-bag1", 20, 20.0),
            ], "meta": {"size": 3}},
        },
        {  # черновик (непроведённый) — в «едет» не входит
            "id": "po-seed-2", "name": "S-0002", "applicable": False,
            "moment": f"{recent} 13:00:00",
            "meta": {"href": f"{BASE}/entity/purchaseorder/po-seed-2",
                     "type": "purchaseorder"},
            "positions": {"rows": [pos("v-tee1-S", 50)], "meta": {"size": 1}},
        },
        {  # старше окна истории — отсекается фильтром moment
            "id": "po-seed-3", "name": "S-0003", "applicable": True,
            "moment": f"{old} 12:00:00",
            "meta": {"href": f"{BASE}/entity/purchaseorder/po-seed-3",
                     "type": "purchaseorder"},
            "positions": {"rows": [pos("v-shirt1-M", 30)], "meta": {"size": 1}},
        },
        {  # позиция с неизвестным SKU — пропускается, док не валится
            "id": "po-seed-4", "name": "S-0004", "applicable": True,
            "moment": f"{recent} 14:00:00",
            "meta": {"href": f"{BASE}/entity/purchaseorder/po-seed-4",
                     "type": "purchaseorder"},
            "positions": {"rows": [pos("v-ghost-X", 5), pos("p-ring1", 7, 2.0)],
                          "meta": {"size": 2}},
        },
        {  # аудит 18.08: документ на 150 позиций — expand отдаёт только 100,
           # синк обязан дочитать хвост через /positions (иначе «едет» -50)
            "id": "po-seed-5", "name": "S-0005", "applicable": True,
            "moment": f"{recent} 15:00:00",
            "meta": {"href": f"{BASE}/entity/purchaseorder/po-seed-5",
                     "type": "purchaseorder"},
            "positions": {"rows": [pos("p-ring1", 1) for _ in range(150)],
                          "meta": {"size": 150}},
        },
        {  # D-28: ЧУЖОЙ документ с СКОПИРОВАННЫМ маркером «Оборота».
           # Так выглядит самый вероятный способ ошибиться: человек создал
           # документ копией нашего и не стёр текст описания. Маркер есть,
           # заказ №1 существует — но ссылка ведёт на другой документ, значит
           # это НЕ исполнение нашего решения. Синк обязан посчитать его
           # внешним, иначе «Оборот» припишет себе чужой заказ.
           # Номер намеренно несуществующий: сценарий «маркер существующего
           # заказа у чужого документа» проверяется отдельно, прямыми вызовами
           # классификатора (иначе seed ломает дедупликацию push-а — она по
           # той же метке ищет уже созданный документ).
            "id": "po-seed-6", "name": "S-0006", "applicable": True,
            "moment": f"{recent} 16:00:00",
            "description": "Создано в «Обороте»: заказ «Копия» [oborot#9091]",
            "meta": {"href": f"{BASE}/entity/purchaseorder/po-seed-6",
                     "type": "purchaseorder"},
            "positions": {"rows": [pos("v-tee1-M", 12)], "meta": {"size": 1}},
        },
    ]


PURCHASE_ORDERS: list[dict] = _po_seed()


def reset_purchase_orders() -> None:
    PURCHASE_ORDERS.clear()
    PURCHASE_ORDERS.extend(_po_seed())


def expected_incoming() -> dict:
    """{base_name: «едет» шт} из seeded-заказов поставщику (окно HISTORY_DAYS)."""
    cutoff = (TODAY - timedelta(days=HISTORY_DAYS - 1)).isoformat()
    out: dict[str, float] = {}
    for doc in PURCHASE_ORDERS:
        if doc.get("applicable") is False or doc["moment"][:10] < cutoff:
            continue
        for p in doc["positions"]["rows"]:
            href = p["assortment"]["meta"]["href"]
            ext = href.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
            sku = SKU_BY_EXT.get(ext)
            if sku is None:
                continue
            left = float(p["quantity"]) - float(p.get("shipped") or 0)
            if left > 0:
                out[sku["base"]] = out.get(sku["base"], 0.0) + left
    return {b: q for b, q in out.items() if q > 0}


@app.get("/entity/purchaseorder")
def entity_purchaseorder(request: Request, limit: int = 100, offset: int = 0,
                         flt: str = Query(default="", alias="filter"),
                         expand: str = ""):
    _auth(request)
    parsed = _parse_filter(flt)
    m_from = parsed.get("moment>=", "")[:10]
    rows = []
    # seeded + созданные writeback'ом (у последних МС проставил бы moment
    # и applicable сам — эмулируем: сегодня, проведён, shipped=0).
    for doc in PURCHASE_ORDERS + [
        {**d,
         "moment": d.get("moment") or f"{TODAY.isoformat()} 12:00:00",
         "applicable": d.get("applicable", True),
         "positions": {"rows": [{**p, "shipped": p.get("shipped", 0.0)}
                                for p in (d.get("positions") or [])],
                       "meta": {"size": len(d.get("positions") or [])}}}
        for d in CREATED_PURCHASE_ORDERS
    ]:
        day = doc["moment"][:10]
        if m_from and day < m_from:
            continue
        if "positions" in (expand or "") and limit <= 100:
            # как в реальном МС: expand вкладывает не более 100 строк позиций
            full = doc["positions"]["rows"]
            if len(full) > 100:
                doc = dict(doc)
                doc["positions"] = {"rows": full[:100],
                                    "meta": doc["positions"]["meta"]}
            rows.append(doc)
        else:
            stripped = dict(doc)
            stripped["positions"] = {"meta": doc["positions"]["meta"]}
            rows.append(stripped)
    return _page(rows, limit, offset)


# ── Обратная запись: организация, контрагенты, заказ поставщику ──────────────

ORG_EXT_ID = "org-main"

# Изменяемое состояние mock-мира для writeback-тестов.
COUNTERPARTIES: list[dict] = []          # {"id", "name"}
CREATED_PURCHASE_ORDERS: list[dict] = [] # тела принятых POST + присвоенные id/name


def reset_writeback_state() -> None:
    COUNTERPARTIES.clear()
    CREATED_PURCHASE_ORDERS.clear()


def _counterparty_row(cp: dict) -> dict:
    return {
        "id": cp["id"], "name": cp["name"],
        "meta": {"href": f"{BASE}/entity/counterparty/{cp['id']}",
                 "type": "counterparty", "mediaType": "application/json"},
    }


@app.get("/entity/organization")
def entity_organization(request: Request, limit: int = 1000, offset: int = 0):
    _auth(request)
    rows = [{
        "id": ORG_EXT_ID, "name": "ИП Тестовый",
        "meta": {"href": f"{BASE}/entity/organization/{ORG_EXT_ID}",
                 "type": "organization", "mediaType": "application/json"},
    }]
    return _page(rows, limit, offset)


@app.get("/entity/counterparty")
def entity_counterparty(request: Request, limit: int = 1000, offset: int = 0,
                        flt: str = Query(default="", alias="filter")):
    _auth(request)
    name = _parse_filter(flt).get("name", "")
    rows = [_counterparty_row(cp) for cp in COUNTERPARTIES
            if not name or cp["name"] == name]
    return _page(rows, limit, offset)


@app.post("/entity/counterparty")
async def entity_counterparty_create(request: Request):
    _auth(request)
    body = await request.json()
    name = str(body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=412, detail={"errors": [
            {"error": "Ошибка сохранения объекта: поле 'name' не может быть пустым"}
        ]})
    cp = {"id": f"cp-{len(COUNTERPARTIES) + 1:03d}", "name": name}
    COUNTERPARTIES.append(cp)
    return _counterparty_row(cp)


def _require_meta_href(body: dict, field: str, expected_type: str) -> str:
    href = str((((body.get(field) or {}).get("meta")) or {}).get("href") or "")
    if not href or f"/entity/{expected_type}/" not in href:
        raise HTTPException(status_code=412, detail={"errors": [
            {"error": f"Ошибка сохранения объекта: поле '{field}' не задано "
                      f"или не является метаданными {expected_type}"}
        ]})
    return href


@app.post("/entity/purchaseorder")
async def entity_purchaseorder_create(request: Request):
    _auth(request)
    body = await request.json()
    org_href = _require_meta_href(body, "organization", "organization")
    if org_href.rstrip("/").rsplit("/", 1)[-1] != ORG_EXT_ID:
        raise HTTPException(status_code=412, detail={"errors": [
            {"error": "Ошибка сохранения объекта: неизвестное юрлицо"}
        ]})
    agent_href = _require_meta_href(body, "agent", "counterparty")
    agent_id = agent_href.rstrip("/").rsplit("/", 1)[-1]
    if not any(cp["id"] == agent_id for cp in COUNTERPARTIES):
        raise HTTPException(status_code=412, detail={"errors": [
            {"error": "Ошибка сохранения объекта: контрагент не найден"}
        ]})
    positions = body.get("positions") or []
    if not isinstance(positions, list) or not positions:
        raise HTTPException(status_code=412, detail={"errors": [
            {"error": "Ошибка сохранения объекта: нужны позиции positions"}
        ]})
    for pos in positions:
        href = str((((pos.get("assortment") or {}).get("meta")) or {}).get("href") or "")
        ext = href.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
        if ext not in SKU_BY_EXT:
            raise HTTPException(status_code=412, detail={"errors": [
                {"error": f"Ошибка сохранения объекта: ассортимент {ext!r} не найден"}
            ]})
        if float(pos.get("quantity") or 0) <= 0:
            raise HTTPException(status_code=412, detail={"errors": [
                {"error": "Ошибка сохранения объекта: quantity должно быть > 0"}
            ]})
    num = len(CREATED_PURCHASE_ORDERS) + 1
    doc_id = f"po-{num:04d}"
    doc = dict(body)
    doc["id"] = doc_id
    doc["name"] = f"{num:05d}"
    doc["meta"] = {
        "href": f"{BASE}/entity/purchaseorder/{doc_id}",
        "type": "purchaseorder", "mediaType": "application/json",
        "uuidHref": f"https://online.moysklad.ru/app/#purchaseorder/edit?id={doc_id}",
    }
    if int(FAULTS.get("po_429_burst") or 0) > 0:
        # Отказ ДО создания: документа нет, повтор обязателен и безопасен.
        FAULTS["po_429_burst"] = int(FAULTS["po_429_burst"]) - 1
        raise HTTPException(status_code=429, detail="rate limited",
                            headers={"X-Lognex-Retry-TimeInterval": "200"})
    CREATED_PURCHASE_ORDERS.append(doc)
    if int(FAULTS.get("po_create_then_fail") or 0) > 0:
        # Документ создан — и только потом «падает» ответ. Именно так
        # выглядит таймаут на стороне клиента: он не знает, что случилось.
        FAULTS["po_create_then_fail"] = int(FAULTS["po_create_then_fail"]) - 1
        raise HTTPException(status_code=502, detail="bad gateway")
    return doc


# ── Vendor API 1.0: обмен contextKey (приложение маркетплейса) ───────────────
#
# В реальности Vendor API живёт на apps-api.moysklad.ru, но для тестов
# приложение указывает MS_VENDOR_API_BASE на этот же mock. Эндпоинт проверяет
# НАШ JWT (HS256 общим секретом, sub=appUid) — так тест ловит и битую подпись,
# и неправильный sub. Контексты регистрирует тест через VENDOR_CONTEXTS.

VENDOR_APP_ID = "app-oborot-test-0001"
VENDOR_APP_UID = "oborot.test-vendor"
VENDOR_SECRET = "vendor-secret-key-for-tests-do-not-use-in-prod"
VENDOR_ACCOUNT_ID = "acc-11111111-2222-3333-4444-555555555555"
VENDOR_ACCOUNT_NAME = "ООО «Тест-Бренд» (аккаунт МС)"

VENDOR_CONTEXTS: dict[str, dict] = {}  # contextKey -> {"accountId", "uid", ...}


@app.post("/context/{context_key}")
def vendor_context(context_key: str, request: Request):
    import jwt as pyjwt

    raw = request.headers.get("Authorization") or ""
    token = raw[7:].strip() if raw.lower().startswith("bearer ") else raw.strip()
    try:
        claims = pyjwt.decode(token, VENDOR_SECRET, algorithms=["HS256"],
                              options={"require": ["exp", "iat"]})
    except pyjwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="mock: подпись вендорского JWT не сошлась")
    if claims.get("sub") != VENDOR_APP_UID:
        raise HTTPException(status_code=401, detail="mock: sub != appUid")
    if not claims.get("jti"):
        raise HTTPException(status_code=401, detail="mock: нет jti")
    ctx = VENDOR_CONTEXTS.get(context_key)
    if ctx is None:
        raise HTTPException(status_code=404, detail="mock: contextKey не найден")
    return ctx


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
