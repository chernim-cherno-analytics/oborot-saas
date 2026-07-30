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
  и expand positions приходят href-ами без строк (как в реальном МС).

Данные детерминированные: random.Random(SEED), 60 дней истории.
Модуль экспортирует expected_*() — эталонные суммы для проверок теста.
"""
import random
from datetime import date, timedelta

from fastapi import FastAPI, HTTPException, Query, Request

PORT = 9800
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
#        dead — не продаётся вовсе (неликвид).
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
    docs = {"retaildemand": [], "demand": [], "salesreturn": []}

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

    def _add_doc(entity, day, sid, events, num):
        docs[entity].append({
            "id": f"{entity}-{day}-{sid}-{num}",
            "meta": {"href": f"{BASE}/entity/{entity}/{entity}-{day}-{sid}-{num}",
                     "type": entity},
            "moment": f"{day} 14:30:00",
            "store": {"meta": {"href": f"{BASE}/entity/store/{sid}",
                               "type": "store"}},
            "positions": {"rows": _positions(events),
                          "meta": {"size": len(events)}},
        })

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
                _add_doc("salesreturn", day, sid, day_rets, 0)

    return skus, stock_by_day, docs, sale_events, return_events


def _asm_meta(ext: str) -> dict:
    kind = "variant" if ext.startswith("v-") else "product"
    return {"href": f"{BASE}/entity/{kind}/{ext}?expand=none", "type": kind}


SKUS, STOCK_BY_DAY, DOCS, SALE_EVENTS, RETURN_EVENTS = _build_world()
SKU_BY_EXT = {s["ext"]: s for s in SKUS}


# ── Эталонные ожидания для теста ─────────────────────────────────────────────

def expected_net_sales(stores=tuple(TRADE_STORES)) -> dict:
    """{base_name: [нетто-шт, нетто-₽]} по выбранным складам за всю историю."""
    out = {}
    for e in SALE_EVENTS:
        if e["store"] not in stores:
            continue
        base = SKU_BY_EXT[e["ext"]]["base"]
        rec = out.setdefault(base, [0.0, 0.0])
        rec[0] += e["qty"]
        rec[1] += e["price_kop"] / 100.0 * e["qty"] * (1 - e["discount"] / 100.0)
    for e in RETURN_EVENTS:
        if e["store"] not in stores:
            continue
        base = SKU_BY_EXT[e["ext"]]["base"]
        rec = out.setdefault(base, [0.0, 0.0])
        rec[0] -= e["qty"]
        rec[1] -= e["price_kop"] / 100.0 * e["qty"] * (1 - e["discount"] / 100.0)
    return out


def expected_stock_today(stores=tuple(TRADE_STORES)) -> dict:
    """{base_name: остаток-шт на конец сегодняшнего дня} по выбранным складам."""
    out = {}
    for sid in stores:
        for ext, qty in STOCK_BY_DAY[(DATES[-1], sid)].items():
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


@app.get("/entity/assortment")
def entity_assortment(request: Request, limit: int = 1000, offset: int = 0):
    _auth(request)
    rows = []
    # родительские product для вариантных моделей
    for pid, name, path, price, cost, _rate, flags in SIZED:
        rows.append({
            "id": pid, "name": name, "pathName": path,
            "meta": {"href": f"{BASE}/entity/product/{pid}", "type": "product"},
            "salePrices": [{"value": price * 100}],
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
                "salePrices": [{"value": sku["price"] * 100}],
                "buyPrice": {"value": sku["cost"] * 100},
                "archived": False,
            })
        else:
            rows.append({
                "id": sku["ext"], "name": sku["name"], "pathName": sku["path"],
                "meta": {"href": f"{BASE}/entity/product/{sku['ext']}",
                         "type": "product"},
                "salePrices": [{"value": sku["price"] * 100}],
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


@app.get("/report/stock/all")
def report_stock_all(request: Request, limit: int = 1000, offset: int = 0,
                     flt: str = Query(default="", alias="filter")):
    _auth(request)
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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
