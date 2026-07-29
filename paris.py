# -*- coding: utf-8 -*-
"""
Страница «Париж»: остатки международного магазина (Shopify, склад Chernim Cherno France)
рядом с российскими остатками + план подсортировки «привезти в Париж».

Подключение: в конце main.py
    try:
        import paris; paris.attach(app)
    except Exception as e:
        print("paris attach failed:", e)

Требует переменные окружения (client credentials grant, Dev Dashboard app "oborot"):
    SHOPIFY_CLIENT_ID     — Client ID приложения
    SHOPIFY_CLIENT_SECRET — Secret приложения
    SHOPIFY_SHOP          — домен магазина (по умолчанию chernim-cherno.myshopify.com)
Либо (запасной вариант) статический SHOPIFY_TOKEN.
"""
import os, time, json
import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse, FileResponse

SHOP = os.environ.get("SHOPIFY_SHOP", "chernim-cherno.myshopify.com")
STATIC_TOKEN = os.environ.get("SHOPIFY_TOKEN", "")
CLIENT_ID = os.environ.get("SHOPIFY_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("SHOPIFY_CLIENT_SECRET", "")
API_VER = "2024-10"

_TOK = {"t": 0, "token": None}  # access token, живёт 24 ч — обновляем каждые 23 ч


def _get_token():
    if STATIC_TOKEN:
        return STATIC_TOKEN
    if not (CLIENT_ID and CLIENT_SECRET):
        return None
    now = time.time()
    if _TOK["token"] and now - _TOK["t"] < 23 * 3600:
        return _TOK["token"]
    r = httpx.post(
        f"https://{SHOP}/admin/oauth/access_token",
        data={"grant_type": "client_credentials",
              "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
        timeout=20)
    r.raise_for_status()
    tok = r.json().get("access_token")
    _TOK["token"] = tok
    _TOK["t"] = now
    return tok

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# Сопоставление названий: английское (Shopify) -> русское базовое имя (МойСклад)
# Ключ — точное название товара в Shopify. None = соответствие не найдено.
# ---------------------------------------------------------------------------
PARIS_MAP = {
    "\"Leopard\" Satin Shirt": "Атласная леопардовая рубашка",
    "\"Origami\" Shirt": "Рубашка-оригами",
    "\"Sketch\" Shirt": "Рубашка \"Скетч\"",
    "\"Leopard\" Coat": "Пальто \"Леопард\"",
    "\"Shar Pei\" Metallic Bomber": "Бомбер-шарпей металлик",
    "\"Shar Pei\" Black Bomber": "Бомбер-шарпей черный",
    "\"Fata Morgana\" Black Jacket": "Черный пиджак \"Фата-моргана\"",
    "\"Thunderbolt\" Black Jacket": "Черный пиджак \"Молниеносный\"",
    "\"Night Out\" Grey Jacket": "Пиджак серый \"На выход\"",
    "Black and Grey Double Jacket": "Пиджак двойной черно-серый",
    "\"Night Out\" Black Jacket": "Пиджак черный \"На выход\"",
    "\"Night Out\" Brown Jacket": "Пиджак коричневый \"На выход\"",
    "\"Riding the Wave\" Black Trousers": "Черные брюки \"На волне\"",
    "\"Riding the Wave\" Beige Trousers": "Бежевые брюки \"На волне\"",
    "\"Downhill\" Black Trousers": "Черные брюки \"По наклонной\"",
    "\"Rugby\" Grey Trousers": "Серые брюки \"Регби\"",
    "\"Dandy\" Black Trousers": "Черные брюки \"Франт\"",
    "\"Tie\" Black Trousers": "Черные брюки \"Галстук\"",
    "\"Sneakers\" Black Trousers": "Черные брюки \"Сникерс\"",
    "\"Tie\" Grey Trousers": "Серые брюки \"Галстук\"",
    "\"Night Out\" Grey Trousers": "Брюки серые \"На выход\"",
    "\"Night Out\" Black Trousers": "Брюки черные \"На выход\"",
    "Grey-Black Trousers with Patch Pockets": "Серо-черные брюки с накладными карманами",
    "Black Trousers with Patch Pockets": "Черные брюки с накладными карманами",
    "\"Forward\" Green Sweater": "Зеленый свитер \"Нападающий\"",
    "\"Forward\" Red Sweater": "Красный свитер \"Нападающий\"",
    "\"Forward\" Blue Sweater": "Синий свитер \"Нападающий\"",
    "\"Forward\" Grey Sweater": "Серый свитер \"Нападающий\"",
    "\"Pleasant\" Ivory Sweater": "Сливочный свитер \"Приятный\"",
    "\"No Frills\" Brown Long Sleeve": "Коричневый лонгслив \"Всё ровно\"",
    "\"Red Heart\" Black Sweater": "Черный свитер с красным сердцем",
    "\"Comfort Plus\" White Sweater with Lettering": "Белый свитер \"Комфорт плюс\" с надписью",
    "\"Comfort Plus\" White Sweater with Logo": "Белый свитер \"Комфорт плюс\" с логотипом",
    "\"Cascade\" Black Sweatshirt": "Черный свитшот \"Каскад\"",
    "\"Blacker Than Black\" Sweater": "Свитер \"Чернее черного\"",
    "\"Rugby\" Brown Sweatshirt": "Свитшот \"Регби\" коричневый",
    "\"Rugby\" Black Sweatshirt": "Свитшот \"Регби\" черный",
    "\"Sketch\" Blue Hoodie": "Синий худи \"Скетч\"",
    "\"Sketch\" Black Hoodie": "Черный худи \"Скетч\"",
    "\"Sketch\" Red Hoodie": "Красный худи \"Скетч\"",
    "\"Sketch\" Grey Hoodie": "Серый худи \"Скетч\"",
    "\"Riding the Wave\" Black Denim Shirt": "Черная джинсовая рубашка \"На волне\"",
    "\"Riding the Wave\" Beige Denim Shirt": "Бежевая джинсовая рубашка \"На волне\"",
    "\"The Rock\" Black Shirt": "Черная рубашка \"Скала\"",
    "\"Twilight\" Shirt": "Рубашка \"Полутьма\"",
    "\"Swan\" Light Blue Shirt": "Сизая рубашка \"Лебедь\"",
    "\"Swan\" Beige Shirt": "Рубашка \"Лебедь\"",
    "\"Strictness\" White Shirt": "Белая рубашка \"Строгость\"",
    "\"Pattern\" White Shirt": "Белая рубашка \"Выкройка\"",
    "\"Matador\" Light Green Shirt": "Салатовая рубашка \"Матадор\"",
    "\"Matador\" Light Blue Shirt": "Голубая рубашка \"Матадор\"",
    "\"Matador\" Black Shirt": "Черная рубашка \"Матадор\"",
    "\"Butterfly\" Light Blue Shirt": "Голубая рубашка \"Бабочка\"",
    "\"Sketch\" Black Shirt": "Черная рубашка \"Скетч\"",
    "\"Maneuver, Maneuver\" Shirt": "Рубашка \"Лавировали, лавировали\"",
    "\"Tie\" Black Shirt": "Черная рубашка \"Галстук\"",
    "\"Tie\" Grey Shirt": "Серая рубашка \"Галстук\"",
    "\"Swallowtail\" Black Shirt": "Черная рубашка \"Ласточкин хвост\"",
    "\"Mandarin\" White Shirt": "Белая рубашка \"Мандарин\"",
    "\"Bow Tie\" Black Shirt": "Черная рубашка \"Галстук-бабочка\"",
    "\"Amalfi\" Yellow Shirt": "Желтая рубашка \"Амальфи\"",
    "\"Carte blanche\" White Silk Shirt": "Белая шелковая рубашка \"Carte blanche\"",
    "\"We'll See\" Black Tank Top": "Черная майка \"Посмотрим\"",
    "\"Antique\" Burgundy Tank Top": "Фиолетовая майка \"Античная\"",
    "\"Antique\" White Tank Top": "Белая майка \"Античная\"",
    "\"Antique\" Brown Tank Top": "Коричневая майка \"Античная\"",
    "\"Crossroad\" Red Tank Top": "Красная майка \"Перекресток\"",
    "\"On Thin Ice\" White T-Shirt": "Белая футболка \"По тонкому льду\"",
    "\"On Thin Ice\" Black T-Shirt": "Черная футболка \"По тонкому льду\"",
    "\"Dragonfly\" Black Sleeveless Tank Top": "Черная майка без рукавов \"Стрекоза\"",
    "\"Saturn\" White T-Shirt": "Белая футболка \"Сатурн\"",
    "\"Saturn\" Black T-Shirt": "Черная футболка \"Сатурн\"",
    "\"Kiss\" White Sleeveless T-Shirt": "Белая футболка без рукавов \"I kiss better than AI\"",
    "\"On a Cruise\" T-Shirt with Blue Details": "Футболка \"В круиз\" с синими вставками",
    "\"On a Cruise\" T-Shirt with Grey Details": "Футболка \"В круиз\" с серыми вставками",
    "\"Origami\" Black Shorts": "Черные шорты \"Оригами\"",
    "\"Riding the Wave\" Beige Denim Shorts": "Бежевые джинсовые шорты \"На волне\"",
    "\"Riding the Wave\" Black Denim Shorts": "Черные джинсовые шорты \"На волне\"",
    "\"The Rock\" Black Shorts": "Черные шорты \"Скала\"",
    "\"Decent\" Brown Suiting Shorts": "Коричневые шорты из костюмной ткани \"Приличные\"",
    "\"Decent\" Grey Tailored Shorts": "Серые шорты из костюмной ткани \"Приличные\"",
    "\"Decent\" Black Tailored Shorts": "Чёрные шорты из костюмной ткани \"Приличные\"",
    "\"Heads or Tails\" Shorts with Blue Insert": "Шорты \"Орел и Решка\" с голубой вставкой",
    "\"Heads or Tails\" Shorts with Green Insert": "Шорты \"Орел и Решка\" с зеленой вставкой",
    "\"Heads or Tails\" Shorts with Grey Insert": "Шорты \"Орел и Решка\" с серой вставкой",
    "\"Matador\" Sunglasses Bloody Gold": "Очки \"Матадор\" Fakoshima x ChernimCherno",
    "\"Matador\" Sunglasses Brown-Silver": "Очки \"Матадор\" Fakoshima x ChernimCherno",
    "\"Eyebrow\" Sunglasses Black-Gold": "Очки с бровями Fakoshima x ChernimCherno",
    "\"Eyebrow\" Sunglasses Black-Silver": "Очки с бровями Fakoshima x ChernimCherno",
    "\"Eyebrow\" Sunglasses Bloody-Gold": "Очки с бровями Fakoshima x ChernimCherno",
    "\"Eyebrow\" Sunglasses Black-Gun Metal": "Очки с бровями Fakoshima x ChernimCherno",
    "\"Eyebrow\" Sunglasses Brown-Silver 2": "Очки с бровями Fakoshima x ChernimCherno",
    "\"Hare\" Black Belt": "Ремень \"Заяц\" черный",
    "\"Hare\" Silver Belt": "Ремень \"Заяц\" серебряный",
    "\"Zipper\" Silver Belt": "Ремень \"Молния\" серебряный",
    "Boxer Triple Pack Brown": "Коричневые боксеры \"Боксёр\"",
    "Boxer Triple Pack White": "Белые боксеры \"Боксёр\"",
    "Ribbed White Briefs": "Белые брифы \"В рубчик\"",
    "Ribbed Pink Briefs": "Розовые брифы \"В рубчик\"",
    "Ribbed Grey Briefs": "Серые брифы \"В рубчик\"",
    "Ribbed Pink Boxers": "Розовые боксеры \"В рубчик\"",
    "\"Starting Monday\" Sports Bag": "Спортивная сумка \"С понедельника\"",
    "\"Essential\" Bracelet": "Браслет \"База\"",
    "\"Essential\" Necklace": "Цепь на шею \"База\"",
    "\"Wave\" Ring": "Кольцо \"Волна\"",
    "Chain Ring": "Кольцо \"Цепь\"",
    "\"Summer\" Blue Jeans": "Голубые джинсы \"Лето\"",
    "\"Flash\" Black Pants": "Черные брюки \"Молниеносные\"",
    "\"White Smoke\" Trench Coat": "Тренч \"Белый дым\"",
    "\"Been Looking for This\" Black Long Sleeve": "Чёрный лонгслив \"Искал такой\"",
    "\"Been Looking for This\" Yellow T-shirt": "Желтая футболка \"Искал такую\"",
    "\"Been Looking for This\" Brown T-shirt": "Коричневая футболка \"Искал такую\"",
    "\"Been Looking for This\" Grey T-shirt": "Серая футболка \"Искал такую\"",
    "\"Interference\" Black and White Long Sleeve": "Чёрно-белый лонгслив \"Интерференция\"",
    "\"Cherry Cola Nights\" Pink Tank Top": "Розовая майка \"Cherry Cola Nights\"",
    "\"Mon Amour, Go Away With Me\" White Tank Top": "Белая майка \"Mon Amour\"",
    "\"Amour in July\" Black Tank Top": "Синяя майка \"Amour in July\"",
    "Blue Jeans \"Octopus\"": "Голубые джинсы \"Осьминог\"",
    "\"White Heart\" Black Sweater": "Черный свитер с белым сердцем",
    "\"Field\" Black Trousers": "Черные брюки \"Поле\"",
    "\"Snake Tongue\" Lip Cuff": "Кафф на губу \"Змеиный язык\"",
    "\"Lollipop\" Pendant": "Подвеска \"Леденец\"",
    "\"Knife\" Pendant": "Подвеска \"Нож\"",
    "\"Sword\" Pendant": "Подвеска \"Меч\"",
    "\"Arrow\" Pendant": "Подвеска \"Стрела\"",
    "Thin Chain Necklace 55 cm": "Тонкая цепочка 55 см",
    "\"Jazz\" White Trousers": "Молочные брюки \"Джаз\"",
    "\"Jazz\" Black Trousers": "Черные брюки \"Джаз\"",
    "\"Saturn\" Black Trousers": "Черные брюки \"Сатурн\"",
    "\"Under the Gun\" Brown Trousers": "Коричневые брюки \"Под прицелом\"",
    "\"Under the Gun\" Black Trousers": "Черные брюки \"Под прицелом\"",
    "\"Nowhere\" Tank Top": "Укороченная майка \"The south coast of Nowhere\"",
    "Black Satin Trousers": "Атласные черные брюки",
    "Metallic Satin Trousers": "Атласные брюки металлик",
    "Blue Satin Trousers": "Атласные синие брюки",
    "Black Jersey with Reliefs": "Черная майка с рельефами мужская",
    "White Jersey with Reliefs": "Майка с рельефами мужская",
    "\"Summer\" Milk Tank Top": "Молочная майка \"Лето\"",
    "\"Summer\" Grey Tank Top": "Серая майка \"Лето\"",
    "\"Holster\" White Tank Top": "Белая майка с вставками \"Кобура\"",
    "\"No plans\" White T-Shirt": "Белая футболка с контрастными вставками \"No plans\"",
    "\"No plans\" Black Sleeveless Tank Top": "Черная футболка без рукавов \"No plans\"",
    "\"Call me maybe never\" Light Blue Sleeveless Tank Top": "Голубая футболка без рукавов \"Call me maybe never\"",
    "\"Escaping Reality\" Light Blue Sleeveless Tank Top": "Голубая футболка без рукавов \"Escaping Reality\"",
    "\"Lost in summer\" Black Sleeveless Tank Top": "Черная футболка без рукавов \"Lost in summer\"",
    "\"Going Out\" Beige Trousers": "Брюки бежевые \"На выход\"",
    "\"Amalfi\" Black Sweater": "Черный свитер \"Амальфи\"",
    "\"Amalfi\" Brown Sweater": "Коричневый свитер \"Амальфи\"",
    "\"The Sea is Restless\" Milk Sweatshirt": "Молочный свитшот \"Море волнуется раз\"",
    # ---- соответствие не найдено (нет очевидного русского имени) ----
    "\"Puck\" Black Trousers": "Брюки со складками",
    "\"Cowboy\" White Shirt": "Белая рубашка \"Ковбой\"",
    "\"Cowboy\" Black Shirt": "Черная рубашка \"Ковбой\"",
    "\"Heads and Tails\" Blue-Light Blue Shirt": "Рубашка сине-голубая \"Орёл и Решка\"",
    "\"Heads and Tails\" White and Light Blue Shirt": "Рубашка бело-голубая \"Орёл и Решка\"",
    "\"Good Guy\" Jacket": "Куртка \"Хорошего парня\"",
    "\"Bad Guy\" Jacket": "Куртка \"Плохого парня\"",
    "\"Trinity\" Black Trench Coat": "Черный тренч \"Тринити\"",
    "\"Black Stars\" Jacket": "Черная куртка \"Звезды\"",
    "\"Dance\" Black Trousers": "Черные брюки \"Танец\"",
    "\"Yes/No\" Black Sweater": "Черный свитер \"Yes/No\"",
    "\"Chernish\" White Shirt": "Белая рубашка \"Черныш\"",
    "\"Cupid\" Black and White Shirt": "Черно-белая рубашка \"Амур\"",
    "\"Cupid\" Blue and Black Shirt": "Голубо-черная рубашка \"Амур\"",
    "\"Shine\" Silver Shirt": "Серебряная рубашка \"Блеск\"",
    "\"Drama\" White Jacquard Shirt": "Белая жаккардовая рубашка \"Драма\"",
    "\"Drama\" Black Jacquard Shirt": "Черная жаккардовая рубашка \"Драма\"",
    "\"Tulip\" Khaki and Black Shirt": "Рубашка \"Тюльпан\" хаки с черным",
    "\"Tulip\" Black and White Shirt": "Рубашка \"Тюльпан\" черно-белая",
    "Boxer Triple Pack Multicolor": "Комплект разноцветных боксеров \"Боксёр\"",  # ключи РФ канонизированы (_canon_name срезает финальные скобки) — "(3 шт)" в значении не матчился никогда
    "\"Hitman\" Yellow Bag": "Желтая сумка \"Килл Билл\"",
    "\"Hitman\" Black Bag": "Черная сумка \"Килл Билл\"",
    "\"Evening\" Black Shorts": "Черные шорты \"На вечер\"",
    "Chainmail Crop Top": "Кроп кольчуга",
    "Chainmail Bib": "Манишка кольчуга",
    "Black & White Scarf": "Шарф",
    "12 mm Hoop Earring": "Серьга 12 мм",
    "\"Rugby\" Black Bomber": "Черный бомбер \"Регби\"",
}

_CACHE = {"t": 0, "data": None}
CACHE_SEC = 600

_ONE = {"default title", "one size", "one-size", "os"}


def _norm_size(s):
    if not s or s.strip().lower() in _ONE:
        return "One Size"
    return s.strip()


def _fetch_shopify():
    try:
        token = _get_token()
    except Exception as e:
        return {"error": "не удалось получить токен Shopify: " + str(e)[:200]}
    if not token:
        return {"error": "SHOPIFY_CLIENT_ID / SHOPIFY_CLIENT_SECRET не заданы в Environment на Render"}
    url = f"https://{SHOP}/admin/api/{API_VER}/graphql.json"
    headers = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}
    q = """
    query($first: Int!, $after: String) {
      products(first: $first, after: $after, query: "status:active") {
        pageInfo { hasNextPage endCursor }
        nodes {
          title
          totalInventory
          variants(first: 30) { nodes { title inventoryQuantity } }
        }
      }
    }"""
    items, after = [], None
    for _ in range(20):
        r = httpx.post(url, headers=headers,
                       json={"query": q, "variables": {"first": 100, "after": after}},
                       timeout=30)
        if r.status_code == 429:
            time.sleep(2.5)
            continue
        if r.status_code == 401 and not STATIC_TOKEN:
            _TOK["token"] = None  # токен истёк — берём свежий и повторяем
            token = _get_token()
            headers["X-Shopify-Access-Token"] = token
            continue
        r.raise_for_status()
        j = r.json()
        if "errors" in j and not j.get("data"):
            return {"error": str(j["errors"])[:300]}
        pr = j["data"]["products"]
        for p in pr["nodes"]:
            sizes = {}
            for v in p["variants"]["nodes"]:
                sz = _norm_size(v.get("title"))
                sizes[sz] = sizes.get(sz, 0) + int(v.get("inventoryQuantity") or 0)
            en = p["title"].strip()
            items.append({
                "en": en,
                "ru": PARIS_MAP.get(en),
                "mapped": en in PARIS_MAP,
                "total": int(p.get("totalInventory") or 0),
                "sizes": sizes,
            })
        if not pr["pageInfo"]["hasNextPage"]:
            break
        after = pr["pageInfo"]["endCursor"]
    return {"shop": SHOP, "fetched": time.strftime("%Y-%m-%d %H:%M"), "items": items}


def paris_stock(force: int = 0):
    now = time.time()
    if not force and _CACHE["data"] and now - _CACHE["t"] < CACHE_SEC:
        return JSONResponse(_CACHE["data"])
    try:
        data = _fetch_shopify()
    except Exception as e:
        data = {"error": str(e)[:300]}
    if "error" not in data:
        _CACHE["data"] = data
        _CACHE["t"] = now
    return JSONResponse(data)


def paris_page():
    return FileResponse(os.path.join(BASE_DIR, "paris.html"))


def money_page():
    return FileResponse(os.path.join(BASE_DIR, "money.html"))


_SBS_CACHE = {}   # days -> (timestamp, data)


def sales_by_size(days: int = 365):
    """Нетто-продажи по размерам за последние N дней: {base: {size: qty}}.
    Размер — из скобок в конце полного имени МойСклада. Кэш 1 час (по ключу days).
    ВАЖНО: нетто агрегируется по каноническому base+size, фильтр q<=0 — ПОСЛЕ
    агрегации. Раньше фильтр стоял до неё (по полному имени): возврат по старому/
    переименованному имени с отрицательным нетто выбрасывался целиком и не вычитался
    из канонической позиции — доли размеров на /order завышались."""
    import re
    import main as site
    from datetime import date, timedelta
    now = time.time()
    cached = _SBS_CACHE.get(days)
    if cached is not None and now - cached[0] < 3600:
        return JSONResponse(cached[1])
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    conn = site.get_db()
    rows = conn.execute(
        "SELECT sku_name, SUM(CASE WHEN doc_type='sale' THEN qty ELSE -qty END) AS q "
        "FROM sales_data WHERE date >= ? GROUP BY sku_name", (cutoff,)).fetchall()
    conn.close()
    out = {}
    for row in rows:
        full = str(row["sku_name"] or "")
        q = float(row["q"] or 0)
        if not full:
            continue
        base = site._canon_name(full)
        m = re.search(r"\(([^)]+)\)\s*$", full)
        size = m.group(1).strip() if m else "One Size"
        d = out.setdefault(base, {})
        d[size] = d.get(size, 0) + q            # со знаком: возвраты вычитаются
    for base in list(out.keys()):               # фильтр после агрегации
        out[base] = {s: round(q, 1) for s, q in out[base].items() if q > 0}
        if not out[base]:
            del out[base]
    _SBS_CACHE[days] = (now, out)
    return JSONResponse(out)


def attach(app: FastAPI):
    from fastapi.routing import APIRoute
    routes = [
        APIRoute("/paris", paris_page, methods=["GET"]),
        APIRoute("/money", money_page, methods=["GET"]),
        APIRoute("/api/paris-stock", paris_stock, methods=["GET"]),
        APIRoute("/api/sales-by-size", sales_by_size, methods=["GET"]),
    ]
    for r in routes:
        app.router.routes.insert(0, r)
    print("paris module attached")
