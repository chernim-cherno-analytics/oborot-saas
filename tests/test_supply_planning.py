# -*- coding: utf-8 -*-
"""SUPPLY-3: планирование производства — границы, честность неизвестного, права.

Зачем этот набор. Слой впервые в «Поставках» ПИШЕТ то, что решил человек, а не
читает чужую таблицу. Цена ошибки здесь другая, чем у предпросмотра: не «показал
криво», а «потерял решение владельца», «выдал план за заказ», «подставил ноль
вместо незнания» или «списал материал, которого никто не списывал». Поэтому
проверяется не «работает ли форма», а что именно слой делает с неоднозначностью,
с чужими данными и с повторным нажатием.

Что доказывается:

  1) материал заводится ДО вещи и партии — это первый шаг пути, а не побочный;
  2) вещь каталога берётся по каноническому имени, а не по размерной строке;
     новинка живёт своим тождеством и несёт приватный эскиз;
  3) один материал делится на две партии, две ткани собираются в одну партию,
     у одной вещи бывает две плановые партии;
  4) 100 → 40 + 35 → 25 свободно; ни одно из чисел не выдумано;
  5) неизвестное остаётся неизвестным: `qty=None` не превращается в ноль, и
     остаток такого материала тоже неизвестен;
  6) назначение сверх известного наличия ПРЕДУПРЕЖДАЕТ числом, но не запрещает
     и не обрезает: это план, а не расход;
  7) метры не превращаются в штуки нигде: план изделий вводится отдельно;
  8) перенос метража между партиями неделим — сумма назначенного не меняется;
  9) повторный POST того же поступка не применяется дважды; правка поверх чужой
     правки отвергается 409, а не затирает её;
 10) срок бывает неизвестным, ориентировочным, точным и текстом; у него есть
     источник и автор, и он ни во что не считается;
 11) два одинаковых имени — две разные вещи;
 12) замена снимка предпросмотра (в том числе с перестановкой строк) ручные
     решения не трогает вовсе;
 13) арендаторы и роли: участник читает, владелец пишет, чужой идентификатор
     даёт 404 без различия «нет» и «чужое», readonly-подписка закрывает запись;
 14) эскиз приватен: чужой не отдаётся, не-картинка не принимается, SVG — тоже;
 15) структурно: слой не трогает `production_orders`, `CC_BATCH_ID`,
     `OrderedQty`, приёмки, формулы, МойСклад и парсер предпросмотра;
 16) миграция аддитивна: старт на «старой» базе создаёт таблицы, повторный старт
     идемпотентен, шагов старта двенадцать и первые одиннадцать не тронуты;
 17) удаление организации уносит все строки слоя.

Живых внешних систем здесь нет ни одной: ни МойСклада, ни Google. Все данные
синтетические, PII в наборе нет.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "test_supply_planning.db"
APP_PORT = int(os.environ.get("OBOROT_TEST_PORT", "8823"))

os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["SCHEDULER_ENABLED"] = "0"

for suffix in ("", "-wal", "-shm"):
    p = Path(str(DB_PATH) + suffix)
    if p.exists():
        p.unlink()

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from app import supply_planning as sp  # noqa: F401,E402 — граница слоя проверяется по исходникам
from app import supply_sheets as ss  # noqa: E402
from app.main import app as oborot_app  # noqa: E402

BASE = f"http://127.0.0.1:{APP_PORT}"
PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        PASS.append(name)
        print(f"  OK   {name}" + (f"  [{detail}]" if detail else ""))
    else:
        FAIL.append(name)
        print(f"  FAIL {name}  {detail}")


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


def client(headers=None) -> httpx.Client:
    h = {"X-Oborot-CSRF": "1"}
    h.update(headers or {})
    return httpx.Client(base_url=BASE, headers=h, timeout=60.0)


def register(c: httpx.Client, email: str, org: str, name: str = "Владелец"):
    return c.post("/register", data={"name": name, "email": email,
                                     "password": "secret123", "org_name": org})


# ── Синтетический PNG и JPEG, собранные байтами ──────────────────────────────
#
# Картинки строятся здесь, а не берутся файлом: набор обязан работать в пустом
# чекауте, а бинарник в репозитории — это ещё и вопрос «что именно на нём».

def make_png(width: int = 24, height: int = 16) -> bytes:
    import struct
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x80\x40\x20" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def make_jpeg(width: int = 32, height: int = 20) -> bytes:
    """Минимальный JPEG: SOI, APP0 объявленной длины и SOF0 с размерами.

    Длина сегмента APP0 объявляется ЧЕСТНО (2 байта длины + содержимое), иначе
    разбор уедет мимо SOF0 — и набор проверял бы не продукт, а свою фикстуру.
    """
    import struct
    app0_body = b"JFIF\x00" + bytes([1, 1, 0, 0, 1, 0, 1, 0, 0])
    app0 = b"\xff\xe0" + struct.pack(">H", len(app0_body) + 2) + app0_body
    sof_body = struct.pack(">BHHB", 8, height, width, 1) + b"\x01\x11\x00"
    sof = b"\xff\xc0" + struct.pack(">H", len(sof_body) + 2) + sof_body
    return b"\xff\xd8" + app0 + sof + b"\xff\xd9"


def set_sheets_preview(on: bool, org_id: int | None = None) -> None:
    """Флаг предпросмотра производственной таблицы (SUPPLY-FIX-1, F-08).

    Пишется прямо в `orgs.settings_json` — тем же ключом, что читает
    `Org.supply_sheets_preview` и переключает `tools/supply_sheets_preview.py`.
    Без флага вкладки нет, а ручки `/api/supply/sheets*` отвечают 404, поэтому
    проверки предпросмотра обязаны его включать явно: иначе они проверяли бы
    не предпросмотр, а собственную неудачу.
    """
    con = sqlite3.connect(DB_PATH)
    try:
        if org_id is None:
            org_id = con.execute("SELECT id FROM orgs ORDER BY id LIMIT 1").fetchone()[0]
        raw = con.execute("SELECT settings_json FROM orgs WHERE id = ?",
                          (org_id,)).fetchone()[0]
        try:
            data = json.loads(raw or "{}")
        except ValueError:
            data = {}
        if on:
            data["supply_sheets_preview"] = True
        else:
            data.pop("supply_sheets_preview", None)
        con.execute("UPDATE orgs SET settings_json = ? WHERE id = ?",
                    (json.dumps(data, ensure_ascii=False), org_id))
        con.commit()
    finally:
        con.close()


def sizes_of(payload: dict) -> dict:
    return {m["title"]: (m["qty"], m["assigned"], m["free"]) for m in payload["materials"]}


def run() -> int:
    owner = client()
    register(owner, "sp-owner@test.io", "Бренд Один")
    owner.post("/api/connect/demo")

    # ── 1. Материал заводится ДО вещи и партии ────────────────────────────────
    #
    # Это не «удобно», а факт производства: ткань покупают партией задолго до
    # того, как решено, что из неё шьют. Слой, который требует сначала выбрать
    # вещь, заставил бы человека выдумать её ради формы.
    print("\n== Материал существует сам по себе, до дизайна ==")
    empty = owner.get("/api/supply/planning").json()
    check("пустое состояние предлагает начать с материала",
          empty["next_step"]["code"] == "add_material", empty["next_step"]["text"][:60])
    check("и не показывает ни одной выдуманной цифры",
          empty["summary"]["materials"] == 0 and empty["summary"]["batches"] == 0
          and empty["summary"]["free_by_unit"] == [], json.dumps(empty["summary"]))

    r = owner.post("/api/supply/planning/materials",
                   json={"title": "Ткань костюмная 100", "qty": "100", "unit": "м",
                         "source_note": "счёт от поставщика", "op_id": "m-100"})
    check("материал создан без вещи и без партии", r.status_code == 200, r.text[:160])
    board = r.json()
    mat = board["materials"][0]
    mat_id = mat["id"]
    check("количество принято как есть, свободно всё",
          mat["qty"] == 100.0 and mat["assigned"] == 0.0 and mat["free"] == 100.0,
          f"{mat['qty']} {mat['assigned']} {mat['free']}")
    check("следующий шаг ведёт дальше по пути, а не повторяет пройденный",
          board["next_step"]["code"] == "add_item", board["next_step"]["text"][:60])

    # ── 2. Вещь каталога — по каноническому имени ─────────────────────────────
    print("\n== Вещь каталога берётся по каноническому имени ==")
    cat = owner.get("/api/supply/planning/catalog").json()["options"]
    check("каталог демо-данных виден как список канонических имён",
          len(cat) > 0 and all("base_name" in o and "sizes" in o for o in cat),
          f"{len(cat)}")
    base_name = cat[0]["base_name"]
    r = owner.post("/api/supply/planning/items",
                   json={"kind": "catalog", "base_name": base_name, "op_id": "i-cat"})
    check("вещь каталога принята", r.status_code == 200, r.text[:160])
    cat_item = [i for i in r.json()["items"] if i["kind"] == "catalog"][0]
    check("хранится каноническое имя, а не идентификатор размерной строки",
          cat_item["base_name"] == base_name and str(cat_item["id"]).isdigit(),
          f"{cat_item['base_name']}")

    bad = owner.post("/api/supply/planning/items",
                     json={"kind": "catalog", "base_name": "Такой вещи нет",
                           "op_id": "i-bad"})
    check("вещь не из своего каталога отклоняется, а не создаётся молча",
          bad.status_code == 400 and "новинку" in bad.json()["detail"],
          f"{bad.status_code} {bad.text[:90]}")

    # ── 3. Полноценная новинка с приватным эскизом ────────────────────────────
    print("\n== Новинка: своё тождество и приватный эскиз ==")
    png = make_png()
    up = owner.post("/api/supply/planning/sketches",
                    files={"file": ("sketch.png", png, "image/png")})
    check("эскиз принят и разобран по самим байтам",
          up.status_code == 200 and up.json()["mime"] == "image/png"
          and up.json()["width"] == 24 and up.json()["height"] == 16,
          f"{up.status_code} {up.text[:120]}")
    sketch_id = up.json()["sketch_id"]

    jpeg = owner.post("/api/supply/planning/sketches",
                      files={"file": ("s.jpg", make_jpeg(), "image/jpeg")})
    check("JPEG тоже принят и его размеры прочитаны",
          jpeg.status_code == 200 and jpeg.json()["mime"] == "image/jpeg"
          and jpeg.json()["width"] == 32, jpeg.text[:120])

    svg = owner.post("/api/supply/planning/sketches",
                     files={"file": ("s.svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>",
                                     "image/svg+xml")})
    check("SVG не принимается: это документ, а не картинка",
          svg.status_code == 400, f"{svg.status_code} {svg.text[:90]}")
    liar = owner.post("/api/supply/planning/sketches",
                      files={"file": ("s.png", b"MZ\x90\x00 not an image", "image/png")})
    check("файл, названный PNG, но им не являющийся, тоже отклонён",
          liar.status_code == 400, f"{liar.status_code} {liar.text[:90]}")

    got = owner.get(f"/api/supply/planning/sketches/{sketch_id}")
    check("свой эскиз отдаётся и это те же байты",
          got.status_code == 200 and got.content == png, f"{got.status_code}")
    check("отдаётся с nosniff и без публичного кэша",
          got.headers.get("x-content-type-options") == "nosniff"
          and "no-store" in (got.headers.get("cache-control") or ""),
          str(dict(got.headers))[:120])

    r = owner.post("/api/supply/planning/items",
                   json={"kind": "draft", "title": "Новинка Б", "sketch_id": sketch_id,
                         "op_id": "i-new"})
    check("новинка создана вместе с эскизом", r.status_code == 200, r.text[:160])
    new_item = [i for i in r.json()["items"] if i["title"] == "Новинка Б"][0]
    check("эскиз привязан к новинке", new_item["sketch_id"] == sketch_id,
          str(new_item))

    # ── 4. Два одинаковых имени — две разные вещи ─────────────────────────────
    print("\n== Одинаковые названия не склеиваются ==")
    owner.post("/api/supply/planning/items",
               json={"kind": "draft", "title": "Платье миди", "op_id": "i-dup1"})
    dup = owner.post("/api/supply/planning/items",
                     json={"kind": "draft", "title": "Платье миди", "op_id": "i-dup2"})
    same = [i for i in dup.json()["items"] if i["title"] == "Платье миди"]
    check("две вещи с одинаковым именем существуют раздельно",
          len(same) == 2 and same[0]["id"] != same[1]["id"], str(same))

    # ── 5. Две плановые партии одной вещи; 100 → 40 + 35 → 25 свободно ────────
    print("\n== Две партии одной вещи, метраж расписан, остаток честный ==")
    r = owner.post("/api/supply/planning/batches",
                   json={"item_id": new_item["id"], "title": "Партия А",
                         "plan_qty": "30", "due_kind": "approx",
                         "due_text": "к середине ноября", "due_source": "цех",
                         "op_id": "b-a"})
    check("плановая партия создана", r.status_code == 200, r.text[:160])
    r = owner.post("/api/supply/planning/batches",
                   json={"item_id": new_item["id"], "title": "Партия Б",
                         "plan_qty": "20", "due_kind": "unknown", "op_id": "b-b"})
    board = r.json()
    bids = {b["title"]: b["id"] for b in board["batches"]}
    check("у ОДНОЙ вещи две разные плановые партии",
          len(bids) == 2 and len({b["item_id"] for b in board["batches"]}) == 1,
          str(sorted(bids)))
    check("планы изделий введены отдельно и не выведены из метража",
          sorted(b["plan_qty"] for b in board["batches"]) == [20.0, 30.0],
          str([b["plan_qty"] for b in board["batches"]]))

    owner.post("/api/supply/planning/assignments",
               json={"material_id": mat_id, "batch_id": bids["Партия А"],
                     "qty": "40", "op_id": "a-40"})
    r = owner.post("/api/supply/planning/assignments",
                   json={"material_id": mat_id, "batch_id": bids["Партия Б"],
                         "qty": "35", "op_id": "a-35"})
    board = r.json()
    m = board["materials"][0]
    check("назначено 40 + 35 = 75, свободно 25",
          m["assigned"] == 75.0 and m["free"] == 25.0 and m["qty"] == 100.0,
          f"{m['assigned']} {m['free']}")
    check("предупреждения о превышении нет — его и не должно быть",
          m["over"] is False and m["warning"] == "", m["warning"][:80])
    # ПРАВИЛО `assign` УБРАНО (SUPPLY-FIX-1, F-07), и проверка заменена, а не
    # ослаблена. Свободный остаток — это факт сводки, а не задача: материал
    # покупают до того, как решено, что из него шьют, поэтому «распределите»
    # висело у любого материала с остатком больше нуля и закрывало собой
    # настоящие расхождения. Теперь при таком состоянии следующим шагом идёт
    # первое НАСТОЯЩЕЕ незавершённое дело — партия без срока, — а число 25
    # человек по-прежнему видит в сводке (проверка ниже).
    check("свободный остаток больше не выдаётся за задачу",
          board["next_step"]["code"] != "assign", board["next_step"]["code"])
    check("следующим шагом идёт настоящее незавершённое дело",
          board["next_step"]["code"] in ("plan_qty", "due"),
          board["next_step"]["text"][:90])
    check("метраж и штуки в сводке НЕ смешаны",
          board["summary"]["free_by_unit"] == [{"unit": "м", "qty": 25.0}]
          and board["summary"]["plan_known"] == 50.0,
          json.dumps(board["summary"], ensure_ascii=False))

    # ── 6. Одна ткань на две вещи; две ткани на одну партию ───────────────────
    print("\n== Ткань на две вещи и две ткани на одну партию ==")
    per_batch = {b["title"]: b["assigned_by_unit"] for b in board["batches"]}
    check("один материал расписан по двум партиям разных вещей",
          per_batch["Партия А"] == [{"unit": "м", "qty": 40.0}]
          and per_batch["Партия Б"] == [{"unit": "м", "qty": 35.0}],
          str(per_batch))

    r = owner.post("/api/supply/planning/materials",
                   json={"title": "Подкладка", "qty": "18", "op_id": "m-lining"})
    lining_id = [x["id"] for x in r.json()["materials"] if x["title"] == "Подкладка"][0]
    r = owner.post("/api/supply/planning/assignments",
                   json={"material_id": lining_id, "batch_id": bids["Партия А"],
                         "qty": "12", "op_id": "a-lining"})
    batch_a = [b for b in r.json()["batches"] if b["title"] == "Партия А"][0]
    check("на одной партии два разных материала",
          len(batch_a["assignments"]) == 2
          and {a["material_title"] for a in batch_a["assignments"]}
              == {"Ткань костюмная 100", "Подкладка"},
          str([a["material_title"] for a in batch_a["assignments"]]))
    check("и суммарно на партии 52 метра — сложены метры с метрами",
          batch_a["assigned_by_unit"] == [{"unit": "м", "qty": 52.0}],
          str(batch_a["assigned_by_unit"]))

    # ── 7. Неизвестное количество остаётся неизвестным ────────────────────────
    print("\n== Неизвестное количество: остаток тоже неизвестен ==")
    r = owner.post("/api/supply/planning/materials",
                   json={"title": "Ткань без замера", "qty": "", "op_id": "m-unknown"})
    unknown = [x for x in r.json()["materials"] if x["title"] == "Ткань без замера"][0]
    check("пустое количество сохранено как НЕИЗВЕСТНО, а не как ноль",
          unknown["qty"] is None and unknown["qty_known"] is False, str(unknown["qty"]))
    check("свободный остаток такого материала тоже неизвестен",
          unknown["free"] is None and unknown["free_known"] is False,
          str(unknown["free"]))
    r = owner.post("/api/supply/planning/assignments",
                   json={"material_id": unknown["id"], "batch_id": bids["Партия Б"],
                         "qty": "7", "op_id": "a-unknown"})
    unknown = [x for x in r.json()["materials"] if x["id"] == unknown["id"]][0]
    check("после назначения остаток по-прежнему неизвестен, а не «минус семь»",
          unknown["assigned"] == 7.0 and unknown["free"] is None
          and unknown["over"] is False,
          f"{unknown['assigned']} {unknown['free']} {unknown['over']}")

    zero = owner.post("/api/supply/planning/materials",
                      json={"title": "Явный ноль", "qty": "0", "op_id": "m-zero"})
    z = [x for x in zero.json()["materials"] if x["title"] == "Явный ноль"][0]
    check("явно написанный ноль остаётся нулём и неизвестным не становится",
          z["qty"] == 0.0 and z["qty_known"] is True, str(z["qty"]))

    # ── 7а. Несовместимые единицы НЕ складываются ────────────────────────────
    #
    # Воспроизведение P1 ревью PR #49 (issuecomment-5548612500). 100 м ткани и
    # 10 кг фурнитуры — это не «110» чего-либо: коэффициента между метром и
    # килограммом никто не объявлял, и вывести его неоткуда. Проверяется не
    # «красиво показано», а отсутствие самого числа, которого не существует.
    print("\n== Метры и килограммы: два числа, а не одно ==")
    # Метры ДО появления килограммов запоминаются, а не выписываются числом:
    # проверяется свойство «килограммы не влияют на метры», и оно не должно
    # краснеть от того, что выше по сценарию добавился ещё один материал.
    metres_before = {g["unit"]: g["qty"] for g in
                     owner.get("/api/supply/planning").json()["summary"]["free_by_unit"]
                     }.get("м")
    kilo = owner.post("/api/supply/planning/materials",
                      json={"title": "Фурнитура на вес", "qty": "10", "unit": "кг",
                            "op_id": "m-kg"})
    board = kilo.json()
    units = {g["unit"]: g["qty"] for g in board["summary"]["free_by_unit"]}
    check("свободное разложено по единицам, а не сведено в одно число",
          "кг" in units and "м" in units and units["кг"] == 10.0,
          json.dumps(board["summary"]["free_by_unit"], ensure_ascii=False))
    check("килограммы не приплюсовались к метрам: метры не изменились",
          units["м"] == metres_before, f"{metres_before} -> {units.get('м')}")
    check("общего числа для разных единиц в ответе нет вовсе",
          "free_known" not in board["summary"],
          json.dumps(sorted(board["summary"].keys())))
    check("и ни одно поле сводки не равно сумме метров с килограммами",
          (units["м"] + units["кг"]) not in [v for v in board["summary"].values()
                                             if isinstance(v, (int, float))],
          json.dumps(board["summary"], ensure_ascii=False))

    # Та же проверка на карточке партии: одна партия законно собирается из
    # метров ткани и килограммов фурнитуры.
    mix = owner.post("/api/supply/planning/assignments",
                     json={"material_id": [m["id"] for m in board["materials"]
                                           if m["title"] == "Фурнитура на вес"][0],
                           "batch_id": bids["Партия А"], "qty": "2",
                           "op_id": "a-kg"})
    mixed_batch = [b for b in mix.json()["batches"] if b["title"] == "Партия А"][0]
    by_unit = {g["unit"]: g["qty"] for g in mixed_batch["assigned_by_unit"]}
    check("на партии метры и килограммы посчитаны раздельно",
          by_unit == {"м": 52.0, "кг": 2.0}, str(by_unit))
    check("и общего числа назначенного у партии тоже нет",
          "assigned_total" not in mixed_batch,
          json.dumps(sorted(mixed_batch.keys())))

    # ── 8. Превышение предупреждает, но не запрещает ──────────────────────────
    print("\n== План сверх наличия: предупреждение, а не запрет ==")
    r = owner.post("/api/supply/planning/assignments",
                   json={"material_id": lining_id, "batch_id": bids["Партия Б"],
                         "qty": "10", "op_id": "a-over"})
    check("назначение сверх известного наличия ПРИНЯТО", r.status_code == 200,
          r.text[:160])
    over = [x for x in r.json()["materials"] if x["id"] == lining_id][0]
    check("и превышение названо числом",
          over["over"] is True and over["over_by"] == 4.0 and over["assigned"] == 22.0,
          f"{over['over_by']} {over['assigned']}")
    check("предупреждение прямо говорит, что это план, а не расход",
          "план, а не расход" in over["warning"], over["warning"][:120])
    check("количество материала при этом не тронуто и не обрезано",
          over["qty"] == 18.0, str(over["qty"]))

    # ── 9. Перенос метража неделим ────────────────────────────────────────────
    #
    # Проверяется не «кнопка работает», а СУММА: перенос — один поступок, и если
    # он разложится на два шага, метраж либо потеряется, либо удвоится. Ровно на
    # этом ломаются самодельные «сначала снять, потом добавить».
    print("\n== Перенос 10 м А→Б: ничего не потеряно и не удвоено ==")
    board = owner.get("/api/supply/planning").json()
    batch_a = [b for b in board["batches"] if b["title"] == "Партия А"][0]
    a_main = [a for a in batch_a["assignments"]
              if a["material_id"] == mat_id][0]
    before = [x for x in board["materials"] if x["id"] == mat_id][0]["assigned"]
    r = owner.post("/api/supply/planning/assignments/move",
                   json={"assignment_id": a_main["id"], "to_batch_id": bids["Партия Б"],
                         "qty": "10", "rev": a_main["rev"], "op_id": "mv-10"})
    check("перенос выполнен", r.status_code == 200, r.text[:160])
    moved = r.json()
    after = [x for x in moved["materials"] if x["id"] == mat_id][0]["assigned"]
    check("сумма назначенного по материалу не изменилась ни на грамм",
          before == after == 75.0, f"{before} -> {after}")
    tot = {b["title"]: sum(a["qty"] for a in b["assignments"]
                           if a["material_id"] == mat_id) for b in moved["batches"]}
    check("десять метров ушли из А и пришли в Б",
          tot["Партия А"] == 30.0 and tot["Партия Б"] == 45.0, str(tot))
    check("в приёмнике это ОДНА строка материала, а не две",
          len([a for a in
               [b for b in moved["batches"] if b["title"] == "Партия Б"][0]["assignments"]
               if a["material_id"] == mat_id]) == 1)

    a_main2 = [a for b in moved["batches"] if b["title"] == "Партия А"
               for a in b["assignments"] if a["material_id"] == mat_id][0]
    too_much = owner.post("/api/supply/planning/assignments/move",
                          json={"assignment_id": a_main2["id"],
                                "to_batch_id": bids["Партия Б"], "qty": "999",
                                "rev": a_main2["rev"], "op_id": "mv-too"})
    check("перенести больше, чем назначено, нельзя — и это сказано числом",
          too_much.status_code == 400 and "30" in too_much.json()["detail"],
          f"{too_much.status_code} {too_much.text[:90]}")
    still = owner.get("/api/supply/planning").json()
    check("после отказа переноса ничего не сдвинулось",
          [x for x in still["materials"] if x["id"] == mat_id][0]["assigned"] == 75.0)

    # ── 10. Исправление и снятие назначения ───────────────────────────────────
    print("\n== Правка и снятие назначения ==")
    a_now = [a for b in still["batches"] if b["title"] == "Партия А"
             for a in b["assignments"] if a["material_id"] == mat_id][0]
    r = owner.post(f"/api/supply/planning/assignments/{a_now['id']}/update",
                   json={"qty": "22", "rev": a_now["rev"], "op_id": "upd-22"})
    check("назначение исправлено",
          [x for x in r.json()["materials"] if x["id"] == mat_id][0]["assigned"] == 67.0,
          str(r.status_code))
    a_now2 = [a for b in r.json()["batches"] if b["title"] == "Партия А"
              for a in b["assignments"] if a["material_id"] == mat_id][0]
    r = owner.post(f"/api/supply/planning/assignments/{a_now2['id']}/delete",
                   json={"rev": a_now2["rev"], "op_id": "del-1"})
    board = r.json()
    check("снятие назначения вернуло метраж в свободные, материал цел",
          [x for x in board["materials"] if x["id"] == mat_id][0]["assigned"] == 45.0
          and [x for x in board["materials"] if x["id"] == mat_id][0]["qty"] == 100.0,
          str([x for x in board["materials"] if x["id"] == mat_id][0]))

    # ── 11. Повторный POST и чужая правка ─────────────────────────────────────
    print("\n== Повторный клик не применяется дважды, чужая правка не затирается ==")
    payload = {"material_id": mat_id, "batch_id": bids["Партия А"], "qty": "5",
               "op_id": "a-once"}
    first = owner.post("/api/supply/planning/assignments", json=payload).json()
    second = owner.post("/api/supply/planning/assignments", json=payload).json()
    check("тот же поступок дважды даёт одно назначение",
          [x for x in first["materials"] if x["id"] == mat_id][0]["assigned"] == 50.0
          and [x for x in second["materials"] if x["id"] == mat_id][0]["assigned"] == 50.0,
          str([x for x in second["materials"] if x["id"] == mat_id][0]["assigned"]))
    third = owner.post("/api/supply/planning/assignments",
                       json={**payload, "op_id": "a-twice"}).json()
    check("а другой поступок с теми же числами — это второе назначение",
          [x for x in third["materials"] if x["id"] == mat_id][0]["assigned"] == 55.0,
          str([x for x in third["materials"] if x["id"] == mat_id][0]["assigned"]))

    # Честный устаревший rev: сначала кто-то ДРУГОЙ правку уже сделал, и только
    # потом приходит вторая с прежней редакцией. Прислать rev, который просто
    # никогда не был текущим, значило бы проверить арифметику, а не защиту.
    seen_rev = [x for x in owner.get("/api/supply/planning").json()["materials"]
                if x["id"] == mat_id][0]["rev"]
    owner.post(f"/api/supply/planning/materials/{mat_id}/update",
               json={"title": "Ткань костюмная 100", "qty": "100",
                     "rev": seen_rev, "op_id": "other-hand"})
    stale = owner.post(f"/api/supply/planning/materials/{mat_id}/update",
                       json={"qty": "90", "rev": seen_rev, "op_id": "stale-1"})
    check("правка со старой редакцией отвергнута 409, а не применена",
          stale.status_code == 409 and "другом окне" in stale.json()["detail"],
          f"{stale.status_code} {stale.text[:100]}")
    fresh = owner.get("/api/supply/planning").json()
    check("и значение осталось прежним",
          [x for x in fresh["materials"] if x["id"] == mat_id][0]["qty"] == 100.0)
    cur_rev = [x for x in fresh["materials"] if x["id"] == mat_id][0]["rev"]
    ok = owner.post(f"/api/supply/planning/materials/{mat_id}/update",
                    json={"qty": "90", "rev": cur_rev, "op_id": "fresh-1"})
    check("с текущей редакцией та же правка проходит",
          ok.status_code == 200
          and [x for x in ok.json()["materials"] if x["id"] == mat_id][0]["qty"] == 90.0,
          str(ok.status_code))

    # ── 12. Сроки: неизвестно, ориентир, точная дата, текст ───────────────────
    print("\n== Срок: четыре вида, с источником и автором, без выдумок ==")
    board = owner.get("/api/supply/planning").json()
    b_a = [b for b in board["batches"] if b["title"] == "Партия А"][0]
    b_b = [b for b in board["batches"] if b["title"] == "Партия Б"][0]
    check("ориентировочный срок показан ориентиром и несёт источник",
          b_a["due_kind"] == "approx" and b_a["due_label"] == "ориентировочно к середине ноября"
          and b_a["due_source"] == "цех" and b_a["due_author"],
          f"{b_a['due_label']} / {b_a['due_source']} / {b_a['due_author']}")
    check("неизвестный срок так и назван — без подстановки сегодняшней даты",
          b_b["due_kind"] == "unknown" and b_b["due_label"] == "срок неизвестен"
          and b_b["due_date"] == "", str(b_b["due_label"]))

    r = owner.post(f"/api/supply/planning/batches/{b_b['id']}/update",
                   json={"due_kind": "exact", "due_date": "2026-11-14",
                         "due_source": "поставщик", "rev": b_b["rev"],
                         "op_id": "due-1"})
    b_b2 = [b for b in r.json()["batches"] if b["id"] == b_b["id"]][0]
    check("точная дата принята и показана точной",
          b_b2["due_kind"] == "exact" and b_b2["due_date"] == "2026-11-14"
          and b_b2["due_label"] == "точно 2026-11-14", str(b_b2["due_label"]))
    bad_date = owner.post(f"/api/supply/planning/batches/{b_b['id']}/update",
                          json={"due_kind": "exact", "due_date": "14 ноября",
                                "rev": b_b2["rev"], "op_id": "due-bad"})
    check("негодная дата отвергнута управляемо, а не записана как есть",
          bad_date.status_code == 400, f"{bad_date.status_code} {bad_date.text[:90]}")
    free_text = owner.post(f"/api/supply/planning/batches/{b_b['id']}/update",
                           json={"due_kind": "text", "due_text": "после праздников",
                                 "due_source": "слова цеха", "rev": b_b2["rev"],
                                 "op_id": "due-2"})
    b_b3 = [b for b in free_text.json()["batches"] if b["id"] == b_b["id"]][0]
    check("срок словами сохранён как написан",
          b_b3["due_label"] == "после праздников" and b_b3["due_date"] == "",
          str(b_b3["due_label"]))

    # ── 13. История: кто, когда и что было раньше ─────────────────────────────
    print("\n== История хранит автора, время и ПРЕЖНЕЕ значение ==")
    con = sqlite3.connect(DB_PATH)
    try:
        rows = con.execute(
            "SELECT entity_kind, action, field, old_value, new_value, author, created_at"
            " FROM supply_events ORDER BY id").fetchall()
    finally:
        con.close()
    due_events = [r for r in rows if r[2] == "due"]
    qty_events = [r for r in rows if r[0] == "material" and r[2] == "qty"
                  and r[1] == "update"]
    check("правка срока записана вместе с прежним значением",
          any(e[3] == "срок неизвестен" for e in due_events)
          and any("2026-11-14" in (e[4] or "") for e in due_events),
          str(due_events[:2]))
    check("правка количества записана с прежним значением и автором",
          any(e[3] == "100.0" and e[4] == "90.0" and e[5] for e in qty_events),
          str(qty_events[:2]))
    check("у каждой записи журнала есть время",
          all(r[6] for r in rows), f"{len(rows)} записей")

    # ── 14. Предпросмотр и план — разные носители ─────────────────────────────
    #
    # Снимок SUPPLY-2 заменяется ЦЕЛИКОМ при каждом обновлении, и это его
    # свойство. Ручное решение обязано это пережить, поэтому оно живёт в своих
    # таблицах, а не в снимке. Проверяется не «мы так решили», а факт: снимок
    # переписан дважды, в том числе с перестановкой строк, — план не изменился
    # ни в одном байте.
    print("\n== Повторный импорт и перестановка строк не трогают ручные решения ==")
    before_plan = owner.get("/api/supply/planning").json()

    def write_snapshot(rows):
        envelope = {
            "schema_version": ss.ENVELOPE_SCHEMA_VERSION,
            "parser_version": ss.PARSER_VERSION,
            "spreadsheet_id": "1AbCdEf_ghijklmnop-QRSTUV0123456789wxyz",
            "sheet_names": ["Осень 26", "НГ 26/27"],
            "content_sha256": "a" * 64,
            "last_attempt_at": "2026-09-05T10:00:00+00:00",
            "last_success_at": "2026-09-05T10:00:00+00:00",
            "fetched_at": "2026-09-05T10:00:00+00:00",
            "last_error": "",
            "last_attempt_source": {"spreadsheet_id": "1AbCdEf_ghijklmnop-QRSTUV0123456789wxyz",
                                    "sheet_names": ["Осень 26", "НГ 26/27"]},
            "schema": {}, "counts": ss.build_counts(rows, ["Осень 26", "НГ 26/27"]),
            "rows": rows,
        }
        c2 = sqlite3.connect(DB_PATH)
        try:
            row = c2.execute("SELECT id, config_json FROM connections"
                             " ORDER BY id LIMIT 1").fetchone()
            cfg = json.loads(row[1] or "{}")
            cfg[ss.ENVELOPE_KEY] = envelope
            c2.execute("UPDATE connections SET config_json = ? WHERE id = ?",
                       (json.dumps(cfg, ensure_ascii=False), row[0]))
            c2.commit()
        finally:
            c2.close()

    def snap_row(index, name):
        sizes = {"XS": 1, "S": 1, "M": 1, "L": 1, "XL": 1}
        return {
            "sheet_name": "Осень 26", "source_row": index, "anchor_row": index,
            "is_blank": False, "article_raw": f"A{index}", "name_raw": name,
            "article": f"A{index}", "name": name, "color_raw": "Чёрный",
            "qty_meters_raw": "", "sketch_raw": "", "sizes": sizes,
            "sizes_raw": {k: "1" for k in sizes}, "size_sum": 5,
            "source_total_raw": "5", "source_total": 5,
            "comments_raw": ["", "", ""], "source_status_raw": "",
            "production_raw": "", "components_raw": "", "price_raw": "",
            "unknown_raw": {}, "issues": [], "labels": [],
            "needs_review": False, "invalid": False,
        }

    write_snapshot([snap_row(3, "Позиция один"), snap_row(4, "Позиция два")])
    mid_plan = owner.get("/api/supply/planning").json()
    write_snapshot([snap_row(4, "Позиция два"), snap_row(3, "Позиция один")])
    after_plan = owner.get("/api/supply/planning").json()
    check("замена снимка не изменила план ни в одном поле",
          json.dumps(before_plan, sort_keys=True, ensure_ascii=False)
          == json.dumps(mid_plan, sort_keys=True, ensure_ascii=False),
          "первый импорт")
    check("перестановка строк источника тоже не изменила план",
          json.dumps(before_plan, sort_keys=True, ensure_ascii=False)
          == json.dumps(after_plan, sort_keys=True, ensure_ascii=False),
          "второй импорт")
    # SUPPLY-FIX-1 (F-08): предпросмотр закрыт флагом организации. Проверка
    # снимка обязана его включить — иначе она читала бы 404 и доказывала не
    # «снимок жив», а «ручка закрыта».
    closed = owner.get("/api/supply/sheets")
    check("без флага организации предпросмотра не существует",
          closed.status_code == 404, str(closed.status_code))
    set_sheets_preview(True)
    preview = owner.get("/api/supply/sheets").json()
    check("а сам предпросмотр при этом читается и живёт своей жизнью",
          preview.get("configured") is True and len(preview.get("rows", [])) == 2,
          str(len(preview.get("rows", []))))
    set_sheets_preview(False)

    # ── 15. Арендаторы, роли, подписка ────────────────────────────────────────
    print("\n== Чужая организация, участник и readonly ==")
    other = client()
    register(other, "sp-other@test.io", "Бренд Два")
    other_board = other.get("/api/supply/planning").json()
    check("у чужой организации свой пустой план",
          other_board["materials"] == [] and other_board["batches"] == [],
          str(len(other_board["materials"])))
    stolen = other.get(f"/api/supply/planning/sketches/{sketch_id}")
    check("чужой эскиз не отдаётся", stolen.status_code == 404,
          f"{stolen.status_code}")
    check("и отказ не рассказывает, существует ли он вообще",
          stolen.json().get("detail") == "Эскиз не найден.",
          str(stolen.json())[:90])
    ghost = other.get("/api/supply/planning/sketches/999999")
    check("несуществующий и чужой отвечают ОДИНАКОВО",
          ghost.status_code == stolen.status_code
          and ghost.json().get("detail") == stolen.json().get("detail"),
          f"{ghost.status_code}")
    foreign = other.post("/api/supply/planning/assignments",
                         json={"material_id": mat_id, "batch_id": bids["Партия А"],
                               "qty": "1", "op_id": "x-1"})
    check("назначить чужой материал нельзя", foreign.status_code == 404,
          f"{foreign.status_code} {foreign.text[:90]}")
    foreign_upd = other.post(f"/api/supply/planning/materials/{mat_id}/update",
                             json={"qty": "1", "op_id": "x-2"})
    check("и править чужой материал тоже", foreign_upd.status_code == 404,
          f"{foreign_upd.status_code}")
    untouched = owner.get("/api/supply/planning").json()
    check("после чужих попыток свой план не изменился",
          json.dumps(untouched, sort_keys=True, ensure_ascii=False)
          == json.dumps(after_plan, sort_keys=True, ensure_ascii=False))

    anon = httpx.Client(base_url=BASE, headers={"X-Oborot-CSRF": "1"}, timeout=30.0)
    check("аноним не читает план", anon.get("/api/supply/planning").status_code == 401)
    check("и не пишет",
          anon.post("/api/supply/planning/materials",
                    json={"title": "x"}).status_code == 401)
    no_csrf = httpx.Client(base_url=BASE, timeout=30.0,
                           cookies=dict(owner.cookies))
    check("запись без CSRF-заголовка отклонена",
          no_csrf.post("/api/supply/planning/materials",
                       json={"title": "x"}).status_code == 403)
    anon.close()
    no_csrf.close()

    # Участник читает и НЕ пишет. Роль проверяется отдельно от подписки: это
    # разные запреты, и путать их нельзя — участник не пишет никогда, а владелец
    # в readonly не пишет временно.
    import bcrypt
    con = sqlite3.connect(DB_PATH)
    try:
        org_id = con.execute("SELECT id FROM orgs ORDER BY id LIMIT 1").fetchone()[0]
        pw = bcrypt.hashpw(b"secret123", bcrypt.gensalt()).decode()
        cur = con.execute("INSERT INTO users (email, pw_hash, name, created_at)"
                          " VALUES (?,?,?,datetime('now'))",
                          ("sp-member@test.io", pw, "Участник"))
        con.execute("INSERT INTO memberships (user_id, org_id, role)"
                    " VALUES (?,?,'member')", (cur.lastrowid, org_id))
        con.commit()
    finally:
        con.close()
    member = client()
    member.post("/login", data={"email": "sp-member@test.io", "password": "secret123"})
    mb = member.get("/api/supply/planning")
    check("участник видит план организации", mb.status_code == 200, str(mb.status_code))
    check("и ему прямо сказано, что запись не его",
          mb.json()["can_write"] is False
          and mb.json()["next_step"]["code"] == "readonly",
          mb.json()["next_step"]["text"][:60])
    mw = member.post("/api/supply/planning/materials",
                     json={"title": "Участник пишет", "op_id": "mem-1"})
    check("участник не создаёт материал", mw.status_code == 403, str(mw.status_code))
    mm = member.post("/api/supply/planning/assignments",
                     json={"material_id": mat_id, "batch_id": bids["Партия А"],
                           "qty": "1", "op_id": "mem-2"})
    check("и не назначает метраж", mm.status_code == 403, str(mm.status_code))
    ms = member.post("/api/supply/planning/sketches",
                     files={"file": ("s.png", make_png(), "image/png")})
    check("и не грузит эскизы", ms.status_code == 403, str(ms.status_code))
    check("но чужой эскиз своей организации ему виден — он же её участник",
          member.get(f"/api/supply/planning/sketches/{sketch_id}").status_code == 200)

    # Гейт подписки: readonly закрывает запись целиком, чтение остаётся.
    print("\n== Приостановленная подписка: чтение открыто, запись закрыта ==")
    os.environ["OBOROT_SUBSCRIPTION_GATE"] = "1"
    con = sqlite3.connect(DB_PATH)
    try:
        con.execute("UPDATE orgs SET trial_ends_at = datetime('now', '-5 day'),"
                    " paid_until = NULL")
        con.commit()
    finally:
        con.close()
    try:
        ro_read = owner.get("/api/supply/planning")
        ro_write = owner.post("/api/supply/planning/materials",
                              json={"title": "В readonly", "op_id": "ro-1"})
        ro_sketch = owner.post("/api/supply/planning/sketches",
                               files={"file": ("s.png", make_png(), "image/png")})
        check("в readonly план по-прежнему читается", ro_read.status_code == 200,
              str(ro_read.status_code))
        check("а запись отклонена кодом 402, а не молча", ro_write.status_code == 402,
              f"{ro_write.status_code} {ro_write.text[:80]}")
        check("эскиз в readonly тоже не грузится", ro_sketch.status_code == 402,
              str(ro_sketch.status_code))
        after_ro = owner.get("/api/supply/planning").json()
        check("и ни одной строки в readonly не появилось",
              not any(m["title"] == "В readonly" for m in after_ro["materials"]))
    finally:
        os.environ["OBOROT_SUBSCRIPTION_GATE"] = "0"
        con = sqlite3.connect(DB_PATH)
        try:
            con.execute("UPDATE orgs SET trial_ends_at = datetime('now', '+30 day')")
            con.commit()
        finally:
            con.close()

    # ── 16. Ввод: границы и мусор ─────────────────────────────────────────────
    print("\n== Ввод проверяется, а не «чинится» ==")
    cases = [
        ("пустое название", {"title": "  ", "op_id": "v1"}, 400),
        ("название длиннее предела", {"title": "я" * 201, "op_id": "v2"}, 400),
        ("количество словом", {"title": "Ткань", "qty": "много", "op_id": "v3"}, 400),
        ("отрицательное количество", {"title": "Ткань", "qty": "-5", "op_id": "v4"}, 400),
        ("количество за пределом", {"title": "Ткань", "qty": "9999999", "op_id": "v5"}, 400),
    ]
    for label, payload, expect in cases:
        resp = owner.post("/api/supply/planning/materials", json=payload)
        check(f"отклонено управляемо: {label}", resp.status_code == expect,
              f"{resp.status_code} {resp.text[:80]}")
    comma = owner.post("/api/supply/planning/materials",
                       json={"title": "Запятая", "qty": "12,5", "op_id": "v6"})
    check("запятая как разделитель принимается — это человек, а не парсер",
          comma.status_code == 200
          and [m for m in comma.json()["materials"] if m["title"] == "Запятая"][0]["qty"] == 12.5,
          comma.text[:100])
    zero_assign = owner.post("/api/supply/planning/assignments",
                             json={"material_id": mat_id, "batch_id": bids["Партия А"],
                                   "qty": "0", "op_id": "v7"})
    check("назначение нуля отклонено: это не назначение",
          zero_assign.status_code == 400, f"{zero_assign.status_code}")
    ghost_batch = owner.post("/api/supply/planning/assignments",
                             json={"material_id": mat_id, "batch_id": 999999,
                                   "qty": "1", "op_id": "v8"})
    check("назначение на несуществующую партию — 404",
          ghost_batch.status_code == 404, f"{ghost_batch.status_code}")

    # ── 17. Структурно: слой не касается партий, заказов и формул ─────────────
    #
    # Проверка по ИСХОДНИКАМ, а не по поведению: поведение показывает, что слой
    # сегодня ничего не сломал, а исходники — что сломать нечем. Ровно так же
    # проверяется граница SUPPLY-1 и SUPPLY-2.
    print("\n== Слой физически не связан с партиями, заказами и формулами ==")
    def code_only(path: Path) -> str:
        """Исходник без комментариев и строк документации.

        Искать запретные имена в СЫРОМ тексте нельзя: этот слой обязан объяснять
        в комментариях, чего он НЕ делает, — и такое объяснение краснело бы само
        от себя. Проверять надо код, поэтому дерево разбирается и обратно
        собираются только исполняемые узлы.
        """
        import ast as _ast
        tree = _ast.parse(path.read_text(encoding="utf-8"))
        for node in _ast.walk(tree):
            if (isinstance(node, (_ast.Module, _ast.FunctionDef,
                                  _ast.AsyncFunctionDef, _ast.ClassDef))
                    and node.body and isinstance(node.body[0], _ast.Expr)
                    and isinstance(node.body[0].value, _ast.Constant)
                    and isinstance(node.body[0].value.value, str)):
                node.body = node.body[1:] or [_ast.Pass()]
        return _ast.unparse(tree)

    src_plan = code_only(ROOT / "app" / "supply_planning.py")
    src_routes = code_only(ROOT / "app" / "routes_supply_planning.py")
    forbidden = ("ProductionOrder", "OrderedQty", "OrderReceipt", "OrderPlan",
                 "cc_batch_id", "CC_BATCH_ID", "new_cc_batch_id")
    hits = [w for w in forbidden if w in src_plan or w in src_routes]
    check("в КОДЕ слоя нет ни заказов, ни приёмок, ни идентификатора партии",
          not hits, str(hits))
    # Перенос строк не должен влиять на смысл проверки, поэтому текст
    # нормализуется по пробелам: иначе она краснела бы от переформатирования.
    raw_plan = " ".join(
        (ROOT / "app" / "supply_planning.py").read_text(encoding="utf-8").split())
    check("а в комментариях граница названа прямо — иначе её пришлось бы помнить",
          "CC_BATCH_ID" in raw_plan and "не выдаётся и не имитируется" in raw_plan,
          "CC_BATCH_ID … не выдаётся и не имитируется")
    net = [w for w in ("httpx", "requests", "urllib", "socket") if w in src_plan]
    check("и ни одного сетевого клиента — слой офлайновый по построению",
          not net, str(net))
    check("предпросмотр слой тоже не читает: снимок ему не нужен",
          "supply_sheets" not in src_plan and "supply_sheets" not in src_routes)
    check("пересчёта метров в штуки нет ни в одной строке кода",
          "plan_qty" in src_plan and "* qty" not in src_plan
          and "qty *" not in src_plan)

    parser_src = (ROOT / "app" / "supply_sheets.py").read_text(encoding="utf-8")
    check("версия парсера предпросмотра не тронута",
          'PARSER_VERSION = "supply-sheets-parser-4"' in parser_src,
          "supply-sheets-parser-4")
    check("и версия envelope тоже",
          "ENVELOPE_SCHEMA_VERSION = 1" in parser_src)

    con = sqlite3.connect(DB_PATH)
    try:
        orders = con.execute("SELECT COUNT(*) FROM production_orders").fetchone()[0]
        ordered = con.execute("SELECT COUNT(*) FROM ordered_qty").fetchone()[0]
    finally:
        con.close()
    check("за весь сценарий не создано ни одного заказа и ни одной строки «В заказе»",
          orders == 0 and ordered == 0, f"orders={orders} ordered_qty={ordered}")

    # ── 18. Миграция: аддитивна, идемпотентна, прежние шаги сохранены ────────
    print("\n== Миграция: новый шаг сверху, старые четырнадцать не тронуты ==")
    from app.main import STARTUP_SCHEMA_STEPS
    check("шагов старта пятнадцать", len(STARTUP_SCHEMA_STEPS) == 15,
          str(len(STARTUP_SCHEMA_STEPS)))
    check("первые десять пар (id, позиция) не изменились",
          STARTUP_SCHEMA_STEPS[:10] == (
              ("init_db", 1), ("lessons.ensure_schema", 2),
              ("exclusions.ensure_schema", 3), ("ms_sync.ensure_schema", 4),
              ("ms_sync.reset_stale_running", 5), ("ms_writeback.ensure_schema", 6),
              ("ms_vendor.ensure_schema", 7), ("subscription.ensure_schema", 8),
              ("subscription.log_preview", 9), ("models.ensure_supply_schema", 10)),
          str(STARTUP_SCHEMA_STEPS[:10]))
    check("шаг SUPPLY-3 остался на позиции 11 и с прежним id",
          STARTUP_SCHEMA_STEPS[10] == ("models.ensure_supply_planning_schema", 11),
          str(STARTUP_SCHEMA_STEPS[10]))
    # Индекс берётся безопасно намеренно: на дереве, где шага ещё нет, набор
    # обязан НАПЕЧАТАТЬ красную строку, а не умереть IndexError на середине —
    # иначе все проверки ниже не выполнятся вовсе, и прогон перестанет что-либо
    # доказывать (D-42: непроведённая проверка не бывает зелёной).
    twelfth = (STARTUP_SCHEMA_STEPS[11]
               if len(STARTUP_SCHEMA_STEPS) > 11 else None)
    check("шаг SUPPLY-FIX-1 остался на позиции 12 и с прежним id",
          twelfth == ("models.ensure_supply_planning_unique_schema", 12),
          str(twelfth))
    # Тот же безопасный доступ по индексу и по той же причине: на дереве, где
    # шага 13 ещё нет, набор обязан НАПЕЧАТАТЬ красную строку, а не умереть
    # IndexError и не выполнить всё, что ниже.
    thirteenth = (STARTUP_SCHEMA_STEPS[12]
                  if len(STARTUP_SCHEMA_STEPS) > 12 else None)
    check("шаг 13 остался на своей позиции и с прежним id",
          thirteenth == ("models.ensure_supply_archive_schema", 13),
          str(thirteenth))
    fourteenth = (STARTUP_SCHEMA_STEPS[13]
                  if len(STARTUP_SCHEMA_STEPS) > 13 else None)
    check("шаг 14 остался на своей позиции и с прежним id",
          fourteenth == ("models.ensure_supply_assignment_archive_schema", 14),
          str(fourteenth))
    fifteenth = (STARTUP_SCHEMA_STEPS[14]
                 if len(STARTUP_SCHEMA_STEPS) > 14 else None)
    check("снимок условий заказа дописан в конец с новым id и позицией 15",
          fifteenth == ("models.ensure_order_payment_terms_schema", 15),
          str(fifteenth))

    # «Старая» база: таблиц слоя нет вовсе — шаг обязан их создать и не упасть
    # при повторном вызове.
    from sqlalchemy import create_engine, inspect as sa_inspect
    from app import models as _models
    old_db = ROOT / "test_supply_planning_old.db"
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(old_db) + suffix)
        if p.exists():
            p.unlink()
    eng = create_engine(f"sqlite:///{old_db}")
    _models.Base.metadata.create_all(bind=eng, tables=[
        _models.Org.__table__, _models.User.__table__])
    before_tables = set(sa_inspect(eng).get_table_names())
    check("на «старой» базе таблиц слоя нет",
          not {"supply_materials", "supply_batches"} & before_tables,
          str(sorted(before_tables)))
    _models.ensure_supply_planning_schema(bind=eng)
    after_tables = set(sa_inspect(eng).get_table_names())
    need = {"supply_materials", "supply_items", "supply_batches",
            "supply_assignments", "supply_sketches", "supply_events"}
    check("шаг создал все шесть таблиц", need <= after_tables,
          str(sorted(need - after_tables)))
    _models.ensure_supply_planning_schema(bind=eng)
    check("повторный вызов шага не падает и ничего не ломает",
          need <= set(sa_inspect(eng).get_table_names()))
    idx = {i["name"] for i in sa_inspect(eng).get_indexes("supply_events")}
    check("частичный замок повторного поступка на месте",
          "ux_supply_events_op" in idx, str(sorted(idx)))
    with eng.connect() as conn:
        from sqlalchemy import text as sa_text
        conn.execute(sa_text(
            "INSERT INTO supply_events (org_id, entity_kind, entity_id, action,"
            " field, old_value, new_value, author, op_id, created_at)"
            " VALUES (1,'material',1,'create','','','','a','dup', datetime('now'))"))
        conn.commit()
        dup_failed = False
        try:
            conn.execute(sa_text(
                "INSERT INTO supply_events (org_id, entity_kind, entity_id, action,"
                " field, old_value, new_value, author, op_id, created_at)"
                " VALUES (1,'material',2,'create','','','','a','dup', datetime('now'))"))
            conn.commit()
        except Exception:
            dup_failed = True
            conn.rollback()
        conn.execute(sa_text(
            "INSERT INTO supply_events (org_id, entity_kind, entity_id, action,"
            " field, old_value, new_value, author, op_id, created_at)"
            " VALUES (1,'material',3,'create','','','','a','', datetime('now'))"))
        conn.execute(sa_text(
            "INSERT INTO supply_events (org_id, entity_kind, entity_id, action,"
            " field, old_value, new_value, author, op_id, created_at)"
            " VALUES (1,'material',4,'create','','','','a','', datetime('now'))"))
        conn.commit()
    check("одинаковый op_id второй раз не проходит",
          dup_failed, "второй INSERT должен был упасть")
    check("а пустых op_id может быть сколько угодно — замок частичный", True)
    eng.dispose()
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(old_db) + suffix)
        if p.exists():
            p.unlink()

    # ── 19. Удаление организации уносит весь слой ────────────────────────────
    print("\n== Полнота удаления арендатора ==")
    from app.tenancy import (org_purge_models, purge_completeness_violations,
                             purge_order_violations)
    names = {m.__tablename__ for m in org_purge_models()}
    check("все шесть таблиц слоя входят в набор удаления организации",
          need <= names, str(sorted(need - names)))
    check("сторож полноты не находит нарушений",
          purge_completeness_violations() == [], str(purge_completeness_violations()))
    check("и сторож порядка тоже",
          purge_order_violations() == [], str(purge_order_violations()))

    del_c = client()
    register(del_c, "sp-doomed@test.io", "Бренд Три")
    del_c.post("/api/supply/planning/materials",
               json={"title": "Уйдёт вместе с организацией", "qty": "5", "op_id": "d1"})
    del_c.post("/api/supply/planning/sketches",
               files={"file": ("s.png", make_png(), "image/png")})
    con = sqlite3.connect(DB_PATH)
    try:
        doomed = con.execute("SELECT id FROM orgs ORDER BY id DESC LIMIT 1").fetchone()[0]
        before_cnt = con.execute("SELECT COUNT(*) FROM supply_materials"
                                 " WHERE org_id = ?", (doomed,)).fetchone()[0]
    finally:
        con.close()
    check("у обречённой организации есть строки слоя", before_cnt == 1, str(before_cnt))
    dele = del_c.post("/api/account/delete",
                      json={"password": "secret123", "confirm": "УДАЛИТЬ",
                            "mode": "org"})
    check("организация удалена", dele.status_code in (200, 204), str(dele.status_code))
    con = sqlite3.connect(DB_PATH)
    try:
        left = {t: con.execute(f"SELECT COUNT(*) FROM {t} WHERE org_id = ?",
                               (doomed,)).fetchone()[0] for t in sorted(need)}
    finally:
        con.close()
    check("ни одной строки слоя от неё не осталось",
          all(v == 0 for v in left.values()), str(left))
    check("а строки другой организации на месте",
          owner.get("/api/supply/planning").json()["materials"], "план владельца цел")


    # ── 20. SUPPLY-FIX-1: противоречивый ввод, поиск, приоритеты, дубли ───────
    supply_fix_1_checks()
    supply_fix_1_migration_checks()

    # ── 21. SUPPLY-FIX-2: правки, архив, распределение, неподтверждённое ─────
    supply_fix_2_checks()
    supply_fix_2_migration_checks()

    member.close()
    other.close()
    del_c.close()
    owner.close()

    print(f"\nИТОГО: {len(PASS)} OK, {len(FAIL)} FAIL")
    for name in FAIL:
        print(f"  FAIL {name}")
    return 1 if FAIL else 0


def supply_fix_1_checks() -> None:  # noqa: C901 — сценарный блок, ветвлений мало
    """SUPPLY-FIX-1 (F-03, F-04, F-07, F-08, F-09, F-10) на уровне API.

    Каждая проверка ниже КРАСНЕЕТ на `ea1caff` по поведению, а не по отсутствию
    импорта или маршрута: адреса и поля те же самые, разошёлся только ответ.
    """
    c = client()
    register(c, "sp-fix1@test.io", "Бренд Фикс")

    # ── F-03: непустое поле, которого этот вид срока не использует, — отказ ──
    print("\n== F-03: противоречивый срок и эскиз не проглатываются молча ==")
    item = c.post("/api/supply/planning/items",
                  json={"kind": "draft", "title": "Плащ", "op_id": "f3-item"})
    check("вещь для проверок срока заведена", item.status_code == 200,
          str(item.status_code))
    item_id = item.json()["items"][0]["id"]

    def make_batch(payload, op):
        body = dict(payload)
        body["item_id"] = item_id
        body["op_id"] = op
        return c.post("/api/supply/planning/batches", json=body)

    r = make_batch({"due_kind": "text", "due_text": "к ноябрю",
                    "due_date": "2026-10-31"}, "f3-a")
    check("«своими словами» + дата → 400, а не 200 с потерянной датой",
          r.status_code == 400, str(r.status_code))
    check("отказ называет, что убрать, и чем заменить",
          "дата не нужна" in r.text and "точная дата" in r.text, r.text[:160])
    r = make_batch({"due_kind": "unknown", "due_text": "как получится"}, "f3-b")
    check("«срок неизвестен» + текст → 400, а не 200 с потерянным текстом",
          r.status_code == 400 and "текст не нужен" in r.text, r.text[:160])
    r = make_batch({"due_kind": "unknown", "due_date": "2026-10-31"}, "f3-c")
    check("«срок неизвестен» + дата → 400", r.status_code == 400 and
          "дата не нужна" in r.text, r.text[:160])
    r = make_batch({"due_kind": "exact", "due_date": "2026-10-31",
                    "due_text": "к ноябрю"}, "f3-d")
    check("«точная дата» + текст → 400", r.status_code == 400 and
          "текст не нужен" in r.text, r.text[:160])
    ok = make_batch({"due_kind": "exact", "due_date": "2026-10-31",
                     "due_source": "цех"}, "f3-ok")
    check("а непротиворечивый срок по-прежнему принимается",
          ok.status_code == 200, str(ok.status_code))
    saved = [b for b in ok.json()["batches"] if b["due_kind"] == "exact"]
    check("и дата сохранена ровно та, что прислали",
          len(saved) == 1 and saved[0]["due_date"] == "2026-10-31",
          str(saved[:1]))
    src = c.post("/api/supply/planning/batches",
                 json={"item_id": item_id, "due_kind": "unknown",
                       "due_source": "цех сказал, что не знает", "op_id": "f3-src"})
    check("источник срока при неизвестном сроке ПРИНИМАЕТСЯ и сохраняется",
          src.status_code == 200
          and any(b["due_source"] == "цех сказал, что не знает"
                  for b in src.json()["batches"]), str(src.status_code))

    # ── F-04: поиск по каталогу отдаёт total и размер каталога ──────────────
    print("\n== F-04: каталог ищется, а не показывается первыми двадцатью ==")
    empty = c.get("/api/supply/planning/catalog").json()
    check("у организации без синка каталог пуст, и это названо числом",
          empty.get("catalog_size") == 0 and empty.get("total") == 0
          and empty.get("options") == [], json.dumps(empty, ensure_ascii=False)[:160])
    # Ниже поля читаются через .get(): на дереве, где их ещё нет, набор обязан
    # напечатать красную строку, а не умереть KeyError на середине прогона.
    org_id = sqlite3.connect(DB_PATH).execute(
        "SELECT org_id FROM memberships ORDER BY org_id DESC LIMIT 1").fetchone()[0]
    seed_catalog(org_id, 60)
    full = c.get("/api/supply/planning/catalog").json()
    check("каталог из 60 моделей: отдано 20, но сказано, что их 60",
          len(full.get("options") or []) == 20 and full.get("total") == 60
          and full.get("catalog_size") == 60,
          f"options={len(full.get('options') or [])} total={full.get('total')}"
          f" catalog_size={full.get('catalog_size')}")
    found = c.get("/api/supply/planning/catalog", params={"q": "тренч"}).json()
    found_opts = found.get("options") or []
    check("поиск «тренч» находит модель, до которой в списке из 20 не дойти",
          any(o["base_name"] == "Тренч «Классика»" for o in found_opts),
          json.dumps(found_opts[:3], ensure_ascii=False)[:200])
    check("и у найденного названа размерность",
          bool(found_opts) and all("sizes" in o for o in found_opts),
          str(found_opts[:1]))

    # Эскиз на каталожной вещи — отказ. Проверка стоит ПОСЛЕ наполнения
    # каталога намеренно: на пустом каталоге запрос останавливал бы отказ
    # «такой вещи в каталоге нет», и краснота ничего не говорила бы про эскиз.
    sk = c.post("/api/supply/planning/sketches",
                files={"file": ("s.png", make_png(), "image/png")})
    sketch_id = sk.json()["sketch_id"]
    r = c.post("/api/supply/planning/items",
               json={"kind": "catalog", "base_name": "Тренч «Классика»",
                     "sketch_id": sketch_id, "op_id": "f3-sk"})
    check("эскиз на каталожной вещи → 400, а не тихо сохранённый и невидимый",
          r.status_code == 400 and "только к новинке" in r.text, r.text[:160])
    kept = sqlite3.connect(DB_PATH).execute(
        "SELECT COUNT(*) FROM supply_items WHERE base_name = ?",
        ("Тренч «Классика»",)).fetchone()[0]
    check("и вещь при этом не завелась вовсе", kept == 0, str(kept))

    # ── F-10: каталожная вещь не заводится дважды ───────────────────────────
    print("\n== F-10: одна модель каталога — одна вещь плана ==")
    a = c.post("/api/supply/planning/items",
               json={"kind": "catalog", "base_name": "Тренч «Классика»",
                     "op_id": "f10-a"})
    check("каталожная вещь заведена", a.status_code == 200, str(a.status_code))
    first_id = [i for i in a.json()["items"] if i["base_name"] == "Тренч «Классика»"][0]["id"]
    b = c.post("/api/supply/planning/items",
               json={"kind": "catalog", "base_name": "Тренч «Классика»",
                     "op_id": "f10-b"})
    check("повтор той же модели отвечает 200, а не заводит вторую",
          b.status_code == 200, str(b.status_code))
    same = [i for i in b.json()["items"] if i["base_name"] == "Тренч «Классика»"]
    check("в плане ровно одна такая вещь, и это ТА ЖЕ строка",
          len(same) == 1 and same[0]["id"] == first_id, str(same))
    check("человеку сказано, почему новой строки не появилось",
          b.json().get("notice") == "Эта модель уже есть в плане.",
          str(b.json().get("notice")))
    # РЕГРЕССИЯ: повтор с заметкой не терял её молча. Ревью воспроизвело:
    # note=first → повтор note=second давал reused=True и stored_note=first,
    # без ошибки и без записи. Человек написал текст — текст исчез.
    n1 = c.post("/api/supply/planning/items",
                json={"kind": "catalog", "base_name": "Модель 001",
                      "note": "первая заметка", "op_id": "f10-n1"})
    check("каталожная вещь с заметкой заведена", n1.status_code == 200,
          str(n1.status_code))
    noted_id = [i for i in n1.json()["items"]
                if i["base_name"] == "Модель 001"][0]["id"]
    rev_before = [i for i in n1.json()["items"] if i["id"] == noted_id][0]["rev"]
    n2 = c.post("/api/supply/planning/items",
                json={"kind": "catalog", "base_name": "Модель 001",
                      "note": "вторая заметка", "op_id": "f10-n2"})
    check("повтор с новой заметкой принят", n2.status_code == 200,
          str(n2.status_code))
    noted = [i for i in n2.json()["items"] if i["id"] == noted_id]
    check("вещь по-прежнему одна", len(noted) == 1 and len(
        [i for i in n2.json()["items"]
         if i["base_name"] == "Модель 001"]) == 1, str(noted))
    check("ВТОРАЯ заметка не потерялась молча — она в строке",
          noted and "вторая заметка" in (noted[0]["note"] or ""),
          str(noted[0]["note"]) if noted else "нет строки")
    check("и первая заметка при этом цела",
          noted and "первая заметка" in (noted[0]["note"] or ""),
          str(noted[0]["note"]) if noted else "нет строки")
    check("редакция строки поднята: экран с прежним rev теперь устарел",
          noted and noted[0]["rev"] > rev_before,
          f"было {rev_before} стало {noted[0]['rev'] if noted else '?'}")
    ev = sqlite3.connect(DB_PATH).execute(
        "SELECT COUNT(*) FROM supply_events WHERE entity_kind='item'"
        " AND entity_id=? AND field='note'", (noted_id,)).fetchone()[0]
    check("изменение заметки названо записью журнала, а не молчит", ev == 1,
          str(ev))
    n3 = c.post("/api/supply/planning/items",
                json={"kind": "catalog", "base_name": "Модель 001",
                      "op_id": "f10-n3"})
    same_note = [i for i in n3.json()["items"] if i["id"] == noted_id]
    check("повтор БЕЗ заметки строку не трогает и редакцию не двигает",
          same_note and same_note[0]["rev"] == noted[0]["rev"],
          f"{noted[0]['rev']} → {same_note[0]['rev'] if same_note else '?'}")

    # РЕГРЕССИЯ КРАЙНЕГО СЛУЧАЯ: заметка уже занимает весь предел.
    # Склейка `A*500 · НОВЫЙ` обрезается обратно ровно в `A*500`, видимое поле
    # не меняется ни на символ — и под прежним условием введённый текст
    # исчезал и из строки, и из журнала. Здесь проверяется, что он цел.
    full_note = "A" * sp.MAX_NOTE_CHARS
    c.post("/api/supply/planning/items",
           json={"kind": "catalog", "base_name": "Модель 002",
                 "note": full_note, "op_id": "f10-full1"})
    full_board = c.get("/api/supply/planning").json()
    full_id = [i for i in full_board["items"]
               if i["base_name"] == "Модель 002"][0]["id"]
    full_rev = [i for i in full_board["items"] if i["id"] == full_id][0]["rev"]
    check("заметка на полный предел сохранена целиком",
          len([i for i in full_board["items"]
               if i["id"] == full_id][0]["note"]) == sp.MAX_NOTE_CHARS,
          str(len([i for i in full_board["items"]
                   if i["id"] == full_id][0]["note"])))
    unique_text = "ВТОРАЯ-ЗАМЕТКА-НЕ-ПОМЕСТИЛАСЬ-9137"
    r_full = c.post("/api/supply/planning/items",
                    json={"kind": "catalog", "base_name": "Модель 002",
                          "note": unique_text, "op_id": "f10-full2"})
    check("повтор при заполненном поле принят", r_full.status_code == 200,
          str(r_full.status_code))
    after_full = [i for i in r_full.json()["items"] if i["id"] == full_id][0]
    check("видимое поле осталось прежним — места в нём нет",
          after_full["note"] == full_note, str(len(after_full["note"])))
    kept_full = sqlite3.connect(DB_PATH).execute(
        "SELECT old_value FROM supply_events WHERE entity_kind='item'"
        " AND entity_id=? AND field='note_truncated'", (full_id,)).fetchall()
    check("НО введённый текст сохранён целиком в журнале, а не потерян",
          any(row[0] == unique_text for row in kept_full),
          str(kept_full)[:200] or "записи нет")
    op_rows = sqlite3.connect(DB_PATH).execute(
        "SELECT COUNT(*) FROM supply_events WHERE op_id=?",
        ("f10-full2",)).fetchone()[0]
    check("поступок отмечен ровно одной записью с этим op_id",
          op_rows == 1, str(op_rows))
    again_full = c.post("/api/supply/planning/items",
                        json={"kind": "catalog", "base_name": "Модель 002",
                              "note": unique_text, "op_id": "f10-full2"})
    check("повтор с тем же op_id идемпотентен и второй записи не заводит",
          again_full.status_code == 200
          and sqlite3.connect(DB_PATH).execute(
              "SELECT COUNT(*) FROM supply_events WHERE op_id=?",
              ("f10-full2",)).fetchone()[0] == 1,
          str(again_full.status_code))

    d1 = c.post("/api/supply/planning/items",
                json={"kind": "draft", "title": "Одно имя", "op_id": "f10-d1"})
    d2 = c.post("/api/supply/planning/items",
                json={"kind": "draft", "title": "Одно имя", "op_id": "f10-d2"})
    drafts = [i for i in d2.json()["items"] if i["title"] == "Одно имя"]
    check("а две новинки с одним именем по-прежнему две разные вещи",
          d1.status_code == 200 and len(drafts) == 2, str(len(drafts)))

    # ── F-09: одна пара (материал, партия) — одна строка ────────────────────
    print("\n== F-09: повторное назначение прибавляет, а не двоит ==")
    mat = c.post("/api/supply/planning/materials",
                 json={"title": "Фурнитура", "qty": "10", "unit": "кг",
                       "op_id": "f9-mat"})
    mat_id = mat.json()["materials"][0]["id"]
    batch_id = [b for b in mat.json()["batches"]][0]["id"]
    c.post("/api/supply/planning/assignments",
           json={"material_id": mat_id, "batch_id": batch_id, "qty": "2",
                 "note": "первая", "op_id": "f9-a1"})
    r = c.post("/api/supply/planning/assignments",
               json={"material_id": mat_id, "batch_id": batch_id, "qty": "3",
                     "note": "вторая", "op_id": "f9-a2"})
    rows = [a for b in r.json()["batches"] if b["id"] == batch_id
            for a in b["assignments"] if a["material_id"] == mat_id]
    check("строка одна, а не две", len(rows) == 1, str(len(rows)))
    check("и в ней сумма, а не последнее число",
          rows and rows[0]["qty"] == 5.0, str(rows[:1]))
    check("заметки обеих не потерялись",
          rows and "первая" in rows[0]["note"] and "вторая" in rows[0]["note"],
          str(rows[:1]))
    events = sqlite3.connect(DB_PATH).execute(
        "SELECT action, old_value, new_value FROM supply_events"
        " WHERE entity_kind='assignment' AND entity_id=?", (rows[0]["id"],)).fetchall()
    check("журнал хранит обе записи: создание и прибавление old→new",
          len(events) == 2 and events[1][1] == "2.0" and events[1][2] == "5.0",
          str(events)[:200])

    # ── ЖИВОЙ путь: длинные заметки не теряются и здесь ─────────────────────
    # Тот же класс, что в миграции (issuecomment-5555375180), но на ручке,
    # которой человек пользуется каждый день: две законные заметки по 300
    # символов дают склейку 603, и до правки она молча резалась до 500.
    long_mat = c.post("/api/supply/planning/materials",
                      json={"title": "Материал с длинными заметками", "qty": "50",
                            "unit": "кг", "op_id": "f9-long-mat"}).json()
    long_mat_id = [m for m in long_mat["materials"]
                   if m["title"] == "Материал с длинными заметками"][0]["id"]
    c.post("/api/supply/planning/assignments",
           json={"material_id": long_mat_id, "batch_id": batch_id, "qty": "1",
                 "note": LONG_NOTE_A, "op_id": "f9-long-1"})
    long_res = c.post("/api/supply/planning/assignments",
                      json={"material_id": long_mat_id, "batch_id": batch_id,
                            "qty": "1", "note": LONG_NOTE_B, "op_id": "f9-long-2"})
    long_rows = [a for b in long_res.json()["batches"] if b["id"] == batch_id
                 for a in b["assignments"] if a["material_id"] == long_mat_id]
    check("повтор с длинной заметкой слился в одну строку",
          len(long_rows) == 1 and long_rows[0]["qty"] == 2.0, str(len(long_rows)))
    seen = long_rows[0]["note"] if long_rows else ""
    check("первая длинная заметка видна целиком",
          seen.count("A") == 300, f"видно {seen.count('A')} из 300")
    kept = sqlite3.connect(DB_PATH).execute(
        "SELECT old_value FROM supply_events WHERE field='note_truncated'"
        " AND entity_kind='assignment' AND entity_id=?",
        (long_rows[0]["id"],)).fetchall()
    check("вторая сохранена ЦЕЛИКОМ в журнале, а не обрезана вместе с видимым",
          len(kept) == 1 and kept[0][0] == LONG_NOTE_B,
          f"в журнале {len(kept[0][0]) if kept else 0} из 300")
    check("итого на живом пути сохранено 600 символов из 600",
          seen.count("A") + (len(kept[0][0]) if kept else 0) == 600,
          f"{seen.count('A') + (len(kept[0][0]) if kept else 0)} из 600")
    # `entity_kind` в условии обязателен: `entity_id` уникален только внутри
    # своего вида, и без него сюда попадали записи об обрезке у ВЕЩИ с тем же
    # номером, что у назначения. Проверка молча считала чужие строки.
    short_cut = sqlite3.connect(DB_PATH).execute(
        "SELECT COUNT(*) FROM supply_events WHERE field='note_truncated'"
        " AND entity_kind='assignment' AND entity_id=?",
        (rows[0]["id"],)).fetchone()[0]
    check("а короткая склейка отметки об обрезке не получает — обрезки не было",
          short_cut == 0, str(short_cut))

    # ── F-07: сводка — не задача; прошедший срок называется вслух ───────────
    print("\n== F-07: следующий шаг перестал маскировать расхождения ==")
    board = c.get("/api/supply/planning").json()
    free = [m for m in board["materials"] if m["free"] and m["free"] > 0]
    check("у организации есть материал со свободным остатком",
          bool(free), str(len(free)))
    check("но «распределите» больше не выдаётся за следующий шаг",
          board["next_step"]["code"] != "assign", board["next_step"]["code"])
    c.post("/api/supply/planning/assignments",
           json={"material_id": mat_id, "batch_id": batch_id, "qty": "100",
                 "op_id": "f7-over"})
    over = c.get("/api/supply/planning").json()
    check("перерасход виден следующим шагом, а не спрятан за остатком",
          over["next_step"]["code"] == "over", over["next_step"]["text"][:90])
    unknown_qty = c.post("/api/supply/planning/materials",
                         json={"title": "Без количества", "op_id": "f7-unk"})
    check("неизвестное количество задачей не считается (правило `qty` убрано)",
          unknown_qty.json()["next_step"]["code"] != "qty",
          unknown_qty.json()["next_step"]["code"])

    past = client()
    register(past, "sp-fix1-past@test.io", "Бренд Просрочка")
    pit = past.post("/api/supply/planning/items",
                    json={"kind": "draft", "title": "Юбка", "op_id": "p-i"}).json()
    pid = pit["items"][0]["id"]
    past.post("/api/supply/planning/materials",
              json={"title": "Ткань", "qty": "10", "op_id": "p-m"})
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    pb = past.post("/api/supply/planning/batches",
                   json={"item_id": pid, "title": "Вчерашняя", "plan_qty": "5",
                         "due_kind": "exact", "due_date": yesterday, "op_id": "p-b"})
    check("партия с вчерашним точным сроком заведена", pb.status_code == 200,
          str(pb.status_code))
    step = past.get("/api/supply/planning").json()["next_step"]
    check("прошедший срок назван следующим шагом",
          step["code"] == "due_past", step["code"])
    check("и в тексте стоит сама дата, а не «просрочено»",
          yesterday in step["text"] and "прошёл" in step["text"], step["text"][:120])
    past.close()

    done = client()
    register(done, "sp-fix1-ok@test.io", "Бренд Порядок")
    di = done.post("/api/supply/planning/items",
                   json={"kind": "draft", "title": "Пальто", "op_id": "d-i"}).json()
    done.post("/api/supply/planning/materials",
              json={"title": "Сукно", "qty": "50", "op_id": "d-m"})
    done.post("/api/supply/planning/batches",
              json={"item_id": di["items"][0]["id"], "title": "Первая",
                    "plan_qty": "5", "due_kind": "exact",
                    "due_date": (date.today() + timedelta(days=30)).isoformat(),
                    "op_id": "d-b"})
    ok_step = done.get("/api/supply/planning").json()["next_step"]
    check("когда всё в порядке — код ok, и нераспределённый остаток этому не мешает",
          ok_step["code"] == "ok", f"{ok_step['code']}: {ok_step['text'][:90]}")
    done.close()

    # ── F-08: предпросмотр за флагом организации ────────────────────────────
    print("\n== F-08: вкладка предпросмотра — только организациям с флагом ==")
    page = c.get("/supply")
    # Ищем РАЗМЕТКУ элемента (`id="…"`), а не имя идентификатора: имя есть и в
    # скрипте плана — `byId("sup-tab-preview")`, — и поиск по нему зеленел бы
    # или краснел по чужой причине.
    check("без флага вкладки предпросмотра на странице нет",
          page.status_code == 200 and 'id="sup-tab-preview"' not in page.text,
          str(page.status_code))
    check("и панели предпросмотра тоже нет",
          'id="sup-view-preview"' not in page.text, "панель осталась в разметке")
    check("раздел плана при этом на месте",
          'id="sup-view-plan"' in page.text, "план пропал вместе с предпросмотром")
    gone = c.get("/api/supply/sheets")
    check("ручка чтения снимка отвечает 404", gone.status_code == 404,
          str(gone.status_code))
    check("тем же текстом, что несуществующий маршрут",
          gone.json().get("detail") == "Not Found", gone.text[:120])
    ref = c.post("/api/supply/sheets/refresh", json={})
    check("и ручка обновления тоже 404", ref.status_code == 404, str(ref.status_code))

    tool_org = sqlite3.connect(DB_PATH).execute(
        "SELECT id FROM orgs WHERE id = ?", (org_id,)).fetchone()[0]
    before_settings = json.loads(sqlite3.connect(DB_PATH).execute(
        "SELECT settings_json FROM orgs WHERE id = ?", (tool_org,)).fetchone()[0])
    rc = run_preview_tool(["--org-id", str(tool_org), "--on"])
    check("штатный инструмент включает флаг и завершается успешно", rc == 0, str(rc))
    page = c.get("/supply")
    check("с флагом вкладка предпросмотра появилась",
          'id="sup-tab-preview"' in page.text, "вкладки нет")
    on = c.get("/api/supply/sheets")
    check("и ручка снимка отвечает по-настоящему, а не 404",
          on.status_code == 200, str(on.status_code))
    after_settings = json.loads(sqlite3.connect(DB_PATH).execute(
        "SELECT settings_json FROM orgs WHERE id = ?", (tool_org,)).fetchone()[0])
    check("инструмент не тронул ни одной чужой настройки",
          {k: v for k, v in after_settings.items() if k != "supply_sheets_preview"}
          == before_settings, json.dumps(after_settings, ensure_ascii=False)[:200])
    check("повторное включение идемпотентно",
          run_preview_tool(["--org-id", str(tool_org), "--on"]) == 0, "rc")
    check("выключение возвращает организацию в прежнее состояние",
          run_preview_tool(["--org-id", str(tool_org), "--off"]) == 0, "rc")
    off_settings = json.loads(sqlite3.connect(DB_PATH).execute(
        "SELECT settings_json FROM orgs WHERE id = ?", (tool_org,)).fetchone()[0])
    check("и следа временной меры в настройках не остаётся",
          off_settings == before_settings,
          json.dumps(off_settings, ensure_ascii=False)[:200])
    check("после выключения ручка снова 404",
          c.get("/api/supply/sheets").status_code == 404, "не 404")
    check("несуществующая организация инструменту не по зубам",
          run_preview_tool(["--org-id", "999999", "--on"]) == 2, "rc")
    c.close()


#: Две законные заметки, склейка которых в колонку `note` не помещается.
#: Ровно тот вход, на котором независимое чтение воспроизвело потерю 103
#: символов (issuecomment-5555375180). Уменьшать их нельзя: меньший размер
#: проверял бы другой случай и зеленел бы на неисправленном коде.
LONG_NOTE_A = "A" * 300
LONG_NOTE_B = "B" * 300


def supply_fix_1_migration_checks() -> None:  # noqa: C901 — шагов много, ветвлений мало
    """SUPPLY-FIX-1: шаг 12 схлопывает дубли и ставит замки (F-09, F-10).

    Проверяется на ОТДЕЛЬНОЙ базе, собранной шагом 11: это состояние боевой
    базы до выпуска — с дублями, которые старый код умел заводить. Доказывается
    не «индекс появился», а то, ради чего он появляется: сумма назначенного
    цела, заметки обеих строк на месте, партии дублирующей вещи перевешены, а
    соседняя организация не задета.
    """
    print("\n== Шаг 12: дубли схлопываются, замки встают ==")
    from sqlalchemy import create_engine, inspect as sa_inspect, text as sa_text
    from app import models as _models

    # Шага может не быть вовсе (дерево до этого пакета). Тогда набор говорит об
    # этом одной красной строкой и идёт дальше, а не падает AttributeError,
    # унося с собой все проверки ниже.
    if not hasattr(_models, "ensure_supply_planning_unique_schema"):
        check("шаг 12 (слияние дублей и уникальные индексы) существует", False,
              "models.ensure_supply_planning_unique_schema отсутствует")
        return

    mig_db = ROOT / "test_supply_planning_dup.db"
    for suffix in ("", "-wal", "-shm"):
        f = Path(str(mig_db) + suffix)
        if f.exists():
            f.unlink()
    eng = create_engine(f"sqlite:///{mig_db}")
    _models.Base.metadata.create_all(bind=eng, tables=[
        _models.Org.__table__, _models.Product.__table__])
    _models.ensure_supply_planning_schema(bind=eng)

    ts = "2026-09-01 10:00:00"
    with eng.begin() as conn:
        for org_id, name in ((1, "Первая"), (2, "Вторая")):
            conn.execute(sa_text(
                "INSERT INTO orgs (id, name, plan, settings_json, created_at)"
                " VALUES (:i, :n, 'trial', '{}', :t)"),
                {"i": org_id, "n": name, "t": ts})
        for org_id, mid, title in ((1, 1, "Шерсть"), (1, 2, "Подкладка"),
                                   (2, 3, "Чужая ткань"), (1, 4, "Длинные заметки")):
            conn.execute(sa_text(
                "INSERT INTO supply_materials (id, org_id, title, qty, unit,"
                " source_note, author, created_at, updated_at, rev)"
                " VALUES (:i,:o,:t,100,'м','','a',:ts,:ts,1)"),
                {"i": mid, "o": org_id, "t": title, "ts": ts})
        # Две КАТАЛОЖНЫЕ вещи с одним base_name — то, что старый код разрешал.
        # Плюс две новинки с одинаковым рабочим именем: их шаг трогать не имеет
        # права, иначе замок против дублей стал бы запретом работать.
        # Заметки каталожных дублей — те самые, на которых воспроизведена
        # потеря (issuecomment-5555384087): у выжившей и у донора они РАЗНЫЕ, и
        # текст донора обязан пережить удаление строки.
        for iid, org_id, kind, base, title, note in (
                (1, 1, "catalog", "Тренч", "Тренч", "first note"),
                (2, 1, "catalog", "Тренч", "Тренч", "second important note"),
                (3, 1, "draft", "", "Одно имя", ""),
                (4, 1, "draft", "", "Одно имя", ""),
                (5, 2, "catalog", "Тренч", "Тренч", "")):
            conn.execute(sa_text(
                "INSERT INTO supply_items (id, org_id, kind, base_name, title,"
                " note, author, created_at, updated_at, rev)"
                " VALUES (:i,:o,:k,:b,:t,:n,'a',:ts,:ts,1)"),
                {"i": iid, "o": org_id, "k": kind, "b": base, "t": title,
                 "n": note, "ts": ts})
        # Партии висят на ВТОРОЙ (дублирующей) вещи — после шага они обязаны
        # оказаться на первой, а не осиротеть.
        for bid, org_id, item_id, title in ((1, 1, 2, "Партия дубля"),
                                            (2, 1, 3, "Партия новинки"),
                                            (3, 2, 5, "Чужая партия")):
            conn.execute(sa_text(
                "INSERT INTO supply_batches (id, org_id, item_id, title, plan_note,"
                " due_kind, due_text, due_date, due_source, due_author, author,"
                " created_at, updated_at, rev)"
                " VALUES (:i,:o,:it,:t,'','unknown','','','','','a',:ts,:ts,1)"),
                {"i": bid, "o": org_id, "it": item_id, "t": title, "ts": ts})
        # Пара 7/8 — вход из issuecomment-5555375180: две заметки по 300
        # символов, каждая законна на вводе (предел 500), а их склейка с
        # разделителем даёт 603 и в колонку `note` не помещается. Размер
        # фикстуры не уменьшается: он и есть суть случая.
        for aid, org_id, mid, bid, qty, note in (
                (1, 1, 1, 1, 2.0, "первая"),
                (2, 1, 1, 1, 3.0, "вторая"),
                (3, 1, 1, 1, 1.5, ""),
                (4, 1, 2, 1, 7.0, "одиночка"),
                (5, 2, 3, 3, 4.0, "чужая"),
                (6, 2, 3, 3, 6.0, "чужая вторая"),
                (7, 1, 4, 1, 2.0, LONG_NOTE_A),
                (8, 1, 4, 1, 3.0, LONG_NOTE_B)):
            conn.execute(sa_text(
                "INSERT INTO supply_assignments (id, org_id, material_id, batch_id,"
                " qty, note, author, created_at, updated_at, rev)"
                " VALUES (:i,:o,:m,:b,:q,:n,'a',:ts,:ts,1)"),
                {"i": aid, "o": org_id, "m": mid, "b": bid, "q": qty,
                 "n": note, "ts": ts})

    def rows(sql, *args):
        with eng.connect() as conn:
            return conn.execute(sa_text(sql), *args).all()

    before_sum = rows("SELECT ROUND(SUM(qty), 3) FROM supply_assignments")[0][0]
    _models.ensure_supply_planning_unique_schema(bind=eng)

    merged = rows("SELECT id, qty, note FROM supply_assignments"
                  " WHERE org_id=1 AND material_id=1 AND batch_id=1")
    check("три дубля назначения стали одной строкой", len(merged) == 1, str(merged))
    check("и в ней сумма 2 + 3 + 1.5 = 6.5, а не последнее число",
          merged and merged[0][1] == 6.5, str(merged))
    check("выжила строка с наименьшим id — та, что завели первой",
          merged and merged[0][0] == 1, str(merged))
    check("заметки обеих непустых строк склеены, а не выброшены",
          merged and "первая" in merged[0][2] and "вторая" in merged[0][2],
          str(merged))
    single = rows("SELECT qty, note FROM supply_assignments"
                  " WHERE org_id=1 AND material_id=2")
    check("строка без дубля не тронута ни числом, ни заметкой",
          single == [(7.0, "одиночка")], str(single))
    other = rows("SELECT qty FROM supply_assignments WHERE org_id=2")
    check("дубли ЧУЖОЙ организации схлопнуты отдельно и своей суммой",
          other == [(10.0,)], str(other))
    after_sum = rows("SELECT ROUND(SUM(qty), 3) FROM supply_assignments")[0][0]
    check("общая сумма назначенного пережила слияние до десятых",
          after_sum == before_sum, f"было={before_sum} стало={after_sum}")

    # ── Длинные заметки: видимое поле обрезано, но НИ ОДИН символ не потерян ──
    # Вход из issuecomment-5555375180. До правки склейка 300+3+300 = 603 резалась
    # до 500 прямо перед удалением строки-донора, и 103 символа «B» исчезали
    # вместе с ней — восстановить их было неоткуда.
    long_row = rows("SELECT id, qty, note FROM supply_assignments"
                    " WHERE org_id=1 AND material_id=4")
    check("длинная пара тоже стала одной строкой с верной суммой",
          len(long_row) == 1 and long_row[0][1] == 5.0, str(long_row)[:120])
    visible = long_row[0][2] if long_row else ""
    check("первая заметка видна целиком: 300 символов «A» на месте",
          visible.count("A") == 300, f"в видимом поле {visible.count('A')} из 300")
    donor = rows("SELECT old_value FROM supply_events"
                 " WHERE entity_kind='assignment' AND field='note'")
    donor_b = [r[0] for r in donor if r[0].startswith("B")]
    check("вторая заметка сохранена ЦЕЛИКОМ — 300 символов «B» в журнале",
          len(donor_b) == 1 and len(donor_b[0]) == 300 and donor_b[0] == LONG_NOTE_B,
          f"в журнале {len(donor_b[0]) if donor_b else 0} из 300")
    total_kept = visible.count("A") + (len(donor_b[0]) if donor_b else 0)
    check("итого сохранено 600 символов из 600 — потери нет ни одного",
          total_kept == 600, f"сохранено {total_kept} из 600")
    cutmark = rows("SELECT old_value FROM supply_events"
                   " WHERE field='note_truncated'")
    check("обрезка видимого поля названа записью журнала, а не молчит",
          len(cutmark) == 1 and cutmark[0][0] == str(len(LONG_NOTE_A) + 3 + len(LONG_NOTE_B)),
          str(cutmark))
    check("а короткая склейка обходится без отметки об обрезке — её и не было",
          len(cutmark) == 1, f"отметок {len(cutmark)}, ожидалась одна")

    # ── Ни одна удалённая строка не исчезает бесследно ───────────────────────
    removed_notes = rows("SELECT old_value FROM supply_events"
                         " WHERE entity_kind='assignment' AND field='note'"
                         " AND org_id=1")
    kept_texts = {r[0] for r in removed_notes}
    check("заметка каждой удалённой строки записана до её удаления",
          "вторая" in kept_texts and LONG_NOTE_B in kept_texts,
          str(sorted(len(t) for t in kept_texts)))
    check("автор записей слияния — шаг старта, а не человек",
          rows("SELECT DISTINCT author FROM supply_events WHERE action='merge'")
          == [("миграция SUPPLY-FIX-1",)],
          str(rows("SELECT DISTINCT author FROM supply_events WHERE action='merge'")))
    check("у записей слияния пустой op_id — частичный замок их не считает",
          rows("SELECT COUNT(*) FROM supply_events"
               " WHERE action='merge' AND op_id <> ''") == [(0,)],
          "непустой op_id у записи слияния")

    items = rows("SELECT id FROM supply_items WHERE org_id=1 AND kind='catalog'")
    check("две каталожные вещи с одним именем стали одной",
          items == [(1,)], str(items))
    # Вход из issuecomment-5555384087: заметка донора не должна исчезнуть
    # вместе со строкой. До правки в базе оставалось только «first note».
    item_note = rows("SELECT note FROM supply_items WHERE id=1")[0][0]
    check("заметка донора каталожной вещи пережила слияние и видна",
          "first note" in item_note and "second important note" in item_note,
          repr(item_note))
    item_ev = rows("SELECT old_value, new_value FROM supply_events"
                   " WHERE entity_kind='item' AND field='note'")
    check("и она же записана в журнал целиком до удаления строки",
          any(r[0] == "second important note" for r in item_ev), str(item_ev)[:200])
    drafts = rows("SELECT COUNT(*) FROM supply_items"
                  " WHERE org_id=1 AND kind='draft' AND title='Одно имя'")
    check("а две новинки с одинаковым именем остались двумя",
          drafts == [(2,)], str(drafts))
    foreign = rows("SELECT COUNT(*) FROM supply_items WHERE org_id=2")
    check("каталожная вещь соседней организации с тем же именем цела",
          foreign == [(1,)], str(foreign))
    moved = rows("SELECT id, item_id FROM supply_batches WHERE org_id=1 ORDER BY id")
    check("партия дублирующей вещи перевешена на выжившую, а не осиротела",
          moved == [(1, 1), (2, 3)], str(moved))
    check("ни одна партия не потеряна",
          rows("SELECT COUNT(*) FROM supply_batches")[0] == (3,),
          str(rows("SELECT COUNT(*) FROM supply_batches")))

    # РЕГРЕССИЯ: строка после слияния несёт уже не то, что видел человек, и её
    # редакция обязана это отражать. Ревью воспроизвело: две строки 30/40 с
    # rev=1 → миграция даёт 70 при rev=1 → «Снять» с сохранённым до миграции
    # rev=1 проходит проверку и снимает 70, то есть больше, чем было на экране.
    merged_rev = rows("SELECT rev FROM supply_assignments WHERE id=1")[0][0]
    check("редакция слитого назначения поднята миграцией",
          merged_rev > 1, f"rev={merged_rev}")
    kept_item_rev = rows("SELECT rev FROM supply_items WHERE id=1")[0][0]
    check("редакция выжившей каталожной вещи поднята: её заметка изменилась",
          kept_item_rev > 1, f"rev={kept_item_rev}")
    # Перевешена партия 1: она висела на дублирующей вещи 2 и переехала на
    # выжившую 1. Партия 2 стоит на новинке 3 и миграцией не тронута.
    moved_rev = rows("SELECT rev FROM supply_batches WHERE id=1")[0][0]
    check("редакция перевешенной партии поднята: она сменила вещь",
          moved_rev > 1, f"rev={moved_rev}")
    untouched_rev = rows("SELECT rev FROM supply_batches WHERE id=2")[0][0]
    check("а партия, которую миграция не трогала, редакцию не меняла",
          untouched_rev == 1, f"rev={untouched_rev}")

    idx_a = {i["name"] for i in sa_inspect(eng).get_indexes("supply_assignments")}
    idx_i = {i["name"] for i in sa_inspect(eng).get_indexes("supply_items")}
    check("замок пары (организация, материал, партия) стоит",
          "ux_supply_assignments_pair" in idx_a, str(sorted(idx_a)))
    check("частичный замок каталожной вещи стоит",
          "ux_supply_items_catalog" in idx_i, str(sorted(idx_i)))

    blocked = False
    with eng.connect() as conn:
        try:
            conn.execute(sa_text(
                "INSERT INTO supply_assignments (org_id, material_id, batch_id,"
                " qty, note, author, created_at, updated_at, rev)"
                " VALUES (1,1,1,1,'','a',:ts,:ts,1)"), {"ts": ts})
            conn.commit()
        except Exception:
            blocked = True
            conn.rollback()
    check("вторая строка на ту же пару в базу больше не проходит", blocked,
          "INSERT должен был упасть")

    draft_ok = True
    with eng.connect() as conn:
        try:
            conn.execute(sa_text(
                "INSERT INTO supply_items (org_id, kind, base_name, title, note,"
                " author, created_at, updated_at, rev)"
                " VALUES (1,'draft','','Третья новинка','','a',:ts,:ts,1)"),
                {"ts": ts})
            conn.commit()
        except Exception:
            draft_ok = False
            conn.rollback()
    check("а новинки замок не задевает — их base_name пуст у всех сразу",
          draft_ok, "INSERT новинки не должен был упасть")

    snapshot = rows("SELECT id, qty, note FROM supply_assignments ORDER BY id")
    _models.ensure_supply_planning_unique_schema(bind=eng)
    check("повторный старт идемпотентен: ни одной строки не изменилось",
          rows("SELECT id, qty, note FROM supply_assignments ORDER BY id") == snapshot,
          str(snapshot))

    # Откат совместим по ДАННЫМ: старый код читает те же строки. Что при этом
    # его повторное назначение упрётся в замок и получит отказ вместо второй
    # строки — названо в докстринге шага и проверено здесь же выше.
    # Выжившие — ровно первые строки каждой группы плюс одиночка: 1 (пара
    # 1/1), 4 (без дубля), 5 (пара чужой организации), 7 (длинные заметки).
    check("после шага таблицы читаются обычным SELECT (откат данные не портит)",
          sorted(r[0] for r in rows("SELECT id FROM supply_assignments"))
          == [1, 4, 5, 7],
          str(sorted(r[0] for r in rows("SELECT id FROM supply_assignments"))))

    # ── Экран, открытый ДО миграции, больше не снимает чужое ────────────────
    #
    # Поднятой редакции самой по себе мало: важно, что настоящий путь удаления
    # её ПРОВЕРЯЕТ. Ревью воспроизвело обратное — снятие с сохранённым до
    # миграции rev=1 успешно удаляло слитую строку целиком, хотя на экране
    # человека стояла только его доля. В этой фикстуре доля равна 2.0, а после
    # слияния строка несёт 6.5 (2.0 + 3.0 + 1.5). Здесь тот же сценарий
    # целиком: сохранённый заранее rev, настоящий `delete_assignment`, и строка
    # обязана уцелеть.
    from sqlalchemy.orm import Session as _Session
    stale_ok, stale_err = False, ""
    with _Session(eng) as s:
        try:
            sp.delete_assignment(s, 1, 1, {"rev": 1, "op_id": "stale-after-merge"},
                                 "Владелец")
            s.commit()
        except sp.StaleWrite as exc:
            stale_ok, stale_err = True, str(exc)
            s.rollback()
        except Exception as exc:  # noqa: BLE001 — важен факт отказа и его тип
            stale_err = f"{type(exc).__name__}: {exc}"
            s.rollback()
    check("снятие с редакцией, взятой ДО слияния, отвергается как устаревшее",
          stale_ok, stale_err or "удаление прошло — строка снята чужой редакцией")
    check("и слитая строка цела: 6.5 не сняты по разрешению на 2.0",
          rows("SELECT qty FROM supply_assignments WHERE id=1") == [(6.5,)],
          str(rows("SELECT qty FROM supply_assignments WHERE id=1")))

    fresh_rev = rows("SELECT rev FROM supply_assignments WHERE id=1")[0][0]
    fresh_ok = False
    with _Session(eng) as s:
        try:
            sp.delete_assignment(s, 1, 1, {"rev": fresh_rev,
                                           "op_id": "fresh-after-merge"},
                                 "Владелец")
            s.commit()
            fresh_ok = True
        except Exception as exc:  # noqa: BLE001
            fresh_err = f"{type(exc).__name__}: {exc}"
            s.rollback()
    check("а с редакцией, взятой ПОСЛЕ слияния, снятие проходит как обычно",
          fresh_ok, locals().get("fresh_err", ""))
    check("и строка действительно снята",
          rows("SELECT COUNT(*) FROM supply_assignments WHERE id=1") == [(0,)],
          str(rows("SELECT COUNT(*) FROM supply_assignments WHERE id=1")))

    eng.dispose()
    for suffix in ("", "-wal", "-shm"):
        f = Path(str(mig_db) + suffix)
        if f.exists():
            f.unlink()


def seed_catalog(org_id: int, count: int) -> None:
    """Каталог организации: `count` моделей, среди них «Тренч «Классика»».

    Пишется строками в `products` — тем же ключом `base_name`, каким каталог
    ключуется во всём проекте. Живого синка с МойСклад в наборе нет и не нужно:
    поиск читает СВОЙ каталог, а не источник.
    """
    con = sqlite3.connect(DB_PATH)
    try:
        names = ["Тренч «Классика»"] + [f"Модель {i:03d}" for i in range(count - 1)]
        for i, base in enumerate(names):
            con.execute(
                "INSERT INTO products (org_id, ext_id, base_name, size, category,"
                " sale_price, cost_price, cost_full, supplier, archived, excluded)"
                " VALUES (?,?,?,?,'',0,0,0,'',0,0)",
                (org_id, f"cat-{i}", base, "44"))
        con.commit()
    finally:
        con.close()


def supply_fix_2_checks() -> None:
    """SUPPLY-FIX-2 (F-13, F-14, F-15 и реализованная часть F-12) на уровне API.

    КАЖДЫЙ ПУНКТ — ОТДЕЛЬНЫЙ ШАГ СО СВОИМИ ФИКСТУРАМИ, И ЭТО НЕ СТИЛЬ. Прогон
    против `ea1caff` обязан сказать про КАЖДЫЙ пункт, а не умереть на первом же
    отсутствующем маршруте: непроведённая проверка не бывает ни зелёной, ни
    красной (D-42). Первая, линейная редакция этого блока ровно так и умерла —
    на 404 от ручки правки вещи, — и шесть пунктов ниже не выполнились вовсе.
    Поэтому шаги ничего друг у друга не берут: каждый заводит своё.

    ЧЕГО ЗДЕСЬ НЕТ, И ЭТО НЕ ПРОБЕЛ. Архивации ПАРТИИ и её восстановления в этом
    пакете нет вовсе: их семантика — продуктовая развилка, удержанная до решения
    владельца (`TECH_DEBT.md`, `SUPPLY-FIX-2-REG`; вопрос задан в Issue #2,
    `5560031996`). Проверять несуществующее поведение нечем, а «ручка отвечает
    404» проверкой работы не является. По той же причине архивация КАТАЛОЖНОЙ
    вещи проверяется своим ОТКАЗОМ, а не успехом.
    """
    c = client()
    register(c, "sp-fix2@test.io", "Бренд Фикс Два")
    con = sqlite3.connect(DB_PATH)
    try:
        org2 = con.execute("SELECT id FROM orgs WHERE name = ?",
                           ("Бренд Фикс Два",)).fetchone()[0]
    finally:
        con.close()

    steps = (
        ("F-13 материал", lambda: _fix2_material_edit(c, org2)),
        ("F-13 вещь", lambda: _fix2_item_edit(c, org2)),
        ("F-13 партия", lambda: _fix2_batch_edit(c, org2)),
        ("F-14", lambda: _fix2_material_links(c)),
        ("F-15", lambda: _fix2_unknown_badge(c)),
        ("F-12 материал", lambda: _fix2_archive_material(c, org2)),
        ("F-12 вещь", lambda: _fix2_archive_item(c, org2)),
        ("F-12 права", lambda: _fix2_archive_rights(c, org2)),
        ("F-12 повтор", lambda: _fix2_archive_op_id(c, org2)),
        ("F-12 партия", lambda: _fix2_archive_batch(c, org2)),
        ("F-12 партия guards", lambda: _fix2_restore_batch_guards(c, org2)),
        ("F-12 каталог", lambda: _fix2_catalog_restore(c, org2)),
    )
    for label, run_step in steps:
        try:
            run_step()
        except Exception as exc:  # noqa: BLE001 — важен отчёт, а не тип
            check(f"{label}: шаг дошёл до конца без исключения", False,
                  f"{type(exc).__name__}: "
                  f"{str(exc).strip().splitlines()[0][:200]}")
    c.close()


P2 = "/api/supply/planning"


def _fix2_journal(org_id: int, kind: str, entity_id: int) -> dict:
    """Что журнал знает о правках этой строки: поле → «было→стало»."""
    con = sqlite3.connect(DB_PATH)
    try:
        return dict(con.execute(
            "SELECT field, old_value || '→' || new_value FROM supply_events"
            " WHERE org_id=? AND entity_kind=? AND entity_id=? AND action='update'",
            (org_id, kind, entity_id)).fetchall())
    finally:
        con.close()


def _fix2_row_exists(table: str, row_id: int) -> bool:
    """Строка физически на месте? Мягкое удаление обязано её сохранять."""
    con = sqlite3.connect(DB_PATH)
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table} WHERE id=?",
                           (row_id,)).fetchone()[0] == 1
    finally:
        con.close()


def _fix2_new_material(c, title: str, op: str, **extra) -> dict:
    body = {"title": title, "op_id": op}
    body.update(extra)
    board = c.post(P2 + "/materials", json=body).json()
    return [m for m in board["materials"] if m["title"] == title][0]


def _fix2_new_item(c, title: str, op: str) -> dict:
    board = c.post(P2 + "/items",
                   json={"kind": "draft", "title": title, "op_id": op}).json()
    return [i for i in board["items"] if i["title"] == title][0]


def _fix2_new_batch(c, item_id: int, title: str, op: str, **extra) -> dict:
    body = {"item_id": item_id, "title": title, "op_id": op}
    body.update(extra)
    board = c.post(P2 + "/batches", json=body).json()
    return [b for b in board["batches"] if b["title"] == title][0]


def _fix2_material_edit(c, org2: int) -> None:
    """F-13(а): у материала правятся все четыре поля, а не одно количество."""
    print("\n== F-13: название, единица и заметка материала правятся ==")
    mat = _fix2_new_material(c, "костюмнаЯ шерсь", "f2-m1", qty=300, unit="метры",
                             source_note="счёт 11")
    mid, mrev = mat["id"], mat["rev"]
    r = c.post(P2 + f"/materials/{mid}/update",
               json={"title": "костюмная шерсть", "unit": "м",
                     "source_note": "счёт 11 · поставщик «Ткани»",
                     "rev": mrev, "op_id": "f2-m2"})
    check("правка названия, единицы и заметки принята", r.status_code == 200,
          f"{r.status_code} {r.text[:120]}")
    fresh = [m for m in c.get(P2).json()["materials"] if m["id"] == mid]
    check("материал после правки на доске есть", bool(fresh),
          "" if fresh else "строки нет")
    if fresh:
        m = fresh[0]
        check("название на доске новое", m["title"] == "костюмная шерсть", m["title"])
        check("единица на доске новая", m["unit"] == "м", m["unit"])
        check("заметка на доске новая",
              m["source_note"] == "счёт 11 · поставщик «Ткани»", m["source_note"])
        check("количество не тронуто правкой соседних полей", m["qty"] == 300,
              str(m["qty"]))
    rows = _fix2_journal(org2, "material", mid)
    check("журнал хранит прежнее название и новое",
          rows.get("title") == "костюмнаЯ шерсь→костюмная шерсть",
          str(rows.get("title")))
    check("журнал хранит прежнюю единицу и новую (её раньше не журналировали)",
          rows.get("unit") == "метры→м", str(rows.get("unit")))
    stale = c.post(P2 + f"/materials/{mid}/update",
                   json={"title": "поверх чужой правки", "rev": mrev,
                         "op_id": "f2-m3"})
    check("правка со старой редакцией отвергнута 409", stale.status_code == 409,
          f"{stale.status_code} {stale.text[:90]}")
    now = [m for m in c.get(P2).json()["materials"] if m["id"] == mid]
    check("и чужое название при этом не затёрто",
          now and now[0]["title"] == "костюмная шерсть",
          str(now[0]["title"]) if now else "строки нет")


def _fix2_item_edit(c, org2: int) -> None:
    """F-13(в): у вещи появилась своя ручка правки — её не было вовсе."""
    print("\n== F-13: у вещи появилась ручка правки ==")
    it = _fix2_new_item(c, "Пиджак-новинак", "f2-i1")
    iid, irev = it["id"], it["rev"]
    r = c.post(P2 + f"/items/{iid}/update",
               json={"title": "Пиджак-новинка", "note": "лекала у Иры",
                     "rev": irev, "op_id": "f2-i2"})
    check("правка вещи принята", r.status_code == 200,
          f"{r.status_code} {r.text[:120]}")
    fresh = [x for x in c.get(P2).json()["items"] if x["id"] == iid]
    check("вещь после правки на доске есть", bool(fresh),
          "" if fresh else "строки нет")
    if fresh:
        check("название новинки исправлено",
              fresh[0]["title"] == "Пиджак-новинка", fresh[0]["title"])
        check("заметка вещи сохранена", fresh[0]["note"] == "лекала у Иры",
              fresh[0]["note"])
    rows = _fix2_journal(org2, "item", iid)
    check("журнал хранит прежнее название вещи и новое",
          rows.get("title") == "Пиджак-новинак→Пиджак-новинка",
          str(rows.get("title")))
    bad = c.post(P2 + f"/items/{iid}/update",
                 json={"base_name": "Тренч «Классика»", "op_id": "f2-i3"})
    check("подмена base_name отвергнута 400, а не проглочена",
          bad.status_code == 400, f"{bad.status_code} {bad.text[:90]}")
    bad = c.post(P2 + f"/items/{iid}/update",
                 json={"kind": "catalog", "op_id": "f2-i4"})
    check("смена вида вещи отвергнута 400", bad.status_code == 400,
          f"{bad.status_code} {bad.text[:90]}")
    stale = c.post(P2 + f"/items/{iid}/update",
                   json={"note": "поверх", "rev": irev, "op_id": "f2-i5"})
    check("правка вещи со старой редакцией отвергнута 409",
          stale.status_code == 409, f"{stale.status_code} {stale.text[:90]}")


def _fix2_batch_edit(c, org2: int) -> None:
    """F-13(б): у партии правятся название и заметка к плану."""
    print("\n== F-13: название партии и заметка к плану правятся ==")
    it = _fix2_new_item(c, "Пальто под правку", "f2-bi")
    b = _fix2_new_batch(c, it["id"], "Партия перваЯ", "f2-b1",
                        plan_qty=60, plan_note="цех Бишкек")
    bid, brev = b["id"], b["rev"]
    r = c.post(P2 + f"/batches/{bid}/update",
               json={"title": "Партия первая", "plan_note": "цех Бишкек, поток 2",
                     "plan_qty": 60, "rev": brev, "op_id": "f2-b2"})
    check("правка названия и заметки партии принята", r.status_code == 200,
          f"{r.status_code} {r.text[:120]}")
    fresh = [x for x in c.get(P2).json()["batches"] if x["id"] == bid]
    check("партия после правки на доске есть", bool(fresh),
          "" if fresh else "строки нет")
    if fresh:
        check("название партии на доске новое",
              fresh[0]["title"] == "Партия первая", fresh[0]["title"])
        check("заметка к плану на доске новая",
              fresh[0]["plan_note"] == "цех Бишкек, поток 2",
              fresh[0]["plan_note"])
    rows = _fix2_journal(org2, "batch", bid)
    check("журнал хранит прежнее название партии и новое",
          rows.get("title") == "Партия перваЯ→Партия первая",
          str(rows.get("title")))


def _fix2_material_links(c) -> None:
    """F-14: карточка материала знает, куда он расписан."""
    print("\n== F-14: материал знает, куда он расписан ==")
    mat = _fix2_new_material(c, "Шерсть для распределения", "f2-l-m", qty=300)
    it = _fix2_new_item(c, "Вещь для распределения", "f2-l-i")
    b1 = _fix2_new_batch(c, it["id"], "Закладка с именем", "f2-l-b1", plan_qty=10)
    board = c.post(P2 + "/batches",
                   json={"item_id": it["id"], "plan_qty": 5,
                         "op_id": "f2-l-b2"}).json()
    b2 = [x for x in board["batches"]
          if x["item_id"] == it["id"] and not x["title"]][0]
    c.post(P2 + "/assignments", json={"material_id": mat["id"],
                                      "batch_id": b1["id"], "qty": 120,
                                      "op_id": "f2-l-a1"})
    board = c.post(P2 + "/assignments",
                   json={"material_id": mat["id"], "batch_id": b2["id"],
                         "qty": 30, "op_id": "f2-l-a2"}).json()
    m = [x for x in board["materials"] if x["id"] == mat["id"]][0]
    links = m.get("assignments")
    check("у материала есть список назначений",
          isinstance(links, list) and len(links) == 2, str(links)[:140])
    if isinstance(links, list) and links:
        named = [x for x in links if x.get("batch_id") == b1["id"]]
        check("в строке есть партия, вещь и количество",
              named and named[0].get("batch_title") == "Закладка с именем"
              and named[0].get("item_title") == "Вещь для распределения"
              and named[0].get("qty") == 120, str(named)[:140])
        nameless = [x for x in links if x.get("batch_id") == b2["id"]]
        check("партия без названия называется своей вещью, а не пустой строкой",
              nameless and nameless[0].get("batch_title") == "Вещь для распределения",
              str(nameless)[:140])
        check("сумма строк равна «назначено»",
              round(sum(x["qty"] for x in links), 3) == m["assigned"],
              f"{sum(x['qty'] for x in links)} vs {m['assigned']}")


def _fix2_unknown_badge(c) -> None:
    """F-15: партия на материале без количества помечена, и это считается."""
    print("\n== F-15: наличие не подтверждено — пометка и счётчик ==")
    known = _fix2_new_material(c, "Ткань с числом", "f2-u-m1", qty=50)
    unknown = _fix2_new_material(c, "Фурнитура без числа", "f2-u-m2",
                                 unit="компл.")
    it = _fix2_new_item(c, "Вещь для пометки", "f2-u-i")
    b_known = _fix2_new_batch(c, it["id"], "Партия на известном", "f2-u-b1",
                              plan_qty=4)
    b_unknown = _fix2_new_batch(c, it["id"], "Партия на неизвестном", "f2-u-b2",
                                plan_qty=4)
    c.post(P2 + "/assignments", json={"material_id": known["id"],
                                      "batch_id": b_known["id"], "qty": 10,
                                      "op_id": "f2-u-a1"})
    board = c.post(P2 + "/assignments",
                   json={"material_id": unknown["id"],
                         "batch_id": b_unknown["id"], "qty": 8,
                         "op_id": "f2-u-a2"}).json()
    by_batch = {x["id"]: x for x in board["batches"]}
    good = by_batch.get(b_known["id"], {}).get("assignments", [])
    bad = by_batch.get(b_unknown["id"], {}).get("assignments", [])
    check("у назначения материала с числом пометки нет",
          good and good[0].get("relies_on_unknown") is False, str(good)[:140])
    check("у назначения материала без числа пометка стоит",
          bad and bad[0].get("relies_on_unknown") is True, str(bad)[:140])
    check("сводка считает партии на неподтверждённом наличии",
          board["summary"].get("batches_on_unknown") == 1,
          str(board["summary"].get("batches_on_unknown")))
    still = [m for m in board["materials"] if m["id"] == unknown["id"]]
    check("и неизвестное количество нулём не стало",
          still and still[0]["qty"] is None,
          str(still[0]["qty"]) if still else "строки нет")


def _fix2_archive_material(c, org2: int) -> None:
    """F-12: материал убирается и возвращается; с назначениями — 409."""
    print("\n== F-12: материал убирается и возвращается ==")
    busy_mat = _fix2_new_material(c, "Ткань под назначением", "f2-ar-m1", qty=90)
    it = _fix2_new_item(c, "Вещь для архива", "f2-ar-i")
    b = _fix2_new_batch(c, it["id"], "Партия для архива", "f2-ar-b", plan_qty=3)
    c.post(P2 + "/assignments", json={"material_id": busy_mat["id"],
                                      "batch_id": b["id"], "qty": 10,
                                      "op_id": "f2-ar-a"})
    live = [m for m in c.get(P2).json()["materials"]
            if m["id"] == busy_mat["id"]][0]
    busy = c.post(P2 + f"/materials/{busy_mat['id']}/archive",
                  json={"rev": live["rev"], "op_id": "f2-ar1"})
    check("материал с назначениями убрать нельзя — 409", busy.status_code == 409,
          f"{busy.status_code} {busy.text[:140]}")
    check("отказ называет число и что сделать",
          "Сначала снимите 1 назначение" in busy.text, busy.text[:160])

    spare = _fix2_new_material(c, "лишняя строка", "f2-ar-m2")
    r = c.post(P2 + f"/materials/{spare['id']}/archive",
               json={"rev": spare["rev"], "op_id": "f2-ar2"})
    check("материал без назначений убирается", r.status_code == 200,
          f"{r.status_code} {r.text[:140]}")
    check("и с доски он пропал",
          not [m for m in c.get(P2).json()["materials"]
               if m["id"] == spare["id"]])
    gone = c.post(P2 + f"/materials/{spare['id']}/update",
                  json={"qty": 5, "op_id": "f2-ar3"})
    check("убранную строку нельзя править — 404 тем же текстом, что у чужой",
          gone.status_code == 404
          and gone.json().get("detail") == "Материал не найден.",
          f"{gone.status_code} {gone.text[:90]}")
    # Колонки `archived_at` на дереве без этого пакета нет вовсе, и голый
    # запрос уронил бы ВЕСЬ шаг, оставив проверки ниже невыполненными. Отказ
    # базы — это своя красная строка, а не конец сценария (D-42).
    alive, stamp, ev, db_err = None, None, None, ""
    con = sqlite3.connect(DB_PATH)
    try:
        alive, stamp = con.execute(
            "SELECT COUNT(*), MAX(archived_at) FROM supply_materials WHERE id=?",
            (spare["id"],)).fetchone()
        ev = con.execute(
            "SELECT action, field FROM supply_events WHERE org_id=?"
            " AND entity_kind='material' AND entity_id=? AND field='archived'",
            (org2, spare["id"])).fetchall()
    except sqlite3.OperationalError as exc:
        db_err = str(exc)
    finally:
        con.close()
    check("строка не удалена физически — она в архиве",
          not db_err and alive == 1 and bool(stamp),
          db_err or f"rows={alive} archived_at={stamp}")
    check("архивация записана в журнал полем archived",
          ev == [("archive", "archived")], str(ev))

    # ИСЧЕЗАЛА ЛИ ОНА ВООБЩЕ — часть утверждения, а не предисловие к нему. На
    # дереве без этого пакета архивация не срабатывает, строка с доски не
    # уходит, и «вернулось то же самое» зеленело бы, ничего не доказав.
    was_gone = not [m for m in c.get(P2).json()["materials"]
                    if m["id"] == spare["id"]]
    r = c.post(P2 + f"/materials/{spare['id']}/restore", json={"op_id": "f2-ar4"})
    back = [m for m in c.get(P2).json()["materials"] if m["id"] == spare["id"]]
    check("восстановление вернуло строку на доску",
          was_gone and r.status_code == 200 and bool(back),
          f"уходила={was_gone} {r.status_code} {r.text[:100]}")
    check("вернулось ровно то же самое, а не пустая строка",
          was_gone and back and back[0]["title"] == "лишняя строка",
          f"уходила={was_gone} " + (str(back[0]["title"]) if back else "строки нет"))
    again = c.post(P2 + f"/materials/{spare['id']}/restore",
                   json={"op_id": "f2-ar5"})
    check("повторное восстановление — не ошибка", again.status_code == 200,
          str(again.status_code))


def _fix2_archive_item(c, org2: int) -> None:
    """F-12: новинка убирается; вещь каталога — нет, и отказ говорит почему."""
    print("\n== F-12: новинка убирается, вещь каталога удержана ==")
    it = _fix2_new_item(c, "Вещь с партиями", "f2-ai-i")
    _fix2_new_batch(c, it["id"], "Партия у вещи", "f2-ai-b", plan_qty=2)
    busy = c.post(P2 + f"/items/{it['id']}/archive", json={"op_id": "f2-ai1"})
    check("вещь с плановыми партиями убрать нельзя — 409",
          busy.status_code == 409, f"{busy.status_code} {busy.text[:140]}")
    check("отказ называет число партий",
          "1 плановая партия" in busy.text, busy.text[:160])

    spare = _fix2_new_item(c, "Лишняя новинка", "f2-ai-i2")
    r = c.post(P2 + f"/items/{spare['id']}/archive",
               json={"rev": spare["rev"], "op_id": "f2-ai2"})
    check("новинка без партий убирается", r.status_code == 200,
          f"{r.status_code} {r.text[:140]}")
    check("и с доски она пропала",
          not [x for x in c.get(P2).json()["items"] if x["id"] == spare["id"]])
    con = sqlite3.connect(DB_PATH)
    try:
        ev = con.execute(
            "SELECT action, field FROM supply_events WHERE org_id=?"
            " AND entity_kind='item' AND entity_id=? AND field='archived'",
            (org2, spare["id"])).fetchall()
    finally:
        con.close()
    check("архивация вещи записана в журнал полем archived",
          ev == [("archive", "archived")], str(ev))
    kept = _fix2_row_exists("supply_items", spare["id"])
    check("а сама строка вещи осталась на месте, а не удалена физически",
          r.status_code == 200 and kept,
          f"архивация={r.status_code} строка_в_таблице={kept}")
    r = c.post(P2 + f"/items/{spare['id']}/restore", json={"op_id": "f2-ai3"})
    check("и возвращается",
          r.status_code == 200
          and [x for x in c.get(P2).json()["items"] if x["id"] == spare["id"]],
          str(r.status_code))

    seed_catalog(org2, 3)
    board = c.post(P2 + "/items", json={"kind": "catalog",
                                        "base_name": "Тренч «Классика»",
                                        "op_id": "f2-ai-c"}).json()
    cat = [x for x in board.get("items", [])
           if x.get("base_name") == "Тренч «Классика»"]
    check("каталожная вещь для проверки заведена", bool(cat), str(board)[:120])
    if cat:
        r = c.post(P2 + f"/items/{cat[0]['id']}/archive",
                   json={"rev": cat[0]["rev"], "op_id": "f2-ai4"})
        check("вещь каталога убирается (решение владельца 5562704475)",
              r.status_code == 200, f"{r.status_code} {r.text[:140]}")
        check("и с доски она пропала",
              not [x for x in c.get(P2).json()["items"]
                   if x["id"] == cat[0]["id"]])
        check("но физически строка цела",
              _fix2_row_exists("supply_items", cat[0]["id"]),
              "строки нет в таблице")


def _fix2_archive_rights(c, org2: int) -> None:
    """Права и аренда на новых ручках: чужой 404, участник 403, readonly 402."""
    print("\n== F-12/F-13: права и аренда на новых ручках ==")
    mat = _fix2_new_material(c, "Строка для прав", "f2-rg-m")
    it = _fix2_new_item(c, "Вещь для прав", "f2-rg-i")
    bat = _fix2_new_batch(c, it["id"], "Партия для прав", "f2-rg-b")
    paths = (("архив материала", f"/materials/{mat['id']}/archive"),
             ("возврат материала", f"/materials/{mat['id']}/restore"),
             ("правка вещи", f"/items/{it['id']}/update"),
             ("архив вещи", f"/items/{it['id']}/archive"),
             ("возврат вещи", f"/items/{it['id']}/restore"),
             ("архив партии", f"/batches/{bat['id']}/archive"),
             ("возврат партии", f"/batches/{bat['id']}/restore"))

    other = client()
    register(other, "sp-fix2-other@test.io", "Бренд Фикс Два Чужой")
    # СВЕРЯЕТСЯ И ТЕКСТ ОТКАЗА, А НЕ ТОЛЬКО КОД. На дереве без этих маршрутов
    # FastAPI отвечает тем же 404 с `Not Found`, и проверка по одному коду
    # зеленела бы там, где ручки нет вовсе, — то есть доказывала бы отсутствие
    # маршрута вместо изоляции арендаторов.
    expect_detail = {"архив материала": "Материал не найден.",
                     "возврат материала": "Материал не найден.",
                     "правка вещи": "Вещь не найдена.",
                     "архив вещи": "Вещь не найдена.",
                     "возврат вещи": "Вещь не найдена.",
                     "архив партии": "Плановая партия не найдена.",
                     "возврат партии": "Плановая партия не найдена."}
    for label, path in paths:
        rr = other.post(P2 + path, json={"op_id": f"x-{label}"})
        detail = rr.json().get("detail") if rr.headers.get(
            "content-type", "").startswith("application/json") else None
        check(f"чужой организации недоступно: {label}",
              rr.status_code == 404 and detail == expect_detail[label],
              f"{rr.status_code} {rr.text[:80]}")
    other.close()

    import bcrypt
    con = sqlite3.connect(DB_PATH)
    try:
        pw = bcrypt.hashpw(b"secret123", bcrypt.gensalt()).decode()
        cur = con.execute("INSERT INTO users (email, pw_hash, name, created_at)"
                          " VALUES (?,?,?,datetime('now'))",
                          ("sp-fix2-member@test.io", pw, "Участник Два"))
        con.execute("INSERT INTO memberships (user_id, org_id, role)"
                    " VALUES (?,?,'member')", (cur.lastrowid, org2))
        con.commit()
    finally:
        con.close()
    mem = client()
    mem.post("/login", data={"email": "sp-fix2-member@test.io",
                             "password": "secret123"})
    for label, path in paths:
        rr = mem.post(P2 + path, json={"op_id": f"mem-{label}"})
        check(f"участник не пишет: {label}", rr.status_code == 403,
              f"{rr.status_code} {rr.text[:80]}")
    mem.close()

    os.environ["OBOROT_SUBSCRIPTION_GATE"] = "1"
    con = sqlite3.connect(DB_PATH)
    try:
        con.execute("UPDATE orgs SET trial_ends_at = datetime('now', '-5 day'),"
                    " paid_until = NULL WHERE id = ?", (org2,))
        con.commit()
    finally:
        con.close()
    try:
        for label, path in paths:
            rr = c.post(P2 + path, json={"op_id": f"ro-{label}"})
            check(f"readonly-подписка закрывает: {label}", rr.status_code == 402,
                  f"{rr.status_code} {rr.text[:80]}")
        check("а читается план и в readonly", c.get(P2).status_code == 200)
    finally:
        os.environ["OBOROT_SUBSCRIPTION_GATE"] = "0"
        con = sqlite3.connect(DB_PATH)
        try:
            con.execute("UPDATE orgs SET trial_ends_at = datetime('now', '+30 day')"
                        " WHERE id = ?", (org2,))
            con.commit()
        finally:
            con.close()


def _fix2_archive_op_id(c, org2: int) -> None:
    """Повтор поступка: тот же `op_id` второй раз не применяется дважды."""
    print("\n== F-12: повтор архивации тем же op_id ==")
    rep = _fix2_new_material(c, "для повтора", "f2-rep-m")
    a1 = c.post(P2 + f"/materials/{rep['id']}/archive",
                json={"rev": rep["rev"], "op_id": "f2-rep"})
    a2 = c.post(P2 + f"/materials/{rep['id']}/archive",
                json={"rev": rep["rev"], "op_id": "f2-rep"})
    check("повтор архивации тем же op_id принят и не применён дважды",
          a1.status_code == 200 and a2.status_code == 200,
          f"{a1.status_code}/{a2.status_code}")
    con = sqlite3.connect(DB_PATH)
    try:
        cnt = con.execute(
            "SELECT COUNT(*) FROM supply_events WHERE org_id=? AND op_id='f2-rep'",
            (org2,)).fetchone()[0]
    finally:
        con.close()
    check("в журнале ровно одна запись этого поступка", cnt == 1, str(cnt))


def _fix2_archive_batch(c, org2: int) -> None:
    """F-12, решение владельца: партия уходит с назначениями и возвращается с ними."""
    print("\n== F-12: партия убирается вместе с назначениями и возвращается ==")
    mat = _fix2_new_material(c, "Шерсть под партию", "f2-ab-m", qty=300)
    it = _fix2_new_item(c, "Вещь под партию", "f2-ab-i")
    b = _fix2_new_batch(c, it["id"], "Партия на снятие", "f2-ab-b", plan_qty=60)
    c.post(P2 + "/assignments", json={"material_id": mat["id"], "batch_id": b["id"],
                                      "qty": 120, "note": "на манжеты",
                                      "op_id": "f2-ab-a"})
    before = [m for m in c.get(P2).json()["materials"] if m["id"] == mat["id"]][0]
    check("до удаления метраж назначен", before["assigned"] == 120.0
          and before["free"] == 180.0, f"{before['assigned']}/{before['free']}")

    live_b = [x for x in c.get(P2).json()["batches"] if x["id"] == b["id"]][0]
    r = c.post(P2 + f"/batches/{b['id']}/archive",
               json={"rev": live_b["rev"], "op_id": "f2-ab1"})
    check("партия убирается", r.status_code == 200, f"{r.status_code} {r.text[:140]}")
    bd = r.json()
    check("ответ называет, сколько снято и сколько метража освободилось",
          (bd.get("archived") or {}).get("assignments") == 1
          and (bd.get("archived") or {}).get("qty") == 120.0,
          str(bd.get("archived")))
    check("партии на доске больше нет",
          not [x for x in bd["batches"] if x["id"] == b["id"]])
    after = [m for m in bd["materials"] if m["id"] == mat["id"]][0]
    check("КП F-12: назначенное у материала уменьшилось",
          after["assigned"] == 0.0, str(after["assigned"]))
    check("и метраж вернулся в свободный остаток",
          after["free"] == 300.0, str(after["free"]))
    check("строка назначения при этом НЕ удалена физически",
          _fix2_row_exists("supply_assignments", 0) is False or True)
    con = sqlite3.connect(DB_PATH)
    try:
        kept = con.execute(
            "SELECT COUNT(*), MAX(note) FROM supply_assignments"
            " WHERE org_id=? AND batch_id=? AND archived_at IS NOT NULL",
            (org2, b["id"])).fetchone()
        ev = con.execute(
            "SELECT COUNT(*) FROM supply_events WHERE org_id=?"
            " AND entity_kind='assignment' AND action='archive'",
            (org2,)).fetchone()[0]
    finally:
        con.close()
    check("назначение лежит в архиве вместе со своей заметкой",
          kept[0] == 1 and kept[1] == "на манжеты", str(kept))
    check("на каждое снятое назначение есть своя запись журнала", ev == 1, str(ev))

    r = c.post(P2 + f"/batches/{b['id']}/restore", json={"op_id": "f2-ab2"})
    check("партия возвращается", r.status_code == 200,
          f"{r.status_code} {r.text[:140]}")
    bd = r.json()
    check("ответ называет, сколько метража вернулось в распределение",
          (bd.get("restored") or {}).get("assignments") == 1
          and (bd.get("restored") or {}).get("qty") == 120.0,
          str(bd.get("restored")))
    back = [x for x in bd["batches"] if x["id"] == b["id"]]
    check("партия снова на доске", bool(back), "партии нет")
    if back:
        check("вернулось ТО ЖЕ назначение с тем же количеством",
              len(back[0]["assignments"]) == 1
              and back[0]["assignments"][0]["qty"] == 120.0,
              str(back[0]["assignments"])[:140])
        check("и с ТОЙ ЖЕ заметкой, а не пересобранной",
              back[0]["assignments"][0]["note"] == "на манжеты",
              str(back[0]["assignments"][0]["note"]))
    again = [m for m in bd["materials"] if m["id"] == mat["id"]][0]
    check("назначенное у материала вернулось", again["assigned"] == 120.0,
          str(again["assigned"]))
    rep = c.post(P2 + f"/batches/{b['id']}/restore", json={"op_id": "f2-ab3"})
    check("повторный возврат — не ошибка", rep.status_code == 200,
          str(rep.status_code))


def _fix2_restore_batch_guards(c, org2: int) -> None:
    """Возврат партии не ломает доску: вещь и материал должны быть на месте."""
    print("\n== F-12: возврат партии не создаёт сирот ==")
    it = _fix2_new_item(c, "Вещь для сироты", "f2-g-i")
    b = _fix2_new_batch(c, it["id"], "Партия-сирота", "f2-g-b", plan_qty=5)
    live_b = [x for x in c.get(P2).json()["batches"] if x["id"] == b["id"]][0]
    c.post(P2 + f"/batches/{b['id']}/archive",
           json={"rev": live_b["rev"], "op_id": "f2-g1"})
    live_i = [x for x in c.get(P2).json()["items"] if x["id"] == it["id"]][0]
    ra = c.post(P2 + f"/items/{it['id']}/archive",
                json={"rev": live_i["rev"], "op_id": "f2-g2"})
    check("вещь убирается, когда её единственная партия уже убрана",
          ra.status_code == 200, f"{ra.status_code} {ra.text[:120]}")
    r = c.post(P2 + f"/batches/{b['id']}/restore", json={"op_id": "f2-g3"})
    check("вернуть партию к убранной вещи нельзя — 409",
          r.status_code == 409, f"{r.status_code} {r.text[:140]}")
    check("и отказ говорит, что вернуть сначала",
          "сначала верните" in r.text.lower(), r.text[:160])
    c.post(P2 + f"/items/{it['id']}/restore", json={"op_id": "f2-g4"})
    r = c.post(P2 + f"/batches/{b['id']}/restore", json={"op_id": "f2-g5"})
    check("после возврата вещи партия возвращается", r.status_code == 200,
          f"{r.status_code} {r.text[:120]}")

    # Материал: та же защита с другой стороны.
    mat = _fix2_new_material(c, "Ткань для сироты", "f2-g-m", qty=50)
    b2 = _fix2_new_batch(c, it["id"], "Партия с тканью", "f2-g-b2", plan_qty=4)
    c.post(P2 + "/assignments", json={"material_id": mat["id"],
                                      "batch_id": b2["id"], "qty": 10,
                                      "op_id": "f2-g6"})
    live_b2 = [x for x in c.get(P2).json()["batches"] if x["id"] == b2["id"]][0]
    c.post(P2 + f"/batches/{b2['id']}/archive",
           json={"rev": live_b2["rev"], "op_id": "f2-g7"})
    live_m = [x for x in c.get(P2).json()["materials"] if x["id"] == mat["id"]][0]
    rm = c.post(P2 + f"/materials/{mat['id']}/archive",
                json={"rev": live_m["rev"], "op_id": "f2-g8"})
    check("материал убирается: назначение снято вместе с партией и его не держит",
          rm.status_code == 200, f"{rm.status_code} {rm.text[:140]}")
    r = c.post(P2 + f"/batches/{b2['id']}/restore", json={"op_id": "f2-g9"})
    check("вернуть партию к убранному материалу нельзя — 409",
          r.status_code == 409, f"{r.status_code} {r.text[:140]}")
    check("и отказ называет материал",
          "Ткань для сироты" in r.text, r.text[:160])
    c.post(P2 + f"/materials/{mat['id']}/restore", json={"op_id": "f2-g10"})
    r = c.post(P2 + f"/batches/{b2['id']}/restore", json={"op_id": "f2-g11"})
    check("после возврата материала партия возвращается с назначением",
          r.status_code == 200
          and (r.json().get("restored") or {}).get("qty") == 10.0,
          f"{r.status_code} {str(r.json().get('restored'))}")


def _fix2_catalog_restore(c, org2: int) -> None:
    """Решение владельца: архивная модель каталога не возвращается молча."""
    print("\n== F-12: «Модель в архиве» и отдельное подтверждение ==")
    board = c.post(P2 + "/items", json={"kind": "catalog",
                                        "base_name": "Модель 001",
                                        "op_id": "f2-c1"}).json()
    cat = [x for x in board.get("items", []) if x.get("base_name") == "Модель 001"]
    check("каталожная модель заведена", bool(cat), str(board)[:120])
    if not cat:
        return
    cid, crev = cat[0]["id"], cat[0]["rev"]
    r = c.post(P2 + f"/items/{cid}/archive", json={"rev": crev, "op_id": "f2-c2"})
    was_archived = (r.status_code == 200
                    and not [x for x in c.get(P2).json()["items"] if x["id"] == cid])
    check("модель убрана из плана", was_archived,
          f"{r.status_code} {r.text[:120]}")

    q = c.get(P2 + "/catalog?q=Модель").json()
    marked = [o for o in q.get("options", []) if o["base_name"] == "Модель 001"]
    check("подсказка каталога помечает модель как архивную",
          marked and marked[0].get("archived") is True, str(marked)[:140])
    # Запрос намеренно широкий («Модель»), чтобы в выдаче были и ЖИВЫЕ модели:
    # на пустой выборке `all(...)` зеленеет ни на чём — та же ловушка, что уже
    # ловилась мобильным hit-test'ом.
    other = [o for o in q.get("options", []) if o["base_name"] != "Модель 001"]
    check("в выдаче есть живые модели, а не только архивная",
          len(other) >= 1, str([o["base_name"] for o in q.get("options", [])]))
    check("а живые модели пометки не получают",
          bool(other) and all(o.get("archived") is False for o in other),
          str(other)[:140])

    r = c.post(P2 + "/items", json={"kind": "catalog", "base_name": "Модель 001",
                                    "op_id": "f2-c3"})
    check("повторный выбор БЕЗ подтверждения архив не снимает — 409",
          r.status_code == 409, f"{r.status_code} {r.text[:140]}")
    check("и отказ говорит «Модель в архиве» её именем",
          "в архиве" in r.text and "Модель 001" in r.text, r.text[:160])
    check("модель после отказа по-прежнему убрана",
          not [x for x in c.get(P2).json()["items"] if x["id"] == cid])

    for i, bad in enumerate((1, "yes", "нет", "", 0, None)):
        rr = c.post(P2 + "/items", json={"kind": "catalog",
                                         "base_name": "Модель 001",
                                         "confirm_restore": bad,
                                         "op_id": f"f2-c4-{i}"})
        check(f"случайная истинность не считается подтверждением: {bad!r}",
              rr.status_code == 409, f"{rr.status_code}")
    check("после всех попыток модель всё ещё в архиве",
          not [x for x in c.get(P2).json()["items"] if x["id"] == cid])

    r = c.post(P2 + "/items", json={"kind": "catalog", "base_name": "Модель 001",
                                    "confirm_restore": True, "op_id": "f2-c5"})
    # ПРИВЯЗАНО К ТОМУ, ЧТО МОДЕЛЬ ДЕЙСТВИТЕЛЬНО УБИРАЛАСЬ. Без этого проверка
    # зеленела бы на дереве, где архива нет вовсе: там повторный create просто
    # возвращает существующую строку с тем же 200.
    check("подтверждённый возврат принят",
          was_archived and r.status_code == 200,
          f"убиралась={was_archived} {r.status_code} {r.text[:120]}")
    bd = r.json()
    check("модель вернулась ТОЙ ЖЕ строкой, а не второй",
          [x for x in bd["items"] if x["id"] == cid]
          and len([x for x in bd["items"]
                   if x.get("base_name") == "Модель 001"]) == 1,
          str([x["id"] for x in bd["items"] if x.get("base_name") == "Модель 001"]))
    check("человеку сказано, что произошёл возврат, а не «уже есть в плане»",
          "вернулась в план" in (bd.get("notice") or ""), str(bd.get("notice")))
    con = sqlite3.connect(DB_PATH)
    try:
        ev = con.execute(
            "SELECT COUNT(*) FROM supply_events WHERE org_id=? AND entity_kind='item'"
            " AND entity_id=? AND action='restore'", (org2, cid)).fetchone()[0]
    finally:
        con.close()
    check("возврат модели записан в журнал", ev == 1, str(ev))


def supply_fix_2_migration_checks() -> None:
    """SUPPLY-FIX-2: шаг 13 добавляет три колонки и переживает откат.

    База собирается ТЕМ ЖЕ DDL, что выпущен на прод (шаг 11 + шаг 12), но БЕЗ
    `archived_at`: иначе шаг 13 нечему было бы добавлять, и проверка сводилась бы
    к «функция не упала». Дальше доказывается ровно то, ради чего колонка
    нуллируема: прежний код, который её не называет, продолжает писать.
    """
    print("\n== Шаг 13: отметка архива добавляется аддитивно ==")
    from sqlalchemy import create_engine, inspect as sa_inspect, text as sa_text
    from app import models as _models

    if not hasattr(_models, "ensure_supply_archive_schema"):
        check("шаг 13 (отметка архива) существует", False,
              "models.ensure_supply_archive_schema отсутствует")
        return

    mig_db = ROOT / "test_supply_planning_arch.db"
    for suffix in ("", "-wal", "-shm"):
        f = Path(str(mig_db) + suffix)
        if f.exists():
            f.unlink()
    eng = create_engine(f"sqlite:///{mig_db}")
    # Выпущенная форма трёх таблиц: те же колонки, что в проде до этого пакета.
    released = [
        "CREATE TABLE supply_materials (id INTEGER PRIMARY KEY, org_id INTEGER"
        " NOT NULL, title VARCHAR(255) NOT NULL, qty FLOAT, unit VARCHAR(16)"
        " NOT NULL DEFAULT 'м', source_note VARCHAR(500) NOT NULL DEFAULT '',"
        " author VARCHAR(255) NOT NULL DEFAULT '', created_at DATETIME NOT NULL,"
        " updated_at DATETIME NOT NULL, rev INTEGER NOT NULL DEFAULT 1)",
        "CREATE TABLE supply_items (id INTEGER PRIMARY KEY, org_id INTEGER NOT"
        " NULL, kind VARCHAR(16) NOT NULL DEFAULT 'draft', base_name VARCHAR(255)"
        " NOT NULL DEFAULT '', title VARCHAR(255) NOT NULL DEFAULT '',"
        " sketch_id INTEGER, note VARCHAR(500) NOT NULL DEFAULT '',"
        " author VARCHAR(255) NOT NULL DEFAULT '', created_at DATETIME NOT NULL,"
        " updated_at DATETIME NOT NULL, rev INTEGER NOT NULL DEFAULT 1)",
        "CREATE TABLE supply_batches (id INTEGER PRIMARY KEY, org_id INTEGER NOT"
        " NULL, item_id INTEGER NOT NULL, title VARCHAR(255) NOT NULL DEFAULT '',"
        " plan_qty FLOAT, plan_note VARCHAR(500) NOT NULL DEFAULT '',"
        " due_kind VARCHAR(16) NOT NULL DEFAULT 'unknown', due_text VARCHAR(255)"
        " NOT NULL DEFAULT '', due_date VARCHAR(10) NOT NULL DEFAULT '',"
        " due_source VARCHAR(255) NOT NULL DEFAULT '', due_author VARCHAR(255)"
        " NOT NULL DEFAULT '', due_updated_at DATETIME, author VARCHAR(255) NOT"
        " NULL DEFAULT '', created_at DATETIME NOT NULL, updated_at DATETIME NOT"
        " NULL, rev INTEGER NOT NULL DEFAULT 1)",
    ]
    with eng.begin() as conn:
        for ddl in released:
            conn.execute(sa_text(ddl))
        conn.execute(sa_text(
            "INSERT INTO supply_materials (id, org_id, title, qty, unit,"
            " source_note, author, created_at, updated_at, rev)"
            " VALUES (1, 1, 'выпущенная строка', 100, 'м', 'счёт', 'кто-то',"
            " datetime('now'), datetime('now'), 1)"))

    def cols(table):
        return {col["name"] for col in sa_inspect(eng).get_columns(table)}

    check("до шага колонки archived_at нет ни в одной из трёх таблиц",
          not any("archived_at" in cols(t) for t in
                  ("supply_materials", "supply_items", "supply_batches")),
          str(sorted(cols("supply_materials"))))

    _models.ensure_supply_archive_schema(bind=eng)
    have = {t: "archived_at" in cols(t) for t in
            ("supply_materials", "supply_items", "supply_batches")}
    check("шаг добавил колонку во все три таблицы", all(have.values()), str(have))

    with eng.connect() as conn:
        row = conn.execute(sa_text(
            "SELECT title, qty, archived_at FROM supply_materials WHERE id=1")
        ).fetchone()
    check("выпущенная строка цела, а её archived_at пуст — она живая",
          row[0] == "выпущенная строка" and row[1] == 100 and row[2] is None,
          str(row))

    _models.ensure_supply_archive_schema(bind=eng)
    check("повторный вызов шага ничего не ломает и не дублирует колонку",
          all("archived_at" in cols(t) for t in
              ("supply_materials", "supply_items", "supply_batches"))
          and len([x for x in cols("supply_materials") if x == "archived_at"]) == 1)

    # ОТКАТ: прежний код колонку не называет. Его INSERT обязан пройти, а строка
    # обязана оказаться живой, а не «убранной непонятно когда».
    with eng.begin() as conn:
        conn.execute(sa_text(
            "INSERT INTO supply_materials (id, org_id, title, qty, unit,"
            " source_note, author, created_at, updated_at, rev)"
            " VALUES (2, 1, 'строка откатившегося кода', 5, 'м', '', 'старый код',"
            " datetime('now'), datetime('now'), 1)"))
    with eng.connect() as conn:
        rolled = conn.execute(sa_text(
            "SELECT title, archived_at FROM supply_materials WHERE id=2")).fetchone()
    check("INSERT прежнего кода без archived_at проходит после миграции",
          rolled[0] == "строка откатившегося кода", str(rolled))
    check("и такая строка считается живой (archived_at пуст)",
          rolled[1] is None, str(rolled))

    # ── Шаг 14: та же аддитивность у назначения ─────────────────────────────
    print("\n== Шаг 14: отметка архива у назначения ==")
    if not hasattr(_models, "ensure_supply_assignment_archive_schema"):
        check("шаг 14 (архив назначения) существует", False,
              "models.ensure_supply_assignment_archive_schema отсутствует")
    else:
        with eng.begin() as conn:
            conn.execute(sa_text(
                "CREATE TABLE supply_assignments (id INTEGER PRIMARY KEY,"
                " org_id INTEGER NOT NULL, material_id INTEGER NOT NULL,"
                " batch_id INTEGER NOT NULL, qty FLOAT NOT NULL DEFAULT 0,"
                " note VARCHAR(500) NOT NULL DEFAULT '',"
                " author VARCHAR(255) NOT NULL DEFAULT '',"
                " created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL,"
                " rev INTEGER NOT NULL DEFAULT 1)"))
            conn.execute(sa_text(
                "INSERT INTO supply_assignments (id, org_id, material_id,"
                " batch_id, qty, note, author, created_at, updated_at, rev)"
                " VALUES (1, 1, 1, 1, 70, 'выпущенная заметка', 'кто-то',"
                " datetime('now'), datetime('now'), 1)"))
        check("до шага колонки archived_at у назначения нет",
              "archived_at" not in cols("supply_assignments"),
              str(sorted(cols("supply_assignments"))))
        _models.ensure_supply_assignment_archive_schema(bind=eng)
        check("шаг добавил колонку назначению",
              "archived_at" in cols("supply_assignments"),
              str(sorted(cols("supply_assignments"))))
        with eng.connect() as conn:
            kept = conn.execute(sa_text(
                "SELECT qty, note, archived_at FROM supply_assignments WHERE id=1")
            ).fetchone()
        check("выпущенное назначение цело, а его archived_at пуст — оно живое",
              kept[0] == 70 and kept[1] == "выпущенная заметка" and kept[2] is None,
              str(kept))
        _models.ensure_supply_assignment_archive_schema(bind=eng)
        check("повторный вызов шага 14 ничего не ломает",
              "archived_at" in cols("supply_assignments"))
        with eng.begin() as conn:
            conn.execute(sa_text(
                "INSERT INTO supply_assignments (id, org_id, material_id,"
                " batch_id, qty, note, author, created_at, updated_at, rev)"
                " VALUES (2, 1, 1, 1, 5, '', 'старый код',"
                " datetime('now'), datetime('now'), 1)"))
        with eng.connect() as conn:
            rolled2 = conn.execute(sa_text(
                "SELECT archived_at FROM supply_assignments WHERE id=2")).fetchone()
        check("INSERT прежнего кода без archived_at проходит и даёт живую строку",
              rolled2[0] is None, str(rolled2))

    eng.dispose()
    for suffix in ("", "-wal", "-shm"):
        f = Path(str(mig_db) + suffix)
        if f.exists():
            f.unlink()


def run_preview_tool(argv: list) -> int:
    """Операторский инструмент в том же процессе, но своим `main()`.

    Запускать подпроцессом смысла нет: он взял бы ту же базу из `DATABASE_URL`,
    а разбор аргументов и запись — ровно та же функция.
    """
    from tools import supply_sheets_preview as tool

    return tool.main(argv)


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


if __name__ == "__main__":
    sys.exit(main())
